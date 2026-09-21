"""Phase 4: AI worker integration for burst semantic-analysis reuse.

Drives the real ``AIWorker`` against a live disposable catalog to verify the
acceptance criteria from ``docs/burst-semantic-analysis-reuse.md``: the first
burst frame invokes the VLM while a near-identical frame reuses its semantics;
face rows stay asset-specific; the reused run carries its own provenance;
retries are idempotent and ``forceFull`` recomputes; a model or pipeline change
(or an unknown digest) forces computation; observe mode reports a match but
still invokes the VLM; deleting the source does not invalidate the target; and
reordered jobs never publish another asset's face data.

The VLM (``analyze_semantics``), the Ollama digest lookup
(``resolve_model_digest``), and the face detector (``faces``) are stubbed so the
tests never start the AI worker services or touch CUDA/Ollama. Only the reuse
decision, fingerprint persistence, and the atomic publish path run for real.

Phase 3A's service gate is in place for every test here: both service URLs
are stub-configured and both probes stubbed healthy, so the worker claims
exactly as before the gate existed.
"""

from uuid import uuid4

from PIL import Image, ImageDraw
from sqlalchemy import text
from test_integration import backend as backend  # noqa: F401
from test_integration import pytestmark  # noqa: F401

import photo_server.ai_worker as ai_worker
from photo_server.ai_worker import AIWorker
from photo_server.analysis import SemanticAnalysis
from photo_server.fingerprints import BURST_HASH_VERSION
from photo_server.models import Blob, Manifest, Mutation
from photo_server.reuse import REUSE_POLICY_VERSION
from photo_server.state import mutate
from photo_server.worker import cache_paths

DIGEST = "abc123def456"
CAMERA = {"Make": "Canon", "Model": "EOS R5"}

FAKE_SEMANTIC = SemanticAnalysis(
    summary="A quiet outdoor scene.",
    photo_types=["landscape"],
    scene="outdoors",
    setting="outdoor",
)


# --- PIL-generated near-duplicate previews (no external assets). ---


def base_image() -> Image.Image:
    width, height = 640, 480
    img = Image.new("L", (width, height))
    img.putdata([40 + int(180 * x / (width - 1)) for _ in range(height) for x in range(width)])
    ImageDraw.Draw(img).rectangle([120, 120, 300, 380], fill=250)
    return img


def shifted_image() -> Image.Image:
    # A uniform +2 brightness shift keeps pHash/dHash identical and the pixel
    # similarity comfortably above the 0.95 threshold.
    return base_image().point(lambda p: min(255, p + 2))


# --- Catalog / worker helpers. ---


def add_asset(backend, capture_time=None, metadata=None) -> str:
    asset_id = uuid4()
    blob_id = uuid4()
    blob = Blob(
        blob_id=blob_id,
        role="ORIGINAL_JPEG",
        original_filename="sample.JPG",
        object_key=f"originals/{asset_id}/sample.JPG",
        sha256=uuid4().hex + uuid4().hex,
        size_bytes=100,
        mime_type="image/jpeg",
    )
    manifest = Manifest(
        library_id=backend.service.library_id,
        asset_id=asset_id,
        operation_id=uuid4(),
        primary_blob_id=blob_id,
        blobs=[blob],
        imported_at="2026-01-01T00:00:00+00:00",
        capture_time=capture_time,
        metadata=metadata or {},
    )
    backend.service.catalog.apply(manifest)
    return str(asset_id)


def make_preview(backend, asset_id: str, image: Image.Image) -> None:
    catalog = backend.service.catalog
    manifest = catalog.get(asset_id)
    paths = cache_paths(backend.service, manifest)
    paths["preview"].parent.mkdir(parents=True, exist_ok=True)
    image.save(paths["preview"], format="JPEG")
    catalog.finish_processing_job(asset_id, "preview-v1", "ready")


def park_ai_job(backend, asset_id: str) -> None:
    """Make an asset's ai-v1 job unclaimable until it is requeued."""
    backend.service.catalog.finish_processing_job(asset_id, "ai-v1", "ready")


def requeue_ai(backend, asset_id: str, force_full: bool = False) -> None:
    backend.service.catalog.queue_processing([asset_id], ["ai-v1"], force_full=force_full)


class FaceStub:
    model_version = "stub"

    def analyze(self, jpeg) -> list[dict]:
        return [{"box": [0.1, 0.1, 0.2, 0.2], "confidence": 0.9, "embedding": [1.0, 0.0]}]


