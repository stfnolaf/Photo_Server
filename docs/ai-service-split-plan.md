# Implementation plan: optional, swappable AI services (OpenAI-standard VLM client + face-service microservice)

## Problem

All AI inference currently runs inside the `ai-worker` container on the
photo server's machine:

- **Semantic (VLM)** — `analysis.py:analyze_semantics` speaks Ollama's
  *native* API (`POST /api/chat` with `format`/`think`/`keep_alive`/
  `num_ctx`/`num_predict`; digest via `GET /api/tags`). The client is bound
  to Ollama even though the payload is just "JPEG + prompt → structured
  JSON".
- **Faces** — `analysis.py:AdaFaceAnalyzer` runs the verified YuNet
  (detection) + SFace (alignment) + AdaFace IR101 (512-d embedding,
  onnxruntime CUDA) pipeline *in-process* inside the worker. The worker
  container therefore carries the CUDA image (`Dockerfile.ai`), the GPU
  reservation, and the `/models` bind mount (`compose.yaml: ai-worker`).

The goal is **flexibility**, not relocation:

1. **The VLM target is interchangeable.** The client should speak the
   OpenAI chat-completions standard so the endpoint is a plain URL: local
   Ollama (`http://ollama:11434/v1`), Ollama on another machine, or any
   hosted OpenAI-compatible provider. Choosing — or not choosing — a VLM
   is a configuration decision.
2. **Face intelligence lives in its own stateless microservice** with a
   stable contract, so the photo server contains no face models at all.
   Where that service runs (this machine, another, a box with more GPU) is
   an operator decision the photo server never has to know about. The
   in-process face path is removed, not kept as a fallback: the photo
   server is for bookkeeping — it prepares inputs, stores results, and
   matches stored embeddings; it runs no learned models.
3. **AI is an optional configuration.** The server must work completely
   (import, previews, search, people UI) with no AI configured. `ai-v1`
   jobs accumulate as `pending` (they are already created
   unconditionally at import — `catalog.py:359-378`), and the worker pushes
   the backlog through whatever services happen to be configured.

### What actually runs on the GPU today (confirmed from this repo)

- **1× AdaFace IR101** — a single onnxruntime `InferenceSession` in the
  single `ai-worker` process (single-concurrency loop; one analyzer
  instance). This repo does not run a second AdaFace; it only mounts the
  sibling `face-scanner`'s verified model files read-only. If a second
  AdaFace process is observed on the box, it is the scanner itself.
- **1× Qwen3-VL 8B (Q4)** — the Ollama service, pinned to
  `OLLAMA_MAX_LOADED_MODELS=1`, `OLLAMA_NUM_PARALLEL=1`, 10 m keep-alive
  (≈ 13.1 GB combined on the RTX 3090, README §330).

Target footprint when this plan (including Phase 5) is complete:
**1× AdaFace (in the face-service) + 1× Qwen3-VL** — the scanner's
second AdaFace is retired, leaving real VRAM headroom.

### Other load-bearing facts

- **Job lifecycle.** `jobs(asset_id, job_type, status, attempts, error,
  lease_until, force_full)` — no schema change is needed for this plan.
  `claim_ai_job` (`catalog.py:1160`) claims a `pending` `ai-v1` job (or one
  `running` with an expired 1800 s lease) only after the asset's
  `preview-v1` job is terminal; `finish_ai_job` (`catalog.py:1207`) sets
  the terminal status and clears the lease. A `failed` job stays failed
  until requeued manually (`POST /assets/{id}/analysis`, CLI `analyze`,
  "Analyze again").
- **Matching stays on the server.** `complete_ai_analysis`
  (`catalog.py:1223`) compares each incoming embedding against person
  centroids in Postgres using `face_match_threshold`. That is bookkeeping
  (comparing stored vectors), not inference — it never moves.
- **Worker stage order** (`ai_worker.py:run_once`): setup → fingerprint →
  semantic-reuse decision → face → semantic. Fingerprinting
  (pHash/dHash, `fingerprints.py`) is pure deterministic computation with no
  learned models — it stays in the worker. Face failures already fail the
  job with `stage: "face"` and a `stageFailures` counter; remote transport
  failures reuse the same attribution (Phase 3A refines it).
- **Test stubbing.** The AI-worker tests monkeypatch
  `ai_worker.analyze_semantics` and `ai_worker.resolve_model_digest`, and
  *assign* the instance attribute `worker._faces` (declared at
  `ai_worker.py:39-48`) to a duck-typed stub whose
  `.analyze(jpeg) -> list[dict]` matches `AdaFaceAnalyzer`'s
  (`test_burst_integration.py:111-118`, `test_ai_worker_preview_miss.py`,
  `test_ai_worker_reuse.py`). Keeping those names and shapes means the
  switch to a remote analyzer (Phase 2B) changes each test file by a
  couple of lines at most.
- **Reuse gates** (`reuse.py`) treat `UNKNOWN_DIGEST` as "never reuse" —
  the safe default when a non-Ollama provider cannot report a model digest.
- **Burst reuse is semantic-only — and stays that way (settled, Q6).**
  The worker runs the `burst-reuse-v1` policy per job
  (`ai_worker.py:_semantic_reuse`): eight gates — fingerprint version,
  model digest, pipeline/model identity, aspect/dimensions, 3 s capture
  window, camera identity, document/screenshot/visible-text exclusions,
  pHash ≤ 4 / dHash ≤ 6, and 16×16 pixel similarity ≥ 0.95 — all must
  pass before a target inherits a source's semantic analysis. The face
  stage is **not** part of the policy: face inference runs on every
  claimed job (`ai_worker.py:98-109`), because it is a fraction of a
  second per image against the VLM's long pole, and per-image detection
  keeps every frame's boxes and embeddings frame-specific.
- **`force_full` is the existing per-photo opt-in** (`jobs.force_full`,
  migration 008; set via the `POST /assets/analyze` body,
  `retry_analysis?force_full=true`, or the CLI): the policy is still
  evaluated (the result reports `wouldHaveReused`) but never applied —
  the job computes for real.
- **Repo layout (settled, Q7).** AI service code lives in its own
  top-level folder, `face-service/` — a sibling of `backend/` and
  `frontend/`, never a subpackage of the photo server. It is a
  standalone Python package with its own `pyproject.toml`, lock file,
  `Dockerfile` (the CUDA image, moved here from `backend/Dockerfile.ai`),
  and tests. The photo server keeps only the thin HTTP client
  (`face_client.py`) — the same split as the existing `upload_client.py`.
- **Visibility.** `GET /health` (`api.py:175`, `HealthOut` in
  `api_schemas.py:544`) already reports `analysisPending/Running/Failed`;
  the frontend `Health` type (`frontend/web/src/api/types`) and
  `PhotoInspector.tsx` ("Waiting for the background GPU worker") consume
  it. The OpenAPI spec is a checked-in golden artifact
  (`openapi/openapi.json`, `scripts/check_openapi.py`), so any
  `HealthOut` change requires a spec regeneration.

## Goals and non-goals

**Goals**

- G1: `analyze_semantics`/`resolve_model_digest` speak OpenAI
  chat-completions; the VLM endpoint (local Ollama, remote, or hosted) is
  an env var.
- G2: `face-service` is the **sole** runtime home of the face models:
  its own top-level folder (`face-service/`, not under `backend/` —
  settled, Q7), token-authenticated, stateless, GPU, verified models,
  one stable HTTP contract. The in-process path is removed; the photo
  server's images carry no CUDA/onnxruntime/OpenCV-face stack, no model
  files, and no GPU reservation.
- G3: AI fully optional (empty URLs → worker idles, backlog accumulates,
  everything else works). When configured and healthy, the worker drains
  the backlog; transient unavailability requeues instead of failing.
- G4: **Concurrency mirrors the hosted-provider model:** the photo
  server is a well-behaved client (small in-flight request bound,
  default 1; no client-side rate limiting); the services are the pacer
  (face-service: internal FIFO queue + 429/`Retry-After` when saturated;
  Ollama: its own knobs; hosted providers: their own rate limits). The
  same client code serves local and hosted.
- G5: Docs for all configurations: no AI / local AI / remote or hosted AI.

**Non-goals**

- No model changes: same YuNet/SFace/AdaFace files, same
  `ADAFACE_IDENTITY` provenance, same ArcFace normalization. Stored
  embeddings remain comparable — no re-analysis is required.
