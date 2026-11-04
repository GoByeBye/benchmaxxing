"""Converters treat all upstream/candidate Python as data; no host execution."""

import ast
import bz2
import gzip
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from benchmaxxing.providers.coding import convert_coding


def convert(
    root: Path, suite: str, rows: Any, *, variant: str | None = None, suffix: str = ".json"
) -> Any:
    path = root / ("source" + suffix)
    data = json.dumps(rows).encode()
    if suffix.endswith(".gz"):
        data = gzip.compress(data)
    elif suffix.endswith(".bz2"):
        data = bz2.compress(data)
    path.write_bytes(data)
    return convert_coding(
        suite,
        path,
        revision="fixture-release-1",
        source_sha256=hashlib.sha256(data).hexdigest(),
        variant=variant,
    )


def human() -> dict[str, Any]:
    return {
        "task_id": "HumanEval/0",
        "prompt": 'def plus_one(x):\n    """Add one."""\n',
        "entry_point": "plus_one",
        "canonical_solution": "    return x + 1\n",
        "test": "def check(candidate):\n    assert candidate(4) == 5\n",
    }


def bigcode() -> dict[str, Any]:
    return {
        "task_id": "BigCodeBench/0",
        "complete_prompt": 'def task_func(x):\n    """Add one."""\n',
        "instruct_prompt": "Write task_func(x), returning x plus one.",
        "code_prompt": "def task_func(x):\n",
        "entry_point": "task_func",
        "canonical_solution": "    return x + 1\n",
        "test": "import unittest\nclass TestCases(unittest.TestCase):\n"
        "    def test_one(self):\n        self.assertEqual(task_func(4), 5)\n",
    }


def swe() -> dict[str, Any]:
    return {
        "instance_id": "demo__repo-42",
        "repo": "demo/repo",
        "base_commit": "a" * 40,
        "version": "1.0",
        "problem_statement": "Repair the documented off-by-one defect.",
        "patch": "gold patch must remain private",
        "test_patch": "hidden test patch",
        "FAIL_TO_PASS": '["test_fix"]',
        "PASS_TO_PASS": ["test_preserve"],
        "image": "example.invalid/task:prepared",
        "eval_script": "#!/bin/bash\npytest -rA\n",
        "log_parser": "pytest",
        "eval_type": "pass_and_fail",
    }


def scicode() -> dict[str, Any]:
    return {
        "problem_id": "74",
        "problem_name": "QR",
        "problem_description_main": "Compute QR.",
        "problem_background_main": "Use the stated sign convention.",
        "problem_io": "A matrix.",
        "required_dependencies": "import numpy as np",
        "general_solution": None,
        "sub_steps": [
            {
                "step_number": "74.1",
                "step_description_prompt": "Return R.",
                "step_background": "All reflectors are required.",
                "function_header": "def householder(A):",
                "ground_truth_code": None,
                "test_cases": ["assert np.allclose(householder(np.eye(2)), target)"],
            }
        ],
    }


@pytest.mark.parametrize("suffix", [".json", ".jsonl.gz", ".jsonl.bz2"])
def test_human_frozen_tests_are_private_and_identity_preserved(tmp_path: Path, suffix: str) -> None:
    row = human()
    (task,) = convert(tmp_path, "evalplus-humaneval", row, suffix=suffix)
    assert task.upstream_id == "HumanEval/0"
    assert task.recipe.network == "none"
    assert task.recipe.settings["profile"] == "hf-frozen-assertions"
    assert "candidate(4)" not in task.prompt + "".join(task.public_files.values())
    assert row["test"] == task.private_files["tests.py"]
    assert task.good_solution == row["prompt"] + row["canonical_solution"]
    assert task.check_count is None
    assert task.recipe.grader_result_path == "/tmp/benchmaxxing-grade/result.json"
    ast.parse(task.private_files["grade.py"])


def test_mbpp_hf_preserves_public_tests_but_hides_expanded_tests(tmp_path: Path) -> None:
    row = {
        "task_id": 2,
        "prompt": "Return the shared values.",
        "code": "def common(a, b):\n    return set(a) & set(b)\n",
        "test_imports": [],
        "test_list": ["assert common([1], [1]) == {1}"],
        "test": "assert common([99], []) == set()",
    }
    (task,) = convert(tmp_path, "evalplus-mbpp", row)
    assert task.upstream_id == "2"
    assert "common([1]" in task.public_files["public_tests.py"]
    assert "[99]" not in "".join(task.public_files.values())
    assert "return set" not in task.public_files["solution.py"]
    assert "def common(a, b)" in task.public_files["solution.py"]
    ast.parse(task.private_files["grade.py"])


