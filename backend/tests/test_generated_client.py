"""Unit tests for the generated Python API client (docs/openapi-codegen-plan.md
Phase 6; no services required).

``photo_server/generated`` is the committed output of @hey-api/openapi-python
driven from the checked-in spec — the same spec the web client generates
from. These tests pin the part of that artifact the ``photo-upload`` CLI
depends on:

- the generated models validate and round-trip every wire body the CLI
  consumes (the phase 2 golden fixture), so a spec/model regression fails
  here even on a machine without Node;
- the generated models agree with the server's own ``api_schemas`` models on
  those same bodies — both derive from one spec, so any divergence is a
  generator or spec bug;
- the CLI's upload flow works end-to-end through the typed models against an
  httpx.MockTransport serving the golden bodies, and fails loudly (contract
  violation) when a response stops matching the spec.
"""

import json
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError
from test_api_schemas import assert_round_trip, normalize

from photo_server import api_schemas as s
from photo_server.generated import pydantic_gen as g

FIXTURES = Path(__file__).parent / "fixtures" / "api_golden"
PHASE2 = FIXTURES / "phase2.json"
BATCH = "787bbcfd-cf82-5f88-8254-43cc9270430a"


def _cases() -> list[dict]:
    return json.loads(PHASE2.read_text())["cases"]


def _golden_case(method: str, path: str, status: int | None = None) -> dict:
    for case in _cases():
        if case["method"] == method and case["path"] == path:
            if status is None or case["status"] == status:
                return case
    raise AssertionError(f"no {method} {path} (status {status}) in phase 2 fixture")


def _is_list_case(case: dict) -> bool:
    return case["method"] == "GET" and case["path"].split("?")[0] == "/upload-batches"


def _single_case_model(case: dict):
    """The (server, generated) model pair for one non-list 2xx upload case."""
    method, path = case["method"], case["path"]
    if case["status"] >= 300:
        return None
    if method == "POST" and path == "/upload-batches":
        return s.UploadBatchOut, g.UploadBatchOut
    if path == "/upload-queue":
        return s.UploadQueueStatusOut, g.UploadQueueStatusOut
    if path == "/health":
        return s.HealthOut, g.HealthOut
    if "/files/" in path and method == "PUT":
        return s.UploadFileReceipt, g.UploadFileReceipt
    if method == "DELETE" and path.startswith("/upload-batches/"):
        return s.BatchAbandonedOut, g.BatchAbandonedOut
    if path.startswith("/upload-batches/"):  # GET, seal, retry
        return s.UploadBatchOut, g.UploadBatchOut
    return None


def test_generated_models_round_trip_cli_wire_bodies():
    """Every 2xx body the CLI could consume validates through the generated
    models and round-trips identically (golden phase 2)."""
    checked = 0
    for case in _cases():
        if case["status"] >= 300:
            continue
        method, path = case["method"], case["path"]
        if _is_list_case(case):
            for item in case["body"]:
                assert_round_trip(g.UploadBatchOut, item)
            checked += len(case["body"])
            continue
        pair = _single_case_model(case)
        assert pair is not None, f"no model mapping for {method} {path}"
        assert_round_trip(pair[1], case["body"])
        checked += 1
    assert checked >= 20, "phase 2 fixture no longer covers the upload flow"


def test_generated_and_server_models_agree():
    """The generated models and the server's api_schemas models accept the
    same bodies and round-trip them identically (one spec, two model
    families). The list endpoint's container is trivially equal, so its
    items are checked against both families individually."""
    for case in _cases():
        if case["status"] >= 300:
            continue
        if _is_list_case(case):
            for item in case["body"]:
                assert_round_trip(s.UploadBatchOut, item)
                assert_round_trip(g.UploadBatchOut, item)
            continue
        pair = _single_case_model(case)
        if pair is None:
            continue
        assert_round_trip(pair[0], case["body"])
        assert_round_trip(pair[1], case["body"])


def _first_create_body() -> dict:
    return _golden_case("POST", "/upload-batches", 201)["body"]


def test_generated_and_server_models_reject_together():
    """Contract breaks (extra key, missing key, wrong type) are rejected by
    both model families — the generated client fails as loudly as the
    server would 500."""
    body = _first_create_body()

    broken = {**body, "unexpected": 1}
    with pytest.raises(ValidationError):
        s.UploadBatchOut.model_validate(broken)
    with pytest.raises(ValidationError):
        g.UploadBatchOut.model_validate(broken)

    missing = {key: value for key, value in body.items() if key != "files"}
    with pytest.raises(ValidationError):
        s.UploadBatchOut.model_validate(missing)
    with pytest.raises(ValidationError):
        g.UploadBatchOut.model_validate(missing)

    wrong_type = {**body, "status": 42}
    with pytest.raises(ValidationError):
        s.UploadBatchOut.model_validate(wrong_type)
    with pytest.raises(ValidationError):
        g.UploadBatchOut.model_validate(wrong_type)


def test_generated_request_models_validate_the_cli_declaration():
    """The CLI validates its outgoing declaration dict through the generated
    request model (the same model the server validates against): a spec-
    faithful dict round-trips, and drifts (missing key, zero size, empty
    files) are rejected before anything hits the wire."""
    batch_id = UUID(BATCH)
    payload = {
        "batchId": str(batch_id),
        "files": [
            {"path": "a/one.jpg", "sizeBytes": 1234, "mimeType": "image/jpeg"},
            {"path": "a/one.xmp", "sizeBytes": 77, "mimeType": "text/x-adobe"},
        ],
    }
    model = g.UploadBatchRequest.model_validate(payload)
    assert model.batch_id == batch_id
    assert model.files[0].size_bytes == 1234
    assert normalize(model.model_dump(mode="json", by_alias=True)) == normalize(payload)

    broken = {"batchId": str(batch_id), "files": [{"path": "a/one.jpg", "sizeBytes": 0}]}
    with pytest.raises(ValidationError):
        g.UploadBatchRequest.model_validate(broken)
    with pytest.raises(ValidationError):
        g.UploadBatchRequest.model_validate({"files": []})


