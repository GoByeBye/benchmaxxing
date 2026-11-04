"""Preparation validation and optional real Docker native-isolation checks."""

import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any

import pytest

from benchmaxxing.contracts import Budgets
from benchmaxxing.environment import (
    EnvironmentError,
    ExecutionCancelled,
    OutputLimitExceeded,
    PreparationError,
)
from benchmaxxing.providers import preparation, registry
from benchmaxxing.providers.coding import convert_coding
from benchmaxxing.providers.contracts import ImportedTask
from benchmaxxing.providers.sandbox import NativeEnvironment, _native_result


@pytest.fixture
def native_task(tmp_path: Path) -> ImportedTask:
    """Explicitly named fake benchmark; its solution runs only in Docker tests."""
    row = {
        "task_id": "HumanEval/0",
        "prompt": 'def answer(x):\n    """Add one."""\n',
        "entry_point": "answer",
        "canonical_solution": "    return x + 1\n",
        "test": "def check(candidate):\n    assert candidate(1) == 2\n",
    }
    source = tmp_path / "fake-source.json"
    source.write_text(json.dumps(row))
    (task,) = convert_coding(
        "evalplus-humaneval",
        source,
        revision="fake-test-v1",
        source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
    )
    registry.install(tmp_path, source, [task])
    return task


