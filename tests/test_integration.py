import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from psycopg import sql
from sqlalchemy.engine import make_url

from photo_server.config import Settings
from photo_server.export import export_library
from photo_server.service import Service
from photo_server.storage import Storage
from photo_server.worker import cache_paths, run_once

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("PHOTO_RUN_INTEGRATION") != "1",
        reason="Set PHOTO_RUN_INTEGRATION=1 for live disposable backends",
    ),
]


@dataclass
class Backend:
    service: Service
    fresh_catalog: object
    root: Path


@pytest.fixture
def backend(tmp_path):
    base = Settings()
    root = tmp_path / "source"
    root.mkdir()
    base = base.model_copy(update={"s3_bucket": f"photo-test-{uuid4().hex}", "import_root": root})
    storage = Storage(base)
    storage.ensure_bucket()
    url = make_url(base.database_url)
    admin_url = url.set(drivername="postgresql").render_as_string(hide_password=False)
    services, databases = [], []

    def fresh_catalog():
        name = f"photo_test_{uuid4().hex}"
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        databases.append(name)
        settings = base.model_copy(
            update={
                "database_url": url.set(database=name).render_as_string(hide_password=False),
                "data_dir": tmp_path / name,
            }
        )
        service = Service(settings)
        services.append(service)
        service.initialize()
        return service

    try:
        yield Backend(fresh_catalog(), fresh_catalog, root)
    finally:
        for service in services:
            service.catalog.engine.dispose()
        for name in databases:
            with psycopg.connect(admin_url, autocommit=True) as admin:
                admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
        # This fixture created a fresh random bucket. Never clean the configured library bucket.
        for key in list(storage.keys("")):
            storage.client.delete_object(Bucket=storage.bucket, Key=key)
        storage.client.delete_bucket(Bucket=storage.bucket)


def photo(root, name="sample.JPG", color="red"):
    path = root / name
    Image.new("RGB", (80, 60), color).save(path, format="JPEG")
    return path


def test_storage_refuses_overwrite(backend):
    storage = backend.service.storage
    assert storage.put("probe", b"first", "text/plain")
    assert not storage.put("probe", b"second", "text/plain")
    assert b"".join(storage.chunks("probe")) == b"first"


def test_import_duplicate_retry_sidecar_and_empty_database_recovery(backend):
    service = backend.service
    original = photo(backend.root)
    sidecar = backend.root / "sample.xmp"
    sidecar.write_text('<x:xmpmeta xmlns:x="adobe:ns:meta/"/>')
    before = original.read_bytes(), sidecar.read_bytes()
    operation = uuid4()
    paths = [original.name, sidecar.name]
    result = service.import_batch(paths, operation)
    assert result["results"][0]["status"] == "imported", result
    asset_id = result["results"][0]["assetId"]
    assert service.catalog.counts() == {"assets": 1, "blobs": 2}
    assert service.import_batch(paths, operation)["results"][0]["replayed"]
    assert service.import_batch(paths, uuid4())["results"][0]["status"] == "duplicate"
    assert len(list(service.storage.keys("originals/"))) == 2
    fresh = backend.fresh_catalog()
    assert fresh.catalog.counts() == {"assets": 0, "blobs": 0}
    assert fresh.recover(full=True) == {"recovered": 1, "verification": "sha256", "errors": []}
    assert fresh.catalog.get(asset_id).document() == service.catalog.get(asset_id).document()
    assert (original.read_bytes(), sidecar.read_bytes()) == before
    photo(backend.root, color="blue")
    retry = service.import_batch(paths, operation)
    assert retry["results"][0]["status"] == "failed"
    assert "changed file content" in retry["results"][0]["error"]


def test_crash_after_manifest_before_database_is_recoverable(backend, monkeypatch):
    service = backend.service
    path = photo(backend.root)
    operation = uuid4()
    with monkeypatch.context() as patch:

        def crash(_manifest):
            raise RuntimeError("simulated database failure after S3 commit")

        patch.setattr(service.catalog, "apply", crash)
        result = service.import_batch([path.name], operation)
    assert result["results"][0]["status"] == "failed"
    assert service.catalog.counts()["assets"] == 0
    assert len(list(service.storage.keys("state/assets/"))) == 1
    assert service.recover(full=True)["recovered"] == 1
    retry = service.import_batch([path.name], operation)
    assert retry["results"][0]["replayed"]
    assert service.catalog.counts()["assets"] == 1


