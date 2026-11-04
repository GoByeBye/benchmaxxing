"""Versioned public contracts shared by the CLI, engine and HTTP API."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Budgets(Contract):
    max_turns: int = Field(default=24, ge=1, le=256)
    wall_seconds: float = Field(default=1800, ge=1, le=86400)
    command_seconds: float = Field(default=10, ge=0.1, le=300)
    output_bytes: int = Field(default=65536, ge=1024, le=1048576)
    artifact_bytes: int = Field(default=1048576, ge=1024, le=16777216)
    memory_mb: int = Field(default=256, ge=64, le=32768)
    cpus: float = Field(default=1, ge=0.1, le=32)
    pids: int = Field(default=64, ge=16, le=512)


class Generation(Contract):
    temperature: float = Field(default=0, ge=0, le=2)
    max_tokens: int = Field(default=2048, ge=1, le=32768)
    seed: int | None = None


class InferenceProfile(Contract):
    mode: Literal["offline", "api"] = "offline"
    base_url: str = "http://127.0.0.1:8080/v1"
    model: str = Field(min_length=1)
    api_key_env: str | None = None

    @model_validator(mode="after")
    def validate_endpoint(self) -> "InferenceProfile":
        # Import lazily: the adapter itself consumes these public contracts.
        from benchmaxxing.model import ProfileError, validate_profile

        try:
            self.base_url = validate_profile(self)
        except ProfileError as exc:
            raise ValueError(str(exc)) from exc
        return self


class RunSpec(Contract):
    schema_version: Literal[1] = 1
    task_id: str = Field(
        default="python.interval-merge", pattern=r"^[a-z][a-z0-9_-]*\.[a-z0-9-]+$", max_length=100
    )
    participant: Literal["scripted", "openai"] = "scripted"
    scripted_variant: Literal["good", "bad", "refuse"] = "good"
    inference: InferenceProfile | None = None
    generation: Generation = Field(default_factory=Generation)
    budgets: Budgets = Field(default_factory=Budgets)

    @model_validator(mode="after")
    def require_inference(self) -> "RunSpec":
        if self.participant == "openai" and self.inference is None:
            raise ValueError("An inference profile is required for the openai participant")
        return self


RunStatus = Literal[
    "queued",
    "running",
    "passed",
    "failed",
    "infrastructure_error",
    "unsupported",
    "cancelled",
    "budget_exhausted",
    "interrupted",
]
TERMINAL_STATUSES: set[str] = {
    "passed",
    "failed",
    "infrastructure_error",
    "unsupported",
    "cancelled",
    "budget_exhausted",
    "interrupted",
}


class CheckResult(Contract):
    name: str
    passed: bool
    detail: str = ""


class ScoreCard(Contract):
    schema_version: Literal[1] = 1
    grader_version: str = "interval-blackbox/1"
    passed: bool
    score: float = Field(ge=0, le=1)
    checks: list[CheckResult] = Field(default_factory=list)


class RunRecord(Contract):
    id: str
    status: RunStatus
    spec: RunSpec
    created_at: str
    updated_at: str
    score: ScoreCard | None = None
    error: str | None = None
    refusal: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RunEvent(Contract):
    seq: int
    run_id: str
    timestamp: str
    kind: str
    data: dict[str, Any]


class ArtifactRecord(Contract):
    id: str
    run_id: str
    name: str
    media_type: str
    size: int
    sha256: str


class TaskSpec(Contract):
    schema_version: Literal[1] = 1
    id: str = "python.interval-merge"
    version: str = "1.0.0"
    title: str = "Repair interval merging"
    category: str = "programming"
    description: str
    task_hash: str
    image: str
    kind: Literal["python", "json"] = "python"
    submission_file: str = "solution.py"
    difficulty: Literal["easy", "medium", "hard"] = "easy"
    tags: list[str] = Field(default_factory=list)
    check_count: int = Field(default=8, ge=1)
    rubric: str = ""


class RunDetail(Contract):
    run: RunRecord
    artifacts: list[ArtifactRecord]


class RunActivity(Contract):
    id: str
    status: Literal["queued", "running"]
    task_id: str
    task_title: str
    category: str
    participant: str
    model: str | None = None
    created_at: str
    started_at: str | None = None
    updated_at: str
    phase: str = "queued"
    turn: int = Field(default=0, ge=0)
    max_turns: int
    wall_seconds: float
    elapsed_seconds: float = Field(default=0, ge=0)
    queue_position: int | None = None
    last_event_at: str | None = None
    last_event_seq: int = Field(default=0, ge=0)
    cancellation_requested: bool = False
    last_tool: str | None = None
    checks_done: int = Field(default=0, ge=0)
    checks_total: int = Field(default=0, ge=0)


class ActivitySnapshot(Contract):
    schema_version: Literal[1] = 1
    server_time: str
    runs: list[RunActivity]


class SessionInfo(Contract):
    token: str
    version: str


class DoctorReport(Contract):
    ready: bool
    checks: dict[str, Any]
