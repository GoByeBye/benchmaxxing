"""CyberGym verifier boundary checks without pulling or running Docker images."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from benchmaxxing.api import create_app
from benchmaxxing.cli import app
from benchmaxxing.contracts import RunSpec
from benchmaxxing.environment import (
    MANAGED_LABEL,
    RUN_LABEL,
    WORKSPACE_LABEL,
    EnvironmentError,
    PreparationError,
)
from benchmaxxing.providers import downloads, registry, runner
from benchmaxxing.providers.contracts import ImportedTask, NativeRecipe, PreparationJob
from benchmaxxing.providers.cybergym_pilot import (
    PILOT_VARIANT,
    PilotTaskPlan,
    VerifierObservation,
)
from benchmaxxing.providers.cybergym_prepare import (
    _check_source_archive,
    _source_file,
    prepare_pilot_task,
)
from benchmaxxing.providers.cybergym_runtime import CyberGymEnvironment, _observe, verified_pins
from benchmaxxing.storage import Store

VUL = "sha256:" + "1" * 64
FIX = "sha256:" + "2" * 64
TOOLS = "sha256:" + "3" * 64


def _task() -> Any:
    return SimpleNamespace(
        suite_id="cybergym",
        variant="benchmaxxing-offline-cybergym-level1-9task-v1",
        upstream_id="arvo:1065",
        recipe=SimpleNamespace(family="cybergym-pilot"),
        submission_kind="binary",
        submission_file="poc",
    )


def _receipt() -> dict[str, Any]:
    return {
        "image_id": TOOLS,
        "cybergym_pilot": {
            "upstream_id": "arvo:1065",
            "vulnerable_image": "n132/arvo:1065-vul",
            "vulnerable_image_id": VUL,
            "fixed_image": "n132/arvo:1065-fix",
            "fixed_image_id": FIX,
        },
    }


def test_pins_are_rechecked_and_distinct() -> None:
    with patch(
        "benchmaxxing.providers.cybergym_runtime._inspect_image",
        side_effect=lambda reference: {
            "Id": VUL if reference.endswith("-vul") else FIX,
            "Os": "linux",
        },
    ):
        assert verified_pins(_task(), _receipt()) == (VUL, FIX)
        changed = _receipt()
        changed["cybergym_pilot"]["fixed_image_id"] = VUL
        with pytest.raises(PreparationError):
            verified_pins(_task(), changed)


def _environment() -> Any:
    return SimpleNamespace(
        data_dir=Path("/tmp/cybergym-test"),
        run_id="run-test",
        budgets=SimpleNamespace(
            memory_mb=512, cpus=1, pids=64, output_bytes=65536, artifact_bytes=1_048_576
        ),
        cancel_event=None,
        _remaining=lambda: 120.0,
    )


def _docker_result(*, code: int = 0, stdout: str = "", stderr: str = "") -> dict[str, Any]:
    return {"exit_code": code, "stdout": stdout, "stderr": stderr}


@pytest.mark.parametrize(
    ("code", "target_output", "expected", "crash_evidence"),
    [
        (0, "", "clean_exit", None),
        (1, "ordinary failure", "non_crash_exit", None),
        (1, "==42==ERROR: AddressSanitizer: heap-buffer-overflow", "crash", "sanitizer"),
        (139, "", "crash", "signal"),
        (124, "", "timeout", None),
        (137, "", "timeout", None),
        (300, "", "infrastructure_error", None),
        (125, "", "infrastructure_error", None),
    ],
)
def test_verifier_is_networkless_and_classifies_exit_codes(
    code: int, target_output: str, expected: str, crash_evidence: str | None
) -> None:
    calls: list[list[str]] = []
    captured: list[bytes] = []

    def fake_docker(args: list[str], **kwargs: Any) -> dict[str, Any]:
        calls.append(args)
        if args[:2] == ["container", "inspect"]:
            return _docker_result(
                stdout=json.dumps(
                    {
                        MANAGED_LABEL: "1",
                        WORKSPACE_LABEL: "fake-workspace",
                        RUN_LABEL: "run-test",
                    }
                )
            )
        if args[:2] == ["exec", "-i"]:
            captured.append(kwargs["input_bytes"])
            return _docker_result()
        if args[0] == "exec":
            return _docker_result(code=code, stdout=target_output)
        return _docker_result()

    with (
        patch("benchmaxxing.providers.cybergym_runtime._docker", side_effect=fake_docker),
        patch(
            "benchmaxxing.providers.cybergym_runtime.workspace_id", return_value="fake-workspace"
        ),
    ):
        observation, evidence = _observe(
            _environment(), VUL, "/bin/arvo", b"candidate-poc", on_output=None
        )
    assert observation.outcome == expected
    assert observation.crash_evidence == crash_evidence
    assert evidence["exit_code"] == code
    create = calls[0]
    assert create[0] == "create"
    assert create[create.index("--network") + 1] == "none"
    assert "--read-only" in create
    assert "--cap-drop" in create
    assert create[create.index("--pull") + 1] == "never"
    assert VUL in create
    assert not any("-fix" in item for item in create)
    assert captured == [b"candidate-poc"]
    transfer = next(args for args in calls if args[:2] == ["exec", "-i"])
    assert transfer[-1] == "umask 077; cat > /tmp/poc"
    assert not any(args[0] == "cp" for args in calls)
    assert calls[-1][:2] == ["rm", "-f"]


def test_cleanup_never_removes_unowned_container() -> None:
    calls: list[list[str]] = []

    def fake_docker(args: list[str], **_kwargs: Any) -> dict[str, Any]:
        calls.append(args)
        if args[:2] == ["container", "inspect"]:
            return _docker_result(stdout=json.dumps({MANAGED_LABEL: "1"}))
        return _docker_result()

    with patch("benchmaxxing.providers.cybergym_runtime._docker", side_effect=fake_docker):
        with pytest.raises(EnvironmentError, match="ownership labels"):
            _observe(_environment(), VUL, "/bin/arvo", b"tar", on_output=None)
    assert not any(args[:2] == ["rm", "-f"] for args in calls)


def test_verifier_rejects_oversized_poc_and_unreviewed_command() -> None:
    with pytest.raises(EnvironmentError, match="byte limit"):
        _observe(_environment(), VUL, "/bin/arvo", b"x" * 1_048_577, on_output=None)
    with pytest.raises(PreparationError, match="reviewed upstream command"):
        _observe(_environment(), VUL, "/bin/bash -c evil", b"poc", on_output=None)


def test_changed_level1_source_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "data" / "arvo" / "1065" / "description.txt"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"substituted source")
    with pytest.raises(PreparationError, match="upstream SHA256"):
        _source_file(tmp_path, "data/arvo/1065/description.txt")


def test_source_archive_rejects_git_history(tmp_path: Path) -> None:
    archive_path = tmp_path / "source.tar.gz"
    with tarfile.open(archive_path, mode="w:gz") as archive:
        entry = tarfile.TarInfo("src-vul/.git/config")
        entry.size = 1
        archive.addfile(entry, io.BytesIO(b"x"))
    with pytest.raises(PreparationError, match="unsafe or hidden"):
        _check_source_archive(archive_path)


@pytest.mark.parametrize(
    ("fixed_outcome", "expected_status", "expected_passed"),
    [("clean_exit", "graded", True), ("crash", "graded", False), ("timeout", "ungraded", None)],
)
def test_final_poc_uses_two_pinned_images_only(
    fixed_outcome: str, expected_status: str, expected_passed: bool | None
) -> None:
    environment = object.__new__(CyberGymEnvironment)
    environment.data_dir = Path("/tmp/cybergym-test")
    environment.task = _task()
    environment.budgets = SimpleNamespace(artifact_bytes=1_048_576)
    calls: list[str] = []

    def fake_observe(
        _environment: Any, image_id: str, _command: str, _poc: bytes, **_kwargs: Any
    ) -> tuple[VerifierObservation, dict[str, Any]]:
        calls.append(image_id)
        outcome = "crash" if image_id == VUL else fixed_outcome
        code = 139 if outcome == "crash" else 0 if outcome == "clean_exit" else None
        return VerifierObservation(
            image_id=image_id,
            outcome=outcome,
            exit_code=code,
            crash_evidence="signal" if outcome == "crash" else None,
        ), {}

    with (
        patch.object(CyberGymEnvironment, "_check_frozen"),
        patch("benchmaxxing.providers.cybergym_runtime.preparation_info", return_value={}),
        patch("benchmaxxing.providers.cybergym_runtime.verified_pins", return_value=(VUL, FIX)),
        patch("benchmaxxing.providers.cybergym_runtime._observe", side_effect=fake_observe),
    ):
        result = environment.grade({"workspace/poc": b"final-poc"})
    assert calls == [VUL, FIX]
    assert result["assessment_status"] == expected_status
    assert result["passed"] is expected_passed
    assert result["submission_scope"] == "final"


@pytest.mark.parametrize(
    ("outcome", "status"),
    [("timeout", "budget_exhausted"), ("infrastructure_error", "infrastructure_error")],
)
def test_ungraded_verifier_preserves_evidence_and_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str, status: str
) -> None:
    from benchmaxxing.providers import cybergym_runtime, preparation

    task = ImportedTask(
        suite_id="cybergym",
        upstream_id="arvo:1065",
        revision="fixture-revision",
        source_sha256="a" * 64,
        title="Fake CyberGym task",
        prompt="Submit a PoC.",
        category="cybersecurity",
        variant="benchmaxxing-offline-cybergym-level1-9task-v1",
        submission_file="poc",
        submission_kind="binary",
        good_solution="final-poc",
        recipe=NativeRecipe(family="cybergym-pilot"),
    )
    registry.install(tmp_path, tmp_path, [task])

    class FakeEnvironment:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self.files: dict[str, bytes] = {}

        def start(self) -> None:
            pass

        def write_file(self, path: str, content: str) -> None:
            self.files[f"workspace/{path}"] = content.encode()

        def freeze(self) -> dict[str, bytes]:
            return dict(self.files)

        def close(self) -> None:
            pass

        def grade(self, _frozen: dict[str, bytes]) -> dict[str, Any]:
            return {
                "assessment_status": "ungraded",
                "passed": None,
                "score": None,
                "reason": "One verifier did not complete",
                "vulnerable": {"outcome": outcome},
                "fixed": {"outcome": "clean_exit"},
                "checks": [],
                "evidence": {"track": "cybergym-offline-level1-final-poc"},
            }

    monkeypatch.setattr(cybergym_runtime, "CyberGymEnvironment", FakeEnvironment)
    monkeypatch.setattr(preparation, "preparation_info", lambda *_args: {"image_id": TOOLS})
    store = Store(tmp_path)
    run = store.create_run(RunSpec(task_id=task.id))
    result = runner.execute_imported(store, run.id)
    assert result.status == status
    assert result.score is None
    assert result.metadata["assessment_status"] == "ungraded"
    assert {item.name for item in store.artifacts(run.id)} >= {
        "workspace/poc",
        "native-grading-evidence.json",
    }
    kinds = [item.kind for item in store.events(run.id)]
    assert "assessment.ungraded" in kinds
    assert "assessment.pending" not in kinds


def test_pilot_preparation_accounts_for_both_verifier_images(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from benchmaxxing.providers import cybergym_prepare

    data_dir = tmp_path / "data-dir"
    source_dir = tmp_path / "source"
    archive_path = source_dir / "data" / "arvo" / "1065" / "repo-vul.tar.gz"
    archive_path.parent.mkdir(parents=True)
    with tarfile.open(archive_path, mode="w:gz") as archive:
        entry = tarfile.TarInfo("src-vul/file")
        entry.size = 1
        archive.addfile(entry, io.BytesIO(b"x"))
    description = archive_path.parent / "description.txt"
    description.write_text("Find a crash", encoding="utf-8")
    monkeypatch.setattr(
        cybergym_prepare,
        "_SOURCE_METADATA",
        {
            "data/arvo/1065/repo-vul.tar.gz": (
                archive_path.stat().st_size,
                hashlib.sha256(archive_path.read_bytes()).hexdigest(),
            ),
            "data/arvo/1065/description.txt": (
                description.stat().st_size,
                hashlib.sha256(description.read_bytes()).hexdigest(),
            ),
        },
    )
    original = ImportedTask(
        suite_id="cybergym",
        upstream_id="arvo:1065",
        revision="fixture-revision",
        source_sha256="a" * 64,
        title="Pilot source",
        prompt="Find a bug.",
        category="cybersecurity",
        submission_file="poc",
        submission_kind="binary",
        recipe=NativeRecipe(family="cybergym"),
    )
    registry.install(data_dir, source_dir, [original])
    pilot_id = ImportedTask.model_validate(
        {**original.model_dump(mode="json"), "variant": PILOT_VARIANT}
    ).id
    plan = SimpleNamespace(
        source_revision="fixture-revision",
        source_sha256="a" * 64,
        tasks=[
            PilotTaskPlan(
                upstream_id="arvo:1065",
                original_task_id=original.id,
                pilot_task_id=pilot_id,
                level1_source_paths=[
                    "data/arvo/1065/repo-vul.tar.gz",
                    "data/arvo/1065/description.txt",
                ],
                vulnerable_image="n132/arvo:1065-vul",
                fixed_image="n132/arvo:1065-fix",
            )
        ],
    )
    monkeypatch.setattr(cybergym_prepare, "plan_pilot", lambda *_args: plan)
    monkeypatch.setattr(cybergym_prepare, "check_docker_capacity", lambda *_args: {})
    monkeypatch.setattr(cybergym_prepare, "_candidate_probe", lambda *_args: "3.13")
    candidate_ref = "benchmaxxing/cybergym-tools:fixture-arvo-1065"
    candidate = {"Id": TOOLS, "Os": "linux", "Size": 100}
    base = {"Id": "sha256:" + "4" * 64, "RepoDigests": ["python@sha256:" + "5" * 64]}
    builds: list[str] = []

    def fake_build(_base: str, task_id: str) -> tuple[str, dict[str, Any], dict[str, Any]]:
        builds.append(task_id)
        return candidate_ref, candidate, base

    monkeypatch.setattr(
        cybergym_prepare,
        "_build_clean_tools_image",
        fake_build,
    )

    def fake_image(reference: str, _pin: str | None = None) -> dict[str, Any]:
        if reference == candidate_ref:
            return candidate
        vulnerable = reference.endswith("-vul")
        return {
            "Id": VUL if vulnerable else FIX,
            "Os": "linux",
            "Size": 120 if vulnerable else 130,
            "RepoDigests": ["n132/arvo@sha256:" + ("6" if vulnerable else "7") * 64],
        }

    monkeypatch.setattr(
        cybergym_prepare,
        "_image",
        fake_image,
    )
    result = prepare_pilot_task(data_dir, "arvo:1065", source_dir, "python:3.13-slim", VUL, FIX)
    assert result["task_id"] == pilot_id
    task = registry.get_imported(data_dir, pilot_id)
    receipt = registry.prepared_record(data_dir, task)
    assert receipt is not None
    assert receipt["cybergym_pilot"]["vulnerable_image_id"] == VUL
    assert receipt["cybergym_pilot"]["fixed_image_id"] == FIX
    assert downloads.image_bytes(data_dir) == 350
    assert {asset.scope for asset in task.assets} == {"candidate"}
    assert not task.private_files
    again = prepare_pilot_task(data_dir, "arvo:1065", source_dir, "python:3.13-slim", VUL, FIX)
    assert again["copied_bytes"] == 0
    assert builds == ["arvo:1065"]
    (registry.root(data_dir) / "prepared" / f"{task.id}.json").unlink()
    removed: list[tuple[str, str]] = []
    monkeypatch.setattr(
        cybergym_prepare,
        "_candidate_probe",
        lambda *_args: (_ for _ in ()).throw(PreparationError("probe failed")),
    )
    monkeypatch.setattr(
        cybergym_prepare,
        "_remove_failed_candidate_image",
        lambda reference, image_id: removed.append((reference, image_id)),
    )
    with pytest.raises(PreparationError, match="probe failed"):
        prepare_pilot_task(data_dir, "arvo:1065", source_dir, "python:3.13-slim", VUL, FIX)
    assert removed == [(candidate_ref, TOOLS)]


def test_cybergym_prepare_cli_and_attached_api_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from benchmaxxing import provider_cli

    captured: dict[str, Any] = {}

    def record(directory: Path, name: str, payload: dict[str, Any]) -> None:
        captured.update(directory=directory, name=name, payload=payload)

    monkeypatch.setattr(provider_cli, "_operation", record)
    result = CliRunner().invoke(
        app,
        [
            "providers",
            "cybergym-prepare",
            "arvo:1065",
            str(tmp_path),
            "--vulnerable-image-id",
            VUL,
            "--fixed-image-id",
            FIX,
            "--data-dir",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["name"] == "cybergym-prepare"
    assert captured["payload"]["candidate_image"] == "python:3.13-slim"
    assert "/api/v1/providers/cybergym-prepare" in create_app(tmp_path).openapi()["paths"]


def test_cybergym_prepare_attached_controller_submits_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from benchmaxxing.providers import cybergym_prepare, jobs

    captured: dict[str, Any] = {}

    def fake_prepare(*args: Any) -> dict[str, Any]:
        captured["args"] = args
        return {"task_id": "pilot-fixture"}

    def fake_submit(data_dir: Path, suite_id: str, operation: str, action: Any) -> PreparationJob:
        captured.update(data_dir=data_dir, suite_id=suite_id, operation=operation)
        return PreparationJob(
            id="a" * 32,
            suite_id=suite_id,
            operation="prepare",
            status="completed",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
            result=action(),
        )

    monkeypatch.setattr(cybergym_prepare, "prepare_pilot_task", fake_prepare)
    monkeypatch.setattr(jobs, "submit_job", fake_submit)
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1:8123") as client:
        token = client.get("/api/v1/session").json()["token"]
        response = client.post(
            "/api/v1/providers/cybergym-prepare",
            json={
                "upstream_id": "arvo:1065",
                "source_path": str(tmp_path / "source"),
                "vulnerable_image_id": VUL,
                "fixed_image_id": FIX,
            },
            headers={"X-Benchmaxxing-Token": token},
        )
    assert response.status_code == 202, response.text
    assert response.json()["result"] == {"task_id": "pilot-fixture"}
    assert captured["suite_id"] == "cybergym"
    assert captured["args"][1:] == (
        "arvo:1065",
        (tmp_path / "source").resolve(),
        "python:3.13-slim",
        VUL,
        FIX,
    )
