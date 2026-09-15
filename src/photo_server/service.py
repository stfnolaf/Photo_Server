import hashlib
import os
import re
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID, uuid4, uuid5

from photo_server import metadata
from photo_server.catalog import Catalog
from photo_server.config import LibraryError, Settings
from photo_server.models import Blob, Manifest
from photo_server.selection import plan_import, role
from photo_server.storage import CHUNK, Storage


class Service:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.storage = Storage(settings)
        self.catalog = Catalog(settings.database_url)
        self.library_id: UUID | None = None
        self.scratch = settings.data_dir / "scratch"
        self.scratch.mkdir(parents=True, exist_ok=True)

    def initialize(self) -> dict:
        with self.catalog.writer():
            self.storage.ensure_bucket()
            if self.storage.head("library.json") is None:
                from photo_server.storage import canonical_json

                self.storage.put(
                    "library.json",
                    canonical_json({"schemaVersion": 1, "libraryId": str(uuid4())}),
                    "application/json",
                )
            marker = self.storage.get_json("library.json")
            if marker.get("schemaVersion") != 1:
                raise LibraryError("Unsupported S3 library schema")
            self.library_id = UUID(marker["libraryId"])
            self.catalog.initialize(str(self.library_id))
        return {"libraryId": str(self.library_id), "bucket": self.storage.bucket}

    def plan(self, paths: list[str]) -> dict:
        return plan_import(self.settings, paths)

    @contextmanager
    def stage(self, relative: str):
        root = self.settings.import_root.resolve(strict=True)
        source = (root / relative).resolve(strict=True)
        if not source.is_relative_to(root) or not source.is_file():
            raise LibraryError("Source is outside the import root or is not a regular file")
        with TemporaryDirectory(dir=self.scratch) as directory:
            staged = Path(directory) / source.name
            digest, size = hashlib.sha256(), 0
            with source.open("rb") as incoming, staged.open("wb") as outgoing:
                before = os.fstat(incoming.fileno())
                if before.st_size > self.settings.max_file_bytes:
                    raise LibraryError(f"File exceeds configured size limit: {relative}")
                while chunk := incoming.read(CHUNK):
                    size += len(chunk)
                    if size > self.settings.max_file_bytes:
                        raise LibraryError(f"File exceeds configured size limit: {relative}")
                    digest.update(chunk)
                    outgoing.write(chunk)
                after = os.fstat(incoming.fileno())
            if not size or (before.st_size, before.st_mtime_ns) != (
                after.st_size,
                after.st_mtime_ns,
            ):
                raise LibraryError(f"Source is empty or changed during import: {relative}")
            yield staged, digest.hexdigest(), size

    def _load_manifest(self, key: str, full: bool = False) -> Manifest:
        manifest = Manifest.model_validate(self.storage.get_json(key))
        if manifest.key != key or manifest.library_id != self.library_id:
            raise LibraryError(f"Manifest key/library mismatch: {key}")
        for blob in manifest.blobs:
            self.storage.verify(blob.object_key, blob.size_bytes, blob.sha256, full=full)
        return manifest

    def _recover(self, full: bool) -> dict:
        latest = {}
        errors = []
        for key in self.storage.keys("state/assets/"):
            match = re.fullmatch(r"state/assets/([0-9a-f-]+)/([0-9]{8})\.json", key)
            if not match:
                errors.append({"key": key, "error": "Unrecognized manifest key"})
                continue
            asset, revision = match.groups()
            if asset not in latest or revision > latest[asset][0]:
                latest[asset] = (revision, key)
        recovered = 0
        for _, key in latest.values():
            try:
                manifest = self._load_manifest(key, full)
                self.catalog.apply(manifest)
                recovered += 1
            except Exception as error:
                errors.append({"key": key, "error": str(error)})
        return {
            "recovered": recovered,
            "verification": "sha256" if full else "size",
            "errors": errors,
        }

    def recover(self, full: bool = False) -> dict:
        with self.catalog.writer():
            return self._recover(full)

    def import_batch(self, paths: list[str], operation_id: UUID) -> dict:
        plan = self.plan(paths)
        with self.catalog.writer():
            reconciliation = self._recover(False)
            if reconciliation["errors"]:
                raise LibraryError(
                    f"Resolve recovery errors before importing: {reconciliation['errors']}"
                )
            intent = {
                "schemaVersion": 1,
                "libraryId": str(self.library_id),
                "operationId": str(operation_id),
                "plan": plan,
            }
            self.storage.put_json(f"imports/{operation_id}/intent.json", intent)
            results = []
            for entry in plan["assets"]:
                try:
                    result = self._import_asset(entry, operation_id)
                    results.append({"path": entry["path"], **result})
                except Exception as error:
                    results.append({"path": entry["path"], "status": "failed", "error": str(error)})
                    # A commit may have reached S3. Do not construct later state from a stale DB.
                    break
            completed = {entry["path"] for entry in results}
            results.extend(
                {"path": entry["path"], "status": "not_attempted"}
                for entry in plan["assets"]
                if entry["path"] not in completed
            )
            successful = {
                entry["path"] for entry in results if entry["status"] in {"imported", "duplicate"}
            }
            skipped = [
                {**entry, "status": "skipped" if entry["selected"] in successful else "deferred"}
                for entry in plan["skipped"]
            ]
            return {
                "operationId": str(operation_id),
                "results": results,
                "skipped": skipped,
                "warnings": plan["warnings"],
            }

    def _import_asset(self, entry: dict, operation_id: UUID) -> dict:
        from contextlib import ExitStack

        assert self.library_id is not None
        asset_id = uuid5(self.library_id, f"{operation_id}:{entry['path']}")
        manifest_key = f"state/assets/{asset_id}/00000001.json"
        receipt_key = f"imports/{operation_id}/results/{asset_id}.json"
        with ExitStack() as stack:
            files = [
                stack.enter_context(self.stage(path))
                for path in [entry["path"], *entry["sidecars"]]
            ]
            fingerprints = [
                {"name": path.name, "sha256": digest, "size": size} for path, digest, size in files
            ]
            if self.storage.head(receipt_key):
                receipt = self.storage.get_json(receipt_key)
                if receipt["inputs"] != fingerprints:
                    raise LibraryError("Operation ID was reused with changed file content")
                manifest = self._load_manifest(receipt["manifestKey"])
                self.catalog.apply(manifest)
                return {
                    "status": receipt["status"],
                    "assetId": str(manifest.asset_id),
                    "replayed": True,
                }

            if self.storage.head(manifest_key):
                manifest = self._load_manifest(manifest_key, full=True)
                durable_fingerprints = [
                    {"name": blob.original_filename, "sha256": blob.sha256, "size": blob.size_bytes}
                    for blob in manifest.blobs
                ]
                if durable_fingerprints != fingerprints:
                    raise LibraryError("Operation ID was reused with changed file content")
                status = "imported"
            else:
                manifest = self.catalog.find_hash(files[0][1])
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
                    info, mime = metadata.extract(files[0][0], self.settings.exiftool)
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
                            self.storage.put(
                                blob.object_key, stream, blob.mime_type, {"sha256": digest}
                            )
                        self.storage.verify(blob.object_key, size, digest)
                        imported_blobs.append(blob)
                    manifest = Manifest(
                        library_id=self.library_id,
                        asset_id=asset_id,
                        operation_id=operation_id,
                        primary_blob_id=imported_blobs[0].blob_id,
                        blobs=imported_blobs,
                        imported_at=datetime.now(UTC).isoformat(),
                        capture_time=info.get("captureTime"),
                        metadata=info,
                    )
                    self.storage.put_json(manifest.key, manifest.document())
                    status = "imported"

            self.storage.put_json(
                receipt_key,
                {
                    "schemaVersion": 1,
                    "inputs": fingerprints,
                    "manifestKey": manifest.key,
                    "status": status,
                },
            )
            self.catalog.apply(manifest)
            return {"status": status, "assetId": str(manifest.asset_id), "replayed": False}
