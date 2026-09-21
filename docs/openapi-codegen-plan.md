# Implementation plan: typed API endpoints for OpenAPI client code generation

## Goal

Make `/openapi.json` the complete, authoritative contract for every JSON
endpoint — typed request **and** response models on each operation — so that
clients (the web app, `upload_client.py`, future desktop shell) can be
**generated** from the spec instead of being maintained in parallel with the
backend. Today the web client hand-mirrors ~40 response shapes in
`frontend/web/src/api/types.ts`, and every backend dict-literal change must be
repeated by hand in TypeScript with no check that they agree.

After this work:

1. Every JSON operation in the spec has a request schema and a response schema
   (no empty `{}`).
2. `openapi/openapi.json` is a checked-in artifact, and a CI check fails if it
   drifts from the code — so "regenerate the client" is always reproducible.
3. `src/api/types.ts` is reduced to non-contract types (client-internal shapes
   only); everything else comes from generated code.
4. **No wire-format change on any endpoint.** This is a declaration effort,
   not a rename. Goldens prove it.

## Current state (what exists, what is missing)

**Already typed (request side) — keep as-is:**

- `api.py`: `UploadFileDeclaration`, `UploadBatchRequest`, `ProcessingRequest`,
  `AnalysisRequest`, `PersonNameRequest`, `PersonMergeRequest`,
  `FaceMoveRequest` (all `to_camel` alias models)
- `browsing.py`: `BrowseQuery`, `OperationRequest`, `UserStatePatch`,
  `AlbumPatch`

**Untyped (the work): every response, plus a few inputs.**

| Area | Endpoint pattern | What is returned today |
|---|---|---|
| Catalog | `GET /assets`, `GET /library/assets` | dict list / page dict from `asset_summary()` + `browse()` |
| Catalog | `GET /assets/{id}` | **inline dict assembled in the endpoint** (document + technical + status) |
| Catalog | `GET /assets/{id}/burst` | `burst_detail()` dict |
| People | `GET /people`, `GET /people/{id}` | dict with `sampleFaces`/`faces` built in SQL (jsonb) + Python |
| Upload | `POST/GET /upload-batches`, `GET /upload-batches/{id}`, `DELETE`, `PUT .../files/{fid}`, `POST .../seal`, `POST .../retry`, `GET /upload-queue` | `describe_batch()` dicts, receipt dicts, `queue_counts()` + gate dict |
| Mutations | `PATCH` user-state/metadata, `DELETE`/`POST restore` asset, album CRUD, `POST /processing`, `POST /analysis`, burst representative | `mutation_result()` dict, Album documents (include nested `mutation`), queue-count dicts |
| People ops | `PATCH /people/{id}`, `POST /people/{id}/merge`, `POST /faces/move` | operation result dicts from `commit_face_operation()` |
| Misc | `GET /health`, `POST /assets/{id}/preview/retry`, `POST /maintenance/verify` | inline dicts |

Binary endpoints (`original`, `preview`, `thumbnail`, face thumbnail) return
`StreamingResponse`/`FileResponse`/`Response` and sometimes a 202
`{"status":"pending"}` JSON body. The client uses these as **URLs** (`<img
src>`, `<a download>`, XHR upload), so they need only a documented media type
plus the 202 shape — no response model.

## Load-bearing design decisions (finalized)

These are settled design decisions, not open questions — every phase below
assumes them as fixed.

1. **New module `backend/src/photo_server/api_schemas.py`** holds all
   response models. The existing request models stay where they are
   (`api.py`/`browsing.py`) for the duration of this plan and are imported
   from here in place; consolidating them into `api_schemas.py` as one
   canonical home is a separate refactor and out of scope here.
2. **Response models use the same config as inputs:**
   `ConfigDict(alias_generator=to_camel, populate_by_name=True)` — fields
   snake_case in code, camelCase on the wire (matches `DurableModel` precedent
   and the existing hand-written TS types verbatim).
3. **Fail loudly on actual deviation.** No `extra="allow"` on response
   models (the Pydantic `ignore` default) — verified against this
   FastAPI/Pydantic install: extras in the returned dict are silently
   dropped, defaults are filled, missing required fields →
   `ResponseValidationError` (500). That behavior is *the design, not a
   side effect*: `response_model=` is both the declaration and the
   enforcement, so if a service dict actually diverges from the declared
   model — a required field no longer present, a field no longer a dict —
   the endpoint 500s instead of silently drifting the client contract; the
   tests below make that a test failure, not a production surprise. The one
   class of field exempt from value validation is `dict[str, Any]`
   (Decision 4): the container is still verified (must be a dict with
   string keys — verified in this install, no key coercion), but its
   values are never evaluated and pass through unchanged, so such a field
   500s only when the field itself stops being a dict, which is a real
   divergence. Every other field of every model is fully verified. The
   one narrowing of "fully verified" was settled in Phase 1b: JSON has a
   single ``number`` type and every consumer treats ``1`` and ``1.0``
   identically, so numeric fields are declared ``StrictFloat`` (which in
   this pydantic accepts both int and float input). Golden fixtures are
   recorded *as emitted* (a faithful byte-level transcript; only dict keys
   are sorted) and the *comparison* additionally equates integral-valued
   floats with integers (``normalize`` in
   ``backend/tests/test_api_contract.py``) — that leniency never accepts a
   different value. Recording is deterministic because ``StrictFloat``
   converges every producer spelling to a float rendering. The one-time
   Phase 1b byte transition (the list-path jsonb-normalized confidence
   spelling 1 now emitted as 1.0) is pinned in the re-recorded
   ``phase1b.json``.
4. **Free-form data stays free-form — `dict[str, Any]`, used judiciously.**
   The EXIF `metadata` block (content varies by camera/lens maker; there is
   no stable schema), the normalized `technical` block, and queue-job
   `result` blobs are record-keeping data: typed `dict[str, Any]`
   (`{"type": "object", "additionalProperties": true}` in the spec,
   `Record<string, unknown>` in TS) — container enforced, values not
   (Decision 3). That is a deliberately minimal use of the escape hatch:
   only fields whose content is genuinely open-ended get it, never a whole
   response, and a field whose wire value can be null declares
   `dict[str, Any] | None` (bare `dict[str, Any]` rejects null — verified
   in this install). EXIF is stored for record-keeping now, not consumed:
   searching by aperture / shutter speed / focal length is a separate
   future project (its own normalization and indexing, not a response
   model) that this plan neither blocks nor commits to — so no typed
   `TechnicalFields` model here.
