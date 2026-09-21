"""Phase 7: end-to-end rollout verification for burst semantic-analysis reuse.

Drives the full pipeline against a live disposable catalog: a short burst is
imported from real (EXIF-stamped) JPEG files, the ordinary worker renders the
previews, and the AI worker analyzes the frames with the VLM and face detector
stubbed. The test verifies the rollout acceptance criteria from
``docs/burst-semantic-analysis-reuse.md`` under both ``observe`` and ``on``
modes:

- under ``on`` the first claimed frame computes its semantics while the two
  near-identical frames reuse them (the VLM is invoked exactly once);
- under ``observe`` every frame computes, but the two frames that would have
  reused report a ``reuseMatch`` instead of silently reusing;
- the operational counters (``semanticComputed``/``semanticReused``,
  ``rejectionsByGate``, ``forcedFull``, ``wouldHaveReused``,
  ``vlmTimeAvoided``, ``stageFailures``) are emitted on every result;
- the three frames cluster into one burst, and best-shot selection via
  ``burst.setRepresentative`` is reversible and idempotent;
- a face-stage failure is reported with its stage and ``stageFailures`` counter.

The AI worker services are never started: ``analyze_semantics``,
``resolve_model_digest``, and the face detector are stubbed, so no CUDA or
Ollama work happens and no production data is touched.

Phase 3A's service gate is in place for every test here: both service URLs
are stub-configured and both probes stubbed healthy, so the worker claims
exactly as before the gate existed.
"""

from uuid import UUID, uuid4

import pytest
from PIL import Image, ImageDraw
from test_integration import backend as backend  # noqa: F401
from test_integration import pytestmark  # noqa: F401

import photo_server.ai_worker as ai_worker
from photo_server.ai_worker import AIWorker
from photo_server.analysis import SemanticAnalysis
from photo_server.models import Mutation
from photo_server.state import mutate
from photo_server.worker import run_once

DIGEST = "abc123def456"
CAMERA = {"Make": "Canon", "Model": "EOS R5"}

FAKE_SEMANTIC = SemanticAnalysis(
    summary="A quiet outdoor scene.",
    photo_types=["landscape"],
    scene="outdoors",
    setting="outdoor",
)


# --- PIL-generated near-duplicate frames (no external assets). ---


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


def write_frame(path, image: Image.Image, seconds: int) -> None:
    """Save ``image`` as a JPEG stamped with camera EXIF and a capture time."""
    img = image.convert("RGB")
    exif = img.getexif()
    exif[0x010F] = CAMERA["Make"]
    exif[0x0110] = CAMERA["Model"]
    ifd = exif.get_ifd(0x8769)
    ifd[0x9003] = f"2026:01:01 12:00:{seconds:02d}"
    ifd[0x9011] = "+00:00"
    img.save(path, format="JPEG", exif=exif)


# --- Worker stubs (never touch CUDA/Ollama). ---


class FaceStub:
    model_version = "stub"

    def analyze(self, jpeg) -> list[dict]:
        return [{"box": [0.1, 0.1, 0.2, 0.2], "confidence": 0.9, "embedding": [1.0, 0.0]}]


class FailingFaceStub:
    """Fails the first face analysis, then succeeds.

    This proves a failed job is not retried (the next claim is a different
    asset) while the remaining frames can still complete.
    """

    model_version = "stub"

    def __init__(self) -> None:
        self.calls = 0

    def analyze(self, jpeg) -> list[dict]:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("simulated face detector failure")
        return [{"box": [0.1, 0.1, 0.2, 0.2], "confidence": 0.9, "embedding": [1.0, 0.0]}]


def make_worker(backend, monkeypatch, calls: list, faces=None) -> AIWorker:
    # Phase 3A gate: both service URLs stub-configured (non-empty) and both
    # probes stubbed healthy, so the worker claims exactly as before the gate
    # existed.
    backend.service.settings = backend.service.settings.model_copy(
        update={"ai_base_url": "http://vlm-stub/v1", "face_service_url": "http://face-stub/"}
    )
    worker = AIWorker(backend.service)
    worker._faces = faces or FaceStub()
    monkeypatch.setattr(worker, "_probe_vlm", lambda: True)
    monkeypatch.setattr(worker, "_probe_face", lambda: True)

    def fake_analyze_semantics(settings, jpeg):
        calls[0] += 1
        return FAKE_SEMANTIC, DIGEST, {}

    monkeypatch.setattr(ai_worker, "analyze_semantics", fake_analyze_semantics)
    monkeypatch.setattr(ai_worker, "resolve_model_digest", lambda settings, model: DIGEST)
    return worker


