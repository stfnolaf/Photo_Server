"""Phase 3 durability tests against isolated S3 buckets and PostgreSQL databases."""

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


def imported(backend, name="sample.JPG", color="red"):
    result = backend.service.import_batch([photo(backend.root, name, color).name], uuid4())
    assert result["results"][0]["status"] == "imported", result
    return UUID(result["results"][0]["assetId"])


def patch(service, asset_id, operation_id=None, **changes):
    return mutate(
        service,
        operation_id or uuid4(),
        Mutation(
            action="asset.patch",
            entity_id=asset_id,
            changes=changes,
        ),
    )


def test_crash_retry_and_older_replay_after_database_loss(backend, monkeypatch):
    service = backend.service
    asset_id = imported(backend)
    original = service.catalog.get(str(asset_id))
    operation = uuid4()
    apply = service.catalog.apply

    def crash(snapshot):
        if snapshot.revision > 1:
            raise RuntimeError("database unavailable after durable commit")
        apply(snapshot)

    with monkeypatch.context() as context:
        context.setattr(service.catalog, "apply", crash)
        with pytest.raises(RuntimeError, match="database unavailable"):
            patch(service, asset_id, operation, rating=5)
        assert service.catalog.user_state(str(asset_id))["rating"] == 0
        assert (
            service.storage.get_json(f"state/assets/{asset_id}/00000002.json")["userState"][
                "rating"
            ]
            == 5
        )
        with pytest.raises(LibraryError, match="paused"):
            patch(service, asset_id, favorite=True)
        assert len(list(service.storage.keys(f"state/assets/{asset_id}/"))) == 2

    assert service.recover()["errors"] == []
    first = patch(service, asset_id, operation, rating=5)
    old = service.catalog.get(str(asset_id))
    newer = patch(service, asset_id, rating=2, favorite=True)
    service.catalog.apply(original)
    service.catalog.apply(old)
    assert service.catalog.get(str(asset_id)).revision == newer["revision"] == 3
    fresh = backend.fresh_catalog()
    assert fresh.recover(full=True)["errors"] == []
    assert patch(fresh, asset_id, operation, rating=5) == first
    assert fresh.catalog.user_state(str(asset_id))["rating"] == 2
    assert fresh.catalog.user_state(str(asset_id))["favorite"] is True
    assert len(list(service.storage.keys(f"state/assets/{asset_id}/"))) == 3
    with pytest.raises(LibraryError, match="different request"):
        patch(fresh, asset_id, operation, rating=1)


def test_uncertain_s3_success_is_inspected_and_uncommitted_failure_is_not_acknowledged(
    backend, monkeypatch
):
    service = backend.service
    asset_id = imported(backend)
    put = service.storage.put_json

    def timeout_after_write(key, value):
        put(key, value)
        raise TimeoutError("lost S3 response")

    with monkeypatch.context() as context:
        context.setattr(service.storage, "put_json", timeout_after_write)
        result = patch(service, asset_id, caption="Saved despite lost response")
    assert result["revision"] == 2
    before = service.catalog.get(str(asset_id)).document()

    def fail_before_write(*args):
        raise TimeoutError("S3 unavailable")

    with monkeypatch.context() as context:
        context.setattr(service.storage, "put_json", fail_before_write)
        with pytest.raises(TimeoutError):
            patch(service, asset_id, rating=5)
    assert service.catalog.get(str(asset_id)).document() == before
    assert len(list(service.storage.keys(f"state/assets/{asset_id}/"))) == 2


