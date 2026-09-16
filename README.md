# Photo Server

Phases 1–3 of the [system design](self_hosted_photo_organizer_design.md): a Python/FastAPI backend that accepts network uploads, queues onboarding in PostgreSQL, and stores originals plus recoverable JSON manifests in S3. A separate threaded worker onboards uploads and generates JPEG previews from embedded RAW previews or JPEG/HEIF originals. The independently deployed web frontend provides a timeline, search, metadata editing, albums, and trash/restore. User changes are saved as immutable S3 revisions before success is returned; PostgreSQL is a recoverable projection.

See the [Phase 1](docs/verification.md), [Phase 2](docs/phase-2-verification.md), and [Phase 3](docs/phase-3-verification.md) verification reports for storage, recovery, API, and browser checks.

## Project structure

```text
backend/                              Python backend and tests
└── src/photo_server/db_migrations/   Ordered PostgreSQL migration SQL
frontend/web/                         Static browser application and nginx runtime
docs/                                 Verification reports
compose.yaml                          Development/deployment composition
```

The backend has no dependency on a frontend build. Each frontend owns its source and runtime and communicates through the published API. The web frontend uses an nginx `/api` proxy to the backend, keeping browser requests on one origin. See the [backend](backend/README.md) and [frontend](frontend/README.md) notes for their individual boundaries.

## Start

Docker and Docker Compose are sufficient; ExifTool and Python dependencies are installed in the backend image, and nginx serves the web image.

```bash
cp -n .env.example .env
chmod 600 .env
```

Set the S3 endpoint and database password in `.env`, then start:

```bash
docker compose up --build -d
```

- Photo library: **http://SERVER_IP:3000/**
- Interactive API documentation: **http://SERVER_IP:8000/docs**
- Health, queue, and asset counts: **http://SERVER_IP:8000/health**
- S3 endpoint: `PHOTO_S3_ENDPOINT` in `.env`; bucket `photo-library`, unsigned requests by default.
- PostgreSQL: `localhost:55432`; credentials and database name come from `POSTGRES_USER`, `POSTGRES_PASSWORD`, and `POSTGRES_DB` in `.env`.
- PostgreSQL and preview caches use separate named Docker volumes. S3 holds originals, manifests, upload declarations, staged uploads, and onboarding receipts.

The API and web frontend bind to all interfaces by default. V1 has no API authentication, so expose ports 8000 and 3000 only on a trusted network. Set `PHOTO_API_BIND=127.0.0.1` and/or `PHOTO_WEB_BIND=127.0.0.1` when a configured reverse proxy is the only entry point.

`docker compose down` stops services while retaining the volumes. Avoid `down -v` unless intentionally discarding the local catalog and cache.

### Upgrade an existing Phase 2 library

Stop the old API and worker before starting the new version so that local-only writes cannot race migration:

```bash
docker compose stop api worker
docker compose build api worker web
docker compose up -d postgres
docker compose run --rm --no-deps api photo-server migrate
docker compose up -d api worker web
```

The explicit `migrate` command applies PostgreSQL schema migrations and then reconciles durable S3 state. API and worker startup run the same checks automatically, but the command makes upgrades observable before serving traffic. It also migrates existing non-default ratings/favorites into revision 2 in S3, reading each new revision back before applying it to PostgreSQL. Interrupted migration resumes on retry. Keep the existing PostgreSQL volume until this completes: S3 cannot recover Phase 2 metadata that was lost before migration. Default ratings/favorites are already represented by the original import's defaults.

### Database migrations

PostgreSQL DDL and data backfills live in numbered SQL files under [`backend/src/photo_server/db_migrations`](backend/src/photo_server/db_migrations). The current sequence is:

| Version | File | Purpose |
|---:|---|---|
| 000 | `000_migration_tracking.sql` | Bootstrap the migration ledger |
| 001 | `001_initial_catalog.sql` | Initial asset, blob, job, and upload catalog |
| 002 | `002_browsing.sql` | Browse fields, ratings/favorites, backfill, constraints, and indexes |
| 003 | `003_durable_user_state.sql` | Albums, operation replay records, and asset tombstones |

Every file is idempotent: table, column, constraint, and index creation is guarded, and the browsing backfill can run repeatedly. Normal operation still executes each version once. The `schema_migrations` table records its version, name, SHA-256 checksum, and application time; startup refuses gaps, checksum changes, version disagreement, a database from another library, or a database newer than the application. Add a new numbered file for every schema change instead of editing an applied file.

The runner holds a PostgreSQL transaction-level advisory lock, applies all pending SQL and ledger updates in one transaction, and updates the compatibility field `library.schema_version`. API and worker processes can start together; one migrates while the other waits and then observes the completed version. New databases run 001 through the current version. Pre-ledger databases are adopted from their existing `library.schema_version`: versions already present are entered in the ledger using the canonical file checksums, then only newer migrations run.

