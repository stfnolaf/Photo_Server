from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import ValidationError

import photo_server.api as api_module
from photo_server.api import create_app
from photo_server.auth import hash_password, issue_session, verify_password, verify_session
from photo_server.config import Settings


def test_password_and_session_primitives():
    password_hash = hash_password("correct horse")
    assert verify_password("correct horse", password_hash)
    assert not verify_password("wrong horse", password_hash)
    token = issue_session("s" * 32, 60, now=100)
    assert verify_session("s" * 32, token, now=120)
    assert not verify_session("s" * 32, token, now=161)
    assert not verify_session("x" * 32, token, now=120)


def test_auth_enabled_requires_secrets():
    try:
        Settings(
            _env_file=None,
            s3_endpoint="http://localhost:9000",
            database_url="postgresql+psycopg://test@localhost/test",
            auth_enabled=True,
        )
    except ValidationError as error:
        assert "PHOTO_PASSWORD_HASH" in str(error)
    else:
        raise AssertionError("auth startup validation accepted missing secrets")


class _Service:
    def __init__(self, settings):
        self.settings = settings
        self.catalog = type("Catalog", (), {"engine": self})()
        self.storage = type("Storage", (), {})()

    def dispose(self):
        return None

    def initialize(self):
        return {}


def test_login_cookie_flags_and_request_policy(monkeypatch):
    settings = Settings(
        _env_file=None,
        s3_endpoint="http://127.0.0.1:9",
        database_url="postgresql+psycopg://photo:photo@127.0.0.1:5432/photo",
        data_dir=Path("/tmp/photo-server-auth-tests"),
        auth_enabled=True,
        password_hash=hash_password("secret"),
        session_secret="s" * 32,
        api_token="t" * 32,
        session_cookie_secure=True,
    )
    monkeypatch.setattr(api_module, "Service", _Service)
    monkeypatch.setattr(api_module, "reconcile_ready_upload_batches", lambda service: {})
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        assert client.get("/livez").status_code == 200
        assert client.get("/health").status_code == 401
        assert client.post("/auth/login", json={"password": "nope"}).status_code == 401
        response = client.post("/auth/login", json={"password": "secret"})
        assert response.status_code == 200
        cookie = response.headers["set-cookie"]
        assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie
        assert client.get("/auth/session").json() == {"authenticated": True}
        assert client.post("/auth/logout", headers={"Origin": "https://evil.example"}).status_code == 403
        client.cookies.clear()
        assert client.get("/openapi.json").status_code == 401
        assert client.get("/openapi.json", headers={"Authorization": "Bearer " + "t" * 32}).status_code == 200
