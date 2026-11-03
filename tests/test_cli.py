import json
from pathlib import Path

import httpx
from typer.testing import CliRunner

from benchmaxxing import cli
from benchmaxxing.cli import app
from benchmaxxing.contracts import RunSpec
from benchmaxxing.storage import Store

runner = CliRunner()


def test_cli_rejects_invalid_config_without_run(tmp_path: Path) -> None:
    config = tmp_path / "bad.json"
    config.write_text(json.dumps({"participant": "openai"}))
    result = runner.invoke(
        app, ["run", "--config", str(config), "--data-dir", str(tmp_path / "data")]
    )
    assert result.exit_code == 2
    assert "Invalid run configuration" in result.output
    assert not (tmp_path / "data").exists()


def test_export_refuses_to_replace_existing_file(tmp_path: Path) -> None:
    store = Store(tmp_path / "data")
    run = store.create_run(RunSpec())
    store.update_run(run.id, status="passed")
    output = tmp_path / "run.zip"
    output.write_bytes(b"keep")
    result = runner.invoke(
        app,
        [
            "export",
            run.id,
            "--output",
            str(output),
            "--data-dir",
            str(store.data_dir),
        ],
    )
    assert result.exit_code == 2
    assert output.read_bytes() == b"keep"


def test_attach_reuses_health_checked_client_without_reopening(tmp_path: Path, monkeypatch) -> None:
    store = Store(tmp_path)
    queued = store.create_run(RunSpec())
    finished = store.update_run(queued.id, status="passed")

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("health"):
            return httpx.Response(200, json={"workspace_id": store.workspace_id})
        if request.method == "POST":
            assert request.headers["X-Benchmaxxing-Token"] == "test-session"
            return httpx.Response(201, json=queued.model_dump(mode="json"))
        return httpx.Response(200, json={"run": finished.model_dump(mode="json"), "artifacts": []})

    client = httpx.Client(base_url="http://127.0.0.1:8123", transport=httpx.MockTransport(respond))
    client.get("/api/v1/health")  # Discovery already opens HTTPX's client state.
    monkeypatch.setattr(
        cli, "_remote", lambda _: (client, {"X-Benchmaxxing-Token": "test-session"})
    )
    result = runner.invoke(app, ["run", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["status"] == "passed"
    assert client.is_closed
