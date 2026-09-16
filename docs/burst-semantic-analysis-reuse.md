# Burst-aware semantic analysis reuse

## Status

Proposed design. This document describes a future optimization; it does not change the
meaning of an asset or the existing exact-content deduplication behavior.

## Problem

Machine-gun shutter sequences commonly contain many photographs with nearly identical
visual content. The files are still distinct photographs: their pixels, capture times,
expressions, focus, and face locations may differ. SHA-256 therefore differs, and the
current AI worker runs the full vision-language model (VLM) for every frame.

The goal is to avoid redundant VLM inference when another frame is sufficiently similar,
without merging assets or silently reusing analysis across merely related photographs.

## Decision

Add a cheap, versioned perceptual fingerprint and use it to find an already analyzed
frame from the same likely burst. When the match passes conservative visual and metadata
gates, copy that frame's semantic analysis instead of invoking the VLM.

Face detection and face embeddings continue to run for every asset. Every asset also
receives its own analysis run and durable artifact, whether its semantics were computed
or reused.

The governing invariant is:

> Semantic descriptions may be inherited from a verified near-duplicate; image-specific
> face observations and analysis provenance may not be inherited.

The existing SHA-256 check remains unchanged. It detects byte-identical uploads and is
not used to detect burst frames.

## Non-goals

- Do not collapse, delete, hide, or stack near-duplicate assets.
- Do not select the "best" photograph from a burst.
- Do not use semantic embeddings to decide that analysis is reusable.
- Do not reuse face counts, boxes, confidence values, or embeddings.
- Do not guarantee that every burst avoids all redundant inference. False negatives are
  preferable to false-positive reuse.

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

## Future work

Near-duplicate stacking, best-shot selection, library-wide visual search, and semantic
embedding search can reuse parts of this infrastructure, but require separate product
and accuracy decisions. They must not broaden the conservative semantic-reuse policy
implicitly.
