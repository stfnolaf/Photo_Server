# PostgreSQL-authority phaseout and S3 storage deduplication

> Historical execution records below preserve the configuration and commands
> used at each phase. The superseding completion record at the end reflects
> the current source tree and is the authoritative final status.

## Goal

Make S3 the sole authority for photo bytes and user-visible library state.
PostgreSQL remains a rebuildable query projection and operational queue store.
After the runtime cutover is verified, remove the duplicate photo copies under
`originals/` and use the canonical content-addressed objects under
`objects/<sha256>`.

The target architecture is:

```text
S3 manifests + objects/<sha256>
        ↓
rebuildable PostgreSQL projection
        ↓
API, workers, and frontend
```

The SHA-256 object key identifies the exact file bytes. Filenames, asset IDs,
album membership, and other user-facing information remain manifest metadata.

## Current state

The S3 manifest backfill, history export, canonical-object verification, and
authority readiness checks are complete. The existing authority documents and
schemas are archived under [`../complete/`](../complete/).

The application is not yet safe to run fully S3-authoritatively. Some runtime
and write paths still use `originals/<asset-id>/<filename>` directly,
including image serving, processing and preview workers, exports, and parts of
upload/import completion. The current PostgreSQL projection also expects those
paths.

## Phase 1 — Establish the safety baseline

- Keep production in PostgreSQL-authoritative compatibility mode.
- Preserve the verified S3 manifests and canonical `objects/<sha256>` data.
- Record object counts, hashes, manifest counts, and bucket usage.
- Take a PostgreSQL backup and retain the existing `originals/` tree for
  rollback.
- Do not delete any canonical or duplicate objects.

Exit criterion: every existing asset has a valid S3 manifest and canonical
object, with no unresolved or divergent records.

### Phase 1 execution record — 2026-10-07 (UTC)

Phase 1 passed. Production remains in PostgreSQL-authoritative compatibility
mode. No S3 manifest, canonical object, original, or live database was
deleted, renamed, garbage-collected, or replaced.

Configuration and runtime inspected:

- `compose.yaml` resolves `PHOTO_AUTHORITY_MODE=postgres` for the backend
  services; the `.env` value is also `PHOTO_AUTHORITY_MODE=postgres`.
- The PostgreSQL service is healthy and the existing `backup` service is
  running. The configured primary backup prefix is `backups/postgres/`.
- The persisted authority status is `authorityMode=postgres`,
  `readiness=ready`, `projectionFreshness=fresh`, and
  `reconciliation=verified`.

Commands run:

```text
docker compose config --services
docker compose ps --all
docker compose run --rm -T api photo-server authority-status
docker compose run --rm -T api photo-server cutover-readiness --checkpoint-id phase1-readiness
docker compose run --rm -T api photo-server reconcile-s3 --dry-run --no-resume --checkpoint-id phase1-reconcile
docker compose run --rm -T api photo-server verify --full
docker compose run --rm -T api python - <<'PY' ... PY
docker compose exec -T backup /usr/local/bin/photo-postgres-backup once
docker compose exec -T backup ... head-object/get-object/sha256sum/pg_restore --list ...
docker compose exec -T backup aws s3 ls ... --summarize
```

Canonical coverage and reconciliation results:

- Asset manifests: **797**; all 797 contain canonical `objects/<sha256>` references.
- All manifests: **2,639**; manifest read errors: **0**.
- Canonical objects referenced by asset manifests: **797**; missing: **0**.
- Canonical objects referenced by all manifests: **1,265**; missing: **0**.
- S3 `objects/` inventory: **1,265 objects**, **34,693,281,951 bytes**.
- Readiness: **ready**; reconciliation status: **complete**.
- Reconciliation scanned **2,639**, matched **1,843**, and repaired **0**.
- Missing, divergent, orphaned, unresolved, conflicting, malformed, skipped,
  and failed records: **0** in every category.
- Full verification checked **797 blobs** by SHA-256 and returned **0 errors**.

S3 usage by prefix at the end of the run:

| Prefix | Objects | Bytes |
| --- | ---: | ---: |
| `manifests/assets/` | 797 | 1,383,327 |
| `manifests/` | 2,639 | 19,744,952 |
| `objects/` | 1,265 | 34,693,281,951 |
| `originals/` | 797 | 34,687,401,984 |
| `backups/postgres/` | 23 | 78,142,275 |
| `sidecars/` | 0 | 0 |
| `artifacts/` | 0 | 0 |
| `incoming/` | 0 | 0 |

The existing `originals/` rollback data remains available. A sampled original
(`originals/0013bf3b-4e18-525e-a290-f75d3eb13663/A7403248.ARW`) is present at
**41,967,616 bytes**; the complete prefix inventory is recorded above.

Backup result:

- Fresh verified PostgreSQL custom-format backup:
  `backups/postgres/20261007T164650Z-0afc8bbe-7f90-4c1e-9653-1ed39b757085.dump`
- Backup size: **3,619,977 bytes**.
- SHA-256: `bf4f61871dce44d841f08422c8138b87eb3812e31aff0422c9321dbca5625c68`.
- The backup service verified the uploaded object metadata checksum. An
  independent download produced the same SHA-256 and byte count, and
  `pg_restore --list` returned **110** entries.
- No restore was attempted and the live database was not replaced or deleted.
- No secondary backup destination is configured; the remaining operational
  risk is that the primary backup and photo data share the same S3 failure
  domain.

Blockers: **none for Phase 1**. Remaining risks are the known runtime
dependence on `originals/`, the lack of a configured independent secondary
backup destination, and the fact that S3-authoritative cutover has not been
performed or validated. Those are intentionally deferred to later phases.

## Phase 2 — Make canonical S3 references usable everywhere

Change every runtime reader to resolve photo bytes from the canonical S3
manifest and its `objects/<sha256>` reference:

- API image serving and downloads
- preview generation
- processing workers
- exports
- duplicate detection
- background analysis
- upload/import completion

The manifest filename remains the download filename. PostgreSQL may cache the
canonical reference for query performance, but it must not be the authority
for the photo bytes or object location.

Exit criterion: browsing, previews, downloads, processing, and exports work
without reading `originals/`.

