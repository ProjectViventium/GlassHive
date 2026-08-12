from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .recurrence import canonical_recurrence_owner
from .runtime_identity import derive_legacy_backend_label

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
WorkerCloseState = Literal["terminating", "termination_failed", "terminated"]
RunState = Literal["queued", "running", "interrupted", "paused", "completed", "failed", "cancelled"]
ScheduleState = Literal["pending", "running", "queued", "completed", "failed", "cancelled"]
RecurringScheduleOccurrenceState = Literal[
    "pending",
    "claimed",
    "running",
    "queued",
    "completed",
    "failed",
    "cancelled",
    "skipped",
    "retryable",
    "action_required",
]
RecurrenceType = Literal["once", "daily", "interval", "cron", "rfc5545"]
RecurrenceDstPolicy = Literal["next_valid_earliest", "next_valid_latest"]
RecurrenceOverlapPolicy = Literal["skip", "queue"]
RecurrenceCatchUpPolicy = Literal["skip", "bounded", "coalesce"]
DesktopActionName = Literal["terminal", "files", "browser", "focus_browser", "codex", "claude", "openclaw"]
ExecutionMode = Literal["docker", "host"]
WorkspaceKind = Literal["named", "ephemeral", "legacy"]
WORKSPACE_KINDS = {"named", "ephemeral", "legacy"}


def normalize_workspace_kind(value: object) -> WorkspaceKind:
    normalized = str(value or "legacy").strip().lower()
    if normalized not in WORKSPACE_KINDS:
        raise ValueError("workspace kind must be named, ephemeral, or legacy")
    return cast(WorkspaceKind, normalized)


def normalize_workspace_tags(values: list[str] | tuple[str, ...] | None) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        tag = str(value or "").strip().casefold()
        if not tag or tag in seen:
            continue
        if len(tag) > 64:
            raise ValueError("workspace tags must be 64 characters or fewer")
        seen.add(tag)
        normalized.append(tag)
    if len(normalized) > 32:
        raise ValueError("a workspace can have at most 32 tags")
    return normalized


class WorkspaceDuplicateReport(BaseModel):
    model_config = ConfigDict(extra="allow")

    source_state: Literal["pending", "copied", "empty", "filtered", "missing", "template"]
    copied_files: int = Field(ge=0)
    skipped_items: int = Field(ge=0)


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
    workspace_kind: WorkspaceKind = "legacy"
    tags: list[str] = Field(default_factory=list)


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
    close_state: WorkerCloseState | None = None
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
    workspace_kind: WorkspaceKind = "legacy"
    tags: list[str] = Field(default_factory=list)
    last_activity_at: str = ""
    duplication_report: WorkspaceDuplicateReport | None = None
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
        raw_state = str(data.get("state") or "")
        if raw_state in {"terminating", "termination_failed"}:
            data = dict(data)
            data["close_state"] = raw_state
            # Keep the frozen public state enum compatible while the optional close-state field
            # carries truthful close progress for modern clients.
            data["state"] = "terminated"
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


class CreateRecurringScheduleRequest(BaseModel):
    instruction: str = Field(min_length=1)
    recurrence_type: RecurrenceType
    interval_seconds: int | None = None
    local_time: str = ""
    timezone_name: str = "UTC"
    dst_policy: RecurrenceDstPolicy = "next_valid_earliest"
    first_run_at: str | None = None
    cron_expression: str = ""
    rrule: str = ""
    starts_at: str | None = None
    ends_at: str | None = None
    enabled: bool = True
    overlap_policy: RecurrenceOverlapPolicy = "skip"
    misfire_grace_seconds: int = Field(default=300, ge=0, le=604800)
    catch_up_policy: RecurrenceCatchUpPolicy = "skip"
    max_catch_up_occurrences: int = Field(default=1, ge=1, le=10)
    jitter_seconds: int = Field(default=0, ge=0, le=900)
    schedule_text: str = ""
    bootstrap_bundle: dict[str, object] | None = None


