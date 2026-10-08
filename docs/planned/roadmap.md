# Photo Server Roadmap

## Current position

The core library is already in strong shape. It has canonical S3 state,
content-addressed photo objects, rebuildable PostgreSQL projections, resumable
uploads, queue leasing and retry logic, RAW-first ingestion, preview caching
and eviction, optional AI services, face review, albums, trash/restore,
exports, migrations, OpenAPI checks, and broad backend integration coverage.

The S3-authority transition, canonical reads and writes, projection rebuild,
reconciliation, recovery checkpoints, and originals retirement are implemented.
The next phase should close the remaining operational contract around S3
snapshot recovery, software/data compatibility, capacity reporting, and
day-to-day workflow quality.

## Recommended priority

1. Production hardening
2. Bulk library workflows and metadata portability
3. UI/UX refinement and accessibility
4. Measurement-driven performance work
5. Larger product expansions such as video

If the server will remain strictly on a trusted LAN, authentication can remain
optional, but canonical-data recovery, observability, storage monitoring, and
recovery testing should still come first.

## Milestone 1: Production hardening

### Authentication and request protection

Authentication is implemented as an opt-in single-user session boundary, but
TLS termination, browser hardening, and deployment guidance remain operational
responsibilities for anything beyond a trusted LAN.

- Harden the existing single-user authentication path for reverse-proxy use.
- Protect canonical objects, previews, uploads, mutations, health details, and API
  documentation appropriately.
- Add authorization boundaries so future multi-user support does not require
  rewriting every route.
- Add CSRF protection for browser-originated mutations.
- Document trusted-LAN, reverse-proxy, and internet-facing deployment modes.
- Add rate limits and request/body limits for uploads and mutation endpoints.

Completion criteria:

- Unauthenticated requests cannot read or mutate the library.
- Uploads and retries continue to work after authentication challenges.
- Authentication behavior is covered by API tests and documented in the
  deployment guide.

### Canonical storage recovery and compatibility

PostgreSQL backups are implemented and useful as an optional fast-recovery
artifact. They are not the authority for user-visible state. Canonical S3 data
is protected by ZFS snapshots and offline backups. Session 5’s compatibility
contract and disposable S3-first recovery exercise are complete; remaining
work is deployment-specific snapshot scheduling and recording production
RPO/RTO values.

- Record production snapshot cadence and measured restore/rebuild timings using
  the completed Session 5 procedure.
- Document what operational queue state is intentionally lost.
- Keep PostgreSQL dumps, if retained, checksum-verified and clearly labeled as
  an acceleration path rather than the durable source of truth.

Completion criteria:

- A canonical S3 snapshot or offline copy has an explicit software/data
  compatibility record.
- A documented recovery exercise rebuilds PostgreSQL and verifies the library
  from that canonical copy.
- RPO/RTO and intentionally non-durable operational state are recorded.

### Observability and operations

The existing health endpoint, structured logging, queue metrics, storage
readiness, backup status, and worker observability provide a foundation.
Production operation still needs trends, correlation, and alertable conditions.

- Emit structured logs with asset ID, batch ID, job ID, operation ID, and stage.
- Extend metrics and dashboards for:
  - upload throughput and failures;
  - onboarding, preview, and AI queue depth and age;
  - job retries, leases, and terminal failures;
  - preview-cache hits, misses, bytes, evictions, and regeneration latency;
  - AI stage latency, reuse rate, and provider errors;
  - canonical snapshot age, recovery-contract status, and optional backup age.
- Separate liveness from readiness checks.
- Add alerts or a documented operator checklist for stale workers, growing
  queues, failed backups, low disk space, and unavailable storage.
- Add a small admin/health view in the web UI.

Completion criteria:

- An operator can determine why ingestion or processing is stalled without
  attaching a debugger.
- Queue and backup failures are visible before they become data-loss events.

### Storage lifecycle and capacity safety

Session 6 is the remaining storage-operations work. The system already has
canonical-object verification, fail-closed report-only garbage collection,
abandoned-upload cleanup, and preview-cache eviction.

- Implement the unified report in
  [`06-storage-capacity.md`](06-storage-capacity.md).
- Account for canonical objects, manifests, processing artifacts, staging,
  recovery checkpoints, backups, previews, and local disk usage separately.
- Add low-disk-space, staging-growth, cache, and backup-age warnings.
- Keep any future deletion limited to explicitly scoped, explainable,
  recoverable candidates; never treat canonical objects as reclaimable merely
  because PostgreSQL does not reference them.

Completion criteria:

- The operator can identify reclaimable storage without changing data.
- Cleanup is recoverable or requires explicit confirmation with a complete
  report of what will be removed.

## Milestone 2: Library workflows and portability

### Generated XMP sidecars

Imported XMP is retained, but generated XMP is currently a documented future
boundary. This is the most important portability feature.

- Define the supported mapping for ratings, captions, keywords, locations,
  and other edited metadata.
- Generate deterministic XMP from PostgreSQL state.
- Store generated sidecars as versioned immutable artifacts or export them on
  demand.
- Provide per-photo and batch export/download actions.
- Make conflicts between imported and generated metadata explicit.
- Add round-trip tests with representative RAW and JPEG fixtures.

Completion criteria:

- A user can export edited metadata and open it in a compatible photo tool.
- Re-running export is deterministic and does not alter originals.

### Bulk selection and actions

Add selection across the timeline, album, search results, and burst views.

