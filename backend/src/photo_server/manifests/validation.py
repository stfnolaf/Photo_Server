"""Shared strict scalar and ancestry validation for manifest models."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

SCHEMA_VERSION = 1
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
UTC_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
ASSET_FIELDS = {
    "schemaVersion",
    "libraryId",
    "assetId",
    "revision",
    "parentRevision",
    "operationId",
    "createdAt",
    "importedAt",
    "captureTime",
    "blobs",
    "primaryBlobId",
    "extractedMetadata",
    "userState",
    "deletedAt",
    "processing",
}
ALBUM_FIELDS = {
    "schemaVersion",
    "libraryId",
    "albumId",
    "revision",
    "parentRevision",
    "operationId",
    "createdAt",
    "name",
    "description",
    "assetIds",
    "deletedAt",
}
PERSON_FIELDS = {
    "schemaVersion",
    "libraryId",
    "personId",
    "revision",
    "parentRevision",
    "operationId",
    "createdAt",
    "displayName",
    "faceIds",
    "deletedAt",
}
FACE_FIELDS = {
    "schemaVersion",
    "libraryId",
    "faceId",
    "assetId",
    "analysisRunId",
    "personId",
    "faceIndex",
    "boundingBox",
    "confidence",
    "embedding",
}
FINGERPRINT_FIELDS = {
    "schemaVersion",
    "libraryId",
    "assetId",
    "algorithmVersion",
    "pHash",
    "dHash",
    "width",
    "height",
    "chromaHistogram",
    "createdAt",
}
BURST_FIELDS = {
    "schemaVersion",
    "libraryId",
    "revision",
    "parentRevision",
    "operationId",
    "createdAt",
    "policyVersion",
    "operation",
    "clusters",
    "excludedAssetIds",
}
BURST_OPERATION_FIELDS = {"action", "clusterId", "assetId"}
BURST_CLUSTER_FIELDS = {
    "clusterId",
    "representativeAssetId",
    "representativeSelected",
    "assetIds",
}
TOMBSTONE_FIELDS = {
    "schemaVersion",
    "libraryId",
    "entityType",
    "entityId",
    "revision",
    "parentRevision",
    "operationId",
    "deletedAt",
    "createdAt",
}
ARTIFACT_FIELDS = {
    "schemaVersion",
    "assetId",
    "artifactId",
    "processingType",
    "modelName",
    "modelVersion",
    "pipelineVersion",
    "inputSha256",
    "implementationVersion",
    "createdAt",
    "sourceObjectKey",
    "resultObject",
}
BLOB_FIELDS = {"blobId", "role", "objectKey", "originalFilename", "sha256", "sizeBytes", "mimeType"}
PROCESSING_FIELDS = {
    "artifactKey",
    "artifactSha256",
    "inputSha256",
    "processingType",
    "implementationVersion",
}
USER_STATE_FIELDS = {"rating", "favorite", "caption", "keywords", "location"}


def check_fields(value: Any, allowed: set[str], name: str) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"{name} has unknown fields: {sorted(unknown)}")


def require_str(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def require_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def check_uuid(value: Any, name: str) -> UUID:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a UUID string")
    try:
        result = UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"{name} is not a valid UUID") from exc
    if str(result) != value:
        raise ValueError(f"{name} must use canonical lowercase UUID form")
    return result


def check_sha(value: Any, name: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def check_timestamp(value: Any, name: str) -> str:
    if not isinstance(value, str) or not UTC_TIMESTAMP_RE.fullmatch(value):
        raise ValueError(f"{name} must be an RFC 3339 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError(f"{name} is not a valid timestamp") from exc
    if parsed.tzinfo != timezone.utc:
        raise ValueError(f"{name} must be UTC")
    return value


def check_common(value: dict[str, Any], name: str) -> None:
    if value.get("schemaVersion") != SCHEMA_VERSION:
        raise ValueError(f"{name} has unsupported schemaVersion")
    revision = require_int(value.get("revision"), "revision")
    parent = value.get("parentRevision")
    if revision == 1:
        if parent is not None:
            raise ValueError("revision 1 must not have a parentRevision")
    elif parent != revision - 1:
        raise ValueError("revision parent must be exactly revision - 1")


def validate_revision(
    record: Any, *, expected_parent: int | None = None, current_revision: int | None = None
) -> None:
    """Validate one record against an optional optimistic-concurrency boundary."""
    revision = require_int(getattr(record, "revision", None), "revision")
    parent = getattr(record, "parent_revision", None)
    if revision == 1 and parent is not None:
        raise ValueError("revision 1 must not have a parent")
    if revision > 1 and parent != revision - 1:
        raise ValueError("invalid revision ancestry")
    if expected_parent is not None and parent != expected_parent:
        raise ValueError("stale revision parent")
    if current_revision is not None and revision <= current_revision:
        raise ValueError("stale revision")


def validate_history(records: list[Any] | tuple[Any, ...]) -> Any:
    """Validate a complete linear entity history and return its latest record."""
    if not records:
        raise ValueError("history must not be empty")
    ordered = sorted(records, key=lambda x: x.revision)
    revisions = [x.revision for x in ordered]
    if revisions != list(range(1, len(records) + 1)):
        raise ValueError("history has gaps or duplicate revisions")
    for index, record in enumerate(ordered):
        validate_revision(record)
        if index and record.parent_revision != ordered[index - 1].revision:
            raise ValueError("invalid ancestry")
    if sum(1 for x in records if x.revision == ordered[-1].revision) != 1:
        raise ValueError("multiple heads")
    return ordered[-1]
