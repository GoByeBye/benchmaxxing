"""Reviewed log-summary adaptation, with optional frozen-source Docker fixtures."""

import os
from pathlib import Path

import pytest

from benchmaxxing.contracts import Budgets
from benchmaxxing.environment import EnvironmentError, PreparationError
from benchmaxxing.providers import interactive, interactive_preparation, registry
from benchmaxxing.providers.sandbox import NativeEnvironment


def test_log_summary_requires_exact_reviewed_source(tmp_path, monkeypatch):
    (tmp_path / "environment").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "task.toml").write_text('[task]\nname="terminal-bench/log-summary-date-ranges"\n')
    (tmp_path / "instruction.md").write_text("Summarize local logs.")
    (tmp_path / "environment/Dockerfile").write_text("FROM python:3.13-slim-bookworm\n")
    (tmp_path / "tests/test.sh").write_text("echo private\n")
    kwargs = dict(suite_id="terminal-bench-2-1", revision="fixture", source_sha256="a" * 64)
    (unreviewed,) = interactive.convert_harbor(tmp_path, **kwargs)
    assert "offline_profile" not in unreviewed.recipe.settings
    fingerprint = interactive_preparation._HARBOR_PROFILES["log-summary-date-ranges-offline-v1"][
        "fingerprint"
    ]
    monkeypatch.setattr(registry, "source_hash", lambda path: fingerprint)
    (reviewed,) = interactive.convert_harbor(tmp_path, **kwargs)
    assert reviewed.recipe.settings["artifacts"] == ["/app/summary.csv"]
    assert reviewed.variant.endswith("-artifacts-v1-prepacked-pytest-v1")
    assert "pip install" not in reviewed.private_files["benchmaxxing-offline-test.sh"]

    def absent_image(image):
        raise EnvironmentError("Explicitly absent fixture image")

    def forbidden_docker(*args, **kwargs):
        pytest.fail("A missing local base must never initiate a build or pull")

    monkeypatch.setattr(interactive_preparation, "_inspect_image", absent_image)
    monkeypatch.setattr(interactive_preparation, "_docker", forbidden_docker)
    with pytest.raises(PreparationError, match="runtime first"):
        interactive_preparation.prepare_harbor_profile(tmp_path, reviewed)


@pytest.mark.docker
@pytest.mark.skipif(
    not os.environ.get("BENCHMAXXING_LOG_SUMMARY_SOURCE"),
    reason="Requires explicitly selected frozen log-summary source and prepared local pytest image",
)
def test_log_summary_native_good_bad(tmp_path):
    source = Path(os.environ["BENCHMAXXING_LOG_SUMMARY_SOURCE"])
    (task,) = interactive.convert_harbor(
        source,
        suite_id="terminal-bench-2-1",
        revision="7131e4375048a0e408a8fb404b5f499d726b695b",
        source_sha256=registry.source_hash(source),
    )
    assert task.recipe.settings["offline_profile"] == "log-summary-date-ranges-offline-v1"
    registry.install(tmp_path, source, [task])
    receipt = interactive_preparation.prepare_harbor_profile(tmp_path, task)
    assert receipt["build_provenance"]["network"] == "none"
    oracle = (source / "solution/solve.sh").read_text()
    # The upstream solution's package bootstrap is unnecessary in the prepared image.
    oracle = oracle.replace("apt-get update && apt-get install -y grep coreutils", "true")
    for label, script, expected in (
        ("good", oracle, True),
        ("bad", "printf 'period,severity,count\\ntoday,ERROR,0\\n' > /app/summary.csv", False),
    ):
        env = NativeEnvironment(
            tmp_path, "log-summary-" + label, Budgets(wall_seconds=120, command_seconds=90), task
        )
        try:
            env.start()
            assert env.execute("test ! -e /tests/test_outputs.py")["exit_code"] == 0
            env.write_file("fixture.sh", script)
            assert env.execute("cd /app && bash /workspace/fixture.sh")["exit_code"] == 0
            frozen = env.freeze()
        finally:
            env.close()
        result = env.grade(frozen)
        assert result["passed"] is expected, result