5. **Checked-in spec + drift check.** `openapi/openapi.json` is committed;
   `scripts/check_openapi.py` regenerates from `create_app()` and diffs.
   The spec is a *projection, not a second source of truth*: it is never
   hand-edited, the drift check enforces code → spec only, and the Pydantic
   models in `api_schemas.py` remain the single source of truth for the
   contract. The committed copy is what makes codegen stable between backend
   releases and what breaks the habit of "the endpoint is what the dict
   says, not what the spec says" — today's served spec is too incomplete to
   act as a contract, so the dict is the de-facto truth; after this plan the
   spec is a complete, enforced mirror of it.
   **Why commit what regenerates in one command:** the value is the *diff*,
   not the file. A committed spec turns every wire change into a reviewable,
   version-diffable, shareable contract artifact, and it is the anchor the
   drift check needs — an on-demand-only file has nothing to drift
   *against*, so the check degrades to a pass/fail boolean. The same logic
   commits `src/api/generated/` (Phase 5a): it is small (~2k lines), `tsc`
   needs it present on a fresh clone, and the 0.x generator can change its
   output between bumps — committing it (with the exact version pinned)
   turns each bump into a visible, reviewable client diff instead of a
   silent build change. The middle ground — commit the spec, generate the
   client at build time via a `prepare` script — is deliberately not taken:
   it removes the client diff from review and adds a build step whose output
   varies with the generator version.
6. **Generator: `hey-api`** (`@hey-api/openapi-ts`; manual at
   heyapi.dev/docs/openapi/typescript/get-started) — the current de-facto
   standard (used by Vercel, OpenCode, PayPal; 5.4k stars). Unlike
   `openapi-typescript` (a types-only `.d.ts`), it generates the *full* typed
   client: one function per operation plus the request/response types, and
   the `@hey-api/client-fetch` plugin bundles its zero-dependency fetch
   runtime into the generated output (`bundle: true`, the default) — so the
   client still ships **no npm runtime dependency** and stays `tsc -b`
   friendly. It is 0.x ("initial development"): pin the exact version
   (0.99.0 at the time of writing), read the published migration notes on
   upgrade, and run it under Node 22+. The thin runtime layer — `ApiError`
   normalization, XHR upload progress, the durable mutation journal,
   `apiUrl()` for binary URLs — stays hand-written: the generated client
   accepts a custom `fetch` (via its `createClientConfig()`/`setConfig`
   hooks), which is where `ApiError` plugs in, and everything else goes
   through the generated per-operation functions.
7. **Wire format is frozen.** Response models are written to match the dict
   literals *exactly* as emitted today (same keys, same nullability, same
   nesting), camel aliases included. The golden response fixtures (mechanism
   and seed in Phase 0, one golden section added per phase thereafter) are
   the proof obligation for every later phase.

### Global invariants (every phase, non-negotiable)

- Golden fixtures grow **by appending new cases only**: each phase records
  goldens for its own endpoints; every previously recorded case stays
  byte-identical (status, headers of interest, body) and keeps being checked
  in every later phase.
- Full existing suite green: ruff, backend unit + integration
  (`PHOTO_RUN_INTEGRATION=1`), frontend `npm run check` + `vitest`.
- Error semantics unchanged: 404s, 409 `LibraryError`, 422 validation,
  202 + `Retry-After` on derivative/face endpoints.
- `openapi/openapi.json` regenerated and committed in the same change; its diff
  contains only the schemas added in that phase (Phase 4b's diff additionally
  contains `operationId` fields — spec metadata, never wire format).
- No client-visible behavior change in any phase except the last frontend
  ones (5a/5b), which swap the client's *source of types* without changing
  what it sends.

## Rollout phases

Each phase is independently shippable and sized for a single **100k-context**
implementation pass. The sizing budget that produced this split:

- **≤ ~1,500 lines of source per phase checklist** (≈ 15–17k tokens at
  ~10–12 tokens/line), with a hard ceiling of ~1,800 lines (20k tokens =
  20% of the window) for phases whose checked-in state is legitimately
  big — Phase 3b lands at ~1,630 for exactly that reason. One declared
  exception: Phase 5a at ~2,650 lines (~29% of the window, but ~2k of it
  is generated code that is skimmed, not reasoned over). Phase 5b stays
  small because its sweep is driven by the type-checker's error list, not
  by reading the import-only files in advance (see that phase).
- **Counting rules, applied to every checklist below:** a file this
  phase *edits* is read whole; a large file is read only for the cited
  sections; the golden contract-test file contributes its helpers + the
  latest section (~150 lines), never all prior sections — it is
  append-only by design, and prior sections are verified by the test
  runner, not by reading; golden fixture JSON bodies are never held
  (the runner diffs them); `openapi/openapi.json` is held only as the
  per-phase diff (Phase 0: the path list plus a spot-check of the
  empty-schema shape); newly written code *counts*. The finished spec
  grows from today's 2,213 lines to roughly 3,500 — no phase holds it
  whole; the cumulative read at Phase 4's wrap-up is a human review, not
  model context.
- **≤ 6 endpoint rewires per phase**, up to 9 when every endpoint in the
  phase shares one or two shapes from the same source family (Phases 2 and
  3b).
- **Golden additions limited to the phase's own endpoints**, so each pass
  only seeds the scenarios it is typing — no phase has to hold the whole
  API's behavior in context.

The working set thus consumes at most ~20% of the 100k window (Phase 5a
declared excepted, ~29%, the bulk of it generated code); the rest is
budget for reasoning, tool I/O, test output, and iteration — the parts
that actually overflow a small context. Land in order: Phases 0–4b are
backend-only (1b depends on 1a; 3a and 3b depend on 1a; 2 is independent),
Phases 5a→5b are the client switch over, Phase 6 is optional. The union of
the per-phase
golden sections covers the same request matrix (assets, bursts, people,
albums, upload batches, queue ops, face ops, error cases) as a single
monolithic scenario would.

