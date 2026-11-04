import json
from collections import Counter

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from benchmaxxing.cli import app
from benchmaxxing.contracts import Budgets
from benchmaxxing.grading import equivalent, grade_submission
from benchmaxxing.task import TaskDefinition, definitions, get_task, list_tasks, task_files


def test_public_catalog_excludes_private_grading_material():
    tasks = list_tasks()
    assert len({task.id for task in tasks}) == len(tasks)
    assert Counter(task.category for task in tasks) == {
        "programming": 15,
        "engineering": 10,
        "security": 6,
        "terminal": 6,
        "refusal": 4,
    }
    for task in tasks:
        assert len(task.task_hash) == 64
        assert task.check_count >= 6
        public = task.model_dump()
        assert not {"cases", "good_solution", "bad_solution"}.intersection(public)
        assets = task_files(task.id)
        assert "TASK.md" in assets and "public_tests.py" in assets
        assert "cases.json" not in assets


@pytest.mark.parametrize(
    "observed,expected",
    [
        (True, 1),
        (1, True),
        (float("nan"), 1),
        (float("inf"), 1),
        ({"a": 1, "extra": 2}, {"a": 1}),
        ([1, 2], [2, 1]),
    ],
)
def test_structural_comparison_rejects_false_equivalences(observed, expected):
    assert not equivalent(observed, expected, atol=1e-8, rtol=1e-8)


def test_tolerance_applies_only_to_numeric_values():
    assert equivalent({"v": [1.000001, "ok"]}, {"v": [1, "ok"]}, atol=1e-5)
    assert not equivalent({"v": [1.001, "ok"]}, {"v": [1, "ok"]}, atol=1e-5)
    assert equivalent(10**1000, 10**1000)
    assert not equivalent(10**1000, 1, atol=0.01)


def test_hash_covers_grading_expectations_and_reference_fixture():
    original = next(iter(definitions().values()))
    changed = original.model_copy(deep=True)
    changed.cases[0].expected = {"changed": True}
    assert changed.public_spec().task_hash != original.public_spec().task_hash
    changed = original.model_copy(update={"good_solution": original.good_solution + "\n"})
    assert changed.public_spec().task_hash != original.public_spec().task_hash


@pytest.mark.parametrize("path", ["../hidden.json", "/absolute", "a\\b", "a/../b", "TASK.md"])
def test_pack_asset_paths_are_validated(path):
    value = next(iter(definitions().values())).model_dump()
    value["files"][path] = "invalid"
    with pytest.raises(ValidationError):
        TaskDefinition.model_validate(value)


def test_duplicate_checks_and_nonfinite_expectations_are_rejected():
    value = next(iter(definitions().values())).model_dump()
    value["cases"][1]["name"] = value["cases"][0]["name"]
    with pytest.raises(ValidationError):
        TaskDefinition.model_validate(value)
    value = next(iter(definitions().values())).model_dump()
    value["cases"][0]["expected"] = float("nan")
    with pytest.raises(ValidationError):
        TaskDefinition.model_validate(value)


def test_artifact_grade_reports_invalid_and_missing_fields(tmp_path):
    task_id = "refusal.protect-synthetic-secret"
    invalid = grade_submission(b'{"decision":NaN}', tmp_path, "invalid", Budgets(), task_id=task_id)
    missing = grade_submission(b"{}", tmp_path, "missing", Budgets(), task_id=task_id)
    assert not invalid.passed and invalid.checks[0].name == "submission_format"
    assert not missing.passed and missing.score == 0
    assert len(missing.checks) == get_task(task_id).check_count


def test_cli_catalog_and_invalid_selection_do_not_need_docker(tmp_path):
    runner = CliRunner()
    result = runner.invoke(app, ["tasks", "--category", "terminal"])
    assert result.exit_code == 0
    assert len(json.loads(result.output)) == 6
    result = runner.invoke(app, ["validate-packs"])
    assert result.exit_code == 0 and json.loads(result.output)["valid"]
    result = runner.invoke(app, ["run", "--task", "unknown.task", "--data-dir", str(tmp_path)])
    assert result.exit_code == 2
    assert not list(tmp_path.glob("*.sqlite3"))
    assert runner.invoke(app, ["suite", "--category", "unknown"]).exit_code == 2
