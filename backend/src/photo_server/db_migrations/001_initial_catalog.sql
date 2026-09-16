CREATE TABLE IF NOT EXISTS library (
    singleton INTEGER PRIMARY KEY,
    library_id VARCHAR NOT NULL,
    schema_version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS assets (
    id VARCHAR PRIMARY KEY,
    original_filename TEXT NOT NULL,
    sha256 VARCHAR(64) NOT NULL UNIQUE,
    state_revision INTEGER NOT NULL,
    manifest JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS blobs (
    id VARCHAR PRIMARY KEY,
    asset_id VARCHAR NOT NULL REFERENCES assets(id),
    role VARCHAR NOT NULL,
    object_key TEXT NOT NULL UNIQUE,
    original_filename TEXT NOT NULL,
    sha256 VARCHAR(64) NOT NULL,
    size_bytes BIGINT NOT NULL,
    mime_type VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    asset_id VARCHAR NOT NULL REFERENCES assets(id),
    job_type VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    attempts INTEGER NOT NULL,
    error TEXT,
    lease_until BIGINT,
    PRIMARY KEY (asset_id, job_type)
);

CREATE TABLE IF NOT EXISTS upload_batches (
    id VARCHAR PRIMARY KEY,
    status VARCHAR NOT NULL,
    created_at BIGINT NOT NULL,
    sealed_at BIGINT,
    error TEXT
);

CREATE TABLE IF NOT EXISTS upload_files (
    id VARCHAR PRIMARY KEY,
    batch_id VARCHAR NOT NULL REFERENCES upload_batches(id) ON DELETE CASCADE,
    relative_path TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    size_bytes BIGINT NOT NULL,
    mime_type VARCHAR,
    staging_key TEXT NOT NULL UNIQUE,
    required INTEGER NOT NULL,
    status VARCHAR NOT NULL,
    reason VARCHAR,
    sha256 VARCHAR(64),
    asset_id VARCHAR,
    error TEXT,
    UNIQUE (batch_id, relative_path)
);

CREATE TABLE IF NOT EXISTS onboarding_jobs (
    id VARCHAR PRIMARY KEY,
    batch_id VARCHAR NOT NULL REFERENCES upload_batches(id) ON DELETE CASCADE,
    primary_file_id VARCHAR NOT NULL REFERENCES upload_files(id),
    sidecar_file_ids JSONB NOT NULL,
    status VARCHAR NOT NULL,
    attempts INTEGER NOT NULL,
    lease_until BIGINT,
    result JSONB,
    error TEXT
);

CREATE INDEX IF NOT EXISTS ix_onboarding_jobs_claim ON onboarding_jobs (status, lease_until);
