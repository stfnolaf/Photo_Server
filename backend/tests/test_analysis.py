import io
import json as json_module

import httpx
import pytest
from PIL import Image
from pydantic import ValidationError

from photo_server.analysis import SemanticAnalysis, analyze_semantics, prepare_jpeg, searchable_text


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


def test_ollama_uses_structured_local_vision_request(monkeypatch):
    captured = {}

    class Response:
        def __init__(self, body):
            self.body = body

        def raise_for_status(self):
            return None

        def json(self):
            return self.body

    class Client:
        def __init__(self, **kwargs):
            captured["client"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def post(self, path, json):
            captured["path"] = path
            captured["payload"] = json
            return Response({"message": {"content": json_module.dumps(result())}})

        def get(self, path):
            assert path == "/api/tags"
            return Response({"models": [{"name": "qwen-test", "digest": "sha256:model"}]})

    monkeypatch.setattr(httpx, "Client", Client)
    settings = type(
        "Settings",
        (),
        {
            "ai_ollama_url": "http://ollama:11434",
            "ai_timeout_seconds": 30,
            "ai_model": "qwen-test",
            "ai_context_tokens": 4096,
        },
    )()
    analysis, digest, _metrics = analyze_semantics(settings, b"jpeg")
    assert analysis.scene == "mountain lake"
    assert digest == "sha256:model"
    assert captured["path"] == "/api/chat"
    assert captured["payload"]["stream"] is False
    assert captured["payload"]["think"] is False
    assert captured["payload"]["format"]["additionalProperties"] is False
    assert captured["payload"]["messages"][0]["images"]
