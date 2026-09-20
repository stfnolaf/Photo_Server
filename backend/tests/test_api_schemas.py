"""Unit tests for the phase 1a response models (no services required).

Each model in ``photo_server.api_schemas`` is validated against real JSON
captured in the golden fixtures (``tests/fixtures/api_golden/``) and must
round-trip it identically after recursive key sorting. The models must also
reject the wire shapes the response-model design forbids: unexpected keys,
wrong primitive types, out-of-range values, and the wrong document variant.

The fixtures are recorded by the integration tests; these unit tests skip
when a fixture is not present yet (e.g. before the first phase 1a recording).
"""

import json
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import TypeAdapter, ValidationError

from photo_server.api_schemas import (
    AnalysisFaceOut,
    AnalysisResultOut,
    AnalysisStatusOut,
    AssetDetailOut,
    AssetDetailV1Out,
    AssetDetailV2Out,
    AssetDocOut,
    AssetDocV1Out,
    AssetDocV2Out,
    BlobOut,
    BrowsePageOut,
    BurstDetailOut,
    LocationOut,
    MutationOut,
    PhotoSummaryOut,
    PreviewStatusOut,
    ProcessingStatusOut,
    UserStateOut,
)

FIXTURES = Path(__file__).parent / "fixtures" / "api_golden"
SEED = FIXTURES / "seed.json"
PHASE1A = FIXTURES / "phase1a.json"


