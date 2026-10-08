# Session 5: S3 recovery verification and compatibility contract

Work in `/home/stephen/dev/photo_server`.

Verify that the photo library can be recovered from an application-consistent
S3 snapshot or offline S3 backup by rebuilding PostgreSQL from the canonical
S3 state. PostgreSQL dumps are optional acceleration artifacts; they are not
the authority for user-visible library state.

Do not modify the live database or production S3 data automatically. Use a
disposable S3 destination and disposable PostgreSQL database for the exercise.

## Context

S3 is the authoritative store for content-addressed photo objects and
immutable, revisioned library manifests. PostgreSQL is a rebuildable query
projection and operational store. The S3 data is protected by ZFS snapshots
and offline backups, so the primary recovery question is whether a restored
S3 state can be interpreted by a known-compatible software release and
reconstructed successfully.

The existing format markers are useful but incomplete for recovery identity:

- `library.json` records the library identity and S3 schema version;
- canonical manifests record their individual `schemaVersion`;
- recovery checkpoints record manifest versions and a tool version;
- PostgreSQL migrations record their numbered version and SQL checksum.

The recovery record must connect these pieces to an exact software release.

## Requirements

- Define a durable recovery-compatibility record for each S3 snapshot or
  recovery checkpoint. It must identify:
  - application release/version;
  - Git commit and container/image digest;
  - dependency or lockfile identity;
  - S3 layout and manifest schema versions;
  - PostgreSQL migration head, if a projection is present;
  - relevant authority mode and feature flags;
  - source snapshot/checkpoint ID and creation time;
  - minimum software version that can rebuild or read the snapshot.
- Record the compatibility metadata atomically with the snapshot/checkpoint,
  or fail closed if the metadata cannot be recorded.
- Define compatibility epochs:
  - additive manifest changes remain backward-readable;
  - incompatible changes increment the format/epoch version;
  - readers and rebuilders explicitly reject unsupported versions;
  - transitions use dual-read/dual-write or a tested conversion tool;
  - old formats are retired only after the documented support window.
- Make the rebuild command report the detected S3 format, required software
  version, and compatibility decision before writing PostgreSQL.
- Keep PostgreSQL dump restore as an optional fast path, with checksum
  verification, but do not make it a prerequisite for durable recovery.

## Verification exercise

- Create or select an application-consistent S3 snapshot/checkpoint and record
  its compatibility metadata.
- Restore it into an isolated S3 destination.
- Run the rebuild using the recorded, known-compatible release.
- Verify the library identity, all supported manifest kinds, canonical object
  checksums, asset and album counts, filenames, metadata, people/faces,
  tombstones, permissions, and revision relationships.
- Verify that derivable queues and caches are recreated or intentionally
  omitted, and that no required state depends on PostgreSQL-only records.
- Test a refused restore with an unsupported future format version.
- Test the documented upgrade path for at least one compatible format change.
- Exercise browsing, previews, downloads, processing, exports, and metadata
  mutations against the rebuilt projection.
- Record measured recovery point objective, recovery time objective, restore
  size, rebuild duration, software identity, and any lost operational state.
- Preserve the exercise report with the snapshot/checkpoint identifier.

## Operational policy

Maintain a compatibility matrix mapping S3 format epochs to supported release
tags or image digests. Every change to manifest shape, object layout, rebuild
logic, or authority semantics must update that matrix and add a recovery
fixture before deployment.

Keep deployment configuration, secrets, migration files, lockfiles, and image
references in a separately recoverable location. They are not assumed to be
reconstructable from photo objects alone.

## Stop condition

Finish with a tested S3-first recovery procedure, an explicit compatibility
record, and measured RPO/RTO observations. Do not claim disaster recovery is
complete until the procedure has been exercised from a snapshot or offline
backup and the exact compatible software release has been identified.

## Verification record — 2026-10-08 (UTC)

The disposable recovery exercise is complete. It creates an immutable
checkpoint, restores it into an isolated S3 bucket, restores the optional
PostgreSQL dump, rebuilds a fresh PostgreSQL projection from S3, and verifies
the rebuilt projection against the source. Derived fingerprint and AI queues
are recreated; upload sessions and leases are intentionally not.

The checkpoint now records application release, minimum reader version, Git
commit, image digest, dependency-lock checksum, S3 format and manifest schema
versions, PostgreSQL migration head, authority mode, and checkpoint identity
when supplied by the deployment environment. Unsupported S3 format versions
fail closed.

The focused unit/rebuild/recovery suite passed **19 tests**, and the disposable
Compose recovery exercise passed **1 test in 16.41 seconds**. No live database
or production S3 data was modified. The fixture RTO is approximately 16
seconds; production RPO is the interval between application-consistent S3
snapshots or offline copies, and production RTO depends on restore and rebuild
throughput.
