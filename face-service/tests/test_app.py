"""In-process face-service tests.

The analyzer is monkeypatched throughout: the dev venv has no
cv2/numpy/onnxruntime, so the suite runs without the GPU stack (FastAPI
TestClient against the real app). No service, database, or object store is
contacted; every fixture is a dummy generated in-process.
"""

import io
import math
import stat
import threading
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError

from face_service import app as face_app
from face_service.analyzer import ADAFACE_IDENTITY

HEALTH_URL = "/health"
ANALYZE_URL = "/v1/faces/analyze"
CT = {"content-type": "image/jpeg"}

CANNED_FACE = {
    "box": [0.1, 0.2, 0.3, 0.4],
    "confidence": 0.97,
    "embedding": [0.01414213562373095] * 512,
}


def dummy_jpeg() -> bytes:
    image = Image.new("RGB", (64, 64), (120, 80, 40))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    return buffer.getvalue()


def make_settings(**overrides):
    values = {
        "face_service_bind": "127.0.0.1:8901",
        "face_service_token": "",
        "face_models_dir": "/models",
        "face_detection_threshold": 0.8,
        "face_service_concurrency": 1,
        "face_tls_dir": None,
    }
    values.update(overrides)
    return face_app.Settings(**values)


class FaceStub:
    """Duck-typed stand-in for AdaFaceAnalyzer (no GPU stack in the dev venv)."""

    def __init__(self, settings):
        self.settings = settings
        self.runtime_label = "onnxruntime-test"
        self.analyzes: list[bytes] = []
        self.block_on = None  # a threading.Event set by tests to hold requests

    def analyze(self, jpeg: bytes):
        self.analyzes.append(jpeg)
        if self.block_on is not None:
            self.block_on.wait()
        return [
            {"box": list(CANNED_FACE["box"]), "confidence": CANNED_FACE["confidence"],
             "embedding": list(CANNED_FACE["embedding"])}
        ]


def build_app(monkeypatch, settings, *, queue_cap=face_app.QUEUE_CAP):
    monkeypatch.setattr(face_app, "AdaFaceAnalyzer", FaceStub)
    return face_app.create_app(settings, queue_cap=queue_cap)


def wait_health(client, **fields):
    """Poll /health until every field in ``fields`` matches (or time out)."""
    deadline = time.monotonic() + 15
    body: dict = {}
    while time.monotonic() < deadline:
        body = client.get(HEALTH_URL).json()
        if all(body.get(key) == value for key, value in fields.items()):
            return body
        time.sleep(0.01)
    raise AssertionError(f"/health never reached {fields}; last body: {body}")


def threaded_post(client, results, tag):
    def run():
        results[tag] = client.post(ANALYZE_URL, content=dummy_jpeg(), headers=CT)

    return threading.Thread(target=run, daemon=True)


def test_settings_defaults():
    settings = face_app.Settings()
    assert settings.face_service_bind == "0.0.0.0:8901"
    assert settings.face_service_token == ""
    assert settings.face_detection_threshold == 0.8
    assert settings.face_service_concurrency == 1
    assert settings.face_tls_dir is None


def test_concurrency_is_bounded(monkeypatch):
    for bad in (0, 5):
        with pytest.raises(ValidationError):
            make_settings(face_service_concurrency=bad)


def test_health_reports_starting_then_ready(monkeypatch):
    gate = threading.Event()

    class SlowStart(FaceStub):
        def __init__(self, settings):
            super().__init__(settings)
            gate.wait(10)

    monkeypatch.setattr(face_app, "AdaFaceAnalyzer", SlowStart)
    app = face_app.create_app(make_settings())
    with TestClient(app) as client:
        response = client.get(HEALTH_URL)
        assert response.status_code == 503
        assert response.json() == {"status": "starting"}
        response = client.post(ANALYZE_URL, content=dummy_jpeg(), headers=CT)
        assert response.status_code == 503
        assert response.json() == {"status": "starting"}

        gate.set()
        wait_health(client, status="ok")
        response = client.post(ANALYZE_URL, content=dummy_jpeg(), headers=CT)
        assert response.status_code == 200


def test_health_shape(monkeypatch):
    app = build_app(monkeypatch, make_settings())
    with TestClient(app) as client:
        wait_health(client, status="ok")
        body = client.get(HEALTH_URL).json()
    assert body == {
        "status": "ok",
        "models": {
            "faceDetector": "yunet-2023mar",
            "faceEmbedding": {
                "name": ADAFACE_IDENTITY["name"],
                "revision": ADAFACE_IDENTITY["revision"],
                "weightsSha256": ADAFACE_IDENTITY["weights_sha256"],
                "runtime": "onnxruntime-test",
            },
        },
        "detectionThreshold": 0.8,
        "concurrency": 1,
        "inFlight": 0,
        "queueDepth": 0,
    }


