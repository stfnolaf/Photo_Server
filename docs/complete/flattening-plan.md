# Application and PostgreSQL flattening plan

## Goal

Reset the test-era deployment onto one current application contract and one
current PostgreSQL schema. The reset is safe because the authoritative media
and sidecar objects have independent copies outside the live database.

The result should have:

- one supported API contract and one generated frontend client;
- one current manifest/document shape, with no legacy compatibility variants;
- one PostgreSQL baseline schema instead of the historical migration chain;
- burst clustering on `burst-cluster-v2` from the first imported asset;
- a lightweight fingerprint/backfill path that does not require face or VLM AI;
- a repeatable rebuild from the external S3 copy.

This plan does not rewrite Git history. It resets deployed state and simplifies
the source tree while retaining the historical commits for audit and recovery.

## Non-goals

- Do not delete the external S3 copy until the rebuilt library has been
  validated end to end.
- Do not preserve database UUIDs, operation history, queue attempts, previews,
  analysis runs, or burst representatives unless there is a demonstrated need.
- Do not retain compatibility code merely because it existed in the test-era
  database. Anything retained must have a current consumer or a clear recovery
  purpose.

## Target state

### Storage and authority

PostgreSQL remains the authority for the current catalog, metadata, user state,
albums, processing state, fingerprints, bursts, and AI results. S3 remains the
authority for immutable originals, sidecars, generated analysis artifacts, and
backups. The reset starts with an empty database and repopulates it through the
normal import/processing paths.

The rebuilt database should contain only current schema objects. The migration
runner should apply a bootstrap tracking table and one baseline schema, rather
than replaying migrations `001` through `010` or adopting legacy schema
versions.

### API and models

There should be one supported wire shape for each endpoint. Remove obsolete
version branches, legacy response variants, and compatibility parsing after
confirming that the web client and any operational scripts use the current
contract. `openapi/openapi.json` remains generated from the backend and the
frontend generated client is regenerated from that exact artifact.

The current manifest shape becomes the only accepted shape. Legacy
`schema_version` handling and migration-only document forms can be removed or
reduced to a single constant. Existing `asset.migrate` behavior should be
removed if it exists only to support pre-reset data.

### Burst and analysis

The baseline includes fingerprint and burst tables using `burst-cluster-v2`.
Burst context uses perceptual similarity plus capture time, camera identity,
filename sequence, import time, and optional shutter count as implemented in
`bursts.py`. A fresh import must not require VLM or face-service availability
just to establish fingerprints and burst membership.

## Execution phases

### Phase 0 — Freeze and inventory

1. Stop imports, metadata refreshes, workers, AI workers, and scheduled backups
   that target the old database.
2. Record the deployed application commit, environment configuration, database
   name, S3 endpoint/bucket, and object counts.
3. Create one final PostgreSQL dump and retain it with the external S3 copy,
   even though it is not the target source of truth.
4. Produce an S3 inventory of originals and sidecars, including object key,
   size, checksum/ETag where meaningful, media type, and original filename.
5. Verify that the external copy contains every original and sidecar required
   for reimport. Record missing, duplicate, and unreadable objects before any
   destructive operation.

Exit criterion: the external source can be read independently of the live
PostgreSQL instance, and the final dump can be restored into a disposable
database.

### Phase 1 — Define the current contract

1. Mark the current backend models, API schemas, SQL tables, and generated
   TypeScript client as the starting point for the target contract.
2. Remove fields whose only purpose is migration compatibility, such as
   legacy document variants, if no current endpoint or worker consumes them.
3. Decide which operational records survive a reset. Default: none, except
   current user-facing metadata that is explicitly exported first.
4. Freeze the burst policy identifier as `burst-cluster-v2` and document the
   threshold/evidence constants as part of the target contract.
5. Add a reset marker/version to health or operator output so it is obvious
   which deployment is serving the flattened state.

Exit criterion: the target models and endpoint list are written down, with no
remaining requirement to read an old database row.

### Phase 2 — Replace migrations with a baseline

Create a fresh migration set rather than editing applied historical migrations.
The recommended layout is:

- `000_migration_tracking.sql` — minimal migration bookkeeping;
- `001_current_schema.sql` — the complete target schema, including catalog,
  browsing, user state, uploads, jobs, AI analysis, fingerprints, bursts, and
  preview-cache tables.

Update the migration runner so a new database only needs the baseline. Remove
legacy adoption and checksum logic that exists solely to carry old databases,
unless it remains useful for validating a fresh deployment. Keep an explicit
failure when a database is not empty or does not match the reset procedure;
silent destructive initialization is not acceptable.

The baseline must include all current constraints, indexes, foreign keys,
defaults, JSONB columns, and unique keys. Generate it from the current table
declarations only after comparing those declarations against the SQL actually
used by the application.

