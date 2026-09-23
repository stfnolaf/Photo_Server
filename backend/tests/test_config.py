import pytest
from pydantic import ValidationError
from sqlalchemy.engine import make_url

from photo_server.config import Settings
from photo_server.storage import Storage


def clear_credentials(monkeypatch):
    for key in (
        "POSTGRES_PASSWORD",
        "PHOTO_DATABASE_URL",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)


def test_database_password_is_required_without_url_override(monkeypatch):
    clear_credentials(monkeypatch)
    with pytest.raises(ValidationError, match="Set POSTGRES_PASSWORD or PHOTO_DATABASE_URL"):
        Settings(_env_file=None, s3_endpoint="http://localhost:9000")


def test_database_password_is_encoded_and_hidden(monkeypatch):
    clear_credentials(monkeypatch)
    password = "unit-test@:/$ password"
    settings = Settings(
        _env_file=None,
        s3_endpoint="http://localhost:9000",
        POSTGRES_PASSWORD=password,
        postgres_host="database",
        postgres_port=5432,
    )
    parsed = make_url(settings.database_url)
    assert parsed.password == password
    assert parsed.host == "database" and parsed.port == 5432
    assert password not in repr(settings)
    assert settings.database_url not in repr(settings)


def test_s3_credentials_are_loaded_from_dotenv(monkeypatch, tmp_path):
    clear_credentials(monkeypatch)
    env = tmp_path / ".env"
    env.write_text(
        "AWS_ACCESS_KEY_ID=unit-test-id\nAWS_SECRET_ACCESS_KEY=unit-test-secret\n"
        "AWS_SESSION_TOKEN=unit-test-session\n"
    )
    settings = Settings(
        _env_file=env,
        s3_endpoint="http://localhost:9000",
        s3_anonymous=False,
        database_url="postgresql+psycopg://test@localhost/test",
    )
    captured = {}

    def client(*args, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("photo_server.storage.boto3.client", client)
    Storage(settings)
    assert captured["aws_access_key_id"] == "unit-test-id"
    assert captured["aws_secret_access_key"] == "unit-test-secret"
    assert captured["aws_session_token"] == "unit-test-session"
    assert "unit-test-secret" not in repr(settings)


def test_ai_image_sizes_have_separate_environment_overrides(monkeypatch, tmp_path):
    clear_credentials(monkeypatch)
    env = tmp_path / ".env"
    env.write_text(
        "PHOTO_AI_FACE_MAX_IMAGE_SIDE=1800\n"
        "PHOTO_AI_VLM_MAX_IMAGE_SIDE=1024\n"
    )
    settings = Settings(
        _env_file=env,
        s3_endpoint="http://localhost:9000",
        database_url="postgresql+psycopg://test@localhost/test",
    )
    assert settings.ai_face_max_image_side == 1800
    assert settings.ai_vlm_max_image_side == 1024


def test_burst_cluster_thresholds_are_environment_overrides(monkeypatch, tmp_path):
    clear_credentials(monkeypatch)
    env = tmp_path / ".env"
    env.write_text(
        "PHOTO_BURST_CLUSTER_PHASH_MAX_DISTANCE=28\n"
        "PHOTO_BURST_CLUSTER_DHASH_MAX_DISTANCE=19\n"
    )
    settings = Settings(
        _env_file=env,
        s3_endpoint="http://localhost:9000",
        database_url="postgresql+psycopg://test@localhost/test",
    )
    assert settings.burst_cluster_phash_max_distance == 28
    assert settings.burst_cluster_dhash_max_distance == 19


def test_ai_settings_unconfigured_by_default(monkeypatch):
    # Phase 3A: AI is an optional configuration — the VLM endpoint is empty
    # by default (the worker idles; compose sets the local URL explicitly).
    clear_credentials(monkeypatch)
    settings = Settings(
        _env_file=None,
        s3_endpoint="http://localhost:9000",
        database_url="postgresql+psycopg://test@localhost/test",
    )
    assert settings.ai_base_url == ""
    assert settings.ai_api_key == ""
    assert settings.ai_extra_body == ""
    assert settings.ai_model == "unsloth/Qwen3-VL-8B-Instruct-bnb-4bit"


def test_ai_endpoint_settings_come_from_environment(monkeypatch, tmp_path):
    clear_credentials(monkeypatch)
    env = tmp_path / ".env"
    env.write_text(
        "PHOTO_AI_BASE_URL=http://vlm.example.com:11434/v1\n"
        "PHOTO_AI_API_KEY=sk-test\n"
        'PHOTO_AI_EXTRA_BODY={"options": {"num_ctx": 8192}}\n'
    )
    settings = Settings(
        _env_file=env,
        s3_endpoint="http://localhost:9000",
        database_url="postgresql+psycopg://test@localhost/test",
    )
    assert settings.ai_base_url == "http://vlm.example.com:11434/v1"
    assert settings.ai_api_key == "sk-test"
    assert settings.ai_extra_body == '{"options": {"num_ctx": 8192}}'


@pytest.mark.parametrize(
    "extra",
    ['{"options": 1', '[1, 2]', '"options"', "42"],
)
def test_ai_extra_body_must_be_a_json_object(monkeypatch, extra):
    clear_credentials(monkeypatch)
    with pytest.raises(ValidationError, match="PHOTO_AI_EXTRA_BODY"):
        Settings(
            _env_file=None,
            s3_endpoint="http://localhost:9000",
            database_url="postgresql+psycopg://test@localhost/test",
            ai_extra_body=extra,
        )
