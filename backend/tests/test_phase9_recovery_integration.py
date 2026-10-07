"""Disposable PostgreSQL/SeaweedFS Phase 9 recovery acceptance.

The Compose document is generated in pytest's temporary directory from the
two services this test actually needs. It has no named volumes and is removed
with the exact project/file in finally.
"""

from __future__ import annotations

import json
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
from sqlalchemy import insert, select, update
from sqlalchemy.engine import make_url

from photo_server.backfill import backfill_postgres_to_s3
from photo_server.catalog import (
    ai_stage_jobs,
    album_assets,
    albums,
    analysis_runs,
    assets,
    faces,
    jobs,
    onboarding_jobs,
    people,
    upload_batches,
)
from photo_server.config import Settings
from photo_server.manifests import canonical_json
from photo_server.rebuild import compare_projections, rebuild_from_s3
from photo_server.recovery import (
    create_recovery_checkpoint,
    restore_postgres_dump,
    restore_recovery_checkpoint,
)
from photo_server.service import Service
from photo_server.storage import Storage

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("PHOTO_RUN_PHASE9_RECOVERY_INTEGRATION") != "1",
        reason="Set PHOTO_RUN_PHASE9_RECOVERY_INTEGRATION=1 for disposable recovery acceptance",
    ),
]


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait(url: str) -> None:
    for _ in range(90):
        try:
            with urlopen(url, timeout=1):
                return
        except OSError:
            time.sleep(0.5)
    raise RuntimeError(f"disposable service did not become ready: {url}")


