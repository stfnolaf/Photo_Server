"""S3-first AI publication: content-addressed results and immutable records."""

import dataclasses
import hashlib
from uuid import UUID, uuid4, uuid5

import pytest

from photo_server.ai_publication import (
    build_face_record,
    build_processing_artifact,
    canonical_payload,
    cluster_faces,
    derive_face_id,
    derive_person_id,
    derive_run_id,
    publish_ai_artifact,
    result_sha,
    s3_face_state,
    unit,
)
from photo_server.canonical import CanonicalIntegrityError, CanonicalPublisher
from photo_server.manifests import (
    PersonManifest,
    decode_face_manifest,
    decode_processing_artifact,
    encode,
)

LIBRARY_ID = "2d20e22e-8d55-4355-8433-82689417b31d"
ASSET_ID = "7d1c3f9e-4b21-4a55-9c08-3f6e1a2b4c5d"
INPUT_SHA = "a" * 64
PIPELINE_VERSION = "photo-ai-v1"
RUN_ID = str(
    derive_run_id(LIBRARY_ID, "photo-ai", ASSET_ID, INPUT_SHA, PIPELINE_VERSION, "b" * 64)
)
PERSON_A = str(uuid5(UUID(LIBRARY_ID), "fixture:person-a"))
PERSON_B = str(uuid5(UUID(LIBRARY_ID), "fixture:person-b"))
PERSON_OLD = str(uuid5(UUID(LIBRARY_ID), "fixture:person-old"))
PERSON_LEGACY = str(uuid5(UUID(LIBRARY_ID), "fixture:person-legacy"))


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


def test_unit_normalizes_and_preserves_zero():
    assert unit([3.0, 4.0]) == [0.6, 0.8]
    assert unit([0.0, 0.0]) == [0.0, 0.0]


def test_result_sha_and_derived_ids_are_deterministic_and_disjoint():
    payload = make_payload()
    reordered = {
        "pipelineVersion": payload["pipelineVersion"],
        "semanticOrigin": payload["semanticOrigin"],
        "models": payload["models"],
        "assetId": payload["assetId"],
        "schemaVersion": payload["schemaVersion"],
        "inputSha256": payload["inputSha256"],
        "analysisType": payload["analysisType"],
        "libraryId": payload["libraryId"],
        "metrics": payload["metrics"],
        "faces": payload["faces"],
        "semantic": payload["semantic"],
    }
    assert result_sha(payload) == result_sha(reordered)
    # Per-publication fields never participate in the content address.
    with_identity = dict(payload)
    with_identity["runId"] = str(uuid4())
    with_identity["createdAt"] = "2026-01-01T00:00:00Z"
    assert result_sha(payload) == result_sha(with_identity)

    run_id = derive_run_id(
        LIBRARY_ID, "photo-ai", ASSET_ID, INPUT_SHA, PIPELINE_VERSION, result_sha(payload)
    )
    face_id = derive_face_id(LIBRARY_ID, ASSET_ID, run_id, 0)
    person_id = derive_person_id(LIBRARY_ID, run_id, 0)
    assert len({run_id, face_id, person_id}) == 3
    # Face ids are disjoint from the face-operation target namespace.
    assert face_id != uuid5(UUID(LIBRARY_ID), f"face-operation:{run_id}")
    # str and UUID spellings agree.
    assert derive_face_id(LIBRARY_ID, ASSET_ID, str(run_id), 0) == face_id
    assert derive_person_id(str(LIBRARY_ID), run_id, 0) == person_id


def test_face_record_is_byte_stable_and_codec_valid():
    face = {"box": [1.0, 2.0, 3.0, 4.0], "confidence": 0.91, "embedding": [0.5, -0.5, 0.5]}
    face_id = str(derive_face_id(LIBRARY_ID, ASSET_ID, RUN_ID, 0))
    person_id = str(derive_person_id(LIBRARY_ID, RUN_ID, 0))
    record_a, bytes_a = build_face_record(LIBRARY_ID, face_id, ASSET_ID, RUN_ID, 0, face, person_id)
    record_b, bytes_b = build_face_record(
        UUID(LIBRARY_ID), face_id, ASSET_ID, UUID(RUN_ID), 0, face, person_id
    )
    assert record_a == record_b and bytes_a == bytes_b
    decoded = decode_face_manifest(bytes_a)
    assert decoded == record_a
    assert decoded.bounding_box == {"x": 1.0, "y": 2.0, "width": 3.0, "height": 4.0}
    assert decoded.face_index == 0
    assert decoded.embedding == [0.5, -0.5, 0.5]
    # The person attribution is part of the record's identity.
    _other, other_bytes = build_face_record(LIBRARY_ID, face_id, ASSET_ID, RUN_ID, 0, face, PERSON_B)
    assert other_bytes != bytes_a


