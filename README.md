# Photo Server

This is a Python/FastAPI, PostgreSQL, and S3 photo library. PostgreSQL is the single source of truth for assets, extracted metadata, user metadata, albums, operation retries, tombstones, jobs, and the current analysis index. S3 holds immutable originals, imported XMP sidecars, versioned AI artifacts, upload staging objects, and scheduled PostgreSQL backups. A threaded media worker keeps uploads and previews responsive; a separate single-concurrency CUDA worker performs face and semantic analysis without entering the ingestion critical path.

See the historical [Phase 1](docs/verification.md), [Phase 2](docs/phase-2-verification.md), and [Phase 3](docs/phase-3-verification.md) reports, plus the current [PostgreSQL-authority verification](docs/postgres-authority-verification.md).

## Project structure

```text
backend/                              Python backend and tests
└── src/photo_server/db_migrations/   Ordered PostgreSQL migration SQL
frontend/web/                         React/TypeScript browser application and nginx runtime
docs/                                 Verification reports
compose.yaml                          Development/deployment composition
```

The backend has no dependency on a frontend build. Each frontend owns its source and runtime and communicates through the published API. The web frontend uses an nginx `/api` proxy to the backend, keeping browser requests on one origin. See the [backend](backend/README.md) and [frontend](frontend/README.md) notes for their individual boundaries.

## Start

Docker and Docker Compose are sufficient for the core service. Local AI also requires an NVIDIA driver, NVIDIA Container Toolkit, and the verified AdaFace model directory from the sibling face-scanner. Confirm `docker run --rm --gpus all ubuntu nvidia-smi` works before starting.

```bash
cp -n .env.example .env
chmod 600 .env
```

Set the S3 endpoint and database password in `.env`. `PHOTO_FACE_MODEL_DIR` defaults to `../face-scanner/runtime/raw-jpeg/models`; it must contain `face_detection_yunet_2023mar.onnx`, `face_recognition_sface_2021dec.onnx`, `adaface-ir101.onnx`, and `adaface-ir101.json`. The worker verifies their provenance and checksums and refuses CPU fallback. Then start:

```bash
docker compose up --build -d
```

The first start pulls the approximately 6.1 GB `qwen3-vl:8b-instruct-q4_K_M` model into the persistent `ollama-data` volume. API, upload, and preview services can run while that one-time download completes. Ollama has no published host port; photographs are sent only over the private Compose network.

- Photo library: **http://SERVER_IP:3000/**
- Interactive API documentation: **http://SERVER_IP:8000/docs**
- Health, queue, and asset counts: **http://SERVER_IP:8000/health**
- S3 endpoint: `PHOTO_S3_ENDPOINT` in `.env`; bucket `photo-library`, unsigned requests by default.
- PostgreSQL: `localhost:55432`; credentials and database name come from `POSTGRES_USER`, `POSTGRES_PASSWORD`, and `POSTGRES_DB` in `.env`.
- PostgreSQL and preview caches use separate named Docker volumes. S3 holds originals, imported sidecars, staging objects, and hourly PostgreSQL backups under `backups/postgres/`.

The API and web frontend bind to all interfaces by default. V1 has no API authentication, so expose ports 8000 and 3000 only on a trusted network. Set `PHOTO_API_BIND=127.0.0.1` and/or `PHOTO_WEB_BIND=127.0.0.1` when a configured reverse proxy is the only entry point.

`docker compose down` stops services while retaining the volumes. Avoid `down -v` unless intentionally discarding the local catalog and cache.

### Upgrade an existing Phase 2 library

Stop the old API and worker before starting the new version so that local-only writes cannot race migration:

```bash
docker compose stop api worker ai-worker backup
docker compose build api worker ai-worker web backup
docker compose up -d postgres
docker compose run --rm --no-deps api photo-server migrate
docker compose up -d api worker ai-worker web backup
```

The explicit `migrate` command applies PostgreSQL schema migrations before traffic resumes. Startup performs the same check automatically. Existing Phase 2 ratings/favorites are promoted into the PostgreSQL asset snapshot; no S3 state documents are created. Keep the existing PostgreSQL volume until a verified version-4 backup has completed because PostgreSQL is now authoritative.

### Database migrations

PostgreSQL DDL and data backfills live in numbered SQL files under [`backend/src/photo_server/db_migrations`](backend/src/photo_server/db_migrations). The current sequence is:

