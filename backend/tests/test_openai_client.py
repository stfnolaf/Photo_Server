"""Wire-level tests for the OpenAI-standard VLM client in analysis.py.

The VLM endpoint is simulated with an ``httpx.MockTransport`` forced through
a patched ``httpx.Client``: no AI service is started and no request leaves
the process.
"""

import base64
import json

import httpx
import pytest
from pydantic import ValidationError

from photo_server.analysis import (
    AIClientError,
    AIRequestError,
    AIServiceUnavailableError,
    SemanticAnalysis,
    analyze_semantics,
    resolve_model_digest,
)
from photo_server.config import Settings

VALID_ANALYSIS = {
    "summary": "Two people hiking beside a blue alpine lake",
    "photoTypes": ["travel", "group"],
    "scene": "mountain lake",
    "setting": "outdoor",
    "objects": [{"name": "backpack", "count": 2}],
    "activities": ["hiking"],
    "tags": ["mountains"],
    "visibleText": [],
}


def make_settings(**overrides) -> Settings:
    values = dict(
        _env_file=None,
        s3_endpoint="http://s3:9000",
        database_url="postgresql+psycopg://test:pw@localhost/test",
        ai_base_url="http://vlm:11434/v1",
        ai_model="qwen3-vl:8b-instruct-q4_K_M",
    )
    values.update(overrides)
    return Settings(**values)


def scripted(handler):
    """Run ``handler`` per request and record every request made."""

    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        result = handler(request, len(requests))
        if isinstance(result, BaseException):
            raise result
        return result

    return handle, requests


def patch_transport(monkeypatch, handle):
    real_client = httpx.Client

    class _Client(real_client):
        def __init__(self, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handle)
            super().__init__(**kwargs)

    monkeypatch.setattr(httpx, "Client", _Client)


def completion(content=VALID_ANALYSIS, usage=None, fingerprint="fp-test"):
    if isinstance(content, dict):
        content = json.dumps(content)
    body = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
    }
    if usage is not None:
        body["usage"] = usage
    if fingerprint is not None:
        body["system_fingerprint"] = fingerprint
    return httpx.Response(200, json=body)


def models_response(*entries):
    return httpx.Response(200, json={"object": "list", "data": list(entries)})


def chat_requests(requests):
    return [request for request in requests if request.method == "POST"]


def test_error_classes_form_a_hierarchy():
    assert issubclass(AIRequestError, AIClientError)
    assert issubclass(AIServiceUnavailableError, AIClientError)


