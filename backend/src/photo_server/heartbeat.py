"""File-backed worker heartbeats; no database schema is required."""

import json
import os
import socket
import time
from pathlib import Path
from uuid import uuid4

WORKER_ID = f"{socket.gethostname()}-{os.getpid()}-{uuid4().hex[:8]}"


def heartbeat_path(data_dir: Path, worker_type: str) -> Path:
    return data_dir / "heartbeats" / f"{worker_type}.json"


def write_heartbeat(data_dir: Path, worker_type: str, **fields) -> None:
    path = heartbeat_path(data_dir, worker_type)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"workerType": worker_type, "workerId": WORKER_ID, "timestamp": time.time(), **fields}))
    os.replace(temporary, path)


def read_heartbeat(data_dir: Path, worker_type: str, stale_seconds: int) -> dict:
    path = heartbeat_path(data_dir, worker_type)
    if not path.is_file():
        return {"status": "not-configured"}
    try:
        value = json.loads(path.read_text())
        age = max(0.0, time.time() - float(value["timestamp"]))
    except (OSError, ValueError, TypeError, KeyError):
        return {"status": "stale"}
    return {"status": "stale" if age > stale_seconds else "running", "ageSeconds": round(age, 1), "workerType": worker_type}
