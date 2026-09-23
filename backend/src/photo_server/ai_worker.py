"""Background dispatcher for photo analysis (Phase 3A: AI is optional).

Both AI services are remote (Phases 1/2B of ``docs/ai-service-split-plan.md``):
the VLM through the OpenAI-standard client in ``analysis`` and the face
stage through the face-service client in ``face_client``. The worker holds
no learned models — it prepares inputs, calls the services, and stores
results (matching against stored embeddings stays in the catalog).

Phase 3A makes AI an optional configuration:

- **Not configured** (``PHOTO_AI_BASE_URL`` or ``PHOTO_FACE_SERVICE_URL``
  empty): the dispatcher never claims; analysis jobs accumulate as
  ``pending`` (they are created at import) and the backlog drains once the
  URLs are set. The loop sleeps a fixed 10 s per iteration and emits a slow
  heartbeat (at most every 60 s) with the backlog size.
- **Configured**: the dispatcher probes both services (face-service
  ``GET /health``, VLM ``GET /models``, 5 s timeouts) and caches the result
  30 s per process (Q4). Jobs are claimed only when both probes are healthy.
  While a service is unhealthy the dispatcher claims nothing and sleeps with
  a 10 s backoff doubling to 120 s; the probes never raise out of the loop.
- **In-flight bound**: the configured AI dispatcher count limits assets in
  flight; within each asset, independent face and semantic stages run in
  parallel and join before publication.
- **Failure classification mid-job**: the service-unavailable classes
  (``AIServiceUnavailableError`` from the VLM client,
  ``FaceServiceUnavailable`` from the face client: connection errors,
  timeouts, 429 with ``Retry-After``, 502-504) return the job to
  ``pending`` — the backlog survives the outage and the same job is
  claimed again when the services recover. Every other failure (4xx,
  model/decode errors, invalid VLM JSON) fails the job with its stage,
  exactly as before; recovery is the existing manual requeue.
"""

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import uuid4

import httpx
from pydantic import ValidationError

from photo_server.analysis import (
    ANALYSIS_TYPE,
    PIPELINE_VERSION,
    AIServiceUnavailableError,
    SemanticAnalysis,
    analyze_semantics,
    jpeg_dimensions,
    prepare_jpeg,
    resolve_model_digest,
    searchable_text,
)
from photo_server.browsing import camera_time
from photo_server.ai_pipeline import independent
from photo_server.face_client import ADAFACE_IDENTITY, FaceServiceUnavailable, RemoteFaceAnalyzer
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

# Gate / dispatcher tuning (Phase 3A of the split plan).
_PROBE_TIMEOUT_SECONDS = 5.0  # per-service probe timeout
_PROBE_CACHE_SECONDS = 30.0  # probe result cache (Q4)
_IDLE_SLEEP_SECONDS = 10.0  # not configured: fixed cadence, no claim
_HEARTBEAT_SECONDS = 60.0  # at most one slow heartbeat per minute
_BACKOFF_INITIAL_SECONDS = 10.0  # unhealthy: 10 s doubling...
_BACKOFF_MAX_SECONDS = 120.0  # ...to 120 s
_NO_JOBS_SLEEP_SECONDS = 2.0  # ready but no backlog: today's idle cadence


class _StopRunning(Exception):
    """Sentinel that ends the dispatcher loop cleanly (test/ops seam)."""


