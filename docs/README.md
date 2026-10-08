# Documentation status

## Complete

The S3-authoritative architecture, manifest contracts, backfill,
reconciliation, mutation, retention, recovery, authority cutover, and AI
analysis publication work are complete implementation records under
[`complete/`](complete/), including the Phase 10 cutover contract. The
remaining work is tracked in [`planned/`](planned/).

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
- session 5: S3 recovery verification and compatibility contract
- PostgreSQL authority phaseout and originals retirement
- AI analysis publication to the S3-authoritative plane

The AI-service plan's final face-scanner consolidation is a separate follow-up
in the sibling `face-scanner` repository. The preview plan's face-crop cache is
explicitly optional and was not required for the completed preview-cache work.
The rollout note documents the shipped `observe`/`on` controls; switching the
default to `on` remains an operational threshold decision.

## In progress

The `in-progress/` directory is currently empty. The completed phaseout record
is [`postgres-authority-phaseout.md`](complete/postgres-authority-phaseout.md).

## Planned

The `planned/` directory contains the next unstarted operational sessions and
the roadmap, including storage-capacity safety and later operational work.

## S3 authority cutover

The completed architecture, Phase 10 cutover contract, and final phaseout
record are archived under [`complete/`](complete/). The old
`PHOTO_AUTHORITY_MODE` compatibility switch has been removed; use the current
authority-status and recovery controls when operating or rebuilding the
library.