def test_request_shape_bearer_and_extra_body(monkeypatch):
    usage = {"prompt_tokens": 11, "completion_tokens": 77, "total_tokens": 88}

    def handler(request, _n):
        if request.method == "GET":
            assert request.url.path == "/v1/models"
            return models_response(
                {"id": "qwen3-vl:8b-instruct-q4_K_M", "digest": "sha256:abc"}
            )
        return completion(usage=usage)

    handle, requests = scripted(handler)
    patch_transport(monkeypatch, handle)
    settings = make_settings(ai_api_key="sk-test", ai_extra_body='{"options": {"num_ctx": 8192}}')
    analysis, digest, metrics = analyze_semantics(settings, b"jpeg-bytes")

    assert analysis.scene == "mountain lake"
    assert analysis.objects[0].name == "backpack"
    assert digest == "sha256:abc"
    assert metrics == {
        "prompt_tokens": 11,
        "completion_tokens": 77,
        "total_tokens": 88,
        "system_fingerprint": "fp-test",
    }

    chat = chat_requests(requests)
    assert len(chat) == 1  # a successful response is never retried
    chat = chat[0]
    assert chat.url.path == "/v1/chat/completions"
    assert chat.headers["Authorization"] == "Bearer sk-test"
    body = json.loads(chat.content)
    assert body["model"] == "qwen3-vl:8b-instruct-q4_K_M"
    assert body["temperature"] == 0
    assert body["max_tokens"] == 900
    assert body["options"] == {"num_ctx": 8192}  # extra body merged in
    format_body = body["response_format"]
    assert format_body["type"] == "json_schema"
    assert format_body["json_schema"]["name"] == "photo_analysis"
    assert format_body["json_schema"]["strict"] is True
    assert format_body["json_schema"]["schema"] == SemanticAnalysis.model_json_schema(by_alias=True)
    message = body["messages"][0]
    assert message["role"] == "user"
    parts = message["content"]
    assert parts[0]["type"] == "text"
    assert parts[0]["text"].startswith("Analyze this personal photograph")
    assert "Do not identify people" in parts[0]["text"]
    assert parts[1]["type"] == "image_url"
    url = parts[1]["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == b"jpeg-bytes"


def test_no_bearer_header_when_key_empty(monkeypatch):
    def handler(request, _n):
        if request.method == "GET":
            return models_response()
        return completion()

    handle, requests = scripted(handler)
    patch_transport(monkeypatch, handle)
    analyze_semantics(make_settings(), b"x")
    assert "Authorization" not in chat_requests(requests)[0].headers


def test_extra_body_cannot_override_contract_fields(monkeypatch):
    def handler(request, _n):
        if request.method == "GET":
            return models_response()
        return completion()

    handle, requests = scripted(handler)
    patch_transport(monkeypatch, handle)
    settings = make_settings(ai_extra_body='{"temperature": 7, "options": {"num_ctx": 2}}')
    analyze_semantics(settings, b"x")
    body = json.loads(chat_requests(requests)[0].content)
    assert body["temperature"] == 0  # contract fields win
    assert body["max_tokens"] == 900
    assert body["options"] == {"num_ctx": 2}


@pytest.mark.parametrize("status", [400, 422])
def test_json_schema_rejection_falls_back_to_json_object(monkeypatch, status):
    formats = []

    def handler(request, _n):
        if request.method == "GET":
            return models_response({"id": "qwen3-vl:8b-instruct-q4_K_M", "digest": "sha256:abc"})
        formats.append(json.loads(request.content)["response_format"]["type"])
        if formats[-1] == "json_schema":
            return httpx.Response(
                status,
                json={"error": {"message": "json_schema not supported", "type": "invalid_request_error"}},
            )
        return completion()

    handle, requests = scripted(handler)
    patch_transport(monkeypatch, handle)
    analysis, digest, _metrics = analyze_semantics(make_settings(), b"x")
    assert analysis.scene == "mountain lake"
    assert digest == "sha256:abc"
    assert formats == ["json_schema", "json_object"]  # exactly one retry
    assert len(chat_requests(requests)) == 2


@pytest.mark.parametrize("status", [400, 422])
def test_persistent_rejection_is_a_request_error(monkeypatch, status):
    def handler(request, _n):
        if request.method == "GET":
            return models_response()
        return httpx.Response(status, json={"error": {"message": "bad schema"}})

    handle, requests = scripted(handler)
    patch_transport(monkeypatch, handle)
    with pytest.raises(AIRequestError, match="bad schema"):
        analyze_semantics(make_settings(), b"x")
    assert len(chat_requests(requests)) == 2  # the retry was made, then it failed


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (429, AIServiceUnavailableError),
        (502, AIServiceUnavailableError),
        (503, AIServiceUnavailableError),
        (504, AIServiceUnavailableError),
    ],
)
def test_rate_limited_and_gateway_errors_are_service_unavailable(monkeypatch, status, expected):
    def handler(request, _n):
        return httpx.Response(status, json={"error": {"message": "saturated"}})

    handle, requests = scripted(handler)
    patch_transport(monkeypatch, handle)
    with pytest.raises(expected, match="saturated"):
        analyze_semantics(make_settings(), b"x")
    assert len(requests) == 1  # 429/5xx are never retried


@pytest.mark.parametrize("status", [401, 403, 404, 500, 501, 505])
def test_other_http_errors_are_request_errors(monkeypatch, status):
    def handler(request, _n):
        return httpx.Response(status, json={"error": {"message": f"bad-{status}"}})

    handle, requests = scripted(handler)
    patch_transport(monkeypatch, handle)
    with pytest.raises(AIRequestError, match=f"bad-{status}"):
        analyze_semantics(make_settings(), b"x")
    assert len(requests) == 1