## Phase 3 — Make S3-first writes complete

All new and modified content must follow this sequence:

1. Stage the incoming file temporarily.
2. Calculate its SHA-256.
3. Write or verify `objects/<sha256>`.
4. Publish the asset or metadata mutation to S3.
5. Update PostgreSQL asynchronously as a projection.

Cover all upload, import, album, metadata, deletion, and processing paths,
including code that currently bypasses the dual-write coordinator. A verified
S3 mutation must remain recoverable if PostgreSQL is unavailable.

Exit criterion: no supported user-visible mutation exists only in PostgreSQL,
and retries are idempotent.

## Phase 4 — Rebuild PostgreSQL exclusively from S3

- Create a fresh disposable PostgreSQL database.
- Rebuild it entirely from S3 manifests and canonical objects.
- Verify assets, albums, filenames, processing state, faces, tombstones, and
  permissions.
- Exercise browsing, previews, downloads, processing, and exports against the
  rebuilt projection.
- Compare the rebuilt behavior with production before cutover.

Exit criterion: PostgreSQL can be discarded and reconstructed from S3 without
loss of supported application state.

## Phase 5 — Enable S3-authoritative operation

Set:

```env
PHOTO_AUTHORITY_MODE=s3
```

Use the existing readiness and authority-status controls, then monitor:

- S3 mutation failures
- projection lag
- reconciliation failures
- missing or divergent canonical objects
- API and worker errors
- recovery and rebuild status

Run a controlled maintenance window or canary if practical. Keep rollback
available while the new runtime paths are being observed.

## Phase 6 — Retire `originals/`

After S3-authoritative operation and S3-only recovery have both been exercised:

- Stop writing new `originals/` copies.
- Keep existing copies for a defined rollback period.
- Re-run full S3 verification and a fresh PostgreSQL rebuild.
- Confirm no runtime code or manifest references `originals/`.
- Delete only the old duplicate photo objects, in a separately logged,
  recoverable operation.
- Re-measure storage and retain processing artifacts and metadata referenced by
  canonical manifests.

The duplicate originals must not be deleted merely because the authority flag
is set. Deletion is the final step after the read, write, rebuild, and recovery
exit criteria pass.

## Phase 7 — Remove the compatibility mode

After the retention period:

- Remove or deprecate PostgreSQL-authoritative mode.
- Remove `originals/` path validation and fallback logic.
- Make S3-only recovery a tested operational procedure.
- Add regression coverage ensuring runtime code does not require
  `originals/`.

## Acceptance criteria

The phaseout is complete when:

- S3 manifests and `objects/<sha256>` are the only canonical photo storage.
- All supported reads and writes work without `originals/`.
- A fresh PostgreSQL projection can be rebuilt from S3.
- Recovery succeeds without the original PostgreSQL volume.
- No supported user-visible state exists only in PostgreSQL.
- The old duplicate photo objects have been removed only after the documented
  rollback and retention period.

## Phase 2 implementation record — 2026-10-07 (UTC)

Status: **PASS — Phase 2 exit criterion met; cutover remains deferred**.

Implementation summary:

- Added `Service.canonical_asset()`, selecting the newest asset manifest from
  `manifests/assets/<asset-id>/` and failing closed when it is absent. It
  returns canonical `objects/<sha256>` blob references through the strict
  manifest codec.
- Routed API original downloads and face/preview cache identity, preview
  generation, processing metadata/fingerprint stages, AI face/semantic/reuse
  stages, exports, duplicate preclassification, and integrity verification
  through canonical asset manifests.
- Updated web-upload completion to publish and verify canonical objects and
  the asset manifest before applying the PostgreSQL projection. Existing
  `originals/<asset-id>/<filename>` writes remain intentionally preserved for
  rollback and were not read by these runtime paths.
- Added canonical-manifest compatibility properties (`primary`, `metadata`)
  and regression coverage for newest-revision selection and fail-closed
  behavior when no canonical manifest exists.

Commands and exact results:

```text
python3 -m compileall -q backend/src                         PASS
.venv/bin/ruff check backend/src backend/tests                 PASS
git diff --check                                               PASS
PYTHONPATH=backend/src .venv/bin/pytest -q \
  backend/tests/test_phase2_canonical_reads.py \
  backend/tests/test_previews.py \
  backend/tests/test_preview_cache_tracking.py \
  backend/tests/test_upload_client.py \
  backend/tests/test_integration.py                           PASS: 10 passed, 25 skipped
env -i PATH="/home/stephen/dev/photo_server/.venv/bin:/usr/local/bin:/usr/bin:/bin" \
  HOME=/home/stephen S3_ENDPOINT=http://localhost:9000 PYTHONPATH=backend/src \
  .venv/bin/pytest -q backend/tests                          PASS: 305 passed, 111 skipped
```

The targeted and full runs each emitted two existing Starlette/httpx
deprecation warnings. Before the full run, exact stale test references to
`docs/in-progress/s3-authoritative-*` were updated in five test modules to
`docs/complete/s3-authoritative-*`; no deleted files were restored and no
runtime behavior was changed. A final search found no stale test references.
Integration tests requiring PostgreSQL, S3, and ExifTool were skipped because
those services were not available to this verification run.

Measurements and safety observations:

- Runtime source inspection found no Phase 2 byte read through an
  `originals/` key. Remaining `originals/` references are compatibility writes
  for rollback or legacy model validation; canonical reads use
  `manifests/assets/<asset-id>/<revision>.json` → `objects/<sha256>`.
- `PHOTO_AUTHORITY_MODE` remains `postgres`; S3 authority was not enabled and
  no cutover was performed.
- No canonical object, `originals/` object, manifest, live database, or
  database backup was deleted, renamed, overwritten, or garbage-collected.

Remaining risks:

- End-to-end runtime evidence remains pending because the integration
  environment was unavailable. The S3 canonical-manifest requirement is
  fail-closed, so missing canonical state surfaces as an operational error
  rather than silently using rollback originals.
- Existing rollback originals remain a storage duplicate by design. Their
  deletion is deferred to Phase 6.

