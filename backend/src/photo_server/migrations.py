"""Transactional PostgreSQL migration runner for packaged SQL migrations."""

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


def _legacy_version(connection) -> int:
    if connection.scalar(text("SELECT to_regclass('public.library')")) is None:
        return 0
    value = connection.scalar(text("SELECT schema_version FROM library WHERE singleton = 1"))
    if value is None:
        raise LibraryError("The database has a library table but no singleton library record")
    return int(value)


def migrate(engine: Engine, library_id: str) -> dict:
    """Bring a database to the packaged schema in one locked transaction.

    Databases created before SQL migrations are adopted at their recorded
    ``library.schema_version``. Their historical migration rows are inserted
    with the checksums of the now-canonical SQL files before pending migrations
    run. No DDL is defined in this module.
    """

    migrations = available_migrations()
    bootstrap, versioned = migrations[0], migrations[1:]
    latest = versioned[-1].version
    applied_now: list[dict] = []

    with engine.begin() as connection:
        connection.execute(text("SELECT pg_advisory_xact_lock(:lock)"), {"lock": MIGRATION_LOCK})
        legacy_version = _legacy_version(connection)
        if legacy_version > latest:
            raise LibraryError(
                f"Database schema {legacy_version} is newer than this application ({latest})"
            )

        tracking_exists = connection.scalar(text("SELECT to_regclass('public.schema_migrations')"))
        if tracking_exists is None:
            connection.exec_driver_sql(bootstrap.sql)
            if legacy_version:
                for migration in versioned[:legacy_version]:
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
        if legacy_version != current:
            raise LibraryError(
                "library.schema_version and schema_migrations disagree; repair is required"
            )

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
            else:
                connection.execute(
                    text("UPDATE library SET schema_version = :version WHERE singleton = 1"),
                    {"version": migration.version},
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
