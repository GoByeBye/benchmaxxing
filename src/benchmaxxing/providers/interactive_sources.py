"""Explicit, bounded preparation of immutable interactive benchmark source data.

This downloads archives only when called by the preparation flow. Extraction
never imports Python, runs shell scripts, installs packages, or follows links.
Website images and model weights are separate preparation requirements.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from benchmaxxing.providers.registry import _atomic_json, source_hash

MIB = 1024 * 1024
SOURCES: dict[str, dict[str, Any]] = {
    "terminal-bench-2-1": {
        "revision": "7131e4375048a0e408a8fb404b5f499d726b695b",
        "url": "https://codeload.github.com/harbor-framework/terminal-bench-2-1/tar.gz/"
        "7131e4375048a0e408a8fb404b5f499d726b695b",
        "format": "tar.gz",
        "max_download_bytes": 128 * MIB,
        "max_extracted_bytes": 512 * MIB,
    },
    "terminal-bench-4": {
        "revision": "452bf305c6daa62fc59061d22133a7cbc7c1572e",
        "release": "v4.0.0",
        "url": "https://codeload.github.com/harbor-framework/terminal-bench/tar.gz/"
        "452bf305c6daa62fc59061d22133a7cbc7c1572e",
        "format": "tar.gz",
        "max_download_bytes": 512 * MIB,
        "max_extracted_bytes": 2048 * MIB,
    },
    "miniwob": {
        "revision": "7fd85d71a4b60325c6585396ec4f48377d049838",
        "url": "https://codeload.github.com/Farama-Foundation/miniwob-plusplus/tar.gz/"
        "7fd85d71a4b60325c6585396ec4f48377d049838",
        "sha256": "89cc878fc4769d8815630f5367c5c53b0ad4ae0a98b6fa9663eba83cf99a3807",
        "format": "tar.gz",
        "max_download_bytes": 16 * MIB,
        "max_extracted_bytes": 64 * MIB,
    },
    "webarena-verified": {
        "revision": "6473f72db5dcefc97b5725b59e734504edc28a21",
        "url": "https://codeload.github.com/ServiceNow/webarena-verified/tar.gz/"
        "6473f72db5dcefc97b5725b59e734504edc28a21",
        "format": "tar.gz",
        "max_download_bytes": 16 * MIB,
        "max_extracted_bytes": 64 * MIB,
    },
    "visualwebarena": {
        "revision": "libvisualwebarena-0.0.15",
        "url": "https://files.pythonhosted.org/packages/5c/de/"
        "321b533a7ec8538396addbc3206eef0ada4153208f783679e6091d94b2f7/"
        "libvisualwebarena-0.0.15-py3-none-any.whl",
        "sha256": "02c2f66b6a7ccb98e11c6decd8b00e8fc262223b002b80a13464e20bcdfe2999",
        "format": "zip",
        "max_download_bytes": 16 * MIB,
        "max_extracted_bytes": 64 * MIB,
    },
}


def fetch_interactive_sources(data_dir: Path, suite_id: str) -> dict[str, Any]:
    """Shared CLI/API entry point, using the aggregate preparation cap and lock."""
    from benchmaxxing.providers.downloads import cache_root, download, extract_archive
    from benchmaxxing.providers.interactive import convert_interactive

    if suite_id not in SOURCES:
        raise ValueError("No reviewed offline source download recipe for this suite")
    spec = SOURCES[suite_id]
    directory = cache_root(data_dir) / "sources" / suite_id / spec["revision"]
    archive = directory / ("source.zip" if spec["format"] == "zip" else "source.tar.gz")
    receipt = download(
        data_dir,
        spec["url"],
        archive,
        max_bytes=spec["max_download_bytes"],
        sha256=spec.get("sha256"),
    )
    extracted = directory / "source"
    if not extracted.exists():
        extract_archive(data_dir, archive, extracted, max_bytes=spec["max_extracted_bytes"])
    children = list(extracted.iterdir())
    source = children[0] if len(children) == 1 and children[0].is_dir() else extracted
    digest = source_hash(source)
    snapshot_path = directory / "snapshot.json"
    if snapshot_path.exists():
        old = json.loads(snapshot_path.read_text(encoding="utf-8"))
        if old.get("source_sha256") != digest or old.get("archive_sha256") != receipt["sha256"]:
            raise ValueError("Prepared interactive source changed; inspect the cached snapshot")
    tasks = convert_interactive(suite_id, source, revision=spec["revision"], source_sha256=digest)
    result = {
        "schema_version": 1,
        "suite_id": suite_id,
        "source_path": str(source),
        "revision": spec["revision"],
        "source_sha256": digest,
        "tasks": len(tasks),
        "archive_sha256": receipt["sha256"],
        "downloaded_bytes": receipt["bytes"],
    }
    _atomic_json(snapshot_path, result)
    return result
