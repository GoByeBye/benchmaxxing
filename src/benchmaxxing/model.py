"""Bounded OpenAI-compatible requests with explicit local/API profiles.

Only HTTP transport is standardized here. A loopback endpoint is not evidence
that the process serving it performs local inference; users must verify that
runtime independently with internet access disabled.
"""

from __future__ import annotations

import asyncio
import codecs
import ipaddress
import json
import os
import re
import threading
import time
from collections.abc import Callable
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


class TextRedactor:
    """Redact one text channel without leaking credentials split across writes."""

    def __init__(self, secret: str) -> None:
        self.secret = secret
        self.pending = ""

    def feed(self, text: str) -> str:
        value = self.pending + text
        self.pending = ""
        if self.secret:
            # Replace complete matches before retaining a suffix: overlapping prefixes
            # (e.g. secret 'aaa', value 'aaaa') must not leak through the boundary.
            pieces: list[str] = []
            start = 0
            while (position := value.find(self.secret, start)) >= 0:
                pieces.extend((value[start:position], "[REDACTED]"))
                start = position + len(self.secret)
            tail = value[start:]
            keep = 0
            for length in range(min(len(self.secret) - 1, len(tail)), 0, -1):
                if self.secret.startswith(tail[-length:]):
                    keep = length
                    break
            if keep:
                self.pending = tail[-keep:]
                tail = tail[:-keep]
            value = "".join(pieces) + tail
        return value

    def finish(self) -> str:
        """Release a nonmatching suffix after successful end-of-channel."""
        value, self.pending = self.pending, ""
        return value


class _DeltaEmitter:
    def __init__(self, callback: Callable[[dict[str, Any]], None], secret: str) -> None:
        self.callback = callback
        self.secret = secret
        self.redactors: dict[tuple[str, int | None, str | None], TextRedactor] = {}

    def text(
        self,
        kind: str,
        text: str,
        *,
        index: int | None = None,
        field_name: str | None = None,
        final: bool = False,
    ) -> None:
        key = kind, index, field_name
        redactor = self.redactors.setdefault(key, TextRedactor(self.secret))
        value = redactor.finish() if final else redactor.feed(text)
        if value:
            event: dict[str, Any] = {"type": kind, "text": value}
            if index is not None:
                event.update(index=index, field=field_name)
            self.callback(event)

    def finish(self) -> None:
        for kind, index, field_name in self.redactors:
            self.text(kind, "", index=index, field_name=field_name, final=True)


class _SSEFrames:
    """Strict incremental UTF-8 and SSE framing, including split CRLF delimiters."""

    def __init__(self, limit: int) -> None:
        self.decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self.buffer = ""
        self.data: list[str] = []
        self.size = 0
        self.limit = limit
        self.first = True

    def feed(self, chunk: bytes) -> list[str]:
        try:
            text = self.decoder.decode(chunk)
        except UnicodeError as exc:
            raise ModelProtocolError("Inference stream contains invalid UTF-8") from exc
        if self.first and text:
            text = text.removeprefix("\ufeff")
            self.first = False
        self.buffer += text
        frames = []
        while match := re.search(r"\r\n|\r|\n", self.buffer):
            if match.group() == "\r" and match.end() == len(self.buffer):
                break
            line = self.buffer[: match.start()]
            self.buffer = self.buffer[match.end() :]
            if len(line.encode("utf-8")) + self.size > self.limit:
                raise ModelOutputLimitExceeded("Inference stream frame exceeded the output budget")
            if not line:
                if self.data:
                    frames.append("\n".join(self.data))
                self.data = []
                self.size = 0
            elif line.startswith("data:"):
                value = line[5:].removeprefix(" ")
                self.data.append(value)
                self.size += len(value.encode("utf-8")) + 1
        if len(self.buffer.encode("utf-8")) + self.size > self.limit:
            raise ModelOutputLimitExceeded("Inference stream frame exceeded the output budget")
        return frames


