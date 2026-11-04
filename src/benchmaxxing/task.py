"""The installed, versioned reference task and deterministic test participants."""

import hashlib
import json
import math
from functools import lru_cache
from importlib.resources import files
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import Field, model_validator

from benchmaxxing.contracts import Contract, TaskSpec

DEFAULT_IMAGE = "python:3.13-slim"
REFERENCE_NAMES = ("TASK.md", "solution.py", "public_tests.py")
REFERENCE_ID = "python.interval-merge"


def reference_files() -> dict[str, bytes]:
    directory = files("benchmaxxing").joinpath("reference")
    return {name: directory.joinpath(name).read_bytes() for name in REFERENCE_NAMES}


PROMPT = reference_files()["TASK.md"].decode("utf-8")

GOOD_SOLUTION = """def merge_intervals(intervals):
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged
"""

BAD_SOLUTION = "def merge_intervals(intervals):\n    return []\n"


def _reference_task() -> TaskSpec:
    digest = hashlib.sha256()
    for name, content in sorted(reference_files().items()):
        digest.update(name.encode("utf-8") + b"\0" + content + b"\0")
    return TaskSpec(description=PROMPT, task_hash=digest.hexdigest(), image=DEFAULT_IMAGE)


class Case(Contract):
    name: str = Field(min_length=1, max_length=100)
    input: Any = None
    expected: Any
    path: list[str | int] = Field(default_factory=list, max_length=16)


