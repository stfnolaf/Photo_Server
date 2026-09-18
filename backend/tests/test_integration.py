import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from psycopg import sql
from sqlalchemy.engine import make_url

from photo_server.config import Settings
from photo_server.export import export_library
from photo_server.service import Service
from photo_server.storage import Storage
from photo_server.worker import cache_paths, run_once

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("PHOTO_RUN_INTEGRATION") != "1",
        reason="Set PHOTO_RUN_INTEGRATION=1 for live disposable backends",
    ),
]


@dataclass
class Backend:
    service: Service
    fresh_catalog: object
    root: Path


@pytest.fixture
def backend(tmp_path):
    base = Settings()
    root = tmp_path / "source"
    root.mkdir()
    base = base.model_copy(update={"s3_bucket": f"photo-test-{uuid4().hex}", "import_root": root})
    storage = Storage(base)
    storage.ensure_bucket()
    url = make_url(base.database_url)
    admin_url = url.set(drivername="postgresql").render_as_string(hide_password=False)
    services, databases = [], []

    def fresh_catalog():
        name = f"photo_test_{uuid4().hex}"
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        databases.append(name)
        settings = base.model_copy(
            update={
                "database_url": url.set(database=name).render_as_string(hide_password=False),
                "data_dir": tmp_path / name,
            }
        )
        service = Service(settings)
        services.append(service)
        service.initialize()
        return service

    try:
        yield Backend(fresh_catalog(), fresh_catalog, root)
    finally:
        for service in services:
            service.catalog.engine.dispose()
        for name in databases:
            with psycopg.connect(admin_url, autocommit=True) as admin:
                admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
        # This fixture created a fresh random bucket. Never clean the configured library bucket.
        for key in list(storage.keys("")):
            storage.client.delete_object(Bucket=storage.bucket, Key=key)
        storage.client.delete_bucket(Bucket=storage.bucket)


def photo(root, name="sample.JPG", color="red"):
    path = root / name
    Image.new("RGB", (80, 60), color).save(path, format="JPEG")
    return path


def test_storage_refuses_overwrite(backend):
    storage = backend.service.storage
    assert storage.put("probe", b"first", "text/plain")
    assert not storage.put("probe", b"second", "text/plain")
    assert b"".join(storage.chunks("probe")) == b"first"


def test_import_duplicate_retry_sidecar_and_postgres_authority(backend):
    service = backend.service
    original = photo(backend.root)
    sidecar = backend.root / "sample.xmp"
    sidecar.write_text('<x:xmpmeta xmlns:x="adobe:ns:meta/"/>')
    before = original.read_bytes(), sidecar.read_bytes()
    operation = uuid4()
    paths = [original.name, sidecar.name]
    result = service.import_batch(paths, operation)
    assert result["results"][0]["status"] == "imported", result
    assert service.catalog.counts() == {"assets": 1, "blobs": 2}
    assert service.import_batch(paths, operation)["results"][0]["replayed"]
    assert service.import_batch(paths, uuid4())["results"][0]["status"] == "duplicate"
    assert len(list(service.storage.keys("originals/"))) == 2
    assert list(service.storage.keys("state/")) == []
    fresh = backend.fresh_catalog()
    assert fresh.catalog.counts() == {"assets": 0, "blobs": 0}
    assert fresh.verify(full=True) == {
        "assetsChecked": 0,
        "blobsChecked": 0,
        "verification": "sha256",
        "errors": [],
    }
    assert (original.read_bytes(), sidecar.read_bytes()) == before
    photo(backend.root, color="blue")
    retry = service.import_batch(paths, operation)
    assert retry["results"][0]["status"] == "failed"
    assert "changed file content" in retry["results"][0]["error"]


