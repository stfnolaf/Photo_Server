"""S3-first AI publication: content-addressed results and immutable records."""

import dataclasses
import hashlib
from uuid import UUID, uuid5

import pytest

from photo_server.ai_publication import (
    build_processing_artifact,
    canonical_payload,
    derive_run_id,
    publish_ai_artifact,
)
from photo_server.canonical import CanonicalIntegrityError, CanonicalPublisher
from photo_server.manifests import decode_processing_artifact, encode

LIBRARY_ID = "2d20e22e-8d55-4355-8433-82689417b31d"
ASSET_ID = "7d1c3f9e-4b21-4a55-9c08-3f6e1a2b4c5d"
INPUT_SHA = "a" * 64
PIPELINE_VERSION = "photo-ai-v1"


class FakeStorage:
    def __init__(self):
        self.objects = {}

    def head(self, key):
        body = self.objects.get(key)
        return None if body is None else {"ContentLength": len(body)}

    def put(self, key, body, _mime):
        if key not in self.objects:
            self.objects[key] = body.read() if hasattr(body, "read") else body
        return key not in self.objects

    def read_bytes(self, key, limit=None):
        value = self.objects[key]
        if limit is not None and len(value) > limit:
            raise AssertionError("limit")
        return value

    def verify(self, key, size, digest, full=True):
        value = self.objects[key]
        if len(value) != size or hashlib.sha256(value).hexdigest() != digest:
            from photo_server.config import LibraryError

            raise LibraryError("verification failed")

    def keys(self, prefix):
        return [key for key in self.objects if key.startswith(prefix)]


def make_payload(**overrides):
    payload = {
        "schemaVersion": 1,
        "libraryId": LIBRARY_ID,
        "assetId": ASSET_ID,
        "analysisType": "photo-ai",
        "inputSha256": INPUT_SHA,
        "pipelineVersion": PIPELINE_VERSION,
        "models": {
            "semantic": {"name": "qwen3-vl", "digest": "abc123def456"},
            "faceDetector": "yunet-2023mar",
            "faceEmbedding": {
                "modelId": "antelopev2",
                "task": "face-recognition",
                "network": "arcface",
                "inputSize": "112x112",
                "outputDim": 512,
                "normalization": "rgb",
                "runtime": "stub",
            },
        },
        "semantic": {"labels": ["dog"], "summary": "A dog."},
        "faces": [
            {
                "box": [10.0, 20.0, 90.0, 100.0],
                "confidence": 0.9,
                "embedding": [0.1, 0.2],
            }
        ],
        "metrics": {"elapsedMs": 12.3},
        "semanticOrigin": "computed",
    }
    payload.update(overrides)
    return payload


def test_derive_run_id_is_deterministic_and_content_sensitive():
    base = {
        "library_id": LIBRARY_ID,
        "analysis_type": "photo-ai",
        "asset_id": ASSET_ID,
        "input_sha256": INPUT_SHA,
        "pipeline_version": PIPELINE_VERSION,
        "result_sha256": "b" * 64,
    }
    assert derive_run_id(**base) == derive_run_id(**base)
    assert isinstance(derive_run_id(**base), UUID)
    assert derive_run_id(**{**base, "result_sha256": "c" * 64}) != derive_run_id(**base)
    assert derive_run_id(**{**base, "pipeline_version": "photo-ai-v2"}) != derive_run_id(**base)
    assert derive_run_id(**{**base, "asset_id": "9" * 32 + "9" * 13}) != derive_run_id(
        **base
    )
    assert derive_run_id(**{**base, "analysis_type": "fingerprint"}) != derive_run_id(**base)
    # Same content through a different payload spelling yields the same id.
    assert canonical_payload({"runId": "x", "createdAt": "y", "a": 1}) == {"a": 1}


def test_build_processing_artifact_is_byte_stable_and_codec_valid():
    record_a, bytes_a, run_a = build_processing_artifact(
        LIBRARY_ID, make_payload(), created_at="2026-01-01T00:00:00+00:00"
    )
    record_b, bytes_b, run_b = build_processing_artifact(
        LIBRARY_ID, make_payload(), created_at="2026-01-01T00:00:00+00:00"
    )
    assert run_a == run_b
    assert bytes_a == bytes_b
    assert len(bytes_a) > 0
    # A later wall clock does not change the content-addressed identity.
    record_c, bytes_c, run_c = build_processing_artifact(
        LIBRARY_ID, make_payload(), created_at="2026-02-01T00:00:00+00:00"
    )
    assert run_c == run_a and bytes_c == bytes_a
    sha = hashlib.sha256(bytes_a).hexdigest()
    assert record_a.result_object.object_key == f"objects/{sha}"
    assert record_a.artifact_id == UUID(run_a)
    assert record_a.created_at == "2026-01-01T00:00:00Z"
    assert record_a.model_name is None and record_a.model_version is None
    # Codec round-trip validates the derived record.
    assert decode_processing_artifact(encode(record_a)) == record_a
    with pytest.raises(ValueError):
        build_processing_artifact(
            LIBRARY_ID,
            make_payload(assetId="not-a-uuid"),
            created_at="2026-01-01T00:00:00+00:00",
        )


