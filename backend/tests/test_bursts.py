"""Burst clustering and best-shot tests against a live catalog."""

import inspect
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from test_integration import backend as backend  # noqa: F401
from test_integration import pytestmark  # noqa: F401

from photo_server.browsing import BrowseQuery
from photo_server.bursts import BURST_CLUSTER_POLICY_VERSION
from photo_server.catalog import Catalog, image_fingerprints
from photo_server.config import LibraryError
from photo_server.fingerprints import BURST_HASH_VERSION, Fingerprint, hamming_distance
from photo_server.models import Blob, Manifest, Mutation
from photo_server.state import mutate

WIDTH, HEIGHT = 640, 480
PHASH = "0123456789abcdef"
DHASH = "fedcba9876543210"
CAMERA = {"Make": "Canon", "Model": "EOS R5"}


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


def browse_items(backend):
    return backend.service.catalog.browse(BrowseQuery())["items"]


def get_item(backend, asset_id):
    return next(item for item in browse_items(backend) if item["assetId"] == asset_id)


def cluster_id_of(backend, asset_id):
    detail = backend.service.catalog.burst_detail(asset_id)
    return detail["burstId"] if detail else None


def set_rep(backend, cluster_id, asset_id, operation_id=None):
    return mutate(
        backend.service,
        operation_id or uuid4(),
        Mutation(
            action="burst.setRepresentative",
            entity_id=UUID(cluster_id),
            changes={"representativeAssetId": asset_id},
        ),
    )


def delete_asset(backend, asset_id):
    mutate(
        backend.service,
        uuid4(),
        Mutation(action="asset.delete", entity_id=UUID(asset_id), changes={}),
    )


def restore_asset(backend, asset_id):
    mutate(
        backend.service,
        uuid4(),
        Mutation(action="asset.restore", entity_id=UUID(asset_id), changes={}),
    )


def make_pair(backend):
    """Two near-identical frames that share a burst cluster."""
    catalog = backend.service.catalog
    a = add_asset(backend, capture_time="2026-01-01T12:00:00+00:00", metadata=CAMERA)
    b = add_asset(backend, capture_time="2026-01-01T12:00:01+00:00", metadata=CAMERA)
    catalog.upsert_fingerprint(a, fp())
    catalog.upsert_fingerprint(b, fp())
    return a, b


# --- Cluster creation and membership -----------------------------------------


def test_first_frame_creates_cluster_and_is_representative(backend):
    catalog = backend.service.catalog
    asset_id = add_asset(backend, capture_time="2026-01-01T12:00:00+00:00")
    catalog.upsert_fingerprint(asset_id, fp())
    detail = catalog.burst_detail(asset_id)
    assert detail is not None
    assert detail["representativeAssetId"] == asset_id
    assert detail["burstId"]
    assert len(detail["frames"]) == 1
    with catalog.engine.connect() as conn:
        row = conn.execute(
            text("SELECT policy_version FROM burst_clusters WHERE id = :id"),
            {"id": detail["burstId"]},
        ).mappings().one()
    assert row["policy_version"] == BURST_CLUSTER_POLICY_VERSION


def test_near_identical_frames_share_cluster(backend):
    catalog = backend.service.catalog
    a, b = make_pair(backend)
    assert cluster_id_of(backend, a) == cluster_id_of(backend, b)


def test_existing_cluster_reconciles_with_later_compatible_cluster(backend):
    """A frame that already has a cluster can still absorb a compatible one."""
    catalog = backend.service.catalog
    a = add_asset(backend, capture_time="2026-01-01T12:00:00+00:00", metadata=CAMERA)
    b = add_asset(backend, capture_time="2026-01-01T12:00:01+00:00", metadata=CAMERA)
    catalog.upsert_fingerprint(a, fp(phash="0000000000000000", dhash="0000000000000000"))
    catalog.upsert_fingerprint(b, fp(phash="ffffffffffffffff", dhash="ffffffffffffffff"))
    first_cluster = cluster_id_of(backend, a)
    second_cluster = cluster_id_of(backend, b)
    assert first_cluster != second_cluster

    # Simulate a later fingerprint recalculation making b compatible with a.
    with catalog.engine.begin() as connection:
        connection.execute(
            image_fingerprints.update()
            .where(image_fingerprints.c.asset_id == b)
            .values(phash="0000000000000001", dhash="0000000000000001")
        )
    catalog.upsert_fingerprint(b, fp(phash="0000000000000001", dhash="0000000000000001"))
    assert cluster_id_of(backend, b) == second_cluster
    assert cluster_id_of(backend, a) == second_cluster
    detail = catalog.burst_detail(a)
    assert detail is not None
    assert {frame["assetId"] for frame in detail["frames"]} == {a, b}
    detail = catalog.burst_detail(a)
    assert len(detail["frames"]) == 2
    item_a = get_item(backend, a)
    item_b = get_item(backend, b)
    assert item_a["burstId"] == item_b["burstId"]
    assert item_a["burstSize"] == 2
    assert item_b["burstSize"] == 2


