import time
from contextlib import asynccontextmanager
from io import BytesIO
from typing import Annotated, Literal
from urllib.parse import quote
from uuid import UUID, uuid5

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

from photo_server.api_schemas import (
    AlbumOut,
    AssetDetailOut,
    AssetDocOut,
    BatchAbandonedOut,
    BrowsePageOut,
    BurstDetailOut,
    BurstRepresentativeOut,
    HealthOut,
    MutationResultOut,
    PeoplePageOut,
    PersonDetailOut,
    PreviewStatusOut,
    QueueResultOut,
    UploadBatchOut,
    UploadFileReceipt,
    UploadQueueStatusOut,
)
from photo_server.browsing import AlbumPatch, BrowseQuery, OperationRequest, UserStatePatch
from photo_server.config import LibraryError, Settings
from photo_server.metadata import technical_fields
from photo_server.models import Mutation
from photo_server.service import Service
from photo_server.state import mutate
from photo_server.uploads import (
    UploadGate,
    abandon_batch,
    create_batch,
    describe_batch,
    list_active_batches,
    receive_file,
    seal_batch,
)
from photo_server.worker import cache_paths

# In-process throttle for preview_cache.last_accessed_at updates: asset id ->
# monotonic timestamp of the last touch. Bounded by the number of assets
# served per process lifetime; a per-request DB UPDATE would be wasteful.
_preview_touches: dict[str, float] = {}
_PREVIEW_TOUCH_COOLDOWN = 30.0