def make_worker(backend, monkeypatch, calls: list, digest: str = DIGEST, pipeline_version=None):
    # Phase 3A gate: both service URLs stub-configured (non-empty) and both
    # probes stubbed healthy, so the worker claims exactly as before the gate
    # existed.
    backend.service.settings = backend.service.settings.model_copy(
        update={"ai_base_url": "http://vlm-stub/v1", "face_service_url": "http://face-stub/"}
    )
    worker = AIWorker(backend.service)
    worker._faces = FaceStub()
    monkeypatch.setattr(worker, "_probe_vlm", lambda: True)
    monkeypatch.setattr(worker, "_probe_face", lambda: True)

    def fake_analyze_semantics(settings, jpeg):
        calls[0] += 1
        return FAKE_SEMANTIC, digest, {}

    monkeypatch.setattr(ai_worker, "analyze_semantics", fake_analyze_semantics)
    monkeypatch.setattr(ai_worker, "resolve_model_digest", lambda settings, model: digest)
    if pipeline_version is not None:
        monkeypatch.setattr(ai_worker, "PIPELINE_VERSION", pipeline_version)
    return worker


def current_run(backend, asset_id: str) -> dict:
    with backend.service.catalog.engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT id, semantic_origin, source_run_id, reuse_policy_version, "
                "similarity, model_name, model_version, pipeline_version, input_hash, "
                "object_key, searchable_text, result "
                "FROM analysis_runs WHERE asset_id = :a AND is_current = true"
            ),
            {"a": asset_id},
        ).mappings().one()
    return dict(row)


def face_rows(backend, run_id: str) -> list[dict]:
    with backend.service.catalog.engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT id, asset_id, analysis_run_id, person_id, face_index "
                "FROM faces WHERE analysis_run_id = :run"
            ),
            {"run": run_id},
        ).mappings().all()
    return [dict(r) for r in rows]


def enable_reuse(backend) -> None:
    backend.service.settings = backend.service.settings.model_copy(
        update={"ai_semantic_reuse_mode": "on"}
    )


# --- Acceptance criteria. ---


def test_first_frame_computed_then_near_duplicate_reused(backend, monkeypatch):
    calls = [0]
    worker = make_worker(backend, monkeypatch, calls)
    enable_reuse(backend)

    source_id = add_asset(backend, capture_time="2026-01-01T12:00:00+00:00", metadata=CAMERA)
    make_preview(backend, source_id, base_image())
    source_result = worker.run_once()
    assert source_result["status"] == "ready"
    assert source_result["semanticOrigin"] == "computed"
    assert calls[0] == 1

    target_id = add_asset(backend, capture_time="2026-01-01T12:00:01+00:00", metadata=CAMERA)
    make_preview(backend, target_id, shifted_image())
    target_result = worker.run_once()
    assert target_result["status"] == "ready"
    assert target_result["semanticOrigin"] == "reused"
    assert calls[0] == 1  # the near-duplicate did not invoke the VLM

    source_run = current_run(backend, source_id)
    target_run = current_run(backend, target_id)
    # The reused run has its own input hash and object key, and inherits the
    # source's searchable text.
    assert target_run["input_hash"] != source_run["input_hash"]
    assert target_id in target_run["object_key"]
    assert target_run["searchable_text"] == source_run["searchable_text"]
    # Provenance points at the source run under the burst-reuse policy.
    assert target_run["source_run_id"] == source_run["id"]
    assert target_run["reuse_policy_version"] == REUSE_POLICY_VERSION
    assert target_run["similarity"]["policyVersion"] == REUSE_POLICY_VERSION
    assert target_run["similarity"]["fingerprintVersion"] == BURST_HASH_VERSION


def test_reused_run_stores_distinct_face_rows(backend, monkeypatch):
    calls = [0]
    worker = make_worker(backend, monkeypatch, calls)
    enable_reuse(backend)

    source_id = add_asset(backend, capture_time="2026-01-01T12:00:00+00:00", metadata=CAMERA)
    make_preview(backend, source_id, base_image())
    worker.run_once()
    target_id = add_asset(backend, capture_time="2026-01-01T12:00:01+00:00", metadata=CAMERA)
    make_preview(backend, target_id, shifted_image())
    worker.run_once()

    source_faces = face_rows(backend, current_run(backend, source_id)["id"])
    target_faces = face_rows(backend, current_run(backend, target_id)["id"])
    assert len(source_faces) == 1
    assert len(target_faces) == 1
    # Each run's face row belongs to its own asset and has a distinct row id.
    assert source_faces[0]["asset_id"] == source_id
    assert target_faces[0]["asset_id"] == target_id
    assert source_faces[0]["id"] != target_faces[0]["id"]


