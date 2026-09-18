"""Burst clustering: group near-identical frames into display clusters.

Clustering is an always-on display feature, independent of the semantic-reuse
rollout mode. A frame joins a cluster when its fingerprint is persisted and
leaves only when the frame is deleted.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import insert, select

from photo_server.browsing import camera_time
from photo_server.catalog import assets, burst_clusters, burst_members, image_fingerprints
from photo_server.config import LibraryError
from photo_server.fingerprints import Fingerprint, hamming_distance
from photo_server.models import Manifest

BURST_CLUSTER_POLICY_VERSION = "burst-cluster-v1"
CAPTURE_WINDOW = timedelta(seconds=3)
_MISSING_TIME_MS = 10**18


@dataclass(frozen=True)
class ClusterCandidate:
    asset_id: str
    cluster_id: str
    phash: str
    dhash: str
    capture_time: datetime | None
    camera_identity: str | None


def _camera_identity(metadata: dict) -> str | None:
    """Stable camera identifier: ``Make Model``, or None when unknown."""
    parts = [metadata.get("Make"), metadata.get("Model")]
    parts = [part for part in parts if part]
    return " ".join(parts) if parts else None


def _capture_distance_ms(a: datetime | None, b: datetime | None) -> int:
    if a is None or b is None:
        return _MISSING_TIME_MS
    return abs(int((a - b).total_seconds() * 1000))


def _candidate_rows(
    connection,
    asset_id: str,
    version: str,
    width: int,
    height: int,
) -> list:
    """Fingerprinted, non-deleted frames in an existing cluster with matching dimensions."""
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
                ).join(burst_members, burst_members.c.asset_id == assets.c.id)
            )
            .where(*filters)
            .order_by(assets.c.id)
        )
        .mappings()
        .all()
    )


def _passes_gates(
    target: Manifest,
    fingerprint: Fingerprint,
    candidate: ClusterCandidate,
    phash_max: int,
    dhash_max: int,
) -> bool:
    """Clustering gates evaluated relative to an existing cluster member."""
    target_capture = camera_time(target.capture_time)
    if (
        target_capture is not None
        and candidate.capture_time is not None
        and abs(target_capture - candidate.capture_time) > CAPTURE_WINDOW
    ):
        return False
    target_camera = _camera_identity(target.metadata)
    if (
        target_camera is not None
        and candidate.camera_identity is not None
        and target_camera.casefold() != candidate.camera_identity.casefold()
    ):
        return False
    if hamming_distance(fingerprint.phash, candidate.phash) > phash_max:
        return False
    if hamming_distance(fingerprint.dhash, candidate.dhash) > dhash_max:
        return False
    return True


def join_or_create_cluster(
    connection,
    asset_id: str,
    fingerprint: Fingerprint,
    phash_max: int,
    dhash_max: int,
) -> str:
    """Assign one fingerprinted frame to a burst cluster, creating one if needed."""
    existing = connection.scalar(
        select(burst_members.c.cluster_id).where(burst_members.c.asset_id == asset_id)
    )
    if existing is not None:
        return existing
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
                capture_time=camera_time(manifest.capture_time),
                camera_identity=_camera_identity(manifest.metadata),
            )
        )

    target_capture = camera_time(target.capture_time)
    candidates.sort(
        key=lambda c: (
            hamming_distance(fingerprint.phash, c.phash),
            hamming_distance(fingerprint.dhash, c.dhash),
            _capture_distance_ms(target_capture, c.capture_time),
            c.asset_id,
        )
    )

    for candidate in candidates:
        if _passes_gates(target, fingerprint, candidate, phash_max, dhash_max):
            connection.execute(
                insert(burst_members).values(
                    cluster_id=candidate.cluster_id, asset_id=asset_id
                )
            )
            return candidate.cluster_id

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
        raise LibraryError("Restore this item before editing")
    connection.execute(
        burst_clusters.update()
        .where(burst_clusters.c.id == cluster_id)
        .values(representative_asset_id=asset_id)
    )
    return {"burstId": cluster_id, "representativeAssetId": asset_id}