- No person matching or embedding storage in the face service.
- No meaningful increase in GPU parallelism now (hardware-constrained;
  the design only has to make it a knob, default 1).
- No public-CA certificate infrastructure: cross-machine TLS uses the
  service's self-signed CA (or the existing reverse proxy), not a public
  CA.
- No DB migration (the `jobs` table already models the backlog).

## Global invariants (every phase must preserve these)

- **Originals are the source of truth.** The split moves only inference;
  no pixel data moves between services.
- **The photo server never runs learned models.** No model files, no GPU
  reservation, no inference libraries — and no face code at all — in any
  image the server-side services (`api`, `worker`, `ai-worker`) use.
  Deterministic fingerprinting is bookkeeping and stays; everything
  learned moves to the standalone `face-service/` folder (Q7).
- **Inference failure never blocks ingestion.** Import, previews, and the
  media worker behave identically whether or not AI exists. A dead or
  unconfigured AI service is a queue that waits, never an error that
  blocks.
- **One embedding space.** The face client may only use a service whose
  model identity (name/revision/sha256) matches the verified
  `ADAFACE_IDENTITY`; otherwise the service is treated as unhealthy.
  Mixing embedding generations silently corrupts person grouping.
- **The artifact contract is unchanged.** `analysis_runs`, `faces` rows,
  and the S3 artifact JSON keep their exact shape, so existing libraries
  and the OpenAPI golden remain valid (modulo the deliberate `HealthOut`
  fields).
- **No silent CPU fallback.** The face service keeps AdaFace's
  "refuse without CUDA" guard and exits non-zero if it cannot run the
  verified models; a process that cannot embed is not a face service.
- **Deterministic failure attribution.** Every AI job result still reports
  `stage` and `stageFailures`; remote transport failures are a named
  class but map onto the same counters.

## Concurrency model: the services are the pacer, the photo server is a well-behaved client

The model mirrors how a client talks to OpenAI or Anthropic: the client
never rate-limits the provider, and the provider paces the client.

- **Photo server = bounded client, not a rate limiter.** The AI worker
  claims `ai-v1` jobs and keeps at most `PHOTO_AI_WORKER_CONCURRENCY`
  (new setting, default **1**) requests in flight — a *client resource
  cap* (open sockets, in-memory JPEGs), not a throttle: there is no
  client-side rate limiting, no token bucket, no sleep-between-requests
  logic. At 1 it degenerates to today's single-consumer loop, so default
  behavior is byte-identical to current operation. It is also not "fire
  all": no client opens thousands of connections against a single-GPU
  box, which is no more what you would do to a hosted API than you would
  to a local one.
- **face-service = the pacer.** It accepts concurrent requests up to a
  bounded internal FIFO queue and runs **one image through the GPU
  pipeline at a time** (single-flight is a requirement, not a choice:
  OpenCV's `FaceDetectorYN` carries per-call `setInputSize` state, so
  concurrent detection is a data race; `PHOTO_FACE_SERVICE_CONCURRENCY`,
  default 1, max 4, lifts it later with per-thread isolated state). When
  the queue is saturated it does exactly what OpenAI/Anthropic do:
  **`429` with `Retry-After`** (`503` is reserved for the not-yet-ready
  startup state). Queue depth and in-flight count are exposed in
  `/health`.
- **VLM = the pacer via the provider's own semantics:** Ollama queues
  internally (single-stream today, `OLLAMA_NUM_PARALLEL` /
  `OLLAMA_MAX_LOADED_MODELS` = 1); hosted OpenAI/Anthropic respond with
  429 + `Retry-After` when you exceed your tier. The client's code path
  is identical for both: send, wait, honor 429 with exponential backoff
  and jitter, retry.
- **429 handling end-to-end:** a 429 (or timeout) on an in-flight
  request returns the affected job to `pending` (Phase 3A) and the
  worker backs off before claiming more — a saturated or rate-limited
  service paces the whole backlog without the photo server ever
  implementing its own rate limiting.
- **Why default 1 across the board:** the VLM is the long pole
  (single-stream today) and face inference is a fraction of a second per
  image, so face parallelism buys nothing until the VLM can run more
  than one at a time; and the current box already carries its full
  budget. Locally the service's queue absorbs bursts, so the 429 path
  rarely fires — it *feels* exactly like fire-and-wait; against a hosted
  provider the same 429 path is what actually paces you. Scaling later
  is a knob turn on the services plus the client bound — no
  photo-server architectural change, which keeps the photo server honest
  as a bookkeeper.

## Phase sizing: each phase fits a 100k-context model

Every phase below is designed to be handed to a **fresh 100k-context
model with no conversational memory**: the phase section in this document
is the entire brief. Each phase section is therefore self-contained —
what must be true before it starts, the closed list of files it reads and
writes, the changes, its tests, its definition of done, its hands-on
validation, and a handoff note for the next fresh model.

**The budget.** Of 100k tokens, roughly 15–20k are permanently consumed
by system context, the phase brief, tool-call overhead, and the model's
own reasoning; that leaves ~80k for the working set. Rules of thumb: a
line of code or JSON costs about 8 tokens, a line of prose about 15. Two
facts shape the sizing:

- **Edits echo their text.** Every edit re-presents the old string and
  the new string, so ten small edits to one file cost more context than
  one large edit of the same region. Prefer a few large hunks.
- **Test output is the biggest variable.** A single failing test with a
  traceback can run to hundreds of lines. A model that runs the whole
  backend suite (15,700 lines of tests) at once drowns in output; a
  model that runs the phase's own test files individually does not.

**Sizing rules (applied to every phase below):**

1. **Closed manifest.** Each phase lists every file it may read in full
   (with the current line count) and every file it creates or modifies.
   *Range reads* — a named region of a file that is not otherwise
   edited — are allowed and count at the size of the range. Reading
   anything outside the manifest is a phase-design defect: stop and
   report, do not explore. If a manifest file has grown >25% beyond its
   listed line count, re-assess the split before starting, not after.
2. **Working-set cap.** Total full-read + full-write lines per phase:
   ≤ 2,000 (~16k tokens) is comfortable; ≤ ~5,000 is acceptable when the
   extra volume is read-once files edited surgically and test runs are
   targeted; beyond that, the phase must be split. Phase 3B is the
   documented ceiling case.
3. **Diff-only artifacts.** `openapi/openapi.json` (4,777 lines, 127 KB)
   and the generated frontend client (`frontend/web/src/api/generated/`,
   ~3,300 lines) are **never read in full**: they are regenerated by
   `scripts/dump_openapi.py` and `npm run generate:api` respectively, and
   only the resulting `git diff` is read (usually a few dozen lines).
   Lock files are hand-maintained and small (≤ 50 lines) — they are read
   in full.
4. **Targeted tests.** A phase runs the test files in its manifest,
   individually. The full gate — `run_all_tests.sh` (ruff, the OpenAPI
   check, the complete pytest suite, frontend check/test) — is run once
   at the end of the phase, and only its per-section summary lines are
   read (it writes the full logs to `test-reports/`), never the raw
   stream.
5. **One phase, one commit.** Each phase ends green on its own tests and
   on the full gate, and is committed as a unit. A phase that cannot be
   completed from its manifest alone is not a valid phase — split it.

**Per-phase start ritual** (everything a fresh model reads before writing
code): the *Global invariants* section above (~30 lines) → that phase's
section in full → the manifest files. Nothing else.

| Phase | Full reads (lines) | Writes (lines) | Diff-only artifacts | Sized |
|-------|--------------------|----------------|---------------------|-------|
| 1. OpenAI-standard VLM client | ≈ 850 (analysis, config, compose, 2 test files, lock, pyproject, `.env.example`) + README range | ≈ 600 | none | comfortable |
| 2A. Stand up the face-service package | ≈ 380 (analysis + the backend build files it mirrors) | ≈ 900 (all new) | none | comfortable |
| 2B. Photo server switches to the client | ≈ 2,750 (worker, config, compose, client pattern, new package, 3 test files) | ≈ 750 | none | acceptable |
| 3A. Worker gate, backlog drain, in-flight bound | ≈ 1,650 (worker, config, 3 test files) + catalog range | ≈ 900 | none | acceptable |
| 3B. Backend `/health` visibility | ≈ 4,800 (2 API files, 2 large test files, client) | ≈ 400 | `openapi/openapi.json` | **ceiling case** |
| 3C. Frontend visibility | ≈ 600 (4 small frontend files) | ≈ 150 | generated client | comfortable |
| 4. Documentation, env migration, ops | ≈ 950 (README, `.env.example`, rollout doc, `run_all_tests.sh`, compose) + design-doc ranges | ≈ 350 | none | comfortable |
| 5. face-scanner consolidation | ≈ 650 this repo (service contract + compose); the scanner repo carries its own manifest in its own brief | ≈ 100 if the contract is extended | none | this repo's part comfortable; the scanner side is a separate project |

