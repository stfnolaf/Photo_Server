"""Pure matching policy for canonical burst clustering."""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from photo_server.fingerprints import Fingerprint, chroma_histogram_distance, hamming_distance
from photo_server.models import Manifest

BURST_CLUSTER_POLICY_VERSION = "burst-cluster-v2"
CAPTURE_WINDOW = timedelta(seconds=3)
UPLOAD_NEAR_WINDOW = timedelta(minutes=10)
UPLOAD_FAR_WINDOW = timedelta(days=7)
_MISSING_TIME_MS = 10**18
_MIN_EVIDENCE_SCORE = 4
# The normal thresholds are intentionally conservative. Filename sequence can
# identify a likely camera sequence, but it must not turn visually unrelated
# frames into one burst. A modest sequence relaxation handles zoom and pose
# changes; the wider exception is reserved for an immediately consecutive
# frame captured almost at once.
_SEQUENCE_PHASH_MAX = 22
_SEQUENCE_DHASH_MAX = 16
_CLOSE_SEQUENCE_PHASH_MAX = 30
_CLOSE_SEQUENCE_DHASH_MAX = 25
_CLOSE_SEQUENCE_WINDOW = timedelta(seconds=5)
_SEQUENCE_EXTENSION_WINDOW = timedelta(seconds=120)
_FILENAME_SEQUENCE = re.compile(r"^(?P<prefix>.*?)(?P<sequence>\d{2,})(?P<suffix>\.[^.]+)?$", re.IGNORECASE)
_SHUTTER_COUNT_KEYS = ("ShutterCount", "Shutter Count", "MakerNotes:ShutterCount")


@dataclass(frozen=True)
class ClusterCandidate:
    asset_id: str
    cluster_id: str
    phash: str
    dhash: str
    capture_time: datetime | None
    imported_at: datetime | None
    camera_identity: str | None
    original_filename: str
    shutter_count: int | None
    chroma_histogram: str | None = None


@dataclass(frozen=True)
class BurstDecision:
    """Explainable result of the display-clustering policy."""

    accepted: bool
    score: int
    evidence: tuple[str, ...]
    rejection: str | None = None


def _camera_identity(metadata: dict) -> str | None:
    """Stable camera identifier: ``Make Model``, or None when unknown."""
    parts = [metadata.get("Make"), metadata.get("Model")]
    parts = [part for part in parts if part]
    return " ".join(parts) if parts else None


def _burst_time(value: str | None) -> datetime | None:
    """Parse event times as instants when offsets are available.

    ``camera_time`` is intentionally local-calendar based for browsing. Burst
    detection instead uses the actual instant when EXIF offsets are present, so
    cameras with different timezone settings do not look synchronized merely
    because their displayed clock values match.
    """
    try:
        parsed = datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None
    if parsed is not None and parsed.tzinfo is not None:
        return parsed.astimezone(UTC)
    return parsed


def _capture_distance_ms(a: datetime | None, b: datetime | None) -> int:
    if a is None or b is None:
        return _MISSING_TIME_MS
    try:
        return abs(int((a - b).total_seconds() * 1000))
    except TypeError:
        # An offset-bearing timestamp cannot be safely compared with an
        # offset-less timestamp; leave ordering to the other deterministic keys.
        return _MISSING_TIME_MS


def _filename_evidence(target: str, candidate: str) -> tuple[int, str | None]:
    """Return evidence for camera-style sequential filenames.

    Repeated generic names such as IMG_0001 are not sufficient on their own;
    sequence evidence is only a supplement to the temporal/camera signals.
    """
    target_match = _FILENAME_SEQUENCE.match(target)
    candidate_match = _FILENAME_SEQUENCE.match(candidate)
    if not target_match or not candidate_match:
        return 0, None
    if target_match.group("suffix").casefold() != candidate_match.group("suffix").casefold():
        return 0, None
    if target_match.group("prefix").casefold() != candidate_match.group("prefix").casefold():
        return 0, None
    delta = abs(int(target_match.group("sequence")) - int(candidate_match.group("sequence")))
    if delta == 0:
        return 0, None
    if delta <= 6:
        return 2, f"filename_sequence_delta_{delta}"
    return 0, None


def _shutter_count(metadata: dict) -> int | None:
    """Read a maker-specific shutter count when ExifTool exposed one."""
    for key in _SHUTTER_COUNT_KEYS:
        value = metadata.get(key)
        if value is None:
            continue
        match = re.search(r"\d+", str(value).replace(",", ""))
        if match:
            return int(match.group())
    return None


