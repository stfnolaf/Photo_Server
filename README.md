# Photo Server

Phases 1 and 2 of the [system design](self_hosted_photo_organizer_design.md): a Python/FastAPI photo library that accepts network uploads, queues onboarding in PostgreSQL, and stores originals plus recoverable JSON manifests in S3. A separate threaded worker onboards uploads and generates JPEG previews from embedded RAW previews or JPEG/HEIF originals. The browser UI provides a timeline, metadata, search, filters, ratings, and favorites.

See the [Phase 1](docs/verification.md) and [Phase 2](docs/phase-2-verification.md) verification reports for storage, recovery, API, and browser checks.

## Start

Docker and Docker Compose are sufficient; ExifTool and Python dependencies are installed in the application image.

```bash
cp -n .env.example .env
chmod 600 .env
```

Set the S3 endpoint and database password in `.env`, then start:

```bash
docker compose up --build -d
```

- Photo library: **http://SERVER_IP:8000/**
- Interactive API documentation: **http://SERVER_IP:8000/docs**
- Health, queue, and asset counts: **http://SERVER_IP:8000/health**
- S3 endpoint: `PHOTO_S3_ENDPOINT` in `.env`; bucket `photo-library`, unsigned requests by default.
- PostgreSQL: `localhost:55432`; credentials and database name come from `POSTGRES_USER`, `POSTGRES_PASSWORD`, and `POSTGRES_DB` in `.env`.
- PostgreSQL and preview caches use separate named Docker volumes. S3 holds originals, manifests, upload declarations, staged uploads, and onboarding receipts.

The API binds to all interfaces by default so another LAN machine can upload. V1 has no API authentication, so expose port 8000 only on a trusted network. Set `PHOTO_API_BIND=127.0.0.1` if a reverse proxy will be the only entry point.

`docker compose down` stops services while retaining the volumes. Avoid `down -v` unless intentionally discarding the local catalog and cache.

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

Python builds the database URL from the PostgreSQL settings, including proper password encoding. Compose supplies the internal database host/port; host-side administration commands use `PHOTO_POSTGRES_HOST` and `PHOTO_POSTGRES_PORT`. `PHOTO_DATABASE_URL` can override the assembled URL.

For authenticated S3, set `PHOTO_S3_ANONYMOUS=false` and provide AWS credentials in `.env` or through the standard AWS credential chain. Session tokens are supported.

## Upload from another machine

Install this repository on the client machine, then upload explicit files or an explicitly selected directory:

```bash
python3 -m venv .venv
.venv/bin/pip install .

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
| `PATCH /assets/{id}/user-state` | Set a local rating (0–5) and/or favorite flag |
| `GET /assets/{id}/original` | Stream the original |
| `GET /assets/{id}/preview` | Get a JPEG preview, or `202` while pending |
| `GET /assets/{id}/thumbnail` | Get a JPEG thumbnail, or `202` while pending |
| `POST /assets/{id}/preview/retry` | Retry preview generation |
| `POST /maintenance/reconcile?verify=true` | Reconcile S3 manifests and verify originals |

## Browse the library

Open `http://SERVER_IP:8000/`. The responsive browser UI includes:

- A capture-time timeline grouped by month. Photos without a usable capture time use their import time and are identified as such in the API.
- Incremental loading with stable cursor pagination, newest/oldest sorting, and filters for date, media format, minimum rating, and favorites.
- Case-insensitive literal search across original filenames, camera make/model, and lens metadata.
- Lazy, bounded thumbnail loading and explicit pending, unavailable, and failed preview states.
- A full preview viewer with recorded EXIF metadata, original download, arrow-key navigation, `F` for favorite, and `0`–`5` for ratings.

Ratings and favorites persist in PostgreSQL across normal service restarts and reconciliation. They are intentionally local prototype state in Phase 2: rebuilding PostgreSQL from S3 resets them. Phase 3 will migrate this state to revisioned durable objects before providing database-loss recovery.

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

Without `--verify`, recovery checks each blob's existence and size. With it, recovery downloads and hashes every blob. Both modes validate manifest schemas, ownership, and references. Unsupported newest revisions cause errors; recovery does not silently fall back to older revisions.

API startup also reconciles upload declarations, completed staged objects, seal markers, and onboarding receipts. The worker uses PostgreSQL leases so a job interrupted while running becomes claimable again.

Export works **without PostgreSQL** and verifies each original:

```bash
mkdir -p .runtime/export
docker compose run --rm --no-deps \
  -v "$PWD/.runtime/export:/export" api photo-server export /export
```

Export writes `<asset-id>/<original-filename>` plus `manifest.json` into an empty destination. It retains imported XMP files. Generating interoperable XMP from user metadata is future work.

## Worker

The worker prioritizes onboarding jobs, then preview jobs. RAW previews come from ExifTool's `JpgFromRaw`, `PreviewImage`, or `ThumbnailImage` tags; no RAW rendering occurs. JPEG and HEIF originals are decoded with Pillow/pillow-heif. Camera orientation is applied to derived previews.

Process one queued job manually:

```bash
docker compose run --rm api photo-server worker --once
```

## Development and verification

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/pip install --no-deps -e .
.venv/bin/ruff check src tests
.venv/bin/pytest -q
```

Run the integration suite against the configured S3 service and local PostgreSQL:

```bash
docker compose up -d postgres
docker compose run --rm --no-deps \
  -v "$PWD/tests:/app/tests:ro" -v "$PWD/src:/app/src:ro" \
  -e PYTHONPATH=/app/src -e PHOTO_RUN_INTEGRATION=1 \
  api python -m pytest -q tests -p no:cacheprovider
```

The tests create random `photo-test-*` buckets and `photo_test_*` databases and clean up only those resources. They exercise multipart HTTP upload, RAW companion selection, PostgreSQL-backed onboarding, durable queue recovery, immutable writes, duplicate handling, corruption detection, standalone export, and cache reconstruction.

## Current boundaries

- One client/user per library is the supported V1 usage.
- V1 has no API authentication or TLS termination; keep it on a trusted LAN or place it behind a configured reverse proxy.
- Completed files and queue state survive service restarts. An individual client-to-API PUT is streamed and must restart from byte zero if its network connection fails.
- Immutable revision-1 manifests and original imports are implemented. Phase 2 ratings/favorites are local PostgreSQL state. Albums, people, generated XMP, and durable user-state revisions remain in later phases.
- No automatic source-folder watcher or mass migration exists. The network client enumerates only paths explicitly provided by the user.
- Recovery currently enumerates manifests on explicit reconciliation/startup. Incremental reconciliation can follow measurements on a larger library.
- No automatic garbage collection or original deletion is implemented.
- Application-server recovery is implemented. This repo does not administer or independently back up the SeaweedFS deployment on the NAS.