## Rollout order and dependencies

| Phase | What it buys you standalone | Depends on |
|-------|----------------------------|------------|
| 1. OpenAI-standard VLM client | The VLM endpoint is any OpenAI-compatible URL: Ollama via `/v1` behaves as before; any other provider is an env change. | — |
| 2A. Stand up the face-service package | A runnable, tested, standalone `face-service/` (own package, CUDA image, `/health`, `POST /v1/faces/analyze`). The photo server is byte-identical (still runs its own in-process face path). | — |
| 2B. Face-service becomes the sole face home | Face inference runs only in the face-service container; the worker is remote-only; server images are model-free and GPU-free. Face inference stays per-image (Q6). | 2A |
| 3A. Optional AI: worker gate, backlog drain, dispatcher bound | The server works with AI off (worker idles, backlog accumulates); when configured, the backlog drains; transient outages requeue instead of failing; the dispatcher has its in-flight bound. | 1, 2B |
| 3B. Backend service visibility | `/health` reports configured/reachable for both services (the face probe also checks identity); the OpenAPI golden is updated. | 3A |
| 3C. Frontend service visibility | The web UI distinguishes "AI not configured" from "waiting for the worker" in the photo inspector. | 3B |
| 4. Documentation, env migration, ops | Choosing a topology (none / local / remote / hosted) is a documented config exercise, not a code change. | 1–3C |
| 5. face-scanner consolidation | The face-service is the shared face-inference endpoint for both consumers; the box runs one AdaFace instead of two, with the freed VRAM as headroom. | 2B (scanner-side changes live in the face-scanner repo) |

Phases 1 and 2A are independent of each other (each keeps existing
deployments working: Ollama via `/v1` for the VLM; the photo server
untouched in 2A). 2B flips the server over in one commit; 3A composes
the behavior of 1 and 2B into the optional-AI worker; 3B and 3C are the
two halves of visibility (backend, then frontend); Phase 4 is docs;
Phase 5 is a cross-repo consolidation (the face-scanner lives in its own
repository) and lands last — it does not block Phases 1–4. Every row is
sized per *Phase sizing* so a fresh 100k-context model can carry it
alone.

---

## Phase 1 — OpenAI-standard VLM client

### Context manifest

Read in full (line counts as of this writing):

| File | Lines | Why |
|------|-------|-----|
| `backend/src/photo_server/analysis.py` | 273 | two functions rewritten in place (names/signatures kept) |
| `backend/src/photo_server/config.py` | 89 | rename / add / drop settings |
| `compose.yaml` | 157 | the `PHOTO_AI_*` env lines |
| `backend/tests/test_analysis.py` | 108 | must keep passing (`prepare_jpeg` etc. stay) |
| `backend/tests/test_config.py` | 83 | names and defaults change |
| `backend/requirements.lock` | 37 | confirm httpx is present; no new dependencies expected |
| `backend/pyproject.toml` | 42 | confirm no dependency change is needed |
| `.env.example` | 57 | env names change |

Read by range only: `README.md:82-118` (the Configuration section, env
table).

Create / modify: `analysis.py`, `config.py`, `compose.yaml`,
`.env.example`, the README Configuration section, `test_config.py`, and
a new `backend/tests/test_openai_client.py` (~200 lines).

Diff-only artifacts: none — this phase makes no OpenAPI change, and
`openapi/openapi.json` must stay byte-identical (`check_openapi.py` is
the proof). Do not read anything else; the face path (also in
`analysis.py`) is untouched here — Phases 2A/2B handle it.

### Changes

`backend/src/photo_server/analysis.py` — rewrite the two Ollama functions,
keeping their names and signatures (tests monkeypatch them through the
`ai_worker` namespace):

