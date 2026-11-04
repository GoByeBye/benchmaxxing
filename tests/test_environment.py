import json
import sys
import threading
from pathlib import Path

import pytest

from benchmaxxing import environment
from benchmaxxing.contracts import Budgets
from benchmaxxing.environment import (
    DockerEnvironment,
    ExecutionCancelled,
    ExecutionTimeout,
    OutputLimitExceeded,
    PreparationError,
    _bounded_process,
)
from benchmaxxing.task import get_task, reference_files


def test_task_hash_tracks_public_assets():
    assert set(reference_files()) == {"TASK.md", "solution.py", "public_tests.py"}
    assert len(get_task().task_hash) == 64
    assert get_task().task_hash == get_task().task_hash


def test_missing_preparation_never_calls_docker(tmp_path, monkeypatch):
    monkeypatch.setattr(
        environment, "_docker", lambda *a, **k: pytest.fail("Unexpected Docker call")
    )
    with pytest.raises(PreparationError, match="prepare"):
        environment.preparation_info(tmp_path)


def test_preparation_records_exact_identity_and_run_only_inspects(tmp_path, monkeypatch):
    calls = []
    image_id = "sha256:" + "a" * 64

    def docker(args, **kwargs):
        calls.append(args)
        return {
            "exit_code": 0,
            "stdout": json.dumps(
                {
                    "Id": image_id,
                    "Os": "linux",
                    "Architecture": "amd64",
                    "RepoDigests": [],
                }
            ),
            "stderr": "",
        }

    monkeypatch.setattr(environment, "_docker", docker)
    prepared = environment.prepare(tmp_path)
    assert prepared["image_id"] == image_id
    assert prepared["task_hash"] == get_task().task_hash
    calls.clear()
    assert environment.preparation_info(tmp_path) == prepared
    assert all(call[:2] == ["image", "inspect"] for call in calls)


@pytest.mark.parametrize(
    "path", ["", "/etc/passwd", "../secret", "nested/../../secret", "C:\\secret", "a\0b"]
)
def test_workspace_path_validation(path):
    with pytest.raises(ValueError):
        DockerEnvironment._validate_path(path)


def test_preparation_rejects_image_flags(tmp_path):
    with pytest.raises(PreparationError, match="Invalid"):
        environment.prepare(tmp_path, "--privileged")


def test_bounded_output_stops_flood():
    with pytest.raises(OutputLimitExceeded):
        _bounded_process(
            [sys.executable, "-c", "import sys; sys.stdout.write('x'*1000000)"],
            timeout=5,
            output_limit=1024,
        )


def test_bounded_process_timeout():
    with pytest.raises(ExecutionTimeout):
        _bounded_process([sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.1)


def test_cancelled_process_never_starts():
    cancellation = threading.Event()
    cancellation.set()
    with pytest.raises(ExecutionCancelled):
        _bounded_process(["nonexistent-executable"], timeout=1, cancel_event=cancellation)


def test_workspace_id_stable_and_separate(tmp_path):
    assert environment.workspace_id(tmp_path) == environment.workspace_id(tmp_path / ".")
    assert environment.workspace_id(tmp_path) != environment.workspace_id(tmp_path / "another")


def test_container_create_uses_restricted_flags_and_pinned_image(tmp_path, monkeypatch):
    calls = []
    image_id = "sha256:" + "b" * 64
    monkeypatch.setattr(
        environment, "preparation_info", lambda path, task_id: {"image_id": image_id}
    )

    def docker(args, **kwargs):
        calls.append(args)
        return {"exit_code": 0, "stdout": "", "stderr": "", "duration_seconds": 0}

    monkeypatch.setattr(environment, "_docker", docker)
    task = DockerEnvironment(tmp_path, "test-run", Budgets())
    task.start()
    task.close()
    create = calls[0]
    for flag, value in [
        ("--pull", "never"),
        ("--network", "none"),
        ("--user", "65534:65534"),
        ("--cap-drop", "ALL"),
    ]:
        assert create[create.index(flag) + 1] == value
    assert "--read-only" in create
    assert image_id in create
    assert "--privileged" not in create
    assert "--volume" not in create


def test_cleanup_filters_workspace_and_run(tmp_path, monkeypatch):
    calls = []

    def docker(args, **kwargs):
        calls.append(args)
        return {"exit_code": 0, "stdout": "abc123abc123\n" if args[0] == "ps" else "", "stderr": ""}

    monkeypatch.setattr(environment, "_docker", docker)
    assert environment.cleanup_workspace(tmp_path, "run-one") == ["abc123abc123"]
    assert f"label={environment.WORKSPACE_LABEL}={environment.workspace_id(tmp_path)}" in calls[0]
    assert f"label={environment.RUN_LABEL}=run-one" in calls[0]
    assert calls[1] == ["rm", "-f", "abc123abc123"]


def test_artifact_input_limit(tmp_path):
    task = DockerEnvironment(Path(tmp_path), "run", Budgets(artifact_bytes=1024))
    with pytest.raises(OutputLimitExceeded):
        task.write_file("solution.py", "a" * 1025)
