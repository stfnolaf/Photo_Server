"""Wire-level tests for the face-service client (Phase 2B of the
AI-service-split plan).

The face-service is simulated with an ``httpx.MockTransport`` forced through
a patched ``httpx.Client``: no AI service is started and no request leaves
the process.
"""

import json

import httpx
import pytest

from photo_server.config import Settings
from photo_server.face_client import (
    ADAFACE_IDENTITY,
    FaceServiceError,
    FaceServiceUnavailable,
    RemoteFaceAnalyzer,
)

JPEG = b"\xff\xd8\xff\xe0dummy-jpeg-bytes"

FACES = [
    {
        "box": [0.1, 0.1, 0.2, 0.2],
        "confidence": 0.97,
        "embedding": [1.0, 0.0, -0.25],
    }
]


def make_settings(**overrides) -> Settings:
    values = dict(
        _env_file=None,
        s3_endpoint="http://s3:9000",
        database_url="postgresql+psycopg://test:pw@localhost/test",
        face_service_url="http://face:8901",
        face_service_token="tok-test",
        face_service_timeout=120,
    )
    values.update(overrides)
    return Settings(**values)


def health_payload(**changes):
    payload = {
        "status": "ok",
        "models": {
            "faceDetector": "yunet-2023mar",
            "faceEmbedding": {
                "name": ADAFACE_IDENTITY["name"],
                "revision": ADAFACE_IDENTITY["revision"],
                "weightsSha256": ADAFACE_IDENTITY["weights_sha256"],
                "runtime": "onnxruntime-1.23.2",
            },
        },
        "detectionThreshold": 0.8,
        "concurrency": 1,
        "inFlight": 1,
        "queueDepth": 3,
    }
    payload["models"]["faceEmbedding"].update(changes)
    return payload


def analyze_payload(faces=FACES):
    # Serialized by hand (and not via ``json=``) so the NaN case can travel
    # on the wire the way a real (sloppy) provider response would: httpx's
    # ``json=`` encoding is strict and refuses non-finite values outright.
    body = json.dumps({"schemaVersion": 1, "faces": faces}).encode()
    return httpx.Response(
        200, content=body, headers={"Content-Type": "application/json"}
    )


def scripted(handler):
    """Run ``handler`` per request and record every request made."""

    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        result = handler(request, len(requests))
        if isinstance(result, BaseException):
            raise result
        return result

    return handle, requests


def patch_transport(monkeypatch, handle):
    real_client = httpx.Client

    class _Client(real_client):
        def __init__(self, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handle)
            super().__init__(**kwargs)

    monkeypatch.setattr(httpx, "Client", _Client)


def make_analyzer(settings=None):
    return RemoteFaceAnalyzer(settings or make_settings())


# --- request shape ----------------------------------------------------------


def ok_health():
    return httpx.Response(200, json=health_payload())


def test_analyze_request_shape_bearer_and_raw_jpeg(monkeypatch):
    handle, requests = scripted(
        lambda request, _n: ok_health() if request.method == "GET" else analyze_payload()
    )
    patch_transport(monkeypatch, handle)
    analyzer = make_analyzer()
    faces = analyzer.analyze(JPEG)

    assert faces == FACES
    assert [request.method for request in requests] == ["GET", "POST"]
    assert requests[0].url.path == "/health"
    assert requests[1].url.path == "/v1/faces/analyze"
    for request in requests:
        assert request.headers["Authorization"] == "Bearer tok-test"
    assert requests[1].headers["Content-Type"] == "image/jpeg"
    # The worker's prepare_jpeg output goes over the wire raw: the service
    # needs no resize policy of its own.
    assert requests[1].content == JPEG
    # The verified runtime label from the health payload is what the worker
    # records in the artifact's models.faceEmbedding.runtime.
    assert analyzer.model_version == "onnxruntime-1.23.2"


def test_analyze_without_token_sends_no_bearer(monkeypatch):
    handle, requests = scripted(
        lambda request, _n: ok_health() if request.method == "GET" else analyze_payload()
    )
    patch_transport(monkeypatch, handle)
    make_analyzer(make_settings(face_service_token="")).analyze(JPEG)
    for request in requests:
        assert "Authorization" not in request.headers


def test_health_returns_payload_and_runtime(monkeypatch):
    handle, _requests = scripted(lambda request, _n: ok_health())
    patch_transport(monkeypatch, handle)
    analyzer = make_analyzer()
    payload = analyzer.health()
    assert payload["status"] == "ok"
    assert payload["queueDepth"] == 3
    assert analyzer.model_version == "onnxruntime-1.23.2"


# --- the identity guard (client side of the dual pin) ------------------------


@pytest.mark.parametrize("changes", [
    {"name": "adaface-ir101-other"},
    {"revision": "0000000000000000000000000000000000000000"},
    {"weightsSha256": "0" * 64},
])
def test_identity_mismatch_is_unavailable(monkeypatch, changes):
    handle, _requests = scripted(
        lambda request, _n: httpx.Response(200, json=health_payload(**changes))
    )
    patch_transport(monkeypatch, handle)
    with pytest.raises(FaceServiceUnavailable, match="embedding model mismatch"):
        make_analyzer().health()


