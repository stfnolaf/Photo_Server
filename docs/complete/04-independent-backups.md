# Session 4: Independent backup destination

Work in `/home/stephen/dev/photo_server`.

Add a second backup destination and backup health reporting. Do not implement
restore verification or destructive cleanup in this session.

## Context

The existing backup service creates PostgreSQL custom-format dumps, checksums
them, uploads them to configured object storage, and retains old backups. The
database backups currently share the primary object-storage failure domain with
the photo originals.

Relevant areas include:

- `ops/postgres-backup/backup.sh`
- `ops/postgres-backup/Dockerfile`
- `compose.yaml`
- `.env.example`
- `backend/src/photo_server/config.py`
- `backend/src/photo_server/service.py`

## Requirements

- Add a second destination abstraction, supporting either a second
  S3-compatible endpoint or an explicitly mounted filesystem/NAS destination.
- Keep the primary destination behavior backward compatible.
- Add configuration for endpoint/path, bucket, credentials, retention, and
  whether secondary success is required for an overall healthy backup.
- Upload or copy the same verified dump to both destinations.
- Verify checksums at each destination.
- Report primary, secondary, and overall backup state separately through the
  existing operational health/status mechanism.
- Never log credentials or full signed URLs.
- Update Compose and `.env.example` with safe, commented examples.

## Verification

- Test primary success/secondary success.
- Test primary failure/secondary success.
- Test secondary failure/primary success and degraded status.
- Test checksum mismatch and retention behavior independently.
- Preserve current guarded restore behavior.
- Run backup tests, backend tests, lint, and any OpenAPI checks required by
  response changes.

## Stop condition

Do not automatically restore databases or delete originals. End with documented
failure semantics and a clear explanation of when backup health is degraded.
