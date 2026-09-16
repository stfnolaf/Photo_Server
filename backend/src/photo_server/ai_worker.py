"""Single-concurrency background worker for local CUDA photo analysis."""

import json
import time
from datetime import UTC, datetime
from uuid import uuid4

from photo_server.analysis import (
    ADAFACE_IDENTITY,
    ANALYSIS_TYPE,
    PIPELINE_VERSION,
    AdaFaceAnalyzer,
    analyze_semantics,
    prepare_jpeg,
    searchable_text,
)
from photo_server.service import Service
from photo_server.worker import cache_paths


class AIWorker:
    def __init__(self, service: Service):
        self.service = service
        self._faces = None

    @property
    def faces(self) -> AdaFaceAnalyzer:
        if self._faces is None:
            self._faces = AdaFaceAnalyzer(self.service.settings)
        return self._faces

    def run_once(self) -> dict | None:
        job = self.service.catalog.claim_ai_job()
        if job is None:
            return None
        asset_id = job["asset_id"]
        try:
            if job["preview_status"] != "ready":
                raise RuntimeError(
                    f"AI analysis requires a usable preview; preview is {job['preview_status']}"
                )
            manifest = self.service.catalog.get(asset_id)
            if manifest is None:
                raise FileNotFoundError("AI job references a missing asset")
            preview = cache_paths(self.service, manifest)["preview"]
            if not preview.is_file():
                raise RuntimeError("Preview is marked ready but its cache file is missing")

            face_jpeg = prepare_jpeg(preview, self.service.settings.ai_face_max_image_side)
            vlm_jpeg = prepare_jpeg(preview, self.service.settings.ai_vlm_max_image_side)
            # The stages are deliberately serialized: AdaFace finishes its short
            # CUDA batch before Ollama starts the much heavier VLM inference.
            faces = self.faces.analyze(face_jpeg)
            semantic, model_digest, metrics = analyze_semantics(self.service.settings, vlm_jpeg)
            run_id = str(uuid4())
            created_at = datetime.now(UTC).isoformat()
            public_result = {
                **semantic.document(),
                "faceCount": len(faces),
            }
            artifact = {
                "schemaVersion": 1,
                "runId": run_id,
                "libraryId": str(manifest.library_id),
                "assetId": asset_id,
                "analysisType": ANALYSIS_TYPE,
                "inputSha256": manifest.primary.sha256,
                "pipelineVersion": PIPELINE_VERSION,
                "createdAt": created_at,
                "models": {
                    "semantic": {
                        "name": self.service.settings.ai_model,
                        "digest": model_digest,
                    },
                    "faceDetector": "yunet-2023mar",
                    "faceEmbedding": {
                        **ADAFACE_IDENTITY,
                        "runtime": self.faces.model_version,
                    },
                },
                "semantic": semantic.document(),
                "faces": faces,
                "metrics": metrics,
            }
            object_key = f"analysis/{asset_id}/{PIPELINE_VERSION}/{run_id}.json"
            self.service.storage.put_json(object_key, artifact)
            completed = self.service.catalog.complete_ai_analysis(
                asset_id=asset_id,
                run_id=run_id,
                model_name=self.service.settings.ai_model,
                model_version=model_digest,
                pipeline_version=PIPELINE_VERSION,
                input_hash=manifest.primary.sha256,
                object_key=object_key,
                result=public_result,
                searchable=searchable_text(semantic),
                detected_faces=faces,
                match_threshold=self.service.settings.face_match_threshold,
                created_at=created_at,
            )
            return {
                "jobType": "analysis",
                "assetId": asset_id,
                "status": "ready",
                "runId": run_id,
                **completed,
            }
        except Exception as error:
            try:
                self.service.catalog.finish_ai_job(asset_id, "failed", str(error))
            except Exception:
                pass
            return {
                "jobType": "analysis",
                "assetId": asset_id,
                "status": "failed",
                "error": str(error),
            }


def run(service: Service, once: bool = False):
    worker = AIWorker(service)
    if once:
        return worker.run_once() or {"status": "idle"}
    while True:
        result = worker.run_once()
        if result:
            print(json.dumps(result), flush=True)
        else:
            time.sleep(2)
