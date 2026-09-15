from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PHOTO_", env_file=".env", extra="ignore")

    s3_endpoint: str
    s3_bucket: str = "photo-library"
    s3_anonymous: bool = True
    database_url: str = Field(default="", repr=False)
    postgres_host: str = "localhost"
    postgres_port: int = Field(default=55432, ge=1, le=65535)
    postgres_user: str = Field(default="photo", validation_alias="POSTGRES_USER")
    postgres_password: SecretStr | None = Field(default=None, validation_alias="POSTGRES_PASSWORD")
    postgres_db: str = Field(default="photo", validation_alias="POSTGRES_DB")
    aws_access_key_id: SecretStr | None = Field(default=None, validation_alias="AWS_ACCESS_KEY_ID")
    aws_secret_access_key: SecretStr | None = Field(
        default=None, validation_alias="AWS_SECRET_ACCESS_KEY"
    )
    aws_session_token: SecretStr | None = Field(default=None, validation_alias="AWS_SESSION_TOKEN")
    import_root: Path = Path("imports")
    data_dir: Path = Path(".runtime")
    max_batch_files: int = Field(default=25, ge=1, le=10000)
    max_file_bytes: int = Field(default=512 * 1024 * 1024, ge=1)
    exiftool: str = "exiftool"

    @model_validator(mode="after")
    def configure_database(self):
        if not self.database_url:
            if self.postgres_password is None or not self.postgres_password.get_secret_value():
                raise ValueError(
                    "Set POSTGRES_PASSWORD or PHOTO_DATABASE_URL in the environment/.env"
                )
            self.database_url = URL.create(
                "postgresql+psycopg",
                username=self.postgres_user,
                password=self.postgres_password.get_secret_value(),
                host=self.postgres_host,
                port=self.postgres_port,
                database=self.postgres_db,
            ).render_as_string(hide_password=False)
        return self


class LibraryError(Exception):
    """An operation failed without changing source files."""
