"""Unit tests for the burst-hash-v1 perceptual fingerprint module (Phase 1).

Covers the Phase 1 acceptance criteria:
- stable pHash/dHash values for fixed fixtures;
- small exposure and compression changes remaining within thresholds;
- translations, changed composition, and unrelated images being rejected;
- deterministic candidate ordering and Hamming distance.
"""

import io
from datetime import UTC, datetime, timedelta

from PIL import Image, ImageDraw, ImageEnhance, ImageOps

from photo_server.fingerprints import (
    BURST_HASH_VERSION,
    DHASH_MAX_DISTANCE,
    PHASH_MAX_DISTANCE,
    Candidate,
    Fingerprint,
    candidate_order,
    compute_fingerprint,
    hamming_distance,
)

WIDTH, HEIGHT = 640, 480


def _gradient(width: int, height: int) -> Image.Image:
    """Horizontal luminance gradient (dark left -> light right)."""
    img = Image.new("RGB", (width, height))
    px = img.load()
    for x in range(width):
        value = 40 + int(180 * x / width)
        for y in range(height):
            px[x, y] = (value, value, value)
    return img


def base_image() -> Image.Image:
    """A deterministic scene: gradient plus a bright subject block."""
    img = _gradient(WIDTH, HEIGHT)
    ImageDraw.Draw(img).rectangle((120, 120, 300, 380), fill=(250, 250, 250))
    return img


def unrelated_image() -> Image.Image:
    """Vertical stripes: visually unrelated to the base scene."""
    img = Image.new("RGB", (WIDTH, HEIGHT))
    px = img.load()
    for x in range(WIDTH):
        value = 240 if (x // 40) % 2 == 0 else 30
        for y in range(HEIGHT):
            px[x, y] = (value, value, value)
    return img


def moved_image() -> Image.Image:
    """Same gradient, but the subject block translated to the right third."""
    img = _gradient(WIDTH, HEIGHT)
    ImageDraw.Draw(img).rectangle((400, 120, 580, 380), fill=(250, 250, 250))
    return img


def to_jpeg(img: Image.Image, quality: int = 92) -> bytes:
    out = io.BytesIO()
    img.save(out, "JPEG", quality=quality)
    return out.getvalue()


def fingerprint(img: Image.Image, quality: int = 92) -> Fingerprint:
    return compute_fingerprint(to_jpeg(img, quality))


def test_fingerprint_is_stable_for_fixed_fixture():
    base = base_image()
    first = fingerprint(base)
    second = fingerprint(base)
    assert first == second
    # Pin the exact burst-hash-v1 values for the fixed fixture so any change to the
    # resize/colorspace/DCT/bit-ordering rules is detected (and forces a new version).
    assert first.phash == "873838c738c7c739"
    assert first.dhash == "0000383838181000"
    assert first.algorithm_version == BURST_HASH_VERSION
    assert first.width == WIDTH
    assert first.height == HEIGHT
    # Both hashes are 16-character lowercase hex (unsigned 64-bit values).
    for value in (first.phash, first.dhash):
        assert len(value) == 16
        int(value, 16)
        assert value == value.lower()


def test_exposure_changes_stay_within_thresholds():
    base = fingerprint(base_image())
    bright = fingerprint(ImageEnhance.Brightness(base_image()).enhance(1.15))
    dark = fingerprint(ImageEnhance.Brightness(base_image()).enhance(0.85))
    for near in (bright, dark):
        assert hamming_distance(base.phash, near.phash) <= PHASH_MAX_DISTANCE
        assert hamming_distance(base.dhash, near.dhash) <= DHASH_MAX_DISTANCE


def test_compression_change_stays_within_thresholds():
    base = fingerprint(base_image())
    compressed = fingerprint(base_image(), quality=70)
    assert hamming_distance(base.phash, compressed.phash) <= PHASH_MAX_DISTANCE
    assert hamming_distance(base.dhash, compressed.dhash) <= DHASH_MAX_DISTANCE


def test_translation_is_rejected():
    base = fingerprint(base_image())
    moved = fingerprint(moved_image())
    assert (
        hamming_distance(base.phash, moved.phash) > PHASH_MAX_DISTANCE
        or hamming_distance(base.dhash, moved.dhash) > DHASH_MAX_DISTANCE
    )


def test_changed_composition_is_rejected():
    base = fingerprint(base_image())
    mirrored = fingerprint(ImageOps.mirror(base_image()))
    assert (
        hamming_distance(base.phash, mirrored.phash) > PHASH_MAX_DISTANCE
        or hamming_distance(base.dhash, mirrored.dhash) > DHASH_MAX_DISTANCE
    )


def test_unrelated_image_is_rejected():
    base = fingerprint(base_image())
    other = fingerprint(unrelated_image())
    assert (
        hamming_distance(base.phash, other.phash) > PHASH_MAX_DISTANCE
        or hamming_distance(base.dhash, other.dhash) > DHASH_MAX_DISTANCE
    )


def test_hamming_distance_known_values():
    zero = "0" * 16
    assert hamming_distance(zero, zero) == 0
    assert hamming_distance("3" * 16, zero) == 32  # 0b0011 per digit -> 2 bits x 16
    assert hamming_distance("f" * 16, zero) == 64  # 0b1111 per digit -> 4 bits x 16
    assert hamming_distance("8000000000000000", zero) == 1  # most significant bit
    assert hamming_distance("0000000000000001", zero) == 1  # least significant bit
    assert hamming_distance("0000000000000003", zero) == 2


def test_candidate_order_is_deterministic():
    base = datetime(2026, 1, 1, tzinfo=UTC)
    target = Fingerprint(
        algorithm_version=BURST_HASH_VERSION,
        phash="0" * 16,
        dhash="0" * 16,
        width=WIDTH,
        height=HEIGHT,
    )
    one = "0000000000000001"  # 1 bit from the target
    two = "0000000000000003"  # 2 bits from the target
    candidates = [
        Candidate("b", two, "0" * 16, base + timedelta(seconds=2)),
        Candidate("c", one, "0" * 16, base + timedelta(seconds=1)),
        Candidate("a", two, "0" * 16, base),
        Candidate("d", one, "0" * 16, base + timedelta(seconds=1)),
    ]
    ordered = candidate_order(target, base, candidates)
    # (pHash, dHash, capture distance, asset id): c and d tie on the first three keys,
    # so asset id breaks the tie; a precedes b on capture distance.
    assert [c.asset_id for c in ordered] == ["c", "d", "a", "b"]


def test_candidate_order_puts_missing_capture_time_last():
    base = datetime(2026, 1, 1, tzinfo=UTC)
    target = Fingerprint(
        algorithm_version=BURST_HASH_VERSION,
        phash="0" * 16,
        dhash="0" * 16,
        width=WIDTH,
        height=HEIGHT,
    )
    one = "0000000000000001"
    candidates = [
        Candidate("e", one, "0" * 16, None),  # no capture time
        Candidate("c", one, "0" * 16, base + timedelta(seconds=1)),
    ]
    ordered = candidate_order(target, base, candidates)
    assert [c.asset_id for c in ordered] == ["c", "e"]