Update migration tests to verify:

- a blank database reaches the target schema in one application;
- a second initialization is idempotent;
- required tables, indexes, constraints, and defaults exist;
- no legacy migration version is required by startup;
- the application refuses an unexpected non-empty database during reset.

### Phase 3 — Remove compatibility code

Delete or simplify code that only supports the discarded state:

- legacy manifest schema parsing and validators;
- migration adoption and historical schema-version branches;
- old API response unions and version-specific serializers;
- compatibility-only operations and repair paths;
- tests whose only purpose is old-schema continuity;
- stale rollout flags and comments describing completed transitions.

Keep normal retry, idempotency, lease, deletion, restore, and backup behavior;
those are current operational guarantees rather than migration compatibility.

Run the backend type/schema tests after each removal so a deleted branch does
not silently alter the current wire contract.

### Phase 4 — Regenerate the API contract and client

1. Start the backend against a disposable database created from the baseline.
2. Regenerate `openapi/openapi.json` using the repository script.
3. Run the strict OpenAPI coverage check.
4. Regenerate `frontend/web/src/api/generated` with the pinned generator.
5. Remove hand-written contract types that duplicate generated types, keeping
   only client-internal view models and runtime helpers.
6. Run TypeScript, frontend unit, and API-golden tests.

Any intentional wire-format change must be recorded as a current-contract
change. It must not be justified as preserving a deleted API version.

### Phase 5 — Add the reset-safe processing path

Before reimport, add or finish a dedicated fingerprint command/job that:

1. generates or reuses a preview;
2. prepares the bounded, orientation-corrected JPEG;
3. computes and stores `burst-hash-v1` if absent;
4. assigns burst membership using `burst-cluster-v2`;
5. is idempotent, resumable, batched, and independent of face/VLM services.

The normal import path should enqueue this lightweight stage after preview
generation. AI analysis remains a separate optional stage. This ensures that a
fresh library gets burst grouping even when AI services are disabled.

Add operator output for total assets, fingerprints created, already-present
fingerprints, clusters created, members assigned, skipped assets, and errors.

### Phase 6 — Rebuild from S3

Use a disposable staging database and bucket first.

1. Materialize the external S3 originals and sidecars into an importable
   filesystem layout, preserving original filenames and sidecar relationships.
2. Import through the normal upload/import path, not direct SQL inserts.
3. Run metadata/preview processing.
4. Run fingerprint and burst processing.
5. Start AI processing only if desired; it must not be required for catalog or
   burst correctness.
6. Compare counts and checksums against the S3 inventory.
7. Verify representative samples across RAW, JPEG, HEIF, sidecars, duplicate
   files, malformed metadata, missing capture times, timezone offsets, and
   maker-specific shutter counts.

If preserving user ratings, captions, keywords, albums, or other state is later
required, import that state through an explicit export/import tool keyed by a
stable source checksum—not by old database UUIDs.

### Phase 7 — Cut over and validate

Run the flattened application against the rebuilt staging state and validate:

- browse, search, preview, original download, trash, restore, and deletion;
- upload deduplication and sidecar handling;
- metadata refresh and preview regeneration;
- burst counts, representative selection, deletion, and restore;
- fingerprints and burst membership with AI services disabled;
- optional AI queueing, reuse, failure, retry, and service-unavailable behavior;
- database backup and restore into a second disposable database;
- OpenAPI generation/check and frontend build.

Only after these checks pass should the live database be recreated and the
reimport repeated. Keep the old database volume and final dump available for a
defined rollback window, even though the S3 media remains the primary recovery
source.

### Phase 8 — Remove reset-only scaffolding

After cutover:

- remove temporary import scripts and staging credentials;
- remove reset markers and one-time destructive commands from normal startup;
- retain the reusable S3 inventory, import/export verification, and database
  restore checks;
- update deployment, onboarding, backup, and recovery documentation;
- record the new baseline schema version and application release.

## Rollback

Rollback means starting the previous application against the final PostgreSQL
dump or old database volume. It does not mean attempting to reverse the
baseline migration in place. New S3 objects created after cutover should be
identified by inventory timestamp and retained; do not delete them as part of
database rollback.

## Definition of done

- A new deployment starts with a blank database and one baseline schema.
- No request, worker, or startup path reads legacy schema/API variants.
- The generated OpenAPI spec and frontend client are current and reproducible.
- A clean S3 reimport produces the expected catalog and previews.
- Burst fingerprints and `burst-cluster-v2` memberships are created without AI
  services.
- The application passes the full backend/frontend test suite against fresh
  PostgreSQL and S3 resources.
- A tested database backup and an independent S3 copy remain available.