def test_crash_after_original_before_database_reuses_immutable_object(backend, monkeypatch):
    service = backend.service
    path = photo(backend.root)
    operation = uuid4()
    with monkeypatch.context() as patch:

        def crash(_manifest):
            raise RuntimeError("simulated database failure after object commit")

        patch.setattr(service.catalog, "apply", crash)
        result = service.import_batch([path.name], operation)
    assert result["results"][0]["status"] == "failed"
    assert service.catalog.counts()["assets"] == 0
    assert len(list(service.storage.keys("originals/"))) == 1
    assert list(service.storage.keys("state/")) == []
    retry = service.import_batch([path.name], operation)
    assert retry["results"][0]["status"] == "imported"
    assert service.catalog.counts()["assets"] == 1


def test_changed_retry_cannot_replace_orphaned_original(backend, monkeypatch):
    service = backend.service
    path = photo(backend.root)
    operation = uuid4()
    with monkeypatch.context() as patch:
        patch.setattr(
            service.catalog,
            "apply",
            lambda _manifest: (_ for _ in ()).throw(RuntimeError("database unavailable")),
        )
        result = service.import_batch([path.name], operation)
    assert result["results"][0]["status"] == "failed"
    assert len(list(service.storage.keys("originals/"))) == 1
    photo(backend.root, color="blue")
    retry = service.import_batch([path.name], operation)
    assert retry["results"][0]["status"] == "failed"
    assert "Checksum verification failed" in retry["results"][0]["error"]
    assert len(list(service.storage.keys("originals/"))) == 1


def test_failed_raw_does_not_claim_companion_was_imported(backend):
    raw = backend.root / "broken.ARW"
    raw.write_bytes(b"not a valid RAW")
    companion = photo(backend.root, "broken.JPG")
    result = backend.service.import_batch([companion.name, raw.name], uuid4())
    assert result["results"][0]["status"] == "failed"
    assert result["skipped"][0]["status"] == "deferred"
    assert companion.exists() and raw.exists()
    assert backend.service.catalog.counts()["assets"] == 0


def test_storage_verification_detects_corruption(backend):
    service = backend.service
    path = photo(backend.root)
    result = service.import_batch([path.name], uuid4())
    manifest = service.catalog.get(result["results"][0]["assetId"])
    corrupted = bytearray(path.read_bytes())
    corrupted[-1] ^= 1
    service.storage.client.put_object(
        Bucket=service.storage.bucket, Key=manifest.primary.object_key, Body=bytes(corrupted)
    )
    result = service.verify(full=True)
    assert result["blobsChecked"] == 0
    assert "Checksum verification failed" in result["errors"][0]["error"]


def test_database_backed_export_and_preview_cache_rebuild(backend, tmp_path):
    service = backend.service
    source = photo(backend.root)
    result = service.import_batch([source.name], uuid4())
    manifest = service.catalog.get(result["results"][0]["assetId"])
    assert run_once(service)["status"] == "ready"
    targets = cache_paths(service, manifest)
    assert all(path.exists() for path in targets.values())
    targets["thumbnail"].unlink()
    service.catalog.queue_preview(str(manifest.asset_id))
    assert run_once(service)["status"] == "ready"
    destination = tmp_path / "export"
    assert export_library(service.settings, destination)["exported"] == 1
    exported = destination / str(manifest.asset_id) / source.name
    assert hashlib.sha256(exported.read_bytes()).hexdigest() == manifest.primary.sha256


