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
    choose_reusable_source_detailed,
    extract_semantic,
)
from photo_server.service import Service
from photo_server.worker import cache_paths, generate


class AIWorker:
    def __init__(self, service: Service):
        self.service = service
        self._faces = None
        # Wall-clock duration of the most recent real VLM call on this worker.
        # Reused frames report it as the estimated VLM time avoided.
        self._last_vlm_seconds: float | None = None

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
        stage = "setup"
        rejections: dict[str, int] = {}
        policy_evaluated = False
        try:
            if job["preview_status"] != "ready":
                raise RuntimeError(
                    f"AI analysis requires a usable preview; preview is {job['preview_status']}"
                )
            manifest = self.service.catalog.get(asset_id)
            if manifest is None:
                raise FileNotFoundError("AI job references a missing asset")
            preview = cache_paths(self.service, manifest)["preview"]
            regenerate_preview = False
            if not preview.is_file():
                # The cache is disposable (LRU eviction or a wiped volume can
                # remove a "ready" set at any time), so regenerate it from the
                # immutable original instead of failing the job. A "ready"
                # preview file is the only input this pipeline needs; if it
                # cannot be rebuilt, fall through to the same "unavailable"
                # error the preview pipeline itself records.
                regenerate_preview = True
                if not generate(self.service, manifest):
                    self.service.catalog.finish_job(
                        asset_id, "unavailable", "Preview could not be regenerated"
                    )
                    raise RuntimeError(
                        "Preview is marked ready but regeneration is unavailable"
                    )

            face_jpeg = prepare_jpeg(preview, self.service.settings.ai_face_max_image_side)
            vlm_jpeg = prepare_jpeg(preview, self.service.settings.ai_vlm_max_image_side)
            # Fingerprint computation and persistence are part of the analysis
            # pipeline: a failure here fails the AI job normally.
            stage = "fingerprint"
            fingerprint = compute_fingerprint(vlm_jpeg)
            if self.service.catalog.get_fingerprint(asset_id, BURST_HASH_VERSION) is None:
                self.service.catalog.upsert_fingerprint(asset_id, fingerprint)
            model_digest = resolve_model_digest(self.service.settings, self.service.settings.ai_model)
            stage = "semantic"
            decision, source, source_run_id, rejections, policy_evaluated = self._semantic_reuse(
                manifest, fingerprint, vlm_jpeg, model_digest, force_full
            )
            # The stages are deliberately serialized: AdaFace finishes its short
            # CUDA batch before Ollama starts the much heavier VLM inference.
            stage = "face"
            faces = self.faces.analyze(face_jpeg)
            reuse_mode = self.service.settings.ai_semantic_reuse_mode
            matched_source = source
            matched_source_run_id = source_run_id
            stage = "semantic"
            if reuse_mode == "on" and not force_full and decision.accepted and source is not None:
                # The semantic description is inherited from the verified
                # near-duplicate; face observations and provenance stay
                # target-specific and are never inherited.
                semantic = source.semantic
                metrics = {}
                semantic_origin = "reused"
                reuse_policy_version = REUSE_POLICY_VERSION
                similarity = decision.similarity
            else:
                vlm_started = time.monotonic()
                semantic, model_digest, metrics = analyze_semantics(self.service.settings, vlm_jpeg)
                self._last_vlm_seconds = time.monotonic() - vlm_started
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
            if regenerate_preview:
                # Operational signal: this job rebuilt an evicted or wiped preview.
                result["previewRegenerated"] = True
            if reuse_mode == "observe" and decision.accepted and matched_source is not None:
                # Observe mode reports the match but still invoked the VLM.
                result["reuseMatch"] = {
                    "assetId": matched_source.asset_id,
                    "runId": matched_source_run_id,
                }
            result["counters"] = self._counters(
                semantic_origin, force_full, rejections, policy_evaluated
            )
            return result
        except Exception as error:
            try:
                self.service.catalog.finish_ai_job(asset_id, "failed", str(error))
            except Exception:
                pass
            result = {
                "jobType": "analysis",
                "assetId": asset_id,
                "status": "failed",
                "error": str(error),
                "stage": stage,
            }
            result["counters"] = self._counters(
                None, force_full, rejections, policy_evaluated, failed_stage=stage
            )
            return result

    def _counters(
        self,
        semantic_origin: str | None,
        force_full: bool,
        rejections: dict[str, int],
        policy_evaluated: bool,
        failed_stage: str | None = None,
    ) -> dict:
        """Operational counters for the rollout (see docs/rollout-semantic-reuse.md).

        Emitted on every analysis result so the JSON log lines can be aggregated:
        - ``semanticComputed`` / ``semanticReused``: analyses computed vs reused;
        - ``rejectionsByGate``: candidates rejected by each policy gate;
        - ``forcedFull``: the job requested a full analysis;
        - ``wouldHaveReused``: the policy would have reused, but the job was
          forced full (only known when the policy was still evaluated);
        - ``vlmTimeAvoided``: wall-clock seconds of the last real VLM call,
          reported on reused runs as the estimated VLM time avoided;
        - ``stageFailures``: failures split by setup/fingerprint/face/semantic.
        """
        counters: dict = {
            "semanticComputed": 1 if semantic_origin == "computed" else 0,
            "semanticReused": 1 if semantic_origin == "reused" else 0,
            "forcedFull": 1 if force_full else 0,
            "wouldHaveReused": (
                1 if (force_full and policy_evaluated and semantic_origin == "computed") else 0
            ),
            "vlmTimeAvoided": (
                round(self._last_vlm_seconds, 4)
                if semantic_origin == "reused" and self._last_vlm_seconds is not None
                else None
            ),
            "rejectionsByGate": dict(rejections),
            "stageFailures": {
                "setup": 0,
                "fingerprint": 0,
                "face": 0,
                "semantic": 0,
            },
        }
        if failed_stage is not None:
            counters["stageFailures"][failed_stage] = 1
        return counters

    def _semantic_reuse(
        self,
        manifest,
        fingerprint,
        vlm_jpeg: bytes,
        model_digest: str,
        force_full: bool,
    ) -> tuple[ReuseDecision, ReuseSource | None, str | None, dict[str, int], bool]:
        """Resolve a reusable semantic run for the target, if the policy allows it.

        Returns ``(decision, source, source_run_id, rejections, evaluated)``
        where ``rejections`` counts candidates rejected by each policy gate and
        ``evaluated`` records whether the policy was actually evaluated (so a
        forced-full job can still report ``wouldHaveReused``).

        The policy is evaluated whenever the mode is not ``off`` and the current
        model digest is known, even for forced-full jobs, so the rollout
        counters can report what reuse would have done. A forced-full job
        still computes; the caller must not apply the decision. A candidate
        lookup failure is logged as an optimization failure and falls back to
        full VLM analysis; it never fails the job.
        """
        settings = self.service.settings
        mode = settings.ai_semantic_reuse_mode
        if mode == "off" or model_digest == UNKNOWN_DIGEST:
            return (
                ReuseDecision(accepted=False, reason=None, similarity=None),
                None,
                None,
                {},
                False,
            )
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
            chosen, rejections = choose_reusable_source_detailed(target, sources, settings)
            if chosen is None:
                return (
                    ReuseDecision(accepted=False, reason=None, similarity=None),
                    None,
                    None,
                    rejections,
                    True,
                )
            source, decision = chosen
            return decision, source, source_run_ids.get(source.asset_id), rejections, True
        except Exception as error:
            # Candidate lookup failure falls back to full VLM analysis only
            # when the database remains healthy enough to publish the result.
            print(
                f"semantic reuse optimization failed for {manifest.asset_id}: {error}",
                file=sys.stderr,
            )
            return (
                ReuseDecision(accepted=False, reason=None, similarity=None),
                None,
                None,
                {},
                False,
            )

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
            # The source preview is not cached locally (e.g. evicted by the LRU
            # loop or lost to a volume wipe); skip it rather than risk an
            # incorrect pixel-similarity decision. The target degrades to a full
            # analysis, which the miss-path regeneration above keeps correct.
            print(
                json.dumps(
                    {
                        "status": "reuse_candidate_skipped",
                        "reason": "preview_cache_miss",
                        "assetId": candidate.asset_id,
                    }
                ),
                flush=True,
            )
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
