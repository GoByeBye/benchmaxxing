"""Saved inference endpoints and bounded, explicit model discovery."""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Literal

import httpx
from pydantic import Field, model_validator

from benchmaxxing.contracts import Contract, InferenceProfile


class EndpointInput(Contract):
    name: str = Field(min_length=1, max_length=100)
    mode: Literal["offline", "api"] = "offline"
    base_url: str = Field(default="http://127.0.0.1:8080/v1", max_length=2048)
    api_key_env: str | None = Field(default=None, max_length=200)
    api_style: Literal["chat_completions", "responses"] = "chat_completions"
    responses_reasoning: bool = False
    vllm_priority: int | None = Field(default=None, strict=True, ge=-2147483648, le=2147483647)
    model_ids: list[str] = Field(default_factory=list, max_length=1000)

    @model_validator(mode="after")
    def validate_settings(self) -> EndpointInput:
        profile = InferenceProfile(
            mode=self.mode,
            base_url=self.base_url,
            api_key_env=self.api_key_env,
            api_style=self.api_style,
            responses_reasoning=self.responses_reasoning,
            vllm_priority=self.vllm_priority,
            model="discovery",
        )
        self.base_url = profile.base_url
        if any(not item.strip() or len(item) > 500 for item in self.model_ids):
            raise ValueError("Model IDs must contain 1 to 500 characters")
        self.model_ids = list(dict.fromkeys(self.model_ids))
        return self


class Endpoint(EndpointInput):
    id: str


class ModelDiscovery(Contract):
    endpoint_id: str
    model_ids: list[str]


class EndpointStore:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "endpoints.sqlite3"

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=5)
        db.execute(
            "CREATE TABLE IF NOT EXISTS endpoints (id TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        with closing(db), db:
            yield db

    def list(self) -> list[Endpoint]:
        with self._connect() as db:
            return [
                Endpoint.model_validate_json(row[0])
                for row in db.execute("SELECT value FROM endpoints ORDER BY rowid")
            ]

    def get(self, endpoint_id: str) -> Endpoint:
        with self._connect() as db:
            row = db.execute("SELECT value FROM endpoints WHERE id = ?", (endpoint_id,)).fetchone()
        if row is None:
            raise KeyError(endpoint_id)
        return Endpoint.model_validate_json(row[0])

    def save(self, value: EndpointInput, endpoint_id: str | None = None) -> Endpoint:
        result = Endpoint(id=endpoint_id or uuid.uuid4().hex, **value.model_dump())
        with self._connect() as db:
            if endpoint_id is None:
                db.execute(
                    "INSERT INTO endpoints VALUES (?, ?)", (result.id, result.model_dump_json())
                )
            else:
                cursor = db.execute(
                    "UPDATE endpoints SET value = ? WHERE id = ?",
                    (result.model_dump_json(), endpoint_id),
                )
                if not cursor.rowcount:
                    raise KeyError(endpoint_id)
        return result

    def delete(self, endpoint_id: str) -> None:
        with self._connect() as db:
            if not db.execute("DELETE FROM endpoints WHERE id = ?", (endpoint_id,)).rowcount:
                raise KeyError(endpoint_id)


def discover_models(endpoint: Endpoint) -> ModelDiscovery:
    """Never use proxies or redirects; offline URLs were pinned to numeric loopback."""
    headers = {}
    if endpoint.api_key_env:
        key = os.environ.get(endpoint.api_key_env)
        if not key:
            raise ValueError("The configured API key environment variable is not set")
        headers["Authorization"] = f"Bearer {key}"
    deadline = time.monotonic() + 20
    try:
        with httpx.Client(timeout=5, trust_env=False, follow_redirects=False) as client:
            with client.stream("GET", endpoint.base_url + "/models", headers=headers) as response:
                if response.status_code != 200:
                    raise ValueError(f"Model discovery returned HTTP {response.status_code}")
                content = bytearray()
                for chunk in response.iter_bytes():
                    if time.monotonic() > deadline or len(content) + len(chunk) > 1048576:
                        raise ValueError("Model discovery exceeded its time or size limit")
                    content.extend(chunk)
        payload = json.loads(content)
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or len(rows) > 10000:
            raise ValueError("Model discovery returned an invalid model list")
        ids = []
        for row in rows:
            model_id = row.get("id") if isinstance(row, dict) else None
            if not isinstance(model_id, str) or not model_id.strip() or len(model_id) > 500:
                raise ValueError("Model discovery returned an invalid model ID")
            ids.append(model_id)
        return ModelDiscovery(endpoint_id=endpoint.id, model_ids=sorted(set(ids)))
    except (httpx.HTTPError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Model discovery failed; check the endpoint and server") from exc