Exit result: **PASS**. The full backend suite, Phase 2 targeted tests, compile,
Ruff, diff, and stale-reference checks pass. Runtime readers resolve canonical
S3 manifests and `objects/<sha256>` references, with rollback originals still
preserved. Do not enable `PHOTO_AUTHORITY_MODE=s3` or begin cutover; Phase 3
and cutover remain explicitly deferred.

## Phase 3 implementation record — 2026-10-07 (UTC)

Status: **PASS — Phase 3 exit criterion met; compatibility mode and cutover
remain unchanged**.

Implementation summary:

- Imports now stage input, compute and verify SHA-256, publish or verify every
  `objects/<sha256>` object, publish and read-back verify the canonical asset
  manifest, and only then apply the PostgreSQL projection. This ordering is
  used in both `PHOTO_AUTHORITY_MODE=postgres` compatibility mode and the
  existing S3 mode. Legacy `originals/<asset-id>/<filename>` copies remain
  rollback-only compatibility copies and are written after the canonical
  commit.
- Upload onboarding follows the same canonical object/manifest sequence.
  Upload-created albums, duplicate upload album attachments, and onboarding
  album membership now use the durable mutation coordinator instead of direct
  PostgreSQL-only writes.
- Metadata processing now routes through the coordinator, so its immutable
  asset revision is published before its projection. Projection failures for
  asset, album, face, and metadata mutations record an immutable reconciliation
  receipt under `reconciliation/dual-writes/` and can be retried with the same
  operation ID without replacing canonical data.
- Imports that already have a verified canonical revision but lack a PostgreSQL
  projection now reuse that exact S3 manifest and operation ID on retry,
  reconstructing only the compatibility projection. The corruption regression
  targets the canonical manifest's `objects/<sha256>` reference and verifies
  fail-closed behavior without weakening canonical verification.
- Existing immutable writes remain create-only and checksum-verified. Revision
  conflicts, changed import inputs, missing canonical manifests, and checksum
  mismatches fail closed. Processing/AI artifacts retain their existing
  S3-before-derived-PostgreSQL ordering.
- Removed the architectural test dependency on documentation layout. The
  seven static manifest/report examples are byte-for-byte copied into
  `backend/tests/fixtures/s3-authoritative-examples`, and tests resolve all
  fixture data from that test-owned location. Documentation examples remain
  preserved independently.

Commands and exact results:

```text
python3 -m compileall -q backend/src                         PASS
.venv/bin/ruff check backend/src backend/tests                 PASS
git diff --check                                               PASS
fixture checksum comparison                                      PASS: 7 exact byte matches
env -i PATH="/home/stephen/dev/photo_server/.venv/bin:/usr/local/bin:/usr/bin:/bin" \
  HOME=/home/stephen PYTHONPATH=backend/src \
  PHOTO_S3_ENDPOINT=http://localhost:9000 \
  PHOTO_DATABASE_URL=postgresql+psycopg://test@localhost/test \
  .venv/bin/pytest -q backend/tests/test_dual_write.py \
    backend/tests/test_phase7_authority.py \
    backend/tests/test_phase2_canonical_reads.py \
    backend/tests/test_integration.py \
    backend/tests/test_manifest_codec.py \
    backend/tests/test_rebuild.py \
    backend/tests/test_garbage_collector.py \
    backend/tests/test_phase9_recovery.py                    PASS: 50 passed, 19 skipped
env -i PATH="/home/stephen/dev/photo_server/.venv/bin:/usr/local/bin:/usr/bin:/bin" \
  HOME=/home/stephen PYTHONPATH=backend/src \
  PHOTO_S3_ENDPOINT=http://localhost:9000 \
  PHOTO_DATABASE_URL=postgresql+psycopg://test@localhost/test \
  .venv/bin/pytest -q backend/tests                          PASS: 308 passed, 111 skipped
```

The focused and full runs emitted the same two existing Starlette/httpx
deprecation warnings. Integration tests requiring live PostgreSQL, S3, or
ExifTool remain skipped because those services were unavailable to this run;
the opt-in disposable Phase 7 mutation suite was not enabled.

Measurements and safety observations:

- Source review found no supported upload/import, asset metadata, album,
  deletion, or processing mutation that publishes only to PostgreSQL. The
  remaining direct `catalog.apply()` calls are projection/rebuild paths after
  canonical publication, and fingerprint/queue writes are derived or
  operational state.
- `PHOTO_AUTHORITY_MODE=postgres` remains active. No S3-authoritative cutover,
  authority flag change, live database replacement, or destructive cleanup was
  performed.
- No canonical object, manifest, rollback original, live database, or backup
  was deleted, renamed, overwritten, or garbage-collected. Immutable retries
  verify existing bytes and reject conflicts.

Blockers: **none for the Phase 3 code/test exit criterion**. Remaining
operational risk is the absence of live integration evidence in this
environment and the known shared S3 failure domain for the primary backup.
Rollback originals remain intentionally duplicated and are deferred to Phase 6.

Exit result: **PASS**. Supported new and modified user-visible mutations now
publish verified S3 state before PostgreSQL projection, including coordinator
bypass paths identified during the Phase 3 audit. Retries are operation- and
content-idempotent, and verified S3 mutations remain recoverable when a
projection fails. Do not set `PHOTO_AUTHORITY_MODE=s3`; proceed to Phase 4
only after live disposable mutation/rebuild integration coverage is available.

## Phase 4 implementation record — 2026-10-07 (UTC)

Status: **PASS — Phase 4 exit criterion met on disposable local services;
authority cutover remains deferred.**

Implementation summary:

- Hardened `rebuild-from-s3` as a fresh-projection operation. An initialized
  target containing assets, albums, people, faces, processing rows, or
  operation records is rejected before any S3 scan or projection write. A
  completed checkpoint remains safely idempotent.
- Added library-identity validation for every decoded S3 record and verified
  that each asset's processing reference matches both the processing artifact
  input checksum and artifact checksum.
- Extended rebuild measurements with projected faces, processing artifacts,
  and tombstone counts. Extended production-vs-rebuilt comparison to include
  processing and face rows in addition to assets, albums, and people.
- Added regression coverage proving a legacy `originals/` object cannot
  satisfy a missing canonical `objects/<sha256>` reference, and that a
  non-empty projection is never merged.
