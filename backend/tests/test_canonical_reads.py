"""Canonical S3 runtime read tests."""

import json
from pathlib import Path
from uuid import UUID

import pytest

from photo_server.config import LibraryError
from photo_server.service import Service

EXAMPLES = Path(__file__).parent / "fixtures" / "s3-authoritative-examples"


class MemoryStorage:
    def __init__(self, values):
        self.values = values

    def keys(self, prefix):
        return (key for key in sorted(self.values) if key.startswith(prefix))

    def read_bytes(self, key):
        return self.values[key]


def service_with_manifest(revision=1):
    payload = json.loads((EXAMPLES / "asset-manifest-v1.json").read_text())
    payload["revision"] = revision
    payload["parentRevision"] = revision - 1 if revision > 1 else None
    key = f"manifests/assets/{payload['assetId']}/{revision}.json"
    service = Service.__new__(Service)
    service.storage = MemoryStorage({key: json.dumps(payload).encode()})
    return service, UUID(payload["assetId"]), payload


def test_runtime_resolver_reads_the_latest_canonical_asset_manifest():
    service, asset_id, payload = service_with_manifest(revision=3)
    older = dict(payload, revision=2, parentRevision=1)
    service.storage.values[
        f"manifests/assets/{asset_id}/2.json"
    ] = json.dumps(older).encode()

    manifest = service.canonical_asset(asset_id)

    assert manifest.revision == 3
    assert manifest.primary.object_key == f"objects/{manifest.primary.sha256}"


def test_runtime_resolver_requires_a_canonical_manifest():
    service = Service.__new__(Service)
    service.storage = MemoryStorage({})

    with pytest.raises(LibraryError, match="Canonical asset manifest is missing"):
        service.canonical_asset("00000000-0000-0000-0000-000000000001")
