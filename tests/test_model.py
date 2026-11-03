from __future__ import annotations

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from benchmaxxing.contracts import Generation, InferenceProfile
from benchmaxxing.model import (
    ModelCancelled,
    ModelDeadlineExceeded,
    ModelError,
    ModelOutputLimitExceeded,
    ModelProtocolError,
    OpenAIAdapter,
    UnsupportedModel,
    parse_response,
)


def response_payload(**message: Any) -> dict[str, Any]:
    return {
        "model": "local-test",
        "choices": [
            {
                "message": {"role": "assistant", "content": None, **message},
                "finish_reason": "tool_calls",
            }
        ],
    }


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/v1",
        "http://192.168.1.1/v1",
        "http://127.1/v1",
        "http://localhost.evil.test/v1",
        "http://2130706433/v1",
        "http://[::ffff:192.168.1.1]/v1",
        "http://localhost./v1",
        "ftp://127.0.0.1/v1",
        "http://user:password@127.0.0.1/v1",
        "http://127.0.0.1/v1?key=secret",
        "http://127.0.0.1/v1#key",
        "http://127.0.0.1:bad/v1",
    ],
)
def test_offline_profiles_reject_nonlocal_or_credential_urls(url: str) -> None:
    with pytest.raises(ValidationError):
        InferenceProfile(model="test", base_url=url)


def test_localhost_is_canonicalized_before_transport() -> None:
    assert (
        InferenceProfile(model="test", base_url="http://localhost:9000/v1/").base_url
        == "http://127.0.0.1:9000/v1"
    )
    assert (
        InferenceProfile(model="test", base_url="http://[::1]:9000/v1").base_url
        == "http://[::1]:9000/v1"
    )
    assert (
        InferenceProfile(model="test", mode="api", base_url="https://example.com/v1").base_url
        == "https://example.com/v1"
    )


def test_key_setting_is_variable_name() -> None:
    with pytest.raises(ValidationError):
        InferenceProfile(model="test", api_key_env="sk-a-secret")


def test_request_and_response_with_real_local_http_server(monkeypatch: pytest.MonkeyPatch) -> None:
    received: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            data = json.dumps(
                response_payload(
                    tool_calls=[
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "submit", "arguments": "{}"},
                        }
                    ]
                )
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.delenv("NO_PROXY", raising=False)
    try:
        adapter = OpenAIAdapter(
            InferenceProfile(
                model="configured-model", base_url=f"http://127.0.0.1:{server.server_port}/v1"
            ),
            Generation(seed=7),
        )
        result = adapter.complete(
            [{"role": "user", "content": "test"}], [], deadline=time.monotonic() + 3
        )
        assert result.model == "local-test"
        assert result.usage is None
        assert result.tool_calls[0].name == "submit"
        assert received[0]["model"] == "configured-model"
        assert received[0]["seed"] == 7
        assert received[0]["stream"] is False
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_redirect_never_reaches_destination() -> None:
    urls = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return httpx.Response(307, headers={"Location": "https://example.com/secret"})

    adapter = OpenAIAdapter(
        InferenceProfile(model="test"), Generation(), transport=httpx.MockTransport(handler)
    )
    with pytest.raises(ModelProtocolError, match="redirects"):
        adapter.complete([], [], deadline=time.monotonic() + 2)
    assert urls == ["http://127.0.0.1:8080/v1/chat/completions"]


def test_credentials_are_headers_only_and_redacted_in_responses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MODEL_KEY", "credential-for-test")
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=response_payload(content="credential-for-test"))

    adapter = OpenAIAdapter(
        InferenceProfile(model="test", api_key_env="MODEL_KEY"),
        Generation(),
        transport=httpx.MockTransport(handler),
    )
    result = adapter.complete([], [], deadline=time.monotonic() + 2)
    assert requests[0].headers["Authorization"] == "Bearer credential-for-test"
    assert b"credential-for-test" not in requests[0].content
    assert result.content == "[REDACTED]"


def test_error_response_body_never_enters_safe_error() -> None:
    adapter = OpenAIAdapter(
        InferenceProfile(model="test"),
        Generation(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(401, text="SECRET server error")
        ),
    )
    with pytest.raises(ModelError) as error:
        adapter.complete([], [], deadline=time.monotonic() + 2)
    assert str(error.value) == "Inference endpoint returned HTTP 401"


@pytest.mark.parametrize("body", [b"not-json", b"[]", b'{"choices":[]}'])
def test_invalid_payload_is_protocol_error(body: bytes) -> None:
    adapter = OpenAIAdapter(
        InferenceProfile(model="test"),
        Generation(),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body)),
    )
    with pytest.raises(ModelProtocolError):
        adapter.complete([], [], deadline=time.monotonic() + 2)


def test_response_body_is_bounded() -> None:
    adapter = OpenAIAdapter(
        InferenceProfile(model="test"),
        Generation(),
        max_response_bytes=100,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 101)),
    )
    with pytest.raises(ModelOutputLimitExceeded):
        adapter.complete([], [], deadline=time.monotonic() + 2)


def test_total_deadline_cancels_pending_request() -> None:
    closed = threading.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        try:
            await asyncio.sleep(5)
            return httpx.Response(200, json=response_payload(content="late"))
        finally:
            closed.set()

    adapter = OpenAIAdapter(
        InferenceProfile(model="test"), Generation(), transport=httpx.MockTransport(handler)
    )
    start = time.monotonic()
    with pytest.raises(ModelDeadlineExceeded):
        adapter.complete([], [], deadline=start + 0.1)
    assert time.monotonic() - start < 1
    assert closed.is_set()


def test_cancellation_closes_pending_request() -> None:
    cancelled = threading.Event()
    closed = threading.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        try:
            await asyncio.sleep(5)
            return httpx.Response(200, json=response_payload(content="late"))
        finally:
            closed.set()

    adapter = OpenAIAdapter(
        InferenceProfile(model="test"), Generation(), transport=httpx.MockTransport(handler)
    )
    timer = threading.Timer(0.1, cancelled.set)
    timer.start()
    try:
        with pytest.raises(ModelCancelled):
            adapter.complete([], [], deadline=time.monotonic() + 5, cancel_event=cancelled)
        assert closed.is_set()
    finally:
        timer.cancel()


def test_legacy_function_call_is_unsupported() -> None:
    with pytest.raises(UnsupportedModel):
        parse_response(response_payload(function_call={"name": "submit", "arguments": "{}"}))


def test_zero_and_unknown_usage_are_distinct() -> None:
    payload = response_payload(content="done")
    payload["usage"] = {"prompt_tokens": 0, "completion_tokens": 0}
    assert parse_response(payload).usage == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": None,
    }


def test_duplicate_tool_identifiers_are_rejected() -> None:
    call = {"id": "same", "type": "function", "function": {"name": "submit", "arguments": "{}"}}
    with pytest.raises(ModelProtocolError):
        parse_response(response_payload(tool_calls=[call, call]))


@pytest.mark.parametrize("tool_calls", [{}, "", 0, False])
def test_falsey_nonlist_tool_calls_are_rejected(tool_calls: Any) -> None:
    with pytest.raises(ModelProtocolError):
        parse_response(response_payload(tool_calls=tool_calls))
