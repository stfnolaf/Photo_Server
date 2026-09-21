"""Local, versioned photo understanding and face embeddings.

The semantic model is reached through an OpenAI-compatible chat-completions
endpoint (``ai_base_url``; the default is the private Ollama service's ``/v1``
path). Face detection/alignment follows the sibling face-scanner's YuNet
pipeline and uses its verified AdaFace IR101 CUDA export for embeddings.
"""

import base64
import hashlib
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


ADAFACE_IDENTITY = {
    "name": "adaface-ir101",
    "revision": "54f602a0737bd1ee4a4e7e9fd089a485f397fefd",
    "weights_sha256": "2ea535a43877bd3de8091903935c783ce335be66a9f8917fae9a7a18ae4bbf56",
    "preprocessing": "RGB float32 [-1,1] NCHW, ArcFace 112x112",
}
MODEL_FILES = {
    "detector": (
        "face_detection_yunet_2023mar.onnx",
        "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
    ),
    "aligner": (
        "face_recognition_sface_2021dec.onnx",
        "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
    ),
}


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
    """Create a bounded, metadata-free RGB input for local model inference."""
    with Image.open(path) as source:
        image = source.convert("RGB")
        image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=90, optimize=True)
    return output.getvalue()


def _model_digest(client: httpx.Client, model: str) -> str:
    response = client.get("/models")
    response.raise_for_status()
    payload = response.json()
    entries = payload.get("data", []) if isinstance(payload, dict) else []
    requested = model.removesuffix(":latest")
    for entry in entries:
        name = str(entry.get("id", ""))
        if name == model or name.removesuffix(":latest") == requested:
            return str(entry.get("digest") or "unknown")
    return "unknown"


def resolve_model_digest(settings, model: str) -> str:
    """Resolve the current model digest from the VLM without starting inference.

    ``GET {ai_base_url}/models`` with a short fixed timeout; the entry's
    ``digest`` field (an Ollama ``/v1/models`` extension) is used when
    present. Any failure — unreachable, HTTP error, model not listed, or
    no digest field — yields ``"unknown"``, which the reuse gates treat
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
        # Provider extensions (e.g. Ollama options.num_ctx); validated as a
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


def _checked_model(directory: Path, filename: str, expected: str) -> Path:
    path = directory / filename
    if not path.is_file():
        raise RuntimeError(f"Required face model is missing: {path}")
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != expected:
        raise RuntimeError(f"Face model checksum mismatch: {path}")
    return path


class AdaFaceAnalyzer:
    """One CUDA AdaFace session, with CPU YuNet detection and SFace alignment."""

    def __init__(self, settings):
        import cv2
        import onnxruntime as ort

        directory = settings.face_models_dir
        detector = _checked_model(directory, *MODEL_FILES["detector"])
        aligner = _checked_model(directory, *MODEL_FILES["aligner"])
        metadata_path = directory / "adaface-ir101.json"
        model_path = directory / "adaface-ir101.onnx"
        if not metadata_path.is_file() or not model_path.is_file():
            raise RuntimeError(f"Required AdaFace model files are missing from {directory}")
        metadata = json.loads(metadata_path.read_text())
        if any(metadata.get(key) != value for key, value in ADAFACE_IDENTITY.items()):
            raise RuntimeError("AdaFace provenance differs from the verified face-scanner model")
        _checked_model(directory, model_path.name, metadata.get("onnx_sha256", ""))

        self.detector = cv2.FaceDetectorYN.create(
            str(detector), "", (320, 320), settings.face_detection_threshold, 0.3, 5000
        )
        self.aligner = cv2.FaceRecognizerSF.create(str(aligner), "")
        ort.preload_dlls(directory="")
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(
            str(model_path),
            sess_options=options,
            providers=[("CUDAExecutionProvider", {"use_tf32": "0"}), "CPUExecutionProvider"],
        )
        if self.session.get_providers()[0] != "CUDAExecutionProvider":
            raise RuntimeError(
                "AdaFace requires CUDA; refusing silent CPU fallback. Check Docker GPU access."
            )
        self.input_name = self.session.get_inputs()[0].name
        self.model_version = f"{metadata['revision']}:{metadata['onnx_sha256']}"

    def analyze(self, jpeg: bytes) -> list[dict]:
        import cv2
        import numpy as np

        with Image.open(io.BytesIO(jpeg)) as source:
            rgb = np.asarray(source.convert("RGB"))
        bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        height, width = rgb.shape[:2]
        self.detector.setInputSize((width, height))
        _, found = self.detector.detect(bgr)
        if found is None:
            return []

        detected, aligned = [], []
        for face in found:
            x, y, face_width, face_height = map(float, face[:4])
            detected.append(
                {
                    "box": [
                        max(0.0, x / width),
                        max(0.0, y / height),
                        min(1.0, face_width / width),
                        min(1.0, face_height / height),
                    ],
                    "confidence": float(face[-1]),
                }
            )
            crop = self.aligner.alignCrop(bgr, face)
            aligned.append(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB))

        for start in range(0, len(aligned), 32):
            images = np.asarray(aligned[start : start + 32], dtype=np.float32) / 127.5 - 1.0
            images = np.ascontiguousarray(images.transpose(0, 3, 1, 2))
            vectors = self.session.run(None, {self.input_name: images})[0]
            if vectors.shape != (len(images), 512) or not np.isfinite(vectors).all():
                raise RuntimeError("AdaFace returned invalid embeddings")
            vectors /= np.maximum(np.linalg.norm(vectors, axis=1, keepdims=True), 1e-12)
            for item, vector in zip(detected[start : start + 32], vectors, strict=True):
                item["embedding"] = vector.astype(float).tolist()
        return detected