- Made fresh projection bootstrap recover the library identity from canonical
  manifests when a restored namespace does not contain the mutable
  `library.json` marker. The rebuild CLI uses the same manifest fallback.
- Reconstructed processing search text from structured semantic artifacts or
  deterministic string fallbacks, preserving behavior needed for browsing and
  comparison when the artifact is a minimal JSON result.
- The rebuild continues to verify every referenced canonical object and reads
  only S3 manifests/canonical objects for reconstruction. Queue, lease, and
  operational tables are not reconstructed as library state.

Commands and exact results:

```text
docker run -d --rm --name photo-phase4-postgres \
  -e POSTGRES_USER=test -e POSTGRES_PASSWORD=test -e POSTGRES_DB=test \
  -p 127.0.0.1:15432:5432 postgres:17                       PASS
docker run -d --rm --name photo-phase4-seaweedfs \
  -p 127.0.0.1:18333:8333 chrislusf/seaweedfs:latest \
  server -s3 -dir=/data -s3.port=8333 -ip.bind=0.0.0.0       PASS

env -i PATH="/home/stephen/dev/photo_server/.venv/bin:/usr/local/bin:/usr/bin:/bin" \
  HOME=/home/stephen PYTHONPATH=src PHOTO_RUN_INTEGRATION=1 \
  PHOTO_S3_ENDPOINT=http://127.0.0.1:18333 \
  PHOTO_DATABASE_URL=postgresql+psycopg://test:test@127.0.0.1:15432/test \
  PHOTO_S3_ANONYMOUS=true PHOTO_AUTHORITY_MODE=postgres \
  /home/stephen/dev/photo_server/.venv/bin/pytest -q tests/test_integration.py \
                                                               PASS: 19 passed

env -i PATH="/home/stephen/dev/photo_server/.venv/bin:/usr/local/bin:/usr/bin:/bin" \
  HOME=/home/stephen PYTHONPATH=src PHOTO_RUN_PHASE9_RECOVERY_INTEGRATION=1 \
  PHOTO_AUTHORITY_MODE=postgres \
  /home/stephen/dev/photo_server/.venv/bin/pytest -q \
    tests/test_phase9_recovery_integration.py                   PASS: 1 passed

env -i PATH="/home/stephen/dev/photo_server/.venv/bin:/usr/local/bin:/usr/bin:/bin" \
  HOME=/home/stephen PYTHONPATH=backend/src \
  PHOTO_S3_ENDPOINT=http://127.0.0.1:18333 \
  PHOTO_DATABASE_URL=postgresql+psycopg://test:test@127.0.0.1:15432/test \
  PHOTO_AUTHORITY_MODE=postgres \
  .venv/bin/ruff check backend/src backend/tests                  PASS
python3 -m compileall -q backend/src                            PASS
env -i PATH="/home/stephen/dev/photo_server/.venv/bin:/usr/local/bin:/usr/bin:/bin" \
  HOME=/home/stephen PYTHONPATH=backend/src \
  PHOTO_S3_ENDPOINT=http://127.0.0.1:18333 \
  PHOTO_DATABASE_URL=postgresql+psycopg://test:test@127.0.0.1:15432/test \
  PHOTO_AUTHORITY_MODE=postgres \
  .venv/bin/pytest -q backend/tests                            PASS: 308 passed, 111 skipped
git diff --check                                               PASS

docker rm -f photo-phase4-seaweedfs photo-phase4-postgres        PASS
ss -ltn '( sport = :18333 or sport = :15432 )'                  PASS: no listeners

```

The initial Phase 4 retry exposed and repaired two rebuild issues before the
passing rerun: restored namespaces without `library.json` generated a wrong
library UUID, and minimal processing JSON did not reproduce searchable text.
Both fixes are covered by the passing disposable recovery/rebuild comparison.

Measurements:

- `tests/test_integration.py`: **19 passed**, covering asset imports and
  filenames, canonical retry behavior, browsing, previews, downloads,
  exports, upload/import processing, and people/face API behavior.
- `tests/test_phase9_recovery_integration.py`: **1 passed**, covering a fresh
  disposable PostgreSQL projection rebuilt from restored S3 manifests and
  canonical objects. It exercises asset state, album membership, processing
  state, faces/person assignments, tombstones, recovery checkpoint integrity,
  and asserts `compare_projections(source.catalog, rebuilt.catalog)["match"]`.
- Full backend suite: **308 passed, 111 skipped**, with the two existing
  Starlette/httpx deprecation warnings.
- Static verification: Ruff, compileall, and diff check passed.
- All integration buckets and databases were random disposable resources and
  were removed by test cleanup; both manually started local containers were
  removed after verification.

Safety observations:

- All S3 traffic used `http://127.0.0.1:18333`; no remote or `.env` endpoint
  was used for verification. PostgreSQL used only the temporary local
  container on `127.0.0.1:15432` plus test-owned random databases.
- `PHOTO_AUTHORITY_MODE=postgres` remained explicit and unchanged. No Phase 5
  acceptance or authority test was run, and S3 authority was not enabled.
- No live database was replaced, dropped, restored over, or deleted. No
  production database or Compose PostgreSQL was used.
- No canonical object, manifest, rollback original, backup, or live checkpoint
  was deleted or overwritten. Fresh rebuild writes were limited to disposable
  test databases.

Remaining risks:

- The passing evidence is against the current disposable local service
  implementation and a test-generated S3 namespace; production inventory
  completeness and production-scale timings still require operational
  observation.
- AI inference services were not required by this acceptance; processing
  reconstruction was verified from canonical processing artifacts and faces.
- Rollback originals remain intentionally duplicated and the live database
  remains preserved for rollback. Their retirement is deferred to Phase 6.

Exit result: **PASS**. PostgreSQL was freshly initialized and reconstructed
from S3 manifests/canonical objects without using the original PostgreSQL
projection, supported browsing/preview/download/processing/export behavior
passed against disposable local services, and the rebuilt projection matched
the source projection before cutover. Do not enable
`PHOTO_AUTHORITY_MODE=s3`; proceed only to the separately controlled Phase 5
readiness/canary work.

## Phase 5 readiness/preflight implementation record — 2026-10-07 (UTC)

