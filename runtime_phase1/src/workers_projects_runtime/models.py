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
DesktopActionName = Literal["terminal", "files", "browser", "focus_browser", "codex", "claude", "openclaw"]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class CreateProjectRequest(BaseModel):
    owner_id: str
    title: str
    goal: str
    default_worker_profile: str = "openclaw-general"


class ProjectResponse(BaseModel):
    project_id: str
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
    bootstrap_profile: str | None = None
    bootstrap_bundle: dict[str, object] | None = None


class WorkerResponse(BaseModel):
    worker_id: str
    project_id: str
    owner_id: str
    name: str
    role: str
    profile: str
    backend: str
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
    last_run_id: str | None = None
    last_error: str | None = None
    created_at: str
    updated_at: str


class AssignRunRequest(BaseModel):
    instruction: str = Field(min_length=1)


class SendMessageRequest(BaseModel):
    message: str = Field(min_length=1)


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
    instruction: str
    state: RunState
    queued_at: str
    started_at: str | None = None
    ended_at: str | None = None
    output_text: str = ""
    error_text: str = ""


class EventResponse(BaseModel):
    event_id: str
    project_id: str
    worker_id: str
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