- `resolve_model_digest(settings, model) -> str`:
  `GET {base}/models` (short fixed timeout, 5 s). Match the requested
  model id (with `:latest` handling as today). Use the entry's `digest`
  field if present (Ollama's `/v1/models` includes it as an extension);
  otherwise return `"unknown"` → reuse gates reject as today. Any
  connection error → `"unknown"` (existing behavior).
- `analyze_semantics(settings, jpeg) -> (SemanticAnalysis, digest, metrics)`:
  `POST {base}/chat/completions`:
  ```json
  {
    "model": "<PHOTO_AI_MODEL>",
    "messages": [{"role": "user", "content": [
      {"type": "text", "text": "<same prompt, schema inlined>"},
      {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,<b64>"}}
    ]}],
    "temperature": 0,
    "max_tokens": 900,
    "response_format": {"type": "json_schema",
      "json_schema": {"name": "photo_analysis", "strict": true,
                      "schema": <SemanticAnalysis JSON schema>}}
  }
  ```
  plus `Authorization: Bearer <key>` when the key is set, and any
  `PHOTO_AI_EXTRA_BODY` JSON merged into the body (provider extensions,
  e.g. Ollama `options.num_ctx`).
  - **Fallback ladder:** on 400/422 caused by the `json_schema` format,
    retry once with `{"type": "json_object"}` (the prompt already embeds
    the schema). Parse `choices[0].message.content` as JSON and validate
    with `SemanticAnalysis` — the existing safety net stays the final
    authority. (Ollama 0.12 supports `json_schema` on `/v1`; the ladder
    exists for other OpenAI-compatible servers.)
  - **Metrics:** map OpenAI `usage`/`system_fingerprint` onto the existing
    `metrics` dict (keys may change; the artifact tolerates any dict —
    `metrics` is provenance info, not a frozen contract).
  - **Error classes:** distinguish connection/timeout, 429
    (rate-limited — the hosted-provider pacing signal), and 502–504
    (a *service-unavailable* class, routed to requeue in Phase 3A) from
    4xx (a *request* class → job fails, as today).

`backend/src/photo_server/config.py`:

- Rename `ai_ollama_url` → `ai_base_url` (env `PHOTO_AI_BASE_URL`).
  Phase 1 keeps today's target as the default value
  (`http://ollama:11434/v1` — the old value plus the `/v1` path) and
  compose sets it explicitly, so existing deployments are unaffected;
  the *empty* default lands in Phase 3A with the optional-AI behavior.
- Add `ai_api_key: str = ""` (`PHOTO_AI_API_KEY`; Ollama accepts any
  non-empty key).
- Add `ai_extra_body: str = ""` (`PHOTO_AI_EXTRA_BODY`; JSON object merged
  into the request; validated at parse, startup error on bad JSON).
- Drop `ai_context_tokens` (no standard equivalent; Ollama's default
  4096 matches the old default; the extra-body escape hatch covers the
  rest).

`compose.yaml`: `PHOTO_AI_OLLAMA_URL: http://ollama:11434` →
`PHOTO_AI_BASE_URL: http://ollama:11434/v1`; drop
`PHOTO_AI_CONTEXT_TOKENS`. Ollama services unchanged (this phase does not
move them).

`.env.example` and the README Configuration section: update to the new
names; note Ollama is one target among many, not a dependency. (The
design doc is updated in Phase 4, alongside the full matrix.)

### Tests

- `test_config`: new names, defaults, extra-body validation.
- New `tests/test_openai_client.py` using `httpx.MockTransport`:
  - request shape (message parts, `json_schema` body, bearer header,
    extra-body merge);
  - `json_schema` 400 → single retry with `json_object` → success;
  - invalid JSON / schema violation → job-failing `ValidationError`;
  - `GET /models` digest resolution (Ollama-style `digest` field, missing
    field → `"unknown"`, unreachable → `"unknown"`);
  - connection error / 429 / 503 → service-unavailable class; 401/422 →
    request class.
- Existing stubs (`ai_worker.analyze_semantics`,
  `ai_worker.resolve_model_digest`) keep working unmodified — the
  signatures did not change.

### Definition of done

`PHOTO_AI_BASE_URL=http://ollama:11434/v1` against the pinned Ollama
0.12.7 produces the same `SemanticAnalysis` quality as before (manual
spot-check on a few images), the digest is recorded in the artifact, and
the reuse counter still distinguishes computed vs reused. Full test suite
green, including `check_openapi.py` (no spec change expected this phase).

### Hands-on validation

1. Targeted: `.venv/bin/pytest backend/tests/test_analysis.py
   backend/tests/test_openai_client.py backend/tests/test_config.py`.
2. Full gate: `bash run_all_tests.sh` — all sections green.
3. Live: `docker compose up -d`, then
   `docker compose run --rm ai-worker photo-server ai-worker --once`
   against Ollama via `/v1` — confirm the artifact's
   `models.semantic.digest` is the Ollama digest and the reuse counter
   behaves.

### Handoff

After Phase 1: the VLM speaks OpenAI chat-completions; the endpoint is
`PHOTO_AI_BASE_URL` (compose: `http://ollama:11434/v1`) with
`PHOTO_AI_API_KEY` and `PHOTO_AI_EXTRA_BODY` available, and
`PHOTO_AI_CONTEXT_TOKENS` is gone. Errors are classified into
service-unavailable (connection/timeout/429/502–504) and request (4xx)
classes — Phase 3A routes the former to requeue. The face path is still
in-process in `analysis.py`; that is Phases 2A/2B's business. The
OpenAPI golden is byte-identical.

---

## Phase 2A — Stand up the face-service package (photo server untouched)

Pure addition: create the `face-service/` folder (settled, Q7) as a
standalone, runnable package. Nothing under `backend/` changes in this
phase, so every existing deployment behaves byte-identically. The face
code is *copied* from `analysis.py` here; the removal from the photo
server happens in Phase 2B.

### Context manifest

Read in full:

| File | Lines | Why |
|------|-------|-----|
| `backend/src/photo_server/analysis.py` | 273 | source of the move (copied verbatim) |
| `backend/pyproject.toml` | 42 | packaging pattern to mirror (hatchling src layout) |
| `backend/requirements.lock` | 37 | lock style; web-stack versions to mirror |
| `backend/requirements-ai.lock` | 9 | the 9 GPU packages that move into the new lock |
| `backend/Dockerfile.ai` | 17 | the image that moves (the `.ai` suffix dies with the move — one folder, one image) |

Create (all new files; nothing else is touched):

| File | ~Lines | Content |
|------|--------|---------|
| `face-service/pyproject.toml` | 45 | hatchling src layout; deps: fastapi, uvicorn, pydantic-settings, pillow, numpy, cryptography (TLS certificate generation); dev extra: pytest, ruff; console script `face-service`. opencv/onnxruntime-gpu are deliberately **not** here — they are imported lazily inside the analyzer and come from `requirements.lock` in the image only (same split as `backend/requirements-ai.lock`) |
| `face-service/requirements.lock` | 50 | hand-maintained `pip freeze` snapshot: the web stack at the versions in `backend/requirements.lock` (fastapi, uvicorn, pydantic, pydantic-settings, pillow; numpy at the AI lock's version) plus cryptography and the 9 GPU packages from `backend/requirements-ai.lock` |
| `face-service/Dockerfile` | 17 | the CUDA image moved from `backend/Dockerfile.ai` (`python:3.12-slim-bookworm`, apt `libgomp1 libimage-exiftool-perl`, install the lock, `pip install --no-deps .`); `CMD ["face-service", "serve"]` |
| `face-service/src/face_service/__init__.py` | 1 | — |
| `face-service/src/face_service/analyzer.py` | 250 | `AdaFaceAnalyzer`, `ADAFACE_IDENTITY`, `MODEL_FILES`, `_checked_model` — copied unchanged from `analysis.py` (their cv2/numpy/onnxruntime imports stay lazy inside the methods, so the dev venv and the server images need no GPU stack to import this module) |
| `face-service/src/face_service/config.py` | 60 | the single home of all service-side settings: `PHOTO_FACE_SERVICE_BIND` (default `0.0.0.0:8901`), `PHOTO_FACE_SERVICE_TOKEN` (default empty → startup warning, dev-only), `PHOTO_FACE_MODELS_DIR`, `PHOTO_FACE_DETECTION_THRESHOLD` (0.8), `PHOTO_FACE_SERVICE_CONCURRENCY` (1, max 4) |
| `face-service/src/face_service/app.py` | 230 | the FastAPI service (below) |
| `face-service/tests/test_app.py` | 250 | the in-process suite (below) |

Diff-only artifacts: none (no compose, no OpenAPI, no backend change).
The photo server is a read-only source for the copy — do not read
anything else.

### Changes

- **The package's `app.py` is the FastAPI service itself** (the photo
  server's images never import it; only the service process does):
  - **Startup:** build `AdaFaceAnalyzer(settings)` (same constructor:
    model checksums, `ADAFACE_IDENTITY` provenance check, CUDA-only
    guard). The app answers `/health` with `503 {"status": "starting"}`
    until the session is built; a startup failure exits non-zero
    (compose restarts it; the worker side simply sees an unhealthy
    service).
  - **Single-flight executor:** all analyze requests funnel through one
    in-flight slot with a FIFO queue behind it
    (`PHOTO_FACE_SERVICE_CONCURRENCY`, default 1, max 4 — at >1,
    per-worker threads with isolated detector state; default 1 is
    correct because `FaceDetectorYN` is stateful and the box has no
    headroom). Requests beyond the bound wait in the queue; the queue
    has a soft 64-deep cap → **429 with `Retry-After`** when saturated
    (the hosted-provider saturation semantics; a wedged client must not
    pile up memory).
  - `GET /health` →
    ```json
    {"status": "ok",
     "models": {"faceDetector": "yunet-2023mar",
                "faceEmbedding": {"name": "adaface-ir101", "revision": "...",
                                  "weightsSha256": "...", "runtime": "onnxruntime-..."}},
     "detectionThreshold": 0.8,
     "concurrency": 1, "inFlight": 0, "queueDepth": 0}
    ```
    — the same provenance data the artifact already records
    (`ai_worker.py:139-149`) plus live queue state.
  - `POST /v1/faces/analyze` — body: raw JPEG bytes (the server already
    sends `prepare_jpeg(preview, ai_face_max_image_side)` output, so the
    service needs no resizing policy of its own). Guards:
    `Content-Type: image/jpeg`; size cap 16 MB → 413; PIL decode failure
    → 422; model error → 500. Response:
    ```json
    {"schemaVersion": 1,
     "faces": [{"box": [x, y, w, h], "confidence": 0.97,
               "embedding": [/* 512 floats, unit-normalized */]}]}
    ```
    exactly the shape `AdaFaceAnalyzer.analyze()` returns today.
  - **Auth and transport (settled, Q1): mirror the hosted provider APIs**
    (OpenAI/Anthropic) so the photo server's client code is identical
    whether the compute is local or hosted: `Authorization: Bearer
    <token>` on every endpoint (401 otherwise; an empty token prints a
    startup warning — docker-internal / dev only), OpenAI-shaped JSON
    errors (`{"error": {"message": ...}}`), 429 + `Retry-After`
    saturation semantics, and **TLS for any cross-machine use**. For TLS
    the service auto-generates a self-signed CA + server certificate at
    first start (persisted in a `face-tls` volume); the photo server
    verifies against that CA (`PHOTO_FACE_SERVICE_CA`, mounted
    read-only) — or TLS is terminated at the existing reverse proxy with
    a real certificate. Plain `http://` remains acceptable on the
    docker-internal network (the same trust domain as the API's existing
    "no auth on a trusted network" stance).
  - Served via uvicorn through the package's own console script
    (`face-service serve`; bind `0.0.0.0:8901` inside the container;
    `PHOTO_FACE_SERVICE_BIND` override for the later two-machine
    topology). It is deliberately **not** a `photo-server` CLI subcommand
    — the photo server package has no face code to serve.
- **The identity constant is pinned in both packages — this phase is
  the service side of the pin:** the service checks the weights it loads
  against its copy of `ADAFACE_IDENTITY` at startup (invariant: no
  silent model swap). Phase 2B adds the client-side pin (the photo
  server's `face_client` rejects a `/health` response whose identity
  differs — invariant: one embedding space). Two pins of the same
  verified fact, like a protocol version; the bind-mounted model
  directory is the common source.

### Tests

`face-service/tests/test_app.py` (analyzer monkeypatched — the dev venv
has no cv2/numpy/onnxruntime, so the suite runs without the GPU stack;
FastAPI `TestClient`): 503-while-starting → 200 after ready; health
shape (identity, thresholds, queue fields); bearer auth (401
wrong/missing, 200 with it); 413 oversized; 422 undecodable; 500 on
analyzer exception; **single-flight:** two concurrent requests → second
waits (queue depth 1 in health while first in flight), both complete;
saturation → 429 + `Retry-After`; response shape equals `analyze()`'s
(box normalization, 512 finite embeddings).

### Definition of done

`face-service` runs standalone and its suite is green. The photo server
is untouched: `git diff --stat -- backend/` is empty, and the full
`run_all_tests.sh` is green exactly as before. Until Phase 2B the face
code exists in two places (a copy in `face-service/`, the original in
`analysis.py`) — that duplication is deliberate and temporary; the two
`ADAFACE_IDENTITY` copies must be byte-identical.

### Hands-on validation

1. `.venv/bin/pytest face-service/tests/` (plus ruff over
   `face-service/`).
2. Full gate: `bash run_all_tests.sh` — unchanged and green.
3. Standalone image run (it is not in compose yet — that lands in 2B);
   from the repo root:
   ```
   docker build -t face-service face-service/
   MODEL_DIR=$(cd "${PHOTO_FACE_MODEL_DIR:-../face-scanner/runtime/raw-jpeg/models}" && pwd)
   docker run --rm --gpus all -v "$MODEL_DIR:/models:ro" \
     -e PHOTO_FACE_MODELS_DIR=/models -e PHOTO_FACE_SERVICE_BIND=0.0.0.0:8901 \
     -p 127.0.0.1:8901:8901 face-service
   ```
   then, from a second shell: `curl /health` (expect `503
   {"status": "starting"}` → `200` with identity and queue fields) and
   `POST /v1/faces/analyze` with a known JPEG (expect boxes + 512-d
   embeddings); fire two concurrent requests and watch `queueDepth` in
   `/health` prove serialization; flood past the queue cap and confirm
   429 + `Retry-After`.

### Handoff

After 2A: `face-service/` exists, builds, and is validated standalone;
the photo server is byte-identical and still runs its in-process face
path (the temporary duplicate). The face-service compose entry does not
exist yet — 2B adds it and deletes the server-side copy.

---

## Phase 2B — Photo server switches to the face-service

The flip: one commit that removes the in-process face path from the
photo server and routes the worker through the new `face_client.py`.
After this commit the server images are model-free and GPU-free.

### Context manifest

Read in full:

| File | Lines | Why |
|------|-------|-----|
| `backend/src/photo_server/analysis.py` | 273 | remove the face code (the 2A copy is the source of truth) |
| `backend/src/photo_server/ai_worker.py` | 420 | `_faces` becomes a `RemoteFaceAnalyzer` |
| `backend/src/photo_server/config.py` | 89 | add the three face-service settings; drop two |
| `compose.yaml` | 157 | the new `face-service` service; the `ai-worker` switch |
| `backend/src/photo_server/upload_client.py` | 215 | the client pattern to mirror (httpx, error classes, timeouts) |
| `face-service/src/face_service/app.py` | ~230 | the exact `/health` and analyze contract the client speaks |
| `face-service/src/face_service/config.py` | ~60 | the service-side env names (the client must match them) |
| `backend/tests/test_ai_worker_preview_miss.py` | 352 | the `worker._faces = FaceStub()` pattern |
| `backend/tests/test_ai_worker_reuse.py` | 435 | same |
| `backend/tests/test_burst_integration.py` | 326 | same |
| `backend/tests/test_analysis.py` | 108 | must keep passing once the face code leaves |
| `backend/pyproject.toml`, `backend/requirements.lock` | 42, 37 | confirm the base image can host the client (httpx and pillow are already locked) — no new dependencies expected |

Create / modify: new `backend/src/photo_server/face_client.py` (~150);
edits to `analysis.py` (remove `AdaFaceAnalyzer`, `ADAFACE_IDENTITY`,
`MODEL_FILES`, `_checked_model` — keep the VLM functions,
`prepare_jpeg`, `SemanticAnalysis`, `searchable_text`, and the pipeline
constants), `ai_worker.py`, `config.py`, `compose.yaml`, the three
worker test files (small edits), and a new
`backend/tests/test_face_client.py` (~120); **deletes**
`backend/Dockerfile.ai` and `backend/requirements-ai.lock` (both moved in
2A).

Diff-only artifacts: none (no OpenAPI change).

**Sizing note:** this is the larger of the two 2x phases (~2,750 lines
of full reads, most of them read-once files edited surgically) —
comfortably inside the 100k cap with targeted test runs. If a smaller
model carries it, do the code changes first and the test-file updates as
a second, separate step.

### Changes

- **`face_client.py`** — client code stays in the photo server package
  (the same split as `upload_client.py`); the photo server's package
  never imports `face_service`, it knows the service only through this
  module. `RemoteFaceAnalyzer`:
  - `.analyze(jpeg) -> list[dict]` (identical contract to what the
    worker consumed from the local analyzer), `.health() -> dict`
    (short 5 s timeout), `.model_version` (from health).
  - Error classes: `FaceServiceUnavailable` (connection refused,
    timeout, 429 with `Retry-After`, 502/503/504) vs `FaceServiceError`
    (other 4xx, 500, bad response shape).
  - **Identity guard — the client side of the dual pin (the service
    side landed in 2A):** on each `health()`, verify
    `models.faceEmbedding` matches the `ADAFACE_IDENTITY` constant
    (name/revision/sha256) recorded in this package; mismatch →
    `FaceServiceUnavailable` with a clear message (invariant: one
    embedding space). A model swap on the service is detected live, not
    just at startup.
- **`ai_worker.py`** — the `_faces` attribute (lines 39–48) becomes
  `RemoteFaceAnalyzer(settings)` unconditionally; the local
  `AdaFaceAnalyzer` construction is deleted. The artifact's
  `models.faceEmbedding` is recorded from the verified health response —
  the artifact shape is unchanged. The fingerprint stage stays in the
  worker (pure computation, no models).
- **The face stage stays per-image (settled, Q6).** The burst-reuse
  decision governs only the semantic stage, exactly as today: face
  inference runs on every claimed job, so the worker sends one
  face-service request per image. Rationale: face inference is a
  fraction of a second per image against the VLM's 30+ s long pole, so
  deduplicating it saves little; per-image detection keeps every
  frame's boxes, crops, and embeddings frame-specific (no copied-box
  drift, one embedding space preserved); and the service's
  single-flight FIFO queue absorbs burst traffic for a few milliseconds.
  `force_full` (the existing per-photo opt-in) keeps its current meaning
  — it bypasses semantic reuse only.
- **`config.py`** — add `face_service_url: str = ""`
  (`PHOTO_FACE_SERVICE_URL`, e.g. `http://face-service:8901`),
  `face_service_token: str = ""` (`PHOTO_FACE_SERVICE_TOKEN`),
  `face_service_timeout: int = 120` (10–3600); **remove**
  `face_models_dir` and `face_detection_threshold` — with the analyzer
  out of the package they have no home here. The face-service's own
  `config.py` (from 2A) is the single home of all service-side settings:
  `PHOTO_FACE_SERVICE_BIND`, `PHOTO_FACE_SERVICE_TOKEN`,
  `PHOTO_FACE_MODELS_DIR`, `PHOTO_FACE_DETECTION_THRESHOLD`, and
  `PHOTO_FACE_SERVICE_CONCURRENCY`. `face_match_threshold` stays
  server-side (centroids live in Postgres; matching is bookkeeping).
- **`compose.yaml`**:
  - New service `face-service`: `build: ./face-service` (its
    `Dockerfile` is the CUDA image moved from `backend/Dockerfile.ai`
    in 2A), command `face-service serve`; **receives** the GPU
    reservation, the read-only `/models` bind (via the existing
    `PHOTO_FACE_MODEL_DIR` host-path env), and env: token, detection
    threshold, concurrency, bind. No Postgres, no `app-data` volume, no
    host ports (docker-network only for now).
  - `ai-worker`: switches to the base `Dockerfile` image — **no GPU
    reservation, no `/models` bind, no AI lock file, no `ollama-model`
    `depends_on`**. It needs only httpx, PIL, and the fingerprint code,
    all in the base requirements. Env gains
    `PHOTO_FACE_SERVICE_URL` and `PHOTO_FACE_SERVICE_TOKEN`; its two
    face-model env vars (`PHOTO_FACE_MODELS_DIR`,
    `PHOTO_FACE_DETECTION_THRESHOLD`) move to the face-service.
  - `ollama` / `ollama-model` services stay in this compose file for
    now; they move to the other machine's compose file when that
    happens.
  - `backend/`: `Dockerfile.ai` and `requirements-ai.lock` are deleted
    (both moved into `face-service/` in 2A); the base image and its
    `Dockerfile` are unchanged.

### Tests

- `backend/tests/test_face_client.py` (new; the client stays in the
  photo server package): mock transport — success shape;
  connection error/timeout/503 (including saturated) →
  `FaceServiceUnavailable`; 401/422/500 → `FaceServiceError`; identity
  mismatch → unavailable; bearer header sent.
- Worker tests: `worker._faces = FaceStub()` still covers the pipeline
  (the duck-typed stub now stands for the remote analyzer — the
  existing tests need no more than cosmetic changes); add one test
  where `_faces` is a `RemoteFaceAnalyzer` pointed at a stub HTTP
  server (worker sends the prepared JPEG; artifact records the
  verified remote identity).

### Definition of done

In the modified compose file, the face-service is the only process that
loads a face model (verifiable: `ai-worker`/`api`/`worker` images
contain no onnxruntime/opencv packages — `docker run --rm <ai-worker
image> pip freeze | grep -i "onnxruntime\|opencv"` is empty, and
importing `photo_server.ai_worker` under the base requirements succeeds
without `cv2`; and repo-wide, `onnxruntime` appears only under
`face-service/`). The worker completes a full analysis job through the
service; face rows and person grouping are identical to pre-split
output (same models ⇒ same embeddings). With `PHOTO_FACE_SERVICE_URL`
unset, the worker's face stage is unavailable by construction — that
behavior is completed in Phase 3A (idle, not error).

### Hands-on validation

1. Targeted: `.venv/bin/pytest backend/tests/test_face_client.py
   backend/tests/test_ai_worker_preview_miss.py
   backend/tests/test_ai_worker_reuse.py
   backend/tests/test_burst_integration.py backend/tests/test_analysis.py`.
2. Full gate: `bash run_all_tests.sh` — green (ruff, OpenAPI check,
   suite, frontend).
3. `docker compose build && docker compose up -d` (the face-service
   comes up from its new compose entry); a full
   `docker compose run --rm ai-worker photo-server ai-worker --once`
   through it; diff a freshly analyzed photo's face rows against a
   pre-split baseline (same models ⇒ identical embeddings);
   `docker run --rm <ai-worker image> pip freeze | grep -i
   "onnxruntime\|opencv"` → empty.

### Handoff

After 2B: the photo server has no face code, no GPU, and no model
files; `PHOTO_FACE_SERVICE_URL` / `_TOKEN` (timeout 120 s) are the only
face-related server settings; the face-service compose entry exists
(GPU reservation, read-only `/models` bind); the worker is remote-only.
With the URL *unset* the face stage still fails the job (today's
failure path) — Phase 3A turns that into a clean idle.

---

## Phase 3A — Worker gate, backlog drain, dispatcher bound

This phase makes "the server works without AI, and the backlog drains
once AI is configured" true on the worker side, and gives the
dispatcher its bound.

### Context manifest

Read in full: `backend/src/photo_server/ai_worker.py` (420),
`backend/src/photo_server/config.py` (89),
`backend/tests/test_ai_worker_preview_miss.py` (352),
`backend/tests/test_ai_worker_reuse.py` (435),
`backend/tests/test_burst_integration.py` (326).
Read by range only: `backend/src/photo_server/catalog.py:1160-1230`
(`claim_ai_job` / `finish_ai_job` — reference only; never edited by
this phase).

Create / modify: `ai_worker.py`, `config.py`, the three worker test
files. Diff-only artifacts: none (no OpenAPI change).

### Changes

- **Not configured** (`ai_base_url` or `face_service_url` empty): never
  claim. Sleep 10 s per loop iteration; log a state-transition line
  (once) and a slow heartbeat (at most every 60 s): `AI services not
  configured; N analysis jobs waiting`. `ai-worker --once` returns
  `{"status": "idle", "reason": "ai-not-configured"}`.
  **This is where the empty default lands:** `Settings.ai_base_url`
  changes from Phase 1's placeholder default
  (`http://ollama:11434/v1`) to `""`; `face_service_url` has been
  default-empty since 2B. Compose sets both explicitly, so standard
  deployments are unaffected — an operator opting out of AI simply
  leaves the URLs empty.
- **Configured, probing:** face service `GET /health` (5 s) and VLM
  `GET {base}/models` (5 s), results cached 30 s per process. Jobs are
  claimed only when both are healthy (a job always runs both stages).
  If either is unhealthy: backoff sleep (10 s doubling to 120 s), no
  claim, transition-logged. Probes must never raise out of the loop.
- **In-flight bound:** the loop maintains at most
  `PHOTO_AI_WORKER_CONCURRENCY` (new setting, default **1**, 1–8)
  analyses in flight, claiming as slots free. At 1 this is today's
  single-consumer loop; the job rows' 1800 s lease already makes
  crashed in-flight jobs reclaimable.
- **Failure classification mid-job:**
  - *Service-unavailable class* (connection error, timeout, 429 with
    `Retry-After`, 502–504, from either service): the job goes back to
    **`pending`** via the existing
    `finish_ai_job(asset_id, "pending", error)` (attempts already
    incremented at claim; lease cleared). The backlog survives outages
    and the same job is claimed again when the services recover — this
    is what "the backlog should be pushed through them" means
    operationally. `stageFailures` still count the attempt.
  - *Any other failure* (4xx, model/decode errors, invalid VLM JSON):
    `failed` with `stage` and error, exactly as today; recovery is the
    existing manual requeue ("Analyze again", `photo-server analyze
    --asset`).
- **No DB migration.** The backlog is the existing `pending` `ai-v1`
  rows; no new status values, no schema change, no migration file. No
  cap on pending-wait: a backlog may wait indefinitely (it is just
  `jobs` rows, and `analysisPending` in `/health` makes the size
  visible).

### Tests

- Gate and dispatcher:
  - both URLs empty → `run_once` claims nothing (job row untouched:
    `pending`, attempts 0), returns the idle result;
  - configured + stub-healthy → claims and completes (existing
    happy-path tests, now with the gate in place);
  - configured, face service 503 at the gate → no claim, backoff; then
    healthy → claim (two-loop test);
  - mid-job: face service dies after the fingerprint stage → job row
    `pending` again with attempts 1 and the error text; next loop with
    the service restored → the job completes;
  - VLM 4xx (bad key) → job `failed` (not requeued);
  - bound 2 → two jobs in flight, third claimed only after one finishes
    (stubbed services; assert concurrent in-flight rows in the jobs
    table).

### Definition of done

Starting the stack with no AI env vars: everything works;
`analysisPending` grows with imports; no AI process consumes GPU; no
job ever becomes `failed` because of missing services. Then with the AI
env vars set: within one loop the worker starts claiming and the
backlog drains in `asset_id` order; a killed `face-service` mid-drain
leaves jobs pending (not failed) and the drain resumes on restart.

### Hands-on validation

1. Targeted: `.venv/bin/pytest` over the three worker test files.
2. Full gate: `bash run_all_tests.sh`.
3. Live: (a) start with the AI env vars removed — import a small batch,
   confirm `analysisPending` grows and nothing fails; (b) set the env
   vars and `docker compose up -d ai-worker` — watch the backlog drain
   and confirm claim order is `asset_id`; (c) `docker compose kill
   face-service` mid-drain — confirm jobs return to `pending` and
   resume on restart; (d) stop Ollama — the same requeue behavior;
   (e) set `PHOTO_AI_WORKER_CONCURRENCY=2` briefly and confirm two
   in-flight rows without any error (then restore 1); (f) saturate the
   face-service deliberately (flood its queue) and confirm the
   hosted-style pacing: 429 + `Retry-After` on the wire, dispatcher
   backing off in its log, drain resuming without operator action.

### Handoff

After 3A: the worker idles cleanly when AI is unconfigured, drains the
backlog when it is, requeues (never fails) on service-unavailable
errors, and keeps its in-flight bound. The empty-URL default now lives
in `Settings` (compose still sets the URLs explicitly). `/health` still
shows no service visibility — that is 3B/3C.

---

## Phase 3B — Backend service visibility (`/health`)

### Context manifest

Read in full: `backend/src/photo_server/api.py` (801),
`backend/src/photo_server/api_schemas.py` (723),
`backend/tests/test_api_contract.py` (2,077 — the largest single read in
this plan; read it once and edit surgically: only the `/health` fixture
block and the health assertions change),
`backend/tests/test_api_schemas.py` (1,059 — same),
`backend/src/photo_server/face_client.py` (~150 — reuse its probe and
identity-check logic; do not duplicate them).

Create / modify: `api.py` (the health endpoint runs the probes),
`api_schemas.py` (four new `StrictBool` fields on `HealthOut`),
`face_client.py` (a timeout parameter so the API can reuse the existing
identity-checked probe at its 3 s budget while the worker keeps its 5 s
default), and surgical edits to the two test files.

Diff-only artifacts: `openapi/openapi.json` — regenerated with
`scripts/dump_openapi.py`; and `frontend/web/src/api/generated/` —
regenerated with `npm run generate:api`. Read only the diffs (expected:
four new properties on the `HealthOut` schema and the corresponding four
generated fields). `scripts/check_openapi.py` and frontend `check:api` are
the gates, not reads.

**Sizing note: this is the ceiling case of the plan** (~5,200 lines of
full reads, the bulk of it the two test files; every edit is surgical).
It fits a 100k model with targeted test runs. If a smaller model
carries it, split 3B into **3B1** (`api.py` + `api_schemas.py` + spec
regeneration) and **3B2** (the two golden test files) — the split point
is clean because the spec change is self-describing.

### Changes

- **`/health` extension (`api.py`, `HealthOut`).** Add four fields:
  `aiSemanticConfigured`, `aiSemanticReachable`, `aiFaceConfigured`,
  `aiFaceReachable` (all `StrictBool`). The API process runs its own
  short (3 s) probes, cached 30 s; a probe failure makes the field
  `false`, never the endpoint. `configured` is a config check;
  `reachable` is the probe (the face probe also checks the identity
  match, so a model drift shows as unreachable). This is the one
  deliberate extension of the artifact contract (global invariant).

### Tests

- `/health`: `HealthOut` round-trip for all four flag combinations;
  `test_api_contract` golden for `/health` updated; regenerate
  `openapi/openapi.json` (`scripts/dump_openapi.py`) so
  `check_openapi.py` passes; `test_api_schemas` round-trip.

### Definition of done

`/health` reports all four flags correctly in every combination
(configured × reachable, for both services); the regenerated spec
passes `check_openapi.py`; full suite green; the frontend still builds
against its current generated types (the new fields are additive — the
frontend consumes them in 3C).

### Hands-on validation

1. Targeted: `.venv/bin/pytest backend/tests/test_api_contract.py
   backend/tests/test_api_schemas.py`.
2. `.venv/bin/python scripts/dump_openapi.py && git diff
   openapi/openapi.json` (only the `HealthOut` properties) and
   `.venv/bin/python scripts/check_openapi.py`.
3. Full gate: `bash run_all_tests.sh`.
4. Live: `docker compose up -d`; `curl -s localhost:8000/health` — with
   AI configured, both reachable flags true; `docker compose kill
   face-service` → `aiFaceReachable` flips to false within 30 s; unset
   one AI URL and restart → its `Configured` flag is false.

### Handoff

After 3B: `HealthOut` carries the four service-visibility fields, the
OpenAPI and generated-client artifacts are synchronized, and the API
process probes the services itself (3 s probes, 30 s cache; the face probe
includes the identity check). The web UI has not consumed the fields yet —
that is 3C.

---

## Phase 3C — Frontend service visibility

### Context manifest

Read in full: `frontend/web/src/api/types.ts` (96),
`frontend/web/src/features/photo/PhotoInspector.tsx` (127),
`frontend/web/src/features/shell/AppShell.tsx` (102),
`frontend/web/src/api/client.ts` (264 — how `health` flows to the
inspector).

Diff-only artifact: `frontend/web/src/api/generated/` (~3,300 lines) —
already synchronized in Phase 3B; read only the `git diff` to confirm the
four new fields remain the generated `HealthOut` surface.
`npm run check:api` is the gate, not a read.

Create / modify: `types.ts` (the hand-written `Health` type gains the
four flags) and `PhotoInspector.tsx` (one conditional string).

### Changes

- **Frontend.** `Health` type gains the four flags.
  `PhotoInspector.tsx`: the `pending` branch splits — AI not configured
  ⇒ "AI services are not configured; this photo will be analyzed once
  they are"; configured ⇒ today's "Waiting for the background GPU
  worker". (One conditional string; the inspector already receives
  `detail.analysis` and the shell already carries `health`.)

