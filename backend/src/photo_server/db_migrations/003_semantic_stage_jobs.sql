INSERT INTO ai_stage_jobs (asset_id, stage, status, attempts)
SELECT id, 'semantic', 'pending', 0 FROM assets
ON CONFLICT (asset_id, stage) DO NOTHING;
