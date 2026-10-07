ALTER TABLE jobs ADD COLUMN IF NOT EXISTS queued_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE ai_stage_jobs ADD COLUMN IF NOT EXISTS queued_at TIMESTAMPTZ NOT NULL DEFAULT now();
ALTER TABLE onboarding_jobs ADD COLUMN IF NOT EXISTS queued_at TIMESTAMPTZ NOT NULL DEFAULT now();

CREATE INDEX IF NOT EXISTS ix_jobs_queue_age ON jobs (status, queued_at);
CREATE INDEX IF NOT EXISTS ix_onboarding_queue_age ON onboarding_jobs (status, queued_at);
