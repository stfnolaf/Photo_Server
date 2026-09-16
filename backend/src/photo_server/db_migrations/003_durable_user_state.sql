ALTER TABLE assets ADD COLUMN IF NOT EXISTS deleted_at TEXT;

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
