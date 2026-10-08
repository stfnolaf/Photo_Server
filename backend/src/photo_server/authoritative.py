"""S3-authoritative durable mutation coordinator.

This module deliberately only owns the durable manifest write.  PostgreSQL
continues to own live projections and the existing catalog methods remain the
projection applier.  A failed projection is therefore recoverable by retrying
the same operation id or by reconciliation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid5

from photo_server.ai_publication import (
    build_face_record,
    build_processing_artifact,
    cluster_faces,
    derive_face_id,
    derive_person_id,
    publish_ai_artifact,
    s3_face_state,
    unit,
)
from photo_server.config import LibraryError
from photo_server.manifests import (
    AlbumManifest,
    AssetManifest,
    BlobReference,
    FaceManifest,
    Location,
    PersonManifest,
    Tombstone,
    UserState,
    decode_album_manifest,
    decode_asset_manifest,
    decode_face_manifest,
    decode_person_manifest,
    encode,
)
from photo_server.models import Album, Manifest, Mutation


def _utc(value: str | None) -> str:
    if not value:
        return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC).isoformat().replace("+00:00", "Z")


class AuthoritativeMutationCoordinator:
    def __init__(self, service):
        self.service = service

    def _library_id(self) -> UUID:
        if self.service.library_id is None:
            raise LibraryError("Canonical library identity is missing")
        return self.service.library_id

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
        # CanonicalPublisher performs create-only PUT, full read-back, and
        # checksum verification.  No projection call occurs before this returns.
        self.service.publisher._put_immutable(key, encode(record), "application/json")
        if self.service.storage.read_bytes(key) != encode(record):
            raise LibraryError(f"Verified immutable write changed: {key}")

    def _asset_projection(self, manifest: AssetManifest, mutation: Mutation) -> Manifest:
        projection = self.service.publisher.projection_from_manifest(manifest)
        return Manifest.model_validate({
            **projection.model_dump(mode="json"),
            "revision": manifest.revision,
            "previous_revision": manifest.parent_revision,
            "operation_id": manifest.operation_id,
            "mutation": mutation.document() if manifest.revision > 1 else None,
        })

    def _album_projection(self, manifest: AlbumManifest, mutation: Mutation) -> Album:
        return Album.model_validate({
            "schemaVersion": 1,
            "libraryId": str(manifest.library_id),
            "albumId": str(manifest.album_id),
            "revision": manifest.revision,
            "previousRevision": manifest.parent_revision,
            "operationId": str(manifest.operation_id),
            "mutation": mutation.document(),
            "name": manifest.name,
            "description": manifest.description,
            "assetIds": [str(asset_id) for asset_id in manifest.asset_ids],
            "deletedAt": manifest.deleted_at,
        })

    def publish(self, operation_id: UUID, mutation: Mutation) -> Manifest | Album:
        catalog = self.service.catalog
        kind, action = mutation.action.split(".")
        if kind == "asset":
            if catalog.get(str(mutation.entity_id)) is None:
                raise FileNotFoundError("Entity not found")
            canonical_heads = []
            prefix = f"manifests/assets/{mutation.entity_id}/"
            for key in self.service.storage.keys(prefix):
                suffix = key.removeprefix(prefix).removesuffix(".json")
                if suffix.isdigit():
                    canonical_heads.append((int(suffix), key))
            if not canonical_heads:
                raise LibraryError(f"Canonical asset manifest is missing: {mutation.entity_id}")
            _, head_key = max(canonical_heads)
            canonical = decode_asset_manifest(self.service.storage.read_bytes(head_key))
            if canonical.operation_id == operation_id:
                return self._asset_projection(canonical, mutation)
            current = self.service.publisher.projection_from_manifest(canonical)
            revision_base = canonical.revision
            if mutation.expected_revision is not None and revision_base != mutation.expected_revision:
                raise LibraryError("Revision changed; reload before editing")
            if current.deleted_at and action not in {"restore", "delete"}:
                raise LibraryError("Unhide this item before editing")
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
                raise LibraryError("Invalid asset mutation")
            proposed = Manifest.model_validate({**values, "revision": revision_base + 1,
                "previous_revision": revision_base, "operation_id": operation_id,
                "mutation": mutation.document()})
            manifest_key = f"manifests/assets/{proposed.asset_id}/{proposed.revision}.json"
            while self.service.storage.head(manifest_key) is not None:
                stored = decode_asset_manifest(self.service.storage.read_bytes(manifest_key))
                if stored.operation_id == operation_id:
                    if action == "delete":
                        tombstone_key = f"tombstones/asset/{proposed.asset_id}/{proposed.revision}.json"
                        if self.service.storage.head(tombstone_key) is None:
                            tombstone = Tombstone(
                                self._library_id(), "asset", proposed.asset_id, proposed.revision,
                                proposed.revision - 1, operation_id,
                                stored.deleted_at or stored.created_at, stored.created_at,
                            )
                            self._put(tombstone_key, tombstone)
                    return self._asset_projection(stored, mutation)
                # The projection may have been stale when the canonical head
                # was first read. Advance from the object that won the
                # immutable revision race and retry at the next revision.
                converter = getattr(self.service.publisher, "projection_from_manifest", None)
                if converter is None:
                    raise LibraryError("Immutable manifest revision conflicts with this operation")
                current = converter(stored)
                revision_base = stored.revision
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
                proposed = Manifest.model_validate({
                    **values, "revision": revision_base + 1,
                    "previous_revision": revision_base, "operation_id": operation_id,
                    "mutation": mutation.document(),
                })
                manifest_key = f"manifests/assets/{proposed.asset_id}/{proposed.revision}.json"
            record = self._asset(proposed, proposed.revision, operation_id, mutation)
            self._put(manifest_key, record)
            if action == "delete":
                tombstone = Tombstone(self._library_id(), "asset", proposed.asset_id,
                    proposed.revision, proposed.revision - 1, operation_id, record.deleted_at or record.created_at,
                    record.created_at)
                self._put(f"tombstones/asset/{proposed.asset_id}/{proposed.revision}.json", tombstone)
            return proposed
        elif kind == "album":
            projected = catalog.get_album(str(mutation.entity_id))
            prefix = f"manifests/albums/{mutation.entity_id}/"
            canonical_heads = []
            for key in self.service.storage.keys(prefix):
                suffix = key.removeprefix(prefix).removesuffix(".json")
                if suffix.isdigit():
                    canonical_heads.append((int(suffix), key))
            current = None
            if canonical_heads:
                _, head_key = max(canonical_heads)
                head = decode_album_manifest(self.service.storage.read_bytes(head_key))
                if head.operation_id == operation_id:
                    return self._album_projection(head, mutation)
                current = self._album_projection(
                    head,
                    Mutation(
                        action="album.patch" if head.revision > 1 else "album.create",
                        entity_id=head.album_id,
                        changes={},
                        expected_revision=head.parent_revision,
                    ),
                )
            elif projected is not None:
                raise LibraryError(f"Canonical album manifest is missing: {mutation.entity_id}")
            if current is None and action != "create":
                raise FileNotFoundError("Entity not found")
            if current is not None and action == "create":
                raise LibraryError("Album already exists")
            if mutation.expected_revision is not None and (
                current is None or current.revision != mutation.expected_revision
            ):
                raise LibraryError("Revision changed; reload before editing")
            if current is not None and current.deleted_at and action not in {"restore", "delete"}:
                raise LibraryError("Unhide this item before editing")
            values = current.document() if current else {"name": "", "description": "", "assetIds": [], "deletedAt": None}
            values.update(mutation.changes)
            if action == "delete":
                values["deletedAt"] = values.get("deletedAt") or datetime.now(UTC).isoformat().replace("+00:00", "Z")
            elif action == "restore":
                values["deletedAt"] = None
            elif action not in {"create", "patch"}:
                raise LibraryError("Invalid album mutation")
            existing_assets = set(current.asset_ids) if current else set()
            for asset_id in values.get("assetIds", []):
                asset = self.service.canonical_asset(asset_id)
                if asset.deleted_at and UUID(str(asset_id)) not in existing_assets:
                    raise LibraryError(f"Unhide asset before adding it to an album: {asset_id}")
            revision = current.revision + 1 if current else 1
            if self.service.library_id is None:
                raise LibraryError("Canonical library identity is missing")
            record = self._album(current, self.service.library_id, mutation.entity_id, revision, operation_id, values)
            manifest_key = f"manifests/albums/{record.album_id}/{record.revision}.json"
            if self.service.storage.head(manifest_key) is not None:
                stored = decode_album_manifest(self.service.storage.read_bytes(manifest_key))
                if stored.operation_id != operation_id:
                    raise LibraryError("Immutable manifest revision conflicts with this operation")
                if action == "delete":
                    tombstone_key = f"tombstones/album/{record.album_id}/{record.revision}.json"
                    if self.service.storage.head(tombstone_key) is None:
                        tombstone = Tombstone(
                            self._library_id(), "album", record.album_id, record.revision,
                            record.revision - 1, operation_id,
                            stored.deleted_at or stored.created_at, stored.created_at,
                        )
                        self._put(tombstone_key, tombstone)
                return self._album_projection(stored, mutation)
            self._put(manifest_key, record)
            if action == "delete":
                tombstone = Tombstone(self._library_id(), "album", record.album_id, record.revision,
                    record.parent_revision or 1, operation_id, record.deleted_at or record.created_at, record.created_at)
                self._put(f"tombstones/album/{record.album_id}/{record.revision}.json", tombstone)
            return self._album_projection(record, mutation)

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
            library_id=self._library_id(), person_id=person_id, revision=revision,
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
            library_id=self._library_id(), person_id=person_id,
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
                self._library_id(), "person", person_id, revision,
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
                target_id = str(uuid5(self._library_id(), f"face-operation:{operation_id}"))
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

    def publish_ai_analysis(self, run_id: UUID, request: dict) -> dict:
        """Publish one AI run S3-first: result object, face records, person revisions.

        Every write is immutable and adoptive, and the run id is the
        deterministic identity of the result content, so a retry re-reads
        what the first attempt published and only fills the gaps. Faces
        whose record already exists are adopted verbatim — the record's
        person is the truth — and only the missing faces are clustered,
        against the current per-person centroid sums; a face matching
        nothing opens a new person with a deterministic id. Person
        revisions build on the head manifest, so a concurrent rename or
        merge is never regressed, and a revision is skipped (but still
        reported) when the head already contains all of the run's faces.
        The centroid read is best-effort and deliberately ordered late
        (after the result object is durable) to minimize the window in
        which a parallel run can skew clustering; a skewed assignment is
        recoverable through face merge, unlike a lost one.
        """
        library_id = self._library_id()
        result = request["result"]
        model = result.get("models", {}).get("semantic") or {}
        record, payload_bytes, built = build_processing_artifact(
            library_id,
            result,
            created_at=request["createdAt"],
            model_name=model.get("name"),
            model_version=model.get("digest"),
            source_object_key=request.get("sourceObjectKey"),
        )
        if built != str(run_id):
            raise LibraryError(f"AI run id does not match the result content: {run_id}")
        publish_ai_artifact(
            self.service.storage, self.service.publisher, record, payload_bytes
        )

        centroids = self.service.catalog.ai_centroids()
        people = self.service.catalog.all_people()
        if not centroids and not people:
            centroids, people = s3_face_state(self.service.storage)
        person_sums = {item["personId"]: list(item["sum"]) for item in centroids}
        snapshots = {item["personId"]: item for item in people}

        asset_id = request["assetId"]
        faces = request["faces"]
        assignments: dict[int, str] = {}
        stored_records: dict[int, FaceManifest] = {}
        pending: list[tuple[int, dict]] = []
        used: set[str] = set()
        for index, face in enumerate(faces):
            face_id = str(derive_face_id(library_id, asset_id, run_id, index))
            key = f"manifests/faces/{face_id}.json"
            if self.service.storage.head(key) is None:
                pending.append((index, face))
                continue
            record = decode_face_manifest(self.service.storage.read_bytes(key))
            person_id = str(record.person_id)
            assignments[index] = person_id
            vector = unit(record.embedding)
            person_sums.setdefault(person_id, [0.0] * len(vector))
            person_sums[person_id] = [
                a + b for a, b in zip(person_sums[person_id], vector, strict=True)
            ]
            used.add(person_id)
            stored_records[index] = record
        for index, person_id in cluster_faces(
            pending,
            person_sums,
            used,
            self.service.settings.face_match_threshold,
            lambda index: str(derive_person_id(library_id, run_id, index)),
        ):
            assignments[index] = person_id

        final_faces: list[dict] = []
        for index, face in enumerate(faces):
            face_id = str(derive_face_id(library_id, asset_id, run_id, index))
            record = stored_records.get(index)
            if record is None:
                record, _ = build_face_record(
                    library_id, face_id, asset_id, run_id, index, face, assignments[index]
                )
                self.service.publisher.publish_record(
                    f"manifests/faces/{face_id}.json", record, "face"
                )
            box = record.bounding_box
            final_faces.append(
                {
                    "faceId": face_id,
                    "personId": assignments[index],
                    "faceIndex": index,
                    "box": [box["x"], box["y"], box["width"], box["height"]],
                    "confidence": record.confidence,
                    "embedding": record.embedding,
                }
            )

        new_faces: dict[str, list[str]] = {}
        for face in final_faces:
            new_faces.setdefault(face["personId"], []).append(face["faceId"])
        person_revisions: list[dict] = []
        for person_id in sorted(new_faces):
            added = set(new_faces[person_id])
            revision = self._person_revision(UUID(person_id))
            head = None
            tombstoned = False
            if revision:
                candidate = decode_person_manifest(
                    self.service.storage.read_bytes(
                        f"manifests/people/{person_id}/{revision}.json"
                    )
                )
                if candidate.deleted_at is None:
                    head = candidate
                else:
                    tombstoned = True
            if head is not None:
                display_name = head.display_name
                created_at = head.created_at
                base = {str(face_id) for face_id in head.face_ids}
            else:
                snapshot = snapshots.get(person_id)
                display_name = snapshot["displayName"] if snapshot else ""
                # The codec requires Z-normalized RFC 3339 timestamps; the
                # request carries +00:00-suffixed wall clock, so normalize
                # at write time (a no-op for Z values).
                created_at = _utc(
                    (snapshot["createdAt"] if snapshot else "") or request["createdAt"]
                )
                base = set(snapshot["faceIds"]) if snapshot else set()
            new_set = sorted(base | added)
            if head is not None:
                if not added <= base:
                    self._put(
                        f"manifests/people/{person_id}/{revision + 1}.json",
                        PersonManifest(
                            library_id=library_id,
                            person_id=UUID(person_id),
                            revision=revision + 1,
                            parent_revision=revision or None,
                            operation_id=run_id,
                            created_at=created_at,
                            display_name=display_name,
                            face_ids=tuple(UUID(face_id) for face_id in new_set),
                            deleted_at=None,
                        ),
                    )
            elif not tombstoned:
                # A person without any head opens at revision 1; a
                # tombstoned one is never revived by an AI run.
                self._put(
                    f"manifests/people/{person_id}/{revision + 1}.json",
                    PersonManifest(
                        library_id=library_id,
                        person_id=UUID(person_id),
                        revision=revision + 1,
                        parent_revision=revision or None,
                        operation_id=run_id,
                        created_at=created_at,
                        display_name=display_name,
                        face_ids=tuple(UUID(face_id) for face_id in new_set),
                        deleted_at=None,
                    ),
                )
            person_revisions.append(
                {
                    "personId": person_id,
                    "displayName": display_name,
                    "createdAt": created_at,
                    "faceIds": new_set,
                }
            )

        return {
            "assetId": request["assetId"],
            "createdAt": request["createdAt"],
            "sourceObjectKey": request.get("sourceObjectKey"),
            "reusePolicyVersion": request.get("reusePolicyVersion"),
            "result": result,
            "faces": final_faces,
            "personRevisions": person_revisions,
        }
