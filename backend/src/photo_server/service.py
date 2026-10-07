import hashlib
import json
import os
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID, uuid4, uuid5

from photo_server.catalog import Catalog
from photo_server.config import LibraryError, Settings
from photo_server.dual_write import DualWritePublisher
from photo_server.models import Blob, Manifest
from photo_server.processing import STAGE_JOBS, extract_metadata
from photo_server.selection import plan_import, role
from photo_server.storage import CHUNK, Storage


class Service:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.storage = Storage(settings)
        self.dual_write = DualWritePublisher(self.storage)
        self.catalog = Catalog(settings.database_url, settings)
        self.library_id: UUID | None = None
        self.scratch = settings.data_dir / "scratch"
        self.scratch.mkdir(parents=True, exist_ok=True)

    def authority_status(self) -> dict:
        """Return the persisted cutover status without requiring PostgreSQL."""
        from photo_server.cutover import authority_status

        return authority_status(self)

    def cutover_readiness(self, **kwargs) -> dict:
        from photo_server.cutover import cutover_readiness

        return cutover_readiness(self, **kwargs)

    def activate_s3_authority(self, readiness: dict | None = None) -> dict:
        from photo_server.cutover import CutoverManager

        return CutoverManager(self).activate(readiness)

    def rollback_s3_authority(self, reason: str = "rollback requested") -> dict:
        from photo_server.cutover import CutoverManager

        return CutoverManager(self).rollback(reason)

    def reconcile_authority(self, **kwargs) -> dict:
        from photo_server.cutover import CutoverManager

        return CutoverManager(self).reconcile(**kwargs)

    def initialize(self, recover_uploads: bool = True) -> dict:
        with self.catalog.writer():
            self.storage.ensure_bucket()
            marker = (
                self.storage.get_json("library.json")
                if self.storage.head("library.json") is not None
                else None
            )
            if marker and marker.get("schemaVersion") != 1:
                raise LibraryError("Unsupported S3 library schema")
            database_id = self.catalog.existing_library_id()
            proposed_id = database_id or (UUID(marker["libraryId"]) if marker else uuid4())
            self.library_id = proposed_id
            database_migrations = self.catalog.initialize(str(self.library_id))
            self.library_id = self.catalog.library_id()
            if marker and UUID(marker["libraryId"]) != self.library_id:
                raise LibraryError("PostgreSQL and the S3 media bucket belong to different libraries")
            if marker is None:
                from photo_server.storage import canonical_json

                self.storage.put(
                    "library.json",
                    canonical_json({"schemaVersion": 1, "libraryId": str(self.library_id)}),
                    "application/json",
                )
        interrupted = self.catalog.resume_interrupted_uploads() if recover_uploads else 0
        return {
            "libraryId": str(self.library_id),
            "bucket": self.storage.bucket,
            "databaseMigrations": database_migrations,
            "interruptedUploadsReset": interrupted,
        }

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

    def verify(self, full: bool = False) -> dict:
        """Verify that PostgreSQL's authoritative records reference valid S3 blobs."""
        errors, checked = [], 0
        manifests = self.catalog.all_assets()
        asset_ids = {manifest.asset_id for manifest in manifests}
        for manifest in manifests:
            for blob in manifest.blobs:
                try:
                    self.storage.verify(
                        blob.object_key, blob.size_bytes, blob.sha256, full=full
                    )
                    checked += 1
                except Exception as error:
                    errors.append({"key": blob.object_key, "error": str(error)})
        for album in self.catalog.all_albums():
            for asset_id in album.asset_ids:
                if asset_id not in asset_ids:
                    errors.append(
                        {
                            "key": f"album:{album.album_id}",
                            "error": f"Album references missing asset {asset_id}",
                        }
                    )
        return {
            "assetsChecked": len(manifests),
            "blobsChecked": checked,
            "verification": "sha256" if full else "size",
            "errors": errors,
        }

    def backup_status(self) -> dict:
        status_path = self.settings.postgres_backup_status_path
        try:
            status = json.loads(status_path.read_text())
        except (FileNotFoundError, OSError, ValueError):
            status = None
        if isinstance(status, dict):
            primary = self._backup_destination_status(status.get("primary"))
            secondary = self._backup_destination_status(status.get("secondary"))
            overall = status.get("overallStatus")
            if primary is not None and secondary is not None and overall in {
                "healthy", "degraded", "unavailable"
            }:
                return {
                    "postgresBackupKey": primary.get("key"),
                    "postgresBackupAt": primary.get("at"),
                    "postgresBackupPrimary": primary,
                    "postgresBackupSecondary": secondary,
                    "postgresBackupOverall": overall,
                }

        return self._backup_status_from_primary_storage()

    @staticmethod
    def _backup_destination_status(value: object) -> dict | None:
        if not isinstance(value, dict) or value.get("status") not in {
            "success", "failed", "unavailable", "not-configured"
        }:
            return None
        result = {"status": value["status"]}
        for field in ("key", "at", "errorClass"):
            if field in value:
                if not isinstance(value[field], str):
                    return None
                result[field] = value[field]
        if "verified" in value:
            if not isinstance(value["verified"], bool):
                return None
            result["verified"] = value["verified"]
        return result

    def _backup_status_from_primary_storage(self) -> dict:
        secondary_configured = bool(
            self.settings.postgres_backup_secondary_endpoint
            or self.settings.postgres_backup_secondary_path
        )
        keys = list(self.storage.keys(self.settings.postgres_backup_prefix.rstrip("/") + "/"))
        if not keys:
            return {
                "postgresBackupKey": None,
                "postgresBackupAt": None,
                "postgresBackupPrimary": {"status": "unavailable"},
                "postgresBackupSecondary": {
                    "status": "unavailable" if secondary_configured else "not-configured"
                },
                "postgresBackupOverall": "unavailable",
            }
        key = max(keys)
        head = self.storage.head(key)
        modified = head.get("LastModified") if head else None
        return {
            "postgresBackupKey": key,
            "postgresBackupAt": modified.isoformat() if modified else None,
            "postgresBackupPrimary": {
                "status": "success" if modified else "unavailable",
                "key": key,
                "at": modified.isoformat() if modified else None,
                "verified": True,
            },
            "postgresBackupSecondary": {
                "status": "unavailable" if secondary_configured else "not-configured"
            },
            "postgresBackupOverall": (
                "healthy"
                if modified and not (secondary_configured and self.settings.postgres_backup_secondary_required)
                else "degraded" if modified else "unavailable"
            ),
        }

    def queue_processing(
        self,
        asset_ids: list[UUID] | None = None,
        stages: list[str] | None = None,
        include_deleted: bool = False,
    ) -> dict:
        """Queue reusable processing stages for selected assets or the library."""
        selected_stages = list(dict.fromkeys(stages or STAGE_JOBS))
        unknown = set(selected_stages) - set(STAGE_JOBS)
        if unknown:
            raise LibraryError(f"Unsupported processing stages: {', '.join(sorted(unknown))}")
        return self.catalog.queue_processing(
            [str(asset_id) for asset_id in asset_ids] if asset_ids is not None else None,
            [STAGE_JOBS[stage] for stage in selected_stages],
            include_deleted,
        )

    def queue_analysis(
        self,
        asset_ids: list[UUID] | None = None,
        include_deleted: bool = False,
        force_full: bool = False,
    ) -> dict:
        """Queue local AI analysis independently from ingestion workers."""
        return self.catalog.queue_ai(
            [str(asset_id) for asset_id in asset_ids] if asset_ids is not None else None,
            include_deleted,
            force_full,
        )

    def import_batch(self, paths: list[str], operation_id: UUID) -> dict:
        plan = self.plan(paths)
        batch_inputs = []
        for entry in plan["assets"]:
            for relative in [entry["path"], *entry["sidecars"]]:
                with self.stage(relative) as (_path, digest, size):
                    batch_inputs.append({"name": relative, "sha256": digest, "size": size})
        batch_error = None
        try:
            self.dual_write.assert_operation_input(operation_id, "batch", batch_inputs)
        except Exception as error:
            # Preserve the existing import API's per-entry failure response.
            batch_error = error
        with self.catalog.writer():
            results = []
            if batch_error is not None:
                results.append(
                    {"path": plan["assets"][0]["path"], "status": "failed", "error": str(batch_error)}
                )
            else:
                for entry in plan["assets"]:
                    try:
                        result = self._import_asset(entry, operation_id)
                        results.append({"path": entry["path"], **result})
                    except Exception as error:
                        results.append({"path": entry["path"], "status": "failed", "error": str(error)})
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
        with ExitStack() as stack:
            files = [
                stack.enter_context(self.stage(path))
                for path in [entry["path"], *entry["sidecars"]]
            ]
            fingerprints = [
                {"name": path.name, "sha256": digest, "size": size} for path, digest, size in files
            ]
            self.dual_write.assert_operation_input(operation_id, entry["path"], fingerprints)
            manifest = self.catalog.get(str(asset_id))
            if manifest:
                stored = [
                    {"name": blob.original_filename, "sha256": blob.sha256, "size": blob.size_bytes}
                    for blob in manifest.blobs
                ]
                if stored != fingerprints:
                    raise LibraryError("Operation ID was reused with changed file content")
                # A previous attempt may have committed PostgreSQL and failed
                # during canonical-manifest publication. Re-run the complete
                # immutable publication on every idempotent retry.
                for path, digest, size in files:
                    self.dual_write.publish_object(path, digest, size)
                try:
                    self.dual_write.publish_manifest(
                        self.dual_write.manifest_from_legacy(manifest)
                    )
                except Exception as error:
                    self.dual_write.record_reconciliation(
                        operation_id,
                        {
                            "status": "database-written-manifest-failed",
                            "assetId": str(asset_id),
                            "error": str(error),
                        },
                    )
                    raise
                return {
                    "status": "imported",
                    "assetId": str(manifest.asset_id),
                    "replayed": True,
                }

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
                info, mime = extract_metadata(self, files[0][0])
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
                        self.storage.put(blob.object_key, stream, blob.mime_type, {"sha256": digest})
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
                # Publish canonical bytes before the unchanged PostgreSQL
                # apply. A DB failure leaves content to be completed by retry.
                for path, digest, size in files:
                    self.dual_write.publish_object(path, digest, size)
                canonical = self.dual_write.manifest_from_legacy(manifest)
                if self.settings.authority_mode == "s3":
                    # Phase 10: canonical state must be durable before the
                    # PostgreSQL projection is changed. A projection failure
                    # is repaired by reconciliation or an idempotent retry.
                    self.dual_write.publish_manifest(canonical)
                    try:
                        self.catalog.apply(manifest)
                    except Exception as error:
                        self.dual_write.record_reconciliation(
                            operation_id,
                            {"status": "canonical-written-projection-failed", "assetId": str(asset_id), "error": str(error)},
                        )
                        raise
                else:
                    try:
                        self.catalog.apply(manifest)
                    except Exception as error:
                        self.dual_write.record_reconciliation(
                            operation_id,
                            {"status": "object-written-database-failed", "assetId": str(asset_id), "error": str(error)},
                        )
                        raise
                    try:
                        self.dual_write.publish_manifest(canonical)
                    except Exception as error:
                        self.dual_write.record_reconciliation(
                            operation_id,
                            {"status": "database-written-manifest-failed", "assetId": str(asset_id), "error": str(error)},
                        )
                        raise
                status = "imported"
            return {"status": status, "assetId": str(manifest.asset_id), "replayed": False}
