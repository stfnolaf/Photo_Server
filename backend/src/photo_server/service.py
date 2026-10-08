import hashlib
import json
import os
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID, uuid4, uuid5

from photo_server.canonical import CanonicalPublisher
from photo_server.catalog import Catalog
from photo_server.config import LibraryError, Settings
from photo_server.manifests import AssetManifest, decode_asset_manifest
from photo_server.models import Blob, Manifest
from photo_server.processing import STAGE_JOBS, extract_metadata
from photo_server.selection import plan_import, role
from photo_server.storage import CHUNK, Storage


class Service:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.storage = Storage(settings)
        self.publisher = CanonicalPublisher(self.storage)
        self.catalog = Catalog(settings.database_url, settings)
        self.library_id: UUID | None = None
        self.scratch = settings.data_dir / "scratch"
        self.scratch.mkdir(parents=True, exist_ok=True)

    def canonical_asset(self, asset_id: str | UUID) -> AssetManifest:
        """Load the current S3 asset manifest for byte-serving runtime paths.

        PostgreSQL remains the projection used for indexed queries.
        Runtime bytes always resolve through the canonical manifest.
        """
        asset_id = str(asset_id)
        prefix = f"manifests/assets/{asset_id}/"
        revisions = []
        for key in self.storage.keys(prefix):
            suffix = key.removeprefix(prefix).removesuffix(".json")
            if suffix.isdigit():
                revisions.append((int(suffix), key))
        if not revisions:
            raise LibraryError(f"Canonical asset manifest is missing: {asset_id}")
        _, key = max(revisions)
        return decode_asset_manifest(self.storage.read_bytes(key))

    def publish_fingerprint(self, asset_id: str, fingerprint):
        from photo_server.burst_authority import BurstAuthority

        return BurstAuthority(self).publish_fingerprint(asset_id, fingerprint)

    def recluster_bursts(self) -> dict:
        from photo_server.burst_authority import BurstAuthority

        return BurstAuthority(self).refresh(uuid4(), clear_exclusions=True)

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
            if marker:
                proposed_id = UUID(marker["libraryId"])
            else:
                from photo_server.rebuild import discover_library_id

                manifest_id = discover_library_id(self.storage)
                if manifest_id:
                    proposed_id = manifest_id
                elif database_id:
                    raise LibraryError(
                        "Canonical S3 library identity is missing for the existing database projection"
                    )
                else:
                    proposed_id = uuid4()
            self.library_id = proposed_id
            database_migrations = self.catalog.initialize(str(self.library_id))
            self.library_id = self.catalog.library_id()
            if marker and UUID(marker["libraryId"]) != self.library_id:
                raise LibraryError("The database projection and canonical S3 library belong to different libraries")
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
        """Verify canonical manifests and their referenced S3 objects."""
        errors, checked = [], 0
        projections = self.catalog.all_assets()
        asset_ids = {manifest.asset_id for manifest in projections}
        manifests = []
        for projection in projections:
            try:
                manifests.append(self.canonical_asset(projection.asset_id))
            except Exception as error:
                errors.append({"key": f"asset:{projection.asset_id}", "error": str(error)})
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
            "assetsChecked": len(projections),
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
            self.publisher.assert_operation_input(operation_id, "batch", batch_inputs)
        except Exception as error:
            # Preserve the existing import API's per-entry failure response.
            batch_error = error
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
            self.publisher.assert_operation_input(operation_id, entry["path"], fingerprints)
            manifest = self.catalog.get(str(asset_id))
            if manifest:
                stored = [
                    {"name": blob.original_filename, "sha256": blob.sha256, "size": blob.size_bytes}
                    for blob in manifest.blobs
                ]
                if stored != fingerprints:
                    raise LibraryError("Operation ID was reused with changed file content")
                canonical = self.canonical_asset(asset_id)
                canonical_fingerprints = [
                    {"name": blob.original_filename, "sha256": blob.sha256, "size": blob.size_bytes}
                    for blob in canonical.blobs
                ]
                if canonical.operation_id != operation_id or canonical_fingerprints != fingerprints:
                    raise LibraryError("Database projection conflicts with the canonical import manifest")
                # Re-uploading identical source bytes may repair a missing
                # content object, but the canonical manifest is never derived
                # from the database projection.
                for path, digest, size in files:
                    self.publisher.publish_object(path, digest, size)
                return {
                    "status": "imported",
                    "assetId": str(manifest.asset_id),
                    "replayed": True,
                }

            manifest = self.catalog.find_hash(files[0][1])
            if manifest:
                canonical = self.canonical_asset(manifest.asset_id)
                existing_sidecars = {
                    blob.sha256 for blob in canonical.blobs if blob.role == "SIDECAR"
                }
                if any(digest not in existing_sidecars for _, digest, _ in files[1:]):
                    raise LibraryError(
                        "Original already exists with different sidecars; metadata merging is not implemented"
                    )
                status = "duplicate"
            else:
                # A prior attempt may have completed the canonical S3 import
                # and failed while applying PostgreSQL. Reuse that immutable
                # revision and its timestamps instead of rebuilding a new
                # manifest that would conflict on the same S3 key.
                canonical_key = f"manifests/assets/{asset_id}/1.json"
                canonical = None
                if self.storage.head(canonical_key) is not None:
                    canonical = decode_asset_manifest(self.storage.read_bytes(canonical_key))
                    if canonical.operation_id != operation_id:
                        raise LibraryError("Immutable import manifest conflicts with this operation")
                    canonical_fingerprints = [
                        {"name": blob.original_filename, "sha256": blob.sha256, "size": blob.size_bytes}
                        for blob in canonical.blobs
                    ]
                    if canonical_fingerprints != fingerprints:
                        raise LibraryError("Operation ID was reused with changed file content")
                    manifest = self.publisher.projection_from_manifest(canonical)
                    for path, digest, size in files:
                        self.publisher.publish_object(path, digest, size)
                    self.publisher.publish_manifest(canonical)
                    try:
                        self.catalog.apply(manifest)
                    except Exception as error:
                        self.publisher.record_reconciliation(
                            operation_id,
                            {"status": "canonical-written-projection-failed", "assetId": str(asset_id), "error": str(error)},
                        )
                        raise
                    return {"status": "imported", "assetId": str(asset_id), "replayed": True}

                info, mime = extract_metadata(self, files[0][0])
                imported_blobs = []
                for index, (path, digest, size) in enumerate(files):
                    blob = Blob(
                        blob_id=uuid5(asset_id, path.name),
                        role=role(path),
                        original_filename=path.name,
                        object_key=f"objects/{digest}",
                        sha256=digest,
                        size_bytes=size,
                        mime_type=mime if index == 0 else "application/rdf+xml",
                    )
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
                # Publish and verify canonical bytes before the PostgreSQL
                # projection. A DB failure leaves the import recoverable.
                for path, digest, size in files:
                    self.publisher.publish_object(path, digest, size)
                canonical = self.publisher.manifest_from_projection(manifest)
                self.publisher.publish_manifest(canonical)
                manifest = self.publisher.projection_from_manifest(canonical)
                try:
                    self.catalog.apply(manifest)
                except Exception as error:
                    self.publisher.record_reconciliation(
                        operation_id,
                        {"status": "canonical-written-projection-failed", "assetId": str(asset_id), "error": str(error)},
                    )
                    raise
                status = "imported"
            return {"status": status, "assetId": str(manifest.asset_id), "replayed": False}
