from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest

from benchmaxxing.providers import specialized_sources as sources
from benchmaxxing.providers.specialized import convert_specialized


def test_snapshot_verifies_existing_files_before_reuse(tmp_path: Path) -> None:
    path = tmp_path / "data.txt"
    path.write_bytes(b"changed")
    with pytest.raises(ValueError, match="checksum"):
        sources._verify(path, {"path": "data.txt", "size": 7, "sha256": "0" * 64})


def test_snapshot_rejects_duplicate_and_escaping_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sources.downloads, "cache_root", lambda _: tmp_path)
    for names in [["../escape"], ["same", "same"]]:
        with pytest.raises(ValueError):
            sources._snapshot(
                tmp_path,
                tmp_path / "source",
                [{"path": name, "size": 1, "url": "https://example.invalid"} for name in names],
            )


def test_snapshot_deduplicates_verified_blobs_and_bounds_streams(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sources.downloads, "cache_root", lambda _: tmp_path)
    reservations = []
    monkeypatch.setattr(
        sources.downloads, "check_capacity", lambda _, size: reservations.append(size)
    )
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=b"data")

    real_client = httpx.Client
    monkeypatch.setattr(
        sources.httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs),
    )
    digest = hashlib.sha256(b"data").hexdigest()
    entries = [
        {
            "path": f"file-{index}",
            "size": 4,
            "sha256": digest,
            "url": "https://example.invalid/data",
        }
        for index in range(2)
    ]
    sources._snapshot(tmp_path, tmp_path / "source", entries)
    assert len(calls) == 1
    assert reservations == [8, 8]
    assert (tmp_path / "source/file-1").read_bytes() == b"data"
    with pytest.raises(ValueError, match="exceeds"):
        sources._snapshot(
            tmp_path,
            tmp_path / "oversize",
            [{"path": "data", "size": 3, "url": "https://example.invalid/data"}],
        )
    assert not (tmp_path / "oversize/data").exists()
    assert not list((tmp_path / "oversize").glob("*.benchmaxxing-part"))


def test_cybench_cohort_excludes_auxiliary_metadata(tmp_path: Path) -> None:
    for name in ["published", "auxiliary"]:
        path = tmp_path / name / "metadata/metadata.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"hard_prompt": name, "subtasks": [{"answer": "flag"}]}))
    (tmp_path / "task_list.txt").write_text("published\n")
    tasks = convert_specialized("cybench", tmp_path, revision="pin", source_sha256="b" * 64)
    assert [task.upstream_id for task in tasks] == ["published"]
    assert all(asset.scope == "grader" for asset in tasks[0].assets)
    (tmp_path / "benchmaxxing-source-materialization.json").write_text('{"archive_sha256":"pin"}')
    (tmp_path / "task_list.txt").write_text("published\nmissing-upstream\n")
    available = convert_specialized("cybench", tmp_path, revision="pin", source_sha256="c" * 64)
    assert len(available) == 1
    assert available[0].variant == "benchmaxxing-offline-30m-available-cohort-v1"
    assert available[0].recipe.settings["missing_upstream_tasks"] == ["missing-upstream"]
    assert any("materialization" in asset.source for asset in available[0].assets)


def test_cybench_materialization_keeps_inputs_and_omits_unused_oracle_binaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import io
    import tarfile

    archive = tmp_path / "full-source.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        for name, content in {
            "task/init_script.sh": b"echo fixture",
            "task/metadata/metadata.json": b'{"answer":"hidden flag"}',
            "task/metadata/solution/oracle.exe": b"unused oracle fixture",
        }.items():
            entry = tarfile.TarInfo("cybench-pin/" + name)
            entry.size = len(content)
            stream.addfile(entry, io.BytesIO(content))
    monkeypatch.setattr(sources.downloads, "cache_root", lambda _: tmp_path)
    monkeypatch.setattr(sources.downloads, "check_capacity", lambda *_: None)
    destination = sources._cybench_inputs(tmp_path, archive, tmp_path / "inputs", "pin")
    assert (destination / "task/init_script.sh").read_bytes() == b"echo fixture"
    assert not (destination / "task/metadata/solution").exists()
    manifest = json.loads((destination / "benchmaxxing-source-materialization.json").read_text())
    assert manifest["omitted_oracle_solution_paths"] == ["task/metadata/solution/oracle.exe"]
    assert manifest["archive_sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
    (destination / "task/init_script.sh").write_bytes(b"changed bytes")
    with pytest.raises(ValueError, match="missing or changed"):
        sources._cybench_inputs(tmp_path, archive, destination, "pin")
