"""Explicit, bounded preparation of immutable official coding-suite snapshots."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import httpx

from benchmaxxing.environment import EnvironmentError, _docker, _inspect_image, _require_success
from benchmaxxing.providers import downloads, registry
from benchmaxxing.providers.accounting import capacity_preparation
from benchmaxxing.providers.coding import UPSTREAM_REVISIONS, convert_coding
from benchmaxxing.providers.preparation import check_docker_capacity, prepare_suite

SOURCE_TOOLS_IMAGE = "benchmaxxing/source-tools:pyarrow-19.0.1"
SOURCE_TOOLS_DOCKERFILE = """FROM python:3.11-slim
RUN python -m pip install --no-cache-dir pyarrow==19.0.1 \
 && mkdir -p /workspace /grader && chown -R 65534:65534 /workspace /grader
ENV HF_HUB_OFFLINE=1 HF_DATASETS_OFFLINE=1 PIP_NO_INDEX=1
USER 65534:65534
WORKDIR /workspace
"""

SOURCES: dict[str, dict[str, Any]] = {
    "evalplus-humaneval": {
        "repo": "evalplus/humanevalplus",
        "revision": "d32357cf319e50e9c8d8dab5ea876c72b0fd321b",
        "file": "data/test-00000-of-00001-5973903632b82d40.parquet",
        "rows": 164,
    },
    "evalplus-mbpp": {
        "repo": "evalplus/mbppplus",
        "revision": "b2d74c91837c3f2a20c1299ae98133cbe7cfa077",
        "file": "data/test-00000-of-00001-d5781c9c51e02795.parquet",
        "rows": 378,
    },
    "bigcodebench": {
        "repo": "bigcode/bigcodebench",
        "revision": "b74c0d0bf70d2c0bc459be537895cca163007f1a",
        "file": "data/v0.1.4-00000-of-00001.parquet",
        "rows": 1140,
    },
    "swebench-lite": {
        "repo": "SWE-bench/SWE-bench_Lite",
        "revision": "b0dde1093fe417d83b7184254edf8199c1f0dff5",
        "file": "data/test-00000-of-00001.parquet",
        "rows": 300,
    },
    "swebench-verified": {
        "repo": "SWE-bench/SWE-bench_Verified",
        "revision": "78f471bf655a3137b2e8a75af1501690ec009ec3",
        "file": "data/test-00000-of-00001.parquet",
        "rows": 500,
    },
    "scicode-verified": {
        "repo": "shhu2001/SciCode-Verified",
        "revision": "eea11a866be6860725258702b39ef8651ed26abd",
        "file": "data/problems_test.jsonl",
        "rows": 64,
    },
}

_PARQUET_CONVERTER = """import json,sys
import pyarrow as pa
import pyarrow.parquet as pq
raw=sys.stdin.buffer.read(33554433)
if len(raw)>33554432: raise ValueError('Parquet source too large')
table=pq.read_table(pa.BufferReader(raw))
if table.num_rows>20000: raise ValueError('Too many source rows')
for batch in table.to_batches(max_chunksize=32):
 for row in batch.to_pylist():
  print(json.dumps(row,ensure_ascii=False,allow_nan=False,separators=(',',':')))