def test_phase_two_migration_survives_interrupt_and_verifies_before_acknowledgment(
    backend, monkeypatch
):
    from sqlalchemy import text

    service = backend.service
    asset_id = imported(backend)
    original = service.catalog.get(str(asset_id)).document()
    set_legacy_state(service, str(asset_id), {"rating": 4, "favorite": True})
    with service.catalog.engine.begin() as connection:
        connection.execute(text("DROP TABLE operations, album_assets, albums"))
        connection.execute(text("ALTER TABLE assets DROP COLUMN deleted_at"))
        connection.execute(text("DELETE FROM schema_migrations WHERE version = 3"))
        connection.execute(text("UPDATE library SET schema_version = 2"))
    service.catalog.initialize(str(service.library_id))
    apply = service.catalog.apply

    def crash(snapshot):
        if snapshot.revision > 1:
            raise RuntimeError("migration crash after S3")
        return apply(snapshot)

    with monkeypatch.context() as context:
        context.setattr(service.catalog, "apply", crash)
        assert service.recover()["errors"]
    assert service.catalog.get(str(asset_id)).revision == 1
    assert service.catalog.user_state(str(asset_id))["rating"] == 4
    assert service.recover()["errors"] == []
    assert service.recover()["migrated"] == 0
    assert service.storage.get_json(f"state/assets/{asset_id}/00000001.json") == original
    assert len(list(service.storage.keys(f"state/assets/{asset_id}/"))) == 2
    fresh = backend.fresh_catalog()
    assert fresh.recover(full=True)["errors"] == []
    assert fresh.catalog.user_state(str(asset_id)) == UserState(rating=4, favorite=True).document()


def test_failed_migration_keeps_local_values_and_blocks_api_startup(backend, monkeypatch):
    from photo_server.storage import Storage

    service = backend.service
    asset_id = imported(backend)
    set_legacy_state(service, str(asset_id), {"rating": 5})

    def fail(*args):
        raise RuntimeError("storage unavailable")

    with monkeypatch.context() as context:
        context.setattr(Storage, "put_json", fail)
        with pytest.raises(RuntimeError, match="recovery requires attention"):
            with TestClient(create_app(service.settings)):
                pass
    assert service.catalog.user_state(str(asset_id))["rating"] == 5
    assert service.catalog.get(str(asset_id)).revision == 1
    assert service.recover()["migrated"] == 1


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
    assert (
        service.catalog.user_state(str(asset_id))
        == UserState(
            rating=3,
            favorite=True,
            caption="Coast",
            keywords=["sea"],
        ).document()
    )


