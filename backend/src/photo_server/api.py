import asyncio
import copy
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from io import BytesIO
from typing import Annotated, Literal
from urllib.parse import quote
from uuid import UUID, uuid4, uuid5

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel
from sqlalchemy import text

from photo_server.api_schemas import (
    AlbumOut,
    BatchAbandonedOut,
    BrowsePageOut,
    BurstDetailOut,
    BurstMemberRemovedOut,
    BurstReclusterOut,
    BurstRepresentativeOut,
    CurrentAssetDetailOut,
    CurrentAssetDocOut,
    FaceMoveOut,
    HealthFailureOut,
    HealthOut,
    LivenessOut,
    LoginRequest,
    MutationResultOut,
    Pending202Out,
    PeoplePageOut,
    PersonDetailOut,
    PersonMergeOut,
    PersonRenameOut,
    PreviewStatusOut,
    QueueResultOut,
    ReadinessFailureOut,
    ReadinessOut,
    SessionOut,
    UploadBatchOut,
    UploadBatchRequest,
    UploadFileReceipt,
    UploadQueueStatusOut,
    VerifyOut,
)
from photo_server.app_logging import log_event
from photo_server.auth import auth_scheme, issue_session, origin_allowed, verify_password
from photo_server.browsing import AlbumPatch, BrowseQuery, OperationRequest, UserStatePatch
from photo_server.config import LibraryError, Settings
from photo_server.derivative_identity import derivative_etag, derivative_version
from photo_server.face_client import RemoteFaceAnalyzer
from photo_server.heartbeat import read_heartbeat
from photo_server.metadata import technical_fields
from photo_server.metrics import Metrics, safe_metrics
from photo_server.models import Mutation
from photo_server.reconcile import reconcile_s3_to_postgres
from photo_server.service import Service
from photo_server.state import mutate, mutate_face
from photo_server.uploads import (
    UploadGate,
    abandon_batch,
    cleanup_abandoned_batches,
    create_batch,
    describe_batch,
    list_active_batches,
    receive_file,
    reconcile_ready_upload_batches,
    seal_batch,
)
from photo_server.worker import cache_paths

# In-process throttle for preview_cache.last_accessed_at updates: asset id ->
# monotonic timestamp of the last touch. Bounded by the number of assets
# served per process lifetime; a per-request DB UPDATE would be wasteful.
_preview_touches: dict[str, float] = {}
_PREVIEW_TOUCH_COOLDOWN = 30.0


def _run_reconciliation_monitor(service: Service) -> None:
    """Run a fresh, read-only reconciliation pass and log actionable drift."""
    checkpoint_id = f"monitor-{uuid4().hex}"
    try:
        report = reconcile_s3_to_postgres(
            service.storage,
            service.catalog,
            checkpoint_id=checkpoint_id,
            resume=False,
            dry_run=True,
            report_only=True,
        )
        counts = report.get("counts", {})
        drift_count = sum(
            int(counts.get(category, 0) or 0)
            for category in (
                "missing",
                "divergent",
                "orphaned",
                "unresolved",
                "conflicting",
                "malformed",
                "failed",
            )
        )
        if report.get("status") != "complete":
            log_event(
                "reconciliation_monitor_failed",
                stage="reconciliation_monitor",
                checkpoint_id=checkpoint_id,
                status=report.get("status"),
                drift_count=drift_count,
            )
        elif drift_count:
            log_event(
                "reconciliation_monitor_drift",
                stage="reconciliation_monitor",
                checkpoint_id=checkpoint_id,
                status=report.get("status"),
                drift_count=drift_count,
                counts=counts,
            )
    except Exception as error:
        log_event(
            "reconciliation_monitor_failed",
            stage="reconciliation_monitor",
            checkpoint_id=checkpoint_id,
            error_class=type(error).__name__,
        )


def _metric_queue_snapshot(service: Service) -> dict[str, dict[str, int]]:
    """Translate the existing queue query into bounded Prometheus labels."""
    values = service.catalog.queue_counts()
    return {
        "onboarding": {
            "pending": int(values.get("onboardingPending", 0) or 0),
            "running": int(values.get("onboardingRunning", 0) or 0),
            "failed": int(values.get("onboardingFailed", 0) or 0),
        },
        "processing": {
            "pending": int(values.get("processingPending", 0) or 0),
            "running": int(values.get("processingRunning", 0) or 0),
            "failed": int(values.get("processingFailed", 0) or 0),
        },
        "preview": {
            "pending": int(values.get("previewPending", 0) or 0),
            "running": int(values.get("previewRunning", 0) or 0),
            "failed": int(values.get("previewFailed", 0) or 0),
        },
        "analysis": {
            "pending": int(values.get("analysisPending", 0) or 0),
            "running": int(values.get("analysisRunning", 0) or 0),
            "failed": int(values.get("analysisFailed", 0) or 0),
        },
    }


def derivative_cache_headers(settings: Settings) -> dict[str, str]:
    """Return browser and shared-cache policy for immutable derivatives."""
    if settings.public_derivative_cache:
        policy = "public, max-age=31536000, immutable"
        return {"Cache-Control": policy, "CDN-Cache-Control": policy}
    return {
        "Cache-Control": "private, max-age=3600",
        "CDN-Cache-Control": "private, max-age=3600",
    }