def test_api_upload_queue_onboarding_restart_and_preview(backend):
    from photo_server.api import create_app

    source = photo(backend.root)
    with TestClient(create_app(backend.service.settings)) as client:
        assert client.get("/health").status_code == 200
        batch_id = uuid4()
        response = client.post(
            "/upload-batches",
            json={
                "batchId": str(batch_id),
                "files": [
                    {
                        "path": source.name,
                        "sizeBytes": source.stat().st_size,
                        "mimeType": "image/jpeg",
                    }
                ],
            },
        )
        assert response.status_code == 201, response.text
        upload = response.json()["files"][0]
        assert upload["required"] and upload["status"] == "waiting"
        assert client.put(upload["uploadUrl"], content=source.read_bytes()).status_code == 200
        response = client.post(f"/upload-batches/{batch_id}/seal")
        assert response.status_code == 202 and response.json()["status"] == "queued"
        listed = client.get("/upload-batches").json()
        assert [(batch["batchId"], batch["status"]) for batch in listed] == [
            (str(batch_id), "queued")
        ]
        assert client.delete(f"/upload-batches/{batch_id}").status_code == 409
        assert client.get("/upload-queue").json()["onboardingPending"] == 1

        onboarded = run_once(backend.service)
        assert onboarded["jobType"] == "onboarding" and onboarded["status"] == "imported", onboarded
        asset_id = onboarded["assetId"]
        assert client.get(f"/upload-batches/{batch_id}").json()["status"] == "complete"
        assert client.get("/assets").json()[0]["assetId"] == asset_id
        assert client.get(f"/assets/{asset_id}/original").content == source.read_bytes()
        assert client.get(f"/assets/{asset_id}/preview").status_code == 202
        assert run_once(backend.service)["jobType"] == "preview"
        assert client.get(f"/assets/{asset_id}/thumbnail").headers["content-type"] == "image/jpeg"
        assert client.post(f"/assets/{asset_id}/preview/retry").json()["status"] == "pending"

    assert backend.service.catalog.counts() == {"assets": 1, "blobs": 1}


def test_unsealed_uploads_can_be_discarded_or_expire(backend):
    from sqlalchemy import text

    from photo_server.api import create_app
    from photo_server.uploads import cleanup_abandoned_batches

    source = photo(backend.root)

    def start_and_upload(client, batch_id):
        response = client.post(
            "/upload-batches",
            json={
                "batchId": str(batch_id),
                "files": [{"path": source.name, "sizeBytes": source.stat().st_size}],
            },
        )
        upload = response.json()["files"][0]
        assert client.put(upload["uploadUrl"], content=source.read_bytes()).status_code == 200
        assert list(backend.service.storage.keys(f"incoming/{batch_id}/"))

    with TestClient(create_app(backend.service.settings)) as client:
        discarded = uuid4()
        start_and_upload(client, discarded)
        assert client.get("/upload-batches").json()[0]["batchId"] == str(discarded)
        response = client.delete(f"/upload-batches/{discarded}")
        assert response.status_code == 200
        assert response.json()["status"] == "deleted"
        assert backend.service.catalog.upload_batch(discarded) is None
        assert client.get("/upload-batches").json() == []
        assert list(backend.service.storage.keys(f"incoming/{discarded}/")) == []

        expired = uuid4()
        start_and_upload(client, expired)
        with backend.service.catalog.engine.begin() as connection:
            connection.execute(
                text("UPDATE upload_batches SET updated_at = 0 WHERE id = :id"),
                {"id": str(expired)},
            )
        result = cleanup_abandoned_batches(backend.service)
        assert result["batchesDeleted"] == 1
        assert backend.service.catalog.upload_batch(expired) is None
        assert list(backend.service.storage.keys(f"incoming/{expired}/")) == []


