from photo_server.reconcile import ReconciliationReport, _compare, reconcile_s3_to_postgres


class EmptyStorage:
    def __init__(self):
        self.writes = []

    def keys(self, prefix):
        return []

    def head(self, key):
        return None

    def put_json_mutable(self, key, value):
        self.writes.append((key, value))


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
    assert result["authority"] == "postgres"
    assert result["queuesAndLeases"] == "untouched"


def test_checkpoint_is_written_only_after_non_dry_run_scan():
    storage = EmptyStorage()
    result = reconcile_s3_to_postgres(storage, EmptyCatalog(), dry_run=False, report_only=True)
    assert result["status"] == "complete"
    assert len(storage.writes) == 1
    assert storage.writes[0][1]["complete"] is True


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
