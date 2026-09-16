# Backend

The backend contains the Python package, FastAPI service, upload client, worker, PostgreSQL catalog, S3 storage integration, and backend tests.

Build and run it from the repository root with Docker Compose, or install it locally:

```bash
python3 -m venv ../.venv
../.venv/bin/pip install -r requirements.lock
../.venv/bin/pip install --no-deps -e .
```

The API listens on port 8000 in its container. Its root redirects to `/docs`; frontends consume its JSON and media endpoints independently.

PostgreSQL schema changes live in `src/photo_server/db_migrations/*.sql`. Files are consecutively numbered, idempotent, checksummed after application, and run transactionally under an advisory lock. `migrations.py` executes them; `catalog.py` contains query mappings but no schema creation or inline migration DDL. Create a new numbered SQL file for a schema change and never modify an applied migration. Run `photo-server migrate` to apply pending SQL and reconcile S3 state before starting traffic.

Phase 3 mutations go through `state.py`: reconcile, create and verify an immutable S3 revision, update the catalog, and acknowledge. Every API metadata/album/trash request requires an `operationId`; clients retain it across retries. The PostgreSQL schema upgrade is additive, and startup migrates Phase 2 ratings/favorites before accepting requests. See the root README for the upgrade sequence and API contract.
