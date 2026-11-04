"""Native upstream tasks in bounded disposable containers, never on the host."""

from __future__ import annotations

import base64
import io
import json
import math
import tarfile
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

from benchmaxxing.contracts import Budgets
from benchmaxxing.environment import (
    MANAGED_LABEL,
    RUN_LABEL,
    WORKSPACE_LABEL,
    DockerEnvironment,
    EnvironmentError,
    ExecutionCancelled,
    ExecutionTimeout,
    OutputLimitExceeded,
    PreparationError,
    _docker,
    _require_success,
    _require_tool_success,
    workspace_id,
)
from benchmaxxing.providers import registry
from benchmaxxing.providers.contracts import ImportedTask, relative_path
from benchmaxxing.providers.preparation import preparation_info

_WRITE_ABSOLUTE = """import pathlib,sys
root=pathlib.Path(sys.argv[1]).resolve(); p=root.joinpath(sys.argv[2]).resolve()
if not p.is_relative_to(root): raise ValueError('Transfer path leaves its declared root')
p.parent.mkdir(parents=True,exist_ok=True)
with p.open('wb') as target:
 while block:=sys.stdin.buffer.read(1048576): target.write(block)
"""

_READ_ABSOLUTE = """import base64,pathlib,stat,sys
p=pathlib.Path(sys.argv[1]); limit=int(sys.argv[2])
if p.is_symlink() or not stat.S_ISREG(p.stat().st_mode): raise ValueError('Not a regular artifact')
with p.open('rb') as source: data=source.read(limit+1)
if len(data)>limit:
 print('BENCHMAXXING_TOOL_LIMIT:Artifact exceeds byte limit'); sys.exit(66)
print(base64.b64encode(data).decode())
"""

_SNAPSHOT = """import base64,json,pathlib,stat,sys
roots=json.loads(sys.argv[1]); limit=int(sys.argv[2]); total=0; result={}
def add(p,root):
 global total
 if p.is_symlink() or not p.resolve().is_relative_to(root.resolve()):
  raise ValueError('Artifact symlinks are not supported')
 if not stat.S_ISREG(p.stat().st_mode): raise ValueError('Artifact is not a regular file')
 name=p.as_posix().lstrip('/')
 if name in result: return
 with p.open('rb') as source: data=source.read(limit-total+1)
 total+=len(data)
 if total>limit or len(result)>=4096:
  print('BENCHMAXXING_TOOL_LIMIT:Snapshot exceeds artifact budget'); sys.exit(66)
 result[name]=base64.b64encode(data).decode()
for name in roots:
 root=pathlib.Path(name)
 if root.is_symlink() or root.resolve()!=root:
  raise ValueError('Artifact root traverses a symlink')
 if root.is_file(): add(root,root.parent)
 elif root.is_dir():
  for p in sorted(root.rglob('*')):
   if p.is_symlink(): raise ValueError('Artifact symlinks are not supported')
   if not p.is_dir(): add(p,root)
 else: raise ValueError('Declared artifact is missing: '+name)
print(json.dumps(result))
"""


def _absolute(value: str) -> str:
    path = PurePosixPath(value)
    if not value.startswith("/") or str(path) != value or ".." in path.parts:
        raise PreparationError("Native artifacts require normalized absolute container paths")
    if any(c in value for c in ("\\", "\0", "*", "?", "[")):
        raise PreparationError("Native artifact paths cannot contain escapes or globs")
    if path == PurePosixPath("/") or path.parts[1] in {
        "grader",
        "tests",
        "solution",
        "proc",
        "sys",
        "dev",
        "etc",
        "root",
        "run",
        "usr",
        "bin",
        "sbin",
        "lib",
        "lib64",
        "opt",
    }:
        raise PreparationError("Artifact root is outside supported task locations")
    return value


