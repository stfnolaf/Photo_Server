"""Unit tests for the burst-reuse-v1 semantic-reuse policy (Phase 3).

Covers the acceptance criteria from ``docs/burst-semantic-analysis-reuse.md``:
the time, camera, aspect-ratio, text-content, model, and pipeline gates, plus
extraction of only ``SemanticAnalysis`` fields from a source result.

All tests are pure: no database, S3, or Ollama. Previews are PIL-generated.
"""

import io
from datetime import UTC, datetime, timedelta

import pytest
from PIL import Image, ImageDraw
from pydantic import ValidationError

from photo_server.analysis import DetectedObject, SemanticAnalysis
from photo_server.config import Settings
from photo_server.fingerprints import BURST_HASH_VERSION, Fingerprint
from photo_server.reuse import (
    REASON_ASPECT_RATIO,
    REASON_CAMERA,
    REASON_CAPTURE_TIME,
    REASON_DHASH,
    REASON_DOCUMENT,
    REASON_FINGERPRINT_VERSION,
    REASON_MISSING_TIME,
    REASON_MODEL_DIGEST,
    REASON_MODEL_NAME,
    REASON_PHASH,
    REASON_PIPELINE,
    REASON_PIXEL,
    REASON_SAME_ASSET,
    REASON_UNKNOWN_DIGEST,
    REASON_VISIBLE_TEXT,
    REUSE_POLICY_VERSION,
    ReuseAsset,
    ReuseSource,
    choose_reusable_source,
    evaluate_reuse,
    extract_semantic,
    pixel_similarity,
)

PIPELINE = "photo-ai-v1"
MODEL_NAME = "qwen3-vl:8b-instruct-q4_K_M"
DIGEST = "abc123def456"
T0 = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)


# --- PIL-generated previews (no external assets). ---


def _gradient() -> Image.Image:
    width, height = 640, 480
    img = Image.new("L", (width, height))
    data: list[int] = []
    for _ in range(height):
        for x in range(width):
            data.append(40 + int(180 * x / (width - 1)))
    img.putdata(data)
    return img


def base_image() -> Image.Image:
    img = _gradient()
    ImageDraw.Draw(img).rectangle([120, 120, 300, 380], fill=250)
    return img


