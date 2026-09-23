"""Local, versioned photo understanding through remote AI services.

The semantic model is reached through an OpenAI-compatible chat-completions
endpoint (``ai_base_url``; the default Compose target is vLLM's ``/v1`` path).
Face detection and embedding run in the standalone face-service, which
the worker talks to through ``face_client`` (Phase 2B of
``docs/ai-service-split-plan.md``) — no learned models live in the photo
server.
"""

import base64
import io
import json
from pathlib import Path
from typing import Literal

import httpx
from PIL import Image
from pydantic import Field, field_validator

from photo_server.models import DurableModel

PIPELINE_VERSION = "photo-ai-v1"
ANALYSIS_JOB_TYPE = "ai-v1"
ANALYSIS_TYPE = "photo-ai"

# A model-list lookup is a short health-style probe, not an inference request.
DIGEST_TIMEOUT_SECONDS = 5.0


class AIClientError(Exception):
    """Base class for VLM (OpenAI chat-completions) client failures."""


class AIRequestError(AIClientError):
    """The VLM rejected the request (4xx, or an unusable response shape).

    The analysis job fails with this error, as it did before the split."""


class AIServiceUnavailableError(AIClientError):
    """The VLM could not be reached or is pacing the client (connection
    error, timeout, 429, 502-504). Phase 3A routes this class to job
    requeue instead of failure; until then it fails the job like any
    other error."""


class DetectedObject(DurableModel):
    name: str = Field(min_length=1, max_length=80)
    count: int = Field(default=1, ge=1, le=1000)


class SemanticAnalysis(DurableModel):
    summary: str = Field(min_length=1, max_length=800)
    photo_types: list[
        Literal[
            "portrait",
            "group",
            "street",
            "travel",
            "landscape",
            "wildlife",
            "architecture",
            "event",
            "food",
            "document",
            "screenshot",
            "other",
        ]
    ] = Field(min_length=1, max_length=4)
    scene: str = Field(min_length=1, max_length=160)
    setting: Literal["indoor", "outdoor", "mixed", "unknown"]
    objects: list[DetectedObject] = Field(default_factory=list, max_length=40)
    activities: list[str] = Field(default_factory=list, max_length=20)
    tags: list[str] = Field(default_factory=list, max_length=40)
    visible_text: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("activities", "tags", "visible_text")
    @classmethod
    def bounded_terms(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        if any(not value or len(value) > 160 for value in cleaned):
            raise ValueError("Analysis terms must contain 1-160 characters")
        return list(dict.fromkeys(cleaned))


def searchable_text(analysis: SemanticAnalysis, people: list[str] | None = None) -> str:
    """Flatten structured output for deterministic literal content search."""
    values = [
        analysis.summary,
        *analysis.photo_types,
        analysis.scene,
        analysis.setting,
        *(item.name for item in analysis.objects),
        *analysis.activities,
        *analysis.tags,
        *analysis.visible_text,
        *(people or []),
    ]
    return " ".join(dict.fromkeys(value.strip() for value in values if value.strip()))


def prepare_jpeg(path: Path, max_side: int) -> bytes:
    """Create a bounded, metadata-free RGB input for model inference
    (sent to the VLM or the face-service)."""
    with Image.open(path) as source:
        image = source.convert("RGB")
        image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=90, optimize=True)
    return output.getvalue()


def jpeg_dimensions(jpeg: bytes) -> tuple[int, int]:
    """Return encoded JPEG dimensions without decoding pixel data."""
    with Image.open(io.BytesIO(jpeg)) as image:
        return image.size


def _model_digest(client: httpx.Client, model: str) -> str:
    response = client.get("/models")
    response.raise_for_status()
    payload = response.json()
    entries = payload.get("data", []) if isinstance(payload, dict) else []
    requested = model.removesuffix(":latest")
    for entry in entries:
        name = str(entry.get("id", ""))
        if name == model or name.removesuffix(":latest") == requested:
            return str(entry.get("digest") or f"model:{name}")
    return "unknown"


def resolve_model_digest(settings, model: str) -> str:
    """Resolve the current model digest from the VLM without starting inference.

    ``GET {ai_base_url}/models`` with a short fixed timeout. Use a provider
    digest when one is exposed; otherwise use the matched model ID as a
    stable provider-neutral identity. Any failure — unreachable, HTTP error,
    or model not listed — yields ``"unknown"``, which the reuse gates treat
    as "never reuse".
    """
    try:
        with httpx.Client(
            base_url=settings.ai_base_url.rstrip("/"), timeout=DIGEST_TIMEOUT_SECONDS
        ) as client:
            return _model_digest(client, model)
    except httpx.HTTPError:
        return "unknown"


