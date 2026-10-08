"""Upload onboarding recovery tests."""

import hashlib
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4, uuid5

import pytest

from photo_server.canonical import CanonicalPublisher
from photo_server.config import LibraryError
from photo_server.uploads import _commit_staged


class MemoryStorage:
    bucket = "test"

    def __init__(self):
        self.values = {}

    def head(self, key):
        body = self.values.get(key)
        return None if body is None else {"ContentLength": len(body)}

    def read_bytes(self, key):
        return self.values[key]

    def put(self, key, body, _mime, _metadata=None):
        if key in self.values:
            return False
        self.values[key] = body
        return True

    def verify(self, key, size, sha256, full=True):
        body = self.values[key]
        if len(body) != size or hashlib.sha256(body).hexdigest() != sha256:
            raise AssertionError(f"verification failed for {key}")

    def keys(self, prefix=""):
        return sorted(key for key in self.values if key.startswith(prefix))


class Catalog:
    def __init__(self):
        self.applied = []
        self.fail_apply = True

    @contextmanager
    def writer(self):
        yield

    @contextmanager
    def digest_lock(self, _digest):
        yield

    def get(self, _asset_id):
        return None

    def find_hash(self, _digest):
        return None

    def apply(self, manifest):
        if self.fail_apply:
            self.fail_apply = False
            raise RuntimeError("postgres unavailable")
        self.applied.append(manifest)


class Service:
    library_id = uuid4()

    def __init__(self):
        self.storage = MemoryStorage()
        self.publisher = CanonicalPublisher(self.storage)
        self.catalog = Catalog()


def _job(job_id: UUID, filename: str) -> dict:
    return {"id": str(job_id)}


def test_onboarding_retries_existing_canonical_manifest_after_projection_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service = Service()
    job_id = uuid4()
    job = _job(job_id, "photo.jpg")
    source = tmp_path / "photo.jpg"
    source.write_bytes(b"photo bytes")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    files = [(source, digest, source.stat().st_size)]
    monkeypatch.setattr("photo_server.uploads.extract_metadata", lambda *_: ({}, "image/jpeg"))

    with pytest.raises(RuntimeError, match="postgres unavailable"):
        _commit_staged(service, job, files)

    manifest_key = (
        f"manifests/assets/{uuid5(service.library_id, f'upload:{job_id}')}/1.json"
    )
    first_manifest = service.storage.values[manifest_key]
    assert service.storage.keys("reconciliation/projection-writes/")
    assert not service.catalog.applied

    result = _commit_staged(service, job, files)

    assert result == {
        "status": "imported",
        "assetId": str(uuid5(service.library_id, f"upload:{job_id}")),
        "replayed": True,
    }
    assert service.storage.values[manifest_key] == first_manifest
    assert len(service.catalog.applied) == 1
    assert service.catalog.applied[0].operation_id == job_id


def test_onboarding_recovery_rejects_changed_inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    service = Service()
    job_id = uuid4()
    job = _job(job_id, "photo.jpg")
    source = tmp_path / "photo.jpg"
    source.write_bytes(b"photo bytes")
    first_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    monkeypatch.setattr("photo_server.uploads.extract_metadata", lambda *_: ({}, "image/jpeg"))

    with pytest.raises(RuntimeError):
        _commit_staged(service, job, [(source, first_digest, source.stat().st_size)])

    source.write_bytes(b"changed bytes")
    changed_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    with pytest.raises(LibraryError, match="changed file content"):
        _commit_staged(service, job, [(source, changed_digest, source.stat().st_size)])
