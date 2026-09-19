"""Phase 2 of the preview cache eviction plan: LRU eviction and orphan sweep.

Runs against the disposable Postgres/S3 infrastructure from ``test_integration``
(``PHOTO_RUN_INTEGRATION=1``). No AI worker is involved; every test gets its own
throwaway catalog via the function-scoped ``backend`` fixture.
"""

import hashlib
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from test_integration import backend as backend  # Reuse disposable infrastructure.
from test_integration import (
    photo,
    pytestmark,  # noqa: F401  apply the integration marker to every test here.
)

from photo_server.config import Settings
from photo_server.models import Blob, Manifest
from photo_server.worker import (
    cache_paths,
    evict_previews,
    run_once,
    sweep_orphaned_preview_dirs,
)


def cache_size(service, asset_id) -> int:
    """Actual on-disk bytes of one asset's cached preview set."""
    return sum(
        path.stat().st_size
        for path in cache_paths(service, service.catalog.get(asset_id)).values()
    )


def set_job(service, asset_id, job_type: str, status: str):
    """Upsert one (asset, job) row to a known status, for exemption setup."""
    with service.catalog.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO jobs (asset_id, job_type, status, attempts) "
                "VALUES (:asset_id, :job_type, :status, 0) "
                "ON CONFLICT (asset_id, job_type) DO UPDATE SET status = EXCLUDED.status"
            ),
            {"asset_id": asset_id, "job_type": job_type, "status": status},
        )


def synthetic_asset_id(service, number: int, preview_bytes: int, thumbnail_bytes: int) -> str:
    """Insert a catalog row plus a preview_cache row without touching S3 or disk.

    The preview/ai jobs that ``apply()`` queues are terminalized so the synthetic
    row is eviction-eligible unless a test deliberately re-activates one.
    """
    asset_id = "40000000-0000-0000-0000-" + f"{number:012d}"
    blob = Blob(
        blob_id=uuid4(),
        role="ORIGINAL_JPEG",
        original_filename=f"evict-{number}.JPG",
        object_key=f"originals/{asset_id}/evict-{number}.JPG",
        sha256=hashlib.sha256(str(number).encode()).hexdigest(),
        size_bytes=1,
        mime_type="image/jpeg",
    )
    manifest = Manifest(
        library_id=service.library_id,
        asset_id=UUID(asset_id),
        operation_id=uuid4(),
        primary_blob_id=blob.blob_id,
        blobs=[blob],
        imported_at="2026-01-01T00:00:00Z",
    )
    service.catalog.apply(manifest)
    set_job(service, asset_id, "preview-v1", "ready")
    set_job(service, asset_id, "ai-v1", "ready")
    service.catalog.record_preview_cache(asset_id, preview_bytes, thumbnail_bytes)
    return asset_id


def imported_asset(service, name: str, color: str) -> str:
    """Import one photo, generate its preview, and retire the pending AI job.

    A fresh import carries a pending ``ai-v1`` job, which makes the asset
    eviction-exempt; completing it models a library whose analysis already ran.
    """
    asset_id = service.import_batch(
        [photo(service.settings.import_root, name, color).name], uuid4()
    )["results"][0]["assetId"]
    for _ in range(8):
        claim = run_once(service)
        if claim and claim.get("status") == "ready":
            break
    assert cache_size(service, asset_id) > 0, "preview files must exist after run_once"
    # Model "AI analysis already finished" so the fresh import is evictable.
    set_job(service, asset_id, "ai-v1", "ready")
    return asset_id


def test_settings_cache_defaults_and_env(monkeypatch):
    for key in ("PHOTO_CACHE_MAX_BYTES", "PHOTO_CACHE_EVICTION_INTERVAL", "PHOTO_CACHE_EVICT_TARGET_RATIO"):
        monkeypatch.delenv(key, raising=False)
    base = dict(_env_file=None, s3_endpoint="http://localhost:9000", POSTGRES_PASSWORD="x")
    settings = Settings(**base)
    # Safe default: eviction off until explicitly enabled.
    assert (settings.cache_max_bytes, settings.cache_eviction_interval_seconds,
            settings.cache_eviction_target_ratio) == (0, 300, 0.9)
    monkeypatch.setenv("PHOTO_CACHE_MAX_BYTES", "1000")
    monkeypatch.setenv("PHOTO_CACHE_EVICTION_INTERVAL", "60")
    monkeypatch.setenv("PHOTO_CACHE_EVICT_TARGET_RATIO", "0.75")
    settings = Settings(**base)
    assert (settings.cache_max_bytes, settings.cache_eviction_interval_seconds,
            settings.cache_eviction_target_ratio) == (1000, 60, 0.75)
    monkeypatch.setenv("PHOTO_CACHE_MAX_BYTES", "-1")
    with pytest.raises(ValidationError):
        Settings(**base)


