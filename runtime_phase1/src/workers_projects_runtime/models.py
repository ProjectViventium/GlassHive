from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .runtime_identity import derive_legacy_backend_label

ProjectStatus = Literal["active", "paused", "completed", "archived", "failed"]
WorkerState = Literal[
    "created",
    "starting",
    "ready",
    "running",
    "stopping",
    "paused",
    "needs_input",
    "failed",
    "terminated",
]
RunState = Literal[
    "queued",
    "running",
    "settling",
    "interrupted",
    "paused",
    "needs_input",
    "completed",
    "failed",
    "cancelled",
]
ScheduleState = Literal[
    "pending",
    "running",
    "queued",
    "needs_input",
    "completed",
    "failed",
    "cancelled",
]
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
    profile: str = Field(
        default="",
        description="Worker profile selector. Empty means use the project/deployment default.",
    )
    backend: str = Field(
        default="",
        description="Deprecated compatibility field. Runtime is derived from profile and execution_mode.",
    )
    execution_mode: str = Field(
        default="",
        description="Execution mode, host or docker. Empty means use the deployment default.",
    )
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
    runtime: str = ""
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
    current_run_id: str | None = None
    last_error: str | None = None
    created_at: str
    updated_at: str

    @model_validator(mode="before")
    @classmethod
    def derive_legacy_backend_from_profile(cls, data):
        if not isinstance(data, dict):
            return data
        backend = derive_legacy_backend_label(
            profile=data.get("profile"),
            runtime=data.get("runtime"),
            backend=data.get("backend"),
        )
        if backend:
            data = dict(data)
            data["backend"] = backend
        if not data.get("current_run_id") and data.get("state") in {"running", "paused"}:
            data = dict(data)
            data["current_run_id"] = data.get("last_run_id") or None
        return data


class AssignRunRequest(BaseModel):
    instruction: str = Field(min_length=1)
    effort: str | None = None
    bootstrap_bundle: dict[str, object] | None = None


class SendMessageRequest(BaseModel):
    message: str = Field(min_length=1)


class CreateDelegationRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    goal: str = Field(min_length=1, max_length=10000)
    instruction: str = Field(min_length=1, max_length=100000)
    profile: str = Field(default="", max_length=100)
    execution_mode: str = Field(default="", alias="executionMode", max_length=20)
    worker_name: str = Field(default="", alias="workerName", max_length=200)
    worker_role: str = Field(default="", alias="workerRole", max_length=500)
    workspace_root: str | None = Field(default=None, alias="workspaceRoot", max_length=4096)
    bootstrap_profile: str | None = Field(default=None, alias="bootstrapProfile", max_length=200)
    bootstrap_bundle: dict[str, object] | None = Field(default=None, alias="bootstrapBundle")
    origin_ref: str | None = Field(
        default=None,
        alias="originRef",
        min_length=8,
        max_length=192,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@-]+$",
    )
    origin_surface: Literal["web", "telegram", "voice", "workbench", "scheduler"] = Field(
        default="web",
        alias="originSurface",
    )


class CallbackAssociationVerifyRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    origin_ref: str = Field(alias="originRef", min_length=8, max_length=192)
    work_ref: str = Field(alias="workRef", min_length=8, max_length=192)
    worker_id: str = Field(alias="workerId", min_length=8, max_length=192)
    run_id: str = Field(alias="runId", min_length=8, max_length=192)


class CapabilityReauthorizationRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    version: Literal[1]
    authorization_ref: str = Field(
        alias="authorizationRef",
        min_length=8,
        max_length=192,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@-]+$",
    )
    max_expires_at: str = Field(alias="maxExpiresAt", min_length=20, max_length=64)
    scope_fingerprint: str = Field(
        alias="scopeFingerprint", min_length=8, max_length=256
    )


class ActiveWorkActionRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    action: Literal[
        "queue",
        "message",
        "steer",
        "pause",
        "resume",
        "stop",
        "retry",
        "dismiss",
    ]
    instruction: str | None = Field(default=None, max_length=100000)
    capability_reauthorization: CapabilityReauthorizationRequest | None = Field(
        default=None, alias="capabilityReauthorization"
    )
    idempotency_key: str = Field(
        alias="idempotencyKey",
        min_length=8,
        max_length=192,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@-]+$",
    )


class RunActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    capabilityId: str = Field(min_length=5, max_length=192, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@-]+$")
    action: Literal["retry", "cancel"]
    projectId: str = Field(min_length=1, max_length=192, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@-]*$")
    workerId: str = Field(min_length=1, max_length=192, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@-]*$")
    runId: str = Field(min_length=1, max_length=192, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@-]*$")
    idempotencyKey: str = Field(min_length=8, max_length=192, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@-]+$")


class RunActionCorrelation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    projectId: str
    workerId: str
    runId: str


class RunActionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    status: Literal["queued", "pending", "accepted"]
    action: Literal["retry", "cancel"]
    projectId: str
    workerId: str
    sourceRunId: str
    newRun: RunActionCorrelation | None
    confirmationPending: bool
    idempotentReplay: bool


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
    native_session_id: str = ""
    native_capabilities_json: str = "{}"
    native_child_summary_json: str = "{}"


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
    payload_json: str = "{}"
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
