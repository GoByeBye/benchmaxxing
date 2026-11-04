"""Stopped-container recovery reads data only; no candidate code runs on the host."""

from __future__ import annotations

import base64
import io
import json
import os
import sys
import tarfile
import threading
from pathlib import Path
from typing import Any

import pytest

from benchmaxxing.contracts import Budgets, InferenceProfile, RunSpec
from benchmaxxing.environment import (
    OutputLimitExceeded,
    PreparationError,
    _bounded_process,
    _docker,
    workspace_id,
)
from benchmaxxing.model import ModelResponse, ToolCall
from benchmaxxing.providers import preparation, registry, runner, sandbox
from benchmaxxing.providers.contracts import ImportedTask, NativeRecipe
from benchmaxxing.storage import Store


def archive(entries: list[tuple[str, bytes, bytes]]) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as output:
        for name, content, kind in entries:
            entry = tarfile.TarInfo(name)
            entry.type = kind
            if kind == tarfile.REGTYPE:
                entry.size = len(content)
            elif kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                entry.linkname = "/private/grader"
            output.addfile(entry, io.BytesIO(content) if entry.isfile() else None)
    return stream.getvalue()


def task() -> ImportedTask:
    return ImportedTask(
        suite_id="terminal-bench-2-1",
        upstream_id="fake-recovery-fixture",
        revision="fake-v1",
        source_sha256="a" * 64,
        title="Recovery fixture",
        prompt="Write output.",
        category="terminal",
        submission_file="solution.py",
        recipe=NativeRecipe(
            family="recovery-fixture",
            workdir="/workspace",
            candidate_command=["sh", "-c", "exit 99"],
            grader_command=["false"],
            grader_result_path="/grader/result.json",
        ),
    )


def test_binary_stdout_preserves_invalid_utf8_and_does_not_stream_binary() -> None:
    chunks = []
    result = _bounded_process(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(bytes(range(256))); "
            "sys.stderr.write('diagnostic')",
        ],
        timeout=5,
        binary_stdout=True,
        on_output=lambda stream, text: chunks.append((stream, text)),
    )
    assert result["stdout"] == ""
    assert base64.b64decode(result["stdout_base64"]) == bytes(range(256))
    assert chunks == [("stderr", "diagnostic")]


def test_binary_stdout_keeps_the_combined_output_limit() -> None:
    with pytest.raises(OutputLimitExceeded):
        _bounded_process(
            [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'x'*2000)"],
            timeout=5,
            binary_stdout=True,
            output_limit=1024,
        )


@pytest.mark.parametrize("root", ["/usr", "/bin", "/sbin", "/lib", "/lib64", "/opt"])
def test_artifact_roots_cannot_restore_over_grader_runtimes(root: str) -> None:
    with pytest.raises(PreparationError):
        sandbox._absolute(root)


def test_recovery_tar_keeps_regular_binary_and_skips_unsafe_entries() -> None:
    data = archive(
        [
            ("out", b"", tarfile.DIRTYPE),
            ("out/good.bin", b"\x00\xff\x80", tarfile.REGTYPE),
            ("out/link", b"", tarfile.SYMTYPE),
            ("out/hardlink", b"", tarfile.LNKTYPE),
            ("out/device", b"", tarfile.CHRTYPE),
            ("out/../../escape", b"bad", tarfile.REGTYPE),
            ("/absolute", b"bad", tarfile.REGTYPE),
            ("out/too-big", b"x" * 1025, tarfile.REGTYPE),
            ("other-root/file", b"bad", tarfile.REGTYPE),
        ]
    )
    files, skipped = sandbox._recovery_tar(data, "/app/out", byte_limit=1024, file_limit=8)
    assert files == {"app/out/good.bin": b"\x00\xff\x80"}
    assert len(skipped) == 3


def test_recovery_tar_rejects_duplicate_paths_and_truncation() -> None:
    raw = archive(
        [("solution.py", b"one", tarfile.REGTYPE), ("solution.py", b"two", tarfile.REGTYPE)]
    )
    files, rejected = sandbox._recovery_tar(
        raw, "/workspace/solution.py", byte_limit=1024, file_limit=4
    )
    assert not files and "Duplicate" in rejected[0]
    files, rejected = sandbox._recovery_tar(
        raw[:513], "/workspace/solution.py", byte_limit=1024, file_limit=4
    )
    assert not files and rejected


