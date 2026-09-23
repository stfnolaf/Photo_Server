# Self-Hosted Photo Organizer: System Design

## Current persistence decision (2026-09-16)

This decision supersedes earlier sections of this document wherever they describe PostgreSQL as a disposable projection or per-asset/per-album JSON in S3 as canonical state. Those sections are retained as design history.

- PostgreSQL is the single source of truth for all structured library state: assets, blob references, extracted metadata, ratings, captions, keywords, locations, albums, tombstones, operation retries, and queues.
- S3 is the source of truth for immutable original bytes and imported sidecars. It also holds transient upload objects, generated artifacts when appropriate, and PostgreSQL backup files.
- Normal imports and mutations do not create state manifests, revision histories, upload declarations, seal markers, or result receipts in S3.
- Ingestion and reprocessing share versioned processing stages. New uploads run metadata extraction from their verified local file; `POST /processing` queues the same stage against immutable S3 originals for one, many, or all assets. PostgreSQL job types are the extension point for later face and object detection.
- The bundled backup service writes hourly PostgreSQL custom-format dumps to S3 and retains 168 by default. This gives a one-hour default recovery-point objective; continuous WAL archiving is a future improvement.
- Recovery restores PostgreSQL from a verified backup, verifies referenced S3 objects, and regenerates caches. An empty database cannot reconstruct library metadata from media objects alone.
- A portable application export remains available, but it is an export artifact rather than a second live source of truth.
- The S3/NAS system needs its own independent backup. Keeping the database dump beside the media protects against application-server/database-volume loss, not loss of that shared storage system.

The current invariant is:

> PostgreSQL says what the library is; S3 holds its files; tested backups protect both.

## 1. Purpose

Build a self-hosted photo organizer for a large RAW-centric library without a subscription service.

The system should sit on top of storage the user owns, provide a modern photo-library experience, and remain recoverable even if the application server is completely wiped or the application itself is abandoned.

The design favors:

- RAW-first workflows.
- TrueNAS/ZFS for durable storage.
- SeaweedFS exposing S3-compatible object storage.
- PostgreSQL for fast relational queries and indexing.
- Disposable application/compute nodes.
- Explicit separation between irreplaceable state and regenerable cache data.
- A clean path to future distributed workers or additional storage nodes.
- Escape from the application at any time by exporting S3 objects back to an ordinary filesystem.

The most important architectural invariant is:

> The application server may be destroyed and rebuilt without losing irreplaceable library state.

A second important invariant is that a committed database record never reference unverified, mutable media bytes. Originals are written and verified under immutable S3 keys before the PostgreSQL asset transaction commits.

### V1 scope

- Implementation stack: Python/FastAPI API, a separate threaded Python worker, PostgreSQL jobs, and Docker Compose. Use ExifTool and native image libraries for metadata/preview work.
- One user per library, using one client at a time.
- One API process bounds concurrent network-to-S3 transfers. A small worker thread pool handles durable onboarding and preview jobs. PostgreSQL row locks and per-content-hash advisory locks prevent two workers from claiming the same work or committing the same original concurrently.
- RAW, JPEG, and HEIF originals, with typical RAW sizes of 30–150 MB. Stream imports and hashing instead of holding whole originals in memory. Total library size and asset count remain to be measured.
- Prefer a RAW over same-basename JPEG/HEIF companions in the same folder and import batch, using the rule in Section 34.
- Use embedded RAW previews. Photo editing and RAW rendering are future work.
- Keep stable IDs and storage boundaries that permit later scaling, but defer distributed coordination and multi-client behavior.

The durability guarantee now comes from PostgreSQL backup/restore plus immutable S3 originals, rather than application-maintained JSON mirrors.

### Development environment

- S3 endpoint: `PHOTO_S3_ENDPOINT` in the local `.env`, currently anonymous on the LAN.
- Initial bucket: `photo-library`.
- Existing photos remain outside the application until explicitly uploaded over the network. No NAS source directory is mounted into the containers.
- Initial validation uses disposable fixtures or an explicit handful of photos. No mass migration is authorized.
- The implementation accepts complete client-declared batches of up to 1,000 files by default. It selects RAW/media representations before requesting bytes, limits active transfers, and durably queues onboarding work. See `README.md` for the current protocol and commands.

---

## 2. High-Level Architecture

```text
                               Clients
                    Web / Desktop / Mobile
                                |
                              HTTPS
                                |
                                v
                    +-----------------------+
                    |   Application Server  |
                    |-----------------------|
                    | API                   |
                    | Import coordinator    |
                    | Search                |
                    | PostgreSQL            |
                    | Job queue             |
                    | AI/RAW workers        |
                    |                       |
                    | Local NVMe:           |
                    | - thumbnails          |
                    | - preview cache       |
                    | - temp files          |
                    | - live PostgreSQL     |
                    +-----------+-----------+
                                |
                                | S3 API
                                |
                                v
             +------------------------------------------+
             |              TrueNAS                     |
             |------------------------------------------|
             | ZFS                                      |
             |                                          |
             | SeaweedFS                                |
             | - master/filer                           |
             | - volume storage                         |
             | - S3 gateway                             |
             |                                          |
             | Durable S3 state:                        |
             | - RAW/JPEG/HEIF originals                |
             | - asset manifests                        |
             | - user metadata snapshots                |
             | - edits (future)                         |
             | - AI results / embeddings / faces        |
             | - expensive derived artifacts            |
             | - PostgreSQL backups / WAL archives      |
             +------------------------------------------+
```

The application server performs compute and serves the product.

TrueNAS holds the durable state.

PostgreSQL is the live query engine, but the important state needed to reconstruct the library is also persisted to S3.

---

## 3. Why S3 Instead of Direct SMB Access

S3 and SMB solve different problems.

SMB exposes filesystem semantics:

```text
open("/mnt/photos/foo.ARW")
rename(...)
stat(...)
```

S3 exposes object semantics:

```text
PutObject(key, stream)
GetObject(key)
HeadObject(key)
DeleteObject(key)
```

