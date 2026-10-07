# S3-authoritative library architecture plan

## Goal

Make S3 the source of truth for photo objects and user-visible library state.
PostgreSQL becomes a rebuildable retrieval index and operational work queue.
Loss of PostgreSQL must therefore cause rebuild time, not loss of catalog or
user metadata.

This is a migration from the current design, where PostgreSQL is authoritative
for asset records, metadata, albums, tombstones, and queue state.

## Design decisions

- S3 objects remain content-addressed and immutable once written. This applies
  to canonical records and backup copies, not to the user's live ability to
  delete a photo.
- User-visible mutations create new versioned manifest objects. They do not
  overwrite an existing manifest.
- Deletion is represented by a tombstone before physical garbage collection.
- PostgreSQL indexes the latest valid S3 manifests. It is not required for
  reconstructing the library.
- Queues, leases, and worker heartbeats are operational state. They may be
  recreated after a rebuild; durable user/library state must be represented in
  S3 manifests.
- Every manifest carries schema version, entity ID, revision, parent revision,
  timestamps, object references, and referenced-object SHA-256 values.
- A manifest is considered committed only after its complete body is present
  and its checksum can be verified. Pointer/index updates happen afterward.
- No phase removes existing PostgreSQL authority or deletes live/original
  objects until a later cutover phase has passed its rebuild and recovery tests.

## Proposed S3 layout

```text
objects/<sha256>                         immutable photo/sidecar bytes
manifests/assets/<asset-id>/<revision>.json
manifests/albums/<album-id>/<revision>.json
tombstones/<entity-type>/<entity-id>/<revision>.json
indexes/checkpoints/<checkpoint-id>.json
```

The exact key layout may change during Phase 1. Mutable “latest” pointers are
optional conveniences; versioned manifests and deterministic revision
selection remain canonical.

## Session phases

### Phase 1 — Inventory and canonical state contract

Phase 1 is documented in [`s3-authoritative-phase1.md`](s3-authoritative-phase1.md),
with normative schemas in [`s3-authoritative-schemas/`](s3-authoritative-schemas/)
and examples in [`s3-authoritative-examples/`](s3-authoritative-examples/).

Status: contract delivered; runtime codec and validation remain Phase 2.

Document every field currently stored in PostgreSQL, classify it as canonical,
derived, or operational, and define the manifest schemas and revision rules.
Include assets, blobs, extracted metadata, user metadata, albums, tombstones,
processing results, and object checksums.

Deliverables:

- Versioned JSON schemas and example manifests.
- Explicit rules for concurrent edits, retries, idempotency, and deletes.
- A migration matrix from current PostgreSQL rows to manifest fields.
- Decision on which processing artifacts are canonical versus reproducible.

Exit criteria: a fresh PostgreSQL database can be described entirely as a
derived projection of the documented canonical records.

### Phase 2 — Manifest codec and validation library

Implement pure code for encoding, decoding, validating, canonicalizing, and
hashing manifests. Add strict schema-version handling and reject malformed or
ambiguous records.

Status: delivered. The isolated [`photo_server.manifests`](../../backend/src/photo_server/manifests/)
package provides immutable Python models and a standard-library-only codec for
asset, album, tombstone, and processing-artifact records. It enforces version
and field strictness, duplicate-key rejection, canonical UTF-8 JSON, SHA-256
hashing, object/reference rules, and linear revision ancestry without any
PostgreSQL, S3, network, or filesystem dependency. Focused coverage is in
[`test_manifest_codec.py`](../../backend/tests/test_manifest_codec.py).

Deliverables:

- Python manifest models and canonical JSON serialization.
- Revision and parent-revision validation.
- Object-reference and SHA-256 validation.
- Unit tests for valid, invalid, duplicate, stale, and future-schema records.

Exit criteria: manifests round-trip byte-stably and all validation logic works
without PostgreSQL or a live S3 service.

### Phase 3 — Safe dual-write for new imports

For new imports, write the immutable photo object and canonical asset manifest
to S3 while retaining the existing PostgreSQL write path. Add an idempotency
key so retries cannot create conflicting revisions.

