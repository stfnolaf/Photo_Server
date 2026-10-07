# Phase 1: canonical state contract

This document is the Phase 1 inventory and contract for the S3-authoritative
architecture described in
[`s3-authoritative-architecture-plan.md`](s3-authoritative-architecture-plan.md).
It describes the current PostgreSQL schema as of migration `006` and defines
what must survive loss of PostgreSQL.

The JSON Schemas in [`s3-authoritative-schemas/`](s3-authoritative-schemas/)
are normative. Examples are in
[`s3-authoritative-examples/`](s3-authoritative-examples/). Phase 1 does not
write these records or change runtime authority.

## Authority vocabulary

- **Canonical**: user-visible or recovery-critical state. It is represented by
  a complete, immutable, versioned S3 record. PostgreSQL may index it, but is
  not needed to reconstruct it.
- **Durable derived**: expensive or useful output that is persisted in S3 with
  provenance and can be re-created from canonical objects. It is not required
  to reconstruct the basic library, but a valid copy is preferred during a
  rebuild.
- **Derived**: a deterministic query/index/cache projection. It can be deleted
  and rebuilt from canonical records and durable-derived artifacts.
- **Operational**: leases, retries, queues, staging progress, and heartbeats.
  It is intentionally disposable.

## Canonical record families

| S3 record | Identity and revision | Purpose |
| --- | --- | --- |
| Asset manifest | `assetId`, positive integer `revision` | Imported blobs, extracted metadata snapshot, user state, capture/import identity, and processing references. |
| Album manifest | `albumId`, positive integer `revision` | Album name, description, ordered membership, and deletion state. |
| Tombstone | `entityType`, `entityId`, positive integer `revision` | Durable deletion barrier for assets, albums, and people. |
| Person manifest | `personId`, positive integer `revision` | User-visible display name and manual face-assignment state. Uses the album manifest shape's revision rules. |
| Object bytes | SHA-256 digest | Immutable originals, sidecars, and retained processing artifacts. |

Asset and album manifests are defined in JSON Schema. Person manifests are
intentionally deferred to the Phase 2 codec but use the same envelope and
revision rules as an album; this avoids inventing a second mutation protocol.

## Revision and commit rules

1. Revisions start at `1` and increase by exactly one. A revision includes
   `parentRevision: null` only for revision 1; later revisions name the exact
   immediately preceding revision.
2. An entity has one linear history. A writer must supply the expected parent
   revision. If it is not the latest valid revision, the operation returns a
   conflict and does not create a revision. Merging two branches is not
   implicit.
3. `operationId` is the idempotency key. Replaying the same operation with the
   same entity, parent, and canonical payload returns the original result. Reusing
   it with different inputs is an integrity error.
4. A writer creates a deterministic versioned key, writes the complete JSON
   body, verifies the stored byte count and SHA-256, and only then considers the
   revision committed. A latest pointer is advisory and never authoritative.
5. JSON is UTF-8, has no duplicate object keys, uses the schema's declared
   types, and is serialized canonically for checksums: sorted keys, compact
   separators, and no insignificant whitespace. Timestamps are RFC 3339 UTC.
6. Every referenced object includes its SHA-256, byte size, and immutable key.
   A manifest is invalid if a referenced object is missing, has a different
   checksum, or is referenced more than once under different identities.
7. Unknown schema versions are rejected. Unknown fields are rejected in the
   Phase 2 validator; this Phase 1 schema uses `additionalProperties: false`.
8. A malformed newest revision is an integrity failure, not permission to fall
   back silently to an older revision. Recovery reports the entity and stops
   projection for that entity.

## Deletes, restores, and retries

- A delete first commits a tombstone whose revision is greater than the last
  visible entity revision. The entity is hidden immediately in projections;
  physical bytes and old manifests remain subject to the later retention and
  garbage-collection policy.
- A restore creates a new entity revision with `deletedAt: null` and an
  expected parent equal to the tombstone revision. It never edits or removes
  the tombstone. The latest valid record wins by revision, and a stale restore
  conflicts.
- A repeated delete with the same `operationId` is idempotent. A delete with a
  stale parent conflicts even if the entity is already deleted.
- Concurrent album membership changes conflict on the album parent revision;
  membership is owned by the album manifest, not duplicated in asset records.
- Object upload retries are safe because object keys are content-addressed.
  Existing bytes are accepted only after a checksum and size match. A same-key
  mismatch is an integrity error.
- Queue loss may cause work to be repeated. Processing publication is guarded
  by `(assetId, processingType, inputSha256, implementationVersion)` and is
  therefore idempotent; queue status, attempts, errors, leases, and timestamps
  are not canonical.

## Processing-artifact decision

Originals, imported sidecars, and user edits are canonical. Processing results
are split as follows:

| Current data | Phase 1 classification | Recovery decision |
| --- | --- | --- |
| Extracted EXIF/XMP metadata used as the displayed metadata snapshot | Canonical asset state | Copy into each asset revision under `extractedMetadata`; re-extraction may produce a later revision only through an explicit metadata refresh. |
| Semantic analysis JSON, face boxes, and embeddings | Durable derived | Store an immutable artifact with source object hash, model/pipeline identity, and artifact checksum. Reindex if present; recompute if absent. |
| Image fingerprints and burst clusters | Derived | Recompute from canonical originals and versioned algorithms. A user-selected representative, if exposed as a mutation, belongs in a future canonical burst/person record. |
| Preview and thumbnail bytes/cache accounting | Derived | Recreate from originals. Never required for catalog recovery. |
| Search text and “current” analysis flags | Derived | Rebuild from asset manifests and valid artifacts. |

