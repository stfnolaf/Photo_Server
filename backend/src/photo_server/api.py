from contextlib import asynccontextmanager
from typing import Annotated
from urllib.parse import quote
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from photo_server.browsing import BrowseQuery, UserStatePatch
from photo_server.config import LibraryError, Settings
from photo_server.service import Service
from photo_server.uploads import (
    UploadGate,
    create_batch,
    describe_batch,
    receive_file,
    seal_batch,
)
from photo_server.worker import cache_paths


class UploadFileDeclaration(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    path: str = Field(min_length=1, max_length=1024)
    size_bytes: int = Field(gt=0)
    mime_type: str | None = Field(default=None, max_length=255)


class UploadBatchRequest(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    batch_id: UUID | None = None
    files: list[UploadFileDeclaration] = Field(min_length=1, max_length=10000)


def create_app(settings: Settings | None = None) -> FastAPI:
    service = Service(settings or Settings())
    upload_gate = UploadGate(service.settings.upload_workers)

    @asynccontextmanager
    async def lifespan(app):
        service.initialize()
        result = service.recover()
        if result["errors"]:
            raise RuntimeError(f"Library recovery requires attention: {result['errors']}")
        app.state.service = service
        app.state.upload_gate = upload_gate
        yield
        service.catalog.engine.dispose()

    app = FastAPI(title="Photo Server", version="0.2.0", lifespan=lifespan)
    origins = [
        origin.strip() for origin in service.settings.cors_origins.split(",") if origin.strip()
    ]
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_methods=["GET", "POST", "PUT", "PATCH"],
            allow_headers=["Content-Type", "Content-Length"],
        )

    @app.exception_handler(LibraryError)
    async def library_error(request: Request, error: LibraryError):
        return JSONResponse(status_code=409, content={"detail": str(error)})

    @app.exception_handler(FileNotFoundError)
    async def missing_file(request: Request, error: FileNotFoundError):
        return JSONResponse(status_code=404, content={"detail": "An input file does not exist"})

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/docs")

    @app.get("/health")
    def health():
        service.storage.client.head_bucket(Bucket=service.storage.bucket)
        return {
            "status": "ok",
            "libraryId": str(service.library_id),
            **service.catalog.counts(),
            **service.catalog.queue_counts(),
            **upload_gate.status(),
        }

    @app.post("/upload-batches", status_code=201)
    def start_upload_batch(body: UploadBatchRequest):
        return create_batch(
            service,
            [file.model_dump(by_alias=True) for file in body.files],
            body.batch_id,
        )

    @app.get("/upload-batches/{batch_id}")
    def get_upload_batch(batch_id: UUID):
        return describe_batch(service, batch_id)

    @app.put("/upload-batches/{batch_id}/files/{file_id}")
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

    @app.post("/upload-batches/{batch_id}/seal", status_code=202)
    def finish_upload_batch(batch_id: UUID):
        return seal_batch(service, batch_id)

    @app.post("/upload-batches/{batch_id}/retry", status_code=202)
    def retry_upload_batch(batch_id: UUID):
        service.catalog.retry_upload_batch(batch_id)
        return describe_batch(service, batch_id)

    @app.get("/upload-queue")
    def upload_queue():
        return {**service.catalog.queue_counts(), **upload_gate.status()}

    @app.get("/assets")
    def list_assets(
        limit: int = Query(default=100, ge=1, le=1000), offset: int = Query(default=0, ge=0)
    ):
        return service.catalog.list_assets(limit, offset)

    @app.get("/library/assets")
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

    @app.get("/assets/{asset_id}")
    def get_asset(asset_id: UUID):
        manifest = find(asset_id)
        return {
            **manifest.document(),
            "preview": service.catalog.preview_status(str(asset_id)),
            "userState": service.catalog.user_state(str(asset_id)),
        }

    @app.patch("/assets/{asset_id}/user-state")
    def update_user_state(asset_id: UUID, body: UserStatePatch):
        state = service.catalog.update_user_state(
            str(asset_id), body.model_dump(exclude_unset=True)
        )
        if state is None:
            raise HTTPException(404, "Asset not found")
        return state

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
        return FileResponse(
            path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"}
        )

    @app.get("/assets/{asset_id}/preview")
    def preview(asset_id: UUID):
        return derivative(asset_id, "preview")

    @app.get("/assets/{asset_id}/thumbnail")
    def thumbnail(asset_id: UUID):
        return derivative(asset_id, "thumbnail")

    @app.post("/assets/{asset_id}/preview/retry")
    def retry_preview(asset_id: UUID):
        find(asset_id)
        service.catalog.queue_preview(str(asset_id))
        return service.catalog.preview_status(str(asset_id))

    @app.post("/maintenance/reconcile")
    def reconcile(verify: bool = False):
        return service.recover(verify)

    return app


app = create_app()
