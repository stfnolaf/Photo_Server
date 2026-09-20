#!/usr/bin/env bash
#
# run_all_tests.sh
# ----------------
# Run the full photo-server test suite and write a single, reviewable report.
#
# Intended workflow (run this yourself, with the coding agent OFF so your AI
# compute is free):
#
#   1. Make sure the compose stack is up:
#        docker compose -f compose.yaml up -d
#      (postgres, api, worker, ai-worker, web, ollama; S3 must be reachable)
#
#   2. Run this script:
#        ./run_all_tests.sh
#
#   3. Later, point your coding agent at the report, e.g.:
#        "Review test-reports/latest.txt and summarize any failures."
#
# Reports are written to (repo-relative):
#   test-reports/test-run-<UTC-timestamp>.txt   (one file per run)
#   test-reports/latest.txt                     (always the most recent run)
#
# Sections run, in order:
#   0. Preflight / environment + service status   (informational)
#   1. Lint (ruff, backend)
#   2. OpenAPI spec drift (scripts/check_openapi.py)       (no services needed)
#   3. Backend tests: unit + integration + AI-worker e2e   (PHOTO_RUN_INTEGRATION=1)
#   4. Frontend typecheck (tsc) + unit tests (vitest)      (skipped if node/npm missing)
#
# Exit code: 0 if every recorded section passed (or was skipped), 1 if any failed,
#            2 if the environment is unusable (e.g. venv missing).

set -uo pipefail

# --- Resolve paths ----------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$SCRIPT_DIR"
cd "$ROOT"

VENV="$ROOT/.venv"
PY="$VENV/bin/python"
RUFF="$VENV/bin/ruff"
COMPOSE_FILE="$ROOT/compose.yaml"
FE_DIR="$ROOT/frontend/web"

REPORT_DIR="$ROOT/test-reports"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
REPORT="$REPORT_DIR/test-run-$STAMP.txt"
LATEST="$REPORT_DIR/latest.txt"
mkdir -p "$REPORT_DIR"

# Start the report fresh.
: > "$REPORT"

# --- Logging helpers --------------------------------------------------------
# Write a line (or block) to both stdout and the report.
log() {
  printf '%s\n' "$*" | tee -a "$REPORT"
}

# Run a command, teeing its combined output to stdout and the report.
# Returns the command's own exit code (not tee's).
run_capture() {
  "$@" 2>&1 | tee -a "$REPORT"
  return "${PIPESTATUS[0]}"
}

# Record a section result and accumulate a human-readable summary.
OVERALL_FAIL=0
SUMMARY=""
record() {
  local name="$1" status="$2"
  SUMMARY+="$(printf '  %-28s %s' "$name" "$status")"$'\n'
  if [ "$status" = "FAIL" ]; then
    OVERALL_FAIL=1
  fi
}

# Always copy the (possibly partial) report to latest and show where it is.
cleanup() {
  local ec=$?
  if [ -f "$REPORT" ]; then
    cp -f "$REPORT" "$LATEST" 2>/dev/null || true
  fi
  echo
  echo "============================================================"
  echo " Done (exit $ec)."
  echo " Report : $REPORT"
  echo " Latest : $LATEST"
  echo "============================================================"
  exit "$ec"
}
trap cleanup EXIT

# --- Report header ----------------------------------------------------------
log "==================================================================="
log " photo-server test run"
log "==================================================================="
log "Started (UTC)  : $(date -u '+%Y-%m-%d %H:%M:%S')"
log "Host           : $(hostname 2>/dev/null || echo unknown)"
log "Repo root      : $ROOT"
if command -v git >/dev/null 2>&1 && git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  log "Git commit     : $(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null)"
  log "Git branch     : $(git -C "$ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null)"
  if [ -n "$(git -C "$ROOT" status --porcelain 2>/dev/null)" ]; then
    log "Git working tree: DIRTY (uncommitted changes present)"
  else
    log "Git working tree: clean"
  fi
else
  log "Git commit     : (not a git repo or git unavailable)"
fi
log "Python         : $([ -x "$PY" ] && "$PY" --version 2>&1 || echo "MISSING ($PY)")"
log "API port       : ${PHOTO_API_PORT:-8000}"
log "Web port       : ${PHOTO_WEB_PORT:-3000}"
log "==================================================================="
log

