"""Burst clustering: group near-identical frames into display clusters.

Clustering is an always-on display feature, independent of the semantic-reuse
rollout mode. A frame joins a cluster when its fingerprint is persisted and
leaves only when the frame is deleted.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import insert, select

from photo_server.catalog import assets, burst_clusters, burst_members, image_fingerprints
from photo_server.config import LibraryError
from photo_server.fingerprints import Fingerprint, hamming_distance
from photo_server.models import Manifest

BURST_CLUSTER_POLICY_VERSION = "burst-cluster-v2"
CAPTURE_WINDOW = timedelta(seconds=3)
UPLOAD_NEAR_WINDOW = timedelta(minutes=10)
UPLOAD_FAR_WINDOW = timedelta(days=7)
_MISSING_TIME_MS = 10**18
_MIN_EVIDENCE_SCORE = 4
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
    if delta <= 3:
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
) -> BurstDecision:
    """Apply burst-v2 gates and return the evidence used for the decision."""
    if hamming_distance(fingerprint.phash, candidate.phash) > phash_max:
        return BurstDecision(False, 0, (), "phash_distance")
    if hamming_distance(fingerprint.dhash, candidate.dhash) > dhash_max:
        return BurstDecision(False, 0, (), "dhash_distance")

    target_camera = _camera_identity(target.metadata)
    if (
        target_camera is not None
        and candidate.camera_identity is not None
        and target_camera.casefold() != candidate.camera_identity.casefold()
    ):
        return BurstDecision(False, 0, (), "camera_conflict")

    score = 0
    evidence: list[str] = []
    target_capture = _burst_time(target.capture_time)
    if target_capture is not None and candidate.capture_time is not None:
        try:
            capture_delta = abs(target_capture - candidate.capture_time)
        except TypeError:
            capture_delta = None
        if capture_delta is not None:
            if capture_delta > capture_window:
                # A camera can pause between frames, or metadata can be
                # rounded/coarsened. Keep the time gate strict unless the
                # camera sequence and shutter count independently identify a
                # consecutive pair.
                filename_score, _ = _filename_evidence(
                    target.primary.original_filename, candidate.original_filename
                )
                target_shutter = _shutter_count(target.metadata)
                candidate_shutter = candidate.shutter_count
                shutter_confirms = (
                    target_shutter is not None
                    and candidate_shutter is not None
                    and abs(target_shutter - candidate_shutter) <= 3
                )
                if filename_score == 0 or not shutter_confirms:
                    return BurstDecision(False, 0, (), "capture_time_outside_window")
                evidence.append(f"capture_delta_outside_window_{capture_delta.total_seconds():g}s")
            else:
                score += 3
                evidence.append(f"capture_delta_{capture_delta.total_seconds():g}s")

    if target_camera is not None and candidate.camera_identity is not None:
        score += 1
        evidence.append("same_camera")

    filename_score, filename_reason = _filename_evidence(
        target.primary.original_filename, candidate.original_filename
    )
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

    target_shutter = _shutter_count(target.metadata)
    if target_shutter is not None and candidate.shutter_count is not None:
        shutter_delta = abs(target_shutter - candidate.shutter_count)
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


def _candidate_rows(
    connection,
    asset_id: str,
    version: str,
    width: int,
    height: int,
) -> list:
    """Cluster representatives that can accept a new frame.

    Matching only against representatives prevents transitive chaining: a
    frame cannot gradually stretch a cluster by matching one slightly
    different member after another.
    """
    filters = [
        assets.c.id != asset_id,
        assets.c.deleted_at.is_(None),
        image_fingerprints.c.algorithm_version == version,
        image_fingerprints.c.width == width,
        image_fingerprints.c.height == height,
    ]
    return (
        connection.execute(
            select(
                assets.c.id,
                image_fingerprints.c.phash,
                image_fingerprints.c.dhash,
                assets.c.manifest,
                burst_members.c.cluster_id,
            )
            .select_from(
                assets.join(
                    image_fingerprints,
                    (image_fingerprints.c.asset_id == assets.c.id)
                    & (image_fingerprints.c.algorithm_version == version),
                )
                .join(burst_members, burst_members.c.asset_id == assets.c.id)
                .join(burst_clusters, burst_clusters.c.id == burst_members.c.cluster_id)
            )
            .where(*filters, assets.c.id == burst_clusters.c.representative_asset_id)
            .order_by(assets.c.id)
        )
        .mappings()
        .all()
    )


def join_or_create_cluster(
    connection,
    asset_id: str,
    fingerprint: Fingerprint,
    phash_max: int,
    dhash_max: int,
    capture_window_seconds: int = int(CAPTURE_WINDOW.total_seconds()),
) -> str:
    """Assign one fingerprinted frame to a burst cluster, creating or merging as needed.

    Fingerprints can arrive in any order. A frame may initially form a
    singleton cluster before another compatible frame is fingerprinted, so an
    existing membership must not make the frame permanently ineligible for
    reconciliation.
    """
    existing = connection.scalar(
        select(burst_members.c.cluster_id).where(burst_members.c.asset_id == asset_id)
    )
    target_row = (
        connection.execute(select(assets.c.manifest).where(assets.c.id == asset_id))
        .mappings()
        .one_or_none()
    )
    if target_row is None:
        raise LibraryError(f"Asset not found: {asset_id}")
    target = Manifest.model_validate(target_row["manifest"])

    candidates = []
    for row in _candidate_rows(
        connection,
        asset_id,
        fingerprint.algorithm_version,
        fingerprint.width,
        fingerprint.height,
    ):
        manifest = Manifest.model_validate(row["manifest"])
        candidates.append(
            ClusterCandidate(
                asset_id=row["id"],
                cluster_id=row["cluster_id"],
                phash=row["phash"],
                dhash=row["dhash"],
                capture_time=_burst_time(manifest.capture_time),
                imported_at=_burst_time(manifest.imported_at),
                camera_identity=_camera_identity(manifest.metadata),
                original_filename=manifest.primary.original_filename,
                shutter_count=_shutter_count(manifest.metadata),
            )
        )

    target_capture = _burst_time(target.capture_time)
    candidates.sort(
        key=lambda c: (
            hamming_distance(fingerprint.phash, c.phash),
            hamming_distance(fingerprint.dhash, c.dhash),
            _capture_distance_ms(target_capture, c.capture_time),
            c.asset_id,
        )
    )

    for candidate in candidates:
        if _evaluate_candidate(
            target,
            fingerprint,
            candidate,
            phash_max,
            dhash_max,
            timedelta(seconds=capture_window_seconds),
        ).accepted:
            if existing is None:
                connection.execute(
                    insert(burst_members).values(
                        cluster_id=candidate.cluster_id, asset_id=asset_id
                    )
                )
                return candidate.cluster_id
            if candidate.cluster_id != existing:
                # Keep the target's cluster ID stable and absorb the other
                # cluster. Its members retain their membership; only the
                # redundant cluster record is removed.
                connection.execute(
                    burst_members.update()
                    .where(burst_members.c.cluster_id == candidate.cluster_id)
                    .values(cluster_id=existing)
                )
                connection.execute(
                    burst_clusters.delete().where(burst_clusters.c.id == candidate.cluster_id)
                )
            return existing

    if existing is not None:
        return existing

    cluster_id = str(uuid4())
    connection.execute(
        insert(burst_clusters).values(
            id=cluster_id,
            representative_asset_id=asset_id,
            policy_version=BURST_CLUSTER_POLICY_VERSION,
        )
    )
    connection.execute(
        insert(burst_members).values(cluster_id=cluster_id, asset_id=asset_id)
    )
    return cluster_id


def remove_member(connection, asset_id: str) -> None:
    """Remove one frame from its burst cluster, moving the representative if needed."""
    row = (
        connection.execute(
            select(burst_members.c.cluster_id, burst_clusters.c.representative_asset_id)
            .join_from(
                burst_members, burst_clusters, burst_clusters.c.id == burst_members.c.cluster_id
            )
            .where(burst_members.c.asset_id == asset_id)
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return
    cluster_id = row["cluster_id"]
    was_representative = row["representative_asset_id"] == asset_id
    connection.execute(
        burst_members.delete().where(burst_members.c.asset_id == asset_id)
    )
    if not was_representative:
        return
    survivor = connection.scalar(
        select(burst_members.c.asset_id)
        .where(burst_members.c.cluster_id == cluster_id)
        .order_by(burst_members.c.asset_id)
        .limit(1)
    )
    if survivor is None:
        connection.execute(
            burst_clusters.delete().where(burst_clusters.c.id == cluster_id)
        )
    else:
        connection.execute(
            burst_clusters.update()
            .where(burst_clusters.c.id == cluster_id)
            .values(representative_asset_id=survivor)
        )


def set_representative(connection, cluster_id: str, asset_id: str) -> dict:
    """Move the burst representative to another surviving member."""
    cluster = (
        connection.execute(select(burst_clusters).where(burst_clusters.c.id == cluster_id))
        .mappings()
        .one_or_none()
    )
    if cluster is None:
        raise FileNotFoundError("Burst not found")
    membership = connection.scalar(
        select(burst_members.c.cluster_id).where(
            burst_members.c.cluster_id == cluster_id,
            burst_members.c.asset_id == asset_id,
        )
    )
    if membership is None:
        raise LibraryError("Asset is not a member of this burst")
    asset_row = (
        connection.execute(select(assets.c.id, assets.c.deleted_at).where(assets.c.id == asset_id))
        .mappings()
        .one_or_none()
    )
    if asset_row is None:
        raise LibraryError("Representative asset does not exist")
    if asset_row["deleted_at"] is not None:
        raise LibraryError("Unhide this item before editing")
    connection.execute(
        burst_clusters.update()
        .where(burst_clusters.c.id == cluster_id)
        .values(representative_asset_id=asset_id)
    )
    return {"burstId": cluster_id, "representativeAssetId": asset_id}
