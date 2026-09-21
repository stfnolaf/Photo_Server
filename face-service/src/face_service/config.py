"""Face-service settings: the single home of all service-side configuration.

The photo server keeps only its client-side settings (URL, token, timeout) in
its own config; every knob of the service process lives here. Environment
variables use the ``PHOTO_`` prefix like the server's, so a compose file can
share values between the two services verbatim:

- ``PHOTO_FACE_SERVICE_BIND`` — host:port to bind (default ``0.0.0.0:8901``).
- ``PHOTO_FACE_SERVICE_TOKEN`` — bearer token required on every endpoint.
  Empty means unauthenticated (dev-only; the service warns at startup).
- ``PHOTO_FACE_MODELS_DIR`` — verified model files (mounted read-only).
- ``PHOTO_FACE_DETECTION_THRESHOLD`` — YuNet confidence gate (default 0.8).
- ``PHOTO_FACE_SERVICE_CONCURRENCY`` — in-flight analyze slots, 1-4.
  Default 1: the OpenCV detector is stateful, so parallelism would need
  isolated per-slot state; the GPU pipeline is single-flight bound anyway.
- ``PHOTO_FACE_TLS_DIR`` — optional directory for the auto-generated
  self-signed CA + server certificate (TLS for cross-machine use; plain HTTP
  stays acceptable docker-internal).

Unlike the server's ``Settings`` this class reads no ``.env`` file: the
service is deployed standalone and is driven by its process environment.
"""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PHOTO_", extra="ignore")

    face_service_bind: str = "0.0.0.0:8901"
    face_service_token: str = ""
    face_models_dir: Path = Path("/models")
    face_detection_threshold: float = Field(default=0.8, ge=0.1, le=1.0)
    face_service_concurrency: int = Field(default=1, ge=1, le=4)
    face_tls_dir: Path | None = None
