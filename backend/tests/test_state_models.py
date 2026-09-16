import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from photo_server.browsing import AlbumPatch, UserStatePatch
from photo_server.metadata import extract, lens_display, technical_fields


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


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        ({"LensModel": "FE 35mm F1.4 GM"}, "FE 35mm F1.4 GM"),
        ({"LensMake": "Sigma", "LensType": "24-70mm F2.8 DG DN"}, "Sigma 24-70mm F2.8 DG DN"),
        ({"LensID": "Canon EF 50mm f/1.8 STM"}, "Canon EF 50mm f/1.8 STM"),
        ({"LensID": 0}, None),
    ],
)
def test_lens_display_uses_camera_vendor_fallbacks(metadata, expected):
    assert lens_display(metadata) == expected


def test_technical_fields_normalize_common_exposure_metadata():
    assert technical_fields(
        {
            "LensModel": "35mm Prime",
            "FNumber": 2.8,
            "FocalLength": 35,
            "FocalLengthIn35mmFormat": 52,
            "ISO": 400,
            "ExposureTime": 0.008,
        }
    ) == {
        "lens": "35mm Prime",
        "aperture": 2.8,
        "focalLength": 35,
        "focalLength35mm": 52,
        "iso": 400,
        "exposureTime": 0.008,
        "shutterSpeed": None,
        "exposureCompensation": None,
        "exposureProgram": None,
        "meteringMode": None,
        "flash": None,
        "whiteBalance": None,
    }


def test_extraction_requests_and_normalizes_lens_and_exposure_fields(tmp_path, monkeypatch):
    path = tmp_path / "sample.JPG"
    path.write_bytes(b"fixture")
    recorded = {}
    document = {
        "SourceFile": str(path),
        "FileType": "JPEG",
        "MIMEType": "image/jpeg",
        "LensMake": "Sigma",
        "LensModel": "24-70mm F2.8 DG DN",
        "FNumber": 2.8,
        "FocalLength": 50,
        "FocalLengthIn35mmFormat": 50,
        "ISO": 800,
        "ExposureTime": 0.004,
    }

    def run(arguments, **kwargs):
        recorded["arguments"] = arguments
        return SimpleNamespace(stdout=json.dumps([document]).encode())

    monkeypatch.setattr("photo_server.metadata.subprocess.run", run)
    metadata, mime = extract(path, "exiftool")
    assert mime == "image/jpeg"
    assert metadata["lensDisplay"] == "Sigma 24-70mm F2.8 DG DN"
    assert technical_fields(metadata)["exposureTime"] == 0.004
    assert "-LensSpecification" in recorded["arguments"]
    assert "-FocalLengthIn35mmFormat#" in recorded["arguments"]
    assert "-ExposureCompensation#" in recorded["arguments"]