# Phase 3B of the AI service split plan (docs/ai-service-split-plan.md):
# /health reports whether each AI service is configured and reachable. The
# API process runs its own probes — a GET {ai_base_url}/models for the
# OpenAI-compatible VLM and the face client's identity-checked GET /health —
# at a 3 s timeout (shorter than the worker gate's 5 s, because /health is
# user-facing and must not stall on a dead service), cached 30 s per process
# (the same Q4 cadence the worker gate uses). Any probe failure makes the
# reachable flag false; it never fails the endpoint. The face probe reuses
# face_client's logic (a name/revision/weightsSha256 drift in the reported
# embedding model therefore shows as unreachable — one embedding space,
# global invariant), and a not-configured service (empty URL) is simply not
# probed.
API_HEALTH_PROBE_TIMEOUT_SECONDS = 3.0
_AI_PROBE_CACHE_SECONDS = 30.0


def _probe_vlm(settings: Settings) -> bool:
    """The /health VLM probe: GET {ai_base_url}/models (3 s, bearer key when
    set). The same classification the worker gate's probe applies at its
    5 s timeout — any failure (connection error, timeout, or a non-2xx, a
    bad-key 401 included) is unreachable, so a misconfigured endpoint shows
    as unreachable instead of burning the backlog into failed jobs — but at
    the shorter /health budget."""
    if not settings.ai_base_url:
        return False
    headers = {"Authorization": f"Bearer {settings.ai_api_key}"} if settings.ai_api_key else {}
    try:
        with httpx.Client(timeout=API_HEALTH_PROBE_TIMEOUT_SECONDS) as client:
            response = client.get(f"{settings.ai_base_url}/models", headers=headers)
        return 200 <= response.status_code < 300
    except Exception:
        # A probe failure never fails the endpoint: unreachable.
        return False


def _probe_face(settings: Settings) -> bool:
    """The /health face-service probe: the face client's own
    identity-checked GET /health (face_client.RemoteFaceAnalyzer.health) at
    the 3 s API budget — reused, not duplicated: an unreachable host, a
    timeout, a bad token, 429 pacing, 502-504, and an embedding-model
    identity mismatch all surface as unreachable."""
    if not settings.face_service_url:
        return False
    try:
        RemoteFaceAnalyzer(settings).health(timeout=API_HEALTH_PROBE_TIMEOUT_SECONDS)
        return True
    except Exception:
        # A probe failure never fails the endpoint: unreachable.
        return False


class ProcessingRequest(BaseModel):
    """Select processing stages and either explicit assets or the active library."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    asset_ids: list[UUID] | None = Field(default=None, min_length=1, max_length=10000)
    stages: list[Literal["metadata"]] = Field(default_factory=lambda: ["metadata"], min_length=1)
    include_deleted: bool = False


class AnalysisRequest(BaseModel):
    """Select explicit assets or the complete active library for AI analysis."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    asset_ids: list[UUID] | None = Field(default=None, min_length=1, max_length=10000)
    include_deleted: bool = False
    force_full: bool = False


class PersonNameRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    operation_id: UUID
    display_name: str = Field(default="", max_length=200)

    @field_validator("display_name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        return value.strip()


class PersonMergeRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    operation_id: UUID
    target_person_id: UUID


class FaceMoveRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    operation_id: UUID
    face_ids: list[UUID] = Field(min_length=1, max_length=1000)
    target_person_id: UUID | None = None


def _record_access(service: Service, manifest, asset_id: str) -> None:
    """Bump preview_cache.last_accessed_at for a served preview, at most once
    per cooldown. Best-effort bookkeeping: never let it break the response."""
    now = time.monotonic()
    last = _preview_touches.get(asset_id)
    if last is not None and now - last < _PREVIEW_TOUCH_COOLDOWN:
        return
    _preview_touches[asset_id] = now
    try:
        if not service.catalog.touch_preview_cache(asset_id):
            # Row missing (e.g. cache dir created before tracking): insert
            # from the files on disk.
            paths = cache_paths(service, manifest)
            service.catalog.backfill_preview_cache(
                asset_id,
                paths["preview"].stat().st_size,
                paths["thumbnail"].stat().st_size,
            )
    except Exception:
        pass


def create_app(settings: Settings | None = None) -> FastAPI:
    service = Service(settings or Settings())
    upload_gate = UploadGate(service.settings.upload_workers)
    metrics = Metrics()

    @asynccontextmanager
    async def lifespan(app):
        service.initialize()
        await asyncio.to_thread(reconcile_ready_upload_batches, service)

        async def maintenance():
            while True:
                await asyncio.sleep(service.settings.upload_cleanup_interval_seconds)
                try:
                    await asyncio.to_thread(reconcile_ready_upload_batches, service)
                    await asyncio.to_thread(cleanup_abandoned_batches, service)
                except Exception as error:
                    # Cleanup is best-effort and must never take down the API.
                    log_event("maintenance_failed", stage="maintenance", error_class=type(error).__name__)

        maintenance_task = asyncio.create_task(maintenance())

        async def reconciliation_monitor():
            while True:
                await asyncio.sleep(service.settings.reconciliation_monitor_interval_seconds)
                await asyncio.to_thread(_run_reconciliation_monitor, service)

        reconciliation_monitor_task = asyncio.create_task(reconciliation_monitor())
        app.state.service = service
        app.state.upload_gate = upload_gate
        app.state.metrics = metrics
        try:
            yield
        finally:
            maintenance_task.cancel()
            reconciliation_monitor_task.cancel()
            await asyncio.gather(
                maintenance_task,
                reconciliation_monitor_task,
                return_exceptions=True,
            )
            service.catalog.engine.dispose()

    app = FastAPI(
        title="Photo Server",
        version="0.6.0",
        description=(
            "Single-user photo library API. When authentication is enabled, "
            "`/livez` is public; all library, health, operational, docs, and "
            "asset routes require the signed browser session cookie or the "
            "configured bearer token."
        ),
        lifespan=lifespan,
    )
    # Phase 3B: the AI-service probe cache (Q4): the monotonic timestamp of
    # the last probe plus the two reachable results. A dict so the health
    # handler updates it in place; on app.state as the test/ops seam (the
    # worker gate keeps the same shape on its own instance).
    ai_probe_cache = {"ts": 0.0, "semantic": False, "face": False}
    app.state.ai_probe_cache = ai_probe_cache
    app.state.metrics = metrics
    origins = [
        origin.strip() for origin in service.settings.cors_origins.split(",") if origin.strip()
    ]
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "Content-Length", "X-CSRF-Token"],
            allow_credentials=True,
        )

    public_paths = {"/livez", "/auth/login", "/auth/session", "/auth/logout"}

    @app.middleware("http")
    async def protect_requests(request: Request, call_next):
        if not service.settings.auth_enabled or request.method == "OPTIONS":
            return await call_next(request)
        path = request.url.path
        if path in public_paths:
            if request.method in {"POST", "PUT", "PATCH", "DELETE"} and not origin_allowed(request, origins):
                return JSONResponse(status_code=403, content={"detail": "Unsafe cross-origin request"})
            return await call_next(request)
        scheme = auth_scheme(
            request,
            service.settings.session_secret.get_secret_value(),
            service.settings.session_cookie_name,
            service.settings.api_token.get_secret_value(),
        )
        if scheme == "none":
            return JSONResponse(status_code=401, content={"detail": "Authentication required"})
        if scheme == "cookie" and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            if not origin_allowed(request, origins):
                return JSONResponse(status_code=403, content={"detail": "Unsafe cross-origin request"})
        request.state.auth_scheme = scheme
        return await call_next(request)

    @app.exception_handler(LibraryError)
    async def library_error(request: Request, error: LibraryError):
        return JSONResponse(status_code=409, content={"detail": str(error)})

    @app.exception_handler(FileNotFoundError)
    async def missing_file(request: Request, error: FileNotFoundError):
        return JSONResponse(status_code=404, content={"detail": "Requested item does not exist"})

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/docs")

    @app.post("/auth/login", response_model=SessionOut, operation_id="login")
    def login(body: LoginRequest, request: Request, response: Response):
        # Keep the failure shape and timing independent of private resources.
        valid = verify_password(body.password, service.settings.password_hash)
        if not valid:
            log_event("authentication_failed", reason="invalid_credentials")
            raise HTTPException(401, "Invalid credentials")
        token = issue_session(
            service.settings.session_secret.get_secret_value(),
            service.settings.session_ttl_seconds,
        )
        response.set_cookie(
            service.settings.session_cookie_name,
            token,
            max_age=service.settings.session_ttl_seconds,
            httponly=True,
            secure=service.settings.session_cookie_secure,
            samesite=service.settings.session_cookie_samesite,
            path="/",
        )
        return {"authenticated": True}

    @app.get("/auth/session", response_model=SessionOut, operation_id="getCurrentSession")
    def current_session(request: Request):
        authenticated = not service.settings.auth_enabled
        if service.settings.auth_enabled:
            authenticated = auth_scheme(
                request,
                service.settings.session_secret.get_secret_value(),
                service.settings.session_cookie_name,
                service.settings.api_token.get_secret_value(),
            ) != "none"
        return {"authenticated": authenticated}

    @app.post("/auth/logout", response_model=SessionOut, operation_id="logout")
    def logout(response: Response):
        response.delete_cookie(service.settings.session_cookie_name, path="/")
        return {"authenticated": False}

    def dependency_status() -> tuple[dict, dict]:
        database = {"status": "ready", "error_class": None}
        storage = {"status": "ready", "error_class": None}
        try:
            with service.catalog.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except Exception as error:
            database = {"status": "unavailable", "error_class": type(error).__name__}
        try:
            service.storage.client.head_bucket(Bucket=service.storage.bucket)
        except Exception as error:
            storage = {"status": "unavailable", "error_class": type(error).__name__}
        return database, storage

    @app.get("/livez", response_model=LivenessOut, operation_id="getLiveness")
    def livez():
        return {"status": "ok"}

    @app.get("/readyz", response_model=ReadinessOut, responses={503: {"model": ReadinessFailureOut}}, operation_id="getReadiness")
    def readyz():
        database, storage = dependency_status()
        if database["status"] != "ready" or storage["status"] != "ready":
            log_event("readiness_failed", database=database, storage=storage)
            return JSONResponse(status_code=503, content={"status": "not_ready", "database": {"status": database["status"], "errorClass": database["error_class"]}, "storage": {"status": storage["status"], "errorClass": storage["error_class"]}})
        return {"status": "ready", "database": database, "storage": storage}

    @app.get("/health", response_model=HealthOut, responses={503: {"model": HealthFailureOut}}, operation_id="getHealth")
    def health():
        database, storage = dependency_status()
        if database["status"] != "ready":
            log_event("health_unavailable", database=database, storage=storage)
            return JSONResponse(status_code=503, content={"status": "unavailable", "database": {"status": database["status"], "errorClass": database["error_class"]}, "storage": {"status": storage["status"], "errorClass": storage["error_class"]}})
        # Phase 3B: probe both AI services at most once every 30 s (Q4); the
        # probes never raise — a failure makes the reachable flag false.
        now = time.monotonic()
        if now - ai_probe_cache["ts"] >= _AI_PROBE_CACHE_SECONDS:
            # Probe independently so two dead services cost at most the
            # single per-service timeout, not the sum of both timeouts.
            with ThreadPoolExecutor(max_workers=2) as executor:
                semantic_probe = executor.submit(_probe_vlm, service.settings)
                face_probe = executor.submit(_probe_face, service.settings)
                ai_probe_cache["semantic"] = semantic_probe.result()
                ai_probe_cache["face"] = face_probe.result()
            ai_probe_cache["ts"] = now
        backup = service.backup_status() if storage["status"] == "ready" else {
            "postgresBackupKey": None, "postgresBackupAt": None,
            "postgresBackupPrimary": {"status": "unavailable"},
            "postgresBackupSecondary": {"status": "unavailable"},
            "postgresBackupOverall": "unavailable",
        }
        return {
            "status": "degraded" if storage["status"] != "ready" or backup.get("postgresBackupOverall", "healthy") != "healthy" else "ok",
            "libraryId": str(service.library_id),
            **service.catalog.counts(),
            **service.catalog.queue_counts(),
            **upload_gate.status(),
            **backup,
            "aiSemanticConfigured": bool(service.settings.ai_base_url),
            "aiSemanticReachable": ai_probe_cache["semantic"],
            "aiFaceConfigured": bool(service.settings.face_service_url),
            "aiFaceReachable": ai_probe_cache["face"],
            "database": database,
            "storage": storage,
            "workers": [
                {"workerType": worker_type, **read_heartbeat(service.settings.data_dir, worker_type, service.settings.worker_stale_seconds)}
                for worker_type in ("worker", "ai-worker")
            ],
        }

    @app.get("/metrics", include_in_schema=False)
    def prometheus_metrics(request: Request):
        """Opt-in Prometheus text endpoint with bounded, non-private labels."""
        if not service.settings.metrics_enabled:
            raise HTTPException(404, "Metrics are disabled")
        if service.settings.metrics_loopback_only and request.client and request.client.host not in {
            "127.0.0.1", "::1", "localhost", "testclient"
        }:
            raise HTTPException(404, "Metrics are loopback-only")
        try:
            for queue, values in _metric_queue_snapshot(service).items():
                for status, value in values.items():
                    safe_metrics(metrics, "set", "photo_queue_jobs", value, {"queue": queue, "status": status})
            try:
                oldest = service.catalog.queue_oldest_ages()
            except Exception:
                oldest = {queue: -1 for queue in _metric_queue_snapshot(service)}
            for queue, value in oldest.items():
                safe_metrics(metrics, "set", "photo_queue_oldest_job_age_seconds", value, {"queue": queue})
            safe_metrics(metrics, "set", "photo_preview_cache_bytes", service.catalog.preview_cache_total_bytes())
            try:
                backup = service.backup_status()
            except Exception:
                safe_metrics(metrics, "inc", "photo_backup_failures")
                backup = {}
            backup_at = backup.get("postgresBackupAt")
            if backup_at:
                from datetime import UTC, datetime

                safe_metrics(metrics, "set", "photo_backup_age_seconds", max(0, (datetime.now(UTC) - datetime.fromisoformat(backup_at)).total_seconds()))
            else:
                safe_metrics(metrics, "set", "photo_backup_age_seconds", -1)
        except Exception:
            # Scraping must never make the API or its primary database path fail.
            safe_metrics(metrics, "inc", "photo_metrics_collection_failures")
        return Response(content=metrics.render(), media_type="text/plain; version=0.0.4")

    @app.post(
        "/upload-batches",
        status_code=201,
        response_model=UploadBatchOut,
        operation_id="createUploadBatch",
    )
    def start_upload_batch(body: UploadBatchRequest):
        result = create_batch(
            service,
            [file.model_dump(by_alias=True, exclude_none=True) for file in body.files],
            body.batch_id,
            body.album_id,
            body.album_name,
        )
        safe_metrics(metrics, "inc", "photo_upload_batches")
        return result

    @app.get(
        "/upload-batches",
        response_model=list[UploadBatchOut],
        operation_id="listUploadBatches",
    )
    def get_active_upload_batches(limit: int = Query(default=100, ge=1, le=1000)):
        return list_active_batches(service, limit)

    @app.get(
        "/upload-batches/{batch_id}",
        response_model=UploadBatchOut,
        operation_id="getUploadBatch",
    )
    def get_upload_batch(batch_id: UUID):
        return describe_batch(service, batch_id)

    @app.delete(
        "/upload-batches/{batch_id}",
        response_model=BatchAbandonedOut,
        operation_id="abandonUploadBatch",
    )
    def discard_upload_batch(batch_id: UUID):
        return abandon_batch(service, batch_id)

    @app.put(
        "/upload-batches/{batch_id}/files/{file_id}",
        response_model=UploadFileReceipt,
        operation_id="uploadFile",
    )
    async def upload_file(batch_id: UUID, file_id: UUID, request: Request):
        header = request.headers.get("content-length")
        try:
            content_length = int(header) if header is not None else None
        except ValueError as error:
            raise HTTPException(400, "Invalid Content-Length header") from error
        result = await receive_file(
            service,
            upload_gate,
            batch_id,
            file_id,
            request.stream(),
            content_length,
        )
        if not result.get("replayed"):
            safe_metrics(metrics, "inc", "photo_uploaded_bytes", float(content_length or 0))
        return result

    @app.post(
        "/upload-batches/{batch_id}/seal",
        status_code=202,
        response_model=UploadBatchOut,
        operation_id="sealUploadBatch",
    )
    def finish_upload_batch(batch_id: UUID):
        return seal_batch(service, batch_id)

    @app.post(
        "/upload-batches/{batch_id}/retry",
        status_code=202,
        response_model=UploadBatchOut,
        operation_id="retryUploadBatch",
    )
    def retry_upload_batch(batch_id: UUID):
        service.catalog.retry_upload_batch(batch_id)
        return describe_batch(service, batch_id)

    @app.get("/upload-queue", response_model=UploadQueueStatusOut, operation_id="getUploadQueue")
    def upload_queue():
        return {**service.catalog.queue_counts(), **upload_gate.status()}

    @app.get("/assets", response_model=list[CurrentAssetDocOut], operation_id="listAssets")
    def list_assets(
        limit: int = Query(default=100, ge=1, le=1000), offset: int = Query(default=0, ge=0)
    ):
        return service.catalog.list_assets(limit, offset)

    @app.get("/library/assets", response_model=BrowsePageOut, operation_id="browseAssets")
    def browse_assets(query: Annotated[BrowseQuery, Query()]):
        try:
            return service.catalog.browse(query)
        except ValueError as error:
            raise HTTPException(400, str(error)) from error

    def find(asset_id: UUID):
        manifest = service.catalog.get(str(asset_id))
        if manifest is None:
            raise HTTPException(404, "Asset not found")
        return manifest

    @app.get("/assets/{asset_id}", response_model=CurrentAssetDetailOut, operation_id="getAssetDetail")
    def get_asset(asset_id: UUID):
        manifest = find(asset_id)
        return {
            **manifest.document(),
            "thumbnailUrl": f"/assets/{asset_id}/thumbnail?v={derivative_version('thumbnail', manifest.primary.sha256)}",
            "previewUrl": f"/assets/{asset_id}/preview?v={derivative_version('preview', manifest.primary.sha256)}",
            "technical": technical_fields(manifest.metadata),
            "processing": service.catalog.processing_status(str(asset_id)),
            "analysis": service.catalog.analysis_status(str(asset_id)),
            "preview": service.catalog.preview_status(str(asset_id)),
            "userState": service.catalog.user_state(str(asset_id)),
        }

    @app.post(
        "/processing",
        status_code=202,
        response_model=QueueResultOut,
        operation_id="queueProcessing",
    )
    def queue_processing(body: ProcessingRequest):
        return service.queue_processing(
            body.asset_ids,
            body.stages,
            body.include_deleted,
        )

    @app.post(
        "/analysis",
        status_code=202,
        response_model=QueueResultOut,
        operation_id="queueAnalysis",
    )
    def queue_analysis(body: AnalysisRequest):
        return service.queue_analysis(
            body.asset_ids, body.include_deleted, body.force_full
        )

    @app.post(
        "/assets/{asset_id}/analysis/retry",
        status_code=202,
        response_model=QueueResultOut,
        operation_id="retryAnalysis",
    )
    def retry_analysis(asset_id: UUID, force_full: bool = Query(default=False)):
        find(asset_id)
        return service.queue_analysis([asset_id], force_full=force_full)

    @app.get("/people", response_model=PeoplePageOut, operation_id="listPeople")
    def list_people(
        q: str = Query(default="", max_length=200),
        limit: int = Query(default=500, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ):
        return service.catalog.list_people(q, limit, offset)

    @app.get("/people/{person_id}", response_model=PersonDetailOut, operation_id="getPerson")
    def get_person(
        person_id: UUID,
        limit: int = Query(default=2000, ge=1, le=5000),
        offset: int = Query(default=0, ge=0),
    ):
        person = service.catalog.person_detail(str(person_id), limit, offset)
        if person is None:
            raise HTTPException(404, "Person not found")
        return person

    @app.patch("/people/{person_id}", response_model=PersonRenameOut, operation_id="renamePerson")
    def rename_person(person_id: UUID, body: PersonNameRequest):
        return mutate_face(
            service,
            body.operation_id,
            {
                "action": "person.rename",
                "personId": str(person_id),
                "displayName": body.display_name,
            },
        )

    @app.post(
        "/people/{person_id}/merge",
        response_model=PersonMergeOut,
        operation_id="mergePerson",
    )
    def merge_person(person_id: UUID, body: PersonMergeRequest):
        return mutate_face(
            service,
            body.operation_id,
            {
                "action": "person.merge",
                "sourcePersonId": str(person_id),
                "targetPersonId": str(body.target_person_id),
            },
        )

    @app.post("/faces/move", response_model=FaceMoveOut, operation_id="moveFaces")
    def move_faces(body: FaceMoveRequest):
        face_ids = [str(face_id) for face_id in body.face_ids]
        if len(set(face_ids)) != len(face_ids):
            raise HTTPException(422, "Face IDs must be unique")
        return mutate_face(
            service,
            body.operation_id,
            {
                "action": "faces.move",
                "faceIds": face_ids,
                "targetPersonId": str(body.target_person_id) if body.target_person_id else None,
            },
        )

    # Phase 4 (plan): the four binary endpoints serve bytes, not JSON, and
    # the client uses them as URLs (<img src>, <a download>, XHR upload), so
    # they get documented media types and the 202 + Retry-After contract
    # instead of a response model — there is no JSON 200 body to type.
    # ``responses=`` is documentation-only in FastAPI (it never touches the
    # wire), and the openapi() projection below strips the untyped
    # application/json placeholder FastAPI merges under these 200s, so the
    # spec records only what is actually served.
    def derivative_responses() -> dict:
        return {
            "200": {
                "description": "The generated JPEG derivative",
                "content": {"image/jpeg": {"schema": {"type": "string", "format": "binary"}}},
                "headers": {
                    "Cache-Control": {
                        "description": "private, max-age=3600",
                        "schema": {"type": "string"},
                    },
                    "ETag": {
                        "description": "Stable validator for the derivative representation",
                        "schema": {"type": "string"},
                    },
                    "CDN-Cache-Control": {
                        "description": "Shared-cache policy; public only when explicitly enabled",
                        "schema": {"type": "string"},
                    },
                },
            },
            "202": {
                "description": "The JPEG is not ready yet; re-request after the Retry-After seconds",
                "model": Pending202Out,
                "headers": {
                    "Retry-After": {
                        "description": "Seconds until the derivative is likely ready (2)",
                        "schema": {"type": "integer"},
                    },
                    "Cache-Control": {
                        "description": "no-store because the derivative is not ready",
                        "schema": {"type": "string"},
                    },
                    "CDN-Cache-Control": {
                        "description": "no-store because the derivative is not ready",
                        "schema": {"type": "string"},
                    },
                },
            },
        }

    @app.get(
        "/faces/{face_id}/thumbnail",
        responses=derivative_responses(),
        operation_id="getFaceThumbnail",
    )
    def face_thumbnail(face_id: UUID, request: Request):
        face = service.catalog.face(str(face_id))
        if face is None:
            raise HTTPException(404, "Face not found")
        if service.catalog.get(face["asset_id"]) is None:
            raise HTTPException(404, "Photograph not found")
        manifest = service.canonical_asset(face["asset_id"])
        path = cache_paths(service, manifest)["preview"]
        if not path.exists():
            safe_metrics(metrics, "inc", "photo_preview_cache_misses", labels={"kind": "face_thumbnail"})
            status = service.catalog.preview_status(str(manifest.asset_id))
            if status["status"] == "unavailable":
                raise HTTPException(
                    404,
                    "Photograph preview is unavailable",
                    headers={"Cache-Control": "no-store", "CDN-Cache-Control": "no-store"},
                )
            if status["status"] == "failed":
                raise HTTPException(
                    503,
                    "Photograph preview generation failed",
                    headers={"Cache-Control": "no-store", "CDN-Cache-Control": "no-store"},
                )
            service.catalog.queue_preview(str(manifest.asset_id))
            return JSONResponse(
                status_code=202,
                content={"status": "pending"},
                headers={
                    "Retry-After": "2",
                    "Cache-Control": "no-store",
                    "CDN-Cache-Control": "no-store",
                },
            )
        safe_metrics(metrics, "inc", "photo_preview_cache_hits", labels={"kind": "face_thumbnail"})
        etag = derivative_etag(f"face-thumbnail-{face_id}", manifest.primary.sha256)
        headers = {**derivative_cache_headers(service.settings), "ETag": etag}
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        with Image.open(path) as source:
            image = source.convert("RGB")
            x, y, width, height = [float(value) for value in face["bounding_box"]]
            center_x, center_y = x + width / 2, y + height / 2
            side = max(width, height) * 1.45
            left = max(0.0, center_x - side / 2)
            top = max(0.0, center_y - side / 2)
            right = min(1.0, center_x + side / 2)
            bottom = min(1.0, center_y + side / 2)
            crop = image.crop(
                (
                    round(left * image.width),
                    round(top * image.height),
                    max(round(right * image.width), round(left * image.width) + 1),
                    max(round(bottom * image.height), round(top * image.height) + 1),
                )
            )
            crop.thumbnail((320, 320), Image.Resampling.LANCZOS)
            output = BytesIO()
            crop.save(output, format="JPEG", quality=88)
        return Response(output.getvalue(), media_type="image/jpeg", headers=headers)

    @app.patch(
        "/assets/{asset_id}/user-state",
        response_model=MutationResultOut,
        operation_id="patchAssetUserState",
    )
    @app.patch(
        "/assets/{asset_id}/metadata",
        response_model=MutationResultOut,
        operation_id="patchAssetMetadata",
    )
    def update_user_state(asset_id: UUID, body: UserStatePatch):
        return mutate(
            service,
            body.operation_id,
            Mutation(
                action="asset.patch",
                entity_id=asset_id,
                changes=body.changes(),
                expected_revision=body.expected_revision,
            ),
        )

    @app.delete("/assets/{asset_id}", response_model=MutationResultOut, operation_id="trashAsset")
    def trash_asset(asset_id: UUID, body: OperationRequest):
        return mutate(
            service,
            body.operation_id,
            Mutation(
                action="asset.delete",
                entity_id=asset_id,
                expected_revision=body.expected_revision,
            ),
        )

    @app.post(
        "/assets/{asset_id}/restore",
        response_model=MutationResultOut,
        operation_id="restoreAsset",
    )
    def restore_asset(asset_id: UUID, body: OperationRequest):
        return mutate(
            service,
            body.operation_id,
            Mutation(
                action="asset.restore",
                entity_id=asset_id,
                expected_revision=body.expected_revision,
            ),
        )

    @app.get("/assets/{asset_id}/burst", response_model=BurstDetailOut, operation_id="getBurst")
    def get_asset_burst(asset_id: UUID):
        find(asset_id)
        detail = service.catalog.burst_detail(str(asset_id))
        if detail is None:
            raise HTTPException(404, "Asset has no burst")
        return detail

    @app.post(
        "/assets/{asset_id}/burst/representative",
        response_model=BurstRepresentativeOut,
        operation_id="setBurstRepresentative",
    )
    def set_burst_representative(asset_id: UUID, body: OperationRequest):
        find(asset_id)
        detail = service.catalog.burst_detail(str(asset_id))
        if detail is None:
            raise HTTPException(409, "Asset is not in a burst")
        return mutate(
            service,
            body.operation_id,
            Mutation(
                action="burst.setRepresentative",
                entity_id=UUID(detail["burstId"]),
                changes={"representativeAssetId": str(asset_id)},
            ),
        )

    @app.post(
        "/assets/{asset_id}/burst/remove",
        response_model=BurstMemberRemovedOut,
        operation_id="removeBurstMember",
    )
    def remove_burst_member(asset_id: UUID, body: OperationRequest):
        find(asset_id)
        detail = service.catalog.burst_detail(str(asset_id))
        if detail is None:
            raise HTTPException(409, "Asset is not in a burst")
        return mutate(
            service,
            body.operation_id,
            Mutation(
                action="burst.removeMember",
                entity_id=UUID(detail["burstId"]),
                changes={"assetId": str(asset_id)},
            ),
        )

    @app.get("/albums", response_model=list[AlbumOut], operation_id="listAlbums")
    def list_albums(deleted: bool = False):
        return service.catalog.list_albums(deleted)

    @app.post("/albums", status_code=201, response_model=AlbumOut, operation_id="createAlbum")
    def create_album(body: AlbumPatch):
        if body.name is None:
            raise HTTPException(422, "Provide an album name")
        album_id = uuid5(service.library_id, f"album:{body.operation_id}")
        return mutate(
            service,
            body.operation_id,
            Mutation(
                action="album.create",
                entity_id=album_id,
                changes=body.changes(),
                expected_revision=body.expected_revision,
            ),
        )

    @app.get("/albums/{album_id}", response_model=AlbumOut, operation_id="getAlbum")
    def get_album(album_id: UUID):
        album = service.catalog.get_album(str(album_id))
        if album is None:
            raise HTTPException(404, "Album not found")
        return album.document()

    @app.patch("/albums/{album_id}", response_model=AlbumOut, operation_id="updateAlbum")
    def update_album(album_id: UUID, body: AlbumPatch):
        return mutate(
            service,
            body.operation_id,
            Mutation(
                action="album.patch",
                entity_id=album_id,
                changes=body.changes(),
                expected_revision=body.expected_revision,
            ),
        )

    @app.delete("/albums/{album_id}", response_model=AlbumOut, operation_id="trashAlbum")
    def trash_album(album_id: UUID, body: OperationRequest):
        """DELETE with a JSON request body (the durable-mutation journal
        protocol): the body carries the client-chosen ``operationId`` and
        optionally ``expectedRevision``. Generated clients must send it
        explicitly; a bodyless DELETE is rejected as 422."""
        return mutate(
            service,
            body.operation_id,
            Mutation(
                action="album.delete",
                entity_id=album_id,
                expected_revision=body.expected_revision,
            ),
        )

    @app.post("/albums/{album_id}/restore", response_model=AlbumOut, operation_id="restoreAlbum")
    def restore_album(album_id: UUID, body: OperationRequest):
        return mutate(
            service,
            body.operation_id,
            Mutation(
                action="album.restore",
                entity_id=album_id,
                expected_revision=body.expected_revision,
            ),
        )

    @app.get(
        "/assets/{asset_id}/original",
        responses={
            "200": {
                "description": "The stored original, streamed; the media type is the stored blob's MIME type",
                "content": {
                    "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
                },
                "headers": {
                    "Content-Length": {
                        "description": "The stored size in bytes",
                        "schema": {"type": "integer"},
                    },
                    "Content-Disposition": {
                        "description": "attachment; filename*=UTF-8''<url-encoded original filename>",
                        "schema": {"type": "string"},
                    },
                },
            }
        },
        operation_id="downloadOriginal",
    )
    def original(asset_id: UUID):
        manifest = find(asset_id)
        manifest = service.canonical_asset(asset_id)
        if service.storage.head(manifest.primary.object_key) is None:
            raise HTTPException(503, "Original is missing from storage")
        return StreamingResponse(
            service.storage.chunks(manifest.primary.object_key),
            media_type=manifest.primary.mime_type,
            headers={
                "Content-Length": str(manifest.primary.size_bytes),
                "Content-Disposition": "attachment; filename*=UTF-8''"
                + quote(manifest.primary.original_filename, safe=""),
            },
        )

    def derivative(asset_id: UUID, kind: str, request: Request):
        manifest = find(asset_id)
        manifest = service.canonical_asset(asset_id)
        path = cache_paths(service, manifest)[kind]
        if not path.exists():
            safe_metrics(metrics, "inc", "photo_preview_cache_misses", labels={"kind": kind})
            status = service.catalog.preview_status(str(asset_id))
            if status["status"] == "unavailable":
                raise HTTPException(
                    404,
                    "Embedded preview unavailable",
                    headers={"Cache-Control": "no-store", "CDN-Cache-Control": "no-store"},
                )
            if status["status"] == "failed":
                raise HTTPException(
                    503,
                    "Preview generation failed; see asset status",
                    headers={"Cache-Control": "no-store", "CDN-Cache-Control": "no-store"},
                )
            service.catalog.queue_preview(str(asset_id))
            return JSONResponse(
                status_code=202,
                content={"status": "pending"},
                headers={
                    "Retry-After": "2",
                    "Cache-Control": "no-store",
                    "CDN-Cache-Control": "no-store",
                },
            )
        safe_metrics(metrics, "inc", "photo_preview_cache_hits", labels={"kind": kind})
        _record_access(service, manifest, str(asset_id))
        etag = derivative_etag(kind, manifest.primary.sha256)
        headers = {**derivative_cache_headers(service.settings), "ETag": etag}
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        return FileResponse(
            path, media_type="image/jpeg", headers=headers
        )

    @app.get(
        "/assets/{asset_id}/preview",
        responses=derivative_responses(),
        operation_id="getAssetPreview",
    )
    def preview(asset_id: UUID, request: Request):
        return derivative(asset_id, "preview", request)

    @app.get(
        "/assets/{asset_id}/thumbnail",
        responses=derivative_responses(),
        operation_id="getAssetThumbnail",
    )
    def thumbnail(asset_id: UUID, request: Request):
        return derivative(asset_id, "thumbnail", request)

    @app.post(
        "/assets/{asset_id}/preview/retry",
        response_model=PreviewStatusOut,
        operation_id="retryPreview",
    )
    def retry_preview(asset_id: UUID):
        find(asset_id)
        service.catalog.queue_preview(str(asset_id))
        return service.catalog.preview_status(str(asset_id))

    @app.post("/maintenance/verify", response_model=VerifyOut, operation_id="verifyStorage")
    def verify_storage(full: bool = False):
        return service.verify(full)

    @app.post(
        "/maintenance/recluster-bursts",
        response_model=BurstReclusterOut,
        operation_id="reclusterBursts",
    )
    def recluster_bursts():
        return service.recluster_bursts()

    # The responses= declarations above are merged by FastAPI into (not
    # replacing) the untyped application/json placeholder it writes for
    # modelless 200 responses, which would leave an empty JSON schema beside
    # the documented media type in the spec. This projection — spec only,
    # never the wire — drops the placeholder from the four binary 200s so
    # the checked-in spec and the served /openapi.json and /docs record
    # exactly the media types and headers that are actually served.
    #
    # The projection also hoists the 2xx schemas FastAPI emits inline because
    # they are not a single named model: the list[X] arrays and the
    # discriminated unions (their members are $refs, the wrapper is not). An
    # inline schema carries no component title, so pydantic falls back to a
    # title derived from the response field name ("Response <operationId>"),
    # breaking the naming precedent of the operations whose 2xx schema is a
    # $ref to a named component. Hoisting gives every 2xx JSON response a
    # $ref (and generated clients stable type names): the unions reuse the
    # code alias names (AssetDocOut, AssetDetailOut) and the arrays are named
    # <Model>OutList, matching the ...Out component convention.
    # FastAPI caches the generated schema in app.openapi_schema, so the
    # projection deep-copies before mutating.
    _binary_200_media = {
        "/assets/{asset_id}/original": "application/octet-stream",
        "/assets/{asset_id}/preview": "image/jpeg",
        "/assets/{asset_id}/thumbnail": "image/jpeg",
        "/faces/{face_id}/thumbnail": "image/jpeg",
    }
    # AssetDocOut is now a named flat model, so no array-item union hoisting is
    # required here.
    _hoist_items = {}
    # (path, method, status) -> component name for the whole inline 2xx schema.
    _hoist_response = {
        ("/albums", "get", "200"): "AlbumOutList",
        ("/assets", "get", "200"): "AssetDocOutList",
        ("/upload-batches", "get", "200"): "UploadBatchOutList",
        ("/assets/{asset_id}", "get", "200"): "AssetDetailOut",
    }
    _base_openapi = app.openapi

    def openapi() -> dict:
        spec = copy.deepcopy(_base_openapi())
        security_schemes = spec.setdefault("components", {}).setdefault("securitySchemes", {})
        security_schemes["bearerToken"] = {
            "type": "http",
            "scheme": "bearer",
            "description": "Static PHOTO_API_TOKEN for CLI and automation.",
        }
        security_schemes["sessionCookie"] = {
            "type": "apiKey",
            "in": "cookie",
            "name": service.settings.session_cookie_name,
            "description": "HTTP-only signed browser session cookie.",
        }
        for path, path_item in spec.get("paths", {}).items():
            if path in {"/livez", "/auth/login", "/auth/session", "/auth/logout"}:
                continue
            for operation in path_item.values():
                if isinstance(operation, dict) and "responses" in operation:
                    operation["security"] = [{"bearerToken": []}, {"sessionCookie": []}]
        for path, media_type in _binary_200_media.items():
            content = spec["paths"][path]["get"]["responses"]["200"]["content"]
            content.pop("application/json", None)
            content.setdefault(
                media_type, {"schema": {"type": "string", "format": "binary"}}
            )
        components = spec["components"]["schemas"]
        for (path, method, status), name in _hoist_items.items():
            schema = (
                spec["paths"][path][method]["responses"][status]
                ["content"]["application/json"]["schema"]
            )
            items = schema.get("items")
            if not isinstance(items, dict) or "oneOf" not in items:
                raise RuntimeError(
                    f"{method.upper()} {path}: expected inline union items, "
                    f"got {items!r}; update _hoist_items"
                )
            if name in components:
                raise RuntimeError(f"component {name} already defined")
            items["title"] = name
            components[name] = items
            schema["items"] = {"$ref": f"#/components/schemas/{name}"}
        for (path, method, status), name in _hoist_response.items():
            content = (
                spec["paths"][path][method]["responses"][status]
                ["content"]["application/json"]
            )
            schema = content["schema"]
            if "$ref" in schema:
                continue
            if name.endswith("OutList"):
                if schema.get("type") != "array" or not isinstance(
                    schema.get("items"), dict
                ):
                    raise RuntimeError(
                        f"{method.upper()} {path}: expected inline array, "
                        f"got {schema!r}; update _hoist_response"
                    )
            elif "oneOf" not in schema:
                raise RuntimeError(
                    f"{method.upper()} {path}: expected inline union, "
                    f"got {schema!r}; update _hoist_response"
                )
            if name in components:
                raise RuntimeError(f"component {name} already defined")
            schema["title"] = name
            components[name] = schema
            content["schema"] = {"$ref": f"#/components/schemas/{name}"}
        return spec

    app.openapi = openapi

    return app


app = create_app()