| # | Phase | Adds to spec | Depends on |
|---|---|---|---|
| 0 | Baseline: spec artifact + golden mechanism (small seed) | (no schema change) | — |
| 1a | Asset browse & detail (assets, browse, burst) | 14 response shapes, 4 endpoints | 0 |
| 1b | People reads (list + detail) | 4 response shapes, 2 endpoints | 1a |
| 2 | Upload & health endpoints | 8 response shapes, 9 endpoints | 0 |
| 3a | Albums (CRUD + restore) | 1 response shape, 6 endpoints | 1a |
| 3b | Asset mutations & queue operations | 3 response shapes, 9 endpoints | 1a |
| 4 | People/face ops + binary-endpoint docs | 4 result shapes + media types | 1a, 2 |
| 4b | Operation IDs on every route (spec-only) | `operationId` per operation | 4 |
| 5a | Frontend: hey-api client core (shims kept) | — | 0–4b |
| 5b | Frontend: type-import sweep (tsc-driven) + shim removal | — | 5a |
| 6 | Backend Python client + CI hardening (optional) | — | 5a |

---

### Phase 0 — Baseline: spec artifact, golden mechanism, small seed

**Goal:** make the current (partially typed) behavior a checked-in,
comparable artifact and prove the record/verify mechanism on a *small*
scenario — so every later phase can prove its own endpoints didn't move,
without any single pass having to hold the whole API in context.

**Changes**

- `scripts/dump_openapi.py`: imports `create_app()`, writes
  `openapi/openapi.json` (sorted keys, stable formatting).
- Commit `openapi/openapi.json`.
- `scripts/check_openapi.py`: runs the dumper in a temp path, diffs against
  the checked-in file; exit 1 on drift. (Also asserts, starting Phase 1a
  onward, that no JSON operation's 2xx response is the empty schema
  FastAPI emits for untyped operations — `content:
  {"application/json": {"schema": {}}}`; a real response schema is a
  `$ref` or an object with `properties` — add the assertion as a
  `--strict-coverage` flag now, wire it on in Phase 4.)
- Golden mechanism: a new `backend/tests/test_api_contract.py` (or a helper
  module it feeds) reusing the disposable-backend fixture style from
  `test_integration.py` (the `backend` fixture, `photo()`, and the
  `catalog_fixture()` seeding helper). A **fixed sequence of requests** is
  captured as `(method, path, status, body)` tuples and diffed against
  frozen JSON fixtures in `backend/tests/fixtures/api_golden/`.
  **In this phase the sequence is deliberately small** — just enough to prove
  deterministic recording: a couple of imported assets, `GET /assets`,
  `GET /library/assets`, `GET /assets/{id}`, `GET /health`, plus two error
  cases (404 unknown asset, 422 on `BrowseQuery`). **Each later phase
  appends a named golden section for its own endpoints** to the same
  sequence; previously recorded cases are never touched.
- Normalization is deliberate and is designed *once, here*: sort dict keys
  before comparison; timestamps and generated UUIDs that are
  non-deterministic are either pinned in the scenario (client-chosen IDs; a
  fixed clock is not available — pin IDs instead) or masked with a
  documented filter. Prefer pinning scenario inputs over output masking.
- `run_all_tests.sh`: add a section running `scripts/check_openapi.py`
  (spec drift) — cheap, no services needed.
- README note: "After changing any request/response shape: run
  `scripts/dump_openapi.py`, review the diff, regenerate goldens if the wire
  format intentionally changed."

**Tests:** the new contract test itself (first run records the seed fixtures
— reviewed before commit; run twice to prove determinism).

**Context checklist (~970 lines):** `api.py` (the four seed endpoints only
— `GET /assets`, `GET /library/assets`, `GET /assets/{id}`, `GET /health`
— ~50 of 558; the full endpoint inventory comes from the spec's path list,
not from holding the file), the freshly dumped `openapi/openapi.json`
(2,213 lines today — hold the path list, ~40 lines, plus a spot-check of
two or three `{"schema": {}}` 200-responses), `test_integration.py`
(fixture section only — `backend`/`photo()`/`catalog_fixture()`, 95 of
766), `run_all_tests.sh` (251), plus the new code being written
(`dump_openapi.py` ~40, `check_openapi.py` ~70, the contract-test
helpers + seed section ~260, golden fixtures skimmed once ~100).

**Definition of done:**
- `openapi/openapi.json` committed; `check_openapi.py` passes.
- `PHOTO_RUN_INTEGRATION=1 pytest backend/tests/test_api_contract.py`
  records then verifies the seed goldens deterministically (run twice).
- Full suite green.

---

### Phase 1a — Asset browse & detail (assets, browse, burst)

**Goal:** the asset side of the browse/detail graph — the client's
most-consumed shapes — exists in the spec. (The people side is Phase 1b; the
split exists so no single pass holds both the analysis/processing submodels
and the face-group SQL at once.)

**Models (new, in `api_schemas.py`), matching today's dict literals exactly:**

- `PhotoSummaryOut` — the 22 keys of `browsing.asset_summary()` (incl.
  `technical: dict[str, Any]`, `preview: PreviewStatusOut`, `thumbnailUrl`,
  `previewUrl`).
- `PreviewStatusOut` — `{status, error}` (status union
  `missing|pending|running|ready|failed|unavailable`).
- `BlobOut`, `UserStateOut`, `LocationOut` — mirror `models.py`
  (`Blob`, `UserState`, `Location`) for the response side.
- `ProcessingStatusOut` — `{jobType, status, attempts, error}` (one row per
  processing job).
- `AnalysisStatusOut` — the 12 keys of `catalog.analysis_status()`, with
  `result: AnalysisResultOut | None` and `faces: list[AnalysisFaceOut]`;
  `AnalysisResultOut`/`AnalysisFaceOut` mirror the current TS
  `AnalysisResult`/`AnalysisObject` exactly — `objects` is a typed
  `list[AnalysisObjectOut]` (`{name: str, count: int}`), *not* free-form:
  it takes the `dict[str, Any]` treatment only if the seeded golden shows
  object entries whose shape is genuinely open-ended (model any extra keys
  the golden reveals rather than widening the type).
- `AssetDocOut` — the manifest document shape (`schemaVersion, libraryId,
  assetId, revision, previousRevision, operationId, primaryBlobId, blobs,
  importedAt, captureTime, metadata: dict[str, Any], deletedAt, mutation:
  MutationOut | None`); `GET /assets` returns a list of these.
- `AssetDetailOut` — `GET /assets/{id}`: the `AssetDocOut` fields **plus**
  `technical`, `processing: list[ProcessingStatusOut]`, `analysis:
  AnalysisStatusOut`, `preview`, `userState`. The endpoint's inline dict
  becomes the composition of these.
- `BurstDetailOut`, `BrowsePageOut` — mirror `burst_detail()`,
  `catalog.browse()`.
- `MutationOut` — the `Mutation` document (needed here for
  `AssetDocOut.mutation`; reused in Phases 3a/3b).