def _vlm_body(settings, jpeg: bytes, response_format: str) -> dict:
    schema = SemanticAnalysis.model_json_schema(by_alias=True)
    prompt = (
        "Analyze this personal photograph for a private photo-library search index. "
        "Describe only visually supported content. Use concrete, common object and scene terms, "
        "include relevant photo types, activities, weather, landmarks, and readable text in the "
        "appropriate fields. Do not identify people or infer sensitive traits. Return JSON matching "
        f"this schema exactly: {json.dumps(schema, separators=(',', ':'))}"
    )
    if response_format == "json_object":
        format_body: dict = {"type": "json_object"}
    else:
        format_body = {
            "type": "json_schema",
            "json_schema": {"name": "photo_analysis", "strict": True, "schema": schema},
        }
    body: dict = {}
    if settings.ai_extra_body:
        # Provider extensions; validated as a
        # JSON object at config parse. Contract fields below always win.
        body.update(json.loads(settings.ai_extra_body))
    body.update(
        {
            "model": settings.ai_model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{base64.b64encode(jpeg).decode('ascii')}"
                            },
                        },
                    ],
                }
            ],
            "temperature": 0,
            "max_tokens": 900,
            "response_format": format_body,
        }
    )
    return body


def _vlm_client(settings) -> httpx.Client:
    headers = {"Authorization": f"Bearer {settings.ai_api_key}"} if settings.ai_api_key else {}
    return httpx.Client(
        base_url=settings.ai_base_url.rstrip("/"),
        timeout=settings.ai_timeout_seconds,
        headers=headers,
    )


def _raise_for_status(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    detail = ""
    try:
        body = response.json()
        if isinstance(body, dict) and isinstance(body.get("error"), dict):
            detail = str(body["error"].get("message") or "")
    except (httpx.DecodingError, ValueError):
        detail = response.text[:200]
    message = f"VLM request failed with HTTP {response.status_code}"
    if detail:
        message = f"{message}: {detail}"
    if response.status_code == 429 or response.status_code in (502, 503, 504):
        raise AIServiceUnavailableError(message) from None
    raise AIRequestError(message) from None


def _vlm_content(payload) -> str:
    if not isinstance(payload, dict):
        raise AIRequestError("VLM response is not a JSON object")
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise AIRequestError("VLM response is missing choices[0].message.content") from None
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    if not isinstance(content, str):
        raise AIRequestError("VLM response content is not text")
    return content


def _vlm_metrics(payload: dict) -> dict:
    metrics: dict = {}
    usage = payload.get("usage")
    if isinstance(usage, dict):
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            if usage.get(key) is not None:
                metrics[key] = usage[key]
    if payload.get("system_fingerprint") is not None:
        metrics["system_fingerprint"] = payload["system_fingerprint"]
    return metrics


def analyze_semantics(settings, jpeg: bytes) -> tuple[SemanticAnalysis, str, dict]:
    """Run one VLM analysis through the OpenAI chat-completions endpoint.

    Returns the validated ``SemanticAnalysis``, the current model digest,
    and provenance metrics. Connection errors, timeouts, 429, and 502-504
    raise ``AIServiceUnavailableError``; other 4xx responses and unusable
    response shapes raise ``AIRequestError``. A successful response whose
    JSON does not match ``SemanticAnalysis`` raises ``pydantic.ValidationError``
    and fails the job, as before. If the strict ``json_schema`` response
    format is rejected (400/422), one retry uses ``json_object`` — the
    prompt already embeds the schema.
    """
    with _vlm_client(settings) as client:
        try:
            response = client.post(
                "/chat/completions", json=_vlm_body(settings, jpeg, "json_schema")
            )
            if response.status_code in (400, 422):
                response = client.post(
                    "/chat/completions", json=_vlm_body(settings, jpeg, "json_object")
                )
            _raise_for_status(response)
            payload = response.json()
        except httpx.TransportError as error:
            raise AIServiceUnavailableError(f"VLM unreachable: {error}") from error
        except (httpx.DecodingError, ValueError) as error:
            raise AIRequestError(f"VLM returned a non-JSON response: {error}") from error
        analysis = SemanticAnalysis.model_validate_json(_vlm_content(payload))
    return analysis, resolve_model_digest(settings, settings.ai_model), _vlm_metrics(payload)