### Tests

- `npm run check` (tsc) passes; `npm run check:api` passes against the
  regenerated spec; `npm test` (the existing vitest suite) unchanged and
  green; visual spot-check of the inspector in both states.

### Definition of done — the full "optional AI" acceptance

This is the last of the 3x group; the full requested behavior lands
here:

- Starting the stack with no AI env vars: everything works;
  `analysisPending` grows with imports; no AI process consumes GPU; no
  job ever becomes `failed` because of missing services; the inspector
  says "AI services are not configured…".
- With the AI env vars set: the backlog drains in `asset_id` order;
  `/health` and the inspector reflect each transition (configured /
  reachable / waiting).
- A killed `face-service` mid-drain leaves jobs pending (not failed)
  and the drain resumes on restart; the `Reachable` flag and the
  inspector wording follow within the 30 s cache.

### Hands-on validation

1. `cd frontend/web && npm run generate:api && git diff
   src/api/generated` (only the four new fields) && `npm run check:api
   && npm run check && npm test`.
2. Full gate: `bash run_all_tests.sh` (the last 3x phase — the complete
   suite must be green).
3. Browser: with no AI env vars, open a freshly imported photo in the
   inspector — the "not configured" wording; with AI set and a job
   pending — the "waiting" wording; `docker compose kill face-service`
   — the wording and `/health` follow within 30 s.

