import json
from types import SimpleNamespace

import pytest

from photo_server import cutover


class Storage:
    def __init__(self):
        self.values = {}

    def get_json(self, key):
        return json.loads(self.values[key])

    def put_json_mutable(self, key, value):
        self.values[key] = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def service(mode="s3"):
    return SimpleNamespace(
        settings=SimpleNamespace(authority_mode=mode, authority_status_key="indexes/authority/status.json"),
        storage=Storage(), catalog=object(),
    )


def clean_report():
    return {"status": "complete", "missing": [], "divergent": [], "orphaned": [],
            "unresolved": [], "conflicting": [], "malformed": [], "failed": []}


def test_readiness_is_deterministic_and_activation_is_explicit(monkeypatch):
    monkeypatch.setattr(cutover, "reconcile_s3_to_postgres", lambda *args, **kwargs: clean_report())
    manager = cutover.CutoverManager(service())
    first = manager.readiness()
    second = manager.readiness()
    assert first == second
    assert first["status"] == "ready"
    assert manager.activate(first)["authorityMode"] == "s3"


@pytest.mark.parametrize("issue", ["missing", "malformed", "divergent", "unresolved"])
def test_readiness_fails_closed_for_projection_or_namespace_issues(monkeypatch, issue):
    report = clean_report()
    report[issue] = [{"key": "stable", "reason": issue}]
    monkeypatch.setattr(cutover, "reconcile_s3_to_postgres", lambda *args, **kwargs: report)
    result = cutover.CutoverManager(service()).readiness()
    assert result["status"] == "not-ready"
    with pytest.raises(RuntimeError):
        cutover.CutoverManager(service()).activate(result)


def test_rollback_and_emergency_fallback_preserve_only_operational_marker():
    target = service()
    manager = cutover.CutoverManager(target)
    assert manager.rollback("failed readiness")["authorityMode"] == "postgres"
    assert manager.emergency_fallback("storage unavailable")["failureReason"].startswith("emergency")
    assert set(target.storage.values) == {"indexes/authority/status.json"}
