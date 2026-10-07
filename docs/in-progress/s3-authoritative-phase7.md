# Phase 7 — S3-authoritative mutations (complete)

Phase 7 changes only the durable mutation path. PostgreSQL remains authoritative
for live API reads and continues to hold derived browse, album-membership, face,
queue, lease, and worker projections. The API does not infer live state from
S3 manifests and this phase does not perform an authority cutover.

For an asset, album, person, or supported face-assignment mutation the coordinator validates the expected parent
revision, builds the complete next snapshot, writes its immutable revision under
`manifests/{assets,albums,people}/<id>/<revision>.json`, reads it back, and verifies
its checksum before calling the existing PostgreSQL projection transaction.
Deletes additionally write `tombstones/{asset,album,person}/<id>/<revision>.json`.
Person rename, face move, and person merge publish complete person snapshots;
face IDs are membership owned by the person snapshot. Face-move-created person
IDs are deterministic from the operation ID so the S3 write can precede the
PostgreSQL projection.

An S3 failure leaves PostgreSQL unchanged. A projection failure after the
verified S3 commit leaves a durable revision ahead of PostgreSQL; retrying the
same operation ID or running reconciliation repairs the projection. Restore
writes a new live revision; it never removes a tombstone. Queues, leases,
heartbeats, and worker checkpoints are operational-only and are not manifests.

The versioned manifest schemas and examples are in
`docs/in-progress/s3-authoritative-schemas/` and
`docs/in-progress/s3-authoritative-examples/`. The disposable suite is opt-in with
`PHOTO_RUN_PHASE7_MUTATION_INTEGRATION=1`. It creates a random Compose project,
ports, database, bucket, and temporary directory; it never references the
repository Compose file or its volumes. Cleanup runs `down` only against that
generated Compose file and project name.

```text
.venv/bin/pytest -q backend/tests/test_phase7_authority.py
.venv/bin/pytest -q backend/tests/test_phase7_mutation_integration.py
.venv/bin/pytest -q backend/tests
.venv/bin/ruff check backend/src backend/tests
.venv/bin/python -m compileall -q backend/src
.venv/bin/python scripts/check_openapi.py --strict-coverage
```

The disposable suite writes its Compose file below pytest's temporary
directory, chooses a random Compose project, ports, database, bucket, and data
directory, and uses no repository Compose resources or named volumes. Cleanup
is automatic and scoped to the generated file:

```text
docker compose -f <pytest-temp>/phase7-compose.yaml \
  --project-name <photo-phase7-random> down -v --remove-orphans
```

Only run that command with the exact generated file and project name. No broad
Docker prune or repository-volume cleanup is part of Phase 7.
