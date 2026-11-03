"""Bounded OpenAI-compatible requests with explicit local/API profiles.

Only HTTP transport is standardized here. A loopback endpoint is not evidence
that the process serving it performs local inference; users must verify that
runtime independently with internet access disabled.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from benchmaxxing.contracts import Generation, InferenceProfile


class ModelError(RuntimeError):
    """A safe-to-display inference error, without response bodies or keys."""


class ProfileError(ModelError):
    pass


class ModelProtocolError(ModelError):
    pass


class UnsupportedModel(ModelError):
    pass


class ModelCancelled(ModelError):
    pass


class ModelDeadlineExceeded(ModelError):
    pass


class ModelOutputLimitExceeded(ModelError):
    pass


def validate_profile(profile: InferenceProfile) -> str:
    """Return a canonical base URL. Offline mode never resolves a hostname."""
    try:
        parts = urlsplit(profile.base_url)
        host = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise ProfileError("The inference base URL is invalid") from exc
    if (
        parts.scheme not in {"http", "https"}
        or not host
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or any(char.isspace() for char in profile.base_url)
        or "\\" in profile.base_url
    ):
        raise ProfileError("Use an HTTP(S) base URL without credentials, query, or fragment")
    if profile.api_key_env and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", profile.api_key_env):
        raise ProfileError("The API key setting must name an environment variable")
    if profile.mode == "offline":
        if host.lower() == "localhost":
            host = "127.0.0.1"
        try:
            address = ipaddress.ip_address(host)
        except ValueError as exc:
            raise ProfileError(
                "Offline inference requires a numeric loopback address or localhost"
            ) from exc
        if not address.is_loopback:
            raise ProfileError("Offline inference requires a loopback address")
        host = str(address)
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc += f":{port}"
    return urlunsplit((parts.scheme, netloc, parts.path.rstrip("/"), "", ""))


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str

    def message_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments},
        }


@dataclass(frozen=True)
class ModelResponse:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    model: str | None = None
    usage: dict[str, int | None] | None = None
    refusal: str | None = None
    finish_reason: str | None = None

    def message_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            result["tool_calls"] = [call.message_dict() for call in self.tool_calls]
        if self.refusal is not None:
            result["refusal"] = self.refusal
        return result


class OpenAIAdapter:
    """An adapter with a total deadline, bounded response, and no retries."""

    def __init__(
        self,
        profile: InferenceProfile,
        generation: Generation,
        *,
        max_response_bytes: int = 1_048_576,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.profile = profile
        self.base_url = validate_profile(profile)
        self.generation = generation
        self.max_response_bytes = max_response_bytes
        self.transport = transport
        self._api_key = os.environ.get(profile.api_key_env, "") if profile.api_key_env else ""
        if profile.api_key_env and not self._api_key:
            raise ProfileError("The configured API key environment variable is unset or empty")

    def redact(self, value: Any) -> Any:
        if isinstance(value, str):
            return value.replace(self._api_key, "[REDACTED]") if self._api_key else value
        if isinstance(value, dict):
            return {key: self.redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        return value

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        deadline: float,
        cancel_event: threading.Event | None = None,
    ) -> ModelResponse:
        return asyncio.run(self._complete(messages, tools, deadline, cancel_event))

    async def _complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        deadline: float,
        cancel_event: threading.Event | None,
    ) -> ModelResponse:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ModelDeadlineExceeded("Run time budget exhausted during inference")
        if cancel_event is not None and cancel_event.is_set():
            raise ModelCancelled("Inference cancelled")
        request = asyncio.create_task(self._request(messages, tools, remaining))

        async def wait_cancel() -> None:
            while cancel_event is None or not cancel_event.is_set():
                await asyncio.sleep(0.05)

        cancellation = asyncio.create_task(wait_cancel())
        try:
            done, _ = await asyncio.wait(
                {request, cancellation},
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancellation in done:
                raise ModelCancelled("Inference cancelled")
            if request not in done:
                raise ModelDeadlineExceeded("Run time budget exhausted during inference")
            return await request
        finally:
            for pending in (request, cancellation):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(request, cancellation, return_exceptions=True)

    async def _request(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], remaining: float
    ) -> ModelResponse:
        payload: dict[str, Any] = {
            "model": self.profile.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "stream": False,
            "temperature": self.generation.temperature,
            "max_tokens": self.generation.max_tokens,
        }
        if self.generation.seed is not None:
            payload["seed"] = self.generation.seed
        # Do not allow a compressed response to expand before our byte limit.
        headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        timeout = httpx.Timeout(
            connect=min(10, remaining), read=None, write=min(10, remaining), pool=min(10, remaining)
        )
        try:
            async with httpx.AsyncClient(
                trust_env=False,
                follow_redirects=False,
                timeout=timeout,
                transport=self.transport,
            ) as client:
                async with client.stream(
                    "POST", self.base_url + "/chat/completions", json=payload, headers=headers
                ) as response:
                    if 300 <= response.status_code < 400:
                        raise ModelProtocolError("Inference redirects are disabled")
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        raise ModelProtocolError("Inference response must use identity encoding")
                    if response.status_code >= 400:
                        # Bodies can contain credentials or arbitrary provider text.
                        raise ModelError(f"Inference endpoint returned HTTP {response.status_code}")
                    body = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=8192):
                        if len(body) + len(chunk) > self.max_response_bytes:
                            raise ModelOutputLimitExceeded(
                                "Inference response exceeded the output budget"
                            )
                        body.extend(chunk)
        except httpx.TimeoutException as exc:
            raise ModelDeadlineExceeded("Inference transport timed out") from exc
        except httpx.HTTPError as exc:
            raise ModelError("Could not communicate with the inference endpoint") from exc
        try:
            payload_result = json.loads(body)
        except (ValueError, UnicodeError) as exc:
            raise ModelProtocolError("Inference endpoint returned invalid JSON") from exc
        return parse_response(self.redact(payload_result))


def parse_response(payload: Any) -> ModelResponse:
    """Validate only the compatible chat subset we actually use."""
    if not isinstance(payload, dict):
        raise ModelProtocolError("Inference response must be an object")
    choices = payload.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise ModelProtocolError("Inference response must contain one choice")
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict) or message.get("role", "assistant") != "assistant":
        raise ModelProtocolError("Inference choice must contain an assistant message")
    content = message.get("content")
    refusal = message.get("refusal")
    if content is not None and not isinstance(content, str):
        raise ModelProtocolError("Assistant content must be text or null")
    if refusal is not None and not isinstance(refusal, str):
        raise ModelProtocolError("Assistant refusal must be text or null")
    if message.get("function_call") is not None:
        raise UnsupportedModel(
            "The endpoint returned legacy function calls; tool calls are required"
        )
    raw_calls = message.get("tool_calls")
    if raw_calls is None:
        raw_calls = []
    if not isinstance(raw_calls, list) or len(raw_calls) > 32:
        raise ModelProtocolError("Assistant tool calls must be a list of at most 32 items")
    calls = []
    identifiers: set[str] = set()
    for call in raw_calls:
        if not isinstance(call, dict) or call.get("type") != "function":
            raise UnsupportedModel("Only function tool calls are supported")
        function = call.get("function")
        if (
            not isinstance(call.get("id"), str)
            or not call["id"]
            or call["id"] in identifiers
            or not isinstance(function, dict)
            or not isinstance(function.get("name"), str)
            or not isinstance(function.get("arguments"), str)
        ):
            raise ModelProtocolError("Malformed or duplicate function tool call")
        identifiers.add(call["id"])
        calls.append(ToolCall(call["id"], function["name"], function["arguments"]))
    usage = payload.get("usage")
    normalized_usage: dict[str, int | None] | None = None
    if usage is not None:
        if not isinstance(usage, dict):
            raise ModelProtocolError("Usage must be an object or null")
        normalized_usage = {}
        for field_name in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = usage.get(field_name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ModelProtocolError("Token usage must contain nonnegative integers")
            normalized_usage[field_name] = value
    model = payload.get("model")
    finish_reason = choice.get("finish_reason")
    if model is not None and not isinstance(model, str):
        raise ModelProtocolError("Returned model identifier must be text")
    if finish_reason is not None and not isinstance(finish_reason, str):
        raise ModelProtocolError("Finish reason must be text")
    return ModelResponse(content, calls, model, normalized_usage, refusal, finish_reason)
