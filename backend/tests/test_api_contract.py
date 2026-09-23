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

Bodies are captured as ``(method, path, status, body)`` cases and recorded
*as emitted*: only dictionary keys are sorted (JSON object key order is not
part of the contract — Postgres jsonb stores its own key order and
Pydantic serializes in model field order), while array order and numeric
spellings are preserved, so the fixture file is a faithful byte-level
transcript of the wire. The *comparison* against a recorded fixture applies
one further leniency: integral-valued floats equal integers, because JSON
has a single ``number`` type and no consumer can distinguish ``1`` from
``1.0``. That leniency never accepts a different *value* — it only closes
the spelling gap that lets a numeric field be declared ``StrictFloat``
(see ``api_schemas.py``) when one producer spells an integral number as
``1`` and another as ``1.0``.

Recording and verification
--------------------------
The first run of a section writes its fixture file under
``tests/fixtures/api_golden/`` (recorded as emitted); every later run must
match it under the comparison normalization above. Run a section twice
after recording to prove determinism.

Adding sections (Phases 1a onward)
----------------------------------
Append-only, by design: each phase adds a new fixture file and a new test
function that feeds its own request list to :func:`run_sequence`. Previously
recorded sections are never edited in place; if a later phase intentionally
changes a wire format it re-records that section as a *new file* (e.g.
``seed_v2.json``) so the old bytes stay reviewable in git history.

Sections whose scenario drives state changes through the API (phase 2's
upload lifecycle) may also use two extensions of :func:`run_sequence`:

- A request case can be a dict with ``method``, ``path``, and optional
  ``body`` (bytes) and ``headers``, so POST/PUT requests with request bodies
  are expressible. The recorded fixture still captures only the response
  ``(method, path, status, body)`` — the request bytes live in the test
  scenario code, not the fixture.
- A :class:`Hook` is a labelled between-request step (a SQL pin or seed)
  that runs inside the client session but records nothing. Phase 2 uses
  hooks to pin the wall-clock ``created_at``/``sealed_at`` stamps written by
  the endpoints and to move onboarding jobs to terminal states directly in
  SQL, so the recorded bytes are deterministic and no worker (let alone the
  AI worker service) is ever started.
- :func:`run_sequence` accepts an optional ``clock`` epoch: when set, the
  ``time`` function imported by the catalog and uploads modules is pinned to
  that epoch for the whole client session, so wall-clock fields that an
  endpoint writes *and echoes into its own recorded response* (``sealedAt``
  in the 202 seal body) are deterministic without touching the real clock
  (S3 signing, psycopg, and the process keep the real time).
