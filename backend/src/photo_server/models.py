from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel


class DurableModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    def document(self) -> dict:
        return self.model_dump(mode="json", by_alias=True)


class Blob(DurableModel):
    blob_id: UUID
    role: Literal["ORIGINAL_RAW", "ORIGINAL_JPEG", "ORIGINAL_HEIF", "SIDECAR"]
    original_filename: str
    object_key: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)
    mime_type: str


class Location(DurableModel):
    name: str = Field(default="", max_length=500)
    latitude: float | None = Field(default=None, ge=-90, le=90, allow_inf_nan=False)
    longitude: float | None = Field(default=None, ge=-180, le=180, allow_inf_nan=False)

    @model_validator(mode="after")
    def coordinates(self):
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("Provide both latitude and longitude")
        return self


class UserState(DurableModel):
    rating: int = Field(default=0, ge=0, le=5, strict=True)
    favorite: bool = Field(default=False, strict=True)
    caption: str = Field(default="", max_length=10000)
    keywords: list[str] = Field(default_factory=list, max_length=200)
    location: Location | None = None

    @model_validator(mode="after")
    def keyword_values(self):
        if any(not word.strip() or len(word) > 200 for word in self.keywords):
            raise ValueError("Keywords must contain 1–200 characters")
        if len(set(self.keywords)) != len(self.keywords):
            raise ValueError("Duplicate keywords")
        return self


class Mutation(DurableModel):
    action: Literal[
        "asset.patch",
        "asset.delete",
        "asset.restore",
        "asset.migrate",
        "asset.metadata",
        "album.create",
        "album.patch",
        "album.delete",
        "album.restore",
        "burst.setRepresentative",
    ]
    entity_id: UUID
    changes: dict = Field(default_factory=dict)
    expected_revision: int | None = Field(default=None, ge=1)


class Manifest(DurableModel):
    schema_version: Literal[1, 2] = 1
    library_id: UUID
    asset_id: UUID
    revision: int = Field(default=1, ge=1, le=99999999)
    previous_revision: int | None = Field(default=None, ge=1)
    operation_id: UUID
    primary_blob_id: UUID
    blobs: list[Blob] = Field(min_length=1)
    imported_at: str
    capture_time: str | None = None
    metadata: dict = Field(default_factory=dict)
    user_state: UserState = Field(default_factory=UserState)
    deleted_at: str | None = None
    mutation: Mutation | None = None

    def document(self) -> dict:
        document = super().document()
        if self.schema_version == 1:
            for name in ("userState", "deletedAt", "mutation"):
                document.pop(name)
        return document

    @model_validator(mode="after")
    def validate_blobs(self):
        if self.schema_version == 1:
            if (
                self.revision != 1
                or self.previous_revision is not None
                or self.mutation is not None
                or self.deleted_at is not None
                or self.user_state != UserState()
            ):
                raise ValueError("Legacy manifests must be unchanged revision 1 imports")
        elif (
            self.revision < 2
            or self.previous_revision != self.revision - 1
            or self.mutation is None
            or self.mutation.entity_id != self.asset_id
            or not self.mutation.action.startswith("asset.")
            or "user_state" not in self.model_fields_set
        ):
            raise ValueError("Invalid asset revision ancestry or mutation")
        ids = [blob.blob_id for blob in self.blobs]
        keys = [blob.object_key for blob in self.blobs]
        if len(set(ids)) != len(ids) or len(set(keys)) != len(keys):
            raise ValueError("Duplicate blob IDs or keys")
        primary = [blob for blob in self.blobs if blob.blob_id == self.primary_blob_id]
        if len(primary) != 1 or primary[0].role == "SIDECAR":
            raise ValueError("A media original must be the primary blob")
        for blob in self.blobs:
            name = blob.original_filename
            if name in {"", ".", ".."} or "/" in name or "\\" in name:
                raise ValueError("Original filename must be a basename")
            if blob.object_key != f"originals/{self.asset_id}/{name}":
                raise ValueError("Blob key does not match its asset and original filename")
        return self

    @property
    def primary(self) -> Blob:
        return next(blob for blob in self.blobs if blob.blob_id == self.primary_blob_id)

class Album(DurableModel):
    schema_version: Literal[1] = 1
    library_id: UUID
    album_id: UUID
    revision: int = Field(ge=1, le=99999999)
    previous_revision: int | None = Field(default=None, ge=1)
    operation_id: UUID
    mutation: Mutation
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=10000)
    asset_ids: list[UUID] = Field(default_factory=list, max_length=100000)
    deleted_at: str | None = None

    @model_validator(mode="after")
    def ancestry(self):
        if (
            self.previous_revision != (self.revision - 1 or None)
            or self.mutation.entity_id != self.album_id
            or not self.mutation.action.startswith("album.")
            or not self.name.strip()
        ):
            raise ValueError("Invalid album revision or name")
        if len(set(self.asset_ids)) != len(self.asset_ids):
            raise ValueError("Album membership cannot contain duplicates")
        return self