def normalize(value):
    """Recursive key sorting: JSON object key order is not part of the contract."""
    if isinstance(value, dict):
        return {key: normalize(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [normalize(item) for item in value]
    return value


def cases(path: Path) -> dict[tuple[str, str], dict]:
    if not path.exists():
        pytest.skip(f"golden fixture {path.name} not recorded yet")
    payload = json.loads(path.read_text())
    return {(case["method"], case["path"]): case for case in payload["cases"]}


def body(fixture: Path, path: str, *, status: int = 200):
    entry = cases(fixture).get(("GET", path))
    assert entry is not None and entry["status"] == status, f"GET {path} (status {status}) missing"
    return entry["body"]


def assert_round_trip(model_or_adapter, data):
    """Validate ``data`` and assert the serialized output is identical to it."""
    instance = (
        model_or_adapter.model_validate(data)
        if hasattr(model_or_adapter, "model_validate")
        else model_or_adapter.validate_python(data)
    )
    dumped = instance.model_dump(mode="json", by_alias=True)
    assert normalize(dumped) == normalize(data), (
        "model does not round-trip the wire JSON:\n"
        f"  wire:  {json.dumps(normalize(data), sort_keys=True)}\n"
        f"  model: {json.dumps(normalize(dumped), sort_keys=True)}"
    )
    return instance


def asset_path(number: int) -> str:
    return f"/assets/{UUID(int=number)}"


DOC_ADAPTER = TypeAdapter(AssetDocOut)
DETAIL_ADAPTER = TypeAdapter(AssetDetailOut)


def test_asset_doc_union_round_trips_both_versions():
    """The document union routes on schemaVersion and preserves each variant's
    exact key set (11 for v1, 14 for v2) byte-for-byte."""
    seed_docs = body(SEED, "/assets")
    for doc in seed_docs:
        assert doc["schemaVersion"] == 1
        assert set(doc) == {
            "schemaVersion",
            "libraryId",
            "assetId",
            "revision",
            "previousRevision",
            "operationId",
            "primaryBlobId",
            "blobs",
            "importedAt",
            "captureTime",
            "metadata",
        }
        instance = DOC_ADAPTER.validate_python(doc)
        assert isinstance(instance, AssetDocV1Out)
        assert_round_trip(AssetDocV1Out, doc)

    phase1a_docs = body(PHASE1A, "/assets")
    v2_docs = [doc for doc in phase1a_docs if doc["schemaVersion"] == 2]
    v1_docs = [doc for doc in phase1a_docs if doc["schemaVersion"] == 1]
    assert v2_docs and v1_docs
    for doc in v2_docs:
        assert set(doc) == set(seed_docs[0]) | {"userState", "deletedAt", "mutation"}
        assert doc["previousRevision"] is not None and doc["mutation"] is not None
        instance = DOC_ADAPTER.validate_python(doc)
        assert isinstance(instance, AssetDocV2Out)
        assert_round_trip(AssetDocV2Out, doc)
    for doc in v1_docs:
        instance = DOC_ADAPTER.validate_python(doc)
        assert isinstance(instance, AssetDocV1Out)
        assert_round_trip(AssetDocV1Out, doc)


def test_asset_detail_round_trips():
    seed_detail = body(SEED, asset_path(1))
    assert seed_detail["schemaVersion"] == 1
    instance = DETAIL_ADAPTER.validate_python(seed_detail)
    assert isinstance(instance, AssetDetailV1Out)
    assert_round_trip(AssetDetailV1Out, seed_detail)

    for number in (13, 16):
        detail = body(PHASE1A, asset_path(number))
        assert detail["schemaVersion"] == 2
        instance = DETAIL_ADAPTER.validate_python(detail)
        assert isinstance(instance, AssetDetailV2Out)
        assert_round_trip(AssetDetailV2Out, detail)

    for number in (10, 14, 15):
        detail = body(PHASE1A, asset_path(number))
        assert detail["schemaVersion"] == 1
        assert_round_trip(AssetDetailV1Out, detail)


def test_photo_summary_round_trips():
    for page in (body(PHASE1A, "/library/assets"), body(SEED, "/library/assets")):
        for item in page["items"]:
            assert_round_trip(PhotoSummaryOut, item)


def test_browse_page_and_burst_round_trip():
    for path in (
        "/library/assets",
        "/library/assets?limit=2",
        "/library/assets?media_type=RAW",
        "/library/assets?date_from=2024-05-01&date_to=2024-05-01",
        "/library/assets?q=river",
        "/library/assets?rating_min=4&favorite=true",
        "/library/assets?deleted=true",
    ):
        assert_round_trip(BrowsePageOut, body(PHASE1A, path))

    assert_round_trip(BrowsePageOut, body(SEED, "/library/assets"))
    assert_round_trip(BurstDetailOut, body(PHASE1A, f"{asset_path(11)}/burst"))


def test_nested_models_round_trip():
    detail = body(PHASE1A, asset_path(13))
    for blob in detail["blobs"]:
        assert_round_trip(BlobOut, blob)
    assert detail["userState"]["location"] is not None
    assert_round_trip(UserStateOut, detail["userState"])
    assert_round_trip(LocationOut, detail["userState"]["location"])
    assert_round_trip(MutationOut, detail["mutation"])
    for job in detail["processing"]:
        assert_round_trip(ProcessingStatusOut, job)
    assert detail["analysis"]["result"] is not None
    assert_round_trip(AnalysisStatusOut, detail["analysis"])
    assert_round_trip(AnalysisResultOut, detail["analysis"]["result"])
    for face in detail["analysis"]["faces"]:
        assert_round_trip(AnalysisFaceOut, face)

    # Default (v1) user state: location absent, empty caption/keywords.
    v1_detail = body(PHASE1A, asset_path(10))
    assert v1_detail["userState"]["location"] is None
    assert_round_trip(UserStateOut, v1_detail["userState"])

    # Every preview status across the recorded details and browse items.
    seen = set()
    for number in (10, 13, 14, 15, 16):
        status = body(PHASE1A, asset_path(number))["preview"]
        assert_round_trip(PreviewStatusOut, status)
        seen.add(status["status"])
    for item in body(PHASE1A, "/library/assets")["items"]:
        assert_round_trip(PreviewStatusOut, item["preview"])
        seen.add(item["preview"]["status"])
    assert seen == {"missing", "pending", "running", "ready", "failed", "unavailable"}

    # Every analysis status across the recorded details.
    seen = set()
    for number in (10, 13, 14, 15, 16):
        analysis = body(PHASE1A, asset_path(number))["analysis"]
        assert_round_trip(AnalysisStatusOut, analysis)
        seen.add(analysis["status"])
    assert seen == {"missing", "pending", "ready", "failed"}

    # Both mutation actions (patch on 13, delete on 16).
    assert body(PHASE1A, asset_path(13))["mutation"]["action"] == "asset.patch"
    assert body(PHASE1A, asset_path(16))["mutation"]["action"] == "asset.delete"
    assert_round_trip(MutationOut, body(PHASE1A, asset_path(16))["mutation"])


def test_models_reject_forbidden_shapes():
    v1_doc = body(SEED, "/assets")[0]
    v2_doc = next(doc for doc in body(PHASE1A, "/assets") if doc["schemaVersion"] == 2)
    summary = body(PHASE1A, "/library/assets")["items"][0]

    # A v1 document carrying a v2-only key breaks the union (the v1 variant
    # forbids extras instead of silently dropping or passing the key through).
    with pytest.raises(ValidationError):
        DOC_ADAPTER.validate_python({**v1_doc, "userState": {}})
    # v2 documents must carry a non-null mutation and integer ancestry.
    with pytest.raises(ValidationError):
        DOC_ADAPTER.validate_python({k: v for k, v in v2_doc.items() if k != "mutation"})
    with pytest.raises(ValidationError):
        DOC_ADAPTER.validate_python({**v2_doc, "previousRevision": None})
    # Wrong primitive types never coerce.
    with pytest.raises(ValidationError):
        PhotoSummaryOut.model_validate({**summary, "width": "4000"})
    with pytest.raises(ValidationError):
        PhotoSummaryOut.model_validate({**summary, "favorite": "true"})
    with pytest.raises(ValidationError):
        PreviewStatusOut.model_validate({"status": "weird", "error": None})
    with pytest.raises(ValidationError):
        BlobOut.model_validate(
            {
                "blobId": v1_doc["blobs"][0]["blobId"],
                "role": "ORIGINAL_TIFF",
                "objectKey": v1_doc["blobs"][0]["objectKey"],
                "sha256": v1_doc["blobs"][0]["sha256"],
                "sizeBytes": v1_doc["blobs"][0]["sizeBytes"],
                "originalFilename": v1_doc["blobs"][0]["originalFilename"],
            }
        )
    # Unexpected keys are a contract break, not a pass-through.
    with pytest.raises(ValidationError):
        PhotoSummaryOut.model_validate({**summary, "surprise": 1})
    # Out-of-range values are rejected.
    with pytest.raises(ValidationError):
        UserStateOut.model_validate(
            {
                "rating": 7,
                "favorite": False,
                "caption": "",
                "keywords": [],
                "location": None,
            }
        )
    with pytest.raises(ValidationError):
        LocationOut.model_validate({"name": "X", "latitude": 91, "longitude": 0})
    # A v1 document claiming revision 2 is invalid (v1 revisions are always 1).
    with pytest.raises(ValidationError):
        DOC_ADAPTER.validate_python({**v1_doc, "revision": 2})
