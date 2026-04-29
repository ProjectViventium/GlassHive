from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from .models import utc_now


class Store:
    def __init__(self, db_path: str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;

                CREATE TABLE IF NOT EXISTS projects (
                    project_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    status TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    default_worker_profile TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS workers (
                    worker_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    role TEXT NOT NULL,
                    profile TEXT NOT NULL,
                    backend TEXT NOT NULL,
                    execution_mode TEXT NOT NULL DEFAULT 'docker',
                    alias TEXT,
                    runtime TEXT NOT NULL,
                    model TEXT NOT NULL,
                    state TEXT NOT NULL,
                    bootstrap_profile TEXT,
                    bootstrap_bundle_json TEXT,
                    gateway_url TEXT,
                    takeover_url TEXT,
                    control_url TEXT,
                    gateway_port INTEGER,
                    gateway_token TEXT,
                    session_key TEXT,
                    state_dir TEXT,
                    workspace_dir TEXT,
                    workspace_root TEXT,
                    pid INTEGER,
                    last_run_id TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES projects(project_id)
                );

                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    worker_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    instruction TEXT NOT NULL,
                    state TEXT NOT NULL,
                    queued_at TEXT NOT NULL,
                    started_at TEXT,
                    ended_at TEXT,
                    output_text TEXT NOT NULL,
                    error_text TEXT NOT NULL,
                    FOREIGN KEY(worker_id) REFERENCES workers(worker_id),
                    FOREIGN KEY(project_id) REFERENCES projects(project_id)
                );

                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    worker_id TEXT NOT NULL,
                    run_id TEXT,
                    event_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(worker_id) REFERENCES workers(worker_id),
                    FOREIGN KEY(project_id) REFERENCES projects(project_id)
                );

                CREATE TABLE IF NOT EXISTS callback_outbox (
                    callback_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    worker_id TEXT NOT NULL,
                    run_id TEXT,
                    event_type TEXT NOT NULL,
                    url TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    delivered_at TEXT,
                    FOREIGN KEY(worker_id) REFERENCES workers(worker_id),
                    FOREIGN KEY(project_id) REFERENCES projects(project_id)
                );

                CREATE INDEX IF NOT EXISTS idx_workers_project_id ON workers(project_id);
                CREATE INDEX IF NOT EXISTS idx_runs_worker_state ON runs(worker_id, state, queued_at);
                CREATE INDEX IF NOT EXISTS idx_events_worker_created ON events(worker_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_callback_outbox_status_updated ON callback_outbox(status, updated_at);
                """
            )
            worker_columns = {row["name"] for row in conn.execute("PRAGMA table_info(workers)").fetchall()}
            if "bootstrap_profile" not in worker_columns:
                conn.execute("ALTER TABLE workers ADD COLUMN bootstrap_profile TEXT")
            if "bootstrap_bundle_json" not in worker_columns:
                conn.execute("ALTER TABLE workers ADD COLUMN bootstrap_bundle_json TEXT")
            if "execution_mode" not in worker_columns:
                conn.execute("ALTER TABLE workers ADD COLUMN execution_mode TEXT NOT NULL DEFAULT 'docker'")
            if "alias" not in worker_columns:
                conn.execute("ALTER TABLE workers ADD COLUMN alias TEXT")
            if "workspace_root" not in worker_columns:
                conn.execute("ALTER TABLE workers ADD COLUMN workspace_root TEXT")

    def _row(self, value: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(value) if value is not None else None

    def _rows(self, values: list[sqlite3.Row]) -> list[dict[str, Any]]:
        return [dict(v) for v in values]

    def create_project(self, owner_id: str, title: str, goal: str, default_worker_profile: str) -> dict[str, Any]:
        project_id = f"prj_{uuid.uuid4().hex[:10]}"
        now = utc_now()
        data = {
            "project_id": project_id,
            "owner_id": owner_id,
            "title": title,
            "goal": goal,
            "status": "active",
            "summary": "",
            "default_worker_profile": default_worker_profile,
            "created_at": now,
            "updated_at": now,
        }
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO projects (project_id, owner_id, title, goal, status, summary, default_worker_profile, created_at, updated_at)
                VALUES (:project_id, :owner_id, :title, :goal, :status, :summary, :default_worker_profile, :created_at, :updated_at)
                """,
                data,
            )
        return data

    def list_projects(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM projects ORDER BY created_at DESC").fetchall()
        return self._rows(rows)

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM projects WHERE project_id = ?", (project_id,)).fetchone()
        return self._row(row)

    def update_project(self, project_id: str, **fields: Any) -> dict[str, Any] | None:
        if not fields:
            return self.get_project(project_id)
        fields["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = :{key}" for key in fields.keys())
        fields["project_id"] = project_id
        with self._connect() as conn:
            conn.execute(f"UPDATE projects SET {assignments} WHERE project_id = :project_id", fields)
            row = conn.execute("SELECT * FROM projects WHERE project_id = ?", (project_id,)).fetchone()
        return self._row(row)

    def create_worker(
        self,
        project_id: str,
        owner_id: str,
        name: str,
        role: str,
        profile: str,
        backend: str,
        runtime: str,
        model: str,
        execution_mode: str = "docker",
        alias: str | None = None,
        workspace_root: str | None = None,
        bootstrap_profile: str | None = None,
        bootstrap_bundle: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        worker_id = f"wrk_{uuid.uuid4().hex[:10]}"
        now = utc_now()
        data = {
            "worker_id": worker_id,
            "project_id": project_id,
            "owner_id": owner_id,
            "name": name,
            "role": role,
            "profile": profile,
            "backend": backend,
            "execution_mode": execution_mode,
            "alias": alias,
            "runtime": runtime,
            "model": model,
            "state": "created",
            "bootstrap_profile": bootstrap_profile,
            "bootstrap_bundle_json": json.dumps(bootstrap_bundle) if bootstrap_bundle else None,
            "gateway_url": None,
            "takeover_url": f"/ui/workers/{worker_id}",
            "control_url": f"/ui/workers/{worker_id}",
            "gateway_port": None,
            "gateway_token": None,
            "session_key": None,
            "state_dir": None,
            "workspace_dir": None,
            "workspace_root": workspace_root,
            "pid": None,
            "last_run_id": None,
            "last_error": None,
            "created_at": now,
            "updated_at": now,
        }
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO workers (
                    worker_id, project_id, owner_id, name, role, profile, backend, execution_mode, alias, runtime, model, state,
                    bootstrap_profile, bootstrap_bundle_json, gateway_url, takeover_url, control_url, gateway_port, gateway_token, session_key,
                    state_dir, workspace_dir, workspace_root, pid, last_run_id, last_error, created_at, updated_at
                ) VALUES (
                    :worker_id, :project_id, :owner_id, :name, :role, :profile, :backend, :execution_mode, :alias, :runtime, :model, :state,
                    :bootstrap_profile, :bootstrap_bundle_json, :gateway_url, :takeover_url, :control_url, :gateway_port, :gateway_token, :session_key,
                    :state_dir, :workspace_dir, :workspace_root, :pid, :last_run_id, :last_error, :created_at, :updated_at
                )
                """,
                data,
            )
        self.add_event(project_id, worker_id, None, "worker.created", f"Worker {name} created")
        return data

    def list_all_workers(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM workers ORDER BY created_at DESC").fetchall()
        return self._rows(rows)

    def list_workers(self, project_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM workers WHERE project_id = ? ORDER BY created_at DESC", (project_id,)).fetchall()
        return self._rows(rows)

    def get_worker(self, worker_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM workers WHERE worker_id = ?", (worker_id,)).fetchone()
        return self._row(row)

    def find_worker_by_alias(self, project_id: str, owner_id: str, alias: str, execution_mode: str | None = None) -> dict[str, Any] | None:
        alias_value = alias.strip()
        if not alias_value:
            return None
        query = "SELECT * FROM workers WHERE project_id = ? AND owner_id = ? AND alias = ?"
        params: list[Any] = [project_id, owner_id, alias_value]
        if execution_mode:
            query += " AND execution_mode = ?"
            params.append(execution_mode)
        query += " ORDER BY created_at DESC LIMIT 1"
        with self._connect() as conn:
            row = conn.execute(query, params).fetchone()
        return self._row(row)

    def update_worker(self, worker_id: str, **fields: Any) -> dict[str, Any] | None:
        if not fields:
            return self.get_worker(worker_id)
        fields["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = :{key}" for key in fields.keys())
        fields["worker_id"] = worker_id
        with self._connect() as conn:
            conn.execute(f"UPDATE workers SET {assignments} WHERE worker_id = :worker_id", fields)
            row = conn.execute("SELECT * FROM workers WHERE worker_id = ?", (worker_id,)).fetchone()
        return self._row(row)

    def update_worker_state(self, worker_id: str, state: str, last_error: str | None = None) -> dict[str, Any] | None:
        fields: dict[str, Any] = {"state": state}
        if last_error is not None:
            fields["last_error"] = last_error
        return self.update_worker(worker_id, **fields)

    def create_run(self, worker_id: str, project_id: str, instruction: str, state: str = "queued") -> dict[str, Any]:
        run_id = f"run_{uuid.uuid4().hex[:10]}"
        queued_at = utc_now()
        data = {
            "run_id": run_id,
            "worker_id": worker_id,
            "project_id": project_id,
            "instruction": instruction,
            "state": state,
            "queued_at": queued_at,
            "started_at": queued_at if state == "running" else None,
            "ended_at": None,
            "output_text": "",
            "error_text": "",
        }
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (run_id, worker_id, project_id, instruction, state, queued_at, started_at, ended_at, output_text, error_text)
                VALUES (:run_id, :worker_id, :project_id, :instruction, :state, :queued_at, :started_at, :ended_at, :output_text, :error_text)
                """,
                data,
            )
        self.update_worker(worker_id, last_run_id=run_id)
        return data

    def list_runs_for_worker(self, worker_id: str, limit: int = 25) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs WHERE worker_id = ? ORDER BY queued_at DESC LIMIT ?",
                (worker_id, limit),
            ).fetchall()
        return self._rows(rows)

    def list_runs_for_project(self, project_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs WHERE project_id = ? ORDER BY queued_at DESC LIMIT ?",
                (project_id, limit),
            ).fetchall()
        return self._rows(rows)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return self._row(row)

    def update_run(self, run_id: str, **fields: Any) -> dict[str, Any] | None:
        if not fields:
            return self.get_run(run_id)
        assignments = ", ".join(f"{key} = :{key}" for key in fields.keys())
        fields["run_id"] = run_id
        with self._connect() as conn:
            conn.execute(f"UPDATE runs SET {assignments} WHERE run_id = :run_id", fields)
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return self._row(row)

    def claim_next_queued_run(self, worker_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM runs WHERE worker_id = ? AND state = 'queued' ORDER BY queued_at ASC LIMIT 1",
                (worker_id,),
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            started_at = utc_now()
            conn.execute(
                "UPDATE runs SET state = 'running', started_at = ? WHERE run_id = ?",
                (started_at, row["run_id"]),
            )
            claimed = conn.execute("SELECT * FROM runs WHERE run_id = ?", (row["run_id"],)).fetchone()
            conn.execute("COMMIT")
        return self._row(claimed)

    def get_active_run(self, worker_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE worker_id = ? AND state = 'running' ORDER BY started_at DESC LIMIT 1",
                (worker_id,),
            ).fetchone()
        return self._row(row)

    def has_queued_runs(self, worker_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM runs WHERE worker_id = ? AND state = 'queued' LIMIT 1",
                (worker_id,),
            ).fetchone()
        return row is not None

    def finalize_run(self, run_id: str, state: str, output_text: str = "", error_text: str = "") -> dict[str, Any] | None:
        return self.update_run(
            run_id,
            state=state,
            ended_at=utc_now(),
            output_text=output_text,
            error_text=error_text,
        )

    def finalize_run_if_state(
        self,
        run_id: str,
        expected_state: str,
        state: str,
        output_text: str = "",
        error_text: str = "",
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE runs
                SET state = ?, ended_at = ?, output_text = ?, error_text = ?
                WHERE run_id = ? AND state = ?
                """,
                (state, utc_now(), output_text, error_text, run_id, expected_state),
            )
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        updated = self._row(row)
        if cur.rowcount and updated:
            return updated
        return None

    def cancel_pending_runs(self, worker_id: str, error_text: str, state: str = "cancelled") -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE runs SET state = ?, ended_at = ?, error_text = ? WHERE worker_id = ? AND state IN ('queued', 'running')",
                (state, utc_now(), error_text, worker_id),
            )
        return cur.rowcount

    def add_event(self, project_id: str, worker_id: str, run_id: str | None, event_type: str, message: str) -> dict[str, Any]:
        data = {
            "event_id": f"evt_{uuid.uuid4().hex[:10]}",
            "project_id": project_id,
            "worker_id": worker_id,
            "run_id": run_id,
            "event_type": event_type,
            "message": message,
            "created_at": utc_now(),
        }
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO events (event_id, project_id, worker_id, run_id, event_type, message, created_at) VALUES (:event_id, :project_id, :worker_id, :run_id, :event_type, :message, :created_at)",
                data,
            )
        return data

    def list_events(self, worker_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM events WHERE worker_id = ? ORDER BY created_at ASC", (worker_id,)).fetchall()
        return self._rows(rows)

    def list_project_events(self, project_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM events WHERE project_id = ? ORDER BY created_at ASC", (project_id,)).fetchall()
        return self._rows(rows)

    def upsert_callback_outbox(
        self,
        *,
        callback_id: str,
        project_id: str,
        worker_id: str,
        run_id: str | None,
        event_type: str,
        url: str,
        payload_json: str,
    ) -> dict[str, Any]:
        now = utc_now()
        data = {
            "callback_id": callback_id,
            "project_id": project_id,
            "worker_id": worker_id,
            "run_id": run_id,
            "event_type": event_type,
            "url": url,
            "payload_json": payload_json,
            "status": "pending",
            "attempts": 0,
            "last_error": "",
            "created_at": now,
            "updated_at": now,
            "delivered_at": None,
        }
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO callback_outbox (
                    callback_id, project_id, worker_id, run_id, event_type, url, payload_json,
                    status, attempts, last_error, created_at, updated_at, delivered_at
                )
                VALUES (
                    :callback_id, :project_id, :worker_id, :run_id, :event_type, :url, :payload_json,
                    :status, :attempts, :last_error, :created_at, :updated_at, :delivered_at
                )
                ON CONFLICT(callback_id) DO UPDATE SET
                    project_id = excluded.project_id,
                    worker_id = excluded.worker_id,
                    run_id = excluded.run_id,
                    event_type = excluded.event_type,
                    url = excluded.url,
                    payload_json = excluded.payload_json,
                    status = 'pending',
                    last_error = '',
                    updated_at = excluded.updated_at,
                    delivered_at = NULL
                """,
                data,
            )
            row = conn.execute("SELECT * FROM callback_outbox WHERE callback_id = ?", (callback_id,)).fetchone()
        return dict(row)

    def mark_callback_delivered(self, callback_id: str, *, attempts: int, payload_json: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE callback_outbox
                SET status = 'delivered',
                    attempts = attempts + ?,
                    payload_json = ?,
                    last_error = '',
                    updated_at = ?,
                    delivered_at = ?
                WHERE callback_id = ?
                """,
                (attempts, payload_json, utc_now(), utc_now(), callback_id),
            )
            row = conn.execute("SELECT * FROM callback_outbox WHERE callback_id = ?", (callback_id,)).fetchone()
        return self._row(row)

    def mark_callback_pending(
        self,
        callback_id: str,
        *,
        attempts: int,
        payload_json: str,
        last_error: str,
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE callback_outbox
                SET status = 'pending',
                    attempts = attempts + ?,
                    payload_json = ?,
                    last_error = ?,
                    updated_at = ?
                WHERE callback_id = ?
                """,
                (attempts, payload_json, last_error[-2000:], utc_now(), callback_id),
            )
            row = conn.execute("SELECT * FROM callback_outbox WHERE callback_id = ?", (callback_id,)).fetchone()
        return self._row(row)

    def list_pending_callbacks(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM callback_outbox
                WHERE status = 'pending'
                ORDER BY updated_at ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return self._rows(rows)

    def metrics(self) -> dict[str, int]:
        with self._connect() as conn:
            projects = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
            workers = conn.execute("SELECT COUNT(*) FROM workers").fetchone()[0]
            runs = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            queued_runs = conn.execute("SELECT COUNT(*) FROM runs WHERE state = 'queued'").fetchone()[0]
            active_runs = conn.execute("SELECT COUNT(*) FROM runs WHERE state = 'running'").fetchone()[0]
            events = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        return {
            "projects": projects,
            "workers": workers,
            "runs": runs,
            "queued_runs": queued_runs,
            "active_runs": active_runs,
            "events": events,
        }
