import hashlib
import json
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

from photo_server.manifests import canonical_json
from photo_server.rebuild import RebuildReport, _history, rebuild_from_s3

EXAMPLES = Path(__file__).parents[2] / "docs/in-progress/s3-authoritative-examples"


class FakeStorage:
    def __init__(self, objects=None):
        self.objects = dict(objects or {})

    def keys(self, prefix):
        return sorted(key for key in self.objects if key.startswith(prefix))

    def head(self, key):
        value = self.objects.get(key)
        return None if value is None else {"ContentLength": len(value)}

    def read_bytes(self, key, limit=None):
        value = self.objects[key]
        return value if limit is None else value[: limit + 1]

    def get_json(self, key):
        return json.loads(self.objects[key])

    def put(self, key, body, _mime):
        self.objects[key] = body
        return True


class FakeCatalog:
    def __init__(self):
        self.assets = []
        self.albums = []
        self.people = []

    @contextmanager
    def writer(self):
        yield

    def apply(self, value):
        self.assets.append(value)

    def apply_album(self, value):
        self.albums.append(value)

    def apply_person(self, value, face_ids=()):
        self.people.append((value, tuple(face_ids)))


def asset_payload():
    payload = json.loads((EXAMPLES / "asset-manifest-v1.json").read_text())
    digest = hashlib.sha256(b"photo").hexdigest()
    payload["blobs"][0].update({"objectKey": f"objects/{digest}", "sha256": digest, "sizeBytes": 5})
    payload["blobs"] = payload["blobs"][:1]
    payload["primaryBlobId"] = payload["blobs"][0]["blobId"]
    return payload, digest


def test_scan_is_sorted_and_checkpoint_resume_is_idempotent():
    payload, digest = asset_payload()
    first = canonical_json(payload)
    second_payload = deepcopy(payload)
    second_payload["assetId"] = "00000000-0000-4000-8000-000000000102"
    second_payload["operationId"] = "00000000-0000-4000-8000-000000000202"
    second = canonical_json(second_payload)
    storage = FakeStorage(
        {
            f"objects/{digest}": b"photo",
            "manifests/assets/00000000-0000-4000-8000-000000000101/1.json": first,
            "manifests/assets/00000000-0000-4000-8000-000000000102/1.json": second,
        }
    )
    catalog = FakeCatalog()
    paused = rebuild_from_s3(storage, catalog, checkpoint_id="resume", stop_after=1)
    assert paused["status"] == "paused"
    result = rebuild_from_s3(storage, catalog, checkpoint_id="resume")
    assert result["status"] == "complete"
    assert [str(item.asset_id) for item in catalog.assets] == [
        "00000000-0000-4000-8000-000000000102"
    ]
    repeated = rebuild_from_s3(storage, catalog, checkpoint_id="resume")
    assert repeated["status"] == "complete"


def test_missing_object_and_manifest_checksum_are_reported_without_projection():
    payload, _ = asset_payload()
    key = "manifests/assets/00000000-0000-4000-8000-000000000101/1.json"
    storage = FakeStorage({key: json.dumps(payload).encode()})
    catalog = FakeCatalog()
    result = rebuild_from_s3(storage, catalog, checkpoint_id="errors", resume=False)
    assert result["status"] == "failed"
    assert result["checksumMismatches"]
    assert not catalog.assets


def test_malformed_newest_revision_does_not_fall_back():
    payload, digest = asset_payload()
    newest = deepcopy(payload)
    newest["revision"] = 2
    newest["parentRevision"] = 1
    newest["unexpected"] = True
    storage = FakeStorage(
        {
            f"objects/{digest}": b"photo",
            "manifests/assets/00000000-0000-4000-8000-000000000101/1.json": canonical_json(payload),
            "manifests/assets/00000000-0000-4000-8000-000000000101/2.json": canonical_json(newest),
        }
    )
    catalog = FakeCatalog()
    result = rebuild_from_s3(storage, catalog, checkpoint_id="malformed", resume=False)
    assert result["status"] == "failed"
    assert result["malformedManifests"]
    assert not catalog.assets


def test_duplicate_revision_and_parent_gap_are_reported():
    payload, digest = asset_payload()
    duplicate = deepcopy(payload)
    duplicate["operationId"] = "00000000-0000-4000-8000-000000000299"
    gap = deepcopy(payload)
    gap["revision"] = 3
    gap["parentRevision"] = 2
    storage = FakeStorage(
        {
            f"objects/{digest}": b"photo",
            "manifests/assets/00000000-0000-4000-8000-000000000101/1.json": canonical_json(payload),
            "manifests/assets/00000000-0000-4000-8000-000000000101/3.json": canonical_json(gap),
        }
    )
    result = rebuild_from_s3(storage, FakeCatalog(), checkpoint_id="ancestry", resume=False)
    assert result["status"] == "failed"
    assert result["parentGaps"]
    from photo_server.manifests import decode_asset_manifest

    first = decode_asset_manifest(canonical_json(payload))
    second = decode_asset_manifest(canonical_json(duplicate))
    report = RebuildReport()
    assert _history([("a", first), ("b", second)], report, "asset", str(first.asset_id)) is None
    assert report.duplicate_revisions
    assert report.multiple_valid_heads


def test_person_manifest_is_projected_with_canonical_face_assignments():
    person = json.loads((EXAMPLES / "person-manifest-v1.json").read_text())
    key = f"manifests/people/{person['personId']}/1.json"
    catalog = FakeCatalog()
    result = rebuild_from_s3(
        FakeStorage({key: canonical_json(person)}), catalog,
        checkpoint_id="person", resume=False,
    )
    assert result["status"] == "complete"
    assert result["projectedPeople"] == 1
    assert catalog.people[0][0].display_name == "Avery"
    assert [str(face_id) for face_id in catalog.people[0][1]] == person["faceIds"]
