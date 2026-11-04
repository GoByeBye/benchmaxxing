from __future__ import annotations

import ast
import io
import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any

import pytest

from benchmaxxing.contracts import Budgets
from benchmaxxing.environment import (
    EnvironmentError,
    ExecutionCancelled,
    ExecutionTimeout,
    OutputLimitExceeded,
    PreparationError,
    ToolOperationError,
)
from benchmaxxing.providers import browser as transport
from benchmaxxing.providers.interactive import (
    InteractiveImportError,
    convert_harbor,
    convert_interactive,
)
from benchmaxxing.providers.runtimes import interactive_browser as worker

DIGEST = "a" * 64


def write(root: Path, name: str, value: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    return path


def harbor_task(root: Path, name: str = "repair") -> Path:
    folder = root / "tasks" / name
    write(
        folder,
        "task.toml",
        """schema_version = "1.1"
artifacts = ["/app/result.bin"]
[task]
name = "terminal-bench/repair"
description = "Repair the local program"
[metadata]
difficulty = "medium"
tags = ["debugging"]
[environment]
docker_image = "example/image:prepared"
allow_internet = true
cpus = 2
memory_mb = 4096
[verifier]
environment_mode = "separate"
timeout_sec = 900
[agent]
timeout_sec = 28800
""",
    )
    write(folder, "instruction.md", "Repair /app/program.py and write /app/result.bin.")
    write(folder, "environment/Dockerfile", "FROM example/image:prepared\nWORKDIR /app\n")
    write(folder, "environment/input.txt", "public input")
    write(folder, "tests/test.sh", "#!/bin/sh\necho private_expected_result\n")
    write(folder, "solution/solve.sh", "#!/bin/sh\necho private_oracle\n")
    return folder


def convert(suite: str, source: Path) -> Any:
    return convert_interactive(suite, source, revision="frozen-revision", source_sha256=DIGEST)


def test_harbor_preserves_native_identity_and_private_assets(tmp_path: Path) -> None:
    harbor_task(tmp_path)
    (task,) = convert("terminal-bench-2-1", tmp_path)
    assert task.upstream_id == "terminal-bench/repair"
    assert task.source_sha256 == DIGEST
    assert task.difficulty == "medium"
    assert task.recipe.network == "none"
    assert task.recipe.settings["artifacts"] == ["/app/result.bin"]
    assert task.recipe.settings["verifier_environment_mode"] == "separate"
    assert task.recipe.settings["agent"]["timeout_sec"] == 28800
    assert task.recipe.grader_command == ["bash", "/tests/test.sh"]
    assert not task.recipe.blockers
    assert {asset.scope for asset in task.assets} == {"runtime", "grader"}
    assert all(asset.sha256 and len(asset.sha256) == 64 for asset in task.assets)
    public = json.dumps(task.public_files) + task.prompt
    assert "private_expected_result" not in public and "private_oracle" not in public
    assert any("canonical" in note for note in task.comparison_notes)


def test_harbor_single_directory_and_gpu_gate(tmp_path: Path) -> None:
    folder = harbor_task(tmp_path)
    path = folder / "task.toml"
    path.write_text(
        path.read_text().replace("cpus = 2", 'cpus = 2\ngpus = 1\ngpu_types = ["H100"]')
    )
    (task,) = convert_harbor(
        folder, suite_id="terminal-bench-4", revision="v4.0.0", source_sha256=DIGEST
    )
    assert any("H100" in blocker for blocker in task.recipe.blockers)
    assert all(not asset.source.startswith("tasks/") for asset in task.assets)
    assert task.recipe.settings["environment"]["gpus"] == 1


def test_harbor_missing_verifier_fails_without_execution(tmp_path: Path) -> None:
    folder = harbor_task(tmp_path)
    (folder / "tests/test.sh").unlink()
    with pytest.raises(InteractiveImportError, match="Missing regular source"):
        convert("terminal-bench-2-1", tmp_path)


def test_harbor_sidecar_and_filtered_artifacts_are_preserved_but_not_falsely_ready(
    tmp_path: Path,
) -> None:
    folder = harbor_task(tmp_path)
    spec = folder / "task.toml"
    spec.write_text(
        spec.read_text().replace(
            'artifacts = ["/app/result.bin"]',
            'artifacts = ["/app/result.bin", {source="/shared/result.json", service="api"}, '
            '{source="/app/generated", exclude=["node_modules"]}]',
        )
    )
    (task,) = convert("terminal-bench-4", tmp_path)
    assert task.recipe.settings["artifacts"] == ["/app/result.bin"]
    assert len(task.recipe.settings["upstream_artifacts"]) == 3
    assert any("task-local service: api" in item for item in task.recipe.blockers)
    assert any("exclusion" in item for item in task.recipe.blockers)


@pytest.mark.parametrize(
    "name",
    ["cancel-async-tasks", "regex-log", "sqlite-db-truncate"],
)
def test_named_but_changed_harbor_task_does_not_receive_reviewed_profile(
    tmp_path: Path, name: str
) -> None:
    folder = harbor_task(tmp_path, name)
    spec = folder / "task.toml"
    spec.write_text(spec.read_text().replace("terminal-bench/repair", f"terminal-bench/{name}"))
    (task,) = convert("terminal-bench-2-1", tmp_path)
    assert "offline_profile" not in task.recipe.settings
    assert not task.variant.endswith("-artifacts-v1")


@pytest.mark.parametrize(
    ("name", "artifact", "profile"),
    [
        ("regex-log", "/app/regex.txt", "regex-log-offline-v1"),
        ("sqlite-db-truncate", "/app/recover.json", "sqlite-db-truncate-offline-v1"),
    ],
)
def test_reviewed_harbor_python_profiles_use_private_offline_grader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    artifact: str,
    profile: str,
) -> None:
    from benchmaxxing.providers import interactive_preparation, registry

    folder = harbor_task(tmp_path, name)
    spec = folder / "task.toml"
    spec.write_text(spec.read_text().replace("terminal-bench/repair", f"terminal-bench/{name}"))
    write(folder, "tests/test_outputs.py", "def test_example(): assert True\n")
    if name == "sqlite-db-truncate":
        write(folder, "environment/trunc.db", "frozen input")
    fingerprint = interactive_preparation._HARBOR_PROFILES[profile]["fingerprint"]
    monkeypatch.setattr(registry, "source_hash", lambda _: fingerprint)

    (task,) = convert_harbor(
        folder, suite_id="terminal-bench-2-1", revision="frozen", source_sha256=DIGEST
    )
    assert task.recipe.settings["offline_profile"] == profile
    assert task.recipe.settings["artifacts"] == [artifact]
    assert task.recipe.settings["reviewed_source_sha256"] == fingerprint
    assert task.recipe.grader_command == ["bash", "/grader/benchmaxxing-offline-test.sh"]
    assert task.variant.endswith("-prepacked-pytest-v1")
    wrapper = task.private_files["benchmaxxing-offline-test.sh"]
    assert "python -m pytest --ctrf" in wrapper
    assert "apt" not in wrapper and "curl" not in wrapper and "uvx" not in wrapper
    assert "test_outputs.py" not in json.dumps(task.public_files)
    if name == "sqlite-db-truncate":
        source = next(asset for asset in task.assets if asset.target == "environment/trunc.db")
        assert task.recipe.settings["probe_file_sha256"] == {"/app/trunc.db": source.sha256}