@pytest.fixture
def fake_docker(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    image = {
        "Id": "sha256:" + "a" * 64,
        "Os": "linux",
        "Size": 1024,
        "Architecture": "amd64",
        "RepoDigests": [],
    }
    probes = {
        "modules": {"numpy": {"file": "/image/numpy/__init__.py"}},
        "distributions": {"numpy": {"version": "1.26.4", "direct_url": {}}},
        "commands": {"python": "/image/python"},
    }
    monkeypatch.setattr(preparation, "_inspect_image", lambda image_name: image)
    monkeypatch.setattr(
        preparation,
        "_docker",
        lambda args, **kwargs: {"exit_code": 0, "stdout": json.dumps(probes), "stderr": ""},
    )
    return probes


def test_preparation_records_verified_image_and_asset_identity(
    tmp_path: Path, native_task: ImportedTask, fake_docker: dict[str, Any]
) -> None:
    record = preparation.prepare_imported(tmp_path, native_task, image="fake-runtime:1")
    assert record["image_id"] == "sha256:" + "a" * 64
    assert record["sandbox_profile"] == "native-unprivileged-writable-v1"
    assert record["probes"] == fake_docker
    assert preparation.preparation_info(tmp_path, native_task) == record


def test_wrong_dependency_version_never_becomes_prepared(
    tmp_path: Path, native_task: ImportedTask, fake_docker: dict[str, Any]
) -> None:
    fake_docker["distributions"]["numpy"]["version"] = "0.0.1"
    with pytest.raises(PreparationError, match="declared dependency"):
        preparation.prepare_imported(tmp_path, native_task, image="fake-runtime:1")
    assert registry.prepared_record(tmp_path, native_task) is None


def test_exact_native_source_proof_is_required_and_preserved(
    tmp_path: Path, native_task: ImportedTask, fake_docker: dict[str, Any]
) -> None:
    expected = {"/opt/grader/scorer.py": "a" * 64}
    native_task.recipe.settings["probe_file_sha256"] = expected
    with pytest.raises(PreparationError, match="source hash proof"):
        preparation.prepare_imported(tmp_path, native_task, image="fake-runtime:1")
    fake_docker["file_sha256"] = expected
    record = preparation.prepare_imported(tmp_path, native_task, image="fake-runtime:1")
    assert record["probes"]["file_sha256"] == expected
    record["build_provenance"] = {"source_commit": "b" * 40}
    registry._atomic_json(registry.root(tmp_path) / "prepared" / f"{native_task.id}.json", record)
    checked = preparation.prepare_imported(tmp_path, native_task, image="fake-runtime:1")
    assert checked["build_provenance"] == record["build_provenance"]


def test_blocker_cannot_be_bypassed_by_existing_image(
    tmp_path: Path, native_task: ImportedTask, fake_docker: dict[str, Any]
) -> None:
    native_task.recipe.blockers = ["A grading asset is missing"]
    with pytest.raises(PreparationError, match="grading asset"):
        preparation.prepare_imported(tmp_path, native_task, image="fake-runtime:1")
    assert registry.prepared_record(tmp_path, native_task) is None


def test_full_filesystem_and_external_network_tasks_remain_blocked(
    tmp_path: Path, native_task: ImportedTask, fake_docker: dict[str, Any]
) -> None:
    native_task.recipe.settings["requires_filesystem_snapshot"] = True
    with pytest.raises(PreparationError, match="filesystem snapshot"):
        preparation.prepare_imported(tmp_path, native_task, image="fake-runtime:1")
    native_task.recipe.settings.pop("requires_filesystem_snapshot")
    native_task.recipe.network = "external"
    with pytest.raises(PreparationError, match="external networking"):
        preparation.prepare_imported(tmp_path, native_task, image="fake-runtime:1")


def test_unbounded_image_pull_is_rejected_before_download(
    tmp_path: Path, native_task: ImportedTask, fake_docker: dict[str, Any]
) -> None:
    with pytest.raises(PreparationError, match="max_image_bytes"):
        preparation.prepare_imported(tmp_path, native_task, image="fake-runtime:1", pull=True)


def test_frozen_paths_cannot_replace_grader_or_escape_submission_root(
    tmp_path: Path, native_task: ImportedTask
) -> None:
    env = NativeEnvironment(tmp_path, "test-ownership", Budgets(), native_task)
    for name in (
        "grader/grade.py",
        "workspace/../grader/grade.py",
        "etc/passwd",
        "/workspace/solution.py",
    ):
        with pytest.raises((ValueError, RuntimeError)):
            env._check_frozen({name: b"bad"})
    env._check_frozen({"workspace/solution.py": b"valid captured data"})


def test_snapshot_byte_limit_applies_before_restore(
    tmp_path: Path, native_task: ImportedTask
) -> None:
    env = NativeEnvironment(tmp_path, "test-budget", Budgets(artifact_bytes=1024), native_task)
    with pytest.raises(OutputLimitExceeded):
        env._check_frozen({"workspace/solution.py": b"x" * 1025})


def test_partial_suite_preparation_preserves_successful_pins(
    tmp_path: Path, native_task: ImportedTask, fake_docker: dict[str, Any]
) -> None:
    from copy import deepcopy

    blocked = deepcopy(native_task)
    blocked.upstream_id = "HumanEval/blocked"
    blocked.recipe.blockers = ["Missing verified source asset"]
    result = preparation.prepare_suite(tmp_path, [native_task, blocked], image="fake-runtime:1")
    assert result["prepared_tasks"] == 1
    assert result["task_ids"] == [native_task.id]
    assert result["errors"] == {blocked.id: "Missing verified source asset"}
    assert registry.prepared_record(tmp_path, native_task) is not None
    assert registry.prepared_record(tmp_path, blocked) is None


@pytest.mark.parametrize(
    "raw",
    [
        b"{}",
        b'{"passed":true,"score":0,"score":1,"checks":[],"evidence":{}}',
        b'{"passed":true,"score":1,"checks":[],"evidence":{"x":NaN}}',
        b'{"passed":1,"score":1,"checks":[],"evidence":{}}',
        b'{"passed":true,"score":1,"checks":{},"evidence":{}}',
    ],
)
def test_native_result_rejects_ambiguous_or_malformed_evidence(raw: bytes) -> None:
    with pytest.raises(EnvironmentError):
        _native_result(raw)


def test_native_result_preserves_ungraded_and_continuous_outcomes() -> None:
    for score in (None, 0.25):
        result = {"passed": None, "score": score, "checks": [], "evidence": {"native": True}}
        assert _native_result(json.dumps(result).encode()) == result


native_docker = pytest.mark.skipif(
    os.environ.get("BENCHMAXXING_NATIVE_DOCKER_TESTS") != "1",
    reason="Build examples/providers/evalplus-frozen.Dockerfile and enable native Docker tests",
)


@native_docker
@pytest.mark.docker
def test_native_fresh_grader_good_bad_and_early_exit(
    tmp_path: Path, native_task: ImportedTask
) -> None:
    preparation.prepare_imported(tmp_path, native_task, image="benchmaxxing/evalplus-frozen:1")
    for label, source, expected in (
        ("good", native_task.good_solution, True),
        ("bad", native_task.bad_solution, False),
        ("early", "import os; os._exit(0)\n", False),
    ):
        env = NativeEnvironment(
            tmp_path, label, Budgets(memory_mb=512, wall_seconds=120), native_task
        )
        try:
            env.start()
            inspection = env.execute(
                "test ! -e /grader/tests.py && test ! -e /var/run/docker.sock && id -u"
            )
            assert inspection["exit_code"] == 0
            assert inspection["stdout"].strip() == "65534"
            assert source is not None
            env.write_file("solution.py", source)
            frozen = env.freeze()
        finally:
            env.close()
        result = env.grade(frozen)
        assert result["passed"] is expected


@native_docker
@pytest.mark.docker
def test_native_live_output_and_cancel_terminate_candidate(
    tmp_path: Path, native_task: ImportedTask
) -> None:
    preparation.prepare_imported(tmp_path, native_task, image="benchmaxxing/evalplus-frozen:1")
    cancel = threading.Event()
    env = NativeEnvironment(tmp_path, "cancel", Budgets(memory_mb=512), native_task, cancel)
    output = []
    try:
        env.start()

        def on_output(stream: str, text: str) -> None:
            output.append(text)
            cancel.set()

        with pytest.raises(ExecutionCancelled):
            env.execute(
                "python -u -c \"import time; print('live'); time.sleep(60)\"", on_output=on_output
            )
        assert "live" in "".join(output)
    finally:
        env.close()


@native_docker
@pytest.mark.docker
def test_native_writable_layer_budget_stops_output_flood(
    tmp_path: Path, native_task: ImportedTask
) -> None:
    native_task.recipe.settings["writable_layer_limit_bytes"] = 16 * 1024 * 1024
    preparation.prepare_imported(tmp_path, native_task, image="benchmaxxing/evalplus-frozen:1")
    env = NativeEnvironment(tmp_path, "disk-budget", Budgets(memory_mb=512), native_task)
    try:
        env.start()
        with pytest.raises(OutputLimitExceeded):
            env.execute(
                "python -c \"import time; open('/workspace/large','wb').write(b'x'*20000000); "
                'time.sleep(10)"'
            )
    finally:
        env.close()
