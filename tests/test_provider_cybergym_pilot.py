"""CyberGym pilot planning must not accidentally make metadata-only tasks runnable."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from benchmaxxing.cli import app
from benchmaxxing.providers import registry
from benchmaxxing.providers.contracts import ImportSettings
from benchmaxxing.providers.cybergym_pilot import (
    EXCLUDED_LARGE_ID,
    PILOT_CANDIDATE_BASE_IMAGE,
    PILOT_IDS,
    PILOT_VARIANT,
    PilotPlan,
    VerifierObservation,
    assess_final_poc,
    plan_pilot,
    preflight_local_images,
)
from benchmaxxing.providers.specialized import convert_specialized
from benchmaxxing.providers.specialized_sources import PINS


def _fixture(data_dir: Path, *, malformed: bool = False) -> None:
    revision = PINS["cybergym"]
    source = data_dir / "imports" / "sources" / "cybergym" / revision
    source.mkdir(parents=True)
    (source / ".gitattributes").write_text("fixture\n", encoding="utf-8")
    (source / "README.md").write_text("fixture\n", encoding="utf-8")
    rows = []
    for task_id in [*PILOT_IDS, EXCLUDED_LARGE_ID]:
        provider, number = task_id.split(":", 1)
        level1 = [
            f"data/{provider}/{number}/repo-vul.tar.gz",
            f"data/{provider}/{number}/description.txt",
        ]
        if malformed and task_id == PILOT_IDS[0]:
            level1.append(f"data/{provider}/{number}/repo-fix.tar.gz")
        rows.append(
            {
                "task_id": task_id,
                "vulnerability_description": "Synthetic vulnerability fixture.",
                "task_difficulty": {"level1": level1},
            }
        )
    (source / "tasks.json").write_text(json.dumps(rows), encoding="utf-8")
    manifest = source.parent / f"{revision}.source-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "revision": revision,
                "files": [
                    {"path": ".gitattributes"},
                    {"path": "README.md"},
                    {"path": "tasks.json"},
                ],
            }
        ),
        encoding="utf-8",
    )
    digest = registry.source_hash(source)
    originals = convert_specialized("cybergym", source, revision=revision, source_sha256=digest)
    registry.install(data_dir, source, originals)


def _image_pins(plan: PilotPlan) -> dict[str, str]:
    references = plan.image_references
    return {reference: f"sha256:{index:064x}" for index, reference in enumerate(references, 1)}


def test_nine_task_plan_is_distinct_and_stays_blocked(tmp_path: Path) -> None:
    _fixture(tmp_path)
    plan = plan_pilot(tmp_path)
    assert len(plan.tasks) == 9
    assert plan.runnable is False
    assert plan.variant == PILOT_VARIANT
    assert [task.upstream_id for task in plan.tasks] == list(PILOT_IDS)
    assert EXCLUDED_LARGE_ID not in [task.upstream_id for task in plan.tasks]
    assert plan.candidate_base_image == PILOT_CANDIDATE_BASE_IMAGE
    assert plan.image_references[0] == plan.tasks[0].vulnerable_image
    assert len(set(plan.image_references)) == 18
    assert all(task.original_task_id != task.pilot_task_id for task in plan.tasks)
    assert all(
        registry.prepared_record(tmp_path, task) is None
        for task in registry.imported_tasks(tmp_path, "cybergym")
    )
    assert not any(
        (tmp_path / "imports" / "tasks" / f"{task.pilot_task_id}.json").exists()
        for task in plan.tasks
    )


def test_plan_rejects_patched_source_exposure_and_modified_metadata(tmp_path: Path) -> None:
    _fixture(tmp_path, malformed=True)
    with pytest.raises(ValueError, match="level-1 source declaration"):
        plan_pilot(tmp_path)
    source = tmp_path / "imports" / "sources" / "cybergym" / PINS["cybergym"]
    (source / "README.md").write_text("changed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="provenance differs"):
        plan_pilot(tmp_path)


def test_local_image_preflight_requires_distinct_pinned_linux_images(tmp_path: Path) -> None:
    _fixture(tmp_path)
    plan = plan_pilot(tmp_path)
    pins = _image_pins(plan)

    def inspect(reference: str) -> dict[str, object]:
        return {"Id": pins[reference], "Os": "linux", "Size": 100_000_000}

    result = preflight_local_images(tmp_path, plan, pins, inspect_image=inspect)
    assert result.total_image_size_bytes == 1_800_000_000
    assert result.within_storage_cap is True
    assert result.runnable is False
    assert (
        result.image_id_for(plan.tasks[0].vulnerable_image) == pins[plan.tasks[0].vulnerable_image]
    )
    with pytest.raises(ValueError, match="exactly one"):
        preflight_local_images(tmp_path, plan, {}, inspect_image=inspect)
    duplicate = dict(pins)
    duplicate[plan.tasks[0].fixed_image] = duplicate[plan.tasks[0].vulnerable_image]
    with pytest.raises(ValueError, match="distinct"):
        preflight_local_images(tmp_path, plan, duplicate, inspect_image=inspect)
    with pytest.raises(ValueError, match="differs from its Linux pin"):
        preflight_local_images(
            tmp_path,
            plan,
            pins,
            inspect_image=lambda reference: {"Id": pins[reference], "Os": "windows", "Size": 10},
        )
    with pytest.raises(ValueError, match="150 GB ceiling"):
        preflight_local_images(
            tmp_path,
            plan,
            pins,
            inspect_image=lambda reference: {
                "Id": pins[reference],
                "Os": "linux",
                "Size": 10_000_000_000,
            },
        )
    registry.save_settings(tmp_path, ImportSettings(storage_limit_bytes=1_000_000_000))
    with pytest.raises(ValueError, match="configured total storage cap"):
        preflight_local_images(tmp_path, plan, pins, inspect_image=inspect)


def test_final_poc_requires_vulnerable_only_crash() -> None:
    digest = "a" * 64
    vul = VerifierObservation(
        image_id="sha256:" + "b" * 64,
        outcome="crash",
        exit_code=139,
        crash_evidence="signal",
    )
    fixed = VerifierObservation(image_id="sha256:" + "c" * 64, outcome="clean_exit", exit_code=0)
    passed = assess_final_poc(
        upstream_id=PILOT_IDS[0], submission_sha256=digest, vulnerable=vul, fixed=fixed
    )
    assert (passed.assessment_status, passed.passed, passed.score) == ("graded", True, 1.0)
    with pytest.raises(ValueError, match="score disagrees"):
        type(passed).model_validate({**passed.model_dump(), "score": 0.0})
    failed = assess_final_poc(
        upstream_id=PILOT_IDS[0],
        submission_sha256=digest,
        vulnerable=vul,
        fixed=VerifierObservation(
            image_id=fixed.image_id,
            outcome="crash",
            exit_code=139,
            crash_evidence="signal",
        ),
    )
    assert (failed.assessment_status, failed.passed, failed.score) == ("graded", False, 0.0)
    ungraded = assess_final_poc(
        upstream_id=PILOT_IDS[0],
        submission_sha256=digest,
        vulnerable=vul,
        fixed=VerifierObservation(image_id=fixed.image_id, outcome="timeout"),
    )
    assert (ungraded.assessment_status, ungraded.passed, ungraded.score) == ("ungraded", None, None)
    for non_crash_code in (0, 124, 137, 300):
        with pytest.raises(ValueError, match="target signal or sanitizer failure"):
            VerifierObservation(
                image_id=vul.image_id,
                outcome="crash",
                exit_code=non_crash_code,
                crash_evidence="signal",
            )
    with pytest.raises(ValueError, match="target signal or sanitizer failure"):
        VerifierObservation(image_id=vul.image_id, outcome="crash", exit_code=1)
    assert (
        VerifierObservation(image_id=vul.image_id, outcome="non_crash_exit", exit_code=1).outcome
        == "non_crash_exit"
    )
    assert (
        VerifierObservation(image_id=vul.image_id, outcome="timeout", exit_code=124).outcome
        == "timeout"
    )
    assert (
        VerifierObservation(
            image_id=vul.image_id, outcome="infrastructure_error", exit_code=137
        ).outcome
        == "infrastructure_error"
    )
    with pytest.raises(ValueError, match="distinct images"):
        assess_final_poc(
            upstream_id=PILOT_IDS[0],
            submission_sha256=digest,
            vulnerable=vul,
            fixed=VerifierObservation(image_id=vul.image_id, outcome="clean_exit", exit_code=0),
        )


def test_cli_plan_is_read_only(tmp_path: Path) -> None:
    _fixture(tmp_path)
    before = sorted((tmp_path / "imports").rglob("*"))
    response = CliRunner().invoke(app, ["providers", "cybergym-plan", "--data-dir", str(tmp_path)])
    assert response.exit_code == 0, response.output
    assert json.loads(response.output)["plan"]["runnable"] is False
    assert sorted((tmp_path / "imports").rglob("*")) == before