def test_reused_run_provenance_in_database_and_artifact(backend, monkeypatch):
    calls = [0]
    worker = make_worker(backend, monkeypatch, calls)
    enable_reuse(backend)

    source_id = add_asset(backend, capture_time="2026-01-01T12:00:00+00:00", metadata=CAMERA)
    make_preview(backend, source_id, base_image())
    worker.run_once()
    target_id = add_asset(backend, capture_time="2026-01-01T12:00:01+00:00", metadata=CAMERA)
    make_preview(backend, target_id, shifted_image())
    worker.run_once()

    source_run = current_run(backend, source_id)
    target_run = current_run(backend, target_id)
    assert target_run["semantic_origin"] == "reused"
    assert target_run["source_run_id"] == source_run["id"]
    assert target_run["reuse_policy_version"] == REUSE_POLICY_VERSION
    assert target_run["similarity"]["pixelSimilarity"] >= 0.95

    artifact = backend.service.storage.get_json(target_run["object_key"])
    assert artifact["assetId"] == target_id
    assert artifact["inputSha256"] == target_run["input_hash"]
    assert artifact["semanticOrigin"] == "reused"
    assert artifact["semanticSource"] == {"assetId": source_id, "runId": source_run["id"]}
    assert artifact["similarity"] == target_run["similarity"]


def test_retry_is_idempotent(backend, monkeypatch):
    calls = [0]
    worker = make_worker(backend, monkeypatch, calls)
    enable_reuse(backend)

    source_id = add_asset(backend, capture_time="2026-01-01T12:00:00+00:00", metadata=CAMERA)
    make_preview(backend, source_id, base_image())
    first = worker.run_once()
    assert first["status"] == "ready"
    assert calls[0] == 1

    # A normal retry recomputes (no qualifying near-duplicate) and publishes a
    # new current run, leaving the job ready.
    requeue_ai(backend, source_id)
    retry = worker.run_once()
    assert retry["status"] == "ready"
    assert calls[0] == 2
    retry_run = current_run(backend, source_id)
    assert retry_run["id"] != first["runId"]
    assert retry_run["semantic_origin"] == "computed"


def test_force_full_invokes_vlm(backend, monkeypatch):
    calls = [0]
    worker = make_worker(backend, monkeypatch, calls)
    enable_reuse(backend)

    source_id = add_asset(backend, capture_time="2026-01-01T12:00:00+00:00", metadata=CAMERA)
    make_preview(backend, source_id, base_image())
    worker.run_once()
    assert calls[0] == 1

    target_id = add_asset(backend, capture_time="2026-01-01T12:00:01+00:00", metadata=CAMERA)
    make_preview(backend, target_id, shifted_image())
    # Without force_full this target would reuse the source; force it to recompute.
    requeue_ai(backend, target_id, force_full=True)
    result = worker.run_once()
    assert result["status"] == "ready"
    assert result["semanticOrigin"] == "computed"
    assert calls[0] == 2
    target_run = current_run(backend, target_id)
    assert target_run["semantic_origin"] == "computed"
    assert target_run["source_run_id"] is None


def test_model_digest_change_forces_computation(backend, monkeypatch):
    calls = [0]
    worker = make_worker(backend, monkeypatch, calls)
    enable_reuse(backend)

    source_id = add_asset(backend, capture_time="2026-01-01T12:00:00+00:00", metadata=CAMERA)
    make_preview(backend, source_id, base_image())
    worker.run_once()
    assert calls[0] == 1

    # The model changed: the current digest no longer matches the source's.
    monkeypatch.setattr(ai_worker, "resolve_model_digest", lambda settings, model: "zzz999unknown")
    target_id = add_asset(backend, capture_time="2026-01-01T12:00:01+00:00", metadata=CAMERA)
    make_preview(backend, target_id, shifted_image())
    result = worker.run_once()
    assert result["status"] == "ready"
    assert result["semanticOrigin"] == "computed"
    assert calls[0] == 2


def test_pipeline_change_forces_computation(backend, monkeypatch):
    calls = [0]
    worker = make_worker(backend, monkeypatch, calls)
    enable_reuse(backend)

    source_id = add_asset(backend, capture_time="2026-01-01T12:00:00+00:00", metadata=CAMERA)
    make_preview(backend, source_id, base_image())
    worker.run_once()
    assert calls[0] == 1

    # The pipeline version changed after the source was analyzed.
    monkeypatch.setattr(ai_worker, "PIPELINE_VERSION", "photo-ai-v2")
    target_id = add_asset(backend, capture_time="2026-01-01T12:00:01+00:00", metadata=CAMERA)
    make_preview(backend, target_id, shifted_image())
    result = worker.run_once()
    assert result["status"] == "ready"
    assert result["semanticOrigin"] == "computed"
    assert calls[0] == 2


