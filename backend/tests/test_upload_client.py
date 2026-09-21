"""Unit tests for the photo-upload CLI's contract handling (docs/
openapi-codegen-plan.md Phase 6; no services required).

The CLI validates every JSON exchange through the server's own Pydantic
models (``photo_server.api_schemas`` — the classes the routes validate
with and the source of the checked-in spec). These tests pin:

- the outgoing declaration: constructing the request model is the
  validation, its JSON dump is byte-faithful to the wire, and bad input
  (zero size, no files) is rejected before anything is sent;
- the bodies the CLI consumes: the phase 2 golden bodies validate through
  those models (the full round-trip of every 2xx body is
  ``test_api_schemas``' job, so here only the CLI's slice), and a body
  that drifts from the spec is a loud contract violation, not a
  ``KeyError`` mid-flow;
- the upload flow end-to-end against an httpx.MockTransport serving the
  golden bodies, including the wait/poll loop and the exact request bytes.
"""

import json
import mimetypes
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError
from test_api_schemas import assert_round_trip, normalize

from photo_server.api_schemas import (
    UploadBatchOut,
    UploadBatchRequest,
    UploadFileDeclaration,
    UploadFileReceipt,
)

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


def _cli_case_model(case: dict):
    """The model the CLI validates a golden case's body with (None for
    bodies the CLI never consumes: the list endpoint, queue, health)."""
    method, path = case["method"], case["path"]
    if case["status"] >= 300:
        return None
    if method == "POST" and path == "/upload-batches":
        return UploadBatchOut
    if method == "PUT" and "/files/" in path:
        return UploadFileReceipt
    if method == "POST" and path.endswith("/seal"):
        return UploadBatchOut
    if method == "GET" and path.startswith("/upload-batches/"):
        return UploadBatchOut
    return None


def test_cli_consumed_bodies_validate_through_server_models():
    """Every 2xx body the CLI consumes in the phase 2 fixture validates
    through the server's models and round-trips identically."""
    checked = 0
    for case in _cases():
        if case["status"] >= 300:
            continue
        model = _cli_case_model(case)
        if model is None:
            continue
        assert_round_trip(model, case["body"])
        checked += 1
    assert checked >= 5, "phase 2 fixture no longer covers the upload flow"


def test_cli_declaration_constructs_validates_and_dumps_to_wire_shape():
    """Constructing the request model is the declaration's validation (the
    same model the server validates the body against); the JSON dump is the
    exact wire spelling the CLI sends."""
    batch_id = UUID(BATCH)
    declaration = UploadBatchRequest(
        batch_id=batch_id,
        files=[
            UploadFileDeclaration(path="a/one.jpg", size_bytes=1234, mime_type="image/jpeg"),
            UploadFileDeclaration(path="a/one.xmp", size_bytes=77, mime_type="text/x-adobe"),
        ],
    )
    payload = declaration.model_dump(mode="json", by_alias=True)
    assert payload == {
        "batchId": BATCH,
        "files": [
            {"path": "a/one.jpg", "sizeBytes": 1234, "mimeType": "image/jpeg"},
            {"path": "a/one.xmp", "sizeBytes": 77, "mimeType": "text/x-adobe"},
        ],
    }

    # The wire spelling parses back (aliases, optional mimeType omitted).
    parsed = UploadBatchRequest.model_validate(
        {"batchId": BATCH, "files": [{"path": "a/one.jpg", "sizeBytes": 1234}]}
    )
    assert parsed.files[0].mime_type is None

    # Drifts are rejected before anything hits the wire.
    with pytest.raises(ValidationError):
        UploadFileDeclaration(path="a/one.jpg", size_bytes=0)
    with pytest.raises(ValidationError):
        UploadBatchRequest(batch_id=batch_id, files=[])


def _mock_client(monkeypatch, bodies: dict, calls: list):
    """Replace the CLI's httpx.Client with a MockTransport-backed client and
    record (method, path, content) for every request it makes."""

    def handler(request: httpx.Request) -> httpx.Response:
        key = (request.method, request.url.path)
        if key not in bodies:
            raise AssertionError(f"unexpected request {request.method} {request.url.path}")
        calls.append((request.method, request.url.path, request.content))
        status, payload = bodies[key]
        return httpx.Response(status, json=payload)

    transport = httpx.MockTransport(handler)
    real_client = httpx.Client  # captured before the patch below

    class _Client:
        def __init__(self, base_url=None, timeout=None):
            self._inner = real_client(base_url=base_url, transport=transport)

        def post(self, url, **kwargs):
            return self._inner.post(url, **kwargs)

        def put(self, url, **kwargs):
            return self._inner.put(url, **kwargs)

        def get(self, url, **kwargs):
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


def _expected_declaration(root: Path) -> dict:
    """The exact declaration the CLI builds for the seeded batch (sizes
    from the files, mimes from the standard library)."""
    files = []
    for name in ("one.jpg", "one.xmp", "three.arw", "three.jpg", "two.jpg"):
        files.append(
            {
                "path": f"a/{name}",
                "sizeBytes": (root / "a" / name).stat().st_size,
                "mimeType": mimetypes.guess_type(name)[0] or "application/octet-stream",
            }
        )
    return {"batchId": BATCH, "files": files}


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


def test_upload_cli_flow_sends_declared_bytes_and_validates_responses(tmp_path, monkeypatch):
    import photo_server.upload_client as upload_client

    args = _flow_arguments(tmp_path)
    root = args[1]
    bodies, create = _scenario_bodies()
    seal = _golden_case("POST", f"/upload-batches/{BATCH}/seal", 202)["body"]
    put_paths = [f["uploadUrl"] for f in create["files"] if f["uploadUrl"]]
    calls = []
    _mock_client(monkeypatch, bodies, calls)

    batch, raw = upload_client.upload(*args, wait=False)

    assert batch.status == "queued"
    assert normalize(raw) == normalize(seal), "final stdout must be the raw wire JSON"
    # The POST body is the request model's JSON dump, byte for byte
    # (httpx serializes json bodies compactly: allow_nan=False,
    # separators=(",", ":")).
    post = next(c for c in calls if c[0] == "POST" and c[1] == "/upload-batches")
    expected_bytes = json.dumps(
        _expected_declaration(root), ensure_ascii=False, allow_nan=False, separators=(",", ":")
    )
    assert post[2].decode() == expected_bytes
    assert [c[1] for c in calls if c[0] == "PUT"] == put_paths
    assert not any(c[0] == "GET" for c in calls)


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

    assert batch.status == "complete"
    assert sleeps == [1], "one poll interval between the sealed batch and its completion"
    assert any(c[0] == "GET" and c[1] == f"/upload-batches/{BATCH}" for c in calls)
    assert normalize(raw) == normalize(complete)


def test_upload_cli_fails_loud_on_contract_violation(tmp_path, monkeypatch):
    """A response that drifts from the spec (a key the models do not
    declare) is a contract violation, not a KeyError two frames later."""
    import photo_server.upload_client as upload_client

    seal = _golden_case("POST", f"/upload-batches/{BATCH}/seal", 202)["body"]
    bodies, _ = _scenario_bodies(get_body={**seal, "surprise": "the spec does not declare this"})
    _mock_client(monkeypatch, bodies, [])
    monkeypatch.setattr("photo_server.upload_client.time.sleep", lambda seconds: None)

    with pytest.raises(RuntimeError, match="violates the OpenAPI contract"):
        upload_client.upload(*_flow_arguments(tmp_path), wait=True)
