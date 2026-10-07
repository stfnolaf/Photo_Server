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

Deliverables:

- Dry-run export with counts, checksums, and unresolved-reference report.
- Idempotent backfill command.
- Verification that every referenced object exists and matches its checksum.
- Rebuild comparison against the source database.

Exit criteria: all supported current library state exists in S3 manifests and
a clean database rebuild produces an equivalent projection.

### Phase 6 — Reconciliation and derived-index maintenance

Add a recurring reconciler that detects manifests missing from PostgreSQL,
stale projections, orphaned writes, and tombstones not reflected in the index.
Make PostgreSQL loss/replacement a supported operational event.

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

Define when bytes may be physically removed. A live delete should immediately
hide an item from the library, but old object bytes and manifest revisions must
remain while retained recovery checkpoints reference them.

Deliverables:

- Reachability calculation from retained manifests/tombstones.
- Dry-run garbage collector with safety reports.
- Retention policy for deleted objects and old revisions.
- Tests proving no object needed by a retained checkpoint is deleted.

This phase must remain opt-in and dry-run-only until restore testing is complete.

### Phase 9 — Backup and recovery cutover

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
