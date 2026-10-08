"""Immutable recovery checkpoints for canonical S3 state.

The builder is deliberately storage-and-database agnostic.  ``Storage``-like
objects are accepted so recovery can be exercised against a disposable S3
bucket without ever touching the production bucket.  Only the progress marker
is mutable; canonical records, copied objects, and the completed checkpoint
are immutable.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable

from photo_server.manifests import ManifestCodecError, canonical_json, decode

CHECKPOINT_SCHEMA_VERSION = 1
TOOL_VERSION = "recovery-v1"
S3_FORMAT_VERSION = 1
APPLICATION_VERSION = "0.6.0"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CANONICAL_PREFIXES = (
    ("library-state/", "burst"),
    ("manifests/assets/", "asset"),
    ("manifests/albums/", "album"),
    ("manifests/people/", "person"),
    ("manifests/faces/", "face"),
    ("manifests/processing/", "processing"),
    ("manifests/fingerprints/", "fingerprint"),
    ("manifests/bursts/", "burst"),
    ("tombstones/", "tombstone"),
)


class RecoveryError(RuntimeError):
    """A checkpoint operation failed closed."""


def _compatibility_metadata(manifest_versions: list[int], value: dict[str, Any] | None = None) -> dict[str, Any]:
    if value is not None:
        result = dict(value)
    else:
        try:
            from photo_server.migrations import available_migrations

            migration_head = max(item.version for item in available_migrations())
        except (ImportError, ValueError):
            migration_head = 0
        result = {
            "applicationVersion": os.environ.get("PHOTO_RELEASE_VERSION", APPLICATION_VERSION),
            "gitCommit": os.environ.get("PHOTO_GIT_COMMIT", "unrecorded"),
            "imageDigest": os.environ.get("PHOTO_IMAGE_DIGEST", "unrecorded"),
            "dependencyLockSha256": os.environ.get("PHOTO_DEPENDENCY_LOCK_SHA256", "unrecorded"),
            "s3FormatVersion": S3_FORMAT_VERSION,
            "manifestSchemaVersions": sorted(set(manifest_versions)),
            "minimumReaderVersion": os.environ.get("PHOTO_MIN_RECOVERY_READER_VERSION", APPLICATION_VERSION),
            "authorityMode": "s3",
            "projectionMigrationHead": migration_head,
        }
    if set(result) != {
        "applicationVersion", "gitCommit", "imageDigest", "dependencyLockSha256",
        "s3FormatVersion", "manifestSchemaVersions", "minimumReaderVersion",
        "authorityMode", "projectionMigrationHead",
    }:
        raise RecoveryError("invalid recovery compatibility metadata")
    if (
        not all(isinstance(result[key], str) and result[key] for key in (
            "applicationVersion", "gitCommit", "imageDigest", "dependencyLockSha256",
            "minimumReaderVersion", "authorityMode",
        ))
        or result["authorityMode"] != "s3"
        or result["s3FormatVersion"] != S3_FORMAT_VERSION
        or not isinstance(result["manifestSchemaVersions"], list)
        or not result["manifestSchemaVersions"]
        or any(isinstance(item, bool) or not isinstance(item, int) or item < 1 for item in result["manifestSchemaVersions"])
        or not isinstance(result["projectionMigrationHead"], int)
        or result["projectionMigrationHead"] < 0
    ):
        raise RecoveryError("invalid recovery compatibility metadata")
    return result


def _utc(value: str | None = None) -> str:
    if value:
        return value
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _put_immutable(storage: Any, key: str, body: bytes, mime: str = "application/octet-stream") -> bool:
    try:
        written = storage.put(key, body, mime)
    except Exception as exc:  # storage outages are intentionally non-success
        raise RecoveryError(f"destination write failed: {key}: {exc}") from exc
    if written is False:
        try:
            existing = storage.read_bytes(key)
        except Exception as exc:
            raise RecoveryError(f"destination conflict cannot be read: {key}: {exc}") from exc
        if existing != body:
            raise RecoveryError(f"immutable destination conflict: {key}")
    try:
        head = storage.head(key)
        if head is None or head.get("ContentLength") != len(body):
            raise RecoveryError(f"destination size verification failed: {key}")
        actual = _sha(storage.read_bytes(key))
    except RecoveryError:
        raise
    except Exception as exc:
        raise RecoveryError(f"destination verification failed: {key}: {exc}") from exc
    if actual != _sha(body):
        raise RecoveryError(f"destination checksum verification failed: {key}")
    return bool(written)


def _read_verified(storage: Any, key: str) -> tuple[bytes, dict[str, Any]]:
    try:
        head = storage.head(key)
        if head is None:
            raise RecoveryError(f"source object is missing: {key}")
        body = storage.read_bytes(key)
    except RecoveryError:
        raise
    except Exception as exc:
        raise RecoveryError(f"source read failed: {key}: {exc}") from exc
    size = head.get("ContentLength")
    digest = _sha(body)
    if size != len(body):
        raise RecoveryError(f"source size changed during read: {key}")
    return body, {"key": key, "sha256": digest, "sizeBytes": len(body)}


def _decode_kind(key: str, body: bytes, kind: str) -> dict[str, Any]:
    try:
        model = decode(body, "processing-artifact" if kind == "processing" else kind)
        document = model.to_dict()
        if canonical_json(document) != body:
            raise RecoveryError(f"non-canonical {kind} manifest: {key}")
        return document
    except (ManifestCodecError, ValueError, TypeError) as exc:
        raise RecoveryError(f"malformed {kind} manifest: {key}: {exc}") from exc


@dataclass(frozen=True)
class RecoveryCheckpoint:
    checkpoint_id: str
    checkpoint_timestamp: str
    source_bucket: str
    source_endpoint: str
    manifest_revisions: tuple[dict[str, Any], ...]
    objects: tuple[dict[str, Any], ...]
    postgres_dump: dict[str, Any] | None
    compatibility: dict[str, Any] = field(default_factory=dict)
    schema_versions: tuple[int, ...] = (1,)
    tool_version: str = TOOL_VERSION
    schema_version: int = CHECKPOINT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "checkpointId": self.checkpoint_id,
            "checkpointTimestamp": self.checkpoint_timestamp,
            "source": {"bucket": self.source_bucket, "endpoint": self.source_endpoint},
            "schemaVersions": list(self.schema_versions),
            "toolVersion": self.tool_version,
            "compatibility": self.compatibility,
            "manifestRevisions": list(self.manifest_revisions),
            "objects": list(self.objects),
            "postgresDump": self.postgres_dump,
        }


def encode_checkpoint(value: RecoveryCheckpoint | dict[str, Any]) -> bytes:
    payload = value.to_dict() if isinstance(value, RecoveryCheckpoint) else value
    required = {"schemaVersion", "checkpointId", "checkpointTimestamp", "source", "schemaVersions", "toolVersion", "compatibility", "manifestRevisions", "objects", "postgresDump"}
    if set(payload) != required:
        raise RecoveryError("checkpoint has unknown or missing fields")
    if payload["schemaVersion"] != CHECKPOINT_SCHEMA_VERSION or not isinstance(payload["checkpointId"], str) or not payload["checkpointId"]:
        raise RecoveryError("invalid checkpoint identity")
    if not isinstance(payload["source"], dict) or set(payload["source"]) != {"bucket", "endpoint"}:
        raise RecoveryError("invalid checkpoint source")
    if not isinstance(payload["manifestRevisions"], list) or not isinstance(payload["objects"], list):
        raise RecoveryError("invalid checkpoint inventory")
    if not isinstance(payload["checkpointTimestamp"], str) or not isinstance(payload["toolVersion"], str):
        raise RecoveryError("invalid checkpoint metadata")
    compatibility = payload["compatibility"]
    if not isinstance(compatibility, dict):
        raise RecoveryError("invalid recovery compatibility metadata")
    _compatibility_metadata([], compatibility)
    if not isinstance(payload["schemaVersions"], list) or any(
        isinstance(item, bool) or not isinstance(item, int) or item < 1
        for item in payload["schemaVersions"]
    ):
        raise RecoveryError("invalid schema versions")
    for item in payload["manifestRevisions"]:
        if not isinstance(item, dict) or set(item) != {"key", "sha256", "sizeBytes", "kind", "schemaVersion", "revision"}:
            raise RecoveryError("invalid manifest inventory entry")
        if not isinstance(item["key"], str) or not item["key"].endswith(".json"):
            raise RecoveryError("invalid manifest inventory key")
        if not isinstance(item["kind"], str) or item["kind"] not in {"asset", "album", "person", "face", "processing", "fingerprint", "burst", "tombstone"}:
            raise RecoveryError("invalid manifest inventory kind")
        if not isinstance(item["schemaVersion"], int) or not isinstance(item["revision"], (int, type(None))):
            raise RecoveryError("invalid manifest inventory metadata")
        if not isinstance(item["sha256"], str) or not SHA256_RE.fullmatch(item["sha256"]):
            raise RecoveryError("invalid manifest inventory checksum")
        if isinstance(item["sizeBytes"], bool) or not isinstance(item["sizeBytes"], int) or item["sizeBytes"] < 0:
            raise RecoveryError("invalid manifest inventory size")
    for item in payload["objects"]:
        if not isinstance(item, dict) or set(item) != {"key", "sha256", "sizeBytes"}:
            raise RecoveryError("invalid object inventory entry")
        if not isinstance(item["key"], str) or not isinstance(item["sha256"], str) or not SHA256_RE.fullmatch(item["sha256"]):
            raise RecoveryError("invalid object inventory entry")
        if isinstance(item["sizeBytes"], bool) or not isinstance(item["sizeBytes"], int) or item["sizeBytes"] < 0:
            raise RecoveryError("invalid object inventory size")
    dump = payload["postgresDump"]
    if dump is not None:
        if not isinstance(dump, dict) or set(dump) != {"key", "sha256", "sizeBytes", "format"}:
            raise RecoveryError("invalid PostgreSQL dump metadata")
        if not isinstance(dump["key"], str) or not SHA256_RE.fullmatch(dump["sha256"]):
            raise RecoveryError("invalid PostgreSQL dump metadata")
        if isinstance(dump["sizeBytes"], bool) or not isinstance(dump["sizeBytes"], int) or dump["sizeBytes"] <= 0:
            raise RecoveryError("invalid PostgreSQL dump size")
    return canonical_json(payload)


def decode_checkpoint(body: bytes | str) -> RecoveryCheckpoint:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise RecoveryError(f"duplicate checkpoint field: {key}")
            value[key] = item
        return value

    try:
        value = json.loads(body, object_pairs_hook=reject_duplicates)
    except (RecoveryError, ValueError, TypeError) as exc:
        raise RecoveryError("malformed checkpoint JSON") from exc
    encoded = encode_checkpoint(value)
    if isinstance(body, bytes) and encoded != body:
        raise RecoveryError("checkpoint is not canonical JSON")
    return RecoveryCheckpoint(
        checkpoint_id=value["checkpointId"],
        checkpoint_timestamp=value["checkpointTimestamp"],
        source_bucket=value["source"]["bucket"],
        source_endpoint=value["source"]["endpoint"],
        manifest_revisions=tuple(value["manifestRevisions"]),
        objects=tuple(value["objects"]),
        postgres_dump=value["postgresDump"],
        compatibility=value["compatibility"],
        schema_versions=tuple(value["schemaVersions"]),
        tool_version=value["toolVersion"],
        schema_version=value["schemaVersion"],
    )


def _dump_postgres(database_url: str, runner: Callable[..., bytes] | None = None) -> bytes:
    if runner:
        try:
            result = runner(database_url)
        except Exception as exc:
            raise RecoveryError(f"PostgreSQL dump failed: {exc}") from exc
        if not isinstance(result, bytes) or not result:
            raise RecoveryError("PostgreSQL dump runner returned no bytes")
        return result
    try:
        result = subprocess.run(
            ["pg_dump", "--dbname", database_url, "--format=custom", "--no-owner", "--no-acl"],
            check=True, capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RecoveryError(f"PostgreSQL dump failed: {exc}") from exc
    if not result.stdout:
        raise RecoveryError("PostgreSQL dump was empty")
    return result.stdout


def _inventory(source: Any, checkpoint_timestamp: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    manifests: list[dict[str, Any]] = []
    object_keys: set[str] = set()
    for prefix, kind in CANONICAL_PREFIXES:
        try:
            keys = sorted(source.keys(prefix))
        except Exception as exc:
            raise RecoveryError(f"source list failed: {prefix}: {exc}") from exc
        for key in keys:
            body, info = _read_verified(source, key)
            document = _decode_kind(key, body, kind)
            info.update({"kind": kind, "schemaVersion": document["schemaVersion"], "revision": document.get("revision")})
            manifests.append(info)
            for blob in document.get("blobs", ()):
                object_keys.add(blob["objectKey"])
            result = document.get("resultObject")
            if result:
                object_keys.add(result["objectKey"])
            for ref in document.get("processing", ()):
                object_keys.add(ref["artifactKey"])
    objects: list[dict[str, Any]] = []
    for key in sorted(object_keys):
        body, info = _read_verified(source, key)
        objects.append(info)
    return sorted(manifests, key=lambda x: x["key"]), objects


def create_recovery_checkpoint(
    source: Any,
    destination: Any,
    checkpoint_id: str,
    *,
    database_url: str | None = None,
    dump_runner: Callable[..., bytes] | None = None,
    checkpoint_prefix: str = "indexes/recovery-checkpoints",
    progress_prefix: str = "indexes/checkpoints",
    source_endpoint: str = "",
    resume: bool = True,
    stop_after: int | None = None,
    checkpoint_timestamp: str | None = None,
    compatibility: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create or resume one deterministic checkpoint and return its report."""
    final_key = f"{checkpoint_prefix.rstrip('/')}/{checkpoint_id}.json"
    progress_key = f"{progress_prefix.rstrip('/')}/recovery-{checkpoint_id}.json"
    try:
        if destination.head(final_key) is not None:
            checkpoint = decode_checkpoint(destination.read_bytes(final_key))
            return verify_recovery_checkpoint(destination, checkpoint=checkpoint, checkpoint_key=final_key)
        timestamp = checkpoint_timestamp
        if resume and destination.head(progress_key) is not None:
            progress = destination.get_json(progress_key)
            timestamp = progress.get("checkpointTimestamp")
        timestamp = _utc(timestamp)
        manifests, objects = _inventory(source, timestamp)
        if stop_after is not None:
            if stop_after < 0:
                raise RecoveryError("stop_after must be non-negative")
            if stop_after < len(manifests):
                progress = {"schemaVersion": 1, "checkpointId": checkpoint_id, "checkpointTimestamp": timestamp, "nextManifest": stop_after}
                destination.put_json_mutable(progress_key, progress)
                return {"status": "paused", "checkpoint": progress_key, "checkpointId": checkpoint_id, "scanned": stop_after}
        dump_info = None
        if database_url:
            dump = _dump_postgres(database_url, dump_runner)
            dump_key = f"{checkpoint_prefix.rstrip('/')}/{checkpoint_id}/postgres-derived-index.dump"
            _put_immutable(destination, dump_key, dump, "application/vnd.postgresql.dump")
            dump_info = {"key": dump_key, "sha256": _sha(dump), "sizeBytes": len(dump), "format": "postgres-custom"}
        for item in manifests + objects:
            body = source.read_bytes(item["key"])
            _put_immutable(destination, f"{checkpoint_id}/{item['key']}", body, "application/json" if item["key"].endswith(".json") else "application/octet-stream")
        checkpoint = RecoveryCheckpoint(
            checkpoint_id, timestamp, getattr(source, "bucket", ""), source_endpoint,
            tuple(manifests), tuple(objects), dump_info,
            _compatibility_metadata([item["schemaVersion"] for item in manifests], compatibility),
        )
        encoded = encode_checkpoint(checkpoint)
        _put_immutable(destination, final_key, encoded, "application/json")
        return verify_recovery_checkpoint(destination, checkpoint=checkpoint, checkpoint_key=final_key)
    except RecoveryError as exc:
        return {"status": "failed", "checkpointId": checkpoint_id, "checkpoint": final_key, "errors": [{"reason": str(exc)}]}


