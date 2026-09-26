"""Stable identities for browser-served image derivatives."""

PREVIEW_RENDERER_VERSION = "v1"


def derivative_version(kind: str, sha256: str) -> str:
    """Return the URL/cache identity for one immutable derivative."""
    return f"{kind}-{PREVIEW_RENDERER_VERSION}-{sha256}"


def derivative_etag(kind: str, sha256: str) -> str:
    return f'"{derivative_version(kind, sha256)}"'
