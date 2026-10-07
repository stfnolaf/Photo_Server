import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from photo_server.manifests import canonical_json
from photo_server.recovery import (
    create_recovery_checkpoint,
    decode_checkpoint,
    restore_recovery_checkpoint,
    verify_recovery_checkpoint,
)

EXAMPLES = Path(__file__).parents[2] / "docs/in-progress/s3-authoritative-examples"


class MemoryStorage:
    def __init__(self, values=None, bucket="source"):
        self.values = dict(values or {})
        self.bucket = bucket
        self.mutable_writes = []

    def keys(self, prefix):
        return sorted(key for key in self.values if key.startswith(prefix))

    def head(self, key):
        value = self.values.get(key)
        if value is None:
            return None
        body = value[0] if isinstance(value, tuple) else value
        return {"ContentLength": len(body), "LastModified": datetime.now(UTC)}

    def read_bytes(self, key, limit=None):
        value = self.values[key]
        body = value[0] if isinstance(value, tuple) else value
        return body if limit is None else body[: limit + 1]

    def get_json(self, key):
        return json.loads(self.read_bytes(key))

    def put(self, key, body, _mime, _metadata=None):
        if key in self.values:
            return False
        self.values[key] = body
        return True

    def put_json_mutable(self, key, value):
        self.mutable_writes.append(key)
        self.values[key] = canonical_json(value)

    def delete(self, key):
        self.values.pop(key, None)


def _manifest(name, kind, key):
    value = json.loads((EXAMPLES / name).read_text())
    return key, canonical_json(value)


def _source():
    original = b"shared-original"
    digest = hashlib.sha256(original).hexdigest()
    asset = json.loads((EXAMPLES / "asset-manifest-v1.json").read_text())
    asset["blobs"] = asset["blobs"][:1]
    asset["blobs"][0].update({"objectKey": f"objects/{digest}", "sha256": digest, "sizeBytes": len(original)})
    asset["primaryBlobId"] = asset["blobs"][0]["blobId"]
    asset_body = canonical_json(asset)
    album_key, album_body = _manifest("album-manifest-v1.json", "album", "manifests/albums/00000000-0000-4000-0000-000000000301/1.json")
    person_key, person_body = _manifest("person-manifest-v1.json", "person", "manifests/people/00000000-0000-4000-8000-000000000501/1.json")
    tombstone_key, tombstone_body = _manifest("tombstone-v1.json", "tombstone", "tombstones/asset/00000000-0000-4000-8000-000000000101/2.json")
    return MemoryStorage({
        "manifests/assets/00000000-0000-4000-8000-000000000101/1.json": asset_body,
        album_key: album_body,
        person_key: person_body,
        tombstone_key: tombstone_body,
        f"objects/{digest}": original,
    })


def test_checkpoint_copies_all_manifest_kinds_deduplicates_objects_and_is_repeatable():
    source = _source()
    destination = MemoryStorage(bucket="backup")
    first = create_recovery_checkpoint(source, destination, "nightly", database_url="db", dump_runner=lambda _url: b"dump")
    second = create_recovery_checkpoint(source, destination, "nightly", database_url="db", dump_runner=lambda _url: b"different")
    assert first == second
    assert first["status"] == "complete"
    checkpoint = decode_checkpoint(destination.read_bytes("indexes/recovery-checkpoints/nightly.json"))
    assert {item["kind"] for item in checkpoint.manifest_revisions} == {"asset", "album", "person", "tombstone"}
    assert len(checkpoint.objects) == 1
    assert verify_recovery_checkpoint(destination, checkpoint=checkpoint)["status"] == "complete"


