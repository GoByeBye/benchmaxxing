"""Explicit small specialized runtime preparation, isolated from execution."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from benchmaxxing.environment import EnvironmentError, _docker, _inspect_image, _require_success
from benchmaxxing.providers import downloads, preparation, registry
from benchmaxxing.providers.accounting import capacity_preparation

_RESPONSE_DOCKERFILE = """FROM python:3.13-slim
RUN mkdir -p /workspace /grader && chmod 1777 /workspace /grader
ENV PYTHONUNBUFFERED=1 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PIP_NO_INDEX=1
USER 65534:65534
WORKDIR /workspace
CMD ["sleep", "infinity"]
"""


@capacity_preparation
def prepare_specialized_runtime(data_dir: Path, suite_id: str) -> dict[str, Any]:
    if suite_id not in {"xstest", "harmbench", "intercode-ctf"}:
        raise ValueError("This suite requires its separate upstream runtime/judge preparation")
    tasks = registry.imported_tasks(data_dir, suite_id)
    if not tasks:
        raise ValueError("Fetch and import the full suite source before preparing its runtime")
    ctf = suite_id == "intercode-ctf"
    image = "benchmaxxing/intercode-ctf-tools:1" if ctf else "benchmaxxing/refusal-response:1"
    dockerfile = (
        Path(__file__).with_name("specialized_intercode.Dockerfile").read_text()
        if ctf
        else _RESPONSE_DOCKERFILE
    )
    try:
        info = _inspect_image(image)
    except EnvironmentError:
        preparation.check_docker_capacity(data_dir, 4_000_000_000 if ctf else 1_000_000_000)
        build = (
            downloads.cache_root(data_dir)
            / "build"
            / ("intercode-ctf" if ctf else "refusal-response")
        )
        build.mkdir(parents=True, exist_ok=True)
        downloads.write_prepared_file(data_dir, build / "Dockerfile", dockerfile.encode("utf-8"))
        _require_success(
            _docker(["build", "-t", image, str(build)], timeout=1200, output_limit=1048576),
            "Specialized runtime build failed",
        )
        info = _inspect_image(image)
    registry._atomic_json(
        registry.root(data_dir)
        / "prepared"
        / ("runtime-intercode.json" if ctf else "runtime-refusal.json"),
        {
            "image_id": info["Id"],
            "image_size_bytes": info.get("Size", 0),
            "dockerfile_sha256": hashlib.sha256(dockerfile.encode()).hexdigest(),
            "scope": "response collection with ungraded assessment"
            if not ctf
            else "offline CTF tools",
        },
    )
    result = preparation.prepare_suite(data_dir, tasks, image=info["Id"])
    if not ctf:
        result["assessment_status"] = "needs_review"
        result["note"] = (
            "Responses can be collected offline; no local judge is configured "
            "and no correctness score is invented."
        )
    return result
