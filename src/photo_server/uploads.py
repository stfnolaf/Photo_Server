import asyncio
import hashlib
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from uuid import UUID, uuid4, uuid5

from botocore.exceptions import ClientError

from photo_server import metadata
from photo_server.config import LibraryError
from photo_server.models import Blob, Manifest
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


def declaration_key(batch_id: UUID | str) -> str:
    return f"upload-batches/{batch_id}/declaration.json"


def sealed_key(batch_id: UUID | str) -> str:
    return f"upload-batches/{batch_id}/sealed.json"


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
    key = declaration_key(batch_id)
    if service.storage.head(key):
        declaration = service.storage.get_json(key)
        comparable = {name: declaration.get(name) for name in proposed if name != "createdAt"}
        expected = {name: proposed[name] for name in proposed if name != "createdAt"}
        if comparable != expected:
            raise LibraryError("Batch ID was reused with a different file declaration")
    else:
        declaration = proposed
        try:
            service.storage.put_json(key, declaration)
        except LibraryError:
            # Another request may have created the same caller-supplied batch ID.
            declaration = service.storage.get_json(key)
            comparable = {name: declaration.get(name) for name in proposed if name != "createdAt"}
            expected = {name: proposed[name] for name in proposed if name != "createdAt"}
            if comparable != expected:
                raise
    service.catalog.create_upload_batch(batch_id, _records(batch_id, declaration))
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
    declaration = service.storage.get_json(declaration_key(batch_id))
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
    service.storage.put_json(
        sealed_key(batch_id),
        {"schemaVersion": 1, "libraryId": str(service.library_id), "batchId": str(batch_id)},
    )
    service.catalog.seal_upload_batch(batch_id, declaration["plan"])
    return describe_batch(service, batch_id)


def recover_upload_batches(service) -> dict:
    recovered, errors = 0, []
    for key in service.storage.keys("upload-batches/"):
        match = re.fullmatch(r"upload-batches/([0-9a-f-]+)/declaration\.json", key)
        if not match:
            continue
        try:
            batch_id = UUID(match.group(1))
            declaration = service.storage.get_json(key)
            if declaration["libraryId"] != str(service.library_id) or declaration["batchId"] != str(
                batch_id
            ):
                raise LibraryError("Upload declaration library or key mismatch")
            records = _records(batch_id, declaration)
            service.catalog.create_upload_batch(batch_id, records)
            state = service.catalog.upload_batch(batch_id)
            by_id = {row["id"]: row for row in state["files"]}
            is_sealed = service.storage.head(sealed_key(batch_id)) is not None

            if is_sealed:
                for asset in declaration["plan"]["assets"]:
                    job_id = uuid5(batch_id, f"onboard:{asset['path']}")
                    receipt_key = f"upload-batches/{batch_id}/results/{job_id}.json"
                    if not service.storage.head(receipt_key):
                        continue
                    receipt = service.storage.get_json(receipt_key)
                    paths = [asset["path"], *asset["sidecars"]]
                    inputs = receipt.get("inputs", [])
                    if len(paths) != len(inputs):
                        raise LibraryError(f"Onboarding receipt input mismatch: {receipt_key}")
                    for path, fingerprint in zip(paths, inputs, strict=True):
                        record = next(item for item in records if item["relative_path"] == path)
                        if (
                            fingerprint.get("name") != PurePosixPath(path).name
                            or fingerprint.get("size") != record["size_bytes"]
                        ):
                            raise LibraryError(f"Onboarding receipt input mismatch: {receipt_key}")
                        service.catalog.complete_upload(record["id"], fingerprint["sha256"])

            for record in records:
                status = by_id[record["id"]]["status"]
                if not record["required"] or status not in {"waiting", "uploading"}:
                    continue
                head = service.storage.head(record["staging_key"])
                if head and head["ContentLength"] == record["size_bytes"]:
                    service.catalog.complete_upload(record["id"], None)
                elif status == "uploading":
                    service.catalog.fail_upload(record["id"], "Interrupted upload; retry the file")
            if is_sealed:
                service.catalog.seal_upload_batch(batch_id, declaration["plan"])
            recovered += 1
        except Exception as error:
            errors.append({"key": key, "error": str(error)})
    return {"uploadBatchesRecovered": recovered, "errors": errors}


def process_onboarding_job(service, job: dict) -> dict:
    receipt_key = f"upload-batches/{job['batch_id']}/results/{job['id']}.json"
    if service.storage.head(receipt_key):
        receipt = service.storage.get_json(receipt_key)
        manifest = service._load_manifest(receipt["manifestKey"])
        service.catalog.apply(manifest)
        result = {"status": receipt["status"], "assetId": str(manifest.asset_id), "replayed": True}
        service.catalog.finish_onboarding_job(job, result=result)
        _delete_staging(service, job)
        return result

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
            result = _commit_staged(service, job, staged, receipt_key)
        service.catalog.finish_onboarding_job(job, result=result)
        _delete_staging(service, job)
        return result
    except Exception as error:
        service.catalog.finish_onboarding_job(job, error=str(error))
        return {"status": "failed", "error": str(error), "jobId": job["id"]}


def _delete_staging(service, job: dict):
    """Cleanup is best-effort because final objects and the receipt are already durable."""
    for file_id in [job["primary_file_id"], *job["sidecar_file_ids"]]:
        try:
            service.storage.delete(job["files"][file_id]["staging_key"])
        except Exception:
            pass


def _commit_staged(
    service, job: dict, files: list[tuple[Path, str, int]], receipt_key: str
) -> dict:
    assert service.library_id is not None
    operation_id = UUID(job["id"])
    asset_id = uuid5(service.library_id, f"upload:{job['id']}")
    manifest_key = f"state/assets/{asset_id}/00000001.json"
    fingerprints = [
        {"name": path.name, "sha256": digest, "size": size} for path, digest, size in files
    ]
    with service.catalog.digest_lock(files[0][1]):
        if service.storage.head(manifest_key):
            manifest = service._load_manifest(manifest_key, full=True)
            durable = [
                {"name": blob.original_filename, "sha256": blob.sha256, "size": blob.size_bytes}
                for blob in manifest.blobs
            ]
            if durable != fingerprints:
                raise LibraryError("Onboarding job conflicts with an existing manifest")
            status = "imported"
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
            else:
                info, mime = metadata.extract(files[0][0], service.settings.exiftool)
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
                service.storage.put_json(manifest.key, manifest.document())
                status = "imported"
        service.storage.put_json(
            receipt_key,
            {
                "schemaVersion": 1,
                "inputs": fingerprints,
                "manifestKey": manifest.key,
                "status": status,
            },
        )
        service.catalog.apply(manifest)
    return {"status": status, "assetId": str(manifest.asset_id), "replayed": False}
