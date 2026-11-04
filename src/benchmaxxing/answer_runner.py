"""Shared no-tool answer lifecycle for knowledge and supplied-context tasks."""

from __future__ import annotations

import hashlib
import platform
import threading
import time
from typing import Any

from benchmaxxing import __version__
from benchmaxxing.answers import (
    ANSWER_HARNESS,
    ANSWER_SYSTEM,
    context,
    definitions,
    fixture,
    grade,
    render_prompt,
)
from benchmaxxing.contracts import RunRecord, RunStatus
from benchmaxxing.environment import (
    EnvironmentError,
    ExecutionCancelled,
    OutputLimitExceeded,
    preparation_info,
)
from benchmaxxing.model import (
    ModelCancelled,
    ModelDeadlineExceeded,
    ModelError,
    ModelOutputLimitExceeded,
    ModelResponse,
    OpenAIAdapter,
    UnsupportedModel,
)
from benchmaxxing.storage import Store


def execute_answer(
    store: Store, run_id: str, cancel_event: threading.Event | None = None
) -> RunRecord:
    run = store.get_run(run_id)
    if run.status != "queued":
        return run
    spec = run.spec
    cancel = cancel_event or threading.Event()
    started = time.monotonic()
    deadline = started + spec.budgets.wall_seconds
    model: OpenAIAdapter | None = None
    status: RunStatus = "infrastructure_error"
    error = None
    refusal = None
    metadata = dict(run.metadata)
    used_artifact_bytes = turns = 0
    submitted = False

    def checkpoint() -> None:
        if cancel.is_set():
            raise ExecutionCancelled("Run cancelled")
        if time.monotonic() >= deadline:
            raise ModelDeadlineExceeded("Run time budget exhausted")

    def event(kind: str, value: dict[str, Any]) -> None:
        store.append_event(run_id, kind, model.redact(value) if model else value)

    def artifact(name: str, raw: bytes, media_type: str) -> str:
        nonlocal used_artifact_bytes
        if used_artifact_bytes + len(raw) > spec.budgets.artifact_bytes:
            raise OutputLimitExceeded("Answer evidence exceeds the total artifact byte budget")
        record = store.add_artifact(run_id, name, raw, media_type)
        used_artifact_bytes += len(raw)
        event("artifact", record.model_dump(mode="json"))
        return record.id

    store.update_run(run_id, status="running")
    event("run.started", {"status": "running"})
    try:
        checkpoint()
        event("phase", {"phase": "preflight"})
        task = definitions()[spec.task_id]
        prepared = preparation_info(store.data_dir, task.id)
        public = task.public_spec()
        prompt = render_prompt(task)
        prompt_bytes = prompt.encode("utf-8")
        if len(prompt_bytes) > 4 * 1024 * 1024:
            raise OutputLimitExceeded("Answer input exceeds the four MiB harness limit")
        prompt_id = artifact("input-prompt.txt", prompt_bytes, "text/plain; charset=utf-8")
        system_id = artifact(
            "system-prompt.txt", ANSWER_SYSTEM.encode(), "text/plain; charset=utf-8"
        )
        supplied, offsets = context(task.id)
        context_metadata: dict[str, Any] | None = None
        if task.retrieval:
            context_metadata = {
                **task.retrieval.model_dump(mode="json"),
                "sha256": hashlib.sha256(supplied.encode()).hexdigest(),
                "characters": len(supplied),
                "tokens": None,
                "tokenizer": None,
                "record_offsets": offsets,
                "artifact_id": prompt_id,
                "artifact_start_byte": len(prompt.split("<context>\n", 1)[0].encode())
                + len(b"<context>\n"),
                "artifact_byte_length": len(supplied.encode()),
            }
        metadata.update(
            {
                "framework_version": __version__,
                "harness_version": ANSWER_HARNESS,
                "participant_kind": "model_with_harness"
                if spec.participant == "openai"
                else "scripted_fixture",
                "task": public.model_dump(mode="json"),
                "execution_profile": public.execution_profile,
                "tools_available": [],
                "environment": {
                    "profile": "prompt-only",
                    "candidate_container": False,
                    "external_task_network": False,
                },
                "preparation": prepared,
                "platform": {
                    "system": platform.system(),
                    "release": platform.release(),
                    "machine": platform.machine(),
                    "python": platform.python_version(),
                },
                "requested_model": spec.inference.model if spec.inference else None,
                "inference_mode": spec.inference.mode if spec.inference else "none",
                "requested_generation": spec.generation.model_dump(mode="json")
                if spec.inference
                else None,
                "effective_generation": None,
                "model_provenance": None,
                "input_prompt": {
                    "artifact_id": prompt_id,
                    "sha256": hashlib.sha256(prompt_bytes).hexdigest(),
                    "bytes": len(prompt_bytes),
                    "system_artifact_id": system_id,
                },
                "context": context_metadata,
                "reference_sources": [source.model_dump(mode="json") for source in task.sources],
                "usage": None,
            }
        )
        store.update_run(run_id, metadata=metadata)
        checkpoint()
        if spec.participant == "openai":
            assert spec.inference is not None
            model = OpenAIAdapter(
                spec.inference, spec.generation, max_response_bytes=spec.budgets.output_bytes
            )
        event("phase", {"phase": "participant"})
        messages = [
            {"role": "system", "content": ANSWER_SYSTEM},
            {"role": "user", "content": prompt},
        ]
        turns = 1
        event(
            "model.request",
            {
                "turn": turns,
                "messages": messages,
                "tools": [],
                "generation": spec.generation.model_dump(mode="json") if model else None,
                "response_mode": "stream" if model else "scripted",
            },
        )
        if model:
            response = model.complete(
                messages,
                [],
                deadline=deadline,
                cancel_event=cancel,
                on_delta=lambda value: event("model.delta", {"turn": 1, **value}),
            )
        else:
            response = ModelResponse(
                content="The scripted fixture declines this task."
                if spec.scripted_variant == "refuse"
                else fixture(task, spec.scripted_variant),
                refusal="scripted_fixture" if spec.scripted_variant == "refuse" else None,
                model="scripted/" + spec.scripted_variant,
                finish_reason="stop",
            )
        event(
            "model.response",
            {
                "turn": 1,
                "message": response.message_dict(),
                "model": response.model,
                "usage": response.usage,
                "finish_reason": response.finish_reason,
            },
        )
        metadata["usage"] = response.usage
        metadata["returned_models"] = [response.model] if response.model else []
        if response.refusal:
            refusal = "explicit_provider" if model else "scripted_fixture"
        checkpoint()
        if response.tool_calls:
            raise UnsupportedModel("Answer-only profiles reject returned tool calls")
        raw = (response.content or "").encode()
        if len(raw) > spec.budgets.output_bytes:
            raise ModelOutputLimitExceeded("Answer exceeded the response byte budget")
        artifact("submission.json", raw, "application/json")
        submitted = bool(raw)
        if response.finish_reason == "length":
            raise ModelOutputLimitExceeded("Answer exhausted its response-token budget")
        event("phase", {"phase": "grade"})
        score, assessment = grade(task, raw)
        metadata["answer_assessment"] = assessment
        for index, check in enumerate(score.checks, 1):
            checkpoint()
            event(
                "grading.check",
                {
                    "completed": index,
                    "total": len(score.checks),
                    "name": check.name,
                    "passed": check.passed,
                },
            )
        store.update_run(run_id, score=score)
        event("grading", score.model_dump(mode="json"))
        status = "passed" if score.passed else "failed"
    except (ExecutionCancelled, ModelCancelled):
        status, error = "cancelled", "Run cancelled"
    except (ModelDeadlineExceeded, ModelOutputLimitExceeded, OutputLimitExceeded) as exc:
        status, error = "budget_exhausted", str(exc)
    except UnsupportedModel as exc:
        status, error = "unsupported", str(exc)
    except (EnvironmentError, ModelError) as exc:
        status, error = "infrastructure_error", str(exc)
    except Exception:
        status, error = "infrastructure_error", "Unexpected internal error during answer evaluation"
    finally:
        metadata.update(
            elapsed_seconds=time.monotonic() - started, model_turns=turns, submitted=submitted
        )
        safe_error = model.redact(error) if model else error
        store.update_run(
            run_id,
            status=status,
            error=safe_error,
            refusal=refusal,
            metadata=model.redact(metadata) if model else metadata,
            event_kind="run.finished",
            event_data={"status": status, "error": safe_error},
        )
    return store.get_run(run_id)
