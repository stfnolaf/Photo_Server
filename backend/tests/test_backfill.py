import hashlib
from datetime import UTC, datetime
from uuid import UUID

from photo_server.backfill import (
    BackfillReport,
    _immutable_put,
    asset_manifest_from_row,
    tombstone_from_row,
)
from photo_server.manifests import decode_asset_manifest, encode

LIBRARY = "00000000-0000-4000-8000-000000000001"
ASSET = "00000000-0000-4000-8000-000000000002"
BLOB = "00000000-0000-4000-8000-000000000003"
DIGEST = hashlib.sha256(b"raw").hexdigest()


class Storage:
    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.puts = []

    def head(self, key):
        return None if key not in self.objects else {"ContentLength": len(self.objects[key])}

    def verify(self, key, size, digest):
        assert len(self.objects[key]) == size
        assert hashlib.sha256(self.objects[key]).hexdigest() == digest

    def put(self, key, body, _mime):
        self.puts.append(key)
        self.objects[key] = body


def row():
    return {
        "id": ASSET, "state_revision": 1,
        "manifest": {
            "libraryId": LIBRARY, "assetId": ASSET, "operationId": ASSET,
            "createdAt": "2025-01-01T00:00:00Z", "importedAt": "2025-01-01T00:00:00Z",
            "captureTime": None, "primaryBlobId": BLOB, "extractedMetadata": {},
            "userState": {"rating": 0, "favorite": False, "caption": "", "keywords": [], "location": None},
        }, "rating": 0, "favorite": False,
    }


def test_postgres_row_conversion_round_trips_through_codec():
    record = asset_manifest_from_row(row(), [{
        "id": BLOB, "role": "ORIGINAL_RAW", "object_key": "originals/x/raw.nef",
        "original_filename": "raw.nef", "sha256": DIGEST, "size_bytes": 3, "mime_type": "image/x-raw",
    }])
    assert decode_asset_manifest(encode(record)) == record
    assert record.blobs[0].object_key == f"objects/{DIGEST}"


def test_tombstone_generation_is_next_revision_and_deterministic():
    deleted = datetime(2025, 1, 2, tzinfo=UTC)
    first = tombstone_from_row(UUID(LIBRARY), "asset", UUID(ASSET), 4, deleted)
    assert first == tombstone_from_row(UUID(LIBRARY), "asset", UUID(ASSET), 4, deleted)
    assert first.revision == 5 and first.parent_revision == 4


def test_immutable_put_dry_run_does_not_mutate_and_existing_bytes_are_verified():
    storage = Storage()
    report = BackfillReport()
    _immutable_put(storage, "objects/x", b"bytes", "application/octet-stream", report, True)
    assert storage.puts == [] and storage.objects == {}
    storage.objects["objects/x"] = b"bytes"
    _immutable_put(storage, "objects/x", b"bytes", "application/octet-stream", report, False)
    assert storage.puts == []