def test_ai_analysis_is_separate_searchable_durable_and_requeueable(backend):
    from photo_server.api import create_app

    service = backend.service
    source = photo(backend.root, "alpine-trip.JPG")
    imported = service.import_batch([source.name], uuid4())
    asset_id = imported["results"][0]["assetId"]
    manifest = service.catalog.get(asset_id)

    assert service.catalog.analysis_status(asset_id)["status"] == "pending"
    assert run_once(service)["jobType"] == "preview"
    claimed = service.catalog.claim_ai_job()
    assert claimed == {"asset_id": asset_id, "preview_status": "ready", "force_full": False}

    run_id = str(uuid4())
    object_key = f"analysis/{asset_id}/photo-ai-v1/{run_id}.json"
    service.storage.put_json(object_key, {"schemaVersion": 1, "runId": run_id})
    result = {
        "summary": "Two hikers beside an alpine lake",
        "photoTypes": ["travel", "group"],
        "scene": "mountain lake",
        "setting": "outdoor",
        "objects": [{"name": "backpack", "count": 2}],
        "activities": ["hiking"],
        "tags": ["mountains"],
        "visibleText": [],
        "faceCount": 2,
    }
    completed = service.catalog.complete_ai_analysis(
        asset_id=asset_id,
        run_id=run_id,
        model_name="qwen-test",
        model_version="sha256:model",
        pipeline_version="photo-ai-v1",
        input_hash=manifest.primary.sha256,
        object_key=object_key,
        result=result,
        searchable="Two hikers alpine lake travel group backpack hiking mountains",
        detected_faces=[
            {"box": [0.1, 0.1, 0.2, 0.3], "confidence": 0.99, "embedding": [1.0, 0.0]},
            {"box": [0.5, 0.1, 0.2, 0.3], "confidence": 0.98, "embedding": [1.0, 0.0]},
        ],
        match_threshold=0.4,
        created_at=datetime.now(UTC).isoformat(),
    )
    assert completed == {"faceCount": 2, "personCount": 2}
    assert service.storage.get_json(object_key)["runId"] == run_id

    with TestClient(create_app(service.settings)) as client:
        detail = client.get(f"/assets/{asset_id}").json()
        assert detail["analysis"]["status"] == "ready"
        assert detail["analysis"]["result"]["scene"] == "mountain lake"
        assert len({face["personId"] for face in detail["analysis"]["faces"]}) == 2
        search = client.get("/library/assets", params={"q": "backpack"}).json()
        assert [item["assetId"] for item in search["items"]] == [asset_id]
        queued = client.post(f"/assets/{asset_id}/analysis/retry")
        assert queued.status_code == 202 and queued.json()["jobsQueued"] == 1
        refreshed = client.get(f"/assets/{asset_id}").json()["analysis"]
        assert refreshed["status"] == "pending" and refreshed["runId"] == run_id
        duplicate = client.post(f"/assets/{asset_id}/analysis/retry")
        assert duplicate.status_code == 202
        assert duplicate.json()["jobsQueued"] == 0
        assert duplicate.json()["jobsAlreadyQueued"] == 1


def test_api_batch_does_not_request_raw_companions(backend):
    from photo_server.api import create_app

    raw = backend.root / "pair.ARW"
    raw.write_bytes(b"raw fixture")
    jpeg = photo(backend.root, "pair.JPG")
    with TestClient(create_app(backend.service.settings)) as client:
        response = client.post(
            "/upload-batches",
            json={
                "files": [
                    {"path": jpeg.name, "sizeBytes": jpeg.stat().st_size},
                    {"path": raw.name, "sizeBytes": raw.stat().st_size},
                ]
            },
        )
    assert response.status_code == 201, response.text
    by_path = {file["path"]: file for file in response.json()["files"]}
    assert by_path[raw.name]["required"]
    assert by_path[jpeg.name]["status"] == "skipped"
    assert by_path[jpeg.name]["uploadUrl"] is None