| Version | File | Purpose |
|---:|---|---|
| 000 | `000_migration_tracking.sql` | Bootstrap the migration ledger |
| 001 | `001_initial_catalog.sql` | Initial asset, blob, job, and upload catalog |
| 002 | `002_browsing.sql` | Browse fields, ratings/favorites, backfill, constraints, and indexes |
| 003 | `003_durable_user_state.sql` | Albums, operation replay records, and asset tombstones |
| 004 | `004_postgres_authority.sql` | Record PostgreSQL as the structured-state authority |
| 005 | `005_ai_analysis.sql` | AI run artifacts, face embeddings/groups, current search index, and analysis queue |

Every file is idempotent: table, column, constraint, and index creation is guarded, and the browsing backfill can run repeatedly. Normal operation still executes each version once. The `schema_migrations` table records its version, name, SHA-256 checksum, and application time; startup refuses gaps, checksum changes, version disagreement, a database from another library, or a database newer than the application. Add a new numbered file for every schema change instead of editing an applied file.

The runner holds a PostgreSQL transaction-level advisory lock, applies all pending SQL and ledger updates in one transaction, and updates the compatibility field `library.schema_version`. API and worker processes can start together; one migrates while the other waits and then observes the completed version. New databases run 001 through the current version. Pre-ledger databases are adopted from their existing `library.schema_version`: versions already present are entered in the ledger using the canonical file checksums, then only newer migrations run.

[`catalog.py`](backend/src/photo_server/catalog.py) still declares SQLAlchemy table mappings because application queries need column metadata. Those mappings no longer create or alter tables: `MetaData.create_all()` and inline DDL have been removed. The SQL files are the schema source of truth, while [`migrations.py`](backend/src/photo_server/migrations.py) only discovers, validates, locks, and executes them.

All structured-state migrations happen in PostgreSQL. Application startup also promotes any legacy Phase 2 rating/favorite columns into the current PostgreSQL asset document.

### Configuration

`.env` is local and ignored by Git and Docker builds; `.env.example` contains placeholders suitable for committing. Keep a private copy of deployment settings outside Git.

The relevant queue settings are:

| Variable | Default | Purpose |
|---|---:|---|
| `PHOTO_MAX_BATCH_FILES` | 1000 | Maximum declarations in one complete batch |
| `PHOTO_MAX_FILE_BYTES` | 512 MiB | Maximum size of one uploaded file |
| `PHOTO_UPLOAD_WORKERS` | 4 | Simultaneous API-to-S3 transfers; extra requests wait asynchronously |
| `PHOTO_UPLOAD_ABANDON_SECONDS` | 86400 | Idle time before an unsealed upload and its staging objects are discarded |
| `PHOTO_WORKER_THREADS` | 4 | Concurrent onboarding/preview jobs in the worker process |
| `PHOTO_UPLOAD_PART_BYTES` | 8 MiB | Memory and S3 multipart chunk size per active upload |
| `PHOTO_AI_MODEL` | `qwen3-vl:8b-instruct-q4_K_M` | Local Ollama vision model and quantization |
| `PHOTO_AI_CONTEXT_TOKENS` | 4096 | Bounded VLM context to retain GPU headroom |
| `PHOTO_AI_FACE_MAX_IMAGE_SIDE` | 2000 | Longest image edge supplied to YuNet/AdaFace |
| `PHOTO_AI_VLM_MAX_IMAGE_SIDE` | 1280 | Longest image edge supplied to Qwen |
| `PHOTO_FACE_MODEL_DIR` | sibling scanner models | Host directory mounted read-only into the AI worker |
| `PHOTO_FACE_DETECTION_THRESHOLD` | 0.8 | YuNet face detection threshold |
| `PHOTO_FACE_MATCH_THRESHOLD` | 0.4 | AdaFace centroid similarity starting point |
| `PHOTO_CORS_ORIGINS` | empty | Comma-separated browser origins allowed to call the API |
| `PHOTO_WEB_BIND` | `0.0.0.0` | Host interface for the web frontend |
| `PHOTO_WEB_PORT` | `3000` | Host port for the web frontend |
| `PHOTO_CACHE_MAX_BYTES` | `0` (50 GiB in compose) | LRU byte budget for the local preview cache; `0` disables eviction |
| `PHOTO_CACHE_EVICTION_INTERVAL` | 300 | Seconds between worker cache-eviction passes |
| `PHOTO_CACHE_EVICT_TARGET_RATIO` | 0.9 | When over budget, evict until the cache is at most budget × ratio |
| `PHOTO_POSTGRES_BACKUP_INTERVAL_SECONDS` | 3600 | Seconds between full PostgreSQL backups |
| `PHOTO_POSTGRES_BACKUP_RETENTION` | 168 | Number of backups retained in S3 |
| `PHOTO_POSTGRES_BACKUP_PREFIX` | `backups/postgres` | Backup object-key prefix |

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