@pytest.mark.parametrize("owned", [True, False])
def test_stopped_recovery_ignores_cancel_but_verifies_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, owned: bool
) -> None:
    cancel = threading.Event()
    cancel.set()
    env = sandbox.NativeEnvironment(tmp_path, "recovery", Budgets(), task(), cancel, deadline=1)
    env._created = True
    calls = []
    payload = archive([("solution.py", b"partial", tarfile.REGTYPE)])

    def docker(args: list[str], **kwargs: Any) -> dict[str, Any]:
        calls.append(args)
        assert "cancel_event" not in kwargs
        if args[0] == "inspect":
            value = {"Running": True} if len(calls) == 1 else {"Running": False}
            if len(calls) == 1:
                value = {
                    "State": value,
                    "Config": {
                        "Labels": {
                            sandbox.MANAGED_LABEL: "1",
                            sandbox.RUN_LABEL: "recovery",
                            sandbox.WORKSPACE_LABEL: workspace_id(tmp_path)
                            if owned
                            else "different",
                        }
                    },
                }
            return {"exit_code": 0, "stdout": json.dumps(value), "stderr": ""}
        if args[0] == "cp":
            assert kwargs["binary_stdout"]
            return {
                "exit_code": 0,
                "stdout_base64": base64.b64encode(payload).decode(),
                "stderr": "",
            }
        assert args == ["kill", env.name]
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(sandbox, "_docker", docker)
    files, report = env.recover_submission()
    assert report["status"] == ("recovered" if owned else "unavailable")
    assert files == ({"workspace/solution.py": b"partial"} if owned else {})
    assert all(call[0] != "exec" for call in calls)
    if not owned:
        assert len(calls) == 1


@pytest.mark.docker
@pytest.mark.skipif(
    os.environ.get("BENCHMAXXING_NATIVE_DOCKER_TESTS") != "1",
    reason="Requires an explicitly prepared local native image",
)
@pytest.mark.parametrize("failure", ["cancel", "timeout", "symlink"])
def test_real_interrupted_runner_recovers_submission_and_cleans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    imported = task()
    registry.install(tmp_path, tmp_path, [imported])
    preparation.prepare_imported(
        tmp_path,
        imported,
        image=os.environ.get("BENCHMAXXING_NATIVE_IMAGE", "benchmaxxing/evalplus-frozen:1"),
    )
    cancel = threading.Event()
    store = Store(tmp_path)
    original_append = store.append_event
    expected = "partial output: \u03c0\n"

    def append(*args: Any, **kwargs: Any) -> Any:
        value = original_append(*args, **kwargs)
        if failure in {"cancel", "symlink"} and len(args) > 1 and args[1] == "tool.output":
            cancel.set()
        return value

    class Redactor:
        def feed(self, text: str) -> str:
            return text

        def finish(self) -> str:
            return ""

    class Adapter:
        def __init__(self, *args: Any, **kwargs: Any):
            pass

        def redact(self, value: Any) -> Any:
            return value

        def new_text_redactor(self) -> Redactor:
            return Redactor()

        def complete(self, *args: Any, **kwargs: Any) -> ModelResponse:
            command = "python -u -c \"import time; print('ready'); time.sleep(60)\""
            if failure == "symlink":
                command = (
                    "rm /workspace/solution.py && ln -s /etc/passwd /workspace/solution.py && "
                    + command
                )
            return ModelResponse(
                content=None,
                model="fake-recovery-model",
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCall(
                        "write",
                        "write_file",
                        json.dumps({"path": "solution.py", "content": expected}),
                    ),
                    ToolCall(
                        "wait",
                        "execute",
                        json.dumps({"command": command}),
                    ),
                ],
            )

    monkeypatch.setattr(store, "append_event", append)
    monkeypatch.setattr(runner, "OpenAIAdapter", Adapter)
    record = store.create_run(
        RunSpec(
            task_id=imported.id,
            participant="openai",
            inference=InferenceProfile(model="fake", base_url="http://127.0.0.1:1/v1"),
            budgets=Budgets(memory_mb=512, wall_seconds=120, command_seconds=5),
        )
    )
    result = runner.execute_imported(store, record.id, cancel)
    assert result.status == ("budget_exhausted" if failure == "timeout" else "cancelled"), (
        result.error
    )
    assert result.score is None and result.metadata["submitted"] is False
    if failure == "symlink":
        assert result.metadata["partial_submission"] is False
        assert result.metadata["submission_recovery"]["status"] == "unavailable"
        assert not store.artifacts(record.id)
    else:
        assert result.metadata["partial_submission"] is True
        assert result.metadata["submission_recovery"]["status"] == "recovered"
        saved = next(a for a in store.artifacts(record.id) if a.name == "workspace/solution.py")
        assert store.artifact_content(record.id, saved.id)[1] == expected.encode()
    assert any(event.kind == "submission.recovery" for event in store.events(record.id))
    remaining = _docker(
        ["ps", "-aq", "--filter", f"label={sandbox.WORKSPACE_LABEL}={workspace_id(tmp_path)}"]
    )
    assert remaining["exit_code"] == 0 and remaining["stdout"].strip() == ""
