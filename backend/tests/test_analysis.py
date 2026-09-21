import io

import pytest
from PIL import Image
from pydantic import ValidationError

from photo_server.analysis import SemanticAnalysis, prepare_jpeg, searchable_text


def result(**changes):
    value = {
        "summary": "Two people hiking beside a blue alpine lake",
        "photoTypes": ["travel", "group"],
        "scene": "mountain lake",
        "setting": "outdoor",
        "objects": [{"name": "backpack", "count": 2}, {"name": "lake", "count": 1}],
        "activities": ["hiking"],
        "tags": ["mountains", "holiday"],
        "visibleText": [],
    }
    value.update(changes)
    return value


def test_semantic_analysis_flattens_content_for_search():
    analysis = SemanticAnalysis.model_validate(result())
    text = searchable_text(analysis)
    for term in ("hiking", "backpack", "travel", "mountain lake", "holiday"):
        assert term in text


@pytest.mark.parametrize(
    "changes",
    [
        {"photoTypes": ["not-a-type"]},
        {"setting": "space"},
        {"objects": [{"name": "", "count": 1}]},
        {"activities": [""]},
        {"unexpected": True},
    ],
)
def test_semantic_analysis_rejects_unbounded_model_output(changes):
    with pytest.raises(ValidationError):
        SemanticAnalysis.model_validate(result(**changes))


def test_prepare_jpeg_bounds_dimensions_and_drops_metadata(tmp_path):
    source = tmp_path / "source.jpg"
    Image.new("RGB", (2400, 1200), "navy").save(source, exif=b"Exif\x00\x00fixture")
    prepared = prepare_jpeg(source, 1000)
    with Image.open(io.BytesIO(prepared)) as image:
        assert image.size == (1000, 500)
        assert not image.getexif()
