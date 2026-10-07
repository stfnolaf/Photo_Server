"""Phase 5 export of the PostgreSQL projection into the canonical S3 namespace.

The exporter intentionally uses the existing SQLAlchemy tables instead of the
service API.  This keeps the live read/write path untouched and makes the
operation safe to run in maintenance mode while PostgreSQL remains authority.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid5

from sqlalchemy import select

from photo_server.catalog import (
    album_assets,
    albums,
    analysis_runs,
    assets,
    blobs,
    faces,
    people,
)
from photo_server.config import LibraryError
from photo_server.dual_write import DualWriteIntegrityError
from photo_server.manifests import (
    AlbumManifest,
    AssetManifest,
    BlobReference,
    FaceManifest,
    PersonManifest,
    ProcessingArtifact,
    ProcessingReference,
    Tombstone,
    UserState,
    decode,
    encode,
)
from photo_server.manifests.codec import ManifestCodecError

BACKFILL_NAMESPACE = UUID("9a9efc2a-bc84-5e4e-bb39-8d4cfe6a8d7d")


@dataclass
class BackfillReport:
    status: str = "complete"
    scanned: int = 0
    exported: int = 0
    skipped: int = 0
    objects_exported: int = 0
    objects_verified: int = 0
    checkpoint: str | None = None
    unresolved: list[dict[str, str]] = field(default_factory=list)
    conflicting: list[dict[str, str]] = field(default_factory=list)
    failed: list[dict[str, str]] = field(default_factory=list)
    malformed: list[dict[str, str]] = field(default_factory=list)
    verified: int = 0

    @property
    def errors(self) -> list[dict[str, str]]:
        return self.unresolved + self.conflicting + self.failed + self.malformed

    def as_dict(self) -> dict[str, Any]:
        counts = {
            "scanned": self.scanned,
            "exported": self.exported,
            "skipped": self.skipped,
            "objectsExported": self.objects_exported,
            "objectsVerified": self.objects_verified,
            "unresolved": len(self.unresolved),
            "conflicting": len(self.conflicting),
            "malformed": len(self.malformed),
            "failed": len(self.failed),
            "verified": self.verified,
            "conflictsByCategory": {
                "conflictingOperationIds": sum(
                    x.get("category") == "conflicting-operation-id" for x in self.conflicting
                ),
                "duplicateRevisions": sum(
                    x.get("category") == "duplicate-revision" for x in self.conflicting
                ),
                "immutableObjectConflicts": sum(
                    x.get("category") == "immutable-object" for x in self.conflicting
                ),
                "immutableManifestConflicts": sum(
                    x.get("category") == "immutable-manifest" for x in self.conflicting
                ),
                "checksumMismatches": sum(
                    x.get("category") == "checksum-mismatch" for x in self.conflicting
                ),
                "sizeMismatches": sum(
                    x.get("category") == "size-mismatch" for x in self.conflicting
                ),
                "malformedPostgresRows": len(self.malformed),
                "unresolvedReferences": len(self.unresolved),
            },
        }
        return {
            "status": "failed" if self.errors else self.status,
            "scanned": self.scanned,
            "exported": self.exported,
            "skipped": self.skipped,
            "objectsExported": self.objects_exported,
            "objectsVerified": self.objects_verified,
            "checkpoint": self.checkpoint,
            "unresolved": self.unresolved,
            "conflicting": self.conflicting,
            "failed": self.failed,
            "malformed": self.malformed,
            "errors": self.errors,
            "counts": counts,
        }


def _timestamp(value: Any, *, fallback: str | None = None) -> str:
    if value is None:
        value = fallback or datetime.now(UTC)
    if isinstance(value, datetime):
        value = value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, str) and value.endswith("+00:00"):
        value = value[:-6] + "Z"
    if not isinstance(value, str):
        raise ValueError("timestamp is not a string")
    return value


def _uuid(value: Any, name: str) -> UUID:
    result = UUID(str(value))
    if str(result) != str(value):
        raise ValueError(f"{name} is not a canonical UUID")
    return result


def _operation(value: Any, entity: UUID, revision: int) -> UUID:
    if value:
        return _uuid(value, "operationId")
    return uuid5(BACKFILL_NAMESPACE, f"{entity}:{revision}")


def asset_manifest_from_row(asset_row: Any, blob_rows: list[Any], processing=()) -> AssetManifest:
    """Convert a current ``assets`` row and its blob rows to the Phase 2 model."""
    raw = asset_row["manifest"] if isinstance(asset_row, dict) else asset_row.manifest
    if not isinstance(raw, dict):
        raise ValueError("assets.manifest must be a JSON object")
    asset_id = _uuid(asset_row["id"] if isinstance(asset_row, dict) else asset_row.id, "assetId")
    revision = int(
        asset_row["state_revision"] if isinstance(asset_row, dict) else asset_row.state_revision
    )
    user = raw.get("userState", {})
    location = user.get("location")
    state = UserState(
        rating=int(
            user.get(
                "rating",
                asset_row.get("rating", 0) if isinstance(asset_row, dict) else asset_row.rating,
            )
        ),
        favorite=bool(
            user.get(
                "favorite",
                asset_row.get("favorite", False)
                if isinstance(asset_row, dict)
                else asset_row.favorite,
            )
        ),
        caption=str(user.get("caption", "")),
        keywords=tuple(user.get("keywords", [])),
        location=None
        if location is None
        else __import__("photo_server.manifests", fromlist=["Location"]).Location(
            str(location["name"]), float(location["latitude"]), float(location["longitude"])
        ),
    )
    refs = tuple(
        BlobReference(
            blob_id=_uuid(row["id"], "blobId"),
            role=row["role"],
            object_key=f"objects/{row['sha256']}",
            original_filename=row["original_filename"],
            sha256=row["sha256"],
            size_bytes=int(row["size_bytes"]),
            mime_type=row["mime_type"] or "application/octet-stream",
        )
        for row in sorted(blob_rows, key=lambda item: str(item["id"]))
    )
    primary = raw.get("primaryBlobId") or (str(refs[0].blob_id) if refs else None)
    if not refs or primary is None:
        raise ValueError("asset has no canonical blob")
    return AssetManifest(
        library_id=_uuid(raw.get("libraryId"), "libraryId"),
        asset_id=asset_id,
        revision=revision,
        parent_revision=None if revision == 1 else revision - 1,
        operation_id=_operation(raw.get("operationId"), asset_id, revision),
        created_at=_timestamp(raw.get("createdAt"), fallback=raw.get("importedAt")),
        imported_at=_timestamp(raw.get("importedAt")),
        capture_time=raw.get("captureTime"),
        blobs=refs,
        primary_blob_id=_uuid(primary, "primaryBlobId"),
        extracted_metadata=raw.get("extractedMetadata", raw.get("metadata", {})),
        user_state=state,
        deleted_at=None if raw.get("deletedAt") is None else _timestamp(raw["deletedAt"]),
        processing=tuple(processing),
    )


def tombstone_from_row(
    library_id: UUID, entity_type: str, entity_id: UUID, revision: int, deleted_at: Any
) -> Tombstone:
    deleted = _timestamp(deleted_at)
    return Tombstone(
        library_id=library_id,
        entity_type=entity_type,
        entity_id=entity_id,
        revision=revision + 1,
        parent_revision=revision,
        operation_id=uuid5(BACKFILL_NAMESPACE, f"delete:{entity_type}:{entity_id}:{revision}"),
        deleted_at=deleted,
        created_at=deleted,
    )


def _immutable_put(
    storage, key: str, body: bytes, mime: str, report: BackfillReport, dry_run: bool
) -> None:
    existing = storage.head(key)
    digest = hashlib.sha256(body).hexdigest()
    if existing is not None:
        if existing.get("ContentLength") != len(body):
            raise DualWriteIntegrityError(f"existing immutable key has a different size: {key}")
        try:
            storage.verify(key, len(body), digest)
        except (AssertionError, ValueError, KeyError, LibraryError) as error:
            raise DualWriteIntegrityError(
                f"existing immutable key has different bytes: {key}"
            ) from error
        report.skipped += 1
        report.verified += 1
        return
    if not dry_run:
        storage.put(key, body, mime)
        try:
            storage.verify(key, len(body), digest)
        except (AssertionError, ValueError, KeyError, LibraryError) as error:
            raise DualWriteIntegrityError(
                f"written immutable key failed verification: {key}"
            ) from error
        report.objects_exported += int(key.startswith("objects/"))
    report.objects_verified += 1
    report.verified += 1


def _copy_object(
    storage, source_key: str, digest: str, size: int, report: BackfillReport, dry_run: bool
) -> None:
    head = storage.head(source_key)
    if head is None and source_key != f"objects/{digest}":
        source_key = f"objects/{digest}"
        head = storage.head(source_key)
    if head is None:
        raise FileNotFoundError(f"missing referenced object: {source_key}")
    if head.get("ContentLength") != size:
        raise ValueError(f"object size mismatch: {source_key}")
    data = storage.read_bytes(source_key)
    if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
        raise ValueError(f"object checksum mismatch: {source_key}")
    _immutable_put(storage, f"objects/{digest}", data, "application/octet-stream", report, dry_run)


def _conflict_category(key: str, reason: str) -> str:
    if "size" in reason.lower():
        return "size-mismatch"
    if "checksum" in reason.lower() or "bytes" in reason.lower():
        return "checksum-mismatch"
    return "immutable-manifest" if key.startswith("manifests/") else "immutable-object"


def _processing_references(
    connection,
    storage,
    asset_id: str,
    blob_digests: set[str],
    report: BackfillReport,
    dry_run: bool,
):
    refs = []
    rows = connection.execute(
        select(analysis_runs)
        .where(analysis_runs.c.asset_id == asset_id, analysis_runs.c.is_current.is_(True))
        .order_by(analysis_runs.c.analysis_type, analysis_runs.c.id)
    ).mappings()
    for row in rows:
        input_digest = row["input_hash"]
        if input_digest not in blob_digests:
            report.unresolved.append(
                {
                    "category": "processing",
                    "key": str(row["id"]),
                    "reason": "processing input does not reference an asset blob",
                }
            )
            continue
        source = row["object_key"]
        head = storage.head(source)
        if head is None:
            report.unresolved.append(
                {
                    "category": "processing",
                    "key": source,
                    "reason": "missing processing artifact object",
                }
            )
            continue
        data = storage.read_bytes(source)
        digest = hashlib.sha256(data).hexdigest()
        _copy_object(storage, source, digest, len(data), report, dry_run)
        try:
            run_id = _uuid(row["id"], "analysisRunId")
            artifact = ProcessingArtifact(
                asset_id=_uuid(asset_id, "assetId"),
                artifact_id=run_id,
                processing_type=row["analysis_type"],
                pipeline_version=row["pipeline_version"],
                input_sha256=input_digest,
                implementation_version=row["pipeline_version"],
                result_object=BlobReference(
                    UUID(int=0),
                    "SIDECAR",
                    f"objects/{digest}",
                    "analysis-result.json",
                    digest,
                    len(data),
                    "application/json",
                ),
                created_at=_timestamp(row["created_at"]),
                model_name=row["model_name"],
                model_version=row["model_version"],
                source_object_key=row["object_key"],
            )
            artifact_key = f"manifests/processing/{run_id}.json"
            artifact_body = encode(artifact)
            _immutable_put(
                storage, artifact_key, artifact_body, "application/json", report, dry_run
            )
        except (KeyError, TypeError, ValueError, ManifestCodecError) as error:
            report.malformed.append(
                {"category": "analysis-run", "key": str(row.get("id")), "reason": str(error)}
            )
            continue
        refs.append(
            ProcessingReference(
                artifact_key=f"objects/{digest}",
                artifact_sha256=digest,
                input_sha256=input_digest,
                processing_type=row["analysis_type"],
                implementation_version=row["pipeline_version"],
            )
        )
    return tuple(refs)


def backfill_postgres_to_s3(
    catalog, storage, *, checkpoint_id="backfill", resume=True, dry_run=False, stop_after=None
) -> dict[str, Any]:
    """Export canonical PostgreSQL state in deterministic order.

    A checkpoint is written only after a record has been fully verified and
    published.  Consequently a process kill repeats at most one record and is
    safe because all target keys are immutable.
    """
    report = BackfillReport()
    checkpoint_key = f"indexes/checkpoints/{checkpoint_id}.json"
    cursor = None
    if resume and not dry_run and storage.head(checkpoint_key):
        checkpoint = storage.get_json(checkpoint_key)
        if checkpoint.get("complete"):
            resume = False
        else:
            cursor = checkpoint.get("lastKey")
    with catalog.engine.connect() as connection:
        library_id = connection.scalar(
            select(
                __import__("photo_server.catalog", fromlist=["library"]).library.c.library_id
            ).where(
                __import__("photo_server.catalog", fromlist=["library"]).library.c.singleton == 1
            )
        )
        if not library_id:
            report.failed.append({"record": "library", "reason": "missing library identity"})
            return report.as_dict()
        library_uuid = _uuid(library_id, "libraryId")
        asset_rows = list(connection.execute(select(assets).order_by(assets.c.id)).mappings())
        album_rows = list(connection.execute(select(albums).order_by(albums.c.id)).mappings())
        person_rows = list(connection.execute(select(people).order_by(people.c.id)).mappings())
        known_asset_ids = {row["id"] for row in asset_rows}
        known_person_ids = {row["id"] for row in person_rows}
        face_rows = list(connection.execute(select(faces).order_by(faces.c.id)).mappings())
        known_face_ids = {row["id"] for row in face_rows}
        for face in face_rows:
            if face["asset_id"] not in known_asset_ids:
                report.unresolved.append(
                    {
                        "category": "face",
                        "key": str(face["id"]),
                        "reason": "face references missing asset",
                    }
                )
            if face["person_id"] not in known_person_ids:
                report.unresolved.append(
                    {
                        "category": "face",
                        "key": str(face["id"]),
                        "reason": "face references missing person",
                    }
                )
        known_run_ids = {
            row["id"] for row in connection.execute(select(analysis_runs.c.id)).mappings()
        }
        for face in face_rows:
            if face["analysis_run_id"] not in known_run_ids:
                report.unresolved.append(
                    {
                        "category": "face",
                        "key": str(face["id"]),
                        "reason": "face references missing analysis-run",
                    }
                )
        records = [
            (f"manifests/assets/{row['id']}/{row['state_revision']}.json", "asset", row)
            for row in asset_rows
        ]
        records += [
            (f"manifests/albums/{row['id']}/{row['state_revision']}.json", "album", row)
            for row in album_rows
        ]
        records += [(f"manifests/people/{row['id']}/1.json", "person", row) for row in person_rows]
        records += [(f"manifests/faces/{row['id']}.json", "face", row) for row in face_rows]
        operations: dict[tuple[str, tuple[str, str]], bytes] = {}
        for key, kind, row in sorted(records, key=lambda value: value[0]):
            if cursor and key <= cursor:
                continue
            report.scanned += 1
            try:
                if kind == "asset":
                    blob_rows = list(
                        connection.execute(
                            select(blobs).where(blobs.c.asset_id == row["id"]).order_by(blobs.c.id)
                        ).mappings()
                    )
                    for blob in blob_rows:
                        _copy_object(
                            storage,
                            blob["object_key"],
                            blob["sha256"],
                            int(blob["size_bytes"]),
                            report,
                            dry_run,
                        )
                    processing = _processing_references(
                        connection,
                        storage,
                        row["id"],
                        {blob["sha256"] for blob in blob_rows},
                        report,
                        dry_run,
                    )
                    record = asset_manifest_from_row(row, blob_rows, processing)
                    kind_for_codec = "asset"
                elif kind == "album":
                    state = dict(row["state"])
                    memberships = list(
                        connection.execute(
                            select(album_assets)
                            .where(album_assets.c.album_id == row["id"])
                            .order_by(album_assets.c.position, album_assets.c.asset_id)
                        ).mappings()
                    )
                    record = AlbumManifest(
                        library_id=library_uuid,
                        album_id=_uuid(row["id"], "albumId"),
                        revision=int(row["state_revision"]),
                        parent_revision=None
                        if int(row["state_revision"]) == 1
                        else int(row["state_revision"]) - 1,
                        operation_id=_operation(
                            state.get("operationId"),
                            _uuid(row["id"], "albumId"),
                            int(row["state_revision"]),
                        ),
                        # The live Album projection has no created-at column;
                        # use a stable contract fallback so repeated exports
                        # produce identical immutable bytes.
                        created_at=_timestamp(
                            state.get("createdAt"), fallback="1970-01-01T00:00:00Z"
                        ),
                        name=state.get("name", ""),
                        description=state.get("description", ""),
                        asset_ids=tuple(_uuid(x["asset_id"], "assetId") for x in memberships),
                        deleted_at=None
                        if row["deleted_at"] is None
                        else _timestamp(row["deleted_at"]),
                    )
                    missing = [
                        str(asset_id)
                        for asset_id in record.asset_ids
                        if str(asset_id) not in known_asset_ids
                    ]
                    if missing:
                        report.unresolved.append(
                            {
                                "category": "album",
                                "key": key,
                                "reason": f"album references missing assets: {missing}",
                            }
                        )
                    kind_for_codec = "album"
                else:
                    if kind == "face":
                        record = FaceManifest(
                            library_id=library_uuid,
                            face_id=_uuid(row["id"], "faceId"),
                            asset_id=_uuid(row["asset_id"], "assetId"),
                            analysis_run_id=_uuid(row["analysis_run_id"], "analysisRunId"),
                            person_id=_uuid(row["person_id"], "personId"),
                            face_index=int(row["face_index"]),
                            bounding_box=row["bounding_box"],
                            confidence=float(row["confidence"]),
                            embedding=list(row["embedding"]),
                        )
                        kind_for_codec = "face"
                    else:
                        person_id = _uuid(row["id"], "personId")
                        record = PersonManifest(
                            library_id=library_uuid,
                            person_id=person_id,
                            revision=1,
                            parent_revision=None,
                            operation_id=uuid5(BACKFILL_NAMESPACE, f"person:{person_id}:1"),
                            created_at=_timestamp(row["created_at"]),
                            display_name=row["display_name"] or "",
                            face_ids=tuple(
                                _uuid(face["id"], "faceId")
                                for face in connection.execute(
                                    select(faces.c.id)
                                    .where(faces.c.person_id == row["id"])
                                    .order_by(faces.c.id)
                                ).mappings()
                            ),
                            deleted_at=None,
                        )
                        missing_faces = [
                            str(face_id)
                            for face_id in record.face_ids
                            if str(face_id) not in known_face_ids
                        ]
                        if missing_faces:
                            report.unresolved.append(
                                {
                                    "category": "person",
                                    "key": key,
                                    "reason": f"person references missing faces: {missing_faces}",
                                }
                            )
                        kind_for_codec = "person"
                body = encode(record)
                decode(body, kind_for_codec)
                operation_id = str(
                    getattr(
                        record, "operation_id", uuid5(BACKFILL_NAMESPACE, f"{kind}:{row['id']}")
                    )
                )
                operation_scope = (kind, str(row["id"]))
                prior = operations.get((operation_id, operation_scope))
                if prior is not None and prior != body:
                    report.conflicting.append(
                        {
                            "category": "conflicting-operation-id",
                            "key": key,
                            "reason": f"conflicting operation ID: {operation_id}",
                        }
                    )
                operations[(operation_id, operation_scope)] = body
                _immutable_put(storage, key, body, "application/json", report, dry_run)
                report.exported += 1
                deleted = row["deleted_at"] if kind in {"asset", "album"} else None
                if deleted is not None:
                    tombstone = tombstone_from_row(
                        library_uuid,
                        kind,
                        _uuid(row["id"], f"{kind}Id"),
                        int(row["state_revision"]),
                        deleted,
                    )
                    tomb_key = f"tombstones/{kind}/{row['id']}/{tombstone.revision}.json"
                    tomb_body = encode(tombstone)
                    decode(tomb_body, "tombstone")
                    _immutable_put(
                        storage, tomb_key, tomb_body, "application/json", report, dry_run
                    )
                report.checkpoint = key
                if not dry_run:
                    checkpoint = {"schemaVersion": 1, "lastKey": key, "complete": False}
                    if hasattr(storage, "put_json_mutable"):
                        storage.put_json_mutable(checkpoint_key, checkpoint)
                    else:
                        storage.put_json(checkpoint_key, checkpoint)
                if stop_after is not None and report.scanned >= stop_after:
                    report.status = "paused"
                    return report.as_dict()
            except ManifestCodecError as error:
                report.malformed.append({"key": key, "reason": str(error)})
            except (KeyError, TypeError, ValueError) as error:
                report.malformed.append({"key": key, "reason": str(error)})
            except FileNotFoundError as error:
                report.unresolved.append({"category": kind, "key": key, "reason": str(error)})
            except DualWriteIntegrityError as error:
                reason = str(error)
                report.conflicting.append(
                    {"category": _conflict_category(key, reason), "key": key, "reason": reason}
                )
    if not dry_run:
        checkpoint = {"schemaVersion": 1, "lastKey": report.checkpoint, "complete": True}
        if hasattr(storage, "put_json_mutable"):
            storage.put_json_mutable(checkpoint_key, checkpoint)
        else:
            storage.put_json(checkpoint_key, checkpoint)
    return report.as_dict()
