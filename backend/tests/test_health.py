import json
import logging
from pathlib import Path
from uuid import UUID

from fastapi.testclient import TestClient

import photo_server.api as api_module
from photo_server.api import create_app
from photo_server.app_logging import log_event
from photo_server.config import Settings


class _Connection:
    def __init__(self, broken=False):
        self.broken = broken

    def __enter__(self):
        if self.broken:
            raise RuntimeError("database password=do-not-log")
        return self

    def __exit__(self, *args):
        return False

    def execute(self, statement):
        return None


class _Catalog:
    def __init__(self, broken=False):
        self.engine = self
        self.broken = broken

    def connect(self):
        return _Connection(self.broken)

    def dispose(self):
        return None

    def counts(self):
        return {"assets": 0, "blobs": 0}

    def queue_counts(self):
        return {
            "uploadBatchesQueued": 0,
            "onboardingPending": 0,
            "onboardingRunning": 0,
            "onboardingFailed": 0,
            "processingPending": 0,
            "processingRunning": 0,
            "processingFailed": 0,
            "previewPending": 0,
            "previewRunning": 0,
            "previewFailed": 0,
            "analysisPending": 0,
            "analysisRunning": 0,
            "analysisFailed": 0,
        }


class _StorageClient:
    def __init__(self, broken=False):
        self.broken = broken

    def head_bucket(self, **kwargs):
        if self.broken:
            raise ConnectionError("signedUrl=do-not-log")


class _Storage:
    def __init__(self, broken=False):
        self.bucket = "test"
        self.client = _StorageClient(broken)


class _Service:
    def __init__(self, settings, *, database_broken=False, storage_broken=False):
        self.settings = settings
        self.catalog = _Catalog(database_broken)
        self.storage = _Storage(storage_broken)
        self.library_id = UUID("11111111-1111-4111-8111-111111111111")

    def initialize(self):
        return {}

    def backup_status(self):
        return {"postgresBackupKey": None, "postgresBackupAt": None}


def _client(monkeypatch, *, database_broken=False, storage_broken=False):
    settings = Settings(
        s3_endpoint="http://127.0.0.1:9",
        database_url="postgresql+psycopg://photo:photo@127.0.0.1:5432/photo",
        data_dir=Path("/tmp/photo-server-health-tests"),
    )
    monkeypatch.setattr(
        api_module,
        "Service",
        lambda value: _Service(
            value, database_broken=database_broken, storage_broken=storage_broken
        ),
    )
    monkeypatch.setattr(api_module, "reconcile_ready_upload_batches", lambda service: {})
    monkeypatch.setattr(api_module, "cleanup_abandoned_batches", lambda service: {})
    return TestClient(create_app(settings))


def test_livez_does_not_check_dependencies(monkeypatch):
    with _client(monkeypatch, database_broken=True, storage_broken=True) as client:
        assert client.get("/livez").status_code == 200
        assert client.get("/livez").json() == {"status": "ok"}


def test_readyz_reports_dependency_failure(monkeypatch):
    with _client(monkeypatch, storage_broken=True) as client:
        response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "database": {"status": "ready", "errorClass": None},
        "storage": {"status": "unavailable", "errorClass": "ConnectionError"},
    }


def test_health_degraded_and_unavailable_are_structured(monkeypatch):
    with _client(monkeypatch, storage_broken=True) as client:
        degraded = client.get("/health")
    assert degraded.status_code == 200
    assert degraded.json()["status"] == "degraded"
    assert degraded.json()["storage"]["status"] == "unavailable"

    with _client(monkeypatch, database_broken=True, storage_broken=True) as client:
        unavailable = client.get("/health")
    assert unavailable.status_code == 503
    assert unavailable.json()["status"] == "unavailable"
    assert unavailable.json()["database"]["errorClass"] == "RuntimeError"


def test_structured_logs_redact_secrets(caplog):
    with caplog.at_level(logging.INFO, logger="photo_server"):
        log_event(
            "test_event",
            asset_id="asset-1",
            operation_id="op-1",
            api_key="secret-key",
            signed_url="https://example.invalid/signed",
            image_bytes=b"pixels",
        )
    record = json.loads(caplog.records[-1].message)
    assert record["event"] == "test_event"
    assert record["asset_id"] == "asset-1"
    assert record["api_key"] == "[REDACTED]"
    assert record["signed_url"] == "[REDACTED]"
    assert record["image_bytes"] == "[REDACTED]"
    assert "secret-key" not in caplog.text
    assert "pixels" not in caplog.text