def test_malformed_source_and_destination_corruption_fail_closed():
    source = _source()
    source.values["manifests/assets/00000000-0000-4000-8000-000000000101/1.json"] = b"{}"
    result = create_recovery_checkpoint(source, MemoryStorage(), "bad")
    assert result["status"] == "failed"

    source = _source()
    destination = MemoryStorage(bucket="backup")
    result = create_recovery_checkpoint(source, destination, "corrupt")
    assert result["status"] == "complete"
    checkpoint = decode_checkpoint(destination.read_bytes("indexes/recovery-checkpoints/corrupt.json"))
    object_key = f"corrupt/{checkpoint.objects[0]['key']}"
    destination.values[object_key] = b"changed"
    assert verify_recovery_checkpoint(destination, checkpoint=checkpoint)["status"] == "failed"


def test_restore_is_idempotent_and_does_not_mutate_source():
    source = _source()
    destination = MemoryStorage(bucket="backup")
    create_recovery_checkpoint(source, destination, "restore")
    before = dict(destination.values)
    target = MemoryStorage(bucket="fresh")
    result = restore_recovery_checkpoint(destination, target, "indexes/recovery-checkpoints/restore.json")
    assert result["status"] == "complete"
    assert dict(destination.values) == before
    assert restore_recovery_checkpoint(destination, target, "indexes/recovery-checkpoints/restore.json")["status"] == "complete"


def test_restore_rolls_back_new_destination_objects_after_partial_failure():
    source = _source()
    backup = MemoryStorage(bucket="backup")
    create_recovery_checkpoint(source, backup, "partial")

    class FailingTarget(MemoryStorage):
        def __init__(self):
            super().__init__(bucket="fresh")
            self.writes = 0

        def put(self, key, body, mime, metadata=None):
            self.writes += 1
            if self.writes == 2:
                raise OSError("destination outage")
            return super().put(key, body, mime, metadata)

    target = FailingTarget()
    result = restore_recovery_checkpoint(backup, target, "indexes/recovery-checkpoints/partial.json")
    assert result["status"] == "failed"
    assert target.values == {}


def test_postgres_dump_restore_is_verified_and_failure_is_non_success():
    source = _source()
    backup = MemoryStorage(bucket="backup")
    create_recovery_checkpoint(source, backup, "dump", database_url="db", dump_runner=lambda _: b"dump")
    from photo_server.recovery import restore_postgres_dump

    restored = []
    result = restore_postgres_dump(
        backup, "indexes/recovery-checkpoints/dump.json", lambda body: restored.append(body)
    )
    assert result["status"] == "complete"
    assert restored == [b"dump"]
    failed = restore_postgres_dump(
        backup, "indexes/recovery-checkpoints/dump.json", lambda _body: (_ for _ in ()).throw(OSError("db down"))
    )
    assert failed["status"] == "failed"


def test_source_destination_and_dump_outages_fail_closed():
    class BrokenList(MemoryStorage):
        def keys(self, prefix):
            raise OSError(f"list outage: {prefix}")

    assert create_recovery_checkpoint(BrokenList(), MemoryStorage(), "list") ["status"] == "failed"

    source = _source()
    source.values.pop(next(key for key in source.values if key.startswith("objects/")))
    assert create_recovery_checkpoint(source, MemoryStorage(), "missing")["status"] == "failed"

    class BrokenDestination(MemoryStorage):
        def put(self, *_args, **_kwargs):
            raise OSError("copy outage")

    assert create_recovery_checkpoint(_source(), BrokenDestination(), "copy")["status"] == "failed"
    failed_dump = create_recovery_checkpoint(
        _source(), MemoryStorage(), "dump-failure", database_url="db",
        dump_runner=lambda _url: (_ for _ in ()).throw(OSError("pg_dump outage")),
    )
    assert failed_dump["status"] == "failed"


def test_immutable_destination_conflict_fails_closed():
    source = _source()
    destination = MemoryStorage(bucket="backup")
    destination.values["nightly/manifests/assets/00000000-0000-4000-8000-000000000101/1.json"] = b"conflict"
    result = create_recovery_checkpoint(source, destination, "nightly")
    assert result["status"] == "failed"