def test_upsert_fingerprint_is_idempotent_for_membership(backend):
    catalog = backend.service.catalog
    a, _ = make_pair(backend)
    cluster_id = cluster_id_of(backend, a)
    catalog.upsert_fingerprint(a, fp())
    assert cluster_id_of(backend, a) == cluster_id
    with catalog.engine.connect() as conn:
        count = conn.scalar(
            text("SELECT count(*) FROM burst_members WHERE asset_id = :a"),
            {"a": a},
        )
    assert count == 1


# --- Representative selection ------------------------------------------------


def test_set_representative_persists_and_survives_reload(backend):
    a, b = make_pair(backend)
    cluster_id = cluster_id_of(backend, a)
    result = set_rep(backend, cluster_id, b)
    assert result["representativeAssetId"] == b
    assert result["burstId"] == cluster_id
    reloaded = Catalog(backend.service.settings.database_url)
    assert reloaded.burst_detail(a)["representativeAssetId"] == b


def test_set_representative_is_reversible(backend):
    a, b = make_pair(backend)
    cluster_id = cluster_id_of(backend, a)
    set_rep(backend, cluster_id, b)
    assert backend.service.catalog.burst_detail(a)["representativeAssetId"] == b
    set_rep(backend, cluster_id, a)
    assert backend.service.catalog.burst_detail(a)["representativeAssetId"] == a


def test_set_representative_idempotent(backend):
    a, b = make_pair(backend)
    cluster_id = cluster_id_of(backend, a)
    operation = uuid4()
    first = set_rep(backend, cluster_id, b, operation_id=operation)
    second = set_rep(backend, cluster_id, b, operation_id=operation)
    assert first == second


def test_set_representative_rejects_non_member(backend):
    a, b = make_pair(backend)
    c = add_asset(backend, capture_time="2026-01-01T12:00:02+00:00", metadata=CAMERA)
    cluster_id = cluster_id_of(backend, a)
    with pytest.raises(LibraryError, match="not a member"):
        set_rep(backend, cluster_id, c)


def test_set_representative_rejects_unknown_cluster(backend):
    catalog = backend.service.catalog
    a = add_asset(backend, capture_time="2026-01-01T12:00:00+00:00", metadata=CAMERA)
    catalog.upsert_fingerprint(a, fp())
    fake_cluster = str(uuid4())
    with pytest.raises(FileNotFoundError, match="Burst not found"):
        set_rep(backend, fake_cluster, a)


# --- Deletion semantics ------------------------------------------------------


def test_delete_non_representative_leaves_burst_intact(backend):
    catalog = backend.service.catalog
    a, b = make_pair(backend)
    cluster_id = cluster_id_of(backend, a)
    rep = catalog.burst_detail(a)["representativeAssetId"]
    non_rep = b if rep == a else a
    delete_asset(backend, non_rep)
    detail = catalog.burst_detail(rep)
    assert detail is not None
    assert detail["burstId"] == cluster_id
    assert detail["representativeAssetId"] == rep
    assert len(detail["frames"]) == 1


def test_delete_representative_moves_to_survivor(backend):
    catalog = backend.service.catalog
    a, b = make_pair(backend)
    cluster_id = cluster_id_of(backend, a)
    rep = catalog.burst_detail(a)["representativeAssetId"]
    survivor = b if rep == a else a
    delete_asset(backend, rep)
    detail = catalog.burst_detail(survivor)
    assert detail is not None
    assert detail["burstId"] == cluster_id
    assert detail["representativeAssetId"] == survivor
    assert len(detail["frames"]) == 1


