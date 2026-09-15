import hashlib
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import UUID

from photo_server.config import LibraryError, Settings
from photo_server.models import Manifest
from photo_server.storage import Storage, canonical_json


def export_library(settings: Settings, destination: Path) -> dict:
    """Export with only S3 credentials and manifests; PostgreSQL is never contacted."""
    storage = Storage(settings)
    marker = storage.get_json("library.json")
    if marker.get("schemaVersion") != 1:
        raise LibraryError("Unsupported library schema")
    library_id = UUID(marker["libraryId"])
    destination.mkdir(parents=True, exist_ok=True)
    exported = 0
    for key in storage.keys("state/assets/"):
        manifest = Manifest.model_validate(storage.get_json(key))
        if manifest.library_id != library_id or manifest.key != key:
            raise LibraryError(f"Invalid manifest: {key}")
        folder = destination / str(manifest.asset_id)
        folder.mkdir(exist_ok=True)
        for blob in manifest.blobs:
            target = folder / blob.original_filename
            if target.exists():
                raise LibraryError(
                    f"Export destination already contains {target}; choose an empty destination"
                )
            with NamedTemporaryFile(dir=folder, delete=False) as output:
                temporary = Path(output.name)
                digest, size = hashlib.sha256(), 0
                try:
                    for chunk in storage.chunks(blob.object_key):
                        digest.update(chunk)
                        size += len(chunk)
                        output.write(chunk)
                    output.flush()
                    if digest.hexdigest() != blob.sha256 or size != blob.size_bytes:
                        raise LibraryError(f"Export checksum failed: {blob.object_key}")
                    os.link(temporary, target)  # Atomic create; never overwrite an existing file.
                finally:
                    temporary.unlink(missing_ok=True)
        with (folder / "manifest.json").open("xb") as output:
            output.write(canonical_json(manifest.document()))
        exported += 1
    return {"exported": exported, "destination": str(destination), "verification": "sha256"}