Status: implemented for new imports. [`dual_write.py`](../../backend/src/photo_server/dual_write.py)
publishes and verifies content-addressed objects and Phase 2 asset manifests;
[`service.py`](../../backend/src/photo_server/service.py) keeps the existing
PostgreSQL import projection and repairs either side on an idempotent retry.
Focused coverage is in
[`test_dual_write.py`](../../backend/tests/test_dual_write.py), alongside the
codec tests linked in Phase 2. Immutable reconciliation events are recorded
under `reconciliation/dual-writes/<operationId>/` for incomplete writes.

Do not change reads or existing mutation behavior yet.

Deliverables:

- Dual-write import path.
- Failure recovery for object-written/DB-failed and DB-written/manifest-failed
  cases.
- Metrics and reconciliation records for incomplete dual writes.
- Integration tests proving repeated imports are idempotent.

Exit criteria: every newly imported asset has a valid S3 manifest, and current
API behavior remains unchanged.

### Phase 4 — Rebuild-from-S3 command

Implement an offline or maintenance-mode command that scans manifests and
tombstones, resolves the latest valid revision per entity, and rebuilds an
empty PostgreSQL database.

Status: implemented. [`photo-server rebuild-from-s3`](../../backend/src/photo_server/cli.py)
uses [`rebuild.py`](../../backend/src/photo_server/rebuild.py) to scan the
canonical namespace in sorted key order, validate every record with the Phase 2
codec, verify manifest and referenced-object checksums/sizes, validate linear
ancestry, and project the existing PostgreSQL read model. It writes resumable
scan checkpoints under `indexes/checkpoints/` and emits separate reports for
malformed records, missing objects, checksum failures, parent gaps, operation
conflicts, duplicate revisions, and multiple valid heads. The
[`compare_projections`](../../backend/src/photo_server/rebuild.py) helper
compares rebuilt and existing asset/album projections while ignoring queues and
other operational state. Focused coverage is in
[`test_rebuild.py`](../../backend/tests/test_rebuild.py).

Deliverables:

- Fresh-database schema/projection builder.
- Deterministic ordering and resumable checkpoints.
- Conflict and malformed-manifest report.
- Comparison tool between rebuilt and existing PostgreSQL projections.

Exit criteria: a disposable database rebuilt from dual-written fixtures matches
the existing projection for assets, metadata, albums, and deletions.

### Phase 5 — Backfill existing PostgreSQL state

Export current authoritative PostgreSQL rows into canonical manifests and
tombstones. Write them to a new S3 namespace without changing live reads.

Status: complete after the disposable people/faces/processing-artifact
acceptance passed. PostgreSQL remains authoritative; this phase performs no
authority cutover.
[`photo_server.backfill`](../../backend/src/photo_server/backfill.py)
provides `photo-server backfill-to-s3` (also available as `backfill-s3`). It
deterministically exports assets, albums, people, face rows, deletion
tombstones, and current durable processing references; copies or verifies immutable bytes at
`objects/<sha256>`; validates every generated record through the Phase 2
codec; refuses immutable-key conflicts; and writes resumable checkpoints under
`indexes/checkpoints/`. `--dry-run` performs source and existing-target
verification without S3 mutations. The command reports exported, skipped,
unresolved, conflicting, malformed, and failed records. Repeated runs are
idempotent and do not perform authority cutover.

The Phase 4 [`rebuild-from-s3`](../../backend/src/photo_server/rebuild.py)
scanner consumes the generated asset, album, person, face, processing-artifact,
and tombstone namespace;
its [`compare_projections`](../../backend/src/photo_server/rebuild.py) helper
compares assets, albums, people, display names, and face assignments for
source-versus-rebuild verification. Checkpoints are mutable operational
metadata; manifests and object bytes remain immutable.

Verification completed so far:

- Focused: `.venv/bin/pytest -q backend/tests/test_manifest_codec.py backend/tests/test_backfill.py backend/tests/test_rebuild.py`.
- Complete backend: `.venv/bin/pytest -q backend/tests`.
- Isolated acceptance: `PHOTO_RUN_PHASE5_INTEGRATION=1 .venv/bin/pytest -q
  backend/tests/test_phase5_integration.py`; it starts only random PostgreSQL
  and SeaweedFS S3-compatible resources and cleans only those named resources.
