"""Original, offline frontend implementation briefs and explicit preparation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from benchmaxxing.environment import _docker, _inspect_image, _require_success
from benchmaxxing.providers import downloads, registry
from benchmaxxing.providers.contracts import ImportedTask, NativeRecipe

VERSION = "1.0.0"
SUITE_ID = "frontend-design"
BROWSER_IMAGE = "benchmaxxing/miniwob-frozen:0.14.3"


def fetch_frontend_source(data_dir: Path) -> dict[str, Any]:
    """Install bundled original data explicitly, requiring no internet."""
    with downloads.capacity_batch(data_dir):
        return _fetch_frontend_source(data_dir)


def _fetch_frontend_source(data_dir: Path) -> dict[str, Any]:
    source = downloads.cache_root(data_dir) / "sources" / SUITE_ID / VERSION
    grader = Path(__file__).parents[1] / "runtimes" / "frontend_grader.py"
    records = json.loads((grader.parent / "frontend_briefs.json").read_text(encoding="utf-8"))[
        "briefs"
    ]
    source.mkdir(parents=True, exist_ok=True)
    registry._atomic_json(source / "briefs.json", {"version": VERSION, "briefs": records})
    downloads.check_capacity(data_dir, grader.stat().st_size)
    (source / "frontend_grader.py").write_bytes(grader.read_bytes())
    downloads.note_file(source / "frontend_grader.py")
    return {
        "source_path": str(source),
        "revision": VERSION,
        "source_sha256": registry.source_hash(source),
    }


def convert_frontend(
    source: Path, *, revision: str, source_sha256: str, variant: str | None = None
) -> list[ImportedTask]:
    value = json.loads((source / "briefs.json").read_text(encoding="utf-8"))
    if value.get("version") != VERSION or revision != VERSION:
        raise ValueError("Unsupported frontend brief version")
    grader = (source / "frontend_grader.py").read_text(encoding="utf-8")
    return [
        ImportedTask(
            suite_id=SUITE_ID,
            upstream_id="benchmaxxing/" + brief["id"],
            revision=VERSION,
            source_sha256=source_sha256,
            title=brief["title"],
            category="frontend",
            difficulty="medium",
            variant=variant or "benchmaxxing-original-ui-30m-v1",
            submission_file="index.html",
            submission_kind="text",
            prompt=(
                f"# {brief['title']}\n\n{brief['requirements']}\n\n"
                "Implement a complete interface in /workspace/index.html with embedded CSS and "
                "JavaScript. No external fonts, packages, assets or services. Use semantic HTML, "
                "meaningful layout, clear visual hierarchy and visible keyboard focus. "
                "An independent browser verifies the written requirements at desktop and mobile "
                "sizes. Visual polish is reviewed separately from objective functional checks. "
                "Save index.html and call submit.\n"
            ),
            public_files={
                "index.html": (
                    '<!doctype html><html lang="en"><head><meta charset="utf-8">'
                    "<title>Frontend task</title></head><body>"
                    "<h1>Build your interface</h1></body></html>"
                ),
                "README.txt": brief["requirements"],
            },
            private_files={
                "frontend_grader.py": grader,
                "frontend-case.json": json.dumps({"scenario": brief["id"]}),
            },
            recipe=NativeRecipe(
                family="frontend-design",
                image=BROWSER_IMAGE,
                tools="workspace",
                network="none",
                grader_command=["python", "/grader/frontend_grader.py"],
                grader_result_path="/workspace/frontend-result.json",
                settings={
                    "probe_modules": ["playwright.sync_api"],
                    "writable_layer_limit_bytes": 268435456,
                    "record_grading_artifacts": True,
                },
            ),
            licenses=["AGPL-3.0-only"],
            source_urls=["https://github.com/GoByeBye/benchmaxxing"],
            tags=[
                "original",
                "frontend",
                "accessibility",
                "responsive",
                "functional-grading",
                "visual-review",
            ],
            comparison_notes=[
                "Original Benchmaxxing brief, not an imported leaderboard task.",
                "Objective browser checks are separate from unscored human aesthetic review.",
            ],
            good_solution=brief["good_solution"],
            bad_solution=brief["bad_solution"],
        )
        for brief in value["briefs"]
    ]


def prepare_frontend_runtime(data_dir: Path) -> dict[str, Any]:
    from benchmaxxing.environment import EnvironmentError
    from benchmaxxing.providers.interactive_preparation import prepare_miniwob_runtime
    from benchmaxxing.providers.preparation import prepare_suite

    try:
        info = _inspect_image(BROWSER_IMAGE)
    except EnvironmentError:
        prepare_miniwob_runtime(data_dir)
        info = _inspect_image(BROWSER_IMAGE)
    probe = (
        "from playwright.sync_api import sync_playwright; p=sync_playwright().start(); "
        "b=p.chromium.launch(headless=True,args=['--no-sandbox']); page=b.new_page(); "
        "page.set_content('<h1>Offline frontend runtime</h1>'); "
        "assert page.locator('h1').inner_text()=='Offline frontend runtime'; b.close(); p.stop()"
    )
    _require_success(
        _docker(
            [
                "run",
                "--rm",
                "--pull",
                "never",
                "--network",
                "none",
                "--user",
                "65534:65534",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--memory",
                "2048m",
                "--pids-limit",
                "128",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,size=268435456,mode=1777",
                "--entrypoint",
                "python",
                info["Id"],
                "-c",
                probe,
            ],
            timeout=90,
        ),
        "Prepared browser runtime failed its offline Chromium probe",
    )
    return prepare_suite(data_dir, registry.imported_tasks(data_dir, SUITE_ID), image=info["Id"])
