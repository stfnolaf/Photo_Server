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
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS people (
    id VARCHAR PRIMARY KEY,
    display_name TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
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

CREATE UNIQUE INDEX IF NOT EXISTS ux_analysis_runs_current
    ON analysis_runs (asset_id, analysis_type) WHERE is_current;
CREATE INDEX IF NOT EXISTS ix_analysis_runs_asset
    ON analysis_runs (asset_id, analysis_type, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_analysis_runs_search
    ON analysis_runs USING gin (to_tsvector('simple', searchable_text));
CREATE INDEX IF NOT EXISTS ix_faces_current_person
    ON faces (person_id, asset_id);

INSERT INTO jobs (asset_id, job_type, status, attempts)
SELECT id, 'ai-v1', 'pending', 0
FROM assets
ON CONFLICT (asset_id, job_type) DO NOTHING;
