"""The installed, versioned reference task and deterministic test participants."""

import hashlib
from importlib.resources import files

from benchmaxxing.contracts import TaskSpec

DEFAULT_IMAGE = "python:3.13-slim"
REFERENCE_NAMES = ("TASK.md", "solution.py", "public_tests.py")


def reference_files() -> dict[str, bytes]:
    directory = files("benchmaxxing").joinpath("reference")
    return {name: directory.joinpath(name).read_bytes() for name in REFERENCE_NAMES}


PROMPT = reference_files()["TASK.md"].decode("utf-8")

GOOD_SOLUTION = """def merge_intervals(intervals):
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged
"""

BAD_SOLUTION = "def merge_intervals(intervals):\n    return []\n"


def get_task() -> TaskSpec:
    digest = hashlib.sha256()
    for name, content in sorted(reference_files().items()):
        digest.update(name.encode("utf-8") + b"\0" + content + b"\0")
    return TaskSpec(description=PROMPT, task_hash=digest.hexdigest(), image=DEFAULT_IMAGE)
