"""Perceptual image fingerprints for burst-aware semantic reuse and clustering.

`burst-hash-v1` is a pure, deterministic computation over an orientation-corrected,
metadata-free preview rendering. It produces two independent 64-bit hashes and a
compact chroma signature:

- pHash: low-frequency composition via a DCT of a 32x32 grayscale rendering.
- dHash: coarse edge structure via horizontal gradients of a 9x8 grayscale rendering.
- chroma histogram: a 12-bin hue distribution from the same 32x32 preview, ignoring
  dark or nearly gray pixels so clothing/color changes can provide a cheap scene-break
  signal when the grayscale hashes need a relaxed threshold.

Both are expressed as 16-character lowercase hex strings (unsigned 64-bit values).
The exact resize, colorspace, DCT, and bit-ordering rules are frozen by the algorithm
version; changing any of them requires a new version rather than reinterpreting stored
hashes. This module is pure computation: it performs no database or worker work.
"""

from __future__ import annotations

import io
import math
import statistics
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from typing import Sequence

from PIL import Image, ImageOps

from photo_server.models import DurableModel

BURST_HASH_VERSION = "burst-hash-v1"

# Conservative perceptual similarity thresholds. These are the fixed constants of the
# burst-reuse-v1 policy (see reuse.py); they are intentionally not configurable.
PHASH_MAX_DISTANCE = 4
DHASH_MAX_DISTANCE = 6

_PHASH_SIZE = 32
_DHASH_SIZE = (9, 8)
_CHROMA_SIZE = (32, 32)
_CHROMA_BINS = 12
_CHROMA_MIN_SATURATION = 38  # approximately 15% in Pillow's 0..255 HSV range
_CHROMA_MIN_VALUE = 20
_CHROMA_MIN_SAMPLES = 16
_BLOCK = 8
_MISSING_TIME_MS = 10**18


class Fingerprint(DurableModel):
    """A burst-hash-v1 fingerprint for one asset."""

    algorithm_version: str = BURST_HASH_VERSION
    phash: str
    dhash: str
    width: int
    height: int
    chroma_histogram: str | None = None


@dataclass(frozen=True)
class Candidate:
    """A candidate source frame for reuse/clustering, keyed by asset."""

    asset_id: str
    phash: str
    dhash: str
    capture_time: datetime | None
    chroma_histogram: str | None = None


@lru_cache(maxsize=1)
def _dct_basis(n: int) -> list[list[float]]:
    """DCT-II basis table: basis[input][output] = cos(pi/n * (input + 0.5) * output)."""
    return [
        [math.cos(math.pi / n * (x + 0.5) * u) for u in range(n)]
        for x in range(n)
    ]


def _dct2d_lowfreq(pixels: Sequence[float], n: int, block: int) -> list[list[float]]:
    """2-D DCT-II of an n x n image, returning only the top-left block x block."""
    basis = _dct_basis(n)
    # Row transform: M[r][u] for r in 0..n-1, u in 0..block-1.
    m = [[0.0] * block for _ in range(n)]
    for r in range(n):
        row = pixels[r * n : (r + 1) * n]
        for u in range(block):
            total = 0.0
            for x in range(n):
                total += row[x] * basis[x][u]
            m[r][u] = total
    # Column transform: R[r][u] for r, u in 0..block-1.
    result = [[0.0] * block for _ in range(block)]
    for u in range(block):
        column = [m[x][u] for x in range(n)]
        for r in range(block):
            total = 0.0
            for x in range(n):
                total += column[x] * basis[x][r]
            result[r][u] = total
    return result


def _bits_to_hex(bits: Sequence[bool]) -> str:
    """Pack 64 row-major bits into a 16-character lowercase hex string (MSB first)."""
    value = 0
    for index, bit in enumerate(bits):
        if bit:
            value |= 1 << (63 - index)
    return format(value, "016x")


def _phash(gray: Image.Image) -> str:
    resized = gray.resize((_PHASH_SIZE, _PHASH_SIZE), Image.Resampling.LANCZOS)
    pixels = [float(p) for p in resized.get_flattened_data()]
    block = _dct2d_lowfreq(pixels, _PHASH_SIZE, _BLOCK)
    values = [block[r][u] for r in range(_BLOCK) for u in range(_BLOCK)]
    median = statistics.median(values[1:])  # exclude the DC term (index 0)
    return _bits_to_hex([value > median for value in values])


