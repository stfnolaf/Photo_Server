import json
import subprocess
from pathlib import Path

from photo_server.config import LibraryError
from photo_server.selection import HEIF, JPEG, RAW


def lens_display(metadata: dict) -> str | None:
    """Choose the most descriptive lens name emitted by different camera makers."""
    value = next(
        (
            metadata.get(name)
            for name in ("LensModel", "LensID", "LensType", "LensInfo", "LensSpecification")
            if metadata.get(name) not in (None, "", 0, "0")
        ),
        None,
    )
    if value is None:
        return None
    if isinstance(value, (list, dict)):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    value = str(value).strip()
    make = str(metadata.get("LensMake") or "").strip()
    if make and make.casefold() not in value.casefold():
        return f"{make} {value}"
    return value


def technical_fields(metadata: dict) -> dict:
    """Normalize commonly displayed exposure fields while retaining raw EXIF."""
    return {
        "lens": lens_display(metadata),
        "aperture": metadata.get("FNumber") or metadata.get("Aperture"),
        "focalLength": metadata.get("FocalLength"),
        "focalLength35mm": metadata.get("FocalLengthIn35mmFormat"),
        "iso": metadata.get("ISO"),
        "exposureTime": metadata.get("ExposureTime"),
        "shutterSpeed": metadata.get("ShutterSpeed"),
        "exposureCompensation": metadata.get("ExposureCompensation"),
        "exposureProgram": metadata.get("ExposureProgram"),
        "meteringMode": metadata.get("MeteringMode"),
        "flash": metadata.get("Flash"),
        "whiteBalance": metadata.get("WhiteBalance"),
    }


def extract(path: Path, executable: str) -> tuple[dict, str]:
    result = subprocess.run(
        [
            executable,
            "-json",
            "-FileType",
            "-MIMEType",
            "-Make",
            "-Model",
            "-LensMake",
            "-LensModel",
            "-LensID",
            "-LensType",
            "-LensInfo",
            "-LensSpecification",
            "-FocalLength#",
            "-FocalLengthIn35mmFormat#",
            "-FNumber#",
            "-Aperture#",
            "-ExposureTime#",
            "-ShutterSpeed",
            "-ISO#",
            "-ExposureCompensation#",
            "-ExposureProgram",
            "-MeteringMode",
            "-Flash",
            "-WhiteBalance",
            "-DateTimeOriginal",
            "-OffsetTimeOriginal",
            "-ImageWidth#",
            "-ImageHeight#",
            "-Orientation#",
            "-GPSLatitude#",
            "-GPSLongitude#",
            "-Error",
            str(path),
        ],
        capture_output=True,
        check=True,
        timeout=90,
    )
    metadata = json.loads(result.stdout)[0]
    metadata.pop("SourceFile", None)
    detected = metadata.get("FileType", "").upper()
    ext = path.suffix.lower()
    allowed = {suffix[1:].upper() for suffix in RAW}
    if ext in JPEG:
        allowed = {"JPEG"}
    elif ext in HEIF:
        allowed = {"HEIC", "HEIF", "HIF"}
    if metadata.get("Error") or detected not in allowed:
        raise LibraryError(f"File content does not match a supported original: {path.name}")
    capture = metadata.get("DateTimeOriginal")
    if capture:
        # Preserve unknown timezone as a naive timestamp; never assume the server's timezone.
        metadata["captureTime"] = capture.replace(":", "-", 2).replace(" ", "T", 1)
        if metadata.get("OffsetTimeOriginal"):
            metadata["captureTime"] += metadata["OffsetTimeOriginal"]
    lens = lens_display(metadata)
    if lens:
        metadata["lensDisplay"] = lens
    return metadata, metadata.get("MIMEType", "application/octet-stream")
