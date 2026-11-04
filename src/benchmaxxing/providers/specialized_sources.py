"""Explicit, bounded official sources for security and refusal providers."""

from __future__ import annotations

import hashlib
import json
import shutil
import tarfile
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from filelock import FileLock

from benchmaxxing.providers import downloads, registry
from benchmaxxing.providers.contracts import relative_path
from benchmaxxing.providers.specialized import convert_specialized
from benchmaxxing.runtimes.specialized_prepare import prepare_sources

PINS = {
    "xstest": "d7bb5bd738c1fcbc36edd83d5e7d1b71a3e2d84d",
    "harmbench": "8e1604d1171fe8a48d8febecd22f600e462bdcdd",
    "intercode-ctf": "c3e46d827cfc9d4c704ec078f7abf9f41e3191d8",
    "cybench": "ca483c6443f17a20ba7d6bac3d274859a1721e51",
    "cybergym": "bde190ded494e52bc684b66073b436c9d992c7c6",
}
_HF = {
    "cybergym": "sunblaze-ucb/cybergym",
}


def _verify(path: Path, row: dict[str, Any]) -> str:
    if not path.is_file() or path.is_symlink() or path.stat().st_size != row["size"]:
        raise ValueError(f"Source file is missing or changed: {row['path']}")
    digest = registry.file_hash(path)
    if row.get("sha256") and digest != row["sha256"]:
        raise ValueError(f"Source checksum differs: {row['path']}")
    if row.get("git_blob"):
        state = hashlib.sha1(usedforsecurity=False)
        state.update(f"blob {row['size']}\0".encode())
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                state.update(chunk)
        if state.hexdigest() != row["git_blob"]:
            raise ValueError(f"Pinned Git blob differs: {row['path']}")
    return digest


def _snapshot(
    data_dir: Path,
    source: Path,
    entries: list[dict[str, Any]],
    *,
    headers: dict[str, str] | None = None,
) -> None:
    """Fetch exact-sized immutable files, deduplicating identical upstream blobs."""
    with downloads.capacity_batch(data_dir):
        _snapshot_files(data_dir, source, entries, headers=headers)


def _snapshot_files(
    data_dir: Path,
    source: Path,
    entries: list[dict[str, Any]],
    *,
    headers: dict[str, str] | None = None,
) -> None:
    base = downloads.cache_root(data_dir).resolve()
    if not source.resolve().is_relative_to(base):
        raise ValueError("Source snapshot must remain inside the configured cache")
    names = set()
    for row in entries:
        relative_path(row["path"])
        if row["path"] in names or row["size"] < 0:
            raise ValueError("Duplicate or negative-sized source file")
        names.add(row["path"])
    downloads.check_capacity(
        data_dir, sum(row["size"] for row in entries if not (source / row["path"]).exists())
    )
    source.mkdir(parents=True, exist_ok=True)
    cached: dict[str, Path] = {}
    with httpx.Client(
        trust_env=False, follow_redirects=True, timeout=120, headers=headers
    ) as client:
        # Each bounded batch reserves its entire write size while holding the
        # same global preparation lock. Other providers can proceed between
        # batches; no repeatedly scanned cache is needed for every tiny file.
        for offset in range(0, len(entries), 64):
            batch = entries[offset : offset + 64]
            if all((source / row["path"]).exists() for row in batch):
                for row in batch:
                    path = source / row["path"]
                    _verify(path, row)
                    cached[str(row.get("sha256") or row.get("git_blob") or row["url"])] = path
                continue
            with FileLock(str(base / "preparation.lock"), timeout=120):
                downloads.check_capacity(
                    data_dir,
                    sum(row["size"] for row in batch if not (source / row["path"]).exists()),
                )
                for row in batch:
                    path = source / row["path"]
                    key = str(row.get("sha256") or row.get("git_blob") or row["url"])
                    if path.exists():
                        _verify(path, row)
                        cached[key] = path
                        continue
                    path.parent.mkdir(parents=True, exist_ok=True)
                    temporary = path.with_name(path.name + ".benchmaxxing-part")
                    try:
                        if key in cached:
                            shutil.copyfile(cached[key], temporary)
                        else:
                            size = 0
                            with client.stream("GET", row["url"]) as response:
                                response.raise_for_status()
                                with temporary.open("wb") as stream:
                                    for chunk in response.iter_bytes(1024 * 1024):
                                        size += len(chunk)
                                        if size > row["size"]:
                                            raise ValueError("Source exceeds its pinned file size")
                                        stream.write(chunk)
                        digest = _verify(temporary, row)
                        temporary.replace(path)
                        downloads.note_file(path)
                        cached[key] = path
                        row["sha256"] = digest
                    finally:
                        temporary.unlink(missing_ok=True)
                        downloads.note_file(temporary)


