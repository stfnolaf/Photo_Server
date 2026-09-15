from contextlib import contextmanager

from sqlalchemy import (
    BigInteger,
    Column,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
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

    def initialize(self, library_id: str):
        schema.create_all(self.engine)
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
