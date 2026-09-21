"""Typed response models for the JSON endpoints (OpenAPI codegen,
phases 1a-1b: asset browse/detail, then people reads; phase 2: upload and
health endpoints; phase 3a: album CRUD and restore; phase 3b: asset
mutations and queue operations; phase 4: people/face operation results and
the storage-verify report).

The four binary endpoints (original/preview/thumbnail/face-thumbnail) carry
no JSON 200 body, so they are documented with explicit media types plus a
``Pending202Out`` 202 instead of a response model (plan phase 4,
"declare binary, don't over-model").

These models describe exactly what the endpoints put on the wire: same key sets,
same nullability, same nesting as the dict literals the handlers build today
(the golden fixtures in tests/test_api_contract.py are the proof obligation).
They carry no business logic; all values arrive pre-computed from the catalog.

Design rules (plan decisions 3 and 7):
- ``extra="forbid"``: an unexpected key is a contract break and fails loud
  (500) instead of being silently dropped or passed through.
- Field types are strict primitives (``StrictInt``/``StrictStr``/...) so
  validation can never rewrite a wire value; ``UUID`` fields stay lax because
  str -> UUID -> str round-trips stably and the JSON body is always text.
  The one sanctioned exception is the int/float duality of JSON numbers:
  JSON has a single ``number`` type and every consumer treats ``1`` and
  ``1.0`` identically, so numeric fields are declared ``StrictFloat`` (which
  accepts both int and float input in this pydantic). The golden *comparison*
  (``normalize`` in ``tests/test_api_contract.py``) equates integral-valued
  floats with integers, and golden *recording* stays faithful (as emitted):
  a ``StrictFloat`` field converges every producer spelling to a float
  rendering, so the recorded bytes are deterministic. Settled in phase 1b,
  when Postgres jsonb's rendering of an integral ``float8`` confidence as
  the JSON integer ``1`` made the duality concrete.
- The asset manifest document exists in two schema versions (the v1 document
  has exactly 11 keys; v2 adds ``userState``/``deletedAt`` and a non-null
  ``mutation``). A single flat model cannot express both without adding or
  dropping keys, so the doc types are a discriminated union on
  ``schema_version``: one variant per version, each ``extra="forbid"``.
- ``dict[str, Any]`` is used only for genuinely open fields (EXIF ``metadata``,
  derived ``technical``, ``Mutation.changes``); the container itself is enforced.

The OpenAPI spec emitted from these models is the source of truth for client
code generation; the golden fixtures pin the wire bytes.
"""

from typing import Annotated, Any, Literal, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr
from pydantic.alias_generators import to_camel

# The 12 semantic photo classes the VLM reports (analysis.py SemanticAnalysis).
PHOTO_TYPES = Literal[
    "portrait",
    "group",
    "street",
    "travel",
    "landscape",
    "wildlife",
    "architecture",
    "event",
    "food",
    "document",
    "screenshot",
    "other",
]

# The 10 mutation actions a manifest can carry (models.py Mutation).
MUTATION_ACTIONS = Literal[
    "asset.patch",
    "asset.delete",
    "asset.restore",
    "asset.migrate",
    "asset.metadata",
    "album.create",
    "album.patch",
    "album.delete",
    "album.restore",
    "burst.setRepresentative",
]


