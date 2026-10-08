import json
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PHOTO_", env_file=".env", extra="ignore", populate_by_name=True
    )

    s3_endpoint: str
    s3_bucket: str = "photo-library"
    s3_anonymous: bool = True
    s3_multipart_expiry_days: int = Field(
        default=2, ge=1, le=30, validation_alias="PHOTO_S3_MULTIPART_EXPIRY_DAYS"
    )
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
    upload_cleanup_interval_seconds: int = Field(
        default=60, ge=10, le=86400, validation_alias="PHOTO_UPLOAD_CLEANUP_INTERVAL"
    )
    worker_threads: int = Field(default=4, ge=1, le=32)
    worker_heartbeat_interval_seconds: int = Field(default=15, ge=5, le=300)
    worker_stale_seconds: int = Field(default=60, ge=15, le=3600)
    postgres_backup_prefix: str = "backups/postgres"
    postgres_backup_status_path: Path = Path("/var/lib/photo-backup/status.json")
    postgres_backup_secondary_endpoint: str = ""
    postgres_backup_secondary_path: str = ""
    postgres_backup_secondary_bucket: str = "postgres-backups"
    postgres_backup_secondary_retention: int = Field(default=168, ge=1)
    postgres_backup_secondary_required: bool = False
    postgres_backup_secondary_anonymous: bool = False
    postgres_backup_secondary_access_key_id: SecretStr | None = Field(default=None, repr=False)
    postgres_backup_secondary_secret_access_key: SecretStr | None = Field(default=None, repr=False)
    postgres_backup_secondary_session_token: SecretStr | None = Field(default=None, repr=False)
    recovery_checkpoint_prefix: str = "indexes/recovery-checkpoints"
    recovery_progress_prefix: str = "indexes/checkpoints"
    upload_part_bytes: int = Field(
        default=8 * 1024 * 1024,
        ge=5 * 1024 * 1024,
        le=64 * 1024 * 1024,
    )
    cors_origins: str = ""
    exiftool: str = "exiftool"
    # OpenAI-compatible VLM endpoint: empty = AI not configured (Phase 3A —
    # the worker idles and analysis jobs accumulate as pending); any remote
    # or hosted provider otherwise. Compose sets the local vLLM /v1 URL
    # explicitly, so standard deployments are unaffected.
    ai_base_url: str = ""
    ai_model: str = "unsloth/Qwen3-VL-8B-Instruct-bnb-4bit"
    ai_timeout_seconds: int = Field(default=600, ge=30, le=3600)
    ai_api_key: str = ""
    ai_extra_body: str = ""
    ai_face_max_image_side: int = Field(default=2000, ge=512, le=4096)
    ai_vlm_max_image_side: int = Field(default=1280, ge=512, le=4096)
    # Number of AI dispatcher loops. Keep at one while benchmarking; increasing
    # this permits multiple semantic requests in flight and should be paired
    # with a matching vLLM max-num-seqs setting.
    ai_workers: int = Field(default=1, ge=1, le=8)
    # Semantic-reuse rollout mode: "off" always invokes the VLM, "observe" records
    # the reuse decision but still invokes the VLM, "on" reuses semantics when every
    # gate passes. The first release defaults to "observe".
    ai_semantic_reuse_mode: Literal["off", "observe", "on"] = "observe"
    # Burst clustering thresholds. Clustering is an always-on display feature and
    # is independent of the semantic-reuse rollout mode; these only affect which
    # frames are grouped into a burst.
    # Calibrated against the Sony A7 IV Hawaii set: a modest zoom can reach
    # pHash 22 / dHash 14 while still being an obvious burst pair. Keep both
    # gates so composition changes do not pass on pHash alone.
    burst_cluster_phash_max_distance: int = Field(default=17, ge=0, le=64)
    burst_cluster_dhash_max_distance: int = Field(default=15, ge=0, le=64)
    burst_cluster_chroma_max_distance: float = Field(default=0.15, ge=0, le=1)
    burst_cluster_capture_window_seconds: int = Field(default=35, ge=1, le=3600)
    # Face inference runs in the standalone face-service (Phase 2B of
    # docs/ai-service-split-plan.md); the worker is a plain HTTP client
    # (face_client). An empty URL means AI is not configured: Phase 3A's
    # gate leaves the worker idle (analysis jobs accumulate as pending)
    # instead of failing them. The service's own knobs (models dir,
    # detection threshold, concurrency, bind) live in the face-service's
    # config.
    face_service_url: str = ""
    face_service_token: str = ""
    face_service_timeout: int = Field(default=120, ge=10, le=3600)
    face_match_threshold: float = Field(default=0.4, ge=0.0, le=1.0)
    # Preview cache LRU limits. Previews are disposable: a missing file always
    # resolves through the 202 + regenerate path, so over budget only costs a
    # re-encode of evicted assets. 0 disables eviction (default: off).
    cache_max_bytes: int = Field(default=0, ge=0)
    cache_eviction_interval_seconds: int = Field(
        default=300, ge=10, le=86400, validation_alias="PHOTO_CACHE_EVICTION_INTERVAL"
    )
    cache_eviction_target_ratio: float = Field(
        default=0.9, ge=0.05, le=1.0, validation_alias="PHOTO_CACHE_EVICT_TARGET_RATIO"
    )
    # Opt-in only: the V1 API has no authentication, so enabling this makes
    # anyone who can obtain a derivative URL eligible to receive the cached
    # image from a shared CDN.
    public_derivative_cache: bool = False
    # Metrics are opt-in because the endpoint includes library operational
    # information. When enabled, expose /metrics only to loopback clients.
    metrics_enabled: bool = False
    metrics_loopback_only: bool = True
    # Authentication is opt-in for compatibility with existing development
    # installs. Enabling it requires all three independent secrets below.
    auth_enabled: bool = False
    password_hash: str = Field(default="", repr=False, validation_alias="PHOTO_PASSWORD_HASH")
    session_secret: SecretStr | None = Field(default=None, repr=False, validation_alias="PHOTO_SESSION_SECRET")
    api_token: SecretStr | None = Field(default=None, repr=False, validation_alias="PHOTO_API_TOKEN")
    session_ttl_seconds: int = Field(default=43200, ge=300, le=2592000)
    session_cookie_name: str = "photo_session"
    session_cookie_secure: bool = True
    session_cookie_samesite: Literal["lax", "strict"] = "lax"

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

    @model_validator(mode="after")
    def validate_ai_extra_body(self):
        # Provider extensions merged into the VLM request body. Must be a JSON
        # object when set, so a typo is a startup error, not a failure
        # mid-analysis.
        if self.ai_extra_body:
            try:
                value = json.loads(self.ai_extra_body)
            except json.JSONDecodeError as error:
                raise ValueError(f"PHOTO_AI_EXTRA_BODY must be a JSON object: {error.msg}") from error
            if not isinstance(value, dict):
                raise ValueError("PHOTO_AI_EXTRA_BODY must be a JSON object")
        return self

    @model_validator(mode="after")
    def validate_auth(self):
        if not self.auth_enabled:
            return self
        if not self.password_hash or not self.password_hash.startswith("pbkdf2_sha256$"):
            raise ValueError("PHOTO_PASSWORD_HASH must be a pbkdf2_sha256 password hash")
        if self.session_secret is None or len(self.session_secret.get_secret_value()) < 32:
            raise ValueError("PHOTO_SESSION_SECRET must contain at least 32 characters")
        if self.api_token is None or len(self.api_token.get_secret_value()) < 32:
            raise ValueError("PHOTO_API_TOKEN must contain at least 32 characters")
        if not self.session_cookie_name.replace("_", "").isalnum():
            raise ValueError("PHOTO_SESSION_COOKIE_NAME must be a simple cookie name")
        return self

    @model_validator(mode="after")
    def validate_backup_destinations(self):
        if self.postgres_backup_secondary_endpoint and self.postgres_backup_secondary_path:
            raise ValueError(
                "Configure only one secondary backup destination: endpoint or path"
            )
        return self


class LibraryError(Exception):
    """An operation failed without changing source files."""
