"""Catalog selection and controller reuse, with fake execution and HTTP only."""

import json
import threading
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from benchmaxxing import cli
from benchmaxxing.api import create_app
from benchmaxxing.contracts import Budgets, RunRecord, RunSpec
from benchmaxxing.controller import Controller
from benchmaxxing.grading import grade_submission
from benchmaxxing.storage import Store
from benchmaxxing.task import get_definition, get_task

runner = CliRunner()


def fake_execution(store: Store, run_id: str, cancel: threading.Event | None) -> RunRecord:
    """Record completion without evaluating any task code or launching Docker."""
    return store.update_run(run_id, status="passed")


@pytest.fixture
def catalog_client(tmp_path: Path):
    owner = Controller(tmp_path, execute=fake_execution, cleanup=lambda _: None)
    with TestClient(
        create_app(tmp_path, controller=owner), base_url="http://127.0.0.1:8123"
    ) as client:
        yield client


def test_api_and_cli_catalog_agree_without_private_material(catalog_client: TestClient) -> None:
    response = catalog_client.get("/api/v1/tasks")
    assert response.status_code == 200
    catalog = {item["id"]: item for item in response.json()}
    selected = catalog["python.topological-order"]
    assert selected == get_task("python.topological-order").model_dump(mode="json")
    assert selected["submission_file"] == "solution.py"
    assert selected["check_count"] >= 6
    assert catalog["terminal.log-triage"]["submission_file"] == "submission.json"
    assert catalog["terminal.log-triage"]["kind"] == "json"
    for item in catalog.values():
        assert not {"files", "cases", "good_solution", "bad_solution"}.intersection(item)
    private = get_definition("python.topological-order")
    assert private.good_solution not in response.text
    assert "SYNTHETIC-NOT-A-REAL-KEY" not in response.text
    listed = runner.invoke(
        cli.app,
        [
            "tasks",
            "--category",
            "terminal",
            "--data-dir",
            str(catalog_client.app.state.controller.store.data_dir),
        ],
    )
    assert listed.exit_code == 0, listed.output
    assert json.loads(listed.stdout) == [
        item for item in catalog.values() if item["category"] == "terminal"
    ]


def test_api_unknown_installed_task_is_422_without_persisting(catalog_client: TestClient) -> None:
    token = catalog_client.get("/api/v1/session").json()["token"]
    response = catalog_client.post(
        "/api/v1/runs",
        json={"task_id": "python.not-an-installed-task"},
        headers={"X-Benchmaxxing-Token": token},
    )
    assert response.status_code == 422
    assert "Unknown installed task" in response.text
    assert catalog_client.get("/api/v1/runs").json() == []


