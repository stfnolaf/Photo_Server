# Web client

The web client is a TypeScript/React single-page application for organizing and viewing the Photo Server library. It is built as a static bundle and served by nginx. Browser requests use nginx's same-origin `/api` proxy by default, so the Compose deployment does not need CORS.

## Workspaces

- **Library** is a virtualized, capture-time timeline with search, filters, adjustable density, favorites, albums, selection, and trash.
- **People** is a face-catalog workspace for naming detected people, combining split groups, and moving incorrect faces into an existing or new group.
- **Photo** is a routed loupe workspace with a filmstrip, zoom, keyboard navigation, ratings, favorites, metadata, album membership, technical metadata, and original download.
- Phone layouts use compact navigation and stacked inspectors instead of shrinking the desktop sidebars.

The backend owns durable library state. TanStack Query owns cached server state, Zustand owns only local layout preferences, and URL parameters own the active collection and filters. Mutations retain the backend's operation-ID protocol: the complete pending request is written to library-scoped local storage before it is sent and remains available for retry after an interrupted response.

Source boundaries are intentionally portable:

```text
src/
├── api/          # transport contracts and durable operation journal
├── domain/       # framework-independent formatting and library behavior
├── state/        # local presentation preferences
├── hooks/        # query and URL adapters
├── components/   # shared presentation primitives
└── features/     # Library, Photo, albums, and application shell
```

A later Tauri desktop shell can serve the same Vite bundle and replace platform services such as file selection without moving domain behavior into native code. Set `VITE_API_ROOT` at build time when the client should call an absolute API origin rather than the bundled `/api` proxy.

## Development

Node.js 22.12 or newer is required; the production build uses Node.js 24.

```bash
cd frontend/web
npm ci
npm run dev
```

Vite proxies `/api`, `/docs`, and `/openapi.json` to `http://localhost:8000`. Set `PHOTO_DEV_API` to use another development backend.

Checks and production build:

```bash
npm run check
npm test
npm run build
npm audit --omit=dev
```

From the repository root, the normal deployment remains:

```bash
docker compose up --build -d web
```

Open `http://SERVER_IP:3000/`. The backend remains independently available at `http://SERVER_IP:8000/`.