### Handoff

After 3C: Phases 1–3 are complete — the VLM is any OpenAI-compatible
URL, faces run only in the face-service, AI is fully optional (idle
worker, draining backlog, requeue-on-outage), and both services' state
is visible in `/health` and the UI. Remaining: Phase 4 (docs/ops) and
Phase 5 (scanner consolidation).

---

## Phase 4 — Documentation, env migration, ops

### Context manifest

Read in full: `README.md` (380), `.env.example` (57),
`docs/rollout-semantic-reuse.md` (61), `run_all_tests.sh` (287),
`compose.yaml` (157 — for the exact env lines in the matrix).
Read by range only: `self_hosted_photo_organizer_design.md` §27
Distributed Compute (~1,195–1,229) and its AI mentions in §39 Suggested
Implementation Order (~1,660–1,690) — the 1,876-line design doc is never
read in full.

Create / modify: `README.md`, `.env.example`, the design-doc ranges,
`docs/rollout-semantic-reuse.md`, `run_all_tests.sh`.
Diff-only artifacts: none.

### Changes

- `README.md`:
  - Architecture paragraph: AI is now optional, swappable intelligence —
    a VLM endpoint (any OpenAI-compatible target) plus a face-service
    microservice — consumed by the `ai-worker` dispatcher; the server is
    fully functional without either, and choosing a provider or a
    compute location is configuration.
  - **Configuration matrix** with exact env lines:
    1. *No AI:* both URLs empty. Full library, no analysis, no GPU use
       by this project.
    2. *Local AI (current default):* everything in this compose project;
       `PHOTO_AI_BASE_URL=http://ollama:11434/v1`,
       `PHOTO_FACE_SERVICE_URL=http://face-service:8901`, shared
       `PHOTO_FACE_SERVICE_TOKEN`.
    3. *Remote / hosted AI:* face-service (and Ollama, if not hosted) in
       the other machine's compose project, bound to the trusted-LAN
       interface with TLS (the auto-generated CA) and the shared token;
       this project drops those services and points the URLs across the
       network; `PHOTO_AI_BASE_URL` may point at any hosted
       OpenAI-compatible endpoint instead (privacy note:
       with a hosted VLM, images leave the local network — the operator's
       call; the VLM prompt still forbids identifying people).
    4. *Concurrency knobs:* `PHOTO_AI_WORKER_CONCURRENCY` and
       `PHOTO_FACE_SERVICE_CONCURRENCY` (both default 1); document the
       VRAM arithmetic for turning them up (even after Phase 5 the box
       runs 1× AdaFace + 1× Qwen3-VL, which leaves no headroom for a
       second of anything on the 3090).
  - Migration note for existing `.env`: `PHOTO_AI_OLLAMA_URL` →
    `PHOTO_AI_BASE_URL` (add `/v1`), `PHOTO_AI_CONTEXT_TOKENS` removed
    (Ollama default 4096; use `PHOTO_AI_EXTRA_BODY` for other values),
    `PHOTO_FACE_MODELS_DIR`/`PHOTO_FACE_DETECTION_THRESHOLD` now set for
    the face-service, AI is off until the URLs are set.
