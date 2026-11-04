from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest

from benchmaxxing.contracts import Generation, InferenceProfile
from benchmaxxing.model import (
    ModelCancelled,
    ModelDeadlineExceeded,
    ModelOutputLimitExceeded,
    ModelProtocolError,
    OpenAIAdapter,
    TextRedactor,
)

DONE = b"data: [DONE]\n\n"


def frame(delta: dict[str, Any], finish: str | None = None) -> bytes:
    return event(
        {
            "model": "test-model",
            "choices": [
                {"index": 0, "delta": delta, "finish_reason": finish},
            ],
        }
    )


def event(payload: dict[str, Any]) -> bytes:
    return b"data: " + json.dumps(payload, ensure_ascii=False).encode() + b"\n\n"


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes], *, stall: bool = False) -> None:
        self.chunks = chunks
        self.stall = stall
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.stall:
            await asyncio.sleep(60)

    async def aclose(self) -> None:
        self.closed = True


def adapter_for(
    stream: Chunks, *, limit: int = 65536, key_env: str | None = None, tokens: int = 2048
) -> tuple[OpenAIAdapter, list[httpx.Request]]:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=stream)

    return OpenAIAdapter(
        InferenceProfile(model="requested", api_key_env=key_env),
        Generation(max_tokens=tokens),
        max_response_bytes=limit,
        transport=httpx.MockTransport(handler),
    ), requests


@contextmanager
def local_server(handler: type[BaseHTTPRequestHandler]) -> Iterator[ThreadingHTTPServer]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def test_real_http_delivers_deltas_before_completion_and_assembles_tool_arguments() -> None:
    content_seen, tool_seen = threading.Event(), threading.Event()
    received, delivered = [], []
    gates = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.end_headers()
            first = frame({"role": "assistant", "content": "hé"}).replace(b"\n", b"\r\n")
            # Split inside a UTF-8 character, then inside the CRLF delimiter.
            position = first.index("é".encode()) + 1
            for chunk in (first[:position], first[position:-1], first[-1:]):
                self.wfile.write(chunk)
                self.wfile.flush()
            gates.append(content_seen.wait(2))
            self.wfile.write(
                frame(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": "write_", "arguments": '{"pa'},
                            }
                        ]
                    }
                )
            )
            self.wfile.flush()
            gates.append(tool_seen.wait(2))
            self.wfile.write(
                frame(
                    {
                        "tool_calls": [
                            {"index": 0, "function": {"name": "file", "arguments": 'th":"x"}'}}
                        ]
                    }
                )
            )
            self.wfile.write(frame({}, "tool_calls"))
            self.wfile.write(
                event(
                    {
                        "choices": [],
                        "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
                    }
                )
            )
            self.wfile.write(DONE)
            self.wfile.flush()

        def log_message(self, format: str, *args: object) -> None:
            pass

    def on_delta(delta: dict[str, Any]) -> None:
        delivered.append(delta)
        if delta["type"] == "content":
            content_seen.set()
        if delta["type"] == "tool_call_delta" and delta["field"] == "arguments":
            tool_seen.set()

    with local_server(Handler) as server:
        adapter = OpenAIAdapter(
            InferenceProfile(
                model="requested", base_url=f"http://127.0.0.1:{server.server_port}/v1"
            ),
            Generation(),
        )
        result = adapter.complete([], [], deadline=time.monotonic() + 6, on_delta=on_delta)
    assert gates == [True, True], "Short writes were buffered until completion"
    assert len(received) == 1
    assert received[0]["stream"] is True
    assert received[0]["stream_options"] == {"include_usage": True}
    assert result.content == "hé"
    assert result.tool_calls[0].name == "write_file"
    assert result.tool_calls[0].arguments == '{"path":"x"}'
    assert result.tool_calls[0].id == "call-1"
    assert result.usage == {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12}
    assert delivered[0] == {"type": "mode", "mode": "sse"}
    assert delivered[-1] == {"type": "finish", "finish_reason": "tool_calls"}