def test_crash_after_original_before_manifest_reuses_original(backend, monkeypatch):
    service = backend.service
    path = photo(backend.root)
    operation = uuid4()
    real_put = service.storage.put_json
    with monkeypatch.context() as patch:

        def crash(key, value):
            if key.startswith("state/assets/"):
                raise RuntimeError("simulated process failure before manifest")
            real_put(key, value)

        patch.setattr(service.storage, "put_json", crash)
        result = service.import_batch([path.name], operation)
    assert result["results"][0]["status"] == "failed"
    assert len(list(service.storage.keys("originals/"))) == 1
    assert service.import_batch([path.name], operation)["results"][0]["status"] == "imported"
    assert len(list(service.storage.keys("originals/"))) == 1


def test_failed_raw_does_not_claim_companion_was_imported(backend):
    raw = backend.root / "broken.ARW"
    raw.write_bytes(b"not a valid RAW")
    companion = photo(backend.root, "broken.JPG")
    result = backend.service.import_batch([companion.name, raw.name], uuid4())
    assert result["results"][0]["status"] == "failed"
    assert result["skipped"][0]["status"] == "deferred"
    assert companion.exists() and raw.exists()
    assert backend.service.catalog.counts()["assets"] == 0


def test_corruption_is_detected_without_indexing_into_new_database(backend):
    service = backend.service
    path = photo(backend.root)
    result = service.import_batch([path.name], uuid4())
    manifest = service.catalog.get(result["results"][0]["assetId"])
    corrupted = bytearray(path.read_bytes())
    corrupted[-1] ^= 1
    service.storage.client.put_object(
        Bucket=service.storage.bucket, Key=manifest.primary.object_key, Body=bytes(corrupted)
    )
    fresh = backend.fresh_catalog()
    result = fresh.recover(full=True)
    assert result["recovered"] == 0
    assert "Checksum verification failed" in result["errors"][0]["error"]
    assert fresh.catalog.counts()["assets"] == 0


def test_newer_unsupported_manifest_does_not_fall_back(backend):
    service = backend.service
    result = service.import_batch([photo(backend.root).name], uuid4())
    manifest = service.catalog.get(result["results"][0]["assetId"])
    document = manifest.document()
    document["revision"] = 2
    document["schemaVersion"] = 999
    service.storage.put_json(f"state/assets/{manifest.asset_id}/00000002.json", document)
    fresh = backend.fresh_catalog()
    assert fresh.recover()["errors"]
    assert fresh.catalog.counts()["assets"] == 0


def test_export_without_database_and_preview_cache_rebuild(backend, tmp_path):
    service = backend.service
    source = photo(backend.root)
    result = service.import_batch([source.name], uuid4())
    manifest = service.catalog.get(result["results"][0]["assetId"])
    assert run_once(service)["status"] == "ready"
    targets = cache_paths(service, manifest)
    assert all(path.exists() for path in targets.values())
    targets["thumbnail"].unlink()
    service.catalog.queue_preview(str(manifest.asset_id))
    assert run_once(service)["status"] == "ready"
    broken_db = service.settings.model_copy(
        update={"database_url": "postgresql+psycopg://invalid@localhost:1/absent"}
    )
    destination = tmp_path / "export"
    assert export_library(broken_db, destination)["exported"] == 1
    exported = destination / str(manifest.asset_id) / source.name
    assert hashlib.sha256(exported.read_bytes()).hexdigest() == manifest.primary.sha256


def test_api_plan_import_health_and_preview(backend):
    from photo_server.api import create_app

    source = photo(backend.root)
    with TestClient(create_app(backend.service.settings)) as client:
        assert client.get("/health").status_code == 200
        assert client.post("/imports/plan", json={"paths": [source.name]}).status_code == 200
        response = client.post(
            "/imports", json={"paths": [source.name], "operation_id": str(uuid4())}
        )
        assert response.status_code == 200
        asset_id = response.json()["results"][0]["assetId"]
        assert client.get("/assets").json()[0]["assetId"] == asset_id
        assert client.get(f"/assets/{asset_id}/original").content == source.read_bytes()
        assert client.get(f"/assets/{asset_id}/preview").status_code == 202
        run_once(backend.service)
        assert client.get(f"/assets/{asset_id}/thumbnail").headers["content-type"] == "image/jpeg"
        assert client.post(f"/assets/{asset_id}/preview/retry").json()["status"] == "pending"


def test_standalone_heif_import_and_preview(backend):
    import pillow_heif

    path = backend.root / "standalone.HEIF"
    pillow_heif.from_pillow(Image.new("RGB", (80, 60), "green")).save(path)
    result = backend.service.import_batch([path.name], uuid4())
    assert result["results"][0]["status"] == "imported", result
    manifest = backend.service.catalog.get(result["results"][0]["assetId"])
    assert manifest.primary.role == "ORIGINAL_HEIF"
    assert run_once(backend.service)["status"] == "ready"
