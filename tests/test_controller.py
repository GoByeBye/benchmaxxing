import threading
import time
from pathlib import Path

import pytest

from benchmaxxing.contracts import RunRecord, RunSpec
from benchmaxxing.controller import Controller, ControllerBusy, RecoveryPending, read_descriptor
from benchmaxxing.storage import Store


def test_exclusive_owner_and_recovery(tmp_path: Path) -> None:
    stale = Store(tmp_path).create_run(RunSpec())
    owner = Controller(tmp_path, cleanup=lambda _: None)
    owner.start()
    try:
        assert read_descriptor(tmp_path)["workspace_id"] == owner.store.workspace_id
        other = Controller(tmp_path, cleanup=lambda _: None)
        with pytest.raises(ControllerBusy):
            other.start()
        assert owner.store.get_run(stale.id).status == "interrupted"
    finally:
        owner.stop()
    assert read_descriptor(tmp_path) is None
    other.start()
    other.stop()


def test_queue_cancellation_does_not_execute_candidate(tmp_path: Path) -> None:
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def execute(store: Store, run_id: str, cancel: threading.Event | None) -> RunRecord:
        calls.append(run_id)
        store.update_run(run_id, status="running")
        entered.set()
        release.wait(5)
        return store.update_run(run_id, status="passed")

    owner = Controller(tmp_path, execute=execute, cleanup=lambda _: None)
    owner.start()
    try:
        first = owner.submit(RunSpec())
        assert entered.wait(2)
        second = owner.submit(RunSpec())
        assert owner.cancel(second.id).status == "cancelled"
        release.set()
        for _ in range(100):
            if owner.store.get_run(first.id).status == "passed":
                break
            time.sleep(0.01)
    finally:
        release.set()
        owner.stop()
    assert calls == [first.id]
    assert owner.store.get_run(second.id).status == "cancelled"


def test_owner_lock_releases_across_threads(tmp_path: Path) -> None:
    owner = Controller(tmp_path, cleanup=lambda _: None)
    starter = threading.Thread(target=owner.start)
    starter.start()
    starter.join(timeout=3)
    assert owner.started
    stopper = threading.Thread(target=owner.stop)
    stopper.start()
    stopper.join(timeout=3)
    assert not owner.started
    replacement = Controller(tmp_path, cleanup=lambda _: None)
    replacement.start()
    replacement.stop()


def test_failed_recovery_blocks_submission_until_cleanup_succeeds(tmp_path: Path) -> None:
    available = False

    def cleanup(_: Path) -> None:
        if not available:
            raise RuntimeError("Docker unavailable")

    def execute(store: Store, run_id: str, _: threading.Event | None) -> RunRecord:
        return store.update_run(run_id, status="passed")

    owner = Controller(tmp_path, cleanup=cleanup, execute=execute)
    owner.start()
    try:
        with pytest.raises(RecoveryPending):
            owner.submit(RunSpec())
        assert owner.store.list_runs() == []
        available = True
        owner.submit(RunSpec())
        assert owner.recovery_warning is None
    finally:
        owner.stop()
