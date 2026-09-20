"""Unit tests for the phase 1a-1b response models (no services required).

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
    FaceRefOut,
    LocationOut,
    MutationOut,
    PeoplePageOut,
    PersonDetailOut,
    PersonSummaryOut,
    PhotoSummaryOut,
    PreviewStatusOut,
    ProcessingStatusOut,
    UserStateOut,
)

FIXTURES = Path(__file__).parent / "fixtures" / "api_golden"
SEED = FIXTURES / "seed.json"
PHASE1A = FIXTURES / "phase1a.json"
PHASE1B = FIXTURES / "phase1b.json"


def normalize(value):
    """The goldens' comparison normalization (tests/test_api_contract.py):
    key order is irrelevant and integral-valued floats equal integers
    (JSON's ``number`` does not distinguish 1 from 1.0); array order is
    preserved."""
    if isinstance(value, dict):
        return {key: normalize(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [normalize(item) for item in value]
    if isinstance(value, float) and value.is_integer():
        return int(value)
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


# ---------------------------------------------------------------------------
# Phase 1b: people-read models (GET /people, GET /people/{id}).
# ---------------------------------------------------------------------------


def un_paged_person_detail(display_name: str) -> dict:
    """The un-paged detail body of the seeded person with this display name."""
    for (_method, path), case in cases(PHASE1B).items():
        if (
            path.startswith("/people/")
            and "?" not in path
            and case["status"] == 200
            and case["body"]["displayName"] == display_name
        ):
            return case["body"]
    raise AssertionError(f"no un-paged person detail for {display_name!r}")


def test_people_page_round_trips():
    for path in (
        "/people",
        "/people?q=ave",
        "/people?q=photo-10",
        "/people?q=zzz",
        "/people?limit=2",
        "/people?limit=2&offset=2",
    ):
        assert_round_trip(PeoplePageOut, body(PHASE1B, path))
    # The full list counts every matching person, not just the page.
    full = body(PHASE1B, "/people")
    assert (full["total"], full["named"], full["unnamed"]) == (3, 2, 1)
    # Named people sort before unnamed; faceCount breaks the name tie.
    assert [item["displayName"] for item in full["items"]] == ["Avery", "Sam", ""]
    # A no-match query keeps the wrapper shape with an empty page.
    assert body(PHASE1B, "/people?q=zzz")["items"] == []


def test_person_detail_round_trips():
    details = [
        case["body"]
        for (_method, path), case in cases(PHASE1B).items()
        if path.startswith("/people/") and case["status"] == 200
    ]
    assert len(details) == 7  # five people, plus Avery's two paged views
    for detail in details:
        assert_round_trip(PersonDetailOut, detail)
    # People whose faces are all invisible keep the empty-array shape.
    for display_name in ("Ghost", "Stale"):
        detail = un_paged_person_detail(display_name)
        assert detail["faceCount"] == 0 and detail["photoCount"] == 0 and detail["faces"] == []
    # Paged views still report the full counts.
    paged = body(
        PHASE1B,
        next(path for (_method, path) in cases(PHASE1B) if path.endswith("?limit=1&offset=1")),
    )
    assert paged["faceCount"] == 2 and paged["photoCount"] == 2 and len(paged["faces"]) == 1


def test_face_ref_accepts_both_numeric_encodings():
    avery_page = body(PHASE1B, "/people")["items"][0]
    integral, fractional = avery_page["sampleFaces"]
    # The fixture records the wire as emitted: StrictFloat converges both
    # producer paths (SQL jsonb copy, Python jsonb parse) to a float
    # rendering, so the one value the SQL path used to spell as the JSON
    # integer 1 now spells 1.0 on both roads.
    assert type(integral["confidence"]) is float and integral["confidence"] == 1.0
    assert integral["box"] == [0.0, 0.0, 1.0, 1.0]
    assert fractional["confidence"] == 0.9 and type(fractional["confidence"]) is float
    for face in avery_page["sampleFaces"]:
        assert_round_trip(FaceRefOut, face)
        assert_round_trip(PersonSummaryOut, avery_page)
    # The same face arriving with the other spelling (a producer that
    # writes integral numbers, or a jsonb parse that yields Python ints)
    # must validate and compare identically: the number, not its
    # spelling, is the contract.
    int_encoded = {
        "faceId": integral["faceId"],
        "assetId": integral["assetId"],
        "originalFilename": integral["originalFilename"],
        "box": [int(value) for value in integral["box"]],
        "confidence": int(integral["confidence"]),
        "thumbnailUrl": integral["thumbnailUrl"],
    }
    assert_round_trip(FaceRefOut, int_encoded)
    dumped_int = FaceRefOut.model_validate(int_encoded).model_dump(mode="json", by_alias=True)
    dumped_float = FaceRefOut.model_validate(integral).model_dump(mode="json", by_alias=True)
    assert normalize(dumped_int) == normalize(dumped_float)
    for face in un_paged_person_detail("Avery")["faces"]:
        assert_round_trip(FaceRefOut, face)


def test_analysis_face_accepts_integral_encodings():
    # The analysis path reads a JSONB ``faces`` column whose numeric values
    # the producer may have written as integers. StrictFloat accepts both
    # spellings, so an integral box or confidence can never 500 (a latent
    # hazard before the phase 1b number-spelling policy), and the rendering
    # converges on floats: the number, not its spelling, is the contract.
    faces = body(PHASE1A, asset_path(13))["analysis"]["faces"]
    assert faces
    for face in faces:
        integral = integralize(face)
        assert_round_trip(AnalysisFaceOut, integral)
        dumped_integral = AnalysisFaceOut.model_validate(integral).model_dump(mode="json", by_alias=True)
        dumped_float = AnalysisFaceOut.model_validate(face).model_dump(mode="json", by_alias=True)
        assert normalize(dumped_integral) == normalize(dumped_float)


def integralize(value):
    """Rewrite integral-valued floats as ints (the other spelling of the
    same JSON number), recursively."""
    if isinstance(value, dict):
        return {key: integralize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [integralize(item) for item in value]
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def test_people_models_reject_forbidden_shapes():
    avery_page = body(PHASE1B, "/people")["items"][0]
    face = avery_page["sampleFaces"][1]
    with pytest.raises(ValidationError):
        PeoplePageOut.model_validate({**body(PHASE1B, "/people?q=zzz"), "surprise": 1})
    with pytest.raises(ValidationError):
        PersonSummaryOut.model_validate({**avery_page, "faceCount": "2"})
    # PersonDetail exposes faces, not sampleFaces.
    with pytest.raises(ValidationError):
        PersonDetailOut.model_validate({**un_paged_person_detail("Avery"), "sampleFaces": []})
    with pytest.raises(ValidationError):
        FaceRefOut.model_validate({**face, "confidence": "high"})
    with pytest.raises(ValidationError):
        FaceRefOut.model_validate({**face, "box": ["0.1"]})
    with pytest.raises(ValidationError):
        FaceRefOut.model_validate({k: v for k, v in face.items() if k != "faceId"})
