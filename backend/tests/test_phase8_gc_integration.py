"""Disposable PostgreSQL/S3 Phase 8 retention acceptance.

This suite is opt-in because it owns Docker resources.  It never uses the
repository Compose file or named production volumes.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.request import urlopen
from uuid import uuid4

import psycopg
import pytest
from PIL import Image
from sqlalchemy import select

from photo_server.catalog import jobs
from photo_server.config import Settings
from photo_server.garbage_collector import RetentionPolicy, collect_garbage
from photo_server.models import Mutation
from photo_server.service import Service
from photo_server.state import mutate

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_RUN_PHASE8_GC_INTEGRATION") != "1",
    reason="Set PHOTO_RUN_PHASE8_GC_INTEGRATION=1 for disposable Phase 8 acceptance",
)


def _port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait(url, postgres=False):
    for _ in range(60):
        try:
            if postgres:
                with psycopg.connect(url):
                    return
            urlopen(url, timeout=1).close()
            return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError("disposable backend did not become ready")


@pytest.fixture
def disposable(tmp_path: Path):
    project = f"photo-phase8-{uuid4().hex}"
    database = f"phase8_{uuid4().hex[:16]}"
    bucket = f"phase8-{uuid4().hex}"
    pg_port, s3_port = _port(), _port()
    compose = tmp_path / "phase8-compose.yaml"
    compose.write_text(f"""services:
  postgres:
    image: postgres:17
    environment:
      POSTGRES_USER: phase8
      POSTGRES_PASSWORD: phase8
      POSTGRES_DB: {database}
    ports: [\"127.0.0.1:{pg_port}:5432\"]
    healthcheck:
      test: [CMD-SHELL, pg_isready -U phase8 -d {database}]
      interval: 1s
      timeout: 2s
      retries: 30
  s3:
    image: chrislusf/seaweedfs:latest
    command: server -s3 -dir=/data -s3.port=8333
    ports: [\"127.0.0.1:{s3_port}:8333\"]
""")
    env = {**os.environ, "COMPOSE_PROJECT_NAME": project}
    try:
        subprocess.run(["docker", "compose", "-f", str(compose), "up", "-d", "--wait"], check=True, env=env)
        database_url = f"postgresql+psycopg://phase8:phase8@127.0.0.1:{pg_port}/{database}"
        _wait(database_url.replace("+psycopg", ""), postgres=True)
        _wait(f"http://127.0.0.1:{s3_port}")
        yield {"project": project, "compose": compose, "database": database, "bucket": bucket,
               "database_url": database_url, "s3": f"http://127.0.0.1:{s3_port}", "root": tmp_path}
    finally:
        subprocess.run(["docker", "compose", "-f", str(compose), "down", "-v", "--remove-orphans"],
                       check=False, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def test_disposable_reachability_retention_and_cleanup(disposable):
    root = disposable["root"]
    settings = Settings(_env_file=None, s3_endpoint=disposable["s3"], s3_bucket=disposable["bucket"],
                        s3_anonymous=True, database_url=disposable["database_url"],
                        import_root=root / "imports", data_dir=root / "runtime")
    service = Service(settings)
    service.initialize()
    (root / "imports").mkdir()
    Image.new("RGB", (16, 16), "red").save(root / "imports" / "one.jpg", format="JPEG")
    imported = service.import_batch(["one.jpg"], uuid4())
    assert imported["results"][0]["status"] == "imported"
    asset_id = imported["results"][0]["assetId"]
    with service.catalog.engine.connect() as connection:
        queue_before = [dict(row) for row in connection.execute(select(jobs)).mappings()]
    mutate(service, uuid4(), Mutation(action="asset.delete", entity_id=asset_id, changes={}, expected_revision=1))
    assert not service.catalog.list_assets()
    service.storage.put("incoming/disposable/stale", b"stale", "application/octet-stream")
    orphan = "objects/" + ("f" * 64)
    service.storage.put(orphan, b"orphan", "application/octet-stream")
    before_keys = sorted(service.storage.keys(""))
    as_of = (datetime.now(UTC) + timedelta(days=1)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    first = collect_garbage(service.storage, checkpoint_id="phase8", stop_after=1,
                             as_of=as_of,
                             policy=RetentionPolicy(original_object_days=0, temporary_upload_days=0))
    assert first["status"] == "paused", (first["errors"], first["unresolvedReferences"])
    resumed = collect_garbage(service.storage, checkpoint_id="phase8", as_of=as_of,
                               policy=RetentionPolicy(original_object_days=0, temporary_upload_days=0))
    repeated = collect_garbage(service.storage, checkpoint_id="phase8", as_of=as_of,
                                policy=RetentionPolicy(original_object_days=0, temporary_upload_days=0))
    assert resumed == repeated
    assert orphan in {item["key"] for item in resumed["candidates"]}
    canonical_before = sorted(service.storage.keys("manifests/")) + sorted(service.storage.keys("tombstones/"))
    assert canonical_before
    assert sorted(service.storage.keys("")) != []
    assert before_keys != sorted(service.storage.keys(""))  # only the operational GC checkpoint was added
    with service.catalog.engine.connect() as connection:
        assert [dict(row) for row in connection.execute(select(jobs)).mappings()] == queue_before