For this application, object semantics are desirable because they create a strong ownership boundary around the photo library.

The application should not think of a photo as:

```text
/mnt/nas/photos/2026/hawaii/DSC01234.ARW
```

It should think of it as:

```text
asset_id = 01J...
storage_key = originals/01J.../DSC01234.ARW
```

This gives several benefits:

- Stable asset identity is independent of filenames and folders.
- The application cannot accidentally rely on arbitrary POSIX behavior.
- Storage can later move to another S3-compatible implementation without rewriting the library.
- Multiple workers can access the same objects without identical filesystem mounts.
- Additional disks or SeaweedFS volume servers can be added behind the same S3 endpoint.
- Fine-grained credentials can be issued to API servers, workers, backups, and read-only tools.

S3 does **not** automatically make storage distributed. SeaweedFS is responsible for the actual storage topology, placement, replication, rebalancing, and future scale-out.

---

## 4. TrueNAS and SeaweedFS

SeaweedFS should run on or adjacent to the TrueNAS storage so that its persistent data resides on local ZFS-backed storage.

Preferred topology:

```text
Photo App ---- S3 ----> SeaweedFS on TrueNAS ---- local I/O ----> ZFS pool
```

Avoid this topology unless a specific storage implementation explicitly supports it:

```text
Photo App ---- S3 ----> SeaweedFS on another server ---- SMB ----> TrueNAS
```

The latter adds an unnecessary network-filesystem dependency beneath the object store.

### Important ownership rule

Do not expose SeaweedFS's backing data as a normal writable SMB share.

This recreates the original dual-authority problem:

```text
                  S3 writes
                     |
                     v
SeaweedFS backing storage
                     ^
                     |
                  SMB writes
```

Only SeaweedFS should mutate the object-store backing data.

SMB may still be used for separate workflows such as an import drop directory or an exported/read-only filesystem view.

### SeaweedFS persistence

SeaweedFS's filer metadata maps object names to stored content. It is separate from the photo application's PostgreSQL catalog. Recovering PostgreSQL from S3 manifests assumes SeaweedFS can still locate those manifests.

Record the persistent paths for the selected deployment's volume data, filer metadata store, master state, and configuration/credentials. Keep them on persistent NAS storage, outside disposable container filesystems. Verify that recreating the SeaweedFS containers against those paths preserves object access.

S3 is an interface to the stored originals, not an additional copy of them.

---

## 5. NAS Filesystem Layout

The exact SeaweedFS on-disk layout is an implementation detail. At the TrueNAS/ZFS level, keep application-owned datasets separate from human-managed shares.

Conceptually:

```text
tank/
├── seaweed/
│   ├── metadata/
│   └── volumes/
│
├── photo-import/        # optional SMB RW dropbox
│
└── backups/
    └── external-staging/
```

The `seaweed` dataset is application-owned.

The user should not manually rename or delete files inside it.

The optional `photo-import` share is human-owned and may be mounted read/write over SMB.

---

## 6. Durable vs. Disposable State

### Durable state: must survive an app-server wipe

Store on S3/TrueNAS:

- Original RAW files.
- Original JPEG/HEIF files.
- Video originals when video support is introduced.
- Original sidecars.
- Canonical asset manifests.
- User-authored metadata that would be painful to lose.
- Edit instructions when photo editing is introduced; outside V1.
- Album definitions and membership if they are not trivially reconstructable.
- Face detections and face/person assignments.
- AI analysis results.
- Embeddings.
- Expensive-to-regenerate derived data.
- PostgreSQL backups.
- PostgreSQL WAL archives if continuous recovery is implemented.
- Application schema/version metadata required for recovery.

### Disposable state: safe to lose

Store on application-server NVMe:

- Thumbnail cache.
- Preview cache, when cheap to regenerate.
- Temporary RAW conversions when RAW rendering is introduced.
- Temporary upload chunks.
- Transcoding scratch space when video support is introduced.
- Derived search indexes that can be rebuilt.
- Local job scratch state.
- Local logs beyond the desired retention period.

A useful rule is:

> If losing it is merely inconvenient, it can live on the app server.  
> If losing it means redoing human work or hours/days of compute, persist it to S3.

---

## 7. PostgreSQL Placement

Run the live PostgreSQL instance on local SSD/NVMe on the application server for low latency and predictable database behavior.

Do **not** put `PGDATA` on an SMB-mounted NAS share.

```text
App Server NVMe
└── postgres/
```

PostgreSQL should be continuously or frequently backed up to S3:

```text
App Server PostgreSQL
        |
        | pg_dump / base backup / WAL archive
        v
S3
└── backups/postgres/
```

This achieves two goals:

1. The live database performs well.
2. Reimaging the app server does not destroy the only copy of the library catalog.

### Alternative

If near-zero recovery point for PostgreSQL is more important than keeping the app server self-contained, PostgreSQL may instead run directly on TrueNAS or another durable server with its data on a local ZFS dataset.

What should still be avoided is PostgreSQL running on one machine with its live database files on a remote SMB share.

---

## 8. PostgreSQL Is an Index, Not the Only Source of Truth

PostgreSQL should contain the fast relational representation of the library:

- assets
- blobs
- EXIF metadata
- albums
- tags
- ratings
- favorites
- people
- face assignments
- analysis state
- search indexes
- job state

However, destroying PostgreSQL must not make the original S3 objects undecipherable.

At minimum, S3 must retain enough durable metadata to reconstruct:

- asset ID
- original filename
- object key
- content type
- content hash
- byte size
- import time
- capture time when known
- current durable user state
- expensive compute results

The recovery model should be:

```text
S3 durable state
      |
      | recovery/index rebuild
      v
New PostgreSQL
      |
      v
Normal application
```

---

## 9. Asset Identity

Never use the original filename as the primary identity.

Camera filenames repeat.

Use a UUID or ULID generated before the original is stored.

Example:

```text
asset_id = 01K5M6...
original_filename = DSC01234.ARW
```

The asset ID survives:

- renames
- exports
- RAW+JPEG pairing
- sidecars
- edits
- metadata changes
- migrations between storage systems

