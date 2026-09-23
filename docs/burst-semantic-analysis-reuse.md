# Burst-aware semantic reuse and clustering

## Status

Proposed design. This document describes two related capabilities built on the same
perceptual fingerprint:

1. **Semantic reuse** — avoid redundant VLM inference for near-identical burst frames.
2. **Burst clustering and best-shot selection** — group near-identical frames into a
   single grid cell in the frontend and let the user pick the best frame.

Neither changes the meaning of an asset or the existing exact-content deduplication
behavior. Every frame remains a distinct, durable asset.

## Problem

Machine-gun shutter sequences commonly contain many photographs with nearly identical
visual content. The files are still distinct photographs: their pixels, capture times,
expressions, focus, and face locations may differ. SHA-256 therefore differs, and the
current AI worker runs the full vision-language model (VLM) for every frame.

Two goals follow from that reality:

- Avoid redundant VLM inference when another frame is sufficiently similar, without
  merging assets or silently reusing analysis across merely related photographs.
- Present a burst as one image in the library grid so the user can review it as a
  single stack, then select the best frame (for example, the one where everyone's eyes
  are open) without losing any frame.

## Decision

Add a cheap, versioned perceptual fingerprint and use it for two purposes:

- **Semantic reuse.** Find an already analyzed frame from the same likely burst. When
  the match passes conservative visual and metadata gates, copy that frame's semantic
  analysis instead of invoking the VLM.
- **Burst clustering.** Group frames that are near-duplicates of one another into a
  burst so the library grid renders them as a single stacked image. The user can then
  choose the best frame, which becomes the burst's representative.

Face detection and face embeddings continue to run for every asset. Every asset also
receives its own analysis run and durable artifact, whether its semantics were computed
or reused.

The governing invariant is:

> Semantic descriptions may be inherited from a verified near-duplicate; image-specific
> face observations and analysis provenance may not be inherited.

A second invariant governs clustering:

> Clustering is a display and user-preference concern. It never merges, deletes, or
> hides frames at rest; every frame remains a distinct, durable asset, and the
> representative choice is reversible user state.

The existing SHA-256 check remains unchanged. It detects byte-identical uploads and is
not used to detect burst frames.

## Non-goals

- Do not merge, delete, or physically collapse burst frames. Clustering is a display
  grouping; every frame remains a distinct, durable asset at rest.
- Do not use semantic embeddings to decide that analysis is reusable.
- Do not reuse face counts, boxes, confidence values, or embeddings.
- Do not guarantee that every burst avoids all redundant inference. False negatives are
  preferable to false-positive reuse.
- Do not make best-shot selection automatic. The user makes the choice; the system only
  records and persists it.

## Why perceptual hashing

A cryptographic hash changes completely after any pixel or metadata change. A perceptual
hash maps visually similar raster images to nearby bit strings and makes comparison a
cheap Hamming-distance calculation.

Semantic embeddings are intentionally unsuitable for the reuse decision: two different
views of the same mountain or person may be semantically close while containing details
that merit separate descriptions. Perceptual similarity, constrained by capture metadata,
answers the narrower question required here.

The first implementation should use two independent 64-bit image hashes:

- pHash for low-frequency composition;
- dHash for coarse edge structure.

Both are computed from a fixed-size, orientation-corrected, metadata-free rendering of
the generated preview. The exact resize, colorspace, DCT, bit ordering, and orientation
rules form an immutable algorithm version, initially `burst-hash-v1`. Changing any of
those rules requires a new version rather than reinterpretation of stored hashes.

## Matching policy

An asset is eligible to inherit semantics only from a current, successful analysis run.
The source must satisfy all of the following:

1. It is a different asset.
2. Its fingerprint algorithm version matches.
3. Its semantic pipeline, model name, and model digest match the requested analysis.
4. Its aspect ratio is effectively identical and its dimensions are compatible.
5. Its capture time is within three seconds of the target when both timestamps are known.
6. Its camera identity matches when both assets provide camera make/model or a stable
   device identifier.
