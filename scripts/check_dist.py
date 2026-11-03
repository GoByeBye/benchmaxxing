"""Fail release checks if distributable archives contain runtime or private state."""

import argparse
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

FORBIDDEN = {
    ".benchmaxxing",
    ".venv",
    "node_modules",
    "work",
    "test-results",
    "playwright-report",
    "__pycache__",
    ".git",
}


def check_names(names: set[str]) -> None:
    for name in names:
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or FORBIDDEN.intersection(path.parts):
            raise ValueError(f"Unexpected archive member: {name}")
        if path.name.startswith(".env") or path.suffix in {".sqlite3", ".gguf", ".safetensors"}:
            raise ValueError(f"Private/runtime artifact in archive: {name}")
        if path.name == "controller.json":
            raise ValueError("Controller descriptor must never be distributed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, nargs="?", default=Path("dist"))
    args = parser.parse_args()
    wheels = list(args.directory.glob("*.whl"))
    sources = list(args.directory.glob("*.tar.gz"))
    if not wheels or not sources:
        raise ValueError("Build both a wheel and source distribution before checking")
    for wheel in wheels:
        with zipfile.ZipFile(wheel) as archive:
            names = set(archive.namelist())
            check_names(names)
            for required in ("benchmaxxing/static/index.html", "benchmaxxing/reference/TASK.md"):
                if required not in names:
                    raise ValueError(f"Wheel missing {required}")
            print(f"{wheel.name}: {len(names)} verified members")
    for source in sources:
        with tarfile.open(source, "r:gz") as archive:
            members = archive.getmembers()
            names = {member.name for member in members}
            check_names(names)
            if any(member.issym() or member.islnk() for member in members):
                raise ValueError("Source distribution contains links")
            relative = {
                str(PurePosixPath(name).relative_to(PurePosixPath(name).parts[0])) for name in names
            }
            for required in (
                "pyproject.toml",
                "uv.lock",
                "web/package-lock.json",
                "LICENSE",
                "src/benchmaxxing/static/index.html",
                "src/benchmaxxing/reference/TASK.md",
            ):
                if required not in relative:
                    raise ValueError(f"Source distribution missing {required}")
            print(f"{source.name}: {len(names)} verified members")


if __name__ == "__main__":
    main()
