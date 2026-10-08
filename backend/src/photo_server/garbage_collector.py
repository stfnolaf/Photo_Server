"""Fail-closed, dry-run-only object retention and reachability planning.

The planner intentionally has no deletion path. It validates the complete
canonical namespace before reporting an object as reclaimable; an incomplete,
malformed, or unavailable scan produces an empty candidate set.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from photo_server.fingerprints import BURST_HASH_VERSION
from photo_server.manifests import (
    FaceManifest,
    FingerprintManifest,
    ManifestCodecError,
    ProcessingArtifact,
    decode,
    encode,
)

CANONICAL_PREFIXES = (
    "library-state/",
    "manifests/",
    "tombstones/",
    "objects/",
    "indexes/checkpoints/",
    "indexes/recovery-checkpoints/",
    "reconciliation/",
    "incoming/",
)
MANIFEST_KINDS = {
    "assets": "asset",
    "albums": "album",
    "people": "person",
    "faces": "face",
    "processing": "processing-artifact",
    "fingerprints": "fingerprint",
    "bursts": "burst",
}


@dataclass(frozen=True)
class RetentionPolicy:
    """Retention is measured from the canonical record/object timestamp."""

    deleted_days: int = 30
    historical_revision_days: int = 90
    tombstone_days: int = 90
    original_object_days: int = 30
    processing_artifact_days: int = 30
    temporary_upload_days: int = 2
    checkpoint_days: int = 30
    reconciliation_days: int = 30
    backup_checkpoint_days: int = 168

    def as_dict(self) -> dict[str, int]:
        return {
            "deletedDays": self.deleted_days,
            "historicalRevisionDays": self.historical_revision_days,
            "tombstoneDays": self.tombstone_days,
            "originalObjectDays": self.original_object_days,
            "processingArtifactDays": self.processing_artifact_days,
            "temporaryUploadDays": self.temporary_upload_days,
            "checkpointDays": self.checkpoint_days,
            "reconciliationDays": self.reconciliation_days,
            "backupCheckpointDays": self.backup_checkpoint_days,
        }


@dataclass
class GarbageCollectionReport:
    status: str = "complete"
    as_of: str = ""
    scanned: int = 0
    protected: list[dict[str, Any]] = field(default_factory=list)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    unresolved_references: list[dict[str, str]] = field(default_factory=list)
    safety_warnings: list[dict[str, str]] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    bytes_by_category: dict[str, int] = field(default_factory=dict)
    checkpoint: str | None = None
    deletion_enabled: bool = False

    @property
    def failed(self) -> bool:
        return self.status == "failed" or bool(self.errors or self.unresolved_references)

    def as_dict(self) -> dict[str, Any]:
        candidates = sorted(self.candidates, key=lambda x: (x["key"], x["category"]))
        return {
            "status": "failed" if self.failed else self.status,
            "mode": "dry-run",
            "deletionEnabled": False,
            "asOf": self.as_of,
            "scanned": self.scanned,
            "candidates": candidates if not self.failed else [],
            "candidateCount": len(candidates) if not self.failed else 0,
            "candidateBytes": sum(x["sizeBytes"] for x in candidates) if not self.failed else 0,
            "protected": sorted(self.protected, key=lambda x: (x["key"], x["reason"])),
            "bytesByCategory": dict(sorted(self.bytes_by_category.items())),
            "unresolvedReferences": sorted(self.unresolved_references, key=lambda x: (x["key"], x["reason"])),
            "safetyWarnings": sorted(self.safety_warnings, key=lambda x: (x["key"], x["reason"])),
            "errors": sorted(self.errors, key=lambda x: (x["key"], x["reason"])),
            "checkpoint": self.checkpoint,
        }


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _head_time(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    if isinstance(value, str):
        return _parse_time(value)
    raise ValueError("object LastModified is not a timestamp")


def _identity(key: str) -> tuple[str, str, int] | None:
    parts = key.split("/")
    if not key.endswith(".json"):
        return None
    try:
        if key == "library-state/bursts.json":
            return "burst-current", "state", 1
        if len(parts) == 4 and parts[0] == "manifests":
            kind = MANIFEST_KINDS.get(parts[1])
            if kind in {"asset", "album", "person"}:
                return kind, parts[2], int(parts[3][:-5])
        if len(parts) == 3 and parts[0] == "manifests" and parts[1] in {"faces", "processing"}:
            return MANIFEST_KINDS[parts[1]], parts[2][:-5], 1
        if len(parts) == 3 and parts[0] == "manifests" and parts[1] == "bursts":
            return "burst", "state", int(parts[2][:-5])
        if len(parts) == 4 and parts[0] == "manifests" and parts[1] == "fingerprints":
            return "fingerprint", f"{parts[2]}:{parts[3][:-5]}", 1
        if len(parts) == 4 and parts[0] == "tombstones":
            return "tombstone", f"{parts[1]}:{parts[2]}", int(parts[3][:-5])
    except (KeyError, ValueError):
        return None
    return None


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def collect_garbage(
    storage: Any,
    *,
    policy: RetentionPolicy | None = None,
    checkpoint_id: str = "default",
    resume: bool = True,
    stop_after: int | None = None,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Return a deterministic dry-run plan.  No delete operation is reachable."""
    policy = policy or RetentionPolicy()
    report = GarbageCollectionReport()
    checkpoint_key = f"indexes/checkpoints/gc-{checkpoint_id}.json"
    try:
        saved = storage.get_json(checkpoint_key) if resume and storage.head(checkpoint_key) else {}
        if as_of is None:
            as_of = saved.get("asOf") or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        now = _parse_time(as_of)
        report.as_of = as_of
        report.checkpoint = checkpoint_key

        keys: dict[str, list[str]] = {prefix: sorted(storage.keys(prefix)) for prefix in CANONICAL_PREFIXES}
        manifest_keys = sorted(
            keys["library-state/"] + keys["manifests/"] + keys["tombstones/"]
        )
        records: dict[str, Any] = {}
        histories: dict[tuple[str, str], list[tuple[str, Any]]] = {}
        auxiliary: list[tuple[str, Any]] = []
        tombstones: list[tuple[str, Any]] = []
        for key in manifest_keys:
            identity = _identity(key)
            if identity is None:
                report.errors.append({"key": key, "reason": "unknown or malformed canonical key"})
                continue
            kind = identity[0]
            try:
                body = storage.read_bytes(key)
                record = decode(body, "burst" if kind == "burst-current" else kind)
                if body != encode(record):
                    raise ManifestCodecError("record is not canonical")
            except Exception as error:
                report.errors.append({"key": key, "reason": f"manifest validation failed: {error}"})
                continue
            records[key] = record
            if kind == "tombstone":
                tombstones.append((key, record))
            else:
                if kind in {"asset", "album", "person", "burst", "burst-current"}:
                    entity = (
                        (kind, "state")
                        if kind in {"burst", "burst-current"}
                        else (kind, str(getattr(record, f"{kind}_id")))
                    )
                    histories.setdefault(entity, []).append((key, record))
                else:
                    auxiliary.append((key, record))

        retained_keys: set[str] = set()
        referenced: dict[str, set[str]] = {}
        expected_objects: dict[str, tuple[str, int | None]] = {}
        tombstone_by_entity: dict[tuple[str, str], Any] = {}
        for _, record in tombstones:
            identity = (record.entity_type, str(record.entity_id))
            prior = tombstone_by_entity.get(identity)
            if prior is None or record.revision > prior.revision:
                tombstone_by_entity[identity] = record
        for entity, values in histories.items():
            values.sort(key=lambda item: (item[1].revision, item[0]))
            latest = values[-1][1]
            if entity[0] == "burst-current":
                retained_keys.add(values[-1][0])
                continue
            revisions = [record.revision for _, record in values]
            if len(revisions) != len(set(revisions)):
                report.errors.append({"key": entity[1], "reason": "duplicate manifest revision"})
            expected_revision = 1
            for _, record in values:
                if record.revision != expected_revision or record.parent_revision != (expected_revision - 1 or None):
                    report.errors.append({"key": entity[1], "reason": "manifest revision ancestry is incomplete"})
                    break
                expected_revision += 1
            for key, record in values:
                age = now - _parse_time(record.created_at)
                tombstone = tombstone_by_entity.get(entity)
                restored_after_tombstone = tombstone is not None and latest.revision > tombstone.revision
                tombstone_age = now - _parse_time(tombstone.deleted_at) if tombstone else None
                deleted_at = getattr(record, "deleted_at", None)
                deleted_age = now - _parse_time(deleted_at) if deleted_at else None
                current_retained = (
                    restored_after_tombstone
                    or (tombstone_age is None and deleted_age is None)
                    or (tombstone_age is not None and tombstone_age <= timedelta(days=policy.tombstone_days))
                    or (deleted_age is not None and deleted_age <= timedelta(days=policy.deleted_days))
                )
                if (record is latest and current_retained) or age <= timedelta(days=policy.historical_revision_days):
                    retained_keys.add(key)
            if getattr(latest, "deleted_at", None) and entity not in tombstone_by_entity:
                report.unresolved_references.append(
                    {"key": entity[1], "reason": "deleted entity has no tombstone root"}
                )
        for key, tombstone in tombstones:
            parent_revisions = {
                record.revision
                for history_key, record in histories.get((tombstone.entity_type, str(tombstone.entity_id)), [])
                if history_key in records
            }
            if tombstone.parent_revision not in parent_revisions:
                report.errors.append({"key": key, "reason": "tombstone parent manifest is missing"})
            age = now - _parse_time(tombstone.deleted_at)
            if age <= timedelta(days=policy.tombstone_days):
                retained_keys.add(key)
                for history_key, _ in histories.get((tombstone.entity_type, str(tombstone.entity_id)), []):
                    retained_keys.add(history_key)

        # Album membership and person->face->asset links are durable reachability roots.
        faces_by_id = {str(r.face_id): r for _, r in auxiliary if isinstance(r, FaceManifest)}
        for key, record in auxiliary:
            created_at = getattr(record, "created_at", None)
            age = now - _parse_time(created_at) if created_at else timedelta(0)
            if age <= timedelta(days=policy.processing_artifact_days):
                retained_keys.add(key)
            if isinstance(record, FingerprintManifest):
                retained_keys.add(key)
        for key in sorted(retained_keys):
            record = records.get(key)
            if record is None:
                continue
            for attr in ("blobs", "processing", "result_object"):
                value = getattr(record, attr, None)
                if attr == "blobs":
                    for blob in value or ():
                        referenced.setdefault(blob.object_key, set()).add(key)
                        expected_objects[blob.object_key] = (blob.sha256, blob.size_bytes)
                elif attr == "processing":
                    for ref in value or ():
                        referenced.setdefault(ref.artifact_key, set()).add(key)
                        referenced.setdefault(f"objects/{ref.artifact_sha256}", set()).add(key)
                        expected_objects[ref.artifact_key] = (ref.artifact_sha256, None)
                        expected_objects[f"objects/{ref.artifact_sha256}"] = (ref.artifact_sha256, None)
                elif value is not None:
                    referenced.setdefault(value.object_key, set()).add(key)
            if hasattr(record, "asset_ids"):
                for asset_id in record.asset_ids:
                    for history_key, _asset in histories.get(("asset", str(asset_id)), []):
                        if history_key in retained_keys:
                            referenced.setdefault(history_key, set()).add(key)
            if hasattr(record, "face_ids"):
                for face_id in record.face_ids:
                    face = faces_by_id.get(str(face_id))
                    if face:
                        for asset_key, _ in histories.get(("asset", str(face.asset_id)), []):
                            if asset_key in retained_keys:
                                referenced.setdefault(asset_key, set()).add(key)
                    else:
                        report.unresolved_references.append({"key": key, "reason": f"missing face {face_id}"})

        for key, record in auxiliary:
            if isinstance(record, ProcessingArtifact):
                referenced.setdefault(record.result_object.object_key, set()).add(key)
                expected_objects[record.result_object.object_key] = (
                    record.result_object.sha256,
                    record.result_object.size_bytes,
                )
                if record.source_object_key:
                    referenced.setdefault(record.source_object_key, set()).add(key)

        fingerprint_assets = {
            (str(record.asset_id), record.algorithm_version): key
            for key, record in auxiliary
            if isinstance(record, FingerprintManifest)
        }
        burst_history = histories.get(("burst-current", "state"), []) or histories.get(
            ("burst", "state"), []
        )
        if burst_history:
            burst_key, burst = max(burst_history, key=lambda item: item[1].revision)
            for cluster in burst.clusters:
                for asset_id in cluster.asset_ids:
                    asset_id = str(asset_id)
                    fingerprint_key = fingerprint_assets.get((asset_id, BURST_HASH_VERSION))
                    if fingerprint_key is None:
                        report.unresolved_references.append(
                            {
                                "key": burst_key,
                                "reason": f"burst member {asset_id} has no fingerprint manifest",
                            }
                        )
                    else:
                        referenced.setdefault(fingerprint_key, set()).add(burst_key)
                    asset_history = histories.get(("asset", asset_id), [])
                    if not asset_history:
                        report.unresolved_references.append(
                            {
                                "key": burst_key,
                                "reason": f"burst member {asset_id} has no asset manifest",
                            }
                        )
                    for asset_key, _record in asset_history:
                        if asset_key in retained_keys:
                            referenced.setdefault(asset_key, set()).add(burst_key)

        # Recovery checkpoints are roots.  Their payload is intentionally opaque,
        # but exact canonical key strings are safe to recognize.
        for checkpoint_prefix in ("indexes/checkpoints/", "indexes/recovery-checkpoints/"):
            for key in sorted(keys[checkpoint_prefix]):
                try:
                    checkpoint = storage.get_json(key)
                    head = storage.head(key)
                    modified = _head_time(head.get("LastModified")) if head else None
                    if modified is None or (now - modified <= timedelta(days=policy.checkpoint_days)):
                        retained_keys.add(key)
                        root_payload = {k: value for k, value in checkpoint.items() if k not in {"lastKey", "asOf"}}
                        for value in _strings(root_payload):
                            if value.startswith(("objects/", "incoming/", "manifests/", "tombstones/")):
                                referenced.setdefault(value, set()).add(key)
                except Exception as error:
                    report.errors.append({"key": key, "reason": f"checkpoint scan failed: {error}"})

        # Verify every durable object reference before looking at candidates.
        for object_key, _roots in sorted(referenced.items()):
            if not object_key.startswith(("objects/", "incoming/")):
                continue
            try:
                head = storage.head(object_key)
                if head is None:
                    report.unresolved_references.append({"key": object_key, "reason": "referenced object is missing"})
                else:
                    body = storage.read_bytes(object_key)
                    digest = hashlib.sha256(body).hexdigest()
                    expected_sha, expected_size = expected_objects.get(object_key, (None, None))
                    if expected_sha and digest != expected_sha:
                        report.unresolved_references.append({"key": object_key, "reason": "referenced object checksum mismatch"})
                    if expected_size is not None and (len(body) != expected_size or head.get("ContentLength") != expected_size):
                        report.unresolved_references.append({"key": object_key, "reason": "referenced object size mismatch"})
            except Exception as error:
                report.errors.append({"key": object_key, "reason": f"object head failed: {error}"})

        object_keys = sorted(keys["objects/"])
        temp_keys = sorted(keys["incoming/"])
        all_candidates = [(key, "content-addressed-object", policy.original_object_days) for key in object_keys]
        all_candidates += [(key, "temporary-upload", policy.temporary_upload_days) for key in temp_keys]
        all_candidates.sort(key=lambda item: item[0])
        # Roots and object metadata are recomputed on every invocation.  This
        # makes a resumed report complete and deterministic even if the bucket
        # changed while an earlier bounded scan was paused; ``lastKey`` remains
        # an audit/resume marker, never an authority to skip safety validation.
        scan_items = all_candidates[:stop_after] if stop_after is not None else all_candidates
        for key, category, age_days in scan_items:
            report.scanned += 1
            try:
                head = storage.head(key)
                if head is None:
                    report.errors.append({"key": key, "reason": "listed object disappeared before head"})
                    continue
                modified = _head_time(head.get("LastModified"))
                if modified is None:
                    report.safety_warnings.append({"key": key, "reason": "object age is unknown; protected"})
                    continue
                age = now - modified
                if key in referenced:
                    report.protected.append({"key": key, "reason": "reachable from retained durable root", "referencing": sorted(referenced[key])})
                elif age >= timedelta(days=age_days):
                    report.candidates.append({
                        "key": key, "category": category, "sizeBytes": int(head.get("ContentLength", 0)),
                        "reason": "unreferenced and retention age expired", "referencing": [],
                        "retentionAgeSeconds": int(age.total_seconds()), "policy": f"{category}:{age_days}d",
                    })
                    report.bytes_by_category[category] = report.bytes_by_category.get(category, 0) + int(head.get("ContentLength", 0))
                else:
                    report.protected.append({"key": key, "reason": "unreferenced but retention age not expired", "referencing": []})
            except Exception as error:
                report.errors.append({"key": key, "reason": f"object scan failed: {error}"})

        if stop_after is not None and len(scan_items) < len(all_candidates):
            report.status = "paused"
        checkpoint = {
            "schemaVersion": 1,
            "asOf": as_of,
            "complete": report.status == "complete",
            "lastKey": scan_items[-1][0] if report.status == "paused" and scan_items else None,
        }
        if hasattr(storage, "put_json_mutable"):
            storage.put_json_mutable(checkpoint_key, checkpoint)
        else:
            storage.put(checkpoint_key, json.dumps(checkpoint, sort_keys=True).encode(), "application/json")
    except Exception as error:
        report.status = "failed"
        report.errors.append({"key": "scan", "reason": str(error)})
        report.candidates.clear()
    if report.failed:
        report.candidates.clear()
    return report.as_dict()


garbage_collect = collect_garbage
