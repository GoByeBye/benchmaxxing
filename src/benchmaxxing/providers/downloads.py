"""Explicit bounded preparation downloads; never imported by run-time adapters."""

from __future__ import annotations

import hashlib
import json
import shutil
import tarfile
import uuid
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from filelock import FileLock

from benchmaxxing.providers.accounting import (
    active,
    before_file_write,
    storage_bytes,
)
from benchmaxxing.providers.accounting import (
    capacity_batch as capacity_batch,
)
from benchmaxxing.providers.accounting import (
    note_file as note_file,
)
from benchmaxxing.providers.accounting import (
    note_tree as note_tree,
)
from benchmaxxing.providers.contracts import relative_path
from benchmaxxing.providers.registry import _atomic_json, asset_root, file_hash, root, settings


def cache_root(data_dir: Path) -> Path:
    return asset_root(data_dir).parent


def write_prepared_file(data_dir: Path, destination: Path, payload: bytes) -> None:
    """Account for a retained preparation artifact before publishing its bytes."""
    with capacity_batch(data_dir):
        batch = active(data_dir)
        assert batch is not None
        if not batch.contains(destination.absolute()) or destination.is_symlink():
            raise ValueError("Prepared artifact must be inside the preparation store")
        before_file_write(destination, len(payload))
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".tmp-" + uuid.uuid4().hex)
        try:
            temporary.write_bytes(payload)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
        note_file(destination)


def cache_bytes(data_dir: Path) -> int:
    directory = cache_root(data_dir)
    if not directory.is_dir():
        return 0
    return sum(
        path.stat().st_size
        for path in directory.rglob("*")
        if path.is_file() and not path.is_symlink()
    )


def image_bytes(data_dir: Path) -> int:
    """Count each recorded imported image once, never unrelated user images/volumes."""
    images: dict[str, int] = {}
    receipts = [
        *(root(data_dir) / "prepared").glob("*.json"),
        *(root(data_dir)).glob("*-tools.json"),
        *(root(data_dir) / "runtime-builds").glob("*.json"),
    ]
    for path in receipts:
        record = json.loads(path.read_text(encoding="utf-8"))
        image_id = record.get("image_id")
        if isinstance(image_id, str):
            images[image_id] = max(images.get(image_id, 0), int(record.get("image_size_bytes", 0)))
    return sum(images.values())


