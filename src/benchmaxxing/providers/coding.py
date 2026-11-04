"""Offline, data-only importers for published coding benchmarks.

These converters never import a solution, run an upstream harness, download a
dataset, or install a dependency. Embedded drivers are *files* for a fresh native
grader sandbox; the controller must never execute them on the host.
"""

from __future__ import annotations

import ast
import bz2
import gzip
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

from benchmaxxing.providers.contracts import ImportedTask, NativeRecipe, SourceAsset

SUITES = {
    "evalplus-humaneval",
    "evalplus-mbpp",
    "bigcodebench",
    "swebench-lite",
    "swebench-verified",
    "scicode-verified",
}
UPSTREAM_REVISIONS = {
    "evalplus": "26d6d00bb1fd0fa37f39c99d5290da67891d1c5e",
    "bigcodebench": "09dd993f46c3fbf3a799465bb96d524edcb0b199",
    "swebench": "02e7a74ffd0b707aab73d203fe87bdc7c76afc8e",
    "scicode": "0b40bd4a3a8a7820cd7eac68f1dbee590bac69b4",
}
DATASET_REVISIONS = {
    "bigcodebench": "b74c0d0bf70d2c0bc459be537895cca163007f1a",
    "swebench-lite": "b0dde1093fe417d83b7184254edf8199c1f0dff5",
    "swebench-verified": "78f471bf655a3137b2e8a75af1501690ec009ec3",
    "scicode-verified": "eea11a866be6860725258702b39ef8651ed26abd",
}
MAX_SOURCE_BYTES = 256 * 1024 * 1024
MAX_RECORDS = 20000
RESULT_PATH = "/tmp/benchmaxxing-grade/result.json"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate source JSON field: {key}")
        result[key] = value
    return result


def _constant(value: str) -> Any:
    raise ValueError(f"Non-finite JSON value: {value}")


def _loads(value: str) -> Any:
    try:
        return json.loads(value, object_pairs_hook=_object, parse_constant=_constant)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("Invalid benchmark JSON") from exc


def _read_records(source: Path) -> list[dict[str, Any]]:
    if source.is_symlink() or not source.is_file():
        raise ValueError("Coding source must be a regular JSON/JSONL file")
    if source.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError("Coding source exceeds the 256 MiB input limit")
    suffix = source.suffix.lower()
    if suffix == ".parquet":
        raise ValueError(
            "Convert Parquet to JSONL in the explicit preparation container; "
            "the offline controller does not load Parquet dependencies"
        )
    opener = gzip.open if suffix == ".gz" else bz2.open if suffix == ".bz2" else open
    with opener(source, "rb") as handle:
        raw = handle.read(MAX_SOURCE_BYTES + 1)
    if len(raw) > MAX_SOURCE_BYTES:
        raise ValueError("Decompressed coding source exceeds 256 MiB")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("Coding source must be UTF-8") from exc
    if not text.strip():
        raise ValueError("Coding source is empty")
    # JSONL files can contain one object; JSON exports may be arrays or one object.
    try:
        decoded = _loads(text)
    except ValueError:
        decoded = [_loads(line) for line in text.splitlines() if line.strip()]
    records = decoded if isinstance(decoded, list) else [decoded]
    if not records or len(records) > MAX_RECORDS:
        raise ValueError("Coding source must contain between 1 and 20000 records")
    if any(not isinstance(row, dict) for row in records):
        raise ValueError("Each coding record must be an object")
    return records


def _text(row: dict[str, Any], key: str, *, empty: bool = False) -> str:
    value = row.get(key)
    if not isinstance(value, str) or (not empty and not value.strip()):
        raise ValueError(f"Expected source string field {key}")
    return value


def _strings(row: dict[str, Any], key: str) -> list[str]:
    value = row.get(key)
    if isinstance(value, str):  # Original SWE-bench serialized these columns.
        value = _loads(value)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"Expected string list field {key}")
    return value


