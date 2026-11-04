"""Black-box grading: candidate code and trusted expectations never co-reside."""

from __future__ import annotations

import json
import math
import threading
from pathlib import Path
from typing import Any

from benchmaxxing.contracts import Budgets, CheckResult, ScoreCard
from benchmaxxing.environment import DockerEnvironment, OutputLimitExceeded
from benchmaxxing.task import REFERENCE_ID, TaskDefinition, get_definition

# These expectations stay in the controller; they are never copied to a worker.
CASES: tuple[tuple[str, list[list[int]], list[list[int]]], ...] = (
    ("empty", [], []),
    ("singleton", [[3, 9]], [[3, 9]]),
    ("disjoint_unsorted", [[8, 9], [-2, 0], [3, 4]], [[-2, 0], [3, 4], [8, 9]]),
    ("overlap", [[1, 4], [2, 6], [8, 10], [9, 9]], [[1, 6], [8, 10]]),
    ("touching", [[1, 2], [2, 3], [3, 3], [5, 6]], [[1, 3], [5, 6]]),
    ("nested_duplicates", [[0, 20], [3, 4], [0, 20], [9, 12]], [[0, 20]]),
    ("negative", [[-5, -2], [-8, -4], [-1, 0]], [[-8, -2], [-1, 0]]),
    ("large_integers", [[10**30, 10**30 + 2], [10**30 + 1, 10**30 + 8]], [[10**30, 10**30 + 8]]),
)

DRIVER = """import importlib.util,json,sys
values=json.load(sys.stdin)
spec=importlib.util.spec_from_file_location('submission','/workspace/solution.py')
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
answer=module.merge_intervals(values)
print(json.dumps({'answer':answer,'input_after':values},allow_nan=False))
"""


def grade_submission(
    source: bytes,
    data_dir: Path,
    run_id: str,
    budgets: Budgets,
    cancel_event: threading.Event | None = None,
    *,
    deadline: float | None = None,
    task_id: str = REFERENCE_ID,
) -> ScoreCard:
    """Run the submitted source in a new container, compare observations on host."""
    if len(source) > budgets.artifact_bytes:
        raise OutputLimitExceeded("Submission exceeds artifact byte limit")
    if task_id != REFERENCE_ID:
        return _grade_pack(
            source, data_dir, run_id, budgets, cancel_event, deadline, get_definition(task_id)
        )
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError:
        return ScoreCard(
            passed=False,
            score=0,
            checks=[
                CheckResult(
                    name="source_encoding",
                    passed=False,
                    detail="Submission must be UTF-8 Python source",
                )
            ],
        )
    environment = DockerEnvironment(data_dir, run_id, budgets, cancel_event, deadline=deadline)
    checks: list[CheckResult] = []
    try:
        environment.start()
        environment.write_file("solution.py", text)
        for name, values, expected in CASES:
            execution = environment.run_python(DRIVER, json.dumps(values).encode("utf-8"))
            if execution["exit_code"] != 0:
                checks.append(
                    CheckResult(
                        name=name,
                        passed=False,
                        detail=f"Candidate exited {execution['exit_code']}: "
                        f"{execution['stderr'][:1000]}",
                    )
                )
                continue
            try:
                observed = json.loads(execution["stdout"])
                correct = (
                    isinstance(observed, dict)
                    and observed.get("answer") == expected
                    and observed.get("input_after") == values
                )
            except (TypeError, ValueError):
                correct = False
            checks.append(
                CheckResult(
                    name=name,
                    passed=correct,
                    detail="Expected output and input preservation verified"
                    if correct
                    else (
                        "Candidate output did not match the expected intervals, "
                        "changed input, or was invalid JSON"
                    ),
                )
            )
    finally:
        environment.close()
    successes = sum(check.passed for check in checks)
    return ScoreCard(passed=successes == len(CASES), score=successes / len(CASES), checks=checks)


