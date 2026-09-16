import asyncio
import hashlib
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from time import time
from uuid import UUID, uuid4, uuid5

from botocore.exceptions import ClientError

from photo_server.config import LibraryError
from photo_server.models import Blob, Manifest
from photo_server.processing import extract_metadata
from photo_server.selection import plan_names, role


class UploadGate:
    """Bound active S3 transfers; excess HTTP requests wait without blocking the event loop."""

    def __init__(self, workers: int):
        self.workers = workers
        self._semaphore = asyncio.Semaphore(workers)
        self._lock = asyncio.Lock()
        self.active = 0
        self.waiting = 0

    @asynccontextmanager
    async def slot(self):
        async with self._lock:
            self.waiting += 1
        acquired = False
        try:
            await self._semaphore.acquire()
            acquired = True
            async with self._lock:
                self.waiting -= 1
                self.active += 1
            yield
        finally:
            async with self._lock:
                if acquired:
                    self.active -= 1
                else:
                    self.waiting -= 1
            if acquired:
                self._semaphore.release()

    def status(self) -> dict:
        return {
            "uploadWorkers": self.workers,
            "uploadsActive": self.active,
            "uploadsWaiting": self.waiting,
        }


def _records(batch_id: UUID, declaration: dict) -> list[dict]:
    skipped = {item["path"]: item["reason"] for item in declaration["plan"]["skipped"]}
    required = {
        path
        for asset in declaration["plan"]["assets"]
        for path in [asset["path"], *asset["sidecars"]]
    }
    records = []
    for file in declaration["files"]:
        file_id = uuid5(batch_id, f"file:{file['path']}")
        records.append(
            {
                "id": str(file_id),
                "batch_id": str(batch_id),
                "relative_path": file["path"],
                "original_filename": PurePosixPath(file["path"]).name,
                "size_bytes": file["sizeBytes"],
                "mime_type": file.get("mimeType"),
                "staging_key": f"incoming/{batch_id}/{file_id}",
                "required": 1 if file["path"] in required else 0,
                "status": "waiting" if file["path"] in required else "skipped",
                "reason": skipped.get(
                    file["path"],
                    "unassigned_sidecar" if file["path"] not in required else None,
                ),
            }
        )
    return records


def create_batch(service, files: list[dict], batch_id: UUID | None = None) -> dict:
    batch_id = batch_id or uuid4()
    if len(files) > service.settings.max_batch_files:
        raise LibraryError(f"A batch may contain at most {service.settings.max_batch_files} files")
    normalized = []
    for file in files:
        path = PurePosixPath(file["path"]).as_posix()
        size = file["sizeBytes"]
        if size <= 0 or size > service.settings.max_file_bytes:
            raise LibraryError(f"File size is outside the configured limit: {path}")
        normalized.append({"path": path, "sizeBytes": size, "mimeType": file.get("mimeType")})
    plan = plan_names([file["path"] for file in normalized], service.settings.max_batch_files)
    if not plan["assets"]:
        raise LibraryError("A batch must contain at least one supported photo")
    proposed = {
        "schemaVersion": 1,
        "libraryId": str(service.library_id),
        "batchId": str(batch_id),
        "createdAt": datetime.now(UTC).isoformat(),
        "files": normalized,
        "plan": plan,
    }
    service.catalog.create_upload_batch(batch_id, _records(batch_id, proposed))
    return describe_batch(service, batch_id)


def describe_batch(service, batch_id: UUID | str) -> dict:
    value = service.catalog.upload_batch(batch_id)
    if value is None:
        raise LibraryError("Upload batch not found")
    batch = value["batch"]
    files = [
        {
            "fileId": row["id"],
            "path": row["relative_path"],
            "sizeBytes": row["size_bytes"],
            "mimeType": row["mime_type"],
            "required": bool(row["required"]),
            "status": row["status"],
            "reason": row["reason"],
            "assetId": row["asset_id"],
            "error": row["error"],
            "uploadUrl": f"/upload-batches/{batch_id}/files/{row['id']}"
            if row["required"]
            else None,
        }
        for row in value["files"]
    ]
    return {
        "batchId": str(batch_id),
        "status": batch["status"],
        "createdAt": batch["created_at"],
        "sealedAt": batch["sealed_at"],
        "files": files,
        "jobs": [
            {
                "jobId": row["id"],
                "status": row["status"],
                "attempts": row["attempts"],
                "result": row["result"],
                "error": row["error"],
            }
            for row in value["jobs"]
        ],
    }


def list_active_batches(service, limit: int = 100) -> list[dict]:
    return [describe_batch(service, batch_id) for batch_id in service.catalog.active_upload_batch_ids(limit)]


