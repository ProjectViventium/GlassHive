from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .models import utc_now
from .run_actions import RunActionError


_FAILURE_FIELD_NAMES = {
    "failure_class",
    "failure_retryable",
    "failure_structured",
    "failure_user_message",
    "failure_recommended_recovery",
    "failure_diagnostic_summary",
}
WORK_STOP_FIELD_NAMES = frozenset(
    {
        "work_stop_id",
        "work_stop_requested_at",
        "work_stop_settled_at",
        "work_stop_outcome",
    }
)
CANCELLATION_CLEAR_FIELDS: dict[str, Any] = {
    "failure_class": "",
    "failure_retryable": 0,
    "failure_structured": 0,
    "failure_user_message": "",
    "failure_recommended_recovery": "",
    "failure_diagnostic_summary": "",
    "retry_after": None,
    "retry_attempts": 0,
    "last_retry_class": "",
}

COMPUTE_OPERATION_KINDS = frozenset(
    {
        "idle",
        "needs_input",
        "paused",
        "pause_worker",
        "resume_worker",
        "max_duration",
        "pause_run",
        "resume_run",
        "interrupt_run",
        "steer_run",
        "stop_run",
        "terminate_worker",
    }
)
COMPUTE_OPERATION_SCOPES = frozenset({"compute_only", "run", "work", "worker"})
COMPUTE_OPERATION_SCOPE_BY_KIND = {
    "idle": "compute_only",
    "needs_input": "compute_only",
    "paused": "compute_only",
    "pause_worker": "compute_only",
    "resume_worker": "compute_only",
    "max_duration": "run",
    "pause_run": "run",
    "resume_run": "run",
    "interrupt_run": "run",
    "steer_run": "run",
    "stop_run": "work",
    "terminate_worker": "worker",
}
RUN_SCOPED_OPERATION_KINDS = frozenset(
    {
        "paused",
        "max_duration",
        "pause_run",
        "resume_run",
        "interrupt_run",
        "steer_run",
        "stop_run",
    }
)
STOPPING_OPERATION_KINDS = frozenset(
    {"max_duration", "stop_run", "terminate_worker"}
)
NONTERMINAL_RUN_STATES = frozenset(
    {"queued", "running", "settling", "paused", "needs_input"}
)
EXECUTING_RUN_STATES = frozenset(
    {"running", "settling", "paused", "needs_input"}
)
PROCESS_BEARING_RUN_STATES = frozenset({"running", "settling", "paused"})
TERMINAL_RUN_STATES = frozenset(
    {"completed", "failed", "cancelled", "interrupted"}
)
STEER_REPLACEMENT_SUPPRESSED_ERROR = (
    "Steer replacement suppressed because target completed"
)
RUNTIME_INFO_FIELD_NAMES = frozenset(
    {
        "runtime",
        "model",
        "gateway_url",
        "gateway_port",
        "gateway_token",
        "session_key",
        "state_dir",
        "workspace_dir",
        "pid",
        "takeover_url",
        "control_url",
        "last_error",
    }
)
COMPUTE_OPERATION_CLEAR_FIELDS: dict[str, Any] = {
    "compute_release_token": "",
    "compute_release_owner": "",
    "compute_release_claimed_at": None,
    "compute_release_expires_at": None,
    "compute_release_kind": "",
    "compute_release_scope": "compute_only",
    "compute_release_container_id": "",
    "compute_release_session_fingerprint": "",
    "compute_release_target_run_id": "",
    "compute_release_target_started_at": "",
    "compute_release_terminal_run_id": "",
    "compute_release_replacement_run_id": "",
    "compute_release_runtime_confirmed_at": None,
    "compute_release_runtime_proof_digest": "",
    "compute_release_operation_id": "",
}
LIFECYCLE_EFFECT_KINDS = frozenset(
    {
        "callback.run_cancelled",
        "callback.run_paused",
        "callback.run_resumed",
        "callback.run_resumed_in_place",
        "callback.run_resumed_queued",
        "callback.run_interrupted",
        "callback.run_steered",
        "callback.work_stopped",
        "callback.worker_paused",
        "callback.worker_resumed",
        "callback.worker_terminated",
        "signed_links.revoke_worker",
    }
)
LIFECYCLE_EFFECT_ERROR_CODES = frozenset(
    {
        "callback_config_missing",
        "callback_build_failed",
        "callback_enqueue_failed",
        "signed_link_revoke_failed",
        "transient_dependency",
        "unknown",
    }
)


class ProviderFamilyStoppedError(RuntimeError):
    """Raised when a durable graph-family Stop fence rejects a late start."""


class DelegationIdempotencyConflictError(RuntimeError):
    """Raised when a delegation key is reused for a different canonical request."""


class IsolatedParallelAdmissionConflictError(RuntimeError):
    """Raised when durable host work blocks an automatic isolated launch."""


class ActiveWorkActionConflictError(RuntimeError):
    """Raised when an active-work action key is reused for a different action."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "active_work_idempotency_conflict",
    ) -> None:
        super().__init__(message)
        self.code = str(code)


class WorkAdmissionError(RuntimeError):
    """Public-safe stable reason a work-producing mutation was fenced."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = str(code)


class HostRunLeaseCapacityError(RuntimeError):
    """Atomic persisted host admission rejection with a machine-readable class."""

    code = "host_capacity"

    def __init__(self, message: str, *, capacity_class: str) -> None:
        super().__init__(message)
        self.capacity_class = str(capacity_class or "host")


