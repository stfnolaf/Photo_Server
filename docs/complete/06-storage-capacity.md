# Session 6: Storage accounting, retention, and capacity safety

Status: Complete.

Work in `/home/stephen/dev/photo_server`.

Implement read-only storage accounting and capacity warnings across the
canonical S3 store and local derived storage. Keep destructive cleanup
explicit, narrowly scoped, and separately approved.

## Context

S3 is authoritative. Photo bytes live in immutable content-addressed
`objects/<sha256>` entries and are referenced by immutable manifests. The
remaining S3 namespaces include manifests, processing artifacts, upload
staging, recovery checkpoints, and PostgreSQL backups. The old
`originals/<asset-id>/...` copies have been retired and must not be treated as
the canonical photo store.

The project already has several bounded cleanup or safety mechanisms:

- `photo-server verify --full` checks canonical object bytes;
- `photo-server garbage-collect` produces a fail-closed retention report and
  does not delete objects;
- abandoned upload batches clean up their own staging objects after the lease
  window;
- the local preview cache evicts derived files by byte budget;
- recovery checkpoints and offline/ZFS copies are separate recovery concerns.

The missing capability is a unified view of storage growth, reclaimable
derived data, capacity pressure, and retention risk.

## Requirements

- Add a read-only command such as `photo-server storage report` with human and
  JSON output.
- Report S3 usage by namespace, at minimum:
  - canonical `objects/` bytes and object count;
  - manifest bytes and revision counts;
  - processing and AI artifact bytes, split into current and historical where
    possible;
  - active and stale upload staging;
  - recovery checkpoints and progress records;
  - PostgreSQL backup bytes, age, and retention status;
  - unknown or unclassified prefixes.
- Report canonical integrity and reference health:
  - missing manifests or referenced canonical objects;
  - malformed or unsupported manifest versions;
  - divergent or orphaned records from reconciliation;
  - objects that are candidates for retention review, with the referencing
    manifest/revision and retention reason.
- Report local derived storage separately:
  - PostgreSQL volume usage where available;
  - preview-cache rows versus files and accounted bytes;
  - orphaned preview directories;
  - temporary import or processing directories;
  - filesystem free space and configured cache limits.
- Make capacity thresholds configurable for local free space, preview cache,
  staging growth, backup age, and any storage endpoint quota that can be
  queried reliably.
- Surface warning/degraded status in health and structured logs without
  exposing credentials or full object paths unnecessarily.
- Reject or pause new uploads only after a clearly documented hard threshold
  is crossed; warnings must remain non-blocking.
- Keep reports bounded. Do not download every canonical object unless the
  operator explicitly requests full SHA-256 verification.

## Retention and safety requirements

- Treat `objects/` and manifests as canonical. Never classify them as
  reclaimable solely because they are old or absent from PostgreSQL.
- Treat previews, rebuild checkpoints, progress markers, abandoned staging,
  and historical processing artifacts according to explicit retention rules.
- Never delete from a live or offline backup snapshot as part of routine
  cleanup.
- Keep the first implementation read-only, including all candidate reports.
- Every candidate must identify its namespace, key, size, age, reference
  status, retention rule, and recovery impact.
- Active upload leases and in-progress recovery/rebuild checkpoints must never
  be reported as stale.
- Any future deletion command must require an explicit scope, dry-run output,
  confirmation, and a durable execution report.

## Verification

- Add fixtures for missing canonical objects, malformed manifests, unsupported
  schema versions, stale staging, orphaned processing artifacts, checkpoint
  retention, backup retention, and preview-cache inconsistencies.
- Test namespace accounting and stable JSON output.
- Test warning and hard-threshold behavior without deleting data.
- Test fail-closed behavior when S3, PostgreSQL, or local filesystem metrics
  are unavailable.
- Confirm reports do not confuse a rebuildable PostgreSQL projection or local
  preview cache with canonical library state.
- Run backend tests, lint, and operational health checks.

## Stop condition

End with a trusted report-only tool, documented namespace ownership, explicit
retention rules, and measured capacity thresholds. A later, separately
approved session may add narrowly scoped cleanup after reports have been
reviewed against the recovery and compatibility contract in Session 5.
