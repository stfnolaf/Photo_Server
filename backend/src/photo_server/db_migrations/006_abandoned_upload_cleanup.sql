ALTER TABLE upload_batches
    ADD COLUMN IF NOT EXISTS updated_at BIGINT;

UPDATE upload_batches
SET updated_at = COALESCE(sealed_at, created_at)
WHERE updated_at IS NULL;

ALTER TABLE upload_batches
    ALTER COLUMN updated_at SET NOT NULL;

CREATE INDEX IF NOT EXISTS ix_upload_batches_cleanup
    ON upload_batches (status, updated_at);