"""

import contextlib
import difflib
import hashlib
import json
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from sqlalchemy import and_, delete, insert, select, update
from sqlalchemy.engine import make_url

import photo_server.api as api_module
from photo_server.api import create_app
from photo_server.browsing import BrowseQuery
from photo_server.catalog import (
    analysis_runs,
    assets,
    burst_clusters,
    burst_members,
    faces,
    jobs,
    onboarding_jobs,
    people,
    upload_batches,
    upload_files,
)
from photo_server.config import Settings
from photo_server.face_client import ADAFACE_IDENTITY
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
UNKNOWN_PERSON_ID = UUID("33333333-3333-4333-8333-333333333333")

FIXTURES = Path(__file__).parent / "fixtures" / "api_golden"


@dataclass
class Backend:
    service: Service
    fresh_catalog: object
    root: Path


@dataclass
class Hook:
    """A between-request step inside :func:`run_sequence`: the ``action``
    (typically SQL pinning or seeding) runs during the client session, in
    sequence with the request cases, but records nothing."""

    label: str
    action: Callable[[], None]


@pytest.fixture
def backend(tmp_path):
    base = Settings()
    root = tmp_path / "source"
    root.mkdir()
    # s3_bucket/import_root stay random and disposable; upload_workers and
    # the two AI service URLs are pinned because they flow into a recorded
    # body (/health's uploadWorkers and Phase 3B's four service-visibility
    # flags) and the goldens must not depend on the surrounding environment:
    # the recorded /health is always the AI-not-configured state.
    base = base.model_copy(
        update={
            "s3_bucket": f"photo-test-{uuid4().hex}",
            "import_root": root,
            "upload_workers": 4,
            "ai_base_url": "",
            "face_service_url": "",
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


def key_sort(value):
    """Recursive dictionary-key sorting — the only change made at recording.

    JSON object key order is not part of the contract (Postgres jsonb
    stores its own key order and Pydantic serializes in model field
    order); array order and numeric spellings are preserved, so the
    recorded fixture is a faithful byte-level transcript of the wire.
    """
    if isinstance(value, dict):
        return {key: key_sort(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [key_sort(item) for item in value]
    return value


def normalize(value):
    """The comparison normalization: key sorting plus number spelling.

    JSON has one ``number`` type and no consumer can distinguish ``1``
    from ``1.0``, so when a live body is compared against a recorded
    fixture, integral-valued floats equal their integers. No other value
    is ever rewritten or accepted.
    """
    value = key_sort(value)
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def capture(client, method, path, *, body=None, headers=None) -> dict:
    response = client.request(method, path, content=body, headers=headers)
    return {
        "method": method.upper(),
        "path": path,
        "status": response.status_code,
        "body": key_sort(response.json()) if response.content else None,
    }


def _case_request(case) -> tuple[str, str, bytes | None, dict | None]:
    """Unpack a request case: a ``(method, path)`` pair or a dict with
    ``method``/``path`` plus optional ``body`` (bytes) and ``headers``."""
    if isinstance(case, dict):
        return case["method"], case["path"], case.get("body"), case.get("headers")
    method, path = case
    return method, path, None, None


def run_sequence(
    backend: Backend,
    section: str,
    cases: list[tuple[str, str] | dict | Hook],
    *,
    describe: str,
    clock: int | None = None,
):
    """Record the section's fixture on first run, verify it on every run after.

    ``cases`` is executed against a fresh ``TestClient`` of the backend's app
    in the given order. Each entry is a request case (a ``(method, path)``
    pair or a dict with ``method``/``path`` plus optional ``body``/``headers``)
    or a :class:`Hook` (a between-request step that records nothing). Only
    request cases are recorded in the fixture.

    If ``clock`` is given, the ``time`` function imported by the catalog and
    uploads modules is pinned to that epoch for the whole client session, so
    wall-clock fields the endpoints write and echo into recorded bodies
    (notably ``sealedAt`` in the 202 seal response) are deterministic across
    runs. The real clock is used everywhere else: S3 request signing,
    psycopg, and the process itself.
    """
    stack = contextlib.ExitStack()
    if clock is not None:

        def pinned() -> float:
            return float(clock)

        stack.enter_context(mock.patch("photo_server.catalog.time", new=pinned))
        stack.enter_context(mock.patch("photo_server.uploads.time", new=pinned))
    with stack, TestClient(create_app(backend.service.settings)) as client:
        recorded = []
        for case in cases:
            if isinstance(case, Hook):
                case.action()
                continue
            method, path, body, headers = _case_request(case)
            recorded.append(capture(client, method, path, body=body, headers=headers))
    fixture = FIXTURES / f"{section}.json"
    payload = {
        "schemaVersion": 1,
        "section": section,
        "libraryId": str(GOLDEN_LIBRARY_ID),
        "description": describe,
        "normalization": (
            "recorded as emitted: dict keys sorted recursively, array "
            "order and numeric spellings preserved; comparison "
            "additionally equates integral-valued floats with integers "
            "(JSON numbers do not distinguish 1 from 1.0)"
        ),
        "cases": recorded,
    }
    if not fixture.exists():
        FIXTURES.mkdir(parents=True, exist_ok=True)
        fixture.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        print(f"\nrecorded {fixture} ({len(recorded)} cases) — review and commit it")
        return

    expected = json.loads(fixture.read_text())
    # The contract is the request -> response mapping; the metadata
    # fields (description, normalization policy) are prose for reviewers.
    if normalize(expected["cases"]) != normalize(payload["cases"]):
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
            "seed_flat",
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
                policy_version="burst-cluster-v2",
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
            "phase1a_flat",
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


# ---------------------------------------------------------------------------
# Phase 1b: response_model types for the two people-read endpoints
# (GET /people, GET /people/{id}).
# ---------------------------------------------------------------------------

PHASE1B_MINIMAL_RESULT = {
    "summary": "Phase 1b golden probe.",
    "photoTypes": ["portrait"],
    "scene": "indoor",
    "setting": "indoor",
    "objects": [],
    "activities": [],
    "tags": [],
    "visibleText": [],
    "faceCount": 1,
    "personCount": 1,
}


def seed_phase_1b(service) -> dict:
    """Seed the phase 1b scenario and return the pinned person id per key.

    Shapes covered: a named person with two faces on two distinct photos
    (``avery``; one face carries an all-integer bounding box and a confidence
    of exactly 1.0, exercising jsonb's integer normalization inside the
    SQL-built ``sampleFaces`` arrays), a second named person sharing that
    first photo (``sam``), an unnamed person (``unnamed``), a person whose
    only face sits on a hidden asset (``ghost``; excluded from ``/people``
    but whose detail returns empty arrays), and a person whose only face
    belongs to a non-current analysis run (``stale``; likewise excluded).
    """
    assets = {
        number: pinned_catalog_fixture(
            service,
            number,
            capture,
            media=media,
        )
        for number, (capture, media) in {
            10: ("2024-05-01T00:10:00+08:00", "JPEG"),
            11: ("2024-05-02T00:10:00+08:00", "JPEG"),
            12: ("2024-05-03T00:10:00+08:00", "JPEG"),
            13: (None, "HEIF"),
        }.items()
    }
    asset_ids = {number: str(manifest.asset_id) for number, manifest in assets.items()}
    persons = {
        key: str(uuid5(GOLDEN_NAMESPACE, f"phase1b-person-{number}"))
        for key, number in {"avery": 1, "sam": 2, "unnamed": 3, "ghost": 4, "stale": 5}.items()
    }

    with service.catalog.engine.begin() as connection:
        for key, (name, created) in {
            "avery": ("Avery", "2025-01-02T12:00:00+00:00"),
            "sam": ("Sam", "2025-01-02T12:05:00+00:00"),
            "unnamed": ("", "2025-01-02T12:10:00+00:00"),
            "ghost": ("Ghost", "2025-01-02T12:15:00+00:00"),
            "stale": ("Stale", "2025-01-02T12:20:00+00:00"),
        }.items():
            connection.execute(
                insert(people).values(
                    id=persons[key],
                    display_name=name,
                    created_at=datetime.fromisoformat(created),
                )
            )
        for number in (10, 11, 12, 13):
            run_id = str(uuid5(GOLDEN_NAMESPACE, f"phase1b-run-{number}"))
            connection.execute(
                insert(analysis_runs).values(
                    id=run_id,
                    asset_id=asset_ids[number],
                    analysis_type="photo-ai",
                    model_name="stub-vlm",
                    model_version="stub-digest-1",
                    pipeline_version="photo-ai-v1",
                    input_hash="0" * 64,
                    object_key=f"analysis/{asset_ids[number]}/photo-ai-v1/{run_id}.json",
                    result=PHASE1B_MINIMAL_RESULT,
                    searchable_text="Phase 1b golden probe portrait indoor",
                    is_current=True,
                    semantic_origin="computed",
                    created_at=datetime.fromisoformat("2025-01-05T08:30:00+00:00"),
                )
            )
        # A second, non-current run on asset 12; the ``stale`` face points
        # at it, so it never appears in a people response.
        stale_run = str(uuid5(GOLDEN_NAMESPACE, "phase1b-run-12b"))
        connection.execute(
            insert(analysis_runs).values(
                id=stale_run,
                asset_id=asset_ids[12],
                analysis_type="photo-ai",
                model_name="stub-vlm",
                model_version="stub-digest-2",
                pipeline_version="photo-ai-v1",
                input_hash="f" * 64,
                object_key=f"analysis/{asset_ids[12]}/photo-ai-v1/{stale_run}.json",
                result=PHASE1B_MINIMAL_RESULT,
                searchable_text="Phase 1b golden probe portrait indoor",
                is_current=False,
                semantic_origin="computed",
                created_at=datetime.fromisoformat("2025-01-06T08:30:00+00:00"),
            )
        )
        for tag, (number, run_key, person, index, box, confidence) in {
            "10-0": (10, "10", "avery", 0, [0.1, 0.2, 0.3, 0.4], 0.9),
            "11-0": (11, "11", "avery", 0, [0.0, 0.0, 1.0, 1.0], 1.0),
            "12-0": (12, "12", "unnamed", 0, [0.2, 0.3, 0.4, 0.5], 0.85),
            "10-1": (10, "10", "sam", 1, [0.5, 0.6, 0.7, 0.8], 0.75),
            "13-0": (13, "13", "ghost", 0, [0.3, 0.4, 0.5, 0.6], 0.7),
            "12-1": (12, "12b", "stale", 0, [0.4, 0.5, 0.6, 0.7], 0.65),
        }.items():
            run_id = (
                stale_run
                if run_key == "12b"
                else str(uuid5(GOLDEN_NAMESPACE, f"phase1b-run-{run_key}"))
            )
            connection.execute(
                insert(faces).values(
                    id=str(uuid5(GOLDEN_NAMESPACE, f"phase1b-face-{tag}")),
                    asset_id=asset_ids[number],
                    analysis_run_id=run_id,
                    person_id=persons[person],
                    face_index=index,
                    bounding_box=box,
                    confidence=confidence,
                    embedding=[0.1, 0.2, 0.3, 0.4],
                )
            )

    # Hide asset 13 so ``ghost``'s only face points at a deleted asset.
    service.catalog.commit_mutation(
        uuid5(GOLDEN_NAMESPACE, "phase1b-hide-13"),
        Mutation(action="asset.delete", entity_id=UUID(int=13), changes={}),
    )
    return persons


def phase_1b_cases(persons: dict) -> list[tuple[str, str]]:
    """The fixed request list for the phase 1b golden section."""
    return [
        ("GET", "/people"),
        ("GET", "/people?q=ave"),
        ("GET", "/people?q=photo-10"),
        ("GET", "/people?q=zzz"),
        ("GET", "/people?limit=2"),
        ("GET", "/people?limit=2&offset=2"),
        ("GET", f"/people/{persons['avery']}"),
        ("GET", f"/people/{persons['avery']}?limit=1"),
        ("GET", f"/people/{persons['avery']}?limit=1&offset=1"),
        ("GET", f"/people/{persons['sam']}"),
        ("GET", f"/people/{persons['unnamed']}"),
        ("GET", f"/people/{persons['ghost']}"),
        ("GET", f"/people/{persons['stale']}"),
        ("GET", f"/people/{UNKNOWN_PERSON_ID}"),
    ]


def test_phase_1b(backend):
    """Phase 1b: the two people-read endpoints typed with response_model.

    The request list exercises every shape the phase 1b models declare:
    named and unnamed people, SQL-built ``sampleFaces`` arrays (including
    the jsonb integer normalization of integral boxes/confidences), paged
    face lists, people with no visible faces (empty arrays), the query
    filters, and the 404 unknown person. The AI worker service is never
    started; all analysis/face state is seeded.
    """
    persons = seed_phase_1b(backend.service)
    run_sequence(
        backend,
        "phase1b",
        phase_1b_cases(persons),
        describe=(
            "phase1b: four pinned assets and five pinned people — Avery "
            "(two faces on two photos, one of them an integral box with "
            "confidence 1.0), Sam (sharing Avery's first photo), an unnamed "
            "person, Ghost (face on a hidden asset), and Stale (face on a "
            "non-current run); GET /people (full, q by name, q by filename, "
            "no match, limit/offset paging), a person detail for every "
            "person (including empty face arrays) plus limit/offset paging "
            "on Avery's faces, and a 404 unknown person"
        ),
    )


# ---------------------------------------------------------------------------
# Phase 2: upload and health endpoints.
#
# All five batches use client-chosen batch ids (UploadBatchRequest accepts
# batchId), so every identity in the section is uuid5-derived and the
# recorded bytes are reproducible on a fresh backend. created_at/sealed_at
# are wall-clock writes of the endpoints and are pinned in SQL by hooks
# immediately after each write; onboarding jobs move to their terminal
# states by direct SQL seeding (the worker — let alone the AI worker
# service — is never started), mirroring what finish_onboarding_job()
# would write.
# ---------------------------------------------------------------------------

GOLDEN_BATCH_A = uuid5(GOLDEN_NAMESPACE, "phase2-batch-a")
GOLDEN_BATCH_B = uuid5(GOLDEN_NAMESPACE, "phase2-batch-b")
GOLDEN_BATCH_C = uuid5(GOLDEN_NAMESPACE, "phase2-batch-c")
GOLDEN_BATCH_D = uuid5(GOLDEN_NAMESPACE, "phase2-batch-d")
GOLDEN_BATCH_E = uuid5(GOLDEN_NAMESPACE, "phase2-batch-e")
UNKNOWN_BATCH_ID = UUID("44444444-4444-4444-8444-444444444444")
UNKNOWN_FILE_ID = UUID("55555555-5555-4555-8555-555555555555")

# Pinned epoch-second stamps (base 2025-01-01T00:00:00Z). created_at drives
# GET /upload-batches ordering (created_at desc, id), so the pins also fix
# the list order: B (…640) > E (…635) > D (…630) > C (…620) > A (…600).
PINNED_CREATED_A = 1735689600
PINNED_SEALED_A = 1735689610
PINNED_CREATED_C = 1735689620
PINNED_CREATED_D = 1735689630
PINNED_CREATED_E = 1735689635
PINNED_SEALED_E = 1735689655
PINNED_CREATED_B = 1735689640
PINNED_SEALED_B = 1735689650

# The simulated onboarding failure recorded in batch B's job and file rows.
PINNED_ONBOARDING_ERROR = "Simulated onboarding failure (golden)"


def _upload_body(seed: str, size: int) -> bytes:
    """Deterministic request body: ``seed`` repeated, truncated to ``size``
    bytes, so every receipt sha256 is a pure function of (seed, size)."""
    data = seed.encode("ascii")
    return (data * (size // len(data) + 1))[:size]


def _file_id(batch_id: UUID, path: str) -> UUID:
    """uuid5 with the same derivation uploads.py _records() uses."""
    return uuid5(batch_id, f"file:{path}")


def _job_id(batch_id: UUID, path: str) -> UUID:
    """uuid5 with the same derivation catalog.seal_upload_batch() uses."""
    return uuid5(batch_id, f"onboard:{path}")


def _onboard_asset_id(service, job: UUID) -> UUID:
    """uuid5 with the same derivation uploads.py _commit_staged() uses."""
    return uuid5(service.library_id, f"upload:{job}")


def _pin_batch(
    service, batch_id: UUID, *, created_at: int | None = None, sealed_at: int | None = None
) -> None:
    values = {}
    if created_at is not None:
        values["created_at"] = created_at
    if sealed_at is not None:
        values["sealed_at"] = sealed_at
    with service.catalog.engine.begin() as connection:
        connection.execute(
            upload_batches.update().where(upload_batches.c.id == str(batch_id)).values(**values)
        )


def _set_batch_status(service, batch_id: UUID, status: str) -> None:
    with service.catalog.engine.begin() as connection:
        connection.execute(
            upload_batches.update().where(upload_batches.c.id == str(batch_id)).values(status=status)
        )


def _complete_onboarding_job(service, batch_id: UUID, primary: str, sidecars: list[str]) -> None:
    """Move one onboarding job to the same terminal state the worker's
    finish_onboarding_job() would write: job complete with its result blob
    and attempts counted, the primary and sidecar files imported with the
    shared asset id."""
    job = _job_id(batch_id, primary)
    asset_id = _onboard_asset_id(service, job)
    file_ids = [_file_id(batch_id, primary)] + [_file_id(batch_id, path) for path in sidecars]
    with service.catalog.engine.begin() as connection:
        connection.execute(
            onboarding_jobs.update()
            .where(onboarding_jobs.c.id == str(job))
            .values(
                status="complete",
                attempts=1,
                lease_until=None,
                result={"assetId": str(asset_id), "replayed": False, "status": "imported"},
                error=None,
            )
        )
        connection.execute(
            upload_files.update()
            .where(upload_files.c.id.in_([str(file) for file in file_ids]))
            .values(status="imported", asset_id=str(asset_id), error=None)
        )


def _fail_onboarding_jobs(service, batch_id: UUID, paths: list[str]) -> None:
    """Fail a sealed batch's onboarding jobs as finish_onboarding_job(error)
    would: jobs failed (attempts counted, error set), their files failed,
    and the batch status refreshed to failed."""
    with service.catalog.engine.begin() as connection:
        for path in paths:
            connection.execute(
                onboarding_jobs.update()
                .where(onboarding_jobs.c.id == str(_job_id(batch_id, path)))
                .values(status="failed", attempts=1, lease_until=None, error=PINNED_ONBOARDING_ERROR)
            )
            connection.execute(
                upload_files.update()
                .where(upload_files.c.id == str(_file_id(batch_id, path)))
                .values(status="failed", error=PINNED_ONBOARDING_ERROR)
            )
        connection.execute(
            upload_batches.update()
            .where(upload_batches.c.id == str(batch_id))
            .values(status="failed")
        )


def _post_batch(batch_id: UUID, files: list[dict]) -> dict:
    return {
        "method": "POST",
        "path": "/upload-batches",
        "body": json.dumps({"batchId": str(batch_id), "files": files}).encode("utf-8"),
        "headers": {"Content-Type": "application/json"},
    }


def _put_file(batch_id: UUID, path: str, size: int) -> dict:
    return {
        "method": "PUT",
        "path": f"/upload-batches/{batch_id}/files/{_file_id(batch_id, path)}",
        "body": _upload_body(path, size),
    }


def phase_2_cases(service) -> list:
    """The phase 2 request list: five pinned batches walking every upload
    lifecycle and error shape, then the list/limit, queue, and health reads.
    Hooks (recorded nothing) pin wall-clock stamps and seed onboarding
    terminal states between the requests."""
    a, b, c, d, e = GOLDEN_BATCH_A, GOLDEN_BATCH_B, GOLDEN_BATCH_C, GOLDEN_BATCH_D, GOLDEN_BATCH_E
    files_a = [
        {"path": "a/one.jpg", "sizeBytes": 100, "mimeType": "image/jpeg"},
        {"path": "a/one.xmp", "sizeBytes": 40, "mimeType": "application/rdf+xml"},
        {"path": "a/two.jpg", "sizeBytes": 100, "mimeType": "image/jpeg"},
        {"path": "a/three.jpg", "sizeBytes": 80, "mimeType": "image/jpeg"},
        {"path": "a/three.arw", "sizeBytes": 200, "mimeType": "image/x-raw-adorne"},
    ]
    files_b = [
        {"path": "b/one.jpg", "sizeBytes": 100, "mimeType": "image/jpeg"},
        {"path": "b/two.jpg", "sizeBytes": 100, "mimeType": "image/jpeg"},
    ]
    cases = []

    # A: the full lifecycle — create (two assets, one attached sidecar, one
    # raw-preferred skip) -> four uploads -> replayed PUT -> skipped-file
    # 409 -> seal -> seeded onboarding completion -> complete.
    cases.append(_post_batch(a, files_a))
    cases.append(Hook("pin batch A created_at", lambda: _pin_batch(service, a, created_at=PINNED_CREATED_A)))
    cases.append(("GET", f"/upload-batches/{a}"))
    cases.append(("GET", "/upload-batches?limit=100"))
    cases.append(_put_file(a, "a/one.jpg", 100))
    cases.append(_put_file(a, "a/one.xmp", 40))
    cases.append(_put_file(a, "a/two.jpg", 100))
    cases.append(_put_file(a, "a/three.arw", 200))
    cases.append(_put_file(a, "a/one.jpg", 100))  # replay: same body, replayed=true
    cases.append(_put_file(a, "a/three.jpg", 80))  # skipped file: 409 selection rules
    cases.append(("POST", f"/upload-batches/{a}/seal"))
    cases.append(
        Hook(
            "seed batch A onboarding completion",
            lambda: (
                _pin_batch(service, a, sealed_at=PINNED_SEALED_A),
                _complete_onboarding_job(service, a, "a/one.jpg", ["a/one.xmp"]),
                _complete_onboarding_job(service, a, "a/two.jpg", []),
                _complete_onboarding_job(service, a, "a/three.arw", []),
                _set_batch_status(service, a, "complete"),
            ),
        )
    )
    cases.append(("GET", f"/upload-batches/{a}"))

    # B: fail and retry — sealed, onboarding seeded to failed, POST retry
    # moves the jobs back to pending (attempts preserved) and the batch to
    # queued.
    cases.append(_post_batch(b, files_b))
    cases.append(Hook("pin batch B created_at", lambda: _pin_batch(service, b, created_at=PINNED_CREATED_B)))
    cases.append(_put_file(b, "b/one.jpg", 100))
    cases.append(_put_file(b, "b/two.jpg", 100))
    cases.append(("POST", f"/upload-batches/{b}/seal"))
    cases.append(
        Hook(
            "seed batch B onboarding failure",
            lambda: (
                _pin_batch(service, b, sealed_at=PINNED_SEALED_B),
                _fail_onboarding_jobs(service, b, ["b/one.jpg", "b/two.jpg"]),
            ),
        )
    )
    cases.append(("GET", f"/upload-batches/{b}"))
    cases.append(("POST", f"/upload-batches/{b}/retry"))
    cases.append(("GET", f"/upload-batches/{b}"))

    # C: create -> upload -> discard (BatchAbandonedOut) -> GET after
    # deletion: 409 "Upload batch not found" (the LibraryError contract).
    cases.append(_post_batch(c, [{"path": "c/one.jpg", "sizeBytes": 100, "mimeType": "image/jpeg"}]))
    cases.append(Hook("pin batch C created_at", lambda: _pin_batch(service, c, created_at=PINNED_CREATED_C)))
    cases.append(_put_file(c, "c/one.jpg", 100))
    cases.append(("DELETE", f"/upload-batches/{c}"))
    cases.append(("GET", f"/upload-batches/{c}"))

    # D: content-length mismatch (declared 50, sent 100 -> 409, file row
    # reset to waiting with the declaration error) -> seal 409 (required
    # file missing) -> discard.
    cases.append(_post_batch(d, [{"path": "d/one.jpg", "sizeBytes": 50, "mimeType": "image/jpeg"}]))
    cases.append(Hook("pin batch D created_at", lambda: _pin_batch(service, d, created_at=PINNED_CREATED_D)))
    cases.append(_put_file(d, "d/one.jpg", 100))
    cases.append(("GET", f"/upload-batches/{d}"))
    cases.append(("POST", f"/upload-batches/{d}/seal"))
    cases.append(("DELETE", f"/upload-batches/{d}"))

    # E: sealed batch — DELETE 409 (sealed batches cannot be discarded),
    # retry 409 (no failed jobs), PUT 409 (already sealed).
    cases.append(_post_batch(e, [{"path": "e/one.jpg", "sizeBytes": 100, "mimeType": "image/jpeg"}]))
    cases.append(Hook("pin batch E created_at", lambda: _pin_batch(service, e, created_at=PINNED_CREATED_E)))
    cases.append(_put_file(e, "e/one.jpg", 100))
    cases.append(("POST", f"/upload-batches/{e}/seal"))
    cases.append(Hook("pin batch E sealed_at", lambda: _pin_batch(service, e, sealed_at=PINNED_SEALED_E)))
    cases.append(("DELETE", f"/upload-batches/{e}"))
    cases.append(("POST", f"/upload-batches/{e}/retry"))
    cases.append(_put_file(e, "e/one.jpg", 100))

    # Misc: unknown file of a batch, unknown batch, empty declaration (422),
    # list/limit ordering, the queue, and health.
    cases.append(
        {"method": "PUT", "path": f"/upload-batches/{a}/files/{UNKNOWN_FILE_ID}", "body": _upload_body("x", 50)}
    )
    cases.append(("GET", f"/upload-batches/{UNKNOWN_BATCH_ID}"))
    cases.append(
        {
            "method": "POST",
            "path": "/upload-batches",
            "body": json.dumps({"files": []}).encode("utf-8"),
            "headers": {"Content-Type": "application/json"},
        }
    )
    cases.append(("GET", "/upload-batches?limit=1"))
    cases.append(("GET", "/upload-batches"))
    cases.append(("GET", "/upload-queue"))
    cases.append(("GET", "/health"))
    return cases


def test_phase_2(backend):
    """Phase 2: the upload lifecycle and the health endpoint typed with
    response_model; every endpoint is rewired but no bytes on the wire
    change.

    Five client-named batches: A walks the full lifecycle (five declared
    files — two assets, one attached sidecar, one raw-preferred skip;
    create -> four uploads -> replayed PUT -> skipped-file 409 -> seal ->
    seeded onboarding completion -> complete), B walks failure and retry
    (sealed, onboarding seeded failed, POST retry -> queued with attempts
    preserved), C is discarded after one upload (then 409 on GET), D hits
    the content-length mismatch and the missing-file seal 409 (then
    discarded), and E is sealed (DELETE 409, retry 409, PUT 409). Plus a
    409 unknown batch, a 409 unknown file, a 422 empty declaration, the
    created_at-ordered list with limit, /upload-queue, and /health. The AI
    worker service is never started; all onboarding terminal states are
    seeded in SQL and every wall-clock stamp is pinned by a hook.
    """
    run_sequence(
        backend,
        "phase2_v2",
        phase_2_cases(backend.service),
        clock=PINNED_SEALED_A,
        describe=(
            "phase2: five pinned batches (A complete with a sidecar, B "
            "failed then retried to queued, C discarded, D content-length "
            "mismatch then discarded, E sealed) exercising every upload "
            "lifecycle state and error shape, then GET /upload-batches "
            "(created_at-ordered, limit=1 and full), GET /upload-queue "
            "(13 counts plus gate statistics), and GET /health (ok, library "
            "id, asset/blob counts, every queue count, gate statistics, null "
            "backup marker); onboarding terminal states seeded in SQL, "
            "created_at/sealed_at pinned by hooks, the catalog clock pinned "
            "for the session (sealedAt in the seal 202 bodies), no workers "
            "started"
        ),
    )


# ---------------------------------------------------------------------------
# Phase 3a: albums (CRUD + restore).
#
# Every album identity is client-chosen: POST /albums derives the album id
# as uuid5(libraryId, "album:{operationId}"), so the whole section is a
# function of the pinned operation ids and the recorded bytes are
# reproducible on a fresh backend. The only wall-clock write that lands in
# a recorded body is the album deletion's deletedAt: the catalog module's
# ``datetime`` is patched for the client session (the TestClient lifespan
# and every request run inside the patch) so ``datetime.now(UTC)`` returns
# a fixed instant. The pre-seed below hides an asset before the patch
# starts; its stamp is pinned in SQL like the phase 1a seed and is never
# recorded. No worker — let alone the AI worker service — is started.
# ---------------------------------------------------------------------------

UNKNOWN_ALBUM_ID = UUID("66666666-6666-4666-8666-666666666666")

ALBUM_A_OPS = {
    "create": uuid5(GOLDEN_NAMESPACE, "phase3a-a-create"),
    "rename": uuid5(GOLDEN_NAMESPACE, "phase3a-a-rename"),
    "stale": uuid5(GOLDEN_NAMESPACE, "phase3a-a-stale"),
    "add-third": uuid5(GOLDEN_NAMESPACE, "phase3a-a-add-third"),
    "add-hidden": uuid5(GOLDEN_NAMESPACE, "phase3a-a-add-hidden"),
    "delete": uuid5(GOLDEN_NAMESPACE, "phase3a-a-delete"),
    "restore": uuid5(GOLDEN_NAMESPACE, "phase3a-a-restore"),
}
ALBUM_B_OPS = {
    "create": uuid5(GOLDEN_NAMESPACE, "phase3a-b-create"),
    "unknown-patch": uuid5(GOLDEN_NAMESPACE, "phase3a-unknown-patch"),
    "unknown-delete": uuid5(GOLDEN_NAMESPACE, "phase3a-unknown-delete"),
    "unknown-restore": uuid5(GOLDEN_NAMESPACE, "phase3a-unknown-restore"),
}
ALBUM_422_OPS = {"null-name": uuid5(GOLDEN_NAMESPACE, "phase3a-422-null-name")}
# The album ids the endpoint derives from the create operation ids.
ALBUM_A = uuid5(GOLDEN_LIBRARY_ID, f"album:{ALBUM_A_OPS['create']}")
ALBUM_B = uuid5(GOLDEN_LIBRARY_ID, f"album:{ALBUM_B_OPS['create']}")

# The fixed instant the patched catalog ``datetime.now(UTC)`` returns; the
# deletion's deletedAt is its ISO rendering.
PHASE3A_PINNED_NOW = "2025-03-01T00:00:00+00:00"
PHASE3A_HIDDEN_ASSET_STAMP = "2025-02-01T00:00:00+00:00"


class _PinnedNowDatetime(datetime):
    """Stand-in for the catalog module's ``datetime``: ``now()`` returns a
    fixed instant so the wall-clock ``deletedAt`` a deletion mutation
    writes is deterministic in the golden; every other ``datetime``
    operation keeps behaving normally."""

    @classmethod
    def now(cls, tz=None):
        instant = datetime(2025, 3, 1, 0, 0, 0, tzinfo=UTC)
        return instant if tz is None else instant.astimezone(tz)


def _phase3a_json_case(method: str, path: str, payload: dict) -> dict:
    return {
        "method": method,
        "path": path,
        "body": json.dumps(payload).encode("utf-8"),
        "headers": {"Content-Type": "application/json"},
    }


def seed_phase_3a(service) -> dict:
    """Seed three live assets and one hidden one for the membership
    checks. The hidden asset's deletion is a pre-seed step, so its
    wall-clock stamp is pinned in SQL (like the phase 1a seed) and never
    recorded."""
    made = {
        number: pinned_catalog_fixture(
            service, number, "2024-06-01T08:30:00+02:00", media="HEIF", camera="Leica"
        )
        for number in (71, 72, 73)
    }
    made[74] = pinned_catalog_fixture(service, 74, None, media="RAW", camera="Canon")
    hidden = made[74]
    service.catalog.commit_mutation(
        uuid5(GOLDEN_NAMESPACE, "phase3a-hide-74"),
        Mutation(action="asset.delete", entity_id=hidden.asset_id, changes={}),
    )
    with service.catalog.engine.begin() as connection:
        document = connection.execute(
            select(assets.c.manifest).where(assets.c.id == str(hidden.asset_id))
        ).scalar_one()
        document["deletedAt"] = PHASE3A_HIDDEN_ASSET_STAMP
        connection.execute(
            update(assets)
            .where(assets.c.id == str(hidden.asset_id))
            .values(deleted_at=PHASE3A_HIDDEN_ASSET_STAMP, manifest=document)
        )
    return {
        "members": [str(made[number].asset_id) for number in (71, 72, 73)],
        "hidden": str(made[74].asset_id),
    }


def phase_3a_cases(seeded: dict) -> list:
    a, unknown = str(ALBUM_A), str(UNKNOWN_ALBUM_ID)
    first_two, third = seeded["members"][:2], seeded["members"][2]
    return [
        # Album A: the full lifecycle.
        _phase3a_json_case(
            "POST",
            "/albums",
            {
                "operationId": str(ALBUM_A_OPS["create"]),
                "name": "Golden Trip",
                "description": "Pinned holiday frames.",
                "assetIds": first_two,
            },
        ),
        ("GET", "/albums"),
        ("GET", f"/albums/{a}"),
        _phase3a_json_case(
            "PATCH",
            f"/albums/{a}",
            {
                "operationId": str(ALBUM_A_OPS["rename"]),
                "expectedRevision": 1,
                "name": "Golden Trip II",
            },
        ),
        _phase3a_json_case(
            "PATCH",
            f"/albums/{a}",
            {
                "operationId": str(ALBUM_A_OPS["stale"]),
                "expectedRevision": 1,
                "description": "Stale revision conflict.",
            },
        ),
        _phase3a_json_case(
            "PATCH",
            f"/albums/{a}",
            {
                "operationId": str(ALBUM_A_OPS["add-third"]),
                "expectedRevision": 2,
                "assetIds": first_two + [third],
            },
        ),
        _phase3a_json_case(
            "PATCH",
            f"/albums/{a}",
            {
                "operationId": str(ALBUM_A_OPS["add-hidden"]),
                "expectedRevision": 3,
                "assetIds": [seeded["hidden"]],
            },
        ),
        _phase3a_json_case(
            "DELETE",
            f"/albums/{a}",
            {
                "operationId": str(ALBUM_A_OPS["delete"]),
                "expectedRevision": 3,
            },
        ),
        ("GET", "/albums"),
        ("GET", "/albums?deleted=true"),
        _phase3a_json_case(
            "POST", f"/albums/{a}/restore", {"operationId": str(ALBUM_A_OPS["restore"])}
        ),
        ("GET", "/albums"),
        ("GET", f"/albums/{a}"),
        ("GET", "/albums?deleted=true"),
        # Album B: a minimal create, pinning the list order.
        _phase3a_json_case(
            "POST",
            "/albums",
            {"operationId": str(ALBUM_B_OPS["create"]), "name": "Second"},
        ),
        ("GET", "/albums"),
        # Unknown album: both 404 shapes (the endpoint's own and the
        # commit path's "requested item does not exist").
        ("GET", f"/albums/{unknown}"),
        _phase3a_json_case(
            "PATCH",
            f"/albums/{unknown}",
            {
                "operationId": str(ALBUM_B_OPS["unknown-patch"]),
                "expectedRevision": 1,
                "name": "Ghost",
            },
        ),
        _phase3a_json_case(
            "DELETE",
            f"/albums/{unknown}",
            {"operationId": str(ALBUM_B_OPS["unknown-delete"])},
        ),
        _phase3a_json_case(
            "POST",
            f"/albums/{unknown}/restore",
            {"operationId": str(ALBUM_B_OPS["unknown-restore"])},
        ),
        # Request validation: a bodyless create (operationId missing) and
        # an explicit null field.
        _phase3a_json_case("POST", "/albums", {}),
        _phase3a_json_case(
            "POST",
            "/albums",
            {"operationId": str(ALBUM_422_OPS["null-name"]), "name": None},
        ),
    ]


def test_phase_3a(backend):
    """Phase 3a: the six album operations become schema-complete
    (``response_model=AlbumOut``).

    Golden: create (with members) -> list -> get -> rename -> stale
    409 -> add member -> hidden-add 409 -> delete (pinned clock) ->
    deleted list -> restore; a second album pins the list order; 404s on
    get/patch/delete/restore of an unknown album; 422s on a bodyless
    create and a null name. The catalog's ``datetime.now()`` is pinned
    for the session, so the deletion's ``deletedAt`` is the fixed
    instant; no workers are started.
    """
    seeded = seed_phase_3a(backend.service)
    with mock.patch("photo_server.catalog.datetime", new=_PinnedNowDatetime):
        run_sequence(
            backend,
            "phase3a",
            phase_3a_cases(seeded),
            describe=(
                "phase3a: album CRUD and restore on two pinned albums "
                "(ids derived from client-chosen operation ids): A walks "
                "create (with members) -> list -> get -> rename -> "
                "stale-revision 409 -> add third member -> add hidden "
                "asset 409 -> delete (pinned wall-clock deletedAt) -> "
                "deleted list -> restore; B is a minimal create pinning "
                "the list order; 404s on get/patch/delete/restore of an "
                "unknown album; 422s on a bodyless create and a null "
                "name; catalog datetime.now() pinned for the session; no "
                "workers started"
            ),
        )


# ---------------------------------------------------------------------------
# Phase 3b: asset mutations and queue operations.
#
# Asset identities are ``pinned_catalog_fixture`` numbers (uuid5-free,
# UUID(int=number)); every operation id and the burst cluster id are
# uuid5-derived, so the whole section is reproducible on a fresh backend.
# The only wall-clock write that lands in a recorded body is the asset
# deletion's deletedAt: the catalog module's ``datetime`` is patched for
# the client session (a distinct pinned instant from phase 3a's), exactly
# like the phase 3a section. Job rows are seeded directly in SQL (import
# creates preview-v1/ai-v1 pending and metadata-v1 ready rows), so the
# queue responses' counters are deterministic and no worker — let alone
# the AI worker service — is started.
# ---------------------------------------------------------------------------

PHASE3B_OPS = {
    "31-userstate": uuid5(GOLDEN_NAMESPACE, "phase3b-31-userstate"),
    "31-userstate-stale": uuid5(GOLDEN_NAMESPACE, "phase3b-31-userstate-stale"),
    "31-delete": uuid5(GOLDEN_NAMESPACE, "phase3b-31-delete"),
    "31-userstate-hidden": uuid5(GOLDEN_NAMESPACE, "phase3b-31-userstate-hidden"),
    "31-restore": uuid5(GOLDEN_NAMESPACE, "phase3b-31-restore"),
    "32-userstate": uuid5(GOLDEN_NAMESPACE, "phase3b-32-userstate"),
    "32-userstate-nofields": uuid5(GOLDEN_NAMESPACE, "phase3b-32-userstate-nofields"),
    "32-userstate-badrating": uuid5(GOLDEN_NAMESPACE, "phase3b-32-userstate-badrating"),
    "33-rep": uuid5(GOLDEN_NAMESPACE, "phase3b-33-rep"),
    "36-rep": uuid5(GOLDEN_NAMESPACE, "phase3b-36-rep"),
    "unknown-userstate": uuid5(GOLDEN_NAMESPACE, "phase3b-unknown-userstate"),
    "unknown-delete": uuid5(GOLDEN_NAMESPACE, "phase3b-unknown-delete"),
    "unknown-restore": uuid5(GOLDEN_NAMESPACE, "phase3b-unknown-restore"),
    "unknown-rep": uuid5(GOLDEN_NAMESPACE, "phase3b-unknown-rep"),
}
PHASE3B_CLUSTER_ID = str(uuid5(GOLDEN_NAMESPACE, "burst-phase3b"))
# The fixed instant the patched catalog ``datetime.now(UTC)`` returns in
# this section (distinct from phase 3a's so the two fixtures differ); the
# asset deletion's deletedAt is its ISO rendering.
PHASE3B_PINNED_NOW = "2025-04-01T00:00:00+00:00"
PHASE3B_AI_ERROR = "simulated phase3b AI failure"


class _Phase3bPinnedNowDatetime(_PinnedNowDatetime):
    """Phase 3b's variant of the phase 3a pinned clock: same mechanism,
    the distinct fixed instant above."""

    @classmethod
    def now(cls, tz=None):
        instant = datetime(2025, 4, 1, 0, 0, 0, tzinfo=UTC)
        return instant if tz is None else instant.astimezone(tz)


def _phase3b_json_case(method: str, path: str, payload: dict) -> dict:
    return {
        "method": method,
        "path": path,
        "body": json.dumps(payload).encode("utf-8"),
        "headers": {"Content-Type": "application/json"},
    }


def seed_phase_3b(service) -> dict:
    """Seed the phase 3b scenario: six pinned assets (31-36), a three-frame
    burst cluster (33-35, representative 34), and job rows in mixed states
    so the queue operations' counters are interesting: 32's metadata job
    running and its AI job failed, 33's metadata/AI jobs missing (the
    import-created rows deleted), 31's preview job running. Returns the
    pinned asset id per number."""
    spec = {
        31: ("2024-06-01T10:00:00+02:00", "JPEG", "Sony"),
        32: ("2024-06-01T11:00:00+02:00", "JPEG", "Canon"),
        33: ("2024-06-02T10:00:00+02:00", "JPEG", "Nikon"),
        34: ("2024-06-02T10:00:01+02:00", "JPEG", "Nikon"),
        35: ("2024-06-02T10:00:02+02:00", "JPEG", "Nikon"),
        36: ("2024-06-03T10:00:00+02:00", "RAW", "Apple"),
    }
    made = {
        number: pinned_catalog_fixture(
            service, number, capture, media=media, camera=camera
        )
        for number, (capture, media, camera) in spec.items()
    }
    asset_ids = {number: str(manifest.asset_id) for number, manifest in made.items()}

    with service.catalog.engine.begin() as connection:
        # Burst cluster: frame 34 is the representative until the section
        # moves it to 33 through the API.
        connection.execute(
            insert(burst_clusters).values(
                id=PHASE3B_CLUSTER_ID,
                representative_asset_id=asset_ids[34],
                policy_version="burst-cluster-v2",
                created_at=datetime.fromisoformat("2025-01-02T12:00:00+00:00"),
            )
        )
        for number in (33, 34, 35):
            connection.execute(
                insert(burst_members).values(
                    cluster_id=PHASE3B_CLUSTER_ID, asset_id=asset_ids[number]
                )
            )
        # Mixed job states for the queue operations.
        connection.execute(
            jobs.update()
            .where(jobs.c.asset_id == asset_ids[32], jobs.c.job_type == "metadata-v1")
            .values(status="running", attempts=1, lease_until=None)
        )
        connection.execute(
            jobs.update()
            .where(jobs.c.asset_id == asset_ids[32], jobs.c.job_type == "ai-v1")
            .values(status="failed", attempts=1, error=PHASE3B_AI_ERROR)
        )
        connection.execute(
            delete(jobs).where(
                jobs.c.asset_id == asset_ids[33], jobs.c.job_type.in_(("metadata-v1", "ai-v1"))
            )
        )
        connection.execute(
            jobs.update()
            .where(jobs.c.asset_id == asset_ids[31], jobs.c.job_type == "preview-v1")
            .values(status="running", attempts=1, error=None)
        )
    return asset_ids


def phase_3b_cases(asset_ids: dict) -> list:
    """The phase 3b request list: the four asset-mutation endpoints
    (user-state/metadata patch, delete, restore), the burst representative
    endpoint, and the four queue endpoints, walking every success shape
    (including the idempotent-replay path), every 409/422 the endpoints
    raise, and both 404 detail shapes."""
    a = {number: f"/assets/{asset_ids[number]}" for number in asset_ids}
    unknown = f"/assets/{UNKNOWN_ASSET_ID}"
    ops = PHASE3B_OPS
    full_state = {
        "rating": 3,
        "favorite": True,
        "caption": "Burst test frame",
        "keywords": ["burst", "test"],
        "location": {"name": "Harbor", "latitude": -33.8688, "longitude": 151.2093},
    }
    cases = [
        # --- asset 31: the full mutation lifecycle -------------------------
        # v1 -> v2 user-state patch: the full five-field state.
        _phase3b_json_case("PATCH", f"{a[31]}/user-state", {
            "operationId": str(ops["31-userstate"]),
            **full_state,
        }),
        # Stale expectedRevision (asset is at revision 2 now) -> 409.
        _phase3b_json_case("PATCH", f"{a[31]}/user-state", {
            "operationId": str(ops["31-userstate-stale"]),
            "expectedRevision": 1,
            "caption": "Stale edit",
        }),
        # Idempotent replay: the exact first request again -> the stored
        # result, byte-identical to the first 200.
        _phase3b_json_case("PATCH", f"{a[31]}/user-state", {
            "operationId": str(ops["31-userstate"]),
            **full_state,
        }),
        # The same operation id with a different request -> 409.
        _phase3b_json_case("PATCH", f"{a[31]}/user-state", {
            "operationId": str(ops["31-userstate"]),
            "rating": 1,
        }),
        # Delete: v2 -> v3 with the pinned wall-clock deletedAt.
        _phase3b_json_case("DELETE", a[31], {
            "operationId": str(ops["31-delete"]),
            "expectedRevision": 2,
        }),
        # Editing a hidden asset -> 409 "Unhide this item before editing".
        _phase3b_json_case("PATCH", f"{a[31]}/user-state", {
            "operationId": str(ops["31-userstate-hidden"]),
            "rating": 2,
        }),
        # Restore: v3 -> v4, deletedAt back to null.
        _phase3b_json_case("POST", f"{a[31]}/restore", {
            "operationId": str(ops["31-restore"]),
            "expectedRevision": 3,
        }),
        # --- asset 32: the second decorator of the dual route --------------
        # PATCH /metadata is the same endpoint function; favorite-only
        # change -> v2 with every other user-state field at its default.
        _phase3b_json_case("PATCH", f"{a[32]}/metadata", {
            "operationId": str(ops["32-userstate"]),
            "favorite": True,
        }),
        # Idempotent replay of the metadata-route patch.
        _phase3b_json_case("PATCH", f"{a[32]}/metadata", {
            "operationId": str(ops["32-userstate"]),
            "favorite": True,
        }),
        # No change fields -> 422 "Provide at least one metadata field".
        _phase3b_json_case("PATCH", f"{a[32]}/user-state", {
            "operationId": str(ops["32-userstate-nofields"]),
        }),
        # Out-of-range rating -> 422.
        _phase3b_json_case("PATCH", f"{a[32]}/user-state", {
            "operationId": str(ops["32-userstate-badrating"]),
            "rating": 9,
        }),
        # --- burst representative ------------------------------------------
        # Move the cluster's representative from 34 to 33 (a member).
        _phase3b_json_case("POST", f"{a[33]}/burst/representative", {
            "operationId": str(ops["33-rep"]),
        }),
        # An asset outside any burst -> 409 (before the mutation commits).
        _phase3b_json_case("POST", f"{a[36]}/burst/representative", {
            "operationId": str(ops["36-rep"]),
        }),
        # --- queue operations ----------------------------------------------
        # Targeted processing: 31's import-ready row re-queues, 32's running
        # row is skipped, 33's missing row is created.
        _phase3b_json_case("POST", "/processing", {
            "assetIds": [asset_ids[31], asset_ids[32], asset_ids[33]],
            "stages": ["metadata"],
            "includeDeleted": False,
        }),
        # Same asset again: its row is pending now -> already queued.
        _phase3b_json_case("POST", "/processing", {
            "assetIds": [asset_ids[31]],
            "stages": ["metadata"],
            "includeDeleted": False,
        }),
        # The whole library (defaults: all stages, no hidden assets):
        # 31/33 pending, 32 running, 34-36 import-ready.
        _phase3b_json_case("POST", "/processing", {}),
        # Targeted analysis: 31 pending, 32 failed (re-queued), 33 missing
        # (created).
        _phase3b_json_case("POST", "/analysis", {
            "assetIds": [asset_ids[31], asset_ids[32], asset_ids[33]],
            "includeDeleted": False,
            "forceFull": False,
        }),
        # The whole library: every AI row is pending by now.
        _phase3b_json_case("POST", "/analysis", {}),
        # Single-asset retry with forceFull: the pending row gets the flag
        # but still counts as already queued.
        {"method": "POST", "path": f"{a[32]}/analysis/retry?forceFull=true"},
        # Single-asset retry with the default query.
        {"method": "POST", "path": f"{a[31]}/analysis/retry"},
        # Preview retry on a running job: the upsert skips running rows, so
        # the status echoes back running with a null error.
        {"method": "POST", "path": f"{a[31]}/preview/retry"},
        # Preview retry on an import-pending row: stays pending.
        {"method": "POST", "path": f"{a[32]}/preview/retry"},
        # --- unknown assets: both 404 detail shapes -------------------------
        # commit_mutation's FileNotFoundError -> the generic detail string.
        _phase3b_json_case("PATCH", f"{unknown}/user-state", {
            "operationId": str(ops["unknown-userstate"]),
            "rating": 1,
        }),
        _phase3b_json_case("DELETE", unknown, {"operationId": str(ops["unknown-delete"])}),
        _phase3b_json_case("POST", f"{unknown}/restore", {
            "operationId": str(ops["unknown-restore"]),
        }),
        # find()'s HTTPException detail (burst representative, analysis and
        # preview retry call it before touching the queue).
        _phase3b_json_case("POST", f"{unknown}/burst/representative", {
            "operationId": str(ops["unknown-rep"]),
        }),
        _phase3b_json_case("POST", "/processing", {"assetIds": [str(UNKNOWN_ASSET_ID)]}),
        _phase3b_json_case("POST", "/analysis", {"assetIds": [str(UNKNOWN_ASSET_ID)]}),
        {"method": "POST", "path": f"{unknown}/analysis/retry"},
        {"method": "POST", "path": f"{unknown}/preview/retry"},
        # --- request validation 422s ----------------------------------------
        _phase3b_json_case("POST", "/processing", {"stages": ["bogus"]}),
        _phase3b_json_case("POST", "/analysis", {"assetIds": []}),
    ]
    return cases


def test_phase_3b(backend):
    """Phase 3b: the nine asset-mutation and queue operations become
    schema-complete (``response_model=MutationResultOut`` /
    ``BurstRepresentativeOut`` / ``QueueResultOut`` / the reused
    ``PreviewStatusOut``).

    Golden: asset 31 walks user-state patch (full state) -> stale-revision
    409 -> idempotent replay -> operation-id reuse 409 -> delete (pinned
    wall-clock deletedAt) -> hidden-edit 409 -> restore; asset 32 patches
    through the /metadata route (favorite only) with an idempotent replay
    plus 422s (no fields, out-of-range rating); the burst cluster (33-35)
    moves its representative to 33 and asset 36 gets the not-in-a-burst
    409; /processing and /analysis run targeted and library-wide over a
    mixed ready/running/missing/pending/failed job state, then the
    single-asset /analysis/retry (forceFull and default) and /preview/retry
    on running and pending preview jobs; 404s for unknown assets on every
    endpoint (both detail shapes); 422s on a bogus processing stage and an
    empty analysis asset list. The catalog's ``datetime.now()`` is pinned
    to this section's instant for the session; no workers are started.
    """
    asset_ids = seed_phase_3b(backend.service)
    with mock.patch("photo_server.catalog.datetime", new=_Phase3bPinnedNowDatetime):
        run_sequence(
            backend,
            "phase3b",
            phase_3b_cases(asset_ids),
            describe=(
                "phase3b: asset mutations and queue operations on six "
                "pinned assets: 31 walks user-state patch (full state) -> "
                "stale-revision 409 -> idempotent replay -> operation-id "
                "reuse 409 -> delete (pinned wall-clock deletedAt) -> "
                "hidden-edit 409 -> restore; 32 patches through the "
                "/metadata route (favorite only) with an idempotent "
                "replay, plus 422s (no fields, out-of-range rating); "
                "burst frames 33-35 move the representative to 33 and "
                "asset 36 gets the not-in-a-burst 409; /processing and "
                "/analysis run targeted and library-wide over a mixed "
                "ready/running/missing/pending/failed job state, then "
                "/analysis/retry (forceFull and default) and "
                "/preview/retry on running and pending preview jobs; "
                "404s for unknown assets on every endpoint (both detail "
                "shapes); 422s on a bogus processing stage and an empty "
                "analysis asset list; catalog datetime.now() pinned for "
                "the session; no workers started"
            ),
        )


# ---------------------------------------------------------------------------
# Phase 4: face-operation results (rename/merge/move), the binary
# face-thumbnail contract, and the storage-verify report.
# ---------------------------------------------------------------------------

UNKNOWN_FACE_ID = UUID("77777777-7777-4777-8777-777777777777")

PHASE4_MINIMAL_RESULT = {
    "summary": "Phase 4 golden probe.",
    "photoTypes": ["portrait"],
    "scene": "studio",
    "setting": "indoor",
    "objects": [],
    "activities": [],
    "tags": ["golden"],
    "visibleText": [],
    "faceCount": 1,
    "personCount": 1,
}

PHASE4_PERSONS = {
    key: uuid5(GOLDEN_NAMESPACE, f"phase4-person-{number}")
    for key, number in {
        "avery": 1,
        "sam": 2,
        "ben": 3,
        "ghost": 4,
        "stale": 5,
        "new": 6,  # the person a targetless face move creates (uuid4 pinned)
    }.items()
}

PHASE4_OPS = {
    key: uuid5(GOLDEN_NAMESPACE, f"phase4-op-{key}")
    for key in (
        "rename",
        "rename-unknown",
        "merge-same",
        "merge",
        "merge-src-missing",
        "merge-tgt-missing",
        "move-to-avery",
        "move-new",
        "move-stale",
        "move-unknown-face",
        "move-tgt-missing",
        "move-dup",
        "move-empty",
    )
}


def seed_phase_4(service) -> dict:
    """Seed the phase 4 scenario and return the pinned ids per key.

    Four assets (81-84) each carry one current photo-ai run and faces:
    avery (one face on 81 and one on 82), sam (one face on 81), ben (one
    face on 83), and an unnamed ghost (one face on 84); asset 84 also
    carries a second, non-current run whose face is stale. The ghost
    asset's preview job is pinned ``unavailable`` so its face thumbnail
    takes the 404 branch instead of the 202 branch.
    """
    assets = {
        number: pinned_catalog_fixture(
            service,
            number,
            capture,
            media=media,
        )
        for number, (capture, media) in {
            81: ("2024-07-01T00:10:00+08:00", "JPEG"),
            82: ("2024-07-02T00:10:00+08:00", "JPEG"),
            83: ("2024-07-03T00:10:00+08:00", "HEIF"),
            84: (None, "JPEG"),
        }.items()
    }
    asset_ids = {number: str(manifest.asset_id) for number, manifest in assets.items()}
    runs = {
        number: str(uuid5(GOLDEN_NAMESPACE, f"phase4-run-{number}"))
        for number in (81, 82, 83, 84)
    }
    stale_run = str(uuid5(GOLDEN_NAMESPACE, "phase4-run-84b"))
    face_ids = {tag: uuid5(GOLDEN_NAMESPACE, f"phase4-face-{tag}") for tag in ("81a", "82a", "81b", "83b", "84g", "84s")}

    with service.catalog.engine.begin() as connection:
        for key, (name, created) in {
            "avery": ("Avery", "2025-01-02T12:00:00+00:00"),
            "sam": ("Sam", "2025-01-02T12:05:00+00:00"),
            "ben": ("Ben", "2025-01-02T12:10:00+00:00"),
            "ghost": ("", "2025-01-02T12:15:00+00:00"),
            "stale": ("Stale", "2025-01-02T12:20:00+00:00"),
        }.items():
            connection.execute(
                insert(people).values(
                    id=str(PHASE4_PERSONS[key]),
                    display_name=name,
                    created_at=datetime.fromisoformat(created),
                )
            )
        for number in (81, 82, 83, 84):
            connection.execute(
                insert(analysis_runs).values(
                    id=runs[number],
                    asset_id=asset_ids[number],
                    analysis_type="photo-ai",
                    model_name="stub-vlm",
                    model_version="stub-digest-1",
                    pipeline_version="photo-ai-v1",
                    input_hash="0" * 64,
                    object_key=f"analysis/{asset_ids[number]}/photo-ai-v1/{runs[number]}.json",
                    result=PHASE4_MINIMAL_RESULT,
                    searchable_text="Phase 4 golden probe portrait studio",
                    is_current=True,
                    semantic_origin="computed",
                    created_at=datetime.fromisoformat("2025-01-05T08:30:00+00:00"),
                )
            )
        connection.execute(
            insert(analysis_runs).values(
                id=stale_run,
                asset_id=asset_ids[84],
                analysis_type="photo-ai",
                model_name="stub-vlm",
                model_version="stub-digest-2",
                pipeline_version="photo-ai-v1",
                input_hash="f" * 64,
                object_key=f"analysis/{asset_ids[84]}/photo-ai-v1/{stale_run}.json",
                result=PHASE4_MINIMAL_RESULT,
                searchable_text="Phase 4 golden probe portrait studio",
                is_current=False,
                semantic_origin="computed",
                created_at=datetime.fromisoformat("2025-01-06T08:30:00+00:00"),
            )
        )
        for tag, (number, run, person, index, box, confidence) in {
            "81a": (81, runs[81], "avery", 0, [0.1, 0.2, 0.3, 0.4], 0.9),
            "82a": (82, runs[82], "avery", 0, [0.0, 0.0, 1.0, 1.0], 1.0),
            "81b": (81, runs[81], "sam", 1, [0.5, 0.6, 0.7, 0.8], 0.75),
            "83b": (83, runs[83], "ben", 0, [0.2, 0.3, 0.4, 0.5], 0.85),
            "84g": (84, runs[84], "ghost", 0, [0.3, 0.4, 0.5, 0.6], 0.7),
            "84s": (84, stale_run, "stale", 1, [0.4, 0.5, 0.6, 0.7], 0.65),
        }.items():
            connection.execute(
                insert(faces).values(
                    id=str(face_ids[tag]),
                    asset_id=asset_ids[number],
                    analysis_run_id=run,
                    person_id=str(PHASE4_PERSONS[person]),
                    face_index=index,
                    bounding_box=box,
                    confidence=confidence,
                    embedding=[0.1, 0.2, 0.3, 0.4],
                )
            )
        # The ghost asset's preview job is unavailable: its face thumbnail
        # must take the 404 branch instead of the 202 branch.
        connection.execute(
            update(jobs)
            .where(
                jobs.c.asset_id == asset_ids[84],
                jobs.c.job_type == "preview-v1",
            )
            .values(status="unavailable", error="No embedded preview in the source file")
        )
    return {
        "persons": {key: str(value) for key, value in PHASE4_PERSONS.items()},
        "faces": {key: str(value) for key, value in face_ids.items()},
        "ops": {key: str(value) for key, value in PHASE4_OPS.items()},
    }


def _phase4_json_case(method: str, path: str, body: dict) -> dict:
    return {
        "method": method,
        "path": path,
        "body": json.dumps(body).encode(),
        "headers": {"Content-Type": "application/json"},
    }


def phase_4_cases(seeded: dict) -> list:
    avery = seeded["persons"]["avery"]
    sam = seeded["persons"]["sam"]
    ben = seeded["persons"]["ben"]
    f81a = seeded["faces"]["81a"]
    f82a = seeded["faces"]["82a"]
    f83b = seeded["faces"]["83b"]
    f84g = seeded["faces"]["84g"]
    f84s = seeded["faces"]["84s"]
    ops = seeded["ops"]
    return [
        # person.rename: rename, idempotent replay, operation-id reuse
        # 409, and the unknown-person 404.
        _phase4_json_case(
            "PATCH", f"/people/{avery}", {"operationId": ops["rename"], "displayName": "Avery L."}
        ),
        _phase4_json_case(
            "PATCH", f"/people/{avery}", {"operationId": ops["rename"], "displayName": "Avery L."}
        ),
        _phase4_json_case(
            "PATCH", f"/people/{avery}", {"operationId": ops["rename"], "displayName": "Clash"}
        ),
        _phase4_json_case(
            "PATCH",
            f"/people/{UNKNOWN_PERSON_ID}",
            {"operationId": ops["rename-unknown"], "displayName": "Nobody"},
        ),
        # person.merge: same-person 409 first (it must not consume a
        # person), then sam into avery (one face moves) plus an
        # idempotent replay, then 404s for a missing source and a missing
        # target.
        _phase4_json_case(
            "POST",
            f"/people/{avery}/merge",
            {"operationId": ops["merge-same"], "targetPersonId": avery},
        ),
        _phase4_json_case(
            "POST",
            f"/people/{sam}/merge",
            {"operationId": ops["merge"], "targetPersonId": avery},
        ),
        _phase4_json_case(
            "POST",
            f"/people/{sam}/merge",
            {"operationId": ops["merge"], "targetPersonId": avery},
        ),
        _phase4_json_case(
            "POST",
            f"/people/{UNKNOWN_PERSON_ID}/merge",
            {"operationId": ops["merge-src-missing"], "targetPersonId": avery},
        ),
        _phase4_json_case(
            "POST",
            f"/people/{ben}/merge",
            {"operationId": ops["merge-tgt-missing"], "targetPersonId": str(UNKNOWN_PERSON_ID)},
        ),
        # faces.move: ben's face to the existing avery, replay; the ghost
        # face with no target (creates a pinned new person), replay; then
        # 409s for a stale and an unknown face, a 404 for a missing
        # target, and 422s for duplicate and empty face lists.
        _phase4_json_case(
            "POST",
            "/faces/move",
            {"operationId": ops["move-to-avery"], "faceIds": [f83b], "targetPersonId": avery},
        ),
        _phase4_json_case(
            "POST",
            "/faces/move",
            {"operationId": ops["move-to-avery"], "faceIds": [f83b], "targetPersonId": avery},
        ),
        _phase4_json_case(
            "POST", "/faces/move", {"operationId": ops["move-new"], "faceIds": [f84g]}
        ),
        _phase4_json_case(
            "POST", "/faces/move", {"operationId": ops["move-new"], "faceIds": [f84g]}
        ),
        _phase4_json_case(
            "POST",
            "/faces/move",
            {"operationId": ops["move-stale"], "faceIds": [f84s], "targetPersonId": avery},
        ),
        _phase4_json_case(
            "POST",
            "/faces/move",
            {"operationId": ops["move-unknown-face"], "faceIds": [str(UNKNOWN_FACE_ID)], "targetPersonId": avery},
        ),
        _phase4_json_case(
            "POST",
            "/faces/move",
            {"operationId": ops["move-tgt-missing"], "faceIds": [f82a], "targetPersonId": str(UNKNOWN_PERSON_ID)},
        ),
        _phase4_json_case(
            "POST",
            "/faces/move",
            {"operationId": ops["move-dup"], "faceIds": [f82a, f82a], "targetPersonId": avery},
        ),
        _phase4_json_case(
            "POST", "/faces/move", {"operationId": ops["move-empty"], "faceIds": []}
        ),
        # The face-thumbnail contract: a pending preview yields the
        # 202 + Retry-After body, an unavailable preview the 404 branch,
        # and unknown and stale faces the face-not-found 404.
        ("GET", f"/faces/{f81a}/thumbnail"),
        ("GET", f"/faces/{f84g}/thumbnail"),
        ("GET", f"/faces/{UNKNOWN_FACE_ID}/thumbnail"),
        ("GET", f"/faces/{f84s}/thumbnail"),
        # The storage-verify report over the four seeded blobs: the
        # default head-only pass, then the full sha256 pass (the
        # disposable test bucket only — never the configured library).
        ("POST", "/maintenance/verify"),
        ("POST", "/maintenance/verify?full=true"),
    ]


def test_phase_4(backend):
    """Phase 4: the face-review operations and the storage-verify report
    become schema-complete (``PersonRenameOut`` / ``PersonMergeOut`` /
    ``FaceMoveOut`` / ``VerifyOut``), and the binary face-thumbnail
    endpoint is documented with its media type and 202 + Retry-After
    contract.

    Golden: avery is renamed (idempotent replay, operation-id reuse 409,
    unknown-person 404); sam is merged into avery after a same-person 409
    (replay, missing-source 404, missing-target 404); ben's face moves to
    avery (replay), the ghost face moves without a target and creates a
    new person with a pinned id (replay), then stale-face 409,
    unknown-face 409, missing-target 404, duplicate-ids 422, and
    empty-list 422; the face thumbnail serves the 202 pending body for a
    missing preview, the unavailable-preview 404, and face-not-found 404s
    for unknown and stale faces; POST /maintenance/verify reports
    size-only and full sha256 passes over the four seeded blobs with no
    errors. The catalog module's uuid4 is pinned to this section's new
    person id for the session; no workers are started.
    """
    seeded = seed_phase_4(backend.service)

    def pinned_uuid4() -> UUID:
        return PHASE4_PERSONS["new"]

    with mock.patch("photo_server.catalog.uuid4", new=pinned_uuid4):
        run_sequence(
            backend,
            "phase4",
            phase_4_cases(seeded),
            describe=(
                "phase4: face-review operations and the storage-verify "
                "report on four pinned assets with five people and six "
                "faces (five current-run, one non-current-run): avery is "
                "renamed (idempotent replay, operation-id reuse 409, "
                "unknown-person 404); after a same-person 409, sam is "
                "merged into avery (one face moves; replay, "
                "missing-source 404, missing-target 404); ben's face "
                "moves to avery (replay), the ghost face moves without a "
                "target and creates a new person with a pinned id "
                "(replay), then stale-face 409, unknown-face 409, "
                "missing-target 404, duplicate-ids 422, empty-list 422; "
                "the face thumbnail serves the 202 pending body (Retry-"
                "After 2) for a missing preview, the unavailable-preview "
                "404, and face-not-found 404s for unknown and stale "
                "faces; POST /maintenance/verify reports the size-only "
                "and full sha256 passes over the four seeded blobs "
                "(assetsChecked 4, blobsChecked 4, no errors) against "
                "the disposable test bucket only; catalog uuid4 pinned "
                "for the session; no workers started"
            ),
        )

# ---------------------------------------------------------------------------
# Phase 3B's additive /health wire contract gets its own append-only golden.
# The seed and Phase 2 fixtures remain immutable historical contracts.


def test_phase_3b_health_golden(backend):
    """Record the four new service-visibility fields separately."""
    run_sequence(
        backend,
        "phase3b_health",
        [("GET", "/health")],
        describe=(
            "phase3b: GET /health with the four AI service-visibility flags; "
            "both AI URLs are unset, so all four flags are false"
        ),
    )


# ---------------------------------------------------------------------------
# AI service split plan Phase 3B (docs/ai-service-split-plan.md): /health
# service visibility.
#
# The two AI services are dummy fixtures for this section: 127.0.0.1 stubs
# serving only the JSON contracts the /health probes consume (the VLM's
# /models, the face-service's identity-checked /health). No AI service is
# started, no model is loaded, no GPU is touched — the point is the probe
# behavior, and every configured x reachable combination for both services
# is covered, including the ones that cannot come from a live service (a
# drifted embedding identity, a dead port).
# ---------------------------------------------------------------------------


class _VlmStub:
    """Dummy fixture for an OpenAI-compatible VLM endpoint: serves only
    ``GET /v1/models`` (the Phase 3B /health probe target). ``status``
    controls the response; ``bearer``, when set, is the expected
    ``Authorization`` header (a mismatch answers 401). Records every
    request as ``(method, path, authorization)``."""

    def __init__(self, status: int = 200, bearer: str | None = None):
        self.requests: list[tuple[str, str, str | None]] = []
        self.status = status
        self.bearer = bearer
        self._stopped = False
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                stub.requests.append(("GET", self.path, self.headers.get("Authorization")))
                if self.path != "/v1/models":
                    self.send_response(404)
                    self.end_headers()
                    return
                if stub.bearer and self.headers.get("Authorization") != stub.bearer:
                    self.send_response(401)
                    self.end_headers()
                    return
                body = json.dumps({"object": "list", "data": [{"id": "stub-vlm"}]}).encode()
                self.send_response(stub.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/v1"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def stop(self):
        if self._stopped:
            return
        self._stopped = True
        self._server.shutdown()
        self._server.server_close()


class _FaceStub:
    """Dummy fixture for the face-service's ``GET /health`` contract (the
    Phase 3B /health probe target): reports the verified embedding identity
    (``ADAFACE_IDENTITY``) or a caller-supplied drifted one. No model is
    loaded; only the JSON shape the real service serves is reproduced.
    ``status`` controls the response; ``bearer``, when set, is the expected
    ``Authorization`` header (a mismatch answers 401)."""

    def __init__(self, status: int = 200, bearer: str | None = None, identity: dict | None = None):
        self.requests: list[tuple[str, str, str | None]] = []
        self.status = status
        self.bearer = bearer
        self._stopped = False
        identity = dict(identity or ADAFACE_IDENTITY)
        self.payload = {
            "status": "ok",
            "models": {
                "faceDetector": "yunet-2023mar",
                "faceEmbedding": {
                    "name": identity["name"],
                    "revision": identity["revision"],
                    "weightsSha256": identity["weights_sha256"],
                    "runtime": "stub-onnxruntime",
                },
            },
            "detectionThreshold": 0.8,
            "concurrency": 1,
            "inFlight": 0,
            "queueDepth": 0,
        }
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                stub.requests.append(("GET", self.path, self.headers.get("Authorization")))
                if self.path != "/health":
                    self.send_response(404)
                    self.end_headers()
                    return
                if stub.bearer and self.headers.get("Authorization") != stub.bearer:
                    self.send_response(401)
                    self.end_headers()
                    return
                body = json.dumps(stub.payload).encode()
                self.send_response(stub.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def stop(self):
        if self._stopped:
            return
        self._stopped = True
        self._server.shutdown()
        self._server.server_close()


def _health_flags(client: TestClient) -> tuple[bool, bool, bool, bool]:
    """GET /health and return the four service-visibility flags. A probe
    failure must never fail the endpoint: 200 or the test fails."""
    response = client.get("/health")
    assert response.status_code == 200, (
        f"/health must answer 200 even when a service probe fails "
        f"(got {response.status_code}: {response.text[:200]})"
    )
    body = response.json()
    return (
        body["aiSemanticConfigured"],
        body["aiSemanticReachable"],
        body["aiFaceConfigured"],
        body["aiFaceReachable"],
    )


def test_health_ai_service_visibility(backend):
    """Every configured x reachable combination for both services. Fresh
    app per case (a fresh probe cache), so the first /health always probes:
    - not configured (empty URL): configured and reachable both false, and
      no probe traffic reaches any stub;
    - configured + healthy: both true; the probes hit GET /models and
      GET /health with the bearer credentials when configured;
    - configured but unreachable: reachable false (HTTP 503, 401, and — for
      the face service — a drifted embedding identity, which the reused
      identity check turns into unreachable: one embedding space, global
      invariant);
    - mixed: one service configured, the other not;
    - dead ports last (connection refused), once the stubs have died."""
    vlm = _VlmStub()
    face = _FaceStub()
    drifted = _FaceStub(identity={"name": "adaface-ir101", "revision": "0" * 40,
                                  "weights_sha256": "0" * 64, "preprocessing": "x"})
    try:
        base = backend.service.settings  # the fixture pins both URLs empty

        def with_urls(**updates):
            return TestClient(create_app(base.model_copy(update=updates)))

        # 1. Not configured: all four false, no probe traffic.
        with with_urls() as client:
            assert _health_flags(client) == (False, False, False, False)
        assert vlm.requests == [] and face.requests == [] and drifted.requests == []

        # 2. Both configured and healthy: all four true, probes with auth.
        with with_urls(
            ai_base_url=vlm.url,
            face_service_url=face.url,
            ai_api_key="vlm-key",
            face_service_token="face-token",
        ) as client:
            assert _health_flags(client) == (True, True, True, True)
        assert vlm.requests == [("GET", "/v1/models", "Bearer vlm-key")]
        assert face.requests == [("GET", "/health", "Bearer face-token")]

        # 3. VLM unreachable while its stub is still up: an HTTP 503 and a
        #    rejected key (401) both classify as unreachable.
        vlm.status = 503
        with with_urls(ai_base_url=vlm.url, face_service_url=face.url) as client:
            assert _health_flags(client) == (True, False, True, True)
        vlm.status = 302
        with with_urls(ai_base_url=vlm.url, face_service_url=face.url) as client:
            assert _health_flags(client) == (True, False, True, True)
        vlm.status = 200
        vlm.bearer = "expected-key"
        with with_urls(
            ai_base_url=vlm.url, face_service_url=face.url, ai_api_key="wrong-key"
        ) as client:
            assert _health_flags(client) == (True, False, True, True)
        vlm.bearer = None

        # 4. Face service unreachable, three ways while the stubs are up:
        #    an HTTP 503, a rejected token (401), and a drifted embedding
        #    identity (200 + "ok", but the identity check fails:
        #    unreachable — never a silently different embedding space).
        face.status = 503
        with with_urls(ai_base_url=vlm.url, face_service_url=face.url) as client:
            assert _health_flags(client) == (True, True, True, False)
        face.status = 302
        with with_urls(ai_base_url=vlm.url, face_service_url=face.url) as client:
            assert _health_flags(client) == (True, True, True, False)
        face.status = 200
        face.bearer = "expected-token"
        with with_urls(
            ai_base_url=vlm.url, face_service_url=face.url, face_service_token="wrong-token"
        ) as client:
            assert _health_flags(client) == (True, True, True, False)
        face.bearer = None
        with with_urls(ai_base_url=vlm.url, face_service_url=drifted.url) as client:
            assert _health_flags(client) == (True, True, True, False)
        assert any(request[1] == "/health" for request in drifted.requests)

        # 5. Mixed: exactly one service configured, the other not.
        with with_urls(face_service_url=face.url) as client:
            assert _health_flags(client) == (False, False, True, True)
        with with_urls(ai_base_url=vlm.url) as client:
            assert _health_flags(client) == (True, True, False, False)

        # 6. Dead ports last (the stubs die for good): a refused connection
        #    is unreachable, and the endpoint still answers 200.
        vlm.stop()
        face.stop()
        with with_urls(ai_base_url=f"http://127.0.0.1:{vlm.port}/v1") as client:
            assert _health_flags(client) == (True, False, False, False)
        with with_urls(face_service_url=f"http://127.0.0.1:{face.port}") as client:
            assert _health_flags(client) == (False, False, True, False)
    finally:
        vlm.stop()
        face.stop()
        drifted.stop()


def test_health_ai_probes_cached_for_30_seconds(backend):
    """Probe results are cached 30 s per process (Q4): a service that
    starts failing mid-window keeps its last (reachable) result until the
    window expires — the stubs see exactly one probe per window — and when
    the window does expire a fresh probe runs and the flags flip to
    unreachable. A failed probe never fails the endpoint."""
    vlm = _VlmStub()
    face = _FaceStub()
    try:
        settings = backend.service.settings.model_copy(
            update={"ai_base_url": vlm.url, "face_service_url": face.url}
        )
        app = create_app(settings)
        with TestClient(app) as client:
            # First window: one probe per service, both reachable.
            assert _health_flags(client) == (True, True, True, True)
            assert len(vlm.requests) == 1 and len(face.requests) == 1

            # Both services start failing mid-window (still up, now 503):
            # the cached result shields /health — no re-probe happens.
            vlm.status = 503
            face.status = 503
            assert _health_flags(client) == (True, True, True, True)
            assert len(vlm.requests) == 1 and len(face.requests) == 1

            # Expire the window (the test/ops seam on app.state): the fresh
            # probes hit the failing services and the flags flip.
            app.state.ai_probe_cache["ts"] -= api_module._AI_PROBE_CACHE_SECONDS + 1
            assert _health_flags(client) == (True, False, True, False)
            assert len(vlm.requests) == 2 and len(face.requests) == 2

            # Now the services are fully dead (connection refused): a
            # re-probe of dead ports keeps the flags false, and /health
            # still answers 200.
            vlm.stop()
            face.stop()
            app.state.ai_probe_cache["ts"] -= api_module._AI_PROBE_CACHE_SECONDS + 1
            assert _health_flags(client) == (True, False, True, False)
    finally:
        vlm.stop()
        face.stop()
