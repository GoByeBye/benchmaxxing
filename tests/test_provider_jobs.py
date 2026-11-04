"""Preparation polling must preserve terminal state and useful failure evidence."""

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import typer
from fastapi.testclient import TestClient

from benchmaxxing import cli, provider_cli
from benchmaxxing.api import create_app
from benchmaxxing.controller import Controller
from benchmaxxing.providers import jobs, registry, runtime
from benchmaxxing.providers.contracts import ImportedTask, NativeRecipe, PreparationJob
from benchmaxxing.providers.progress import report_progress


def test_cli_preserves_partial_preparation_errors_without_spurious_exception(
    tmp_path, monkeypatch, capsys
):
    result = {
        "prepared_tasks": 1,
        "task_ids": ["reviewed"],
        "errors": {"unreviewed": "No reviewed offline build recipe exists"},
    }
    printed = []
    monkeypatch.setattr(cli, "_remote", lambda _: None)
    monkeypatch.setattr(registry, "imported_tasks", lambda *args: [object()])
    monkeypatch.setattr(runtime, "prepare_runtime", lambda *args, **kwargs: result)
    monkeypatch.setattr(provider_cli, "_print", printed.append)

    with pytest.raises(typer.Exit) as raised:
        provider_cli._operation(
            tmp_path,
            "prepare",
            {"suite_id": "terminal-bench-2-1", "image": None, "pull": False},
        )

    assert raised.value.exit_code == 2
    assert printed == [result]
    assert capsys.readouterr().err == ""


def test_poll_cannot_replace_concurrent_completion_with_interrupted(tmp_path, monkeypatch):
    entered = threading.Event()
    finish_action = threading.Event()
    expected = {"prepared_tasks": 3, "task_ids": ["one", "two", "three"]}

    def action():
        entered.set()
        assert finish_action.wait(timeout=5)
        return expected

    with ThreadPoolExecutor(max_workers=1) as pool:
        monkeypatch.setattr(jobs, "_pool", pool)
        job = jobs.submit_job(tmp_path, "miniwob", "prepare", action)
        drained = pool.submit(lambda: None)
        try:
            assert entered.wait(timeout=5)
            job_path = jobs._path(tmp_path, job.id)
            read_bytes = Path.read_bytes
            polling_thread = threading.get_ident()
            intercepted = False

            def read_while_worker_finishes(path):
                nonlocal intercepted
                data = read_bytes(path)
                if path == job_path and threading.get_ident() == polling_thread and not intercepted:
                    intercepted = True
                    assert jobs.PreparationJob.model_validate_json(data).status == "running"
                    # Force completion before the stale read returns whenever the
                    # lifecycle guard allows it. A correctly locked poll defers
                    # completion until its read/active-state decision has ended.
                    completion_allowed = jobs._guard.acquire(blocking=False)
                    if completion_allowed:
                        jobs._guard.release()
                    finish_action.set()
                    if completion_allowed:
                        drained.result(timeout=5)
                return data

            with monkeypatch.context() as patch:
                patch.setattr(Path, "read_bytes", read_while_worker_finishes)
                observed = jobs.get_job(tmp_path, job.id)
        finally:
            finish_action.set()
            drained.result(timeout=5)

    assert intercepted
    assert observed.status in {"running", "completed"}
    persisted = jobs.get_job(tmp_path, job.id)
    assert persisted.status == "completed"
    assert persisted.result == expected
    assert persisted.error is None


def test_failed_job_keeps_last_persisted_progress(tmp_path, monkeypatch):
    progress = {"phase": "canonical_assets", "generated_records": 7, "total_records": 10}

    def action():
        report_progress(tmp_path, "frontend-design", progress)
        report_progress(tmp_path, "frontend-design", {"current_record": "washer-8"})
        raise ValueError("Canonical render failed")

    with ThreadPoolExecutor(max_workers=1) as pool:
        monkeypatch.setattr(jobs, "_pool", pool)
        job = jobs.submit_job(tmp_path, "frontend-design", "prepare", action)
        pool.submit(lambda: None).result(timeout=5)

    failed = jobs.get_job(tmp_path, job.id)
    assert failed.status == "failed"
    assert failed.error == "Canonical render failed"
    assert failed.result == {**progress, "current_record": "washer-8"}
    assert job.id not in jobs._active


def test_prepare_acceptance_defers_full_catalog_loading(tmp_path, monkeypatch):
    task_directory = registry.root(tmp_path) / "tasks"
    task_directory.mkdir(parents=True)
    # Acceptance only needs a matching record path. Its contents are validated
    # later by the worker, which can report malformed records in the durable job.
    (task_directory / "imported.evalplus-humaneval-placeholder.json").write_text("invalid JSON")
    queued = PreparationJob(
        id="c" * 32,
        suite_id="evalplus-humaneval",
        operation="prepare",
        status="queued",
        created_at="2026-11-04T00:00:00+00:00",
        updated_at="2026-11-04T00:00:00+00:00",
    )
    actions = []
    preparations = []

    def forbid_catalog_loading(*args, **kwargs):
        raise AssertionError("HTTP acceptance must not deserialize the imported catalog")

    def queue_without_execution(data_dir, suite_id, operation, action):
        assert data_dir == tmp_path
        assert suite_id == queued.suite_id and operation == "prepare"
        actions.append(action)
        return queued

    def prepare_in_worker(data_dir, suite_id, *, image, pull):
        preparations.append((data_dir, suite_id, image, pull))
        return {"prepared_tasks": 0, "errors": {"record": "Invalid imported record"}}

    monkeypatch.setattr(registry, "imported_tasks", forbid_catalog_loading)
    monkeypatch.setattr(jobs, "submit_job", queue_without_execution)
    monkeypatch.setattr(runtime, "prepare_runtime", prepare_in_worker)
    owner = Controller(tmp_path, cleanup=lambda _: None)
    with TestClient(
        create_app(tmp_path, controller=owner), base_url="http://127.0.0.1:8123"
    ) as client:
        token = client.get("/api/v1/session").json()["token"]
        response = client.post(
            "/api/v1/providers/prepare",
            json={"suite_id": queued.suite_id, "image": "existing-image", "pull": False},
            headers={"X-Benchmaxxing-Token": token},
        )
        assert response.status_code == 202
        assert response.json()["id"] == queued.id
        assert response.json()["status"] == "queued"
        assert len(actions) == 1 and preparations == []

    result = actions[0]()
    assert preparations == [(tmp_path, queued.suite_id, "existing-image", False)]
    assert result["errors"] == {"record": "Invalid imported record"}


def test_suite_filter_skips_unrelated_corrupt_records(tmp_path):
    task = ImportedTask(
        suite_id="evalplus-humaneval",
        upstream_id="fake/0",
        revision="fake-v1",
        source_sha256="a" * 64,
        title="Explicit fake task",
        prompt="No code is executed by this fixture.",
        category="programming",
        recipe=NativeRecipe(family="fake"),
    )
    task_directory = registry.root(tmp_path) / "tasks"
    registry._atomic_json(
        task_directory / f"{task.id}.json",
        {"task": task.model_dump(mode="json"), "sha256": task.task_hash},
    )
    for other in ("bigcodebench", "evalplus-humanevalish"):
        (task_directory / f"imported.{other}-corrupt.json").write_text("invalid JSON")

    assert registry.imported_tasks(tmp_path, task.suite_id) == [task]
    with pytest.raises(ValueError):
        registry.imported_tasks(tmp_path)
