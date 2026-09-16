import hashlib
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from photo_server.config import LibraryError, Settings
from photo_server.service import Service
from photo_server.storage import canonical_json


def export_library(settings: Settings, destination: Path, include_trash: bool = False) -> dict:
    """Create a portable export from PostgreSQL state and immutable S3 originals."""
    service = Service(settings)
    try:
        service.initialize(recover_uploads=False)
        manifests = service.catalog.all_assets()
        albums = service.catalog.all_albums()
        known_assets = {manifest.asset_id for manifest in manifests}
        if any(asset_id not in known_assets for album in albums for asset_id in album.asset_ids):
            raise LibraryError("Cannot export an album that references a missing asset")
        destination.mkdir(parents=True, exist_ok=True)
        if any(destination.iterdir()):
            raise LibraryError("Choose an empty export destination")
        exported = 0
        for manifest in manifests:
            if manifest.deleted_at and not include_trash:
                continue
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
                        for chunk in service.storage.chunks(blob.object_key):
                            digest.update(chunk)
                            size += len(chunk)
                            output.write(chunk)
                        output.flush()
                        if digest.hexdigest() != blob.sha256 or size != blob.size_bytes:
                            raise LibraryError(f"Export checksum failed: {blob.object_key}")
                        os.link(temporary, target)
                    finally:
                        temporary.unlink(missing_ok=True)
            with (folder / "manifest.json").open("xb") as output:
                output.write(canonical_json(manifest.document()))
            exported += 1
        with (destination / "library-state.json").open("xb") as output:
            output.write(
                canonical_json(
                    {
                        "schemaVersion": 1,
                        "libraryId": str(service.library_id),
                        "albums": [album.document() for album in albums],
                        "trashedAssets": [
                            manifest.document() for manifest in manifests if manifest.deleted_at
                        ],
                    }
                )
            )
        return {
            "exported": exported,
            "destination": str(destination),
            "verification": "sha256",
        }
    finally:
        service.catalog.engine.dispose()
