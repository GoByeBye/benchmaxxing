"""One controller and one execution worker per data directory."""

from __future__ import annotations

import json
import os
import secrets
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from benchmaxxing import __version__
from benchmaxxing.contracts import TERMINAL_STATUSES, RunRecord, RunSpec
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
        self.active_id: str | None = None
        self.guard = threading.Lock()
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
            self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="benchmark")
            self.started = True
        except BaseException:
            self.lock.release()
            raise

    def submit(self, spec: RunSpec) -> RunRecord:
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
            self.executor.submit(self._run, record.id, cancel)
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
                self.active_id = run_id
            self.execute(self.store, run_id, cancel)
        except Exception:
            # Preserve a reliable terminal record even if an adapter unexpectedly fails.
            self.store.update_run(
                run_id,
                status="infrastructure_error",
                error="Execution worker failed",
                event_kind="infrastructure_error",
                event_data={"reason": "worker_failure"},
            )
        finally:
            with self.guard:
                if self.active_id == run_id:
                    self.active_id = None
                self.cancellations.pop(run_id, None)

    def cancel(self, run_id: str) -> RunRecord:
        with self.guard:
            record = self.store.get_run(run_id)
            if record.status in TERMINAL_STATUSES:
                return record
            event = self.cancellations.get(run_id)
            if event is not None:
                event.set()
            if self.active_id != run_id and record.status == "queued":
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
