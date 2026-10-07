"""Deterministic S3/PostgreSQL reconciliation.

This is deliberately separate from the live catalog writers.  PostgreSQL is
still the authority for normal requests; an explicit ``apply`` run may repair
the derived projection from validated immutable records, but never resolves a
conflict by overwriting a live row.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import insert, select

from photo_server.catalog import analysis_runs, faces, people
from photo_server.manifests import (
    AlbumManifest,
    AssetManifest,
    PersonManifest,
    ProcessingArtifact,
    Tombstone,
    encode,
)
from photo_server.rebuild import (
    MANIFEST_PREFIXES,
    _album_projection,
    _asset_projection,
    _decode_record,
    _entity,
    _history,
    _key_identity,
    _verify_object,
)


@dataclass
class ReconciliationReport:
    status: str = "complete"
    scanned: int = 0
    matched: int = 0
    missing: list[dict[str, str]] = field(default_factory=list)
    divergent: list[dict[str, str]] = field(default_factory=list)
    orphaned: list[dict[str, str]] = field(default_factory=list)
    unresolved: list[dict[str, str]] = field(default_factory=list)
    conflicting: list[dict[str, str]] = field(default_factory=list)
    malformed: list[dict[str, str]] = field(default_factory=list)
    skipped: int = 0
    repaired: int = 0
    failed: list[dict[str, str]] = field(default_factory=list)
    checkpoint: str | None = None
    dry_run: bool = True
    apply_requested: bool = False
    # Internal compatibility fields used by the strict Phase 4 scanner
    validated: int = 0
    objects_checked: int = 0
    missing_objects: list[dict[str, str]] = field(default_factory=list)
    checksum_mismatches: list[dict[str, str]] = field(default_factory=list)
    malformed_manifests: list[dict[str, str]] = field(default_factory=list)
    parent_gaps: list[dict[str, str]] = field(default_factory=list)
    conflicting_operation_ids: list[dict[str, str]] = field(default_factory=list)
    duplicate_revisions: list[dict[str, str]] = field(default_factory=list)
    multiple_valid_heads: list[dict[str, str]] = field(default_factory=list)
    unresolved_references: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "dryRun": self.dry_run,
            "applyRequested": self.apply_requested,
            "checkpoint": self.checkpoint,
            "scanned": self.scanned,
            "matched": self.matched,
            "missing": self.missing,
            "divergent": self.divergent,
            "orphaned": self.orphaned,
            "unresolved": self.unresolved,
            "conflicting": self.conflicting,
            "malformed": self.malformed,
            "skipped": self.skipped,
            "repaired": self.repaired,
            "failed": self.failed,
            "counts": {
                "scanned": self.scanned,
                "matched": self.matched,
                "missing": len(self.missing),
                "divergent": len(self.divergent),
                "orphaned": len(self.orphaned),
                "unresolved": len(self.unresolved),
                "conflicting": len(self.conflicting),
                "malformed": len(self.malformed),
                "skipped": self.skipped,
                "repaired": self.repaired,
                "failed": len(self.failed),
            },
            "authority": "postgres",
            "queuesAndLeases": "untouched",
        }


def _checkpoint(storage, key: str, value: dict[str, Any], dry_run: bool) -> None:
    if not dry_run:
        storage.put_json_mutable(key, value)


def _issue(bucket: list[dict[str, str]], category: str, key: str, reason: str) -> None:
    bucket.append({"category": category, "key": key, "reason": reason})


def _norm(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _norm(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_norm(item) for item in value]
    if hasattr(value, "isoformat"):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, str) and value.endswith("+00:00"):
        return value[:-6] + "Z"
    return value


def _compare(report: ReconciliationReport, kind: str, expected: dict, actual: dict) -> None:
    for key in sorted(set(expected) - set(actual)):
        _issue(report.missing, kind, key, "durable projection row is missing from PostgreSQL")
    for key in sorted(set(actual) - set(expected)):
        _issue(report.orphaned, kind, key, "PostgreSQL row has no selected S3 projection")
    for key in sorted(set(expected) & set(actual)):
        if _norm(expected[key]) == _norm(actual[key]):
            report.matched += 1
        else:
            _issue(report.divergent, kind, key, "durable projection differs from S3")


def _scan(storage, report: ReconciliationReport, checkpoint_id: str, resume: bool, stop_after: int | None):
    checkpoint_key = f"indexes/checkpoints/reconciliation-{checkpoint_id}.json"
    cursor = None
    if resume and storage.head(checkpoint_key) is not None and not report.dry_run:
        checkpoint = storage.get_json(checkpoint_key)
        if checkpoint.get("complete"):
            return {}, set(), set(), checkpoint_key, True
        cursor = checkpoint.get("lastKey")
    keys = sorted(key for prefix, _ in MANIFEST_PREFIXES for key in storage.keys(prefix))
    records: dict[tuple[str, str], list[tuple[str, Any]]] = {}
    referenced: set[str] = set()
    for key in keys:
        if cursor and key <= cursor:
            continue
        identity = _key_identity(key)
        report.scanned += 1
        if identity is None:
            _issue(report.malformed, "manifest-key", key, "key does not match canonical layout")
            continue
        kind = "tombstone" if identity[0] == "tombstone" else identity[0]
        codec_kind = "processing-artifact" if kind == "processing" else kind
        try:
            body = storage.read_bytes(key)
            record = _decode_record(storage, key, codec_kind, report)
            if record is None:
                continue
            if body != encode(record):
                _issue(report.malformed, "manifest", key, "manifest is not canonical")
                continue
            actual = _entity(record)
            expected_identity = (
                (record.entity_type, str(record.entity_id), record.revision)
                if isinstance(record, Tombstone)
                else identity
            )
            if actual != expected_identity:
                _issue(report.malformed, "manifest-identity", key, "record identity differs from key")
                continue
            records.setdefault((actual[0], actual[1]), []).append((key, record))
            if isinstance(record, AssetManifest):
                for blob in record.blobs:
                    referenced.add(blob.object_key)
                    _verify_object(storage, blob.object_key, blob.size_bytes, blob.sha256, report)
                for ref in record.processing:
                    referenced.add(ref.artifact_key)
                    if storage.head(ref.artifact_key) is None:
                        _issue(report.unresolved, "processing", ref.artifact_key, "asset references missing artifact object")
            elif isinstance(record, ProcessingArtifact):
                referenced.add(record.result_object.object_key)
                _verify_object(storage, record.result_object.object_key, record.result_object.size_bytes, record.result_object.sha256, report)
            report.checkpoint = key
            _checkpoint(storage, checkpoint_key, {"schemaVersion": 1, "lastKey": key}, report.dry_run)
            if stop_after is not None and report.scanned >= stop_after:
                report.status = "paused"
                return records, referenced, set(), checkpoint_key, False
        except Exception as error:  # malformed/corrupt remote records are reportable
            _issue(report.failed, "scan", key, str(error))
    return records, referenced, set(keys), checkpoint_key, False


def reconcile_s3_to_postgres(storage, catalog, *, checkpoint_id: str = "default", resume: bool = True,
                             dry_run: bool = True, apply: bool = False, report_only: bool = False,
                             stop_after: int | None = None) -> dict[str, Any]:
    """Reconcile immutable S3 state with the durable PostgreSQL projection."""
    report = ReconciliationReport(dry_run=dry_run, apply_requested=apply and not report_only)
    if apply and report_only:
        _issue(report.failed, "options", checkpoint_id, "apply and report-only are mutually exclusive")
        report.status = "failed"
        return report.as_dict()
    if dry_run:
        resume = False  # dry-run must never consume or write a mutable checkpoint
    records, referenced, keys, checkpoint_key, already_complete = _scan(
        storage, report, checkpoint_id, resume, stop_after
    )
    report.missing.extend(report.missing_objects)
    report.divergent.extend(report.checksum_mismatches)
    report.malformed.extend(report.malformed_manifests)
    report.conflicting.extend(report.conflicting_operation_ids)
    report.unresolved.extend(report.unresolved_references)
    report.malformed.extend(report.parent_gaps)
    report.conflicting.extend(report.duplicate_revisions)
    report.conflicting.extend(report.multiple_valid_heads)
    if report.status == "paused":
        return report.as_dict()
    if already_complete:
        return report.as_dict()

    selected: dict[tuple[str, str], Any] = {}
    tombstones: dict[tuple[str, str], Tombstone] = {}
    for identity, values in sorted(records.items()):
        if identity[0] in {"face", "processing"}:
            continue
        chosen = _history(values, report, identity[0], identity[1])
        if chosen is not None:
            selected[identity] = chosen
            if isinstance(chosen, Tombstone):
                tombstones[(chosen.entity_type, str(chosen.entity_id))] = chosen

    if report.malformed or report.failed:
        report.status = "failed"
        return report.as_dict()

    expected_assets, expected_albums, expected_people = {}, {}, {}
    for (kind, entity_id), record in selected.items():
        if isinstance(record, Tombstone):
            candidates = [
                value for _key, value in records.get((kind, entity_id), [])
                if not isinstance(value, Tombstone) and value.revision <= record.parent_revision
            ]
            if not candidates:
                _issue(report.unresolved, "tombstone", f"{kind}:{entity_id}", "tombstone has no parent snapshot")
                continue
            record = max(candidates, key=lambda value: value.revision)
        tombstone = tombstones.get((kind, entity_id))
        if isinstance(record, AssetManifest):
            expected_assets[entity_id] = _asset_projection(record, tombstone.deleted_at if tombstone else None).document()
        elif isinstance(record, AlbumManifest):
            expected_albums[entity_id] = _album_projection(record, tombstone.deleted_at if tombstone else None).document()
        elif isinstance(record, PersonManifest):
            expected_people[entity_id] = {"personId": entity_id, "displayName": record.display_name,
                                          "createdAt": record.created_at, "faceIds": [str(x) for x in record.face_ids]}

    actual_assets = {str(x.asset_id): x.document() for x in catalog.all_assets()}
    actual_albums = {str(x.album_id): x.document() for x in catalog.all_albums()}
    actual_people = {x["personId"]: x for x in catalog.all_people()}
    _compare(report, "asset", expected_assets, actual_assets)
    _compare(report, "album", expected_albums, actual_albums)
    _compare(report, "person", expected_people, actual_people)

    # Ordered memberships and tombstones are explicitly compared, not hidden
    # inside an album JSON comparison.
    expected_memberships = {key: list(value.get("assetIds", [])) for key, value in expected_albums.items()}
    with catalog.engine.connect() as connection:
        rows = connection.execute(select(__import__("photo_server.catalog", fromlist=["album_assets"]).album_assets)).mappings()
        actual_memberships: dict[str, list[str]] = {}
        for row in sorted(rows, key=lambda x: (x["album_id"], x["position"], x["asset_id"])):
            actual_memberships.setdefault(row["album_id"], []).append(row["asset_id"])
    _compare(report, "ordered-membership", expected_memberships, actual_memberships)
    for identity, tombstone in sorted(tombstones.items()):
        actual = actual_assets.get(identity[1]) if identity[0] == "asset" else actual_albums.get(identity[1])
        if actual is None:
            _issue(report.missing, "tombstone", f"{identity[0]}:{identity[1]}", "tombstone target is missing")
        elif actual.get("deletedAt") != tombstone.deleted_at:
            _issue(report.divergent, "tombstone", f"{identity[0]}:{identity[1]}", "deletedAt differs")
    for kind, values in sorted((("asset", actual_assets), ("album", actual_albums))):
        for entity_id, document in values.items():
            if document.get("deletedAt") and (kind, entity_id) not in tombstones:
                _issue(report.missing, "tombstone", f"{kind}:{entity_id}", "deleted PostgreSQL row has no S3 tombstone")

    # Face and processing rows are durable-derived records; queues and leases
    # are intentionally never selected or written here.
    expected_faces = {key[1]: values[0][1].to_dict() for key, values in records.items() if key[0] == "face"}
    expected_runs = {key[1]: values[0][1] for key, values in records.items() if key[0] == "processing"}
    with catalog.engine.connect() as connection:
        actual_faces = {row["id"]: dict(row) for row in connection.execute(select(faces)).mappings()}
        actual_runs = {row["id"]: dict(row) for row in connection.execute(select(analysis_runs)).mappings()}
    face_projection = {key: {"id": value["id"], "asset_id": value["asset_id"], "analysis_run_id": value["analysis_run_id"],
                             "person_id": value["person_id"], "face_index": value["face_index"], "bounding_box": value["bounding_box"],
                             "confidence": value["confidence"], "embedding": value["embedding"]} for key, value in actual_faces.items()}
    expected_face_projection = {key: {"id": key, "asset_id": str(value["assetId"]), "analysis_run_id": str(value["analysisRunId"]),
                                     "person_id": str(value["personId"]), "face_index": value["faceIndex"], "bounding_box": value["boundingBox"],
                                     "confidence": value["confidence"], "embedding": value["embedding"]} for key, value in expected_faces.items()}
    _compare(report, "face", expected_face_projection, face_projection)
    expected_run_projection = {key: {"id": key, "asset_id": str(value.asset_id), "analysis_type": value.processing_type,
                                     "pipeline_version": value.pipeline_version, "input_hash": value.input_sha256,
                                     "object_key": value.source_object_key or value.result_object.object_key} for key, value in expected_runs.items()}
    actual_run_projection = {key: {"id": key, "asset_id": value["asset_id"], "analysis_type": value["analysis_type"],
                                   "pipeline_version": value["pipeline_version"], "input_hash": value["input_hash"], "object_key": value["object_key"]}
                             for key, value in actual_runs.items()}
    _compare(report, "analysis-run", expected_run_projection, actual_run_projection)
    asset_ids = set(expected_assets)
    person_ids = set(expected_people)
    run_ids = set(expected_runs)
    for key, value in expected_faces.items():
        for category, reference, known in (
            ("asset", value["assetId"], asset_ids),
            ("person", value["personId"], person_ids),
            ("analysis-run", value["analysisRunId"], run_ids),
        ):
            if str(reference) not in known:
                _issue(report.unresolved, category, key, f"face references missing {category}")
    for key, value in expected_people.items():
        for face_id in value["faceIds"]:
            if face_id not in expected_faces:
                _issue(report.unresolved, "face", key, f"person references missing face {face_id}")

    for key in sorted(set(storage.keys("objects/")) - referenced):
        _issue(report.orphaned, "object", key, "immutable object is not referenced by a durable manifest")
    for key, values in records.items():
        if key[0] in {"face", "processing"} and not values:
            _issue(report.orphaned, "manifest", str(key), "manifest has no record")

    if (
        report.unresolved
        or report.conflicting
        or report.malformed
        or report.failed
        or report.missing_objects
        or report.checksum_mismatches
    ):
        report.status = "failed"
    elif apply and not dry_run and not report_only:
        report.status = "complete"
        # Only repair absent rows.  Divergent rows remain PostgreSQL-authoritative
        # and are never silently overwritten by maintenance.
        for entity_id, document in expected_assets.items():
            if entity_id not in actual_assets:
                from photo_server.models import Manifest
                catalog.apply(Manifest.model_validate(document))
                report.repaired += 1
        for entity_id, document in expected_albums.items():
            if entity_id not in actual_albums:
                from photo_server.models import Album
                catalog.apply_album(Album.model_validate(document))
                report.repaired += 1
        if not (report.unresolved or report.conflicting or report.malformed or report.failed):
            with catalog.engine.begin() as connection:
                for entity_id, _document in expected_people.items():
                    if entity_id not in actual_people:
                        person = next(
                            value for key, value in selected.items()
                            if key == ("person", entity_id)
                        )
                        connection.execute(
                            insert(people).values(
                                id=entity_id,
                                display_name=person.display_name,
                                created_at=person.created_at.replace("Z", "+00:00"),
                            )
                        )
                        report.repaired += 1
                for entity_id, artifact in expected_runs.items():
                    if entity_id not in actual_runs:
                        result = json.loads(storage.read_bytes(artifact.result_object.object_key))
                        connection.execute(
                            insert(analysis_runs).values(
                                id=entity_id,
                                asset_id=str(artifact.asset_id),
                                analysis_type=artifact.processing_type,
                                model_name=artifact.model_name or "",
                                model_version=artifact.model_version or "",
                                pipeline_version=artifact.pipeline_version,
                                input_hash=artifact.input_sha256,
                                object_key=artifact.source_object_key or artifact.result_object.object_key,
                                result=result,
                                searchable_text="",
                                is_current=True,
                                semantic_origin="computed",
                                created_at=artifact.created_at.replace("Z", "+00:00"),
                            )
                        )
                        report.repaired += 1
                for entity_id, value in expected_faces.items():
                    if entity_id not in actual_faces:
                        connection.execute(
                            insert(faces).values(
                                id=entity_id,
                                asset_id=str(value["assetId"]),
                                analysis_run_id=str(value["analysisRunId"]),
                                person_id=str(value["personId"]),
                                face_index=value["faceIndex"],
                                bounding_box=value["boundingBox"],
                                confidence=value["confidence"],
                                embedding=value["embedding"],
                            )
                        )
                        report.repaired += 1
    else:
        report.status = "complete"
    if not dry_run:
        _checkpoint(storage, checkpoint_key, {"schemaVersion": 1, "complete": True, "lastKey": None}, False)
    return report.as_dict()


reconcile = reconcile_s3_to_postgres