def test_unknown_digest_disables_reuse(backend, monkeypatch):
    calls = [0]
    worker = make_worker(backend, monkeypatch, calls, digest="unknown")
    enable_reuse(backend)

    source_id = add_asset(backend, capture_time="2026-01-01T12:00:00+00:00", metadata=CAMERA)
    make_preview(backend, source_id, base_image())
    worker.run_once()
    assert calls[0] == 1
    assert current_run(backend, source_id)["semantic_origin"] == "computed"

    target_id = add_asset(backend, capture_time="2026-01-01T12:00:01+00:00", metadata=CAMERA)
    make_preview(backend, target_id, shifted_image())
    result = worker.run_once()
    assert result["status"] == "ready"
    assert result["semanticOrigin"] == "computed"
    assert calls[0] == 2


def test_observe_mode_reports_match_but_still_invokes_vlm(backend, monkeypatch):
    calls = [0]
    worker = make_worker(backend, monkeypatch, calls)
    # The default mode is "observe"; leave it unchanged.

    source_id = add_asset(backend, capture_time="2026-01-01T12:00:00+00:00", metadata=CAMERA)
    make_preview(backend, source_id, base_image())
    source_result = worker.run_once()
    assert source_result["status"] == "ready"
    assert source_result["semanticOrigin"] == "computed"
    assert calls[0] == 1

    target_id = add_asset(backend, capture_time="2026-01-01T12:00:01+00:00", metadata=CAMERA)
    make_preview(backend, target_id, shifted_image())
    target_result = worker.run_once()
    assert target_result["status"] == "ready"
    # Observe mode still invokes the VLM for the near-duplicate...
    assert target_result["semanticOrigin"] == "computed"
    assert calls[0] == 2
    # ...but reports the match it would have reused.
    source_run = current_run(backend, source_id)
    assert target_result["reuseMatch"] == {"assetId": source_id, "runId": source_run["id"]}
    # The published run is computed, not reused.
    target_run = current_run(backend, target_id)
    assert target_run["semantic_origin"] == "computed"
    assert target_run["source_run_id"] is None


def test_deleting_source_does_not_invalidate_target(backend, monkeypatch):
    calls = [0]
    worker = make_worker(backend, monkeypatch, calls)
    enable_reuse(backend)

    source_id = add_asset(backend, capture_time="2026-01-01T12:00:00+00:00", metadata=CAMERA)
    make_preview(backend, source_id, base_image())
    worker.run_once()
    target_id = add_asset(backend, capture_time="2026-01-01T12:00:01+00:00", metadata=CAMERA)
    make_preview(backend, target_id, shifted_image())
    worker.run_once()
    target_run = current_run(backend, target_id)
    assert target_run["semantic_origin"] == "reused"
    assert target_run["source_run_id"] is not None

    # Soft-delete the source asset.
    mutate(backend.service, uuid4(), Mutation(action="asset.delete", entity_id=source_id, changes={}))

    # The target's already-published result stays ready and valid.
    after = current_run(backend, target_id)
    assert after["id"] == target_run["id"]
    assert after["semantic_origin"] == "reused"
    assert after["source_run_id"] == target_run["source_run_id"]
    assert after["result"] == target_run["result"]
    status = backend.service.catalog.analysis_status(target_id)
    assert status["status"] == "ready"
    assert status["runId"] == target_run["id"]


def test_reordered_jobs_keep_face_rows_asset_specific(backend, monkeypatch):
    calls = [0]
    worker = make_worker(backend, monkeypatch, calls)
    enable_reuse(backend)

    first_id = add_asset(backend, capture_time="2026-01-01T12:00:00+00:00", metadata=CAMERA)
    second_id = add_asset(backend, capture_time="2026-01-01T12:00:01+00:00", metadata=CAMERA)
    # Process the second frame first; park the first so it is not claimed.
    park_ai_job(backend, first_id)
    make_preview(backend, second_id, shifted_image())
    second_result = worker.run_once()
    assert second_result["assetId"] == second_id
    assert second_result["semanticOrigin"] == "computed"
    make_preview(backend, first_id, base_image())
    requeue_ai(backend, first_id)
    first_result = worker.run_once()
    assert first_result["assetId"] == first_id
    assert first_result["semanticOrigin"] == "reused"

    # Every face row belongs only to its own asset's run, even though the
    # first frame reused the second frame's semantics.
    for run, asset_id in (
        (current_run(backend, first_id), first_id),
        (current_run(backend, second_id), second_id),
    ):
        for row in face_rows(backend, run["id"]):
            assert row["asset_id"] == asset_id
