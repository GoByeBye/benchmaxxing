from __future__ import annotations

import json
import time
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from benchmaxxing.contracts import Generation, InferenceProfile
from benchmaxxing.model import (
    ModelOutputLimitExceeded,
    ModelProtocolError,
    OpenAIAdapter,
    UnsupportedModel,
    parse_responses_response,
)


def response(**overrides: Any) -> dict[str, Any]:
    return {
        "status": "completed",
        "model": "test",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "hello"}],
            }
        ],
        **overrides,
    }


@pytest.mark.parametrize("reasoning", [False, True])
def test_responses_round_trip_and_buffered_events(
    monkeypatch: pytest.MonkeyPatch, reasoning: bool
) -> None:
    monkeypatch.setenv("RESPONSES_TEST_KEY", "secret-test-key")
    requests: list[dict[str, Any]] = []
    outputs = [
        response(
            output=[
                {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque", "summary": []},
                {
                    "type": "function_call",
                    "id": "fc_1",
                    "call_id": "call_1",
                    "name": "submit",
                    "arguments": "{}",
                    "status": "completed",
                },
            ]
        ),
        response(
            output=[
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "secret-test-key done"}],
                }
            ],
            usage={"input_tokens": 12, "output_tokens": 3, "total_tokens": 15},
        ),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://127.0.0.1:8080/v1/responses"
        assert request.headers["authorization"] == "Bearer secret-test-key"
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=outputs.pop(0))

    adapter = OpenAIAdapter(
        InferenceProfile(
            model="test",
            api_style="responses",
            api_key_env="RESPONSES_TEST_KEY",
            responses_reasoning=reasoning,
        ),
        Generation(seed=42),
        transport=httpx.MockTransport(handler),
    )
    messages = [{"role": "system", "content": "instructions"}, {"role": "user", "content": "work"}]
    tools = [
        {
            "type": "function",
            "function": {"name": "submit", "parameters": {"type": "object", "properties": {}}},
        }
    ]
    events: list[dict[str, Any]] = []
    first = adapter.complete(messages, tools, deadline=time.monotonic() + 3, on_delta=events.append)
    assert first.tool_calls[0].id == "call_1"
    assert first.finish_reason == "tool_calls"
    second = adapter.complete(
        [
            *messages,
            first.message_dict(),
            {"role": "tool", "tool_call_id": "call_1", "content": "accepted"},
        ],
        tools,
        deadline=time.monotonic() + 3,
        on_delta=events.append,
    )
    assert second.content == "[REDACTED] done"
    assert second.usage == {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15}
    assert requests[0]["stream"] is False
    assert requests[0]["store"] is False
    assert requests[0].get("include") == (["reasoning.encrypted_content"] if reasoning else None)
    assert requests[0]["max_output_tokens"] == 16384
    assert "seed" not in requests[0] and "messages" not in requests[0]
    assert requests[0]["tools"][0]["name"] == "submit"
    assert requests[0]["tools"][0]["strict"] is False
    assert requests[1]["input"][2]["type"] == "reasoning"
    assert requests[1]["input"][3]["call_id"] == "call_1"
    assert requests[1]["input"][4] == {
        "type": "function_call_output",
        "call_id": "call_1",
        "output": "accepted",
    }
    assert events[0] == {"type": "mode", "mode": "json"}
    assert events[-1] == {"type": "finish", "finish_reason": "stop"}
    assert "secret-test-key" not in json.dumps(events)


@pytest.mark.parametrize(
    "overrides",
    [
        {"error": {"message": "secret"}},
        {"status": "failed"},
        {"status": "in_progress"},
        {"output": None},
        {"output": [None]},
        {"output": [{"type": "message", "role": "user", "content": []}]},
        {
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": 7}],
                }
            ]
        },
        {"output": [{"type": "function_call", "call_id": "", "name": "f", "arguments": "{}"}]},
        {"output": [{"type": "function_call", "call_id": "c", "name": "f", "arguments": {}}]},
        {"usage": {"input_tokens": True}},
        {"usage": {"output_tokens": -1}},
        {"status": "incomplete", "incomplete_details": {"reason": "unknown"}},
    ],
)
def test_invalid_responses_are_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(ModelProtocolError):
        parse_responses_response(response(**overrides))


def test_responses_refusal_multiple_text_and_duplicate_calls() -> None:
    result = parse_responses_response(
        response(
            output=[
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": "a"},
                        {"type": "output_text", "text": "b"},
                        {"type": "refusal", "refusal": "declined"},
                    ],
                }
            ]
        )
    )
    assert result.content == "ab" and result.refusal == "declined"
    call = {"type": "function_call", "call_id": "c", "name": "f", "arguments": "{}"}
    with pytest.raises(ModelProtocolError):
        parse_responses_response(response(output=[call, call]))
    with pytest.raises(UnsupportedModel):
        parse_responses_response(response(output=[{"type": "web_search_call"}]))
    with pytest.raises(ModelOutputLimitExceeded):
        parse_responses_response(
            response(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
        )


def test_responses_content_filter_preserves_text_and_drops_incomplete_tools() -> None:
    filtered = parse_responses_response(
        response(
            status="incomplete",
            incomplete_details={"reason": "content_filter"},
            output=[
                {
                    "type": "message",
                    "role": "assistant",
                    "status": "incomplete",
                    "content": [{"type": "output_text", "text": "Partial text"}],
                },
                {
                    "type": "function_call",
                    "status": "incomplete",
                    "call_id": "call_1",
                    "name": "submit",
                    "arguments": "{",
                },
            ],
        )
    )
    assert filtered.content == "Partial text"
    assert filtered.tool_calls == []
    assert filtered.refusal is None
    assert filtered.finish_reason == "content_filter"
    assert (
        parse_responses_response(
            response(
                status="incomplete", incomplete_details={"reason": "content_filter"}, output=[]
            )
        ).finish_reason
        == "content_filter"
    )


def test_responses_default_and_invalid_style() -> None:
    assert InferenceProfile(model="test").api_style == "chat_completions"
    with pytest.raises(ValidationError):
        InferenceProfile(model="test", api_style="invalid")  # type: ignore[arg-type]


def test_responses_json_size_bound_without_delta() -> None:
    adapter = OpenAIAdapter(
        InferenceProfile(model="test", api_style="responses"),
        Generation(),
        max_response_bytes=32,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response())),
    )
    with pytest.raises(ModelOutputLimitExceeded):
        adapter.complete([], [], deadline=time.monotonic() + 3)