The original filename remains preserved as metadata.

Keep asset ID, content hash, and original filename separate: the ID identifies the logical photo, the hash identifies/verifies a blob's bytes, and the filename preserves its source name regardless of the chosen object key.

ULIDs are attractive because they remain globally unique while also sorting roughly by creation/import time, but ordinary UUIDs are also acceptable.

---

## 10. S3 Object Layout

A recommended initial layout is a single logical library bucket with structured prefixes.

```text
photo-library/
├── originals/
│   └── <asset-id>/
│       ├── DSC01234.ARW
│       └── DSC01234.xmp
│
├── state/
│   ├── assets/
│   │   └── <asset-id>/
│   │       └── <revision>.json
│   │
│   ├── albums/
│   │   └── <album-id>/
│   │       └── <revision>.json
│   │
│   └── people/
│       └── <person-id>/
│           └── <revision>.json
│
├── analysis/
│   └── <asset-id>/
│       ├── faces/
│       ├── embeddings/
│       └── models/
│
├── durable-derivatives/
│   └── <asset-id>/
│
└── backups/
    └── postgres/
```

Separate buckets are also reasonable if different lifecycle, retention, or permission policies are needed. The key architectural requirement is logical separation, not the exact bucket count.

The sidecar is optional. In V1, a selected RAW does not also bring in its same-basename JPEG/HEIF companions.

---

## 11. Asset Manifest

Every imported asset should have a durable manifest in S3.

One manifest describes one logical photo and lists every blob actually imported for it. V1 normally has one media original, optionally with an original metadata sidecar. Listing blobs now also supports additional originals later without guessing relationships from filenames during recovery.

Example:

```json
{
  "schemaVersion": 1,
  "libraryId": "00000000-0000-4000-8000-000000000001",
  "assetId": "01K5M6H7...",
  "revision": 1,
  "previousRevision": null,
  "operationId": "01K5IMPORT...",
  "primaryBlobId": "01K5BLOB...",
  "blobs": [
    {
      "blobId": "01K5BLOB...",
      "role": "ORIGINAL_RAW",
      "originalFilename": "DSC01234.ARW",
      "objectKey": "originals/01K5M6H7.../DSC01234.ARW",
      "sha256": "8f3a...",
      "sizeBytes": 64321987,
      "mimeType": "image/x-sony-arw"
    }
  ],
  "captureTime": "2026-09-08T18:42:11-10:00",
  "importedAt": "2026-09-14T17:00:00-07:00",
  "metadata": {
    "Make": "Sony",
    "Model": "ILCE-7M4"
  }
}
```

The original RAW remains authoritative for EXIF data, but the manifest makes basic recovery possible without having to inspect every object immediately.

Important values such as content hash and original filename may also be mirrored into S3 object metadata.

---

## 12. Durable User Metadata

If the app server is intended to be truly disposable, human-created metadata should not exist only in PostgreSQL.

Examples:

- rating
- favorite flag
- caption
- keywords
- locations
- album membership
- manual people assignments
- hidden/rejected state

Crop, rotation, and other edit instructions are deferred until a future editor. Original filenames are preserved per blob in the manifest.

Use immutable revisioned JSON state objects as the canonical application metadata.

Example:

```text
state/assets/<asset-id>/00000001.json
state/assets/<asset-id>/00000002.json
state/assets/<asset-id>/00000003.json
```

Each revision contains the current durable state for that entity.

Each revision records an operation ID and the preceding revision number (`null` for the initial revision). The latest successfully committed revision is current. An unreadable or unsupported newest revision is an integrity error; do not silently fall back to older state, which could undo a deletion or human change.

Album state owns membership and ordering; the asset's album list in PostgreSQL is derived from it. Avoid requiring both an asset revision and an album revision for one membership change.

Deletion should be represented by a tombstone rather than immediately destroying the durable history.

For interoperability, selected photo metadata may also be exported as XMP sidecars.

XMP is especially appropriate for:

- ratings
- labels
- keywords
- captions
- copyright

XMP is an optional metadata interoperability/export format in V1. Application-specific relationships such as album membership and manual person assignments remain canonical in JSON. Recovery must not depend on encoding those relationships into XMP, and V1 writes no edit instructions.

---

## 13. S3-First Mutation Semantics

To make the application server disposable, a successful user mutation should be durable outside the application server before the API acknowledges it.

For durable metadata changes:

```text
User changes rating
      |
      v
Write new durable state revision to S3
      |
      v
Apply/update PostgreSQL
      |
      v
Return success
```

This avoids a failure mode where:

1. PostgreSQL is updated locally.
2. The API returns success.
3. The app server disk dies before the state reaches the NAS.

If the S3 write succeeds but the PostgreSQL update fails, the system can replay/reconcile the durable state into PostgreSQL.

All state application must therefore be idempotent.

### V1 coordination and retries

V1 runs one API process and one worker process. PostgreSQL claims onboarding jobs with `FOR UPDATE SKIP LOCKED`, and advisory locks keyed by the primary SHA-256 serialize duplicate detection and manifest commits for identical media. Broad distributed writer election is unnecessary for the one-client V1 scope.

- Reconcile pending durable state on startup before accepting new mutations.
- Use an operation ID retained across retries and stored in the durable revision. A retry of an already-committed operation returns that operation's result without applying the change again.
- Write revisions with create-only semantics so an existing revision cannot be overwritten. Verify this behavior against the deployed storage implementation.
- If an S3 write times out with an unknown outcome, inspect its intended revision and operation ID before retrying or moving on.
- If S3 succeeds but PostgreSQL fails, pause subsequent mutations until reconciliation catches PostgreSQL up. Do not construct the next revision from stale database state.
- Apply a revision to PostgreSQL only if it is newer than the stored revision. Replaying equal or older revisions must not undo current state.

This contract applies to durable user mutations from Phase 3 onward; Phase 2's local metadata is explicitly a prototype milestone.

---

## 14. Import Pipeline

Implemented V1 upload/onboarding flow:

```text
1. The client declares a complete batch with relative filenames and byte sizes
2. Select media using the same-folder RAW preference rule in Section 34 and return upload URLs only for required files
3. Stream and hash each required file through the API into immutable S3 staging, with a bounded number of active transfers
4. Write an S3 seal marker and enqueue one durable PostgreSQL onboarding job per selected photo
5. Worker threads claim jobs with row locks and leases
6. Download and verify one staged asset into bounded local scratch
7. Detect exact duplicates under a per-content-hash lock (Section 33)
8. Extract basic metadata and store new originals in final S3 keys
9. Verify byte count/checksum and write the durable asset manifest
10. Insert/update PostgreSQL and write an S3 onboarding receipt
11. Remove staged objects and queue embedded-preview/thumbnail generation
12. Queue optional AI analysis when implemented
```

The API permits four active transfers by default. Excess upload requests wait on an asynchronous semaphore, while completed uploads remain durable in S3. Onboarding uses four worker threads by default and a durable PostgreSQL queue, so hundreds of declared files do not tie up the API or need to fit in memory. Exact duplicate checking streams one asset into local scratch; peak scratch and memory use remain bounded for 30–150 MB originals.

The import result identifies imported files, exact duplicates, skipped companions, and failures. A skipped companion is not a stored blob and must not appear in the asset manifest as one.

Do not depend on an S3 rename operation.

S3 does not provide normal filesystem renames. Client bytes first use deterministic `incoming/` keys because the content hash and final asset decision are not known until the stream completes. Onboarding chooses immutable final keys, copies verified bytes there, and later deletes staging.

### Partial import handling

Use an explicit asset state machine:

```text
DISCOVERED
UPLOADING
STORED
INDEXED
READY
FAILED
```

Orphaned objects may exist if a crash occurs between S3 storage and PostgreSQL indexing. A reconciliation process should discover and repair or garbage-collect them.

---

## 15. Optional Future SMB Import Dropbox

For convenience, expose a separate NAS share:

```text
/photo-import
```

Permissions:

- User: read/write.
- Photo app: read/write or read/delete after successful import.

Example:

```text
SD card
   |
   v
SMB photo-import/
   |
   v
Importer
   |
   v
S3 originals/
```

Filesystem notifications may be used to trigger imports quickly, but they should not be the correctness mechanism.

Use:

```text
filesystem event -> discover pending files
periodic scan    -> recover missed discoveries
explicit import -> process a completed folder as one batch
```

This is outside V1. V1 clients declare a complete network batch before sending file bytes, which solves the same JPEG-before-RAW ordering problem without a server-side filesystem mount.

Skipped companion files stay untouched in the source folder. Any optional delete-after-import behavior applies only to files actually imported and verified, never to files skipped by the filename heuristic.

The actual S3 library remains app-owned.

---

## 16. Core PostgreSQL Model

A useful starting schema:

### `assets`

```text
id
media_type
capture_time
imported_at
original_filename
primary_blob_id
width
height
duration
camera_make
camera_model
lens
rating
favorite
caption
state_revision
deleted_at
```

### `blobs`

Each blob belongs to exactly one asset in V1. The asset may own several blobs, but distinct assets do not share a blob. `assets.original_filename` is a convenience value from the primary blob; retain each blob's original filename separately for recovery/export.

```text
id
asset_id
role
original_filename
bucket
object_key
sha256
size_bytes
mime_type
created_at
```

Possible roles:

```text
ORIGINAL_RAW
ORIGINAL_JPEG
ORIGINAL_HEIF
SIDECAR
```

Video roles and processing may be added later.

### `albums`

```text
id
name
description
created_at
state_revision
deleted_at
```

### `album_assets`

```text
album_id
asset_id
position
added_at
```

### `analysis_runs`

```text
id
asset_id
analysis_type
model_name
model_version
input_hash
object_key
status
created_at
```

### `faces`

```text
id
asset_id
analysis_run_id
bounding_box
embedding_object_key
person_id
confidence
```

### `people`

```text
id
display_name
state_revision
```

### `jobs`

```text
id
job_type
asset_id
status
attempts
idempotency_key
created_at
updated_at
```

PostgreSQL may also use `pgvector` for fast embedding search even if canonical embeddings remain stored in S3.

V1 also uses `upload_batches`, `upload_files`, and `onboarding_jobs`. These tables track declared files, active/sealed batch state, staged object keys, checksums, job attempts, leases, and results. S3 declarations, seal markers, and result receipts remain the recovery source if these queue tables are lost.

---

## 17. AI and Face Analysis

AI results are expensive-to-recompute derived state and should therefore be durable on S3.

Store outputs by asset and model version.

Example:

```text
analysis/
└── <asset-id>/
    ├── faces/
    │   └── face-detector-v3.json
    │
    ├── embeddings/
    │   └── clip-vit-l14-v2.bin
    │
    └── semantic/
        └── vision-model-v5.json
```

Every analysis artifact should record:

- asset ID
- original content hash
- analysis type
- model name
- model version
- application pipeline version
- creation time

This prevents stale AI results from silently being reused after an original or model changes.

PostgreSQL should index the results for queries, but the expensive outputs should survive a database rebuild.

---

## 18. Thumbnails and Preview Cache

Small thumbnails and ordinary previews are caches.

V1 extracts the embedded preview from RAW originals and derives browser previews/thumbnails from it. If no usable embedded preview is available, keep the original and show "preview unavailable"; defer RAW rendering. Standalone JPEG/HEIF originals are decoded for previews directly.

Example local layout:

```text
/var/cache/photo-app/
├── thumbnails/
│   ├── 256/
│   └── 1024/
│
└── previews/
    └── 2560/
```

Their names should be deterministic from:

- asset ID
- source hash or revision
- derivative profile/version

Example:

```text
<asset-id>-<source-hash>-thumb256-v2.avif
```

This makes invalidation and regeneration straightforward.

If a particular derivative becomes very expensive to regenerate, it may be promoted into `durable-derivatives/` in S3.

---

## 19. Storage Abstraction in Application Code

Even though the initial implementation is S3, keep storage behind a narrow interface.