- `self_hosted_photo_organizer_design.md`: update §27 Distributed
  Compute (and the AI mentions in §39) to match (bookkeeping server /
  optional intelligence services; concurrency ownership split; burst
  reuse stays semantic-only per Q6).
- `docs/rollout-semantic-reuse.md`: note the Q6 decision — the policy
  continues to govern the semantic stage only; the face stage runs on
  every claimed job.
- `.env.example`: new names, empty-by-default AI URLs (comment block
  explaining the matrix), token placeholder.
- `run_all_tests.sh`: gains a face-service section — ruff over
  `face-service/` and `pytest face-service/tests/` (the suite is
  in-process `TestClient` with the analyzer monkeypatched, so no GPU or
  compose service is needed); the venv instructions gain
  `pip install -e 'face-service[dev]'` alongside
  `pip install -e 'backend[dev]'`.

### Definition of done

A fresh checkout plus any one row of the configuration matrix
reproduces that configuration; the README's GPU note now names the
face-service as the AdaFace host and states the concurrency split; no
documentation mentions Ollama's native API.

### Hands-on validation

1. `bash run_all_tests.sh` — including the new face-service section
   (ruff + pytest, no GPU needed).
2. Fresh-clone walkthrough of matrix rows 1–3; on the two-machine row,
   the TLS check: the face service serves HTTPS with the auto-generated
   CA (client verifies; wrong CA is rejected) and refuses
   unauthenticated cross-machine callers; cross-machine `curl` of
   `/health`.