def test_people_api_names_combines_and_corrects_face_groups(backend):
    from photo_server.api import create_app

    service = backend.service
    source = photo(backend.root, "people.JPG")
    imported = service.import_batch([source.name], uuid4())
    assert imported["results"][0]["status"] == "imported", imported
    asset_id = imported["results"][0]["assetId"]
    manifest = service.catalog.get(asset_id)
    assert run_once(service)["status"] == "ready"
    service.catalog.complete_ai_analysis(
        asset_id=asset_id,
        run_id=str(uuid4()),
        model_name="test",
        model_version="test",
        pipeline_version="photo-ai-v1",
        input_hash=manifest.primary.sha256,
        object_key=f"analysis/{asset_id}/test.json",
        result={"faceCount": 3},
        searchable="people",
        detected_faces=[
            {"box": [0.05, 0.1, 0.2, 0.3], "confidence": 0.99, "embedding": [1.0, 0.0]},
            {"box": [0.4, 0.1, 0.2, 0.3], "confidence": 0.98, "embedding": [1.0, 0.0]},
            {"box": [0.72, 0.1, 0.2, 0.3], "confidence": 0.97, "embedding": [1.0, 0.0]},
        ],
        match_threshold=0.4,
        created_at=datetime.now(UTC).isoformat(),
    )

    with TestClient(create_app(service.settings)) as client:
        groups = client.get("/people").json()
        assert groups["total"] == 3
        assert groups["unnamed"] == 3
        target, move_source, merge_source = groups["items"]
        face_id = move_source["sampleFaces"][0]["faceId"]
        crop = client.get(f"/faces/{face_id}/thumbnail")
        assert crop.status_code == 200
        assert crop.headers["content-type"] == "image/jpeg"

        operation_id = str(uuid4())
        named = client.patch(
            f"/people/{target['personId']}",
            json={"operationId": operation_id, "displayName": "Alex"},
        )
        assert named.status_code == 200
        assert named.json()["displayName"] == "Alex"
        replay = client.patch(
            f"/people/{target['personId']}",
            json={"operationId": operation_id, "displayName": "Alex"},
        )
        assert replay.json() == named.json()

        moved = client.post(
            "/faces/move",
            json={
                "operationId": str(uuid4()),
                "faceIds": [face_id],
                "targetPersonId": target["personId"],
            },
        )
        assert moved.status_code == 200
        assert moved.json()["movedFaces"] == 1

        merged = client.post(
            f"/people/{merge_source['personId']}/merge",
            json={"operationId": str(uuid4()), "targetPersonId": target["personId"]},
        )
        assert merged.status_code == 200
        assert merged.json()["personId"] == target["personId"]
        combined = client.get(f"/people/{target['personId']}").json()
        assert combined["displayName"] == "Alex"
        assert combined["faceCount"] == 3

        separated = client.post(
            "/faces/move",
            json={
                "operationId": str(uuid4()),
                "faceIds": [combined["faces"][0]["faceId"]],
                "targetPersonId": None,
            },
        )
        assert separated.status_code == 200
        assert separated.json()["createdPerson"] is True
        refreshed = client.get("/people").json()
        assert refreshed["total"] == 2
        assert refreshed["named"] == 1


def test_api_streams_a_file_as_multiple_s3_parts(backend):
    from photo_server.api import create_app

    settings = backend.service.settings.model_copy(update={"upload_part_bytes": 5 * 1024 * 1024})
    payload = b"multipart-fixture" * 350_000
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/upload-batches",
            json={"files": [{"path": "large.JPG", "sizeBytes": len(payload)}]},
        )
        assert response.status_code == 201, response.text
        upload = response.json()["files"][0]
        result = client.put(upload["uploadUrl"], content=payload)
    assert result.status_code == 200, result.text
    assert result.json()["sha256"] == hashlib.sha256(payload).hexdigest()


def test_onboarding_jobs_are_claimed_concurrently_without_duplicates(backend):
    from photo_server.api import create_app

    sources = [
        photo(backend.root, f"parallel-{number}.JPG", (number * 30, 20, 120)) for number in range(6)
    ]
    with TestClient(create_app(backend.service.settings)) as client:
        response = client.post(
            "/upload-batches",
            json={
                "files": [
                    {"path": source.name, "sizeBytes": source.stat().st_size} for source in sources
                ]
            },
        )
        assert response.status_code == 201, response.text
        for upload in response.json()["files"]:
            source = backend.root / upload["path"]
            assert client.put(upload["uploadUrl"], content=source.read_bytes()).status_code == 200
        batch_id = response.json()["batchId"]
        assert client.post(f"/upload-batches/{batch_id}/seal").status_code == 202

        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda _: run_once(backend.service), range(len(sources))))

        assert all(result["jobType"] == "onboarding" for result in results)
        assert len({result["jobId"] for result in results}) == len(sources)
        assert client.get(f"/upload-batches/{batch_id}").json()["status"] == "complete"


