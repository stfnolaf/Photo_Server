# PostgreSQL-authority verification — 2026-09-16

## Delivered behavior

PostgreSQL is the single source of truth for asset records, extracted and user metadata, albums, tombstones, operation retries, and queues. Metadata and album mutations update their current snapshot and operation result in one database transaction. S3 stores immutable originals, imported XMP sidecars, staging objects, and database backups; new imports and edits do not create `state/assets`, `state/albums`, declaration, seal, or receipt JSON objects.

The Compose `backup` service creates an immediate PostgreSQL custom-format dump, uploads it to `backups/postgres/` with SHA-256 object metadata, repeats hourly, and retains 168 backups by default. Restore requires an explicit database-name confirmation, verifies the dump checksum, recreates the named database, and runs `pg_restore --exit-on-error`. The default recovery-point objective is one hour, not zero; continuous WAL archiving remains future work.

ExifTool ingestion now requests descriptive maker-specific lens fields and normalized aperture, focal length, 35mm-equivalent focal length, ISO, exposure time/shutter speed, exposure compensation, exposure program, metering, flash, and white balance. Search and cards use the normalized lens name, and the viewer displays the technical fields while retaining the complete extracted map. Onboarding and reruns share a versioned processing-stage implementation. `POST /processing` and `photo-server refresh-metadata` queue one, many, or all existing assets for worker reprocessing from their immutable originals.

## Automated verification

**85 tests passed** in the production application image against PostgreSQL, the configured S3 service, and ExifTool. Coverage includes:

- rollback of both the state change and retry record after a simulated mid-transaction failure;
- operation replay and global operation-ID conflict detection;
- concurrent patches without lost fields;
- imports, sidecars, multipart upload, job leases, immutable-object retry, and duplicate detection without S3 state JSON;
- Phase 2 rating/favorite promotion wholly inside PostgreSQL;
- lens/exposure metadata normalization, refresh, search, API output, and viewer data;
- albums, ordered membership, trash/restore, previews, storage verification, and database-backed portable export;
- migration continuity, checksums, adoption, and idempotence through schema version 4.

The backup image built successfully. A real dump was uploaded and checksum-verified, then restored into a disposable PostgreSQL database and queried successfully before that test database was removed. The active development database was not replaced.

## Remaining operational boundary

The PostgreSQL backup is stored in the same S3 system as the media by default. This protects against loss of the PostgreSQL volume or application server, but not loss of the NAS/S3 system or credentials capable of deleting both. An independent S3/NAS backup remains required for that failure mode. Full backups are periodic; WAL archiving or synchronous replication is required for a smaller recovery-point objective.