def check_capacity(data_dir: Path, additional_bytes: int, *, used_bytes: int | None = None) -> None:
    config = settings(data_dir)
    directory = cache_root(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    batch = active(data_dir)
    if batch is not None:
        if used_bytes is not None and used_bytes < batch.used:
            raise ValueError("Explicit usage cannot understate the active preparation batch")
        batch.check(additional_bytes)
        if used_bytes is None:
            return
    used = storage_bytes(data_dir) + image_bytes(data_dir) if used_bytes is None else used_bytes
    if additional_bytes < 0 or used + additional_bytes > config.storage_limit_bytes:
        raise ValueError("Preparation would exceed the configured total storage cap")
    if shutil.disk_usage(directory).free - additional_bytes < config.reserve_free_bytes:
        raise ValueError("Preparation would consume the configured free-space reserve")


def download(
    data_dir: Path,
    url: str,
    destination: Path,
    *,
    max_bytes: int,
    sha256: str | None = None,
) -> dict[str, object]:
    from benchmaxxing.providers.locking import preparation_scope

    with preparation_scope(data_dir):
        return _download(data_dir, url, destination, max_bytes=max_bytes, sha256=sha256)


def _download(
    data_dir: Path,
    url: str,
    destination: Path,
    *,
    max_bytes: int,
    sha256: str | None = None,
) -> dict[str, object]:
    """Download one explicitly requested source; preserve exact bytes and provenance."""

    def checked_https(value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Preparation sources and redirects must use HTTPS without credentials")
        return value

    checked_https(url)
    base = cache_root(data_dir).resolve()
    if not destination.resolve().is_relative_to(base):
        raise ValueError("Downloaded sources must remain inside the configured preparation cache")
    base.mkdir(parents=True, exist_ok=True)
    with FileLock(str(base / "preparation.lock"), timeout=0):
        if destination.exists():
            digest = file_hash(destination)
            if sha256 and digest != sha256:
                raise ValueError("Existing cached source does not match its expected checksum")
            receipt = destination.with_name(destination.name + ".receipt.json")
            if not receipt.is_file():
                raise ValueError(
                    "Existing source has no preparation receipt; inspect it before reuse"
                )
            saved: dict[str, object] = json.loads(receipt.read_text(encoding="utf-8"))
            if saved.get("url") != url or saved.get("sha256") != digest:
                raise ValueError("Cached source provenance changed")
            return saved
        check_capacity(data_dir, max_bytes)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.part")
        count = 0
        digest_state = hashlib.sha256()
        try:
            with httpx.Client(
                trust_env=False, follow_redirects=False, timeout=httpx.Timeout(60, connect=20)
            ) as client:
                current_url = url
                for _ in range(10):
                    with client.stream("GET", current_url) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            location = response.headers.get("location")
                            if not location:
                                raise ValueError("Preparation source redirect has no location")
                            current_url = checked_https(urljoin(str(response.url), location))
                            continue
                        response.raise_for_status()
                        length = response.headers.get("content-length")
                        if length is not None and int(length) > max_bytes:
                            raise ValueError("Source exceeds its explicit download byte limit")
                        with temporary.open("xb") as output:
                            for block in response.iter_bytes(1024 * 1024):
                                count += len(block)
                                if count > max_bytes:
                                    raise ValueError(
                                        "Source exceeded its explicit download byte limit"
                                    )
                                digest_state.update(block)
                                output.write(block)
                        break
                else:
                    raise ValueError("Preparation source redirected too many times")
            digest = digest_state.hexdigest()
            if sha256 and digest != sha256:
                raise ValueError("Downloaded source failed checksum verification")
            temporary.replace(destination)
            note_file(destination)
            receipt_value: dict[str, object] = {
                "schema_version": 1,
                "url": url,
                "sha256": digest,
                "bytes": count,
            }
            _atomic_json(destination.with_name(destination.name + ".receipt.json"), receipt_value)
            return receipt_value
        finally:
            temporary.unlink(missing_ok=True)


@contextmanager
def _extraction_directory(destination: Path, base: Path) -> Iterator[Path]:
    stage = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.partial")
    stage.mkdir(parents=True)
    try:
        yield stage
        stage.rename(destination)
    finally:
        # Only remove this fresh, verified staging directory inside the cache.
        if stage.exists() and stage.resolve().is_relative_to(base) and stage != base:
            shutil.rmtree(stage)


def extract_archive(
    data_dir: Path, archive: Path, destination: Path, *, max_bytes: int, max_files: int = 100000
) -> None:
    from benchmaxxing.providers.locking import preparation_scope

    with preparation_scope(data_dir):
        _extract_archive(data_dir, archive, destination, max_bytes=max_bytes, max_files=max_files)


def _extract_archive(
    data_dir: Path, archive: Path, destination: Path, *, max_bytes: int, max_files: int
) -> None:
    """Extract regular data only, rejecting links, traversal, devices and archive bombs."""
    base = cache_root(data_dir).resolve()
    if not destination.resolve().is_relative_to(base) or destination.exists():
        raise ValueError("Choose a new extraction directory inside the preparation cache")
    check_capacity(data_dir, max_bytes)
    with _extraction_directory(destination, base) as stage:
        _extract_data(archive, stage, max_bytes=max_bytes, max_files=max_files)
    note_tree(destination)


def _extract_data(archive: Path, destination: Path, *, max_bytes: int, max_files: int) -> None:
    count = 0
    total = 0

    def target(name: str, size: int) -> Path:
        nonlocal count, total
        relative_path(name.rstrip("/"))
        count += 1
        total += size
        if count > max_files or total > max_bytes or size < 0:
            raise ValueError("Expanded archive exceeds its preparation limits")
        path = destination / name
        if not path.resolve().is_relative_to(destination.resolve()):
            raise ValueError("Archive path leaves the extraction directory")
        return path

    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as zipped:
            for member in zipped.infolist():
                mode = member.external_attr >> 16
                if mode & 0o170000 not in (0, 0o100000, 0o040000):
                    raise ValueError("Archive contains a symlink or special file")
                path = target(member.filename, member.file_size)
                if member.is_dir():
                    path.mkdir(parents=True, exist_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with zipped.open(member) as source, path.open("xb") as output:
                        shutil.copyfileobj(source, output, 1024 * 1024)
    else:
        with tarfile.open(archive, "r:*") as tar:
            for entry in tar:
                if not entry.isfile() and not entry.isdir():
                    raise ValueError("Archive contains a link or special file")
                path = target(entry.name, entry.size)
                if entry.isdir():
                    path.mkdir(parents=True, exist_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    tar_source = tar.extractfile(entry)
                    assert tar_source is not None
                    with tar_source, path.open("xb") as output:
                        shutil.copyfileobj(tar_source, output, 1024 * 1024)
