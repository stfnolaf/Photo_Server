"""PostgreSQL-authority tests against isolated S3 buckets and databases."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from test_integration import backend as backend  # Reuse disposable infrastructure.
from test_integration import photo, pytestmark, set_legacy_state  # noqa: F401

from photo_server.api import create_app
from photo_server.browsing import BrowseQuery
from photo_server.config import LibraryError
from photo_server.export import export_library
from photo_server.migrations import available_migrations
from photo_server.models import Mutation, UserState
from photo_server.state import mutate
from photo_server.worker import run_once


def imported(backend, name="sample.JPG", color="red"):
    result = backend.service.import_batch([photo(backend.root, name, color).name], uuid4())
    assert result["results"][0]["status"] == "imported", result
    return UUID(result["results"][0]["assetId"])


def patch(service, asset_id, operation_id=None, **changes):
    return mutate(
        service,
        operation_id or uuid4(),
        Mutation(action="asset.patch", entity_id=asset_id, changes=changes),
    )


def test_mutation_and_retry_record_are_one_database_transaction(backend, monkeypatch):
    service = backend.service
    asset_id = imported(backend)
    operation = uuid4()
    before = service.catalog.get(str(asset_id)).document()
    apply = service.catalog._apply

    def fail_after_update(connection, snapshot):
        apply(connection, snapshot)
        raise RuntimeError("simulated transaction failure")

    with monkeypatch.context() as context:
        context.setattr(service.catalog, "_apply", fail_after_update)
        with pytest.raises(RuntimeError, match="transaction failure"):
            patch(service, asset_id, operation, rating=5)

    assert service.catalog.get(str(asset_id)).document() == before
    assert service.catalog.operation(operation) is None
    result = patch(service, asset_id, operation, rating=5)
    assert result["rating"] == 5 and result["revision"] == 2
    assert patch(service, asset_id, operation, rating=5) == result
    with pytest.raises(LibraryError, match="different request"):
        patch(service, asset_id, operation, rating=1)
    assert list(service.storage.keys("state/")) == []


def test_parallel_patches_serialize_without_losing_fields(backend):
    service = backend.service
    asset_id = imported(backend)
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(
            executor.map(
                lambda changes: patch(service, asset_id, **changes),
                [
                    {"rating": 3},
                    {"favorite": True},
                    {"caption": "Coast"},
                    {"keywords": ["sea"]},
                ],
            )
        )
    assert sorted(result["revision"] for result in results) == [2, 3, 4, 5]
    assert service.catalog.user_state(str(asset_id)) == UserState(
        rating=3, favorite=True, caption="Coast", keywords=["sea"]
    ).document()


def test_metadata_albums_tombstones_and_database_backed_export(backend, tmp_path):
    import json

    service = backend.service
    first, second = imported(backend), imported(backend, "second.JPG", "blue")
    originals = set(service.storage.keys("originals/"))
    with TestClient(create_app(service.settings)) as client:
        metadata = {
            "operationId": str(uuid4()),
            "rating": 4,
            "favorite": True,
            "caption": "Rock & sea <sunset>",
            "keywords": ["coast", "holiday"],
            "location": {"name": "North shore", "latitude": 45.5, "longitude": -122.5},
        }
        response = client.patch(f"/assets/{first}/metadata", json=metadata)
        assert response.status_code == 200, response.text
        assert client.get("/library/assets?q=north%20shore").json()["total"] == 1
        create = {
            "operationId": str(uuid4()),
            "name": "Trip",
            "assetIds": [str(second), str(first)],
        }
        album = client.post("/albums", json=create).json()
        assert client.post("/albums", json=create).json() == album
        album_id = album["albumId"]
        edit = {
            "operationId": str(uuid4()),
            "name": "Coast trip",
            "description": "A holiday",
            "assetIds": [str(first), str(second)],
            "expectedRevision": 1,
        }
        response = client.patch(f"/albums/{album_id}", json=edit)
        assert response.status_code == 200, response.text
        assert response.json()["assetIds"] == [str(first), str(second)]
        assert client.patch(f"/albums/{album_id}", json=edit).json() == response.json()
        delete = {"operationId": str(uuid4())}
        assert client.request("DELETE", f"/assets/{first}", json=delete).status_code == 200
        assert client.get("/library/assets").json()["total"] == 1
        assert client.get("/library/assets?deleted=true").json()["total"] == 1
        assert (
            client.patch(
                f"/assets/{first}/metadata",
                json={"operationId": str(uuid4()), "rating": 1},
            ).status_code
            == 409
        )
        assert (
            client.post(f"/assets/{first}/restore", json={"operationId": str(uuid4())}).status_code
            == 200
        )
        assert client.get(f"/assets/{first}").json()["userState"]["caption"] == metadata["caption"]
        assert (
            client.request(
                "DELETE", f"/albums/{album_id}", json={"operationId": str(uuid4())}
            ).status_code
            == 200
        )
        assert client.get("/albums") .json() == []
        assert (
            client.post(
                f"/albums/{album_id}/restore", json={"operationId": str(uuid4())}
            ).status_code
            == 200
        )
        assert (
            client.request(
                "DELETE", f"/assets/{second}", json={"operationId": str(uuid4())}
            ).status_code
            == 200
        )
        expected_album = client.get(f"/albums/{album_id}").json()

    assert set(service.storage.keys("originals/")) == originals
    assert list(service.storage.keys("state/")) == []
    assert service.catalog.browse(BrowseQuery())["total"] == 1
    assert service.catalog.browse(BrowseQuery(deleted=True))["total"] == 1
    destination = tmp_path / "export-active"
    assert export_library(service.settings, destination)["exported"] == 1
    exported = json.loads((destination / str(first) / "manifest.json").read_text())
    assert exported["userState"]["caption"] == metadata["caption"]
    library = json.loads((destination / "library-state.json").read_text())
    assert library["albums"] == [expected_album]
    assert library["trashedAssets"][0]["assetId"] == str(second)
    assert export_library(
        service.settings, tmp_path / "export-all", include_trash=True
    )["exported"] == 2


def test_operation_ids_are_global_across_assets_and_albums(backend):
    service = backend.service
    asset_id = imported(backend)
    operation, album_id = uuid4(), uuid4()
    request = Mutation(action="album.create", entity_id=album_id, changes={"name": "Trip"})
    first = mutate(service, operation, request)
    assert mutate(service, operation, request) == first
    with pytest.raises(LibraryError, match="different request"):
        patch(service, asset_id, operation, rating=5)


def test_phase_two_rating_and_favorite_are_promoted_inside_postgres(backend):
    service = backend.service
    asset_id = imported(backend)
    set_legacy_state(service, str(asset_id), {"rating": 4, "favorite": True})
    assert service.catalog.migrate_legacy_user_state() == 1
    current = service.catalog.get(str(asset_id))
    assert current.revision == 2
    assert current.user_state == UserState(rating=4, favorite=True)
    assert service.catalog.migrate_legacy_user_state() == 0
    assert list(service.storage.keys("state/")) == []


def test_processing_endpoint_refreshes_lens_and_exposure_fields(backend, monkeypatch):
    service = backend.service
    asset_id = imported(backend)
    extracted = {
        "FileType": "JPEG",
        "MIMEType": "image/jpeg",
        "Make": "Sony",
        "Model": "A7",
        "LensMake": "Sigma",
        "LensModel": "24-70mm F2.8 DG DN",
        "lensDisplay": "Sigma 24-70mm F2.8 DG DN",
        "FNumber": 2.8,
        "FocalLength": 50,
        "FocalLengthIn35mmFormat": 50,
        "ISO": 800,
        "ExposureTime": 0.004,
        "captureTime": "2026-01-02T03:04:05",
    }
    monkeypatch.setattr("photo_server.metadata.extract", lambda path, executable: (extracted, "image/jpeg"))
    with TestClient(create_app(service.settings)) as client:
        response = client.post(
            "/processing",
            json={"assetIds": [str(asset_id)], "stages": ["metadata"]},
        )
        assert response.status_code == 202
        assert response.json() == {
            "assets": 1,
            "jobsQueued": 1,
            "jobsAlreadyQueued": 0,
            "jobsAlreadyRunning": 0,
            "jobTypes": ["metadata-v1"],
        }
        assert client.get("/upload-queue").json()["processingPending"] == 1

    result = run_once(service)
    assert result["jobType"] == "processing"
    assert result["stage"] == "metadata-v1"
    assert result["status"] == "updated"
    summary = service.catalog.browse(BrowseQuery(q="sigma"))["items"][0]
    assert summary["lens"] == "Sigma 24-70mm F2.8 DG DN"
    assert summary["technical"]["aperture"] == 2.8
    assert summary["technical"]["iso"] == 800
    assert service.catalog.processing_status(str(asset_id))[0]["status"] == "ready"


def test_processing_endpoint_can_queue_many_or_the_active_library(backend):
    service = backend.service
    first = imported(backend)
    second = imported(backend, "second.JPG", "blue")
    with TestClient(create_app(service.settings)) as client:
        response = client.post(
            "/processing",
            json={"assetIds": [str(first), str(second)]},
        )
        assert response.status_code == 202
        assert response.json()["jobsQueued"] == 2
        response = client.post("/processing", json={})
        assert response.status_code == 202
        assert response.json()["assets"] == 2
        assert response.json()["jobsQueued"] == 0
        assert response.json()["jobsAlreadyQueued"] == 2


def test_missing_blob_is_reported_without_turning_s3_into_state_authority(backend):
    service = backend.service
    asset_id = imported(backend)
    manifest = service.catalog.get(str(asset_id))
    service.storage.delete(manifest.primary.object_key)
    report = service.verify(full=True)
    assert report["errors"] and report["blobsChecked"] == 0
    assert patch(service, asset_id, caption="Catalog remains authoritative")["caption"]


def test_migration_sql_files_are_idempotent(backend):
    service = backend.service
    with service.catalog.engine.begin() as connection:
        for migration in available_migrations():
            connection.exec_driver_sql(migration.sql)
        for migration in available_migrations():
            connection.exec_driver_sql(migration.sql)
    assert service.catalog.initialize(str(service.library_id))["applied"] == []


def test_legacy_phase_two_database_is_adopted_then_migrated(backend):
    from sqlalchemy import text

    service = backend.service
    with service.catalog.engine.begin() as connection:
        connection.execute(text("DROP TABLE operations, album_assets, albums"))
        connection.execute(text("ALTER TABLE assets DROP COLUMN deleted_at"))
        connection.execute(text("ALTER TABLE library DROP COLUMN state_authority"))
        connection.execute(text("DROP TABLE schema_migrations"))
        connection.execute(text("UPDATE library SET schema_version = 2"))
    result = service.catalog.initialize(str(service.library_id))
    assert result == {
        "fromVersion": 2,
        "toVersion": 6,
        "applied": [
            {"version": 3, "name": "durable_user_state"},
            {"version": 4, "name": "postgres_authority"},
            {"version": 5, "name": "ai_analysis"},
            {"version": 6, "name": "abandoned_upload_cleanup"},
        ],
    }


def test_changed_applied_migration_checksum_is_rejected(backend, monkeypatch):
    migrations = available_migrations()
    changed = [
        replace(migration, checksum="0" * 64) if migration.version == 2 else migration
        for migration in migrations
    ]
    monkeypatch.setattr("photo_server.migrations.available_migrations", lambda: changed)
    with pytest.raises(LibraryError, match="no longer matches"):
        backend.service.catalog.initialize(str(backend.service.library_id))
