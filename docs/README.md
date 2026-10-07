# Documentation status

## Complete

Phase 9 recovery checkpoint contracts and implementation are documented in
[`in-progress/s3-authoritative-phase9.md`](in-progress/s3-authoritative-phase9.md)
and the recovery checkpoint schemas. Checkpoint creation is resumable,
immutable at completion, and independent of the production S3 destination.

The `complete/` directory contains delivered behavior, implementation records,
and completed session briefs:

- authentication and request protection
- PostgreSQL authority and backup behavior
- database/API flattening
- OpenAPI client-contract work
- optional/swappable AI services
- burst semantic reuse and clustering
- preview-cache eviction
- semantic-reuse rollout policy and observability
- sessions 1–4: health/logging, metrics, authentication, and independent backups

The AI-service plan's final face-scanner consolidation is a separate follow-up
in the sibling `face-scanner` repository. The preview plan's face-crop cache is
explicitly optional and was not required for the completed preview-cache work.
The rollout note documents the shipped `observe`/`on` controls; switching the
default to `on` remains an operational threshold decision.

## In progress

The S3-authoritative Phase 1 contract is documented in
[`s3-authoritative-phase1.md`](in-progress/s3-authoritative-phase1.md), with
versioned schemas and examples alongside it.

Phase 6 reconciliation is documented in
[`s3-authoritative-phase6.md`](in-progress/s3-authoritative-phase6.md). It is
maintenance-only: PostgreSQL remains authoritative for live reads and writes,
and no authority cutover occurs. The disposable reconciliation integration
suite is the required Phase 6 exit gate.

Phase 7 S3-first mutations are documented in
[`s3-authoritative-phase7.md`](in-progress/s3-authoritative-phase7.md).
Phase 7 is complete: the mutation integration suite is the required exit gate;
PostgreSQL remains authoritative for live reads and S3 is authoritative for
immutable mutation history and recovery.

Phase 8 object retention and dry-run garbage-collection policy is documented in
[`s3-authoritative-phase8.md`](in-progress/s3-authoritative-phase8.md). It is
opt-in and fail-closed; destructive deletion is disabled until restore testing
is complete.

## Planned

The `planned/` directory contains the next unstarted operational sessions and
the roadmap, including restore verification and storage-capacity safety.

## Current architecture work

[`s3-authoritative-architecture-plan.md`](s3-authoritative-architecture-plan.md)
stays at the top level as the current architecture pivot and primary active
design document.
# S3 authority cutover

Phase 10 is documented in [s3-authoritative-phase10.md](in-progress/s3-authoritative-phase10.md).
Use the deterministic cutover readiness report before setting
`PHOTO_AUTHORITY_MODE=s3`; PostgreSQL remains a rebuildable projection and
operational queue store.