def verify_recovery_checkpoint(destination: Any, *, checkpoint: RecoveryCheckpoint | None = None, checkpoint_key: str | None = None) -> dict[str, Any]:
    try:
        if checkpoint is None:
            if not checkpoint_key:
                raise RecoveryError("checkpoint key is required")
            checkpoint = decode_checkpoint(destination.read_bytes(checkpoint_key))
        checked = 0
        for item in checkpoint.manifest_revisions + checkpoint.objects:
            key = f"{checkpoint.checkpoint_id}/{item['key']}"
            body, info = _read_verified(destination, key)
            if info["sha256"] != item["sha256"] or info["sizeBytes"] != item["sizeBytes"]:
                raise RecoveryError(f"checkpoint object mismatch: {item['key']}")
            checked += 1
        if checkpoint.postgres_dump:
            _body, info = _read_verified(destination, checkpoint.postgres_dump["key"])
            if info["sha256"] != checkpoint.postgres_dump["sha256"] or info["sizeBytes"] != checkpoint.postgres_dump["sizeBytes"]:
                raise RecoveryError("PostgreSQL dump checksum or size mismatch")
        return {"status": "complete", "checkpointId": checkpoint.checkpoint_id, "checked": checked, "checkpoint": checkpoint_key}
    except (RecoveryError, KeyError, TypeError) as exc:
        return {"status": "failed", "errors": [{"reason": str(exc)}]}


