import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from benchmaxxing.api import create_app
from benchmaxxing.contracts import RunRecord
from benchmaxxing.controller import Controller
from benchmaxxing.storage import Store


def fake_execution(store: Store, run_id: str, cancel: threading.Event | None) -> RunRecord:
    store.update_run(run_id, status="running")
    store.append_event(run_id, "tool_result", {"stdout": "<script>alert(1)</script>"})
    store.add_artifact(run_id, "solution.py", b"print('hello')")
    store.append_event(run_id, "complete", {"status": "passed"})
    return store.update_run(run_id, status="passed")


@pytest.fixture
def client(tmp_path: Path):
    owner = Controller(tmp_path, execute=fake_execution, cleanup=lambda _: None)
    with TestClient(
        create_app(tmp_path, controller=owner), base_url="http://127.0.0.1:8123"
    ) as http:
        yield http


def test_local_boundary_requires_origin_token_and_valid_host(client: TestClient) -> None:
    assert client.post("/api/v1/runs", json={}).status_code == 403
    token = client.get("/api/v1/session").json()["token"]
    headers = {"X-Benchmaxxing-Token": token}
    assert (
        client.post(
            "/api/v1/runs",
            json={},
            headers={
                **headers,
                "Origin": "https://attacker.invalid",
            },
        ).status_code
        == 403
    )
    assert client.get("/api/v1/session", headers={"Origin": "null"}).status_code == 403
    assert client.get("/api/v1/session", headers={"Host": "attacker.invalid"}).status_code == 400
    assert (
        client.get("/api/v1/session", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    )


def test_runs_events_artifacts_and_export_agree(client: TestClient) -> None:
    token = client.get("/api/v1/session").json()["token"]
    response = client.post("/api/v1/runs", json={}, headers={"X-Benchmaxxing-Token": token})
    assert response.status_code == 201
    run_id = response.json()["id"]
    for _ in range(100):
        detail = client.get(f"/api/v1/runs/{run_id}").json()
        if detail["run"]["status"] == "passed":
            break
        time.sleep(0.01)
    assert detail["run"]["status"] == "passed"
    assert client.get("/api/v1/runs").json()[0]["id"] == run_id
    trace = client.get(f"/api/v1/runs/{run_id}/events").text
    assert "event: run_event" in trace
    assert "event: complete" in trace
    sequences = [int(line[4:]) for line in trace.splitlines() if line.startswith("id: ")]
    resumed = client.get(
        f"/api/v1/runs/{run_id}/events",
        headers={"Last-Event-ID": str(sequences[-1])},
    ).text
    assert "event: run_event" not in resumed
    artifact = detail["artifacts"][0]
    content = client.get(f"/api/v1/runs/{run_id}/artifacts/{artifact['id']}")
    assert content.content == b"print('hello')"
    assert content.headers["X-Content-Type-Options"] == "nosniff"
    assert client.get(f"/api/v1/runs/{run_id}/export").content.startswith(b"PK")
    assert client.get(f"/api/v1/runs/{run_id}/artifacts/not-an-artifact").status_code == 404


def test_invalid_spec_is_not_persisted(client: TestClient) -> None:
    token = client.get("/api/v1/session").json()["token"]
    response = client.post(
        "/api/v1/runs",
        headers={"X-Benchmaxxing-Token": token},
        json={
            "participant": "openai",
            "budgets": {"wall_seconds": -1},
        },
    )
    assert response.status_code == 422
    assert client.get("/api/v1/runs").json() == []


def test_invalid_credential_url_is_not_echoed_or_saved(client: TestClient) -> None:
    token = client.get("/api/v1/session").json()["token"]
    response = client.post(
        "/api/v1/runs",
        headers={"X-Benchmaxxing-Token": token},
        json={
            "participant": "openai",
            "inference": {"model": "test", "base_url": "http://user:secret-value@127.0.0.1/v1"},
        },
    )
    assert response.status_code == 422
    assert "secret-value" not in response.text
    assert client.get("/api/v1/runs").json() == []