7. pHash Hamming distance is at most 4 and dHash Hamming distance is at most 6.
8. A final inexpensive comparison of normalized preview pixels passes the configured
   similarity threshold.

The numerical thresholds are intentionally conservative starting values, not universal
truths. They must be validated against this library before reuse is enabled. The final
comparison protects against hash collisions; it can use a fixed 256-pixel luminance
render and a small normalized pixel-error calculation without introducing another model.

If several sources qualify, choose the candidate with the lowest tuple of pHash distance,
dHash distance, capture-time distance, then asset ID. Deterministic tie-breaking makes
retries reproducible.

Do not reuse semantic output from analyses classified as `document` or `screenshot`, or
from results containing `visibleText`. Small visual differences in those images may carry
materially different text.

Missing or ambiguous metadata narrows optimization rather than weakening visual gates.
A candidate with no capture time may be observed during rollout, but should not be used
automatically in the initial policy.

## Burst clustering and best-shot selection

The same `burst-hash-v1` fingerprints that gate semantic reuse also drive a display-only
grouping of near-duplicate frames. A burst is a set of frames that are near-duplicates of
one another under a clustering policy. The library grid renders each burst as a single
stacked image, and the user can review the individual frames and choose the best one,
which becomes the burst's representative.

Clustering is governed by the second invariant from the Decision section: it is a
display and user-preference concern. It never merges, deletes, or hides frames at rest.
Every frame remains a distinct, durable asset, and the representative choice is
reversible user state.

### Clustering policy

Clustering uses a separate, display-only policy, now `burst-cluster-v2`, so that
tuning it can never broaden the conservative semantic-reuse policy. A frame joins a burst
when its perceptual fingerprint matches and its contextual evidence is sufficient relative
to an existing member of that burst:

1. It is a different asset.
2. Its fingerprint algorithm version matches.
3. Its aspect ratio is effectively identical and its dimensions are compatible.
4. Its capture time is within three seconds of the member when both timestamps are known;
   timestamps with offsets are compared as instants, rather than as local clock values.
   A wider time gap can be accepted when matching camera-sequence filenames and a shutter
   count gap of at most three provide independent confirmation.
5. Its camera identity matches when both assets provide camera make/model. Known
   conflicting camera identities veto the match.
6. pHash Hamming distance is at most the configured threshold (initially 24) and dHash
   Hamming distance is at most the configured threshold (initially 16). These display-only
   thresholds are calibrated to tolerate modest zoom while retaining both independent hash
   gates.
7. It reaches the minimum contextual evidence score. Capture proximity contributes three
   points, matching camera identity one, a nearby filename sequence two, nearby import
   time one, and a nearby shutter count one to three. A shutter-count gap over 20 or an
   import gap over seven days contributes a small negative signal. At least two evidence
   signals and four points are required.

Filename sequence and import time are corroborating signals, not requirements. Filenames
are only recognized when both names have the same non-numeric prefix and extension and
their numeric portions are within three frames. Generic repeated names therefore cannot
create a burst by themselves. Shutter count is optional and reads maker-specific metadata
such as `ShutterCount` when ExifTool exposes it; absent or unsupported maker metadata is
neutral.

Unlike the reuse policy, clustering does not require the member to have a current
analysis run, and it does not perform the final pixel comparison. The Hamming gates are
sufficient for a display grouping, where a false positive only stacks two similar frames
that the user can still expand and review. The thresholds start at the reuse values and
are tuned independently; changing them increments the clustering policy version without
affecting `reuse_policy_version`. The clustering thresholds are configurable in the
environment file, so operators can tune burst grouping without a code change (see the
Configuration and rollout section). The policy version is persisted on each cluster so
historical clusters can be explicitly rebuilt when the policy changes.

A frame with no fingerprint, or with metadata that fails the gates, is not clustered and
is shown as an individual frame.

### Cluster identity and storage

A burst is identified by a cluster record. The cluster's representative is the frame the
grid displays for the burst. The first frame analyzed creates the cluster and is its
initial representative; the user can change it.