class UploadFileDeclaration(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    path: str = Field(min_length=1, max_length=1024)
    size_bytes: int = Field(gt=0)
    mime_type: str | None = Field(default=None, max_length=255)


class UploadBatchRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    batch_id: UUID | None = None
    files: list[UploadFileDeclaration] = Field(min_length=1, max_length=10000)


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

    @asynccontextmanager
    async def lifespan(app):
        service.initialize()
        app.state.service = service
        app.state.upload_gate = upload_gate
        yield
        service.catalog.engine.dispose()

    app = FastAPI(title="Photo Server", version="0.6.0", lifespan=lifespan)
    origins = [
        origin.strip() for origin in service.settings.cors_origins.split(",") if origin.strip()
    ]
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Content-Type", "Content-Length"],
        )

    @app.exception_handler(LibraryError)
    async def library_error(request: Request, error: LibraryError):
        return JSONResponse(status_code=409, content={"detail": str(error)})

    @app.exception_handler(FileNotFoundError)
    async def missing_file(request: Request, error: FileNotFoundError):
        return JSONResponse(status_code=404, content={"detail": "Requested item does not exist"})

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/docs")

    @app.get("/health", response_model=HealthOut)
    def health():
        service.storage.client.head_bucket(Bucket=service.storage.bucket)
        return {
            "status": "ok",
            "libraryId": str(service.library_id),
            **service.catalog.counts(),
            **service.catalog.queue_counts(),
            **upload_gate.status(),
            **service.backup_status(),
        }

    @app.post("/upload-batches", status_code=201, response_model=UploadBatchOut)
    def start_upload_batch(body: UploadBatchRequest):
        return create_batch(
            service,
            [file.model_dump(by_alias=True) for file in body.files],
            body.batch_id,
        )

    @app.get("/upload-batches", response_model=list[UploadBatchOut])
    def get_active_upload_batches(limit: int = Query(default=100, ge=1, le=1000)):
        return list_active_batches(service, limit)

    @app.get("/upload-batches/{batch_id}", response_model=UploadBatchOut)
    def get_upload_batch(batch_id: UUID):
        return describe_batch(service, batch_id)

    @app.delete("/upload-batches/{batch_id}", response_model=BatchAbandonedOut)
    def discard_upload_batch(batch_id: UUID):
        return abandon_batch(service, batch_id)

    @app.put("/upload-batches/{batch_id}/files/{file_id}", response_model=UploadFileReceipt)
    async def upload_file(batch_id: UUID, file_id: UUID, request: Request):
        header = request.headers.get("content-length")
        try:
            content_length = int(header) if header is not None else None
        except ValueError as error:
            raise HTTPException(400, "Invalid Content-Length header") from error
        return await receive_file(
            service,
            upload_gate,
            batch_id,
            file_id,
            request.stream(),
            content_length,
        )

    @app.post("/upload-batches/{batch_id}/seal", status_code=202, response_model=UploadBatchOut)
    def finish_upload_batch(batch_id: UUID):
        return seal_batch(service, batch_id)

    @app.post("/upload-batches/{batch_id}/retry", status_code=202, response_model=UploadBatchOut)
    def retry_upload_batch(batch_id: UUID):
        service.catalog.retry_upload_batch(batch_id)
        return describe_batch(service, batch_id)

    @app.get("/upload-queue", response_model=UploadQueueStatusOut)
    def upload_queue():
        return {**service.catalog.queue_counts(), **upload_gate.status()}

    @app.get("/assets", response_model=list[AssetDocOut])
    def list_assets(
        limit: int = Query(default=100, ge=1, le=1000), offset: int = Query(default=0, ge=0)
    ):
        return service.catalog.list_assets(limit, offset)

    @app.get("/library/assets", response_model=BrowsePageOut)
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

    @app.get("/assets/{asset_id}", response_model=AssetDetailOut)
    def get_asset(asset_id: UUID):
        manifest = find(asset_id)
        return {
            **manifest.document(),
            "technical": technical_fields(manifest.metadata),
            "processing": service.catalog.processing_status(str(asset_id)),
            "analysis": service.catalog.analysis_status(str(asset_id)),
            "preview": service.catalog.preview_status(str(asset_id)),
            "userState": service.catalog.user_state(str(asset_id)),
        }

    @app.post("/processing", status_code=202, response_model=QueueResultOut)
    def queue_processing(body: ProcessingRequest):
        return service.queue_processing(
            body.asset_ids,
            body.stages,
            body.include_deleted,
        )

    @app.post("/analysis", status_code=202, response_model=QueueResultOut)
    def queue_analysis(body: AnalysisRequest):
        return service.queue_analysis(
            body.asset_ids, body.include_deleted, body.force_full
        )

    @app.post("/assets/{asset_id}/analysis/retry", status_code=202, response_model=QueueResultOut)
    def retry_analysis(asset_id: UUID, force_full: bool = Query(default=False)):
        find(asset_id)
        return service.queue_analysis([asset_id], force_full=force_full)

    @app.get("/people", response_model=PeoplePageOut)
    def list_people(
        q: str = Query(default="", max_length=200),
        limit: int = Query(default=500, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
    ):
        return service.catalog.list_people(q, limit, offset)

    @app.get("/people/{person_id}", response_model=PersonDetailOut)
    def get_person(
        person_id: UUID,
        limit: int = Query(default=2000, ge=1, le=5000),
        offset: int = Query(default=0, ge=0),
    ):
        person = service.catalog.person_detail(str(person_id), limit, offset)
        if person is None:
            raise HTTPException(404, "Person not found")
        return person

    @app.patch("/people/{person_id}")
    def rename_person(person_id: UUID, body: PersonNameRequest):
        return service.catalog.commit_face_operation(
            body.operation_id,
            {
                "action": "person.rename",
                "personId": str(person_id),
                "displayName": body.display_name,
            },
        )

    @app.post("/people/{person_id}/merge")
    def merge_person(person_id: UUID, body: PersonMergeRequest):
        return service.catalog.commit_face_operation(
            body.operation_id,
            {
                "action": "person.merge",
                "sourcePersonId": str(person_id),
                "targetPersonId": str(body.target_person_id),
            },
        )

    @app.post("/faces/move")
    def move_faces(body: FaceMoveRequest):
        face_ids = [str(face_id) for face_id in body.face_ids]
        if len(set(face_ids)) != len(face_ids):
            raise HTTPException(422, "Face IDs must be unique")
        return service.catalog.commit_face_operation(
            body.operation_id,
            {
                "action": "faces.move",
                "faceIds": face_ids,
                "targetPersonId": str(body.target_person_id) if body.target_person_id else None,
            },
        )

    @app.get("/faces/{face_id}/thumbnail")
    def face_thumbnail(face_id: UUID):
        face = service.catalog.face(str(face_id))
        if face is None:
            raise HTTPException(404, "Face not found")
        manifest = service.catalog.get(face["asset_id"])
        if manifest is None:
            raise HTTPException(404, "Photograph not found")
        path = cache_paths(service, manifest)["preview"]
        if not path.exists():
            status = service.catalog.preview_status(str(manifest.asset_id))
            if status["status"] == "unavailable":
                raise HTTPException(404, "Photograph preview is unavailable")
            if status["status"] == "failed":
                raise HTTPException(503, "Photograph preview generation failed")
            service.catalog.queue_preview(str(manifest.asset_id))
            return JSONResponse(
                status_code=202,
                content={"status": "pending"},
                headers={"Retry-After": "2"},
            )
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
        return Response(
            output.getvalue(),
            media_type="image/jpeg",
            headers={"Cache-Control": "private, max-age=3600"},
        )

    @app.patch("/assets/{asset_id}/user-state", response_model=MutationResultOut)
    @app.patch("/assets/{asset_id}/metadata", response_model=MutationResultOut)
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

    @app.delete("/assets/{asset_id}", response_model=MutationResultOut)
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

    @app.post("/assets/{asset_id}/restore", response_model=MutationResultOut)
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

    @app.get("/assets/{asset_id}/burst", response_model=BurstDetailOut)
    def get_asset_burst(asset_id: UUID):
        find(asset_id)
        detail = service.catalog.burst_detail(str(asset_id))
        if detail is None:
            raise HTTPException(404, "Asset has no burst")
        return detail

    @app.post("/assets/{asset_id}/burst/representative", response_model=BurstRepresentativeOut)
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

    @app.get("/albums", response_model=list[AlbumOut])
    def list_albums(deleted: bool = False):
        return service.catalog.list_albums(deleted)

    @app.post("/albums", status_code=201, response_model=AlbumOut)
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

    @app.get("/albums/{album_id}", response_model=AlbumOut)
    def get_album(album_id: UUID):
        album = service.catalog.get_album(str(album_id))
        if album is None:
            raise HTTPException(404, "Album not found")
        return album.document()

    @app.patch("/albums/{album_id}", response_model=AlbumOut)
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

    @app.delete("/albums/{album_id}", response_model=AlbumOut)
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

    @app.post("/albums/{album_id}/restore", response_model=AlbumOut)
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

    @app.get("/assets/{asset_id}/original")
    def original(asset_id: UUID):
        manifest = find(asset_id)
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

    def derivative(asset_id: UUID, kind: str):
        manifest = find(asset_id)
        path = cache_paths(service, manifest)[kind]
        if not path.exists():
            status = service.catalog.preview_status(str(asset_id))
            if status["status"] == "unavailable":
                raise HTTPException(404, "Embedded preview unavailable")
            if status["status"] == "failed":
                raise HTTPException(503, "Preview generation failed; see asset status")
            service.catalog.queue_preview(str(asset_id))
            return JSONResponse(
                status_code=202, content={"status": "pending"}, headers={"Retry-After": "2"}
            )
        _record_access(service, manifest, str(asset_id))
        return FileResponse(
            path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"}
        )

    @app.get("/assets/{asset_id}/preview")
    def preview(asset_id: UUID):
        return derivative(asset_id, "preview")

    @app.get("/assets/{asset_id}/thumbnail")
    def thumbnail(asset_id: UUID):
        return derivative(asset_id, "thumbnail")

    @app.post("/assets/{asset_id}/preview/retry", response_model=PreviewStatusOut)
    def retry_preview(asset_id: UUID):
        find(asset_id)
        service.catalog.queue_preview(str(asset_id))
        return service.catalog.preview_status(str(asset_id))

    @app.post("/maintenance/verify")
    def verify_storage(full: bool = False):
        return service.verify(full)

    return app


app = create_app()
