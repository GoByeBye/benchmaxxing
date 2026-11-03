import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest

from benchmaxxing.contracts import RunSpec
from benchmaxxing.storage import Store


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