def test_publish_is_idempotent_and_byte_stable():
    storage = FakeStorage()
    publisher = CanonicalPublisher(storage)
    payload = make_payload()
    record_a, bytes_a, run_a = build_processing_artifact(
        LIBRARY_ID, payload, created_at="2026-01-01T00:00:00+00:00"
    )
    object_key, record_key, adopted = publish_ai_artifact(storage, publisher, record_a, bytes_a)
    assert adopted is False
    assert object_key == f"objects/{record_a.result_object.sha256}"
    assert storage.keys("") == [object_key, record_key]  # exactly two keys
    first_record_bytes = storage.read_bytes(record_key)
    first_object_bytes = storage.read_bytes(object_key)

    # Retry: same payload, later wall clock. The record key already exists
    # with the same logical content, so it is adopted without a rewrite.
    record_b, bytes_b, run_b = build_processing_artifact(
        LIBRARY_ID, payload, created_at="2026-02-01T00:00:00+00:00"
    )
    assert run_b == run_a
    object_key2, record_key2, adopted2 = publish_ai_artifact(storage, publisher, record_b, bytes_b)
    assert adopted2 is True
    assert (object_key2, record_key2) == (object_key, record_key)
    assert storage.read_bytes(record_key) == first_record_bytes
    assert storage.read_bytes(object_key) == first_object_bytes
    stored = decode_processing_artifact(first_record_bytes)
    assert stored.created_at == "2026-01-01T00:00:00Z"  # first publication stands
    assert storage.keys("") == [object_key, record_key]  # no extra keys


def test_publish_conflicts_on_divergent_record_or_object_bytes():
    payload = make_payload()
    record, payload_bytes, run_id = build_processing_artifact(
        LIBRARY_ID, payload, created_at="2026-01-01T00:00:00+00:00"
    )
    record_key = f"manifests/processing/{run_id}.json"

    # A record under the same logical key with a different result digest.
    other_payload = make_payload(semantic={"labels": ["cat"], "summary": "A cat."})
    other, other_bytes, _ = build_processing_artifact(
        LIBRARY_ID, other_payload, created_at="2026-01-01T00:00:00+00:00"
    )
    assert other.result_object.sha256 != record.result_object.sha256
    divergent = dataclasses.replace(
        other,
        artifact_id=record.artifact_id,
        result_object=dataclasses.replace(
            other.result_object,
            object_key=f"objects/{other.result_object.sha256}",
        ),
    )
    storage = FakeStorage()
    publisher = CanonicalPublisher(storage)
    storage.put(record_key, encode(divergent), "application/json")
    with pytest.raises(CanonicalIntegrityError):
        publish_ai_artifact(storage, publisher, record, payload_bytes)

    # Undecodable bytes at the record key.
    storage = FakeStorage()
    publisher = CanonicalPublisher(storage)
    storage.put(record_key, b"not-json", "application/json")
    with pytest.raises(CanonicalIntegrityError):
        publish_ai_artifact(storage, publisher, record, payload_bytes)

    # A pre-existing object with different bytes.
    storage = FakeStorage()
    publisher = CanonicalPublisher(storage)
    storage.put(record.result_object.object_key, b"corrupt", "application/json")
    with pytest.raises(CanonicalIntegrityError):
        publish_ai_artifact(storage, publisher, record, payload_bytes)


def test_publish_uses_model_metadata_when_given():
    storage = FakeStorage()
    publisher = CanonicalPublisher(storage)
    record, payload_bytes, _ = build_processing_artifact(
        LIBRARY_ID,
        make_payload(),
        created_at="2026-01-01T00:00:00+00:00",
        model_name="qwen3-vl",
        model_version="abc123def456",
    )
    _object_key, record_key, adopted = publish_ai_artifact(storage, publisher, record, payload_bytes)
    assert adopted is False
    stored = decode_processing_artifact(storage.read_bytes(record_key))
    assert stored.model_name == "qwen3-vl"
    assert stored.model_version == "abc123def456"
