-- Phase 1 of the preview cache eviction plan: track byte sizes and last
-- access time for every cached preview set. Nothing evicts anything yet;
-- these rows only make the cache observable.
CREATE TABLE IF NOT EXISTS preview_cache (
    asset_id VARCHAR PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE,
    preview_bytes BIGINT,
    thumbnail_bytes BIGINT,
    last_accessed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_preview_cache_accessed
    ON preview_cache (last_accessed_at);
