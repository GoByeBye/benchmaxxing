"""Explicit benchmark-provider preparation commands."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Annotated, Any, Literal

import httpx
import typer
from filelock import FileLock, Timeout

from benchmaxxing.providers.contracts import ImportSettings

app = typer.Typer(no_args_is_help=True, help="Import and prepare upstream benchmark suites.")
DataOption = Annotated[Path | None, typer.Option("--data-dir")]


def _directory(value: Path | None) -> Path:
    from benchmaxxing.cli import resolve_data_dir

    return resolve_data_dir(value)


def _print(value: Any) -> None:
    from benchmaxxing.cli import _print as print_json

    print_json(value)


def _operation(
    directory: Path,
    name: Literal["fetch", "import", "prepare", "swe-plan", "swe-prepare"],
    payload: dict[str, Any],
) -> None:
    from benchmaxxing.cli import _remote
    from benchmaxxing.providers.registry import convert, imported_tasks, install
    from benchmaxxing.providers.runtime import prepare_runtime
    from benchmaxxing.providers.sources import fetch_suite

    remote = _remote(directory)
    lease = None
    try:
        if remote is None:
            directory.mkdir(parents=True, exist_ok=True)
            lease = FileLock(str(directory / "controller.lock"), timeout=0)
            try:
                lease.acquire()
            except Timeout:
                raise ValueError(
                    "A controller owns this directory but could not be reached. "
                    "Restore its connection before starting preparation."
                ) from None
        if remote:
            client, headers = remote
            response = client.post(f"/api/v1/providers/{name}", json=payload, headers=headers)
            response.raise_for_status()
            job = response.json()
            while job["status"] in {"queued", "running"}:
                time.sleep(1)
                response = client.get(f"/api/v1/providers/jobs/{job['id']}")
                response.raise_for_status()
                job = response.json()
            if job["status"] != "completed":
                raise ValueError(job.get("error") or f"Preparation {job['status']}")
            result = job["result"]
        elif name == "fetch":
            result = fetch_suite(directory, payload["suite_id"])
        elif name == "import":
            source = Path(payload["source_path"]).resolve()
            tasks = convert(
                payload["suite_id"],
                source,
                revision=payload["revision"],
                variant=payload.get("variant"),
            )
            result = install(directory, source, tasks)
        elif name in {"swe-plan", "swe-prepare"}:
            from benchmaxxing.providers.coding_sources import (
                prepare_swebench_instance,
                swebench_preparation_plan,
            )
            from benchmaxxing.providers.locking import preparation_scope

            with preparation_scope(directory):
                result = (
                    swebench_preparation_plan(directory, payload["task_id"])
                    if name == "swe-plan"
                    else prepare_swebench_instance(
                        directory,
                        payload["task_id"],
                        reservation_bytes=payload["reservation_bytes"],
                        pull=payload["pull"],
                    )
                )
        else:
            tasks = imported_tasks(directory, payload["suite_id"])
            if not tasks:
                raise ValueError("Fetch or import the suite before preparing its runtime")
            result = prepare_runtime(
                directory, payload["suite_id"], image=payload["image"], pull=payload["pull"]
            )
        _print(result)
        if result.get("errors"):
            raise typer.Exit(2)
    except typer.Exit:
        raise
    except (ValueError, OSError, RuntimeError, httpx.HTTPError) as exc:
        typer.echo(f"Provider preparation failed: {exc}", err=True)
        raise typer.Exit(2) from None
    finally:
        if remote:
            remote[0].close()
        if lease is not None:
            lease.release()


@app.command("list")
def list_suites(
    data_dir: DataOption = None,
    include_archived: Annotated[bool, typer.Option(help="Include archived providers")] = False,
) -> None:
    """List published, imported and prepared coverage without network access."""
    from benchmaxxing.providers.registry import suite_statuses

    _print(
        [
            value.model_dump(mode="json")
            for value in suite_statuses(_directory(data_dir), include_archived=include_archived)
        ]
    )


@app.command()
def configure(
    data_dir: DataOption = None,
    cache_dir: Annotated[Path | None, typer.Option()] = None,
    limit_gb: Annotated[int, typer.Option(min=1, max=250)] = 250,
    reserve_gb: Annotated[int, typer.Option(min=0)] = 10,
) -> None:
    """Set the shared preparation cache and total byte budget (decimal GB)."""
    from benchmaxxing.cli import _remote
    from benchmaxxing.providers.registry import save_settings, settings

    directory = _directory(data_dir)
    old = settings(directory)
    value = ImportSettings(
        storage_limit_bytes=limit_gb * 1_000_000_000,
        reserve_free_bytes=reserve_gb * 1_000_000_000,
        cache_dir=str(cache_dir.resolve()) if cache_dir else old.cache_dir,
    )
    remote = _remote(directory)
    try:
        if remote:
            response = remote[0].put(
                "/api/v1/providers/settings", json=value.model_dump(), headers=remote[1]
            )
            response.raise_for_status()
        else:
            save_settings(directory, value)
    finally:
        if remote:
            remote[0].close()
    _print(value.model_dump())


@app.command()
def fetch(suite_id: str, data_dir: DataOption = None) -> None:
    """Explicitly download a full pinned source and install its task records."""
    _operation(_directory(data_dir), "fetch", {"suite_id": suite_id})


@app.command("import")
def import_source(
    suite_id: str,
    source: Path,
    revision: Annotated[str, typer.Option()],
    variant: Annotated[str | None, typer.Option()] = None,
    data_dir: DataOption = None,
) -> None:
    """Import an existing local source; never execute source files on the host."""
    _operation(
        _directory(data_dir),
        "import",
        {
            "suite_id": suite_id,
            "source_path": str(source.resolve()),
            "revision": revision,
            "variant": variant,
        },
    )


@app.command()
def prepare(
    suite_id: str,
    image: Annotated[
        str | None, typer.Option(help="Optional local image; omit to build a reviewed runtime")
    ] = None,
    data_dir: DataOption = None,
    pull: Annotated[bool, typer.Option("--pull/--no-pull")] = False,
) -> None:
    """Verify the installed suite's assets and runtime, then record immutable image pins."""
    _operation(
        _directory(data_dir), "prepare", {"suite_id": suite_id, "image": image, "pull": pull}
    )


@app.command("swe-plan")
def swe_plan(task_id: str, data_dir: DataOption = None) -> None:
    """Write a reviewable per-instance SWE offline preparation plan; never pull images."""
    _operation(_directory(data_dir), "swe-plan", {"task_id": task_id})


@app.command("swe-prepare")
def swe_prepare(
    task_id: str,
    reserve_gb: Annotated[int, typer.Option(min=1, max=250)],
    data_dir: DataOption = None,
    pull: Annotated[bool, typer.Option("--pull/--no-pull")] = False,
) -> None:
    """Explicitly build an inspected SWE instance with a declared storage reservation."""
    _operation(
        _directory(data_dir),
        "swe-prepare",
        {"task_id": task_id, "reservation_bytes": reserve_gb * 1_000_000_000, "pull": pull},
    )
