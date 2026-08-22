from __future__ import annotations

import json
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from ..config import get_settings


def _jobs_dir() -> Path:
    d = get_settings().data_dir / "deploy-jobs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(job_id: str) -> Path:
    return _jobs_dir() / f"{job_id}.json"


def _write(job: dict[str, Any]) -> None:
    path = _path(job["id"])
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(job, indent=2))
    tmp.replace(path)


def get_job(job_id: str) -> dict[str, Any] | None:
    path = _path(job_id)
    if not path.exists():
        return None
    return json.loads(path.read_text())


def create_job(kind: str, meta: dict[str, Any]) -> dict[str, Any]:
    job = {
        "id": secrets.token_hex(8),
        "kind": kind,
        "status": "queued",
        "progress": 0,
        "logs": [],
        "error": None,
        "result": None,
        "meta": meta,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _write(job)
    return job


def log(job_id: str, message: str, progress: Optional[int] = None) -> None:
    job = get_job(job_id)
    if not job:
        return
    job["logs"].append(
        {
            "t": time.strftime("%H:%M:%S"),
            "msg": message,
        }
    )
    # keep last 200 lines
    job["logs"] = job["logs"][-200:]
    if progress is not None:
        job["progress"] = max(0, min(100, int(progress)))
    job["status"] = "running"
    job["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _write(job)


def finish(job_id: str, result: dict[str, Any] | None = None) -> None:
    job = get_job(job_id)
    if not job:
        return
    job["status"] = "done"
    job["progress"] = 100
    job["result"] = result
    job["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _write(job)


def fail(job_id: str, error: str) -> None:
    job = get_job(job_id)
    if not job:
        return
    job["status"] = "error"
    job["error"] = error
    job["logs"].append({"t": time.strftime("%H:%M:%S"), "msg": f"ERROR: {error}"})
    job["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _write(job)


ProgressFn = Callable[[str, Optional[int]], None]


def run_in_background(job_id: str, fn: Callable[[ProgressFn], dict[str, Any]]) -> None:
    def _runner() -> None:
        def progress(message: str, pct: int | None = None) -> None:
            # allow callers to pass progress(msg) only
            log(job_id, message, pct)

        try:
            log(job_id, "Starting…", 1)
            result = fn(progress)
            finish(job_id, result)
        except Exception as e:
            fail(job_id, str(e))

    threading.Thread(target=_runner, daemon=True, name=f"deploy-{job_id}").start()
