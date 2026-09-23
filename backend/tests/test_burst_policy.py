from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from photo_server.bursts import ClusterCandidate, _evaluate_candidate, _filename_evidence
from photo_server.fingerprints import Fingerprint
from photo_server.models import Blob, Manifest


def _manifest(*, filename="DSC0001.ARW", capture_time=None, imported_at="2026-01-01T00:00:00+00:00", metadata=None):
    asset_id = uuid4()
    blob_id = uuid4()
    blob = Blob(
        blob_id=blob_id,
        role="ORIGINAL_RAW",
        original_filename=filename,
        object_key=f"originals/{asset_id}/{filename}",
        sha256=uuid4().hex + uuid4().hex,
        size_bytes=100,
        mime_type="image/x-sony-arw",
    )
    return Manifest(
        library_id=uuid4(),
        asset_id=asset_id,
        operation_id=uuid4(),
        primary_blob_id=blob_id,
        blobs=[blob],
        imported_at=imported_at,
        capture_time=capture_time,
        metadata=metadata or {},
    )


def _candidate(manifest, *, shutter_count=None):
    return ClusterCandidate(
        asset_id=str(manifest.asset_id),
        cluster_id=str(uuid4()),
        phash="0123456789abcdef",
        dhash="fedcba9876543210",
        capture_time=None,
        imported_at=None,
        camera_identity="Sony ILCE-7M4",
        original_filename=manifest.primary.original_filename,
        shutter_count=shutter_count,
    )


def _calibration_decision(
    target_number: int,
    candidate_number: int,
    seconds: int,
    phash_distance: int,
    dhash_distance: int,
    chroma_change: bool = False,
):
    start = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    camera = {"Make": "Sony", "Model": "ILCE-7M4"}
    target = _manifest(
        filename=f"A{target_number}.ARW",
        capture_time=start.isoformat(),
        metadata=camera,
    )
    candidate_manifest = _manifest(
        filename=f"A{candidate_number}.ARW",
        capture_time=(start + timedelta(seconds=seconds)).isoformat(),
        metadata=camera,
    )
    candidate = replace(
        _candidate(candidate_manifest),
        capture_time=start + timedelta(seconds=seconds),
        phash=f"{(1 << phash_distance) - 1:016x}",
        dhash=f"{(1 << dhash_distance) - 1:016x}",
        chroma_histogram=("00" + "ff" + "00" * 10) if chroma_change else ("ff" + "00" * 11),
    )
    return _evaluate_candidate(
        target,
        Fingerprint(
            phash="0" * 16,
            dhash="0" * 16,
            width=640,
            height=480,
            chroma_histogram="ff" + "00" * 11,
        ),
        candidate,
        17,
        15,
        timedelta(seconds=35),
    )


def test_sequence_filename_is_only_positive_when_prefix_and_extension_match():
    assert _filename_evidence("DSC0001.ARW", "DSC0002.ARW") == (2, "filename_sequence_delta_1")
    assert _filename_evidence("DSC0001.ARW", "IMG0002.ARW") == (0, None)
    assert _filename_evidence("DSC0001.ARW", "DSC0002.JPG") == (0, None)


def test_capture_and_camera_are_sufficient_context():
    target = _manifest(
        capture_time="2026-01-01T12:00:00+00:00",
        metadata={"Make": "Sony", "Model": "ILCE-7M4"},
    )
    candidate = _candidate(target)
    # Candidate times are parsed by the catalog before policy evaluation.
    candidate = replace(
        candidate, capture_time=datetime(2026, 1, 1, 12, 0, 1, tzinfo=timezone.utc)
    )
    decision = _evaluate_candidate(
        target,
        Fingerprint(phash="0123456789abcdef", dhash="fedcba9876543210", width=640, height=480),
        candidate,
        4,
        6,
    )
    assert decision.accepted
    assert "same_camera" in decision.evidence


