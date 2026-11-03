"""CLI using the same controller, records and execution service as the UI."""

from __future__ import annotations

import json
import os
import time
import tomllib
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

import httpx
import typer
import uvicorn
from pydantic import ValidationError

from benchmaxxing.contracts import TERMINAL_STATUSES, InferenceProfile, RunRecord, RunSpec
from benchmaxxing.controller import Controller, ControllerBusy, RecoveryPending, read_descriptor
from benchmaxxing.storage import Store, workspace_id

app = typer.Typer(
    no_args_is_help=True, help="Local, reproducible benchmarks for models and agents."
)
DataOption = Annotated[
    Path | None, typer.Option("--data-dir", help="Shared run and asset directory")
]


def resolve_data_dir(value: Path | None) -> Path:
    return (value or Path(os.environ.get("BENCHMAXXING_DATA_DIR", ".benchmaxxing"))).resolve()


def _print(value: Any) -> None:
    typer.echo(json.dumps(value, indent=2, ensure_ascii=False))


def _remote(data_dir: Path) -> tuple[httpx.Client, dict[str, str]] | None:
    descriptor = read_descriptor(data_dir)
    if not descriptor or not descriptor.get("base_url"):
        return None
    url = descriptor["base_url"]
    parsed = urlsplit(url)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        return None
    client = httpx.Client(base_url=url, trust_env=False, follow_redirects=False, timeout=5)
    try:
        health = client.get("/api/v1/health")
        health.raise_for_status()
        if health.json().get("workspace_id") != workspace_id(data_dir):
            client.close()
            return None
        return client, {"X-Benchmaxxing-Token": descriptor["token"]}
    except (httpx.HTTPError, ValueError, KeyError):
        client.close()
        return None


@app.command()
def doctor(data_dir: DataOption = None) -> None:
    """Check the Docker engine and already prepared assets; never download."""
    from benchmaxxing.environment import doctor as inspect_environment

    report = inspect_environment(resolve_data_dir(data_dir))
    _print(report)
    if not report.get("ready"):
        raise typer.Exit(2)