def test_split_secrets_are_redacted_per_text_and_tool_channel(monkeypatch) -> None:
    monkeypatch.setenv("STREAM_KEY", "secret-abc")
    frames = [
        frame({"content": "before sec", "refusal": "se", "reasoning_content": "sec"}),
        frame({"content": "ret-a", "refusal": "cret-", "reasoning_content": "ret-abc"}),
        frame({"content": "bc after", "refusal": "abc"}),
        frame(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call",
                        "type": "function",
                        "function": {"name": "execute", "arguments": '{"command":"secret-'},
                    }
                ]
            }
        ),
        frame({"tool_calls": [{"index": 0, "function": {"arguments": 'abc"}'}}]}),
        frame({}, "tool_calls"),
        DONE,
    ]
    stream = Chunks([byte for payload in frames for byte in (payload[:3], payload[3:])])
    adapter, requests = adapter_for(stream, key_env="STREAM_KEY")
    delivered = []
    result = adapter.complete([], [], deadline=time.monotonic() + 3, on_delta=delivered.append)
    assert "secret-abc" not in json.dumps(delivered)
    assert (
        "".join(d["text"] for d in delivered if d["type"] == "content") == "before [REDACTED] after"
    )
    assert "".join(d["text"] for d in delivered if d["type"] == "refusal") == "[REDACTED]"
    assert "".join(d["text"] for d in delivered if d["type"] == "reasoning") == "[REDACTED]"
    assert result.content == "before [REDACTED] after"
    assert result.refusal == "[REDACTED]"
    assert result.tool_calls[0].arguments == '{"command":"[REDACTED]"}'
    assert len(requests) == 1 and stream.closed


@pytest.mark.parametrize(
    ("secret", "chunks", "expected"),
    [
        ("aaa", ["aa", "aa", "a", "a"], "[REDACTED][REDACTED]"),
        ("abc", ["a", "b", "x", "ab", "c", "ab"], "abx[REDACTED]ab"),
        ("", ["a", "b"], "ab"),
    ],
)
def test_text_redactor_overlap_and_channel_end(secret, chunks, expected) -> None:
    redactor = TextRedactor(secret)
    output = "".join(redactor.feed(chunk) for chunk in chunks) + redactor.finish()
    assert output == expected
    assert redactor.finish() == ""


@pytest.mark.parametrize("reason", ["cancel", "deadline"])
@pytest.mark.parametrize("has_partial", [False, True])
def test_stalled_stream_closes_on_cancellation_or_total_deadline(reason, has_partial) -> None:
    stream = Chunks([frame({"content": "partial"})] if has_partial else [], stall=True)
    adapter, requests = adapter_for(stream)
    cancelled = threading.Event()
    delivered = []
    timer = threading.Timer(0.1, cancelled.set)
    if reason == "cancel":
        timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(ModelCancelled if reason == "cancel" else ModelDeadlineExceeded):
            adapter.complete(
                [],
                [],
                deadline=started + (3 if reason == "cancel" else 0.1),
                cancel_event=cancelled,
                on_delta=delivered.append,
            )
    finally:
        timer.cancel()
    assert time.monotonic() - started < 1
    assert any(d["type"] == "content" for d in delivered) is has_partial
    assert not any(d["type"] == "finish" for d in delivered)
    assert stream.closed and len(requests) == 1


@pytest.mark.parametrize(
    "chunks",
    [
        [frame({"content": "partial"})],
        [frame({"content": "complete"}, "stop")],
        [frame({"content": "partial"}), DONE],
        [b"data: {bad json}\n\n", DONE],
        [b"data: \xff\n\n", DONE],
        [event({"choices": []}), DONE],
        [event({"choices": [{"index": 0, "delta": {}}, {"index": 1, "delta": {}}]}), DONE],
        [event({"choices": [{"index": 1, "delta": {}}]}), DONE],
        [
            frame(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "x",
                            "type": "function",
                            "function": {"name": "submit", "arguments": {}},
                        }
                    ]
                }
            ),
            DONE,
        ],
        [frame({"content": "a"}, "stop"), frame({"content": "b"}), DONE],
        [
            frame(
                {
                    "tool_calls": [
                        {"index": 0, "id": "x", "type": "function", "function": {"arguments": "{}"}}
                    ]
                },
                "tool_calls",
            ),
            DONE,
        ],
        [event({"error": {"message": "PRIVATE PROVIDER BODY"}}), DONE],
    ],
)
def test_malformed_or_truncated_stream_is_not_a_completed_response(chunks) -> None:
    stream = Chunks(chunks)
    adapter, requests = adapter_for(stream)
    delivered = []
    with pytest.raises(ModelProtocolError) as error:
        adapter.complete([], [], deadline=time.monotonic() + 2, on_delta=delivered.append)
    assert "PRIVATE" not in str(error.value)
    assert not any(d["type"] == "finish" for d in delivered)
    assert len(requests) == 1 and stream.closed


def test_stream_overhead_has_separate_bound_from_assembled_payload() -> None:
    stream = Chunks(
        [b"".join([frame({"content": "x"}) for _ in range(20)]) + frame({}, "stop") + DONE]
    )
    adapter, _ = adapter_for(stream, limit=64)
    result = adapter.complete([], [], deadline=time.monotonic() + 2, on_delta=lambda _: None)
    assert result.content == "x" * 20


