"""Disposable S3/PostgreSQL projection reconciliation acceptance.

Only randomly named Compose projects, ports, databases, buckets, and temporary
directories are created. The repository Compose file and production resources
are never read or mutated.
"""

from __future__ import annotations

import hashlib
import os
import socket
import subprocess
import time
from pathlib import Path
from urllib.request import urlopen
from uuid import uuid4

import psycopg
import pytest
from PIL import Image
from sqlalchemy import select, update

from photo_server.canonical import CanonicalIntegrityError
from photo_server.catalog import album_assets, assets, jobs
from photo_server.config import Settings
from photo_server.models import Mutation
from photo_server.reconcile import reconcile_s3_to_postgres
from photo_server.service import Service
from photo_server.state import mutate

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("PHOTO_RUN_RECONCILIATION_INTEGRATION") != "1",
        reason="Set PHOTO_RUN_RECONCILIATION_INTEGRATION=1 for disposable reconciliation acceptance",
    ),
]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_postgres(url: str) -> None:
    for _ in range(60):
        try:
            with psycopg.connect(url):
                return
        except psycopg.OperationalError:
            time.sleep(0.5)
    raise RuntimeError("disposable PostgreSQL did not become ready")


def _wait_s3(endpoint: str) -> None:
    for _ in range(60):
        try:
            with urlopen(endpoint, timeout=1):
                return
        except OSError:
            time.sleep(0.5)
    raise RuntimeError("disposable S3 did not become ready")


