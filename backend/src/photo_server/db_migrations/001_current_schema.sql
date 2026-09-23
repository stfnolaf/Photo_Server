CREATE TABLE IF NOT EXISTS library (
    singleton INTEGER PRIMARY KEY,
    library_id VARCHAR NOT NULL,
    schema_version INTEGER NOT NULL,
    state_authority VARCHAR NOT NULL DEFAULT 'postgres',
    CONSTRAINT ck_library_state_authority CHECK (state_authority = 'postgres')
);

CREATE TABLE IF NOT EXISTS assets (
    id VARCHAR PRIMARY KEY,
    original_filename TEXT NOT NULL,
    sha256 VARCHAR(64) NOT NULL UNIQUE,
    state_revision INTEGER NOT NULL,
    manifest JSONB NOT NULL,
    timeline_at TIMESTAMP NOT NULL,
    media_type VARCHAR NOT NULL,
    search_text TEXT NOT NULL,
    rating INTEGER NOT NULL DEFAULT 0,
    favorite BOOLEAN NOT NULL DEFAULT false,
    deleted_at TEXT,
    CONSTRAINT ck_assets_rating CHECK (rating BETWEEN 0 AND 5)
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

CREATE TABLE IF NOT EXISTS albums (
    id VARCHAR PRIMARY KEY,
    state_revision INTEGER NOT NULL,
    state JSONB NOT NULL,
    deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS album_assets (
    album_id VARCHAR NOT NULL REFERENCES albums(id),
    asset_id VARCHAR NOT NULL REFERENCES assets(id),
    position INTEGER NOT NULL,
    PRIMARY KEY (album_id, asset_id)
);

CREATE TABLE IF NOT EXISTS operations (
    id VARCHAR PRIMARY KEY,
    request JSONB NOT NULL,
    result JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    asset_id VARCHAR NOT NULL REFERENCES assets(id),
    job_type VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    attempts INTEGER NOT NULL,
    error TEXT,
    lease_until BIGINT,
    force_full BOOLEAN NOT NULL DEFAULT false,
    PRIMARY KEY (asset_id, job_type)
);

CREATE TABLE IF NOT EXISTS upload_batches (
    id VARCHAR PRIMARY KEY,
    status VARCHAR NOT NULL,
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL,
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

CREATE TABLE IF NOT EXISTS analysis_runs (
    id VARCHAR PRIMARY KEY,
    asset_id VARCHAR NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    analysis_type VARCHAR NOT NULL,
    model_name TEXT NOT NULL,
    model_version TEXT NOT NULL,
    pipeline_version TEXT NOT NULL,
    input_hash VARCHAR(64) NOT NULL,
    object_key TEXT NOT NULL UNIQUE,
    result JSONB NOT NULL,
    searchable_text TEXT NOT NULL,
    is_current BOOLEAN NOT NULL DEFAULT true,
    semantic_origin VARCHAR NOT NULL DEFAULT 'computed',
    source_run_id VARCHAR REFERENCES analysis_runs(id) ON DELETE SET NULL,
    reuse_policy_version TEXT,
    similarity JSONB,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS people (
    id VARCHAR PRIMARY KEY,
    display_name TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS faces (
    id VARCHAR PRIMARY KEY,
    asset_id VARCHAR NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    analysis_run_id VARCHAR NOT NULL REFERENCES analysis_runs(id) ON DELETE CASCADE,
    person_id VARCHAR NOT NULL REFERENCES people(id),
    face_index INTEGER NOT NULL,
    bounding_box JSONB NOT NULL,
    confidence DOUBLE PRECISION NOT NULL,
    embedding JSONB NOT NULL,
    UNIQUE (analysis_run_id, face_index)
);

CREATE TABLE IF NOT EXISTS image_fingerprints (
    asset_id VARCHAR NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    algorithm_version TEXT NOT NULL,
    phash VARCHAR(16) NOT NULL,
    dhash VARCHAR(16) NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (asset_id, algorithm_version)
);

CREATE TABLE IF NOT EXISTS burst_clusters (
    id VARCHAR PRIMARY KEY,
    representative_asset_id VARCHAR NOT NULL REFERENCES assets(id) ON DELETE RESTRICT,
    policy_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS burst_members (
    cluster_id VARCHAR NOT NULL REFERENCES burst_clusters(id) ON DELETE CASCADE,
    asset_id VARCHAR NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    PRIMARY KEY (cluster_id, asset_id)
);

CREATE TABLE IF NOT EXISTS preview_cache (
    asset_id VARCHAR PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE,
    preview_bytes BIGINT,
    thumbnail_bytes BIGINT,
    last_accessed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_assets_timeline ON assets (timeline_at, id);
CREATE INDEX IF NOT EXISTS ix_assets_favorites ON assets (timeline_at, id) WHERE favorite;
CREATE INDEX IF NOT EXISTS ix_onboarding_jobs_claim ON onboarding_jobs (status, lease_until);
CREATE UNIQUE INDEX IF NOT EXISTS ux_analysis_runs_current
    ON analysis_runs (asset_id, analysis_type) WHERE is_current;
CREATE INDEX IF NOT EXISTS ix_analysis_runs_asset
    ON analysis_runs (asset_id, analysis_type, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_analysis_runs_search
    ON analysis_runs USING gin (to_tsvector('simple', searchable_text));
CREATE INDEX IF NOT EXISTS ix_faces_current_person ON faces (person_id, asset_id);
CREATE INDEX IF NOT EXISTS ix_image_fingerprints_version ON image_fingerprints (algorithm_version);
CREATE INDEX IF NOT EXISTS ix_burst_members_asset ON burst_members (asset_id);
CREATE INDEX IF NOT EXISTS ix_preview_cache_accessed ON preview_cache (last_accessed_at);
CREATE INDEX IF NOT EXISTS ix_upload_batches_cleanup ON upload_batches (status, updated_at);
