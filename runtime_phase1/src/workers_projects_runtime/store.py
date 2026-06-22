from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import utc_now


_FAILURE_FIELD_NAMES = {
    "failure_class",
    "failure_retryable",
    "failure_user_message",
    "failure_recommended_recovery",
    "failure_diagnostic_summary",
}


def _normalized_failure_fields(fields: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key in _FAILURE_FIELD_NAMES:
        if key not in fields:
            continue
        value = fields.get(key)
        if key == "failure_retryable":
            normalized[key] = 1 if bool(value) else 0
        else:
            normalized[key] = str(value or "")
    return normalized


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
                    tenant_id TEXT NOT NULL DEFAULT 'local',
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
                    tenant_id TEXT NOT NULL DEFAULT 'local',
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
                    favorite INTEGER NOT NULL DEFAULT 0,
                    compute_released_at TEXT,
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
                    tenant_id TEXT NOT NULL DEFAULT 'local',
                    instruction TEXT NOT NULL,
                    state TEXT NOT NULL,
                    queued_at TEXT NOT NULL,
                    started_at TEXT,
                    ended_at TEXT,
                    output_text TEXT NOT NULL,
                    error_text TEXT NOT NULL,
                    failure_class TEXT NOT NULL DEFAULT '',
                    failure_retryable INTEGER NOT NULL DEFAULT 0,
                    failure_user_message TEXT NOT NULL DEFAULT '',
                    failure_recommended_recovery TEXT NOT NULL DEFAULT '',
                    failure_diagnostic_summary TEXT NOT NULL DEFAULT '',
                    retry_after TEXT,
                    retry_attempts INTEGER NOT NULL DEFAULT 0,
                    last_retry_class TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(worker_id) REFERENCES workers(worker_id),
                    FOREIGN KEY(project_id) REFERENCES projects(project_id)
                );

                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    worker_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL DEFAULT 'local',
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
                    tenant_id TEXT NOT NULL DEFAULT 'local',
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

                CREATE TABLE IF NOT EXISTS scheduled_runs (
                    schedule_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    worker_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL DEFAULT 'local',
                    owner_id TEXT NOT NULL,
                    instruction TEXT NOT NULL,
                    schedule_text TEXT NOT NULL DEFAULT '',
                    run_at TEXT NOT NULL,
                    state TEXT NOT NULL,
                    queued_run_id TEXT,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(worker_id) REFERENCES workers(worker_id),
                    FOREIGN KEY(project_id) REFERENCES projects(project_id)
                );

                CREATE TABLE IF NOT EXISTS user_preferences (
                    tenant_id TEXT NOT NULL DEFAULT 'local',
                    owner_id TEXT NOT NULL,
                    default_worker_profile TEXT NOT NULL DEFAULT '',
                    codex_reasoning_effort TEXT NOT NULL DEFAULT '',
                    claude_effort TEXT NOT NULL DEFAULT '',
                    openclaw_effort TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, owner_id)
                );

                CREATE INDEX IF NOT EXISTS idx_workers_project_id ON workers(project_id);
                CREATE INDEX IF NOT EXISTS idx_runs_worker_state ON runs(worker_id, state, queued_at);
                CREATE INDEX IF NOT EXISTS idx_events_worker_created ON events(worker_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_callback_outbox_status_updated ON callback_outbox(status, updated_at);
                CREATE INDEX IF NOT EXISTS idx_scheduled_runs_state_run_at ON scheduled_runs(state, run_at);
                """
            )
            project_columns = {row["name"] for row in conn.execute("PRAGMA table_info(projects)").fetchall()}
            if "tenant_id" not in project_columns:
                conn.execute("ALTER TABLE projects ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'local'")
            worker_columns = {row["name"] for row in conn.execute("PRAGMA table_info(workers)").fetchall()}
            if "tenant_id" not in worker_columns:
                conn.execute("ALTER TABLE workers ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'local'")
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
            if "favorite" not in worker_columns:
                conn.execute("ALTER TABLE workers ADD COLUMN favorite INTEGER NOT NULL DEFAULT 0")
            if "compute_released_at" not in worker_columns:
                conn.execute("ALTER TABLE workers ADD COLUMN compute_released_at TEXT")
            run_columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
            if "tenant_id" not in run_columns:
                conn.execute("ALTER TABLE runs ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'local'")
            if "failure_class" not in run_columns:
                conn.execute("ALTER TABLE runs ADD COLUMN failure_class TEXT NOT NULL DEFAULT ''")
            if "failure_retryable" not in run_columns:
                conn.execute("ALTER TABLE runs ADD COLUMN failure_retryable INTEGER NOT NULL DEFAULT 0")
            if "failure_user_message" not in run_columns:
                conn.execute("ALTER TABLE runs ADD COLUMN failure_user_message TEXT NOT NULL DEFAULT ''")
            if "failure_recommended_recovery" not in run_columns:
                conn.execute("ALTER TABLE runs ADD COLUMN failure_recommended_recovery TEXT NOT NULL DEFAULT ''")
            if "failure_diagnostic_summary" not in run_columns:
                conn.execute("ALTER TABLE runs ADD COLUMN failure_diagnostic_summary TEXT NOT NULL DEFAULT ''")
            if "retry_after" not in run_columns:
                conn.execute("ALTER TABLE runs ADD COLUMN retry_after TEXT")
            if "retry_attempts" not in run_columns:
                conn.execute("ALTER TABLE runs ADD COLUMN retry_attempts INTEGER NOT NULL DEFAULT 0")
            if "last_retry_class" not in run_columns:
                conn.execute("ALTER TABLE runs ADD COLUMN last_retry_class TEXT NOT NULL DEFAULT ''")
            event_columns = {row["name"] for row in conn.execute("PRAGMA table_info(events)").fetchall()}
            if "tenant_id" not in event_columns:
                conn.execute("ALTER TABLE events ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'local'")
            callback_columns = {row["name"] for row in conn.execute("PRAGMA table_info(callback_outbox)").fetchall()}
            if "tenant_id" not in callback_columns:
                conn.execute("ALTER TABLE callback_outbox ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'local'")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_projects_tenant_owner ON projects(tenant_id, owner_id, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_workers_tenant_owner ON workers(tenant_id, owner_id, project_id, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_tenant_project ON runs(tenant_id, project_id, queued_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_events_tenant_project ON events(tenant_id, project_id, created_at)")
            schedule_columns = {row["name"] for row in conn.execute("PRAGMA table_info(scheduled_runs)").fetchall()}
            if "owner_id" not in schedule_columns:
                conn.execute("ALTER TABLE scheduled_runs ADD COLUMN owner_id TEXT NOT NULL DEFAULT ''")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_scheduled_runs_tenant_owner ON scheduled_runs(tenant_id, owner_id, run_at)")
            preferences_columns = {row["name"] for row in conn.execute("PRAGMA table_info(user_preferences)").fetchall()}
            if "codex_reasoning_effort" not in preferences_columns:
                conn.execute("ALTER TABLE user_preferences ADD COLUMN codex_reasoning_effort TEXT NOT NULL DEFAULT ''")
            if "claude_effort" not in preferences_columns:
                conn.execute("ALTER TABLE user_preferences ADD COLUMN claude_effort TEXT NOT NULL DEFAULT ''")
            if "openclaw_effort" not in preferences_columns:
                conn.execute("ALTER TABLE user_preferences ADD COLUMN openclaw_effort TEXT NOT NULL DEFAULT ''")

    def _row(self, value: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(value) if value is not None else None

    def _rows(self, values: list[sqlite3.Row]) -> list[dict[str, Any]]:
        return [dict(v) for v in values]

    def get_user_preferences(self, tenant_id: str, owner_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM user_preferences WHERE tenant_id = ? AND owner_id = ?",
                (tenant_id or "local", owner_id),
            ).fetchone()
        return self._row(row)

    def upsert_user_preferences(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        default_worker_profile: str | None = None,
        codex_reasoning_effort: str | None = None,
        claude_effort: str | None = None,
        openclaw_effort: str | None = None,
    ) -> dict[str, Any]:
        existing = self.get_user_preferences(tenant_id, owner_id) or {}
        now = utc_now()
        data = {
            "tenant_id": tenant_id or "local",
            "owner_id": owner_id,
            "default_worker_profile": (
                existing.get("default_worker_profile", "") if default_worker_profile is None else default_worker_profile
            ),
            "codex_reasoning_effort": (
                existing.get("codex_reasoning_effort", "") if codex_reasoning_effort is None else codex_reasoning_effort
            ),
            "claude_effort": existing.get("claude_effort", "") if claude_effort is None else claude_effort,
            "openclaw_effort": existing.get("openclaw_effort", "") if openclaw_effort is None else openclaw_effort,
            "updated_at": now,
        }
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO user_preferences (
                    tenant_id, owner_id, default_worker_profile, codex_reasoning_effort,
                    claude_effort, openclaw_effort, updated_at
                )
                VALUES (
                    :tenant_id, :owner_id, :default_worker_profile, :codex_reasoning_effort,
                    :claude_effort, :openclaw_effort, :updated_at
                )
                ON CONFLICT(tenant_id, owner_id) DO UPDATE SET
                    default_worker_profile = excluded.default_worker_profile,
                    codex_reasoning_effort = excluded.codex_reasoning_effort,
                    claude_effort = excluded.claude_effort,
                    openclaw_effort = excluded.openclaw_effort,
                    updated_at = excluded.updated_at
                """,
                data,
            )
            row = conn.execute(
                "SELECT * FROM user_preferences WHERE tenant_id = ? AND owner_id = ?",
                (data["tenant_id"], data["owner_id"]),
            ).fetchone()
        return dict(row)

    def create_project(
        self,
        owner_id: str,
        title: str,
        goal: str,
        default_worker_profile: str,
        tenant_id: str = "local",
    ) -> dict[str, Any]:
        project_id = f"prj_{uuid.uuid4().hex[:10]}"
        now = utc_now()
        data = {
            "project_id": project_id,
            "tenant_id": tenant_id or "local",
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
                INSERT INTO projects (project_id, tenant_id, owner_id, title, goal, status, summary, default_worker_profile, created_at, updated_at)
                VALUES (:project_id, :tenant_id, :owner_id, :title, :goal, :status, :summary, :default_worker_profile, :created_at, :updated_at)
                """,
                data,
            )
        return data

    def list_projects(self, tenant_id: str | None = None, owner_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM projects"
        clauses: list[str] = []
        params: list[Any] = []
        if tenant_id:
            clauses.append("tenant_id = ?")
            params.append(tenant_id)
        if owner_id:
            clauses.append("owner_id = ?")
            params.append(owner_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return self._rows(rows)

    def get_project(
        self,
        project_id: str,
        tenant_id: str | None = None,
        owner_id: str | None = None,
    ) -> dict[str, Any] | None:
        query = "SELECT * FROM projects WHERE project_id = ?"
        params: list[Any] = [project_id]
        if tenant_id:
            query += " AND tenant_id = ?"
            params.append(tenant_id)
        if owner_id:
            query += " AND owner_id = ?"
            params.append(owner_id)
        with self._connect() as conn:
            row = conn.execute(query, params).fetchone()
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
        tenant_id: str = "local",
    ) -> dict[str, Any]:
        worker_id = f"wrk_{uuid.uuid4().hex[:10]}"
        now = utc_now()
        data = {
            "worker_id": worker_id,
            "project_id": project_id,
            "tenant_id": tenant_id or "local",
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
            "favorite": 0,
            "compute_released_at": None,
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
                    worker_id, project_id, tenant_id, owner_id, name, role, profile, backend, execution_mode, alias, runtime, model, state,
                    bootstrap_profile, bootstrap_bundle_json, gateway_url, takeover_url, control_url, gateway_port, gateway_token, session_key,
                    state_dir, workspace_dir, workspace_root, favorite, compute_released_at, pid, last_run_id, last_error, created_at, updated_at
                ) VALUES (
                    :worker_id, :project_id, :tenant_id, :owner_id, :name, :role, :profile, :backend, :execution_mode, :alias, :runtime, :model, :state,
                    :bootstrap_profile, :bootstrap_bundle_json, :gateway_url, :takeover_url, :control_url, :gateway_port, :gateway_token, :session_key,
                    :state_dir, :workspace_dir, :workspace_root, :favorite, :compute_released_at, :pid, :last_run_id, :last_error, :created_at, :updated_at
                )
                """,
                data,
            )
        self.add_event(project_id, worker_id, None, "worker.created", f"Worker {name} created", tenant_id=tenant_id)
        return data

    def list_all_workers(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM workers ORDER BY created_at DESC").fetchall()
        return self._rows(rows)

    def list_workers(
        self,
        project_id: str,
        tenant_id: str | None = None,
        owner_id: str | None = None,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM workers WHERE project_id = ?"
        params: list[Any] = [project_id]
        if tenant_id:
            query += " AND tenant_id = ?"
            params.append(tenant_id)
        if owner_id:
            query += " AND owner_id = ?"
            params.append(owner_id)
        query += " ORDER BY created_at DESC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return self._rows(rows)

    def list_worker_options(
        self,
        *,
        tenant_id: str | None = None,
        owner_id: str | None = None,
        states: set[str] | None = None,
        exclude_states: set[str] | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        query = """
            SELECT
                workers.worker_id,
                workers.project_id,
                workers.tenant_id,
                workers.owner_id,
                workers.name,
                workers.role,
                workers.profile,
                workers.execution_mode,
                workers.alias,
                workers.state,
                workers.favorite,
                workers.last_run_id,
                workers.created_at,
                workers.updated_at,
                projects.title AS project_title,
                projects.goal AS project_goal
            FROM workers
            LEFT JOIN projects ON projects.project_id = workers.project_id
        """
        clauses: list[str] = []
        params: list[Any] = []
        if tenant_id:
            clauses.append("workers.tenant_id = ?")
            params.append(tenant_id)
        if owner_id:
            clauses.append("workers.owner_id = ?")
            params.append(owner_id)
        if states:
            placeholders = ", ".join("?" for _ in states)
            clauses.append(f"workers.state IN ({placeholders})")
            params.extend(sorted(states))
        if exclude_states:
            placeholders = ", ".join("?" for _ in exclude_states)
            clauses.append(f"workers.state NOT IN ({placeholders})")
            params.extend(sorted(exclude_states))
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY workers.favorite DESC, workers.updated_at DESC, workers.created_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 25)))
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return self._rows(rows)

    def get_worker(
        self,
        worker_id: str,
        tenant_id: str | None = None,
        owner_id: str | None = None,
    ) -> dict[str, Any] | None:
        query = "SELECT * FROM workers WHERE worker_id = ?"
        params: list[Any] = [worker_id]
        if tenant_id:
            query += " AND tenant_id = ?"
            params.append(tenant_id)
        if owner_id:
            query += " AND owner_id = ?"
            params.append(owner_id)
        with self._connect() as conn:
            row = conn.execute(query, params).fetchone()
        return self._row(row)

    def find_worker_by_alias(
        self,
        project_id: str,
        owner_id: str,
        alias: str,
        execution_mode: str | None = None,
        tenant_id: str | None = None,
    ) -> dict[str, Any] | None:
        alias_value = alias.strip()
        if not alias_value:
            return None
        query = "SELECT * FROM workers WHERE project_id = ? AND owner_id = ? AND alias = ?"
        params: list[Any] = [project_id, owner_id, alias_value]
        if tenant_id:
            query += " AND tenant_id = ?"
            params.append(tenant_id)
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

    def count_workers(
        self,
        *,
        tenant_id: str | None = None,
        owner_id: str | None = None,
        states: set[str] | None = None,
        exclude_states: set[str] | None = None,
    ) -> int:
        query = "SELECT COUNT(*) FROM workers"
        clauses: list[str] = []
        params: list[Any] = []
        if tenant_id:
            clauses.append("tenant_id = ?")
            params.append(tenant_id)
        if owner_id:
            clauses.append("owner_id = ?")
            params.append(owner_id)
        if states:
            placeholders = ", ".join("?" for _ in states)
            clauses.append(f"state IN ({placeholders})")
            params.extend(sorted(states))
        if exclude_states:
            placeholders = ", ".join("?" for _ in exclude_states)
            clauses.append(f"state NOT IN ({placeholders})")
            params.extend(sorted(exclude_states))
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        with self._connect() as conn:
            return int(conn.execute(query, params).fetchone()[0])

    def create_run(self, worker_id: str, project_id: str, instruction: str, state: str = "queued") -> dict[str, Any]:
        run_id = f"run_{uuid.uuid4().hex[:10]}"
        queued_at = utc_now()
        worker = self.get_worker(worker_id) or {}
        project = self.get_project(project_id) or {}
        tenant_id = str(worker.get("tenant_id") or project.get("tenant_id") or "local")
        data = {
            "run_id": run_id,
            "worker_id": worker_id,
            "project_id": project_id,
            "tenant_id": tenant_id,
            "instruction": instruction,
            "state": state,
            "queued_at": queued_at,
            "started_at": queued_at if state == "running" else None,
            "ended_at": None,
            "output_text": "",
            "error_text": "",
            "failure_class": "",
            "failure_retryable": 0,
            "failure_user_message": "",
            "failure_recommended_recovery": "",
            "failure_diagnostic_summary": "",
            "retry_after": None,
            "retry_attempts": 0,
            "last_retry_class": "",
        }
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (
                    run_id, worker_id, project_id, tenant_id, instruction, state, queued_at,
                    started_at, ended_at, output_text, error_text, failure_class,
                    failure_retryable, failure_user_message, failure_recommended_recovery,
                    failure_diagnostic_summary, retry_after, retry_attempts, last_retry_class
                )
                VALUES (
                    :run_id, :worker_id, :project_id, :tenant_id, :instruction, :state,
                    :queued_at, :started_at, :ended_at, :output_text, :error_text,
                    :failure_class, :failure_retryable, :failure_user_message,
                    :failure_recommended_recovery, :failure_diagnostic_summary,
                    :retry_after, :retry_attempts, :last_retry_class
                )
                """,
                data,
            )
        self.update_worker(worker_id, last_run_id=run_id)
        return data

    def list_runs_for_worker(
        self,
        worker_id: str,
        limit: int = 25,
        tenant_id: str | None = None,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM runs WHERE worker_id = ?"
        params: list[Any] = [worker_id]
        if tenant_id:
            query += " AND tenant_id = ?"
            params.append(tenant_id)
        query += " ORDER BY queued_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return self._rows(rows)

    def list_runs_for_project(
        self,
        project_id: str,
        limit: int = 50,
        tenant_id: str | None = None,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM runs WHERE project_id = ?"
        params: list[Any] = [project_id]
        if tenant_id:
            query += " AND tenant_id = ?"
            params.append(tenant_id)
        query += " ORDER BY queued_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return self._rows(rows)

    def get_run(self, run_id: str, tenant_id: str | None = None) -> dict[str, Any] | None:
        query = "SELECT * FROM runs WHERE run_id = ?"
        params: list[Any] = [run_id]
        if tenant_id:
            query += " AND tenant_id = ?"
            params.append(tenant_id)
        with self._connect() as conn:
            row = conn.execute(query, params).fetchone()
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

    def peek_next_queued_run(self, worker_id: str, now_iso: str | None = None) -> dict[str, Any] | None:
        now_iso = now_iso or utc_now()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM runs
                WHERE worker_id = ?
                  AND state = 'queued'
                  AND (retry_after IS NULL OR retry_after = '' OR retry_after <= ?)
                ORDER BY queued_at ASC
                LIMIT 1
                """,
                (worker_id, now_iso),
            ).fetchone()
        return self._row(row)

    def claim_next_queued_run(self, worker_id: str) -> dict[str, Any] | None:
        now_iso = utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT * FROM runs
                WHERE worker_id = ?
                  AND state = 'queued'
                  AND (retry_after IS NULL OR retry_after = '' OR retry_after <= ?)
                ORDER BY queued_at ASC
                LIMIT 1
                """,
                (worker_id, now_iso),
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            started_at = utc_now()
            conn.execute(
                "UPDATE runs SET state = 'running', started_at = ?, retry_after = NULL WHERE run_id = ?",
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

    def list_runs_by_state(self, state: str, limit: int = 1000) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs WHERE state = ? ORDER BY started_at ASC LIMIT ?",
                (state, max(1, int(limit))),
            ).fetchall()
        return self._rows(rows)

    def has_queued_runs(self, worker_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM runs WHERE worker_id = ? AND state = 'queued' LIMIT 1",
                (worker_id,),
            ).fetchone()
        return row is not None

    def next_retry_after_for_worker(self, worker_id: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT retry_after FROM runs
                WHERE worker_id = ?
                  AND state = 'queued'
                  AND retry_after IS NOT NULL
                  AND retry_after != ''
                ORDER BY retry_after ASC
                LIMIT 1
                """,
                (worker_id,),
            ).fetchone()
        if row is None:
            return None
        return str(row["retry_after"] or "") or None

    def requeue_run_for_retry(
        self,
        run_id: str,
        *,
        retry_after: str,
        error_text: str = "",
        last_retry_class: str = "",
        **failure_fields: Any,
    ) -> dict[str, Any] | None:
        normalized_failure_fields = _normalized_failure_fields(failure_fields)
        update_fields = {
            "run_id": run_id,
            "retry_after": retry_after,
            "error_text": error_text,
            "last_retry_class": str(last_retry_class or normalized_failure_fields.get("failure_class") or ""),
            **normalized_failure_fields,
        }
        failure_assignments = "".join(f", {key} = :{key}" for key in normalized_failure_fields.keys())
        with self._connect() as conn:
            conn.execute(
                f"""
                UPDATE runs
                SET state = 'queued',
                    ended_at = NULL,
                    retry_after = :retry_after,
                    retry_attempts = COALESCE(retry_attempts, 0) + 1,
                    last_retry_class = :last_retry_class,
                    error_text = :error_text{failure_assignments}
                WHERE run_id = :run_id
                  AND state IN ('queued', 'running')
                """,
                update_fields,
            )
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return self._row(row)

    def finalize_run(
        self,
        run_id: str,
        state: str,
        output_text: str = "",
        error_text: str = "",
        **failure_fields: Any,
    ) -> dict[str, Any] | None:
        fields = {
            "state": state,
            "ended_at": utc_now(),
            "output_text": output_text,
            "error_text": error_text,
            "retry_after": None,
        }
        fields.update(_normalized_failure_fields(failure_fields))
        return self.update_run(run_id, **fields)

    def finalize_run_if_state(
        self,
        run_id: str,
        expected_state: str,
        state: str,
        output_text: str = "",
        error_text: str = "",
        **failure_fields: Any,
    ) -> dict[str, Any] | None:
        normalized_failure_fields = _normalized_failure_fields(failure_fields)
        update_fields = {
            "state": state,
            "ended_at": utc_now(),
            "output_text": output_text,
            "error_text": error_text,
            **normalized_failure_fields,
            "run_id": run_id,
            "expected_state": expected_state,
        }
        failure_assignments = "".join(f", {key} = :{key}" for key in normalized_failure_fields.keys())
        with self._connect() as conn:
            cur = conn.execute(
                f"""
                UPDATE runs
                SET state = :state, ended_at = :ended_at, output_text = :output_text, error_text = :error_text{failure_assignments}
                WHERE run_id = :run_id AND state = :expected_state
                """,
                update_fields,
            )
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        updated = self._row(row)
        if cur.rowcount and updated:
            return updated
        return None

    def cancel_pending_runs(self, worker_id: str, error_text: str, state: str = "cancelled") -> int:
        with self._connect() as conn:
            schedule_state = "cancelled" if state == "cancelled" else "failed"
            conn.execute(
                """
                UPDATE scheduled_runs
                SET state = ?, last_error = ?, updated_at = ?
                WHERE queued_run_id IN (
                    SELECT run_id FROM runs WHERE worker_id = ? AND state IN ('queued', 'running')
                )
                  AND state IN ('queued', 'running')
                """,
                (schedule_state, error_text, utc_now(), worker_id),
            )
            cur = conn.execute(
                "UPDATE runs SET state = ?, ended_at = ?, error_text = ? WHERE worker_id = ? AND state IN ('queued', 'running')",
                (state, utc_now(), error_text, worker_id),
            )
        return cur.rowcount

    def create_scheduled_run(
        self,
        *,
        worker_id: str,
        project_id: str,
        owner_id: str,
        instruction: str,
        run_at: str,
        schedule_text: str = "",
        tenant_id: str = "local",
    ) -> dict[str, Any]:
        now = utc_now()
        data = {
            "schedule_id": f"sch_{uuid.uuid4().hex[:10]}",
            "project_id": project_id,
            "worker_id": worker_id,
            "tenant_id": tenant_id or "local",
            "owner_id": owner_id,
            "instruction": instruction,
            "schedule_text": schedule_text,
            "run_at": run_at,
            "state": "pending",
            "queued_run_id": None,
            "last_error": "",
            "created_at": now,
            "updated_at": now,
        }
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO scheduled_runs (
                    schedule_id, project_id, worker_id, tenant_id, owner_id, instruction,
                    schedule_text, run_at, state, queued_run_id, last_error, created_at, updated_at
                )
                VALUES (
                    :schedule_id, :project_id, :worker_id, :tenant_id, :owner_id, :instruction,
                    :schedule_text, :run_at, :state, :queued_run_id, :last_error, :created_at, :updated_at
                )
                """,
                data,
            )
        return data

    def get_schedule(
        self,
        schedule_id: str,
        tenant_id: str | None = None,
        owner_id: str | None = None,
    ) -> dict[str, Any] | None:
        query = "SELECT * FROM scheduled_runs WHERE schedule_id = ?"
        params: list[Any] = [schedule_id]
        if tenant_id:
            query += " AND tenant_id = ?"
            params.append(tenant_id)
        if owner_id:
            query += " AND owner_id = ?"
            params.append(owner_id)
        with self._connect() as conn:
            row = conn.execute(query, params).fetchone()
        return self._row(row)

    def list_schedules_for_worker(
        self,
        worker_id: str,
        *,
        tenant_id: str | None = None,
        owner_id: str | None = None,
        include_done: bool = False,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM scheduled_runs WHERE worker_id = ?"
        params: list[Any] = [worker_id]
        if tenant_id:
            query += " AND tenant_id = ?"
            params.append(tenant_id)
        if owner_id:
            query += " AND owner_id = ?"
            params.append(owner_id)
        if not include_done:
            query += " AND state IN ('pending', 'queued', 'running')"
        query += " ORDER BY run_at ASC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return self._rows(rows)

    def list_due_schedules(self, now_iso: str, limit: int = 25) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM scheduled_runs
                WHERE state = 'pending' AND run_at <= ?
                ORDER BY run_at ASC
                LIMIT ?
                """,
                (now_iso, limit),
            ).fetchall()
        return self._rows(rows)

    def claim_schedule(self, schedule_id: str) -> dict[str, Any] | None:
        now = utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE scheduled_runs SET state = 'running', updated_at = ? WHERE schedule_id = ? AND state = 'pending'",
                (now, schedule_id),
            )
            row = conn.execute("SELECT * FROM scheduled_runs WHERE schedule_id = ?", (schedule_id,)).fetchone()
            conn.execute("COMMIT")
        if cur.rowcount != 1:
            return None
        return self._row(row)

    def finalize_schedule(
        self,
        schedule_id: str,
        *,
        state: str,
        queued_run_id: str | None = None,
        last_error: str = "",
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE scheduled_runs
                SET state = ?, queued_run_id = ?, last_error = ?, updated_at = ?
                WHERE schedule_id = ? AND state NOT IN ('completed', 'failed', 'cancelled')
                """,
                (state, queued_run_id, last_error, utc_now(), schedule_id),
            )
            row = conn.execute("SELECT * FROM scheduled_runs WHERE schedule_id = ?", (schedule_id,)).fetchone()
        return self._row(row)

    def finalize_schedule_for_run(
        self,
        run_id: str,
        *,
        state: str,
        last_error: str = "",
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE scheduled_runs
                SET state = ?, last_error = ?, updated_at = ?
                WHERE queued_run_id = ? AND state IN ('queued', 'running')
                """,
                (state, last_error, utc_now(), run_id),
            )
            row = conn.execute("SELECT * FROM scheduled_runs WHERE queued_run_id = ?", (run_id,)).fetchone()
        return self._row(row)

    def cancel_schedule(self, schedule_id: str) -> dict[str, Any] | None:
        return self.finalize_schedule(schedule_id, state="cancelled", queued_run_id=None)

    def add_event(
        self,
        project_id: str,
        worker_id: str,
        run_id: str | None,
        event_type: str,
        message: str,
        *,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        if not tenant_id:
            worker = self.get_worker(worker_id) or {}
            project = self.get_project(project_id) or {}
            tenant_id = str(worker.get("tenant_id") or project.get("tenant_id") or "local")
        data = {
            "event_id": f"evt_{uuid.uuid4().hex[:10]}",
            "project_id": project_id,
            "worker_id": worker_id,
            "tenant_id": tenant_id,
            "run_id": run_id,
            "event_type": event_type,
            "message": message,
            "created_at": utc_now(),
        }
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO events (event_id, project_id, worker_id, tenant_id, run_id, event_type, message, created_at) VALUES (:event_id, :project_id, :worker_id, :tenant_id, :run_id, :event_type, :message, :created_at)",
                data,
            )
        return data

    def list_events(self, worker_id: str, tenant_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM events WHERE worker_id = ?"
        params: list[Any] = [worker_id]
        if tenant_id:
            query += " AND tenant_id = ?"
            params.append(tenant_id)
        query += " ORDER BY created_at ASC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return self._rows(rows)

    def list_project_events(self, project_id: str, tenant_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM events WHERE project_id = ?"
        params: list[Any] = [project_id]
        if tenant_id:
            query += " AND tenant_id = ?"
            params.append(tenant_id)
        query += " ORDER BY created_at ASC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
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
        worker = self.get_worker(worker_id) or {}
        project = self.get_project(project_id) or {}
        tenant_id = str(worker.get("tenant_id") or project.get("tenant_id") or "local")
        data = {
            "callback_id": callback_id,
            "project_id": project_id,
            "worker_id": worker_id,
            "tenant_id": tenant_id,
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
                    callback_id, project_id, worker_id, tenant_id, run_id, event_type, url, payload_json,
                    status, attempts, last_error, created_at, updated_at, delivered_at
                )
                VALUES (
                    :callback_id, :project_id, :worker_id, :tenant_id, :run_id, :event_type, :url, :payload_json,
                    :status, :attempts, :last_error, :created_at, :updated_at, :delivered_at
                )
                ON CONFLICT(callback_id) DO UPDATE SET
                    project_id = excluded.project_id,
                    worker_id = excluded.worker_id,
                    tenant_id = excluded.tenant_id,
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

    def claim_pending_callback(self, callback_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE callback_outbox
                SET status = 'delivering', updated_at = ?
                WHERE callback_id = ? AND status = 'pending'
                """,
                (utc_now(), callback_id),
            )
        return cur.rowcount == 1

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

    def mark_callback_dead_lettered(
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
                SET status = 'dead_lettered',
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

    def reclaim_stale_delivering_callbacks(self, *, stale_before: str, limit: int = 50) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE callback_outbox
                SET status = 'pending',
                    last_error = CASE
                        WHEN last_error = '' THEN 'callback delivery was interrupted and reclaimed'
                        ELSE last_error
                    END,
                    updated_at = ?
                WHERE callback_id IN (
                    SELECT callback_id
                    FROM callback_outbox
                    WHERE status = 'delivering' AND updated_at < ?
                    ORDER BY updated_at ASC
                    LIMIT ?
                )
                """,
                (utc_now(), stale_before, limit),
            )
        return cur.rowcount

    def metrics(self, tenant_id: str | None = None, owner_id: str | None = None) -> dict[str, int]:
        project_clause = ""
        worker_clause = ""
        project_params: list[Any] = []
        worker_params: list[Any] = []
        run_params: list[Any] = []
        event_params: list[Any] = []
        clauses: list[str] = []
        if tenant_id:
            clauses.append("tenant_id = ?")
            project_params.append(tenant_id)
            worker_params.append(tenant_id)
        if owner_id:
            clauses.append("owner_id = ?")
            project_params.append(owner_id)
            worker_params.append(owner_id)
        if clauses:
            project_clause = " WHERE " + " AND ".join(clauses)
            worker_clause = " WHERE " + " AND ".join(clauses)
        run_join = ""
        run_clause = ""
        event_join = ""
        event_clause = ""
        callback_join = ""
        callback_clause = ""
        callback_params: list[Any] = []
        if tenant_id or owner_id:
            run_join = " JOIN workers ON workers.worker_id = runs.worker_id"
            event_join = " JOIN workers ON workers.worker_id = events.worker_id"
            callback_join = " JOIN workers ON workers.worker_id = callback_outbox.worker_id"
            run_filters: list[str] = []
            event_filters: list[str] = []
            callback_filters: list[str] = []
            if tenant_id:
                run_filters.append("runs.tenant_id = ?")
                event_filters.append("events.tenant_id = ?")
                callback_filters.append("callback_outbox.tenant_id = ?")
                run_params.append(tenant_id)
                event_params.append(tenant_id)
                callback_params.append(tenant_id)
            if owner_id:
                run_filters.append("workers.owner_id = ?")
                event_filters.append("workers.owner_id = ?")
                callback_filters.append("workers.owner_id = ?")
                run_params.append(owner_id)
                event_params.append(owner_id)
                callback_params.append(owner_id)
            run_clause = " WHERE " + " AND ".join(run_filters)
            event_clause = " WHERE " + " AND ".join(event_filters)
            callback_clause = " WHERE " + " AND ".join(callback_filters)
        with self._connect() as conn:
            projects = conn.execute(f"SELECT COUNT(*) FROM projects{project_clause}", project_params).fetchone()[0]
            workers = conn.execute(f"SELECT COUNT(*) FROM workers{worker_clause}", worker_params).fetchone()[0]
            runs = conn.execute(f"SELECT COUNT(*) FROM runs{run_join}{run_clause}", run_params).fetchone()[0]
            queued_runs_query = f"SELECT COUNT(*) FROM runs{run_join}"
            active_runs_query = f"SELECT COUNT(*) FROM runs{run_join}"
            queued_filters = ["runs.state = 'queued'"]
            active_filters = ["runs.state = 'running'"]
            queued_params = list(run_params)
            active_params = list(run_params)
            if run_clause:
                extra = run_clause.removeprefix(" WHERE ")
                queued_filters.append(extra)
                active_filters.append(extra)
            queued_runs_query += " WHERE " + " AND ".join(queued_filters)
            active_runs_query += " WHERE " + " AND ".join(active_filters)
            queued_runs = conn.execute(queued_runs_query, queued_params).fetchone()[0]
            active_runs = conn.execute(active_runs_query, active_params).fetchone()[0]
            events = conn.execute(f"SELECT COUNT(*) FROM events{event_join}{event_clause}", event_params).fetchone()[0]
            callback_from = f"callback_outbox{callback_join}"
            callback_pending_query = f"SELECT COUNT(*) FROM {callback_from}"
            callback_delivering_query = f"SELECT COUNT(*) FROM {callback_from}"
            callback_dead_lettered_query = f"SELECT COUNT(*) FROM {callback_from}"
            callback_max_attempts_query = f"SELECT COALESCE(MAX(callback_outbox.attempts), 0) FROM {callback_from}"
            callback_oldest_pending_query = f"SELECT MIN(callback_outbox.updated_at) FROM {callback_from}"
            pending_filters = ["callback_outbox.status = 'pending'"]
            delivering_filters = ["callback_outbox.status = 'delivering'"]
            active_callback_filters = ["callback_outbox.status IN ('pending', 'delivering')"]
            dead_lettered_filters = ["callback_outbox.status = 'dead_lettered'"]
            pending_params = list(callback_params)
            delivering_params = list(callback_params)
            max_attempts_params = list(callback_params)
            dead_lettered_params = list(callback_params)
            if callback_clause:
                extra = callback_clause.removeprefix(" WHERE ")
                pending_filters.append(extra)
                delivering_filters.append(extra)
                active_callback_filters.append(extra)
                dead_lettered_filters.append(extra)
            callback_pending_query += " WHERE " + " AND ".join(pending_filters)
            callback_delivering_query += " WHERE " + " AND ".join(delivering_filters)
            callback_dead_lettered_query += " WHERE " + " AND ".join(dead_lettered_filters)
            callback_oldest_pending_query += " WHERE " + " AND ".join(pending_filters)
            callback_max_attempts_query += " WHERE " + " AND ".join(active_callback_filters)
            callback_pending = conn.execute(callback_pending_query, pending_params).fetchone()[0]
            callback_delivering = conn.execute(callback_delivering_query, delivering_params).fetchone()[0]
            callback_dead_lettered = conn.execute(callback_dead_lettered_query, dead_lettered_params).fetchone()[0]
            callback_max_attempts = conn.execute(callback_max_attempts_query, max_attempts_params).fetchone()[0]
            oldest_pending = conn.execute(callback_oldest_pending_query, pending_params).fetchone()[0]
        callback_oldest_pending_age_seconds = 0
        if oldest_pending:
            try:
                oldest_dt = datetime.fromisoformat(str(oldest_pending))
                if oldest_dt.tzinfo is None:
                    oldest_dt = oldest_dt.replace(tzinfo=timezone.utc)
                callback_oldest_pending_age_seconds = max(
                    0,
                    int((datetime.now(timezone.utc) - oldest_dt).total_seconds()),
                )
            except ValueError:
                callback_oldest_pending_age_seconds = 0
        return {
            "projects": projects,
            "workers": workers,
            "runs": runs,
            "queued_runs": queued_runs,
            "active_runs": active_runs,
            "events": events,
            "callback_pending": callback_pending,
            "callback_delivering": callback_delivering,
            "callback_dead_lettered": callback_dead_lettered,
            "callback_max_attempts": callback_max_attempts,
            "callback_oldest_pending_age_seconds": callback_oldest_pending_age_seconds,
        }
