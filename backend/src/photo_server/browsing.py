"""Rebuildable browse fields and the public timeline query contract."""

import base64
import hashlib
import json
from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from photo_server.models import DurableModel, Location, Manifest, UserState


def camera_time(value: str | None) -> datetime | None:
    """Sort by the camera's calendar, retaining its local day even with an offset.

    Unknown timezones stay unknown. The immutable manifest retains the original
    timestamp/offset. Invalid EXIF dates fall back to the import timestamp.
    """
    try:
        return datetime.fromisoformat(value).replace(tzinfo=None) if value else None
    except (TypeError, ValueError):
        return None


def browse_fields(manifest: Manifest) -> dict:
    capture = camera_time(manifest.capture_time)
    imported = camera_time(manifest.imported_at)
    if imported is None:
        raise ValueError("Manifest has no valid import timestamp")
    metadata = manifest.metadata
    return {
        "timeline_at": capture or imported,
        "media_type": manifest.primary.role.removeprefix("ORIGINAL_"),
        "search_text": " ".join(
            str(value)
            for value in (
                manifest.primary.original_filename,
                metadata.get("Make", ""),
                metadata.get("Model", ""),
                metadata.get("LensModel", ""),
                metadata.get("LensID", ""),
                manifest.user_state.caption,
                " ".join(manifest.user_state.keywords),
                manifest.user_state.location.name if manifest.user_state.location else "",
            )
        ),
    }


class BrowseQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    q: str = Field(default="", max_length=200)
    date_from: date | None = None
    date_to: date | None = None
    media_type: Literal["RAW", "JPEG", "HEIF"] | None = None
    rating_min: int = Field(default=0, ge=0, le=5)
    favorite: bool | None = None
    deleted: bool = False
    album_id: UUID | None = None
    sort: Literal["newest", "oldest"] = "newest"
    limit: int = Field(default=60, ge=1, le=200)
    cursor: str | None = Field(default=None, min_length=1, max_length=2048)

    @model_validator(mode="after")
    def date_range(self):
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("Start date must be on or before end date")
        return self

    def fingerprint(self) -> str:
        filters = self.model_dump(mode="json", exclude={"limit", "cursor"})
        return hashlib.sha256(json.dumps(filters, sort_keys=True).encode()).hexdigest()[:24]

    def encode_cursor(self, row) -> str:
        payload = [1, self.fingerprint(), row["timeline_at"].isoformat(), row["id"]]
        return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")

    def decode_cursor(self) -> tuple[datetime, str] | None:
        if self.cursor is None:
            return None
        try:
            payload = json.loads(
                base64.b64decode(
                    self.cursor + "=" * (-len(self.cursor) % 4), altchars=b"-_", validate=True
                )
            )
            if not isinstance(payload, list) or len(payload) != 4:
                raise ValueError
            version, fingerprint, timestamp, asset_id = payload
            if type(version) is not int or not all(
                isinstance(value, str) for value in (fingerprint, timestamp, asset_id)
            ):
                raise ValueError
            instant = datetime.fromisoformat(timestamp)
            if version != 1 or fingerprint != self.fingerprint() or instant.tzinfo is not None:
                raise ValueError
            return instant, str(UUID(asset_id))
        except (ValueError, TypeError, UnicodeError) as error:
            raise ValueError("Invalid cursor or cursor belongs to different filters") from error


class OperationRequest(DurableModel):
    operation_id: UUID
    expected_revision: int | None = Field(default=None, ge=1)


class UserStatePatch(OperationRequest):
    rating: int | None = Field(default=None, ge=0, le=5, strict=True)
    favorite: bool | None = Field(default=None, strict=True)
    caption: str | None = Field(default=None, max_length=10000)
    keywords: list[str] | None = Field(default=None, max_length=200)
    location: Location | None = None

    def changes(self):
        return self.model_dump(
            mode="json",
            by_alias=True,
            exclude_unset=True,
            exclude={"operation_id", "expected_revision"},
        )

    @model_validator(mode="after")
    def nonempty(self):
        changes = self.changes()
        if not changes:
            raise ValueError("Provide at least one metadata field")
        UserState.model_validate(changes)
        return self


class AlbumPatch(OperationRequest):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=10000)
    asset_ids: list[UUID] | None = Field(default=None, max_length=100000)

    def changes(self):
        return self.model_dump(
            mode="json",
            by_alias=True,
            exclude_unset=True,
            exclude={"operation_id", "expected_revision"},
        )

    @model_validator(mode="after")
    def valid_changes(self):
        changes = self.changes()
        if not changes or any(value is None for value in changes.values()):
            raise ValueError("Provide album fields; null is not allowed")
        if self.name is not None and not self.name.strip():
            raise ValueError("Album name cannot be blank")
        if self.asset_ids is not None and len(set(self.asset_ids)) != len(self.asset_ids):
            raise ValueError("Album membership cannot contain duplicates")
        return self


def asset_summary(row) -> dict:
    manifest = Manifest.model_validate(row["manifest"])
    metadata = manifest.metadata
    return {
        "assetId": row["id"],
        "originalFilename": row["original_filename"],
        "mediaType": row["media_type"],
        "timelineTime": row["timeline_at"].isoformat(),
        "dateSource": "capture" if camera_time(manifest.capture_time) else "import",
        "captureTime": manifest.capture_time,
        "importedAt": manifest.imported_at,
        "width": metadata.get("ImageWidth"),
        "height": metadata.get("ImageHeight"),
        "cameraMake": metadata.get("Make"),
        "cameraModel": metadata.get("Model"),
        "lens": metadata.get("LensModel") or metadata.get("LensID"),
        "sizeBytes": manifest.primary.size_bytes,
        "rating": row["rating"],
        "favorite": row["favorite"],
        "caption": manifest.user_state.caption,
        "deletedAt": manifest.deleted_at,
        "revision": manifest.revision,
        "preview": {"status": row["preview_status"] or "missing", "error": row["preview_error"]},
        "thumbnailUrl": f"/assets/{row['id']}/thumbnail",
        "previewUrl": f"/assets/{row['id']}/preview",
    }
