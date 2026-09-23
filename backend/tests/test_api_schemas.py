"""Unit tests for the phase 1a-4 response models (no services required).

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
    AlbumOut,
    AnalysisFaceOut,
    AnalysisResultOut,
    AnalysisStatusOut,
    BatchAbandonedOut,
    BlobOut,
    BrowsePageOut,
    BurstDetailOut,
    BurstRepresentativeOut,
    CurrentAssetDetailOut,
    CurrentAssetDocOut,
    FaceMoveOut,
    FaceRefOut,
    HealthOut,
    LocationOut,
    MutationOut,
    MutationResultOut,
    Pending202Out,
    PeoplePageOut,
    PersonDetailOut,
    PersonMergeOut,
    PersonRenameOut,
    PersonSummaryOut,
    PhotoSummaryOut,
    PreviewStatusOut,
    ProcessingStatusOut,
    QueueCountsOut,
    QueueResultOut,
    UploadBatchOut,
    UploadFileOut,
    UploadFileReceipt,
    UploadJobOut,
    UploadQueueStatusOut,
    UserStateOut,
    VerifyErrorOut,
    VerifyOut,
)

FIXTURES = Path(__file__).parent / "fixtures" / "api_golden"
SEED = FIXTURES / "seed.json"
PHASE1A = FIXTURES / "phase1a.json"
PHASE1B = FIXTURES / "phase1b.json"
PHASE2 = FIXTURES / "phase2_v2.json"
PHASE3A = FIXTURES / "phase3a.json"
PHASE3B = FIXTURES / "phase3b.json"
PHASE4 = FIXTURES / "phase4.json"


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


DOC_ADAPTER = TypeAdapter(CurrentAssetDocOut)
DETAIL_ADAPTER = TypeAdapter(CurrentAssetDetailOut)


def test_current_asset_doc_round_trips():
    for doc in body(FIXTURES / "seed_flat.json", "/assets"):
        assert doc["schemaVersion"] == 2
        assert_round_trip(CurrentAssetDocOut, doc)


def test_current_asset_detail_round_trips():
    detail = body(FIXTURES / "phase1a_flat.json", asset_path(13))
    assert detail["schemaVersion"] == 2
    assert_round_trip(CurrentAssetDetailOut, detail)


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
    current_doc = body(FIXTURES / "seed_flat.json", "/assets")[0]
    summary = body(PHASE1A, "/library/assets")["items"][0]

    # Current documents require the complete state shape.
    with pytest.raises(ValidationError):
        DOC_ADAPTER.validate_python({k: v for k, v in current_doc.items() if k != "userState"})
    with pytest.raises(ValidationError):
        DOC_ADAPTER.validate_python({**current_doc, "schemaVersion": 1})
    # Wrong primitive types never coerce.
    with pytest.raises(ValidationError):
        PhotoSummaryOut.model_validate({**summary, "width": "4000"})
    with pytest.raises(ValidationError):
        PhotoSummaryOut.model_validate({**summary, "favorite": "true"})
    # The producer always emits both summary URLs; null is a caught bug.
    with pytest.raises(ValidationError):
        PhotoSummaryOut.model_validate({**summary, "thumbnailUrl": None})
    with pytest.raises(ValidationError):
        PhotoSummaryOut.model_validate({**summary, "previewUrl": None})
    with pytest.raises(ValidationError):
        PreviewStatusOut.model_validate({"status": "weird", "error": None})
    with pytest.raises(ValidationError):
        BlobOut.model_validate(
            {
                "blobId": current_doc["blobs"][0]["blobId"],
                "role": "ORIGINAL_TIFF",
                "objectKey": current_doc["blobs"][0]["objectKey"],
                "sha256": current_doc["blobs"][0]["sha256"],
                "sizeBytes": current_doc["blobs"][0]["sizeBytes"],
                "originalFilename": current_doc["blobs"][0]["originalFilename"],
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


# ---------------------------------------------------------------------------
# Phase 2: upload and health models.
# ---------------------------------------------------------------------------


def phase2_cases() -> list[dict]:
    if not PHASE2.exists():
        pytest.skip(f"golden fixture {PHASE2.name} not recorded yet")
    return json.loads(PHASE2.read_text())["cases"]


def phase2_find(method: str, path: str, *, status: int | None = None) -> dict:
    matches = [
        case
        for case in phase2_cases()
        if case["method"] == method and case["path"] == path and (status is None or case["status"] == status)
    ]
    assert matches, f"{method} {path} (status {status}) missing from {PHASE2.name}"
    return matches[0]


def phase2_batch_bodies() -> list[dict]:
    """Every recorded body in the describe_batch() shape: the 201 creates,
    the 202 seal/retry responses, the single-batch GETs, and every item of
    the list responses."""
    batches = []
    for case in phase2_cases():
        body = case["body"]
        if isinstance(body, dict) and "batchId" in body and "files" in body and "jobs" in body:
            batches.append(body)
        elif isinstance(body, list) and all(
            isinstance(item, dict) and "batchId" in item and "files" in item for item in body
        ):
            batches.extend(body)
    return batches


def test_upload_batch_models_round_trip():
    batches = phase2_batch_bodies()
    # The section records every lifecycle state the models declare:
    # accepting (GET before seal), queued (seal/retry), complete (A), failed
    # (B), and the discarded batches' 201 bodies.
    states = {body["status"] for body in batches}
    assert {"accepting", "queued", "complete", "failed"} <= states
    for batch in batches:
        assert_round_trip(UploadBatchOut, batch)
        for file in batch["files"]:
            assert_round_trip(UploadFileOut, file)
        for job in batch["jobs"]:
            assert_round_trip(UploadJobOut, job)
    # The free-form job result blob (decision 4) validates as a dict.
    complete = next(body for body in batches if body["status"] == "complete")
    for job in complete["jobs"]:
        assert isinstance(job["result"], dict)
        assert job["result"]["assetId"]
    # skipped/failed files carry a reason or error; uploaded files carry a
    # uuid assetId only after onboarding completes.
    for file in complete["files"]:
        if file["status"] == "skipped":
            assert file["reason"] is not None and file["assetId"] is None
        if file["status"] == "imported":
            UUID(file["assetId"])


def test_upload_receipt_models_round_trip():
    receipts = [
        case["body"]
        for case in phase2_cases()
        if case["method"] == "PUT" and case["status"] == 200
    ]
    assert receipts, "no PUT receipts recorded in the phase2 golden"
    # The one replayed PUT must carry the same digest as its fresh upload.
    by_file = {}
    for receipt in receipts:
        by_file.setdefault(receipt["fileId"], []).append(receipt)
    replayed = [receipt for receipts_ in by_file.values() for receipt in receipts_ if receipt["replayed"]]
    assert replayed, "no replayed PUT recorded in the phase2 golden"
    for receipt in replayed:
        fresh = next(item for item in by_file[receipt["fileId"]] if not item["replayed"])
        assert receipt["sha256"] == fresh["sha256"]
    for receipt in receipts:
        assert_round_trip(UploadFileReceipt, receipt)
    abandoned = [
        case["body"]
        for case in phase2_cases()
        if case["method"] == "DELETE" and case["status"] == 200
    ]
    assert len(abandoned) == 2
    for body in abandoned:
        assert_round_trip(BatchAbandonedOut, body)


def test_queue_and_health_models_round_trip():
    queue_body = phase2_find("GET", "/upload-queue")["body"]
    assert_round_trip(UploadQueueStatusOut, queue_body)
    health = phase2_find("GET", "/health")["body"]
    assert_round_trip(HealthOut, health)
    # The models' field sets must match the wire exactly: every recorded key
    # is a model field, and every model field appears on the wire.
    assert set(UploadQueueStatusOut.model_validate(queue_body).model_dump(by_alias=True).keys()) == set(
        queue_body
    )
    assert set(HealthOut.model_validate(health).model_dump(by_alias=True).keys()) == set(health)


def test_health_out_round_trips_every_flag_combination():
    """Phase 3B of the AI service split plan: the four service-visibility
    flags round-trip in every configured x reachable combination for both
    services, and only as strict booleans (a string or an int is a contract
    break, like every other wire primitive here)."""
    health = phase2_find("GET", "/health")["body"]
    for semantic_configured in (False, True):
        for semantic_reachable in (False, True):
            for face_configured in (False, True):
                for face_reachable in (False, True):
                    body = {
                        **health,
                        "aiSemanticConfigured": semantic_configured,
                        "aiSemanticReachable": semantic_reachable,
                        "aiFaceConfigured": face_configured,
                        "aiFaceReachable": face_reachable,
                    }
                    assert_round_trip(HealthOut, body)
    with pytest.raises(ValidationError):
        HealthOut.model_validate({**health, "aiFaceReachable": "true"})
    with pytest.raises(ValidationError):
        HealthOut.model_validate({**health, "aiSemanticConfigured": 1})


def test_upload_models_reject_forbidden_shapes():
    batches = phase2_batch_bodies()
    complete = next(body for body in batches if body["status"] == "complete")
    accepting = next(body for body in batches if body["status"] == "accepting")
    with pytest.raises(ValidationError):
        UploadBatchOut.model_validate({**accepting, "surprise": 1})
    with pytest.raises(ValidationError):
        UploadBatchOut.model_validate({**accepting, "status": "sealed"})
    with pytest.raises(ValidationError):
        UploadBatchOut.model_validate({**accepting, "createdAt": "1735689600"})
    with pytest.raises(ValidationError):
        UploadBatchOut.model_validate({**complete, "sealedAt": "soon"})
    with pytest.raises(ValidationError):
        UploadFileOut.model_validate({**complete["files"][0], "required": "true"})
    with pytest.raises(ValidationError):
        UploadFileOut.model_validate({**complete["files"][0], "status": "staged"})
    with pytest.raises(ValidationError):
        UploadFileOut.model_validate({**complete["files"][0], "sizeBytes": 100.0})
    with pytest.raises(ValidationError):
        UploadJobOut.model_validate({**complete["jobs"][0], "result": ["not", "a", "dict"]})
    with pytest.raises(ValidationError):
        UploadJobOut.model_validate({**complete["jobs"][0], "status": "done"})
    receipt = next(case["body"] for case in phase2_cases() if case["method"] == "PUT" and case["status"] == 200)
    with pytest.raises(ValidationError):
        UploadFileReceipt.model_validate({**receipt, "status": "pending"})
    with pytest.raises(ValidationError):
        UploadFileReceipt.model_validate({**receipt, "sha256": receipt["sha256"].upper()})
    with pytest.raises(ValidationError):
        UploadFileReceipt.model_validate({**receipt, "sha256": receipt["sha256"][:60]})
    with pytest.raises(ValidationError):
        UploadFileReceipt.model_validate({**receipt, "replayed": "no"})
    abandoned = next(
        case["body"] for case in phase2_cases() if case["method"] == "DELETE" and case["status"] == 200
    )
    with pytest.raises(ValidationError):
        BatchAbandonedOut.model_validate({**abandoned, "status": "abandoned"})
    health = phase2_find("GET", "/health")["body"]
    with pytest.raises(ValidationError):
        HealthOut.model_validate({**health, "surprise": 1})
    with pytest.raises(ValidationError):
        HealthOut.model_validate({**health, "status": "degraded"})
    with pytest.raises(ValidationError):
        HealthOut.model_validate({**health, "assets": "0"})
    queue = phase2_find("GET", "/upload-queue")["body"]
    with pytest.raises(ValidationError):
        QueueCountsOut.model_validate({**queue, "uploadsActive": 1})
    with pytest.raises(ValidationError):
        UploadQueueStatusOut.model_validate({k: v for k, v in queue.items() if k != "uploadsActive"})
    # The one status the frontend union does not declare is still wire
    # legal: a batch can be observed in the deleting window, and the
    # frozen-wire model must accept it (decision 7).
    assert_round_trip(UploadBatchOut, {**accepting, "status": "deleting"})


# ---------------------------------------------------------------------------
# Phase 3a: album models (all six album endpoints).
# ---------------------------------------------------------------------------


def phase3a_album_bodies() -> list[dict]:
    """Every recorded body in the album-document shape: the 201 creates, the
    200 patch/delete/restore responses, the single-album GETs, and every
    item of every list response."""
    if not PHASE3A.exists():
        pytest.skip(f"golden fixture {PHASE3A.name} not recorded yet")
    albums = []
    for case in json.loads(PHASE3A.read_text())["cases"]:
        body = case["body"]
        if isinstance(body, dict) and "albumId" in body and "mutation" in body:
            albums.append(body)
        elif isinstance(body, list) and body and all(
            isinstance(item, dict) and "albumId" in item and "mutation" in item
            for item in body
        ):
            albums.extend(body)
    return albums


def test_album_model_round_trips():
    """The model accepts every recorded album document and re-emits it
    unchanged, with the exact key set the wire carries (extra="forbid"
    would 500 on a key drift in either direction)."""
    albums = phase3a_album_bodies()
    assert albums, "no album documents recorded in the phase3a golden"
    for album in albums:
        instance = assert_round_trip(AlbumOut, album)
        assert set(instance.model_dump(by_alias=True)) == set(album)
        assert_round_trip(MutationOut, album["mutation"])
        assert album["mutation"]["entityId"] == album["albumId"]
    # The section exercises every album mutation action, both
    # previousRevision spellings, and both deletedAt states.
    actions = {album["mutation"]["action"] for album in albums}
    assert actions == {"album.create", "album.patch", "album.delete", "album.restore"}
    creates = [a for a in albums if a["mutation"]["action"] == "album.create"]
    later = [a for a in albums if a["mutation"]["action"] != "album.create"]
    assert creates and all(a["previousRevision"] is None for a in creates)
    assert later and all(
        isinstance(a["previousRevision"], int) and a["previousRevision"] >= 1 for a in later
    )
    stamped = [a for a in albums if a["deletedAt"] is not None]
    assert stamped and all(a["mutation"]["action"] == "album.delete" for a in stamped)
    assert any(a["deletedAt"] is None for a in albums)
    # The change set carries exactly the album fields the client sent:
    # the create/patch fields on those actions, nothing on delete/restore.
    for album in albums:
        action = album["mutation"]["action"]
        if action in {"album.delete", "album.restore"}:
            assert album["mutation"]["changes"] == {}
        else:
            assert set(album["mutation"]["changes"]) <= {"name", "description", "assetIds"}


def test_album_model_rejects_forbidden_shapes():
    albums = phase3a_album_bodies()
    create = next(a for a in albums if a["mutation"]["action"] == "album.create")
    deleted = next(a for a in albums if a["deletedAt"] is not None)
    with pytest.raises(ValidationError):
        AlbumOut.model_validate({**create, "surprise": 1})
    with pytest.raises(ValidationError):
        AlbumOut.model_validate({**create, "revision": "1"})
    with pytest.raises(ValidationError):
        AlbumOut.model_validate({**create, "schemaVersion": 2})
    with pytest.raises(ValidationError):
        AlbumOut.model_validate({**create, "name": ""})
    with pytest.raises(ValidationError):
        AlbumOut.model_validate({**create, "name": "x" * 201})
    with pytest.raises(ValidationError):
        AlbumOut.model_validate({k: v for k, v in create.items() if k != "mutation"})
    with pytest.raises(ValidationError):
        AlbumOut.model_validate({**create, "previousRevision": 0})
    with pytest.raises(ValidationError):
        AlbumOut.model_validate({**deleted, "deletedAt": 1})
    with pytest.raises(ValidationError):
        AlbumOut.model_validate({**create, "assetIds": ["not-a-uuid"]})
    with pytest.raises(ValidationError):
        AlbumOut.model_validate({**create, "mutation": {**create["mutation"], "action": "album.explode"}})


# ---------------------------------------------------------------------------
# Phase 3b: asset mutation and queue models (the nine mutation/queue
# endpoints: user-state/metadata patch, delete, restore, burst
# representative, /processing, /analysis, /analysis/retry, /preview/retry).
# ---------------------------------------------------------------------------

USER_STATE_KEYS = ("rating", "favorite", "caption", "keywords", "location")


def phase3b_cases() -> list[tuple[str, str, int, dict]]:
    if not PHASE3B.exists():
        pytest.skip(f"golden fixture {PHASE3B.name} not recorded yet")
    return [
        (case["method"], case["path"], case["status"], case["body"])
        for case in json.loads(PHASE3B.read_text())["cases"]
    ]


def phase3b_mutation_results() -> list[dict]:
    """Every recorded 200 mutation-result body: the five user-state keys
    plus the asset/operation identity (the burst representative's 200
    bodies carry a different key set and are excluded)."""
    results = [
        body
        for method, path, status, body in phase3b_cases()
        if status == 200
        and isinstance(body, dict)
        and {"assetId", "operationId", "revision", "deletedAt"} <= body.keys()
        and set(USER_STATE_KEYS) <= body.keys()
    ]
    assert results, "no mutation results recorded in the phase3b golden"
    return results


def test_mutation_result_model_round_trips():
    """The model accepts every recorded mutation result and re-emits it
    unchanged, with the exact key set the wire carries (extra="forbid"
    would 500 on a key drift in either direction); the embedded user
    state is itself a valid UserStateOut document."""
    for result in phase3b_mutation_results():
        instance = assert_round_trip(MutationResultOut, result)
        assert set(instance.model_dump(by_alias=True)) == set(result)
        assert_round_trip(
            UserStateOut, {key: result[key] for key in USER_STATE_KEYS}
        )
    # The section walks the lifecycle: delete results carry the pinned
    # deletedAt stamp, patch/restore results null; the revision sequence
    # advances 2 -> 2 (replay) -> 3 -> 4 on the round-trip asset and
    # 2 -> 2 (replay) on the metadata-route asset.
    deleted = [r for r in phase3b_mutation_results() if r["deletedAt"] is not None]
    assert deleted and all(isinstance(r["deletedAt"], str) for r in deleted)
    assert any(r["deletedAt"] is None for r in phase3b_mutation_results())
    round_trip_asset = [
        r for r in phase3b_mutation_results() if r["assetId"] == str(UUID(int=31))
    ]
    assert [r["revision"] for r in round_trip_asset] == [2, 2, 3, 4]
    metadata_route = [
        r for r in phase3b_mutation_results() if r["assetId"] == str(UUID(int=32))
    ]
    assert [r["revision"] for r in metadata_route] == [2, 2]
    # The metadata-route patch set only the favorite: the other user-state
    # fields echo their v1 defaults in the recorded bodies.
    assert all(
        r["favorite"] and r["rating"] == 0 and r["caption"] == "" and r["keywords"] == []
        and r["location"] is None
        for r in metadata_route
    )


def test_burst_representative_model_round_trips():
    reps = [
        body
        for method, path, status, body in phase3b_cases()
        if status == 200 and isinstance(body, dict) and "burstId" in body
    ]
    assert reps, "no burst representative results recorded in the phase3b golden"
    for body in reps:
        instance = assert_round_trip(BurstRepresentativeOut, body)
        assert set(instance.model_dump(by_alias=True)) == set(body)


def test_queue_result_model_round_trips():
    """Every recorded 202 queue response round-trips, and the three
    counters partition the selected (asset x job-type) pairs exactly."""
    results = [
        body
        for method, path, status, body in phase3b_cases()
        if status == 202 and isinstance(body, dict) and "jobTypes" in body
    ]
    assert results, "no queue results recorded in the phase3b golden"
    for body in results:
        instance = assert_round_trip(QueueResultOut, body)
        assert set(instance.model_dump(by_alias=True)) == set(body)
        assert (
            body["jobsQueued"]
            + body["jobsAlreadyQueued"]
            + body["jobsAlreadyRunning"]
            == body["assets"] * len(body["jobTypes"])
        )
    # The section covers both job families (processing and analysis).
    assert {tuple(body["jobTypes"]) for body in results} == {("metadata-v1",), ("ai-v1",)}


def test_preview_retry_model_round_trips():
    results = [
        body
        for method, path, status, body in phase3b_cases()
        if status == 200 and isinstance(body, dict) and set(body) == {"status", "error"}
    ]
    assert results, "no preview retry results recorded in the phase3b golden"
    for body in results:
        instance = assert_round_trip(PreviewStatusOut, body)
        assert set(instance.model_dump(by_alias=True)) == set(body)
    # The section exercises a running preview job (unmodified by the
    # retry upsert) and a pending one.
    assert {body["status"] for body in results} == {"running", "pending"}
    assert all(body["error"] is None for body in results)


def test_mutation_result_model_rejects_forbidden_shapes():
    result = phase3b_mutation_results()[0]
    with pytest.raises(ValidationError):
        MutationResultOut.model_validate({**result, "surprise": 1})
    with pytest.raises(ValidationError):
        MutationResultOut.model_validate(
            {key: value for key, value in result.items() if key != "assetId"}
        )
    with pytest.raises(ValidationError):
        MutationResultOut.model_validate({**result, "revision": "2"})
    with pytest.raises(ValidationError):
        MutationResultOut.model_validate({**result, "favorite": "true"})
    with pytest.raises(ValidationError):
        MutationResultOut.model_validate({**result, "assetId": "not-a-uuid"})
    with pytest.raises(ValidationError):
        MutationResultOut.model_validate({**result, "deletedAt": 1})
    with pytest.raises(ValidationError):
        MutationResultOut.model_validate({**result, "rating": 6})
    with pytest.raises(ValidationError):
        MutationResultOut.model_validate({**result, "keywords": [1]})
    with pytest.raises(ValidationError):
        MutationResultOut.model_validate(
            {**result, "location": {"name": "Harbor", "latitude": 91, "longitude": 0}}
        )


def test_burst_representative_model_rejects_forbidden_shapes():
    reps = [
        body
        for method, path, status, body in phase3b_cases()
        if status == 200 and isinstance(body, dict) and "burstId" in body
    ]
    rep = reps[0]
    with pytest.raises(ValidationError):
        BurstRepresentativeOut.model_validate({**rep, "burstId": "not-a-uuid"})
    with pytest.raises(ValidationError):
        BurstRepresentativeOut.model_validate(
            {"representativeAssetId": rep["representativeAssetId"]}
        )
    with pytest.raises(ValidationError):
        BurstRepresentativeOut.model_validate({**rep, "surprise": 1})


def test_queue_result_model_rejects_forbidden_shapes():
    queue = [
        body
        for method, path, status, body in phase3b_cases()
        if status == 202 and isinstance(body, dict) and "jobTypes" in body
    ][0]
    with pytest.raises(ValidationError):
        QueueResultOut.model_validate({**queue, "jobTypes": "metadata-v1"})
    with pytest.raises(ValidationError):
        QueueResultOut.model_validate({**queue, "jobsQueued": "2"})
    with pytest.raises(ValidationError):
        QueueResultOut.model_validate(
            {key: value for key, value in queue.items() if key != "assets"}
        )
    with pytest.raises(ValidationError):
        QueueResultOut.model_validate({**queue, "surprise": 1})


# ---------------------------------------------------------------------------
# Phase 4: face-operation results (rename/merge/move) and the storage
# verify report (VerifyOut / VerifyErrorOut / Pending202Out).
# ---------------------------------------------------------------------------


def phase4_cases() -> list[tuple[str, str, int, dict | None]]:
    if not PHASE4.exists():
        pytest.skip(f"golden fixture {PHASE4.name} not recorded yet")
    return [
        (case["method"], case["path"], case["status"], case["body"])
        for case in json.loads(PHASE4.read_text())["cases"]
    ]


def phase4_success_bodies(status: int, key: str) -> list[dict]:
    return [
        body
        for method, path, case_status, body in phase4_cases()
        if case_status == status and isinstance(body, dict) and key in body
    ]


def test_person_rename_model_round_trips():
    """Every recorded rename result (fresh and idempotent replay) round-trips
    with the exact wire key set, and the replay echoes the stored result."""
    bodies = [
        body
        for method, path, status, body in phase4_cases()
        if status == 200
        and method == "PATCH"
        and path.startswith("/people/")
        and "displayName" in body
    ]
    assert len(bodies) == 2, "expected a rename and its replay"
    for body in bodies:
        instance = assert_round_trip(PersonRenameOut, body)
        assert set(instance.model_dump(by_alias=True)) == set(body)
    assert bodies[0] == bodies[1], "idempotent replay must return the stored result"


def test_person_merge_model_round_trips():
    """The merge result carries the target, the merged source, and the face
    count, and never echoes a request-only field."""
    bodies = phase4_success_bodies(200, "mergedPersonId")
    assert len(bodies) == 2, "expected a merge and its replay"
    for body in bodies:
        instance = assert_round_trip(PersonMergeOut, body)
        assert set(instance.model_dump(by_alias=True)) == set(body)
        assert body["personId"] != body["mergedPersonId"]
    assert bodies[0] == bodies[1]


def test_face_move_model_round_trips():
    """The face-move result reports the destination person, the face count,
    and whether a person was created; the request's targetPersonId is never
    echoed (the client reads the destination back from personId)."""
    bodies = phase4_success_bodies(200, "createdPerson")
    assert len(bodies) == 4, "expected two moves and their replays"
    for body in bodies:
        instance = assert_round_trip(FaceMoveOut, body)
        assert set(instance.model_dump(by_alias=True)) == set(body)
        assert "targetPersonId" not in body
    created = [b for b in bodies if b["createdPerson"]]
    assert created and all(b["movedFaces"] == 1 for b in bodies)
    assert all(b["createdPerson"] is False for b in bodies if not b["createdPerson"])


def test_verify_model_round_trips():
    """The verify report's two variants (size-only and full sha256) round-trip
    with the exact key set, empty error list, and matched check counts."""
    bodies = phase4_success_bodies(200, "verification")
    assert len(bodies) == 2, "expected a size and a full verify"
    for body in bodies:
        instance = assert_round_trip(VerifyOut, body)
        assert set(instance.model_dump(by_alias=True)) == set(body)
        assert body["errors"] == []
        assert body["assetsChecked"] == body["blobsChecked"]
    assert {b["verification"] for b in bodies} == {"size", "sha256"}


def test_pending_202_model_round_trips():
    """The 202 body of the binary derivative endpoints is a single
    ``pending`` status; the model is documentation-only (the handler emits
    the JSONResponse directly) but must still accept the exact wire shape."""
    bodies = [
        body
        for method, path, status, body in phase4_cases()
        if status == 202 and isinstance(body, dict) and "status" in body
    ]
    assert bodies, "no 202 pending bodies recorded in the phase4 golden"
    for body in bodies:
        instance = assert_round_trip(Pending202Out, body)
        assert set(instance.model_dump(by_alias=True)) == set(body)
        assert body["status"] == "pending"


def test_verify_error_model_rejects_forbidden_shapes():
    good = {"key": "originals/00000000-0000-0000-0000-000000000081/x", "error": "size mismatch"}
    assert_round_trip(VerifyErrorOut, good)
    with pytest.raises(ValidationError):
        VerifyErrorOut.model_validate({**good, "surprise": 1})
    with pytest.raises(ValidationError):
        VerifyErrorOut.model_validate({"key": good["key"]})
    with pytest.raises(ValidationError):
        VerifyErrorOut.model_validate({**good, "key": 1})
    with pytest.raises(ValidationError):
        VerifyErrorOut.model_validate({**good, "error": None})


def test_verify_model_rejects_forbidden_shapes():
    body = phase4_success_bodies(200, "verification")[0]
    with pytest.raises(ValidationError):
        VerifyOut.model_validate({**body, "surprise": 1})
    with pytest.raises(ValidationError):
        VerifyOut.model_validate({**body, "verification": "md5"})
    with pytest.raises(ValidationError):
        VerifyOut.model_validate({**body, "assetsChecked": "4"})
    with pytest.raises(ValidationError):
        VerifyOut.model_validate({**body, "blobsChecked": 1.5})
    with pytest.raises(ValidationError):
        VerifyOut.model_validate(
            {key: value for key, value in body.items() if key != "errors"}
        )
    with pytest.raises(ValidationError):
        VerifyOut.model_validate(
            {**body, "errors": [{"key": "k"}]}  # missing the error field
        )
    with pytest.raises(ValidationError):
        VerifyOut.model_validate(
            {**body, "errors": [{"key": "k", "error": "e", "surprise": 1}]}
        )


def test_person_rename_model_rejects_forbidden_shapes():
    body = phase4_success_bodies(200, "displayName")[0]
    with pytest.raises(ValidationError):
        PersonRenameOut.model_validate({**body, "surprise": 1})
    with pytest.raises(ValidationError):
        PersonRenameOut.model_validate({**body, "personId": "not-a-uuid"})
    with pytest.raises(ValidationError):
        PersonRenameOut.model_validate({**body, "operationId": "not-a-uuid"})
    with pytest.raises(ValidationError):
        PersonRenameOut.model_validate(
            {key: value for key, value in body.items() if key != "displayName"}
        )
    with pytest.raises(ValidationError):
        PersonRenameOut.model_validate({**body, "personId": 1})


def test_person_merge_model_rejects_forbidden_shapes():
    body = phase4_success_bodies(200, "mergedPersonId")[0]
    with pytest.raises(ValidationError):
        PersonMergeOut.model_validate({**body, "surprise": 1})
    with pytest.raises(ValidationError):
        PersonMergeOut.model_validate({**body, "mergedPersonId": "not-a-uuid"})
    with pytest.raises(ValidationError):
        PersonMergeOut.model_validate({**body, "movedFaces": "1"})
    with pytest.raises(ValidationError):
        PersonMergeOut.model_validate(
            {key: value for key, value in body.items() if key != "movedFaces"}
        )


def test_face_move_model_rejects_forbidden_shapes():
    body = phase4_success_bodies(200, "createdPerson")[0]
    with pytest.raises(ValidationError):
        FaceMoveOut.model_validate({**body, "surprise": 1})
    with pytest.raises(ValidationError):
        FaceMoveOut.model_validate({**body, "personId": "not-a-uuid"})
    with pytest.raises(ValidationError):
        FaceMoveOut.model_validate({**body, "createdPerson": "false"})
    with pytest.raises(ValidationError):
        FaceMoveOut.model_validate({**body, "movedFaces": 1.5})
    with pytest.raises(ValidationError):
        FaceMoveOut.model_validate(
            {key: value for key, value in body.items() if key != "createdPerson"}
        )
    # The request-only targetPersonId is not part of the result document.
    with pytest.raises(ValidationError):
        FaceMoveOut.model_validate({**body, "targetPersonId": "not-a-uuid"})


def test_pending_202_model_rejects_forbidden_shapes():
    assert_round_trip(Pending202Out, {"status": "pending"})
    with pytest.raises(ValidationError):
        Pending202Out.model_validate({"status": "ready"})
    with pytest.raises(ValidationError):
        Pending202Out.model_validate({})
    with pytest.raises(ValidationError):
        Pending202Out.model_validate({"status": "pending", "surprise": 1})
