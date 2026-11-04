"""Exact accounting for explicitly serialized, audited preparation batches."""

from __future__ import annotations

import json
import os
import shutil
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Any, Concatenate

from benchmaxxing.providers import registry

_thread = threading.local()


def storage_roots(data_dir: Path) -> list[Path]:
    from benchmaxxing.providers.downloads import cache_root

    cache = cache_root(data_dir).resolve()
    records = registry.root(data_dir).resolve()
    if records.is_relative_to(cache):
        return [cache]
    if cache.is_relative_to(records):
        return [records]
    return [cache, records]


def _files(directory: Path, excluded: Path | None = None) -> dict[Path, int]:
    result: dict[Path, int] = {}
    if not directory.is_dir():
        return result
    for parent, dirs, files in os.walk(directory, followlinks=False):
        dirs[:] = [
            name
            for name in dirs
            if not (Path(parent) / name).is_symlink() and Path(parent) / name != excluded
        ]
        for name in files:
            path = Path(parent) / name
            if path.is_file() and not path.is_symlink():
                result[path] = path.stat().st_size
    return result


def storage_bytes(data_dir: Path) -> int:
    """Include local import records when their cache lives on another drive."""
    # Job progress is controller state, like runs, not prepared dependency data.
    # Other request threads may publish it while a serialized batch is active.
    excluded = registry.root(data_dir).resolve() / "jobs"
    return sum(sum(_files(directory, excluded).values()) for directory in storage_roots(data_dir))


class _Batch:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir.resolve()
        self.roots = storage_roots(data_dir)
        self.excluded = registry.root(data_dir).resolve() / "jobs"
        self.files: dict[Path, int] = {}
        for directory in self.roots:
            self.files.update(_files(directory, self.excluded))
        self.file_bytes = sum(self.files.values())
        self.receipts: dict[Path, tuple[str, int]] = {}
        self.images: dict[str, int] = {}
        self.image_bytes = 0
        self.imports = registry.root(data_dir).resolve()
        for path in self.files:
            if self.is_receipt(path):
                self.update_image(path)

    def contains(self, path: Path) -> bool:
        return not path.is_relative_to(self.excluded) and any(
            path.is_relative_to(directory) for directory in self.roots
        )

    def is_receipt(self, path: Path) -> bool:
        return path.suffix == ".json" and (
            path.parent in {self.imports / "prepared", self.imports / "runtime-builds"}
            or path.parent == self.imports
            and path.name.endswith("-tools.json")
        )

    def update_image(self, path: Path) -> None:
        old = self.receipts.get(path)
        record: dict[str, Any] = (
            json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        )
        identity = record.get("image_id")
        current = (
            (identity, int(record.get("image_size_bytes", 0)))
            if isinstance(identity, str)
            else None
        )
        if current == old:
            return
        if current is None:
            self.receipts.pop(path, None)
        else:
            self.receipts[path] = current
        if old is not None and (current is None or current[0] != old[0] or current[1] < old[1]):
            previous = self.images.get(old[0], 0)
            maximum = max(
                (size for image, size in self.receipts.values() if image == old[0]), default=0
            )
            self.images[old[0]] = maximum
            self.image_bytes += maximum - previous
        if current is not None and current[1] > self.images.get(current[0], 0):
            previous = self.images.get(current[0], 0)
            self.images[current[0]] = current[1]
            self.image_bytes += current[1] - previous

    @property
    def used(self) -> int:
        return self.file_bytes + self.image_bytes

    def note(self, path: Path) -> None:
        if not self.contains(path):
            return
        old = self.files.get(path, 0)
        if path.is_file() and not path.is_symlink():
            size = path.stat().st_size
            self.files[path] = size
        else:
            size = 0
            self.files.pop(path, None)
        self.file_bytes += size - old
        if self.is_receipt(path):
            self.update_image(path)

    def check(
        self, additional: int, *, directory: Path | None = None, physical: int | None = None
    ) -> None:
        from benchmaxxing.providers.downloads import cache_root

        config = registry.settings(self.data_dir)
        if additional < 0 or self.used + additional > config.storage_limit_bytes:
            raise ValueError("Preparation would exceed the configured total storage cap")
        location = directory or cache_root(self.data_dir)
        while not location.exists():
            location = location.parent
        extra = additional if physical is None else physical
        if shutil.disk_usage(location).free - extra < config.reserve_free_bytes:
            raise ValueError("Preparation would consume the configured free-space reserve")


def active(data_dir: Path) -> _Batch | None:
    batches: dict[Path, _Batch] = getattr(_thread, "batches", {})
    return batches.get(data_dir.resolve())


@contextmanager
def capacity_batch(data_dir: Path) -> Iterator[None]:
    """Hold the global workflow lock; callers must note all direct writes/deletes.

    Download, extraction and registry JSON writers notify automatically. Direct
    artifact writes use note_file; a bulk copy uses note_tree. A final full audit
    detects omitted notifications instead of silently trusting stale accounting.
    """
    from benchmaxxing.providers.downloads import image_bytes
    from benchmaxxing.providers.locking import preparation_scope

    with preparation_scope(data_dir):
        if active(data_dir) is not None:
            yield
            return
        batch = _Batch(data_dir)
        batches: dict[Path, _Batch] = getattr(_thread, "batches", {})
        batches[data_dir.resolve()] = batch
        _thread.batches = batches
        try:
            yield
            measured = storage_bytes(data_dir) + image_bytes(data_dir)
            if measured != batch.used:
                raise ValueError(
                    "Preparation accounting missed a file/image change; batch was not verified"
                )
            batch.check(0)
        finally:
            batches.pop(data_dir.resolve(), None)


def capacity_preparation[**P, R](
    function: Callable[Concatenate[Path, P], R],
) -> Callable[Concatenate[Path, P], R]:
    """Make a direct preparation entrypoint own one exact accounting batch."""

    @wraps(function)
    def execute(data_dir: Path, /, *args: P.args, **kwargs: P.kwargs) -> R:
        with capacity_batch(data_dir):
            return function(data_dir, *args, **kwargs)

    return execute


def note_file(path: Path) -> None:
    normalized = path.absolute()
    for batch in getattr(_thread, "batches", {}).values():
        batch.note(normalized)


def note_tree(directory: Path) -> None:
    normalized = directory.absolute()
    for batch in getattr(_thread, "batches", {}).values():
        current = _files(normalized, batch.excluded)
        previous = {path for path in batch.files if path.is_relative_to(normalized)}
        for path in current.keys() | previous:
            batch.note(path)


def before_file_write(path: Path, size: int, *, value: Any = None) -> None:
    normalized = path.absolute()
    for batch in getattr(_thread, "batches", {}).values():
        if batch.contains(normalized):
            growth = max(0, size - batch.files.get(normalized, 0))
            if batch.is_receipt(normalized) and isinstance(value, dict):
                image_id = value.get("image_id")
                if isinstance(image_id, str):
                    image_size = int(value.get("image_size_bytes", 0))
                    growth += max(0, image_size - batch.images.get(image_id, 0))
            batch.check(growth, directory=normalized.parent, physical=size)
