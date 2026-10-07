import hashlib
from pathlib import Path
from uuid import UUID

import pytest

from photo_server.dual_write import DualWriteIntegrityError, DualWritePublisher
from photo_server.manifests import decode_asset_manifest, encode


class FakeStorage:
    def __init__(self):
        self.objects = {}

    def head(self, key):
        body = self.objects.get(key)
        return None if body is None else {"ContentLength": len(body)}

    def put(self, key, body, _mime):
        if key not in self.objects:
            self.objects[key] = body.read() if hasattr(body, "read") else body
        return key not in self.objects

    def read_bytes(self, key, limit=None):
        value = self.objects[key]
        if limit is not None and len(value) > limit:
            raise AssertionError("limit")
        return value

    def verify(self, key, size, digest, full=True):
        value = self.objects[key]
        if len(value) != size or hashlib.sha256(value).hexdigest() != digest:
            from photo_server.config import LibraryError

            raise LibraryError("verification failed")

    def keys(self, prefix):
        return [key for key in self.objects if key.startswith(prefix)]


def test_content_addressed_put_is_idempotent_and_verifies_existing_bytes(tmp_path: Path):
    data = b"photo-bytes"
    path = tmp_path / "photo.jpg"
    path.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    storage = FakeStorage()
    publisher = DualWritePublisher(storage)

    assert publisher.publish_object(path, digest, len(data)) == f"objects/{digest}"
    assert publisher.publish_object(path, digest, len(data)) == f"objects/{digest}"
    storage.objects[f"objects/{digest}"] = b"wrong"
    with pytest.raises(DualWriteIntegrityError, match="size|checksum"):
        publisher.publish_object(path, digest, len(data))

    with pytest.raises(Exception, match="checksum"):
        publisher.publish_object(path, "0" * 64, len(data))


def test_manifest_is_codec_validated_and_checksum_verified():
    import json

    example = Path(__file__).parents[2] / "docs/in-progress/s3-authoritative-examples/asset-manifest-v1.json"
    manifest = decode_asset_manifest(example.read_bytes())
    storage = FakeStorage()
    publisher = DualWritePublisher(storage)
    key = publisher.publish_manifest(manifest)
    assert key == f"manifests/assets/{manifest.asset_id}/1.json"
    assert storage.objects[key] == encode(manifest)
    storage.objects[key] = json.dumps(manifest.to_dict()).encode()
    with pytest.raises(DualWriteIntegrityError, match="checksum|Immutable"):
        publisher.publish_manifest(manifest)


def test_operation_id_reuse_compares_canonical_inputs():
    storage = FakeStorage()
    publisher = DualWritePublisher(storage)
    operation = UUID("00000000-0000-4000-8000-000000000201")
    publisher.assert_operation_input(operation, "batch", [{"name": "a.jpg", "sha256": "a" * 64, "size": 1}])
    with pytest.raises(DualWriteIntegrityError, match="different canonical inputs"):
        publisher.assert_operation_input(operation, "batch", [{"name": "a.jpg", "sha256": "b" * 64, "size": 1}])
