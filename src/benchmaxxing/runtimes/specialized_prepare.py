"""Explicit, byte-bounded preparation of small specialized upstream source files.

This is intentionally separate from all converters/runners. It never installs or
executes downloaded code. Global preparation accounting remains the controller's
responsibility; this helper enforces a conservative per-download workspace cap.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any

REPOSITORIES = {
    "xstest": "paul-rottger/xstest",
    "harmbench": "centerforaisafety/HarmBench",
    "intercode-ctf": "princeton-nlp/intercode",
}
DEFAULT_CAP = 100_000_000


def selected_path(suite: str, path: str) -> bool:
    """Select source/annotations, never model weights or generated completions."""
    basename = PurePosixPath(path).name.lower()
    if "/" not in path and basename in {"readme.md", "license", "license.md", "license.txt"}:
        return True
    if suite == "xstest":
        return path == "xstest_prompts.csv" or (
            path.startswith("evaluation/") and path.endswith(".py")
        )
    if suite == "intercode-ctf":
        return path.startswith("data/ctf/") or path in {
            "docker/ctf.Dockerfile",
            "docker/docker.gitignore",
            "run_demo.py",
            "intercode/envs/ctf/ctf_env.py",
        }
    return (
        path.startswith("data/behavior_datasets/")
        or path.startswith("data/multimodal_behavior_datasets/")
        or path.startswith("data/copyright_classifier_hashes/")
        or path.startswith("data/multimodal_behavior_images/")
        or path.startswith("data/classifier_prompts/")
        or path in {"evaluate_completions.py", "eval_utils.py", "requirements.txt"}
        or path.startswith("docs/")
    )


def checked_destination(root: Path, name: str) -> Path:
    pure = PurePosixPath(name)
    if not name or pure.is_absolute() or ".." in pure.parts or str(pure) != name:
        raise ValueError("Invalid upstream path")
    if "\\" in name or ":" in name or "\0" in name:
        raise ValueError("Invalid upstream path")
    target = root / name
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError("Upstream path escapes destination")
    return target


def _request(url: str) -> Any:
    # Preparation is the only networked path. No credential or ambient proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, headers={"User-Agent": "Benchmaxxing-prepare/1"})
    return opener.open(request, timeout=60)


def _get_json(url: str) -> Any:
    with _request(url) as response:
        data = response.read(8_000_001)
    if len(data) > 8_000_000:
        raise ValueError("Source metadata exceeds eight megabytes")
    return json.loads(data)


def check_budget(current_bytes: int, announced_bytes: int, cap_bytes: int) -> None:
    if min(current_bytes, announced_bytes, cap_bytes) < 0:
        raise ValueError("Negative preparation accounting value")
    if current_bytes + announced_bytes > cap_bytes:
        raise ValueError("Source preparation exceeds its reserved byte budget")


def prepare_sources(
    suite: str,
    destination: Path,
    cap_bytes: int = DEFAULT_CAP,
    revision: str | None = None,
    resume: bool = False,
) -> dict[str, Any]:
    if suite not in REPOSITORIES:
        raise ValueError("Unsupported specialized source downloader")
    if destination.exists() and any(destination.iterdir()) and not resume:
        raise ValueError("Use an empty destination; prepared snapshots are immutable")
    if resume and (not revision or (destination / "benchmaxxing-source.json").exists()):
        raise ValueError("Resume requires an explicit revision and an incomplete snapshot")
    repo = REPOSITORIES[suite]
    commit = revision or _get_json(f"https://api.github.com/repos/{repo}/commits/HEAD")["sha"]
    if not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("Upstream did not return an immutable Git commit")
    tree = _get_json(f"https://api.github.com/repos/{repo}/git/trees/{commit}?recursive=1")
    if tree.get("truncated"):
        raise ValueError("Upstream source tree was truncated")
    entries = [
        entry
        for entry in tree["tree"]
        if entry["type"] == "blob" and selected_path(suite, entry["path"])
    ]
    if not entries:
        raise ValueError("No supported source files in pinned upstream revision")
    announced = sum(entry["size"] for entry in entries)
    check_budget(0, announced, cap_bytes)
    destination.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "suite_id": suite,
        "repository": f"https://github.com/{repo}",
        "revision": commit,
        "files": [],
        "downloaded_bytes": 0,
        "scope": "source-and-annotations; no model weights or runtime dependency installation",
    }
    for entry in sorted(entries, key=lambda value: value["path"]):
        if entry.get("mode") == "120000":
            raise ValueError("Source download does not follow repository symlinks")
        target = checked_destination(destination, entry["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        encoded_path = urllib.parse.quote(entry["path"], safe="/")
        url = f"https://raw.githubusercontent.com/{repo}/{commit}/{encoded_path}"
        digest = hashlib.sha256()
        size = 0
        if resume and target.is_file() and not target.is_symlink():
            contents = target.read_bytes()
            git_hash = hashlib.sha1(
                f"blob {len(contents)}\0".encode() + contents, usedforsecurity=False
            ).hexdigest()
            if git_hash == entry["sha"] and not contents.startswith(
                b"version https://git-lfs.github.com/spec/v1"
            ):
                check_budget(manifest["downloaded_bytes"], len(contents), cap_bytes)
                manifest["downloaded_bytes"] += len(contents)
                manifest["files"].append(
                    {
                        "path": entry["path"],
                        "url": url,
                        "size": len(contents),
                        "sha256": hashlib.sha256(contents).hexdigest(),
                        "git_blob": entry["sha"],
                    }
                )
                continue
        if target.is_symlink():
            raise ValueError("Destination must not contain symlinks")
        with _request(url) as response, target.open("wb" if resume else "xb") as stream:
            while chunk := response.read(65536):
                check_budget(manifest["downloaded_bytes"] + size, len(chunk), cap_bytes)
                if size + len(chunk) > entry["size"]:
                    raise ValueError("Downloaded source exceeded its announced size")
                stream.write(chunk)
                digest.update(chunk)
                size += len(chunk)
        if size != entry["size"]:
            raise ValueError("Downloaded source size differs from pinned Git tree")
        if size < 1024 and target.read_bytes().startswith(
            b"version https://git-lfs.github.com/spec/v1"
        ):
            pointer = target.read_text(encoding="ascii")
            match = re.search(r"oid sha256:([a-f0-9]{64})\nsize (\d+)", pointer)
            if not match:
                raise ValueError("Malformed Git LFS pointer")
            expected_hash, expected_size = match.group(1), int(match.group(2))
            check_budget(manifest["downloaded_bytes"], expected_size, cap_bytes)
            url = f"https://media.githubusercontent.com/media/{repo}/{commit}/{encoded_path}"
            size = 0
            digest = hashlib.sha256()
            with _request(url) as response, target.open("wb") as stream:
                while chunk := response.read(65536):
                    if size + len(chunk) > expected_size:
                        raise ValueError("Git LFS download exceeded its pinned size")
                    check_budget(manifest["downloaded_bytes"] + size, len(chunk), cap_bytes)
                    stream.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
            if size != expected_size or digest.hexdigest() != expected_hash:
                raise ValueError("Git LFS content does not match its pinned object")
        manifest["downloaded_bytes"] += size
        manifest["files"].append(
            {
                "path": entry["path"],
                "url": url,
                "size": size,
                "sha256": digest.hexdigest(),
                "git_blob": entry["sha"],
            }
        )
    raw = json.dumps(manifest, sort_keys=True, indent=2)
    check_budget(manifest["downloaded_bytes"], len(raw.encode()), cap_bytes)
    (destination / "benchmaxxing-source.json").write_text(raw, encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", choices=sorted(REPOSITORIES))
    parser.add_argument("destination", type=Path)
    parser.add_argument("--cap-bytes", type=int, default=DEFAULT_CAP)
    parser.add_argument("--revision")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    manifest = prepare_sources(
        args.suite, args.destination, args.cap_bytes, args.revision, args.resume
    )
    print(
        json.dumps(
            {key: manifest[key] for key in ("suite_id", "revision", "downloaded_bytes", "scope")}
        )
    )


if __name__ == "__main__":
    main()
