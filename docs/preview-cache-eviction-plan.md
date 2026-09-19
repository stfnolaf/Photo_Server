# Implementation plan: preview cache accounting and LRU eviction

## Problem

Previews and thumbnails live in a local, unbounded cache:
`{data_dir}/cache/{asset_id}-{primary_sha256}-v1/{preview.jpg,thumbnail.jpg}`.
Nothing ever deletes from it and nothing tracks access, so the cache grows
monotonically and old, unviewed images accumulate preview files forever.

The design intent (design doc §6) is that previews are **disposable**: they can
always be regenerated from the immutable S3 original. The API already behaves
this way — a missing preview file yields `202 pending` + requeue + regenerate —
so eviction is safe *by construction* for the API. Two things stand in the way
of actually evicting:

1. **No access tracking.** LRU needs `last_accessed_at` and per-file sizes;
   neither exists anywhere today.
2. **The AI worker is coupled to the cache.** `ai_worker.py` hard-fails when a
   preview is marked `ready` but its file is missing, and semantic reuse
   silently skips assets whose source preview isn't cached. Eviction must not
   break the AI pipeline.

## Global invariants (every phase must preserve these)

- **Originals are the source of truth.** A preview file may never be the last
  copy of any pixel data. Eviction deletes cache files only.
- **Miss path is the safety net.** After any deletion,
  `GET /assets/{id}/preview` must return `202` and regenerate from S3, never
  `500`.
- **AI jobs never see a missing preview.** Either the asset is exempt from
  eviction while AI work is pending/running, or the AI worker regenerates on
  miss (Phase 3 does both).
- **Eviction is idempotent and crash-safe.** Deleting files that are already
  gone, rows that are already gone, or files mid-`generate()` (atomic
  `os.replace` means a partial write never appears at the final path) must all
  be no-ops, not errors.
- **Deletion is two-phase per asset:** remove DB row and files only as a unit
  we can reason about — delete files first, then the row (or vice versa, but
  pick one and test the crash window). Recommended: delete row first *inside
  the same transaction that selects*, then delete files; a file left on disk
  without a row is reclaimed by the orphan sweep (below).
- **Orphan sweep:** eviction also removes cache directories with no matching
  `preview_cache` row (e.g. from a crashed two-phase delete, or an asset
  deleted while generating).

## Rollout order and dependencies

| Phase | What it buys you standalone | Depends on |
|-------|----------------------------|------------|
| 1. Access & size tracking | Pure observability: know the cache's true size, per-file bytes, and access recency. No behavior change. | — |
| 2. LRU eviction loop | The actual cache limit (`PHOTO_CACHE_MAX_BYTES`). | Phase 1 (needs sizes + LRU key) |
| 3. AI-worker resilience | AI jobs regenerate on miss; AI-pending assets eviction-exempt. Fixes the latent volume-wipe bug. | Phase 2 (only then is a miss actually possible in practice) |
| 4. Face-crop cache (optional) | Stops re-encoding 320px face crops per request. | Phase 2 (reuses the eviction policy) |

Phases 1–3 are independently shippable. Recommend landing in order; do not
ship Phase 2 before Phase 3 in a long-term sense, but a short window is safe
because Phase 2 exempts assets with pending/running AI jobs.

---

## Phase 1 — Access & size tracking

**Goal:** record, for every cached preview set, its byte sizes and last
access time. No eviction yet.

### Changes

**Migration `0010_preview_cache.sql`** (new file in
`backend/src/photo_server/db_migrations/`):

```sql
CREATE TABLE IF NOT EXISTS preview_cache (
    asset_id VARCHAR PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE,
    preview_bytes BIGINT,
    thumbnail_bytes BIGINT,
    last_accessed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_preview_cache_accessed
    ON preview_cache (last_accessed_at);
```

- `preview_bytes`/`thumbnail_bytes` nullable: a row may exist while one
  derivative is still being generated (or an asset is preview-unavailable with
  a row we keep for status symmetry — decide: recommended to keep rows only
  for `ready` assets, simpler).
- The runner (`migrations.py`) auto-discovers the file; next number is 010.
- `ON DELETE CASCADE` keeps the table consistent when assets are purged.

**`worker.py` — `generate()`:**
- On successful generation, `INSERT ... ON CONFLICT (asset_id) DO UPDATE`
  with the two file sizes (from the temp file before `os.replace`, or
  `stat` after) and `last_accessed_at = now()`.
