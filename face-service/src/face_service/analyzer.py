"""Verified face models: YuNet detection, SFace alignment, AdaFace IR101 embeddings.

Copied verbatim from ``photo_server.analysis`` when the face pipeline moved out
of the photo server (service split, Phase 2A). The photo server keeps its copy
until Phase 2B removes it, so the two ``ADAFACE_IDENTITY`` constants must stay
byte-identical (one embedding space). The cv2/numpy/onnxruntime imports stay
lazy inside the class, so importing this module needs no GPU stack.
"""

import hashlib
import io
import json
from pathlib import Path

from PIL import Image

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
