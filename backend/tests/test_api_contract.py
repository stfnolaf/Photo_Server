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
from pathlib import Path
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from sqlalchemy.engine import make_url

from photo_server.api import create_app
from photo_server.config import Settings
from photo_server.models import Blob, Manifest
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
    manifest = Manifest(
        library_id=service.library_id,
        asset_id=asset_id,
        operation_id=operation_id,
        primary_blob_id=blob.blob_id,
        blobs=[blob],
        imported_at=imported,
        capture_time=capture,
        metadata={"Make": camera, "Model": "Camera", "LensModel": "35mm Prime"},
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
