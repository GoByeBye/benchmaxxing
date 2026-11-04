"""Cross-process serialization for a shared preparation cache."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Concatenate

from filelock import FileLock, Timeout

from benchmaxxing.providers.downloads import cache_root

_thread = threading.local()


@contextmanager
def preparation_scope(data_dir: Path) -> Iterator[None]:
    directory = cache_root(data_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    key = str(directory)
    held: set[str] = getattr(_thread, "held", set())
    if key in held:
        yield
        return
    try:
        with FileLock(str(directory / "workflow.lock"), timeout=0):
            held.add(key)
            _thread.held = held
            try:
                yield
            finally:
                held.remove(key)
    except Timeout as exc:
        raise ValueError(
            "Another preparation operation owns this cache; wait for it to finish and retry"
        ) from exc


def serialized_preparation[**P, R](
    function: Callable[Concatenate[Path, P], R],
) -> Callable[Concatenate[Path, P], R]:
    @wraps(function)
    def execute(data_dir: Path, /, *args: P.args, **kwargs: P.kwargs) -> R:
        with preparation_scope(data_dir):
            return function(data_dir, *args, **kwargs)

    return execute
