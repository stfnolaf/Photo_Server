ALTER TABLE assets
    ADD COLUMN IF NOT EXISTS timeline_at TIMESTAMP,
    ADD COLUMN IF NOT EXISTS media_type VARCHAR,
    ADD COLUMN IF NOT EXISTS search_text TEXT,
    ADD COLUMN IF NOT EXISTS rating INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS favorite BOOLEAN NOT NULL DEFAULT false;

CREATE OR REPLACE FUNCTION photo_try_timestamp(value TEXT) RETURNS TIMESTAMP
LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN
    RETURN substring(value FROM 1 FOR 19)::timestamp;
EXCEPTION WHEN OTHERS THEN
    RETURN NULL;
END;
$$;

UPDATE assets AS asset
SET
    timeline_at = COALESCE(
        photo_try_timestamp(asset.manifest ->> 'captureTime'),
        photo_try_timestamp(asset.manifest ->> 'importedAt')
    ),
    media_type = replace(
        (
            SELECT value ->> 'role'
            FROM jsonb_array_elements(asset.manifest -> 'blobs')
            WHERE value ->> 'blobId' = asset.manifest ->> 'primaryBlobId'
        ),
        'ORIGINAL_',
        ''
    ),
    search_text = concat_ws(
        ' ',
        asset.original_filename,
        asset.manifest -> 'metadata' ->> 'Make',
        asset.manifest -> 'metadata' ->> 'Model',
        asset.manifest -> 'metadata' ->> 'LensModel',
        asset.manifest -> 'metadata' ->> 'LensID'
    );

DROP FUNCTION IF EXISTS photo_try_timestamp(TEXT);

ALTER TABLE assets
    ALTER COLUMN timeline_at SET NOT NULL,
    ALTER COLUMN media_type SET NOT NULL,
    ALTER COLUMN search_text SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'assets'::regclass
          AND conname = 'ck_assets_rating'
    ) THEN
        ALTER TABLE assets
            ADD CONSTRAINT ck_assets_rating CHECK (rating BETWEEN 0 AND 5);
    END IF;
END;
$$;

CREATE INDEX IF NOT EXISTS ix_assets_timeline ON assets (timeline_at, id);
CREATE INDEX IF NOT EXISTS ix_assets_favorites ON assets (timeline_at, id) WHERE favorite;
