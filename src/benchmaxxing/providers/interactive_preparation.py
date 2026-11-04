"""Explicit builds for the reviewed offline interactive runtime profiles."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from benchmaxxing.environment import (
    EnvironmentError,
    PreparationError,
    _docker,
    _inspect_image,
    _require_success,
)
from benchmaxxing.providers import registry
from benchmaxxing.providers.contracts import ImportedTask
from benchmaxxing.providers.downloads import cache_root, download
from benchmaxxing.providers.preparation import check_docker_capacity, prepare_imported

_HARBOR_PROFILES = {
    "cancel-async-tasks-offline-v1": {
        "suite_id": "terminal-bench-2-1",
        "upstream_id": "terminal-bench/cancel-async-tasks",
        "base": "python@sha256:ae14d4ce8771606477a81446e7ef429edd6762305fc50c84507c1041fa8285d1",
        "pytest": "8.4.1",
        "ctrf": "0.3.5",
        "lock": "interactive_cancel_async.lock",
        "fingerprint": "98e3155bda13ca6665508fa1427a13489e62a6ef6d48c6749ad089fd3c315f87",
    },
    "sound-change-cascade-offline-v1": {
        "suite_id": "terminal-bench-4",
        "upstream_id": "terminal-bench/sound-change-cascade",
        "base": "python@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337",
        "pytest": "9.1.1",
        "ctrf": "0.5.2",
        "lock": "interactive_sound_change.lock",
        "fingerprint": "c8245d420256523310f98df434912880d8989fbc69ffb1f0c1566240c4867a12",
    },
    "regex-log-offline-v1": {
        "suite_id": "terminal-bench-2-1",
        "upstream_id": "terminal-bench/regex-log",
        "base": "benchmaxxing/cancel-async-tasks-offline-v1:latest",
        "base_runtime_label": "cancel-async-tasks-offline-v1",
        "fingerprint": "181ee872aa005fa1082d9de4b2ffc01ef3aa322eb08cc0061db35153d9169701",
    },
    "sqlite-db-truncate-offline-v1": {
        "suite_id": "terminal-bench-2-1",
        "upstream_id": "terminal-bench/sqlite-db-truncate",
        "base": "benchmaxxing/cancel-async-tasks-offline-v1:latest",
        "base_runtime_label": "cancel-async-tasks-offline-v1",
        "fingerprint": "07c19eaf9e7cb52889b73d2ef279d704971dae5fa40992d0646c1005a09fa47c",
    },
}


def _sound_wheels(data_dir: Path, locked: Path, context: Path) -> dict[str, int]:
    """Fetch only hash-locked pure Python wheels as inert bytes, with size caps."""
    cache = cache_root(data_dir) / "wheels" / "sound-change-cascade-offline-v1"
    wheel_dir = context / "wheels"
    wheel_dir.mkdir()
    compressed = expanded = count = 0
    for line in locked.read_text(encoding="utf-8-sig").splitlines():
        requirement, expected = line.split(" --hash=sha256:")
        package, version = requirement.split("==")
        metadata = cache / f"{package}-{version}.json"
        download(
            data_dir,
            f"https://pypi.org/pypi/{package}/{version}/json",
            metadata,
            max_bytes=1024 * 1024,
        )
        matches = [
            entry
            for entry in json.loads(metadata.read_bytes())["urls"]
            if entry["digests"]["sha256"] == expected
            and entry["filename"].endswith("-none-any.whl")
        ]
        if len(matches) != 1:
            raise PreparationError("The locked pure-Python wheel is unavailable")
        entry = matches[0]
        filename = entry["filename"]
        if Path(filename).name != filename or "/" in filename or "\\" in filename:
            raise PreparationError("Invalid prepared wheel name")
        target = cache / filename
        download(data_dir, entry["url"], target, max_bytes=4 * 1024**2, sha256=expected)
        compressed += target.stat().st_size
        with zipfile.ZipFile(target) as archive:
            expanded += sum(member.file_size for member in archive.infolist())
        if compressed > 16 * 1024**2 or expanded > 64 * 1024**2:
            raise PreparationError("Reviewed wheel set exceeds its incremental build reservation")
        shutil.copyfile(target, wheel_dir / filename)
        count += 1
    if count != 6:
        raise PreparationError("Reviewed wheel set must contain exactly six locked distributions")
    return {"wheel_count": count, "compressed_bytes": compressed, "expanded_bytes": expanded}


def _prepare_sound_profile(data_dir: Path, task: ImportedTask) -> dict[str, Any]:
    """Reuse a present base; no apt, base pull, or network during the image build."""
    profile = _HARBOR_PROFILES["sound-change-cascade-offline-v1"]
    try:
        base = _inspect_image(profile["base"])
    except EnvironmentError as exc:
        raise PreparationError(
            "Prepare the pinned Python 3.12 base before this incremental build"
        ) from exc
    reservation = check_docker_capacity(data_dir, 256 * 1024**2)
    cache = cache_root(data_dir) / "builds"
    cache.mkdir(parents=True, exist_ok=True)
    locked = Path(__file__).parent / "runtimes" / profile["lock"]
    with tempfile.TemporaryDirectory(prefix="sound-change-", dir=cache) as name:
        context = Path(name)
        source_bytes = 0
        for asset in task.assets:
            if asset.scope != "runtime" or not asset.target.startswith("environment/"):
                continue
            source = registry.asset_root(data_dir) / str(asset.sha256)
            if not source.is_file() or registry.file_hash(source) != asset.sha256:
                raise PreparationError("Runtime source asset changed")
            source_bytes += source.stat().st_size
            if source_bytes > 128 * 1024:
                raise PreparationError("Reviewed source exceeds the bounded build context")
            target = context / asset.target.removeprefix("environment/")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        wheel_sizes = _sound_wheels(data_dir, locked, context)
        shutil.copyfile(locked, context / "requirements.lock")
        dockerfile = f"""FROM {profile["base"]}