def _evaluate_candidate(
    target: Manifest,
    fingerprint: Fingerprint,
    candidate: ClusterCandidate,
    phash_max: int,
    dhash_max: int,
    capture_window: timedelta = CAPTURE_WINDOW,
    chroma_max: float = 0.15,
) -> BurstDecision:
    """Apply burst-v2 gates and return the evidence used for the decision."""
    target_camera = _camera_identity(target.metadata)
    if (
        target_camera is not None
        and candidate.camera_identity is not None
        and target_camera.casefold() != candidate.camera_identity.casefold()
    ):
        return BurstDecision(False, 0, (), "camera_conflict")

    filename_score, filename_reason = _filename_evidence(
        target.primary.original_filename, candidate.original_filename
    )
    target_shutter = _shutter_count(target.metadata)
    shutter_delta = (
        abs(target_shutter - candidate.shutter_count)
        if target_shutter is not None and candidate.shutter_count is not None
        else None
    )
    sequence_delta = None
    if filename_reason:
        sequence_delta = int(filename_reason.rsplit("_", 1)[-1])
    target_capture = _burst_time(target.capture_time)
    capture_delta = None
    if target_capture is not None and candidate.capture_time is not None:
        try:
            capture_delta = abs(target_capture - candidate.capture_time)
        except TypeError:
            capture_delta = None

    shutter_confirms = (
        target_shutter is not None
        and candidate.shutter_count is not None
        and shutter_delta is not None
        and shutter_delta <= 3
    )
    exact_sequence = sequence_delta == 1
    within_capture_window = capture_delta is not None and capture_delta <= capture_window
    close_exact_sequence = (
        exact_sequence
        and capture_delta is not None
        and capture_delta <= min(capture_window, _CLOSE_SEQUENCE_WINDOW)
    )
    # Existing catalogs may not have persisted maker-specific shutter counts,
    # so filename sequence is useful on its own when the capture times agree.
    # Beyond the ordinary window, only an exact next filename gets a bounded
    # extension; a broad filename match must not override elapsed time.
    sequence_relaxation = (
        sequence_delta is not None
        and sequence_delta <= 6
        and (
            within_capture_window
            or shutter_confirms
            or (
                exact_sequence
                and capture_delta is not None
                and capture_delta <= _SEQUENCE_EXTENSION_WINDOW
            )
        )
    )
    if close_exact_sequence:
        effective_phash_max = max(phash_max, _CLOSE_SEQUENCE_PHASH_MAX)
        effective_dhash_max = max(dhash_max, _CLOSE_SEQUENCE_DHASH_MAX)
    elif sequence_relaxation:
        effective_phash_max = max(phash_max, _SEQUENCE_PHASH_MAX)
        effective_dhash_max = max(dhash_max, _SEQUENCE_DHASH_MAX)
    else:
        effective_phash_max = phash_max
        effective_dhash_max = dhash_max
    phash_distance = hamming_distance(fingerprint.phash, candidate.phash)
    dhash_distance = hamming_distance(fingerprint.dhash, candidate.dhash)
    if phash_distance > effective_phash_max:
        return BurstDecision(False, 0, (), "phash_distance")
    if dhash_distance > effective_dhash_max:
        return BurstDecision(False, 0, (), "dhash_distance")
    if phash_distance > phash_max or dhash_distance > dhash_max:
        chroma_distance = chroma_histogram_distance(
            fingerprint.chroma_histogram, candidate.chroma_histogram
        )
        if chroma_distance is not None and chroma_distance > chroma_max:
            return BurstDecision(False, 0, (), "chroma_distance")

    score = 0
    evidence: list[str] = []
    if target_capture is not None and candidate.capture_time is not None:
        if capture_delta is not None:
            if capture_delta > capture_window:
                # A camera can pause between frames, but elapsed time remains
                # negative evidence. Only the bounded exact-sequence or
                # shutter-confirmed path can compensate for this gap.
                if filename_score == 0 or not (sequence_relaxation or shutter_confirms):
                    return BurstDecision(False, 0, (), "capture_time_outside_window")
                evidence.append(f"capture_delta_outside_window_{capture_delta.total_seconds():g}s")
                if sequence_relaxation:
                    score += 1
                    evidence.append("bounded_sequence_time_extension")
            else:
                score += 3
                evidence.append(f"capture_delta_{capture_delta.total_seconds():g}s")

    if target_camera is not None and candidate.camera_identity is not None:
        score += 1
        evidence.append("same_camera")

    score += filename_score
    if filename_reason:
        evidence.append(filename_reason)

    target_imported = _burst_time(target.imported_at)
    if target_imported is not None and candidate.imported_at is not None:
        try:
            upload_delta = abs(target_imported - candidate.imported_at)
        except TypeError:
            upload_delta = None
        if upload_delta is not None:
            if upload_delta <= UPLOAD_NEAR_WINDOW:
                score += 1
                evidence.append("nearby_import_time")
            elif upload_delta > UPLOAD_FAR_WINDOW:
                score -= 1
                evidence.append("distant_import_time")

    if target_shutter is not None and candidate.shutter_count is not None:
        assert shutter_delta is not None
        if shutter_delta <= 3:
            score += 3
            evidence.append(f"shutter_count_delta_{shutter_delta}")
        elif shutter_delta <= 20:
            score += 1
            evidence.append(f"nearby_shutter_count_delta_{shutter_delta}")
        else:
            score -= 1
            evidence.append(f"distant_shutter_count_delta_{shutter_delta}")

    # A perceptual match must have corroboration from at least two independent
    # signals. Capture+camera is the normal path; filename/upload/shutter data
    # can compensate when one of the camera timestamps is unavailable.
    accepted = score >= _MIN_EVIDENCE_SCORE and len(evidence) >= 2
    return BurstDecision(accepted, score, tuple(evidence), None if accepted else "insufficient_context")
