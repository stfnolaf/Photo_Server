# Backend

The backend contains the Python package, FastAPI service, upload client, worker, PostgreSQL catalog, S3 storage integration, and backend tests.

Build and run it from the repository root with Docker Compose, or install it locally:

```bash
python3 -m venv ../.venv
../.venv/bin/pip install -r requirements.lock
../.venv/bin/pip install --no-deps -e .
```

The API listens on port 8000 in its container. Its root redirects to `/docs`; frontends consume its JSON and media endpoints independently.

PostgreSQL schema changes live in `src/photo_server/db_migrations/*.sql`. Files are consecutively numbered, idempotent, checksummed after application, and run transactionally under an advisory lock. `migrations.py` executes them; `catalog.py` contains query mappings but no schema creation or inline migration DDL. Create a new numbered SQL file for a schema change and never modify an applied migration. Run `photo-server migrate` to apply pending SQL before starting traffic.

PostgreSQL is authoritative for all structured library state. Mutations go through `state.py` and `Catalog.commit_mutation`, which commit the current snapshot and operation retry result atomically. S3 contains immutable media, imported sidecars, staging objects, and PostgreSQL backups; no asset/album state JSON is written. Every API metadata/album/trash request requires an `operationId`; clients retain it across retries. The versioned processing registry is shared by onboarding and worker reruns. `POST /processing` and `photo-server refresh-metadata` queue lens and exposure extraction for one, many, or all existing assets. See the root README for backup, restore, upgrade, and API contracts.
