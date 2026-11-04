import threading
import time
from pathlib import Path

import pytest

from benchmaxxing.contracts import ExecutionSettings, InferenceProfile, RunRecord, RunSpec
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


@pytest.mark.parametrize("shared_endpoint", [False, True])
def test_models_execute_in_parallel_with_bounded_capacity(
    tmp_path: Path, shared_endpoint: bool
) -> None:
    entered = {model: threading.Event() for model in ("first", "second", "third")}
    released = {model: threading.Event() for model in entered}

    def execute(store: Store, run_id: str, _: threading.Event | None) -> RunRecord:
        run = store.get_run(run_id)
        assert run.spec.inference is not None
        model = run.spec.inference.model
        store.update_run(run_id, status="running")
        entered[model].set()
        assert released[model].wait(5)
        return store.update_run(run_id, status="passed")

    owner = Controller(tmp_path, execute=execute, cleanup=lambda _: None)
    owner.start()
    try:
        owner.configure(ExecutionSettings(max_parallel_runs=2))
        runs = []
        for index, model in enumerate(entered):
            port = 8080 if shared_endpoint else 8080 + index
            runs.append(
                owner.submit(
                    RunSpec(
                        participant="openai",
                        inference=InferenceProfile(
                            model=model,
                            base_url=f"http://127.0.0.1:{port}/v1",
                        ),
                    )
                )
            )
        assert entered["first"].wait(2)
        assert entered["second"].wait(2)
        assert not entered["third"].wait(0.1)
        assert owner.store.get_run(runs[2].id).status == "queued"
        released["first"].set()
        assert entered["third"].wait(2)
        assert owner.store.get_run(runs[1].id).status == "running"
    finally:
        for release in released.values():
            release.set()
        owner.stop()
    assert all(owner.store.get_run(run.id).status == "passed" for run in runs)
    assert all(
        owner.store.get_run(run.id).metadata["execution"] == {"max_parallel_runs": 2}
        for run in runs
    )


def test_live_limit_changes_preserve_running_attempts_and_fifo_queue(tmp_path: Path) -> None:
    entered = [threading.Event() for _ in range(4)]
    released = [threading.Event() for _ in entered]
    order: list[str] = []
    condition = threading.Condition()

    def execute(store: Store, run_id: str, _: threading.Event | None) -> RunRecord:
        with condition:
            index = len(order)
            order.append(run_id)
        store.update_run(run_id, status="running")
        entered[index].set()
        assert released[index].wait(5)
        return store.update_run(run_id, status="passed")

    owner = Controller(tmp_path, execute=execute, cleanup=lambda _: None)
    owner.start()
    try:
        runs = [owner.submit(RunSpec()) for _ in entered]
        assert entered[0].wait(2)
        assert not entered[1].wait(0.1)
        owner.configure(ExecutionSettings(max_parallel_runs=2))
        assert entered[1].wait(2)
        assert owner.store.get_run(runs[0].id).metadata["execution"]["max_parallel_runs"] == 2
        assert owner.store.get_run(runs[1].id).metadata["execution"]["max_parallel_runs"] == 2
        owner.configure(ExecutionSettings(max_parallel_runs=1))
        released[0].set()
        assert not entered[2].wait(0.1)
        assert owner.store.get_run(runs[1].id).status == "running"
        released[1].set()
        assert entered[2].wait(2)
        assert owner.store.get_run(runs[2].id).metadata["execution"]["max_parallel_runs"] == 1
        assert not entered[3].is_set()
        released[2].set()
        assert entered[3].wait(2)
    finally:
        for release in released:
            release.set()
        owner.stop()
    assert order == [run.id for run in runs]


def test_parallel_cancellation_is_scoped_and_stop_cancels_queue(tmp_path: Path) -> None:
    entered = threading.Barrier(3)
    second_running = threading.Event()
    calls: list[str] = []

    def execute(store: Store, run_id: str, cancel: threading.Event | None) -> RunRecord:
        assert cancel is not None
        calls.append(run_id)
        store.update_run(run_id, status="running")
        entered.wait(timeout=3)
        if store.get_run(run_id).spec.scripted_variant == "bad":
            second_running.set()
        assert cancel.wait(5)
        return store.update_run(
            run_id,
            status="cancelled" if cancel.is_set() else "passed",
            event_kind="cancelled" if cancel.is_set() else "passed",
        )

    owner = Controller(tmp_path, execute=execute, cleanup=lambda _: None)
    owner.start()
    try:
        owner.configure(ExecutionSettings(max_parallel_runs=2))
        first = owner.submit(RunSpec())
        second = owner.submit(RunSpec(scripted_variant="bad"))
        entered.wait(timeout=3)
        assert second_running.wait(2)
        queued = owner.submit(RunSpec())
        owner.configure(ExecutionSettings(max_parallel_runs=1))
        owner.cancel(first.id)
        assert owner.store.get_run(second.id).status == "running"
        assert not owner.cancellations[second.id].is_set()
    finally:
        owner.stop()
    assert set(calls) == {first.id, second.id}
    assert owner.store.get_run(first.id).status == "cancelled"
    assert owner.store.get_run(queued.id).status == "cancelled"
    assert owner.store.events(queued.id)[-1].kind == "cancelled"


def test_worker_failure_does_not_stop_other_runs(tmp_path: Path) -> None:
    succeeded = threading.Event()

    def execute(store: Store, run_id: str, _: threading.Event | None) -> RunRecord:
        if store.get_run(run_id).spec.scripted_variant == "bad":
            raise RuntimeError("Explicit fake worker failure")
        result = store.update_run(run_id, status="passed")
        succeeded.set()
        return result

    owner = Controller(tmp_path, execute=execute, cleanup=lambda _: None)
    owner.start()
    try:
        owner.configure(ExecutionSettings(max_parallel_runs=2))
        failed = owner.submit(RunSpec(scripted_variant="bad"))
        passed = owner.submit(RunSpec())
        assert succeeded.wait(2)
    finally:
        owner.stop()
    assert owner.store.get_run(failed.id).status == "infrastructure_error"
    assert owner.store.events(failed.id)[-1].kind == "infrastructure_error"
    assert owner.store.get_run(passed.id).status == "passed"


def test_execution_settings_survive_controller_restart(tmp_path: Path) -> None:
    owner = Controller(tmp_path, cleanup=lambda _: None)
    owner.start()
    try:
        snapshot = owner.configure(ExecutionSettings(max_parallel_runs=3))
        snapshot.max_parallel_runs = 8
        assert owner.settings.max_parallel_runs == 3
    finally:
        owner.stop()
    owner = Controller(tmp_path, cleanup=lambda _: None)
    owner.start()
    try:
        assert owner.settings.max_parallel_runs == 3
    finally:
        owner.stop()
