"""Shared import lifecycle tests with explicitly fake, non-executing environments."""

import hashlib
import io
import json
import threading
import time
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient

from benchmaxxing import cli
from benchmaxxing.api import create_app
from benchmaxxing.contracts import InferenceProfile, RunSpec
from benchmaxxing.controller import Controller
from benchmaxxing.environment import EnvironmentError, ExecutionCancelled, ExecutionTimeout
from benchmaxxing.model import ModelError, ModelResponse, ToolCall
from benchmaxxing.providers import downloads, jobs, preparation, registry, runner, sandbox
from benchmaxxing.providers.contracts import ImportedTask, ImportSettings, NativeRecipe, SourceAsset
from benchmaxxing.storage import Store


def fake_task(**updates) -> ImportedTask:
    values = dict(
        suite_id="evalplus-humaneval",
        upstream_id="fake/0",
        revision="fake-v1",
        source_sha256="a" * 64,
        title="Explicit fake fixture",
        prompt="Add one.",
        category="programming",
        good_solution="correct",
        bad_solution="wrong",
        recipe=NativeRecipe(
            family="fake", grader_command=["fake-grader"], grader_result_path="/grader/result.json"
        ),
    )
    values.update(updates)
    return ImportedTask(**values)


@pytest.fixture
def fake_runtime(tmp_path, monkeypatch):
    instances = []

    class FakeEnvironment:
        """Never interprets candidate code or launches a process."""

        def __init__(self, *args, **kwargs):
            self.files = {}
            self.closed = False
            instances.append(self)

        def start(self):
            pass

        def write_file(self, path, content):
            self.files[f"workspace/{path}"] = content.encode()

        def freeze(self):
            return self.files.copy()

        def recover_submission(self):
            return self.files.copy(), {"status": "recovered" if self.files else "unavailable"}

        def close(self):
            self.closed = True

        def grade(self, frozen):
            assert self.closed or not self.files
            passed = next(iter(frozen.values())) == b"correct"
            return {"passed": passed, "score": int(passed), "checks": []}

    monkeypatch.setattr(sandbox, "NativeEnvironment", FakeEnvironment)
    monkeypatch.setattr(preparation, "preparation_info", lambda *args: {"image_id": "fake-image"})
    return instances


@pytest.mark.parametrize("variant,status", [("good", "passed"), ("bad", "failed")])
def test_imported_lifecycle_exports_original_identity(tmp_path, fake_runtime, variant, status):
    task = fake_task()
    registry.install(tmp_path, tmp_path, [task])
    store = Store(tmp_path)
    record = store.create_run(RunSpec(task_id=task.id, scripted_variant=variant))
    result = runner.execute_imported(store, record.id)
    assert result.status == status, result.error
    assert all(item.closed for item in fake_runtime)
    assert result.metadata["harness_version"] == "benchmaxxing-imports/1"
    assert result.metadata["upstream"]["task_id"] == "fake/0"
    assert result.metadata["usage"] == {
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
    }
    assert store.events(record.id)[-1].kind == "run.finished"
    with zipfile.ZipFile(io.BytesIO(store.export_run(record.id))) as bundle:
        exported = json.loads(bundle.read("run.json"))
        assert exported["metadata"]["upstream"]["source_sha256"] == "a" * 64
    assert runner.execute_imported(store, record.id) == result


@pytest.mark.parametrize(
    "failure,status",
    [
        (ExecutionCancelled("cancel"), "cancelled"),
        (ExecutionTimeout("deadline"), "budget_exhausted"),
        (ModelError("malformed model response"), "infrastructure_error"),
    ],
)
def test_imported_failure_preserves_files_and_closes_environment(
    tmp_path, monkeypatch, fake_runtime, failure, status
):
    task = fake_task()
    registry.install(tmp_path, tmp_path, [task])
    original = sandbox.NativeEnvironment.write_file

    def failing_write(self, path, content):
        original(self, path, content)
        raise failure

    monkeypatch.setattr(sandbox.NativeEnvironment, "write_file", failing_write)
    store = Store(tmp_path)
    record = store.create_run(RunSpec(task_id=task.id))
    result = runner.execute_imported(store, record.id)
    assert result.status == status
    assert all(item.closed for item in fake_runtime)
    assert any(item.name == "workspace/solution.py" for item in store.artifacts(record.id))
    assert result.metadata["submission_recovery"]["status"] == "recovered"
    assert result.metadata["submitted"] is False and result.metadata["partial_submission"] is True


