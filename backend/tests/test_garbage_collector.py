import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from photo_server.garbage_collector import RetentionPolicy, collect_garbage
from photo_server.manifests import canonical_json

ROOT = Path(__file__).parents[2] / "docs/in-progress/s3-authoritative-examples"


class FakeStorage:
    def __init__(self, values):
        self.values = dict(values)
        self.writes = []

    def keys(self, prefix):
        return sorted(key for key in self.values if key.startswith(prefix))

    def head(self, key):
        value = self.values.get(key)
        if value is None:
            return None
        body, modified = value if isinstance(value, tuple) else (value, datetime(2020, 1, 1, tzinfo=timezone.utc))
        return {"ContentLength": len(body), "LastModified": modified}

    def read_bytes(self, key):
        value = self.values[key]
        return value[0] if isinstance(value, tuple) else value

    def get_json(self, key):
        return json.loads(self.read_bytes(key))

    def put_json_mutable(self, key, value):
        self.writes.append(key)
        self.values[key] = (canonical_json(value), datetime.now(timezone.utc))


def _storage():
    payload = json.loads((ROOT / "asset-manifest-v1.json").read_text())
    live = b"live"
    digest = hashlib.sha256(live).hexdigest()
    payload["blobs"] = [payload["blobs"][0]]
    payload["blobs"][0].update({"objectKey": f"objects/{digest}", "sha256": digest, "sizeBytes": len(live)})
    payload["primaryBlobId"] = payload["blobs"][0]["blobId"]
    now = datetime(2026, 10, 6, tzinfo=timezone.utc)
    old = (b"orphan", datetime(2020, 1, 1, tzinfo=timezone.utc))
    temp = (b"partial", datetime(2020, 1, 1, tzinfo=timezone.utc))
    return FakeStorage({
        "manifests/assets/00000000-0000-4000-8000-000000000101/1.json": canonical_json(payload),
        f"objects/{digest}": (live, now),
        f"objects/{hashlib.sha256(old[0]).hexdigest()}": old,
        "incoming/batch/partial": temp,
    })


def _tombstone(deleted_at="2026-10-06T19:10:00Z"):
    value = json.loads((ROOT / "tombstone-v1.json").read_text())
    value.update({"revision": 2, "parentRevision": 1, "deletedAt": deleted_at, "createdAt": deleted_at})
    return canonical_json(value)


def test_reachable_objects_are_protected_and_expired_objects_are_candidates():
    storage = _storage()
    result = collect_garbage(
        storage,
        as_of="2026-10-07T00:00:00Z",
        policy=RetentionPolicy(original_object_days=1, temporary_upload_days=1),
    )
    assert result["status"] == "complete"
    keys = {item["key"] for item in result["candidates"]}
    orphan_key = f"objects/{hashlib.sha256(b'orphan').hexdigest()}"
    assert "incoming/batch/partial" in keys
    assert orphan_key in keys
    assert f"objects/{hashlib.sha256(b'live').hexdigest()}" not in keys
    assert f"objects/{hashlib.sha256(b'live').hexdigest()}" not in keys


def test_malformed_manifest_fails_closed_and_never_reports_candidates():
    storage = _storage()
    key = "manifests/assets/bad/1.json"
    storage.values[key] = b'{"not":"a manifest"}'
    result = collect_garbage(storage, as_of="2026-10-07T00:00:00Z")
    assert result["status"] == "failed"
    assert result["candidates"] == []


def test_scan_checkpoint_resume_and_repeat_are_deterministic():
    storage = _storage()
    first = collect_garbage(storage, checkpoint_id="resume", stop_after=1, as_of="2026-10-07T00:00:00Z")
    assert first["status"] == "paused"
    resumed = collect_garbage(storage, checkpoint_id="resume", as_of="2026-10-07T00:00:00Z")
    repeated = collect_garbage(storage, checkpoint_id="resume", as_of="2026-10-07T00:00:00Z")
    assert resumed == repeated
    assert storage.writes == ["indexes/checkpoints/gc-resume.json"] * 3


def test_default_mode_has_no_delete_operation():
    storage = _storage()
    before = dict(storage.values)
    result = collect_garbage(storage, as_of="2026-10-07T00:00:00Z")
    assert result["deletionEnabled"] is False
    assert set(storage.values) == set(before) | {"indexes/checkpoints/gc-default.json"}


def test_retained_tombstone_protects_deleted_asset_and_expiry_allows_candidate():
    storage = _storage()
    tombstone_key = "tombstones/asset/00000000-0000-4000-8000-000000000101/2.json"
    storage.values[tombstone_key] = _tombstone()
    retained = collect_garbage(storage, as_of="2026-10-07T00:00:00Z")
    live_key = f"objects/{hashlib.sha256(b'live').hexdigest()}"
    assert retained["status"] == "complete"
    assert live_key not in {item["key"] for item in retained["candidates"]}

    storage.values[tombstone_key] = _tombstone("2020-01-01T00:00:00Z")
    expired = collect_garbage(
        storage,
        as_of="2026-10-07T00:00:00Z",
        policy=RetentionPolicy(historical_revision_days=0, tombstone_days=1, deleted_days=1, original_object_days=1),
    )
    assert expired["status"] == "complete"
    assert live_key in {item["key"] for item in expired["candidates"]}


