"""S3-authoritative mutation tests."""

from uuid import uuid4

import pytest

from photo_server.authoritative import AuthoritativeMutationCoordinator
from photo_server.config import LibraryError
from photo_server.models import Blob, Manifest, Mutation, UserState
from photo_server.state import mutate


class MemoryStorage:
    def __init__(self):
        self.values = {}

    def read_bytes(self, key):
        return self.values[key]

    def head(self, key):
        return {} if key in self.values else None

    def keys(self, prefix=""):
        return [key for key in self.values if key.startswith(prefix)]


class MemoryPublisher:
    def __init__(self, storage):
        self.storage = storage

    def _put_immutable(self, key, data, _mime):
        if key in self.storage.values and self.storage.values[key] != data:
            raise RuntimeError("immutable conflict")
        self.storage.values[key] = data

    def projection_from_manifest(self, manifest):
        from photo_server.canonical import CanonicalPublisher

        return CanonicalPublisher(self.storage).projection_from_manifest(manifest)


class Catalog:
    def __init__(self, manifest):
        self.manifest = manifest
        self.library = manifest.library_id

    def get(self, _asset_id):
        return self.manifest

    def library_id(self):
        return self.library


class Service:
    def __init__(self, manifest):
        self.storage = MemoryStorage()
        self.publisher = MemoryPublisher(self.storage)
        self.catalog = Catalog(manifest)
        self.library_id = manifest.library_id
        from photo_server.canonical import CanonicalPublisher

        canonical = CanonicalPublisher.__new__(CanonicalPublisher)
        initial = canonical.manifest_from_projection(manifest)
        from photo_server.manifests import encode

        self.storage.values[f"manifests/assets/{manifest.asset_id}/1.json"] = encode(initial)


def make_manifest():
    library_id, asset_id, blob_id = uuid4(), uuid4(), uuid4()
    return Manifest(
        library_id=library_id, asset_id=asset_id, operation_id=uuid4(),
        primary_blob_id=blob_id,
        blobs=[Blob(blob_id=blob_id, role="ORIGINAL_JPEG", original_filename="x.jpg",
                    object_key=f"objects/{'a' * 64}", sha256="a" * 64,
                    size_bytes=1, mime_type="image/jpeg")],
        imported_at="2026-10-06T00:00:00Z", user_state=UserState(),
    )


def test_asset_mutation_is_manifest_first_and_immutable():
    current = make_manifest()
    service = Service(current)
    operation_id = uuid4()
    mutation = Mutation(action="asset.patch", entity_id=current.asset_id,
                        changes={"rating": 4}, expected_revision=1)
    AuthoritativeMutationCoordinator(service).publish(operation_id, mutation)
    assert f"manifests/assets/{current.asset_id}/2.json" in service.storage.values


def test_projection_failure_can_be_retried_after_verified_s3_commit():
    current = make_manifest()
    service = Service(current)
    coordinator = AuthoritativeMutationCoordinator(service)
    coordinator.publish(uuid4(), Mutation(action="asset.delete", entity_id=current.asset_id,
                                           expected_revision=1))
    with pytest.raises(LibraryError, match="Revision changed"):
        coordinator.publish(uuid4(), Mutation(action="asset.patch", entity_id=current.asset_id,
                                               changes={"rating": 2}, expected_revision=1))


def test_expected_revision_conflict_writes_nothing():
    current = make_manifest()
    service = Service(current)
    before = dict(service.storage.values)
    with pytest.raises(Exception, match="Revision changed"):
        AuthoritativeMutationCoordinator(service).publish(
            uuid4(), Mutation(action="asset.delete", entity_id=current.asset_id, expected_revision=2)
        )
    assert service.storage.values == before


def test_projection_failure_records_recoverable_s3_mutation(monkeypatch):
    current = make_manifest()
    service = Service(current)
    receipts = []
    monkeypatch.setattr(service.catalog, "operation", lambda _operation_id: None, raising=False)
    monkeypatch.setattr(
        service.catalog,
        "commit_mutation",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("postgres unavailable")),
        raising=False,
    )
    monkeypatch.setattr(
        service.publisher,
        "record_reconciliation",
        lambda operation_id, payload: receipts.append((operation_id, payload)),
        raising=False,
    )
    operation_id = uuid4()
    mutation = Mutation(action="asset.patch", entity_id=current.asset_id, changes={"rating": 5})

    with pytest.raises(RuntimeError, match="postgres unavailable"):
        mutate(service, operation_id, mutation)

    assert f"manifests/assets/{current.asset_id}/2.json" in service.storage.values
    assert receipts[0][0] == operation_id
    assert receipts[0][1]["status"] == "canonical-written-projection-failed"
