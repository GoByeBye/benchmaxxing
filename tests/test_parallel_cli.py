"""Multi-model CLI configuration with fake execution and HTTP only."""

import json
import threading
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from benchmaxxing import cli
from benchmaxxing.contracts import RunRecord, RunSpec
from benchmaxxing.controller import Controller
from benchmaxxing.storage import Store

runner = CliRunner()


def fake_execution(store: Store, run_id: str, cancel: threading.Event | None) -> RunRecord:
    """Finish a record without launching a task environment or model request."""
    return store.update_run(run_id, status="passed")


def write_model_config(path: Path, model: str, port: int, tokens: int, priority: int) -> None:
    path.write_text(
        json.dumps(
            {
                "participant": "openai",
                "inference": {
                    "base_url": f"http://127.0.0.1:{port}/v1",
                    "model": model,
                    "vllm_priority": priority,
                },
                "generation": {"max_tokens": tokens},
                "budgets": {"max_turns": tokens // 10},
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize("same_endpoint", [False, True])
def test_suite_interleaves_models_and_preserves_each_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, same_endpoint: bool
) -> None:
    submitted: list[RunSpec] = []
    owners: list[Controller] = []

    class FakeExecutionController(Controller):
        def __init__(self, directory: Path):
            super().__init__(directory, execute=fake_execution, cleanup=lambda _: None)
            owners.append(self)

        def submit(self, spec: RunSpec) -> RunRecord:
            submitted.append(spec)
            return super().submit(spec)

    monkeypatch.setattr(cli, "Controller", FakeExecutionController)
    monkeypatch.setattr(cli, "_remote", lambda _: None)
    configs = [tmp_path / "a.json", tmp_path / "b.json"]
    ports = [8081, 8081 if same_endpoint else 8082]
    write_model_config(configs[0], "model-a", ports[0], 70, -2)
    write_model_config(configs[1], "model-b", ports[1], 110, 0)
    result = runner.invoke(
        cli.app,
        [
            "suite",
            "--config",
            str(configs[0]),
            "--config",
            str(configs[1]),
            "--task",
            "python.interval-merge",
            "--task",
            "python.shortest-route",
            "--repeat",
            "2",
            "--parallel",
            "2",
            "--data-dir",
            str(tmp_path / "data"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert [(spec.task_id, spec.inference.model) for spec in submitted] == [
        (task, model)
        for _ in range(2)
        for task in ["python.interval-merge", "python.shortest-route"]
        for model in ["model-a", "model-b"]
    ]
    for index, spec in enumerate(submitted):
        model_index = index % 2
        assert spec.inference.base_url == f"http://127.0.0.1:{ports[model_index]}/v1"
        assert spec.inference.vllm_priority == [-2, 0][model_index]
        assert spec.generation.max_tokens == [70, 110][model_index]
        assert spec.budgets.max_turns == [7, 11][model_index]
    summary = json.loads(result.stdout)
    assert summary["attempts"] == 8
    assert [item["model"] for item in summary["results"]] == ["model-a", "model-b"] * 4
    assert len({item["id"] for item in summary["results"]}) == 8
    owner = owners[0]
    assert owner.settings.max_parallel_runs == 2
    assert json.loads(owner.settings_path.read_text())["max_parallel_runs"] == 2
    assert all(
        run.metadata["execution"]["max_parallel_runs"] == 2 for run in owner.store.list_runs()
    )


class FakeRemoteController:
    """HTTP stand-in that records configuration before accepting completed runs."""

    def __init__(self, directory: Path, *, reject_settings: bool = False):
        self.store = Store(directory)
        self.requests: list[tuple[str, str]] = []
        self.specs: list[RunSpec] = []
        self.settings: dict[str, int] | None = None
        self.reject_settings = reject_settings
        self.client = httpx.Client(
            base_url="http://127.0.0.1:8123", transport=httpx.MockTransport(self.respond)
        )

    def respond(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path))
        assert request.headers["X-Benchmaxxing-Token"] == "test-session"
        if request.method == "PUT" and request.url.path == "/api/v1/execution/settings":
            if self.reject_settings:
                return httpx.Response(503, json={"detail": "fake settings failure"})
            self.settings = json.loads(request.content)
            return httpx.Response(200, json=self.settings)
        assert request.method == "POST" and request.url.path == "/api/v1/runs"
        spec = RunSpec.model_validate_json(request.content)
        self.specs.append(spec)
        record = self.store.create_run(spec)
        record = self.store.update_run(record.id, status="passed")
        return httpx.Response(201, json=record.model_dump(mode="json"))

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            cli, "_remote", lambda _: (self.client, {"X-Benchmaxxing-Token": "test-session"})
        )


@pytest.mark.parametrize("parallel", [None, 3])
def test_suite_configures_attached_controller_only_when_requested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, parallel: int | None
) -> None:
    remote = FakeRemoteController(tmp_path / "data")
    remote.install(monkeypatch)
    args = ["suite", "--task", "python.interval-merge", "--data-dir", str(tmp_path / "data")]
    if parallel is not None:
        args.extend(["--parallel", str(parallel)])
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    assert remote.requests == (
        [("PUT", "/api/v1/execution/settings")] if parallel is not None else []
    ) + [("POST", "/api/v1/runs")]
    assert remote.settings == ({"max_parallel_runs": 3} if parallel is not None else None)
    assert remote.client.is_closed


def test_suite_does_not_submit_when_remote_parallel_setting_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote = FakeRemoteController(tmp_path / "data", reject_settings=True)
    remote.install(monkeypatch)
    result = runner.invoke(
        cli.app,
        ["suite", "--task", "python.interval-merge", "--parallel", "2"],
    )
    assert result.exit_code == 2
    assert remote.requests == [("PUT", "/api/v1/execution/settings")]
    assert remote.store.list_runs() == []
    assert remote.client.is_closed


@pytest.mark.parametrize("parallel", ["0", "9", "1.5"])
def test_suite_rejects_invalid_parallel_limit_before_creating_data(
    tmp_path: Path, parallel: str
) -> None:
    directory = tmp_path / "data"
    result = runner.invoke(
        cli.app,
        ["suite", "--parallel", parallel, "--data-dir", str(directory)],
    )
    assert result.exit_code == 2
    assert not directory.exists()


@pytest.mark.parametrize("override", [None, 0, -7])
def test_run_priority_override_preserves_explicit_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, override: int | None
) -> None:
    config = tmp_path / "model.json"
    write_model_config(config, "model-a", 8081, 70, -2)
    remote = FakeRemoteController(tmp_path / "data")
    remote.install(monkeypatch)
    args = ["run", "--config", str(config)]
    if override is not None:
        args.extend(["--vllm-priority", str(override)])
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    assert len(remote.specs) == 1
    assert remote.specs[0].inference.vllm_priority == (-2 if override is None else override)
    assert remote.client.is_closed


def test_run_priority_requires_model_before_creating_data(tmp_path: Path) -> None:
    directory = tmp_path / "data"
    result = runner.invoke(cli.app, ["run", "--vllm-priority", "0", "--data-dir", str(directory)])
    assert result.exit_code == 2
    assert not directory.exists()


def test_run_priority_with_explicit_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote = FakeRemoteController(tmp_path / "data")
    remote.install(monkeypatch)
    result = runner.invoke(
        cli.app,
        [
            "run",
            "--endpoint",
            "http://127.0.0.1:8081/v1",
            "--model",
            "explicit-model",
            "--vllm-priority",
            "0",
        ],
    )
    assert result.exit_code == 0, result.output
    assert len(remote.specs) == 1
    assert remote.specs[0].inference.model == "explicit-model"
    assert remote.specs[0].inference.vllm_priority == 0


@pytest.mark.parametrize("priority", ["2147483648", "-2147483649"])
def test_run_rejects_out_of_range_priority_before_creating_data(
    tmp_path: Path, priority: str
) -> None:
    directory = tmp_path / "data"
    result = runner.invoke(
        cli.app,
        [
            "run",
            "--endpoint",
            "http://127.0.0.1:8081/v1",
            "--model",
            "explicit-model",
            "--vllm-priority",
            priority,
            "--data-dir",
            str(directory),
        ],
    )
    assert result.exit_code == 2
    assert not directory.exists()