def test_interactive_source_snapshot_rejects_local_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from benchmaxxing.providers import downloads, interactive_sources

    spec = interactive_sources.SOURCES["terminal-bench-2-1"]
    cached = tmp_path / "sources" / "terminal-bench-2-1" / spec["revision"]
    folder = harbor_task(cached / "source" / "repository")
    monkeypatch.setattr(downloads, "cache_root", lambda _: tmp_path)
    monkeypatch.setattr(
        downloads, "download", lambda *args, **kwargs: {"sha256": DIGEST, "bytes": 123}
    )
    receipt = interactive_sources.fetch_interactive_sources(tmp_path, "terminal-bench-2-1")
    assert receipt["tasks"] == 1
    assert interactive_sources.fetch_interactive_sources(tmp_path, "terminal-bench-2-1") == receipt
    (folder / "instruction.md").write_text("Changed after preparation")
    with pytest.raises(ValueError, match="source changed"):
        interactive_sources.fetch_interactive_sources(tmp_path, "terminal-bench-2-1")


def test_unreviewed_harbor_profile_never_builds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from benchmaxxing.providers import interactive_preparation

    harbor_task(tmp_path)
    (task,) = convert("terminal-bench-2-1", tmp_path)
    task.recipe.settings["offline_profile"] = "cancel-async-tasks-offline-v1"
    task.recipe.settings["reviewed_source_sha256"] = DIGEST
    monkeypatch.setattr(
        interactive_preparation, "_docker", lambda *a, **k: pytest.fail("unexpected Docker call")
    )
    with pytest.raises(PreparationError, match="No reviewed offline build"):
        interactive_preparation.prepare_harbor_profile(tmp_path, task)


