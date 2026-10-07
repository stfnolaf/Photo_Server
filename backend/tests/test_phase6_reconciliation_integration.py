"""Disposable PostgreSQL/S3 Phase 6 acceptance.

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
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import urlopen
from uuid import uuid4

import psycopg
import pytest
from PIL import Image
from sqlalchemy import delete, insert, select, update

from photo_server.backfill import backfill_postgres_to_s3
from photo_server.catalog import album_assets, albums, analysis_runs, assets, faces, jobs, people
from photo_server.config import Settings
from photo_server.reconcile import reconcile_s3_to_postgres
from photo_server.service import Service

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("PHOTO_RUN_PHASE6_RECONCILIATION_INTEGRATION") != "1",
        reason="Set PHOTO_RUN_PHASE6_RECONCILIATION_INTEGRATION=1 for disposable Phase 6 acceptance",
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
    project = f"photo-phase6-{uuid4().hex}"
    database = f"phase6_{uuid4().hex[:16]}"
    bucket = f"phase6-{uuid4().hex}"
    pg_port, s3_port = _free_port(), _free_port()
    compose = tmp_path / "phase6-compose.yaml"
    compose.write_text(
        f"""services:
  postgres:
    image: postgres:17
    environment:
      POSTGRES_USER: phase6
      POSTGRES_PASSWORD: phase6
      POSTGRES_DB: {database}
    ports: [\"127.0.0.1:{pg_port}:5432\"]
    healthcheck:
      test: [CMD-SHELL, pg_isready -U phase6 -d {database}]
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
        database_url = f"postgresql+psycopg://phase6:phase6@127.0.0.1:{pg_port}/{database}"
        _wait_postgres(f"postgresql://phase6:phase6@127.0.0.1:{pg_port}/{database}")
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
    person_id, face_id, run_id, album_id = str(uuid4()), str(uuid4()), str(uuid4()), str(uuid4())
    input_hash = service.catalog.get(asset_ids[0]).primary.sha256
    artifact_source = f"analysis/{asset_ids[0]}/fixture/{run_id}.json"
    service.storage.put_json(artifact_source, {"assetId": asset_ids[0], "faces": 1})
    created = datetime(2026, 10, 6, 19, 0, tzinfo=UTC)
    with service.catalog.engine.begin() as connection:
        connection.execute(insert(people).values(id=person_id, display_name="Avery", created_at=created))
        connection.execute(insert(analysis_runs).values(
            id=run_id, asset_id=asset_ids[0], analysis_type="photo-ai",
            model_name="fixture", model_version="1", pipeline_version="fixture-v1",
            input_hash=input_hash, object_key=artifact_source,
            result={"description": "fixture"}, searchable_text="fixture",
            is_current=True, semantic_origin="computed", created_at=created,
        ))
        connection.execute(insert(faces).values(
            id=face_id, asset_id=asset_ids[0], analysis_run_id=run_id,
            person_id=person_id, face_index=0,
            bounding_box={"x": 1, "y": 2, "width": 3, "height": 4},
            confidence=0.99, embedding=[0.1, 0.2],
        ))
        connection.execute(insert(albums).values(
            id=album_id, state_revision=1,
            state={"schemaVersion": 1, "libraryId": str(service.library_id),
                   "albumId": album_id, "revision": 1, "previousRevision": None,
                   "operationId": str(uuid4()), "name": "Fixture", "description": "Phase 6",
                   "assetIds": asset_ids, "deletedAt": None,
                   "mutation": {"action": "album.create", "entityId": album_id,
                                 "changes": {}, "expectedRevision": None}},
            deleted_at=None,
        ))
        connection.execute(insert(album_assets), [
            {"album_id": album_id, "asset_id": asset_ids[0], "position": 0},
            {"album_id": album_id, "asset_id": asset_ids[1], "position": 1},
        ])
        deleted_manifest = dict(connection.scalar(select(assets.c.manifest).where(assets.c.id == asset_ids[1])))
        deleted_manifest["deletedAt"] = "2026-10-06T20:00:00Z"
        connection.execute(update(assets).where(assets.c.id == asset_ids[1]).values(
            deleted_at="2026-10-06T20:00:00Z", manifest=deleted_manifest
        ))
    for key in list(service.storage.keys("manifests/")):
        service.storage.delete(key)
    exported = backfill_postgres_to_s3(service.catalog, service.storage, resume=False)
    assert exported["status"] == "complete", exported

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
        connection.execute(delete(faces).where(faces.c.id == face_id))
        connection.execute(delete(analysis_runs).where(analysis_runs.c.id == run_id))
        connection.execute(delete(people).where(people.c.id == person_id))
        connection.execute(update(album_assets).where(
            (album_assets.c.album_id == album_id) & (album_assets.c.asset_id == asset_ids[0])
        ).values(position=1))

    face_manifest_key = next(service.storage.keys("manifests/faces/"))
    face_manifest_body = service.storage.read_bytes(face_manifest_key)
    service.storage.delete(face_manifest_key)
    processing_manifest_key = next(service.storage.keys("manifests/processing/"))
    processing_manifest_body = service.storage.read_bytes(processing_manifest_key)
    service.storage.delete(processing_manifest_key)

    before_keys = sorted(service.storage.keys(""))
    dry = reconcile_s3_to_postgres(service.storage, service.catalog, checkpoint_id="dry", dry_run=True)
    assert dry["status"] in {"complete", "failed"}, dry
    assert dry["dryRun"] is True
    assert sorted(service.storage.keys("")) == before_keys
    assert dry["missing"] or dry["divergent"] or dry["orphaned"]
    assert dry["unresolved"]
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
    service.storage.put(face_manifest_key, face_manifest_body, "application/json")
    service.storage.put(processing_manifest_key, processing_manifest_body, "application/json")

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

    # Immutable conflict detection remains a Phase 5 writer invariant.
    service.storage.client.put_object(
        Bucket=service.storage.bucket, Key=missing_key, Body=missing_body + b"conflict"
    )
    conflict = backfill_postgres_to_s3(
        service.catalog, service.storage, checkpoint_id="immutable-conflict", resume=False
    )
    assert conflict["status"] == "failed"
    service.storage.client.put_object(
        Bucket=service.storage.bucket, Key=missing_key, Body=missing_body
    )
    service.storage.put(missing_key, missing_body, "application/json")