Membership is computed when a frame's fingerprint is persisted and is stable thereafter:
a frame joins a cluster when its fingerprint is written and leaves only when the frame is
deleted. The representative can change without changing membership.

### Best-shot selection

Reviewing a burst shows its individual frames. The user selects the best frame (for
example, the one where everyone's eyes are open). That frame becomes the cluster's
representative, and the grid then displays it. The choice is durable and reversible:
selecting a different frame later simply moves the representative. No frame is deleted,
merged, or hidden at rest.

The selection is committed through the existing mutation flow (`mutate` →
`commit_mutation`) so that it is PostgreSQL-authoritative, idempotent, and backed up with
the rest of the library state. It is expressed as a new mutation action,
`burst.setRepresentative`, whose entity is the cluster and whose change sets
`representativeAssetId`. Because setting a representative is idempotent, it does not need
a separate optimistic-concurrency revision; the existing `operation_id` provides
idempotency.

### API surface

- `asset_summary` gains `burstId` (null for non-burst frames), `burstSize` (the true
  member count), and `burstRepresentativeAssetId`. The grid derives
  `isBurstRepresentative` as `assetId == burstRepresentativeAssetId`.
- The browse query joins assets to their cluster so each frame carries these fields.
  `burstSize` is the maintained member count, not the number of frames visible on the
  current page, so the badge stays correct even if a burst's frames span a page boundary.
- A new endpoint `GET /assets/{asset_id}/burst` returns the burst containing that asset:
  its `burstId`, `representativeAssetId`, and the member frames as photo summaries. The
  frontend uses it to populate the expand/review view when not all members are already in
  the current page.
- `burst.setRepresentative` is added to the `Mutation` action set. Its `entity_id` is the
  cluster ID and its `changes` set `representativeAssetId`. It is committed through
  `commit_mutation` alongside the idempotency record, so a retried request with the same
  `operation_id` does not apply the change twice.

### Frontend behavior

- The library grid renders one cell per burst. The cell shows the representative's image
  (constructed from `burstRepresentativeAssetId`, so it renders even if that frame is on
  another page) and a count badge equal to `burstSize` when the burst has more than one
  frame.
- A burst cell carries a stack indicator. Opening it, or choosing "select best", fetches
  the burst's members and shows the individual frames as a strip.
- Clicking a frame commits `burst.setRepresentative` for that frame. The grid then
  displays the new representative. The choice is reversible.
- Non-burst frames render exactly as today. The virtualized row model gains one row per
  burst rather than one per frame, which reduces the row count and does not change the
  virtualization or month-grouping logic.

## Processing flow

Fingerprinting belongs after preview generation because previews are already
orientation-corrected, bounded inputs available to the AI worker. It should not delay or
complicate upload commit.

The AI worker flow becomes:

1. Claim `ai-v1` after `preview-v1` is ready, as today.
2. Prepare the bounded JPEG.
3. Compute and persist `burst-hash-v1` fingerprints if absent.
4. Resolve the current Ollama model digest without starting inference.
5. Query for a reusable semantic run using the matching policy.
6. Run AdaFace detection and embedding for the target image in all cases.
7. If a source qualifies, validate and copy only the fields represented by
   `SemanticAnalysis`. Otherwise call `analyze_semantics` normally.
8. Write a target-specific analysis artifact containing the decision and provenance.
9. Atomically publish the target's analysis run, faces, and completed job as today.

Only `ready` source runs are candidates. The worker does not wait for a similar job that
is pending or running. With the current single AI worker, the first frame encountered in
a burst receives full analysis and later frames can reuse it. Future concurrent workers
may occasionally analyze two representatives; that is safe and preferable to adding a
coarse distributed lock.

## Data model

Add a migration after `006_abandoned_upload_cleanup.sql`.

### `image_fingerprints`

```sql
CREATE TABLE image_fingerprints (
    asset_id VARCHAR NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    algorithm_version TEXT NOT NULL,
    phash VARCHAR(16) NOT NULL,
    dhash VARCHAR(16) NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (asset_id, algorithm_version)
);

CREATE INDEX ix_image_fingerprints_version
    ON image_fingerprints (algorithm_version);
```

Hex text keeps unsigned 64-bit values portable through SQLAlchemy and PostgreSQL. Hamming
distance can initially be calculated in Python after the catalog query uses capture time,
camera metadata, aspect ratio, and algorithm version to produce a small candidate set.
This avoids requiring a PostgreSQL extension. If profiling later shows the candidate set
is too large, store bit strings and add an indexed search strategy in a separate change.

### `burst_clusters`

```sql
CREATE TABLE burst_clusters (
    id VARCHAR PRIMARY KEY,
    representative_asset_id VARCHAR NOT NULL REFERENCES assets(id) ON DELETE RESTRICT,
    policy_version TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE burst_members (
    cluster_id VARCHAR NOT NULL REFERENCES burst_clusters(id) ON DELETE CASCADE,
    asset_id VARCHAR NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    PRIMARY KEY (cluster_id, asset_id)
);

CREATE INDEX ix_burst_members_asset ON burst_members (asset_id);
```

`burst_clusters.representative_asset_id` is the frame the grid displays for the burst.
`burst_members` records membership. The browse query joins `assets` to `burst_members`
and `burst_clusters` to attach `burstId`, `burstSize` (a maintained member count, or a
count subquery), and `burstRepresentativeAssetId` to each `asset_summary`.

`representative_asset_id` uses `ON DELETE RESTRICT` so that deleting the representative
cannot silently drop the cluster. Deleting the representative instead moves the
representative to another surviving member (or, if the last member is deleted, the
cluster and its membership rows are removed by the `ON DELETE CASCADE` on
`burst_members`). This keeps the invariant that a cluster always displays a real frame.

### `analysis_runs`

Add nullable provenance fields:

```sql
ALTER TABLE analysis_runs
    ADD COLUMN semantic_origin TEXT NOT NULL DEFAULT 'computed',
    ADD COLUMN source_run_id VARCHAR REFERENCES analysis_runs(id) ON DELETE SET NULL,
    ADD COLUMN reuse_policy_version TEXT,
    ADD COLUMN similarity JSONB;
```

`semantic_origin` is `computed` or `reused`. For a reused result, `similarity` records both
Hamming distances, pixel similarity, capture-time distance, and the fingerprint version.
The database row and S3 artifact contain the same provenance. The source result is copied
into the new run; the target run does not depend on reading the source run at query time.

The source foreign key is diagnostic lineage. If the source asset is later deleted,
`source_run_id` may become null while the immutable target artifact retains the original
source run and asset IDs.

## Analysis artifacts and public results

The target artifact retains the existing target identifiers and `inputSha256`. Add:

```json
{
  "semanticOrigin": "reused",
  "semanticSource": {
    "assetId": "...",
    "runId": "..."
  },
  "similarity": {
    "policyVersion": "burst-reuse-v1",
    "fingerprintVersion": "burst-hash-v1",
    "phashDistance": 2,
    "dhashDistance": 3,
    "captureDeltaMs": 180,
    "pixelSimilarity": 0.99
  }
}
```

Semantic fields and `searchable_text` may be copied. `faceCount`, `personCount`, face rows,
and any future image-local detections must be generated from the target. Code should copy
through the `SemanticAnalysis` model rather than cloning the source's combined public
result, preventing image-local fields from leaking into the target.

The asset-detail API may expose `semanticOrigin` and source asset ID for diagnosis, but
normal browsing does not need to distinguish computed and reused results.

## Retry and version behavior

- A normal retry may reuse another qualifying current run.
- Add a `forceFull` option to the analysis queue/API and a corresponding CLI flag for
  validation and repair. It bypasses reuse for that job without deleting fingerprints.
- A different pipeline version, model name, or model digest never reuses an old result.
- An unknown model digest disables reuse; it does not fall back to matching only by name.
- Fingerprints are regenerable processing data. A new fingerprint algorithm is populated
  lazily when an analysis job runs.
- Threshold changes increment `reuse_policy_version`; they do not change the fingerprint
  algorithm version.

## Configuration and rollout

Introduce `PHOTO_AI_SEMANTIC_REUSE_MODE` with three values:

- `off`: compute fingerprints only when otherwise required; always invoke the VLM.
- `observe`: find and record the candidate decision in logs/metrics, but invoke the VLM.
- `on`: reuse semantics when every gate passes.

Default the first release to `observe`. Record candidate IDs, distances, policy rejection
reasons, and whether the VLM was invoked. Review a representative sample of accepted and
near-threshold pairs, including portraits, movement, exposure changes, screenshots, and
unrelated photos taken close together. Enable `on` only after choosing thresholds from
those observations.

Useful operational counters are:

- semantic analyses computed;
- semantic analyses reused;
- candidates rejected by each gate;
- estimated VLM time avoided;
- forced full analyses;
- failures split between fingerprint, face, and semantic stages.

Clustering ships as an always-on display feature, not gated by `PHOTO_AI_SEMANTIC_REUSE_MODE`.
It is display-only and carries no correctness risk: a false positive only stacks two similar
frames that the user can still expand and review, and no frame is ever merged, deleted, or
hidden at rest. Because it is governed by the separate `burst-cluster-v2` policy version,
tuning its thresholds can never broaden the conservative semantic-reuse policy, and the reuse
mode (`off`/`observe`/`on`) does not change clustering behavior.

The clustering similarity thresholds are configurable in the environment file:

- `PHOTO_BURST_CLUSTER_PHASH_MAX_DISTANCE` (default 24): maximum pHash Hamming distance
  for a frame to join a burst.
- `PHOTO_BURST_CLUSTER_DHASH_MAX_DISTANCE` (default 16): maximum dHash Hamming distance
  for a frame to join a burst.
- `PHOTO_BURST_CLUSTER_CAPTURE_WINDOW_SECONDS` (default 35): capture-time window for
  the normal burst-context match. Consecutive filename and shutter-count evidence can
  still corroborate a slower sequence outside this window.

These settings affect only the display-only clustering policy. The conservative
semantic-reuse thresholds remain fixed constants of the `burst-reuse-v1` policy and are
not configurable, so environment tuning can never broaden the reuse gates.

Use `photo-server recluster-bursts` or `POST /maintenance/recluster-bursts` after changing
these settings. The operation rebuilds all current burst memberships from persisted
fingerprints and is safe to repeat. It also removes pending semantic-analysis work for
non-representative burst members; running analysis is allowed to finish, and existing
analysis results are preserved.

## Failure handling

- Fingerprint decode or persistence failure fails the AI job normally; it must not produce
  an analysis whose provenance cannot be reproduced.
- Candidate lookup failure falls back to full VLM analysis only when the database remains
  healthy enough to publish the result. It is logged as an optimization failure.
- Invalid inherited semantic JSON rejects that candidate and invokes the VLM.
- Face analysis failure fails the whole job even if reusable semantics exist.
- As in the existing flow, an S3 artifact written before a database failure may be orphaned
  and can be handled by future artifact garbage collection.

## Testing

Unit tests should cover:

- stable pHash/dHash values for fixed fixtures;
- small exposure and compression changes remaining within thresholds;
- translations, changed composition, and unrelated images being rejected;
- deterministic candidate ordering and Hamming distance;
- time, camera, aspect-ratio, text-content, model, and pipeline gates;
- extraction of only `SemanticAnalysis` fields from a source result.

Integration tests should prove:

- the first burst frame invokes the VLM and a later near-identical frame does not;
- face analysis runs and stores distinct face rows for both frames;
- the reused run has its own input hash, object key, searchable text, and provenance;
- retries are idempotent and `forceFull` invokes the VLM;
- a model or pipeline change forces computation;
- observe mode reports a match but still invokes the VLM;
- deleting the source asset does not invalidate the target's current result;
- concurrent or reordered jobs may compute extra analyses but never publish another
  asset's face data.

Clustering and best-shot tests should prove:

- a burst of near-identical frames renders as one grid cell with a correct count badge;
- `burst.setRepresentative` persists the choice and survives a reload;
- selecting a different frame later moves the representative and is reversible;
- deleting a non-representative member leaves the burst intact;
- deleting the representative moves it to another surviving member, and deleting the last
  member removes the cluster;
- a frame with no fingerprint is not clustered and renders as an individual frame;
- semantic-reuse mode (`off`/`observe`/`on`) does not change clustering behavior, and
  clustering does not change semantic-reuse behavior;
- `burst.setRepresentative` is idempotent under a repeated `operation_id`.

## Future work

Library-wide visual search and semantic embedding search can reuse parts of this
infrastructure, but require separate product and accuracy decisions. They must not
broaden the conservative semantic-reuse policy implicitly.

Near-duplicate stacking and best-shot selection are now part of this design and are
deliberately display-only; they do not change the semantic-reuse gates.

## Phased implementation plan

This section decomposes the plan into phases, each small enough to hand off to a model
with a 192k-token context. A phase is self-contained: the only context a fresh model
needs is this document plus the phase's own description. Phases are ordered by
dependency; a phase depends only on phases that precede it. Every phase lists the exact
files it creates or modifies, its acceptance criteria (drawn from the Testing section
above), and the command that verifies it.

The single migration described in the Data model section is split across `007`
(fingerprint table and `analysis_runs` provenance columns, Phase 2) and `008`
(clustering tables, Phase 5) so that each phase owns its own migration and stays
independently verifiable. `migrations.py` enforces consecutive numbering from `000`, so
these must be the next two numbers after `006_abandoned_upload_cleanup.sql`.

### Phase 1 — Perceptual fingerprint module

**Objective.** Implement `burst-hash-v1` as pure computation: pHash and dHash, Hamming
distance, and deterministic candidate ordering. No database or worker changes.

**Create.**
- `backend/src/photo_server/fingerprints.py`
  - `BURST_HASH_VERSION = "burst-hash-v1"`.
  - `compute_fingerprint(jpeg: bytes) -> Fingerprint` returning `phash`, `dhash` (hex
    `VARCHAR(16)` strings), `width`, `height`. Compute from the orientation-corrected,
    metadata-free preview rendering produced by `analysis.prepare_jpeg`; strip EXIF.
  - `hamming_distance(a: str, b: str) -> int` over the 64-bit hex values.
  - `candidate_order(...)` producing the deterministic ordering (pHash distance, dHash
    distance, capture-time distance, asset ID).
- `backend/tests/test_fingerprints.py`

**Acceptance criteria** (Testing → Unit tests):
- Stable pHash/dHash values for fixed fixtures.
- Small exposure and compression changes remain within thresholds.
- Translations, changed composition, and unrelated images are rejected.
- Deterministic candidate ordering and Hamming distance.

**Verification.** `pytest backend/tests/test_fingerprints.py`

### Phase 2 — Fingerprint persistence and candidate queries

**Objective.** Persist fingerprints and query small candidate sets. Introduces migration
`007` and the `analysis_runs` provenance columns.

**Create.**
- `backend/src/photo_server/db_migrations/007_burst_fingerprints.sql`
  - The `image_fingerprints` table (Data model → `image_fingerprints`).
  - `ALTER TABLE analysis_runs` adding `semantic_origin`, `source_run_id`,
    `reuse_policy_version`, and `similarity` (Data model → `analysis_runs`).
- `backend/tests/test_fingerprint_persistence.py`

**Modify.**
- `backend/src/photo_server/catalog.py`
  - SQLAlchemy `Table` declarations for `image_fingerprints` and the new
    `analysis_runs` columns.
  - `upsert_fingerprint(...)` — idempotent by `(asset_id, algorithm_version)`.
  - `get_fingerprint(asset_id, version)`.
  - `find_fingerprint_candidates(...)` — filter on algorithm version, capture-time
    window, camera identity, and aspect ratio to produce a small candidate set; Hamming
    distance is computed in Python over that set (Data model → `image_fingerprints`).

**Acceptance criteria.**
- Migration `007` applies cleanly and is checksum-verified by `migrations.py`.
- Fingerprint upsert is idempotent under a repeated write.
- The candidate query returns only rows matching the version, capture-time, camera, and
  aspect-ratio filters.

**Verification.** `pytest backend/tests/test_migrations.py backend/tests/test_fingerprint_persistence.py`

### Phase 3 — Reuse policy engine

**Objective.** Implement the `burst-reuse-v1` gate evaluation and the
`PHOTO_AI_SEMANTIC_REUSE_MODE` setting. Pure policy: no worker changes yet.

**Create.**
- `backend/src/photo_server/reuse.py`
  - `REUSE_POLICY_VERSION = "burst-reuse-v1"`.
  - `ReuseDecision` (accepted/rejected, rejection reason, similarity payload).
  - `evaluate_reuse(target, source, settings) -> ReuseDecision` implementing all eight
    gates from Matching policy, with the conservative thresholds as fixed constants (not
    configurable). Excludes `document`/`screenshot` photo types and any source result
    with `visible_text`.
  - `extract_semantic(source_result) -> SemanticAnalysis` copying only the fields
    represented by `SemanticAnalysis` (Analysis artifacts → "copy through the
    `SemanticAnalysis` model").
- `backend/tests/test_reuse.py`

**Modify.**
- `backend/src/photo_server/config.py`
  - `ai_semantic_reuse_mode: Literal["off", "observe", "on"] = "observe"`.

**Acceptance criteria** (Testing → Unit tests):
- Time, camera, aspect-ratio, text-content, model, and pipeline gates.
- Extraction of only `SemanticAnalysis` fields from a source result.

**Verification.** `pytest backend/tests/test_reuse.py`

### Phase 4 — AI worker integration

**Objective.** Wire reuse into the worker flow (Processing flow steps 3–9) and add
`forceFull`.

**Modify.**
- `backend/src/photo_server/ai_worker.py`
  - After `prepare_jpeg`, compute and persist `burst-hash-v1` if absent (Phase 2).
  - Resolve the current Ollama model digest without starting inference.
  - Query reusable runs (Phase 2) and evaluate the policy (Phase 3).
  - Run AdaFace for the target in all cases.
  - If a source qualifies, copy only `SemanticAnalysis` fields; otherwise call
    `analyze_semantics`.
  - Write the target artifact with `semanticOrigin`, `semanticSource`, and `similarity`
    (Analysis artifacts), then publish atomically as today.
  - Honor `forceFull` (bypass reuse for that job without deleting fingerprints).
- `backend/src/photo_server/analysis.py` — carry `forceFull` through the job/queue.
- `backend/src/photo_server/api.py` — `forceFull` on the analysis retry endpoint.
- `backend/src/photo_server/cli.py` — `forceFull` CLI flag.

**Create.**
- `backend/tests/test_ai_worker_reuse.py`

**Acceptance criteria** (Testing → Integration tests):
- The first burst frame invokes the VLM; a later near-identical frame does not.
- Face analysis runs and stores distinct face rows for both frames.
- The reused run has its own input hash, object key, searchable text, and provenance.
- Retries are idempotent and `forceFull` invokes the VLM.
- A model or pipeline change forces computation; an unknown digest disables reuse.
- Observe mode reports a match but still invokes the VLM.
- Deleting the source asset does not invalidate the target's current result.
- Concurrent or reordered jobs may compute extra analyses but never publish another
  asset's face data.

**Verification.** `pytest backend/tests/test_ai_worker_reuse.py`

### Phase 5 — Clustering and best-shot backend

**Objective.** Implement `burst-cluster-v2` membership, representative management,
deletion semantics, and the burst API surface. Introduces migration `008`.

**Create.**
- `backend/src/photo_server/db_migrations/008_burst_clusters.sql`
  - `burst_clusters` and `burst_members` (Data model → `burst_clusters`).
- `backend/src/photo_server/bursts.py`
  - Membership computation from fingerprints using the configurable
    `PHOTO_BURST_CLUSTER_*` thresholds (separate from the reuse policy).
  - Representative management and the deletion semantics described in Data model
    (the representative moves to a surviving member; deleting the last member removes the
    cluster).
- `backend/tests/test_bursts.py`

**Modify.**
- `backend/src/photo_server/catalog.py`
  - `Table` declarations for `burst_clusters`/`burst_members`.
  - A `burst` branch in `commit_mutation` for `burst.setRepresentative` (idempotent via
    the existing `operation_id`).
  - Representative-move and last-member-removal handling on asset deletion.
- `backend/src/photo_server/models.py`
  - Add `"burst.setRepresentative"` to the `Mutation.action` literal.
- `backend/src/photo_server/api.py`
  - `GET /assets/{asset_id}/burst` (the expand/review strip payload).
  - A `burst.setRepresentative` mutation endpoint.
- `backend/src/photo_server/browsing.py`
  - Attach `burstId`, `burstSize`, and `burstRepresentativeAssetId` to `asset_summary`
    via a join on `burst_members`/`burst_clusters`.
- `backend/src/photo_server/config.py`
  - `burst_cluster_phash_max_distance: int = 24` and
    `burst_cluster_dhash_max_distance: int = 16`.

**Acceptance criteria** (Testing → Clustering and best-shot tests):
- `burst.setRepresentative` persists and survives a reload; selecting another frame moves
  the representative and is reversible; a repeated `operation_id` is idempotent.
- Deleting a non-representative member leaves the burst intact; deleting the
  representative moves it to a surviving member; deleting the last member removes the
  cluster.
- A frame with no fingerprint is not clustered.
- Semantic-reuse mode does not change clustering behavior, and clustering does not change
  semantic-reuse behavior.

**Verification.** `pytest backend/tests/test_bursts.py`

### Phase 6 — Frontend burst display and best-shot selection

**Objective.** Render one grid cell per burst and let the user pick the best frame.

**Modify.**
- `frontend/web/src/api/types.ts`
  - Add `burstId`, `burstSize`, and `burstRepresentativeAssetId` to `PhotoSummary`; add
    a `BurstDetail` type for `GET /assets/{asset_id}/burst`.
- `frontend/web/src/api/client.ts`
  - `getBurst(assetId)` for the review strip.
- `frontend/web/src/features/library/LibraryPage.tsx`
  - `buildRows`/`VirtualRow`: collapse a burst's frames into a single row/cell keyed by
    the representative, preserving month grouping and virtualization.
- `frontend/web/src/features/library/PhotoCard.tsx`
  - A count badge when `burstSize > 1` and a stack indicator; open the review strip.
- `frontend/web/src/features/library/BurstStrip.tsx` (new)
  - Expand/review strip listing the burst's frames; clicking a frame commits
    `burst.setRepresentative` through the existing `mutate` flow.

**Acceptance criteria** (Testing → Clustering and best-shot tests, frontend portion):
- A burst of near-identical frames renders as one grid cell with a correct count badge.
- Selecting a frame in the strip commits `burst.setRepresentative` and updates the
  representative on reload.

**Verification.** `npm test` in `frontend/web` (Vitest) plus a manual review-strip check.

### Phase 7 — Rollout verification and operational counters

**Objective.** Prove the end-to-end behavior and instrument the rollout described in
Configuration and rollout.

**Create.**
- `backend/tests/test_burst_integration.py`
  - End-to-end: a burst uploads, the first frame computes, later frames reuse (mode
    `on`), observe mode reports but computes, clustering stacks the frames, and best-shot
    selection is reversible.
- Operational counters (Configuration and rollout → "Useful operational counters"):
  semantic analyses computed/reused, candidates rejected by each gate, estimated VLM time
  avoided, forced full analyses, and failures split by fingerprint/face/semantic stage.

**Modify.**
- `backend/src/photo_server/ai_worker.py` / `reuse.py` — emit the counters (logs/metrics).
- `docs/` — a short rollout note: default `observe`, review accepted and near-threshold
  pairs, then enable `on`.

**Acceptance criteria.**
- The end-to-end integration test passes under both `observe` and `on`.
- All counters are emitted and observable.

**Verification.** `pytest backend/tests/` (full suite).