def test_browser_build_checks_backing_capacity_before_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from benchmaxxing.providers import interactive_preparation

    def insufficient(*args: Any) -> None:
        raise PreparationError("Docker backing disk reserve")

    monkeypatch.setattr(interactive_preparation, "check_docker_capacity", insufficient)
    monkeypatch.setattr(
        interactive_preparation, "_docker", lambda *a, **k: pytest.fail("unexpected Docker call")
    )
    with pytest.raises(PreparationError, match="backing disk reserve"):
        interactive_preparation.prepare_miniwob_runtime(tmp_path)


def test_incremental_harbor_build_never_pulls_an_absent_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from benchmaxxing.providers import interactive_preparation

    folder = harbor_task(tmp_path)
    spec = folder / "task.toml"
    spec.write_text(
        spec.read_text().replace("terminal-bench/repair", "terminal-bench/sound-change-cascade")
    )
    (task,) = convert("terminal-bench-4", tmp_path)
    task.recipe.settings.update(
        {
            "offline_profile": "sound-change-cascade-offline-v1",
            "reviewed_source_sha256": interactive_preparation._HARBOR_PROFILES[
                "sound-change-cascade-offline-v1"
            ]["fingerprint"],
        }
    )

    def missing(*args: Any) -> None:
        raise EnvironmentError("No such image")

    monkeypatch.setattr(interactive_preparation, "_inspect_image", missing)
    monkeypatch.setattr(
        interactive_preparation, "download", lambda *a, **k: pytest.fail("downloaded")
    )
    monkeypatch.setattr(interactive_preparation, "_docker", lambda *a, **k: pytest.fail("pulled"))
    with pytest.raises(PreparationError, match="pinned Python 3.12 base"):
        interactive_preparation.prepare_harbor_profile(tmp_path, task)


def test_prepacked_harbor_profile_never_pulls_an_absent_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from benchmaxxing.providers import interactive_preparation, registry

    folder = harbor_task(tmp_path, "regex-log")
    spec = folder / "task.toml"
    spec.write_text(spec.read_text().replace("terminal-bench/repair", "terminal-bench/regex-log"))
    fingerprint = interactive_preparation._HARBOR_PROFILES["regex-log-offline-v1"]["fingerprint"]
    monkeypatch.setattr(registry, "source_hash", lambda _: fingerprint)
    (task,) = convert_harbor(
        folder, suite_id="terminal-bench-2-1", revision="frozen", source_sha256=DIGEST
    )

    def missing(*args: Any) -> None:
        raise EnvironmentError("No such image")

    monkeypatch.setattr(interactive_preparation, "_inspect_image", missing)
    monkeypatch.setattr(
        interactive_preparation, "download", lambda *a, **k: pytest.fail("downloaded")
    )
    monkeypatch.setattr(interactive_preparation, "_docker", lambda *a, **k: pytest.fail("pulled"))
    with pytest.raises(PreparationError, match="reviewed cancel-async-tasks"):
        interactive_preparation.prepare_harbor_profile(tmp_path, task)


