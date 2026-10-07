# Documentation status

## Complete

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

There are currently no local implementation documents in this bucket.

## Planned

The `planned/` directory contains the next unstarted operational sessions and
the roadmap, including restore verification and storage-capacity safety.

## Current architecture work

[`s3-authoritative-architecture-plan.md`](s3-authoritative-architecture-plan.md)
stays at the top level as the current architecture pivot and primary active
design document.