def _hf_source(data_dir: Path, suite_id: str) -> Path:
    repo, revision = _HF[suite_id], PINS[suite_id]
    source = downloads.cache_root(data_dir) / "sources" / suite_id / revision
    manifest = source.parent / f"{revision}.source-manifest.json"
    if manifest.is_file():
        entries = json.loads(manifest.read_text(encoding="utf-8"))["files"]
    else:
        entries = []
        url: str | None = (
            f"https://huggingface.co/api/datasets/{repo}/tree/{revision}?recursive=true&limit=1000"
        )
        with httpx.Client(trust_env=False, timeout=60) as client:
            while url:
                response = client.get(url)
                response.raise_for_status()
                if len(response.content) > 16_000_000:
                    raise ValueError("Dataset metadata page is too large")
                for row in response.json():
                    if row["type"] != "file":
                        continue
                    name = row["path"]
                    if suite_id == "cybergym" and name not in {
                        "tasks.json",
                        "README.md",
                        ".gitattributes",
                    }:
                        continue
                    entries.append(
                        {
                            "path": name,
                            "size": row["size"],
                            "url": (
                                f"https://huggingface.co/datasets/{repo}/resolve/"
                                f"{revision}/{quote(name, safe='/')}"
                            ),
                            **(
                                {"sha256": row["lfs"]["oid"]}
                                if row.get("lfs")
                                else {"git_blob": row["oid"]}
                            ),
                        }
                    )
                url = response.links.get("next", {}).get("url")
                if url and not url.startswith(
                    f"https://huggingface.co/api/datasets/{repo}/tree/{revision}"
                ):
                    raise ValueError("Unexpected dataset pagination URL")
                if len(entries) > 20000:
                    raise ValueError("Dataset inventory exceeds its file limit")
        registry._atomic_json(manifest, {"revision": revision, "files": entries})
    _snapshot(data_dir, source, entries)
    return source


def _small_source(data_dir: Path, suite_id: str) -> Path:
    source = downloads.cache_root(data_dir) / "sources" / suite_id
    manifest = source / "benchmaxxing-source.json"
    if not manifest.is_file():
        cap = 600_000_000 if suite_id == "harmbench" else 150_000_000
        with FileLock(str(downloads.cache_root(data_dir) / "preparation.lock"), timeout=120):
            downloads.check_capacity(data_dir, cap)
            prepare_sources(
                suite_id,
                source,
                cap,
                PINS[suite_id],
                resume=source.is_dir() and any(source.iterdir()),
            )
    value = json.loads(manifest.read_text(encoding="utf-8"))
    if value["revision"] != PINS[suite_id]:
        raise ValueError("Existing source snapshot uses a different revision")
    for row in value["files"]:
        relative_path(row["path"])
        # Git LFS media uses the recorded SHA256 rather than the pointer's Git hash.
        _verify(source / row["path"], {key: item for key, item in row.items() if key != "git_blob"})
    downloads.check_capacity(data_dir, 0)
    return source


def _cybench_source(data_dir: Path) -> Path:
    revision = PINS["cybench"]
    directory = downloads.cache_root(data_dir) / "sources" / "cybench"
    archive = directory / f"{revision}.tar.gz"
    downloads.download(
        data_dir,
        f"https://codeload.github.com/andyzorigin/cybench/tar.gz/{revision}",
        archive,
        max_bytes=2_200_000_000,
    )
    return _cybench_inputs(data_dir, archive, directory / (revision + "-task-inputs-v1"), revision)


