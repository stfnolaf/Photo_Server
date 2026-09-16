ALTER TABLE library
    ADD COLUMN IF NOT EXISTS state_authority VARCHAR NOT NULL DEFAULT 'postgres';

UPDATE library SET state_authority = 'postgres' WHERE state_authority <> 'postgres';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'library'::regclass
          AND conname = 'ck_library_state_authority'
    ) THEN
        ALTER TABLE library
            ADD CONSTRAINT ck_library_state_authority CHECK (state_authority = 'postgres');
    END IF;
END;
$$;