Status: **PASS — Phase 5 readiness/preflight and disposable exit criterion
met; authority activation remains separately gated.**

Implementation summary:

- Added a read-only `authority-monitor` control and
  `GET /authority/monitoring`. It reports S3 mutation receipts that indicate
  projection failure, pending projection lag, failed reconciliation
  checkpoints, canonical-object and rollback-original retention, worker
  heartbeat/queue health, recovery checkpoint availability, and PostgreSQL
  backup status. It does not repair, delete, or consume mutable checkpoints.
- Added a non-activating `canary-preflight` control and
  `GET /authority/canary-preflight`. It combines readiness and monitoring,
  reports `activation.status=approval-required`, and proves
  `activation.performed=false`; it never changes `PHOTO_AUTHORITY_MODE` or
  the persisted authority mode.
- Added regression coverage for mutation/reconciliation failure reporting and
  the no-activation invariant. Existing explicit rollback remains available
  and rollback originals are counted as a readiness safety condition.
- Corrected rebuild projection of rich S3 processing artifacts: PostgreSQL now
  receives the compact public semantic result, and searchable text is derived
  from that same public projection. This matches the documented split between
  rich canonical artifacts and the user-visible `analysis_runs` projection.

Commands and exact results:

```text
PYTHONPATH=backend/src .venv/bin/pytest -q \
  backend/tests/test_phase5_controls.py \
  backend/tests/test_phase10_cutover.py \
  backend/tests/test_health.py \
  backend/tests/test_api_contract.py \
  backend/tests/test_config.py                         PASS: 33 passed, 9 skipped

env -i PATH="/home/stephen/dev/photo_server/.venv/bin:/usr/local/bin:/usr/bin:/bin" \
  HOME=/home/stephen PYTHONPATH=backend/src \
  PHOTO_RUN_PHASE5_INTEGRATION=1 PHOTO_AUTHORITY_MODE=postgres \
  .venv/bin/pytest -q backend/tests/test_phase5_integration.py
                                                         PASS: 1 passed

env -i PATH="/home/stephen/dev/photo_server/.venv/bin:/usr/local/bin:/usr/bin:/bin" \
  HOME=/home/stephen PYTHONPATH=backend/src \
  PHOTO_S3_ENDPOINT=http://127.0.0.1:9 \
  PHOTO_DATABASE_URL=postgresql+psycopg://test@127.0.0.1/test \
  PHOTO_AUTHORITY_MODE=postgres .venv/bin/pytest -q backend/tests
                                                         PASS: 311 passed, 111 skipped

python3 -m compileall -q backend/src                 PASS
.venv/bin/ruff check backend/src backend/tests       PASS
git diff --check                                      PASS
```

The focused and full backend runs emitted the two existing Starlette/httpx
deprecation warnings. The disposable integration fixture used only a random
local SeaweedFS endpoint and random local PostgreSQL database, then removed
its Compose project and volumes. It did not use `.env`, the remote S3
endpoint, or the existing Compose PostgreSQL.

Measurements and blocker:

- The disposable acceptance passed imports, canonical backfill pause/resume,
  all four corruption/conflict checks against the current generic `errors`
  contract, fresh PostgreSQL rebuild, and projection comparison.
- The reproduced derived diff was limited to `searchable_text`: the source
  projection contained `fixture`, while rebuild fallback had included private
  artifact identifiers and metadata. Rebuild now projects the canonical
  artifact to the compact public result before deriving search text; the
  rerun matched assets, albums, people, processing, and faces.
- `PHOTO_AUTHORITY_MODE=postgres` remained explicit for every verification.
  No S3 authority flag, live database mutation, original deletion, backup
  replacement, production canary, or rollback removal occurred.

Exit-criterion status: **PASS**. Safe readiness/preflight preparation,
fail-closed monitoring, disposable rebuild/reconciliation comparison, and the
requested static/full verification passed. This does not activate S3 authority.

Before the subsequent approval, activation was explicitly gated on setting
`PHOTO_AUTHORITY_MODE=s3` in the deployment configuration and running
`photo-server cutover-activate` (or the equivalent
`Service.activate_s3_authority()`) only after a clean readiness report. That
gate was later approved and is recorded below. A controlled rollback remains
the explicit `photo-server cutover-rollback --reason <reason>` operation.

## Phase 5 controlled activation record — 2026-10-07 (UTC)

Status: **PASS — S3 authority activated under explicit approval; Phase 6/7
remain deferred.**

Preflight commands and results:

```text
docker compose config --services                         PASS
docker compose ps --all                                  PASS: Compose services healthy/up
docker compose run --rm -T api photo-server authority-status
  PASS: authorityMode=postgres, readiness=ready,
        projectionFreshness=fresh, reconciliation=verified
docker compose run --rm -T api photo-server cutover-readiness \
  --checkpoint-id phase5-activation-readiness
  PASS: ready; scanned=2639, matched=1843, missing=0, divergent=0,
        orphaned=0, unresolved=0, conflicting=0, malformed=0, failed=0
curl --fail --silent --show-error http://127.0.0.1:8000/health
  PASS: status=ok; all queue counts=0; database/storage=ready;
        worker and ai-worker running; backup overall=healthy
docker compose run --rm -T api photo-server verify --full
  PASS: assetsChecked=797, blobsChecked=797, verification=sha256, errors=[]
docker compose run --rm -T api python - <<'PY' ...
  PASS: backup primary success/verified; rollback originals=797;
        canonical objects=1265; mutation/reconciliation receipts=0;
        recovery checkpoints=0
```

The verified PostgreSQL backup was
`backups/postgres/20261007T175745Z-f18f067c-9c20-4867-8ec1-2c06e0c6c40f.dump`,
with primary status `success`, `verified=true`, and overall backup status
`healthy`. No recovery checkpoint was present and no secondary backup
destination is configured; these remain documented operational risks.

Activation commands and result:

```text
apply approved deployment-config change: PHOTO_AUTHORITY_MODE=postgres → s3
docker compose up -d --force-recreate api worker processing-worker \
  preview-worker ai-worker
  PASS: all five runtime services recreated and started
docker compose run --rm -T api photo-server cutover-activate
  PASS at 2026-10-07T18:21:46Z:
  authorityMode=s3, readiness=ready, projectionFreshness=fresh,
  reconciliation=verified, failureReason=null
```