1. `POST /upload-batches` creates the authoritative PostgreSQL batch and file records.
2. Each required `PUT` streams through the API into an immutable multipart object under `incoming/`. Only `PHOTO_UPLOAD_WORKERS` transfers run at once; additional HTTP requests wait on an asynchronous semaphore, so they do not occupy worker threads.
3. `POST /upload-batches/{id}/seal` creates PostgreSQL onboarding jobs.
4. The worker thread pool claims jobs with PostgreSQL row locking and leases. Each job verifies the staged bytes, runs the reusable metadata processing stage, stores immutable originals, commits the asset to PostgreSQL, and removes its staged copy.
5. The same worker pool runs explicitly queued processing stages and then generates previews. Processing can be safely queued again as extractors improve.
6. Asset creation also adds an `ai-v1` job. The independent AI worker waits for a usable preview, then runs YuNet/AdaFace and Qwen sequentially. Upload completion never waits for this queue.

PostgreSQL preserves queue state across service restarts. A staged or final object written immediately before a process failure is safely reused on retry because object keys are immutable and bytes are verified. Recovery after PostgreSQL loss uses a database backup; uploads newer than the restored backup may need to be resubmitted.

Unsealed batches with no activity for `PHOTO_UPLOAD_ABANDON_SECONDS` are automatically removed from PostgreSQL and S3 staging. The web queue's **Dismiss** action performs the same cleanup immediately after active transfers stop. Sealed batches are never removed by this cleanup, including failed onboarding batches that remain available for retry.

## API

Use `/docs` for the full schemas.

| Endpoint | Purpose |
|---|---|
| `POST /upload-batches` | Declare a complete batch and receive per-file upload URLs |
| `GET /upload-batches` | List active and failed batches for queue recovery after a page reload |
| `PUT /upload-batches/{batch}/files/{file}` | Stream one required file to durable S3 staging |
| `POST /upload-batches/{batch}/seal` | Close uploads and enqueue background onboarding |
| `GET /upload-batches/{batch}` | Inspect file/job status and results |
| `DELETE /upload-batches/{batch}` | Discard an inactive, unsealed batch and its staged data |
| `POST /upload-batches/{batch}/retry` | Requeue failed onboarding jobs |
| `GET /upload-queue` | Inspect active/waiting transfers and durable job counts |
| `GET /assets?limit=100&offset=0` | List paginated asset records |
| `GET /library/assets` | Search/filter the capture-time timeline with cursor pagination |
| `GET /assets/{id}` | Get a manifest plus processing, preview, and current AI-analysis status/results |
| `POST /processing` | Queue metadata processing for one, many, or all active assets |
| `POST /analysis` | Queue or requeue local AI analysis for one, many, or all active assets |
| `POST /assets/{id}/analysis/retry` | Explicitly request analysis again for one photograph |
| `GET /people` and `GET /people/{id}` | Browse detected face groups and review every current face assignment |
| `PATCH /people/{id}` | Name or unname a detected person group |
| `POST /people/{id}/merge` | Combine a split face group into another person |
| `POST /faces/move` | Move selected faces to an existing or new group |
| `GET /faces/{id}/thumbnail` | Get a local cropped face thumbnail from the photo preview cache |
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
| `POST /maintenance/verify?full=true` | Verify PostgreSQL's referenced S3 originals |

## Browse the library

Open `http://SERVER_IP:3000/`. The responsive browser UI includes:

- A dense, virtualized capture-time timeline grouped by month, with adjustable thumbnail size and persistent workspace layout. Photos without a usable capture time use their import time and are identified in the interface.
- Incremental loading with stable cursor pagination, newest/oldest sorting, and filters for date, media format, minimum rating, and favorites.
- Case-insensitive literal search across original metadata and AI summaries, photo types, scenes, objects, activities, tags, and visible text.
- Lazy, bounded thumbnail loading and explicit pending, unavailable, and failed preview states.
- A routed loupe workspace with zoom, a filmstrip, camera/exposure information, all recorded EXIF, original download, arrow-key navigation, `F` for favorite, and `0`–`5` for ratings.
- A face-catalog workspace with contact sheets for naming people, combining groups, and selecting incorrect detections to move into another or a new group.
- A Lightroom-style desktop shell with library and album navigation, collapsible inspector panels, and phone-specific navigation and stacked detail controls.

