"""Preparation-only: turn downloaded wheels into a complete hash-locked input."""

from __future__ import annotations

import email
import hashlib
import sys
import zipfile
from pathlib import Path


def lock_wheels(directory: Path) -> str:
    lines = []
    names = set()
    files = sorted(directory.iterdir())
    if not files or any(path.suffix != ".whl" for path in files):
        raise ValueError("Preparation requires a nonempty directory containing only wheels")
    for path in files:
        with zipfile.ZipFile(path) as archive:
            metadata_paths = [
                name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
            ]
            if len(metadata_paths) != 1:
                raise ValueError("Ambiguous wheel metadata")
            metadata = email.message_from_bytes(archive.read(metadata_paths[0]))
        name, version = str(metadata["Name"]), str(metadata["Version"])
        if name.lower() in names or any(char in name + version for char in "\r\n \t"):
            raise ValueError("Invalid or duplicate wheel identity")
        names.add(name.lower())
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        lines.append(f"{name}=={version} --hash=sha256:{digest}")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    print(lock_wheels(Path(sys.argv[1])), end="")