- Disposable lifecycle smoke: random PostgreSQL and S3-compatible containers,
  random database/bucket, dry-run, real export, interruption/resume, repeat,
  fresh rebuild, and projection comparison. The containers, databases, bucket,
  and temporary files were explicitly removed after the run. The full
  people/faces/processing-artifact fixture covers dry-run mutation
  protection, interruption/resume, repeat execution, object and manifest
  corruption, missing processing manifests, immutable conflicts, fresh-database
  rebuild, display names, canonical face assignments, tombstones, albums, and
  metadata. Focused coverage is in
  [`test_backfill.py`](../../backend/tests/test_backfill.py),
  [`test_rebuild.py`](../../backend/tests/test_rebuild.py), and
  [`test_manifest_codec.py`](../../backend/tests/test_manifest_codec.py).
- Static gates: `.venv/bin/ruff check backend/src backend/tests`,
  `.venv/bin/python -m compileall -q backend/src`,
  `.venv/bin/python scripts/check_openapi.py`, and `git diff --check`.
- Frontend gates: `npm run check`, `npm test -- --run`, and `npm run build` in
  `frontend/web`.

Operational cleanup is deliberately narrow: remove only the named disposable
containers, the random `phase5-*` bucket, and the random `phase5_*` database
created by the integration command. Do not run broad Docker or bucket cleanup,
and do not touch the configured production Compose resources.

Deliverables:

- Dry-run export with counts, checksums, and unresolved-reference report.
- Idempotent backfill command.
- Verification that every referenced object exists and matches its checksum.
- Rebuild comparison against the source database.

Implementation and contract references: [`backfill.py`](../../backend/src/photo_server/backfill.py),
[`rebuild.py`](../../backend/src/photo_server/rebuild.py),
[`manifests/models.py`](../../backend/src/photo_server/manifests/models.py),
[`manifests/codec.py`](../../backend/src/photo_server/manifests/codec.py),
[`db_migrations/001_current_schema.sql`](../../backend/src/photo_server/db_migrations/001_current_schema.sql),
and the examples under
[`s3-authoritative-examples`](./s3-authoritative-examples/). Run
`photo-server backfill-to-s3 --dry-run`, then
`photo-server backfill-to-s3`, and use
`photo-server compare-s3 <disposable-database-url>` for the narrow operational
workflow. Cleanup is limited to the exact random Compose project, bucket,
databases, and temporary directory emitted by the isolated test.

Exit criteria: all supported current library state exists in S3 manifests and
a clean database rebuild produces an equivalent projection.

### Phase 6 — Reconciliation and derived-index maintenance

Implemented as the explicit maintenance command
[`photo-server reconcile-s3`](s3-authoritative-phase6.md). It detects manifests
missing from PostgreSQL, PostgreSQL rows missing from S3, divergent asset and
user state, albums and ordered memberships, tombstones, people, display names,
faces, assignments, analysis runs, processing references, orphaned immutable
objects/manifests, malformed/corrupt records, checksum/size mismatches, and
unresolved references. PostgreSQL remains authoritative for live reads and
writes; Phase 6 performs no authority cutover.

Status: implementation and focused coverage are in progress; the dedicated
disposable reconciliation acceptance must pass before this phase can be marked
complete.

Deliverables:

- S3-to-PostgreSQL reconciliation job.
- Checkpointing, retry, bounded scans, and alertable failure states.
- Health/status reporting for manifest lag and rebuild status.
- Tests for interrupted reconciliation and repeated execution.

Exit criteria: deleting/recreating the database and running reconciliation
restores the searchable projection without using the old database.

### Phase 7 — S3-authoritative mutations and reads

Move metadata edits, album changes, and deletes to the manifest-first path:
write and verify the next S3 revision, then update PostgreSQL. Reads continue
to use PostgreSQL for performance, with reconciliation as the repair path.

Use optimistic concurrency based on expected parent revision. Return a conflict
instead of silently overwriting a newer manifest.

Deliverables:

