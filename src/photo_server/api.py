from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field

from photo_server.config import LibraryError, Settings
from photo_server.service import Service
from photo_server.worker import cache_paths


class PlanRequest(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=10000)


class ImportRequest(PlanRequest):
    operation_id: UUID


def create_app(settings: Settings | None = None) -> FastAPI:
    service = Service(settings or Settings())

    @asynccontextmanager
    async def lifespan(app):
        service.initialize()
        result = service.recover()
        if result["errors"]:
            raise RuntimeError(f"Library recovery requires attention: {result['errors']}")
        app.state.service = service
        yield
        service.catalog.engine.dispose()

    app = FastAPI(title="Photo Server", version="0.1.0", lifespan=lifespan)

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
        return {"status": "ok", "libraryId": str(service.library_id), **service.catalog.counts()}

    @app.post("/imports/plan")
    def plan(body: PlanRequest):
        return service.plan(body.paths)

    @app.post("/imports")
    def import_batch(body: ImportRequest):
        return service.import_batch(body.paths, body.operation_id)

    @app.get("/assets")
    def list_assets(
        limit: int = Query(default=100, ge=1, le=1000), offset: int = Query(default=0, ge=0)
    ):
        return service.catalog.list_assets(limit, offset)

    def find(asset_id: UUID):
        manifest = service.catalog.get(str(asset_id))
        if manifest is None:
            raise HTTPException(404, "Asset not found")
        return manifest

    @app.get("/assets/{asset_id}")
    def get_asset(asset_id: UUID):
        manifest = find(asset_id)
        return {**manifest.document(), "preview": service.catalog.preview_status(str(asset_id))}

    @app.get("/assets/{asset_id}/original")
    def original(asset_id: UUID):
        manifest = find(asset_id)
        if service.storage.head(manifest.primary.object_key) is None:
            raise HTTPException(503, "Original is missing from storage")
        return StreamingResponse(
            service.storage.chunks(manifest.primary.object_key),
            media_type=manifest.primary.mime_type,
            headers={"Content-Length": str(manifest.primary.size_bytes)},
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
        return FileResponse(path, media_type="image/jpeg")

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