@pytest.mark.parametrize(
    "chunks,limit",
    [
        ([frame({"content": "x" * 1000}), frame({}, "stop"), DONE], 64),
        ([b":" + b"x" * (64 * 32 + 262144)], 64),
    ],
)
def test_stream_payload_and_wire_budgets(chunks, limit) -> None:
    stream = Chunks(chunks)
    adapter, requests = adapter_for(stream, limit=limit)
    with pytest.raises(ModelOutputLimitExceeded):
        adapter.complete([], [], deadline=time.monotonic() + 2, on_delta=lambda _: None)
    assert len(requests) == 1 and stream.closed


@pytest.mark.parametrize("mode", ["sse", "json"])
def test_reported_usage_does_not_reinterpret_the_requested_token_budget(mode) -> None:
    if mode == "sse":
        chunks = [
            frame({"content": "x"}, "stop"),
            event({"choices": [], "usage": {"completion_tokens": 3}}),
            DONE,
        ]
    else:
        chunks = [
            json.dumps(
                {
                    "choices": [
                        {"message": {"role": "assistant", "content": "x"}, "finish_reason": "stop"}
                    ],
                    "usage": {"completion_tokens": 3},
                }
            ).encode()
        ]
    adapter, requests = adapter_for(Chunks(chunks), tokens=2)
    delivered = []
    result = adapter.complete([], [], deadline=time.monotonic() + 2, on_delta=delivered.append)
    assert result.content == "x"
    assert result.usage == {"prompt_tokens": None, "completion_tokens": 3, "total_tokens": None}
    assert json.loads(requests[0].content)["max_tokens"] == 2
    assert {"type": "usage", "usage": result.usage} in delivered


def test_whole_json_fallback_is_explicit_without_a_second_request(monkeypatch) -> None:
    monkeypatch.setenv("STREAM_KEY", "private-value")
    payload = {
        "model": "test-model",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "private-value",
                    "reasoning_content": "returned hint",
                    "tool_calls": [
                        {
                            "id": "call",
                            "type": "function",
                            "function": {"name": "submit", "arguments": "{}"},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"completion_tokens": 4},
    }
    encoded = json.dumps(payload).encode()
    stream = Chunks([encoded[:1], encoded[1:10], encoded[10:]])
    # This also exercises servers incorrectly retaining the SSE response content type.
    adapter, requests = adapter_for(stream, key_env="STREAM_KEY")
    delivered = []
    result = adapter.complete([], [], deadline=time.monotonic() + 2, on_delta=delivered.append)
    assert delivered[0] == {"type": "mode", "mode": "json"}
    assert result.content == "[REDACTED]" and result.tool_calls[0].name == "submit"
    assert {"type": "reasoning", "text": "returned hint"} in delivered
    assert "private-value" not in json.dumps(delivered)
    assert len(requests) == 1


def test_multiple_interleaved_calls_keep_their_indices() -> None:
    stream = Chunks(
        [
            frame(
                {
                    "tool_calls": [
                        {
                            "index": 1,
                            "id": "b",
                            "type": "function",
                            "function": {"name": "submit", "arguments": "{"},
                        },
                        {
                            "index": 0,
                            "id": "a",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": '{"path":'},
                        },
                    ]
                }
            ),
            frame(
                {
                    "tool_calls": [
                        {"index": 0, "function": {"arguments": '"x"}'}},
                        {"index": 1, "function": {"arguments": "}"}},
                    ]
                }
            ),
            frame({}, "tool_calls"),
            DONE,
        ]
    )
    adapter, _ = adapter_for(stream)
    result = adapter.complete([], [], deadline=time.monotonic() + 2, on_delta=lambda _: None)
    assert [(call.id, call.name, call.arguments) for call in result.tool_calls] == [
        ("a", "read_file", '{"path":"x"}'),
        ("b", "submit", "{}"),
    ]


def test_comments_multiline_data_and_bytewise_utf8() -> None:
    raw = (
        b": keepalive\r\n\r\n"
        + b'data: {"choices":\r\ndata: '
        + '[{"index":0,"delta":{"content":"🙂"},"finish_reason":"stop"}]}'.encode()
        + b"\r\n\r\n"
        + DONE
    )
    stream = Chunks([bytes([byte]) for byte in raw])
    adapter, _ = adapter_for(stream)
    result = adapter.complete([], [], deadline=time.monotonic() + 2, on_delta=lambda _: None)
    assert result.content == "🙂"
