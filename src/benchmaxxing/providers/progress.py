"""Persist preparation progress without changing the versioned job contract."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from benchmaxxing.providers import jobs
from benchmaxxing.providers.contracts import PreparationJob
from benchmaxxing.providers.registry import _atomic_json


def report_progress(data_dir: Path, suite_id: str, details: dict[str, Any]) -> None:
    """Update only this controller's running preparation for the given suite.

    Headless preparations have no job and deliberately do nothing here. The
    existing ``result`` field also works with an already-running controller;
    its normal completion write replaces these intermediate measurements.
    """
    with jobs._guard:
        matches: list[tuple[Path, PreparationJob]] = []
        for job_id in jobs._active:
            path = jobs._path(data_dir, job_id)
            if not path.is_file():
                continue
            job = PreparationJob.model_validate_json(path.read_bytes())
            if job.suite_id == suite_id and job.status == "running":
                matches.append((path, job))
        if len(matches) != 1:
            return
        path, job = matches[0]
        job.result = {**(job.result or {}), **details}
        job.updated_at = datetime.now(UTC).isoformat()
        _atomic_json(path, job.model_dump(mode="json"))
