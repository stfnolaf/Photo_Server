# Session 1: Health, readiness, and structured logging

Work in `/home/stephen/dev/photo_server`.

Implement the first production-hardening slice. Do not implement
authentication, metrics, backup replication, restore verification, or cleanup
in this session.

## Context

The API currently exposes `/health`, which checks storage, reports queue counts,
and probes optional AI services. Dependency failures can still turn health into
an unexpected error. Worker and maintenance activity is not consistently
correlated in logs.

Relevant areas include:

- `backend/src/photo_server/api.py`
- `backend/src/photo_server/service.py`
- `backend/src/photo_server/worker.py`
- `backend/src/photo_server/ai_worker.py`
- `backend/src/photo_server/config.py`
- `backend/src/photo_server/api_schemas.py`
- `backend/tests/`

## Requirements

- Add `/livez` for process liveness with no dependency checks.
- Add `/readyz` for database and required object-storage readiness.
- Keep `/health` as the detailed operational endpoint, but make dependency
  failures explicit and structured rather than allowing unexpected exceptions.
- Preserve the existing health response fields unless an additive change is
  necessary.
- Add worker heartbeat/staleness information where it can be done without a
  schema migration; clearly distinguish “worker not configured” from “worker
  stale”.
- Introduce structured application logging with stable event names and useful
  correlation fields where available: asset ID, batch ID, job ID, operation ID,
  stage, attempt, duration, and error class.
- Never log passwords, tokens, API keys, signed URLs, or image bytes.
- Make maintenance-loop failures visible in logs while preserving the current
  best-effort behavior.

## Verification

- Add unit and API tests for healthy, degraded, and unavailable database/storage
  states.
- Test that `/livez` remains successful when dependencies are unavailable.
- Test readiness failure status and response shape.
- Test representative structured log records and secret redaction.
- Run the relevant backend tests, OpenAPI drift checks, and lint.
- Update `openapi/openapi.json` and generated frontend API types if response
  contracts change.

## Stop condition

Finish with a concise summary of changed files, test commands/results, and any
follow-up decisions. Do not start metrics or authentication.
