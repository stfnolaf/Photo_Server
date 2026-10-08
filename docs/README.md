# Documentation status

## Complete

The S3-authoritative architecture, manifest contracts, backfill,
reconciliation, mutation, retention, recovery, and authority-cutover work are
complete implementation records under [`complete/`](complete/), including the
Phase 10 cutover contract. The remaining work is tracked separately in the
PostgreSQL-authority phaseout plan below.

The S3-authoritative recovery checkpoint contracts and implementation are
documented in [`complete/s3-authoritative-phase9.md`](complete/s3-authoritative-phase9.md)
and the accompanying recovery checkpoint schemas. Checkpoint creation is
resumable, immutable at completion, and independent of the production S3
destination.

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

The remaining work is documented in
[`postgres-authority-phaseout.md`](in-progress/postgres-authority-phaseout.md).
It covers migrating all runtime reads and writes to canonical S3 objects,
running S3-authoritative mode, rebuilding PostgreSQL from S3, and finally
removing the duplicate `originals/` photo objects.

## Planned

The `planned/` directory contains the next unstarted operational sessions and
the roadmap, including restore verification and storage-capacity safety.

## S3 authority cutover

The completed architecture and Phase 10 cutover contract are archived under
[`complete/`](complete/). Use the deterministic cutover readiness report before
setting `PHOTO_AUTHORITY_MODE=s3`; the active storage-deduplication and
compatibility-removal plan is
[`postgres-authority-phaseout.md`](in-progress/postgres-authority-phaseout.md).
