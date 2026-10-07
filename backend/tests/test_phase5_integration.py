"""Disposable end-to-end Phase 5 acceptance.

This suite owns its random Compose project, ports, bucket, databases, and
temporary directory. It never reads or mutates the production Compose stack.
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
from sqlalchemy import insert, select, update
from sqlalchemy.engine import make_url

from photo_server.backfill import backfill_postgres_to_s3
from photo_server.catalog import album_assets, albums, analysis_runs, assets, faces, people
from photo_server.config import Settings
from photo_server.rebuild import compare_projections, rebuild_from_s3
from photo_server.service import Service

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("PHOTO_RUN_PHASE5_INTEGRATION") != "1",
        reason="Set PHOTO_RUN_PHASE5_INTEGRATION=1 for disposable Phase 5 acceptance",
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
    raise RuntimeError("disposable S3 endpoint did not become ready")


@pytest.fixture
def disposable_backends(tmp_path):
    project = f"photo-phase5-{uuid4().hex}"
    database = f"phase5_{uuid4().hex[:16]}"
    bucket = f"phase5-{uuid4().hex}"
    pg_port, s3_port = _free_port(), _free_port()
    compose = tmp_path / "compose.yaml"
    compose.write_text(
        f"""services:
  postgres:
    image: postgres:17
    environment:
      POSTGRES_USER: phase5
      POSTGRES_PASSWORD: phase5
      POSTGRES_DB: {database}
    ports: [\"127.0.0.1:{pg_port}:5432\"]
    healthcheck:
      test: [CMD-SHELL, pg_isready -U phase5 -d {database}]
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
            check=True,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        database_url = f"postgresql+psycopg://phase5:phase5@127.0.0.1:{pg_port}/{database}"
        admin_url = f"postgresql://phase5:phase5@127.0.0.1:{pg_port}/{database}"
        _wait_postgres(admin_url)
        _wait_s3(f"http://127.0.0.1:{s3_port}")
        yield {
            "bucket": bucket,
            "database_url": database_url,
            "admin_url": admin_url,
            "s3_endpoint": f"http://127.0.0.1:{s3_port}",
            "root": tmp_path,
        }
    finally:
        subprocess.run(
            ["docker", "compose", "-f", str(compose), "down", "-v", "--remove-orphans"],
            check=False,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )


def _settings(backends, database_url: str, root: Path) -> Settings:
    return Settings(
        _env_file=None,
        s3_endpoint=backends["s3_endpoint"],
        s3_bucket=backends["bucket"],
        s3_anonymous=True,
        database_url=database_url,
        import_root=root / "imports",
        data_dir=root / "runtime" / hashlib.sha256(database_url.encode()).hexdigest()[:8],
    )


def _seed_people_and_processing(service: Service, asset_id: str):
    person_id, face_id, run_id = str(uuid4()), str(uuid4()), str(uuid4())
    created = datetime(2026, 10, 6, 19, 0, tzinfo=UTC)
    input_hash = service.catalog.get(asset_id).primary.sha256
    artifact_key = f"analysis/{asset_id}/photo-ai-v1/{run_id}.json"
    service.storage.put_json(
        artifact_key,
        {
            "schemaVersion": 1,
            "runId": run_id,
            "assetId": asset_id,
            "inputSha256": input_hash,
            "pipelineVersion": "photo-ai-v1",
            "faces": [
                {
                    "box": {"x": 1, "y": 2, "width": 3, "height": 4},
                    "confidence": 0.99,
                    "embedding": [0.1, 0.2],
                }
            ],
            "semantic": {"description": "fixture"},
        },
    )
    with service.catalog.engine.begin() as connection:
        connection.execute(
            insert(people).values(id=person_id, display_name="Avery", created_at=created)
        )
        connection.execute(
            insert(analysis_runs).values(
                id=run_id,
                asset_id=asset_id,
                analysis_type="photo-ai",
                model_name="fixture-model",
                model_version="fixture-v1",
                pipeline_version="photo-ai-v1",
                input_hash=input_hash,
                object_key=artifact_key,
                result={"description": "fixture", "faceCount": 1},
                searchable_text="fixture",
                is_current=True,
                semantic_origin="computed",
                created_at=created,
            )
        )
        connection.execute(
            insert(faces).values(
                id=face_id,
                asset_id=asset_id,
                analysis_run_id=run_id,
                person_id=person_id,
                face_index=0,
                bounding_box={"x": 1, "y": 2, "width": 3, "height": 4},
                confidence=0.99,
                embedding=[0.1, 0.2],
            )
        )
    return person_id, face_id, run_id


def test_phase5_full_disposable_people_faces_processing_acceptance(disposable_backends):
    root = disposable_backends["root"]
    source = Service(_settings(disposable_backends, disposable_backends["database_url"], root))
    source.initialize()
    imports = root / "imports"
    imports.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (16, 16), "red").save(imports / "one.jpg", format="JPEG")
    Image.new("RGB", (16, 16), "blue").save(imports / "two.jpg", format="JPEG")
    imported = source.import_batch(["one.jpg", "two.jpg"], uuid4())
    assert all(item["status"] == "imported" for item in imported["results"]), imported
    asset_ids = [item["assetId"] for item in imported["results"]]
    person_id, face_id, run_id = _seed_people_and_processing(source, asset_ids[0])
    album_id = str(uuid4())
    with source.catalog.engine.begin() as connection:
        connection.execute(
            insert(albums).values(
                id=album_id,
                state_revision=1,
                state={
                    "schemaVersion": 1,
                    "libraryId": str(source.library_id),
                    "albumId": album_id,
                    "revision": 1,
                    "previousRevision": None,
                    "operationId": str(uuid4()),
                    "name": "Fixture",
                    "description": "Fixture album",
                    "assetIds": asset_ids,
                    "deletedAt": None,
                    "mutation": {
                        "action": "album.create",
                        "entityId": album_id,
                        "changes": {},
                        "expectedRevision": None,
                    },
                },
                deleted_at=None,
            )
        )
        connection.execute(
            insert(album_assets),
            [
                {"album_id": album_id, "asset_id": asset_ids[0], "position": 0},
                {"album_id": album_id, "asset_id": asset_ids[1], "position": 1},
            ],
        )
        deleted_at = "2026-10-06T20:00:00Z"
        deleted_manifest = dict(
            connection.scalar(select(assets.c.manifest).where(assets.c.id == asset_ids[1]))
        )
        deleted_manifest["deletedAt"] = deleted_at
        connection.execute(
            update(assets)
            .where(assets.c.id == asset_ids[1])
            .values(deleted_at=deleted_at, manifest=deleted_manifest)
        )

    storage = source.storage
    # Phase 3 dual-write manifests are intentionally removed in this disposable
    # fixture; the immutable Phase 5 backfill must create the namespace and must
    # still preserve the original content objects.
    for key in list(storage.keys("manifests/")):
        storage.delete(key)
    before = sorted(storage.keys(""))
    dry = backfill_postgres_to_s3(
        source.catalog, storage, checkpoint_id="dry", resume=False, dry_run=True
    )
    assert dry["status"] == "complete", dry
    assert sorted(storage.keys("")) == before
    paused = backfill_postgres_to_s3(
        source.catalog, storage, checkpoint_id="phase5", resume=False, stop_after=1
    )
    assert paused["status"] == "paused", paused
    completed = backfill_postgres_to_s3(
        source.catalog, storage, checkpoint_id="phase5", resume=True
    )
    assert completed["status"] == "complete", completed
    repeated = backfill_postgres_to_s3(source.catalog, storage, checkpoint_id="phase5", resume=True)
    assert repeated["status"] == "complete", repeated

    asset_key = f"manifests/assets/{asset_ids[0]}/1.json"
    original_manifest = storage.read_bytes(asset_key)
    storage.client.put_object(Bucket=storage.bucket, Key=asset_key, Body=original_manifest + b"x")
    corrupted = rebuild_from_s3(storage, source.catalog, checkpoint_id="corrupt", resume=False)
    assert corrupted["status"] == "failed"
    assert corrupted["checksumMismatches"] or corrupted["malformedManifests"]
    storage.client.put_object(Bucket=storage.bucket, Key=asset_key, Body=original_manifest)

    primary = source.catalog.get(asset_ids[0]).primary
    object_key = f"objects/{primary.sha256}"
    original_object = storage.read_bytes(object_key)
    storage.client.put_object(
        Bucket=storage.bucket, Key=object_key, Body=original_object + b"corrupt"
    )
    object_corrupt = rebuild_from_s3(
        storage, source.catalog, checkpoint_id="object-corrupt", resume=False
    )
    assert object_corrupt["status"] == "failed" and object_corrupt["checksumMismatches"]
    storage.client.put_object(Bucket=storage.bucket, Key=object_key, Body=original_object)

    processing_key = next(storage.keys("manifests/processing/"))
    processing_body = storage.read_bytes(processing_key)
    storage.delete(processing_key)
    missing_processing = rebuild_from_s3(
        storage, source.catalog, checkpoint_id="missing-processing", resume=False
    )
    assert missing_processing["status"] == "failed"
    assert any(
        item["category"] == "processing" for item in missing_processing["unresolvedReferences"]
    )
    storage.put(processing_key, processing_body, "application/json")

    storage.client.put_object(
        Bucket=storage.bucket, Key=asset_key, Body=original_manifest + b"conflict"
    )
    immutable_conflict = backfill_postgres_to_s3(
        source.catalog, storage, checkpoint_id="immutable-conflict", resume=False
    )
    assert immutable_conflict["status"] == "failed"
    conflict_counts = immutable_conflict["counts"]["conflictsByCategory"]
    assert conflict_counts["immutableManifestConflicts"] + conflict_counts["sizeMismatches"] >= 1
    storage.client.put_object(Bucket=storage.bucket, Key=asset_key, Body=original_manifest)

    admin = make_url(disposable_backends["database_url"]).set(
        drivername="postgresql", database="postgres"
    )
    fresh_name = f"phase5_fresh_{uuid4().hex[:16]}"
    with psycopg.connect(
        admin.render_as_string(hide_password=False), autocommit=True
    ) as connection:
        connection.execute(f'CREATE DATABASE "{fresh_name}"')
    try:
        fresh_url = (
            make_url(disposable_backends["database_url"])
            .set(database=fresh_name)
            .render_as_string(hide_password=False)
        )
        fresh = Service(_settings(disposable_backends, fresh_url, root))
        fresh.initialize()
        rebuilt = rebuild_from_s3(storage, fresh.catalog, checkpoint_id="fresh", resume=False)
        assert rebuilt["status"] == "complete", rebuilt
        assert rebuilt["projectedPeople"] == 1
        comparison = compare_projections(source.catalog, fresh.catalog)
        assert comparison["match"], comparison
        assert fresh.catalog.all_people()[0]["displayName"] == "Avery"
        assert fresh.catalog.all_people()[0]["faceIds"] == [face_id]
        with fresh.catalog.engine.connect() as connection:
            assert (
                connection.scalar(select(analysis_runs.c.id).where(analysis_runs.c.id == run_id))
                == run_id
            )
            assert connection.scalar(select(faces.c.id).where(faces.c.id == face_id)) == face_id
    finally:
        with psycopg.connect(
            admin.render_as_string(hide_password=False), autocommit=True
        ) as connection:
            connection.execute(f'DROP DATABASE IF EXISTS "{fresh_name}" WITH (FORCE)')
