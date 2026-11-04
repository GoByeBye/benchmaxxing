"""Explicit reviewed runtime preparation; never called by measured execution."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from benchmaxxing.providers import registry
from benchmaxxing.providers.accounting import capacity_preparation
from benchmaxxing.providers.catalog import require_active_suite
from benchmaxxing.providers.contracts import ImportedTask
from benchmaxxing.providers.operator_scope import check_preparation_scope
from benchmaxxing.providers.preparation import prepare_imported, prepare_suite


@capacity_preparation
def prepare_runtime(
    data_dir: Path, suite_id: str, *, image: str | None = None, pull: bool = False
) -> dict[str, Any]:
    require_active_suite(suite_id)
    check_preparation_scope(data_dir, suite_id)
    tasks = registry.imported_tasks(data_dir, suite_id)
    if not tasks:
        raise ValueError("Fetch or import the suite's source before preparing its runtime")
    if suite_id == "frontend-design" and not image:
        from benchmaxxing.providers.frontend import prepare_frontend_runtime

        return prepare_frontend_runtime(data_dir)
    if image:
        return prepare_suite(data_dir, tasks, image=image, pull=pull)
    pins = [registry.prepared_record(data_dir, task) for task in tasks]
    if all(pins):
        groups: dict[str, list[ImportedTask]] = {}
        for task, pin in zip(tasks, pins, strict=True):
            assert pin is not None
            groups.setdefault(pin["image_id"], []).append(task)
        result: dict[str, Any] = {
            "prepared_tasks": 0,
            "task_ids": [],
            "image_ids": [],
            "errors": {},
        }
        for image_id, group in groups.items():
            checked = prepare_suite(data_dir, group, image=image_id)
            result["prepared_tasks"] += checked["prepared_tasks"]
            result["task_ids"].extend(checked["task_ids"])
            result["image_ids"].extend(checked["image_ids"])
            result["errors"].update(checked["errors"])
        return result
    if suite_id in {"evalplus-humaneval", "evalplus-mbpp", "bigcodebench", "scicode-verified"}:
        from benchmaxxing.providers.coding_sources import prepare_coding_runtime

        return prepare_coding_runtime(data_dir, suite_id)
    if suite_id == "miniwob":
        from benchmaxxing.providers.interactive_preparation import prepare_miniwob_runtime

        return prepare_miniwob_runtime(data_dir)
    if suite_id in {"terminal-bench-2-1", "terminal-bench-4"}:
        from benchmaxxing.environment import EnvironmentError
        from benchmaxxing.providers.interactive_preparation import prepare_harbor_profile

        prepared = []
        images = set()
        errors = {}
        cache: dict[str, Any] = {}
        for task, pin in zip(tasks, pins, strict=True):
            try:
                if pin:
                    record = prepare_imported(data_dir, task, image=pin["image_id"], _cache=cache)
                else:
                    record = prepare_harbor_profile(data_dir, task)
                prepared.append(task.id)
                images.add(record["image_id"])
            except (EnvironmentError, ValueError) as exc:
                errors[task.id] = str(exc)[:2000]
        registry.refresh_prepared_index(data_dir)
        return {
            "prepared_tasks": len(prepared),
            "task_ids": prepared,
            "image_ids": sorted(images),
            "errors": errors,
        }
    if suite_id == "webarena-verified":
        from benchmaxxing.providers.interactive_preparation import webarena_preparation_plan

        plan = webarena_preparation_plan(data_dir)
        return {
            "prepared_tasks": 0,
            "task_ids": [],
            "plan": plan,
            "errors": {suite_id: "Full website data exceeds the configured preparation cap"},
        }
    if suite_id in {"swebench-lite", "swebench-verified"}:
        raise ValueError(
            "SWE-bench requires separately pinned per-instance images and native scripts. "
            "Use providers swe-plan and swe-prepare after inspecting the instance's image size."
        )
    from benchmaxxing.providers.specialized_sources import prepare_specialized_runtime

    check_preparation_scope(data_dir, suite_id)
    return prepare_specialized_runtime(data_dir, suite_id)
