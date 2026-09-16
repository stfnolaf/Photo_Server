# Phase 1 verification — 2026-09-15

> Historical report: database-free recovery described here was superseded by the [PostgreSQL-authority decision](postgres-authority-verification.md).

## Automated tests

**27 passed:** 14 unit tests and 13 integration tests using the actual SeaweedFS endpoint, disposable PostgreSQL databases, and ExifTool in Docker.

Verified:

- Environment-based credentials, database password encoding, masked settings representations, and loading S3 credentials from `.env`.
- RAW preference within one folder/batch, including input order, extension case, ambiguous candidates, and XMP attachment.
- Explicit file/count limits and rejection of source symlinks outside the configured import root.
- SeaweedFS rejects an attempted overwrite with `If-None-Match: *` and retains the first object's bytes.
- SHA-256 deduplication, replay of operation IDs, and rejection of changed content on retry.
- Recovery after failure between original storage and manifest commit, and between manifest commit and database indexing.
- Detection of same-size object corruption during full recovery.
- An unsupported newer manifest causes an error without silently using the older revision.
- Standalone export with an unreachable PostgreSQL URL.
- Preview cache reconstruction, standalone HEIF decoding, and API asset-download behavior.
- Complete network-batch declaration, RAW companion suppression before transfer, and HTTP-to-S3 multipart upload.
- Durable upload/onboarding recovery into a fresh PostgreSQL database after staged objects have been cleaned up.
- Concurrent claiming and completion of six onboarding jobs without duplicate claims or a stale batch status.

Test buckets/databases were randomly named and removed after the tests. The development library was not cleared. Two dependency deprecation warnings originated in Starlette's test client; they did not affect the checks.

## Real sample verification

Imported exactly **three existing source photos** (two Sony ARWs and one JPEG), totaling **64,719,679 bytes**, into `photo-library`.

- Recorded source SHA-256 values before importing; re-read all three sources afterward and confirmed identical hashes and sizes.
- Verified uploaded object bytes against the source hashes before committing manifests.
- Extracted embedded previews from both RAWs and generated thumbnails/previews for all three originals.
- Retrieved all six derived images over the running HTTP API; inspected both RAW thumbnails visually.
- Recovered the three complete manifests and their original hashes into a separate, initially empty PostgreSQL database. The development database remained intact.
- Exported all three assets directly from S3 with an intentionally unreachable database URL and verified hashes.
- Used a temporary copy of one RAW plus a generated same-stem JPEG to verify RAW preference and duplicate reuse. This added no new asset/original to the development library; the temporary files were removed.
- Confirmed the later network-upload deployment removes the NAS source mount entirely.

No directory tree was imported or migrated. The development library contains three assets and three original blobs. Local test exports and the temporary recovery database were removed after verification.

## Running services

- Interactive API: `http://192.168.8.180:8000/docs`
- Health and upload queue: `http://192.168.8.180:8000/health`
- One API container with four bounded upload transfers, one four-thread onboarding/preview worker, and PostgreSQL, managed by Docker Compose.

This demonstrates application-catalog recovery with the NAS available. Recreating SeaweedFS containers against the existing filer/volume persistence paths remains a deployment test to perform on the NAS itself. No NAS administration was attempted.
