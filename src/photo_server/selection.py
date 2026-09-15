from collections import defaultdict
from pathlib import Path

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


def plan_import(settings: Settings, paths: list[str]) -> dict:
    if not paths or len(paths) > settings.max_batch_files:
        raise LibraryError(
            f"Provide 1–{settings.max_batch_files} explicit files; directories are not imported"
        )
    root = settings.import_root.resolve(strict=True)
    groups = defaultdict(list)
    seen = set()
    for value in paths:
        candidate = Path(value)
        path = (candidate if candidate.is_absolute() else root / candidate).resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file():
            raise LibraryError("Every input must be a regular file inside the import root")
        if path in seen:
            continue
        seen.add(path)
        if path.suffix.lower() not in MEDIA | {".xmp"}:
            raise LibraryError(f"Unsupported file extension: {path.name}")
        relative = path.relative_to(root).as_posix()
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
