"""Explicit scientific wheel preparation, with measured disk reservations."""

from __future__ import annotations

import base64
import json
import re
import uuid
import zipfile
from pathlib import Path
from typing import Any

from benchmaxxing.environment import EnvironmentError, _docker, _inspect_image, _require_success
from benchmaxxing.providers import downloads, registry
from benchmaxxing.providers.accounting import capacity_preparation
from benchmaxxing.providers.preparation import check_docker_capacity

_PINS = ["numpy==1.26.4", "scipy==1.13.1", "h5py==3.11.0", "matplotlib==3.9.0", "sympy==1.12.1"]
_LIST = """import pathlib,json
print(json.dumps([{"name":p.name,"bytes":p.stat().st_size}
 for p in pathlib.Path('/tmp/wheels').glob('*.whl')]))
"""
_TRANSFER = """import base64,pathlib,sys
print(base64.b64encode(pathlib.Path('/tmp/wheels',sys.argv[1]).read_bytes()).decode())
"""


def _download(data_dir: Path, wheel_dir: Path) -> dict[str, Any]:
    downloads.check_capacity(data_dir, 1000000000)
    try:
        base = _inspect_image("python:3.12-slim-bookworm")
    except EnvironmentError:
        check_docker_capacity(data_dir, 1000000000)
        _require_success(
            _docker(["pull", "python:3.12-slim-bookworm"], timeout=600),
            "SciCode base image preparation failed",
        )
        base = _inspect_image("python:3.12-slim-bookworm")
    name = "benchmaxxing-scicode-prepare-" + uuid.uuid4().hex
    try:
        _require_success(
            _docker(
                [
                    "run",
                    "-d",
                    "--name",
                    name,
                    "--pull",
                    "never",
                    "--network",
                    "bridge",
                    "--read-only",
                    "--user",
                    "65534:65534",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges:true",
                    "--memory",
                    "1024m",
                    "--pids-limit",
                    "64",
                    "--tmpfs",
                    "/tmp:rw,nosuid,nodev,size=536870912,mode=1777",
                    "--entrypoint",
                    "python",
                    base["Id"],
                    "-c",
                    "import time; time.sleep(1800)",
                ],
                timeout=30,
            ),
            "Could not start SciCode wheel preparation",
        )
        _require_success(
            _docker(
                [
                    "exec",
                    name,
                    "python",
                    "-m",
                    "pip",
                    "download",
                    "--only-binary=:all:",
                    "--no-cache-dir",
                    "--disable-pip-version-check",
                    "--dest",
                    "/tmp/wheels",
                    *_PINS,
                ],
                timeout=600,
                output_limit=1048576,
            ),
            "SciCode wheel download failed",
        )
        names = json.loads(
            _require_success(
                _docker(
                    [
                        "exec",
                        name,
                        "python",
                        "-c",
                        _LIST,
                    ],
                    timeout=30,
                ),
                "Could not inspect prepared wheels",
            )
        )
        wheel_dir.mkdir(parents=True, exist_ok=True)
        for item in names:
            if (
                not re.fullmatch(r"[A-Za-z0-9_.+-]+[.]whl", item["name"])
                or not 0 < item["bytes"] <= 134217728
            ):
                raise ValueError("Prepared wheel has an invalid name or excessive size")
            raw = _require_success(
                _docker(
                    [
                        "exec",
                        name,
                        "python",
                        "-c",
                        _TRANSFER,
                        item["name"],
                    ],
                    timeout=120,
                    output_limit=item["bytes"] * 2 + 4096,
                ),
                "Wheel transfer failed",
            )
            payload = base64.b64decode(raw.strip(), validate=True)
            if len(payload) != item["bytes"]:
                raise ValueError("Prepared wheel transfer size differs")
            downloads.write_prepared_file(data_dir, wheel_dir / item["name"], payload)
            item["sha256"] = registry.file_hash(wheel_dir / item["name"])
        return {"image_id": base["Id"], "base_image_size": base["Size"], "wheels": names}
    finally:
        _docker(["rm", "-f", name], timeout=30)


@capacity_preparation
def prepare_scicode_wheels(data_dir: Path) -> dict[str, Any]:
    """Resolve wheels in Docker, then inspect their bytes without importing them."""
    directory = downloads.cache_root(data_dir) / "build" / "scicode-verified"
    wheel_dir = directory / "wheels"
    receipt_path = directory / "wheel-capacity.json"
    record = (
        json.loads(receipt_path.read_text(encoding="utf-8"))
        if receipt_path.is_file()
        else _download(data_dir, wheel_dir)
    )
    total = 0
    expanded = 0
    for item in record["wheels"]:
        if not re.fullmatch(r"[A-Za-z0-9_.+-]+[.]whl", item["name"]):
            raise ValueError("Prepared wheel name is invalid")
        path = wheel_dir / item["name"]
        if path.is_symlink() or registry.file_hash(path) != item["sha256"]:
            raise ValueError("Prepared wheel hash changed")
        total += path.stat().st_size
        with zipfile.ZipFile(path) as archive:
            item["expanded_bytes"] = sum(entry.file_size for entry in archive.infolist())
        expanded += item["expanded_bytes"]
    if total > 536870912 or expanded > 1500000000:
        raise ValueError("Prepared scientific wheels exceed their explicit size allowance")
    # Covers context, retained wheel layer, installed layer/export copies, plus
    # metadata/tar overhead. --no-compile prevents generated bytecode in build.
    record.update(
        wheel_bytes=total,
        expanded_bytes=expanded,
        reservation_bytes=4 * total + 2 * expanded + 134217728,
    )
    info = _inspect_image(record["image_id"])
    record["base_image_size"] = max(int(record.get("base_image_size", 0)), int(info["Size"]))
    refs = [ref for ref in info.get("RepoDigests", []) if ref.startswith("python@sha256:")]
    if not refs:
        raise ValueError("Scientific base must have a local immutable Python repository digest")
    record["base_reference"] = refs[0]
    _inspect_image(record["base_reference"])
    registry._atomic_json(receipt_path, record)
    return record
