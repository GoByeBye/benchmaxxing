"""Batch accounting remains exact without scanning the whole cache per file."""

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from filelock import FileLock

from benchmaxxing.providers import accounting, downloads, registry
from benchmaxxing.providers.contracts import ImportSettings


@pytest.fixture
def data(tmp_path: Path) -> Path:
    directory = tmp_path / "controller"
    registry.save_settings(
        directory,
        ImportSettings(
            cache_dir=str(tmp_path / "cache"),
            storage_limit_bytes=1_000_000_000,
            reserve_free_bytes=0,
        ),
    )
    return directory


def test_batch_tracks_writes_overwrites_deletes_and_external_import_records(data: Path) -> None:
    record = registry.root(data) / "tasks" / "example.json"
    source = downloads.cache_root(data) / "source"
    with downloads.capacity_batch(data):
        initial = accounting.active(data)
        assert initial is not None and initial.used > 0  # C-side settings file is counted.
        source.write_bytes(b"x" * 200)
        downloads.note_file(source)
        registry._atomic_json(record, {"private": "evidence"})
        registry._atomic_json(record, {"private": "replacement evidence"})
        source.unlink()
        downloads.note_file(source)
        assert initial.used == accounting.storage_bytes(data)


def test_large_batch_scans_only_at_entry_and_final_audit(
    data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scan = accounting._files
    calls = []
    monkeypatch.setattr(
        accounting, "_files", lambda path, *args: (calls.append(path), scan(path, *args))[1]
    )
    with downloads.capacity_batch(data):
        with downloads.capacity_batch(data):
            for number in range(100):
                downloads.check_capacity(data, 1000)
                registry._atomic_json(downloads.cache_root(data) / f"{number}.json", {"n": number})
    assert len(calls) == 4  # Two distinct roots, once at entry and once at exit.


def test_batch_deduplicates_images_and_checks_growth_before_publication(data: Path) -> None:
    first = registry.root(data) / "runtime-builds" / "one.json"
    second = registry.root(data) / "runtime-builds" / "two.json"
    image = {"image_id": "sha256:" + "a" * 64, "image_size_bytes": 600_000_000}
    with downloads.capacity_batch(data):
        registry._atomic_json(first, image)
        registry._atomic_json(second, image)
        state = accounting.active(data)
        assert state is not None and state.image_bytes == 600_000_000
        registry._atomic_json(first, image | {"image_size_bytes": 700_000_000})
        assert state.image_bytes == 700_000_000
        before = first.read_bytes()
        with pytest.raises(ValueError, match="storage cap"):
            registry._atomic_json(first, image | {"image_size_bytes": 1_000_000_000})
        assert first.read_bytes() == before
        with pytest.raises(ValueError, match="storage cap"):
            downloads.check_capacity(data, 300_000_000)


def test_batch_requires_workflow_ownership_and_detects_unnotified_write(data: Path) -> None:
    cache = downloads.cache_root(data)
    cache.mkdir(parents=True, exist_ok=True)
    with FileLock(str(cache / "workflow.lock")):
        with pytest.raises(ValueError, match="owns this cache"):
            with downloads.capacity_batch(data):
                pytest.fail("Entered another owner's batch")
    with pytest.raises(ValueError, match="missed"):
        with downloads.capacity_batch(data):
            (cache / "untracked").write_bytes(b"not silently ignored")
    with downloads.capacity_batch(data):  # A new batch takes a fresh snapshot.
        state = accounting.active(data)
        assert state is not None and state.used == accounting.storage_bytes(data)


def test_batch_keeps_physical_reserve_checks(data: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with downloads.capacity_batch(data):
        with monkeypatch.context() as patch:
            patch.setattr(accounting.shutil, "disk_usage", lambda _: SimpleNamespace(free=5))
            with pytest.raises(ValueError, match="free-space reserve"):
                downloads.check_capacity(data, 6)


def test_download_updates_exact_batch_bytes(data: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = httpx.Client
    transport = httpx.MockTransport(lambda _: httpx.Response(200, content=b"frozen source data"))
    monkeypatch.setattr(downloads.httpx, "Client", lambda **kw: client(transport=transport, **kw))
    destination = downloads.cache_root(data) / "data.bin"
    with downloads.capacity_batch(data):
        result = downloads.download(data, "https://example.test/source", destination, max_bytes=100)
        assert result["bytes"] == 18
        state = accounting.active(data)
        assert state is not None and state.used == accounting.storage_bytes(data)


def test_retained_preparation_file_is_accounted_before_atomic_overwrite(
    data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = downloads.cache_root(data) / "build" / "recipe.txt"
    with downloads.capacity_batch(data):
        downloads.write_prepared_file(data, path, b"first recipe")
        batch = accounting.active(data)
        assert batch is not None and batch.used == accounting.storage_bytes(data)
        config = registry.settings(data)
        monkeypatch.setattr(
            registry,
            "settings",
            lambda _: config.model_copy(update={"storage_limit_bytes": batch.used + 3}),
        )
        with pytest.raises(ValueError, match="storage cap"):
            downloads.write_prepared_file(data, path, b"x" * 100)
        assert path.read_bytes() == b"first recipe"
        assert batch.used == accounting.storage_bytes(data)


def test_preparation_download_follows_only_https_redirects(
    data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_client = httpx.Client
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.host == "example.test":
            return httpx.Response(302, headers={"location": "https://files.example.test/final"})
        return httpx.Response(200, content=b"verified source")

    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(
        downloads.httpx, "Client", lambda **kwargs: original_client(transport=transport, **kwargs)
    )
    destination = downloads.cache_root(data) / "redirected.bin"
    result = downloads.download(data, "https://example.test/source", destination, max_bytes=100)
    assert requests == ["https://example.test/source", "https://files.example.test/final"]
    assert result["sha256"] == registry.file_hash(destination)


def test_preparation_download_rejects_http_redirect_before_request(
    data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_client = httpx.Client
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://files.example.test/final"})

    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(
        downloads.httpx, "Client", lambda **kwargs: original_client(transport=transport, **kwargs)
    )
    destination = downloads.cache_root(data) / "insecure.bin"
    with pytest.raises(ValueError, match="HTTPS"):
        downloads.download(data, "https://example.test/source", destination, max_bytes=100)
    assert requests == ["https://example.test/source"]
    assert not destination.exists()


def test_runtime_preparation_checks_receipt_growth_before_publish(
    data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from benchmaxxing.providers import runtime

    config = registry.settings(data)
    used = accounting.storage_bytes(data)
    monkeypatch.setattr(
        registry,
        "settings",
        lambda _: config.model_copy(update={"storage_limit_bytes": used + 16}),
    )
    monkeypatch.setattr(runtime, "require_active_suite", lambda _: None)
    monkeypatch.setattr(runtime, "check_preparation_scope", lambda *_: None)
    monkeypatch.setattr(registry, "imported_tasks", lambda *_: [object()])
    receipt = registry.root(data) / "prepared" / "oversized.json"

    def write_receipt(*_args: object, **_kwargs: object) -> dict[str, object]:
        registry._atomic_json(receipt, {"evidence": "x" * 100})
        return {}

    monkeypatch.setattr(runtime, "prepare_suite", write_receipt)
    with pytest.raises(ValueError, match="storage cap"):
        runtime.prepare_runtime(data, "evalplus-humaneval", image="local-test-image")
    assert not receipt.exists()


def test_swe_plan_checks_direct_file_growth_before_publish(
    data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from benchmaxxing.providers.coding_sources import swebench_preparation_plan

    config = registry.settings(data)
    used = accounting.storage_bytes(data)
    monkeypatch.setattr(
        registry,
        "settings",
        lambda _: config.model_copy(update={"storage_limit_bytes": used + 8}),
    )
    task = SimpleNamespace(
        suite_id="swebench-lite",
        private_files={
            "upstream.json": '{"image":"python:3.13-slim",'
            '"eval_script":"python -m pip install -e .\\npytest -q\\n"}'
        },
        upstream_id="demo__repo-42",
        id="imported.swebench-lite-demo-42",
        source_sha256="a" * 64,
        revision="fixture",
    )
    monkeypatch.setattr(registry, "get_imported", lambda *_: task)
    directory = downloads.cache_root(data) / "build" / "swebench" / task.upstream_id
    with pytest.raises(ValueError, match="storage cap"):
        swebench_preparation_plan(data, task.id)
    assert not (directory / "prepare-dependencies.sh").exists()
    assert not (directory / "plan.json").exists()


def test_controller_job_progress_does_not_invalidate_dependency_accounting(data: Path) -> None:
    import threading

    with downloads.capacity_batch(data):
        worker = threading.Thread(
            target=registry._atomic_json,
            args=(registry.root(data) / "jobs" / "other-request.json", {"status": "queued"}),
        )
        worker.start()
        worker.join()
        state = accounting.active(data)
        assert state is not None and state.used == accounting.storage_bytes(data)