@pytest.mark.parametrize(
    "suite,prefix", [("evalplus-humaneval", "HumanEval"), ("evalplus-mbpp", "Mbpp")]
)
def test_native_evalplus_keeps_special_oracles_in_official_harness(
    tmp_path: Path, suite: str, prefix: str
) -> None:
    row = human() | {
        "task_id": prefix + "/0",
        "contract": "",
        "base_input": [[1]],
        "plus_input": [[5]],
        "atol": 0,
    }
    (task,) = convert(tmp_path, suite, row)
    assert task.check_count == 2
    assert task.recipe.settings["profile"] == "official-differential-jsonl"
    assert "OVERRIDE_PATH" in task.private_files["grade.py"]
    assert "from evalplus.evaluate import evaluate" in task.private_files["grade.py"]
    assert "[5]" not in "".join(task.public_files.values())
    ast.parse(task.private_files["grade.py"])


@pytest.mark.parametrize("variant,split", [(None, "instruct"), ("complete", "complete")])
def test_bigcodebench_uses_local_native_evaluator_and_distinct_prompt_tracks(
    tmp_path: Path, variant: str | None, split: str
) -> None:
    (task,) = convert(tmp_path, "bigcodebench", bigcode(), variant=variant)
    assert task.recipe.settings["execution"] == "local"
    assert task.recipe.settings["split"] == split
    assert "assertEqual" not in task.prompt + "".join(task.public_files.values())
    assert 'execution="local"' in task.private_files["grade.py"]
    assert "BIGCODEBENCH_OVERRIDE_PATH" in task.private_files["grade.py"]
    ast.parse(task.private_files["grade.py"])


@pytest.mark.parametrize("suite", ["swebench-lite", "swebench-verified"])
def test_swe_native_schema_keeps_grading_patch_and_test_lists_private(
    tmp_path: Path, suite: str
) -> None:
    (task,) = convert(tmp_path, suite, swe())
    assert task.upstream_id == "demo__repo-42"
    assert task.submission_file == "submission.patch"
    assert task.check_count == 2
    assert task.recipe.settings["base_commit"] == "a" * 40
    assert not task.recipe.blockers
    public = task.prompt + "".join(task.public_files.values())
    assert "test_fix" not in public
    assert "gold patch" not in public
    assert "get_eval_report" in task.private_files["grade.py"]
    ast.parse(task.private_files["grade.py"])


def test_swe_dependency_install_blocks_run_and_prepared_override_is_recorded(
    tmp_path: Path,
) -> None:
    row = swe() | {"eval_script": "python -m pip install -e .[test]\npytest -rA\n"}
    (task,) = convert(tmp_path, "swebench-lite", row)
    assert any("installs/downloads" in value for value in task.recipe.blockers)
    row["benchmaxxing_prepared"] = {"eval_script": "pytest -rA\n", "receipt_sha256": "b" * 64}
    (prepared,) = convert(tmp_path, "swebench-lite", row)
    assert not prepared.recipe.blockers
    assert prepared.recipe.settings["prepared_receipt_sha256"] == "b" * 64
    assert "pip install" in prepared.private_files["upstream.json"]
    assert "pip install" not in prepared.private_files["eval.sh"]


def test_legacy_swe_requires_real_native_preparation(tmp_path: Path) -> None:
    row = swe()
    for field in ("image", "log_parser", "eval_type", "eval_script"):
        row.pop(field)
    (task,) = convert(tmp_path, "swebench-verified", row)
    assert len(task.recipe.blockers) == 3
    assert task.recipe.image is None


def test_swe_preparation_plan_preserves_tests_and_moves_only_install(
    tmp_path: Path,
) -> None:
    from benchmaxxing.providers import registry
    from benchmaxxing.providers.coding_sources import swebench_preparation_plan

    row = swe() | {
        "eval_script": "cd /testbed\npython -m pip install -e .[test] --verbose\npytest -rA\n"
    }
    (task,) = convert(tmp_path, "swebench-lite", row)
    from unittest.mock import patch

    with patch.object(registry, "get_imported", return_value=task):
        plan = swebench_preparation_plan(tmp_path, task.id)
    directory = Path(plan["directory"])
    assert (directory / "eval.offline.sh").read_text() == "cd /testbed\npytest -rA\n"
    assert "pytest" not in (directory / "prepare-dependencies.sh").read_text()
    assert plan["runtime_prepared"] is False
    assert plan["source_sha256"] == task.source_sha256


def test_swe_pathlike_identity_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="instance identity"):
        convert(tmp_path, "swebench-lite", swe() | {"instance_id": "../../escape"})


def test_scicode_whole_problem_is_distinct_and_missing_assets_block_preparation(
    tmp_path: Path,
) -> None:
    row = scicode()
    (task,) = convert(tmp_path, "scicode-verified", row)
    assert task.upstream_id == "74"
    assert task.check_count == 1
    assert "single-env" in task.variant
    assert task.recipe.settings["canonical_generation"] == "cumulative per-step"
    assert task.good_solution is None
    assert "np.allclose" not in task.prompt + "".join(task.public_files.values())
    assert len(task.recipe.blockers) == 7
    assert task.assets == []
    ast.parse(task.private_files["grade.py"])