def test_cluster_faces_matches_boundary_and_one_face_per_person():
    made = []

    def new_person_id(index):
        made.append(index)
        return f"person-{index}"

    sums = {"A": [10.0, 0.0]}
    used: set[str] = set()
    # Face 0 scores 1.0 against A: match. Face 1 scores 0.0: new person.
    # Face 2 scores 1.0 against A, but A already received a face this run:
    # it cannot receive another, so face 2 opens its own person.
    assignments = cluster_faces(
        [
            (0, {"embedding": [1.0, 0.0]}),
            (1, {"embedding": [0.0, 1.0]}),
            (2, {"embedding": [1.0, 0.0]}),
        ],
        sums,
        used,
        0.4,
        new_person_id,
    )
    assert assignments == [(0, "A"), (1, "person-1"), (2, "person-2")]
    assert used == {"A", "person-1", "person-2"}
    assert sums["A"] == [11.0, 0.0]  # the match is folded into the sum in place
    assert sums["person-1"] == [0.0, 1.0]
    assert sums["person-2"] == [1.0, 0.0]
    assert made == [1, 2]  # the factory is keyed by the original face index

    # Boundary: a score exactly equal to the threshold does not match.
    sums2 = {"A": [1.0, 0.0]}
    assignments2 = cluster_faces([(0, {"embedding": [1.0, 0.0]})], sums2, set(), 1.0, new_person_id)
    assert assignments2 == [(0, "person-0")]
    assert sums2["person-0"] == [1.0, 0.0]
    assert sums2["A"] == [1.0, 0.0]  # the unmatched person's sum is untouched


def test_cluster_retry_after_partial_adoption_reproduces_assignment():
    def new_person_id(index):
        return f"person-{index}"

    faces = [
        (0, {"embedding": [1.0, 0.0]}),
        (1, {"embedding": [0.0, 1.0]}),
        (2, {"embedding": [1.0, 0.0]}),
    ]
    # Original pass: face 0 matches A (the tie with B resolves to A, the
    # earlier dict entry); face 1 matches neither and opens person-1;
    # face 2 matches B because A is already used this run.
    first = cluster_faces(faces, {"A": [10.0, 0.0], "B": [10.0, 0.0]}, set(), 0.4, new_person_id)
    assert first == [(0, "A"), (1, "person-1"), (2, "B")]

    # Partial retry: face 0's record exists and was adopted into A, so A
    # is already used and its sum carries face 0's unit vector. Only the
    # pending faces (1, 2) are reclustered, against the folded state;
    # they must land on the same person ids — possible only because new
    # person ids are keyed by the original face index, not by how many
    # were created so far.
    retry = [
        (0, "A"),
    ] + cluster_faces(
        faces[1:],
        {"A": [11.0, 0.0], "B": [10.0, 0.0]},
        {"A"},
        0.4,
        new_person_id,
    )
    assert retry == first


def _put_person(
    storage,
    person_id: str,
    revision: int,
    face_ids: list[str],
    display_name: str,
    created_at: str,
    deleted_at: str | None = None,
    parent_revision: int | None = None,
) -> None:
    manifest = PersonManifest(
        library_id=UUID(LIBRARY_ID),
        person_id=UUID(person_id),
        revision=revision,
        parent_revision=parent_revision,
        operation_id=UUID(int=1),
        created_at=created_at,
        display_name=display_name,
        face_ids=tuple(UUID(face_id) for face_id in face_ids),
        deleted_at=deleted_at,
    )
    storage.put(
        f"manifests/people/{person_id}/{revision}.json", encode(manifest), "application/json"
    )


