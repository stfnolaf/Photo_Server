from contextlib import contextmanager
from datetime import UTC, datetime
from datetime import time as day_time
from threading import RLock
from time import time
from uuid import UUID, uuid5

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
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
    tuple_,
)
from sqlalchemy.dialects.postgresql import JSONB, insert

from photo_server.browsing import BrowseQuery, asset_summary, browse_fields
from photo_server.config import LibraryError
from photo_server.migrations import migrate
from photo_server.models import Album, Manifest, Mutation, UserState

schema = MetaData()
library = Table(
    "library",
    schema,
    Column("singleton", Integer, primary_key=True),
    Column("library_id", String, nullable=False),
    Column("schema_version", Integer, nullable=False),
    Column("state_authority", String, nullable=False, server_default="postgres"),
)
assets = Table(
    "assets",
    schema,
    Column("id", String, primary_key=True),
    Column("original_filename", Text, nullable=False),
    Column("sha256", String(64), nullable=False, unique=True),
    Column("state_revision", Integer, nullable=False),
    Column("manifest", JSONB, nullable=False),
    Column("timeline_at", DateTime, nullable=False),
    Column("media_type", String, nullable=False),
    Column("search_text", Text, nullable=False),
    Column("rating", Integer, nullable=False, server_default="0"),
    Column("favorite", Boolean, nullable=False, server_default="false"),
    Column("deleted_at", Text),
)
albums = Table(
    "albums",
    schema,
    Column("id", String, primary_key=True),
    Column("state_revision", Integer, nullable=False),
    Column("state", JSONB, nullable=False),
    Column("deleted_at", Text),
)
album_assets = Table(
    "album_assets",
    schema,
    Column("album_id", String, ForeignKey("albums.id"), primary_key=True),
    Column("asset_id", String, ForeignKey("assets.id"), primary_key=True),
    Column("position", Integer, nullable=False),
)
operations = Table(
    "operations",
    schema,
    Column("id", String, primary_key=True),
    Column("request", JSONB, nullable=False),
    Column("result", JSONB, nullable=False),
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


class Catalog:
    def __init__(self, url: str):
        self.engine = create_engine(url, pool_pre_ping=True)
        self._writer_lock = RLock()

    @contextmanager
    def writer(self):
        # Serializes CLI, API, and onboarding writers across processes.
        with self._writer_lock, self.engine.connect() as connection:
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

    def initialize(self, library_id: str) -> dict:
        return migrate(self.engine, library_id)

    def existing_library_id(self) -> UUID | None:
        """Read identity without assuming that migrations have already run."""
        with self.engine.connect() as connection:
            if connection.scalar(text("SELECT to_regclass('public.library')")) is None:
                return None
            value = connection.scalar(text("SELECT library_id FROM library WHERE singleton = 1"))
        return UUID(value) if value else None

    def resume_interrupted_uploads(self) -> int:
        """Make request-scoped uploads retryable after an API process restart."""
        with self.engine.begin() as connection:
            result = connection.execute(
                upload_files.update()
                .where(upload_files.c.status == "uploading")
                .values(status="waiting", error="Interrupted upload; retry the file")
            )
        return result.rowcount

    def apply(self, manifest: Manifest):
        with self.engine.begin() as connection:
            self._apply(connection, manifest)

    def _apply(self, connection, manifest: Manifest):
        """Project one authoritative asset snapshot using the caller's transaction."""
        existing = connection.execute(
            select(assets.c.manifest)
            .where(assets.c.id == str(manifest.asset_id))
            .with_for_update()
        ).scalar_one_or_none()
        if existing:
            current = Manifest.model_validate(existing)
            from photo_server.state import import_identity

            if import_identity(current) != import_identity(manifest):
                raise LibraryError(f"Conflicting original identity for {manifest.asset_id}")
            if current.revision == manifest.revision and existing != manifest.document():
                raise LibraryError(f"Conflicting revision for {manifest.asset_id}")
            if manifest.revision <= current.revision:
                return
            connection.execute(
                assets.update()
                .where(assets.c.id == str(manifest.asset_id))
                .values(
                    manifest=manifest.document(),
                    state_revision=manifest.revision,
                    rating=manifest.user_state.rating,
                    favorite=manifest.user_state.favorite,
                    deleted_at=manifest.deleted_at,
                    **browse_fields(manifest),
                )
            )
        else:
            connection.execute(
                insert(assets).values(
                    id=str(manifest.asset_id),
                    original_filename=manifest.primary.original_filename,
                    sha256=manifest.primary.sha256,
                    state_revision=manifest.revision,
                    manifest=manifest.document(),
                    rating=manifest.user_state.rating,
                    favorite=manifest.user_state.favorite,
                    deleted_at=manifest.deleted_at,
                    **browse_fields(manifest),
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
        if not existing or (manifest.mutation and manifest.mutation.action == "asset.metadata"):
            # New imports already ran this stage locally. Worker reruns reach here
            # through asset.metadata before the claimed job is marked ready.
            connection.execute(
                insert(jobs)
                .values(
                    asset_id=str(manifest.asset_id),
                    job_type="metadata-v1",
                    status="ready",
                    attempts=1,
                )
                .on_conflict_do_nothing()
            )

    def find_hash(self, digest: str) -> Manifest | None:
        with self.engine.connect() as connection:
            value = connection.execute(
                select(assets.c.manifest).where(assets.c.sha256 == digest)
            ).scalar_one_or_none()
        manifest = Manifest.model_validate(value) if value else None
        if manifest and manifest.deleted_at:
            raise LibraryError(
                f"Matching asset {manifest.asset_id} is in trash; restore it before importing"
            )
        return manifest

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
                    select(assets.c.manifest)
                    .where(assets.c.deleted_at.is_(None))
                    .order_by(assets.c.id)
                    .limit(limit)
                    .offset(offset)
                ).scalars()
            )

    def all_assets(self) -> list[Manifest]:
        """Return every authoritative asset, including trash, for verify/export."""
        with self.engine.connect() as connection:
            values = list(connection.scalars(select(assets.c.manifest).order_by(assets.c.id)))
        return [Manifest.model_validate(value) for value in values]

    def counts(self) -> dict:
        with self.engine.connect() as connection:
            return {
                "assets": connection.scalar(select(func.count()).select_from(assets)),
                "blobs": connection.scalar(select(func.count()).select_from(blobs)),
            }

    def browse(self, query: BrowseQuery) -> dict:
        filters = [
            assets.c.deleted_at.is_not(None) if query.deleted else assets.c.deleted_at.is_(None)
        ]
        if query.album_id:
            filters.append(
                assets.c.id.in_(
                    select(album_assets.c.asset_id)
                    .join(albums)
                    .where(
                        album_assets.c.album_id == str(query.album_id),
                        albums.c.deleted_at.is_(None),
                    )
                )
            )
        if query.q:
            # Literal substring search: '%' and '_' in filenames are not wildcards.
            filters.append(assets.c.search_text.icontains(query.q, autoescape=True))
        if query.date_from:
            filters.append(assets.c.timeline_at >= datetime.combine(query.date_from, day_time.min))
        if query.date_to:
            filters.append(assets.c.timeline_at <= datetime.combine(query.date_to, day_time.max))
        if query.media_type:
            filters.append(assets.c.media_type == query.media_type)
        if query.rating_min:
            filters.append(assets.c.rating >= query.rating_min)
        if query.favorite is not None:
            filters.append(assets.c.favorite == query.favorite)
        statement = select(
            assets, jobs.c.status.label("preview_status"), jobs.c.error.label("preview_error")
        ).outerjoin(jobs, (jobs.c.asset_id == assets.c.id) & (jobs.c.job_type == "preview-v1"))
        statement = statement.where(*filters)
        cursor = query.decode_cursor()
        position = tuple_(assets.c.timeline_at, assets.c.id)
        if cursor:
            statement = statement.where(
                position < cursor if query.sort == "newest" else position > cursor
            )
        ordering = (assets.c.timeline_at, assets.c.id)
        if query.sort == "newest":
            ordering = tuple(column.desc() for column in ordering)
        with self.engine.connect().execution_options(
            isolation_level="REPEATABLE READ"
        ) as connection:
            total = connection.scalar(select(func.count()).select_from(assets).where(*filters))
            rows = (
                connection.execute(statement.order_by(*ordering).limit(query.limit + 1))
                .mappings()
                .all()
            )
        has_more = len(rows) > query.limit
        rows = rows[: query.limit]
        return {
            "items": [asset_summary(row) for row in rows],
            "total": total,
            "nextCursor": query.encode_cursor(rows[-1]) if has_more else None,
        }

    def user_state(self, asset_id: str) -> dict | None:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(assets.c.rating, assets.c.favorite, assets.c.manifest).where(
                        assets.c.id == asset_id
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return None
        state = Manifest.model_validate(row["manifest"]).user_state.document()
        return {**state, "rating": row["rating"], "favorite": row["favorite"]}

    def operation(self, operation_id: UUID) -> dict | None:
        with self.engine.connect() as connection:
            row = (
                connection.execute(select(operations).where(operations.c.id == str(operation_id)))
                .mappings()
                .one_or_none()
            )
        return dict(row) if row else None

    def commit_mutation(self, operation_id: UUID, mutation: Mutation) -> dict:
        """Apply state and record its retry result in one PostgreSQL transaction."""
        from photo_server.state import mutation_result

        with self.writer(), self.engine.begin() as connection:
            previous = (
                connection.execute(
                    select(operations)
                    .where(operations.c.id == str(operation_id))
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if previous:
                if previous["request"] != mutation.document():
                    raise LibraryError("Operation ID was reused with a different request")
                return previous["result"]

            entity_id = str(mutation.entity_id)
            kind, action = mutation.action.split(".")
            if kind == "asset":
                value = connection.scalar(
                    select(assets.c.manifest)
                    .where(assets.c.id == entity_id)
                    .with_for_update()
                )
                current = Manifest.model_validate(value) if value else None
            else:
                value = connection.scalar(
                    select(albums.c.state)
                    .where(albums.c.id == entity_id)
                    .with_for_update()
                )
                current = Album.model_validate(value) if value else None

            if current is None and mutation.action != "album.create":
                raise FileNotFoundError("Entity not found")
            if mutation.expected_revision is not None and (
                current is None or current.revision != mutation.expected_revision
            ):
                raise LibraryError("Revision changed; reload before editing")
            if current and current.deleted_at and action not in {"restore", "delete"}:
                raise LibraryError("Restore this item before editing")

            if kind == "asset":
                changes = {}
                if action in {"patch", "migrate"}:
                    changes["user_state"] = UserState.model_validate(
                        {**current.user_state.document(), **mutation.changes}
                    )
                elif action == "metadata":
                    changes["metadata"] = mutation.changes["metadata"]
                    changes["capture_time"] = mutation.changes.get("captureTime")
                elif action in {"delete", "restore"}:
                    changes["deleted_at"] = (
                        (current.deleted_at or datetime.now(UTC).isoformat())
                        if action == "delete"
                        else None
                    )
                else:
                    raise LibraryError("Invalid asset mutation")
                snapshot = Manifest.model_validate(
                    {
                        **current.model_dump(),
                        "schema_version": 2,
                        "revision": current.revision + 1,
                        "previous_revision": current.revision,
                        "operation_id": operation_id,
                        "mutation": mutation,
                        **changes,
                    }
                )
                self._apply(connection, snapshot)
            else:
                if action == "create" and current:
                    raise LibraryError("Album already exists")
                values = (
                    current.document()
                    if current
                    else {
                        "libraryId": connection.scalar(
                            select(library.c.library_id).where(library.c.singleton == 1)
                        ),
                        "albumId": entity_id,
                    }
                )
                if action in {"create", "patch"}:
                    values.update(mutation.changes)
                elif action in {"delete", "restore"}:
                    values["deletedAt"] = (
                        (current.deleted_at or datetime.now(UTC).isoformat())
                        if action == "delete"
                        else None
                    )
                else:
                    raise LibraryError("Invalid album mutation")
                for asset_id in values.get("assetIds", []):
                    asset_value = connection.scalar(
                        select(assets.c.manifest).where(assets.c.id == str(asset_id))
                    )
                    asset = Manifest.model_validate(asset_value) if asset_value else None
                    if asset is None:
                        raise LibraryError(f"Album asset does not exist: {asset_id}")
                    if asset.deleted_at and (
                        not current or UUID(str(asset_id)) not in current.asset_ids
                    ):
                        raise LibraryError(f"Restore asset before adding it to an album: {asset_id}")
                snapshot = Album.model_validate(
                    {
                        **values,
                        "revision": current.revision + 1 if current else 1,
                        "previousRevision": current.revision if current else None,
                        "operationId": str(operation_id),
                        "mutation": mutation.document(),
                    }
                )
                self._apply_album(connection, snapshot)

            result = mutation_result(snapshot)
            connection.execute(
                insert(operations).values(
                    id=str(operation_id), request=mutation.document(), result=result
                )
            )
            return result

    def migrate_legacy_user_state(self) -> int:
        """Move Phase 2 rating/favorite columns into authoritative asset snapshots."""
        with self.engine.connect() as connection:
            rows = list(
                connection.execute(
                    select(assets.c.id, assets.c.rating, assets.c.favorite, assets.c.manifest).where(
                        assets.c.state_revision == 1,
                        (assets.c.rating != 0) | assets.c.favorite,
                    )
                ).mappings()
            )
        migrated = 0
        for row in rows:
            manifest = Manifest.model_validate(row["manifest"])
            operation_id = uuid5(
                manifest.library_id, f"postgres-authority-migration:{manifest.asset_id}"
            )
            self.commit_mutation(
                operation_id,
                Mutation(
                    action="asset.migrate",
                    entity_id=manifest.asset_id,
                    changes={"rating": row["rating"], "favorite": row["favorite"]},
                ),
            )
            migrated += 1
        return migrated

    def library_id(self) -> UUID:
        with self.engine.connect() as connection:
            value = connection.scalar(select(library.c.library_id).where(library.c.singleton == 1))
        if value is None:
            raise LibraryError("Database library identity is missing")
        return UUID(value)

    def get_album(self, album_id: str) -> Album | None:
        with self.engine.connect() as connection:
            value = connection.scalar(select(albums.c.state).where(albums.c.id == album_id))
        return Album.model_validate(value) if value else None

    def list_albums(self, deleted: bool = False) -> list[dict]:
        with self.engine.connect() as connection:
            return list(
                connection.scalars(
                    select(albums.c.state)
                    .where(
                        albums.c.deleted_at.is_not(None)
                        if deleted
                        else albums.c.deleted_at.is_(None)
                    )
                    .order_by(albums.c.id)
                )
            )

    def all_albums(self) -> list[Album]:
        with self.engine.connect() as connection:
            values = list(connection.scalars(select(albums.c.state).order_by(albums.c.id)))
        return [Album.model_validate(value) for value in values]

    def _apply_album(self, connection, album: Album):
        current = connection.scalar(
            select(albums.c.state).where(albums.c.id == str(album.album_id)).with_for_update()
        )
        if current and current["revision"] >= album.revision:
            if current["revision"] == album.revision and current != album.document():
                raise LibraryError(f"Conflicting album revision: {album.album_id}")
            return
        connection.execute(
            insert(albums)
            .values(
                id=str(album.album_id),
                state_revision=album.revision,
                state=album.document(),
                deleted_at=album.deleted_at,
            )
            .on_conflict_do_update(
                index_elements=[albums.c.id],
                set_={
                    "state_revision": album.revision,
                    "state": album.document(),
                    "deleted_at": album.deleted_at,
                },
            )
        )
        connection.execute(
            album_assets.delete().where(album_assets.c.album_id == str(album.album_id))
        )
        if album.asset_ids:
            connection.execute(
                insert(album_assets),
                [
                    dict(album_id=str(album.album_id), asset_id=str(asset_id), position=position)
                    for position, asset_id in enumerate(album.asset_ids)
                ],
            )

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

    def claim_processing_job(self) -> dict | None:
        """Claim the next versioned processing stage before derived previews."""
        from photo_server.processing import PROCESSING_JOB_TYPES

        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    select(jobs.c.asset_id, jobs.c.job_type)
                    .where(
                        jobs.c.job_type.in_(PROCESSING_JOB_TYPES),
                        (jobs.c.status == "pending")
                        | ((jobs.c.status == "running") & (jobs.c.lease_until < int(time()))),
                    )
                    .order_by(jobs.c.job_type, jobs.c.asset_id)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return None
            connection.execute(
                jobs.update()
                .where(
                    jobs.c.asset_id == row["asset_id"],
                    jobs.c.job_type == row["job_type"],
                )
                .values(
                    status="running",
                    attempts=jobs.c.attempts + 1,
                    lease_until=int(time()) + 900,
                    error=None,
                )
            )
            return dict(row)

    def finish_processing_job(
        self, asset_id: str, job_type: str, status: str, error: str | None = None
    ):
        with self.engine.begin() as connection:
            connection.execute(
                jobs.update()
                .where(jobs.c.asset_id == asset_id, jobs.c.job_type == job_type)
                .values(status=status, error=error, lease_until=None)
            )

    def queue_processing(
        self,
        asset_ids: list[str] | None,
        job_types: list[str],
        include_deleted: bool = False,
    ) -> dict:
        """Queue stages for explicit assets, or every eligible library asset."""
        with self.engine.begin() as connection:
            statement = select(assets.c.id)
            if asset_ids is not None:
                requested = set(asset_ids)
                if not requested:
                    raise LibraryError("Provide at least one asset ID or process the whole library")
                selected = set(connection.scalars(statement.where(assets.c.id.in_(requested))))
                missing = requested - selected
                if missing:
                    raise FileNotFoundError(f"Assets not found: {', '.join(sorted(missing))}")
            else:
                if not include_deleted:
                    statement = statement.where(assets.c.deleted_at.is_(None))
                selected = set(connection.scalars(statement))

            queued = 0
            already_running = 0
            for asset_id in sorted(selected):
                for job_type in job_types:
                    current_status = connection.scalar(
                        select(jobs.c.status)
                        .where(
                            jobs.c.asset_id == asset_id,
                            jobs.c.job_type == job_type,
                        )
                        .with_for_update()
                    )
                    if current_status == "running":
                        already_running += 1
                        continue
                    connection.execute(
                        insert(jobs)
                        .values(
                            asset_id=asset_id,
                            job_type=job_type,
                            status="pending",
                            attempts=0,
                        )
                        .on_conflict_do_update(
                            index_elements=[jobs.c.asset_id, jobs.c.job_type],
                            set_={"status": "pending", "error": None, "lease_until": None},
                            where=jobs.c.status != "running",
                        )
                    )
                    queued += 1
        return {
            "assets": len(selected),
            "jobsQueued": queued,
            "jobsAlreadyRunning": already_running,
            "jobTypes": job_types,
        }

    def processing_status(self, asset_id: str) -> list[dict]:
        from photo_server.processing import PROCESSING_JOB_TYPES

        with self.engine.connect() as connection:
            rows = connection.execute(
                select(jobs.c.job_type, jobs.c.status, jobs.c.attempts, jobs.c.error).where(
                    jobs.c.asset_id == asset_id,
                    jobs.c.job_type.in_(PROCESSING_JOB_TYPES),
                )
            ).mappings()
            return [dict(row) for row in rows]

    def finish_job(self, asset_id: str, status: str, error: str | None = None):
        with self.engine.begin() as connection:
            connection.execute(
                jobs.update()
                .where(jobs.c.asset_id == asset_id, jobs.c.job_type == "preview-v1")
                .values(status=status, error=error, lease_until=None)
            )

    def preview_status(self, asset_id: str) -> dict:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(jobs.c.status, jobs.c.error).where(
                        jobs.c.asset_id == asset_id, jobs.c.job_type == "preview-v1"
                    )
                )
                .mappings()
                .one_or_none()
            )
        return dict(row) if row else {"status": "missing", "error": None}

    def queue_preview(self, asset_id: str):
        with self.engine.begin() as connection:
            connection.execute(
                insert(jobs)
                .values(asset_id=asset_id, job_type="preview-v1", status="pending", attempts=0)
                .on_conflict_do_update(
                    index_elements=[jobs.c.asset_id, jobs.c.job_type],
                    set_={"status": "pending", "error": None},
                    where=jobs.c.status != "running",
                )
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
        from photo_server.processing import PROCESSING_JOB_TYPES

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
                "processingPending": connection.scalar(
                    select(func.count())
                    .select_from(jobs)
                    .where(
                        jobs.c.job_type.in_(PROCESSING_JOB_TYPES),
                        jobs.c.status == "pending",
                    )
                ),
                "processingRunning": connection.scalar(
                    select(func.count())
                    .select_from(jobs)
                    .where(
                        jobs.c.job_type.in_(PROCESSING_JOB_TYPES),
                        jobs.c.status == "running",
                    )
                ),
                "processingFailed": connection.scalar(
                    select(func.count())
                    .select_from(jobs)
                    .where(
                        jobs.c.job_type.in_(PROCESSING_JOB_TYPES),
                        jobs.c.status == "failed",
                    )
                ),
            }
