"""Offline converters for security and refusal suites.

These functions read already downloaded, immutable source snapshots. They never
run upstream setup scripts, import upstream Python, install packages or download.
Native requirements and blockers remain explicit until preparation verifies them.
"""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from benchmaxxing.providers.contracts import ImportedTask, NativeRecipe, SourceAsset, relative_path

SOURCES = {
    "intercode-ctf": "https://github.com/princeton-nlp/intercode",
    "cybench": "https://github.com/andyzorigin/cybench",
    "cybergym": "https://github.com/sunblaze-ucb/cybergym",
    "xstest": "https://github.com/paul-rottger/xstest",
    "harmbench": "https://github.com/centerforaisafety/HarmBench",
}
SPECIALIZED_SUITES = frozenset(SOURCES)
GRADER_RESULT = "/tmp/benchmaxxing-grade/result.json"
CYBERGYM_FULL_BYTES = 10_000_000_000_000


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _path(root: Path, relative: str) -> Path:
    relative_path(relative)
    result = root / relative
    if not result.resolve().is_relative_to(root.resolve()):
        raise ValueError("Source asset escapes its prepared root")
    for item in (result, *result.parents):
        if item == root.parent:
            break
        if item.is_symlink():
            raise ValueError("Symbolic links are not accepted as imported assets")
    return result


def _text(path: Path) -> str:
    if path.stat().st_size > 16_000_000:
        raise ValueError("Text source exceeds the 16 MB converter limit")
    return path.read_text(encoding="utf-8-sig")


def _records(path: Path) -> list[dict[str, Any]]:
    if path.suffix == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as stream:
            rows: Any = list(csv.DictReader(stream))
    elif path.suffix == ".jsonl":
        rows = [json.loads(line) for line in _text(path).splitlines() if line.strip()]
    else:
        rows = json.loads(_text(path))
        if isinstance(rows, dict):
            rows = rows.get("tasks", rows.get("records", rows))
        if isinstance(rows, dict):
            rows = [dict(value, task_id=key) for key, value in rows.items()]
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("Expected an array of task records")
    return rows


def _required(row: dict[str, Any], name: str) -> str:
    value = row.get(name)
    if not isinstance(value, (str, int)) or not str(value).strip():
        raise ValueError(f"Missing required upstream field: {name}")
    return str(value)


def _asset(
    root: Path, source: str, target: str, scope: Literal["candidate", "grader", "runtime"]
) -> SourceAsset:
    path = _path(root, source)
    if not path.is_file():
        raise ValueError(f"Missing prepared asset: {source}")
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return SourceAsset(source=source, target=target, scope=scope, sha256=digest)


def _tree(
    root: Path, directory: Path, scope: Literal["candidate", "grader", "runtime"], prefix: str = ""
) -> list[SourceAsset]:
    assets: list[SourceAsset] = []
    if not directory.is_dir():
        return assets
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError("Symbolic links are not accepted as imported assets")
        if path.is_file():
            target = prefix + path.relative_to(directory).as_posix()
            assets.append(_asset(root, path.relative_to(root).as_posix(), target, scope))
    return assets


def _helper(name: str = "specialized_grader.py") -> str:
    return _text(Path(__file__).resolve().parents[1] / "runtimes" / name)


def _grader(mode: str, submission: str) -> list[str]:
    return [
        "python",
        "/grader/specialized_grader.py",
        mode,
        "--submission",
        f"/workspace/{submission}",
        "--reference",
        "/grader/reference.json",
        "--output",
        GRADER_RESULT,
    ]


def _task(
    suite: str,
    row_id: str,
    prompt: str,
    recipe: NativeRecipe,
    revision: str,
    digest: str,
    variant: str | None,
    **values: Any,
) -> ImportedTask:
    return ImportedTask(
        suite_id=suite,
        upstream_id=row_id,
        revision=revision,
        source_sha256=digest,
        title=values.pop("title", f"{suite} · {row_id}")[:300],
        prompt=prompt,
        category=values.pop("category", "security"),
        recipe=recipe,
        variant=variant or "benchmaxxing-offline-30m-v1",
        source_urls=[SOURCES[suite]],
        **values,
    )


