"""Transactional PostgreSQL baseline runner for fresh library databases."""

import re
from dataclasses import dataclass
from hashlib import sha256
from importlib.resources import files

from sqlalchemy import Engine, text

from photo_server.config import LibraryError

MIGRATION_LOCK = 7046868302
MIGRATION_NAME = re.compile(r"^(\d{3})_([a-z0-9_]+)\.sql$")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str
    checksum: str


def available_migrations() -> list[Migration]:
    migrations = []
    root = files("photo_server").joinpath("db_migrations")
    for resource in root.iterdir():
        match = MIGRATION_NAME.fullmatch(resource.name)
        if not match:
            continue
        data = resource.read_bytes()
        migrations.append(
            Migration(
                version=int(match.group(1)),
                name=match.group(2),
                sql=data.decode("utf-8"),
                checksum=sha256(data).hexdigest(),
            )
        )
    migrations.sort(key=lambda migration: migration.version)
    versions = [migration.version for migration in migrations]
    if not versions or versions[0] != 0 or versions != list(range(versions[-1] + 1)):
        raise LibraryError("Database migration files must be consecutively numbered from 000")
    return migrations


def migrate(engine: Engine, library_id: str) -> dict:
    """Create or validate the one current schema in one locked transaction.

    The flattened deployment intentionally has no upgrade path from the
    discarded test-era schema. A non-empty database without this baseline is
    rejected rather than silently adopted.
    """

    migrations = available_migrations()
    bootstrap, versioned = migrations[0], migrations[1:]
    latest = versioned[-1].version
    applied_now: list[dict] = []

    with engine.begin() as connection:
        connection.execute(text("SELECT pg_advisory_xact_lock(:lock)"), {"lock": MIGRATION_LOCK})
        tracking_exists = connection.scalar(text("SELECT to_regclass('public.schema_migrations')"))
        if tracking_exists is None:
            has_objects = connection.scalar(
                text("""
                    SELECT EXISTS (
                        SELECT 1 FROM pg_class c
                        JOIN pg_namespace n ON n.oid = c.relnamespace
                        WHERE n.nspname = 'public'
                          AND c.relkind IN ('r', 'p', 'v', 'm')
                          AND c.relname NOT IN ('schema_migrations')
                    )
                """)
            )
            if has_objects:
                raise LibraryError(
                    "PostgreSQL is not empty and has no flattened schema; reset it explicitly"
                )
            connection.exec_driver_sql(bootstrap.sql)

        applied = {
            row.version: row
            for row in connection.execute(
                text("SELECT version, name, checksum FROM schema_migrations ORDER BY version")
            )
        }
        expected_applied = list(range(1, max(applied, default=0) + 1))
        if sorted(applied) != expected_applied:
            raise LibraryError("Database migration history is not contiguous")
        for migration in versioned:
            recorded = applied.get(migration.version)
            if recorded and (
                recorded.name != migration.name or recorded.checksum != migration.checksum
            ):
                raise LibraryError(
                    f"Applied migration {migration.version:03d} no longer matches its SQL file"
                )

        current = max(applied, default=0)

        for migration in versioned[current:]:
            connection.exec_driver_sql(migration.sql)
            if migration.version == 1:
                connection.execute(
                    text("""
                        INSERT INTO library (singleton, library_id, schema_version)
                        VALUES (1, :library_id, 1)
                    """),
                    {"library_id": library_id},
                )
            connection.execute(
                text("""
                    INSERT INTO schema_migrations (version, name, checksum)
                    VALUES (:version, :name, :checksum)
                """),
                {
                    "version": migration.version,
                    "name": migration.name,
                    "checksum": migration.checksum,
                },
            )
            applied_now.append({"version": migration.version, "name": migration.name})

        connection.execute(
            text("UPDATE library SET schema_version = :version WHERE singleton = 1"),
            {"version": latest},
        )

        row = connection.execute(
            text("SELECT library_id, schema_version FROM library WHERE singleton = 1")
        ).one()
        if row.library_id != library_id:
            raise LibraryError("Database belongs to a different library")
        if row.schema_version != latest:
            raise LibraryError("Database did not reach the expected schema version")

    return {
        "fromVersion": current,
        "toVersion": latest,
        "applied": applied_now,
    }
