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
    max_batch_files: int = Field(default=1000, ge=1, le=10000)
    max_file_bytes: int = Field(default=512 * 1024 * 1024, ge=1)
    upload_workers: int = Field(default=4, ge=1, le=32)
    upload_abandon_seconds: int = Field(default=24 * 60 * 60, ge=300)
    worker_threads: int = Field(default=4, ge=1, le=32)
    postgres_backup_prefix: str = "backups/postgres"
    upload_part_bytes: int = Field(
        default=8 * 1024 * 1024,
        ge=5 * 1024 * 1024,
        le=64 * 1024 * 1024,
    )
    cors_origins: str = ""
    exiftool: str = "exiftool"
    ai_ollama_url: str = "http://ollama:11434"
    ai_model: str = "qwen3-vl:8b-instruct-q4_K_M"
    ai_timeout_seconds: int = Field(default=600, ge=30, le=3600)
    ai_context_tokens: int = Field(default=4096, ge=2048, le=32768)
    ai_face_max_image_side: int = Field(default=2000, ge=512, le=4096)
    ai_vlm_max_image_side: int = Field(default=1280, ge=512, le=4096)
    face_models_dir: Path = Path("/models")
    face_detection_threshold: float = Field(default=0.8, ge=0.1, le=1.0)
    face_match_threshold: float = Field(default=0.4, ge=0.0, le=1.0)

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