def test_under_budget_evicts_nothing(backend):
    service = backend.service
    asset_id = imported_asset(service, "quiet.JPG", "red")
    size = cache_size(service, asset_id)
    service.settings.cache_max_bytes = size + 1
    assert evict_previews(service) == {
        "status": "ok",
        "evicted": 0,
        "freedBytes": 0,
        "remainingBytes": size,
    }
    assert service.catalog.preview_cache_total_bytes() == size
    assert cache_size(service, asset_id) == size  # files untouched


def test_over_budget_evicts_least_recently_used_first(backend, capsys):
    service = backend.service
    oldest, middle, newest = (
        imported_asset(service, name, color)
        for name, color in [("old.JPG", "red"), ("mid.JPG", "green"), ("new.JPG", "blue")]
    )
    backdate = service.catalog.backdate_preview_cache_access
    backdate([oldest], 3_600)
    backdate([middle], 1_800)
    # newest keeps its fresh timestamp, so it must survive despite any size.
    sizes = {a: cache_size(service, a) for a in (oldest, middle, newest)}
    total = sum(sizes.values())
    # A budget equal to two sets' worth lies strictly between every set and the
    # whole, forcing exactly the two least-recently-used sets out.
    service.settings.cache_max_bytes = 2 * sizes[newest]
    assert 0 < service.settings.cache_max_bytes < total
    result = evict_previews(service)
    assert result["status"] == "ok" and result["evicted"] == 2
    assert result["freedBytes"] == sizes[oldest] + sizes[middle]
    assert not cache_paths(service, service.catalog.get(oldest))["preview"].exists()
    assert not cache_paths(service, service.catalog.get(oldest))["thumbnail"].exists()
    assert not cache_paths(service, service.catalog.get(middle))["preview"].exists()
    assert cache_size(service, newest) == sizes[newest]  # fresh set survives
    # The cache now fits the budget; the survivor is the only row left.
    assert result["remainingBytes"] == service.catalog.preview_cache_total_bytes()
    assert result["remainingBytes"] <= service.settings.cache_max_bytes
    with service.catalog.engine.connect() as connection:
        rows = [row[0] for row in connection.execute(text("SELECT asset_id FROM preview_cache"))]
    assert rows == [newest]
    logged = capsys.readouterr().out
    assert f'"assetId": "{oldest}"' in logged and f'"assetId": "{middle}"' in logged
    assert f'"assetId": "{newest}"' not in logged
    assert '"status": "preview_eviction"' in logged


def test_lru_order_and_hysteresis_stop(backend):
    catalog = backend.service.catalog
    # Distinct, controlled row sizes plus staggered access times make the
    # selected set exactly determined. Sizes stalest -> newest: 100, 200, 500, 900
    # (total 1700). budget 1600 -> target int(1440) -> must free 260 -> take
    # a(100) then b(200) (300 >= 260) and stop before c.
    a = synthetic_asset_id(backend.service, 81, 100, 0)  # stalest
    b = synthetic_asset_id(backend.service, 82, 200, 0)
    c = synthetic_asset_id(backend.service, 83, 500, 0)  # noqa: E305
    d = synthetic_asset_id(backend.service, 84, 900, 0)  # newest
    catalog.backdate_preview_cache_access([a], 4_800)
    catalog.backdate_preview_cache_access([b], 3_600)
    catalog.backdate_preview_cache_access([c], 2_400)
    catalog.backdate_preview_cache_access([d], 1_200)
    assert catalog.preview_cache_total_bytes() == 1700
    selected = catalog.select_preview_cache_evictions(1600, 0.9)
    assert selected == [{"asset_id": a, "bytes": 100}, {"asset_id": b, "bytes": 200}]
    assert catalog.preview_cache_total_bytes() == 1400
    # After the eviction the cache fits; a covering budget evicts nothing.
    assert catalog.select_preview_cache_evictions(1400, 0.9) == []
    # And the survivors are exactly c and d.
    with catalog.engine.connect() as connection:
        survivors = sorted(
            row[0] for row in connection.execute(text("SELECT asset_id FROM preview_cache"))
        )
    assert survivors == sorted([c, d])


