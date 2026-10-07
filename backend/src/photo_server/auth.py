"""Single-user authentication primitives.

The API deliberately has no user table in V1. Sessions are signed, short-lived
claims containing no identifying data; the configured password hash and static
API token are the only credentials.
"""
import base64
import hashlib
import hmac
import secrets
import time
from typing import Literal

from fastapi import Request

SESSION_VERSION = "v1"
SESSION_HASH = "sha256"
SESSION_SALT_BYTES = 16
SESSION_TOKEN_BYTES = 32


def hash_password(password: str, *, iterations: int = 310_000) -> str:
    """Create the documented password hash format for deployment setup."""
    salt = secrets.token_bytes(SESSION_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(SESSION_HASH, password.encode(), salt, iterations)
    def encoder(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).decode().rstrip("=")
    return f"pbkdf2_sha256${iterations}${encoder(salt)}${encoder(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, raw_iterations, raw_salt, raw_digest = encoded.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        iterations = int(raw_iterations)
        if iterations < 100_000 or iterations > 2_000_000:
            return False
        def decode(value: str) -> bytes:
            return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        salt = decode(raw_salt)
        expected = decode(raw_digest)
        actual = hashlib.pbkdf2_hmac(SESSION_HASH, password.encode(), salt, iterations)
        return hmac.compare_digest(actual, expected)
    except (TypeError, ValueError):
        return False


def issue_session(secret: str, ttl_seconds: int, now: int | None = None) -> str:
    expires = int(time.time() if now is None else now) + ttl_seconds
    nonce = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
    payload = f"{SESSION_VERSION}.{expires}.{nonce}"
    signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def verify_session(secret: str, token: str | None, now: int | None = None) -> bool:
    if not token:
        return False
    try:
        version, raw_expires, nonce, signature = token.split(".", 3)
        if version != SESSION_VERSION or not nonce:
            return False
        expires = int(raw_expires)
        if expires <= int(time.time() if now is None else now):
            return False
        payload = f"{version}.{expires}.{nonce}"
        expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)
    except (TypeError, ValueError):
        return False


def request_origin(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-proto", request.url.scheme).split(",", 1)[0].strip()
    return f"{forwarded}://{request.url.netloc}"


def origin_allowed(request: Request, configured: list[str]) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        # Same-origin form/navigation requests commonly omit Origin. They are
        # still protected by SameSite=Lax and are not cross-origin signals.
        return True
    return origin == request_origin(request) or origin in configured


def auth_scheme(request: Request, secret: str, cookie_name: str, api_token: str) -> Literal["cookie", "token", "none"]:
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
        if api_token and hmac.compare_digest(supplied, api_token):
            return "token"
    if verify_session(secret, request.cookies.get(cookie_name)):
        return "cookie"
    return "none"
