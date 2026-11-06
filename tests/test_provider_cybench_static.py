"""Data-only Cybench preparation checks; synthetic challenge bytes are never run."""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from benchmaxxing.environment import PreparationError
from benchmaxxing.providers import cybench_static, registry, runtime
from benchmaxxing.providers.contracts import ImportSettings
from benchmaxxing.providers.preparation import _validate_recipe
from benchmaxxing.providers.specialized import convert_specialized
from benchmaxxing.providers.specialized_sources import PINS
from benchmaxxing.runtimes.specialized_grader import grade_flag


def _write(root: Path, name: str, content: bytes | str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode() if isinstance(content, str) else content)
    return path


def _hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data_dir = tmp_path / "data"
    cache = tmp_path / "cache"
    registry.save_settings(
        data_dir,
        ImportSettings(
            cache_dir=str(cache), storage_limit_bytes=1_000_000_000, reserve_free_bytes=0
        ),
    )
    source = cache / "sources" / "cybench" / f"{PINS['cybench']}-task-inputs-v1"
    upstream_id = "benchmark/fixture/[Very Easy] Static Puzzle"
    script = b'cp challenge/input.txt "$1"\n'
    plain = b"public puzzle input"
    member = b"synthetic executable bytes"
    _write(source, f"{upstream_id}/init_script.sh", script)
    _write(source, f"{upstream_id}/challenge/input.txt", plain)
    _write(source, f"{upstream_id}/challenge/secret.py", "FLAG = 'fixture-private'\n")
    archive = source / upstream_id / "release" / "input.zip"
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("inside/payload.bin", member)
    _write(
        source,
        f"{upstream_id}/metadata/metadata.json",
        json.dumps(
            {
                "hard_prompt": "Inspect input.txt and payload.bin; write the final flag.",
                "target_host": "",
                "subtasks": [{"answer": "FLAG{fixture-private}"}],
            }
        ),
    )
    source_hash = registry.source_hash(source)
    (original,) = convert_specialized(
        "cybench", source, revision=PINS["cybench"], source_sha256=source_hash
    )
    registry.install(data_dir, source, [original])
    _write(
        cache,
        "manifests/cybench.prepared-source.json",
        json.dumps(
            {
                "suite_id": "cybench",
                "source_path": str(source.resolve()),
                "revision": PINS["cybench"],
                "source_sha256": source_hash,
            }
        ),
    )
    plan = cybench_static.StaticPlan(
        schema_version=1,
        source_revision=PINS["cybench"],
        source_sha256=source_hash,
        base_variant=original.variant,
        variant="benchmaxxing-offline-30m-static-test-v1",
        tasks=[
            cybench_static.StaticTask(
                upstream_id=upstream_id,
                init_script_sha256=_hash(script),
                inputs=[
                    cybench_static.StaticInput(
                        kind="copy", source="challenge/input.txt", target="input.txt",
                        size=len(plain), sha256=_hash(plain),
                    ),
                    cybench_static.StaticInput(
                        kind="zip_member", source="release/input.zip",
                        member="inside/payload.bin", target="payload.bin",
                        size=len(member), sha256=_hash(member),
                        archive_sha256=registry.file_hash(archive), executable=True,
                    ),
                ],
            )
        ],
    )
    plan_path = tmp_path / "reviewed-plan.json"
    plan_path.write_text(plan.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(cybench_static, "_PLAN_PATH", plan_path)
    return data_dir, original, plan, plain, member


def test_static_variant_keeps_private_material_outside_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir, original, plan, plain, member = _fixture(tmp_path, monkeypatch)
    image = "sha256:" + "a" * 64
    monkeypatch.setattr(cybench_static, "_inspect_image", lambda _: {"Id": image, "Os": "linux"})
    captured = []

    def fake_prepare(_data_dir, tasks, *, image, pull):
        captured.extend(tasks)
        assert not pull and image == "sha256:" + "a" * 64
        for task in tasks:
            _validate_recipe(task)
        return {"prepared_tasks": len(tasks), "task_ids": [task.id for task in tasks], "errors": {}}

    monkeypatch.setattr(cybench_static, "prepare_suite", fake_prepare)
    result = cybench_static.prepare_static_runtime(
        data_dir, [original], image=image, pull=False
    )
    assert result["reviewed_tasks"] == 1
    assert result["variant"] == plan.variant
    (adapted,) = captured
    assert adapted.id != original.id
    assert adapted.source_sha256 == original.source_sha256
    assert adapted.upstream_id == original.upstream_id
    assert adapted.prompt.startswith(original.prompt + "\n\n")
    assert "/workspace: input.txt, payload.bin" in adapted.prompt
    assert adapted.recipe.network == "none" and adapted.recipe.blockers == []
    assert adapted.recipe.settings["candidate_executable_files"] == ["payload.bin"]
    candidate = {asset.target: asset for asset in adapted.assets if asset.scope == "candidate"}
    assert set(candidate) == {"input.txt", "payload.bin"}
    cache = tmp_path / "cache"
    assert (cache / candidate["input.txt"].source).read_bytes() == plain
    assert (cache / candidate["payload.bin"].source).read_bytes() == member
    assert not any("secret" in asset.source for asset in candidate.values())
    assert "reference.json" not in candidate
    assert "FLAG{fixture-private}" not in adapted.prompt
    assert "FLAG{fixture-private}" in adapted.private_files["reference.json"]
    reference = json.loads(adapted.private_files["reference.json"])["flag"]
    assert reference.encode() not in b"".join(
        (cache / asset.source).read_bytes() for asset in candidate.values()
    )
    assert grade_flag(reference, reference)["passed"]
    assert not grade_flag("FLAG{incorrect}", reference)["passed"]
    assert registry.get_imported(data_dir, original.id).task_hash == original.task_hash
    assert registry.get_imported(data_dir, adapted.id).task_hash == adapted.task_hash


def test_ten_static_tasks_prepare_idempotently_in_separate_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "isolated-data"
    cache = tmp_path / "isolated-cache"
    registry.save_settings(
        data_dir,
        ImportSettings(
            cache_dir=str(cache), storage_limit_bytes=1_000_000_000, reserve_free_bytes=0
        ),
    )
    source = cache / "sources" / "cybench" / f"{PINS['cybench']}-task-inputs-v1"
    entries = []
    for index in range(10):
        upstream_id = f"benchmark/fixture/static-{index}"
        script = b'cp challenge/input.txt "$1"\n'
        payload = f"public-{index}".encode()
        _write(source, f"{upstream_id}/init_script.sh", script)
        _write(source, f"{upstream_id}/challenge/input.txt", payload)
        _write(
            source,
            f"{upstream_id}/metadata/metadata.json",
            json.dumps(
                {"hard_prompt": "Inspect input.txt.", "subtasks": [{"answer": "synthetic"}]}
            ),
        )
        entries.append(
            cybench_static.StaticTask(
                upstream_id=upstream_id,
                init_script_sha256=_hash(script),
                inputs=[
                    cybench_static.StaticInput(
                        kind="copy", source="challenge/input.txt", target="input.txt",
                        size=len(payload), sha256=_hash(payload),
                    )
                ],
            )
        )
    source_hash = registry.source_hash(source)
    originals = convert_specialized(
        "cybench", source, revision=PINS["cybench"], source_sha256=source_hash
    )
    registry.install(data_dir, source, originals)
    _write(
        cache,
        "manifests/cybench.prepared-source.json",
        json.dumps({
            "suite_id": "cybench", "source_path": str(source.resolve()),
            "revision": PINS["cybench"], "source_sha256": source_hash,
        }),
    )
    plan = cybench_static.StaticPlan(
        schema_version=1, source_revision=PINS["cybench"], source_sha256=source_hash,
        base_variant=originals[0].variant, variant="benchmaxxing-offline-30m-static-test-v1",
        tasks=entries,
    )
    plan_path = tmp_path / "ten-plan.json"
    plan_path.write_text(plan.model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(cybench_static, "_PLAN_PATH", plan_path)
    image = "sha256:" + "a" * 64
    monkeypatch.setattr(cybench_static, "_inspect_image", lambda _: {"Id": image, "Os": "linux"})

    def fake_prepare(_data_dir, tasks, *, image, pull):
        assert not pull and len(tasks) == 10
        for task in tasks:
            _validate_recipe(task)
        return {"prepared_tasks": len(tasks), "task_ids": [task.id for task in tasks], "errors": {}}

    monkeypatch.setattr(cybench_static, "prepare_suite", fake_prepare)
    first = cybench_static.prepare_static_runtime(data_dir, originals, image=image, pull=False)
    again = cybench_static.prepare_static_runtime(data_dir, originals, image=image, pull=False)
    assert first["prepared_tasks"] == again["prepared_tasks"] == 10
    assert first["task_ids"] == again["task_ids"]
    assert registry.source_hash(source) == source_hash
    assert all(
        registry.get_imported(data_dir, task.id).task_hash == task.task_hash
        for task in originals
    )


def test_static_zip_rejects_traversal_even_when_unselected(tmp_path: Path) -> None:
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("../escape", b"wrong")
        package.writestr("inside/payload.bin", b"selected")
    item = cybench_static.StaticInput(
        kind="zip_member", source="release/bad.zip", member="inside/payload.bin",
        target="payload.bin", size=8, sha256=_hash(b"selected"),
        archive_sha256=registry.file_hash(archive),
    )
    with pytest.raises(ValueError):
        cybench_static._zip_member_bytes(archive, item)


def test_bad_zip_member_hash_cannot_publish_a_variant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir, original, plan, _, _ = _fixture(tmp_path, monkeypatch)
    plan.tasks[0].inputs[1].sha256 = "0" * 64
    cybench_static._PLAN_PATH.write_text(plan.model_dump_json(), encoding="utf-8")
    image = "sha256:" + "a" * 64
    monkeypatch.setattr(cybench_static, "_inspect_image", lambda _: {"Id": image, "Os": "linux"})
    with pytest.raises(ValueError, match="ZIP member hash differs"):
        cybench_static.prepare_static_runtime(data_dir, [original], image=image, pull=False)
    assert [task.id for task in registry.imported_tasks(data_dir, "cybench")] == [original.id]


def test_static_preparation_requires_existing_pinned_image(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="pinned Linux image"):
        cybench_static.prepare_static_runtime(tmp_path, [], image=None, pull=False)
    with pytest.raises(ValueError, match="never pulls"):
        cybench_static.prepare_static_runtime(
            tmp_path, [], image="sha256:" + "a" * 64, pull=True
        )


def test_executable_declaration_must_name_candidate_basename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, original, _, _, _ = _fixture(tmp_path, monkeypatch)
    original.recipe.blockers = []
    original.recipe.settings["candidate_executable_files"] = ["../secret.py"]
    with pytest.raises(PreparationError, match="Executable candidate"):
        _validate_recipe(original)


def test_runtime_routes_cybench_image_to_static_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, original, _, _, _ = _fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(runtime, "check_preparation_scope", lambda *_: None)
    monkeypatch.setattr(runtime.registry, "imported_tasks", lambda *_: [original])
    calls = []

    def fake_static(_data_dir, tasks, *, image, pull):
        calls.append((tasks, image, pull))
        return {"prepared_tasks": 1}

    monkeypatch.setattr(cybench_static, "prepare_static_runtime", fake_static)
    result = runtime.prepare_runtime(
        tmp_path, "cybench", image="sha256:" + "a" * 64, pull=False
    )
    assert result["prepared_tasks"] == 1
    assert calls == [([original], "sha256:" + "a" * 64, False)]


def test_reviewed_static_plan_excludes_hidden_source_files() -> None:
    plan = cybench_static.load_plan()
    assert plan.variant == "benchmaxxing-offline-30m-static-inputs-v2"
    assert len(plan.tasks) == 10
    assert sum(len(task.inputs) for task in plan.tasks) == 15
    assert all(
        not {"metadata", "secret.py", "solution"}.intersection(Path(item.source).parts)
        for task in plan.tasks for item in task.inputs
    )


def test_offline_prompt_resolves_upstream_location_placeholders() -> None:
    inputs = [
        cybench_static.StaticInput(
            kind="copy", source="challenge/source.py", target="source.py",
            size=1, sha256="a" * 64,
        ),
        cybench_static.StaticInput(
            kind="copy", source="challenge/output.txt", target="output.txt",
            size=1, sha256="b" * 64,
        ),
    ]
    original = "Inspect $TMP_DIR and /challenge in the parent directory."
    prompt = cybench_static._offline_prompt(original, inputs)
    assert prompt.startswith(original + "\n\n")
    assert "/workspace: source.py, output.txt" in prompt
    assert "the parent directory, or the working directory refer to /workspace" in prompt
    assert "FLAG{" not in prompt