def test_webarena_plan_preserves_unknown_sizes_and_never_marks_ready(tmp_path: Path) -> None:
    from benchmaxxing.providers.interactive_preparation import webarena_preparation_plan

    plan = webarena_preparation_plan(tmp_path)
    assert plan["state"] == "blocked"
    assert plan["measured_map_archive_bytes"] == sum(item["bytes"] or 0 for item in plan["assets"])
    assert plan["estimated_full_data_bytes"] > plan["storage_limit_bytes"]
    assert plan["assets"][-1]["bytes"] is None
    assert all(image["image_id"] is None for image in plan["images"])
    assert plan["blockers"]


def test_miniwob_copies_support_assets_and_does_not_expose_html(tmp_path: Path) -> None:
    write(tmp_path, "miniwob/html/miniwob/click-test.html", "<script>PRIVATE_REWARD_LOGIC</script>")
    write(tmp_path, "miniwob/html/core/core.js", "// shared seeded task engine")
    (task,) = convert("miniwob", tmp_path)
    assert task.upstream_id == "miniwob.click-test"
    assert len(task.assets) == 2
    assert all(asset.scope == "runtime" for asset in task.assets)
    assert "PRIVATE_REWARD_LOGIC" not in json.dumps(task.public_files)
    assert task.recipe.network == "none"
    assert task.recipe.tools == "browser"
    assert task.recipe.settings["browser_config"]["seed"] == 0


def test_miniwob_does_not_relabel_unregistered_demos_as_benchmark_tasks(tmp_path: Path) -> None:
    write(tmp_path, "html/miniwob/click-test.html", "<button>click</button>")
    write(tmp_path, "html/miniwob/chase-circle.html", "<canvas>demo</canvas>")
    (task,) = convert("miniwob", tmp_path)
    assert task.upstream_id == "miniwob.click-test"
    assert len(task.assets) == 2


def browser_record() -> dict[str, Any]:
    return {
        "task_id": 3,
        "intent_template_id": 14,
        "revision": 2,
        "intent": "Read the total shown in the local shop.",
        "sites": ["shopping"],
        "start_urls": ["__SHOPPING__"],
        "eval": [{"evaluator": "AgentResponseEvaluator", "expected": {"answer": "HIDDEN"}}],
    }


def test_verified_preserves_task_revision_and_hides_evaluator(tmp_path: Path) -> None:
    write(tmp_path, "webarena-verified.json", json.dumps([browser_record()]))
    (task,) = convert("webarena-verified", tmp_path)
    assert task.upstream_id == "webarena_verified.14.3.2"
    assert task.revision == "frozen-revision"
    assert "HIDDEN" not in json.dumps(task.public_files)
    assert "HIDDEN" in task.private_files["task.json"]
    assert "expected" not in json.dumps(task.recipe.settings)
    assert task.recipe.settings["requires_fresh_services"] is True
    assert task.recipe.network == "task-local"


def test_browser_dataset_rejects_duplicate_identity(tmp_path: Path) -> None:
    write(tmp_path, "webarena-verified.json", json.dumps([browser_record(), browser_record()]))
    with pytest.raises(InteractiveImportError, match="Duplicate"):
        convert("webarena-verified", tmp_path)


def test_visual_remote_judge_is_explicitly_blocked(tmp_path: Path) -> None:
    record = browser_record()
    record["eval"] = {"reference_answers": {"fuzzy_match": ["expected"]}}
    write(tmp_path, "test_raw.json", json.dumps([record]))
    (task,) = convert("visualwebarena", tmp_path)
    assert any("external model judge" in blocker for blocker in task.recipe.blockers)
    assert any("blip2" in requirement for requirement in task.recipe.requirements)


def test_visual_split_ids_cannot_silently_become_global_ids(tmp_path: Path) -> None:
    write(tmp_path, "config_files/vwa/test_classifieds.raw.json", json.dumps([browser_record()]))
    with pytest.raises(InteractiveImportError, match="test_raw.json"):
        convert("visualwebarena", tmp_path)


