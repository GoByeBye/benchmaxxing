"""Progress must never adopt stale jobs or report completion prematurely."""

import threading

from benchmaxxing.providers import jobs
from benchmaxxing.providers.progress import report_progress
from benchmaxxing.providers.registry import _atomic_json


def test_progress_is_visible_before_preparation_finishes(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    details = {"phase": "canonical_assets", "generated_records": 2, "total_records": 10}

    def action():
        report_progress(tmp_path, "frontend-design", details)
        report_progress(tmp_path, "frontend-design", {"current_record": "washer-2"})
        entered.set()
        assert release.wait(timeout=10)
        return {"prepared_tasks": 10}

    job = jobs.submit_job(tmp_path, "frontend-design", "prepare", action)
    try:
        assert entered.wait(timeout=5)
        current = jobs.get_job(tmp_path, job.id)
        assert current.status == "running"
        assert current.result == {**details, "current_record": "washer-2"}
        assert current.error is None
    finally:
        release.set()
        jobs._pool.submit(lambda: None).result(timeout=5)
    finished = jobs.get_job(tmp_path, job.id)
    assert finished.status == "completed"
    assert finished.result == {"prepared_tasks": 10}


def test_progress_does_not_adopt_another_controller_or_completed_job(tmp_path, monkeypatch):
    job = jobs.PreparationJob(
        id="a" * 32,
        suite_id="frontend-design",
        operation="prepare",
        status="running",
        created_at="2026-11-04T00:00:00+00:00",
        updated_at="2026-11-04T00:00:00+00:00",
    )
    path = jobs._path(tmp_path, job.id)
    _atomic_json(path, job.model_dump(mode="json"))
    before = path.read_bytes()
    monkeypatch.setattr(jobs, "_active", set())
    report_progress(tmp_path, "frontend-design", {"generated_records": 1})
    assert path.read_bytes() == before
    monkeypatch.setattr(jobs, "_active", {job.id})
    report_progress(tmp_path, "miniwob", {"generated_records": 1})
    assert path.read_bytes() == before
    job.status = "completed"
    _atomic_json(path, job.model_dump(mode="json"))
    before = path.read_bytes()
    report_progress(tmp_path, "frontend-design", {"generated_records": 1})
    assert path.read_bytes() == before
