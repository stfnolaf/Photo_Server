# Phase 8 — object retention and garbage-collection policy

Phase 8 ships an opt-in, dry-run-only planner: `photo-server garbage-collect`
(alias `gc-s3`). It never deletes an object, manifest, tombstone, checkpoint,
queue row, lease, heartbeat, worker row, or PostgreSQL projection. Destructive
garbage collection remains disabled until restore testing proves it safe.

## Retention policy

The defaults are 30 days for deleted assets/albums/people and tombstones, 90
days for historical manifest revisions, 30 days for unreferenced content and
processing objects, 2 days for temporary `incoming/` uploads, and 30 days for
recovery/reconciliation checkpoints. Retention is configurable per dry run.
Current revisions and any revision reachable from a retained tombstone remain
protected. Shared content-addressed objects remain protected while any root
references them. PostgreSQL live deletion only hides an entity; it does not
make its bytes reclaimable.

## Roots and safety

The planner scans sorted keys only in `manifests/`, `tombstones/`,
`objects/`, `indexes/checkpoints/`, `reconciliation/`, and `incoming/` in the
configured bucket. Roots include current and retained historical manifests,
retained tombstones, retained recovery checkpoints, album memberships,
person/face assignments, processing references, artifact result objects, and
their source objects. Malformed, unknown, missing, divergent, or conflicting
records fail closed: the result is non-success and its candidate list is
empty. S3 list/head/read errors, checkpoint corruption, and incomplete scans
have the same behavior.

The report includes deterministic candidates, category byte totals, retention
age and policy decision, referencing roots, unresolved references, warnings,
and the resumable `indexes/checkpoints/gc-<id>.json` scan checkpoint. Supplying
`--as-of` makes report comparisons explicit; the checkpoint also preserves the
evaluation instant across resume.
The versioned [report schema](s3-authoritative-schemas/garbage-collection-report-v1.json)
and [example](s3-authoritative-examples/garbage-collection-report-v1.json) are
part of the contract.

```text
photo-server garbage-collect --checkpoint-id nightly --as-of 2026-10-06T00:00:00Z
photo-server garbage-collect --no-resume --temporary-upload-days 7
```

The only mutable write is the operational scan checkpoint. It is not a
reachability root unless it is retained, and it is never used to alter durable
state. Interrupted scans resume after the recorded sorted key; repeated runs
are idempotent and deterministic for the same `as-of` and policy.

## Recovery and disposable integration

Rollback is simply to stop running the command; no canonical state changed.
Recovery uses the existing manifest/tombstone rebuild and reconciliation paths.
The dedicated disposable suite must generate a random Compose file, project,
ports, database, bucket, and temporary directory. Its `finally` block may
remove only that exact file/project/containers/database/bucket/directory; it
must not use production Compose resources, named volumes, or broad cleanup.

Phase 8 is not complete until focused tests, the complete backend/static and
frontend gates, and the disposable PostgreSQL/S3 reachability/retention suite
pass.
