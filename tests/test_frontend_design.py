"""Original frontend tasks exercise browser behavior and preserve review evidence."""

import base64
import hashlib
import json
import os
import threading
from pathlib import Path

import pytest

from benchmaxxing.contracts import Budgets, RunSpec
from benchmaxxing.engine import execute_run
from benchmaxxing.environment import EnvironmentError, OutputLimitExceeded
from benchmaxxing.providers import frontend, registry
from benchmaxxing.providers.frontend import (
    VERSION,
    convert_frontend,
    convert_frontend_extension,
    fetch_frontend_extension_source,
    fetch_frontend_source,
    prepare_frontend_extension_runtime,
    prepare_frontend_runtime,
)
from benchmaxxing.providers.runner import _grading_artifacts
from benchmaxxing.providers.sandbox import NativeEnvironment
from benchmaxxing.storage import Store


def installed(directory, extension=False):
    fetch = fetch_frontend_extension_source if extension else fetch_frontend_source
    convert = convert_frontend_extension if extension else convert_frontend
    fetched = fetch(directory)
    source = Path(fetched["source_path"])
    tasks = convert(source, revision=VERSION, source_sha256=fetched["source_sha256"])
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


@pytest.fixture(scope="module", params=[False, True], ids=["original", "extended"])
def prepared(tmp_path_factory, request):
    directory = tmp_path_factory.mktemp("frontend-docker")
    tasks = installed(directory, request.param)
    prepare = prepare_frontend_extension_runtime if request.param else prepare_frontend_runtime
    result = prepare(directory)
    assert result["prepared_tasks"] == len(tasks) and not result["errors"]
    return directory, tasks


@pytest.mark.docker
@docker_enabled
@pytest.mark.parametrize("index", range(6))
@pytest.mark.parametrize("good", [True, False])
def test_original_frontend_good_bad_in_fresh_browser_grader(prepared, index, good):
    directory, tasks = prepared
    if index >= len(tasks):
        pytest.skip("extension pack contains three briefs")
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


def test_extension_pack_keeps_original_source_identity(tmp_path):
    original = installed(tmp_path)
    extended = installed(tmp_path, True)
    assert len(extended) == 3
    assert {task.suite_id for task in extended} == {"frontend-design-extended"}
    assert len({task.id for task in original + extended}) == 9
    assert [task.model_dump() for task in installed(tmp_path)] == [
        task.model_dump() for task in original
    ]
    for task in extended:
        assert "1280px" in task.prompt and "390px" in task.prompt
        assert task.recipe.network == "none"
        assert "frontend_grader.py" not in task.public_files
        assert task.good_solution != task.bad_solution


@pytest.mark.docker
@docker_enabled
@pytest.mark.parametrize(
    ("index", "before", "after"),
    [
        (0, "+ +d.value", "+ 4"),
        (1, "String(!open)", "'false'"),
        (2, "'Ember'", "'Wrong title'"),
    ],
)
def test_extension_rejects_behavior_regressions(prepared, index, before, after):
    directory, tasks = prepared
    if tasks[0].suite_id != "frontend-design-extended":
        pytest.skip("extension mutation fixtures only")
    task = tasks[index]
    assert before in task.good_solution
    budget = Budgets(memory_mb=2048, pids=256, command_seconds=30, artifact_bytes=8 * 1024 * 1024)
    environment = NativeEnvironment(
        directory, f"frontend-mutation-{index}", budget, task, threading.Event()
    )
    try:
        environment.start()
        environment.write_file("index.html", task.good_solution.replace(before, after))
        frozen = environment.freeze()
        environment.close()
        result = environment.grade(frozen)
        assert not result["passed"], result
        functional = [
            check
            for check in result["checks"]
            if "_at_" in check["name"] and not check["name"].startswith("overflow")
        ]
        assert len(functional) == 2
        assert all(not check["passed"] for check in functional), result
    finally:
        environment.close()


@pytest.mark.parametrize("extension", [False, True], ids=["original-crlf", "extended-lf"])
def test_frontend_source_bytes_and_hash_ignore_checkout_line_endings(
    tmp_path, monkeypatch, extension
):
    runtime = Path(frontend.__file__).parents[1] / "runtimes"
    prefix = "frontend_extension" if extension else "frontend"
    grader = (runtime / f"{prefix}_grader.py").read_bytes().replace(b"\r\n", b"\n")
    briefs = json.loads((runtime / f"{prefix}_briefs.json").read_text(encoding="utf-8"))
    fake_package = tmp_path / "package"
    fake_runtime = fake_package / "runtimes"
    fake_runtime.mkdir(parents=True)
    monkeypatch.setattr(frontend, "__file__", str(fake_package / "providers" / "frontend.py"))
    fetch = fetch_frontend_extension_source if extension else fetch_frontend_source
    convert = convert_frontend_extension if extension else convert_frontend
    results = []
    records = []
    for label, newline in [("lf", b"\n"), ("crlf", b"\r\n")]:
        (fake_runtime / f"{prefix}_grader.py").write_bytes(grader.replace(b"\n", newline))
        (fake_runtime / f"{prefix}_briefs.json").write_bytes(
            json.dumps(briefs, indent=2).encode().replace(b"\n", newline)
        )
        result = fetch(tmp_path / label)
        frozen = (Path(result["source_path"]) / "frontend_grader.py").read_bytes()
        expected = grader if extension else grader.replace(b"\n", b"\r\n")
        assert frozen == expected
        results.append(result["source_sha256"])
        records.append(
            [
                task.model_dump()
                for task in convert(
                    Path(result["source_path"]),
                    revision=VERSION,
                    source_sha256=result["source_sha256"],
                )
            ]
        )
    assert results[0] == results[1]
    assert records[0] == records[1]
    if not extension:
        assert results[0] == "0572d83dcd61e7680359255620e6f71337d0c556eee7aac4eebb6b8bd76c4712"
