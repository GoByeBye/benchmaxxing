"""Verify explicitly prepared native runtimes; execution never installs anything."""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from benchmaxxing.environment import (
    EnvironmentError,
    PreparationError,
    _docker,
    _inspect_image,
    _require_success,
)
from benchmaxxing.providers import registry
from benchmaxxing.providers.accounting import capacity_preparation
from benchmaxxing.providers.contracts import ImportedTask

SANDBOX_PROFILE = "native-unprivileged-writable-v1"


def check_docker_capacity(data_dir: Path, additional: int) -> dict[str, Any]:
    """Reserve cache capacity and conservatively verify the Docker backing disk.

    Docker Desktop's Linux disk is commonly on the Windows system drive even
    when the source cache lives elsewhere. Until its location is known, require
    the reservation on both disks. Remote Linux daemons need a local proof.
    """
    from benchmaxxing.providers.downloads import check_capacity

    check_capacity(data_dir, additional)
    if os.name == "nt":
        backing = Path(os.environ.get("SystemDrive", "C:") + "/")
        method = "conservative-windows-system-drive"
    else:
        output = _require_success(
            _docker(["info", "--format", "{{json .DockerRootDir}}"], timeout=30),
            "Could not determine Docker backing storage",
        )
        backing = Path(json.loads(output))
        method = "local-docker-root"
        if not backing.is_dir():
            raise PreparationError(
                "Docker backing storage is not locally inspectable; verify its capacity "
                "before preparing this runtime"
            )
    reserve = registry.settings(data_dir).reserve_free_bytes
    free = shutil.disk_usage(backing).free
    if free - additional < reserve:
        raise PreparationError(
            f"Docker backing disk needs {additional + reserve:,} free bytes; "
            f"only {free:,} are available at {backing}"
        )
    return {
        "path": str(backing),
        "method": method,
        "free_bytes": free,
        "reservation_bytes": additional,
        "reserve_free_bytes": reserve,
    }


_PROBE = """import hashlib,importlib,importlib.metadata,json,os,pathlib,shutil,sys
spec=json.loads(sys.argv[1]); result={'modules':{},'distributions':{},'commands':{}}
for name in spec['modules']:
 module=importlib.import_module(name)
 result['modules'][name]={'file':getattr(module,'__file__',None)}
for name in spec['distributions']:
 dist=importlib.metadata.distribution(name)
 result['distributions'][name]={'version':dist.version,
  'direct_url':json.loads(dist.read_text('direct_url.json') or '{}')}
for command in spec['commands']:
 found=shutil.which(command)
 if not found: raise RuntimeError('Missing executable: '+command)
 result['commands'][command]=found
for directory in {spec['workdir'],'/workspace','/grader'}:
 root=pathlib.Path(directory)
 if root.resolve()!=root or not root.is_dir() or not os.access(root,os.W_OK|os.X_OK):
  raise RuntimeError('Prepared directories must be real and writable by uid 65534: '+directory)
for name in spec['files']:
 if not pathlib.Path(name).is_file(): raise RuntimeError('Missing prepared file: '+name)
result['file_sha256']={}
for name,expected in spec['file_sha256'].items():
 path=pathlib.Path(name)
 if path.is_symlink() or not path.is_file(): raise RuntimeError('Invalid source probe: '+name)
 with path.open('rb') as source:
  state=hashlib.sha256()
  while block:=source.read(1048576): state.update(block)
 actual=state.hexdigest()
 if actual!=expected: raise RuntimeError('Prepared upstream source hash differs: '+name)
 result['file_sha256'][name]=actual
print(json.dumps(result))
"""


def _assets(data_dir: Path, task: ImportedTask, verified: set[str] | None = None) -> dict[str, str]:
    hashes = {}
    base = registry.asset_root(data_dir)
    for asset in task.assets:
        if asset.sha256 is None:
            raise PreparationError("Import source assets before preparing the runtime")
        path = base / asset.sha256
        if (
            not path.is_file()
            or path.is_symlink()
            or (
                (verified is None or asset.sha256 not in verified)
                and registry.file_hash(path) != asset.sha256
            )
        ):
            raise PreparationError(
                f"Imported asset missing or changed: {asset.target}; import again"
            )
        if verified is not None:
            verified.add(asset.sha256)
        hashes[f"{asset.scope}:{asset.target}"] = asset.sha256
    return hashes


