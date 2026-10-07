"""Deterministic reconstruction of the PostgreSQL projection from S3.

This module deliberately has no mutation or reconciliation responsibilities.  It
only reads the Phase 1 namespace, validates the complete histories, and applies
the selected snapshots to an empty Catalog.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from photo_server.catalog import Catalog, analysis_runs, faces, people
from photo_server.manifests import (
    AlbumManifest,
    AssetManifest,
    FaceManifest,
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
    ("tombstones/", "tombstone"),
)


@dataclass
class RebuildReport:
    scanned: int = 0
    validated: int = 0
    objects_checked: int = 0
    projected_assets: int = 0
    projected_albums: int = 0
    projected_people: int = 0
    checkpoint: str | None = None
    malformed_manifests: list[dict[str, str]] = field(default_factory=list)
    checksum_mismatches: list[dict[str, str]] = field(default_factory=list)
    missing_objects: list[dict[str, str]] = field(default_factory=list)
    parent_gaps: list[dict[str, str]] = field(default_factory=list)
    conflicting_operation_ids: list[dict[str, str]] = field(default_factory=list)
    duplicate_revisions: list[dict[str, str]] = field(default_factory=list)
    multiple_valid_heads: list[dict[str, str]] = field(default_factory=list)
    unresolved_references: list[dict[str, str]] = field(default_factory=list)
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
            "checkpoint": self.checkpoint,
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
    if parts[0] == "tombstones" and len(parts) == 4:
        try:
            return "tombstone", f"{parts[1]}:{parts[2]}", int(parts[3][:-5])
        except ValueError:
            return None
    return None


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
    return record.entity_type, str(record.entity_id), record.revision


def _operation_document(record: Any) -> dict[str, Any]:
    return {"operationId": str(record.operation_id), "entity": _entity(record)[:2]}


def _verify_object(
    storage: Any, key: str, size: int | None, digest: str, report: RebuildReport
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


def _asset_projection(record: AssetManifest, deleted_at: str | None = None) -> Manifest:
    blobs = []
    for blob in record.blobs:
        blobs.append(
            {**blob.to_dict(), "objectKey": f"originals/{record.asset_id}/{blob.original_filename}"}
        )
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
        payload["mutation"] = {
            "action": "asset.patch",
            "entityId": str(record.asset_id),
            "changes": {},
            "expectedRevision": record.parent_revision,
        }
    return Manifest.model_validate(payload)


def _album_projection(record: AlbumManifest, deleted_at: str | None = None) -> Album:
    payload = record.to_dict()
    payload.update(
        {
            "previousRevision": record.parent_revision,
            "deletedAt": deleted_at if deleted_at is not None else record.deleted_at,
            "mutation": {
                "action": "album.patch" if record.revision > 1 else "album.create",
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
) -> dict[str, Any]:
    """Scan and project S3 records; ``stop_after`` is a test/maintenance pause hook."""
    report = RebuildReport()
    checkpoint_key = f"indexes/checkpoints/{checkpoint_id}.json"
    cursor = None
    if resume and storage.head(checkpoint_key) is not None:
        try:
            checkpoint = storage.get_json(checkpoint_key)
            if checkpoint.get("complete"):
                return report.as_dict()
            cursor = checkpoint.get("lastKey")
        except (ValueError, TypeError, KeyError) as error:
            report.errors.append({"key": checkpoint_key, "reason": f"invalid checkpoint: {error}"})
            return report.as_dict()

    keys = sorted(key for prefix, _ in MANIFEST_PREFIXES for key in storage.keys(prefix))
    records: dict[tuple[str, str], list[tuple[str, Any]]] = {}
    operation_records: dict[tuple[str, str, str], tuple[str, bytes]] = {}
    for key in keys:
        if cursor and key <= cursor:
            continue
        identity = _key_identity(key)
        if identity is None:
            _record_error(report, key, "manifest key does not match canonical layout")
            continue
        kind = "tombstone" if identity[0] == "tombstone" else identity[0]
        codec_kind = "processing-artifact" if kind == "processing" else kind
        report.scanned += 1
        record = _decode_record(storage, key, codec_kind, report)
        if record is None:
            continue
        actual_kind, entity_id, revision = _entity(record)
        if isinstance(record, (FaceManifest, ProcessingArtifact)):
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
                )
                records.setdefault((actual_kind, entity_id), []).append((key, record))
            else:
                records.setdefault((actual_kind, entity_id), []).append((key, record))
            report.checkpoint = key
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
                _verify_object(storage, blob.object_key, blob.size_bytes, blob.sha256, report)
            for reference in record.processing:
                _verify_object(
                    storage, reference.artifact_key, None, reference.artifact_sha256, report
                )
        operation_id = str(record.operation_id)
        operation_identity = (_entity(record)[0] + ":" + _entity(record)[1], encode(record))
        prior = operation_records.get((operation_id, actual_kind, entity_id))
        if prior is not None and prior != operation_identity:
            report.conflicting_operation_ids.append(
                {"operationId": operation_id, "entity": operation_identity[0]}
            )
        operation_records[(operation_id, actual_kind, entity_id)] = operation_identity
        records.setdefault((actual_kind, entity_id), []).append((key, record))
        report.checkpoint = key
        _write_checkpoint(storage, checkpoint_key, {"schemaVersion": 1, "lastKey": key})
        if stop_after is not None and report.scanned >= stop_after:
            return report.as_dict() | {"status": "paused"}

    selected: dict[tuple[str, str], Any] = {}
    for (kind, entity_id), values in sorted(records.items()):
        if kind in {"face", "processing"}:
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
    face_by_id = {str(record.face_id): record for record in selected_faces}
    run_by_id = {str(record.artifact_id): record for record in selected_runs}
    run_by_artifact_key = {record.result_object.object_key: record for record in selected_runs}
    asset_ids = {key[1] for key in records if key[0] == "asset"}
    person_ids = {key[1] for key in records if key[0] == "person"}
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
    # A person-only manifest is retained as backwards-compatible codec
    # coverage.  Backfill-produced namespaces always include face manifests;
    # once that durable namespace is present, assignments without rows are an
    # integrity failure rather than an empty projection.
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
            tombstones[(chosen.entity_type, str(chosen.entity_id))] = chosen
            entity_values = records.get((chosen.entity_type, str(chosen.entity_id)), [])
            snapshots = [
                item
                for item in entity_values
                if isinstance(item[1], (AssetManifest, AlbumManifest, PersonManifest))
                and item[1].revision <= chosen.parent_revision
            ]
            if not snapshots:
                report.parent_gaps.append(
                    {"entity": entity_id, "reason": "tombstone has no parent snapshot"}
                )
                continue
            chosen = max((item[1] for item in snapshots), key=lambda item: item.revision)
        if isinstance(chosen, AssetManifest):
            latest_assets[entity_id] = chosen
        elif isinstance(chosen, AlbumManifest):
            latest_albums[entity_id] = chosen
        elif isinstance(chosen, PersonManifest):
            latest_people[entity_id] = chosen
    by_asset = sorted(latest_assets.values(), key=lambda x: str(x.asset_id))
    by_album = sorted(latest_albums.values(), key=lambda x: str(x.album_id))
    with catalog.writer():
        for record in by_asset:
            tombstone = tombstones.get(("asset", str(record.asset_id)))
            projection = _asset_projection(record, tombstone.deleted_at if tombstone else None)
            catalog.apply(projection)
            report.projected_assets += 1
        for record in by_album:
            tombstone = tombstones.get(("album", str(record.album_id)))
            catalog.apply_album(
                _album_projection(record, tombstone.deleted_at if tombstone else None)
            )
            report.projected_albums += 1
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
                            result=result,
                            searchable_text="",
                            is_current=True,
                            semantic_origin="computed",
                            created_at=run.created_at.replace("Z", "+00:00"),
                        )
                    )
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
        for record in sorted(latest_people.values(), key=lambda x: str(x.person_id)):
            tombstone = tombstones.get(("person", str(record.person_id)))
            if hasattr(catalog, "apply_person"):
                catalog.apply_person(record, record.face_ids)
            report.projected_people += 1
    _write_checkpoint(
        storage, checkpoint_key, {"schemaVersion": 1, "lastKey": None, "complete": True}
    )
    return report.as_dict()


def compare_projections(expected: Catalog, actual: Catalog) -> dict[str, Any]:
    """Compare durable user-visible projections, ignoring operational queues."""

    def normalize(value):
        if isinstance(value, dict):
            return {key: normalize(item) for key, item in value.items()}
        if isinstance(value, list):
            return [normalize(item) for item in value]
        return value[:-6] + "Z" if isinstance(value, str) and value.endswith("+00:00") else value

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
    return {
        "match": expected_assets == actual_assets
        and expected_albums == actual_albums
        and not any(people.values()),
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
    }
