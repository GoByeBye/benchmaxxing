"""Persisted preparation jobs, separate from measured candidate execution."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from benchmaxxing.providers.contracts import PreparationJob
from benchmaxxing.providers.registry import _atomic_json, root

_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="benchmark-preparation")
_active: set[str] = set()
_guard = threading.Lock()


def _path(data_dir: Path, job_id: str) -> Path:
    if len(job_id) != 32 or any(char not in "0123456789abcdef" for char in job_id):
        raise ValueError("Invalid preparation job ID")
    return root(data_dir) / "jobs" / f"{job_id}.json"


def get_job(data_dir: Path, job_id: str) -> PreparationJob:
    with _guard:
        job = PreparationJob.model_validate_json(_path(data_dir, job_id).read_bytes())
        active = job_id in _active
        if job.status in {"queued", "running"} and not active:
            job.status = "interrupted"
            job.error = (
                "Preparation controller stopped; cached sources remain available for inspection"
            )
            job.updated_at = datetime.now(UTC).isoformat()
            _atomic_json(_path(data_dir, job_id), job.model_dump(mode="json"))
    return job


def list_jobs(data_dir: Path) -> list[PreparationJob]:
    return sorted(
        (get_job(data_dir, path.stem) for path in (root(data_dir) / "jobs").glob("*.json")),
        key=lambda job: job.created_at,
        reverse=True,
    )[:100]


def submit_job(
    data_dir: Path,
    suite_id: str,
    operation: Literal["import", "prepare", "fetch"],
    action: Callable[[], dict[str, Any]],
) -> PreparationJob:
    now = datetime.now(UTC).isoformat()
    job = PreparationJob(
        id=uuid.uuid4().hex,
        suite_id=suite_id,
        operation=operation,
        status="queued",
        created_at=now,
        updated_at=now,
    )
    with _guard:
        _active.add(job.id)
    _atomic_json(_path(data_dir, job.id), job.model_dump(mode="json"))

    def execute() -> None:
        try:
            job.status = "running"
            job.updated_at = datetime.now(UTC).isoformat()
            _atomic_json(_path(data_dir, job.id), job.model_dump(mode="json"))
            job.result = action()
            job.status = "completed"
        except Exception as exc:
            job.status = "failed"
            job.error = str(exc)[:2000]
        finally:
            with _guard:
                try:
                    if job.status == "failed":
                        saved = PreparationJob.model_validate_json(
                            _path(data_dir, job.id).read_bytes()
                        )
                        job.result = saved.result
                    job.updated_at = datetime.now(UTC).isoformat()
                    _atomic_json(_path(data_dir, job.id), job.model_dump(mode="json"))
                finally:
                    _active.discard(job.id)

    _pool.submit(execute)
    return job.model_copy(deep=True)
