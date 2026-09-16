# Phase 3 verification — 2026-09-16

## Delivered behavior

Phase 3 stores complete asset metadata and album snapshots in immutable S3 revisions. It adds captions, keywords, named locations/coordinates, ordered album membership, tombstones, and restore to the API and web frontend. Existing revision 1 imports remain unchanged. Originals and imported sidecars are retained through deletion and restore.

One mutation coordinator serializes state changes using a PostgreSQL advisory lock. It reconciles durable history before constructing a new revision, resolves uncertain S3 writes by inspecting the intended key, reads committed documents back, applies PostgreSQL, and then returns success. Operation IDs and their request/result snapshots survive in revision history. Retries return the original result, including after a database rebuild; old revisions cannot roll the catalog backward. An optional expected revision protects edits based on stale state.

Startup migrates existing Phase 2 ratings/favorites before serving requests. Migration retains local values until the new snapshot is durably written and verified. If a write, read-back, or application fails, startup fails and the migration resumes on retry. Default ratings/favorites are represented by the original import defaults and need no extra revision.

Recovery reconstructs current metadata, albums, ordered memberships, tombstones, and operation results. Missing histories, invalid reference chains, changed original identities, and unreadable/unsupported newest revisions block mutations. Standalone export uses S3 alone, writes current metadata with each active photo, includes album and trash state, and can optionally include trashed originals.

## Automated verification

**89 tests passed** with PostgreSQL, the configured S3 service, and ExifTool. All integration resources used disposable `photo-test-*` buckets and `photo_test_*` databases.

The Phase 3 tests cover:

- Failure after an S3 commit but before the PostgreSQL update; subsequent mutations remain blocked until reconciliation succeeds.
- Lost S3 responses with committed writes, failed uncommitted writes, and inspection of the intended revision before proceeding.
- Database reconstruction, retry of an older operation after a newer revision, rejection of reused IDs with different requests, and monotonic replay for assets and albums.
- Parallel metadata patches without lost fields.
- Phase 2 schema/state migration, interruption after its S3 write, read-back verification, restart, and migration failure blocking API startup while retaining local values.
- Metadata validation, explicit clearing, literal search of edited metadata, and preservation of original EXIF/identity.
- Album creation, rename, description, ordered membership, missing-member rejection, stale-revision rejection, deletion, and restoration.
- Photo tombstones, exclusion from normal browsing, trash browsing, restore, preserved album membership, and refusal to reuse a trashed duplicate during import.
- Full database recovery and standalone export of edited metadata, ordered albums, and trash with original verification.
- Unsupported schemas, missing/noncontiguous ancestry, incomplete snapshots, changed original identity, and missing entire durable histories.
- Fresh SQL initialization, legacy Phase 2 adoption, migration checksums, transactional version tracking, and repeated execution of every idempotent migration file.

The local unit run passed **59 tests**, with **30 integration tests deselected** by their explicit marker. Ruff, JavaScript syntax, packaging, and whitespace checks passed. The built wheel contains the migration runner and all SQL files. API, worker, and web Docker images built successfully at backend version 0.3.0. The test runner reports the two existing Starlette/httpx deprecation warnings.

## Configured database migration

The existing library database was backed up to `.runtime/pre-phase3-db-20260916.UGpUOC.dump`, then migrated explicitly while API and worker processes were stopped. It began at legacy schema version 2 with three assets, no migration ledger, and no non-default local ratings/favorites. The runner adopted versions 1–2, applied `003_durable_user_state.sql`, and reconciled all three S3 assets without errors.

Post-migration checks confirmed schema version 3, three checksum-bearing ledger records, and the `albums`, `album_assets`, and `operations` tables. Running `photo-server migrate` again applied no SQL and completed reconciliation successfully, verifying operational idempotence.

## Browser verification

Chromium checks ran against an isolated API and web deployment with two generated JPEG fixtures, including a migrated Phase 2 rating/favorite. They exercised:

- Caption, keyword, and location editing; metadata search; album creation, editing, membership, deletion, and restore.
- Photo trash and restore, including retained album membership.
- A server-committed rating change followed by an intentionally lost HTTP response: the browser retained its operation ID across reload, retried it, and received the existing revision without an additional write.
- Desktop (1440×1000) and mobile (390×844) rendering without horizontal overflow.
- Photo previews, metadata controls, and album dialogs with no unexpected browser errors and no automated axe accessibility violations.

Desktop and mobile screenshots were inspected. The browser database, bucket, and temporary service containers were isolated from the existing development library.

## Operational boundaries

Use the [README upgrade instructions](../README.md#upgrade-an-existing-phase-2-library) to stop Phase 2 writers before starting the upgraded services. Keep the old PostgreSQL volume until migration completes. Mutation clients must now supply and retain `operationId`.

Complete histories are currently enumerated before mutations and onboarding commits; cost grows with library history. Incremental reconciliation and history compaction are deferred. Keep every revision so old operation IDs remain recoverable. A single application database and API process per library are supported.

Trash has no automatic purge or garbage collection. Generated XMP, photo editing, people/AI state, database backups, and independent NAS recovery remain later work. This phase makes acknowledged user state recoverable from S3; it does not add a separate copy of S3 storage.