The photo viewer edits captions, keywords, named locations and coordinates, and album membership. The album editor supports names, descriptions, membership removal, and ordering. Album collections retain the capture-time timeline; the album editor and API expose their saved membership order. Trash hides photos from normal browsing and supports restore; it never removes originals. Trashing an album leaves its photos in the library. Membership survives photo deletion and restore.

The AI inspector shows queue/failure state, the structured description, scene and object terms, detected face/group counts, analysis time, and an explicit **Analyze again** action. Each successful run writes a new immutable artifact beneath `analysis/<asset-id>/photo-ai-v1/`; PostgreSQL atomically switches the current searchable run only after that object exists. Old artifacts remain available for recovery and audit.

### Durable mutation protocol

Every metadata, album, trash, restore, or face-review request requires a client-generated UUID `operationId` in the JSON body. Retain the same ID and body when retrying an interrupted request. For example:

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

A shared PostgreSQL advisory lock serializes metadata and album mutations. Each current snapshot and its operation-ID retry record are committed in one PostgreSQL transaction, so a failure cannot expose the state change without its retry result or vice versa.

An already committed operation returns its original result even if newer revisions exist. Reusing its ID for a different request returns HTTP 409. Fetch current state separately after retry if other changes have happened. The web frontend stores pending requests in browser local storage, scoped to the library ID, and offers **Retry save** after failures or reloads.

Asset and album snapshots, revision counters, and operation results live only in PostgreSQL. The application does not write per-asset or per-album state JSON to S3. Imported XMP files are retained unchanged as asset blobs; generated XMP remains future work.

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

## Backup, recovery, verification, and export

The `backup` Compose service immediately creates a PostgreSQL custom-format dump, repeats hourly, uploads it under `backups/postgres/`, records its SHA-256 in S3 object metadata, and retains the newest 168 backups by default. Trigger one explicitly with:

```bash
docker compose run --rm backup once
```

Restore is intentionally guarded and destructive. Stop writers, choose a backup key, and make the database name an explicit confirmation:

```bash
docker compose stop api worker backup
docker compose run --rm \
  -e PHOTO_RESTORE_CONFIRM="${POSTGRES_DB:-photo}" \
  backup restore backups/postgres/YYYYMMDDTHHMMSSZ-ID.dump
docker compose up -d api worker backup
```

The restore downloads the dump, verifies its recorded SHA-256, replaces the named database, and runs `pg_restore --exit-on-error`. These periodic full backups give a default recovery-point objective of at most one hour while the backup service and S3 are healthy. They are not continuous WAL archiving. The S3/SeaweedFS data itself still needs an independent copy or NAS snapshot policy; a database dump stored in the same S3 system does not protect against losing that system.

Verify current catalog references without changing state:

```bash
docker compose run --rm api photo-server verify --full
```

Queue expanded lens and exposure metadata extraction for existing assets. The worker performs the work asynchronously:

```bash
docker compose run --rm api photo-server refresh-metadata
# One or many assets:
docker compose run --rm api photo-server refresh-metadata --asset UUID --asset UUID
```

The equivalent API requests are:

```bash
# One or many assets
curl --fail-with-body -X POST http://SERVER_IP:8000/processing \
  -H 'Content-Type: application/json' \
  -d '{"assetIds":["ASSET_UUID"],"stages":["metadata"]}'

# Every active asset, including photos already in the database
curl --fail-with-body -X POST http://SERVER_IP:8000/processing \
  -H 'Content-Type: application/json' -d '{}'
```

Export reads PostgreSQL and verifies every downloaded original:

```bash
mkdir -p .runtime/export
docker compose run --rm --no-deps \
  -v "$PWD/.runtime/export:/export" api photo-server export /export
```

Export writes active originals to `<asset-id>/<original-filename>` plus a portable `manifest.json`, including current metadata, into an empty destination. `library-state.json` preserves album definitions, ordering, and trashed asset metadata. Add `--include-trash` to also download trashed originals. Export requires PostgreSQL because it is the source of truth, and it retains imported XMP files.

## Workers

The media worker prioritizes onboarding jobs, versioned processing stages such as `metadata-v1`, and then preview jobs. RAW previews come from ExifTool's `JpgFromRaw`, `PreviewImage`, or `ThumbnailImage` tags; no RAW rendering occurs. JPEG and HEIF originals are decoded with Pillow/pillow-heif. Camera orientation is applied to derived previews.