def test_cancel_before_import_preflight_does_not_construct_environment(tmp_path, fake_runtime):
    store = Store(tmp_path)
    record = store.create_run(RunSpec(task_id=fake_task().id))
    cancel = threading.Event()
    cancel.set()
    assert runner.execute_imported(store, record.id, cancel).status == "cancelled"
    assert not fake_runtime


def test_response_assessment_remains_ungraded(tmp_path, monkeypatch, fake_runtime):
    task = fake_task(
        recipe=NativeRecipe(family="fake", tools="response"), submission_file="response.txt"
    )
    registry.install(tmp_path, tmp_path, [task])
    monkeypatch.setattr(
        sandbox.NativeEnvironment, "grade", lambda *args: {"status": "needs_review"}
    )
    store = Store(tmp_path)
    record = store.create_run(RunSpec(task_id=task.id))
    result = runner.execute_imported(store, record.id)
    assert result.status == "completed" and result.score is None
    assert result.metadata["assessment_status"] == "needs_review"
    assert result.refusal is None


def test_adapter_stream_precedes_validated_tool(tmp_path, monkeypatch, fake_runtime):
    task = fake_task()
    registry.install(tmp_path, tmp_path, [task])

    class FakeAdapter:
        def __init__(self, *args, **kwargs):
            self.turn = 0

        def redact(self, value):
            return value

        def complete(self, messages, tools, **kwargs):
            self.turn += 1
            kwargs["on_delta"]({"type": "text", "text": "Working"})
            return ModelResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        str(self.turn),
                        "write_file",
                        json.dumps({"path": "solution.py", "content": "correct"}),
                    )
                    if self.turn == 1
                    else ToolCall("2", "submit", "{}")
                ],
                model="fake-stream-model",
                finish_reason="tool_calls",
            )

    monkeypatch.setattr(runner, "OpenAIAdapter", FakeAdapter)
    store = Store(tmp_path)
    record = store.create_run(
        RunSpec(
            task_id=task.id,
            participant="openai",
            inference=InferenceProfile(base_url="http://127.0.0.1/v1", model="fake"),
        )
    )
    assert runner.execute_imported(store, record.id).status == "passed"
    kinds = [item.kind for item in store.events(record.id)]
    assert kinds.index("model.delta") < kinds.index("model.response") < kinds.index("tool.started")


@pytest.mark.parametrize(
    "score", [{"score": 1}, {"passed": True, "score": float("nan")}, {"passed": "yes", "score": 1}]
)
def test_invalid_native_scores_do_not_become_passes(score):
    with pytest.raises(EnvironmentError):
        runner._score(score, "fake", "v1")


def test_continuous_native_score_does_not_invent_a_pass_threshold():
    score = runner._score({"passed": None, "score": 0.7}, "fake", "v1")
    assert score.passed is None and score.score == 0.7


def test_import_copies_assets_but_public_contract_omits_grader(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "hidden.txt").write_text("private expected answer")
    task = fake_task(assets=[SourceAsset(source="hidden.txt", target="hidden.txt", scope="grader")])
    directory = tmp_path / "data"
    result = registry.install(directory, source, [task])
    assert result["copied_bytes"] == len(b"private expected answer")
    assert "private expected answer" not in registry.public_task(directory, task).model_dump_json()
    imported = registry.get_imported(directory, task.id)
    assert imported.assets[0].sha256 == hashlib.sha256(b"private expected answer").hexdigest()
    assert registry.install(directory, source, [task])["copied_bytes"] == 0


def test_import_collision_is_preflighted_before_any_new_records(tmp_path):
    existing = fake_task()
    registry.install(tmp_path, tmp_path, [existing])
    new = fake_task(upstream_id="fake/new")
    altered = fake_task(prompt="Changed task under same identity")
    with pytest.raises(ValueError, match="immutable"):
        registry.install(tmp_path, tmp_path, [new, altered])
    assert [task.id for task in registry.imported_tasks(tmp_path)] == [existing.id]