@pytest.fixture
def disposable_backends(tmp_path: Path):
    project = f"photo-phase9-{uuid4().hex}"
    database = f"phase9_{uuid4().hex[:16]}"
    bucket = f"phase9-{uuid4().hex}"
    backup_bucket = f"phase9-backup-{uuid4().hex}"
    pg_port, s3_port = _port(), _port()
    compose = tmp_path / "phase9-compose.yaml"
    compose.write_text(
        f"""services:
  postgres:
    image: postgres:17
    environment:
      POSTGRES_USER: phase9
      POSTGRES_PASSWORD: phase9
      POSTGRES_DB: {database}
    ports: [\"127.0.0.1:{pg_port}:5432\"]
    healthcheck:
      test: [CMD-SHELL, pg_isready -U phase9 -d {database}]
      interval: 1s
      timeout: 2s
      retries: 45
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
        endpoint = f"http://127.0.0.1:{s3_port}"
        database_url = f"postgresql+psycopg://phase9:phase9@127.0.0.1:{pg_port}/{database}"
        admin_url = f"postgresql://phase9:phase9@127.0.0.1:{pg_port}/{database}"
        for _ in range(90):
            try:
                with psycopg.connect(admin_url):
                    break
            except psycopg.OperationalError:
                time.sleep(0.5)
        else:
            raise RuntimeError("disposable PostgreSQL did not become ready")
        _wait(endpoint)
        yield {
            "project": project, "compose": compose, "database": database,
            "database_url": database_url, "admin_url": admin_url, "endpoint": endpoint,
            "bucket": bucket, "backup_bucket": backup_bucket, "root": tmp_path,
        }
    finally:
        subprocess.run(
            ["docker", "compose", "-f", str(compose), "down", "-v", "--remove-orphans"],
            check=False, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        compose.unlink(missing_ok=True)


def _settings(backends, database_url: str, root: Path, bucket: str) -> Settings:
    return Settings(
        _env_file=None, s3_endpoint=backends["endpoint"], s3_bucket=bucket,
        s3_anonymous=True, database_url=database_url, import_root=root / "imports",
        data_dir=root / "runtime" / bucket,
    )


def _create_database(admin_url: str, name: str) -> str:
    admin = make_url(admin_url)
    with psycopg.connect(admin.set(database="postgres").render_as_string(hide_password=False), autocommit=True) as connection:
        connection.execute(f'CREATE DATABASE "{name}"')
    return admin.set(database=name, drivername="postgresql+psycopg").render_as_string(hide_password=False)


def _operational_snapshot(catalog):
    snapshot = {}
    for table in (jobs, ai_stage_jobs, onboarding_jobs, upload_batches):
        with catalog.engine.connect() as connection:
            rows = connection.execute(select(table)).mappings().all()
        snapshot[table.name] = json.loads(json.dumps([dict(row) for row in rows], default=str, sort_keys=True))
    return snapshot


def test_disposable_checkpoint_resume_restore_and_rebuild(disposable_backends):
    root = disposable_backends["root"]
    source = Service(_settings(disposable_backends, disposable_backends["database_url"], root, disposable_backends["bucket"]))
    source.initialize()
    imports = root / "imports"
    imports.mkdir()
    for name, color in (("one.jpg", "red"), ("two.jpg", "blue")):
        Image.new("RGB", (32, 24), color).save(imports / name, format="JPEG")
    imported = source.import_batch(["one.jpg", "two.jpg"], uuid4())
    assert all(item["status"] == "imported" for item in imported["results"]), imported
    asset_ids = [item["assetId"] for item in imported["results"]]

    person_id, face_id, run_id, album_id = (str(uuid4()) for _ in range(4))
    input_hash = source.catalog.get(asset_ids[0]).primary.sha256
    artifact_source = f"analysis/{asset_ids[0]}/{run_id}.json"
    artifact_body = json.dumps({"description": "phase9 fixture"}, sort_keys=True).encode()
    source.storage.put(artifact_source, artifact_body, "application/json")
    created = datetime(2026, 10, 6, 19, 0, tzinfo=UTC)
    with source.catalog.engine.begin() as connection:
        connection.execute(insert(people).values(id=person_id, display_name="Avery", created_at=created))
        connection.execute(insert(analysis_runs).values(
            id=run_id, asset_id=asset_ids[0], analysis_type="photo-ai", model_name="fixture",
            model_version="1", pipeline_version="fixture-v1", input_hash=input_hash,
            object_key=artifact_source, result={"description": "phase9 fixture"},
            searchable_text="phase9 fixture", is_current=True, semantic_origin="computed", created_at=created,
        ))
        connection.execute(insert(faces).values(
            id=face_id, asset_id=asset_ids[0], analysis_run_id=run_id, person_id=person_id,
            face_index=0, bounding_box={"x": 1, "y": 2, "width": 3, "height": 4},
            confidence=0.99, embedding=[0.1, 0.2],
        ))
        connection.execute(insert(albums).values(
            id=album_id, state_revision=1,
            state={"schemaVersion": 1, "libraryId": str(source.library_id), "albumId": album_id,
                   "revision": 1, "previousRevision": None, "operationId": str(uuid4()),
                   "name": "Fixture", "description": "Phase 9", "assetIds": asset_ids,
                   "deletedAt": None, "mutation": {"action": "album.create", "entityId": album_id,
                   "changes": {}, "expectedRevision": None}}, deleted_at=None,
        ))
        connection.execute(insert(album_assets), [
            {"album_id": album_id, "asset_id": asset_ids[0], "position": 0},
            {"album_id": album_id, "asset_id": asset_ids[1], "position": 1},
        ])
        deleted = dict(connection.scalar(select(assets.c.manifest).where(assets.c.id == asset_ids[1])))
        deleted["deletedAt"] = "2026-10-06T20:00:00Z"
        connection.execute(update(assets).where(assets.c.id == asset_ids[1]).values(
            deleted_at="2026-10-06T20:00:00Z", manifest=deleted,
        ))

    for key in list(source.storage.keys("manifests/")) + list(source.storage.keys("tombstones/")):
        source.storage.delete(key)
    exported = backfill_postgres_to_s3(source.catalog, source.storage, resume=False)
    assert exported["status"] == "complete", exported
    operational_before = _operational_snapshot(source.catalog)
    canonical_before = {
        key: source.storage.read_bytes(key)
        for key in source.storage.keys("manifests/")
    }
    canonical_before.update({
        key: source.storage.read_bytes(key)
        for key in source.storage.keys("tombstones/")
    })
    canonical_before.update({
        key: source.storage.read_bytes(key)
        for key in source.storage.keys("objects/")
    })
    backup = Storage(_settings(disposable_backends, disposable_backends["database_url"], root, disposable_backends["backup_bucket"]))
    backup.ensure_bucket()

    def dump(_url):
        result = subprocess.run(
            ["docker", "compose", "-f", str(disposable_backends["compose"]), "exec", "-T", "postgres",
             "pg_dump", "--dbname", f"postgresql://phase9:phase9@localhost:5432/{disposable_backends['database']}",
             "--format=custom", "--no-owner", "--no-acl"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={**os.environ, "COMPOSE_PROJECT_NAME": disposable_backends["project"]},
        )
        return result.stdout

    paused = create_recovery_checkpoint(source.storage, backup, "integration", database_url="unused", dump_runner=dump, stop_after=1)
    assert paused["status"] == "paused", paused
    first = create_recovery_checkpoint(source.storage, backup, "integration", database_url="unused", dump_runner=dump)
    repeated = create_recovery_checkpoint(source.storage, backup, "integration", database_url="unused", dump_runner=lambda _: b"ignored")
    assert first["status"] == repeated["status"] == "complete", first
    assert canonical_json(first) == canonical_json(repeated)
    assert _operational_snapshot(source.catalog) == operational_before
    assert {
        key: source.storage.read_bytes(key)
        for key in canonical_before
    } == canonical_before

    restored_bucket = f"phase9-restored-{uuid4().hex}"
    restored_storage = Storage(_settings(disposable_backends, disposable_backends["database_url"], root, restored_bucket))
    restored_storage.ensure_bucket()
    restored = restore_recovery_checkpoint(backup, restored_storage, "indexes/recovery-checkpoints/integration.json")
    assert restored["status"] == "complete", restored
    dump_db = f"phase9_dump_{uuid4().hex[:16]}"
    dump_url = _create_database(disposable_backends["admin_url"], dump_db)

    def restore_dump(body):
        subprocess.run(
            ["docker", "compose", "-f", str(disposable_backends["compose"]), "exec", "-T", "postgres",
             "pg_restore", "--dbname", f"postgresql://phase9:phase9@localhost:5432/{dump_db}",
             "--no-owner", "--no-acl"],
            input=body, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={**os.environ, "COMPOSE_PROJECT_NAME": disposable_backends["project"]},
        )

    dump_result = restore_postgres_dump(
        backup, "indexes/recovery-checkpoints/integration.json", restore_dump
    )
    assert dump_result["status"] == "complete", dump_result
    dump_catalog = Service(_settings(disposable_backends, dump_url, root, restored_bucket)).catalog
    assert dump_catalog.existing_library_id() == source.library_id
    assert dump_catalog.counts() == source.catalog.counts()
    fresh_url = _create_database(disposable_backends["admin_url"], f"phase9_restore_{uuid4().hex[:16]}")
    rebuilt = Service(_settings(disposable_backends, fresh_url, root, restored_bucket))
    rebuilt.initialize(str(source.library_id))
    report = rebuild_from_s3(restored_storage, rebuilt.catalog, checkpoint_id="restored", resume=False)
    assert report["status"] == "complete", report
    assert compare_projections(source.catalog, rebuilt.catalog)["match"]

    malformed_key = "manifests/assets/bad/1.json"
    source.storage.put(malformed_key, b"{}", "application/json")
    failed = create_recovery_checkpoint(source.storage, backup, "malformed", resume=False)
    assert failed["status"] == "failed"
    source.storage.delete(malformed_key)
