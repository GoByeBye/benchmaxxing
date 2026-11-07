"""Data-only interchange between upstream importers and the common controller.

Converters do not download dependencies or execute upstream/candidate code. Their
recipes describe operations that the preparation and sandbox layers must validate.
An imported record is not runnable merely because conversion succeeded.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import Field, model_validator

from benchmaxxing.contracts import Contract


def relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or "\0" in value
        or ":" in value
        or path.is_absolute()
        or ".." in path.parts
        or str(path) != value
        or value == "."
    ):
        raise ValueError("Expected a normalized relative asset path")
    return value


class SourceAsset(Contract):
    """A regular source file to copy into an explicitly separated bundle area."""

    source: str
    target: str
    scope: Literal["candidate", "grader", "runtime"]
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def paths(self) -> SourceAsset:
        relative_path(self.source)
        relative_path(self.target)
        return self


class NativeRecipe(Contract):
    """Runtime requirements, not host shell commands or permission grants."""

    family: str
    image: str | None = None
    # Optional post-submission materialization, executed before freezing artifacts.
    candidate_command: list[str] = Field(default_factory=list)
    grader_command: list[str] = Field(default_factory=list)
    grader_result_path: str | None = None
    grader_format: str = "json"
    workdir: str = "/workspace"
    network: Literal["none", "task-local", "external"] = "none"
    # The final value only decodes historical imported records; no runtime uses it.
    tools: Literal["workspace", "browser", "response", "blender"] = "workspace"
    settings: dict[str, Any] = Field(default_factory=dict)
    requirements: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)


class ImportedTask(Contract):
    schema_version: Literal[1] = 1
    suite_id: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    upstream_id: str = Field(min_length=1, max_length=1024)
    revision: str = Field(min_length=1, max_length=256)
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    title: str = Field(min_length=1, max_length=300)
    prompt: str = Field(min_length=1, max_length=1000000)
    category: str
    difficulty: Literal["easy", "medium", "hard"] = "hard"
    variant: str = "benchmaxxing-offline-30m-v1"
    public_files: dict[str, str] = Field(default_factory=dict)
    private_files: dict[str, str] = Field(default_factory=dict)
    assets: list[SourceAsset] = Field(default_factory=list)
    submission_file: str = "solution.py"
    submission_kind: Literal["python", "text", "json", "patch", "workspace", "binary"] = "python"
    recipe: NativeRecipe
    source_urls: list[str] = Field(default_factory=list)
    licenses: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    check_count: int | None = Field(default=None, ge=1)
    comparison_notes: list[str] = Field(default_factory=list)
    good_solution: str | None = None
    bad_solution: str | None = None

    @model_validator(mode="after")
    def paths(self) -> ImportedTask:
        relative_path(self.submission_file)
        for names in (self.public_files, self.private_files):
            for name in names:
                relative_path(name)
        for scope, names in (("candidate", self.public_files), ("grader", self.private_files)):
            targets = [asset.target for asset in self.assets if asset.scope == scope]
            if len(targets) != len(set(targets)) or set(targets).intersection(names):
                raise ValueError("Duplicate asset destination")
        return self

    @property
    def id(self) -> str:
        identity = json.dumps(
            [self.suite_id, self.upstream_id, self.revision, self.variant, self.source_sha256]
        )
        return f"imported.{self.suite_id}-{hashlib.sha256(identity.encode()).hexdigest()[:20]}"

    @property
    def task_hash(self) -> str:
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True, allow_nan=False)
        return hashlib.sha256(payload.encode()).hexdigest()


class SuiteDescriptor(Contract):
    id: str
    name: str
    category: str
    upstream_url: str
    version: str
    published_size: str
    description: str
    license: str
    offline: Literal["supported-path", "adaptation", "conditional", "unavailable"]
    requirements: list[str] = Field(default_factory=list)
    source_url: str | None = None
    source_format: str = "directory"
    active: bool = True
    origin: Literal["upstream", "original"] = "upstream"


class SuiteStatus(Contract):
    suite: SuiteDescriptor
    state: Literal["not_installed", "imported", "prepared", "partial", "blocked"]
    imported_tasks: int = 0
    prepared_tasks: int = 0
    blockers: list[str] = Field(default_factory=list)
    revisions: list[str] = Field(default_factory=list)
    source_bytes: int = 0


class ImportRequest(Contract):
    suite_id: str
    source_path: str
    revision: str
    variant: str | None = None


class PreparationRequest(Contract):
    suite_id: str
    image: str | None = None
    pull: bool = False


class FetchRequest(Contract):
    suite_id: str


class SWEPlanRequest(Contract):
    task_id: str


class SWEPreparationRequest(SWEPlanRequest):
    reservation_bytes: int = Field(ge=1_000_000_000, le=250_000_000_000)
    pull: bool = False


class CyberGymPreparationRequest(Contract):
    upstream_id: str = Field(pattern=r"^(?:arvo|oss-fuzz):[0-9]+$")
    source_path: str = Field(min_length=1, max_length=4096)
    candidate_image: Literal["python:3.13-slim"] = "python:3.13-slim"
    vulnerable_image_id: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    fixed_image_id: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")


class ImportSettings(Contract):
    schema_version: Literal[1] = 1
    storage_limit_bytes: int = Field(default=250_000_000_000, ge=1_000_000_000, le=250_000_000_000)
    reserve_free_bytes: int = Field(default=10_000_000_000, ge=0)
    cache_dir: str | None = None


class PreparationJob(Contract):
    id: str
    suite_id: str
    operation: Literal["import", "prepare", "fetch"]
    status: Literal["queued", "running", "completed", "failed", "interrupted"]
    created_at: str
    updated_at: str
    error: str | None = None
    result: dict[str, Any] | None = None