def test_historical_revision_and_checkpoint_roots_protect_shared_objects():
    storage = _storage()
    payload = json.loads((ROOT / "asset-manifest-v1.json").read_text())
    digest = hashlib.sha256(b"live").hexdigest()
    payload["blobs"] = [payload["blobs"][0]]
    payload["blobs"][0].update({"objectKey": f"objects/{digest}", "sha256": digest, "sizeBytes": 4})
    payload["primaryBlobId"] = payload["blobs"][0]["blobId"]
    payload.update({"revision": 2, "parentRevision": 1, "operationId": "00000000-0000-4000-8000-000000000202"})
    storage.values["manifests/assets/00000000-0000-4000-8000-000000000101/2.json"] = canonical_json(payload)
    checkpoint = {"schemaVersion": 1, "manifestKeys": ["objects/" + hashlib.sha256(b"orphan").hexdigest()]}
    storage.values["indexes/checkpoints/recovery.json"] = (canonical_json(checkpoint), datetime(2026, 10, 6, tzinfo=timezone.utc))
    result = collect_garbage(storage, as_of="2026-10-07T00:00:00Z")
    assert result["status"] == "complete"
    assert f"objects/{hashlib.sha256(b'orphan').hexdigest()}" not in {item["key"] for item in result["candidates"]}


def test_missing_checkpoint_reference_and_object_fail_closed():
    storage = _storage()
    storage.values["indexes/checkpoints/recovery.json"] = (
        canonical_json({"schemaVersion": 1, "objectKey": "objects/" + "e" * 64}),
        datetime(2026, 10, 6, tzinfo=timezone.utc),
    )
    result = collect_garbage(storage, as_of="2026-10-07T00:00:00Z")
    assert result["status"] == "failed"
    assert result["candidates"] == []


def test_storage_outage_and_malformed_checkpoint_fail_closed():
    class Broken(FakeStorage):
        def keys(self, prefix):
            if prefix == "objects/":
                raise OSError("list outage")
            return super().keys(prefix)

    result = collect_garbage(Broken(_storage().values), as_of="2026-10-07T00:00:00Z")
    assert result["status"] == "failed"
    assert result["candidates"] == []


def test_processing_artifact_reference_and_checksum_mismatch_fail_closed():
    storage = _storage()
    input_digest = hashlib.sha256(b"live").hexdigest()
    result_bytes = b"durable-derived"
    result_digest = hashlib.sha256(result_bytes).hexdigest()
    artifact = json.loads((ROOT / "processing-artifact-v1.json").read_text())
    artifact["assetId"] = "00000000-0000-4000-8000-000000000101"
    artifact["inputSha256"] = input_digest
    artifact["resultObject"].update({"objectKey": f"objects/{result_digest}", "sha256": result_digest, "sizeBytes": len(result_bytes)})
    asset_key = "manifests/assets/00000000-0000-4000-8000-000000000101/1.json"
    asset = json.loads(storage.read_bytes(asset_key))
    asset["processing"] = [{
        "artifactKey": f"objects/{result_digest}", "artifactSha256": result_digest,
        "inputSha256": input_digest, "processingType": "semantic-analysis",
        "implementationVersion": "semantic-v1",
    }]
    storage.values[asset_key] = canonical_json(asset)
    storage.values["manifests/processing/00000000-0000-4000-8000-000000000601.json"] = canonical_json(artifact)
    storage.values[f"objects/{result_digest}"] = (result_bytes, datetime(2026, 10, 6, tzinfo=timezone.utc))
    result = collect_garbage(storage, as_of="2026-10-07T00:00:00Z")
    assert result["status"] == "complete"
    assert f"objects/{result_digest}" not in {item["key"] for item in result["candidates"]}

    storage.values[f"objects/{result_digest}"] = (b"corrupt", datetime(2026, 10, 6, tzinfo=timezone.utc))
    result = collect_garbage(storage, as_of="2026-10-07T00:00:00Z")
    assert result["status"] == "failed"
    assert result["candidates"] == []


def test_divergent_history_and_out_of_scope_prefix_are_safe():
    storage = _storage()
    payload = json.loads((ROOT / "asset-manifest-v1.json").read_text())
    payload["revision"] = 3
    payload["parentRevision"] = 2
    payload["operationId"] = "00000000-0000-4000-8000-000000000203"
    storage.values["manifests/assets/00000000-0000-4000-8000-000000000101/3.json"] = canonical_json(payload)

    class Isolated(FakeStorage):
        def keys(self, prefix):
            assert prefix in {"manifests/", "tombstones/", "objects/", "indexes/checkpoints/", "reconciliation/", "incoming/"}
            return super().keys(prefix)

    isolated = Isolated(storage.values)
    result = collect_garbage(isolated, as_of="2026-10-07T00:00:00Z")
    assert result["status"] == "failed"
    assert result["candidates"] == []

    storage = _storage()
    storage.values["indexes/checkpoints/bad.json"] = b"not-json"
    result = collect_garbage(storage, as_of="2026-10-07T00:00:00Z")
    assert result["status"] == "failed"
    assert result["candidates"] == []
