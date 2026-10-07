"""Phase 10 S3 authority cutover control plane."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from photo_server.reconcile import reconcile_s3_to_postgres


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class AuthorityStatus:
    mode: str = "postgres"
    readiness: str = "unknown"
    projection_freshness: str = "unknown"
    reconciliation: str = "not-run"
    last_verified_recovery_checkpoint: str | None = None
    failure_reason: str | None = None
    updated_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"schemaVersion": 1, "authorityMode": self.mode, "readiness": self.readiness,
                "projectionFreshness": self.projection_freshness,
                "reconciliation": self.reconciliation,
                "lastVerifiedRecoveryCheckpoint": self.last_verified_recovery_checkpoint,
                "failureReason": self.failure_reason, "updatedAt": self.updated_at}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AuthorityStatus":
        return cls(mode=value.get("authorityMode", "postgres"), readiness=value.get("readiness", "unknown"),
                   projection_freshness=value.get("projectionFreshness", "unknown"),
                   reconciliation=value.get("reconciliation", "not-run"),
                   last_verified_recovery_checkpoint=value.get("lastVerifiedRecoveryCheckpoint"),
                   failure_reason=value.get("failureReason"), updated_at=value.get("updatedAt"))


def _sorted_report(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _sorted_report(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        values = [_sorted_report(item) for item in value]
        return sorted(values, key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")))
    return value


class CutoverManager:
    def __init__(self, service):
        self.service = service
        self.key = service.settings.authority_status_key

    def status(self) -> dict[str, Any]:
        try:
            value = self.service.storage.get_json(self.key)
        except Exception:
            value = None
        if not isinstance(value, dict):
            value = AuthorityStatus(mode=self.service.settings.authority_mode).as_dict()
        return _sorted_report(value)

    def _save(self, status: AuthorityStatus) -> dict[str, Any]:
        value = status.as_dict()
        self.service.storage.put_json_mutable(self.key, value)
        return _sorted_report(value)

    def readiness(self, *, checkpoint_id: str = "phase10-readiness") -> dict[str, Any]:
        report = reconcile_s3_to_postgres(self.service.storage, self.service.catalog,
                                          checkpoint_id=checkpoint_id, resume=False,
                                          dry_run=True, report_only=True)
        names = ("missing", "divergent", "orphaned", "unresolved", "conflicting", "malformed", "failed")
        issues = {name: report.get(name, []) for name in names if report.get(name)}
        ready = report.get("status") == "complete" and not issues
        result = _sorted_report({"schemaVersion": 1, "status": "ready" if ready else "not-ready",
                                 "authorityMode": self.service.settings.authority_mode,
                                 "checkpointId": checkpoint_id, "issues": issues,
                                 "reconciliation": report})
        self._save(AuthorityStatus(mode="s3" if ready and self.service.settings.authority_mode == "s3" else "postgres",
                                   readiness="ready" if ready else "not-ready",
                                   projection_freshness="fresh" if ready else "stale",
                                   reconciliation="verified" if ready else "failed",
                                   failure_reason=None if ready else "canonical S3 state and PostgreSQL projection are not equivalent",
                                   updated_at=_now()))
        return result

    def activate(self, readiness: dict[str, Any] | None = None) -> dict[str, Any]:
        result = readiness or self.readiness()
        if result.get("status") != "ready":
            raise RuntimeError("S3 authority cutover is not ready")
        return self._save(AuthorityStatus(mode="s3", readiness="ready", projection_freshness="fresh",
                                          reconciliation="verified", updated_at=_now()))

    def rollback(self, reason: str = "cutover rollback requested") -> dict[str, Any]:
        return self._save(AuthorityStatus(mode="postgres", readiness="rollback",
                                          projection_freshness="unknown", reconciliation="rollback",
                                          failure_reason=reason, updated_at=_now()))

    def emergency_fallback(self, reason: str) -> dict[str, Any]:
        return self.rollback(f"emergency fallback: {reason}")

    def reconcile(self, *, checkpoint_id: str = "phase10-reconcile", apply: bool = True) -> dict[str, Any]:
        return reconcile_s3_to_postgres(self.service.storage, self.service.catalog,
                                        checkpoint_id=checkpoint_id, resume=False,
                                        dry_run=not apply, apply=apply)


def cutover_readiness(service, **kwargs) -> dict[str, Any]:
    return CutoverManager(service).readiness(**kwargs)


def authority_status(service) -> dict[str, Any]:
    return CutoverManager(service).status()