### Handoff

After 4: choosing a topology (none / local / remote / hosted) is a
documented config exercise, not a code change; the `.env` migration is
documented; `run_all_tests.sh` covers the new folder. Remaining:
Phase 5 (face-scanner consolidation, cross-repo).

---

## Phase 5 — face-scanner consolidation (cross-repo)

The sibling `face-scanner` runs its own YuNet + AdaFace pipeline on the
same box — the second AdaFace in the current 13.1 GB footprint. Once the
face-service exists, the scanner's inference is replaced by calls to it,
so the box runs **one** AdaFace session instead of two and the freed
VRAM is headroom for the future knobs (VLM parallelism, a bigger model).

### Sizing and manifest

Phase 5 is a **project, not a single-model phase**: the scanner-side
work lives in the face-scanner repository, which gets its own brief
document there (with its own manifest, sized by the same rules). The
photo-server side of it — in this repository — is small:

Read in full: `face-service/src/face_service/app.py` (~230),
`face-service/tests/test_app.py` (~250), `compose.yaml` (157).
Create / modify (only if the contract needs extending): new endpoint(s)
in `face-service` (~50–100 lines each) with tests; the scanner repo's
diff is out of scope for this repository's phase.

### Changes

- **face-service (this repo):** confirm the contract covers the
  scanner's workload: `POST /v1/faces/analyze` (JPEG in → boxes +
  embeddings out), `/health`, bearer auth, TLS as in Phase 2A. If the
  scanner needs anything beyond that (a batch endpoint, different
  detection thresholds, raw aligned crops in addition to embeddings),
  those endpoints are added here first, in this repo.
  *Open item to confirm in the face-scanner repo before implementing:
  its exact request shape and whether it consumes only embeddings or
  also crops/boxes directly.*
- **face-scanner (its repo, its change history):** remove its
  in-process detector/aligner/embedding stack and call the
  face-service with the shared token (same compose network today:
  `http://face-service:8901`; its cross-machine form later). Its model
  directory stays as the *source* of the verified files the
  face-service loads — the scanner stops *running* them.
- **compose (this repo):** no shape change; the face-service keeps its
  single read-only model bind. The box's GPU footprint becomes
  1× AdaFace + 1× Qwen3-VL.

### Definition of done

The GPU process list shows exactly one AdaFace session on the box; the
scanner produces identical face results through the face-service as it
did in-process (diff on a fixed sample set); VRAM headroom is
measurably larger than the pre-consolidation 13.1 GB footprint.

### Hands-on validation

Point the face-scanner at the face-service and run both consumers on
the same sample set — results diff clean; `nvidia-smi` shows one AdaFace
process; record the freed VRAM against the 13.1 GB baseline.

### Handoff

Final state of this plan: 1× AdaFace (in the face-service) + 1×
Qwen3-VL on the box; the face-service is the shared face-inference
endpoint for both consumers; the photo server is pure bookkeeping. No
further phases in this document — anything beyond is a Follow-up.

---

## Cross-cutting notes

- **Risks.**
  - `json_schema` support on arbitrary OpenAI-compatible servers is
    uneven — the `json_object` fallback ladder plus pydantic validation
    bounds the blast radius (one malformed response fails one job, as
    today).
  - Dropping `num_ctx` from the standard path: Ollama's default (4096)
    matches the old explicit value, but a different provider may have a
    smaller default context — watch for truncated VLM output on the first
    hosted-provider run; the extra-body escape hatch is the fix.
  - Backlog drain at the default bound is single-stream at both ends:
    for a large first-ever backlog this is a wait, not a failure
    (throughput = today's per-image rate). Raising the bound is the
    documented escape hatch, gated on hardware headroom.
  - The face service and Ollama share the GPU; both already fit (13.1 GB
    total). If Ollama later loads a bigger model, revisit before turning
    any concurrency knob up.
- **Settled decisions (no longer open).**
  - **Q1 — Transport:** mirror the hosted provider APIs (OpenAI/
    Anthropic): bearer token, OpenAI-shaped JSON errors, 429 +
    `Retry-After` saturation semantics, TLS for cross-machine use
    (self-signed CA auto-generated by the service and verified by the
    client, or terminated at the existing reverse proxy). Plain HTTP
    stays acceptable docker-internal.
  - **Q2 — Local in-process face mode:** removed; the face-service is
    the sole home of face intelligence (a global invariant).
  - **Q3 — Name/port:** `face-service`, port 8901 — confirmed.
  - **Q4 — Probe cache:** 30 s — confirmed.
  - **Q5 — face-scanner:** consolidated into the face-service (Phase
    5); the box runs one AdaFace, not two.
  - **Q6 — Burst deduplication scope:** the `burst-reuse-v1` policy
    stays semantic-only; face inference runs on every image. Rationale:
    face inference is a fraction of a second against the VLM's long
    pole (already deduplicated per burst), and per-image detection keeps
    each frame's boxes and embeddings frame-specific. `force_full`
    remains the per-photo re-analysis opt-in (unchanged from today).
  - **Q7 — Repo layout:** the AI service code lives in its own
    top-level folder (`face-service/`), not under `backend/`; the photo
    server package keeps only the thin `face_client.py` and the
    dispatcher, mirroring the existing `upload_client.py` split.

## Follow-ups (deliberate deferred changes)

- Raising the client in-flight bound and the face-service concurrency
  once the box has headroom (or a second GPU machine joins) — the 429
  path makes this safe either way.
- Ollama `OLLAMA_NUM_PARALLEL` > 1 for the VLM (the long pole) — only
  meaningful with the matching client bound.
- Per-provider VLM capability detection (auto-detect `json_schema`
  support once instead of falling back per request).
- If the face-scanner's workload grows (batches, crops, multiple
  threshold profiles), dedicated face-service endpoints rather than
  overloading the analyze contract.
