"""Reviewed, data-only Cybench input variants for tasks without local services.

The versioned manifest names exact source files or ZIP members and their hashes.
Upstream init scripts, challenge programs, and archive members are never executed
during preparation. The original imported task records remain immutable.
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import Field, model_validator

from benchmaxxing.contracts import Contract
from benchmaxxing.environment import EnvironmentError, _inspect_image
from benchmaxxing.providers import downloads, registry
from benchmaxxing.providers.contracts import ImportedTask, SourceAsset, relative_path
from benchmaxxing.providers.preparation import prepare_suite
from benchmaxxing.providers.specialized_sources import PINS

_PLAN_PATH = Path(__file__).resolve().parents[1] / "runtimes" / "cybench_static_inputs_v1.json"
_MISSING_INPUTS = "Missing prepared benchmaxxing-inputs from upstream init_script.sh"
_MAX_INPUT_BYTES = 8 * 1024 * 1024
_MAX_ARCHIVE_BYTES = 16 * 1024 * 1024
_MAX_ARCHIVE_CONTENT_BYTES = 64 * 1024 * 1024


class StaticInput(Contract):
    kind: Literal["copy", "zip_member"]
    source: str
    target: str
    size: int = Field(ge=1, le=_MAX_INPUT_BYTES)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    member: str | None = None
    archive_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    executable: bool = False

    @model_validator(mode="after")
    def paths(self) -> StaticInput:
        relative_path(self.source)
        relative_path(self.target)
        if PurePosixPath(self.target).name != self.target:
            raise ValueError("Static candidate inputs must have basename targets")
        if self.kind == "zip_member":
            if self.member is None or self.archive_sha256 is None:
                raise ValueError("ZIP inputs need a pinned member and archive hash")
            relative_path(self.member)
        elif self.member is not None or self.archive_sha256 is not None:
            raise ValueError("Direct copies cannot declare ZIP metadata")
        return self


class StaticTask(Contract):
    upstream_id: str
    init_script_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    inputs: list[StaticInput] = Field(min_length=1)

    @model_validator(mode="after")
    def identities(self) -> StaticTask:
        relative_path(self.upstream_id)
        targets = [item.target for item in self.inputs]
        if len(targets) != len(set(targets)):
            raise ValueError("Static task candidate targets must be unique")
        return self


class StaticPlan(Contract):
    schema_version: Literal[1]
    source_revision: str
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    base_variant: str
    variant: str
    tasks: list[StaticTask] = Field(min_length=1)

    @model_validator(mode="after")
    def identities(self) -> StaticPlan:
        names = [task.upstream_id for task in self.tasks]
        if len(names) != len(set(names)) or self.base_variant == self.variant:
            raise ValueError("Static plan identities must be distinct")
        return self


def load_plan() -> StaticPlan:
    return StaticPlan.model_validate_json(_PLAN_PATH.read_bytes())


def _source_snapshot(data_dir: Path, plan: StaticPlan) -> tuple[Path, Path]:
    cache = downloads.cache_root(data_dir).resolve()
    source = cache / "sources" / "cybench" / f"{plan.source_revision}-task-inputs-v1"
    manifest = cache / "manifests" / "cybench.prepared-source.json"
    if plan.source_revision != PINS["cybench"] or not manifest.is_file():
        raise ValueError("Fetch the pinned Cybench source before static preparation")
    record = json.loads(manifest.read_text(encoding="utf-8"))
    if (
        record.get("suite_id") != "cybench"
        or record.get("revision") != plan.source_revision
        or record.get("source_sha256") != plan.source_sha256
        or not isinstance(record.get("source_path"), str)
        or Path(record["source_path"]).resolve() != source.resolve()
        or source.is_symlink()
        or not source.is_dir()
    ):
        raise ValueError("Pinned Cybench source manifest differs; fetch again explicitly")
    if registry.source_hash(source) != plan.source_sha256:
        raise ValueError("Pinned Cybench source files changed; fetch again explicitly")
    return cache, source


def _source_asset(task: ImportedTask, name: str, expected_hash: str) -> None:
    if not any(
        asset.source == name and asset.sha256 == expected_hash for asset in task.assets
    ):
        raise ValueError(f"Cybench imported source asset differs: {name}")


def _verified_file(source: Path, name: str, digest: str, size: int | None = None) -> Path:
    path = registry._source_file(source, name)
    if (size is not None and path.stat().st_size != size) or registry.file_hash(path) != digest:
        raise ValueError(f"Pinned Cybench file differs: {name}")
    return path


def _zip_member_bytes(archive: Path, item: StaticInput) -> bytes:
    if archive.stat().st_size > _MAX_ARCHIVE_BYTES:
        raise ValueError("Cybench static ZIP exceeds the archive byte limit")
    try:
        with zipfile.ZipFile(archive) as source:
            members = source.infolist()
            names = [member.filename for member in members]
            if len(members) > 128 or len(names) != len(set(names)):
                raise ValueError("Cybench static ZIP has too many or duplicate members")
            if sum(member.file_size for member in members) > _MAX_ARCHIVE_CONTENT_BYTES:
                raise ValueError("Cybench static ZIP exceeds the expanded byte limit")
            for member in members:
                relative_path(member.filename.rstrip("/"))
                mode = stat.S_IFMT(member.external_attr >> 16)
                if mode not in {0, stat.S_IFREG, stat.S_IFDIR} or member.flag_bits & 1:
                    raise ValueError("Cybench static ZIP has unsupported member types")
            assert item.member is not None
            member = source.getinfo(item.member)
            if member.is_dir() or member.file_size != item.size:
                raise ValueError("Pinned Cybench ZIP member size differs")
            if member.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                raise ValueError("Cybench static ZIP compression is unsupported")
            chunks: list[bytes] = []
            total = 0
            with source.open(member) as stream:
                while chunk := stream.read(65536):
                    total += len(chunk)
                    if total > item.size:
                        raise ValueError("Cybench ZIP member exceeded its pinned size")
                    chunks.append(chunk)
            result = b"".join(chunks)
    except (zipfile.BadZipFile, KeyError, OSError, RuntimeError) as exc:
        raise ValueError("Could not read the pinned Cybench ZIP member") from exc
    if len(result) != item.size or hashlib.sha256(result).hexdigest() != item.sha256:
        raise ValueError("Pinned Cybench ZIP member hash differs")
    return result


def _candidate_asset(
    data_dir: Path, cache: Path, source: Path, task: ImportedTask, item: StaticInput,
    plan: StaticPlan,
) -> SourceAsset:
    original = f"{task.upstream_id}/{item.source}"
    _source_asset(task, original, item.archive_sha256 or item.sha256)
    if item.kind == "copy":
        _verified_file(source, original, item.sha256, item.size)
        location = source.relative_to(cache).as_posix() + "/" + original
    else:
        assert item.archive_sha256 is not None
        archive = _verified_file(source, original, item.archive_sha256)
        payload = _zip_member_bytes(archive, item)
        location = f"derived/cybench-static-inputs-v1/{plan.source_sha256}/{item.sha256}"
        destination = cache / location
        if not destination.parent.resolve().is_relative_to(cache):
            raise ValueError("Derived Cybench input leaves its cache")
        if destination.exists():
            if destination.is_symlink() or destination.stat().st_size != item.size:
                raise ValueError("Derived Cybench input changed")
            if registry.file_hash(destination) != item.sha256:
                raise ValueError("Derived Cybench input hash changed")
        else:
            downloads.write_prepared_file(data_dir, destination, payload)
    return SourceAsset(
        source=location, target=item.target, scope="candidate", sha256=item.sha256
    )


def _offline_prompt(original: str, inputs: list[StaticInput]) -> str:
    names = ", ".join(item.target for item in inputs)
    return (
        original.rstrip()
        + "\n\nOffline input location: The supplied challenge files are in /workspace: "
        + names
        + ". References in the original prompt to $TMP_DIR, /challenge, "
        "the parent directory, or the working directory refer to /workspace "
        "for this variant."
    )


def _adapt_task(
    data_dir: Path, cache: Path, source: Path, original: ImportedTask, entry: StaticTask,
    plan: StaticPlan,
) -> ImportedTask:
    if (
        original.suite_id != "cybench"
        or original.upstream_id != entry.upstream_id
        or original.revision != plan.source_revision
        or original.source_sha256 != plan.source_sha256
        or original.variant != plan.base_variant
        or original.recipe.network != "none"
        or original.recipe.workdir != "/workspace"
        or original.recipe.blockers != [_MISSING_INPUTS]
        or original.recipe.settings.get("mode") != "unguided"
    ):
        raise ValueError(
            f"Cybench static task differs from the reviewed source: {entry.upstream_id}"
        )
    script = f"{entry.upstream_id}/init_script.sh"
    _source_asset(original, script, entry.init_script_sha256)
    _verified_file(source, script, entry.init_script_sha256)
    candidate = [
        _candidate_asset(data_dir, cache, source, original, item, plan)
        for item in entry.inputs
    ]
    task = original.model_copy(deep=True)
    task.variant = plan.variant
    task.title = (original.title + " · offline static inputs")[:300]
    task.prompt = _offline_prompt(original.prompt, entry.inputs)
    task.recipe.blockers = []
    task.recipe.requirements = ["Pinned local Linux image", "Reviewed static inputs"]
    task.recipe.settings["static_input_manifest"] = {
        "version": plan.schema_version,
        "init_script_sha256": entry.init_script_sha256,
        "inputs": [item.model_dump(mode="json") for item in entry.inputs],
    }
    task.recipe.settings["candidate_executable_files"] = [
        item.target for item in entry.inputs if item.executable
    ]
    task.comparison_notes.append(
        "Offline static-input variant; upstream setup script was inspected but not executed."
    )
    prefix = source.relative_to(cache).as_posix() + "/"
    for asset in task.assets:
        asset.source = prefix + asset.source
    task.assets.extend(candidate)
    return ImportedTask.model_validate(task.model_dump(mode="json"))


def prepare_static_runtime(
    data_dir: Path, tasks: list[ImportedTask], *, image: str | None, pull: bool
) -> dict[str, Any]:
    """Install only reviewed static variants and probe an already local Linux image."""
    if pull:
        raise ValueError("Cybench static preparation never pulls images")
    if not image or not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
        raise ValueError("Provide an existing pinned Linux image with --image sha256:...")
    try:
        info = _inspect_image(image)
    except EnvironmentError as exc:
        raise ValueError("Pinned Cybench image is unavailable locally") from exc
    if info.get("Id") != image or info.get("Os") != "linux":
        raise ValueError("Pinned Cybench image must be a local Linux image")
    plan = load_plan()
    cache, source = _source_snapshot(data_dir, plan)
    originals = {
        task.upstream_id: task
        for task in tasks
        if task.variant == plan.base_variant and task.source_sha256 == plan.source_sha256
    }
    if any(entry.upstream_id not in originals for entry in plan.tasks):
        raise ValueError("The reviewed Cybench source cohort is not fully imported")
    adapted = [
        _adapt_task(data_dir, cache, source, originals[entry.upstream_id], entry, plan)
        for entry in plan.tasks
    ]
    registry.install(data_dir, cache, adapted)
    result = prepare_suite(data_dir, adapted, image=image, pull=False)
    result["variant"] = plan.variant
    result["reviewed_tasks"] = len(plan.tasks)
    return result