async def receive_file(
    service,
    gate: UploadGate,
    batch_id: UUID,
    file_id: UUID,
    stream,
    content_length: int | None,
) -> dict:
    async with gate.slot():
        row = await asyncio.to_thread(service.catalog.begin_upload, batch_id, file_id)
        if row["status"] == "uploaded":
            return {
                "fileId": str(file_id),
                "status": "uploaded",
                "sha256": row["sha256"],
                "replayed": True,
            }
        expected = row["size_bytes"]
        if content_length is not None and content_length != expected:
            await asyncio.to_thread(
                service.catalog.fail_upload,
                file_id,
                "Content-Length does not match the declaration",
            )
            raise LibraryError("Content-Length does not match the batch declaration")
        existing = await asyncio.to_thread(service.storage.head, row["staging_key"])
        if existing is not None:
            try:
                if existing["ContentLength"] != expected:
                    raise LibraryError("A conflicting immutable staging object already exists")
                digest = await asyncio.to_thread(_hash_object, service, row["staging_key"])
                await asyncio.to_thread(service.catalog.complete_upload, file_id, digest)
                return {
                    "fileId": str(file_id),
                    "status": "uploaded",
                    "sha256": digest,
                    "replayed": True,
                }
            except BaseException as error:
                await asyncio.to_thread(service.catalog.fail_upload, file_id, str(error))
                raise

        client = service.storage.client
        upload_id = None
        try:
            created = await asyncio.to_thread(
                client.create_multipart_upload,
                Bucket=service.storage.bucket,
                Key=row["staging_key"],
                ContentType=row["mime_type"] or "application/octet-stream",
                Metadata={"declared-size": str(expected)},
            )
            upload_id = created["UploadId"]
            parts, buffer, digest, total = [], bytearray(), hashlib.sha256(), 0

            async def send_part(data: bytes):
                number = len(parts) + 1
                response = await asyncio.to_thread(
                    client.upload_part,
                    Bucket=service.storage.bucket,
                    Key=row["staging_key"],
                    UploadId=upload_id,
                    PartNumber=number,
                    Body=data,
                )
                parts.append({"PartNumber": number, "ETag": response["ETag"]})

            async for chunk in stream:
                if not chunk:
                    continue
                total += len(chunk)
                if total > expected or total > service.settings.max_file_bytes:
                    raise LibraryError("Upload exceeds its declared or configured size")
                digest.update(chunk)
                buffer.extend(chunk)
                while len(buffer) >= service.settings.upload_part_bytes:
                    data = bytes(buffer[: service.settings.upload_part_bytes])
                    del buffer[: service.settings.upload_part_bytes]
                    await send_part(data)
            if buffer:
                await send_part(bytes(buffer))
            if total != expected or not parts:
                raise LibraryError("Upload size does not match the batch declaration")
            try:
                await asyncio.to_thread(
                    client.complete_multipart_upload,
                    Bucket=service.storage.bucket,
                    Key=row["staging_key"],
                    UploadId=upload_id,
                    MultipartUpload={"Parts": parts},
                    IfNoneMatch="*",
                )
            except ClientError as error:
                if error.response["ResponseMetadata"]["HTTPStatusCode"] != 412:
                    raise
                actual = await asyncio.to_thread(_hash_object, service, row["staging_key"])
                if actual != digest.hexdigest():
                    raise LibraryError(
                        "A conflicting immutable staging object already exists"
                    ) from error
            upload_id = None
            await asyncio.to_thread(
                service.storage.verify, row["staging_key"], expected, digest.hexdigest()
            )
            await asyncio.to_thread(service.catalog.complete_upload, file_id, digest.hexdigest())
            return {
                "fileId": str(file_id),
                "status": "uploaded",
                "sha256": digest.hexdigest(),
                "replayed": False,
            }
        except BaseException as error:
            if upload_id:
                try:
                    await asyncio.to_thread(
                        client.abort_multipart_upload,
                        Bucket=service.storage.bucket,
                        Key=row["staging_key"],
                        UploadId=upload_id,
                    )
                except Exception:
                    pass
            await asyncio.to_thread(service.catalog.fail_upload, file_id, str(error))
            raise


def _hash_object(service, key: str) -> str:
    digest = hashlib.sha256()
    for chunk in service.storage.chunks(key):
        digest.update(chunk)
    return digest.hexdigest()


def seal_batch(service, batch_id: UUID) -> dict:
    batch = service.catalog.upload_batch(batch_id)
    if batch is None:
        raise LibraryError("Upload batch not found")
    missing = [
        row["relative_path"]
        for row in batch["files"]
        if row["required"] and row["status"] != "uploaded"
    ]
    if missing:
        raise LibraryError(f"Upload these required files before sealing: {missing}")
    plan = plan_names(
        [row["relative_path"] for row in batch["files"]], service.settings.max_batch_files
    )
    service.catalog.seal_upload_batch(batch_id, plan)
    return describe_batch(service, batch_id)


def _delete_claimed_batch(service, claimed: dict) -> dict:
    batch_id = claimed["batchId"]
    prefix = f"incoming/{batch_id}/"
    aborted = service.storage.abort_multipart_uploads(prefix)
    for key in claimed["stagingKeys"]:
        service.storage.delete(key)
    service.catalog.finish_upload_batch_cleanup(batch_id)
    return {
        "batchId": batch_id,
        "status": "deleted",
        "filesDeleted": len(claimed["stagingKeys"]),
        "multipartUploadsAborted": aborted,
    }


