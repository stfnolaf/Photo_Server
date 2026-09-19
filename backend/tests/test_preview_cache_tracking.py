"""Phase 1 of the preview cache eviction plan: access and size tracking."""

import io
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import text
from test_integration import backend as backend  # Reuse disposable infrastructure.
from test_integration import (
    photo,
    pytestmark,  # noqa: F401
)

from photo_server.models import Blob, Manifest
from photo_server.worker import cache_paths, generate, rebuild_cache_index, run_once


def recording_catalog():
    calls = {"record": [], "backfill": [], "touch": []}

    def record(asset_id, preview_bytes, thumbnail_bytes):
        calls["record"].append((asset_id, preview_bytes, thumbnail_bytes))

    def backfill(asset_id, preview_bytes, thumbnail_bytes):
        calls["backfill"].append((asset_id, preview_bytes, thumbnail_bytes))
        return True

    def touch(asset_id):
        calls["touch"].append(asset_id)
        return True

    catalog = SimpleNamespace(
        record_preview_cache=record,
        backfill_preview_cache=backfill,
        touch_preview_cache=touch,
    )
    return catalog, calls


def preview_fixture(tmp_path, content, role="ORIGINAL_RAW", metadata=None):
    asset_id = uuid4()
    blob = Blob(
        blob_id=uuid4(),
        role=role,
        original_filename="sample.ARW",
        object_key=f"originals/{asset_id}/sample.ARW",
        sha256="a" * 64,
        size_bytes=len(content),
        mime_type="image/x-sony-arw",
    )
    manifest = Manifest(
        library_id=uuid4(),
        asset_id=asset_id,
        operation_id=uuid4(),
        primary_blob_id=blob.blob_id,
        blobs=[blob],
        imported_at="2026-01-01T00:00:00Z",
        metadata=metadata or {},
    )
    catalog, calls = recording_catalog()
    service = SimpleNamespace(
        settings=SimpleNamespace(data_dir=tmp_path, exiftool="exiftool"),
        scratch=tmp_path,
        storage=SimpleNamespace(chunks=lambda key: [content]),
        catalog=catalog,
    )
    return service, manifest, calls


def jpeg_bytes(size=(100, 60)):
    output = io.BytesIO()
    Image.new("RGB", size, "blue").save(output, "JPEG")
    return output.getvalue()


def test_generate_records_exact_sizes(tmp_path):
    service, manifest, calls = preview_fixture(tmp_path, jpeg_bytes(), role="ORIGINAL_JPEG")
    assert generate(service, manifest) is True
    targets = cache_paths(service, manifest)
    assert calls["record"] == [
        (str(manifest.asset_id), targets["preview"].stat().st_size, targets["thumbnail"].stat().st_size)
    ]
    assert calls["backfill"] == []
    # Second run: files already exist, so only the backfill path may fire.
    assert generate(service, manifest) is True
    assert calls["record"] == [
        (str(manifest.asset_id), targets["preview"].stat().st_size, targets["thumbnail"].stat().st_size)
    ]
    assert calls["backfill"] == [
        (str(manifest.asset_id), targets["preview"].stat().st_size, targets["thumbnail"].stat().st_size)
    ]


def test_unavailable_raw_records_nothing(tmp_path, monkeypatch):
    service, manifest, calls = preview_fixture(tmp_path, b"raw-original")
    monkeypatch.setattr(
        "photo_server.worker.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=b""),
    )
    assert generate(service, manifest) is False
    assert calls["record"] == []
    assert calls["backfill"] == []
    assert calls["touch"] == []