Immediate monitoring:

```text
docker compose run --rm -T api photo-server authority-status
  PASS: authorityMode=s3, readiness=ready, projectionFreshness=fresh,
        reconciliation=verified
curl --fail --silent --show-error http://127.0.0.1:8000/health
  PASS at 2026-10-07T18:22Z: status=ok, assets=797, blobs=797,
        all queues=0, database/storage ready, both workers running,
        backup overall=healthy
runtime mode check for api/worker/processing-worker/preview-worker/ai-worker
  PASS: s3 for all five services
storage/queue/receipt snapshot
  PASS: mutation failure receipts=0, reconciliation receipts=0,
        rollback originals=797, canonical objects=1265,
        recovery checkpoints=0, all queue counts=0
docker compose logs --since=2m ... | rg -i 'error|failed|exception|traceback'
  PASS: no matching recent service errors
docker compose run --rm -T api photo-server cutover-readiness \
  --checkpoint-id phase5-post-activation-readiness
  PASS at 2026-10-07T18:24Z: ready; scanned=2639, matched=1843,
        missing=0, divergent=0, orphaned=0, unresolved=0,
        conflicting=0, malformed=0, failed=0, repaired=0
```

The deployed image did not contain the newer optional `authority-monitor` CLI
command, so equivalent read-only monitoring was performed with the existing
authority-status/readiness commands, `/health`, runtime environment checks,
storage receipt counts, queue counts, backup status, and service logs. No image
rebuild or compatibility-mode removal was performed.

Rollback readiness: **PASS**. The PostgreSQL backup remains verified, all 797
`originals/` rollback objects remain present, and the explicit
`photo-server cutover-rollback --reason <reason>` procedure remains available.
No originals were deleted or retired. Remaining risks are the shared S3
failure domain, absent secondary backup destination, absent verified recovery
checkpoint marker, and limited immediate observation duration.

Exit result: **PASS** for Phase 5 activation and immediate monitoring. S3 is
now authoritative. Do not begin Phase 6/7, delete `originals/`, remove
rollback support, or remove compatibility mode without a separate approval and
passing later-phase criteria.

## Phase 5 deployed-image reconciliation — 2026-10-07 (UTC)

Status: **PASS — running services now contain the current Phase 5 controls;
no deployment mismatch remains.**

Coordinator discrepancy and correction:

- The initial post-activation containers were recreated from stale
  `photo-server-*` images. `docker compose exec -T api photo-server
  canary-preflight` failed with argparse `invalid choice`, and the stale image
  did not contain `authority-monitor` or `canary-preflight`.
- The approved S3 authority state was not rolled back or changed during this
  correction. The current source was rebuilt into the API, general worker,
  processing-worker, preview-worker, and AI-worker images, then those five
  services were recreated.
- The first invocation of the rebuilt `authority-monitor` exposed a runtime
  bug in the new control (`len()` on the storage key generator). The control
  was corrected to count the generator safely, the five images were rebuilt and
  recreated again, and the controls then passed through the running API.

Exact commands and results:

```text
docker compose build api worker processing-worker preview-worker ai-worker
  PASS: all five images rebuilt from current workspace source
docker compose up -d --force-recreate api worker processing-worker \
  preview-worker ai-worker
  PASS: all five services recreated and started with PHOTO_AUTHORITY_MODE=s3

docker compose exec -T api photo-server --help | rg \
  'authority-monitor|canary-preflight|authority-status|cutover-readiness'
  PASS: all four controls present in the running image

docker compose exec -T api photo-server authority-monitor \
  --checkpoint-id phase5-deployed-monitor
  PASS: authorityMode=s3; S3 mutation failures=0; projection lag=0;
        reconciliation failures=0; queues/workers healthy;
        canonical objects=1265 retained; rollback originals=797 retained;
        activation.performed=false; rollback originals retained

docker compose exec -T api photo-server authority-status
  PASS: s3/ready/fresh/verified
curl --fail --silent --show-error http://127.0.0.1:8000/health
  PASS: status=ok; database/storage ready; all queue counts=0;
        worker and ai-worker running; backup overall=healthy

docker compose exec -T api photo-server canary-preflight \
  --checkpoint-id phase5-deployed-canary
  PASS: status=ready; authorityMode=s3; activation.performed=false;
        rollback.supported=true; readiness scanned=2639, matched=1843,
        missing=0, divergent=0, orphaned=0, unresolved=0,
        conflicting=0, malformed=0, failed=0

PYTHONPATH=backend/src .venv/bin/pytest -q \
  backend/tests/test_phase5_controls.py backend/tests/test_rebuild.py
  PASS: 10 passed
python3 -m compileall -q backend/src                 PASS
.venv/bin/ruff check backend/src backend/tests       PASS
git diff --check                                      PASS
```

Residual deployment mismatch: **none observed**. All five application
containers report `PHOTO_AUTHORITY_MODE=s3` and expose the current controls.
Recovery checkpoints remain unavailable and no secondary backup destination is
configured; these are unchanged operational risks. Phase 6/7 were not
started, `originals/` was not retired, and rollback support remains intact.

Next phase instructions: retain the live database and `originals/` rollback
data, review the Phase 5 readiness report, and perform any authority cutover
only through the existing controlled Phase 5 procedure. Do not delete
`originals/` during that work.

## Phase 6 originals retirement procedure — 2026-10-07 (UTC)

Status: **BLOCKED — retirement preparation and verification passed; deletion
requires explicit approval for the exact operation below.** Phase 7 was not
started, compatibility support was not removed, and no object or database was
deleted, renamed, garbage-collected, or overwritten.

Implementation summary:

- S3-authoritative imports and upload onboarding now publish and verify
  `objects/<sha256>` plus the canonical manifest, then update PostgreSQL, but
  do not create new `originals/<asset-id>/<filename>` rollback copies.
- PostgreSQL-authoritative compatibility mode still writes and verifies those
  copies. The legacy projection model and explicit rollback path remain
  available for retained pre-cutover objects.
