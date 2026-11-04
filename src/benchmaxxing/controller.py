"""One controller and a bounded execution pool per data directory."""

from __future__ import annotations

import json
import os
import secrets
import threading
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from benchmaxxing import __version__
from benchmaxxing.contracts import TERMINAL_STATUSES, ExecutionSettings, RunRecord, RunSpec
from benchmaxxing.storage import Store

Execute = Callable[[Store, str, threading.Event | None], RunRecord]


class ControllerBusy(RuntimeError):
    pass


class RecoveryPending(RuntimeError):
    pass


def _execute(store: Store, run_id: str, cancel: threading.Event | None) -> RunRecord:
    from benchmaxxing.engine import execute_run

    return execute_run(store, run_id, cancel)


def _cleanup(data_dir: Path) -> None:
    from benchmaxxing.environment import cleanup_workspace

    cleanup_workspace(data_dir)


class Controller:
    def __init__(
        self,
        data_dir: Path,
        *,
        execute: Execute = _execute,
        cleanup: Callable[[Path], Any] = _cleanup,
    ):
        self.store = Store(data_dir)
        self.token = secrets.token_urlsafe(32)
        self.lock = FileLock(str(self.store.data_dir / "controller.lock"), thread_local=False)
        self.descriptor = self.store.data_dir / "controller.json"
        self.executor: ThreadPoolExecutor | None = None
        self.cancellations: dict[str, threading.Event] = {}
        self.active_ids: set[str] = set()
        self.pending: deque[str] = deque()
        self.guard = threading.Lock()
        self.settings_path = self.store.data_dir / "execution-settings.json"
        self._settings = ExecutionSettings()
        self.execute = execute
        self.cleanup = cleanup
        self.started = False
        self.stopping = False
        self.recovery_warning: str | None = None

    def start(self, base_url: str | None = None) -> None:
        if self.started:
            return
        try:
            self.lock.acquire(timeout=0)
        except Timeout as exc:
            raise ControllerBusy("A controller already owns this data directory") from exc
        try:
            if self.settings_path.exists():
                self._settings = ExecutionSettings.model_validate_json(
                    self.settings_path.read_text(encoding="utf-8")
                )
            self.store.mark_interrupted()
            try:
                self.cleanup(self.store.data_dir)
            except Exception:
                self.recovery_warning = (
                    "Docker cleanup unavailable; doctor can diagnose engine access"
                )
            descriptor = {
                "schema_version": 1,
                "pid": os.getpid(),
                "version": __version__,
                "workspace_id": self.store.workspace_id,
                "base_url": base_url,
                "token": self.token,
            }
            temporary = self.descriptor.with_suffix(".tmp")
            temporary.write_text(json.dumps(descriptor), encoding="utf-8")
            if os.name != "nt":
                temporary.chmod(0o600)
            temporary.replace(self.descriptor)
            self.executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="benchmark")
            self.stopping = False
            self.started = True
        except BaseException:
            self.lock.release()
            raise

    @property
    def settings(self) -> ExecutionSettings:
        with self.guard:
            return self._settings.model_copy()

    def configure(self, settings: ExecutionSettings) -> ExecutionSettings:
        """Persist a new limit; running attempts finish when the limit is reduced."""
        value = ExecutionSettings.model_validate(settings.model_dump())
        with self.guard:
            if not self.started or self.stopping:
                raise RuntimeError("Controller is not accepting configuration changes")
            temporary = self.settings_path.with_suffix(".tmp")
            temporary.write_text(value.model_dump_json(), encoding="utf-8")
            temporary.replace(self.settings_path)
            self._settings = value
            for run_id in self.active_ids:
                self.store.update_run(run_id, execution_limit=value.max_parallel_runs)
            self._dispatch()
            return value.model_copy()

    def _dispatch(self) -> None:
        """Admit FIFO attempts while holding guard, never blocking a pool worker."""
        if self.stopping or self.executor is None:
            return
        while self.pending and len(self.active_ids) < self._settings.max_parallel_runs:
            run_id = self.pending.popleft()
            cancel = self.cancellations[run_id]
            record = self.store.get_run(run_id)
            if record.status in TERMINAL_STATUSES:
                self.cancellations.pop(run_id, None)
                continue
            self.store.update_run(run_id, execution_limit=self._settings.max_parallel_runs)
            self.active_ids.add(run_id)
            self.executor.submit(self._run, run_id, cancel)

    def submit(self, spec: RunSpec) -> RunRecord:
        from benchmaxxing.task import get_task

        task = get_task(spec.task_id, data_dir=self.store.data_dir)
        if task.suite_id is not None:
            from benchmaxxing.providers.catalog import require_active_suite

            require_active_suite(task.suite_id)
        with self.guard:
            if not self.started or self.stopping or self.executor is None:
                raise RuntimeError("Controller is not accepting runs")
            if self.recovery_warning is not None:
                try:
                    self.cleanup(self.store.data_dir)
                except Exception:
                    raise RecoveryPending(
                        "Docker recovery could not clean owned containers. Check Docker and retry."
                    ) from None
                self.recovery_warning = None
            record = self.store.create_run(spec)
            cancel = threading.Event()
            self.cancellations[record.id] = cancel
            self.pending.append(record.id)
            self._dispatch()
            return record

    def _run(self, run_id: str, cancel: threading.Event) -> None:
        try:
            with self.guard:
                if cancel.is_set():
                    if self.store.get_run(run_id).status not in TERMINAL_STATUSES:
                        self.store.update_run(
                            run_id,
                            status="cancelled",
                            event_kind="cancelled",
                            event_data={"phase": "queue"},
                        )
                    return
            self.execute(self.store, run_id, cancel)
        except Exception:
            # Preserve a reliable terminal record even if an adapter unexpectedly fails.
            if self.store.get_run(run_id).status not in TERMINAL_STATUSES:
                self.store.update_run(
                    run_id,
                    status="infrastructure_error",
                    error="Execution worker failed",
                    event_kind="infrastructure_error",
                    event_data={"reason": "worker_failure"},
                )
        finally:
            with self.guard:
                self.active_ids.discard(run_id)
                self.cancellations.pop(run_id, None)
                self._dispatch()

    def cancel(self, run_id: str) -> RunRecord:
        with self.guard:
            record = self.store.get_run(run_id)
            if record.status in TERMINAL_STATUSES:
                return record
            event = self.cancellations.get(run_id)
            if event is not None:
                event.set()
            if run_id not in self.active_ids and record.status == "queued":
                if run_id in self.pending:
                    self.pending.remove(run_id)
                    self.cancellations.pop(run_id, None)
                return self.store.update_run(
                    run_id,
                    status="cancelled",
                    event_kind="cancelled",
                    event_data={"phase": "queue"},
                )
            self.store.append_event(run_id, "cancellation_requested", {})
        return self.store.get_run(run_id)

    def stop(self) -> None:
        if not self.started:
            return
        with self.guard:
            self.stopping = True
            for cancel in self.cancellations.values():
                cancel.set()
            while self.pending:
                run_id = self.pending.popleft()
                self.cancellations.pop(run_id, None)
                if self.store.get_run(run_id).status not in TERMINAL_STATUSES:
                    self.store.update_run(
                        run_id,
                        status="cancelled",
                        event_kind="cancelled",
                        event_data={"phase": "queue"},
                    )
        if self.executor is not None:
            self.executor.shutdown(wait=True)
        try:
            self.cleanup(self.store.data_dir)
        except Exception:
            self.recovery_warning = "Docker cleanup unavailable"
        try:
            current = json.loads(self.descriptor.read_text(encoding="utf-8"))
            if current.get("token") == self.token:
                self.descriptor.unlink()
        except (OSError, ValueError):
            pass
        finally:
            self.started = False
            self.lock.release()


def read_descriptor(data_dir: Path) -> dict[str, Any] | None:
    try:
        value: Any = json.loads((data_dir / "controller.json").read_text(encoding="utf-8"))
        if isinstance(value, dict) and value.get("schema_version") == 1:
            return value
    except (OSError, ValueError):
        pass
    return None