# --- Load .env (Settings needs S3 endpoint, Postgres password, ports) --------
if [ -f "$ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$ROOT/.env" 2>/dev/null || log "WARNING: failed to source $ROOT/.env (continuing with current environment)"
  set +a
  log "Loaded environment from $ROOT/.env"
else
  log "WARNING: no .env at $ROOT/.env — S3/Postgres settings may be missing; integration tests may fail."
fi
log

# --- Environment guard ------------------------------------------------------
if [ ! -x "$PY" ]; then
  log "ERROR: Python venv not found at $PY."
  log "       Create it with:  python3 -m venv .venv && .venv/bin/pip install -e 'backend[dev]'"
  exit 2
fi

# --- Section 0: Preflight / service status (informational) ------------------
log "==================================================================="
log " SECTION 0: Preflight / service status (informational)"
log "==================================================================="
API_PORT="${PHOTO_API_PORT:-8000}"

if command -v docker >/dev/null 2>&1 && [ -f "$COMPOSE_FILE" ]; then
  log "\$ docker compose -f compose.yaml ps"
  docker compose -f "$COMPOSE_FILE" ps 2>&1 | tee -a "$REPORT" || true
else
  log "(docker compose not available or compose.yaml missing — skipping service snapshot)"
fi
log

if command -v curl >/dev/null 2>&1; then
  log "Probing API health: http://localhost:${API_PORT}/health"
  if curl -fsS "http://localhost:${API_PORT}/health" 2>&1 | tee -a "$REPORT"; then
    log "API health: OK"
  else
    log "API health: FAILED — integration tests that need live backends may fail."
  fi
else
  log "(curl not available — skipping API health probe)"
fi
log

# --- Section 1: Lint (ruff, backend) ----------------------------------------
log "==================================================================="
log " SECTION 1: Lint (ruff, backend)"
log "==================================================================="
if [ -x "$RUFF" ]; then
  run_capture bash -c "cd '$ROOT/backend' && '$RUFF' check ."
  rc=$?
  if [ "$rc" -eq 0 ]; then
    log "RESULT: Lint (ruff) -> PASS"
    record "lint-ruff" "PASS"
  else
    log "RESULT: Lint (ruff) -> FAIL (exit $rc)"
    record "lint-ruff" "FAIL"
  fi
else
  log "ruff not found at $RUFF — skipping lint"
  record "lint-ruff" "SKIP"
fi
log

# --- Section 2: OpenAPI spec drift ------------------------------------------
# openapi/openapi.json is a checked-in artifact (docs/openapi-codegen-plan.md
# Phase 0): regenerate it from create_app() into a temp path and diff. No
# services are contacted. Phase 4 turns on --strict-coverage here, from which
# point on every JSON operation must carry a response schema.
log "==================================================================="
log " SECTION 2: OpenAPI spec drift (scripts/check_openapi.py)"
log "==================================================================="
run_capture bash -c "cd '$ROOT' && '$PY' scripts/check_openapi.py"
rc=$?
if [ "$rc" -eq 0 ]; then
  log "RESULT: OpenAPI spec drift -> PASS"
  record "spec-drift" "PASS"
else
  log "RESULT: OpenAPI spec drift -> FAIL (exit $rc)"
  record "spec-drift" "FAIL"
fi
log

# --- Section 3: Backend tests (unit + integration + AI-worker e2e) ----------
# This is the key section: with PHOTO_RUN_INTEGRATION=1 the live-backend
# integration tests (including the AI-worker end-to-end tests) are enabled.
# They use disposable Postgres databases and S3 buckets, and stub the GPU /
# Ollama model calls, so no GPU is required to run them.
log "==================================================================="
log " SECTION 3: Backend tests (unit + integration + AI-worker e2e)"
log "           PHOTO_RUN_INTEGRATION=1"
log "==================================================================="
run_capture bash -c "cd '$ROOT' && PHOTO_RUN_INTEGRATION=1 '$PY' -m pytest backend/tests/ -v"
rc=$?
if [ "$rc" -eq 0 ]; then
  log "RESULT: Backend tests -> PASS"
  record "backend-tests" "PASS"
else
  log "RESULT: Backend tests -> FAIL (exit $rc)"
  record "backend-tests" "FAIL"
fi
log

# --- Section 4: Frontend (typecheck + unit tests) ---------------------------
log "==================================================================="
log " SECTION 4: Frontend (tsc typecheck + vitest)"
log "==================================================================="
if command -v npm >/dev/null 2>&1 && [ -d "$FE_DIR" ]; then
  run_capture bash -c "cd '$FE_DIR' && npm run check"
  rc=$?
  if [ "$rc" -eq 0 ]; then
    log "RESULT: Frontend typecheck (tsc) -> PASS"
    record "frontend-typecheck" "PASS"
  else
    log "RESULT: Frontend typecheck (tsc) -> FAIL (exit $rc)"
    record "frontend-typecheck" "FAIL"
  fi
  log

  run_capture bash -c "cd '$FE_DIR' && npm test"
  rc=$?
  if [ "$rc" -eq 0 ]; then
    log "RESULT: Frontend unit tests (vitest) -> PASS"
    record "frontend-tests" "PASS"
  else
    log "RESULT: Frontend unit tests (vitest) -> FAIL (exit $rc)"
    record "frontend-tests" "FAIL"
  fi
  log
else
  log "node/npm not available or $FE_DIR missing — skipping frontend checks"
  record "frontend-typecheck" "SKIP"
  record "frontend-tests" "SKIP"
  log
fi

# --- Summary ----------------------------------------------------------------
log "==================================================================="
log " SUMMARY"
log "==================================================================="
log "$SUMMARY"
log "-------------------------------------------------------------------"
if [ "$OVERALL_FAIL" -eq 0 ]; then
  log "OVERALL: PASS (no failing sections)"
else
  log "OVERALL: FAIL (one or more sections failed)"
fi
log "Finished (UTC) : $(date -u '+%Y-%m-%d %H:%M:%S')"
log "==================================================================="

exit "$OVERALL_FAIL"