def test_standalone_heif_import_and_preview(backend):
    import pillow_heif

    path = backend.root / "standalone.HEIF"
    pillow_heif.from_pillow(Image.new("RGB", (80, 60), "green")).save(path)
    result = backend.service.import_batch([path.name], uuid4())
    assert result["results"][0]["status"] == "imported", result
    manifest = backend.service.catalog.get(result["results"][0]["assetId"])
    assert manifest.primary.role == "ORIGINAL_HEIF"
    assert run_once(backend.service)["status"] == "ready"


def catalog_fixture(
    service,
    number,
    capture,
    *,
    imported="2025-01-02T12:00:00Z",
    name=None,
    media="JPEG",
    camera="SONY",
):
    """Tiny durable synthetic records for query tests."""
    from uuid import UUID

    from photo_server.models import Blob, Manifest

    asset_id = UUID(int=number)
    filename = name or f"photo-{number}.{media}"
    blob = Blob(
        blob_id=uuid4(),
        role=f"ORIGINAL_{media}",
        original_filename=filename,
        object_key=f"originals/{asset_id}/{filename}",
        sha256=hashlib.sha256(str(number).encode().ljust(100, b"0")).hexdigest(),
        size_bytes=100,
        mime_type="image/jpeg",
    )
    manifest = Manifest(
        library_id=service.library_id,
        asset_id=asset_id,
        operation_id=uuid4(),
        primary_blob_id=blob.blob_id,
        blobs=[blob],
        imported_at=imported,
        capture_time=capture,
        metadata={"Make": camera, "Model": "Camera", "LensModel": "35mm Prime"},
    )
    service.storage.put(blob.object_key, str(number).encode().ljust(100, b"0"), "image/jpeg")
    service.catalog.apply(manifest)
    return manifest


def set_legacy_state(service, asset_id, changes):
    from photo_server.catalog import assets

    with service.catalog.engine.begin() as connection:
        connection.execute(assets.update().where(assets.c.id == asset_id).values(**changes))