class FakeRemoteController:
    """A local HTTP controller stand-in backed by real records, never task execution."""

    def __init__(self, data_dir: Path, *, fail_submission: int | None = None):
        self.store = Store(data_dir)
        self.submitted: list[RunSpec] = []
        self.cancelled: list[str] = []
        self.polled: list[str] = []
        self.fail_submission = fail_submission
        self.post_count = 0
        self.client_options: list[dict] = []
        self.client = httpx.Client(
            base_url="http://127.0.0.1:8123", transport=httpx.MockTransport(self.respond)
        )

    def respond(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/health":
            return httpx.Response(200, json={"workspace_id": self.store.workspace_id})
        if request.method == "POST":
            assert request.headers["X-Benchmaxxing-Token"] == "local-test-session"
        if request.method == "POST" and path == "/api/v1/runs":
            self.post_count += 1
            if self.post_count == self.fail_submission:
                return httpx.Response(503, json={"detail": "fake controller unavailable"})
            spec = RunSpec.model_validate_json(request.content)
            self.submitted.append(spec)
            record = self.store.create_run(spec)
            return httpx.Response(201, json=record.model_dump(mode="json"))
        if request.method == "POST" and path.endswith("/cancel"):
            run_id = path.split("/")[-2]
            self.cancelled.append(run_id)
            record = self.store.update_run(run_id, status="cancelled")
            return httpx.Response(200, json=record.model_dump(mode="json"))
        if request.method == "GET" and path.startswith("/api/v1/runs/"):
            run_id = path.rsplit("/", 1)[1]
            self.polled.append(run_id)
            record = self.store.update_run(run_id, status="passed")
            return httpx.Response(200, json={"run": record.model_dump(mode="json")})
        raise AssertionError(f"Unexpected mock request: {request.method} {path}")

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def client_factory(**kwargs):
            self.client_options.append(kwargs)
            return self.client

        def unexpected_headless(*args, **kwargs):
            raise AssertionError("A reachable controller must be reused")

        monkeypatch.setattr(cli.httpx, "Client", client_factory)
        monkeypatch.setattr(cli, "Controller", unexpected_headless)
        monkeypatch.setattr(
            cli,
            "read_descriptor",
            lambda _: {"base_url": "http://127.0.0.1:8123", "token": "local-test-session"},
        )


def test_run_task_override_preserves_configuration_and_attaches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote = FakeRemoteController(tmp_path / "data")
    remote.install(monkeypatch)
    config = tmp_path / "run.json"
    config.write_text(
        json.dumps(
            {
                "task_id": "python.interval-merge",
                "participant": "openai",
                "inference": {"base_url": "http://127.0.0.1:8081/v1", "model": "local-test"},
                "generation": {"max_tokens": 123, "temperature": 0.25, "seed": 11},
                "budgets": {"max_turns": 7, "wall_seconds": 90},
            }
        ),
        encoding="utf-8",
    )
    result = runner.invoke(
        cli.app,
        [
            "run",
            "--config",
            str(config),
            "--task",
            "python.topological-order",
            "--data-dir",
            str(remote.store.data_dir),
        ],
    )
    assert result.exit_code == 0, result.output
    assert len(remote.submitted) == 1
    spec = remote.submitted[0]
    assert spec.task_id == "python.topological-order"
    assert spec.participant == "openai" and spec.inference.model == "local-test"
    assert spec.generation.max_tokens == 123 and spec.generation.seed == 11
    assert spec.generation.temperature == 0.25
    assert spec.budgets.max_turns == 7 and spec.budgets.wall_seconds == 90
    record = json.loads(result.stdout)
    assert record["spec"]["task_id"] == spec.task_id and record["status"] == "passed"
    assert remote.polled == [record["id"]]
    assert remote.client_options == [
        {
            "base_url": "http://127.0.0.1:8123",
            "trust_env": False,
            "follow_redirects": False,
            "timeout": 5,
        }
    ]
    assert remote.client.is_closed


def test_suite_attachment_creates_independent_repeats_and_keeps_model_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote = FakeRemoteController(tmp_path / "data")
    remote.install(monkeypatch)
    config = tmp_path / "suite.toml"
    config.write_text(
        'participant = "openai"\n'
        'task_id = "python.interval-merge"\n'
        '[inference]\nbase_url = "http://127.0.0.1:8081/v1"\nmodel = "local-suite"\n'
        "[budgets]\nmax_turns = 9\nwall_seconds = 75\n"
        "[generation]\nmax_tokens = 456\nseed = 99\n",
        encoding="utf-8",
    )
    ids = ["python.topological-order", "python.shortest-route"]
    result = runner.invoke(
        cli.app,
        [
            "suite",
            "--config",
            str(config),
            "--task",
            ids[0],
            "--task",
            ids[1],
            "--category",
            "programming",
            "--repeat",
            "2",
            "--data-dir",
            str(remote.store.data_dir),
        ],
    )
    assert result.exit_code == 0, result.output
    summary = json.loads(result.stdout)
    assert (summary["attempts"], summary["task_count"], summary["repeat"]) == (4, 2, 2)
    attempted = [item["id"] for item in summary["results"]]
    assert len(set(attempted)) == 4
    assert set(remote.polled) == set(attempted)
    assert [spec.task_id for spec in remote.submitted].count(ids[0]) == 2
    assert [spec.task_id for spec in remote.submitted].count(ids[1]) == 2
    for spec in remote.submitted:
        assert spec.participant == "openai" and spec.inference.model == "local-suite"
        assert spec.budgets.max_turns == 9 and spec.budgets.wall_seconds == 75
        assert spec.generation.max_tokens == 456 and spec.generation.seed == 99
    assert len(remote.client_options) == 1
    assert remote.client.is_closed


def test_suite_headless_selection_and_variant_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instances = []

    class FakeHeadlessController:
        def __init__(self, data_dir):
            self.store = Store(data_dir)
            self.specs = []
            self.started = False
            self.stopped = False
            instances.append(self)

        def start(self):
            self.started = True

        def submit(self, spec):
            assert self.started
            self.specs.append(spec)
            run = self.store.create_run(spec)
            return self.store.update_run(run.id, status="passed")

        def stop(self):
            self.stopped = True

    monkeypatch.setattr(cli, "_remote", lambda _: None)
    monkeypatch.setattr(cli, "Controller", FakeHeadlessController)
    config = tmp_path / "suite.json"
    config.write_text(json.dumps({"scripted_variant": "good", "budgets": {"max_turns": 6}}))
    result = runner.invoke(
        cli.app,
        [
            "suite",
            "--config",
            str(config),
            "--category",
            "terminal",
            "--task",
            "terminal.log-triage",
            "--task",
            "terminal.config-overlay",
            "--repeat",
            "2",
            "--variant",
            "bad",
            "--data-dir",
            str(tmp_path / "data"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert len(instances) == 1 and instances[0].started and instances[0].stopped
    owner = instances[0]
    assert len(owner.specs) == 4
    assert {spec.task_id for spec in owner.specs} == {
        "terminal.log-triage",
        "terminal.config-overlay",
    }
    assert all(
        spec.scripted_variant == "bad" and spec.budgets.max_turns == 6 for spec in owner.specs
    )
    summary = json.loads(result.stdout)
    assert len({item["id"] for item in summary["results"]}) == 4


def test_suite_submission_failure_preserves_accepted_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote = FakeRemoteController(tmp_path / "data", fail_submission=3)
    remote.install(monkeypatch)
    result = runner.invoke(
        cli.app,
        [
            "suite",
            "--task",
            "python.topological-order",
            "--repeat",
            "3",
            "--data-dir",
            str(remote.store.data_dir),
        ],
    )
    assert result.exit_code == 2, result.output
    assert "accepted controller runs remain queued/running" in result.output
    records = remote.store.list_runs()
    assert len(records) == 2 and all(record.status == "queued" for record in records)
    assert not remote.cancelled
    assert not remote.polled
    assert remote.client.is_closed


@pytest.mark.parametrize("change", ["synthetic_secret_extra", "missing_field", "duplicate_field"])
def test_refusal_submission_requires_exact_rubric_fields(tmp_path: Path, change: str) -> None:
    definition = get_definition("refusal.protect-synthetic-secret")
    document = json.loads(definition.good_solution)
    if change == "synthetic_secret_extra":
        document["extra"] = json.loads(definition.files["fixture.json"])["credential"]
    elif change == "missing_field":
        del document["secret_disclosed"]
    payload = json.dumps(document)
    if change == "duplicate_field":
        payload = '{"decision":"SYNTHETIC-NOT-A-REAL-KEY-917",' + payload[1:]
    score = grade_submission(
        payload.encode(), tmp_path, "shape-check", Budgets(), task_id=definition.id
    )
    assert not score.passed
    assert any(not check.passed for check in score.checks)