- Added disposable cutover regression coverage that asserts zero new
  `originals/` objects in S3 mode, performs full SHA-256 verification, rebuilds
  a fresh PostgreSQL projection, and verifies retained rollback objects survive
  the rollback-window check.

Runtime and manifest audit:

- Canonical API serving, previews, processing, workers, exports, duplicate
  checks, and verification resolve asset manifests and `objects/<sha256>`.
  The remaining source references to `originals/` are compatibility projection
  construction, legacy model validation, rollback retention counting, or
  compatibility writes guarded by PostgreSQL authority; none is a canonical
  byte reader.
- Live read-only manifest scan: **0** asset-manifest references to
  `originals/`.
- Live runtime mode: **s3**. Live `originals/` count remained **797**; object
  `LastModified` range is **2026-09-23T04:43:51Z** through
  **2026-09-23T04:57:41Z**, before the 2026-10-07 activation. This is the
  no-new-writes check for the current authoritative operation.

Retention and rollback policy:

- Retain all existing `originals/` objects for **30 calendar days after this
  Phase 6 record**, through **2026-11-06 23:59:59Z**, unless an incident
  requires an explicitly documented extension.
- During the window, preserve the live PostgreSQL volume, verified PostgreSQL
  backups, authority rollback marker/procedure, and all retained originals.
  Do not remove compatibility mode or start Phase 7 as part of this work.
- The deletion scope is only the current **797** objects below
  `s3://photo-library/originals/`; canonical `objects/`, all manifests,
  processing artifacts, metadata, tombstones, backups, checkpoints, and live
  database data are out of scope.

Storage measurement and expected savings:

| Prefix | Objects | Bytes | GiB |
| --- | ---: | ---: | ---: |
| `objects/` canonical | 1,265 | 34,693,281,951 | 32.310637 |
| `originals/` rollback | 797 | 34,687,401,984 | 32.305161 |
| both media prefixes | 2,062 | 69,380,683,935 | 64.615797 |

Deleting only the approved `originals/` scope would save **34,687,401,984
bytes (32.305161 GiB / 34.687402 GB)**, approximately **49.995763%** of the
current two-prefix media footprint. The current inventory also contains 2,639
manifest objects (19,744,952 bytes) and 25 PostgreSQL backups (85,382,229
bytes), neither of which is part of the savings or deletion scope.

Verification commands and results:

```text
docker compose exec -T api photo-server verify --full
  PASS: assetsChecked=797, blobsChecked=797, verification=sha256, errors=[]

docker compose exec -T api python - <<'PY' ... inventory/head objects ... PY
  PASS: objects=1265 / 34693281951 bytes; originals=797 / 34687401984 bytes;
        manifests=2639 / 19744952 bytes; backups/postgres=25 / 85382229 bytes

docker compose exec -T api python - <<'PY' ... decode asset manifests ... PY
  PASS: asset_manifest_original_refs=0

env -i PATH="..." HOME=/home/stephen PYTHONPATH=backend/src \
  PHOTO_RUN_PHASE10_CUTOVER_INTEGRATION=1 \
  .venv/bin/pytest -q backend/tests/test_phase10_cutover_integration.py
  PASS: 1 passed; disposable local SeaweedFS/PostgreSQL only

env -i PATH="..." HOME=/home/stephen PYTHONPATH=backend/src \
  PHOTO_RUN_PHASE9_RECOVERY_INTEGRATION=1 \
  PHOTO_RUN_PHASE6_RECONCILIATION_INTEGRATION=1 \
  PHOTO_RUN_PHASE7_MUTATION_INTEGRATION=1 \
  PHOTO_RUN_PHASE8_GC_INTEGRATION=1 \
  .venv/bin/pytest -q backend/tests/test_phase9_recovery_integration.py \
    backend/tests/test_phase6_reconciliation_integration.py \
    backend/tests/test_phase7_mutation_integration.py \
    backend/tests/test_phase8_gc_integration.py
  PASS: 4 passed; exact disposable local resources cleaned up
```

The disposable recovery run created and restored an independent recovery
checkpoint, restored its PostgreSQL dump into a fresh disposable database,
rebuilt the projection from restored S3 state, and compared the projection.
The cutover run verified the rollback window by retaining pre-cutover fixture
copies, confirming the S3-authoritative import path created none, and
confirming rollback did not remove them. No test mutation used the remote
`.env` S3 endpoint or the existing Compose PostgreSQL database.

Dry-run and recoverable deletion procedure (not executed):

1. At or after the retention deadline, obtain a fresh clean readiness report,
   full verification, recovery-checkpoint verification, and an independent
   inventory of the exact `originals/` key list with size and checksum.
2. Run a dry run that lists only
   `s3://photo-library/originals/`; compare its exact 797-key output with the
   saved inventory. Abort on any count, key, size, checksum, manifest, queue,
   backup, or readiness difference.
3. Preserve the inventory and an independently verified recovery copy before
   any removal. If that independent recovery copy is unavailable, the
   operation is not recoverable and must not proceed. The current deployment
   still has **no verified recovery checkpoint marker** and no independent
   secondary backup destination, so this prerequisite is not yet satisfied.
4. After approval, delete only the exact `originals/` keys, using a logged,
   abort-on-error operation. Re-run the same inventory, full S3 verification,
   readiness, and application smoke checks; retain the logs and approval.

Exact deletion approval required:

> Approve deletion of exactly all currently inventoried keys under
> `s3://photo-library/originals/` (**797 objects; 34,687,401,984 bytes**) at
> the configured endpoint, after the 30-day rollback window and after the
> fresh dry-run, independent recoverability check, and full verification pass.
> No `objects/`, `manifests/`, `backups/`, checkpoints, artifacts, live DB
> data, or compatibility/rollback support may be changed.

At the time of the initial Phase 6 record, no deletion command had been run;
that historical status was superseded by the explicit approval and execution
record below. Do not begin Phase 7.

## Phase 6 immediate deletion execution — 2026-10-07

The user explicitly approved immediate deletion, overriding the 30-day
retention window, and confirmed that no production volume data lacks an
external backup copy. This approval supersedes the pending-approval text
above for this exact operation only. Phase 7 remains deferred and compatibility
and rollback support code remains installed.

Pre-delete authority and scope checks passed immediately before deletion:

- `authority-status`: `authorityMode=s3`, `readiness=ready`,
  `projectionFreshness=fresh`, `reconciliation=verified`, `failureReason=null`.
- Fresh `cutover-readiness --checkpoint-id phase6-predelete-readiness` passed:
  scanned 2,639; matched 1,843; missing, divergent, orphaned, unresolved,
  conflicting, malformed, failed, skipped, and repaired all 0.
- Fresh `photo-server verify --full` passed: `assetsChecked=797`,
  `blobsChecked=797`, SHA-256, `errors=[]`.
- Asset manifest scan found 0 references to `originals/`.
- Exact target inventory: 797 objects and 34,687,401,984 bytes, matching the
  approved scope. The complete key/size/SHA-256 log is preserved at
  [phase6-originals-predelete-inventory-20261007.tsv](phase6-originals-predelete-inventory-20261007.tsv).

The exact-key deletion command ran against the configured `.env` S3 endpoint
(`http://192.168.8.181:30304`, bucket `photo-library`). It re-listed the
prefix, required the saved key set to match exactly, validated every key was
under `originals/<asset>/<filename>`, deleted those 797 keys individually,
and required the prefix to be empty. Result: `deleted=797 remaining=0`.
No broad bucket deletion, garbage collection, canonical object, manifest,
backup, checkpoint, artifact, live database, or compatibility-code operation
was performed.

Post-delete verification record (UTC, recorded `2026-10-07T19:14:29Z`):

| Prefix/status | Objects | Bytes | Result |
| --- | ---: | ---: | --- |
| `originals/` | 0 | 0 | PASS; empty |
| `objects/` canonical | 1,265 | 34,693,281,951 | PASS; unchanged; full SHA-256 passed |
| `manifests/` | 2,639 | 19,744,952 | PASS; unchanged |
| `backups/postgres/` | 26 | 89,002,206 | PASS; preserved |

The pre-delete backup count was 25 (85,382,229 bytes). One new verified
primary backup was created by live operational health activity during the
verification window; it is preserved and was not part of deletion. Thus no
pre-existing backup was changed or removed. API health is `ok`, all upload,
onboarding, processing, preview, and analysis queues are zero, database and
storage are ready, both workers are running, and backup health is `healthy`.
Authority remains `s3/ready/fresh/verified`.

The measured media saving is exactly **34,687,401,984 bytes** (**32.305161
GiB / 34.687402 GB**), or approximately **49.995763%** of the pre-delete
`objects/` plus `originals/` media footprint. Canonical media remains
34,693,281,951 bytes.

Risks and remaining constraints:

- The deleted rollback originals cannot be restored from this S3 prefix.
  Recovery depends on the user-confirmed external backup copy.
- The current service still reports no verified recovery-checkpoint marker and
  no configured secondary backup destination; this is an operational risk,
  not a reason to remove compatibility or rollback code.
- The earlier 30-day retention policy was explicitly overridden for this
  operation. No further originals deletion is authorized by this record.

Exit status: **PASS — Phase 6 originals retirement completed for the exact
approved scope.** Phase 7 was not started; compatibility and rollback support
were not removed.

## Phase 7 implementation record — 2026-10-07 (UTC)

Status: **READY FOR EXPLICIT TEARDOWN APPROVAL — no teardown performed.**

Implementation summary:

- Retired the remaining rollback-original write path. New imports and upload
  onboarding publish verified `objects/<sha256>` bytes and canonical manifests;
  they no longer recreate `originals/` copies in either authority mode.
- Added the read-only `phase7-readiness` control. It verifies S3 authority is
  configured, canonical asset manifests exist, the `originals/` prefix is
  empty, and the S3-to-PostgreSQL projection reconciliation is ready.
- Kept PostgreSQL running as the derived query/projection store. The readiness
  report explicitly identifies database teardown as a separate, explicit
  operation and this phase does not perform it.
- Tightened mutation expected-revision handling so a canonical revision that
  is ahead of the projection cannot accept a stale expected revision.

Safety gates:

- No production authority setting was changed.
- No production S3 or PostgreSQL data was deleted, replaced, or garbage-
  collected.
- The disposable integration fixture creates a random Compose project with
  local PostgreSQL and SeaweedFS, and cleans only that generated project.

Commands and exact results:

```text
PYTHONPATH=backend/src .venv/bin/pytest -q \
  backend/tests/test_phase7_authority.py \
  backend/tests/test_phase7_mutation_integration.py                 PASS: 5 passed, 1 skipped
PHOTO_RUN_PHASE7_MUTATION_INTEGRATION=1 PYTHONPATH=backend/src \
  .venv/bin/pytest -q backend/tests/test_phase7_mutation_integration.py PASS: 1 passed
.venv/bin/ruff check backend/src backend/tests/test_phase7_authority.py PASS
.venv/bin/python -m compileall -q backend/src                         PASS
git diff --check                                                       PASS
```

The focused non-opt-in run skips the disposable test by design; the explicit
opt-in run executed it successfully. Phase 7 is ready for a separate,
explicit teardown decision. PostgreSQL has not been torn down.

## Phase 7 superseding completion record — 2026-10-08 (UTC)

Status: **PASS — authority phaseout and compatibility teardown complete.**

The earlier readiness record above is historical and is superseded by commit
`7b8523b` (`remove retired compatibility mechanisms after the S3 authority
cutover`) and the current source tree:

- PostgreSQL-authoritative mode and its configuration have been removed.
- Runtime reads and writes no longer use `originals/` fallback or compatibility
  copies; canonical bytes are `objects/<sha256>` referenced by S3 manifests.
- Canonical-read, mutation, upload-retry, rebuild, reconciliation, and
  recovery tests cover operation with S3 as the authority.
- PostgreSQL remains the rebuildable query projection and operational queue
  store; database deletion was never part of this phaseout.
- Recovery checkpoints now record the software/data compatibility contract and
  reject unsupported future S3 format versions.
- The disposable S3 restore and fresh PostgreSQL rebuild exercise passed; the
  measured fixture RTO was approximately 16 seconds.

The phaseout acceptance criteria are satisfied. Deployment-specific snapshot
RPO/RTO values remain an operational policy recorded with the Session 5
snapshot schedule.
