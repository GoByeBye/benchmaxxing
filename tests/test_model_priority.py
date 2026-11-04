from __future__ import annotations

import io
import json
import time
import zipfile
from pathlib import Path
from typing import Any, Literal

import httpx
import pytest
from pydantic import ValidationError

from benchmaxxing.contracts import Generation, InferenceProfile, RunSpec
from benchmaxxing.endpoints import EndpointInput, EndpointStore
from benchmaxxing.model import OpenAIAdapter
from benchmaxxing.storage import Store


@pytest.mark.parametrize("priority", [None, 0, -10, 10, -2147483648, 2147483647])
@pytest.mark.parametrize("api_style", ["chat_completions", "responses"])
@pytest.mark.parametrize("streaming", [False, True])
def test_priority_is_optional_on_each_request_path(
    priority: int | None,
    api_style: Literal["chat_completions", "responses"],
    streaming: bool,
) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if api_style == "responses":
            return httpx.Response(
                200,
                json={
                    "model": "test",
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "done"}],
                        }
                    ],
                },
            )
        if streaming:
            chunk = {
                "model": "test",
                "choices": [{"index": 0, "delta": {"content": "done"}, "finish_reason": "stop"}],
            }
            return httpx.Response(
                200,
                headers={"Content-Type": "text/event-stream"},
                content=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode(),
            )
        return httpx.Response(
            200,
            json={
                "model": "test",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "done"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    adapter = OpenAIAdapter(
        InferenceProfile(model="test", api_style=api_style, vllm_priority=priority),
        Generation(),
        transport=httpx.MockTransport(handle),
    )
    deltas: list[dict[str, Any]] = []
    result = adapter.complete(
        [], [], deadline=time.monotonic() + 3, on_delta=deltas.append if streaming else None
    )
    assert result.content == "done"
    assert len(requests) == 1
    payload = json.loads(requests[0].content)
    if priority is None:
        assert "priority" not in payload
    else:
        assert payload["priority"] == priority
        assert type(payload["priority"]) is int
    assert payload["stream"] is (streaming and api_style == "chat_completions")


@pytest.mark.parametrize("priority", [True, False, 1.0, -0.5, "1", "", 2147483648, -2147483649])
def test_priority_rejects_nonintegers_and_out_of_range_values(priority: Any) -> None:
    with pytest.raises(ValidationError):
        InferenceProfile(model="test", vllm_priority=priority)
    with pytest.raises(ValidationError):
        EndpointInput(name="Local", vllm_priority=priority)


@pytest.mark.parametrize("priority", [None, 0, -5])
def test_priority_survives_saved_endpoints_and_run_exports(
    tmp_path: Path, priority: int | None
) -> None:
    endpoints = EndpointStore(tmp_path)
    saved = endpoints.save(EndpointInput(name="Local", model_ids=["test"], vllm_priority=priority))
    reloaded = EndpointStore(tmp_path).get(saved.id)
    assert reloaded.vllm_priority == priority
    store = Store(tmp_path)
    run = store.create_run(
        RunSpec(
            participant="openai",
            inference=InferenceProfile(model="test", vllm_priority=reloaded.vllm_priority),
        )
    )
    assert store.get_run(run.id).spec.inference is not None
    store.update_run(run.id, status="cancelled", event_kind="cancelled")
    with zipfile.ZipFile(io.BytesIO(store.export_run(run.id))) as archive:
        exported = json.loads(archive.read("run.json"))
    assert exported["spec"]["inference"]["vllm_priority"] == priority