def _normalized_failure_fields(fields: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key in _FAILURE_FIELD_NAMES:
        if key not in fields:
            continue
        value = fields.get(key)
        if key in {"failure_retryable", "failure_structured"}:
            normalized[key] = 1 if bool(value) else 0
        else:
            normalized[key] = str(value or "")
    return normalized


def _terminal_failure_fields(state: str, fields: dict[str, Any]) -> dict[str, Any]:
    if state not in {"completed", "cancelled"}:
        return _normalized_failure_fields(fields)
    return {
        key: value
        for key, value in CANCELLATION_CLEAR_FIELDS.items()
        if key in _FAILURE_FIELD_NAMES
    }


class Store:
    def __init__(self, db_path: str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._harden_database_files()

    def _harden_database_files(self) -> None:
        """Keep local runtime state private even when upgrading an older database."""

        for candidate in (
            self.db_path,
            Path(f"{self.db_path}-wal"),
            Path(f"{self.db_path}-shm"),
            Path(f"{self.db_path}-journal"),
        ):
            try:
                if candidate.exists():
                    os.chmod(candidate, 0o600)
            except OSError:
                # A later connection will still fail normally if the database is
                # unreadable. Permission hardening must not mask that root error.
                continue

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30)
        try:
            # SQLite scopes foreign-key enforcement to each connection and
            # defaults it to disabled. Every Store path, including startup
            # migrations and reopened instances, must reject orphan writes and
            # honor the schema's declared referential actions.
            conn.execute("PRAGMA foreign_keys = ON")
            enabled = conn.execute("PRAGMA foreign_keys").fetchone()
            if enabled is None or int(enabled[0]) != 1:
                raise RuntimeError(
                    "SQLite foreign-key enforcement could not be enabled"
                )
            conn.row_factory = sqlite3.Row
            # sqlite3.Connection.__exit__ commits or rolls back but does not
            # close the descriptor. Keep that transaction behavior while the
            # Store context owns deterministic connection cleanup.
            with conn:
                yield conn
        finally:
            conn.close()

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
                    trusted_run_lane TEXT NOT NULL DEFAULT 'mission',
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
                    compute_release_token TEXT NOT NULL DEFAULT '',
                    compute_release_owner TEXT NOT NULL DEFAULT '',
                    compute_release_claimed_at TEXT,
                    compute_release_expires_at TEXT,
                    compute_release_epoch INTEGER NOT NULL DEFAULT 0,
                    compute_release_kind TEXT NOT NULL DEFAULT '',
                    compute_release_scope TEXT NOT NULL DEFAULT 'compute_only',
                    compute_release_container_id TEXT NOT NULL DEFAULT '',
                    compute_release_session_fingerprint TEXT NOT NULL DEFAULT '',
                    compute_release_target_run_id TEXT NOT NULL DEFAULT '',
                    compute_release_target_started_at TEXT NOT NULL DEFAULT '',
                    compute_release_terminal_run_id TEXT NOT NULL DEFAULT '',
                    compute_release_replacement_run_id TEXT NOT NULL DEFAULT '',
                    compute_release_runtime_confirmed_at TEXT,
                    compute_release_runtime_proof_digest TEXT NOT NULL DEFAULT '',
                    compute_release_operation_id TEXT NOT NULL DEFAULT '',
                    work_stop_id TEXT NOT NULL DEFAULT '',
                    work_stop_requested_at TEXT,
                    work_stop_settled_at TEXT,
                    work_stop_outcome TEXT NOT NULL DEFAULT '',
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
                    failure_structured INTEGER NOT NULL DEFAULT 0,
                    failure_user_message TEXT NOT NULL DEFAULT '',
                    failure_recommended_recovery TEXT NOT NULL DEFAULT '',
                    failure_diagnostic_summary TEXT NOT NULL DEFAULT '',
                    retry_after TEXT,
                    retry_attempts INTEGER NOT NULL DEFAULT 0,
                    last_retry_class TEXT NOT NULL DEFAULT '',
                    native_session_id TEXT NOT NULL DEFAULT '',
                    native_capabilities_json TEXT NOT NULL DEFAULT '{}',
                    native_child_summary_json TEXT NOT NULL DEFAULT '{}',
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
                    payload_json TEXT NOT NULL DEFAULT '{}',
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
                    http_accepted_at TEXT,
                    FOREIGN KEY(worker_id) REFERENCES workers(worker_id),
                    FOREIGN KEY(project_id) REFERENCES projects(project_id)
                );

                CREATE TABLE IF NOT EXISTS lifecycle_operation_effects (
                    effect_id TEXT PRIMARY KEY,
                    operation_digest TEXT NOT NULL,
                    operation_epoch INTEGER NOT NULL,
                    operation_kind TEXT NOT NULL,
                    effect_kind TEXT NOT NULL,
                    worker_id TEXT NOT NULL,
                    run_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    lease_owner TEXT NOT NULL DEFAULT '',
                    lease_epoch INTEGER NOT NULL DEFAULT 0,
                    lease_expires_at TEXT,
                    next_attempt_at TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error_code TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    applied_at TEXT,
                    UNIQUE(
                        operation_digest, operation_epoch, operation_kind,
                        effect_kind, worker_id, run_id
                    ),
                    FOREIGN KEY(worker_id) REFERENCES workers(worker_id)
                );

                CREATE TABLE IF NOT EXISTS capability_grant_revocations (
                    revocation_id TEXT PRIMARY KEY,
                    authorization_ref TEXT NOT NULL,
                    origin_ref TEXT NOT NULL,
                    work_ref TEXT NOT NULL,
                    worker_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    grant_id TEXT NOT NULL,
                    container_generation_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'armed',
                    lease_owner TEXT NOT NULL DEFAULT '',
                    lease_epoch INTEGER NOT NULL DEFAULT 0,
                    lease_expires_at TEXT,
                    next_attempt_at TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error_code TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    applied_at TEXT,
                    UNIQUE(grant_id, container_generation_id),
                    FOREIGN KEY(worker_id) REFERENCES workers(worker_id),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );

                CREATE INDEX IF NOT EXISTS capability_grant_revocations_pending_idx
                    ON capability_grant_revocations(
                        status, next_attempt_at, lease_expires_at, created_at
                    );

                CREATE TABLE IF NOT EXISTS run_action_uses (
                    capability_id TEXT PRIMARY KEY,
                    action TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    worker_id TEXT NOT NULL,
                    source_run_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_code TEXT NOT NULL DEFAULT '',
                    new_run_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(project_id) REFERENCES projects(project_id),
                    FOREIGN KEY(worker_id) REFERENCES workers(worker_id),
                    FOREIGN KEY(source_run_id) REFERENCES runs(run_id),
                    FOREIGN KEY(new_run_id) REFERENCES runs(run_id)
                );

                CREATE TABLE IF NOT EXISTS service_assertion_nonces (
                    audience TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    nonce TEXT NOT NULL,
                    issued_at_epoch INTEGER NOT NULL,
                    expires_at_epoch INTEGER NOT NULL,
                    request_method TEXT NOT NULL,
                    request_path TEXT NOT NULL,
                    consumed_at TEXT NOT NULL,
                    PRIMARY KEY (audience, tenant_id, owner_id, nonce)
                );

                CREATE TABLE IF NOT EXISTS delegations (
                    work_ref TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    origin_ref TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL,
                    origin_surface TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    worker_id TEXT NOT NULL,
                    initial_run_id TEXT NOT NULL,
                    current_run_id TEXT NOT NULL,
                    dismissed_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (tenant_id, owner_id, idempotency_key),
                    FOREIGN KEY(project_id) REFERENCES projects(project_id),
                    FOREIGN KEY(worker_id) REFERENCES workers(worker_id),
                    FOREIGN KEY(initial_run_id) REFERENCES runs(run_id),
                    FOREIGN KEY(current_run_id) REFERENCES runs(run_id)
                );

                CREATE TABLE IF NOT EXISTS active_work_action_uses (
                    action_use_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    work_ref TEXT NOT NULL,
                    source_run_id TEXT NOT NULL DEFAULT '',
                    effect_phase TEXT NOT NULL DEFAULT '',
                    lifecycle_operation_id TEXT NOT NULL DEFAULT '',
                    lifecycle_operation_kind TEXT NOT NULL DEFAULT '',
                    lifecycle_target_run_id TEXT NOT NULL DEFAULT '',
                    executor_id TEXT NOT NULL DEFAULT '',
                    lease_expires_at TEXT,
                    idempotency_key TEXT NOT NULL,
                    action TEXT NOT NULL,
                    payload_digest TEXT NOT NULL,
                    status TEXT NOT NULL,
                    response_json TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE (tenant_id, owner_id, work_ref, idempotency_key),
                    FOREIGN KEY(work_ref) REFERENCES delegations(work_ref)
                );

                CREATE TABLE IF NOT EXISTS host_run_leases (
                    lease_id TEXT PRIMARY KEY,
                    runtime_family TEXT NOT NULL,
                    lane TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    worker_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    executor_id TEXT NOT NULL,
                    pid INTEGER,
                    process_group INTEGER,
                    process_start_identity TEXT NOT NULL DEFAULT '',
                    startup_token TEXT NOT NULL DEFAULT '',
                    startup_state TEXT NOT NULL DEFAULT 'legacy_unknown',
                    startup_confirmed_at TEXT,
                    startup_identity_kind TEXT NOT NULL DEFAULT '',
                    startup_container_id TEXT NOT NULL DEFAULT '',
                    startup_session_id TEXT NOT NULL DEFAULT '',
                    mutation_scope TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    acquired_at TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    reconciled_at TEXT,
                    released_at TEXT,
                    release_reason TEXT NOT NULL DEFAULT '',
                    UNIQUE (run_id),
                    FOREIGN KEY(worker_id) REFERENCES workers(worker_id),
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
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
                    fallback_model_id TEXT NOT NULL DEFAULT '',
                    fallback_reasoning_effort TEXT NOT NULL DEFAULT '',
                    fallback_instruction TEXT NOT NULL DEFAULT '',
                    fallback_state TEXT NOT NULL DEFAULT '',
                    fallback_from_run_id TEXT NOT NULL DEFAULT '',
                    response_timeout_s REAL,
                    response_deadline_at TEXT NOT NULL DEFAULT '',
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

                CREATE TABLE IF NOT EXISTS provider_stop_tombstones (
                    tenant_id TEXT NOT NULL DEFAULT 'local',
                    owner_id TEXT NOT NULL,
                    base_idempotency_key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, owner_id, base_idempotency_key)
                );

                CREATE INDEX IF NOT EXISTS idx_workers_project_id ON workers(project_id);
                CREATE INDEX IF NOT EXISTS idx_runs_worker_state ON runs(worker_id, state, queued_at);
                CREATE INDEX IF NOT EXISTS idx_runs_state_retry_after_worker ON runs(state, retry_after, worker_id);
                CREATE INDEX IF NOT EXISTS idx_events_worker_created ON events(worker_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_callback_outbox_status_updated ON callback_outbox(status, updated_at);
                CREATE INDEX IF NOT EXISTS idx_lifecycle_effects_status_lease
                    ON lifecycle_operation_effects(status, lease_expires_at, created_at);
                CREATE INDEX IF NOT EXISTS idx_run_action_uses_source ON run_action_uses(source_run_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_service_assertion_nonces_expiry ON service_assertion_nonces(expires_at_epoch);
                CREATE INDEX IF NOT EXISTS idx_delegations_owner_updated ON delegations(tenant_id, owner_id, updated_at);
                CREATE INDEX IF NOT EXISTS idx_active_work_action_uses_work ON active_work_action_uses(work_ref, created_at);
                CREATE INDEX IF NOT EXISTS idx_host_run_leases_active_family_lane
                    ON host_run_leases(status, runtime_family, lane, heartbeat_at);
                CREATE INDEX IF NOT EXISTS idx_host_run_leases_active_owner
                    ON host_run_leases(status, tenant_id, owner_id, lane);
                CREATE INDEX IF NOT EXISTS idx_scheduled_runs_state_run_at ON scheduled_runs(state, run_at);
                CREATE INDEX IF NOT EXISTS idx_provider_sessions_owner ON provider_sessions(tenant_id, owner_id, updated_at);
                CREATE INDEX IF NOT EXISTS idx_provider_sessions_worker ON provider_sessions(worker_id);
                CREATE INDEX IF NOT EXISTS idx_provider_requests_session ON provider_requests(session_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_provider_activity_request ON provider_activity(request_id, sequence_id);
                CREATE INDEX IF NOT EXISTS idx_provider_stop_tombstones_expiry ON provider_stop_tombstones(expires_at);
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
            if "trusted_run_lane" not in worker_columns:
                conn.execute(
                    "ALTER TABLE workers ADD COLUMN trusted_run_lane TEXT NOT NULL DEFAULT 'mission'"
                )
            # Provider sessions are created only by the trusted conversation
            # provider. Backfill that durable linkage when upgrading a runtime
            # whose older workers carried only model-visible bootstrap metadata.
            conn.execute(
                """
                UPDATE workers
                SET trusted_run_lane = 'conversation'
                WHERE worker_id IN (SELECT worker_id FROM provider_sessions)
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_workers_execution_lane_state "
                "ON workers(execution_mode, trusted_run_lane, state)"
            )
            if "alias" not in worker_columns:
                conn.execute("ALTER TABLE workers ADD COLUMN alias TEXT")
            if "workspace_root" not in worker_columns:
                conn.execute("ALTER TABLE workers ADD COLUMN workspace_root TEXT")
            if "favorite" not in worker_columns:
                conn.execute("ALTER TABLE workers ADD COLUMN favorite INTEGER NOT NULL DEFAULT 0")
            if "compute_released_at" not in worker_columns:
                conn.execute("ALTER TABLE workers ADD COLUMN compute_released_at TEXT")
            if "compute_release_token" not in worker_columns:
                conn.execute(
                    "ALTER TABLE workers ADD COLUMN compute_release_token TEXT NOT NULL DEFAULT ''"
                )
            if "compute_release_owner" not in worker_columns:
                conn.execute(
                    "ALTER TABLE workers ADD COLUMN compute_release_owner TEXT NOT NULL DEFAULT ''"
                )
            if "compute_release_claimed_at" not in worker_columns:
                conn.execute(
                    "ALTER TABLE workers ADD COLUMN compute_release_claimed_at TEXT"
                )
            if "compute_release_expires_at" not in worker_columns:
                conn.execute(
                    "ALTER TABLE workers ADD COLUMN compute_release_expires_at TEXT"
                )
            if "compute_release_epoch" not in worker_columns:
                conn.execute(
                    "ALTER TABLE workers ADD COLUMN compute_release_epoch INTEGER NOT NULL DEFAULT 0"
                )
            if "compute_release_kind" not in worker_columns:
                conn.execute(
                    "ALTER TABLE workers ADD COLUMN compute_release_kind TEXT NOT NULL DEFAULT ''"
                )
            if "compute_release_scope" not in worker_columns:
                conn.execute(
                    "ALTER TABLE workers ADD COLUMN compute_release_scope TEXT NOT NULL DEFAULT 'compute_only'"
                )
            if "compute_release_container_id" not in worker_columns:
                conn.execute(
                    "ALTER TABLE workers ADD COLUMN compute_release_container_id TEXT NOT NULL DEFAULT ''"
                )
            if "compute_release_session_fingerprint" not in worker_columns:
                conn.execute(
                    "ALTER TABLE workers ADD COLUMN compute_release_session_fingerprint TEXT NOT NULL DEFAULT ''"
                )
            if "compute_release_target_run_id" not in worker_columns:
                conn.execute(
                    "ALTER TABLE workers ADD COLUMN compute_release_target_run_id TEXT NOT NULL DEFAULT ''"
                )
            if "compute_release_target_started_at" not in worker_columns:
                conn.execute(
                    "ALTER TABLE workers ADD COLUMN compute_release_target_started_at TEXT NOT NULL DEFAULT ''"
                )
            if "compute_release_terminal_run_id" not in worker_columns:
                conn.execute(
                    "ALTER TABLE workers ADD COLUMN compute_release_terminal_run_id TEXT NOT NULL DEFAULT ''"
                )
            if "compute_release_replacement_run_id" not in worker_columns:
                conn.execute(
                    "ALTER TABLE workers ADD COLUMN compute_release_replacement_run_id TEXT NOT NULL DEFAULT ''"
                )
            if "compute_release_runtime_confirmed_at" not in worker_columns:
                conn.execute(
                    "ALTER TABLE workers ADD COLUMN compute_release_runtime_confirmed_at TEXT"
                )
            if "compute_release_runtime_proof_digest" not in worker_columns:
                conn.execute(
                    "ALTER TABLE workers ADD COLUMN compute_release_runtime_proof_digest TEXT NOT NULL DEFAULT ''"
                )
            if "compute_release_operation_id" not in worker_columns:
                conn.execute(
                    "ALTER TABLE workers ADD COLUMN compute_release_operation_id TEXT NOT NULL DEFAULT ''"
                )
                conn.execute(
                    """
                    UPDATE workers
                    SET compute_release_operation_id = 'op_legacy_' || lower(hex(randomblob(16)))
                    WHERE compute_release_token != ''
                      AND compute_release_operation_id = ''
                    """
                )
            if "work_stop_id" not in worker_columns:
                conn.execute(
                    "ALTER TABLE workers ADD COLUMN work_stop_id TEXT NOT NULL DEFAULT ''"
                )
            if "work_stop_requested_at" not in worker_columns:
                conn.execute("ALTER TABLE workers ADD COLUMN work_stop_requested_at TEXT")
            if "work_stop_settled_at" not in worker_columns:
                conn.execute("ALTER TABLE workers ADD COLUMN work_stop_settled_at TEXT")
            if "work_stop_outcome" not in worker_columns:
                conn.execute(
                    "ALTER TABLE workers ADD COLUMN work_stop_outcome TEXT NOT NULL DEFAULT ''"
                )
            conn.execute(
                """
                UPDATE workers
                SET compute_release_scope = CASE compute_release_kind
                        WHEN 'max_duration' THEN 'run'
                        WHEN 'pause_run' THEN 'run'
                        WHEN 'resume_run' THEN 'run'
                        WHEN 'interrupt_run' THEN 'run'
                        WHEN 'steer_run' THEN 'run'
                        WHEN 'stop_run' THEN 'work'
                        WHEN 'terminate_worker' THEN 'worker'
                        ELSE 'compute_only'
                    END,
                    work_stop_id = CASE
                        WHEN compute_release_kind = 'stop_run'
                          AND compute_release_token != ''
                          AND work_stop_id = ''
                        THEN compute_release_token ELSE work_stop_id END,
                    work_stop_requested_at = CASE
                        WHEN compute_release_kind = 'stop_run'
                          AND compute_release_token != ''
                          AND work_stop_requested_at IS NULL
                        THEN COALESCE(compute_release_claimed_at, updated_at)
                        ELSE work_stop_requested_at END
                WHERE (
                    compute_release_token != ''
                    AND compute_release_kind IN (
                        'idle', 'paused', 'pause_worker', 'resume_worker',
                        'max_duration', 'pause_run', 'resume_run', 'interrupt_run',
                        'steer_run', 'stop_run', 'terminate_worker'
                    )
                    AND compute_release_scope != CASE compute_release_kind
                        WHEN 'max_duration' THEN 'run'
                        WHEN 'pause_run' THEN 'run'
                        WHEN 'resume_run' THEN 'run'
                        WHEN 'interrupt_run' THEN 'run'
                        WHEN 'steer_run' THEN 'run'
                        WHEN 'stop_run' THEN 'work'
                        WHEN 'terminate_worker' THEN 'worker'
                        ELSE 'compute_only'
                    END
                )
                   OR compute_release_scope NOT IN ('compute_only', 'run', 'work', 'worker')
                   OR (compute_release_kind = 'stop_run' AND compute_release_token != '')
                """
            )
            lifecycle_effect_columns = {
                row["name"]
                for row in conn.execute(
                    "PRAGMA table_info(lifecycle_operation_effects)"
                ).fetchall()
            }
            if "lease_epoch" not in lifecycle_effect_columns:
                conn.execute(
                    "ALTER TABLE lifecycle_operation_effects "
                    "ADD COLUMN lease_epoch INTEGER NOT NULL DEFAULT 0"
                )
            if "next_attempt_at" not in lifecycle_effect_columns:
                conn.execute(
                    "ALTER TABLE lifecycle_operation_effects "
                    "ADD COLUMN next_attempt_at TEXT"
                )
            lifecycle_effect_sql_row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' "
                "AND name = 'lifecycle_operation_effects'"
            ).fetchone()
            compact_effect_sql = "".join(
                str((lifecycle_effect_sql_row or {"sql": ""})["sql"] or "")
                .lower()
                .split()
            )
            full_effect_identity = (
                "unique(operation_digest,operation_epoch,operation_kind,"
                "effect_kind,worker_id,run_id)"
            )
            legacy_effect_table_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' "
                "AND name = 'lifecycle_operation_effects_legacy_identity'"
            ).fetchone() is not None
            if (
                full_effect_identity not in compact_effect_sql
                or legacy_effect_table_exists
            ):
                effect_columns_sql = """
                    effect_id, operation_digest, operation_epoch, operation_kind,
                    effect_kind, worker_id, run_id, status, lease_owner,
                    lease_epoch, lease_expires_at, next_attempt_at, attempts,
                    last_error_code, created_at, updated_at, applied_at
                """

                def merge_effect_rows(source_table: str, target_table: str) -> None:
                    source_columns = {
                        row["name"]
                        for row in conn.execute(
                            f"PRAGMA table_info({source_table})"
                        ).fetchall()
                    }
                    source_next_attempt = (
                        "next_attempt_at"
                        if "next_attempt_at" in source_columns
                        else "NULL AS next_attempt_at"
                    )
                    source_columns_sql = f"""
                        effect_id, operation_digest, operation_epoch,
                        operation_kind, effect_kind, worker_id, run_id, status,
                        lease_owner, lease_epoch, lease_expires_at,
                        {source_next_attempt}, attempts, last_error_code,
                        created_at, updated_at, applied_at
                    """
                    conn.execute(
                        f"""
                        INSERT OR IGNORE INTO {target_table} ({effect_columns_sql})
                        SELECT {source_columns_sql} FROM {source_table}
                        """
                    )
                    next_attempt_match = (
                        "target.next_attempt_at IS source.next_attempt_at"
                        if "next_attempt_at" in source_columns
                        else "target.next_attempt_at IS NULL"
                    )
                    mismatched = conn.execute(
                        f"""
                        SELECT COUNT(*)
                        FROM {source_table} AS source
                        LEFT JOIN {target_table} AS target
                          ON target.effect_id = source.effect_id
                        WHERE target.effect_id IS NULL
                           OR NOT (
                                target.operation_digest IS source.operation_digest
                            AND target.operation_epoch IS source.operation_epoch
                            AND target.operation_kind IS source.operation_kind
                            AND target.effect_kind IS source.effect_kind
                            AND target.worker_id IS source.worker_id
                            AND target.run_id IS source.run_id
                            AND target.status IS source.status
                            AND target.lease_owner IS source.lease_owner
                            AND target.lease_epoch IS source.lease_epoch
                            AND target.lease_expires_at IS source.lease_expires_at
                            AND {next_attempt_match}
                            AND target.attempts IS source.attempts
                            AND target.last_error_code IS source.last_error_code
                            AND target.created_at IS source.created_at
                            AND target.updated_at IS source.updated_at
                            AND target.applied_at IS source.applied_at
                           )
                        """
                    ).fetchone()[0]
                    if int(mismatched or 0):
                        raise RuntimeError(
                            "Lifecycle effect identity migration could not preserve every row"
                        )

                conn.execute("SAVEPOINT lifecycle_effect_identity_migration")
                try:
                    if full_effect_identity not in compact_effect_sql:
                        build_table = "lifecycle_operation_effects_full_identity"
                        conn.execute(
                            f"DROP TABLE IF EXISTS {build_table}"
                        )
                        conn.execute(
                            f"""
                            CREATE TABLE {build_table} (
                                effect_id TEXT PRIMARY KEY,
                                operation_digest TEXT NOT NULL,
                                operation_epoch INTEGER NOT NULL,
                                operation_kind TEXT NOT NULL,
                                effect_kind TEXT NOT NULL,
                                worker_id TEXT NOT NULL,
                                run_id TEXT NOT NULL DEFAULT '',
                                status TEXT NOT NULL DEFAULT 'pending',
                                lease_owner TEXT NOT NULL DEFAULT '',
                                lease_epoch INTEGER NOT NULL DEFAULT 0,
                                lease_expires_at TEXT,
                                next_attempt_at TEXT,
                                attempts INTEGER NOT NULL DEFAULT 0,
                                last_error_code TEXT NOT NULL DEFAULT '',
                                created_at TEXT NOT NULL,
                                updated_at TEXT NOT NULL,
                                applied_at TEXT,
                                UNIQUE(
                                    operation_digest, operation_epoch,
                                    operation_kind, effect_kind, worker_id, run_id
                                ),
                                FOREIGN KEY(worker_id) REFERENCES workers(worker_id)
                            )
                            """
                        )
                        merge_effect_rows(
                            "lifecycle_operation_effects", build_table
                        )
                        if legacy_effect_table_exists:
                            merge_effect_rows(
                                "lifecycle_operation_effects_legacy_identity",
                                build_table,
                            )
                        conn.execute(
                            "DROP INDEX IF EXISTS idx_lifecycle_effects_status_lease"
                        )
                        conn.execute("DROP TABLE lifecycle_operation_effects")
                        if legacy_effect_table_exists:
                            conn.execute(
                                "DROP TABLE lifecycle_operation_effects_legacy_identity"
                            )
                        conn.execute(
                            f"ALTER TABLE {build_table} "
                            "RENAME TO lifecycle_operation_effects"
                        )
                    elif legacy_effect_table_exists:
                        merge_effect_rows(
                            "lifecycle_operation_effects_legacy_identity",
                            "lifecycle_operation_effects",
                        )
                        conn.execute(
                            "DROP TABLE lifecycle_operation_effects_legacy_identity"
                        )
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_lifecycle_effects_status_lease "
                        "ON lifecycle_operation_effects("
                        "status, lease_expires_at, created_at)"
                    )
                    conn.execute("RELEASE lifecycle_effect_identity_migration")
                except Exception:
                    conn.execute(
                        "ROLLBACK TO lifecycle_effect_identity_migration"
                    )
                    conn.execute("RELEASE lifecycle_effect_identity_migration")
                    raise
            final_lifecycle_effect_columns = {
                row["name"]
                for row in conn.execute(
                    "PRAGMA table_info(lifecycle_operation_effects)"
                ).fetchall()
            }
            if "next_attempt_at" not in final_lifecycle_effect_columns:
                conn.execute(
                    "ALTER TABLE lifecycle_operation_effects "
                    "ADD COLUMN next_attempt_at TEXT"
                )
            run_columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
            if "tenant_id" not in run_columns:
                conn.execute("ALTER TABLE runs ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'local'")
            if "failure_class" not in run_columns:
                conn.execute("ALTER TABLE runs ADD COLUMN failure_class TEXT NOT NULL DEFAULT ''")
            if "failure_retryable" not in run_columns:
                conn.execute("ALTER TABLE runs ADD COLUMN failure_retryable INTEGER NOT NULL DEFAULT 0")
            if "failure_structured" not in run_columns:
                conn.execute("ALTER TABLE runs ADD COLUMN failure_structured INTEGER NOT NULL DEFAULT 0")
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
            if "native_session_id" not in run_columns:
                conn.execute("ALTER TABLE runs ADD COLUMN native_session_id TEXT NOT NULL DEFAULT ''")
            if "native_capabilities_json" not in run_columns:
                conn.execute("ALTER TABLE runs ADD COLUMN native_capabilities_json TEXT NOT NULL DEFAULT '{}'")
            if "native_child_summary_json" not in run_columns:
                conn.execute("ALTER TABLE runs ADD COLUMN native_child_summary_json TEXT NOT NULL DEFAULT '{}'")
            host_lease_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(host_run_leases)").fetchall()
            }
            if "mutation_scope" not in host_lease_columns:
                conn.execute(
                    "ALTER TABLE host_run_leases ADD COLUMN mutation_scope TEXT NOT NULL DEFAULT ''"
                )
            for column_name, definition in (
                ("startup_token", "TEXT NOT NULL DEFAULT ''"),
                ("startup_state", "TEXT NOT NULL DEFAULT 'legacy_unknown'"),
                ("startup_confirmed_at", "TEXT"),
                ("startup_identity_kind", "TEXT NOT NULL DEFAULT ''"),
                ("startup_container_id", "TEXT NOT NULL DEFAULT ''"),
                ("startup_session_id", "TEXT NOT NULL DEFAULT ''"),
            ):
                if column_name not in host_lease_columns:
                    conn.execute(
                        f"ALTER TABLE host_run_leases ADD COLUMN {column_name} {definition}"
                    )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_host_run_leases_active_mutation_scope "
                "ON host_run_leases(status, mutation_scope)"
            )
            event_columns = {row["name"] for row in conn.execute("PRAGMA table_info(events)").fetchall()}
            if "tenant_id" not in event_columns:
                conn.execute("ALTER TABLE events ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'local'")
            if "payload_json" not in event_columns:
                conn.execute("ALTER TABLE events ADD COLUMN payload_json TEXT NOT NULL DEFAULT '{}'")
            callback_columns = {row["name"] for row in conn.execute("PRAGMA table_info(callback_outbox)").fetchall()}
            if "tenant_id" not in callback_columns:
                conn.execute("ALTER TABLE callback_outbox ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'local'")
            if "http_accepted_at" not in callback_columns:
                conn.execute("ALTER TABLE callback_outbox ADD COLUMN http_accepted_at TEXT")
            delegation_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(delegations)").fetchall()
            }
            if "dismissed_at" not in delegation_columns:
                conn.execute("ALTER TABLE delegations ADD COLUMN dismissed_at TEXT")
            if "origin_ref" not in delegation_columns:
                conn.execute(
                    "ALTER TABLE delegations ADD COLUMN origin_ref TEXT NOT NULL DEFAULT ''"
                )
            active_work_action_columns = {
                row["name"]
                for row in conn.execute(
                    "PRAGMA table_info(active_work_action_uses)"
                ).fetchall()
            }
            if "source_run_id" not in active_work_action_columns:
                conn.execute(
                    "ALTER TABLE active_work_action_uses "
                    "ADD COLUMN source_run_id TEXT NOT NULL DEFAULT ''"
                )
            if "effect_phase" not in active_work_action_columns:
                conn.execute(
                    "ALTER TABLE active_work_action_uses "
                    "ADD COLUMN effect_phase TEXT NOT NULL DEFAULT ''"
                )
            if "lifecycle_operation_id" not in active_work_action_columns:
                conn.execute(
                    "ALTER TABLE active_work_action_uses "
                    "ADD COLUMN lifecycle_operation_id TEXT NOT NULL DEFAULT ''"
                )
            if "lifecycle_operation_kind" not in active_work_action_columns:
                conn.execute(
                    "ALTER TABLE active_work_action_uses "
                    "ADD COLUMN lifecycle_operation_kind TEXT NOT NULL DEFAULT ''"
                )
            if "lifecycle_target_run_id" not in active_work_action_columns:
                conn.execute(
                    "ALTER TABLE active_work_action_uses "
                    "ADD COLUMN lifecycle_target_run_id TEXT NOT NULL DEFAULT ''"
                )
            if "executor_id" not in active_work_action_columns:
                conn.execute(
                    "ALTER TABLE active_work_action_uses "
                    "ADD COLUMN executor_id TEXT NOT NULL DEFAULT ''"
                )
            if "lease_expires_at" not in active_work_action_columns:
                conn.execute(
                    "ALTER TABLE active_work_action_uses ADD COLUMN lease_expires_at TEXT"
                )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_delegations_owner_origin_ref "
                "ON delegations(tenant_id, owner_id, origin_ref) WHERE origin_ref <> ''"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_delegations_owner_active_order "
                "ON delegations(tenant_id, owner_id, dismissed_at, "
                "updated_at DESC, created_at DESC, work_ref DESC)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_projects_tenant_owner ON projects(tenant_id, owner_id, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_workers_tenant_owner ON workers(tenant_id, owner_id, project_id, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_tenant_project ON runs(tenant_id, project_id, queued_at)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_runs_state_retry_after_worker "
                "ON runs(state, retry_after, worker_id)"
            )
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
            provider_request_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(provider_requests)").fetchall()
            }
            for column_name in (
                "fallback_model_id",
                "fallback_reasoning_effort",
                "fallback_instruction",
                "fallback_state",
                "fallback_from_run_id",
                "response_deadline_at",
            ):
                if column_name not in provider_request_columns:
                    conn.execute(
                        f"ALTER TABLE provider_requests ADD COLUMN {column_name} TEXT NOT NULL DEFAULT ''"
                    )
            if "response_timeout_s" not in provider_request_columns:
                conn.execute("ALTER TABLE provider_requests ADD COLUMN response_timeout_s REAL")
            provider_request_info = {
                row["name"]: row
                for row in conn.execute(
                    "PRAGMA table_info(provider_requests)"
                ).fetchall()
            }
            if int(provider_request_info["run_id"]["notnull"] or 0):
                # SQLite cannot drop a NOT NULL constraint in place. Rebuild the
                # exact table transactionally so upgraded databases can create a
                # provider request before its native run has been assigned.
                # Rebuild the child activity table in the same savepoint. With
                # foreign keys enforced, dropping a populated parent first is
                # illegal; moving the child rows to a table bound to the new
                # parent preserves every relationship throughout the swap.
                conn.execute("SAVEPOINT provider_requests_nullable_run_id")
                try:
                    conn.execute("DROP TABLE IF EXISTS provider_activity_migrated")
                    conn.execute("DROP TABLE IF EXISTS provider_requests_migrated")
                    conn.execute(
                        """
                        CREATE TABLE provider_requests_migrated (
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
                            fallback_model_id TEXT NOT NULL DEFAULT '',
                            fallback_reasoning_effort TEXT NOT NULL DEFAULT '',
                            fallback_instruction TEXT NOT NULL DEFAULT '',
                            fallback_state TEXT NOT NULL DEFAULT '',
                            fallback_from_run_id TEXT NOT NULL DEFAULT '',
                            response_timeout_s REAL,
                            response_deadline_at TEXT NOT NULL DEFAULT '',
                            created_at TEXT NOT NULL,
                            updated_at TEXT NOT NULL,
                            UNIQUE (tenant_id, owner_id, idempotency_key),
                            FOREIGN KEY(session_id) REFERENCES provider_sessions(session_id),
                            FOREIGN KEY(run_id) REFERENCES runs(run_id)
                        )
                        """
                    )
                    conn.execute(
                        """
                        INSERT INTO provider_requests_migrated (
                            request_id, tenant_id, owner_id, session_id, run_id,
                            idempotency_key, message_id, stream_id, state,
                            requested_history_count, response_json,
                            fallback_model_id, fallback_reasoning_effort,
                            fallback_instruction, fallback_state,
                            fallback_from_run_id, response_timeout_s,
                            response_deadline_at, created_at, updated_at
                        )
                        SELECT
                            request_id, tenant_id, owner_id, session_id, run_id,
                            idempotency_key, message_id, stream_id, state,
                            requested_history_count, response_json,
                            fallback_model_id, fallback_reasoning_effort,
                            fallback_instruction, fallback_state,
                            fallback_from_run_id, response_timeout_s,
                            response_deadline_at, created_at, updated_at
                        FROM provider_requests
                        """
                    )
                    conn.execute(
                        """
                        CREATE TABLE provider_activity_migrated (
                            sequence_id INTEGER PRIMARY KEY AUTOINCREMENT,
                            request_id TEXT NOT NULL,
                            event_type TEXT NOT NULL,
                            summary TEXT NOT NULL,
                            payload_json TEXT NOT NULL DEFAULT '{}',
                            created_at TEXT NOT NULL,
                            FOREIGN KEY(request_id)
                                REFERENCES provider_requests_migrated(request_id)
                        )
                        """
                    )
                    conn.execute(
                        """
                        INSERT INTO provider_activity_migrated (
                            sequence_id, request_id, event_type, summary,
                            payload_json, created_at
                        )
                        SELECT
                            sequence_id, request_id, event_type, summary,
                            payload_json, created_at
                        FROM provider_activity
                        """
                    )
                    conn.execute("DROP INDEX IF EXISTS idx_provider_activity_request")
                    conn.execute("DROP TABLE provider_activity")
                    conn.execute("DROP TABLE provider_requests")
                    conn.execute(
                        "ALTER TABLE provider_requests_migrated RENAME TO provider_requests"
                    )
                    conn.execute(
                        "ALTER TABLE provider_activity_migrated "
                        "RENAME TO provider_activity"
                    )
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_provider_requests_session "
                        "ON provider_requests(session_id, created_at)"
                    )
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_provider_activity_request "
                        "ON provider_activity(request_id, sequence_id)"
                    )
                    conn.execute("RELEASE provider_requests_nullable_run_id")
                except Exception:
                    conn.execute("ROLLBACK TO provider_requests_nullable_run_id")
                    conn.execute("RELEASE provider_requests_nullable_run_id")
                    raise

            violation = conn.execute("PRAGMA foreign_key_check").fetchone()
            if violation is not None:
                raise RuntimeError(
                    "SQLite foreign-key integrity check failed "
                    "(one or more violations)"
                )

    def acquire_host_run_lease(
        self,
        *,
        runtime_family: str,
        lane: str,
        tenant_id: str,
        owner_id: str,
        worker_id: str,
        run_id: str,
        executor_id: str,
        conversation_limit: int,
        mission_limit: int,
        account_mission_limit: int,
        tenant_mission_limit: int,
        mutation_scope: str = "",
        lease_ttl_s: float,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Atomically admit one exact host run across every service process."""

        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        now_iso = current.isoformat()
        expires_at = (current + timedelta(seconds=max(1.0, float(lease_ttl_s)))).isoformat()
        clean_family = str(runtime_family or "host").strip().lower()
        clean_lane = "conversation" if str(lane).strip().lower() == "conversation" else "mission"
        clean_mutation_scope = str(mutation_scope or "").strip()
        startup_token = f"start_{uuid.uuid4().hex}"
        lane_limit = max(
            1,
            int(conversation_limit if clean_lane == "conversation" else mission_limit),
        )
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM host_run_leases WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if existing is not None and str(existing["status"] or "") == "active":
                if str(existing["executor_id"] or "") != str(executor_id):
                    conn.execute("ROLLBACK")
                    raise HostRunLeaseCapacityError(
                        "The exact host run is already owned by another live executor.",
                        capacity_class="exact_run_owned",
                    )
                conn.execute("COMMIT")
                return {**dict(existing), "idempotent_replay": True}

            family_lane_count = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM host_run_leases
                    WHERE status = 'active' AND runtime_family = ? AND lane = ?
                    """,
                    (clean_family, clean_lane),
                ).fetchone()[0]
            )
            if family_lane_count >= lane_limit:
                conn.execute("ROLLBACK")
                raise HostRunLeaseCapacityError(
                    f"The {clean_family} {clean_lane} host lane is at its configured capacity.",
                    capacity_class="family_lane",
                )

            if clean_lane == "mission":
                account_count = int(
                    conn.execute(
                        """
                        SELECT COUNT(*) FROM host_run_leases
                        WHERE status = 'active' AND lane = 'mission'
                          AND tenant_id = ? AND owner_id = ?
                        """,
                        (tenant_id, owner_id),
                    ).fetchone()[0]
                )
                if account_count >= max(1, int(account_mission_limit)):
                    conn.execute("ROLLBACK")
                    raise HostRunLeaseCapacityError(
                        "The account is at its configured active mission capacity.",
                        capacity_class="account",
                    )
                tenant_count = int(
                    conn.execute(
                        """
                        SELECT COUNT(*) FROM host_run_leases
                        WHERE status = 'active' AND lane = 'mission' AND tenant_id = ?
                        """,
                        (tenant_id,),
                    ).fetchone()[0]
                )
                if tenant_count >= max(1, int(tenant_mission_limit)):
                    conn.execute("ROLLBACK")
                    raise HostRunLeaseCapacityError(
                        "The tenant is at its configured active mission capacity.",
                        capacity_class="tenant",
                    )
                if clean_mutation_scope:
                    mutation_owner = conn.execute(
                        """
                        SELECT run_id FROM host_run_leases
                        WHERE status = 'active' AND mutation_scope = ? AND run_id != ?
                        LIMIT 1
                        """,
                        (clean_mutation_scope, run_id),
                    ).fetchone()
                    if mutation_owner is not None:
                        conn.execute("ROLLBACK")
                        raise HostRunLeaseCapacityError(
                            "The target repository already has an active host mutation mission.",
                            capacity_class="mutation_scope",
                        )

            if existing is None:
                data = {
                    "lease_id": f"hrl_{uuid.uuid4().hex}",
                    "runtime_family": clean_family,
                    "lane": clean_lane,
                    "tenant_id": tenant_id,
                    "owner_id": owner_id,
                    "worker_id": worker_id,
                    "run_id": run_id,
                    "executor_id": executor_id,
                    "pid": None,
                    "process_group": None,
                    "process_start_identity": "",
                    "startup_token": startup_token,
                    "startup_state": "reserved",
                    "startup_confirmed_at": None,
                    "startup_identity_kind": "",
                    "startup_container_id": "",
                    "startup_session_id": "",
                    "mutation_scope": clean_mutation_scope,
                    "status": "active",
                    "acquired_at": now_iso,
                    "heartbeat_at": now_iso,
                    "expires_at": expires_at,
                    "reconciled_at": None,
                    "released_at": None,
                    "release_reason": "",
                }
                conn.execute(
                    """
                    INSERT INTO host_run_leases (
                        lease_id, runtime_family, lane, tenant_id, owner_id,
                        worker_id, run_id, executor_id, pid, process_group,
                        process_start_identity, startup_token, startup_state,
                        startup_confirmed_at, startup_identity_kind,
                        startup_container_id, startup_session_id,
                        mutation_scope, status, acquired_at, heartbeat_at,
                        expires_at, reconciled_at, released_at, release_reason
                    ) VALUES (
                        :lease_id, :runtime_family, :lane, :tenant_id, :owner_id,
                        :worker_id, :run_id, :executor_id, :pid, :process_group,
                        :process_start_identity, :startup_token, :startup_state,
                        :startup_confirmed_at, :startup_identity_kind,
                        :startup_container_id, :startup_session_id,
                        :mutation_scope, :status, :acquired_at, :heartbeat_at,
                        :expires_at, :reconciled_at, :released_at, :release_reason
                    )
                    """,
                    data,
                )
            else:
                data = {
                    **dict(existing),
                    "runtime_family": clean_family,
                    "lane": clean_lane,
                    "tenant_id": tenant_id,
                    "owner_id": owner_id,
                    "worker_id": worker_id,
                    "executor_id": executor_id,
                    "pid": None,
                    "process_group": None,
                    "process_start_identity": "",
                    "startup_token": startup_token,
                    "startup_state": "reserved",
                    "startup_confirmed_at": None,
                    "startup_identity_kind": "",
                    "startup_container_id": "",
                    "startup_session_id": "",
                    "mutation_scope": clean_mutation_scope,
                    "status": "active",
                    "acquired_at": now_iso,
                    "heartbeat_at": now_iso,
                    "expires_at": expires_at,
                    "reconciled_at": None,
                    "released_at": None,
                    "release_reason": "",
                }
                conn.execute(
                    """
                    UPDATE host_run_leases
                    SET runtime_family = :runtime_family, lane = :lane,
                        tenant_id = :tenant_id, owner_id = :owner_id,
                        worker_id = :worker_id, executor_id = :executor_id,
                        pid = :pid, process_group = :process_group,
                        process_start_identity = :process_start_identity,
                        startup_token = :startup_token,
                        startup_state = :startup_state,
                        startup_confirmed_at = :startup_confirmed_at,
                        startup_identity_kind = :startup_identity_kind,
                        startup_container_id = :startup_container_id,
                        startup_session_id = :startup_session_id,
                        mutation_scope = :mutation_scope,
                        status = :status, acquired_at = :acquired_at,
                        heartbeat_at = :heartbeat_at, expires_at = :expires_at,
                        reconciled_at = :reconciled_at, released_at = :released_at,
                        release_reason = :release_reason
                    WHERE lease_id = :lease_id
                    """,
                    data,
                )
            conn.execute("COMMIT")
        return {**data, "idempotent_replay": False}

    def get_host_run_lease(self, lease_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM host_run_leases WHERE lease_id = ?",
                (lease_id,),
            ).fetchone()
        return self._row(row)

    def validate_host_run_start_reservation(
        self,
        *,
        worker_id: str,
        run_id: str,
        run_started_at: str,
        lease_id: str,
        startup_token: str,
        executor_id: str,
    ) -> dict[str, Any] | None:
        """Re-read the exact pre-launch tuple while its worker lifecycle lock is held."""

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            worker = conn.execute(
                "SELECT * FROM workers WHERE worker_id = ?", (worker_id,)
            ).fetchone()
            run = conn.execute(
                "SELECT * FROM runs WHERE run_id = ? AND worker_id = ?",
                (run_id, worker_id),
            ).fetchone()
            lease = conn.execute(
                "SELECT * FROM host_run_leases WHERE lease_id = ? AND run_id = ?",
                (lease_id, run_id),
            ).fetchone()
            exact = bool(
                worker is not None
                and run is not None
                and lease is not None
                and str(run["state"] or "") == "running"
                and str(run["started_at"] or "") == str(run_started_at or "")
                and str(lease["status"] or "") == "active"
                and str(lease["worker_id"] or "") == str(worker_id)
                and str(lease["executor_id"] or "") == str(executor_id)
                and str(lease["startup_token"] or "") == str(startup_token)
                and str(lease["startup_state"] or "") == "reserved"
                and not str(worker["compute_release_token"] or "")
                and not str(worker["work_stop_id"] or "")
                and str(worker["state"] or "")
                not in {"paused", "needs_input", "stopping", "terminated"}
            )
            result = (
                {
                    "worker": self._row(worker),
                    "run": self._row(run),
                    "lease": self._row(lease),
                }
                if exact
                else None
            )
            conn.execute("COMMIT")
        return result

    def mark_host_run_start_termination_unconfirmed(
        self,
        *,
        lease_id: str,
        run_id: str,
        executor_id: str,
        startup_token: str,
    ) -> dict[str, Any] | None:
        """Fence an ambiguous spawned identity so it cannot be reused as pre-launch."""

        now = utc_now()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE host_run_leases
                SET startup_state = 'termination_unconfirmed', heartbeat_at = ?
                WHERE lease_id = ? AND run_id = ? AND executor_id = ?
                  AND startup_token = ? AND status = 'active'
                  AND startup_state = 'reserved'
                """,
                (now, lease_id, run_id, executor_id, startup_token),
            )
            if cursor.rowcount != 1:
                return None
            row = conn.execute(
                "SELECT * FROM host_run_leases WHERE lease_id = ?", (lease_id,)
            ).fetchone()
        return self._row(row)

    def confirm_host_run_start(
        self,
        *,
        worker_id: str,
        run_id: str,
        run_started_at: str,
        lease_id: str,
        startup_token: str,
        executor_id: str,
        identity_kind: str,
        pid: int | None,
        process_group: int | None,
        process_start_identity: str,
        container_id: str,
        session_id: str,
        callback_record: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Publish exact startup identity and its started evidence in one CAS."""

        clean_kind = str(identity_kind or "").strip().lower()
        clean_start_identity = str(process_start_identity or "").strip()
        clean_container_id = str(container_id or "").strip()
        clean_session_id = str(session_id or "").strip()
        clean_pid = int(pid or 0)
        clean_group = int(process_group or 0)
        if clean_kind == "host_process":
            valid_identity = bool(
                clean_pid > 0 and clean_start_identity and clean_session_id
            )
        elif clean_kind == "docker_session":
            valid_identity = bool(
                clean_container_id
                and clean_session_id
                and clean_pid > 0
                and clean_start_identity.startswith(
                    f"docker:{clean_container_id}:{clean_session_id}:{run_id}:"
                )
            )
        elif clean_kind == "in_process":
            valid_identity = bool(
                not clean_container_id
                and clean_pid == 0
                and clean_group == 0
                and not clean_start_identity
                and clean_session_id == "in-process"
            )
        else:
            valid_identity = False
        if not valid_identity:
            raise ValueError("Run startup confirmation requires an exact identity")

        fingerprint_material = "\0".join(
            (
                "glasshive.run-start.v1",
                clean_kind,
                clean_container_id,
                clean_session_id,
                str(clean_pid),
                clean_start_identity,
            )
        )
        fingerprint = hashlib.sha256(
            fingerprint_material.encode("utf-8")
        ).hexdigest()
        event_id = "evt_start_" + hashlib.sha256(
            str(startup_token).encode("utf-8")
        ).hexdigest()
        now = utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            worker = conn.execute(
                "SELECT * FROM workers WHERE worker_id = ?", (worker_id,)
            ).fetchone()
            run = conn.execute(
                "SELECT * FROM runs WHERE run_id = ? AND worker_id = ?",
                (run_id, worker_id),
            ).fetchone()
            lease = conn.execute(
                "SELECT * FROM host_run_leases WHERE lease_id = ? AND run_id = ?",
                (lease_id, run_id),
            ).fetchone()
            if worker is None or run is None or lease is None:
                conn.execute("COMMIT")
                return None
            exact_lease = bool(
                str(lease["status"] or "") == "active"
                and str(lease["worker_id"] or "") == str(worker_id)
                and str(lease["executor_id"] or "") == str(executor_id)
                and str(lease["startup_token"] or "") == str(startup_token)
            )
            exact_run = bool(
                str(run["state"] or "") == "running"
                and str(run["started_at"] or "") == str(run_started_at or "")
            )
            worker_allows_start = bool(
                not str(worker["compute_release_token"] or "")
                and not str(worker["work_stop_id"] or "")
                and str(worker["state"] or "")
                not in {"paused", "needs_input", "stopping", "terminated"}
            )
            if not exact_lease or not exact_run or not worker_allows_start:
                conn.execute("COMMIT")
                return None
            if str(lease["startup_state"] or "") == "confirmed":
                same_identity = bool(
                    str(lease["startup_identity_kind"] or "") == clean_kind
                    and int(lease["pid"] or 0) == clean_pid
                    and int(lease["process_group"] or 0) == clean_group
                    and str(lease["process_start_identity"] or "")
                    == clean_start_identity
                    and str(lease["startup_container_id"] or "")
                    == clean_container_id
                    and str(lease["startup_session_id"] or "") == clean_session_id
                )
                if not same_identity:
                    conn.execute("COMMIT")
                    return None
                event = conn.execute(
                    "SELECT * FROM events WHERE event_id = ?", (event_id,)
                ).fetchone()
                conn.execute("COMMIT")
                return {
                    "lease": self._row(lease),
                    "event": self._row(event),
                    "idempotent_replay": True,
                }
            if str(lease["startup_state"] or "") != "reserved":
                conn.execute("COMMIT")
                return None
            cursor = conn.execute(
                """
                UPDATE host_run_leases
                SET startup_state = 'confirmed', startup_confirmed_at = ?,
                    startup_identity_kind = ?, startup_container_id = ?,
                    startup_session_id = ?, pid = ?, process_group = ?,
                    process_start_identity = ?, heartbeat_at = ?
                WHERE lease_id = ? AND run_id = ? AND worker_id = ?
                  AND executor_id = ? AND startup_token = ?
                  AND status = 'active' AND startup_state = 'reserved'
                """,
                (
                    now,
                    clean_kind,
                    clean_container_id,
                    clean_session_id,
                    clean_pid or None,
                    clean_group or None,
                    clean_start_identity,
                    now,
                    lease_id,
                    run_id,
                    worker_id,
                    executor_id,
                    startup_token,
                ),
            )
            if cursor.rowcount != 1:
                conn.execute("ROLLBACK")
                return None
            conn.execute(
                "UPDATE workers SET state = 'running', updated_at = ? WHERE worker_id = ?",
                (now, worker_id),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO events (
                    event_id, project_id, worker_id, tenant_id, run_id,
                    event_type, message, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, 'run.started', ?, ?, ?)
                """,
                (
                    event_id,
                    run["project_id"],
                    worker_id,
                    run["tenant_id"],
                    run_id,
                    str(run["instruction"] or ""),
                    json.dumps(
                        {
                            "identityKind": clean_kind,
                            "identityFingerprint": fingerprint,
                        },
                        sort_keys=True,
                    ),
                    now,
                ),
            )
            callback = None
            if callback_record:
                callback_id = "cb_start_" + hashlib.sha256(
                    str(startup_token).encode("utf-8")
                ).hexdigest()
                callback_data = {
                    **callback_record,
                    "callback_id": callback_id,
                    "project_id": str(run["project_id"] or ""),
                    "worker_id": worker_id,
                    "tenant_id": str(run["tenant_id"] or "local"),
                    "run_id": run_id,
                    "event_type": "run.started",
                    "status": "pending",
                    "attempts": 0,
                    "last_error": "",
                    "created_at": now,
                    "updated_at": now,
                    "delivered_at": None,
                    "http_accepted_at": None,
                }
                conn.execute(
                    """
                    INSERT OR IGNORE INTO callback_outbox (
                        callback_id, project_id, worker_id, tenant_id, run_id,
                        event_type, url, payload_json, status, attempts, last_error,
                        created_at, updated_at, delivered_at, http_accepted_at
                    ) VALUES (
                        :callback_id, :project_id, :worker_id, :tenant_id, :run_id,
                        :event_type, :url, :payload_json, :status, :attempts, :last_error,
                        :created_at, :updated_at, :delivered_at, :http_accepted_at
                    )
                    """,
                    callback_data,
                )
                callback = conn.execute(
                    "SELECT * FROM callback_outbox WHERE callback_id = ?",
                    (callback_id,),
                ).fetchone()
            confirmed_lease = conn.execute(
                "SELECT * FROM host_run_leases WHERE lease_id = ?", (lease_id,)
            ).fetchone()
            event = conn.execute(
                "SELECT * FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
            conn.execute("COMMIT")
        return {
            "lease": self._row(confirmed_lease),
            "event": self._row(event),
            "callback": self._row(callback),
            "idempotent_replay": False,
        }

    def requeue_unconfirmed_host_run_start(
        self,
        *,
        worker_id: str,
        run_id: str,
        lease_id: str,
        startup_token: str,
        retry_after: str,
        error_text: str,
    ) -> dict[str, Any] | None:
        """Atomically retire one proven-absent startup generation and requeue its run."""

        now = utc_now()
        event_id = "evt_start_requeue_" + hashlib.sha256(
            str(startup_token).encode("utf-8")
        ).hexdigest()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            worker = conn.execute(
                "SELECT * FROM workers WHERE worker_id = ?", (worker_id,)
            ).fetchone()
            run = conn.execute(
                "SELECT * FROM runs WHERE run_id = ? AND worker_id = ?",
                (run_id, worker_id),
            ).fetchone()
            lease = conn.execute(
                "SELECT * FROM host_run_leases WHERE lease_id = ? AND run_id = ?",
                (lease_id, run_id),
            ).fetchone()
            exact = bool(
                worker is not None
                and run is not None
                and lease is not None
                and str(run["state"] or "") in {"running", "queued"}
                and str(lease["worker_id"] or "") == worker_id
                and str(lease["status"] or "") == "active"
                and str(lease["startup_state"] or "")
                in {"reserved", "termination_unconfirmed"}
                and str(lease["startup_token"] or "") == str(startup_token)
            )
            if not exact:
                conn.execute("COMMIT")
                return None
            conn.execute(
                """
                UPDATE host_run_leases
                SET status = 'released', released_at = ?, release_reason = ?,
                    reconciled_at = COALESCE(reconciled_at, ?)
                WHERE lease_id = ? AND run_id = ? AND status = 'active'
                  AND startup_state IN ('reserved', 'termination_unconfirmed')
                  AND startup_token = ?
                """,
                (
                    now,
                    "startup_generation_cleaned",
                    now,
                    lease_id,
                    run_id,
                    startup_token,
                ),
            )
            conn.execute(
                """
                UPDATE runs
                SET state = 'queued', ended_at = NULL, retry_after = ?,
                    retry_attempts = COALESCE(retry_attempts, 0) + 1,
                    last_retry_class = 'service_startup_fenced', error_text = ?,
                    failure_class = 'service_startup_fenced', failure_retryable = 1,
                    failure_structured = 1,
                    failure_user_message = ?, failure_recommended_recovery = ?,
                    failure_diagnostic_summary = ?
                WHERE run_id = ? AND worker_id = ? AND state IN ('running', 'queued')
                """,
                (
                    retry_after,
                    error_text,
                    "GlassHive safely recovered an interrupted worker startup and will retry.",
                    "No action is required unless this work remains queued.",
                    "Restart recovery cleaned the exact unconfirmed startup generation.",
                    run_id,
                    worker_id,
                ),
            )
            conn.execute(
                """
                UPDATE workers
                SET state = CASE
                        WHEN state IN ('paused', 'needs_input', 'stopping', 'terminated')
                        THEN state ELSE 'starting' END,
                    last_error = '', updated_at = ?
                WHERE worker_id = ?
                """,
                (now, worker_id),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO events (
                    event_id, project_id, worker_id, tenant_id, run_id,
                    event_type, message, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, 'run.requeued', ?, ?, ?)
                """,
                (
                    event_id,
                    run["project_id"],
                    worker_id,
                    run["tenant_id"],
                    run_id,
                    "Interrupted startup generation was safely cleaned; retry queued",
                    json.dumps(
                        {"failureClass": "service_startup_fenced"},
                        sort_keys=True,
                    ),
                    now,
                ),
            )
            recovered = conn.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            conn.execute("COMMIT")
        return self._row(recovered)

    def get_active_host_run_lease_for_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM host_run_leases WHERE run_id = ? AND status = 'active'",
                (run_id,),
            ).fetchone()
        return self._row(row)

    def has_unconfirmed_host_run_start(self, worker_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM host_run_leases
                WHERE worker_id = ? AND status = 'active'
                  AND startup_state = 'termination_unconfirmed'
                LIMIT 1
                """,
                (worker_id,),
            ).fetchone()
        return row is not None

    def list_active_host_run_leases(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM host_run_leases
                WHERE status = 'active'
                ORDER BY acquired_at ASC, lease_id ASC
                """
            ).fetchall()
        return self._rows(rows)

    def list_stale_host_run_leases(
        self,
        *,
        heartbeat_before: str,
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM host_run_leases
                WHERE status = 'active' AND heartbeat_at <= ?
                ORDER BY heartbeat_at ASC, lease_id ASC
                """,
                (heartbeat_before,),
            ).fetchall()
        return self._rows(rows)

    def heartbeat_host_run_lease(
        self,
        lease_id: str,
        *,
        executor_id: str | None,
        pid: int | None = None,
        process_group: int | None = None,
        process_start_identity: str = "",
        startup_identity_kind: str = "",
        startup_container_id: str = "",
        startup_session_id: str = "",
        lease_ttl_s: float = 30.0,
        reconciled: bool = False,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        now_iso = current.isoformat()
        expires_at = (current + timedelta(seconds=max(1.0, float(lease_ttl_s)))).isoformat()
        clauses = [
            "lease_id = ?",
            "status = 'active'",
            """NOT EXISTS (
                SELECT 1 FROM runs
                WHERE runs.run_id = host_run_leases.run_id
                  AND runs.state IN ('completed', 'failed', 'cancelled')
            )""",
        ]
        params: list[Any] = [lease_id]
        if executor_id is not None:
            clauses.append("executor_id = ?")
            params.append(executor_id)
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE host_run_leases
                SET heartbeat_at = ?, expires_at = ?,
                    pid = COALESCE(?, pid),
                    process_group = COALESCE(?, process_group),
                    process_start_identity = CASE
                        WHEN ? != '' THEN ? ELSE process_start_identity END,
                    startup_identity_kind = CASE
                        WHEN startup_state = 'reserved' AND ? != ''
                        THEN ? ELSE startup_identity_kind END,
                    startup_container_id = CASE
                        WHEN startup_state = 'reserved' AND ? != ''
                        THEN ? ELSE startup_container_id END,
                    startup_session_id = CASE
                        WHEN startup_state = 'reserved' AND ? != ''
                        THEN ? ELSE startup_session_id END,
                    reconciled_at = CASE WHEN ? THEN ? ELSE reconciled_at END
                WHERE {' AND '.join(clauses)}
                """,
                (
                    now_iso,
                    expires_at,
                    pid,
                    process_group,
                    process_start_identity,
                    process_start_identity,
                    startup_identity_kind,
                    startup_identity_kind,
                    startup_container_id,
                    startup_container_id,
                    startup_session_id,
                    startup_session_id,
                    1 if reconciled else 0,
                    now_iso,
                    *params,
                ),
            )
            if cursor.rowcount != 1:
                return None
            row = conn.execute(
                "SELECT * FROM host_run_leases WHERE lease_id = ?",
                (lease_id,),
            ).fetchone()
        return self._row(row)

    def release_host_run_lease(
        self,
        lease_id: str,
        *,
        executor_id: str | None,
        reason: str,
    ) -> dict[str, Any] | None:
        now = utc_now()
        clauses = ["lease_id = ?", "status = 'active'"]
        params: list[Any] = [lease_id]
        if executor_id is not None:
            clauses.append("executor_id = ?")
            params.append(executor_id)
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE host_run_leases
                SET status = 'released', released_at = ?, release_reason = ?,
                    reconciled_at = COALESCE(reconciled_at, ?)
                WHERE {' AND '.join(clauses)}
                """,
                (now, str(reason or "released"), now, *params),
            )
            if cursor.rowcount == 0:
                row = conn.execute(
                    "SELECT * FROM host_run_leases WHERE lease_id = ?",
                    (lease_id,),
                ).fetchone()
                return self._row(row)
            row = conn.execute(
                "SELECT * FROM host_run_leases WHERE lease_id = ?",
                (lease_id,),
            ).fetchone()
        return self._row(row)

    def consume_service_assertion_nonce(
        self,
        *,
        audience: str,
        tenant_id: str,
        owner_id: str,
        nonce: str,
        issued_at_epoch: int,
        expires_at_epoch: int,
        request_method: str,
        request_path: str,
        now_epoch: int | None = None,
    ) -> bool:
        current_epoch = int(datetime.now(timezone.utc).timestamp()) if now_epoch is None else int(now_epoch)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM service_assertion_nonces WHERE expires_at_epoch < ?",
                (current_epoch,),
            )
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO service_assertion_nonces (
                    audience, tenant_id, owner_id, nonce, issued_at_epoch,
                    expires_at_epoch, request_method, request_path, consumed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    audience,
                    tenant_id,
                    owner_id,
                    nonce,
                    int(issued_at_epoch),
                    int(expires_at_epoch),
                    str(request_method or "").upper(),
                    request_path,
                    utc_now(),
                ),
            )
            conn.execute("COMMIT")
        return cursor.rowcount == 1

    @staticmethod
    def _delegation_select() -> str:
        return """
            SELECT
                delegations.*,
                projects.status AS project_status,
                workers.state AS worker_state,
                workers.profile AS worker_profile,
                workers.execution_mode AS worker_execution_mode,
                workers.last_error AS worker_last_error,
                runs.run_id AS run_id,
                runs.state AS run_state,
                runs.queued_at AS run_queued_at,
                runs.started_at AS run_started_at,
                runs.ended_at AS run_ended_at,
                runs.output_text AS run_output_text,
                runs.error_text AS run_error_text,
                runs.failure_class AS run_failure_class,
                runs.failure_retryable AS run_failure_retryable,
                runs.failure_user_message AS run_failure_user_message,
                runs.failure_recommended_recovery AS run_failure_recommended_recovery
                , runs.retry_after AS run_retry_after
                , runs.native_session_id AS run_native_session_id
                , runs.native_capabilities_json AS run_native_capabilities_json
                , runs.native_child_summary_json AS run_native_child_summary_json
            FROM delegations
            JOIN projects ON projects.project_id = delegations.project_id
            JOIN workers ON workers.worker_id = delegations.worker_id
            LEFT JOIN runs ON runs.run_id = COALESCE(
                (
                    SELECT active_run.run_id
                    FROM runs AS active_run
                    WHERE active_run.worker_id = delegations.worker_id
                      AND active_run.state IN (
                          'running', 'settling', 'queued', 'paused', 'needs_input'
                      )
                    ORDER BY
                        CASE active_run.state
                            WHEN 'running' THEN 0
                            WHEN 'settling' THEN 1
                            WHEN 'paused' THEN 2
                            WHEN 'needs_input' THEN 3
                            ELSE 4
                        END,
                        active_run.queued_at DESC
                    LIMIT 1
                ),
                delegations.current_run_id
            )
        """

    def reserve_delegation(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        idempotency_key: str,
        request_digest: str,
        origin_ref: str,
        title: str,
        goal: str,
        instruction: str,
        origin_surface: str,
        worker_name: str,
        worker_role: str,
        profile: str,
        backend: str,
        runtime: str,
        model: str,
        execution_mode: str,
        alias: str | None = None,
        workspace_root: str | None = None,
        bootstrap_profile: str | None = None,
        bootstrap_bundle: dict[str, Any] | None = None,
        require_isolated_parallel_ready: bool = False,
    ) -> dict[str, Any]:
        now = utc_now()
        work_ref = f"work_{uuid.uuid4().hex}"
        project_id = f"prj_{uuid.uuid4().hex[:10]}"
        worker_id = f"wrk_{uuid.uuid4().hex[:10]}"
        run_id = f"run_{uuid.uuid4().hex[:10]}"
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if require_isolated_parallel_ready:
                active_host = int(
                    conn.execute(
                        """
                        SELECT COUNT(DISTINCT worker_id)
                        FROM (
                            SELECT workers.worker_id AS worker_id
                            FROM workers
                            JOIN runs ON runs.worker_id = workers.worker_id
                            WHERE workers.execution_mode = 'host'
                              AND workers.trusted_run_lane = 'mission'
                              AND runs.state IN (
                                  'queued', 'running', 'settling', 'paused', 'needs_input'
                              )
                            UNION
                            SELECT leases.worker_id AS worker_id
                            FROM host_run_leases AS leases
                            JOIN workers ON workers.worker_id = leases.worker_id
                            WHERE leases.status = 'active'
                              AND workers.execution_mode = 'host'
                              AND workers.trusted_run_lane = 'mission'
                        ) AS active_host_missions
                        """
                    ).fetchone()[0]
                    or 0
                )
                if active_host:
                    conn.execute("ROLLBACK")
                    raise IsolatedParallelAdmissionConflictError(
                        "Existing host-native mission work blocks isolated Parallel admission."
                    )
            existing = conn.execute(
                """
                SELECT * FROM delegations
                WHERE tenant_id = ? AND owner_id = ? AND idempotency_key = ?
                """,
                (tenant_id, owner_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                if str(existing["request_digest"] or "") != request_digest:
                    conn.execute("ROLLBACK")
                    raise DelegationIdempotencyConflictError(
                        "The delegation idempotency key was reused with a different request."
                    )
                existing_ref = str(existing["work_ref"])
                conn.execute("COMMIT")
                record = self.get_delegation(existing_ref, tenant_id=tenant_id, owner_id=owner_id)
                if not record:
                    raise RuntimeError("The prior delegation reservation is unavailable")
                return {**record, "idempotent_replay": True}

            if origin_ref:
                origin_binding = conn.execute(
                    """
                    SELECT work_ref FROM delegations
                    WHERE tenant_id = ? AND owner_id = ? AND origin_ref = ?
                    """,
                    (tenant_id, owner_id, origin_ref),
                ).fetchone()
                if origin_binding is not None:
                    conn.execute("ROLLBACK")
                    raise DelegationIdempotencyConflictError(
                        "The delegation origin reference is already bound to other work."
                    )

            project = {
                "project_id": project_id,
                "tenant_id": tenant_id,
                "owner_id": owner_id,
                "title": title,
                "goal": goal,
                "status": "active",
                "summary": "",
                "default_worker_profile": profile,
                "created_at": now,
                "updated_at": now,
            }
            conn.execute(
                """
                INSERT INTO projects (
                    project_id, tenant_id, owner_id, title, goal, status, summary,
                    default_worker_profile, created_at, updated_at
                ) VALUES (
                    :project_id, :tenant_id, :owner_id, :title, :goal, :status, :summary,
                    :default_worker_profile, :created_at, :updated_at
                )
                """,
                project,
            )
            worker = {
                "worker_id": worker_id,
                "project_id": project_id,
                "tenant_id": tenant_id,
                "owner_id": owner_id,
                "name": worker_name,
                "role": worker_role,
                "profile": profile,
                "backend": backend,
                "execution_mode": execution_mode,
                "trusted_run_lane": "mission",
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
                "last_run_id": run_id,
                "last_error": None,
                "created_at": now,
                "updated_at": now,
            }
            conn.execute(
                """
                INSERT INTO workers (
                    worker_id, project_id, tenant_id, owner_id, name, role, profile,
                    backend, execution_mode, trusted_run_lane, alias, runtime, model, state,
                    bootstrap_profile, bootstrap_bundle_json, gateway_url, takeover_url,
                    control_url, gateway_port, gateway_token, session_key, state_dir,
                    workspace_dir, workspace_root, favorite, compute_released_at, pid,
                    last_run_id, last_error, created_at, updated_at
                ) VALUES (
                    :worker_id, :project_id, :tenant_id, :owner_id, :name, :role, :profile,
                    :backend, :execution_mode, :trusted_run_lane, :alias, :runtime, :model, :state,
                    :bootstrap_profile, :bootstrap_bundle_json, :gateway_url, :takeover_url,
                    :control_url, :gateway_port, :gateway_token, :session_key, :state_dir,
                    :workspace_dir, :workspace_root, :favorite, :compute_released_at, :pid,
                    :last_run_id, :last_error, :created_at, :updated_at
                )
                """,
                worker,
            )
            run = {
                "run_id": run_id,
                "worker_id": worker_id,
                "project_id": project_id,
                "tenant_id": tenant_id,
                "instruction": instruction,
                "state": "queued",
                "queued_at": now,
                "started_at": None,
                "ended_at": None,
                "output_text": "",
                "error_text": "",
                "failure_class": "",
                "failure_retryable": 0,
                "failure_structured": 0,
                "failure_user_message": "",
                "failure_recommended_recovery": "",
                "failure_diagnostic_summary": "",
                "retry_after": None,
                "retry_attempts": 0,
                "last_retry_class": "",
            }
            conn.execute(
                """
                INSERT INTO runs (
                    run_id, worker_id, project_id, tenant_id, instruction, state,
                    queued_at, started_at, ended_at, output_text, error_text,
                    failure_class, failure_retryable, failure_structured,
                    failure_user_message, failure_recommended_recovery,
                    failure_diagnostic_summary, retry_after, retry_attempts,
                    last_retry_class
                ) VALUES (
                    :run_id, :worker_id, :project_id, :tenant_id, :instruction, :state,
                    :queued_at, :started_at, :ended_at, :output_text, :error_text,
                    :failure_class, :failure_retryable, :failure_structured,
                    :failure_user_message, :failure_recommended_recovery,
                    :failure_diagnostic_summary, :retry_after, :retry_attempts,
                    :last_retry_class
                )
                """,
                run,
            )
            delegation = {
                "work_ref": work_ref,
                "tenant_id": tenant_id,
                "owner_id": owner_id,
                "idempotency_key": idempotency_key,
                "request_digest": request_digest,
                "origin_ref": origin_ref,
                "title": title,
                "origin_surface": origin_surface,
                "project_id": project_id,
                "worker_id": worker_id,
                "initial_run_id": run_id,
                "current_run_id": run_id,
                "dismissed_at": None,
                "created_at": now,
                "updated_at": now,
            }
            conn.execute(
                """
                INSERT INTO delegations (
                    work_ref, tenant_id, owner_id, idempotency_key, request_digest,
                    origin_ref, title, origin_surface, project_id, worker_id, initial_run_id,
                    current_run_id, dismissed_at, created_at, updated_at
                ) VALUES (
                    :work_ref, :tenant_id, :owner_id, :idempotency_key, :request_digest,
                    :origin_ref, :title, :origin_surface, :project_id, :worker_id, :initial_run_id,
                    :current_run_id, :dismissed_at, :created_at, :updated_at
                )
                """,
                delegation,
            )
            for event_type, event_run_id, message in (
                ("worker.created", None, f"Worker {worker_name} created"),
                ("run.queued", run_id, instruction),
            ):
                conn.execute(
                    """
                    INSERT INTO events (
                        event_id, project_id, worker_id, tenant_id, run_id,
                        event_type, message, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"evt_{uuid.uuid4().hex[:12]}",
                        project_id,
                        worker_id,
                        tenant_id,
                        event_run_id,
                        event_type,
                        message,
                        now,
                    ),
                )
            conn.execute("COMMIT")
        record = self.get_delegation(work_ref, tenant_id=tenant_id, owner_id=owner_id)
        if not record:
            raise RuntimeError("The delegation reservation was not readable after commit")
        return {**record, "idempotent_replay": False}

    def get_delegation(
        self,
        work_ref: str,
        *,
        tenant_id: str,
        owner_id: str,
    ) -> dict[str, Any] | None:
        query = self._delegation_select() + """
            WHERE delegations.work_ref = ?
              AND delegations.tenant_id = ?
              AND delegations.owner_id = ?
            LIMIT 1
        """
        with self._connect() as conn:
            row = conn.execute(query, (work_ref, tenant_id, owner_id)).fetchone()
        return self._row(row)

    def get_delegation_by_idempotency_key(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        """Read one committed reservation without consulting mutable admission state."""

        query = self._delegation_select() + """
            WHERE delegations.tenant_id = ?
              AND delegations.owner_id = ?
              AND delegations.idempotency_key = ?
            LIMIT 1
        """
        with self._connect() as conn:
            row = conn.execute(
                query,
                (tenant_id, owner_id, idempotency_key),
            ).fetchone()
        return self._row(row)

    def list_active_delegations(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        limit: int = 50,
        before: tuple[str, str, str] | None = None,
    ) -> list[dict[str, Any]]:
        query = self._delegation_select() + """
            WHERE delegations.tenant_id = ?
              AND delegations.owner_id = ?
              AND delegations.dismissed_at IS NULL
        """
        params: list[Any] = [tenant_id, owner_id]
        if before is not None:
            updated_at, created_at, work_ref = before
            query += """
              AND (
                    delegations.updated_at < ?
                 OR (delegations.updated_at = ? AND delegations.created_at < ?)
                 OR (
                        delegations.updated_at = ?
                    AND delegations.created_at = ?
                    AND delegations.work_ref < ?
                 )
              )
            """
            params.extend(
                [updated_at, updated_at, created_at, updated_at, created_at, work_ref]
            )
        query += """
            ORDER BY delegations.updated_at DESC, delegations.created_at DESC,
                     delegations.work_ref DESC
            LIMIT ?
        """
        params.append(max(1, min(int(limit), 100)))
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return self._rows(rows)

    def count_active_delegations(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        before: tuple[str, str, str] | None = None,
    ) -> int:
        query = """
            SELECT COUNT(*) FROM delegations
            WHERE delegations.tenant_id = ?
              AND delegations.owner_id = ?
              AND delegations.dismissed_at IS NULL
        """
        params: list[Any] = [tenant_id, owner_id]
        if before is not None:
            updated_at, created_at, work_ref = before
            query += """
              AND (
                    delegations.updated_at < ?
                 OR (delegations.updated_at = ? AND delegations.created_at < ?)
                 OR (
                        delegations.updated_at = ?
                    AND delegations.created_at = ?
                    AND delegations.work_ref < ?
                 )
              )
            """
            params.extend(
                [updated_at, updated_at, created_at, updated_at, created_at, work_ref]
            )
        with self._connect() as conn:
            row = conn.execute(query, params).fetchone()
        return int(row[0] if row else 0)

    def get_delegation_for_worker(
        self,
        worker_id: str,
        *,
        tenant_id: str,
        owner_id: str,
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM delegations
                WHERE worker_id = ? AND tenant_id = ? AND owner_id = ?
                LIMIT 1
                """,
                (worker_id, tenant_id, owner_id),
            ).fetchone()
        return self._row(row)

    def get_delegation_by_origin(
        self,
        origin_ref: str,
        *,
        tenant_id: str,
        owner_id: str,
    ) -> dict[str, Any] | None:
        query = self._delegation_select() + """
            WHERE delegations.origin_ref = ?
              AND delegations.tenant_id = ?
              AND delegations.owner_id = ?
            LIMIT 1
        """
        with self._connect() as conn:
            row = conn.execute(query, (origin_ref, tenant_id, owner_id)).fetchone()
        return self._row(row)

    def verify_callback_association(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        origin_ref: str,
        work_ref: str,
        worker_id: str,
        run_id: str,
    ) -> dict[str, Any] | None:
        """Resolve an exact mission/run binding without exposing partial matches."""

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT delegations.origin_ref, delegations.work_ref
                FROM delegations
                JOIN runs
                  ON runs.run_id = ?
                 AND runs.worker_id = delegations.worker_id
                 AND runs.project_id = delegations.project_id
                 AND runs.tenant_id = delegations.tenant_id
                WHERE delegations.tenant_id = ?
                  AND delegations.owner_id = ?
                  AND delegations.origin_ref = ?
                  AND delegations.work_ref = ?
                  AND delegations.worker_id = ?
                LIMIT 1
                """,
                (run_id, tenant_id, owner_id, origin_ref, work_ref, worker_id),
            ).fetchone()
        return self._row(row)

    def update_delegation_current_run(
        self,
        work_ref: str,
        *,
        tenant_id: str,
        owner_id: str,
        run_id: str,
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE delegations
                SET current_run_id = ?, dismissed_at = NULL, updated_at = ?
                WHERE work_ref = ? AND tenant_id = ? AND owner_id = ?
                """,
                (run_id, utc_now(), work_ref, tenant_id, owner_id),
            )
        return self.get_delegation(work_ref, tenant_id=tenant_id, owner_id=owner_id)

    def dismiss_delegation(
        self,
        work_ref: str,
        *,
        tenant_id: str,
        owner_id: str,
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE delegations
                SET dismissed_at = COALESCE(dismissed_at, ?), updated_at = ?
                WHERE work_ref = ? AND tenant_id = ? AND owner_id = ?
                """,
                (utc_now(), utc_now(), work_ref, tenant_id, owner_id),
            )
        return self.get_delegation(work_ref, tenant_id=tenant_id, owner_id=owner_id)

    def reserve_active_work_action(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        work_ref: str,
        idempotency_key: str,
        action: str,
        payload_digest: str,
        expected_current_run_id: str = "",
        expected_source_run_id: str = "",
        expected_source_state: str = "",
        expected_source_started_at: str = "",
        executor_id: str = "",
        lease_seconds: float = 30.0,
    ) -> dict[str, Any]:
        current = datetime.now(timezone.utc)
        now = current.isoformat()
        normalized_executor_id = str(executor_id or "").strip()
        lease_expires_at = (
            current + timedelta(seconds=max(1.0, float(lease_seconds)))
        ).isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            delegation = conn.execute(
                """
                SELECT current_run_id, worker_id FROM delegations
                WHERE work_ref = ? AND tenant_id = ? AND owner_id = ?
                """,
                (work_ref, tenant_id, owner_id),
            ).fetchone()
            if delegation is None:
                conn.execute("ROLLBACK")
                raise KeyError("Active work not found")
            selected_run = conn.execute(
                """
                SELECT * FROM runs
                WHERE worker_id = ?
                  AND state IN ('running', 'settling', 'queued', 'paused', 'needs_input')
                ORDER BY
                    CASE state
                        WHEN 'running' THEN 0
                        WHEN 'settling' THEN 1
                        WHEN 'paused' THEN 2
                        WHEN 'needs_input' THEN 3
                        ELSE 4
                    END,
                    queued_at DESC
                LIMIT 1
                """,
                (delegation["worker_id"],),
            ).fetchone()
            if selected_run is None and str(delegation["current_run_id"] or ""):
                # `_delegation_select()` falls back to the delegation's current
                # run when no controllable generation remains.  Keep action
                # reservation bound to that same terminal generation so Retry
                # and Dismiss reach their normal action policy, and a stale
                # nonterminal action reports "not available" rather than a
                # synthetic generation race.
                selected_run = conn.execute(
                    "SELECT * FROM runs WHERE run_id = ? AND worker_id = ?",
                    (
                        delegation["current_run_id"],
                        delegation["worker_id"],
                    ),
                ).fetchone()
            selected_source_run_id = str(
                (selected_run["run_id"] if selected_run is not None else None)
                or delegation["current_run_id"]
                or ""
            )
            clean_expected_current = str(expected_current_run_id or "").strip()
            clean_expected_source = str(expected_source_run_id or "").strip()
            clean_expected_state = str(expected_source_state or "").strip()
            clean_expected_started_at = str(expected_source_started_at or "").strip()
            source_generation_changed = bool(
                (
                    clean_expected_current
                    and clean_expected_current
                    != str(delegation["current_run_id"] or "")
                )
                or (
                    clean_expected_source
                    and clean_expected_source != selected_source_run_id
                )
                or (
                    clean_expected_state
                    and (
                        selected_run is None
                        or clean_expected_state != str(selected_run["state"] or "")
                    )
                )
                or (
                    clean_expected_source
                    and clean_expected_started_at
                    != str(
                        selected_run["started_at"] or ""
                        if selected_run is not None
                        else ""
                    )
                )
            )
            existing = conn.execute(
                """
                SELECT * FROM active_work_action_uses
                WHERE tenant_id = ? AND owner_id = ? AND work_ref = ? AND idempotency_key = ?
                """,
                (tenant_id, owner_id, work_ref, idempotency_key),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["action"] or "") != action
                    or str(existing["payload_digest"] or "") != payload_digest
                ):
                    conn.execute("ROLLBACK")
                    raise ActiveWorkActionConflictError(
                        "The active-work action idempotency key was reused with a different request."
                    )
                if str(existing["status"] or "") == "completed":
                    conn.execute("COMMIT")
                    return {
                        **self._row(existing),
                        "idempotent_replay": True,
                        "should_execute": False,
                        "recovery_takeover": False,
                    }
                if (
                    str(existing["status"] or "") == "failed"
                    and str(existing["response_json"] or "").strip()
                ):
                    conn.execute("COMMIT")
                    return {
                        **self._row(existing),
                        "idempotent_replay": True,
                        "should_execute": False,
                        "recovery_takeover": False,
                    }
                # Stop's permanent work tombstone fences every operation that can
                # create or control work. Dismiss is metadata-only and must remain
                # available after that tombstone so an acknowledged terminal card
                # can be hidden without reviving or deleting the work history.
                if action not in {"stop", "dismiss"}:
                    try:
                        self._require_worker_work_admission(
                            conn.execute(
                                "SELECT * FROM workers WHERE worker_id = ?",
                                (delegation["worker_id"],),
                            ).fetchone()
                        )
                    except WorkAdmissionError as exc:
                        conn.execute("ROLLBACK")
                        raise ActiveWorkActionConflictError(
                            str(exc), code=exc.code
                        ) from exc
                if not str(existing["source_run_id"] or ""):
                    conn.execute(
                        """
                        UPDATE active_work_action_uses
                        SET source_run_id = ?, updated_at = ?
                        WHERE action_use_id = ? AND source_run_id = ''
                        """,
                        (
                            selected_source_run_id,
                            now,
                            existing["action_use_id"],
                        ),
                    )
                    existing = conn.execute(
                        "SELECT * FROM active_work_action_uses WHERE action_use_id = ?",
                        (existing["action_use_id"],),
                    ).fetchone()
                if str(existing["status"] or "") == "failed":
                    conn.execute(
                        """
                        UPDATE active_work_action_uses
                        SET status = 'pending', last_error = '', executor_id = ?,
                            lease_expires_at = ?, updated_at = ?
                        WHERE action_use_id = ?
                        """,
                        (
                            normalized_executor_id,
                            lease_expires_at,
                            now,
                            existing["action_use_id"],
                        ),
                    )
                    existing = conn.execute(
                        "SELECT * FROM active_work_action_uses WHERE action_use_id = ?",
                        (existing["action_use_id"],),
                    ).fetchone()
                    conn.execute("COMMIT")
                    return {
                        **self._row(existing),
                        "idempotent_replay": True,
                        "should_execute": True,
                        "recovery_takeover": False,
                    }
                existing_executor = str(existing["executor_id"] or "")
                raw_expiry = str(existing["lease_expires_at"] or "")
                try:
                    lease_is_live = bool(raw_expiry) and datetime.fromisoformat(
                        raw_expiry
                    ) > current
                except ValueError:
                    lease_is_live = False
                can_take_over = bool(normalized_executor_id) and (
                    not existing_executor
                    or (existing_executor != normalized_executor_id and not lease_is_live)
                )
                if can_take_over:
                    conn.execute(
                        """
                        UPDATE active_work_action_uses
                        SET executor_id = ?, lease_expires_at = ?, updated_at = ?
                        WHERE action_use_id = ? AND status = 'pending'
                        """,
                        (
                            normalized_executor_id,
                            lease_expires_at,
                            now,
                            existing["action_use_id"],
                        ),
                    )
                    existing = conn.execute(
                        "SELECT * FROM active_work_action_uses WHERE action_use_id = ?",
                        (existing["action_use_id"],),
                    ).fetchone()
                    conn.execute("COMMIT")
                    return {
                        **self._row(existing),
                        "idempotent_replay": True,
                        "should_execute": True,
                        "recovery_takeover": True,
                    }
                conn.execute("COMMIT")
                return {
                    **self._row(existing),
                    "idempotent_replay": True,
                    "should_execute": False,
                    "recovery_takeover": False,
                }
            if source_generation_changed:
                conn.execute("ROLLBACK")
                raise ActiveWorkActionConflictError(
                    "The active-work run generation changed; refresh before retrying.",
                    code="active_work_generation_changed",
                )
            if action not in {"stop", "dismiss"}:
                try:
                    self._require_worker_work_admission(
                        conn.execute(
                            "SELECT * FROM workers WHERE worker_id = ?",
                            (delegation["worker_id"],),
                        ).fetchone()
                    )
                except WorkAdmissionError as exc:
                    conn.execute("ROLLBACK")
                    raise ActiveWorkActionConflictError(
                        str(exc), code=exc.code
                    ) from exc
            data = {
                "action_use_id": f"awu_{uuid.uuid4().hex}",
                "tenant_id": tenant_id,
                "owner_id": owner_id,
                "work_ref": work_ref,
                "source_run_id": selected_source_run_id,
                "effect_phase": "",
                "lifecycle_operation_id": "",
                "lifecycle_operation_kind": "",
                "lifecycle_target_run_id": "",
                "executor_id": normalized_executor_id,
                "lease_expires_at": lease_expires_at,
                "idempotency_key": idempotency_key,
                "action": action,
                "payload_digest": payload_digest,
                "status": "pending",
                "response_json": "",
                "last_error": "",
                "created_at": now,
                "updated_at": now,
            }
            conn.execute(
                """
                INSERT INTO active_work_action_uses (
                    action_use_id, tenant_id, owner_id, work_ref, source_run_id,
                    effect_phase, lifecycle_operation_id, lifecycle_operation_kind,
                    lifecycle_target_run_id, executor_id, lease_expires_at, idempotency_key,
                    action, payload_digest, status, response_json, last_error,
                    created_at, updated_at
                ) VALUES (
                    :action_use_id, :tenant_id, :owner_id, :work_ref, :source_run_id,
                    :effect_phase, :lifecycle_operation_id, :lifecycle_operation_kind,
                    :lifecycle_target_run_id, :executor_id, :lease_expires_at, :idempotency_key,
                    :action, :payload_digest, :status, :response_json, :last_error,
                    :created_at, :updated_at
                )
                """,
                data,
            )
            conn.execute("COMMIT")
        return {
            **data,
            "idempotent_replay": False,
            "should_execute": True,
            "recovery_takeover": False,
        }

    def bind_active_work_action_lifecycle(
        self,
        action_use_id: str,
        *,
        operation_id: str,
        operation_kind: str,
        target_run_id: str,
        executor_id: str,
    ) -> dict[str, Any] | None:
        """Bind a receipt once to the exact durable lifecycle operation it owns."""

        with self._connect() as conn:
            cursor = self._bind_active_work_action_lifecycle_in_transaction(
                conn,
                action_use_id=action_use_id,
                operation_id=operation_id,
                operation_kind=operation_kind,
                target_run_id=target_run_id,
                executor_id=executor_id,
            )
            if cursor != 1:
                return None
            row = conn.execute(
                "SELECT * FROM active_work_action_uses WHERE action_use_id = ?",
                (action_use_id,),
            ).fetchone()
        return self._row(row)

    @staticmethod
    def _bind_active_work_action_lifecycle_in_transaction(
        conn: sqlite3.Connection,
        *,
        action_use_id: str,
        operation_id: str,
        operation_kind: str,
        target_run_id: str,
        executor_id: str,
    ) -> int:
        """CAS-bind an owned receipt inside its lifecycle claim transaction."""

        clean_operation = str(operation_id or "").strip()
        clean_kind = str(operation_kind or "").strip().lower()
        clean_target = str(target_run_id or "").strip()
        expected_action = {
            "pause_run": "pause",
            "resume_run": "resume",
            "steer_run": "steer",
            "stop_run": "stop",
        }.get(clean_kind, "")
        if (
            not clean_operation
            or not expected_action
            or not clean_target
            or not str(executor_id or "").strip()
        ):
            raise ValueError("Action lifecycle binding requires an exact owned operation")
        cursor = conn.execute(
            """
            UPDATE active_work_action_uses
            SET lifecycle_operation_id = ?, lifecycle_operation_kind = ?,
                lifecycle_target_run_id = ?, updated_at = ?
            WHERE action_use_id = ? AND status = 'pending' AND executor_id = ?
              AND action = ? AND source_run_id = ?
              AND (
                    lifecycle_operation_id = ''
                    OR (
                        lifecycle_operation_id = ?
                        AND lifecycle_operation_kind = ?
                        AND lifecycle_target_run_id = ?
                    )
              )
            """,
            (
                clean_operation,
                clean_kind,
                clean_target,
                utc_now(),
                action_use_id,
                str(executor_id),
                expected_action,
                clean_target,
                clean_operation,
                clean_kind,
                clean_target,
            ),
        )
        return int(cursor.rowcount)

    def checkpoint_active_work_action(
        self,
        action_use_id: str,
        effect_phase: str,
        *,
        executor_id: str = "",
    ) -> dict[str, Any] | None:
        """Persist a completed subeffect before the next non-atomic action step."""

        with self._connect() as conn:
            owner_clause = " AND executor_id = ?" if executor_id else ""
            params: list[Any] = [str(effect_phase or "")[:100], utc_now(), action_use_id]
            if executor_id:
                params.append(str(executor_id))
            cursor = conn.execute(
                f"""
                UPDATE active_work_action_uses
                SET effect_phase = ?, updated_at = ?
                WHERE action_use_id = ? AND status = 'pending'
                {owner_clause}
                """,
                params,
            )
            if cursor.rowcount != 1:
                return None
            row = conn.execute(
                "SELECT * FROM active_work_action_uses WHERE action_use_id = ?",
                (action_use_id,),
            ).fetchone()
        return self._row(row)

    def resume_needs_input_active_work_action(
        self,
        action_use_id: str,
        *,
        worker_id: str,
        run_id: str,
        executor_id: str,
    ) -> dict[str, Any] | None:
        """Atomically re-admit an authorization-blocked run and its action proof."""

        now = utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            action = conn.execute(
                "SELECT * FROM active_work_action_uses WHERE action_use_id = ?",
                (action_use_id,),
            ).fetchone()
            worker = conn.execute(
                "SELECT * FROM workers WHERE worker_id = ?", (worker_id,)
            ).fetchone()
            run = conn.execute(
                "SELECT * FROM runs WHERE run_id = ? AND worker_id = ?",
                (run_id, worker_id),
            ).fetchone()
            if (
                action is None
                or worker is None
                or run is None
                or str(action["status"] or "") != "pending"
                or str(action["executor_id"] or "") != str(executor_id)
                or str(action["action"] or "") != "resume"
                or str(action["source_run_id"] or "") != str(run_id)
            ):
                conn.execute("ROLLBACK")
                return None
            operation_id = str(action["lifecycle_operation_id"] or "").strip()
            if operation_id:
                if (
                    str(action["lifecycle_operation_kind"] or "") != "resume_run"
                    or str(action["lifecycle_target_run_id"] or "") != str(run_id)
                ):
                    conn.execute("ROLLBACK")
                    return None
            else:
                operation_id = "op_" + uuid.uuid4().hex
            if str(run["state"] or "") == "needs_input":
                cursor = conn.execute(
                    """
                    UPDATE runs SET state = 'queued', ended_at = NULL,
                        error_text = '', retry_after = NULL
                    WHERE run_id = ? AND worker_id = ? AND state = 'needs_input'
                    """,
                    (run_id, worker_id),
                )
                if cursor.rowcount != 1:
                    conn.execute("ROLLBACK")
                    return None
                conn.execute(
                    "UPDATE workers SET state = 'starting', last_error = '', updated_at = ? "
                    "WHERE worker_id = ?",
                    (now, worker_id),
                )
            elif not (
                str(run["state"] or "") == "queued"
                and str(action["effect_phase"] or "")
                == "authorization_re_admitted"
            ):
                conn.execute("ROLLBACK")
                return None
            conn.execute(
                """
                UPDATE active_work_action_uses
                SET lifecycle_operation_id = ?, lifecycle_operation_kind = 'resume_run',
                    lifecycle_target_run_id = ?,
                    effect_phase = 'authorization_re_admitted', updated_at = ?
                WHERE action_use_id = ? AND status = 'pending' AND executor_id = ?
                """,
                (operation_id, run_id, now, action_use_id, str(executor_id)),
            )
            self._insert_lifecycle_event(
                conn,
                operation_token=operation_id,
                operation_epoch=0,
                operation_kind="resume_run",
                worker=worker,
                run_id=run_id,
                event_type="run.authorization_resumed",
                message="Authorization attention cleared; exact run queued for re-admission",
                payload={"operation_kind": "resume_run"},
            )
            self._enqueue_lifecycle_effects(
                conn,
                operation_token=operation_id,
                operation_epoch=0,
                operation_kind="resume_run",
                worker_id=worker_id,
                run_id=run_id,
                effect_kinds=("callback.run_resumed_queued",),
            )
            updated_run = conn.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            updated_worker = conn.execute(
                "SELECT * FROM workers WHERE worker_id = ?", (worker_id,)
            ).fetchone()
            conn.execute("COMMIT")
        return {"run": self._row(updated_run), "worker": self._row(updated_worker)}

    def get_active_work_action(self, action_use_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM active_work_action_uses WHERE action_use_id = ?",
                (action_use_id,),
            ).fetchone()
        return self._row(row)

    def release_active_work_action_leases(self, executor_id: str) -> int:
        """Make unfinished actions recoverable when this service exits cleanly."""

        normalized = str(executor_id or "").strip()
        if not normalized:
            return 0
        now = utc_now()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE active_work_action_uses
                SET lease_expires_at = ?, updated_at = ?
                WHERE executor_id = ? AND status = 'pending'
                """,
                (now, now, normalized),
            )
        return int(cursor.rowcount)

    def finish_active_work_action(
        self,
        action_use_id: str,
        *,
        response: dict[str, Any],
        current_run_id: str | None = None,
        executor_id: str = "",
    ) -> dict[str, Any] | None:
        now = utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM active_work_action_uses WHERE action_use_id = ?",
                (action_use_id,),
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return None
            if executor_id and str(row["executor_id"] or "") != str(executor_id):
                conn.execute("ROLLBACK")
                return None
            if str(row["status"] or "") != "pending":
                conn.execute("ROLLBACK")
                return None
            delegation = conn.execute(
                """
                SELECT current_run_id, updated_at FROM delegations
                WHERE work_ref = ? AND tenant_id = ? AND owner_id = ?
                """,
                (row["work_ref"], row["tenant_id"], row["owner_id"]),
            ).fetchone()
            canonical_response = dict(response)
            if (
                current_run_id
                and delegation is not None
                and str(delegation["current_run_id"] or "") != str(current_run_id)
            ):
                conn.execute(
                    """
                    UPDATE delegations
                    SET current_run_id = ?, updated_at = ?
                    WHERE work_ref = ? AND tenant_id = ? AND owner_id = ?
                    """,
                    (
                        current_run_id,
                        now,
                        row["work_ref"],
                        row["tenant_id"],
                        row["owner_id"],
                    ),
                )
                canonical_response["updatedAt"] = now
            elif delegation is not None:
                canonical_response["updatedAt"] = str(delegation["updated_at"] or "")
            cursor = conn.execute(
                """
                UPDATE active_work_action_uses
                SET status = 'completed', response_json = ?, last_error = '', updated_at = ?
                WHERE action_use_id = ? AND status = 'pending'
                """,
                (
                    json.dumps(
                        canonical_response, sort_keys=True, separators=(",", ":")
                    ),
                    now,
                    action_use_id,
                ),
            )
            if cursor.rowcount != 1:
                conn.execute("ROLLBACK")
                return None
            finished = conn.execute(
                "SELECT * FROM active_work_action_uses WHERE action_use_id = ?",
                (action_use_id,),
            ).fetchone()
            conn.execute("COMMIT")
        return self._row(finished)

    def fail_active_work_action(
        self,
        action_use_id: str,
        error: str,
        *,
        executor_id: str = "",
        failure_response: dict[str, Any] | None = None,
    ) -> bool:
        with self._connect() as conn:
            owner_clause = " AND executor_id = ?" if executor_id else ""
            serialized_response = (
                json.dumps(failure_response, sort_keys=True, separators=(",", ":"))
                if failure_response is not None
                else ""
            )
            params: list[Any] = [
                str(error or "")[:1000],
                serialized_response,
                utc_now(),
                action_use_id,
            ]
            if executor_id:
                params.append(str(executor_id))
            cursor = conn.execute(
                f"""
                UPDATE active_work_action_uses
                SET status = CASE WHEN effect_phase <> '' THEN 'pending' ELSE 'failed' END,
                    last_error = ?,
                    response_json = CASE
                        WHEN effect_phase = '' THEN ?
                        ELSE response_json
                    END,
                    updated_at = ?
                WHERE action_use_id = ? AND status = 'pending'{owner_clause}
                """,
                params,
            )
        return cursor.rowcount == 1

    def fail_unbound_active_work_control(
        self,
        action_use_id: str,
        *,
        executor_id: str,
    ) -> bool:
        """Permanently fail one owned legacy control receipt with no causal binding."""

        clean_executor = str(executor_id or "").strip()
        if not clean_executor:
            return False
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE active_work_action_uses
                SET status = 'failed',
                    last_error = 'active_work_action_binding_unavailable',
                    response_json = ?,
                    lease_expires_at = NULL, updated_at = ?
                WHERE action_use_id = ? AND status = 'pending'
                  AND executor_id = ?
                  AND lifecycle_operation_id = ''
                  AND action IN ('pause', 'resume', 'steer', 'stop')
                """,
                (
                    json.dumps(
                        {
                            "detail": {
                                "code": "active_work_action_binding_unavailable",
                                "message": (
                                    "This unfinished control predates durable lifecycle binding. "
                                    "Refresh and reissue it with a new idempotency key."
                                ),
                            },
                            "statusCode": 409,
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    utc_now(),
                    action_use_id,
                    clean_executor,
                ),
            )
        return cursor.rowcount == 1

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

    def get_provider_session_by_worker(self, worker_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM provider_sessions WHERE worker_id = ? ORDER BY updated_at DESC LIMIT 1",
                (worker_id,),
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
            linked_worker = conn.execute(
                """
                UPDATE workers
                SET trusted_run_lane = 'conversation', updated_at = ?
                WHERE worker_id = ? AND tenant_id = ? AND owner_id = ?
                """,
                (now, worker_id, tenant_id or "local", owner_id),
            )
            if linked_worker.rowcount != 1:
                raise ValueError("Provider session worker binding is invalid")
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

    def list_provider_requests_by_idempotency_family(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        base_idempotency_key: str,
    ) -> list[dict[str, Any]]:
        """Return one owner-scoped base request and its graph-execution children."""

        escaped = (
            str(base_idempotency_key or "")
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM provider_requests
                WHERE tenant_id = ?
                  AND owner_id = ?
                  AND (
                    idempotency_key = ?
                    OR idempotency_key LIKE ? ESCAPE '\\'
                  )
                ORDER BY created_at DESC, request_id DESC
                """,
                (
                    tenant_id,
                    owner_id,
                    base_idempotency_key,
                    f"{escaped}:graph:%",
                ),
            ).fetchall()
        return [self._row(row) for row in rows if row is not None]

    def upsert_provider_stop_tombstone(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        base_idempotency_key: str,
        ttl_seconds: float,
    ) -> dict[str, Any]:
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        expires_at = (now_dt + timedelta(seconds=max(0.001, float(ttl_seconds)))).isoformat()
        data = {
            "tenant_id": tenant_id or "local",
            "owner_id": owner_id,
            "base_idempotency_key": base_idempotency_key,
            "created_at": now,
            "expires_at": expires_at,
        }
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM provider_stop_tombstones WHERE expires_at <= ?",
                (now,),
            )
            conn.execute(
                """
                INSERT INTO provider_stop_tombstones (
                    tenant_id, owner_id, base_idempotency_key, created_at, expires_at
                ) VALUES (
                    :tenant_id, :owner_id, :base_idempotency_key, :created_at, :expires_at
                )
                ON CONFLICT (tenant_id, owner_id, base_idempotency_key)
                DO UPDATE SET created_at = excluded.created_at,
                              expires_at = excluded.expires_at
                """,
                data,
            )
            row = conn.execute(
                """
                SELECT * FROM provider_stop_tombstones
                WHERE tenant_id = ? AND owner_id = ? AND base_idempotency_key = ?
                """,
                (data["tenant_id"], owner_id, base_idempotency_key),
            ).fetchone()
            conn.execute("COMMIT")
        return self._row(row) or data

    def is_provider_stop_tombstone_active(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        idempotency_keys: tuple[str, ...],
    ) -> bool:
        keys = tuple(dict.fromkeys(str(key).strip() for key in idempotency_keys if str(key).strip()))
        if not keys:
            return False
        placeholders = ", ".join("?" for _ in keys)
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT 1 FROM provider_stop_tombstones
                WHERE tenant_id = ?
                  AND owner_id = ?
                  AND base_idempotency_key IN ({placeholders})
                  AND expires_at > ?
                LIMIT 1
                """,
                (tenant_id or "local", owner_id, *keys, utc_now()),
            ).fetchone()
        return row is not None

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
        fallback_model_id: str = "",
        fallback_reasoning_effort: str = "",
        fallback_instruction: str = "",
        response_timeout_s: float | None = None,
        response_deadline_at: str = "",
        base_idempotency_key: str = "",
    ) -> tuple[dict[str, Any], bool]:
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
            "fallback_model_id": str(fallback_model_id or "").strip(),
            "fallback_reasoning_effort": str(fallback_reasoning_effort or "").strip(),
            "fallback_instruction": str(fallback_instruction or ""),
            "fallback_state": "",
            "fallback_from_run_id": "",
            "response_timeout_s": (
                float(response_timeout_s) if response_timeout_s is not None else None
            ),
            "response_deadline_at": str(response_deadline_at or "").strip(),
            "created_at": now,
            "updated_at": now,
        }
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                stop_keys = tuple(
                    dict.fromkeys(
                        key
                        for key in (
                            str(base_idempotency_key or "").strip(),
                            str(idempotency_key or "").strip(),
                        )
                        if key
                    )
                )
                if stop_keys:
                    placeholders = ", ".join("?" for _ in stop_keys)
                    stopped = conn.execute(
                        f"""
                        SELECT 1 FROM provider_stop_tombstones
                        WHERE tenant_id = ?
                          AND owner_id = ?
                          AND base_idempotency_key IN ({placeholders})
                          AND expires_at > ?
                        LIMIT 1
                        """,
                        (tenant_id or "local", owner_id, *stop_keys, now),
                    ).fetchone()
                    if stopped:
                        conn.execute("ROLLBACK")
                        raise ProviderFamilyStoppedError(
                            "GlassHive request was cancelled before native execution started"
                        )
                existing = conn.execute(
                    """
                    SELECT * FROM provider_requests
                    WHERE tenant_id = ? AND owner_id = ? AND idempotency_key = ?
                    """,
                    (tenant_id or "local", owner_id, idempotency_key),
                ).fetchone()
                if existing:
                    conn.execute("COMMIT")
                    return self._row(existing) or data, False
                conn.execute(
                    """
                    INSERT INTO provider_requests (
                        request_id, tenant_id, owner_id, session_id, run_id, idempotency_key,
                        message_id, stream_id, state, requested_history_count, response_json,
                        fallback_model_id, fallback_reasoning_effort, fallback_instruction,
                        fallback_state, fallback_from_run_id, response_timeout_s,
                        response_deadline_at,
                        created_at, updated_at
                    ) VALUES (
                        :request_id, :tenant_id, :owner_id, :session_id, :run_id, :idempotency_key,
                        :message_id, :stream_id, :state, :requested_history_count, :response_json,
                        :fallback_model_id, :fallback_reasoning_effort, :fallback_instruction,
                        :fallback_state, :fallback_from_run_id, :response_timeout_s,
                        :response_deadline_at,
                        :created_at, :updated_at
                    )
                    """,
                    data,
                )
                conn.execute("COMMIT")
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

    def claim_provider_request_fallback(
        self,
        request_id: str,
        *,
        expected_run_id: str,
    ) -> dict[str, Any] | None:
        """Durably elect one serial-fallback starter across pollers/processes."""

        now = utc_now()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE provider_requests
                SET fallback_state = 'claimed',
                    fallback_from_run_id = :expected_run_id,
                    updated_at = :updated_at
                WHERE request_id = :request_id
                  AND run_id = :expected_run_id
                  AND fallback_state = ''
                  AND state IN ('queued', 'running')
                """,
                {
                    "request_id": request_id,
                    "expected_run_id": expected_run_id,
                    "updated_at": now,
                },
            )
            if cursor.rowcount != 1:
                return None
            row = conn.execute(
                "SELECT * FROM provider_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        return self._row(row)

    def start_provider_request_fallback(
        self,
        request_id: str,
        *,
        expected_run_id: str,
        fallback_run_id: str,
        session_id: str,
    ) -> dict[str, Any] | None:
        now = utc_now()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE provider_requests
                SET session_id = :session_id,
                    run_id = :fallback_run_id,
                    state = 'queued',
                    fallback_state = 'started',
                    response_json = '',
                    updated_at = :updated_at
                WHERE request_id = :request_id
                  AND run_id = :expected_run_id
                  AND fallback_state = 'claimed'
                  AND state IN ('queued', 'running')
                """,
                {
                    "request_id": request_id,
                    "expected_run_id": expected_run_id,
                    "fallback_run_id": fallback_run_id,
                    "session_id": session_id,
                    "updated_at": now,
                },
            )
            if cursor.rowcount != 1:
                return None
            row = conn.execute(
                "SELECT * FROM provider_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        return self._row(row)

    def fail_stale_provider_request_fallback(
        self,
        request_id: str,
        *,
        claimed_before: str,
    ) -> dict[str, Any] | None:
        """Turn an abandoned fallback claim into an honest terminal failure."""

        now = utc_now()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE provider_requests
                SET state = 'failed',
                    fallback_state = 'failed',
                    updated_at = :updated_at
                WHERE request_id = :request_id
                  AND fallback_state = 'claimed'
                  AND state IN ('queued', 'running')
                  AND updated_at <= :claimed_before
                """,
                {
                    "request_id": request_id,
                    "claimed_before": claimed_before,
                    "updated_at": now,
                },
            )
            if cursor.rowcount != 1:
                return None
            row = conn.execute(
                "SELECT * FROM provider_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        return self._row(row)

    def claim_provider_request_cancel(self, request_id: str) -> dict[str, Any] | None:
        """Make Stop sticky and cancel its exact queued run in one transaction."""

        now = utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE provider_requests
                SET state = 'cancelled',
                    fallback_state = 'cancelled',
                    updated_at = :updated_at
                WHERE request_id = :request_id
                  AND state IN ('queued', 'running')
                """,
                {"request_id": request_id, "updated_at": now},
            )
            if cursor.rowcount != 1:
                conn.execute("COMMIT")
                return None
            request_row = conn.execute(
                "SELECT * FROM provider_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            run_id = str(request_row["run_id"] or "") if request_row else ""
            queued_cancelled = 0
            if run_id:
                run_cursor = conn.execute(
                    """
                    UPDATE runs
                    SET state = 'cancelled',
                        ended_at = ?,
                        error_text = 'Cancelled by operator before execution',
                        failure_class = '', failure_retryable = 0,
                        failure_structured = 0, failure_user_message = '',
                        failure_recommended_recovery = '',
                        failure_diagnostic_summary = '', retry_after = NULL,
                        retry_attempts = 0, last_retry_class = ''
                    WHERE run_id = ? AND state = 'queued'
                    """,
                    (now, run_id),
                )
                queued_cancelled = run_cursor.rowcount
            if queued_cancelled:
                conn.execute(
                    """
                    UPDATE scheduled_runs
                    SET state = 'cancelled',
                        last_error = 'Cancelled by operator before execution',
                        updated_at = ?
                    WHERE queued_run_id = ? AND state IN ('queued', 'running')
                    """,
                    (now, run_id),
                )
            row = conn.execute(
                "SELECT * FROM provider_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            conn.execute("COMMIT")
        return self._row(row)

    def claim_provider_request_deadline(self, request_id: str) -> dict[str, Any] | None:
        """Make a provider response deadline terminal before native cleanup begins."""

        now = utc_now()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE provider_requests
                SET state = 'failed',
                    fallback_state = 'deadline_exceeded',
                    updated_at = :updated_at
                WHERE request_id = :request_id
                  AND state IN ('queued', 'running')
                """,
                {"request_id": request_id, "updated_at": now},
            )
            if cursor.rowcount != 1:
                return None
            row = conn.execute(
                "SELECT * FROM provider_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        return self._row(row)

    def arbitrate_provider_request_deadline(
        self,
        request_id: str,
        *,
        default_timeout_s: float,
        failure_class: str,
        failure_user_message: str,
        failure_recommended_recovery: str,
        failure_diagnostic_summary: str,
    ) -> dict[str, Any]:
        """Atomically accept an on-time terminal run or make its deadline final."""

        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()

        def parsed_timestamp(value: Any) -> datetime | None:
            try:
                parsed = datetime.fromisoformat(str(value or ""))
            except ValueError:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            request_row = conn.execute(
                "SELECT * FROM provider_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if request_row is None:
                conn.execute("COMMIT")
                return {
                    "request": None,
                    "run": None,
                    "deadline_exceeded": False,
                    "newly_expired": False,
                }

            request = dict(request_row)
            run_id = str(request.get("run_id") or "").strip()
            run_row = (
                conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
                if run_id
                else None
            )
            run = dict(run_row) if run_row is not None else None

            try:
                stored_timeout = float(request.get("response_timeout_s"))
            except (TypeError, ValueError):
                stored_timeout = 0.0
            if not math.isfinite(stored_timeout) or stored_timeout <= 0:
                stored_timeout = float(default_timeout_s)

            deadline = parsed_timestamp(request.get("response_deadline_at"))
            if deadline is None and str(request.get("state") or "") in {
                "queued",
                "running",
            }:
                created_at = parsed_timestamp(request.get("created_at")) or now_dt
                deadline = created_at + timedelta(seconds=stored_timeout)
                conn.execute(
                    """
                    UPDATE provider_requests
                    SET response_timeout_s = ?, response_deadline_at = ?, updated_at = ?
                    WHERE request_id = ?
                    """,
                    (stored_timeout, deadline.isoformat(), now, request_id),
                )
                request["response_timeout_s"] = stored_timeout
                request["response_deadline_at"] = deadline.isoformat()
                request["updated_at"] = now

            request_state = str(request.get("state") or "")
            run_state = str((run or {}).get("state") or "")
            run_ended_at = parsed_timestamp((run or {}).get("ended_at"))
            run_terminal = run_state in {
                "completed",
                "failed",
                "cancelled",
                "interrupted",
            }
            run_terminal_on_time = bool(
                deadline
                and run_terminal
                and run_ended_at
                and run_ended_at <= deadline
            )
            completed_late = bool(
                deadline
                and request_state == "completed"
                and run_state == "completed"
                and run_ended_at
                and run_ended_at > deadline
            )
            deadline_elapsed = bool(deadline and now_dt >= deadline)
            should_expire = completed_late or (
                request_state in {"queued", "running"}
                and deadline_elapsed
                and not run_terminal_on_time
            )

            newly_expired = False
            if should_expire:
                conn.execute(
                    """
                    UPDATE provider_requests
                    SET state = 'failed',
                        fallback_state = 'deadline_exceeded',
                        response_json = '',
                        updated_at = ?
                    WHERE request_id = ?
                    """,
                    (now, request_id),
                )
                if run_id:
                    conn.execute(
                        """
                        UPDATE runs
                        SET state = 'failed',
                            ended_at = COALESCE(ended_at, ?),
                            output_text = '',
                            error_text = ?,
                            failure_class = ?,
                            failure_retryable = 1,
                            failure_structured = 1,
                            failure_user_message = ?,
                            failure_recommended_recovery = ?,
                            failure_diagnostic_summary = ?
                        WHERE run_id = ?
                          AND state IN ('queued', 'running', 'completed', 'failed')
                        """,
                        (
                            now,
                            failure_user_message,
                            failure_class,
                            failure_user_message,
                            failure_recommended_recovery,
                            failure_diagnostic_summary,
                            run_id,
                        ),
                    )
                newly_expired = True

            request_row = conn.execute(
                "SELECT * FROM provider_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            run_row = (
                conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
                if run_id
                else None
            )
            conn.execute("COMMIT")

        request = self._row(request_row)
        run = self._row(run_row)
        return {
            "request": request,
            "run": run,
            "deadline_exceeded": bool(
                request
                and str(request.get("fallback_state") or "")
                == "deadline_exceeded"
            ),
            "newly_expired": newly_expired,
        }

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

    def update_provider_request_if_state(
        self,
        request_id: str,
        expected_states: tuple[str, ...],
        **fields: Any,
    ) -> dict[str, Any] | None:
        """Update an active provider row without reviving a terminal Stop/deadline."""

        if not fields or not expected_states:
            return None
        fields["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = :{key}" for key in fields)
        fields["request_id"] = request_id
        expected_parameters = {
            f"expected_state_{index}": state
            for index, state in enumerate(expected_states)
        }
        placeholders = ", ".join(
            f":{key}" for key in expected_parameters
        )
        parameters = {**fields, **expected_parameters}
        with self._connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE provider_requests
                SET {assignments}
                WHERE request_id = :request_id AND state IN ({placeholders})
                """,
                parameters,
            )
            row = conn.execute(
                "SELECT * FROM provider_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        return self._row(row) if cursor.rowcount == 1 else None

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
        trusted_run_lane: str = "mission",
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
            "trusted_run_lane": (
                "conversation" if trusted_run_lane == "conversation" else "mission"
            ),
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
                    worker_id, project_id, tenant_id, owner_id, name, role, profile, backend, execution_mode, trusted_run_lane, alias, runtime, model, state,
                    bootstrap_profile, bootstrap_bundle_json, gateway_url, takeover_url, control_url, gateway_port, gateway_token, session_key,
                    state_dir, workspace_dir, workspace_root, favorite, compute_released_at, pid, last_run_id, last_error, created_at, updated_at
                ) VALUES (
                    :worker_id, :project_id, :tenant_id, :owner_id, :name, :role, :profile, :backend, :execution_mode, :trusted_run_lane, :alias, :runtime, :model, :state,
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

    def count_active_host_missions(self) -> int:
        """Count durable host mission roots that could conflict with Parallel Main.

        Nonterminal rows remain blockers even when paused or waiting for input:
        policy prevents them from restarting while isolated Parallel mode is
        enabled, but readiness must not describe that retained work as gone.
        An active lease is included independently so a crash between state
        transitions cannot make the capability probe optimistic.
        """

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(DISTINCT worker_id)
                FROM (
                    SELECT workers.worker_id AS worker_id
                    FROM workers
                    JOIN runs ON runs.worker_id = workers.worker_id
                    WHERE workers.execution_mode = 'host'
                      AND workers.trusted_run_lane = 'mission'
                      AND runs.state IN (
                          'queued', 'running', 'settling', 'paused', 'needs_input'
                      )
                    UNION
                    SELECT leases.worker_id AS worker_id
                    FROM host_run_leases AS leases
                    JOIN workers ON workers.worker_id = leases.worker_id
                    WHERE leases.status = 'active'
                      AND workers.execution_mode = 'host'
                      AND workers.trusted_run_lane = 'mission'
                ) AS active_host_missions
                """
            ).fetchone()
        return int(row[0] or 0)

    def active_host_mission_worker_ids(self) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT worker_id
                FROM (
                    SELECT workers.worker_id AS worker_id
                    FROM workers
                    JOIN runs ON runs.worker_id = workers.worker_id
                    WHERE workers.execution_mode = 'host'
                      AND workers.trusted_run_lane = 'mission'
                      AND runs.state IN (
                          'queued', 'running', 'settling', 'paused', 'needs_input'
                      )
                    UNION
                    SELECT leases.worker_id AS worker_id
                    FROM host_run_leases AS leases
                    JOIN workers ON workers.worker_id = leases.worker_id
                    WHERE leases.status = 'active'
                      AND workers.execution_mode = 'host'
                      AND workers.trusted_run_lane = 'mission'
                )
                """
            ).fetchall()
        return {str(row["worker_id"]) for row in rows}

    def list_host_mission_workers(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM workers
                WHERE execution_mode = 'host'
                  AND trusted_run_lane = 'mission'
                ORDER BY created_at ASC, worker_id ASC
                """
            ).fetchall()
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

    def update_worker(
        self,
        worker_id: str,
        *,
        touch_updated_at: bool = True,
        **fields: Any,
    ) -> dict[str, Any] | None:
        if not fields:
            return self.get_worker(worker_id)
        protected = WORK_STOP_FIELD_NAMES.intersection(fields)
        if protected:
            raise ValueError(
                "Permanent work-stop fields may only be changed by lifecycle transactions"
            )
        if touch_updated_at:
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

    @staticmethod
    def _lifecycle_event_id(
        *,
        operation_token: str,
        operation_epoch: int,
        operation_kind: str,
        event_type: str,
        worker_id: str,
        run_id: str,
    ) -> str:
        operation_digest = hashlib.sha256(
            str(operation_token).encode("utf-8")
        ).hexdigest()
        material = "\0".join(
            (
                "glasshive.lifecycle-event.v1",
                operation_digest,
                str(int(operation_epoch)),
                str(operation_kind),
                str(event_type),
                str(worker_id),
                str(run_id or ""),
            )
        )
        return "evt_op_" + hashlib.sha256(material.encode("utf-8")).hexdigest()

    @staticmethod
    def _insert_lifecycle_event(
        conn: sqlite3.Connection,
        *,
        operation_token: str,
        operation_epoch: int,
        operation_kind: str,
        worker: sqlite3.Row,
        run_id: str,
        event_type: str,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> str:
        """Insert one same-transaction event for an exact lifecycle operation."""

        event_id = Store._lifecycle_event_id(
            operation_token=operation_token,
            operation_epoch=operation_epoch,
            operation_kind=operation_kind,
            event_type=event_type,
            worker_id=str(worker["worker_id"]),
            run_id=run_id,
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO events (
                event_id, project_id, worker_id, tenant_id, run_id,
                event_type, message, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                str(worker["project_id"]),
                str(worker["worker_id"]),
                str(worker["tenant_id"] or "local"),
                str(run_id or "") or None,
                str(event_type),
                str(message),
                json.dumps(payload or {}, ensure_ascii=False, sort_keys=True),
                utc_now(),
            ),
        )
        return event_id

    def has_lifecycle_operation_event(
        self,
        *,
        operation_id: str,
        operation_kind: str,
        event_type: str,
        worker_id: str,
        run_id: str,
    ) -> bool:
        clean_operation = str(operation_id or "").strip()
        if not clean_operation:
            return False
        event_id = self._lifecycle_event_id(
            operation_token=clean_operation,
            operation_epoch=0,
            operation_kind=str(operation_kind or "").strip().lower(),
            event_type=str(event_type or "").strip(),
            worker_id=str(worker_id or "").strip(),
            run_id=str(run_id or "").strip(),
        )
        with self._connect() as conn:
            return conn.execute(
                "SELECT 1 FROM events WHERE event_id = ? LIMIT 1", (event_id,)
            ).fetchone() is not None

    @staticmethod
    def _accept_cancel_actions_for_runs(
        conn: sqlite3.Connection,
        run_ids: list[str],
        *,
        updated_at: str,
    ) -> int:
        clean_ids = [str(run_id) for run_id in run_ids if str(run_id)]
        if not clean_ids:
            return 0
        placeholders = ", ".join("?" for _ in clean_ids)
        cursor = conn.execute(
            f"""
            UPDATE run_action_uses
            SET status = 'accepted', result_code = 'cancellation_confirmed',
                updated_at = ?
            WHERE source_run_id IN ({placeholders})
              AND action = 'cancel'
              AND status IN ('reserved', 'executing')
            """,
            (updated_at, *clean_ids),
        )
        return int(cursor.rowcount)

    @staticmethod
    def _enqueue_lifecycle_effects(
        conn: sqlite3.Connection,
        *,
        operation_token: str,
        operation_epoch: int,
        operation_kind: str,
        worker_id: str,
        run_id: str = "",
        effect_kinds: tuple[str, ...] | list[str],
    ) -> list[str]:
        """Persist public-safe idempotent effect references in the caller's txn."""

        token = str(operation_token or "")
        kind = str(operation_kind or "").strip().lower()
        clean_run_id = str(run_id or "")
        if not token or kind not in COMPUTE_OPERATION_KINDS:
            raise ValueError("Lifecycle effects require an exact operation identity")
        operation_digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = utc_now()
        effect_ids: list[str] = []
        for effect_kind in effect_kinds:
            clean_effect_kind = str(effect_kind or "").strip().lower()
            if clean_effect_kind not in LIFECYCLE_EFFECT_KINDS:
                raise ValueError(
                    f"Unsupported lifecycle operation effect: {clean_effect_kind}"
                )
            material = "\0".join(
                (
                    "glasshive.lifecycle-effect.v1",
                    operation_digest,
                    str(int(operation_epoch)),
                    kind,
                    clean_effect_kind,
                    str(worker_id),
                    clean_run_id,
                )
            )
            effect_id = "ope_" + hashlib.sha256(
                material.encode("utf-8")
            ).hexdigest()
            conn.execute(
                """
                INSERT OR IGNORE INTO lifecycle_operation_effects (
                    effect_id, operation_digest, operation_epoch, operation_kind,
                    effect_kind, worker_id, run_id, status, lease_owner,
                    lease_epoch, lease_expires_at, attempts, last_error_code,
                    created_at, updated_at, applied_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', '', 0, NULL, 0, '', ?, ?, NULL)
                """,
                (
                    effect_id,
                    operation_digest,
                    int(operation_epoch),
                    kind,
                    clean_effect_kind,
                    str(worker_id),
                    clean_run_id,
                    now,
                    now,
                ),
            )
            effect_ids.append(effect_id)
        return effect_ids

    def enqueue_capability_grant_revocation(
        self, binding: dict[str, str]
    ) -> dict[str, Any]:
        """Persist a secret-free exact grant/generation revocation before projection."""

        expected = {
            "authorizationRef",
            "originRef",
            "workRef",
            "workerId",
            "runId",
            "grantId",
            "containerGenerationId",
        }
        if not isinstance(binding, dict) or set(binding) != expected:
            raise ValueError("Capability revocation requires an exact binding")
        normalized = {key: str(binding.get(key) or "").strip() for key in expected}
        if any(not value or len(value) > 256 for value in normalized.values()):
            raise ValueError("Capability revocation requires an exact binding")
        material = "\0".join(
            ("glasshive.capability-revocation.v1",)
            + tuple(normalized[key] for key in sorted(expected))
        )
        revocation_id = "cgr_" + hashlib.sha256(material.encode("utf-8")).hexdigest()
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO capability_grant_revocations (
                    revocation_id, authorization_ref, origin_ref, work_ref,
                    worker_id, run_id, grant_id, container_generation_id,
                    status, lease_owner, lease_epoch, lease_expires_at,
                    next_attempt_at, attempts, last_error_code,
                    created_at, updated_at, applied_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'armed', '', 0, NULL,
                          NULL, 0, '', ?, ?, NULL)
                """,
                (
                    revocation_id,
                    normalized["authorizationRef"],
                    normalized["originRef"],
                    normalized["workRef"],
                    normalized["workerId"],
                    normalized["runId"],
                    normalized["grantId"],
                    normalized["containerGenerationId"],
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM capability_grant_revocations WHERE revocation_id = ?",
                (revocation_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("Capability revocation persistence failed")
        return self._row(row) or {}

    def list_capability_grant_revocations(
        self, *, status: str | None = None
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM capability_grant_revocations"
        params: list[Any] = []
        if status is not None:
            query += " WHERE status = ?"
            params.append(str(status))
        query += " ORDER BY created_at ASC, revocation_id ASC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return self._rows(rows)

    def activate_capability_grant_revocation(
        self, revocation_id: str
    ) -> dict[str, Any] | None:
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE capability_grant_revocations
                SET status = 'pending', updated_at = ?
                WHERE revocation_id = ? AND status = 'armed'
                """,
                (now, str(revocation_id or "")),
            )
            row = conn.execute(
                "SELECT * FROM capability_grant_revocations WHERE revocation_id = ?",
                (str(revocation_id or ""),),
            ).fetchone()
        return self._row(row)

    def activate_due_capability_grant_revocations(self) -> int:
        """Arm crash leftovers only after their exact runtime lease is no longer active."""

        now = utc_now()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE capability_grant_revocations AS revocation
                SET status = 'pending', updated_at = ?
                WHERE status = 'armed' AND NOT EXISTS (
                    SELECT 1 FROM host_run_leases AS lease
                    WHERE lease.run_id = revocation.run_id AND lease.status = 'active'
                )
                """,
                (now,),
            )
        return int(cursor.rowcount)

    def claim_next_capability_grant_revocation(
        self,
        executor_id: str,
        *,
        ttl_s: float = 60.0,
        now: datetime | None = None,
        created_before: str | None = None,
        revocation_id: str | None = None,
    ) -> dict[str, Any] | None:
        owner = str(executor_id or "").strip()
        if not owner:
            raise ValueError("Capability revocation claims require an executor")
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        now_iso = current.isoformat()
        expires_at = (
            current + timedelta(seconds=max(1.0, float(ttl_s)))
        ).isoformat()
        clauses = [
            "((status = 'pending' AND (next_attempt_at IS NULL OR next_attempt_at <= ?))",
            " OR (status = 'applying' AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?))",
        ]
        params: list[Any] = [now_iso, now_iso]
        clean_created_before = str(created_before or "").strip()
        if clean_created_before:
            clauses.append(" AND created_at <= ?")
            params.append(clean_created_before)
        clean_revocation_id = str(revocation_id or "").strip()
        if clean_revocation_id:
            clauses.append(" AND revocation_id = ?")
            params.append(clean_revocation_id)
        where_clause = "".join(clauses)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                f"""
                SELECT * FROM capability_grant_revocations
                WHERE {where_clause}
                ORDER BY created_at ASC, revocation_id ASC LIMIT 1
                """,
                params,
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            cursor = conn.execute(
                """
                UPDATE capability_grant_revocations
                SET status = 'applying', lease_owner = ?,
                    lease_epoch = lease_epoch + 1, lease_expires_at = ?,
                    next_attempt_at = NULL, attempts = attempts + 1, updated_at = ?
                WHERE revocation_id = ? AND (
                    (status = 'pending' AND (next_attempt_at IS NULL OR next_attempt_at <= ?))
                    OR (status = 'applying' AND lease_expires_at IS NOT NULL
                        AND lease_expires_at <= ?)
                )
                """,
                (owner, expires_at, now_iso, row["revocation_id"], now_iso, now_iso),
            )
            if cursor.rowcount != 1:
                conn.execute("ROLLBACK")
                return None
            claimed = conn.execute(
                "SELECT * FROM capability_grant_revocations WHERE revocation_id = ?",
                (row["revocation_id"],),
            ).fetchone()
            conn.execute("COMMIT")
        return self._row(claimed)

    def mark_capability_grant_revocation_applied(
        self, revocation_id: str, executor_id: str, *, lease_epoch: int
    ) -> dict[str, Any] | None:
        now = utc_now()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE capability_grant_revocations
                SET status = 'applied', lease_owner = '', lease_expires_at = NULL,
                    next_attempt_at = NULL, last_error_code = '',
                    updated_at = ?, applied_at = ?
                WHERE revocation_id = ? AND status = 'applying'
                  AND lease_owner = ? AND lease_epoch = ?
                """,
                (now, now, str(revocation_id or ""), str(executor_id or ""), int(lease_epoch)),
            )
            if cursor.rowcount != 1:
                return None
            row = conn.execute(
                "SELECT * FROM capability_grant_revocations WHERE revocation_id = ?",
                (str(revocation_id or ""),),
            ).fetchone()
        return self._row(row)

    def retry_capability_grant_revocation(
        self,
        revocation_id: str,
        executor_id: str,
        *,
        lease_epoch: int,
        error_code: str,
        retry_delay_s: float = 0.0,
    ) -> dict[str, Any] | None:
        clean_code = str(error_code or "").strip().lower()
        if clean_code not in {
            "broker_revocation_rejected",
            "broker_revocation_unavailable",
            "transient_dependency",
            "unknown",
        }:
            raise ValueError("Capability revocation failures require a safe error code")
        delay_s = max(0.0, min(float(retry_delay_s), 24 * 3600.0))
        retry_at = (
            datetime.now(timezone.utc) + timedelta(seconds=delay_s)
        ).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE capability_grant_revocations
                SET status = 'pending', lease_owner = '', lease_expires_at = NULL,
                    next_attempt_at = ?, last_error_code = ?, updated_at = ?
                WHERE revocation_id = ? AND status = 'applying'
                  AND lease_owner = ? AND lease_epoch = ?
                """,
                (
                    retry_at if delay_s > 0 else None,
                    clean_code,
                    utc_now(),
                    str(revocation_id or ""),
                    str(executor_id or ""),
                    int(lease_epoch),
                ),
            )
            if cursor.rowcount != 1:
                return None
            row = conn.execute(
                "SELECT * FROM capability_grant_revocations WHERE revocation_id = ?",
                (str(revocation_id or ""),),
            ).fetchone()
        return self._row(row)

    def list_lifecycle_operation_effects(
        self,
        *,
        worker_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM lifecycle_operation_effects"
        clauses: list[str] = []
        params: list[Any] = []
        if worker_id is not None:
            clauses.append("worker_id = ?")
            params.append(str(worker_id))
        if status is not None:
            clauses.append("status = ?")
            params.append(str(status))
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at ASC, effect_id ASC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return self._rows(rows)

    def paired_lifecycle_effect_is_applied(
        self,
        effect: dict[str, Any],
        *,
        required_effect_kind: str,
    ) -> bool:
        """Check the exact operation/worker peer required before a later sink."""

        required_kind = str(required_effect_kind or "").strip().lower()
        if required_kind not in LIFECYCLE_EFFECT_KINDS:
            return False
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT status FROM lifecycle_operation_effects
                WHERE operation_digest = ? AND operation_epoch = ?
                  AND operation_kind = ? AND effect_kind = ?
                  AND worker_id = ? AND run_id = ?
                LIMIT 1
                """,
                (
                    str(effect.get("operation_digest") or ""),
                    int(effect.get("operation_epoch") or 0),
                    str(effect.get("operation_kind") or ""),
                    required_kind,
                    str(effect.get("worker_id") or ""),
                    str(effect.get("run_id") or ""),
                ),
            ).fetchone()
        return bool(row and str(row["status"] or "") == "applied")

    def has_pending_lifecycle_effects(self) -> bool:
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT 1 FROM lifecycle_operation_effects
                WHERE status IN ('pending', 'applying') LIMIT 1
                """
            ).fetchone() is not None

    def claim_next_lifecycle_effect(
        self,
        executor_id: str,
        *,
        ttl_s: float = 60.0,
        now: datetime | None = None,
        effect_kinds: tuple[str, ...] | None = None,
        created_before: str | None = None,
    ) -> dict[str, Any] | None:
        owner = str(executor_id or "").strip()
        if not owner:
            raise ValueError("Lifecycle effect claims require an executor")
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        now_iso = current.isoformat()
        expires_at = (
            current + timedelta(seconds=max(1.0, float(ttl_s)))
        ).isoformat()
        clean_kinds = tuple(
            str(item or "").strip().lower()
            for item in (effect_kinds or ())
            if str(item or "").strip()
        )
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            kind_clause = ""
            params: list[Any] = [now_iso, now_iso]
            created_clause = ""
            clean_created_before = str(created_before or "").strip()
            if clean_created_before:
                created_clause = " AND created_at <= ?"
                params.append(clean_created_before)
            if clean_kinds:
                kind_clause = " AND effect_kind IN (" + ",".join(
                    "?" for _ in clean_kinds
                ) + ")"
                params.extend(clean_kinds)
            row = conn.execute(
                f"""
                SELECT * FROM lifecycle_operation_effects
                WHERE ((status = 'pending'
                        AND (next_attempt_at IS NULL OR next_attempt_at <= ?))
                   OR (status = 'applying' AND lease_expires_at IS NOT NULL
                       AND lease_expires_at <= ?))
                  {created_clause}
                  {kind_clause}
                ORDER BY created_at ASC, effect_id ASC
                LIMIT 1
                """,
                params,
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            cursor = conn.execute(
                """
                UPDATE lifecycle_operation_effects
                SET status = 'applying', lease_owner = ?,
                    lease_epoch = lease_epoch + 1, lease_expires_at = ?,
                    next_attempt_at = NULL,
                    attempts = attempts + 1, updated_at = ?
                WHERE effect_id = ? AND (
                    (status = 'pending'
                     AND (next_attempt_at IS NULL OR next_attempt_at <= ?))
                    OR (status = 'applying' AND lease_expires_at IS NOT NULL
                        AND lease_expires_at <= ?)
                )
                """,
                (
                    owner,
                    expires_at,
                    now_iso,
                    row["effect_id"],
                    now_iso,
                    now_iso,
                ),
            )
            if cursor.rowcount != 1:
                conn.execute("ROLLBACK")
                return None
            claimed = conn.execute(
                "SELECT * FROM lifecycle_operation_effects WHERE effect_id = ?",
                (row["effect_id"],),
            ).fetchone()
            conn.execute("COMMIT")
        return self._row(claimed)

    def mark_lifecycle_effect_applied(
        self, effect_id: str, executor_id: str, *, lease_epoch: int
    ) -> dict[str, Any] | None:
        now = utc_now()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE lifecycle_operation_effects
                SET status = 'applied', lease_owner = '', lease_expires_at = NULL,
                    next_attempt_at = NULL, last_error_code = '',
                    updated_at = ?, applied_at = ?
                WHERE effect_id = ? AND status = 'applying' AND lease_owner = ?
                  AND lease_epoch = ?
                """,
                (now, now, effect_id, str(executor_id or ""), int(lease_epoch)),
            )
            if cursor.rowcount != 1:
                return None
            row = conn.execute(
                "SELECT * FROM lifecycle_operation_effects WHERE effect_id = ?",
                (effect_id,),
            ).fetchone()
        return self._row(row)

    def retry_lifecycle_effect(
        self,
        effect_id: str,
        executor_id: str,
        *,
        lease_epoch: int,
        error_code: str,
        retry_delay_s: float = 0.0,
    ) -> dict[str, Any] | None:
        clean_code = str(error_code or "").strip().lower()
        if clean_code not in LIFECYCLE_EFFECT_ERROR_CODES:
            raise ValueError("Lifecycle effect failures require a safe error code")
        delay_s = max(0.0, min(float(retry_delay_s), 24 * 3600.0))
        retry_at = (
            datetime.now(timezone.utc) + timedelta(seconds=delay_s)
        ).isoformat()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE lifecycle_operation_effects
                SET status = 'pending', lease_owner = '', lease_expires_at = NULL,
                    next_attempt_at = ?, last_error_code = ?, updated_at = ?
                WHERE effect_id = ? AND status = 'applying' AND lease_owner = ?
                  AND lease_epoch = ?
                """,
                (
                    retry_at if delay_s > 0 else None,
                    clean_code,
                    utc_now(),
                    effect_id,
                    str(executor_id or ""),
                    int(lease_epoch),
                ),
            )
            if cursor.rowcount != 1:
                return None
            row = conn.execute(
                "SELECT * FROM lifecycle_operation_effects WHERE effect_id = ?",
                (effect_id,),
            ).fetchone()
        return self._row(row)

    @staticmethod
    def _compute_release_claim_expired(row: sqlite3.Row, now_iso: str) -> bool:
        expires_at = str(row["compute_release_expires_at"] or "").strip()
        return bool(expires_at and expires_at <= now_iso)

    def try_claim_worker_compute_release(
        self,
        worker_id: str,
        *,
        expected_updated_at: str,
        expected_last_run_id: str,
        expected_state: str,
        expected_container_id: str,
        owner: str,
        ttl_s: float,
        kind: str = "idle",
        scope: str = "",
        target_run_id: str = "",
        expected_target_started_at: str = "",
        expected_session_fingerprint: str = "",
        replacement_run: dict[str, Any] | None = None,
        action_use_id: str = "",
        action_executor_id: str = "",
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Fence compute starts before one policy-scoped destructive operation."""

        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        now_iso = current.isoformat()
        expires_at = (
            current + timedelta(seconds=max(30.0, float(ttl_s)))
        ).isoformat()
        token = f"release_{uuid.uuid4().hex}"
        requested_kind = str(kind or "idle").strip().lower()
        expected_scope = COMPUTE_OPERATION_SCOPE_BY_KIND.get(requested_kind, "")
        requested_scope = str(scope or expected_scope).strip().lower()
        requested_target = str(target_run_id or "").strip()
        requested_target_started_at = str(expected_target_started_at or "").strip()
        requested_session_fingerprint = str(expected_session_fingerprint or "").strip()
        replacement_data = dict(replacement_run or {})
        requested_replacement = str(replacement_data.get("run_id") or "").strip()
        requested_action_use = str(action_use_id or "").strip()
        requested_action_executor = str(action_executor_id or "").strip()
        if bool(requested_action_use) != bool(requested_action_executor):
            raise ValueError(
                "Lifecycle claim action binding requires both receipt and executor"
            )
        if requested_action_use and requested_kind not in {
            "pause_run",
            "resume_run",
            "steer_run",
            "stop_run",
        }:
            raise ValueError(
                "Only active-work run controls may bind a lifecycle claim receipt"
            )
        if requested_kind != "steer_run" and requested_replacement:
            raise ValueError("Only steer_run may reserve a replacement run")
        if requested_kind == "steer_run" and not requested_replacement:
            raise ValueError("steer_run requires a durable replacement run")
        if requested_kind not in COMPUTE_OPERATION_KINDS:
            raise ValueError(f"Unsupported worker compute operation kind: {requested_kind}")
        if requested_scope not in COMPUTE_OPERATION_SCOPES:
            raise ValueError(f"Unsupported worker compute operation scope: {requested_scope}")
        if requested_scope != expected_scope:
            raise ValueError(
                f"Worker compute operation {requested_kind} requires {expected_scope} scope"
            )
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            worker = conn.execute(
                "SELECT * FROM workers WHERE worker_id = ?", (worker_id,)
            ).fetchone()
            if worker is None:
                conn.execute("COMMIT")
                return None
            existing_token = str(worker["compute_release_token"] or "").strip()
            if existing_token and not self._compute_release_claim_expired(worker, now_iso):
                conn.execute("COMMIT")
                return None
            persisted_kind = str(worker["compute_release_kind"] or "").strip().lower()
            if existing_token and not persisted_kind:
                persisted_kind = "idle"
            persisted_scope = str(
                worker["compute_release_scope"] or "compute_only"
            ).strip().lower()
            persisted_target = str(worker["compute_release_target_run_id"] or "").strip()
            persisted_target_started_at = str(
                worker["compute_release_target_started_at"] or ""
            ).strip()
            persisted_container = str(worker["compute_release_container_id"] or "").strip()
            persisted_session_fingerprint = str(
                worker["compute_release_session_fingerprint"] or ""
            ).strip()
            persisted_replacement = str(
                worker["compute_release_replacement_run_id"] or ""
            ).strip()
            if existing_token and (
                persisted_kind != requested_kind
                or persisted_scope != requested_scope
                or persisted_target != requested_target
                or persisted_target_started_at != requested_target_started_at
                or persisted_container != str(expected_container_id or "").strip()
                or persisted_session_fingerprint != requested_session_fingerprint
                or persisted_replacement != requested_replacement
            ):
                conn.execute("COMMIT")
                return None

            nonterminal_runs = conn.execute(
                """
                SELECT run_id, state FROM runs
                WHERE worker_id = ?
                  AND state NOT IN ('completed', 'failed', 'cancelled', 'interrupted')
                """,
                (worker_id,),
            ).fetchall()
            active_leases = conn.execute(
                """
                SELECT run_id FROM host_run_leases
                WHERE worker_id = ? AND status = 'active'
                """,
                (worker_id,),
            ).fetchall()
            target = (
                conn.execute(
                    "SELECT * FROM runs WHERE run_id = ? AND worker_id = ?",
                    (requested_target, worker_id),
                ).fetchone()
                if requested_target
                else None
            )
            if target is not None and requested_target_started_at and (
                str(target["started_at"] or "").strip()
                != requested_target_started_at
            ):
                conn.execute("COMMIT")
                return None
            other_executing = any(
                str(row["run_id"]) != requested_target
                and str(row["state"] or "") in PROCESS_BEARING_RUN_STATES
                for row in nonterminal_runs
            )
            other_active_lease = any(
                str(row["run_id"] or "") != requested_target for row in active_leases
            )

            policy_valid = False
            if requested_kind == "idle":
                policy_valid = (
                    not other_executing and not active_leases
                    if existing_token
                    else not nonterminal_runs and not active_leases
                )
            elif requested_kind == "needs_input":
                needs_input_runs = [
                    row
                    for row in nonterminal_runs
                    if str(row["state"] or "") == "needs_input"
                ]
                policy_valid = bool(
                    target
                    and str(target["state"] or "") == "needs_input"
                    and str(worker["state"] or "") == "needs_input"
                    and len(needs_input_runs) == 1
                    and str(needs_input_runs[0]["run_id"]) == requested_target
                    and not other_executing
                    and not active_leases
                )
            elif requested_kind in {"pause_worker", "resume_worker"}:
                expected_worker_state = (
                    "paused" if requested_kind == "resume_worker" else ""
                )
                current_worker_state = str(worker["state"] or "")
                policy_valid = bool(
                    not nonterminal_runs
                    and not active_leases
                    and current_worker_state not in {"stopping", "terminated"}
                    and (
                        not expected_worker_state
                        or current_worker_state == expected_worker_state
                    )
                )
            elif requested_kind == "paused":
                paused_runs = [
                    row for row in nonterminal_runs if str(row["state"] or "") == "paused"
                ]
                disallowed_runs = [
                    row
                    for row in nonterminal_runs
                    if str(row["state"] or "")
                    in {"running", "settling", "needs_input"}
                ]
                target_state = str(target["state"] or "") if target is not None else ""
                if requested_target:
                    exact_paused_shape = (
                        len(paused_runs) == 1
                        and str(paused_runs[0]["run_id"]) == requested_target
                    )
                    terminal_takeover_shape = bool(
                        existing_token
                        and not paused_runs
                        and target
                        and target_state in TERMINAL_RUN_STATES
                    )
                    lease_shape = (
                        len(active_leases) <= 1
                        and not other_active_lease
                    )
                    policy_valid = bool(
                        str(worker["state"] or "") in {"paused", "stopping"}
                        and not disallowed_runs
                        and (exact_paused_shape or terminal_takeover_shape)
                        and lease_shape
                    )
                else:
                    policy_valid = bool(
                        not existing_token
                        and str(worker["state"] or "") == "paused"
                        and not nonterminal_runs
                        and not active_leases
                    )
            elif requested_kind == "max_duration":
                policy_valid = bool(
                    target
                    and (
                        existing_token
                        or str(target["state"] or "") in {"running", "settling"}
                    )
                    and not other_executing
                    and len(active_leases) <= 1
                    and not other_active_lease
                )
            elif requested_kind in {
                "pause_run",
                "resume_run",
                "interrupt_run",
                "steer_run",
            }:
                target_state = str(target["state"] or "") if target is not None else ""
                allowed_states = {
                    "pause_run": {"queued", "running", "settling"},
                    "resume_run": {"paused"},
                    "interrupt_run": {"running", "settling"},
                    "steer_run": {"queued", "running", "settling"},
                }[requested_kind]
                policy_valid = bool(
                    target
                    and (
                        existing_token
                        or target_state in allowed_states
                    )
                    and not other_executing
                    and len(active_leases) <= 1
                    and not other_active_lease
                )
            elif requested_kind == "stop_run":
                policy_valid = bool(
                    target
                    and (
                        existing_token
                        or str(target["state"] or "")
                        in {"queued", "running", "settling", "paused", "needs_input"}
                    )
                    and not other_executing
                    and len(active_leases) <= 1
                    and not other_active_lease
                )
            elif requested_kind == "terminate_worker":
                process_runs = [
                    row
                    for row in nonterminal_runs
                    if str(row["state"] or "") in PROCESS_BEARING_RUN_STATES
                ]
                policy_valid = bool(
                    len(process_runs) <= 1
                    and len(active_leases) <= 1
                    and not other_active_lease
                    and (
                        (not requested_target and not process_runs and not active_leases)
                        or (
                            target
                            and (
                                existing_token
                                or str(target["state"] or "") not in TERMINAL_RUN_STATES
                            )
                            and all(
                                str(row["run_id"]) == requested_target
                                for row in process_runs
                            )
                        )
                    )
                )
            if not policy_valid:
                conn.execute("COMMIT")
                return None

            if existing_token:
                if requested_kind == "steer_run":
                    reserved_replacement = conn.execute(
                        "SELECT * FROM runs WHERE run_id = ? AND worker_id = ?",
                        (requested_replacement, worker_id),
                    ).fetchone()
                    if (
                        reserved_replacement is None
                        or str(reserved_replacement["state"] or "") != "queued"
                        or str(reserved_replacement["instruction"] or "")
                        != str(replacement_data.get("instruction") or "")
                    ):
                        conn.execute("COMMIT")
                        return None
                cursor = conn.execute(
                    """
                    UPDATE workers SET
                        compute_release_token = ?, compute_release_owner = ?,
                        compute_release_claimed_at = ?, compute_release_expires_at = ?,
                        compute_release_epoch = compute_release_epoch + 1,
                        compute_release_kind = CASE
                            WHEN compute_release_kind = '' THEN ? ELSE compute_release_kind END,
                        state = CASE
                            WHEN ? IN ('max_duration', 'stop_run', 'terminate_worker') THEN 'stopping'
                            ELSE state END,
                        updated_at = ?
                    WHERE worker_id = ? AND compute_release_token = ?
                      AND compute_release_epoch = ?
                    """,
                    (
                        token,
                        str(owner),
                        now_iso,
                        expires_at,
                        requested_kind,
                        requested_kind,
                        now_iso,
                        worker_id,
                        existing_token,
                        int(worker["compute_release_epoch"] or 0),
                    ),
                )
                if cursor.rowcount != 1:
                    conn.execute("ROLLBACK")
                    return None
                claimed = conn.execute(
                    "SELECT * FROM workers WHERE worker_id = ?", (worker_id,)
                ).fetchone()
                if requested_action_use and self._bind_active_work_action_lifecycle_in_transaction(
                    conn,
                    action_use_id=requested_action_use,
                    operation_id=str(claimed["compute_release_operation_id"] or ""),
                    operation_kind=requested_kind,
                    target_run_id=requested_target,
                    executor_id=requested_action_executor,
                ) != 1:
                    conn.execute("ROLLBACK")
                    return None
                conn.execute("COMMIT")
                return {
                    "token": token,
                    "epoch": int(claimed["compute_release_epoch"] or 0),
                    "worker": self._row(claimed),
                    "takeover": True,
                }
            if (
                str(worker["updated_at"] or "") != str(expected_updated_at or "")
                or str(worker["last_run_id"] or "") != str(expected_last_run_id or "")
                or str(worker["state"] or "") != str(expected_state or "")
                or (
                    bool(worker["compute_released_at"])
                    and requested_kind
                    not in {
                        "resume_worker",
                        "resume_run",
                        "stop_run",
                        "terminate_worker",
                    }
                )
            ):
                conn.execute("COMMIT")
                return None
            if requested_kind == "stop_run" and str(worker["work_stop_id"] or ""):
                conn.execute("COMMIT")
                return None
            if requested_kind == "steer_run":
                if (
                    str(replacement_data.get("worker_id") or "") != worker_id
                    or str(replacement_data.get("project_id") or "")
                    != str(worker["project_id"] or "")
                    or str(replacement_data.get("state") or "") != "queued"
                    or not str(replacement_data.get("instruction") or "")
                ):
                    conn.execute("COMMIT")
                    return None
                existing_replacement = conn.execute(
                    "SELECT 1 FROM runs WHERE run_id = ?", (requested_replacement,)
                ).fetchone()
                if existing_replacement is not None:
                    conn.execute("COMMIT")
                    return None
                self._insert_run_row(conn, replacement_data)
            operation_id = "op_" + uuid.uuid4().hex
            cursor = conn.execute(
                """
                UPDATE workers SET
                    compute_release_token = ?, compute_release_owner = ?,
                    compute_release_claimed_at = ?, compute_release_expires_at = ?,
                    compute_release_epoch = compute_release_epoch + 1,
                    compute_release_kind = ?,
                    compute_release_scope = ?,
                    compute_release_container_id = ?,
                    compute_release_session_fingerprint = ?,
                    compute_release_target_run_id = ?,
                    compute_release_target_started_at = ?,
                    compute_release_terminal_run_id = ?,
                    compute_release_replacement_run_id = ?,
                    compute_release_operation_id = ?,
                    work_stop_id = CASE WHEN ? = 'stop_run' THEN ? ELSE work_stop_id END,
                    work_stop_requested_at = CASE
                        WHEN ? = 'stop_run' THEN ? ELSE work_stop_requested_at END,
                    work_stop_settled_at = CASE
                        WHEN ? = 'stop_run' THEN NULL ELSE work_stop_settled_at END,
                    work_stop_outcome = CASE
                        WHEN ? = 'stop_run' THEN '' ELSE work_stop_outcome END,
                    state = CASE
                        WHEN ? IN ('max_duration', 'stop_run', 'terminate_worker') THEN 'stopping'
                        ELSE state END,
                    updated_at = ?
                WHERE worker_id = ? AND COALESCE(compute_release_token, '') IN ('', ?)
                """,
                (
                    token,
                    str(owner),
                    now_iso,
                    expires_at,
                    requested_kind,
                    requested_scope,
                    str(expected_container_id or ""),
                    requested_session_fingerprint,
                    requested_target,
                    requested_target_started_at,
                    (
                        str(expected_last_run_id or "")
                        if requested_kind == "idle"
                        else ""
                    ),
                    requested_replacement,
                    operation_id,
                    requested_kind,
                    operation_id,
                    requested_kind,
                    now_iso,
                    requested_kind,
                    requested_kind,
                    requested_kind,
                    now_iso,
                    worker_id,
                    existing_token,
                ),
            )
            if cursor.rowcount != 1:
                conn.execute("ROLLBACK")
                return None
            claimed = conn.execute(
                "SELECT * FROM workers WHERE worker_id = ?", (worker_id,)
            ).fetchone()
            if requested_action_use and self._bind_active_work_action_lifecycle_in_transaction(
                conn,
                action_use_id=requested_action_use,
                operation_id=operation_id,
                operation_kind=requested_kind,
                target_run_id=requested_target,
                executor_id=requested_action_executor,
            ) != 1:
                conn.execute("ROLLBACK")
                return None
            conn.execute("COMMIT")
        return {
            "token": token,
            "epoch": int(claimed["compute_release_epoch"] or 0),
            "worker": self._row(claimed),
            "takeover": False,
        }

    def finalize_worker_compute_release(
        self,
        worker_id: str,
        token: str,
        epoch: int,
        *,
        expected_kind: str,
        target_run_id: str = "",
        compute_released_at: str | None,
        runtime_fields: dict[str, Any],
        idle_state: str,
    ) -> dict[str, Any] | None:
        """Publish released compute only for the exact durable release owner."""

        clean_kind = str(expected_kind or "").strip().lower()
        clean_target = str(target_run_id or "").strip()
        if clean_kind not in {
            "idle",
            "needs_input",
            "paused",
            "pause_worker",
            "resume_worker",
        }:
            raise ValueError("Unsupported compute-only finalization kind")
        clean_fields = {
            key: value
            for key, value in runtime_fields.items()
            if key in RUNTIME_INFO_FIELD_NAMES
        }
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            worker = conn.execute(
                "SELECT * FROM workers WHERE worker_id = ?", (worker_id,)
            ).fetchone()
            if (
                worker is None
                or str(worker["compute_release_token"] or "") != str(token)
                or int(worker["compute_release_epoch"] or 0) != int(epoch)
                or str(worker["compute_release_kind"] or "") != clean_kind
                or str(worker["compute_release_scope"] or "") != "compute_only"
                or str(worker["compute_release_target_run_id"] or "") != clean_target
            ):
                conn.execute("COMMIT")
                return None
            if clean_kind == "paused" and clean_target:
                conn.execute(
                    """
                    UPDATE host_run_leases
                    SET status = 'released', released_at = ?,
                        release_reason = 'paused_compute_release_confirmed',
                        reconciled_at = COALESCE(reconciled_at, ?)
                    WHERE worker_id = ? AND run_id = ? AND status = 'active'
                    """,
                    (utc_now(), utc_now(), worker_id, clean_target),
                )
            active = conn.execute(
                """
                SELECT state FROM runs
                WHERE worker_id = ? AND state IN ('running', 'settling')
                LIMIT 1
                """,
                (worker_id,),
            ).fetchone()
            queued = conn.execute(
                "SELECT 1 FROM runs WHERE worker_id = ? AND state = 'queued' LIMIT 1",
                (worker_id,),
            ).fetchone()
            current_state = str(worker["state"] or "")
            if clean_kind == "pause_worker":
                next_state = "paused"
            elif clean_kind == "resume_worker":
                next_state = str(idle_state)
            elif current_state in {"paused", "terminated", "needs_input"}:
                next_state = current_state
            elif active is not None:
                next_state = "running"
            elif queued is not None:
                next_state = "starting"
            else:
                next_state = str(idle_state)
            fields: dict[str, Any] = {
                **clean_fields,
                "state": next_state,
                "compute_released_at": compute_released_at,
                **COMPUTE_OPERATION_CLEAR_FIELDS,
                "updated_at": utc_now(),
                "worker_id": worker_id,
                "token": token,
                "epoch": int(epoch),
            }
            if clean_kind in {"pause_worker", "resume_worker"}:
                event_type = (
                    "worker.paused"
                    if clean_kind == "pause_worker"
                    else "worker.resumed"
                )
                self._insert_lifecycle_event(
                    conn,
                    operation_token=str(worker["compute_release_operation_id"] or token),
                    operation_epoch=0,
                    operation_kind=clean_kind,
                    worker=worker,
                    run_id="",
                    event_type=event_type,
                    message=(
                        "Worker paused"
                        if clean_kind == "pause_worker"
                        else "Worker resumed"
                    ),
                    payload={"operation_kind": clean_kind},
                )
                self._enqueue_lifecycle_effects(
                    conn,
                    operation_token=str(
                        worker["compute_release_operation_id"] or token
                    ),
                    operation_epoch=0,
                    operation_kind=clean_kind,
                    worker_id=worker_id,
                    effect_kinds=(
                        "callback.worker_paused"
                        if clean_kind == "pause_worker"
                        else "callback.worker_resumed",
                    ),
                )
            assignments = ", ".join(
                f"{key} = :{key}"
                for key in fields
                if key not in {"worker_id", "token", "epoch"}
            )
            cursor = conn.execute(
                f"UPDATE workers SET {assignments} "
                "WHERE worker_id = :worker_id AND compute_release_token = :token "
                "AND compute_release_epoch = :epoch",
                fields,
            )
            if cursor.rowcount != 1:
                conn.execute("ROLLBACK")
                return None
            updated = conn.execute(
                "SELECT * FROM workers WHERE worker_id = ?", (worker_id,)
            ).fetchone()
            conn.execute("COMMIT")
        return self._row(updated)

    def confirm_worker_control_runtime_effect(
        self,
        worker_id: str,
        token: str,
        epoch: int,
        *,
        kind: str,
        target_run_id: str,
    ) -> dict[str, Any] | None:
        """Bind confirmed runtime postcondition to the exact durable claim."""

        now = utc_now()
        with self._connect() as conn:
            worker = conn.execute(
                "SELECT * FROM workers WHERE worker_id = ?", (worker_id,)
            ).fetchone()
            if (
                worker is None
                or not str(worker["compute_release_operation_id"] or "")
                or str(worker["compute_release_token"] or "") != str(token)
                or int(worker["compute_release_epoch"] or 0) != int(epoch)
                or str(worker["compute_release_kind"] or "") != str(kind)
                or str(worker["compute_release_target_run_id"] or "")
                != str(target_run_id)
            ):
                return None
            proof_material = "\0".join(
                (
                    "glasshive.control-runtime-proof.v1",
                    str(worker["compute_release_operation_id"] or ""),
                    str(worker["compute_release_kind"] or ""),
                    str(worker["compute_release_scope"] or ""),
                    str(worker["compute_release_target_run_id"] or ""),
                    str(worker["compute_release_target_started_at"] or ""),
                    str(worker["compute_release_container_id"] or ""),
                    str(worker["compute_release_session_fingerprint"] or ""),
                )
            )
            proof_digest = hashlib.sha256(proof_material.encode("utf-8")).hexdigest()
            cursor = conn.execute(
                """
                UPDATE workers
                SET compute_release_runtime_confirmed_at = ?,
                    compute_release_runtime_proof_digest = ?, updated_at = ?
                WHERE worker_id = ? AND compute_release_token = ?
                  AND compute_release_epoch = ? AND compute_release_kind = ?
                  AND compute_release_target_run_id = ?
                """,
                (
                    now,
                    proof_digest,
                    now,
                    worker_id,
                    str(token),
                    int(epoch),
                    str(kind),
                    str(target_run_id),
                ),
            )
            if cursor.rowcount != 1:
                return None
            row = conn.execute(
                "SELECT * FROM workers WHERE worker_id = ?", (worker_id,)
            ).fetchone()
        return self._row(row)

    @staticmethod
    def worker_control_runtime_proof_matches(worker: dict[str, Any] | sqlite3.Row) -> bool:
        if not str(worker["compute_release_runtime_confirmed_at"] or ""):
            return False
        proof_material = "\0".join(
            (
                "glasshive.control-runtime-proof.v1",
                str(worker["compute_release_operation_id"] or ""),
                str(worker["compute_release_kind"] or ""),
                str(worker["compute_release_scope"] or ""),
                str(worker["compute_release_target_run_id"] or ""),
                str(worker["compute_release_target_started_at"] or ""),
                str(worker["compute_release_container_id"] or ""),
                str(worker["compute_release_session_fingerprint"] or ""),
            )
        )
        return bool(
            str(worker["compute_release_runtime_proof_digest"] or "")
            == hashlib.sha256(proof_material.encode("utf-8")).hexdigest()
        )

    def finalize_worker_operation_claim(
        self,
        worker_id: str,
        token: str,
        epoch: int,
        *,
        kind: str,
        target_run_id: str,
        target_expected_states: tuple[str, ...],
        target_state: str,
        target_error_text: str,
        runtime_fields: dict[str, Any],
        idle_state: str,
        compute_released_at: str | None,
    ) -> dict[str, Any] | None:
        """Finalize one exact destructive claim and its target run atomically."""

        clean_kind = str(kind or "").strip().lower()
        clean_target = str(target_run_id or "").strip()
        if clean_kind != "max_duration" or not clean_target:
            raise ValueError("A run-scoped worker operation requires a kind and target run")
        clean_fields = {
            key: value
            for key, value in runtime_fields.items()
            if key in RUNTIME_INFO_FIELD_NAMES
        }
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            worker = conn.execute(
                "SELECT * FROM workers WHERE worker_id = ?", (worker_id,)
            ).fetchone()
            if (
                worker is None
                or str(worker["compute_release_token"] or "") != str(token)
                or int(worker["compute_release_epoch"] or 0) != int(epoch)
                or str(worker["compute_release_kind"] or "") != clean_kind
                or str(worker["compute_release_scope"] or "") != "run"
                or str(worker["compute_release_target_run_id"] or "") != clean_target
            ):
                conn.execute("COMMIT")
                return None

            transitioned = False
            now = utc_now()
            if target_expected_states:
                placeholders = ", ".join("?" for _ in target_expected_states)
                target_started_at = str(
                    worker["compute_release_target_started_at"] or ""
                )
                cursor = conn.execute(
                    f"""
                    UPDATE runs
                    SET state = ?, ended_at = ?, error_text = ?,
                        failure_class = '', failure_retryable = 0,
                        failure_structured = 0, failure_user_message = '',
                        failure_recommended_recovery = '',
                        failure_diagnostic_summary = '', retry_after = NULL,
                        retry_attempts = 0, last_retry_class = ''
                    WHERE run_id = ? AND worker_id = ?
                      AND COALESCE(started_at, '') = ?
                      AND state IN ({placeholders})
                    """,
                    (
                        target_state,
                        now,
                        target_error_text,
                        clean_target,
                        worker_id,
                        target_started_at,
                        *target_expected_states,
                    ),
                )
                transitioned = cursor.rowcount == 1
            if transitioned:
                conn.execute(
                    """
                    UPDATE scheduled_runs
                    SET state = 'cancelled', last_error = ?, updated_at = ?
                    WHERE queued_run_id = ?
                      AND state NOT IN ('completed', 'cancelled')
                    """,
                    (target_error_text, now, clean_target),
                )
                self._accept_cancel_actions_for_runs(
                    conn, [clean_target], updated_at=now
                )
                self._insert_lifecycle_event(
                    conn,
                    operation_token=str(worker["compute_release_operation_id"] or token),
                    operation_epoch=0,
                    operation_kind=clean_kind,
                    worker=worker,
                    run_id=clean_target,
                    event_type="run.duration_exceeded",
                    message=target_error_text,
                    payload={"operation_kind": clean_kind},
                )
            conn.execute(
                """
                UPDATE host_run_leases
                SET status = 'released', released_at = ?, release_reason = ?,
                    reconciled_at = COALESCE(reconciled_at, ?)
                WHERE worker_id = ? AND run_id = ? AND status = 'active'
                """,
                (
                    now,
                    f"{clean_kind}_confirmed",
                    now,
                    worker_id,
                    clean_target,
                ),
            )
            effect_ids = (
                self._enqueue_lifecycle_effects(
                    conn,
                    operation_token=str(worker["compute_release_operation_id"] or token),
                    operation_epoch=0,
                    operation_kind=clean_kind,
                    worker_id=worker_id,
                    run_id=clean_target,
                    effect_kinds=("callback.run_cancelled",),
                )
                if transitioned
                else []
            )
            active = conn.execute(
                """
                SELECT 1 FROM runs
                WHERE worker_id = ? AND state IN ('running', 'settling')
                LIMIT 1
                """,
                (worker_id,),
            ).fetchone()
            queued = conn.execute(
                "SELECT 1 FROM runs WHERE worker_id = ? AND state = 'queued' LIMIT 1",
                (worker_id,),
            ).fetchone()
            if active is not None:
                next_state = "running"
            elif queued is not None:
                next_state = "starting"
            else:
                next_state = str(idle_state)
            fields: dict[str, Any] = {
                **clean_fields,
                "state": next_state,
                "compute_released_at": compute_released_at,
                **COMPUTE_OPERATION_CLEAR_FIELDS,
                "updated_at": now,
                "worker_id": worker_id,
                "token": token,
                "epoch": int(epoch),
            }
            assignments = ", ".join(
                f"{key} = :{key}"
                for key in fields
                if key not in {"worker_id", "token", "epoch"}
            )
            cursor = conn.execute(
                f"UPDATE workers SET {assignments} "
                "WHERE worker_id = :worker_id AND compute_release_token = :token "
                "AND compute_release_epoch = :epoch",
                fields,
            )
            if cursor.rowcount != 1:
                conn.execute("ROLLBACK")
                return None
            updated_worker = conn.execute(
                "SELECT * FROM workers WHERE worker_id = ?", (worker_id,)
            ).fetchone()
            updated_run = conn.execute(
                "SELECT * FROM runs WHERE run_id = ?", (clean_target,)
            ).fetchone()
            conn.execute("COMMIT")
        return {
            "worker": self._row(updated_worker),
            "run": self._row(updated_run),
            "target_transitioned": transitioned,
            "effect_ids": effect_ids,
        }

    def finalize_worker_work_stop_claim(
        self,
        worker_id: str,
        token: str,
        epoch: int,
        *,
        target_run_id: str,
        runtime_fields: dict[str, Any],
        compute_released_at: str | None,
        error_text: str,
    ) -> dict[str, Any] | None:
        """Settle a public work-scoped Stop without reopening the worker later."""

        clean_target = str(target_run_id or "").strip()
        if not clean_target:
            raise ValueError("A work stop requires an exact runtime target")
        clean_fields = {
            key: value
            for key, value in runtime_fields.items()
            if key in RUNTIME_INFO_FIELD_NAMES
        }
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            worker = conn.execute(
                "SELECT * FROM workers WHERE worker_id = ?", (worker_id,)
            ).fetchone()
            if (
                worker is None
                or str(worker["compute_release_token"] or "") != str(token)
                or int(worker["compute_release_epoch"] or 0) != int(epoch)
                or str(worker["compute_release_kind"] or "") != "stop_run"
                or str(worker["compute_release_scope"] or "") != "work"
                or str(worker["compute_release_target_run_id"] or "") != clean_target
                or not str(worker["work_stop_id"] or "")
            ):
                conn.execute("COMMIT")
                return None
            target = conn.execute(
                "SELECT * FROM runs WHERE worker_id = ? AND run_id = ?",
                (worker_id, clean_target),
            ).fetchone()
            if target is None:
                conn.execute("COMMIT")
                return None
            target_state = str(target["state"] or "")
            target_started_at = str(worker["compute_release_target_started_at"] or "")
            target_matches = (
                str(target["started_at"] or "") == target_started_at
                and target_state in NONTERMINAL_RUN_STATES
            )
            if target_state not in TERMINAL_RUN_STATES and not target_matches:
                conn.execute("COMMIT")
                return None
            now = utc_now()
            outcome = "cancelled" if target_matches else "completion_won"
            cancelled_rows = conn.execute(
                """
                SELECT run_id FROM runs
                WHERE worker_id = ?
                  AND state IN ('queued', 'running', 'settling', 'paused', 'needs_input')
                ORDER BY queued_at ASC, run_id ASC
                """,
                (worker_id,),
            ).fetchall()
            cancelled_run_ids = [str(row["run_id"]) for row in cancelled_rows]
            cursor = conn.execute(
                """
                UPDATE runs
                SET state = 'cancelled', ended_at = ?, error_text = ?,
                    failure_class = '', failure_retryable = 0,
                    failure_structured = 0, failure_user_message = '',
                    failure_recommended_recovery = '',
                    failure_diagnostic_summary = '', retry_after = NULL,
                    retry_attempts = 0, last_retry_class = ''
                WHERE worker_id = ?
                  AND state IN ('queued', 'running', 'settling', 'paused', 'needs_input')
                """,
                (now, error_text, worker_id),
            )
            conn.execute(
                """
                UPDATE scheduled_runs
                SET state = 'cancelled', last_error = ?, updated_at = ?
                WHERE worker_id = ? AND state NOT IN ('completed', 'cancelled')
                """,
                (error_text, now, worker_id),
            )
            conn.execute(
                """
                UPDATE active_work_action_uses
                SET status = 'failed', last_error = 'work_stopped',
                    lease_expires_at = NULL, updated_at = ?
                WHERE work_ref IN (
                    SELECT work_ref FROM delegations WHERE worker_id = ?
                ) AND status = 'pending' AND action != 'stop'
                """,
                (now, worker_id),
            )
            self._accept_cancel_actions_for_runs(
                conn, cancelled_run_ids, updated_at=now
            )
            conn.execute(
                """
                UPDATE host_run_leases
                SET status = 'released', released_at = ?,
                    release_reason = 'work_stop_confirmed',
                    reconciled_at = COALESCE(reconciled_at, ?)
                WHERE worker_id = ? AND run_id = ? AND status = 'active'
                """,
                (now, now, worker_id, clean_target),
            )
            effect_ids = (
                self._enqueue_lifecycle_effects(
                    conn,
                    operation_token=str(worker["compute_release_operation_id"] or token),
                    operation_epoch=0,
                    operation_kind="stop_run",
                    worker_id=worker_id,
                    run_id=clean_target,
                    effect_kinds=("callback.work_stopped",),
                )
                if target_matches
                else []
            )
            event_id = self._insert_lifecycle_event(
                conn,
                operation_token=str(worker["compute_release_operation_id"] or token),
                operation_epoch=0,
                operation_kind="stop_run",
                worker=worker,
                run_id=clean_target,
                event_type=(
                    "run.cancelled" if target_matches else "work.stop_completion_won"
                ),
                message=(
                    "Run stop confirmed"
                    if target_matches
                    else "Work completed before its stop could cancel the target run"
                ),
                payload={"work_stop_outcome": outcome},
            )
            fields: dict[str, Any] = {
                **clean_fields,
                "state": "ready",
                "compute_released_at": compute_released_at,
                **COMPUTE_OPERATION_CLEAR_FIELDS,
                "work_stop_settled_at": now,
                "work_stop_outcome": outcome,
                "updated_at": now,
                "worker_id": worker_id,
                "token": token,
                "epoch": int(epoch),
            }
            assignments = ", ".join(
                f"{key} = :{key}"
                for key in fields
                if key not in {"worker_id", "token", "epoch"}
            )
            updated_cursor = conn.execute(
                f"UPDATE workers SET {assignments} "
                "WHERE worker_id = :worker_id AND compute_release_token = :token "
                "AND compute_release_epoch = :epoch AND compute_release_kind = 'stop_run' "
                "AND compute_release_scope = 'work'",
                fields,
            )
            if updated_cursor.rowcount != 1:
                conn.execute("ROLLBACK")
                return None
            updated_worker = conn.execute(
                "SELECT * FROM workers WHERE worker_id = ?", (worker_id,)
            ).fetchone()
            updated_target = conn.execute(
                "SELECT * FROM runs WHERE run_id = ?", (clean_target,)
            ).fetchone()
            conn.execute("COMMIT")
        return {
            "worker": self._row(updated_worker),
            "run": self._row(updated_target),
            "target_transitioned": target_matches,
            "cancelled_run_count": int(cursor.rowcount),
            "work_stop_outcome": outcome,
            "effect_ids": effect_ids,
            "event_id": event_id,
        }

    def worker_compute_use_allowed(self, worker_id: str) -> dict[str, Any] | None:
        """Return the worker only while no destructive operation owns compute."""

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            worker = conn.execute(
                "SELECT * FROM workers WHERE worker_id = ?", (worker_id,)
            ).fetchone()
            if (
                worker is None
                or str(worker["compute_release_token"] or "").strip()
                or str(worker["state"] or "") in {"stopping", "terminated"}
            ):
                conn.execute("COMMIT")
                return None
            conn.execute("COMMIT")
        return self._row(worker)

    def finalize_worker_run_control_claim(
        self,
        worker_id: str,
        token: str,
        epoch: int,
        *,
        kind: str,
        target_run_id: str,
        target_expected_states: tuple[str, ...],
        target_state: str,
        worker_state: str,
        runtime_fields: dict[str, Any],
        error_text: str = "",
        release_lease: bool = False,
    ) -> dict[str, Any] | None:
        """Commit one exact Pause/Resume/Interrupt/Steer control generation."""

        clean_kind = str(kind or "").strip().lower()
        clean_target = str(target_run_id or "").strip()
        if clean_kind not in {
            "pause_run",
            "resume_run",
            "interrupt_run",
            "steer_run",
        } or not clean_target:
            raise ValueError("Run control finalization requires an exact operation target")
        if not target_expected_states:
            raise ValueError("Run control finalization requires expected target states")
        clean_fields = {
            key: value
            for key, value in runtime_fields.items()
            if key in RUNTIME_INFO_FIELD_NAMES
        }
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            worker = conn.execute(
                "SELECT * FROM workers WHERE worker_id = ?", (worker_id,)
            ).fetchone()
            if (
                worker is None
                or str(worker["compute_release_token"] or "") != str(token)
                or int(worker["compute_release_epoch"] or 0) != int(epoch)
                or str(worker["compute_release_kind"] or "") != clean_kind
                or str(worker["compute_release_scope"] or "") != "run"
                or str(worker["compute_release_target_run_id"] or "") != clean_target
            ):
                conn.execute("COMMIT")
                return None
            target = conn.execute(
                "SELECT * FROM runs WHERE run_id = ? AND worker_id = ?",
                (clean_target, worker_id),
            ).fetchone()
            if target is None or str(target["started_at"] or "") != str(
                worker["compute_release_target_started_at"] or ""
            ):
                conn.execute("COMMIT")
                return None
            current_target_state = str(target["state"] or "")
            if current_target_state in TERMINAL_RUN_STATES:
                now = utc_now()
                self._insert_lifecycle_event(
                    conn,
                    operation_token=str(
                        worker["compute_release_operation_id"] or token
                    ),
                    operation_epoch=0,
                    operation_kind=clean_kind,
                    worker=worker,
                    run_id=clean_target,
                    event_type="control.terminal_won",
                    message="Terminal run truth won the lifecycle control race",
                    payload={
                        "operation_kind": clean_kind,
                        "terminal_state": current_target_state,
                    },
                )
                conn.execute(
                    """
                    UPDATE host_run_leases
                    SET status = 'released', released_at = ?,
                        release_reason = ?, reconciled_at = COALESCE(reconciled_at, ?)
                    WHERE worker_id = ? AND run_id = ? AND status = 'active'
                    """,
                    (
                        now,
                        f"{clean_kind}_terminal_won",
                        now,
                        worker_id,
                        clean_target,
                    ),
                )
                fields: dict[str, Any] = {
                    **clean_fields,
                    "state": "ready",
                    **COMPUTE_OPERATION_CLEAR_FIELDS,
                    "updated_at": now,
                    "worker_id": worker_id,
                    "token": token,
                    "epoch": int(epoch),
                }
                assignments = ", ".join(
                    f"{key} = :{key}"
                    for key in fields
                    if key not in {"worker_id", "token", "epoch"}
                )
                cursor = conn.execute(
                    f"UPDATE workers SET {assignments} "
                    "WHERE worker_id = :worker_id AND compute_release_token = :token "
                    "AND compute_release_epoch = :epoch",
                    fields,
                )
                if cursor.rowcount != 1:
                    conn.execute("ROLLBACK")
                    return None
                updated_worker = conn.execute(
                    "SELECT * FROM workers WHERE worker_id = ?", (worker_id,)
                ).fetchone()
                conn.execute("COMMIT")
                return {
                    "worker": self._row(updated_worker),
                    "run": self._row(target),
                    "target_transitioned": False,
                    "terminal_won": True,
                    "event_ids": [],
                }
            if (
                clean_kind in {"resume_run", "interrupt_run"}
                or (clean_kind == "pause_run" and current_target_state != "queued")
            ) and not self.worker_control_runtime_proof_matches(worker):
                conn.execute("COMMIT")
                return None
            placeholders = ", ".join("?" for _ in target_expected_states)
            now = utc_now()
            ended_at = now if target_state in TERMINAL_RUN_STATES else None
            cursor = conn.execute(
                f"""
                UPDATE runs
                SET state = ?, ended_at = ?, error_text = ?, retry_after = NULL
                WHERE run_id = ? AND worker_id = ?
                  AND state IN ({placeholders})
                """,
                (
                    target_state,
                    ended_at,
                    error_text,
                    clean_target,
                    worker_id,
                    *target_expected_states,
                ),
            )
            if cursor.rowcount != 1:
                conn.execute("COMMIT")
                return None
            if release_lease:
                conn.execute(
                    """
                    UPDATE host_run_leases
                    SET status = 'released', released_at = ?, release_reason = ?,
                        reconciled_at = COALESCE(reconciled_at, ?)
                    WHERE worker_id = ? AND run_id = ? AND status = 'active'
                    """,
                    (now, f"{clean_kind}_confirmed", now, worker_id, clean_target),
                )
            event_specs = {
                "pause_run": (
                    ("run.paused", "Run paused by operator"),
                    ("worker.paused", "Worker paused"),
                ),
                "resume_run": (
                    ("run.resumed", "Paused run resumed"),
                    ("worker.resumed", "Worker resumed"),
                ),
                "interrupt_run": (
                    ("worker.interrupted", "Worker interrupted"),
                    ("run.interrupted", "Run interruption accepted"),
                ),
                "steer_run": (),
            }[clean_kind]
            event_ids = [
                self._insert_lifecycle_event(
                    conn,
                    operation_token=str(worker["compute_release_operation_id"] or token),
                    operation_epoch=0,
                    operation_kind=clean_kind,
                    worker=worker,
                    run_id=clean_target,
                    event_type=event_type,
                    message=message,
                    payload={"operation_kind": clean_kind},
                )
                for event_type, message in event_specs
            ]
            callback_effect = {
                "pause_run": "callback.run_paused",
                "resume_run": (
                    "callback.run_resumed_in_place"
                    if target_state == "running"
                    else "callback.run_resumed_queued"
                ),
                "interrupt_run": "callback.run_interrupted",
                "steer_run": "callback.run_steered",
            }[clean_kind]
            effect_ids = self._enqueue_lifecycle_effects(
                conn,
                operation_token=str(worker["compute_release_operation_id"] or token),
                operation_epoch=0,
                operation_kind=clean_kind,
                worker_id=worker_id,
                run_id=clean_target,
                effect_kinds=(callback_effect,),
            )
            fields: dict[str, Any] = {
                **clean_fields,
                "state": worker_state,
                "compute_released_at": (
                    None
                    if clean_kind == "resume_run"
                    else worker["compute_released_at"]
                ),
                **COMPUTE_OPERATION_CLEAR_FIELDS,
                "updated_at": now,
                "worker_id": worker_id,
                "token": token,
                "epoch": int(epoch),
            }
            assignments = ", ".join(
                f"{key} = :{key}"
                for key in fields
                if key not in {"worker_id", "token", "epoch"}
            )
            updated_cursor = conn.execute(
                f"UPDATE workers SET {assignments} "
                "WHERE worker_id = :worker_id AND compute_release_token = :token "
                "AND compute_release_epoch = :epoch",
                fields,
            )
            if updated_cursor.rowcount != 1:
                conn.execute("ROLLBACK")
                return None
            updated_worker = conn.execute(
                "SELECT * FROM workers WHERE worker_id = ?", (worker_id,)
            ).fetchone()
            updated_target = conn.execute(
                "SELECT * FROM runs WHERE run_id = ?", (clean_target,)
            ).fetchone()
            conn.execute("COMMIT")
        return {
            "worker": self._row(updated_worker),
            "run": self._row(updated_target),
            "target_transitioned": True,
            "event_ids": event_ids,
            "effect_ids": effect_ids,
        }

    def abandon_worker_run_control_claim(
        self,
        worker_id: str,
        token: str,
        epoch: int,
        *,
        kind: str,
        target_run_id: str,
        worker_state: str,
        last_error: str,
    ) -> dict[str, Any] | None:
        """Release a non-destructive start claim without publishing success."""

        clean_kind = str(kind or "").strip().lower()
        if clean_kind not in {"resume_run", "resume_worker"}:
            raise ValueError("Only a failed resume startup may abandon its claim")
        fields = {
            "state": worker_state,
            "last_error": last_error,
            **COMPUTE_OPERATION_CLEAR_FIELDS,
            "updated_at": utc_now(),
            "worker_id": worker_id,
            "token": token,
            "epoch": int(epoch),
            "kind": clean_kind,
            "target_run_id": str(target_run_id or ""),
        }
        assignments = ", ".join(
            f"{key} = :{key}"
            for key in fields
            if key
            not in {"worker_id", "token", "epoch", "kind", "target_run_id"}
        )
        with self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE workers SET {assignments} "
                "WHERE worker_id = :worker_id AND compute_release_token = :token "
                "AND compute_release_epoch = :epoch AND compute_release_kind = :kind "
                "AND compute_release_target_run_id = :target_run_id",
                fields,
            )
            if cursor.rowcount != 1:
                return None
            row = conn.execute(
                "SELECT * FROM workers WHERE worker_id = ?", (worker_id,)
            ).fetchone()
        return self._row(row)

    def abandon_stale_worker_compute_release_claim(
        self,
        worker_id: str,
        token: str,
        epoch: int,
        *,
        kind: str,
    ) -> dict[str, Any] | None:
        """Clear a stale compute-only fence without publishing release success.

        An idle/paused claim can outlive the Docker generation it captured or
        race newly queued work.  In either case the old destructive attempt may
        not be projected onto the newer generation.  Clearing only the exact
        owned fence lets normal admission/reaping re-evaluate current truth.
        """

        clean_kind = str(kind or "").strip().lower()
        if clean_kind not in {"idle", "needs_input", "paused"}:
            raise ValueError(
                "Only stale idle, needs-input, or paused release claims may be abandoned"
            )
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            worker = conn.execute(
                "SELECT * FROM workers WHERE worker_id = ?", (worker_id,)
            ).fetchone()
            if (
                worker is None
                or str(worker["compute_release_token"] or "") != str(token)
                or int(worker["compute_release_epoch"] or 0) != int(epoch)
                or str(worker["compute_release_kind"] or "") != clean_kind
                or str(worker["compute_release_scope"] or "") != "compute_only"
            ):
                conn.execute("COMMIT")
                return None
            active = conn.execute(
                """
                SELECT 1 FROM runs
                WHERE worker_id = ? AND state IN ('running', 'settling')
                LIMIT 1
                """,
                (worker_id,),
            ).fetchone()
            queued = conn.execute(
                "SELECT 1 FROM runs WHERE worker_id = ? AND state = 'queued' LIMIT 1",
                (worker_id,),
            ).fetchone()
            current_state = str(worker["state"] or "")
            next_state = (
                "running"
                if active is not None
                else "starting"
                if queued is not None
                else current_state
            )
            fields: dict[str, Any] = {
                "state": next_state,
                **COMPUTE_OPERATION_CLEAR_FIELDS,
                "updated_at": utc_now(),
                "worker_id": worker_id,
                "token": token,
                "epoch": int(epoch),
                "kind": clean_kind,
            }
            assignments = ", ".join(
                f"{key} = :{key}"
                for key in fields
                if key not in {"worker_id", "token", "epoch", "kind"}
            )
            cursor = conn.execute(
                f"UPDATE workers SET {assignments} "
                "WHERE worker_id = :worker_id AND compute_release_token = :token "
                "AND compute_release_epoch = :epoch AND compute_release_kind = :kind",
                fields,
            )
            if cursor.rowcount != 1:
                conn.execute("ROLLBACK")
                return None
            updated = conn.execute(
                "SELECT * FROM workers WHERE worker_id = ?", (worker_id,)
            ).fetchone()
            conn.execute("COMMIT")
        return self._row(updated)

    def rebind_worker_termination_claim_generation(
        self,
        worker_id: str,
        token: str,
        epoch: int,
        *,
        container_id: str,
    ) -> dict[str, Any] | None:
        """Move a durable whole-worker tombstone onto the current generation."""

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE workers
                SET compute_release_container_id = ?,
                    compute_release_epoch = compute_release_epoch + 1,
                    updated_at = ?
                WHERE worker_id = ?
                  AND compute_release_token = ?
                  AND compute_release_epoch = ?
                  AND compute_release_kind = 'terminate_worker'
                  AND compute_release_scope = 'worker'
                  AND state = 'stopping'
                """,
                (str(container_id or ""), utc_now(), worker_id, token, int(epoch)),
            )
            if cursor.rowcount != 1:
                conn.execute("ROLLBACK")
                return None
            updated = conn.execute(
                "SELECT * FROM workers WHERE worker_id = ?", (worker_id,)
            ).fetchone()
            conn.execute("COMMIT")
        return self._row(updated)

    def rebind_worker_compute_release_claim_generation(
        self,
        worker_id: str,
        token: str,
        epoch: int,
        *,
        kind: str,
        container_id: str,
    ) -> dict[str, Any] | None:
        """Rebind an idle/paused compute-only claim to the current generation.

        Recovery may observe that Docker recreated an otherwise idle worker
        after the original destructive attempt failed.  When no newer work is
        admissible, the durable operation remains authoritative and can move to
        the freshly observed generation.  The epoch increment fences every
        caller that still holds the prior container receipt.
        """

        clean_kind = str(kind or "").strip().lower()
        if clean_kind not in {"idle", "needs_input", "paused"}:
            raise ValueError(
                "Only idle, needs-input, or paused compute release may change generation"
            )
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE workers
                SET compute_release_container_id = ?,
                    compute_release_epoch = compute_release_epoch + 1,
                    updated_at = ?
                WHERE worker_id = ?
                  AND compute_release_token = ?
                  AND compute_release_epoch = ?
                  AND compute_release_kind = ?
                  AND compute_release_scope = 'compute_only'
                """,
                (
                    str(container_id or ""),
                    utc_now(),
                    worker_id,
                    token,
                    int(epoch),
                    clean_kind,
                ),
            )
            if cursor.rowcount != 1:
                conn.execute("ROLLBACK")
                return None
            updated = conn.execute(
                "SELECT * FROM workers WHERE worker_id = ?", (worker_id,)
            ).fetchone()
            conn.execute("COMMIT")
        return self._row(updated)

    def finalize_worker_steer_claim(
        self,
        worker_id: str,
        token: str,
        epoch: int,
        *,
        target_run_id: str,
        target_expected_state: str,
        replacement_run_id: str,
        replacement_instruction: str,
        runtime_fields: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Interrupt one exact generation and queue its steer replacement atomically."""

        clean_target = str(target_run_id or "")
        clean_replacement = str(replacement_run_id or "")
        clean_instruction = str(replacement_instruction or "")
        if not clean_target or not clean_replacement or not clean_instruction:
            raise ValueError("Steer finalization requires an exact target and replacement")
        clean_fields = {
            key: value
            for key, value in runtime_fields.items()
            if key in RUNTIME_INFO_FIELD_NAMES
        }
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            worker = conn.execute(
                "SELECT * FROM workers WHERE worker_id = ?", (worker_id,)
            ).fetchone()
            target = conn.execute(
                "SELECT * FROM runs WHERE run_id = ? AND worker_id = ?",
                (clean_target, worker_id),
            ).fetchone()
            if (
                worker is None
                or target is None
                or str(worker["compute_release_token"] or "") != str(token)
                or int(worker["compute_release_epoch"] or 0) != int(epoch)
                or str(worker["compute_release_kind"] or "") != "steer_run"
                or str(worker["compute_release_scope"] or "") != "run"
                or str(worker["compute_release_target_run_id"] or "") != clean_target
                or str(worker["compute_release_replacement_run_id"] or "")
                != clean_replacement
                or str(target["started_at"] or "")
                != str(worker["compute_release_target_started_at"] or "")
            ):
                conn.execute("COMMIT")
                return None
            existing = conn.execute(
                "SELECT * FROM runs WHERE run_id = ?", (clean_replacement,)
            ).fetchone()
            if (
                existing is None
                or str(existing["worker_id"] or "") != worker_id
                or str(existing["state"] or "") != "queued"
                or str(existing["instruction"] or "") != clean_instruction
            ):
                conn.execute("COMMIT")
                return None
            now = utc_now()
            if str(target["state"] or "") in TERMINAL_RUN_STATES:
                self._insert_lifecycle_event(
                    conn,
                    operation_token=str(
                        worker["compute_release_operation_id"] or token
                    ),
                    operation_epoch=0,
                    operation_kind="steer_run",
                    worker=worker,
                    run_id=clean_target,
                    event_type="control.terminal_won",
                    message="Terminal run truth suppressed the steer replacement",
                    payload={
                        "operation_kind": "steer_run",
                        "terminal_state": str(target["state"] or ""),
                        "replacement_run_id": clean_replacement,
                    },
                )
                conn.execute(
                    """
                    UPDATE runs SET state = 'cancelled', ended_at = ?,
                        error_text = ?
                    WHERE run_id = ? AND worker_id = ? AND state = 'queued'
                    """,
                    (
                        now,
                        STEER_REPLACEMENT_SUPPRESSED_ERROR,
                        clean_replacement,
                        worker_id,
                    ),
                )
                conn.execute(
                    """
                    UPDATE delegations SET current_run_id = ?, updated_at = ?
                    WHERE worker_id = ?
                    """,
                    (clean_target, now, worker_id),
                )
                conn.execute(
                    """
                    UPDATE host_run_leases SET status = 'released', released_at = ?,
                        release_reason = 'steer_run_terminal_won',
                        reconciled_at = COALESCE(reconciled_at, ?)
                    WHERE worker_id = ? AND run_id = ? AND status = 'active'
                    """,
                    (now, now, worker_id, clean_target),
                )
                fields = {
                    **clean_fields,
                    "state": "ready",
                    **COMPUTE_OPERATION_CLEAR_FIELDS,
                    "updated_at": now,
                    "worker_id": worker_id,
                    "token": token,
                    "epoch": int(epoch),
                }
                assignments = ", ".join(
                    f"{key} = :{key}"
                    for key in fields
                    if key not in {"worker_id", "token", "epoch"}
                )
                cursor = conn.execute(
                    f"UPDATE workers SET {assignments} "
                    "WHERE worker_id = :worker_id AND compute_release_token = :token "
                    "AND compute_release_epoch = :epoch",
                    fields,
                )
                if cursor.rowcount != 1:
                    conn.execute("ROLLBACK")
                    return None
                updated_worker = conn.execute(
                    "SELECT * FROM workers WHERE worker_id = ?", (worker_id,)
                ).fetchone()
                updated_replacement = conn.execute(
                    "SELECT * FROM runs WHERE run_id = ?", (clean_replacement,)
                ).fetchone()
                conn.execute("COMMIT")
                return {
                    "worker": self._row(updated_worker),
                    "target_run": self._row(target),
                    "replacement_run": self._row(updated_replacement),
                    "target_transitioned": False,
                    "terminal_won": True,
                    "effect_ids": [],
                }
            if str(target["state"] or "") != str(target_expected_state):
                conn.execute("COMMIT")
                return None
            if (
                str(target_expected_state) != "queued"
                and not self.worker_control_runtime_proof_matches(worker)
            ):
                conn.execute("COMMIT")
                return None
            interrupted_state = (
                "cancelled" if str(target_expected_state) == "queued" else "interrupted"
            )
            conn.execute(
                """
                UPDATE runs
                SET state = ?, ended_at = ?, error_text = ?, retry_after = NULL,
                    failure_class = '', failure_retryable = 0,
                    failure_structured = 0, failure_user_message = '',
                    failure_recommended_recovery = '',
                    failure_diagnostic_summary = '', retry_attempts = 0,
                    last_retry_class = ''
                WHERE run_id = ? AND worker_id = ? AND state = ?
                """,
                (
                    interrupted_state,
                    now,
                    "Replaced by operator steer",
                    clean_target,
                    worker_id,
                    target_expected_state,
                ),
            )
            conn.execute(
                """
                UPDATE delegations SET current_run_id = ?, updated_at = ?
                WHERE worker_id = ?
                """,
                (clean_replacement, now, worker_id),
            )
            conn.execute(
                """
                UPDATE host_run_leases
                SET status = 'released', released_at = ?,
                    release_reason = 'steer_run_confirmed',
                    reconciled_at = COALESCE(reconciled_at, ?)
                WHERE worker_id = ? AND run_id = ? AND status = 'active'
                """,
                (now, now, worker_id, clean_target),
            )
            self._insert_lifecycle_event(
                conn,
                operation_token=str(worker["compute_release_operation_id"] or token),
                operation_epoch=0,
                operation_kind="steer_run",
                worker=worker,
                run_id=clean_target,
                event_type=f"run.{interrupted_state}",
                message="Run replaced by operator steer",
            )
            self._insert_lifecycle_event(
                conn,
                operation_token=str(worker["compute_release_operation_id"] or token),
                operation_epoch=0,
                operation_kind="steer_run",
                worker=worker,
                run_id=clean_target,
                event_type="worker.interrupted",
                message="Worker interrupted for operator steer",
            )
            self._insert_lifecycle_event(
                conn,
                operation_token=str(worker["compute_release_operation_id"] or token),
                operation_epoch=0,
                operation_kind="steer_run",
                worker=worker,
                run_id=clean_replacement,
                event_type="worker.steer",
                message="Replacement steer instruction queued",
            )
            effect_ids = self._enqueue_lifecycle_effects(
                conn,
                operation_token=str(worker["compute_release_operation_id"] or token),
                operation_epoch=0,
                operation_kind="steer_run",
                worker_id=worker_id,
                run_id=clean_replacement,
                effect_kinds=("callback.run_steered",),
            )
            fields: dict[str, Any] = {
                **clean_fields,
                "state": "starting",
                "last_run_id": clean_replacement,
                **COMPUTE_OPERATION_CLEAR_FIELDS,
                "updated_at": now,
                "worker_id": worker_id,
                "token": token,
                "epoch": int(epoch),
            }
            assignments = ", ".join(
                f"{key} = :{key}"
                for key in fields
                if key not in {"worker_id", "token", "epoch"}
            )
            cursor = conn.execute(
                f"UPDATE workers SET {assignments} "
                "WHERE worker_id = :worker_id AND compute_release_token = :token "
                "AND compute_release_epoch = :epoch",
                fields,
            )
            if cursor.rowcount != 1:
                conn.execute("ROLLBACK")
                return None
            updated_worker = conn.execute(
                "SELECT * FROM workers WHERE worker_id = ?", (worker_id,)
            ).fetchone()
            updated_target = conn.execute(
                "SELECT * FROM runs WHERE run_id = ?", (clean_target,)
            ).fetchone()
            updated_replacement = conn.execute(
                "SELECT * FROM runs WHERE run_id = ?", (clean_replacement,)
            ).fetchone()
            conn.execute("COMMIT")
        return {
            "worker": self._row(updated_worker),
            "target_run": self._row(updated_target),
            "replacement_run": self._row(updated_replacement),
            "effect_ids": effect_ids,
        }

    def finalize_worker_termination_claim(
        self,
        worker_id: str,
        token: str,
        epoch: int,
        *,
        compute_released_at: str,
        runtime_fields: dict[str, Any],
        error_text: str,
    ) -> dict[str, Any] | None:
        """Cancel all durable work and end a worker for the exact operation owner."""

        clean_fields = {
            key: value
            for key, value in runtime_fields.items()
            if key in RUNTIME_INFO_FIELD_NAMES
        }
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            worker = conn.execute(
                "SELECT * FROM workers WHERE worker_id = ?", (worker_id,)
            ).fetchone()
            if (
                worker is None
                or str(worker["compute_release_token"] or "") != str(token)
                or int(worker["compute_release_epoch"] or 0) != int(epoch)
                or str(worker["compute_release_kind"] or "")
                != "terminate_worker"
                or str(worker["compute_release_scope"] or "") != "worker"
            ):
                conn.execute("COMMIT")
                return None
            now = utc_now()
            cancelled_rows = conn.execute(
                """
                SELECT run_id FROM runs
                WHERE worker_id = ?
                  AND state IN ('queued', 'running', 'settling', 'paused', 'needs_input')
                ORDER BY queued_at ASC, run_id ASC
                """,
                (worker_id,),
            ).fetchall()
            cancelled_run_ids = [str(row["run_id"]) for row in cancelled_rows]
            conn.execute(
                """
                UPDATE runs
                SET state = 'cancelled', ended_at = ?, error_text = ?,
                    failure_class = '', failure_retryable = 0,
                    failure_structured = 0, failure_user_message = '',
                    failure_recommended_recovery = '',
                    failure_diagnostic_summary = '', retry_after = NULL,
                    retry_attempts = 0, last_retry_class = ''
                WHERE worker_id = ?
                  AND state IN ('queued', 'running', 'settling', 'paused', 'needs_input')
                """,
                (now, error_text, worker_id),
            )
            conn.execute(
                """
                UPDATE scheduled_runs
                SET state = 'cancelled', last_error = ?, updated_at = ?
                WHERE worker_id = ? AND state NOT IN ('completed', 'cancelled')
                """,
                (error_text, now, worker_id),
            )
            conn.execute(
                """
                UPDATE active_work_action_uses
                SET status = 'failed', last_error = 'worker_ended',
                    lease_expires_at = NULL, updated_at = ?
                WHERE work_ref IN (
                    SELECT work_ref FROM delegations WHERE worker_id = ?
                ) AND status = 'pending'
                """,
                (now, worker_id),
            )
            self._accept_cancel_actions_for_runs(
                conn, cancelled_run_ids, updated_at=now
            )
            effect_ids = self._enqueue_lifecycle_effects(
                conn,
                operation_token=str(worker["compute_release_operation_id"] or token),
                operation_epoch=0,
                operation_kind="terminate_worker",
                worker_id=worker_id,
                effect_kinds=(
                    "callback.worker_terminated",
                    "signed_links.revoke_worker",
                ),
            )
            event_id = self._insert_lifecycle_event(
                conn,
                operation_token=str(worker["compute_release_operation_id"] or token),
                operation_epoch=0,
                operation_kind="terminate_worker",
                worker=worker,
                run_id="",
                event_type="worker.terminated",
                message="Worker terminated",
                payload={"operation_kind": "terminate_worker"},
            )
            conn.execute(
                """
                UPDATE host_run_leases
                SET status = 'released', released_at = ?,
                    release_reason = 'worker_termination_confirmed',
                    reconciled_at = COALESCE(reconciled_at, ?)
                WHERE worker_id = ? AND status = 'active'
                """,
                (now, now, worker_id),
            )
            fields: dict[str, Any] = {
                **clean_fields,
                "state": "terminated",
                "compute_released_at": compute_released_at,
                **COMPUTE_OPERATION_CLEAR_FIELDS,
                "updated_at": now,
                "worker_id": worker_id,
                "token": token,
                "epoch": int(epoch),
            }
            assignments = ", ".join(
                f"{key} = :{key}"
                for key in fields
                if key not in {"worker_id", "token", "epoch"}
            )
            cursor = conn.execute(
                f"UPDATE workers SET {assignments} "
                "WHERE worker_id = :worker_id AND compute_release_token = :token "
                "AND compute_release_epoch = :epoch",
                fields,
            )
            if cursor.rowcount != 1:
                conn.execute("ROLLBACK")
                return None
            updated = conn.execute(
                "SELECT * FROM workers WHERE worker_id = ?", (worker_id,)
            ).fetchone()
            conn.execute("COMMIT")
        result = self._row(updated) or {}
        result["effect_ids"] = effect_ids
        result["event_id"] = event_id
        return result

    def begin_worker_compute_start(self, worker_id: str) -> dict[str, Any] | None:
        """Atomically reject direct compute starts while release owns the worker."""

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            worker = conn.execute(
                "SELECT * FROM workers WHERE worker_id = ?", (worker_id,)
            ).fetchone()
            if worker is None:
                conn.execute("COMMIT")
                return None
            token = str(worker["compute_release_token"] or "").strip()
            if (
                token
                or str(worker["work_stop_id"] or "")
                or str(worker["state"] or "") in {"stopping", "terminated"}
            ):
                conn.execute("COMMIT")
                return None
            conn.execute(
                """
                UPDATE workers SET state = 'starting', compute_released_at = NULL,
                    updated_at = ?
                WHERE worker_id = ?
                """,
                (utc_now(), worker_id),
            )
            updated = conn.execute(
                "SELECT * FROM workers WHERE worker_id = ?", (worker_id,)
            ).fetchone()
            conn.execute("COMMIT")
        return self._row(updated)

    def list_expired_compute_release_claim_worker_ids(
        self,
        *,
        now: datetime | None = None,
    ) -> list[str]:
        """List takeover candidates without ever removing their compute fence."""

        now_iso = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM workers
                WHERE compute_release_token != ''
                  AND compute_release_expires_at IS NOT NULL
                  AND compute_release_expires_at <= ?
                """,
                (now_iso,),
            ).fetchall()
        return [str(row["worker_id"]) for row in rows]

    def has_compute_release_claims(self) -> bool:
        with self._connect() as conn:
            return conn.execute(
                "SELECT 1 FROM workers WHERE compute_release_token != '' LIMIT 1"
            ).fetchone() is not None

    def worker_compute_release_claim_matches(
        self,
        worker_id: str,
        token: str,
        epoch: int,
    ) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM workers
                WHERE worker_id = ? AND compute_release_token = ?
                  AND compute_release_epoch = ?
                LIMIT 1
                """,
                (worker_id, token, int(epoch)),
            ).fetchone()
        return row is not None

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

    @staticmethod
    def _require_worker_work_admission(
        worker: sqlite3.Row | None,
        *,
        project_id: str | None = None,
    ) -> sqlite3.Row:
        """Validate a work-producing mutation under the caller's write txn."""

        if worker is None:
            raise WorkAdmissionError("worker_not_found", "Worker not found")
        if str(worker["state"] or "") == "terminated":
            raise WorkAdmissionError(
                "worker_terminated", "The workspace has ended"
            )
        if str(worker["work_stop_id"] or ""):
            if worker["work_stop_settled_at"] is None:
                raise WorkAdmissionError(
                    "work_stopping", "A workspace stop operation is in progress"
                )
            raise WorkAdmissionError("work_stopped", "The workspace work has stopped")
        if (
            str(worker["compute_release_token"] or "")
            and str(worker["compute_release_scope"] or "") in {"work", "worker"}
        ):
            raise WorkAdmissionError(
                "work_stopping", "A workspace stop operation is in progress"
            )
        if project_id is not None and str(worker["project_id"] or "") != str(project_id):
            raise WorkAdmissionError(
                "worker_scope_mismatch", "Worker does not belong to the requested project"
            )
        return worker

    @staticmethod
    def _insert_run_row(conn: sqlite3.Connection, data: dict[str, Any]) -> None:
        conn.execute(
            """
            INSERT INTO runs (
                run_id, worker_id, project_id, tenant_id, instruction, state, queued_at,
                started_at, ended_at, output_text, error_text, failure_class,
                failure_retryable, failure_structured, failure_user_message, failure_recommended_recovery,
                failure_diagnostic_summary, retry_after, retry_attempts, last_retry_class,
                native_session_id, native_capabilities_json, native_child_summary_json
            )
            VALUES (
                :run_id, :worker_id, :project_id, :tenant_id, :instruction, :state,
                :queued_at, :started_at, :ended_at, :output_text, :error_text,
                :failure_class, :failure_retryable, :failure_structured, :failure_user_message,
                :failure_recommended_recovery, :failure_diagnostic_summary,
                :retry_after, :retry_attempts, :last_retry_class,
                :native_session_id, :native_capabilities_json, :native_child_summary_json
            )
            """,
            data,
        )

    def create_run(self, worker_id: str, project_id: str, instruction: str, state: str = "queued") -> dict[str, Any]:
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
            "failure_structured": 0,
            "failure_user_message": "",
            "failure_recommended_recovery": "",
            "failure_diagnostic_summary": "",
            "retry_after": None,
            "retry_attempts": 0,
            "last_retry_class": "",
            "native_session_id": "",
            "native_capabilities_json": "{}",
            "native_child_summary_json": "{}",
        }
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            worker = self._require_worker_work_admission(
                conn.execute(
                    "SELECT * FROM workers WHERE worker_id = ?", (worker_id,)
                ).fetchone(),
                project_id=project_id,
            )
            data["tenant_id"] = str(worker["tenant_id"] or "local")
            self._insert_run_row(conn, data)
            conn.execute(
                "UPDATE workers SET last_run_id = ?, updated_at = ? WHERE worker_id = ?",
                (run_id, queued_at, worker_id),
            )
            conn.execute("COMMIT")
        return data

    def create_idempotent_run(
        self,
        *,
        run_id: str,
        worker_id: str,
        project_id: str,
        instruction: str,
    ) -> tuple[dict[str, Any], bool]:
        """Return an existing reservation or atomically guard and create it."""

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if existing is not None:
                conn.execute("COMMIT")
                if (
                    str(existing["worker_id"] or "") != str(worker_id)
                    or str(existing["project_id"] or "") != str(project_id)
                    or str(existing["instruction"] or "") != str(instruction)
                ):
                    raise ValueError(
                        "GlassHive idempotency key was reused with a different instruction"
                    )
                return self._row(existing) or {}, False
            worker = self._require_worker_work_admission(
                conn.execute(
                    "SELECT * FROM workers WHERE worker_id = ?", (worker_id,)
                ).fetchone(),
                project_id=project_id,
            )
            now = utc_now()
            data = {
                "run_id": str(run_id),
                "worker_id": str(worker_id),
                "project_id": str(project_id),
                "tenant_id": str(worker["tenant_id"] or "local"),
                "instruction": str(instruction),
                "state": "queued",
                "queued_at": now,
                "started_at": None,
                "ended_at": None,
                "output_text": "",
                "error_text": "",
                "failure_class": "",
                "failure_retryable": 0,
                "failure_structured": 0,
                "failure_user_message": "",
                "failure_recommended_recovery": "",
                "failure_diagnostic_summary": "",
                "retry_after": None,
                "retry_attempts": 0,
                "last_retry_class": "",
                "native_session_id": "",
                "native_capabilities_json": "{}",
                "native_child_summary_json": "{}",
            }
            self._insert_run_row(conn, data)
            conn.execute(
                "UPDATE workers SET last_run_id = ?, updated_at = ? WHERE worker_id = ?",
                (run_id, now, worker_id),
            )
            created = conn.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            conn.execute("COMMIT")
        return self._row(created) or {}, True

    @staticmethod
    def _validate_existing_run_action(
        row: sqlite3.Row,
        *,
        action: str,
        idempotency_key: str,
        project_id: str,
        worker_id: str,
        source_run_id: str,
        tenant_id: str,
        owner_id: str,
    ) -> None:
        expected = {
            "action": action,
            "idempotency_key": idempotency_key,
            "project_id": project_id,
            "worker_id": worker_id,
            "source_run_id": source_run_id,
            "tenant_id": tenant_id,
            "owner_id": owner_id,
        }
        if any(str(row[key] or "") != str(value or "") for key, value in expected.items()):
            raise RunActionError(
                "capability_replayed",
                "The action capability has already been consumed.",
                status_code=409,
            )

    @staticmethod
    def _require_run_action_scope(
        conn: sqlite3.Connection,
        *,
        project_id: str,
        worker_id: str,
        source_run_id: str,
        tenant_id: str,
        owner_id: str,
    ) -> tuple[sqlite3.Row, sqlite3.Row]:
        worker = conn.execute("SELECT * FROM workers WHERE worker_id = ?", (worker_id,)).fetchone()
        run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (source_run_id,)).fetchone()
        if (
            worker is None
            or run is None
            or str(worker["project_id"] or "") != project_id
            or str(run["project_id"] or "") != project_id
            or str(run["worker_id"] or "") != worker_id
            or str(worker["tenant_id"] or "") != tenant_id
            or str(run["tenant_id"] or "") != tenant_id
            or str(worker["owner_id"] or "") != owner_id
        ):
            raise RunActionError(
                "capability_scope_mismatch",
                "The action capability does not match this workspace run.",
                status_code=403,
            )
        return worker, run

    def create_retry_run_action(
        self,
        *,
        capability_id: str,
        idempotency_key: str,
        project_id: str,
        worker_id: str,
        source_run_id: str,
        tenant_id: str,
        owner_id: str,
        instruction: str,
    ) -> dict[str, Any]:
        now = utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM run_action_uses WHERE capability_id = ?", (capability_id,)
            ).fetchone()
            if existing is not None:
                self._validate_existing_run_action(
                    existing,
                    action="retry",
                    idempotency_key=idempotency_key,
                    project_id=project_id,
                    worker_id=worker_id,
                    source_run_id=source_run_id,
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                )
                new_run = conn.execute(
                    "SELECT * FROM runs WHERE run_id = ?", (existing["new_run_id"],)
                ).fetchone()
                if new_run is None:
                    raise RunActionError(
                        "action_result_unavailable",
                        "The prior action result is unavailable.",
                        status_code=409,
                    )
                conn.execute("COMMIT")
                return {
                    "action": self._row(existing),
                    "run": self._row(new_run),
                    "idempotent_replay": True,
                }

            worker, source_run = self._require_run_action_scope(
                conn,
                project_id=project_id,
                worker_id=worker_id,
                source_run_id=source_run_id,
                tenant_id=tenant_id,
                owner_id=owner_id,
            )
            prior_retry = conn.execute(
                """
                SELECT capability_id FROM run_action_uses
                WHERE action = 'retry'
                  AND source_run_id = ?
                  AND project_id = ?
                  AND worker_id = ?
                  AND tenant_id = ?
                  AND owner_id = ?
                LIMIT 1
                """,
                (source_run_id, project_id, worker_id, tenant_id, owner_id),
            ).fetchone()
            if prior_retry is not None:
                raise RunActionError(
                    "run_already_retried",
                    "This failed run has already been retried.",
                    status_code=409,
                )
            try:
                self._require_worker_work_admission(worker, project_id=project_id)
            except WorkAdmissionError as exc:
                # Keep the established run-actions API vocabulary while the
                # shared work-admission layer uses its canonical internal
                # worker_terminated code.
                public_code = (
                    "worker_ended" if exc.code == "worker_terminated" else exc.code
                )
                raise RunActionError(public_code, str(exc), status_code=409) from exc
            if str(source_run["state"] or "") != "failed" or not bool(source_run["failure_retryable"]):
                raise RunActionError(
                    "run_not_retryable",
                    "This run is not in a retryable failed state.",
                    status_code=409,
                )
            active = conn.execute(
                """
                SELECT run_id FROM runs
                WHERE worker_id = ? AND state IN ('queued', 'running')
                ORDER BY queued_at ASC LIMIT 1
                """,
                (worker_id,),
            ).fetchone()
            if active is not None:
                raise RunActionError(
                    "worker_has_active_run",
                    "This workspace already has active work.",
                    status_code=409,
                )

            new_run_id = f"run_{uuid.uuid4().hex[:10]}"
            run_data = {
                "run_id": new_run_id,
                "worker_id": worker_id,
                "project_id": project_id,
                "tenant_id": tenant_id,
                "instruction": instruction,
                "state": "queued",
                "queued_at": now,
                "started_at": None,
                "ended_at": None,
                "output_text": "",
                "error_text": "",
                "failure_class": "",
                "failure_retryable": 0,
                "failure_structured": 0,
                "failure_user_message": "",
                "failure_recommended_recovery": "",
                "failure_diagnostic_summary": "",
                "retry_after": None,
                "retry_attempts": 0,
                "last_retry_class": "",
            }
            conn.execute(
                """
                INSERT INTO runs (
                    run_id, worker_id, project_id, tenant_id, instruction, state, queued_at,
                    started_at, ended_at, output_text, error_text, failure_class,
                    failure_retryable, failure_structured, failure_user_message,
                    failure_recommended_recovery, failure_diagnostic_summary, retry_after,
                    retry_attempts, last_retry_class
                ) VALUES (
                    :run_id, :worker_id, :project_id, :tenant_id, :instruction, :state,
                    :queued_at, :started_at, :ended_at, :output_text, :error_text,
                    :failure_class, :failure_retryable, :failure_structured,
                    :failure_user_message, :failure_recommended_recovery,
                    :failure_diagnostic_summary, :retry_after, :retry_attempts,
                    :last_retry_class
                )
                """,
                run_data,
            )
            action_data = {
                "capability_id": capability_id,
                "action": "retry",
                "idempotency_key": idempotency_key,
                "project_id": project_id,
                "worker_id": worker_id,
                "source_run_id": source_run_id,
                "tenant_id": tenant_id,
                "owner_id": owner_id,
                "status": "completed",
                "result_code": "run_queued",
                "new_run_id": new_run_id,
                "created_at": now,
                "updated_at": now,
            }
            conn.execute(
                """
                INSERT INTO run_action_uses (
                    capability_id, action, idempotency_key, project_id, worker_id,
                    source_run_id, tenant_id, owner_id, status, result_code,
                    new_run_id, created_at, updated_at
                ) VALUES (
                    :capability_id, :action, :idempotency_key, :project_id, :worker_id,
                    :source_run_id, :tenant_id, :owner_id, :status, :result_code,
                    :new_run_id, :created_at, :updated_at
                )
                """,
                action_data,
            )
            conn.execute(
                "UPDATE workers SET last_run_id = ?, updated_at = ? WHERE worker_id = ?",
                (new_run_id, now, worker_id),
            )
            conn.execute("COMMIT")
        return {"action": action_data, "run": run_data, "idempotent_replay": False}

    def reserve_cancel_run_action(
        self,
        *,
        capability_id: str,
        idempotency_key: str,
        project_id: str,
        worker_id: str,
        source_run_id: str,
        tenant_id: str,
        owner_id: str,
    ) -> dict[str, Any]:
        now = utc_now()
        stale_before = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM run_action_uses WHERE capability_id = ?", (capability_id,)
            ).fetchone()
            worker, source_run = self._require_run_action_scope(
                conn,
                project_id=project_id,
                worker_id=worker_id,
                source_run_id=source_run_id,
                tenant_id=tenant_id,
                owner_id=owner_id,
            )
            if existing is not None:
                self._validate_existing_run_action(
                    existing,
                    action="cancel",
                    idempotency_key=idempotency_key,
                    project_id=project_id,
                    worker_id=worker_id,
                    source_run_id=source_run_id,
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                )
                action_status = str(existing["status"] or "")
                source_state = str(source_run["state"] or "")
                if action_status == "accepted":
                    conn.execute("COMMIT")
                    return {
                        "action": self._row(existing),
                        "idempotent_replay": True,
                        "should_execute": False,
                    }
                if str(worker["state"] or "") == "terminated":
                    raise RunActionError("worker_ended", "The workspace has ended.", status_code=409)
                if action_status == "conflict":
                    code = str(existing["result_code"] or "run_not_active")
                    raise RunActionError(
                        code,
                        "The run completed before cancellation could be accepted."
                        if code == "run_already_completed"
                        else "This run is no longer active.",
                        status_code=409,
                        details={"state": source_state},
                    )
                if source_state == "completed":
                    conn.execute(
                        """
                        UPDATE run_action_uses
                        SET status = 'conflict', result_code = 'run_already_completed', updated_at = ?
                        WHERE capability_id = ?
                        """,
                        (now, capability_id),
                    )
                    conn.execute("COMMIT")
                    raise RunActionError(
                        "run_already_completed",
                        "The run completed before cancellation could be accepted.",
                        status_code=409,
                        details={"state": "completed"},
                    )
                if source_state not in {"running", "interrupted", "cancelled"}:
                    raise RunActionError(
                        "run_not_active",
                        "This run is no longer active.",
                        status_code=409,
                        details={"state": source_state},
                    )
                if source_state in {"interrupted", "cancelled"}:
                    conn.execute(
                        """
                        UPDATE run_action_uses
                        SET status = 'accepted', result_code = 'cancellation_requested', updated_at = ?
                        WHERE capability_id = ?
                        """,
                        (now, capability_id),
                    )
                    accepted = conn.execute(
                        "SELECT * FROM run_action_uses WHERE capability_id = ?", (capability_id,)
                    ).fetchone()
                    conn.execute("COMMIT")
                    return {
                        "action": self._row(accepted),
                        "idempotent_replay": True,
                        "should_execute": False,
                    }
                should_execute = action_status in {"failed", "reserved"} or (
                    action_status == "executing" and str(existing["updated_at"] or "") <= stale_before
                )
                if should_execute:
                    conn.execute(
                        """
                        UPDATE run_action_uses
                        SET status = 'executing', result_code = 'cancellation_requested', updated_at = ?
                        WHERE capability_id = ?
                        """,
                        (now, capability_id),
                    )
                    claimed = conn.execute(
                        "SELECT * FROM run_action_uses WHERE capability_id = ?", (capability_id,)
                    ).fetchone()
                    conn.execute("COMMIT")
                    return {
                        "action": self._row(claimed),
                        "idempotent_replay": True,
                        "should_execute": True,
                    }
                conn.execute("COMMIT")
                return {
                    "action": self._row(existing),
                    "idempotent_replay": True,
                    "should_execute": False,
                }
            if str(worker["state"] or "") == "terminated":
                raise RunActionError("worker_ended", "The workspace has ended.", status_code=409)
            source_state = str(source_run["state"] or "")
            if source_state == "completed":
                raise RunActionError(
                    "run_already_completed",
                    "The run completed before cancellation could be accepted.",
                    status_code=409,
                    details={"state": "completed"},
                )
            if source_state != "running":
                raise RunActionError(
                    "run_not_active",
                    "This run is no longer active.",
                    status_code=409,
                    details={"state": source_state},
                )
            action_data = {
                "capability_id": capability_id,
                "action": "cancel",
                "idempotency_key": idempotency_key,
                "project_id": project_id,
                "worker_id": worker_id,
                "source_run_id": source_run_id,
                "tenant_id": tenant_id,
                "owner_id": owner_id,
                "status": "executing",
                "result_code": "cancellation_requested",
                "new_run_id": None,
                "created_at": now,
                "updated_at": now,
            }
            conn.execute(
                """
                INSERT INTO run_action_uses (
                    capability_id, action, idempotency_key, project_id, worker_id,
                    source_run_id, tenant_id, owner_id, status, result_code,
                    new_run_id, created_at, updated_at
                ) VALUES (
                    :capability_id, :action, :idempotency_key, :project_id, :worker_id,
                    :source_run_id, :tenant_id, :owner_id, :status, :result_code,
                    :new_run_id, :created_at, :updated_at
                )
                """,
                action_data,
            )
            conn.execute("COMMIT")
        return {"action": action_data, "idempotent_replay": False, "should_execute": True}

    def update_run_action_result(
        self,
        capability_id: str,
        *,
        status: str,
        result_code: str,
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE run_action_uses
                SET status = ?, result_code = ?, updated_at = ?
                WHERE capability_id = ?
                """,
                (status, result_code, utc_now(), capability_id),
            )
            row = conn.execute(
                "SELECT * FROM run_action_uses WHERE capability_id = ?", (capability_id,)
            ).fetchone()
        return self._row(row)

    def get_run_action(self, capability_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM run_action_uses WHERE capability_id = ?", (capability_id,)
            ).fetchone()
        return self._row(row)

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

    @staticmethod
    def _run_is_owned_by_destructive_claim(
        worker: sqlite3.Row, run_id: str
    ) -> bool:
        return bool(
            str(worker["compute_release_token"] or "")
            and str(worker["compute_release_target_run_id"] or "") == str(run_id)
            and str(worker["compute_release_kind"] or "")
            in {
                "pause_run",
                "resume_run",
                "interrupt_run",
                "steer_run",
                "max_duration",
                "stop_run",
                "terminate_worker",
            }
        )

    def update_run(self, run_id: str, **fields: Any) -> dict[str, Any] | None:
        if not fields:
            return self.get_run(run_id)
        if str(fields.get("state") or "") == "cancelled":
            for key, value in CANCELLATION_CLEAR_FIELDS.items():
                fields.setdefault(key, value)
        assignments = ", ".join(f"{key} = :{key}" for key in fields.keys())
        fields["run_id"] = run_id
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run = conn.execute(
                "SELECT worker_id, state FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                conn.execute("COMMIT")
                return None
            worker = conn.execute(
                "SELECT * FROM workers WHERE worker_id = ?", (run["worker_id"],)
            ).fetchone()
            projected_state = str(fields.get("state", run["state"]) or "")
            state_changes = projected_state != str(run["state"] or "")
            if state_changes and worker is not None and self._run_is_owned_by_destructive_claim(
                worker, run_id
            ) and projected_state not in TERMINAL_RUN_STATES:
                conn.execute("COMMIT")
                return None
            if projected_state in NONTERMINAL_RUN_STATES:
                self._require_worker_work_admission(
                    worker
                )
            conn.execute(f"UPDATE runs SET {assignments} WHERE run_id = :run_id", fields)
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            conn.execute("COMMIT")
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

    def list_due_retry_worker_ids(self, now_iso: str | None = None, limit: int = 1000) -> list[str]:
        now_iso = now_iso or utc_now()
        with self._connect() as conn:
            rows = conn.execute(
                """
                WITH due_workers AS (
                    SELECT
                        runs.worker_id,
                        workers.tenant_id,
                        workers.owner_id,
                        MIN(runs.queued_at) AS first_queued_at
                    FROM runs
                    JOIN workers ON workers.worker_id = runs.worker_id
                    WHERE runs.state = 'queued'
                      AND workers.state NOT IN (
                          'paused', 'needs_input', 'stopping', 'terminated'
                      )
                      AND (runs.retry_after IS NULL OR runs.retry_after = '' OR runs.retry_after <= ?)
                    GROUP BY runs.worker_id, workers.tenant_id, workers.owner_id
                ), ranked AS (
                    SELECT
                        worker_id,
                        tenant_id,
                        owner_id,
                        first_queued_at,
                        ROW_NUMBER() OVER (
                            PARTITION BY tenant_id, owner_id
                            ORDER BY first_queued_at ASC, worker_id ASC
                        ) AS owner_round
                    FROM due_workers
                )
                SELECT worker_id
                FROM ranked
                ORDER BY owner_round ASC, first_queued_at ASC,
                         tenant_id ASC, owner_id ASC, worker_id ASC
                LIMIT ?
                """,
                (now_iso, max(1, int(limit))),
            ).fetchall()
        return [str(row["worker_id"]) for row in rows]

    def next_queued_retry_after(self, now_iso: str | None = None) -> str | None:
        now_iso = now_iso or utc_now()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT MIN(runs.retry_after) AS retry_after
                FROM runs
                JOIN workers ON workers.worker_id = runs.worker_id
                WHERE runs.state = 'queued'
                  AND workers.state NOT IN (
                      'paused', 'needs_input', 'stopping', 'terminated'
                  )
                  AND runs.retry_after IS NOT NULL
                  AND runs.retry_after != ''
                  AND runs.retry_after > ?
                """,
                (now_iso,),
            ).fetchone()
        if row is None:
            return None
        return str(row["retry_after"] or "") or None

    def claim_next_queued_run(self, worker_id: str) -> dict[str, Any] | None:
        now_iso = utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            worker = conn.execute(
                "SELECT * FROM workers WHERE worker_id = ?", (worker_id,)
            ).fetchone()
            if worker is None:
                conn.execute("COMMIT")
                return None
            release_token = str(worker["compute_release_token"] or "").strip()
            if release_token or str(worker["work_stop_id"] or "") or str(worker["state"] or "") in {
                "paused",
                "needs_input",
                "stopping",
                "terminated",
            }:
                conn.execute("COMMIT")
                return None
            if conn.execute(
                """
                SELECT 1 FROM runs
                WHERE worker_id = ? AND state IN ('running', 'settling', 'paused', 'needs_input')
                LIMIT 1
                """,
                (worker_id,),
            ).fetchone() is not None:
                conn.execute("COMMIT")
                return None
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
            conn.execute(
                """
                UPDATE workers SET state = 'starting', compute_released_at = NULL,
                    updated_at = ?
                WHERE worker_id = ?
                """,
                (utc_now(), worker_id),
            )
            claimed = conn.execute("SELECT * FROM runs WHERE run_id = ?", (row["run_id"],)).fetchone()
            conn.execute("COMMIT")
        return self._row(claimed)

    def get_active_run(self, worker_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE worker_id = ? AND state IN ('running', 'settling') ORDER BY started_at DESC LIMIT 1",
                (worker_id,),
            ).fetchone()
        return self._row(row)

    def get_controllable_run(self, worker_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM runs
                WHERE worker_id = ? AND state IN ('queued', 'running', 'settling', 'paused')
                ORDER BY COALESCE(started_at, queued_at) DESC
                LIMIT 1
                """,
                (worker_id,),
            ).fetchone()
        return self._row(row)

    def list_nonterminal_runs_for_worker(self, worker_id: str) -> list[dict[str, Any]]:
        """Return every durable mission run that still requires control or execution."""

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM runs
                WHERE worker_id = ?
                  AND state IN ('queued', 'running', 'settling', 'paused', 'needs_input')
                ORDER BY
                    CASE state
                        WHEN 'running' THEN 0
                        WHEN 'settling' THEN 1
                        WHEN 'paused' THEN 2
                        WHEN 'needs_input' THEN 3
                        ELSE 4
                    END,
                    COALESCE(started_at, queued_at) ASC,
                    run_id ASC
                """,
                (worker_id,),
            ).fetchall()
        return self._rows(rows)

    def cancel_queued_runs_for_worker(
        self,
        worker_id: str,
        *,
        error_text: str,
        exclude_run_id: str = "",
    ) -> list[dict[str, Any]]:
        """Cancel pending siblings as one mission-scoped Stop subeffect."""

        now = utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT * FROM runs
                WHERE worker_id = ? AND state IN ('queued', 'needs_input')
                  AND (? = '' OR run_id != ?)
                ORDER BY queued_at ASC, run_id ASC
                """,
                (worker_id, exclude_run_id, exclude_run_id),
            ).fetchall()
            run_ids = [str(row["run_id"]) for row in rows]
            if run_ids:
                placeholders = ",".join("?" for _ in run_ids)
                conn.execute(
                    f"""
                    UPDATE runs
                    SET state = 'cancelled', ended_at = ?, error_text = ?,
                        failure_class = '', failure_retryable = 0,
                        failure_structured = 0, failure_user_message = '',
                        failure_recommended_recovery = '',
                        failure_diagnostic_summary = '', retry_after = NULL,
                        retry_attempts = 0, last_retry_class = ''
                    WHERE run_id IN ({placeholders})
                      AND state IN ('queued', 'needs_input')
                    """,
                    (now, error_text, *run_ids),
                )
            conn.execute("COMMIT")
        return [
            self.get_run(run_id)
            for run_id in run_ids
            if self.get_run(run_id) is not None
        ]

    def transition_run_if_state(
        self,
        run_id: str,
        expected_state: str,
        state: str,
        **fields: Any,
    ) -> dict[str, Any] | None:
        if state == "cancelled":
            for key, value in CANCELLATION_CLEAR_FIELDS.items():
                fields.setdefault(key, value)
        values = {"run_id": run_id, "expected_state": expected_state, "state": state, **fields}
        assignments = ["state = :state"]
        assignments.extend(f"{key} = :{key}" for key in fields)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run = conn.execute(
                "SELECT worker_id FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                conn.execute("COMMIT")
                return None
            worker = conn.execute(
                "SELECT * FROM workers WHERE worker_id = ?", (run["worker_id"],)
            ).fetchone()
            if (
                worker is not None
                and self._run_is_owned_by_destructive_claim(worker, run_id)
                and state not in TERMINAL_RUN_STATES
            ):
                conn.execute("COMMIT")
                return None
            if state in NONTERMINAL_RUN_STATES:
                self._require_worker_work_admission(
                    worker
                )
            cursor = conn.execute(
                f"UPDATE runs SET {', '.join(assignments)} WHERE run_id = :run_id AND state = :expected_state",
                values,
            )
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            conn.execute("COMMIT")
        updated = self._row(row)
        return updated if cursor.rowcount and updated else None

    def mark_run_needs_input(
        self,
        run_id: str,
        *,
        expected_state: str = "running",
        error_text: str,
        failure_class: str,
        failure_user_message: str,
    ) -> dict[str, Any] | None:
        return self.transition_run_if_state(
            run_id,
            expected_state,
            "needs_input",
            ended_at=None,
            error_text=error_text,
            retry_after=None,
            failure_class=failure_class,
            failure_retryable=0,
            failure_structured=1,
            failure_user_message=failure_user_message,
            failure_recommended_recovery="Provide the requested authorization, then resume this work.",
            failure_diagnostic_summary="Deferred broker admission requires user input.",
        )

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

    def has_queued_capacity_retry(self, worker_id: str) -> bool:
        """Return whether restart recovery may reactivate a persisted capacity wait."""

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM runs
                WHERE worker_id = ?
                  AND state = 'queued'
                  AND retry_after IS NOT NULL
                  AND retry_after != ''
                  AND failure_retryable = 1
                  AND failure_structured = 1
                  AND failure_class IN ('host_capacity', 'host_worker_busy')
                  AND last_retry_class IN ('host_capacity', 'host_worker_busy')
                LIMIT 1
                """,
                (worker_id,),
            ).fetchone()
        return row is not None

    def has_active_operator_pause(self, worker_id: str) -> bool:
        """Return whether the latest explicit pause/resume intent is still paused."""

        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT event_type FROM events
                WHERE worker_id = ?
                  AND event_type IN (
                      'worker.paused', 'worker.resumed', 'worker.resumed_by_alias'
                  )
                -- Events are append-only in this local SQLite store. rowid is
                -- the durable transition order and avoids timestamp ties.
                ORDER BY rowid DESC
                LIMIT 1
                """,
                (worker_id,),
            ).fetchone()
        return row is not None and str(row["event_type"] or "") == "worker.paused"

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
        consume_retry_budget: bool = True,
        **failure_fields: Any,
    ) -> dict[str, Any] | None:
        normalized_failure_fields = _normalized_failure_fields(failure_fields)
        update_fields = {
            "run_id": run_id,
            "retry_after": retry_after,
            "error_text": error_text,
            "last_retry_class": str(last_retry_class or normalized_failure_fields.get("failure_class") or ""),
            "retry_attempt_increment": 1 if consume_retry_budget else 0,
            **normalized_failure_fields,
        }
        failure_assignments = "".join(f", {key} = :{key}" for key in normalized_failure_fields.keys())
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run = conn.execute(
                "SELECT worker_id FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                conn.execute("COMMIT")
                return None
            self._require_worker_work_admission(
                conn.execute(
                    "SELECT * FROM workers WHERE worker_id = ?", (run["worker_id"],)
                ).fetchone()
            )
            conn.execute(
                f"""
                UPDATE runs
                SET state = 'queued',
                    ended_at = NULL,
                    retry_after = :retry_after,
                    retry_attempts = COALESCE(retry_attempts, 0) + :retry_attempt_increment,
                    last_retry_class = :last_retry_class,
                    error_text = :error_text{failure_assignments}
                WHERE run_id = :run_id
                  AND state IN ('queued', 'running')
                """,
                update_fields,
            )
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            conn.execute("COMMIT")
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
        fields.update(_terminal_failure_fields(state, failure_fields))
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
        normalized_failure_fields = _terminal_failure_fields(state, failure_fields)
        cancellation_retry_fields = (
            {
                "retry_attempts": 0,
                "last_retry_class": "",
            }
            if state == "cancelled"
            else {}
        )
        update_fields = {
            "state": state,
            "ended_at": utc_now(),
            "output_text": output_text,
            "error_text": error_text,
            "retry_after": None,
            **normalized_failure_fields,
            **cancellation_retry_fields,
            "run_id": run_id,
            "expected_state": expected_state,
        }
        failure_assignments = "".join(f", {key} = :{key}" for key in normalized_failure_fields.keys())
        failure_assignments += "".join(
            f", {key} = :{key}" for key in cancellation_retry_fields
        )
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run = conn.execute(
                "SELECT worker_id FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                conn.execute("COMMIT")
                return None
            worker = conn.execute(
                "SELECT * FROM workers WHERE worker_id = ?", (run["worker_id"],)
            ).fetchone()
            if (
                worker is not None
                and self._run_is_owned_by_destructive_claim(worker, run_id)
                and state not in TERMINAL_RUN_STATES
            ):
                conn.execute("COMMIT")
                return None
            if state in NONTERMINAL_RUN_STATES:
                self._require_worker_work_admission(
                    worker
                )
            cur = conn.execute(
                f"""
                UPDATE runs
                SET state = :state, ended_at = :ended_at, output_text = :output_text,
                    error_text = :error_text, retry_after = :retry_after{failure_assignments}
                WHERE run_id = :run_id AND state = :expected_state
                """,
                update_fields,
            )
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            conn.execute("COMMIT")
        updated = self._row(row)
        if cur.rowcount and updated:
            return updated
        return None

    def cancel_pending_runs(self, worker_id: str, error_text: str, state: str = "cancelled") -> int:
        if state not in TERMINAL_RUN_STATES:
            raise ValueError("Pending work may only be cancelled into a terminal state")
        with self._connect() as conn:
            schedule_state = "cancelled" if state == "cancelled" else "failed"
            conn.execute(
                """
                UPDATE scheduled_runs
                SET state = ?, last_error = ?, updated_at = ?
                WHERE queued_run_id IN (
                    SELECT run_id FROM runs WHERE worker_id = ? AND state IN (
                        'queued', 'running', 'settling', 'paused', 'needs_input'
                    )
                )
                  AND state IN ('queued', 'running')
                """,
                (schedule_state, error_text, utc_now(), worker_id),
            )
            cur = conn.execute(
                """
                UPDATE runs SET state = ?, ended_at = ?, error_text = ?,
                    failure_class = CASE WHEN ? = 'cancelled' THEN '' ELSE failure_class END,
                    failure_retryable = CASE WHEN ? = 'cancelled' THEN 0 ELSE failure_retryable END,
                    failure_structured = CASE WHEN ? = 'cancelled' THEN 0 ELSE failure_structured END,
                    failure_user_message = CASE WHEN ? = 'cancelled' THEN '' ELSE failure_user_message END,
                    failure_recommended_recovery = CASE WHEN ? = 'cancelled' THEN '' ELSE failure_recommended_recovery END,
                    failure_diagnostic_summary = CASE WHEN ? = 'cancelled' THEN '' ELSE failure_diagnostic_summary END,
                    retry_after = CASE WHEN ? = 'cancelled' THEN NULL ELSE retry_after END,
                    retry_attempts = CASE WHEN ? = 'cancelled' THEN 0 ELSE retry_attempts END,
                    last_retry_class = CASE WHEN ? = 'cancelled' THEN '' ELSE last_retry_class END
                WHERE worker_id = ?
                  AND state IN ('queued', 'running', 'settling', 'paused', 'needs_input')
                """,
                (
                    state,
                    utc_now(),
                    error_text,
                    state,
                    state,
                    state,
                    state,
                    state,
                    state,
                    state,
                    state,
                    state,
                    worker_id,
                ),
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
            conn.execute("BEGIN IMMEDIATE")
            worker = self._require_worker_work_admission(
                conn.execute(
                    "SELECT * FROM workers WHERE worker_id = ?", (worker_id,)
                ).fetchone(),
                project_id=project_id,
            )
            if (
                str(worker["owner_id"] or "") != str(owner_id)
                or str(worker["tenant_id"] or "") != str(tenant_id or "local")
            ):
                conn.execute("ROLLBACK")
                raise RuntimeError("Schedule scope does not match the workspace")
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
            conn.execute("COMMIT")
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
            schedule = conn.execute(
                "SELECT * FROM scheduled_runs WHERE schedule_id = ?", (schedule_id,)
            ).fetchone()
            if schedule is None or str(schedule["state"] or "") != "pending":
                conn.execute("COMMIT")
                return None
            self._require_worker_work_admission(
                conn.execute(
                    "SELECT * FROM workers WHERE worker_id = ?",
                    (schedule["worker_id"],),
                ).fetchone(),
                project_id=str(schedule["project_id"] or ""),
            )
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

    def rebind_schedule_run(
        self,
        source_run_id: str,
        replacement_run_id: str,
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE scheduled_runs
                SET queued_run_id = ?, state = 'queued', last_error = '', updated_at = ?
                WHERE queued_run_id = ? AND state = 'needs_input'
                """,
                (replacement_run_id, utc_now(), source_run_id),
            )
            row = conn.execute(
                "SELECT * FROM scheduled_runs WHERE queued_run_id = ?",
                (replacement_run_id,),
            ).fetchone()
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
        payload: dict[str, Any] | None = None,
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
            "payload_json": json.dumps(payload or {}, ensure_ascii=False, sort_keys=True),
            "created_at": utc_now(),
        }
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO events (event_id, project_id, worker_id, tenant_id, run_id, event_type, message, payload_json, created_at) VALUES (:event_id, :project_id, :worker_id, :tenant_id, :run_id, :event_type, :message, :payload_json, :created_at)",
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
            "http_accepted_at": None,
        }
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO callback_outbox (
                    callback_id, project_id, worker_id, tenant_id, run_id, event_type, url, payload_json,
                    status, attempts, last_error, created_at, updated_at, delivered_at,
                    http_accepted_at
                )
                VALUES (
                    :callback_id, :project_id, :worker_id, :tenant_id, :run_id, :event_type, :url, :payload_json,
                    :status, :attempts, :last_error, :created_at, :updated_at, :delivered_at,
                    :http_accepted_at
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
                    delivered_at = NULL,
                    http_accepted_at = NULL
                WHERE callback_outbox.status NOT IN (
                    'http_accepted', 'delivered', 'dead_lettered'
                )
                """,
                data,
            )
            row = conn.execute("SELECT * FROM callback_outbox WHERE callback_id = ?", (callback_id,)).fetchone()
        return dict(row)

    def insert_callback_outbox_once(
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
        """Insert one immutable callback intent without rewinding delivery state."""

        clean_callback_id = str(callback_id or "").strip()
        if not clean_callback_id:
            raise ValueError("Callback insertion requires a deterministic identity")
        now = utc_now()
        worker = self.get_worker(worker_id) or {}
        project = self.get_project(project_id) or {}
        tenant_id = str(
            worker.get("tenant_id") or project.get("tenant_id") or "local"
        )
        with self._connect() as conn:
            inserted = conn.execute(
                """
                INSERT OR IGNORE INTO callback_outbox (
                    callback_id, project_id, worker_id, tenant_id, run_id,
                    event_type, url, payload_json, status, attempts, last_error,
                    created_at, updated_at, delivered_at, http_accepted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, '', ?, ?, NULL, NULL)
                """,
                (
                    clean_callback_id,
                    str(project_id or ""),
                    str(worker_id or ""),
                    tenant_id,
                    run_id,
                    str(event_type or ""),
                    str(url or ""),
                    str(payload_json or "{}"),
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM callback_outbox WHERE callback_id = ?",
                (clean_callback_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("Callback outbox insertion did not persist")
        return {**dict(row), "_inserted": inserted.rowcount == 1}

    def get_callback_outbox(self, callback_id: str) -> dict[str, Any] | None:
        """Read one durable callback intent without changing delivery state."""

        clean_callback_id = str(callback_id or "").strip()
        if not clean_callback_id:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM callback_outbox WHERE callback_id = ?",
                (clean_callback_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def mark_callback_http_accepted(
        self,
        callback_id: str,
        *,
        attempts: int,
        payload_json: str,
    ) -> dict[str, Any] | None:
        accepted_at = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE callback_outbox
                SET status = 'http_accepted',
                    attempts = attempts + ?,
                    payload_json = ?,
                    last_error = '',
                    updated_at = ?,
                    delivered_at = NULL,
                    http_accepted_at = ?
                WHERE callback_id = ?
                """,
                (attempts, payload_json, accepted_at, accepted_at, callback_id),
            )
            row = conn.execute("SELECT * FROM callback_outbox WHERE callback_id = ?", (callback_id,)).fetchone()
        return self._row(row)

    def accept_cancel_actions_for_run(self, run_id: str) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE run_action_uses
                SET status = 'accepted',
                    result_code = 'cancellation_confirmed',
                    updated_at = ?
                WHERE source_run_id = ?
                  AND action = 'cancel'
                  AND status IN ('reserved', 'executing')
                """,
                (utc_now(), run_id),
            )
        return cur.rowcount

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

    def list_pending_callbacks(
        self, limit: int = 50, *, created_before: str | None = None
    ) -> list[dict[str, Any]]:
        clean_created_before = str(created_before or "").strip()
        created_clause = " AND created_at <= ?" if clean_created_before else ""
        params: list[Any] = []
        if clean_created_before:
            params.append(clean_created_before)
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM callback_outbox
                WHERE status = 'pending'
                {created_clause}
                ORDER BY updated_at ASC
                LIMIT ?
                """,
                params,
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
