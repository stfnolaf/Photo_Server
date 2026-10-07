"""Disposable Phase 10 PostgreSQL/SeaweedFS cutover acceptance."""

from __future__ import annotations

import os
import socket
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import urlopen
from uuid import uuid4

import psycopg
import pytest
from PIL import Image
from sqlalchemy import delete, insert, select

from photo_server.backfill import backfill_postgres_to_s3
from photo_server.catalog import album_assets, albums, analysis_runs, faces, jobs, people
from photo_server.config import Settings
from photo_server.cutover import CutoverManager
from photo_server.rebuild import compare_projections, rebuild_from_s3
from photo_server.service import Service

pytestmark = pytest.mark.skipif(
    os.environ.get("PHOTO_RUN_PHASE10_CUTOVER_INTEGRATION") != "1",
    reason="Set PHOTO_RUN_PHASE10_CUTOVER_INTEGRATION=1 for disposable Phase 10 acceptance",
)


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_postgres(url: str) -> None:
    for _ in range(90):
        try:
            with psycopg.connect(url):
                return
        except psycopg.OperationalError:
            time.sleep(0.5)
    raise RuntimeError("disposable PostgreSQL did not become ready")


def _wait_s3(endpoint: str) -> None:
    for _ in range(90):
        try:
            with urlopen(endpoint, timeout=1):
                return
        except OSError:
            time.sleep(0.5)
    raise RuntimeError("disposable SeaweedFS did not become ready")