def _mock_client(monkeypatch, bodies: dict, calls: list):
    """Replace the CLI's httpx.Client with a MockTransport-backed client and
    record (method, path) for every request it makes."""

    def handler(request: httpx.Request) -> httpx.Response:
        key = (request.method, request.url.path)
        if key not in bodies:
            raise AssertionError(f"unexpected request {request.method} {request.url.path}")
        status, payload = bodies[key]
        return httpx.Response(status, json=payload)

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client  # captured before the patch below

    class _Client:
        def __init__(self, base_url=None, timeout=None):
            self._inner = real_client(base_url=base_url, transport=transport)

        def post(self, url, **kwargs):
            calls.append(("POST", url))
            return self._inner.post(url, **kwargs)

        def put(self, url, **kwargs):
            calls.append(("PUT", url))
            return self._inner.put(url, **kwargs)

        def get(self, url, **kwargs):
            calls.append(("GET", url))
            return self._inner.get(url, **kwargs)

        def close(self):
            self._inner.close()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    monkeypatch.setattr("photo_server.upload_client.httpx.Client", _Client)


def _seed_files(tmp_path) -> Path:
    """The phase 2 fixture's first batch, verbatim: three originals, a
    skipped JPEG companion, and an XMP sidecar."""
    root = tmp_path / "source"
    (root / "a").mkdir(parents=True)
    for name in ("one.jpg", "one.xmp", "three.arw", "three.jpg", "two.jpg"):
        (root / "a" / name).write_bytes(b"golden-" + name.encode())
    return root


def _flow_arguments(tmp_path):
    return (
        "http://api.test",
        _seed_files(tmp_path),
        [f"a/{name}" for name in ("one.jpg", "one.xmp", "three.arw", "three.jpg", "two.jpg")],
        False,
        1,
        UUID(BATCH),
    )


def _scenario_bodies(get_body=None) -> tuple[dict, dict]:
    """(bodies, create_body): the first phase 2 scenario — create, the four
    required file PUTs, the seal; plus a GET whose body may be overridden."""
    create = _golden_case("POST", "/upload-batches", 201)["body"]
    seal = _golden_case("POST", f"/upload-batches/{BATCH}/seal", 202)["body"]
    put_paths = [f["uploadUrl"] for f in create["files"] if f["uploadUrl"]]
    assert len(put_paths) == 4
    bodies = {
        ("POST", "/upload-batches"): (201, create),
        ("POST", f"/upload-batches/{BATCH}/seal"): (202, seal),
    }
    for path in put_paths:
        bodies[("PUT", path)] = (200, _golden_case("PUT", path, 200)["body"])
    if get_body is not None:
        bodies[("GET", f"/upload-batches/{BATCH}")] = (200, get_body)
    return bodies, create


def test_upload_cli_flow_validates_through_generated_models(tmp_path, monkeypatch):
    import photo_server.upload_client as upload_client

    bodies, create = _scenario_bodies()
    seal = _golden_case("POST", f"/upload-batches/{BATCH}/seal", 202)["body"]
    put_paths = [f["uploadUrl"] for f in create["files"] if f["uploadUrl"]]
    calls = []
    _mock_client(monkeypatch, bodies, calls)

    batch, raw = upload_client.upload(*_flow_arguments(tmp_path), wait=False)

    assert batch.status is g.UploadBatchOutStatus.QUEUED
    assert normalize(raw) == normalize(seal), "final stdout must be the raw wire JSON"
    assert ("POST", "/upload-batches") in calls
    assert [call for call in calls if call[0] == "PUT"] == [("PUT", p) for p in put_paths]
    assert not any(call[0] == "GET" for call in calls)


def test_upload_cli_wait_polls_until_complete(tmp_path, monkeypatch):
    import photo_server.upload_client as upload_client

    complete = next(
        case["body"]
        for case in _cases()
        if case["method"] == "GET"
        and case["path"] == f"/upload-batches/{BATCH}"
        and case["body"]["status"] == "complete"
    )
    bodies, _ = _scenario_bodies(get_body=complete)
    calls = []
    _mock_client(monkeypatch, bodies, calls)
    sleeps = []
    monkeypatch.setattr(
        "photo_server.upload_client.time.sleep", lambda seconds: sleeps.append(seconds)
    )

    batch, raw = upload_client.upload(*_flow_arguments(tmp_path), wait=True)

    assert batch.status is g.UploadBatchOutStatus.COMPLETE
    assert sleeps == [1], "one poll interval between the sealed batch and its completion"
    assert ("GET", f"/upload-batches/{BATCH}") in calls
    assert normalize(raw) == normalize(complete)


def test_upload_cli_fails_loud_on_contract_violation(tmp_path, monkeypatch):
    """A response that drifts from the spec (a key the models do not
    declare) is a contract violation, not a KeyError two frames later."""
    import photo_server.upload_client as upload_client

    seal = _golden_case("POST", f"/upload-batches/{BATCH}/seal", 202)["body"]
    bodies, _ = _scenario_bodies(get_body={**seal, "surprise": "the spec does not declare this"})
    calls = []
    _mock_client(monkeypatch, bodies, calls)
    sleeps = []
    monkeypatch.setattr(
        "photo_server.upload_client.time.sleep", lambda seconds: sleeps.append(seconds)
    )

    with pytest.raises(RuntimeError, match="violates the OpenAPI contract"):
        upload_client.upload(*_flow_arguments(tmp_path), wait=True)