Example:

```text
BlobStore
---------
Put(key, stream, metadata)
PutIfAbsent(key, stream, metadata)
Get(key)
GetRange(key, offset, length)
Head(key)
Delete(key)
List(prefix)
```

`PutIfAbsent` atomically creates a key or reports that it already exists. Use it for immutable state revisions and originals; an existence check followed by an unconditional write does not provide the same contract. Implement and verify that contract for each backend used.

Avoid leaking arbitrary filesystem concepts into the rest of the application.

Do not make application code depend on:

```text
RenameDirectory
WatchFilesystem
MoveFile
OpenByPath
```

A filesystem backend may still be useful for development or tests:

```text
FilesystemBlobStore
S3BlobStore
```

Production may use `S3BlobStore` pointed at SeaweedFS.

---

## 20. Export and Escape Hatch

S3 does not inherently trap the RAW files.

An S3 object consists of:

- a key
- bytes
- metadata

The original RAW bytes remain intact.

At any time, a generic S3 client may enumerate and download the objects.

Example:

```text
S3:
originals/01K.../DSC01234.ARW
```

can be exported as:

```text
/export/originals/01K.../DSC01234.ARW
```

The photo application should also provide a human-friendly export mode.

Example:

```text
/export/
└── 2026/
    └── Hawaii/
        ├── DSC01234.ARW
        ├── DSC01234.xmp
        ├── DSC01235.ARW
        └── DSC01235.xmp
```

A robust export command should:

1. enumerate assets
2. fetch original bytes
3. restore original filenames
4. optionally write XMP/JSON metadata
5. verify hashes
6. produce an export report

A recovery/export path should also work without PostgreSQL by using S3 manifests and durable state.

Do not assume SeaweedFS's internal backing files are themselves a pleasant human-readable representation of S3 objects. The supported escape hatch is the S3 API.

---

## 21. Database Recovery

Provide a first-class recovery command.

Conceptually:

```text
photo-server recover \
  --s3-endpoint http://truenas:8333 \
  --bucket photo-library
```

Recovery process:

```text
1. Connect to S3
2. Enumerate asset manifests
3. Validate schema versions
4. Validate referenced original objects
5. Reconstruct assets/blobs
6. Extract EXIF where necessary
7. Replay current durable metadata revisions
8. Reconstruct albums/people
9. Load AI analysis records
10. Rebuild vector/search indexes
11. Mark thumbnails/previews missing
12. Regenerate caches lazily or in background
```

This should be testable from an empty PostgreSQL instance.

A disaster-recovery test is not complete until this process has actually been exercised.

---

## 22. Reconciliation

Because S3 and PostgreSQL are separate systems, mismatches will eventually happen.

Run a reconciliation job periodically.

Examples:

### Manifest exists, DB row missing

```text
S3 asset found
DB asset absent
=> reconstruct DB record
```

### DB row exists, original missing

```text
DB references object
S3 HEAD returns missing
=> mark integrity error and alert
```

### Analysis object exists, DB index missing

```text
=> reindex analysis
```

### Temporary/orphan original

```text
S3 object exists
no manifest after grace period
=> quarantine or garbage collect
```

The reconciler should be safe to rerun.

---

## 23. Delete Semantics

Avoid immediate permanent deletion.

Recommended flow:

```text
user deletes asset
      |
      v
write durable tombstone
      |
      v
mark deleted in PostgreSQL
      |
      v
hide from normal UI
      |
      v
retention window
      |
      v
garbage collect blobs
```

A 30- or 60-day retention window is reasonable.

If supported and operationally appropriate, object versioning or ZFS snapshots can provide an additional recovery layer.

---

## 24. Backups

RAID/ZFS redundancy is not a backup.

SeaweedFS data on a redundant ZFS pool protects against some hardware failures, but the NAS remains a single failure domain.

V1 uses the existing mirrored pair of 12 TB HDDs, with disk-failure alerts and a plan to shut down until replacement drives are available. Independent/off-site backup is deferred by choice. The immediate recovery target is an application-server wipe while NAS storage remains available.

A future independent backup would provide:

```text
Primary:
TrueNAS + SeaweedFS

Secondary:
independent copy of the S3 library
```

Possible secondary targets:

- second NAS
- offline external disk
- another physical location
- cloud object storage
- periodically attached backup media

When implemented, the logical backup must include:

- originals
- durable state/manifests
- AI/expensive compute outputs if desired
- PostgreSQL backup chain
- SeaweedFS/application configuration necessary to recover service credentials and topology

An S3-level copy can populate a fresh object store. A physical SeaweedFS backup instead needs volume data together with consistent filer metadata and the deployment state/configuration needed to restore service. Container recreation using existing persistent NAS paths is a V1 acceptance test; restoring after complete NAS loss is deferred.

ZFS snapshots are useful for accidental deletion and rollback but do not replace an independent copy.

---

## 25. PostgreSQL Backup Strategy

PostgreSQL is canonical, so database backup is mandatory rather than an optimization. There are three practical levels.

### Level 1: periodic logical backup

```text
pg_dump -> S3
```

Implemented: the Compose backup service creates a custom-format dump immediately and hourly, stores its SHA-256 in S3 object metadata, retains 168 backups, and provides a guarded restore command. The default RPO is one hour while backup and S3 remain healthy.

### Level 2: periodic physical base backups

Better for larger databases and faster recovery.

### Level 3: base backups + WAL archiving

Allows point-in-time recovery and minimizes metadata loss.

WAL archiving is not implemented yet. It is the next step if the one-hour logical-backup RPO is insufficient. Because there is no second live state representation, changes committed after the newest usable backup are not recoverable after total PostgreSQL-volume loss.

---

## 26. Failure Model

### Application server is wiped

Expected result:

```text
reinstall app
connect to S3
restore PostgreSQL backup
regenerate caches
resume
```

No originals or durable analysis are lost.

### PostgreSQL is corrupted

Expected result:

```text
restore DB backup
verify referenced S3 objects
```

### Thumbnail cache disappears

Expected result:

