# Backend

The backend contains the Python package, FastAPI service, upload client, worker, PostgreSQL catalog, S3 storage integration, and backend tests.

Build and run it from the repository root with Docker Compose, or install it locally:

```bash
python3 -m venv ../.venv
../.venv/bin/pip install -r requirements.lock
../.venv/bin/pip install --no-deps -e .
```

The API listens on port 8000 in its container. Its root redirects to `/docs`; frontends consume its JSON and media endpoints independently.