def test_metadata_albums_tombstones_restore_and_standalone_export(backend, tmp_path):
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
        assert client.get("/library/assets?q=holiday").json()["total"] == 1
        create = {
            "operationId": str(uuid4()),
            "name": "Trip",
            "assetIds": [str(second), str(first)],
        }
        response = client.post("/albums", json=create)
        assert response.status_code == 201, response.text
        album = response.json()
        album_id = album["albumId"]
        assert client.post("/albums", json=create).json() == album
        assert client.get(f"/library/assets?album_id={album_id}").json()["total"] == 2
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
        assert (
            client.patch(
                f"/albums/{album_id}", json={**edit, "operationId": str(uuid4())}
            ).status_code
            == 409
        )
        assert client.patch(f"/albums/{album_id}", json=edit).json() == response.json()
        assert (
            client.patch(
                f"/albums/{album_id}",
                json={"operationId": str(uuid4()), "assetIds": [str(uuid4())]},
            ).status_code
            == 409
        )
        delete = {"operationId": str(uuid4())}
        assert client.request("DELETE", f"/assets/{first}", json=delete).status_code == 200
        assert client.get("/library/assets").json()["total"] == 1
        assert client.get("/library/assets?deleted=true").json()["total"] == 1
        assert len(client.get("/assets").json()) == 1
        assert client.get(f"/library/assets?album_id={album_id}").json()["total"] == 1
        assert client.get(f"/albums/{album_id}").json()["assetIds"] == [str(first), str(second)]
        duplicate = service.import_batch(["sample.JPG"], uuid4())
        assert duplicate["results"][0]["status"] == "failed"
        assert "trash" in duplicate["results"][0]["error"]
        assert (
            client.patch(
                f"/assets/{first}/metadata", json={"operationId": str(uuid4()), "rating": 1}
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
        assert client.get("/albums").json() == []
        assert len(client.get("/albums?deleted=true").json()) == 1
        assert client.get("/library/assets").json()["total"] == 2
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
    fresh = backend.fresh_catalog()
    assert fresh.recover(full=True)["errors"] == []
    assert fresh.catalog.get_album(album_id).document() == expected_album
    assert fresh.catalog.browse(BrowseQuery())["total"] == 1
    assert fresh.catalog.browse(BrowseQuery(deleted=True))["total"] == 1
    assert fresh.catalog.user_state(str(first))["location"] == metadata["location"]
    broken_db = service.settings.model_copy(
        update={"database_url": "postgresql+psycopg://invalid@localhost:1/absent"}
    )
    destination = tmp_path / "export-active"
    assert export_library(broken_db, destination)["exported"] == 1
    exported = json.loads((destination / str(first) / "manifest.json").read_text())
    assert exported["userState"]["caption"] == metadata["caption"]
    library = json.loads((destination / "library-state.json").read_text())
    assert library["albums"] == [expected_album]
    assert library["trashedAssets"][0]["assetId"] == str(second)
    assert export_library(broken_db, tmp_path / "export-all", include_trash=True)["exported"] == 2


def test_corrupt_newest_album_blocks_recovery_mutations_and_export(backend, tmp_path):
    service = backend.service
    asset_id = imported(backend)
    album_id = uuid4()
    first = mutate(
        service,
        uuid4(),
        Mutation(
            action="album.create",
            entity_id=album_id,
            changes={"name": "Trip", "assetIds": [str(asset_id)]},
        ),
    )
    key = f"state/albums/{album_id}/00000002.json"
    service.storage.put_json(key, {**first, "schemaVersion": 999, "revision": 2})
    fresh = backend.fresh_catalog()
    assert fresh.recover()["errors"]
    assert fresh.catalog.list_albums() == []
    with pytest.raises(LibraryError, match="paused"):
        patch(service, asset_id, rating=5)
    with pytest.raises(LibraryError, match="invalid durable state"):
        export_library(service.settings, tmp_path / "bad-export")
    assert service.catalog.get(str(asset_id)).revision == 1


def test_album_replay_is_monotonic_and_operation_ids_are_global(backend):
    service = backend.service
    asset_id = imported(backend)
    operation, album_id = uuid4(), uuid4()
    request = Mutation(action="album.create", entity_id=album_id, changes={"name": "Trip"})
    first = mutate(service, operation, request)
    old = service.catalog.get_album(str(album_id))
    latest = mutate(
        service,
        uuid4(),
        Mutation(action="album.patch", entity_id=album_id, changes={"name": "New name"}),
    )
    service.catalog.apply_album(old)
    assert service.catalog.get_album(str(album_id)).document() == latest
    fresh = backend.fresh_catalog()
    assert fresh.recover()["errors"] == []
    assert mutate(fresh, operation, request) == first
    assert fresh.catalog.get_album(str(album_id)).document() == latest
    with pytest.raises(LibraryError, match="different request"):
        patch(fresh, asset_id, operation, rating=5)


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
        connection.execute(text("DROP TABLE schema_migrations"))
        connection.execute(text("UPDATE library SET schema_version = 2"))
    result = service.catalog.initialize(str(service.library_id))
    assert result == {
        "fromVersion": 2,
        "toVersion": 3,
        "applied": [{"version": 3, "name": "durable_user_state"}],
    }
    with service.catalog.engine.connect() as connection:
        assert list(
            connection.scalars(text("SELECT version FROM schema_migrations ORDER BY version"))
        ) == [1, 2, 3]


def test_changed_applied_migration_checksum_is_rejected(backend, monkeypatch):
    migrations = available_migrations()
    changed = [
        replace(migration, checksum="0" * 64) if migration.version == 2 else migration
        for migration in migrations
    ]
    monkeypatch.setattr("photo_server.migrations.available_migrations", lambda: changed)
    with pytest.raises(LibraryError, match="no longer matches"):
        backend.service.catalog.initialize(str(backend.service.library_id))


@pytest.mark.parametrize("remove_all", [False, True])
def test_missing_durable_history_blocks_new_mutations(backend, remove_all):
    service = backend.service
    asset_id = imported(backend)
    patch(service, asset_id, rating=5)
    prefix = f"state/assets/{asset_id}/"
    service.storage.delete(prefix + "00000002.json")
    if remove_all:
        service.storage.delete(prefix + "00000001.json")
    with pytest.raises(LibraryError, match="paused"):
        patch(service, asset_id, favorite=True)
    assert service.storage.head(prefix + "00000003.json") is None
    assert service.catalog.user_state(str(asset_id))["rating"] == 5
