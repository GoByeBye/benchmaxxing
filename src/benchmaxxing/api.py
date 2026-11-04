"""Loopback-only HTTP API and bundled UI for a single local operator."""

from __future__ import annotations

import asyncio
import hmac
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from benchmaxxing import __version__
from benchmaxxing.contracts import (
    TERMINAL_STATUSES,
    ActivitySnapshot,
    DoctorReport,
    RunDetail,
    RunRecord,
    RunSpec,
    SessionInfo,
    TaskSpec,
)
from benchmaxxing.controller import Controller, RecoveryPending
from benchmaxxing.providers.contracts import (
    FetchRequest,
    ImportRequest,
    ImportSettings,
    PreparationJob,
    PreparationRequest,
    SuiteStatus,
    SWEPlanRequest,
    SWEPreparationRequest,
)


def create_app(
    data_dir: Path,
    *,
    port: int = 8123,
    controller: Controller | None = None,
) -> FastAPI:
    owner = controller or Controller(data_dir)
    allowed_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await asyncio.to_thread(owner.start, f"http://127.0.0.1:{port}")
        try:
            yield
        finally:
            await asyncio.to_thread(owner.stop)

    application = FastAPI(
        title="Benchmaxxing",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/v1/openapi.json",
    )
    application.state.controller = owner
    application.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

    @application.middleware("http")
    async def local_boundary(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        origin = request.headers.get("origin")
        if origin and origin not in allowed_origins:
            return Response("Origin is not allowed", status_code=403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            return Response("Cross-site requests are not allowed", status_code=403)
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            supplied = request.headers.get("x-benchmaxxing-token", "")
            if not hmac.compare_digest(supplied, owner.token):
                return Response("Session token required", status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; object-src 'none'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @application.exception_handler(KeyError)
    async def missing(_: Request, __: KeyError) -> Response:
        return Response("Resource not found", status_code=404)

    @application.exception_handler(RecoveryPending)
    async def recovery_required(_: Request, exc: RecoveryPending) -> Response:
        return JSONResponse({"detail": str(exc)}, status_code=503)

    @application.exception_handler(RequestValidationError)
    async def invalid_spec(_: Request, exc: RequestValidationError) -> Response:
        # Never reflect credential-bearing invalid inputs or validation context.
        errors = [{"loc": e["loc"], "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
        return JSONResponse({"detail": errors}, status_code=422)

    @application.get("/api/v1/health")
    def health() -> dict[str, str]:
        return {"version": __version__, "workspace_id": owner.store.workspace_id}

    @application.get("/api/v1/session", response_model=SessionInfo)
    def session() -> SessionInfo:
        return SessionInfo(token=owner.token, version=__version__)

    @application.get("/api/v1/doctor", response_model=DoctorReport)
    def doctor() -> DoctorReport:
        from benchmaxxing.environment import doctor as inspect_environment

        result = inspect_environment(owner.store.data_dir)
        if "checks" in result:
            return DoctorReport.model_validate(result)
        return DoctorReport(ready=bool(result.get("ready", False)), checks=result)

    @application.get("/api/v1/tasks", response_model=list[TaskSpec])
    def tasks(include_archived: bool = False) -> list[TaskSpec]:
        from benchmaxxing.task import list_tasks

        return list_tasks(data_dir=owner.store.data_dir, include_archived=include_archived)

    @application.get("/api/v1/providers", response_model=list[SuiteStatus])
    def providers(include_archived: bool = False) -> list[SuiteStatus]:
        from benchmaxxing.providers.registry import suite_statuses

        return suite_statuses(owner.store.data_dir, include_archived=include_archived)

    @application.get("/api/v1/providers/settings", response_model=ImportSettings)
    def provider_settings() -> ImportSettings:
        from benchmaxxing.providers.registry import settings

        return settings(owner.store.data_dir)

    @application.put("/api/v1/providers/settings", response_model=ImportSettings)
    def set_provider_settings(value: ImportSettings) -> ImportSettings:
        from benchmaxxing.providers.registry import save_settings

        try:
            save_settings(owner.store.data_dir, value)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return value

    @application.get("/api/v1/providers/jobs", response_model=list[PreparationJob])
    def provider_jobs() -> list[PreparationJob]:
        from benchmaxxing.providers.jobs import list_jobs

        return list_jobs(owner.store.data_dir)

    @application.get("/api/v1/providers/jobs/{job_id}", response_model=PreparationJob)
    def provider_job(job_id: str) -> PreparationJob:
        from benchmaxxing.providers.jobs import get_job

        try:
            return get_job(owner.store.data_dir, job_id)
        except (ValueError, OSError) as exc:
            raise HTTPException(404, "Preparation job not found") from exc

    @application.post("/api/v1/providers/import", status_code=202, response_model=PreparationJob)
    def import_provider(value: ImportRequest) -> PreparationJob:
        from benchmaxxing.providers.catalog import require_active_suite
        from benchmaxxing.providers.jobs import submit_job
        from benchmaxxing.providers.registry import convert, install

        try:
            require_active_suite(value.suite_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

        def import_source() -> dict[str, object]:
            source = Path(value.source_path).resolve()
            tasks = convert(value.suite_id, source, revision=value.revision, variant=value.variant)
            return install(owner.store.data_dir, source, tasks)

        return submit_job(owner.store.data_dir, value.suite_id, "import", import_source)

    @application.post("/api/v1/providers/prepare", status_code=202, response_model=PreparationJob)
    def prepare_provider(value: PreparationRequest) -> PreparationJob:
        from benchmaxxing.providers.catalog import require_active_suite
        from benchmaxxing.providers.jobs import submit_job
        from benchmaxxing.providers.registry import root

        try:
            require_active_suite(value.suite_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        records = (root(owner.store.data_dir) / "tasks").glob(f"imported.{value.suite_id}-*.json")
        if next(records, None) is None:
            raise HTTPException(422, "Import the suite's source before preparing its runtime")

        def prepare_source() -> dict[str, object]:
            from benchmaxxing.providers.runtime import prepare_runtime

            return prepare_runtime(
                owner.store.data_dir, value.suite_id, image=value.image, pull=value.pull
            )

        return submit_job(owner.store.data_dir, value.suite_id, "prepare", prepare_source)

    @application.post("/api/v1/providers/fetch", status_code=202, response_model=PreparationJob)
    def fetch_provider(value: FetchRequest) -> PreparationJob:
        from benchmaxxing.providers.catalog import require_active_suite
        from benchmaxxing.providers.jobs import submit_job
        from benchmaxxing.providers.sources import fetch_suite

        try:
            require_active_suite(value.suite_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return submit_job(
            owner.store.data_dir,
            value.suite_id,
            "fetch",
            lambda: fetch_suite(owner.store.data_dir, value.suite_id),
        )

    def swe_job(value: SWEPlanRequest, *, prepare: bool) -> PreparationJob:
        from benchmaxxing.providers.coding_sources import (
            prepare_swebench_instance,
            swebench_preparation_plan,
        )
        from benchmaxxing.providers.jobs import submit_job
        from benchmaxxing.providers.locking import preparation_scope
        from benchmaxxing.providers.registry import get_imported

        try:
            task = get_imported(owner.store.data_dir, value.task_id)
            if task.suite_id not in {"swebench-lite", "swebench-verified"}:
                raise ValueError("Choose an imported SWE-bench instance")
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

        def action() -> dict[str, object]:
            with preparation_scope(owner.store.data_dir):
                if prepare:
                    assert isinstance(value, SWEPreparationRequest)
                    return prepare_swebench_instance(
                        owner.store.data_dir,
                        value.task_id,
                        reservation_bytes=value.reservation_bytes,
                        pull=value.pull,
                    )
                return swebench_preparation_plan(owner.store.data_dir, value.task_id)

        return submit_job(owner.store.data_dir, task.suite_id, "prepare", action)

    @application.post("/api/v1/providers/swe-plan", status_code=202, response_model=PreparationJob)
    def plan_swe_provider(value: SWEPlanRequest) -> PreparationJob:
        return swe_job(value, prepare=False)

    @application.post(
        "/api/v1/providers/swe-prepare", status_code=202, response_model=PreparationJob
    )
    def prepare_swe_provider(value: SWEPreparationRequest) -> PreparationJob:
        return swe_job(value, prepare=True)

    @application.get("/api/v1/run-progress", response_model=ActivitySnapshot)
    @application.get("/api/v1/activity", response_model=ActivitySnapshot, deprecated=True)
    def activity() -> ActivitySnapshot:
        return owner.store.activity()

    @application.get("/api/v1/runs", response_model=list[RunRecord])
    def list_runs(limit: int = Query(default=1000, ge=1, le=10000)) -> list[RunRecord]:
        return owner.store.list_runs(limit)

    @application.post("/api/v1/runs", response_model=RunRecord, status_code=201)
    def start_run(spec: RunSpec) -> RunRecord:
        try:
            return owner.submit(spec)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @application.get("/api/v1/runs/{run_id}", response_model=RunDetail)
    def run_detail(run_id: str) -> RunDetail:
        return RunDetail(run=owner.store.get_run(run_id), artifacts=owner.store.artifacts(run_id))

    @application.post("/api/v1/runs/{run_id}/cancel", response_model=RunRecord)
    def cancel_run(run_id: str) -> RunRecord:
        return owner.cancel(run_id)

    @application.get("/api/v1/runs/{run_id}/events")
    async def events(
        request: Request, run_id: str, after: int = Query(default=0, ge=0)
    ) -> Response:
        owner.store.get_run(run_id)
        last = request.headers.get("last-event-id")
        if last:
            if not last.isdigit():
                raise HTTPException(400, "Invalid Last-Event-ID")
            after = max(after, int(last))

        async def stream() -> AsyncIterator[str]:
            cursor = after
            heartbeat = time.monotonic()
            while not await request.is_disconnected():
                batch = await asyncio.to_thread(owner.store.events, run_id, after=cursor, limit=64)
                for event in batch:
                    cursor = event.seq
                    yield f"id: {event.seq}\nevent: run_event\ndata: {event.model_dump_json()}\n\n"
                if len(batch) == 64:
                    continue
                record = await asyncio.to_thread(owner.store.get_run, run_id)
                if record.status in TERMINAL_STATUSES:
                    # Drain events committed just before the terminal state became visible.
                    remaining = await asyncio.to_thread(
                        owner.store.events, run_id, after=cursor, limit=1
                    )
                    if remaining:
                        continue
                    yield "event: complete\ndata: {}\n\n"
                    break
                if time.monotonic() - heartbeat >= 10:
                    yield ": heartbeat\n\n"
                    heartbeat = time.monotonic()
                await asyncio.sleep(0.2)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            },
        )

    @application.get("/api/v1/runs/{run_id}/artifacts/{artifact_id}")
    def artifact(run_id: str, artifact_id: str) -> Response:
        try:
            record, content = owner.store.artifact_content(run_id, artifact_id)
        except ValueError as exc:
            raise HTTPException(409, "Artifact integrity check failed") from exc
        return Response(
            content,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{record.id}.txt"',
            },
        )

    @application.get("/api/v1/runs/{run_id}/export")
    def export(run_id: str) -> Response:
        try:
            content = owner.store.export_run(run_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return Response(
            content,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="benchmaxxing-{run_id}.zip"',
            },
        )

    static = Path(__file__).parent / "static"
    if (static / "index.html").is_file():
        application.mount("/", StaticFiles(directory=static, html=True), name="ui")
    else:

        @application.get("/", response_class=HTMLResponse)
        def build_required() -> str:
            return (
                "<h1>Benchmaxxing</h1><p>Build the UI: "
                "cd web &amp;&amp; npm ci &amp;&amp; npm run build</p>"
            )

    return application