def _requirements(task: ImportedTask) -> tuple[list[str], dict[str, str]]:
    modules = list(task.recipe.settings.get("probe_modules", []))
    family = task.recipe.family
    if family == "evalplus":
        modules += ["numpy"]
        if task.recipe.settings.get("profile") == "official-differential-jsonl":
            modules += ["evalplus.evaluate"]
    elif family == "bigcodebench":
        modules += ["bigcodebench.evaluate"]
    elif family == "swebench":
        modules += ["swebench.harness.grading"]
    elif family == "scicode":
        modules += ["numpy", "scipy", "h5py", "matplotlib", "sympy"]
    versions = {}
    for requirement in task.recipe.requirements:
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+-]+)", requirement)
        if match:
            versions[match[1]] = match[2]
    if any(
        not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", name)
        for name in modules
    ):
        raise PreparationError("Invalid preparation module probe")
    return sorted(set(modules)), versions


def _validate_recipe(task: ImportedTask) -> None:
    if task.recipe.blockers:
        raise PreparationError("; ".join(task.recipe.blockers))
    if task.recipe.network == "external":
        raise PreparationError("This imported task requires external networking and is not offline")
    executable = task.recipe.settings.get("candidate_executable_files", [])
    candidate_targets = {asset.target for asset in task.assets if asset.scope == "candidate"}
    if (
        not isinstance(executable, list)
        or len(executable) > 64
        or bool(executable) and task.recipe.workdir != "/workspace"
        or any(
            not isinstance(name, str)
            or name not in candidate_targets
            or PurePosixPath(name).name != name
            for name in executable
        )
        or len(executable) != len(set(executable))
    ):
        raise PreparationError("Executable candidate files must be declared candidate basenames")
    if task.recipe.settings.get("requires_filesystem_snapshot"):
        raise PreparationError(
            "Native task needs a full filesystem snapshot; prepare an explicit bounded artifact "
            "export recipe before execution"
        )
    if task.recipe.family == "browsergym" and task.suite_id != "miniwob":
        raise PreparationError(
            "Prepare and validate the task-local website snapshot/reset runtime; "
            "Python packages alone cannot make this browser suite ready"
        )
    if not task.recipe.grader_command or not task.recipe.grader_result_path:
        raise PreparationError("Prepare a native grading command and recorded result path")
    workdir = task.recipe.workdir
    if (
        not workdir.startswith("/")
        or ".." in Path(workdir).parts
        or "\\" in workdir
        or workdir in {"/", "/etc", "/proc", "/sys", "/dev", "/grader"}
    ):
        raise PreparationError("Declare a safe absolute task workdir")


