"""SQLite is the authority for runs/events; artifacts are immutable, hashed files."""

from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import uuid
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from benchmaxxing.contracts import (
    TERMINAL_STATUSES,
    ActivitySnapshot,
    ArtifactRecord,
    RunActivity,
    RunEvent,
    RunRecord,
    RunSpec,
    RunStatus,
    ScoreCard,
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def workspace_id(data_dir: Path) -> str:
    return hashlib.sha256(os.path.normcase(str(data_dir.resolve())).encode()).hexdigest()[:24]


class Store:
    def __init__(self, data_dir: Path | str):
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.workspace_id = workspace_id(self.data_dir)
        self.db_path = self.data_dir / "runs.sqlite3"
        self.artifact_dir = self.data_dir / "artifacts"
        self.artifact_dir.mkdir(exist_ok=True)
        with self._connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError(f"Unsupported database schema version {version}")
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, record TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(id),
                    timestamp TEXT NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_run ON events(run_id, seq);
                CREATE INDEX IF NOT EXISTS runs_status ON runs(json_extract(record, '$.status'));
                CREATE TABLE IF NOT EXISTS run_progress (
                    run_id TEXT PRIMARY KEY REFERENCES runs(id),
                    phase TEXT NOT NULL DEFAULT 'queued', started_at TEXT,
                    turn INTEGER NOT NULL DEFAULT 0, last_tool TEXT,
                    last_event_seq INTEGER NOT NULL DEFAULT 0, last_event_at TEXT,
                    cancellation_requested INTEGER NOT NULL DEFAULT 0,
                    checks_done INTEGER NOT NULL DEFAULT 0, checks_total INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                    record TEXT NOT NULL
                );
                PRAGMA user_version=1;
            """)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def create_run(self, spec: RunSpec, metadata: dict[str, Any] | None = None) -> RunRecord:
        now = utc_now()
        record = RunRecord(
            id=uuid.uuid4().hex,
            status="queued",
            spec=spec,
            created_at=now,
            updated_at=now,
            metadata=metadata or {},
        )
        with self._connect() as db:
            db.execute("INSERT INTO runs VALUES (?, ?)", (record.id, record.model_dump_json()))
        self.append_event(record.id, "queued", {"task_id": spec.task_id})
        return record

    def get_run(self, run_id: str) -> RunRecord:
        with self._connect() as db:
            row = db.execute("SELECT record FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return RunRecord.model_validate_json(row["record"])

    def list_runs(self, limit: int = 100) -> list[RunRecord]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT record FROM runs ORDER BY rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [RunRecord.model_validate_json(row["record"]) for row in rows]

    @staticmethod
    def _progress(
        db: sqlite3.Connection,
        run_id: str,
        seq: int,
        timestamp: str,
        kind: str,
        data: dict[str, Any],
    ) -> None:
        """Update a small projection atomically with its authoritative event."""
        db.execute("INSERT OR IGNORE INTO run_progress(run_id) VALUES (?)", (run_id,))
        values: dict[str, Any] = {"last_event_seq": seq, "last_event_at": timestamp}
        phases = {"preflight", "environment", "participant", "collect", "grade", "cleanup"}
        if kind == "run.started":
            values.update(started_at=timestamp, phase="preflight")
        elif kind == "phase" and isinstance(data.get("phase"), str) and data["phase"] in phases:
            values["phase"] = data["phase"]
        elif kind == "model.request":
            values["phase"] = "inference"
            turn = data.get("turn")
            if isinstance(turn, int) and not isinstance(turn, bool) and 0 <= turn <= 256:
                values["turn"] = turn
        elif kind == "model.delta" and data.get("type") in (
            "content",
            "refusal",
            "reasoning",
            "tool_call_delta",
        ):
            values["phase"] = "generating"
        elif kind == "tool.started":
            values["phase"] = "tool"
            tool = data.get("tool")
            values["last_tool"] = (
                tool
                if isinstance(tool, str)
                and tool in {"read_file", "write_file", "execute", "submit"}
                else "unknown"
            )
        elif kind == "tool.result":
            values["phase"] = "participant"
        elif kind == "grading.check":
            done, total = data.get("completed"), data.get("total")
            if type(done) is int and type(total) is int and 0 <= done <= total <= 128:
                values.update(phase="grade", checks_done=done, checks_total=total)
        elif kind == "cancellation_requested":
            values["cancellation_requested"] = 1
        elif kind in {"run.finished", "cancelled", "interrupted", "infrastructure_error"}:
            values["phase"] = str(data.get("status", kind))[:32]
        assignments = ", ".join(f"{name}=?" for name in values)
        db.execute(
            f"UPDATE run_progress SET {assignments} WHERE run_id=?", [*values.values(), run_id]
        )

    def activity(self) -> ActivitySnapshot:
        """Read active progress without replaying large prompts or scanning run history."""
        from benchmaxxing.task import get_task

        now = datetime.now(UTC)
        with self._connect() as db:
            rows = db.execute("""
                SELECT r.record, p.* FROM runs r LEFT JOIN run_progress p ON p.run_id=r.id
                WHERE json_extract(r.record, '$.status') IN ('queued','running')
                ORDER BY CASE json_extract(r.record, '$.status') WHEN 'running' THEN 0 ELSE 1 END,
                    r.rowid
            """).fetchall()
        activities: list[RunActivity] = []
        position = 0
        for row in rows:
            run = RunRecord.model_validate_json(row["record"])
            saved_task = run.metadata.get("task")
            if not isinstance(saved_task, dict):
                try:
                    saved_task = get_task(run.spec.task_id, data_dir=self.data_dir).model_dump()
                except ValueError:
                    saved_task = {}
            queued = run.status == "queued"
            if queued:
                position += 1
            started = row["started_at"]
            elapsed = 0.0
            if started is not None and not queued:
                try:
                    elapsed = max(0.0, (now - datetime.fromisoformat(started)).total_seconds())
                except (ValueError, TypeError):
                    started = None
            activities.append(
                RunActivity(
                    id=run.id,
                    status="queued" if queued else "running",
                    task_id=run.spec.task_id,
                    task_title=str(saved_task.get("title", run.spec.task_id)),
                    category=str(saved_task.get("category", "unknown")),
                    participant=run.spec.participant,
                    model=run.spec.inference.model
                    if run.spec.inference
                    else f"scripted/{run.spec.scripted_variant}",
                    created_at=run.created_at,
                    updated_at=run.updated_at,
                    started_at=started,
                    phase="queued" if queued else (row["phase"] or "preflight"),
                    turn=row["turn"] or 0,
                    max_turns=run.spec.budgets.max_turns,
                    wall_seconds=run.spec.budgets.wall_seconds,
                    elapsed_seconds=elapsed,
                    queue_position=position if queued else None,
                    last_event_at=row["last_event_at"],
                    last_event_seq=row["last_event_seq"] or 0,
                    cancellation_requested=bool(row["cancellation_requested"]),
                    last_tool=row["last_tool"],
                    checks_done=row["checks_done"] or 0,
                    checks_total=row["checks_total"] or 0,
                )
            )
        return ActivitySnapshot(server_time=now.isoformat(), runs=activities)

    def update_run(
        self,
        run_id: str,
        *,
        status: RunStatus | None = None,
        score: ScoreCard | None = None,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
        refusal: str | None = None,
        event_kind: str | None = None,
        event_data: dict[str, Any] | None = None,
    ) -> RunRecord:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT record FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            record = RunRecord.model_validate_json(row["record"])
            if status is not None:
                record.status = status
            if score is not None:
                record.score = score
            if error is not None:
                record.error = error
            if metadata is not None:
                record.metadata.update(metadata)
            if refusal is not None:
                record.refusal = refusal
            record.updated_at = utc_now()
            db.execute("UPDATE runs SET record=? WHERE id=?", (record.model_dump_json(), run_id))
            if event_kind is not None:
                cursor = db.execute(
                    "INSERT INTO events(run_id,timestamp,kind,data) VALUES (?,?,?,?)",
                    (run_id, record.updated_at, event_kind, json.dumps(event_data or {})),
                )
                assert cursor.lastrowid is not None
                self._progress(
                    db, run_id, cursor.lastrowid, record.updated_at, event_kind, event_data or {}
                )
        return record

    def append_event(self, run_id: str, kind: str, data: dict[str, Any]) -> RunEvent:
        timestamp = utc_now()
        with self._connect() as db:
            cursor = db.execute(
                "INSERT INTO events(run_id,timestamp,kind,data) VALUES (?,?,?,?)",
                (run_id, timestamp, kind, json.dumps(data, ensure_ascii=False)),
            )
            seq = cursor.lastrowid
            assert seq is not None
            self._progress(db, run_id, seq, timestamp, kind, data)
        return RunEvent(seq=seq, run_id=run_id, timestamp=timestamp, kind=kind, data=data)

    def events(self, run_id: str, after: int = 0, *, limit: int | None = None) -> list[RunEvent]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM events WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?",
                (run_id, after, -1 if limit is None else limit),
            ).fetchall()
        return [
            RunEvent(
                seq=r["seq"],
                run_id=r["run_id"],
                timestamp=r["timestamp"],
                kind=r["kind"],
                data=json.loads(r["data"]),
            )
            for r in rows
        ]

    def add_artifact(
        self,
        run_id: str,
        name: str,
        content: bytes,
        media_type: str = "text/plain",
    ) -> ArtifactRecord:
        self.get_run(run_id)
        record = ArtifactRecord(
            id=uuid.uuid4().hex,
            run_id=run_id,
            name=name,
            media_type=media_type,
            size=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        )
        target = self.artifact_dir / record.id
        # Exclusive creation prevents overwriting or following an existing symlink.
        with target.open("xb") as file:
            file.write(content)
        with self._connect() as db:
            db.execute(
                "INSERT INTO artifacts VALUES (?,?,?)",
                (
                    record.id,
                    run_id,
                    record.model_dump_json(),
                ),
            )
        return record

    def artifacts(self, run_id: str) -> list[ArtifactRecord]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT record FROM artifacts WHERE run_id=? ORDER BY rowid",
                (run_id,),
            ).fetchall()
        return [ArtifactRecord.model_validate_json(r["record"]) for r in rows]

    def artifact_content(self, run_id: str, artifact_id: str) -> tuple[ArtifactRecord, bytes]:
        with self._connect() as db:
            row = db.execute(
                "SELECT record FROM artifacts WHERE id=? AND run_id=?",
                (artifact_id, run_id),
            ).fetchone()
        if row is None:
            raise KeyError(artifact_id)
        record = ArtifactRecord.model_validate_json(row["record"])
        path = self.artifact_dir / record.id
        if path.is_symlink() or path.resolve().parent != self.artifact_dir.resolve():
            raise ValueError("Artifact path failed integrity validation")
        content = path.read_bytes()
        if len(content) != record.size or hashlib.sha256(content).hexdigest() != record.sha256:
            raise ValueError("Artifact checksum mismatch")
        return record, content

    def mark_interrupted(self) -> list[RunRecord]:
        interrupted = []
        for record in self.list_runs(limit=1000000):
            if record.status in {"queued", "running"}:
                updated = self.update_run(
                    record.id,
                    status="interrupted",
                    error="Controller stopped before completion",
                    event_kind="interrupted",
                    event_data={"reason": "controller_restart"},
                )
                interrupted.append(updated)
        return interrupted

    def export_run(self, run_id: str) -> bytes:
        run = self.get_run(run_id)
        if run.status not in TERMINAL_STATUSES:
            raise ValueError("Run must finish before export")
        events = self.events(run_id)
        entries: dict[str, bytes] = {
            "run.json": run.model_dump_json(indent=2).encode(),
            "events.jsonl": ("\n".join(e.model_dump_json() for e in events) + "\n").encode(),
        }
        artifacts = self.artifacts(run_id)
        entries["artifacts.json"] = json.dumps(
            [a.model_dump() for a in artifacts],
            indent=2,
        ).encode()
        for artifact in artifacts:
            _, content = self.artifact_content(run_id, artifact.id)
            entries[f"artifacts/{artifact.id}"] = content
        manifest = {
            "schema_version": 1,
            "run_id": run_id,
            "files": {
                name: {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
                for name, data in entries.items()
            },
        }
        entries["manifest.json"] = json.dumps(manifest, indent=2).encode()
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in entries.items():
                archive.writestr(name, data)
        return output.getvalue()
