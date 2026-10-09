# Preview derivative pipeline

Status: Planned.

Design the preview pipeline around the way the web application actually
displays photographs. Fast browsing should not wait for a high-quality
dedicated-view render, and face thumbnails should not implicitly depend on the
same expensive derivative as the full photograph viewer.

## Context

The current preview worker treats preview generation as one job. A job reads
the original (or an embedded RAW preview), applies orientation, and writes both
`thumbnail.jpg` and `preview.jpg` into one asset cache directory. The API has
one `preview-v1` job state, and a missing derivative queues that same job.

The frontend uses these derivatives for materially different purposes:

- library cards need a small image immediately;
- the photo filmstrip needs the same small image;
- face groups need a face crop with enough source resolution to remain useful;
- the dedicated photo view needs a larger, higher-quality image;
- downloads need the original, not a derived preview.

The current design makes a cold thumbnail request pay for the larger preview
path as well. The frontend also receives `202 Pending` and waits according to
the retry delay before checking again. With a spinning HDD, the combined
queue, polling, RAW extraction, and JPEG work is visible as a multi-second
interaction even when the final derivative only takes about a second to make.

## Goals

- Make library and filmstrip images appear as quickly as possible.
- Keep high-quality rendering out of the critical path for browsing.
- Give face crops an independent source and queue priority.
- Preserve the immutable originals and canonical S3 authority.
- Make cache hits, cold renders, queue delay, and storage time measurable
  separately.
- Allow preview worker concurrency to be tuned per derivative class.
- Make derivative URL changes invalidate stale browser and proxy caches.

## Non-goals

- Replacing the original RAW/JPEG files.
- Making RAW files directly viewable in the browser.
- Rebuilding face detection or changing face assignments.
- Adding a CDN or requiring network storage.
- Automatically deleting existing cache data during the first rollout.

## Derivative classes

### `grid`

The default library derivative. It is used by library cards and the photo
filmstrip. It should be generated from the fastest usable source, normally the
embedded JPEG/preview in a RAW file, and resized to approximately 256–320px on
the long edge.

Suggested defaults:

- JPEG, quality  seventy-five to eighty;
- 320px long edge;
- orientation applied;
- no expensive sharpening or high-quality upscale;
- highest queue priority;
- generated during import when practical, otherwise generated on first view.

The filmstrip should reuse this derivative rather than create a second class.

### `face-source`

An intermediate source for face crops. It should be large enough that a crop
can be reduced to the face-card display size without looking soft, but should
not be the same size or quality as the dedicated photo view.

Suggested defaults:

- 768–1024px long edge;
- JPEG, quality  eighty to eighty-five;
- orientation applied;
- lower priority than `grid`;
- generated lazily when the first face crop is requested, or opportunistically
  after `grid` generation if the asset has detected faces.

The face-thumbnail endpoint should use `face-source`. It must not queue or
wait for `detail`.

### `detail`

The dedicated photo-view derivative. It is requested only when the user opens a
photograph or explicitly zooms into it.

Suggested defaults:

- 2048–2560px long edge initially;
- JPEG, quality  ninety to ninety-two;
- orientation and color handling preserved;
- normal or low queue priority;
- generated on demand, with optional background warming for adjacent photos.

The exact size should be benchmarked against the largest supported display and
the cost of decoding the embedded RAW preview. The first implementation can
retain the current 2560px behavior under a new derivative identity.

### `original`

The immutable original remains the download/export path. It is not a preview
derivative and is never generated into the local preview cache. RAW originals
continue to be downloaded as RAW files; browser display is handled by
`detail`.

## Proposed cache and queue model

Replace the single pair of files with independently identified derivatives:

```text
cache/<asset-id>-<sha256>-grid-v1/grid.jpg
cache/<asset-id>-<sha256>-face-source-v1/face-source.jpg
cache/<asset-id>-<sha256>-detail-v1/detail.jpg
```

The exact directory layout may differ, but the derivative kind and renderer
version must be part of the cache identity and URL/ETag. A renderer change
must never serve a stale file under the old identity.

The database should track derivative state independently. The simplest
compatible shape is a derivative table keyed by `(asset_id, kind)` with:

- status: `missing`, `pending`, `running`, `ready`, `failed`;
- attempts and error;
- queued and completed timestamps;
- byte size and last access time;
- renderer version and source hash.

If a new table is too invasive for the first slice, retain the jobs table with
job types such as `preview-grid-v1`, `preview-face-source-v1`, and
`preview-detail-v1`, then extend cache accounting to key rows by kind. The
long-term table is preferable because cache accounting, status, and queue
state are currently coupled to the old two-file assumption.

## Queue priority and worker behavior

Use one queue implementation with explicit derivative priority rather than
four independent processes competing for the same HDD:

1. `grid` requests and import-time grid jobs;
2. `face-source` requests;
3. `detail` requests;
4. optional background warming.

The worker should claim the highest-priority available derivative and retain
per-kind concurrency limits. A reasonable initial deployment is:

```text
grid:        3–4 workers
face-source: 1–2 workers
detail:      1 worker
```

These are starting points, not fixed requirements. More workers may reduce
latency on SSD storage but can make an HDD slower through seek contention.
The worker should expose queue age and render duration so the values can be
tuned from measurements.

