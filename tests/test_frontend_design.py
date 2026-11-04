"""Original frontend tasks exercise browser behavior and preserve review evidence."""

import base64
import hashlib
import os
import threading
from pathlib import Path

import pytest

from benchmaxxing.contracts import Budgets, RunSpec
from benchmaxxing.engine import execute_run
from benchmaxxing.environment import EnvironmentError, OutputLimitExceeded
from benchmaxxing.providers import registry
from benchmaxxing.providers.frontend import (
    VERSION,
    convert_frontend,
    fetch_frontend_source,
    prepare_frontend_runtime,
)
from benchmaxxing.providers.runner import _grading_artifacts
from benchmaxxing.providers.sandbox import NativeEnvironment
from benchmaxxing.storage import Store


def installed(directory):
    fetched = fetch_frontend_source(directory)
    source = Path(fetched["source_path"])
    tasks = convert_frontend(source, revision=VERSION, source_sha256=fetched["source_sha256"])
    registry.install(directory, source, tasks)
    return tasks


def test_frontend_sources_are_original_and_private_checks_are_separated(tmp_path):
    tasks = installed(tmp_path)
    assert len(tasks) == 6
    assert len({task.id for task in tasks}) == 6
    for task in tasks:
        assert task.category == "frontend"
        assert task.recipe.network == "none"
        assert "frontend_grader.py" in task.private_files
        assert "frontend_grader.py" not in task.public_files
        assert task.good_solution != task.bad_solution
        assert "Visual polish is reviewed separately" in task.prompt
        assert task.recipe.settings["record_grading_artifacts"] is True
    assert fetch_frontend_source(tmp_path)["source_sha256"] == tasks[0].source_sha256


def png_artifact():
    raw = b"\x89PNG\r\n\x1a\n" + b"\0" * 8 + (390).to_bytes(4, "big") + (844).to_bytes(4, "big")
    return {
        "name": "mobile.png",
        "media_type": "image/png",
        "base64": base64.b64encode(raw).decode(),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


@pytest.mark.parametrize(
    "change",
    [
        {"name": "../mobile.png"},
        {"name": "bad\\mobile.png"},
        {"media_type": "image/svg+xml"},
        {"base64": "%%%"},
        {"sha256": "0" * 64},
    ],
)
def test_grading_artifact_protocol_rejects_untrusted_metadata(change):
    item = png_artifact() | change
    with pytest.raises(EnvironmentError):
        _grading_artifacts({"artifacts": [item]}, 4096)


def test_grading_artifacts_validate_hash_and_combined_budget():
    item = png_artifact()
    decoded = _grading_artifacts({"artifacts": [item]}, 4096)
    assert hashlib.sha256(decoded["mobile.png"]).hexdigest() == item["sha256"]
    with pytest.raises(EnvironmentError):
        _grading_artifacts({"artifacts": [item, item]}, 4096)
    with pytest.raises((EnvironmentError, OutputLimitExceeded)):
        _grading_artifacts({"artifacts": [item]}, 8)


docker_enabled = pytest.mark.skipif(
    os.environ.get("BENCHMAXXING_FRONTEND_DOCKER_TESTS") != "1",
    reason="requires explicitly prepared local Chromium runtime",
)


@pytest.fixture(scope="module")
def prepared(tmp_path_factory):
    directory = tmp_path_factory.mktemp("frontend-docker")
    tasks = installed(directory)
    result = prepare_frontend_runtime(directory)
    assert result["prepared_tasks"] == 6 and not result["errors"]
    return directory, tasks


@pytest.mark.docker
@docker_enabled
@pytest.mark.parametrize("index", range(6))
@pytest.mark.parametrize("good", [True, False])
def test_original_frontend_good_bad_in_fresh_browser_grader(prepared, index, good):
    directory, tasks = prepared
    task = tasks[index]
    budget = Budgets(
        wall_seconds=120,
        command_seconds=30,
        memory_mb=2048,
        pids=256,
        artifact_bytes=8 * 1024 * 1024,
    )
    environment = NativeEnvironment(
        directory, f"frontend-{index}-{good}", budget, task, threading.Event()
    )
    try:
        environment.start()
        assert environment.execute("test ! -e /grader/frontend_grader.py")["exit_code"] == 0
        environment.write_file("index.html", task.good_solution if good else task.bad_solution)
        frozen = environment.freeze()
        environment.close()
        result = environment.grade(frozen)
        assert result["passed"] is good, result
        assert (result["score"] == 1) is good
        assert len(_grading_artifacts(result, budget.artifact_bytes)) == 2
        assert result["evidence"]["aesthetic_review"]["status"] == "needs_review"
        assert result["evidence"]["aesthetic_review"]["score"] is None
    finally:
        environment.close()


@pytest.mark.docker
@docker_enabled
def test_frontend_shared_runner_records_pngs_and_grading_evidence(prepared):
    directory, tasks = prepared
    store = Store(directory)
    run = store.create_run(
        RunSpec(
            task_id=tasks[0].id,
            budgets=Budgets(
                memory_mb=2048,
                pids=256,
                command_seconds=30,
                artifact_bytes=8 * 1024 * 1024,
            ),
        )
    )
    result = execute_run(store, run.id)
    assert result.status == "passed", result.error
    artifacts = store.artifacts(run.id)
    assert sum(item.media_type == "image/png" for item in artifacts) == 2
    assert any(item.name == "native-grading-evidence.json" for item in artifacts)