"""


@capacity_preparation
def prepare_source_tools(data_dir: Path) -> dict[str, Any]:
    """Explicitly build the small parser image, then pin and probe its identity."""
    try:
        info = _inspect_image(SOURCE_TOOLS_IMAGE)
    except EnvironmentError:
        check_docker_capacity(data_dir, 1024 * 1024 * 1024)
        build = downloads.cache_root(data_dir) / "build" / "source-tools"
        build.mkdir(parents=True, exist_ok=True)
        downloads.write_prepared_file(
            data_dir, build / "Dockerfile", SOURCE_TOOLS_DOCKERFILE.encode("utf-8")
        )
        _require_success(
            _docker(
                ["build", "-t", SOURCE_TOOLS_IMAGE, str(build)],
                timeout=600,
                output_limit=1048576,
            ),
            "Source-tools build failed",
        )
        info = _inspect_image(SOURCE_TOOLS_IMAGE)
    version = _require_success(
        _docker(
            [
                "run",
                "--rm",
                "--pull",
                "never",
                "--network",
                "none",
                "--read-only",
                "--user",
                "65534:65534",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--memory",
                "512m",
                "--pids-limit",
                "64",
                "--entrypoint",
                "python",
                info["Id"],
                "-c",
                "import pyarrow; print(pyarrow.__version__)",
            ],
            timeout=30,
            output_limit=4096,
        ),
        "Source-tools runtime probe failed",
    ).strip()
    if version != "19.0.1" or info.get("Os") != "linux":
        raise ValueError("Source-tools image has a different parser version or platform")
    after_probe = _inspect_image(info["Id"])
    info["Size"] = max(int(info.get("Size", 0)), int(after_probe.get("Size", 0)))
    record = {
        "schema_version": 1,
        "image_id": info["Id"],
        "image_size_bytes": info.get("Size", 0),
        "pyarrow_version": version,
        "dockerfile_sha256": hashlib.sha256(SOURCE_TOOLS_DOCKERFILE.encode()).hexdigest(),
    }
    registry._atomic_json(registry.root(data_dir) / "source-tools.json", record)
    return record


def _convert_parquet(data_dir: Path, source: Path, destination: Path) -> dict[str, Any]:
    receipt_path = destination.with_name(destination.name + ".conversion.json")
    digest = registry.file_hash(source)
    converter_hash = hashlib.sha256(_PARQUET_CONVERTER.encode()).hexdigest()
    if destination.is_file() and receipt_path.is_file():
        record: dict[str, Any] = json.loads(receipt_path.read_text(encoding="utf-8"))
        if (
            record.get("source_sha256") != digest
            or record.get("converter_sha256") != converter_hash
            or record.get("jsonl_sha256") != registry.file_hash(destination)
        ):
            raise ValueError("Cached Parquet conversion changed; inspect before preparing again")
        return record
    downloads.check_capacity(data_dir, 256 * 1024 * 1024)
    runtime = prepare_source_tools(data_dir)
    result = _docker(
        [
            "run",
            "--rm",
            "-i",
            "--pull",
            "never",
            "--network",
            "none",
            "--read-only",
            "--user",
            "65534:65534",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--memory",
            "2048m",
            "--memory-swap",
            "2048m",
            "--cpus",
            "1",
            "--pids-limit",
            "64",
            "-e",
            "OPENBLAS_NUM_THREADS=1",
            "--entrypoint",
            "python",
            runtime["image_id"],
            "-c",
            _PARQUET_CONVERTER,
        ],
        timeout=180,
        input_path=source,
        output_limit=256 * 1024 * 1024,
    )
    output = _require_success(result, "Isolated Parquet conversion failed")
    downloads.write_prepared_file(data_dir, destination, output.encode("utf-8"))
    record = {
        "schema_version": 1,
        "source_sha256": digest,
        "converter_sha256": converter_hash,
        "jsonl_sha256": registry.file_hash(destination),
        "converter_image_id": runtime["image_id"],
    }
    registry._atomic_json(receipt_path, record)
    return record


def _fetch_scicode_assets(data_dir: Path, directory: Path, base_url: str) -> None:
    downloads.download(
        data_dir,
        base_url + "/test_data_cleaned.h5",
        directory / "test_data_cleaned.h5",
        max_bytes=1500 * 1024 * 1024,
    )
    target = directory / "test_data_cleaned.h5"
    with target.open("rb") as source:
        md5 = hashlib.file_digest(source, "md5").hexdigest()
    if md5 != "2b41a7df40ddc23ce651ec05b8ecb6f8":
        raise ValueError("SciCode HDF5 differs from the published v2 checksum")
    downloads.download(
        data_dir, base_url + "/manifest.json", directory / "manifest.json", max_bytes=65536
    )
    downloads.download(data_dir, base_url + "/LICENSE", directory / "LICENSE", max_bytes=65536)
    source_url = (
        "https://raw.githubusercontent.com/flyingwagner/scicode-verified/"
        + UPSTREAM_REVISIONS["scicode"]
        + "/"
    )
    for relative in [
        "eval_clean/vendor/scicode/__init__.py",
        "eval_clean/vendor/scicode/parse/__init__.py",
        "eval_clean/vendor/scicode/parse/parse.py",
        "eval_clean/vendor/scicode/compare/__init__.py",
        "eval_clean/vendor/scicode/compare/cmp.py",
        "eval_clean/vendor/LICENSE",
        "eval_clean/vendor/eval_data/13.6.txt",
        "eval_clean/vendor/eval_data/62.1.txt",
        "eval_clean/vendor/eval_data/76.3.txt",
    ]:
        destination = directory / relative
        api_path = destination.with_name(destination.name + ".github.json")
        if not api_path.is_file():
            try:
                downloads.download(data_dir, source_url + relative, destination, max_bytes=262144)
                continue
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 501:
                    raise
        # Some gateways reject chunked empty raw GitHub files. The immutable
        # contents endpoint provides the same bytes with a Git blob checksum.
        api_url = (
            "https://api.github.com/repos/flyingwagner/scicode-verified/contents/"
            + relative
            + "?ref="
            + UPSTREAM_REVISIONS["scicode"]
        )
        downloads.download(data_dir, api_url, api_path, max_bytes=524288)
        metadata = json.loads(api_path.read_text(encoding="utf-8"))
        if metadata.get("encoding") != "base64" or metadata.get("type") != "file":
            raise ValueError("GitHub contents endpoint did not return a regular source file")
        payload = base64.b64decode(metadata["content"])
        git_hash = hashlib.sha1(b"blob " + str(len(payload)).encode() + b"\0" + payload).hexdigest()
        if git_hash != metadata["sha"] or len(payload) > 262144:
            raise ValueError("GitHub source blob checksum or size differs")
        if destination.exists() and destination.read_bytes() != payload:
            raise ValueError("Prepared GitHub source file changed")
        downloads.write_prepared_file(data_dir, destination, payload)
        registry._atomic_json(
            destination.with_name(destination.name + ".github-receipt.json"),
            {
                "url": api_url,
                "git_blob_sha1": git_hash,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "bytes": len(payload),
            },
        )


@capacity_preparation
def fetch_coding_sources(data_dir: Path, suite_id: str) -> dict[str, Any]:
    """Prepare a complete pinned dataset; never start model inference or candidate code.

    The returned source_path is suitable for registry.convert/install. Imports
    remain unprepared until their actual native runtime probes also pass.
    """
    if suite_id not in SOURCES:
        raise ValueError("Unknown coding source suite")
    config = SOURCES[suite_id]
    revision = str(config["revision"])
    directory = downloads.cache_root(data_dir) / "sources" / suite_id / revision
    base_url = f"https://huggingface.co/datasets/{config['repo']}/resolve/{revision}"
    source_path = directory / "records.jsonl"
    raw_path = source_path if suite_id == "scicode-verified" else directory / "source.parquet"
    receipt = downloads.download(
        data_dir, base_url + "/" + config["file"], raw_path, max_bytes=32 * 1024 * 1024
    )
    downloads.download(data_dir, base_url + "/README.md", directory / "README.md", max_bytes=262144)
    if suite_id == "scicode-verified":
        _fetch_scicode_assets(data_dir, directory, base_url)
        with source_path.open("rb") as stream:
            md5 = hashlib.file_digest(stream, "md5").hexdigest()
        if md5 != "5c604d8dbf52642bd94e13b92c8f52eb":
            raise ValueError("SciCode JSONL differs from the published v2 checksum")
    else:
        _convert_parquet(data_dir, raw_path, source_path)
        if suite_id.startswith("swebench-"):
            license_url = (
                "https://raw.githubusercontent.com/SWE-bench/SWE-bench/"
                + UPSTREAM_REVISIONS["swebench"]
                + "/LICENSE"
            )
        else:
            repo = (
                "evalplus/evalplus"
                if suite_id.startswith("evalplus-")
                else "bigcode-project/bigcodebench"
            )
            family = "evalplus" if suite_id.startswith("evalplus-") else "bigcodebench"
            license_url = (
                f"https://raw.githubusercontent.com/{repo}/{UPSTREAM_REVISIONS[family]}/LICENSE"
            )
        downloads.download(data_dir, license_url, directory / "LICENSE", max_bytes=65536)
    digest = registry.file_hash(source_path)
    tasks = convert_coding(suite_id, source_path, revision=revision, source_sha256=digest)
    if len(tasks) != config["rows"]:
        raise ValueError(f"Expected {config['rows']} upstream rows; converted {len(tasks)}")
    record = {
        "suite_id": suite_id,
        "source_path": str(source_path.resolve()),
        "revision": revision,
        "source_sha256": digest,
        "tasks": len(tasks),
        "download": receipt,
        "runtime_prepared": False,
    }
    registry._atomic_json(directory / "prepared-source.json", record)
    return record


@capacity_preparation
def prepare_coding_runtime(data_dir: Path, suite_id: str) -> dict[str, Any]:
    """Explicitly build a supported offline runtime and verify every imported task.

    SWE-bench requires a separately prepared image for each repository instance;
    this function intentionally does not download hundreds of large images.
    Docker build files are distributed in the source checkout under examples.
    """
    config = {
        "evalplus-humaneval": ("evalplus-frozen", "benchmaxxing/evalplus-frozen:1", 1073741824),
        "evalplus-mbpp": ("evalplus-frozen", "benchmaxxing/evalplus-frozen:1", 1073741824),
        "bigcodebench": ("bigcodebench", "benchmaxxing/bigcodebench-frozen:1", 50000000000),
        "scicode-verified": ("scicode-verified", "benchmaxxing/scicode-verified:1", 3000000000),
    }
    if suite_id not in config:
        raise ValueError(
            "This suite needs explicitly prepared per-instance images and offline evaluation "
            "scripts; prepare those separately before prepare_suite"
        )
    name, image, reservation = config[suite_id]
    dockerfile = (
        Path(__file__).resolve().parents[3] / "examples" / "providers" / f"{name}.Dockerfile"
    )
    if not dockerfile.is_file():
        raise ValueError("Use a Benchmaxxing source checkout to build its provider runtime recipe")
    definition = dockerfile.read_bytes()
    record_path = registry.root(data_dir) / f"{name}-tools.json"
    existing = json.loads(record_path.read_text()) if record_path.is_file() else None
    definition_hash = hashlib.sha256(definition).hexdigest()
    if existing is not None:
        if existing.get("dockerfile_sha256") != definition_hash:
            raise ValueError(
                "Prepared runtime recipe changed; inspect the old receipt before rebuilding"
            )
        info = _inspect_image(existing["image_id"])
        if info.get("Id") != existing["image_id"]:
            raise ValueError("Prepared runtime image identity changed")
    else:
        build_args = []
        wheels = None
        if suite_id == "scicode-verified":
            from benchmaxxing.providers.coding_wheels import prepare_scicode_wheels

            wheels = prepare_scicode_wheels(data_dir)
            reservation = int(wheels["reservation_bytes"])
            build_args = [
                "--network",
                "none",
                "--build-arg",
                "BASE_IMAGE=" + wheels["base_reference"],
            ]
        physical = check_docker_capacity(data_dir, reservation)
        build = downloads.cache_root(data_dir) / "build" / name
        build.mkdir(parents=True, exist_ok=True)
        downloads.write_prepared_file(data_dir, build / "Dockerfile", definition)
        _require_success(
            _docker(
                ["build", "--pull=false", *build_args, "-t", image, str(build)],
                timeout=3600,
                output_limit=4194304,
            ),
            "Coding runtime build failed",
        )
        info = _inspect_image(image)
        downloads.check_capacity(data_dir, int(info.get("Size", 0)))
        existing = {
            "schema_version": 1,
            "image_id": info["Id"],
            "image_size_bytes": info.get("Size", 0),
            "dockerfile_sha256": definition_hash,
            "physical_capacity": physical,
            "wheel_manifest": wheels,
        }
        registry._atomic_json(record_path, existing)
    tasks = [
        registry.get_imported(data_dir, path.stem)
        for path in sorted((registry.root(data_dir) / "tasks").glob(f"imported.{suite_id}-*.json"))
    ]
    if not tasks:
        raise ValueError("Import the suite's complete pinned source before preparing its runtime")
    result = prepare_suite(data_dir, tasks, image=info["Id"])
    after_probe = _inspect_image(info["Id"])
    existing["image_size_bytes"] = max(
        int(existing.get("image_size_bytes", 0)), int(after_probe.get("Size", 0))
    )
    registry._atomic_json(record_path, existing)
    return {"runtime": existing, **result}


@capacity_preparation
def swebench_preparation_plan(data_dir: Path, task_id: str) -> dict[str, Any]:
    """Write a reviewable per-instance offline build plan without pulling images.

    Only complete, single-line editable project installs can be moved from the
    upstream evaluation script. Other installation forms need their own audited
    recipe. Test commands, patches, parser and expected test IDs are unchanged.
    """
    task = registry.get_imported(data_dir, task_id)
    if task.suite_id not in {"swebench-lite", "swebench-verified"}:
        raise ValueError("Choose a SWE-bench instance")
    row = json.loads(task.private_files["upstream.json"])
    image = row.get("image")
    script = row.get("eval_script")
    if not isinstance(image, str) or not isinstance(script, str):
        raise ValueError("The upstream snapshot lacks its native image or evaluation script")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:@-]{0,511}", image):
        raise ValueError("The upstream image reference is malformed")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-[0-9]+", task.upstream_id):
        raise ValueError("The upstream instance identity is malformed")
    lines = script.splitlines(keepends=True)
    install_pattern = re.compile(
        r"^(?:python -m pip|pip) install -e \.(?:\[[A-Za-z0-9_,.-]+\])?(?: --verbose)?$"
    )
    moved = [index for index, line in enumerate(lines) if install_pattern.fullmatch(line.strip())]
    if not moved:
        raise ValueError(
            "This instance needs a reviewed preparation recipe for its install commands"
        )
    prefix = "".join(lines[: max(moved) + 1])
    if any(word in prefix for word in ("git apply", "<<", "pytest", "Start Test Output")):
        raise ValueError(
            "Dependency setup is interleaved with grading; review this instance separately"
        )
    offline = "".join(line for index, line in enumerate(lines) if index not in moved)
    if re.search(
        r"\b(pip(?:3)?\s+install|apt(?:-get)?\s+(?:install|update)|conda\s+(?:install|create)|npm\s+(?:ci|install)|curl\b|wget\b)",
        offline,
    ):
        raise ValueError("An additional dependency download remains in the evaluation script")
    directory = downloads.cache_root(data_dir) / "build" / "swebench" / task.upstream_id
    directory.mkdir(parents=True, exist_ok=True)
    downloads.write_prepared_file(
        data_dir, directory / "prepare-dependencies.sh", prefix.encode("utf-8")
    )
    downloads.write_prepared_file(data_dir, directory / "eval.offline.sh", offline.encode("utf-8"))
    dockerfile = (
        "ARG BASE_IMAGE\nFROM ${BASE_IMAGE}\nUSER root\n"
        "COPY prepare-dependencies.sh /tmp/benchmaxxing-prepare-dependencies.sh\n"
        "RUN bash /tmp/benchmaxxing-prepare-dependencies.sh"
        " && rm /tmp/benchmaxxing-prepare-dependencies.sh\n"
        "RUN python -m pip install --no-cache-dir git+https://github.com/SWE-bench/SWE-bench.git@"
        + UPSTREAM_REVISIONS["swebench"]
        + "\n"
        "RUN mkdir -p /workspace /grader && chown 65534:65534 /workspace /grader"
        " && chown -R 65534:65534 /testbed\n"
        "ENV HF_HUB_OFFLINE=1 HF_DATASETS_OFFLINE=1 PIP_NO_INDEX=1\n"
        "USER 65534:65534\nWORKDIR /testbed\n"
    )
    downloads.write_prepared_file(data_dir, directory / "Dockerfile", dockerfile.encode("utf-8"))
    record = {
        "schema_version": 1,
        "task_id": task.id,
        "upstream_id": task.upstream_id,
        "source_sha256": task.source_sha256,
        "revision": task.revision,
        "upstream_image": image,
        "directory": str(directory),
        "preparation_script_sha256": registry.file_hash(directory / "prepare-dependencies.sh"),
        "upstream_eval_sha256": hashlib.sha256(script.encode()).hexdigest(),
        "offline_eval_sha256": hashlib.sha256(offline.encode()).hexdigest(),
        "moved_install_lines": [lines[index].strip() for index in moved],
        "variant": "benchmaxxing-prepared-editable-patch-v1",
        "runtime_prepared": False,
    }
    registry._atomic_json(directory / "plan.json", record)
    return record


@capacity_preparation
def prepare_swebench_instance(
    data_dir: Path, task_id: str, *, reservation_bytes: int, pull: bool = False
) -> dict[str, Any]:
    """Explicit per-instance build, source adaptation, import, and offline probe.

    The caller supplies a storage reservation after inspecting the image's
    manifest. This operation never runs during model evaluation.
    """
    if reservation_bytes < 1000000000:
        raise ValueError("Reserve at least 1 GB for the per-instance runtime build")
    plan = swebench_preparation_plan(data_dir, task_id)
    physical = check_docker_capacity(data_dir, reservation_bytes)
    requested = str(plan["upstream_image"])
    if pull:
        _require_success(
            _docker(["pull", requested], timeout=1800), "SWE-bench instance pull failed"
        )
    base = _inspect_image(requested)
    references = base.get("RepoDigests", [])
    if not references:
        raise ValueError("Prepare a local image with its immutable upstream repository digest")
    pinned_base = references[0]
    _inspect_image(pinned_base)
    directory = Path(plan["directory"])
    image = "benchmaxxing/swe-" + hashlib.sha256(task_id.encode()).hexdigest()[:20] + ":1"
    _require_success(
        _docker(
            [
                "build",
                "--pull=false",
                "--build-arg",
                "BASE_IMAGE=" + pinned_base,
                "-t",
                image,
                str(directory),
            ],
            timeout=3600,
            output_limit=4194304,
        ),
        "SWE-bench per-instance preparation failed",
    )
    info = _inspect_image(image)
    downloads.check_capacity(data_dir, int(info.get("Size", 0)))
    receipt = {
        **plan,
        "base_image_id": base["Id"],
        "image_id": info["Id"],
        "image_size_bytes": info.get("Size", 0),
        "physical_capacity": physical,
    }
    receipt_path = directory / "runtime.json"
    registry._atomic_json(receipt_path, receipt)
    # Utility receipt accounts for the prepared image before task registration.
    registry._atomic_json(registry.root(data_dir) / ("swe-" + task_id + "-tools.json"), receipt)
    original = registry.get_imported(data_dir, task_id)
    row = json.loads(original.private_files["upstream.json"])
    row["benchmaxxing_prepared"] = {
        "image": info["Id"],
        "eval_script": (directory / "eval.offline.sh").read_text(encoding="utf-8"),
        "receipt_sha256": registry.file_hash(receipt_path),
        "original_source_sha256": original.source_sha256,
    }
    source = directory / "records.jsonl"
    downloads.write_prepared_file(
        data_dir, source, (json.dumps(row, allow_nan=False) + "\n").encode("utf-8")
    )
    for asset in original.assets:
        if asset.scope == "runtime" and asset.target.startswith("provenance/") and asset.sha256:
            downloads.write_prepared_file(
                data_dir,
                directory / Path(asset.target).name,
                (registry.asset_root(data_dir) / asset.sha256).read_bytes(),
            )
    tasks = convert_coding(
        original.suite_id,
        source,
        revision=original.revision,
        source_sha256=registry.file_hash(source),
        variant=plan["variant"],
    )
    registry.install(data_dir, source, tasks)
    result = prepare_suite(data_dir, tasks, image=info["Id"])
    return {"runtime": receipt, **result}
