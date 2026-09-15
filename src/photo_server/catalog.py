from contextlib import contextmanager
from time import time
from uuid import UUID, uuid5

from sqlalchemy import (
    BigInteger,
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    func,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, insert

from photo_server.config import LibraryError
from photo_server.models import Manifest

schema = MetaData()
library = Table(
    "library",
    schema,
    Column("singleton", Integer, primary_key=True),
    Column("library_id", String, nullable=False),
    Column("schema_version", Integer, nullable=False),
)
assets = Table(
    "assets",
    schema,
    Column("id", String, primary_key=True),
    Column("original_filename", Text, nullable=False),
    Column("sha256", String(64), nullable=False, unique=True),
    Column("state_revision", Integer, nullable=False),
    Column("manifest", JSONB, nullable=False),
)
blobs = Table(
    "blobs",
    schema,
    Column("id", String, primary_key=True),
    Column("asset_id", String, ForeignKey("assets.id"), nullable=False),
    Column("role", String, nullable=False),
    Column("object_key", Text, nullable=False, unique=True),
    Column("original_filename", Text, nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("size_bytes", BigInteger, nullable=False),
    Column("mime_type", String, nullable=False),
)
jobs = Table(
    "jobs",
    schema,
    Column("asset_id", String, ForeignKey("assets.id"), primary_key=True),
    Column("job_type", String, primary_key=True),
    Column("status", String, nullable=False),
    Column("attempts", Integer, nullable=False, default=0),
    Column("error", Text),
    Column("lease_until", BigInteger),
)
upload_batches = Table(
    "upload_batches",
    schema,
    Column("id", String, primary_key=True),
    Column("status", String, nullable=False),
    Column("created_at", BigInteger, nullable=False),
    Column("sealed_at", BigInteger),
    Column("error", Text),
)
upload_files = Table(
    "upload_files",
    schema,
    Column("id", String, primary_key=True),
    Column("batch_id", String, ForeignKey("upload_batches.id", ondelete="CASCADE"), nullable=False),
    Column("relative_path", Text, nullable=False),
    Column("original_filename", Text, nullable=False),
    Column("size_bytes", BigInteger, nullable=False),
    Column("mime_type", String),
    Column("staging_key", Text, nullable=False, unique=True),
    Column("required", Integer, nullable=False),
    Column("status", String, nullable=False),
    Column("reason", String),
    Column("sha256", String(64)),
    Column("asset_id", String),
    Column("error", Text),
    UniqueConstraint("batch_id", "relative_path"),
)
onboarding_jobs = Table(
    "onboarding_jobs",
    schema,
    Column("id", String, primary_key=True),
    Column("batch_id", String, ForeignKey("upload_batches.id", ondelete="CASCADE"), nullable=False),
    Column("primary_file_id", String, ForeignKey("upload_files.id"), nullable=False),
    Column("sidecar_file_ids", JSONB, nullable=False),
    Column("status", String, nullable=False),
    Column("attempts", Integer, nullable=False, default=0),
    Column("lease_until", BigInteger),
    Column("result", JSONB),
    Column("error", Text),
)
onboarding_claim_index = Index(
    "ix_onboarding_jobs_claim",
    onboarding_jobs.c.status,
    onboarding_jobs.c.lease_until,
)


class Catalog:
    def __init__(self, url: str):
        self.engine = create_engine(url, pool_pre_ping=True)

    @contextmanager
    def writer(self):
        # Serializes the CLI, API, and recovery against the same PostgreSQL database.
        with self.engine.connect() as connection:
            connection.execute(text("SELECT pg_advisory_lock(7046868301)"))
            connection.commit()
            try:
                yield
            finally:
                connection.execute(text("SELECT pg_advisory_unlock(7046868301)"))
                connection.commit()

    @contextmanager
    def digest_lock(self, digest: str):
        """Serialize deduplication for one content hash across worker threads/processes."""
        with self.engine.connect() as connection:
            connection.execute(
                text("SELECT pg_advisory_lock(hashtextextended(:digest, 0))"), {"digest": digest}
            )
            connection.commit()
            try:
                yield
            finally:
                connection.execute(
                    text("SELECT pg_advisory_unlock(hashtextextended(:digest, 0))"),
                    {"digest": digest},
                )
                connection.commit()

    def initialize(self, library_id: str):
        schema.create_all(self.engine)
        onboarding_claim_index.create(self.engine, checkfirst=True)
        with self.engine.begin() as connection:
            connection.execute(
                insert(library)
                .values(singleton=1, library_id=library_id, schema_version=1)
                .on_conflict_do_nothing()
            )
            row = connection.execute(select(library)).mappings().one()
            if row["library_id"] != library_id or row["schema_version"] != 1:
                raise LibraryError("Database belongs to a different library or schema version")

    def apply(self, manifest: Manifest):
        with self.engine.begin() as connection:
            existing = connection.execute(
                select(assets.c.manifest).where(assets.c.id == str(manifest.asset_id))
            ).scalar_one_or_none()
            if existing:
                if existing != manifest.document():
                    raise LibraryError(f"Conflicting immutable manifest for {manifest.asset_id}")
            else:
                connection.execute(
                    insert(assets).values(
                        id=str(manifest.asset_id),
                        original_filename=manifest.primary.original_filename,
                        sha256=manifest.primary.sha256,
                        state_revision=manifest.revision,
                        manifest=manifest.document(),
                    )
                )
                for blob in manifest.blobs:
                    connection.execute(
                        insert(blobs).values(
                            id=str(blob.blob_id),
                            asset_id=str(manifest.asset_id),
                            role=blob.role,
                            object_key=blob.object_key,
                            original_filename=blob.original_filename,
                            sha256=blob.sha256,
                            size_bytes=blob.size_bytes,
                            mime_type=blob.mime_type,
                        )
                    )
            connection.execute(
                insert(jobs)
                .values(
                    asset_id=str(manifest.asset_id),
                    job_type="preview-v1",
                    status="pending",
                    attempts=0,
                )
                .on_conflict_do_nothing()
            )

    def find_hash(self, digest: str) -> Manifest | None:
        with self.engine.connect() as connection:
            value = connection.execute(
                select(assets.c.manifest).where(assets.c.sha256 == digest)
            ).scalar_one_or_none()
        return Manifest.model_validate(value) if value else None

    def get(self, asset_id: str) -> Manifest | None:
        with self.engine.connect() as connection:
            value = connection.execute(
                select(assets.c.manifest).where(assets.c.id == asset_id)
            ).scalar_one_or_none()
        return Manifest.model_validate(value) if value else None

    def list_assets(self, limit: int = 100, offset: int = 0) -> list[dict]:
        with self.engine.connect() as connection:
            return list(
                connection.execute(
                    select(assets.c.manifest).order_by(assets.c.id).limit(limit).offset(offset)
                ).scalars()
            )

    def counts(self) -> dict:
        with self.engine.connect() as connection:
            return {
                "assets": connection.scalar(select(func.count()).select_from(assets)),
                "blobs": connection.scalar(select(func.count()).select_from(blobs)),
            }

    def claim_job(self) -> str | None:
        with self.engine.begin() as connection:
            row = connection.execute(
                text("""
                SELECT asset_id FROM jobs
                WHERE job_type = 'preview-v1' AND
                  (status = 'pending' OR (status = 'running' AND lease_until < EXTRACT(EPOCH FROM now())))
                ORDER BY asset_id FOR UPDATE SKIP LOCKED LIMIT 1
            """)
            ).first()
            if row is None:
                return None
            connection.execute(
                text("""
                UPDATE jobs SET status = 'running', attempts = attempts + 1,
                lease_until = EXTRACT(EPOCH FROM now())::bigint + 600
                WHERE asset_id = :asset_id AND job_type = 'preview-v1'
            """),
                {"asset_id": row[0]},
            )
            return row[0]

    def finish_job(self, asset_id: str, status: str, error: str | None = None):
        with self.engine.begin() as connection:
            connection.execute(
                jobs.update()
                .where(jobs.c.asset_id == asset_id)
                .values(status=status, error=error, lease_until=None)
            )

    def preview_status(self, asset_id: str) -> dict:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(jobs.c.status, jobs.c.error).where(jobs.c.asset_id == asset_id)
                )
                .mappings()
                .one_or_none()
            )
        return dict(row) if row else {"status": "missing", "error": None}

    def queue_preview(self, asset_id: str):
        with self.engine.begin() as connection:
            connection.execute(
                jobs.update()
                .where(jobs.c.asset_id == asset_id, jobs.c.status != "running")
                .values(status="pending", error=None)
            )

    def create_upload_batch(self, batch_id: UUID, files: list[dict]):
        now = int(time())
        with self.engine.begin() as connection:
            connection.execute(
                insert(upload_batches)
                .values(id=str(batch_id), status="accepting", created_at=now)
                .on_conflict_do_nothing()
            )
            for file in files:
                connection.execute(insert(upload_files).values(**file).on_conflict_do_nothing())
            stored = list(
                connection.execute(
                    select(upload_files).where(upload_files.c.batch_id == str(batch_id))
                ).mappings()
            )
            expected = {
                file["id"]: (
                    file["relative_path"],
                    file["size_bytes"],
                    file["mime_type"],
                    file["required"],
                    file["reason"],
                )
                for file in files
            }
            actual = {
                row["id"]: (
                    row["relative_path"],
                    row["size_bytes"],
                    row["mime_type"],
                    row["required"],
                    row["reason"],
                )
                for row in stored
            }
            if expected != actual:
                raise LibraryError("Batch ID was reused with a different file declaration")

    def upload_batch(self, batch_id: UUID | str) -> dict | None:
        with self.engine.connect() as connection:
            batch = (
                connection.execute(
                    select(upload_batches).where(upload_batches.c.id == str(batch_id))
                )
                .mappings()
                .one_or_none()
            )
            if batch is None:
                return None
            files = list(
                connection.execute(
                    select(upload_files)
                    .where(upload_files.c.batch_id == str(batch_id))
                    .order_by(upload_files.c.relative_path)
                ).mappings()
            )
            job_rows = list(
                connection.execute(
                    select(onboarding_jobs)
                    .where(onboarding_jobs.c.batch_id == str(batch_id))
                    .order_by(onboarding_jobs.c.id)
                ).mappings()
            )
        return {
            "batch": dict(batch),
            "files": [dict(row) for row in files],
            "jobs": [dict(row) for row in job_rows],
        }

    def begin_upload(self, batch_id: UUID, file_id: UUID) -> dict:
        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    select(upload_files, upload_batches.c.status.label("batch_status"))
                    .join(upload_batches, upload_files.c.batch_id == upload_batches.c.id)
                    .where(
                        upload_files.c.id == str(file_id),
                        upload_files.c.batch_id == str(batch_id),
                    )
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise LibraryError("Upload file does not belong to this batch")
            if not row["required"]:
                raise LibraryError("This companion was skipped by the batch selection rules")
            if row["batch_status"] != "accepting":
                raise LibraryError("This batch is already sealed")
            if row["status"] == "uploaded":
                return dict(row)
            if row["status"] == "uploading":
                raise LibraryError("This file is already being uploaded")
            connection.execute(
                upload_files.update()
                .where(upload_files.c.id == str(file_id))
                .values(status="uploading", error=None)
            )
            return dict(row)

    def complete_upload(self, file_id: UUID | str, sha256: str | None):
        with self.engine.begin() as connection:
            connection.execute(
                upload_files.update()
                .where(
                    upload_files.c.id == str(file_id),
                    upload_files.c.status.in_(["waiting", "uploading", "uploaded"]),
                )
                .values(status="uploaded", sha256=sha256, error=None)
            )

    def fail_upload(self, file_id: UUID | str, error: str):
        with self.engine.begin() as connection:
            connection.execute(
                upload_files.update()
                .where(upload_files.c.id == str(file_id), upload_files.c.status == "uploading")
                .values(status="waiting", error=error)
            )

    def seal_upload_batch(self, batch_id: UUID, plan: dict):
        with self.engine.begin() as connection:
            batch = (
                connection.execute(
                    select(upload_batches)
                    .where(upload_batches.c.id == str(batch_id))
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if batch is None:
                raise LibraryError("Upload batch not found")
            if batch["status"] != "accepting":
                return
            rows = list(
                connection.execute(
                    select(upload_files).where(upload_files.c.batch_id == str(batch_id))
                ).mappings()
            )
            by_path = {row["relative_path"]: row for row in rows}
            missing = [
                row["relative_path"]
                for row in rows
                if row["required"] and row["status"] != "uploaded"
            ]
            if missing:
                raise LibraryError(f"Upload these required files before sealing: {missing}")
            for asset in plan["assets"]:
                primary = by_path[asset["path"]]
                job_id = uuid5(batch_id, f"onboard:{asset['path']}")
                connection.execute(
                    insert(onboarding_jobs)
                    .values(
                        id=str(job_id),
                        batch_id=str(batch_id),
                        primary_file_id=primary["id"],
                        sidecar_file_ids=[by_path[path]["id"] for path in asset["sidecars"]],
                        status="pending",
                        attempts=0,
                    )
                    .on_conflict_do_nothing()
                )
            connection.execute(
                upload_files.update()
                .where(upload_files.c.batch_id == str(batch_id), upload_files.c.required == 1)
                .values(status="queued")
            )
            connection.execute(
                upload_batches.update()
                .where(upload_batches.c.id == str(batch_id))
                .values(status="queued", sealed_at=int(time()))
            )

    def claim_onboarding_job(self, lease_seconds: int = 900) -> dict | None:
        now = int(time())
        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    select(onboarding_jobs)
                    .where(
                        (onboarding_jobs.c.status == "pending")
                        | (
                            (onboarding_jobs.c.status == "running")
                            & (onboarding_jobs.c.lease_until < now)
                        )
                    )
                    .order_by(onboarding_jobs.c.id)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None
            ids = [row["primary_file_id"], *row["sidecar_file_ids"]]
            connection.execute(
                onboarding_jobs.update()
                .where(onboarding_jobs.c.id == row["id"])
                .values(
                    status="running",
                    attempts=onboarding_jobs.c.attempts + 1,
                    lease_until=now + lease_seconds,
                    error=None,
                )
            )
            connection.execute(
                upload_files.update()
                .where(upload_files.c.id.in_(ids))
                .values(status="processing", error=None)
            )
            connection.execute(
                upload_batches.update()
                .where(upload_batches.c.id == row["batch_id"])
                .values(status="processing")
            )
            file_rows = list(
                connection.execute(
                    select(upload_files).where(upload_files.c.id.in_(ids))
                ).mappings()
            )
            return {**dict(row), "files": {file["id"]: dict(file) for file in file_rows}}

    def finish_onboarding_job(
        self, job: dict, result: dict | None = None, error: str | None = None
    ):
        ids = [job["primary_file_id"], *job["sidecar_file_ids"]]
        with self.engine.begin() as connection:
            if error:
                connection.execute(
                    onboarding_jobs.update()
                    .where(onboarding_jobs.c.id == job["id"])
                    .values(status="failed", lease_until=None, error=error)
                )
                connection.execute(
                    upload_files.update()
                    .where(upload_files.c.id.in_(ids))
                    .values(status="failed", error=error)
                )
            else:
                outcome = result["status"]
                connection.execute(
                    onboarding_jobs.update()
                    .where(onboarding_jobs.c.id == job["id"])
                    .values(status="complete", lease_until=None, result=result, error=None)
                )
                connection.execute(
                    upload_files.update()
                    .where(upload_files.c.id.in_(ids))
                    .values(status=outcome, asset_id=result["assetId"], error=None)
                )
        self._refresh_upload_batch_status(job["batch_id"])

    def _refresh_upload_batch_status(self, batch_id: UUID | str):
        # Run after the job transaction commits. The last concurrent finisher then sees
        # every earlier completion instead of leaving the batch stuck at "processing".
        with self.engine.begin() as connection:
            remaining = connection.scalar(
                select(func.count())
                .select_from(onboarding_jobs)
                .where(
                    onboarding_jobs.c.batch_id == str(batch_id),
                    onboarding_jobs.c.status.in_(["pending", "running"]),
                )
            )
            failed = connection.scalar(
                select(func.count())
                .select_from(onboarding_jobs)
                .where(
                    onboarding_jobs.c.batch_id == str(batch_id),
                    onboarding_jobs.c.status == "failed",
                )
            )
            status = "processing" if remaining else ("failed" if failed else "complete")
            connection.execute(
                upload_batches.update()
                .where(upload_batches.c.id == str(batch_id))
                .values(status=status)
            )

    def retry_upload_batch(self, batch_id: UUID):
        with self.engine.begin() as connection:
            failed_ids = list(
                connection.execute(
                    select(onboarding_jobs.c.id).where(
                        onboarding_jobs.c.batch_id == str(batch_id),
                        onboarding_jobs.c.status == "failed",
                    )
                ).scalars()
            )
            if not failed_ids:
                raise LibraryError("This batch has no failed onboarding jobs")
            file_ids = list(
                connection.execute(
                    select(
                        onboarding_jobs.c.primary_file_id, onboarding_jobs.c.sidecar_file_ids
                    ).where(onboarding_jobs.c.id.in_(failed_ids))
                )
            )
            flat_ids = [value for primary, sidecars in file_ids for value in [primary, *sidecars]]
            connection.execute(
                onboarding_jobs.update()
                .where(onboarding_jobs.c.id.in_(failed_ids))
                .values(status="pending", error=None)
            )
            connection.execute(
                upload_files.update()
                .where(upload_files.c.id.in_(flat_ids))
                .values(status="queued", error=None)
            )
            connection.execute(
                upload_batches.update()
                .where(upload_batches.c.id == str(batch_id))
                .values(status="queued")
            )

    def queue_counts(self) -> dict:
        with self.engine.connect() as connection:
            return {
                "uploadBatchesQueued": connection.scalar(
                    select(func.count())
                    .select_from(upload_batches)
                    .where(upload_batches.c.status.in_(["queued", "processing"]))
                ),
                "onboardingPending": connection.scalar(
                    select(func.count())
                    .select_from(onboarding_jobs)
                    .where(onboarding_jobs.c.status == "pending")
                ),
                "onboardingRunning": connection.scalar(
                    select(func.count())
                    .select_from(onboarding_jobs)
                    .where(onboarding_jobs.c.status == "running")
                ),
                "onboardingFailed": connection.scalar(
                    select(func.count())
                    .select_from(onboarding_jobs)
                    .where(onboarding_jobs.c.status == "failed")
                ),
            }
