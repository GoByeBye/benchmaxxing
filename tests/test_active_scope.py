"""Inactive labs and removed provider data stay outside active catalogs."""

import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from benchmaxxing.api import create_app
from benchmaxxing.cli import app
from benchmaxxing.contracts import RunSpec
from benchmaxxing.controller import Controller
from benchmaxxing.providers import registry, runtime, sources
from benchmaxxing.providers.contracts import ImportedTask, NativeRecipe
from benchmaxxing.task import get_task, list_tasks
from scripts.check_dist import UNSHIPPED_LABS, check_names


def install_archived(directory, suite_id):
    task = ImportedTask(
        suite_id=suite_id,
        upstream_id="fake",
        revision="test-v1",
        source_sha256="a" * 64,
        title="Archived synthetic fixture",
        prompt="Fixture prompt",
        category="security",
        recipe=NativeRecipe(family="fake"),
    )
    registry.install(directory, directory, [task])
    return task


def test_archive_filter_preserves_direct_inspection_and_blocks_new_runs(tmp_path):
    task = install_archived(tmp_path, "cyberlabs")
    assert task.id not in {item.id for item in list_tasks(data_dir=tmp_path)}
    assert task.id in {item.id for item in list_tasks(data_dir=tmp_path, include_archived=True)}
    assert get_task(task.id, data_dir=tmp_path).upstream_id == "fake"
    controller = Controller(tmp_path, cleanup=lambda _: None)
    with pytest.raises(ValueError, match="archived"):
        controller.submit(RunSpec(task_id=task.id))
    assert not controller.store.list_runs()


def test_api_cli_agree_on_active_and_archived_views(tmp_path):
    suite_id = "cyberlabs"
    task = install_archived(tmp_path, suite_id)
    owner = Controller(tmp_path, cleanup=lambda _: None)
    with TestClient(
        create_app(tmp_path, controller=owner), base_url="http://127.0.0.1:8123"
    ) as client:
        active = client.get("/api/v1/providers").json()
        archived = client.get("/api/v1/providers?include_archived=true").json()
        assert not any(item["suite"]["id"] == suite_id for item in active)
        assert (
            next(item for item in archived if item["suite"]["id"] == suite_id)["suite"]["active"]
            is False
        )
        assert not any(item["id"] == task.id for item in client.get("/api/v1/tasks").json())
        assert any(
            item["id"] == task.id
            for item in client.get("/api/v1/tasks?include_archived=true").json()
        )
        response = client.post(
            "/api/v1/runs", json={"task_id": task.id}, headers={"x-benchmaxxing-token": owner.token}
        )
        assert response.status_code == 422 and "archived" in response.text
        for operation, payload in (
            ("fetch", {"suite_id": suite_id}),
            ("prepare", {"suite_id": suite_id}),
            ("import", {"suite_id": suite_id, "source_path": str(tmp_path), "revision": "test"}),
        ):
            response = client.post(
                f"/api/v1/providers/{operation}",
                json=payload,
                headers={"x-benchmaxxing-token": owner.token},
            )
            assert response.status_code == 422 and "archived" in response.text
        assert client.get("/api/v1/providers/jobs").json() == []
    result = CliRunner().invoke(app, ["providers", "list", "--data-dir", str(tmp_path)])
    assert result.exit_code == 0
    assert json.loads(result.output) == active
    result = CliRunner().invoke(
        app, ["providers", "list", "--include-archived", "--data-dir", str(tmp_path)]
    )
    assert result.exit_code == 0
    assert json.loads(result.output) == archived


def test_archived_lab_public_entry_points_stop_before_preparation(tmp_path):
    task = install_archived(tmp_path, "cyberlabs")
    with pytest.raises(ValueError, match="archived"):
        sources.fetch_suite(tmp_path, "cyberlabs")
    with pytest.raises(ValueError, match="archived"):
        registry.convert("cyberlabs", tmp_path, revision="test")
    with pytest.raises(ValueError, match="archived"):
        runtime.prepare_runtime(tmp_path, "cyberlabs")
    assert registry.imported_tasks(tmp_path, "cyberlabs") == [task]
    assert registry.prepared_record(tmp_path, task) is None


def test_removed_provider_records_remain_readable_by_exact_id_only(tmp_path):
    # Simulate a pre-upgrade data directory without consulting the new registry.
    legacy = ImportedTask(
        suite_id="benchcad",
        upstream_id="old-fixture",
        revision="old-v1",
        source_sha256="a" * 64,
        title="Historical fixture",
        prompt="Earlier task prompt",
        category="cad",
        recipe=NativeRecipe(family="benchcad", tools="blender"),
    )
    registry._atomic_json(
        registry.root(tmp_path) / "tasks" / f"{legacy.id}.json",
        {"task": legacy.model_dump(mode="json"), "sha256": legacy.task_hash},
    )
    registry._write_catalog(tmp_path, [legacy])
    assert get_task(legacy.id, data_dir=tmp_path).title == "Historical fixture"
    assert legacy.id not in {task.id for task in list_tasks(data_dir=tmp_path)}
    assert legacy.id not in {
        task.id for task in list_tasks(data_dir=tmp_path, include_archived=True)
    }

    owner = Controller(tmp_path, cleanup=lambda _: None)
    run = owner.store.create_run(
        RunSpec(task_id=legacy.id), metadata={"task": {"title": legacy.title, "category": "cad"}}
    )
    artifact = owner.store.add_artifact(run.id, "old-submission.txt", b"original evidence")
    owner.store.update_run(run.id, status="interrupted", event_kind="interrupted")
    with TestClient(
        create_app(tmp_path, controller=owner), base_url="http://127.0.0.1:8123"
    ) as client:
        assert not any(
            item["suite"]["id"] == legacy.suite_id
            for item in client.get("/api/v1/providers?include_archived=true").json()
        )
        assert not any(
            item["id"] == legacy.id
            for item in client.get("/api/v1/tasks?include_archived=true").json()
        )
        assert client.get(f"/api/v1/runs/{run.id}").json()["run"]["spec"]["task_id"] == legacy.id
        assert (
            client.get(f"/api/v1/runs/{run.id}/artifacts/{artifact.id}").content
            == b"original evidence"
        )
        with zipfile.ZipFile(
            io.BytesIO(client.get(f"/api/v1/runs/{run.id}/export").content)
        ) as bundle:
            assert json.loads(bundle.read("run.json"))["spec"]["task_id"] == legacy.id
            assert bundle.read(f"artifacts/{artifact.id}") == b"original evidence"
        response = client.post(
            "/api/v1/runs",
            json={"task_id": legacy.id},
            headers={"x-benchmaxxing-token": owner.token},
        )
        assert response.status_code == 422 and "Unknown" in response.text
        for operation, payload in (
            ("fetch", {"suite_id": legacy.suite_id}),
            ("prepare", {"suite_id": legacy.suite_id}),
            (
                "import",
                {"suite_id": legacy.suite_id, "source_path": str(tmp_path), "revision": "old-v1"},
            ),
        ):
            response = client.post(
                f"/api/v1/providers/{operation}",
                json=payload,
                headers={"x-benchmaxxing-token": owner.token},
            )
            assert response.status_code == 422 and "Unknown" in response.text


@pytest.mark.parametrize("partial", sorted(UNSHIPPED_LABS))
def test_release_check_rejects_unfinished_lab_members(partial):
    for archive_name in (
        f"benchmaxxing/{partial}",
        f"benchmaxxing-0.1.0/src/benchmaxxing/{partial}",
    ):
        with pytest.raises(ValueError, match="Unfinished lab implementation"):
            check_names({archive_name})
