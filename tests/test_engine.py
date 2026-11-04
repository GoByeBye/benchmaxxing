from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from benchmaxxing import engine
from benchmaxxing.contracts import Budgets, CheckResult, InferenceProfile, RunSpec, ScoreCard
from benchmaxxing.environment import PreparationError, ToolOperationError
from benchmaxxing.model import ModelResponse, ToolCall, UnsupportedModel
from benchmaxxing.storage import Store
from benchmaxxing.task import GOOD_SOLUTION


class FakeEnvironment:
    instances: list[FakeEnvironment] = []

    def __init__(
        self,
        data_dir: Path,
        run_id: str,
        budgets: Budgets,
        cancel_event: threading.Event,
        *,
        deadline: float,
        task_id: str = "python.interval-merge",
    ) -> None:
        self.files = {
            "solution.py": "def merge_intervals(intervals): return intervals\n",
            "TASK.md": "Repair solution.py",
        }
        self.closed = False
        self.cancellation = cancel_event
        self.instances.append(self)

    def start(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    def read_file(self, path: str) -> str:
        return self.files[path]

    def write_file(self, path: str, content: str) -> None:
        self.files[path] = content

    def execute(self, command: str, *, on_output=None) -> dict[str, Any]:
        if on_output:
            on_output("stdout", "public tests complete")
        return {
            "exit_code": 0,
            "stdout": "public tests complete",
            "stderr": "",
            "duration_seconds": 0.01,
        }

    def collect(self) -> bytes:
        return self.files["solution.py"].encode()


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Store:
    FakeEnvironment.instances = []
    monkeypatch.setattr(engine, "DockerEnvironment", FakeEnvironment)
    monkeypatch.setattr(
        engine, "preparation_info", lambda path, task_id: {"image_id": "sha256:test"}
    )

    def grade(source: bytes, *args: Any, **kwargs: Any) -> ScoreCard:
        assert FakeEnvironment.instances[-1].closed
        passed = source.decode() == GOOD_SOLUTION
        return ScoreCard(
            passed=passed,
            score=1 if passed else 0,
            checks=[CheckResult(name="reference", passed=passed)],
        )

    monkeypatch.setattr(engine, "grade_submission", grade)
    return Store(tmp_path / "runs")


@pytest.mark.parametrize(("variant", "status"), [("good", "passed"), ("bad", "failed")])
def test_scripted_run_completes_and_preserves_evidence(
    store: Store, variant: Any, status: str
) -> None:
    queued = store.create_run(RunSpec(scripted_variant=variant))
    result = engine.execute_run(store, queued.id)
    assert result.status == status
    assert result.metadata["model_turns"] == 4
    assert result.metadata["participant_kind"] == "scripted_fixture"
    assert result.metadata["usage"]["total_tokens"] is None
    assert result.score is not None
    assert len(store.artifacts(queued.id)) == 1
    assert FakeEnvironment.instances[0].closed
    kinds = [event.kind for event in store.events(queued.id)]
    assert "model.request" in kinds and "model.response" in kinds
    assert "tool.result" in kinds and "grading" in kinds
    assert kinds[-1] == "run.finished"


def test_preparation_failure_precedes_model_or_environment(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing(path: Path, task_id: str) -> None:
        raise PreparationError("Run prepare before execution")

    monkeypatch.setattr(engine, "preparation_info", missing)
    queued = store.create_run(
        RunSpec(participant="openai", inference=InferenceProfile(model="test"))
    )
    result = engine.execute_run(store, queued.id)
    assert result.status == "infrastructure_error"
    assert result.error == "Run prepare before execution"
    assert not FakeEnvironment.instances
    assert not any(event.kind == "model.request" for event in store.events(queued.id))


def test_model_turn_budget_is_distinct_from_wrong_answer(store: Store) -> None:
    queued = store.create_run(RunSpec(budgets=Budgets(max_turns=1)))
    result = engine.execute_run(store, queued.id)
    assert result.status == "budget_exhausted"
    assert result.score is None
    assert FakeEnvironment.instances[0].closed


def test_cancel_before_start_skips_environment(store: Store) -> None:
    cancellation = threading.Event()
    cancellation.set()
    queued = store.create_run(RunSpec())
    result = engine.execute_run(store, queued.id, cancellation)
    assert result.status == "cancelled"
    assert not FakeEnvironment.instances


def test_cancellation_after_tool_cleans_up(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    def execute(environment: FakeEnvironment, command: str, *, on_output=None) -> dict[str, Any]:
        environment.cancellation.set()
        return {"exit_code": 0}

    monkeypatch.setattr(FakeEnvironment, "execute", execute)
    queued = store.create_run(RunSpec())
    result = engine.execute_run(store, queued.id)
    assert result.status == "cancelled"
    assert FakeEnvironment.instances[0].closed


def test_refusal_is_recorded_separately(store: Store) -> None:
    queued = store.create_run(RunSpec(scripted_variant="refuse"))
    result = engine.execute_run(store, queued.id)
    assert result.status == "failed"
    assert result.refusal == "scripted_fixture"
    assert result.score is None
    assert store.artifacts(queued.id)


def test_completed_run_is_not_executed_again(store: Store) -> None:
    queued = store.create_run(RunSpec())
    engine.execute_run(store, queued.id)
    before = len(store.events(queued.id))
    engine.execute_run(store, queued.id)
    assert len(store.events(queued.id)) == before
    assert len(FakeEnvironment.instances) == 1


def test_live_deltas_precede_validated_tool_execution(store: Store, monkeypatch):
    from benchmaxxing.model import TextRedactor

    class StreamingAdapter:
        def __init__(self, *args, **kwargs):
            self.scripted = engine.ScriptedParticipant("good")

        def redact(self, value):
            return value

        def new_text_redactor(self):
            return TextRedactor("")

        def complete(self, messages, tools, *, on_delta, **kwargs):
            response = self.scripted.complete()
            previous_starts = sum(e.kind == "tool.started" for e in store.events(run.id))
            on_delta({"type": "mode", "mode": "sse"})
            on_delta({"type": "content", "text": "Working "})
            on_delta({"type": "content", "text": "now"})
            assert (
                "".join(
                    e.data["text"]
                    for e in store.events(run.id)
                    if e.kind == "model.delta"
                    and e.data.get("type") == "content"
                    and e.data["turn"] == self.scripted.turn
                )
                == "Working now"
            )
            on_delta({"type": "tool_call_delta", "index": 0, "field": "arguments", "text": "{"})
            assert sum(e.kind == "tool.started" for e in store.events(run.id)) == previous_starts
            assert any(e.kind == "model.delta" for e in store.events(run.id))
            return response

    monkeypatch.setattr(engine, "OpenAIAdapter", StreamingAdapter)
    run = store.create_run(RunSpec(participant="openai", inference=InferenceProfile(model="test")))
    result = engine.execute_run(store, run.id)
    assert result.status == "passed"
    assert result.metadata["requested_response_mode"] == "stream"
    assert result.metadata["response_modes"] == ["sse"]
    events = store.events(run.id)
    for turn in range(1, 5):
        deltas = [e for e in events if e.kind == "model.delta" and e.data["turn"] == turn]
        completed = next(e for e in events if e.kind == "model.response" and e.data["turn"] == turn)
        assert all(e.seq < completed.seq for e in deltas)
        assert (
            "".join(e.data.get("text", "") for e in deltas if e.data["type"] == "content")
            == "Working now"
        )
    output = [e for e in events if e.kind == "tool.output"]
    assert "".join(e.data["text"] for e in output) == "public tests complete"
    assert next(e for e in events if e.kind == "tool.started").data["arguments"]


def adapter_fixture(
    monkeypatch: pytest.MonkeyPatch, responses: list[ModelResponse | Exception]
) -> None:
    class Adapter:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def redact(self, value: Any) -> Any:
            return value

        def new_text_redactor(self):
            class Identity:
                def feed(self, text):
                    return text

                def finish(self):
                    return ""

            return Identity()

        def complete(self, *args: Any, **kwargs: Any) -> ModelResponse:
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

    monkeypatch.setattr(engine, "OpenAIAdapter", Adapter)


def test_normal_final_answer_is_not_automatically_labeled_refusal(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter_fixture(monkeypatch, [ModelResponse(content="I am done.", finish_reason="stop")])
    queued = store.create_run(
        RunSpec(participant="openai", inference=InferenceProfile(model="test"))
    )
    result = engine.execute_run(store, queued.id)
    assert result.status == "failed"
    assert result.refusal is None


def test_provider_content_filter_has_distinct_trace_without_refusal_label(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter_fixture(monkeypatch, [ModelResponse(content=None, finish_reason="content_filter")])
    queued = store.create_run(
        RunSpec(participant="openai", inference=InferenceProfile(model="test"))
    )
    result = engine.execute_run(store, queued.id)
    assert result.status == "failed"
    assert result.refusal is None
    events = list(store.events(queued.id))
    filtered = [item for item in events if item.kind == "model.filtered"]
    assert len(filtered) == 1 and filtered[0].data == {"turn": 1, "reason": "content_filter"}
    assert any(
        item.kind == "model.response" and item.data["finish_reason"] == "content_filter"
        for item in events
    )


def test_unsupported_tool_protocol_has_separate_status(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter_fixture(monkeypatch, [UnsupportedModel("Legacy functions are unsupported")])
    queued = store.create_run(
        RunSpec(participant="openai", inference=InferenceProfile(model="test"))
    )
    result = engine.execute_run(store, queued.id)
    assert result.status == "unsupported"
    assert FakeEnvironment.instances[0].closed


def test_invalid_tool_arguments_can_be_repaired(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter_fixture(
        monkeypatch,
        [
            ModelResponse(None, [ToolCall("1", "write_file", "not-json")]),
            ModelResponse(
                None,
                [
                    ToolCall(
                        "2",
                        "write_file",
                        json.dumps({"path": "solution.py", "content": GOOD_SOLUTION}),
                    )
                ],
            ),
            ModelResponse(None, [ToolCall("3", "submit", "{}")]),
        ],
    )
    queued = store.create_run(
        RunSpec(participant="openai", inference=InferenceProfile(model="test"))
    )
    result = engine.execute_run(store, queued.id)
    assert result.status == "passed"
    errors = [
        event.data
        for event in store.events(queued.id)
        if event.kind == "tool.result" and "error" in event.data["result"]
    ]
    assert errors


def test_unknown_internal_errors_do_not_persist_exception_text(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    def start(environment: FakeEnvironment) -> None:
        raise RuntimeError("sensitive credential here")

    monkeypatch.setattr(FakeEnvironment, "start", start)
    queued = store.create_run(RunSpec())
    result = engine.execute_run(store, queued.id)
    assert result.status == "infrastructure_error"
    assert "sensitive" not in result.model_dump_json()
    assert "sensitive" not in json.dumps([event.model_dump() for event in store.events(queued.id)])
    assert FakeEnvironment.instances[0].closed


@pytest.mark.parametrize(
    "exception", [ValueError("Path leaves workspace"), ToolOperationError("File not found")]
)
def test_invalid_file_operations_are_recoverable(
    store: Store, monkeypatch: pytest.MonkeyPatch, exception: Exception
) -> None:
    def read(environment: FakeEnvironment, path: str) -> str:
        raise exception

    monkeypatch.setattr(FakeEnvironment, "read_file", read)
    queued = store.create_run(RunSpec())
    result = engine.execute_run(store, queued.id)
    assert result.status == "passed"
    assert any(
        event.kind == "tool.result" and "error" in event.data["result"]
        for event in store.events(queued.id)
    )


def test_missing_submission_is_task_failure(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    def collect(environment: FakeEnvironment) -> bytes:
        raise ToolOperationError("File not found")

    monkeypatch.setattr(FakeEnvironment, "collect", collect)
    queued = store.create_run(RunSpec())
    result = engine.execute_run(store, queued.id)
    assert result.status == "failed"
    assert result.error == "The submitted solution.py could not be collected"


def test_final_state_and_event_are_committed_together(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    update = store.update_run
    terminal_updates = []

    def observed_update(run_id: str, **kwargs: Any) -> Any:
        if kwargs.get("status") in {
            "passed",
            "failed",
            "cancelled",
            "infrastructure_error",
            "budget_exhausted",
        }:
            terminal_updates.append(kwargs)
            assert kwargs["event_kind"] == "run.finished"
            assert kwargs["event_data"]["status"] == kwargs["status"]
        return update(run_id, **kwargs)

    monkeypatch.setattr(store, "update_run", observed_update)
    queued = store.create_run(RunSpec())
    assert engine.execute_run(store, queued.id).status == "passed"
    assert len(terminal_updates) == 1
