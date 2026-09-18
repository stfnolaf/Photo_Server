"""Single-concurrency background worker for local CUDA photo analysis."""

import json
import sys
import time
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import ValidationError

from photo_server.analysis import (
    ADAFACE_IDENTITY,
    ANALYSIS_TYPE,
    PIPELINE_VERSION,
    AdaFaceAnalyzer,
    analyze_semantics,
    prepare_jpeg,
    resolve_model_digest,
    searchable_text,
)
from photo_server.browsing import camera_time
from photo_server.fingerprints import BURST_HASH_VERSION, compute_fingerprint
from photo_server.reuse import (
    REUSE_POLICY_VERSION,
    UNKNOWN_DIGEST,
    ReuseAsset,
    ReuseDecision,
    ReuseSource,
    choose_reusable_source,
    extract_semantic,
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
        force_full = bool(job.get("force_full", False))
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
            # Fingerprint computation and persistence are part of the analysis
            # pipeline: a failure here fails the AI job normally.
            fingerprint = compute_fingerprint(vlm_jpeg)
            if self.service.catalog.get_fingerprint(asset_id, BURST_HASH_VERSION) is None:
                self.service.catalog.upsert_fingerprint(asset_id, fingerprint)
            model_digest = resolve_model_digest(self.service.settings, self.service.settings.ai_model)
            decision, source, source_run_id = self._semantic_reuse(
                manifest, fingerprint, vlm_jpeg, model_digest, force_full
            )
            # The stages are deliberately serialized: AdaFace finishes its short
            # CUDA batch before Ollama starts the much heavier VLM inference.
            faces = self.faces.analyze(face_jpeg)
            reuse_mode = self.service.settings.ai_semantic_reuse_mode
            matched_source = source
            matched_source_run_id = source_run_id
            if reuse_mode == "on" and decision.accepted and source is not None:
                # The semantic description is inherited from the verified
                # near-duplicate; face observations and provenance stay
                # target-specific and are never inherited.
                semantic = source.semantic
                metrics = {}
                semantic_origin = "reused"
                reuse_policy_version = REUSE_POLICY_VERSION
                similarity = decision.similarity
            else:
                semantic, model_digest, metrics = analyze_semantics(self.service.settings, vlm_jpeg)
                semantic_origin = "computed"
                source = None
                source_run_id = None
                reuse_policy_version = None
                similarity = None
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
                "semanticOrigin": semantic_origin,
            }
            if source is not None:
                artifact["semanticSource"] = {
                    "assetId": source.asset_id,
                    "runId": source_run_id,
                }
                artifact["similarity"] = similarity
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
                semantic_origin=semantic_origin,
                source_run_id=source_run_id,
                reuse_policy_version=reuse_policy_version,
                similarity=similarity,
            )
            result = {
                "jobType": "analysis",
                "assetId": asset_id,
                "status": "ready",
                "runId": run_id,
                "semanticOrigin": semantic_origin,
                **completed,
            }
            if reuse_mode == "observe" and decision.accepted and matched_source is not None:
                # Observe mode reports the match but still invoked the VLM.
                result["reuseMatch"] = {
                    "assetId": matched_source.asset_id,
                    "runId": matched_source_run_id,
                }
            return result
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

    def _semantic_reuse(
        self,
        manifest,
        fingerprint,
        vlm_jpeg: bytes,
        model_digest: str,
        force_full: bool,
    ) -> tuple[ReuseDecision, ReuseSource | None, str | None]:
        """Resolve a reusable semantic run for the target, if the policy allows it.

        Reuse is only attempted when the mode is not ``off``, the job does not
        request a full analysis, and the current model digest is known. A
        candidate lookup failure is logged as an optimization failure and falls
        back to full VLM analysis; it never fails the job.
        """
        settings = self.service.settings
        mode = settings.ai_semantic_reuse_mode
        if mode == "off" or force_full or model_digest == UNKNOWN_DIGEST:
            return ReuseDecision(accepted=False, reason=None, similarity=None), None, None
        target = ReuseAsset(
            asset_id=str(manifest.asset_id),
            fingerprint=fingerprint,
            capture_time=camera_time(manifest.capture_time),
            camera_identity=_camera_identity(manifest.metadata),
            pipeline_version=PIPELINE_VERSION,
            model_name=settings.ai_model,
            model_digest=model_digest,
            preview=vlm_jpeg,
        )
        try:
            candidates = self.service.catalog.find_fingerprint_candidates(
                str(manifest.asset_id), BURST_HASH_VERSION
            )
            sources: list[ReuseSource] = []
            source_run_ids: dict[str, str] = {}
            for candidate in candidates:
                status = self.service.catalog.analysis_status(candidate.asset_id)
                if (
                    status["status"] != "ready"
                    or status["runId"] is None
                    or status["result"] is None
                    or status["modelVersion"] is None
                ):
                    continue
                try:
                    semantic = extract_semantic(status["result"])
                except ValidationError:
                    # Invalid inherited semantic JSON rejects this candidate.
                    continue
                fields = self._load_reuse_source(candidate, settings)
                if fields is None:
                    continue
                # The source's pipeline, model, and digest are the ones
                # recorded when its run was computed; any change to them
                # must never be reused.
                fields["model_digest"] = status["modelVersion"]
                fields["pipeline_version"] = status["pipelineVersion"]
                fields["model_name"] = status["model"]
                source = ReuseAsset(**fields)
                sources.append(ReuseSource(**source.__dict__, semantic=semantic))
                source_run_ids[source.asset_id] = status["runId"]
            chosen = choose_reusable_source(target, sources, settings)
            if chosen is None:
                return ReuseDecision(accepted=False, reason=None, similarity=None), None, None
            source, decision = chosen
            return decision, source, source_run_ids.get(source.asset_id)
        except Exception as error:
            # Candidate lookup failure falls back to full VLM analysis only
            # when the database remains healthy enough to publish the result.
            print(
                f"semantic reuse optimization failed for {manifest.asset_id}: {error}",
                file=sys.stderr,
            )
            return ReuseDecision(accepted=False, reason=None, similarity=None), None, None

    def _load_reuse_source(self, candidate, settings) -> dict | None:
        """Build the ``ReuseAsset`` fields for a candidate, or ``None`` to skip it."""
        manifest = self.service.catalog.get(candidate.asset_id)
        if manifest is None:
            return None
        fingerprint = self.service.catalog.get_fingerprint(
            candidate.asset_id, BURST_HASH_VERSION
        )
        if fingerprint is None:
            return None
        preview = cache_paths(self.service, manifest)["preview"]
        if not preview.is_file():
            # The source preview is not cached locally; skip it rather than
            # risk an incorrect pixel-similarity decision.
            return None
        source_preview = prepare_jpeg(preview, settings.ai_vlm_max_image_side)
        return dict(
            asset_id=candidate.asset_id,
            fingerprint=fingerprint,
            capture_time=camera_time(manifest.capture_time),
            camera_identity=_camera_identity(manifest.metadata),
            pipeline_version=PIPELINE_VERSION,
            model_name=settings.ai_model,
            # Placeholder; the caller replaces it with the digest recorded
            # on the source's stored run.
            model_digest="",
            preview=source_preview,
        )


def _camera_identity(metadata: dict) -> str | None:
    """Render the Make/Model pair as the camera identity used by the policy."""
    parts = [str(metadata[key]) for key in ("Make", "Model") if metadata.get(key)]
    return " ".join(parts) if parts else None


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
