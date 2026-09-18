"""Fingerprint persistence and candidate-query tests against a live catalog."""

from uuid import uuid4

from sqlalchemy import text
from test_integration import backend as backend  # noqa: F401
from test_integration import pytestmark  # noqa: F401

from photo_server.fingerprints import BURST_HASH_VERSION, Fingerprint
from photo_server.models import Blob, Manifest

WIDTH, HEIGHT = 640, 480
PHASH = "0123456789abcdef"
DHASH = "fedcba9876543210"


def fp(width=WIDTH, height=HEIGHT, version=BURST_HASH_VERSION, phash=PHASH, dhash=DHASH):
    return Fingerprint(
        algorithm_version=version, phash=phash, dhash=dhash, width=width, height=height
    )


def add_asset(backend, capture_time=None, metadata=None):
    asset_id = uuid4()
    blob_id = uuid4()
    blob = Blob(
        blob_id=blob_id,
        role="ORIGINAL_JPEG",
        original_filename="sample.JPG",
        object_key=f"originals/{asset_id}/sample.JPG",
        sha256=uuid4().hex + uuid4().hex,
        size_bytes=100,
        mime_type="image/jpeg",
    )
    manifest = Manifest(
        library_id=backend.service.library_id,
        asset_id=asset_id,
        operation_id=uuid4(),
        primary_blob_id=blob_id,
        blobs=[blob],
        imported_at="2026-01-01T00:00:00+00:00",
        capture_time=capture_time,
        metadata=metadata or {},
    )
    backend.service.catalog.apply(manifest)
    return str(asset_id)


def test_migration_zero_seven_creates_fingerprint_schema(backend):
    catalog = backend.service.catalog
    with catalog.engine.connect() as connection:
        assert (
            connection.scalar(text("SELECT to_regclass('public.image_fingerprints')")) is not None
        )
        columns = {
            row["column_name"]
            for row in connection.execute(
                text("SELECT column_name FROM information_schema.columns WHERE table_name = 'analysis_runs'")
            ).mappings()
        }
    assert {"semantic_origin", "source_run_id", "reuse_policy_version", "similarity"} <= columns


def test_fingerprint_upsert_is_idempotent(backend):
    catalog = backend.service.catalog
    asset_id = add_asset(backend, capture_time="2026-01-01T12:00:00+00:00")
    catalog.upsert_fingerprint(asset_id, fp())
    catalog.upsert_fingerprint(asset_id, fp())
    with catalog.engine.connect() as connection:
        count = connection.scalar(
            text(
                "SELECT count(*) FROM image_fingerprints "
                "WHERE asset_id = :a AND algorithm_version = :v"
            ),
            {"a": asset_id, "v": BURST_HASH_VERSION},
        )
    assert count == 1


def test_get_fingerprint_roundtrip_and_version(backend):
    catalog = backend.service.catalog
    asset_id = add_asset(backend, capture_time="2026-01-01T12:00:00+00:00")
    value = fp()
    catalog.upsert_fingerprint(asset_id, value)
    assert catalog.get_fingerprint(asset_id, BURST_HASH_VERSION) == value
    assert catalog.get_fingerprint(asset_id, "burst-hash-v0") is None
    assert catalog.get_fingerprint(str(uuid4()), BURST_HASH_VERSION) is None


def test_candidate_query_filters_on_version_time_camera_and_dimensions(backend):
    catalog = backend.service.catalog
    camera = {"Make": "Canon", "Model": "EOS R5"}
    target = add_asset(backend, capture_time="2026-01-01T12:00:00+00:00", metadata=camera)
    catalog.upsert_fingerprint(target, fp())

    in_window = add_asset(backend, capture_time="2026-01-01T12:00:01+00:00", metadata=camera)
    catalog.upsert_fingerprint(in_window, fp())

    out_of_window = add_asset(backend, capture_time="2026-01-01T12:00:10+00:00", metadata=camera)
    catalog.upsert_fingerprint(out_of_window, fp())

    different_model = add_asset(
        backend,
        capture_time="2026-01-01T12:00:01+00:00",
        metadata={"Make": "Canon", "Model": "X-T4"},
    )
    catalog.upsert_fingerprint(different_model, fp())

    different_dimensions = add_asset(backend, capture_time="2026-01-01T12:00:01+00:00", metadata=camera)
    catalog.upsert_fingerprint(different_dimensions, fp(width=800, height=600))

    different_version = add_asset(backend, capture_time="2026-01-01T12:00:01+00:00", metadata=camera)
    catalog.upsert_fingerprint(different_version, fp(version="burst-hash-v0"))

    candidates = catalog.find_fingerprint_candidates(target, BURST_HASH_VERSION)
    assert [candidate.asset_id for candidate in candidates] == [in_window]
    assert candidates[0].phash == PHASH
    assert candidates[0].dhash == DHASH
    assert candidates[0].capture_time is not None


def test_target_without_capture_time_yields_no_candidates(backend):
    catalog = backend.service.catalog
    camera = {"Make": "Canon", "Model": "EOS R5"}
    target = add_asset(backend, capture_time=None, metadata=camera)
    catalog.upsert_fingerprint(target, fp())
    other = add_asset(backend, capture_time="2026-01-01T12:00:00+00:00", metadata=camera)
    catalog.upsert_fingerprint(other, fp())
    assert catalog.find_fingerprint_candidates(target, BURST_HASH_VERSION) == []


def test_camera_gate_is_vacuous_when_target_lacks_camera_info(backend):
    catalog = backend.service.catalog
    target = add_asset(backend, capture_time="2026-01-01T12:00:00+00:00", metadata={})
    catalog.upsert_fingerprint(target, fp())
    other = add_asset(
        backend,
        capture_time="2026-01-01T12:00:01+00:00",
        metadata={"Make": "Fujifilm", "Model": "X-T4"},
    )
    catalog.upsert_fingerprint(other, fp())
    candidates = catalog.find_fingerprint_candidates(target, BURST_HASH_VERSION)
    assert [candidate.asset_id for candidate in candidates] == [other]