[`catalog.py`](backend/src/photo_server/catalog.py) still declares SQLAlchemy table mappings because application queries need column metadata. Those mappings no longer create or alter tables: `MetaData.create_all()` and inline DDL have been removed. The SQL files are the schema source of truth, while [`migrations.py`](backend/src/photo_server/migrations.py) only discovers, validates, locks, and executes them.

Database schema migration and durable-state migration are separate steps. SQL migrations change PostgreSQL's rebuildable query schema. The following S3 reconciliation restores asset/album state and migrates Phase 2 ratings/favorites outside PostgreSQL. `photo-server migrate` runs both and exits unsuccessfully if either layer needs attention.

### Configuration

`.env` is local and ignored by Git and Docker builds; `.env.example` contains placeholders suitable for committing. Keep a private copy of deployment settings outside Git.

The relevant queue settings are:

| Variable | Default | Purpose |
|---|---:|---|
| `PHOTO_MAX_BATCH_FILES` | 1000 | Maximum declarations in one complete batch |
| `PHOTO_MAX_FILE_BYTES` | 512 MiB | Maximum size of one uploaded file |
| `PHOTO_UPLOAD_WORKERS` | 4 | Simultaneous API-to-S3 transfers; extra requests wait asynchronously |
| `PHOTO_WORKER_THREADS` | 4 | Concurrent onboarding/preview jobs in the worker process |
| `PHOTO_UPLOAD_PART_BYTES` | 8 MiB | Memory and S3 multipart chunk size per active upload |
| `PHOTO_CORS_ORIGINS` | empty | Comma-separated browser origins allowed to call the API |
| `PHOTO_WEB_BIND` | `0.0.0.0` | Host interface for the web frontend |
| `PHOTO_WEB_PORT` | `3000` | Host port for the web frontend |

Python builds the database URL from the PostgreSQL settings, including proper password encoding. Compose supplies the internal database host/port; host-side administration commands use `PHOTO_POSTGRES_HOST` and `PHOTO_POSTGRES_PORT`. `PHOTO_DATABASE_URL` can override the assembled URL.

The bundled web frontend reaches the API through its same-origin proxy, so it does not need a CORS entry. Set `PHOTO_CORS_ORIGINS` for other browser frontends that call port 8000 directly.

For authenticated S3, set `PHOTO_S3_ANONYMOUS=false` and provide AWS credentials in `.env` or through the standard AWS credential chain. Session tokens are supported.

## Upload from another machine

Install this repository on the client machine, then upload explicit files or an explicitly selected directory:

```bash
python3 -m venv .venv
.venv/bin/pip install ./backend

.venv/bin/photo-upload \
  --root /home/me/Pictures \
  --recursive \
  --parallel 8 \
  --wait \
  http://SERVER_IP:8000 \
  trip-to-import
```

The client prints a batch ID before transferring anything. Retain it: rerun with `--batch-id ID` after an interrupted transfer. Directories require `--recursive`, which prevents an accidental whole-library upload when a directory path is mistyped.

The client first declares every filename and size. The server applies the complete-batch selection rules and returns upload URLs only for required files. It therefore does not transfer a same-folder JPEG/HEIF when exactly one same-stem RAW is present. The client then uploads required files concurrently and seals the batch. `--wait` polls until background onboarding completes.

Selection rules:

- Exactly one RAW with the same stem in the same folder takes precedence over declared JPEG/HEIF companions, regardless of declaration order.
- Multiple RAW candidates are reported as ambiguous and kept independently. Without RAW, JPEG/HEIF originals are kept independently.
- One same-stem XMP attaches to one selected media original. Ambiguous/orphan sidecars are reported and skipped.
- Exact media duplicates return the existing asset. A duplicate carrying a new/different sidecar fails because metadata merging is not implemented yet.
- Selection never matches across folders or separate batches.

## Upload lifecycle and queue

An upload batch moves through these states:

```text
accepting -> queued -> processing -> complete
                                  \-> failed -> retry
```

1. `POST /upload-batches` stores an immutable declaration in S3 and creates PostgreSQL file records.
2. Each required `PUT` streams through the API into an immutable multipart object under `incoming/`. Only `PHOTO_UPLOAD_WORKERS` transfers run at once; additional HTTP requests wait on an asynchronous semaphore, so they do not occupy worker threads.
3. `POST /upload-batches/{id}/seal` creates a durable S3 seal marker and PostgreSQL onboarding jobs.
4. The worker thread pool claims jobs with PostgreSQL row locking and leases. Each job verifies the staged bytes, extracts metadata, stores final originals and a manifest, records a durable result receipt, and removes its staged copy.
5. The same worker pool generates previews after onboarding.

