"""Data-only import of Harbor tasks and prepared BrowserGym datasets.

Nothing in this module imports upstream Python, builds images, or visits a URL.
An import preserves a native evaluation recipe; preparation must satisfy that
recipe before it can be selected for execution.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from pathlib import Path
from typing import Any, Literal

from benchmaxxing.providers.contracts import ImportedTask, NativeRecipe, SourceAsset

_MAX_METADATA_BYTES = 32 * 1024 * 1024
_DEFAULT_VARIANT = "benchmaxxing-offline-30m-v1"
# These HTML demos are deliberately absent from BrowserGym 0.14.3's 125-task registry.
_MINIWOB_UNREGISTERED = {
    "button-delay",
    "chase-circle",
    "hover-shape",
    "moving-items",
    "simon-says",
}
_URLS = {
    "terminal-bench-2-1": "https://github.com/harbor-framework/terminal-bench-2-1",
    "terminal-bench-4": "https://github.com/harbor-framework/terminal-bench/tree/v4.0.0",
    "miniwob": "https://github.com/Farama-Foundation/miniwob-plusplus",
    "webarena-verified": "https://github.com/ServiceNow/webarena-verified",
    "visualwebarena": "https://github.com/web-arena-x/visualwebarena",
    "workarena": "https://github.com/ServiceNow/WorkArena",
}


class InteractiveImportError(ValueError):
    """The supplied local snapshot is incomplete or not a supported format."""


def _regular(root: Path, path: Path) -> Path:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise InteractiveImportError("Asset is outside the supplied source directory") from exc
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise InteractiveImportError(f"Symlinks are not importable: {relative.as_posix()}")
    if not path.is_file():
        raise InteractiveImportError(f"Missing regular source file: {relative.as_posix()}")
    return path


def _text(root: Path, path: Path) -> str:
    _regular(root, path)
    if path.stat().st_size > _MAX_METADATA_BYTES:
        raise InteractiveImportError(f"Metadata exceeds 32 MiB: {path.name}")
    return path.read_text(encoding="utf-8-sig")


def _asset(
    root: Path, path: Path, target: str, scope: Literal["candidate", "grader", "runtime"]
) -> SourceAsset:
    _regular(root, path)
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return SourceAsset(
        source=path.relative_to(root).as_posix(), target=target, scope=scope, sha256=digest
    )


def _tree(
    root: Path, folder: Path, target: str, scope: Literal["candidate", "grader", "runtime"]
) -> list[SourceAsset]:
    assets = []
    if folder.is_symlink():
        raise InteractiveImportError(f"Symlink directory is not importable: {folder.name}")
    for path in sorted(folder.rglob("*")):
        if path.is_symlink():
            raise InteractiveImportError(f"Symlink is not importable: {path.name}")
        if path.is_file():
            assets.append(
                _asset(root, path, f"{target}/{path.relative_to(folder).as_posix()}", scope)
            )
    return assets


def _difficulty(value: Any) -> Literal["easy", "medium", "hard"]:
    if value in ("easy", "medium", "hard"):
        return value  # type: ignore[no-any-return]
    return "hard"


def _table(raw: dict[str, Any], key: str) -> dict[str, Any]:
    result = raw.get(key, {})
    if not isinstance(result, dict):
        raise InteractiveImportError(f"Expected a TOML table for {key}")
    return result


def _reviewed_harbor(task: ImportedTask, folder: Path) -> ImportedTask:
    """Enable only byte-reviewed bounded-output adaptations, never a blanket snapshot."""
    standard_pytest = (
        "mkdir -p /logs/verifier\n"
        "python -m pytest --ctrf /logs/verifier/ctrf.json /tests/test_outputs.py -rA\n"
        "if [ $? -eq 0 ]; then echo 1; else echo 0; fi > /logs/verifier/reward.txt\n"
    )
    reviewed = {
        ("terminal-bench-2-1", "terminal-bench/cancel-async-tasks"): {
            "fingerprint": "98e3155bda13ca6665508fa1427a13489e62a6ef6d48c6749ad089fd3c315f87",
            "profile": "cancel-async-tasks-offline-v1",
            "artifacts": ["/app/run.py"],
            "pytest": "8.4.1",
            "ctrf": "0.3.5",
            "wrapper": "mkdir -p /logs/verifier\ncp /tests/test.py /app/test.py\n"
            "python -m pytest --ctrf /logs/verifier/ctrf.json /tests/test_outputs.py -rA\n"
            "if [ $? -eq 0 ]; then echo 1; else echo 0; fi > /logs/verifier/reward.txt\n",
        },
        ("terminal-bench-4", "terminal-bench/sound-change-cascade"): {
            "fingerprint": "c8245d420256523310f98df434912880d8989fbc69ffb1f0c1566240c4867a12",
            "profile": "sound-change-cascade-offline-v1",
            "artifacts": ["/app/rules.json", "/app/ordering.txt"],
            "pytest": "9.1.1",
            "ctrf": "0.5.2",
            "wrapper": "mkdir -p /logs/verifier\nbash /tests/test.sh\n",
        },
        ("terminal-bench-2-1", "terminal-bench/regex-log"): {
            "fingerprint": "181ee872aa005fa1082d9de4b2ffc01ef3aa322eb08cc0061db35153d9169701",
            "profile": "regex-log-offline-v1",
            "artifacts": ["/app/regex.txt"],
            "pytest": "8.4.1",
            "ctrf": "0.3.5",
            "wrapper": standard_pytest,
        },
        ("terminal-bench-2-1", "terminal-bench/sqlite-db-truncate"): {
            "fingerprint": "07c19eaf9e7cb52889b73d2ef279d704971dae5fa40992d0646c1005a09fa47c",
            "profile": "sqlite-db-truncate-offline-v1",
            "artifacts": ["/app/recover.json"],
            "pytest": "8.4.1",
            "ctrf": "0.3.5",
            "wrapper": standard_pytest,
        },
    }.get((task.suite_id, task.upstream_id))
    if reviewed is None:
        return task
    from benchmaxxing.providers.registry import source_hash

    if source_hash(folder) != reviewed["fingerprint"]:
        return task
    if not task.variant.endswith("-artifacts-v1"):
        task.variant += "-artifacts-v1"
    if reviewed["profile"] == "sound-change-cascade-offline-v1":
        task.variant += "-nogit-v1"
        task.comparison_notes.append(
            "The bounded prepared image omits the upstream optional git executable. "
            "Task inputs, the Python rule engine and native grading assertions are unchanged."
        )
    if reviewed["profile"] in {
        "regex-log-offline-v1",
        "sqlite-db-truncate-offline-v1",
    }:
        task.variant += "-prepacked-pytest-v1"
        task.comparison_notes.append(
            "The upstream verifier's dependency download is replaced by a pinned, "
            "already prepared Python 3.13/pytest runtime. Native assertions are unchanged."
        )
    if reviewed["profile"] == "regex-log-offline-v1":
        task.comparison_notes.append(
            "The offline runtime uses Python 3.13 slim Bookworm in place of the upstream "
            "Ubuntu 24.04 task image; compare only this distinct environment variant."
        )
    task.recipe.settings.update(
        {
            "offline_profile": reviewed["profile"],
            "reviewed_source_sha256": reviewed["fingerprint"],
            "artifacts": reviewed["artifacts"],
            "requires_filesystem_snapshot": False,
            "probe_modules": ["pytest"],
            "max_image_bytes": 2 * 1024**3,
        }
    )
    task.recipe.requirements += [
        f"pytest=={reviewed['pytest']}",
        f"pytest-json-ctrf=={reviewed['ctrf']}",
    ]
    if reviewed["profile"] == "sqlite-db-truncate-offline-v1":
        source = next(
            asset
            for asset in task.assets
            if asset.scope == "runtime" and asset.target == "environment/trunc.db"
        )
        assert source.sha256 is not None
        task.recipe.settings["probe_file_sha256"] = {"/app/trunc.db": source.sha256}
    task.recipe.grader_command = ["bash", "/grader/benchmaxxing-offline-test.sh"]
    task.private_files["benchmaxxing-offline-test.sh"] = "#!/bin/bash\nset -uo pipefail\n" + str(
        reviewed["wrapper"]
    )
    task.comparison_notes += [
        "Reviewed bounded-artifact offline adaptation: only the named output files cross into "
        "a fresh immutable grader image; candidate interpreter/system changes are discarded.",
        "Native test inputs and assertions are unchanged. Grading dependencies are prepared "
        "in advance and execution performs no installation.",
    ]
    return task


def convert_harbor(
    source: Path,
    *,
    suite_id: str,
    revision: str,
    source_sha256: str,
    variant: str | None = None,
) -> list[ImportedTask]:
    """Import one Harbor task, a task collection, or a repository's tasks/.

    Asset source paths are relative to ``source``. The environment build context
    remains separate from hidden verifier and oracle files. Native artifacts and
    verifier mode are retained so a controller cannot mistake /workspace alone
    for a complete submission.
    """
    root = Path(source).absolute()
    if root.is_symlink() or not root.is_dir():
        raise InteractiveImportError("Harbor source must be a regular directory")
    if (root / "task.toml").is_file():
        paths = [root / "task.toml"]
    else:
        collection = root / "tasks" if (root / "tasks").is_dir() else root
        paths = sorted(collection.glob("*/task.toml"))
    if not paths:
        raise InteractiveImportError("No Harbor task.toml files found in source or tasks/")
    tasks = []
    identifiers = set()
    for path in paths:
        config = tomllib.loads(_text(root, path))
        folder = path.parent
        task = _table(config, "task")
        metadata = _table(config, "metadata")
        environment = _table(config, "environment")
        verifier = _table(config, "verifier")
        agent = _table(config, "agent")
        upstream_id = str(task.get("name", folder.name))
        if upstream_id in identifiers:
            raise InteractiveImportError(f"Duplicate native task identity: {upstream_id}")
        identifiers.add(upstream_id)
        prompt = _text(root, folder / "instruction.md")
        _regular(root, folder / "tests" / "test.sh")
        assets = _tree(root, folder / "environment", "environment", "runtime")
        assets += _tree(root, folder / "tests", "tests", "grader")
        assets += _tree(root, folder / "solution", "solution", "grader")
        image = environment.get("docker_image")
        if image is not None and (not isinstance(image, str) or not image):
            raise InteractiveImportError(f"Invalid docker_image for {upstream_id}")
        requirements = [
            "A digest-pinned native environment image prepared without verifier/oracle files",
            "A fresh verifier sandbox with native artifacts and tests mounted privately",
            "Sandbox verification of the upstream reward file and verifier exit code",
        ]
        blockers = []
        if image is None and not (folder / "environment" / "Dockerfile").is_file():
            blockers.append("No upstream Docker image or environment/Dockerfile is available")
        if environment.get("gpus", 0):
            blockers.append(
                "Requires prepared GPU isolation and upstream GPU type: "
                + json.dumps(environment.get("gpu_types", ["unspecified"]))
            )
        if environment.get("mcp_servers"):
            blockers.append(
                "Upstream MCP services require an explicitly isolated local service recipe"
            )
        upstream_artifacts = config.get("artifacts", [])
        if not isinstance(upstream_artifacts, list):
            raise InteractiveImportError(f"Invalid native artifact paths for {upstream_id}")
        artifacts = []
        for item in upstream_artifacts:
            if isinstance(item, str):
                artifacts.append(item.rstrip("/"))
            elif isinstance(item, dict) and isinstance(item.get("source"), str):
                if item.get("service"):
                    blockers.append(
                        "Native artifact collection requires prepared task-local service: "
                        + str(item["service"])
                    )
                elif item.get("exclude"):
                    blockers.append(
                        "Native artifact exclusion filters need an audited bounded output variant"
                    )
                else:
                    artifacts.append(item["source"].rstrip("/"))
            else:
                raise InteractiveImportError(f"Invalid native artifact entry for {upstream_id}")
        notes = [
            "Offline 30-minute harness variant; compare only matching network, tools and budgets.",
            "Upstream grader and oracle are private; import does not run either on the host.",
        ]
        if environment.get("allow_internet") or suite_id == "terminal-bench-4":
            notes.append(
                "Upstream permits external networking. Offline task feasibility must be verified "
                "during preparation; this variant is not the canonical leaderboard condition."
            )
        settings: dict[str, Any] = {
            "schema_version": config.get("schema_version", config.get("version", "1.0")),
            "build_context": "environment",
            "dockerfile": "environment/Dockerfile",
            "environment": environment,
            "verifier": verifier,
            "agent": agent,
            "artifacts": artifacts,
            "upstream_artifacts": upstream_artifacts,
            "verifier_environment_mode": verifier.get("environment_mode", "same"),
            "requires_filesystem_snapshot": not bool(upstream_artifacts),
            "upstream_category": metadata.get("category"),
            "upstream_task": task,
        }
        tasks.append(
            _reviewed_harbor(
                ImportedTask(
                    suite_id=suite_id,
                    upstream_id=upstream_id,
                    revision=revision,
                    source_sha256=source_sha256,
                    title=str(task.get("description") or upstream_id)[:300],
                    prompt=prompt,
                    category="terminal",
                    difficulty=_difficulty(metadata.get("difficulty")),
                    variant=variant or _DEFAULT_VARIANT,
                    public_files={"TASK.md": prompt},
                    private_files={"upstream-task.toml": _text(root, path)},
                    assets=assets,
                    submission_file="submission.json",
                    submission_kind="workspace",
                    recipe=NativeRecipe(
                        family="harbor",
                        image=image,
                        grader_command=["bash", "/tests/test.sh"],
                        grader_result_path="/logs/verifier/reward.txt",
                        grader_format="harbor-reward",
                        workdir="/app",
                        settings=settings,
                        requirements=requirements,
                        blockers=blockers,
                    ),
                    source_urls=[
                        _URLS.get(suite_id, "https://harborframework.com/docs/task-format")
                    ],
                    licenses=["Apache-2.0", "Task assets retain their upstream licenses"],
                    tags=["imported", "harbor", *map(str, metadata.get("tags", []))],
                    comparison_notes=notes,
                ),
                folder,
            )
        )
    return tasks


def _records(root: Path, candidates: list[str]) -> list[dict[str, Any]]:
    path = next((root / name for name in candidates if (root / name).is_file()), None)
    if path is None:
        raise InteractiveImportError("Missing prepared dataset: " + " or ".join(candidates))
    raw = json.loads(_text(root, path))
    if not isinstance(raw, list) or not raw or any(not isinstance(item, dict) for item in raw):
        raise InteractiveImportError("Dataset must be a nonempty JSON array of native task objects")
    return raw


def _browser_recipe(
    suite_id: str, config: dict[str, Any], *, blockers: list[str] | None = None
) -> NativeRecipe:
    requirements = [
        "A digest-pinned BrowserGym worker image with locked wheels and prepared Chromium",
        "Controller-owned worker; private task configuration is never available through file tools",
    ]
    if suite_id == "miniwob":
        requirements += [
            "browsergym-miniwob==0.14.3",
            "browsergym-core==0.14.3",
            "playwright==1.44.0",
        ]
    if suite_id != "miniwob":
        requirements += [
            "Prepared NLTK tokenizers/punkt_tab (adapter imports must never download it)",
            "Fresh owned website snapshots on an isolated task-local network for each attempt",
            "An explicit URL map and verified per-attempt service reset receipt",
        ]
    if suite_id == "visualwebarena":
        requirements += [
            "Prepared Salesforce/blip2-flan-t5-xl weights with HF/Transformers offline mode",
            "All goal images mirrored locally with checksums and explicit URL replacements",
        ]
    return NativeRecipe(
        family="browsergym",
        grader_command=[
            "python",
            "/runtime/interactive_browser.py",
            "--grade",
            "/tmp/benchmaxxing-grade/result.json",
        ],
        grader_result_path="/tmp/benchmaxxing-grade/result.json",
        tools="browser",
        network="none" if suite_id == "miniwob" else "task-local",
        settings={
            "worker_command": [
                "python",
                "/runtime/interactive_browser.py",
                "--config",
                "/grader/browser.json",
            ],
            "worker_protocol": "benchmaxxing-browser-jsonl-v1",
            "requires_fresh_services": suite_id != "miniwob",
            "observation_profiles": ["axtree", "screenshot"],
            "browser_config": config,
            "probe_modules": ["browsergym.miniwob"] if suite_id == "miniwob" else [],
            "probe_files": ["/opt/playwright/chromium-1117/chrome-linux/chrome"]
            if suite_id == "miniwob"
            else [],
        },
        requirements=requirements,
        blockers=blockers or [],
    )


def _miniwob(root: Path, revision: str, digest: str, variant: str | None) -> list[ImportedTask]:
    html = next(
        (
            path
            for path in (root / "miniwob/html", root / "html", root)
            if (path / "miniwob").is_dir()
        ),
        None,
    )
    if html is None:
        raise InteractiveImportError(
            "Expected MiniWoB html/miniwob/*.html and sibling static assets"
        )
    # Keep all supporting scripts/CSS/images. One generated HTML file is not a complete task.
    assets = _tree(root, html, "miniwob/html", "runtime")
    files = sorted((html / "miniwob").glob("*.html"))
    if not files:
        raise InteractiveImportError("No MiniWoB HTML tasks found")
    tasks = []
    for path in files:
        name = path.stem
        if name in _MINIWOB_UNREGISTERED:
            continue
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name):
            raise InteractiveImportError(f"Unsupported MiniWoB task filename: {path.name}")
        prompt = (
            f"Complete MiniWoB task {name}. The browser reset supplies the seeded task goal. "
            "Use the browser observation and action tools, then submit when finished."
        )
        config = {
            "schema_version": 1,
            "suite_id": "miniwob",
            "task_id": name,
            "miniwob_base_url": "file:///runtime/miniwob/html/miniwob/",
            "seed": 0,
        }
        tasks.append(
            ImportedTask(
                suite_id="miniwob",
                upstream_id=f"miniwob.{name}",
                revision=revision,
                source_sha256=digest,
                title=f"MiniWoB: {name}",
                prompt=prompt,
                category="browser",
                variant=variant or _DEFAULT_VARIANT,
                public_files={"TASK.md": prompt},
                private_files={"browser.json": json.dumps(config, sort_keys=True)},
                assets=assets,
                submission_file="response.txt",
                submission_kind="text",
                recipe=_browser_recipe("miniwob", config),
                source_urls=[_URLS["miniwob"]],
                licenses=["MIT (MiniWoB++)", "Apache-2.0 (BrowserGym)"],
                tags=["imported", "browsergym", "seeded"],
                check_count=1,
                comparison_notes=[
                    "Seed and actual reset goal are recorded for each attempt.",
                    "The bounded structured action set excludes arbitrary Python and file uploads.",
                ],
            )
        )
    return tasks


def _browser_tasks(
    root: Path, suite_id: str, revision: str, digest: str, variant: str | None
) -> list[ImportedTask]:
    if suite_id == "webarena-verified":
        records = _records(
            root, ["assets/dataset/webarena-verified.json", "webarena-verified.json"]
        )
    elif suite_id == "visualwebarena":
        # BrowserGym's merged native dataset has globally unique IDs. Site-split
        # originals reuse IDs; assigning offsets silently would invent identities.
        records = _records(root, ["test_raw.json", "visualwebarena/test_raw.json"])
    else:
        records = _records(root, ["workarena-tasks.json"])
    tasks = []
    identifiers = set()
    for record in records:
        task_id = record.get("task_id")
        if isinstance(task_id, bool) or not isinstance(task_id, (str, int)):
            raise InteractiveImportError("Every browser task must have a native task_id")
        if str(task_id) in identifiers:
            raise InteractiveImportError(f"Duplicate browser task_id: {task_id}")
        identifiers.add(str(task_id))
        prompt = record.get("intent", record.get("goal"))
        if not isinstance(prompt, str) or not prompt.strip():
            raise InteractiveImportError(f"Browser task {task_id} is missing its public intent")
        if suite_id != "workarena" and "eval" not in record:
            raise InteractiveImportError(f"Browser task {task_id} is missing its native evaluator")
        upstream_id = f"{suite_id}.{task_id}"
        if suite_id == "webarena-verified":
            if "revision" not in record or "intent_template_id" not in record:
                raise InteractiveImportError(
                    "Verified tasks require native revision and template ID"
                )
            upstream_id = (
                f"webarena_verified.{record['intent_template_id']}.{task_id}.{record['revision']}"
            )
        config = {
            "schema_version": 1,
            "suite_id": suite_id,
            "task_id": task_id,
            "task_config_path": "/grader/task.json",
            "seed": 0,
        }
        blockers = []
        if suite_id == "workarena":
            blockers.append("WorkArena requires a hosted licensed ServiceNow instance; not offline")
        if suite_id == "visualwebarena":
            serialized_eval = json.dumps(record["eval"], sort_keys=True)
            if any(key in serialized_eval for key in ('"fuzzy_match"', '"ua_match"')):
                blockers.append(
                    "Native evaluator invokes an external model judge; requires a separately "
                    "identified and prepared local-judge variant"
                )
        recipe = _browser_recipe(suite_id, config, blockers=blockers)
        if suite_id == "workarena":
            recipe.network = "external"
        tasks.append(
            ImportedTask(
                suite_id=suite_id,
                upstream_id=upstream_id,
                revision=revision,
                source_sha256=digest,
                title=f"{suite_id}: {task_id}",
                prompt=prompt,
                category="browser",
                difficulty=_difficulty(record.get("overall_difficulty")),
                variant=variant or _DEFAULT_VARIANT,
                public_files={"TASK.md": prompt},
                private_files={
                    "task.json": json.dumps(record, sort_keys=True, ensure_ascii=False),
                    "browser.json": json.dumps(config, sort_keys=True),
                },
                submission_file="response.txt",
                submission_kind="text",
                recipe=recipe,
                source_urls=[_URLS[suite_id], "https://github.com/ServiceNow/BrowserGym"],
                licenses=["MIT" if suite_id == "visualwebarena" else "Apache-2.0"],
                tags=["imported", "browsergym", *map(str, record.get("sites", []))],
                comparison_notes=[
                    "Native task/evaluator identity retains the exact private configuration.",
                    "Offline 30-minute structured-action variant; no Python or file uploads.",
                    "The worker owns browser state and evaluation, keeping answers private.",
                ],
            )
        )
    return tasks


def convert_interactive(
    suite_id: str, source: Path, *, revision: str, source_sha256: str, variant: str | None = None
) -> list[ImportedTask]:
    """Convert a frozen, locally prepared source directory without executing it."""
    root = Path(source).absolute()
    if root.is_symlink() or not root.is_dir():
        raise InteractiveImportError("Interactive source must be a regular directory")
    if suite_id in ("terminal-bench-2-1", "terminal-bench-4"):
        return convert_harbor(
            root, suite_id=suite_id, revision=revision, source_sha256=source_sha256, variant=variant
        )
    if suite_id == "miniwob":
        return _miniwob(root, revision, source_sha256, variant)
    if suite_id in ("webarena-verified", "visualwebarena", "workarena"):
        return _browser_tasks(root, suite_id, revision, source_sha256, variant)
    raise InteractiveImportError(f"Unsupported interactive suite: {suite_id}")
