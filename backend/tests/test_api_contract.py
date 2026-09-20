"""API wire-contract goldens: a fixed request sequence diffed against frozen fixtures.

The spec (`openapi/openapi.json`) declares *shape*; these goldens prove the
*bytes*. A later phase that adds a ``response_model=`` to an endpoint must keep
this file green: the same requests return the same normalized bodies, so a
model that disagrees with the emitted dict shows up here as a diff, and a wire
change that a model *does* permit shows up here as a fixture edit to review.

Mechanism
---------
Each test runs against the disposable-backend fixture pattern from
``test_integration.py`` (fresh random S3 bucket, fresh random Postgres
database, torn down after the test). Every non-deterministic input is pinned
*in the scenario* rather than masked in the output:

- The library identity: ``library.json`` is pre-seeded with a fixed
  ``libraryId`` before ``Service.initialize()``, so every document carries the
  same id instead of a fresh random one.
- Asset, blob, and operation ids: :func:`pinned_catalog_fixture` derives all of
  them from the fixture number (``uuid5``), and it uses fixed filenames,
  sizes, digests, import/capture timestamps, and metadata.
- Error cases use fixed unknown ids.
- Settings values that flow into recorded bodies (``uploadWorkers`` in
  ``/health``) are pinned in the fixture, so the goldens are stable when the
  surrounding environment changes.

Bodies are captured as ``(method, path, status, body)`` cases. The only
normalization is recursive dictionary-key sorting, because JSON object key
order is not part of the contract (Postgres jsonb stores its own key order,
and Pydantic serializes in model field order); array order is preserved.

Recording and verification
--------------------------
The first run of a section writes its fixture file under
``tests/fixtures/api_golden/``; every later run must match it after
normalization. Run a section twice after recording to prove determinism.

Adding sections (Phases 1a onward)
----------------------------------
Append-only, by design: each phase adds a new fixture file and a new test
function that feeds its own request list to :func:`run_sequence`. Previously
recorded sections are never edited in place; if a later phase intentionally
changes a wire format it re-records that section as a *new file* (e.g.
``seed_v2.json``) so the old bytes stay reviewable in git history.
"""

import difflib
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from sqlalchemy import and_, delete, insert, select, update
from sqlalchemy.engine import make_url

from photo_server.api import create_app
from photo_server.browsing import BrowseQuery
from photo_server.catalog import (
    analysis_runs,
    assets,
    burst_clusters,
    burst_members,
    faces,
    jobs,
    people,
)
from photo_server.config import Settings
from photo_server.models import Blob, Manifest, Mutation
from photo_server.service import Service
from photo_server.storage import Storage, canonical_json

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("PHOTO_RUN_INTEGRATION") != "1",
        reason="Set PHOTO_RUN_INTEGRATION=1 for live disposable backends",
    ),
]

# Pinned identities shared by every golden fixture in this module.
GOLDEN_LIBRARY_ID = UUID("11111111-1111-4111-8111-111111111111")
GOLDEN_NAMESPACE = uuid5(NAMESPACE_DNS, "photo-server:api-golden")
UNKNOWN_ASSET_ID = UUID("22222222-2222-4222-8222-222222222222")

FIXTURES = Path(__file__).parent / "fixtures" / "api_golden"


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
    # s3_bucket/import_root stay random and disposable; upload_workers is
    # pinned because it flows into a recorded body (/health's uploadWorkers)
    # and the golden must not depend on the surrounding environment.
    base = base.model_copy(
        update={
            "s3_bucket": f"photo-test-{uuid4().hex}",
            "import_root": root,
            "upload_workers": 4,
        }
    )
    storage = Storage(base)
    storage.ensure_bucket()
    # Pin the library identity before initialize(): Service reads the marker
    # and uses its libraryId instead of generating a random one.
    storage.put(
        "library.json",
        canonical_json({"schemaVersion": 1, "libraryId": str(GOLDEN_LIBRARY_ID)}),
        "application/json",
    )
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


