# Web frontend

The web frontend is a standalone static application served by nginx. It sends API and media requests through nginx's `/api` proxy to the Compose `api` service, so browser requests stay on one origin and do not require CORS configuration.

From the repository root:

```bash
docker compose up --build -d web
```

Open `http://SERVER_IP:3000/`. The backend remains independently available at `http://SERVER_IP:8000/`.

The source files are in `src/`; there is no generated bundle or checked-in dependency tree.

The viewer edits durable metadata and album membership. Albums support ordering, names/descriptions, trash, and restore; photos have their own trash view. Pending mutation requests are kept in browser local storage, scoped to the library ID, so **Retry save** uses the same operation ID after a lost response or reload.
