"""Burst-aware semantic-reuse policy (`burst-reuse-v1`).

This module is pure policy: it decides whether a target asset may inherit the
semantic analysis of a source asset, and which of several qualifying sources to
choose. It performs no database, worker, or model work.

The `burst-reuse-v1` policy is deliberately conservative. Every gate must pass for
reuse to be accepted; a single failure rejects the candidate and the target is
analyzed by the VLM instead. False negatives are preferable to false-positive reuse
(two different scenes described by the same text). The numerical thresholds are fixed
constants of this policy version and are intentionally not configurable; changing any
of them requires incrementing `REUSE_POLICY_VERSION` (never the fingerprint algorithm
version).
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from PIL import Image, ImageOps
from pydantic.alias_generators import to_camel

from photo_server.analysis import SemanticAnalysis
from photo_server.config import Settings
from photo_server.fingerprints import (
    BURST_HASH_VERSION,
    DHASH_MAX_DISTANCE,
    PHASH_MAX_DISTANCE,
    Fingerprint,
    hamming_distance,
)

REUSE_POLICY_VERSION = "burst-reuse-v1"

# --- Fixed constants of the burst-reuse-v1 policy (not configurable). ---
# The pHash/dHash Hamming limits reuse the frozen burst-hash-v1 constants.
CAPTURE_WINDOW_MS = 3000
ASPECT_RATIO_TOLERANCE = 0.02
DIMENSION_TOLERANCE = 0.05
PIXEL_SIMILARITY_THRESHOLD = 0.95
_PIXEL_SIZE = 16  # 16 x 16 = the fixed 256-pixel luminance render for gate 8.

UNKNOWN_DIGEST = "unknown"

# Stable rejection-reason identifiers. These are the keys for the "candidates
# rejected by each gate" operational counter; keep them stable within the policy.
REASON_SAME_ASSET = "same_asset"
REASON_FINGERPRINT_VERSION = "fingerprint_version_mismatch"
REASON_UNKNOWN_DIGEST = "unknown_model_digest"
REASON_PIPELINE = "pipeline_mismatch"
REASON_MODEL_NAME = "model_name_mismatch"
REASON_MODEL_DIGEST = "model_digest_mismatch"
REASON_ASPECT_RATIO = "aspect_ratio_mismatch"
REASON_MISSING_TIME = "missing_capture_time"
REASON_CAPTURE_TIME = "capture_time_mismatch"
REASON_CAMERA = "camera_mismatch"
REASON_DOCUMENT = "document_or_screenshot"
REASON_VISIBLE_TEXT = "visible_text"
REASON_PHASH = "phash_distance"
REASON_DHASH = "dhash_distance"
REASON_PIXEL = "pixel_similarity"


@dataclass(frozen=True)
class ReuseAsset:
    """One asset's reuse-relevant facts.

    ``fingerprint`` is the asset's burst-hash-v1 fingerprint. ``capture_time`` and
    ``camera_identity`` are the asset's capture metadata (``None`` when unknown).
    ``pipeline_version``/``model_name``/``model_digest`` identify the analysis the
    asset was produced with (for a target, the requested analysis). ``preview`` is
    the metadata-free preview JPEG used by the final pixel comparison.
    """

    asset_id: str
    fingerprint: Fingerprint
    capture_time: datetime | None
    camera_identity: str | None
    pipeline_version: str
    model_name: str
    model_digest: str
    preview: bytes


@dataclass(frozen=True)
class ReuseSource(ReuseAsset):
    """A candidate source: a ReuseAsset plus its stored semantic analysis."""

    semantic: SemanticAnalysis


@dataclass(frozen=True)
class ReuseDecision:
    """The outcome of evaluating one source against a target.

    ``reason`` is ``None`` when accepted, otherwise a stable rejection-reason
    identifier (one of the REASON_* constants). ``similarity`` is the provenance
    payload recorded for accepted reuse and ``None`` otherwise.
    """

    accepted: bool
    reason: str | None
    similarity: dict | None


def _accept(similarity: dict) -> ReuseDecision:
    return ReuseDecision(accepted=True, reason=None, similarity=similarity)


def _reject(reason: str) -> ReuseDecision:
    return ReuseDecision(accepted=False, reason=reason, similarity=None)


def _relative_diff(a: float, b: float) -> float:
    """Relative difference of two magnitudes, normalized by the larger one."""
    denominator = max(abs(a), abs(b))
    if denominator == 0:
        return 0.0
    return abs(a - b) / denominator


def _capture_distance_ms(a: datetime | None, b: datetime | None) -> int:
    if a is None or b is None:
        return 10**18
    return int(abs((a - b).total_seconds() * 1000))


def _luminance_render(jpeg: bytes, size: int = _PIXEL_SIZE) -> list[float]:
    """Render a preview to a fixed-size grayscale (luminance) pixel list."""
    with Image.open(io.BytesIO(jpeg)) as source:
        image = ImageOps.exif_transpose(source).convert("L")
        image = image.resize((size, size), Image.Resampling.LANCZOS)
        return [float(p) for p in image.get_flattened_data()]


def pixel_similarity(target_preview: bytes, source_preview: bytes) -> float:
    """Normalized similarity of two previews over a 256-pixel luminance render.

    1 minus the mean absolute per-pixel difference (normalized to 0..1 by 255).
    Identical renders give 1.0; the value is the ``pixelSimilarity`` recorded in
    the reuse provenance payload.
    """
    target = _luminance_render(target_preview)
    source = _luminance_render(source_preview)
    mean_abs_diff = sum(abs(x - y) for x, y in zip(target, source, strict=True)) / len(target)
    return 1.0 - mean_abs_diff / 255.0


def _similarity_payload(
    phash_distance: int,
    dhash_distance: int,
    capture_delta_ms: int,
    similarity_value: float,
) -> dict:
    return {
        "policyVersion": REUSE_POLICY_VERSION,
        "fingerprintVersion": BURST_HASH_VERSION,
        "phashDistance": phash_distance,
        "dhashDistance": dhash_distance,
        "captureDeltaMs": capture_delta_ms,
        "pixelSimilarity": round(similarity_value, 4),
    }


def evaluate_reuse(target: ReuseAsset, source: ReuseSource, settings: Settings) -> ReuseDecision:
    """Evaluate whether ``target`` may inherit ``source``'s semantic analysis.

    Implements the eight burst-reuse-v1 gates in order (cheap checks first, the
    expensive pixel comparison last), plus the text-content exclusions. Every gate
    must pass; the first failure rejects the candidate. ``settings`` is accepted for
    API consistency with the worker; the thresholds are fixed policy constants.
    """
    # 1. It is a different asset.
    if target.asset_id == source.asset_id:
        return _reject(REASON_SAME_ASSET)

    # 2. Its fingerprint algorithm version matches.
    if target.fingerprint.algorithm_version != source.fingerprint.algorithm_version:
        return _reject(REASON_FINGERPRINT_VERSION)

    # 3. Its semantic pipeline, model name, and model digest match the requested
    #    analysis. An unknown digest disables reuse (no fallback to name-only).
    if target.model_digest == UNKNOWN_DIGEST or source.model_digest == UNKNOWN_DIGEST:
        return _reject(REASON_UNKNOWN_DIGEST)
    if target.pipeline_version != source.pipeline_version:
        return _reject(REASON_PIPELINE)
    if target.model_name != source.model_name:
        return _reject(REASON_MODEL_NAME)
    if target.model_digest != source.model_digest:
        return _reject(REASON_MODEL_DIGEST)

    # 4. Its aspect ratio is effectively identical and its dimensions compatible.
    tf, sf = target.fingerprint, source.fingerprint
    if (
        _relative_diff(tf.width / tf.height, sf.width / sf.height) > ASPECT_RATIO_TOLERANCE
        or _relative_diff(tf.width, sf.width) > DIMENSION_TOLERANCE
        or _relative_diff(tf.height, sf.height) > DIMENSION_TOLERANCE
    ):
        return _reject(REASON_ASPECT_RATIO)

    # 5. Its capture time is within three seconds of the target when both are
    #    known. A missing time narrows optimization (rejects) rather than
    #    weakening the gate.
    if target.capture_time is None or source.capture_time is None:
        return _reject(REASON_MISSING_TIME)
    capture_delta_ms = _capture_distance_ms(target.capture_time, source.capture_time)
    if capture_delta_ms > CAPTURE_WINDOW_MS:
        return _reject(REASON_CAPTURE_TIME)

    # 6. Its camera identity matches when both assets provide one.
    if (
        target.camera_identity is not None
        and source.camera_identity is not None
        and target.camera_identity.casefold() != source.camera_identity.casefold()
    ):
        return _reject(REASON_CAMERA)

    # Text-content exclusions: never reuse document/screenshot analyses or results
    # containing visible text (small visual differences may carry different text).
    if "document" in source.semantic.photo_types or "screenshot" in source.semantic.photo_types:
        return _reject(REASON_DOCUMENT)
    if source.semantic.visible_text:
        return _reject(REASON_VISIBLE_TEXT)

    # 7. pHash Hamming distance at most 4 and dHash Hamming distance at most 6.
    phash_distance = hamming_distance(tf.phash, sf.phash)
    if phash_distance > PHASH_MAX_DISTANCE:
        return _reject(REASON_PHASH)
    dhash_distance = hamming_distance(tf.dhash, sf.dhash)
    if dhash_distance > DHASH_MAX_DISTANCE:
        return _reject(REASON_DHASH)

    # 8. A final inexpensive comparison of normalized preview pixels passes the
    #    similarity threshold (collision guard).
    similarity_value = pixel_similarity(target.preview, source.preview)
    if similarity_value < PIXEL_SIMILARITY_THRESHOLD:
        return _reject(REASON_PIXEL)

    return _accept(
        _similarity_payload(phash_distance, dhash_distance, capture_delta_ms, similarity_value)
    )


def extract_semantic(source_result: dict) -> SemanticAnalysis:
    """Copy only the ``SemanticAnalysis`` fields out of a source's public result.

    A combined public result (e.g. ``{**semantic.document(), "faceCount": ...}``)
    carries image-local fields such as ``faceCount`` and face rows that must NOT
    leak into the target. This filters the result down to exactly the fields
    represented by ``SemanticAnalysis`` (accepting either the field name or its
    camelCase alias) and validates through the model, so image-local fields are
    dropped.

    Raises ``pydantic.ValidationError`` if the inherited semantic content is missing
    required fields or is otherwise invalid; the caller rejects that candidate and
    invokes the VLM.
    """
    valid_keys: set[str] = set()
    for field_name in SemanticAnalysis.model_fields:
        valid_keys.add(field_name)
        valid_keys.add(to_camel(field_name))
    filtered = {key: value for key, value in source_result.items() if key in valid_keys}
    return SemanticAnalysis.model_validate(filtered)


def choose_reusable_source_detailed(
    target: ReuseAsset, sources: Sequence[ReuseSource], settings: Settings
) -> tuple[tuple[ReuseSource, ReuseDecision] | None, dict[str, int]]:
    """Pick the best qualifying source and count per-gate rejections.

    Returns ``(chosen, rejections)`` where ``chosen`` is ``(source, decision)``
    for the best accepted source (or ``None`` when no source qualifies) and
    ``rejections`` maps each stable rejection-reason identifier to the number of
    sources rejected at that gate. Each rejected source is counted exactly once,
    at the first gate that failed (matching ``evaluate_reuse``'s short-circuit
    order); this is the "candidates rejected by each gate" operational counter.
    """
    best: tuple[tuple[int, int, int, str], ReuseSource, ReuseDecision] | None = None
    rejections: dict[str, int] = {}
    for source in sources:
        decision = evaluate_reuse(target, source, settings)
        if not decision.accepted:
            reason = decision.reason or "unknown"
            rejections[reason] = rejections.get(reason, 0) + 1
            continue
        key = (
            hamming_distance(target.fingerprint.phash, source.fingerprint.phash),
            hamming_distance(target.fingerprint.dhash, source.fingerprint.dhash),
            _capture_distance_ms(target.capture_time, source.capture_time),
            source.asset_id,
        )
        if best is None or key < best[0]:
            best = (key, source, decision)
    if best is None:
        return None, rejections
    return (best[1], best[2]), rejections


def choose_reusable_source(
    target: ReuseAsset, sources: Sequence[ReuseSource], settings: Settings
) -> tuple[ReuseSource, ReuseDecision] | None:
    """Pick the best qualifying source for ``target``, or ``None`` if none qualify.

    Evaluates every source with ``evaluate_reuse`` and, among the accepted ones,
    returns the candidate with the lowest (pHash distance, dHash distance,
    capture-time distance, asset ID) tuple. Deterministic tie-breaking makes
    retries reproducible.
    """
    chosen, _ = choose_reusable_source_detailed(target, sources, settings)
    return chosen


__all__ = [
    "ASPECT_RATIO_TOLERANCE",
    "CAPTURE_WINDOW_MS",
    "DIMENSION_TOLERANCE",
    "PIXEL_SIMILARITY_THRESHOLD",
    "REASON_CAPTURE_TIME",
    "REASON_CAMERA",
    "REASON_DHASH",
    "REASON_DOCUMENT",
    "REASON_FINGERPRINT_VERSION",
    "REASON_MISSING_TIME",
    "REASON_MODEL_DIGEST",
    "REASON_MODEL_NAME",
    "REASON_PIPELINE",
    "REASON_PHASH",
    "REASON_PIXEL",
    "REASON_SAME_ASSET",
    "REASON_VISIBLE_TEXT",
    "REUSE_POLICY_VERSION",
    "ReuseAsset",
    "ReuseDecision",
    "ReuseSource",
    "choose_reusable_source",
    "choose_reusable_source_detailed",
    "evaluate_reuse",
    "extract_semantic",
    "pixel_similarity",
]
