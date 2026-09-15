from collections import defaultdict
from pathlib import Path, PurePosixPath

from photo_server.config import LibraryError, Settings

RAW = {".arw", ".cr2", ".cr3", ".nef", ".nrw", ".dng", ".raf", ".rw2", ".orf", ".pef", ".srw"}
JPEG = {".jpg", ".jpeg"}
HEIF = {".heif", ".heic", ".hif"}
MEDIA = RAW | JPEG | HEIF


def role(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in RAW:
        return "ORIGINAL_RAW"
    if ext in JPEG:
        return "ORIGINAL_JPEG"
    if ext in HEIF:
        return "ORIGINAL_HEIF"
    return "SIDECAR"


def plan_names(paths: list[str], max_files: int) -> dict:
    """Plan a complete client batch without touching a filesystem."""
    if not paths or len(paths) > max_files:
        raise LibraryError(f"Provide 1–{max_files} files in a batch")
    groups = defaultdict(list)
    seen = set()
    for value in paths:
        if not value or "\\" in value:
            raise LibraryError("Upload paths must be non-empty POSIX paths")
        path = PurePosixPath(value)
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise LibraryError("Upload paths must be relative and cannot traverse directories")
        relative = path.as_posix()
        if relative in seen:
            raise LibraryError(f"Duplicate path in batch: {relative}")
        seen.add(relative)
        if path.suffix.lower() not in MEDIA | {".xmp"}:
            raise LibraryError(f"Unsupported file extension: {path.name}")
        groups[(path.parent, path.stem)].append((path, relative))

    assets, skipped, warnings = [], [], []
    for group in groups.values():
        media = [(path, rel) for path, rel in group if path.suffix.lower() in MEDIA]
        raws = [(path, rel) for path, rel in media if path.suffix.lower() in RAW]
        sidecars = [rel for path, rel in group if path.suffix.lower() == ".xmp"]
        chosen = raws if len(raws) == 1 else media
        if len(raws) == 1:
            skipped.extend(
                {"path": rel, "selected": raws[0][1], "reason": "raw_preferred"}
                for path, rel in media
                if path.suffix.lower() not in RAW
            )
        if len(raws) > 1:
            warnings.append(
                {"reason": "multiple_raw_candidates", "paths": [rel for _, rel in media]}
            )
        for _, rel in chosen:
            attached = sidecars if len(chosen) == 1 and len(sidecars) == 1 else []
            assets.append({"path": rel, "sidecars": attached})
        if sidecars and (len(chosen) != 1 or len(sidecars) != 1):
            warnings.append({"reason": "unassigned_sidecars", "paths": sidecars})
    return {
        "assets": sorted(assets, key=lambda entry: entry["path"]),
        "skipped": sorted(skipped, key=lambda entry: entry["path"]),
        "warnings": warnings,
    }


def plan_import(settings: Settings, paths: list[str]) -> dict:
    """Legacy local-file planner retained for compatibility and low-level tests."""
    root = settings.import_root.resolve(strict=True)
    relative_paths = []
    seen = set()
    for value in paths:
        candidate = Path(value)
        path = (candidate if candidate.is_absolute() else root / candidate).resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file():
            raise LibraryError("Every input must be a regular file inside the import root")
        relative = path.relative_to(root).as_posix()
        if relative not in seen:
            seen.add(relative)
            relative_paths.append(relative)
    return plan_names(relative_paths, settings.max_batch_files)
