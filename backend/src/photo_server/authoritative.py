"""S3-first durable mutation coordinator (Phase 7).

This module deliberately only owns the durable manifest write.  PostgreSQL
continues to own live projections and the existing catalog methods remain the
projection applier.  A failed projection is therefore recoverable by retrying
the same operation id or by reconciliation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid5

from photo_server.config import LibraryError
from photo_server.manifests import (
    AlbumManifest,
    AssetManifest,
    BlobReference,
    Location,
    PersonManifest,
    Tombstone,
    UserState,
    decode_album_manifest,
    decode_asset_manifest,
    decode_person_manifest,
    encode,
)
from photo_server.models import Album, Manifest, Mutation


def _utc(value: str | None) -> str:
    if not value:
        return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    return value.replace("+00:00", "Z")


class AuthoritativeMutationCoordinator:
    def __init__(self, service):
        self.service = service

    def _asset(self, current: Manifest, revision: int, operation_id: UUID, mutation: Mutation) -> AssetManifest:
        state = current.user_state
        location = state.location
        v1_location = None
        if location is not None and location.latitude is not None and location.longitude is not None:
            v1_location = Location(location.name, location.latitude, location.longitude)
        blobs = tuple(
            BlobReference(
                blob.blob_id, blob.role, f"objects/{blob.sha256}", blob.original_filename,
                blob.sha256, blob.size_bytes, blob.mime_type,
            ) for blob in current.blobs
        )
        return AssetManifest(
            library_id=current.library_id, asset_id=current.asset_id,
            revision=revision, parent_revision=revision - 1 if revision > 1 else None,
            operation_id=operation_id, created_at=_utc(current.imported_at),
            imported_at=_utc(current.imported_at), capture_time=_utc(current.capture_time) if current.capture_time else None,
            blobs=blobs, primary_blob_id=current.primary_blob_id,
            extracted_metadata=current.metadata, user_state=UserState(
                state.rating, state.favorite, state.caption, tuple(state.keywords), v1_location
            ), deleted_at=_utc(current.deleted_at) if current.deleted_at else None, processing=(),
        )

    def _album(self, current: Album | None, library_id: UUID, album_id: UUID,
               revision: int, operation_id: UUID, values: dict) -> AlbumManifest:
        return AlbumManifest(
            library_id=library_id, album_id=album_id, revision=revision,
            parent_revision=current.revision if current else None, operation_id=operation_id,
            created_at=datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
            name=values["name"], description=values.get("description", ""),
            asset_ids=tuple(UUID(str(x)) for x in values.get("assetIds", [])),
            deleted_at=values.get("deletedAt"),
        )

    def _put(self, key: str, record) -> None:
        # DualWritePublisher performs create-only PUT, full read-back, and
        # checksum verification.  No projection call occurs before this returns.
        self.service.dual_write._put_immutable(key, encode(record), "application/json")
        if self.service.storage.read_bytes(key) != encode(record):
            raise LibraryError(f"Verified immutable write changed: {key}")

    def publish(self, operation_id: UUID, mutation: Mutation) -> None:
        catalog = self.service.catalog
        kind, action = mutation.action.split(".")
        if kind == "asset":
            current = catalog.get(str(mutation.entity_id))
            if current is None:
                raise FileNotFoundError("Entity not found")
            if mutation.expected_revision is not None and current.revision != mutation.expected_revision:
                raise LibraryError("Revision changed; reload before editing")
            values = current.model_dump(mode="json")
            if action == "patch":
                values["user_state"] = {**current.user_state.document(), **mutation.changes}
                values["user_state"].pop("metadata", None)
                values["user_state"].pop("captureTime", None)
                if "metadata" in mutation.changes:
                    values["metadata"] = mutation.changes["metadata"]
                    values["capture_time"] = mutation.changes.get("captureTime")
            elif action == "metadata":
                values["metadata"] = mutation.changes["metadata"]
                values["capture_time"] = mutation.changes.get("captureTime")
            elif action == "delete":
                values["deleted_at"] = current.deleted_at or datetime.now(UTC).isoformat().replace("+00:00", "Z")
            elif action == "restore":
                values["deleted_at"] = None
            else:
                return
            proposed = Manifest.model_validate({**values, "revision": current.revision + 1,
                "previous_revision": current.revision, "operation_id": operation_id,
                "mutation": mutation.document()})
            manifest_key = f"manifests/assets/{proposed.asset_id}/{proposed.revision}.json"
            if self.service.storage.head(manifest_key) is not None:
                stored = decode_asset_manifest(self.service.storage.read_bytes(manifest_key))
                if stored.operation_id != operation_id:
                    raise LibraryError("Immutable manifest revision conflicts with this operation")
                if action == "delete":
                    tombstone_key = f"tombstones/asset/{proposed.asset_id}/{proposed.revision}.json"
                    if self.service.storage.head(tombstone_key) is None:
                        tombstone = Tombstone(
                            catalog.library_id(), "asset", proposed.asset_id, proposed.revision,
                            proposed.revision - 1, operation_id,
                            stored.deleted_at or stored.created_at, stored.created_at,
                        )
                        self._put(tombstone_key, tombstone)
                return
            record = self._asset(proposed, proposed.revision, operation_id, mutation)
            self._put(manifest_key, record)
            if action == "delete":
                tombstone = Tombstone(catalog.library_id(), "asset", proposed.asset_id,
                    proposed.revision, current.revision, operation_id, record.deleted_at or record.created_at,
                    record.created_at)
                self._put(f"tombstones/asset/{proposed.asset_id}/{proposed.revision}.json", tombstone)
        elif kind == "album":
            current = catalog.get_album(str(mutation.entity_id))
            if mutation.expected_revision is not None and (current is None or current.revision != mutation.expected_revision):
                raise LibraryError("Revision changed; reload before editing")
            if current is None and action != "create":
                raise FileNotFoundError("Entity not found")
            values = current.document() if current else {"name": "", "description": "", "assetIds": [], "deletedAt": None}
            values.update(mutation.changes)
            if action == "delete":
                values["deletedAt"] = values.get("deletedAt") or datetime.now(UTC).isoformat().replace("+00:00", "Z")
            elif action == "restore":
                values["deletedAt"] = None
            revision = current.revision + 1 if current else 1
            record = self._album(current, catalog.library_id(), mutation.entity_id, revision, operation_id, values)
            manifest_key = f"manifests/albums/{record.album_id}/{record.revision}.json"
            if self.service.storage.head(manifest_key) is not None:
                stored = decode_album_manifest(self.service.storage.read_bytes(manifest_key))
                if stored.operation_id != operation_id:
                    raise LibraryError("Immutable manifest revision conflicts with this operation")
                if action == "delete":
                    tombstone_key = f"tombstones/album/{record.album_id}/{record.revision}.json"
                    if self.service.storage.head(tombstone_key) is None:
                        tombstone = Tombstone(
                            catalog.library_id(), "album", record.album_id, record.revision,
                            record.revision - 1, operation_id,
                            stored.deleted_at or stored.created_at, stored.created_at,
                        )
                        self._put(tombstone_key, tombstone)
                return
            self._put(manifest_key, record)
            if action == "delete":
                tombstone = Tombstone(catalog.library_id(), "album", record.album_id, record.revision,
                    record.parent_revision or 1, operation_id, record.deleted_at or record.created_at, record.created_at)
                self._put(f"tombstones/album/{record.album_id}/{record.revision}.json", tombstone)

    def publish_person_rename(self, operation_id: UUID, person_id: UUID, display_name: str) -> None:
        people = {UUID(item["personId"]): item for item in self.service.catalog.all_people()}
        current = people.get(person_id)
        if current is None:
            raise FileNotFoundError("Person not found")
        prefix = f"manifests/people/{person_id}/"
        revisions = []
        for key in self.service.storage.keys(prefix):
            suffix = key.removeprefix(prefix).removesuffix(".json")
            if suffix.isdigit():
                stored = decode_person_manifest(self.service.storage.read_bytes(key))
                if stored.operation_id == operation_id:
                    return
                revisions.append(int(suffix))
        revision = max(revisions, default=0) + 1
        record = PersonManifest(
            library_id=self.service.catalog.library_id(), person_id=person_id, revision=revision,
            parent_revision=revision - 1 if revision > 1 else None, operation_id=operation_id,
            created_at=current["createdAt"], display_name=display_name,
            face_ids=tuple(UUID(face_id) for face_id in current["faceIds"]), deleted_at=None,
        )
        self._put(f"{prefix}{revision}.json", record)

    def _person_revision(self, person_id: UUID) -> int:
        prefix = f"manifests/people/{person_id}/"
        values = []
        for key in self.service.storage.keys(prefix):
            suffix = key.removeprefix(prefix).removesuffix(".json")
            if suffix.isdigit():
                values.append(int(suffix))
        return max(values, default=0)

    def _publish_person(self, operation_id: UUID, item: dict, *, display_name: str | None = None,
                        face_ids: list[str] | None = None, deleted_at: str | None = None) -> None:
        person_id = UUID(item["personId"])
        current_revision = self._person_revision(person_id)
        revision = current_revision + 1
        created_at = item["createdAt"]
        prefix = f"manifests/people/{person_id}/"
        record = PersonManifest(
            library_id=self.service.catalog.library_id(), person_id=person_id,
            revision=revision, parent_revision=current_revision or None,
            operation_id=operation_id, created_at=created_at,
            display_name=item["displayName"] if display_name is None else display_name,
            face_ids=tuple(UUID(face_id) for face_id in (item["faceIds"] if face_ids is None else face_ids)),
            deleted_at=deleted_at,
        )
        key = f"{prefix}{revision}.json"
        if self.service.storage.head(key) is None:
            self._put(key, record)
        else:
            stored = decode_person_manifest(self.service.storage.read_bytes(key))
            if stored.operation_id != operation_id:
                raise LibraryError("Immutable person revision conflicts with this operation")
        if deleted_at is not None:
            if revision == 1:
                raise LibraryError("A person tombstone requires a prior person revision")
            tombstone = Tombstone(
                self.service.catalog.library_id(), "person", person_id, revision,
                revision - 1, operation_id, deleted_at, created_at,
            )
            tombstone_key = f"tombstones/person/{person_id}/{revision}.json"
            if self.service.storage.head(tombstone_key) is None:
                self._put(tombstone_key, tombstone)

    def publish_face_operation(self, operation_id: UUID, request: dict) -> None:
        action = request.get("action")
        if action == "person.rename":
            self.publish_person_rename(operation_id, UUID(request["personId"]), request["displayName"])
            return
        people = {item["personId"]: item for item in self.service.catalog.all_people()}
        if action == "person.merge":
            source_id = request["sourcePersonId"]
            target_id = request["targetPersonId"]
            source = people.get(source_id)
            target = people.get(target_id)
            if source is None or target is None:
                raise FileNotFoundError("Person not found")
            merged_faces = list(dict.fromkeys([*target["faceIds"], *source["faceIds"]]))
            self._publish_person(operation_id, target, face_ids=merged_faces)
            deleted_at = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
            if self._person_revision(UUID(source_id)) == 0:
                self._publish_person(operation_id, source)
            self._publish_person(operation_id, source, face_ids=[], deleted_at=deleted_at)
            return
        if action == "faces.move":
            face_ids = set(request["faceIds"])
            target_id = request.get("targetPersonId")
            if target_id is None:
                target_id = str(uuid5(self.service.catalog.library_id(), f"face-operation:{operation_id}"))
                target = {
                    "personId": target_id, "displayName": "", "faceIds": [],
                    "createdAt": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
                }
                people[target_id] = target
            else:
                target = people.get(target_id)
                if target is None:
                    raise FileNotFoundError("Destination person not found")
            affected = []
            for item in people.values():
                remaining = [face_id for face_id in item["faceIds"] if face_id not in face_ids]
                if remaining != item["faceIds"]:
                    affected.append((item, remaining))
            if any(face_id not in {value for item in people.values() for value in item["faceIds"]}
                   for face_id in face_ids):
                raise LibraryError("One or more selected faces are no longer current")
            target_faces = list(dict.fromkeys([*target["faceIds"], *face_ids]))
            for item, remaining in affected:
                if item["personId"] != target_id:
                    self._publish_person(operation_id, item, face_ids=remaining)
            self._publish_person(operation_id, target, face_ids=target_faces)
            return
        raise LibraryError("Invalid face operation")
