-- Phase 4: AI worker integration.
-- A job-level force_full flag bypasses semantic reuse for a single analysis
-- job (validation and repair). Fingerprints are untouched.
ALTER TABLE jobs
    ADD COLUMN IF NOT EXISTS force_full BOOLEAN NOT NULL DEFAULT false;