The processing artifact schema records enough provenance to prevent an artifact
from being applied to a different original. Its presence is durable, but its
absence does not make the library unrecoverable.

## PostgreSQL migration inventory

The following matrix accounts for every application table and column in the
current migrations. Indexes and foreign keys are projection mechanics, not
state of their own.

| Table / columns | Classification | Canonical destination or rebuild rule |
| --- | --- | --- |
| `library.library_id` | Canonical identity | Library configuration/checkpoint; repeated in every manifest. |
| `library.schema_version`, `state_authority` | Operational/compatibility | Migration metadata only; not library state. |
| `assets.id`, `state_revision`, `manifest`, `timeline_at`, `media_type`, `original_filename`, `sha256`, `deleted_at` | Canonical | Asset manifest envelope, `captureTime`, blob reference, and tombstone. `state_revision` is the manifest revision. |
| `assets.rating`, `favorite`, `search_text` | Rating/favorite canonical; search text derived | Asset manifest `userState`; rebuild search text. |
| `blobs.id`, `asset_id`, `role`, `object_key`, `original_filename`, `sha256`, `size_bytes`, `mime_type` | Canonical reference | Asset manifest `blobs`; bytes remain content-addressed objects. |
| `albums.id`, `state_revision`, `state`, `deleted_at` | Canonical | Album manifest and album tombstone. |
| `album_assets.album_id`, `asset_id`, `position` | Canonical relationship | Ordered `assetIds` in the owning album manifest. |
| `operations.id`, `request`, `result` | Operational idempotency receipt | Rebuildable from manifest `operationId` and payload; retained separately only as an optimization. |
| `jobs.asset_id`, `job_type`, `status`, `attempts`, `error`, `lease_until`, `force_full`, `queued_at` | Operational | Recreate queue entries from active asset manifests; do not export leases, attempts, errors, or timestamps as canonical state. |
| `upload_batches.id`, `status`, `created_at`, `updated_at`, `sealed_at`, `error`, `album_id`; `upload_files.id`, `batch_id`, `relative_path`, `original_filename`, `size_bytes`, `mime_type`, `staging_key`, `required`, `status`, `reason`, `sha256`, `asset_id`, `error`; `onboarding_jobs.id`, `batch_id`, `primary_file_id`, `sidecar_file_ids`, `status`, `attempts`, `lease_until`, `result`, `error`, `queued_at` | Operational ingestion workflow | Staging declarations and incomplete transfers may be reported/quarantined. Completed assets are represented by asset manifests and objects; queue rows are discarded on rebuild. |
| `analysis_runs.id`, `asset_id`, `analysis_type`, `model_name`, `model_version`, `pipeline_version`, `input_hash`, `object_key`, `result`, `searchable_text`, `is_current`, `semantic_origin`, `source_run_id`, `reuse_policy_version`, `similarity`, `created_at` | Durable derived | Processing artifact manifest plus immutable result object. `searchable_text`, current flags, and reuse bookkeeping are rebuilt/indexed. |
| `people.id`, `display_name`, `created_at` | Display name canonical; timestamp metadata | Person manifest (Phase 2 codec uses album envelope rules). |
| `faces.id`, `asset_id`, `analysis_run_id`, `person_id`, `face_index`, `bounding_box`, `confidence`, `embedding` | Detection durable derived; assignment canonical | Detection/embedding processing artifact; manual `personId` assignment is stored in a person manifest or canonical assignment record. |
| `image_fingerprints.asset_id`, `algorithm_version`, `phash`, `dhash`, `width`, `height`, `chroma_histogram`, `created_at` | Derived | Recompute from the referenced original and algorithm version. |
| `burst_clusters.id`, `representative_asset_id`, `policy_version`, `created_at`; `burst_members.cluster_id`, `asset_id` | Derived unless a user-selected representative is explicitly exposed | Recompute using policy version; any future user decision becomes a canonical versioned record. |
| `preview_cache.asset_id`, `preview_bytes`, `thumbnail_bytes`, `last_accessed_at`, `updated_at` | Derived cache | Recreate previews and byte accounting; no recovery dependency. |
| `schema_migrations.*` | Operational | Recreated by database bootstrap. |

## Projection invariant

A fresh PostgreSQL database is a projection of: the library identity, all
valid asset/album/person revisions, tombstones, canonical object bytes, and
available durable processing artifacts. Projection code must be deterministic:
for each entity it selects the highest valid revision after validating its
parent chain, applies tombstones as deletion barriers, then rebuilds joins,
search text, current-analysis flags, fingerprints, bursts, previews, and jobs.

It must report, rather than conceal: missing referenced objects, checksum
mismatches, duplicate revisions, parent gaps, conflicting operation IDs,
unsupported schema versions, and multiple valid heads. This invariant is the
Phase 1 exit criterion; implementation of the codec and rebuild is Phase 2 and
Phase 4 work respectively.
