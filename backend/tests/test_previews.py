import io
from types import SimpleNamespace
from uuid import uuid4

from PIL import Image

from photo_server.models import Blob, Manifest
from photo_server.worker import cache_paths, generate


def preview_fixture(tmp_path, content, role="ORIGINAL_RAW", metadata=None):
    asset_id = uuid4()
    blob = Blob(
        blob_id=uuid4(),
        role=role,
        original_filename="sample.ARW",
        object_key=f"originals/{asset_id}/sample.ARW",
        sha256="a" * 64,
        size_bytes=len(content),
        mime_type="image/x-sony-arw",
    )
    manifest = Manifest(
        library_id=uuid4(),
        asset_id=asset_id,
        operation_id=uuid4(),
        primary_blob_id=blob.blob_id,
        blobs=[blob],
        imported_at="2026-01-01T00:00:00Z",
        metadata=metadata or {},
    )
    catalog = SimpleNamespace(
        record_preview_cache=lambda *args: None,
        backfill_preview_cache=lambda *args: True,
        touch_preview_cache=lambda *args: True,
    )
    service = SimpleNamespace(
        settings=SimpleNamespace(data_dir=tmp_path, exiftool="exiftool"),
        scratch=tmp_path,
        storage=SimpleNamespace(chunks=lambda key: [content]),
        catalog=catalog,
    )
    return service, manifest


def jpeg_bytes(size=(100, 60)):
    output = io.BytesIO()
    Image.new("RGB", size, "blue").save(output, "JPEG")
    return output.getvalue()


def test_no_embedded_raw_preview_is_unavailable(tmp_path, monkeypatch):
    service, manifest = preview_fixture(tmp_path, b"raw-original")
    calls = []

    def extract(args, **kwargs):
        calls.append(args[2])
        return SimpleNamespace(returncode=0, stdout=b"")

    monkeypatch.setattr("photo_server.worker.subprocess.run", extract)
    assert generate(service, manifest) is False
    assert calls == ["-JpgFromRaw", "-PreviewImage", "-ThumbnailImage"]
    assert not any(path.exists() for path in cache_paths(service, manifest).values())


def test_corrupt_embedded_preview_falls_back_and_applies_orientation(tmp_path, monkeypatch):
    data = jpeg_bytes()
    broken = data[:-30]
    # Pillow's old header-only verification accepts this truncated JPEG.
    with Image.open(io.BytesIO(broken)) as candidate:
        candidate.verify()
    service, manifest = preview_fixture(tmp_path, b"raw-original", metadata={"Orientation": 6})
    outputs = iter([broken, data])
    monkeypatch.setattr(
        "photo_server.worker.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=next(outputs)),
    )
    assert generate(service, manifest) is True
    for path in cache_paths(service, manifest).values():
        with Image.open(path) as image:
            assert image.size == (60, 100)
            assert not image.getexif().get(274)  # Pixels are already oriented.


def test_large_jpeg_generates_bounded_derivatives_without_upscaling(tmp_path):
    service, manifest = preview_fixture(tmp_path, jpeg_bytes((3000, 1500)), role="ORIGINAL_JPEG")
    assert generate(service, manifest)
    targets = cache_paths(service, manifest)
    with Image.open(targets["thumbnail"]) as thumb, Image.open(targets["preview"]) as preview:
        assert thumb.size == (256, 128)
        assert preview.size == (2560, 1280)
