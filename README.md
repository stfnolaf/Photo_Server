# Photo Server

Phase 1 of the [system design](self_hosted_photo_organizer_design.md): a Python/FastAPI service, PostgreSQL catalog, and S3 originals with durable JSON manifests. A Python worker extracts embedded RAW previews and generates JPEG thumbnails. There is an interactive API, but no photo-library UI yet.

See the [verification report](docs/verification.md) for automated checks and the three-photo test against the NAS.

## Start

Docker and Docker Compose are sufficient; ExifTool and Python dependencies are installed in the application image.

```bash
cp -n .env.example .env
chmod 600 .env
```

Set the S3 endpoint, source directory, and database password in `.env`, then start:

```bash
docker compose up --build -d
```

- API and interactive documentation: **http://localhost:8000/docs**
- Health and asset counts: **http://localhost:8000/health**
- S3 endpoint: `PHOTO_S3_ENDPOINT` in `.env`; bucket `photo-library`, unsigned requests by default.
- PostgreSQL: `localhost:55432`; credentials and database name come from `POSTGRES_USER`, `POSTGRES_PASSWORD`, and `POSTGRES_DB` in `.env`.
- Source directory: `PHOTO_IMPORT_ROOT` in `.env`, mounted **read-only** at `/imports` inside the API and worker.
- PostgreSQL and preview caches use separate named Docker volumes. S3 holds originals, manifests, and import receipts.

`docker compose down` stops services while retaining these volumes. Avoid `down -v` unless intentionally discarding the local catalog and cache.

The API and worker initialize the library if needed and reconcile existing S3 manifests at startup. They share a database lock for imports/recovery. Run one API worker and one background worker for V1.

### Configuration and credentials

`.env` is local and ignored by Git and Docker builds; `.env.example` contains placeholders suitable for committing. Keep a private copy of your deployment settings outside Git.

Python builds the database URL from the PostgreSQL settings, including proper password encoding. Compose supplies the internal database host/port; host-side CLI commands use `PHOTO_POSTGRES_HOST` and `PHOTO_POSTGRES_PORT`. `PHOTO_DATABASE_URL` can override the assembled URL for host-side commands or an explicit `docker compose run -e PHOTO_DATABASE_URL=...` invocation.

Existing PostgreSQL volumes keep their initialized credentials; use the matching password in `.env`. For authenticated S3, set `PHOTO_S3_ANONYMOUS=false` and provide AWS credentials in `.env` or through the standard AWS credential chain. Session tokens are supported.

## Import an explicit sample batch

The CLI accepts individual files, relative to the import root or as absolute paths inside it. **It does not recurse into directories.** The default limit is 25 input files and 512 MiB per file, configurable in `.env`.

First inspect selection without reading media contents or writing to S3:

```bash
docker compose run --rm api photo-server plan \
  'trip/DSC01234.ARW' 'trip/DSC01234.HEIF' 'trip/DSC01234.xmp'
```

Then import the same explicit files:

```bash
docker compose run --rm api photo-server import \
  --operation-id 34f853cf-d3be-4f9f-9292-804243e1c83c \
  'trip/DSC01234.ARW' 'trip/DSC01234.HEIF' 'trip/DSC01234.xmp'
```

These are illustrative paths. Generate a new operation ID for a new batch (`python3 -c 'import uuid; print(uuid.uuid4())'`), or omit the option and retain the ID the CLI prints. **Reuse the same ID and file list to retry an interrupted import.** Reusing it with different content fails instead of overwriting stored originals.

Selection rules:

- Exactly one RAW with the same stem in the same folder takes precedence over listed JPEG/HEIF companions, regardless of input order.
- Include all candidate representations in the same batch. The importer does not discover unlisted companions or match across batches.
- Multiple RAW candidates are reported as ambiguous and imported independently. Without RAW, JPEG/HEIF originals are imported independently.
- One same-stem XMP attaches to one selected media original. Ambiguous/orphan sidecars are reported and left unassigned.
- Exact media duplicates return the existing asset. A duplicate carrying a new/different sidecar fails with an explanation; merging existing metadata is future work.
- Skipped sources remain untouched. If RAW import fails, companions are reported as deferred. A failure stops subsequent assets in the batch; retry with its operation ID.

Each selected file is streamed into local scratch while hashing, uploaded with create-only semantics, then downloaded and checked against its SHA-256 before its manifest is committed. The source is opened for reading only. Peak scratch usage is bounded by one asset and its sidecar. The additional verification download is intentional for this first implementation.