def test_catalog_touch_guard_and_backfill(backend):
    service = backend.service
    asset_id = service.import_batch([photo(backend.root).name], uuid4())["results"][0]["assetId"]
    assert service.catalog.record_preview_cache(asset_id, 100, 50) is None
    # Backfill never updates an existing row...
    assert service.catalog.backfill_preview_cache(asset_id, 1, 2) is False
    # ...and a just-recorded row counts as freshly accessed.
    assert service.catalog.touch_preview_cache(asset_id) is False
    with service.catalog.engine.begin() as connection:
        connection.execute(
            text("UPDATE preview_cache SET last_accessed_at = now() - interval '1 hour' "
                 "WHERE asset_id = :id"),
            {"id": asset_id},
        )
    assert service.catalog.touch_preview_cache(asset_id) is True
    assert service.catalog.touch_preview_cache(asset_id) is False  # within the 30s guard
    with service.catalog.engine.begin() as connection:
        row = connection.execute(
            text("SELECT preview_bytes, thumbnail_bytes FROM preview_cache "
                 "WHERE asset_id = :id"),
            {"id": asset_id},
        ).one()
    assert row == (100, 50)  # backfill never overwrote the recorded sizes


def test_api_serving_touches_at_most_once_per_cooldown(backend):
    from photo_server.api import create_app

    service = backend.service
    asset_id = service.import_batch([photo(backend.root).name], uuid4())["results"][0]["assetId"]
    assert run_once(service)["status"] == "ready"
    with TestClient(create_app(service.settings)) as client:
        first = client.get(f"/assets/{asset_id}/preview")
        assert first.status_code == 200
        with service.catalog.engine.begin() as connection:
            connection.execute(
                text("UPDATE preview_cache SET last_accessed_at = now() - interval '1 hour' "
                     "WHERE asset_id = :id"),
                {"id": asset_id},
            )
        assert service.catalog.touch_preview_cache(asset_id) is True  # backdated
        second = client.get(f"/assets/{asset_id}/preview")
        assert second.status_code == 200  # in-process cooldown skips the touch
        with service.catalog.engine.begin() as connection:
            row = connection.execute(
                text("SELECT preview_bytes, thumbnail_bytes FROM preview_cache "
                     "WHERE asset_id = :id"),
                {"id": asset_id},
            ).one()
    assert row.preview_bytes > 0 and row.thumbnail_bytes > 0


def test_api_backfills_missing_row(backend):
    from photo_server.api import create_app

    service = backend.service
    asset_id = service.import_batch([photo(backend.root).name], uuid4())["results"][0]["assetId"]
    assert run_once(service)["status"] == "ready"
    with service.catalog.engine.begin() as connection:
        # Simulate a cache directory that predates tracking.
        connection.execute(text("DELETE FROM preview_cache WHERE asset_id = :id"), {"id": asset_id})
    with TestClient(create_app(service.settings)) as client:
        response = client.get(f"/assets/{asset_id}/thumbnail")
        assert response.status_code == 200
    with service.catalog.engine.begin() as connection:
        row = connection.execute(
            text("SELECT preview_bytes, thumbnail_bytes FROM preview_cache "
                 "WHERE asset_id = :id"),
            {"id": asset_id},
        ).one()
    assert row is not None and row.preview_bytes > 0 and row.thumbnail_bytes > 0


def test_rebuild_cache_index_backfills_and_skips_orphans(backend):
    service = backend.service
    asset_id = service.import_batch([photo(backend.root).name], uuid4())["results"][0]["assetId"]
    assert run_once(service)["status"] == "ready"
    with service.catalog.engine.begin() as connection:
        connection.execute(text("DELETE FROM preview_cache"))
    # An orphaned directory whose asset no longer exists.
    orphan = service.settings.data_dir / "cache" / f"{uuid4()}-{'b' * 64}-v1"
    orphan.mkdir(parents=True)
    (orphan / "preview.jpg").write_bytes(b"orphan")
    (orphan / "thumbnail.jpg").write_bytes(b"orphan")
    result = rebuild_cache_index(service)
    assert result == {"status": "ok", "directories": 2, "rowsAdded": 1, "rowsSkipped": 1}
    with service.catalog.engine.begin() as connection:
        row = connection.execute(
            text("SELECT preview_bytes, thumbnail_bytes FROM preview_cache "
                 "WHERE asset_id = :id"),
            {"id": asset_id},
        ).one()
    targets = cache_paths(service, service.catalog.get(asset_id))
    assert row == (targets["preview"].stat().st_size, targets["thumbnail"].stat().st_size)
