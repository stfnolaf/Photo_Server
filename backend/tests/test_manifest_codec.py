import json
from copy import deepcopy
from pathlib import Path

import pytest

from photo_server.manifests import (
    ManifestCodecError,
    canonical_json,
    canonicalize,
    decode,
    encode,
    sha256,
    validate_history,
    validate_revision,
)

ROOT = Path(__file__).parents[2]
EXAMPLES = ROOT / "backend/tests/fixtures/s3-authoritative-examples"


def document(name):
    return json.loads((EXAMPLES / name).read_text())


@pytest.mark.parametrize(
    ("kind", "name"),
    [
        ("asset", "asset-manifest-v1.json"),
        ("album", "album-manifest-v1.json"),
        ("tombstone", "tombstone-v1.json"),
        ("processing-artifact", "processing-artifact-v1.json"),
        ("person", "person-manifest-v1.json"),
    ],
)
def test_valid_examples_round_trip_byte_stably(kind, name):
    payload = (EXAMPLES / name).read_bytes()
    first = canonicalize(payload, kind)
    assert first == canonicalize(first, kind)
    assert encode(decode(payload, kind)) == first
    assert sha256(decode(payload, kind)) == sha256(decode(first, kind))


def test_canonical_json_is_utf8_sorted_compact_and_deterministic():
    value = {"z": "café", "a": {"z": 2, "a": 1}}
    assert encode(decode(json.dumps(document("album-manifest-v1.json")), "album"))
    from photo_server.manifests.codec import canonical_json

    assert canonical_json(value) == b'{"a":{"a":1,"z":2},"z":"caf\xc3\xa9"}'
    assert canonical_json(value) == canonical_json({"a": value["a"], "z": value["z"]})


@pytest.mark.parametrize(
    "payload",
    [b"{", b"[]", b'{"schemaVersion": 1,}', b"\xff"],
)
def test_malformed_json_is_rejected(payload):
    with pytest.raises(ManifestCodecError):
        decode(payload, "album")


def test_duplicate_keys_are_rejected():
    source = json.dumps(document("album-manifest-v1.json"))[:-1] + ',"name":"duplicate"}'
    with pytest.raises(ManifestCodecError, match="duplicate"):
        decode(source, "album")


@pytest.mark.parametrize(
    ("field", "value"),
    [("unexpected", True), ("schemaVersion", 99)],
)
def test_unknown_fields_and_unsupported_versions_are_rejected(field, value):
    value_doc = document("album-manifest-v1.json")
    value_doc[field] = value
    with pytest.raises(ManifestCodecError):
        decode(json.dumps(value_doc), "album")


def test_revision_and_ancestry_rules_reject_gaps_stale_and_future_records():
    first = decode((EXAMPLES / "album-manifest-v1.json").read_bytes(), "album")
    initial = deepcopy(document("album-manifest-v1.json"))
    initial.update(revision=1, parentRevision=None)
    first = decode(json.dumps(initial), "album")
    second_doc = deepcopy(initial)
    second_doc.update(revision=2, parentRevision=1)
    second = decode(json.dumps(second_doc), "album")
    assert validate_history([second, first]) is second
    with pytest.raises(ValueError):
        validate_history([first, second, second])
    with pytest.raises(ValueError, match="stale"):
        validate_revision(second, current_revision=2)
    with pytest.raises(ValueError, match="stale"):
        validate_revision(second, expected_parent=7)


def test_invalid_primary_blob_duplicate_reference_and_checksum_are_rejected():
    value = document("asset-manifest-v1.json")
    value["primaryBlobId"] = value["blobs"][1]["blobId"]
    with pytest.raises(ManifestCodecError, match="primary"):
        decode(json.dumps(value), "asset")
    value = document("asset-manifest-v1.json")
    value["blobs"].append(deepcopy(value["blobs"][0]))
    with pytest.raises(ManifestCodecError, match="unique"):
        decode(json.dumps(value), "asset")
    value = document("asset-manifest-v1.json")
    value["blobs"][0]["sha256"] = "A" * 64
    with pytest.raises(ManifestCodecError, match="SHA"):
        decode(json.dumps(value), "asset")


def test_invalid_tombstone_and_processing_reference_are_rejected():
    tombstone = document("tombstone-v1.json")
    tombstone["parentRevision"] = 1
    with pytest.raises(ManifestCodecError, match="revision"):
        decode(json.dumps(tombstone), "tombstone")
    asset = document("asset-manifest-v1.json")
    asset["processing"] = [
        {
            "artifactKey": "objects/" + "c" * 64,
            "artifactSha256": "c" * 64,
            "inputSha256": "d" * 64,
            "processingType": "semantic",
            "implementationVersion": "v1",
        }
    ]
    with pytest.raises(ManifestCodecError, match="input checksum"):
        decode(json.dumps(asset), "asset")


def test_person_manifest_round_trips_empty_name_and_assignments():
    payload = document("person-manifest-v1.json")
    payload["displayName"] = ""
    record = decode(json.dumps(payload), "person")
    assert record.display_name == ""
    assert decode(encode(record), "person") == record


def test_fingerprint_and_burst_manifests_round_trip_strictly():
    fingerprint = {
        "schemaVersion": 1,
        "libraryId": "00000000-0000-4000-8000-000000000001",
        "assetId": "00000000-0000-4000-8000-000000000002",
        "algorithmVersion": "burst-hash-v1",
        "pHash": "0123456789abcdef",
        "dHash": "fedcba9876543210",
        "width": 1200,
        "height": 800,
        "chromaHistogram": "00" * 12,
        "createdAt": "2026-01-01T00:00:00Z",
    }
    burst = {
        "schemaVersion": 1,
        "libraryId": fingerprint["libraryId"],
        "revision": 1,
        "parentRevision": None,
        "operationId": "00000000-0000-4000-8000-000000000003",
        "createdAt": "2026-01-01T00:00:01Z",
        "policyVersion": "burst-cluster-v2",
        "operation": {
            "action": "burst.setRepresentative",
            "clusterId": "00000000-0000-4000-8000-000000000004",
            "assetId": fingerprint["assetId"],
        },
        "clusters": [
            {
                "clusterId": "00000000-0000-4000-8000-000000000004",
                "representativeAssetId": fingerprint["assetId"],
                "representativeSelected": True,
                "assetIds": [fingerprint["assetId"]],
            }
        ],
        "excludedAssetIds": [],
    }
    assert encode(decode(canonical_json(fingerprint), "fingerprint")) == canonical_json(fingerprint)
    assert encode(decode(canonical_json(burst), "burst")) == canonical_json(burst)
    burst["clusters"][0]["representativeAssetId"] = (
        "00000000-0000-4000-8000-000000000099"
    )
    with pytest.raises(ManifestCodecError, match="representative"):
        decode(canonical_json(burst), "burst")