@pytest.fixture
def disposable_backends(tmp_path):
    project = f"photo-reconcile-{uuid4().hex}"
    database = f"reconcile_{uuid4().hex[:16]}"
    bucket = f"reconcile-{uuid4().hex}"
    pg_port, s3_port = _free_port(), _free_port()
    compose = tmp_path / "reconcile-compose.yaml"
    compose.write_text(
        f"""services:
  postgres:
    image: postgres:17
    environment:
      POSTGRES_USER: reconcile
      POSTGRES_PASSWORD: reconcile
      POSTGRES_DB: {database}
    ports: [\"127.0.0.1:{pg_port}:5432\"]
    healthcheck:
      test: [CMD-SHELL, pg_isready -U reconcile -d {database}]
      interval: 1s
      timeout: 2s
      retries: 30
  s3:
    image: chrislusf/seaweedfs:latest
    command: server -s3 -dir=/data -s3.port=8333
    ports: [\"127.0.0.1:{s3_port}:8333\"]
"""
    )
    env = {**os.environ, "COMPOSE_PROJECT_NAME": project}
    try:
        subprocess.run(
            ["docker", "compose", "-f", str(compose), "up", "-d", "--wait"],
            check=True, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        database_url = f"postgresql+psycopg://reconcile:reconcile@127.0.0.1:{pg_port}/{database}"
        _wait_postgres(f"postgresql://reconcile:reconcile@127.0.0.1:{pg_port}/{database}")
        _wait_s3(f"http://127.0.0.1:{s3_port}")
        yield {
            "database_url": database_url,
            "bucket": bucket,
            "s3_endpoint": f"http://127.0.0.1:{s3_port}",
            "root": tmp_path,
        }
    finally:
        subprocess.run(
            ["docker", "compose", "-f", str(compose), "down", "-v", "--remove-orphans"],
            check=False, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )


def _settings(backends, root: Path) -> Settings:
    return Settings(
        _env_file=None,
        s3_endpoint=backends["s3_endpoint"],
        s3_bucket=backends["bucket"],
        s3_anonymous=True,
        database_url=backends["database_url"],
        import_root=root / "imports",
        data_dir=root / "runtime",
    )


def test_disposable_reconciliation_lifecycle(disposable_backends):
    root = disposable_backends["root"]
    service = Service(_settings(disposable_backends, root))
    service.initialize()
    imports = root / "imports"
    imports.mkdir()
    Image.new("RGB", (12, 12), "red").save(imports / "one.jpg", format="JPEG")
    Image.new("RGB", (12, 12), "blue").save(imports / "two.jpg", format="JPEG")
    imported = service.import_batch(["one.jpg", "two.jpg"], uuid4())
    assert all(item["status"] == "imported" for item in imported["results"])
    asset_ids = [item["assetId"] for item in imported["results"]]
    album_id = uuid4()
    mutate(service, uuid4(), Mutation(
        action="album.create", entity_id=album_id,
        changes={"name": "Fixture", "description": "Reconciliation fixture", "assetIds": asset_ids},
    ))
    mutate(service, uuid4(), Mutation(
        action="asset.delete", entity_id=asset_ids[1], changes={}, expected_revision=1,
    ))

    with service.catalog.engine.connect() as connection:
        queue_before = [dict(row) for row in connection.execute(select(jobs)).mappings()]

    missing_key = next(service.storage.keys(f"manifests/assets/{asset_ids[0]}/"))
    missing_body = service.storage.read_bytes(missing_key)
    service.storage.delete(missing_key)
    service.storage.put(
        "objects/" + hashlib.sha256(b"orphan").hexdigest(), b"orphan", "image/jpeg"
    )
    with service.catalog.engine.begin() as connection:
        connection.execute(update(assets).where(assets.c.id == asset_ids[1]).values(rating=5))
        connection.execute(update(album_assets).where(
            (album_assets.c.album_id == str(album_id)) & (album_assets.c.asset_id == asset_ids[0])
            ).values(position=2))

    before_keys = sorted(service.storage.keys(""))
    dry = reconcile_s3_to_postgres(service.storage, service.catalog, checkpoint_id="dry", dry_run=True)
    assert dry["status"] in {"complete", "failed"}, dry
    assert dry["dryRun"] is True
    assert sorted(service.storage.keys("")) == before_keys
    assert dry["missing"] or dry["divergent"] or dry["orphaned"]
    assert any(item["category"] == "ordered-membership" for item in dry["divergent"])
    tombstone_key = next(service.storage.keys(f"tombstones/asset/{asset_ids[1]}/"))
    tombstone_body = service.storage.read_bytes(tombstone_key)
    service.storage.delete(tombstone_key)
    missing_tombstone = reconcile_s3_to_postgres(
        service.storage, service.catalog, checkpoint_id="missing-tombstone", dry_run=True
    )
    assert any(item["category"] == "tombstone" for item in missing_tombstone["missing"])
    service.storage.put(tombstone_key, tombstone_body, "application/json")

    paused = reconcile_s3_to_postgres(
        service.storage, service.catalog, checkpoint_id="resume", dry_run=False,
        report_only=True, stop_after=1, resume=False,
    )
    assert paused["status"] == "paused"
    resumed = reconcile_s3_to_postgres(
        service.storage, service.catalog, checkpoint_id="resume", dry_run=False,
        report_only=True, resume=True,
    )
    assert resumed["status"] in {"complete", "failed"}
    repeated = reconcile_s3_to_postgres(
        service.storage, service.catalog, checkpoint_id="resume", dry_run=False,
        report_only=True, resume=True,
    )
    assert repeated["status"] == "complete"
    assert repeated["counts"]["scanned"] == 0

    with service.catalog.engine.connect() as connection:
        assert [dict(row) for row in connection.execute(select(jobs)).mappings()] == queue_before

    # Corrupt an immutable manifest and an immutable referenced object. Both
    # must fail the scan and are restored only in this disposable bucket.
    service.storage.client.put_object(
        Bucket=service.storage.bucket, Key=missing_key, Body=missing_body + b"corrupt"
    )
    corrupt_manifest = reconcile_s3_to_postgres(
        service.storage, service.catalog, checkpoint_id="corrupt-manifest", dry_run=True
    )
    assert corrupt_manifest["status"] == "failed"
    service.storage.client.put_object(
        Bucket=service.storage.bucket, Key=missing_key, Body=missing_body
    )
    primary = service.catalog.get(asset_ids[0]).primary
    object_key = f"objects/{primary.sha256}"
    object_body = service.storage.read_bytes(object_key)
    service.storage.client.put_object(
        Bucket=service.storage.bucket, Key=object_key, Body=object_body + b"corrupt"
    )
    corrupt_object = reconcile_s3_to_postgres(
        service.storage, service.catalog, checkpoint_id="corrupt-object", dry_run=True
    )
    assert corrupt_object["status"] == "failed"
    service.storage.client.put_object(
        Bucket=service.storage.bucket, Key=object_key, Body=object_body
    )

    # Canonical keys remain immutable even when the projection is divergent.
    service.storage.client.put_object(
        Bucket=service.storage.bucket, Key=missing_key, Body=missing_body + b"conflict"
    )
    with pytest.raises(CanonicalIntegrityError):
        service.publisher._put_immutable(missing_key, missing_body, "application/json")
    service.storage.client.put_object(
        Bucket=service.storage.bucket, Key=missing_key, Body=missing_body
    )
    service.storage.put(missing_key, missing_body, "application/json")