WORKDIR /app
COPY engine/ /app/engine/
COPY data/train.tsv /app/data/train.tsv
COPY requirements.lock /opt/benchmaxxing-requirements.lock
COPY wheels/ /tmp/wheels/
RUN python -m pip install --no-cache-dir --no-index --require-hashes \\
    --find-links=/tmp/wheels -r /opt/benchmaxxing-requirements.lock && rm -rf /tmp/wheels
RUN mkdir -p /workspace /grader /app /opt/benchmaxxing \\
    && chmod 1777 /workspace /grader && chown -R 65534:65534 /app \\
    && python -m pip freeze > /opt/benchmaxxing/python-packages.lock
LABEL org.benchmaxxing.runtime="sound-change-cascade-prepacked-nogit-v1"
"""
        (context / "Dockerfile").write_text(dockerfile, encoding="utf-8")
        # Recheck after preparing the bounded wheels, just before consuming Docker space.
        check_docker_capacity(data_dir, 256 * 1024**2)
        image = "benchmaxxing/sound-change-cascade-offline-v1"
        _require_success(
            _docker(
                ["build", "--pull=false", "--network", "none", "--tag", image, str(context)],
                timeout=300,
                output_limit=1024**2,
            ),
            "Reviewed bounded Harbor runtime build failed",
        )
    result = prepare_imported(data_dir, task, image=image)
    result["build_provenance"] = {
        "variant": "sound-change-cascade-prepacked-nogit-v1",
        "base_image_id": base["Id"],
        "source_bytes": source_bytes,
        "wheels": wheel_sizes,
        "dependency_lock_sha256": registry.file_hash(locked),
        "dockerfile_sha256": hashlib.sha256(dockerfile.encode()).hexdigest(),
        "network": "none",
        "capacity": reservation,
        "notes": "The optional upstream git utility is omitted; "
        "no apt step or base pull is performed.",
    }
    registry._atomic_json(registry.root(data_dir) / "prepared" / f"{task.id}.json", result)
    return result


def _prepare_prepacked_python_profile(data_dir: Path, task: ImportedTask) -> dict[str, Any]:
    """Reuse a reviewed local pytest image; copy only a frozen task input if needed."""
    profile_name = task.recipe.settings["offline_profile"]
    profile = _HARBOR_PROFILES[profile_name]
    try:
        base = _inspect_image(profile["base"])
    except EnvironmentError as exc:
        raise PreparationError(
            "Prepare the reviewed cancel-async-tasks Python/pytest runtime first"
        ) from exc
    config = base.get("Config") or {}
    labels = config.get("Labels") or {}
    if (
        base.get("Os") != "linux"
        or labels.get("org.benchmaxxing.runtime") != profile["base_runtime_label"]
    ):
        raise PreparationError("The cached Python/pytest runtime has the wrong identity")

    runtime = {asset.target: asset for asset in task.assets if asset.scope == "runtime"}
    expected = {"environment/Dockerfile"}
    if profile_name == "sqlite-db-truncate-offline-v1":
        expected.add("environment/trunc.db")
    if set(runtime) != expected:
        raise PreparationError("Reviewed task runtime assets changed")

    image = base["Id"]
    provenance: dict[str, Any] = {
        "variant": profile_name,
        "prepacked_base_image_id": image,
        "base_runtime_label": profile["base_runtime_label"],
        "network": "none",
        "dependency_install": "none; exact pytest and pytest-json-ctrf versions probed",
    }
    if profile_name == "sqlite-db-truncate-offline-v1":
        check_docker_capacity(data_dir, 256 * 1024**2)
        source = runtime["environment/trunc.db"]
        asset_path = registry.asset_root(data_dir) / str(source.sha256)
        if source.sha256 is None or not asset_path.is_file():
            raise PreparationError("The reviewed SQLite input asset is missing")
        if registry.file_hash(asset_path) != source.sha256:
            raise PreparationError("The reviewed SQLite input asset changed")
        cache = cache_root(data_dir) / "builds"
        cache.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="sqlite-recovery-", dir=cache) as name:
            context = Path(name)
            shutil.copyfile(asset_path, context / "trunc.db")
            dockerfile = (
                f"FROM {profile['base']}\n"
                "WORKDIR /app\n"
                "COPY trunc.db /app/trunc.db\n"
                "RUN chmod 0444 /app/trunc.db\n"
                'LABEL org.benchmaxxing.runtime="sqlite-db-truncate-offline-v1"\n'
            )
            (context / "Dockerfile").write_text(dockerfile, encoding="utf-8")
            image = "benchmaxxing/sqlite-db-truncate-offline-v1:latest"
            _require_success(
                _docker(
                    [
                        "build",
                        "--pull=false",
                        "--network",
                        "none",
                        "--tag",
                        image,
                        str(context),
                    ],
                    timeout=120,
                    output_limit=1024**2,
                ),
                "Reviewed SQLite runtime build failed",
            )
            provenance["input_sha256"] = source.sha256
            provenance["dockerfile_sha256"] = hashlib.sha256(dockerfile.encode()).hexdigest()
    result = prepare_imported(data_dir, task, image=image, pull=False)
    result["build_provenance"] = provenance
    registry._atomic_json(registry.root(data_dir) / "prepared" / f"{task.id}.json", result)
    return result


def prepare_harbor_profile(data_dir: Path, task: ImportedTask) -> dict[str, Any]:
    """Build only a reviewed bounded-output task; never silently generalize it."""
    profile_name = task.recipe.settings.get("offline_profile")
    if not isinstance(profile_name, str):
        raise PreparationError("No reviewed offline build recipe exists for this native task")
    profile = _HARBOR_PROFILES.get(profile_name)
    if (
        profile is None
        or task.suite_id != profile["suite_id"]
        or task.upstream_id != profile["upstream_id"]
        or task.recipe.settings.get("reviewed_source_sha256") != profile["fingerprint"]
    ):
        raise PreparationError("No reviewed offline build recipe exists for this native task")
    if profile_name == "sound-change-cascade-offline-v1":
        return _prepare_sound_profile(data_dir, task)
    if profile_name in {"regex-log-offline-v1", "sqlite-db-truncate-offline-v1"}:
        return _prepare_prepacked_python_profile(data_dir, task)
    check_docker_capacity(data_dir, 2 * 1024**3)
    cache = cache_root(data_dir) / "builds"
    cache.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="interactive-", dir=cache) as name:
        context = Path(name)
        for asset in task.assets:
            if asset.scope != "runtime" or not asset.target.startswith("environment/"):
                continue
            if asset.sha256 is None:
                raise PreparationError("Import task assets before building the environment")
            source = registry.asset_root(data_dir) / asset.sha256
            if not source.is_file() or registry.file_hash(source) != asset.sha256:
                raise PreparationError("Runtime source asset changed")
            target = context / asset.target.removeprefix("environment/")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        dockerfile = context / "Dockerfile"
        original = dockerfile.read_text(encoding="utf-8")
        lines = original.splitlines()
        if sum(line.lstrip().upper().startswith("FROM ") for line in lines) != 1:
            raise PreparationError("Reviewed native profile requires exactly one image stage")
        lines = [
            f"FROM {profile['base']}" if line.lstrip().upper().startswith("FROM ") else line
            for line in lines
        ]
        locked = Path(__file__).parent / "runtimes" / profile["lock"]
        if not locked.is_file():
            raise PreparationError("The reviewed native dependency lock is missing")
        shutil.copyfile(locked, context / "benchmaxxing-requirements.txt")
        lines += [
            "COPY benchmaxxing-requirements.txt /opt/benchmaxxing-requirements.lock",
            "RUN python -m pip download --only-binary=:all: --require-hashes "
            "--dest /tmp/wheels -r /opt/benchmaxxing-requirements.lock "
            "&& python -m pip install --no-cache-dir --no-index --require-hashes "
            "--find-links=/tmp/wheels -r /opt/benchmaxxing-requirements.lock "
            "&& rm -rf /tmp/wheels",
            "RUN mkdir -p /workspace /grader /app /opt/benchmaxxing "
            "&& chmod 1777 /workspace /grader && chown -R 65534:65534 /app "
            "&& python -m pip freeze > /opt/benchmaxxing/python-packages.lock",
            f'LABEL org.benchmaxxing.runtime="{profile_name}"',
        ]
        dockerfile.write_text("\n".join(lines) + "\n", encoding="utf-8")
        image = "benchmaxxing/" + str(profile_name)
        _require_success(
            _docker(
                ["build", "--pull=false", "--tag", image, str(context)],
                timeout=1200,
                output_limit=4 * 1024 * 1024,
            ),
            "Reviewed Harbor runtime build failed",
        )
    return prepare_imported(data_dir, task, image=image)


def prepare_miniwob_runtime(data_dir: Path) -> dict[str, Any]:
    """Explicit download/build followed by verification of all imported templates."""
    from benchmaxxing.providers.preparation import prepare_suite

    check_docker_capacity(data_dir, 3 * 1024**3)
    runtime = Path(__file__).parent / "runtimes"
    image = "benchmaxxing/miniwob-frozen:0.14.3"
    _require_success(
        _docker(
            [
                "build",
                "--pull=false",
                "--file",
                str(runtime / "interactive_miniwob.Dockerfile"),
                "--tag",
                image,
                str(runtime),
            ],
            timeout=1200,
            output_limit=4 * 1024 * 1024,
        ),
        "MiniWoB runtime preparation failed",
    )
    return prepare_suite(
        data_dir, registry.imported_tasks(data_dir, "miniwob"), image=image, pull=False
    )


def webarena_preparation_plan(data_dir: Path) -> dict[str, Any]:
    """Record measured full-suite prerequisites without starting partial websites.

    Sizes are HEAD responses from the exact URLs in the frozen Verified source.
    Wikipedia is an upstream estimate; its source was unavailable during the
    inventory. Image sizes remain unknown until immutable manifests are obtained.
    """
    assets = [
        {
            "url": "https://webarena-map-server-data.s3.amazonaws.com/osm_tile_server.tar",
            "bytes": 41280327680,
            "size_kind": "http-content-length",
        },
        {
            "url": "https://webarena-map-server-data.s3.amazonaws.com/nominatim_volumes.tar",
            "bytes": 124774901760,
            "size_kind": "http-content-length",
        },
        {
            "url": "https://webarena-map-server-data.s3.amazonaws.com/osrm_routing.tar",
            "bytes": 21278935040,
            "size_kind": "http-content-length",
        },
        {
            "url": "http://metis.lti.cs.cmu.edu/webarena-images/wikipedia_en_all_maxi_2022-05.zim",
            "bytes": None,
            "estimated_bytes": 100000000000,
            "size_kind": "upstream-estimate",
            "availability": "source-refused-connection-at-inventory",
        },
    ]
    map_bytes = 187334164480
    estimated_data = map_bytes + 100000000000
    limit = registry.settings(data_dir).storage_limit_bytes
    reasons = [
        "Full offline WebArena requires map archives totaling 187334164480 bytes plus "
        "approximately 100 GB of Wikipedia data, before images or extraction copies.",
        "The original Wikipedia source refused connection; its exact size and hash are unknown.",
        "Website image manifest inventory was rate-limited; image identities/sizes remain unknown.",
    ]
    if estimated_data > limit:
        reasons.insert(
            0, f"Estimated full data {estimated_data} bytes exceeds the {limit}-byte cap"
        )
    result: dict[str, Any] = {
        "schema_version": 1,
        "suite_id": "webarena-verified",
        "state": "blocked",
        "inventory_date": "2026-11-04",
        "assets": assets,
        "measured_map_archive_bytes": map_bytes,
        "estimated_full_data_bytes": estimated_data,
        "storage_limit_bytes": limit,
        "images": [
            {
                "reference": f"am1n3e/webarena-verified-{name}",
                "image_id": None,
                "image_size_bytes": None,
            }
            for name in ("shopping", "shopping_admin", "reddit", "gitlab", "wikipedia", "map")
        ],
        "blockers": reasons,
        "source_revision": "6473f72db5dcefc97b5725b59e734504edc28a21",
        "source_urls": [
            "https://github.com/ServiceNow/webarena-verified/blob/"
            "6473f72db5dcefc97b5725b59e734504edc28a21/docs/environments/map.md",
            "https://github.com/ServiceNow/webarena-verified/blob/"
            "6473f72db5dcefc97b5725b59e734504edc28a21/docs/environments/wikipedia.md",
        ],
    }
    registry._atomic_json(registry.root(data_dir) / "webarena-verified-tools.json", result)
    return result
