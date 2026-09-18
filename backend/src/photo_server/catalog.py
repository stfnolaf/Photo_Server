from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from datetime import time as day_time
from threading import RLock
from time import time
from uuid import UUID, uuid4, uuid5

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    and_,
    create_engine,
    func,
    or_,
    select,
    text,
    tuple_,
)
from sqlalchemy.dialects.postgresql import JSONB, insert

from photo_server.browsing import BrowseQuery, asset_summary, browse_fields, camera_time
from photo_server.config import LibraryError, Settings
from photo_server.fingerprints import BURST_HASH_VERSION, Candidate, Fingerprint
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
    Column("force_full", Boolean, nullable=False, server_default="false"),
)
upload_batches = Table(
    "upload_batches",
    schema,
    Column("id", String, primary_key=True),
    Column("status", String, nullable=False),
    Column("created_at", BigInteger, nullable=False),
    Column("updated_at", BigInteger, nullable=False),
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
analysis_runs = Table(
    "analysis_runs",
    schema,
    Column("id", String, primary_key=True),
    Column("asset_id", String, ForeignKey("assets.id", ondelete="CASCADE"), nullable=False),
    Column("analysis_type", String, nullable=False),
    Column("model_name", Text, nullable=False),
    Column("model_version", Text, nullable=False),
    Column("pipeline_version", Text, nullable=False),
    Column("input_hash", String(64), nullable=False),
    Column("object_key", Text, nullable=False, unique=True),
    Column("result", JSONB, nullable=False),
    Column("searchable_text", Text, nullable=False),
    Column("is_current", Boolean, nullable=False, default=True),
    Column("semantic_origin", String, nullable=False, server_default="computed"),
    Column("source_run_id", String, ForeignKey("analysis_runs.id", ondelete="SET NULL")),
    Column("reuse_policy_version", Text),
    Column("similarity", JSONB),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
people = Table(
    "people",
    schema,
    Column("id", String, primary_key=True),
    Column("display_name", Text, nullable=False, default=""),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
faces = Table(
    "faces",
    schema,
    Column("id", String, primary_key=True),
    Column("asset_id", String, ForeignKey("assets.id", ondelete="CASCADE"), nullable=False),
    Column(
        "analysis_run_id",
        String,
        ForeignKey("analysis_runs.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("person_id", String, ForeignKey("people.id"), nullable=False),
    Column("face_index", Integer, nullable=False),
    Column("bounding_box", JSONB, nullable=False),
    Column("confidence", Float, nullable=False),
    Column("embedding", JSONB, nullable=False),
)
image_fingerprints = Table(
    "image_fingerprints",
    schema,
    Column("asset_id", String, ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True),
    Column("algorithm_version", Text, primary_key=True),
    Column("phash", String(16), nullable=False),
    Column("dhash", String(16), nullable=False),
    Column("width", Integer, nullable=False),
    Column("height", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
burst_clusters = Table(
    "burst_clusters",
    schema,
    Column("id", String, primary_key=True),
    Column(
        "representative_asset_id",
        String,
        ForeignKey("assets.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("policy_version", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
burst_members = Table(
    "burst_members",
    schema,
    Column(
        "cluster_id",
        String,
        ForeignKey("burst_clusters.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "asset_id",
        String,
        ForeignKey("assets.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


class Catalog:
    def __init__(self, url: str, settings: Settings | None = None):
        self.engine = create_engine(url, pool_pre_ping=True)
        self._writer_lock = RLock()
        if settings is None:
            self._burst_phash_max = 4
            self._burst_dhash_max = 6
        else:
            self._burst_phash_max = settings.burst_cluster_phash_max_distance
            self._burst_dhash_max = settings.burst_cluster_dhash_max_distance

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
            select(assets.c.manifest).where(assets.c.id == str(manifest.asset_id)).with_for_update()
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
        connection.execute(
            insert(jobs)
            .values(
                asset_id=str(manifest.asset_id),
                job_type="ai-v1",
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
            semantic_match = (
                select(analysis_runs.c.id)
                .where(
                    analysis_runs.c.asset_id == assets.c.id,
                    analysis_runs.c.is_current,
                    analysis_runs.c.searchable_text.icontains(query.q, autoescape=True),
                )
                .exists()
            )
            person_match = (
                select(faces.c.id)
                .select_from(
                    faces.join(analysis_runs, faces.c.analysis_run_id == analysis_runs.c.id).join(
                        people, faces.c.person_id == people.c.id
                    )
                )
                .where(
                    faces.c.asset_id == assets.c.id,
                    analysis_runs.c.is_current,
                    people.c.display_name.icontains(query.q, autoescape=True),
                )
                .exists()
            )
            filters.append(
                or_(
                    assets.c.search_text.icontains(query.q, autoescape=True),
                    semantic_match,
                    person_match,
                )
            )
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
        member_count_alias = burst_members.alias("member_count")
        member_count = (
            select(func.count())
            .select_from(member_count_alias)
            .where(member_count_alias.c.cluster_id == burst_members.c.cluster_id)
            .scalar_subquery()
        )
        statement = select(
            assets,
            jobs.c.status.label("preview_status"),
            jobs.c.error.label("preview_error"),
            burst_members.c.cluster_id.label("burst_id"),
            burst_clusters.c.representative_asset_id.label("burst_representative"),
            member_count.label("burst_size"),
        ).outerjoin(
            jobs, (jobs.c.asset_id == assets.c.id) & (jobs.c.job_type == "preview-v1")
        ).outerjoin(burst_members, burst_members.c.asset_id == assets.c.id).outerjoin(
            burst_clusters, burst_clusters.c.id == burst_members.c.cluster_id
        )
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
        from photo_server.bursts import remove_member, set_representative
        from photo_server.state import mutation_result

        with self.writer(), self.engine.begin() as connection:
            previous = (
                connection.execute(
                    select(operations).where(operations.c.id == str(operation_id)).with_for_update()
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
                    select(assets.c.manifest).where(assets.c.id == entity_id).with_for_update()
                )
                current = Manifest.model_validate(value) if value else None
            elif kind == "album":
                value = connection.scalar(
                    select(albums.c.state).where(albums.c.id == entity_id).with_for_update()
                )
                current = Album.model_validate(value) if value else None
            else:
                current = None

            if kind != "burst":
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
                if action == "delete":
                    remove_member(connection, entity_id)
                elif action == "restore":
                    self._join_burst(connection, entity_id)
                result = mutation_result(snapshot)
            elif kind == "burst":
                if action != "setRepresentative":
                    raise LibraryError("Invalid burst mutation")
                result = set_representative(
                    connection,
                    entity_id,
                    mutation.changes.get("representativeAssetId"),
                )
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
                        raise LibraryError(
                            f"Restore asset before adding it to an album: {asset_id}"
                        )
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
                    select(
                        assets.c.id, assets.c.rating, assets.c.favorite, assets.c.manifest
                    ).where(
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
        force_full: bool = False,
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
            already_queued = 0
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
                    if current_status == "pending":
                        if force_full:
                            # A forceFull retry must override an already-queued
                            # job, or the worker would reuse instead of recomputing.
                            connection.execute(
                                jobs.update()
                                .where(jobs.c.asset_id == asset_id, jobs.c.job_type == job_type)
                                .values(force_full=True)
                            )
                        already_queued += 1
                        continue
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
                            force_full=force_full,
                        )
                        .on_conflict_do_update(
                            index_elements=[jobs.c.asset_id, jobs.c.job_type],
                            set_={
                                "status": "pending",
                                "error": None,
                                "lease_until": None,
                                "force_full": force_full,
                            },
                            where=jobs.c.status != "running",
                        )
                    )
                    queued += 1
        return {
            "assets": len(selected),
            "jobsQueued": queued,
            "jobsAlreadyQueued": already_queued,
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

    def upsert_fingerprint(self, asset_id: str, fingerprint: Fingerprint):
        """Persist a fingerprint idempotently for (asset_id, algorithm_version)."""
        with self.engine.begin() as connection:
            connection.execute(
                insert(image_fingerprints)
                .values(
                    asset_id=asset_id,
                    algorithm_version=fingerprint.algorithm_version,
                    phash=fingerprint.phash,
                    dhash=fingerprint.dhash,
                    width=fingerprint.width,
                    height=fingerprint.height,
                )
                .on_conflict_do_nothing(index_elements=["asset_id", "algorithm_version"])
            )
            if fingerprint.algorithm_version == BURST_HASH_VERSION:
                self._join_burst(connection, asset_id)

    def _join_burst(self, connection, asset_id: str):
        """Compute burst membership for one fingerprinted frame in the caller's transaction."""
        from photo_server.bursts import join_or_create_cluster

        row = (
            connection.execute(
                select(image_fingerprints).where(
                    image_fingerprints.c.asset_id == asset_id,
                    image_fingerprints.c.algorithm_version == BURST_HASH_VERSION,
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return
        fingerprint = Fingerprint(
            algorithm_version=row["algorithm_version"],
            phash=row["phash"],
            dhash=row["dhash"],
            width=row["width"],
            height=row["height"],
        )
        join_or_create_cluster(
            connection,
            asset_id,
            fingerprint,
            self._burst_phash_max,
            self._burst_dhash_max,
        )

    def get_fingerprint(self, asset_id: str, version: str) -> Fingerprint | None:
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(image_fingerprints).where(
                        image_fingerprints.c.asset_id == asset_id,
                        image_fingerprints.c.algorithm_version == version,
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            return None
        return Fingerprint(
            algorithm_version=row["algorithm_version"],
            phash=row["phash"],
            dhash=row["dhash"],
            width=row["width"],
            height=row["height"],
        )

    def find_fingerprint_candidates(self, target_asset_id: str, version: str) -> list[Candidate]:
        """Small candidate set for burst matching.

        SQL filters on algorithm version, capture-time window, camera identity,
        and dimensions; Hamming distance is applied in Python by the caller.
        """
        with self.engine.connect() as connection:
            target = (
                connection.execute(
                    select(assets, image_fingerprints)
                    .join_from(
                        assets,
                        image_fingerprints,
                        (image_fingerprints.c.asset_id == assets.c.id)
                        & (image_fingerprints.c.algorithm_version == version),
                    )
                    .where(assets.c.id == target_asset_id)
                )
                .mappings()
                .one_or_none()
            )
            if target is None:
                return []
            manifest = Manifest.model_validate(target["manifest"])
            capture = camera_time(manifest.capture_time)
            if capture is None:
                return []
            filters = [
                assets.c.id != target_asset_id,
                assets.c.deleted_at.is_(None),
                image_fingerprints.c.algorithm_version == version,
                image_fingerprints.c.width == target["width"],
                image_fingerprints.c.height == target["height"],
                assets.c.manifest["captureTime"].astext.isnot(None),
                assets.c.timeline_at >= capture - timedelta(seconds=3),
                assets.c.timeline_at <= capture + timedelta(seconds=3),
            ]
            for field in ("Make", "Model"):
                value = manifest.metadata.get(field)
                if value is not None:
                    filters.append(
                        or_(
                            assets.c.manifest["metadata"][field].astext == value,
                            assets.c.manifest["metadata"][field].astext.is_(None),
                        )
                    )
            rows = (
                connection.execute(
                    select(
                        assets.c.id,
                        image_fingerprints.c.phash,
                        image_fingerprints.c.dhash,
                        assets.c.timeline_at,
                    )
                    .select_from(
                        assets.join(
                            image_fingerprints,
                            (image_fingerprints.c.asset_id == assets.c.id)
                            & (image_fingerprints.c.algorithm_version == version),
                        )
                    )
                    .where(*filters)
                    .order_by(assets.c.id)
                )
                .mappings()
                .all()
            )
        return [
            Candidate(
                asset_id=row["id"],
                phash=row["phash"],
                dhash=row["dhash"],
                capture_time=row["timeline_at"],
            )
            for row in rows
        ]

    def burst_detail(self, asset_id: str) -> dict | None:
        """The burst containing one asset: its ID, representative, and member frames."""
        with self.engine.connect() as connection:
            cluster = (
                connection.execute(
                    select(burst_clusters)
                    .join_from(
                        burst_members,
                        burst_clusters,
                        burst_clusters.c.id == burst_members.c.cluster_id,
                    )
                    .where(burst_members.c.asset_id == asset_id)
                )
                .mappings()
                .one_or_none()
            )
            if cluster is None:
                return None
            cluster_id = cluster["id"]
            representative = cluster["representative_asset_id"]
            rows = (
                connection.execute(
                    select(
                        assets,
                        jobs.c.status.label("preview_status"),
                        jobs.c.error.label("preview_error"),
                    )
                    .outerjoin(
                        jobs,
                        (jobs.c.asset_id == assets.c.id) & (jobs.c.job_type == "preview-v1"),
                    )
                    .where(
                        assets.c.id.in_(
                            select(burst_members.c.asset_id).where(
                                burst_members.c.cluster_id == cluster_id
                            )
                        )
                    )
                    .order_by(assets.c.timeline_at, assets.c.id)
                )
                .mappings()
                .all()
            )
        return {
            "burstId": cluster_id,
            "representativeAssetId": representative,
            "frames": [asset_summary(row) for row in rows],
        }

    def claim_ai_job(self) -> dict | None:
        """Claim AI only after the ordinary worker has resolved the preview."""
        ai_jobs = jobs.alias("ai_jobs")
        preview_jobs = jobs.alias("preview_jobs")
        now = int(time())
        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    select(
                        ai_jobs.c.asset_id,
                        ai_jobs.c.force_full,
                        preview_jobs.c.status.label("preview_status"),
                    )
                    .select_from(
                        ai_jobs.join(
                            preview_jobs,
                            (preview_jobs.c.asset_id == ai_jobs.c.asset_id)
                            & (preview_jobs.c.job_type == "preview-v1"),
                        )
                    )
                    .where(
                        ai_jobs.c.job_type == "ai-v1",
                        (ai_jobs.c.status == "pending")
                        | ((ai_jobs.c.status == "running") & (ai_jobs.c.lease_until < now)),
                        preview_jobs.c.status.in_(["ready", "failed", "unavailable"]),
                    )
                    .order_by(ai_jobs.c.asset_id)
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
                .where(jobs.c.asset_id == row["asset_id"], jobs.c.job_type == "ai-v1")
                .values(
                    status="running",
                    attempts=jobs.c.attempts + 1,
                    lease_until=now + 1800,
                    error=None,
                )
            )
            return dict(row)

    def finish_ai_job(self, asset_id: str, status: str, error: str | None = None):
        with self.engine.begin() as connection:
            connection.execute(
                jobs.update()
                .where(jobs.c.asset_id == asset_id, jobs.c.job_type == "ai-v1")
                .values(status=status, error=error, lease_until=None)
            )

    def queue_ai(
        self,
        asset_ids: list[str] | None,
        include_deleted: bool = False,
        force_full: bool = False,
    ) -> dict:
        return self.queue_processing(asset_ids, ["ai-v1"], include_deleted, force_full)

    def complete_ai_analysis(
        self,
        *,
        asset_id: str,
        run_id: str,
        model_name: str,
        model_version: str,
        pipeline_version: str,
        input_hash: str,
        object_key: str,
        result: dict,
        searchable: str,
        detected_faces: list[dict],
        match_threshold: float,
        created_at: str,
        semantic_origin: str = "computed",
        source_run_id: str | None = None,
        reuse_policy_version: str | None = None,
        similarity: dict | None = None,
    ) -> dict:
        """Atomically publish a run, cluster its faces, and finish its job."""
        from math import sqrt
        from uuid import uuid4

        def unit(vector):
            length = sqrt(sum(value * value for value in vector)) or 1.0
            return [value / length for value in vector]

        with self.engine.begin() as connection:
            # Face centroids and current-run replacement must be serialized even
            # if an operator deliberately starts more than one AI worker.
            connection.execute(text("SELECT pg_advisory_xact_lock(7046868303)"))
            current_hash = connection.scalar(
                select(assets.c.sha256).where(assets.c.id == asset_id).with_for_update()
            )
            if current_hash is None:
                raise FileNotFoundError("AI job references a missing asset")
            if current_hash != input_hash:
                raise LibraryError("Asset changed while AI analysis was running")

            face_table = faces
            centroid_rows = connection.execute(
                select(face_table.c.person_id, face_table.c.embedding)
                .select_from(
                    face_table.join(
                        analysis_runs,
                        face_table.c.analysis_run_id == analysis_runs.c.id,
                    )
                )
                .where(analysis_runs.c.is_current)
            )
            sums: dict[str, list[float]] = {}
            for person_id, embedding in centroid_rows:
                vector = unit(embedding)
                if person_id not in sums:
                    sums[person_id] = [0.0] * len(vector)
                sums[person_id] = [a + b for a, b in zip(sums[person_id], vector, strict=True)]

            assigned = []
            used: set[str] = set()
            for index, face in enumerate(detected_faces):
                vector = unit(face["embedding"])
                best_person, best_score = None, -1.0
                for person_id, total in sums.items():
                    if person_id in used:
                        continue
                    center = unit(total)
                    score = sum(a * b for a, b in zip(center, vector, strict=True))
                    if score > best_score:
                        best_person, best_score = person_id, score
                if best_person is None or best_score <= match_threshold:
                    best_person = str(uuid4())
                    connection.execute(
                        insert(people).values(
                            id=best_person,
                            display_name="",
                            created_at=datetime.fromisoformat(created_at),
                        )
                    )
                    sums[best_person] = [0.0] * len(vector)
                sums[best_person] = [a + b for a, b in zip(sums[best_person], vector, strict=True)]
                used.add(best_person)
                assigned.append((index, best_person, face))

            public_result = {**result, "personCount": len(used)}
            connection.execute(
                analysis_runs.update()
                .where(
                    analysis_runs.c.asset_id == asset_id,
                    analysis_runs.c.analysis_type == "photo-ai",
                    analysis_runs.c.is_current,
                )
                .values(is_current=False)
            )
            connection.execute(
                insert(analysis_runs).values(
                    id=run_id,
                    asset_id=asset_id,
                    analysis_type="photo-ai",
                    model_name=model_name,
                    model_version=model_version,
                    pipeline_version=pipeline_version,
                    input_hash=input_hash,
                    object_key=object_key,
                    result=public_result,
                    searchable_text=searchable,
                    is_current=True,
                    created_at=datetime.fromisoformat(created_at),
                    semantic_origin=semantic_origin,
                    source_run_id=source_run_id,
                    reuse_policy_version=reuse_policy_version,
                    similarity=similarity,
                )
            )
            if assigned:
                connection.execute(
                    insert(face_table),
                    [
                        {
                            "id": str(uuid4()),
                            "asset_id": asset_id,
                            "analysis_run_id": run_id,
                            "person_id": person_id,
                            "face_index": index,
                            "bounding_box": face["box"],
                            "confidence": face["confidence"],
                            "embedding": face["embedding"],
                        }
                        for index, person_id, face in assigned
                    ],
                )
            connection.execute(
                jobs.update()
                .where(jobs.c.asset_id == asset_id, jobs.c.job_type == "ai-v1")
                .values(status="ready", error=None, lease_until=None)
            )
        return {"faceCount": len(assigned), "personCount": len(used)}

    def analysis_status(self, asset_id: str) -> dict:
        face_table = faces
        with self.engine.connect() as connection:
            job = (
                connection.execute(
                    select(jobs.c.status, jobs.c.attempts, jobs.c.error).where(
                        jobs.c.asset_id == asset_id, jobs.c.job_type == "ai-v1"
                    )
                )
                .mappings()
                .one_or_none()
            )
            run = (
                connection.execute(
                    select(analysis_runs).where(
                        analysis_runs.c.asset_id == asset_id,
                        analysis_runs.c.analysis_type == "photo-ai",
                        analysis_runs.c.is_current,
                    )
                )
                .mappings()
                .one_or_none()
            )
            face_rows = []
            if run:
                face_rows = list(
                    connection.execute(
                        select(
                            face_table.c.face_index,
                            face_table.c.bounding_box,
                            face_table.c.confidence,
                            face_table.c.person_id,
                            people.c.display_name,
                        )
                        .join(people, face_table.c.person_id == people.c.id)
                        .where(face_table.c.analysis_run_id == run["id"])
                        .order_by(face_table.c.face_index)
                    ).mappings()
                )
        value = {
            "status": job["status"] if job else "missing",
            "attempts": job["attempts"] if job else 0,
            "error": job["error"] if job else None,
            "runId": run["id"] if run else None,
            "model": run["model_name"] if run else None,
            "modelVersion": run["model_version"] if run else None,
            "pipelineVersion": run["pipeline_version"] if run else None,
            "analyzedAt": run["created_at"].isoformat() if run else None,
            "artifactKey": run["object_key"] if run else None,
            "result": run["result"] if run else None,
            "faces": [
                {
                    "faceIndex": row["face_index"],
                    "box": row["bounding_box"],
                    "confidence": row["confidence"],
                    "personId": row["person_id"],
                    "personName": row["display_name"] or None,
                }
                for row in face_rows
            ],
        }
        return value

    def list_people(self, query: str = "", limit: int = 500, offset: int = 0) -> dict:
        """Return current face groups with a small representative contact sheet."""
        pattern = f"%{query.strip()}%"
        statement = text("""
            WITH current_faces AS (
                SELECT f.id, f.asset_id, f.person_id, f.bounding_box, f.confidence,
                       a.original_filename
                FROM faces f
                JOIN analysis_runs ar ON ar.id = f.analysis_run_id AND ar.is_current
                JOIN assets a ON a.id = f.asset_id AND a.deleted_at IS NULL
            )
            SELECT p.id, p.display_name, stats.face_count, stats.photo_count,
                   COALESCE(samples.items, '[]'::jsonb) AS samples
            FROM people p
            JOIN LATERAL (
                SELECT count(*)::integer AS face_count,
                       count(DISTINCT cf.asset_id)::integer AS photo_count
                FROM current_faces cf WHERE cf.person_id = p.id
            ) stats ON stats.face_count > 0
            LEFT JOIN LATERAL (
                SELECT jsonb_agg(jsonb_build_object(
                    'faceId', sample.id,
                    'assetId', sample.asset_id,
                    'originalFilename', sample.original_filename,
                    'box', sample.bounding_box,
                    'confidence', sample.confidence,
                    'thumbnailUrl', '/faces/' || sample.id || '/thumbnail'
                ) ORDER BY sample.confidence DESC) AS items
                FROM (
                    SELECT cf.* FROM current_faces cf
                    WHERE cf.person_id = p.id
                    ORDER BY cf.confidence DESC, cf.id
                    LIMIT 4
                ) sample
            ) samples ON true
            WHERE (:query = '' OR p.display_name ILIKE :pattern OR EXISTS (
                SELECT 1 FROM current_faces cf
                WHERE cf.person_id = p.id AND cf.original_filename ILIKE :pattern
            ))
            ORDER BY (NULLIF(trim(p.display_name), '') IS NULL), stats.face_count DESC,
                     lower(p.display_name), p.created_at, p.id
        """)
        with self.engine.connect() as connection:
            rows = list(
                connection.execute(
                    statement, {"query": query.strip(), "pattern": pattern}
                ).mappings()
            )
        page = rows[offset : offset + limit]
        return {
            "items": [
                {
                    "personId": row["id"],
                    "displayName": row["display_name"],
                    "faceCount": row["face_count"],
                    "photoCount": row["photo_count"],
                    "sampleFaces": row["samples"],
                }
                for row in page
            ],
            "total": len(rows),
            "named": sum(bool(row["display_name"].strip()) for row in rows),
            "unnamed": sum(not row["display_name"].strip() for row in rows),
        }

    def person_detail(self, person_id: str, limit: int = 2000, offset: int = 0) -> dict | None:
        """Return the current, reviewable faces assigned to one person."""
        with self.engine.connect() as connection:
            person = (
                connection.execute(select(people).where(people.c.id == person_id))
                .mappings()
                .one_or_none()
            )
            if person is None:
                return None
            filters = (
                faces.c.person_id == person_id,
                analysis_runs.c.is_current,
                assets.c.deleted_at.is_(None),
            )
            source = faces.join(analysis_runs, faces.c.analysis_run_id == analysis_runs.c.id).join(
                assets, faces.c.asset_id == assets.c.id
            )
            total = connection.scalar(select(func.count()).select_from(source).where(*filters))
            photo_count = connection.scalar(
                select(func.count(func.distinct(faces.c.asset_id)))
                .select_from(source)
                .where(*filters)
            )
            rows = list(
                connection.execute(
                    select(
                        faces.c.id,
                        faces.c.asset_id,
                        faces.c.bounding_box,
                        faces.c.confidence,
                        assets.c.original_filename,
                    )
                    .select_from(source)
                    .where(*filters)
                    .order_by(assets.c.timeline_at.desc(), assets.c.id, faces.c.face_index)
                    .limit(limit)
                    .offset(offset)
                ).mappings()
            )
        return {
            "personId": person_id,
            "displayName": person["display_name"],
            "faceCount": total,
            "photoCount": photo_count,
            "faces": [
                {
                    "faceId": row["id"],
                    "assetId": row["asset_id"],
                    "originalFilename": row["original_filename"],
                    "box": row["bounding_box"],
                    "confidence": row["confidence"],
                    "thumbnailUrl": f"/faces/{row['id']}/thumbnail",
                }
                for row in rows
            ],
        }

    def face(self, face_id: str) -> dict | None:
        """Resolve one current face for its cropped thumbnail."""
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(faces.c.asset_id, faces.c.bounding_box)
                    .join(analysis_runs, faces.c.analysis_run_id == analysis_runs.c.id)
                    .where(faces.c.id == face_id, analysis_runs.c.is_current)
                )
                .mappings()
                .one_or_none()
            )
        return dict(row) if row else None

    def commit_face_operation(self, operation_id: UUID, request: dict) -> dict:
        """Apply a face-review edit and record its idempotent result atomically."""
        with self.writer(), self.engine.begin() as connection:
            previous = (
                connection.execute(
                    select(operations).where(operations.c.id == str(operation_id)).with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if previous:
                if previous["request"] != request:
                    raise LibraryError("Operation ID was reused with a different request")
                return previous["result"]

            connection.execute(text("SELECT pg_advisory_xact_lock(7046868303)"))
            action = request.get("action")
            if action == "person.rename":
                person_id = request["personId"]
                exists = connection.scalar(
                    select(people.c.id).where(people.c.id == person_id).with_for_update()
                )
                if exists is None:
                    raise FileNotFoundError("Person not found")
                connection.execute(
                    people.update()
                    .where(people.c.id == person_id)
                    .values(display_name=request["displayName"])
                )
                result = {
                    "operationId": str(operation_id),
                    "personId": person_id,
                    "displayName": request["displayName"],
                }
            elif action == "person.merge":
                source_id, target_id = request["sourcePersonId"], request["targetPersonId"]
                if source_id == target_id:
                    raise LibraryError("Choose two different people to combine")
                found = set(
                    connection.scalars(
                        select(people.c.id).where(people.c.id.in_([source_id, target_id]))
                    )
                )
                if found != {source_id, target_id}:
                    raise FileNotFoundError("Person not found")
                moved = connection.scalar(
                    select(func.count()).select_from(faces).where(faces.c.person_id == source_id)
                )
                connection.execute(
                    faces.update().where(faces.c.person_id == source_id).values(person_id=target_id)
                )
                connection.execute(people.delete().where(people.c.id == source_id))
                result = {
                    "operationId": str(operation_id),
                    "personId": target_id,
                    "mergedPersonId": source_id,
                    "movedFaces": moved,
                }
            elif action == "faces.move":
                face_ids = request["faceIds"]
                rows = list(
                    connection.execute(
                        select(faces.c.id)
                        .join(analysis_runs, faces.c.analysis_run_id == analysis_runs.c.id)
                        .where(faces.c.id.in_(face_ids), analysis_runs.c.is_current)
                        .with_for_update()
                    ).scalars()
                )
                if set(rows) != set(face_ids):
                    raise LibraryError("One or more selected faces are no longer current")
                target_id = request.get("targetPersonId")
                created = target_id is None
                if target_id is None:
                    target_id = str(uuid4())
                    connection.execute(
                        insert(people).values(
                            id=target_id,
                            display_name="",
                            created_at=datetime.now(UTC),
                        )
                    )
                elif connection.scalar(select(people.c.id).where(people.c.id == target_id)) is None:
                    raise FileNotFoundError("Destination person not found")
                connection.execute(
                    faces.update().where(faces.c.id.in_(face_ids)).values(person_id=target_id)
                )
                result = {
                    "operationId": str(operation_id),
                    "personId": target_id,
                    "movedFaces": len(face_ids),
                    "createdPerson": created,
                }
            else:
                raise LibraryError("Invalid face operation")

            connection.execute(
                insert(operations).values(id=str(operation_id), request=request, result=result)
            )
            return result

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
                .values(id=str(batch_id), status="accepting", created_at=now, updated_at=now)
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
            connection.execute(
                upload_batches.update()
                .where(
                    upload_batches.c.id == str(batch_id),
                    upload_batches.c.status == "accepting",
                )
                .values(updated_at=now)
            )

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

    def active_upload_batch_ids(self, limit: int = 100) -> list[str]:
        with self.engine.connect() as connection:
            return list(
                connection.execute(
                    select(upload_batches.c.id)
                    .where(
                        upload_batches.c.status.in_(
                            ["accepting", "queued", "processing", "failed"]
                        )
                    )
                    .order_by(upload_batches.c.created_at.desc(), upload_batches.c.id)
                    .limit(limit)
                ).scalars()
            )

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
            connection.execute(
                upload_batches.update()
                .where(upload_batches.c.id == str(batch_id))
                .values(updated_at=int(time()))
            )
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
            batch_id = connection.execute(
                upload_files.update()
                .where(
                    upload_files.c.id == str(file_id),
                    upload_files.c.status.in_(["waiting", "uploading", "uploaded"]),
                )
                .values(status="uploaded", sha256=sha256, error=None)
                .returning(upload_files.c.batch_id)
            ).scalar_one_or_none()
            if batch_id:
                connection.execute(
                    upload_batches.update()
                    .where(upload_batches.c.id == batch_id)
                    .values(updated_at=int(time()))
                )

    def fail_upload(self, file_id: UUID | str, error: str):
        with self.engine.begin() as connection:
            batch_id = connection.execute(
                upload_files.update()
                .where(upload_files.c.id == str(file_id), upload_files.c.status == "uploading")
                .values(status="waiting", error=error)
                .returning(upload_files.c.batch_id)
            ).scalar_one_or_none()
            if batch_id:
                connection.execute(
                    upload_batches.update()
                    .where(upload_batches.c.id == batch_id)
                    .values(updated_at=int(time()))
                )

    def claim_upload_batch_cleanup(self, batch_id: UUID | str) -> dict:
        """Lock an unsealed, inactive batch so its staged objects can be removed."""
        with self.engine.begin() as connection:
            batch = connection.execute(
                select(upload_batches)
                .where(upload_batches.c.id == str(batch_id))
                .with_for_update()
            ).mappings().one_or_none()
            if batch is None:
                raise LibraryError("Upload batch not found")
            if batch["status"] not in {"accepting", "deleting"}:
                raise LibraryError("A sealed upload batch cannot be discarded")
            active = connection.scalar(
                select(func.count())
                .select_from(upload_files)
                .where(
                    upload_files.c.batch_id == str(batch_id),
                    upload_files.c.status == "uploading",
                )
            )
            if active:
                raise LibraryError("Wait for active file transfers to stop before discarding")
            files = list(
                connection.execute(
                    select(upload_files.c.staging_key)
                    .where(upload_files.c.batch_id == str(batch_id))
                ).scalars()
            )
            connection.execute(
                upload_batches.update()
                .where(upload_batches.c.id == str(batch_id))
                .values(status="deleting", updated_at=int(time()))
            )
        return {"batchId": str(batch_id), "stagingKeys": files}

    def claim_abandoned_upload_batch(self, cutoff: int) -> dict | None:
        """Claim one stale unsealed batch without racing another cleanup worker."""
        uploading = (
            select(func.count())
            .select_from(upload_files)
            .where(
                upload_files.c.batch_id == upload_batches.c.id,
                upload_files.c.status == "uploading",
            )
            .scalar_subquery()
        )
        with self.engine.begin() as connection:
            batch_id = connection.scalar(
                select(upload_batches.c.id)
                .where(
                    or_(
                        upload_batches.c.status == "deleting",
                        and_(
                            upload_batches.c.status == "accepting",
                            upload_batches.c.updated_at < cutoff,
                        ),
                    ),
                    uploading == 0,
                )
                .order_by(upload_batches.c.updated_at, upload_batches.c.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if batch_id is None:
                return None
            files = list(
                connection.execute(
                    select(upload_files.c.staging_key)
                    .where(upload_files.c.batch_id == batch_id)
                ).scalars()
            )
            connection.execute(
                upload_batches.update()
                .where(upload_batches.c.id == batch_id)
                .values(status="deleting", updated_at=int(time()))
            )
        return {"batchId": batch_id, "stagingKeys": files}

    def finish_upload_batch_cleanup(self, batch_id: UUID | str):
        with self.engine.begin() as connection:
            connection.execute(
                upload_batches.delete().where(
                    upload_batches.c.id == str(batch_id),
                    upload_batches.c.status == "deleting",
                )
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
                .values(status="queued", sealed_at=int(time()), updated_at=int(time()))
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
                .values(status=status, updated_at=int(time()))
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
                .values(status="queued", updated_at=int(time()))
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
                "previewPending": connection.scalar(
                    select(func.count())
                    .select_from(jobs)
                    .where(jobs.c.job_type == "preview-v1", jobs.c.status == "pending")
                ),
                "previewRunning": connection.scalar(
                    select(func.count())
                    .select_from(jobs)
                    .where(jobs.c.job_type == "preview-v1", jobs.c.status == "running")
                ),
                "previewFailed": connection.scalar(
                    select(func.count())
                    .select_from(jobs)
                    .where(jobs.c.job_type == "preview-v1", jobs.c.status == "failed")
                ),
                "analysisPending": connection.scalar(
                    select(func.count())
                    .select_from(jobs)
                    .where(jobs.c.job_type == "ai-v1", jobs.c.status == "pending")
                ),
                "analysisRunning": connection.scalar(
                    select(func.count())
                    .select_from(jobs)
                    .where(jobs.c.job_type == "ai-v1", jobs.c.status == "running")
                ),
                "analysisFailed": connection.scalar(
                    select(func.count())
                    .select_from(jobs)
                    .where(jobs.c.job_type == "ai-v1", jobs.c.status == "failed")
                ),
            }