```text
regenerate
```

No recovery procedure beyond normal background processing is necessary.

### SeaweedFS is temporarily unavailable

The application may continue serving metadata already in PostgreSQL but cannot reliably return originals or commit durable mutations.

Do not acknowledge durable writes while S3 is unavailable.

### TrueNAS is destroyed

Complete NAS loss is outside the V1 recovery guarantee. Once an independent backup is implemented, restore the S3 library from that copy.

### Application project is abandoned

Use an S3 client or a standalone export utility to retrieve all originals and metadata.

This is a deliberate design requirement.

---

## 27. Distributed Compute

S3 and PostgreSQL provide the durable boundary while inference can move
beyond the photo-server machine. The photo server is bookkeeping: it owns
originals, previews, job leases, analysis artifacts, stored embeddings, and
person matching. Intelligence is optional and split into two interchangeable
network services:

```text
                         +----------------------+
                         | Photo API + workers  |
                         | PostgreSQL + S3      |
                         +----------+-----------+
                                    |
                         bounded HTTP clients
                         +----------+-----------+
                         |                      |
                         v                      v
                 OpenAI-compatible VLM    face-service (GPU)
                 local / remote / hosted  YuNet + SFace + AdaFace
```

The AI worker keeps at most `PHOTO_AI_WORKER_CONCURRENCY` requests in flight;
this is a client resource bound, not a rate limiter. The services pace work:
the VLM provider owns its queue/rate limits, while face-service owns a FIFO
queue and returns `429` with `Retry-After` when saturated. Defaults are one
in-flight request and one face-service GPU slot. Semantic burst reuse applies
only to the VLM stage; face inference runs for every claimed image.

Each service needs network access and its own credentials. The photo server
does not need model files or a GPU. Plain HTTP is acceptable on the private
Compose network; cross-machine face-service traffic uses the service's
generated CA/TLS and the shared bearer token. The workers do not need
identical photo filesystem mounts.

---

## 28. Storage Scale-Out

The S3 endpoint hides the physical location of objects from the application.

Initially:

```text
Photo App
    |
    | S3
    v
SeaweedFS
    |
    v
TrueNAS storage
```

Later, if SeaweedFS is expanded:

```text
                   +--> volume server A
Photo App -> S3 -> +--> volume server B
                   +--> volume server C
```

The photo application does not need to change object keys or data models.

Storage expansion and rebalancing remain SeaweedFS operational concerns rather than photo-application concerns.

Do not build a distributed SeaweedFS cluster prematurely. A single-node deployment is sufficient until capacity or redundancy requirements justify more complexity.

---

## 29. Security Boundaries

### App credentials

The photo application gets S3 permissions required for its buckets/prefixes.

### Worker credentials

Workers should receive only the prefixes they need where practical.

Example:

```text
originals/*      read
analysis/*       read/write
```

### Backup credentials

Backup jobs generally need read access to all durable objects and write access only to the backup destination.

### Human NAS account

The user's ordinary SMB account should **not** have write access to SeaweedFS backing storage.

It may have:

```text
photo-import/    read/write
photo-export/    read
```

### Network exposure

Prefer:

```text
Internet/client
    |
   HTTPS
    |
Photo API
```

Do not expose SeaweedFS's S3 endpoint directly to the public internet unless there is a specific need and appropriate security controls.

---

## 30. API Surface

An initial API may include:

```text
POST   /upload-batches
PUT    /upload-batches/:batch/files/:file
POST   /upload-batches/:batch/seal
GET    /upload-batches/:batch
POST   /upload-batches/:batch/retry
GET    /upload-queue

GET    /assets/:id
DELETE /assets/:id

GET    /assets/:id/original
GET    /assets/:id/thumbnail
GET    /assets/:id/preview

PATCH  /assets/:id/metadata

POST   /albums
GET    /albums/:id
PATCH  /albums/:id
POST   /albums/:id/assets

GET    /search
GET    /people

POST   /assets/:id/reanalyze
POST   /maintenance/reconcile
```

Upload currently flows through the application server into S3 multipart staging. A bounded asynchronous gate controls active transfers; PostgreSQL queues subsequent onboarding independently of the HTTP connection.

A future optimization may use presigned S3 uploads, with the application generating the asset ID and upload target before the client uploads directly to SeaweedFS.

---

## 31. Job Processing

Start simple.

A PostgreSQL-backed jobs table is likely sufficient for one user.

Jobs should have:

- unique ID
- job type
- asset ID
- idempotency key
- status
- attempt count
- lease/heartbeat if workers are distributed
- pipeline/model version

Typical jobs:

```text
ONBOARD_UPLOAD
EXTRACT_METADATA
GENERATE_THUMBNAIL
GENERATE_PREVIEW
FACE_DETECTION
GENERATE_EMBEDDING
SEMANTIC_ANALYSIS
RECONCILE_ASSET
DELETE_EXPIRED_ASSET
```

Do not introduce Redis/NATS/Kafka unless the workload actually requires it.

---

## 32. Content Hashing and Integrity

Compute a strong content hash during import, ideally SHA-256.

Use it for:

- integrity verification
- duplicate detection
- corruption detection
- derivative invalidation
- analysis input identity

Store it in:

- asset manifest
- PostgreSQL
- optionally S3 user metadata

Example invariant:

```text
for each blob in manifest.blobs:
    sha256(download(blob.objectKey)) == blob.sha256
```

The app should periodically or on-demand verify objects against recorded hashes.

---

## 33. Duplicate Handling

The same byte-identical RAW, JPEG, or HEIF may be imported more than once.

The content hash allows detection.

V1 policy:

```text
if sha256 matches an existing active asset's original:
    return the existing asset
    do not create another asset or store another original
```

If the matching asset is in the trash, report that state and require an explicit restore before reusing it. Do not silently resurrect a deleted asset through import.

One asset can belong to many albums without duplicating its original. V1 does not share blobs between distinct assets. Such sharing would require an `asset_blobs` relationship and deletion rules that retain blobs while any asset still references them; defer both until needed.