def import_burst(backend) -> list[str]:
    """Import one three-frame burst and return the asset ids in file order."""
    frames = [
        ("burst-a.jpg", base_image(), 0),
        ("burst-b.jpg", shifted_image(), 1),
        ("burst-c.jpg", base_image(), 2),
    ]
    for name, image, seconds in frames:
        write_frame(backend.root / name, image, seconds)
    result = backend.service.import_batch(
        [name for name, _, _ in frames], uuid4()
    )
    by_path = {entry["path"]: entry for entry in result["results"]}
    for name, _, _ in frames:
        entry = by_path[name]
        assert entry["status"] == "imported", result
    # The capture time and camera identity must survive the import: the reuse
    # policy and the burst candidate search both depend on them.
    for name, _, seconds in frames:
        asset_id = by_path[name]["assetId"]
        manifest = backend.service.catalog.get(asset_id)
        assert manifest.capture_time == f"2026-01-01T12:00:{seconds:02d}+00:00", (
            name,
            manifest.capture_time,
        )
        assert manifest.metadata.get("Make") == CAMERA["Make"]
        assert manifest.metadata.get("Model") == CAMERA["Model"]
    return [by_path[name]["assetId"] for name, _, _ in frames]


def drive_previews(backend, asset_ids: set[str]) -> None:
    """Run the ordinary worker until every burst frame has a ready preview."""
    ready: set[str] = set()
    for _ in range(20):
        result = run_once(backend.service)
        if result is None:
            break
        if result["jobType"] == "preview":
            if result["status"] != "ready":
                pytest.fail(f"Preview job not ready: {result}")
            ready.add(result["assetId"])
    assert ready == asset_ids, f"Previews ready for {ready}, expected {asset_ids}"


# --- Acceptance criteria. ---


