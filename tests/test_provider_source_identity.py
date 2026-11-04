"""Imported source identity must be stable on Windows and Linux."""

import hashlib
from pathlib import Path

from benchmaxxing.providers.registry import source_hash


def test_mixed_case_and_prefix_paths_have_one_portable_source_hash(tmp_path: Path) -> None:
    contents = {
        "a.txt": b"lower",
        "B.txt": b"upper",
        "path-tracing/x.txt": b"directory prefix",
        "path-tracing-reverse/y.txt": b"longer directory prefix",
    }
    for relative, content in contents.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    expected = hashlib.sha256()
    # This explicit order matches the identities previously issued on Windows.
    for relative in (
        "a.txt",
        "B.txt",
        "path-tracing/x.txt",
        "path-tracing-reverse/y.txt",
    ):
        expected.update(relative.encode() + b"\0")
        expected.update(hashlib.sha256(contents[relative]).digest())
    assert source_hash(tmp_path) == expected.hexdigest()
