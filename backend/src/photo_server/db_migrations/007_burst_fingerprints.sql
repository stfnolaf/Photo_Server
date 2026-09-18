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

CREATE INDEX IF NOT EXISTS ix_image_fingerprints_version
    ON image_fingerprints (algorithm_version);

ALTER TABLE analysis_runs
    ADD COLUMN IF NOT EXISTS semantic_origin TEXT NOT NULL DEFAULT 'computed',
    ADD COLUMN IF NOT EXISTS source_run_id VARCHAR REFERENCES analysis_runs(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS reuse_policy_version TEXT,
    ADD COLUMN IF NOT EXISTS similarity JSONB;