def restore_recovery_checkpoint(checkpoint_source: Any, target: Any, checkpoint_key: str, *, checkpoint_id: str | None = None) -> dict[str, Any]:
    """Restore canonical records into a fresh destination; source is read-only."""
    created_keys: list[str] = []
    try:
        checkpoint = decode_checkpoint(checkpoint_source.read_bytes(checkpoint_key))
        if checkpoint_id and checkpoint_id != checkpoint.checkpoint_id:
            raise RecoveryError("checkpoint id does not match metadata")
        verify = verify_recovery_checkpoint(checkpoint_source, checkpoint=checkpoint, checkpoint_key=checkpoint_key)
        if verify["status"] != "complete":
            return verify
        copied = 0
        for item in checkpoint.manifest_revisions + checkpoint.objects:
            body = checkpoint_source.read_bytes(f"{checkpoint.checkpoint_id}/{item['key']}")
            absent = target.head(item["key"]) is None
            _put_immutable(target, item["key"], body, "application/json" if item["key"].endswith(".json") else "application/octet-stream")
            if absent:
                created_keys.append(item["key"])
            copied += 1
        return {"status": "complete", "checkpointId": checkpoint.checkpoint_id, "copied": copied}
    except (RecoveryError, KeyError, TypeError) as exc:
        for key in created_keys:
            try:
                target.delete(key)
            except Exception:
                pass
        return {"status": "failed", "errors": [{"reason": str(exc)}]}


def restore_postgres_dump(
    checkpoint_source: Any,
    checkpoint_key: str,
    restore_runner: Callable[[bytes], Any],
) -> dict[str, Any]:
    """Verify and restore the optional derived-index dump through a caller-owned runner."""
    try:
        checkpoint = decode_checkpoint(checkpoint_source.read_bytes(checkpoint_key))
        if checkpoint.postgres_dump is None:
            raise RecoveryError("checkpoint does not contain a PostgreSQL dump")
        body, info = _read_verified(checkpoint_source, checkpoint.postgres_dump["key"])
        if info["sha256"] != checkpoint.postgres_dump["sha256"] or info["sizeBytes"] != checkpoint.postgres_dump["sizeBytes"]:
            raise RecoveryError("PostgreSQL dump checksum or size mismatch")
        restore_runner(body)
        return {"status": "complete", "checkpointId": checkpoint.checkpoint_id, "restoredBytes": len(body)}
    except (RecoveryError, KeyError, TypeError, OSError, ValueError) as exc:
        return {"status": "failed", "errors": [{"reason": str(exc)}]}
