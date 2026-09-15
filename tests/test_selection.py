import pytest

from photo_server.config import LibraryError, Settings
from photo_server.selection import plan_import, plan_names


def selection_settings(root):
    return Settings(
        _env_file=None,
        s3_endpoint="http://localhost:9000",
        database_url="postgresql+psycopg://test@localhost/test",
        import_root=root,
    )


def inputs(tmp_path, *names):
    for name in names:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")
    return selection_settings(tmp_path)


def test_raw_preference_is_order_independent_and_keeps_sidecar(tmp_path):
    names = ["trip/DSC1.HEIF", "trip/DSC1.JPG", "trip/DSC1.arw", "trip/DSC1.xmp"]
    settings = inputs(tmp_path, *names)
    plan = plan_import(settings, names)
    assert plan["assets"] == [{"path": "trip/DSC1.arw", "sidecars": ["trip/DSC1.xmp"]}]
    assert {entry["path"] for entry in plan["skipped"]} == set(names[:2])
    assert plan == plan_import(settings, list(reversed(names)))
    assert all((tmp_path / name).read_bytes() == b"fixture" for name in names)


@pytest.mark.parametrize(
    "names",
    [
        ["day1/DSC1.ARW", "day2/DSC1.JPG"],
        ["DSC1.JPG", "DSC1.HEIC"],
        ["DSC1.ARW", "DSC1.DNG", "DSC1.JPG"],
        ["DSC1.ARW", "dsc1.JPG"],
    ],
)
def test_distinct_or_ambiguous_files_are_not_suppressed(tmp_path, names):
    plan = plan_import(inputs(tmp_path, *names), names)
    assert {entry["path"] for entry in plan["assets"]} == set(names)
    assert not plan["skipped"]


def test_ambiguous_sidecar_is_reported(tmp_path):
    names = ["DSC1.JPG", "DSC1.HEIF", "DSC1.xmp"]
    plan = plan_import(inputs(tmp_path, *names), names)
    assert all(not entry["sidecars"] for entry in plan["assets"])
    assert plan["warnings"] == [{"reason": "unassigned_sidecars", "paths": ["DSC1.xmp"]}]


def test_explicit_files_only_and_limit(tmp_path):
    settings = inputs(tmp_path, "a.JPG", "b.JPG").model_copy(update={"max_batch_files": 1})
    for paths in (["."], ["a.JPG", "b.JPG"], []):
        with pytest.raises(LibraryError):
            plan_import(settings, paths)


def test_rejects_symlink_outside_source_root(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "private.JPG"
    outside.write_bytes(b"private")
    (source / "linked.JPG").symlink_to(outside)
    with pytest.raises(LibraryError):
        plan_import(selection_settings(source), ["linked.JPG"])


def test_deduplicates_same_input_path(tmp_path):
    settings = inputs(tmp_path, "a.JPG")
    assert len(plan_import(settings, ["a.JPG", str(tmp_path / "a.JPG")])["assets"]) == 1


def test_network_batch_selection_rejects_unsafe_and_duplicate_paths():
    for paths in (["/absolute.JPG"], ["../outside.JPG"], ["a.JPG", "a.JPG"]):
        with pytest.raises(LibraryError):
            plan_names(paths, 1000)


def test_network_batch_prefers_raw_without_touching_a_filesystem():
    plan = plan_names(["trip/a.HEIF", "trip/a.ARW", "trip/a.xmp"], 1000)
    assert plan["assets"] == [{"path": "trip/a.ARW", "sidecars": ["trip/a.xmp"]}]
    assert plan["skipped"] == [
        {"path": "trip/a.HEIF", "selected": "trip/a.ARW", "reason": "raw_preferred"}
    ]