- The early-return path (both files already exist) should also upsert if no
  row exists (backfill for files generated before this migration), using
  `stat` sizes. Do **not** bump `last_accessed_at` there.

**`api.py` — `derivative()`:**
- On the happy path (file served), record access. To avoid one write per
  request, throttle: keep an in-process dict `asset_id -> last_touch` and
  only `UPDATE preview_cache SET last_accessed_at = now() WHERE asset_id = ...
  AND last_accessed_at < now() - interval '30 seconds'` when the per-asset
  cooldown has elapsed. 30s granularity is fine for LRU at photo-library
  timescales. The UPDATE's rowcount also tells you the row existed; if not,
  insert one (size via `stat`).
- Do not touch the 202/404/503 paths.

**Backfill for existing deployments:**
- A one-shot path is enough: when the worker starts (or a CLI command
  `photo cache-rebuild-index` if you prefer explicitness), walk
  `{data_dir}/cache/`, and for each directory matching the
  `{asset_id}-{sha256}-v1` pattern that has a row-less asset, `stat` the files
  and upsert. This also catches the orphan case in the reverse direction.
  Keep it simple: a loop in the worker's first `_cleanup_loop` iteration is
  acceptable; a CLI subcommand is nicer. Pick one in the plan review.

### Tests (new `tests/test_preview_cache_tracking.py`, extend `test_previews.py`)

- `generate()` upserts sizes; second `generate()` (files exist) upserts missing
  row but does not bump `last_accessed_at`.
- Serving a preview via the API (existing test harness style from
  `test_previews.py` / `test_integration.py`) updates `last_accessed_at` at
  most once per cooldown window.
- Migration test: `test_migrations.py` pattern — fresh DB applies 0010;
  re-running is a no-op (checksum stable).

### Context checklist for the implementing model

- `backend/src/photo_server/worker.py` (full)
- `backend/src/photo_server/api.py` (`derivative` + endpoint section)
- `backend/src/photo_server/catalog.py` (connection/session pattern, `preview_status`)
- `backend/src/photo_server/config.py`, `compose.yaml`
- `backend/src/photo_server/db_migrations/009_burst_clusters.sql` (style),
  `migrations.py` (runner semantics)
- `backend/tests/test_previews.py`, `tests/test_migrations.py`

### Definition of done

- `SELECT count(*), coalesce(sum(preview_bytes),0) FROM preview_cache;`
  reflects the cache after a fresh import + a few views.
- Full existing test suite passes; new tests green.
- No behavior change visible to the API.

---

## Phase 2 — LRU eviction loop

**Goal:** when the cache exceeds a configured byte budget, evict
least-recently-accessed entries until back under budget.

### Changes

**`config.py` + `compose.yaml`:**
- `cache_max_bytes: int = 0` → env `PHOTO_CACHE_MAX_BYTES` (0 = disabled).
- `cache_eviction_interval_seconds: int = 300` →
  `PHOTO_CACHE_EVICTION_INTERVAL` (loop period; default 5 min).
