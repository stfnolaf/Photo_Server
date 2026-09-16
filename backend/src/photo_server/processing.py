"""Reusable, versioned processing stages for imported assets.

Onboarding uses the same metadata stage against its verified local upload. The
worker can later run that stage again from the immutable S3 original, allowing
extractors to evolve without coupling reprocessing to upload batches.
"""

import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid5

from photo_server import metadata
from photo_server.config import LibraryError
from photo_server.models import Manifest, Mutation
from photo_server.storage import canonical_json

STAGE_JOBS = {"metadata": "metadata-v1"}
PROCESSING_JOB_TYPES = tuple(STAGE_JOBS.values())


def extract_metadata(service, path: Path) -> tuple[dict, str]:
    """Run the metadata stage against a verified local original."""
    return metadata.extract(path, service.settings.exiftool)


def process_metadata(service, manifest: Manifest) -> dict:
    """Rebuild one asset's extracted metadata from its immutable original."""
    with TemporaryDirectory(dir=service.scratch) as directory:
        path = Path(directory) / manifest.primary.original_filename
        digest, size = hashlib.sha256(), 0
        with path.open("wb") as output:
            for chunk in service.storage.chunks(manifest.primary.object_key):
                digest.update(chunk)
                size += len(chunk)
                output.write(chunk)
        if size != manifest.primary.size_bytes or digest.hexdigest() != manifest.primary.sha256:
            raise LibraryError(f"Checksum verification failed: {manifest.primary.object_key}")
        info, _mime = extract_metadata(service, path)

    capture_time = info.get("captureTime")
    if info == manifest.metadata and capture_time == manifest.capture_time:
        return {"status": "unchanged", "revision": manifest.revision}

    changes = {"metadata": info, "captureTime": capture_time}
    fingerprint = hashlib.sha256(canonical_json(changes)).hexdigest()
    operation_id = uuid5(
        manifest.library_id,
        f"metadata-v1:{manifest.asset_id}:{fingerprint}",
    )
    result = service.catalog.commit_mutation(
        operation_id,
        Mutation(
            action="asset.metadata",
            entity_id=manifest.asset_id,
            changes=changes,
        ),
    )
    return {"status": "updated", "revision": result["revision"]}


def run_stage(service, asset_id: str, job_type: str) -> dict:
    """Dispatch a claimed processing job through the stage registry."""
    manifest = service.catalog.get(asset_id)
    if manifest is None:
        raise FileNotFoundError("Processing job references a missing asset")
    if job_type == STAGE_JOBS["metadata"]:
        return process_metadata(service, manifest)
    raise LibraryError(f"Unsupported processing job type: {job_type}")