def _refusal(
    suite: str, source: Path, revision: str, digest: str, variant: str | None
) -> list[ImportedTask]:
    filename = (
        "xstest_prompts.csv"
        if suite == "xstest"
        else ("data/behavior_datasets/harmbench_behaviors_text_all.csv")
    )
    file = source if source.is_file() else _path(source, filename)
    rows = _records(file)
    if suite == "harmbench" and source.is_dir():
        multimodal = _path(source, "data/behavior_datasets/harmbench_behaviors_multimodal_all.csv")
        if multimodal.is_file():
            rows.extend(_records(multimodal))
    tasks = []
    for row in rows:
        if suite == "xstest":
            task_id, prompt = _required(row, "id"), _required(row, "prompt")
            label = _required(row, "label")
            if label not in {"safe", "unsafe"}:
                raise ValueError("XSTest label must be safe or unsafe")
            rubric: dict[str, Any] = {
                "expected_behavior": "comply" if label == "safe" else "refuse",
                "labels": ["full_compliance", "partial_refusal", "full_refusal"],
                "upstream": row,
            }
            licenses = ["CC-BY-4.0"]
        else:
            task_id, prompt = _required(row, "BehaviorID"), _required(row, "Behavior")
            context = str(row.get("ContextString", ""))
            if context:
                prompt = context + "\n\n---\n\n" + prompt
            rubric = {
                "expected_behavior": "refuse",
                "upstream": row,
                "metric": "harmful_behavior_completion",
                "classifier_label": None,
            }
            licenses = ["MIT"]
        recipe = NativeRecipe(
            family="refusal-response",
            tools="response",
            grader_command=_grader("refusal", "response.txt"),
            grader_result_path=GRADER_RESULT,
            requirements=["A recorded human annotation or prepared local judge for assessment"],
            settings={"assessment_status": "ungraded", "provider_refusal_is_not_a_grade": True},
        )
        assets = []
        if suite == "harmbench":
            recipe.settings["native_evaluation"] = {
                "argv": [
                    "python",
                    "evaluate_completions.py",
                    "--cls_path",
                    "/models/harmbench-cls",
                    "--behaviors_path",
                    "/grader/behaviors.csv",
                    "--completions_path",
                    "/grader/completions.json",
                    "--save_path",
                    "/grader/evaluation.json",
                ],
                "format": "BehaviorID -> [{test_case, generation, label}]",
                "judge": "cais/HarmBench-Llama-2-13b-cls",
                "offline_env": {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"},
            }
            if row.get("ImageFileName"):
                image_name = _required(row, "ImageFileName")
                relative_path(image_name)
                image_path = f"data/multimodal_behavior_images/{image_name}"
                # Seven frozen CSV rows retain JPEG names while the published
                # assets are PNG. Preserve the declared name and exact mapping.
                if source.is_dir() and not _path(source, image_path).is_file():
                    png_path = str(Path(image_path).with_suffix(".png").as_posix())
                    if _path(source, png_path).is_file():
                        recipe.settings["image_filename_mapping"] = {
                            "declared": image_path,
                            "resolved": png_path,
                            "reason": "Published asset uses PNG for the same exact basename",
                        }
                        image_path = png_path
                        image_name = Path(image_name).with_suffix(".png").as_posix()
                recipe.settings["image_assets"] = [f"images/{image_name}"]
                recipe.settings["modality"] = "image-text"
                recipe.settings["native_evaluation"]["judge"] = (
                    "cais/HarmBench-Llama-2-13b-cls-multimodal-behaviors"
                )
                recipe.requirements.append(
                    "Vision-capable participant with image attachment support"
                )
                if source.is_dir() and _path(source, image_path).is_file():
                    assets.append(_asset(source, image_path, f"images/{image_name}", "candidate"))
                else:
                    recipe.blockers.append(f"Missing prepared multimodal reference: {image_path}")
            else:
                recipe.settings["modality"] = "text"
        tasks.append(
            _task(
                suite,
                task_id,
                prompt,
                recipe,
                revision,
                digest,
                variant,
                category="refusal",
                submission_file="response.txt",
                submission_kind="text",
                assets=assets,
                private_files={"reference.json": _json(rubric), "specialized_grader.py": _helper()},
                licenses=licenses,
                tags=[suite, str(row.get("type", row.get("FunctionalCategory", "")))],
                comparison_notes=[
                    "Prompt-only response track; no jailbreak search or substring grader."
                ],
            )
        )
    return tasks


def _intercode(source: Path, revision: str, digest: str, variant: str | None) -> list[ImportedTask]:
    root = source.parent if source.is_file() else source
    file = source if source.is_file() else _path(source, "data/ctf/ic_ctf.json")
    tasks = []
    for row in _records(file):
        task_id = _required(row, "task_id")
        relative_path(task_id)
        asset_dir = file.parent / "task_assets" / task_id
        assets = []
        for asset in _tree(root, asset_dir, "candidate"):
            if "solution" in Path(asset.target).parts:
                asset.scope = "grader"
                asset.target = "upstream-solution/" + asset.target
            assets.append(asset)
        recipe = NativeRecipe(
            family="intercode-ctf",
            image="benchmaxxing/intercode-ctf-tools:1",
            tools="workspace",
            grader_command=_grader("flag", "flag.txt"),
            grader_result_path=GRADER_RESULT,
            requirements=[
                "Build and pin upstream InterCode CTF tool image during preparation",
                "Stage only this task's files; no gold dataset in candidate image",
            ],
            settings={
                "upstream_submit": "submit <flag>",
                "flag_case_sensitive": False,
                "upstream_setup": row.get("setup"),
                "asset_directory": asset_dir.relative_to(root).as_posix(),
                "runtime_dockerfile": "specialized_intercode.Dockerfile",
            },
        )
        if row.get("setup"):
            recipe.blockers.append("Upstream setup must be materialized offline before execution")
        prompt = _required(row, "query")
        if "nc " in prompt or "http://" in prompt or "https://" in prompt:
            recipe.blockers.append(
                "External task service requires a separately identified local variant"
            )
        tasks.append(
            _task(
                "intercode-ctf",
                task_id,
                prompt,
                recipe,
                revision,
                digest,
                variant,
                assets=assets,
                submission_file="flag.txt",
                submission_kind="text",
                check_count=1,
                private_files={
                    "reference.json": _json(
                        {"flag": _required(row, "gold"), "case_sensitive": False}
                    ),
                    "specialized_grader.py": _helper(),
                },
                licenses=["MIT", "Upstream challenge assets retain their licenses"],
                tags=[str(value) for value in row.get("tags", [])],
                comparison_notes=[
                    "Case-insensitive exact submitted-flag comparison follows CTFEnv."
                ],
            )
        )
    return tasks


def _cybench(source: Path, revision: str, digest: str, variant: str | None) -> list[ImportedTask]:
    if not source.is_dir():
        raise ValueError("CyBench source must be a prepared repository or task directory")
    metadata_files = sorted(source.rglob("metadata/metadata.json"))
    cohort = source / "task_list.txt"
    materialization = source / "benchmaxxing-source-materialization.json"
    missing = []
    if cohort.is_file():
        names = [line.strip() for line in _text(cohort).splitlines() if line.strip()]
        if len(names) != len(set(names)):
            raise ValueError("CyBench cohort has duplicate task identities")
        metadata_files = []
        for name in names:
            path = _path(source, name + "/metadata/metadata.json")
            if not path.is_file() and materialization.is_file():
                missing.append(name)
            else:
                metadata_files.append(path)
    provenance = []
    for name in ["benchmaxxing-source-materialization.json", "LICENSE"]:
        if (source / name).is_file():
            provenance.append(_asset(source, name, "provenance/" + name, "grader"))
    tasks = []
    for file in metadata_files:
        directory = file.parent.parent
        row = json.loads(_text(file))
        subtasks = row.get("subtasks")
        if not isinstance(subtasks, list) or not subtasks:
            raise ValueError("CyBench task requires final-subtask answer metadata")
        task_id = directory.relative_to(source).as_posix()
        if task_id == ".":
            task_id = directory.name
        recipe = NativeRecipe(
            family="cybench",
            network="task-local" if row.get("target_host") else "none",
            grader_command=_grader("flag", "flag.txt"),
            grader_result_path=GRADER_RESULT,
            requirements=[
                "Pinned CyBench agent image and task server images",
                "Materialize init_script.sh inputs in isolation during preparation",
            ],
            settings={
                "mode": "unguided",
                "target_host": row.get("target_host", ""),
                "tools": row.get("tools", {}),
                "stateful_shell": row.get("stateful_shell"),
                "task_directory": directory.relative_to(source).as_posix(),
                "upstream_subtask_count": len(subtasks),
                "missing_upstream_tasks": missing,
            },
        )
        if row.get("target_host"):
            recipe.blockers.append("Task-local service topology must be prepared and verified")
        if row.get("internet", {}).get("necessary"):
            recipe.blockers.append("Task declares necessary internet; offline variant required")
        # init_script is upstream executable code, never run on the controller.
        runtime_assets = _tree(source, directory, "runtime", "task/")
        for asset in runtime_assets:
            if "metadata" in Path(asset.target).parts:
                asset.scope = "grader"
                asset.target = "upstream/" + asset.target
        prepared = directory / "benchmaxxing-inputs"
        if not prepared.is_dir():
            recipe.blockers.append(
                "Missing prepared benchmaxxing-inputs from upstream init_script.sh"
            )
        tasks.append(
            _task(
                "cybench",
                task_id,
                _required(row, "hard_prompt"),
                recipe,
                revision,
                digest,
                variant or ("benchmaxxing-offline-30m-available-cohort-v1" if missing else None),
                assets=[*runtime_assets, *_tree(source, prepared, "candidate"), *provenance],
                submission_file="flag.txt",
                submission_kind="text",
                check_count=1,
                private_files={
                    "reference.json": _json(
                        {"flag": _required(subtasks[-1], "answer"), "case_sensitive": True}
                    ),
                    "upstream-metadata.json": _json(row),
                    "specialized_grader.py": _helper(),
                },
                licenses=["Apache-2.0", "Challenge-specific upstream licenses"],
                tags=[str(value) for value in row.get("categories", [])],
                comparison_notes=[
                    "Unguided final-flag track; subtask-guided results are separate.",
                    *(
                        [
                            f"Only {len(metadata_files)} declared tasks are available; "
                            f"{len(missing)} listed upstream tasks are absent from the release."
                        ]
                        if missing
                        else []
                    ),
                ],
            )
        )
    return tasks


def _cybergym(source: Path, revision: str, digest: str, variant: str | None) -> list[ImportedTask]:
    file = source if source.is_file() else _path(source, "tasks.json")
    tasks = []
    for row in _records(file):
        task_id = _required(row, "task_id")
        recipe = NativeRecipe(
            family="cybergym",
            network="task-local",
            requirements=[
                "Pinned vulnerable and patched verifier images",
                "Local CyberGym server",
                "Controller-brokered PoC submission and verification",
            ],
            blockers=[
                "Full CyberGym server data (~10 TB) exceeds the total 250 GB preparation cap"
            ],
            settings={
                "minimum_full_server_bytes": CYBERGYM_FULL_BYTES,
                "preparation_state": "blocked_size_budget",
                "upstream_record": row,
                "task_generation": [
                    "python",
                    "-m",
                    "cybergym.task.gen_task",
                    "--task-id",
                    task_id,
                    "--difficulty",
                    "level1",
                ],
                "verification": "scripts/verify_agent_result.py; vulnerable AND patched runs",
            },
        )
        tasks.append(
            _task(
                "cybergym",
                task_id,
                str(
                    row.get("vulnerability_description")
                    or row.get("description")
                    or row.get("description.txt")
                    or "Reproduce the vulnerability described in the prepared task."
                ),
                recipe,
                revision,
                digest,
                variant,
                submission_file="poc",
                submission_kind="binary",
                private_files={"upstream-record.json": _json(row)},
                licenses=["Apache-2.0"],
                comparison_notes=[
                    "Full-suite entry remains blocked; no subset is presented as full coverage.",
                    "A crash alone is not a valid differential reproduction assessment.",
                ],
            )
        )
    return tasks


def convert_specialized(
    suite_id: str, source: Path, *, revision: str, source_sha256: str, variant: str | None = None
) -> list[ImportedTask]:
    """Convert already prepared upstream data; no network and no host execution."""
    if source.is_symlink():
        raise ValueError("Source must not be a symbolic link")
    functions = {
        "intercode-ctf": _intercode,
        "cybench": _cybench,
        "cybergym": _cybergym,
    }
    if suite_id in {"xstest", "harmbench"}:
        tasks = _refusal(suite_id, source, revision, source_sha256, variant)
    elif suite_id in functions:
        tasks = functions[suite_id](source, revision, source_sha256, variant)
    else:
        raise ValueError(f"Unknown specialized suite: {suite_id}")
    if not tasks:
        raise ValueError("No upstream tasks found; source is not a materialized evaluation dataset")
    identities = [task.upstream_id for task in tasks]
    if len(identities) != len(set(identities)):
        raise ValueError("Duplicate upstream task identity")
    return tasks
