from __future__ import annotations

import json
import fcntl
import hashlib
import hmac
import logging
import os
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Condition, Event, Lock, Thread
from urllib.parse import quote, urlencode, urlparse

import httpx

from .broker_admission import (
    BrokerAdmissionError,
    admit_capability_grant,
    revoke_capability_grant,
)
from .deliverables import (
    PROFESSIONAL_ARTIFACT_EXTENSIONS,
    SUPPORT_ARTIFACT_DIR_NAMES,
    deliverable_payload,
    is_user_deliverable_relative_path,
    is_valid_professional_artifact,
)
from .failure_classification import classify_runtime_error
from .models import utc_now
from .native_team import NativeTeamProjection
from .openclaw_runtime import (
    HostCapacityError,
    ProviderRateLimitError,
    RuntimeErrorBase,
    RuntimeInfo,
    RunStartupRejectedError,
    WorkerInterruptedError,
    WorkerPausedError,
    WorkerRuntime,
    WorkerTerminatedError,
)
from .operator_urls import surface_aware_watch_url
from .runtime_env import load_viventium_runtime_env
from .runtime_identity import derive_legacy_backend_label
from .run_actions import (
    RunActionError,
    mint_run_action_capability,
    unverified_run_action_claims,
    verify_run_action_capability,
)
from .run_evidence import FINAL_REPORT_PATTERN
from .run_states import TERMINAL_RUN_STATES
from .signed_links import (
    append_signed_query,
    create_signed_link_ref,
    is_worker_signed_link_revoked,
    revoke_signed_link_refs_for_worker,
    sign_link_params,
    signed_link_ref_url,
    sign_link_token,
)
from .store import (
    DelegationIdempotencyConflictError,
    HostRunLeaseCapacityError,
    IsolatedParallelAdmissionConflictError,
    STEER_REPLACEMENT_SUPPRESSED_ERROR,
    Store,
)
from .workspace_continuation import continuation_instruction


logger = logging.getLogger(__name__)
TERMINAL_CALLBACK_MESSAGE_LIMIT = 4000
VIVENTIUM_CALLBACK_PATH = "/api/viventium/glasshive/callback"
SCHEDULING_CORTEX_CALLBACK_PATH = "/internal/scheduled-prompts/glasshive-callback"
ACTIONABLE_CALLBACK_LINK_EVENTS = {
    "run.failed",
    "run.paused",
    "run.needs_input",
    "run.interrupted",
    "run.cancelled",
}
PARENT_VISIBLE_CALLBACK_FIELDS = ("user_id", "conversation_id", "parent_message_id", "message_id")
CALLBACK_DEAD_LETTER_IMMEDIATE_STATUS_CODES = {400, 401, 403, 404, 409, 410, 422, 501}
CALLBACK_RETRYABLE_STATUS_CODES = {408, 425, 429}
RUN_STATE_BY_EVENT = {
    "run.queued": "queued",
    "run.requeued": "queued",
    "run.waiting_on_capacity": "queued",
    "run.started": "running",
    "run.stopping": "stopping",
    "run.completed": "completed",
    "run.failed": "failed",
    "run.paused": "paused",
    "run.needs_input": "needs_input",
    # `interrupted` is an internal per-run outcome.  Public callback state
    # remains within the canonical completed/failed/cancelled terminal set.
    "run.interrupted": "cancelled",
    "worker.interrupted": "cancelled",
    "run.cancelled": "cancelled",
}
_UNSET = object()


class _WorkerLifecycleGuard:
    """One idempotently releasable cross-process worker lifecycle flock."""

    def __init__(self, handle) -> None:
        self._handle = handle
        self._released = False
        self._release_lock = Lock()

    def release(self) -> None:
        with self._release_lock:
            if self._released:
                return
            self._released = True
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()


class ParallelExecutionIsolationError(RuntimeError):
    """Automatic conversation-orchestrated missions may not enter the host lane."""

    def __init__(self, message: str, *, reason_code: str = "") -> None:
        super().__init__(message)
        self.reason_code = str(reason_code or "").strip()


PARALLEL_CLEAN_ROOM_EXECUTION_POLICY = "parallel-clean-room-v1"
PARALLEL_CLEAN_ROOM_BOOTSTRAP_PROFILE = "clean-room"
PARALLEL_CLEAN_ROOM_BROKER_NAME = "glasshive-user-capabilities"
PARALLEL_CLEAN_ROOM_BROKER_TOKEN_ENV = "GLASSHIVE_CAPABILITY_BROKER_TOKEN"
PARALLEL_CLEAN_ROOM_FORBIDDEN_BUNDLE_KEYS = {
    "anthropicapikey",
    "apikey",
    "authtoken",
    "bearertoken",
    "claudecodeoauthtoken",
    "claudesettingslocal",
    "credential",
    "credentials",
    "credentialspath",
    "openaiapikey",
    "providerapikey",
    "providerauth",
    "providercredentials",
    "providerenv",
    "providerheaders",
    "providertoken",
    "providertokens",
}

PARALLEL_CLEAN_ROOM_REJECTION_CODES = {
    "host bootstrap profiles are not allowed": "host_profile",
    "bootstrap bundle is invalid": "bundle_invalid",
    "execution policy is server-owned": "caller_execution_policy",
    "caller provider credentials are not allowed": "caller_provider_credentials",
    "caller bootstrap environment is not allowed": "caller_environment",
    "files must be a workspace-scoped list": "files_not_workspace_list",
    "every file must be workspace-scoped": "file_not_workspace_scoped",
    "home-scoped files are not allowed": "home_scoped_file",
    "workspace file path is invalid": "workspace_path_invalid",
    "workspace provider or credential config files are not allowed": "workspace_authority_file",
    "capability broker metadata is invalid": "broker_metadata_invalid",
    "caller broker credentials are not allowed": "caller_broker_credentials",
    "caller MCP config is not allowed": "caller_mcp_config",
    "caller Claude MCP config is not allowed": "caller_claude_mcp_config",
    "caller Codex MCP config is not allowed": "caller_codex_mcp_config",
}


def _parallel_clean_room_rejected(reason: str) -> ParallelExecutionIsolationError:
    return ParallelExecutionIsolationError(
        f"Automatic Parallel work rejected unsafe bootstrap authority: {reason}.",
        reason_code=PARALLEL_CLEAN_ROOM_REJECTION_CODES.get(reason, "bundle_invalid"),
    )


def _canonical_authority_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _validate_parallel_clean_room_files(bundle: dict) -> None:
    raw_files = bundle.get("files")
    if raw_files is None:
        return
    if not isinstance(raw_files, list):
        raise _parallel_clean_room_rejected("files must be a workspace-scoped list")
    for entry in raw_files:
        if not isinstance(entry, dict):
            raise _parallel_clean_room_rejected("every file must be workspace-scoped")
        scope = str(entry.get("scope") or "workspace").strip().lower()
        if scope != "workspace":
            raise _parallel_clean_room_rejected("home-scoped files are not allowed")
        raw_path = str(entry.get("path") or "").strip()
        if not raw_path:
            filename = str(entry.get("filename") or entry.get("file_id") or "").strip()
            raw_path = f"uploads/{filename}" if filename else ""
        relative = Path(raw_path.lstrip("/"))
        if not raw_path or relative.is_absolute() or ".." in relative.parts:
            raise _parallel_clean_room_rejected("workspace file path is invalid")
        normalized_path = relative.as_posix().lower()
        first_part = relative.parts[0].lower() if relative.parts else ""
        if (
            first_part in {".claude", ".codex", ".glasshive", ".git", ".ssh"}
            or normalized_path
            in {
                ".mcp.json",
                ".netrc",
                ".npmrc",
                ".pypirc",
                ".gitconfig",
                ".git-credentials",
                ".config/gh/hosts.yml",
                ".config/glab-cli/config.yml",
            }
            or normalized_path == ".env"
            or normalized_path.startswith(".env.")
        ):
            raise _parallel_clean_room_rejected(
                "workspace provider or credential config files are not allowed"
            )


def _contains_parallel_forbidden_authority_key(value: object) -> bool:
    if isinstance(value, dict):
        for key, nested in value.items():
            if _canonical_authority_key(key) in PARALLEL_CLEAN_ROOM_FORBIDDEN_BUNDLE_KEYS:
                return True
            if _contains_parallel_forbidden_authority_key(nested):
                return True
    elif isinstance(value, list):
        return any(_contains_parallel_forbidden_authority_key(item) for item in value)
    return False


