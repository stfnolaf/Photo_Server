import json
import subprocess
from pathlib import Path

from photo_server.config import LibraryError
from photo_server.selection import HEIF, JPEG, RAW


def extract(path: Path, executable: str) -> tuple[dict, str]:
    result = subprocess.run(
        [
            executable,
            "-json",
            "-n",
            "-FileType",
            "-MIMEType",
            "-Make",
            "-Model",
            "-LensModel",
            "-LensID",
            "-FocalLength",
            "-FNumber",
            "-ExposureTime",
            "-ISO",
            "-DateTimeOriginal",
            "-OffsetTimeOriginal",
            "-ImageWidth",
            "-ImageHeight",
            "-Orientation",
            "-GPSLatitude",
            "-GPSLongitude",
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
    return metadata, metadata.get("MIMEType", "application/octet-stream")