Identical filenames do not prove identical content. The separate RAW preference heuristic below chooses which representation to import without claiming their bytes are duplicates.

---

## 34. RAW + JPEG + Sidecars

### V1 import selection

Within one complete import batch, group media candidates by source-relative parent folder and filename stem (the filename without its final extension). Compare stems exactly; recognize supported extensions case-insensitively.

- If a group contains exactly one supported RAW, select it and skip its JPEG/HEIF companions.
- If there is no RAW, import the JPEG/HEIF files normally. Do not choose arbitrarily between JPEG and HEIF; only byte-identical duplicates are collapsed by Section 33.
- If there are multiple RAW candidates or source folders cannot be distinguished, preserve the candidates for normal import and report the ambiguity rather than guessing which image to suppress.
- This is a filename heuristic for common camera output, not visual similarity detection. Show skipped files and the selected RAW in the import result.
- Confirm companion skips as successful only when the selected RAW is durably imported or matched to an existing active asset. If RAW import fails, report the group failure and leave companion files available for retry or a separate import.
- Never delete skipped source files. XMP files are metadata sidecars, not JPEG/HEIF alternatives to discard; an imported sidecar is listed separately in the manifest.
- Do not match across folders or independent import batches. A JPEG imported earlier is not automatically replaced when a RAW arrives later.

Examples:

| Files in one import batch | Result |
|---|---|
| `trip/DSC01234.ARW`, `trip/DSC01234.HEIF`, `trip/DSC01234.JPG` | Import RAW; skip HEIF and JPEG |
| `trip/DSC01235.HEIC` | Import HEIF original |
| `trip/DSC01236.JPG`, `trip/DSC01236.HEIF` | Import both; no RAW preference applies |
| `day1/DSC01234.ARW`, `day2/DSC01234.JPG` | Import both; folders differ |
| `trip/DSC01234.ARW`, `trip/DSC01234.DNG`, `trip/DSC01234.JPG` | Report ambiguity and import candidates normally |

### Asset/blob structure

One logical `Asset` may own multiple blobs, but V1 normally stores only the selected media original and any imported metadata sidecar. Skipped companions are not part of the stored asset.

V1 example:

```text
Asset 01K...
├── DSC01234.ARW
└── DSC01234.xmp
```

The data model should therefore distinguish:

```text
Asset
```

from:

```text
Blob
```

This leaves room for future support of:

- RAW+JPEG pairs
- Live Photo-like pairs
- video + sidecar
- alternate originals
- imported XMP metadata

---

## 35. Original Immutability

Original media objects should be immutable after successful import.

V1 changes metadata only and has no photo editor. When editing is introduced, edits should create:

- edit instructions
- metadata revisions
- derivatives

They should not rewrite the RAW.

This provides a clear integrity model and makes backup/recovery dramatically simpler.

---

## 36. Observability

Expose basic operational metrics:

- S3 connectivity
- PostgreSQL connectivity
- available NAS capacity
- local cache capacity
- import queue depth
- active and waiting network uploads
- analysis queue depth
- failed jobs
- reconciliation errors
- missing objects
- checksum failures
- last successful PostgreSQL backup
- last successful independent S3 backup, when configured

The UI should make integrity problems visible rather than silently ignoring them.

---

## 37. Non-Goals for V1

Do not initially build:

- distributed PostgreSQL
- multiple users/clients per library or multiple metadata writers
- a multi-node SeaweedFS cluster unless required
- complex event streaming infrastructure
- automatic cross-cloud replication
- arbitrary filesystem synchronization
- bidirectional SMB/S3 mutation
- destructive in-place RAW edits
- photo editing and RAW rendering
- visual duplicate detection or pairing across import batches
- shared blobs between distinct assets
- elaborate cache tiering
- custom object-store implementation

The goal is a reliable photo application, not a storage research project.

---

## 38. Recommended Deployment for V1

### TrueNAS

```text
ZFS
└── SeaweedFS
    └── S3 endpoint
```

Durable objects:

```text
originals/
state/
analysis/
durable-derivatives/
backups/postgres/
```

Optional:

```text
SMB photo-import/
```

### Application server

Containers/services:

```text
reverse proxy
photo-api
photo-worker
postgres
```

Local NVMe:

```text
postgres data
thumbnail cache
preview cache
temp processing
```

### Network

```text
clients -> HTTPS -> app server
app server -> S3 -> TrueNAS
app server -> PostgreSQL localhost/private network
```

SeaweedFS stays on the trusted LAN.

---

## 39. Suggested Implementation Order

### Phase 1: Storage and core import

Implement:

- asset IDs
- S3 BlobStore
- complete-batch enumeration and same-folder RAW preference
- declared network batches, bounded multipart staging, and durable onboarding jobs
- threaded onboarding workers
- SHA-256
- exact-duplicate reuse of existing assets
- manifest with a blob list and original filenames
- PostgreSQL asset/blob schema
- EXIF extraction
- basic recovery scan
- verification that SeaweedFS container recreation preserves stored objects

At the end of Phase 1, a RAW should be importable and recoverable from S3 without relying on the original PostgreSQL instance.

### Phase 2: Browsing

Implement:

- thumbnail generation from embedded RAW previews or JPEG/HEIF originals
- previews, with an explicit unavailable state when a RAW has no usable embedded preview
- timeline
- metadata display
- rating/favorite
- basic search

Phase 2 uses the PostgreSQL schema introduced in Phase 1. Ratings/favorites may be local-only at this prototype milestone; their survival across an app-server wipe is introduced in Phase 3.

### Phase 3: Durable user state

Implement:

- revisioned asset state in S3
- one serialized mutation coordinator with operation IDs and crash reconciliation
- albums
- metadata edits
- tombstones
- reconciliation

At the end of Phase 3, destroying PostgreSQL should be inconvenient but not catastrophic.

As part of this phase, migrate any existing Phase 2 ratings/favorites into durable state and verify them before claiming the durability guarantee. Metadata editing here means fields such as ratings, captions, and locations; photo editing is deferred.

### Phase 4: Operational recovery

Implement:

- PostgreSQL backup to S3
- WAL archiving if desired
- full `recover` command
- integrity checker
- export command
- automated disaster-recovery test

### Phase 5: AI

The optional AI split is now implemented as two services: the photo server
dispatches through an OpenAI-compatible VLM and the standalone face-service
owns face detection and embeddings. Remaining work is scanner consolidation,
model/version tracking refinements, durable AI outputs in S3, and any future
PostgreSQL/pgvector indexing.

### Phase 6: Scale-out

Only when needed:

- additional worker machines
- GPU inference services (optional and independently placed)
- additional SeaweedFS volume capacity
- more sophisticated queues
- independent/off-site replication

---

## 40. Acceptance Tests for the Architecture

Before trusting the system with the real library, perform these tests.

Run the relevant cases as each phase introduces its functionality. Phase 2 remains a prototype with local user metadata; user-state recovery applies from Phase 3, and AI recovery applies once Phase 5 is implemented.

### Test A: Destroy PostgreSQL

```text
1. Import sample library.
2. Create ratings/albums.
3. Run AI analysis.
4. Delete PostgreSQL.
5. Create empty PostgreSQL.
6. Run recovery.
```

Expected:

- originals return
- asset identity returns
- user metadata returns
- albums return
- expensive analysis returns
- thumbnails regenerate

### Test B: Wipe application server

```text
1. Shut down server.
2. Reimage server.
3. Reinstall app.
4. Connect it to the existing S3 endpoint.
```

Expected:

- no irreplaceable information lost
- library can be restored/rebuilt

### Test C: Export without application

Using only S3-compatible tooling:

```text
1. Enumerate originals.
2. Download all objects.
3. Verify checksums.
```

Expected:

- every original RAW remains independently retrievable

### Test D: Full human-friendly export

Using the standalone/application export tool:

```text
1. Export to filesystem.
2. Restore original names.
3. Write sidecar metadata.
4. Verify hashes.
```

Expected:

- the library can leave the system cleanly

### Test E: Partial import crash

Kill the application at different stages of import.

Expected:

- restart/reconciliation produces either a valid asset or a safe orphan eligible for cleanup
- no ambiguous half-registered original

### Test F: Import representation selection

Exercise the cases in Section 34, including a client batch where the JPEG is listed before its RAW, same stems in different folders, multiple RAW candidates, and a RAW that fails import.

Expected:

- an unambiguous RAW group imports only the RAW media original, plus any selected metadata sidecar
- skipped companion media are not transferred to S3 or listed as stored blobs
- same-stem files in other folders and ambiguous candidates are not suppressed
- a failed RAW import is reported and all skipped source files remain available
- retrying a completed import returns the existing asset without adding another copy

### Test G: Recreate SeaweedFS containers

Recreate the containers with their existing persistent NAS paths and configuration. Enumerate manifests, retrieve originals, and verify recorded hashes.

Expected: object names and content remain accessible without the old containers or the photo application's PostgreSQL instance.

### Test H: Durable metadata crash and retry

From Phase 3, interrupt a rating change after its S3 write but before its PostgreSQL update or client response. Restart, reconcile, and retry using the same operation ID. Also replay an older revision after a newer one.

Expected: the committed change survives, its retry does not apply it again, and replay cannot move PostgreSQL backward to an older revision.

---

## 41. Core Design Decisions

| Concern | Decision |
|---|---|
| V1 usage | One user and one client at a time per library; one serialized metadata writer |
| Original storage | SeaweedFS S3 on TrueNAS/ZFS |
| Original mutation | Immutable after import |
| Asset identity | UUID/ULID, independent of filename |
| Asset record | PostgreSQL snapshot with owned blobs, hashes, original filenames, and metadata |
| Import selection | Prefer one RAW over same-stem JPEG/HEIF in the same folder and batch |
| Exact duplicates | Reuse the existing active asset; no blobs shared between distinct assets |
| V1 previews | Embedded RAW previews; decode standalone JPEG/HEIF; no RAW rendering |
| Photo editing | Deferred; V1 metadata only |
| Live database | PostgreSQL on app-server local NVMe |
| DB durability | Hourly custom-format backup to S3; 168 retained; WAL future work |
| DB role | Single source of truth for structured library state |
| Durable metadata | Canonical PostgreSQL records; XMP optional for metadata export |
| AI/face output | S3, indexed by PostgreSQL |
| Thumbnails/cache | Local app-server NVMe, regenerable |
| Human RW NAS access | Separate import share only |
| S3 backing storage | Never manually mutated over SMB |
| SeaweedFS deployment | Persistent NAS paths for storage/catalog/configuration; verify container recreation |
| NAS disaster recovery | Independent backup deferred in V1 |
| Export | Database-backed app export with S3 checksum verification |
| Scale-out | Hidden behind S3; SeaweedFS/worker concern |
| Internet access | Through photo API, not directly to storage |
| Disaster-recovery target | App server may be wiped without losing durable state |

---

## 42. Final Mental Model

The system has three layers.

```text
                 PRODUCT LAYER
          Photo API / UI / search / jobs
                        |
                        v
               STRUCTURED STATE
                    PostgreSQL
          canonical, relational, backed up
                        |
                        v
                  FILE LAYER
                SeaweedFS / S3
       originals + sidecars + DB backups
                        |
                        v
                    TrueNAS/ZFS
```

PostgreSQL answers:

> "What is in this library, and what matches this query?"

S3 answers:

> "Where are the immutable file bytes and database backups?"

ZFS answers:

> "How are those bytes safely stored on these disks?"

The application server answers:

> "How do I turn those durable objects into a useful photo-management experience?"

The design should preserve this separation.

If the application server disappears, the durable library remains.

If the PostgreSQL volume disappears, restore the newest verified database backup from S3.

If the application is abandoned, S3 still exposes original bytes and PostgreSQL can be restored with standard tools. A portable export should be created before abandoning the application if ordinary filesystem metadata is desired.

If additional compute or storage is added later, it can join behind existing S3 and API boundaries without changing the identity of the photo library.