@pytest.mark.parametrize("reuse_mode", ["observe", "on"])
def test_burst_rollout(backend, monkeypatch, reuse_mode):
    asset_ids = import_burst(backend)
    drive_previews(backend, set(asset_ids))

    calls = [0]
    worker = make_worker(backend, monkeypatch, calls)
    backend.service.settings = backend.service.settings.model_copy(
        update={"ai_semantic_reuse_mode": reuse_mode}
    )

    results = [worker.run_once() for _ in range(3)]
    assert all(result is not None for result in results), results
    assert all(result["status"] == "ready" for result in results), results
    assert {result["assetId"] for result in results} == set(asset_ids)

    # --- Mode-specific reuse behavior. ---
    if reuse_mode == "on":
        # The first claimed frame computes; the two near-duplicates reuse it.
        # (Claim order is by asset id, so identify the computed frame by its
        # origin rather than by import position.)
        computed = [result for result in results if result["semanticOrigin"] == "computed"]
        reused = [result for result in results if result["semanticOrigin"] == "reused"]
        assert len(computed) == 1 and len(reused) == 2, results
        assert calls[0] == 1  # the VLM was invoked exactly once
        for result in reused:
            assert result["counters"]["semanticReused"] == 1
            assert result["counters"]["semanticComputed"] == 0
            assert result["counters"]["vlmTimeAvoided"] is not None
            assert "reuseMatch" not in result  # only observe mode reports matches
        for result in computed:
            assert result["counters"]["semanticComputed"] == 1
            assert result["counters"]["semanticReused"] == 0
    else:
        # Observe mode never reuses: every frame invokes the VLM, but the two
        # frames that would have reused report the match they would have used.
        assert all(result["semanticOrigin"] == "computed" for result in results), results
        assert calls[0] == 3
        matched = [result for result in results if "reuseMatch" in result]
        assert len(matched) == 2, results
        for result in matched:
            assert result["reuseMatch"]["assetId"] in set(asset_ids)
            assert result["reuseMatch"]["assetId"] != result["assetId"]
            assert result["reuseMatch"]["runId"]
            assert result["counters"]["semanticComputed"] == 1

    # --- Operational counters are emitted on every result. ---
    for result in results:
        counters = result["counters"]
        assert set(counters) == {
            "semanticComputed",
            "semanticReused",
            "forcedFull",
            "wouldHaveReused",
            "vlmTimeAvoided",
            "rejectionsByGate",
            "stageFailures",
        }
        assert counters["forcedFull"] == 0
        assert counters["wouldHaveReused"] == 0
        assert isinstance(counters["rejectionsByGate"], dict)
        assert counters["stageFailures"] == {
            "setup": 0,
            "fingerprint": 0,
            "face": 0,
            "semantic": 0,
        }
        assert counters["semanticComputed"] + counters["semanticReused"] == 1
        # Face observations are per-asset even for reused runs.
        assert result["faceCount"] == 1
        assert result["personCount"] >= 1

    # --- Clustering: the three frames stack into one burst. ---
    details = {
        asset_id: backend.service.catalog.burst_detail(asset_id) for asset_id in asset_ids
    }
    assert all(detail is not None for detail in details.values()), details
    burst_ids = {detail["burstId"] for detail in details.values()}
    assert len(burst_ids) == 1  # one shared cluster
    burst_id = next(iter(burst_ids))
    for detail in details.values():
        assert len(detail["frames"]) == 3
        assert {frame["assetId"] for frame in detail["frames"]} == set(asset_ids)

    # --- Best-shot selection is reversible and idempotent. ---
    original_rep = details[asset_ids[0]]["representativeAssetId"]
    other = next(asset_id for asset_id in asset_ids if asset_id != original_rep)
    operation = uuid4()
    mutate(
        backend.service,
        operation,
        Mutation(
            action="burst.setRepresentative",
            entity_id=UUID(burst_id),
            changes={"representativeAssetId": other},
        ),
    )
    assert backend.service.catalog.burst_detail(asset_ids[0])["representativeAssetId"] == other
    # Replaying the same operation is a no-op that keeps the new representative.
    mutate(
        backend.service,
        operation,
        Mutation(
            action="burst.setRepresentative",
            entity_id=UUID(burst_id),
            changes={"representativeAssetId": other},
        ),
    )
    assert backend.service.catalog.burst_detail(asset_ids[0])["representativeAssetId"] == other
    # A new operation can revert the best shot.
    mutate(
        backend.service,
        uuid4(),
        Mutation(
            action="burst.setRepresentative",
            entity_id=UUID(burst_id),
            changes={"representativeAssetId": original_rep},
        ),
    )
    assert (
        backend.service.catalog.burst_detail(asset_ids[0])["representativeAssetId"]
        == original_rep
    )


def test_face_stage_failure_reports_stage_counters(backend, monkeypatch):
    asset_ids = import_burst(backend)
    drive_previews(backend, set(asset_ids))

    calls = [0]
    worker = make_worker(backend, monkeypatch, calls, faces=FailingFaceStub())
    backend.service.settings = backend.service.settings.model_copy(
        update={"ai_semantic_reuse_mode": "off"}
    )

    result = worker.run_once()
    assert result is not None
    assert result["status"] == "failed"
    assert result["jobType"] == "analysis"
    assert result["stage"] == "face"
    assert "simulated face detector failure" in result["error"]
    # The failure is attributed to the face stage and nothing was computed.
    assert result["counters"]["stageFailures"] == {
        "setup": 0,
        "fingerprint": 0,
        "face": 1,
        "semantic": 0,
    }
    assert result["counters"]["semanticComputed"] == 0
    assert result["counters"]["semanticReused"] == 0
    assert result["counters"]["rejectionsByGate"] == {}
    assert calls[0] == 0  # the VLM was never invoked

    # The failed job is not retried: the next claim is a different asset, and
    # the pipeline can still complete its remaining frames.
    next_result = worker.run_once()
    assert next_result["assetId"] != result["assetId"]
    assert next_result["status"] == "ready"
