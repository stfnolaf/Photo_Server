# Session 6: Storage reporting and capacity safety

Work in `/home/stephen/dev/photo_server`.

Implement read-only storage reporting and capacity warnings. Do not enable
automatic original deletion or broad garbage collection in this session.

## Context

The project has immutable originals, staging objects, local preview-cache
eviction, AI artifacts, backups, and a full storage verification command. The
current documented boundary is that automatic garbage collection and original
deletion are not implemented.

## Requirements

- Add a read-only CLI command such as:

  `photo-server storage report`

- Report, where practical:
  - missing referenced originals;
  - unreferenced original objects;
  - stale upload staging;
  - orphaned preview directories;
  - preview rows without files and files without rows;
  - unreferenced AI artifacts;
  - backup objects outside retention;
  - local disk usage by category.
- Support human-readable and JSON output.
- Add configurable capacity thresholds for free disk, staging, cache, and
  backup age.
- Include warnings in operational health/status and logs.
- Reject or pause new uploads only when a clearly documented hard safety
  threshold is crossed.
- Keep report generation bounded and avoid downloading every original unless
  explicitly requested by a full verification mode.

## Safety requirements

- The first implementation must be read-only.
- Do not delete originals, database rows, or arbitrary object-store keys.
- Make any future cleanup candidates explainable by reason and source record.
- Avoid treating temporary upload state as orphaned while an active lease exists.

## Verification

- Add fixtures for missing objects, orphaned objects, stale staging, and cache
  inconsistencies.
- Test threshold warnings and hard upload refusal behavior.
- Test JSON output stability.
- Test report behavior when object storage or the database is unavailable.
- Run backend tests, lint, and operational health checks.

## Stop condition

End with a trusted report-only tool and documented cleanup candidates. A later
session may add explicitly confirmed cleanup after real-world reports have been
reviewed.
