# AI analysis records: S3-authoritative publication

## Gap

The S3 canonical plane already defines and *reads* the three AI record types —
`manifests/processing/{artifact-id}.json` (`ProcessingArtifact`),
`manifests/faces/{face-id}.json` (`FaceManifest`), and the `PersonManifest`
revisions — and rebuild/reconcile/GC treat them as authoritative input. But the
AI worker has **no writer** for them: it stores the raw artifact under the data
plane (`analysis/{asset}/{pipeline-version}/{run}.json`) and hands the
catalog a run id (`uuid4`) plus the data-plane key. `complete_ai_analysis` then
invents further `uuid4` ids for faces and persons that exist nowhere in S3.
Result: a rebuild from S3 alone cannot reproduce the AI projection, and a
library whose Postgres is lost cannot recover its faces/people.

## Target

S3-first mutation, the same pattern already used for assets, albums, people,
bursts, and fingerprints:

```text
worker assembles the analysis result
        ↓
S3: objects/{sha256}  (content-addressed result bytes, create-only + verified)
    manifests/processing/{artifact-id}.json   (ProcessingArtifact, immutable)
    manifests/faces/{face-id}.json            (FaceManifest per face, immutable)
    manifests/people/{person-id}/{n}.json     (PersonManifest revisions)
        ↓
PostgreSQL projection (pure: demote/insert runs, upsert face rows,
apply person assignments, reset jobs) — never invents new identity
```

## Identity scheme (all deterministic, `uuid5`-based)

| id | formula |
| --- | --- |
| logical run / artifact | `uuid5(library_id, "processing:photo-ai:{asset_id}:{input_sha256}:{pipeline_version}:{result_sha}")` |
| face | `uuid5(library_id, "face:{asset_id}:{run_id}:{face_index}")` |
| new person | `uuid5(library_id, "person:{run_id}:{face_index}")` |

`{result_sha}` is the SHA-256 of the canonical-JSON result payload, which is
the data-plane artifact minus the two identity/wall-clock fields (`runId`,
`createdAt`) — including `runId` would be circular since it derives from the
sha. Because the run id is a pure function of content, a retry of the same
logical analysis (same input, same model digest, same faces) reproduces the
exact same keys and is a byte-stable no-op against the immutable S3 records
(the publisher's head → decode → re-encode → create-only put already no-ops on
identical bytes). A genuinely different result (model change, different VLM
output, changed face embedding runtime) is a new logical run: new keys, and the
previous run's records stay as immutable history while the projection demotes
its run rows to non-current.

`ProcessingArtifact.from_dict` enforces `resultObject.objectKey ==
"objects/{sha256}"`, so every record — including promoted legacy artifacts —
must have a content-addressed object; the legacy `analysis/...` key is carried
in `sourceObjectKey` when present.

## Commit plan

1. **Content-addressed result + processing record.** Both live paths
   (`_publish_staged_analysis`, legacy `_execute`) derive the run id from the
   result bytes, write `objects/{sha}` through the publisher's create-only
   primitive, and publish the `ProcessingArtifact` record. The data-plane
   `analysis/` write is kept during migration (the promotion pass and older
   recovery paths read it). The DB run row now carries the deterministic id and
   the `objects/` key; `complete_ai_analysis` is otherwise unchanged.
2. **Faces and persons S3-first.** Clustering still reads the current
   Postgres projection (it is the efficient working set; the S3 records are
   the durability guarantee — when the DB set is empty but
   `manifests/faces/` is not, the S3 scan is the source). The new run's face
   records are published (deterministic ids, bounding box in dict form,
   embedding, confidence, assigned person), then PersonManifest revisions for
   every affected person (new persons get `uuid5` ids; a person's `faceIds`
   lists its *current* faces — faces of current runs — so a re-analysis adds
   the new run's faces and drops the old run's). `mutate_ai` mirrors
   `mutate_face`: operation id = run id, S3 publication under the mutation
   lock, then a pure `commit_ai_analysis` projection (idempotent,
   fail-closed on divergent rows, `record_reconciliation` on projection
   failure).
3. **Promotion pass.** A new CLI command publishes records for existing
   data-plane artifacts: run/face/person ids reuse the legacy Postgres ids when
   the rows exist (so reconcile already matches), otherwise the deterministic
   formulas; the legacy key goes to `sourceObjectKey`; the bytes are copied to
   `objects/{sha}` so the record validates and reconcile's object checks pass.
   Idempotent and re-runnable; `--apply` also (re)seeds the projection.
4. **Supporting changes.** GC's `CANONICAL_PREFIXES` gains `analysis/` and
   `analysis-stages/` so retention reporting covers the data-plane artifacts;
   rebuild sets `analysis_runs.is_current` per newest run for each
   `(asset_id, analysis_type)` instead of marking every rebuilt run current.

## Reconcile compatibility notes

- Reconcile compares `analysis_runs` on `{id, asset_id, analysis_type,
  pipeline_version, input_hash, object_key}` where `object_key =
  sourceObjectKey or resultObject.objectKey` — the live projection uses the
  `objects/` key, promoted runs use the legacy key, and both match their
  records by construction. `result`, `searchable_text`, `is_current`, and
  `created_at` are *not* compared, so the rebuild-time `is_current` fix and
  projection-time bookkeeping cannot create divergences.
- Face rows are compared field-for-field after bounding-box normalization
  (list ↔ dict), so projected rows must store exactly what the face record
  says, including `person_id`.
- Person comparison uses `all_people()` (all face rows per person). Historical
  face rows of a re-assigned person keep their person in the DB while the
  PersonManifest lists only current faces; the projection therefore
  re-assigns *all* face rows of an affected person (not just the new run's)
  so `all_people()` and the manifest agree.
- Orphaned and missing records are report-only (reconcile only auto-repairs
  missing rows, never overwrites divergent ones), so the migration window
  between a deploy and the promotion pass can show transient orphans without
  failing reconciliation.
