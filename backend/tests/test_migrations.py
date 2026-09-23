from hashlib import sha256

from photo_server.migrations import available_migrations


def test_packaged_migrations_are_contiguous_and_checksummed():
    migrations = available_migrations()
    assert [migration.version for migration in migrations] == [0, 1, 2, 3, 4, 5]
    assert all(
        migration.checksum == sha256(migration.sql.encode()).hexdigest() for migration in migrations
    )


def test_schema_ddl_lives_in_migration_files():
    migrations = available_migrations()
    documents = {migration.version: migration.sql for migration in migrations}
    assert "CREATE TABLE IF NOT EXISTS schema_migrations" in documents[0]
    assert "CREATE TABLE IF NOT EXISTS assets" in documents[1]
    assert "CREATE TABLE IF NOT EXISTS albums" in documents[1]
    assert "CREATE TABLE IF NOT EXISTS analysis_runs" in documents[1]
    assert "CREATE TABLE IF NOT EXISTS image_fingerprints" in documents[1]
    assert "CREATE TABLE IF NOT EXISTS burst_clusters" in documents[1]
    assert "CREATE TABLE IF NOT EXISTS burst_members" in documents[1]
    assert "CREATE TABLE IF NOT EXISTS preview_cache" in documents[1]
    assert "CREATE TABLE IF NOT EXISTS ai_stage_jobs" in documents[2]
    assert "'semantic'" in documents[3]


def test_schema_migrations_are_written_to_be_idempotent():
    documents = {migration.version: migration.sql for migration in available_migrations()}
    assert "CREATE TABLE IF NOT EXISTS schema_migrations" in documents[0]
    assert "CREATE TABLE IF NOT EXISTS assets" in documents[1]
    assert "CREATE TABLE IF NOT EXISTS jobs" in documents[1]
    assert "CREATE TABLE IF NOT EXISTS image_fingerprints" in documents[1]
    assert "CREATE INDEX IF NOT EXISTS ix_image_fingerprints_version" in documents[1]
    assert "CREATE TABLE IF NOT EXISTS burst_clusters" in documents[1]
    assert "CREATE INDEX IF NOT EXISTS ix_burst_members_asset" in documents[1]
    assert "CREATE TABLE IF NOT EXISTS preview_cache" in documents[1]
    assert "CREATE INDEX IF NOT EXISTS ix_preview_cache_accessed" in documents[1]
    assert "CREATE TABLE IF NOT EXISTS ai_stage_jobs" in documents[2]
    assert "'semantic'" in documents[3]
