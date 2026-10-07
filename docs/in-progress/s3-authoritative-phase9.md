# Phase 9 — backup and recovery cutover

`photo-server create-recovery-checkpoint --checkpoint-id <id>` scans and
strictly decodes every canonical asset, album, person, face, processing, and
tombstone manifest. It verifies every referenced content-addressed object,
copies each manifest/object once into the independent destination, and writes
the immutable metadata record below `indexes/recovery-checkpoints/`. A retained
PostgreSQL custom-format dump is optional; it is a derived-index convenience,
never the recovery authority.

The only mutable record is `indexes/checkpoints/recovery-<id>.json`, which
preserves the checkpoint timestamp for resume. Source S3 is read-only. A
repeated completed invocation reads the immutable metadata and returns the same
canonical report. Missing, malformed, conflicting, divergent, or failed reads
and writes return `failed` and never produce a successful checkpoint.

Use `verify-recovery-checkpoint` with the independent checkpoint endpoint and
bucket before restore, then use `restore-recovery-checkpoint` into a fresh
bucket. Supplying `--database-url` also streams the verified custom-format
PostgreSQL dump through `pg_restore` into a fresh derived-index database.
Restore canonical state and the dump independently, then run
`rebuild-from-s3` to prove S3 projection equivalence. An interrupted create is
resumed using the same ID. An interrupted restore is safe to retry because
destination writes are immutable and idempotent. If a new destination write
fails, restore removes only the exact objects created during that attempt;
pre-existing destination objects and the source checkpoint remain unchanged.

Garbage collection remains dry-run-only and treats every recovery checkpoint
as a protected root. Queue, lease, worker, heartbeat, and operational
checkpoint rows are not included in canonical recovery state and are never
modified by checkpoint creation or restore.
