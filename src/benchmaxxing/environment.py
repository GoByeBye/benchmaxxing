"""Bounded Docker execution. Generated code is never executed on the host."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from benchmaxxing.contracts import Budgets
from benchmaxxing.task import DEFAULT_IMAGE, REFERENCE_ID, get_task, list_tasks, task_files

WORKSPACE_LABEL = "org.benchmaxxing.workspace"
RUN_LABEL = "org.benchmaxxing.run"
MANAGED_LABEL = "org.benchmaxxing.managed"
PREPARED_FILE = "prepared.json"


class EnvironmentError(RuntimeError):
    """The Docker environment is unavailable or an operation failed."""


class PreparationError(EnvironmentError):
    """Explicit preparation is required before execution."""


class ToolOperationError(EnvironmentError):
    """A recoverable workspace operation failed inside an available container."""


class ExecutionCancelled(EnvironmentError):
    """The operator cancelled execution."""


class ExecutionTimeout(EnvironmentError):
    """A command or run exhausted its time budget."""


class OutputLimitExceeded(EnvironmentError):
    """Execution exceeded its bounded output or artifact budget."""


def workspace_id(data_dir: Path) -> str:
    return hashlib.sha256(os.path.normcase(str(data_dir.resolve())).encode()).hexdigest()[:24]


def _bounded_process(
    args: list[str],
    *,
    timeout: float,
    output_limit: int = 1048576,
    input_bytes: bytes | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Drain both pipes concurrently without ever retaining unbounded output."""
    if cancel_event is not None and cancel_event.is_set():
        raise ExecutionCancelled("Run cancelled")
    if timeout <= 0:
        raise ExecutionTimeout("Run time budget exhausted")
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            args,
            stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except OSError as exc:
        raise EnvironmentError(f"Could not start {args[0]}: {exc}") from exc
    streams: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
    guard = threading.Lock()
    exceeded = threading.Event()
    total = 0

    def reader(name: str, pipe: Any) -> None:
        nonlocal total
        try:
            while block := pipe.read(4096):
                with guard:
                    remaining = max(0, output_limit - total)
                    streams[name].extend(block[:remaining])
                    total += len(block[:remaining])
                    if len(block) > remaining:
                        exceeded.set()
        finally:
            pipe.close()

    def writer() -> None:
        assert process.stdin is not None
        try:
            process.stdin.write(input_bytes or b"")
            process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass

    threads = [
        threading.Thread(target=reader, args=(name, pipe), daemon=True)
        for name, pipe in (("stdout", process.stdout), ("stderr", process.stderr))
    ]
    for thread in threads:
        thread.start()
    if input_bytes is not None:
        threading.Thread(target=writer, daemon=True).start()
    failure: EnvironmentError | None = None
    try:
        while process.poll() is None:
            if cancel_event is not None and cancel_event.is_set():
                failure = ExecutionCancelled("Run cancelled")
            elif exceeded.is_set():
                failure = OutputLimitExceeded(f"Output exceeded {output_limit} bytes")
            elif time.monotonic() - started >= timeout:
                failure = ExecutionTimeout(f"Operation exceeded its {timeout:.2f}s time budget")
            if failure is not None:
                process.kill()
                break
            time.sleep(0.025)
        process.wait(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
        for thread in threads:
            thread.join(timeout=2)
    if failure is not None:
        raise failure
    if exceeded.is_set():
        raise OutputLimitExceeded(f"Output exceeded {output_limit} bytes")
    return {
        "exit_code": process.returncode,
        "stdout": bytes(streams["stdout"]).decode("utf-8", errors="replace"),
        "stderr": bytes(streams["stderr"]).decode("utf-8", errors="replace"),
        "duration_seconds": time.monotonic() - started,
    }


def _docker(args: list[str], *, timeout: float = 30, **kwargs: Any) -> dict[str, Any]:
    executable = shutil.which("docker")
    if executable is None:
        raise EnvironmentError("Docker is not installed or is not on PATH")
    return _bounded_process([executable, *args], timeout=timeout, **kwargs)


def _require_success(result: dict[str, Any], action: str) -> str:
    if result["exit_code"] != 0:
        detail = (result["stderr"] or result["stdout"]).strip()
        raise EnvironmentError(f"{action}: {detail[:2000]}")
    return str(result["stdout"])


def _inspect_image(image: str) -> dict[str, Any]:
    text = _require_success(
        _docker(["image", "inspect", "--format", "{{json .}}", image]),
        "Could not inspect prepared image",
    )
    try:
        value: dict[str, Any] = json.loads(text)
        return value
    except (ValueError, TypeError) as exc:
        raise EnvironmentError("Docker returned invalid image metadata") from exc


def prepare(data_dir: Path, image: str = DEFAULT_IMAGE, *, pull: bool = True) -> dict[str, Any]:
    """The only image-download entry point. Pin the actual content after pulling."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:@-]{0,511}", image):
        raise PreparationError("Invalid container image reference")
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    if pull:
        _require_success(_docker(["pull", image], timeout=600), "Image preparation failed")
    info = _inspect_image(image)
    manifest = {
        "schema_version": 1,
        "requested_image": image,
        "image_id": info["Id"],
        "repo_digests": info.get("RepoDigests", []),
        "architecture": info.get("Architecture"),
        "os": info.get("Os"),
        "task_hash": get_task().task_hash,
        "task_hashes": {task.id: task.task_hash for task in list_tasks()},
        "prepared_at": datetime.now(UTC).isoformat(),
    }
    if manifest["os"] != "linux":
        raise PreparationError("The reference pack requires a Linux container image")
    temporary = data_dir / f".{PREPARED_FILE}.{uuid.uuid4().hex}.tmp"
    temporary.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    temporary.replace(data_dir / PREPARED_FILE)
    return manifest


def preparation_info(data_dir: Path, task_id: str = REFERENCE_ID) -> dict[str, Any]:
    try:
        manifest: dict[str, Any] = json.loads((Path(data_dir) / PREPARED_FILE).read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise PreparationError(
            "Reference assets are not prepared; run `benchmaxxing prepare`."
        ) from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise PreparationError("Unsupported preparation record; run `benchmaxxing prepare`.")
    hashes = manifest.get("task_hashes", {REFERENCE_ID: manifest.get("task_hash")})
    if not isinstance(hashes, dict) or hashes.get(task_id) != get_task(task_id).task_hash:
        raise PreparationError(
            "Task assets are new or changed; run `benchmaxxing prepare --no-pull` "
            "to register installed packs using the local image, or `prepare` to download it."
        )
    image_id = manifest.get("image_id", "")
    if not isinstance(image_id, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise PreparationError("Invalid prepared image ID; run `benchmaxxing prepare`.")
    try:
        info = _inspect_image(image_id)
    except EnvironmentError as exc:
        raise PreparationError(
            f"Prepared image unavailable; run `benchmaxxing prepare`. {exc}"
        ) from exc
    if info.get("Id") != image_id or info.get("Os") != "linux":
        raise PreparationError("Prepared image identity/platform mismatch; prepare again.")
    return manifest


def doctor(data_dir: Path) -> dict[str, Any]:
    checks: dict[str, Any] = {"docker_cli": shutil.which("docker") is not None}
    try:
        version = _require_success(
            _docker(["version", "--format", "{{json .Server}}"]), "Docker engine unavailable"
        )
        checks["docker_engine"] = json.loads(version)
    except (EnvironmentError, ValueError) as exc:
        checks["docker_engine"] = {"error": str(exc)}
    try:
        checks["prepared"] = preparation_info(data_dir)
        hashes = checks["prepared"].get(
            "task_hashes", {REFERENCE_ID: checks["prepared"].get("task_hash")}
        )
        checks["catalog"] = {
            "installed": len(list_tasks()),
            "unprepared": [
                task.id for task in list_tasks() if hashes.get(task.id) != task.task_hash
            ],
        }
    except EnvironmentError as exc:
        checks["prepared"] = {"error": str(exc)}
    ready = bool(
        checks["docker_cli"]
        and isinstance(checks["docker_engine"], dict)
        and "error" not in checks["docker_engine"]
        and "error" not in checks["prepared"]
        and not checks.get("catalog", {}).get("unprepared", [])
    )
    return {"ready": ready, "checks": checks}


def cleanup_workspace(data_dir: Path, run_id: str | None = None) -> list[str]:
    """Remove only explicitly labeled resources belonging to this workspace."""
    filters = [
        "--filter",
        f"label={MANAGED_LABEL}=1",
        "--filter",
        f"label={WORKSPACE_LABEL}={workspace_id(Path(data_dir))}",
    ]
    if run_id is not None:
        filters += ["--filter", f"label={RUN_LABEL}={run_id}"]
    output = _require_success(_docker(["ps", "-aq", *filters]), "Could not list owned containers")
    removed: list[str] = []
    for container_id in output.splitlines():
        if re.fullmatch(r"[0-9a-f]{12,64}", container_id):
            _require_success(
                _docker(["rm", "-f", container_id]), "Could not remove owned container"
            )
            removed.append(container_id)
    return removed


_TOOL_ERROR_MARKER = "BENCHMAXXING_TOOL_ERROR:"
_TOOL_LIMIT_MARKER = "BENCHMAXXING_TOOL_LIMIT:"
_READ_FILE = """import base64,pathlib,sys
try:
 p=pathlib.Path('/workspace').joinpath(sys.argv[1]).resolve()
 if not p.is_relative_to('/workspace'): raise ValueError('Path leaves workspace')
 with p.open('rb') as f: data=f.read(int(sys.argv[2])+1)
 if len(data)>int(sys.argv[2]):
  print('BENCHMAXXING_TOOL_LIMIT:File exceeds byte limit')
  sys.exit(66)
 print(base64.b64encode(data).decode('ascii'))
except (OSError,ValueError) as error:
 print('BENCHMAXXING_TOOL_ERROR:'+str(error))
 sys.exit(65)
"""
_WRITE_FILE = """import pathlib,sys
try:
 p=pathlib.Path('/workspace').joinpath(sys.argv[1]).resolve()
 if not p.is_relative_to('/workspace'): raise ValueError('Path leaves workspace')
 p.parent.mkdir(parents=True,exist_ok=True)
 p.write_bytes(sys.stdin.buffer.read())
except (OSError,ValueError) as error:
 print('BENCHMAXXING_TOOL_ERROR:'+str(error))
 sys.exit(65)
"""


def _require_tool_success(result: dict[str, Any], action: str) -> str:
    if result["exit_code"] == 66 and result["stdout"].startswith(_TOOL_LIMIT_MARKER):
        raise OutputLimitExceeded("File exceeds byte limit")
    if result["exit_code"] == 65 and result["stdout"].startswith(_TOOL_ERROR_MARKER):
        raise ToolOperationError(result["stdout"][len(_TOOL_ERROR_MARKER) :].strip()[:2000])
    return _require_success(result, action)


class DockerEnvironment:
    """One disposable environment, with exact image identity and bounded tools."""

    def __init__(
        self,
        data_dir: Path,
        run_id: str,
        budgets: Budgets,
        cancel_event: threading.Event | None = None,
        *,
        deadline: float | None = None,
        task_id: str = REFERENCE_ID,
    ):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", run_id):
            raise ValueError("Invalid run ID")
        self.data_dir = Path(data_dir)
        self.task_id = task_id
        self.run_id = run_id
        self.budgets = budgets
        self.cancel_event = cancel_event
        self.deadline = deadline or time.monotonic() + budgets.wall_seconds
        self.name = f"benchmaxxing-{uuid.uuid4().hex}"
        self.image_id = ""
        self._created = False

    def _remaining(self, maximum: float | None = None) -> float:
        remaining = self.deadline - time.monotonic()
        return min(remaining, maximum) if maximum is not None else remaining

    def _operation(
        self,
        args: list[str],
        *,
        input_bytes: bytes | None = None,
        output_limit: int | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        try:
            return _docker(
                args,
                timeout=self._remaining(timeout or self.budgets.command_seconds),
                input_bytes=input_bytes,
                output_limit=output_limit or self.budgets.output_bytes,
                cancel_event=self.cancel_event,
            )
        except (ExecutionCancelled, ExecutionTimeout, OutputLimitExceeded):
            self._abort()
            raise

    def _abort(self) -> None:
        if self._created:
            try:
                _docker(["kill", self.name], timeout=10, output_limit=4096)
            except EnvironmentError:
                pass

    def start(self) -> None:
        info = preparation_info(self.data_dir, self.task_id)
        self.image_id = info["image_id"]
        args = [
            "create",
            "--pull",
            "never",
            "--name",
            self.name,
            "--label",
            f"{MANAGED_LABEL}=1",
            "--label",
            f"{WORKSPACE_LABEL}={workspace_id(self.data_dir)}",
            "--label",
            f"{RUN_LABEL}={self.run_id}",
            "--network",
            "none",
            "--read-only",
            "--user",
            "65534:65534",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--memory",
            f"{self.budgets.memory_mb}m",
            "--memory-swap",
            f"{self.budgets.memory_mb}m",
            "--cpus",
            str(self.budgets.cpus),
            "--pids-limit",
            str(self.budgets.pids),
            "--ulimit",
            "nofile=256:256",
            "--ulimit",
            "fsize=16777216:16777216",
            "--tmpfs",
            "/workspace:rw,noexec,nosuid,nodev,size=16777216,uid=65534,gid=65534,mode=0700",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=16777216,uid=65534,gid=65534,mode=0700",
            "--workdir",
            "/workspace",
            "--init",
            self.image_id,
            "python",
            "-u",
            "-c",
            "import time; time.sleep(86400)",
        ]
        self._created = True
        try:
            _require_success(self._operation(args, timeout=30), "Could not create task container")
            _require_success(
                self._operation(["start", self.name], timeout=30), "Could not start task container"
            )
            for name, content in task_files(self.task_id).items():
                self.write_file(name, content.decode("utf-8"))
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _validate_path(path: str) -> str:
        candidate = PurePosixPath(path)
        if (
            not path
            or "\\" in path
            or "\0" in path
            or candidate.is_absolute()
            or ".." in candidate.parts
        ):
            raise ValueError("Tool paths must be relative paths within /workspace")
        return path

    def _read_bytes(self, path: str, limit: int) -> bytes:
        path = self._validate_path(path)
        result = self._operation(
            ["exec", self.name, "python", "-I", "-c", _READ_FILE, path, str(limit)],
            output_limit=limit * 2 + 4096,
        )
        output = _require_tool_success(result, "Could not read workspace file")
        try:
            return base64.b64decode(output.strip(), validate=True)
        except ValueError as exc:
            raise EnvironmentError("Invalid workspace file transfer") from exc

    def read_file(self, path: str) -> str:
        return self._read_bytes(path, self.budgets.output_bytes).decode("utf-8", errors="replace")

    def write_file(self, path: str, content: str) -> None:
        path = self._validate_path(path)
        payload = content.encode("utf-8")
        if len(payload) > self.budgets.artifact_bytes:
            raise OutputLimitExceeded("File exceeds artifact byte limit")
        _require_tool_success(
            self._operation(
                ["exec", "-i", self.name, "python", "-I", "-c", _WRITE_FILE, path],
                input_bytes=payload,
            ),
            "Could not write workspace file",
        )

    def execute(self, command: str) -> dict[str, Any]:
        if "\0" in command or len(command.encode("utf-8")) > self.budgets.output_bytes:
            raise ValueError("Command is invalid or exceeds input limit")
        return self._operation(["exec", self.name, "sh", "-lc", command])

    def run_python(self, script: str, input_bytes: bytes) -> dict[str, Any]:
        """Execute a trusted driver in the sandbox; only its observations return."""
        return self._operation(
            ["exec", "-i", self.name, "python", "-I", "-c", script], input_bytes=input_bytes
        )

    def collect(self) -> bytes:
        return self._read_bytes(get_task(self.task_id).submission_file, self.budgets.artifact_bytes)

    def close(self) -> None:
        if self._created:
            result = _docker(["rm", "-f", self.name], timeout=15, output_limit=8192)
            if result["exit_code"] != 0 and "No such container" not in result["stderr"]:
                _require_success(result, "Could not remove task container")
            self._created = False

    def __enter__(self) -> DockerEnvironment:
        self.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
