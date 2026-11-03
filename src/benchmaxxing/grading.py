"""Black-box grading: candidate code and trusted expectations never co-reside."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from benchmaxxing.contracts import Budgets, CheckResult, ScoreCard
from benchmaxxing.environment import DockerEnvironment, OutputLimitExceeded

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
) -> ScoreCard:
    """Run the submitted source in a new container, compare observations on host."""
    if len(source) > budgets.artifact_bytes:
        raise OutputLimitExceeded("Submission exceeds artifact byte limit")
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
