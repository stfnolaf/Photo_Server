# Rollout note: burst semantic-analysis reuse

This note covers rolling out the semantic-analysis reuse policy
(`burst-reuse-v1`, see `docs/burst-semantic-analysis-reuse.md`). The policy is
controlled by `PHOTO_AI_SEMANTIC_REUSE_MODE` with three values:

- `off`: always invoke the VLM.
- `observe`: evaluate the reuse policy and record the decision, but still invoke the VLM.
- `on`: reuse the source frame's semantics when every gate passes.

The Q6 decision is deliberate: this policy governs the semantic stage only.
The face stage runs on every claimed AI job, even when semantic reuse is
accepted, because face boxes and embeddings remain specific to each frame and
the face-service is the sole inference owner.

## Rollout plan

1. **Default the first release to `observe`.** The worker records, for every
   AI job, whether a candidate was found, which gate rejected it (if any), and
   whether the VLM was invoked — without changing what gets stored.
2. **Review the operational counters** described below. In particular, review a
   representative sample of *accepted* and *near-threshold* candidate pairs,
   including:
   - portraits (faces must not be reused across different people),
   - movement (same scene, different framing),
   - exposure changes (same scene, different brightness),
   - screenshots and documents (excluded by policy, verify they stay excluded),
   - unrelated photos taken close together (the capture-time and camera gates).
3. **Enable `on` only after choosing thresholds from those observations.**
   Adjust the policy constants in `src/photo_server/reuse.py` if the observed
   near-threshold pairs warrant it, and bump `REUSE_POLICY_VERSION`.

## Operational counters

Every AI worker job logs a JSON line (via `run()`) containing a `counters`
object with these keys:

| Counter | Meaning |
| --- | --- |
| `semanticComputed` | 1 if this job invoked the VLM for semantics, else 0. |
| `semanticReused` | 1 if this job reused a source frame's semantics, else 0. |
| `forcedFull` | 1 if the job was a forced full re-analysis (ignoring reuse). |
| `wouldHaveReused` | 1 if a forced full analysis would otherwise have reused (observe-style signal under force). |
| `vlmTimeAvoided` | Estimated seconds of VLM time avoided by reusing (null when not reused). |
| `rejectionsByGate` | Map of gate name → count of candidates rejected by that gate (empty when no candidates). |
| `stageFailures` | Map of stage (`setup`, `fingerprint`, `face`, `semantic`) → 1 if that stage failed this job, else 0. |

In `observe` mode, accepted (but not applied) matches are additionally reported
per job as `reuseMatch: {assetId, runId}` so a reviewer can diff the reused
source against the computed result.

The gate names in `rejectionsByGate` are the stable `REASON_*` identifiers from
`src/photo_server/reuse.py`: `same_asset`, `fingerprint_version_mismatch`,
`unknown_model_digest`, `pipeline_mismatch`, `model_name_mismatch`,
`model_digest_mismatch`, `aspect_ratio_mismatch`, `missing_capture_time`,
`capture_time_mismatch`, `camera_mismatch`, `document_or_screenshot`,
`visible_text`, `phash_distance`, `dhash_distance`, `pixel_similarity`.

## Verification

- Unit: `cd backend && ../.venv/bin/python -m pytest tests/`
- Integration (disposable Postgres/S3, no production data, no AI services):
  `pytest backend/tests/` with `PHOTO_RUN_INTEGRATION=1` and the compose
  Postgres running. `tests/test_burst_integration.py` drives a full burst
  through import → preview → AI analysis under both `observe` and `on`, and
  verifies the counters, clustering, and reversible best-shot selection.
