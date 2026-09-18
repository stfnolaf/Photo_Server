from hashlib import sha256

from photo_server.migrations import available_migrations


def test_packaged_migrations_are_contiguous_and_checksummed():
    migrations = available_migrations()
    assert [migration.version for migration in migrations] == [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]
    assert all(
        migration.checksum == sha256(migration.sql.encode()).hexdigest() for migration in migrations
    )


def test_schema_ddl_lives_in_migration_files():
    migrations = available_migrations()
    documents = {migration.version: migration.sql for migration in migrations}
    assert "CREATE TABLE IF NOT EXISTS schema_migrations" in documents[0]
    assert "CREATE TABLE IF NOT EXISTS assets" in documents[1]
    assert "ADD COLUMN IF NOT EXISTS rating" in documents[2]
    assert "CREATE TABLE IF NOT EXISTS albums" in documents[3]
    assert "state_authority" in documents[4]
    assert "CREATE TABLE IF NOT EXISTS analysis_runs" in documents[5]
    assert "ADD COLUMN IF NOT EXISTS updated_at" in documents[6]
    assert "CREATE TABLE IF NOT EXISTS image_fingerprints" in documents[7]
    assert "ADD COLUMN IF NOT EXISTS semantic_origin" in documents[7]
    assert "ADD COLUMN IF NOT EXISTS force_full" in documents[8]
    assert "CREATE TABLE IF NOT EXISTS burst_clusters" in documents[9]
    assert "CREATE TABLE IF NOT EXISTS burst_members" in documents[9]


def test_schema_migrations_are_written_to_be_idempotent():
    documents = {migration.version: migration.sql for migration in available_migrations()}
    assert "CREATE TABLE IF NOT EXISTS schema_migrations" in documents[0]
    assert documents[1].count("CREATE TABLE IF NOT EXISTS") == 7
    assert "ADD COLUMN IF NOT EXISTS rating" in documents[2]
    assert "CREATE INDEX IF NOT EXISTS ix_assets_timeline" in documents[2]
    assert "ADD COLUMN IF NOT EXISTS deleted_at" in documents[3]
    assert "ADD COLUMN IF NOT EXISTS state_authority" in documents[4]
    assert "INSERT INTO jobs" in documents[5]
    assert "CREATE INDEX IF NOT EXISTS ix_upload_batches_cleanup" in documents[6]
    assert "CREATE TABLE IF NOT EXISTS image_fingerprints" in documents[7]
    assert "CREATE INDEX IF NOT EXISTS ix_image_fingerprints_version" in documents[7]
    assert "ADD COLUMN IF NOT EXISTS force_full" in documents[8]
    assert "CREATE TABLE IF NOT EXISTS burst_clusters" in documents[9]
    assert "CREATE INDEX IF NOT EXISTS ix_burst_members_asset" in documents[9]
