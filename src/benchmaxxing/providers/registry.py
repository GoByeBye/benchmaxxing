"""Immutable imported records and content-addressed source assets.

Installation copies data, never imports source Python or runs source shell scripts.
Only explicitly prepared images can make an imported task runnable.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path, PureWindowsPath
from typing import Any

from filelock import FileLock

from benchmaxxing.contracts import TaskSpec
from benchmaxxing.providers.catalog import SUITES, get_suite
from benchmaxxing.providers.contracts import ImportedTask, ImportSettings, SuiteStatus
from benchmaxxing.providers.difficulty import display_difficulty


def root(data_dir: Path) -> Path:
    return Path(data_dir) / "imports"


def _atomic_json(path: Path, value: Any) -> None:
    from benchmaxxing.providers.accounting import before_file_write, note_file

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    payload = (json.dumps(value, indent=2, allow_nan=False) + "\n").encode("utf-8")
    before_file_write(path, len(payload), value=value)
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, path)
        note_file(path)
    finally:
        temporary.unlink(missing_ok=True)


def settings(data_dir: Path) -> ImportSettings:
    path = root(data_dir) / "settings.json"
    if path.is_file():
        return ImportSettings.model_validate_json(path.read_bytes())
    return ImportSettings()


def save_settings(data_dir: Path, value: ImportSettings) -> None:
    from benchmaxxing.providers.locking import preparation_scope

    with preparation_scope(data_dir):
        current_assets = asset_root(data_dir).resolve()
        requested_assets = (
            Path(value.cache_dir).resolve() / "assets"
            if value.cache_dir
            else root(data_dir) / "assets"
        ).resolve()
        if current_assets != requested_assets and (
            any(_records(data_dir)) or (current_assets.is_dir() and any(current_assets.iterdir()))
        ):
            raise ValueError(
                "Installed assets use the current cache. Changing its directory requires "
                "an explicit offline migration; choose a new empty data directory instead."
            )
        _atomic_json(root(data_dir) / "settings.json", value.model_dump(mode="json"))


def asset_root(data_dir: Path) -> Path:
    configured = settings(data_dir).cache_dir
    return Path(configured).resolve() / "assets" if configured else root(data_dir) / "assets"


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def source_hash(source: Path) -> str:
    """Fingerprint source files in one order on Windows and Linux.

    Windows-style component ordering preserves hashes already issued by Windows
    installations. The text tie-breaker makes case-colliding POSIX paths stable.
    """
    if source.is_symlink():
        raise ValueError("Source symlinks are not supported")
    if source.is_file():
        return file_hash(source)
    if not source.is_dir():
        raise ValueError("Prepared source does not exist")
    digest = hashlib.sha256()

    def order(path: Path) -> tuple[PureWindowsPath, str]:
        relative = path.relative_to(source)
        return PureWindowsPath(*relative.parts), relative.as_posix()

    for path in sorted(source.rglob("*"), key=order):
        if ".git" in path.relative_to(source).parts:
            continue
        if path.is_symlink():
            raise ValueError("Source trees must not contain symlinks")
        if path.is_file():
            digest.update(path.relative_to(source).as_posix().encode() + b"\0")
            digest.update(bytes.fromhex(file_hash(path)))
    return digest.hexdigest()


def _records(data_dir: Path) -> Iterator[Path]:
    directory = root(data_dir) / "tasks"
    if directory.is_dir():
        yield from sorted(directory.glob("imported.*.json"))


def get_imported(data_dir: Path, task_id: str) -> ImportedTask:
    if not task_id.startswith("imported.") or any(c in task_id for c in "/\\:"):
        raise ValueError("Invalid imported task ID")
    path = root(data_dir) / "tasks" / f"{task_id}.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        task = ImportedTask.model_validate(value["task"])
        if task.id != task_id or task.task_hash != value["sha256"]:
            raise ValueError("Imported task identity or hash changed; import the source again")
        return task
    except FileNotFoundError:
        raise ValueError(
            "Imported task is unavailable; import and prepare its source first"
        ) from None


def imported_tasks(data_dir: Path, suite_id: str | None = None) -> list[ImportedTask]:
    result = []
    for path in _records(data_dir):
        if suite_id is not None and not path.stem.startswith(f"imported.{suite_id}-"):
            continue
        task = get_imported(data_dir, path.stem)
        if suite_id is None or task.suite_id == suite_id:
            result.append(task)
    return result


def prepared_record(data_dir: Path, task: ImportedTask) -> dict[str, Any] | None:
    path = root(data_dir) / "prepared" / f"{task.id}.json"
    try:
        value: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        if value.get("task_hash") != task.task_hash or value.get("schema_version") != 1:
            return None
        return value
    except (OSError, ValueError):
        return None


def public_task(data_dir: Path, task: ImportedTask) -> TaskSpec:
    prepared = prepared_record(data_dir, task)
    return TaskSpec(
        id=task.id,
        version="1.0.0",
        title=task.title,
        category=task.category,
        description=task.prompt,
        task_hash=task.task_hash,
        image=str(prepared["image_id"]) if prepared else task.recipe.image or "unprepared",
        kind="native",
        submission_file=task.submission_file,
        difficulty=display_difficulty(task.suite_id, task.upstream_id, task.difficulty),
        tags=[task.suite_id, *task.tags],
        check_count=task.check_count,
        rubric="; ".join(task.comparison_notes)
        or "Versioned upstream grader in an isolated runtime.",
        suite_id=task.suite_id,
        upstream_id=task.upstream_id,
        upstream_revision=task.revision,
        variant=task.variant,
        prepared=prepared is not None,
    )


def suite_statuses(data_dir: Path, *, include_archived: bool = False) -> list[SuiteStatus]:
    entries = catalog_entries(data_dir)
    prepared_index = _prepared_index(data_dir)
    by_suite: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        by_suite.setdefault(entry["task"]["suite_id"], []).append(entry)
    result = []
    for suite in SUITES:
        if not include_archived and not suite.active:
            continue
        tasks = by_suite.get(suite.id, [])
        prepared = sum(
            prepared_index.get(item["task"]["id"], {}).get("task_hash") == item["task"]["task_hash"]
            for item in tasks
        )
        blockers = sorted({item for task in tasks for item in task["blockers"]})
        if suite.offline == "unavailable":
            blockers = list(suite.requirements)
        result.append(
            SuiteStatus(
                suite=suite,
                state=(
                    "blocked"
                    if suite.offline == "unavailable"
                    else "prepared"
                    if tasks and prepared == len(tasks)
                    else "partial"
                    if prepared
                    else "imported"
                    if tasks
                    else "not_installed"
                ),
                imported_tasks=len(tasks),
                prepared_tasks=prepared,
                blockers=blockers,
                revisions=sorted({item["task"]["upstream_revision"] for item in tasks}),
            )
        )
    return result


def _write_catalog(data_dir: Path, tasks: list[ImportedTask]) -> None:
    """A derived public projection: never trusted for execution or grading."""
    grouped: dict[str, list[ImportedTask]] = {}
    for task in tasks:
        grouped.setdefault(task.suite_id, []).append(task)
    for suite_id, members in grouped.items():
        path = root(data_dir) / "catalog" / f"{suite_id}.json"
        entries = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        for task in members:
            projection = public_task(data_dir, task).model_dump(mode="json")
            projection["prepared"] = False
            projection["image"] = task.recipe.image or "unprepared"
            entries[task.id] = {"task": projection, "blockers": task.recipe.blockers}
        _atomic_json(path, entries)


def refresh_prepared_index(data_dir: Path) -> None:
    """Snapshot only image/hash fields used by catalog pages, not runtime verification."""
    directory = root(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    with FileLock(str(directory / "prepared-index.lock"), timeout=30):
        entries = {}
        for path in (directory / "prepared").glob("imported.*.json"):
            record = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(record.get("task_hash"), str) and isinstance(record.get("image_id"), str):
                entries[path.stem] = {
                    "task_hash": record["task_hash"],
                    "image_id": record["image_id"],
                }
        _atomic_json(directory / "prepared-index.json", entries)


def _prepared_index(data_dir: Path) -> dict[str, Any]:
    path = root(data_dir) / "prepared-index.json"
    if not path.is_file():
        return {}
    value: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return value


def rebuild_catalog(data_dir: Path) -> None:
    """Explicit recovery for installations predating the public catalog projection."""
    _write_catalog(data_dir, imported_tasks(data_dir))
    refresh_prepared_index(data_dir)


def catalog_entries(data_dir: Path) -> list[dict[str, Any]]:
    entries = []
    for path in sorted((root(data_dir) / "catalog").glob("*.json")):
        entries.extend(json.loads(path.read_text(encoding="utf-8")).values())
    return entries


def list_public_tasks(data_dir: Path) -> list[TaskSpec]:
    prepared = _prepared_index(data_dir)
    tasks = []
    for entry in catalog_entries(data_dir):
        task = TaskSpec.model_validate(entry["task"])
        if task.suite_id and task.upstream_id:
            task.difficulty = display_difficulty(task.suite_id, task.upstream_id, task.difficulty)
        record = prepared.get(task.id, {})
        task.prepared = record.get("task_hash") == task.task_hash
        if task.prepared:
            task.image = record["image_id"]
        tasks.append(task)
    return tasks


def _source_file(source: Path, relative: str) -> Path:
    base = source if source.is_dir() else source.parent
    path = base / relative
    if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(base.resolve()):
        raise ValueError(f"Source asset is not a regular contained file: {relative}")
    current = path
    while current != base:
        if current.is_symlink():
            raise ValueError("Source assets cannot traverse symlinks")
        current = current.parent
    return path


def install(data_dir: Path, source: Path, tasks: list[ImportedTask]) -> dict[str, Any]:
    from benchmaxxing.providers.accounting import capacity_batch

    with capacity_batch(data_dir):
        return _install(data_dir, source, tasks)


def _install(data_dir: Path, source: Path, tasks: list[ImportedTask]) -> dict[str, Any]:
    if not tasks or len({task.id for task in tasks}) != len(tasks):
        raise ValueError("An import must contain unique task identities")
    directory = root(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    with FileLock(str(directory / "import.lock"), timeout=0):
        blobs = asset_root(data_dir)
        blobs.mkdir(parents=True, exist_ok=True)
        known: dict[Path, tuple[str, int]] = {}
        needed: dict[str, tuple[Path, int]] = {}
        for task in tasks:
            get_suite(task.suite_id)
            for asset in task.assets:
                path = _source_file(source, asset.source)
                if path not in known:
                    known[path] = file_hash(path), path.stat().st_size
                digest, size = known[path]
                if asset.sha256 is not None and asset.sha256 != digest:
                    raise ValueError(f"Source asset checksum mismatch: {asset.source}")
                asset.sha256 = digest
                if not (blobs / digest).is_file():
                    needed[digest] = path, size
        from benchmaxxing.providers.downloads import check_capacity

        additional = sum(size for _, size in needed.values())
        check_capacity(data_dir, additional)
        for task in tasks:
            existing = directory / "tasks" / f"{task.id}.json"
            if existing.is_file() and get_imported(data_dir, task.id).task_hash != task.task_hash:
                raise ValueError("An immutable task record already exists; choose a new variant")
        for digest, (path, _) in needed.items():
            temporary = blobs / f".{digest}.{uuid.uuid4().hex}.tmp"
            try:
                shutil.copyfile(path, temporary)
                if file_hash(temporary) != digest:
                    raise ValueError("Source asset changed during import")
                os.replace(temporary, blobs / digest)
                from benchmaxxing.providers.accounting import note_file

                note_file(blobs / digest)
            finally:
                temporary.unlink(missing_ok=True)
        # Publish records only after every referenced blob is available.
        for task in tasks:
            _atomic_json(
                directory / "tasks" / f"{task.id}.json",
                {"task": task.model_dump(mode="json"), "sha256": task.task_hash},
            )
        _write_catalog(data_dir, tasks)
        return {
            "imported_tasks": len(tasks),
            "copied_bytes": additional,
            "suites": sorted({task.suite_id for task in tasks}),
            "task_ids": [task.id for task in tasks],
        }


def convert(
    suite_id: str, source: Path, *, revision: str, variant: str | None = None
) -> list[ImportedTask]:
    from benchmaxxing.providers.catalog import require_active_suite

    require_active_suite(suite_id)
    digest = source_hash(source)
    if suite_id == "frontend-design":
        from benchmaxxing.providers.frontend import convert_frontend

        return convert_frontend(source, revision=revision, source_sha256=digest, variant=variant)
    if suite_id == "frontend-design-extended":
        from benchmaxxing.providers.frontend import convert_frontend_extension

        return convert_frontend_extension(
            source, revision=revision, source_sha256=digest, variant=variant
        )
    if suite_id in {
        "evalplus-humaneval",
        "evalplus-mbpp",
        "bigcodebench",
        "swebench-lite",
        "swebench-verified",
        "scicode-verified",
    }:
        from benchmaxxing.providers.coding import convert_coding

        return convert_coding(
            suite_id, source, revision=revision, source_sha256=digest, variant=variant
        )
    if suite_id in {
        "terminal-bench-2-1",
        "terminal-bench-4",
        "miniwob",
        "webarena-verified",
        "visualwebarena",
        "workarena",
    }:
        from benchmaxxing.providers.interactive import convert_interactive

        return convert_interactive(
            suite_id, source, revision=revision, source_sha256=digest, variant=variant
        )
    from benchmaxxing.providers.specialized import convert_specialized

    return convert_specialized(
        suite_id, source, revision=revision, source_sha256=digest, variant=variant
    )
