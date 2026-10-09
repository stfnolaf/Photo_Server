"""Read-only storage accounting and retention-risk reporting.

This module intentionally treats the S3 bucket as an inventory, not a cleanup
queue.  Only derived namespaces and temporary data can become candidates;
canonical objects and manifests are always protected.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from typing import Any

from photo_server.app_logging import log_event
from photo_server.config import Settings
from photo_server.manifests import decode, encode

CANONICAL_PREFIXES = ("objects/", "manifests/", "tombstones/", "library-state/")
KNOWN_PREFIXES = CANONICAL_PREFIXES + (
    "incoming/", "indexes/", "processing/", "ai/"
)


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return str(value)


def _age(now: datetime, modified: Any) -> int | None:
    if not isinstance(modified, datetime):
        return None
    return max(0, int((now - modified.astimezone(UTC)).total_seconds()))


def _items(storage: Any) -> list[dict]:
    if hasattr(storage, "objects"):
        return sorted(
            [item for item in storage.objects("") if not item["key"].startswith("backups/postgres/")],
            key=lambda item: item["key"],
        )
    result = []
    for key in sorted(storage.keys("")):
        if key.startswith("backups/postgres/"):
            continue
        head = storage.head(key) or {}
        result.append({"key": key, "sizeBytes": int(head.get("ContentLength", 0)), "lastModified": head.get("LastModified")})
    return result


def _namespace(key: str) -> str:
    if key.startswith("objects/"):
        return "canonical_objects"
    if key.startswith(("manifests/", "tombstones/", "library-state/")):
        return "manifests"
    if key.startswith("incoming/"):
        return "upload_staging"
    if key.startswith("indexes/recovery-checkpoints/"):
        return "recovery_checkpoints"
    if key.startswith("indexes/checkpoints/"):
        return "progress_records"
    if key.startswith(("processing/", "ai/", "analysis/", "analysis-stages/")):
        return "processing_artifacts"
    return "unknown"


def _manifest_kind(key: str) -> str | None:
    if key.startswith("manifests/assets/"):
        return "asset"
    if key.startswith("manifests/albums/"):
        return "album"
    if key.startswith("manifests/people/"):
        return "person"
    if key.startswith("manifests/faces/"):
        return "face"
    if key.startswith("manifests/fingerprints/"):
        return "fingerprint"
    if key.startswith("manifests/bursts/"):
        return "burst"
    if key.startswith("manifests/processing/"):
        return "processing-artifact"
    if key.startswith("tombstones/"):
        return "tombstone"
    if key == "library.json":
        return None
    return None


def _local_report(settings: Settings, catalog: Any | None, now: datetime) -> dict:
    result: dict[str, Any] = {"status": "ok", "errors": [], "filesystem": {}, "previewCache": {}}
    try:
        usage = shutil.disk_usage(settings.data_dir)
        result["filesystem"] = {
            "path": str(settings.data_dir), "totalBytes": usage.total,
            "usedBytes": usage.used, "freeBytes": usage.free,
            "warningThresholdBytes": settings.storage_free_space_warning_bytes,
            "hardThresholdBytes": settings.storage_free_space_hard_bytes,
        }
        if usage.free <= settings.storage_free_space_hard_bytes:
            result["status"] = "hard"
        elif usage.free <= settings.storage_free_space_warning_bytes:
            result["status"] = "warning"
    except OSError as error:
        result["status"] = "unavailable"
        result["errors"].append({"scope": "filesystem", "errorClass": type(error).__name__})

    cache = settings.data_dir / "cache"
    cache_files, cache_bytes, directories = 0, 0, set()
    if cache.exists():
        try:
            for path in cache.rglob("*"):
                if path.is_file():
                    cache_files += 1
                    cache_bytes += path.stat().st_size
                    directories.add(path.parent.name)
        except OSError as error:
            result["errors"].append({"scope": "preview-cache-files", "errorClass": type(error).__name__})
    rows = None
    if catalog is not None and hasattr(catalog, "preview_cache_inventory"):
        try:
            rows = catalog.preview_cache_inventory()
        except Exception as error:
            result["errors"].append({"scope": "preview-cache-rows", "errorClass": type(error).__name__})
    result["previewCache"] = {
        "fileCount": cache_files, "directoryCount": len(directories), "fileBytes": cache_bytes,
        "rowCount": len(rows) if rows is not None else None,
        "accountedBytes": sum(int(row.get("bytes", 0)) for row in rows) if rows is not None else None,
        "orphanedDirectories": [],
        "configuredLimitBytes": settings.cache_max_bytes,
        "status": "unavailable" if rows is None and catalog is not None else "ok",
    }
    if rows is not None:
        row_ids = {str(row["asset_id"]) for row in rows}
        result["previewCache"]["orphanedDirectories"] = sorted(
            name for name in directories if not any(name.startswith(asset_id + "-") for asset_id in row_ids)
        )
    result["temporaryDirectories"] = [
        {"path": str(path), "exists": path.exists()}
        for path in (settings.data_dir / "scratch", settings.data_dir / "tmp", settings.data_dir / "processing")
    ]
    return result


def storage_report(storage: Any, settings: Settings, catalog: Any | None = None, *, as_of: datetime | None = None) -> dict:
    now = as_of.astimezone(UTC) if as_of else _now()
    report: dict[str, Any] = {
        "schemaVersion": 1, "asOf": now.isoformat().replace("+00:00", "Z"),
        "status": "ok", "readOnly": True, "s3": {}, "integrity": {
            "missingReferences": [], "malformedManifests": [], "unsupportedManifests": [],
            "divergentOrOrphanedRecords": [], "retentionReview": [],
        }, "local": {}, "errors": [],
    }
    try:
        objects = _items(storage)
    except Exception as error:
        report["status"] = "unavailable"
        report["errors"].append({"scope": "s3", "errorClass": type(error).__name__})
        return report
    groups: dict[str, dict] = {}
    for item in objects:
        group = groups.setdefault(_namespace(item["key"]), {"bytes": 0, "objects": 0, "oldest": None})
        group["bytes"] += int(item.get("sizeBytes", 0))
        group["objects"] += 1
        modified = item.get("lastModified")
        if modified and (group["oldest"] is None or modified < group["oldest"]):
            group["oldest"] = modified
    for group in groups.values():
        group["oldest"] = _iso(group["oldest"])
    for name in ("canonical_objects", "manifests", "processing_artifacts", "upload_staging", "recovery_checkpoints", "progress_records", "unknown"):
        groups.setdefault(name, {"bytes": 0, "objects": 0, "oldest": None})
    report["s3"]["namespaces"] = groups
    report["s3"]["totalBytes"] = sum(item["sizeBytes"] for item in objects)
    report["s3"]["totalObjects"] = len(objects)
    manifest_items = [item for item in objects if _namespace(item["key"]) == "manifests"]
    revisions: dict[str, int] = {}
    for item in manifest_items:
        kind = _manifest_kind(item["key"]) or "other"
        revisions[kind] = revisions.get(kind, 0) + 1
    report["s3"]["manifestRevisionCount"] = len(manifest_items)
    report["s3"]["manifestRevisionsByKind"] = revisions
    processing_items = [item for item in objects if item["key"].startswith(("manifests/processing/", "analysis/", "analysis-stages/", "processing/", "ai/"))]
    legacy_analysis = [item for item in objects if item["key"].startswith("analysis/")]
    stage_cache = [item for item in objects if item["key"].startswith("analysis-stages/")]
    report["s3"]["processingArtifacts"] = {
        "bytes": sum(item["sizeBytes"] for item in processing_items),
        "objects": len(processing_items),
        "currentBytes": None,
        "historicalBytes": sum(item["sizeBytes"] for item in processing_items),
        "currentStatus": "unavailable_without_projection",
        "legacyAnalysisBytes": sum(item["sizeBytes"] for item in legacy_analysis),
        "legacyAnalysisObjects": len(legacy_analysis),
        "stageCacheBytes": sum(item["sizeBytes"] for item in stage_cache),
        "stageCacheObjects": len(stage_cache),
        "stageCacheRebuildable": True,
    }
    object_map = {item["key"]: item for item in objects}
    referenced: dict[str, list[str]] = {}
    for item in objects:
        key = item["key"]
        kind = _manifest_kind(key)
        if kind is None:
            continue
        try:
            body = storage.read_bytes(key)
            record = decode(body, kind)
            if body != encode(record):
                raise ValueError("record is not canonical JSON")
            for blob in getattr(record, "blobs", ()):
                referenced.setdefault(blob.object_key, []).append(key)
            for ref in getattr(record, "processing", ()):
                referenced.setdefault(ref.artifact_key, []).append(key)
                referenced.setdefault(f"objects/{ref.artifact_sha256}", []).append(key)
            result_object = getattr(record, "result_object", None)
            if result_object is not None:
                referenced.setdefault(result_object.object_key, []).append(key)
        except Exception as error:
            message = str(error)
            target = "unsupportedManifests" if "schema" in message.lower() or "version" in message.lower() else "malformedManifests"
            report["integrity"][target].append({"key": key, "error": message})
    for key, roots in sorted(referenced.items()):
        if key not in object_map:
            report["integrity"]["missingReferences"].append({"key": key, "referencing": sorted(set(roots))})
    for item in objects:
        key = item["key"]
        age = _age(now, item.get("lastModified"))
        if age is None:
            continue
        if key.startswith("incoming/") and age >= settings.upload_abandon_seconds:
            report["integrity"]["retentionReview"].append({"namespace": "upload_staging", "key": key, "sizeBytes": item["sizeBytes"], "ageSeconds": age, "referencing": sorted(set(referenced.get(key, []))), "retentionRule": f"stale after {settings.upload_abandon_seconds}s", "recoveryImpact": "abandoned upload only"})
        elif key.startswith("manifests/processing/") and age >= settings.storage_backup_max_age_seconds:
            report["integrity"]["retentionReview"].append({"namespace": "processing_artifacts", "key": key, "sizeBytes": item["sizeBytes"], "ageSeconds": age, "referencing": sorted(set(referenced.get(key, []))), "retentionRule": "historical processing artifact review", "recoveryImpact": "rebuildable derived analysis"})
    report["local"] = _local_report(settings, catalog, now)
    if report["local"].get("status") in {"warning", "hard", "unavailable"} or report["integrity"]["missingReferences"] or report["integrity"]["malformedManifests"]:
        report["status"] = "hard" if report["local"].get("status") == "hard" else "warning"
    log_event("storage_capacity_report", status=report["status"], total_bytes=report["s3"]["totalBytes"], retention_candidates=len(report["integrity"]["retentionReview"]))
    return report


def format_storage_report(report: dict) -> str:
    namespaces = report.get("s3", {}).get("namespaces", {})
    lines = [f"storage status: {report.get('status')}", f"S3 total: {report.get('s3', {}).get('totalBytes', 0)} bytes / {report.get('s3', {}).get('totalObjects', 0)} objects"]
    lines.extend(f"  {name}: {value['bytes']} bytes / {value['objects']} objects" for name, value in sorted(namespaces.items()))
    lines.append(f"retention review candidates: {len(report.get('integrity', {}).get('retentionReview', []))}")
    return "\n".join(lines)


def local_capacity_state(settings: Settings) -> str:
    """Return local upload-admission state without probing or mutating storage."""
    try:
        free = shutil.disk_usage(settings.data_dir).free
    except OSError:
        return "unavailable"
    if free <= settings.storage_free_space_hard_bytes:
        return "hard"
    if free <= settings.storage_free_space_warning_bytes:
        return "warning"
    return "ok"