class TaskDefinition(Contract):
    """Trusted installed pack. Only prompt and files enter a candidate workspace."""

    schema_version: Literal[1] = 1
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]*\.[a-z0-9-]+$", max_length=100)
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    title: str = Field(min_length=1, max_length=120)
    category: Literal["programming", "engineering", "security", "terminal", "refusal"]
    difficulty: Literal["easy", "medium", "hard"]
    tags: list[str] = Field(default_factory=list, max_length=16)
    kind: Literal["python", "json"] = "python"
    entrypoint: str = Field(default="solve", pattern=r"^[a-zA-Z][a-zA-Z0-9_]*$")
    prompt: str = Field(min_length=40, max_length=50000)
    rubric: str = ""
    files: dict[str, str]
    good_solution: str = Field(min_length=1, max_length=1000000)
    bad_solution: str = Field(min_length=1, max_length=1000000)
    cases: list[Case] = Field(min_length=1, max_length=128)
    atol: float = Field(default=0, ge=0, allow_inf_nan=False)
    rtol: float = Field(default=0, ge=0, allow_inf_nan=False)
    preserve_input: bool = True
    expected_decision: Literal["comply", "refuse"] | None = None

    @model_validator(mode="after")
    def validate_assets(self) -> "TaskDefinition":
        if len({case.name for case in self.cases}) != len(self.cases):
            raise ValueError("Check names must be unique")
        if self.good_solution == self.bad_solution:
            raise ValueError("Good and bad fixtures must differ")
        if "public_tests.py" not in self.files:
            raise ValueError("A public check script is required")
        if self.kind == "python" and "solution.py" not in self.files:
            raise ValueError("Python tasks require starter solution.py")
        for name, content in self.files.items():
            path = PurePosixPath(name)
            if (
                not name
                or name == "TASK.md"
                or "\\" in name
                or "\0" in name
                or path.is_absolute()
                or ".." in path.parts
                or str(path) != name
            ):
                raise ValueError("Pack assets must use normalized relative workspace paths")
            if len(content.encode()) > 1000000:
                raise ValueError("Pack asset exceeds one megabyte")
        if sum(len(s.encode()) for s in self.files.values()) > 4000000:
            raise ValueError("Pack assets exceed four megabytes")
        if self.expected_decision is not None and (
            self.kind != "json" or self.category != "refusal"
        ):
            raise ValueError("Decision rubrics require a refusal JSON task")
        if self.expected_decision is not None and any(
            len(case.path) != 1 or not isinstance(case.path[0], str) for case in self.cases
        ):
            raise ValueError("Decision rubrics must cover named top-level fields")
        for case in self.cases:
            _validate_json(case.input)
            _validate_json(case.expected)
        return self

    @property
    def submission_file(self) -> str:
        return "solution.py" if self.kind == "python" else "submission.json"

    def public_spec(self) -> TaskSpec:
        content = json.dumps(
            self.model_dump(), sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        return TaskSpec(
            id=self.id,
            version=self.version,
            title=self.title,
            category=self.category,
            description=self.prompt,
            task_hash=hashlib.sha256(content.encode()).hexdigest(),
            image=DEFAULT_IMAGE,
            kind=self.kind,
            submission_file=self.submission_file,
            difficulty=self.difficulty,
            tags=self.tags,
            check_count=len(self.cases),
            rubric=self.rubric
            or (
                f"Independent function checks. Numeric tolerances: absolute {self.atol:g}, "
                f"relative {self.rtol:g}. "
                f"Input preservation: {'required' if self.preserve_input else 'not required'}."
                if self.kind == "python"
                else "Required JSON artifact fields are compared with independent expected values."
            ),
        )


def _validate_json(value: Any, depth: int = 0) -> None:
    if depth > 32:
        raise ValueError("Pack JSON nesting exceeds 32 levels")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Pack JSON values must be finite")
    if isinstance(value, dict):
        for item in value.values():
            _validate_json(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _validate_json(item, depth + 1)


@lru_cache(maxsize=1)
def definitions() -> dict[str, TaskDefinition]:
    result: dict[str, TaskDefinition] = {}
    directory = files("benchmaxxing").joinpath("packs")
    if not directory.is_dir():
        return result
    for category in sorted(directory.iterdir(), key=lambda p: p.name):
        if not category.is_dir():
            continue
        for path in sorted(category.iterdir(), key=lambda p: p.name):
            if path.name.endswith(".json"):
                definition = TaskDefinition.model_validate_json(path.read_bytes())
                if definition.id in result or definition.id == REFERENCE_ID:
                    raise ValueError(f"Duplicate task ID: {definition.id}")
                result[definition.id] = definition
    return result


def get_definition(task_id: str) -> TaskDefinition:
    try:
        return definitions()[task_id]
    except KeyError:
        raise ValueError(f"Unknown installed task: {task_id}") from None


def get_task(task_id: str = REFERENCE_ID, *, data_dir: Path | None = None) -> TaskSpec:
    if task_id.startswith(("knowledge.", "retrieval.")):
        from benchmaxxing.answers import definitions as answer_definitions

        try:
            return answer_definitions()[task_id].public_spec()
        except KeyError:
            raise ValueError(f"Unknown installed answer task: {task_id}") from None
    if task_id.startswith("imported."):
        from benchmaxxing.providers.registry import get_imported, public_task

        if data_dir is None:
            raise ValueError("Imported tasks require the selected data directory")
        return public_task(data_dir, get_imported(data_dir, task_id))
    return _reference_task() if task_id == REFERENCE_ID else get_definition(task_id).public_spec()


def list_tasks(*, data_dir: Path | None = None, include_archived: bool = False) -> list[TaskSpec]:
    from benchmaxxing.answers import definitions as answer_definitions

    result = [get_task(), *[value.public_spec() for value in definitions().values()]]
    result.extend(value.public_spec() for value in answer_definitions().values())
    if data_dir is not None:
        from benchmaxxing.providers.catalog import get_suite
        from benchmaxxing.providers.registry import list_public_tasks

        for task in list_public_tasks(data_dir):
            if task.suite_id is None:
                result.append(task)
                continue
            try:
                suite = get_suite(task.suite_id)
            except ValueError:
                # Legacy imports retain their on-disk evidence, but removed
                # providers must never reappear in the active task catalog.
                continue
            if include_archived or suite.active:
                result.append(task)
    return result


def task_files(task_id: str = REFERENCE_ID) -> dict[str, bytes]:
    if task_id.startswith(("knowledge.", "retrieval.")):
        from benchmaxxing.answers import definitions as answer_definitions
        from benchmaxxing.answers import render_prompt

        answer = answer_definitions()[task_id]
        return {
            "TASK.md": render_prompt(answer).encode(),
            "public_tests.py": b"# Answer-only JSON structure is validated by the controller.\n",
        }
    if task_id == REFERENCE_ID:
        return reference_files()
    definition = get_definition(task_id)
    return {
        "TASK.md": definition.prompt.encode(),
        **{name: content.encode() for name, content in definition.files.items()},
    }


def fixture_solution(task_id: str, variant: str) -> str:
    if task_id.startswith(("knowledge.", "retrieval.")):
        from benchmaxxing.answers import definitions as answer_definitions
        from benchmaxxing.answers import fixture

        return fixture(answer_definitions()[task_id], variant)
    if task_id == REFERENCE_ID:
        return GOOD_SOLUTION if variant == "good" else BAD_SOLUTION
    definition = get_definition(task_id)
    return definition.good_solution if variant == "good" else definition.bad_solution
