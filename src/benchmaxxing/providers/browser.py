"""Bounded controller transport for an isolated, private BrowserGym worker."""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from benchmaxxing.contracts import Budgets
from benchmaxxing.environment import (
    MANAGED_LABEL,
    RUN_LABEL,
    WORKSPACE_LABEL,
    EnvironmentError,
    ExecutionCancelled,
    ExecutionTimeout,
    OutputLimitExceeded,
    PreparationError,
    ToolOperationError,
    _docker,
    _inspect_image,
    _require_success,
    workspace_id,
)
from benchmaxxing.providers.contracts import ImportedTask
from benchmaxxing.providers.registry import asset_root, file_hash, root


class BrowserEnvironment:
    """A run owns its worker and, when supplied, every seeded website container.

    A prepared ``browser_runtime`` may describe fresh image-seeded services:
    ``services: [{name, image_id, memory_mb, command?, environment?}]`` and
    ``url_map: {shopping: 'http://shop:80', ...}``. No host volumes, published
    ports, Docker socket, privileged mode, or shared service URLs are accepted.
    Website image snapshots must already include the upstream initial database.
    """

    def __init__(
        self,
        data_dir: Path,
        run_id: str,
        budgets: Budgets,
        task: ImportedTask,
        cancel: threading.Event | None = None,
        *,
        deadline: float | None = None,
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", run_id):
            raise ValueError("Invalid run ID")
        if task.recipe.family != "browsergym":
            raise ValueError("BrowserEnvironment requires a BrowserGym task")
        self.data_dir, self.run_id, self.budgets, self.task = Path(data_dir), run_id, budgets, task
        self.cancel = cancel
        self.deadline = (
            deadline if deadline is not None else time.monotonic() + budgets.wall_seconds
        )
        self.name = f"benchmaxxing-browser-{uuid.uuid4().hex}"
        self.image_id = ""
        self._network: str | None = None
        self._containers: list[str] = []
        self._staging: tempfile.TemporaryDirectory[str] | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._responses: queue.Queue[bytes | None] = queue.Queue(maxsize=2)
        self._overflow = threading.Event()
        self._stderr = bytearray()
        self._stderr_lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._serial = 0
        self._observation: dict[str, Any] | None = None
        self._response_limit = min(
            20 * 1024 * 1024, budgets.output_bytes + 2 * budgets.artifact_bytes + 8192
        )

    @property
    def diagnostics(self) -> str:
        with self._stderr_lock:
            return bytes(self._stderr).decode("utf-8", errors="replace")

    def _remaining(self, maximum: float | None = None) -> float:
        if self.cancel is not None and self.cancel.is_set():
            raise ExecutionCancelled("Run cancelled")
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ExecutionTimeout("Browser run exhausted its elapsed-time budget")
        return min(remaining, maximum) if maximum is not None else remaining

    def _operation(
        self, arguments: list[str], *, timeout: float = 30, output_limit: int = 65536
    ) -> dict[str, Any]:
        return _docker(
            arguments,
            timeout=self._remaining(timeout),
            output_limit=output_limit,
            cancel_event=self.cancel,
        )

    def _labels(self) -> list[str]:
        return [
            "--label",
            f"{MANAGED_LABEL}=1",
            "--label",
            f"{WORKSPACE_LABEL}={workspace_id(self.data_dir)}",
            "--label",
            f"{RUN_LABEL}={self.run_id}",
        ]

    def _services(self, configuration: dict[str, Any]) -> dict[str, Any]:
        services = configuration.get("services")
        urls = configuration.get("url_map")
        if not isinstance(services, list) or not services or not isinstance(urls, dict):
            raise PreparationError(
                "Prepare image-seeded owned websites and their task-local URL map"
            )
        if configuration.get("snapshot_semantics") != "fresh-image-per-attempt":
            raise PreparationError(
                "Website preparation must declare fresh image snapshots per attempt"
            )
        from urllib.parse import urlsplit

        names: set[str] = set()
        memory = 512
        for service in services:
            if not isinstance(service, dict) or not set(service).issubset(
                {"name", "image_id", "memory_mb", "command", "environment"}
            ):
                raise PreparationError("Unsupported website service options")
            name, image = service.get("name"), service.get("image_id")
            if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,50}", name):
                raise PreparationError("Invalid task-local service name")
            if (
                name in names
                or not isinstance(image, str)
                or not re.fullmatch(r"sha256:[a-f0-9]{64}", image)
            ):
                raise PreparationError(
                    "Website services require unique names and pinned local image IDs"
                )
            names.add(name)
            if _inspect_image(image).get("Id") != image:
                raise PreparationError("Prepared website image identity changed")
            service_memory = service.get("memory_mb", 512)
            if type(service_memory) is not int or service_memory < 64:
                raise PreparationError("Invalid website memory limit")
            memory += service_memory
            command, environment = service.get("command", []), service.get("environment", {})
            if not isinstance(command, list) or any(not isinstance(item, str) for item in command):
                raise PreparationError("Website command must be an argv list")
            if not isinstance(environment, dict) or any(
                not isinstance(key, str)
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
                or not isinstance(value, str)
                or "\0" in value
                for key, value in environment.items()
            ):
                raise PreparationError("Invalid website environment mapping")
        if memory > self.budgets.memory_mb:
            raise PreparationError(f"Worker and websites require at least {memory} MiB run memory")
        for value in urls.values():
            if not isinstance(value, str):
                raise PreparationError("Invalid website URL")
            parsed = urlsplit(value)
            if parsed.scheme not in ("http", "https") or parsed.hostname not in names:
                raise PreparationError("Website URL must name a fresh owned service")
        self._network = f"benchmaxxing-browser-net-{uuid.uuid4().hex}"
        _require_success(
            self._operation(["network", "create", "--internal", *self._labels(), self._network]),
            "Could not create task-local network",
        )
        for service in services:
            name = f"{self.name}-{service['name']}"
            self._containers.append(name)
            arguments = [
                "create",
                "--pull",
                "never",
                "--name",
                name,
                *self._labels(),
                "--network",
                self._network,
                "--network-alias",
                service["name"],
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--memory",
                f"{service.get('memory_mb', 512)}m",
                "--memory-swap",
                f"{service.get('memory_mb', 512)}m",
                "--cpus",
                str(self.budgets.cpus / (len(services) + 1)),
                "--pids-limit",
                str(self.budgets.pids),
            ]
            for key, value in service.get("environment", {}).items():
                arguments += ["--env", f"{key}={value}"]
            arguments += [service["image_id"], *service.get("command", [])]
            _require_success(self._operation(arguments), "Could not create owned website")
            _require_success(self._operation(["start", name]), "Could not start owned website")
        return {
            "url_map": urls,
            "attempt_id": self.run_id,
            "reset_receipt": {"attempt_id": self.run_id, "fresh_owned_services": True},
            "image_map": configuration.get("image_map", {}),
        }

    def _materialize(self, destination: Path) -> dict[str, Any]:
        runtime, grader = destination / "runtime", destination / "grader"
        runtime.mkdir()
        grader.mkdir()
        blobs = asset_root(self.data_dir)
        for asset in self.task.assets:
            if asset.scope not in ("runtime", "grader") or not asset.sha256:
                raise PreparationError("Browser assets must be hashed private/runtime files")
            source = blobs / asset.sha256
            if source.is_symlink() or not source.is_file() or file_hash(source) != asset.sha256:
                raise PreparationError(f"Prepared browser asset changed: {asset.target}")
            target = (runtime if asset.scope == "runtime" else grader) / asset.target
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        for name, text in self.task.private_files.items():
            target = grader / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        # Always overwrite with our versioned trusted bridge, never an imported worker.
        shutil.copyfile(
            Path(__file__).parent / "runtimes/interactive_browser.py",
            runtime / "interactive_browser.py",
        )
        config = json.loads((grader / "browser.json").read_text(encoding="utf-8"))
        if not isinstance(config, dict) or config.get("suite_id") != self.task.suite_id:
            raise PreparationError("Browser task configuration does not match the imported suite")
        return config

    def start(self) -> None:
        if self._process is not None or self._staging is not None:
            raise EnvironmentError("Browser worker was already started")
        from benchmaxxing.providers.preparation import preparation_info

        prepared = preparation_info(self.data_dir, self.task)
        self.image_id = prepared["image_id"]
        if self.budgets.memory_mb < 512 or self.budgets.pids < 128:
            raise PreparationError("Browser runs require at least 512 MiB memory and 128 processes")
        directory = root(self.data_dir) / "attempts"
        directory.mkdir(parents=True, exist_ok=True)
        self._staging = tempfile.TemporaryDirectory(prefix=f"{self.run_id}-", dir=directory)
        staging = Path(self._staging.name).resolve()
        if "," in str(staging):
            self.close()
            raise PreparationError("Docker bind mount paths cannot contain commas")
        try:
            config = self._materialize(staging)
            if self.task.suite_id != "miniwob":
                config.update(self._services(prepared.get("browser_runtime", {})))
            (staging / "grader/browser.json").write_text(json.dumps(config), encoding="utf-8")
            services = prepared.get("browser_runtime", {}).get("services", [])
            service_memory = sum(item.get("memory_mb", 512) for item in services)
            arguments = [
                "create",
                "--pull",
                "never",
                "--name",
                self.name,
                *self._labels(),
                "--network",
                self._network or "none",
                "--read-only",
                "--user",
                "65534:65534",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--memory",
                f"{self.budgets.memory_mb - service_memory}m",
                "--memory-swap",
                f"{self.budgets.memory_mb - service_memory}m",
                "--cpus",
                str(self.budgets.cpus / (len(services) + 1)),
                "--pids-limit",
                str(self.budgets.pids),
                "--shm-size",
                "256m",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,size=536870912,uid=65534,gid=65534,mode=0700",
                "--env",
                "HOME=/tmp",
                "--env",
                "PYTHONDONTWRITEBYTECODE=1",
                "--mount",
                f"type=bind,source={staging / 'runtime'},target=/runtime,readonly",
                "--mount",
                f"type=bind,source={staging / 'grader'},target=/grader,readonly",
                "--workdir",
                "/tmp",
                "--init",
                self.image_id,
                "python",
                "-u",
                "-c",
                "import time; time.sleep(86400)",
            ]
            self._containers.append(self.name)
            _require_success(self._operation(arguments), "Could not create private browser worker")
            _require_success(
                self._operation(["start", self.name]), "Could not start browser worker"
            )
            executable = shutil.which("docker")
            if not executable:
                raise PreparationError("Docker executable disappeared")
            self._process = subprocess.Popen(
                [
                    executable,
                    "exec",
                    "-i",
                    self.name,
                    "python",
                    "-u",
                    "/runtime/interactive_browser.py",
                    "--config",
                    "/grader/browser.json",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            self._threads = [
                threading.Thread(target=self._read_stdout, daemon=True),
                threading.Thread(target=self._read_stderr, daemon=True),
            ]
            for thread in self._threads:
                thread.start()
            self._observation = self._rpc({"op": "reset"}, timeout=120)
        except BaseException:
            self.close()
            raise

    def _read_stdout(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        try:
            while line := self._process.stdout.readline(self._response_limit + 1):
                if len(line) > self._response_limit or not line.endswith(b"\n"):
                    self._overflow.set()
                    return
                try:
                    self._responses.put_nowait(line)
                except queue.Full:
                    self._overflow.set()
                    return
        finally:
            try:
                self._responses.put_nowait(None)
            except queue.Full:
                pass

    def _read_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        while block := self._process.stderr.read(4096):
            with self._stderr_lock:
                self._stderr.extend(block)
                if len(self._stderr) > 65536:
                    del self._stderr[:-65536]

    def _rpc(self, request: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        if self._process is None or self._process.stdin is None:
            raise EnvironmentError("Browser worker is not running")
        self._serial += 1
        request = {**request, "id": self._serial}
        payload = (json.dumps(request, allow_nan=False) + "\n").encode()
        if len(payload) > 131072:
            raise OutputLimitExceeded("Browser request exceeds 128 KiB")
        until = time.monotonic() + self._remaining(timeout or self.budgets.command_seconds)
        write_error: list[Exception] = []

        def write() -> None:
            assert self._process is not None and self._process.stdin is not None
            try:
                self._process.stdin.write(payload)
                self._process.stdin.flush()
            except (OSError, ValueError) as exc:
                write_error.append(exc)

        writer = threading.Thread(target=write, daemon=True)
        writer.start()
        try:
            while True:
                self._remaining()
                if time.monotonic() >= until:
                    raise ExecutionTimeout("Browser operation exhausted its time budget")
                if self._overflow.is_set():
                    raise OutputLimitExceeded("Browser response exceeded its output/artifact limit")
                if write_error:
                    raise EnvironmentError("Browser worker input pipe closed")
                try:
                    line = self._responses.get(timeout=0.025)
                except queue.Empty:
                    if self._process.poll() is not None:
                        raise EnvironmentError(
                            "Browser worker exited before returning a response"
                        ) from None
                    continue
                if line is None:
                    raise EnvironmentError("Browser worker closed its response stream")
                try:
                    response = json.loads(line)
                except ValueError as exc:
                    raise EnvironmentError("Browser worker returned malformed JSON") from exc
                if not isinstance(response, dict) or response.get("id") != self._serial:
                    raise EnvironmentError("Browser worker response sequence did not match")
                if response.get("ok") is not True:
                    raise ToolOperationError(str(response.get("error", "Browser operation failed")))
                return {key: value for key, value in response.items() if key not in ("id", "ok")}
        except EnvironmentError as error:
            # A malformed action is recoverable; uncertain transport/native state is not.
            if not isinstance(error, ToolOperationError):
                self.close()
            raise

    def observe(self) -> dict[str, Any]:
        return self._rpc({"op": "observe"})

    def action(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._rpc({"op": "action", "name": name, "arguments": arguments})

    def finalize(self, answer: str) -> dict[str, Any]:
        self._rpc({"op": "finalize", "response": answer}, timeout=120)
        result = _require_success(
            self._operation(
                [
                    "exec",
                    self.name,
                    "python",
                    "/runtime/interactive_browser.py",
                    "--grade",
                    "/tmp/benchmaxxing-grade/result.json",
                ],
                output_limit=self.budgets.artifact_bytes,
            ),
            "Could not collect the authoritative browser grade",
        )
        value = json.loads(result)
        if not isinstance(value, dict):
            raise EnvironmentError("Browser grade must be an object")
        return value

    def close(self) -> None:
        if self._process is not None:
            if self._process.poll() is None:
                self._process.kill()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        failures = []
        for name in reversed(self._containers):
            try:
                result = _docker(["rm", "-f", name], timeout=15, output_limit=4096)
                if result["exit_code"] and "No such container" not in result["stderr"]:
                    failures.append(f"container {name}")
            except EnvironmentError:
                failures.append(f"container {name}")
        self._containers.clear()
        if self._network:
            try:
                result = _docker(["network", "rm", self._network], timeout=15, output_limit=4096)
                if result["exit_code"] and "not found" not in result["stderr"]:
                    failures.append(f"network {self._network}")
            except EnvironmentError:
                failures.append(f"network {self._network}")
            self._network = None
        for thread in self._threads:
            thread.join(timeout=1)
        if self._staging is not None:
            self._staging.cleanup()
            self._staging = None
        if failures:
            raise EnvironmentError("Owned browser resource cleanup failed: " + ", ".join(failures))
