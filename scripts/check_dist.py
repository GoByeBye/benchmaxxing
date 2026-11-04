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
UNSHIPPED_LABS = {
    "providers/cyberlabs.py",
    "providers/cyberlab_environment.py",
    "runtimes/cyberlab_server.py",
    "tests/test_cyberlabs.py",
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
        if any(name.endswith("/" + partial) for partial in UNSHIPPED_LABS):
            raise ValueError(f"Unfinished lab implementation in archive: {name}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, nargs="?", default=Path("dist"))
    args = parser.parse_args()
    package = Path(__file__).resolve().parents[1] / "src" / "benchmaxxing"
    expected_packs = {
        "benchmaxxing/" + p.relative_to(package).as_posix()
        for p in (package / "packs").rglob("*.json")
    }
    expected_answers = {
        "benchmaxxing/" + p.relative_to(package).as_posix()
        for p in (package / "answer_packs").rglob("*.json")
    } | {"benchmaxxing/answers.py", "benchmaxxing/answer_runner.py"}
    expected_providers = {"benchmaxxing/provider_cli.py"}
    for directory in (package / "providers", package / "runtimes"):
        expected_providers.update(
            "benchmaxxing/" + path.relative_to(package).as_posix()
            for path in directory.rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix not in {".pyc", ".pyo"}
            and not any(
                path.relative_to(package).as_posix().endswith(partial) for partial in UNSHIPPED_LABS
            )
        )
    expected_static = {
        "benchmaxxing/" + path.relative_to(package).as_posix()
        for path in (package / "static").rglob("*")
        if path.is_file()
    }
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
            packs = {
                name
                for name in names
                if name.startswith("benchmaxxing/packs/") and name.endswith(".json")
            }
            if packs != expected_packs:
                raise ValueError(
                    f"Wheel pack mismatch: missing={expected_packs - packs}, "
                    f"unexpected={packs - expected_packs}"
                )
            for expected in (expected_providers, expected_static, expected_answers):
                missing = expected - names
                if missing:
                    raise ValueError(f"Wheel missing provider/runtime/UI resources: {missing}")
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
                "docs/providers.md",
                "docs/provider-validation.md",
                "docs/knowledge-retrieval.md",
                "docs/frontend-benchmarks.md",
            ):
                if required not in relative:
                    raise ValueError(f"Source distribution missing {required}")
            if not {"src/" + name for name in expected_packs}.issubset(relative):
                raise ValueError("Source distribution missing installed pack definitions")
            for expected in (expected_providers, expected_static, expected_answers):
                missing = {"src/" + name for name in expected} - relative
                if missing:
                    raise ValueError(
                        f"Source distribution missing provider/runtime/UI resources: {missing}"
                    )
            print(f"{source.name}: {len(names)} verified members")


if __name__ == "__main__":
    main()
