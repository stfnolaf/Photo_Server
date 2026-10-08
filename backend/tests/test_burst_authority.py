from contextlib import contextmanager
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from photo_server.burst_authority import (
    CURRENT_BURST_KEY,
    BurstAuthority,
    load_burst_state,
)
from photo_server.canonical import CanonicalPublisher
from photo_server.config import LibraryError
from photo_server.fingerprints import Fingerprint


class MemoryStorage:
    def __init__(self):
        self.objects = {}

    def keys(self, prefix):
        return sorted(key for key in self.objects if key.startswith(prefix))

    def head(self, key):
        body = self.objects.get(key)
        return None if body is None else {"ContentLength": len(body)}

    def put(self, key, body, _mime):
        self.objects[key] = body
        return True

    def replace(self, key, body, _mime):
        self.objects[key] = body

    def read_bytes(self, key):
        return self.objects[key]

    def verify(self, key, size, digest, full=False):
        import hashlib

        body = self.objects[key]
        if len(body) != size or hashlib.sha256(body).hexdigest() != digest:
            raise LibraryError("verification failed")


class Projection:
    def __init__(self):
        self.fail_once = True
        self.applied = []

    @contextmanager
    def writer(self):
        yield

    def apply_burst_projection(self, fingerprints, snapshot):
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("projection unavailable")
        self.applied.append((fingerprints, snapshot))


def _asset(asset_id, filename):
    return SimpleNamespace(
        asset_id=UUID(asset_id),
        deleted_at=None,
        capture_time="2026-01-01T12:00:00Z",
        imported_at="2026-01-01T12:01:00Z",
        metadata={},
        primary=SimpleNamespace(original_filename=filename),
    )


def _service():
    library_id = uuid4()
    first = str(uuid4())
    second = str(uuid4())
    third = str(uuid4())
    storage = MemoryStorage()
    catalog = Projection()
    assets = {
        first: _asset(first, "IMG_0001.jpg"),
        second: _asset(second, "IMG_0002.jpg"),
        third: _asset(third, "IMG_0003.jpg"),
    }
    service = SimpleNamespace(
        library_id=library_id,
        storage=storage,
        catalog=catalog,
        settings=SimpleNamespace(
            burst_cluster_phash_max_distance=17,
            burst_cluster_dhash_max_distance=15,
            burst_cluster_capture_window_seconds=35,
            burst_cluster_chroma_max_distance=0.15,
        ),
        canonical_asset=lambda asset_id: assets[str(asset_id)],
    )
    service.publisher = CanonicalPublisher(storage)
    return service, first, second, third


def _fingerprint(phash="0000000000000000"):
    return Fingerprint(
        phash=phash,
        dhash="0000000000000000",
        width=1200,
        height=800,
        chroma_histogram="00" * 12,
    )


def test_projection_failure_retries_from_immutable_s3_without_new_revision():
    service, first, _second, _third = _service()
    authority = BurstAuthority(service)

    with pytest.raises(RuntimeError, match="projection unavailable"):
        authority.publish_fingerprint(first, _fingerprint())

    first_state = load_burst_state(service.storage)
    assert first_state is not None and first_state.revision == 1
    authority.publish_fingerprint(first, _fingerprint())
    assert load_burst_state(service.storage) == first_state
    assert [key for key in service.storage.objects if key == CURRENT_BURST_KEY] == [
        CURRENT_BURST_KEY
    ]
    assert len(service.catalog.applied) == 1

    with pytest.raises(LibraryError, match="conflicts"):
        authority.publish_fingerprint(first, _fingerprint("ffffffffffffffff"))


def test_same_fingerprint_can_publish_new_cluster_state_after_metadata_change():
    service, first, second, _third = _service()
    service.catalog.fail_once = False
    authority = BurstAuthority(service)
    fingerprint = _fingerprint()
    authority.publish_fingerprint(first, fingerprint)
    authority.publish_fingerprint(second, fingerprint)
    before = load_burst_state(service.storage)
    assert before is not None and before.revision == 2
    assert len(before.clusters) == 1

    service.canonical_asset(second).capture_time = "2026-01-01T13:00:00Z"
    service.catalog.fail_once = True
    with pytest.raises(RuntimeError, match="projection unavailable"):
        authority.publish_fingerprint(second, fingerprint)

    changed = load_burst_state(service.storage)
    assert changed is not None and changed.revision == 3
    assert changed.operation_id != before.operation_id
    assert len(changed.clusters) == 2

    authority.publish_fingerprint(second, fingerprint)
    retried = load_burst_state(service.storage)
    assert retried == changed
    assert service.catalog.applied[-1][1] == changed


def test_representative_override_is_a_new_canonical_revision_and_idempotent():
    service, first, second, third = _service()
    service.catalog.fail_once = False
    authority = BurstAuthority(service)
    authority.publish_fingerprint(first, _fingerprint())
    authority.publish_fingerprint(second, _fingerprint())
    head = load_burst_state(service.storage)
    assert head is not None
    cluster = next(cluster for cluster in head.clusters if len(cluster.asset_ids) == 2)
    operation_id = uuid4()

    result = authority.mutate(
        operation_id,
        "setRepresentative",
        str(cluster.cluster_id),
        second,
    )
    assert result["representativeAssetId"] == second
    changed = load_burst_state(service.storage)
    assert changed is not None
    assert changed.clusters[0].representative_asset_id == UUID(second)
    assert changed.clusters[0].representative_selected is True
    representative_revision = changed.revision

    with pytest.raises(LibraryError, match="different request"):
        authority.mutate(operation_id, "setRepresentative", str(cluster.cluster_id), first)

    authority.publish_fingerprint(third, _fingerprint())
    after_growth = load_burst_state(service.storage)
    assert after_growth is not None
    assert after_growth.revision > representative_revision
    assert after_growth.clusters[0].representative_asset_id == UUID(second)
    assert after_growth.clusters[0].representative_selected is True

    authority.mutate(operation_id, "setRepresentative", str(cluster.cluster_id), second)
    assert load_burst_state(service.storage).revision == after_growth.revision