- `cache_eviction_target_ratio: float = 0.9` →
  `PHOTO_CACHE_EVICT_TARGET_RATIO`: evict until total ≤ budget × ratio
  (hysteresis so we don't evict a handful of files every loop).
- Set a real budget in `compose.yaml` (e.g. 50 GB) with a comment explaining
  the disposable-cache rationale.

**New module or function in `worker.py`: `evict_previews(service) -> dict`**

Algorithm (single Postgres transaction for selection + row deletion, then
file deletion):

1. `total = SELECT coalesce(sum(preview_bytes),0)+coalesce(sum(thumbnail_bytes),0)
   FROM preview_cache`. If `total <= budget`, return
   `{"status": "ok", "evicted": 0}`.
2. Select candidates `ORDER BY last_accessed_at ASC` (ties: `asset_id`),
   **excluding** assets that have a `jobs` row with
   `job_type IN ('preview-v1','ai-v1') AND status IN ('queued','running')`
   (the AI exemption is cheap here and makes Phase 2 safe before Phase 3).
   Accumulate until `total - freed > budget * target_ratio` would no longer
   hold, i.e. stop once freed bytes bring us under the target.
3. `DELETE FROM preview_cache WHERE asset_id = ANY(:ids)` (same transaction,
   commit).
4. For each evicted asset: `shutil.rmtree(cache_dir, ignore_errors=True)`.
   Log each eviction as a JSON line matching the existing worker log style:
   `{"status": "preview_evicted", "asset_id": ..., "freed_bytes": ...}` and a
   summary `{"status": "preview_eviction", "evicted": n, "freed_bytes": ...,
   "remaining_bytes": ...}`.
5. **Orphan sweep** (same loop, cheap): list `cache/` directories, delete any
   whose `asset_id` prefix has no `preview_cache` row. This is what makes the
   two-phase delete crash-safe and cleans up cache dirs of deleted assets.

**Wiring:** in `worker.py::run()`, add a third thread
`executor.submit(_eviction_loop, service)` alongside `_worker_loop` and
`_cleanup_loop`; the loop sleeps `cache_eviction_interval_seconds`, calls
`evict_previews`, catches and logs exceptions (never kill the loop).

**Race with `generate()`:** none in practice — eviction only selects assets
whose preview job is not queued/running, and `generate()` finishes by
`os.replace`-ing new files; worst case we delete a dir whose files are being
recreated, the row is re-upserted by `generate()`, and the next loop sees it
again. `shutil.rmtree(ignore_errors=True)` + per-file `try/except` on
`os.replace` already tolerate this.

### Tests (new `tests/test_preview_eviction.py`)

- Under budget → nothing evicted.
- Over budget → oldest `last_accessed_at` evicted first; stops at target
  ratio; exempted assets (queued/running preview or ai job) skipped.
- Evicted files actually removed from disk; DB rows gone.
- Orphan sweep removes a cache dir with no row; leaves valid dirs alone.
- Idempotence: running eviction twice in a row is a no-op the second time.
- `cache_max_bytes = 0` → eviction disabled (loop returns immediately).

### Context checklist

- `backend/src/photo_server/worker.py` (full — loops, `run`, `cache_paths`)
- `backend/src/photo_server/catalog.py` (jobs table, session pattern)
- `backend/src/photo_server/config.py`, `compose.yaml`
- `backend/tests/test_previews.py` (fixture style), `tests/test_preview_cache_tracking.py` (Phase 1)
- `backend/src/photo_server/service.py`

### Definition of done

- With a tiny `PHOTO_CACHE_MAX_BYTES` and N test assets, the worker log shows
  eviction of the oldest-accessed assets; `du` of the cache dir matches the
  DB total; re-requesting an evicted preview returns `202` then regenerates.
- Existing suite passes.

---

## Phase 3 — AI-worker resilience

**Goal:** make the AI pipeline correct under cache misses, so eviction (and
volume wipes) can never wedge AI jobs.

### Changes

**`ai_worker.py` — the "ready but file missing" hard-fail:**
- Replace the hard error with regeneration: when
  `preview_status == "ready"` but `preview.jpg` is absent, call
  `generate(service, manifest)` (import from `worker.py`); if it returns
  `False` (genuinely unavailable), fail the job with the existing
  "preview unavailable" path; otherwise continue.
- This also fixes the latent bug where wiping the `app-data` volume while
  Postgres says `ready` permanently fails AI jobs.
- Guard against concurrent regeneration: two AI jobs for the same asset
  shouldn't double-generate. Simplest: the generation is idempotent
  (early-return when both files exist) and `os.replace` is atomic, so a race
  is harmless — but note it. If you want to be strict, only regenerate when
  the asset's `preview-v1` job is not queued/running.

**Eviction exemption hardening (in Phase 2's query, now that it's the AI
worker's problem too):** keep the existing exclusion of assets with
queued/running `ai-v1` jobs. No new code needed if Phase 2 did it; otherwise
add it here.

**Semantic reuse (`_load_reuse_source`):** no change required — skipping a
reuse candidate whose preview isn't cached is already the designed behavior
(degrades to full analysis, which Phase 3 now supports end-to-end). Add a
log line at debug/JSON level when a candidate is skipped for cache miss, so
the degradation is observable.

### Tests (extend `tests/test_ai_worker_reuse.py` or add
`tests/test_ai_worker_preview_miss.py`)

- Preview marked ready + file missing + `generate` succeeds → job proceeds.
- Same but `generate` returns `False` → job fails with the preview-unavailable
  error, not the "ready but missing" error.
- Regeneration is not attempted when the file exists.
- Reuse candidate with missing preview is still skipped (regression guard).

### Context checklist

- `backend/src/photo_server/ai_worker.py` (full)
- `backend/src/photo_server/worker.py` (`generate`, `cache_paths`)
- `backend/src/photo_server/catalog.py` (`preview_status`, job queries)
- `backend/tests/test_ai_worker_reuse.py`, `tests/test_previews.py`

### Definition of done

- `rm -rf {data_dir}/cache` + requeue an `ai-v1` job → job succeeds
  (regenerates its preview).
- Full suite (including the previously-ignored AI reuse tests) passes.

---

## Phase 4 (optional) — Face-crop cache

**Goal:** stop re-encoding a 320px JPEG per face-thumbnail request.

### Design choice

Two options; recommend **(a)**:

- **(a) File cache under the same cache dir:**
  `{data_dir}/cache/{asset_id}-{sha256}-v1/faces/{face_id}.jpg`, evicted with
  the rest of the asset's cache by Phase 2. Key must change when the crop
  changes: include a hash of `(face_id, bounding_box, padding, size)` in the
  filename, or invalidate on face-row update. Simplest correct key:
  `{face_id}-{box_hash8}.jpg`.
- **(b) In-process LRU** (e.g. `cachetools.LRU` keyed by the same composite):
  zero schema/eviction work, but lost on API restart and duplicated across
  API replicas. Fine if the face endpoint is the only consumer and traffic is
  low.

### Changes (for option a)

- `api.py::face_thumbnail()`: compute crop path; if present, `FileResponse`;
  if not, render as today and write atomically (same temp+`os.replace`
  pattern as `worker.py`).
- Add face crop bytes into the eviction accounting: either extend
  `preview_cache` with a `face_cache_bytes` column (migration `0011`) or,
  simpler, have the orphan/sweep phase `du` the `faces/` subdir and include
  it in the asset's size. Recommended: keep it simple — count the whole
  cache dir size on disk for eviction totals rather than summing columns,
  *if* you switch to that in Phase 2. (Decision point: DB-sum vs disk-sum.
  Disk-sum is more accurate for Phase 4 but slower; at ~10⁵ assets it's still
  milliseconds. Pick one and note it.)
- `faces` rows are referenced by `person_id` etc.; deleting face crops must
  never touch Postgres face data — they are pure cache.

### Tests

- Second request for the same face serves from cache (no re-encode: assert
  file mtime unchanged / counter).
- Changing the bounding box produces a new file (old one swept as orphan or
  left — decide; orphan sweep handles it).
- Eviction of the asset removes `faces/` contents too.

### Context checklist

- `backend/src/photo_server/api.py` (`face_thumbnail`, `derivative`)
- `backend/src/photo_server/worker.py` (eviction, `cache_paths`)
- `backend/src/photo_server/catalog.py` (faces queries)
- `backend/tests/test_previews.py`, Phase 2 eviction tests

---

## Cross-cutting notes

- **Config plumbing:** every new setting needs `config.py` + `compose.yaml`
  + (if it affects tests) the test fixtures' `SimpleNamespace(settings=...)`.
- **Logging:** match the existing JSON-lines style in `worker.py`; never log
  file paths that could grow unboundedly in logs (asset_id is fine).
- **No frontend changes** in any phase. The 202-pending flow is already what
  the UI handles.
- **Rollback:** each phase is independently revertable. Phase 1's table can be
  dropped; Phase 2's loop is disabled by `PHOTO_CACHE_MAX_BYTES=0`; Phase 3
  is a behavior fix with no schema.
- **Sizing reference:** a 2560px q85 preview is typically a few hundred KB to
  ~1–2 MB; a 50 GB budget holds on the order of 30k–100k preview sets.

## Suggested validation sequence (per phase)

1. `cd backend && /home/stephen/dev/photo_server/.venv/bin/python -m pytest
   tests/ -q` (add the established `--ignore` list for integration tests if
   running the fast set).
2. `docker compose up` with a small `PHOTO_CACHE_MAX_BYTES` (e.g. 100 MB) and
   a real import; watch worker logs for `preview_evicted` lines; verify
   evicted previews regenerate on demand.
3. For Phase 3, additionally `rm -rf` the cache volume contents and requeue
   AI analysis.
