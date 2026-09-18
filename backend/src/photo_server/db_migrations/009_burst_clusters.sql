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

CREATE INDEX IF NOT EXISTS ix_burst_members_asset ON burst_members (asset_id);