**Endpoint rewiring (decorator-only):**

- `GET /assets` → `response_model=list[AssetDocOut]`
- `GET /library/assets` → `response_model=BrowsePageOut`
- `GET /assets/{asset_id}` → `response_model=AssetDetailOut`
- `GET /assets/{asset_id}/burst` → `response_model=BurstDetailOut`

Service-layer functions keep returning dicts (lowest-risk); the endpoint
decorator validates/serializes. (If a 500 surfaces, the model and the dict
disagree — that is the model being fixed, not the wire format, and the
goldens prove which.)

**Golden additions (this phase's section of the contract test):** several
assets including a burst of 3, user-state edits, a preview job in each
status, browses with filters; error cases: 404 unknown asset, 422 on
`BrowseQuery`, 404 "asset has no burst".

**Tests:** goldens unchanged and green (Phase 0 seed + this section); new
unit tests in `test_api_contract.py`-style unit file (no live DB needed)
validating each new model against representative real JSON captured from the
goldens; full suite.

**Context checklist (~1,350 lines):** `api.py` (the four asset-read
endpoints, ~40 of 558), `browsing.py` (full, 188), `catalog.py` (`browse`,
`list_assets`, `user_state`, `burst_detail`, `analysis_status`,
`processing_status`, `preview_status` — 268 of 2,372), `models.py` (157),
`metadata.py` (`technical_fields`, 109), `frontend/web/src/api/types.ts`
(266, field cross-check — the generated shapes must make every existing TS
field legal), the contract-test file (helpers + seed section, ~150), plus
the new code being written: `api_schemas.py` (~200) and this phase's
golden section (~60).

**Definition of done:** all four operations in the regenerated spec carry
their response schemas; goldens byte-identical (seed + new section); suite
green.

---

### Phase 1b — People reads (list + detail)

**Goal:** the people list/detail shapes in the spec; everything shared comes
from Phase 1a.

**Models (new, in `api_schemas.py`):**

- `PeoplePageOut` — mirrors `catalog.list_people()` (page wrapper + summary
  rows).
- `PersonSummaryOut`, `PersonDetailOut` — mirror the `list_people()` /
  `person_detail()` dicts, incl. `sampleFaces`/`faces` built in SQL (jsonb)
  + Python.
- `FaceRefOut` — the face-row shape used inside the above.
- Reuse from Phase 1a: `PhotoSummaryOut`, `PreviewStatusOut`, `BlobOut`,
  `UserStateOut`, `MutationOut` wherever a person response embeds asset data
  (verify the embedding depth against `person_detail()` at implementation).

**Endpoint rewiring (decorator-only):**

- `GET /people` → `response_model=PeoplePageOut`
- `GET /people/{person_id}` → `response_model=PersonDetailOut`

**Golden additions:** a named person with one or two faces (seeded in this
phase's scenario, so the `faces`/`sampleFaces` arrays are non-empty and
typed), 404 unknown person.

**Tests:** goldens unchanged and green (Phase 0 seed, Phase 1a section +
this section); unit tests for the new models; full suite.

**Context checklist (~1,040 lines):** `api.py` (the two people-read
endpoints, ~18 of 558), `catalog.py` (`list_people`, `person_detail` —
123 of 2,372), `models.py` (157), `api_schemas.py` (whole — this phase
edits it; ~250 after Phase 1a), `types.ts` (266, people-section
cross-check), the contract-test file (helpers + Phase 1a section, ~150),
plus the new code being written (4 models ~45, golden section ~30).

**Definition of done:** both operations schema-complete in the regenerated
spec; goldens unchanged; suite green.

---

### Phase 2 — Upload and health endpoints

**Models:**

- `UploadFileOut` — the 10 keys of `describe_batch()` file rows (status union
  from the frontend's `UploadFileStatus` is the source of truth; verify it
  against every status literal in `uploads.py` + `catalog.py` and use the
  same union).
- `UploadJobOut` — `{jobId, status, attempts, result: dict[str, Any] |
  None, error}` (`result` is a Decision-4 free-form blob: null until the
  job produces one; if present it must be a dict, its values unchecked).
- `UploadBatchOut` — `describe_batch()` shape (camel keys, `createdAt`/
  `sealedAt` as `str`).
- `UploadFileReceipt` — the two `receive_file()` return dicts share
  `{fileId, status, sha256, replayed}`.
- `BatchAbandonedOut` — `_delete_claimed_batch()` shape (`status: "deleted"`).
- `QueueCountsOut` — the 12 queue-count keys from `catalog.queue_counts()`.
- `UploadQueueStatusOut` — `QueueCountsOut` + `{uploadWorkers, uploadsActive,
  uploadsWaiting}`.
- `HealthOut` — today's merge: `{status, libraryId, assets, blobs, ...queue,
  ...gate, postgresBackupKey: str | None, postgresBackupAt: str | None}`.
  (Health currently spreads several dicts at the endpoint; the model is the
  first place the composite shape can be named — do **not** change the merge
  itself in this phase.)

**Endpoint rewiring:** `POST /upload-batches` (`UploadBatchOut`, 201),
`GET /upload-batches` (`list[UploadBatchOut]`) with its `limit` query param,
`GET /upload-batches/{id}` and `POST .../seal`, `POST .../retry`
(`UploadBatchOut`, noting seal/retry respond 202), `DELETE .../{id}`
(`BatchAbandonedOut`), `PUT .../files/{file_id}` (`UploadFileReceipt`),
`GET /upload-queue` (`UploadQueueStatusOut`), `GET /health` (`HealthOut`).

**Golden additions (this phase's section — the upload scenario the seed left
out):** a batch through its life (create → file PUT → seal → 202), a retry,
a discarded batch via `DELETE`; error cases: 404 unknown batch, and **add a
409 content-length-mismatch receipt** — it must remain a 409, not a 500,
after `response_model` is on.

**Tests:** goldens unchanged and green (seed, Phase 1a, Phase 1b sections +
this section); full suite.

**Context checklist (~1,350 lines):** `api.py` (upload + health section,
~61 of 558), `uploads.py` (targeted — `describe_batch` at line 113,
`receive_file` at line 158 including the 409 content-length path, and the
status literals; ~150 of 447), `catalog.py` (`queue_counts`,
`upload_batch`, `active_upload_batch_ids`, `retry_upload_batch` — 162 of
2,372), `service.py` (`backup_status`, 12), `config.py` (89, limits
referenced by 422s), `types.ts` (266, upload section), `api_schemas.py`
(whole — this phase edits it; ~320 after Phase 1b), the contract-test
file (helpers + Phase 1b section, ~160), plus the new code being written
(8 models ~75, golden section ~60).

**Definition of done:** all upload/health operations schema-complete in the
regenerated spec; goldens unchanged; suite green.

---

### Phase 3a — Albums (CRUD + restore)

**Goal:** the album operations schema-complete. Split from asset mutations /
queue operations (Phase 3b) so each pass owns one mutation family.

**Models:**

- `AlbumOut` — the committed album document: `DurableModel` fields
  (`schemaVersion, libraryId, albumId, revision, previousRevision,
  operationId, name, description, assetIds, deletedAt`) **plus**
  `mutation: MutationOut` (today `album.document()` includes it; the TS side
  never looked — keep it in the model so the wire is unchanged). Reuses
  `MutationOut` from Phase 1a.

**Endpoint rewiring:** `POST /albums` (`AlbumOut`, 201), `GET /albums`
(`list[AlbumOut]`), `GET /albums/{id}`, `PATCH /albums/{id}`,
`DELETE /albums/{id}` (DELETE-with-JSON-body stays), and
`POST /albums/{id}/restore` — all `AlbumOut`: for album entities
`state.mutation_result()` returns `album.document()` itself (verified in
`state.py`), so one model covers all six operations.

**Notes:**
- `DELETE` with a JSON body is a pre-existing (deliberate, journal-based)
  protocol; it applies here to `DELETE /albums/{id}` and in Phase 3b to
  `DELETE /assets/{id}`. Codegen clients must send it explicitly. Document
  it on the endpoint so generated clients make it visible, and confirm the
  Phase 5a/5b TS client still sends the body for DELETE.
- `list_albums`/`get_album` return `album.document()`, which also carries
  `previousRevision: null` for first revisions — model it as
  `int | None` accordingly (verify against catalog `_apply_album`).

**Golden additions:** album create / patch / delete / restore, plus 404
unknown album — the key one being **revision-conflict 409 on album patch**.

**Tests:** goldens unchanged and green (seed, Phase 1a, Phase 1b, Phase 2
sections + this section); unit tests for `AlbumOut`; full suite.

**Context checklist (~1,240 lines):** `api.py` (album section, 63 of
558), `models.py` (157 — `Album`, `Mutation`), `catalog.py`
(`get_album`, `list_albums`, `_apply_album` — 56 of 2,372), `state.py`
(41), `api_schemas.py` (whole — this phase edits it; ~320 after Phase 2),
`mutations.tsx` (121) + `types.ts` (266, album section), the
contract-test file (helpers + Phase 2 section, ~160), plus the new code
being written (`AlbumOut` ~15, golden section ~40).

**Definition of done:** all six album operations schema-complete; goldens
unchanged; suite green.

---

### Phase 3b — Asset mutations and queue operations

**Models:**

- `MutationResultOut` — `state.mutation_result()` for assets:
  `UserStateOut` fields (Phase 1a) + `{assetId, operationId, revision,
  deletedAt}`.
- `BurstRepresentativeOut` — `{burstId, representativeAssetId}`.
- `QueueResultOut` — the shared `queue_processing()` shape:
  `{assets, jobsQueued, jobsAlreadyQueued, jobsAlreadyRunning,
  jobTypes: list[str]}` (used by `POST /processing` and `POST /analysis` and
  `POST /assets/{id}/analysis/retry`).
- Reuse from Phase 1a: `PreviewStatusOut` for `POST /assets/{id}/preview/retry`.

**Endpoint rewiring:** `PATCH /assets/{id}/user-state` +
`PATCH /assets/{id}/metadata` (shared `MutationResultOut` — the
dual-decorator pattern stays), `DELETE /assets/{id}` (body
`OperationRequest`; the DELETE-with-body note from Phase 3a applies),
`POST /assets/{id}/restore`, `POST /assets/{id}/burst/representative`,
`POST /processing`, `POST /analysis`, `POST /assets/{id}/analysis/retry`,
`POST /assets/{id}/preview/retry`.

**Golden additions:** asset delete + restore round-trip, 409 revision
conflict on a user-state patch, the queue ops returning 202 with
`Retry-After` where applicable, 409 "not in a burst" on the representative
endpoint.

**Tests:** goldens unchanged and green (all prior sections + this section);
unit tests for the new models; full suite.

**Context checklist (~1,630 lines — the largest non-exception, under the
~1,800-line / 20k-token ceiling):** `api.py` (mutation + queue sections,
89 of 558), `state.py` (41), `models.py` (157), `catalog.py`
(`commit_mutation`, `queue_processing`, `queue_ai`, `preview_status` —
240 of 2,372), `bursts.py` (`set_representative`, 31), `service.py`
(`queue_processing`/`queue_analysis`, 30), `mutations.tsx` (121) +
`types.ts` (266, mutation section), `api_schemas.py` (whole — this phase
edits it; ~385 after Phase 3a), the contract-test file (helpers + Phase
3a section, ~170), plus the new code being written (3 models ~50, golden
section ~50).

**Definition of done:** all nine mutation/queue operations schema-complete;
goldens unchanged; suite green.

---

### Phase 4 — People/face operation results and binary-endpoint docs

**Models (response side of `commit_face_operation`):**

- `PersonRenameOut` — `{operationId, personId, displayName}`
- `PersonMergeOut` — `{operationId, personId, mergedPersonId, movedFaces}`
- `FaceMoveOut` — `{operationId, personId, movedFaces, createdPerson}`
  (note: `targetPersonId` is absent in the dict — keep it absent)
- `VerifyOut` — `{assetsChecked, blobsChecked, verification: "sha256"|"size",
  errors: list[{key, error}]}`

**Endpoint rewiring:** `PATCH /people/{id}` (`PersonRenameOut`),
`POST /people/{id}/merge` (`PersonMergeOut`), `POST /faces/move`
(`FaceMoveOut`), `POST /maintenance/verify` (`VerifyOut`).

**Binary/stream endpoints — document, don't over-model:**
`GET /assets/{id}/original|preview|thumbnail`, `GET /faces/{face_id}/thumbnail`.
Leave them without a `response_model`; add an explicit
`responses={200: {"content": {media_type: {}}}, 202: {...{"status":"pending"}}}`
- style declaration (or a small
`Pending202Out = {"status": "pending"}` model) so the spec records the real
media types (`image/jpeg`, `application/octet-stream` +
`Content-Disposition: attachment`) and the 202 + `Retry-After` contract the
client already honors (`PreviewImage.tsx` polls on it). Generated TS clients
will see these as untyped/binary responses, which is what the client wants.

**Golden additions:** face-op 404s/409s (rename, merge, move against
missing/stale targets) and a 202 face-thumbnail with `Retry-After` for a
missing preview.

**Wrap-up checks (this is the last schema phase):**

- `scripts/check_openapi.py --strict-coverage` now **hard-fails** if any
  JSON operation still has an empty response schema — with all four model
  phases done, the spec is 100% covered and any future untyped endpoint is a
  CI failure, which is the actual "no parallel maintenance" guarantee.
- Manually walk `/docs` + the spec diff of this whole phase series end-to-end
  once (one human review of the cumulative `git diff openapi/openapi.json`).

**Tests:** goldens (all prior sections + this section); unit tests for the
new models; full suite.

**Context checklist (~1,300 lines):** `api.py` (face + binary + misc
section, 130 of 558), `catalog.py` (`commit_face_operation`, `face` —
114 of 2,372), `service.py` (`verify`, 30), `api_schemas.py` (whole —
this phase edits it; the four result models go in here, ~425 after
Phase 3b), the contract-test file (helpers + Phase 3b section, ~180),
`frontend/web/src/features/people/*` (341, result-field consumption —
`FaceMutationResult` in `types.ts` is the cross-check; hold all of
`types.ts`, +266, only if that cross-check needs it — still under the
ceiling), plus the new code being written (4 result models ~40, golden
section ~40).

**Definition of done:** spec coverage assertion passes in CI; all operations
have request + response schemas (or are declared binary with media types);
goldens unchanged; suite green.

---

### Phase 4b — Operation IDs (spec-only, backend)

**Goal:** give every route an explicit `operation_id=` so the generated
client (Phase 5a) gets clean function names.

**Changes**

- `api.py`: add `operation_id=` to every route decorator — all 38
  in-schema routes (34 JSON + 4 binary; the `/` route is
  `include_in_schema=False` and stays out), unique camelCase names chosen
  from each endpoint's purpose (`browseAssets`, `getAssetDetail`,
  `createAlbum`, `uploadFile`, …). FastAPI's auto-generated ids
  (`browse_assets_api_library_assets_get`) would otherwise become the
  generated function names verbatim.
- Regenerate `openapi/openapi.json`: the diff is the `operationId` fields
  plus the hoisted components below. `operationId` is spec metadata — it
  appears in no request or response body, so the goldens stay
  byte-identical.
- `api.py`, the `openapi()` projection (spec only, never the wire): hoist
  the five response schemas FastAPI emits inline because they are not a
  single named model — the three `list[X]` arrays (`/albums`, `/assets`,
  `/upload-batches`) and the two discriminated-union document types
  (`AssetDocOut`, `AssetDetailOut`; the former is also inlined as the
  items of `/assets`) — into `components/schemas` under stable names
  (`AlbumOutList`, `AssetDocOutList`, `UploadBatchOutList`, `AssetDocOut`,
  `AssetDetailOut`), replacing each with a `$ref`. Rationale: an inline
  schema carries no component title, so pydantic falls back to a title
  derived from the response field name (`"Response <operationId>"`, e.g.
  `Response Listalbums`), breaking the naming precedent of the other 34
  operations whose 2xx is a `$ref` to a self-titled component. After the
  hoist, every 2xx JSON response is a bare `$ref` and the component count
  is 51 → 56.

**Tests:** full suite green; goldens unchanged; `check_openapi.py` green
against the new spec.

**Context checklist (~800 lines):** `api.py` (815 — one-line decorator
edits per route, plus the hoist projection in `openapi()`), the spec diff
(~150 lines — `operationId` on the 38 operations plus the five hoisted
components; no other change). The golden re-verify is a *run* of the
existing contract test, not a read of it.

**Definition of done:** every operation in the spec carries a stable,
unique, human-named `operationId`; goldens byte-identical; suite green.

---

### Phase 5a — Frontend: hey-api client core (hand-written shims kept)

**Goal:** the hey-api generator, the committed generated client, and a
`client.ts`/`mutations.tsx` rebuilt on the generated shapes — with **zero
feature-file changes**, so this pass is behavior-neutral by construction and
provable with the existing test suite alone.

**Changes (all in `frontend/web`):**

- `package.json`: add devDependency `@hey-api/openapi-ts` **pinned to an
  exact version** (0.x is initial development; migration notes ship with
  each breaking release). No runtime dependency is added — the fetch client
  is bundled into the generated output by default (`bundle: true`). Add
  scripts `generate:api` (runs the generator, which auto-discovers
  `openapi-ts.config.ts`) and `check:api` (run it with a temp `-o` output
  path, diff against the committed `src/api/generated/` — mirrors the
  backend drift check so a stale generated client can't ship). Wire
  `check:api` into `run_all_tests.sh` in the same commit. The generator
  runs on Node 22+; note that in the dev/CI requirements.
- `openapi-ts.config.ts` (new, checked in): `input:
  '../../openapi/openapi.json'`, `output: { path: 'src/api/generated' }`,
  `plugins: ['@hey-api/client-fetch', '@hey-api/typescript',
  '@hey-api/sdk']` — the fetch client with no `baseUrl` (the spec defines
  no servers, so generated URLs stay relative and keep flowing through the
  Vite dev proxy), default `throwOnError: false` (errors come back in the
  result, so `ApiError` normalization stays in our layer), and the SDK in
  its default flat strategy — one function per operation, named from the
  Phase 4b operationIds. Skip the `@hey-api/schemas` (Zod) and `msw`
  plugins — the app has its own validation/mocking story.
- Commit the generated output (`src/api/generated/`: `index.ts`,
  `client.gen.ts`, the bundled `client/` runtime, `sdk.gen.ts`,
  `types.gen.ts`) from the *same* commit as the final
  `openapi/openapi.json` (the point of the drift checks).
- `src/api/types.ts`: convert each hand-written contract interface
  (`Health`, `PhotoSummary`, `PhotoDetail`, `BurstDetail`, `BrowsePage`,
  `BlobInfo`, `Person*`, `FaceReference`, `FaceMutationResult`, `Album`,
  `MutationResult`, `UploadBatch*`, `UploadQueueStatus`, `PreviewStatus`,
  `Analysis*`, `LocationValue`, `UserState`) into a **re-export shim** of
  the generated schema type (`export type X = Generated.X`) — including
  the renames where the TS name differs from the Pydantic model name
  (`PhotoDetail` → `AssetDetailOut`, `PhotoSummary` → `PhotoSummaryOut`,
  `BlobInfo` → `BlobOut`, …). All 18 existing `import type` sites keep
  compiling unchanged; the shims are deleted in Phase 5b.
- `src/api/client.ts`: keep the runtime behavior exactly; it becomes a
  facade over the generated SDK.
  - `ApiError` unchanged (the `detail` string-or-array handling is the
    backend's validation contract, not the spec's). It plugs into the
    generated client via the custom-`fetch` hook (the generated
    `createClientConfig()` / `setConfig`), so every generated call
    normalizes errors exactly as the old `request<T>` did.
  - The domain methods (`browse()`, the mutation helpers, `apiUrl()` for
    binary URLs) delegate to the generated per-operation functions with
    generated param/result types; the `browse()` filter-to-query mapping
    keeps its current logic.
  - `uploadFile` (XHR with progress) stays hand-written — the generated
    client is `fetch`-based and does no `onprogress`; type its
    request/response with the generated `UploadFileReceipt`/paths.
  - DELETE-with-JSON-body (the journal protocol, Phase 3a note) needs no
    special case: the bundled fetch client builds `new Request(url,
    {method, body})`, so a body goes out on DELETE — verify it in the
    Phase 5b smoke test rather than working around it.
- `src/api/mutations.tsx`: type `mutate()`'s `changes` against the right
  generated request-body type per call site (a small per-domain helper is
  fine; avoid over-abstracting).

**Tests:** `npm run check` + `vitest` — with the feature files untouched,
the existing green *is* the proof the shims are equivalent; the generated
output must type-check with zero `any` leakage at contract boundaries (spot
check: `AssetDetailOut['analysis']['faces'][0].confidence` is `number`).

**Context checklist (~2,650 lines — the one declared exception, ~29% of
the window, ~2k of it skimmable generated output):** `src/api/client.ts`
(172), `src/api/types.ts` (266), `src/api/mutations.tsx` (121),
`vite.config.ts` (17 — proxy config), `openapi-ts.config.ts` (new, ~20),
`package.json` (~10), `run_all_tests.sh` (the section being extended,
~40 of 251), the generated output (`sdk.gen.ts` + `types.gen.ts` +
bundled `client/`, ~2k — read for names, not line-by-line), plus
`openapi/openapi.json` (~3,500 lines by now) as the generator's input
only — never held; the drift check reads it as a diff.

**Definition of done:** generated client committed and drift-checked in
`run_all_tests.sh`; `types.ts` contract interfaces all reduced to re-export
shims (no hand-written contract definitions left); typecheck + tests green
with zero feature-file changes; `openapi.json` and generated client from the
*same* commit.

---

### Phase 5b — Frontend: type-import sweep and shim removal (tsc-driven)

**Goal:** the hand-written TypeScript mirror is gone; every contract type
is imported directly from the generated module — and the sweep is
**discovered by the compiler, not pre-listed**: the shims are deleted
first, `npm run check` produces the work list, and the list is worked down
to zero.

**Why the compiler, not a static list:** the file list in this plan is a
snapshot taken at planning time; by the time Phases 0–4b have landed the
code may have drifted, and a grep for `api/types` misses namespace and
re-export usages. `tsc` misses neither. It is also *mechanical by
construction*: because the Phase 5a shims are exact aliases
(`export type X = Generated.X`), every usage site that compiled in 5a
compiles identically against the generated type, so the only errors this
produces are on the import lines themselves — never semantic errors at
call sites.

**Changes (all in `frontend/web`):**

- `src/api/types.ts`: **delete** the shim re-export block (it is small and
  committed in Phase 5a — recoverable from git if anything goes wrong;
  commenting it out is fine as an in-session trick, but the committed
  state has no dead code). Keep only the genuinely client-internal shapes
  (`LibraryFilters`, `PendingMutation`, `MutationMethod`) — they are not
  generated and never appear in the error list.
- `npm run check` (`tsc -b --pretty false` — the existing script, no new
  tooling): the error list is the work list. Each entry names file, line,
  and the exact member no longer exported (TS2305; TS2339 for
  namespace-import style usage; a re-export barrel, if any, shows up the
  same way).
- Work the list to zero:
  - **Expected — every entry:** point the import at the generated types
    module with an alias that preserves the local name —
    `import type { PhotoSummaryOut as PhotoSummary } from
    '../api/generated/types.gen'` — a one-line edit per file, zero
    changes to usage sites (the aliased type *is* the type the shims
    already stood for). If a statement imports both app and contract
    types from `api/types`, split it: app names stay, contract names
    move.
  - **Unexpected — stop and investigate:** a *semantic* error (TS2322,
    TS18048, …) at a usage site means the Phase 5a shim was **not** an
    exact alias — that is a Phase 5a bug, not a Phase 5b finding. Fix it
    where it belongs (the shim mapping / the generated config) and
    re-run; never `as`-cast or `any`-escape around it in this phase.
- The 5 files with `api.*`/`mutate(` call sites (`PhotoPage`,
  `LibraryPage`, `PeoplePage`, `UploadQueue`, `BurstStrip`) keep calling
  the Phase 5a facade and need no close reading this phase: they already
  compile against the generated types through the shims, and their only
  change is the import line tsc flags.
- **Manual smoke** on `npm run dev` after the swap: browse + filter +
  sort, album CRUD, person rename/merge/move, an upload through the batch
  flow with progress, preview miss → 202 → regenerate, original download,
  trash/restore, pending-mutation retry after a killed request.

**Tests:** `npm run check` exits 0 (the list is exhausted by definition) +
`vitest` (existing domain tests, including `domain/library.test.ts`, which
imports from `api/types`); no backend change this phase, so the goldens
stay untouched. The final `git diff` audits to import-line changes only.

**Context checklist (~710 lines — comfortably under budget):** the `tsc`
error output (bounded by the import statements, ~100–150 lines),
`types.ts` (266), `client.ts` (172), `mutations.tsx` (121) as reference.
The 13 import-only files are *edited* from the error list, not read in
advance.

**Definition of done:** contract types in `types.ts` = 0; every contract
import points at the generated module; `npm run check` + `vitest` green;
manual smoke passes.

---

### Phase 6 (optional) — Backend client + CI hardening

**Context checklist (~250 lines — trivially under budget):**
`upload_client.py` (157), the new `openapi-python.config.ts` (~20),
`run_all_tests.sh` (verification only).

- `upload_client.py` (the `photo-upload` CLI) today speaks the batch API with
  untyped dicts: generate a Python SDK + Pydantic models with
  `@hey-api/openapi-python` — the same generator family as the web client
  (same spec, same `defineConfig` config style, HTTPX client) — or at
  minimum validate its `describe_batch`/receipt parses with the same
  Pydantic models the server uses (`api_schemas` is importable). Keep the
  client small; the win is that the CLI, the web app, and future tools all
  read the same spec through the same generator ecosystem.
- `run_all_tests.sh`: the spec-drift section exists from Phase 0 and the
  frontend `check:api` was wired in Phase 5a — this phase only verifies the
  whole chain (spec drift + client drift + suite) runs in one pass.
- Optional polish: give the 202 branch of the face-thumbnail endpoint the
  same `Pending202Out` model used by `preview()`/`thumbnail()` for symmetry;
  add an `AssetListOut` name if `GET /assets` consumers appear.

## Cross-cutting notes

- **Never change a wire key in these phases.** If a model reveals a genuinely
  bad shape (a key that shouldn't exist, a value that's `None` where the TS
  side assumes non-null), the correct move is a *follow-up* breaking change
  done deliberately — fix the model and the old client in lockstep, with a
  note in this doc's follow-ups list — not an in-phase "while we're here".
- **`ResponseValidationError` = a caught bug, not a defect tolerance.**
  Failing loudly on actual deviation is the design (Decision 3): treat any
  new 500 in tests as "model wrong, fix model toward the dict", and let
  the goldens confirm the wire stayed put. The one exempted class is the
  *values* inside a `dict[str, Any]` field — never validated, never a 500
  source; the field itself stopping being a dict is still a 500, correctly.
- **Operation IDs and expected revisions** (`OperationRequest` and friends)
  are already well-formed for codegen; the durable-mutation journal
  (`mutations.tsx`) is client behavior, not contract — out of scope.
- **Sizing for a 100k-context model (audited against the current tree):**
  every phase's checklist is ≤ ~1,630 lines (Phase 3b, the largest
  non-exception, ≈ 18k tokens = 18% of the window — under the 20k-token
  ceiling); Phase 5a is the one declared exception at ~2,650 (~29%), the
  bulk of it generated code. Phase 5b is ~710 because the type-checker,
  not the pre-computed file list, produces its work list. The cited
  counts were re-measured for this audit (38 in-schema routes in
  `api.py`; the current spec is 2,213 lines; every `catalog.py` /
  `service.py` function range above was re-verified) — if the tree
  drifts, re-run the audit before relying on a number. That leaves at
  least ~75% of the window (outside Phase 5a) for reasoning, tool I/O,
  test output, and iteration, which is what actually overflows on a small
  context. If a phase's checklist grows beyond the ceiling during
  implementation, split at the next model/endpoint boundary — the phase
  table is already cut at those boundaries, so a split costs a renumber,
  not a redesign.
- **Rollback** is per-phase `git revert`: phases only add `response_model=`,
  new model classes, and test artifacts; no data migration is anywhere in
  this plan.

## Suggested validation sequence (per phase)

1. `ruff check` (backend) / `npm run check` (frontend, Phase 5a+).
2. `PHOTO_RUN_INTEGRATION=1 pytest backend/tests/ -v` including
   `test_api_contract.py` with the golden compare (this phase's new section
   records first, then verifies on the second run).
3. Regenerate `openapi/openapi.json`, eyeball the diff for the phase's
   schemas only (Phase 4b: `operationId` fields plus the hoisted
   inline-response components); commit spec + code together.
4. `./run_all_tests.sh` for the full picture before moving to the next phase.

## Follow-ups (deliberate deferred changes)

- **`PhotoDetail` shim targets the v2 variant of `AssetDetailOut`.**
  Untouched imports are still schema-v1 documents (no `deletedAt` key;
  `revision` pinned to the literal `1`). The photo feature page is a
  v2-shaped consumer: every field it reads exists on both variants,
  `deletedAt` is only read through `Boolean(...)` (runtime `undefined`
  behaves identically to `null` — a v1 document is correctly never hidden),
  and any asset mutation upgrades the document to v2, so post-mutation
  documents are genuinely v2. The v1/v2 distinction in the UI is deferred;
  if the UI ever branches on `schemaVersion`, the shim becomes the full
  `AssetDetailOut` union (with care around TanStack `setQueryData` and the
  `revision: 1` literal).
- **List documents: non-null `thumbnailUrl`/`previewUrl`.** The spec models
  `PhotoSummaryOut.thumbnailUrl`/`previewUrl` as `string | null` (the
  Pydantic models are conservative), but the producer
  (`browsing.asset_summary()`) always emits both as f-strings. The
  frontend asserts non-null in the `PhotoSummary` shim and re-derives the
  deterministic `/assets/{assetId}/thumbnail|preview` URLs in the facade
  should a null ever arrive. Follow-up: make the invariant explicit in the
  backend models (e.g. non-`Optional` `StrictStr`) so the spec reflects
  reality, then drop the shim tightening.
- **`mutate()`'s `changes` stays loose.** The journal protocol is
  endpoint-dynamic (`path`/`method` arrive at runtime), so no single
  generated request-body type can statically cover every call site.
  `changes` is typed `Record<string, unknown> & Partial<OperationRequest>`
  — the generated protocol fields (`expectedRevision`) get real types,
  endpoint-specific fields stay untyped here; the generated types still
  apply at the wire boundary via `sendMutation`'s JSON serialization.
- **`API_ROOT` is applied via the SDK `baseUrl`, not a custom `fetch`
  hook.** The plan text for this phase said the facade would prepend
  `API_ROOT` via a custom-`fetch` hook; the implementation instead passes
  it as `createClient({ baseUrl: API_ROOT })`, which the SDK's `buildUrl`
  resolves each relative spec URL against. Behavior is identical (relative
  `/api` roots and full-URL `VITE_API_ROOT` both work; the Vite dev proxy
  and nginx `/api` proxy are untouched) and the committed generated output
  stays prefix-agnostic.
- **Empty browse query string.** The previous hand-rolled client always
  emitted a trailing `?` on a bare browse URL (`/browse?`); the SDK omits
  an empty query string. The request *line* differs by that one byte; the
  query string is empty in both cases, so no key, value, or decoded
  parameter changes — noted for the "no wire change" audit only.