@pytest.fixture
def disposable_backends(tmp_path: Path):
    project = f"photo-phase10-{uuid4().hex}"
    database = f"phase10_{uuid4().hex[:16]}"
    bucket = f"phase10-{uuid4().hex}"
    pg_port, s3_port = _port(), _port()
    compose = tmp_path / "phase10-compose.yaml"
    compose.write_text(f"""services:
  postgres:
    image: postgres:17
    environment:
      POSTGRES_USER: phase10
      POSTGRES_PASSWORD: phase10
      POSTGRES_DB: {database}
    ports: [\"127.0.0.1:{pg_port}:5432\"]
    healthcheck:
      test: [CMD-SHELL, pg_isready -U phase10 -d {database}]
      interval: 1s
      timeout: 2s
      retries: 45
  seaweedfs:
    image: chrislusf/seaweedfs:latest
    command: server -s3 -dir=/data -s3.port=8333
    ports: [\"127.0.0.1:{s3_port}:8333\"]
""")
    env = {**os.environ, "COMPOSE_PROJECT_NAME": project}
    try:
        subprocess.run(["docker", "compose", "-f", str(compose), "up", "-d", "--wait"],
                       check=True, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        endpoint = f"http://127.0.0.1:{s3_port}"
        database_url = f"postgresql+psycopg://phase10:phase10@127.0.0.1:{pg_port}/{database}"
        _wait_postgres(f"postgresql://phase10:phase10@127.0.0.1:{pg_port}/{database}")
        _wait_s3(endpoint)
        yield {"database_url": database_url, "bucket": bucket, "s3_endpoint": endpoint, "root": tmp_path}
    finally:
        subprocess.run(["docker", "compose", "-f", str(compose), "down", "-v", "--remove-orphans"],
                       check=False, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


def _settings(backends: dict, root: Path, database_url: str | None = None) -> Settings:
    return Settings(_env_file=None, s3_endpoint=backends["s3_endpoint"], s3_bucket=backends["bucket"],
                    s3_anonymous=True, database_url=database_url or backends["database_url"],
                    authority_mode="s3", import_root=root / "imports", data_dir=root / "runtime")


def test_disposable_s3_cutover_rebuild_reconcile_and_rollback(disposable_backends):
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
    person_id, face_id, run_id, album_id = map(str, (uuid4(), uuid4(), uuid4(), uuid4()))
    input_hash = service.catalog.get(asset_ids[0]).primary.sha256
    artifact_key = f"analysis/{asset_ids[0]}/fixture/{run_id}.json"
    service.storage.put_json(artifact_key, {"assetId": asset_ids[0], "faces": 1})
    created = datetime(2026, 10, 6, 19, 0, tzinfo=UTC)
    with service.catalog.engine.begin() as connection:
        connection.execute(insert(people).values(id=person_id, display_name="Avery", created_at=created))
        connection.execute(insert(analysis_runs).values(
            id=run_id, asset_id=asset_ids[0], analysis_type="photo-ai", model_name="fixture",
            model_version="1", pipeline_version="fixture-v1", input_hash=input_hash,
            object_key=artifact_key, result={"description": "fixture"}, searchable_text="fixture",
            is_current=True, semantic_origin="computed", created_at=created,
        ))
        connection.execute(insert(faces).values(
            id=face_id, asset_id=asset_ids[0], analysis_run_id=run_id, person_id=person_id,
            face_index=0, bounding_box={"x": 1, "y": 2, "width": 3, "height": 4},
            confidence=0.99, embedding=[0.1, 0.2],
        ))
        connection.execute(insert(albums).values(
            id=album_id, state_revision=1,
            state={"schemaVersion": 1, "libraryId": str(service.library_id), "albumId": album_id,
                   "revision": 1, "previousRevision": None, "operationId": str(uuid4()),
                   "name": "Fixture", "description": "Phase 10", "assetIds": asset_ids,
                   "deletedAt": None, "mutation": {"action": "album.create", "entityId": album_id,
                   "changes": {}, "expectedRevision": None}}, deleted_at=None,
        ))
        connection.execute(insert(album_assets), [
            {"album_id": album_id, "asset_id": asset_ids[0], "position": 0},
            {"album_id": album_id, "asset_id": asset_ids[1], "position": 1},
        ])
    for key in list(service.storage.keys("manifests/")):
        service.storage.delete(key)
    exported = backfill_postgres_to_s3(service.catalog, service.storage, resume=False)
    assert exported["status"] == "complete", exported
    ready = service.cutover_readiness(checkpoint_id="phase10")
    assert ready["status"] == "ready", ready
    assert CutoverManager(service).activate(ready)["authorityMode"] == "s3"
    canonical_prefixes = ("manifests/", "tombstones/", "objects/")
    source_keys = sorted(key for key in service.storage.keys("") if key.startswith(canonical_prefixes))
    with service.catalog.engine.connect() as connection:
        queue_before = [dict(row) for row in connection.execute(select(jobs)).mappings()]

    # Rebuild into a fresh disposable database without copying operational rows.
    fresh_name = f"phase10fresh_{uuid4().hex[:12]}"
    admin = disposable_backends["database_url"].replace("postgresql+psycopg", "postgresql").rsplit("/", 1)[0] + "/postgres"
    with psycopg.connect(admin, autocommit=True) as connection:
        connection.execute(f'CREATE DATABASE "{fresh_name}"')
    fresh_url = disposable_backends["database_url"].rsplit("/", 1)[0] + "/" + fresh_name
    fresh = Service(_settings(disposable_backends, root / "fresh", fresh_url))
    fresh.catalog.initialize(str(service.library_id))
    rebuilt = rebuild_from_s3(fresh.storage, fresh.catalog, resume=False)
    assert rebuilt["status"] == "complete", rebuilt
    assert compare_projections(service.catalog, fresh.catalog)["match"]

    with service.catalog.engine.begin() as connection:
        connection.execute(delete(faces).where(faces.c.id == face_id))
        connection.execute(delete(people).where(people.c.id == person_id))
    repaired = service.reconcile_authority(checkpoint_id="repair", apply=True)
    assert repaired["status"] == "complete" and repaired["repaired"] >= 1, repaired
    assert sorted(key for key in service.storage.keys("") if key.startswith(canonical_prefixes)) == source_keys
    with service.catalog.engine.connect() as connection:
        assert [dict(row) for row in connection.execute(select(jobs)).mappings()] == queue_before
    assert service.rollback_s3_authority("verification fallback")["authorityMode"] == "postgres"
