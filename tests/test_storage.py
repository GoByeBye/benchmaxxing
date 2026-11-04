import hashlib
import io
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from benchmaxxing.contracts import InferenceProfile, RunSpec
from benchmaxxing.storage import Store, utc_now


def test_export_roundtrip_and_integrity(tmp_path: Path) -> None:
    store = Store(tmp_path)
    run = store.create_run(RunSpec())
    first = store.append_event(run.id, "tool", {"stdout": "hello"})
    second = store.append_event(run.id, "finished", {})
    artifact = store.add_artifact(run.id, "../../solution.py", b"print('hello')")
    store.update_run(run.id, status="passed", metadata={"image_id": "sha256:example"})
    assert [e.seq for e in store.events(run.id, after=first.seq)] == [second.seq]
    with zipfile.ZipFile(io.BytesIO(store.export_run(run.id))) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        for name, info in manifest["files"].items():
            data = archive.read(name)
            assert hashlib.sha256(data).hexdigest() == info["sha256"]
            assert len(data) == info["size"]
        assert all(".." not in name for name in archive.namelist())
        assert json.loads(archive.read("run.json"))["status"] == "passed"
        assert archive.read(f"artifacts/{artifact.id}") == b"print('hello')"


def test_artifact_ownership_and_corruption(tmp_path: Path) -> None:
    store = Store(tmp_path)
    first = store.create_run(RunSpec())
    second = store.create_run(RunSpec())
    artifact = store.add_artifact(first.id, "solution.py", b"original")
    with pytest.raises(KeyError):
        store.artifact_content(second.id, artifact.id)
    with pytest.raises(KeyError):
        store.artifact_content(first.id, "../../controller.json")
    (store.artifact_dir / artifact.id).write_bytes(b"modified")
    store.update_run(first.id, status="passed")
    with pytest.raises(ValueError, match="checksum"):
        store.export_run(first.id)


def test_restart_preserves_completed_and_interrupts_active(tmp_path: Path) -> None:
    store = Store(tmp_path)
    completed = store.create_run(RunSpec())
    store.update_run(completed.id, status="passed")
    active = store.create_run(RunSpec())
    store.update_run(active.id, status="running")
    queued = store.create_run(RunSpec())
    reopened = Store(tmp_path)
    assert {r.id for r in reopened.mark_interrupted()} == {active.id, queued.id}
    assert reopened.get_run(completed.id).status == "passed"
    assert reopened.mark_interrupted() == []
    assert reopened.events(active.id)[-1].kind == "interrupted"


@pytest.mark.parametrize(
    ("kind", "data"),
    [
        ("model.delta", {"turn": 1, "type": "refusal", "text": "Declined."}),
        (
            "model.response",
            {"turn": 1, "message": {"role": "assistant", "refusal": "Declined."}},
        ),
    ],
)
def test_explicit_refusal_is_atomic_with_trace_and_survives_restart(
    tmp_path: Path, kind: str, data: dict[str, object]
) -> None:
    store = Store(tmp_path)
    run = store.create_run(RunSpec(participant="openai", inference=InferenceProfile(model="test")))
    store.update_run(run.id, status="running")
    store.append_event(run.id, kind, data)
    reopened = Store(tmp_path)
    assert reopened.get_run(run.id).refusal == "explicit_provider"
    (interrupted,) = reopened.mark_interrupted()
    assert interrupted.refusal == "explicit_provider"
    assert [item.kind for item in reopened.events(run.id)][-2:] == [kind, "interrupted"]
    with zipfile.ZipFile(io.BytesIO(reopened.export_run(run.id))) as archive:
        assert json.loads(archive.read("run.json"))["refusal"] == "explicit_provider"


def test_restart_recovers_refusal_from_legacy_trace(tmp_path: Path) -> None:
    store = Store(tmp_path)
    run = store.create_run(RunSpec(participant="openai", inference=InferenceProfile(model="test")))
    store.update_run(run.id, status="running")
    # Older controllers wrote the response event before updating the run record.
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "INSERT INTO events(run_id,timestamp,kind,data) VALUES (?,?,?,?)",
            (
                run.id,
                utc_now(),
                "model.response",
                json.dumps({"turn": 1, "message": {"role": "assistant", "refusal": "Declined."}}),
            ),
        )
    assert store.get_run(run.id).refusal is None
    (interrupted,) = Store(tmp_path).mark_interrupted()
    assert interrupted.refusal == "explicit_provider"


def test_scripted_refusal_marker_remains_a_fixture(tmp_path: Path) -> None:
    store = Store(tmp_path)
    run = store.create_run(RunSpec(scripted_variant="refuse"))
    store.append_event(
        run.id,
        "model.response",
        {"turn": 1, "message": {"role": "assistant", "refusal": "scripted_fixture"}},
    )
    assert store.get_run(run.id).refusal == "scripted_fixture"


def test_export_requires_finished_run(tmp_path: Path) -> None:
    store = Store(tmp_path)
    run = store.create_run(RunSpec())
    with pytest.raises(ValueError, match="finish"):
        store.export_run(run.id)


def test_terminal_state_and_event_are_committed_together(tmp_path: Path) -> None:
    store = Store(tmp_path)
    run = store.create_run(RunSpec())
    store.update_run(
        run.id, status="passed", event_kind="run.finished", event_data={"status": "passed"}
    )
    reopened = Store(tmp_path)
    assert reopened.get_run(run.id).status == "passed"
    assert reopened.events(run.id)[-1].kind == "run.finished"


def test_worker_metadata_snapshot_cannot_lower_recorded_parallel_limit(tmp_path: Path) -> None:
    store = Store(tmp_path)
    run = store.create_run(RunSpec(), metadata={"execution": {"max_parallel_runs": 1}})
    worker_snapshot = run.metadata
    store.update_run(run.id, execution_limit=2)
    finished = store.update_run(
        run.id,
        status="passed",
        metadata={**worker_snapshot, "elapsed_seconds": 1.5},
        event_kind="run.finished",
        event_data={"status": "passed"},
    )
    assert finished.metadata["execution"] == {"max_parallel_runs": 2}
    assert finished.metadata["elapsed_seconds"] == 1.5
    store.update_run(run.id, execution_limit=8)
    assert store.get_run(run.id).metadata["execution"] == {"max_parallel_runs": 2}
    with zipfile.ZipFile(io.BytesIO(store.export_run(run.id))) as archive:
        assert json.loads(archive.read("run.json"))["metadata"]["execution"] == {
            "max_parallel_runs": 2
        }