The declaration, seal marker, staged objects, final manifest, and result receipt let startup recovery reconstruct interrupted work after PostgreSQL loss. A client must retry an HTTP request interrupted before its file reached S3.

## API

Use `/docs` for the full schemas.

| Endpoint | Purpose |
|---|---|
| `POST /upload-batches` | Declare a complete batch and receive per-file upload URLs |
| `PUT /upload-batches/{batch}/files/{file}` | Stream one required file to durable S3 staging |
| `POST /upload-batches/{batch}/seal` | Close uploads and enqueue background onboarding |
| `GET /upload-batches/{batch}` | Inspect file/job status and results |
| `POST /upload-batches/{batch}/retry` | Requeue failed onboarding jobs |
| `GET /upload-queue` | Inspect active/waiting transfers and durable job counts |
| `GET /assets?limit=100&offset=0` | List paginated manifests |
| `GET /library/assets` | Search/filter the capture-time timeline with cursor pagination |
| `GET /assets/{id}` | Get a manifest and preview-job status |
| `PATCH /assets/{id}/user-state` or `/metadata` | Durably edit rating, favorite, caption, keywords, or location |
| `DELETE /assets/{id}` | Write a tombstone and move a photo to trash |
| `POST /assets/{id}/restore` | Restore a trashed photo |
| `GET /albums?deleted=false` | List active or trashed albums |
| `POST /albums` | Create an album with ordered membership |
| `GET /albums/{id}` | Read album state, including ordered asset IDs |
| `PATCH /albums/{id}` | Rename, describe, add/remove members, or reorder an album |
| `DELETE /albums/{id}` | Move an album to trash without deleting its photos |
| `POST /albums/{id}/restore` | Restore a trashed album |
| `GET /assets/{id}/original` | Stream the original |
| `GET /assets/{id}/preview` | Get a JPEG preview, or `202` while pending |
| `GET /assets/{id}/thumbnail` | Get a JPEG thumbnail, or `202` while pending |
| `POST /assets/{id}/preview/retry` | Retry preview generation |
| `POST /maintenance/reconcile?verify=true` | Reconcile S3 manifests and verify originals |

## Browse the library

Open `http://SERVER_IP:3000/`. The responsive browser UI includes:

- A capture-time timeline grouped by month. Photos without a usable capture time use their import time and are identified as such in the API.
- Incremental loading with stable cursor pagination, newest/oldest sorting, and filters for date, media format, minimum rating, and favorites.
- Case-insensitive literal search across original filenames, camera make/model, lens metadata, captions, keywords, and location names.
- Lazy, bounded thumbnail loading and explicit pending, unavailable, and failed preview states.
- A full preview viewer with recorded EXIF metadata, original download, arrow-key navigation, `F` for favorite, and `0`–`5` for ratings.

The photo viewer edits captions, keywords, named locations and coordinates, and album membership. The album editor supports names, descriptions, membership removal, and ordering. Album collections retain the capture-time timeline; the album editor and API expose their saved membership order. Trash hides photos from normal browsing and supports restore; it never removes originals. Trashing an album leaves its photos in the library. Membership survives photo deletion and restore.

### Durable mutation protocol

Every metadata, album, trash, or restore request requires a client-generated UUID `operationId` in the JSON body. Retain the same ID and body when retrying an interrupted request. For example:

```json
{
  "operationId": "23f6cd22-bedd-443f-927c-d977d779fc06",
  "rating": 5,
  "caption": "Evening at the coast",
  "keywords": ["holiday"],
  "location": {"name": "North shore", "latitude": 45.5, "longitude": -122.5}
}
```

Use `caption: ""`, `keywords: []`, or `location: null` to clear those fields. Album creation requires `name`; `assetIds` replaces the ordered member list in one revision. Delete/restore bodies need only `operationId`. An optional `expectedRevision` rejects edits based on stale state with HTTP 409. Browse trash using `/library/assets?deleted=true` and filter an album using `album_id=UUID`.

A shared PostgreSQL advisory lock serializes mutations, migration, reconciliation, and onboarding commits. The coordinator reconciles before writing, creates an immutable S3 revision, verifies its contents, then applies PostgreSQL. Unknown S3 PUT outcomes are inspected at the intended key. After a database failure, further mutations cannot proceed until reconciliation succeeds. Replaying an older revision never moves the catalog backward.

An already committed operation returns its original result, even if newer revisions exist or PostgreSQL has been rebuilt. Reusing its ID for a different request returns HTTP 409. Fetch current state separately after retry if other changes have happened. The web frontend stores pending requests in browser local storage, scoped to the library ID, and offers **Retry save** after failures or reloads.