def test_workarena_metadata_is_never_marked_offline_runnable(tmp_path: Path) -> None:
    write(tmp_path, "workarena-tasks.json", json.dumps([browser_record()]))
    (task,) = convert("workarena", tmp_path)
    assert task.recipe.network == "external"
    assert any("not offline" in blocker for blocker in task.recipe.blockers)
    with pytest.raises(ValueError, match="hosted WorkArena"):
        worker.preflight({"schema_version": 1, "suite_id": "workarena"})


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("eval", {"code": "print('arbitrary')"}),
        ("upload_file", {"bid": "1", "file": "/grader/task.json"}),
        ("click", {"bid": "1", "unexpected": "x"}),
        ("goto", {"url": "file:///grader/task.json"}),
        ("goto", {"url": "http://internet.example"}),
        ("mouse_click", {"x": True, "y": 2}),
        ("noop", {"wait_ms": 10001}),
        ("tab_focus", {"index": -1}),
    ],
)
def test_action_bridge_rejects_unsafe_or_malformed_calls(name: str, arguments: Any) -> None:
    with pytest.raises(ValueError):
        worker.action_code(name, arguments, {"suite_id": "webarena-verified", "url_map": {}})


def test_action_text_is_one_literal_not_code() -> None:
    payload = "'); __import__('os').system('boom'); #\n"
    code = worker.action_code("fill", {"bid": "4", "value": payload}, {})
    tree = ast.parse(code)
    assert len(tree.body) == 1
    expression = tree.body[0]
    assert isinstance(expression, ast.Expr) and isinstance(expression.value, ast.Call)
    assert ast.literal_eval(expression.value.keywords[1].value) == payload


def test_browser_requires_fresh_reset_before_importing_optional_packages() -> None:
    with pytest.raises(ValueError, match="per-attempt"):
        worker.preflight({"schema_version": 1, "suite_id": "webarena-verified"})


class FakeEnvironment:
    def _get_obs(self) -> dict[str, Any]:
        return {"goal": "Refreshed browser state", "open_pages_urls": ["http://shop/"]}

    def reset(self, *, seed: int) -> tuple[dict[str, Any], dict[str, Any]]:
        assert seed == 17
        return {
            "goal": "Click local button",
            "url": "http://shop/",
            "open_pages_urls": ["http://shop/"],
        }, {"private_expected_answer": "HIDDEN"}

    def step(self, code: str) -> tuple[dict[str, Any], float, bool, bool, dict[str, Any]]:
        assert code.startswith("click(")
        return (
            {"goal": "Click local button", "open_pages_urls": ["http://shop/"]},
            1.0,
            True,
            False,
            {"private_expected_answer": "HIDDEN"},
        )

    def close(self) -> None:
        pass


def test_worker_lifecycle_keeps_reward_private_and_writes_authoritative_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker, "create_environment", lambda config: FakeEnvironment())
    session = worker.BrowserSession(
        {
            "suite_id": "webarena-verified",
            "task_id": 3,
            "seed": 17,
            "url_map": {"shopping": "http://shop/"},
        },
        tmp_path / "result.json",
    )
    reset = session.request({"op": "reset"})
    assert session.request({"op": "observe"})["observation"]["goal"] == "Refreshed browser state"
    with pytest.raises(ValueError, match="exactly one reset"):
        session.request({"op": "reset"})
    step = session.request({"op": "action", "name": "click", "arguments": {"bid": "1"}})
    assert step["terminated"]
    public = json.dumps([reset, step])
    assert "HIDDEN" not in public and "reward" not in public
    with pytest.raises(ValueError, match="ended"):
        session.request({"op": "action", "name": "click", "arguments": {"bid": "1"}})
    final = session.request({"op": "finalize", "response": "done"})
    assert "score" not in final
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["passed"] and result["score"] == 1
    assert result["evidence"]["seed"] == 17 and result["evidence"]["steps"] == 1