class AIWorker:
    def __init__(self, service: Service):
        self.service = service
        # The face stage runs in the standalone face-service; this client is
        # the only face code the server keeps (Phase 2B).
        self._faces = RemoteFaceAnalyzer(service.settings)
        # Wall-clock duration of the most recent real VLM call on this worker.
        # Reused frames report it as the estimated VLM time avoided.
        self._last_vlm_seconds: float | None = None
        # (monotonic timestamp, state, detail) of the last probe round; one
        # AIWorker per process, so per-instance is per-process (Q4).
        self._probe_cache: tuple[float, str, str] | None = None

    @property
    def faces(self) -> RemoteFaceAnalyzer:
        return self._faces

    # --- Service gate (Phase 3A) ------------------------------------------

    def _probe_vlm(self) -> bool:
        """``GET {ai_base_url}/models`` on a 5 s timeout.

        Any failure — connection error, timeout, non-2xx (a bad API key
        answers 401) — makes the VLM unavailable, so a misconfigured
        endpoint paces (no claims) instead of burning the backlog into
        ``failed``. The probe never raises.
        """
        settings = self.service.settings
        if not settings.ai_base_url:
            return False
        try:
            headers = {}
            if settings.ai_api_key:
                headers["Authorization"] = f"Bearer {settings.ai_api_key}"
            with httpx.Client(timeout=_PROBE_TIMEOUT_SECONDS) as client:
                response = client.get(f"{settings.ai_base_url.rstrip('/')}/models", headers=headers)
            return 200 <= response.status_code < 300
        except Exception:
            return False

    def _probe_face(self) -> bool:
        """The face-service ``GET /health`` (5 s, identity-checked by the
        client). Any failure — including an embedding-identity mismatch,
        which the client reports as unavailable — makes the face stage
        unavailable. The probe never raises."""
        if not self.service.settings.face_service_url:
            return False
        try:
            self._faces.health()
            return True
        except Exception:
            return False

    def _gate(self) -> tuple[str, str]:
        """Classify the AI service state and return ``(state, detail)``.

        States: ``not-configured`` (a URL is empty), ``ready`` (both probes
        pass), ``face-only`` (only face-service is healthy), ``vlm-only``
        (only VLM is healthy), or unavailable states. Probe results are
        cached for 30 s per process (Q4).
        """
        settings = self.service.settings
        if not settings.ai_base_url or not settings.face_service_url:
            return "not-configured", "PHOTO_AI_BASE_URL or PHOTO_FACE_SERVICE_URL is empty"
        now = time.monotonic()
        if self._probe_cache is not None and now - self._probe_cache[0] < _PROBE_CACHE_SECONDS:
            return self._probe_cache[1], self._probe_cache[2]
        face_ok = self._probe_face()
        vlm_ok = self._probe_vlm()
        if face_ok and vlm_ok:
            state, detail = "ready", "both services healthy"
        elif not face_ok and not vlm_ok:
            state, detail = "ai-services-unavailable", "both service probes failed"
        elif not face_ok:
            state, detail = "vlm-only", "VLM healthy; face-service /health probe failed"
        else:
            state, detail = "face-only", "face-service healthy; VLM /models probe failed"
        self._probe_cache = (now, state, detail)
        return state, detail

    def _pending_analysis_count(self) -> int | None:
        """The backlog size for the heartbeat; ``None`` if it cannot be read
        (the heartbeat must never take the loop down with it)."""
        try:
            return int(self.service.catalog.queue_counts()["analysisPending"] or 0)
        except Exception:
            return None

    # --- One pass -----------------------------------------------------------

    def _claim(self) -> dict | None:
        return self.service.catalog.claim_ai_job()

    def _claim_face_stage(self) -> dict | None:
        return self.service.catalog.claim_ai_face_stage()

    def _execute_face_stage(self, job: dict) -> dict:
        asset_id = job["asset_id"]
        try:
            manifest = self.service.catalog.get(asset_id)
            if manifest is None:
                raise FileNotFoundError("Face stage references a missing asset")
            if job["preview_status"] != "ready":
                raise RuntimeError(
                    f"Face analysis requires a usable preview; preview is {job['preview_status']}"
                )
            faces = self._load_face_stage(asset_id, manifest.primary.sha256)
            if faces is None:
                preview = cache_paths(self.service, manifest)["preview"]
                if not preview.is_file() and not generate(self.service, manifest):
                    raise RuntimeError("Preview could not be regenerated for face analysis")
                face_jpeg = prepare_jpeg(preview, self.service.settings.ai_face_max_image_side)
                faces = self.faces.analyze(face_jpeg)
                self._persist_face_stage(asset_id, manifest.primary.sha256, faces)
            self.service.catalog.finish_ai_face_stage(asset_id, "ready")
            return {
                "jobType": "analysis-stage",
                "stage": "face",
                "assetId": asset_id,
                "status": "ready",
                "faceCount": len(faces),
            }
        except FaceServiceUnavailable as error:
            self.service.catalog.finish_ai_face_stage(asset_id, "pending", str(error))
            return {
                "jobType": "analysis-stage",
                "stage": "face",
                "assetId": asset_id,
                "status": "requeued",
                "error": str(error),
            }
        except Exception as error:
            self.service.catalog.finish_ai_face_stage(asset_id, "failed", str(error))
            return {
                "jobType": "analysis-stage",
                "stage": "face",
                "assetId": asset_id,
                "status": "failed",
                "error": str(error),
            }

    def _face_stage_key(self, asset_id: str) -> str:
        return f"analysis-stages/{asset_id}/{PIPELINE_VERSION}/face.json"

    def _load_face_stage(self, asset_id: str, input_sha256: str) -> list[dict] | None:
        """Load a durable face result from the current immutable input."""
        key = self._face_stage_key(asset_id)
        if self.service.storage.head(key) is None:
            return None
        try:
            artifact = self.service.storage.get_json(key)
        except Exception:
            # A partial/evicted stage object is not a reason to fail analysis;
            # the face stage can be recomputed.
            return None
        if (
            artifact.get("assetId") != asset_id
            or artifact.get("inputSha256") != input_sha256
            or artifact.get("pipelineVersion") != PIPELINE_VERSION
            or not isinstance(artifact.get("faces"), list)
        ):
            return None
        return artifact["faces"]

    def _persist_face_stage(self, asset_id: str, input_sha256: str, faces: list[dict]) -> None:
        """Persist face output before waiting for semantic inference."""
        self.service.storage.put_json(
            self._face_stage_key(asset_id),
            {
                "schemaVersion": 1,
                "assetId": asset_id,
                "inputSha256": input_sha256,
                "pipelineVersion": PIPELINE_VERSION,
                "stage": "face",
                "faces": faces,
            },
        )

    def _semantic_stage_key(self, asset_id: str) -> str:
        return f"analysis-stages/{asset_id}/{PIPELINE_VERSION}/semantic.json"

    def _load_semantic_stage(self, asset_id: str, input_sha256: str) -> tuple[SemanticAnalysis, str, dict] | None:
        key = self._semantic_stage_key(asset_id)
        if self.service.storage.head(key) is None:
            return None
        try:
            artifact = self.service.storage.get_json(key)
            if (
                artifact.get("assetId") != asset_id
                or artifact.get("inputSha256") != input_sha256
                or artifact.get("pipelineVersion") != PIPELINE_VERSION
            ):
                return None
            return (
                SemanticAnalysis.model_validate(artifact["semantic"]),
                str(artifact["modelDigest"]),
                artifact.get("metrics") or {},
            )
        except Exception:
            return None

    def _persist_semantic_stage(
        self, asset_id: str, input_sha256: str, semantic: SemanticAnalysis,
        model_digest: str, metrics: dict,
    ) -> None:
        self.service.storage.put_json(
            self._semantic_stage_key(asset_id),
            {
                "schemaVersion": 1,
                "assetId": asset_id,
                "inputSha256": input_sha256,
                "pipelineVersion": PIPELINE_VERSION,
                "stage": "semantic",
                "semantic": semantic.document(),
                "modelDigest": model_digest,
                "metrics": metrics,
            },
        )

    def _execute_semantic_stage(self, job: dict) -> dict:
        asset_id = job["asset_id"]
        try:
            manifest = self.service.catalog.get(asset_id)
            if manifest is None:
                raise FileNotFoundError("Semantic stage references a missing asset")
            if job["preview_status"] != "ready":
                raise RuntimeError(
                    f"Semantic analysis requires a usable preview; preview is {job['preview_status']}"
                )
            cached = self._load_semantic_stage(asset_id, manifest.primary.sha256)
            if cached is None:
                preview = cache_paths(self.service, manifest)["preview"]
                if not preview.is_file() and not generate(self.service, manifest):
                    raise RuntimeError("Preview could not be regenerated for semantic analysis")
                jpeg = prepare_jpeg(preview, self.service.settings.ai_vlm_max_image_side)
                semantic, model_digest, metrics = analyze_semantics(self.service.settings, jpeg)
                self._persist_semantic_stage(
                    asset_id, manifest.primary.sha256, semantic, model_digest, metrics
                )
            self.service.catalog.finish_ai_stage(asset_id, "semantic", "ready")
            return {"jobType": "analysis-stage", "stage": "semantic", "assetId": asset_id, "status": "ready"}
        except AIServiceUnavailableError as error:
            self.service.catalog.finish_ai_stage(asset_id, "semantic", "pending", str(error))
            return {"jobType": "analysis-stage", "stage": "semantic", "assetId": asset_id, "status": "requeued", "error": str(error)}
        except Exception as error:
            self.service.catalog.finish_ai_stage(asset_id, "semantic", "failed", str(error))
            return {"jobType": "analysis-stage", "stage": "semantic", "assetId": asset_id, "status": "failed", "error": str(error)}

    def run_once(self) -> dict | None:
        """One dispatcher pass: gate, then claim and execute when ready.

        Returns ``None`` when the services are healthy but nothing is
        claimable, an ``idle`` result (never claiming) when the gate blocks,
        and the analysis result otherwise. ``--once`` maps ``None`` to
        ``{"status": "idle"}``; an unconfigured worker reports
        ``{"status": "idle", "reason": "ai-not-configured"}``.
        """
        state, _ = self._gate()
        if state == "face-only":
            job = self._claim_face_stage()
            return self._execute_face_stage(job) if job is not None else None
        if state == "vlm-only":
            job = self.service.catalog.claim_ai_semantic_stage()
            return self._execute_semantic_stage(job) if job is not None else None
        if state != "ready":
            return {
                "status": "idle",
                "reason": "ai-not-configured" if state == "not-configured" else state,
            }
        job = self._claim()
        if job is None:
            return None
        return self._execute(job)

    def _execute(self, job: dict) -> dict:
        asset_id = job["asset_id"]
        force_full = bool(job.get("force_full", False))
        stage = "setup"
        pipeline_started = time.perf_counter()
        stage_started = pipeline_started
        current_timing_stage = "setup"
        stage_durations_ms: dict[str, float] = {}
        rejections: dict[str, int] = {}
        policy_evaluated = False

        def begin_timing(next_stage: str):
            nonlocal stage_started, current_timing_stage
            stage_durations_ms[current_timing_stage] = round(
                (time.perf_counter() - stage_started) * 1000, 1
            )
            stage_started = time.perf_counter()
            current_timing_stage = next_stage

        def timing_snapshot() -> dict[str, float]:
            if current_timing_stage not in stage_durations_ms:
                stage_durations_ms[current_timing_stage] = round(
                    (time.perf_counter() - stage_started) * 1000, 1
                )
            stage_durations_ms["total"] = round(
                (time.perf_counter() - pipeline_started) * 1000, 1
            )
            return dict(stage_durations_ms)

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
            face_width, face_height = jpeg_dimensions(face_jpeg)
            vlm_width, vlm_height = jpeg_dimensions(vlm_jpeg)
            # Fingerprint computation and persistence are part of the analysis
            # pipeline: a failure here fails the AI job normally.
            begin_timing("fingerprint")
            stage = "fingerprint"
            fingerprint = compute_fingerprint(vlm_jpeg)
            if self.service.catalog.get_fingerprint(asset_id, BURST_HASH_VERSION) is None:
                self.service.catalog.upsert_fingerprint(asset_id, fingerprint)
            model_digest = resolve_model_digest(self.service.settings, self.service.settings.ai_model)
            begin_timing("reuse")
            stage = "semantic"
            decision, source, source_run_id, rejections, policy_evaluated = self._semantic_reuse(
                manifest, fingerprint, vlm_jpeg, model_digest, force_full
            )
            reuse_mode = self.service.settings.ai_semantic_reuse_mode
            matched_source = source
            matched_source_run_id = source_run_id
            reuse_semantic = (
                reuse_mode == "on" and not force_full and decision.accepted and source is not None
            )
            persisted_semantic = (
                None
                if reuse_semantic
                else self._load_semantic_stage(asset_id, manifest.primary.sha256)
            )
            # Face and semantic are independent stages today. Run them in
            # parallel; the publish stage below remains the join point. This
            # is intentionally expressed as a dependency decision rather than
            # a face/semantic special case so future stages can add
            # prerequisites without changing the scheduler shape.
            begin_timing("face")
            stage = "face"
            persisted_faces = self._load_face_stage(asset_id, manifest.primary.sha256)
            if persisted_faces is not None:
                faces = persisted_faces
                face_elapsed = 0.0
            elif persisted_semantic is not None:
                face_started = time.perf_counter()
                faces = self.faces.analyze(face_jpeg)
                face_elapsed = (time.perf_counter() - face_started) * 1000
                self._persist_face_stage(asset_id, manifest.primary.sha256, faces)
            elif reuse_semantic:
                face_started = time.perf_counter()
                faces = self.faces.analyze(face_jpeg)
                face_elapsed = (time.perf_counter() - face_started) * 1000
                self._persist_face_stage(asset_id, manifest.primary.sha256, faces)
            elif not independent("face", "semantic", {"preview"}):
                raise RuntimeError("AI pipeline dependency graph rejected face/semantic stages")

            if reuse_semantic:
                # The semantic description is inherited from the verified
                # near-duplicate; face observations and provenance stay
                # target-specific and are never inherited.
                semantic = source.semantic
                metrics = {}
                semantic_origin = "reused"
                reuse_policy_version = REUSE_POLICY_VERSION
                similarity = decision.similarity
            elif persisted_faces is not None or persisted_semantic is not None:
                semantic_started = time.perf_counter()
                if persisted_semantic is not None:
                    semantic, model_digest, metrics = persisted_semantic
                    semantic_elapsed = 0.0
                else:
                    semantic, model_digest, metrics = analyze_semantics(
                        self.service.settings, vlm_jpeg
                    )
                    semantic_elapsed = (time.perf_counter() - semantic_started) * 1000
                    self._persist_semantic_stage(
                        asset_id, manifest.primary.sha256, semantic, model_digest, metrics
                    )
                    self._last_vlm_seconds = semantic_elapsed / 1000
                semantic_origin = "computed"
                source = None
                source_run_id = None
                reuse_policy_version = None
                similarity = None
            else:
                def run_face_stage():
                    started = time.perf_counter()
                    result = self.faces.analyze(face_jpeg)
                    return result, (time.perf_counter() - started) * 1000

                def run_semantic_stage():
                    started = time.perf_counter()
                    result = analyze_semantics(self.service.settings, vlm_jpeg)
                    return result, (time.perf_counter() - started) * 1000

                with ThreadPoolExecutor(max_workers=2, thread_name_prefix="photo-ai-stage") as pool:
                    face_future = pool.submit(run_face_stage)
                    semantic_future = pool.submit(run_semantic_stage)
                    faces, face_elapsed = face_future.result()
                    # Make the face result durable as soon as that stage
                    # returns. Semantic inference may still be running.
                    self._persist_face_stage(asset_id, manifest.primary.sha256, faces)
                    (semantic, model_digest, metrics), semantic_elapsed = semantic_future.result()
                self._persist_semantic_stage(
                    asset_id, manifest.primary.sha256, semantic, model_digest, metrics
                )
                self._last_vlm_seconds = semantic_elapsed / 1000
                semantic_origin = "computed"
                source = None
                source_run_id = None
                reuse_policy_version = None
                similarity = None
            stage_durations_ms["face"] = round(face_elapsed, 1)
            if reuse_semantic:
                stage_durations_ms["semantic"] = 0.0
            else:
                stage_durations_ms["semantic"] = round(semantic_elapsed, 1)
            stage_started = time.perf_counter()
            current_timing_stage = "publish"
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
                semantic_origin,
                force_full,
                rejections,
                policy_evaluated,
                stage_durations_ms=timing_snapshot(),
                input_metrics={
                    "face": {"width": face_width, "height": face_height, "bytes": len(face_jpeg)},
                    "vlm": {"width": vlm_width, "height": vlm_height, "bytes": len(vlm_jpeg)},
                },
            )
            return result
        except (AIServiceUnavailableError, FaceServiceUnavailable) as error:
            # A service that is unreachable mid-job paces, it does not
            # punish: the job goes back to ``pending`` (the attempt was
            # already counted at the claim, the lease is cleared) and is
            # claimed again when the service recovers — this is what
            # "the backlog is pushed through the services" means
            # operationally. ``stageFailures`` still count the attempt.
            try:
                self.service.catalog.finish_ai_job(asset_id, "pending", str(error))
                status = "requeued"
            except Exception:
                # The requeue write itself failed (the database is down):
                # report it and let the claim lease expire for reclamation.
                status = "failed"
            result = {
                "jobType": "analysis",
                "assetId": asset_id,
                "status": status,
                "reason": "service-unavailable",
                "error": str(error),
                "stage": stage,
            }
            result["counters"] = self._counters(
                None,
                force_full,
                rejections,
                policy_evaluated,
                failed_stage=stage,
                stage_durations_ms=timing_snapshot(),
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
                None,
                force_full,
                rejections,
                policy_evaluated,
                failed_stage=stage,
                stage_durations_ms=timing_snapshot(),
            )
            return result

    def _counters(
        self,
        semantic_origin: str | None,
        force_full: bool,
        rejections: dict[str, int],
        policy_evaluated: bool,
        failed_stage: str | None = None,
        stage_durations_ms: dict[str, float] | None = None,
        input_metrics: dict | None = None,
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
        - ``stageDurationsMs``: elapsed time by pipeline stage plus ``total``.
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
            "stageDurationsMs": dict(stage_durations_ms or {}),
            "inputMetrics": dict(input_metrics or {}),
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


def _log_state_transition(previous: str | None, state: str, detail: str) -> None:
    """One JSON line per gate-state change (it never repeats while the state
    holds; the heartbeat keeps a long outage visible)."""
    if state == "not-configured":
        message = "AI services not configured; the worker idles until both service URLs are set"
    elif state == "ready":
        message = "AI services healthy; claiming analysis jobs again"
    elif state == "face-only":
        message = "VLM unavailable; draining independent face stages"
    elif state == "vlm-only":
        message = "Face service unavailable; draining independent semantic stages"
    else:
        message = f"AI service unavailable ({detail}); not claiming, backing off"
    print(
        json.dumps(
            {
                "status": "state-transition",
                "previous": previous,
                "state": state,
                "detail": detail,
                "message": message,
            }
        ),
        flush=True,
    )


def _run_dispatcher(service: Service) -> None:
    """Run the AI dispatcher.

    ``once`` executes a single pass (gate, then claim and execute when
    ready) and returns its result; ``None`` becomes ``{"status": "idle"}``
    and an unconfigured worker reports
    ``{"status": "idle", "reason": "ai-not-configured"}``.

    The loop claims and executes one analysis at a time. The job row's 1800 s
    lease makes a crashed in-flight job reclaimable.
    Not configured: fixed 10 s cadence, no claim, slow heartbeat.
    Configured but a service unhealthy: 10 s backoff doubling to 120 s, no
    claim, transition-logged. The probes never raise out of the loop.
    """
    worker = AIWorker(service)

    last_state: str | None = None
    backoff = _BACKOFF_INITIAL_SECONDS
    last_heartbeat = 0.0
    try:
        while True:
            state, detail = worker._gate()
            if state == "face-only":
                if state != last_state:
                    _log_state_transition(last_state, state, detail)
                    backoff = _BACKOFF_INITIAL_SECONDS
                    last_heartbeat = 0.0
                last_state = state
                job = worker._claim_face_stage()
                if job is None:
                    time.sleep(_NO_JOBS_SLEEP_SECONDS)
                    continue
                print(json.dumps(worker._execute_face_stage(job)), flush=True)
                continue

            if state == "vlm-only":
                if state != last_state:
                    _log_state_transition(last_state, state, detail)
                    backoff = _BACKOFF_INITIAL_SECONDS
                    last_heartbeat = 0.0
                last_state = state
                job = worker.service.catalog.claim_ai_semantic_stage()
                if job is None:
                    time.sleep(_NO_JOBS_SLEEP_SECONDS)
                    continue
                print(json.dumps(worker._execute_semantic_stage(job)), flush=True)
                continue

            if state != "ready":
                if state != last_state:
                    _log_state_transition(last_state, state, detail)
                    backoff = _BACKOFF_INITIAL_SECONDS
                    last_heartbeat = 0.0
                now = time.monotonic()
                if now - last_heartbeat >= _HEARTBEAT_SECONDS:
                    pending = worker._pending_analysis_count()
                    if state == "not-configured":
                        message = "AI services not configured"
                    else:
                        message = f"AI services unavailable ({detail})"
                    if pending is not None:
                        message = f"{message}; {pending} analysis jobs waiting"
                    print(
                        json.dumps(
                            {
                                "status": "heartbeat",
                                "state": state,
                                "pendingAnalysis": pending,
                                "message": message,
                            }
                        ),
                        flush=True,
                    )
                    last_heartbeat = now
                time.sleep(_IDLE_SLEEP_SECONDS if state == "not-configured" else backoff)
                if state != "not-configured":
                    backoff = min(backoff * 2, _BACKOFF_MAX_SECONDS)
                last_state = state
                continue

            if last_state is not None and last_state != "ready":
                _log_state_transition(last_state, "ready", detail)
            last_state = "ready"

            job = worker._claim()
            if job is None:
                time.sleep(_NO_JOBS_SLEEP_SECONDS)
                continue
            try:
                result = worker._execute(job)
            except Exception as error:
                # An execution must never be able to kill the dispatcher; the
                # job's lease expires for reclamation.
                result = {
                    "jobType": "analysis",
                    "status": "failed",
                    "stage": "setup",
                    "error": f"analysis execution crashed: {error}",
                }
            print(json.dumps(result), flush=True)
    except _StopRunning:
        pass


def run(service: Service, once: bool = False) -> dict | None:
    """Run one pass or one/more independent AI dispatch loops.

    Multiple loops use the database lease/claim mechanism for coordination.
    They are intentionally opt-in because they make both face and semantic
    requests concurrent; the VLM's ``max-num-seqs`` should be configured to
    at least the same value.
    """
    if once:
        worker = AIWorker(service)
        result = worker.run_once()
        return result if result is not None else {"status": "idle"}
    if service.settings.ai_workers == 1:
        _run_dispatcher(service)
        return None
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(
        max_workers=service.settings.ai_workers, thread_name_prefix="photo-ai"
    ) as executor:
        futures = [
            executor.submit(_run_dispatcher, service)
            for _ in range(service.settings.ai_workers)
        ]
        for future in futures:
            future.result()
    return None
