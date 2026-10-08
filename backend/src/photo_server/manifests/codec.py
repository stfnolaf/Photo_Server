"""Canonical JSON encoding, decoding, and hashing for manifest records."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, TypeVar

from .models import (
    AlbumManifest,
    AssetManifest,
    BurstManifest,
    FaceManifest,
    FingerprintManifest,
    PersonManifest,
    ProcessingArtifact,
    Tombstone,
)

T = TypeVar("T")


class ManifestCodecError(ValueError):
    """Raised for malformed JSON, unsupported schema, or invalid manifests."""


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ManifestCodecError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _check_json(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ManifestCodecError("non-finite JSON number")
    if isinstance(value, dict):
        if any(not isinstance(k, str) for k in value):
            raise ManifestCodecError("JSON object keys must be strings")
        for item in value.values():
            _check_json(item)
    elif isinstance(value, list):
        for item in value:
            _check_json(item)


def canonical_json(value: dict[str, Any]) -> bytes:
    """Return deterministic UTF-8 JSON bytes with sorted keys and no whitespace."""
    _check_json(value)
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ManifestCodecError("value is not canonical JSON") from exc


def _decode(payload: bytes | str, model: type[T]) -> T:
    try:
        if isinstance(payload, bytes):
            payload.decode("utf-8")
        value = json.loads(
            payload,
            object_pairs_hook=_reject_duplicates,
            parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)),
        )
    except ManifestCodecError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
        raise ManifestCodecError("malformed JSON") from exc
    try:
        _check_json(value)
        return model.from_dict(value)
    except (ValueError, TypeError, KeyError) as exc:
        raise ManifestCodecError(str(exc)) from exc


def decode_asset_manifest(payload):
    return _decode(payload, AssetManifest)


def decode_album_manifest(payload):
    return _decode(payload, AlbumManifest)


def decode_person_manifest(payload):
    return _decode(payload, PersonManifest)


def decode_face_manifest(payload):
    return _decode(payload, FaceManifest)


def decode_fingerprint_manifest(payload):
    return _decode(payload, FingerprintManifest)


def decode_burst_manifest(payload):
    return _decode(payload, BurstManifest)


def decode_tombstone(payload):
    return _decode(payload, Tombstone)


def decode_processing_artifact(payload):
    return _decode(payload, ProcessingArtifact)


def decode(payload: bytes | str, kind: str):
    models = {
        "asset": AssetManifest,
        "album": AlbumManifest,
        "person": PersonManifest,
        "face": FaceManifest,
        "fingerprint": FingerprintManifest,
        "burst": BurstManifest,
        "tombstone": Tombstone,
        "processing-artifact": ProcessingArtifact,
    }
    try:
        model = models[kind]
    except KeyError as exc:
        raise ManifestCodecError(f"unknown manifest kind: {kind}") from exc
    return _decode(payload, model)


def encode(manifest: Any) -> bytes:
    if not hasattr(manifest, "to_dict"):
        raise ManifestCodecError("unsupported manifest model")
    try:
        return canonical_json(manifest.to_dict())
    except (ValueError, TypeError) as exc:
        raise ManifestCodecError(str(exc)) from exc


def canonicalize(payload: bytes | str, kind: str) -> bytes:
    """Decode and re-encode a record into its canonical byte representation."""
    return encode(decode(payload, kind))


def sha256(manifest: Any) -> str:
    return hashlib.sha256(encode(manifest)).hexdigest()