def test_jsonl_malformed_request_does_not_claim_completion(tmp_path: Path) -> None:
    session = worker.BrowserSession({}, tmp_path / "result.json")
    output = io.StringIO()
    worker.serve(session, io.StringIO('not json\n{"id":4,"op":"observe"}\n'), output)
    records = [json.loads(line) for line in output.getvalue().splitlines()]
    assert len(records) == 2 and not any(record["ok"] for record in records)
    assert records[1]["id"] == 4
    assert not (tmp_path / "result.json").exists()


def test_observation_omits_private_metadata_and_blocks_file_navigation() -> None:
    config = {"suite_id": "webarena-verified", "url_map": {"shopping": "http://shop/"}}
    result = worker.public_observation(
        {"goal": "read", "private_answer": "HIDDEN", "reward": 1, "info": {"expected": "HIDDEN"}},
        config,
    )
    assert "HIDDEN" not in json.dumps(result) and "reward" not in result
    with pytest.raises(ValueError, match="left the allowed"):
        worker.public_observation({"open_pages_urls": ["file:///grader/task.json"]}, config)


def test_structured_goal_accepts_browsergym_tuple_and_preserves_input_image() -> None:
    result = worker.public_observation(
        {
            "goal_object": (
                {"type": "text", "text": "Find this image", "private": "hidden"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,YQ=="}},
                {"type": "image_url", "image_url": {"url": "https://outside.example/a.png"}},
            )
        },
        {},
    )
    assert len(result["goal_object"]) == 2
    assert result["goal_object"][1]["image_url"]["url"].startswith("data:image/png;")
    assert "hidden" not in json.dumps(result)
    assert "upload_file" not in result["available_actions"]


class FakeProcess:
    def __init__(self) -> None:
        self.stdin = io.BytesIO()

    def poll(self) -> None:
        return None


def controller(tmp_path: Path) -> transport.BrowserEnvironment:
    write(tmp_path, "html/miniwob/click-test.html", "<button>click</button>")
    (task,) = convert("miniwob", tmp_path)
    instance = transport.BrowserEnvironment(
        tmp_path / "data", "controller-test", Budgets(memory_mb=1024, pids=256), task
    )
    instance._process = FakeProcess()  # type: ignore[assignment]
    return instance


def test_controller_preserves_response_sequence_and_recoverable_action_errors(
    tmp_path: Path,
) -> None:
    instance = controller(tmp_path)
    instance._responses.put(b'{"id":1,"ok":true,"observation":{"goal":"public"}}\n')
    assert instance.observe() == {"observation": {"goal": "public"}}
    instance._responses.put(b'{"id":2,"ok":false,"error":"Unsupported action"}\n')
    with pytest.raises(ToolOperationError, match="Unsupported action"):
        instance.action("eval", {})


def test_controller_malformed_response_closes_its_owned_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = controller(tmp_path)
    closed = []
    monkeypatch.setattr(instance, "close", lambda: closed.append(True))
    instance._responses.put(b'{"id":999,"ok":true}\n')
    with pytest.raises(EnvironmentError, match="sequence"):
        instance.observe()
    assert closed == [True]


def test_controller_enforces_output_and_operation_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = controller(tmp_path)
    closed = []
    monkeypatch.setattr(instance, "close", lambda: closed.append(True))
    instance._overflow.set()
    with pytest.raises(OutputLimitExceeded):
        instance.observe()
    assert closed == [True]
    instance._overflow.clear()
    with pytest.raises(ExecutionTimeout):
        instance._rpc({"op": "observe"}, timeout=0.001)
    assert closed == [True, True]


def test_controller_checks_cancellation_before_sending_a_browser_action(tmp_path: Path) -> None:
    instance = controller(tmp_path)
    instance.cancel = threading.Event()
    instance.cancel.set()
    with pytest.raises(ExecutionCancelled):
        instance.action("click", {"bid": "1"})
    assert instance._process.stdin.getvalue() == b""  # type: ignore[union-attr]


def test_fresh_service_recipe_cannot_attach_to_shared_or_external_websites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = controller(tmp_path)
    image = "sha256:" + "a" * 64
    monkeypatch.setattr(transport, "_inspect_image", lambda identity: {"Id": identity})
    with pytest.raises(PreparationError, match="fresh owned service"):
        instance._services(
            {
                "snapshot_semantics": "fresh-image-per-attempt",
                "services": [{"name": "shop", "image_id": image, "memory_mb": 128}],
                "url_map": {"shopping": "http://shared-existing-service/"},
            }
        )
    assert instance._network is None and instance._containers == []


@pytest.fixture
def prepared_miniwob(tmp_path: Path) -> tuple[Path, Any]:
    from benchmaxxing.providers.preparation import prepare_imported
    from benchmaxxing.providers.registry import convert as convert_source
    from benchmaxxing.providers.registry import get_imported, install

    source_name = os.environ.get("BENCHMAXXING_MINIWOB_SOURCE")
    if not source_name:
        pytest.skip("Set BENCHMAXXING_MINIWOB_SOURCE to the explicitly prepared frozen HTML source")
    source = Path(source_name)
    tasks = convert_source("miniwob", source, revision="7fd85d71a4b60325c6585396ec4f48377d049838")
    assert len(tasks) == 125
    task = next(task for task in tasks if task.upstream_id == "miniwob.click-test")
    data = tmp_path / "data"
    install(data, source, [task])
    task = get_imported(data, task.id)
    prepare_imported(
        data,
        task,
        image=os.environ.get("BENCHMAXXING_MINIWOB_IMAGE", "benchmaxxing/miniwob-frozen:0.14.3"),
    )
    return data, task


@pytest.mark.docker
@pytest.mark.parametrize("click", [False, True])
def test_prepared_browser_offline_native_grade(
    prepared_miniwob: tuple[Path, Any], click: bool
) -> None:
    from benchmaxxing.contracts import CheckResult
    from benchmaxxing.environment import _docker

    data, task = prepared_miniwob
    environment = transport.BrowserEnvironment(
        data,
        "miniwob-" + uuid.uuid4().hex,
        Budgets(memory_mb=1024, pids=256, command_seconds=30, artifact_bytes=8 * 1024 * 1024),
        task,
    )
    try:
        environment.start()
        inspect = json.loads(_docker(["inspect", environment.name])["stdout"])[0]
        assert inspect["HostConfig"]["NetworkMode"] == "none"
        assert inspect["Config"]["User"] == "65534:65534"
        assert all(not mount["RW"] for mount in inspect["Mounts"] if mount["Type"] == "bind")
        observed = environment.observe()["observation"]
        assert observed["goal"] == "Click the button."
        assert observed["screenshot_png_base64"]
        for url in ("file:///grader/task.json", "https://example.com/"):
            with pytest.raises(ToolOperationError, match="restricted"):
                environment.action("goto", {"url": url})
        if click:
            button = next(
                node
                for node in observed["axtree_object"]["nodes"]
                if node.get("role", {}).get("value") == "button"
            )
            assert environment.action("click", {"bid": button["browsergym_id"]})["terminated"]
        grade = environment.finalize("Clicked the button" if click else "No action")
        assert grade["passed"] is click and grade["score"] == int(click)
        assert grade["evidence"]["seed"] == 0
        assert all(CheckResult.model_validate(check) for check in grade["checks"])
    finally:
        environment.close()
    assert _docker(["inspect", environment.name])["exit_code"] != 0


@pytest.mark.docker
def test_prepared_browser_timeout_removes_owned_worker(prepared_miniwob: tuple[Path, Any]) -> None:
    from benchmaxxing.environment import _docker

    data, task = prepared_miniwob
    environment = transport.BrowserEnvironment(
        data,
        "miniwob-timeout-" + uuid.uuid4().hex,
        Budgets(memory_mb=1024, pids=256, command_seconds=0.1, artifact_bytes=8 * 1024 * 1024),
        task,
    )
    try:
        environment.start()
        with pytest.raises(ExecutionTimeout):
            environment.action("noop", {"wait_ms": 1000})
    finally:
        environment.close()
    assert _docker(["inspect", environment.name])["exit_code"] != 0