def test_health_without_identity_is_unavailable(monkeypatch):
    def handler(request, _n):
        payload = health_payload()
        del payload["models"]["faceEmbedding"]
        return httpx.Response(200, json=payload)

    handle, _requests = scripted(handler)
    patch_transport(monkeypatch, handle)
    with pytest.raises(FaceServiceUnavailable, match="no faceEmbedding identity"):
        make_analyzer().health()


def test_health_status_not_ok_is_an_error(monkeypatch):
    handle, _requests = scripted(
        lambda request, _n: httpx.Response(200, json={"status": "degraded"})
    )
    patch_transport(monkeypatch, handle)
    with pytest.raises(FaceServiceError, match="unusable shape"):
        make_analyzer().health()


# --- transport and status classification -------------------------------------


@pytest.mark.parametrize(
    "response,expected",
    [
        (httpx.Response(429, headers={"Retry-After": "2"}, json={"error": {"message": "saturated"}}),
         FaceServiceUnavailable),
        (httpx.Response(502, json={"error": {"message": "bad gateway"}}), FaceServiceUnavailable),
        (httpx.Response(503, json={"status": "starting"}), FaceServiceUnavailable),
        (httpx.Response(504, json={"error": {"message": "gateway timeout"}}), FaceServiceUnavailable),
    ],
)
def test_unavailable_statuses(monkeypatch, response, expected):
    handle, _requests = scripted(lambda request, _n: response)
    patch_transport(monkeypatch, handle)
    with pytest.raises(expected):
        make_analyzer().analyze(JPEG)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, json={"error": {"message": "Invalid or missing bearer token"}}),
        httpx.Response(413, json={"error": {"message": "Request body too large (limit is 16MB)"}}),
        httpx.Response(415, json={"error": {"message": "Unsupported media type (want image/jpeg)"}}),
        httpx.Response(422, json={"error": {"message": "JPEG could not be decoded"}}),
        httpx.Response(500, json={"error": {"message": "simulated analyzer failure"}}),
    ],
)
def test_request_rejection_statuses(monkeypatch, response):
    handle, _requests = scripted(lambda request, _n: response)
    patch_transport(monkeypatch, handle)
    with pytest.raises(FaceServiceError):
        make_analyzer().analyze(JPEG)


def test_connection_error_is_unavailable(monkeypatch):
    def handler(request, _n):
        raise httpx.ConnectError("connection refused", request=request)

    handle, _requests = scripted(handler)
    patch_transport(monkeypatch, handle)
    with pytest.raises(FaceServiceUnavailable, match="unreachable"):
        make_analyzer().analyze(JPEG)


def test_timeout_is_unavailable(monkeypatch):
    def handler(request, _n):
        raise httpx.ReadTimeout("timed out", request=request)

    handle, _requests = scripted(handler)
    patch_transport(monkeypatch, handle)
    with pytest.raises(FaceServiceUnavailable, match="unreachable"):
        make_analyzer().health()


def test_unconfigured_url_is_unavailable_without_a_request(monkeypatch):
    handle, requests = scripted(lambda request, _n: health_payload())
    patch_transport(monkeypatch, handle)
    with pytest.raises(FaceServiceUnavailable, match="PHOTO_FACE_SERVICE_URL is not set"):
        make_analyzer(make_settings(face_service_url="")).analyze(JPEG)
    assert requests == []


def test_trailing_slash_in_url_is_normalised(monkeypatch):
    handle, requests = scripted(
        lambda request, _n: ok_health() if request.method == "GET" else analyze_payload()
    )
    patch_transport(monkeypatch, handle)
    make_analyzer(make_settings(face_service_url="http://face:8901/")).analyze(JPEG)
    assert all(str(request.url).startswith("http://face:8901/") for request in requests)


# --- response-shape validation ------------------------------------------------


def test_non_json_response_is_an_error(monkeypatch):
    def handler(request, _n):
        if request.method == "GET":
            return httpx.Response(200, json=health_payload())
        return httpx.Response(200, content=b"<html>not json</html>",
                              headers={"Content-Type": "text/html"})

    handle, _requests = scripted(handler)
    patch_transport(monkeypatch, handle)
    with pytest.raises(FaceServiceError, match="not JSON"):
        make_analyzer().analyze(JPEG)


@pytest.mark.parametrize("faces", [
    {"unexpected": True},
    "nope",
    42,
    ["not-a-dict"],
    [{"box": [0.1, 0.1, 0.2], "confidence": 0.9, "embedding": [1.0]}],
    [{"box": [0.1, 0.1, 0.2, 0.2], "confidence": "high", "embedding": [1.0]}],
    [{"box": [0.1, 0.1, 0.2, 0.2], "confidence": 0.9, "embedding": []}],
    [{"box": [0.1, 0.1, 0.2, 0.2], "confidence": 0.9, "embedding": [float("nan")]}],
])
def test_unusable_faces_shape_is_an_error(monkeypatch, faces):
    handle, _requests = scripted(
        lambda request, _n: ok_health() if request.method == "GET" else analyze_payload(faces)
    )
    patch_transport(monkeypatch, handle)
    with pytest.raises(FaceServiceError, match="unusable faces shape|invalid"):
        make_analyzer().analyze(JPEG)


def test_error_classes_form_a_hierarchy():
    assert issubclass(FaceServiceUnavailable, FaceServiceError)
