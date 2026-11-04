from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from benchmaxxing.api import create_app
from benchmaxxing.controller import Controller
from benchmaxxing.endpoints import Endpoint, EndpointInput, EndpointStore, discover_models


def test_endpoint_routes_persist_and_require_token(tmp_path: Path) -> None:
    owner = Controller(tmp_path, cleanup=lambda _: None)
    with TestClient(
        create_app(tmp_path, controller=owner), base_url="http://127.0.0.1:8123"
    ) as client:
        headers = {"X-Benchmaxxing-Token": client.get("/api/v1/session").json()["token"]}
        payload = {"name": "Local", "model_ids": ["one", "two"], "api_style": "responses"}
        assert client.post("/api/v1/endpoints", json=payload).status_code == 403
        added = client.post("/api/v1/endpoints", json=payload, headers=headers)
        assert added.status_code == 201
        endpoint = added.json()
        other = client.post("/api/v1/endpoints", json={"name": "Other"}, headers=headers)
        assert other.status_code == 201
        assert len(EndpointStore(tmp_path).list()) == 2
        assert client.post(f"/api/v1/endpoints/{endpoint['id']}/models").status_code == 403
        updated = client.put(
            f"/api/v1/endpoints/{endpoint['id']}",
            json={**payload, "name": "Renamed"},
            headers=headers,
        )
        assert updated.json()["name"] == "Renamed"
        assert (
            client.delete(f"/api/v1/endpoints/{endpoint['id']}", headers=headers).status_code == 204
        )
        assert (
            client.put(
                f"/api/v1/endpoints/{endpoint['id']}", json=payload, headers=headers
            ).status_code
            == 404
        )


@pytest.mark.parametrize(
    "url", ["https://example.com/v1", "http://169.254.169.254", "http://local.test"]
)
def test_offline_endpoint_rejects_non_loopback(url: str) -> None:
    with pytest.raises(ValidationError):
        EndpointInput(name="Unsafe", base_url=url)


def test_endpoint_rejects_raw_secrets() -> None:
    with pytest.raises(ValidationError):
        EndpointInput.model_validate({"name": "Secret", "api_key": "secret"})
    with pytest.raises(ValidationError):
        EndpointInput(name="Secret", base_url="http://user:secret@localhost/v1")


def test_discovery_transport_and_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    real_client = httpx.Client
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": [{"id": "z"}, {"id": "a"}, {"id": "a"}]})

    def client(**kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        return real_client(**kwargs, transport=httpx.MockTransport(respond))

    monkeypatch.setattr("benchmaxxing.endpoints.httpx.Client", client)
    monkeypatch.setenv("TEST_ENDPOINT_KEY", "test-secret")
    endpoint = Endpoint(
        id="test",
        name="Local",
        base_url="http://localhost:8080/v1",
        api_key_env="TEST_ENDPOINT_KEY",
    )
    assert discover_models(endpoint).model_ids == ["a", "z"]
    assert str(requests[0].url) == "http://127.0.0.1:8080/v1/models"
    assert requests[0].headers["authorization"] == "Bearer test-secret"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(302, headers={"location": "http://evil"}),
        httpx.Response(200, json={"data": [{"id": 2}]}),
        httpx.Response(200, content=b"x" * 1048577),
    ],
)
def test_discovery_rejects_redirect_invalid_or_large_response(monkeypatch, response) -> None:
    real_client = httpx.Client
    monkeypatch.setattr(
        "benchmaxxing.endpoints.httpx.Client",
        lambda **kwargs: real_client(**kwargs, transport=httpx.MockTransport(lambda _: response)),
    )
    with pytest.raises(ValueError):
        discover_models(Endpoint(id="test", name="Local"))
