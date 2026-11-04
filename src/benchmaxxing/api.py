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
    DoctorReport,
    RunDetail,
    RunRecord,
    RunSpec,
    SessionInfo,
    TaskSpec,
)
from benchmaxxing.controller import Controller, RecoveryPending


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
    def tasks() -> list[TaskSpec]:
        from benchmaxxing.task import list_tasks

        return list_tasks()

    @application.get("/api/v1/runs", response_model=list[RunRecord])
    def list_runs(limit: int = Query(default=1000, ge=1, le=10000)) -> list[RunRecord]:
        return owner.store.list_runs(limit)

    @application.post("/api/v1/runs", response_model=RunRecord, status_code=201)
    def start_run(spec: RunSpec) -> RunRecord:
        from benchmaxxing.task import get_task

        try:
            get_task(spec.task_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return owner.submit(spec)

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
                batch = owner.store.events(run_id, after=cursor)
                for event in batch:
                    cursor = event.seq
                    yield f"id: {event.seq}\nevent: run_event\ndata: {event.model_dump_json()}\n\n"
                if owner.store.get_run(run_id).status in TERMINAL_STATUSES:
                    # Drain events committed just before the terminal state became visible.
                    remaining = owner.store.events(run_id, after=cursor)
                    for event in remaining:
                        yield (
                            f"id: {event.seq}\nevent: run_event\n"
                            f"data: {event.model_dump_json()}\n\n"
                        )
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
