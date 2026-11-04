"""Compact live monitoring stays consistent with durable run evidence."""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from benchmaxxing.api import create_app
from benchmaxxing.contracts import Budgets, InferenceProfile, RunSpec
from benchmaxxing.controller import Controller
from benchmaxxing.grading import grade_submission
from benchmaxxing.storage import Store
from benchmaxxing.task import get_definition


def test_activity_fifo_actual_start_and_terminal_removal(tmp_path: Path, monkeypatch):
    store = Store(tmp_path)
    first = store.create_run(RunSpec())
    active = store.create_run(RunSpec(task_id="terminal.log-triage"))
    last = store.create_run(RunSpec())
    started = (datetime.now(UTC) - timedelta(seconds=12)).isoformat()
    monkeypatch.setattr("benchmaxxing.storage.utc_now", lambda: started)
    store.update_run(active.id, status="running", event_kind="run.started")
    store.append_event(active.id, "model.request", {"turn": 3, "messages": ["large prompt"]})
    items = store.activity().runs
    assert [item.id for item in items] == [active.id, first.id, last.id]
    assert [item.queue_position for item in items] == [None, 1, 2]
    assert 12 <= items[0].elapsed_seconds < 15
    assert items[0].phase == "inference" and items[0].turn == 3
    assert items[0].task_title and items[0].category == "terminal"
    assert items[1].elapsed_seconds == 0 and items[1].started_at is None
    store.update_run(active.id, status="passed", event_kind="run.finished")
    assert [item.id for item in store.activity().runs] == [first.id, last.id]


def test_projection_persists_without_reading_prompt_payloads(tmp_path: Path):
    store = Store(tmp_path)
    run = store.create_run(
        RunSpec(
            participant="openai",
            inference=InferenceProfile(
                base_url="http://127.0.0.1:8081/v1",
                model="local-model",
                api_key_env="PRIVATE_TOKEN",
            ),
        )
    )
    store.update_run(run.id, status="running", event_kind="run.started")
    store.append_event(run.id, "model.request", {"turn": 2, "messages": ["SECRET-PROMPT" * 1000]})
    store.append_event(run.id, "tool.started", {"tool": "execute", "arguments": "SECRET-ARGS"})
    assert store.activity().runs[0].last_tool == "execute"
    store.append_event(run.id, "grading.check", {"completed": 3, "total": 8})
    last = store.append_event(run.id, "cancellation_requested", {})
    # A corrupt historical payload must not affect the independently persisted projection.
    with sqlite3.connect(store.db_path) as db:
        db.execute("UPDATE events SET data='unreadable historic payload'")
    report = Store(tmp_path).activity()
    item = report.runs[0]
    assert item.phase == "grade" and (item.checks_done, item.checks_total) == (3, 8)
    assert item.cancellation_requested and item.last_event_seq == last.seq
    assert item.last_event_at == last.timestamp and item.turn == 2
    assert item.model == "local-model"
    assert not any(
        secret in report.model_dump_json()
        for secret in (
            "SECRET",
            "PRIVATE_TOKEN",
            "base_url",
            "arguments",
            "messages",
        )
    )


def test_event_and_projection_roll_back_together(tmp_path: Path):
    store = Store(tmp_path)
    run = store.create_run(RunSpec())
    original = store.activity().runs[0].last_event_seq
    with sqlite3.connect(store.db_path) as db:
        db.execute("""CREATE TRIGGER reject_progress BEFORE UPDATE ON run_progress
            BEGIN SELECT RAISE(ABORT, 'test failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        store.append_event(run.id, "model.request", {"turn": 2})
    with pytest.raises(sqlite3.IntegrityError):
        store.update_run(run.id, status="passed", event_kind="run.finished")
    assert store.get_run(run.id).status == "queued"
    assert store.activity().runs[0].last_event_seq == original
    assert len(store.events(run.id)) == 1


def test_old_database_adds_projection_and_recovers(tmp_path: Path):
    store = Store(tmp_path)
    run = store.create_run(RunSpec())
    with sqlite3.connect(store.db_path) as db:
        db.execute("DROP TABLE run_progress")
    reopened = Store(tmp_path)
    assert reopened.activity().runs[0].phase == "queued"
    assert reopened.activity().runs[0].id == run.id
    reopened.mark_interrupted()
    assert reopened.activity().runs == []
    assert reopened.events(run.id)[-1].kind == "interrupted"


def test_activity_endpoint_and_batched_sse_resume(tmp_path: Path):
    owner = Controller(tmp_path, cleanup=lambda _: None)
    with TestClient(
        create_app(tmp_path, controller=owner), base_url="http://127.0.0.1:8123"
    ) as http:
        run = owner.store.create_run(RunSpec())
        assert http.get("/api/v1/activity").json()["runs"][0]["id"] == run.id
        assert (
            http.get("/api/v1/activity", headers={"Origin": "https://outside.invalid"}).status_code
            == 403
        )
        for number in range(151):
            owner.store.append_event(run.id, "example", {"number": number})
        owner.store.update_run(run.id, status="passed", event_kind="run.finished")
        events = owner.store.events(run.id)
        assert len(owner.store.events(run.id, limit=64)) == 64
        trace = http.get(
            f"/api/v1/runs/{run.id}/events", headers={"Last-Event-ID": str(events[10].seq)}
        ).text
        sequences = [int(line[4:]) for line in trace.splitlines() if line.startswith("id: ")]
        assert sequences == [event.seq for event in events[11:]]
        assert trace.endswith("event: complete\ndata: {}\n\n")
        assert http.get("/api/v1/activity").json()["runs"] == []


def test_grading_progress_reports_actual_completed_checks(tmp_path: Path):
    definition = get_definition("refusal.allow-benign-status")
    observed = []
    result = grade_submission(
        definition.good_solution.encode(),
        tmp_path,
        "progress-test",
        Budgets(),
        task_id=definition.id,
        progress=lambda done, total, check: observed.append((done, total, check)),
    )
    assert result.passed
    assert [item[0] for item in observed] == list(range(1, len(result.checks) + 1))
    assert all(item[1] == len(result.checks) for item in observed)
    assert [item[2] for item in observed] == result.checks
