from photo_server.rebuild import MANIFEST_PREFIXES
from photo_server.reconcile import ReconciliationReport, _compare, _scan, reconcile_s3_to_postgres


class EmptyStorage:
    def __init__(self):
        self.writes = []

    def keys(self, prefix):
        return []

    def head(self, key):
        return None

    def put_json_mutable(self, key, value):
        self.writes.append((key, value))


class CheckpointStorage(EmptyStorage):
    def __init__(self, checkpoint):
        super().__init__()
        self.checkpoint = checkpoint

    def head(self, key):
        return {} if key == "indexes/checkpoints/reconciliation-existing.json" else None

    def get_json(self, key):
        assert key == "indexes/checkpoints/reconciliation-existing.json"
        return self.checkpoint


class ResumableStorage(CheckpointStorage):
    def read_bytes(self, _key):
        return b""

    def keys(self, prefix):
        if prefix == MANIFEST_PREFIXES[0][0]:
            return [
                "manifests/assets/0001.json",
                "manifests/assets/0002.json",
            ]
        return []


class NamespaceChangingStorage(EmptyStorage):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def keys(self, prefix):
        self.calls += 1
        # The first complete namespace listing is empty.  A new manifest
        # appears before the final listing, simulating a concurrent writer.
        if self.calls > len(MANIFEST_PREFIXES) and prefix == MANIFEST_PREFIXES[0][0]:
            return ["manifests/assets/concurrent.json"]
        return []


class EmptyConnection:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, _statement):
        return EmptyResult()


class EmptyResult:
    def mappings(self):
        return self

    def __iter__(self):
        return iter(())


class EmptyEngine:
    def connect(self):
        return EmptyConnection()


class EmptyCatalog:
    engine = EmptyEngine()

    def all_assets(self):
        return []

    def all_albums(self):
        return []

    def all_people(self):
        return []


def test_dry_run_is_zero_mutation_and_complete_for_empty_namespace():
    storage = EmptyStorage()
    result = reconcile_s3_to_postgres(storage, EmptyCatalog(), dry_run=True)
    assert result["status"] == "complete"
    assert result["dryRun"] is True
    assert storage.writes == []
    assert result["authority"] == "s3"
    assert result["queuesAndLeases"] == "untouched"


def test_checkpoint_is_written_only_after_non_dry_run_scan():
    storage = EmptyStorage()
    result = reconcile_s3_to_postgres(storage, EmptyCatalog(), dry_run=False, report_only=True)
    assert result["status"] == "complete"
    assert len(storage.writes) == 1
    assert storage.writes[0][1]["complete"] is True
    assert storage.writes[0][1]["schemaVersion"] == 2


def test_completed_checkpoint_does_not_suppress_a_fresh_scan():
    storage = CheckpointStorage({"schemaVersion": 1, "complete": True, "lastKey": None})
    result = reconcile_s3_to_postgres(
        storage,
        EmptyCatalog(),
        checkpoint_id="existing",
        resume=True,
        dry_run=False,
        report_only=True,
    )
    assert result["status"] == "complete"
    assert len(storage.writes) == 1
    assert storage.writes[0][1]["complete"] is True


def test_resumed_scan_reconciles_keys_before_last_key(monkeypatch):
    storage = ResumableStorage({
        "schemaVersion": 1,
        "complete": False,
        "lastKey": "manifests/assets/0002.json",
    })
    monkeypatch.setattr(
        "photo_server.reconcile._key_identity",
        lambda key: ("asset", key.rsplit("/", 1)[-1], 1),
    )
    monkeypatch.setattr("photo_server.reconcile._decode_record", lambda *_args: None)
    report = ReconciliationReport(dry_run=False)
    _records, _referenced, _keys, _checkpoint, paused = _scan(
        storage, report, "existing", resume=True, stop_after=None
    )
    assert paused is False
    assert report.scanned == 2


def test_namespace_change_during_scan_fails_closed():
    result = reconcile_s3_to_postgres(
        NamespaceChangingStorage(), EmptyCatalog(), dry_run=True
    )
    assert result["status"] == "failed"
    assert result["namespaceChanged"] is True
    assert any(issue["category"] == "namespace" for issue in result["failed"])


class OrphanAsset:
    asset_id = "orphan"

    def document(self):
        return {"assetId": "orphan"}


class OrphanCatalog(EmptyCatalog):
    def all_assets(self):
        return [OrphanAsset()]


def test_unrepaired_projection_drift_is_not_reported_complete():
    result = reconcile_s3_to_postgres(
        EmptyStorage(), OrphanCatalog(), dry_run=True
    )
    assert result["status"] == "drift_remaining"
    assert result["orphaned"]


def test_paused_status_is_explicit_and_never_complete():
    report = ReconciliationReport(status="paused")
    assert report.as_dict()["status"] == "paused"


def test_projection_comparison_keeps_exact_categories():
    report = ReconciliationReport()
    _compare(report, "asset", {"a": {"rating": 1}, "b": {}}, {"a": {"rating": 2}, "c": {}})
    assert report.divergent == [
        {"category": "asset", "key": "a", "reason": "durable projection differs from S3"}
    ]
    assert {item["key"] for item in report.missing} == {"b"}
    assert {item["key"] for item in report.orphaned} == {"c"}
