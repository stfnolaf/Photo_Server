"""S3-first publication of AI analysis results (content-addressed).

The analysis result is the canonical artifact of the AI stage: it is
normalized to canonical JSON bytes, stored exactly once under
``objects/{sha256}``, and referenced from an immutable
``ProcessingArtifact`` record under ``manifests/processing/``.

The logical run identity is deterministic — the uuid5 of the library,
asset, input hash, pipeline version, and result digest — so rerunning
the same analysis yields the same run id, the same object key, and a
byte-stable record (a retry adopts the stored record as a no-op). The
data-plane ``analysis/`` copy is kept for the migration period; the
canonical plane is authoritative.
"""

from __future__ import annotations

import hashlib
from uuid import UUID, uuid5

from photo_server.canonical import CanonicalIntegrityError, CanonicalPublisher
from photo_server.manifests import (
    BlobReference,
    ManifestCodecError,
    ProcessingArtifact,
    canonical_json,
    decode_processing_artifact,
    encode,
)

# Version of the result payload schema this code produces.
IMPLEMENTATION_VERSION = "v1"
RESULT_MIME_TYPE = "application/json"


def _utc(value: str) -> str:
    return value[:-6] + "Z" if value.endswith("+00:00") else value


def canonical_payload(artifact: dict) -> dict:
    """The result payload without its per-publication identity fields.

    ``runId`` is derived from the payload's content (below) and
    ``createdAt`` is the first-publication wall clock, so neither
    participates in content addressing.
    """
    payload = dict(artifact)
    payload.pop("runId", None)
    payload.pop("createdAt", None)
    return payload


def derive_run_id(
    library_id: UUID | str,
    analysis_type: str,
    asset_id: str,
    input_sha256: str,
    pipeline_version: str,
    result_sha256: str,
) -> UUID:
    """Deterministic logical run id for a content-addressed result."""
    base = library_id if isinstance(library_id, UUID) else UUID(library_id)
    name = (
        f"processing:{analysis_type}:{asset_id}:{input_sha256}:"
        f"{pipeline_version}:{result_sha256}"
    )
    return uuid5(base, name)


def build_processing_artifact(
    library_id: UUID | str,
    payload: dict,
    *,
    created_at: str,
    model_name: str | None = None,
    model_version: str | None = None,
    source_object_key: str | None = None,
) -> tuple[ProcessingArtifact, bytes, str]:
    """Validate a result payload and derive its canonical record.

    Returns ``(record, payload_bytes, run_id)`` where the run id is
    deterministic and also the record's ``artifactId``. The record's
    ``createdAt`` is the first-publication wall clock (Z-normalized); a
    later retry that reuses the stored record keeps that original value.
    """
    data = canonical_json(payload)
    result_sha = hashlib.sha256(data).hexdigest()
    run_id = derive_run_id(
        library_id,
        payload["analysisType"],
        payload["assetId"],
        payload["inputSha256"],
        payload["pipelineVersion"],
        result_sha,
    )
    record = ProcessingArtifact(
        asset_id=UUID(payload["assetId"]),
        artifact_id=run_id,
        processing_type=payload["analysisType"],
        pipeline_version=payload["pipelineVersion"],
        input_sha256=payload["inputSha256"],
        implementation_version=IMPLEMENTATION_VERSION,
        result_object=BlobReference(
            blob_id=UUID(int=0),
            role="SIDECAR",
            object_key=f"objects/{result_sha}",
            original_filename="processing-artifact",
            sha256=result_sha,
            size_bytes=len(data),
            mime_type=RESULT_MIME_TYPE,
        ),
        created_at=_utc(created_at),
        model_name=model_name,
        model_version=model_version,
        source_object_key=source_object_key,
    )
    # Round-trip codec validation before anything is written.
    try:
        decode_processing_artifact(encode(record))
    except ManifestCodecError as error:
        raise ValueError(f"derived processing record is not codec-valid: {error}") from error
    return record, data, str(run_id)


def publish_ai_artifact(
    storage,
    publisher: CanonicalPublisher,
    record: ProcessingArtifact,
    payload: bytes,
) -> tuple[str, str, bool]:
    """Publish the content-addressed result object and its record.

    Returns ``(object_key, record_key, adopted)``. When a record already
    exists at the record key it must encode the same logical content
    (same result digest); the stored bytes are adopted as-is, so a retry
    is a byte-stable no-op. Any other content is a hard integrity error.
    """
    object_key = record.result_object.object_key
    publisher.put_immutable(object_key, payload, RESULT_MIME_TYPE)
    record_key = f"manifests/processing/{record.artifact_id}.json"
    existing = storage.head(record_key)
    if existing is not None:
        stored_bytes = storage.read_bytes(record_key)
        try:
            stored = decode_processing_artifact(stored_bytes)
        except ManifestCodecError:
            raise CanonicalIntegrityError(f"Manifest byte verification failed: {record_key}")
        if (
            stored.artifact_id != record.artifact_id
            or stored.result_object.sha256 != record.result_object.sha256
        ):
            raise CanonicalIntegrityError(f"Processing artifact record conflict: {record_key}")
        return object_key, record_key, True
    publisher.publish_record(record_key, record, "processing-artifact")
    return object_key, record_key, False