class _StreamResponse:
    """Assemble one choice; tool fragments are never executable intermediate calls."""

    def __init__(self, emitter: _DeltaEmitter, limit: int) -> None:
        self.emitter = emitter
        self.limit = limit
        self.size = 0
        self.content: str | None = None
        self.refusal: str | None = None
        self.calls: dict[int, dict[str, Any]] = {}
        self.model: str | None = None
        self.usage: dict[str, int | None] | None = None
        self.finish_reason: str | None = None
        self.saw_choice = False

    def charge(self, value: str) -> None:
        self.size += len(value.encode("utf-8"))
        if self.size > self.limit:
            raise ModelOutputLimitExceeded("Inference response exceeded the output budget")

    def accept(self, payload: Any) -> None:
        if not isinstance(payload, dict) or "error" in payload:
            raise ModelProtocolError("Inference stream returned an invalid response event")
        model = payload.get("model")
        if model is not None:
            if not isinstance(model, str) or (self.model is not None and model != self.model):
                raise ModelProtocolError("Inference stream changed its model identifier")
            if self.model is None:
                self.charge(model)
                self.model = model
        usage = payload.get("usage")
        if usage is not None:
            self.usage = _parse_usage(usage)
            assert self.usage is not None
            self.emitter.callback({"type": "usage", "usage": self.usage})
        choices = payload.get("choices")
        if not isinstance(choices, list) or len(choices) > 1:
            raise ModelProtocolError("Inference stream must contain one choice")
        if not choices:
            if usage is None:
                raise ModelProtocolError("Empty stream choices require usage information")
            return
        choice = choices[0]
        if (
            not isinstance(choice, dict)
            or type(choice.get("index", 0)) is not int
            or choice.get("index", 0) != 0
        ):
            raise ModelProtocolError("Inference stream must contain choice zero")
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            raise ModelProtocolError("Inference stream choice must contain a delta")
        if delta.get("role", "assistant") not in (None, "assistant"):
            raise ModelProtocolError("Inference stream role must be assistant")
        if delta.get("function_call") is not None:
            raise UnsupportedModel(
                "The endpoint returned legacy function calls; tool calls are required"
            )
        if self.finish_reason is not None:
            raise ModelProtocolError("Inference stream continued a finished choice")
        self.saw_choice = True
        for name in ("content", "refusal", "reasoning_content", "reasoning"):
            value = delta.get(name)
            if value is None:
                continue
            if not isinstance(value, str):
                raise ModelProtocolError("Inference stream text fields must be strings or null")
            self.charge(value)
            kind = "reasoning" if name.startswith("reasoning") else name
            if name in ("content", "refusal"):
                setattr(self, name, (getattr(self, name) or "") + value)
            self.emitter.text(kind, value)
        calls = delta.get("tool_calls")
        if calls is not None:
            if not isinstance(calls, list) or len(calls) > 32:
                raise ModelProtocolError("Inference stream tool calls must be a bounded list")
            indexes = set()
            for call in calls:
                if not isinstance(call, dict):
                    raise ModelProtocolError("Malformed streamed tool call")
                index = call.get("index")
                if type(index) is not int or not 0 <= index < 32 or index in indexes:
                    raise ModelProtocolError("Invalid streamed tool call index")
                indexes.add(index)
                if call.get("type", "function") != "function":
                    raise UnsupportedModel("Only function tool calls are supported")
                function = call.get("function", {})
                if not isinstance(function, dict):
                    raise ModelProtocolError("Malformed streamed function")
                target = self.calls.setdefault(
                    index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}
                )
                for name, source in (("id", call), ("name", function), ("arguments", function)):
                    if name not in source:
                        continue
                    value = source[name]
                    if not isinstance(value, str):
                        raise ModelProtocolError("Streamed tool fields must be text")
                    self.charge(value)
                    destination = target if name == "id" else target["function"]
                    destination[name] += value
                    self.emitter.text("tool_call_delta", value, index=index, field_name=name)
        finish = choice.get("finish_reason")
        if finish is not None:
            if not isinstance(finish, str) or not finish:
                raise ModelProtocolError("Stream finish reason must be nonempty text")
            self.charge(finish)
            self.finish_reason = finish

    def payload(self) -> dict[str, Any]:
        if not self.saw_choice or self.finish_reason is None:
            raise ModelProtocolError("Inference stream ended before its choice was complete")
        if sorted(self.calls) != list(range(len(self.calls))):
            raise ModelProtocolError("Inference stream has missing tool call indices")
        return {
            "model": self.model,
            "usage": self.usage,
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": self.content,
                        "refusal": self.refusal,
                        "tool_calls": [self.calls[i] for i in sorted(self.calls)],
                    },
                    "finish_reason": self.finish_reason,
                }
            ],
        }


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

    def new_text_redactor(self) -> TextRedactor:
        return TextRedactor(self._api_key)

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        deadline: float,
        cancel_event: threading.Event | None = None,
        on_delta: Callable[[dict[str, Any]], None] | None = None,
    ) -> ModelResponse:
        return asyncio.run(self._complete(messages, tools, deadline, cancel_event, on_delta))

    async def _complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        deadline: float,
        cancel_event: threading.Event | None,
        on_delta: Callable[[dict[str, Any]], None] | None,
    ) -> ModelResponse:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ModelDeadlineExceeded("Run time budget exhausted during inference")
        if cancel_event is not None and cancel_event.is_set():
            raise ModelCancelled("Inference cancelled")
        request = asyncio.create_task(self._request(messages, tools, remaining, on_delta))

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
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        remaining: float,
        on_delta: Callable[[dict[str, Any]], None] | None,
    ) -> ModelResponse:
        payload: dict[str, Any] = {
            "model": self.profile.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "stream": on_delta is not None,
            "temperature": self.generation.temperature,
            "max_tokens": self.generation.max_tokens,
        }
        if self.generation.seed is not None:
            payload["seed"] = self.generation.seed
        if on_delta is not None:
            payload["stream_options"] = {"include_usage": True}
        # Do not allow a compressed response to expand before our byte limit.
        headers = {
            "Accept": "text/event-stream, application/json"
            if on_delta is not None
            else "application/json",
            "Accept-Encoding": "identity",
        }
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
                    return await self._read_response(response, on_delta)
        except httpx.TimeoutException as exc:
            raise ModelDeadlineExceeded("Inference transport timed out") from exc
        except httpx.HTTPError as exc:
            raise ModelError("Could not communicate with the inference endpoint") from exc

    async def _read_response(
        self,
        response: httpx.Response,
        on_delta: Callable[[dict[str, Any]], None] | None,
    ) -> ModelResponse:
        body = bytearray()
        wire_bytes = 0
        wire_limit = (
            self.max_response_bytes if on_delta is None else self.max_response_bytes * 32 + 262144
        )
        mode: str | None = "json" if on_delta is None else None
        emitter = _DeltaEmitter(on_delta, self._api_key) if on_delta is not None else None
        frames = _SSEFrames(self.max_response_bytes + 65536)
        assembled = _StreamResponse(emitter, self.max_response_bytes) if emitter else None
        async for chunk in response.aiter_bytes():
            # No fixed-size HTTPX coalescing: short writes must reach the live feed.
            await asyncio.sleep(0)
            wire_bytes += len(chunk)
            if wire_bytes > wire_limit:
                raise ModelOutputLimitExceeded("Inference wire response exceeded the output budget")
            if mode is None:
                body.extend(chunk)
                prefix = bytes(body).lstrip()
                if prefix.startswith(b"\xef") and len(prefix) < 3:
                    continue
                prefix = prefix.removeprefix(b"\xef\xbb\xbf").lstrip()
                if not prefix:
                    continue
                mode = "json" if prefix[:1] in (b"{", b"[") else "sse"
                assert on_delta is not None
                on_delta({"type": "mode", "mode": mode})
                chunk = bytes(body)
                body.clear()
            if mode == "json":
                if len(body) + len(chunk) > self.max_response_bytes:
                    raise ModelOutputLimitExceeded("Inference response exceeded the output budget")
                body.extend(chunk)
                continue
            assert emitter is not None and assembled is not None and on_delta is not None
            for frame in frames.feed(chunk):
                await asyncio.sleep(0)
                if frame == "[DONE]":
                    result = parse_response(self.redact(assembled.payload()))
                    emitter.finish()
                    on_delta({"type": "finish", "finish_reason": result.finish_reason})
                    return result
                assembled.accept(_decode_json(frame))
        if mode != "json":
            raise ModelProtocolError("Inference stream ended without [DONE]")
        raw_payload = _decode_json(body)
        result = parse_response(self.redact(raw_payload))
        if on_delta is not None:
            assert emitter is not None
            raw_result = parse_response(raw_payload)
            for name in ("content", "refusal"):
                value = getattr(raw_result, name)
                if value is not None:
                    emitter.text(name, value)
            for name in ("reasoning_content", "reasoning"):
                value = raw_payload["choices"][0]["message"].get(name)
                if isinstance(value, str):
                    emitter.text("reasoning", value)
            for index, call in enumerate(raw_result.tool_calls):
                for name in ("id", "name", "arguments"):
                    emitter.text(
                        "tool_call_delta", getattr(call, name), index=index, field_name=name
                    )
            emitter.finish()
            if result.usage is not None:
                on_delta({"type": "usage", "usage": result.usage})
            on_delta({"type": "finish", "finish_reason": result.finish_reason})
        return result