def test_timezone_offset_is_compared_as_an_instant():
    target = _manifest(
        capture_time="2026-01-01T12:00:00+00:00",
        metadata={"Make": "Sony", "Model": "ILCE-7M4"},
    )
    candidate = _candidate(target)
    candidate = replace(
        candidate, capture_time=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    )
    target = target.model_copy(update={"capture_time": "2026-01-01T12:00:00+02:00"})
    decision = _evaluate_candidate(
        target,
        Fingerprint(phash="0123456789abcdef", dhash="fedcba9876543210", width=640, height=480),
        candidate,
        4,
        6,
    )
    assert not decision.accepted
    assert decision.rejection == "capture_time_outside_window"


def test_filename_and_shutter_count_can_compensate_for_missing_capture_time():
    target = _manifest(
        filename="DSC0101.ARW",
        metadata={"Make": "Sony", "Model": "ILCE-7M4", "ShutterCount": 101},
    )
    candidate_manifest = _manifest(
        filename="DSC0102.ARW",
        metadata={"Make": "Sony", "Model": "ILCE-7M4"},
    )
    candidate = _candidate(candidate_manifest, shutter_count=102)
    decision = _evaluate_candidate(
        target,
        Fingerprint(phash="0123456789abcdef", dhash="fedcba9876543210", width=640, height=480),
        candidate,
        4,
        6,
    )
    assert decision.accepted
    assert "filename_sequence_delta_1" in decision.evidence
    assert "shutter_count_delta_1" in decision.evidence


def test_filename_and_shutter_count_can_compensate_for_slow_burst_capture():
    target = _manifest(
        filename="DSC0101.ARW",
        capture_time="2026-01-01T12:00:00+00:00",
        metadata={"Make": "Sony", "Model": "ILCE-7M4", "ShutterCount": 101},
    )
    candidate_manifest = _manifest(
        filename="DSC0102.ARW",
        capture_time="2026-01-01T12:01:58+00:00",
        metadata={"Make": "Sony", "Model": "ILCE-7M4"},
    )
    candidate = replace(
        _candidate(candidate_manifest, shutter_count=102),
        capture_time=datetime(2026, 1, 1, 12, 1, 58, tzinfo=timezone.utc),
    )
    decision = _evaluate_candidate(
        target,
        Fingerprint(phash="0123456789abcdef", dhash="fedcba9876543210", width=640, height=480),
        candidate,
        24,
        16,
    )
    assert decision.accepted
    assert "capture_delta_outside_window_118s" in decision.evidence


def test_close_exact_sequence_allows_large_visual_change():
    decision = _calibration_decision(7403469, 7403470, 4, 30, 25)
    assert decision.accepted


def test_nonconsecutive_sequence_can_use_modest_hash_relaxation():
    decision = _calibration_decision(7402757, 7402760, 2, 22, 15)
    assert decision.accepted


def test_exact_sequence_extension_preserves_two_minute_zoom_pair():
    decision = _calibration_decision(7403514, 7403515, 118, 22, 14)
    assert decision.accepted
    assert "bounded_sequence_time_extension" in decision.evidence


def test_long_exact_sequence_gap_does_not_override_large_visual_change():
    decision = _calibration_decision(7403486, 7403487, 202, 28, 23)
    assert not decision.accepted
    assert decision.rejection == "phash_distance"


def test_modest_sequence_relaxation_rejects_large_phash_change():
    decision = _calibration_decision(7402836, 7402838, 15, 24, 15)
    assert not decision.accepted
    assert decision.rejection == "phash_distance"


def test_filename_gap_over_six_gets_no_hash_relaxation():
    decision = _calibration_decision(7402832, 7402839, 29, 18, 16)
    assert not decision.accepted
    assert decision.rejection == "phash_distance"


def test_just_over_capture_window_requires_modest_hashes():
    decision = _calibration_decision(7403485, 7403486, 36, 20, 23)
    assert not decision.accepted
    assert decision.rejection == "dhash_distance"


def test_relaxed_hash_match_rejects_a_chroma_scene_break():
    decision = _calibration_decision(7403488, 7403489, 17, 20, 11, chroma_change=True)
    assert not decision.accepted
    assert decision.rejection == "chroma_distance"
