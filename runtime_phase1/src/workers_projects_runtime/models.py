from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

ProjectStatus = Literal["active", "paused", "completed", "archived", "failed"]
WorkerState = Literal[
    "created",
    "starting",
    "ready",
    "running",
    "paused",
    "failed",
    "terminated",
]
RunState = Literal["queued", "running", "interrupted", "paused", "completed", "failed", "cancelled"]
ScheduleState = Literal["pending", "running", "queued", "completed", "failed", "cancelled"]
DesktopActionName = Literal["terminal", "files", "browser", "focus_browser", "codex", "claude", "openclaw"]
ExecutionMode = Literal["docker", "host"]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CreateProjectRequest(BaseModel):
    owner_id: str
    title: str
    goal: str
    default_worker_profile: str = ""


class ProjectResponse(BaseModel):
    project_id: str
    tenant_id: str = "local"
    owner_id: str
    title: str
    goal: str
    status: ProjectStatus
    summary: str = ""
    default_worker_profile: str
    created_at: str
    updated_at: str


class CreateWorkerRequest(BaseModel):
    owner_id: str
    name: str
    role: str
    profile: str = "openclaw-general"
    backend: str = "openclaw"
    execution_mode: ExecutionMode = "docker"
    alias: str | None = None
    workspace_root: str | None = None
    bootstrap_profile: str | None = None
    bootstrap_bundle: dict[str, object] | None = None
    start_synchronously: bool = True


class DuplicateWorkerRequest(BaseModel):
    owner_id: str
    source_worker_id: str
    name: str
    role: str


class WorkerResponse(BaseModel):
    worker_id: str
    project_id: str
    tenant_id: str = "local"
    owner_id: str
    name: str
    role: str
    profile: str
    backend: str
    execution_mode: ExecutionMode = "docker"
    alias: str | None = None
    runtime: str = "openclaw"
    model: str = ""
    state: WorkerState
    bootstrap_profile: str | None = None
    gateway_url: str | None = None
    takeover_url: str | None = None
    control_url: str | None = None
    gateway_port: int | None = None
    session_key: str | None = None
    state_dir: str | None = None
    workspace_dir: str | None = None
    workspace_root: str | None = None
    favorite: bool = False
    compute_released_at: str | None = None
    last_run_id: str | None = None
    last_error: str | None = None
    created_at: str
    updated_at: str


class AssignRunRequest(BaseModel):
    instruction: str = Field(min_length=1)
    effort: str | None = None
    bootstrap_bundle: dict[str, object] | None = None


class SendMessageRequest(BaseModel):
    message: str = Field(min_length=1)


class ScheduleRunRequest(BaseModel):
    instruction: str = Field(min_length=1)
    run_at: str | None = None
    schedule_text: str | None = None
    delay_seconds: int | None = Field(default=None, ge=0)
    bootstrap_bundle: dict[str, object] | None = None


class UpdateWorkerMetadataRequest(BaseModel):
    favorite: bool | None = None
    name: str | None = None


class UserPreferencesResponse(BaseModel):
    tenant_id: str = "local"
    owner_id: str
    default_worker_profile: str = ""
    codex_reasoning_effort: str = ""
    claude_effort: str = ""
    openclaw_effort: str = ""
    updated_at: str


class UpdateUserPreferencesRequest(BaseModel):
    default_worker_profile: str | None = None
    codex_reasoning_effort: str | None = None
    claude_effort: str | None = None
    openclaw_effort: str | None = None


class DesktopActionRequest(BaseModel):
    action: DesktopActionName
    url: str | None = None
    run_id: str | None = None


class LaunchFailureRequest(BaseModel):
    reason: str = Field(min_length=1)


class RunResponse(BaseModel):
    run_id: str
    worker_id: str
    project_id: str
    tenant_id: str = "local"
    instruction: str
    state: RunState
    queued_at: str
    started_at: str | None = None
    ended_at: str | None = None
    output_text: str = ""
    error_text: str = ""
    failure_class: str = ""
    failure_retryable: bool = False
    failure_user_message: str = ""
    failure_recommended_recovery: str = ""
    failure_diagnostic_summary: str = ""


class ScheduleResponse(BaseModel):
    schedule_id: str
    worker_id: str
    project_id: str
    tenant_id: str = "local"
    owner_id: str
    instruction: str
    schedule_text: str = ""
    run_at: str
    state: ScheduleState
    queued_run_id: str | None = None
    last_error: str = ""
    created_at: str
    updated_at: str


class EventResponse(BaseModel):
    event_id: str
    project_id: str
    worker_id: str
    tenant_id: str = "local"
    run_id: str | None = None
    event_type: str
    message: str
    created_at: str


class TakeoverInfo(BaseModel):
    supported: bool
    url: str | None = None
    mode: str | None = None
    notes: str | None = None


class DesktopActionResponse(BaseModel):
    action: str
    status: str
    mode: str
    url: str | None = None
    view_url: str | None = None
    notes: str | None = None


class MetricsSummary(BaseModel):
    projects: int
    workers: int
    runs: int
    queued_runs: int
    active_runs: int
    events: int
    callback_pending: int = 0
    callback_delivering: int = 0
    callback_dead_lettered: int = 0
    callback_max_attempts: int = 0
    callback_oldest_pending_age_seconds: int = 0
