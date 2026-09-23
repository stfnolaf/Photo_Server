CREATE TABLE IF NOT EXISTS ai_stage_jobs (
    asset_id VARCHAR NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    stage VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    lease_until BIGINT,
    PRIMARY KEY (asset_id, stage)
);

INSERT INTO ai_stage_jobs (asset_id, stage, status, attempts)
SELECT id, 'face', 'pending', 0 FROM assets
ON CONFLICT (asset_id, stage) DO NOTHING;

CREATE INDEX IF NOT EXISTS ix_ai_stage_jobs_claim
    ON ai_stage_jobs (stage, status, lease_until);
