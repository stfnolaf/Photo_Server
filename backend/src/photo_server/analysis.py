"""Local, versioned photo understanding and face embeddings.

The semantic model is reached only through the private Ollama service. Face
detection/alignment follows the sibling face-scanner's YuNet pipeline and uses
its verified AdaFace IR101 CUDA export for embeddings.
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


def _ollama_model_digest(client: httpx.Client, model: str) -> str:
    response = client.get("/api/tags")
    response.raise_for_status()
    models = response.json().get("models", [])
    requested = model.removesuffix(":latest")
    for entry in models:
        name = str(entry.get("name", ""))
        if name == model or name.removesuffix(":latest") == requested:
            return str(entry.get("digest") or "unknown")
    return "unknown"


def resolve_model_digest(settings, model: str) -> str:
    """Resolve the current model digest from Ollama without starting inference."""
    try:
        with httpx.Client(
            base_url=settings.ai_ollama_url.rstrip("/"), timeout=settings.ai_timeout_seconds
        ) as client:
            return _ollama_model_digest(client, model)
    except httpx.HTTPError:
        return "unknown"


def analyze_semantics(settings, jpeg: bytes) -> tuple[SemanticAnalysis, str, dict]:
    schema = SemanticAnalysis.model_json_schema(by_alias=True)
    prompt = (
        "Analyze this personal photograph for a private photo-library search index. "
        "Describe only visually supported content. Use concrete, common object and scene terms, "
        "include relevant photo types, activities, weather, landmarks, and readable text in the "
        "appropriate fields. Do not identify people or infer sensitive traits. Return JSON matching "
        f"this schema exactly: {json.dumps(schema, separators=(',', ':'))}"
    )
    with httpx.Client(
        base_url=settings.ai_ollama_url.rstrip("/"), timeout=settings.ai_timeout_seconds
    ) as client:
        response = client.post(
            "/api/chat",
            json={
                "model": settings.ai_model,
                "messages": [
                    {
                        "role": "user",
                        "content": prompt,
                        "images": [base64.b64encode(jpeg).decode("ascii")],
                    }
                ],
                "format": schema,
                "stream": False,
                "think": False,
                "keep_alive": "10m",
                "options": {
                    "temperature": 0,
                    "num_ctx": settings.ai_context_tokens,
                    "num_predict": 900,
                },
            },
        )
        response.raise_for_status()
        payload = response.json()
        analysis = SemanticAnalysis.model_validate_json(payload["message"]["content"])
        digest = _ollama_model_digest(client, settings.ai_model)
    metrics = {
        name: payload.get(name)
        for name in (
            "total_duration",
            "load_duration",
            "prompt_eval_count",
            "prompt_eval_duration",
            "eval_count",
            "eval_duration",
        )
        if payload.get(name) is not None
    }
    return analysis, digest, metrics


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
