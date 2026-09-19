import io
import json
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory

import pillow_heif
from PIL import Image, ImageOps

from photo_server.models import Manifest
from photo_server.processing import run_stage
from photo_server.service import Service
from photo_server.uploads import cleanup_abandoned_batches, process_onboarding_job

pillow_heif.register_heif_opener()


def cache_paths(service: Service, manifest: Manifest) -> dict[str, Path]:
    directory = (
        service.settings.data_dir / "cache" / f"{manifest.asset_id}-{manifest.primary.sha256}-v1"
    )
    return {"preview": directory / "preview.jpg", "thumbnail": directory / "thumbnail.jpg"}


def _cache_sizes(targets: dict[str, Path]) -> tuple[int | None, int | None]:
    return tuple(path.stat().st_size if path.exists() else None for path in targets.values())


def generate(service: Service, manifest: Manifest) -> bool:
    targets = cache_paths(service, manifest)
    if all(path.exists() for path in targets.values()):
        # Files from before tracking (or a crashed first run): backfill a row
        # without pretending this is a fresh access.
        preview_bytes, thumbnail_bytes = _cache_sizes(targets)
        service.catalog.backfill_preview_cache(
            str(manifest.asset_id), preview_bytes, thumbnail_bytes
        )
        return True
    with TemporaryDirectory(dir=service.scratch) as directory:
        original = Path(directory) / manifest.primary.original_filename
        with original.open("wb") as stream:
            for chunk in service.storage.chunks(manifest.primary.object_key):
                stream.write(chunk)
        source = original
        if manifest.primary.role == "ORIGINAL_RAW":
            preview = None
            for tag in ("JpgFromRaw", "PreviewImage", "ThumbnailImage"):
                result = subprocess.run(
                    [service.settings.exiftool, "-b", f"-{tag}", str(original)],
                    capture_output=True,
                    timeout=90,
                    check=False,
                )
                if result.returncode == 0 and result.stdout:
                    try:
                        with Image.open(io.BytesIO(result.stdout)) as candidate:
                            # JPEG.verify() only checks the header. Decode pixels so
                            # a corrupt large preview can fall back to a usable tag.
                            candidate.load()
                        preview = result.stdout
                        break
                    except (OSError, ValueError, Image.DecompressionBombError):
                        continue
            if preview is None:
                return False
            source = io.BytesIO(preview)
        with Image.open(source) as image:
            if not image.getexif().get(274) and manifest.metadata.get("Orientation"):
                image.getexif()[274] = int(manifest.metadata["Orientation"])
            oriented = ImageOps.exif_transpose(image).convert("RGB")
            for kind, path in targets.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                resized = oriented.copy()
                edge = 2560 if kind == "preview" else 256
                resized.thumbnail((edge, edge), Image.Resampling.LANCZOS)
                with NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as temporary:
                    temporary_path = Path(temporary.name)
                try:
                    resized.save(
                        temporary_path,
                        format="JPEG",
                        quality=85,
                        icc_profile=image.info.get("icc_profile"),
                    )
                    os.replace(temporary_path, path)
                finally:
                    temporary_path.unlink(missing_ok=True)
    # Record the freshly written set; the upsert also counts as an access.
    preview_bytes, thumbnail_bytes = _cache_sizes(targets)
    service.catalog.record_preview_cache(
        str(manifest.asset_id), preview_bytes, thumbnail_bytes
    )
    return True


def rebuild_cache_index(service: Service) -> dict:
    """Backfill preview_cache rows for cache directories that predate tracking.

    Walks ``{data_dir}/cache`` for ``{asset_id}-{sha256}-v1`` directories and
    inserts a row (sizes via ``stat``) for each asset that lacks one. Never
    updates an existing row. Orphaned directories whose asset was purged are
    counted as skipped.
    """
    cache_root = service.settings.data_dir / "cache"
    directories = 0
    added = 0
    skipped = 0
    if cache_root.is_dir():
        for entry in sorted(cache_root.iterdir()):
            if not entry.is_dir():
                continue
            match = re.fullmatch(r"(?P<asset_id>.+)-[0-9a-f]{64}-v1", entry.name)
            if match is None:
                continue
            directories += 1
            asset_id = match.group("asset_id")
            preview = entry / "preview.jpg"
            thumbnail = entry / "thumbnail.jpg"
            try:
                if service.catalog.backfill_preview_cache(
                    asset_id,
                    preview.stat().st_size if preview.exists() else None,
                    thumbnail.stat().st_size if thumbnail.exists() else None,
                ):
                    added += 1
                else:
                    skipped += 1
            except Exception:
                # Orphaned directory: the asset row is gone (FK violation on
                # insert). Leave the files; they are harmless.
                skipped += 1
    return {"status": "ok", "directories": directories, "rowsAdded": added, "rowsSkipped": skipped}


def run_once(service: Service) -> dict | None:
    onboarding = service.catalog.claim_onboarding_job()
    if onboarding is not None:
        try:
            result = process_onboarding_job(service, onboarding)
        except Exception as error:
            try:
                service.catalog.finish_onboarding_job(onboarding, error=str(error))
            except Exception:
                pass
            result = {"status": "failed", "error": str(error)}
        return {"jobType": "onboarding", "jobId": onboarding["id"], **result}

    processing = service.catalog.claim_processing_job()
    if processing is not None:
        asset_id = processing["asset_id"]
        job_type = processing["job_type"]
        try:
            result = run_stage(service, asset_id, job_type)
            service.catalog.finish_processing_job(asset_id, job_type, "ready")
            return {
                "jobType": "processing",
                "stage": job_type,
                "assetId": asset_id,
                **result,
            }
        except Exception as error:
            service.catalog.finish_processing_job(asset_id, job_type, "failed", str(error))
            return {
                "jobType": "processing",
                "stage": job_type,
                "assetId": asset_id,
                "status": "failed",
                "error": str(error),
            }

    asset_id = service.catalog.claim_job()
    if asset_id is None:
        return None
    try:
        manifest = service.catalog.get(asset_id)
        if manifest is None:
            raise ValueError("Preview job references missing asset")
        status = "ready" if generate(service, manifest) else "unavailable"
        service.catalog.finish_job(asset_id, status)
        return {"jobType": "preview", "assetId": asset_id, "status": status}
    except Exception as error:
        service.catalog.finish_job(asset_id, "failed", str(error))
        return {
            "jobType": "preview",
            "assetId": asset_id,
            "status": "failed",
            "error": str(error),
        }


def _worker_loop(service: Service):
    while True:
        try:
            result = run_once(service)
        except Exception as error:
            print(json.dumps({"status": "worker_error", "error": str(error)}), flush=True)
            time.sleep(2)
            continue
        if result:
            print(json.dumps(result), flush=True)
        else:
            time.sleep(2)


def _cleanup_loop(service: Service):
    while True:
        try:
            result = cleanup_abandoned_batches(service)
            if result["batchesDeleted"]:
                print(json.dumps({"status": "upload_cleanup", **result}), flush=True)
        except Exception as error:
            print(json.dumps({"status": "upload_cleanup_error", "error": str(error)}), flush=True)
        time.sleep(60)


def run(service: Service):
    with ThreadPoolExecutor(
        max_workers=service.settings.worker_threads + 1,
        thread_name_prefix="photo-worker",
    ) as executor:
        futures = [
            executor.submit(_worker_loop, service) for _ in range(service.settings.worker_threads)
        ]
        futures.append(executor.submit(_cleanup_loop, service))
        for future in futures:
            future.result()
