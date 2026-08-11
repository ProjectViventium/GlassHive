from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .models import WorkspaceKind, normalize_workspace_kind, normalize_workspace_tags, utc_now
from .recurrence import canonical_recurrence_owner
from .schema_version import (
    begin_schema_migration,
    execute_schema_script,
    record_schema_version,
    require_compatible_schema,
)
from .state_permissions import ensure_state_directory, secure_state_file


RUNTIME_STORE_SCHEMA_VERSION = 4


class SchedulePrincipalAuthorityStoreError(ValueError):
    """A write lost the race with principal schedule-authority revocation."""


class WorkerClosedStoreError(ValueError):
    """A run reservation lost the race with permanent workspace closure."""


def _workspace_tags_json(values: list[str] | tuple[str, ...] | None) -> str:
    return json.dumps(normalize_workspace_tags(values), ensure_ascii=False, separators=(",", ":"))


_FAILURE_FIELD_NAMES = {
    "failure_class",
    "failure_retryable",
    "failure_user_message",
    "failure_recommended_recovery",
    "failure_diagnostic_summary",
}

_TOKEN_USAGE_FIELD_NAMES = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


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


def _normalized_token_usage(usage: dict[str, Any] | None) -> dict[str, int]:
    normalized: dict[str, int] = {}
    for key in _TOKEN_USAGE_FIELD_NAMES:
        value = (usage or {}).get(key, 0)
        if isinstance(value, bool):
            value = 0
        try:
            parsed = int(value)
        except (TypeError, ValueError, OverflowError):
            parsed = 0
        normalized[key] = max(0, parsed)
    return normalized


