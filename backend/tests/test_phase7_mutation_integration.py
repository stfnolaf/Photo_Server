"""Disposable PostgreSQL/S3 Phase 7 mutation acceptance suite."""

from __future__ import annotations

import os
import socket
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import urlopen
from uuid import UUID, uuid4, uuid5

import psycopg
import pytest
from PIL import Image
from sqlalchemy import insert, select

from photo_server.catalog import analysis_runs, faces, jobs, people
from photo_server.config import LibraryError, Settings
from photo_server.models import Mutation
from photo_server.service import Service
from photo_server.state import mutate, mutate_face

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("PHOTO_RUN_PHASE7_MUTATION_INTEGRATION") != "1",
        reason="Set PHOTO_RUN_PHASE7_MUTATION_INTEGRATION=1 for disposable Phase 7 acceptance",
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
            urlopen(endpoint, timeout=1).close()
            return
        except OSError:
            time.sleep(0.5)
    raise RuntimeError("disposable S3 did not become ready")


@pytest.fixture
def disposable_backends(tmp_path: Path):
    project = f"photo-phase7-{uuid4().hex}"
    database = f"phase7_{uuid4().hex[:16]}"
    bucket = f"phase7-{uuid4().hex}"
    pg_port, s3_port = _free_port(), _free_port()
    compose = tmp_path / "phase7-compose.yaml"
    compose.write_text(
        f"""services:
  postgres:
    image: postgres:17
    environment:
      POSTGRES_USER: phase7
      POSTGRES_PASSWORD: phase7
      POSTGRES_DB: {database}
    ports: [\"127.0.0.1:{pg_port}:5432\"]
    healthcheck:
      test: [CMD-SHELL, pg_isready -U phase7 -d {database}]
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
        _wait_postgres(f"postgresql://phase7:phase7@127.0.0.1:{pg_port}/{database}")
        _wait_s3(f"http://127.0.0.1:{s3_port}")
        yield {
            "database_url": f"postgresql+psycopg://phase7:phase7@127.0.0.1:{pg_port}/{database}",
            "bucket": bucket,
            "s3_endpoint": f"http://127.0.0.1:{s3_port}",
            "root": tmp_path,
        }
    finally:
        subprocess.run(
            ["docker", "compose", "-f", str(compose), "down", "-v", "--remove-orphans"],
            check=False, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )


def _service(backends) -> Service:
    root = backends["root"]
    service = Service(Settings(
        _env_file=None, s3_endpoint=backends["s3_endpoint"], s3_bucket=backends["bucket"],
        s3_anonymous=True, database_url=backends["database_url"],
        import_root=root / "imports", data_dir=root / "runtime",
    ))
    service.initialize()
    (root / "imports").mkdir()
    Image.new("RGB", (16, 16), "red").save(root / "imports" / "one.jpg", format="JPEG")
    Image.new("RGB", (16, 16), "blue").save(root / "imports" / "two.jpg", format="JPEG")
    imported = service.import_batch(["one.jpg", "two.jpg"], uuid4())
    assert all(item["status"] == "imported" for item in imported["results"]), imported
    return service


def test_full_disposable_mutation_lifecycle(disposable_backends):
    service = _service(disposable_backends)
    asset_ids = [item["assetId"] for item in service.catalog.list_assets()]
    asset_id, second_asset_id = asset_ids
    current = service.catalog.get(asset_id)
    assert current.revision == 1
    with service.catalog.engine.connect() as connection:
        queue_before = [dict(row) for row in connection.execute(select(jobs)).mappings()]

    operation_id = uuid4()
    patch = Mutation(action="asset.patch", entity_id=current.asset_id,
                     changes={"rating": 4, "favorite": True}, expected_revision=1)
    result = mutate(service, operation_id, patch)
    assert result["revision"] == 2
    assert service.storage.head(f"manifests/assets/{asset_id}/2.json") is not None
    assert service.catalog.get(asset_id).user_state.rating == 4

    # Live reads are still PostgreSQL reads, even when the S3 client is absent.
    original_storage = service.storage
    service.storage = None
    assert service.catalog.get(asset_id).user_state.favorite is True
    service.storage = original_storage
    assert mutate(service, operation_id, patch) == result
    assert len(list(service.storage.keys(f"manifests/assets/{asset_id}/"))) == 2
    with pytest.raises(LibraryError, match="Revision changed"):
        mutate(service, uuid4(), Mutation(action="asset.patch", entity_id=current.asset_id,
                                           changes={"rating": 2}, expected_revision=1))

    # Queue rows are operational and must not be rewritten as durable state.
    with service.catalog.engine.connect() as connection:
        assert [dict(row) for row in connection.execute(select(jobs)).mappings()] == queue_before

    # S3 failure occurs before the PostgreSQL projection call.
    old_put = service.dual_write._put_immutable
    service.dual_write._put_immutable = lambda *_args: (_ for _ in ()).throw(RuntimeError("s3 down"))
    with pytest.raises(RuntimeError, match="s3 down"):
        mutate(service, uuid4(), Mutation(action="asset.patch", entity_id=current.asset_id,
                                           changes={"rating": 3}, expected_revision=2))
    service.dual_write._put_immutable = old_put
    assert service.catalog.get(asset_id).revision == 2

    # A projection failure leaves a verified S3 revision that the same
    # operation can replay without creating another revision.
    recovery_id = uuid4()
    recovery_mutation = Mutation(action="asset.patch", entity_id=current.asset_id,
                                 changes={"caption": "recover me"}, expected_revision=2)
    old_commit = service.catalog.commit_mutation
    service.catalog.commit_mutation = lambda *_args: (_ for _ in ()).throw(RuntimeError("projection down"))
    with pytest.raises(RuntimeError, match="projection down"):
        mutate(service, recovery_id, recovery_mutation)
    service.catalog.commit_mutation = old_commit
    assert service.catalog.get(asset_id).revision == 2
    assert mutate(service, recovery_id, recovery_mutation)["revision"] == 3

    album_id = uuid5(service.library_id, f"album:{uuid4()}")
    create = Mutation(action="album.create", entity_id=album_id,
                      changes={"name": "Trip", "description": "", "assetIds": asset_ids})
    mutate(service, uuid4(), create)
    reorder = Mutation(action="album.patch", entity_id=album_id,
                       changes={"assetIds": [second_asset_id, asset_id]}, expected_revision=1)
    mutate(service, uuid4(), reorder)
    assert service.catalog.get_album(str(album_id)).asset_ids == [UUID(second_asset_id), UUID(asset_id)]
    with pytest.raises(LibraryError, match="Revision changed"):
        mutate(service, uuid4(), reorder)
    mutate(service, uuid4(), Mutation(action="album.delete", entity_id=album_id,
                                      expected_revision=2))
    assert service.storage.head(f"tombstones/album/{album_id}/3.json") is not None
    mutate(service, uuid4(), Mutation(action="album.restore", entity_id=album_id,
                                      expected_revision=3))
    assert service.catalog.get_album(str(album_id)).deleted_at is None

    mutate(service, uuid4(), Mutation(action="asset.delete", entity_id=current.asset_id,
                                      expected_revision=3))
    assert service.storage.head(f"tombstones/asset/{asset_id}/4.json") is not None
    mutate(service, uuid4(), Mutation(action="asset.restore", entity_id=current.asset_id,
                                      expected_revision=4))
    assert service.catalog.get(asset_id).deleted_at is None

    person_id = uuid4()
    with service.catalog.engine.begin() as connection:
        connection.execute(insert(people).values(
            id=str(person_id), display_name="", created_at=datetime.now(UTC)
        ))
        target_person_id = uuid4()
        connection.execute(insert(people).values(
            id=str(target_person_id), display_name="Target", created_at=datetime.now(UTC)
        ))
        run_id, face_id = uuid4(), uuid4()
        connection.execute(insert(analysis_runs).values(
            id=str(run_id), asset_id=asset_id, analysis_type="fixture",
            model_name="fixture", model_version="1", pipeline_version="1",
            input_hash=service.catalog.get(asset_id).primary.sha256,
            object_key=f"analysis/{asset_id}/{run_id}.json", result={},
            searchable_text="", is_current=True, semantic_origin="computed",
            created_at=datetime.now(UTC),
        ))
        connection.execute(insert(faces).values(
            id=str(face_id), asset_id=asset_id, analysis_run_id=str(run_id),
            person_id=str(person_id), face_index=0,
            bounding_box={"x": 0, "y": 0, "width": 1, "height": 1},
            confidence=0.9, embedding=[0.1, 0.2],
        ))
    rename = {"action": "person.rename", "personId": str(person_id), "displayName": "Avery"}
    mutate_face(service, uuid4(), rename)
    assert list(service.storage.keys(f"manifests/people/{person_id}/"))
    mutate_face(service, uuid4(), {
        "action": "faces.move", "faceIds": [str(face_id)],
        "targetPersonId": str(target_person_id),
    })
    assert len(list(service.storage.keys(f"manifests/people/{target_person_id}/"))) == 1
    mutate_face(service, uuid4(), {
        "action": "person.merge", "sourcePersonId": str(person_id),
        "targetPersonId": str(target_person_id),
    })
    assert service.storage.head(f"tombstones/person/{person_id}/3.json") is not None
    assert UUID(str(person_id))
