"""Deterministic reconstruction of the PostgreSQL projection from S3.

This module deliberately has no mutation or reconciliation responsibilities.  It
only reads the canonical namespace, validates the complete histories, and applies
the selected snapshots to an empty Catalog.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable

from photo_server.app_logging import log_event
from photo_server.catalog import Catalog, analysis_runs, faces, people
from photo_server.fingerprints import BURST_HASH_VERSION
from photo_server.manifests import (
    AlbumManifest,
    AssetManifest,
    BurstManifest,
    FaceManifest,
    FingerprintManifest,
    ManifestCodecError,
    PersonManifest,
    ProcessingArtifact,
    Tombstone,
    decode,
    encode,
)
from photo_server.models import Album, Manifest
from photo_server.storage import Storage

MANIFEST_PREFIXES = (
    ("manifests/assets/", "asset"),
    ("manifests/albums/", "album"),
    ("manifests/people/", "person"),
    ("manifests/faces/", "face"),
    ("manifests/processing/", "processing"),
    ("manifests/fingerprints/", "fingerprint"),
    ("manifests/bursts/", "burst"),
    ("tombstones/", "tombstone"),
)


def discover_library_id(storage: Storage):
    """Discover the library identity from canonical S3 manifests.

    Recovery checkpoints intentionally contain manifests and objects, not the
    mutable library marker.  A fresh projection must therefore be bootstrap-
    able from the immutable namespace alone.
    """
    found = None
    for prefix, kind in MANIFEST_PREFIXES:
        codec_kind = "processing-artifact" if kind == "processing" else kind
        for key in sorted(storage.keys(prefix)):
            if not key.endswith(".json"):
                continue
            try:
                record = decode(storage.read_bytes(key), codec_kind)
            except (ManifestCodecError, ValueError, KeyError, TypeError):
                continue
            library_id = getattr(record, "library_id", None)
            if library_id is None:
                continue
            if found is None:
                found = library_id
            elif found != library_id:
                raise ValueError("S3 manifests contain multiple library identities")
    return found


@dataclass
class RebuildReport:
    scanned: int = 0
    validated: int = 0
    objects_checked: int = 0
    projected_assets: int = 0
    projected_albums: int = 0
    projected_people: int = 0
    projected_faces: int = 0
    projected_processing: int = 0
    projected_fingerprints: int = 0
    projected_burst_clusters: int = 0
    tombstones: int = 0
    checkpoint: str | None = None
    queues_restored: dict[str, int] | None = None
    malformed_manifests: list[dict[str, str]] = field(default_factory=list)
    checksum_mismatches: list[dict[str, str]] = field(default_factory=list)
    missing_objects: list[dict[str, str]] = field(default_factory=list)
    parent_gaps: list[dict[str, str]] = field(default_factory=list)
    conflicting_operation_ids: list[dict[str, str]] = field(default_factory=list)
    duplicate_revisions: list[dict[str, str]] = field(default_factory=list)
    multiple_valid_heads: list[dict[str, str]] = field(default_factory=list)
    unresolved_references: list[dict[str, str]] = field(default_factory=list)
    library_mismatches: list[dict[str, str]] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        result = {
            "status": "failed" if self.has_errors else "complete",
            "scanned": self.scanned,
            "validated": self.validated,
            "objectsChecked": self.objects_checked,
            "projectedAssets": self.projected_assets,
            "projectedAlbums": self.projected_albums,
            "projectedPeople": self.projected_people,
            "projectedFaces": self.projected_faces,
            "projectedProcessing": self.projected_processing,
            "projectedFingerprints": self.projected_fingerprints,
            "projectedBurstClusters": self.projected_burst_clusters,
            "tombstones": self.tombstones,
            "checkpoint": self.checkpoint,
            "queuesRestored": self.queues_restored,
        }
        for name in (
            "malformed_manifests",
            "checksum_mismatches",
            "missing_objects",
            "parent_gaps",
            "conflicting_operation_ids",
            "duplicate_revisions",
            "multiple_valid_heads",
            "unresolved_references",
            "library_mismatches",
        ):
            result[_camel(name)] = getattr(self, name)
        result["errors"] = self.errors
        return result

    @property
    def has_errors(self) -> bool:
        return any(
            getattr(self, name)
            for name in (
                "malformed_manifests",
                "checksum_mismatches",
                "missing_objects",
                "parent_gaps",
                "conflicting_operation_ids",
                "duplicate_revisions",
                "multiple_valid_heads",
                "unresolved_references",
                "library_mismatches",
                "errors",
            )
        )


def _camel(name: str) -> str:
    head, *tail = name.split("_")
    return head + "".join(part.title() for part in tail)


def _key_identity(key: str) -> tuple[str, str, int] | None:
    parts = key.split("/")
    if not parts[-1].endswith(".json"):
        return None
    if parts[0] == "manifests" and parts[1] in {"faces", "processing"} and len(parts) == 3:
        kind = "face" if parts[1] == "faces" else "processing"
        return kind, parts[2][:-5], 1
    if parts[0] == "manifests" and parts[1] == "bursts" and len(parts) == 3:
        try:
            return "burst", "state", int(parts[2][:-5])
        except ValueError:
            return None
    if len(parts) != 4:
        return None
    if parts[0] == "manifests" and parts[1] in {"assets", "albums"}:
        kind = parts[1][:-1]
        try:
            return kind, parts[2], int(parts[3][:-5])
        except ValueError:
            return None
    if parts[0] == "manifests" and parts[1] == "people":
        try:
            return "person", parts[2], int(parts[3][:-5])
        except ValueError:
            return None
    if parts[0] == "manifests" and parts[1] == "fingerprints" and len(parts) == 4:
        return "fingerprint", f"{parts[2]}:{parts[3][:-5]}", 1
    if parts[0] == "tombstones" and len(parts) == 4:
        try:
            return "tombstone", f"{parts[1]}:{parts[2]}", int(parts[3][:-5])
        except ValueError:
            return None
    return None


def _public_processing_result(result: Any) -> dict[str, Any]:
    """Project a rich S3 analysis artifact into the public DB result.

    S3 retains the versioned artifact (metadata, model details, metrics, and
    detections). ``analysis_runs.result`` is intentionally the compact,
    user-visible semantic projection written by the live AI worker. Rebuild
    must reproduce that projection rather than persist the whole artifact.
    """
    if not isinstance(result, dict) or not isinstance(result.get("semantic"), dict):
        return result
    public = dict(result["semantic"])
    if "faceCount" in result:
        public["faceCount"] = result["faceCount"]
    elif isinstance(result.get("faces"), list):
        public["faceCount"] = len(result["faces"])
    if "personCount" in result:
        public["personCount"] = result["personCount"]
    elif isinstance(result.get("faces"), list):
        # The live clustering transaction assigns at most one face from an
        # image to each person, so this is exactly the number it persists.
        public["personCount"] = len(result["faces"])
    return public


def _entity(record: Any) -> tuple[str, str, int]:
    if isinstance(record, AssetManifest):
        return "asset", str(record.asset_id), record.revision
    if isinstance(record, AlbumManifest):
        return "album", str(record.album_id), record.revision
    if isinstance(record, PersonManifest):
        return "person", str(record.person_id), record.revision
    if isinstance(record, FaceManifest):
        return "face", str(record.face_id), 1
    if isinstance(record, ProcessingArtifact):
        return "processing", str(record.artifact_id), 1
    if isinstance(record, FingerprintManifest):
        version = hashlib.sha256(record.algorithm_version.encode()).hexdigest()
        return "fingerprint", f"{record.asset_id}:{version}", 1
    if isinstance(record, BurstManifest):
        return "burst", "state", record.revision
    return record.entity_type, str(record.entity_id), record.revision


def _operation_document(record: Any) -> dict[str, Any]:
    return {"operationId": str(record.operation_id), "entity": _entity(record)[:2]}


def _verify_object(
    storage: Any,
    key: str,
    size: int | None,
    digest: str,
    report: RebuildReport,
    *,
    verify_checksum: bool = True,
) -> bool:
    report.objects_checked += 1
    head = storage.head(key)
    if head is None:
        report.missing_objects.append({"key": key, "reason": "object not found"})
        return False
    if size is not None and head.get("ContentLength") != size:
        report.checksum_mismatches.append(
            {
                "key": key,
                "reason": "size mismatch",
                "expected": str(size),
                "actual": str(head.get("ContentLength")),
            }
        )
        return False
    if not verify_checksum:
        return True
    data = storage.read_bytes(key)
    actual = hashlib.sha256(data).hexdigest()
    if (size is not None and len(data) != size) or actual != digest:
        report.checksum_mismatches.append(
            {"key": key, "reason": "SHA-256 mismatch", "expected": digest, "actual": actual}
        )
        return False
    return True


def _record_error(report: RebuildReport, key: str, reason: str) -> None:
    report.malformed_manifests.append({"key": key, "reason": reason})


def _searchable_fallback(value: Any) -> str:
    text_values: list[str] = []
    if isinstance(value, str) and value.strip():
        text_values.append(value.strip())
    elif isinstance(value, dict):
        for item in value.values():
            text_values.extend(_searchable_fallback(item).split(" "))
    elif isinstance(value, list):
        for item in value:
            text_values.extend(_searchable_fallback(item).split(" "))
    return " ".join(dict.fromkeys(item for item in text_values if item))


def _write_checkpoint(storage: Any, key: str, value: dict) -> None:
    if hasattr(storage, "put_json_mutable"):
        storage.put_json_mutable(key, value)
    else:
        storage.put(key, json.dumps(value, sort_keys=True).encode(), "application/json")


def _decode_record(storage: Any, key: str, kind: str, report: RebuildReport):
    try:
        body = storage.read_bytes(key)
        actual = hashlib.sha256(body).hexdigest()
        record = decode(body, kind)
        canonical = encode(record)
        if body != canonical:
            report.checksum_mismatches.append(
                {
                    "key": key,
                    "reason": "manifest is not canonical",
                    "actual": actual,
                    "expected": hashlib.sha256(canonical).hexdigest(),
                }
            )
            return None
        report.validated += 1
        return record
    except (ManifestCodecError, ValueError, KeyError, TypeError) as error:
        _record_error(report, key, str(error))
        return None


def _history(records: list[tuple[str, Any]], report: RebuildReport, kind: str, entity_id: str):
    if not records:
        return None
    records.sort(key=lambda item: (item[1].revision, item[0]))
    by_revision: dict[int, list[tuple[str, Any]]] = {}
    operations: dict[str, str] = {}
    for key, record in records:
        by_revision.setdefault(record.revision, []).append((key, record))
        operation = str(record.operation_id)
        document = json.dumps(record.to_dict(), sort_keys=True, separators=(",", ":"))
        prior = operations.get(operation)
        if prior is not None and prior != document:
            report.conflicting_operation_ids.append({"entity": entity_id, "operationId": operation})
        operations[operation] = document
    for revision, values in by_revision.items():
        if len(values) > 1:
            report.duplicate_revisions.append({"entity": entity_id, "revision": str(revision)})
            report.multiple_valid_heads.append({"entity": entity_id, "revision": str(revision)})
    selected = []
    for revision in sorted(by_revision):
        values = by_revision[revision]
        if len(values) != 1:
            continue
        record = values[0][1]
        expected = revision - 1 if revision > 1 else None
        if record.parent_revision != expected:
            report.parent_gaps.append(
                {"entity": entity_id, "revision": str(revision), "reason": "parent gap"}
            )
            continue
        if revision == 1 or selected and selected[-1][1].revision == record.parent_revision:
            selected.append(values[0])
    if len(selected) != len(by_revision):
        # A valid later record with a missing parent is an integrity failure;
        # never silently project an older head.
        report.parent_gaps.append({"entity": entity_id, "reason": "revision chain is incomplete"})
        return None
    if len(selected) > 1 and selected[-1][1].revision != max(by_revision):
        report.multiple_valid_heads.append({"entity": entity_id})
        return None
    return selected[-1][1]


def _asset_projection(
    record: AssetManifest,
    deleted_at: str | None = None,
    previous: AssetManifest | None = None,
) -> Manifest:
    blobs = [blob.to_dict() for blob in record.blobs]
    payload = {
        "schemaVersion": 2,
        "libraryId": str(record.library_id),
        "assetId": str(record.asset_id),
        "revision": record.revision,
        "previousRevision": record.parent_revision,
        "operationId": str(record.operation_id),
        "importedAt": record.imported_at,
        "captureTime": record.capture_time,
        "blobs": blobs,
        "primaryBlobId": str(record.primary_blob_id),
        "metadata": record.extracted_metadata,
        "userState": record.user_state.to_dict(),
        "deletedAt": deleted_at if deleted_at is not None else record.deleted_at,
    }
    payload.update(
        {
            "mutation": None,
        }
    )
    if record.revision > 1:
        action = "asset.patch"
        if previous is not None:
            if record.deleted_at is not None and previous.deleted_at is None:
                action = "asset.delete"
            elif record.deleted_at is None and previous.deleted_at is not None:
                action = "asset.restore"
            elif (
                record.extracted_metadata != previous.extracted_metadata
                or record.capture_time != previous.capture_time
            ) and record.user_state == previous.user_state:
                action = "asset.metadata"
        payload["mutation"] = {
            "action": action,
            "entityId": str(record.asset_id),
            "changes": {},
            "expectedRevision": record.parent_revision,
        }
    return Manifest.model_validate(payload)


def _album_projection(
    record: AlbumManifest,
    deleted_at: str | None = None,
    previous: AlbumManifest | None = None,
) -> Album:
    action = "album.create" if record.revision == 1 else "album.patch"
    if previous is not None:
        if record.deleted_at is not None and previous.deleted_at is None:
            action = "album.delete"
        elif record.deleted_at is None and previous.deleted_at is not None:
            action = "album.restore"
    payload = record.to_dict()
    payload.update(
        {
            "previousRevision": record.parent_revision,
            "deletedAt": deleted_at if deleted_at is not None else record.deleted_at,
            "mutation": {
                "action": action,
                "entityId": str(record.album_id),
                "changes": {},
                "expectedRevision": record.parent_revision,
            },
        }
    )
    payload.pop("createdAt", None)
    payload.pop("parentRevision", None)
    return Album.model_validate(payload)


def rebuild_from_s3(
    storage: Storage,
    catalog: Catalog,
    *,
    checkpoint_id: str = "default",
    resume: bool = True,
    stop_after: int | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
    verify_object_checksums: bool = True,
) -> dict[str, Any]:
    """Scan and project S3 records; ``stop_after`` is a test/maintenance pause hook.

    Scan checkpoints report progress, but a restarted rebuild deliberately
    rescans the immutable namespace from the beginning. The selected history
    lives in memory until validation completes, so skipping the already-seen
    prefix would silently omit it from the rebuilt projection.
    """
    report = RebuildReport()
    checkpoint_key = f"indexes/checkpoints/{checkpoint_id}.json"
    if hasattr(catalog, "projection_is_empty") and not catalog.projection_is_empty():
        report.errors.append(
            {
                "reason": "rebuild target is not an empty disposable projection",
                "safety": "refusing to merge S3 state into an existing database",
            }
        )
        return report.as_dict()
    if resume and storage.head(checkpoint_key) is not None:
        try:
            checkpoint = storage.get_json(checkpoint_key)
            if not isinstance(checkpoint, dict) or checkpoint.get("schemaVersion") != 1:
                raise ValueError("unsupported checkpoint schema")
        except (ValueError, TypeError, KeyError) as error:
            report.errors.append({"key": checkpoint_key, "reason": f"invalid checkpoint: {error}"})
            return report.as_dict()

    keys = sorted(key for prefix, _ in MANIFEST_PREFIXES for key in storage.keys(prefix))
    log_event(
        "s3_rebuild_scan_started",
        stage="rebuild",
        checkpoint_id=checkpoint_id,
        manifest_keys=len(keys),
    )
    if progress:
        progress(
            {
                "phase": "scan",
                "scanned": 0,
                "total": len(keys),
                "percent": 0.0,
                "currentKey": None,
            }
        )
    records: dict[tuple[str, str], list[tuple[str, Any]]] = {}
    operation_records: dict[tuple[str, str, str], tuple[str, bytes]] = {}
    expected_library = None
    if hasattr(catalog, "library_id"):
        try:
            expected_library = str(catalog.library_id())
        except Exception:
            expected_library = None
    for key in keys:
        identity = _key_identity(key)
        if identity is None:
            _record_error(report, key, "manifest key does not match canonical layout")
            continue
        kind = "tombstone" if identity[0] == "tombstone" else identity[0]
        codec_kind = "processing-artifact" if kind == "processing" else kind
        report.scanned += 1
        record = _decode_record(storage, key, codec_kind, report)
        if report.scanned == 1 or report.scanned % 100 == 0:
            log_event(
                "s3_rebuild_scan_progress",
                stage="rebuild",
                checkpoint_id=checkpoint_id,
                scanned=report.scanned,
                total=len(keys),
                percent=round(report.scanned / len(keys) * 100, 1) if keys else 100.0,
                current_key=key,
                validated=report.validated,
                records=len(records),
            )
            if progress:
                progress(
                    {
                        "phase": "scan",
                        "scanned": report.scanned,
                        "total": len(keys),
                        "percent": round(report.scanned / len(keys) * 100, 1) if keys else 100.0,
                        "currentKey": key,
                    }
                )
        if record is None:
            continue
        record_library = getattr(record, "library_id", None)
        if expected_library and record_library and str(record_library) != expected_library:
            report.library_mismatches.append(
                {"key": key, "expected": expected_library, "actual": str(record_library)}
            )
        actual_kind, entity_id, revision = _entity(record)
        if isinstance(record, (FaceManifest, ProcessingArtifact, FingerprintManifest)):
            expected_identity = identity
            if (actual_kind, entity_id, revision) != expected_identity:
                _record_error(report, key, "manifest identity does not match its S3 key")
                continue
            if isinstance(record, ProcessingArtifact):
                _verify_object(
                    storage,
                    record.result_object.object_key,
                    record.result_object.size_bytes,
                    record.result_object.sha256,
                    report,
                    verify_checksum=verify_object_checksums,
                )
                records.setdefault((actual_kind, entity_id), []).append((key, record))
            else:
                records.setdefault((actual_kind, entity_id), []).append((key, record))
            report.checkpoint = key
            if report.scanned % 100 == 0:
                _write_checkpoint(storage, checkpoint_key, {"schemaVersion": 1, "lastKey": key})
            if stop_after is not None and report.scanned >= stop_after:
                return report.as_dict() | {"status": "paused"}
            continue
        expected_identity = (
            (record.entity_type, str(record.entity_id), revision)
            if isinstance(record, Tombstone)
            else identity
        )
        if (actual_kind, entity_id, revision) != expected_identity:
            _record_error(report, key, "manifest identity does not match its S3 key")
            continue
        if isinstance(record, AssetManifest):
            for blob in record.blobs:
                _verify_object(
                    storage,
                    blob.object_key,
                    blob.size_bytes,
                    blob.sha256,
                    report,
                    verify_checksum=verify_object_checksums,
                )
            for reference in record.processing:
                _verify_object(
                    storage,
                    reference.artifact_key,
                    None,
                    reference.artifact_sha256,
                    report,
                    verify_checksum=verify_object_checksums,
                )
        record_kind, record_id = (
            ("tombstone", f"{actual_kind}:{entity_id}")
            if isinstance(record, Tombstone)
            else (actual_kind, entity_id)
        )
        operation_id = str(record.operation_id)
        operation_identity = (record_kind + ":" + record_id, encode(record))
        prior = operation_records.get((operation_id, record_kind, record_id))
        if prior is not None and prior != operation_identity:
            report.conflicting_operation_ids.append(
                {"operationId": operation_id, "entity": operation_identity[0]}
            )
        operation_records[(operation_id, record_kind, record_id)] = operation_identity
        records.setdefault((record_kind, record_id), []).append((key, record))
        report.checkpoint = key
        if report.scanned % 100 == 0:
            _write_checkpoint(storage, checkpoint_key, {"schemaVersion": 1, "lastKey": key})
        if stop_after is not None and report.scanned >= stop_after:
            return report.as_dict() | {"status": "paused"}

    selected: dict[tuple[str, str], Any] = {}
    log_event(
        "s3_rebuild_projection_started",
        stage="rebuild",
        checkpoint_id=checkpoint_id,
        scanned=report.scanned,
        records=len(records),
    )
    if progress:
        progress(
            {
                "phase": "projection",
                "scanned": report.scanned,
                "total": len(keys),
                "percent": 99.0,
            }
        )
    for (kind, entity_id), values in sorted(records.items()):
        if kind in {"face", "processing", "fingerprint"}:
            continue
        if kind == "tombstone":
            by_revision: dict[int, list[Tombstone]] = {}
            for _key, value in values:
                by_revision.setdefault(value.revision, []).append(value)
            for revision, revisions in by_revision.items():
                if len(revisions) > 1:
                    report.duplicate_revisions.append(
                        {"entity": entity_id, "revision": str(revision)}
                    )
            if any(len(revisions) > 1 for revisions in by_revision.values()):
                continue
            selected[(kind, entity_id)] = max(
                (value for _key, value in values), key=lambda value: value.revision
            )
            continue
        chosen = _history(values, report, kind, entity_id)
        if chosen is not None:
            selected[(kind, entity_id)] = chosen
    if report.has_errors:
        return report.as_dict()

    selected_faces = [
        record
        for (kind, _), record in records.items()
        if kind == "face"
        for record in [record[0][1]]
    ]
    selected_runs = [
        record
        for (kind, _), record in records.items()
        if kind == "processing"
        for record in [record[0][1]]
    ]
    selected_fingerprints = [
        values[0][1]
        for (kind, _), values in records.items()
        if kind == "fingerprint"
    ]
    from photo_server.burst_authority import load_burst_state

    selected_burst = load_burst_state(storage)
    face_by_id = {str(record.face_id): record for record in selected_faces}
    run_by_id = {str(record.artifact_id): record for record in selected_runs}
    run_by_artifact_key = {record.result_object.object_key: record for record in selected_runs}
    asset_ids = {key[1] for key in records if key[0] == "asset"}
    current_assets = {
        entity_id: record
        for (kind, entity_id), record in selected.items()
        if kind == "asset" and isinstance(record, AssetManifest)
    }
    person_ids = {key[1] for key in records if key[0] == "person"}
    for fingerprint in selected_fingerprints:
        if str(fingerprint.asset_id) not in asset_ids:
            report.unresolved_references.append(
                {
                    "category": "asset",
                    "key": str(fingerprint.asset_id),
                    "reason": "fingerprint asset is not exported",
                }
            )
    if selected_fingerprints and selected_burst is None:
        report.unresolved_references.append(
            {
                "category": "burst",
                "key": "state",
                "reason": "fingerprints exist without a canonical burst snapshot",
            }
        )
    if selected_burst is not None:
        current_fingerprints = {
            (str(record.asset_id), record.algorithm_version)
            for record in selected_fingerprints
        }
        for cluster in selected_burst.clusters:
            for asset_id in cluster.asset_ids:
                asset_id = str(asset_id)
                if (asset_id, BURST_HASH_VERSION) not in current_fingerprints:
                    report.unresolved_references.append(
                        {
                            "category": "fingerprint",
                            "key": str(cluster.cluster_id),
                            "reason": f"burst member {asset_id} has no current canonical fingerprint",
                        }
                    )
                asset = current_assets.get(asset_id)
                if asset is None or asset.deleted_at is not None:
                    report.unresolved_references.append(
                        {
                            "category": "asset",
                            "key": str(cluster.cluster_id),
                            "reason": f"burst member {asset_id} is missing or deleted",
                        }
                    )
        for asset_id in selected_burst.excluded_asset_ids:
            if (str(asset_id), BURST_HASH_VERSION) not in current_fingerprints:
                report.unresolved_references.append(
                    {
                        "category": "fingerprint",
                        "key": "excludedAssetIds",
                        "reason": f"excluded asset {asset_id} has no current canonical fingerprint",
                    }
                )
    for asset in (
        record
        for (kind, _), record in selected.items()
        if kind == "asset" and isinstance(record, AssetManifest)
    ):
        for reference in asset.processing:
            run = run_by_artifact_key.get(reference.artifact_key)
            if run is None:
                report.unresolved_references.append(
                    {
                        "category": "processing",
                        "key": reference.artifact_key,
                        "reason": "asset processing reference has no processing artifact manifest",
                    }
                )
            elif run.input_sha256 != reference.input_sha256:
                report.unresolved_references.append(
                    {
                        "category": "processing",
                        "key": reference.artifact_key,
                        "reason": "processing artifact input checksum does not match asset reference",
                    }
                )
            elif run.result_object.sha256 != reference.artifact_sha256:
                report.unresolved_references.append(
                    {
                        "category": "processing",
                        "key": reference.artifact_key,
                        "reason": "processing artifact checksum does not match asset reference",
                    }
                )
    for face in selected_faces:
        if str(face.asset_id) not in asset_ids:
            report.unresolved_references.append(
                {
                    "category": "asset",
                    "key": str(face.face_id),
                    "reason": "face asset is not exported",
                }
            )
        if str(face.person_id) not in person_ids:
            report.unresolved_references.append(
                {
                    "category": "person",
                    "key": str(face.face_id),
                    "reason": "face person is not exported",
                }
            )
        if str(face.analysis_run_id) not in run_by_id:
            report.unresolved_references.append(
                {
                    "category": "analysis-run",
                    "key": str(face.face_id),
                    "reason": "face analysis run is not exported",
                }
            )
    # A person may have no face records. Once a face namespace is present,
    # assignments without corresponding rows are an integrity failure rather
    # than an empty projection.
    has_face_namespace = any(key.startswith("manifests/faces/") for key in keys)
    if has_face_namespace:
        for person in (
            record
            for (kind, _), values in records.items()
            if kind == "person"
            for record in [values[0][1]]
        ):
            for face_id in person.face_ids:
                if str(face_id) not in face_by_id:
                    report.unresolved_references.append(
                        {
                            "category": "face",
                            "key": str(person.person_id),
                            "reason": f"missing face {face_id}",
                        }
                    )
    if report.unresolved_references:
        return report.as_dict()

    latest_assets = {}
    latest_albums = {}
    latest_people = {}
    tombstones = {}
    for (kind, entity_id), _values in records.items():
        chosen = selected.get((kind, entity_id))
        if chosen is None:
            continue
        if isinstance(chosen, Tombstone):
            report.tombstones += len(_values)
            tombstones[(chosen.entity_type, str(chosen.entity_id))] = chosen
            continue
        if isinstance(chosen, AssetManifest):
            latest_assets[entity_id] = chosen
        elif isinstance(chosen, AlbumManifest):
            latest_albums[entity_id] = chosen
        elif isinstance(chosen, PersonManifest):
            latest_people[entity_id] = chosen
    for identity, tombstone in tombstones.items():
        revisions = records.get(identity, [])
        matching = [
            record for _key, record in revisions
            if record.revision == tombstone.revision
        ]
        if len(matching) != 1:
            report.parent_gaps.append(
                {"entity": f"{identity[0]}:{identity[1]}", "reason": "tombstone has no unique deletion manifest"}
            )
            continue
        deletion = matching[0]
        if (
            deletion.operation_id != tombstone.operation_id
            or deletion.deleted_at != tombstone.deleted_at
            or deletion.parent_revision != tombstone.parent_revision
        ):
            report.conflicting_operation_ids.append(
                {"entity": f"{identity[0]}:{identity[1]}", "operationId": str(tombstone.operation_id)}
            )
    if report.has_errors:
        return report.as_dict()
    by_asset = sorted(latest_assets.values(), key=lambda x: str(x.asset_id))
    by_album = sorted(latest_albums.values(), key=lambda x: str(x.album_id))
    with catalog.writer():
        projections = []
        for record in by_asset:
            previous = next(
                (
                    value for _key, value in records[("asset", str(record.asset_id))]
                    if value.revision == record.parent_revision
                ),
                None,
            )
            projections.append(_asset_projection(record, previous=previous))
        if hasattr(catalog, "apply_projection_batch"):
            catalog.apply_projection_batch(projections)
        else:
            for projection in projections:
                catalog.apply_projection(projection)
        report.projected_assets = len(projections)
        albums = []
        for record in by_album:
            previous = next(
                (
                    value for _key, value in records[("album", str(record.album_id))]
                    if value.revision == record.parent_revision
                ),
                None,
            )
            albums.append(_album_projection(record, previous=previous))
        if hasattr(catalog, "apply_album_batch"):
            catalog.apply_album_batch(albums)
        else:
            for album in albums:
                catalog.apply_album(album)
        report.projected_albums = len(albums)
        # Processing artifacts and faces are durable-derived state.  Queue and
        # lease tables are intentionally not touched by rebuild.
        if hasattr(catalog, "engine"):
            with catalog.engine.begin() as connection:
                for person in sorted(latest_people.values(), key=lambda x: str(x.person_id)):
                    connection.execute(
                        people.insert().values(
                            id=str(person.person_id),
                            display_name=person.display_name,
                            created_at=person.created_at.replace("Z", "+00:00"),
                        )
                    )
                for run in sorted(selected_runs, key=lambda x: str(x.artifact_id)):
                    result = json.loads(storage.read_bytes(run.result_object.object_key))
                    public_result = _public_processing_result(result)
                    searchable = ""
                    semantic = result.get("semantic") if isinstance(result, dict) else None
                    if isinstance(semantic, dict):
                        try:
                            from photo_server.analysis import SemanticAnalysis, searchable_text

                            searchable = searchable_text(SemanticAnalysis.model_validate(semantic))
                        except (TypeError, ValueError):
                            # A processing artifact remains recoverable even if
                            # its optional search projection cannot be derived.
                            searchable = str(result.get("searchableText", ""))
                    if not searchable and isinstance(public_result, dict):
                        searchable = _searchable_fallback(public_result)
                    connection.execute(
                        analysis_runs.insert().values(
                            id=str(run.artifact_id),
                            asset_id=str(run.asset_id),
                            analysis_type=run.processing_type,
                            model_name=run.model_name or "",
                            model_version=run.model_version or "",
                            pipeline_version=run.pipeline_version,
                            input_hash=run.input_sha256,
                            object_key=run.source_object_key or run.result_object.object_key,
                            result=public_result,
                            searchable_text=searchable,
                            is_current=True,
                            semantic_origin=result.get("semanticOrigin", "computed"),
                            source_run_id=result.get("semanticSourceRunId"),
                            similarity=result.get("similarity"),
                            created_at=run.created_at.replace("Z", "+00:00"),
                        )
                    )
                    report.projected_processing += 1
                for face in sorted(selected_faces, key=lambda x: str(x.face_id)):
                    connection.execute(
                        faces.insert().values(
                            id=str(face.face_id),
                            asset_id=str(face.asset_id),
                            analysis_run_id=str(face.analysis_run_id),
                            person_id=str(face.person_id),
                            face_index=face.face_index,
                            bounding_box=face.bounding_box,
                            confidence=face.confidence,
                            embedding=face.embedding,
                        )
                    )
                    report.projected_faces += 1
        for record in sorted(latest_people.values(), key=lambda x: str(x.person_id)):
            tombstone = tombstones.get(("person", str(record.person_id)))
            if hasattr(catalog, "apply_person"):
                catalog.apply_person(record, record.face_ids)
            report.projected_people += 1
        if selected_burst is not None and hasattr(catalog, "apply_burst_projection"):
            catalog.apply_burst_projection(selected_fingerprints, selected_burst)
            report.projected_fingerprints = len(selected_fingerprints)
            report.projected_burst_clusters = len(selected_burst.clusters)
    if hasattr(catalog, "restore_work_queues"):
        asset_ids_for_queue = sorted(str(asset.asset_id) for asset in current_assets.values())
        fingerprint_asset_ids = {str(record.asset_id) for record in selected_fingerprints}
        latest_ai: dict[str, ProcessingArtifact] = {}
        for run in selected_runs:
            if run.processing_type != "photo-ai":
                continue
            asset = current_assets.get(str(run.asset_id))
            if asset is None or run.input_sha256 != asset.primary.sha256:
                continue
            key = str(run.asset_id)
            previous = latest_ai.get(key)
            if previous is None or (run.created_at, str(run.artifact_id)) > (
                previous.created_at,
                str(previous.artifact_id),
            ):
                latest_ai[key] = run
        report.queues_restored = catalog.restore_work_queues(
            asset_ids_for_queue,
            fingerprint_asset_ids,
            set(latest_ai),
        )
    _write_checkpoint(
        storage, checkpoint_key, {"schemaVersion": 1, "lastKey": None, "complete": True}
    )
    result = report.as_dict()
    log_event(
        "s3_rebuild_complete",
        stage="rebuild",
        checkpoint_id=checkpoint_id,
        status=result.get("status"),
        scanned=result.get("scanned", 0),
        projected_assets=result.get("projectedAssets", 0),
        projected_albums=result.get("projectedAlbums", 0),
    )
    return result


def compare_projections(expected: Catalog, actual: Catalog) -> dict[str, Any]:
    """Compare durable user-visible projections, ignoring operational queues."""

    def normalize(value):
        if isinstance(value, dict):
            if "mutation" in value and isinstance(value["mutation"], dict):
                value = {
                    **value,
                    "mutation": {key: item for key, item in value["mutation"].items() if key != "changes"},
                }
            return {key: normalize(item) for key, item in value.items()}
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return value
            if parsed.tzinfo is not None:
                return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")
        return value

    expected_assets = {
        str(item.asset_id): normalize(item.document()) for item in expected.all_assets()
    }
    actual_assets = {str(item.asset_id): normalize(item.document()) for item in actual.all_assets()}
    expected_albums = {
        str(item.album_id): normalize(item.document()) for item in expected.all_albums()
    }
    actual_albums = {str(item.album_id): normalize(item.document()) for item in actual.all_albums()}
    expected_people = {row["personId"]: normalize(row) for row in expected.all_people()}
    actual_people = {row["personId"]: normalize(row) for row in actual.all_people()}
    people = {
        "missing": sorted(set(expected_people) - set(actual_people)),
        "extra": sorted(set(actual_people) - set(expected_people)),
        "different": sorted(
            key
            for key in set(expected_people) & set(actual_people)
            if expected_people[key] != actual_people[key]
        ),
    }
    def derived(catalog: Catalog) -> dict[str, Any]:
        with catalog.engine.connect() as connection:
            runs = [
                dict(row)
                for row in connection.execute(
                    analysis_runs.select().order_by(analysis_runs.c.id)
                ).mappings()
            ]
            face_rows = [
                dict(row)
                for row in connection.execute(
                    faces.select().order_by(faces.c.id)
                ).mappings()
            ]
        for row in runs:
            row.pop("created_at", None)
        for row in face_rows:
            box = row.get("bounding_box")
            if isinstance(box, list) and len(box) == 4:
                row["bounding_box"] = {
                    "x": box[0],
                    "y": box[1],
                    "width": box[2],
                    "height": box[3],
                }
        return {
            "processing": normalize(runs),
            "faces": normalize(face_rows),
            "burst": normalize(catalog.burst_projection()),
        }

    expected_derived = derived(expected)
    actual_derived = derived(actual)
    expected_by_id = {
        kind: ({item["id"]: item for item in values} if kind != "burst" else values)
        for kind, values in expected_derived.items()
    }
    actual_by_id = {
        kind: ({item["id"]: item for item in values} if kind != "burst" else values)
        for kind, values in actual_derived.items()
    }
    derived_diff = {
        kind: {
            "missing": sorted(set(expected_by_id[kind]) - set(actual_by_id[kind])) if kind != "burst" else [],
            "extra": sorted(set(actual_by_id[kind]) - set(expected_by_id[kind])) if kind != "burst" else [],
            "different": ([
                {
                    "id": item_id,
                    "expected": expected_by_id[kind][item_id],
                    "actual": actual_by_id[kind][item_id],
                }
                for item_id in sorted(
                    set(expected_by_id[kind]) & set(actual_by_id[kind])
                )
                if expected_by_id[kind][item_id] != actual_by_id[kind][item_id]
            ] if kind != "burst" else ([] if expected_by_id[kind] == actual_by_id[kind] else [{"expected": expected_by_id[kind], "actual": actual_by_id[kind]}])),
        }
        for kind in expected_derived
    }
    return {
        "match": expected_assets == actual_assets
        and expected_albums == actual_albums
        and not any(people.values())
        and not any(any(value.values()) for value in derived_diff.values()),
        "assets": {
            "missing": sorted(set(expected_assets) - set(actual_assets)),
            "extra": sorted(set(actual_assets) - set(expected_assets)),
            "different": sorted(
                key
                for key in set(expected_assets) & set(actual_assets)
                if expected_assets[key] != actual_assets[key]
            ),
        },
        "albums": {
            "missing": sorted(set(expected_albums) - set(actual_albums)),
            "extra": sorted(set(actual_albums) - set(expected_albums)),
            "different": sorted(
                key
                for key in set(expected_albums) & set(actual_albums)
                if expected_albums[key] != actual_albums[key]
            ),
        },
        "people": people,
        "derived": derived_diff,
    }