@app.command()
def prepare(
    data_dir: DataOption = None,
    image: Annotated[
        str, typer.Option(help="Explicit source image to pull and pin")
    ] = "python:3.13-slim",
) -> None:
    """Explicitly download and pin the reference environment before running offline."""
    from filelock import FileLock, Timeout

    from benchmaxxing.environment import prepare as prepare_environment

    directory = resolve_data_dir(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    try:
        with FileLock(str(directory / "controller.lock"), timeout=0):
            _print(prepare_environment(directory, image=image))
    except Timeout:
        typer.echo("Stop the active controller before preparing this data directory.", err=True)
        raise typer.Exit(2) from None
    except Exception as exc:
        typer.echo(f"Preparation failed: {exc}", err=True)
        raise typer.Exit(2) from None


def _load_spec(
    config: Path | None,
    participant: str | None,
    variant: str | None,
    endpoint: str | None,
    model: str | None,
    api: bool,
    key_env: str | None,
) -> RunSpec:
    values: dict[str, Any] = {}
    if config is not None:
        raw = config.read_bytes()
        values = tomllib.loads(raw.decode()) if config.suffix == ".toml" else json.loads(raw)
    if participant is not None:
        values["participant"] = participant
    if variant is not None:
        values["scripted_variant"] = variant
    if endpoint is not None or model is not None:
        if endpoint is None or model is None:
            raise ValueError("--endpoint and --model must be supplied together")
        values["participant"] = "openai"
        values["inference"] = InferenceProfile(
            base_url=endpoint,
            model=model,
            mode="api" if api else "offline",
            api_key_env=key_env,
        ).model_dump()
    elif api or key_env:
        raise ValueError("--api and --key-env require --endpoint and --model")
    return RunSpec.model_validate(values)


@app.command()
def run(
    data_dir: DataOption = None,
    config: Annotated[Path | None, typer.Option(help="Run specification: TOML or JSON")] = None,
    participant: Annotated[str | None, typer.Option(help="scripted or openai")] = None,
    variant: Annotated[str | None, typer.Option(help="Scripted: good, bad, or refuse")] = None,
    endpoint: Annotated[str | None, typer.Option(help="OpenAI-compatible base URL")] = None,
    model: Annotated[str | None, typer.Option(help="Explicit model ID")] = None,
    api: Annotated[bool, typer.Option(help="Explicitly allow a remote model endpoint")] = False,
    key_env: Annotated[
        str | None, typer.Option(help="Environment variable holding the API key")
    ] = None,
) -> None:
    """Run a task; attach to the local service or use a headless controller."""
    try:
        spec = _load_spec(config, participant, variant, endpoint, model, api, key_env)
    except (ValueError, ValidationError, OSError):
        typer.echo(
            "Invalid run configuration. Check fields, profile, and endpoint settings.", err=True
        )
        raise typer.Exit(2) from None
    directory = resolve_data_dir(data_dir)
    remote = _remote(directory)
    record: RunRecord
    if remote is not None:
        client, headers = remote
        try:
            response = client.post(
                "/api/v1/runs", json=spec.model_dump(mode="json"), headers=headers
            )
            if response.status_code != 201:
                typer.echo(f"Controller rejected run (HTTP {response.status_code}).", err=True)
                raise typer.Exit(2)
            record = RunRecord.model_validate(response.json())
            try:
                while record.status not in TERMINAL_STATUSES:
                    time.sleep(0.15)
                    detail = client.get(f"/api/v1/runs/{record.id}")
                    detail.raise_for_status()
                    record = RunRecord.model_validate(detail.json()["run"])
            except KeyboardInterrupt:
                client.post(f"/api/v1/runs/{record.id}/cancel", headers=headers)
                typer.echo(f"Cancellation requested for {record.id}", err=True)
                raise typer.Exit(130) from None
        except httpx.HTTPError:
            typer.echo(
                "Controller connection failed; inspect the recorded run in the UI.", err=True
            )
            raise typer.Exit(2) from None
        finally:
            client.close()
    else:
        owner = Controller(directory)
        try:
            owner.start()
        except ControllerBusy:
            typer.echo("A controller owns this directory but its API is unavailable.", err=True)
            raise typer.Exit(2) from None
        try:
            record = owner.submit(spec)
            while record.status not in TERMINAL_STATUSES:
                time.sleep(0.1)
                record = owner.store.get_run(record.id)
        except KeyboardInterrupt:
            owner.stop()
            raise typer.Exit(130) from None
        except RecoveryPending as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(2) from None
        finally:
            owner.stop()
    _print(record.model_dump(mode="json"))
    raise typer.Exit(0 if record.status == "passed" else 1 if record.status == "failed" else 2)


@app.command()
def serve(
    data_dir: DataOption = None,
    port: Annotated[int, typer.Option(min=1024, max=65535)] = 8123,
) -> None:
    """Serve the local API and built UI on 127.0.0.1."""
    from benchmaxxing.api import create_app

    uvicorn.run(create_app(resolve_data_dir(data_dir), port=port), host="127.0.0.1", port=port)


@app.command()
def export(
    run_id: str,
    output: Annotated[Path, typer.Option("--output", "-o", help="Destination ZIP file")],
    data_dir: DataOption = None,
) -> None:
    """Export a run, events, artifacts, and checksum manifest without credentials."""
    try:
        content = Store(resolve_data_dir(data_dir)).export_run(run_id)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("xb") as stream:
            stream.write(content)
    except (KeyError, ValueError, OSError) as exc:
        typer.echo(f"Export failed: {exc}", err=True)
        raise typer.Exit(2) from None
    typer.echo(str(output.resolve()))


if __name__ == "__main__":
    app()