## API

Use `/docs` to construct requests. Phase 1 imports files already accessible under the source mount; browser file upload is deferred.

| Endpoint | Purpose |
|---|---|
| `POST /imports/plan` | Select files; body: `{"paths": ["trip/DSC01234.ARW"]}` |
| `POST /imports` | Import; same body plus a caller-generated `operation_id` UUID |
| `GET /assets?limit=100&offset=0` | Paginated manifests |
| `GET /assets/{id}` | Manifest and preview-job status |
| `GET /assets/{id}/original` | Stream the original |
| `GET /assets/{id}/preview` | JPEG preview, or `202` while pending |
| `GET /assets/{id}/thumbnail` | JPEG thumbnail, or `202` while pending |
| `POST /assets/{id}/preview/retry` | Retry a failed/unavailable preview job |
| `POST /maintenance/reconcile?verify=true` | Reconcile S3 state and verify original hashes |

Batch responses contain individual `imported`, `duplicate`, `failed`, or `not_attempted` results. Inspect these statuses even when the HTTP response is 200. The CLI exits nonzero on a failed/partial batch.

## Recovery and export

Rebuild or reconcile the catalog from S3:

```bash
docker compose run --rm api photo-server recover --verify
```

Without `--verify`, recovery checks each blob's existence and size. With it, recovery downloads and hashes every referenced blob. Both modes validate manifest schemas, ownership, and references. Unsupported newest revisions cause errors; recovery does not silently fall back to older revisions. Good assets can still be recovered when another asset has an integrity error; the report and exit code identify incomplete recovery.

To use a fresh database, provide its URL through `PHOTO_DATABASE_URL` when running the CLI. Recovery binds the database to the S3 library ID and refuses to mix libraries. The automated integration tests demonstrate recovery into separate, newly created databases; they do not erase the development catalog.

Export works **without PostgreSQL** and verifies each original:

```bash
mkdir -p .runtime/export
docker compose run --rm --no-deps \
  -v "$PWD/.runtime/export:/export" api photo-server export /export
```

Export writes `<asset-id>/<original-filename>` plus `manifest.json` into an empty destination. It retains original XMP files and reports failures rather than overwriting existing destination files. Generating interoperable XMP from user metadata is future work.

## Preview worker

The worker checks the PostgreSQL jobs table. RAW previews come from ExifTool's `JpgFromRaw`, `PreviewImage`, or `ThumbnailImage` tags; no RAW rendering occurs. JPEG and HEIF originals are decoded with Pillow/pillow-heif. Camera orientation is applied to derived previews.

Jobs expose `pending`, `running`, `ready`, `unavailable`, or `failed` status. No usable embedded RAW image produces `unavailable`. A worker interruption leaves a job reclaimable after its 10-minute lease. Missing caches are queued for regeneration when requested.

Process one pending job manually:

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

`requirements.lock` pins the currently tested runtime and development dependencies. Host-side imports require ExifTool installed separately; the Docker image includes it.

Run the integration suite against the configured S3 service and local PostgreSQL:

```bash
docker compose up -d postgres
docker compose run --rm --no-deps \
  -v "$PWD/tests:/app/tests:ro" -v "$PWD/src:/app/src:ro" \
  -e PYTHONPATH=/app/src -e PHOTO_RUN_INTEGRATION=1 \
  api python -m pytest -q tests -p no:cacheprovider
```

These tests create random `photo-test-*` buckets and `photo_test_*` databases, populate them with tiny generated fixtures, and clean up only those test resources. They exercise conditional writes, duplicate/retry handling, crash recovery, corruption detection, standalone export, API requests, and cache reconstruction.

## Current boundaries

- Immutable revision-1 manifests and original imports are implemented. Ratings, albums, people, and user-state revisions remain in later phases.
- One client/user per library; no automatic source-folder watcher or bulk migration.
- Recovery currently enumerates manifests before each import for correctness. Incremental reconciliation can follow measurements on a larger library.
- Original uploads use single-object PUT, bounded by the configured file-size limit. Multipart/resumable client uploads are deferred.
- Orphan originals from an interrupted import can be reused by retrying its operation. No automatic garbage collection or original deletion is implemented.
- Application-server recovery is implemented. SeaweedFS container recreation and persistence of its filer/volume data must be verified on the NAS deployment; this repo does not administer that deployment.