def pinned_catalog_fixture(
    service,
    number,
    capture,
    *,
    imported="2025-01-02T12:00:00Z",
    name=None,
    media="JPEG",
    camera="SONY",
    extra_metadata=None,
):
    """Tiny durable synthetic record with every id pinned from ``number``.

    Variant of ``test_integration.catalog_fixture`` for golden recording:
    blob and operation ids are ``uuid5``-derived instead of ``uuid4`` so a
    freshly built backend produces byte-identical documents.
    """
    asset_id = UUID(int=number)
    blob_id = uuid5(GOLDEN_NAMESPACE, f"blob-{number}")
    operation_id = uuid5(GOLDEN_NAMESPACE, f"operation-{number}")
    filename = name or f"photo-{number}.{media}"
    blob = Blob(
        blob_id=blob_id,
        role=f"ORIGINAL_{media}",
        original_filename=filename,
        object_key=f"originals/{asset_id}/{filename}",
        sha256=hashlib.sha256(str(number).encode().ljust(100, b"0")).hexdigest(),
        size_bytes=100,
        mime_type="image/jpeg",
    )
    metadata = {"Make": camera, "Model": "Camera", "LensModel": "35mm Prime"}
    if extra_metadata:
        metadata.update(extra_metadata)
    manifest = Manifest(
        library_id=service.library_id,
        asset_id=asset_id,
        operation_id=operation_id,
        primary_blob_id=blob.blob_id,
        blobs=[blob],
        imported_at=imported,
        capture_time=capture,
        metadata=metadata,
    )
    service.storage.put(blob.object_key, str(number).encode().ljust(100, b"0"), "image/jpeg")
    service.catalog.apply(manifest)
    return manifest