class Store:
    def __init__(self, db_path: str) -> None:
        self.db_path = Path(db_path)
        ensure_state_directory(self.db_path.parent)
        self._init_db()
        self._secure_state_files()

    def _secure_state_files(self) -> None:
        if os.name == "nt":
            return
        for path in (self.db_path, Path(f"{self.db_path}-wal"), Path(f"{self.db_path}-shm")):
            secure_state_file(path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        self._secure_state_files()
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            begin_schema_migration(conn)
            require_compatible_schema(
                conn,
                component="runtime_store",
                target_version=RUNTIME_STORE_SCHEMA_VERSION,
            )
            execute_schema_script(
                conn,
                """
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
                    workspace_kind TEXT NOT NULL DEFAULT 'legacy',
                    workspace_tags_json TEXT NOT NULL DEFAULT '[]',
                    duplication_report_json TEXT NOT NULL DEFAULT '{}',
                    compute_released_at TEXT,
                    pid INTEGER,
                    last_run_id TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES projects(project_id)
                );

                CREATE TABLE IF NOT EXISTS workspace_gc_tombstones (
                    worker_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    profile TEXT NOT NULL,
                    execution_mode TEXT NOT NULL,
                    workspace_kind TEXT NOT NULL,
                    original_state TEXT NOT NULL,
                    original_updated_at TEXT NOT NULL,
                    state_dir TEXT NOT NULL DEFAULT '',
                    workspace_dir TEXT NOT NULL DEFAULT '',
                    workspace_root TEXT NOT NULL DEFAULT '',
                    managed_storage_root TEXT NOT NULL DEFAULT '',
                    phase TEXT NOT NULL,
                    claim_token TEXT NOT NULL,
                    claim_expires_at REAL NOT NULL,
                    cleanup_attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    claimed_at REAL NOT NULL,
                    metadata_deleted_at REAL,
                    completed_at REAL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_workspace_gc_tombstones_phase
                    ON workspace_gc_tombstones(phase, claim_expires_at, updated_at);

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
                    runtime_bundle_json TEXT,
                    retry_after TEXT,
                    retry_attempts INTEGER NOT NULL DEFAULT 0,
                    last_retry_class TEXT NOT NULL DEFAULT '',
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_read_input_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
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

                CREATE TABLE IF NOT EXISTS recurring_schedule_definitions (
                    definition_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    worker_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL DEFAULT 'local',
                    owner_id TEXT NOT NULL,
                    scheduler_owner TEXT NOT NULL,
                    instruction TEXT NOT NULL,
                    schedule_text TEXT NOT NULL DEFAULT '',
                    recurrence_type TEXT NOT NULL,
                    interval_seconds INTEGER,
                    local_time TEXT NOT NULL DEFAULT '',
                    timezone_name TEXT NOT NULL,
                    dst_policy TEXT NOT NULL,
                    cron_expression TEXT NOT NULL DEFAULT '',
                    rrule TEXT NOT NULL DEFAULT '',
                    starts_at TEXT,
                    ends_at TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    overlap_policy TEXT NOT NULL DEFAULT 'skip',
                    misfire_grace_seconds INTEGER NOT NULL DEFAULT 300,
                    catch_up_policy TEXT NOT NULL DEFAULT 'coalesce',
                    max_catch_up_occurrences INTEGER NOT NULL DEFAULT 1,
                    jitter_seconds INTEGER NOT NULL DEFAULT 0,
                    next_run_at TEXT NOT NULL,
                    last_occurrence_at TEXT,
                    retired_at TEXT,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(worker_id) REFERENCES workers(worker_id),
                    FOREIGN KEY(project_id) REFERENCES projects(project_id)
                );

                CREATE TABLE IF NOT EXISTS recurring_schedule_occurrences (
                    occurrence_id TEXT PRIMARY KEY,
                    definition_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL DEFAULT 'local',
                    owner_id TEXT NOT NULL,
                    scheduled_for TEXT NOT NULL,
                    detected_at TEXT NOT NULL,
                    scheduled_run_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL DEFAULT 'pending',
                    outcome TEXT NOT NULL DEFAULT 'pending',
                    claimant TEXT NOT NULL DEFAULT '',
                    claimed_at TEXT,
                    claim_expires_at TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    terminal_at TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(definition_id, scheduled_for),
                    UNIQUE(scheduled_run_id),
                    FOREIGN KEY(definition_id) REFERENCES recurring_schedule_definitions(definition_id),
                    FOREIGN KEY(scheduled_run_id) REFERENCES scheduled_runs(schedule_id)
                );

                CREATE TABLE IF NOT EXISTS schedule_principal_authority (
                    tenant_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    authority_epoch INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, owner_id)
                );

                CREATE TABLE IF NOT EXISTS internal_assertion_replay_cache (
                    assertion_fingerprint TEXT PRIMARY KEY,
                    expires_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_internal_assertion_replay_expiry
                    ON internal_assertion_replay_cache(expires_at);

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

                CREATE TABLE IF NOT EXISTS provider_sessions (
                    session_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL DEFAULT 'local',
                    owner_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    worker_id TEXT NOT NULL,
                    workspace_dir TEXT NOT NULL,
                    access_mode TEXT NOT NULL,
                    history_count INTEGER NOT NULL DEFAULT 0,
                    context_manifest_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (tenant_id, owner_id, conversation_id, agent_id),
                    FOREIGN KEY(project_id) REFERENCES projects(project_id),
                    FOREIGN KEY(worker_id) REFERENCES workers(worker_id)
                );

                CREATE TABLE IF NOT EXISTS provider_requests (
                    request_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL DEFAULT 'local',
                    owner_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    run_id TEXT,
                    idempotency_key TEXT NOT NULL,
                    message_id TEXT NOT NULL DEFAULT '',
                    stream_id TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL,
                    requested_history_count INTEGER NOT NULL DEFAULT 0,
                    response_json TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (tenant_id, owner_id, idempotency_key),
                    FOREIGN KEY(session_id) REFERENCES provider_sessions(session_id),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );

                CREATE TABLE IF NOT EXISTS provider_activity (
                    sequence_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(request_id) REFERENCES provider_requests(request_id)
                );

                CREATE INDEX IF NOT EXISTS idx_workers_project_id ON workers(project_id);
                CREATE INDEX IF NOT EXISTS idx_runs_worker_state ON runs(worker_id, state, queued_at);
                CREATE INDEX IF NOT EXISTS idx_events_worker_created ON events(worker_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_callback_outbox_status_updated ON callback_outbox(status, updated_at);
                CREATE INDEX IF NOT EXISTS idx_scheduled_runs_state_run_at ON scheduled_runs(state, run_at);
                CREATE INDEX IF NOT EXISTS idx_recurring_definitions_due
                    ON recurring_schedule_definitions(scheduler_owner, active, next_run_at);
                CREATE INDEX IF NOT EXISTS idx_recurring_definitions_scope
                    ON recurring_schedule_definitions(tenant_id, owner_id, worker_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_recurring_occurrences_scope
                    ON recurring_schedule_occurrences(tenant_id, owner_id, definition_id, scheduled_for DESC);
                CREATE INDEX IF NOT EXISTS idx_provider_sessions_owner ON provider_sessions(tenant_id, owner_id, updated_at);
                CREATE INDEX IF NOT EXISTS idx_provider_requests_session ON provider_requests(session_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_provider_activity_request ON provider_activity(request_id, sequence_id);
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
            if "workspace_kind" not in worker_columns:
                conn.execute("ALTER TABLE workers ADD COLUMN workspace_kind TEXT NOT NULL DEFAULT 'legacy'")
            if "workspace_tags_json" not in worker_columns:
                conn.execute("ALTER TABLE workers ADD COLUMN workspace_tags_json TEXT NOT NULL DEFAULT '[]'")
            if "duplication_report_json" not in worker_columns:
                conn.execute("ALTER TABLE workers ADD COLUMN duplication_report_json TEXT NOT NULL DEFAULT '{}'")
            if "compute_released_at" not in worker_columns:
                conn.execute("ALTER TABLE workers ADD COLUMN compute_released_at TEXT")
            gc_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(workspace_gc_tombstones)").fetchall()
            }
            for column, definition in {
                "state_dir": "TEXT NOT NULL DEFAULT ''",
                "workspace_dir": "TEXT NOT NULL DEFAULT ''",
                "workspace_root": "TEXT NOT NULL DEFAULT ''",
            }.items():
                if column not in gc_columns:
                    conn.execute(f"ALTER TABLE workspace_gc_tombstones ADD COLUMN {column} {definition}")
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
            if "runtime_bundle_json" not in run_columns:
                conn.execute("ALTER TABLE runs ADD COLUMN runtime_bundle_json TEXT")
            if "retry_after" not in run_columns:
                conn.execute("ALTER TABLE runs ADD COLUMN retry_after TEXT")
            if "retry_attempts" not in run_columns:
                conn.execute("ALTER TABLE runs ADD COLUMN retry_attempts INTEGER NOT NULL DEFAULT 0")
            if "last_retry_class" not in run_columns:
                conn.execute("ALTER TABLE runs ADD COLUMN last_retry_class TEXT NOT NULL DEFAULT ''")
            for token_field in _TOKEN_USAGE_FIELD_NAMES:
                if token_field not in run_columns:
                    conn.execute(f"ALTER TABLE runs ADD COLUMN {token_field} INTEGER NOT NULL DEFAULT 0")
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
            recurrence_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(recurring_schedule_definitions)").fetchall()
            }
            recurrence_additions = {
                "cron_expression": "TEXT NOT NULL DEFAULT ''",
                "rrule": "TEXT NOT NULL DEFAULT ''",
                "starts_at": "TEXT",
                "ends_at": "TEXT",
                "enabled": "INTEGER NOT NULL DEFAULT 1",
                "overlap_policy": "TEXT NOT NULL DEFAULT 'skip'",
                "misfire_grace_seconds": "INTEGER NOT NULL DEFAULT 300",
                "catch_up_policy": "TEXT NOT NULL DEFAULT 'coalesce'",
                "max_catch_up_occurrences": "INTEGER NOT NULL DEFAULT 1",
                "jitter_seconds": "INTEGER NOT NULL DEFAULT 0",
                "retired_at": "TEXT",
            }
            for column, definition in recurrence_additions.items():
                if column not in recurrence_columns:
                    conn.execute(
                        f"ALTER TABLE recurring_schedule_definitions ADD COLUMN {column} {definition}"
                    )
            occurrence_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(recurring_schedule_occurrences)").fetchall()
            }
            occurrence_additions = {
                "idempotency_key": "TEXT NOT NULL DEFAULT ''",
                "state": "TEXT NOT NULL DEFAULT 'pending'",
                "outcome": "TEXT NOT NULL DEFAULT 'pending'",
                "claimant": "TEXT NOT NULL DEFAULT ''",
                "claimed_at": "TEXT",
                "claim_expires_at": "TEXT",
                "attempt_count": "INTEGER NOT NULL DEFAULT 0",
                "terminal_at": "TEXT",
            }
            for column, definition in occurrence_additions.items():
                if column not in occurrence_columns:
                    conn.execute(
                        f"ALTER TABLE recurring_schedule_occurrences ADD COLUMN {column} {definition}"
                    )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_recurring_occurrences_idempotency "
                "ON recurring_schedule_occurrences(idempotency_key) WHERE idempotency_key <> ''"
            )
            preferences_columns = {row["name"] for row in conn.execute("PRAGMA table_info(user_preferences)").fetchall()}
            if "codex_reasoning_effort" not in preferences_columns:
                conn.execute("ALTER TABLE user_preferences ADD COLUMN codex_reasoning_effort TEXT NOT NULL DEFAULT ''")
            if "claude_effort" not in preferences_columns:
                conn.execute("ALTER TABLE user_preferences ADD COLUMN claude_effort TEXT NOT NULL DEFAULT ''")
            if "openclaw_effort" not in preferences_columns:
                conn.execute("ALTER TABLE user_preferences ADD COLUMN openclaw_effort TEXT NOT NULL DEFAULT ''")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_workers_workspace_catalog "
                "ON workers(tenant_id, owner_id, workspace_kind, favorite DESC, updated_at DESC, worker_id DESC)"
            )
            record_schema_version(
                conn,
                component="runtime_store",
                version=RUNTIME_STORE_SCHEMA_VERSION,
            )

    def consume_internal_assertion_jti(
        self,
        *,
        tenant_id: str,
        issuer: str,
        jti: str,
        expires_at: int,
        now: int,
    ) -> bool:
        """Atomically consume a signed gateway assertion across runtime processes."""

        fingerprint = hashlib.sha256(
            f"{tenant_id}\0{issuer}\0{jti}".encode("utf-8")
        ).hexdigest()
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM internal_assertion_replay_cache WHERE expires_at < ?",
                (now,),
            )
            inserted = conn.execute(
                """
                INSERT OR IGNORE INTO internal_assertion_replay_cache (
                    assertion_fingerprint,
                    expires_at
                ) VALUES (?, ?)
                """,
                (fingerprint, expires_at),
            )
            return inserted.rowcount == 1

    def get_provider_session(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        conversation_id: str,
        agent_id: str,
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM provider_sessions
                WHERE tenant_id = ? AND owner_id = ? AND conversation_id = ? AND agent_id = ?
                """,
                (tenant_id or "local", owner_id, conversation_id, agent_id),
            ).fetchone()
        return self._row(row)

    def get_provider_session_by_id(self, session_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM provider_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return self._row(row)

    def list_provider_sessions(
        self,
        *,
        tenant_id: str | None = None,
        owner_id: str | None = None,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM provider_sessions"
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
        query += " ORDER BY updated_at DESC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return self._rows(rows)

    def upsert_provider_session(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        conversation_id: str,
        agent_id: str,
        model_id: str,
        project_id: str,
        worker_id: str,
        workspace_dir: str,
        access_mode: str,
        history_count: int = 0,
        context_manifest: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        existing = self.get_provider_session(
            tenant_id=tenant_id,
            owner_id=owner_id,
            conversation_id=conversation_id,
            agent_id=agent_id,
        )
        session_id = str(existing.get("session_id") or "") if existing else f"ghs_{uuid.uuid4().hex}"
        data = {
            "session_id": session_id,
            "tenant_id": tenant_id or "local",
            "owner_id": owner_id,
            "conversation_id": conversation_id,
            "agent_id": agent_id,
            "model_id": model_id,
            "project_id": project_id,
            "worker_id": worker_id,
            "workspace_dir": workspace_dir,
            "access_mode": access_mode,
            "history_count": max(0, int(history_count)),
            "context_manifest_json": json.dumps(context_manifest or {}, sort_keys=True),
            "created_at": str(existing.get("created_at") or now) if existing else now,
            "updated_at": now,
        }
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(
                """
                SELECT 1 FROM workspace_gc_tombstones
                WHERE worker_id = ? AND phase IN ('claimed', 'cleanup_pending') LIMIT 1
                """,
                (worker_id,),
            ).fetchone() is not None:
                raise RuntimeError("Workspace is being garbage-collected")
            conn.execute(
                """
                INSERT INTO provider_sessions (
                    session_id, tenant_id, owner_id, conversation_id, agent_id, model_id,
                    project_id, worker_id, workspace_dir, access_mode, history_count,
                    context_manifest_json, created_at, updated_at
                ) VALUES (
                    :session_id, :tenant_id, :owner_id, :conversation_id, :agent_id, :model_id,
                    :project_id, :worker_id, :workspace_dir, :access_mode, :history_count,
                    :context_manifest_json, :created_at, :updated_at
                )
                ON CONFLICT(tenant_id, owner_id, conversation_id, agent_id) DO UPDATE SET
                    session_id = excluded.session_id,
                    model_id = excluded.model_id,
                    project_id = excluded.project_id,
                    worker_id = excluded.worker_id,
                    workspace_dir = excluded.workspace_dir,
                    access_mode = excluded.access_mode,
                    history_count = excluded.history_count,
                    context_manifest_json = excluded.context_manifest_json,
                    updated_at = excluded.updated_at
                """,
                data,
            )
            row = conn.execute("SELECT * FROM provider_sessions WHERE session_id = ?", (session_id,)).fetchone()
        return dict(row)

    def update_provider_session_history(
        self,
        session_id: str,
        *,
        history_count: int,
        context_manifest: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT history_count FROM provider_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            prior_count = int(existing["history_count"] or 0) if existing else 0
            effective_count = max(prior_count, max(0, int(history_count)))
            fields: dict[str, Any] = {
                "history_count": effective_count,
                "updated_at": utc_now(),
            }
            if context_manifest is not None:
                manifest = dict(context_manifest)
                manifest["messages"] = effective_count
                fields["context_manifest_json"] = json.dumps(manifest, sort_keys=True)
            assignments = ", ".join(f"{key} = :{key}" for key in fields)
            fields["session_id"] = session_id
            conn.execute(f"UPDATE provider_sessions SET {assignments} WHERE session_id = :session_id", fields)
            row = conn.execute("SELECT * FROM provider_sessions WHERE session_id = ?", (session_id,)).fetchone()
        return self._row(row)

    def get_provider_request(
        self,
        request_id: str | None = None,
        *,
        tenant_id: str | None = None,
        owner_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any] | None:
        if request_id:
            query = "SELECT * FROM provider_requests WHERE request_id = ?"
            params: list[Any] = [request_id]
        elif idempotency_key:
            query = "SELECT * FROM provider_requests WHERE idempotency_key = ?"
            params = [idempotency_key]
        else:
            return None
        if tenant_id:
            query += " AND tenant_id = ?"
            params.append(tenant_id)
        if owner_id:
            query += " AND owner_id = ?"
            params.append(owner_id)
        with self._connect() as conn:
            row = conn.execute(query, params).fetchone()
        return self._row(row)

    def list_provider_requests_by_state(
        self,
        states: set[str],
        *,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        clean_states = sorted({str(state or "").strip() for state in states if str(state or "").strip()})
        if not clean_states:
            return []
        placeholders = ",".join("?" for _ in clean_states)
        bounded_limit = max(1, min(int(limit), 5000))
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM provider_requests
                WHERE state IN ({placeholders})
                ORDER BY created_at ASC
                LIMIT ?
                """,
                [*clean_states, bounded_limit],
            ).fetchall()
        return self._rows(rows)

    def create_provider_request(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        session_id: str,
        idempotency_key: str,
        message_id: str,
        stream_id: str,
        requested_history_count: int,
    ) -> tuple[dict[str, Any], bool]:
        existing = self.get_provider_request(
            tenant_id=tenant_id,
            owner_id=owner_id,
            idempotency_key=idempotency_key,
        )
        if existing:
            return existing, False
        now = utc_now()
        data = {
            "request_id": f"chatcmpl-gh-{uuid.uuid4().hex}",
            "tenant_id": tenant_id or "local",
            "owner_id": owner_id,
            "session_id": session_id,
            "run_id": None,
            "idempotency_key": idempotency_key,
            "message_id": message_id,
            "stream_id": stream_id,
            "state": "queued",
            "requested_history_count": max(0, int(requested_history_count)),
            "response_json": "",
            "created_at": now,
            "updated_at": now,
        }
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO provider_requests (
                        request_id, tenant_id, owner_id, session_id, run_id, idempotency_key,
                        message_id, stream_id, state, requested_history_count, response_json,
                        created_at, updated_at
                    ) VALUES (
                        :request_id, :tenant_id, :owner_id, :session_id, :run_id, :idempotency_key,
                        :message_id, :stream_id, :state, :requested_history_count, :response_json,
                        :created_at, :updated_at
                    )
                    """,
                    data,
                )
        except sqlite3.IntegrityError:
            raced = self.get_provider_request(
                tenant_id=tenant_id,
                owner_id=owner_id,
                idempotency_key=idempotency_key,
            )
            if raced:
                return raced, False
            raise
        return data, True

    def update_provider_request(self, request_id: str, **fields: Any) -> dict[str, Any] | None:
        if not fields:
            return self.get_provider_request(request_id)
        fields["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = :{key}" for key in fields)
        fields["request_id"] = request_id
        with self._connect() as conn:
            conn.execute(f"UPDATE provider_requests SET {assignments} WHERE request_id = :request_id", fields)
            row = conn.execute("SELECT * FROM provider_requests WHERE request_id = ?", (request_id,)).fetchone()
        return self._row(row)

    def add_provider_activity(
        self,
        request_id: str,
        event_type: str,
        summary: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = {
            "request_id": request_id,
            "event_type": event_type,
            "summary": summary,
            "payload_json": json.dumps(payload or {}, sort_keys=True),
            "created_at": utc_now(),
        }
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO provider_activity (request_id, event_type, summary, payload_json, created_at)
                VALUES (:request_id, :event_type, :summary, :payload_json, :created_at)
                """,
                data,
            )
            row = conn.execute(
                "SELECT * FROM provider_activity WHERE sequence_id = ?",
                (cursor.lastrowid,),
            ).fetchone()
        return dict(row)

    def list_provider_activity(self, request_id: str, *, after_sequence: int = 0) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM provider_activity
                WHERE request_id = ? AND sequence_id > ?
                ORDER BY sequence_id ASC
                """,
                (request_id, max(0, int(after_sequence))),
            ).fetchall()
        return self._rows(rows)

    def prune_terminal_provider_requests(self, *, updated_before: str) -> int:
        with self._connect() as conn:
            request_rows = conn.execute(
                """
                SELECT request_id FROM provider_requests
                WHERE state IN ('completed', 'failed', 'cancelled') AND updated_at < ?
                """,
                (updated_before,),
            ).fetchall()
            request_ids = [str(row["request_id"]) for row in request_rows]
            if not request_ids:
                return 0
            placeholders = ",".join("?" for _ in request_ids)
            conn.execute(
                f"DELETE FROM provider_activity WHERE request_id IN ({placeholders})",
                request_ids,
            )
            conn.execute(
                f"DELETE FROM provider_requests WHERE request_id IN ({placeholders})",
                request_ids,
            )
        return len(request_ids)

    def list_stale_provider_sessions(self, *, updated_before: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT session.* FROM provider_sessions AS session
                WHERE session.updated_at < ?
                  AND NOT EXISTS (
                    SELECT 1 FROM provider_requests AS request
                    WHERE request.session_id = session.session_id
                  )
                ORDER BY session.updated_at ASC
                """,
                (updated_before,),
            ).fetchall()
        return self._rows(rows)

    def delete_provider_sessions(self, session_ids: list[str]) -> int:
        clean_ids = [str(value).strip() for value in session_ids if str(value).strip()]
        if not clean_ids:
            return 0
        placeholders = ",".join("?" for _ in clean_ids)
        with self._connect() as conn:
            cursor = conn.execute(
                f"DELETE FROM provider_sessions WHERE session_id IN ({placeholders})",
                clean_ids,
            )
        return int(cursor.rowcount or 0)

    def _row(self, value: sqlite3.Row | None) -> dict[str, Any] | None:
        if value is None:
            return None
        data = dict(value)
        if "workspace_tags_json" in data:
            try:
                parsed_tags = json.loads(str(data.get("workspace_tags_json") or "[]"))
            except json.JSONDecodeError:
                parsed_tags = []
            data["tags"] = normalize_workspace_tags(parsed_tags if isinstance(parsed_tags, list) else [])
            data["workspace_kind"] = normalize_workspace_kind(data.get("workspace_kind"))
            data.setdefault("last_activity_at", str(data.get("updated_at") or ""))
        if "duplication_report_json" in data:
            try:
                parsed_report = json.loads(str(data.pop("duplication_report_json") or "{}"))
            except json.JSONDecodeError:
                parsed_report = {}
            if isinstance(parsed_report, dict) and parsed_report:
                data["duplication_report"] = parsed_report
        return data

    def _rows(self, values: list[sqlite3.Row]) -> list[dict[str, Any]]:
        return [row for value in values if (row := self._row(value)) is not None]

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
        project_id: str | None = None,
    ) -> dict[str, Any]:
        project_id = str(project_id or f"prj_{uuid.uuid4().hex[:10]}")
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

    def delete_project_if_empty(self, project_id: str, *, tenant_id: str, owner_id: str) -> bool:
        """Remove a newly-created project only when no worker or dependent state can reference it."""

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                DELETE FROM projects
                WHERE project_id = ? AND tenant_id = ? AND owner_id = ?
                  AND NOT EXISTS (SELECT 1 FROM workers WHERE workers.project_id = projects.project_id)
                """,
                (project_id, tenant_id, owner_id),
            )
        return cursor.rowcount == 1

    def delete_unstarted_worker(
        self,
        worker_id: str,
        *,
        project_id: str,
        tenant_id: str,
        owner_id: str,
    ) -> bool:
        """Roll back a worker that never accepted work or acquired user-scoped state."""

        allowed_event_types = {
            "worker.created",
            "worker.prepared",
            "worker.failed",
            "worker.duplicate_failed",
        }
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            worker = conn.execute(
                """
                SELECT state, pid, last_run_id FROM workers
                WHERE worker_id = ? AND project_id = ? AND tenant_id = ? AND owner_id = ?
                """,
                (worker_id, project_id, tenant_id or "local", owner_id),
            ).fetchone()
            if worker is None:
                return True
            if (
                str(worker["state"] or "") not in {"created", "paused", "failed", "terminated"}
                or worker["pid"] is not None
                or str(worker["last_run_id"] or "").strip()
            ):
                return False

            unexpected_event = conn.execute(
                f"""
                SELECT 1 FROM events
                WHERE worker_id = ? AND event_type NOT IN ({', '.join('?' for _ in allowed_event_types)})
                LIMIT 1
                """,
                (worker_id, *sorted(allowed_event_types)),
            ).fetchone()
            if unexpected_event is not None:
                return False

            runtime_dependencies = (
                "runs",
                "callback_outbox",
                "scheduled_runs",
                "recurring_schedule_definitions",
                "provider_sessions",
            )
            for table in runtime_dependencies:
                if conn.execute(
                    f"SELECT 1 FROM {table} WHERE worker_id = ? LIMIT 1",
                    (worker_id,),
                ).fetchone() is not None:
                    return False

            available_tables = {
                str(row["name"])
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
            }
            control_plane_dependencies = (
                ("provider_account_leases", "worker_id"),
                ("workspace_capability_grants", "worker_id"),
                ("control_plane_pending_changes", "target_id"),
            )
            for table, column in control_plane_dependencies:
                if table in available_tables and conn.execute(
                    f"SELECT 1 FROM {table} WHERE {column} = ? LIMIT 1",
                    (worker_id,),
                ).fetchone() is not None:
                    return False

            conn.execute("DELETE FROM events WHERE worker_id = ?", (worker_id,))
            deleted = conn.execute(
                """
                DELETE FROM workers
                WHERE worker_id = ? AND project_id = ? AND tenant_id = ? AND owner_id = ?
                """,
                (worker_id, project_id, tenant_id or "local", owner_id),
            )
        return deleted.rowcount == 1

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
        workspace_kind: WorkspaceKind | str = "legacy",
        tags: list[str] | None = None,
        duplication_report: dict[str, Any] | None = None,
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
            "workspace_kind": normalize_workspace_kind(workspace_kind),
            "workspace_tags_json": _workspace_tags_json(tags),
            "duplication_report_json": json.dumps(
                duplication_report or {},
                sort_keys=True,
                separators=(",", ":"),
            ),
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
                    state_dir, workspace_dir, workspace_root, favorite, workspace_kind, workspace_tags_json,
                    duplication_report_json, compute_released_at, pid, last_run_id, last_error, created_at, updated_at
                ) VALUES (
                    :worker_id, :project_id, :tenant_id, :owner_id, :name, :role, :profile, :backend, :execution_mode, :alias, :runtime, :model, :state,
                    :bootstrap_profile, :bootstrap_bundle_json, :gateway_url, :takeover_url, :control_url, :gateway_port, :gateway_token, :session_key,
                    :state_dir, :workspace_dir, :workspace_root, :favorite, :workspace_kind, :workspace_tags_json,
                    :duplication_report_json, :compute_released_at, :pid, :last_run_id, :last_error, :created_at, :updated_at
                )
                """,
                data,
            )
        self.add_event(project_id, worker_id, None, "worker.created", f"Worker {name} created", tenant_id=tenant_id)
        return {
            **data,
            "tags": normalize_workspace_tags(tags),
            "last_activity_at": now,
        }

    def list_workspace_catalog(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        workspace_kinds: set[str] | None = None,
        search: str = "",
        tags: list[str] | None = None,
        favorite: bool | None = None,
        cursor_favorite: int | None = None,
        cursor_activity_at: str = "",
        cursor_worker_id: str = "",
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        query = """
            SELECT
                workers.*,
                workers.updated_at AS last_activity_at,
                projects.title AS project_title,
                projects.goal AS project_goal
            FROM workers
            LEFT JOIN projects ON projects.project_id = workers.project_id
        """
        clauses = ["workers.tenant_id = ?", "workers.owner_id = ?"]
        params: list[Any] = [tenant_id or "local", owner_id]
        normalized_kinds = {normalize_workspace_kind(value) for value in workspace_kinds or set()}
        if normalized_kinds:
            placeholders = ", ".join("?" for _ in normalized_kinds)
            clauses.append(f"workers.workspace_kind IN ({placeholders})")
            params.extend(sorted(normalized_kinds))
        clean_search = str(search or "").strip().casefold()
        if clean_search:
            clauses.append(
                "(instr(lower(workers.name), ?) > 0 "
                "OR instr(lower(COALESCE(workers.alias, '')), ?) > 0 "
                "OR instr(lower(COALESCE(projects.title, '')), ?) > 0 "
                "OR instr(lower(COALESCE(projects.goal, '')), ?) > 0)"
            )
            params.extend([clean_search] * 4)
        for tag in normalize_workspace_tags(tags):
            clauses.append("instr(workers.workspace_tags_json, ?) > 0")
            params.append(json.dumps(tag, ensure_ascii=False))
        if favorite is not None:
            clauses.append("workers.favorite = ?")
            params.append(1 if favorite else 0)
        if cursor_favorite is not None:
            if cursor_favorite not in {0, 1} or not cursor_activity_at or not cursor_worker_id:
                raise ValueError("workspace catalog cursor is invalid")
            clauses.append(
                "(workers.favorite < ? "
                "OR (workers.favorite = ? AND workers.updated_at < ?) "
                "OR (workers.favorite = ? AND workers.updated_at = ? AND workers.worker_id < ?))"
            )
            params.extend(
                [
                    cursor_favorite,
                    cursor_favorite,
                    cursor_activity_at,
                    cursor_favorite,
                    cursor_activity_at,
                    cursor_worker_id,
                ]
            )
        query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY workers.favorite DESC, workers.updated_at DESC, workers.worker_id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 101)))
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return self._rows(rows)

    def list_ephemeral_workspace_gc_candidates(
        self,
        *,
        updated_before: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return bounded, inactive ephemeral candidates for the lifecycle reaper.

        Eligibility is rechecked transactionally by ``claim_ephemeral_workspace_gc``. This
        first pass deliberately excludes workers with live/queued work or future schedules
        so the reaper does not wake or race an active one-off workspace.
        """

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT workers.*
                FROM workers
                WHERE workers.workspace_kind = 'ephemeral'
                  AND workers.updated_at < ?
                  AND workers.state NOT IN ('created', 'starting', 'running')
                  AND NOT EXISTS (
                    SELECT 1 FROM workspace_gc_tombstones
                    WHERE workspace_gc_tombstones.worker_id = workers.worker_id
                      AND workspace_gc_tombstones.phase <> 'completed'
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM runs
                    WHERE runs.worker_id = workers.worker_id
                      AND runs.state IN ('queued', 'running', 'paused')
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM scheduled_runs
                    WHERE scheduled_runs.worker_id = workers.worker_id
                      AND scheduled_runs.state IN (
                        'pending', 'claimed', 'running', 'queued', 'retryable', 'action_required'
                      )
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM recurring_schedule_definitions
                    WHERE recurring_schedule_definitions.worker_id = workers.worker_id
                      AND recurring_schedule_definitions.active = 1
                  )
                ORDER BY workers.updated_at ASC, workers.worker_id ASC
                LIMIT ?
                """,
                (updated_before, max(1, min(int(limit), 200))),
            ).fetchall()
        return self._rows(rows)

    def next_scheduled_occurrence_by_worker(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        worker_ids: list[str],
    ) -> dict[str, str]:
        """Return the next locally persisted one-time/recurring occurrence per catalog worker."""

        normalized_ids = list(
            dict.fromkeys(str(worker_id or "").strip() for worker_id in worker_ids if str(worker_id or "").strip())
        )[:200]
        if not normalized_ids:
            return {}
        placeholders = ",".join("?" for _ in normalized_ids)
        params: list[Any] = [tenant_id or "local", owner_id, *normalized_ids]
        params.extend([tenant_id or "local", owner_id, *normalized_ids])
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT worker_id, MIN(next_at) AS next_at
                FROM (
                    SELECT worker_id, run_at AS next_at
                    FROM scheduled_runs
                    WHERE tenant_id = ? AND owner_id = ?
                      AND worker_id IN ({placeholders})
                      AND state IN ('pending', 'claimed', 'queued', 'retryable', 'action_required')
                    UNION ALL
                    SELECT worker_id, next_run_at AS next_at
                    FROM recurring_schedule_definitions
                    WHERE tenant_id = ? AND owner_id = ?
                      AND worker_id IN ({placeholders})
                      AND active = 1
                )
                GROUP BY worker_id
                """,
                params,
            ).fetchall()
        return {
            str(row["worker_id"]): str(row["next_at"] or "")
            for row in rows
            if str(row["worker_id"] or "").strip() and str(row["next_at"] or "").strip()
        }

    @staticmethod
    def _ephemeral_gc_has_blocker(
        conn: sqlite3.Connection,
        *,
        worker_id: str,
        project_id: str,
        now_epoch: float,
    ) -> bool:
        if conn.execute(
            "SELECT 1 FROM runs WHERE worker_id = ? AND state IN ('queued', 'running', 'paused') LIMIT 1",
            (worker_id,),
        ).fetchone() is not None:
            return True
        if conn.execute(
            """
            SELECT 1 FROM scheduled_runs
            WHERE worker_id = ? AND state IN (
                'pending', 'claimed', 'running', 'queued', 'retryable', 'action_required'
            ) LIMIT 1
            """,
            (worker_id,),
        ).fetchone() is not None:
            return True
        if conn.execute(
            "SELECT 1 FROM recurring_schedule_definitions WHERE worker_id = ? AND active = 1 LIMIT 1",
            (worker_id,),
        ).fetchone() is not None:
            return True

        tables = {
            str(item["name"])
            for item in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        if "provider_account_leases" in tables and conn.execute(
            """
            SELECT 1 FROM provider_account_leases
            WHERE worker_id = ? AND released_at IS NULL AND expires_at > ? LIMIT 1
            """,
            (worker_id, float(now_epoch)),
        ).fetchone() is not None:
            return True
        if "control_plane_pending_changes" in tables:
            conn.execute(
                """
                UPDATE control_plane_pending_changes
                SET status = 'expired', resolved_at = ?
                WHERE target_id = ? AND status = 'pending' AND expires_at <= ?
                """,
                (float(now_epoch), worker_id, float(now_epoch)),
            )
            if conn.execute(
                """
                SELECT 1 FROM control_plane_pending_changes
                WHERE target_id = ? AND status = 'pending' AND expires_at > ? LIMIT 1
                """,
                (worker_id, float(now_epoch)),
            ).fetchone() is not None:
                return True
        if "workspace_template_instantiations" in tables and conn.execute(
            """
            SELECT 1 FROM workspace_template_instantiations
            WHERE status = 'pending' AND (worker_id = ? OR project_id = ?) LIMIT 1
            """,
            (worker_id, project_id),
        ).fetchone() is not None:
            return True
        if "workspace_duplications" in tables and conn.execute(
            """
            SELECT 1 FROM workspace_duplications
            WHERE status = 'pending'
              AND (source_worker_id = ? OR worker_id = ? OR project_id = ?)
            LIMIT 1
            """,
            (worker_id, worker_id, project_id),
        ).fetchone() is not None:
            return True
        return False

    def list_recoverable_workspace_gc_claims(
        self,
        *,
        now_epoch: float,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return expired durable claims that a restarted reaper may safely adopt."""

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT workers.*
                FROM workspace_gc_tombstones
                INNER JOIN workers ON workers.worker_id = workspace_gc_tombstones.worker_id
                WHERE workspace_gc_tombstones.phase = 'claimed'
                  AND workspace_gc_tombstones.claim_expires_at <= ?
                  AND workers.workspace_kind = 'ephemeral'
                  AND workers.state = 'gc_claimed'
                ORDER BY workspace_gc_tombstones.claimed_at ASC
                LIMIT ?
                """,
                (float(now_epoch), max(1, min(int(limit), 200))),
            ).fetchall()
        return self._rows(rows)

    def claim_ephemeral_workspace_gc(
        self,
        worker_id: str,
        *,
        updated_before: str,
        now_epoch: float,
        claim_token: str,
        claim_ttl_s: int,
        managed_storage_root: str = "",
    ) -> dict[str, Any] | None:
        """Durably claim one eligible ephemeral workspace before any destructive action."""

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM workspace_gc_tombstones WHERE worker_id = ?",
                (worker_id,),
            ).fetchone()
            row = conn.execute(
                """
                SELECT * FROM workers
                WHERE worker_id = ? AND workspace_kind = 'ephemeral' AND updated_at < ?
                """,
                (worker_id, updated_before),
            ).fetchone()
            if row is None:
                return None
            worker = self._row(row) or {}
            project_id = str(worker.get("project_id") or "")
            if existing is not None:
                if (
                    str(existing["phase"] or "") != "claimed"
                    or float(existing["claim_expires_at"] or 0) > float(now_epoch)
                    or str(worker.get("state") or "") != "gc_claimed"
                ):
                    return None
                if self._ephemeral_gc_has_blocker(
                    conn,
                    worker_id=worker_id,
                    project_id=project_id,
                    now_epoch=now_epoch,
                ):
                    conn.execute(
                        """
                        UPDATE workers SET state = ?, updated_at = ?
                        WHERE worker_id = ? AND state = 'gc_claimed'
                        """,
                        (existing["original_state"], existing["original_updated_at"], worker_id),
                    )
                    conn.execute("DELETE FROM workspace_gc_tombstones WHERE worker_id = ?", (worker_id,))
                    return None
                conn.execute(
                    """
                    UPDATE workspace_gc_tombstones
                    SET claim_token = ?, claim_expires_at = ?, managed_storage_root = ?,
                        last_error = '', updated_at = ?
                    WHERE worker_id = ? AND phase = 'claimed' AND claim_expires_at <= ?
                    """,
                    (
                        claim_token,
                        float(now_epoch) + max(10, min(int(claim_ttl_s), 3600)),
                        str(managed_storage_root or ""),
                        float(now_epoch),
                        worker_id,
                        float(now_epoch),
                    ),
                )
                return worker
            if str(worker.get("state") or "") in {"created", "starting", "running", "gc_claimed"}:
                return None
            if self._ephemeral_gc_has_blocker(
                conn,
                worker_id=worker_id,
                project_id=project_id,
                now_epoch=now_epoch,
            ):
                return None
            conn.execute(
                """
                INSERT INTO workspace_gc_tombstones (
                    worker_id, project_id, tenant_id, owner_id, profile, execution_mode,
                    workspace_kind, original_state, original_updated_at, state_dir, workspace_dir,
                    workspace_root, managed_storage_root, phase, claim_token, claim_expires_at,
                    claimed_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'ephemeral', ?, ?, ?, ?, ?, ?, 'claimed', ?, ?, ?, ?)
                """,
                (
                    worker_id,
                    project_id,
                    str(worker.get("tenant_id") or "local"),
                    str(worker.get("owner_id") or ""),
                    str(worker.get("profile") or ""),
                    str(worker.get("execution_mode") or "docker"),
                    str(worker.get("state") or "ready"),
                    str(worker.get("updated_at") or ""),
                    str(worker.get("state_dir") or ""),
                    str(worker.get("workspace_dir") or ""),
                    str(worker.get("workspace_root") or ""),
                    str(managed_storage_root or ""),
                    claim_token,
                    float(now_epoch) + max(10, min(int(claim_ttl_s), 3600)),
                    float(now_epoch),
                    float(now_epoch),
                ),
            )
            changed = conn.execute(
                """
                UPDATE workers SET state = 'gc_claimed'
                WHERE worker_id = ? AND workspace_kind = 'ephemeral' AND updated_at = ?
                """,
                (worker_id, str(worker.get("updated_at") or "")),
            )
            if changed.rowcount != 1:
                raise RuntimeError("Ephemeral workspace changed before its GC claim was recorded")
        return worker

    def release_ephemeral_workspace_gc_claim(self, worker_id: str, *, claim_token: str) -> bool:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            tombstone = conn.execute(
                """
                SELECT * FROM workspace_gc_tombstones
                WHERE worker_id = ? AND phase = 'claimed' AND claim_token = ?
                """,
                (worker_id, claim_token),
            ).fetchone()
            if tombstone is None:
                return False
            conn.execute(
                """
                UPDATE workers SET state = ?, updated_at = ?
                WHERE worker_id = ? AND state = 'gc_claimed'
                """,
                (tombstone["original_state"], tombstone["original_updated_at"], worker_id),
            )
            conn.execute(
                "DELETE FROM workspace_gc_tombstones WHERE worker_id = ? AND claim_token = ?",
                (worker_id, claim_token),
            )
        return True

    def finalize_ephemeral_workspace_gc(
        self,
        worker_id: str,
        *,
        claim_token: str,
        updated_before: str,
        now_epoch: float,
    ) -> dict[str, Any] | None:
        """Delete claimed runtime metadata while retaining durable replay history."""

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            tombstone = conn.execute(
                """
                SELECT * FROM workspace_gc_tombstones
                WHERE worker_id = ? AND phase = 'claimed' AND claim_token = ?
                """,
                (worker_id, claim_token),
            ).fetchone()
            row = conn.execute(
                """
                SELECT * FROM workers
                WHERE worker_id = ? AND workspace_kind = 'ephemeral'
                  AND state = 'gc_claimed' AND updated_at < ?
                """,
                (worker_id, updated_before),
            ).fetchone()
            if tombstone is None or row is None:
                return None
            worker = self._row(row) or {}
            project_id = str(worker.get("project_id") or "")
            if self._ephemeral_gc_has_blocker(
                conn,
                worker_id=worker_id,
                project_id=project_id,
                now_epoch=now_epoch,
            ):
                return None

            tables = {
                str(item["name"])
                for item in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
            }

            session_ids = [
                str(item["session_id"])
                for item in conn.execute(
                    "SELECT session_id FROM provider_sessions WHERE worker_id = ?",
                    (worker_id,),
                ).fetchall()
            ]
            if session_ids:
                placeholders = ",".join("?" for _ in session_ids)
                request_ids = [
                    str(item["request_id"])
                    for item in conn.execute(
                        f"SELECT request_id FROM provider_requests WHERE session_id IN ({placeholders})",
                        session_ids,
                    ).fetchall()
                ]
                if request_ids:
                    request_placeholders = ",".join("?" for _ in request_ids)
                    conn.execute(
                        f"DELETE FROM provider_activity WHERE request_id IN ({request_placeholders})",
                        request_ids,
                    )
                    conn.execute(
                        f"DELETE FROM provider_requests WHERE request_id IN ({request_placeholders})",
                        request_ids,
                    )
                conn.execute(
                    f"DELETE FROM provider_sessions WHERE session_id IN ({placeholders})",
                    session_ids,
                )

            definition_ids = [
                str(item["definition_id"])
                for item in conn.execute(
                    "SELECT definition_id FROM recurring_schedule_definitions WHERE worker_id = ?",
                    (worker_id,),
                ).fetchall()
            ]
            if definition_ids:
                placeholders = ",".join("?" for _ in definition_ids)
                conn.execute(
                    f"DELETE FROM recurring_schedule_occurrences WHERE definition_id IN ({placeholders})",
                    definition_ids,
                )
            conn.execute(
                """
                DELETE FROM recurring_schedule_occurrences
                WHERE scheduled_run_id IN (SELECT schedule_id FROM scheduled_runs WHERE worker_id = ?)
                """,
                (worker_id,),
            )
            conn.execute("DELETE FROM recurring_schedule_definitions WHERE worker_id = ?", (worker_id,))
            conn.execute("DELETE FROM scheduled_runs WHERE worker_id = ?", (worker_id,))
            conn.execute("DELETE FROM callback_outbox WHERE worker_id = ?", (worker_id,))
            conn.execute("DELETE FROM events WHERE worker_id = ?", (worker_id,))
            conn.execute("DELETE FROM runs WHERE worker_id = ?", (worker_id,))
            if "provider_account_leases" in tables:
                conn.execute("DELETE FROM provider_account_leases WHERE worker_id = ?", (worker_id,))
            if "workspace_capability_grants" in tables:
                conn.execute("DELETE FROM workspace_capability_grants WHERE worker_id = ?", (worker_id,))
            if "control_plane_pending_changes" in tables:
                conn.execute("DELETE FROM control_plane_pending_changes WHERE target_id = ?", (worker_id,))
            deleted = conn.execute(
                "DELETE FROM workers WHERE worker_id = ? AND workspace_kind = 'ephemeral'",
                (worker_id,),
            )
            if deleted.rowcount != 1:
                return None
            project_deleted = conn.execute(
                """
                DELETE FROM projects
                WHERE project_id = ?
                  AND NOT EXISTS (SELECT 1 FROM workers WHERE workers.project_id = projects.project_id)
                """,
                (project_id,),
            ).rowcount == 1
            conn.execute(
                """
                UPDATE workspace_gc_tombstones
                SET phase = 'cleanup_pending', metadata_deleted_at = ?, updated_at = ?, last_error = ''
                WHERE worker_id = ? AND phase = 'claimed' AND claim_token = ?
                """,
                (float(now_epoch), float(now_epoch), worker_id, claim_token),
            )
        return {**worker, "project_deleted": project_deleted}

    def list_workspace_gc_cleanup_candidates(
        self,
        *,
        now_epoch: float,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM workspace_gc_tombstones
                WHERE phase = 'cleanup_pending' AND claim_expires_at <= ?
                ORDER BY updated_at ASC LIMIT ?
                """,
                (float(now_epoch), max(1, min(int(limit), 200))),
            ).fetchall()
        return self._rows(rows)

    def claim_workspace_gc_cleanup(
        self,
        worker_id: str,
        *,
        claim_token: str,
        now_epoch: float,
        claim_ttl_s: int,
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM workspace_gc_tombstones WHERE worker_id = ? AND phase = 'cleanup_pending'",
                (worker_id,),
            ).fetchone()
            if row is None:
                return None
            current_token = str(row["claim_token"] or "")
            if current_token and current_token != claim_token and float(row["claim_expires_at"] or 0) > now_epoch:
                return None
            changed = conn.execute(
                """
                UPDATE workspace_gc_tombstones
                SET claim_token = ?, claim_expires_at = ?, updated_at = ?
                WHERE worker_id = ? AND phase = 'cleanup_pending'
                  AND (claim_token = ? OR claim_token = '' OR claim_expires_at <= ?)
                """,
                (
                    claim_token,
                    float(now_epoch) + max(10, min(int(claim_ttl_s), 3600)),
                    float(now_epoch),
                    worker_id,
                    current_token,
                    float(now_epoch),
                ),
            )
            if changed.rowcount != 1:
                return None
            refreshed = conn.execute(
                "SELECT * FROM workspace_gc_tombstones WHERE worker_id = ?",
                (worker_id,),
            ).fetchone()
        return self._row(refreshed)

    def record_workspace_gc_cleanup(
        self,
        worker_id: str,
        *,
        claim_token: str,
        now_epoch: float,
        error: str = "",
    ) -> bool:
        with self._connect() as conn:
            if error:
                changed = conn.execute(
                    """
                    UPDATE workspace_gc_tombstones
                    SET claim_token = '', claim_expires_at = 0,
                        cleanup_attempts = cleanup_attempts + 1,
                        last_error = ?, updated_at = ?
                    WHERE worker_id = ? AND phase = 'cleanup_pending' AND claim_token = ?
                    """,
                    (str(error)[:2000], float(now_epoch), worker_id, claim_token),
                )
            else:
                changed = conn.execute(
                    """
                    UPDATE workspace_gc_tombstones
                    SET phase = 'completed', claim_token = '', claim_expires_at = 0,
                        cleanup_attempts = cleanup_attempts + 1,
                        last_error = '', completed_at = ?, updated_at = ?
                    WHERE worker_id = ? AND phase = 'cleanup_pending' AND claim_token = ?
                    """,
                    (float(now_epoch), float(now_epoch), worker_id, claim_token),
                )
        return changed.rowcount == 1

    def get_workspace_gc_tombstone(self, worker_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM workspace_gc_tombstones WHERE worker_id = ?",
                (worker_id,),
            ).fetchone()
        return self._row(row)

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

    def workspace_gc_claim_active(self, worker_id: str) -> bool:
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT 1 FROM workspace_gc_tombstones
                WHERE worker_id = ? AND phase IN ('claimed', 'cleanup_pending')
                LIMIT 1
                """,
                (worker_id,),
            ).fetchone() is not None

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

    def update_worker(
        self,
        worker_id: str,
        **fields: Any,
    ) -> dict[str, Any] | None:
        if not fields:
            return self.get_worker(worker_id)
        fields["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = :{key}" for key in fields.keys())
        fields["worker_id"] = worker_id
        target_state = str(fields.get("state") or "")
        protect_close = (
            "state" in fields
            and target_state not in {"terminating", "termination_failed", "terminated"}
        )
        where = "worker_id = :worker_id"
        if protect_close:
            where += " AND state NOT IN ('terminating', 'termination_failed', 'terminated')"
        with self._connect() as conn:
            conn.execute(f"UPDATE workers SET {assignments} WHERE {where}", fields)
            row = conn.execute("SELECT * FROM workers WHERE worker_id = ?", (worker_id,)).fetchone()
        return self._row(row)

    def update_worker_unless_gc_claimed(self, worker_id: str, **fields: Any) -> dict[str, Any] | None:
        """Update a workspace only when no durable GC claim owns its lifecycle."""

        if not fields:
            return None if self.workspace_gc_claim_active(worker_id) else self.get_worker(worker_id)
        fields["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = :{key}" for key in fields.keys())
        fields["worker_id"] = worker_id
        target_state = str(fields.get("state") or "")
        protect_close = "state" in fields and target_state not in {"terminating", "termination_failed", "terminated"}
        where = "worker_id = :worker_id"
        if protect_close:
            where += " AND state NOT IN ('terminating', 'termination_failed', 'terminated')"
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            claimed = conn.execute(
                """
                SELECT 1 FROM workspace_gc_tombstones
                WHERE worker_id = ? AND phase IN ('claimed', 'cleanup_pending') LIMIT 1
                """,
                (worker_id,),
            ).fetchone()
            if claimed is not None:
                return None
            changed = conn.execute(
                f"UPDATE workers SET {assignments} WHERE {where}",
                fields,
            )
            if changed.rowcount != 1:
                return None
            row = conn.execute("SELECT * FROM workers WHERE worker_id = ?", (worker_id,)).fetchone()
        return self._row(row)

    def update_worker_state(self, worker_id: str, state: str, last_error: str | None = None) -> dict[str, Any] | None:
        fields: dict[str, Any] = {"state": state}
        if last_error is not None:
            fields["last_error"] = last_error
        return self.update_worker(worker_id, **fields)

    def begin_worker_termination(self, worker_id: str) -> tuple[dict[str, Any] | None, bool]:
        """Publish permanent close intent once before any external runtime teardown."""

        now = utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            worker = conn.execute(
                "SELECT * FROM workers WHERE worker_id = ?",
                (worker_id,),
            ).fetchone()
            if worker is None:
                conn.execute("COMMIT")
                return None, False
            if str(worker["state"] or "") in {"terminating", "terminated"}:
                conn.execute("COMMIT")
                return self._row(worker), False
            conn.execute(
                "UPDATE workers SET state = 'terminating', last_error = '', updated_at = ? WHERE worker_id = ?",
                (now, worker_id),
            )
            conn.execute(
                """
                UPDATE recurring_schedule_occurrences
                SET state = 'cancelled', outcome = 'workspace_closed', terminal_at = ?
                WHERE scheduled_run_id IN (
                    SELECT schedule_id FROM scheduled_runs WHERE worker_id = ?
                )
                  AND state NOT IN ('completed', 'failed', 'cancelled', 'skipped')
                """,
                (now, worker_id),
            )
            conn.execute(
                """
                UPDATE scheduled_runs
                SET state = 'cancelled', last_error = 'workspace_closed', updated_at = ?
                WHERE worker_id = ?
                  AND state NOT IN ('completed', 'failed', 'cancelled')
                """,
                (now, worker_id),
            )
            conn.execute(
                """
                UPDATE recurring_schedule_definitions
                SET enabled = 0, active = 0, updated_at = ?
                WHERE worker_id = ? AND active = 1
                """,
                (now, worker_id),
            )
            updated = conn.execute(
                "SELECT * FROM workers WHERE worker_id = ?",
                (worker_id,),
            ).fetchone()
            conn.execute("COMMIT")
        return self._row(updated), True

    def fail_worker_termination(self, worker_id: str, message: str) -> dict[str, Any] | None:
        """Let only the owning close attempt make a failed teardown retryable."""

        now = utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE workers
                SET state = 'termination_failed', last_error = ?, updated_at = ?
                WHERE worker_id = ? AND state = 'terminating'
                """,
                (message, now, worker_id),
            )
            row = conn.execute(
                "SELECT * FROM workers WHERE worker_id = ?",
                (worker_id,),
            ).fetchone()
            conn.execute("COMMIT")
        return self._row(row)

    def record_worker_termination_cleanup_failure(
        self,
        worker_id: str,
        message: str,
    ) -> dict[str, Any] | None:
        """Keep a failed post-close runtime cleanup sticky over a successful close writer."""

        now = utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE workers
                SET state = 'termination_failed', last_error = ?, updated_at = ?
                WHERE worker_id = ?
                  AND state IN ('terminating', 'termination_failed', 'terminated')
                """,
                (message, now, worker_id),
            )
            row = conn.execute(
                "SELECT * FROM workers WHERE worker_id = ?",
                (worker_id,),
            ).fetchone()
            conn.execute("COMMIT")
        return self._row(row)

    def complete_worker_termination(
        self,
        worker_id: str,
        **fields: Any,
    ) -> dict[str, Any] | None:
        """Publish terminal runtime data only while this close attempt still owns the CAS."""

        now = utc_now()
        values = {
            **fields,
            "state": "terminated",
            "last_error": "",
            "updated_at": now,
            "worker_id": worker_id,
        }
        assignments = ", ".join(
            f"{key} = :{key}" for key in values if key != "worker_id"
        )
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                f"UPDATE workers SET {assignments} "
                "WHERE worker_id = :worker_id AND state = 'terminating'",
                values,
            )
            row = conn.execute(
                "SELECT * FROM workers WHERE worker_id = ?",
                (worker_id,),
            ).fetchone()
            conn.execute("COMMIT")
        return self._row(row)

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

    def create_run(
        self,
        worker_id: str,
        project_id: str,
        instruction: str,
        state: str = "queued",
        *,
        resume_paused: bool = False,
    ) -> dict[str, Any]:
        run_id = f"run_{uuid.uuid4().hex[:10]}"
        queued_at = utc_now()
        data = {
            "run_id": run_id,
            "worker_id": worker_id,
            "project_id": project_id,
            "tenant_id": "local",
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
            conn.execute("BEGIN IMMEDIATE")
            worker = conn.execute(
                "SELECT * FROM workers WHERE worker_id = ? AND project_id = ?",
                (worker_id, project_id),
            ).fetchone()
            if worker is None:
                raise ValueError("Worker not found for project")
            if str(worker["state"] or "") in {"terminating", "termination_failed", "terminated"}:
                raise WorkerClosedStoreError(
                    "Workspace is closed; create a new workspace for new work"
                )
            if resume_paused and str(worker["state"] or "") == "paused":
                conn.execute(
                    "UPDATE workers SET state = 'starting', last_error = '', updated_at = ? WHERE worker_id = ?",
                    (queued_at, worker_id),
                )
            if conn.execute(
                """
                SELECT 1 FROM workspace_gc_tombstones
                WHERE worker_id = ? AND phase IN ('claimed', 'cleanup_pending') LIMIT 1
                """,
                (worker_id,),
            ).fetchone() is not None:
                raise RuntimeError("Workspace is being garbage-collected")
            data["tenant_id"] = str(worker["tenant_id"] or "local")
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
            conn.execute(
                "UPDATE workers SET last_run_id = ?, updated_at = ? WHERE worker_id = ?",
                (run_id, queued_at, worker_id),
            )
        return data

    def create_or_get_run_for_schedule(
        self,
        schedule_id: str,
        *,
        runtime_bundle: dict[str, Any] | None = None,
        require_principal_authority: bool = False,
    ) -> tuple[dict[str, Any], bool]:
        """Atomically reserve and link one stable run to one scheduled dispatch.

        The schedule link and run insert share a transaction so a process crash can
        never leave an unreferenced run that stale-claim recovery dispatches again.
        """

        run_digest = hashlib.sha256(f"scheduled-run\0{schedule_id}".encode("utf-8")).hexdigest()
        run_id = f"run_sch_{run_digest[:18]}"
        queued_at = utc_now()
        runtime_bundle_json = (
            json.dumps(runtime_bundle, sort_keys=True, separators=(",", ":"))
            if isinstance(runtime_bundle, dict) and runtime_bundle
            else None
        )
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            schedule = conn.execute(
                "SELECT * FROM scheduled_runs WHERE schedule_id = ?",
                (schedule_id,),
            ).fetchone()
            if schedule is None:
                conn.execute("ROLLBACK")
                raise ValueError("Scheduled run not found")
            if require_principal_authority:
                self._require_schedule_principal_authority_in_transaction(
                    conn,
                    tenant_id=str(schedule["tenant_id"] or "local"),
                    owner_id=str(schedule["owner_id"] or ""),
                )

            linked_run_id = str(schedule["queued_run_id"] or "").strip()
            if linked_run_id:
                existing = conn.execute(
                    "SELECT * FROM runs WHERE run_id = ?",
                    (linked_run_id,),
                ).fetchone()
                if existing is None:
                    conn.execute("ROLLBACK")
                    raise RuntimeError("Scheduled run references a missing queued run")
                conn.execute("COMMIT")
                return self._row(existing) or {}, False

            if str(schedule["state"] or "") != "running":
                conn.execute("ROLLBACK")
                raise RuntimeError("Scheduled run must be claimed before dispatch")

            worker = conn.execute(
                "SELECT state FROM workers WHERE worker_id = ? AND project_id = ?",
                (schedule["worker_id"], schedule["project_id"]),
            ).fetchone()
            if worker is None:
                conn.execute("ROLLBACK")
                raise ValueError("Scheduled workspace no longer exists")
            if str(worker["state"] or "") in {"terminating", "termination_failed", "terminated"}:
                conn.execute("ROLLBACK")
                raise WorkerClosedStoreError(
                    "Workspace is closed; create a new workspace for new work"
                )
            if str(worker["state"] or "") == "paused":
                conn.execute(
                    "UPDATE workers SET state = 'starting', last_error = '', updated_at = ? WHERE worker_id = ?",
                    (queued_at, schedule["worker_id"]),
                )

            existing = conn.execute(
                "SELECT * FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            created = existing is None
            if existing is not None:
                expected = (
                    str(schedule["worker_id"]),
                    str(schedule["project_id"]),
                    str(schedule["instruction"]),
                )
                actual = (
                    str(existing["worker_id"]),
                    str(existing["project_id"]),
                    str(existing["instruction"]),
                )
                if actual != expected:
                    conn.execute("ROLLBACK")
                    raise RuntimeError("Stable scheduled run id is bound to another request")
            else:
                conn.execute(
                    """
                    INSERT INTO runs (
                        run_id, worker_id, project_id, tenant_id, instruction, state, queued_at,
                        started_at, ended_at, output_text, error_text, failure_class,
                        failure_retryable, failure_user_message, failure_recommended_recovery,
                        failure_diagnostic_summary, runtime_bundle_json, retry_after,
                        retry_attempts, last_retry_class
                    ) VALUES (?, ?, ?, ?, ?, 'queued', ?, NULL, NULL, '', '', '', 0, '', '', '', ?, NULL, 0, '')
                    """,
                    (
                        run_id,
                        schedule["worker_id"],
                        schedule["project_id"],
                        schedule["tenant_id"],
                        schedule["instruction"],
                        queued_at,
                        runtime_bundle_json,
                    ),
                )

            linked = conn.execute(
                """
                UPDATE scheduled_runs
                SET state = 'queued', queued_run_id = ?, last_error = '', updated_at = ?
                WHERE schedule_id = ? AND state = 'running' AND queued_run_id IS NULL
                """,
                (run_id, queued_at, schedule_id),
            )
            if linked.rowcount != 1:
                conn.execute("ROLLBACK")
                raise RuntimeError("Scheduled run dispatch claim changed before linking")
            conn.execute(
                """
                UPDATE recurring_schedule_occurrences
                SET state = 'queued', outcome = 'pending', claim_expires_at = NULL
                WHERE scheduled_run_id = ?
                  AND state NOT IN ('completed', 'failed', 'cancelled', 'skipped')
                """,
                (schedule_id,),
            )
            conn.execute(
                "UPDATE workers SET last_run_id = ?, updated_at = ? WHERE worker_id = ?",
                (run_id, queued_at, schedule["worker_id"]),
            )
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            conn.execute("COMMIT")
        return self._row(row) or {}, created

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
        if str(fields.get("state") or "") in {
            "completed",
            "failed",
            "cancelled",
            "interrupted",
            "paused",
        }:
            fields["runtime_bundle_json"] = None
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

    def claim_next_queued_run(
        self,
        worker_id: str,
        *,
        require_schedule_principal_authority: bool = False,
    ) -> dict[str, Any] | None:
        now_iso = utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            worker = conn.execute(
                "SELECT state FROM workers WHERE worker_id = ?",
                (worker_id,),
            ).fetchone()
            if worker is None or str(worker["state"] or "") in {
                "terminating",
                "termination_failed",
                "terminated",
            }:
                conn.execute("COMMIT")
                return None
            while True:
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
                scheduled = conn.execute(
                    """
                    SELECT schedule_id, tenant_id, owner_id
                    FROM scheduled_runs
                    WHERE queued_run_id = ?
                    """,
                    (row["run_id"],),
                ).fetchone()
                if require_schedule_principal_authority and scheduled is not None:
                    try:
                        self._require_schedule_principal_authority_in_transaction(
                            conn,
                            tenant_id=str(scheduled["tenant_id"] or "local"),
                            owner_id=str(scheduled["owner_id"] or ""),
                        )
                    except SchedulePrincipalAuthorityStoreError:
                        ended_at = utc_now()
                        conn.execute(
                            """
                            UPDATE runs
                            SET state = 'cancelled', ended_at = ?, error_text = 'principal_disabled',
                                failure_class = 'principal_disabled', failure_retryable = 0
                            WHERE run_id = ? AND state = 'queued'
                            """,
                            (ended_at, row["run_id"]),
                        )
                        conn.execute(
                            """
                            UPDATE scheduled_runs
                            SET state = 'cancelled', last_error = 'principal_disabled', updated_at = ?
                            WHERE schedule_id = ?
                              AND state NOT IN ('completed', 'failed', 'cancelled')
                            """,
                            (ended_at, scheduled["schedule_id"]),
                        )
                        conn.execute(
                            """
                            UPDATE recurring_schedule_occurrences
                            SET state = 'action_required', outcome = 'principal_disabled', terminal_at = ?
                            WHERE scheduled_run_id = ?
                              AND state NOT IN ('completed', 'failed', 'cancelled', 'skipped')
                            """,
                            (ended_at, scheduled["schedule_id"]),
                        )
                        continue
                break
            started_at = utc_now()
            conn.execute(
                """
                UPDATE runs
                SET state = 'running',
                    started_at = ?,
                    retry_after = NULL,
                    error_text = '',
                    failure_class = '',
                    failure_retryable = 0,
                    failure_user_message = '',
                    failure_recommended_recovery = '',
                    failure_diagnostic_summary = ''
                WHERE run_id = ?
                """,
                (started_at, row["run_id"]),
            )
            claimed = conn.execute("SELECT * FROM runs WHERE run_id = ?", (row["run_id"],)).fetchone()
            conn.execute("COMMIT")
        return self._row(claimed)

    def require_schedule_principal_authority_for_run(self, run_id: str) -> None:
        """Fail closed immediately before executing a queued run derived from a schedule."""

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            scheduled = conn.execute(
                """
                SELECT tenant_id, owner_id
                FROM scheduled_runs
                WHERE queued_run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if scheduled is not None:
                self._require_schedule_principal_authority_in_transaction(
                    conn,
                    tenant_id=str(scheduled["tenant_id"] or "local"),
                    owner_id=str(scheduled["owner_id"] or ""),
                )
            conn.execute("COMMIT")

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

    def release_host_capacity_waiters(
        self,
        *,
        profile: str,
        execution_mode: str,
        run_mode: str,
    ) -> list[str]:
        """Make only one released host CLI/auth lane immediately eligible."""

        normalized_profile = str(profile or "").strip()
        normalized_execution_mode = str(execution_mode or "").strip().lower()
        normalized_run_mode = (
            "conversation" if str(run_mode or "").strip().lower() == "conversation" else "mission"
        )
        if not normalized_profile or normalized_execution_mode != "host":
            return []

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT
                    runs.run_id,
                    runs.worker_id,
                    workers.bootstrap_bundle_json
                FROM runs
                INNER JOIN workers ON workers.worker_id = runs.worker_id
                WHERE runs.state = 'queued'
                  AND runs.failure_class = 'host_worker_busy'
                  AND runs.retry_after IS NOT NULL
                  AND runs.retry_after != ''
                  AND workers.profile = ?
                  AND workers.execution_mode = ?
                ORDER BY runs.queued_at ASC, runs.run_id ASC
                """,
                (normalized_profile, normalized_execution_mode),
            ).fetchall()
            matching_rows = []
            for row in rows:
                bundle = {}
                raw_bundle = str(row["bootstrap_bundle_json"] or "").strip()
                if raw_bundle:
                    try:
                        parsed = json.loads(raw_bundle)
                    except json.JSONDecodeError:
                        parsed = {}
                    bundle = parsed if isinstance(parsed, dict) else {}
                waiter_run_mode = (
                    "conversation"
                    if str(bundle.get("run_mode") or "").strip().lower() == "conversation"
                    else "mission"
                )
                if waiter_run_mode == normalized_run_mode:
                    matching_rows.append(row)
                    break
            run_ids = [str(row["run_id"]) for row in matching_rows]
            if run_ids:
                placeholders = ", ".join("?" for _ in run_ids)
                conn.execute(
                    f"""
                    UPDATE runs
                    SET retry_after = NULL
                    WHERE run_id IN ({placeholders})
                      AND state = 'queued'
                      AND failure_class = 'host_worker_busy'
                      AND retry_after IS NOT NULL
                      AND retry_after != ''
                    """,
                    run_ids,
                )
            conn.execute("COMMIT")
        return list(dict.fromkeys(str(row["worker_id"]) for row in matching_rows))

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
        usage: dict[str, Any] | None = None,
        **failure_fields: Any,
    ) -> dict[str, Any] | None:
        fields = {
            "state": state,
            "ended_at": utc_now(),
            "output_text": output_text,
            "error_text": error_text,
            "retry_after": None,
            "runtime_bundle_json": None,
        }
        fields.update(_normalized_token_usage(usage))
        fields.update(_normalized_failure_fields(failure_fields))
        return self.update_run(run_id, **fields)

    def finalize_run_if_state(
        self,
        run_id: str,
        expected_state: str,
        state: str,
        output_text: str = "",
        error_text: str = "",
        usage: dict[str, Any] | None = None,
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
        normalized_usage = _normalized_token_usage(usage)
        update_fields.update(normalized_usage)
        failure_assignments = "".join(f", {key} = :{key}" for key in normalized_failure_fields.keys())
        usage_assignments = "".join(f", {key} = :{key}" for key in normalized_usage.keys())
        with self._connect() as conn:
            cur = conn.execute(
                f"""
                UPDATE runs
                SET state = :state, ended_at = :ended_at, output_text = :output_text,
                    error_text = :error_text, runtime_bundle_json = NULL
                    {failure_assignments}{usage_assignments}
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
                "UPDATE runs SET state = ?, ended_at = ?, error_text = ?, runtime_bundle_json = NULL WHERE worker_id = ? AND state IN ('queued', 'running')",
                (state, utc_now(), error_text, worker_id),
            )
        return cur.rowcount

    @staticmethod
    def _recurring_definition_row(value: sqlite3.Row | None) -> dict[str, Any] | None:
        if value is None:
            return None
        data = dict(value)
        data["active"] = bool(data.get("active"))
        data["enabled"] = bool(data.get("enabled", data["active"]))
        owner = canonical_recurrence_owner(data.get("scheduler_owner"))
        data["schedule_owner"] = owner
        data["owner_action"] = (
            "dispatch_here" if owner == "glasshive_native" else "dispatch_via_viventium_cortex"
        )
        data["next_occurrence_at"] = str(data.get("next_run_at") or "")
        return data

    def create_recurring_schedule_definition(
        self,
        *,
        worker_id: str,
        project_id: str,
        tenant_id: str,
        owner_id: str,
        scheduler_owner: str,
        instruction: str,
        schedule_text: str,
        recurrence_type: str,
        interval_seconds: int | None,
        local_time: str,
        timezone_name: str,
        dst_policy: str,
        next_run_at: str,
        cron_expression: str = "",
        rrule: str = "",
        starts_at: str | None = None,
        ends_at: str | None = None,
        enabled: bool = True,
        overlap_policy: str = "skip",
        misfire_grace_seconds: int = 300,
        catch_up_policy: str = "coalesce",
        max_catch_up_occurrences: int = 1,
        jitter_seconds: int = 0,
        require_principal_authority: bool = False,
    ) -> dict[str, Any]:
        now = utc_now()
        data = {
            "definition_id": f"rsd_{uuid.uuid4().hex[:10]}",
            "project_id": project_id,
            "worker_id": worker_id,
            "tenant_id": tenant_id or "local",
            "owner_id": owner_id,
            "scheduler_owner": scheduler_owner,
            "instruction": instruction,
            "schedule_text": schedule_text,
            "recurrence_type": recurrence_type,
            "interval_seconds": interval_seconds,
            "local_time": local_time,
            "timezone_name": timezone_name,
            "dst_policy": dst_policy,
            "cron_expression": cron_expression,
            "rrule": rrule,
            "starts_at": starts_at,
            "ends_at": ends_at,
            "enabled": 1 if enabled else 0,
            "overlap_policy": overlap_policy,
            "misfire_grace_seconds": misfire_grace_seconds,
            "catch_up_policy": catch_up_policy,
            "max_catch_up_occurrences": max_catch_up_occurrences,
            "jitter_seconds": jitter_seconds,
            "next_run_at": next_run_at,
            "last_occurrence_at": None,
            "active": 1 if enabled else 0,
            "created_at": now,
            "updated_at": now,
        }
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            worker = conn.execute(
                "SELECT state FROM workers WHERE worker_id = ? AND project_id = ?",
                (worker_id, project_id),
            ).fetchone()
            if worker is None:
                raise ValueError("Scheduled workspace no longer exists")
            if str(worker["state"] or "") in {"terminating", "termination_failed", "terminated"}:
                raise WorkerClosedStoreError(
                    "Workspace is closed; create a new workspace for new work"
                )
            if require_principal_authority:
                self._require_schedule_principal_authority_in_transaction(
                    conn,
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                )
            conn.execute(
                """
                INSERT INTO recurring_schedule_definitions (
                    definition_id, project_id, worker_id, tenant_id, owner_id, scheduler_owner,
                    instruction, schedule_text, recurrence_type, interval_seconds, local_time,
                    timezone_name, dst_policy, cron_expression, rrule, starts_at, ends_at,
                    enabled, overlap_policy, misfire_grace_seconds, catch_up_policy,
                    max_catch_up_occurrences, jitter_seconds, next_run_at, last_occurrence_at, active,
                    created_at, updated_at
                )
                VALUES (
                    :definition_id, :project_id, :worker_id, :tenant_id, :owner_id, :scheduler_owner,
                    :instruction, :schedule_text, :recurrence_type, :interval_seconds, :local_time,
                    :timezone_name, :dst_policy, :cron_expression, :rrule, :starts_at, :ends_at,
                    :enabled, :overlap_policy, :misfire_grace_seconds, :catch_up_policy,
                    :max_catch_up_occurrences, :jitter_seconds, :next_run_at, :last_occurrence_at, :active,
                    :created_at, :updated_at
                )
                """,
                data,
            )
        return self._recurring_definition_row_from_dict(data)

    def ensure_schedule_principal_authority(
        self,
        *,
        tenant_id: str,
        owner_id: str,
    ) -> dict[str, Any]:
        tenant = str(tenant_id or "local")
        owner = str(owner_id or "").strip()
        if not owner:
            raise ValueError("schedule principal owner is required")
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO schedule_principal_authority
                    (tenant_id, owner_id, enabled, authority_epoch, updated_at)
                VALUES (?, ?, 1, 1, ?)
                ON CONFLICT(tenant_id, owner_id) DO NOTHING
                """,
                (tenant, owner, now),
            )
            row = conn.execute(
                """
                SELECT tenant_id, owner_id, enabled, authority_epoch, updated_at
                FROM schedule_principal_authority
                WHERE tenant_id = ? AND owner_id = ?
                """,
                (tenant, owner),
            ).fetchone()
        assert row is not None
        result = dict(row)
        result["enabled"] = bool(result["enabled"])
        return result

    @staticmethod
    def _require_schedule_principal_authority_in_transaction(
        conn: sqlite3.Connection,
        *,
        tenant_id: str,
        owner_id: str,
    ) -> None:
        row = conn.execute(
            """
            SELECT enabled
            FROM schedule_principal_authority
            WHERE tenant_id = ? AND owner_id = ?
            """,
            (str(tenant_id or "local"), str(owner_id or "").strip()),
        ).fetchone()
        if row is None or not bool(row["enabled"]):
            raise SchedulePrincipalAuthorityStoreError(
                "scheduled principal has been disabled"
            )

    def get_schedule_principal_authority(
        self,
        *,
        tenant_id: str,
        owner_id: str,
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT tenant_id, owner_id, enabled, authority_epoch, updated_at
                FROM schedule_principal_authority
                WHERE tenant_id = ? AND owner_id = ?
                """,
                (str(tenant_id or "local"), str(owner_id or "").strip()),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["enabled"] = bool(result["enabled"])
        return result

    def set_schedule_principal_authority(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        enabled: bool,
    ) -> dict[str, Any]:
        """Atomically gate future fires and retire already-materialized native work."""

        tenant = str(tenant_id or "local")
        owner = str(owner_id or "").strip()
        if not owner:
            raise ValueError("schedule principal owner is required")
        now = utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO schedule_principal_authority
                    (tenant_id, owner_id, enabled, authority_epoch, updated_at)
                VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(tenant_id, owner_id) DO UPDATE SET
                    enabled = excluded.enabled,
                    authority_epoch = schedule_principal_authority.authority_epoch + 1,
                    updated_at = excluded.updated_at
                """,
                (tenant, owner, 1 if enabled else 0, now),
            )
            deactivated_definitions = 0
            cancelled_scheduled_runs = 0
            if not enabled:
                deactivated_definitions = conn.execute(
                    """
                    UPDATE recurring_schedule_definitions
                    SET active = 0, enabled = 0, updated_at = ?
                    WHERE tenant_id = ? AND owner_id = ? AND active = 1
                    """,
                    (now, tenant, owner),
                ).rowcount
                affected_rows = conn.execute(
                    """
                    SELECT scheduled.schedule_id, scheduled.queued_run_id,
                           queued.state AS queued_run_state
                    FROM scheduled_runs AS scheduled
                    LEFT JOIN runs AS queued ON queued.run_id = scheduled.queued_run_id
                    WHERE scheduled.tenant_id = ? AND scheduled.owner_id = ?
                      AND scheduled.state NOT IN ('completed', 'failed', 'cancelled')
                    """,
                    (tenant, owner),
                ).fetchall()
                cancellable_rows = [
                    row
                    for row in affected_rows
                    if not str(row["queued_run_id"] or "").strip()
                    or str(row["queued_run_state"] or "") == "queued"
                ]
                affected_ids = [str(row["schedule_id"]) for row in cancellable_rows]
                linked_run_ids = [
                    str(row["queued_run_id"])
                    for row in cancellable_rows
                    if str(row["queued_run_id"] or "").strip()
                ]
                if linked_run_ids:
                    placeholders = ",".join("?" for _ in linked_run_ids)
                    conn.execute(
                        f"""
                        UPDATE runs
                        SET state = 'cancelled', ended_at = ?, error_text = 'principal_disabled',
                            failure_class = 'principal_disabled', failure_retryable = 0,
                            failure_user_message = 'Scheduled principal authority was disabled.'
                        WHERE run_id IN ({placeholders}) AND state = 'queued'
                        """,
                        [now, *linked_run_ids],
                    )
                if affected_ids:
                    placeholders = ",".join("?" for _ in affected_ids)
                    cancelled_scheduled_runs = conn.execute(
                        f"""
                        UPDATE scheduled_runs
                        SET state = 'cancelled', last_error = 'principal_disabled', updated_at = ?
                        WHERE schedule_id IN ({placeholders})
                          AND state NOT IN ('completed', 'failed', 'cancelled')
                        """,
                        [now, *affected_ids],
                    ).rowcount
                    conn.execute(
                        f"""
                        UPDATE recurring_schedule_occurrences
                        SET state = 'action_required', outcome = 'principal_disabled', terminal_at = ?
                        WHERE scheduled_run_id IN ({placeholders})
                          AND state NOT IN ('completed', 'failed', 'cancelled', 'skipped')
                        """,
                        [now, *affected_ids],
                    )
            row = conn.execute(
                """
                SELECT tenant_id, owner_id, enabled, authority_epoch, updated_at
                FROM schedule_principal_authority
                WHERE tenant_id = ? AND owner_id = ?
                """,
                (tenant, owner),
            ).fetchone()
            conn.execute("COMMIT")
        assert row is not None
        result = dict(row)
        result.update(
            {
                "enabled": bool(result["enabled"]),
                "deactivated_native_definitions": deactivated_definitions,
                "cancelled_native_occurrences": cancelled_scheduled_runs,
            }
        )
        return result

    @staticmethod
    def _recurring_definition_row_from_dict(data: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(data)
        normalized["active"] = bool(normalized.get("active"))
        normalized["enabled"] = bool(normalized.get("enabled", normalized["active"]))
        owner = canonical_recurrence_owner(normalized.get("scheduler_owner"))
        normalized["schedule_owner"] = owner
        normalized["owner_action"] = (
            "dispatch_here" if owner == "glasshive_native" else "dispatch_via_viventium_cortex"
        )
        normalized["next_occurrence_at"] = str(normalized.get("next_run_at") or "")
        return normalized

    def get_recurring_schedule_definition(
        self,
        definition_id: str,
        *,
        tenant_id: str,
        owner_id: str,
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM recurring_schedule_definitions
                WHERE definition_id = ? AND tenant_id = ? AND owner_id = ?
                """,
                (definition_id, tenant_id or "local", owner_id),
            ).fetchone()
        return self._recurring_definition_row(row)

    def list_recurring_schedule_definitions(
        self,
        worker_id: str,
        *,
        tenant_id: str,
        owner_id: str,
        include_inactive: bool = False,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        query = """
            SELECT * FROM recurring_schedule_definitions
            WHERE worker_id = ? AND tenant_id = ? AND owner_id = ?
        """
        params: list[Any] = [worker_id, tenant_id or "local", owner_id]
        if not include_inactive:
            query += " AND active = 1"
        query += " ORDER BY next_run_at ASC, definition_id ASC LIMIT ?"
        params.append(max(1, min(int(limit), 100)))
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [row for value in rows if (row := self._recurring_definition_row(value)) is not None]

    def list_recurring_schedule_definitions_for_owner(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        include_inactive: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        query = """
            SELECT * FROM recurring_schedule_definitions
            WHERE tenant_id = ? AND owner_id = ?
        """
        params: list[Any] = [tenant_id or "local", owner_id]
        if not include_inactive:
            query += " AND active = 1"
        query += " ORDER BY next_run_at ASC, definition_id ASC LIMIT ?"
        params.append(max(1, min(int(limit), 100)))
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [row for value in rows if (row := self._recurring_definition_row(value)) is not None]

    def deactivate_recurring_schedule_definition(
        self,
        definition_id: str,
        *,
        tenant_id: str,
        owner_id: str,
    ) -> dict[str, Any] | None:
        now = utc_now()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE recurring_schedule_definitions
                SET active = 0, enabled = 0, updated_at = ?
                WHERE definition_id = ? AND tenant_id = ? AND owner_id = ?
                """,
                (now, definition_id, tenant_id or "local", owner_id),
            )
            if cursor.rowcount != 1:
                return None
            row = conn.execute(
                "SELECT * FROM recurring_schedule_definitions WHERE definition_id = ?",
                (definition_id,),
            ).fetchone()
        return self._recurring_definition_row(row)

    def retire_recurring_schedule_definition(
        self,
        definition_id: str,
        *,
        tenant_id: str,
        owner_id: str,
    ) -> dict[str, Any] | None:
        now = utc_now()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE recurring_schedule_definitions
                SET active = 0, enabled = 0, retired_at = COALESCE(retired_at, ?), updated_at = ?
                WHERE definition_id = ? AND tenant_id = ? AND owner_id = ?
                """,
                (now, now, definition_id, tenant_id or "local", owner_id),
            )
            if cursor.rowcount != 1:
                return None
            row = conn.execute(
                "SELECT * FROM recurring_schedule_definitions WHERE definition_id = ?",
                (definition_id,),
            ).fetchone()
        return self._recurring_definition_row(row)

    def update_recurring_schedule_definition(
        self,
        definition_id: str,
        *,
        tenant_id: str,
        owner_id: str,
        fields: dict[str, Any],
        require_principal_authority: bool = False,
    ) -> dict[str, Any] | None:
        allowed = {
            "instruction",
            "schedule_text",
            "recurrence_type",
            "interval_seconds",
            "local_time",
            "timezone_name",
            "dst_policy",
            "cron_expression",
            "rrule",
            "starts_at",
            "ends_at",
            "enabled",
            "overlap_policy",
            "misfire_grace_seconds",
            "catch_up_policy",
            "max_catch_up_occurrences",
            "jitter_seconds",
            "next_run_at",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if "enabled" in updates:
            updates["enabled"] = 1 if bool(updates["enabled"]) else 0
            updates["active"] = updates["enabled"]
        if not updates:
            return self.get_recurring_schedule_definition(
                definition_id,
                tenant_id=tenant_id,
                owner_id=owner_id,
            )
        updates["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = ?" for key in updates)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if bool(updates.get("enabled")):
                definition_worker = conn.execute(
                    """
                    SELECT workers.state
                    FROM recurring_schedule_definitions
                    LEFT JOIN workers
                      ON workers.worker_id = recurring_schedule_definitions.worker_id
                     AND workers.project_id = recurring_schedule_definitions.project_id
                    WHERE recurring_schedule_definitions.definition_id = ?
                      AND recurring_schedule_definitions.tenant_id = ?
                      AND recurring_schedule_definitions.owner_id = ?
                    """,
                    (definition_id, tenant_id or "local", owner_id),
                ).fetchone()
                if definition_worker is not None and definition_worker["state"] is None:
                    conn.execute("ROLLBACK")
                    raise ValueError("Scheduled workspace no longer exists")
                if definition_worker is not None and str(definition_worker["state"] or "") in {
                    "terminating",
                    "termination_failed",
                    "terminated",
                }:
                    conn.execute("ROLLBACK")
                    raise WorkerClosedStoreError(
                        "Workspace is closed; create a new workspace for new work"
                    )
            if require_principal_authority:
                self._require_schedule_principal_authority_in_transaction(
                    conn,
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                )
            cursor = conn.execute(
                f"""
                UPDATE recurring_schedule_definitions
                SET {assignments}
                WHERE definition_id = ? AND tenant_id = ? AND owner_id = ?
                """,
                [*updates.values(), definition_id, tenant_id or "local", owner_id],
            )
            if cursor.rowcount != 1:
                conn.execute("COMMIT")
                return None
            row = conn.execute(
                "SELECT * FROM recurring_schedule_definitions WHERE definition_id = ?",
                (definition_id,),
            ).fetchone()
            conn.execute("COMMIT")
        return self._recurring_definition_row(row)

    def list_due_recurring_schedule_definitions(
        self,
        now_iso: str,
        *,
        scheduler_owner: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM recurring_schedule_definitions
                WHERE scheduler_owner = ? AND active = 1 AND next_run_at <= ?
                ORDER BY next_run_at ASC, definition_id ASC
                LIMIT ?
                """,
                (scheduler_owner, now_iso, max(1, min(int(limit), 100))),
            ).fetchall()
        return [row for value in rows if (row := self._recurring_definition_row(value)) is not None]

    def materialize_recurring_schedule_occurrence(
        self,
        definition_id: str,
        *,
        expected_next_run_at: str,
        scheduled_for: str,
        next_run_at: str,
        detected_at: str,
        dispatch_at: str | None = None,
        occurrence_state: str = "pending",
        outcome: str = "pending",
        deactivate_after: bool = False,
    ) -> dict[str, Any] | None:
        """Atomically advance one definition and create one legacy scheduled-run occurrence."""

        identity_digest = hashlib.sha256(
            f"{definition_id}\0{scheduled_for}".encode("utf-8")
        ).hexdigest()
        occurrence_id = f"occ_{identity_digest[:20]}"
        idempotency_key = f"recurrence:{definition_id}:{identity_digest}"
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                """
                SELECT occurrence.*, scheduled.schedule_id
                FROM recurring_schedule_occurrences AS occurrence
                INNER JOIN scheduled_runs AS scheduled
                    ON scheduled.schedule_id = occurrence.scheduled_run_id
                WHERE occurrence.definition_id = ? AND occurrence.scheduled_for = ?
                """,
                (definition_id, scheduled_for),
            ).fetchone()
            if existing is not None:
                schedule = conn.execute(
                    "SELECT * FROM scheduled_runs WHERE schedule_id = ?",
                    (existing["scheduled_run_id"],),
                ).fetchone()
                conn.execute("COMMIT")
                result = self._row(schedule) or {}
                result.update({"occurrence_id": existing["occurrence_id"], "idempotency_key": existing["idempotency_key"]})
                return result
            definition = conn.execute(
                """
                SELECT * FROM recurring_schedule_definitions
                WHERE definition_id = ?
                  AND scheduler_owner = 'native'
                  AND active = 1
                  AND next_run_at = ?
                """,
                (definition_id, expected_next_run_at),
            ).fetchone()
            if definition is None:
                conn.execute("COMMIT")
                return None

            schedule_id = f"sch_{uuid.uuid4().hex[:10]}"
            schedule_state = (
                "pending"
                if occurrence_state == "pending"
                else "cancelled"
                if occurrence_state == "skipped"
                else "failed"
            )
            conn.execute(
                """
                INSERT INTO scheduled_runs (
                    schedule_id, project_id, worker_id, tenant_id, owner_id, instruction,
                    schedule_text, run_at, state, queued_run_id, last_error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, '', ?, ?)
                """,
                (
                    schedule_id,
                    definition["project_id"],
                    definition["worker_id"],
                    definition["tenant_id"],
                    definition["owner_id"],
                    definition["instruction"],
                    definition["schedule_text"],
                    dispatch_at or scheduled_for,
                    schedule_state,
                    detected_at,
                    detected_at,
                ),
            )
            conn.execute(
                """
                INSERT INTO recurring_schedule_occurrences (
                    occurrence_id, definition_id, tenant_id, owner_id, scheduled_for,
                    detected_at, scheduled_run_id, idempotency_key, state, outcome,
                    claimant, claimed_at, claim_expires_at, attempt_count, terminal_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '', NULL, NULL, 0, ?, ?)
                """,
                (
                    occurrence_id,
                    definition_id,
                    definition["tenant_id"],
                    definition["owner_id"],
                    scheduled_for,
                    detected_at,
                    schedule_id,
                    idempotency_key,
                    occurrence_state,
                    outcome,
                    detected_at if occurrence_state not in {"pending", "claimed", "queued", "running"} else None,
                    detected_at,
                ),
            )
            conn.execute(
                """
                UPDATE recurring_schedule_definitions
                SET next_run_at = ?, last_occurrence_at = ?,
                    active = CASE WHEN ? THEN 0 ELSE active END,
                    enabled = CASE WHEN ? THEN 0 ELSE enabled END,
                    updated_at = ?
                WHERE definition_id = ? AND next_run_at = ?
                """,
                (
                    next_run_at,
                    scheduled_for,
                    1 if deactivate_after else 0,
                    1 if deactivate_after else 0,
                    detected_at,
                    definition_id,
                    expected_next_run_at,
                ),
            )
            schedule = conn.execute(
                "SELECT * FROM scheduled_runs WHERE schedule_id = ?",
                (schedule_id,),
            ).fetchone()
            conn.execute("COMMIT")
        result = self._row(schedule) or {}
        result.update({"occurrence_id": occurrence_id, "idempotency_key": idempotency_key})
        return result

    def claim_recurring_schedule_occurrence(
        self,
        occurrence_id: str,
        *,
        claimant: str,
        now_iso: str,
        lease_seconds: int = 60,
    ) -> dict[str, Any] | None:
        now = datetime.fromisoformat(str(now_iso).replace("Z", "+00:00")).astimezone(timezone.utc)
        expires_at = (now + timedelta(seconds=max(1, min(int(lease_seconds), 3600)))).isoformat()
        now_value = now.isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE recurring_schedule_occurrences
                SET state = 'claimed', claimant = ?, claimed_at = ?, claim_expires_at = ?,
                    attempt_count = attempt_count + 1
                WHERE occurrence_id = ?
                  AND state NOT IN ('completed', 'failed', 'cancelled', 'skipped')
                  AND (claim_expires_at IS NULL OR claim_expires_at <= ? OR claimant = ?)
                """,
                (claimant, now_value, expires_at, occurrence_id, now_value, claimant),
            )
            row = conn.execute(
                "SELECT * FROM recurring_schedule_occurrences WHERE occurrence_id = ?",
                (occurrence_id,),
            ).fetchone()
            conn.execute("COMMIT")
        return dict(row) if cursor.rowcount == 1 and row is not None else None

    def create_recurring_schedule_run_now(
        self,
        definition_id: str,
        *,
        tenant_id: str,
        owner_id: str,
        idempotency_token: str,
        scheduled_for: str,
        require_principal_authority: bool = False,
    ) -> dict[str, Any] | None:
        digest = hashlib.sha256(
            f"{definition_id}\0manual\0{idempotency_token}".encode("utf-8")
        ).hexdigest()
        occurrence_id = f"occ_{digest[:20]}"
        idempotency_key = f"recurrence-manual:{definition_id}:{digest}"
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if require_principal_authority:
                self._require_schedule_principal_authority_in_transaction(
                    conn,
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                )
            definition = conn.execute(
                """
                SELECT * FROM recurring_schedule_definitions
                WHERE definition_id = ? AND tenant_id = ? AND owner_id = ? AND retired_at IS NULL
                """,
                (definition_id, tenant_id or "local", owner_id),
            ).fetchone()
            if definition is None:
                conn.execute("COMMIT")
                return None
            worker = conn.execute(
                "SELECT state FROM workers WHERE worker_id = ? AND project_id = ?",
                (definition["worker_id"], definition["project_id"]),
            ).fetchone()
            if worker is None:
                conn.execute("ROLLBACK")
                raise ValueError("Scheduled workspace no longer exists")
            if str(worker["state"] or "") in {"terminating", "termination_failed", "terminated"}:
                conn.execute("ROLLBACK")
                raise WorkerClosedStoreError(
                    "Workspace is closed; create a new workspace for new work"
                )
            existing = conn.execute(
                "SELECT * FROM recurring_schedule_occurrences WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                schedule = conn.execute(
                    "SELECT * FROM scheduled_runs WHERE schedule_id = ?",
                    (existing["scheduled_run_id"],),
                ).fetchone()
                conn.execute("COMMIT")
                result = self._row(schedule) or {}
                result.update({"occurrence_id": existing["occurrence_id"], "idempotency_key": idempotency_key})
                return result
            schedule_id = f"sch_{uuid.uuid4().hex[:10]}"
            conn.execute(
                """
                INSERT INTO scheduled_runs (
                    schedule_id, project_id, worker_id, tenant_id, owner_id, instruction,
                    schedule_text, run_at, state, queued_run_id, last_error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, '', ?, ?)
                """,
                (
                    schedule_id,
                    definition["project_id"],
                    definition["worker_id"],
                    definition["tenant_id"],
                    definition["owner_id"],
                    definition["instruction"],
                    "Run now",
                    scheduled_for,
                    scheduled_for,
                    scheduled_for,
                ),
            )
            conn.execute(
                """
                INSERT INTO recurring_schedule_occurrences (
                    occurrence_id, definition_id, tenant_id, owner_id, scheduled_for,
                    detected_at, scheduled_run_id, idempotency_key, state, outcome,
                    claimant, claimed_at, claim_expires_at, attempt_count, terminal_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 'manual_pending', '', NULL, NULL, 0, NULL, ?)
                """,
                (
                    occurrence_id,
                    definition_id,
                    definition["tenant_id"],
                    definition["owner_id"],
                    scheduled_for,
                    scheduled_for,
                    schedule_id,
                    idempotency_key,
                    scheduled_for,
                ),
            )
            schedule = conn.execute(
                "SELECT * FROM scheduled_runs WHERE schedule_id = ?",
                (schedule_id,),
            ).fetchone()
            conn.execute("COMMIT")
        result = self._row(schedule) or {}
        result.update({"occurrence_id": occurrence_id, "idempotency_key": idempotency_key})
        return result

    def finalize_recurring_schedule_occurrence(
        self,
        occurrence_id: str,
        *,
        claimant: str,
        state: str,
        outcome: str,
        terminal_at: str,
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE recurring_schedule_occurrences
                SET state = ?, outcome = ?, terminal_at = ?, claim_expires_at = NULL
                WHERE occurrence_id = ? AND claimant = ?
                  AND state NOT IN ('completed', 'failed', 'cancelled', 'skipped')
                """,
                (state, outcome, terminal_at, occurrence_id, claimant),
            )
            row = conn.execute(
                "SELECT * FROM recurring_schedule_occurrences WHERE occurrence_id = ?",
                (occurrence_id,),
            ).fetchone()
        return dict(row) if cursor.rowcount == 1 and row is not None else None

    def list_recurring_schedule_occurrences(
        self,
        definition_id: str,
        *,
        tenant_id: str,
        owner_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT occurrence.*, scheduled.state AS schedule_state,
                       scheduled.queued_run_id, scheduled.last_error
                FROM recurring_schedule_occurrences AS occurrence
                INNER JOIN scheduled_runs AS scheduled
                    ON scheduled.schedule_id = occurrence.scheduled_run_id
                WHERE occurrence.definition_id = ?
                  AND occurrence.tenant_id = ?
                  AND occurrence.owner_id = ?
                ORDER BY occurrence.scheduled_for DESC, occurrence.occurrence_id DESC
                LIMIT ?
                """,
                (definition_id, tenant_id or "local", owner_id, max(1, min(int(limit), 100))),
            ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            schedule_state = item.pop("schedule_state")
            if str(item.get("state") or "pending") in {"pending", "claimed", "queued", "running"}:
                item["state"] = schedule_state
            if not str(item.get("outcome") or "") or item.get("outcome") == "pending":
                if schedule_state in {"completed", "failed", "cancelled"}:
                    item["outcome"] = schedule_state
            items.append(item)
        return items

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
        require_principal_authority: bool = False,
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
            conn.execute("BEGIN IMMEDIATE")
            worker = conn.execute(
                "SELECT state FROM workers WHERE worker_id = ? AND project_id = ?",
                (worker_id, project_id),
            ).fetchone()
            if worker is None:
                raise ValueError("Scheduled workspace no longer exists")
            if str(worker["state"] or "") in {"terminating", "termination_failed", "terminated"}:
                raise WorkerClosedStoreError(
                    "Workspace is closed; create a new workspace for new work"
                )
            if require_principal_authority:
                self._require_schedule_principal_authority_in_transaction(
                    conn,
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                )
            if conn.execute(
                """
                SELECT 1 FROM workspace_gc_tombstones
                WHERE worker_id = ? AND phase IN ('claimed', 'cleanup_pending') LIMIT 1
                """,
                (worker_id,),
            ).fetchone() is not None:
                raise RuntimeError("Workspace is being garbage-collected")
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

    def create_or_get_cortex_workspace_schedule(
        self,
        *,
        occurrence_id: str,
        worker_id: str,
        project_id: str,
        tenant_id: str,
        owner_id: str,
        instruction: str,
        require_principal_authority: bool = False,
    ) -> dict[str, Any]:
        """Reserve one GlassHive scheduled dispatch for one authoritative Cortex occurrence."""

        digest = hashlib.sha256(
            f"viventium-cortex\0{tenant_id}\0{owner_id}\0{occurrence_id}".encode("utf-8")
        ).hexdigest()
        schedule_id = f"sch_cortex_{digest[:18]}"
        now = utc_now()
        expected = {
            "schedule_id": schedule_id,
            "project_id": project_id,
            "worker_id": worker_id,
            "tenant_id": tenant_id or "local",
            "owner_id": owner_id,
            "instruction": instruction,
            "schedule_text": f"viventium-cortex:{occurrence_id}",
            "run_at": now,
        }
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            worker = conn.execute(
                "SELECT state FROM workers WHERE worker_id = ? AND project_id = ?",
                (worker_id, project_id),
            ).fetchone()
            if worker is None:
                raise ValueError("Scheduled workspace no longer exists")
            if str(worker["state"] or "") in {"terminating", "termination_failed", "terminated"}:
                raise WorkerClosedStoreError(
                    "Workspace is closed; create a new workspace for new work"
                )
            if require_principal_authority:
                self._require_schedule_principal_authority_in_transaction(
                    conn,
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                )
            if conn.execute(
                """
                SELECT 1 FROM workspace_gc_tombstones
                WHERE worker_id = ? AND phase IN ('claimed', 'cleanup_pending', 'completed') LIMIT 1
                """,
                (worker_id,),
            ).fetchone() is not None:
                raise RuntimeError("Workspace is being garbage-collected")
            existing = conn.execute(
                "SELECT * FROM scheduled_runs WHERE schedule_id = ?",
                (schedule_id,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO scheduled_runs (
                        schedule_id, project_id, worker_id, tenant_id, owner_id, instruction,
                        schedule_text, run_at, state, queued_run_id, last_error, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, '', ?, ?)
                    """,
                    (
                        schedule_id,
                        project_id,
                        worker_id,
                        tenant_id or "local",
                        owner_id,
                        instruction,
                        expected["schedule_text"],
                        now,
                        now,
                        now,
                    ),
                )
                existing = conn.execute(
                    "SELECT * FROM scheduled_runs WHERE schedule_id = ?",
                    (schedule_id,),
                ).fetchone()
            actual = dict(existing) if existing is not None else {}
            for key, value in expected.items():
                if key == "run_at":
                    continue
                if str(actual.get(key) or "") != str(value or ""):
                    conn.execute("ROLLBACK")
                    raise RuntimeError("Cortex occurrence is already bound to another workspace dispatch")
            conn.execute("COMMIT")
        return actual

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

    def scheduling_cortex_occurrence_for_run(self, run_id: str) -> str:
        """Return the authoritative Cortex occurrence linked to a GlassHive run."""

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT schedule_text
                FROM scheduled_runs
                WHERE queued_run_id = ?
                  AND schedule_id LIKE 'sch_cortex_%'
                  AND schedule_text LIKE 'viventium-cortex:%'
                """,
                (run_id,),
            ).fetchone()
        value = str(row["schedule_text"] if row is not None else "")
        return value.removeprefix("viventium-cortex:").strip() if value else ""

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
        claim_expires_at = (
            datetime.fromisoformat(now.replace("Z", "+00:00")) + timedelta(minutes=5)
        ).isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            schedule_before_claim = conn.execute(
                "SELECT * FROM scheduled_runs WHERE schedule_id = ? AND state = 'pending'",
                (schedule_id,),
            ).fetchone()
            recurrence = conn.execute(
                """
                SELECT definition.overlap_policy
                FROM recurring_schedule_occurrences AS occurrence
                INNER JOIN recurring_schedule_definitions AS definition
                    ON definition.definition_id = occurrence.definition_id
                WHERE occurrence.scheduled_run_id = ?
                """,
                (schedule_id,),
            ).fetchone()
            if (
                schedule_before_claim is not None
                and recurrence is not None
                and str(recurrence["overlap_policy"] or "skip") == "skip"
            ):
                active = conn.execute(
                    """
                    SELECT 1 FROM runs
                    WHERE worker_id = ? AND state IN ('queued', 'running')
                    LIMIT 1
                    """,
                    (schedule_before_claim["worker_id"],),
                ).fetchone()
                if active is not None:
                    conn.execute(
                        """
                        UPDATE scheduled_runs
                        SET state = 'cancelled', last_error = 'Skipped because another run is active',
                            updated_at = ?
                        WHERE schedule_id = ? AND state = 'pending'
                        """,
                        (now, schedule_id),
                    )
                    conn.execute(
                        """
                        UPDATE recurring_schedule_occurrences
                        SET state = 'skipped', outcome = 'overlap_skipped', terminal_at = ?,
                            claimant = '', claimed_at = NULL, claim_expires_at = NULL
                        WHERE scheduled_run_id = ?
                          AND state NOT IN ('completed', 'failed', 'cancelled', 'skipped')
                        """,
                        (now, schedule_id),
                    )
                    conn.execute("COMMIT")
                    return None
            cur = conn.execute(
                "UPDATE scheduled_runs SET state = 'running', updated_at = ? WHERE schedule_id = ? AND state = 'pending'",
                (now, schedule_id),
            )
            if cur.rowcount == 1:
                conn.execute(
                    """
                    UPDATE recurring_schedule_occurrences
                    SET state = 'claimed', outcome = 'pending',
                        claimant = ?, claimed_at = ?, claim_expires_at = ?,
                        attempt_count = attempt_count + 1
                    WHERE scheduled_run_id = ?
                      AND state NOT IN ('completed', 'failed', 'cancelled', 'skipped')
                    """,
                    (f"glasshive_native:{schedule_id}", now, claim_expires_at, schedule_id),
                )
            row = conn.execute("SELECT * FROM scheduled_runs WHERE schedule_id = ?", (schedule_id,)).fetchone()
            conn.execute("COMMIT")
        if cur.rowcount != 1:
            return None
        return self._row(row)

    def recurring_occurrence_for_schedule(self, schedule_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM recurring_schedule_occurrences WHERE scheduled_run_id = ?",
                (schedule_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def mark_recurring_occurrence_retryable(self, schedule_id: str, outcome: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE recurring_schedule_occurrences
                SET state = 'retryable', outcome = ?, claimant = '',
                    claimed_at = NULL, claim_expires_at = NULL
                WHERE scheduled_run_id = ?
                  AND state NOT IN ('completed', 'failed', 'cancelled', 'skipped')
                """,
                (str(outcome or "capacity_deferred")[:500], schedule_id),
            )

    def count_active_runs(self, *, tenant_id: str, owner_id: str | None = None) -> int:
        query = """
            SELECT COUNT(*)
            FROM runs
            INNER JOIN workers ON workers.worker_id = runs.worker_id
            WHERE runs.tenant_id = ? AND runs.state IN ('queued', 'running')
        """
        params: list[Any] = [tenant_id or "local"]
        if owner_id is not None:
            query += " AND workers.owner_id = ?"
            params.append(owner_id)
        with self._connect() as conn:
            row = conn.execute(query, params).fetchone()
        return int(row[0] if row is not None else 0)

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
            terminal = state in {"completed", "failed", "cancelled"}
            conn.execute(
                """
                UPDATE recurring_schedule_occurrences
                SET state = ?, outcome = ?, claim_expires_at = NULL,
                    terminal_at = CASE WHEN ? THEN ? ELSE terminal_at END
                WHERE scheduled_run_id = ?
                  AND state NOT IN ('completed', 'failed', 'cancelled', 'skipped')
                """,
                (
                    state,
                    state if terminal else "pending",
                    1 if terminal else 0,
                    utc_now(),
                    schedule_id,
                ),
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
            terminal_at = utc_now()
            conn.execute(
                """
                UPDATE scheduled_runs
                SET state = ?, last_error = ?, updated_at = ?
                WHERE queued_run_id = ? AND state IN ('queued', 'running')
                """,
                (state, last_error, terminal_at, run_id),
            )
            conn.execute(
                """
                UPDATE recurring_schedule_occurrences
                SET state = ?, outcome = ?, claim_expires_at = NULL, terminal_at = ?
                WHERE scheduled_run_id IN (
                    SELECT schedule_id FROM scheduled_runs WHERE queued_run_id = ?
                )
                  AND state NOT IN ('completed', 'failed', 'cancelled', 'skipped')
                """,
                (state, state, terminal_at, run_id),
            )
            row = conn.execute("SELECT * FROM scheduled_runs WHERE queued_run_id = ?", (run_id,)).fetchone()
        return self._row(row)

    def recover_stale_recurring_occurrence_claims(self, now_iso: str) -> int:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            stale = conn.execute(
                """
                SELECT occurrence.occurrence_id, occurrence.scheduled_run_id
                FROM recurring_schedule_occurrences AS occurrence
                INNER JOIN scheduled_runs AS scheduled
                    ON scheduled.schedule_id = occurrence.scheduled_run_id
                WHERE occurrence.state = 'claimed'
                  AND occurrence.claim_expires_at IS NOT NULL
                  AND occurrence.claim_expires_at <= ?
                  AND scheduled.queued_run_id IS NULL
                """,
                (now_iso,),
            ).fetchall()
            for occurrence in stale:
                conn.execute(
                    """
                    UPDATE recurring_schedule_occurrences
                    SET state = 'retryable', outcome = 'claim_expired', claimant = '',
                        claimed_at = NULL, claim_expires_at = NULL
                    WHERE occurrence_id = ? AND state = 'claimed'
                    """,
                    (occurrence["occurrence_id"],),
                )
                conn.execute(
                    """
                    UPDATE scheduled_runs
                    SET state = 'pending', last_error = 'Recovered expired recurrence claim', updated_at = ?
                    WHERE schedule_id = ? AND state = 'running' AND queued_run_id IS NULL
                    """,
                    (now_iso, occurrence["scheduled_run_id"]),
                )
            conn.execute("COMMIT")
        return len(stale)

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

    def has_run_event(self, run_id: str, event_type: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM events WHERE run_id = ? AND event_type = ? LIMIT 1",
                (run_id, event_type),
            ).fetchone()
        return row is not None

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

    def list_owner_activity(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List recent workspace events without crossing the authenticated owner boundary."""
        bounded_limit = max(1, min(int(limit), 200))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    events.event_id,
                    events.project_id,
                    events.worker_id,
                    events.tenant_id,
                    events.run_id,
                    events.event_type,
                    events.created_at,
                    workers.name AS workspace_name,
                    workers.workspace_kind
                FROM events
                JOIN workers ON workers.worker_id = events.worker_id
                WHERE events.tenant_id = ?
                  AND workers.tenant_id = ?
                  AND workers.owner_id = ?
                ORDER BY events.created_at DESC, events.event_id DESC
                LIMIT ?
                """,
                (tenant_id or "local", tenant_id or "local", owner_id, bounded_limit),
            ).fetchall()
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