def test_browse_timeline_search_filters_and_cursor_stability(backend):
    from photo_server.api import create_app

    service = backend.service
    a = catalog_fixture(service, 1, "2024-05-01T00:10:00+13:00", media="RAW")
    b = catalog_fixture(service, 2, "2024-05-01T00:10:00-08:00", camera="Canon")
    c = catalog_fixture(service, 3, None, media="HEIF", camera="Apple")
    d = catalog_fixture(service, 4, "0000:00:00 00:00:00", imported="2023-01-01T00:00:00Z")
    e = catalog_fixture(service, 5, "2024-04-30T23:59:59", name="100%_done.JPG")
    set_legacy_state(service, str(a.asset_id), {"rating": 5, "favorite": True})
    set_legacy_state(service, str(b.asset_id), {"rating": 3})
    with TestClient(create_app(service.settings)) as client:
        page = client.get("/library/assets?limit=2").json()
        assert page["total"] == 5
        assert [item["assetId"] for item in page["items"]] == [str(c.asset_id), str(b.asset_id)]
        assert page["items"][0]["dateSource"] == "import"
        assert page["items"][1]["timelineTime"] == "2024-05-01T00:10:00"
        assert page["items"][1]["preview"]["status"] == "pending"
        # Inserting ahead of the cursor doesn't repeat/skip assets in later pages.
        catalog_fixture(service, 6, "2026-01-01T00:00:00")
        next_page = client.get(
            "/library/assets", params={"cursor": page["nextCursor"], "limit": 2}
        ).json()
        assert [item["assetId"] for item in next_page["items"]] == [
            str(a.asset_id),
            str(e.asset_id),
        ]
        last = client.get("/library/assets", params={"cursor": next_page["nextCursor"]}).json()
        assert [item["assetId"] for item in last["items"]] == [str(d.asset_id)]
        assert last["nextCursor"] is None
        oldest = client.get("/library/assets?sort=oldest&limit=2").json()
        assert [item["assetId"] for item in oldest["items"]] == [str(d.asset_id), str(e.asset_id)]
        older_next = client.get(
            "/library/assets", params={"sort": "oldest", "cursor": oldest["nextCursor"], "limit": 2}
        ).json()
        assert [item["assetId"] for item in older_next["items"]] == [
            str(a.asset_id),
            str(b.asset_id),
        ]
        for params, expected in [
            ({"date_from": "2024-05-01", "date_to": "2024-05-01"}, 2),
            ({"q": "sOnY"}, 4),
            ({"q": "%_"}, 1),
            ({"q": "35mm"}, 6),
            ({"q": "no such photo"}, 0),
            ({"media_type": "RAW"}, 1),
            ({"rating_min": 4}, 1),
            ({"favorite": True}, 1),
            ({"favorite": False}, 5),
            ({"q": "sony", "rating_min": 5, "favorite": True, "media_type": "RAW"}, 1),
        ]:
            result = client.get("/library/assets", params=params)
            assert result.status_code == 200, result.text
            assert result.json()["total"] == expected, params
        for params in [
            {"limit": 0},
            {"rating_min": 6},
            {"media_type": "VIDEO"},
            {"date_from": "2025-01-02", "date_to": "2025-01-01"},
        ]:
            assert client.get("/library/assets", params=params).status_code == 422
        assert client.get("/library/assets?cursor=garbage").status_code == 400
        assert (
            client.get(
                "/library/assets", params={"cursor": page["nextCursor"], "sort": "oldest"}
            ).status_code
            == 400
        )


def test_ratings_favorites_survive_restart_and_storage_verify(backend):
    from photo_server.api import create_app
    from photo_server.models import UserState

    service = backend.service
    result = service.import_batch([photo(backend.root).name], uuid4())
    asset_id = result["results"][0]["assetId"]
    settings = service.settings.model_copy(update={"cors_origins": "http://library.example"})
    with TestClient(create_app(settings)) as client:
        assert client.get(f"/assets/{asset_id}").json()["userState"] == UserState().document()
        first = client.patch(
            f"/assets/{asset_id}/user-state", json={"operationId": str(uuid4()), "rating": 5}
        )
        assert first.status_code == 200, first.text
        operation = {"operationId": str(uuid4()), "favorite": True}
        second = client.patch(f"/assets/{asset_id}/user-state", json=operation)
        assert second.status_code == 200, second.text
        assert second.json()["rating"] == 5 and second.json()["favorite"] is True
        assert (
            client.patch(f"/assets/{asset_id}/user-state", json=operation).json() == second.json()
        )
        assert (
            client.patch(
                f"/assets/{uuid4()}/user-state", json={"operationId": str(uuid4()), "rating": 1}
            ).status_code
            == 404
        )
        assert (
            client.patch(
                f"/assets/{asset_id}/user-state", json={"operationId": str(uuid4()), "rating": None}
            ).status_code
            == 422
        )
        assert client.patch(f"/assets/{asset_id}/user-state", json={"rating": 1}).status_code == 422
        preflight = client.options(
            f"/assets/{asset_id}",
            headers={"Origin": "http://library.example", "Access-Control-Request-Method": "DELETE"},
        )
        assert (
            preflight.status_code == 200
            and "DELETE" in preflight.headers["access-control-allow-methods"]
        )
        assert client.post("/maintenance/verify").json()["errors"] == []
    expected = UserState(rating=5, favorite=True).document()
    with TestClient(create_app(service.settings)) as client:
        assert client.get(f"/assets/{asset_id}").json()["userState"] == expected
        assert client.get("/library/assets?favorite=true&rating_min=5").json()["total"] == 1
    assert list(service.storage.keys("state/")) == []