class ResponseModel(BaseModel):
    """Base class for all response models.

    camelCase on the wire (matching the hand-built dict literals), strict
    primitives, and ``extra="forbid"`` so any key drift fails loud.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class LocationOut(ResponseModel):
    """A location tag: a name plus an optional lat/long pair (models.py Location)."""

    name: StrictStr
    latitude: StrictFloat | None = Field(
        default=None, ge=-90, le=90, allow_inf_nan=False, description="Degrees north."
    )
    longitude: StrictFloat | None = Field(
        default=None, ge=-180, le=180, allow_inf_nan=False, description="Degrees east."
    )


class UserStateOut(ResponseModel):
    """User-edited state for one asset (models.py UserState)."""

    rating: StrictInt = Field(ge=0, le=5, description="0-5 star rating.")
    favorite: StrictBool
    caption: StrictStr
    keywords: list[StrictStr]
    location: LocationOut | None


class MutationOut(ResponseModel):
    """The last mutation applied to a v2 manifest or album document
    (models.py Mutation). ``entityId`` equals the owning asset/album id on
    the wire (the source model's validator enforces it); ``expectedRevision``
    is null when the client did not send one; ``changes`` is a genuinely
    open object (UserState patch fields, EXIF metadata, album
    name/description/membership)."""

    action: MUTATION_ACTIONS
    entity_id: UUID
    changes: dict[str, Any]
    expected_revision: StrictInt | None = Field(default=None, ge=1)


class BlobOut(ResponseModel):
    """One blob reference inside a manifest document (models.py Blob)."""

    blob_id: UUID
    role: Literal["ORIGINAL_RAW", "ORIGINAL_JPEG", "ORIGINAL_HEIF", "SIDECAR"]
    original_filename: StrictStr
    object_key: StrictStr
    sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$", description="Hex SHA-256.")
    size_bytes: StrictInt = Field(gt=0)
    mime_type: StrictStr


class PreviewStatusOut(ResponseModel):
    """Derivative (preview/thumbnail) job state for one asset (catalog.preview_status)."""

    status: Literal["missing", "pending", "running", "ready", "failed", "unavailable"]
    error: StrictStr | None


class ProcessingStatusOut(ResponseModel):
    """One reprocessing stage job (catalog.processing_status entries).

    ``job_type`` is the one snake_case key in an otherwise camelCase API:
    the catalog copies the database row mapping verbatim, and the goldens
    pin it, so the alias overrides the camelCase generator.
    """

    job_type: StrictStr = Field(alias="job_type")
    status: StrictStr
    attempts: StrictInt
    error: StrictStr | None


class AnalysisObjectOut(ResponseModel):
    """One detected object in a semantic analysis (analysis.py DetectedObject)."""

    name: StrictStr
    count: StrictInt = Field(ge=1)


class AnalysisFaceOut(ResponseModel):
    """One detected face, as exposed in the analysis status (catalog.analysis_status)."""

    face_index: StrictInt
    box: list[StrictFloat] = Field(description="Normalized [x0, y0, x1, y1].")
    confidence: StrictFloat
    person_id: UUID
    person_name: StrictStr | None


class AnalysisResultOut(ResponseModel):
    """The stored public analysis result (analysis_runs.result jsonb).

    This is exactly what the AI worker writes: the semantic document fields
    plus ``faceCount`` and ``personCount``.
    """

    summary: StrictStr
    photoTypes: list[PHOTO_TYPES]
    scene: StrictStr
    setting: Literal["indoor", "outdoor", "mixed", "unknown"]
    objects: list[AnalysisObjectOut]
    activities: list[StrictStr]
    tags: list[StrictStr]
    visibleText: list[StrictStr]
    faceCount: StrictInt
    personCount: StrictInt


class AnalysisStatusOut(ResponseModel):
    """AI analysis job + latest run for one asset (catalog.analysis_status)."""

    status: Literal["missing", "pending", "running", "ready", "failed"]
    attempts: StrictInt
    error: StrictStr | None
    run_id: UUID | None
    model: StrictStr | None
    model_version: StrictStr | None
    pipeline_version: StrictStr | None
    analyzed_at: StrictStr | None = Field(description="ISO-8601 timestamp of the current run.")
    artifact_key: StrictStr | None = Field(description="S3 key of the full analysis artifact.")
    result: AnalysisResultOut | None
    faces: list[AnalysisFaceOut]


class AssetDocV1Out(ResponseModel):
    """A v1 manifest document: exactly the 11 keys the v1 writer emits.

    v1 documents have ``revision`` fixed at 1, ``previousRevision`` null, and
    no ``userState``/``deletedAt``/``mutation`` keys at all.
    """

    schema_version: Literal[1]
    library_id: UUID
    asset_id: UUID
    revision: Literal[1]
    previous_revision: None
    operation_id: UUID
    primary_blob_id: UUID
    blobs: list[BlobOut] = Field(min_length=1)
    imported_at: StrictStr
    capture_time: StrictStr | None
    metadata: dict[str, Any]


class AssetDocV2Out(AssetDocV1Out):
    """A v2 manifest document: v1 fields plus user state and mutation ancestry."""

    schema_version: Literal[2]
    revision: StrictInt
    previous_revision: StrictInt
    user_state: UserStateOut
    deleted_at: StrictStr | None
    mutation: MutationOut


# The asset document as exposed by GET /assets: one variant per schema
# version, selected by the ``schemaVersion`` discriminator.
AssetDocOut = Annotated[Union[AssetDocV1Out, AssetDocV2Out], Field(discriminator="schema_version")]


class PhotoSummaryOut(ResponseModel):
    """One browse row (browsing.asset_summary): the compact card data."""

    asset_id: UUID
    original_filename: StrictStr
    media_type: Literal["RAW", "JPEG", "HEIF"]
    timeline_time: StrictStr
    date_source: Literal["capture", "import"]
    capture_time: StrictStr | None
    imported_at: StrictStr
    width: StrictInt | None
    height: StrictInt | None
    camera_make: StrictStr | None
    camera_model: StrictStr | None
    lens: StrictStr | None
    technical: dict[str, Any]
    size_bytes: StrictInt
    rating: StrictInt
    favorite: StrictBool
    caption: StrictStr
    deleted_at: StrictStr | None
    revision: StrictInt
    burst_id: UUID | None
    burst_size: StrictInt | None
    burst_representative_asset_id: UUID | None
    preview: PreviewStatusOut
    # asset_summary() always emits both as f-strings, so both are non-null
    # by construction (a null here is a caught bug, not a tolerated value).
    thumbnail_url: StrictStr
    preview_url: StrictStr


class BrowsePageOut(ResponseModel):
    """One page of GET /library/assets (catalog.browse)."""

    items: list[PhotoSummaryOut]
    total: StrictInt
    next_cursor: StrictStr | None = Field(
        description="Opaque cursor for the next page; null when exhausted."
    )


class BurstDetailOut(ResponseModel):
    """A burst cluster and its frames (catalog.burst_detail)."""

    burst_id: UUID
    representative_asset_id: UUID
    frames: list[PhotoSummaryOut]


class AssetDetailV1Out(AssetDocV1Out):
    """GET /assets/{id} for a v1 document: the document plus derived blocks."""

    technical: dict[str, Any]
    processing: list[ProcessingStatusOut]
    analysis: AnalysisStatusOut
    preview: PreviewStatusOut
    user_state: UserStateOut


class AssetDetailV2Out(AssetDocV2Out):
    """GET /assets/{id} for a v2 document: the document plus derived blocks.

    ``userState`` is inherited from the document variant; the endpoint
    overwrites it with the same (column-synced) values.
    """

    technical: dict[str, Any]
    processing: list[ProcessingStatusOut]
    analysis: AnalysisStatusOut
    preview: PreviewStatusOut


# The asset detail as exposed by GET /assets/{id}: one variant per schema
# version, selected by the ``schemaVersion`` discriminator.
AssetDetailOut = Annotated[
    Union[AssetDetailV1Out, AssetDetailV2Out], Field(discriminator="schema_version")
]


# ---------------------------------------------------------------------------
# Phase 1b: people reads (GET /people, GET /people/{id}).
# ---------------------------------------------------------------------------


class FaceRefOut(ResponseModel):
    """One face reference inside a person response.

    ``catalog.list_people`` builds its ``sampleFaces`` rows in SQL
    (``jsonb_build_object``) while ``catalog.person_detail`` builds its
    ``faces`` rows in Python, so the same numeric field arrives through two
    different decoders: the SQL path copies the stored jsonb and casts the
    ``float8`` confidence (jsonb normalizes 1.0 to the JSON integer 1),
    while the Python path parses the jsonb box column and reads the raw
    ``float8`` confidence. The field is therefore a plain ``StrictFloat``:
    it accepts both int and float input (no producer spelling can 500), the
    spec stays a clean ``{"type": "number"}``, and both producer paths
    converge on a float rendering on the wire; the golden comparison (see
    the module docstring) equates the spellings and the golden recording
    pins the emitted floats.
    """

    face_id: UUID
    asset_id: UUID
    original_filename: StrictStr
    box: list[StrictFloat] = Field(description="Normalized [x0, y0, x1, y1].")
    confidence: StrictFloat
    thumbnail_url: StrictStr


class PersonSummaryOut(ResponseModel):
    """One row of GET /people (catalog.list_people): counts plus up to four
    highest-confidence sample faces."""

    person_id: UUID
    display_name: StrictStr
    face_count: StrictInt
    photo_count: StrictInt
    sample_faces: list[FaceRefOut]


class PeoplePageOut(ResponseModel):
    """GET /people: the page plus named/unnamed counts over all matches."""

    items: list[PersonSummaryOut]
    total: StrictInt
    named: StrictInt
    unnamed: StrictInt


class PersonDetailOut(ResponseModel):
    """GET /people/{id} (catalog.person_detail): the paged face list."""

    person_id: UUID
    display_name: StrictStr
    face_count: StrictInt
    photo_count: StrictInt
    faces: list[FaceRefOut]


# ---------------------------------------------------------------------------
# Phase 2: upload and health endpoints.
#
# describe_batch() is the shared response of POST /upload-batches,
# GET /upload-batches, GET /upload-batches/{id}, and the 202 responses of
# POST .../seal and POST .../retry; the PUT file receipt and the DELETE
# abandoned-batch receipt have their own small shapes. created_at/sealed_at
# are epoch seconds: BIGINT columns in the catalog, JSON numbers on the wire
# (the frontend's UploadBatch.createdAt/sealedAt are numbers too).
#
# The batch status union deliberately includes "deleting": a transient state
# between claim_upload_batch_cleanup() and finish_upload_batch_cleanup() that
# the frontend's UploadBatchStatus does not declare but the wire can carry in
# that window, and the frozen-wire model must accept (decision 7).
# ---------------------------------------------------------------------------

UPLOAD_BATCH_STATUSES = Literal[
    "accepting",
    "queued",
    "processing",
    "complete",
    "failed",
    "deleting",
]

# The nine upload-file lifecycle states (catalog.upload_files.status); the
# frontend's UploadFileStatus declares the same union.
UPLOAD_FILE_STATUSES = Literal[
    "waiting",
    "uploading",
    "uploaded",
    "skipped",
    "queued",
    "processing",
    "imported",
    "duplicate",
    "failed",
]

# The four onboarding-job states (catalog.onboarding_jobs.status).
UPLOAD_JOB_STATUSES = Literal["pending", "running", "complete", "failed"]


class UploadFileOut(ResponseModel):
    """One file row of describe_batch(): the declared path/size/mime, the
    lifecycle state, the asset it became (null until onboarding completes),
    and the PUT URL (required files only)."""

    file_id: UUID
    path: StrictStr
    size_bytes: StrictInt
    mime_type: StrictStr | None
    required: StrictBool
    status: UPLOAD_FILE_STATUSES
    reason: StrictStr | None
    asset_id: UUID | None
    error: StrictStr | None
    upload_url: StrictStr | None


class UploadJobOut(ResponseModel):
    """One onboarding-job row of describe_batch(). The result is a free-form
    blob (plan decision 4): a dict once the job produces one, values
    unchecked."""

    job_id: UUID
    status: UPLOAD_JOB_STATUSES
    attempts: StrictInt
    result: dict[str, Any] | None
    error: StrictStr | None


class UploadBatchOut(ResponseModel):
    """describe_batch(): the batch header (epoch-second createdAt, nullable
    sealedAt) plus its file and onboarding-job rows."""

    batch_id: UUID
    status: UPLOAD_BATCH_STATUSES
    created_at: StrictInt
    sealed_at: StrictInt | None
    files: list[UploadFileOut]
    jobs: list[UploadJobOut]


class UploadFileReceipt(ResponseModel):
    """PUT /upload-batches/{id}/files/{fileId}: one of the three
    receive_file() returns (fresh upload, re-upload of an equal digest, or a
    full replay); all report status "uploaded"."""

    file_id: UUID
    status: Literal["uploaded"]
    sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    replayed: StrictBool


class BatchAbandonedOut(ResponseModel):
    """DELETE /upload-batches/{id}: the unsealed batch and its staged objects
    are gone."""

    batch_id: UUID
    status: Literal["deleted"]
    files_deleted: StrictInt
    multipart_uploads_aborted: StrictInt


class QueueCountsOut(ResponseModel):
    """catalog.queue_counts(): the thirteen per-queue counts (batches plus the
    onboarding, processing, preview, and analysis jobs)."""

    upload_batches_queued: StrictInt
    onboarding_pending: StrictInt
    onboarding_running: StrictInt
    onboarding_failed: StrictInt
    processing_pending: StrictInt
    processing_running: StrictInt
    processing_failed: StrictInt
    preview_pending: StrictInt
    preview_running: StrictInt
    preview_failed: StrictInt
    analysis_pending: StrictInt
    analysis_running: StrictInt
    analysis_failed: StrictInt


class UploadQueueStatusOut(QueueCountsOut):
    """GET /upload-queue: the queue counts plus the UploadGate's in-process
    transfer statistics (workers, active, waiting)."""

    upload_workers: StrictInt
    uploads_active: StrictInt
    uploads_waiting: StrictInt


class HealthOut(QueueCountsOut):
    """GET /health: the endpoint's merged shape — fixed "ok", the library id,
    asset/blob counts, every queue count, the upload-gate statistics, and the
    latest PostgreSQL backup marker in the media bucket (null until the first
    backup)."""

    status: Literal["ok"]
    library_id: UUID
    assets: StrictInt
    blobs: StrictInt
    upload_workers: StrictInt
    uploads_active: StrictInt
    uploads_waiting: StrictInt
    postgres_backup_key: StrictStr | None
    postgres_backup_at: StrictStr | None


# ---------------------------------------------------------------------------
# Phase 3a: albums (CRUD + restore).
#
# All six album operations return the committed album document: the
# models.py Album DurableModel fields in declaration order plus the mutation
# that produced the revision. ``album.document()`` always carries
# ``mutation`` — the frontend's Album interface never declared it, but it is
# part of the frozen wire, so the model keeps it (decision 7).
# ---------------------------------------------------------------------------

class AlbumOut(ResponseModel):
    """A committed album document (models.py Album).

    ``previousRevision`` is null on the first revision and the previous
    revision number after that (catalog._apply_album); ``deletedAt`` is the
    deletion's wall-clock ISO timestamp while the album is hidden and null
    otherwise; ``assetIds`` is the membership in display order (the catalog
    rejects duplicates and hidden additions at commit time).
    """

    schema_version: Literal[1]
    library_id: UUID
    album_id: UUID
    revision: StrictInt = Field(ge=1, le=99999999)
    previous_revision: StrictInt | None = Field(ge=1)
    operation_id: UUID
    mutation: MutationOut
    name: StrictStr = Field(min_length=1, max_length=200)
    description: StrictStr = Field(max_length=10000)
    asset_ids: list[UUID] = Field(max_length=100000)
    deleted_at: StrictStr | None


# ---------------------------------------------------------------------------
# Phase 3b: asset mutations and queue operations.
#
# The four asset-mutation endpoints (user-state/metadata patch, delete,
# restore) all return state.mutation_result(): the committed v2 user state
# plus the mutation's identity. The burst representative endpoint returns
# the cluster's new identity pair. The three queue endpoints (POST
# /processing, POST /analysis, POST /assets/{id}/analysis/retry) share
# catalog.queue_processing's count shape, and the preview retry echoes
# catalog.preview_status.
# ---------------------------------------------------------------------------

class MutationResultOut(UserStateOut):
    """A committed asset mutation result (state.mutation_result for a
    Manifest).

    The five UserState fields are the post-mutation user state (a patch
    merges into the previous state, so unset fields echo their previous
    values). ``deletedAt`` is the deletion's wall-clock ISO timestamp while
    the asset is hidden and null otherwise: delete results always carry a
    stamp, patch/restore results always null. ``revision`` is the new
    revision number (a mutation always increments, so never 1).
    """

    asset_id: UUID
    operation_id: UUID
    revision: StrictInt
    deleted_at: StrictStr | None


class BurstRepresentativeOut(ResponseModel):
    """POST /assets/{id}/burst/representative: the burst cluster and its
    newly designated representative frame (bursts.set_representative)."""

    burst_id: UUID
    representative_asset_id: UUID


class QueueResultOut(ResponseModel):
    """The shared response of POST /processing, POST /analysis, and
    POST /assets/{id}/analysis/retry (catalog.queue_processing).

    ``assets`` is the number of selected assets; the three job counters
    partition the selected asset/job-type pairs into newly (re)queued rows,
    rows already pending, and rows already running. ``jobTypes`` is the
    database job type per selected stage: ``["metadata-v1"]`` for
    /processing, ``["ai-v1"]`` for /analysis.
    """

    assets: StrictInt
    jobs_queued: StrictInt
    jobs_already_queued: StrictInt
    jobs_already_running: StrictInt
    job_types: list[StrictStr]


# ---------------------------------------------------------------------------
# Phase 4: the durable face-operation results (catalog.commit_face_operation)
# and the storage-integrity report (service.verify).
# ---------------------------------------------------------------------------


class PersonRenameOut(ResponseModel):
    """PATCH /people/{id}: the ``person.rename`` result. ``displayName`` is
    the new display name; the empty string is a valid value (it clears the
    name). The same body is returned for an idempotent replay of the stored
    operation."""

    operation_id: UUID
    person_id: UUID
    display_name: StrictStr


class PersonMergeOut(ResponseModel):
    """POST /people/{id}/merge: the ``person.merge`` result. ``personId`` is
    the surviving target (the merge keeps its name); ``mergedPersonId`` is
    the source whose faces were moved over and whose row was deleted;
    ``movedFaces`` counts the transferred face rows."""

    operation_id: UUID
    person_id: UUID
    merged_person_id: UUID
    moved_faces: StrictInt


class FaceMoveOut(ResponseModel):
    """POST /faces/move: the ``faces.move`` result. ``personId`` is the
    target person — the client's ``targetPersonId`` when one was sent, or
    the newly created group otherwise (signalled by ``createdPerson``).
    Note the wire dict does not echo the request's ``targetPersonId``; the
    client reads the destination back from ``personId``.
    ``movedFaces`` counts the reassigned face rows."""

    operation_id: UUID
    person_id: UUID
    moved_faces: StrictInt
    created_person: StrictBool


class VerifyErrorOut(ResponseModel):
    """One failed check in a POST /maintenance/verify report: the object
    key (or ``album:<albumId>`` for a dangling album reference) and the
    failure message."""

    key: StrictStr
    error: StrictStr


class VerifyOut(ResponseModel):
    """POST /maintenance/verify: the storage-integrity report.
    ``assetsChecked``/``blobsChecked`` count the manifests and the blobs
    that passed; ``verification`` is ``"size"`` for the default head-only
    pass and ``"sha256"`` for ``?full=true``; ``errors`` is empty when
    every blob and album reference checked out."""

    assets_checked: StrictInt
    blobs_checked: StrictInt
    verification: Literal["sha256", "size"]
    errors: list[VerifyErrorOut]


class Pending202Out(ResponseModel):
    """The 202 body of the binary derivative endpoints (asset
    preview/thumbnail, face thumbnail) when the JPEG is not ready yet:
    ``{"status": "pending"}`` plus a ``Retry-After: 2`` header, which the
    client polls on (PreviewImage.tsx, FaceThumbnail.tsx). Declared in the
    spec only (the ``responses=`` 202 entry) — the handlers emit this
    JSONResponse directly and never validate through the model."""

    status: Literal["pending"]
