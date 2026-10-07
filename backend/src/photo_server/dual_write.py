"""Phase 3 S3 publication for newly imported assets."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from photo_server.config import LibraryError
from photo_server.manifests import (
    AssetManifest,
    BlobReference,
    Location,
    UserState,
    canonical_json,
    decode_asset_manifest,
    encode,
    sha256,
)


def _utc(value: str | None) -> str | None:
    if value is None:
        return None
    return value[:-6] + "Z" if value.endswith("+00:00") else value


class DualWriteIntegrityError(LibraryError):
    """An immutable S3 key contains bytes different from the requested bytes."""


class DualWritePublisher:
    """Publish the new canonical representation without changing old reads."""

    def __init__(self, storage):
        self.storage = storage

    def _put_immutable(self, key: str, data: bytes, mime: str) -> None:
        expected = hashlib.sha256(data).hexdigest()
        existing = self.storage.head(key)
        if existing is not None:
            if existing.get("ContentLength") != len(data):
                raise DualWriteIntegrityError(f"Immutable object has incorrect size: {key}")
            try:
                self.storage.verify(key, len(data), expected)
            except LibraryError as error:
                raise DualWriteIntegrityError(f"Immutable object checksum mismatch: {key}") from error
            return
        self.storage.put(key, data, mime)
        self.storage.verify(key, len(data), expected)

    def publish_object(self, path: Path, digest: str, size: int) -> str:
        data = path.read_bytes()
        if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
            raise LibraryError(f"Source object failed checksum verification: {path.name}")
        key = f"objects/{digest}"
        self._put_immutable(key, data, "application/octet-stream")
        return key

    def assert_operation_input(self, operation_id: UUID, identity: str, fingerprints: list[dict]) -> None:
        """Make operation reuse compare canonical import inputs, not just asset IDs."""
        body = canonical_json({"operationId": str(operation_id), "identity": identity, "files": fingerprints})
        suffix = "batch" if identity == "batch" else hashlib.sha256(identity.encode()).hexdigest()
        key = f"reconciliation/dual-writes/operations/{operation_id}/{suffix}.json"
        existing = self.storage.head(key)
        if existing is not None:
            stored = self.storage.read_bytes(key)
            if stored != body:
                raise DualWriteIntegrityError(
                    "Checksum verification failed: Operation ID was reused with different canonical inputs "
                    "(changed file content)"
                )
            return
        if identity == "batch":
            self._put_immutable(key, body, "application/json")
            return
        # A single operation may contain multiple paths.  Compare all prior
        # receipts for this operation so a changed path cannot be smuggled in.
        prefix = f"reconciliation/dual-writes/operations/{operation_id}/"
        for prior_key in self.storage.keys(prefix):
            prior = json.loads(self.storage.read_bytes(prior_key))
            if prior.get("identity") == identity and prior != json.loads(body):
                raise DualWriteIntegrityError("Operation ID was reused with different canonical inputs")
        self._put_immutable(key, body, "application/json")

    def manifest_from_legacy(self, manifest) -> AssetManifest:
        blobs = tuple(
            BlobReference(
                blob_id=blob.blob_id,
                role=blob.role,
                object_key=f"objects/{blob.sha256}",
                original_filename=blob.original_filename,
                sha256=blob.sha256,
                size_bytes=blob.size_bytes,
                mime_type=blob.mime_type,
            )
            for blob in manifest.blobs
        )
        state = manifest.user_state
        location = state.location
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        return AssetManifest(
            library_id=manifest.library_id,
            asset_id=manifest.asset_id,
            revision=1,
            parent_revision=None,
            operation_id=manifest.operation_id,
            created_at=_utc(manifest.imported_at) or now,
            imported_at=_utc(manifest.imported_at) or now,
            capture_time=_utc(manifest.capture_time),
            blobs=blobs,
            primary_blob_id=manifest.primary_blob_id,
            extracted_metadata=manifest.metadata,
            user_state=UserState(
                rating=state.rating,
                favorite=state.favorite,
                caption=state.caption,
                keywords=tuple(state.keywords),
                location=(
                    None
                    if location is None
                    else Location(location.name, location.latitude, location.longitude)
                ),
            ),
            deleted_at=_utc(manifest.deleted_at),
            processing=(),
        )

    def publish_manifest(self, manifest: AssetManifest) -> str:
        data = encode(manifest)
        # Round-trip through the Phase 2 decoder before accepting publication.
        validated = decode_asset_manifest(data)
        data = encode(validated)
        key = f"manifests/assets/{manifest.asset_id}/{manifest.revision}.json"
        self._put_immutable(key, data, "application/json")
        stored = self.storage.read_bytes(key)
        if stored != data or sha256(validated) != hashlib.sha256(stored).hexdigest():
            raise DualWriteIntegrityError(f"Manifest checksum verification failed: {key}")
        return key

    def record_reconciliation(self, operation_id: UUID, payload: dict) -> None:
        """Write a best-effort immutable event for operators and later repair."""
        event = {
            "schemaVersion": 1,
            "operationId": str(operation_id),
            "recordedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            **payload,
        }
        body = canonical_json(event)
        event_key = hashlib.sha256(body).hexdigest()
        try:
            self._put_immutable(
                f"reconciliation/dual-writes/{operation_id}/{event_key}.json",
                body,
                "application/json",
            )
        except Exception:
            # Never mask the original PostgreSQL/S3 failure.
            pass
