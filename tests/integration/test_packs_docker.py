"""Every installed pack must accept its good fixture and reject its bad fixture."""

import json
import os
import shutil
from pathlib import Path

import pytest

from benchmaxxing.contracts import Budgets, RunSpec
from benchmaxxing.engine import execute_run
from benchmaxxing.environment import DockerEnvironment, cleanup_workspace, preparation_info
from benchmaxxing.grading import grade_submission
from benchmaxxing.storage import Store
from benchmaxxing.task import definitions

pytestmark = [
    pytest.mark.docker,
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("BENCHMAXXING_DOCKER_TESTS") != "1",
        reason="Explicitly prepare all packs and enable Docker tests",
    ),
]


@pytest.fixture(scope="module")
def pack_data(tmp_path_factory):
    source = Path(os.environ.get("BENCHMAXXING_TEST_DATA_DIR", ".benchmaxxing"))
    for task_id in definitions():
        preparation_info(source, task_id)
    path = tmp_path_factory.mktemp("pack-validation")
    shutil.copyfile(source / "prepared.json", path / "prepared.json")
    yield path
    cleanup_workspace(path)


@pytest.mark.parametrize("task_id", list(definitions()))
def test_each_pack_public_examples_and_independent_good_bad_checks(pack_data, task_id):
    task = definitions()[task_id]
    budgets = Budgets(wall_seconds=180)
    with DockerEnvironment(pack_data, "public-examples", budgets, task_id=task_id) as env:
        env.write_file(task.submission_file, task.good_solution)
        public = env.execute("python public_tests.py")
        assert public["exit_code"] == 0, public
    progress = []
    good = grade_submission(
        task.good_solution.encode(),
        pack_data,
        "good-fixture",
        budgets,
        task_id=task_id,
        progress=lambda done, total, check: progress.append((done, total, check)),
    )
    bad = grade_submission(
        task.bad_solution.encode(), pack_data, "bad-fixture", budgets, task_id=task_id
    )
    assert good.passed and good.score == 1, good.model_dump()
    assert not bad.passed and bad.score < 1, bad.model_dump()
    assert len(good.checks) == len(task.cases)
    assert [item[0] for item in progress] == list(range(1, len(good.checks) + 1))
    assert all(item[1] == len(good.checks) for item in progress)
    assert [item[2] for item in progress] == good.checks


@pytest.mark.parametrize("task_id", ["terminal.log-triage", "refusal.allow-benign-status"])
def test_json_submission_lifecycle_and_decision_evidence(pack_data, task_id):
    store = Store(pack_data)
    run = store.create_run(RunSpec(task_id=task_id, budgets=Budgets(wall_seconds=180)))
    result = execute_run(store, run.id)
    assert result.status == "passed", result.error
    events = store.events(run.id)
    assert any(event.kind == "tool.started" for event in events)
    graded = [event for event in events if event.kind == "grading.check"]
    assert graded[-1].data["completed"] == len(result.score.checks)
    assert not any(item.id == run.id for item in store.activity().runs)
    artifacts = {item.name: item for item in store.artifacts(run.id)}
    assert "submission.json" in artifacts
    _, payload = store.artifact_content(run.id, artifacts["public-task-assets.json"].id)
    assets = json.loads(payload)
    assert "TASK.md" in assets and "public_tests.py" in assets
    assert "cases" not in assets and "good_solution" not in assets
    if task_id == "terminal.log-triage":
        assert "logs/current.log" in assets
    if task_id.startswith("refusal."):
        assert result.refusal is None
        assert result.metadata["refusal_assessment"]["observed_decision"] == "comply"
    assert cleanup_workspace(pack_data, run.id) == []