def test_archive_traversal_failure_leaves_no_partial_destination(tmp_path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("safe.txt", "okay")
        bundle.writestr("../outside.txt", "escape")
    destination = downloads.cache_root(tmp_path) / "extracted"
    with pytest.raises(ValueError, match="relative"):
        downloads.extract_archive(tmp_path, archive, destination, max_bytes=1024)
    assert not destination.exists()
    assert not list(destination.parent.glob("*.partial"))
    assert not (destination.parent / "outside.txt").exists()


def test_total_cap_includes_source_cache_and_unique_utility_images(tmp_path):
    registry.save_settings(
        tmp_path, ImportSettings(storage_limit_bytes=1_000_000_000, reserve_free_bytes=0)
    )
    directory = downloads.cache_root(tmp_path)
    (directory / "source.bin").write_bytes(b"source")
    pin = {"image_id": "sha256:a", "image_size_bytes": 600_000_000}
    registry._atomic_json(registry.root(tmp_path) / "source-tools.json", pin)
    registry._atomic_json(registry.root(tmp_path) / "prepared" / "one.json", pin)
    assert downloads.image_bytes(tmp_path) == 600_000_000
    with pytest.raises(ValueError, match="storage cap"):
        downloads.check_capacity(tmp_path, 400_000_000)


def test_job_failure_is_persisted_and_stale_job_recovers(tmp_path, monkeypatch):
    futures = []
    submit = jobs._pool.submit

    def capture(action):
        future = submit(action)
        futures.append(future)
        return future

    monkeypatch.setattr(jobs._pool, "submit", capture)

    def failing_action():
        raise ValueError("Missing prepared source")

    job = jobs.submit_job(tmp_path, "miniwob", "import", failing_action)
    futures[0].result(timeout=5)
    for _ in range(100):
        found = jobs.get_job(tmp_path, job.id)
        if found.status == "failed":
            break
        time.sleep(0.01)
    assert found.status == "failed" and found.error == "Missing prepared source"
    found.status = "running"
    registry._atomic_json(jobs._path(tmp_path, found.id), found.model_dump())
    assert jobs.get_job(tmp_path, found.id).status == "interrupted"


def test_status_poll_transport_failures_retry_without_cancelling(tmp_path, monkeypatch):
    store = Store(tmp_path)
    queued = store.create_run(RunSpec())
    finished = store.update_run(queued.id, status="passed")
    requests = []

    def respond(request):
        requests.append(request.method)
        if len(requests) < 3:
            raise httpx.ReadTimeout("temporary loss", request=request)
        return httpx.Response(200, json={"run": finished.model_dump(mode="json")})

    monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    with httpx.Client(
        base_url="http://127.0.0.1", transport=httpx.MockTransport(respond)
    ) as client:
        assert cli._wait_remote_run(client, queued).status == "passed"
    assert requests == ["GET", "GET", "GET"]


def test_provider_api_jobs_are_authorized_and_share_the_catalog(tmp_path):
    source = tmp_path / "humaneval.json"
    source.write_text(
        json.dumps(
            {
                "task_id": "HumanEval/0",
                "prompt": "def answer():\n",
                "entry_point": "answer",
                "canonical_solution": "    return 1\n",
                "test": "def check(candidate):\n    assert candidate() == 1\n",
            }
        )
    )
    data = tmp_path / "data"
    owner = Controller(data, cleanup=lambda _: None)
    with TestClient(create_app(data, controller=owner), base_url="http://127.0.0.1:8123") as client:
        payload = {
            "suite_id": "evalplus-humaneval",
            "source_path": str(source),
            "revision": "fake-api-v1",
        }
        assert client.post("/api/v1/providers/import", json=payload).status_code == 403
        token = client.get("/api/v1/session").json()["token"]
        response = client.post(
            "/api/v1/providers/import", json=payload, headers={"X-Benchmaxxing-Token": token}
        )
        assert response.status_code == 202
        job_id = response.json()["id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = client.get(f"/api/v1/providers/jobs/{job_id}").json()
            if job["status"] not in {"queued", "running"}:
                break
            time.sleep(0.05)
        assert job["status"] == "completed", job
        provider = next(
            item
            for item in client.get("/api/v1/providers").json()
            if item["suite"]["id"] == "evalplus-humaneval"
        )
        assert provider["imported_tasks"] == 1 and provider["prepared_tasks"] == 0
        catalog = client.get("/api/v1/tasks").json()
        imported = [item for item in catalog if item["kind"] == "native"]
        assert len(imported) == 1 and imported[0]["prepared"] is False
        assert "canonical_solution" not in json.dumps(imported)