Asset revisions live at `state/assets/<id>/<revision>.json`; schema 1 revision 1 imports remain unchanged, and schema 2 revisions contain the original identity plus complete user state and tombstones. Album revisions live at `state/albums/<id>/<revision>.json` and exclusively own membership/order. Retain all revisions: recovery rebuilds retry records from their operation IDs and request/result snapshots.

A minimal one-file sequence is:

```bash
BATCH_ID=$(python3 -c 'import uuid; print(uuid.uuid4())')
SIZE=$(wc -c < DSC01234.ARW)

curl --fail-with-body -X POST http://SERVER_IP:8000/upload-batches \
  -H 'Content-Type: application/json' \
  -d "{\"batchId\":\"$BATCH_ID\",\"files\":[{\"path\":\"DSC01234.ARW\",\"sizeBytes\":$SIZE}]}"

# Copy uploadUrl and fileId from that response:
curl --fail-with-body --upload-file DSC01234.ARW \
  http://SERVER_IP:8000/upload-batches/$BATCH_ID/files/FILE_ID

curl --fail-with-body -X POST \
  http://SERVER_IP:8000/upload-batches/$BATCH_ID/seal

curl --fail-with-body \
  http://SERVER_IP:8000/upload-batches/$BATCH_ID
```

## Recovery and export

Rebuild or reconcile the asset catalog from S3:

```bash
docker compose run --rm api photo-server recover --verify
```

Without `--verify`, recovery checks each blob's existence and size. With it, recovery downloads and hashes every blob. Both modes validate revision schemas, complete histories, ownership, immutable original identity, and album references. Recovery restores metadata, albums, tombstones, and operation results, and reports `recovered`, `albumsRecovered`, `migrated`, and `errors`. Missing, unreadable, or unsupported revisions cause errors; recovery does not silently fall back to older state.

API startup also reconciles upload declarations, completed staged objects, seal markers, and onboarding receipts. The worker uses PostgreSQL leases so a job interrupted while running becomes claimable again.

Export works **without PostgreSQL** and verifies each original:

```bash
mkdir -p .runtime/export
docker compose run --rm --no-deps \
  -v "$PWD/.runtime/export:/export" api photo-server export /export
```

Export writes active originals to `<asset-id>/<original-filename>` plus their latest `manifest.json`, including user metadata, into an empty destination. `library-state.json` preserves all album definitions, ordering, and trashed asset metadata. Add `--include-trash` to also download trashed originals. Export retains imported XMP files; generating interoperable XMP remains optional future work.

## Worker

The worker prioritizes onboarding jobs, then preview jobs. RAW previews come from ExifTool's `JpgFromRaw`, `PreviewImage`, or `ThumbnailImage` tags; no RAW rendering occurs. JPEG and HEIF originals are decoded with Pillow/pillow-heif. Camera orientation is applied to derived previews.

Process one queued job manually:

```bash
docker compose run --rm api photo-server worker --once
```

## Development and verification

```bash
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.lock
.venv/bin/pip install --no-deps -e backend
.venv/bin/ruff check backend/src backend/tests
.venv/bin/pytest -q backend/tests
node --check frontend/web/src/library.js
```

Run the integration suite against the configured S3 service and local PostgreSQL:

```bash
docker compose up -d postgres
docker compose run --rm --no-deps \
  -v "$PWD/backend/tests:/app/tests:ro" -v "$PWD/backend/src:/app/src:ro" \
  -e PYTHONPATH=/app/src -e PHOTO_RUN_INTEGRATION=1 \
  api python -m pytest -q tests -p no:cacheprovider
```

The tests create random `photo-test-*` buckets and `photo_test_*` databases and clean up only those resources. They exercise multipart HTTP upload, RAW companion selection, durable queues, immutable writes, duplicate handling, corruption detection, standalone export, cache reconstruction, Phase 2 migration, metadata crash/retry recovery, concurrent changes, album ordering, and tombstones.

## Current boundaries

- One client/user per library is the supported V1 usage.
- V1 has no API authentication or TLS termination; keep it on a trusted LAN or place it behind a configured reverse proxy.
- Completed files and queue state survive service restarts. An individual client-to-API PUT is streamed and must restart from byte zero if its network connection fails.
- Originals, revisioned user metadata, albums, and tombstones are durable in S3. People assignments, AI analysis, photo editing, and generated XMP remain future work.
- No automatic source-folder watcher or mass migration exists. The network client enumerates only paths explicitly provided by the user.
- Reconciliation currently enumerates complete revision histories at startup, before mutations/onboarding commits, and on explicit recovery. Its work grows with library history; incremental reconciliation is deferred pending measurements on a larger library. One application database and one API process per library remain the supported deployment.
- No automatic garbage collection or original deletion is implemented.
- Application-server recovery is implemented. This repo does not administer or independently back up the SeaweedFS deployment on the NAS.