def abandon_batch(service, batch_id: UUID) -> dict:
    """Immediately discard an unsealed batch after its active requests have stopped."""
    return _delete_claimed_batch(service, service.catalog.claim_upload_batch_cleanup(batch_id))


def cleanup_abandoned_batches(service, limit: int = 100) -> dict:
    """Delete stale unsealed batches while retaining every sealed batch for retry."""
    cutoff = int(time()) - service.settings.upload_abandon_seconds
    batches = files = multipart = 0
    for _ in range(limit):
        claimed = service.catalog.claim_abandoned_upload_batch(cutoff)
        if claimed is None:
            break
        result = _delete_claimed_batch(service, claimed)
        batches += 1
        files += result["filesDeleted"]
        multipart += result["multipartUploadsAborted"]
    return {
        "batchesDeleted": batches,
        "filesDeleted": files,
        "multipartUploadsAborted": multipart,
    }


def process_onboarding_job(service, job: dict) -> dict:
    ordered_ids = [job["primary_file_id"], *job["sidecar_file_ids"]]
    rows = [job["files"][file_id] for file_id in ordered_ids]
    try:
        with TemporaryDirectory(dir=service.scratch) as directory:
            staged = []
            for row in rows:
                path = Path(directory) / row["original_filename"]
                digest, size = hashlib.sha256(), 0
                with path.open("wb") as output:
                    for chunk in service.storage.chunks(row["staging_key"]):
                        digest.update(chunk)
                        size += len(chunk)
                        output.write(chunk)
                if size != row["size_bytes"] or (
                    row["sha256"] and digest.hexdigest() != row["sha256"]
                ):
                    raise LibraryError(f"Staged upload failed verification: {row['relative_path']}")
                staged.append((path, digest.hexdigest(), size))
            result = _commit_staged(service, job, staged)
        service.catalog.finish_onboarding_job(job, result=result)
        _delete_staging(service, job)
        return result
    except Exception as error:
        service.catalog.finish_onboarding_job(job, error=str(error))
        return {"status": "failed", "error": str(error), "jobId": job["id"]}


def _delete_staging(service, job: dict):
    """Cleanup is best-effort after final objects and PostgreSQL state commit."""
    for file_id in [job["primary_file_id"], *job["sidecar_file_ids"]]:
        try:
            service.storage.delete(job["files"][file_id]["staging_key"])
        except Exception:
            pass


def _commit_staged(service, job: dict, files: list[tuple[Path, str, int]]) -> dict:
    assert service.library_id is not None
    operation_id = UUID(job["id"])
    asset_id = uuid5(service.library_id, f"upload:{job['id']}")
    fingerprints = [
        {"name": path.name, "sha256": digest, "size": size} for path, digest, size in files
    ]
    with service.catalog.writer(), service.catalog.digest_lock(files[0][1]):
        manifest = service.catalog.get(str(asset_id))
        if manifest:
            stored = [
                {"name": blob.original_filename, "sha256": blob.sha256, "size": blob.size_bytes}
                for blob in manifest.blobs
            ]
            if stored != fingerprints:
                raise LibraryError("Onboarding job conflicts with an existing asset")
            status = "imported"
            replayed = True
        else:
            manifest = service.catalog.find_hash(files[0][1])
            if manifest:
                existing_sidecars = {
                    blob.sha256 for blob in manifest.blobs if blob.role == "SIDECAR"
                }
                if any(digest not in existing_sidecars for _, digest, _ in files[1:]):
                    raise LibraryError(
                        "Original already exists with different sidecars; metadata merging is not implemented"
                    )
                status = "duplicate"
                replayed = False
            else:
                info, mime = extract_metadata(service, files[0][0])
                imported_blobs = []
                for index, (path, digest, size) in enumerate(files):
                    blob = Blob(
                        blob_id=uuid5(asset_id, path.name),
                        role=role(path),
                        original_filename=path.name,
                        object_key=f"originals/{asset_id}/{path.name}",
                        sha256=digest,
                        size_bytes=size,
                        mime_type=mime if index == 0 else "application/rdf+xml",
                    )
                    with path.open("rb") as stream:
                        service.storage.put(
                            blob.object_key, stream, blob.mime_type, {"sha256": digest}
                        )
                    service.storage.verify(blob.object_key, size, digest)
                    imported_blobs.append(blob)
                manifest = Manifest(
                    library_id=service.library_id,
                    asset_id=asset_id,
                    operation_id=operation_id,
                    primary_blob_id=imported_blobs[0].blob_id,
                    blobs=imported_blobs,
                    imported_at=datetime.now(UTC).isoformat(),
                    capture_time=info.get("captureTime"),
                    metadata=info,
                )
                service.catalog.apply(manifest)
                status = "imported"
                replayed = False
    return {"status": status, "assetId": str(manifest.asset_id), "replayed": replayed}