def test_phase_one_catalog_upgrade_backfills_without_changing_manifests(backend):
    from sqlalchemy import text

    from photo_server.browsing import BrowseQuery

    service = backend.service
    a = catalog_fixture(service, 1, "2024-01-01T00:30:00+13:00", media="RAW")
    b = catalog_fixture(service, 2, None)
    with service.catalog.engine.begin() as connection:
        connection.execute(text("DROP TABLE operations, album_assets, albums"))
        connection.execute(
            text("""
            ALTER TABLE assets DROP COLUMN timeline_at, DROP COLUMN media_type,
              DROP COLUMN search_text, DROP COLUMN rating, DROP COLUMN favorite,
              DROP COLUMN deleted_at
        """)
        )
        connection.execute(text("DELETE FROM schema_migrations WHERE version > 1"))
        connection.execute(text("UPDATE library SET schema_version = 1"))
    service.catalog.initialize(str(service.library_id))
    page = service.catalog.browse(BrowseQuery())
    assert page["total"] == 2
    assert page["items"][0]["assetId"] == str(b.asset_id)
    assert page["items"][1]["timelineTime"] == "2024-01-01T00:30:00"
    assert service.catalog.get(str(a.asset_id)).document() == a.document()
    set_legacy_state(service, str(a.asset_id), {"rating": 4, "favorite": True})
    service.catalog.initialize(str(service.library_id))
    service.catalog.apply(a)
    assert service.catalog.user_state(str(a.asset_id))["rating"] == 4
    assert service.catalog.user_state(str(a.asset_id))["favorite"] is True
    assert service.catalog.counts() == {"assets": 2, "blobs": 2}


def test_derivative_api_states(backend):
    from sqlalchemy import text

    from photo_server.api import create_app

    service = backend.service
    result = service.import_batch([photo(backend.root).name], uuid4())
    asset_id = result["results"][0]["assetId"]
    with TestClient(create_app(service.settings)) as client:
        root = client.get("/", follow_redirects=False)
        assert root.status_code == 307 and root.headers["location"] == "/docs"
        assert client.get("/library.js").status_code == 404
        assert client.get("/docs").status_code == 200
        with service.catalog.engine.begin() as connection:
            connection.execute(text("DELETE FROM jobs WHERE asset_id = :id"), {"id": asset_id})
        assert client.get(f"/assets/{asset_id}/thumbnail").status_code == 202
        assert run_once(service)["status"] == "ready"
        response = client.get(f"/assets/{asset_id}/preview")
        assert response.headers["content-type"] == "image/jpeg"
        assert response.headers["cache-control"] == "private, max-age=3600"
        for path in cache_paths(service, service.catalog.get(asset_id)).values():
            path.unlink()
        # Cache loss queues reconstruction even when the recorded job was ready.
        assert client.get(f"/assets/{asset_id}/thumbnail").status_code == 202
        service.catalog.finish_job(asset_id, "unavailable")
        assert client.get(f"/assets/{asset_id}/preview").status_code == 404
        service.catalog.finish_job(asset_id, "failed", "Decoder failure")
        assert client.get(f"/assets/{asset_id}/preview").status_code == 503
        assert client.get(f"/assets/{asset_id}").json()["preview"]["error"] == "Decoder failure"
        assert client.post(f"/assets/{asset_id}/preview/retry").json()["status"] == "pending"
        assert run_once(service)["status"] == "ready"
        original = client.get(f"/assets/{asset_id}/original")
        assert original.content == (backend.root / "sample.JPG").read_bytes()
        assert "attachment;" in original.headers["content-disposition"]
