# Phase 10 — S3 authority cutover

Phase 10 makes immutable S3 manifests and content-addressed objects the
canonical source for user-visible library state. PostgreSQL is a rebuildable
read/index projection and retains operational queues, leases, worker
heartbeats, preview-cache rows, and checkpoints.

## State machine

`postgres` is the compatibility default. A cutover is allowed only when the
deterministic readiness report is `ready`: every manifest is canonical, every
referenced object has the expected size and SHA-256, histories and references
are complete, and PostgreSQL projections are equivalent. Activation writes only
the mutable operational marker `indexes/authority/status.json`; it never
overwrites canonical state.

On projection failure after an immutable S3 commit, run reconciliation with
`apply`. On a failed readiness check or an emergency storage incident, rollback
or fallback records the reason and returns reads to the PostgreSQL projection.
Canonical revisions remain intact and garbage collection remains disabled.

## Commands and reports

The service exposes `cutover_readiness()`, `activate_s3_authority()`,
`reconcile_authority()`, `rollback_s3_authority()`, and `authority_status()`.
Readiness output is sorted and stable for automation. It reports missing,
divergent, orphaned, unresolved, conflicting, malformed, and failed records.

Rebuild uses `rebuild_from_s3` against a fresh database. Reconciliation and
rebuild intentionally do not select or modify queue, lease, worker,
heartbeat, preview-cache, or other operational rows.

## Safety invariants

- PostgreSQL-only canonical writes are not a cutover operation; mutations must
  publish and verify their immutable S3 revision first.
- Expected-parent-revision and operation-id checks remain mandatory.
- Tombstones are canonical records and are checked against deleted projections.
- Source manifests and content-addressed objects are never mutated or deleted
  by readiness, rebuild, rollback, or reconciliation.