@capacity_preparation
def prepare_imported(
    data_dir: Path,
    task: ImportedTask,
    *,
    image: str | None = None,
    pull: bool = False,
    _cache: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Probe a local image and publish a pin only after assets/runtime checks pass.

    Image builds and downloads belong to explicit preparation. Large image pulls
    require a declared size ceiling so they cannot silently exceed the cache cap.
    """
    _validate_recipe(task)
    cache: dict[str, Any] = _cache if _cache is not None else {}
    hashes = _assets(data_dir, task, cache.setdefault("asset_hashes", set()))
    requested = image or task.recipe.image
    if not requested or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:@-]{0,511}", requested):
        raise PreparationError("Build or select an explicit native runtime image")
    if pull and requested not in cache.setdefault("pulled", set()):
        ceiling = task.recipe.settings.get("max_image_bytes")
        if not isinstance(ceiling, int) or isinstance(ceiling, bool) or ceiling <= 0:
            raise PreparationError(
                "Prepare the image explicitly or declare max_image_bytes before pulling"
            )
        check_docker_capacity(data_dir, ceiling)
        _require_success(
            _docker(["pull", requested], timeout=600), "Native image preparation failed"
        )
        cache["pulled"].add(requested)
    try:
        images = cache.setdefault("images", {})
        if requested not in images:
            images[requested] = _inspect_image(requested)
        info = images[requested]
    except EnvironmentError as exc:
        raise PreparationError(
            f"Native image is not local; build it during explicit preparation: {requested}"
        ) from exc
    if info.get("Os") != "linux" or not re.fullmatch(r"sha256:[a-f0-9]{64}", str(info.get("Id"))):
        raise PreparationError("Native tasks require an immutable Linux image identity")
    checked_capacity = cache.setdefault("capacity_images", set())
    if info["Id"] not in checked_capacity:
        _capacity(data_dir, int(info.get("Size", 0)), image_id=info["Id"])
        checked_capacity.add(info["Id"])
    modules, versions = _requirements(task)
    packages = set(versions)
    harness = task.recipe.settings.get("harness_revision")
    distribution = None
    if task.recipe.family in {"bigcodebench", "swebench"}:
        distribution = task.recipe.family
    elif (
        task.recipe.family == "evalplus"
        and task.recipe.settings.get("profile") == "official-differential-jsonl"
    ):
        distribution = "evalplus"
    if distribution:
        packages.add(distribution)
    workdir = task.recipe.workdir
    if task.recipe.family == "harbor":
        workdir = info.get("Config", {}).get("WorkingDir") or workdir
    source_hashes = task.recipe.settings.get("probe_file_sha256", {})
    if not isinstance(source_hashes, dict) or any(
        not isinstance(name, str)
        or not name.startswith("/")
        or str(PurePosixPath(name)) != name
        or ".." in PurePosixPath(name).parts
        or any(c in name for c in ("\\", "\0"))
        or not isinstance(digest, str)
        or not re.fullmatch(r"[a-f0-9]{64}", digest)
        for name, digest in source_hashes.items()
    ):
        raise PreparationError("Source probes require normalized absolute paths and SHA-256 hashes")
    probe = {
        "modules": modules,
        "distributions": sorted(packages),
        "commands": ["python", task.recipe.grader_command[0]],
        "workdir": workdir,
        "files": task.recipe.settings.get("probe_files", []),
        "file_sha256": source_hashes,
    }
    probe_key = info["Id"] + json.dumps(probe, sort_keys=True)
    cached_probes = cache.setdefault("probes", {})
    if probe_key not in cached_probes:
        text = _require_success(
            _docker(
                [
                    "run",
                    "--rm",
                    "--pull",
                    "never",
                    "--network",
                    "none",
                    "--user",
                    "65534:65534",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges:true",
                    "--memory",
                    "2048m",
                    "--memory-swap",
                    "2048m",
                    "--cpus",
                    "1",
                    "--pids-limit",
                    "64",
                    "--tmpfs",
                    "/tmp:rw,nosuid,nodev,size=67108864,mode=1777",
                    "-e",
                    "HF_HUB_OFFLINE=1",
                    "-e",
                    "HF_DATASETS_OFFLINE=1",
                    "-e",
                    "PYTHONDONTWRITEBYTECODE=1",
                    "--entrypoint",
                    "python",
                    info["Id"],
                    "-c",
                    _PROBE,
                    json.dumps(probe),
                ],
                timeout=90,
                output_limit=262144,
            ),
            "Native runtime probe failed",
        )
        cached_probes[probe_key] = text
    text = cached_probes[probe_key]
    try:
        probes: dict[str, Any] = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise PreparationError("Runtime probe returned malformed metadata") from exc
    if source_hashes and probes.get("file_sha256") != source_hashes:
        raise PreparationError("Runtime source hash proof is missing or differs")
    for package, version in versions.items():
        if probes["distributions"][package]["version"] != version:
            raise PreparationError(f"Prepare the declared dependency version: {package}=={version}")
    if distribution and harness:
        found = (
            probes["distributions"][distribution]["direct_url"].get("vcs_info", {}).get("commit_id")
        )
        if found != harness:
            raise PreparationError(
                f"Prepare {distribution} from declared upstream commit {harness}"
            )
    # containerd may initially report compressed size before the first run
    # materializes all layers. Recheck after the probe and account for growth.
    refreshed = cache.setdefault("size_refreshed", set())
    if info["Id"] not in refreshed:
        after_probe = _inspect_image(info["Id"])
        info["Size"] = max(int(info.get("Size", 0)), int(after_probe.get("Size", 0)))
        _capacity(data_dir, info["Size"], image_id=info["Id"])
        refreshed.add(info["Id"])
    writable = task.recipe.settings.get("writable_layer_limit_bytes", 268435456)
    if (
        isinstance(writable, bool)
        or not isinstance(writable, int)
        or not 16777216 <= writable <= 1073741824
    ):
        raise PreparationError("Native writable-layer allowance must be between 16 MiB and 1 GiB")
    record = {
        "schema_version": 1,
        "task_hash": task.task_hash,
        "image_id": info["Id"],
        "requested_image": requested,
        "repo_digests": info.get("RepoDigests", []),
        "architecture": info.get("Architecture"),
        "os": info["Os"],
        "image_size_bytes": int(info.get("Size", 0)),
        "asset_hashes": hashes,
        "sandbox_profile": SANDBOX_PROFILE,
        "workdir": workdir,
        "writable_layer_limit_bytes": writable,
        "probes": probes,
        "prepared_at": datetime.now(UTC).isoformat(),
    }
    previous = registry.prepared_record(data_dir, task)
    if previous is not None and previous.get("image_id") == info["Id"]:
        # Revalidation updates proofs while retaining the reviewed build recipe.
        record = {**previous, **record}
    registry._atomic_json(registry.root(data_dir) / "prepared" / f"{task.id}.json", record)
    if _cache is None:
        registry.refresh_prepared_index(data_dir)
    return record


def _capacity(data_dir: Path, additional: int, *, image_id: str | None = None) -> None:
    from benchmaxxing.providers.downloads import check_capacity

    existing: dict[str, int] = {}
    for path in [
        *(registry.root(data_dir) / "prepared").glob("*.json"),
        *registry.root(data_dir).glob("*-tools.json"),
    ]:
        record = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(record.get("image_id"), str):
            known = existing.get(record["image_id"], 0)
            existing[record["image_id"]] = max(known, int(record.get("image_size_bytes", 0)))
    if image_id in existing:
        additional = max(0, additional - existing[image_id])
    try:
        check_capacity(data_dir, additional)
    except ValueError as exc:
        raise PreparationError(str(exc)) from exc


@capacity_preparation
def prepare_suite(
    data_dir: Path,
    tasks: list[ImportedTask],
    *,
    image: str | None = None,
    pull: bool = False,
    strict: bool = False,
) -> dict[str, Any]:
    """Reuse immutable image/module proofs, checking every task's recipe and assets."""
    if image == "":
        from benchmaxxing.providers.runtime import prepare_runtime

        suites = {task.suite_id for task in tasks}
        if len(suites) != 1:
            raise PreparationError("Automatic runtime preparation requires exactly one suite")
        return prepare_runtime(data_dir, suites.pop())
    cache: dict[str, Any] = {}
    image_ids = set()
    task_ids = []
    errors = {}
    for task in tasks:
        try:
            record = prepare_imported(data_dir, task, image=image, pull=pull, _cache=cache)
            image_ids.add(record["image_id"])
            task_ids.append(task.id)
        except (EnvironmentError, ValueError) as exc:
            if strict:
                raise
            errors[task.id] = str(exc)[:2000]
    registry.refresh_prepared_index(data_dir)
    return {
        "prepared_tasks": len(task_ids),
        "image_ids": sorted(image_ids),
        "task_ids": task_ids,
        "errors": errors,
    }


def preparation_info(data_dir: Path, task: ImportedTask) -> dict[str, Any]:
    """Read-only verification; never pull, build, install, or repair dependencies."""
    _validate_recipe(task)
    record = registry.prepared_record(data_dir, task)
    if record is None or record.get("sandbox_profile") != SANDBOX_PROFILE:
        raise PreparationError(
            "Imported runtime is not prepared; run the suite preparation command"
        )
    image_id = record.get("image_id")
    if not isinstance(image_id, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
        raise PreparationError("Invalid imported image pin; prepare again")
    try:
        info = _inspect_image(image_id)
    except EnvironmentError as exc:
        raise PreparationError(
            "Pinned imported image is unavailable; explicitly prepare again"
        ) from exc
    if info.get("Id") != image_id or info.get("Os") != "linux":
        raise PreparationError("Imported image identity or platform changed; prepare again")
    if _assets(data_dir, task) != record.get("asset_hashes"):
        raise PreparationError("Imported asset manifest changed; prepare again")
    return record