def _cybench_inputs(data_dir: Path, archive: Path, destination: Path, revision: str) -> Path:
    """Materialize task inputs without unused oracle malware executables on the host.

    The complete original archive remains checksummed. Native answer metadata is
    retained privately; oracle solution binaries are not needed by flag grading.
    """
    with downloads.capacity_batch(data_dir):
        base = downloads.cache_root(data_dir).resolve()
        if not destination.resolve().is_relative_to(base):
            raise ValueError("CyBench input materialization must remain inside the cache")
        if not destination.exists():
            downloads.check_capacity(data_dir, 2_500_000_000)
            files, omitted = [], []
            total = 0
            with downloads._extraction_directory(destination, base) as stage:
                with tarfile.open(archive, "r:gz") as stream:
                    for count, member in enumerate(stream, 1):
                        relative_path(member.name.rstrip("/"))
                        if count > 20000 or not (member.isfile() or member.isdir()):
                            raise ValueError("CyBench archive exceeds its file/type limits")
                        parts = Path(member.name).parts
                        if not parts or parts[0] != "cybench-" + revision:
                            raise ValueError(
                                "CyBench archive root differs from its pinned revision"
                            )
                        if len(parts) == 1:
                            continue
                        relative = Path(*parts[1:])
                        if any(
                            parts[index : index + 2] == ("metadata", "solution")
                            for index in range(len(parts) - 1)
                        ):
                            omitted.append(relative.as_posix())
                            continue
                        path = stage / relative
                        if member.isdir():
                            path.mkdir(parents=True, exist_ok=True)
                            continue
                        total += member.size
                        if member.size < 0 or total > 2_500_000_000:
                            raise ValueError("CyBench archive exceeds its expanded byte limit")
                        path.parent.mkdir(parents=True, exist_ok=True)
                        content = stream.extractfile(member)
                        assert content is not None
                        digest = hashlib.sha256()
                        with content, path.open("xb") as output:
                            while chunk := content.read(1024 * 1024):
                                digest.update(chunk)
                                output.write(chunk)
                        row = {
                            "path": relative.as_posix(),
                            "size": member.size,
                            "sha256": digest.hexdigest(),
                        }
                        _verify(path, row)
                        files.append(row)
                (stage / "benchmaxxing-source-materialization.json").write_text(
                    json.dumps(
                        {
                            "revision": revision,
                            "archive_sha256": registry.file_hash(archive),
                            "files": files,
                            "omitted_oracle_solution_paths": omitted,
                            "reason": "Oracle solution binaries are unused by native flag grading",
                        },
                        sort_keys=True,
                    ),
                    encoding="utf-8",
                )
            downloads.note_tree(destination)
        manifest = json.loads(
            (destination / "benchmaxxing-source-materialization.json").read_text(encoding="utf-8")
        )
        if manifest["revision"] != revision or manifest["archive_sha256"] != registry.file_hash(
            archive
        ):
            raise ValueError("CyBench source materialization provenance differs")
        for row in manifest["files"]:
            _verify(destination / row["path"], row)
        return destination


def fetch_specialized_sources(data_dir: Path, suite_id: str) -> dict[str, Any]:
    """Prepare full public source identities; runtime readiness is verified separately."""
    if suite_id not in PINS:
        raise ValueError(
            "No public full source preparation is available for this specialized suite"
        )
    if suite_id in {"xstest", "harmbench", "intercode-ctf"}:
        source = _small_source(data_dir, suite_id)
    elif suite_id == "cybench":
        source = _cybench_source(data_dir)
    else:
        source = _hf_source(data_dir, suite_id)
    digest = registry.source_hash(source)
    tasks = convert_specialized(suite_id, source, revision=PINS[suite_id], source_sha256=digest)
    expected = {
        "xstest": 450,
        "harmbench": 510,
        "intercode-ctf": 100,
    }
    if suite_id in expected and len(tasks) != expected[suite_id]:
        raise ValueError(f"Expected {expected[suite_id]} tasks; converted {len(tasks)}")
    missing = (
        tasks[0].recipe.settings.get("missing_upstream_tasks", [])
        if suite_id == "cybench" and tasks
        else []
    )
    if suite_id == "cybench" and len(tasks) + len(missing) != 40:
        raise ValueError(
            "CyBench available and missing task identities must cover its 40-task list"
        )
    result = {
        "suite_id": suite_id,
        "source_path": str(source.resolve()),
        "revision": PINS[suite_id],
        "source_sha256": digest,
        "tasks": len(tasks),
        "runtime_prepared": False,
    }
    if missing:
        result["unavailable_upstream_tasks"] = missing
        result["declared_cohort_tasks"] = 40
    registry._atomic_json(
        downloads.cache_root(data_dir) / "manifests" / f"{suite_id}.prepared-source.json", result
    )
    return result


def prepare_specialized_runtime(data_dir: Path, suite_id: str) -> dict[str, Any]:
    """Explicitly prepare the reviewed runtime, never during benchmark execution."""
    from benchmaxxing.providers.operator_scope import check_preparation_scope

    check_preparation_scope(data_dir, suite_id)
    if suite_id in {"xstest", "harmbench", "intercode-ctf"}:
        from benchmaxxing.runtimes.specialized_runtime_prepare import (
            prepare_specialized_runtime as prepare,
        )

        return prepare(data_dir, suite_id)
    blockers = {
        "cybench": "Prepare each task-local target topology and materialize init-script "
        "candidate inputs inside isolated preparation containers",
        "cybergym": "Full CyberGym server data (~10 TB) exceeds the total 250 GB cap",
    }
    raise ValueError(blockers.get(suite_id, "No reviewed specialized runtime for this suite"))