class UpdateRecurringScheduleRequest(BaseModel):
    instruction: str | None = Field(default=None, min_length=1)
    recurrence_type: RecurrenceType | None = None
    interval_seconds: int | None = None
    local_time: str | None = None
    timezone_name: str | None = None
    dst_policy: RecurrenceDstPolicy | None = None
    cron_expression: str | None = None
    rrule: str | None = None
    starts_at: str | None = None
    ends_at: str | None = None
    enabled: bool | None = None
    overlap_policy: RecurrenceOverlapPolicy | None = None
    misfire_grace_seconds: int | None = Field(default=None, ge=0, le=604800)
    catch_up_policy: RecurrenceCatchUpPolicy | None = None
    max_catch_up_occurrences: int | None = Field(default=None, ge=1, le=10)
    jitter_seconds: int | None = Field(default=None, ge=0, le=900)
    schedule_text: str | None = None


class RunRecurringScheduleNowRequest(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=128)


class SchedulingCortexWorkspaceRunRequest(BaseModel):
    occurrence_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{8,200}$")
    task_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,200}$")
    tenant_id: str = Field(pattern=r"^[A-Za-z0-9_.:@-]{1,200}$")
    owner_id: str = Field(pattern=r"^[^\x00-\x1f\x7f]{1,512}$")
    project_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,200}$")
    worker_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,200}$")
    execution_mode: Literal["host", "docker"]
    instruction: str = Field(min_length=1, max_length=200_000)
    # Delegated scheduling authority is assertion-bound. Credential-bearing bootstrap
    # bundles are minted inside the runtime immediately before execution and never cross
    # or persist at the queue boundary.
    bootstrap_bundle: None = None


class SchedulePrincipalAuthorityRequest(BaseModel):
    enabled: bool


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
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    total_tokens: int = 0
    effort: str = Field(
        default="",
        description="Normalized per-assignment effort accepted by the runtime.",
    )

    @model_validator(mode="after")
    def calculate_total_tokens(self):
        self.total_tokens = sum(
            max(0, int(value))
            for value in (
                self.input_tokens,
                self.output_tokens,
                self.cache_read_input_tokens,
                self.cache_creation_input_tokens,
            )
        )
        return self


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


class RecurringScheduleDefinitionResponse(BaseModel):
    definition_id: str
    project_id: str
    worker_id: str
    workspace_name: str = ""
    tenant_id: str = "local"
    owner_id: str
    scheduler_owner: str
    schedule_owner: str = ""
    owner_action: str = ""
    instruction: str
    schedule_text: str = ""
    recurrence_type: RecurrenceType
    interval_seconds: int | None = None
    local_time: str = ""
    timezone_name: str
    dst_policy: str
    cron_expression: str = ""
    rrule: str = ""
    starts_at: str | None = None
    ends_at: str | None = None
    enabled: bool = True
    overlap_policy: str = "skip"
    misfire_grace_seconds: int = 300
    catch_up_policy: str = "coalesce"
    max_catch_up_occurrences: int = 1
    jitter_seconds: int = 0
    next_run_at: str
    next_occurrence_at: str = ""
    last_occurrence_at: str | None = None
    last_outcome: str = ""
    last_error: str = ""
    last_delivery_outcome: str | None = None
    last_delivery_reason: str | None = None
    last_delivery_at: str | None = None
    retired_at: str | None = None
    active: bool
    created_at: str
    updated_at: str

    @model_validator(mode="before")
    @classmethod
    def canonicalize_scheduler_owner(cls, value):
        if isinstance(value, dict) and value.get("scheduler_owner"):
            value = dict(value)
            owner = canonical_recurrence_owner(value["scheduler_owner"])
            value["scheduler_owner"] = owner
            value["schedule_owner"] = owner
            value["owner_action"] = (
                "dispatch_here" if owner == "glasshive_native" else "dispatch_via_viventium_cortex"
            )
            value["enabled"] = bool(value.get("enabled", value.get("active", True)))
            value["next_occurrence_at"] = str(value.get("next_run_at") or "")
        return value


class RecurringScheduleOccurrenceResponse(BaseModel):
    occurrence_id: str
    definition_id: str
    tenant_id: str = "local"
    owner_id: str
    scheduled_for: str
    detected_at: str
    scheduled_run_id: str
    idempotency_key: str = ""
    claimant: str = ""
    claimed_at: str | None = None
    claim_expires_at: str | None = None
    attempt_count: int = 0
    outcome: str = "pending"
    terminal_at: str | None = None
    created_at: str
    state: RecurringScheduleOccurrenceState
    queued_run_id: str | None = None
    last_error: str = ""


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