def equivalent(observed: Any, expected: Any, *, atol: float = 0, rtol: float = 0) -> bool:
    """Typed structural equality with explicit numeric tolerances; never accept NaN."""
    if isinstance(expected, bool) or expected is None or isinstance(expected, str):
        return type(observed) is type(expected) and observed == expected
    if isinstance(expected, (int, float)):
        if isinstance(observed, bool) or not isinstance(observed, (int, float)):
            return False
        if isinstance(observed, float) and not math.isfinite(observed):
            return False
        if atol == 0 and rtol == 0:
            return bool(observed == expected)
        try:
            return math.isclose(observed, expected, abs_tol=atol, rel_tol=rtol)
        except (OverflowError, ValueError):
            return False
    if isinstance(expected, list):
        return (
            isinstance(observed, list)
            and len(observed) == len(expected)
            and all(
                equivalent(a, b, atol=atol, rtol=rtol)
                for a, b in zip(observed, expected, strict=True)
            )
        )
    if isinstance(expected, dict):
        return (
            isinstance(observed, dict)
            and observed.keys() == expected.keys()
            and all(
                equivalent(observed[key], value, atol=atol, rtol=rtol)
                for key, value in expected.items()
            )
        )
    return False


def _json(text: str | bytes) -> Any:
    def nonfinite(value: str) -> None:
        raise ValueError(f"Non-finite JSON value: {value}")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON object field")
            result[key] = value
        return result

    return json.loads(text, parse_constant=nonfinite, object_pairs_hook=unique_object)


def _score(checks: list[CheckResult], definition: TaskDefinition) -> ScoreCard:
    count = sum(check.passed for check in checks)
    return ScoreCard(
        grader_version=f"{definition.id}/{definition.version}/blackbox-1",
        passed=bool(checks) and count == len(checks),
        score=count / len(checks) if checks else 0,
        checks=checks,
    )


def _grade_pack(
    source: bytes,
    data_dir: Path,
    run_id: str,
    budgets: Budgets,
    cancel_event: threading.Event | None,
    deadline: float | None,
    definition: TaskDefinition,
) -> ScoreCard:
    checks: list[CheckResult] = []
    if definition.kind == "json":
        try:
            document = _json(source)
        except (ValueError, UnicodeError, RecursionError):
            return _score(
                [
                    CheckResult(
                        name="submission_format", passed=False, detail="Submit finite UTF-8 JSON"
                    )
                ],
                definition,
            )
        for case in definition.cases:
            try:
                exact_fields = {item.path[0] for item in definition.cases if item.path}
                if definition.expected_decision is not None and (
                    not isinstance(document, dict) or set(document) != exact_fields
                ):
                    raise KeyError("Policy decisions require exactly the rubric fields")
                observed = document
                for key in case.path:
                    if isinstance(key, int) and not isinstance(observed, list):
                        raise KeyError(key)
                    if isinstance(key, str) and not isinstance(observed, dict):
                        raise KeyError(key)
                    observed = observed[key]
                passed = equivalent(
                    observed, case.expected, atol=definition.atol, rtol=definition.rtol
                )
            except (KeyError, IndexError, TypeError, RecursionError):
                passed = False
            checks.append(
                CheckResult(
                    name=case.name,
                    passed=passed,
                    detail="Artifact satisfies the rubric"
                    if passed
                    else "Artifact is missing a required field or differs from the rubric",
                )
            )
        return _score(checks, definition)
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError:
        return _score(
            [
                CheckResult(
                    name="source_encoding", passed=False, detail="Submit UTF-8 Python source"
                )
            ],
            definition,
        )
    driver = DRIVER.replace(
        "module.merge_intervals(values)", f"getattr(module, {definition.entrypoint!r})(values)"
    )
    environment = DockerEnvironment(
        data_dir, run_id, budgets, cancel_event, deadline=deadline, task_id=definition.id
    )
    try:
        environment.start()
        environment.write_file("solution.py", text)
        for case in definition.cases:
            result = environment.run_python(
                driver, json.dumps(case.input, allow_nan=False).encode()
            )
            passed = False
            detail = "Unexpected result, mutated input, or invalid JSON output"
            if result["exit_code"] == 0:
                try:
                    observed = _json(result["stdout"])
                    passed = (
                        isinstance(observed, dict)
                        and "answer" in observed
                        and equivalent(
                            observed["answer"],
                            case.expected,
                            atol=definition.atol,
                            rtol=definition.rtol,
                        )
                        and (
                            not definition.preserve_input
                            or (
                                "input_after" in observed
                                and equivalent(observed["input_after"], case.input)
                            )
                        )
                    )
                except (ValueError, UnicodeError, RecursionError):
                    pass
            else:
                detail = f"Candidate exited {result['exit_code']}: {result['stderr'][:1000]}"
            checks.append(
                CheckResult(
                    name=case.name,
                    passed=passed,
                    detail="Expected result and input preservation verified" if passed else detail,
                )
            )
    finally:
        environment.close()
    return _score(checks, definition)