def unrelated_image() -> Image.Image:
    width, height = 640, 480
    img = Image.new("L", (width, height))
    data: list[int] = []
    for _ in range(height):
        for x in range(width):
            data.append(255 if (x // 40) % 2 == 0 else 0)
    img.putdata(data)
    return img


def _to_jpeg(img: Image.Image, quality: int = 92) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


BASE_PREVIEW = _to_jpeg(base_image())
UNRELATED_PREVIEW = _to_jpeg(unrelated_image())


# --- Factories. ---


def make_semantic(
    photo_types=("landscape",), visible_text=(), summary="A quiet outdoor scene."
) -> SemanticAnalysis:
    return SemanticAnalysis(
        summary=summary,
        photo_types=list(photo_types),
        scene="outdoors",
        setting="outdoor",
        visible_text=list(visible_text),
    )


def make_asset(
    asset_id: str = "target",
    phash: str = "0000000000000000",
    dhash: str = "0000000000000000",
    width: int = 640,
    height: int = 480,
    capture_time: datetime | None = None,
    camera_identity: str | None = None,
    pipeline_version: str = PIPELINE,
    model_name: str = MODEL_NAME,
    model_digest: str = DIGEST,
    preview: bytes = BASE_PREVIEW,
    algo_version: str = BURST_HASH_VERSION,
) -> ReuseAsset:
    return ReuseAsset(
        asset_id=asset_id,
        fingerprint=Fingerprint(
            phash=phash, dhash=dhash, width=width, height=height, algorithm_version=algo_version
        ),
        capture_time=capture_time,
        camera_identity=camera_identity,
        pipeline_version=pipeline_version,
        model_name=model_name,
        model_digest=model_digest,
        preview=preview,
    )


def make_source(semantic: SemanticAnalysis | None = None, **kwargs) -> ReuseSource:
    asset = make_asset(**kwargs)
    return ReuseSource(
        asset_id=asset.asset_id,
        fingerprint=asset.fingerprint,
        capture_time=asset.capture_time,
        camera_identity=asset.camera_identity,
        pipeline_version=asset.pipeline_version,
        model_name=asset.model_name,
        model_digest=asset.model_digest,
        preview=asset.preview,
        semantic=semantic if semantic is not None else make_semantic(),
    )


def _target() -> ReuseAsset:
    return make_asset(asset_id="target", capture_time=T0)


def _source(**overrides) -> ReuseSource:
    defaults = dict(
        asset_id="source",
        phash="000000000000000f",  # Hamming distance 4 (at the policy limit)
        dhash="000000000000003f",  # Hamming distance 6 (at the policy limit)
        capture_time=T0 + timedelta(milliseconds=100),
    )
    defaults.update(overrides)
    return make_source(**defaults)


@pytest.fixture(scope="module")
def settings() -> Settings:
    return Settings(s3_endpoint="http://s3:9000", postgres_password="secret")


# --- Acceptance: a fully qualifying source is reused. ---


def test_accepted_reuse(settings):
    decision = evaluate_reuse(_target(), _source(), settings)
    assert decision.accepted is True
    assert decision.reason is None
    assert decision.similarity is not None


def test_similarity_payload_shape(settings):
    decision = evaluate_reuse(_target(), _source(capture_time=T0 + timedelta(milliseconds=180)), settings)
    assert decision.accepted is True
    payload = decision.similarity
    assert set(payload) == {
        "policyVersion",
        "fingerprintVersion",
        "phashDistance",
        "dhashDistance",
        "captureDeltaMs",
        "pixelSimilarity",
    }
    assert payload["policyVersion"] == REUSE_POLICY_VERSION == "burst-reuse-v1"
    assert payload["fingerprintVersion"] == BURST_HASH_VERSION == "burst-hash-v1"
    assert payload["phashDistance"] == 4
    assert payload["dhashDistance"] == 6
    assert payload["captureDeltaMs"] == 180
    assert payload["pixelSimilarity"] == 1.0


# --- Gate 1: different asset. ---


def test_same_asset_rejected(settings):
    decision = evaluate_reuse(_target(), _source(asset_id="target"), settings)
    assert decision.accepted is False
    assert decision.reason == REASON_SAME_ASSET


# --- Gate 2: fingerprint algorithm version. ---


def test_fingerprint_version_mismatch(settings):
    decision = evaluate_reuse(_target(), _source(algo_version="burst-hash-v0"), settings)
    assert decision.reason == REASON_FINGERPRINT_VERSION


# --- Gate 3: pipeline, model name, model digest. ---


def test_pipeline_mismatch(settings):
    decision = evaluate_reuse(_target(), _source(pipeline_version="photo-ai-v0"), settings)
    assert decision.reason == REASON_PIPELINE


def test_model_name_mismatch(settings):
    decision = evaluate_reuse(_target(), _source(model_name="other-model"), settings)
    assert decision.reason == REASON_MODEL_NAME


def test_model_digest_mismatch(settings):
    decision = evaluate_reuse(_target(), _source(model_digest="differentdigest"), settings)
    assert decision.reason == REASON_MODEL_DIGEST


def test_unknown_source_digest_disables_reuse(settings):
    decision = evaluate_reuse(_target(), _source(model_digest="unknown"), settings)
    assert decision.accepted is False
    assert decision.reason == REASON_UNKNOWN_DIGEST


def test_unknown_target_digest_disables_reuse(settings):
    target = make_asset(asset_id="target", capture_time=T0, model_digest="unknown")
    decision = evaluate_reuse(target, _source(), settings)
    assert decision.accepted is False
    assert decision.reason == REASON_UNKNOWN_DIGEST


# --- Gate 4: aspect ratio and dimension compatibility. ---


def test_aspect_ratio_mismatch(settings):
    decision = evaluate_reuse(_target(), _source(width=640, height=400), settings)
    assert decision.accepted is False
    assert decision.reason == REASON_ASPECT_RATIO


def test_dimension_mismatch_same_ratio(settings):
    decision = evaluate_reuse(_target(), _source(width=800, height=600), settings)
    assert decision.accepted is False


# --- Gate 5: capture time within three seconds. ---


def test_capture_time_exceeds_window(settings):
    decision = evaluate_reuse(_target(), _source(capture_time=T0 + timedelta(milliseconds=3001)), settings)
    assert decision.reason == REASON_CAPTURE_TIME


def test_missing_capture_time_rejected(settings):
    decision = evaluate_reuse(_target(), _source(capture_time=None), settings)
    assert decision.reason == REASON_MISSING_TIME


def test_missing_target_capture_time_rejected(settings):
    target = make_asset(asset_id="target", capture_time=None)
    decision = evaluate_reuse(target, _source(), settings)
    assert decision.reason == REASON_MISSING_TIME


# --- Gate 6: camera identity. ---


def test_camera_identity_match(settings):
    target = make_asset(asset_id="target", capture_time=T0, camera_identity="Canon EOS R5")
    decision = evaluate_reuse(target, _source(camera_identity="Canon EOS R5"), settings)
    assert decision.accepted is True


def test_camera_identity_mismatch(settings):
    target = make_asset(asset_id="target", capture_time=T0, camera_identity="Canon EOS R5")
    decision = evaluate_reuse(target, _source(camera_identity="Nikon Z8"), settings)
    assert decision.reason == REASON_CAMERA


def test_camera_gate_skipped_when_missing(settings):
    target = make_asset(asset_id="target", capture_time=T0, camera_identity="Canon EOS R5")
    decision = evaluate_reuse(target, _source(), settings)  # source camera is None
    assert decision.accepted is True


# --- Text-content exclusions. ---


def test_document_type_rejected(settings):
    decision = evaluate_reuse(_target(), _source(semantic=make_semantic(photo_types=["document"])), settings)
    assert decision.reason == REASON_DOCUMENT


def test_screenshot_type_rejected(settings):
    decision = evaluate_reuse(_target(), _source(semantic=make_semantic(photo_types=["screenshot"])), settings)
    assert decision.reason == REASON_DOCUMENT


def test_visible_text_rejected(settings):
    decision = evaluate_reuse(_target(), _source(semantic=make_semantic(visible_text=["HELLO"])), settings)
    assert decision.reason == REASON_VISIBLE_TEXT


# --- Gate 7: pHash / dHash Hamming distances. ---


def test_phash_distance_exceeded(settings):
    decision = evaluate_reuse(_target(), _source(phash="000000000000001f"), settings)  # distance 5
    assert decision.reason == REASON_PHASH


def test_dhash_distance_exceeded(settings):
    decision = evaluate_reuse(_target(), _source(dhash="000000000000007f"), settings)  # distance 7
    assert decision.reason == REASON_DHASH


# --- Gate 8: normalized preview pixel similarity. ---


def test_pixel_similarity_fail(settings):
    decision = evaluate_reuse(_target(), _source(preview=UNRELATED_PREVIEW), settings)
    assert decision.reason == REASON_PIXEL


def test_pixel_similarity_identical():
    assert pixel_similarity(BASE_PREVIEW, BASE_PREVIEW) == 1.0


def test_pixel_similarity_range():
    similarity = pixel_similarity(BASE_PREVIEW, UNRELATED_PREVIEW)
    assert 0.0 <= similarity <= 1.0
    assert similarity < 0.95


# --- extract_semantic copies only SemanticAnalysis fields. ---


def test_extract_semantic_copies_only_semantic_fields():
    semantic = SemanticAnalysis(
        summary="A quiet outdoor scene.",
        photo_types=["landscape"],
        scene="outdoors",
        setting="outdoor",
        objects=[DetectedObject(name="tree", count=3)],
        activities=["hiking"],
        tags=["nature"],
        visible_text=[],
    )
    public_result = {**semantic.document(), "faceCount": 2, "faces": [{"id": 1}]}
    extracted = extract_semantic(public_result)
    assert extracted == semantic
    assert "faceCount" not in extracted.document()
    assert "faces" not in extracted.document()


def test_extract_semantic_accepts_snake_case():
    data = {
        "summary": "A quiet outdoor scene.",
        "photo_types": ["landscape"],
        "scene": "outdoors",
        "setting": "outdoor",
    }
    extracted = extract_semantic(data)
    assert extracted.photo_types == ["landscape"]
    assert extracted.setting == "outdoor"


def test_extract_semantic_invalid_rejects():
    with pytest.raises(ValidationError):
        extract_semantic({"faceCount": 2})


# --- Candidate selection: lowest (phash, dhash, capture, asset_id) tuple. ---


def test_choose_reusable_source_picks_lowest_tuple(settings):
    target = make_asset(asset_id="target", capture_time=T0)
    s1 = make_source(
        asset_id="s1",
        phash="000000000000000f",  # distance 4
        dhash="000000000000003f",  # distance 6
        capture_time=T0 + timedelta(milliseconds=100),
    )
    s2 = make_source(
        asset_id="s2",
        phash="0000000000000003",  # distance 2
        dhash="0000000000000007",  # distance 3
        capture_time=T0 + timedelta(milliseconds=500),
    )
    result = choose_reusable_source(target, [s1, s2], settings)
    assert result is not None
    source, decision = result
    assert source.asset_id == "s2"  # (2, 3, 500) < (4, 6, 100)
    assert decision.accepted is True


def test_choose_reusable_source_tie_break_asset_id(settings):
    target = make_asset(asset_id="target", capture_time=T0)
    s_b = make_source(
        asset_id="s-b",
        phash="0000000000000003",
        dhash="0000000000000007",
        capture_time=T0 + timedelta(milliseconds=500),
    )
    s_a = make_source(
        asset_id="s-a",
        phash="0000000000000003",
        dhash="0000000000000007",
        capture_time=T0 + timedelta(milliseconds=500),
    )
    result = choose_reusable_source(target, [s_b, s_a], settings)
    assert result is not None
    source, _ = result
    assert source.asset_id == "s-a"  # tie on (2, 3, 500); "s-a" < "s-b"


def test_choose_reusable_source_none_when_all_rejected(settings):
    target = make_asset(asset_id="target", capture_time=T0)
    s1 = make_source(asset_id="s1", model_digest="unknown")
    s2 = make_source(asset_id="s2", phash="000000000000001f")  # distance 5
    assert choose_reusable_source(target, [s1, s2], settings) is None


# --- Settings: PHOTO_AI_SEMANTIC_REUSE_MODE. ---


def test_settings_default_reuse_mode():
    s = Settings(s3_endpoint="http://s3:9000", postgres_password="secret")
    assert s.ai_semantic_reuse_mode == "observe"


def test_settings_reuse_mode_validation():
    with pytest.raises(ValidationError):
        Settings(
            s3_endpoint="http://s3:9000",
            postgres_password="secret",
            ai_semantic_reuse_mode="bogus",
        )
