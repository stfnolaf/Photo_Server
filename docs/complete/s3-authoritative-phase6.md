# Phase 6 — reconciliation and derived-index maintenance

Phase 6 adds `photo-server reconcile-s3` (`reconcile.py`) as a maintenance
scanner. PostgreSQL remains authoritative for live reads and writes. There is
no authority cutover in this phase. An explicit `--apply` run repairs missing
derived rows from validated immutable S3 records; divergent PostgreSQL rows are
reported and left untouched.

The scan is deterministic (sorted canonical keys), resumable through the
mutable `indexes/checkpoints/reconciliation-<id>.json` checkpoint, and safe to
repeat. `--dry-run` writes neither PostgreSQL nor the checkpoint. `--apply`
must be combined with an operator-selected checkpoint and is still not a live
write-path change. `--report-only` is an explicit no-repair mode.

The report has `complete`, `paused`, and `failed` statuses and exact counts for
scanned, matched, missing, divergent, orphaned, unresolved, conflicting,
malformed, skipped, repaired, and failed records. Corrupt manifests, checksum
or size mismatches, incomplete histories, immutable conflicts, and unresolved
asset/album/person/face/analysis-run/processing references cannot be reported
as successful completion. Queue, lease, heartbeat, upload, and worker tables
are not scanned or changed.

Implementation and contracts:

- [reconcile.py](../../backend/src/photo_server/reconcile.py)
- [Phase 2 manifest models and codec](../../backend/src/photo_server/manifests/)
- [Phase 4 strict scanner](../../backend/src/photo_server/rebuild.py)
- [report schema](s3-authoritative-schemas/reconciliation-report-v1.json)
- [report example](s3-authoritative-examples/reconciliation-report-v1.json)

Commands:

```text
photo-server reconcile-s3 --dry-run --checkpoint-id nightly
photo-server reconcile-s3 --report-only --checkpoint-id nightly
photo-server reconcile-s3 --apply --checkpoint-id nightly
```

Focused tests belong in `backend/tests/test_reconcile.py`; the disposable
PostgreSQL/S3 acceptance suite belongs in
`backend/tests/test_phase6_reconciliation_integration.py` and must be enabled
explicitly with `PHOTO_RUN_PHASE6_RECONCILIATION_INTEGRATION=1`. It owns only
random Compose resources and performs narrow cleanup of those exact resources.
Phase 6 is not complete until that full disposable suite passes, in addition
to the focused/backend/static/OpenAPI/frontend gates recorded in the project
README.