def test_exempt_assets_skip_eviction(backend):
    service = backend.service
    catalog = service.catalog
    running = synthetic_asset_id(service, 91, 900, 0)     # a big, very stale set
    pending_preview = synthetic_asset_id(service, 92, 800, 0)
    finished = synthetic_asset_id(service, 93, 700, 0)
    for asset_id in (running, pending_preview, finished):
        catalog.backdate_preview_cache_access([asset_id], 9_999_999)

    # Re-activate a job on the first two: a running ai-v1 or a pending preview-v1
    # must protect the row regardless of recency.
    set_job(service, running, "ai-v1", "running")
    set_job(service, pending_preview, "preview-v1", "pending")
    # Only `finished` is a candidate, so it alone is evicted even though the two
    # exempted rows are larger and equally stale.
    total = catalog.preview_cache_total_bytes()
    selected = catalog.select_preview_cache_evictions(total - 1, 0.9)
    assert [entry["asset_id"] for entry in selected] == [finished]

    # Terminal job states do NOT exempt: finish both, and every row becomes
    # evictable. A tiny budget forces all three out.
    set_job(service, running, "ai-v1", "ready")
    set_job(service, pending_preview, "preview-v1", "ready")
    catalog.record_preview_cache(finished, 700, 0)
    catalog.backdate_preview_cache_access([finished], 9_999_999)
    selected = catalog.select_preview_cache_evictions(1, 0.9)
    assert {entry["asset_id"] for entry in selected} == {running, pending_preview, finished}


def test_evicted_files_removed_and_job_untouched(backend):
    service = backend.service
    asset_id = imported_asset(service, "gone.JPG", "purple")
    paths = cache_paths(service, service.catalog.get(asset_id))
    directory = paths["preview"].parent
    assert all(path.exists() for path in paths.values())
    service.settings.cache_max_bytes = 1
    assert service.catalog.preview_cache_total_bytes() > 1
    result = evict_previews(service)
    assert result["evicted"] >= 1
    assert not paths["preview"].exists() and not paths["thumbnail"].exists()
    assert not directory.exists()
    # Eviction only removed cache state; the preview job still reports ready.
    assert service.catalog.preview_status(asset_id)["status"] == "ready"
    # A second pass is a no-op: nothing left to evict or to sweep.
    assert evict_previews(service) == {
        "status": "ok",
        "evicted": 0,
        "freedBytes": 0,
        "remainingBytes": 0,
    }


def test_orphan_sweep_removes_dirs_without_rows(backend):
    service = backend.service
    asset_id = imported_asset(service, "swept.JPG", "orange")
    valid = cache_paths(service, service.catalog.get(asset_id))["preview"].parent
    assert valid.exists()
    orphan = service.settings.data_dir / "cache" / f"orphan-{uuid4().hex}-{'c' * 64}-v1"
    orphan.mkdir(parents=True)
    (orphan / "preview.jpg").write_bytes(b"x")
    foreign = service.settings.data_dir / "cache" / "not-a-cache-name"
    foreign.mkdir()
    stray = service.settings.data_dir / "cache" / "stray-file.jpg"
    stray.write_bytes(b"y")
    # Simulate a crash between the two phases: the row is gone, files remain, so
    # both this dir and the orphaned one are reclaimable.
    with service.catalog.engine.begin() as connection:
        connection.execute(text("DELETE FROM preview_cache WHERE asset_id = :id"), {"id": asset_id})
    removed = sweep_orphaned_preview_dirs(service)
    assert asset_id in removed
    assert not valid.exists()
    assert not orphan.exists()
    assert foreign.exists() and stray.exists()  # invalid names and files are never touched
    # Second pass finds nothing valid without a row; the sweep is quiet.
    assert sweep_orphaned_preview_dirs(service) == []


def test_disabled_budget_skips_everything(backend):
    service = backend.service
    asset_id = imported_asset(service, "kept.JPG", "cyan")
    size = cache_size(service, asset_id)
    assert size > 0
    service.settings.cache_max_bytes = 0
    assert evict_previews(service) == {"status": "disabled"}
    assert cache_size(service, asset_id) == size
    assert service.catalog.select_preview_cache_evictions(0, 0.9) == []


def test_cache_eviction_regen_roundtrip(backend):
    """End-to-end miss path: after eviction the preview regenerates, never 500."""
    from fastapi.testclient import TestClient

    from photo_server.api import create_app

    service = backend.service
    asset_id = imported_asset(service, "regen.JPG", "magenta")
    with TestClient(create_app(service.settings)) as client:
        first = client.get(f"/assets/{asset_id}/preview")
        assert first.status_code == 200
        first_bytes = first.content
    service.settings.cache_max_bytes = 1
    evict_previews(service)
    with TestClient(create_app(service.settings)) as client:
        assert client.get(f"/assets/{asset_id}/preview").status_code == 202  # requeued
        assert run_once(service)["status"] == "ready"
        again = client.get(f"/assets/{asset_id}/preview")
        assert again.status_code == 200
        # Regenerated byte-identically from the immutable S3 original.
        assert again.content == first_bytes
    # The regenerated set is tracked again.
    assert service.catalog.preview_cache_total_bytes() > 0
