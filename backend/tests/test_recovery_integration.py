"""Disposable S3 recovery and projection rebuild acceptance.

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
from pathlib import Path
from urllib.request import urlopen
from uuid import UUID, uuid4

import psycopg
import pytest
from PIL import Image
from sqlalchemy import select
from sqlalchemy.engine import make_url

from photo_server.catalog import (
    ai_stage_jobs,
    jobs,
    onboarding_jobs,
    upload_batches,
)
from photo_server.config import Settings
from photo_server.fingerprints import Fingerprint
from photo_server.manifests import canonical_json
from photo_server.models import Mutation
from photo_server.rebuild import compare_projections, rebuild_from_s3
from photo_server.recovery import (
    create_recovery_checkpoint,
    restore_postgres_dump,
    restore_recovery_checkpoint,
)
from photo_server.service import Service
from photo_server.state import mutate
from photo_server.storage import Storage

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("PHOTO_RUN_RECOVERY_INTEGRATION") != "1",
        reason="Set PHOTO_RUN_RECOVERY_INTEGRATION=1 for disposable recovery acceptance",
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
    project = f"photo-recovery-{uuid4().hex}"
    database = f"recovery_{uuid4().hex[:16]}"
    bucket = f"recovery-{uuid4().hex}"
    backup_bucket = f"recovery-backup-{uuid4().hex}"
    pg_port, s3_port = _port(), _port()
    compose = tmp_path / "recovery-compose.yaml"
    compose.write_text(
        f"""services:
  postgres:
    image: postgres:17
    environment:
      POSTGRES_USER: recovery
      POSTGRES_PASSWORD: recovery
      POSTGRES_DB: {database}
    ports: [\"127.0.0.1:{pg_port}:5432\"]
    healthcheck:
      test: [CMD-SHELL, pg_isready -U recovery -d {database}]
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
        database_url = f"postgresql+psycopg://recovery:recovery@127.0.0.1:{pg_port}/{database}"
        admin_url = f"postgresql://recovery:recovery@127.0.0.1:{pg_port}/{database}"
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
    for name, color in (
        ("IMG_0001.jpg", "red"),
        ("IMG_0002.jpg", "orange"),
        ("delete.jpg", "blue"),
    ):
        Image.new("RGB", (32, 24), color).save(imports / name, format="JPEG")
    imported = source.import_batch(
        ["IMG_0001.jpg", "IMG_0002.jpg", "delete.jpg"], uuid4()
    )
    assert all(item["status"] == "imported" for item in imported["results"]), imported
    asset_ids = [item["assetId"] for item in imported["results"]]

    for asset_id in asset_ids[:2]:
        mutate(source, uuid4(), Mutation(
            action="asset.metadata",
            entity_id=asset_id,
            expected_revision=1,
            changes={
                "metadata": {"Make": "Fixture", "Model": "Camera"},
                "captureTime": "2026-01-01T12:00:00Z",
            },
        ))
        source.publish_fingerprint(
            asset_id,
            Fingerprint(
                phash="0000000000000000",
                dhash="0000000000000000",
                width=32,
                height=24,
                chroma_histogram="00" * 12,
            ),
        )
    burst = source.catalog.burst_detail(asset_ids[0])
    assert burst is not None and len(burst["frames"]) == 2
    mutate(source, uuid4(), Mutation(
        action="burst.setRepresentative",
        entity_id=UUID(burst["burstId"]),
        changes={"representativeAssetId": asset_ids[1]},
    ))
    assert source.catalog.burst_detail(asset_ids[0])["representativeAssetId"] == asset_ids[1]

    album_id = uuid4()
    mutate(source, uuid4(), Mutation(
        action="album.create", entity_id=album_id,
        changes={"name": "Fixture", "description": "Recovery fixture", "assetIds": asset_ids},
    ))
    mutate(source, uuid4(), Mutation(
        action="asset.delete", entity_id=asset_ids[2], changes={}, expected_revision=1,
    ))
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
             "pg_dump", "--dbname", f"postgresql://recovery:recovery@localhost:5432/{disposable_backends['database']}",
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

    restored_bucket = f"recovery-restored-{uuid4().hex}"
    restored_storage = Storage(_settings(disposable_backends, disposable_backends["database_url"], root, restored_bucket))
    restored_storage.ensure_bucket()
    restored = restore_recovery_checkpoint(backup, restored_storage, "indexes/recovery-checkpoints/integration.json")
    assert restored["status"] == "complete", restored
    dump_db = f"recovery_dump_{uuid4().hex[:16]}"
    dump_url = _create_database(disposable_backends["admin_url"], dump_db)

    def restore_dump(body):
        subprocess.run(
            ["docker", "compose", "-f", str(disposable_backends["compose"]), "exec", "-T", "postgres",
             "pg_restore", "--dbname", f"postgresql://recovery:recovery@localhost:5432/{dump_db}",
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
    fresh_url = _create_database(disposable_backends["admin_url"], f"recovery_restore_{uuid4().hex[:16]}")
    rebuilt = Service(_settings(disposable_backends, fresh_url, root, restored_bucket))
    rebuilt.initialize(str(source.library_id))
    report = rebuild_from_s3(restored_storage, rebuilt.catalog, checkpoint_id="restored", resume=False)
    assert report["status"] == "complete", json.dumps(report, indent=2, sort_keys=True)
    assert report["queuesRestored"] == {"assets": 3, "fingerprintPending": 1, "aiPending": 3}
    rebuilt_operational = _operational_snapshot(rebuilt.catalog)
    assert len(rebuilt_operational["jobs"]) == 6
    assert len(rebuilt_operational["ai_stage_jobs"]) == 6
    assert rebuilt_operational["onboarding_jobs"] == []
    assert rebuilt_operational["upload_batches"] == []
    comparison = compare_projections(source.catalog, rebuilt.catalog)
    if not comparison["match"]:
        comparison["sourceAssets"] = [item.document() for item in source.catalog.all_assets()]
        comparison["rebuiltAssets"] = [item.document() for item in rebuilt.catalog.all_assets()]
    assert comparison["match"], json.dumps(comparison, indent=2, sort_keys=True)

    malformed_key = "manifests/assets/bad/1.json"
    source.storage.put(malformed_key, b"{}", "application/json")
    failed = create_recovery_checkpoint(source.storage, backup, "malformed", resume=False)
    assert failed["status"] == "failed"
    source.storage.delete(malformed_key)
