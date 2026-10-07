from uuid import uuid4

import pytest

from photo_server.authoritative import AuthoritativeMutationCoordinator
from photo_server.config import LibraryError
from photo_server.models import Blob, Manifest, Mutation, UserState


class MemoryStorage:
    def __init__(self):
        self.values = {}

    def read_bytes(self, key):
        return self.values[key]

    def head(self, key):
        return {} if key in self.values else None


class MemoryPublisher:
    def __init__(self, storage):
        self.storage = storage

    def _put_immutable(self, key, data, _mime):
        if key in self.storage.values and self.storage.values[key] != data:
            raise RuntimeError("immutable conflict")
        self.storage.values[key] = data


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
        self.dual_write = MemoryPublisher(self.storage)
        self.catalog = Catalog(manifest)


def make_manifest():
    library_id, asset_id, blob_id = uuid4(), uuid4(), uuid4()
    return Manifest(
        library_id=library_id, asset_id=asset_id, operation_id=uuid4(),
        primary_blob_id=blob_id,
        blobs=[Blob(blob_id=blob_id, role="ORIGINAL_JPEG", original_filename="x.jpg",
                    object_key=f"originals/{asset_id}/x.jpg", sha256="a" * 64,
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
    with pytest.raises(LibraryError, match="Immutable manifest revision"):
        coordinator.publish(uuid4(), Mutation(action="asset.patch", entity_id=current.asset_id,
                                               changes={"rating": 2}, expected_revision=1))


def test_expected_revision_conflict_writes_nothing():
    current = make_manifest()
    service = Service(current)
    with pytest.raises(Exception, match="Revision changed"):
        AuthoritativeMutationCoordinator(service).publish(
            uuid4(), Mutation(action="asset.delete", entity_id=current.asset_id, expected_revision=2)
        )
    assert not service.storage.values
