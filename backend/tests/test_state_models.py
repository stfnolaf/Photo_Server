from uuid import uuid4

import pytest
from pydantic import ValidationError

from photo_server.browsing import AlbumPatch, UserStatePatch
from photo_server.models import Album, Blob, Manifest, Mutation
from photo_server.state import asset_revision, histories


@pytest.mark.parametrize(
    "changes",
    [
        {"rating": None},
        {"caption": None},
        {"keywords": None},
        {"keywords": [""]},
        {"keywords": ["same", "same"]},
        {"keywords": ["a" * 201]},
        {"location": {"latitude": 12}},
        {"location": {"latitude": 91, "longitude": 0}},
        {"location": {"latitude": 0, "longitude": -181}},
        {"rotation": 90},
    ],
)
def test_metadata_rejects_invalid_values(changes):
    with pytest.raises(ValidationError):
        UserStatePatch(operation_id=uuid4(), **changes)


def test_metadata_allows_explicit_clearing_without_touching_other_fields():
    patch = UserStatePatch(operation_id=uuid4(), caption="", keywords=[], location=None)
    assert patch.changes() == {"caption": "", "keywords": [], "location": None}


@pytest.mark.parametrize(
    "changes", [{}, {"name": "   "}, {"description": None}, {"assetIds": [str(uuid4())] * 2}]
)
def test_album_patch_rejects_ambiguous_membership_and_empty_changes(changes):
    with pytest.raises(ValidationError):
        AlbumPatch(operation_id=uuid4(), **changes)


class MemoryStorage:
    def __init__(self, documents):
        self.documents = documents

    def keys(self, prefix):
        return (key for key in self.documents if key.startswith(prefix))

    def get_json(self, key):
        return self.documents[key]


def sample_manifest():
    asset_id = uuid4()
    blob = Blob(
        blob_id=uuid4(),
        role="ORIGINAL_JPEG",
        original_filename="a.JPG",
        object_key=f"originals/{asset_id}/a.JPG",
        sha256="0" * 64,
        size_bytes=1,
        mime_type="image/jpeg",
    )
    return Manifest(
        library_id=uuid4(),
        asset_id=asset_id,
        operation_id=uuid4(),
        primary_blob_id=blob.blob_id,
        blobs=[blob],
        imported_at="2025-01-01T00:00:00Z",
    )


@pytest.mark.parametrize(
    "corruption", ["gap", "original", "ancestry", "schema", "missing_state", "library"]
)
def test_history_integrity_errors_never_return_older_state(corruption):
    original = sample_manifest()
    revision = asset_revision(
        original, uuid4(), Mutation(action="asset.patch", entity_id=original.asset_id)
    )
    document = revision.document()
    key = revision.key
    if corruption == "gap":
        document.update(revision=3, previousRevision=2)
        key = key.replace("00000002", "00000003")
    elif corruption == "original":
        document["blobs"][0]["sha256"] = "1" * 64
    elif corruption == "ancestry":
        document["previousRevision"] = None
    elif corruption == "schema":
        document["schemaVersion"] = 999
    elif corruption == "library":
        document["libraryId"] = str(uuid4())
    else:
        document.pop("userState")
    storage = MemoryStorage({original.key: original.document(), key: document})
    chains, errors = histories(storage, original.library_id, "assets")
    assert chains == []
    assert len(errors) == 1


def test_album_history_validates_ancestry_and_key():
    library_id, album_id = uuid4(), uuid4()
    album = Album(
        library_id=library_id,
        album_id=album_id,
        operation_id=uuid4(),
        revision=1,
        mutation=Mutation(action="album.create", entity_id=album_id),
        name="Trip",
    )
    document = album.document()
    document["previousRevision"] = 1
    chains, errors = histories(MemoryStorage({album.key: document}), library_id, "albums")
    assert not chains and errors