def normalize(value):
    """Recursive key sorting: JSON object key order is not part of the contract."""
    if isinstance(value, dict):
        return {key: normalize(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [normalize(item) for item in value]
    return value


def capture(client, method, path) -> dict:
    response = client.request(method, path)
    return {
        "method": method.upper(),
        "path": path,
        "status": response.status_code,
        "body": normalize(response.json()) if response.content else None,
    }


def run_sequence(backend: Backend, section: str, cases: list[tuple[str, str]], *, describe: str):
    """Record the section's fixture on first run, verify it on every run after.

    ``cases`` is the fixed request list as ``(method, path)`` pairs, executed
    against a fresh ``TestClient`` of the backend's app in the given order.
    """
    with TestClient(create_app(backend.service.settings)) as client:
        recorded = [capture(client, method, path) for method, path in cases]
    fixture = FIXTURES / f"{section}.json"
    payload = {
        "schemaVersion": 1,
        "section": section,
        "libraryId": str(GOLDEN_LIBRARY_ID),
        "description": describe,
        "normalization": "dict keys sorted recursively; array order preserved",
        "cases": recorded,
    }
    if not fixture.exists():
        FIXTURES.mkdir(parents=True, exist_ok=True)
        fixture.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        print(f"\nrecorded {fixture} ({len(recorded)} cases) — review and commit it")
        return

    expected = json.loads(fixture.read_text())
    if normalize(expected) != normalize(payload):
        raise AssertionError(
            f"golden section {section!r} diverged from the recorded fixture:\n"
            f"{_case_diff(expected, payload)}\n"
            "If the wire format intentionally changed, re-record the section as a new "
            "fixture file (the plan's append-only rule); otherwise fix the code "
            "toward the fixture."
        )


def _case_diff(expected: dict, actual: dict) -> str:
    expected_cases = {(case["method"], case["path"]): case for case in expected["cases"]}
    actual_cases = {(case["method"], case["path"]): case for case in actual["cases"]}
    changed = [
        key
        for key in sorted(set(expected_cases) | set(actual_cases))
        if normalize(expected_cases.get(key)) != normalize(actual_cases.get(key))
    ]
    if not changed:
        return "(difference is in the fixture metadata, not in the cases)"
    lines = []
    for key in changed:
        want = json.dumps(normalize(expected_cases.get(key) or {}), indent=2, sort_keys=True)
        got = json.dumps(normalize(actual_cases.get(key) or {}), indent=2, sort_keys=True)
        lines.append(f"--- {key[0]} {key[1]}")
        lines.extend(
            difflib.unified_diff(
                want.splitlines(), got.splitlines(), "recorded", "actual", lineterm=""
            )
        )
    return "\n".join(lines)


def test_seed(backend):
    """Phase 0 seed: a couple of seeded assets, the four read endpoints, health,
    and two error cases (404 unknown asset, 422 on BrowseQuery)."""
    service = backend.service
    a = pinned_catalog_fixture(service, 1, "2024-05-01T00:10:00+13:00", media="RAW", camera="Canon")
    pinned_catalog_fixture(service, 2, "2024-05-01T00:10:00-08:00", camera="SONY")
    pinned_catalog_fixture(service, 3, None, media="HEIF", camera="Apple")

    run_sequence(
        backend,
        "seed",
        [
            ("GET", "/assets"),
            ("GET", "/library/assets"),
            ("GET", "/library/assets?limit=2"),
            ("GET", f"/assets/{a.asset_id}"),
            ("GET", "/health"),
            ("GET", f"/assets/{UNKNOWN_ASSET_ID}"),
            ("GET", "/library/assets?limit=0"),
        ],
        describe=(
            "seed: three pinned synthetic assets (RAW/Canon, JPEG/SONY, HEIF/Apple with "
            "no capture time); GET /assets, GET /library/assets (full page and a limit=2 "
            "page carrying a cursor), GET /assets/{id}, GET /health, 404 unknown asset, "
            "422 invalid BrowseQuery (limit=0)"
        ),
    )


# ---------------------------------------------------------------------------
# Phase 1a: response_model types for the four asset-read endpoints
# (GET /assets, GET /assets/{id}, GET /library/assets, GET /assets/{id}/burst).
# ---------------------------------------------------------------------------

PHASE1A_TECHNICAL = {
    "ImageWidth": 4000,
    "ImageHeight": 3000,
    "FNumber": 2.8,
    "FocalLength": 50.0,
    "FocalLengthIn35mmFormat": 50.0,
    "ISO": 100,
    "ExposureTime": "1/250",
    "ShutterSpeed": "1/250",
    "ExposureCompensation": 0,
    "ExposureProgram": "Manual",
    "MeteringMode": "Spot",
    "Flash": "auto",
    "WhiteBalance": "Auto",
}

PHASE1A_ANALYSIS_RESULT = {
    "summary": "A calm river at golden hour.",
    "photoTypes": ["landscape", "travel"],
    "scene": "riverside",
    "setting": "outdoor",
    "objects": [{"name": "river", "count": 1}, {"name": "tree", "count": 3}],
    "activities": ["walking"],
    "tags": ["golden", "river", "evening"],
    "visibleText": [],
    "faceCount": 1,
    "personCount": 1,
}


def seed_phase_1a(service) -> dict:
    """Seed the phase 1a scenario and return the pinned asset id per number.

    Shapes covered: a three-frame burst (cluster seeded directly), a v2
    user-state edit (asset 13), a completed AI analysis with a face and a
    person (asset 13), preview jobs in every status (10 pending, 11 running,
    12 ready, 13 failed, 14 missing, 15 unavailable, 16 pending), a failed
    metadata stage (asset 14), every analysis status (10/11/12/16 pending,
    13 ready, 14 failed, 15 missing), and a hidden v2 asset (16).
    """
    spec = {
        10: ("2024-05-01T00:10:00+08:00", "JPEG", "Canon", {}),
        11: ("2024-05-01T00:10:00+08:00", "JPEG", "Canon", {}),
        12: ("2024-05-01T00:10:01+08:00", "JPEG", "Canon", {}),
        13: ("2024-05-02T09:30:00-08:00", "JPEG", "Sony", PHASE1A_TECHNICAL),
        14: (None, "HEIF", "Apple", {}),
        15: ("2024-05-03T18:00:00Z", "RAW", "Nikon", {}),
        16: ("2024-05-04T12:00:00+05:30", "JPEG", "Fujifilm", {}),
    }
    made = {
        number: pinned_catalog_fixture(
            service, number, capture, media=media, camera=camera, extra_metadata=extra
        )
        for number, (capture, media, camera, extra) in spec.items()
    }
    asset_ids = {number: str(manifest.asset_id) for number, manifest in made.items()}

    with service.catalog.engine.begin() as connection:
        # Burst cluster: frame 11 is the representative.
        cluster_id = str(uuid5(GOLDEN_NAMESPACE, "burst-phase1a"))
        connection.execute(
            insert(burst_clusters).values(
                id=cluster_id,
                representative_asset_id=asset_ids[11],
                policy_version="burst-cluster-v1",
                created_at=datetime.fromisoformat("2025-01-02T12:00:00+00:00"),
            )
        )
        for number in (10, 11, 12):
            connection.execute(
                insert(burst_members).values(cluster_id=cluster_id, asset_id=asset_ids[number])
            )

    # v2 user-state edit on asset 13 (rating/favorite/caption/keywords/location).
    service.catalog.commit_mutation(
        uuid5(GOLDEN_NAMESPACE, "phase1a-operation-13"),
        Mutation(
            action="asset.patch",
            entity_id=made[13].asset_id,
            changes={
                "rating": 4,
                "favorite": True,
                "caption": "Golden hour by the river",
                "keywords": ["golden", "river"],
                "location": {"name": "Riverside Park", "latitude": 47.6062, "longitude": -122.3321},
            },
        ),
    )

    # Hide asset 16, then pin the wall-clock deletedAt stamp the mutation
    # writes so the recorded bytes are stable across runs.
    service.catalog.commit_mutation(
        uuid5(GOLDEN_NAMESPACE, "phase1a-operation-16"),
        Mutation(action="asset.delete", entity_id=made[16].asset_id, changes={}),
    )
    pinned_deleted_at = "2025-02-01T00:00:00+00:00"
    with service.catalog.engine.begin() as connection:
        document = (
            connection.execute(
                select(assets.c.manifest).where(assets.c.id == asset_ids[16])
            )
            .scalar_one()
        )
        document["deletedAt"] = pinned_deleted_at
        connection.execute(
            update(assets)
            .where(assets.c.id == asset_ids[16])
            .values(deleted_at=pinned_deleted_at, manifest=document)
        )

    with service.catalog.engine.begin() as connection:
        # Preview jobs in every status.
        for number, (status, error) in {
            11: ("running", None),
            12: ("ready", None),
            13: ("failed", "Decoder failure"),
            15: ("unavailable", "No embedded preview available"),
        }.items():
            connection.execute(
                update(jobs)
                .where(and_(jobs.c.asset_id == asset_ids[number], jobs.c.job_type == "preview-v1"))
                .values(status=status, attempts=1, error=error)
            )
        # 14 has no preview job row at all -> status "missing".
        connection.execute(
            delete(jobs).where(
                and_(jobs.c.asset_id == asset_ids[14], jobs.c.job_type == "preview-v1")
            )
        )
        # Asset 14's metadata stage failed; asset 15 has no AI job row
        # at all -> analysis status "missing"; asset 14's AI job failed.
        connection.execute(
            update(jobs)
            .where(and_(jobs.c.asset_id == asset_ids[14], jobs.c.job_type == "metadata-v1"))
            .values(status="failed", attempts=1, error="exiftool not found")
        )
        connection.execute(
            delete(jobs).where(
                and_(jobs.c.asset_id == asset_ids[15], jobs.c.job_type == "ai-v1")
            )
        )
        connection.execute(
            update(jobs)
            .where(and_(jobs.c.asset_id == asset_ids[14], jobs.c.job_type == "ai-v1"))
            .values(status="failed", attempts=1, error="simulated AI failure")
        )
        # Asset 13: a completed AI analysis with one face and one person.
        person_id = str(uuid5(GOLDEN_NAMESPACE, "phase1a-person-1"))
        run_id = str(uuid5(GOLDEN_NAMESPACE, "phase1a-run-13"))
        connection.execute(
            insert(people).values(
                id=person_id,
                display_name="Avery",
                created_at=datetime.fromisoformat("2025-01-02T12:00:00+00:00"),
            )
        )
        connection.execute(
            insert(analysis_runs).values(
                id=run_id,
                asset_id=asset_ids[13],
                analysis_type="photo-ai",
                model_name="stub-vlm",
                model_version="stub-digest-1",
                pipeline_version="photo-ai-v1",
                input_hash=made[13].primary.sha256,
                object_key=f"analysis/{asset_ids[13]}/photo-ai-v1/{run_id}.json",
                result=PHASE1A_ANALYSIS_RESULT,
                searchable_text=(
                    "A calm river at golden hour. landscape travel riverside outdoor "
                    "river tree walking golden river evening"
                ),
                is_current=True,
                semantic_origin="computed",
                created_at=datetime.fromisoformat("2025-01-05T08:30:00+00:00"),
            )
        )
        connection.execute(
            insert(faces).values(
                id=str(uuid5(GOLDEN_NAMESPACE, "phase1a-face-13-0")),
                asset_id=asset_ids[13],
                analysis_run_id=run_id,
                person_id=person_id,
                face_index=0,
                bounding_box=[0.1, 0.2, 0.3, 0.4],
                confidence=0.9,
                embedding=[0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8],
            )
        )
        connection.execute(
            update(jobs)
            .where(and_(jobs.c.asset_id == asset_ids[13], jobs.c.job_type == "ai-v1"))
            .values(status="ready", attempts=1, error=None)
        )

    return made


def test_phase_1a(backend):
    """Phase 1a: the four asset-read endpoints typed with response_model.

    The request list exercises every shape the phase 1a models declare:
    both document schema versions, every preview/analysis status, the burst
    detail, cursor paging, each browse filter, and the 404/422 error cases.
    The AI worker service is never started; the analysis state is seeded.
    """
    service = backend.service
    made = seed_phase_1a(service)
    ids = {number: str(manifest.asset_id) for number, manifest in made.items()}

    # The page-2 cursor derives from the exact page-1 boundary row (asset 15,
    # the newest-first ordering); the cursor is a pure function of the query
    # and that row, so it is computed here rather than read from the response.
    cursor = BrowseQuery(limit=2).encode_cursor(
        {"timeline_at": datetime(2024, 5, 3, 18, 0, 0), "id": ids[15]}
    )

    run_sequence(
        backend,
        "phase1a",
        [
            ("GET", "/assets"),
            ("GET", "/assets?limit=3&offset=3"),
            ("GET", f"/assets/{ids[10]}"),
            ("GET", f"/assets/{ids[13]}"),
            ("GET", f"/assets/{ids[14]}"),
            ("GET", f"/assets/{ids[15]}"),
            ("GET", f"/assets/{ids[16]}"),
            ("GET", f"/assets/{ids[11]}/burst"),
            ("GET", f"/assets/{ids[14]}/burst"),
            ("GET", "/library/assets"),
            ("GET", "/library/assets?limit=2"),
            ("GET", f"/library/assets?limit=2&cursor={cursor}"),
            ("GET", "/library/assets?media_type=RAW"),
            ("GET", "/library/assets?date_from=2024-05-01&date_to=2024-05-01"),
            ("GET", "/library/assets?q=river"),
            ("GET", "/library/assets?rating_min=4&favorite=true"),
            ("GET", "/library/assets?deleted=true"),
            ("GET", "/library/assets?date_from=2024-06-01&date_to=2024-05-01"),
            ("GET", f"/assets/{UNKNOWN_ASSET_ID}"),
        ],
        describe=(
            "phase1a: seven pinned assets covering a three-frame burst, a v2 "
            "user-state edit, a completed AI analysis (run + face + person), a "
            "hidden asset, preview jobs in every status, a failed metadata "
            "stage, and every analysis status; GET /assets (full and "
            "paginated), an asset detail for each shape, the burst detail and a "
            "404 for an asset with no burst, browse pages (full, limit=2 with a "
            "pinned cursor, media_type, date window, q, rating_min+favorite, "
            "deleted), a 422 on an inverted date range, and a 404 unknown asset"
        ),
    )