def test_bearer_auth(monkeypatch):
    settings = make_settings(face_service_token="sekrit")
    app = build_app(monkeypatch, settings)
    auth = {"Authorization": "Bearer sekrit"}
    with TestClient(app) as client:
        # /health is 503 "starting" until ready, then 401 without the token:
        # poll with the token until the service answers 200.
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if client.get(HEALTH_URL, headers=auth).status_code == 200:
                break
            time.sleep(0.01)
        else:
            pytest.fail("service never became ready")
        response = client.get(HEALTH_URL)
        assert response.status_code == 401
        assert response.json()["error"]["message"]
        assert client.get(HEALTH_URL, headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.get(HEALTH_URL, headers=auth).status_code == 200
        assert (
            client.post(ANALYZE_URL, content=dummy_jpeg(), headers=CT).status_code == 401
        )
        ok = client.post(ANALYZE_URL, content=dummy_jpeg(), headers={**CT, **auth})
        assert ok.status_code == 200


def test_wrong_content_type_rejected(monkeypatch):
    app = build_app(monkeypatch, make_settings())
    with TestClient(app) as client:
        wait_health(client, status="ok")
        response = client.post(
            ANALYZE_URL, content=dummy_jpeg(), headers={"content-type": "application/octet-stream"}
        )
    assert response.status_code == 415
    assert response.json()["error"]["message"]


def test_oversized_body_rejected(monkeypatch):
    app = build_app(monkeypatch, make_settings())
    huge = b"\xff\xd8" + b"\x00" * face_app.MAX_BODY_BYTES  # over the 16 MB cap
    with TestClient(app) as client:
        wait_health(client, status="ok")
        response = client.post(ANALYZE_URL, content=huge, headers=CT)
    assert response.status_code == 413
    assert response.json()["error"]["message"]


def test_undecodable_body_rejected(monkeypatch):
    app = build_app(monkeypatch, make_settings())
    with TestClient(app) as client:
        wait_health(client, status="ok")
        response = client.post(ANALYZE_URL, content=b"\x00\x01not a jpeg", headers=CT)
    assert response.status_code == 422
    assert response.json()["error"]["message"]


def test_analyzer_error_maps_to_500(monkeypatch):
    class Exploding(FaceStub):
        def analyze(self, jpeg: bytes):
            raise RuntimeError("model exploded")

    monkeypatch.setattr(face_app, "AdaFaceAnalyzer", Exploding)
    app = face_app.create_app(make_settings())
    with TestClient(app) as client:
        wait_health(client, status="ok")
        response = client.post(ANALYZE_URL, content=dummy_jpeg(), headers=CT)
    assert response.status_code == 500
    assert "model exploded" in response.json()["error"]["message"]


def test_requests_are_serialized_single_flight(monkeypatch):
    gate = threading.Event()
    app = build_app(monkeypatch, make_settings())
    results: dict = {}
    with TestClient(app) as client:
        wait_health(client, status="ok")
        app.state.executor.analyzers[0].block_on = gate

        first = threaded_post(client, results, "a")
        first.start()
        wait_health(client, inFlight=1)
        second = threaded_post(client, results, "b")
        second.start()
        # The second request must wait in the queue, not run in parallel.
        wait_health(client, inFlight=1, queueDepth=1)

        gate.set()
        first.join(timeout=15)
        second.join(timeout=15)
        assert not first.is_alive() and not second.is_alive()
        assert results["a"].status_code == 200
        assert results["b"].status_code == 200
        assert len(app.state.executor.analyzers[0].analyzes) == 2
        wait_health(client, inFlight=0, queueDepth=0)


def test_queue_saturation_returns_429_with_retry_after(monkeypatch):
    gate = threading.Event()
    app = build_app(monkeypatch, make_settings(), queue_cap=2)
    results: dict = {}
    with TestClient(app) as client:
        wait_health(client, status="ok")
        app.state.executor.analyzers[0].block_on = gate

        first = threaded_post(client, results, "a")
        first.start()
        wait_health(client, inFlight=1)
        second = threaded_post(client, results, "b")
        second.start()
        wait_health(client, queueDepth=1)
        third = threaded_post(client, results, "c")
        third.start()
        wait_health(client, queueDepth=2)  # queue full
        fourth = threaded_post(client, results, "d")
        fourth.start()
        fourth.join(timeout=15)

        assert results["d"].status_code == 429
        assert int(results["d"].headers["Retry-After"]) >= 1
        assert results["d"].json()["error"]["message"]

        gate.set()
        for thread in (first, second, third):
            thread.join(timeout=15)
        for tag in ("a", "b", "c"):
            assert results[tag].status_code == 200
        wait_health(client, inFlight=0, queueDepth=0)


def test_analyze_response_shape(monkeypatch):
    app = build_app(monkeypatch, make_settings())
    with TestClient(app) as client:
        wait_health(client, status="ok")
        response = client.post(ANALYZE_URL, content=dummy_jpeg(), headers=CT)
    assert response.status_code == 200
    body = response.json()
    # Exactly the shape AdaFaceAnalyzer.analyze() returns, wrapped once.
    assert body == {"schemaVersion": 1, "faces": [CANNED_FACE]}
    face = body["faces"][0]
    assert len(face["embedding"]) == 512
    assert all(math.isfinite(value) for value in face["embedding"])
    assert all(0.0 <= value <= 1.0 for value in face["box"])


def test_tls_certs_are_generated_and_reused(tmp_path):
    pytest.importorskip("cryptography")
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    tls_dir = tmp_path / "tls"
    cert, key = face_app.ensure_tls(tls_dir)
    assert (tls_dir / "ca.crt").is_file() and (tls_dir / "ca.key").is_file()
    assert cert == tls_dir / "server.crt" and key == tls_dir / "server.key"
    assert stat.S_IMODE(key.stat().st_mode) == 0o600

    # A second call reuses the persisted pair instead of regenerating it.
    before = (cert.read_bytes(), key.read_bytes())
    cert2, key2 = face_app.ensure_tls(tls_dir)
    assert (cert2.read_bytes(), key2.read_bytes()) == before

    ca = x509.load_pem_x509_certificate((tls_dir / "ca.crt").read_bytes())
    server = x509.load_pem_x509_certificate(cert.read_bytes())
    server.verify_directly_issued_by(ca)
    san = server.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    names = san.get_values_for_type(x509.DNSName)
    assert "localhost" in names and "face-service" in names
    load_pem_private_key(key.read_bytes(), password=None)
