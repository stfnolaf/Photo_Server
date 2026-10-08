# Backend

The backend contains the Python package, FastAPI service, upload client, media and AI workers, PostgreSQL catalog, S3 storage integration, and backend tests.

Build and run it from the repository root with Docker Compose, or install it locally:

```bash
python3 -m venv ../.venv
../.venv/bin/pip install -r requirements.lock
../.venv/bin/pip install --no-deps -e .
```

The API listens on port 8000 in its container. Its root redirects to `/docs`; frontends consume its JSON and media endpoints independently.

PostgreSQL schema changes live in `src/photo_server/db_migrations/*.sql`. Files are consecutively numbered, idempotent, checksummed after application, and run transactionally under an advisory lock. `migrations.py` executes them; `catalog.py` contains query mappings but no schema creation or inline migration DDL. Create a new numbered SQL file for a schema change and never modify an applied migration. Run `photo-server migrate` to apply pending SQL before starting traffic.

S3 is authoritative for content-addressed media and immutable revisioned manifests. Mutations go through `state.py`, publish and verify the S3 revision first, then update PostgreSQL's rebuildable query projection. Fingerprints use immutable per-asset records, while revisioned full burst snapshots preserve exact cluster membership, identity, exclusions, and representative overrides. PostgreSQL also owns operational queues, leases, upload sessions, and retry receipts. Every API metadata/album/trash request requires an `operationId`; clients retain it across retries. The versioned processing registry is shared by onboarding and worker reruns. `POST /processing` queues metadata extraction, while `POST /analysis` and `photo-server analyze` queue face and semantic analysis on the `ai-worker` service (a plain HTTP client for the VLM and the standalone face-service). See the root README for GPU/model setup, recovery, and API contracts.