def _put_face(storage, asset_id, run_id, index, face, person_id) -> str:
    face_id = str(derive_face_id(LIBRARY_ID, asset_id, run_id, index))
    _record, data = build_face_record(LIBRARY_ID, face_id, asset_id, run_id, index, face, person_id)
    storage.put(f"manifests/faces/{face_id}.json", data, "application/json")
    return face_id


def _put_run(storage, payload, created_at) -> str:
    record, _data, run_id = build_processing_artifact(LIBRARY_ID, payload, created_at=created_at)
    storage.put(
        f"manifests/processing/{record.artifact_id}.json", encode(record), "application/json"
    )
    return run_id


def test_s3_face_state_rebuilds_centroids_people_and_attribution():
    storage = FakeStorage()

    def face(embedding):
        return {"box": [0.1, 0.1, 0.2, 0.3], "confidence": 0.9, "embedding": embedding}

    # Asset A has an old run and a newer one; a fingerprint run (a
    # different processing type) is newer still and must be ignored.
    old_run = _put_run(
        storage,
        make_payload(faces=[face([1.0, 0.0])]),
        "2026-01-01T00:00:00+00:00",
    )
    new_faces = [face([1.0, 0.0]), face([0.0, 1.0]), face([1.0, 0.0])]
    new_run = _put_run(
        storage,
        make_payload(faces=new_faces),
        "2026-02-01T00:00:00+00:00",
    )
    _put_run(
        storage,
        make_payload(analysisType="fingerprint", faces=[]),
        "2026-03-01T00:00:00+00:00",
    )
    storage.put(
        "manifests/processing/99999999-9999-4999-8999-999999999999.json",
        b"not-json",
        "application/json",
    )

    # Face records: one from the old run (excluded), three from the new.
    # The third records PERSON_OLD in the record, but the person head
    # claims it (a move updated the head, not the record).
    _put_face(storage, ASSET_ID, old_run, 0, face([1.0, 0.0]), PERSON_LEGACY)
    face_0 = _put_face(storage, ASSET_ID, new_run, 0, new_faces[0], PERSON_A)
    face_1 = _put_face(storage, ASSET_ID, new_run, 1, new_faces[1], PERSON_B)
    face_2 = _put_face(storage, ASSET_ID, new_run, 2, new_faces[2], PERSON_OLD)

    # Heads: A has two revisions (newest is the head and claims face_2);
    # B's only revision is tombstoned; OLD and LEGACY never got heads.
    _put_person(
        storage, PERSON_A, 1, [face_0], "Alicia", "2026-01-05T00:00:00Z", parent_revision=None
    )
    _put_person(
        storage,
        PERSON_A,
        2,
        [face_0, face_2],
        "Alice",
        "2026-01-05T00:00:00Z",
        parent_revision=1,
    )
    _put_person(
        storage,
        PERSON_B,
        1,
        [face_1],
        "Bob",
        "2026-01-05T00:00:00Z",
        deleted_at="2026-01-06T00:00:00Z",
    )

    centroids, people = s3_face_state(storage)

    by_person = {item["personId"]: item for item in centroids}
    assert set(by_person) == {PERSON_A, PERSON_B}
    assert by_person[PERSON_A]["sum"] == [2.0, 0.0]  # face_0 plus claimed face_2
    assert by_person[PERSON_B]["sum"] == [0.0, 1.0]

    people_by_id = {item["personId"]: item for item in people}
    # The head stands: display name, created time, and claimed face ids.
    assert people_by_id[PERSON_A] == {
        "personId": PERSON_A,
        "displayName": "Alice",
        "createdAt": "2026-01-05T00:00:00Z",
        "faceIds": sorted([face_0, face_2]),
    }
    # The tombstoned person has no head, so it is synthesized headless
    # from its face record, attributed to the newest run's wall clock.
    assert people_by_id[PERSON_B] == {
        "personId": PERSON_B,
        "displayName": "",
        "createdAt": "2026-02-01T00:00:00Z",
        "faceIds": [face_1],
    }
    # OLD's face is claimed by A's head, and LEGACY's only face is from
    # the superseded run, so neither appears as a person.
    assert PERSON_OLD not in people_by_id and PERSON_LEGACY not in people_by_id
    # Heads come before synthesized headless persons, each in id order.
    assert [item["personId"] for item in people] == [PERSON_A, PERSON_B]