- Manifest-first mutation service.
- Tombstone creation and resurrection rules.
- API conflict responses and retry semantics.
- Compatibility tests for old clients and replayed operations.

Exit criteria: all user-visible mutations are recoverable from S3 alone and
PostgreSQL is demonstrably a projection.

### Phase 8 — Object retention and garbage collection policy

Phase 8 is implemented as the opt-in `photo-server garbage-collect` dry-run
planner. A live delete immediately hides an item from PostgreSQL live reads,
but old object bytes and manifest revisions remain while retained tombstones,
history, or recovery checkpoints can restore/rebuild them. The planner scans
only the configured bucket prefixes, validates all durable roots, and fails
closed on any uncertainty. It never deletes canonical objects or manifests.

Deliverables:

- Reachability calculation from retained manifests/tombstones.
- Dry-run garbage collector with safety reports.
- Retention policy for deleted objects and old revisions.
- Tests proving no object needed by a retained checkpoint is deleted.

The dedicated disposable PostgreSQL/S3 reachability suite is the exit gate.
Destructive mode is deliberately absent and remains disabled until restore
testing is complete.

This phase must remain opt-in and dry-run-only until restore testing is complete.

### Phase 9 — Backup and recovery cutover

Status: complete. `photo_server.recovery` scans and strictly validates all
canonical manifest kinds, verifies referenced objects, copies immutable bytes
to an independent destination with post-copy size/SHA-256 verification,
optionally retains a verified PostgreSQL derived-index dump, supports verified
dump restore, and rolls back partial destination copies. The disposable
PostgreSQL/SeaweedFS acceptance passes. See
[`s3-authoritative-phase9.md`](s3-authoritative-phase9.md) for resume, rollback,
restore, and failure semantics.

Change backup health from “PostgreSQL dump replicated” to a complete checkpoint
consisting of:

1. PostgreSQL dump of the derived index, if operationally useful.
2. A manifest checkpoint listing the canonical revisions.
3. Verified copies of any canonical S3 objects not already present in the
   independent backup destination.

Use content-addressed deduplication so unchanged photos are copied once.

Deliverables:

- Checkpoint creation and per-object checksum verification.
- Independent-destination health reporting.
- Restore/rebuild verification into disposable PostgreSQL and S3 namespaces.
- Documented recovery procedure and failure semantics.

Exit criteria: a selected checkpoint can rebuild a working library projection
without the original PostgreSQL instance or primary S3 service.

### Phase 10 — Authority cutover and deprecation

After multiple successful rebuild and recovery exercises, remove PostgreSQL
authority from application code. Keep compatibility readers and migration
telemetry for one release, then remove obsolete write paths.

Deliverables:

- Feature flag and rollback switch during rollout.
- Updated architecture and operations documentation.
- Removal of authority-only database code.
- Final API/OpenAPI, backup, reconciliation, and garbage-collection tests.

Exit criteria: PostgreSQL can be dropped and rebuilt from S3 in a documented
exercise, and no supported user-visible state exists only in PostgreSQL.

## NAS and PostgreSQL placement

This pivot does not require moving PostgreSQL onto the NAS. Until the S3
authority migration is complete, keep PostgreSQL on a durable host or give it
independent backups. If PostgreSQL remains authoritative, a NAS-hosted primary
database may reduce VM-loss risk, but the data directory should use local
storage on the NAS host rather than an NFS-mounted PostgreSQL directory.

After the pivot, PostgreSQL may remain on the expendable VM because it is a
rebuildable index. The NAS/S3 system still needs independent backup protection
for canonical manifests and object bytes.

## Deferred work

- Automatic restore verification is not part of the current backup session.
- Destructive garbage collection is not part of the current backup session.
- WAL archiving and sub-hour database recovery points remain separate concerns.
## Phase 10 cutover

Phase 10 completes the authority transition. Immutable S3 manifests, objects,
processing artifacts, face assignments, and tombstones are canonical. The
Phase 10 control plane verifies the complete namespace and PostgreSQL
projection equivalence before activation, persists explicit authority status,
and supports reconciliation plus emergency rollback without changing source
canonical objects. See `s3-authoritative-phase10.md` and the authority/readiness
schemas for the stable report shapes.
