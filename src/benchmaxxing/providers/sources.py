"""Explicit source preparation shared by the CLI and local service."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from benchmaxxing.providers.catalog import require_active_suite
from benchmaxxing.providers.locking import serialized_preparation
from benchmaxxing.providers.registry import convert, install


@serialized_preparation
def fetch_suite(data_dir: Path, suite_id: str) -> dict[str, Any]:
    """Fetch a pinned complete source, validate it and install immutable task records."""
    suite = require_active_suite(suite_id)
    if suite.offline == "unavailable":
        raise ValueError("; ".join(suite.requirements))
    if suite_id == "frontend-design":
        from benchmaxxing.providers.frontend import fetch_frontend_source

        fetched = fetch_frontend_source(data_dir)
    elif suite_id == "frontend-design-extended":
        from benchmaxxing.providers.frontend import fetch_frontend_extension_source

        fetched = fetch_frontend_extension_source(data_dir)
    elif suite_id in {
        "evalplus-humaneval",
        "evalplus-mbpp",
        "bigcodebench",
        "swebench-lite",
        "swebench-verified",
        "scicode-verified",
    }:
        from benchmaxxing.providers.coding_sources import fetch_coding_sources

        fetched = fetch_coding_sources(data_dir, suite_id)
    elif suite_id in {
        "terminal-bench-2-1",
        "terminal-bench-4",
        "miniwob",
        "webarena-verified",
        "visualwebarena",
    }:
        from benchmaxxing.providers.interactive_sources import fetch_interactive_sources

        fetched = fetch_interactive_sources(data_dir, suite_id)
    else:
        from benchmaxxing.providers.specialized_sources import fetch_specialized_sources

        fetched = fetch_specialized_sources(data_dir, suite_id)
    source = Path(fetched["source_path"])
    tasks = convert(suite_id, source, revision=fetched["revision"])
    result = install(data_dir, source, tasks)
    result.update(
        {
            "source_path": str(source),
            "revision": fetched["revision"],
            "source_sha256": fetched["source_sha256"],
        }
    )
    return result