- Batch rate and favorite changes.
- Batch album add/remove.
- Batch metadata editing where safe.
- Batch trash/restore.
- Batch preview, metadata, and AI retries.
- Show progress, partial failures, and retryable results.

Completion criteria:

- Common cleanup and curation tasks do not require opening photos one at a
  time.
- Batch mutations retain the existing operation-id and retry guarantees.

### Duplicate and similarity review

The project already has exact-content deduplication and perceptual fingerprints.
Use those foundations to make review safer and more visible.

- Add exact duplicate reports.
- Add a near-duplicate/similar-photo review view.
- Show why assets were grouped or considered similar.
- Provide a safe keep/trash/album workflow without silently merging assets.
- Measure false positives before enabling aggressive automation.

Completion criteria:

- Users can find redundant images and make an explicit decision.
- Every asset remains recoverable until the user intentionally removes it.

### Import synchronization

The current upload client enumerates explicitly selected paths and there is no
source-folder watcher or mass migration.

- Add a dry-run import scan.
- Show selected files, skipped companions, duplicates, and conflicts before
  transfer.
- Add an opt-in source-folder watcher or scheduled scan.
- Preserve the existing RAW/sidecar selection rules.
- Make source-folder identity and rename behavior explicit.

Completion criteria:

- A user can repeatedly import a folder without creating duplicate assets.
- Every automatic action is previewable and explainable.

## Milestone 3: UI/UX and accessibility

### Library navigation

- Add calendar/date jumping.
- Add filters for camera, lens, location, people, scene, and objects.
- Add saved searches and saved filter views.
- Improve burst expansion and representative selection.
- Add a keyboard-shortcuts help overlay.

### State visibility

- Make upload, preview, metadata, and AI states persistent and easy to find.
- Add filters for pending, failed, unavailable, and retryable work.
- Show useful error details without exposing internal secrets.
- Add storage, queue, and backup status to an operator panel.
- Distinguish unsealed, active, failed, sealed, and dismissible upload batches.

### Accessibility and interaction quality

The frontend currently has limited unit coverage, with one domain test and no
substantial component or end-to-end suite.

- Test keyboard navigation through the shell, timeline, loupe, dialogs, and
  inspectors.
- Verify focus trapping and focus restoration in modal dialogs.
- Add accessible names, status announcements, and error associations.
- Verify touch target sizes and dense-layout behavior on phones.
- Add component tests for mutations, retries, upload recovery, and pending
  previews.
- Add a small browser-level smoke suite for login, upload, browse, edit, and
  restore flows.

Completion criteria:

- The main library workflows are usable without a mouse.
- Dialogs and asynchronous state changes are understandable to assistive
  technology.
- A frontend regression suite protects the highest-value workflows.

## Milestone 4: Measurement-driven optimization

Performance work should follow instrumentation, especially after the recent
derivative-cache improvements.

### Preview delivery

- Measure cache hit rate, cold-generation latency, and regeneration frequency.
- Coalesce concurrent requests for the same missing preview.
- Prioritize visible thumbnails over background work.
- Verify that eviction and regeneration remain stable at realistic library
  sizes.

### Database and search

- Capture query plans for timeline, album, trash, people, and combined filters.
- Test performance at representative catalog sizes.
- Add or refine indexes based on measured plans.
- Consider PostgreSQL full-text/trigram indexing for broad literal search.
- Keep cursor pagination stable under concurrent imports and mutations.

### Upload and worker throughput

- Measure storage/API backpressure before increasing concurrency.
- Add adaptive upload concurrency where it improves throughput safely.
- Expose stage timing for onboarding, metadata, previews, face analysis, and
  semantic analysis.
- Measure semantic-reuse hit rate and quality before changing reuse thresholds.
- Add prioritization for recently uploaded or manually opened photos.

Completion criteria:

- Each optimization has a measured before/after result.
- No concurrency change is made without queue, memory, storage, and failure
  behavior being observed.

## Later expansion: video

Video is a substantial product and storage expansion rather than a small
feature. Defer it until the production and photo workflows above are stable.

Required design work would include:

- video ingestion and duplicate identity;
- metadata and capture-time extraction;
- poster-frame and thumbnail generation;
- browser playback and range delivery;
- transcoding policy and worker isolation;
- storage and cache budgeting;
- AI behavior for video frames;
- export and backup semantics.

## Suggested implementation sequence

### Short term

1. Add report-only namespace accounting and capacity scans.
2. Finish alertable operational metrics and recovery/status surfaces.
3. Record production snapshot RPO/RTO using the completed recovery procedure.
4. Harden authentication if the service will leave the trusted LAN.

### Medium term

1. Build bulk selection and batch actions.
2. Implement generated XMP export.
3. Add UI health, queue, storage, and recovery surfaces.
4. Add frontend component and browser smoke coverage.

### Longer term

1. Add duplicate/similarity review.
2. Add dry-run import synchronization.
3. Optimize based on production measurements.
4. Decide whether video justifies its operational and storage cost.

## Definition of production-ready

The project should be considered production-ready for its intended deployment
when:

- access is authenticated whenever the service is not isolated on a trusted
  network;
- canonical S3 data survives loss of any single storage system through tested
  snapshots or offline copies;
- S3-first restore and PostgreSQL projection rebuild have been exercised and
  documented against a known-compatible software release;
- queue, worker, storage, cache, and recovery-contract failures are observable;
- storage growth and cleanup are operationally manageable;
- the main frontend workflows have regression coverage;
- bulk curation and metadata export are available;
- the documented deployment boundaries match the actual security posture.
