import json

import pytest

from benchmaxxing import grading
from benchmaxxing.contracts import Budgets
from benchmaxxing.environment import ExecutionCancelled


def test_invalid_source_encoding_never_executes(tmp_path):
    score = grading.grade_submission(b"\xff", tmp_path, "run", Budgets())
    assert not score.passed
    assert score.checks[0].name == "source_encoding"


@pytest.mark.parametrize("response_mode", ["valid", "wrong", "invalid_json", "nonzero"])
def test_grader_decides_outcomes_outside_sandbox(tmp_path, monkeypatch, response_mode):
    instances = []

    class FakeEnvironment:
        def __init__(self, *args, **kwargs):
            self.closed = False
            self.transferred = []
            instances.append(self)

        def start(self):
            pass

        def write_file(self, name, source):
            self.transferred.append(name)

        def run_python(self, driver, payload):
            values = json.loads(payload)
            expected = next(
                answer for _, test_input, answer in grading.CASES if test_input == values
            )
            if response_mode == "valid":
                output = json.dumps({"answer": expected, "input_after": values})
            elif response_mode == "wrong":
                output = json.dumps({"answer": None, "input_after": values})
            else:
                output = "not JSON"
            return {
                "exit_code": int(response_mode == "nonzero"),
                "stdout": output,
                "stderr": "failed",
            }

        def close(self):
            self.closed = True

    monkeypatch.setattr(grading, "DockerEnvironment", FakeEnvironment)
    score = grading.grade_submission(b"source", tmp_path, "run", Budgets())
    assert score.passed is (response_mode == "valid")
    assert len(score.checks) == len(grading.CASES)
    assert instances[0].closed
    assert instances[0].transferred == ["solution.py"]


def test_cancelled_grading_cleans_up(tmp_path, monkeypatch):
    closed = []

    class CancelledEnvironment:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise ExecutionCancelled("cancelled")

        def close(self):
            closed.append(True)

    monkeypatch.setattr(grading, "DockerEnvironment", CancelledEnvironment)
    with pytest.raises(ExecutionCancelled):
        grading.grade_submission(b"source", tmp_path, "run", Budgets())
    assert closed == [True]
