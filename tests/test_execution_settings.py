from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from benchmaxxing.api import create_app
from benchmaxxing.controller import Controller


def test_execution_settings_api_requires_token_and_persists(tmp_path: Path) -> None:
    owner = Controller(tmp_path, cleanup=lambda _: None)
    with TestClient(
        create_app(tmp_path, controller=owner), base_url="http://127.0.0.1:8123"
    ) as http:
        assert http.get("/api/v1/execution/settings").json() == {"max_parallel_runs": 1}
        assert (
            http.put("/api/v1/execution/settings", json={"max_parallel_runs": 2}).status_code == 403
        )
        token = http.get("/api/v1/session").json()["token"]
        response = http.put(
            "/api/v1/execution/settings",
            headers={"X-Benchmaxxing-Token": token},
            json={"max_parallel_runs": 2},
        )
        assert response.status_code == 200
        assert response.json() == {"max_parallel_runs": 2}
        assert http.get("/api/v1/execution/settings").json() == response.json()
    replacement = Controller(tmp_path, cleanup=lambda _: None)
    with TestClient(
        create_app(tmp_path, controller=replacement), base_url="http://127.0.0.1:8123"
    ) as http:
        assert http.get("/api/v1/execution/settings").json() == {"max_parallel_runs": 2}


@pytest.mark.parametrize("value", [0, 9, True, 2.5, "2", None])
def test_execution_settings_api_rejects_invalid_limits(tmp_path: Path, value: object) -> None:
    owner = Controller(tmp_path, cleanup=lambda _: None)
    with TestClient(
        create_app(tmp_path, controller=owner), base_url="http://127.0.0.1:8123"
    ) as http:
        token = http.get("/api/v1/session").json()["token"]
        response = http.put(
            "/api/v1/execution/settings",
            headers={"X-Benchmaxxing-Token": token},
            json={"max_parallel_runs": value},
        )
        assert response.status_code == 422
        assert owner.settings.max_parallel_runs == 1