def _parallel_clean_room_broker_url(bundle: dict) -> str:
    broker = bundle.get("glasshive_capability_broker")
    if broker is None:
        return ""
    if not isinstance(broker, dict):
        raise _parallel_clean_room_rejected("capability broker metadata is invalid")
    broker_url = str(broker.get("url") or "").strip()
    parsed = urlparse(broker_url)
    if (
        str(broker.get("name") or "").strip() != PARALLEL_CLEAN_ROOM_BROKER_NAME
        or broker.get("version") != 1
        or isinstance(broker.get("version"), bool)
        or str(broker.get("status") or "").strip() != "pending_admission"
        or parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise _parallel_clean_room_rejected("capability broker metadata is invalid")
    forbidden_broker_keys = {
        "authorization",
        "bearer",
        "bearertoken",
        "grant",
        "granttoken",
        "password",
        "secret",
        "token",
    }
    if any(
        _canonical_authority_key(key) in forbidden_broker_keys
        for key in broker
    ):
        raise _parallel_clean_room_rejected("caller broker credentials are not allowed")
    return broker_url


def _validate_parallel_clean_room_mcp(bundle: dict) -> None:
    project_mcp = bundle.get("claude_project_mcp")
    codex_config = bundle.get("codex_config_append")
    if project_mcp is None and codex_config is None:
        return
    broker_url = _parallel_clean_room_broker_url(bundle)
    if not broker_url:
        raise _parallel_clean_room_rejected("caller MCP config is not allowed")
    expected_server = {
        "type": "http",
        "transport": "http",
        "url": broker_url,
        "headers": {
            "Authorization": f"Bearer ${{{PARALLEL_CLEAN_ROOM_BROKER_TOKEN_ENV}}}"
        },
    }
    if project_mcp is not None and project_mcp != {
        PARALLEL_CLEAN_ROOM_BROKER_NAME: expected_server
    }:
        raise _parallel_clean_room_rejected("caller Claude MCP config is not allowed")
    expected_codex_config = "\n".join(
        (
            f"[mcp_servers.{PARALLEL_CLEAN_ROOM_BROKER_NAME}]",
            f"url = {json.dumps(broker_url, ensure_ascii=False)}",
            f"bearer_token_env_var = {json.dumps(PARALLEL_CLEAN_ROOM_BROKER_TOKEN_ENV)}",
        )
    )
    if codex_config is not None and (
        not isinstance(codex_config, str)
        or codex_config.strip() != expected_codex_config
    ):
        raise _parallel_clean_room_rejected("caller Codex MCP config is not allowed")


def derive_parallel_clean_room_bootstrap(
    bootstrap_profile: str | None,
    bootstrap_bundle: dict | None,
) -> tuple[str, dict]:
    """Validate Core's automatic launch envelope and add immutable host policy."""

    requested_profile = str(bootstrap_profile or "").strip()
    if requested_profile and requested_profile != PARALLEL_CLEAN_ROOM_BOOTSTRAP_PROFILE:
        raise _parallel_clean_room_rejected("host bootstrap profiles are not allowed")
    if not isinstance(bootstrap_bundle, dict):
        raise _parallel_clean_room_rejected("bootstrap bundle is invalid")
    if "execution_policy" in bootstrap_bundle:
        raise _parallel_clean_room_rejected("execution policy is server-owned")
    if _contains_parallel_forbidden_authority_key(bootstrap_bundle):
        raise _parallel_clean_room_rejected("caller provider credentials are not allowed")
    env = bootstrap_bundle.get("env")
    if env is not None and (not isinstance(env, dict) or bool(env)):
        raise _parallel_clean_room_rejected("caller bootstrap environment is not allowed")
    _validate_parallel_clean_room_files(bootstrap_bundle)
    if bootstrap_bundle.get("glasshive_capability_broker") is not None:
        _parallel_clean_room_broker_url(bootstrap_bundle)
    _validate_parallel_clean_room_mcp(bootstrap_bundle)
    return (
        PARALLEL_CLEAN_ROOM_BOOTSTRAP_PROFILE,
        {
            **bootstrap_bundle,
            "execution_policy": PARALLEL_CLEAN_ROOM_EXECUTION_POLICY,
        },
    )


@dataclass(frozen=True)
class HostResourceUsage:
    child_processes: int
    threads: int
    available_memory_bytes: int
    available_disk_bytes: int = 2**63 - 1
    process_probe_ok: bool = True
    memory_probe_ok: bool = True
    disk_probe_ok: bool = True


def host_resource_usage(active_leases: list[dict]) -> HostResourceUsage:
    """Measure only leased Viventium process trees plus global memory headroom."""

    pids = {
        int(lease.get("pid") or 0)
        for lease in active_leases
        if int(lease.get("pid") or 0) > 0
    }
    descendants: set[int] = set()
    threads = 0
    process_probe_ok = True
    if pids:
        try:
            completed = subprocess.run(
                ["ps", "-axo", "pid=,ppid=,thcount="],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            completed = None
        rows: list[tuple[int, int, int]] = []
        if completed and completed.returncode == 0:
            for line in completed.stdout.splitlines():
                parts = line.split()
                if len(parts) != 3:
                    continue
                try:
                    rows.append(tuple(int(part) for part in parts))
                except ValueError:
                    continue
            descendants = set(pids)
            changed = True
            while changed:
                changed = False
                for pid, parent_pid, _thread_count in rows:
                    if parent_pid in descendants and pid not in descendants:
                        descendants.add(pid)
                        changed = True
            threads = sum(
                max(0, thread_count)
                for pid, _parent_pid, thread_count in rows
                if pid in descendants
            )
        else:
            process_probe_ok = False
    available_memory = 0
    memory_probe_ok = True
    try:
        completed = subprocess.run(
            ["sysctl", "-n", "hw.memsize"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )
        if completed.returncode != 0:
            raise ValueError("memory size probe failed")
        total_memory = int(completed.stdout.strip())
        vm_stat = subprocess.run(
            ["vm_stat"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )
        if vm_stat.returncode != 0:
            raise ValueError("memory headroom probe failed")
        page_match = re.search(r"page size of (\d+) bytes", vm_stat.stdout)
        page_size = int(page_match.group(1)) if page_match else 4096
        free_pages = 0
        for name in ("Pages free", "Pages inactive", "Pages speculative"):
            match = re.search(rf"^{re.escape(name)}:\s+([0-9.]+)\.", vm_stat.stdout, re.MULTILINE)
            if match:
                free_pages += int(match.group(1))
        available_memory = free_pages * page_size
        if not available_memory:
            available_memory = total_memory
    except (OSError, subprocess.TimeoutExpired, ValueError):
        memory_probe_ok = False
        available_memory = 0
    available_disk = 0
    disk_probe_ok = True
    try:
        disk_root = Path(
            os.environ.get("WPR_HOST_RUNTIME_DIR", "").strip()
            or os.environ.get("WPR_HOST_WORKSPACE_ROOT", "").strip()
            or os.getcwd()
        ).expanduser()
        while not disk_root.exists() and disk_root != disk_root.parent:
            disk_root = disk_root.parent
        available_disk = int(shutil.disk_usage(disk_root).free)
    except (OSError, ValueError):
        disk_probe_ok = False
        available_disk = 0
    return HostResourceUsage(
        child_processes=len(descendants),
        threads=threads,
        available_memory_bytes=available_memory,
        available_disk_bytes=available_disk,
        process_probe_ok=process_probe_ok,
        memory_probe_ok=memory_probe_ok,
        disk_probe_ok=disk_probe_ok,
    )


def _bounded_int_env(name: str, default: int, *, min_value: int, max_value: int) -> int:
    try:
        value = int(str(os.environ.get(name, "")).strip())
    except ValueError:
        return default
    return max(min_value, min(value, max_value))


def _bounded_float_env(name: str, default: float, *, min_value: float, max_value: float) -> float:
    try:
        value = float(str(os.environ.get(name, "")).strip())
    except ValueError:
        return default
    return max(min_value, min(value, max_value))


def _env_truthy(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _is_local_scheduling_cortex_callback_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return (
        parsed.path == SCHEDULING_CORTEX_CALLBACK_PATH
        and parsed.scheme in {"http", "https"}
        and host in {"localhost", "127.0.0.1", "::1"}
    )


def _is_callback_status_retryable(status_code: int, url: str) -> bool:
    if status_code in CALLBACK_RETRYABLE_STATUS_CODES:
        return True
    return status_code == 404 and _is_local_scheduling_cortex_callback_url(url)


def _is_callback_status_immediate_dead_letter(status_code: int, url: str) -> bool:
    if _is_callback_status_retryable(status_code, url):
        return False
    return status_code in CALLBACK_DEAD_LETTER_IMMEDIATE_STATUS_CODES


def _enterprise_mode_enabled() -> bool:
    return _env_truthy("GLASSHIVE_ENTERPRISE_MODE") or _env_truthy("WPR_ENTERPRISE_MODE")


def isolated_parallel_policy_enabled() -> bool:
    """Whether same-UID host missions are excluded while Parallel Main is available."""

    return _env_truthy("VIVENTIUM_GLASSHIVE_ISOLATED_PARALLEL_POLICY")


class HostWorkersDisabledError(RuntimeError):
    pass


class GlassHiveQuotaExceededError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        env_name: str = "",
        label: str = "",
        limit: int = 0,
        current_count: int = 0,
        available_workspace_options: list[dict] | None = None,
    ) -> None:
        super().__init__(message)
        self.env_name = env_name
        self.label = label
        self.limit = limit
        self.current_count = current_count
        self.available_workspace_options = available_workspace_options or []


class GlassHiveProfileNotAllowedError(RuntimeError):
    pass


def host_workers_enabled() -> bool:
    value = os.environ.get("GLASSHIVE_HOST_WORKERS_ENABLED", "true").strip().lower()
    return value not in {"0", "false", "no", "off", "disabled"}


def allowed_worker_profiles() -> set[str]:
    raw = (
        os.environ.get("GLASSHIVE_ALLOWED_WORKER_PROFILES", "").strip()
        or os.environ.get("WPR_ALLOWED_WORKER_PROFILES", "").strip()
    )
    if not raw:
        return set()
    return {item.strip() for item in raw.split(",") if item.strip()}


def terminal_callback_full_message(output_text: str, *, fallback: str = "Run completed") -> str:
    text = str(output_text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return fallback

    marker_matches = list(FINAL_REPORT_PATTERN.finditer(text))
    if marker_matches:
        text = text[marker_matches[-1].end() :].strip()
    return text or fallback


def terminal_callback_message(output_text: str, *, fallback: str = "Run completed") -> str:
    text = str(output_text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return fallback

    marker_matches = list(FINAL_REPORT_PATTERN.finditer(text))
    has_final_report = bool(marker_matches)
    if marker_matches:
        text = text[marker_matches[-1].end() :].strip()

    if len(text) <= TERMINAL_CALLBACK_MESSAGE_LIMIT:
        return text or fallback

    if has_final_report:
        return f"{text[: TERMINAL_CALLBACK_MESSAGE_LIMIT - 3].rstrip()}..."

    prefix = "...\n\n"
    paragraph_budget = TERMINAL_CALLBACK_MESSAGE_LIMIT - len(prefix)
    paragraphs = [paragraph.strip() for paragraph in text.split("\n\n") if paragraph.strip()]
    selected: list[str] = []
    current_len = 0
    for paragraph in reversed(paragraphs):
        next_len = current_len + len(paragraph) + (2 if selected else 0)
        if selected and next_len > paragraph_budget:
            break
        selected.insert(0, paragraph)
        current_len = next_len

    if selected:
        message = "\n\n".join(selected).strip()
        if len(message) <= paragraph_budget:
            return f"{prefix}{message}" if len(message) < len(text) else message

    tail_prefix = "..."
    tail = text[-(TERMINAL_CALLBACK_MESSAGE_LIMIT - len(tail_prefix)) :].lstrip()
    if " " in tail[:120]:
        tail = tail[tail.find(" ") + 1 :].lstrip()
    return f"{tail_prefix}{tail}" if tail else fallback


def public_callback_message_text(message: str) -> str:
    text = str(message or "")
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{12,}", r"\1[REDACTED]", text)
    text = re.sub(
        r"(?i)((?:api[_-]?key|token|secret|password|passwd|pwd)\s*[:=]\s*)[^\s\"']{6,}",
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "sk-[REDACTED]", text)
    text = re.sub(r"\b(?:wrk|run|prj)_[A-Za-z0-9_-]{6,}\b", "[glasshive-id]", text)
    text = re.sub(
        r"(?:~\/|\/Users\/|\/home\/|\/private\/var\/|\/var\/folders\/|[A-Za-z]:\\Users\\)[^\s`'\"<>]+",
        "[local path]",
        text,
    )
    return text.strip()


def runtime_failure_callback_message(failure_fields: dict[str, object], fallback: str) -> str:
    message = str(failure_fields.get("failure_user_message") or "").strip() or str(fallback or "Run failed")
    diagnostic = str(failure_fields.get("failure_diagnostic_summary") or "").strip()
    if diagnostic and diagnostic not in message:
        return f"{message}\n\nDetails: {diagnostic}"
    return message


def _is_viventium_callback_url(url: str) -> bool:
    return VIVENTIUM_CALLBACK_PATH in str(url or "")


def _missing_parent_callback_fields(callbacks: dict[str, object]) -> list[str]:
    return [key for key in PARENT_VISIBLE_CALLBACK_FIELDS if not str(callbacks.get(key) or "").strip()]


def callback_run_state(event_type: str, run: dict | None) -> object:
    return RUN_STATE_BY_EVENT.get(str(event_type or ""), (run or {}).get("state"))


def _merge_file_entries(existing: object, incoming: object) -> object:
    if not isinstance(existing, list):
        return incoming
    if not isinstance(incoming, list):
        return existing
    merged: list[object] = []
    indexes_by_path: dict[str, int] = {}
    for item in existing:
        if isinstance(item, dict):
            path = str(item.get("path") or "").strip()
            if path:
                indexes_by_path[path] = len(merged)
        merged.append(item)
    for item in incoming:
        if isinstance(item, dict):
            path = str(item.get("path") or "").strip()
            if path and path in indexes_by_path:
                merged[indexes_by_path[path]] = item
                continue
            if path:
                indexes_by_path[path] = len(merged)
        merged.append(item)
    return merged


def merge_bootstrap_bundle(existing: dict | None, incoming: dict | None) -> dict | None:
    if incoming is None:
        return existing
    if existing is None:
        return dict(incoming)
    merged = dict(existing)
    if (
        "execution_policy" in existing
        and "execution_policy" in incoming
        and incoming["execution_policy"] != existing["execution_policy"]
    ):
        raise ParallelExecutionIsolationError(
            "The server-owned worker execution policy is immutable."
        )
    for key, value in incoming.items():
        current = merged.get(key)
        if key == "files":
            merged[key] = _merge_file_entries(current, value)
        elif isinstance(current, dict) and isinstance(value, dict):
            merged[key] = merge_bootstrap_bundle(current, value) or {}
        else:
            merged[key] = value
    return merged


class WorkersProjectsService:
    def __init__(
        self,
        store: Store,
        runtime: WorkerRuntime,
        max_workers: int = 8,
        reconcile_on_startup: bool | None = None,
    ) -> None:
        self.store = store
        self.runtime = runtime
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="wpr-runner")
        # Interactive provider turns have a separate dispatch lane so autonomous mission workers
        # cannot occupy every service thread before a conversation reaches the host CLI's own
        # profile-isolated capacity lane.
        conversation_workers = max(
            1,
            min(
                max_workers,
                int(os.environ.get("GLASSHIVE_CONVERSATION_EXECUTOR_WORKERS", "2") or "2"),
            ),
        )
        self.conversation_executor = ThreadPoolExecutor(
            max_workers=conversation_workers,
            thread_name_prefix="wpr-conversation",
        )
        self._shutdown_event = Event()
        self._scheduler_wake_event = Event()
        self._processors_lock = Lock()
        self._active_processors: set[str] = set()
        self._processor_generations: dict[str, int] = {}
        self._worker_create_lock = Lock()
        self._deliverable_promotions_lock = Lock()
        self._deliverable_promotions: set[str] = set()
        self._pending_run_starts_lock = Lock()
        self._pending_run_starts: dict[str, dict[str, object]] = {}
        self._run_local_bundles_lock = Condition()
        self._run_local_bundles: dict[str, dict] = {}
        self._run_local_grant_waiters: set[str] = set()
        self._executor_id = f"executor-{os.getpid()}-{uuid.uuid4().hex}"
        observer_setter = getattr(self.runtime, "set_host_process_observer", None)
        if callable(observer_setter):
            observer_setter(self._observe_host_process)
        start_observer_setter = getattr(
            self.runtime, "set_run_start_observer", None
        )
        self._run_start_observer_supported = callable(start_observer_setter)
        if callable(start_observer_setter):
            start_observer_setter(self._observe_run_start)
        native_observer_setter = getattr(self.runtime, "set_native_event_observer", None)
        if callable(native_observer_setter):
            native_observer_setter(self._observe_native_event)
        if reconcile_on_startup is None:
            reconcile_on_startup = not _enterprise_mode_enabled()
        if reconcile_on_startup:
            mission_network_repair = getattr(
                self.runtime,
                "repair_parallel_clean_room_mission_networks",
                None,
            )
            if callable(mission_network_repair):
                try:
                    mission_network_repair()
                except Exception:
                    # Every clean-room run still performs the same strict
                    # repair before authority projection.  Keep control/read
                    # surfaces available while automatic admission fails
                    # closed on the unrepaired boundary.
                    logger.warning(
                        "Failed to repair Parallel clean-room mission networks",
                        exc_info=True,
                    )
            self.reconcile_host_run_leases()
            self.reconcile_all_workers()
        # Capture the durable backlog boundary synchronously, then recover it
        # in one ordered background job. Rows created after construction own
        # their direct/recurring delivery path and must not race startup replay.
        self._startup_recovery_cutoff = utc_now()
        self._startup_recovery_thread = Thread(
            target=self._replay_startup_recovery,
            name="wpr-startup-recovery",
            daemon=True,
        )
        self._startup_recovery_thread.start()
        self._callback_retry_thread = Thread(
            target=self._callback_retry_loop,
            name="wpr-callback-retry",
            daemon=True,
        )
        self._callback_retry_thread.start()
        self._idle_reaper_thread: Thread | None = None
        if self._lifecycle_reaper_enabled():
            self._idle_reaper_thread = Thread(
                target=self._idle_reaper_loop,
                name="wpr-idle-reaper",
                daemon=True,
            )
            self._idle_reaper_thread.start()
        self._scheduler_thread = Thread(
            target=self._scheduler_loop,
            name="wpr-scheduler",
            daemon=True,
        )
        self._scheduler_thread.start()
        self._host_lease_heartbeat_thread = Thread(
            target=self._host_lease_heartbeat_loop,
            name="wpr-host-lease-heartbeat",
            daemon=True,
        )
        self._host_lease_heartbeat_thread.start()
        self._isolated_readiness_thread: Thread | None = None
        if callable(
            getattr(self.runtime, "refresh_isolated_parallel_readiness", None)
        ):
            self._isolated_readiness_thread = Thread(
                target=self._isolated_readiness_loop,
                name="wpr-isolated-readiness",
                daemon=True,
            )
            self._isolated_readiness_thread.start()
        if self.store.has_compute_release_claims():
            self.executor.submit(self.recover_expired_compute_release_claims_once)

    def shutdown(self) -> None:
        with self._processors_lock:
            self._shutdown_event.set()
        with self._run_local_bundles_lock:
            self._run_local_bundles.clear()
            self._run_local_grant_waiters.clear()
            self._run_local_bundles_lock.notify_all()
        self.store.release_active_work_action_leases(self._executor_id)
        self._scheduler_wake_event.set()
        for thread in (
            self._startup_recovery_thread,
            self._callback_retry_thread,
            self._idle_reaper_thread,
            self._scheduler_thread,
            self._host_lease_heartbeat_thread,
            self._isolated_readiness_thread,
        ):
            if thread and thread.is_alive():
                thread.join(timeout=2)
        self.executor.shutdown(wait=True, cancel_futures=False)
        self.conversation_executor.shutdown(wait=True, cancel_futures=False)

    @property
    def executor_id(self) -> str:
        """Stable owner for short-lived durable action execution leases."""

        return self._executor_id

    def _callback_config_for(self, worker: dict) -> dict:
        bundle = self._bootstrap_bundle_for(worker) or {}
        callbacks = bundle.get("callbacks")
        if not isinstance(callbacks, dict) or not callbacks:
            return {}
        resolved = dict(callbacks)
        load_viventium_runtime_env()
        callback_url = (
            os.environ.get("GLASSHIVE_EVENTS_WEBHOOK_URL", "").strip()
            or os.environ.get("VIVENTIUM_GLASSHIVE_CALLBACK_URL", "").strip()
        )
        callback_secret = (
            os.environ.get("GLASSHIVE_EVENTS_HMAC_SECRET", "").strip()
            or os.environ.get("VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", "").strip()
        )
        recovered: list[str] = []
        if callback_url and not (resolved.get("events_webhook_url") or resolved.get("url")):
            resolved["events_webhook_url"] = callback_url
            recovered.append("endpoint")
        if callback_secret and not (resolved.get("hmac_secret") or resolved.get("secret")):
            resolved["hmac_secret"] = callback_secret
            recovered.append("secret")
        if recovered:
            logger.warning(
                "Recovered GlassHive callback %s from canonical runtime env for worker %s; "
                "check MCP/bootstrap request-context propagation.",
                ", ".join(recovered),
                worker.get("worker_id"),
            )
        return resolved

    def _viventium_callback_context_ready(
        self,
        worker: dict,
        callbacks: dict[str, object],
    ) -> bool:
        """Accept either legacy parent identity or Core's opaque durable origin binding."""

        if not _missing_parent_callback_fields(callbacks):
            return True
        origin_ref = str(callbacks.get("origin_ref") or "").strip()
        if not origin_ref:
            return False
        delegation = self.store.get_delegation_for_worker(
            str(worker.get("worker_id") or ""),
            tenant_id=str(worker.get("tenant_id") or "local"),
            owner_id=str(worker.get("owner_id") or ""),
        )
        return bool(
            delegation
            and str(delegation.get("origin_ref") or "").strip() == origin_ref
            and str(delegation.get("work_ref") or "").strip()
        )

    def _derive_callback_secret(self, secret: str, worker_id: str, run_id: str | None) -> bytes:
        binding = f"{worker_id}:{run_id or ''}".encode("utf-8")
        return hmac.new(secret.encode("utf-8"), binding, hashlib.sha256).hexdigest().encode("utf-8")

    def _encode_callback_payload(self, payload: dict) -> bytes:
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

    def _callback_headers(self, callbacks: dict, payload: dict, encoded: bytes) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        secret = str(callbacks.get("hmac_secret") or callbacks.get("secret") or "")
        if secret:
            derived_secret = self._derive_callback_secret(
                secret,
                str(payload.get("worker_id") or ""),
                str(payload.get("run_id") or "") or None,
            )
            headers["X-GlassHive-Signature"] = "sha256=" + hmac.new(derived_secret, encoded, hashlib.sha256).hexdigest()
        return headers

    def _callback_action_capabilities(
        self,
        worker: dict,
        payload: dict,
        callbacks: dict,
    ) -> list[dict[str, object]]:
        event_type = str(payload.get("event") or "")
        run_id = str(payload.get("run_id") or "")
        if event_type not in {"run.started", "run.failed"} or not run_id:
            return []
        run = self.store.get_run(run_id)
        if not run:
            return []
        action = ""
        if event_type == "run.started" and str(run.get("state") or "") == "running":
            action = "cancel"
        elif (
            event_type == "run.failed"
            and str(run.get("state") or "") == "failed"
            and bool(run.get("failure_retryable"))
        ):
            action = "retry"
        if not action:
            return []
        secret = str(callbacks.get("hmac_secret") or callbacks.get("secret") or "")
        if not secret:
            return []
        try:
            return [mint_run_action_capability(secret, worker=worker, run=run, action=action)]
        except RunActionError:
            logger.warning(
                "GlassHive action capability was not minted",
                extra={
                    "worker_id": str(worker.get("worker_id") or ""),
                    "run_id": run_id,
                    "event_type": event_type,
                },
            )
            return []

    def _callback_max_total_attempts(self) -> int:
        return _bounded_int_env("GLASSHIVE_CALLBACK_MAX_TOTAL_ATTEMPTS", 25, min_value=1, max_value=1000)

    def _dead_letter_callback(
        self,
        worker: dict,
        record: dict,
        *,
        callback_id: str,
        attempts: int,
        payload_json: str,
        reason: str,
    ) -> None:
        self.store.mark_callback_dead_lettered(
            callback_id,
            attempts=attempts,
            payload_json=payload_json,
            last_error=reason,
        )
        try:
            self.store.add_event(
                str(record.get("project_id") or worker.get("project_id") or ""),
                str(record.get("worker_id") or worker.get("worker_id") or ""),
                record.get("run_id"),
                "callback.dead_lettered",
                f"{record.get('event_type')}: {reason}",
            )
        except Exception:
            pass

    def _finish_failed_callback_delivery(
        self,
        worker: dict,
        record: dict,
        *,
        callback_id: str,
        attempts: int,
        payload_json: str,
        reason: str,
    ) -> None:
        stored_attempts = int(record.get("attempts") or 0)
        total_attempts = stored_attempts + attempts
        max_total_attempts = self._callback_max_total_attempts()
        if total_attempts >= max_total_attempts:
            self._dead_letter_callback(
                worker,
                record,
                callback_id=callback_id,
                attempts=attempts,
                payload_json=payload_json,
                reason=f"callback retry budget exhausted after {total_attempts} attempts: {reason}",
            )
            return
        self.store.mark_callback_pending(
            callback_id,
            attempts=attempts,
            payload_json=payload_json,
            last_error=reason,
        )
        try:
            self.store.add_event(
                str(record.get("project_id") or worker.get("project_id") or ""),
                str(record.get("worker_id") or worker.get("worker_id") or ""),
                record.get("run_id"),
                "callback.failed",
                f"{record.get('event_type')}: {reason}",
            )
        except Exception:
            pass

    def _deliver_callback_record(self, worker: dict, record: dict, callbacks: dict) -> None:
        callback_id = str(record.get("callback_id") or "")
        if callback_id and not self.store.claim_pending_callback(callback_id):
            return
        stored_attempts = int(record.get("attempts") or 0)
        max_total_attempts = self._callback_max_total_attempts()
        if stored_attempts >= max_total_attempts:
            self._dead_letter_callback(
                worker,
                record,
                callback_id=callback_id,
                attempts=0,
                payload_json=str(record.get("payload_json") or "{}"),
                reason=f"callback retry budget exhausted before delivery after {stored_attempts} attempts",
            )
            return
        url = str(callbacks.get("events_webhook_url") or callbacks.get("url") or record.get("url") or "").strip()
        if not url:
            self._dead_letter_callback(
                worker,
                record,
                callback_id=callback_id,
                attempts=1,
                payload_json=str(record.get("payload_json") or "{}"),
                reason="missing callback url",
            )
            return
        try:
            payload = json.loads(str(record.get("payload_json") or "{}"))
        except json.JSONDecodeError:
            self._dead_letter_callback(
                worker,
                record,
                callback_id=str(record.get("callback_id") or ""),
                attempts=1,
                payload_json=str(record.get("payload_json") or "{}"),
                reason="invalid callback payload json",
            )
            return
        if not isinstance(payload, dict):
            payload = {}
        payload["callback_id"] = str(callback_id or payload.get("callback_id") or f"cb_{uuid.uuid4().hex}")
        stored_payload = dict(payload)
        action_capabilities = self._callback_action_capabilities(worker, payload, callbacks)
        if action_capabilities:
            payload["actionCapabilities"] = action_capabilities

        retry_attempts = _bounded_int_env("GLASSHIVE_CALLBACK_RETRY_ATTEMPTS", 3, min_value=1, max_value=25)
        retry_base_delay_s = _bounded_float_env(
            "GLASSHIVE_CALLBACK_RETRY_BASE_DELAY_S",
            0.5,
            min_value=0.0,
            max_value=60.0,
        )
        last_exc: Exception | None = None
        attempts = 0
        stored_payload_json = json.dumps(stored_payload, ensure_ascii=False)
        for attempt in range(retry_attempts):
            attempts += 1
            payload["callback_ts"] = int(time.time())
            stored_payload["callback_ts"] = payload["callback_ts"]
            stored_payload_json = json.dumps(stored_payload, ensure_ascii=False)
            encoded = self._encode_callback_payload(payload)
            headers = self._callback_headers(callbacks, payload, encoded)
            try:
                response = httpx.post(url, content=encoded, headers=headers, timeout=5.0)
                response.raise_for_status()
                self.store.mark_callback_http_accepted(
                    payload["callback_id"], attempts=attempts, payload_json=stored_payload_json
                )
                return
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                status_code = exc.response.status_code if exc.response is not None else 0
                if _is_callback_status_immediate_dead_letter(status_code, url):
                    self._dead_letter_callback(
                        worker,
                        record,
                        callback_id=payload["callback_id"],
                        attempts=attempts,
                        payload_json=stored_payload_json,
                        reason=f"callback endpoint returned terminal HTTP {status_code}",
                    )
                    return
                if 400 <= status_code < 500 and not _is_callback_status_retryable(status_code, url):
                    break
            except Exception as exc:
                last_exc = exc
            if attempt < retry_attempts - 1 and retry_base_delay_s > 0:
                time.sleep(retry_base_delay_s * (attempt + 1))
        self._finish_failed_callback_delivery(
            worker,
            record,
            callback_id=payload["callback_id"],
            attempts=attempts,
            payload_json=stored_payload_json,
            reason=str(last_exc or "callback delivery failed"),
        )

    def _replay_startup_recovery(self) -> None:
        if self._shutdown_event.is_set():
            return
        try:
            self.reap_needs_input_workers_once()
        except Exception:
            logger.error(
                "GlassHive startup needs-input compute recovery faulted safely",
                extra={"error_code": "transient_dependency"},
            )
        if self._shutdown_event.is_set():
            return
        try:
            self._replay_pending_capability_grant_revocations(
                created_before=self._startup_recovery_cutoff
            )
        except Exception:
            logger.error(
                "GlassHive startup capability revocation recovery faulted safely",
                extra={"error_code": "transient_dependency"},
            )
        if self._shutdown_event.is_set():
            return
        try:
            self._replay_pending_lifecycle_effects(
                created_before=self._startup_recovery_cutoff
            )
        except Exception:
            logger.error(
                "GlassHive startup lifecycle recovery faulted safely",
                extra={"error_code": "transient_dependency"},
            )
        if self._shutdown_event.is_set():
            return
        self._replay_pending_callbacks(
            created_before=self._startup_recovery_cutoff
        )

    def _replay_pending_callbacks(self, *, created_before: str | None = None) -> None:
        if self._shutdown_event.is_set():
            return
        stale_after_s = _bounded_int_env(
            "GLASSHIVE_CALLBACK_DELIVERING_STALE_AFTER_S",
            300,
            min_value=1,
            max_value=24 * 3600,
        )
        stale_before = (datetime.now(timezone.utc) - timedelta(seconds=stale_after_s)).isoformat()
        try:
            self.store.reclaim_stale_delivering_callbacks(stale_before=stale_before, limit=50)
        except Exception:
            pass
        try:
            pending = self.store.list_pending_callbacks(
                limit=50,
                created_before=created_before,
            )
        except Exception:
            return
        for record in pending:
            if self._shutdown_event.is_set():
                return
            worker = self.store.get_worker(str(record.get("worker_id") or ""))
            if not worker:
                continue
            callbacks = self._callback_config_for(worker)
            self._deliver_callback_record(worker, record, callbacks)

    def _callback_retry_loop(self) -> None:
        interval = _bounded_int_env(
            "GLASSHIVE_CALLBACK_RETRY_INTERVAL_S",
            30,
            min_value=1,
            max_value=3600,
        )
        while not self._shutdown_event.wait(interval):
            self._replay_pending_capability_grant_revocations()
            self._replay_pending_lifecycle_effects()
            self._replay_pending_callbacks()

    @staticmethod
    def _capability_revocation_retry_delay_s(record: dict) -> float:
        attempts = max(1, int(record.get("attempts") or 1))
        return float(min(300, 2 ** min(attempts, 8)))

    def _retry_capability_grant_revocation(
        self, record: dict, error_code: str
    ) -> None:
        safe_code = str(error_code or "").strip().lower()
        if safe_code not in {
            "broker_revocation_rejected",
            "broker_revocation_unavailable",
            "transient_dependency",
        }:
            safe_code = "unknown"
        try:
            self.store.retry_capability_grant_revocation(
                str(record.get("revocation_id") or ""),
                self._executor_id,
                lease_epoch=int(record.get("lease_epoch") or 0),
                error_code=safe_code,
                retry_delay_s=self._capability_revocation_retry_delay_s(record),
            )
        except Exception:
            logger.error(
                "GlassHive capability revocation retry persistence unavailable",
                extra={"error_code": "transient_dependency"},
            )

    def _apply_capability_grant_revocation(self, record: dict) -> None:
        body = {
            "authorizationRef": str(record.get("authorization_ref") or ""),
            "originRef": str(record.get("origin_ref") or ""),
            "workRef": str(record.get("work_ref") or ""),
            "workerId": str(record.get("worker_id") or ""),
            "runId": str(record.get("run_id") or ""),
            "grantId": str(record.get("grant_id") or ""),
            "containerGenerationId": str(
                record.get("container_generation_id") or ""
            ),
        }
        load_viventium_runtime_env(
            {
                "VIVENTIUM_GLASSHIVE_ADMISSION_URL",
                "VIVENTIUM_GLASSHIVE_ADMISSION_SECRET",
            }
        )
        try:
            revoke_capability_grant(
                str(os.environ.get("VIVENTIUM_GLASSHIVE_ADMISSION_URL") or ""),
                secret=str(
                    os.environ.get("VIVENTIUM_GLASSHIVE_ADMISSION_SECRET") or ""
                ),
                body=body,
                timeout_seconds=_bounded_float_env(
                    "VIVENTIUM_GLASSHIVE_ADMISSION_TIMEOUT_S",
                    5.0,
                    min_value=0.1,
                    max_value=30.0,
                ),
            )
        except BrokerAdmissionError as exc:
            self._retry_capability_grant_revocation(record, exc.code)
            return
        except Exception:
            self._retry_capability_grant_revocation(record, "unknown")
            return
        self.store.mark_capability_grant_revocation_applied(
            str(record.get("revocation_id") or ""),
            self._executor_id,
            lease_epoch=int(record.get("lease_epoch") or 0),
        )

    def _replay_pending_capability_grant_revocations(
        self,
        *,
        created_before: str | None = None,
        revocation_id: str | None = None,
    ) -> None:
        if self._shutdown_event.is_set():
            return
        self.store.activate_due_capability_grant_revocations()
        for _ in range(100):
            if self._shutdown_event.is_set():
                return
            record = self.store.claim_next_capability_grant_revocation(
                self._executor_id,
                ttl_s=60,
                created_before=created_before,
                revocation_id=revocation_id,
            )
            if not record:
                return
            self._apply_capability_grant_revocation(record)
            if revocation_id:
                return

    def _replay_pending_lifecycle_effects(
        self, *, created_before: str | None = None
    ) -> None:
        """Drain every durable sink kind independently with leased ownership."""

        effect_kinds = (
            # Revocation must commit before a terminal callback is visible.
            "signed_links.revoke_worker",
            "callback.worker_terminated",
            "callback.work_stopped",
            "callback.run_cancelled",
            "callback.run_paused",
            "callback.run_resumed",
            "callback.run_resumed_in_place",
            "callback.run_resumed_queued",
            "callback.run_interrupted",
            "callback.run_steered",
            "callback.worker_paused",
            "callback.worker_resumed",
        )
        # Visit one row per kind per round. A retry-wait row cannot block a
        # different effect kind, worker, or sink.
        faulted_kinds: set[str] = set()
        for _round in range(100):
            if self._shutdown_event.is_set():
                return
            claimed_any = False
            for effect_kind in effect_kinds:
                if self._shutdown_event.is_set():
                    return
                if effect_kind in faulted_kinds:
                    continue
                try:
                    effect = self.store.claim_next_lifecycle_effect(
                        self._executor_id,
                        ttl_s=60,
                        effect_kinds=(effect_kind,),
                        created_before=created_before,
                    )
                except Exception:
                    faulted_kinds.add(effect_kind)
                    logger.error(
                        "GlassHive lifecycle effect claim will retry",
                        extra={
                            "effect_kind": effect_kind,
                            "error_code": "transient_dependency",
                        },
                    )
                    continue
                if not effect:
                    continue
                claimed_any = True
                try:
                    self._apply_lifecycle_effect(effect)
                except Exception:
                    # A single malformed dependency or sink must not kill the
                    # recurring recovery thread or globally HOL-block kinds.
                    self._retry_lifecycle_effect(effect, "unknown")
                    faulted_kinds.add(effect_kind)
                    logger.error(
                        "GlassHive lifecycle effect application faulted safely",
                        extra={
                            "effect_kind": effect_kind,
                            "worker_id": str(effect.get("worker_id") or ""),
                            "error_code": "unknown",
                        },
                    )
            if not claimed_any:
                return

    @staticmethod
    def _lifecycle_effect_retry_delay_s(effect: dict) -> float:
        attempts = max(1, int(effect.get("attempts") or 1))
        return float(min(300, 2 ** min(attempts, 8)))

    def _retry_lifecycle_effect(self, effect: dict, error_code: str) -> bool:
        try:
            retried = self.store.retry_lifecycle_effect(
                str(effect.get("effect_id") or ""),
                self._executor_id,
                lease_epoch=int(effect.get("lease_epoch") or 0),
                error_code=error_code,
                retry_delay_s=self._lifecycle_effect_retry_delay_s(effect),
            )
        except Exception:
            logger.error(
                "GlassHive lifecycle effect retry persistence unavailable",
                extra={
                    "effect_kind": str(effect.get("effect_kind") or ""),
                    "worker_id": str(effect.get("worker_id") or ""),
                    "error_code": "transient_dependency",
                },
            )
            return False
        if not retried:
            return False
        logger.warning(
            "GlassHive lifecycle effect will retry",
            extra={
                "effect_kind": str(effect.get("effect_kind") or ""),
                "worker_id": str(effect.get("worker_id") or ""),
                "error_code": error_code,
            },
        )
        return True

    def _apply_lifecycle_effect(self, effect: dict) -> None:
        effect_id = str(effect.get("effect_id") or "")
        lease_epoch = int(effect.get("lease_epoch") or 0)
        effect_kind = str(effect.get("effect_kind") or "")
        worker = self.store.get_worker(str(effect.get("worker_id") or ""))
        if not worker:
            self._retry_lifecycle_effect(effect, "transient_dependency")
            return

        if effect_kind == "signed_links.revoke_worker":
            try:
                revoke_signed_link_refs_for_worker(str(worker["worker_id"]))
            except Exception:
                self._retry_lifecycle_effect(effect, "signed_link_revoke_failed")
                return
            self.store.mark_lifecycle_effect_applied(
                effect_id,
                self._executor_id,
                lease_epoch=lease_epoch,
            )
            return

        if (
            effect_kind == "callback.worker_terminated"
            and (
                not is_worker_signed_link_revoked(
                    str(worker.get("worker_id") or "")
                )
                or not self.store.paired_lifecycle_effect_is_applied(
                    effect,
                    required_effect_kind="signed_links.revoke_worker",
                )
            )
        ):
            self._retry_lifecycle_effect(effect, "transient_dependency")
            return

        run_id = str(effect.get("run_id") or "")
        run = self.store.get_run(run_id) if run_id else None
        run_required = effect_kind not in {
            "callback.worker_paused",
            "callback.worker_resumed",
            "callback.worker_terminated",
        }
        if run_required and not run:
            self._retry_lifecycle_effect(effect, "transient_dependency")
            return
        event_type, message = {
            "callback.run_cancelled": ("run.cancelled", "Run cancelled"),
            "callback.run_paused": ("run.paused", "Worker paused"),
            "callback.run_resumed": (
                "run.started"
                if str((run or {}).get("state") or "") == "running"
                else "run.queued",
                "Paused run resumed",
            ),
            "callback.run_resumed_in_place": (
                "run.started",
                "Paused run resumed",
            ),
            "callback.run_resumed_queued": (
                "run.queued",
                "Paused run queued for execution restart",
            ),
            "callback.run_interrupted": (
                "run.interrupted",
                "Run interruption accepted",
            ),
            "callback.run_steered": (
                "run.queued",
                "Replacement steer instruction queued",
            ),
            "callback.work_stopped": ("run.cancelled", "Work stop confirmed"),
            "callback.worker_paused": ("worker.paused", "Worker paused"),
            "callback.worker_resumed": ("worker.resumed", "Worker resumed"),
            "callback.worker_terminated": (
                "worker.terminated",
                "Worker terminated",
            ),
        }[effect_kind]
        callback_id = "cb_effect_" + effect_id
        existing_record = self.store.get_callback_outbox(callback_id)
        if existing_record is not None:
            expected_run_id = str((run or {}).get("run_id") or "")
            if (
                str(existing_record.get("worker_id") or "")
                != str(worker.get("worker_id") or "")
                or str(existing_record.get("event_type") or "") != event_type
                or str(existing_record.get("run_id") or "") != expected_run_id
            ):
                self._retry_lifecycle_effect(effect, "callback_enqueue_failed")
                return
            # A deterministic callback row is the durable sink. Its own retry
            # loop owns delivery even if callback configuration later vanishes.
            self.store.mark_lifecycle_effect_applied(
                effect_id,
                self._executor_id,
                lease_epoch=lease_epoch,
            )
            return

        callbacks = self._callback_config_for(worker)
        callback_url = str(
            callbacks.get("events_webhook_url") or callbacks.get("url") or ""
        ).strip()
        if not callback_url or (
            _is_viventium_callback_url(callback_url)
            and not self._viventium_callback_context_ready(worker, callbacks)
        ):
            self._retry_lifecycle_effect(effect, "callback_config_missing")
            return
        try:
            record = self._emit_callback(
                worker,
                event_type,
                run=run,
                message=message,
                callback_id=callback_id,
                insert_once=True,
                submit_delivery=False,
            )
        except Exception:
            self._retry_lifecycle_effect(effect, "callback_enqueue_failed")
            return
        if record is None:
            self._retry_lifecycle_effect(effect, "callback_enqueue_failed")
            return
        # HTTP delivery is owned by callback_outbox. This sink completes once
        # its immutable outbox row durably exists.
        applied = self.store.mark_lifecycle_effect_applied(
            effect_id,
            self._executor_id,
            lease_epoch=lease_epoch,
        )
        if applied and record and not self._shutdown_event.is_set():
            delivery_record = {
                key: value for key, value in record.items() if key != "_inserted"
            }
            self.executor.submit(
                self._deliver_callback_record,
                dict(worker),
                delivery_record,
                callbacks,
            )

    @staticmethod
    def _trusted_run_lane(worker: dict | None) -> str:
        return (
            "conversation"
            if isinstance(worker, dict)
            and str(worker.get("trusted_run_lane") or "").strip().lower()
            == "conversation"
            else "mission"
        )

    def _ensure_execution_allowed(
        self,
        worker_or_mode: dict | str,
        *,
        trusted_run_lane: str = "mission",
    ) -> None:
        execution_mode = (
            str(worker_or_mode.get("execution_mode") or "docker")
            if isinstance(worker_or_mode, dict)
            else str(worker_or_mode or "docker")
        )
        lane = (
            self._trusted_run_lane(worker_or_mode)
            if isinstance(worker_or_mode, dict)
            else (
                "conversation"
                if str(trusted_run_lane or "").strip().lower() == "conversation"
                else "mission"
            )
        )
        if (
            execution_mode == "host"
            and lane == "mission"
            and isolated_parallel_policy_enabled()
        ):
            raise ParallelExecutionIsolationError(
                "Host-native mission roots are unavailable while isolated Parallel policy is enabled."
            )
        if execution_mode == "host" and not host_workers_enabled():
            raise HostWorkersDisabledError(
                "GlassHive host-native workers are disabled by Viventium config"
            )

    def orchestration_capabilities(self) -> dict[str, object]:
        policy_enabled = isolated_parallel_policy_enabled()
        active_worker_ids = self.store.active_host_mission_worker_ids()
        process_status_reader = getattr(
            self.runtime, "host_active_process_status", None
        )
        process_state_uncertain = False
        for worker in self.store.list_host_mission_workers():
            worker_id = str(worker.get("worker_id") or "")
            if not callable(process_status_reader):
                process_state_uncertain = True
                continue
            try:
                status = process_status_reader(worker)
            except Exception:
                logger.exception(
                    "Failed to prove host mission process absence for worker %s",
                    worker_id,
                )
                process_state_uncertain = True
                continue
            state = str((status or {}).get("state") or "uncertain")
            if state == "active":
                active_worker_ids.add(worker_id)
            elif state != "absent":
                process_state_uncertain = True
        isolated_runtime_ready = False
        isolated_runtime_reason = "isolated_runtime_readiness_unavailable"
        isolated_readiness_probe = getattr(
            self.runtime, "isolated_parallel_readiness", None
        )
        if callable(isolated_readiness_probe):
            try:
                try:
                    readiness = isolated_readiness_probe(cached_only=True)
                except TypeError:
                    readiness = isolated_readiness_probe()
                isolated_runtime_ready = bool((readiness or {}).get("ready"))
                raw_reason = str((readiness or {}).get("reason") or "").strip()
                if raw_reason and re.fullmatch(r"[a-z0-9_.-]{1,120}", raw_reason):
                    isolated_runtime_reason = raw_reason
            except Exception:
                logger.exception("Failed to probe isolated Parallel runtime readiness")
        active_host_missions = len(active_worker_ids)
        isolated_parallel_ready = bool(
            policy_enabled
            and active_host_missions == 0
            and not process_state_uncertain
            and isolated_runtime_ready
        )
        if isolated_parallel_ready:
            isolated_parallel_reason = ""
        elif not policy_enabled:
            isolated_parallel_reason = "isolated_parallel_policy_disabled"
        elif active_host_missions > 0:
            isolated_parallel_reason = "host_missions_active"
        elif process_state_uncertain:
            isolated_parallel_reason = "host_mission_state_uncertain"
        else:
            isolated_parallel_reason = isolated_runtime_reason
        return {
            "policyVersion": 1,
            "isolatedParallelReady": isolated_parallel_ready,
            "isolatedParallelReason": isolated_parallel_reason,
            "hostMissionsAllowed": not policy_enabled,
            "hostMissionsActive": active_host_missions,
        }

    def _ensure_profile_allowed(self, profile: str) -> None:
        allowed = allowed_worker_profiles()
        if allowed and str(profile or "").strip() not in allowed:
            raise GlassHiveProfileNotAllowedError(
                f"GlassHive worker profile '{profile}' is not allowed by GLASSHIVE_ALLOWED_WORKER_PROFILES"
            )

    def _ensure_runtime_available(self, profile: str, execution_mode: str) -> None:
        if hasattr(self.runtime, "preflight_worker_profile"):
            self.runtime.preflight_worker_profile(profile, execution_mode)

    def _resolve_worker_model(self, profile: str, execution_mode: str = "docker") -> str:
        try:
            return str(self.runtime.resolve_model(profile, execution_mode=execution_mode) or "")
        except TypeError:
            return str(self.runtime.resolve_model(profile) or "")

    def _refresh_worker_model_for_profile(self, worker: dict) -> dict:
        profile = str(worker.get("profile") or "").strip()
        worker_id = str(worker.get("worker_id") or "").strip()
        if not profile or not worker_id:
            return worker
        bootstrap_bundle = self._bootstrap_bundle_for(worker) or {}
        provider_model = str(bootstrap_bundle.get("provider_model") or "").strip()
        try:
            resolved_model = provider_model or self._resolve_worker_model(
                profile, str(worker.get("execution_mode") or "docker")
            ).strip()
        except Exception as exc:
            logger.warning("Could not resolve model for worker %s profile %s: %s", worker_id, profile, exc)
            return worker
        current_model = str(worker.get("model") or "").strip()
        if not resolved_model or resolved_model == current_model:
            return worker
        updated = self.store.update_worker(worker_id, model=resolved_model) or worker
        self.store.add_event(
            str(worker.get("project_id") or ""),
            worker_id,
            None,
            "worker.model_refreshed",
            f"Worker model refreshed from {current_model or '<unset>'} to {resolved_model}",
        )
        return updated

    def _operator_base_url(self) -> str:
        return (
            os.environ.get("GLASSHIVE_OPERATOR_BASE_URL", "").strip()
            or os.environ.get("WPR_OPERATOR_BASE_URL", "").strip()
            or os.environ.get("WPR_PUBLIC_BASE_URL", "").strip()
        ).rstrip("/")

    def _artifact_base_url(self) -> str:
        return (
            os.environ.get("GLASSHIVE_ARTIFACT_BASE_URL", "").strip()
            or os.environ.get("WPR_ARTIFACT_BASE_URL", "").strip()
            or os.environ.get("GLASSHIVE_RUNTIME_PUBLIC_BASE_URL", "").strip()
            or self._operator_base_url()
        ).rstrip("/")

    def _signed_link_params(self, worker: dict, *, kind: str, path: str = "") -> dict[str, str]:
        if str(worker.get("state") or "") == "terminated":
            return {}
        return sign_link_params(
            kind=kind,
            worker_id=str(worker.get("worker_id") or ""),
            tenant_id=str(worker.get("tenant_id") or ""),
            owner_id=str(worker.get("owner_id") or ""),
            path=path,
        )

    def _signed_artifact_url(self, worker: dict, workspace_path: str, *, kind: str, action: str) -> str:
        base_url = self._artifact_base_url()
        worker_id = str(worker.get("worker_id") or "")
        path = str(workspace_path or "").strip().lstrip("/")
        if (
            str(worker.get("state") or "") == "terminated"
            or not base_url
            or not worker_id
            or not path
            or not is_user_deliverable_relative_path(path)
        ):
            return ""
        token = sign_link_token(
            kind=kind,
            worker_id=worker_id,
            tenant_id=str(worker.get("tenant_id") or ""),
            owner_id=str(worker.get("owner_id") or ""),
            path=path,
        )
        if token:
            ref_id = create_signed_link_ref(token=token)
            return signed_link_ref_url(base_url, ref_id) if ref_id else ""
        if str(worker.get("tenant_id") or "") not in {"", "local"}:
            return ""
        return f"{base_url}/v1/workers/{worker_id}/artifacts/{action}?{urlencode({'path': path})}"

    def _signed_artifact_open_url(self, worker: dict, workspace_path: str) -> str:
        return self._signed_artifact_url(worker, workspace_path, kind="artifact_open", action="open")

    def _signed_artifact_download_url(self, worker: dict, workspace_path: str) -> str:
        return self._signed_artifact_url(worker, workspace_path, kind="artifact_download", action="download")

    def _signed_watch_url(self, worker: dict, callbacks: dict[str, object] | None = None) -> str:
        callbacks = callbacks or {}
        if str(worker.get("state") or "") == "terminated":
            return ""
        worker_id = str(worker.get("worker_id") or "").strip()
        project_id = str(worker.get("project_id") or "").strip()
        base_url = self._operator_base_url()
        if not worker_id or not base_url or not surface_aware_watch_url(
            worker_id,
            project_id,
            request_surface=str(callbacks.get("surface") or ""),
            watch_surface="desktop",
            base_url=base_url,
        ):
            return ""
        token = sign_link_token(
            kind="worker_view",
            worker_id=worker_id,
            tenant_id=str(worker.get("tenant_id") or ""),
            owner_id=str(worker.get("owner_id") or ""),
        )
        watch_url = surface_aware_watch_url(
            worker_id,
            project_id,
            request_surface=str(callbacks.get("surface") or ""),
            watch_surface="desktop",
            base_url=base_url,
        )
        if not watch_url:
            return ""
        if token:
            target_url = append_signed_query(watch_url, {"gh_token": token})
            ref_id = create_signed_link_ref(token=token, target_url=target_url)
            return signed_link_ref_url(base_url, ref_id, route="/r") if ref_id else ""
        if str(worker.get("tenant_id") or "") not in {"", "local"}:
            return ""
        return watch_url

    def _callback_message_with_links(
        self,
        worker: dict,
        message: str,
        deliverable: dict[str, object] | None,
        callbacks: dict[str, object] | None = None,
        *,
        include_watch_link: bool = False,
    ) -> str:
        text = public_callback_message_text(message)
        if not deliverable and not include_watch_link:
            return text
        links: list[str] = []
        workspace_path = str((deliverable or {}).get("workspace_path") or "").strip()
        if deliverable and workspace_path:
            download_url = self._signed_artifact_download_url(worker, workspace_path)
            if download_url:
                links.append(f"File: [Download file]({download_url})")
            open_url = self._signed_artifact_open_url(worker, workspace_path)
            if open_url:
                links.append(f"Preview: [Open GlassHive file]({open_url})")
        watch_url = self._signed_watch_url(worker, callbacks)
        if watch_url:
            links.append(f"View / Steer: [Open GlassHive workspace]({watch_url})")
        if not links:
            return text
        suffix = "\n".join(links)
        return f"{text}\n\n{suffix}" if text else suffix

    def _delegation_callback_state(
        self,
        delegation: dict,
        *,
        callback_run: dict | None,
    ) -> tuple[str, bool]:
        """Project authoritative mission truth without hiding per-run evidence."""

        worker_id = str(delegation.get("worker_id") or "")
        active = self.store.list_nonterminal_runs_for_worker(worker_id)
        if active:
            state_priority = (
                "running",
                "settling",
                "paused",
                "needs_input",
                "queued",
            )
            states = {str(item.get("state") or "") for item in active}
            state = next((item for item in state_priority if item in states), "queued")
            return state, False

        latest = callback_run
        if not latest:
            current_run_id = str(delegation.get("current_run_id") or "")
            latest = self.store.get_run(current_run_id) if current_run_id else None
        state = str((latest or {}).get("state") or "failed")
        if state == "interrupted":
            state = "cancelled"
        if state not in TERMINAL_RUN_STATES:
            # Missing/unknown mission truth must fail nonterminal, never cause
            # Core to terminalize a WorkRef accidentally.
            return state or "queued", False
        return state, True

    def _emit_callback(
        self,
        worker: dict,
        event_type: str,
        *,
        run: dict | None = None,
        message: str = "",
        full_message: str = "",
        deliverable: dict[str, object] | None = None,
        callback_id: str = "",
        insert_once: bool = False,
        submit_delivery: bool = True,
    ) -> dict | None:
        callbacks = self._callback_config_for(worker)
        url = str(callbacks.get("events_webhook_url") or callbacks.get("url") or "").strip()
        if not url:
            return None
        if _is_viventium_callback_url(url):
            if not self._viventium_callback_context_ready(worker, callbacks):
                missing_parent_fields = _missing_parent_callback_fields(callbacks)
                logger.info(
                    "Skipping GlassHive parent callback for worker %s because callback context is incomplete: %s",
                    worker.get("worker_id"),
                    ", ".join(missing_parent_fields),
                )
                return None
        link_safe = event_type != "worker.terminated"
        operator_url = self._signed_watch_url(worker, callbacks) if link_safe else ""
        include_watch_link = link_safe and event_type in ACTIONABLE_CALLBACK_LINK_EVENTS
        payload = {
            "callback_id": str(callback_id or "") or f"cb_{uuid.uuid4().hex}",
            "callback_ts": int(time.time()),
            "event": event_type,
            "project_id": worker.get("project_id"),
            "worker_id": worker.get("worker_id"),
            "run_id": (run or {}).get("run_id"),
            "run_state": callback_run_state(event_type, run),
            "message": self._callback_message_with_links(
                worker,
                message,
                deliverable,
                callbacks,
                include_watch_link=include_watch_link,
            ),
            "full_message": self._callback_message_with_links(
                worker,
                full_message,
                deliverable,
                callbacks,
                include_watch_link=include_watch_link,
            )
            if full_message
            else "",
            "user_id": callbacks.get("user_id"),
            "agent_id": callbacks.get("agent_id"),
            "conversation_id": callbacks.get("conversation_id"),
            "parent_message_id": callbacks.get("parent_message_id"),
            "message_id": callbacks.get("message_id"),
            "surface": callbacks.get("surface"),
            "input_mode": callbacks.get("input_mode"),
            "stream_id": callbacks.get("stream_id"),
            "voice_call_session_id": callbacks.get("voice_call_session_id"),
            "voice_request_id": callbacks.get("voice_request_id"),
            "telegram_chat_id": callbacks.get("telegram_chat_id"),
            "telegram_user_id": callbacks.get("telegram_user_id"),
            "telegram_message_id": callbacks.get("telegram_message_id"),
            "logical_turn_id": callbacks.get("logical_turn_id"),
            "logical_turn_revision": callbacks.get("logical_turn_revision"),
        }
        delegation = self.store.get_delegation_for_worker(
            str(worker.get("worker_id") or ""),
            tenant_id=str(worker.get("tenant_id") or "local"),
            owner_id=str(worker.get("owner_id") or ""),
        )
        if delegation:
            # These identifiers come from the durable delegation reservation,
            # never from callback input supplied by the caller.
            payload["origin_ref"] = str(delegation.get("origin_ref") or "")
            payload["work_ref"] = str(delegation.get("work_ref") or "")
            work_state, work_terminal = self._delegation_callback_state(
                delegation,
                callback_run=run,
            )
            payload["work_state"] = work_state
            payload["work_terminal"] = work_terminal
        failure_class = str((run or {}).get("failure_class") or "").strip()
        if failure_class:
            payload["failure_code"] = failure_class
            payload["failure_class"] = failure_class
            payload["failure_retryable"] = bool((run or {}).get("failure_retryable"))
        projection_resolver = getattr(self.runtime, "effort_projection_for_worker", None)
        if callable(projection_resolver):
            try:
                effort_projection = projection_resolver(worker)
            except Exception:
                effort_projection = {}
            if isinstance(effort_projection, dict) and effort_projection:
                payload["effort_projection"] = {
                    "requested": str(effort_projection.get("requested") or "")[:32],
                    "effective": str(effort_projection.get("effective") or "")[:32],
                    "fallback_reason": str(effort_projection.get("fallback_reason") or "")[:64],
                }
        if deliverable:
            payload["deliverable"] = deliverable
        if operator_url:
            payload["operator_url"] = operator_url
            payload["watch_url"] = operator_url
        insert_callback = (
            self.store.insert_callback_outbox_once
            if insert_once
            else self.store.upsert_callback_outbox
        )
        record = insert_callback(
            callback_id=str(payload["callback_id"]),
            project_id=str(worker.get("project_id") or ""),
            worker_id=str(worker.get("worker_id") or ""),
            run_id=(run or {}).get("run_id"),
            event_type=event_type,
            url=url,
            payload_json=json.dumps(payload, ensure_ascii=False),
        )
        if submit_delivery:
            self.executor.submit(
                self._deliver_callback_record, dict(worker), record, callbacks
            )
        return record

    def _run_start_callback_record(
        self,
        worker: dict,
        run: dict,
        startup_token: str,
    ) -> tuple[dict[str, object] | None, dict]:
        """Build the bounded durable callback row published by the startup CAS.

        This deliberately does not mint signed links or perform network I/O while
        the lifecycle flock is held. Delivery may add an exact run-action
        capability after the transaction commits.
        """

        callbacks = self._callback_config_for(worker)
        url = str(
            callbacks.get("events_webhook_url") or callbacks.get("url") or ""
        ).strip()
        if not url:
            return None, callbacks
        if _is_viventium_callback_url(url):
            if not self._viventium_callback_context_ready(worker, callbacks):
                return None, callbacks
        callback_id = "cb_start_" + hashlib.sha256(
            str(startup_token).encode("utf-8")
        ).hexdigest()
        payload: dict[str, object] = {
            "callback_id": callback_id,
            "callback_ts": int(time.time()),
            "event": "run.started",
            "project_id": worker.get("project_id"),
            "worker_id": worker.get("worker_id"),
            "run_id": run.get("run_id"),
            "run_state": "running",
            "message": public_callback_message_text(
                str(run.get("instruction") or "")
            )[:TERMINAL_CALLBACK_MESSAGE_LIMIT],
            "full_message": "",
        }
        for field in (
            "user_id",
            "agent_id",
            "conversation_id",
            "parent_message_id",
            "message_id",
            "surface",
            "input_mode",
            "stream_id",
            "voice_call_session_id",
            "voice_request_id",
            "telegram_chat_id",
            "telegram_user_id",
            "telegram_message_id",
            "logical_turn_id",
            "logical_turn_revision",
        ):
            payload[field] = callbacks.get(field)
        delegation = self.store.get_delegation_for_worker(
            str(worker.get("worker_id") or ""),
            tenant_id=str(worker.get("tenant_id") or "local"),
            owner_id=str(worker.get("owner_id") or ""),
        )
        if delegation:
            payload["origin_ref"] = str(delegation.get("origin_ref") or "")
            payload["work_ref"] = str(delegation.get("work_ref") or "")
            work_state, work_terminal = self._delegation_callback_state(
                delegation,
                callback_run={**run, "state": "running"},
            )
            payload["work_state"] = work_state
            payload["work_terminal"] = work_terminal
        return {
            "url": url,
            "payload_json": json.dumps(payload, ensure_ascii=False),
        }, callbacks

    def _completion_deliverable(self, worker: dict, run: dict, output_text: str, error_text: str = "") -> dict[str, object] | None:
        return deliverable_payload(worker, run, output_text, output_text, error_text)

    def _promote_completed_deliverable(
        self,
        worker: dict,
        run: dict,
        deliverable: dict[str, object] | None,
    ) -> None:
        if not deliverable:
            return
        if deliverable.get("kind") != "webpage" or deliverable.get("preferred_surface") != "desktop":
            return
        if str(worker.get("execution_mode") or "docker") == "host":
            return
        browser_url = str(deliverable.get("browser_url") or "").strip()
        run_id = str(run.get("run_id") or "").strip()
        worker_id = str(worker.get("worker_id") or "").strip()
        project_id = str(worker.get("project_id") or "").strip()
        if not browser_url or not run_id or not worker_id or not project_id:
            return
        promotion_key = f"{run_id}:{browser_url}"
        if not hasattr(self.runtime, "desktop_action"):
            return
        with self._deliverable_promotions_lock:
            if promotion_key in self._deliverable_promotions:
                return
            existing_events = self.store.list_events(worker_id)
            if any(
                event.get("event_type") == "deliverable.opened"
                and str(event.get("message") or "") == promotion_key
                for event in existing_events
            ):
                self._deliverable_promotions.add(promotion_key)
                return
            self._deliverable_promotions.add(promotion_key)
            try:
                self.desktop_action(worker_id, "browser", url=browser_url, run_id=run_id)
                self.store.add_event(project_id, worker_id, run_id, "deliverable.opened", promotion_key)
            except Exception as exc:
                self._deliverable_promotions.discard(promotion_key)
                logger.warning("Failed to promote GlassHive deliverable %s: %s", promotion_key, exc)

    def _idle_terminate_after_s(self) -> int:
        return _bounded_int_env("GLASSHIVE_IDLE_TERMINATE_AFTER_S", 0, min_value=0, max_value=30 * 24 * 3600)

    def _paused_terminate_after_s(self) -> int:
        return _bounded_int_env("GLASSHIVE_PAUSED_TERMINATE_AFTER_S", 0, min_value=0, max_value=30 * 24 * 3600)

    def _max_run_duration_s(self) -> int:
        return _bounded_int_env("GLASSHIVE_MAX_RUN_DURATION_S", 0, min_value=0, max_value=30 * 24 * 3600)

    def _idle_reaper_interval_s(self) -> int:
        return _bounded_int_env("GLASSHIVE_IDLE_REAPER_INTERVAL_S", 60, min_value=1, max_value=3600)

    def _compute_release_claim_ttl_s(self) -> int:
        return _bounded_int_env(
            "GLASSHIVE_COMPUTE_RELEASE_CLAIM_TTL_S",
            600,
            min_value=30,
            max_value=3600,
        )

    def _worker_lifecycle_lock_timeout_s(self) -> float:
        return _bounded_float_env(
            "GLASSHIVE_WORKER_LIFECYCLE_LOCK_TIMEOUT_S",
            30.0,
            min_value=0.1,
            max_value=300.0,
        )

    def _acquire_worker_lifecycle_guard(
        self, worker_id: str
    ) -> _WorkerLifecycleGuard:
        digest = hashlib.sha256(worker_id.encode("utf-8")).hexdigest()[:24]
        canonical_db_path = Path(self.store.db_path).resolve(strict=False)
        lock_path = Path(f"{canonical_db_path}.compute-release-{digest}.lock")
        handle = lock_path.open("a+")
        lock_path.chmod(0o600)
        deadline = time.monotonic() + self._worker_lifecycle_lock_timeout_s()
        while True:
            try:
                fcntl.flock(
                    handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                )
                return _WorkerLifecycleGuard(handle)
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    handle.close()
                    raise RuntimeErrorBase(
                        "Worker lifecycle control is busy; retry shortly"
                    )
                time.sleep(0.01)

    @contextmanager
    def _worker_compute_release_lock(self, worker_id: str):
        guard = self._acquire_worker_lifecycle_guard(worker_id)
        try:
            yield
        finally:
            guard.release()

    def _runtime_compute_container_id(self, worker: dict) -> str:
        resolver = getattr(self.runtime, "compute_identity", None)
        if not callable(resolver):
            return ""
        identity = resolver(worker)
        return str((identity or {}).get("container_id") or "").strip()

    def _require_claimed_container_generation(self, worker: dict) -> dict:
        """Re-probe and bind one captured Docker generation before control RPC."""

        captured_container_id = str(
            worker.get("compute_release_container_id") or ""
        ).strip()
        if not captured_container_id:
            return worker
        current_container_id = self._runtime_compute_container_id(worker)
        if current_container_id != captured_container_id:
            raise RuntimeErrorBase(
                "Worker sandbox generation changed before exact lifecycle control; "
                "the claim remains fenced"
            )
        return {
            **worker,
            "_compute_release_container_id": captured_container_id,
        }

    def _runtime_control_info_is_confirmed(self, info: RuntimeInfo) -> bool:
        # The deterministic/in-process adapter has no external compute identity;
        # its synchronous return is the confirmation boundary. Real process or
        # Docker adapters must report no live target PID after destructive
        # Interrupt/Steer, and Docker Pause proves suspension in its own call.
        return bool(
            getattr(self.runtime, "requires_run_start_identity", True) is False
            or info.pid is None
        )

    def _require_confirmed_host_control_identity(
        self,
        worker: dict,
        run_id: str,
    ) -> dict:
        """Return one exact confirmed host lease before a destructive signal."""

        if str(worker.get("execution_mode") or "docker") != "host":
            return worker
        if getattr(self.runtime, "requires_run_start_identity", True) is False:
            # Deterministic/in-process adapters have no external PID generation to prove. Their
            # synchronous control call is already the declared identity boundary; the lifecycle
            # claim and per-worker flock below still fence the exact durable run. Never extend this
            # path to a real host adapter, which must publish the full confirmed lease tuple.
            return {**worker, "_active_run_id": run_id}
        lease = self.store.get_active_host_run_lease_for_run(run_id)
        if (
            not lease
            or str(lease.get("worker_id") or "") != str(worker.get("worker_id") or "")
            or str(lease.get("startup_state") or "") != "confirmed"
            or str(lease.get("startup_identity_kind") or "") != "host_process"
            or int(lease.get("pid") or 0) <= 0
            or int(lease.get("process_group") or 0) <= 0
            or not str(lease.get("process_start_identity") or "").strip()
            or not str(lease.get("startup_session_id") or "").strip()
        ):
            raise RuntimeErrorBase(
                "The exact host process identity is not confirmed; control remains pending"
            )
        return {**worker, "_active_run_id": run_id, "_host_run_lease": lease}

    def _claim_exact_run_control(
        self,
        worker: dict,
        run: dict,
        *,
        kind: str,
        replacement_run: dict[str, object] | None = None,
        action_use_id: str = "",
    ) -> dict[str, object]:
        target_run_id = str(run.get("run_id") or "")
        expected_container_id = self._runtime_compute_container_id(worker)
        claim = self.store.try_claim_worker_compute_release(
            str(worker["worker_id"]),
            expected_updated_at=str(worker.get("updated_at") or ""),
            expected_last_run_id=str(worker.get("last_run_id") or ""),
            expected_state=str(worker.get("state") or ""),
            expected_container_id=expected_container_id,
            owner=self._executor_id,
            ttl_s=self._compute_release_claim_ttl_s(),
            kind=kind,
            target_run_id=target_run_id,
            expected_target_started_at=str(run.get("started_at") or ""),
            replacement_run=replacement_run,
            action_use_id=action_use_id,
            action_executor_id=self._executor_id if action_use_id else "",
        )
        if claim is None:
            raise RuntimeErrorBase("active_work_generation_changed")
        return claim

    def _restore_failed_resume_claim(
        self,
        *,
        worker: dict,
        token: str,
        epoch: int,
        kind: str,
        target_run_id: str,
        startup_error: BaseException,
    ) -> None:
        """Restore paused truth only after compensating runtime proof.

        Startup may have made compute live before raising.  Until a compensating
        pause is explicitly confirmed, the durable claim remains as the safety
        fence and no resumed truth is published.
        """

        if getattr(self.runtime, "requires_run_start_identity", True) is False:
            restored = self.store.abandon_worker_run_control_claim(
                str(worker["worker_id"]),
                token,
                epoch,
                kind=kind,
                target_run_id=target_run_id,
                worker_state="paused",
                last_error=str(startup_error),
            )
            if restored is None:
                raise RuntimeErrorBase(
                    "Resume startup failed and paused ownership could not be restored"
                ) from startup_error
            return
        try:
            info = self.runtime.pause_worker(
                self._require_claimed_container_generation(worker)
            )
            if not self._runtime_control_info_is_confirmed(info):
                raise RuntimeErrorBase(
                    "Resume compensation did not confirm paused compute"
                )
        except Exception as compensation_error:
            raise RuntimeErrorBase(
                "Resume startup failed and compensating pause was not confirmed; "
                "the lifecycle claim remains fenced"
            ) from startup_error
        restored = self.store.abandon_worker_run_control_claim(
            str(worker["worker_id"]),
            token,
            epoch,
            kind=kind,
            target_run_id=target_run_id,
            worker_state="paused",
            last_error=str(startup_error),
        )
        if restored is None:
            raise RuntimeErrorBase(
                "Resume startup failed and paused ownership could not be restored"
            ) from startup_error

    def _release_worker_compute(
        self,
        worker: dict,
        *,
        idle_seconds: float,
        kind: str = "idle",
        target_run_id: str = "",
        target_started_at: str = "",
        target_error_text: str = "",
    ) -> dict[str, object] | None:
        worker_id = str(worker.get("worker_id") or "")
        with self._worker_compute_release_lock(worker_id):
            current = self.store.get_worker(worker_id)
            if not current or current.get("compute_released_at"):
                return None
            existing_token = str(current.get("compute_release_token") or "").strip()
            requested_kind = str(
                current.get("compute_release_kind") or kind or "idle"
            ).strip().lower()
            requested_target = str(
                current.get("compute_release_target_run_id") or target_run_id or ""
            ).strip()
            requested_target_started_at = str(
                current.get("compute_release_target_started_at")
                or target_started_at
                or ""
            ).strip()
            if not existing_token and requested_kind == "idle" and (
                self.store.get_active_run(worker_id) or self.store.has_queued_runs(worker_id)
            ):
                return None
            last_run_id = str(current.get("last_run_id") or "").strip()
            expected_container_id = (
                str(current.get("compute_release_container_id") or "").strip()
                if existing_token
                else self._runtime_compute_container_id(current)
            )
            claim = self.store.try_claim_worker_compute_release(
                worker_id,
                expected_updated_at=str(current.get("updated_at") or ""),
                expected_last_run_id=last_run_id,
                expected_state=str(current.get("state") or ""),
                expected_container_id=expected_container_id,
                owner=self._executor_id,
                ttl_s=self._compute_release_claim_ttl_s(),
                kind=requested_kind,
                target_run_id=requested_target,
                expected_target_started_at=requested_target_started_at,
            )
            if claim is None:
                return None
            claimed_worker = dict(claim.get("worker") or current)
            token = str(claim["token"])
            epoch = int(claim["epoch"])
            terminal_run_id = str(
                claimed_worker.get("compute_release_terminal_run_id")
                or last_run_id
                or ""
            ).strip()
            if not self.store.worker_compute_release_claim_matches(
                worker_id, token, epoch
            ):
                return None
            if requested_kind == "idle" and (
                self.store.get_active_run(worker_id)
                or self.store.has_queued_runs(worker_id)
            ):
                abandoned = self.store.abandon_stale_worker_compute_release_claim(
                    worker_id,
                    token,
                    epoch,
                    kind=requested_kind,
                )
                if abandoned is None:
                    raise RuntimeErrorBase(
                        "Idle release ownership changed before current work could be re-evaluated"
                    )
                if self.store.has_queued_runs(worker_id):
                    self._ensure_worker_processor(worker_id)
                return None
            captured_container_id = str(
                claimed_worker.get("compute_release_container_id") or ""
            ).strip()
            if str(claimed_worker.get("execution_mode") or "docker") == "docker":
                current_container_id = self._runtime_compute_container_id(
                    claimed_worker
                )
                if current_container_id != captured_container_id:
                    rebound = (
                        self.store.rebind_worker_compute_release_claim_generation(
                            worker_id,
                            token,
                            epoch,
                            kind=requested_kind,
                            container_id=current_container_id,
                        )
                    )
                    if rebound is None:
                        raise RuntimeErrorBase(
                            "Compute release generation changed before it could be rebound"
                        )
                    claimed_worker = rebound
                    epoch = int(rebound["compute_release_epoch"])
            claimed_runtime_worker = (
                self._worker_with_host_lease(claimed_worker, requested_target)
                if requested_kind == "max_duration" and requested_target
                else claimed_worker
            )
            runtime_worker = {
                **claimed_runtime_worker,
                "_compute_release_container_id": str(
                    claimed_worker.get("compute_release_container_id") or ""
                ).strip(),
            }
            terminal_run = (
                self.store.get_run(terminal_run_id) if terminal_run_id else None
            )
            if terminal_run and str(terminal_run.get("state") or "") in TERMINAL_RUN_STATES:
                runtime_worker["_terminal_run_id"] = terminal_run_id
            info = self.runtime.terminate_worker(runtime_worker)
            idle_state = str(current.get("state") or "")
            if idle_state not in TERMINAL_RUN_STATES:
                idle_state = "paused"
            target_result = None
            if requested_kind == "max_duration":
                target_result = self.store.finalize_worker_operation_claim(
                    worker_id,
                    token,
                    epoch,
                    kind=requested_kind,
                    target_run_id=requested_target,
                    target_expected_states=("running", "settling"),
                    target_state="cancelled",
                    target_error_text=target_error_text,
                    runtime_fields=self._runtime_info_fields(
                        worker_id,
                        info,
                        last_error=target_error_text,
                    ),
                    idle_state="paused",
                    compute_released_at=utc_now(),
                )
                updated = dict((target_result or {}).get("worker") or {})
            else:
                updated = self.store.finalize_worker_compute_release(
                    worker_id,
                    token,
                    epoch,
                    expected_kind=requested_kind,
                    target_run_id=requested_target,
                    compute_released_at=utc_now(),
                    runtime_fields=self._runtime_info_fields(
                        worker_id,
                        info,
                        last_error="",
                    ),
                    idle_state=idle_state,
                )
            if not updated:
                raise RuntimeError("Compute release ownership changed before finalization")
        if requested_kind in {"idle", "needs_input", "paused"}:
            event_type = (
                "worker.paused_compute_terminated"
                if requested_kind == "paused"
                else "worker.needs_input_compute_terminated"
                if requested_kind == "needs_input"
                else "worker.idle_terminated"
            )
            label = (
                "Paused worker"
                if requested_kind == "paused"
                else "Needs-input worker"
                if requested_kind == "needs_input"
                else "Idle worker"
            )
            self.store.add_event(
                str(worker.get("project_id") or ""),
                worker_id,
                None,
                event_type,
                f"{label} compute stopped after {int(idle_seconds)} seconds; workspace state preserved.",
            )
        if (
            updated.get("state") not in {"paused", "terminated", "needs_input"}
            and self.store.has_queued_runs(worker_id)
        ):
            self._ensure_worker_processor(worker_id)
        return {
            "worker_id": worker_id,
            "project_id": worker.get("project_id"),
            "tenant_id": worker.get("tenant_id"),
            "owner_id": worker.get("owner_id"),
            "state": updated.get("state"),
            "idle_seconds": int(idle_seconds),
            "kind": requested_kind,
            "target_run_id": requested_target,
            "target_transitioned": bool(
                (target_result or {}).get("target_transitioned")
            ),
        }

    def _release_needs_input_compute(
        self,
        worker: dict,
        run: dict,
    ) -> dict[str, object] | None:
        try:
            return self._release_worker_compute(
                worker,
                idle_seconds=0,
                kind="needs_input",
                target_run_id=str(run.get("run_id") or ""),
                target_started_at=str(run.get("started_at") or ""),
            )
        except Exception as exc:
            # The needs-input truth is already authoritative. Keep the exact
            # release fence for takeover/restart recovery instead of turning a
            # compute cleanup fault into a false failed run.
            logger.warning(
                "Failed to release needs-input GlassHive worker compute %s: %s",
                str(worker.get("worker_id") or ""),
                exc,
            )
            return None

    def recover_expired_compute_release_claims_once(self) -> list[dict[str, object]]:
        recovered: list[dict[str, object]] = []
        for worker_id in self.store.list_expired_compute_release_claim_worker_ids():
            worker = self.store.get_worker(worker_id)
            if not worker:
                continue
            try:
                kind = str(worker.get("compute_release_kind") or "idle").strip()
                target_run_id = str(
                    worker.get("compute_release_target_run_id") or ""
                ).strip()
                target_started_at = str(
                    worker.get("compute_release_target_started_at") or ""
                ).strip()
                target = self.store.get_run(target_run_id) if target_run_id else None
                if (
                    kind in {"pause_run", "resume_run", "interrupt_run", "steer_run"}
                    and target
                    and str(target.get("state") or "") in TERMINAL_RUN_STATES
                ):
                    replacement = None
                    if kind == "steer_run":
                        replacement = self.store.get_run(
                            str(worker.get("compute_release_replacement_run_id") or "")
                        )
                        if not replacement:
                            raise RuntimeErrorBase(
                                "Terminal-won steer has no exact fenced replacement"
                            )
                    claim = self.store.try_claim_worker_compute_release(
                        worker_id,
                        expected_updated_at=str(worker.get("updated_at") or ""),
                        expected_last_run_id=str(worker.get("last_run_id") or ""),
                        expected_state=str(worker.get("state") or ""),
                        expected_container_id=str(
                            worker.get("compute_release_container_id") or ""
                        ),
                        expected_session_fingerprint=str(
                            worker.get("compute_release_session_fingerprint") or ""
                        ),
                        owner=self._executor_id,
                        ttl_s=self._compute_release_claim_ttl_s(),
                        kind=kind,
                        target_run_id=target_run_id,
                        expected_target_started_at=target_started_at,
                        replacement_run=replacement,
                    )
                    if not claim:
                        raise RuntimeErrorBase(
                            "Terminal-won control claim generation changed"
                        )
                    if kind == "steer_run":
                        operation = self.store.finalize_worker_steer_claim(
                            worker_id,
                            str(claim["token"]),
                            int(claim["epoch"]),
                            target_run_id=target_run_id,
                            target_expected_state="running",
                            replacement_run_id=str(replacement["run_id"]),
                            replacement_instruction=str(replacement["instruction"]),
                            runtime_fields={},
                        )
                    else:
                        operation = self.store.finalize_worker_run_control_claim(
                            worker_id,
                            str(claim["token"]),
                            int(claim["epoch"]),
                            kind=kind,
                            target_run_id=target_run_id,
                            target_expected_states=("running",),
                            target_state="paused" if kind == "pause_run" else "interrupted",
                            worker_state="ready",
                            runtime_fields={},
                            release_lease=True,
                        )
                    if not operation:
                        raise RuntimeErrorBase(
                            "Terminal-won control could not clear its exact fence"
                        )
                    recovered.append(
                        {
                            "worker_id": worker_id,
                            "project_id": worker.get("project_id"),
                            "tenant_id": worker.get("tenant_id"),
                            "owner_id": worker.get("owner_id"),
                            "state": (operation.get("worker") or {}).get("state"),
                            "kind": kind,
                            "target_run_id": target_run_id,
                            "target_transitioned": False,
                            "terminal_won": True,
                        }
                    )
                    continue
                if kind == "stop_run":
                    item = self._recover_stop_run_claim(worker, target_run_id)
                elif kind == "steer_run":
                    replacement_run_id = str(
                        worker.get("compute_release_replacement_run_id") or ""
                    ).strip()
                    replacement = self.store.get_run(replacement_run_id)
                    if (
                        not replacement
                        or str(replacement.get("worker_id") or "") != worker_id
                        or str(replacement.get("state") or "") != "queued"
                    ):
                        raise RuntimeErrorBase(
                            "Expired steer claim has no exact fenced replacement"
                        )
                    recovered_replacement = self.steer_worker(
                        worker_id,
                        "",
                        run_id=target_run_id,
                        _prepared_instruction=str(
                            replacement.get("instruction") or ""
                        ),
                        _replacement_run_id=replacement_run_id,
                    )
                    item = {
                        "worker_id": worker_id,
                        "project_id": worker.get("project_id"),
                        "tenant_id": worker.get("tenant_id"),
                        "owner_id": worker.get("owner_id"),
                        "state": (self.store.get_worker(worker_id) or {}).get(
                            "state"
                        ),
                        "kind": "steer_run",
                        "target_run_id": target_run_id,
                        "replacement_run_id": recovered_replacement.get("run_id"),
                        "target_transitioned": True,
                    }
                elif kind in {
                    "pause_run",
                    "resume_run",
                    "interrupt_run",
                    "pause_worker",
                    "resume_worker",
                }:
                    # Re-enter the same public control path so takeover must
                    # satisfy the persisted exact target/start identity and
                    # the adapter must prove (or idempotently re-prove) the
                    # requested runtime state before the durable CAS commits.
                    if kind == "pause_run":
                        updated = self.pause_worker(
                            worker_id, run_id=target_run_id
                        )
                    elif kind == "resume_run":
                        updated = self.resume_worker(
                            worker_id, run_id=target_run_id
                        )
                    elif kind == "interrupt_run":
                        updated = self.interrupt_worker(
                            worker_id, run_id=target_run_id
                        )
                    elif kind == "pause_worker":
                        updated = self._pause_worker_without_run(worker_id)
                    else:
                        updated = self._resume_worker_without_run(worker_id)
                    durable_target = self.store.get_run(target_run_id)
                    item = (
                        {
                            "worker_id": worker_id,
                            "project_id": worker.get("project_id"),
                            "tenant_id": worker.get("tenant_id"),
                            "owner_id": worker.get("owner_id"),
                            "state": updated.get("state"),
                            "kind": kind,
                            "target_run_id": target_run_id,
                            "target_transitioned": bool(
                                durable_target
                                and str(durable_target.get("state") or "")
                                != str(
                                    {
                                        "pause_run": "running",
                                        "resume_run": "paused",
                                        "interrupt_run": "running",
                                    }.get(kind, "")
                                )
                            ),
                        }
                        if updated
                        else None
                    )
                elif kind == "terminate_worker":
                    termination = self._execute_worker_termination_claim(worker)
                    updated = dict((termination or {}).get("worker") or {})
                    item = (
                        {
                            "worker_id": worker_id,
                            "project_id": worker.get("project_id"),
                            "tenant_id": worker.get("tenant_id"),
                            "owner_id": worker.get("owner_id"),
                            "state": updated.get("state"),
                            "kind": "terminate_worker",
                            "target_run_id": target_run_id,
                            "target_transitioned": True,
                        }
                        if updated and bool((termination or {}).get("target_transitioned"))
                        else None
                    )
                elif kind in {"idle", "needs_input", "paused", "max_duration"}:
                    target_error = ""
                    if kind == "max_duration":
                        target_error = (
                            "Run exceeded its configured maximum duration; compute was "
                            "stopped and workspace state was preserved."
                        )
                    item = self._release_worker_compute(
                        worker,
                        idle_seconds=0,
                        kind=kind,
                        target_run_id=target_run_id,
                        target_started_at=target_started_at,
                        target_error_text=target_error,
                    )
                else:
                    # Unknown/future control claims remain fenced.  Never turn
                    # an unrecognised exact operation into broad termination.
                    logger.error(
                        "Unsupported expired GlassHive lifecycle claim %s for worker %s; retaining fence",
                        kind,
                        worker_id,
                    )
                    item = None
                durable_worker = self.store.get_worker(worker_id) or {}
                if item and str(durable_worker.get("compute_release_token") or ""):
                    raise RuntimeErrorBase(
                        "Recovered control did not clear its exact lifecycle fence"
                    )
                if item:
                    recovered.append(item)
            except Exception as exc:
                logger.warning(
                    "Failed to recover compute release for GlassHive worker %s: %s",
                    worker_id,
                    exc,
                )
        return recovered

    def _lifecycle_reaper_enabled(self) -> bool:
        return (
            self._idle_terminate_after_s() > 0
            or self._paused_terminate_after_s() > 0
            or self._max_run_duration_s() > 0
            or self.store.has_compute_release_claims()
        )

    def _worker_idle_seconds(self, worker: dict) -> float:
        # Reconciliation and presentation bookkeeping may refresh the worker
        # row long after its last run became terminal.  The terminal run end is
        # the durable compute-idle boundary; using worker.updated_at alone can
        # postpone cleanup after every restart and strand unrelated capacity.
        raw = str(worker.get("updated_at") or "")
        last_run_id = str(worker.get("last_run_id") or "").strip()
        if last_run_id:
            last_run = self.store.get_run(last_run_id)
            if (
                last_run
                and str(last_run.get("state") or "") in TERMINAL_RUN_STATES
                and str(last_run.get("ended_at") or "").strip()
            ):
                raw = str(last_run["ended_at"])
        try:
            updated = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            return 0.0
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        return max(0.0, datetime.now(timezone.utc).timestamp() - updated.astimezone(timezone.utc).timestamp())

    def reap_needs_input_workers_once(self) -> list[dict[str, object]]:
        reaped: list[dict[str, object]] = []
        for worker in self.store.list_all_workers():
            worker_id = str(worker.get("worker_id") or "")
            if (
                not worker_id
                or str(worker.get("state") or "") != "needs_input"
                or worker.get("compute_released_at")
            ):
                continue
            nonterminal = self.store.list_nonterminal_runs_for_worker(worker_id)
            needs_input_runs = [
                run
                for run in nonterminal
                if str(run.get("state") or "") == "needs_input"
            ]
            executing = [
                run
                for run in nonterminal
                if str(run.get("state") or "") in {"running", "settling", "paused"}
            ]
            if len(needs_input_runs) != 1 or executing:
                continue
            item = self._release_needs_input_compute(worker, needs_input_runs[0])
            if item:
                reaped.append(item)
        return reaped

    def reap_idle_workers_once(self) -> list[dict[str, object]]:
        reaped = self.recover_expired_compute_release_claims_once()
        reaped.extend(self.reap_needs_input_workers_once())
        threshold = self._idle_terminate_after_s()
        if threshold <= 0:
            return reaped
        terminal_states = TERMINAL_RUN_STATES
        for worker in self.store.list_all_workers():
            worker_id = str(worker.get("worker_id") or "")
            if not worker_id or worker.get("state") in {"terminated", "paused", "running", "starting"}:
                continue
            if self.store.get_active_run(worker_id) or self.store.has_queued_runs(worker_id):
                continue
            idle_seconds = self._worker_idle_seconds(worker)
            if idle_seconds < threshold:
                continue
            try:
                item = self._release_worker_compute(
                    worker,
                    idle_seconds=idle_seconds,
                )
                if item:
                    reaped.append(item)
            except Exception as exc:
                logger.warning("Failed to reap idle GlassHive worker %s: %s", worker_id, exc)
        return reaped

    def reap_paused_workers_once(self) -> list[dict[str, object]]:
        threshold = self._paused_terminate_after_s()
        if threshold <= 0:
            return []
        reaped: list[dict[str, object]] = []
        for worker in self.store.list_all_workers():
            worker_id = str(worker.get("worker_id") or "")
            if not worker_id or worker.get("state") != "paused":
                continue
            if worker.get("compute_released_at"):
                continue
            nonterminal = self.store.list_nonterminal_runs_for_worker(worker_id)
            paused_runs = [
                run for run in nonterminal if str(run.get("state") or "") == "paused"
            ]
            disallowed = [
                run
                for run in nonterminal
                if str(run.get("state") or "")
                in {"running", "settling", "needs_input"}
            ]
            if disallowed or len(paused_runs) > 1:
                continue
            paused_target = paused_runs[0] if paused_runs else None
            idle_seconds = self._worker_idle_seconds(worker)
            if idle_seconds < threshold:
                continue
            try:
                item = self._release_worker_compute(
                    worker,
                    idle_seconds=idle_seconds,
                    kind="paused",
                    target_run_id=str((paused_target or {}).get("run_id") or ""),
                    target_started_at=str(
                        (paused_target or {}).get("started_at") or ""
                    ),
                )
                if item:
                    reaped.append(item)
            except Exception as exc:
                logger.warning("Failed to stop paused GlassHive worker compute %s: %s", worker_id, exc)
        return reaped

    def _invalidate_worker_processor(self, worker_id: str) -> None:
        with self._processors_lock:
            self._processor_generations[worker_id] = self._processor_generations.get(worker_id, 0) + 1
            self._active_processors.discard(worker_id)

    def _run_age_seconds(self, run: dict) -> float:
        raw = str(run.get("started_at") or run.get("queued_at") or "")
        try:
            started = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            logger.warning(
                "GlassHive run %s has an unparseable timestamp for max-duration reaping; treating it as expired.",
                run.get("run_id") or "",
            )
            return float("inf")
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        return max(0.0, datetime.now(timezone.utc).timestamp() - started.astimezone(timezone.utc).timestamp())

    def reap_expired_runs_once(self) -> list[dict[str, object]]:
        threshold = self._max_run_duration_s()
        if threshold <= 0:
            return []
        reaped: list[dict[str, object]] = []
        for run in self.store.list_runs_by_state("running"):
            run_id = str(run.get("run_id") or "")
            worker_id = str(run.get("worker_id") or "")
            if not run_id or not worker_id:
                continue
            age_seconds = self._run_age_seconds(run)
            if age_seconds < threshold:
                continue
            worker = self.store.get_worker(worker_id)
            if not worker:
                continue
            error_text = f"Run exceeded GLASSHIVE_MAX_RUN_DURATION_S={threshold}; compute was stopped and workspace state was preserved."
            try:
                self._invalidate_worker_processor(worker_id)
                item = self._release_worker_compute(
                    worker,
                    idle_seconds=(
                        float(threshold)
                        if age_seconds == float("inf")
                        else age_seconds
                    ),
                    kind="max_duration",
                    target_run_id=run_id,
                    target_started_at=str(run.get("started_at") or ""),
                    target_error_text=error_text,
                )
                if not item:
                    continue
                finalized = bool(item.get("target_transitioned"))
                if finalized:
                    self._emit_callback(
                        worker,
                        "run.cancelled",
                        run={**run, "state": "cancelled", "error_text": error_text},
                        message=error_text,
                    )
                reaped.append(
                    {
                        **item,
                        "run_id": run_id,
                        "run_age_seconds": (
                            threshold
                            if age_seconds == float("inf")
                            else int(age_seconds)
                        ),
                    }
                )
            except Exception as exc:
                logger.warning("Failed to stop expired GlassHive run %s for worker %s: %s", run_id, worker_id, exc)
        return reaped

    def _idle_reaper_loop(self) -> None:
        interval = self._idle_reaper_interval_s()
        while not self._shutdown_event.wait(interval):
            self.reap_idle_workers_once()
            self.reap_paused_workers_once()
            self.reap_expired_runs_once()

    def _scheduler_interval_s(self) -> int:
        return _bounded_int_env("GLASSHIVE_SCHEDULER_INTERVAL_S", 5, min_value=1, max_value=3600)

    def _scheduler_loop(self) -> None:
        interval = self._scheduler_interval_s()
        while not self._shutdown_event.is_set():
            self._scheduler_wake_event.clear()
            self._process_scheduler_cycle()
            if self._shutdown_event.is_set():
                return
            wait_s = self._safe_next_scheduler_wait_s(interval)
            if self._shutdown_event.is_set():
                return
            self._scheduler_wake_event.wait(wait_s)

    def _process_scheduler_cycle(self) -> None:
        for phase_name, phase in (
            ("scheduled runs", self.process_due_schedules_once),
            ("worker retries", self.process_due_worker_retries_once),
        ):
            if self._shutdown_event.is_set():
                return
            try:
                phase()
            except Exception:
                logger.exception("GlassHive scheduler phase failed: %s", phase_name)

    def _next_scheduler_wait_s(self, interval: float) -> float:
        retry_after = self.store.next_queued_retry_after()
        if not retry_after:
            return interval
        try:
            parsed = datetime.fromisoformat(str(retry_after).replace("Z", "+00:00"))
        except ValueError:
            return interval
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        delay_s = parsed.astimezone(timezone.utc).timestamp() - datetime.now(timezone.utc).timestamp()
        if delay_s <= 0:
            return 0.01
        return min(interval, max(0.01, delay_s))

    def _safe_next_scheduler_wait_s(self, interval: float) -> float:
        try:
            return self._next_scheduler_wait_s(interval)
        except Exception:
            logger.exception("GlassHive scheduler wait calculation failed")
            return interval

    def _retry_base_delay_s(self, failure_class: str) -> float:
        if failure_class in {"host_worker_busy", "host_capacity"}:
            return _bounded_float_env(
                "GLASSHIVE_HOST_BUSY_RETRY_BASE_DELAY_S",
                _bounded_float_env("GLASSHIVE_RETRY_BASE_DELAY_S", 5.0, min_value=0.1, max_value=3600.0),
                min_value=0.1,
                max_value=3600.0,
            )
        return _bounded_float_env("GLASSHIVE_RETRY_BASE_DELAY_S", 5.0, min_value=0.1, max_value=3600.0)

    def _retry_max_delay_s(self, failure_class: str) -> float:
        if failure_class in {"host_worker_busy", "host_capacity"}:
            return _bounded_float_env(
                "GLASSHIVE_HOST_BUSY_RETRY_MAX_DELAY_S",
                _bounded_float_env("GLASSHIVE_RETRY_MAX_DELAY_S", 300.0, min_value=0.1, max_value=86400.0),
                min_value=0.1,
                max_value=86400.0,
            )
        return _bounded_float_env("GLASSHIVE_RETRY_MAX_DELAY_S", 300.0, min_value=0.1, max_value=86400.0)

    def _retry_delay_s(self, failure_class: str, attempts: int) -> float:
        base = self._retry_base_delay_s(failure_class)
        max_delay = self._retry_max_delay_s(failure_class)
        exponent = min(max(0, attempts - 1), 8)
        return min(max_delay, base * (2**exponent))

    def _capacity_retry_max_attempts(self) -> int:
        return _bounded_int_env("GLASSHIVE_MAX_CAPACITY_RETRY_ATTEMPTS", 6, min_value=0, max_value=1000)

    def _runtime_capacity_error(self, worker: dict) -> RuntimeErrorBase | None:
        checker = getattr(self.runtime, "worker_capacity_error", None)
        if not callable(checker):
            return None
        error = checker(worker)
        if not error:
            return None
        if isinstance(error, RuntimeErrorBase):
            return error
        return RuntimeErrorBase(str(error))

    def _host_lease_ttl_s(self) -> float:
        return _bounded_float_env(
            "WPR_HOST_LEASE_TTL_S",
            30.0,
            min_value=5.0,
            max_value=3600.0,
        )

    def _host_runtime_family(self, worker: dict) -> str:
        profile = str(worker.get("profile") or worker.get("runtime") or "host").lower()
        if profile.startswith("codex"):
            return "codex"
        if profile.startswith("claude"):
            return "claude"
        return "openclaw"

    def _host_run_lane(self, worker: dict) -> str:
        return self._trusted_run_lane(worker)

    def _worker_with_host_lease(self, worker: dict, run_id: str) -> dict:
        if not run_id:
            return worker
        scoped_worker = {**worker, "_active_run_id": run_id}
        if str(worker.get("execution_mode") or "docker") != "host":
            return scoped_worker
        lease = self.store.get_active_host_run_lease_for_run(run_id)
        return {**scoped_worker, "_host_run_lease": lease} if lease else scoped_worker

    def _deferred_capability_authorization(self, worker: dict) -> dict[str, str] | None:
        bundle = self._bootstrap_bundle_for(worker) or {}
        raw = bundle.get("glasshive_capability_authorization")
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise BrokerAdmissionError(
                "broker_authorization_invalid",
                "The deferred capability authorization is invalid.",
            )
        if set(raw) != {
            "version",
            "status",
            "authorization_ref",
            "origin_ref",
            "max_expires_at",
            "scope_fingerprint",
        }:
            raise BrokerAdmissionError(
                "broker_authorization_invalid",
                "The deferred capability authorization is invalid.",
            )
        if (
            isinstance(raw.get("version"), bool)
            or raw.get("version") != 1
            or raw.get("status") != "pending_admission"
        ):
            raise BrokerAdmissionError(
                "broker_authorization_invalid",
                "The deferred capability authorization is invalid.",
            )
        normalized = {
            "authorization_ref": str(raw.get("authorization_ref") or "").strip(),
            "origin_ref": str(raw.get("origin_ref") or "").strip(),
            "max_expires_at": str(raw.get("max_expires_at") or "").strip(),
            "scope_fingerprint": str(raw.get("scope_fingerprint") or "").strip(),
        }
        if any(not value or len(value) > 512 for value in normalized.values()):
            raise BrokerAdmissionError(
                "broker_authorization_invalid",
                "The deferred capability authorization is invalid.",
            )
        env = bundle.get("env")
        if isinstance(env, dict) and str(
            env.get("GLASSHIVE_CAPABILITY_BROKER_TOKEN") or ""
        ).strip():
            raise BrokerAdmissionError(
                "broker_authorization_invalid",
                "A deferred capability authorization must not contain a bearer grant.",
            )
        try:
            maximum = datetime.fromisoformat(
                normalized["max_expires_at"].replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise BrokerAdmissionError(
                "broker_authorization_invalid",
                "The deferred capability authorization is invalid.",
            ) from exc
        if maximum.tzinfo is None or maximum <= datetime.now(timezone.utc):
            raise BrokerAdmissionError(
                "capability_authorization_horizon_expired",
                "The deferred capability authorization has expired.",
                needs_input=True,
            )
        return normalized

    def _run_local_admitted_worker(
        self,
        worker: dict,
        run: dict,
        *,
        authority_context: dict[str, str] | None = None,
    ) -> dict:
        bundle = self._bootstrap_bundle_for(worker) or {}
        launch_authority = bundle.get("viventium_launch_authority")
        automatic_clean_room = bool(
            str(bundle.get("execution_policy") or "").strip()
            == PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
            and isinstance(launch_authority, dict)
            and launch_authority.get("version") == 1
            and launch_authority.get("kind") == "conversation_orchestrator"
            and launch_authority.get("execution_mode") == "docker"
        )
        requirement = bundle.get("glasshive_capability_requirement")
        if isinstance(requirement, dict):
            try:
                requirement_version = int(requirement.get("version"))
            except (TypeError, ValueError):
                requirement_version = 0
            if (
                requirement_version == 1
                and requirement.get("required") is True
                and str(requirement.get("status") or "").strip() == "unavailable"
            ):
                raise BrokerAdmissionError(
                    "required_capability_unavailable",
                    "Required connected-account authorization is unavailable. Reauthorize or restore the protected capability to continue.",
                    needs_input=True,
                )
        authorization = self._deferred_capability_authorization(worker)
        if automatic_clean_room and authorization is None:
            raise BrokerAdmissionError(
                "capability_authorization_missing",
                "Connect or reauthorize the model account, then resume this work.",
                needs_input=True,
            )
        if authorization is None:
            return worker
        load_viventium_runtime_env(
            {
                "VIVENTIUM_GLASSHIVE_ADMISSION_URL",
                "VIVENTIUM_GLASSHIVE_ADMISSION_SECRET",
            }
        )
        admission_url = str(
            os.environ.get("VIVENTIUM_GLASSHIVE_ADMISSION_URL") or ""
        ).strip()
        admission_secret = str(
            os.environ.get("VIVENTIUM_GLASSHIVE_ADMISSION_SECRET") or ""
        ).strip()
        delegation = self.store.get_delegation_for_worker(
            str(worker.get("worker_id") or ""),
            tenant_id=str(worker.get("tenant_id") or "local"),
            owner_id=str(worker.get("owner_id") or ""),
        )
        if not delegation:
            raise BrokerAdmissionError(
                "broker_admission_binding_missing",
                "The deferred capability authorization is not bound to durable work.",
            )
        origin_ref = str(delegation.get("origin_ref") or "").strip()
        if not origin_ref or origin_ref != authorization["origin_ref"]:
            raise BrokerAdmissionError(
                "broker_admission_binding_mismatch",
                "The deferred capability authorization binding is invalid.",
            )
        container_generation_id = str(
            (authority_context or {}).get("container_generation_id") or ""
        ).strip()
        if not re.fullmatch(r"[a-f0-9]{64}", container_generation_id):
            raise BrokerAdmissionError(
                "broker_admission_generation_unavailable",
                "The exact mission container generation is unavailable.",
                retryable=True,
            )
        admission_body = {
            "authorizationRef": authorization["authorization_ref"],
            "originRef": origin_ref,
            "runId": str(run.get("run_id") or ""),
            "workRef": str(delegation.get("work_ref") or ""),
            "workerId": str(worker.get("worker_id") or ""),
            "containerGenerationId": container_generation_id,
        }
        grant = admit_capability_grant(
            admission_url,
            secret=admission_secret,
            body=admission_body,
            expected_scope_fingerprint=authorization["scope_fingerprint"],
            expected_max_expires_at=authorization["max_expires_at"],
            timeout_seconds=_bounded_float_env(
                "VIVENTIUM_GLASSHIVE_ADMISSION_TIMEOUT_S",
                5.0,
                min_value=0.1,
                max_value=30.0,
            ),
        )
        bundle = self._bootstrap_bundle_for(worker) or {}
        run_bundle = merge_bootstrap_bundle(
            bundle,
            {
                "glasshive_capability_broker": grant.broker_projection(),
                "env": {
                    "GLASSHIVE_CAPABILITY_BROKER_TOKEN": grant.grant_token,
                },
            },
        ) or {}
        revocation_binding = {
            **admission_body,
            "grantId": grant.grant_id,
        }
        revocation = self.store.enqueue_capability_grant_revocation(
            revocation_binding
        )
        # The bearer exists only on this in-memory run object. It never updates
        # workers.bootstrap_bundle_json or another durable row.
        return {
            **worker,
            "bootstrap_bundle_json": json.dumps(run_bundle, ensure_ascii=False),
            "_run_local_capability_binding": revocation_binding,
            "_run_local_capability_revocation_id": str(
                revocation.get("revocation_id") or ""
            ),
        }

    def _revoke_run_local_capability_grant(self, worker: dict) -> None:
        revocation_id = str(
            worker.get("_run_local_capability_revocation_id") or ""
        ).strip()
        if not revocation_id:
            return
        self.store.activate_capability_grant_revocation(revocation_id)
        self._replay_pending_capability_grant_revocations(
            revocation_id=revocation_id
        )

    def _host_resource_capacity_error(
        self,
        prospective_worker: dict | None = None,
        *,
        docker_cached_only: bool = False,
    ) -> HostCapacityError | None:
        leases = self.store.list_active_host_run_leases()
        host_leases: list[dict] = []
        active_docker_worker_ids: set[str] = set()
        unknown_lease_worker = False
        for lease in leases:
            lease_worker = self.store.get_worker(str(lease.get("worker_id") or ""))
            if not lease_worker:
                unknown_lease_worker = True
                continue
            if str(lease_worker.get("execution_mode") or "docker") == "docker":
                active_docker_worker_ids.add(str(lease_worker.get("worker_id") or ""))
            else:
                host_leases.append(lease)
        usage = host_resource_usage(host_leases)
        child_processes = usage.child_processes
        threads = usage.threads
        available_memory = usage.available_memory_bytes
        available_disk = usage.available_disk_bytes
        process_probe_ok = usage.process_probe_ok and not unknown_lease_worker
        memory_probe_ok = usage.memory_probe_ok
        disk_probe_ok = usage.disk_probe_ok
        prospective_docker = (
            isinstance(prospective_worker, dict)
            and str(prospective_worker.get("execution_mode") or "docker") == "docker"
        )
        if prospective_docker or active_docker_worker_ids:
            docker_probe = getattr(self.runtime, "isolated_resource_usage", None)
            try:
                if callable(docker_probe):
                    try:
                        docker_usage = docker_probe(cached_only=docker_cached_only)
                    except TypeError:
                        docker_usage = docker_probe()
                else:
                    docker_usage = None
            except Exception:
                logger.exception("Docker resource admission probe failed")
                docker_usage = None
            if not isinstance(docker_usage, dict):
                process_probe_ok = False
                memory_probe_ok = False
                disk_probe_ok = False
            else:
                try:
                    running_containers = int(
                        docker_usage.get("running_worker_containers") or 0
                    )
                    docker_child_processes = int(
                        docker_usage.get("child_processes") or 0
                    )
                    docker_threads = int(docker_usage.get("threads") or 0)
                    available_memory = min(
                        available_memory,
                        int(docker_usage.get("available_memory_bytes") or 0),
                    )
                    available_disk = min(
                        available_disk,
                        int(docker_usage.get("available_disk_bytes") or 0),
                    )
                except (TypeError, ValueError):
                    running_containers = 0
                    process_probe_ok = False
                    memory_probe_ok = False
                    disk_probe_ok = False
                process_probe_ok = process_probe_ok and bool(
                    docker_usage.get("process_probe_ok")
                )
                memory_probe_ok = memory_probe_ok and bool(
                    docker_usage.get("memory_probe_ok")
                )
                disk_probe_ok = disk_probe_ok and bool(
                    docker_usage.get("disk_probe_ok")
                )
                # Reserve a conservative envelope for accepted Docker leases
                # whose container has not started yet, plus this prospective
                # mission. Actual container usage replaces its reservation on
                # subsequent admissions.
                running_worker_ids_value = docker_usage.get("running_worker_ids")
                if running_worker_ids_value is None:
                    # Backward-compatible fail-safe for runtime adapters that
                    # have not yet published per-worker accounting evidence.
                    pending_containers = max(
                        0,
                        len(active_docker_worker_ids) - max(0, running_containers),
                    ) + (1 if prospective_docker else 0)
                    child_processes += docker_child_processes
                    threads += docker_threads
                else:
                    running_worker_ids = {
                        str(worker_id).strip()
                        for worker_id in running_worker_ids_value
                        if str(worker_id).strip()
                    }
                    if len(running_worker_ids) != max(0, running_containers):
                        process_probe_ok = False
                        memory_probe_ok = False
                        disk_probe_ok = False
                    worker_process_counts_value = docker_usage.get(
                        "worker_process_counts"
                    )
                    if isinstance(worker_process_counts_value, dict):
                        measured_worker_counts: dict[str, tuple[int, int]] = {}
                        try:
                            for worker_id, counts_value in worker_process_counts_value.items():
                                normalized_worker_id = str(worker_id or "").strip()
                                if not normalized_worker_id or not isinstance(
                                    counts_value, dict
                                ):
                                    raise ValueError("invalid worker process measurement")
                                measured_worker_counts[normalized_worker_id] = (
                                    int(counts_value.get("child_processes") or 0),
                                    int(counts_value.get("threads") or 0),
                                )
                            if (
                                set(measured_worker_counts) != running_worker_ids
                                or any(
                                    process_count < 0 or thread_count < 0
                                    for process_count, thread_count in measured_worker_counts.values()
                                )
                                or sum(
                                    process_count
                                    for process_count, _thread_count in measured_worker_counts.values()
                                )
                                != docker_child_processes
                                or sum(
                                    thread_count
                                    for _process_count, thread_count in measured_worker_counts.values()
                                )
                                != docker_threads
                            ):
                                raise ValueError("inconsistent worker process measurement")
                        except (TypeError, ValueError):
                            process_probe_ok = False
                            measured_worker_counts = {}
                        prospective_worker_id = str(
                            (prospective_worker or {}).get("worker_id") or ""
                        ).strip()
                        counted_worker_ids = active_docker_worker_ids & running_worker_ids
                        if prospective_docker and prospective_worker_id in running_worker_ids:
                            counted_worker_ids.add(prospective_worker_id)
                        child_processes += sum(
                            measured_worker_counts.get(worker_id, (0, 0))[0]
                            for worker_id in counted_worker_ids
                        )
                        threads += sum(
                            measured_worker_counts.get(worker_id, (0, 0))[1]
                            for worker_id in counted_worker_ids
                        )
                    else:
                        # Older runtime adapters expose only aggregate Docker
                        # usage, so retain the conservative legacy behavior.
                        child_processes += docker_child_processes
                        threads += docker_threads
                    pending_worker_ids = active_docker_worker_ids - running_worker_ids
                    prospective_worker_id = str(
                        (prospective_worker or {}).get("worker_id") or ""
                    ).strip()
                    if prospective_docker and (
                        not prospective_worker_id
                        or prospective_worker_id not in running_worker_ids
                    ):
                        pending_worker_ids.add(
                            prospective_worker_id or "__prospective_worker__"
                        )
                    pending_containers = len(pending_worker_ids)
                child_processes += pending_containers * _bounded_int_env(
                    "WPR_DOCKER_PROCESS_RESERVATION",
                    20,
                    min_value=1,
                    max_value=100000,
                )
                threads += pending_containers * _bounded_int_env(
                    "WPR_DOCKER_THREAD_RESERVATION",
                    512,
                    min_value=1,
                    max_value=1000000,
                )
                available_memory = max(
                    0,
                    available_memory
                    - pending_containers
                    * _bounded_int_env(
                        "WPR_DOCKER_MEMORY_RESERVATION_MB",
                        3072,
                        min_value=1,
                        max_value=1048576,
                    )
                    * 1024**2,
                )
                available_disk = max(
                    0,
                    available_disk
                    - pending_containers
                    * _bounded_int_env(
                        "WPR_DOCKER_DISK_RESERVATION_MB",
                        4096,
                        min_value=1,
                        max_value=1048576,
                    )
                    * 1024**2,
                )
        process_limit = _bounded_int_env(
            "WPR_HOST_MAX_CHILD_PROCESSES", 64, min_value=1, max_value=100000
        )
        thread_limit = _bounded_int_env(
            "WPR_HOST_MAX_THREADS", 2048, min_value=1, max_value=1000000
        )
        memory_headroom = _bounded_int_env(
            "WPR_HOST_MIN_AVAILABLE_MEMORY_MB",
            2048,
            min_value=0,
            max_value=1048576,
        ) * 1024**2
        disk_headroom = _bounded_int_env(
            "WPR_HOST_MIN_AVAILABLE_DISK_MB",
            4096,
            min_value=0,
            max_value=1048576,
        ) * 1024**2
        if not (
            process_probe_ok
            and memory_probe_ok
            and disk_probe_ok
        ):
            return HostCapacityError(
                "Host resource admission is waiting for a healthy resource probe.",
                capacity_class="resource_probe_unavailable",
            )
        if (
            child_processes >= process_limit
            or threads >= thread_limit
            or available_memory < memory_headroom
            or available_disk < disk_headroom
        ):
            return HostCapacityError(
                "Host resource headroom is below its configured admission guard.",
                capacity_class="resource_pressure",
            )
        return None

    def _host_mutation_scope(self, worker: dict) -> str:
        """Serialize potentially mutating host missions by a structural target identity.

        Host CLIs intentionally retain broad filesystem capabilities. Until a trusted
        caller supplies a target-repository identity, an unscoped mission therefore
        takes one conservative shared mutation lease instead of racing another host
        mission against an unknown checkout. User wording never changes this guard.
        """

        if (
            str(worker.get("execution_mode") or "docker") != "host"
            or self._host_run_lane(worker) != "mission"
        ):
            return ""
        # The current bootstrap is partially model-authored and carries no
        # authenticated target-repository provenance. Never allow its
        # host_mutation_scope/target_repository_root fields to buy a separate
        # lease. A future Core-owned target authority can replace this shared
        # scope after it has an independently verified project binding.
        return hashlib.sha256(b"host-mission:unscoped-mutation-target").hexdigest()

    def _acquire_host_run_lease(self, worker: dict, run: dict) -> dict | None:
        execution_mode = str(worker.get("execution_mode") or "docker")
        lane = self._host_run_lane(worker)
        if (
            execution_mode == "host"
            and lane == "mission"
            and isolated_parallel_policy_enabled()
        ):
            raise HostCapacityError(
                "Host-native mission admission is disabled by isolated Parallel policy.",
                capacity_class="isolated_parallel_policy",
            )
        # Docker isolates each mission's filesystem/process namespace, but all
        # containers still consume the same workstation memory, disk and
        # process budget. Apply the global guard before either substrate starts.
        # Admission consumes only the background-refreshed Docker snapshot. A
        # missing/stale snapshot queues fail-closed; it never cold-runs Docker
        # CLI probes on the durable-acceptance critical path.
        pressure = self._host_resource_capacity_error(
            worker, docker_cached_only=True
        )
        if pressure:
            raise pressure
        try:
            return self.store.acquire_host_run_lease(
                runtime_family=self._host_runtime_family(worker),
                lane=lane,
                tenant_id=str(worker.get("tenant_id") or "local"),
                owner_id=str(worker.get("owner_id") or ""),
                worker_id=str(worker.get("worker_id") or ""),
                run_id=str(run.get("run_id") or ""),
                executor_id=self._executor_id,
                conversation_limit=_bounded_int_env(
                    "WPR_HOST_CONVERSATION_SLOTS_PER_CLI",
                    2,
                    min_value=1,
                    max_value=64,
                ),
                mission_limit=_bounded_int_env(
                    "WPR_HOST_MISSION_SLOTS_PER_CLI", 3, min_value=1, max_value=64
                ),
                account_mission_limit=_bounded_int_env(
                    "WPR_HOST_ACCOUNT_ACTIVE_LIMIT", 4, min_value=1, max_value=256
                ),
                tenant_mission_limit=_bounded_int_env(
                    "WPR_HOST_TENANT_ACTIVE_LIMIT", 12, min_value=1, max_value=1024
                ),
                mutation_scope=(
                    self._host_mutation_scope(worker)
                    if execution_mode == "host"
                    else ""
                ),
                lease_ttl_s=self._host_lease_ttl_s(),
            )
        except HostRunLeaseCapacityError as exc:
            raise HostCapacityError(
                str(exc), capacity_class=exc.capacity_class
            ) from exc

    def _observe_host_process(self, payload: dict[str, object]) -> None:
        run_id = str(payload.get("run_id") or "")
        lease = self.store.get_active_host_run_lease_for_run(run_id)
        if not lease:
            return
        self.store.heartbeat_host_run_lease(
            str(lease["lease_id"]),
            executor_id=self._executor_id,
            pid=int(payload.get("pid") or 0) or None,
            process_group=int(payload.get("process_group") or 0) or None,
            process_start_identity=str(payload.get("process_start_identity") or ""),
            startup_identity_kind=str(payload.get("identity_kind") or ""),
            startup_container_id=str(payload.get("container_id") or ""),
            startup_session_id=str(payload.get("session_id") or ""),
            lease_ttl_s=self._host_lease_ttl_s(),
        )

    def _observe_run_start(self, payload: dict[str, object]) -> None:
        """Accept one exact durable runtime identity and release its launch flock."""

        run_id = str(payload.get("run_id") or "").strip()
        worker_id = str(payload.get("worker_id") or "").strip()
        with self._pending_run_starts_lock:
            pending = dict(self._pending_run_starts.get(run_id) or {})
        if not pending or worker_id != str(pending.get("worker_id") or ""):
            raise RunStartupRejectedError(
                "The exact run startup reservation is no longer active.",
                termination_confirmed=False,
            )
        pending_worker = pending.get("worker")
        if not isinstance(pending_worker, dict):
            raise RunStartupRejectedError(
                "The exact run startup reservation has no worker identity.",
                termination_confirmed=False,
            )
        identity_kind = str(payload.get("identity_kind") or "").strip().lower()
        if getattr(self.runtime, "requires_run_start_identity", True) is False:
            expected_identity_kind = "in_process"
        else:
            execution_mode = str(
                pending_worker.get("execution_mode") or "docker"
            ).strip().lower()
            expected_identity_kind = {
                "host": "host_process",
                "docker": "docker_session",
            }.get(execution_mode, "")
        if identity_kind != expected_identity_kind:
            raise RunStartupRejectedError(
                "The reported runtime identity grade does not match the reserved execution mode.",
                termination_confirmed=False,
            )
        result = self.store.confirm_host_run_start(
            worker_id=worker_id,
            run_id=run_id,
            run_started_at=str(pending.get("run_started_at") or ""),
            lease_id=str(pending.get("lease_id") or ""),
            startup_token=str(pending.get("startup_token") or ""),
            executor_id=self._executor_id,
            identity_kind=identity_kind,
            pid=int(payload.get("pid") or 0) or None,
            process_group=int(payload.get("process_group") or 0) or None,
            process_start_identity=str(
                payload.get("process_start_identity") or ""
            ),
            container_id=str(payload.get("container_id") or ""),
            session_id=str(payload.get("session_id") or ""),
            callback_record=pending.get("callback_record")
            if isinstance(pending.get("callback_record"), dict)
            else None,
        )
        if result is None:
            raise RunStartupRejectedError(
                "The exact run startup reservation changed before confirmation.",
                termination_confirmed=False,
            )
        with self._pending_run_starts_lock:
            current = self._pending_run_starts.get(run_id)
            if current is not None:
                current["confirmed"] = True
        guard = pending.get("guard")
        if isinstance(guard, _WorkerLifecycleGuard):
            guard.release()
        callback = result.get("callback")
        callbacks = pending.get("callbacks")
        worker = pending.get("worker")
        if (
            isinstance(callback, dict)
            and isinstance(callbacks, dict)
            and isinstance(worker, dict)
        ):
            # Network delivery happens only after the durable CAS and flock release.
            self.executor.submit(
                self._deliver_callback_record,
                dict(worker),
                callback,
                dict(callbacks),
            )

    def _confirm_in_process_run_start(self, pending: dict[str, object]) -> None:
        self._observe_run_start(
            {
                "worker_id": str(pending.get("worker_id") or ""),
                "run_id": str(pending.get("run_id") or ""),
                "identity_kind": "in_process",
                "pid": 0,
                "process_group": 0,
                "process_start_identity": "",
                "container_id": "",
                "session_id": "in-process",
            }
        )

    @staticmethod
    def _json_object(value: object) -> dict[str, object]:
        if isinstance(value, dict):
            return dict(value)
        try:
            decoded = json.loads(str(value or "{}"))
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}

    def _observe_native_event(self, observation: dict[str, object]) -> None:
        """Persist one prompt-free provider lifecycle projection for an exact run."""

        worker_id = str(observation.get("worker_id") or "").strip()
        run_id = str(observation.get("run_id") or "").strip()
        provider = str(observation.get("provider") or "").strip().lower()
        event = observation.get("event")
        if provider not in {"codex", "claude"} or not isinstance(event, dict):
            return
        event_type = str(event.get("event_type") or "").strip()
        payload = event.get("payload")
        if not event_type.startswith("provider.") or not isinstance(payload, dict):
            return
        run = self.store.get_run(run_id)
        if not run or str(run.get("worker_id") or "") != worker_id:
            return
        worker = self.store.get_worker(worker_id)
        if not worker:
            return

        capabilities = self._json_object(run.get("native_capabilities_json"))
        child_projection_observed = event_type.startswith("provider.child.") or (
            event_type == "provider.team.message"
        )
        capabilities.update(
            {
                "provider": provider,
                "providerStream": True,
            }
        )
        if child_projection_observed:
            capabilities["childProjection"] = True
        else:
            capabilities.setdefault("childProjection", False)
        existing_summary = self._json_object(run.get("native_child_summary_json"))
        projection = NativeTeamProjection.from_summary(existing_summary)
        if not projection.observable:
            projection = NativeTeamProjection(provider=provider, observable=True)
        projection.apply(event)

        updates: dict[str, object] = {
            "native_capabilities_json": json.dumps(capabilities, sort_keys=True),
            "native_child_summary_json": json.dumps(projection.summary() or {}, sort_keys=True),
        }
        if event_type == "provider.session.started":
            updates["native_session_id"] = str(payload.get("sessionId") or "")[:256]
        self.store.update_run(run_id, **updates)
        self.store.add_event(
            str(worker.get("project_id") or run.get("project_id") or ""),
            worker_id,
            run_id,
            event_type,
            "Native provider lifecycle updated",
            payload=payload,
        )

    def _native_child_reconcile_seconds(self) -> float:
        return _bounded_float_env(
            "GLASSHIVE_NATIVE_CHILD_RECONCILE_SECONDS",
            120.0,
            min_value=0.01,
            max_value=120.0,
        )

    def _settle_native_children(self, worker: dict, run: dict, output: str) -> str:
        """Hold terminal truth while a proven native child remains live.

        The root result is persisted before waiting. Unknown or unobservable
        provider schemas never enter settling. A missing child bookend is
        bounded by the configured window (120 seconds maximum) and becomes an
        explicit degraded projection rather than pinning the mission forever.
        """

        run_id = str(run.get("run_id") or "")
        durable = self.store.get_run(run_id) or run
        capabilities = self._json_object(durable.get("native_capabilities_json"))
        summary = self._json_object(durable.get("native_child_summary_json"))
        projection = NativeTeamProjection.from_summary(summary)
        current_summary = projection.summary()
        if (
            capabilities.get("childProjection") is not True
            or not current_summary
            or int(current_summary.get("activeCount") or 0) <= 0
        ):
            return str(durable.get("state") or "running")

        root_exited_at = datetime.now(timezone.utc)
        summary_with_root = {
            **current_summary,
            "rootExitedAt": root_exited_at.isoformat(),
            "degraded": False,
        }
        transitioned = self.store.transition_run_if_state(
            run_id,
            "running",
            "settling",
            output_text=output,
            native_child_summary_json=json.dumps(summary_with_root, sort_keys=True),
        )
        if not transitioned:
            return str((self.store.get_run(run_id) or {}).get("state") or "")
        self.store.add_event(
            str(worker.get("project_id") or run.get("project_id") or ""),
            str(worker.get("worker_id") or run.get("worker_id") or ""),
            run_id,
            "run.settling",
            "Root result is ready while native child reconciliation continues",
            payload={"activeChildCount": int(current_summary.get("activeCount") or 0)},
        )

        timeout_seconds = self._native_child_reconcile_seconds()
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline and not self._shutdown_event.is_set():
            latest = self.store.get_run(run_id) or {}
            if str(latest.get("state") or "") != "settling":
                return str(latest.get("state") or "")
            latest_summary = self._json_object(latest.get("native_child_summary_json"))
            latest_projection = NativeTeamProjection.from_summary(latest_summary)
            reduced = latest_projection.summary()
            if not reduced or int(reduced.get("activeCount") or 0) <= 0:
                return "settling"
            time.sleep(min(0.05, max(0.001, deadline - time.monotonic())))

        latest = self.store.get_run(run_id) or {}
        if str(latest.get("state") or "") != "settling":
            return str(latest.get("state") or "")
        latest_summary = self._json_object(latest.get("native_child_summary_json"))
        projection = NativeTeamProjection.from_summary(
            latest_summary,
            reconcile_seconds=max(1, int(timeout_seconds)),
        )
        decision = projection.settlement(
            root_exited_at=root_exited_at,
            now=root_exited_at + timedelta(seconds=max(1, int(timeout_seconds))),
        )
        if decision.state == "degraded":
            degraded_summary = {
                **(projection.summary() or {}),
                "rootExitedAt": root_exited_at.isoformat(),
                "degraded": True,
                "lostChildRefs": list(decision.lost_child_refs),
            }
            self.store.update_run(
                run_id,
                native_child_summary_json=json.dumps(degraded_summary, sort_keys=True),
            )
            self.store.add_event(
                str(worker.get("project_id") or run.get("project_id") or ""),
                str(worker.get("worker_id") or run.get("worker_id") or ""),
                run_id,
                "provider.child.reconciliation_lost",
                "Native child state became unknown after the bounded reconciliation window",
                payload={"lostChildRefs": list(decision.lost_child_refs)},
            )
        return "settling"

    def _release_host_run_lease(self, run_id: str, *, reason: str) -> None:
        lease = self.store.get_active_host_run_lease_for_run(run_id)
        worker = (
            self.store.get_worker(str(lease.get("worker_id") or ""))
            if lease
            else None
        )
        if lease and self._lease_is_fenced_by_lifecycle_claim(worker, lease):
            return
        if lease and str(lease.get("executor_id") or "") == self._executor_id:
            self.store.release_host_run_lease(
                str(lease["lease_id"]),
                executor_id=self._executor_id,
                reason=reason,
            )

    def _host_lease_heartbeat_loop(self) -> None:
        interval = max(1.0, min(10.0, self._host_lease_ttl_s() / 3))
        while not self._shutdown_event.wait(interval):
            self._heartbeat_host_run_leases_once()

    def _heartbeat_host_run_leases_once(self) -> None:
        """Renew only live-run leases; one store failure must not kill the daemon."""

        try:
            leases = self.store.list_active_host_run_leases()
        except Exception:
            logger.exception("Host lease heartbeat pass failed")
            return
        for lease in leases:
            if str(lease.get("executor_id") or "") != self._executor_id:
                continue
            try:
                lease_worker = self.store.get_worker(
                    str(lease.get("worker_id") or "")
                )
                if self._lease_is_fenced_by_lifecycle_claim(
                    lease_worker, lease
                ):
                    continue
                run = self.store.get_run(str(lease.get("run_id") or ""))
                if run is None or str(run.get("state") or "") in TERMINAL_RUN_STATES:
                    self.store.release_host_run_lease(
                        str(lease["lease_id"]),
                        executor_id=self._executor_id,
                        reason="run_terminal",
                    )
                    continue
                self.store.heartbeat_host_run_lease(
                    str(lease["lease_id"]),
                    executor_id=self._executor_id,
                    lease_ttl_s=self._host_lease_ttl_s(),
                )
            except Exception:
                logger.exception(
                    "Host lease heartbeat failed for lease %s",
                    str(lease.get("lease_id") or ""),
                )
        try:
            self.reconcile_host_run_leases(stale_after_s=self._host_lease_ttl_s())
        except Exception:
            logger.exception("Host lease heartbeat reconciliation failed")

    def _isolated_readiness_loop(self) -> None:
        refresher = getattr(
            self.runtime, "refresh_isolated_parallel_readiness", None
        )
        if not callable(refresher):
            return
        while not self._shutdown_event.is_set():
            try:
                refresher()
            except Exception:
                logger.exception("Background isolated runtime readiness probe failed")
            if self._shutdown_event.wait(10.0):
                return

    @staticmethod
    def _lease_is_fenced_by_lifecycle_claim(
        worker: dict | None, lease: dict
    ) -> bool:
        """Keep the exact runtime lease owned by an unfinished control."""

        if not worker or not str(worker.get("compute_release_token") or ""):
            return False
        return bool(
            str(worker.get("compute_release_target_run_id") or "")
            == str(lease.get("run_id") or "")
            and str(worker.get("compute_release_kind") or "")
            in {
                "paused",
                "max_duration",
                "pause_run",
                "resume_run",
                "interrupt_run",
                "steer_run",
                "stop_run",
                "terminate_worker",
            }
        )

    def reconcile_host_run_leases(self, *, stale_after_s: float | None = None) -> dict[str, int]:
        threshold = datetime.now(timezone.utc) - timedelta(
            seconds=(self._host_lease_ttl_s() if stale_after_s is None else max(0, stale_after_s))
        )
        result = {"renewed": 0, "released": 0, "unchanged": 0}
        identity_reader = getattr(self.runtime, "host_process_identity", None)
        absence_reader = getattr(self.runtime, "host_process_absence", None)
        for lease in self.store.list_stale_host_run_leases(
            heartbeat_before=threshold.isoformat()
        ):
            worker = self.store.get_worker(str(lease.get("worker_id") or ""))
            if self._lease_is_fenced_by_lifecycle_claim(worker, lease):
                result["unchanged"] += 1
                continue
            run = self.store.get_run(str(lease.get("run_id") or ""))
            if run is None or str(run.get("state") or "") in TERMINAL_RUN_STATES:
                released = self.store.release_host_run_lease(
                    str(lease["lease_id"]),
                    executor_id=None,
                    reason="run_terminal",
                )
                result["released" if released else "unchanged"] += 1
                continue
            if str(lease.get("startup_state") or "") == "reserved":
                outcome = self._reconcile_reserved_host_run_start(
                    lease,
                    worker=worker,
                    identity_reader=identity_reader,
                )
                result[outcome] += 1
                continue
            if str(lease.get("startup_state") or "") == "termination_unconfirmed":
                outcome = self._reconcile_termination_unconfirmed_host_run_start(
                    lease,
                    absence_reader=absence_reader,
                )
                result[outcome] += 1
                continue
            identity = (
                identity_reader(worker, str(lease.get("run_id") or ""))
                if worker and callable(identity_reader)
                else None
            )
            if identity and bool(identity.get("verified")):
                updated = self.store.heartbeat_host_run_lease(
                    str(lease["lease_id"]),
                    executor_id=None,
                    pid=int(identity.get("pid") or 0) or None,
                    process_group=int(identity.get("process_group") or 0) or None,
                    process_start_identity=str(identity.get("process_start_identity") or ""),
                    lease_ttl_s=self._host_lease_ttl_s(),
                    reconciled=True,
                )
                result["renewed" if updated else "unchanged"] += 1
            else:
                released = self.store.release_host_run_lease(
                    str(lease["lease_id"]),
                    executor_id=None,
                    reason="stale_owner_no_verified_process",
                )
                result["released" if released else "unchanged"] += 1
        return result

    def _reconcile_termination_unconfirmed_host_run_start(
        self,
        stale_lease: dict,
        *,
        absence_reader,
    ) -> str:
        """Retry exact cleanup until the fenced startup generation is proven absent."""

        worker_id = str(stale_lease.get("worker_id") or "")
        run_id = str(stale_lease.get("run_id") or "")
        try:
            guard = self._acquire_worker_lifecycle_guard(worker_id)
        except RuntimeErrorBase:
            return "unchanged"
        try:
            lease = self.store.get_host_run_lease(
                str(stale_lease.get("lease_id") or "")
            )
            worker = self.store.get_worker(worker_id)
            run = self.store.get_run(run_id)
            if (
                not lease
                or not worker
                or not run
                or str(lease.get("status") or "") != "active"
                or str(lease.get("startup_state") or "")
                != "termination_unconfirmed"
                or str(lease.get("startup_token") or "")
                != str(stale_lease.get("startup_token") or "")
                or str(run.get("state") or "") in TERMINAL_RUN_STATES
            ):
                return "unchanged"

            captured_identity = self._reserved_start_identity(lease)
            cleanup = getattr(
                self.runtime, "cleanup_unconfirmed_run_start", None
            )
            cleaned = False
            if captured_identity and callable(cleanup):
                try:
                    cleaned = cleanup(worker, run_id, captured_identity) is True
                except Exception:
                    cleaned = False

            confirmed_absent = False
            if not cleaned and callable(absence_reader):
                try:
                    confirmed_absent = absence_reader(worker, run_id) is True
                except Exception:
                    confirmed_absent = False
            if not cleaned and not confirmed_absent:
                return "unchanged"

            recovered = self.store.requeue_unconfirmed_host_run_start(
                worker_id=worker_id,
                run_id=run_id,
                lease_id=str(lease.get("lease_id") or ""),
                startup_token=str(lease.get("startup_token") or ""),
                retry_after=(
                    datetime.now(timezone.utc) + timedelta(seconds=1)
                ).isoformat(),
                error_text=(
                    "GlassHive proved the interrupted startup generation absent "
                    "and queued the exact run for retry."
                ),
            )
            if recovered is None:
                return "unchanged"
            self._scheduler_wake_event.set()
            return "released"
        finally:
            guard.release()

    @staticmethod
    def _reserved_start_identity(lease: dict) -> dict[str, object] | None:
        """Return only an identity durably published before startup confirmation."""

        kind = str(lease.get("startup_identity_kind") or "").strip()
        start_identity = str(lease.get("process_start_identity") or "").strip()
        session_id = str(lease.get("startup_session_id") or "").strip()
        container_id = str(lease.get("startup_container_id") or "").strip()
        try:
            pid = int(lease.get("pid") or 0)
            process_group = int(lease.get("process_group") or pid or 0)
        except (TypeError, ValueError):
            return None
        if kind == "host_process":
            valid = bool(
                pid > 0
                and start_identity.startswith("ps-lstart:")
                and session_id
                and not container_id
            )
        elif kind == "docker_session":
            valid = bool(
                pid > 0
                and container_id
                and session_id
                and start_identity.startswith(
                    f"docker:{container_id}:{session_id}:"
                )
            )
        else:
            valid = False
        if not valid:
            return None
        return {
            "identity_kind": kind,
            "pid": pid,
            "process_group": process_group or pid,
            "process_start_identity": start_identity,
            "container_id": container_id,
            "session_id": session_id,
        }

    @staticmethod
    def _restart_identity_matches_reserved(
        captured: dict[str, object], current: dict[str, object] | None
    ) -> bool:
        if not current or current.get("verified") is not True:
            return False
        fields = (
            "identity_kind",
            "pid",
            "process_group",
            "process_start_identity",
            "container_id",
            "session_id",
        )
        return all(
            str(current.get(field) or "") == str(captured.get(field) or "")
            for field in fields
        )

    @staticmethod
    def _restart_identity_candidate(
        current: dict[str, object] | None,
        *,
        run_id: str,
    ) -> dict[str, object] | None:
        """Validate a token-bound active-session generation without lease copy."""

        if not current or current.get("verified") is not True:
            return None
        kind = str(current.get("identity_kind") or "").strip()
        start_identity = str(
            current.get("process_start_identity") or ""
        ).strip()
        container_id = str(current.get("container_id") or "").strip()
        session_id = str(current.get("session_id") or "").strip()
        try:
            pid = int(current.get("pid") or 0)
            process_group = int(current.get("process_group") or pid or 0)
        except (TypeError, ValueError):
            return None
        if kind == "host_process":
            valid = bool(
                pid > 0
                and process_group > 0
                and session_id
                and not container_id
                and start_identity.startswith("ps-lstart:")
            )
        elif kind == "docker_session":
            valid = bool(
                pid > 0
                and process_group > 0
                and container_id
                and session_id
                and start_identity.startswith(
                    f"docker:{container_id}:{session_id}:{run_id}:"
                )
            )
        else:
            valid = False
        if not valid:
            return None
        return {
            "identity_kind": kind,
            "pid": pid,
            "process_group": process_group,
            "process_start_identity": start_identity,
            "container_id": container_id,
            "session_id": session_id,
        }

    @staticmethod
    def _restart_identity_has_startup_binding(
        lease: dict,
        current: dict[str, object] | None,
    ) -> bool:
        startup_token = str(lease.get("startup_token") or "")
        actual_digest = str(
            (current or {}).get("startup_token_digest") or ""
        ).strip()
        if not startup_token or not re.fullmatch(r"[0-9a-f]{64}", actual_digest):
            return False
        expected_digest = hashlib.sha256(startup_token.encode("utf-8")).hexdigest()
        return hmac.compare_digest(actual_digest, expected_digest)

    def _reconcile_reserved_host_run_start(
        self,
        stale_lease: dict,
        *,
        worker: dict | None,
        identity_reader,
    ) -> str:
        """Confirm or safely retire one restart-surviving startup reservation."""

        worker_id = str(stale_lease.get("worker_id") or "")
        run_id = str(stale_lease.get("run_id") or "")
        callback_delivery: tuple[dict, dict, dict] | None = None
        try:
            guard = self._acquire_worker_lifecycle_guard(worker_id)
        except RuntimeErrorBase:
            return "unchanged"
        try:
            lease = self.store.get_host_run_lease(
                str(stale_lease.get("lease_id") or "")
            )
            worker = self.store.get_worker(worker_id)
            run = self.store.get_run(run_id)
            if (
                not lease
                or not worker
                or not run
                or str(lease.get("status") or "") != "active"
                or str(lease.get("startup_state") or "") != "reserved"
                or str(lease.get("startup_token") or "")
                != str(stale_lease.get("startup_token") or "")
                or str(run.get("state") or "") in TERMINAL_RUN_STATES
            ):
                return "unchanged"

            # This is deliberately a fresh durable-session read under the same
            # cross-process lifecycle flock used by launch and compute release.
            current_identity = (
                identity_reader(worker, run_id)
                if callable(identity_reader)
                else None
            )
            captured_identity = self._reserved_start_identity(lease)
            current_candidate = self._restart_identity_candidate(
                current_identity,
                run_id=run_id,
            )
            token_bound = self._restart_identity_has_startup_binding(
                lease, current_identity
            )
            if captured_identity is None and token_bound:
                captured_identity = current_candidate
            if (
                token_bound
                and captured_identity
                and self._restart_identity_matches_reserved(
                captured_identity, current_identity
                )
            ):
                callback_record, callbacks = self._run_start_callback_record(
                    worker,
                    run,
                    str(lease.get("startup_token") or ""),
                )
                confirmed = self.store.confirm_host_run_start(
                    worker_id=worker_id,
                    run_id=run_id,
                    run_started_at=str(run.get("started_at") or ""),
                    lease_id=str(lease.get("lease_id") or ""),
                    startup_token=str(lease.get("startup_token") or ""),
                    executor_id=str(lease.get("executor_id") or ""),
                    identity_kind=str(current_identity.get("identity_kind") or ""),
                    pid=int(current_identity.get("pid") or 0) or None,
                    process_group=int(current_identity.get("process_group") or 0)
                    or None,
                    process_start_identity=str(
                        current_identity.get("process_start_identity") or ""
                    ),
                    container_id=str(current_identity.get("container_id") or ""),
                    session_id=str(current_identity.get("session_id") or ""),
                    callback_record=callback_record,
                )
                if confirmed is None:
                    return "unchanged"
                callback = confirmed.get("callback")
                if isinstance(callback, dict):
                    callback_delivery = (dict(worker), callback, callbacks)
                return "renewed"

            cleanup = getattr(
                self.runtime, "cleanup_unconfirmed_run_start", None
            )
            cleaned = bool(
                captured_identity
                and callable(cleanup)
                and cleanup(worker, run_id, captured_identity)
            )
            if cleaned:
                requeued = self.store.requeue_unconfirmed_host_run_start(
                    worker_id=worker_id,
                    run_id=run_id,
                    lease_id=str(lease.get("lease_id") or ""),
                    startup_token=str(lease.get("startup_token") or ""),
                    retry_after=(
                        datetime.now(timezone.utc) + timedelta(seconds=1)
                    ).isoformat(),
                    error_text=(
                        "GlassHive safely cleaned an interrupted startup generation "
                        "and queued the exact run for retry."
                    ),
                )
                if requeued is not None:
                    self._scheduler_wake_event.set()
                    return "released"

            self.store.mark_host_run_start_termination_unconfirmed(
                lease_id=str(lease.get("lease_id") or ""),
                run_id=run_id,
                executor_id=str(lease.get("executor_id") or ""),
                startup_token=str(lease.get("startup_token") or ""),
            )
            return "unchanged"
        except Exception:
            logger.exception(
                "Reserved run startup reconciliation failed",
                extra={"worker_id": worker_id, "run_id": run_id},
            )
            return "unchanged"
        finally:
            guard.release()
            if callback_delivery is not None:
                callback_worker, callback, callbacks = callback_delivery
                self.executor.submit(
                    self._deliver_callback_record,
                    callback_worker,
                    callback,
                    callbacks,
                )

    def _requeue_retryable_run(
        self,
        worker: dict,
        run: dict,
        exc: RuntimeErrorBase,
        *,
        failure_fields: dict[str, object] | None = None,
    ) -> dict | None:
        failure_fields = dict(failure_fields or {})
        if not failure_fields:
            failure_fields = classify_runtime_error(
                exc,
                runtime_name=str(worker.get("profile") or worker.get("runtime") or "worker"),
            ).as_store_fields()
        failure_class = str(failure_fields.get("failure_class") or "runtime_retryable")
        attempts = int(run.get("retry_attempts") or 0) + 1
        max_attempts = self._capacity_retry_max_attempts()
        indefinite_wait_classes = {
            "host_capacity",
            "host_worker_busy",
            "provider_rate_limited",
            "provider_quota_exhausted",
        }
        consume_retry_budget = failure_class not in indefinite_wait_classes
        if consume_retry_budget and attempts > max_attempts:
            message = (
                f"GlassHive stopped retrying this run because worker capacity stayed unavailable "
                f"after {max_attempts} retry attempts."
            )
            exhausted_fields = {
                **failure_fields,
                "failure_retryable": 0,
                "failure_user_message": message,
                "failure_recommended_recovery": (
                    "Reuse another available workspace, wait for capacity and explicitly continue this "
                    "workspace, or ask the operator to adjust capacity."
                ),
                "failure_diagnostic_summary": (
                    f"Capacity retry budget exhausted for {failure_class}: {str(exc)}"
                ),
            }
            failed_run = self.store.finalize_run_if_state(
                str(run["run_id"]),
                str(run.get("state") or "running"),
                state="failed",
                error_text=str(exc),
                **exhausted_fields,
            )
            if not failed_run:
                self._record_late_processor_terminal_ignored(
                    worker, run, "retry-exhaustion"
                )
                return None
            self.store.finalize_schedule_for_run(str(run["run_id"]), state="failed", last_error=message)
            self.store.update_worker_state(str(worker["worker_id"]), "ready", last_error=message)
            self.store.add_event(
                str(worker.get("project_id") or ""),
                str(worker["worker_id"]),
                str(run["run_id"]),
                "run.failed",
                message,
            )
            self._emit_callback(
                self.store.get_worker(str(worker["worker_id"])) or worker,
                "run.failed",
                run=failed_run,
                message=message,
            )
            return failed_run
        delay_s = self._retry_delay_s(failure_class, attempts)
        provider_retry_after_s = getattr(exc, "retry_after_s", None)
        if failure_class == "provider_rate_limited" and provider_retry_after_s is not None:
            authoritative_delay = max(
                0.1, min(float(provider_retry_after_s), 86_400.0)
            )
            delay_s = max(delay_s, authoritative_delay)
            # Stable per-run jitter prevents a provider reset stampede while
            # preserving Retry-After as a hard lower bound.
            jitter_seed = int(
                hashlib.sha256(
                    f"{run.get('run_id')}:{attempts}:provider-rate-limit".encode("utf-8")
                ).hexdigest()[:8],
                16,
            )
            jitter_ceiling = min(30.0, delay_s * 0.10)
            delay_s = min(
                86_400.0,
                delay_s + jitter_ceiling * (jitter_seed / 0xFFFFFFFF),
            )
        retry_after = (datetime.now(timezone.utc) + timedelta(seconds=delay_s)).isoformat()
        updated_run = self.store.requeue_run_for_retry(
            str(run["run_id"]),
            retry_after=retry_after,
            error_text=str(exc),
            last_retry_class=failure_class,
            consume_retry_budget=consume_retry_budget,
            **failure_fields,
        )
        self.store.update_worker_state(str(worker["worker_id"]), "ready", last_error="")
        message = str(failure_fields.get("failure_user_message") or "").strip() or (
            "The worker is waiting for host capacity and will retry."
        )
        event_message = f"{message} Retrying after {retry_after}."
        self.store.add_event(
            str(worker.get("project_id") or ""),
            str(worker["worker_id"]),
            str(run["run_id"]),
            "run.waiting_on_capacity",
            event_message,
        )
        self._emit_callback(
            self.store.get_worker(str(worker["worker_id"])) or worker,
            "run.waiting_on_capacity",
            run={**run, **(updated_run or {}), "state": "queued"},
            message=message,
        )
        self._scheduler_wake_event.set()
        return updated_run

    def _active_worker_states(self) -> set[str]:
        return {"created", "starting", "ready", "running", "resuming", "interrupting"}

    def _limit_env(self, name: str) -> int:
        return _bounded_int_env(name, 0, min_value=0, max_value=100000)

    def _quota_workspace_options(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        states: set[str] | None = None,
        exclude_states: set[str] | None = None,
    ) -> list[dict]:
        options: list[dict] = []
        for worker in self.store.list_worker_options(
            tenant_id=tenant_id,
            owner_id=owner_id,
            states=states,
            exclude_states=exclude_states,
            limit=5,
        ):
            options.append(
                {
                    "project_id": worker.get("project_id"),
                    "worker_id": worker.get("worker_id"),
                    "project_title": worker.get("project_title") or "",
                    "workspace_name": worker.get("name") or "",
                    "alias": worker.get("alias") or "",
                    "state": worker.get("state") or "",
                    "profile": worker.get("profile") or "",
                    "execution_mode": worker.get("execution_mode") or "",
                    "updated_at": worker.get("updated_at") or "",
                    "last_run_id": worker.get("last_run_id") or "",
                }
            )
        return options

    def _enforce_worker_limits(self, *, tenant_id: str, owner_id: str) -> None:
        active_states = self._active_worker_states()
        limits = [
            (
                "GLASSHIVE_MAX_ACTIVE_WORKERS_PER_USER",
                self.store.count_workers(tenant_id=tenant_id, owner_id=owner_id, states=active_states),
                "active workers for this user",
                active_states,
                None,
            ),
            (
                "GLASSHIVE_MAX_ACTIVE_WORKERS_PER_TENANT",
                self.store.count_workers(tenant_id=tenant_id, states=active_states),
                "active workers for this tenant",
                active_states,
                None,
            ),
            (
                "GLASSHIVE_MAX_WORKSPACES_PER_USER",
                self.store.count_workers(tenant_id=tenant_id, owner_id=owner_id, exclude_states={"terminated"}),
                "workspaces for this user",
                None,
                {"terminated"},
            ),
            (
                "GLASSHIVE_MAX_WORKSPACES_PER_TENANT",
                self.store.count_workers(tenant_id=tenant_id, exclude_states={"terminated"}),
                "workspaces for this tenant",
                None,
                {"terminated"},
            ),
        ]
        for env_name, current_count, label, option_states, option_exclude_states in limits:
            limit = self._limit_env(env_name)
            if limit and current_count >= limit:
                options = self._quota_workspace_options(
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                    states=option_states,
                    exclude_states=option_exclude_states,
                )
                raise GlassHiveQuotaExceededError(
                    f"GlassHive quota exceeded: {label} is limited by {env_name}={limit}",
                    env_name=env_name,
                    label=label,
                    limit=limit,
                    current_count=current_count,
                    available_workspace_options=options,
                )

    def _parse_run_at(
        self,
        *,
        run_at: str | None = None,
        schedule_text: str | None = None,
        delay_seconds: int | None = None,
    ) -> str:
        if delay_seconds is not None:
            return (datetime.now(timezone.utc) + timedelta(seconds=max(0, int(delay_seconds)))).isoformat()
        raw_run_at = str(run_at or "").strip()
        if raw_run_at:
            normalized = raw_run_at.replace("Z", "+00:00")
            try:
                parsed = datetime.fromisoformat(normalized)
            except ValueError as exc:
                raise ValueError("run_at must be an ISO datetime") from exc
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).isoformat()

        text = str(schedule_text or "").strip().lower()
        now = datetime.now(timezone.utc)
        match = re.search(r"\bin\s+(\d+)\s*(second|seconds|minute|minutes|hour|hours|day|days)\b", text)
        if match:
            value = int(match.group(1))
            unit = match.group(2)
            if unit.startswith("second"):
                delta = timedelta(seconds=value)
            elif unit.startswith("minute"):
                delta = timedelta(minutes=value)
            elif unit.startswith("hour"):
                delta = timedelta(hours=value)
            else:
                delta = timedelta(days=value)
            return (now + delta).isoformat()

        weekdays = {
            "monday": 0,
            "tuesday": 1,
            "wednesday": 2,
            "thursday": 3,
            "friday": 4,
            "saturday": 5,
            "sunday": 6,
        }
        for label, weekday in weekdays.items():
            if re.search(rf"\b{label}s?\b", text):
                days = (weekday - now.weekday()) % 7
                if days == 0:
                    days = 7
                return (now + timedelta(days=days)).replace(hour=9, minute=0, second=0, microsecond=0).isoformat()

        raise ValueError("schedule_text must be explicit, for example 'in 20 minutes', or run_at must be provided")

    def create_project(
        self,
        owner_id: str,
        title: str,
        goal: str,
        default_worker_profile: str,
        tenant_id: str = "local",
    ) -> dict:
        return self.store.create_project(owner_id, title, goal, default_worker_profile, tenant_id=tenant_id)

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
        execution_mode: str,
        workspace_root: str | None = None,
        bootstrap_profile: str | None = None,
        bootstrap_bundle: dict | None = None,
    ) -> dict:
        """Atomically reserve a durable project, worker, and first run."""

        committed = self.store.get_delegation_by_idempotency_key(
            tenant_id=tenant_id,
            owner_id=owner_id,
            idempotency_key=idempotency_key,
        )
        if committed is not None:
            if str(committed.get("request_digest") or "") != request_digest:
                raise DelegationIdempotencyConflictError(
                    "The delegation idempotency key was reused with a different request."
                )
            # The first transaction already durably accepted this exact request.
            # A lost-response replay must not be invalidated by a later profile,
            # runtime, policy, or capacity change. The scheduler owns recovery of
            # any accepted queued work after a crash.
            return {**committed, "idempotent_replay": True}

        launch_authority = (
            bootstrap_bundle.get("viventium_launch_authority")
            if isinstance(bootstrap_bundle, dict)
            else None
        )
        if launch_authority is not None:
            valid_launch_authority = (
                isinstance(launch_authority, dict)
                and set(launch_authority)
                == {"version", "kind", "execution_mode"}
                and launch_authority.get("version") == 1
                and not isinstance(launch_authority.get("version"), bool)
                and launch_authority.get("kind") == "conversation_orchestrator"
                and launch_authority.get("execution_mode") == "docker"
            )
            capabilities = self.orchestration_capabilities()
            if (
                not valid_launch_authority
                or str(execution_mode or "").strip().lower() != "docker"
                or not isolated_parallel_policy_enabled()
                or capabilities["isolatedParallelReady"] is not True
            ):
                raise ParallelExecutionIsolationError(
                    "Automatic Parallel work requires an isolated Docker/workstation runtime."
                )
            bootstrap_profile, bootstrap_bundle = derive_parallel_clean_room_bootstrap(
                bootstrap_profile,
                bootstrap_bundle,
            )
        self._ensure_execution_allowed(execution_mode)
        self._ensure_profile_allowed(profile)
        self._ensure_runtime_available(profile, execution_mode)
        model = self._resolve_worker_model(profile, execution_mode)
        with self._worker_create_lock:
            self._enforce_worker_limits(tenant_id=tenant_id, owner_id=owner_id)
            try:
                record = self.store.reserve_delegation(
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                    idempotency_key=idempotency_key,
                    request_digest=request_digest,
                    origin_ref=origin_ref,
                    title=title,
                    goal=goal,
                    instruction=instruction,
                    origin_surface=origin_surface,
                    worker_name=worker_name,
                    worker_role=worker_role,
                    profile=profile,
                    backend=self._legacy_backend_label(profile, execution_mode, ""),
                    runtime=self._initial_runtime_label(profile, execution_mode),
                    model=model,
                    execution_mode=execution_mode,
                    workspace_root=workspace_root,
                    bootstrap_profile=bootstrap_profile,
                    bootstrap_bundle=bootstrap_bundle,
                    require_isolated_parallel_ready=launch_authority is not None,
                )
            except IsolatedParallelAdmissionConflictError as exc:
                raise ParallelExecutionIsolationError(str(exc)) from exc
        if not bool(record.get("idempotent_replay")):
            worker = self.store.get_worker(str(record.get("worker_id") or ""))
            run = self.store.get_run(str(record.get("initial_run_id") or ""))
            if worker and run:
                self._emit_callback(worker, "run.queued", run=run, message=instruction)
        self.start_assigned_run(str(record.get("worker_id") or ""))
        return record

    def _apply_capability_reauthorization(
        self,
        worker: dict,
        refresh: dict[str, object],
    ) -> dict:
        """Persist only Core's safe, scope-preserving authorization horizon refresh."""

        bundle = self._bootstrap_bundle_for(worker) or {}
        authorization = bundle.get("glasshive_capability_authorization")
        invalid = RuntimeError("capability_reauthorization_invalid")
        if not isinstance(authorization, dict) or set(refresh) != {
            "version",
            "authorization_ref",
            "max_expires_at",
            "scope_fingerprint",
        }:
            raise invalid
        if isinstance(refresh.get("version"), bool) or refresh.get("version") != 1:
            raise invalid
        existing_ref = str(authorization.get("authorization_ref") or "")
        refreshed_ref = str(refresh.get("authorization_ref") or "")
        existing_scope = str(authorization.get("scope_fingerprint") or "")
        refreshed_scope = str(refresh.get("scope_fingerprint") or "")
        if (
            not existing_ref
            or not existing_scope
            or not hmac.compare_digest(existing_ref, refreshed_ref)
            or not hmac.compare_digest(existing_scope, refreshed_scope)
        ):
            raise invalid
        try:
            existing_max = datetime.fromisoformat(
                str(authorization.get("max_expires_at") or "").replace("Z", "+00:00")
            )
            refreshed_text = str(refresh.get("max_expires_at") or "")
            refreshed_max = datetime.fromisoformat(
                refreshed_text.replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise invalid from exc
        if existing_max.tzinfo is None or refreshed_max.tzinfo is None:
            raise invalid
        now = datetime.now(timezone.utc)
        existing_utc = existing_max.astimezone(timezone.utc)
        refreshed_utc = refreshed_max.astimezone(timezone.utc)
        if (
            refreshed_utc <= existing_utc
            or refreshed_utc <= now + timedelta(seconds=60)
            or refreshed_utc > now + timedelta(hours=24, seconds=60)
        ):
            raise invalid
        updated_authorization = {
            **authorization,
            "max_expires_at": refreshed_text,
        }
        updated_bundle = {
            **bundle,
            "glasshive_capability_authorization": updated_authorization,
        }
        updated = self.store.update_worker(
            str(worker["worker_id"]),
            bootstrap_bundle_json=json.dumps(updated_bundle, ensure_ascii=False),
        )
        self.store.add_event(
            str(worker.get("project_id") or ""),
            str(worker["worker_id"]),
            None,
            "capability.authorization_refreshed",
            "Connected capability authorization was explicitly refreshed",
        )
        return updated or worker

    def execute_active_work_action(
        self,
        delegation: dict,
        *,
        action: str,
        instruction: str = "",
        idempotency_key: str,
        capability_reauthorization: dict[str, object] | None = None,
        action_use_id: str = "",
    ) -> dict[str, object]:
        worker_id = str(delegation.get("worker_id") or "")
        project_id = str(delegation.get("project_id") or "")
        run_id = str(delegation.get("run_id") or delegation.get("current_run_id") or "")
        worker = self.require_worker(worker_id)
        # Re-read mission truth at execution time. The roster payload used to
        # render the action can race a completion, pause, or queued sibling.
        live_delegation = self.store.get_delegation(
            str(delegation.get("work_ref") or ""),
            tenant_id=str(delegation.get("tenant_id") or ""),
            owner_id=str(delegation.get("owner_id") or ""),
        )
        if not live_delegation:
            raise RuntimeError("active_work_not_found")
        run_id = str(live_delegation.get("run_id") or live_delegation.get("current_run_id") or run_id)
        if action_use_id:
            action_record = self.store.get_active_work_action(action_use_id) or {}
            bound_run_id = str(action_record.get("source_run_id") or "")
            if bound_run_id and bound_run_id != run_id:
                recovered = self.reconcile_active_work_action(
                    live_delegation,
                    action=action,
                    instruction=instruction,
                    idempotency_key=idempotency_key,
                    source_run_id=bound_run_id,
                    capability_reauthorization=capability_reauthorization,
                    action_use_id=action_use_id,
                )
                if recovered is not None:
                    return recovered
                raise RuntimeError("active_work_generation_changed")
        run = self.require_run(run_id)
        run_state = str(run.get("state") or "")
        public_state = self._active_work_service_state(live_delegation)
        allowed_actions = self._active_work_service_actions(live_delegation, public_state)
        if action not in allowed_actions:
            recovered = self.reconcile_active_work_action(
                live_delegation,
                action=action,
                instruction=instruction,
                idempotency_key=idempotency_key,
                source_run_id=(
                    str(action_record.get("source_run_id") or run_id)
                    if action_use_id
                    else run_id
                ),
                capability_reauthorization=capability_reauthorization,
                action_use_id=action_use_id,
            )
            if recovered is not None:
                return recovered
            raise RuntimeError("active_work_action_not_available")

        if capability_reauthorization is not None and not (
            action == "resume" and run_state == "needs_input"
        ):
            raise RuntimeError("capability_reauthorization_invalid")

        if action in {"queue", "message", "steer"}:
            clean_instruction = str(instruction or "").strip()
            if not clean_instruction:
                raise ValueError("active_work_instruction_required")
            effect_idempotency_key = self._active_work_effect_idempotency_key(
                str(delegation.get("work_ref") or ""),
                idempotency_key,
            )
            if action == "queue":
                created = self.assign_run(
                    worker_id,
                    clean_instruction,
                    event_type="run.followup_queued",
                    idempotency_key=effect_idempotency_key,
                    resume_paused_worker=False,
                )
            elif action == "message":
                # Current host adapters have no proven live-message primitive.
                # Queue at the next safe run boundary and report that truthfully.
                created = self.assign_run(
                    worker_id,
                    self._instruction_for_message(clean_instruction),
                    event_type="worker.message_queued",
                    idempotency_key=effect_idempotency_key,
                    resume_paused_worker=False,
                )
            else:
                created = self.steer_worker(
                    worker_id,
                    clean_instruction,
                    run_id=run_id,
                    idempotency_key=effect_idempotency_key,
                    action_use_id=action_use_id,
                )
                if str(created.get("_control_outcome") or "") == "terminal_won":
                    authoritative = dict(created.get("_control_run") or created)
                    authoritative_state = str(
                        authoritative.get("state") or "completed"
                    )
                    return {
                        "status": "accepted",
                        "state": (
                            "cancelled"
                            if authoritative_state == "interrupted"
                            else authoritative_state
                        ),
                        "run_id": str(authoritative.get("run_id") or run_id),
                        "confirmation_pending": False,
                        "control_outcome": "terminal_won",
                    }
            return {
                "status": "queued",
                "state": "queued",
                "run_id": str(created.get("run_id") or ""),
                "confirmation_pending": False,
                "delivery_mode": (
                    "queued_next_boundary" if action == "message" else "queued"
                ),
            }

        if action == "pause":
            if run_state not in {"queued", "running", "paused"}:
                raise RuntimeError("active_work_not_active")
            paused = self.pause_worker(
                worker_id, run_id=run_id, action_use_id=action_use_id
            )
            if str(paused.get("_control_outcome") or "") == "terminal_won":
                authoritative = dict(paused.get("_control_run") or {})
                authoritative_state = str(
                    authoritative.get("state") or "completed"
                )
                return {
                    "status": "accepted",
                    "state": (
                        "cancelled"
                        if authoritative_state == "interrupted"
                        else authoritative_state
                    ),
                    "run_id": str(authoritative.get("run_id") or run_id),
                    "confirmation_pending": False,
                    "control_outcome": "terminal_won",
                }
            return {
                "status": "accepted",
                "state": "paused",
                "run_id": run_id,
                "confirmation_pending": False,
                "worker": paused,
            }

        if action == "resume":
            if run_state == "needs_input":
                if capability_reauthorization is not None:
                    worker = self._apply_capability_reauthorization(
                        worker,
                        capability_reauthorization,
                    )
                if action_use_id:
                    resumed = self.store.resume_needs_input_active_work_action(
                        action_use_id,
                        worker_id=worker_id,
                        run_id=run_id,
                        executor_id=self._executor_id,
                    )
                    if not resumed:
                        raise RuntimeError("active_work_not_waiting_for_input")
                    self._replay_pending_lifecycle_effects()
                else:
                    resumed_run = self.store.transition_run_if_state(
                        run_id,
                        "needs_input",
                        "queued",
                        ended_at=None,
                        error_text="",
                        retry_after=None,
                    )
                    if not resumed_run:
                        raise RuntimeError("active_work_not_waiting_for_input")
                    self.store.update_worker_state(worker_id, "starting", last_error="")
                    self.store.add_event(
                        project_id,
                        worker_id,
                        run_id,
                        "run.authorization_resumed",
                        "Authorization attention cleared; exact run queued for re-admission",
                    )
                    self._emit_callback(
                        worker,
                        "run.queued",
                        run=resumed_run,
                        message="Authorization attention cleared; run queued for re-admission",
                    )
                self._ensure_worker_processor(worker_id)
                return {
                    "status": "queued",
                    "state": "queued",
                    "run_id": run_id,
                    "confirmation_pending": False,
                    "resume_mode": "authorization_re_admission",
                }
            if str(worker.get("state") or "") != "paused":
                raise RuntimeError("active_work_not_paused")
            resumed = self.resume_worker(
                worker_id, run_id=run_id, action_use_id=action_use_id
            )
            if str(resumed.get("_control_outcome") or "") == "terminal_won":
                authoritative = dict(resumed.get("_control_run") or {})
                authoritative_state = str(
                    authoritative.get("state") or "completed"
                )
                return {
                    "status": "accepted",
                    "state": (
                        "cancelled"
                        if authoritative_state == "interrupted"
                        else authoritative_state
                    ),
                    "run_id": str(authoritative.get("run_id") or run_id),
                    "confirmation_pending": False,
                    "control_outcome": "terminal_won",
                }
            durable_run = self.require_run(run_id)
            resumed_state = str(durable_run.get("state") or "queued")
            return {
                "status": "accepted",
                "state": resumed_state,
                "run_id": run_id,
                "confirmation_pending": False,
                "worker": resumed,
                "resume_mode": (
                    "provider_restart_same_run"
                    if resumed_state == "queued" and bool(run.get("started_at"))
                    else "in_place"
                    if resumed_state == "running"
                    else "queued_same_run"
                ),
            }

        if action == "stop":
            if run_state in {
                "queued",
                "running",
                "settling",
                "paused",
                "needs_input",
            }:
                stopped = self.stop_run(
                    worker_id, run_id, action_use_id=action_use_id
                )
                if not bool(stopped.get("accepted")) and not bool(
                    stopped.get("confirmation_pending")
                ):
                    raise RuntimeError("active_work_stop_not_accepted")
                stopped_run = stopped.get("run") if isinstance(stopped, dict) else None
                response = {
                    "status": "pending" if stopped.get("confirmation_pending") else "accepted",
                    "state": "stopping"
                    if stopped.get("confirmation_pending")
                    else str((stopped_run or {}).get("state") or "cancelled"),
                    "run_id": run_id,
                    "confirmation_pending": bool(stopped.get("confirmation_pending")),
                }
                if str(stopped.get("work_stop_outcome") or "") == "completion_won":
                    response["control_outcome"] = "terminal_won"
                return response
            if run_state == "cancelled":
                return {
                    "status": "accepted",
                    "state": "cancelled",
                    "run_id": run_id,
                    "confirmation_pending": False,
                }
            raise RuntimeError("active_work_not_active")

        if action == "retry":
            if run_state != "failed" or not bool(run.get("failure_retryable")):
                raise RuntimeError("active_work_not_retryable")
            if self.store.get_active_run(worker_id) or self.store.has_queued_runs(worker_id):
                raise RuntimeError("active_work_has_active_run")
            created = self.assign_run(
                worker_id,
                continuation_instruction(previous_run=run),
                event_type="run.queued",
                idempotency_key=self._active_work_effect_idempotency_key(
                    str(delegation.get("work_ref") or ""),
                    idempotency_key,
                ),
            )
            return {
                "status": "queued",
                "state": "queued",
                "run_id": str(created.get("run_id") or ""),
                "confirmation_pending": False,
            }

        if action == "dismiss":
            if run_state not in {"completed", "failed", "cancelled", "interrupted"}:
                raise RuntimeError("active_work_not_terminal")
            self.store.dismiss_delegation(
                str(delegation.get("work_ref") or ""),
                tenant_id=str(delegation.get("tenant_id") or ""),
                owner_id=str(delegation.get("owner_id") or ""),
            )
            return {
                "status": "accepted",
                "state": "cancelled" if run_state == "interrupted" else run_state,
                "run_id": run_id,
                "confirmation_pending": False,
            }

        raise ValueError("active_work_action_invalid")

    @staticmethod
    def _active_work_effect_idempotency_key(work_ref: str, idempotency_key: str) -> str:
        return f"active-work:{str(work_ref or '').strip()}:{str(idempotency_key or '').strip()}"

    @staticmethod
    def _idempotent_run_id(worker_id: str, idempotency_key: str) -> str:
        return "run_idem_" + hashlib.sha256(
            f"{worker_id}\0{idempotency_key}".encode("utf-8")
        ).hexdigest()[:32]

    def active_work_effect_run_id(
        self,
        delegation: dict,
        *,
        idempotency_key: str,
    ) -> str:
        return self._idempotent_run_id(
            str(delegation.get("worker_id") or ""),
            self._active_work_effect_idempotency_key(
                str(delegation.get("work_ref") or ""),
                idempotency_key,
            ),
        )

    def active_work_action_claim_is_pending(self, action_record: dict) -> bool:
        """Whether one receipt is still fenced by its exact bound control claim."""

        action_use_id = str(action_record.get("action_use_id") or "").strip()
        current_action = (
            self.store.get_active_work_action(action_use_id) if action_use_id else None
        ) or {}
        if (
            str(current_action.get("status") or "") != "pending"
            or str(current_action.get("executor_id") or "") != self._executor_id
        ):
            return False
        operation_id = str(
            current_action.get("lifecycle_operation_id") or ""
        ).strip()
        operation_kind = str(
            current_action.get("lifecycle_operation_kind") or ""
        ).strip()
        target_run_id = str(
            current_action.get("lifecycle_target_run_id") or ""
        ).strip()
        expected_kind = {
            "pause": "pause_run",
            "resume": "resume_run",
            "steer": "steer_run",
            "stop": "stop_run",
        }.get(str(current_action.get("action") or ""), "")
        source_run = self.store.get_run(
            str(current_action.get("source_run_id") or "")
        )
        worker = (
            self.store.get_worker(str(source_run.get("worker_id") or ""))
            if source_run
            else None
        ) or {}
        return bool(
            operation_id
            and operation_kind == expected_kind
            and target_run_id == str(current_action.get("source_run_id") or "")
            and str(worker.get("compute_release_token") or "").strip()
            and str(worker.get("compute_release_operation_id") or "")
            == operation_id
            and str(worker.get("compute_release_kind") or "") == operation_kind
            and str(worker.get("compute_release_target_run_id") or "")
            == target_run_id
        )

    @staticmethod
    def _active_work_service_state(record: dict) -> str:
        worker_state = str(record.get("worker_state") or "")
        run_state = str(record.get("run_state") or "")
        if worker_state == "stopping":
            return "stopping"
        if worker_state == "paused" and run_state in {
            "queued",
            "running",
            "settling",
            "paused",
        }:
            return "paused"
        if run_state == "queued" and worker_state == "created":
            return "accepted"
        if run_state == "queued" and worker_state in {"starting", "resuming"}:
            return "starting"
        if run_state == "interrupted":
            return "cancelled"
        if run_state in {
            "queued",
            "running",
            "settling",
            "paused",
            "needs_input",
            "completed",
            "failed",
            "cancelled",
        }:
            return run_state
        return "failed" if worker_state == "failed" else "queued"

    @staticmethod
    def _active_work_service_actions(record: dict, state: str) -> set[str]:
        if state in {"accepted", "queued", "starting", "running"}:
            return {"queue", "message", "steer", "pause", "stop"}
        if state == "settling":
            return {"queue", "message", "stop"}
        if state == "paused":
            return {"queue", "message", "resume", "stop"}
        if state == "needs_input":
            return {"queue", "message", "resume", "stop"}
        if state == "failed" and bool(record.get("run_failure_retryable")):
            return {"retry", "dismiss"}
        if state in {"completed", "failed", "cancelled"}:
            return {"dismiss"}
        return set()

    def reconcile_active_work_action(
        self,
        delegation: dict,
        *,
        action: str,
        instruction: str = "",
        idempotency_key: str,
        source_run_id: str,
        capability_reauthorization: dict[str, object] | None = None,
        action_use_id: str = "",
    ) -> dict[str, object] | None:
        """Recover an action receipt from its durable effect after a lost response."""

        worker_id = str(delegation.get("worker_id") or "")
        project_id = str(delegation.get("project_id") or "")
        tenant_id = str(delegation.get("tenant_id") or "")
        source_run = self.store.get_run(str(source_run_id or ""))
        if (
            not source_run
            or str(source_run.get("worker_id") or "") != worker_id
            or str(source_run.get("project_id") or "") != project_id
            or str(source_run.get("tenant_id") or "") != tenant_id
        ):
            return None

        action_record = (
            self.store.get_active_work_action(action_use_id) if action_use_id else None
        )
        action_operation_id = str(
            (action_record or {}).get("lifecycle_operation_id") or ""
        )
        action_operation_kind = str(
            (action_record or {}).get("lifecycle_operation_kind") or ""
        )
        action_operation_target = str(
            (action_record or {}).get("lifecycle_target_run_id") or ""
        )

        def action_proves(kind: str, event_type: str) -> bool:
            return bool(
                action_operation_id
                and action_operation_kind == kind
                and action_operation_target == str(source_run["run_id"])
                and self.store.has_lifecycle_operation_event(
                    operation_id=action_operation_id,
                    operation_kind=kind,
                    event_type=event_type,
                    worker_id=worker_id,
                    run_id=str(source_run["run_id"]),
                )
            )

        worker = self.store.get_worker(worker_id) or {}
        claim_kind = str(worker.get("compute_release_kind") or "")
        claim_target = str(worker.get("compute_release_target_run_id") or "")
        if claim_kind and claim_target == str(source_run.get("run_id") or ""):
            claim_action = {
                "pause_run": "pause",
                "resume_run": "resume",
                "steer_run": "steer",
                "stop_run": "stop",
            }.get(claim_kind)
            if claim_action == action:
                raw_expiry = str(worker.get("compute_release_expires_at") or "")
                try:
                    claim_expired = bool(raw_expiry) and datetime.fromisoformat(
                        raw_expiry
                    ) <= datetime.now(timezone.utc)
                except ValueError:
                    claim_expired = False
                if claim_expired:
                    self.recover_expired_compute_release_claims_once()
                    worker = self.store.get_worker(worker_id) or {}
                    source_run = self.store.get_run(
                        str(source_run["run_id"])
                    ) or source_run
                if str(worker.get("compute_release_token") or ""):
                    return None

        if action in {"queue", "message", "steer", "retry"}:
            effect_run_id = self.active_work_effect_run_id(
                delegation,
                idempotency_key=idempotency_key,
            )
            effect_run = self.store.get_run(effect_run_id)
            if (
                not effect_run
                or str(effect_run.get("worker_id") or "") != worker_id
                or str(effect_run.get("project_id") or "") != project_id
                or str(effect_run.get("tenant_id") or "") != tenant_id
            ):
                return None
            effect_state = str(effect_run.get("state") or "queued")
            source_state = str(source_run.get("state") or "")
            if (
                action == "steer"
                and source_state in TERMINAL_RUN_STATES
                and effect_state == "cancelled"
                and str(effect_run.get("error_text") or "")
                == STEER_REPLACEMENT_SUPPRESSED_ERROR
                and action_proves("steer_run", "control.terminal_won")
            ):
                current_delegation = self.store.get_delegation(
                    str(delegation.get("work_ref") or ""),
                    tenant_id=tenant_id,
                    owner_id=str(delegation.get("owner_id") or ""),
                )
                if (
                    not current_delegation
                    or str(current_delegation.get("current_run_id") or "")
                    != str(source_run["run_id"])
                ):
                    return None
                return {
                    "status": "accepted",
                    "state": (
                        "cancelled" if source_state == "interrupted" else source_state
                    ),
                    "run_id": str(source_run["run_id"]),
                    "replacement_run_id": effect_run_id,
                    "confirmation_pending": False,
                    "control_outcome": "terminal_won",
                    "advance_current_run": False,
                }
            if action == "steer" and not (
                source_state in {"interrupted", "cancelled"}
                and action_proves("steer_run", f"run.{source_state}")
            ):
                return None
            return {
                "status": "queued" if effect_state == "queued" else "accepted",
                "state": effect_state,
                "run_id": effect_run_id,
                "confirmation_pending": False,
                "delivery_mode": (
                    "queued_next_boundary" if action == "message" else "queued"
                ),
            }

        source_state = str(source_run.get("state") or "")
        if (
            action == "pause"
            and source_state in TERMINAL_RUN_STATES
            and action_proves("pause_run", "control.terminal_won")
        ):
            return {
                "status": "accepted",
                "state": "cancelled" if source_state == "interrupted" else source_state,
                "run_id": str(source_run["run_id"]),
                "confirmation_pending": False,
                "control_outcome": "terminal_won",
                "advance_current_run": False,
            }
        if action == "pause" and source_state == "paused":
            if not action_proves("pause_run", "run.paused"):
                return None
            return {
                "status": "accepted",
                "state": "paused",
                "run_id": str(source_run["run_id"]),
                "confirmation_pending": False,
            }
        if (
            action == "resume"
            and source_state in TERMINAL_RUN_STATES
            and action_proves("resume_run", "control.terminal_won")
        ):
            return {
                "status": "accepted",
                "state": "cancelled" if source_state == "interrupted" else source_state,
                "run_id": str(source_run["run_id"]),
                "confirmation_pending": False,
                "control_outcome": "terminal_won",
                "advance_current_run": False,
            }
        if action == "resume" and source_state in {"queued", "running"}:
            authorization_re_admitted = bool(
                str((action_record or {}).get("effect_phase") or "")
                == "authorization_re_admitted"
                and action_proves("resume_run", "run.authorization_resumed")
            )
            if not (
                authorization_re_admitted
                or action_proves("resume_run", "run.resumed")
            ):
                return None
            if authorization_re_admitted:
                self._replay_pending_lifecycle_effects()
                self._ensure_worker_processor(worker_id)
                resume_mode = "authorization_re_admission"
            elif capability_reauthorization is not None:
                resume_mode = "authorization_re_admission"
            elif source_state == "running":
                resume_mode = "in_place"
            elif bool(source_run.get("started_at")):
                resume_mode = "provider_restart_same_run"
            else:
                resume_mode = "queued_same_run"
            return {
                "status": "queued" if source_state == "queued" else "accepted",
                "state": source_state,
                "run_id": str(source_run["run_id"]),
                "confirmation_pending": False,
                "resume_mode": resume_mode,
            }
        if action == "stop":
            work_stop_outcome = str(worker.get("work_stop_outcome") or "")
            stop_settled = bool(
                action_operation_id
                and action_operation_kind == "stop_run"
                and action_operation_target == str(source_run["run_id"])
                and str(worker.get("work_stop_id") or "") == action_operation_id
                and worker.get("work_stop_settled_at")
                and work_stop_outcome in {"cancelled", "completion_won"}
                and not self.store.list_nonterminal_runs_for_worker(worker_id)
                and action_proves(
                    "stop_run",
                    "run.cancelled"
                    if work_stop_outcome == "cancelled"
                    else "work.stop_completion_won",
                )
            )
            if stop_settled:
                return {
                    "status": "accepted",
                    "state": (
                        "cancelled"
                        if work_stop_outcome == "cancelled"
                        else "cancelled"
                        if source_state == "interrupted"
                        else source_state
                    ),
                    "run_id": str(source_run["run_id"]),
                    "confirmation_pending": False,
                    "control_outcome": (
                        "terminal_won"
                        if work_stop_outcome == "completion_won"
                        else "work_stopped"
                    ),
                    "advance_current_run": False,
                }
            if (
                str(worker.get("state") or "") == "stopping"
                and str(worker.get("compute_release_kind") or "") == "stop_run"
                and str(worker.get("compute_release_scope") or "") == "work"
                and str(worker.get("compute_release_target_run_id") or "")
                == str(source_run["run_id"])
                and str(worker.get("compute_release_operation_id") or "")
                == action_operation_id
                and str(worker.get("work_stop_id") or "") == action_operation_id
            ):
                return {
                    "status": "pending",
                    "state": "stopping",
                    "run_id": str(source_run["run_id"]),
                    "confirmation_pending": True,
                }
            return None
        if action == "dismiss":
            refreshed = self.store.get_delegation(
                str(delegation.get("work_ref") or ""),
                tenant_id=tenant_id,
                owner_id=str(delegation.get("owner_id") or ""),
            )
            if refreshed and refreshed.get("dismissed_at"):
                return {
                    "status": "accepted",
                    "state": "cancelled" if source_state == "interrupted" else source_state,
                    "run_id": str(source_run["run_id"]),
                    "confirmation_pending": False,
                }
        return None

    def _initial_runtime_label(self, profile: str, execution_mode: str) -> str:
        return derive_legacy_backend_label(profile=profile, execution_mode=execution_mode, default="worker")

    def _legacy_backend_label(self, profile: str, execution_mode: str, requested_backend: str) -> str:
        runtime_label = self._initial_runtime_label(profile, execution_mode)
        return derive_legacy_backend_label(
            profile=profile,
            runtime=runtime_label,
            backend=requested_backend,
            execution_mode=execution_mode,
            default="worker",
        )

    def create_worker(
        self,
        project_id: str,
        owner_id: str,
        name: str,
        role: str,
        profile: str,
        backend: str,
        execution_mode: str = "docker",
        alias: str | None = None,
        workspace_root: str | None = None,
        bootstrap_profile: str | None = None,
        bootstrap_bundle: dict | None = None,
        tenant_id: str = "local",
        start_synchronously: bool = True,
        _trusted_run_lane: str = "mission",
    ) -> dict:
        clean_trusted_lane = (
            "conversation" if _trusted_run_lane == "conversation" else "mission"
        )
        if clean_trusted_lane == "conversation" and start_synchronously:
            raise ParallelExecutionIsolationError(
                "A conversation worker must be durably linked before its host runtime starts."
            )
        self._ensure_execution_allowed(
            execution_mode, trusted_run_lane=clean_trusted_lane
        )
        self._ensure_profile_allowed(profile)
        self._ensure_runtime_available(profile, execution_mode)
        model = self._resolve_worker_model(profile, execution_mode)
        with self._worker_create_lock:
            self._enforce_worker_limits(tenant_id=tenant_id or "local", owner_id=owner_id)
            worker = self.store.create_worker(
                project_id=project_id,
                tenant_id=tenant_id,
                owner_id=owner_id,
                name=name,
                role=role,
                profile=profile,
                backend=self._legacy_backend_label(profile, execution_mode, backend),
                runtime=self._initial_runtime_label(profile, execution_mode),
                model=model,
                execution_mode=execution_mode,
                alias=alias,
                workspace_root=workspace_root,
                bootstrap_profile=bootstrap_profile,
                bootstrap_bundle=bootstrap_bundle,
                # Provider session linkage promotes this prepared row to the
                # trusted conversation lane in the same database transaction.
                # A crash before that upsert leaves a non-runnable mission row.
                trusted_run_lane="mission",
            )
        if not start_synchronously:
            prepared = self.store.update_worker_state(worker["worker_id"], "paused", last_error="")
            self.store.add_event(
                project_id,
                worker["worker_id"],
                None,
                "worker.prepared",
                "Worker workspace is prepared and compute will start when a run is queued",
            )
            return prepared or self.store.get_worker(worker["worker_id"]) or worker
        starting_worker = self.store.begin_worker_compute_start(worker["worker_id"])
        if starting_worker is None:
            raise RuntimeErrorBase("Worker compute release is in progress; retry shortly")
        worker = starting_worker
        try:
            info = self.runtime.ensure_worker_ready(worker)
        except Exception as exc:
            updated = self.store.update_worker(
                worker["worker_id"],
                state="failed",
                last_error=str(exc),
            )
            self.store.add_event(project_id, worker["worker_id"], None, "worker.failed", str(exc))
            return updated or worker
        updated = self._apply_runtime_info(
            worker["worker_id"],
            info,
            state="ready",
            last_error="",
            compute_released_at=None,
        )
        self.store.add_event(project_id, worker["worker_id"], None, "worker.ready", f"Worker ready on {info.gateway_url}")
        self._emit_callback(updated or worker, "worker.ready", message="Worker ready")
        return updated or worker

    def activate_prepared_conversation_worker(self, worker_id: str) -> dict:
        worker = self.require_worker(worker_id)
        session = self.store.get_provider_session_by_worker(worker_id)
        if (
            not session
            or self._trusted_run_lane(worker) != "conversation"
            or str(session.get("tenant_id") or "")
            != str(worker.get("tenant_id") or "")
            or str(session.get("owner_id") or "")
            != str(worker.get("owner_id") or "")
        ):
            raise ParallelExecutionIsolationError(
                "The conversation worker does not have a durable provider-session binding."
            )
        self._ensure_execution_allowed(worker)
        return self._start_worker_again(
            worker,
            "worker.ready",
            "Conversation worker ready",
        )

    def find_or_create_worker(
        self,
        project_id: str,
        owner_id: str,
        name: str,
        role: str,
        profile: str,
        backend: str,
        alias: str,
        execution_mode: str = "docker",
        workspace_root: str | None = None,
        bootstrap_profile: str | None = None,
        bootstrap_bundle: dict | None = None,
        tenant_id: str = "local",
        start_synchronously: bool = True,
    ) -> dict:
        self._ensure_execution_allowed(execution_mode)
        self._ensure_profile_allowed(profile)
        self._ensure_runtime_available(profile, execution_mode)
        existing = self.store.find_worker_by_alias(
            project_id,
            owner_id,
            alias,
            execution_mode=execution_mode,
            tenant_id=tenant_id,
        )
        if existing and existing.get("state") != "terminated":
            updates: dict[str, object] = {
                "name": name,
                "role": role,
                "profile": profile,
                "backend": self._legacy_backend_label(profile, execution_mode, backend),
                "runtime": self._initial_runtime_label(profile, execution_mode),
            }
            if workspace_root is not None:
                updates["workspace_root"] = workspace_root
            if bootstrap_profile is not None:
                updates["bootstrap_profile"] = bootstrap_profile
            if bootstrap_bundle is not None:
                updates["bootstrap_bundle_json"] = json.dumps(
                    merge_bootstrap_bundle(self._bootstrap_bundle_for(existing), bootstrap_bundle)
                )
            existing = self.store.update_worker(existing["worker_id"], **updates) or existing
            self.store.add_event(
                project_id,
                existing["worker_id"],
                None,
                "worker.resumed_by_alias",
                f"Reusing worker alias {alias}",
            )
            existing = self._refresh_worker_model_for_profile(existing)
            self._emit_callback(existing, "worker.resumed_by_alias", message=f"Reusing worker alias {alias}")
            return existing
        return self.create_worker(
            project_id=project_id,
            tenant_id=tenant_id,
            owner_id=owner_id,
            name=name,
            role=role,
            profile=profile,
            backend=backend,
            execution_mode=execution_mode,
            alias=alias,
            workspace_root=workspace_root,
            bootstrap_profile=bootstrap_profile,
            bootstrap_bundle=bootstrap_bundle,
            start_synchronously=start_synchronously,
        )

    def update_worker_metadata(
        self,
        worker_id: str,
        *,
        favorite: bool | None = None,
        name: str | None = None,
    ) -> dict:
        worker = self.require_worker(worker_id)
        updates: dict[str, object] = {}
        if favorite is not None:
            updates["favorite"] = 1 if favorite else 0
        if name is not None:
            clean_name = str(name or "").strip()
            if not clean_name:
                raise ValueError("worker name cannot be empty")
            updates["name"] = clean_name[:160]
        if not updates:
            return worker
        updated = self.store.update_worker(worker_id, **updates) or worker
        self.store.add_event(worker["project_id"], worker_id, None, "worker.metadata_updated", "Worker metadata updated")
        return updated

    def schedule_run(
        self,
        worker_id: str,
        instruction: str,
        *,
        run_at: str | None = None,
        schedule_text: str | None = None,
        delay_seconds: int | None = None,
        runtime_bundle: dict | None = None,
    ) -> dict:
        worker = self.require_worker(worker_id)
        self._ensure_execution_allowed(worker)
        if runtime_bundle is not None:
            worker = self.store.update_worker(
                worker_id,
                bootstrap_bundle_json=json.dumps(
                    merge_bootstrap_bundle(self._bootstrap_bundle_for(worker), runtime_bundle)
                ),
            ) or worker
        resolved_run_at = self._parse_run_at(
            run_at=run_at,
            schedule_text=schedule_text,
            delay_seconds=delay_seconds,
        )
        schedule = self.store.create_scheduled_run(
            worker_id=worker_id,
            project_id=str(worker.get("project_id") or ""),
            tenant_id=str(worker.get("tenant_id") or "local"),
            owner_id=str(worker.get("owner_id") or ""),
            instruction=instruction,
            schedule_text=str(schedule_text or ""),
            run_at=resolved_run_at,
        )
        self.store.add_event(
            str(worker.get("project_id") or ""),
            worker_id,
            None,
            "schedule.created",
            f"Scheduled run for {resolved_run_at}",
        )
        self._emit_callback(worker, "schedule.created", message=f"Scheduled run for {resolved_run_at}")
        return schedule

    def process_due_schedules_once(self) -> list[dict[str, object]]:
        processed: list[dict[str, object]] = []
        due = self.store.list_due_schedules(datetime.now(timezone.utc).isoformat(), limit=50)
        for item in due:
            schedule_id = str(item.get("schedule_id") or "")
            claimed = self.store.claim_schedule(schedule_id)
            if not claimed:
                continue
            worker_id = str(claimed.get("worker_id") or "")
            try:
                run = self.assign_run(worker_id, str(claimed.get("instruction") or ""), event_type="schedule.queued")
                run_id = str(run.get("run_id") or "")
                updated = self.store.finalize_schedule(
                    schedule_id,
                    state="queued",
                    queued_run_id=run_id,
                )
                current_run = self.store.get_run(run_id) if run_id else None
                current_state = str((current_run or {}).get("state") or "")
                if current_state in {"completed", "failed", "cancelled", "interrupted", "paused"}:
                    schedule_state = current_state if current_state in {"completed", "cancelled"} else "failed"
                    updated = self.store.finalize_schedule_for_run(
                        run_id,
                        state=schedule_state,
                        last_error=str((current_run or {}).get("error_text") or ""),
                    ) or updated
                processed.append(updated or claimed)
            except Exception as exc:
                updated = self.store.finalize_schedule(schedule_id, state="failed", last_error=str(exc))
                processed.append(updated or claimed)
        return processed

    def process_due_worker_retries_once(self, *, limit: int = 1000) -> list[str]:
        if self._shutdown_event.is_set():
            return []
        worker_ids = self.store.list_due_retry_worker_ids(limit=limit)
        dispatched: list[str] = []
        for worker_id in worker_ids:
            if self._shutdown_event.is_set():
                break
            self._ensure_worker_processor(worker_id)
            dispatched.append(worker_id)
        return dispatched

    def duplicate_worker(
        self,
        source_worker_id: str,
        project_id: str,
        owner_id: str,
        name: str,
        role: str,
    ) -> dict:
        source_worker = self.require_worker(source_worker_id)
        bootstrap_bundle = self._bootstrap_bundle_for(source_worker)
        profile = str(source_worker.get("profile") or "codex-cli")
        execution_mode = str(source_worker.get("execution_mode") or "docker")
        duplicated = self.create_worker(
            project_id=project_id,
            tenant_id=str(source_worker.get("tenant_id") or "local"),
            owner_id=owner_id,
            name=name,
            role=role,
            profile=profile,
            backend=self._legacy_backend_label(profile, execution_mode, str(source_worker.get("backend") or "")),
            execution_mode=execution_mode,
            alias=None,
            workspace_root=str(source_worker.get("workspace_root") or "") or None,
            bootstrap_profile=str(source_worker.get("bootstrap_profile") or "") or None,
            bootstrap_bundle=bootstrap_bundle,
        )
        try:
            self._copy_workspace_contents(source_worker, duplicated)
        except Exception as exc:
            self.store.update_worker(duplicated["worker_id"], state="failed", last_error=str(exc))
            self.store.add_event(
                project_id,
                duplicated["worker_id"],
                None,
                "worker.duplicate_failed",
                str(exc),
            )
            raise
        self.store.add_event(
            project_id,
            duplicated["worker_id"],
            None,
            "worker.duplicated",
            f"Workspace duplicated from {source_worker_id}",
        )
        return self.store.get_worker(duplicated["worker_id"]) or duplicated

    def assign_run(
        self,
        worker_id: str,
        instruction: str,
        event_type: str = "run.queued",
        runtime_bundle: dict | None = None,
        run_local_bundle: dict | None = None,
        start_processor: bool = True,
        idempotency_key: str | None = None,
        resume_paused_worker: bool = True,
    ) -> dict:
        normalized_key = str(idempotency_key or "").strip()
        while True:
            needs_resume = False
            with self._worker_compute_release_lock(worker_id):
                worker = self.require_worker(worker_id)
                self._ensure_execution_allowed(worker)
                self._ensure_runtime_available(
                    str(worker.get("profile") or ""),
                    str(worker.get("execution_mode") or "docker"),
                )
                paused_control_run = (
                    next(
                        (
                            candidate
                            for candidate in self.store.list_nonterminal_runs_for_worker(
                                worker_id
                            )
                            if str(candidate.get("state") or "") == "paused"
                        ),
                        None,
                    )
                    if worker["state"] == "paused"
                    else None
                )
                if (
                    worker["state"] == "paused"
                    and resume_paused_worker
                    and paused_control_run is not None
                ):
                    needs_resume = True
                else:
                    worker = self._refresh_worker_model_for_profile(worker)
                    if runtime_bundle is not None:
                        worker = self.store.update_worker(
                            worker_id,
                            bootstrap_bundle_json=json.dumps(
                                merge_bootstrap_bundle(
                                    self._bootstrap_bundle_for(worker), runtime_bundle
                                )
                            ),
                        ) or worker
                    created = True
                    if normalized_key:
                        run_id = self._idempotent_run_id(worker_id, normalized_key)
                        run, created = self.store.create_idempotent_run(
                            run_id=run_id,
                            worker_id=worker_id,
                            project_id=str(worker["project_id"]),
                            instruction=instruction,
                        )
                    else:
                        run = self.store.create_run(
                            worker_id,
                            worker["project_id"],
                            instruction,
                            state="queued",
                        )
                    if worker["state"] == "paused" and resume_paused_worker:
                        worker = (
                            self.store.update_worker_state(
                                worker_id, "starting", last_error=""
                            )
                            or worker
                        )
                        self.store.add_event(
                            str(worker.get("project_id") or ""),
                            worker_id,
                            None,
                            "worker.resumed",
                            "Idle paused worker queued for startup",
                        )
            if not needs_resume:
                break
            # Never recurse into the non-reentrant lifecycle flock. Resume and
            # its startup handshake own the next generation; only after it
            # succeeds do we retry the run reservation under a fresh guard.
            self.resume_worker(worker_id)
        if run_local_bundle is not None and str(run.get("state") or "") == "queued":
            with self._run_local_bundles_lock:
                self._run_local_bundles[str(run["run_id"])] = dict(run_local_bundle)
                self._run_local_bundles_lock.notify_all()
        if not created:
            if start_processor and str(run.get("state") or "") == "queued":
                self._ensure_worker_processor(worker_id)
            return run
        self.store.add_event(
            worker["project_id"], worker_id, run["run_id"], event_type, instruction
        )
        self._emit_callback(worker, event_type, run=run, message=instruction)
        if start_processor:
            self._ensure_worker_processor(worker_id)
        return run

    def discard_run_local_bundle(self, run_id: str) -> None:
        with self._run_local_bundles_lock:
            self._run_local_bundles.pop(str(run_id), None)

    def attach_run_local_bundle(self, run_id: str, bundle: dict | None) -> bool:
        """Refresh one queued run's invocation-local authority after a retry.

        The exact bearer remains memory-only. Only a still-queued durable run
        may receive it; a running attempt must finish or fail truthfully rather
        than having authority changed underneath the provider process.
        """

        if not bundle:
            return False
        run = self.store.get_run(str(run_id))
        if not run or str(run.get("state") or "") not in {
            "queued",
            "running",
            "needs_input",
        }:
            return False
        with self._run_local_bundles_lock:
            current = self.store.get_run(str(run_id))
            current_state = str((current or {}).get("state") or "")
            if not current or current_state not in {"queued", "running", "needs_input"}:
                return False
            if (
                current_state == "running"
                and str(run_id) not in self._run_local_grant_waiters
            ):
                return False
            if current_state == "needs_input":
                refreshed = self.store.transition_run_if_state(
                    str(run_id),
                    "needs_input",
                    "queued",
                    ended_at=None,
                    retry_after=None,
                    error_text="",
                    failure_class="",
                    failure_retryable=0,
                    failure_structured=0,
                    failure_user_message="",
                    failure_recommended_recovery="",
                    failure_diagnostic_summary="",
                )
                if not refreshed:
                    return False
            self._run_local_bundles[str(run_id)] = dict(bundle)
            self._run_local_bundles_lock.notify_all()
            if current_state == "queued":
                self.store.update_run(
                    str(run_id),
                    retry_after=None,
                    error_text="",
                    failure_class="",
                    failure_retryable=0,
                    failure_structured=0,
                    failure_user_message="",
                    failure_recommended_recovery="",
                    failure_diagnostic_summary="",
                )
        return True

    def has_run_local_bundle(self, run_id: str) -> bool:
        with self._run_local_bundles_lock:
            return str(run_id) in self._run_local_bundles

    def _requires_conversation_invocation_bearer(self, worker: dict) -> bool:
        bundle = self._bootstrap_bundle_for(worker) or {}
        broker = bundle.get("glasshive_capability_broker")
        return bool(
            self._trusted_run_lane(worker) == "conversation"
            and isinstance(broker, dict)
            and str(broker.get("authority_kind") or "").strip()
            == "conversation_orchestrator"
        )

    def _mark_run_local_grant_waiter(self, worker: dict, run_id: str) -> None:
        if not self._requires_conversation_invocation_bearer(worker):
            return
        with self._run_local_bundles_lock:
            self._run_local_grant_waiters.add(str(run_id))

    def _clear_run_local_grant_waiter(self, run_id: str) -> None:
        with self._run_local_bundles_lock:
            self._run_local_grant_waiters.discard(str(run_id))

    def _run_local_worker(
        self,
        worker: dict,
        run: dict,
        *,
        authority_context: dict[str, str] | None = None,
    ) -> dict:
        try:
            run_worker = self._run_local_admitted_worker(
                worker, run, authority_context=authority_context
            )
        except Exception:
            self._clear_run_local_grant_waiter(str(run["run_id"]))
            raise
        requires_invocation_bearer = self._requires_conversation_invocation_bearer(
            run_worker
        )
        run_id = str(run["run_id"])
        with self._run_local_bundles_lock:
            if requires_invocation_bearer:
                self._run_local_grant_waiters.add(run_id)
            transient = self._run_local_bundles.pop(run_id, None)
        transient_env = (
            transient.get("env")
            if isinstance(transient, dict) and isinstance(transient.get("env"), dict)
            else {}
        )
        has_invocation_bearer = bool(
            str(
                transient_env.get("GLASSHIVE_CAPABILITY_BROKER_TOKEN") or ""
            ).strip()
        )
        if requires_invocation_bearer and not has_invocation_bearer:
            deadline = time.monotonic() + 0.5
            with self._run_local_bundles_lock:
                try:
                    while not self._shutdown_event.is_set():
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        self._run_local_bundles_lock.wait(timeout=remaining)
                        transient = self._run_local_bundles.pop(run_id, None)
                        transient_env = (
                            transient.get("env")
                            if isinstance(transient, dict)
                            and isinstance(transient.get("env"), dict)
                            else {}
                        )
                        if str(
                            transient_env.get(
                                "GLASSHIVE_CAPABILITY_BROKER_TOKEN"
                            )
                            or ""
                        ).strip():
                            break
                finally:
                    self._run_local_grant_waiters.discard(run_id)
            has_invocation_bearer = bool(
                str(
                    transient_env.get("GLASSHIVE_CAPABILITY_BROKER_TOKEN") or ""
                ).strip()
            )
        elif requires_invocation_bearer:
            with self._run_local_bundles_lock:
                self._run_local_grant_waiters.discard(run_id)
        if requires_invocation_bearer and not has_invocation_bearer:
            raise BrokerAdmissionError(
                "conversation_capability_grant_required",
                "The conversation capability grant must be refreshed before this queued turn can start.",
                needs_input=True,
            )
        if not transient:
            return run_worker
        run_bundle = merge_bootstrap_bundle(
            self._bootstrap_bundle_for(run_worker), transient
        ) or {}
        return {
            **run_worker,
            "bootstrap_bundle_json": json.dumps(run_bundle, ensure_ascii=False),
        }

    def start_assigned_run(self, worker_id: str) -> None:
        """Start processing after an external owner has durably attached a queued run."""

        self._ensure_worker_processor(worker_id)

    def verify_action_capability(self, capability: str) -> dict[str, object]:
        unverified = unverified_run_action_claims(capability)
        worker_id = str(unverified["workerId"])
        run_id = str(unverified["runId"])
        worker = self.store.get_worker(worker_id)
        run = self.store.get_run(run_id)
        if not worker or not run:
            raise RunActionError(
                "capability_invalid",
                "The action capability is invalid.",
                status_code=401,
            )
        callbacks = self._callback_config_for(worker)
        secret = str(callbacks.get("hmac_secret") or callbacks.get("secret") or "")
        claims = verify_run_action_capability(capability, secret=secret)
        if (
            str(worker.get("project_id") or "") != str(claims["projectId"])
            or str(run.get("project_id") or "") != str(claims["projectId"])
            or str(run.get("worker_id") or "") != worker_id
            or str(worker.get("tenant_id") or "") != str(claims["tenantId"])
            or str(run.get("tenant_id") or "") != str(claims["tenantId"])
            or str(worker.get("owner_id") or "") != str(claims["ownerId"])
        ):
            raise RunActionError(
                "capability_scope_mismatch",
                "The action capability does not match this workspace run.",
                status_code=403,
            )
        return claims

    def execute_run_action(
        self,
        claims: dict[str, object],
        *,
        capability_id: str,
        action: str,
        project_id: str,
        worker_id: str,
        run_id: str,
        idempotency_key: str,
    ) -> dict[str, object]:
        request_scope = {
            "capabilityId": capability_id,
            "action": action,
            "projectId": project_id,
            "workerId": worker_id,
            "runId": run_id,
        }
        if any(str(claims.get(key) or "") != str(value or "") for key, value in request_scope.items()):
            raise RunActionError(
                "capability_scope_mismatch",
                "The action request does not match its capability.",
                status_code=403,
            )
        tenant_id = str(claims["tenantId"])
        owner_id = str(claims["ownerId"])
        worker = self.require_worker(worker_id)

        if action == "retry":
            self._ensure_execution_allowed(worker)
            self._ensure_runtime_available(
                str(worker.get("profile") or ""),
                str(worker.get("execution_mode") or "docker"),
            )
            source_run = self.require_run(run_id)
            instruction = continuation_instruction(previous_run=source_run)
            result = self.store.create_retry_run_action(
                capability_id=capability_id,
                idempotency_key=idempotency_key,
                project_id=project_id,
                worker_id=worker_id,
                source_run_id=run_id,
                tenant_id=tenant_id,
                owner_id=owner_id,
                instruction=instruction,
            )
            new_run = dict(result["run"])
            current_worker = self.store.get_worker(worker_id) or worker
            if current_worker.get("state") == "paused":
                self.store.update_worker_state(worker_id, "starting", last_error="")
                self.store.add_event(
                    project_id,
                    worker_id,
                    None,
                    "worker.resumed",
                    "Worker resume queued for the next run",
                )
            if not result["idempotent_replay"]:
                self.store.add_event(project_id, worker_id, new_run["run_id"], "run.queued", instruction)
                self._emit_callback(
                    self.store.get_worker(worker_id) or worker,
                    "run.queued",
                    run=new_run,
                    message=instruction,
                )
            self._ensure_worker_processor(worker_id)
            return {
                "version": 1,
                "status": "queued",
                "action": "retry",
                "projectId": project_id,
                "workerId": worker_id,
                "sourceRunId": run_id,
                "newRun": {
                    "projectId": project_id,
                    "workerId": worker_id,
                    "runId": str(new_run["run_id"]),
                },
                "confirmationPending": False,
                "idempotentReplay": bool(result["idempotent_replay"]),
            }

        if action != "cancel":
            raise RunActionError("action_invalid", "The requested action is invalid.", status_code=400)
        result = self.store.reserve_cancel_run_action(
            capability_id=capability_id,
            idempotency_key=idempotency_key,
            project_id=project_id,
            worker_id=worker_id,
            source_run_id=run_id,
            tenant_id=tenant_id,
            owner_id=owner_id,
        )
        if result["should_execute"]:
            try:
                stop_result = self.stop_run(worker_id, run_id)
            except Exception as exc:
                self.store.update_run_action_result(
                    capability_id,
                    status="failed",
                    result_code="owner_interrupt_failed",
                )
                raise RunActionError(
                    "cancellation_not_accepted",
                    "The workspace did not accept the cancellation request.",
                    status_code=503,
                ) from exc
            if stop_result.get("termination_error"):
                self.store.update_run_action_result(
                    capability_id,
                    status="failed",
                    result_code="owner_interrupt_failed",
                )
                raise RunActionError(
                    "cancellation_not_accepted",
                    "The workspace did not accept the cancellation request.",
                    status_code=503,
                )
            if bool(stop_result.get("confirmation_pending")):
                return {
                    "version": 1,
                    "status": "pending",
                    "action": "cancel",
                    "projectId": project_id,
                    "workerId": worker_id,
                    "sourceRunId": run_id,
                    "newRun": None,
                    "confirmationPending": True,
                    "idempotentReplay": bool(result["idempotent_replay"]),
                }
            settled = self.store.get_run(run_id)
            settled_state = str((settled or {}).get("state") or "")
            if settled_state != "cancelled":
                result_code = "run_already_completed" if settled_state == "completed" else "run_not_active"
                self.store.update_run_action_result(
                    capability_id,
                    status="conflict",
                    result_code=result_code,
                )
                raise RunActionError(
                    result_code,
                    "The run completed before cancellation could be accepted."
                    if settled_state == "completed"
                    else "The run is no longer active.",
                    status_code=409,
                    details={"state": settled_state},
                )
            self.store.update_run_action_result(
                capability_id,
                status="accepted",
                result_code="cancellation_confirmed",
            )
        action_record = self.store.get_run_action(capability_id) or result["action"]
        accepted = str(action_record.get("status") or "") == "accepted"
        return {
            "version": 1,
            "status": "accepted" if accepted else "pending",
            "action": "cancel",
            "projectId": project_id,
            "workerId": worker_id,
            "sourceRunId": run_id,
            "newRun": None,
            "confirmationPending": True,
            "idempotentReplay": bool(result["idempotent_replay"]),
        }

    def record_launch_failed(self, worker_id: str, reason: str) -> dict:
        worker = self.require_worker(worker_id)
        self.store.cancel_pending_runs(worker_id, error_text=reason, state="failed")
        updated = self.store.update_worker(worker_id, state="failed", last_error=reason)
        self.store.add_event(worker["project_id"], worker_id, None, "worker.launch_failed", reason)
        return updated or worker

    def send_message(self, worker_id: str, message: str) -> dict:
        instruction = self._instruction_for_message(message)
        return self.assign_run(worker_id, instruction, event_type="worker.message")

    def steer_worker(
        self,
        worker_id: str,
        message: str,
        *,
        run_id: str = "",
        idempotency_key: str | None = None,
        action_use_id: str = "",
        _prepared_instruction: str = "",
        _replacement_run_id: str = "",
    ) -> dict:
        instruction = str(_prepared_instruction or "") or self._instruction_for_steer(
            message
        )
        normalized_key = str(idempotency_key or uuid.uuid4().hex).strip()
        replacement_run_id = str(_replacement_run_id or "") or self._idempotent_run_id(
            worker_id, normalized_key
        )
        existing = self.store.get_run(replacement_run_id)
        pending_worker = self.store.get_worker(worker_id) or {}
        pending_same_steer = bool(
            str(pending_worker.get("compute_release_kind") or "") == "steer_run"
            and str(
                pending_worker.get("compute_release_replacement_run_id") or ""
            )
            == replacement_run_id
        )
        if existing:
            if (
                str(existing.get("worker_id") or "") != worker_id
                or str(existing.get("instruction") or "") != instruction
            ):
                raise ValueError(
                    "GlassHive idempotency key was reused with a different steer"
                )
            if not pending_same_steer:
                return existing

        with self._worker_compute_release_lock(worker_id):
            worker = self.require_worker(worker_id)
            self._ensure_execution_allowed(worker)
            persisted_target_run_id = str(
                worker.get("compute_release_target_run_id") or ""
            )
            target = (
                self.require_run(run_id)
                if str(run_id or "").strip()
                else self.require_run(persisted_target_run_id)
                if pending_same_steer and persisted_target_run_id
                else self.store.get_active_run(worker_id)
                or self.store.get_controllable_run(worker_id)
            )
            if (
                not target
                or str(target.get("worker_id") or "") != worker_id
                or str(target.get("state") or "")
                not in {"queued", "running", "settling"}
            ):
                raise RuntimeErrorBase("No exact active run is available to steer")
            target_state = str(target.get("state") or "")
            target_run_id = str(target["run_id"])
            runtime_worker = worker
            if target_state != "queued":
                runtime_worker = self._require_confirmed_host_control_identity(
                    worker, target_run_id
                )
            queued_at = str((existing or {}).get("queued_at") or utc_now())
            replacement_data: dict[str, object] = {
                "run_id": replacement_run_id,
                "worker_id": worker_id,
                "project_id": str(worker["project_id"]),
                "tenant_id": str(worker.get("tenant_id") or "local"),
                "instruction": instruction,
                "state": "queued",
                "queued_at": queued_at,
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
            claim = self._claim_exact_run_control(
                worker,
                target,
                kind="steer_run",
                replacement_run=replacement_data,
                action_use_id=action_use_id,
            )
            claimed_worker = dict(claim.get("worker") or worker)
            token = str(claim["token"])
            epoch = int(claim["epoch"])
            info = None
            runtime_already_confirmed = self.store.worker_control_runtime_proof_matches(
                claimed_worker
            )
            if target_state != "queued" and not runtime_already_confirmed:
                if str(worker.get("execution_mode") or "docker") != "host":
                    runtime_worker = self._worker_with_host_lease(
                        claimed_worker, target_run_id
                    )
                else:
                    runtime_worker = {**runtime_worker, **claimed_worker}
                runtime_worker = self._require_claimed_container_generation(
                    runtime_worker
                )
                try:
                    info = self.runtime.interrupt_worker(
                        runtime_worker, run_id=target_run_id
                    )
                except TypeError as exc:
                    if "run_id" not in str(exc):
                        raise
                    info = self.runtime.interrupt_worker(runtime_worker)
                if not self._runtime_control_info_is_confirmed(info):
                    raise RuntimeErrorBase(
                        "Runtime steer did not confirm the exact process stopped"
                    )
                if not self.store.confirm_worker_control_runtime_effect(
                    worker_id,
                    token,
                    epoch,
                    kind="steer_run",
                    target_run_id=target_run_id,
                ):
                    raise RuntimeErrorBase(
                        "Steer runtime proof lost durable lifecycle ownership"
                    )
                self._invalidate_worker_processor(worker_id)
            operation = self.store.finalize_worker_steer_claim(
                worker_id,
                token,
                epoch,
                target_run_id=target_run_id,
                target_expected_state=target_state,
                replacement_run_id=replacement_run_id,
                replacement_instruction=instruction,
                runtime_fields=(
                    self._runtime_info_fields(worker_id, info, last_error="")
                    if info is not None
                    else {}
                ),
            )
            if not operation:
                raise RuntimeErrorBase(
                    "Steer lost the exact run lifecycle generation before replacement"
                )
            target_run = dict(operation.get("target_run") or target)
            replacement = dict(operation.get("replacement_run") or {})
            if action_use_id:
                self.store.checkpoint_active_work_action(
                    action_use_id,
                    "source_interrupted",
                    executor_id=self._executor_id,
                )
        self._replay_pending_lifecycle_effects()
        if bool(operation.get("terminal_won")):
            return {
                **target_run,
                "_control_outcome": "terminal_won",
                "_control_run": target_run,
            }
        self._ensure_worker_processor(worker_id)
        return replacement

    def desktop_action(
        self,
        worker_id: str,
        action: str,
        *,
        url: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, object]:
        worker = self.require_worker(worker_id)
        self._ensure_execution_allowed(worker)
        if not hasattr(self.runtime, "desktop_action"):
            raise RuntimeErrorBase("Desktop actions are not supported by the configured runtime")
        with self._worker_compute_release_lock(worker_id):
            active_run = self.store.get_active_run(worker_id)
            if worker.get("state") == "running":
                guarded_worker = self.store.worker_compute_use_allowed(worker_id)
            else:
                guarded_worker = self.store.begin_worker_compute_start(worker_id)
            if guarded_worker is None:
                raise RuntimeErrorBase(
                    "Worker compute release is in progress; retry shortly"
                )
            worker = guarded_worker
            try:
                launched = self.runtime.desktop_action(
                    worker, action, url=url, run_id=run_id
                )
            except TypeError as exc:
                if "run_id" not in str(exc):
                    raise
                launched = self.runtime.desktop_action(worker, action, url=url)
            except Exception as exc:
                self.store.update_worker(
                    worker_id,
                    state=str(worker.get("state") or "failed"),
                    last_error=str(exc),
                )
                raise
        target_state = "running" if active_run else "ready"
        self._refresh_runtime_info(worker_id, state=target_state, last_error="")
        self.store.add_event(
            worker["project_id"],
            worker_id,
            active_run["run_id"] if active_run else None,
            "worker.desktop_action",
            f"{action}: {launched.get('notes') or launched.get('status') or 'launched'}",
        )
        return launched

    def _finish_paused_worker_transition(
        self,
        worker: dict,
        active_run: dict | None,
        *,
        runtime_info: RuntimeInfo | None,
    ) -> dict:
        """Commit the durable half of Pause after the runtime is suspended."""

        worker_id = str(worker["worker_id"])
        active_state = str((active_run or {}).get("state") or "")
        paused_run = None
        if active_run and active_state in {"queued", "running", "settling"}:
            paused_run = self.store.transition_run_if_state(
                str(active_run["run_id"]),
                active_state,
                "paused",
                ended_at=None,
                error_text="Paused by operator",
            )
            if not paused_run:
                durable = self.store.get_run(str(active_run["run_id"])) or {}
                if str(durable.get("state") or "") in TERMINAL_RUN_STATES:
                    # Completion/cancellation that commits while the runtime
                    # pause RPC is in flight is authoritative.
                    return self.store.update_worker_state(
                        worker_id, "ready", last_error=""
                    ) or worker
                if str(durable.get("state") or "") == "paused":
                    return self.store.update_worker_state(
                        worker_id, "paused", last_error=""
                    ) or worker
                return worker

        updated = (
            self._apply_runtime_info(
                worker_id,
                runtime_info,
                state="paused",
                last_error=worker.get("last_error") or "",
            )
            if runtime_info is not None
            else self.store.update_worker_state(worker_id, "paused", last_error="")
        )
        if paused_run:
            self.store.add_event(
                worker["project_id"], worker_id, paused_run["run_id"], "run.paused", "Run paused by operator"
            )
            self.store.add_event(
                worker["project_id"], worker_id, paused_run["run_id"], "worker.paused", "Worker paused"
            )
            self._emit_callback(
                worker,
                "run.paused",
                run=paused_run,
                message="Worker paused",
            )
        elif active_run is None:
            self.store.add_event(
                worker["project_id"], worker_id, None, "worker.paused", "Worker paused"
            )
            self._emit_callback(
                worker,
                "worker.paused",
                run=None,
                message="Worker paused",
            )
        return updated or worker

    def pause_worker(
        self,
        worker_id: str,
        *,
        run_id: str = "",
        action_use_id: str = "",
    ) -> dict:
        if not str(run_id or "").strip() and not self.store.get_controllable_run(
            worker_id
        ):
            return self._pause_worker_without_run(worker_id)
        with self._worker_compute_release_lock(worker_id):
            worker = self.require_worker(worker_id)
            active_run = (
                self.require_run(run_id)
                if str(run_id or "").strip()
                else self.store.get_active_run(worker_id)
                or self.store.get_controllable_run(worker_id)
            )
            if active_run and str(active_run.get("worker_id") or "") != worker_id:
                raise RuntimeError("active_work_run_scope_mismatch")
            active_state = str((active_run or {}).get("state") or "")
            if active_state == "paused":
                return (
                    self.store.update_worker_state(worker_id, "paused", last_error="")
                    or worker
                )
            if not active_run or active_state not in {"queued", "running", "settling"}:
                raise RuntimeErrorBase("No exact active run is available to pause")
            target_run_id = str(active_run["run_id"])
            runtime_worker = worker
            if active_state != "queued":
                runtime_worker = self._require_confirmed_host_control_identity(
                    worker, target_run_id
                )
            claim = self._claim_exact_run_control(
                worker,
                active_run,
                kind="pause_run",
                action_use_id=action_use_id,
            )
            claimed_worker = dict(claim.get("worker") or worker)
            token = str(claim["token"])
            epoch = int(claim["epoch"])
            info = None
            runtime_already_confirmed = self.store.worker_control_runtime_proof_matches(
                claimed_worker
            )
            if active_state != "queued" and not runtime_already_confirmed:
                runtime_worker = (
                    self._worker_with_host_lease(claimed_worker, target_run_id)
                    if str(worker.get("execution_mode") or "docker") != "host"
                    else {**runtime_worker, **claimed_worker}
                )
                runtime_worker = self._require_claimed_container_generation(
                    runtime_worker
                )
                info = self.runtime.pause_worker(runtime_worker)
                if not self._runtime_control_info_is_confirmed(info):
                    raise RuntimeErrorBase(
                        "Runtime pause did not confirm the exact process stopped"
                    )
                if not self.store.confirm_worker_control_runtime_effect(
                    worker_id,
                    token,
                    epoch,
                    kind="pause_run",
                    target_run_id=target_run_id,
                ):
                    raise RuntimeErrorBase(
                        "Pause runtime proof lost durable lifecycle ownership"
                    )
                if str(worker.get("execution_mode") or "docker") == "host":
                    self._invalidate_worker_processor(worker_id)
                if action_use_id:
                    self.store.checkpoint_active_work_action(
                        action_use_id,
                        "runtime_paused",
                        executor_id=self._executor_id,
                    )
            operation = self.store.finalize_worker_run_control_claim(
                worker_id,
                token,
                epoch,
                kind="pause_run",
                target_run_id=target_run_id,
                target_expected_states=(active_state,),
                target_state="paused",
                worker_state="paused",
                runtime_fields=(
                    self._runtime_info_fields(worker_id, info, last_error="")
                    if info is not None
                    else {}
                ),
                error_text="Paused by operator",
                release_lease=(
                    active_state != "queued"
                    and str(worker.get("execution_mode") or "docker") == "host"
                ),
            )
            if not operation:
                raise RuntimeErrorBase(
                    "Pause lost the exact run lifecycle generation before finalization"
                )
            updated = dict(operation.get("worker") or claimed_worker)
            paused_run = dict(operation.get("run") or active_run)
            target_transitioned = bool(operation.get("target_transitioned"))
        if not target_transitioned:
            return {
                **updated,
                "_control_outcome": "terminal_won",
                "_control_run": paused_run,
            }
        self._replay_pending_lifecycle_effects()
        return updated

    def _pause_worker_without_run(self, worker_id: str) -> dict:
        """Pause idle compute under the same durable lifecycle ownership."""

        with self._worker_compute_release_lock(worker_id):
            worker = self.require_worker(worker_id)
            if self.store.get_controllable_run(worker_id):
                raise RuntimeErrorBase(
                    "The worker lifecycle generation changed; retry Pause"
                )
            if str(worker.get("state") or "") == "paused":
                return worker
            if str(worker.get("execution_mode") or "docker") == "host":
                raise RuntimeErrorBase(
                    "The exact host process identity is not confirmed; control remains pending"
                )
            claim = self.store.try_claim_worker_compute_release(
                worker_id,
                expected_updated_at=str(worker.get("updated_at") or ""),
                expected_last_run_id=str(worker.get("last_run_id") or ""),
                expected_state=str(worker.get("state") or ""),
                expected_container_id=self._runtime_compute_container_id(worker),
                owner=self._executor_id,
                ttl_s=self._compute_release_claim_ttl_s(),
                kind="pause_worker",
            )
            if claim is None:
                raise RuntimeErrorBase(
                    "The exact worker lifecycle generation changed; retry Pause"
                )
            claimed_worker = dict(claim.get("worker") or worker)
            runtime_worker = self._require_claimed_container_generation(
                claimed_worker
            )
            info = self.runtime.pause_worker(runtime_worker)
            if not self._runtime_control_info_is_confirmed(info):
                raise RuntimeErrorBase(
                    "Runtime pause did not confirm the exact compute state"
                )
            updated = self.store.finalize_worker_compute_release(
                worker_id,
                str(claim["token"]),
                int(claim["epoch"]),
                expected_kind="pause_worker",
                compute_released_at=worker.get("compute_released_at"),
                runtime_fields=self._runtime_info_fields(
                    worker_id, info, last_error=""
                ),
                idle_state="paused",
            )
            if not updated:
                raise RuntimeErrorBase(
                    "Pause lost the exact worker lifecycle generation before finalization"
                )
        self._replay_pending_lifecycle_effects()
        return updated

    def interrupt_worker(self, worker_id: str, run_id: str | None = None) -> dict:
        with self._worker_compute_release_lock(worker_id):
            worker = self.require_worker(worker_id)
            active_run = self.store.get_active_run(worker_id)
            if run_id and (
                not active_run
                or str(active_run.get("run_id") or "") != str(run_id)
            ):
                return worker
            if not active_run or str(active_run.get("state") or "") not in {
                "running",
                "settling",
            }:
                return worker
            target_run_id = str(active_run["run_id"])
            runtime_worker = self._require_confirmed_host_control_identity(
                worker, target_run_id
            )
            claim = self._claim_exact_run_control(
                worker, active_run, kind="interrupt_run"
            )
            claimed_worker = dict(claim.get("worker") or worker)
            token = str(claim["token"])
            epoch = int(claim["epoch"])
            if str(worker.get("execution_mode") or "docker") != "host":
                runtime_worker = self._worker_with_host_lease(
                    claimed_worker, target_run_id
                )
            else:
                runtime_worker = {**runtime_worker, **claimed_worker}
            info = None
            if not self.store.worker_control_runtime_proof_matches(claimed_worker):
                runtime_worker = self._require_claimed_container_generation(
                    runtime_worker
                )
                try:
                    info = self.runtime.interrupt_worker(
                        runtime_worker, run_id=target_run_id
                    )
                except TypeError as exc:
                    if "run_id" not in str(exc):
                        raise
                    info = self.runtime.interrupt_worker(runtime_worker)
                if not self._runtime_control_info_is_confirmed(info):
                    raise RuntimeErrorBase(
                        "Runtime interrupt did not confirm the exact process stopped"
                    )
                if not self.store.confirm_worker_control_runtime_effect(
                    worker_id,
                    token,
                    epoch,
                    kind="interrupt_run",
                    target_run_id=target_run_id,
                ):
                    raise RuntimeErrorBase(
                        "Interrupt runtime proof lost durable lifecycle ownership"
                    )
            self._invalidate_worker_processor(worker_id)
            operation = self.store.finalize_worker_run_control_claim(
                worker_id,
                token,
                epoch,
                kind="interrupt_run",
                target_run_id=target_run_id,
                target_expected_states=(str(active_run.get("state") or "running"),),
                target_state="interrupted",
                worker_state="ready",
                runtime_fields=(
                    self._runtime_info_fields(worker_id, info, last_error="")
                    if info is not None
                    else {}
                ),
                error_text="Interrupted by operator",
                release_lease=True,
            )
            if not operation:
                raise RuntimeErrorBase(
                    "Interrupt lost the exact run lifecycle generation before finalization"
                )
            updated = dict(operation.get("worker") or claimed_worker)
            finalized_run = dict(operation.get("run") or active_run)
            target_transitioned = bool(operation.get("target_transitioned"))
        if not target_transitioned:
            return {
                **updated,
                "_control_outcome": "terminal_won",
                "_control_run": finalized_run,
            }
        self._replay_pending_lifecycle_effects()
        return updated

    def _execute_stop_run_claim(
        self,
        worker: dict,
        run_id: str,
        *,
        action_use_id: str = "",
    ) -> dict[str, object] | None:
        """Own, execute, and atomically finalize one exact run stop."""

        worker_id = str(worker.get("worker_id") or "")
        with self._worker_compute_release_lock(worker_id):
            current = self.store.get_worker(worker_id)
            target = self.store.get_run(run_id)
            existing_token = str(
                (current or {}).get("compute_release_token") or ""
            ).strip()
            if (
                not current
                or not target
                or str(target.get("worker_id") or "") != worker_id
                or (
                    str(target.get("state") or "") in TERMINAL_RUN_STATES
                    and not existing_token
                )
            ):
                return None
            expected_container_id = (
                str(current.get("compute_release_container_id") or "").strip()
                if existing_token
                else self._runtime_compute_container_id(current)
            )
            claim = self.store.try_claim_worker_compute_release(
                worker_id,
                expected_updated_at=str(current.get("updated_at") or ""),
                expected_last_run_id=str(current.get("last_run_id") or ""),
                expected_state=str(current.get("state") or ""),
                expected_container_id=expected_container_id,
                owner=self._executor_id,
                ttl_s=self._compute_release_claim_ttl_s(),
                kind="stop_run",
                target_run_id=run_id,
                expected_target_started_at=str(target.get("started_at") or ""),
                action_use_id=action_use_id,
                action_executor_id=self._executor_id if action_use_id else "",
            )
            if claim is None:
                return None
            claimed_worker = dict(claim.get("worker") or current)
            token = str(claim["token"])
            epoch = int(claim["epoch"])
            if not self.store.worker_compute_release_claim_matches(
                worker_id, token, epoch
            ):
                return None
            durable_target = self.store.get_run(run_id) or target
            if str(durable_target.get("state") or "") in TERMINAL_RUN_STATES:
                # Terminal truth that committed before recovery is authoritative.
                # Settle only the durable Stop/tombstone transaction; there is
                # no exact live target left that this control may signal.
                result = self.store.finalize_worker_work_stop_claim(
                    worker_id,
                    token,
                    epoch,
                    target_run_id=run_id,
                    runtime_fields={},
                    compute_released_at=claimed_worker.get("compute_released_at"),
                    error_text="Stopped by operator",
                )
                if result is None:
                    raise RuntimeError(
                        "Terminal-won run stop ownership changed before finalization"
                    )
                return {**result, "confirmation_pending": False}
            runtime_worker = self._worker_with_host_lease(claimed_worker, run_id)
            runtime_worker = {
                **runtime_worker,
                "_compute_release_container_id": str(
                    claimed_worker.get("compute_release_container_id") or ""
                ).strip(),
            }
            captured_container_id = str(
                claimed_worker.get("compute_release_container_id") or ""
            ).strip()
            if captured_container_id:
                current_container_id = self._runtime_compute_container_id(
                    claimed_worker
                )
                if current_container_id != captured_container_id:
                    raise RuntimeErrorBase(
                        "Worker sandbox generation changed before exact-run stop"
                    )
            active_lease = self.store.get_active_host_run_lease_for_run(run_id)
            prelaunch_reservation = bool(
                active_lease
                and str(active_lease.get("startup_state") or "") == "reserved"
                and not int(active_lease.get("pid") or 0)
                and not str(
                    active_lease.get("process_start_identity") or ""
                ).strip()
                and not str(
                    active_lease.get("startup_container_id") or ""
                ).strip()
                and not str(
                    active_lease.get("startup_session_id") or ""
                ).strip()
            )
            no_started_compute = bool(
                str(target.get("state") or "") in {"queued", "needs_input"}
                and not active_lease
            )
            if prelaunch_reservation or no_started_compute:
                # The work-stop tombstone won before any external identity was
                # accepted. The waiting processor will fail its reservation
                # re-read under the same flock, so there is nothing to signal.
                info = RuntimeInfo(
                    runtime=str(claimed_worker.get("runtime") or ""),
                    model=str(claimed_worker.get("model") or ""),
                    gateway_url=str(claimed_worker.get("gateway_url") or ""),
                    gateway_port=claimed_worker.get("gateway_port"),
                    gateway_token=claimed_worker.get("gateway_token"),
                    session_key=claimed_worker.get("session_key"),
                    state_dir=claimed_worker.get("state_dir"),
                    workspace_dir=claimed_worker.get("workspace_dir"),
                    pid=None,
                )
            else:
                try:
                    info = self.runtime.interrupt_worker(
                        runtime_worker, run_id=run_id
                    )
                except TypeError as exc:
                    if "run_id" not in str(exc):
                        raise
                    info = self.runtime.interrupt_worker(runtime_worker)
            if info.pid is not None:
                updated = self._apply_runtime_info(
                    worker_id,
                    info,
                    state="stopping",
                    last_error="",
                )
                return {
                    "worker": updated or claimed_worker,
                    "run": target,
                    "confirmation_pending": True,
                    "target_transitioned": False,
                }
            result = self.store.finalize_worker_work_stop_claim(
                worker_id,
                token,
                epoch,
                target_run_id=run_id,
                runtime_fields=self._runtime_info_fields(
                    worker_id,
                    info,
                    last_error="",
                ),
                compute_released_at=claimed_worker.get("compute_released_at"),
                error_text="Stopped by operator",
            )
            if result is None:
                raise RuntimeError("Run stop ownership changed before finalization")
            return {
                **result,
                "confirmation_pending": False,
            }

    def _recover_stop_run_claim(
        self,
        worker: dict,
        run_id: str,
    ) -> dict[str, object] | None:
        result = self._execute_stop_run_claim(worker, run_id)
        if not result or result.get("confirmation_pending"):
            return None
        updated = dict(result.get("worker") or {})
        if updated.get("state") not in {"paused", "terminated", "needs_input"}:
            if self.store.has_queued_runs(str(worker.get("worker_id") or "")):
                self._ensure_worker_processor(str(worker.get("worker_id") or ""))
        return {
            "worker_id": worker.get("worker_id"),
            "project_id": worker.get("project_id"),
            "tenant_id": worker.get("tenant_id"),
            "owner_id": worker.get("owner_id"),
            "state": updated.get("state"),
            "kind": "stop_run",
            "target_run_id": run_id,
            "target_transitioned": bool(result.get("target_transitioned")),
        }

    def stop_run(
        self,
        worker_id: str,
        run_id: str,
        *,
        action_use_id: str = "",
    ) -> dict[str, object]:
        """Stop one exact run without claiming cancellation before process death is proven."""

        worker = self.require_worker(worker_id)
        target_run = self.store.get_run(run_id)
        if (
            not target_run
            or str(target_run.get("worker_id") or "") != worker_id
            or str(target_run.get("state") or "") in TERMINAL_RUN_STATES
        ):
            return {
                "worker": worker,
                "run": target_run,
                "confirmation_pending": False,
                "accepted": False,
            }
        try:
            operation = self._execute_stop_run_claim(
                worker, run_id, action_use_id=action_use_id
            )
        except Exception as exc:
            error_text = (
                public_callback_message_text(str(exc))
                or "Run termination could not be confirmed"
            )
            logger.warning(
                "GlassHive exact-run stop remains pending because termination was not confirmed",
                extra={"worker_id": worker_id, "run_id": run_id},
                exc_info=True,
            )
            updated = self.store.update_worker_state(
                worker_id,
                "stopping",
                last_error=error_text,
            )
            message = "Run stop requested; termination confirmation is pending"
            self.store.add_event(
                worker["project_id"],
                worker_id,
                run_id,
                "run.stopping",
                f"{message}: {error_text}",
            )
            return {
                "worker": updated or worker,
                "run": target_run,
                "confirmation_pending": True,
                "accepted": True,
                "termination_error": error_text,
            }
        if operation is None:
            current = self.store.get_worker(worker_id) or worker
            pending = bool(
                current.get("compute_release_token")
                and str(current.get("compute_release_kind") or "") == "stop_run"
                and str(current.get("compute_release_scope") or "") == "work"
                and str(current.get("compute_release_target_run_id") or "") == run_id
                and str(current.get("work_stop_id") or "")
                == str(current.get("compute_release_operation_id") or "")
            )
            if pending and action_use_id:
                action_record = self.store.get_active_work_action(action_use_id) or {}
                pending = bool(
                    str(action_record.get("lifecycle_operation_id") or "")
                    == str(current.get("work_stop_id") or "")
                    and str(action_record.get("lifecycle_operation_kind") or "")
                    == "stop_run"
                    and str(action_record.get("lifecycle_target_run_id") or "")
                    == run_id
                )
            return {
                "worker": current,
                "run": self.store.get_run(run_id),
                "confirmation_pending": pending,
                "accepted": pending,
            }
        confirmation_pending = bool(operation.get("confirmation_pending"))
        updated = dict(operation.get("worker") or worker)
        if confirmation_pending:
            self.store.add_event(
                worker["project_id"],
                worker_id,
                run_id,
                "run.stopping",
                "Run stop requested; termination confirmation is pending",
            )
            self._emit_callback(
                worker,
                "run.stopping",
                run={**target_run, "state": "stopping"},
                message="Run stop requested; termination confirmation is pending",
            )
            return {
                "worker": updated,
                "run": target_run,
                "confirmation_pending": True,
                "accepted": True,
            }
        cancelled_run = operation.get("run")
        transitioned = bool(operation.get("target_transitioned"))
        self._replay_pending_lifecycle_effects()
        if (
            updated.get("state") not in {"paused", "terminated", "needs_input"}
            and self.store.has_queued_runs(worker_id)
        ):
            self._ensure_worker_processor(worker_id)
        return {
            "worker": updated,
            "run": cancelled_run or self.store.get_run(run_id),
            "confirmation_pending": False,
            "accepted": bool(operation.get("work_stop_outcome")) or transitioned,
            "work_stop_outcome": str(operation.get("work_stop_outcome") or ""),
        }

    def _finish_resumed_worker_transition(
        self,
        worker: dict,
        paused_run: dict,
        runtime_updated_worker: dict,
    ) -> dict:
        """Commit the exact paused run after the runtime resume RPC succeeds."""

        worker_id = str(worker["worker_id"])
        # Docker pause freezes the live provider process, so unpausing can
        # continue it in place. Host pause terminates the provider process;
        # requeue the same durable run for the same isolated workspace.
        resume_state = (
            "queued"
            if str(worker.get("execution_mode") or "docker") == "host"
            or not paused_run.get("started_at")
            else "running"
        )
        resumed_run = self.store.transition_run_if_state(
            str(paused_run["run_id"]),
            "paused",
            resume_state,
            ended_at=None,
            error_text="",
            retry_after=None,
        )
        if resumed_run:
            refreshed = self.store.update_worker_state(
                worker_id,
                "running" if resume_state == "running" else "starting",
                last_error="",
            )
            self.store.add_event(
                str(worker["project_id"]),
                worker_id,
                str(paused_run["run_id"]),
                "run.resumed",
                "Paused run resumed",
            )
            self._emit_callback(
                worker,
                "run.started" if resume_state == "running" else "run.queued",
                run=resumed_run,
                message=(
                    "Paused run resumed"
                    if resume_state == "running"
                    else "Paused run queued for execution restart"
                ),
            )
            if resume_state == "queued":
                self._ensure_worker_processor(worker_id)
            return refreshed or runtime_updated_worker
        durable = self.store.get_run(str(paused_run["run_id"])) or {}
        if str(durable.get("state") or "") in TERMINAL_RUN_STATES:
            return self.store.update_worker_state(
                worker_id, "ready", last_error=""
            ) or runtime_updated_worker
        if str(durable.get("state") or "") in {"queued", "running"}:
            return self.store.update_worker_state(
                worker_id,
                "running" if str(durable.get("state")) == "running" else "starting",
                last_error="",
            ) or runtime_updated_worker
        return runtime_updated_worker

    def resume_worker(
        self,
        worker_id: str,
        *,
        run_id: str = "",
        action_use_id: str = "",
    ) -> dict:
        current = self.require_worker(worker_id)
        if (
            str(current.get("compute_release_token") or "")
            and str(current.get("compute_release_expires_at") or "") > utc_now()
        ):
            raise RuntimeErrorBase("Worker compute release is in progress")
        if not str(run_id or "").strip():
            if (
                str(current.get("state") or "") == "paused"
                and not self.store.get_controllable_run(worker_id)
            ):
                return self._resume_worker_without_run(worker_id)
        with self._worker_compute_release_lock(worker_id):
            worker = self.require_worker(worker_id)
            self._ensure_execution_allowed(worker)
            worker = self._refresh_worker_model_for_profile(worker)
            if str(run_id or "").strip():
                paused_run = self.require_run(run_id)
            else:
                controllable_runs = self.store.list_nonterminal_runs_for_worker(
                    worker_id
                )
                paused_run = next(
                    (
                        candidate
                        for candidate in controllable_runs
                        if str(candidate.get("state") or "") == "paused"
                    ),
                    None,
                )
                if paused_run is None:
                    paused_run = (
                        self.store.get_active_run(worker_id)
                        or self.store.get_controllable_run(worker_id)
                    )
            if paused_run and str(paused_run.get("worker_id") or "") != worker_id:
                raise RuntimeError("active_work_run_scope_mismatch")
            if not paused_run or str(paused_run.get("state") or "") != "paused":
                if paused_run and str(paused_run.get("state") or "") in {
                    "queued",
                    "running",
                    "settling",
                }:
                    return worker
                updated = self._start_worker_again(
                    worker, event_type="worker.resumed", message="Worker resumed"
                )
                active_run = self.store.get_active_run(worker_id)
                if active_run:
                    return (
                        self.store.update_worker_state(
                            worker_id, "running", last_error=""
                        )
                        or updated
                    )
                self._ensure_worker_processor(worker_id)
                return updated

            target_run_id = str(paused_run["run_id"])
            claim = self._claim_exact_run_control(
                worker,
                paused_run,
                kind="resume_run",
                action_use_id=action_use_id,
            )
            claimed_worker = dict(claim.get("worker") or worker)
            token = str(claim["token"])
            epoch = int(claim["epoch"])
            compute_was_released = bool(worker.get("compute_released_at"))
            info = None
            if not self.store.worker_control_runtime_proof_matches(claimed_worker):
                try:
                    info = self.runtime.ensure_worker_ready(claimed_worker)
                except Exception as exc:
                    self._restore_failed_resume_claim(
                        worker=claimed_worker,
                        token=token,
                        epoch=epoch,
                        kind="resume_run",
                        target_run_id=target_run_id,
                        startup_error=exc,
                    )
                    raise
                if not self.store.confirm_worker_control_runtime_effect(
                    worker_id,
                    token,
                    epoch,
                    kind="resume_run",
                    target_run_id=target_run_id,
                ):
                    raise RuntimeErrorBase(
                        "Resume startup proof lost durable lifecycle ownership"
                    )
            captured_container_id = str(
                claimed_worker.get("compute_release_container_id") or ""
            ).strip()
            current_container_id = (
                self._runtime_compute_container_id(claimed_worker)
                if str(worker.get("execution_mode") or "docker") == "docker"
                else ""
            )
            external_runtime_requires_identity = bool(
                getattr(self.runtime, "requires_run_start_identity", False)
            )
            exact_paused_generation_resumed = bool(
                not external_runtime_requires_identity
                or (
                    captured_container_id
                    and current_container_id == captured_container_id
                )
            )
            resume_state = (
                "queued"
                if compute_was_released
                or str(worker.get("execution_mode") or "docker") == "host"
                or not paused_run.get("started_at")
                or not exact_paused_generation_resumed
                else "running"
            )
            operation = self.store.finalize_worker_run_control_claim(
                worker_id,
                token,
                epoch,
                kind="resume_run",
                target_run_id=target_run_id,
                target_expected_states=("paused",),
                target_state=resume_state,
                worker_state="starting" if resume_state == "queued" else "running",
                runtime_fields=(
                    self._runtime_info_fields(worker_id, info, last_error="")
                    if info is not None
                    else {}
                ),
                error_text="",
                release_lease=False,
            )
            if not operation:
                raise RuntimeErrorBase(
                    "Resume lost the exact run lifecycle generation before finalization"
                )
            updated = dict(operation.get("worker") or claimed_worker)
            resumed_run = dict(operation.get("run") or paused_run)
            target_transitioned = bool(operation.get("target_transitioned"))
            if action_use_id:
                self.store.checkpoint_active_work_action(
                    action_use_id,
                    "runtime_resumed",
                    executor_id=self._executor_id,
                )
        if not target_transitioned:
            return {
                **updated,
                "_control_outcome": "terminal_won",
                "_control_run": resumed_run,
            }
        self._replay_pending_lifecycle_effects()
        if resume_state == "queued":
            self._ensure_worker_processor(worker_id)
        return updated

    def _resume_worker_without_run(self, worker_id: str) -> dict:
        """Resume an idle paused worker only after its startup handshake succeeds."""

        with self._worker_compute_release_lock(worker_id):
            worker = self.require_worker(worker_id)
            self._ensure_execution_allowed(worker)
            worker = self._refresh_worker_model_for_profile(worker)
            if str(worker.get("state") or "") != "paused":
                return worker
            if self.store.get_controllable_run(worker_id):
                raise RuntimeErrorBase(
                    "The worker lifecycle generation changed; retry Resume"
                )
            expected_container_id = (
                str(worker.get("compute_release_container_id") or "").strip()
                if str(worker.get("compute_release_token") or "").strip()
                else self._runtime_compute_container_id(worker)
            )
            claim = self.store.try_claim_worker_compute_release(
                worker_id,
                expected_updated_at=str(worker.get("updated_at") or ""),
                expected_last_run_id=str(worker.get("last_run_id") or ""),
                expected_state=str(worker.get("state") or ""),
                expected_container_id=expected_container_id,
                owner=self._executor_id,
                ttl_s=self._compute_release_claim_ttl_s(),
                kind="resume_worker",
            )
            if claim is None:
                raise RuntimeErrorBase(
                    "The exact worker lifecycle generation changed; retry Resume"
                )
            claimed_worker = dict(claim.get("worker") or worker)
            try:
                info = self.runtime.ensure_worker_ready(claimed_worker)
            except Exception as exc:
                self._restore_failed_resume_claim(
                    worker=claimed_worker,
                    token=str(claim["token"]),
                    epoch=int(claim["epoch"]),
                    kind="resume_worker",
                    target_run_id="",
                    startup_error=exc,
                )
                raise
            updated = self.store.finalize_worker_compute_release(
                worker_id,
                str(claim["token"]),
                int(claim["epoch"]),
                expected_kind="resume_worker",
                compute_released_at=None,
                runtime_fields=self._runtime_info_fields(
                    worker_id, info, last_error=""
                ),
                idle_state="ready",
            )
            if not updated:
                raise RuntimeErrorBase(
                    "Resume lost the exact worker lifecycle generation before finalization"
                )
        self._replay_pending_lifecycle_effects()
        return updated

    def _execute_worker_termination_claim(
        self, worker: dict
    ) -> dict[str, object] | None:
        """Terminate a whole worker only while the exact durable claim owns it."""

        worker_id = str(worker.get("worker_id") or "")
        with self._worker_compute_release_lock(worker_id):
            claim: dict[str, object] | None = None
            current: dict = {}
            target_run_id = ""
            # A queue processor can atomically promote queued -> running just
            # before this whole-worker tombstone is reserved. That benign CAS
            # loss must not turn an explicit Terminate into a false conflict.
            # Re-read the durable generation while the lifecycle guard keeps
            # the promoted run behind its final pre-launch boundary.
            for _attempt in range(3):
                current = self.store.get_worker(worker_id) or {}
                if not current:
                    return None
                if str(current.get("state") or "") == "terminated" and not str(
                    current.get("compute_release_token") or ""
                ).strip():
                    return {"worker": current, "target_transitioned": False}
                existing_token = str(
                    current.get("compute_release_token") or ""
                ).strip()
                if existing_token:
                    target_run_id = str(
                        current.get("compute_release_target_run_id") or ""
                    ).strip()
                    target_started_at = str(
                        current.get("compute_release_target_started_at") or ""
                    ).strip()
                    expected_container_id = str(
                        current.get("compute_release_container_id") or ""
                    ).strip()
                else:
                    target = self.store.get_active_run(
                        worker_id
                    ) or self.store.get_controllable_run(worker_id)
                    target_run_id = str((target or {}).get("run_id") or "").strip()
                    target_started_at = str(
                        (target or {}).get("started_at") or ""
                    ).strip()
                    expected_container_id = self._runtime_compute_container_id(current)
                claim = self.store.try_claim_worker_compute_release(
                    worker_id,
                    expected_updated_at=str(current.get("updated_at") or ""),
                    expected_last_run_id=str(current.get("last_run_id") or ""),
                    expected_state=str(current.get("state") or ""),
                    expected_container_id=expected_container_id,
                    owner=self._executor_id,
                    ttl_s=self._compute_release_claim_ttl_s(),
                    kind="terminate_worker",
                    target_run_id=target_run_id,
                    expected_target_started_at=target_started_at,
                )
                if claim is not None:
                    break
                refreshed = self.store.get_worker(worker_id) or {}
                if str(refreshed.get("compute_release_token") or "").strip():
                    return None
            if claim is None:
                return None
            claimed_worker = dict(claim.get("worker") or current)
            token = str(claim["token"])
            epoch = int(claim["epoch"])
            if not self.store.worker_compute_release_claim_matches(
                worker_id, token, epoch
            ):
                return None
            if str(claimed_worker.get("execution_mode") or "docker") == "docker":
                captured_container_id = str(
                    claimed_worker.get("compute_release_container_id") or ""
                ).strip()
                current_container_id = self._runtime_compute_container_id(
                    claimed_worker
                )
                if current_container_id != captured_container_id:
                    rebound = self.store.rebind_worker_termination_claim_generation(
                        worker_id,
                        token,
                        epoch,
                        container_id=current_container_id,
                    )
                    if rebound is None:
                        raise RuntimeErrorBase(
                            "Worker termination generation changed before rebinding"
                        )
                    claimed_worker = rebound
                    epoch = int(rebound["compute_release_epoch"])
            runtime_worker = (
                self._worker_with_host_lease(claimed_worker, target_run_id)
                if target_run_id
                else claimed_worker
            )
            runtime_worker = {
                **runtime_worker,
                "_compute_release_container_id": str(
                    claimed_worker.get("compute_release_container_id") or ""
                ).strip(),
            }
            active_host_lease = (
                self.store.get_active_host_run_lease_for_run(target_run_id)
                if target_run_id
                else None
            )
            no_started_host_compute = bool(
                str(claimed_worker.get("execution_mode") or "docker") == "host"
                and (
                    active_host_lease is None
                    or str(active_host_lease.get("startup_state") or "")
                    == "reserved"
                )
            )
            if no_started_host_compute:
                info = RuntimeInfo(
                    runtime=str(claimed_worker.get("runtime") or ""),
                    model=str(claimed_worker.get("model") or ""),
                    gateway_url=str(claimed_worker.get("gateway_url") or ""),
                    gateway_port=claimed_worker.get("gateway_port"),
                    gateway_token=claimed_worker.get("gateway_token"),
                    session_key=claimed_worker.get("session_key"),
                    state_dir=claimed_worker.get("state_dir"),
                    workspace_dir=claimed_worker.get("workspace_dir"),
                    pid=None,
                )
            else:
                info = self.runtime.terminate_worker(runtime_worker)
            updated = self.store.finalize_worker_termination_claim(
                worker_id,
                token,
                epoch,
                compute_released_at=utc_now(),
                runtime_fields=self._runtime_info_fields(
                    worker_id,
                    info,
                    last_error="",
                ),
                error_text="Worker terminated by operator",
            )
            if updated is None:
                raise RuntimeError("Worker termination ownership changed before finalization")
            return {"worker": updated, "target_transitioned": True}

    def terminate_worker(self, worker_id: str) -> dict:
        worker = self.require_worker(worker_id)
        operation = self._execute_worker_termination_claim(worker)
        if operation is None:
            raise RuntimeErrorBase(
                "Worker compute operation is in progress; retry shortly"
            )
        updated = dict(operation.get("worker") or {})
        if not bool(operation.get("target_transitioned")):
            return updated
        self._replay_pending_lifecycle_effects()
        return updated

    def reconcile_all_workers(self) -> None:
        for worker in self.store.list_all_workers():
            try:
                self._reconcile_worker_row(worker)
            except Exception as exc:
                worker_id = str(worker.get("worker_id") or "")
                project_id = str(worker.get("project_id") or "")
                logger.warning("Failed to reconcile GlassHive worker %s", worker_id, exc_info=True)
                self.store.add_event(
                    project_id,
                    worker_id,
                    None,
                    "worker.reconcile_failed",
                    public_callback_message_text(str(exc)) or "Worker reconcile failed",
                )

    def _local_processor_owns(self, worker_id: str) -> bool:
        with self._processors_lock:
            return worker_id in self._active_processors

    def _reconcile_worker_row(self, worker: dict) -> None:
        if worker["state"] == "terminated":
            return
        if str(worker.get("compute_release_token") or ""):
            # Generic reconciliation must not mutate a lifecycle generation
            # owned by an exact Pause/Resume/Interrupt/Steer/Stop claim.
            # The specialized expired-claim recovery path is its sole owner.
            return
        active_run = self.store.get_active_run(worker["worker_id"])
        if active_run:
            recovered = self._collect_completed_run(worker, active_run)
            if recovered:
                self._apply_recovered_run(worker, active_run, recovered)
                return
        if worker["state"] == "paused":
            # An older startup reconcile could project a ready, capacity-waiting
            # no-PID worker to paused. That row has no durable paused run, so
            # restore retry eligibility unless a paused run or the latest
            # explicit pause/resume event proves operator pause intent.
            if (
                not active_run
                and not worker.get("pid")
                and self.store.has_queued_capacity_retry(str(worker["worker_id"]))
                and not any(
                    str(run.get("state") or "") == "paused"
                    for run in self.store.list_nonterminal_runs_for_worker(
                        str(worker["worker_id"])
                    )
                )
                and not self.store.has_active_operator_pause(str(worker["worker_id"]))
            ):
                self.store.update_worker(
                    str(worker["worker_id"]),
                    state="ready",
                    last_error=worker.get("last_error") or "",
                    touch_updated_at=False,
                )
                return
            # Paused is a durable non-terminal run state. Older/crash-split
            # rows can have the worker pause committed while the exact run is
            # still marked running/settling. Enforce the runtime pause first,
            # then repair that run with a terminal-wins CAS so resume can
            # safely restart the same durable mission.
            if active_run:
                runtime_worker = {**worker, "_active_run_id": str(active_run["run_id"])}
                info = self.runtime.pause_worker(runtime_worker)
                paused_run = self.store.transition_run_if_state(
                    str(active_run["run_id"]),
                    str(active_run.get("state") or "running"),
                    "paused",
                    ended_at=None,
                    error_text="",
                    retry_after=None,
                )
                if paused_run:
                    self._apply_runtime_info(
                        worker["worker_id"],
                        info,
                        state="paused",
                        last_error="",
                        touch_updated_at=False,
                    )
                    self.store.add_event(
                        worker["project_id"],
                        worker["worker_id"],
                        paused_run["run_id"],
                        "run.paused",
                        "Recovered an incomplete pause transition",
                    )
                    self._emit_callback(
                        worker,
                        "run.paused",
                        run=paused_run,
                        message="Worker pause recovered",
                    )
            return
        runtime_worker = (
            {**worker, "_active_run_id": str(active_run["run_id"])}
            if active_run
            else worker
        )
        info = self.runtime.reconcile_worker(runtime_worker)
        if worker["state"] == "stopping":
            if info.pid:
                self._apply_runtime_info(
                    worker["worker_id"],
                    info,
                    state="stopping",
                    last_error=worker.get("last_error") or "",
                    touch_updated_at=False,
                )
                return
            if active_run:
                cancelled_run = self.store.finalize_run_if_state(
                    active_run["run_id"],
                    str(active_run.get("state") or "running"),
                    "cancelled",
                    error_text="Stopped by operator",
                )
                if cancelled_run:
                    self.store.finalize_schedule_for_run(
                        active_run["run_id"],
                        state="cancelled",
                        last_error="Stopped by operator",
                    )
                    self.store.accept_cancel_actions_for_run(active_run["run_id"])
                    self.store.add_event(
                        worker["project_id"],
                        worker["worker_id"],
                        active_run["run_id"],
                        "run.cancelled",
                        "Run stop confirmed during reconciliation",
                    )
                    self._emit_callback(
                        worker,
                        "run.cancelled",
                        run=cancelled_run,
                        message="Run stop confirmed",
                    )
            self._apply_runtime_info(
                worker["worker_id"],
                info,
                state="ready",
                last_error="",
                touch_updated_at=False,
            )
            return
        # The host process can exit just before the local processor parses and persists its
        # successful result. In that narrow local finalization window there is no live PID, but
        # the current processor still owns the run. A foreign service instance instead discovers
        # a live owner through the durable active-session PID in the host runtime.
        if active_run and not info.pid and self._local_processor_owns(str(worker["worker_id"])):
            self._apply_runtime_info(
                worker["worker_id"],
                info,
                state=worker["state"],
                last_error=worker.get("last_error") or "",
                touch_updated_at=False,
            )
            return
        if (
            active_run
            and info.pid
            and str(active_run.get("state") or "") in {"running", "settling"}
        ):
            self._apply_runtime_info(
                worker["worker_id"],
                info,
                state="running",
                last_error=worker.get("last_error") or "",
                touch_updated_at=False,
            )
            self._ensure_surviving_run_monitor(
                str(worker["worker_id"]), str(active_run["run_id"])
            )
            return
        state = worker["state"]
        if state in {"running", "ready", "starting"}:
            capacity_retry_pending = (
                not active_run
                and self.store.has_queued_capacity_retry(str(worker["worker_id"]))
            )
            if active_run and info.pid:
                state = "running"
            elif info.pid or capacity_retry_pending:
                state = "ready"
            else:
                state = "paused"
        if not info.pid:
            if active_run:
                orphaned_run = self.store.finalize_run_if_state(
                    active_run["run_id"],
                    str(active_run.get("state") or "running"),
                    "interrupted",
                    error_text="Worker process was not running during reconcile",
                    failure_class="provider_temporarily_unavailable",
                    failure_retryable=1,
                    failure_structured=1,
                    failure_user_message=(
                        "The provider worker stopped unexpectedly before completing the response."
                    ),
                    failure_recommended_recovery=(
                        "Retry the request or use the configured provider fallback."
                    ),
                    failure_diagnostic_summary=(
                        "Reconciliation found no live process for the active host run."
                    ),
                )
                if orphaned_run:
                    logger.warning(
                        "Interrupted orphaned GlassHive host run during reconciliation",
                        extra={
                            "reconciler_pid": os.getpid(),
                            "worker_id": str(worker["worker_id"]),
                            "run_id": str(active_run["run_id"]),
                        },
                    )
                    cleanup_orphaned_run = getattr(self.runtime, "cleanup_orphaned_run", None)
                    if callable(cleanup_orphaned_run):
                        try:
                            cleanup_orphaned_run(runtime_worker, str(active_run["run_id"]))
                        except Exception as exc:
                            logger.warning(
                                "Failed to clean up orphaned GlassHive host process",
                                extra={
                                    "reconciler_pid": os.getpid(),
                                    "worker_id": str(worker["worker_id"]),
                                    "run_id": str(active_run["run_id"]),
                                    "error": str(exc),
                                },
                            )
                    self.store.add_event(
                        worker["project_id"],
                        worker["worker_id"],
                        active_run["run_id"],
                        "run.orphaned",
                        "Active run interrupted because the worker process was not running",
                    )
                    self._emit_callback(
                        worker,
                        "run.interrupted",
                        run=orphaned_run,
                        message="Worker process was not running during reconcile",
                    )
        self._apply_runtime_info(
            worker["worker_id"],
            info,
            state=state,
            last_error=worker.get("last_error") or "",
            touch_updated_at=False,
        )

    def require_project(self, project_id: str) -> dict:
        project = self.store.get_project(project_id)
        if not project:
            raise KeyError("Project not found")
        return project

    def require_worker(self, worker_id: str) -> dict:
        worker = self.store.get_worker(worker_id)
        if not worker:
            raise KeyError("Worker not found")
        return worker

    def require_run(self, run_id: str) -> dict:
        run = self.store.get_run(run_id)
        if not run:
            raise KeyError("Run not found")
        return run

    def _collect_completed_run(self, worker: dict, run: dict) -> dict[str, object] | None:
        if not hasattr(self.runtime, "collect_completed_run"):
            return None
        try:
            return self.runtime.collect_completed_run(
                worker,
                run_id=run["run_id"],
                instruction=str(run.get("instruction") or ""),
            )
        except TypeError as exc:
            if "instruction" in str(exc):
                try:
                    return self.runtime.collect_completed_run(worker, run_id=run["run_id"])
                except TypeError as run_id_exc:
                    if "run_id" not in str(run_id_exc):
                        raise
                    return self.runtime.collect_completed_run(worker)
            if "run_id" not in str(exc):
                raise
            return self.runtime.collect_completed_run(worker)

    def _fresh_user_artifact_deliverable(self, worker: dict, run: dict, deliverable: dict[str, object] | None) -> bool:
        if not deliverable:
            return False
        failure_class = str(run.get("failure_class") or "").strip()
        if failure_class not in {"provider_response_failed", "provider_rate_limited", "runtime_io_failed"}:
            return False
        workspace_path = Path(str(deliverable.get("workspace_path") or "").strip())
        if not workspace_path.parts:
            return False
        if not is_user_deliverable_relative_path(workspace_path):
            return False
        raw_root = str(worker.get("workspace_dir") or "").strip()
        if not raw_root:
            return False
        root = Path(raw_root)
        artifact_path = (root / workspace_path).resolve()
        try:
            artifact_path.relative_to(root.resolve())
        except ValueError:
            return False
        if not artifact_path.is_file():
            return False
        if any(part.lower() in SUPPORT_ARTIFACT_DIR_NAMES for part in workspace_path.parts[:-1]):
            return False
        if not self._looks_like_completed_user_deliverable(workspace_path, artifact_path):
            return False
        started_at = str(run.get("started_at") or run.get("queued_at") or "").strip()
        if not started_at:
            return False
        try:
            started = datetime.fromisoformat(started_at.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return False
        return artifact_path.stat().st_mtime >= started - 5

    def _looks_like_completed_user_deliverable(self, workspace_path: Path, artifact_path: Path) -> bool:
        suffix = artifact_path.suffix.lower()
        name = workspace_path.name.lower()
        if suffix in PROFESSIONAL_ARTIFACT_EXTENSIONS:
            return is_valid_professional_artifact(artifact_path)
        if suffix in {".html", ".htm"}:
            return True
        if suffix not in {".csv", ".json", ".md", ".tsv", ".txt"}:
            return False
        token_pattern = r"(?:^|[^a-z0-9]){}(?:[^a-z0-9]|$)"
        if re.search(token_pattern.format(r"(?:partial|draft|scratch|notes?|research|batch)"), name):
            return False
        if suffix in {".csv", ".json", ".tsv"}:
            try:
                return artifact_path.stat().st_size > 0
            except OSError:
                return False
        return bool(re.search(token_pattern.format(r"(?:final|finished|complete|completed|report|deliverable|summary|brief|workbook|deck)"), name))

    def _artifact_completed_output(self, deliverable: dict[str, object], failure_fields: dict[str, object]) -> str:
        path = str(deliverable.get("workspace_path") or deliverable.get("label") or "generated artifact").strip()
        warning = str(failure_fields.get("failure_user_message") or "").strip()
        lines = [
            "FINAL REPORT:",
            f"GlassHive produced the requested downloadable artifact before the model provider stream ended: `{path}`.",
        ]
        if warning:
            lines.append(f"Provider warning after artifact creation: {warning}")
        return "\n".join(lines)

    def _apply_recovered_run(self, worker: dict, run: dict, recovered: dict[str, object]) -> dict | None:
        worker_id = worker["worker_id"]
        state = str(recovered.get("state") or "failed")
        output_text = str(recovered.get("output_text") or "")
        error_text = str(recovered.get("error_text") or "")
        failure_fields = {
            key: recovered.get(key)
            for key in (
                "failure_class",
                "failure_retryable",
                "failure_structured",
                "failure_user_message",
                "failure_recommended_recovery",
                "failure_diagnostic_summary",
            )
            if key in recovered
        }
        provider_retry_after_s = recovered.get("provider_retry_after_s")
        if (
            state == "failed"
            and str(recovered.get("failure_class") or "") == "provider_rate_limited"
            and isinstance(provider_retry_after_s, (int, float))
            and not isinstance(provider_retry_after_s, bool)
            and float(provider_retry_after_s) > 0
        ):
            self._requeue_retryable_run(
                worker,
                run,
                ProviderRateLimitError(
                    error_text or "Structured provider rate limit",
                    retry_after_s=float(provider_retry_after_s),
                ),
                failure_fields=failure_fields,
            )
            return self.store.get_worker(worker_id)
        if state == "needs_input":
            failure_class = str(
                recovered.get("failure_class")
                or "provider_auth_projection_unavailable"
            )
            message = str(
                recovered.get("failure_user_message")
                or error_text
                or "This work needs user input before it can continue."
            )
            blocked_run = self.store.mark_run_needs_input(
                str(run["run_id"]),
                expected_state=str(run.get("state") or "running"),
                error_text=error_text or message,
                failure_class=failure_class,
                failure_user_message=message,
            )
            if not blocked_run:
                return self.store.get_worker(worker_id)
            self.store.finalize_schedule_for_run(
                str(run["run_id"]),
                state="needs_input",
                last_error=error_text or message,
            )
            self.store.update_worker(
                worker_id,
                state="needs_input",
                last_error=error_text or message,
                last_run_id=run["run_id"],
            )
            needs_input_worker = self.store.get_worker(worker_id) or worker
            self._release_needs_input_compute(
                needs_input_worker,
                {**run, **blocked_run},
            )
            self.store.add_event(
                worker["project_id"],
                worker_id,
                run["run_id"],
                "run.needs_input",
                message,
                payload={"failureCode": failure_class},
            )
            callback_worker = self.store.get_worker(worker_id) or worker
            self._emit_callback(
                callback_worker,
                "run.needs_input",
                run={**run, **blocked_run, "state": "needs_input"},
                message=message,
            )
            return self.store.get_worker(worker_id)
        if state == "completed":
            finalized_run = self.store.finalize_run_if_state(
                run["run_id"],
                str(run.get("state") or "running"),
                "completed",
                output_text=output_text,
            )
            if not finalized_run:
                return self.store.get_worker(worker_id)
            self.store.finalize_schedule_for_run(run["run_id"], state="completed")
            self.store.update_worker(worker_id, state="ready", last_error="", last_run_id=run["run_id"])
            message = terminal_callback_message(output_text)
            full_message = terminal_callback_full_message(output_text)
            self.store.add_event(worker["project_id"], worker_id, run["run_id"], "run.completed", message[:TERMINAL_CALLBACK_MESSAGE_LIMIT] or "Run completed")
            recovered_run = {**run, **finalized_run, "state": "completed", "output_text": output_text}
            refreshed_worker = self._refresh_runtime_info(worker_id, state="ready", last_error="") or self.store.get_worker(worker_id) or worker
            deliverable = self._completion_deliverable(refreshed_worker, recovered_run, output_text, error_text)
            self._promote_completed_deliverable(refreshed_worker, recovered_run, deliverable)
            self._emit_callback(
                refreshed_worker,
                "run.completed",
                run=recovered_run,
                message=message or "Run completed",
                full_message=full_message if full_message != message else "",
                deliverable=deliverable,
            )
        else:
            recovered_run = {**run, "state": "failed", "error_text": error_text, **failure_fields}
            refreshed_worker = self._refresh_runtime_info(worker_id, state="ready", last_error=error_text) or self.store.get_worker(worker_id) or worker
            deliverable = self._completion_deliverable(refreshed_worker, recovered_run, output_text, error_text)
            if self._fresh_user_artifact_deliverable(refreshed_worker, recovered_run, deliverable):
                completed_output = output_text.strip() or self._artifact_completed_output(deliverable or {}, failure_fields)
                finalized_run = self.store.finalize_run_if_state(
                    run["run_id"],
                    "running",
                    "completed",
                    output_text=completed_output,
                )
                if not finalized_run:
                    return self.store.get_worker(worker_id)
                self.store.finalize_schedule_for_run(run["run_id"], state="completed")
                self.store.update_worker(worker_id, state="ready", last_error="", last_run_id=run["run_id"])
                message = terminal_callback_message(completed_output)
                full_message = terminal_callback_full_message(completed_output)
                self.store.add_event(
                    worker["project_id"],
                    worker_id,
                    run["run_id"],
                    "run.completed",
                    message[:TERMINAL_CALLBACK_MESSAGE_LIMIT] or "Run completed with artifacts",
                )
                completed_run = {**run, **finalized_run, "state": "completed", "output_text": completed_output}
                self._promote_completed_deliverable(refreshed_worker, completed_run, deliverable)
                self._emit_callback(
                    refreshed_worker,
                    "run.completed",
                    run=completed_run,
                    message=message or "Run completed with artifacts",
                    full_message=full_message if full_message != message else "",
                    deliverable=deliverable,
                )
                return self.store.get_worker(worker_id)
            finalized_run = self.store.finalize_run_if_state(
                run["run_id"],
                "running",
                "failed",
                error_text=error_text,
                **failure_fields,
            )
            if not finalized_run:
                return self.store.get_worker(worker_id)
            self.store.finalize_schedule_for_run(run["run_id"], state="failed", last_error=error_text)
            self.store.update_worker(worker_id, state="ready", last_error=error_text, last_run_id=run["run_id"])
            self.store.add_event(worker["project_id"], worker_id, run["run_id"], "run.failed", error_text or "Run failed")
            failure_message = runtime_failure_callback_message(failure_fields, error_text or "Run failed")
            self._emit_callback(
                refreshed_worker,
                "run.failed",
                run=recovered_run,
                message=failure_message,
                deliverable=deliverable,
            )
        return self.store.get_worker(worker_id)

    def heal_worker(self, worker_id: str) -> dict | None:
        worker = self.store.get_worker(worker_id)
        if not worker or worker.get("state") in {"paused"}:
            return worker
        active_run = self.store.get_active_run(worker_id)
        if not active_run:
            if worker.get("state") in {"starting", "running"}:
                info = self.runtime.reconcile_worker(worker)
                state = "ready" if info.pid else "paused"
                return self._apply_runtime_info(worker_id, info, state=state, last_error=worker.get("last_error") or "") or worker
            return worker
        recovered = self._collect_completed_run(worker, active_run)
        if not recovered:
            return worker
        self._apply_recovered_run(worker, active_run, recovered)
        with self._processors_lock:
            # Stale processors also check active membership before every state write,
            # so dropping membership here is enough to make an externally healed
            # processor stop touching worker state until a replacement generation is spawned.
            self._active_processors.discard(worker_id)
        refreshed = self.store.get_worker(worker_id)
        if refreshed and refreshed["state"] not in {"paused", "terminated"} and self.store.has_queued_runs(worker_id):
            self._ensure_worker_processor(worker_id)
        return refreshed

    def _start_worker_again(self, worker: dict, event_type: str, message: str) -> dict:
        starting_worker = self.store.begin_worker_compute_start(worker["worker_id"])
        if starting_worker is None:
            raise RuntimeErrorBase("Worker compute release is in progress; retry shortly")
        worker = starting_worker
        try:
            info = self.runtime.ensure_worker_ready(worker)
        except Exception as exc:
            updated = self.store.update_worker(worker["worker_id"], state="failed", last_error=str(exc))
            self.store.add_event(worker["project_id"], worker["worker_id"], None, "worker.failed", str(exc))
            return updated or worker
        updated = self._apply_runtime_info(
            worker["worker_id"],
            info,
            state="ready",
            last_error="",
            compute_released_at=None,
        )
        self.store.add_event(worker["project_id"], worker["worker_id"], None, event_type, message)
        return updated or worker

    def _apply_runtime_info(
        self,
        worker_id: str,
        info: RuntimeInfo,
        state: str,
        last_error: str,
        compute_released_at: str | None | object = _UNSET,
        *,
        touch_updated_at: bool = True,
    ) -> dict | None:
        fields = self._runtime_info_fields(worker_id, info, last_error=last_error)
        if compute_released_at is not _UNSET:
            fields["compute_released_at"] = compute_released_at
        return self.store.update_worker(
            worker_id,
            touch_updated_at=touch_updated_at,
            state=state,
            **fields,
        )

    @staticmethod
    def _runtime_info_fields(
        worker_id: str,
        info: RuntimeInfo,
        *,
        last_error: str,
    ) -> dict[str, object]:
        return {
            "runtime": info.runtime,
            "model": info.model,
            "gateway_url": info.gateway_url,
            "gateway_port": info.gateway_port,
            "gateway_token": info.gateway_token,
            "session_key": info.session_key,
            "state_dir": info.state_dir,
            "workspace_dir": info.workspace_dir,
            "pid": info.pid,
            "takeover_url": f"/ui/workers/{worker_id}",
            "control_url": f"/ui/workers/{worker_id}",
            "last_error": last_error,
        }

    def _bootstrap_bundle_for(self, worker: dict) -> dict | None:
        raw = str(worker.get("bootstrap_bundle_json") or "").strip()
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    def _copy_workspace_contents(self, source_worker: dict, target_worker: dict) -> None:
        source_root_raw = str(source_worker.get("workspace_dir") or "").strip()
        target_root_raw = str(target_worker.get("workspace_dir") or "").strip()
        if not source_root_raw or not target_root_raw:
            return
        source_root = Path(source_root_raw)
        target_root = Path(target_root_raw)
        if not source_root.exists():
            return
        target_root.mkdir(parents=True, exist_ok=True)
        for item in source_root.iterdir():
            target = target_root / item.name
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True, symlinks=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, target)

    def _refresh_runtime_info(self, worker_id: str, state: str, last_error: str = "") -> dict | None:
        worker = self.store.get_worker(worker_id)
        if not worker:
            return None
        try:
            info = self.runtime.reconcile_worker(worker)
        except Exception:
            return worker
        return self._apply_runtime_info(worker_id, info, state=state, last_error=last_error) or worker

    def _instruction_for_message(self, message: str) -> str:
        return f"Operator message for the current worker session:\n\n{message}"

    def _instruction_for_steer(self, message: str) -> str:
        return (
            "Operator steer instruction for the current worker session.\n\n"
            "Treat this as the new highest-priority direction and continue from the current workspace state.\n\n"
            "Execution requirements:\n"
            "- Act on this steer inside the workspace immediately.\n"
            "- Do not stop at an acknowledgement, summary, or plan when the steer requires concrete action.\n"
            "- If the steer redirects or cancels earlier work, perform that interruption first, then carry out the new action.\n"
            "- Use the terminal, files, browser, and available tools as needed.\n"
            "- Remain in execution mode until the steer instruction is satisfied or a real blocker requires operator help.\n\n"
            f"{message}"
        )

    def _ensure_worker_processor(self, worker_id: str) -> None:
        worker = self.store.get_worker(worker_id) or {}
        if not worker or str(worker.get("state") or "") in {
            "paused",
            "needs_input",
            "stopping",
            "terminated",
        }:
            return
        if (
            str(worker.get("compute_release_token") or "").strip()
            or self.store.has_unconfirmed_host_run_start(worker_id)
        ):
            return
        executor = (
            self.conversation_executor
            if self._trusted_run_lane(worker) == "conversation"
            else self.executor
        )
        with self._processors_lock:
            if self._shutdown_event.is_set() or worker_id in self._active_processors:
                return
            generation = self._processor_generations.get(worker_id, 0) + 1
            self._processor_generations[worker_id] = generation
            self._active_processors.add(worker_id)
            try:
                executor.submit(self._process_worker_queue, worker_id, generation)
            except Exception:
                self._active_processors.discard(worker_id)
                raise

    def _processor_is_current(self, worker_id: str, generation: int) -> bool:
        with self._processors_lock:
            return worker_id in self._active_processors and self._processor_generations.get(worker_id) == generation

    def _release_processor(self, worker_id: str, generation: int) -> bool:
        with self._processors_lock:
            if worker_id not in self._active_processors:
                return False
            if self._processor_generations.get(worker_id) != generation:
                return False
            self._active_processors.discard(worker_id)
            return True

    def _ensure_surviving_run_monitor(self, worker_id: str, run_id: str) -> None:
        """Adopt one verified live restart survivor without launching a duplicate run."""

        worker = self.store.get_worker(worker_id) or {}
        executor = (
            self.conversation_executor
            if self._trusted_run_lane(worker) == "conversation"
            else self.executor
        )
        with self._processors_lock:
            if self._shutdown_event.is_set() or worker_id in self._active_processors:
                return
            generation = self._processor_generations.get(worker_id, 0) + 1
            self._processor_generations[worker_id] = generation
            self._active_processors.add(worker_id)
            try:
                executor.submit(
                    self._monitor_surviving_run,
                    worker_id,
                    run_id,
                    generation,
                )
            except Exception:
                self._active_processors.discard(worker_id)
                raise

    def _release_reconciled_run_lease(self, run_id: str, *, reason: str) -> None:
        lease = self.store.get_active_host_run_lease_for_run(run_id)
        if lease:
            self.store.release_host_run_lease(
                str(lease["lease_id"]),
                executor_id=None,
                reason=reason,
            )

    def _monitor_surviving_run(
        self,
        worker_id: str,
        run_id: str,
        generation: int,
    ) -> None:
        """Collect or truthfully requeue a process adopted after service restart."""

        interval = _bounded_float_env(
            "WPR_SURVIVOR_MONITOR_INTERVAL_S",
            0.5,
            min_value=0.01,
            max_value=10.0,
        )
        try:
            while (
                not self._shutdown_event.is_set()
                and self._processor_is_current(worker_id, generation)
            ):
                try:
                    worker = self.store.get_worker(worker_id)
                    run = self.store.get_run(run_id)
                    if not worker or not run:
                        return
                    if str(run.get("state") or "") in TERMINAL_RUN_STATES:
                        self._release_reconciled_run_lease(
                            run_id, reason="survivor_terminal"
                        )
                        return

                    recovered = self._collect_completed_run(worker, run)
                    if recovered:
                        self._apply_recovered_run(worker, run, recovered)
                        self._release_reconciled_run_lease(
                            run_id, reason="survivor_terminal"
                        )
                        return

                    runtime_worker = {**worker, "_active_run_id": run_id}
                    info = self.runtime.reconcile_worker(runtime_worker)
                    if info.pid:
                        self._apply_runtime_info(
                            worker_id,
                            info,
                            state="running",
                            last_error=worker.get("last_error") or "",
                        )
                    else:
                        # The process may have written its terminal evidence just
                        # before disappearing. Collect once more before retrying.
                        recovered = self._collect_completed_run(worker, run)
                        if recovered:
                            self._apply_recovered_run(worker, run, recovered)
                            self._release_reconciled_run_lease(
                                run_id, reason="survivor_terminal"
                            )
                            return
                        self._release_reconciled_run_lease(
                            run_id, reason="survivor_process_exited"
                        )
                        retry_after = (
                            datetime.now(timezone.utc) + timedelta(seconds=1)
                        ).isoformat()
                        requeued = self.store.requeue_run_for_retry(
                            run_id,
                            retry_after=retry_after,
                            error_text=(
                                "The adopted provider process exited before GlassHive could "
                                "confirm a terminal result."
                            ),
                            last_retry_class="provider_temporarily_unavailable",
                            failure_class="provider_temporarily_unavailable",
                            failure_retryable=1,
                            failure_structured=1,
                            failure_user_message=(
                                "The provider worker stopped before its result was confirmed; "
                                "GlassHive will retry this work."
                            ),
                            failure_recommended_recovery=(
                                "No action is required unless the retry remains queued."
                            ),
                            failure_diagnostic_summary=(
                                "A restart-adopted process disappeared without terminal evidence."
                            ),
                        )
                        self.store.update_worker_state(
                            worker_id, "ready", last_error=""
                        )
                        self.store.add_event(
                            str(worker.get("project_id") or ""),
                            worker_id,
                            run_id,
                            "run.requeued",
                            "Restart survivor exited without terminal evidence; retry queued",
                        )
                        self._emit_callback(
                            worker,
                            "run.requeued",
                            run={**run, **(requeued or {}), "state": "queued"},
                            message=(
                                "The provider worker stopped before its result was confirmed; "
                                "GlassHive will retry this work."
                            ),
                        )
                        self._scheduler_wake_event.set()
                        return
                except Exception:
                    logger.exception(
                        "Restart survivor monitor pass failed",
                        extra={"worker_id": worker_id, "run_id": run_id},
                    )
                if self._shutdown_event.wait(interval):
                    return
        finally:
            try:
                released = self._release_processor(worker_id, generation)
                if released:
                    worker = self.store.get_worker(worker_id)
                    if (
                        worker
                        and worker["state"]
                        not in {"paused", "needs_input", "stopping", "terminated"}
                        and self.store.peek_next_queued_run(worker_id)
                    ):
                        self._ensure_worker_processor(worker_id)
            except Exception:
                logger.exception(
                    "Failed to release restart survivor monitor ownership",
                    extra={"worker_id": worker_id, "run_id": run_id},
                )

    def _record_late_processor_terminal_ignored(
        self,
        worker: dict,
        run: dict,
        attempted_state: str,
    ) -> dict:
        durable_run = self.store.get_run(str(run["run_id"])) or run
        self.store.add_event(
            str(worker["project_id"]),
            str(worker["worker_id"]),
            str(run["run_id"]),
            "run.late_completion_ignored",
            (
                f"Ignored late processor {attempted_state} because the durable run state is "
                f"{str(durable_run.get('state') or 'unknown')}"
            ),
        )
        return durable_run

    def _process_worker_queue(self, worker_id: str, generation: int) -> None:
        current_run: dict | None = None
        runtime_invoked = False
        preserve_start_fence = False
        try:
            while True:
                current_run = None
                runtime_invoked = False
                preserve_start_fence = False
                if not self._processor_is_current(worker_id, generation):
                    return
                worker = self.store.get_worker(worker_id)
                if not worker or worker["state"] in {
                    "paused",
                    "needs_input",
                    "stopping",
                    "terminated",
                }:
                    return

                queued_run = self.store.peek_next_queued_run(worker_id)
                if queued_run:
                    capacity_error = self._runtime_capacity_error(worker)
                    if capacity_error:
                        self._requeue_retryable_run(worker, queued_run, capacity_error)
                        return
                    self._mark_run_local_grant_waiter(
                        worker, str(queued_run["run_id"])
                    )

                run = self.store.claim_next_queued_run(worker_id)
                if not run:
                    if queued_run:
                        self._clear_run_local_grant_waiter(
                            str(queued_run["run_id"])
                        )
                    current = self.store.get_worker(worker_id)
                    if (
                        self._processor_is_current(worker_id, generation)
                        and current
                        and current["state"] not in {
                            "paused",
                            "needs_input",
                            "stopping",
                            "terminated",
                            "failed",
                        }
                        and not self.store.get_active_run(worker_id)
                    ):
                        self.store.update_worker_state(worker_id, "ready", last_error="")
                    return

                current_run = run
                worker = self.store.get_worker(worker_id) or worker
                if queued_run and str(queued_run.get("run_id") or "") != str(
                    run.get("run_id") or ""
                ):
                    self._clear_run_local_grant_waiter(
                        str(queued_run.get("run_id") or "")
                    )
                    self._mark_run_local_grant_waiter(
                        worker, str(run["run_id"])
                    )
                try:
                    lease = self._acquire_host_run_lease(worker, run)
                    if not lease:
                        raise RuntimeErrorBase(
                            "GlassHive could not reserve the exact run startup generation."
                        )
                    capacity_error = self._runtime_capacity_error(worker)
                    if capacity_error:
                        raise capacity_error
                    authority_context: dict[str, str] = {}
                    if self._deferred_capability_authorization(worker) is not None:
                        prepare_authority = getattr(
                            self.runtime, "prepare_run_authority_context", None
                        )
                        if not callable(prepare_authority):
                            raise BrokerAdmissionError(
                                "broker_admission_generation_unavailable",
                                "The exact mission container generation is unavailable.",
                                retryable=True,
                            )
                        prepared = prepare_authority(worker, run_id=str(run["run_id"]))
                        if isinstance(prepared, dict):
                            authority_context = {
                                str(key): str(value)
                                for key, value in prepared.items()
                            }
                    run_worker = {
                        **self._run_local_worker(
                            worker, run, authority_context=authority_context
                        ),
                        # Persist only a one-way binding in private active-session
                        # state. The raw startup CAS token remains in SQLite.
                        "_run_startup_token_digest": hashlib.sha256(
                            str(lease.get("startup_token") or "").encode("utf-8")
                        ).hexdigest(),
                    }
                except HostCapacityError as exc:
                    self._clear_run_local_grant_waiter(str(run["run_id"]))
                    self._release_host_run_lease(
                        str(run["run_id"]), reason="capacity_wait"
                    )
                    self._requeue_retryable_run(
                        worker,
                        run,
                        exc,
                        failure_fields=classify_runtime_error(
                            exc,
                            runtime_name=str(
                                worker.get("profile")
                                or worker.get("runtime")
                                or "worker"
                            ),
                        ).as_store_fields(),
                    )
                    return
                except BrokerAdmissionError as exc:
                    self._clear_run_local_grant_waiter(str(run["run_id"]))
                    self._release_host_run_lease(
                        str(run["run_id"]),
                        reason=(
                            "broker_admission_retry"
                            if exc.retryable and not exc.needs_input
                            else "broker_admission_rejected"
                        ),
                    )
                    failure_fields = {
                        "failure_class": exc.code,
                        "failure_retryable": exc.retryable,
                        "failure_structured": True,
                        "failure_user_message": str(exc),
                        "failure_recommended_recovery": (
                            "Provide the requested authorization, then resume this work."
                            if exc.needs_input
                            else "Retry this work after the broker admission service recovers."
                            if exc.retryable
                            else "Review the capability authorization and retry this work."
                        ),
                        "failure_diagnostic_summary": "Deferred broker admission rejected the exact run binding.",
                    }
                    if exc.retryable and not exc.needs_input:
                        self._requeue_retryable_run(
                            worker,
                            run,
                            exc,
                            failure_fields=failure_fields,
                        )
                    elif exc.needs_input:
                        blocked_run = self.store.mark_run_needs_input(
                            str(run["run_id"]),
                            error_text=str(exc),
                            failure_class=exc.code,
                            failure_user_message=str(exc),
                        ) or self.store.get_run(str(run["run_id"])) or run
                        self.store.finalize_schedule_for_run(
                            str(run["run_id"]),
                            state="needs_input",
                            last_error=str(exc),
                        )
                        self.store.update_worker_state(
                            worker_id, "needs_input", last_error=str(exc)
                        )
                        needs_input_worker = self.store.get_worker(worker_id) or worker
                        self._release_needs_input_compute(
                            needs_input_worker,
                            {**run, **blocked_run},
                        )
                        self.store.add_event(
                            str(worker["project_id"]),
                            worker_id,
                            str(run["run_id"]),
                            "run.needs_input",
                            str(exc),
                            payload={"failureCode": exc.code},
                        )
                        self._emit_callback(
                            worker,
                            "run.needs_input",
                            run={**run, **blocked_run},
                            message=str(exc),
                        )
                    else:
                        failed_run = self.store.finalize_run_if_state(
                            str(run["run_id"]),
                            "running",
                            "failed",
                            error_text=str(exc),
                            **failure_fields,
                        ) or self.store.get_run(str(run["run_id"])) or run
                        self.store.finalize_schedule_for_run(
                            str(run["run_id"]), state="failed", last_error=str(exc)
                        )
                        self.store.update_worker_state(
                            worker_id, "ready", last_error=str(exc)
                        )
                        self.store.add_event(
                            str(worker["project_id"]),
                            worker_id,
                            str(run["run_id"]),
                            "run.failed",
                            str(exc),
                            payload={"failureCode": exc.code},
                        )
                        self._emit_callback(
                            worker,
                            "run.failed",
                            run={**run, **failed_run},
                            message=str(exc),
                        )
                    return
                callback_record, callbacks = self._run_start_callback_record(
                    worker,
                    run,
                    str(lease.get("startup_token") or ""),
                )
                lifecycle_guard = self._acquire_worker_lifecycle_guard(worker_id)
                reservation = self.store.validate_host_run_start_reservation(
                    worker_id=worker_id,
                    run_id=str(run["run_id"]),
                    run_started_at=str(run.get("started_at") or ""),
                    lease_id=str(lease.get("lease_id") or ""),
                    startup_token=str(lease.get("startup_token") or ""),
                    executor_id=self._executor_id,
                )
                if reservation is None:
                    lifecycle_guard.release()
                    clear_run_grant = getattr(
                        self.runtime, "clear_run_local_capability_grant", None
                    )
                    if callable(clear_run_grant):
                        clear_run_grant(worker)
                    self._release_host_run_lease(
                        str(run["run_id"]), reason="startup_fenced"
                    )
                    return
                pending_start: dict[str, object] = {
                    "worker_id": worker_id,
                    "run_id": str(run["run_id"]),
                    "run_started_at": str(run.get("started_at") or ""),
                    "lease_id": str(lease.get("lease_id") or ""),
                    "startup_token": str(lease.get("startup_token") or ""),
                    "worker": dict(worker),
                    "callback_record": callback_record,
                    "callbacks": callbacks,
                    "guard": lifecycle_guard,
                    "confirmed": False,
                }
                with self._pending_run_starts_lock:
                    self._pending_run_starts[str(run["run_id"])] = pending_start
                try:
                    try:
                        requires_identity = bool(
                            getattr(
                                self.runtime,
                                "requires_run_start_identity",
                                True,
                            )
                        )
                        if requires_identity:
                            if not self._run_start_observer_supported:
                                raise RunStartupRejectedError(
                                    "The runtime cannot publish an exact startup identity.",
                                    termination_confirmed=True,
                                )
                        else:
                            self._confirm_in_process_run_start(pending_start)
                            worker = (
                                self._refresh_runtime_info(
                                    worker_id,
                                    state="running",
                                    last_error="",
                                )
                                or self.store.get_worker(worker_id)
                                or worker
                            )
                        runtime_invoked = True
                        try:
                            output = self.runtime.run_task(
                                run_worker,
                                run["instruction"],
                                run_id=run["run_id"],
                            )
                        except TypeError as exc:
                            if "run_id" not in str(exc):
                                raise
                            output = self.runtime.run_task(
                                run_worker, run["instruction"]
                            )
                        with self._pending_run_starts_lock:
                            confirmed = bool(
                                (
                                    self._pending_run_starts.get(
                                        str(run["run_id"])
                                    )
                                    or {}
                                ).get("confirmed")
                            )
                        if not confirmed:
                            raise RunStartupRejectedError(
                                "The runtime returned without publishing its exact startup identity.",
                                termination_confirmed=False,
                            )
                    except RunStartupRejectedError as exc:
                        preserve_start_fence = not exc.termination_confirmed
                        if preserve_start_fence:
                            self.store.mark_host_run_start_termination_unconfirmed(
                                lease_id=str(lease.get("lease_id") or ""),
                                run_id=str(run["run_id"]),
                                executor_id=self._executor_id,
                                startup_token=str(lease.get("startup_token") or ""),
                            )
                        raise
                    finally:
                        with self._pending_run_starts_lock:
                            self._pending_run_starts.pop(
                                str(run["run_id"]), None
                            )
                        lifecycle_guard.release()
                        clear_run_grant = getattr(
                            self.runtime, "clear_run_local_capability_grant", None
                        )
                        if callable(clear_run_grant):
                            try:
                                clear_run_grant(worker)
                            except Exception:
                                logger.exception(
                                    "Failed to clear run-local capability grant for worker %s",
                                    worker_id,
                                )
                        try:
                            self._revoke_run_local_capability_grant(run_worker)
                        except Exception:
                            logger.exception(
                                "Failed to revoke run-local capability grant for worker %s",
                                worker_id,
                            )
                        if not preserve_start_fence:
                            self._release_host_run_lease(
                                str(run["run_id"]), reason="runtime_returned"
                            )
                except RunStartupRejectedError as exc:
                    if not exc.termination_confirmed:
                        return
                    retry_error = RuntimeErrorBase(
                        "GlassHive safely stopped a startup attempt that lost durable ownership."
                    )
                    self._requeue_retryable_run(
                        self.store.get_worker(worker_id) or worker,
                        self.store.get_run(str(run["run_id"])) or run,
                        retry_error,
                        failure_fields={
                            "failure_class": "service_startup_fenced",
                            "failure_retryable": 1,
                            "failure_structured": 1,
                            "failure_user_message": (
                                "GlassHive safely recovered an interrupted worker startup and will retry."
                            ),
                            "failure_recommended_recovery": (
                                "No action is required unless this work remains queued."
                            ),
                            "failure_diagnostic_summary": (
                                "The provider startup was stopped before its durable identity was accepted."
                            ),
                        },
                    )
                    return
                except WorkerPausedError as exc:
                    if not self._processor_is_current(worker_id, generation):
                        return
                    paused_run = self.store.transition_run_if_state(
                        str(run["run_id"]),
                        "running",
                        "paused",
                        ended_at=None,
                        error_text=str(exc),
                    )
                    durable = paused_run or self.store.get_run(str(run["run_id"])) or run
                    durable_state = str(durable.get("state") or "")
                    current_worker = self.store.get_worker(worker_id) or worker
                    if (
                        not paused_run
                        and str(current_worker.get("compute_release_token") or "")
                        and str(
                            current_worker.get("compute_release_target_run_id") or ""
                        )
                        == str(run["run_id"])
                    ):
                        return
                    if durable_state in TERMINAL_RUN_STATES:
                        self.store.update_worker_state(worker_id, "ready", last_error="")
                        return
                    if durable_state == "queued":
                        # A host resume may requeue the exact run before the
                        # killed provider unwinds. Preserve that newer CAS; the
                        # processor-finally path starts its replacement.
                        self.store.update_worker_state(worker_id, "starting", last_error="")
                        return
                    self.store.update_worker_state(worker_id, "paused", last_error="")
                    self.store.add_event(worker["project_id"], worker_id, run["run_id"], "run.paused", str(exc))
                    self._emit_callback(worker, "run.paused", run={**run, "state": "paused", "error_text": str(exc)}, message=str(exc))
                    return
                except WorkerInterruptedError as exc:
                    if not self._processor_is_current(worker_id, generation):
                        return
                    current_worker = self.store.get_worker(worker_id) or worker
                    stop_requested = current_worker.get("state") == "stopping"
                    final_state = "cancelled" if stop_requested else "interrupted"
                    finalized_run = self.store.finalize_run_if_state(
                        run["run_id"],
                        "running",
                        state=final_state,
                        error_text=str(exc),
                    )
                    if not finalized_run:
                        self._record_late_processor_terminal_ignored(
                            worker,
                            run,
                            "interruption",
                        )
                        continue
                    self.store.finalize_schedule_for_run(
                        run["run_id"],
                        state="cancelled" if stop_requested else "failed",
                        last_error=str(exc),
                    )
                    self.store.update_worker_state(worker_id, "ready", last_error="")
                    if stop_requested:
                        self.store.accept_cancel_actions_for_run(run["run_id"])
                    event_type = f"run.{final_state}"
                    self.store.add_event(
                        worker["project_id"], worker_id, run["run_id"], event_type, str(exc)
                    )
                    self._emit_callback(
                        worker,
                        event_type,
                        run={**run, **finalized_run},
                        message=str(exc),
                    )
                    continue
                except WorkerTerminatedError as exc:
                    if not self._processor_is_current(worker_id, generation):
                        return
                    recovered = self._collect_completed_run(worker, run)
                    if recovered:
                        self._apply_recovered_run(worker, run, recovered)
                        continue
                    finalized_run = self.store.finalize_run_if_state(
                        run["run_id"],
                        str(run.get("state") or "running"),
                        state="cancelled",
                        error_text=str(exc),
                    )
                    if not finalized_run:
                        self._record_late_processor_terminal_ignored(
                            worker, run, "termination"
                        )
                        return
                    self.store.finalize_schedule_for_run(run["run_id"], state="cancelled", last_error=str(exc))
                    self.store.update_worker_state(worker_id, "terminated", last_error=str(exc))
                    self.store.add_event(worker["project_id"], worker_id, run["run_id"], "run.cancelled", str(exc))
                    self._emit_callback(worker, "run.cancelled", run={**run, "state": "cancelled", "error_text": str(exc)}, message=str(exc))
                    return
                except RuntimeErrorBase as exc:
                    if not self._processor_is_current(worker_id, generation):
                        return
                    current_worker = self.store.get_worker(worker_id) or worker
                    worker_state = current_worker["state"]
                    final_state = "failed"
                    if worker_state == "paused":
                        final_state = "interrupted"
                    elif worker_state == "terminated":
                        final_state = "cancelled"
                    refreshed_worker = (
                        self._refresh_runtime_info(
                            worker_id,
                            state=worker_state if worker_state in {"paused", "terminated"} else "ready",
                            last_error=str(exc),
                        )
                        or self.store.get_worker(worker_id)
                        or current_worker
                    )
                    failure_fields = (
                        classify_runtime_error(
                            exc,
                            runtime_name=str(refreshed_worker.get("profile") or refreshed_worker.get("runtime") or "worker"),
                        ).as_store_fields()
                        if final_state == "failed"
                        else {}
                    )
                    if (
                        final_state == "failed"
                        and str(failure_fields.get("failure_class") or "") != "glasshive_evidence_check_failed"
                    ):
                        recovered = self._collect_completed_run(refreshed_worker, run)
                        if recovered:
                            self._apply_recovered_run(refreshed_worker, run, recovered)
                            continue
                    if (
                        final_state == "failed"
                        and bool(failure_fields.get("failure_retryable"))
                        and str(failure_fields.get("failure_class") or "")
                        in {"host_worker_busy", "host_capacity", "provider_rate_limited"}
                        and (
                            str(failure_fields.get("failure_class") or "")
                            != "provider_rate_limited"
                            or getattr(exc, "retry_after_s", None) is not None
                        )
                    ):
                        self._requeue_retryable_run(refreshed_worker, run, exc, failure_fields=failure_fields)
                        return
                    finalized_run = self.store.finalize_run_if_state(
                        run["run_id"],
                        "running",
                        final_state,
                        error_text=str(exc),
                        **failure_fields,
                    )
                    if not finalized_run:
                        self._record_late_processor_terminal_ignored(
                            current_worker,
                            run,
                            final_state,
                        )
                        if worker_state in {"paused", "terminated"}:
                            return
                        continue
                    self.store.finalize_schedule_for_run(
                        run["run_id"],
                        state="cancelled" if final_state == "cancelled" else "failed",
                        last_error=str(exc),
                    )
                    self.store.update_worker_state(worker_id, worker_state if worker_state in {"paused", "terminated"} else "ready", last_error=str(exc))
                    self.store.add_event(worker["project_id"], worker_id, run["run_id"], f"run.{final_state}", str(exc))
                    failed_run = {
                        **run,
                        **finalized_run,
                        "state": final_state,
                        "error_text": str(exc),
                        **failure_fields,
                    }
                    callback_worker = self.store.get_worker(worker_id) or refreshed_worker
                    deliverable = (
                        self._completion_deliverable(callback_worker, failed_run, "", str(exc))
                        if final_state == "failed"
                        else None
                    )
                    failure_message = runtime_failure_callback_message(failure_fields, str(exc))
                    self._emit_callback(
                        callback_worker,
                        f"run.{final_state}",
                        run=failed_run,
                        message=failure_message,
                        deliverable=deliverable,
                    )
                    if worker_state in {"paused", "terminated"}:
                        return
                    continue
                except Exception as exc:
                    if not self._processor_is_current(worker_id, generation):
                        return
                    failure_fields = classify_runtime_error(
                        exc,
                        runtime_name=str(worker.get("profile") or worker.get("runtime") or "worker"),
                    ).as_store_fields()
                    finalized_run = self.store.finalize_run_if_state(
                        run["run_id"],
                        "running",
                        "failed",
                        error_text=str(exc),
                        **failure_fields,
                    )
                    if not finalized_run:
                        self._record_late_processor_terminal_ignored(worker, run, "failed")
                        continue
                    self.store.finalize_schedule_for_run(run["run_id"], state="failed", last_error=str(exc))
                    self.store.update_worker_state(worker_id, "ready", last_error=str(exc))
                    self.store.add_event(worker["project_id"], worker_id, run["run_id"], "run.failed", str(exc))
                    failed_run = {
                        **run,
                        **finalized_run,
                        "state": "failed",
                        "error_text": str(exc),
                        **failure_fields,
                    }
                    failure_message = runtime_failure_callback_message(failure_fields, str(exc))
                    self._emit_callback(worker, "run.failed", run=failed_run, message=failure_message)
                    continue

                if not self._processor_is_current(worker_id, generation):
                    return
                completion_expected_state = self._settle_native_children(
                    worker,
                    run,
                    output,
                )
                if completion_expected_state not in {"running", "settling"}:
                    self._record_late_processor_terminal_ignored(
                        worker,
                        run,
                        "completion",
                    )
                    continue
                completed_run = self.store.finalize_run_if_state(
                    run["run_id"],
                    completion_expected_state,
                    "completed",
                    output_text=output,
                )
                if not completed_run:
                    self._record_late_processor_terminal_ignored(worker, run, "completion")
                    continue
                self.store.finalize_schedule_for_run(run["run_id"], state="completed")
                self.store.update_worker(worker_id, state="ready", last_error="", last_run_id=run["run_id"])
                message = terminal_callback_message(output)
                full_message = terminal_callback_full_message(output)
                self.store.add_event(worker["project_id"], worker_id, run["run_id"], "run.completed", message[:TERMINAL_CALLBACK_MESSAGE_LIMIT] or "Run completed")
                completed_run = {**run, **completed_run, "state": "completed", "output_text": output}
                refreshed_worker = self._refresh_runtime_info(worker_id, state="ready", last_error="") or self.store.get_worker(worker_id) or worker
                deliverable = self._completion_deliverable(refreshed_worker, completed_run, output)
                self._promote_completed_deliverable(refreshed_worker, completed_run, deliverable)
                self._emit_callback(
                    refreshed_worker,
                    "run.completed",
                    run=completed_run,
                    message=message or "Run completed",
                    full_message=full_message if full_message != message else "",
                    deliverable=deliverable,
                )
                current_run = None
                runtime_invoked = False
        except Exception as exc:
            logger.exception(
                "Unexpected GlassHive worker processor failure",
                extra={
                    "worker_id": worker_id,
                    "run_id": str((current_run or {}).get("run_id") or ""),
                },
            )
            if current_run and not preserve_start_fence:
                try:
                    durable_run = self.store.get_run(str(current_run["run_id"]))
                    if durable_run and str(durable_run.get("state") or "") == "running":
                        recovered = (
                            self._collect_completed_run(
                                self.store.get_worker(worker_id) or {}, durable_run
                            )
                            if runtime_invoked
                            else None
                        )
                        if recovered:
                            self._apply_recovered_run(
                                self.store.get_worker(worker_id) or {},
                                durable_run,
                                recovered,
                            )
                        elif not runtime_invoked:
                            recovery_error = RuntimeErrorBase(
                                "GlassHive recovered an internal processor interruption "
                                "before provider execution started."
                            )
                            self._requeue_retryable_run(
                                self.store.get_worker(worker_id) or {
                                    "worker_id": worker_id,
                                    "project_id": str(
                                        durable_run.get("project_id") or ""
                                    ),
                                },
                                durable_run,
                                recovery_error,
                                failure_fields={
                                    "failure_class": "service_processor_unexpected",
                                    "failure_retryable": 1,
                                    "failure_structured": 1,
                                    "failure_user_message": (
                                        "GlassHive recovered an internal worker interruption and "
                                        "will retry this work."
                                    ),
                                    "failure_recommended_recovery": (
                                        "No action is required unless the work remains queued."
                                    ),
                                    "failure_diagnostic_summary": (
                                        "The processor exited before invoking the provider runtime."
                                    ),
                                },
                            )
                        else:
                            failure_message = (
                                "GlassHive could not safely confirm the provider result after "
                                "an internal processor interruption."
                            )
                            failed_run = self.store.finalize_run_if_state(
                                str(durable_run["run_id"]),
                                "running",
                                "failed",
                                error_text=failure_message,
                                failure_class="service_processor_unexpected",
                                failure_retryable=1,
                                failure_structured=1,
                                failure_user_message=failure_message,
                                failure_recommended_recovery=(
                                    "Retry the work after reviewing any partial workspace output."
                                ),
                                failure_diagnostic_summary=(
                                    "The provider runtime returned, but processor finalization "
                                    "did not complete."
                                ),
                            )
                            if failed_run:
                                self.store.finalize_schedule_for_run(
                                    str(durable_run["run_id"]),
                                    state="failed",
                                    last_error=failure_message,
                                )
                                failed_worker = (
                                    self.store.update_worker_state(
                                        worker_id,
                                        "ready",
                                        last_error=failure_message,
                                    )
                                    or self.store.get_worker(worker_id)
                                    or {}
                                )
                                self.store.add_event(
                                    str(failed_worker.get("project_id") or ""),
                                    worker_id,
                                    str(durable_run["run_id"]),
                                    "run.failed",
                                    failure_message,
                                )
                                self._emit_callback(
                                    failed_worker,
                                    "run.failed",
                                    run={**durable_run, **failed_run},
                                    message=failure_message,
                                )
                except Exception:
                    logger.exception(
                        "Failed to durably recover unexpected worker processor failure",
                        extra={"worker_id": worker_id},
                    )
        finally:
            if current_run and not preserve_start_fence:
                try:
                    self._release_host_run_lease(
                        str(current_run["run_id"]), reason="processor_exit"
                    )
                except Exception:
                    logger.exception(
                        "Failed to release run lease after worker processor exit",
                        extra={
                            "worker_id": worker_id,
                            "run_id": str(current_run.get("run_id") or ""),
                        },
                    )
            try:
                if self._release_processor(worker_id, generation):
                    pending = self.store.get_worker(worker_id)
                    if (
                        pending
                        and pending["state"]
                        not in {"paused", "needs_input", "stopping", "terminated"}
                        and not str(pending.get("compute_release_token") or "").strip()
                        and not self.store.has_unconfirmed_host_run_start(worker_id)
                    ):
                        if self.store.peek_next_queued_run(worker_id):
                            self._ensure_worker_processor(worker_id)
            except Exception:
                logger.exception(
                    "Failed to finalize GlassHive worker processor ownership",
                    extra={"worker_id": worker_id},
                )
