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
import math
from uuid import UUID, uuid5

from photo_server.canonical import CanonicalIntegrityError, CanonicalPublisher
from photo_server.config import LibraryError
from photo_server.manifests import (
    BlobReference,
    FaceManifest,
    ManifestCodecError,
    ProcessingArtifact,
    canonical_json,
    decode_face_manifest,
    decode_person_manifest,
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
    if library_id is None:
        raise LibraryError("Canonical library identity is missing")
    base = library_id if isinstance(library_id, UUID) else UUID(library_id)
    name = (
        f"processing:{analysis_type}:{asset_id}:{input_sha256}:"
        f"{pipeline_version}:{result_sha256}"
    )
    return uuid5(base, name)


def derive_face_id(
    library_id: UUID | str,
    asset_id: str,
    run_id: UUID | str,
    face_index: int,
) -> UUID:
    """Deterministic face record id for one detected face of one run.

    Namespaced by the library id, mirroring the run id convention; the
    index is the face's position in the run's face list, so a retry
    re-derives the same id for the same detection.
    """
    if library_id is None:
        raise LibraryError("Canonical library identity is missing")
    base = library_id if isinstance(library_id, UUID) else UUID(library_id)
    return uuid5(base, f"face:{asset_id}:{run_id}:{face_index}")


def derive_person_id(library_id: UUID | str, run_id: UUID | str, face_index: int) -> UUID:
    """Deterministic person id opened by the face at ``face_index``."""
    if library_id is None:
        raise LibraryError("Canonical library identity is missing")
    base = library_id if isinstance(library_id, UUID) else UUID(library_id)
    return uuid5(base, f"person:{run_id}:{face_index}")


def result_sha(payload: dict) -> str:
    """SHA-256 of the canonical payload: the result's content address."""
    return hashlib.sha256(canonical_json(canonical_payload(payload))).hexdigest()


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
    data = canonical_json(canonical_payload(payload))
    digest = result_sha(payload)
    run_id = derive_run_id(
        library_id,
        payload["analysisType"],
        payload["assetId"],
        payload["inputSha256"],
        payload["pipelineVersion"],
        digest,
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
            object_key=f"objects/{digest}",
            original_filename="processing-artifact",
            sha256=digest,
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
        except ManifestCodecError as err:
            raise CanonicalIntegrityError(f"Manifest byte verification failed: {record_key}") from err
        if (
            stored.artifact_id != record.artifact_id
            or stored.result_object.sha256 != record.result_object.sha256
        ):
            raise CanonicalIntegrityError(f"Processing artifact record conflict: {record_key}")
        return object_key, record_key, True
    publisher.publish_record(record_key, record, "processing-artifact")
    return object_key, record_key, False


def unit(vector: list[float]) -> list[float]:
    """Unit-normalize an embedding; a zero vector stays zero."""
    length = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / length for value in vector]


def build_face_record(
    library_id: UUID | str,
    face_id: str,
    asset_id: str,
    run_id: UUID | str,
    face_index: int,
    face: dict,
    person_id: str,
) -> tuple[FaceManifest, bytes]:
    """Build the canonical face record for one detected face.

    The record carries no per-publication timestamps, so identical
    detection input produces identical bytes; a retry adopts the stored
    record as a byte-stable no-op. ``face`` is the worker's detection
    shape: a four-element ``box`` list and an ``embedding`` list.
    """
    record = FaceManifest(
        library_id=UUID(library_id) if isinstance(library_id, str) else library_id,
        face_id=UUID(face_id),
        asset_id=UUID(asset_id),
        analysis_run_id=UUID(run_id) if isinstance(run_id, str) else run_id,
        person_id=UUID(person_id),
        face_index=face_index,
        bounding_box={
            "x": face["box"][0],
            "y": face["box"][1],
            "width": face["box"][2],
            "height": face["box"][3],
        },
        confidence=float(face["confidence"]),
        embedding=[float(value) for value in face["embedding"]],
    )
    data = encode(record)
    try:
        decoded = decode_face_manifest(data)
    except ManifestCodecError as error:
        raise ValueError(f"derived face record is not codec-valid: {error}") from error
    if decoded != record:
        raise ValueError("face record codec round-trip diverged")
    return record, data


def cluster_faces(
    faces: list[tuple[int, dict]],
    person_sums: dict[str, list[float]],
    used: set[str],
    threshold: float,
    new_person_id,
) -> list[tuple[int, str]]:
    """Cluster pending faces against per-person embedding sums.

    Exact replica of the retired in-transaction clustering: each face is
    scored by dot product against the unit-normalized running sum of
    every person not already used in this run; a match requires a score
    strictly above ``threshold``, and a person that receives one face
    cannot receive another. Returns ``(face_index, person_id)`` pairs in
    input order; ``new_person_id(index)`` is consulted for a face that
    matches nothing. Matched persons are added to ``used`` and each
    face's unit vector is folded into its person's sum in place.
    """
    assignments: list[tuple[int, str]] = []
    for index, face in faces:
        vector = unit(face["embedding"])
        best_person: str | None = None
        best_score = -1.0
        for person_id, total in person_sums.items():
            if person_id in used:
                continue
            center = unit(total)
            score = sum(a * b for a, b in zip(center, vector, strict=True))
            if score > best_score:
                best_person, best_score = person_id, score
        if best_person is None or best_score <= threshold:
            best_person = new_person_id(index)
            person_sums[best_person] = [0.0] * len(vector)
        person_sums[best_person] = [
            a + b for a, b in zip(person_sums[best_person], vector, strict=True)
        ]
        used.add(best_person)
        assignments.append((index, best_person))
    return assignments


def s3_face_state(storage) -> tuple[list[dict], list[dict]]:
    """Rebuild clustering state from the canonical plane alone.

    Fallback for a projection that has neither persons nor face
    centroids (for example right after a wiped database): per-person
    sums of unit embeddings come from the face records of each asset's
    newest run — a face claimed by a person head is attributed to that
    head, since move/merge operations update heads but not records — and
    the person snapshot comes from each person's newest non-tombstone
    revision, synthesized for persons whose face records reference them
    but which never got a head.
    """
    runs: dict[str, str] = {}
    newest: dict[str, str] = {}
    for key in storage.keys("manifests/processing/"):
        try:
            record = decode_processing_artifact(storage.read_bytes(key))
        except Exception:
            continue
        if record.processing_type != "photo-ai":
            continue
        created_at = record.created_at or ""
        runs[record.artifact_id] = created_at
        current = newest.get(record.asset_id)
        if current is None or (created_at, record.artifact_id) > (
            runs.get(current, ""),
            current,
        ):
            newest[record.asset_id] = record.artifact_id

    heads: dict[str, object] = {}
    for key in storage.keys("manifests/people/"):
        try:
            head = decode_person_manifest(storage.read_bytes(key))
        except Exception:
            continue
        if head.deleted_at is not None:
            continue
        person_id = str(head.person_id)
        current = heads.get(person_id)
        if current is None or head.revision > current.revision:
            heads[person_id] = head

    claimed: dict[str, str] = {}
    for person_id in sorted(heads):
        for face_id in heads[person_id].face_ids:
            claimed.setdefault(str(face_id), person_id)

    sums: dict[str, list[float]] = {}
    headless_faces: dict[str, set[str]] = {}
    headless_created: dict[str, str] = {}
    for key in storage.keys("manifests/faces/"):
        try:
            face = decode_face_manifest(storage.read_bytes(key))
        except Exception:
            continue
        if newest.get(face.asset_id) != face.analysis_run_id:
            continue
        face_id = str(face.face_id)
        person_id = claimed.get(face_id, str(face.person_id))
        if person_id not in heads:
            headless_faces.setdefault(person_id, set()).add(face_id)
            run_created = runs.get(face.analysis_run_id, "")
            if run_created and (
                person_id not in headless_created
                or run_created < headless_created[person_id]
            ):
                headless_created[person_id] = run_created
        vector = unit(face.embedding)
        total = sums.get(person_id)
        sums[person_id] = (
            [a + b for a, b in zip([0.0] * len(vector), vector, strict=True)]
            if total is None
            else [a + b for a, b in zip(total, vector, strict=True)]
        )

    people = [
        {
            "personId": person_id,
            "displayName": head.display_name,
            "createdAt": head.created_at,
            "faceIds": [str(face_id) for face_id in sorted(head.face_ids)],
        }
        for person_id, head in sorted(heads.items())
    ]
    for person_id in sorted(headless_faces):
        people.append(
            {
                "personId": person_id,
                "displayName": "",
                "createdAt": headless_created.get(person_id, ""),
                "faceIds": sorted(headless_faces[person_id]),
            }
        )
    centroids = [
        {"personId": person_id, "sum": sums[person_id]} for person_id in sorted(sums)
    ]
    return centroids, people