Previews live in a local, disposable LRU cache. Each cached set tracks its byte sizes and last access in the `preview_cache` table. When the accounted total exceeds `PHOTO_CACHE_MAX_BYTES`, the worker evicts least-recently-accessed sets until it is back at or below `PHOTO_CACHE_EVICT_TARGET_RATIO` of the budget, skipping assets with queued or running preview or AI jobs. Eviction is two-phase (row, then files) and an orphan sweep removes cache directories whose row is gone, so a crash mid-evection cannot wedge the cache. A request for an evicted preview returns `202` and regenerates it from the immutable S3 original; the originals themselves are never touched.

The AI worker has one queue consumer. YuNet detection and alignment complete first, AdaFace IR101 embeds faces on CUDA in batches of at most 32, and only then is the image submitted to Qwen through local Ollama. Qwen is restricted to one loaded model and one parallel request; requests use a 4096-token context by default (Ollama may reserve a larger internal KV allocation). The configured Q4 model plus AdaFace used about 13.1 GB together in the RTX 3090 deployment check, leaving about 11 GB free. Similarity scores are clustering heuristics rather than probabilities; `0.4` carries over the sibling scanner's reviewed starting point.

Process one queued job manually:

```bash
docker compose run --rm api photo-server worker --once
docker compose run --rm ai-worker photo-server ai-worker --once
```

## Development and verification

```bash
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.lock
.venv/bin/pip install --no-deps -e backend
.venv/bin/ruff check backend/src backend/tests
.venv/bin/pytest -q backend/tests
cd frontend/web
npm ci
npm run check
npm test
npm run build
```

Run the integration suite against the configured S3 service and local PostgreSQL:

```bash
docker compose up -d postgres
docker compose run --rm --no-deps \
  -v "$PWD/backend/tests:/app/tests:ro" -v "$PWD/backend/src:/app/src:ro" \
  -e PYTHONPATH=/app/src -e PHOTO_RUN_INTEGRATION=1 \
  api python -m pytest -q tests -p no:cacheprovider
```

The tests create random `photo-test-*` buckets and `photo_test_*` databases and clean up only those resources. They exercise multipart HTTP upload, RAW companion selection, PostgreSQL queues, immutable object writes, duplicate handling, corruption detection, database-backed export, cache reconstruction, atomic mutation rollback/retry, concurrent changes, metadata refresh, album ordering, and tombstones.

`openapi/openapi.json` is a checked-in artifact: it is a projection of `create_app()` generated by `scripts/dump_openapi.py` (no services needed), and `run_all_tests.sh` fails if it drifts from the code (`scripts/check_openapi.py`). After changing any request or response shape, run `scripts/dump_openapi.py`, review the spec diff, and commit both in the same change. Wire bytes, not just shape, are covered by the golden contract tests in `backend/tests/test_api_contract.py` (fixtures in `backend/tests/fixtures/api_golden/`): a new request section records its fixture on first run and verifies it on every later run. If a phase intentionally changes a wire format, re-record that section as a new fixture file and note it in [docs/openapi-codegen-plan.md](docs/openapi-codegen-plan.md).

The same spec drives two checked-in generated clients, each with a drift check in `run_all_tests.sh`: the web client (`frontend/web/src/api/generated`, from `@hey-api/openapi-ts`) and the Python client (`backend/src/photo_server/generated`, from `@hey-api/openapi-python`; regenerate in `backend/` with `npm run generate:api`, verify with `npm run check:api`). The `photo-upload` CLI validates every JSON exchange with the API through the generated Pydantic models, so a spec drift fails loudly in the client instead of as a `KeyError` mid-upload; the generated `Sdk`'s method stubs take no parameters (a 0.x generator limitation), so the CLI keeps a small hand-written httpx transport.

## Current boundaries

- One client/user per library is the supported V1 usage.
- V1 has no API authentication or TLS termination; keep it on a trusted LAN or place it behind a configured reverse proxy.
- Completed files and queue state survive service restarts. An individual client-to-API PUT is streamed and must restart from byte zero if its network connection fails.
- PostgreSQL is the only authoritative structured-state store. Its scheduled backups share the configured S3 failure domain unless that S3 data is independently replicated.
- Originals, imported XMP files, and completed AI run artifacts are immutable S3 blobs. Face naming/manual correction, photo editing, generated XMP, and semantic vector search remain future work.
- No automatic source-folder watcher or mass migration exists. The network client enumerates only paths explicitly provided by the user.
- One application database and one API process per library remain the supported deployment.
- No automatic garbage collection or original deletion is implemented.
- PostgreSQL backup and guarded restore are implemented and exercised. Continuous WAL archiving and an independent backup of SeaweedFS/NAS remain operational follow-up work.
