"""Preparation ownership and cache changes cannot invalidate installed runs."""

import threading

import pytest
from filelock import FileLock
from pydantic import ValidationError
from typer.testing import CliRunner

from benchmaxxing import cli
from benchmaxxing.providers import registry, sources
from benchmaxxing.providers.contracts import ImportedTask, ImportSettings, NativeRecipe
from benchmaxxing.providers.locking import preparation_scope


def test_api_contract_enforces_the_authorized_250_gb_cap():
    assert (
        ImportSettings(storage_limit_bytes=250_000_000_000).storage_limit_bytes == 250_000_000_000
    )
    with pytest.raises(ValidationError):
        ImportSettings(storage_limit_bytes=250_000_000_001)


def test_cache_cannot_be_repointed_after_install(tmp_path):
    directory = tmp_path / "data"
    original = ImportSettings(cache_dir=str(tmp_path / "cache"))
    registry.save_settings(directory, original)
    task = ImportedTask(
        suite_id="xstest",
        upstream_id="fake/0",
        revision="fake",
        source_sha256="a" * 64,
        title="Fake task",
        prompt="Fake prompt",
        category="refusal",
        recipe=NativeRecipe(family="fake"),
    )
    registry.install(directory, tmp_path, [task])
    with pytest.raises(ValueError, match="explicit offline migration"):
        registry.save_settings(directory, ImportSettings(cache_dir=str(tmp_path / "other")))
    assert registry.settings(directory) == original
    assert registry.get_imported(directory, task.id) == task


def test_cache_preparation_scope_is_nested_but_excludes_another_worker(tmp_path):
    errors = []

    def contender():
        try:
            with preparation_scope(tmp_path):
                errors.append("unexpected owner")
        except ValueError as exc:
            errors.append(str(exc))

    with preparation_scope(tmp_path), preparation_scope(tmp_path):
        thread = threading.Thread(target=contender)
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive()
    assert len(errors) == 1 and "Another preparation operation" in errors[0]
    with preparation_scope(tmp_path):
        pass


def test_cli_does_not_prepare_when_service_discovery_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "_remote", lambda _: None)

    def forbidden(*args):
        raise AssertionError("Preparation must not bypass the existing controller")

    monkeypatch.setattr(sources, "fetch_suite", forbidden)
    with FileLock(str(tmp_path / "controller.lock")):
        result = CliRunner().invoke(
            cli.app, ["providers", "fetch", "miniwob", "--data-dir", str(tmp_path)]
        )
    assert result.exit_code == 2
    assert "controller owns this directory" in result.output
