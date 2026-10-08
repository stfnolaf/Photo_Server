"""Canonical S3 authority for fingerprints and burst display state."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from threading import RLock
from uuid import UUID, uuid5

from photo_server.bursts import (
    BURST_CLUSTER_POLICY_VERSION,
    ClusterCandidate,
    _burst_time,
    _camera_identity,
    _capture_distance_ms,
    _evaluate_candidate,
    _filename_evidence,
    _shutter_count,
)
from photo_server.config import LibraryError
from photo_server.fingerprints import BURST_HASH_VERSION, Fingerprint, hamming_distance
from photo_server.manifests import (
    BurstCluster,
    BurstManifest,
    FingerprintManifest,
    decode_burst_manifest,
    decode_fingerprint_manifest,
)

_LOCK = RLock()
CURRENT_BURST_KEY = "library-state/bursts.json"


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def fingerprint_key(asset_id: UUID | str, algorithm_version: str) -> str:
    version = hashlib.sha256(algorithm_version.encode()).hexdigest()
    return f"manifests/fingerprints/{asset_id}/{version}.json"


def _fingerprint(record: FingerprintManifest) -> Fingerprint:
    return Fingerprint(
        algorithm_version=record.algorithm_version,
        phash=record.phash,
        dhash=record.dhash,
        width=record.width,
        height=record.height,
        chroma_histogram=record.chroma_histogram,
    )


def load_fingerprints(storage) -> dict[tuple[str, str], FingerprintManifest]:
    records: dict[tuple[str, str], FingerprintManifest] = {}
    for key in sorted(storage.keys("manifests/fingerprints/")):
        record = decode_fingerprint_manifest(storage.read_bytes(key))
        if key != fingerprint_key(record.asset_id, record.algorithm_version):
            raise LibraryError(f"Fingerprint manifest identity does not match its key: {key}")
        identity = (str(record.asset_id), record.algorithm_version)
        if identity in records:
            raise LibraryError(f"Duplicate canonical fingerprint: {record.asset_id}")
        records[identity] = record
    return records


def load_burst_history(storage) -> tuple[BurstManifest, ...]:
    records = []
    for key in sorted(storage.keys("manifests/bursts/")):
        suffix = key.removeprefix("manifests/bursts/").removesuffix(".json")
        if not suffix.isdigit():
            raise LibraryError(f"Malformed burst manifest key: {key}")
        record = decode_burst_manifest(storage.read_bytes(key))
        if record.revision != int(suffix):
            raise LibraryError(f"Burst manifest revision does not match its key: {key}")
        records.append(record)
    records.sort(key=lambda record: record.revision)
    operation_ids: set[UUID] = set()
    for index, record in enumerate(records, start=1):
        if record.revision != index or record.parent_revision != (index - 1 or None):
            raise LibraryError("Canonical burst history is incomplete")
        if record.operation_id in operation_ids:
            raise LibraryError("Canonical burst history reuses an operation ID")
        operation_ids.add(record.operation_id)
    return tuple(records)


def load_burst_state(storage) -> BurstManifest | None:
    """Load current burst state, falling back to the pre-flattening head."""
    if storage.head(CURRENT_BURST_KEY) is not None:
        return decode_burst_manifest(storage.read_bytes(CURRENT_BURST_KEY))
    history = load_burst_history(storage)
    return history[-1] if history else None


def _assert_library(library_id: UUID, records) -> None:
    if any(record.library_id != library_id for record in records):
        raise LibraryError("Canonical burst records belong to a different library")


def _timeline(record) -> datetime:
    value = record.capture_time or record.imported_at
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except (AttributeError, ValueError):
        return datetime.min


def _cluster_groups(service, fingerprints, excluded: set[str]) -> list[list[str]]:
    assets = {}
    for asset_id, _version in fingerprints:
        if asset_id in assets:
            continue
        try:
            record = service.canonical_asset(asset_id)
        except LibraryError:
            continue
        if record.deleted_at is None:
            assets[asset_id] = record
    ordered = sorted(assets, key=lambda asset_id: (_timeline(assets[asset_id]), asset_id))
    groups: list[list[str]] = []
    membership: dict[str, int] = {}
    for asset_id in ordered:
        if asset_id in excluded:
            continue
        record = fingerprints.get((asset_id, BURST_HASH_VERSION))
        if record is None:
            continue
        value = _fingerprint(record)
        target = assets[asset_id]
        candidates = []
        for candidate_id, group_index in membership.items():
            candidate_record = fingerprints.get((candidate_id, record.algorithm_version))
            if candidate_record is None or (
                candidate_record.width,
                candidate_record.height,
            ) != (record.width, record.height):
                continue
            candidate_asset = assets[candidate_id]
            candidates.append(
                ClusterCandidate(
                    asset_id=candidate_id,
                    cluster_id=str(group_index),
                    phash=candidate_record.phash,
                    dhash=candidate_record.dhash,
                    capture_time=_burst_time(candidate_asset.capture_time),
                    imported_at=_burst_time(candidate_asset.imported_at),
                    camera_identity=_camera_identity(candidate_asset.metadata),
                    original_filename=candidate_asset.primary.original_filename,
                    shutter_count=_shutter_count(candidate_asset.metadata),
                    chroma_histogram=candidate_record.chroma_histogram,
                )
            )

        def order(candidate, target=target, value=value):
            _, reason = _filename_evidence(
                target.primary.original_filename, candidate.original_filename
            )
            return (
                0 if reason == "filename_sequence_delta_1" else 1,
                hamming_distance(value.phash, candidate.phash),
                hamming_distance(value.dhash, candidate.dhash),
                _capture_distance_ms(_burst_time(target.capture_time), candidate.capture_time),
                candidate.asset_id,
            )

        chosen = None
        for candidate in sorted(candidates, key=order):
            if _evaluate_candidate(
                target,
                value,
                candidate,
                service.settings.burst_cluster_phash_max_distance,
                service.settings.burst_cluster_dhash_max_distance,
                timedelta(seconds=service.settings.burst_cluster_capture_window_seconds),
                service.settings.burst_cluster_chroma_max_distance,
            ).accepted:
                chosen = int(candidate.cluster_id)
                break
        if chosen is None:
            chosen = len(groups)
            groups.append([])
        groups[chosen].append(asset_id)
        membership[asset_id] = chosen
    return groups


def _clusters(service, fingerprints, previous: BurstManifest | None, excluded: set[str]):
    groups = _cluster_groups(service, fingerprints, excluded)
    old = list(previous.clusters) if previous else []
    claimed: set[UUID] = set()
    result = []
    for group in groups:
        members = set(group)
        matches = sorted(
            (
                (len(members & {str(value) for value in cluster.asset_ids}), str(cluster.cluster_id), cluster)
                for cluster in old
                if members & {str(value) for value in cluster.asset_ids}
                and cluster.cluster_id not in claimed
            ),
            key=lambda value: (-value[0], value[1]),
        )
        prior = matches[0][2] if matches else None
        if prior is None:
            cluster_id = uuid5(
                service.library_id,
                f"{BURST_CLUSTER_POLICY_VERSION}:{group[0]}",
            )
            representative = UUID(group[0])
            selected = False
        else:
            cluster_id = prior.cluster_id
            claimed.add(cluster_id)
            if str(prior.representative_asset_id) in members:
                representative = prior.representative_asset_id
                selected = prior.representative_selected
            else:
                representative = UUID(group[0])
                selected = False
        result.append(
            BurstCluster(
                cluster_id=cluster_id,
                representative_asset_id=representative,
                representative_selected=selected,
                asset_ids=tuple(UUID(value) for value in sorted(group)),
            )
        )
    return tuple(sorted(result, key=lambda cluster: str(cluster.cluster_id)))


class BurstAuthority:
    def __init__(self, service):
        self.service = service

    def _publish(self, snapshot: BurstManifest) -> BurstManifest:
        self.service.publisher.publish_current_record(
            CURRENT_BURST_KEY, snapshot, "burst"
        )
        return snapshot

    def _snapshot(
        self,
        operation_id: UUID,
        *,
        action: str,
        asset_id: str | None = None,
        excluded=None,
        force=False,
    ) -> BurstManifest:
        previous = load_burst_state(self.service.storage)
        fingerprints = load_fingerprints(self.service.storage)
        _assert_library(self.service.library_id, (previous,) if previous else ())
        _assert_library(self.service.library_id, fingerprints.values())
        exclusions = set(excluded if excluded is not None else (
            str(value) for value in previous.excluded_asset_ids
        )) if previous else set(excluded or ())
        clusters = _clusters(self.service, fingerprints, previous, exclusions)
        if previous and not force and (
            clusters == previous.clusters
            and tuple(sorted(exclusions))
            == tuple(sorted(str(value) for value in previous.excluded_asset_ids))
        ):
            return previous
        return self._publish(
            BurstManifest(
                library_id=self.service.library_id,
                revision=previous.revision + 1 if previous else 1,
                parent_revision=previous.revision if previous else None,
                operation_id=operation_id,
                created_at=_now(),
                policy_version=BURST_CLUSTER_POLICY_VERSION,
                operation_action=action,
                operation_cluster_id=None,
                operation_asset_id=UUID(asset_id) if asset_id is not None else None,
                clusters=clusters,
                excluded_asset_ids=tuple(UUID(value) for value in sorted(exclusions)),
            )
        )

    def publish_fingerprint(self, asset_id: str, fingerprint: Fingerprint) -> BurstManifest:
        with _LOCK, self.service.catalog.writer():
            key = fingerprint_key(asset_id, fingerprint.algorithm_version)
            fingerprint_operation_id = uuid5(
                self.service.library_id,
                "fingerprint:" + ":".join(
                    str(value)
                    for value in (
                        asset_id,
                        fingerprint.algorithm_version,
                        fingerprint.phash,
                        fingerprint.dhash,
                        fingerprint.width,
                        fingerprint.height,
                        fingerprint.chroma_histogram,
                    )
                ),
            )
            if self.service.storage.head(key) is None:
                record = FingerprintManifest(
                    library_id=self.service.library_id,
                    asset_id=UUID(asset_id),
                    algorithm_version=fingerprint.algorithm_version,
                    phash=fingerprint.phash,
                    dhash=fingerprint.dhash,
                    width=fingerprint.width,
                    height=fingerprint.height,
                    chroma_histogram=fingerprint.chroma_histogram,
                    created_at=_now(),
                )
                self.service.publisher.publish_record(key, record, "fingerprint")
            else:
                existing = decode_fingerprint_manifest(self.service.storage.read_bytes(key))
                if existing.library_id != self.service.library_id:
                    raise LibraryError("Canonical fingerprint belongs to a different library")
                if _fingerprint(existing) != fingerprint:
                    raise LibraryError("Canonical fingerprint conflicts with computed fingerprint")
            previous = load_burst_state(self.service.storage)
            parent_revision = previous.revision if previous else 0
            # Fingerprint bytes identify the immutable fingerprint record, but
            # they do not identify a burst transition: metadata or another
            # fingerprint can change clustering while these bytes stay fixed.
            # Scoping the transition to its parent makes each changed snapshot
            # unique, while an S3-written retry observes that snapshot as the
            # current head and reapplies it without publishing another revision.
            operation_id = uuid5(
                fingerprint_operation_id,
                f"burst-parent:{parent_revision}",
            )
            snapshot = self._snapshot(
                operation_id,
                action="fingerprint.publish",
                asset_id=asset_id,
            )
            try:
                self.service.catalog.apply_burst_projection(
                    tuple(load_fingerprints(self.service.storage).values()), snapshot
                )
            except Exception as error:
                self.service.publisher.record_reconciliation(
                    snapshot.operation_id,
                    {
                        "status": "canonical-written-projection-failed",
                        "assetId": asset_id,
                        "action": "fingerprint.publish",
                        "error": str(error),
                    },
                )
                raise
            return snapshot

    def mutate(self, operation_id: UUID, action: str, cluster_id: str, asset_id: str):
        with _LOCK, self.service.catalog.writer():
            previous = load_burst_state(self.service.storage)
            if previous is None:
                raise FileNotFoundError("Burst not found")
            _assert_library(self.service.library_id, (previous,))
            if previous.operation_id == operation_id:
                expected_action = f"burst.{action}"
                if (
                    previous.operation_action != expected_action
                    or str(previous.operation_cluster_id) != cluster_id
                    or str(previous.operation_asset_id) != asset_id
                ):
                    raise LibraryError("Operation ID was reused with a different request")
                self.service.catalog.apply_burst_projection(
                    tuple(load_fingerprints(self.service.storage).values()), previous
                )
                if action == "setRepresentative":
                    return {"burstId": cluster_id, "representativeAssetId": asset_id}
                return {"burstId": cluster_id, "removedAssetId": asset_id}
            clusters = list(previous.clusters)
            excluded = {str(value) for value in previous.excluded_asset_ids}
            if action == "removeMember" and asset_id in excluded:
                self.service.catalog.apply_burst_projection(
                    tuple(load_fingerprints(self.service.storage).values()), previous
                )
                return {"burstId": cluster_id, "removedAssetId": asset_id}
            index = next(
                (i for i, cluster in enumerate(clusters) if str(cluster.cluster_id) == cluster_id),
                None,
            )
            if index is None:
                raise FileNotFoundError("Burst not found")
            cluster = clusters[index]
            members = {str(value) for value in cluster.asset_ids}
            if asset_id not in members:
                raise LibraryError("Asset is not a member of this burst")
            if action == "setRepresentative":
                if (
                    str(cluster.representative_asset_id) == asset_id
                    and cluster.representative_selected
                ):
                    self.service.catalog.apply_burst_projection(
                        tuple(load_fingerprints(self.service.storage).values()), previous
                    )
                    return {"burstId": cluster_id, "representativeAssetId": asset_id}
                clusters[index] = BurstCluster(
                    cluster.cluster_id,
                    UUID(asset_id),
                    True,
                    cluster.asset_ids,
                )
                result = {"burstId": cluster_id, "representativeAssetId": asset_id}
            elif action == "removeMember":
                excluded.add(asset_id)
                remaining = tuple(value for value in cluster.asset_ids if str(value) != asset_id)
                if not remaining:
                    clusters.pop(index)
                else:
                    representative = cluster.representative_asset_id
                    selected = cluster.representative_selected
                    if str(representative) == asset_id:
                        representative = min(remaining, key=str)
                        selected = False
                    clusters[index] = BurstCluster(
                        cluster.cluster_id,
                        representative,
                        selected,
                        remaining,
                    )
                result = {"burstId": cluster_id, "removedAssetId": asset_id}
            else:
                raise LibraryError("Invalid burst mutation")
            snapshot = BurstManifest(
                library_id=self.service.library_id,
                revision=previous.revision + 1,
                parent_revision=previous.revision,
                operation_id=operation_id,
                created_at=_now(),
                policy_version=previous.policy_version,
                operation_action=f"burst.{action}",
                operation_cluster_id=UUID(cluster_id),
                operation_asset_id=UUID(asset_id),
                clusters=tuple(sorted(clusters, key=lambda value: str(value.cluster_id))),
                excluded_asset_ids=tuple(UUID(value) for value in sorted(excluded)),
            )
            self._publish(snapshot)
            self.service.catalog.apply_burst_projection(
                tuple(load_fingerprints(self.service.storage).values()), snapshot
            )
            return result

    def synchronize(self, operation_id: UUID, asset_id: str):
        """Refresh membership after a canonical asset delete/restore."""
        with _LOCK, self.service.catalog.writer():
            fingerprints = tuple(load_fingerprints(self.service.storage).values())
            previous = load_burst_state(self.service.storage)
            if not fingerprints and previous is None:
                return None
            snapshot = self._snapshot(
                operation_id,
                action="asset.synchronize",
                asset_id=asset_id,
            )
            self.service.catalog.apply_burst_projection(fingerprints, snapshot)
            return snapshot

    def refresh(self, operation_id: UUID, *, clear_exclusions: bool = False):
        with _LOCK, self.service.catalog.writer():
            snapshot = self._snapshot(
                operation_id,
                action="burst.recluster",
                excluded=set() if clear_exclusions else None,
                force=True,
            )
            fingerprints = tuple(load_fingerprints(self.service.storage).values())
            self.service.catalog.apply_burst_projection(fingerprints, snapshot)
            return {
                "fingerprintedAssets": len(fingerprints),
                "clusters": len(snapshot.clusters),
                "members": sum(len(cluster.asset_ids) for cluster in snapshot.clusters),
                "semanticJobsCleared": 0,
            }