The idle polling delay should also be reduced or replaced with a wake-up
mechanism. The current worker loop can sleep for two seconds when no job is
available, while the HTTP API tells the browser to retry after two seconds.
The first implementation should use a short bounded poll (for example
250–500ms) and a later implementation may use PostgreSQL notification or a
similar wake-up path.

## API behavior

Add explicit derivative endpoints or extend the existing endpoint contract:

```text
GET /assets/{id}/grid
GET /assets/{id}/detail
GET /faces/{face-id}/thumbnail
```

The face endpoint remains the public crop endpoint. Internally it resolves the
face's asset, ensures `face-source` exists, and crops from that source.

For a missing derivative:

- return `202` with the derivative kind and job state;
- include a short retry hint for the frontend;
- never queue a larger derivative as a side effect of a smaller one;
- keep `404` for genuinely unavailable embedded previews and `503` for
  terminal generation failure.

The API should make the derivative kind visible in metrics and structured
logs, including asset ID, cache hit/miss, queue age, render duration, source
kind, and output bytes.

## Frontend changes

Update response fields so their names express the derivative contract:

- `thumbnailUrl` or `gridUrl` for library cards and filmstrips;
- `faceThumbnailUrl` for face references;
- `detailUrl` for the dedicated photo view.

The existing `PreviewImage` component should retain bounded concurrent fetches,
but its pending behavior should be derivative-aware:

- retry `grid` quickly because it blocks visible library content;
- retry `face-source` in the background where possible;
- show the dedicated photo view immediately with a clear preparing state;
- do not make navigation wait for the entire filmstrip to load.

The first direct image request and the `202` probe should be measured. If the
browser already has a usable URL, it should remain the fast path; if the
server reports pending, the client should avoid an unnecessary two-second
backoff.

The dedicated photo page may optionally prefetch `detail` for the previous and
next assets after the current image has loaded. This should be a low-priority
optimization and must not compete with visible grid work.

## Import and warming policy

Import should enqueue `grid` first. It may enqueue `face-source` only after
face assignments exist, and it should not eagerly enqueue `detail` for the
whole library unless an operator explicitly requests a warm-up.

Provide an explicit warm-up command or maintenance action with scopes such as:

- all active assets;
- a date range;
- an album;
- currently visible/search results.

Warm-up must be resumable, observable, and lower priority than interactive
requests.

## Migration and rollout

1. Add derivative-kind helpers and cache identities without removing the old
   `thumbnail.jpg`/`preview.jpg` readers.
2. Add `grid` generation and change library/filmstrip URLs to use it.
3. Add `detail` generation and switch the dedicated photo page.
4. Add `face-source` generation and switch face thumbnails.
5. Backfill only `grid` for the active library if desired; do not backfill
   `detail` or `face-source` blindly.
6. Retire old cache directories after a measured compatibility window.

The old cache may be treated as a read-only fallback during rollout, but old
files must not be reported as current after a renderer-version change. Cache
eviction must account for each derivative kind independently so a large detail
file does not evict all grid thumbnails for the same user-visible collection.

## Measurements and acceptance criteria

Add metrics and a small benchmark covering warm and cold paths:

- API response time for cache hit;
- time from `202` to derivative ready;
- queue wait time by kind;
- source read time;
- decode/orientation time;
- resize/encode time;
- bytes written and served;
- cache hit ratio by kind;
- queue depth and oldest age by kind;
- worker utilization and HDD I/O wait where available.

The initial rollout is successful when:

- a cold library card normally becomes visible without a multi-second retry
  gap;
- `grid` work is not delayed behind `detail` work;
- face thumbnails no longer wait for a full-size preview;
- opening a dedicated photo still produces the current or better visual
  quality;
- warm cache navigation remains dominated by browser/network latency rather
  than server rendering;
- increasing preview concurrency is justified by measurements rather than
  assumed to help HDD storage.

## Risks and decisions

### HDD contention

More workers can increase seeks and reduce total throughput. Keep per-kind
limits configurable and benchmark 1, 2, 3, and 4 grid workers against a
representative RAW set.

### Embedded preview quality

Some RAW files contain a high-quality embedded JPEG while others contain only a
small preview. The source-selection code should record which embedded tag was
used and fall back through the existing tag order. A small embedded preview
may be acceptable for `grid` but insufficient for `detail`.

### Duplicate decoding

Generating three derivatives independently could read and decode the same RAW
multiple times. Prefer a shared source cache or a single source-preparation
step per asset when multiple derivative jobs are coalesced. Do not introduce
that optimization before independent latency and queue behavior are working.

### Browser caching

Every derivative URL and ETag must include its kind and renderer version. This
is required both for correctness and for testing changes without users being
stuck with an old browser-cached image.

## Open questions

- Is 768px or 1024px the best face-source size for the current face-card
  layouts?
- Should `detail` remain 2560px, or should it follow the client viewport and
  device pixel ratio?
- Should the API expose separate status fields for grid, face-source, and
  detail, or should the frontend infer status from derivative endpoints?
- Is PostgreSQL polling at 250–500ms acceptable, or should queue wake-ups be
  implemented before this work ships?
- Should the first rollout warm grid derivatives for the existing 797-asset
  library, or let them fill on demand?