def _dhash(gray: Image.Image) -> str:
    width, height = _DHASH_SIZE
    resized = gray.resize(_DHASH_SIZE, Image.Resampling.LANCZOS)
    pixels = list(resized.get_flattened_data())
    bits = []
    for row in range(height):
        for col in range(width - 1):
            bits.append(pixels[row * width + col] > pixels[row * width + col + 1])
    return _bits_to_hex(bits)


def _chroma_histogram(image: Image.Image) -> str | None:
    """Return a compact normalized hue histogram for scene-break checks."""
    hsv = image.resize(_CHROMA_SIZE, Image.Resampling.LANCZOS).convert("HSV")
    counts = [0] * _CHROMA_BINS
    for hue, saturation, value in hsv.get_flattened_data():
        if saturation < _CHROMA_MIN_SATURATION or value < _CHROMA_MIN_VALUE:
            continue
        counts[min(_CHROMA_BINS - 1, hue * _CHROMA_BINS // 256)] += 1
    samples = sum(counts)
    if samples < _CHROMA_MIN_SAMPLES:
        return None
    return "".join(f"{round(count * 255 / samples):02x}" for count in counts)


def chroma_histogram_distance(a: str | None, b: str | None) -> float | None:
    """Return normalized L1 distance for two compact chroma histograms."""
    if a is None or b is None or len(a) != _CHROMA_BINS * 2 or len(b) != _CHROMA_BINS * 2:
        return None
    left = bytes.fromhex(a)
    right = bytes.fromhex(b)
    return sum(abs(x - y) for x, y in zip(left, right)) / (2 * 255)


def compute_fingerprint(jpeg: bytes) -> Fingerprint:
    """Compute burst-hash-v1 pHash/dHash from an orientation-corrected preview.

    The input is the metadata-free preview rendering produced by
    ``analysis.prepare_jpeg``. EXIF is ignored (the pixels are already oriented);
    ``exif_transpose`` is applied defensively so a raw, oriented JPEG also hashes
    correctly. ``width``/``height`` record the preview's true dimensions for the
    aspect-ratio gate.
    """
    with Image.open(io.BytesIO(jpeg)) as source:
        image = ImageOps.exif_transpose(source)
        gray = image.convert("L")
        width, height = gray.size
        phash = _phash(gray)
        dhash = _dhash(gray)
        chroma_histogram = _chroma_histogram(image)
    return Fingerprint(
        algorithm_version=BURST_HASH_VERSION,
        phash=phash,
        dhash=dhash,
        width=width,
        height=height,
        chroma_histogram=chroma_histogram,
    )


def hamming_distance(a: str, b: str) -> int:
    """Hamming distance between two 64-bit hex fingerprints."""
    return (int(a, 16) ^ int(b, 16)).bit_count()


def _capture_distance_ms(a: datetime | None, b: datetime | None) -> int:
    if a is None or b is None:
        return _MISSING_TIME_MS
    return int(abs((a - b).total_seconds() * 1000))


def candidate_order(
    target: Fingerprint,
    target_capture: datetime | None,
    candidates: Sequence[Candidate],
) -> list[Candidate]:
    """Order candidates by (pHash distance, dHash distance, capture distance, asset ID).

    Deterministic tie-breaking makes retries reproducible. A missing capture time on
    either side sorts last (a large sentinel distance).
    """

    def sort_key(candidate: Candidate) -> tuple[int, int, int, str]:
        return (
            hamming_distance(target.phash, candidate.phash),
            hamming_distance(target.dhash, candidate.dhash),
            _capture_distance_ms(target_capture, candidate.capture_time),
            candidate.asset_id,
        )

    return sorted(candidates, key=sort_key)


__all__ = [
    "BURST_HASH_VERSION",
    "Candidate",
    "DHASH_MAX_DISTANCE",
    "Fingerprint",
    "PHASH_MAX_DISTANCE",
    "candidate_order",
    "chroma_histogram_distance",
    "compute_fingerprint",
    "hamming_distance",
]
