"""Immutable models for the version 1 canonical manifest records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from .validation import (
    ALBUM_FIELDS,
    ARTIFACT_FIELDS,
    ASSET_FIELDS,
    BLOB_FIELDS,
    FACE_FIELDS,
    PERSON_FIELDS,
    PROCESSING_FIELDS,
    TOMBSTONE_FIELDS,
    USER_STATE_FIELDS,
    check_common,
    check_fields,
    check_sha,
    check_timestamp,
    check_uuid,
    require_int,
    require_str,
)


@dataclass(frozen=True)
class Location:
    name: str
    latitude: float
    longitude: float

    @classmethod
    def from_dict(cls, value: Any) -> "Location":
        check_fields(value, {"name", "latitude", "longitude"}, "location")
        if not isinstance(value["name"], str):
            raise ValueError("location.name must be a string")
        if (
            isinstance(value["latitude"], bool)
            or not isinstance(value["latitude"], (int, float))
            or not -90 <= value["latitude"] <= 90
        ):
            raise ValueError("location.latitude must be between -90 and 90")
        if (
            isinstance(value["longitude"], bool)
            or not isinstance(value["longitude"], (int, float))
            or not -180 <= value["longitude"] <= 180
        ):
            raise ValueError("location.longitude must be between -180 and 180")
        return cls(value["name"], value["latitude"], value["longitude"])

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "latitude": self.latitude, "longitude": self.longitude}


@dataclass(frozen=True)
class UserState:
    rating: int
    favorite: bool
    caption: str
    keywords: tuple[str, ...]
    location: Location | None

    @classmethod
    def from_dict(cls, value: Any) -> "UserState":
        check_fields(value, USER_STATE_FIELDS, "userState")
        rating = value["rating"]
        if isinstance(rating, bool) or not isinstance(rating, int) or rating < 0:
            raise ValueError("userState.rating must be a non-negative integer")
        if not 0 <= rating <= 5:
            raise ValueError("userState.rating must be between 0 and 5")
        if not isinstance(value["favorite"], bool):
            raise ValueError("userState.favorite must be a boolean")
        if not isinstance(value["caption"], str):
            raise ValueError("userState.caption must be a string")
        caption = value["caption"]
        keywords = value["keywords"]
        if (
            not isinstance(keywords, list)
            or any(not isinstance(x, str) or not x for x in keywords)
            or len(set(keywords)) != len(keywords)
        ):
            raise ValueError("userState.keywords must contain unique non-empty strings")
        location = None if value["location"] is None else Location.from_dict(value["location"])
        return cls(rating, value["favorite"], caption, tuple(keywords), location)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rating": self.rating,
            "favorite": self.favorite,
            "caption": self.caption,
            "keywords": list(self.keywords),
            "location": self.location.to_dict() if self.location else None,
        }


@dataclass(frozen=True)
class BlobReference:
    blob_id: UUID
    role: str
    object_key: str
    original_filename: str
    sha256: str
    size_bytes: int
    mime_type: str

    @classmethod
    def from_dict(cls, value: Any) -> "BlobReference":
        check_fields(value, BLOB_FIELDS, "blob")
        role = require_str(value["role"], "blob.role")
        if role not in {"ORIGINAL_RAW", "ORIGINAL_JPEG", "ORIGINAL_HEIF", "SIDECAR"}:
            raise ValueError("invalid blob role")
        object_key = require_str(value["objectKey"], "blob.objectKey")
        checksum = check_sha(value["sha256"], "blob.sha256")
        if object_key != f"objects/{checksum}":
            raise ValueError("blob objectKey must be the content-addressed objects/<sha256> key")
        return cls(
            check_uuid(value["blobId"], "blob.blobId"),
            role,
            object_key,
            require_str(value["originalFilename"], "blob.originalFilename"),
            checksum,
            require_int(value["sizeBytes"], "blob.sizeBytes"),
            require_str(value["mimeType"], "blob.mimeType"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "blobId": str(self.blob_id),
            "role": self.role,
            "objectKey": self.object_key,
            "originalFilename": self.original_filename,
            "sha256": self.sha256,
            "sizeBytes": self.size_bytes,
            "mimeType": self.mime_type,
        }


@dataclass(frozen=True)
class ProcessingReference:
    artifact_key: str
    artifact_sha256: str
    input_sha256: str
    processing_type: str
    implementation_version: str

    @classmethod
    def from_dict(cls, value: Any) -> "ProcessingReference":
        check_fields(value, PROCESSING_FIELDS, "processing reference")
        return cls(
            require_str(value["artifactKey"], "artifactKey"),
            check_sha(value["artifactSha256"], "artifactSha256"),
            check_sha(value["inputSha256"], "inputSha256"),
            require_str(value["processingType"], "processingType"),
            require_str(value["implementationVersion"], "implementationVersion"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifactKey": self.artifact_key,
            "artifactSha256": self.artifact_sha256,
            "inputSha256": self.input_sha256,
            "processingType": self.processing_type,
            "implementationVersion": self.implementation_version,
        }


@dataclass(frozen=True)
class AssetManifest:
    library_id: UUID
    asset_id: UUID
    revision: int
    parent_revision: int | None
    operation_id: UUID
    created_at: str
    imported_at: str
    capture_time: str | None
    blobs: tuple[BlobReference, ...]
    primary_blob_id: UUID
    extracted_metadata: dict[str, Any]
    user_state: UserState
    deleted_at: str | None
    processing: tuple[ProcessingReference, ...]
    schema_version: int = 1

    @classmethod
    def from_dict(cls, v: Any) -> "AssetManifest":
        check_fields(v, ASSET_FIELDS, "asset manifest")
        check_common(v, "asset manifest")
        blobs = tuple(BlobReference.from_dict(x) for x in v["blobs"])
        if (
            not blobs
            or len({x.blob_id for x in blobs}) != len(blobs)
            or len({x.sha256 for x in blobs}) != len(blobs)
            or len({x.object_key for x in blobs}) != len(blobs)
        ):
            raise ValueError("asset blobs must have unique IDs, checksums, and object keys")
        primary = check_uuid(v["primaryBlobId"], "primaryBlobId")
        by_id = {x.blob_id: x for x in blobs}
        if primary not in by_id or by_id[primary].role == "SIDECAR":
            raise ValueError("primary blob must be a media original")
        metadata = v["extractedMetadata"]
        if not isinstance(metadata, dict):
            raise ValueError("extractedMetadata must be an object")
        processing = tuple(ProcessingReference.from_dict(x) for x in v["processing"])
        if len({x.artifact_key for x in processing}) != len(processing) or len(
            {x.artifact_sha256 for x in processing}
        ) != len(processing):
            raise ValueError("duplicate processing reference")
        blob_hashes = {x.sha256 for x in blobs}
        if any(x.input_sha256 not in blob_hashes for x in processing):
            raise ValueError("processing input checksum is not an asset blob")
        return cls(
            check_uuid(v["libraryId"], "libraryId"),
            check_uuid(v["assetId"], "assetId"),
            v["revision"],
            v["parentRevision"],
            check_uuid(v["operationId"], "operationId"),
            check_timestamp(v["createdAt"], "createdAt"),
            check_timestamp(v["importedAt"], "importedAt"),
            None if v["captureTime"] is None else check_timestamp(v["captureTime"], "captureTime"),
            blobs,
            primary,
            metadata,
            UserState.from_dict(v["userState"]),
            None if v["deletedAt"] is None else check_timestamp(v["deletedAt"], "deletedAt"),
            processing,
        )

    def to_dict(self):
        return {
            "schemaVersion": 1,
            "libraryId": str(self.library_id),
            "assetId": str(self.asset_id),
            "revision": self.revision,
            "parentRevision": self.parent_revision,
            "operationId": str(self.operation_id),
            "createdAt": self.created_at,
            "importedAt": self.imported_at,
            "captureTime": self.capture_time,
            "blobs": [x.to_dict() for x in self.blobs],
            "primaryBlobId": str(self.primary_blob_id),
            "extractedMetadata": self.extracted_metadata,
            "userState": self.user_state.to_dict(),
            "deletedAt": self.deleted_at,
            "processing": [x.to_dict() for x in self.processing],
        }


@dataclass(frozen=True)
class AlbumManifest:
    library_id: UUID
    album_id: UUID
    revision: int
    parent_revision: int | None
    operation_id: UUID
    created_at: str
    name: str
    description: str
    asset_ids: tuple[UUID, ...]
    deleted_at: str | None
    schema_version: int = 1

    @classmethod
    def from_dict(cls, v):
        check_fields(v, ALBUM_FIELDS, "album manifest")
        check_common(v, "album manifest")
        ids = tuple(check_uuid(x, "assetIds") for x in v["assetIds"])
        if len(set(ids)) != len(ids):
            raise ValueError("album membership must be unique")
        return cls(
            check_uuid(v["libraryId"], "libraryId"),
            check_uuid(v["albumId"], "albumId"),
            v["revision"],
            v["parentRevision"],
            check_uuid(v["operationId"], "operationId"),
            check_timestamp(v["createdAt"], "createdAt"),
            require_str(v["name"], "name"),
            v["description"] if isinstance(v["description"], str) else require_str(v["description"], "description"),
            ids,
            None if v["deletedAt"] is None else check_timestamp(v["deletedAt"], "deletedAt"),
        )

    def to_dict(self):
        return {
            "schemaVersion": 1,
            "libraryId": str(self.library_id),
            "albumId": str(self.album_id),
            "revision": self.revision,
            "parentRevision": self.parent_revision,
            "operationId": str(self.operation_id),
            "createdAt": self.created_at,
            "name": self.name,
            "description": self.description,
            "assetIds": [str(x) for x in self.asset_ids],
            "deletedAt": self.deleted_at,
        }


@dataclass(frozen=True)
class PersonManifest:
    library_id: UUID
    person_id: UUID
    revision: int
    parent_revision: int | None
    operation_id: UUID
    created_at: str
    display_name: str
    face_ids: tuple[UUID, ...]
    deleted_at: str | None
    schema_version: int = 1

    @classmethod
    def from_dict(cls, v):
        check_fields(v, PERSON_FIELDS, "person manifest")
        check_common(v, "person manifest")
        face_ids = tuple(check_uuid(x, "faceIds") for x in v["faceIds"])
        if len(set(face_ids)) != len(face_ids):
            raise ValueError("person face assignments must be unique")
        display_name = v["displayName"]
        if not isinstance(display_name, str):
            raise ValueError("displayName must be a string")
        return cls(
            check_uuid(v["libraryId"], "libraryId"),
            check_uuid(v["personId"], "personId"),
            v["revision"],
            v["parentRevision"],
            check_uuid(v["operationId"], "operationId"),
            check_timestamp(v["createdAt"], "createdAt"),
            display_name,
            face_ids,
            None if v["deletedAt"] is None else check_timestamp(v["deletedAt"], "deletedAt"),
        )

    def to_dict(self):
        return {
            "schemaVersion": 1,
            "libraryId": str(self.library_id),
            "personId": str(self.person_id),
            "revision": self.revision,
            "parentRevision": self.parent_revision,
            "operationId": str(self.operation_id),
            "createdAt": self.created_at,
            "displayName": self.display_name,
            "faceIds": [str(x) for x in self.face_ids],
            "deletedAt": self.deleted_at,
        }


@dataclass(frozen=True)
class FaceManifest:
    library_id: UUID
    face_id: UUID
    asset_id: UUID
    analysis_run_id: UUID
    person_id: UUID
    face_index: int
    bounding_box: dict[str, Any]
    confidence: float
    embedding: list[float]
    schema_version: int = 1

    @classmethod
    def from_dict(cls, v):
        check_fields(v, FACE_FIELDS, "face manifest")
        if v["schemaVersion"] != 1:
            raise ValueError("face manifest has unsupported schemaVersion")
        if (
            isinstance(v["faceIndex"], bool)
            or not isinstance(v["faceIndex"], int)
            or v["faceIndex"] < 0
        ):
            raise ValueError("faceIndex must be a non-negative integer")
        if not isinstance(v["boundingBox"], dict) or not isinstance(v["embedding"], list):
            raise ValueError("face geometry is malformed")
        if not isinstance(v["confidence"], (int, float)) or isinstance(v["confidence"], bool):
            raise ValueError("confidence must be numeric")
        return cls(
            check_uuid(v["libraryId"], "libraryId"),
            check_uuid(v["faceId"], "faceId"),
            check_uuid(v["assetId"], "assetId"),
            check_uuid(v["analysisRunId"], "analysisRunId"),
            check_uuid(v["personId"], "personId"),
            v["faceIndex"],
            v["boundingBox"],
            float(v["confidence"]),
            v["embedding"],
        )

    def to_dict(self):
        return {
            "schemaVersion": 1,
            "libraryId": str(self.library_id),
            "faceId": str(self.face_id),
            "assetId": str(self.asset_id),
            "analysisRunId": str(self.analysis_run_id),
            "personId": str(self.person_id),
            "faceIndex": self.face_index,
            "boundingBox": self.bounding_box,
            "confidence": self.confidence,
            "embedding": self.embedding,
        }


@dataclass(frozen=True)
class Tombstone:
    library_id: UUID
    entity_type: str
    entity_id: UUID
    revision: int
    parent_revision: int
    operation_id: UUID
    deleted_at: str
    created_at: str
    schema_version: int = 1

    @classmethod
    def from_dict(cls, v):
        check_fields(v, TOMBSTONE_FIELDS, "tombstone")
        check_common(v, "tombstone")
        if v["entityType"] not in {"asset", "album", "person"}:
            raise ValueError("invalid tombstone entityType")
        parent = require_int(v["parentRevision"], "parentRevision")
        if v["revision"] != parent + 1:
            raise ValueError("tombstone revision must follow its parent")
        return cls(
            check_uuid(v["libraryId"], "libraryId"),
            v["entityType"],
            check_uuid(v["entityId"], "entityId"),
            v["revision"],
            parent,
            check_uuid(v["operationId"], "operationId"),
            check_timestamp(v["deletedAt"], "deletedAt"),
            check_timestamp(v["createdAt"], "createdAt"),
        )

    def to_dict(self):
        return {
            "schemaVersion": 1,
            "libraryId": str(self.library_id),
            "entityType": self.entity_type,
            "entityId": str(self.entity_id),
            "revision": self.revision,
            "parentRevision": self.parent_revision,
            "operationId": str(self.operation_id),
            "deletedAt": self.deleted_at,
            "createdAt": self.created_at,
        }


@dataclass(frozen=True)
class ProcessingArtifact:
    asset_id: UUID
    artifact_id: UUID
    processing_type: str
    pipeline_version: str
    input_sha256: str
    implementation_version: str
    result_object: BlobReference
    created_at: str
    model_name: str | None = None
    model_version: str | None = None
    source_object_key: str | None = None
    schema_version: int = 1

    @classmethod
    def from_dict(cls, v):
        check_fields(v, ARTIFACT_FIELDS, "processing artifact")
        for key in ("modelName", "modelVersion"):
            if key in v and not isinstance(v[key], str):
                raise ValueError(f"{key} must be a string")
        result = v["resultObject"]
        check_fields(result, {"objectKey", "sha256", "sizeBytes", "mimeType"}, "resultObject")
        result_key = require_str(result["objectKey"], "resultObject.objectKey")
        result_sha = check_sha(result["sha256"], "resultObject.sha256")
        if result_key != f"objects/{result_sha}":
            raise ValueError("resultObject objectKey must be content-addressed")
        ref = BlobReference(
            UUID(int=0),
            "SIDECAR",
            result_key,
            "processing-artifact",
            result_sha,
            require_int(result["sizeBytes"], "resultObject.sizeBytes"),
            require_str(result["mimeType"], "resultObject.mimeType"),
        )
        return cls(
            check_uuid(v["assetId"], "assetId"),
            check_uuid(v["artifactId"], "artifactId"),
            require_str(v["processingType"], "processingType"),
            require_str(v["pipelineVersion"], "pipelineVersion"),
            check_sha(v["inputSha256"], "inputSha256"),
            require_str(v["implementationVersion"], "implementationVersion"),
            ref,
            check_timestamp(v["createdAt"], "createdAt"),
            v.get("modelName"),
            v.get("modelVersion"),
            v.get("sourceObjectKey"),
        )

    def to_dict(self):
        return {
            "schemaVersion": 1,
            "assetId": str(self.asset_id),
            "artifactId": str(self.artifact_id),
            "processingType": self.processing_type,
            **({"modelName": self.model_name} if self.model_name is not None else {}),
            **({"modelVersion": self.model_version} if self.model_version is not None else {}),
            **({"sourceObjectKey": self.source_object_key} if self.source_object_key is not None else {}),
            "pipelineVersion": self.pipeline_version,
            "inputSha256": self.input_sha256,
            "implementationVersion": self.implementation_version,
            "createdAt": self.created_at,
            "resultObject": {
                "objectKey": self.result_object.object_key,
                "sha256": self.result_object.sha256,
                "sizeBytes": self.result_object.size_bytes,
                "mimeType": self.result_object.mime_type,
            },
        }
