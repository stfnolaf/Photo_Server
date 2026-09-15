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


class Manifest(DurableModel):
    schema_version: Literal[1] = 1
    library_id: UUID
    asset_id: UUID
    revision: Literal[1] = 1
    previous_revision: None = None
    operation_id: UUID
    primary_blob_id: UUID
    blobs: list[Blob] = Field(min_length=1)
    imported_at: str
    capture_time: str | None = None
    metadata: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_blobs(self):
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

    @property
    def key(self) -> str:
        return f"state/assets/{self.asset_id}/{self.revision:08d}.json"
