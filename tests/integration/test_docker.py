"""Real Docker tests; prepare assets explicitly before opting in.

BENCHMAXXING_DOCKER_TESTS=1
BENCHMAXXING_TEST_DATA_DIR=<directory previously prepared by the CLI>
"""

import json
import os
import shlex
import shutil
import threading
import time
from pathlib import Path

import pytest

from benchmaxxing.contracts import Budgets
from benchmaxxing.environment import (
    DockerEnvironment,
    ExecutionCancelled,
    ExecutionTimeout,
    OutputLimitExceeded,
    ToolOperationError,
    cleanup_workspace,
    preparation_info,
)
from benchmaxxing.grading import grade_submission
from benchmaxxing.task import BAD_SOLUTION, GOOD_SOLUTION

pytestmark = [
    pytest.mark.docker,
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("BENCHMAXXING_DOCKER_TESTS") != "1",
        reason="Set BENCHMAXXING_DOCKER_TESTS=1 after explicit preparation",
    ),
]


@pytest.fixture(scope="module")
def prepared_dir(tmp_path_factory):
    source = Path(os.environ.get("BENCHMAXXING_TEST_DATA_DIR", ".benchmaxxing"))
    preparation_info(source)
    destination = tmp_path_factory.mktemp("docker-prepared")
    shutil.copyfile(source / "prepared.json", destination / "prepared.json")
    yield destination
    cleanup_workspace(destination)


def test_good_submission_passes_and_bad_submission_fails(prepared_dir):
    budgets = Budgets(wall_seconds=120)
    good = grade_submission(GOOD_SOLUTION.encode(), prepared_dir, "good", budgets)
    bad = grade_submission(BAD_SOLUTION.encode(), prepared_dir, "bad", budgets)
    assert good.passed and good.score == 1
    assert not bad.passed and bad.score < 1
    assert all(check.passed for check in good.checks)
    assert cleanup_workspace(prepared_dir) == []


def test_workspace_tools_and_isolation(prepared_dir):
    with DockerEnvironment(prepared_dir, "isolation", Budgets()) as task:
        assert "incomplete" in task.read_file("solution.py")
        task.write_file("solution.py", GOOD_SOLUTION)
        assert task.execute("python public_tests.py")["exit_code"] == 0
        assert task.collect().decode() == GOOD_SOLUTION
        inspect_script = (
            "import os,json,pathlib; print(json.dumps({'uid':os.getuid(),"
            "'workspace':sorted(p.name for p in pathlib.Path('/workspace').iterdir()),"
            "'socket':pathlib.Path('/var/run/docker.sock').exists(),"
            "'secret':os.getenv('BENCHMAXXING_TEST_SECRET')}))"
        )
        result = task.execute(f"python -c {shlex.quote(inspect_script)}")
        observed = json.loads(result["stdout"])
        assert observed["uid"] == 65534
        assert not observed["socket"]
        assert observed["secret"] is None
        assert "grading.py" not in observed["workspace"]
        assert "GOOD_SOLUTION" not in task.read_file("TASK.md")
        network = "import socket; socket.create_connection(('1.1.1.1',443),timeout=1)"
        assert task.execute(f"python -c {shlex.quote(network)}")["exit_code"] != 0
        assert task.execute("touch /must-not-write")["exit_code"] != 0
    assert cleanup_workspace(prepared_dir, "isolation") == []


def test_terminal_output_arrives_before_command_finishes(prepared_dir):
    seen = []
    with DockerEnvironment(prepared_dir, "live-terminal", Budgets()) as task:
        script = "import time; print('first'); time.sleep(1); print('second')"
        started = time.monotonic()
        result = task.execute(
            f"python -c {shlex.quote(script)}",
            on_output=lambda stream, text: seen.append((stream, text, time.monotonic())),
        )
        finished = time.monotonic()
    assert result["exit_code"] == 0 and result["stdout"] == "first\nsecond\n"
    assert "".join(text for stream, text, _ in seen if stream == "stdout") == result["stdout"]
    assert seen[0][2] - started < finished - started - 0.5


def test_symlink_cannot_escape_workspace(prepared_dir):
    with DockerEnvironment(prepared_dir, "symlink", Budgets()) as task:
        assert task.execute("ln -s /etc/passwd escaped")["exit_code"] == 0
        from benchmaxxing.environment import EnvironmentError

        with pytest.raises(EnvironmentError, match="Path leaves workspace"):
            task.read_file("escaped")


def test_missing_file_is_recoverable(prepared_dir):
    with DockerEnvironment(prepared_dir, "missing-file", Budgets()) as task:
        with pytest.raises(ToolOperationError, match="No such file"):
            task.read_file("missing.py")
        task.write_file("missing.py", "recovered")
        assert task.read_file("missing.py") == "recovered"


def test_collected_submission_obeys_artifact_budget(prepared_dir):
    with DockerEnvironment(prepared_dir, "artifact-limit", Budgets(artifact_bytes=1024)) as task:
        result = task.execute('python -c \'open("solution.py","w").write("x"*2048)\'')
        assert result["exit_code"] == 0
        with pytest.raises(OutputLimitExceeded):
            task.collect()


def test_command_timeout_cleans_up(prepared_dir):
    task = DockerEnvironment(prepared_dir, "timeout", Budgets(command_seconds=1))
    try:
        task.start()
        with pytest.raises(ExecutionTimeout):
            task.execute("python -c 'import time; time.sleep(30)'")
    finally:
        task.close()
    assert cleanup_workspace(prepared_dir, "timeout") == []


def test_cancellation_kills_running_command(prepared_dir):
    cancelled = threading.Event()
    task = DockerEnvironment(prepared_dir, "cancel", Budgets(), cancelled)
    timer = threading.Timer(0.25, cancelled.set)
    try:
        task.start()
        timer.start()
        with pytest.raises(ExecutionCancelled):
            task.execute("python -c 'import time; time.sleep(30)'")
    finally:
        timer.cancel()
        task.close()
    assert cleanup_workspace(prepared_dir, "cancel") == []


def test_output_flood_is_bounded_and_cleaned(prepared_dir):
    task = DockerEnvironment(prepared_dir, "output-limit", Budgets(output_bytes=1024))
    try:
        task.start()
        with pytest.raises(OutputLimitExceeded):
            task.execute("python -c 'import sys; sys.stdout.write(\"x\"*1000000)'")
    finally:
        task.close()
    assert cleanup_workspace(prepared_dir, "output-limit") == []
