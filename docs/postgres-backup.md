# PostgreSQL backup destinations

The backup container creates one verified custom-format dump and attempts the
primary S3-compatible destination and the optional secondary destination
independently. The secondary can be another S3-compatible endpoint or an
explicitly mounted filesystem/NAS path. Each destination verifies the uploaded
or copied bytes against the dump SHA-256 before retention pruning runs for
that destination.

The container writes a redacted status document to
`PHOTO_POSTGRES_BACKUP_STATUS_PATH`. Compose shares this file with the API,
which exposes `postgresBackupPrimary`, `postgresBackupSecondary`, and
`postgresBackupOverall` in `GET /health`, while retaining the existing
`postgresBackupKey` and `postgresBackupAt` fields for compatibility.

Failure semantics:

- Primary failure is always `degraded`; a secondary success cannot make the
  backup healthy because the primary is the normal restore source.
- If a secondary is configured and `PHOTO_POSTGRES_BACKUP_SECONDARY_REQUIRED`
  is `true`, secondary failure is `degraded` even when primary succeeds.
- If the secondary is optional, its failure remains visible in
  `postgresBackupSecondary`, but a verified primary succeeds overall.
- A missing backup, checksum mismatch, destination error, or required
  secondary failure is reported without restoring a database or deleting photo
  originals. Retention only removes old backup artifacts in the destination
  whose retention policy was invoked.

The scheduled loop records a failed/degraded attempt and continues retrying.
The `once` command returns non-zero for a non-healthy result so operators and
job runners can alert on it. Status fields contain no credentials, signed URLs,
or provider error text.