def test_connection_error_is_service_unavailable(monkeypatch):
    def handler(request, _n):
        raise httpx.ConnectError("connection refused", request=request)

    handle, requests = scripted(handler)
    patch_transport(monkeypatch, handle)
    with pytest.raises(AIServiceUnavailableError, match="unreachable"):
        analyze_semantics(make_settings(), b"x")
    assert len(requests) == 1


def test_timeout_is_service_unavailable(monkeypatch):
    def handler(request, _n):
        raise httpx.ReadTimeout("timed out", request=request)

    handle, _requests = scripted(handler)
    patch_transport(monkeypatch, handle)
    with pytest.raises(AIServiceUnavailableError, match="unreachable"):
        analyze_semantics(make_settings(), b"x")


def test_non_json_response_is_a_request_error(monkeypatch):
    def handler(request, _n):
        return httpx.Response(
            200, content=b"<html>gateway</html>", headers={"Content-Type": "text/html"}
        )

    handle, _requests = scripted(handler)
    patch_transport(monkeypatch, handle)
    with pytest.raises(AIRequestError, match="non-JSON"):
        analyze_semantics(make_settings(), b"x")


def test_unusable_response_shape_is_a_request_error(monkeypatch):
    def handler(request, _n):
        return httpx.Response(200, json={"unexpected": True})

    handle, _requests = scripted(handler)
    patch_transport(monkeypatch, handle)
    with pytest.raises(AIRequestError, match="choices"):
        analyze_semantics(make_settings(), b"x")


def test_invalid_json_response_fails_the_job(monkeypatch):
    def handler(request, _n):
        if request.method == "GET":
            return models_response()
        return completion("this is not json")

    handle, requests = scripted(handler)
    patch_transport(monkeypatch, handle)
    with pytest.raises(ValidationError):
        analyze_semantics(make_settings(), b"x")
    assert len(chat_requests(requests)) == 1  # a 200 response is never retried


def test_schema_violation_fails_the_job(monkeypatch):
    bad = dict(VALID_ANALYSIS, photoTypes=["not-a-type"])

    def handler(request, _n):
        if request.method == "GET":
            return models_response()
        return completion(bad)

    handle, _requests = scripted(handler)
    patch_transport(monkeypatch, handle)
    with pytest.raises(ValidationError):
        analyze_semantics(make_settings(), b"x")


def test_digest_matches_model_id_with_latest_suffix(monkeypatch):
    def handler(request, _n):
        assert request.method == "GET"
        assert request.url.path == "/v1/models"
        return models_response(
            {"id": "qwen3-vl:8b-instruct-q4_K_M:latest", "digest": "sha256:abc"},
            {"id": "llava:13b"},
        )

    handle, requests = scripted(handler)
    patch_transport(monkeypatch, handle)
    settings = make_settings()
    assert resolve_model_digest(settings, settings.ai_model) == "sha256:abc"
    assert len(requests) == 1


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"data": [{"id": "m2:latest"}]}, "model:m2:latest"),  # provider has no digest extension
        ({"data": [{"id": "other", "digest": "sha256:other"}]}, "unknown"),  # no match
        ({"data": []}, "unknown"),  # nothing listed
        ({"wrong": "shape"}, "unknown"),  # not the models envelope
    ],
)
def test_digest_is_unknown_when_unresolvable(monkeypatch, payload, expected):
    def handler(request, _n):
        return httpx.Response(200, json=payload)

    handle, _requests = scripted(handler)
    patch_transport(monkeypatch, handle)
    assert resolve_model_digest(make_settings(), "m2") == expected


def test_digest_http_error_is_unknown(monkeypatch):
    def handler(request, _n):
        return httpx.Response(500, json={"error": {"message": "nope"}})

    handle, _requests = scripted(handler)
    patch_transport(monkeypatch, handle)
    assert resolve_model_digest(make_settings(), "m2") == "unknown"


def test_digest_unreachable_is_unknown(monkeypatch):
    def handler(request, _n):
        raise httpx.ConnectError("connection refused", request=request)

    handle, _requests = scripted(handler)
    patch_transport(monkeypatch, handle)
    assert resolve_model_digest(make_settings(), "m2") == "unknown"