def test_scicode_prepared_assets_are_all_grader_only(tmp_path: Path) -> None:
    names = [
        "test_data_cleaned.h5",
        "eval_clean/vendor/scicode/__init__.py",
        "eval_clean/vendor/scicode/parse/__init__.py",
        "eval_clean/vendor/scicode/parse/parse.py",
        "eval_clean/vendor/scicode/compare/__init__.py",
        "eval_clean/vendor/scicode/compare/cmp.py",
        "eval_clean/vendor/LICENSE",
    ]
    for name in names:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"explicitly named fake asset, never executed")
    (task,) = convert(tmp_path, "scicode-verified", scicode())
    assert not task.recipe.blockers
    assert len(task.assets) == 7
    assert all(asset.scope == "grader" for asset in task.assets)
    assert task.assets[0].target == "test_data_cleaned.h5"


def test_scicode_skipped_upstream_steps_are_supplied_not_scored(tmp_path: Path) -> None:
    row = scicode() | {"problem_id": "13"}
    row["sub_steps"][0]["step_number"] = "13.1"
    row["sub_steps"].append(row["sub_steps"][0] | {"step_number": "13.6"})
    gold = tmp_path / "eval_clean/vendor/eval_data/13.6.txt"
    gold.parent.mkdir(parents=True)
    gold.write_text("def provided(x):\n    return x\n")
    (task,) = convert(tmp_path, "scicode-verified", row)
    assert task.check_count == 1
    assert "def provided" in task.public_files["solution.py"]
    assert not any("unscored" in blocker for blocker in task.recipe.blockers)


def test_scicode_preserves_released_zero_assertion_step_explicitly(tmp_path: Path) -> None:
    row = scicode() | {"problem_id": "72"}
    row["sub_steps"][0].update(step_number="72.6", test_cases=[])
    (task,) = convert(tmp_path, "scicode-verified", row)
    assert task.recipe.settings["zero_assertion_substeps"] == ["72.6"]
    assert task.check_count == 1
    assert json.loads(task.private_files["task.json"])["steps"] == [{"id": "72.6", "tests": []}]


@pytest.mark.parametrize(
    "mutation", [lambda r: r.update(task_id=""), lambda r: r.update(entry_point="a;evil()")]
)
def test_invalid_source_identity_or_entrypoint_is_rejected(tmp_path: Path, mutation: Any) -> None:
    row = human()
    mutation(row)
    with pytest.raises(ValueError):
        convert(tmp_path, "evalplus-humaneval", row)


def test_source_digest_is_verified_and_mutable_revision_rejected(tmp_path: Path) -> None:
    path = tmp_path / "source.json"
    path.write_text(json.dumps(human()))
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        convert_coding("evalplus-humaneval", path, revision="release-1", source_sha256="0" * 64)
    with pytest.raises(ValueError, match="mutable branch"):
        convert_coding("evalplus-humaneval", path, revision="main", source_sha256="0" * 64)


def test_duplicate_rows_and_duplicate_json_keys_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Duplicate upstream"):
        convert(tmp_path, "evalplus-humaneval", [human(), human()])
    path = tmp_path / "source.jsonl"
    path.write_text('{"task_id": "one", "task_id": "two"}\n')
    with pytest.raises(ValueError, match="Duplicate source"):
        convert_coding(
            "evalplus-humaneval",
            path,
            revision="release-1",
            source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        )


def test_jsonl_multiple_records_preserves_source_order(tmp_path: Path) -> None:
    path = tmp_path / "source.jsonl"
    path.write_text(json.dumps(human() | {"task_id": "HumanEval/8"}) + "\n" + json.dumps(human()))
    tasks = convert_coding(
        "evalplus-humaneval",
        path,
        revision="release-1",
        source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    assert [task.upstream_id for task in tasks] == ["HumanEval/8", "HumanEval/0"]


def test_conversion_never_executes_upstream_python(tmp_path: Path) -> None:
    marker = tmp_path / "must-not-exist"
    row = human() | {
        "test": f"open({str(marker)!r}, 'w').write('bad')",
        "canonical_solution": f"open({str(marker)!r}, 'w').write('bad')",
    }
    (task,) = convert(tmp_path, "evalplus-humaneval", row)
    assert not marker.exists()
    assert "must-not-exist" in task.private_files["tests.py"]


def test_parquet_requires_explicit_preparation_conversion(tmp_path: Path) -> None:
    path = tmp_path / "source.parquet"
    path.write_bytes(b"PAR1")
    with pytest.raises(ValueError, match="preparation container"):
        convert_coding(
            "bigcodebench",
            path,
            revision="release-1",
            source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        )