def test_delete_last_member_removes_cluster(backend):
    catalog = backend.service.catalog
    a = add_asset(backend, capture_time="2026-01-01T12:00:00+00:00", metadata=CAMERA)
    catalog.upsert_fingerprint(a, fp())
    cluster_id = cluster_id_of(backend, a)
    assert cluster_id is not None
    delete_asset(backend, a)
    assert catalog.burst_detail(a) is None
    with catalog.engine.connect() as conn:
        count = conn.scalar(
            text("SELECT count(*) FROM burst_clusters WHERE id = :id"),
            {"id": cluster_id},
        )
    assert count == 0


def test_restore_rejoins_cluster(backend):
    a, b = make_pair(backend)
    cluster_id = cluster_id_of(backend, a)
    delete_asset(backend, b)
    assert backend.service.catalog.burst_detail(b) is None
    restore_asset(backend, b)
    assert cluster_id_of(backend, b) == cluster_id


# --- Gate failures produce separate clusters ---------------------------------


def test_capture_out_of_window_separate_cluster(backend):
    catalog = backend.service.catalog
    a = add_asset(backend, capture_time="2026-01-01T12:00:00+00:00", metadata=CAMERA)
    b = add_asset(backend, capture_time="2026-01-01T12:00:05+00:00", metadata=CAMERA)
    catalog.upsert_fingerprint(a, fp())
    catalog.upsert_fingerprint(b, fp())
    assert cluster_id_of(backend, a) != cluster_id_of(backend, b)


def test_different_camera_separate_cluster(backend):
    catalog = backend.service.catalog
    a = add_asset(
        backend,
        capture_time="2026-01-01T12:00:00+00:00",
        metadata={"Make": "Canon", "Model": "EOS R5"},
    )
    b = add_asset(
        backend,
        capture_time="2026-01-01T12:00:01+00:00",
        metadata={"Make": "Canon", "Model": "X-T4"},
    )
    catalog.upsert_fingerprint(a, fp())
    catalog.upsert_fingerprint(b, fp())
    assert cluster_id_of(backend, a) != cluster_id_of(backend, b)


def test_different_dimensions_separate_cluster(backend):
    catalog = backend.service.catalog
    a = add_asset(backend, capture_time="2026-01-01T12:00:00+00:00", metadata=CAMERA)
    b = add_asset(backend, capture_time="2026-01-01T12:00:01+00:00", metadata=CAMERA)
    catalog.upsert_fingerprint(a, fp())
    catalog.upsert_fingerprint(b, fp(width=800, height=600))
    assert cluster_id_of(backend, a) != cluster_id_of(backend, b)


def test_hamming_over_threshold_separate_cluster(backend):
    catalog = backend.service.catalog
    a = add_asset(backend, capture_time="2026-01-01T12:00:00+00:00", metadata=CAMERA)
    b = add_asset(backend, capture_time="2026-01-01T12:00:01+00:00", metadata=CAMERA)
    far_phash = "ffffffffffffffff"
    assert hamming_distance(PHASH, far_phash) > 24
    catalog.upsert_fingerprint(a, fp())
    catalog.upsert_fingerprint(b, fp(phash=far_phash))
    assert cluster_id_of(backend, a) != cluster_id_of(backend, b)


# --- Unfingerprinted frames --------------------------------------------------


def test_no_fingerprint_not_clustered(backend):
    catalog = backend.service.catalog
    a = add_asset(backend, capture_time="2026-01-01T12:00:00+00:00", metadata=CAMERA)
    b = add_asset(backend, capture_time="2026-01-01T12:00:01+00:00", metadata=CAMERA)
    catalog.upsert_fingerprint(a, fp())
    assert catalog.burst_detail(b) is None
    assert get_item(backend, b)["burstId"] is None
    assert get_item(backend, b)["burstSize"] is None


# --- Reuse-mode independence -------------------------------------------------


def test_clustering_does_not_change_reuse_candidates(backend):
    catalog = backend.service.catalog
    target = add_asset(backend, capture_time="2026-01-01T12:00:00+00:00", metadata=CAMERA)
    candidate = add_asset(backend, capture_time="2026-01-01T12:00:01+00:00", metadata=CAMERA)
    catalog.upsert_fingerprint(target, fp())
    catalog.upsert_fingerprint(candidate, fp())
    assert cluster_id_of(backend, target) is not None
    candidates = catalog.find_fingerprint_candidates(target, BURST_HASH_VERSION)
    assert [c.asset_id for c in candidates] == [candidate]


def test_clustering_independent_of_reuse_mode(backend):
    """Clustering is always-on and never reads ai_semantic_reuse_mode."""
    from photo_server import bursts

    assert "ai_semantic_reuse_mode" not in inspect.getsource(bursts)