def _native_result(raw: bytes) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate result field")
            result[key] = value
        return result

    def constant(value: str) -> None:
        raise ValueError("Non-finite result value: " + value)

    def number(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("Non-finite result number")
        return parsed

    try:
        result = json.loads(
            raw, object_pairs_hook=pairs, parse_constant=constant, parse_float=number
        )
    except (ValueError, UnicodeError) as exc:
        raise EnvironmentError("Native grader returned malformed result JSON") from exc
    if not isinstance(result, dict) or not {"passed", "score"} <= result.keys():
        raise EnvironmentError("Native grader result is missing required fields")
    score = result["score"]
    if score is not None and (
        isinstance(score, bool)
        or not isinstance(score, (int, float))
        or not math.isfinite(score)
        or not 0 <= score <= 1
    ):
        raise EnvironmentError("Native grader score is outside [0, 1]")
    if result["passed"] is not None and not isinstance(result["passed"], bool):
        raise EnvironmentError("Native grader passed field must be boolean or null")
    # Native adapters may retain their own additional evidence fields. Normalize
    # absent optional containers without discarding that original report.
    result.setdefault("checks", [])
    result.setdefault("evidence", {})
    if not isinstance(result["checks"], list) or not isinstance(result["evidence"], dict):
        raise EnvironmentError("Native grader checks/evidence have invalid types")
    return result


def _recovery_tar(
    raw: bytes, root: str, *, byte_limit: int, file_limit: int
) -> tuple[dict[str, bytes], list[str]]:
    """Read bounded regular data from Docker's TAR without host extraction."""
    prefix = PurePosixPath(root).name
    files: dict[str, bytes] = {}
    rejected: list[str] = []
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
            members: list[tarfile.TarInfo] = []
            for member in archive:
                if len(members) >= 4096:
                    return {}, ["Archive exceeded the 4096-entry recovery limit"]
                members.append(member)
            names: set[str] = set()
            blocked: set[PurePosixPath] = set()
            for member in members:
                path = PurePosixPath(member.name)
                normalized = member.name.rstrip("/")
                if (
                    not normalized
                    or str(path) != normalized
                    or path.is_absolute()
                    or ".." in path.parts
                    or "\\" in member.name
                    or "\0" in member.name
                    or not path.parts
                    or path.parts[0] != prefix
                ):
                    rejected.append("Unsafe archive path was skipped")
                    blocked.add(path)
                    continue
                if normalized in names:
                    blocked.add(path)
                    rejected.append("Duplicate archive destination was skipped")
                names.add(normalized)
                if (not member.isfile() and not member.isdir()) or member.sparse is not None:
                    blocked.add(path)
                    rejected.append("Link, sparse file or special archive entry was skipped")
            total = 0
            for member in members:
                path = PurePosixPath(member.name)
                if member.isdir() or any(
                    path == bad or path.is_relative_to(bad) for bad in blocked
                ):
                    continue
                if not member.isfile():
                    continue
                if member.size < 0 or member.size > byte_limit - total or len(files) >= file_limit:
                    rejected.append("Artifact exceeded the remaining recovery budget")
                    continue
                relative = PurePosixPath(root.lstrip("/"), *path.parts[1:]).as_posix()
                relative_path(relative)
                stream = archive.extractfile(member)
                if stream is None:
                    rejected.append("Regular archive content was unavailable")
                    continue
                content = stream.read(member.size + 1)
                if len(content) != member.size:
                    rejected.append("Truncated archive content was skipped")
                    continue
                files[relative] = content
                total += len(content)
    except (tarfile.TarError, OSError, ValueError) as exc:
        return {}, [f"Invalid recovery archive: {type(exc).__name__}"]
    return files, list(dict.fromkeys(rejected))


class NativeEnvironment(DockerEnvironment):
    """Candidate and grader use separate fresh instances of one pinned image.

    Unlike the foundation's black-box sandbox, native tools may modify the
    disposable container filesystem as uid 65534. No host filesystem is mounted.
    Only declared artifacts cross into the subsequent native grader instance.
    """

    def __init__(
        self,
        data_dir: Path,
        run_id: str,
        budgets: Budgets,
        task: ImportedTask,
        cancel_event: threading.Event | None = None,
        *,
        deadline: float | None = None,
    ) -> None:
        super().__init__(
            data_dir, run_id, budgets, cancel_event, deadline=deadline, task_id=task.id
        )
        self.task = task
        self.workdir = task.recipe.workdir
        self._frozen: dict[str, bytes] | None = None
        self._network: str | None = None
        self._prepared: dict[str, Any] | None = None
        self._watch_stop = threading.Event()
        self._write_exceeded = threading.Event()
        self._watch_error: str | None = None
        self._watcher: threading.Thread | None = None

    def _operation(
        self,
        args: list[str],
        *,
        input_bytes: bytes | None = None,
        input_path: Path | None = None,
        output_limit: int | None = None,
        timeout: float | None = None,
        on_output: Callable[[str, str], None] | None = None,
    ) -> dict[str, Any]:
        if self._write_exceeded.is_set():
            raise OutputLimitExceeded("Native writable layer exceeded its recorded byte limit")
        if self._watch_error is not None:
            raise EnvironmentError(self._watch_error)
        try:
            result = _docker(
                args,
                timeout=self._remaining(timeout or self.budgets.command_seconds),
                input_bytes=input_bytes,
                input_path=input_path,
                output_limit=output_limit or self.budgets.output_bytes,
                cancel_event=self.cancel_event,
                on_output=on_output,
            )
            if self._write_exceeded.is_set():
                raise OutputLimitExceeded("Native writable layer exceeded its recorded byte limit")
            if self._watch_error is not None:
                raise EnvironmentError(self._watch_error)
            return result
        except (ExecutionCancelled, ExecutionTimeout, OutputLimitExceeded):
            self._abort()
            raise

    def _start(self, *, grader: bool) -> None:
        info = preparation_info(self.data_dir, self.task)
        self._prepared = info
        self.image_id = info["image_id"]
        self.workdir = str(info.get("workdir", self.task.recipe.workdir))
        scope = "grader" if grader else "candidate"
        largest_asset = max(
            (
                registry.asset_root(self.data_dir).joinpath(str(a.sha256)).stat().st_size
                for a in self.task.assets
                if a.scope == scope
            ),
            default=0,
        )
        file_limit = max(67108864, largest_asset + 1)
        labels = [
            "--label",
            f"{MANAGED_LABEL}=1",
            "--label",
            f"{WORKSPACE_LABEL}={workspace_id(self.data_dir)}",
            "--label",
            f"{RUN_LABEL}={self.run_id}",
        ]
        network = "none"
        if self.task.recipe.network == "task-local":
            self._network = "benchmaxxing-native-" + uuid.uuid4().hex
            _require_success(
                self._operation(
                    ["network", "create", "--internal", *labels, self._network], timeout=30
                ),
                "Could not create isolated task-local network",
            )
            network = self._network
        args = [
            "create",
            "--pull",
            "never",
            "--name",
            self.name,
            *labels,
            "--network",
            network,
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
            f"fsize={file_limit}:{file_limit}",
            "--workdir",
            self.workdir,
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=268435456,uid=65534,gid=65534,mode=0700",
            "-e",
            "PYTHONUNBUFFERED=1",
            "-e",
            "HF_HUB_OFFLINE=1",
            "-e",
            "HF_DATASETS_OFFLINE=1",
            "-e",
            "PIP_NO_INDEX=1",
            "-e",
            "PIP_DISABLE_PIP_VERSION_CHECK=1",
            "--init",
            "--entrypoint",
            "python",
            self.image_id,
            "-u",
            "-c",
            "import time; time.sleep(86400)",
        ]
        # Harbor verifier paths exist only in the fresh grader environment.
        if grader and self.task.recipe.family == "harbor":
            insertion = args.index("--init")
            args[insertion:insertion] = [
                "--tmpfs",
                "/tests:rw,nosuid,nodev,size=67108864,uid=65534,gid=65534,mode=0700",
                "--tmpfs",
                "/logs:rw,nosuid,nodev,size=67108864,uid=65534,gid=65534,mode=0700",
            ]
        self._created = True
        try:
            _require_success(self._operation(args, timeout=30), "Could not create native container")
            _require_success(
                self._operation(["start", self.name], timeout=30),
                "Could not start native container",
            )
            if grader:
                for name, text in self.task.private_files.items():
                    self._put("/grader", name, text.encode())
                self._copy_assets("grader")
            else:
                self._put("/workspace", "TASK.md", self.task.prompt.encode())
                for name, text in self.task.public_files.items():
                    self._put("/workspace", name, text.encode())
                self._copy_assets("candidate")
            self._start_watch()
        except BaseException:
            self.close()
            raise

    def _start_watch(self) -> None:
        baseline = self._layer_bytes()
        allowance = int((self._prepared or {}).get("writable_layer_limit_bytes", 268435456))

        def watch() -> None:
            while not self._watch_stop.wait(1):
                try:
                    if self._layer_bytes() > baseline + allowance:
                        self._write_exceeded.set()
                        self._abort()
                        return
                except (EnvironmentError, ValueError, TypeError):
                    # A disappeared/uninspectable runtime cannot continue safely.
                    if not self._watch_stop.is_set():
                        self._watch_error = "Native runtime storage inspection failed"
                        self._abort()
                    return

        self._watcher = threading.Thread(target=watch, daemon=True)
        self._watcher.start()

    def _layer_bytes(self) -> int:
        output = _require_success(
            _docker(
                ["container", "inspect", "--size", "--format", "{{json .SizeRw}}", self.name],
                timeout=10,
                output_limit=4096,
            ),
            "Could not inspect native writable layer",
        )
        size = json.loads(output)
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise EnvironmentError("Docker did not report the native writable layer size")
        return size

    def start(self) -> None:
        self._start(grader=False)

    def _put(self, root: str, name: str, payload: bytes | Path) -> None:
        relative_path(name)
        inputs: dict[str, Any] = (
            {"input_path": payload} if isinstance(payload, Path) else {"input_bytes": payload}
        )
        _require_success(
            self._operation(
                ["exec", "-i", self.name, "python", "-I", "-c", _WRITE_ABSOLUTE, root, name],
                timeout=300,
                **inputs,
            ),
            "Could not transfer native task asset",
        )

    def _copy_assets(self, scope: str) -> None:
        target = "/workspace" if scope == "candidate" else "/grader"
        for asset in self.task.assets:
            if asset.scope != scope:
                continue
            if asset.sha256 is None:
                raise PreparationError("Source assets were not imported")
            path = registry.asset_root(self.data_dir) / asset.sha256
            self._put(target, asset.target, path)
            if (
                scope == "grader"
                and self.task.recipe.family == "harbor"
                and asset.target.startswith("tests/")
            ):
                self._put("/tests", asset.target.removeprefix("tests/"), path)

    def execute(
        self, command: str, *, on_output: Callable[[str, str], None] | None = None
    ) -> dict[str, Any]:
        if self._frozen is not None:
            raise EnvironmentError("The native submission has already been frozen")
        if "\0" in command or len(command.encode()) > self.budgets.output_bytes:
            raise ValueError("Command is invalid or exceeds input limit")
        return self._operation(
            ["exec", "-w", self.workdir, self.name, "sh", "-lc", command],
            on_output=on_output,
        )

    def write_file(self, path: str, content: str) -> None:
        if self._frozen is not None:
            raise EnvironmentError("The native submission has already been frozen")
        super().write_file(path, content)

    def _artifact_roots(self) -> list[str]:
        if self.task.submission_kind == "workspace":
            declared = self.task.recipe.settings.get("artifacts")
            if (
                not isinstance(declared, list)
                or not declared
                or any(not isinstance(p, str) for p in declared)
            ):
                raise PreparationError("Workspace tasks require declared bounded artifact roots")
            roots = [_absolute(p) for p in declared]
        else:
            roots = ["/workspace/" + relative_path(self.task.submission_file)]
        compiled = self.task.recipe.settings.get("compiled_submission_file")
        if compiled is not None:
            if not isinstance(compiled, str):
                raise PreparationError("Invalid compiled submission filename")
            roots.append("/workspace/" + relative_path(compiled))
        return roots

    def freeze(self) -> dict[str, bytes]:
        if self._frozen is not None:
            return dict(self._frozen)
        if self.task.recipe.candidate_command:
            result = self._operation(
                ["exec", "-w", self.workdir, self.name, *self.task.recipe.candidate_command]
            )
            _require_success(result, "Could not materialize submitted artifact")
        roots = self._artifact_roots()
        output = _require_tool_success(
            self._operation(
                [
                    "exec",
                    self.name,
                    "python",
                    "-I",
                    "-c",
                    _SNAPSHOT,
                    json.dumps(roots),
                    str(self.budgets.artifact_bytes),
                ],
                output_limit=self.budgets.artifact_bytes * 2 + 1048576,
            ),
            "Could not freeze declared native artifacts",
        )
        try:
            values = json.loads(output)
            frozen = {
                relative_path(name): base64.b64decode(value, validate=True)
                for name, value in values.items()
            }
        except (ValueError, TypeError, AttributeError) as exc:
            raise EnvironmentError("Invalid native artifact transfer") from exc
        self._check_frozen(frozen)
        self._frozen = frozen
        return dict(frozen)

    def recover_submission(self) -> tuple[dict[str, bytes], dict[str, Any]]:
        """Recover inert partial outputs after stopping this owned attempt.

        This has a separate 20-second cleanup allowance. It never runs the
        candidate's materialization command or any code in the container.
        """
        if self._frozen is not None:
            return dict(self._frozen), {"status": "recovered", "source": "previously-frozen"}
        recovery_limit = min(self.budgets.artifact_bytes, 16 * 1024 * 1024)
        report: dict[str, Any] = {
            "status": "unavailable",
            "roots": [],
            "byte_limit": recovery_limit,
            "timeout_seconds": 20,
        }
        files: dict[str, bytes] = {}
        if not self._created:
            report["reason"] = "Candidate container was not created"
            return files, report
        recovery_deadline = time.monotonic() + 20
        self._watch_stop.set()

        def operation(args: list[str], **kwargs: Any) -> dict[str, Any]:
            return _docker(args, timeout=min(5, recovery_deadline - time.monotonic()), **kwargs)

        try:
            inspected = json.loads(
                _require_success(
                    operation(["inspect", "--format", "{{json .}}", self.name], output_limit=65536),
                    "Could not inspect owned recovery container",
                )
            )
            labels = inspected.get("Config", {}).get("Labels") or {}
            if any(
                labels.get(key) != value
                for key, value in {
                    MANAGED_LABEL: "1",
                    WORKSPACE_LABEL: workspace_id(self.data_dir),
                    RUN_LABEL: self.run_id,
                }.items()
            ):
                raise EnvironmentError("Recovery container ownership did not match")
            if inspected.get("State", {}).get("Running"):
                _require_success(
                    operation(["kill", self.name], output_limit=4096),
                    "Could not stop owned recovery container",
                )
            inspected = json.loads(
                _require_success(
                    operation(
                        ["inspect", "--format", "{{json .State}}", self.name], output_limit=4096
                    ),
                    "Could not verify stopped recovery container",
                )
            )
            if inspected.get("Running") or inspected.get("Paused"):
                raise EnvironmentError("Recovery container is not stopped")
            for root in self._artifact_roots():
                entry: dict[str, Any] = {"path": root, "status": "unavailable"}
                report["roots"].append(entry)
                # Docker cp can follow intermediate symlinks. Direct children of
                # the image's fixed top-level directories avoid that ambiguity.
                if len(PurePosixPath(root).parts) > 3:
                    entry["reason"] = "Nested roots cannot be verified without executing code"
                    continue
                remaining = recovery_limit - sum(map(len, files.values()))
                if remaining < 0 or len(files) >= 4096:
                    entry["reason"] = "Recovery artifact budget exhausted"
                    continue
                copied = operation(
                    ["cp", f"{self.name}:{root}", "-"],
                    binary_stdout=True,
                    output_limit=remaining + 4096 * 2048 + 10240,
                )
                if copied["exit_code"] != 0:
                    entry["reason"] = "Declared artifact is missing or unavailable"
                    continue
                captured, skipped = _recovery_tar(
                    base64.b64decode(copied["stdout_base64"], validate=True),
                    root,
                    byte_limit=remaining,
                    file_limit=4096 - len(files),
                )
                self._check_frozen(captured)
                files.update(captured)
                entry.update(
                    status="recovered" if captured else "unavailable",
                    files=len(captured),
                    bytes=sum(map(len, captured.values())),
                )
                if skipped:
                    entry["skipped"] = skipped
                    if captured:
                        entry["status"] = "partial"
            report["status"] = (
                "recovered"
                if files and all(e["status"] == "recovered" for e in report["roots"])
                else "partial"
                if files
                else "unavailable"
            )
        except (EnvironmentError, ValueError, KeyError, TypeError) as exc:
            report["reason"] = str(exc)[:500]
            report["status"] = "partial" if files else "unavailable"
        return files, report

    def _check_frozen(self, frozen: dict[str, bytes]) -> None:
        if (
            len(frozen) > 4096
            or sum(len(value) for value in frozen.values()) > self.budgets.artifact_bytes
        ):
            raise OutputLimitExceeded("Native snapshot exceeds artifact budget")
        roots = [PurePosixPath(p) for p in self._artifact_roots()]
        for name, value in frozen.items():
            relative_path(name)
            path = PurePosixPath("/" + name)
            if not isinstance(value, bytes) or not any(
                path == root or path.is_relative_to(root) for root in roots
            ):
                raise EnvironmentError("Frozen artifact is outside declared submission roots")

    def collect(self) -> bytes:
        frozen = self.freeze()
        if self.task.submission_kind != "workspace":
            filename = self.task.recipe.settings.get(
                "compiled_submission_file", self.task.submission_file
            )
            return frozen["workspace/" + filename]
        # An archive is evidence only. It is never extracted or executed on the host.
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            for name, content in sorted(frozen.items()):
                entry = tarfile.TarInfo(name)
                entry.size = len(content)
                entry.mode = 0o644
                archive.addfile(entry, io.BytesIO(content))
        payload = stream.getvalue()
        if len(payload) > self.budgets.artifact_bytes:
            raise OutputLimitExceeded("Native artifact archive exceeds artifact budget")
        return payload

    def grade(
        self, frozen: dict[str, bytes], *, on_output: Callable[[str, str], None] | None = None
    ) -> dict[str, Any]:
        """May be called after close(); never reuse or commit the candidate container."""
        self._check_frozen(frozen)
        grader = NativeEnvironment(
            self.data_dir,
            self.run_id,
            self.budgets,
            self.task,
            self.cancel_event,
            deadline=self.deadline,
        )
        try:
            grader._start(grader=True)
            for name, content in frozen.items():
                absolute = PurePosixPath("/" + name)
                # All paths were validated against explicit task roots above.
                grader._put("/" + absolute.parts[1], "/".join(absolute.parts[2:]), content)
            execution = grader._operation(
                ["exec", "-w", grader.workdir, grader.name, *self.task.recipe.grader_command],
                timeout=grader._remaining(),
                on_output=on_output,
            )
            if execution["exit_code"] != 0:
                raise EnvironmentError(
                    "Native grader failed: " + (execution["stderr"] or execution["stdout"])[-2000:]
                )
            path = self.task.recipe.grader_result_path
            if path is None or not path.startswith("/") or ".." in PurePosixPath(path).parts:
                raise PreparationError("Native grader result path must be absolute")
            output = _require_tool_success(
                grader._operation(
                    [
                        "exec",
                        grader.name,
                        "python",
                        "-I",
                        "-c",
                        _READ_ABSOLUTE,
                        path,
                        str(self.budgets.artifact_bytes),
                    ],
                    output_limit=self.budgets.artifact_bytes * 2 + 4096,
                ),
                "Native grader did not produce results",
            )
            raw = base64.b64decode(output.strip(), validate=True)
            if self.task.recipe.grader_format == "harbor-reward":
                score = float(raw.decode().strip())
                if not math.isfinite(score) or not 0 <= score <= 1:
                    raise EnvironmentError("Native verifier reward is outside [0, 1]")
                return {
                    "passed": bool(score) if score in (0, 1) else None,
                    "score": score,
                    "checks": [],
                    "evidence": {
                        "native_reward": score,
                        "grader_format": "harbor-reward",
                        "track": "native-grader",
                    },
                }
            return _native_result(raw)
        finally:
            grader.close()

    def close(self) -> None:
        self._watch_stop.set()
        if self._watcher is not None:
            self._watcher.join(timeout=12)
            self._watcher = None
        try:
            super().close()
        finally:
            if self._network is not None:
                result = _docker(["network", "rm", self._network], timeout=15, output_limit=8192)
                if result["exit_code"] != 0 and "not found" not in result["stderr"]:
                    _require_success(result, "Could not remove owned native network")
                self._network = None