def _identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError("Invalid upstream Python entry point")
    return value


def _identity(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("Upstream task identity must be a string or integer")
    result = str(value)
    if not result or len(result) > 1024 or any(ord(c) < 32 for c in result):
        raise ValueError("Invalid upstream task identity")
    return result


def _recipe(family: str, **kwargs: Any) -> NativeRecipe:
    return NativeRecipe(
        family=family,
        grader_command=["python", "/grader/grade.py"],
        grader_result_path=RESULT_PATH,
        network="none",
        **kwargs,
    )


def convert_coding(
    suite_id: str,
    source: Path,
    *,
    revision: str,
    source_sha256: str,
    variant: str | None = None,
) -> list[ImportedTask]:
    """Convert an explicitly prepared JSON export, preserving upstream identities.

    ``source_sha256`` is the SHA-256 of the input file as stored (compressed bytes
    for .gz/.bz2). Associated assets are resolved relative to its parent and are
    copied/hashed separately by the bundle installer. A source checksum does not
    imply the required image or auxiliary grading assets have been prepared.
    """
    if suite_id not in SUITES:
        raise ValueError(f"Unsupported coding suite: {suite_id}")
    if not re.fullmatch(r"[a-f0-9]{64}", source_sha256):
        raise ValueError("Expected source SHA-256")
    if not revision or revision in {"main", "master", "latest", "HEAD"}:
        raise ValueError("Pin a dataset revision or release, not a mutable branch")
    source = Path(source)
    if source.is_symlink() or not source.is_file():
        raise ValueError("Coding source must be a regular file")
    if source.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError("Coding source exceeds the 256 MiB input limit")
    with source.open("rb") as handle:
        actual = hashlib.file_digest(handle, "sha256").hexdigest()
    if actual != source_sha256:
        raise ValueError("Coding source SHA-256 mismatch")
    result = []
    identities = set()
    for row in _read_records(source):
        if suite_id.startswith("evalplus-"):
            task = _evalplus(suite_id, row, revision, source_sha256, variant)
        elif suite_id == "bigcodebench":
            task = _bigcodebench(row, revision, source_sha256, variant)
        elif suite_id.startswith("swebench-"):
            task = _swebench(suite_id, row, revision, source_sha256, variant)
        else:
            task = _scicode(row, source.parent, revision, source_sha256, variant)
        if task.upstream_id in identities:
            raise ValueError(f"Duplicate upstream task identity: {task.upstream_id}")
        for name in ("LICENSE", "README.md"):
            path = source.parent / name
            if path.is_file() and not path.is_symlink():
                task.assets.append(
                    SourceAsset(source=name, target="provenance/" + name, scope="runtime")
                )
        identities.add(task.upstream_id)
        result.append(task)
    return result


# The following strings are written only into the grader area of a bundle.
_DRIVER_HEADER = '''"""Benchmaxxing adapter: execute only inside a fresh grader container."""
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path("/grader")
OUT = Path("/tmp/benchmaxxing-grade")
OUT.mkdir(parents=True, exist_ok=True)
os.environ.update({"HF_HUB_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1",
                   "XDG_CACHE_HOME": str(OUT / "cache"), "HOME": str(OUT),
                   "PIP_NO_INDEX": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1"})
os.chdir(OUT)
TASK = json.loads((ROOT / "task.json").read_text())

def finish(checks, evidence):
    passed = bool(checks) and all(check["passed"] for check in checks)
    result = {"passed": passed, "score": float(passed), "checks": checks,
              "evidence": evidence}
    (OUT / "result.json").write_text(json.dumps(result, allow_nan=False))

def run_script(body, name, timeout=1800):
    marker = "BENCHMAXXING_TESTS_COMPLETED_" + uuid.uuid4().hex
    path = OUT / (name + ".py")
    path.write_text(body + "\\nprint(" + repr(marker) + ", flush=True)\\n")
    log_path = OUT / (name + ".log")
    with log_path.open("wb") as log:
        try:
            proc = subprocess.run([sys.executable, str(path)], stdout=log,
                                  stderr=subprocess.STDOUT, timeout=timeout, check=False)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            return False, {"reason": "test_timeout", "log": str(log_path)}
    # Bound evidence reads; the outer sandbox also bounds writable/output volume.
    with log_path.open("rb") as log:
        log.seek(max(0, log_path.stat().st_size - 16384))
        tail = log.read(16384).decode("utf-8", errors="replace")
    return code == 0 and marker in tail, {"returncode": code,
        "completed": marker in tail, "output_tail": tail.replace(marker, "[completed]")}

'''

_COMPILED_EVALPLUS_DRIVER = (
    _DRIVER_HEADER
    + """
mode = TASK["adapter_mode"]
body = "import runpy\\nns = runpy.run_path('/workspace/solution.py')\\n"
if mode == "humaneval-compiled":
    body += (ROOT / "tests.py").read_text()
    body += "\\ncheck(ns[" + repr(TASK["entry_point"]) + "])\\n"
else:
    body += "globals().update(ns)\\n" + (ROOT / "tests.py").read_text()
passed, evidence = run_script(body, "evalplus")
finish([{"name": "upstream-expanded-tests", "passed": passed}], evidence)
"""
)

_EVALPLUS_DRIVER = (
    _DRIVER_HEADER
    + """
dataset = TASK["adapter_dataset"]
data_path = OUT / "dataset.jsonl"
data_path.write_text(json.dumps(TASK["upstream"]) + "\\n")
os.environ[("HUMANEVAL" if dataset == "humaneval" else "MBPP") + "_OVERRIDE_PATH"] = str(data_path)
sample = {"task_id": TASK["upstream"]["task_id"],
          "solution": Path('/workspace/solution.py').read_text()}
samples = OUT / "samples.jsonl"
samples.write_text(json.dumps(sample) + "\\n")
from evalplus.evaluate import evaluate
evaluate(dataset=dataset, samples=str(samples), parallel=1, test_details=True,
         output_file=str(OUT / "native.json"))
native = json.loads((OUT / "native.json").read_text())
result = native["eval"][sample["task_id"]][0]
checks = [{"name": name, "passed": result[name + "_status"] == "pass"}
          for name in ("base", "plus")]
finish(checks, {"upstream_result": result, "native_dataset_hash": native["hash"]})
"""
)


def _evalplus(
    suite: str,
    row: dict[str, Any],
    revision: str,
    digest: str,
    variant: str | None,
) -> ImportedTask:
    humaneval = suite == "evalplus-humaneval"
    dataset = "humaneval" if humaneval else "mbpp"
    task_id = _identity(row.get("task_id"))
    prompt = _text(row, "prompt")
    files: dict[str, str] = {}
    notes = [
        "Single complete source submission; Benchmaxxing interactive harness, "
        "not upstream one-shot generation."
    ]
    if "base_input" in row or "plus_input" in row:
        for key in ("base_input", "plus_input"):
            if not isinstance(row.get(key), list) or not row[key]:
                raise ValueError(f"EvalPlus native JSONL requires nonempty {key}")
        _text(row, "contract", empty=True)
        atol = row.get("atol")
        if (
            isinstance(atol, bool)
            or not isinstance(atol, (int, float))
            or not math.isfinite(atol)
            or atol < 0
        ):
            raise ValueError("EvalPlus atol must be finite and nonnegative")
        entry = _identifier(_text(row, "entry_point"))
        good = prompt + _text(row, "canonical_solution")
        if not task_id.startswith("HumanEval/" if humaneval else "Mbpp/"):
            raise ValueError("Native EvalPlus task_id must retain its upstream prefix")
        private = {
            "task.json": _json({"adapter_dataset": dataset, "upstream": row}),
            "grade.py": _EVALPLUS_DRIVER,
        }
        requirements = [f"evalplus from git commit {UPSTREAM_REVISIONS['evalplus']}"]
        profile = "official-differential-jsonl"
        checks = len(row["base_input"]) + len(row["plus_input"])
        files["solution.py"] = prompt + "\n    raise NotImplementedError\n"
    else:
        tests = _text(row, "test")
        # Hugging Face's published exports contain precomputed native assertions.
        if humaneval:
            entry = _identifier(_text(row, "entry_point"))
            good = prompt + _text(row, "canonical_solution")
            files["solution.py"] = prompt + "\n    raise NotImplementedError\n"
        else:
            good = _text(row, "code")
            parsed = ast.parse(good)
            functions = [n for n in parsed.body if isinstance(n, ast.FunctionDef)]
            if not functions:
                raise ValueError("MBPP compiled export must include a function reference")
            entry = functions[0].name
            imports = _strings(row, "test_imports")
            public_tests = _strings(row, "test_list")
            files["public_tests.py"] = (
                "\n".join(["from solution import *", *imports, *public_tests]) + "\n"
            )
            files["solution.py"] = (
                f"def {entry}({ast.unparse(functions[0].args)}):\n    raise NotImplementedError\n"
            )
        private = {
            "task.json": _json({"adapter_mode": dataset + "-compiled", "entry_point": entry}),
            "tests.py": tests,
            "grade.py": _COMPILED_EVALPLUS_DRIVER,
            "upstream.json": _json(row),
        }
        requirements = ["Python 3.11", "numpy==1.26.4"]
        profile = "hf-frozen-assertions"
        checks = None  # Do not invent a test count by guessing Python syntax.
        notes.append(
            "Published Hugging Face frozen assertions; "
            "not interchangeable with another JSONL/oracle revision."
        )
    return ImportedTask(
        suite_id=suite,
        upstream_id=task_id,
        revision=revision,
        source_sha256=digest,
        title=f"EvalPlus {task_id}",
        prompt=prompt + "\n\nSubmit a complete solution.py with the declared function.",
        category="programming",
        variant=variant or f"benchmaxxing-{profile}-v1",
        public_files=files,
        private_files=private,
        recipe=_recipe(
            "evalplus",
            image="python:3.11-slim",
            requirements=requirements,
            settings={
                "dataset": dataset,
                "profile": profile,
                "harness_revision": UPSTREAM_REVISIONS["evalplus"],
            },
        ),
        source_urls=[
            f"https://huggingface.co/datasets/evalplus/{dataset}plus",
            "https://github.com/evalplus/evalplus",
        ],
        licenses=["Apache-2.0"],
        tags=["coding", "function", dataset],
        check_count=checks,
        comparison_notes=notes,
        good_solution=good,
        bad_solution=f"def {entry}(*args, **kwargs):\n    raise NotImplementedError\n",
    )


_BIGCODEBENCH_DRIVER = (
    _DRIVER_HEADER
    + """
data_path = OUT / "dataset.jsonl"
data_path.write_text(json.dumps(TASK["upstream"]) + "\\n")
os.environ["BIGCODEBENCH_OVERRIDE_PATH"] = str(data_path)
sample = {"task_id": TASK["upstream"]["task_id"],
          "solution": Path('/workspace/solution.py').read_text()}
samples = OUT / "samples.jsonl"
samples.write_text(json.dumps(sample) + "\\n")
from bigcodebench.evaluate import evaluate
evaluate(split=TASK["split"], subset="full", samples=str(samples), execution="local",
         parallel=1, pass_k="1", calibrated=False)
native = json.loads((OUT / "samples_eval_results.json").read_text())
result = native["eval"][sample["task_id"]][0]
finish([{"name": "upstream-unittest", "passed": result["status"] == "pass"}],
       {"upstream_result": result, "split": TASK["split"]})
"""
)


def _bigcodebench(
    row: dict[str, Any],
    revision: str,
    digest: str,
    variant: str | None,
) -> ImportedTask:
    task_id = _text(row, "task_id")
    if not re.fullmatch(r"BigCodeBench/\d+", task_id):
        raise ValueError("Preserve BigCodeBench/<integer> task IDs")
    complete = _text(row, "complete_prompt")
    instruct = _text(row, "instruct_prompt")
    code = _text(row, "code_prompt")
    canonical = _text(row, "canonical_solution")
    _text(row, "test")
    entry = _identifier(_text(row, "entry_point"))
    split = "complete" if variant == "complete" else "instruct"
    return ImportedTask(
        suite_id="bigcodebench",
        upstream_id=task_id,
        revision=revision,
        source_sha256=digest,
        title=f"BigCodeBench {task_id}",
        category="programming",
        prompt=(complete if split == "complete" else instruct)
        + "\n\nSubmit a complete solution.py.",
        variant=f"benchmaxxing-{split}-v1"
        if variant in (None, "complete", "instruct")
        else str(variant),
        public_files={
            "solution.py": (complete if split == "complete" else code)
            + "\n    raise NotImplementedError\n"
        },
        private_files={
            "task.json": _json({"upstream": row, "split": split}),
            "grade.py": _BIGCODEBENCH_DRIVER,
        },
        recipe=_recipe(
            "bigcodebench",
            image="bigcodebench/bigcodebench-evaluate:latest",
            settings={
                "split": split,
                "execution": "local",
                "calibrated": False,
                "harness_revision": UPSTREAM_REVISIONS["bigcodebench"],
            },
            requirements=[
                "Pinned BigCodeBench evaluator and Requirements/requirements-eval.txt",
                "All package/data assets installed during preparation",
            ],
        ),
        source_urls=[
            "https://huggingface.co/datasets/bigcode/bigcodebench",
            "https://github.com/bigcode-project/bigcodebench",
        ],
        licenses=["Apache-2.0"],
        tags=["coding", "libraries", split],
        comparison_notes=[
            "Native local evaluator; preserve Complete/Instruct and source dataset revision.",
            "Raw complete submissions, without upstream sample calibration; "
            "interactive harness variant.",
        ],
        good_solution=complete + "\n" + canonical,
        bad_solution=f"def {entry}(*args, **kwargs):\n    raise NotImplementedError\n",
    )


_SWE_DRIVER = (
    _DRIVER_HEADER
    + """
row = TASK["upstream"]
patch = Path('/workspace/submission.patch').read_text()
applied = subprocess.run(["git", "-C", "/testbed", "apply", "--", "/workspace/submission.patch"],
                         capture_output=True, text=True, check=False)
if applied.returncode:
    finish([{"name": "patch-application", "passed": False}], {"stderr": applied.stderr[-16384:]})
else:
    log_path = OUT / "test_output.txt"
    with log_path.open("wb") as log:
        subprocess.run(["bash", "/grader/eval.sh"], stdout=log, stderr=subprocess.STDOUT,
                       timeout=1800, check=False)
    from swebench.types import TestSpec
    from swebench.harness.grading import get_eval_report
    spec = TestSpec(instance_id=row["instance_id"], image=TASK["image"],
        eval_script_list=[], repo=row["repo"], version=row["version"],
        FAIL_TO_PASS=TASK["fail_to_pass"], PASS_TO_PASS=TASK["pass_to_pass"],
        log_parser=row["log_parser"], eval_type=row["eval_type"])
    native = get_eval_report(spec, {"instance_id": row["instance_id"],
        "model_name_or_path": "benchmaxxing", "model_patch": patch}, str(log_path), True)
    report = native[row["instance_id"]]
    if report.get("infra_failure"):
        raise RuntimeError("Upstream SWE-bench reported an infrastructure failure")
    finish([{"name": "upstream-resolved", "passed": report["resolved"]}], native)
"""
)


def _swebench(
    suite: str,
    row: dict[str, Any],
    revision: str,
    digest: str,
    variant: str | None,
) -> ImportedTask:
    task_id = _text(row, "instance_id")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+-[0-9]+", task_id):
        raise ValueError("Invalid upstream SWE-bench instance identity")
    repo = _text(row, "repo")
    base = _text(row, "base_commit")
    if not re.fullmatch(r"[a-f0-9]{40}", base):
        raise ValueError("SWE-bench base_commit must be a full commit SHA")
    _text(row, "version")
    patch = _text(row, "patch")
    _text(row, "test_patch")
    f2p = _strings(row, "FAIL_TO_PASS")
    p2p = _strings(row, "PASS_TO_PASS")
    if not f2p:
        raise ValueError("SWE-bench requires at least one FAIL_TO_PASS test")
    blockers = []
    prepared = row.get("benchmaxxing_prepared", {})
    if not isinstance(prepared, dict):
        raise ValueError("benchmaxxing_prepared must be an object")
    script = prepared.get("eval_script", row.get("eval_script"))
    image = prepared.get("image", row.get("image"))
    if not isinstance(image, str) or not image:
        image = None
        blockers.append(
            "Prepare the upstream per-instance repository image and record its immutable identity"
        )
    if not isinstance(script, str) or not script.strip():
        script = ""
        blockers.append(
            "Legacy export: prepare eval_script/log_parser/eval_type "
            "using the pinned upstream harness"
        )
    if any(
        not isinstance(row.get(key), str) or not row[key] for key in ("log_parser", "eval_type")
    ):
        blockers.append("Prepare the native log_parser and eval_type fields before grading")
    # Dependencies may be downloaded/installed only by explicit preparation.
    if re.search(
        r"\b(pip(?:3)?\s+install|(?:apt|apt-get)\s+(?:install|update)|"
        r"conda\s+(?:install|create)|npm\s+(?:ci|install)|curl\b|wget\b)",
        script,
    ):
        blockers.append(
            "Upstream eval_script installs/downloads dependencies; prepare them first "
            "and supply a reviewed benchmaxxing_prepared.eval_script offline variant"
        )
    if prepared and not re.fullmatch(r"[a-f0-9]{64}", str(prepared.get("receipt_sha256", ""))):
        raise ValueError("Prepared SWE-bench overrides require a preparation receipt SHA-256")
    private = {
        "task.json": _json(
            {"upstream": row, "image": image, "fail_to_pass": f2p, "pass_to_pass": p2p}
        ),
        "eval.sh": script,
        "upstream.json": _json(row),
        "grade.py": _SWE_DRIVER,
    }
    prompt = _text(row, "problem_statement")
    prompt += (
        f"\n\nRepository: {repo} at {base}, available at /testbed.\n"
        "Produce /workspace/submission.patch as a Git patch. "
        "Do not change the grader or tests."
    )
    return ImportedTask(
        suite_id=suite,
        upstream_id=task_id,
        revision=revision,
        source_sha256=digest,
        title=task_id,
        prompt=prompt,
        category="programming",
        variant=variant or "benchmaxxing-offline-patch-v1",
        public_files={"submission.patch": ""},
        private_files=private,
        submission_file="submission.patch",
        submission_kind="patch",
        recipe=_recipe(
            "swebench",
            image=image,
            workdir="/testbed",
            blockers=blockers,
            settings={
                "repository": repo,
                "base_commit": base,
                "harness_revision": UPSTREAM_REVISIONS["swebench"],
                "prepared_receipt_sha256": prepared.get("receipt_sha256"),
            },
            requirements=[
                "Exact per-instance image with repository and dependencies prepared",
                "Pinned SWE-bench parser installed in grader image; no Docker socket",
            ],
        ),
        source_urls=[
            "https://huggingface.co/datasets/SWE-bench/SWE-bench_"
            + ("Lite" if suite.endswith("lite") else "Verified"),
            "https://github.com/SWE-bench/SWE-bench",
        ],
        licenses=[
            "MIT (harness)",
            "Upstream repository and issue/patch licenses retained; "
            "dataset card has no separate license declaration",
        ],
        tags=["coding", "repository", "patch"],
        check_count=len(f2p) + len(p2p),
        comparison_notes=[
            "Preserves upstream issue, base commit, tests and native resolved criterion.",
            "Offline prepared script/build and Benchmaxxing agent conditions form "
            "a distinct variant; not an unqualified leaderboard score.",
        ],
        good_solution=patch,
        bad_solution="not a git patch\n",
    )


_SCICODE_DRIVER = (
    _DRIVER_HEADER
    + """
sys.path.insert(0, '/grader/vendor')
solution = Path('/workspace/solution.py').read_text()
checks = []
evidence = {}
for step in TASK["steps"]:
    body = "import sys\\nsys.path.insert(0, '/grader/vendor')\\n"
    body += TASK["required_dependencies"] + "\\n" + solution + "\\n"
    body += "from scicode.parse.parse import process_hdf5_to_tuple\\n"
    body += ("targets = process_hdf5_to_tuple(" + repr(step["id"]) + ", "
             + str(len(step["tests"])) + ", '/grader/test_data_cleaned.h5')\\n")
    for index, test in enumerate(step["tests"]):
        body += "target = targets[" + str(index) + "]\\n" + test + "\\n"
    passed, detail = run_script(body, 'step-' + step["id"].replace('.', '-'))
    detail["test_case_count"] = len(step["tests"])
    checks.append({"name": step["id"], "passed": passed})
    evidence[step["id"]] = detail
finish(checks, {"steps": evidence, "metric": "all-scored-substeps",
                "environment": "single-prepared-image"})
"""
)


def _asset(root: Path, source: str, target: str, scope: str = "grader") -> SourceAsset | None:
    path = root / source
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
        return None
    return SourceAsset(source=source, target=target, scope=scope)  # type: ignore[arg-type]


def _scicode(
    row: dict[str, Any],
    root: Path,
    revision: str,
    digest: str,
    variant: str | None,
) -> ImportedTask:
    problem_id = _identity(row.get("problem_id"))
    if not re.fullmatch(r"\d+", problem_id):
        raise ValueError("SciCode problem_id must retain its numeric upstream identity")
    name = _text(row, "problem_name")
    description = _text(row, "problem_description_main")
    dependencies = _text(row, "required_dependencies", empty=True)
    steps = row.get("sub_steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("SciCode requires a nonempty sub_steps list")
    prompt = [
        description,
        str(row.get("problem_background_main", "")),
        str(row.get("problem_io", "")),
        dependencies,
    ]
    skipped = {"13.6", "62.1", "76.3"}
    grading = []
    seen = set()
    blockers = []
    public = {
        "solution.py": dependencies + "\n\n# Implement every function documented in TASK.md.\n"
    }
    for step in steps:
        if not isinstance(step, dict):
            raise ValueError("SciCode substeps must be objects")
        sid = _text(step, "step_number")
        if not re.fullmatch(re.escape(problem_id) + r"\.\d+", sid) or sid in seen:
            raise ValueError("SciCode substep identity mismatch or duplicate")
        seen.add(sid)
        header = _text(step, "function_header")
        prompt += [
            f"Subproblem {sid}",
            _text(step, "step_description_prompt"),
            str(step.get("step_background", "")),
            header,
        ]
        if sid in skipped:
            gold_path = root / "eval_clean" / "vendor" / "eval_data" / f"{sid}.txt"
            if (
                gold_path.is_file()
                and not gold_path.is_symlink()
                and gold_path.resolve().is_relative_to(root.resolve())
            ):
                gold = gold_path.read_text(encoding="utf-8")
                public["solution.py"] += (
                    "\n# Official supplied, unscored step " + sid + "\n" + gold + "\n"
                )
            else:
                blockers.append(f"Prepare the upstream supplied code for unscored substep {sid}")
            continue
        tests = _strings(step, "test_cases")
        if not tests and sid != "72.6":
            raise ValueError("Scored SciCode substeps require tests")
        # The v2 release scores 72.6 with an empty test list. Preserve that
        # upstream condition explicitly instead of inventing hidden assertions.
        grading.append({"id": sid, "tests": tests})
    if not grading:
        raise ValueError("SciCode problem has no scored substeps")
    assets = []
    asset_paths = [
        ("test_data_cleaned.h5", "test_data_cleaned.h5"),
        ("eval_clean/vendor/scicode/__init__.py", "vendor/scicode/__init__.py"),
        ("eval_clean/vendor/scicode/parse/__init__.py", "vendor/scicode/parse/__init__.py"),
        ("eval_clean/vendor/scicode/parse/parse.py", "vendor/scicode/parse/parse.py"),
        ("eval_clean/vendor/scicode/compare/__init__.py", "vendor/scicode/compare/__init__.py"),
        ("eval_clean/vendor/scicode/compare/cmp.py", "vendor/scicode/compare/cmp.py"),
        ("eval_clean/vendor/LICENSE", "vendor/LICENSE"),
    ]
    for src, target in asset_paths:
        asset = _asset(root, src, target)
        if asset is None:
            blockers.append(f"Prepare SciCode grading asset beside source JSONL: {src}")
        else:
            assets.append(asset)
    prompt.append(
        "Submit one complete solution.py defining all requested functions. "
        "This variant grades every scored subproblem against your final combined source; "
        "all must pass."
    )
    good = row.get("general_solution")
    return ImportedTask(
        suite_id="scicode-verified",
        upstream_id=problem_id,
        revision=revision,
        source_sha256=digest,
        title=f"SciCode-Verified {problem_id}: {name}",
        prompt="\n\n".join(prompt),
        category="engineering",
        variant=variant or "benchmaxxing-whole-problem-with-background-single-env-v1",
        public_files=public,
        private_files={
            "grade.py": _SCICODE_DRIVER,
            "upstream.json": _json(row),
            "task.json": _json({"steps": grading, "required_dependencies": dependencies}),
        },
        assets=assets,
        recipe=_recipe(
            "scicode",
            image="python:3.12-slim",
            blockers=blockers,
            requirements=[
                "numpy==1.26.4",
                "scipy==1.13.1",
                "h5py==3.11.0",
                "matplotlib==3.9.0",
                "sympy==1.12.1",
                "Pinned upstream vendor parser/compare and v2 HDF5 grading data",
            ],
            settings={
                "harness_revision": UPSTREAM_REVISIONS["scicode"],
                "background": True,
                "metric": "all-scored-substeps",
                "canonical_generation": "cumulative per-step",
                "canonical_environments": "2024/2025 OR",
                "canonical_timeout_per_step_environment": 1800,
                "target_release": "v2",
                "upstream_h5_md5": "2b41a7df40ddc23ce651ec05b8ecb6f8",
                "zero_assertion_substeps": [s["id"] for s in grading if not s["tests"]],
            },
        ),
        source_urls=[
            "https://huggingface.co/datasets/shhu2001/SciCode-Verified",
            "https://github.com/flyingwagner/scicode-verified",
        ],
        licenses=["Apache-2.0; retain original SciCode and SciCode-Verified attribution"],
        tags=["scientific-coding", "whole-problem", "with-background"],
        check_count=len(grading),
        comparison_notes=[
            "Distinct whole-program, single-environment, with-background adaptation.",
            "Canonical SciCode generates cumulative substeps and uses OR across two "
            "scientific environments; do not compare this variant as canonical.",
        ],
        good_solution=good if isinstance(good, str) and good.strip() else None,
        bad_solution="raise NotImplementedError('deliberately incorrect fixture')\n",
    )
