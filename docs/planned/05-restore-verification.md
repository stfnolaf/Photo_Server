# Session 5: Restore verification and recovery runbook

Work in `/home/stephen/dev/photo_server`.

Implement disposable restore verification and document the recovery exercise.
Do not modify the live database automatically and do not add destructive
storage cleanup in this session.

## Context

The project already has guarded restore support and full storage verification.
What is missing is a repeatable check that a backup can actually be restored
and queried before an incident.

## Requirements

- Add a manual CLI command such as `photo-server backup verify`.
- Select the newest valid backup by default, with an explicit backup-key option.
- Verify the recorded checksum before restore.
- Restore into a disposable PostgreSQL database or isolated database name.
- Run migration/schema compatibility checks and representative catalog queries.
- Verify that expected tables, migration ledger, library metadata, and core
  counts are readable.
- Clean up the disposable database even when verification fails, where safe.
- Record verification timestamp, selected backup, checksum result, restore
  result, and query result without leaking secrets.
- Expose the latest verification result in operational health/status.
- Add a recovery runbook covering database loss, object-storage loss, and
  combined recovery using independent backups.

## Safety requirements

- Require an explicit non-live verification target.
- Refuse to run against the configured production database.
- Do not stop or mutate the live API automatically.
- Do not remove source backups after verification.

## Verification

- Test checksum failure.
- Test invalid/missing backup.
- Test refusal to target the live database.
- Test successful disposable restore and query checks.
- Test cleanup after failure.
- Run the command against a disposable Compose database if available.
- Update operator documentation with measured recovery time.

## Stop condition

Finish with a tested recovery procedure and explicit RPO/RTO observations. Do
not claim disaster recovery is complete until the runbook has been exercised.