def _decode_json(value: str | bytes | bytearray) -> Any:
    try:
        return json.loads(value)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ModelProtocolError("Inference endpoint returned invalid JSON") from exc


def _parse_usage(usage: Any) -> dict[str, int | None] | None:
    if usage is None:
        return None
    if not isinstance(usage, dict):
        raise ModelProtocolError("Usage must be an object or null")
    normalized: dict[str, int | None] = {}
    for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(name)
        if value is not None and (type(value) is not int or value < 0):
            raise ModelProtocolError("Token usage must contain nonnegative integers")
        normalized[name] = value
    return normalized


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
            or not function["name"]
            or not isinstance(function.get("arguments"), str)
        ):
            raise ModelProtocolError("Malformed or duplicate function tool call")
        identifiers.add(call["id"])
        calls.append(ToolCall(call["id"], function["name"], function["arguments"]))
    normalized_usage = _parse_usage(payload.get("usage"))
    model = payload.get("model")
    finish_reason = choice.get("finish_reason")
    if model is not None and not isinstance(model, str):
        raise ModelProtocolError("Returned model identifier must be text")
    if finish_reason is not None and not isinstance(finish_reason, str):
        raise ModelProtocolError("Finish reason must be text")
    return ModelResponse(content, calls, model, normalized_usage, refusal, finish_reason)
