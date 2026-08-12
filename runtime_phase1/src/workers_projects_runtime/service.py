from __future__ import annotations

import base64
import binascii
import json
import hashlib
import hmac
import logging
import os
import re
import shutil
import stat
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Lock, Thread, Timer
from urllib.parse import urlencode, urlparse

import httpx

from .auth import multi_user_security_enabled

from .deliverables import (
    PROFESSIONAL_ARTIFACT_EXTENSIONS,
    SUPPORT_ARTIFACT_DIR_NAMES,
    deliverable_payload,
    is_user_deliverable_relative_path,
    is_valid_professional_artifact,
)
from .control_plane import (
    PROFILE_ACCOUNT_PROVIDERS,
    WORKSPACE_ACCOUNT_POLICIES,
    ControlPlaneConflict,
    ControlPlaneStore,
)
from .failure_classification import classify_runtime_error
from .models import WorkspaceKind, normalize_workspace_kind, normalize_workspace_tags, utc_now
from .mission_provider_accounts import mission_provider_account_selection
from .openclaw_runtime import (
    RuntimeErrorBase,
    RuntimeInfo,
    WorkerInterruptedError,
    WorkerPausedError,
    WorkerRuntime,
    WorkerTerminatedError,
)
from .operator_urls import surface_aware_watch_url
from .recurrence import (
    DELEGATED_RECURRENCE_OWNER,
    NATIVE_RECURRENCE_OWNER,
    RECURRENCE_OWNERS,
    canonical_recurrence_owner,
    due_occurrences_and_next,
    first_occurrence_at,
    normalize_recurrence_spec,
    parse_aware_utc,
    recurrence_owner_storage_value,
)
from .runtime_env import load_viventium_runtime_env
from .runtime_identity import derive_legacy_backend_label
from .run_actions import (
    RunActionError,
    mint_run_action_capability,
    unverified_run_action_claims,
    verify_run_action_capability,
)
from .scheduling_owner import SchedulingOwnerIdentity, ViventiumSchedulingOwnerClient
from .signed_links import (
    append_signed_query,
    create_signed_link_ref,
    revoke_signed_link_refs_for_worker,
    sign_link_params,
    signed_link_ref_url,
    sign_link_token,
)
from .store import SchedulePrincipalAuthorityStoreError, Store, WorkerClosedStoreError
from .workspace_continuation import continuation_instruction


logger = logging.getLogger(__name__)
CLOSED_WORKER_STATES = {"terminating", "termination_failed", "terminated"}
TERMINAL_CALLBACK_MESSAGE_LIMIT = 4000
FINAL_REPORT_PATTERN = re.compile(
    r"(?mi)^[ \t]*(?:#{1,6}[ \t]+|>[ \t]*)?"
    r"(?:(?:[*_]{1,3}|`{1,3})[ \t]*)?FINAL REPORT\s*:\s*"
    r"(?:(?:[*_]{1,3}|`{1,3})[ \t]*)?"
)
VIVENTIUM_CALLBACK_PATH = "/api/viventium/glasshive/callback"
SCHEDULING_CORTEX_CALLBACK_PATH = "/internal/scheduled-prompts/glasshive-callback"
ACTIONABLE_CALLBACK_LINK_EVENTS = {"run.failed", "run.paused", "run.interrupted", "run.cancelled"}
PARENT_VISIBLE_CALLBACK_FIELDS = ("user_id", "conversation_id", "parent_message_id", "message_id")
CALLBACK_DEAD_LETTER_IMMEDIATE_STATUS_CODES = {400, 401, 403, 404, 410, 422, 501}
CALLBACK_RETRYABLE_STATUS_CODES = {408, 425, 429}
RUN_STATE_BY_EVENT = {
    "run.queued": "queued",
    "run.waiting_on_capacity": "queued",
    "run.started": "running",
    "run.completed": "completed",
    "run.failed": "failed",
    "run.paused": "paused",
    "run.interrupted": "interrupted",
    "run.cancelled": "cancelled",
}
_UNSET = object()


class SchedulePrincipalAuthorityError(ValueError):
    """A current principal no longer authorizes unattended schedule execution."""

    failure_class = "principal_disabled"


class ScheduleActionRequiredError(ValueError):
    """A user-owned account or capability must be repaired before scheduled work can run."""

    def __init__(self, failure_class: str, message: str, recovery: str) -> None:
        super().__init__(message)
        self.failure_class = failure_class
        self.user_message = message
        self.recovery = recovery


_DUPLICATE_EXCLUDED_PATH_NAMES = frozenset(
    {
        ".aws",
        ".azure",
        ".cache",
        ".claude",
        ".claude.json",
        ".codex",
        ".config",
        ".git",
        ".glasshive",
        ".glasshive-runs",
        ".hg",
        ".local",
        ".mcp.json",
        ".netrc",
        ".npmrc",
        ".pypirc",
        ".ssh",
        ".svn",
        ".venv",
        "auth.json",
        "cookies",
        "cookies.sqlite",
        "credentials",
        "credentials.json",
        "local state",
        "login data",
        "mcp.json",
        "node_modules",
        "session",
        "session.json",
        "sessions",
        "token.json",
        "web data",
    }
)


def _encode_workspace_cursor(worker: dict) -> str:
    payload = {
        "v": 1,
        "favorite": 1 if bool(worker.get("favorite")) else 0,
        "activity": str(worker.get("last_activity_at") or worker.get("updated_at") or ""),
        "worker_id": str(worker.get("worker_id") or ""),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_workspace_cursor(cursor: str | None) -> tuple[int | None, str, str]:
    clean_cursor = str(cursor or "").strip()
    if not clean_cursor:
        return None, "", ""
    try:
        padded = clean_cursor + ("=" * (-len(clean_cursor) % 4))
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        favorite = int(payload["favorite"])
        activity = str(payload["activity"])
        worker_id = str(payload["worker_id"])
    except (ValueError, TypeError, KeyError, UnicodeError, json.JSONDecodeError, binascii.Error) as exc:
        raise ValueError("workspace catalog cursor is invalid") from exc
    if payload.get("v") != 1 or favorite not in {0, 1} or not activity or not worker_id:
        raise ValueError("workspace catalog cursor is invalid")
    return favorite, activity, worker_id


def _workspace_duplicate_path_is_excluded(relative_path: Path) -> bool:
    for part in relative_path.parts:
        normalized = part.casefold()
        if normalized in _DUPLICATE_EXCLUDED_PATH_NAMES:
            return True
        if normalized == ".env" or normalized.startswith(".env."):
            return True
    return False


def _require_path_within_root(path: Path, root: Path, *, label: str) -> Path:
    resolved = path.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(label) from exc
    return resolved


def _workspace_copy_plan(source_root: Path) -> tuple[list[tuple[Path, Path]], int, str]:
    if source_root.is_symlink():
        raise ValueError("unsafe workspace symlink: source root")
    if not source_root.exists():
        return [], 0, "missing"
    if not source_root.is_dir():
        raise ValueError("workspace duplicate source must be a directory")
    files: list[tuple[Path, Path]] = []
    max_files = _bounded_int_env("GLASSHIVE_DUPLICATE_MAX_FILES", 5_000, min_value=1, max_value=100_000)
    max_bytes = _bounded_int_env(
        "GLASSHIVE_DUPLICATE_MAX_BYTES",
        512 * 1024 * 1024,
        min_value=1024,
        max_value=20 * 1024 * 1024 * 1024,
    )
    max_depth = _bounded_int_env(
        "GLASSHIVE_DUPLICATE_MAX_DEPTH",
        64,
        min_value=1,
        max_value=1_024,
    )
    timeout_seconds = _bounded_float_env(
        "GLASSHIVE_DUPLICATE_TIMEOUT_SECONDS",
        30.0,
        min_value=1.0,
        max_value=300.0,
    )
    deadline = time.monotonic() + timeout_seconds
    total_bytes = 0
    skipped_items = 0
    found_items = False
    pending = [source_root]
    while pending:
        if time.monotonic() > deadline:
            raise ValueError("workspace duplicate preflight exceeded its time limit")
        directory = pending.pop()
        try:
            children = sorted(directory.iterdir(), key=lambda value: value.name.casefold())
        except OSError as exc:
            relative_directory = directory.relative_to(source_root)
            raise ValueError(
                f"workspace directory could not be inspected: {relative_directory or Path('.')}"
            ) from exc
        for item in children:
            found_items = True
            relative = item.relative_to(source_root)
            if _workspace_duplicate_path_is_excluded(relative):
                skipped_items += 1
                continue
            if len(relative.parts) > max_depth:
                raise ValueError("workspace duplicate exceeds the configured depth limit")
            try:
                item_stat = item.lstat()
            except OSError as exc:
                raise ValueError(f"workspace item could not be inspected: {relative}") from exc
            if stat.S_ISLNK(item_stat.st_mode):
                try:
                    link_target = Path(os.readlink(item))
                    # Path.resolve(strict=False) stopped raising for symlink loops in
                    # Python 3.13. Follow the link once with stat so dangling and
                    # looping links fail closed on every supported interpreter.
                    item.stat()
                    resolved_target = item.resolve(strict=False)
                    resolved_root = source_root.resolve(strict=True)
                except (OSError, RuntimeError) as exc:
                    raise ValueError(f"unsafe workspace symlink: {relative}") from exc
                if link_target.is_absolute():
                    raise ValueError(f"unsafe workspace symlink: {relative}")
                try:
                    resolved_target.relative_to(resolved_root)
                except ValueError as exc:
                    raise ValueError(f"unsafe workspace symlink: {relative}") from exc
                skipped_items += 1
                continue
            mode = item_stat.st_mode
            if stat.S_ISDIR(mode):
                pending.append(item)
            elif stat.S_ISREG(mode):
                if item_stat.st_nlink > 1:
                    raise ValueError(f"workspace item is not an independent regular file: {relative}")
                files.append((item, relative))
                if len(files) > max_files:
                    raise ValueError("workspace duplicate exceeds the configured file limit")
                total_bytes += int(item_stat.st_size)
                if total_bytes > max_bytes:
                    raise ValueError("workspace duplicate exceeds the configured byte limit")
            else:
                raise ValueError(f"workspace item is not a regular file or directory: {relative}")
    if files:
        source_state = "copied"
    elif found_items:
        source_state = "filtered"
    else:
        source_state = "empty"
    return files, skipped_items, source_state


def _copy_regular_workspace_file(
    source: Path,
    target: Path,
    source_root: Path,
    *,
    max_bytes: int,
    deadline: float,
) -> int:
    root = source_root.resolve(strict=True)
    if source.is_symlink():
        raise ValueError(f"unsafe workspace file changed during duplicate: {source.relative_to(source_root)}")
    resolved_source = _require_path_within_root(
        source,
        root,
        label=f"unsafe workspace file changed during duplicate: {source.relative_to(source_root)}",
    )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(resolved_source, flags)
    temporary_target = target.with_name(f".{target.name}.glasshive-copy-{uuid.uuid4().hex}")
    try:
        opened_stat = os.fstat(descriptor)
        if not stat.S_ISREG(opened_stat.st_mode) or opened_stat.st_nlink > 1:
            raise ValueError(f"workspace item is not a regular file: {source.relative_to(source_root)}")
        target.parent.mkdir(parents=True, exist_ok=True)
        copied_bytes = 0
        with os.fdopen(descriptor, "rb", closefd=False) as source_handle, temporary_target.open("xb") as target_handle:
            while True:
                if time.monotonic() > deadline:
                    raise ValueError("workspace duplicate copy exceeded its time limit")
                remaining = max_bytes - copied_bytes
                if remaining < 0:
                    raise ValueError("workspace duplicate exceeds the configured byte limit")
                chunk = source_handle.read(min(1024 * 1024, remaining + 1))
                if not chunk:
                    break
                copied_bytes += len(chunk)
                if copied_bytes > max_bytes:
                    raise ValueError("workspace duplicate exceeds the configured byte limit")
                target_handle.write(chunk)
        os.replace(temporary_target, target)
        return copied_bytes
    finally:
        os.close(descriptor)
        try:
            temporary_target.unlink()
        except FileNotFoundError:
            pass


def _duplicate_bootstrap_bundle(bundle: dict | None) -> dict | None:
    if not isinstance(bundle, dict):
        return None
    project_definition = bundle.get("project_definition")
    if not isinstance(project_definition, str) or not project_definition.strip():
        return None
    return {"project_definition": project_definition}


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
    return multi_user_security_enabled()


def _reconcile_on_startup_enabled() -> bool:
    configured = str(os.environ.get("GLASSHIVE_RECONCILE_ON_STARTUP") or "").strip().lower()
    if not configured:
        return not _enterprise_mode_enabled()
    if configured in {"1", "true", "yes", "on"}:
        return True
    if configured in {"0", "false", "no", "off"}:
        return False
    raise ValueError("GLASSHIVE_RECONCILE_ON_STARTUP must be true or false")


def _background_consumers_enabled() -> bool:
    configured = str(os.environ.get("GLASSHIVE_BACKGROUND_CONSUMERS_ENABLED") or "true").strip().lower()
    if configured in {"1", "true", "yes", "on"}:
        return True
    if configured in {"0", "false", "no", "off"}:
        return False
    raise ValueError("GLASSHIVE_BACKGROUND_CONSUMERS_ENABLED must be true or false")


def _recurring_schedule_owner() -> str:
    load_viventium_runtime_env()
    configured = str(os.environ.get("GLASSHIVE_RECURRING_SCHEDULE_OWNER") or "").strip().lower()
    viventium_deployment = bool(
        str(os.environ.get("VIVENTIUM_ENV_FILE") or "").strip()
        or str(os.environ.get("VIVENTIUM_GLASSHIVE_CALLBACK_URL") or "").strip()
    )
    if configured and configured not in RECURRENCE_OWNERS:
        raise ValueError(
            "GLASSHIVE_RECURRING_SCHEDULE_OWNER must be glasshive_native or viventium_cortex"
        )
    configured_owner = canonical_recurrence_owner(configured) if configured else ""
    if viventium_deployment:
        if configured_owner == NATIVE_RECURRENCE_OWNER:
            raise ValueError("Viventium deployments must delegate recurrence to Viventium Cortex")
        return DELEGATED_RECURRENCE_OWNER
    return configured_owner or NATIVE_RECURRENCE_OWNER


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
    for key, value in incoming.items():
        current = merged.get(key)
        if key == "files":
            merged[key] = _merge_file_entries(current, value)
        elif isinstance(current, dict) and isinstance(value, dict):
            merged[key] = merge_bootstrap_bundle(current, value) or {}
        else:
            merged[key] = value
    return merged


def _required_capability_servers(bundle: dict | None) -> list[str]:
    if not isinstance(bundle, dict):
        return []
    broker = bundle.get("glasshive_capability_broker")
    values = broker.get("allowed_servers") if isinstance(broker, dict) else None
    if not isinstance(values, list):
        return []
    return sorted(
        {
            normalized
            for value in values
            if (normalized := str(value or "").strip())
            and re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", normalized)
        }
    )[:32]


class WorkersProjectsService:
    def __init__(
        self,
        store: Store,
        runtime: WorkerRuntime,
        max_workers: int = 8,
        reconcile_on_startup: bool | None = None,
        control_plane_store: ControlPlaneStore | None = None,
        scheduling_owner_client: ViventiumSchedulingOwnerClient | None = None,
    ) -> None:
        self.store = store
        self.runtime = runtime
        self.control_plane_store = control_plane_store
        self.scheduling_owner_client = scheduling_owner_client or ViventiumSchedulingOwnerClient()
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
        self._processors_lock = Lock()
        self._active_processors: set[str] = set()
        self._processor_generations: dict[str, int] = {}
        self._runtime_start_locks: dict[str, Lock] = {}
        self._worker_create_lock = Lock()
        self._deliverable_promotions_lock = Lock()
        self._deliverable_promotions: set[str] = set()
        self._callback_retry_thread: Thread | None = None
        self._idle_reaper_thread: Thread | None = None
        self._scheduler_thread: Thread | None = None
        self._background_consumers_enabled = _background_consumers_enabled()
        if reconcile_on_startup is None:
            reconcile_on_startup = _reconcile_on_startup_enabled()
        if self._background_consumers_enabled and reconcile_on_startup:
            self.reconcile_all_workers()
        if self._background_consumers_enabled:
            self.executor.submit(self._replay_pending_callbacks)
            self._callback_retry_thread = Thread(
                target=self._callback_retry_loop,
                name="wpr-callback-retry",
                daemon=True,
            )
            self._callback_retry_thread.start()
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

    def shutdown(self) -> None:
        self._shutdown_event.set()
        for thread in (self._callback_retry_thread, self._idle_reaper_thread, self._scheduler_thread):
            if thread and thread.is_alive():
                thread.join(timeout=2)
        self.executor.shutdown(wait=True, cancel_futures=False)
        self.conversation_executor.shutdown(wait=True, cancel_futures=False)

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

    def scheduling_cortex_callback_config(self, occurrence_id: str) -> dict:
        occurrence_id = str(occurrence_id or "").strip()
        if not occurrence_id:
            return {}
        load_viventium_runtime_env()
        owner_url = str(os.environ.get("GLASSHIVE_SCHEDULING_OWNER_URL") or "").strip()
        secret = str(os.environ.get("VIVENTIUM_GLASSHIVE_CALLBACK_SECRET") or "").strip()
        try:
            parsed = urlparse(owner_url)
        except ValueError:
            return {}
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or not secret:
            return {}
        return {
            "events_webhook_url": f"{parsed.scheme}://{parsed.netloc}{SCHEDULING_CORTEX_CALLBACK_PATH}",
            "hmac_secret": secret,
            "message_id": occurrence_id,
            "callback_kind": "scheduling_cortex",
        }

    def _scheduling_cortex_callback_config_for_run(self, run: dict | None) -> dict:
        run_id = str((run or {}).get("run_id") or "").strip()
        if not run_id:
            return {}
        return self.scheduling_cortex_callback_config(
            self.store.scheduling_cortex_occurrence_for_run(run_id)
        )

    def _callback_config_for_event(self, worker: dict, run: dict | None) -> dict:
        run_id = str((run or {}).get("run_id") or "").strip()
        if run_id and self.store.scheduling_cortex_occurrence_for_run(run_id):
            return self._scheduling_cortex_callback_config_for_run(run)
        return self._callback_config_for(worker)

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
                if response.status_code == 409:
                    self.store.mark_callback_delivered(
                        payload["callback_id"], attempts=attempts, payload_json=stored_payload_json
                    )
                    return
                response.raise_for_status()
                self.store.mark_callback_delivered(
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

    def _replay_pending_callbacks(self) -> None:
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
            pending = self.store.list_pending_callbacks(limit=50)
        except Exception:
            return
        for record in pending:
            worker = self.store.get_worker(str(record.get("worker_id") or ""))
            if not worker:
                continue
            run_id = str(record.get("run_id") or "").strip()
            callbacks = self._callback_config_for_event(
                worker,
                self.store.get_run(run_id) if run_id else None,
            )
            self._deliver_callback_record(worker, record, callbacks)

    def _callback_retry_loop(self) -> None:
        interval = _bounded_int_env(
            "GLASSHIVE_CALLBACK_RETRY_INTERVAL_S",
            30,
            min_value=1,
            max_value=3600,
        )
        while not self._shutdown_event.wait(interval):
            self._replay_pending_callbacks()

    def _ensure_execution_allowed(self, worker_or_mode: dict | str) -> None:
        if isinstance(worker_or_mode, dict) and str(worker_or_mode.get("state") or "") in CLOSED_WORKER_STATES:
            raise ControlPlaneConflict(
                "Workspace is closed; create a new workspace for new work"
            )
        if isinstance(worker_or_mode, dict):
            unresolved = self._unresolved_duplication_reapprovals(worker_or_mode)
            if unresolved:
                raise ControlPlaneConflict(
                    "Copied workspace needs capability review before it can run"
                )
        execution_mode = (
            str(worker_or_mode.get("execution_mode") or "docker")
            if isinstance(worker_or_mode, dict)
            else str(worker_or_mode or "docker")
        )
        if execution_mode == "host" and not host_workers_enabled():
            raise HostWorkersDisabledError(
                "GlassHive host-native workers are disabled by Viventium config"
            )

    def _unresolved_duplication_reapprovals(self, worker: dict) -> list[dict]:
        report = worker.get("duplication_report")
        if not isinstance(report, dict):
            return []
        if str(report.get("duplication_state") or "").strip() == "pending":
            return [{"action_id": "duplication_pending", "kind": "duplication"}]
        items = [
            item
            for item in (report.get("reapproval_items") or [])
            if isinstance(item, dict)
            and str(item.get("action_id") or "").strip()
            and str(item.get("reference") or "").strip()
        ]
        if not items:
            return []
        waived = {
            str(item).strip()
            for item in (report.get("waived_reapprovals") or [])
            if str(item).strip()
        }
        if self.control_plane_store is None:
            return [item for item in items if str(item.get("action_id") or "") not in waived]
        tenant_id = str(worker.get("tenant_id") or "local")
        owner_id = str(worker.get("owner_id") or "")
        worker_id = str(worker.get("worker_id") or "")
        grants = self.control_plane_store.list_workspace_grants(
            tenant_id=tenant_id,
            owner_id=owner_id,
            worker_id=worker_id,
        )
        selection = mission_provider_account_selection(worker)

        def resolved(item: dict) -> bool:
            reference = str(item.get("reference") or "")
            action_id = str(item.get("action_id") or "")
            if action_id in waived:
                return True
            resolution = str(item.get("resolution") or "")
            policy = str(item.get("policy") or "")
            if resolution == "provider_selection" and policy:
                return bool(
                    selection is not None
                    and selection.policy == policy
                    and selection.account_id == reference
                )
            grant_key = {
                "library_grant": "library_id",
                "connection_grant": "connection_id",
                "provider_grant": "account_id",
            }.get(resolution)
            expected_scopes = {
                str(scope).strip()
                for scope in (item.get("scopes") or [])
                if str(scope).strip()
            }
            return bool(grant_key and any(
                str(grant.get(grant_key) or "") == reference
                and {
                    str(scope).strip()
                    for scope in (grant.get("scopes") or [])
                    if str(scope).strip()
                } == expected_scopes
                for grant in grants
            ))

        return [item for item in items if not resolved(item)]

    def _runtime_start_lock(self, worker_id: str) -> Lock:
        with self._processors_lock:
            return self._runtime_start_locks.setdefault(worker_id, Lock())

    @contextmanager
    def _runtime_lifecycle_start_guard(self, worker_id: str):
        """Fence non-run readiness starts against permanent workspace Close."""

        with self._runtime_start_lock(worker_id):
            current = self.require_worker(worker_id)
            self._ensure_execution_allowed(current)
            yield

    def _ensure_worker_ready_with_lifecycle_fence(self, worker: dict) -> RuntimeInfo:
        worker_id = str(worker["worker_id"])

        def persist_runtime_info(info: RuntimeInfo) -> None:
            updated = self._apply_runtime_info(
                worker_id,
                info,
                state="starting",
                last_error="",
            )
            if updated and str(updated.get("state") or "") in CLOSED_WORKER_STATES:
                raise ControlPlaneConflict(
                    "Workspace is closed; create a new workspace for new work"
                )

        return self.runtime.ensure_worker_ready(
            {
                **worker,
                "_runtime_start_guard": lambda: self._runtime_lifecycle_start_guard(
                    worker_id
                ),
                "_runtime_info_callback": persist_runtime_info,
            }
        )

    def _finalize_worker_ready_after_start(
        self,
        worker: dict,
        info: RuntimeInfo,
        *,
        event_type: str,
        message: str,
        context: str,
        emit_callback: bool = False,
    ) -> dict:
        worker_id = str(worker["worker_id"])
        try:
            with self._runtime_lifecycle_start_guard(worker_id):
                updated = self._apply_runtime_info(
                    worker_id,
                    info,
                    state="ready",
                    last_error="",
                    compute_released_at=None,
                )
                if updated and str(updated.get("state") or "") in CLOSED_WORKER_STATES:
                    raise ControlPlaneConflict(
                        "Workspace is closed; create a new workspace for new work"
                    )
                self.store.add_event(
                    str(worker["project_id"]),
                    worker_id,
                    None,
                    event_type,
                    message,
                )
                if emit_callback:
                    self._emit_callback(
                        updated or worker,
                        event_type,
                        message="Worker ready",
                    )
                return updated or worker
        except ControlPlaneConflict:
            self._reject_closed_after_runtime_activity(
                worker_id,
                fallback_worker=worker,
                context=context,
            )
            raise

    @contextmanager
    def _runtime_execution_start_guard(
        self,
        worker_id: str,
        generation: int,
        run_id: str,
    ):
        """Serialize the real external start boundary with durable Close ownership."""

        with self._runtime_start_lock(worker_id):
            current = self.store.get_worker(worker_id)
            run = self.store.get_run(run_id)
            if (
                not self._processor_is_current(worker_id, generation)
                or not current
                or str(current.get("state") or "") in CLOSED_WORKER_STATES
                or not run
                or str(run.get("state") or "") != "running"
            ):
                current_state = str((current or {}).get("state") or "")
                run_state = str((run or {}).get("state") or "")
                if current and current_state in CLOSED_WORKER_STATES:
                    try:
                        self.runtime.terminate_worker(
                            {**current, "_active_run_id": run_id}
                        )
                    except Exception as cleanup_error:
                        message = public_callback_message_text(str(cleanup_error)) or "Worker close-race cleanup failed"
                        self.store.record_worker_termination_cleanup_failure(worker_id, message)
                elif current and (current_state == "paused" or (run and run_state != "running")):
                    try:
                        if current_state == "paused":
                            self.runtime.pause_worker(
                                {**current, "_active_run_id": run_id}
                            )
                        else:
                            try:
                                self.runtime.interrupt_worker(current, run_id=run_id)
                            except TypeError as exc:
                                if "run_id" not in str(exc):
                                    raise
                                self.runtime.interrupt_worker(current)
                    except Exception:
                        logger.error(
                            "Failed to clean up a staged runtime after %s control won for %s",
                            current_state or run_state or "operator",
                            worker_id,
                        )
                raise WorkerTerminatedError("Workspace was closed before the run could start")
            yield

    def _reject_closed_after_runtime_activity(
        self,
        worker_id: str,
        *,
        fallback_worker: dict,
        active_run_id: str = "",
        context: str,
    ) -> dict:
        """Compensate runtime work that lost a race to permanent workspace closure."""

        current = self.store.get_worker(worker_id) or fallback_worker
        if str(current.get("state") or "") not in CLOSED_WORKER_STATES:
            return current
        try:
            cleanup_info = self.runtime.terminate_worker(
                {**current, "_active_run_id": active_run_id}
            )
            if cleanup_info.pid:
                raise RuntimeError(
                    f"Worker compute is still active after {context} close-race cleanup "
                    f"(pid={cleanup_info.pid})"
                )
        except Exception as cleanup_error:
            message = public_callback_message_text(str(cleanup_error)) or "Worker close-race cleanup failed"
            self.store.record_worker_termination_cleanup_failure(worker_id, message)
            logger.error(
                "Failed to clean up runtime recreated by %s after workspace close for %s",
                context,
                worker_id,
            )
        raise ControlPlaneConflict(
            "Workspace is closed; create a new workspace for new work"
        )

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
        if not base_url or not worker_id or not path or not is_user_deliverable_relative_path(path):
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

    def _emit_callback(
        self,
        worker: dict,
        event_type: str,
        *,
        run: dict | None = None,
        message: str = "",
        full_message: str = "",
        deliverable: dict[str, object] | None = None,
    ) -> None:
        callbacks = self._callback_config_for_event(worker, run)
        url = str(callbacks.get("events_webhook_url") or callbacks.get("url") or "").strip()
        if not url:
            return
        if _is_viventium_callback_url(url):
            missing_parent_fields = _missing_parent_callback_fields(callbacks)
            if missing_parent_fields:
                logger.info(
                    "Skipping GlassHive parent callback for worker %s because callback context is incomplete: %s",
                    worker.get("worker_id"),
                    ", ".join(missing_parent_fields),
                )
                return
        operator_url = self._signed_watch_url(worker, callbacks)
        include_watch_link = event_type in ACTIONABLE_CALLBACK_LINK_EVENTS
        payload = {
            "callback_id": f"cb_{uuid.uuid4().hex}",
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
        }
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
        record = self.store.upsert_callback_outbox(
            callback_id=str(payload["callback_id"]),
            project_id=str(worker.get("project_id") or ""),
            worker_id=str(worker.get("worker_id") or ""),
            run_id=(run or {}).get("run_id"),
            event_type=event_type,
            url=url,
            payload_json=json.dumps(payload, ensure_ascii=False),
        )
        if not getattr(self, "_background_consumers_enabled", True):
            return
        self.executor.submit(self._deliver_callback_record, dict(worker), record, callbacks)

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

    def _ephemeral_retention_s(self) -> int:
        return _bounded_int_env(
            "GLASSHIVE_EPHEMERAL_RETENTION_S",
            7 * 24 * 3600,
            min_value=60,
            max_value=365 * 24 * 3600,
        )

    def _ephemeral_gc_enabled(self) -> bool:
        return str(os.environ.get("GLASSHIVE_EPHEMERAL_GC_ENABLED", "true")).strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
            "disabled",
        }

    def _ephemeral_gc_claim_ttl_s(self) -> int:
        return _bounded_int_env(
            "GLASSHIVE_EPHEMERAL_GC_CLAIM_TTL_S",
            60,
            min_value=10,
            max_value=3600,
        )

    def _lifecycle_reaper_enabled(self) -> bool:
        orphan_reaper_enabled = str(os.environ.get("GLASSHIVE_ORPHAN_REAPER_ENABLED", "true")).strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
            "disabled",
        }
        return (
            orphan_reaper_enabled
            or self._ephemeral_gc_enabled()
            or self._idle_terminate_after_s() > 0
            or self._paused_terminate_after_s() > 0
            or self._max_run_duration_s() > 0
        )

    def _managed_ephemeral_storage_root(self, worker: dict) -> Path | None:
        """Attest a canonical managed Docker root without trusting persisted paths."""

        if str(worker.get("execution_mode") or "docker").strip().lower() != "docker":
            return None
        if str(worker.get("workspace_root") or "").strip():
            return None
        worker_id = str(worker.get("worker_id") or "").strip()
        if (
            not worker_id
            or worker_id in {".", ".."}
            or Path(worker_id).name != worker_id
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", worker_id)
        ):
            return None

        runtime_manager = self.runtime
        selector = getattr(runtime_manager, "_runtime_for_worker", None)
        if callable(selector):
            try:
                runtime_manager = selector(worker)
            except Exception:
                return None

        path_map: dict[str, Path] = {}
        root_value: object | None = None
        managed_root = getattr(runtime_manager, "managed_worker_root", None)
        if callable(managed_root):
            try:
                root_value = managed_root(worker)
            except TypeError:
                root_value = managed_root(worker_id)
            except Exception:
                return None
        if root_value is None:
            sandbox = getattr(runtime_manager, "sandbox", None)
            paths = getattr(sandbox, "paths", None)
            if callable(paths):
                try:
                    raw_paths = paths(worker_id)
                except Exception:
                    return None
                if isinstance(raw_paths, dict):
                    path_map = {
                        str(key): Path(value).expanduser()
                        for key, value in raw_paths.items()
                        if value is not None
                    }
                    root_value = path_map.get("worker_root")
        if root_value is None:
            workers_dir = getattr(runtime_manager, "workers_dir", None)
            if workers_dir is not None:
                root_value = Path(workers_dir).expanduser() / worker_id
        if root_value is None:
            return None

        configured_root = Path(root_value).expanduser()
        try:
            canonical_parent = configured_root.parent.resolve(strict=True)
            if not canonical_parent.is_dir():
                return None
            root = canonical_parent / worker_id
            if configured_root.resolve(strict=False) != root:
                return None
            if os.path.lexists(root):
                if root.is_symlink() or not root.is_dir() or root.resolve(strict=True).parent != canonical_parent:
                    return None
        except (OSError, RuntimeError):
            return None

        expected_state = path_map.get("state_dir", root / "state").resolve(strict=False)
        expected_workspace = path_map.get("workspace_dir", expected_state / "workspace").resolve(strict=False)
        for persisted_name, expected in (
            ("state_dir", expected_state),
            ("workspace_dir", expected_workspace),
        ):
            raw = str(worker.get(persisted_name) or "").strip()
            if not raw:
                return None
            try:
                if Path(raw).expanduser().resolve(strict=False) != expected:
                    return None
            except (OSError, RuntimeError):
                return None
        return root

    def _cleanup_workspace_gc_tombstone(
        self,
        tombstone: dict[str, object],
        *,
        claim_token: str,
        now_epoch: float,
    ) -> bool:
        worker_id = str(tombstone.get("worker_id") or "")
        audit_root = str(tombstone.get("managed_storage_root") or "").strip()
        if not worker_id:
            return False
        try:
            if audit_root:
                attested_root = self._managed_ephemeral_storage_root(tombstone)
                if attested_root is None or str(attested_root) != audit_root:
                    raise RuntimeError("managed workspace storage can no longer be attested")
                if os.path.lexists(attested_root):
                    shutil.rmtree(attested_root)
            return self.store.record_workspace_gc_cleanup(
                worker_id,
                claim_token=claim_token,
                now_epoch=now_epoch,
            )
        except Exception as exc:
            message = public_callback_message_text(str(exc)) or "workspace storage cleanup failed"
            self.store.record_workspace_gc_cleanup(
                worker_id,
                claim_token=claim_token,
                now_epoch=now_epoch,
                error=message,
            )
            logger.warning("Failed to clean expired workspace storage %s: %s", worker_id, message)
            return False

    def reap_ephemeral_workspaces_once(self, *, now: datetime | None = None) -> list[dict[str, object]]:
        """Claim, tombstone, and clean expired one-off workspaces without path trust."""

        if not self._ephemeral_gc_enabled():
            return []
        effective_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        cutoff = effective_now - timedelta(seconds=self._ephemeral_retention_s())
        now_epoch = effective_now.timestamp()
        claim_ttl_s = self._ephemeral_gc_claim_ttl_s()

        for pending in self.store.list_workspace_gc_cleanup_candidates(now_epoch=now_epoch, limit=50):
            cleanup_token = f"gc_{uuid.uuid4().hex}"
            claimed_cleanup = self.store.claim_workspace_gc_cleanup(
                str(pending.get("worker_id") or ""),
                claim_token=cleanup_token,
                now_epoch=now_epoch,
                claim_ttl_s=claim_ttl_s,
            )
            if claimed_cleanup is not None:
                self._cleanup_workspace_gc_tombstone(
                    claimed_cleanup,
                    claim_token=cleanup_token,
                    now_epoch=now_epoch,
                )

        candidates = self.store.list_recoverable_workspace_gc_claims(
            now_epoch=now_epoch,
            limit=50,
        )
        candidates.extend(self.store.list_ephemeral_workspace_gc_candidates(
            updated_before=cutoff.isoformat(),
            limit=50,
        ))
        reaped: list[dict[str, object]] = []
        seen: set[str] = set()
        for worker in candidates:
            worker_id = str(worker.get("worker_id") or "")
            if not worker_id or worker_id in seen:
                continue
            seen.add(worker_id)
            storage_root = self._managed_ephemeral_storage_root(worker)
            claim_token = f"gc_{uuid.uuid4().hex}"
            claimed = self.store.claim_ephemeral_workspace_gc(
                worker_id,
                updated_before=cutoff.isoformat(),
                now_epoch=now_epoch,
                claim_token=claim_token,
                claim_ttl_s=claim_ttl_s,
                managed_storage_root=str(storage_root or ""),
            )
            if claimed is None:
                continue
            try:
                info = self.runtime.terminate_worker({**claimed, "_active_run_id": ""})
                if info.pid:
                    raise RuntimeError("ephemeral workspace compute is still active")
                deleted = self.store.finalize_ephemeral_workspace_gc(
                    worker_id,
                    claim_token=claim_token,
                    updated_before=cutoff.isoformat(),
                    now_epoch=now_epoch,
                )
                if deleted is None:
                    self.store.release_ephemeral_workspace_gc_claim(worker_id, claim_token=claim_token)
                    continue
                try:
                    revoke_signed_link_refs_for_worker(worker_id)
                except Exception as revoke_exc:
                    logger.warning("Failed to revoke expired workspace links for %s: %s", worker_id, revoke_exc)
                tombstone = self.store.claim_workspace_gc_cleanup(
                    worker_id,
                    claim_token=claim_token,
                    now_epoch=now_epoch,
                    claim_ttl_s=claim_ttl_s,
                )
                if tombstone is not None:
                    self._cleanup_workspace_gc_tombstone(
                        tombstone,
                        claim_token=claim_token,
                        now_epoch=now_epoch,
                    )
                reaped.append(
                    {
                        "worker_id": worker_id,
                        "project_id": deleted.get("project_id"),
                        "tenant_id": deleted.get("tenant_id"),
                        "owner_id": deleted.get("owner_id"),
                        "workspace_kind": "ephemeral",
                    }
                )
            except Exception as exc:
                self.store.release_ephemeral_workspace_gc_claim(worker_id, claim_token=claim_token)
                logger.warning("Failed to garbage-collect ephemeral GlassHive workspace %s: %s", worker_id, exc)
        return reaped

    def _worker_idle_seconds(self, worker: dict) -> float:
        raw = str(worker.get("updated_at") or "")
        try:
            updated = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            return 0.0
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        return max(0.0, datetime.now(timezone.utc).timestamp() - updated.astimezone(timezone.utc).timestamp())

    def reap_idle_workers_once(self) -> list[dict[str, object]]:
        threshold = self._idle_terminate_after_s()
        if threshold <= 0:
            return []
        terminal_states = {"completed", "failed", "cancelled", "interrupted"}
        reaped: list[dict[str, object]] = []
        for worker in self.store.list_all_workers():
            worker_id = str(worker.get("worker_id") or "")
            if not worker_id or worker.get("state") in {"terminating", "termination_failed", "terminated", "paused", "running", "starting"}:
                continue
            if self.store.get_active_run(worker_id) or self.store.has_queued_runs(worker_id):
                continue
            idle_seconds = self._worker_idle_seconds(worker)
            if idle_seconds < threshold:
                continue
            try:
                info = self.runtime.terminate_worker(worker)
                current_state = str(worker.get("state") or "")
                next_state = current_state if current_state in terminal_states else "paused"
                updated = self._apply_runtime_info(
                    worker_id,
                    info,
                    state=next_state,
                    last_error="",
                    compute_released_at=utc_now(),
                )
                self.store.add_event(
                    str(worker.get("project_id") or ""),
                    worker_id,
                    None,
                    "worker.idle_terminated",
                    f"Idle worker compute stopped after {int(idle_seconds)} seconds; workspace state preserved.",
                )
                reaped.append(
                    {
                        "worker_id": worker_id,
                        "project_id": worker.get("project_id"),
                        "tenant_id": worker.get("tenant_id"),
                        "owner_id": worker.get("owner_id"),
                        "state": (updated or worker).get("state"),
                        "idle_seconds": int(idle_seconds),
                    }
                )
            except Exception as exc:
                logger.warning("Failed to reap idle GlassHive worker %s: %s", worker_id, exc)
        return reaped

    def _reconcile_terminated_worker_compute(self, worker: dict) -> dict[str, object] | None:
        worker_id = str(worker.get("worker_id") or "")
        worker_state = str(worker.get("state") or "")
        if not worker_id or worker_state not in {"terminated", "failed"}:
            return None
        if self.store.get_active_run(worker_id) or self.store.has_queued_runs(worker_id):
            return None
        compute_checker = getattr(self.runtime, "worker_compute_present", None)
        compute_present = bool(compute_checker(worker)) if callable(compute_checker) else bool(self.runtime.reconcile_worker(worker).pid)
        if not compute_present:
            return None
        runtime_worker = {
            **worker,
            "_active_run_id": "",
        }
        self._invalidate_worker_processor(worker_id)
        self.store.cancel_pending_runs(
            worker_id,
            error_text="Worker terminated by operator",
            state="cancelled",
        )
        info = self.runtime.terminate_worker(runtime_worker)
        if info.pid:
            raise RuntimeError(f"Worker compute is still active after termination (pid={info.pid})")
        self._apply_runtime_info(
            worker_id,
            info,
            state=worker_state,
            last_error=str(worker.get("last_error") or ""),
            compute_released_at=worker.get("compute_released_at") or utc_now(),
        )
        event_type = "worker.terminated_compute_reconciled" if worker_state == "terminated" else "worker.failed_compute_reconciled"
        self.store.add_event(
            str(worker.get("project_id") or ""),
            worker_id,
            None,
            event_type,
            f"Orphaned compute removed for a worker already marked {worker_state}.",
        )
        return {"worker_id": worker_id, "project_id": worker.get("project_id")}

    def reap_terminated_workers_once(self) -> list[dict[str, object]]:
        reaped: list[dict[str, object]] = []
        for worker in self.store.list_all_workers():
            try:
                reconciled = self._reconcile_terminated_worker_compute(worker)
                if reconciled:
                    reaped.append(reconciled)
            except Exception as exc:
                logger.warning(
                    "Failed to reconcile terminated GlassHive worker compute %s: %s",
                    str(worker.get("worker_id") or ""),
                    exc,
                )
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
            if self.store.get_active_run(worker_id) or self.store.has_queued_runs(worker_id):
                continue
            idle_seconds = self._worker_idle_seconds(worker)
            if idle_seconds < threshold:
                continue
            try:
                info = self.runtime.terminate_worker(worker)
                updated = self._apply_runtime_info(
                    worker_id,
                    info,
                    state="paused",
                    last_error="",
                    compute_released_at=utc_now(),
                )
                self.store.add_event(
                    str(worker.get("project_id") or ""),
                    worker_id,
                    None,
                    "worker.paused_compute_terminated",
                    f"Paused worker compute stopped after {int(idle_seconds)} seconds; workspace state preserved.",
                )
                reaped.append(
                    {
                        "worker_id": worker_id,
                        "project_id": worker.get("project_id"),
                        "tenant_id": worker.get("tenant_id"),
                        "owner_id": worker.get("owner_id"),
                        "state": (updated or worker).get("state"),
                        "idle_seconds": int(idle_seconds),
                    }
                )
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
                info = self.runtime.terminate_worker(worker)
                finalized = self.store.finalize_run_if_state(run_id, "running", "cancelled", error_text=error_text)
                self.store.finalize_schedule_for_run(run_id, state="cancelled", last_error=error_text)
                updated = self._apply_runtime_info(
                    worker_id,
                    info,
                    state="paused",
                    last_error=error_text,
                    compute_released_at=utc_now(),
                )
                self._wake_host_capacity_waiters(updated or worker)
                self.store.add_event(
                    str(worker.get("project_id") or run.get("project_id") or ""),
                    worker_id,
                    run_id,
                    "run.duration_exceeded",
                    error_text,
                )
                if finalized:
                    self._emit_callback(
                        worker,
                        "run.cancelled",
                        run={**run, "state": "cancelled", "error_text": error_text},
                        message=error_text,
                    )
                reaped.append(
                    {
                        "worker_id": worker_id,
                        "project_id": worker.get("project_id"),
                        "tenant_id": worker.get("tenant_id"),
                        "owner_id": worker.get("owner_id"),
                        "run_id": run_id,
                        "state": (updated or worker).get("state"),
                        "run_age_seconds": threshold if age_seconds == float("inf") else int(age_seconds),
                    }
                )
            except Exception as exc:
                logger.warning("Failed to stop expired GlassHive run %s for worker %s: %s", run_id, worker_id, exc)
        return reaped

    def _idle_reaper_loop(self) -> None:
        interval = self._idle_reaper_interval_s()
        while not self._shutdown_event.wait(interval):
            self.reap_terminated_workers_once()
            self.reap_ephemeral_workspaces_once()
            self.reap_idle_workers_once()
            self.reap_paused_workers_once()
            self.reap_expired_runs_once()

    def _scheduler_interval_s(self) -> int:
        return _bounded_int_env("GLASSHIVE_SCHEDULER_INTERVAL_S", 5, min_value=1, max_value=3600)

    def _scheduler_loop(self) -> None:
        interval = self._scheduler_interval_s()
        while not self._shutdown_event.wait(interval):
            try:
                self.process_due_schedules_once()
            except Exception:
                logger.exception("GlassHive scheduler iteration failed; the scheduler will retry")

    def _retry_base_delay_s(self, failure_class: str) -> float:
        if failure_class == "host_worker_busy":
            return _bounded_float_env(
                "GLASSHIVE_HOST_BUSY_RETRY_BASE_DELAY_S",
                _bounded_float_env("GLASSHIVE_RETRY_BASE_DELAY_S", 5.0, min_value=0.1, max_value=3600.0),
                min_value=0.1,
                max_value=3600.0,
            )
        return _bounded_float_env("GLASSHIVE_RETRY_BASE_DELAY_S", 5.0, min_value=0.1, max_value=3600.0)

    def _retry_max_delay_s(self, failure_class: str) -> float:
        if failure_class == "host_worker_busy":
            return _bounded_float_env(
                "GLASSHIVE_HOST_BUSY_RETRY_MAX_DELAY_S",
                15.0,
                min_value=0.1,
                max_value=60.0,
            )
        return _bounded_float_env("GLASSHIVE_RETRY_MAX_DELAY_S", 300.0, min_value=0.1, max_value=86400.0)

    def _retry_delay_s(self, failure_class: str, attempts: int) -> float:
        base = self._retry_base_delay_s(failure_class)
        max_delay = self._retry_max_delay_s(failure_class)
        exponent = min(max(0, attempts - 1), 8)
        return min(max_delay, base * (2**exponent))

    def _capacity_retry_max_attempts(self, failure_class: str = "") -> int:
        if failure_class == "host_worker_busy":
            return _bounded_int_env(
                "GLASSHIVE_HOST_BUSY_MAX_RETRY_ATTEMPTS",
                240,
                min_value=0,
                max_value=10000,
            )
        return _bounded_int_env("GLASSHIVE_MAX_CAPACITY_RETRY_ATTEMPTS", 6, min_value=0, max_value=1000)

    def _wake_worker_processor_later(self, worker_id: str, delay_s: float) -> None:
        if self._shutdown_event.is_set():
            return

        def wake() -> None:
            if not self._shutdown_event.is_set():
                self._ensure_worker_processor(worker_id)

        timer = Timer(max(0.1, float(delay_s)), wake)
        timer.daemon = True
        timer.start()

    def _schedule_worker_retry_after(self, worker_id: str, retry_after: str | None) -> None:
        if not retry_after:
            return
        try:
            parsed = datetime.fromisoformat(str(retry_after).replace("Z", "+00:00"))
        except ValueError:
            self._wake_worker_processor_later(worker_id, self._scheduler_interval_s())
            return
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        delay_s = max(0.1, parsed.astimezone(timezone.utc).timestamp() - datetime.now(timezone.utc).timestamp())
        self._wake_worker_processor_later(worker_id, delay_s)

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
        max_attempts = self._capacity_retry_max_attempts(failure_class)
        if attempts > max_attempts:
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
            failed_run = self.store.finalize_run(
                str(run["run_id"]),
                state="failed",
                error_text=str(exc),
                **exhausted_fields,
            ) or {**run, "state": "failed", "error_text": str(exc), **exhausted_fields}
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
        retry_after = (datetime.now(timezone.utc) + timedelta(seconds=delay_s)).isoformat()
        updated_run = self.store.requeue_run_for_retry(
            str(run["run_id"]),
            retry_after=retry_after,
            error_text=str(exc),
            last_retry_class=failure_class,
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
        self._wake_worker_processor_later(str(worker["worker_id"]), delay_s)
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
        project_id: str | None = None,
    ) -> dict:
        return self.store.create_project(
            owner_id,
            title,
            goal,
            default_worker_profile,
            tenant_id=tenant_id,
            project_id=project_id,
        )

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
        workspace_kind: WorkspaceKind | str = "legacy",
        tags: list[str] | None = None,
        duplication_report: dict[str, object] | None = None,
    ) -> dict:
        self._ensure_execution_allowed(execution_mode)
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
                workspace_kind=normalize_workspace_kind(workspace_kind),
                tags=normalize_workspace_tags(tags),
                duplication_report=duplication_report,
            )
        if not start_synchronously:
            prepare_workspace = getattr(self.runtime, "prepare_worker_workspace", None)
            if callable(prepare_workspace):
                info = prepare_workspace(worker)
                prepared = self.store.update_worker(
                    worker["worker_id"],
                    state="paused",
                    last_error="",
                    compute_released_at=utc_now(),
                    state_dir=info.state_dir,
                    workspace_dir=info.workspace_dir,
                    session_key=info.session_key,
                    pid=None,
                )
            else:
                prepared = self.store.update_worker_state(worker["worker_id"], "paused", last_error="")
            self.store.add_event(
                project_id,
                worker["worker_id"],
                None,
                "worker.prepared",
                "Worker workspace is prepared and compute will start when a run is queued",
            )
            return prepared or self.store.get_worker(worker["worker_id"]) or worker
        self.store.update_worker_state(worker["worker_id"], "starting")
        try:
            info = self._ensure_worker_ready_with_lifecycle_fence(worker)
        except Exception as exc:
            updated = self.store.update_worker(
                worker["worker_id"],
                state="failed",
                last_error=str(exc),
            )
            if updated and str(updated.get("state") or "") in CLOSED_WORKER_STATES:
                self._reject_closed_after_runtime_activity(
                    worker["worker_id"],
                    fallback_worker=updated,
                    context="workspace creation",
                )
            self.store.add_event(project_id, worker["worker_id"], None, "worker.failed", str(exc))
            return updated or worker
        return self._finalize_worker_ready_after_start(
            worker,
            info,
            event_type="worker.ready",
            message=f"Worker ready on {info.gateway_url}",
            context="workspace creation",
            emit_callback=True,
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
        workspace_kind: WorkspaceKind | str = "legacy",
        tags: list[str] | None = None,
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
        if existing and str(existing.get("state") or "") not in CLOSED_WORKER_STATES:
            existing_worker_id = str(existing.get("worker_id") or "")
            with self._runtime_start_lock(existing_worker_id):
                existing = self.store.get_worker(existing_worker_id) or existing
                if str(existing.get("state") or "") not in CLOSED_WORKER_STATES:
                    if self.store.workspace_gc_claim_active(existing_worker_id):
                        raise RuntimeErrorBase("Workspace is being garbage-collected")
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
                    existing = self.store.update_worker(existing_worker_id, **updates) or existing
                    if str(existing.get("state") or "") in CLOSED_WORKER_STATES:
                        existing = None
                else:
                    existing = None
                if existing is not None:
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
            workspace_kind=workspace_kind,
            tags=tags,
        )

    def update_worker_metadata(
        self,
        worker_id: str,
        *,
        favorite: bool | None = None,
        name: str | None = None,
        workspace_kind: WorkspaceKind | str | None = None,
        tags: list[str] | None = None,
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
        if workspace_kind is not None:
            updates["workspace_kind"] = normalize_workspace_kind(workspace_kind)
        if tags is not None:
            updates["workspace_tags_json"] = json.dumps(
                normalize_workspace_tags(tags),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        if not updates:
            return worker
        updated = self.store.update_worker_unless_gc_claimed(worker_id, **updates)
        if updated is None:
            raise RuntimeErrorBase("Workspace is being garbage-collected")
        self.store.add_event(worker["project_id"], worker_id, None, "worker.metadata_updated", "Worker metadata updated")
        return updated

    def list_workspace_catalog(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        workspace_kinds: set[str] | None = None,
        search: str = "",
        tags: list[str] | None = None,
        favorite: bool | None = None,
        cursor: str | None = None,
        limit: int = 25,
    ) -> dict[str, object]:
        page_size = max(1, min(int(limit), 100))
        normalized_kinds = {normalize_workspace_kind(value) for value in workspace_kinds or set()}
        cursor_favorite, cursor_activity_at, cursor_worker_id = _decode_workspace_cursor(cursor)
        rows = self.store.list_workspace_catalog(
            tenant_id=tenant_id or "local",
            owner_id=owner_id,
            workspace_kinds=normalized_kinds,
            search=search,
            tags=normalize_workspace_tags(tags),
            favorite=favorite,
            cursor_favorite=cursor_favorite,
            cursor_activity_at=cursor_activity_at,
            cursor_worker_id=cursor_worker_id,
            limit=page_size + 1,
        )
        items = rows[:page_size]
        worker_ids = [str(item.get("worker_id") or "") for item in items]

        accounts: dict[str, dict] = {}
        if self.control_plane_store is not None:
            accounts = {
                str(account.get("account_id") or ""): account
                for account in self.control_plane_store.list_provider_accounts(
                    tenant_id=tenant_id or "local",
                    owner_id=owner_id,
                )
            }
        try:
            capability_readiness = (
                self.control_plane_store.workspace_capability_readiness(
                    tenant_id=tenant_id or "local",
                    owner_id=owner_id,
                    worker_ids=worker_ids,
                )
                if self.control_plane_store is not None
                else {
                    worker_id: {
                        "active_grants": 0,
                        "unavailable_grants": 0,
                        "readiness": "ready",
                    }
                    for worker_id in worker_ids
                }
            )
        except Exception as exc:
            logger.warning("Workspace catalog capability readiness is unavailable: %s", exc)
            capability_readiness = {
                worker_id: {
                    "active_grants": 0,
                    "unavailable_grants": 0,
                    "readiness": "unavailable",
                }
                for worker_id in worker_ids
            }

        schedule_readiness = "ready"
        if self.recurring_schedule_owner() == DELEGATED_RECURRENCE_OWNER:
            try:
                definitions = self.list_recurring_schedules(
                    tenant_id=tenant_id or "local",
                    owner_id=owner_id,
                    include_inactive=False,
                    limit=100,
                )
                next_schedule_by_worker: dict[str, str] = {}
                for definition in definitions:
                    schedule_worker_id = str(definition.get("worker_id") or "")
                    next_at = str(
                        definition.get("next_occurrence_at")
                        or definition.get("next_run_at")
                        or ""
                    )
                    current = next_schedule_by_worker.get(schedule_worker_id, "")
                    if next_at and (not current or next_at < current):
                        next_schedule_by_worker[schedule_worker_id] = next_at
                if len(definitions) >= 100:
                    schedule_readiness = "partial"
            except Exception as exc:
                logger.warning("Workspace catalog schedule readiness is unavailable: %s", exc)
                next_schedule_by_worker = {}
                schedule_readiness = "unavailable"
        else:
            next_schedule_by_worker = self.store.next_scheduled_occurrence_by_worker(
                tenant_id=tenant_id or "local",
                owner_id=owner_id,
                worker_ids=worker_ids,
            )

        for item in items:
            worker_id = str(item.get("worker_id") or "")
            raw_bundle = item.get("bootstrap_bundle_json")
            if isinstance(raw_bundle, str) and raw_bundle.strip():
                try:
                    bundle = json.loads(raw_bundle)
                except json.JSONDecodeError:
                    bundle = {}
            else:
                bundle = raw_bundle if isinstance(raw_bundle, dict) else {}
            selection = bundle.get("provider_account") if isinstance(bundle, dict) else None
            policy = str(selection.get("policy") or "legacy") if isinstance(selection, dict) else "legacy"
            account_id = str(selection.get("account_id") or "") if isinstance(selection, dict) else ""
            account = accounts.get(account_id)
            if policy == "legacy":
                item["provider_readiness"] = {
                    "readiness": "deployment_managed",
                    "policy": "legacy",
                }
            elif policy == "personal_preferred" and not account_id:
                item["provider_readiness"] = {
                    "readiness": "deployment_managed",
                    "policy": "personal_preferred",
                    "fallback": True,
                }
            elif not account_id:
                item["provider_readiness"] = {
                    "readiness": "action_required",
                    "policy": policy,
                    "status": "missing",
                }
            elif account is None:
                item["provider_readiness"] = {
                    "readiness": "action_required",
                    "policy": policy,
                    "account_id": account_id,
                    "status": "missing",
                }
            else:
                status = str(account.get("status") or "unknown")
                item["provider_readiness"] = {
                    "readiness": "ready" if status == "ready" else "action_required",
                    "policy": policy,
                    "account_id": account_id,
                    "provider": str(account.get("provider") or ""),
                    "label": str(account.get("label") or ""),
                    "status": status,
                }
            item["capability_readiness"] = capability_readiness.get(
                worker_id,
                {"active_grants": 0, "unavailable_grants": 0, "readiness": "ready"},
            )
            item["next_schedule_at"] = next_schedule_by_worker.get(worker_id, "")
            item["schedule_readiness"] = schedule_readiness
        return {
            "items": items,
            "next_cursor": _encode_workspace_cursor(items[-1]) if len(rows) > page_size and items else None,
        }

    def recurring_schedule_owner(self) -> str:
        return _recurring_schedule_owner()

    def _delegated_schedule_call(
        self,
        action: str,
        payload: dict[str, object],
        *,
        tenant_id: str,
        owner_id: str,
    ) -> object:
        return self.scheduling_owner_client.call(
            action,
            payload,
            identity=SchedulingOwnerIdentity(
                tenant_id=tenant_id or "local",
                owner_id=owner_id,
                agent_id=str(os.environ.get("VIVENTIUM_MAIN_AGENT_ID") or "scheduling-cortex"),
            ),
        )

    def _deactivate_delegated_schedules_for_closed_worker(self, worker: dict) -> int:
        """Deactivate every authoritative delegated definition before Close is complete."""

        if self.recurring_schedule_owner() != DELEGATED_RECURRENCE_OWNER:
            return 0
        tenant_id = str(worker.get("tenant_id") or "local")
        owner_id = str(worker.get("owner_id") or "")
        worker_id = str(worker.get("worker_id") or "")
        deactivated = 0
        for _ in range(100):
            result = self._delegated_schedule_call(
                "list",
                {"worker_id": worker_id, "include_inactive": False, "limit": 100},
                tenant_id=tenant_id,
                owner_id=owner_id,
            )
            if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
                raise RuntimeError("Viventium Scheduling Cortex returned invalid schedule data")
            definition_ids = [
                str(item.get("definition_id") or "").strip()
                for item in result
                if str(item.get("definition_id") or "").strip()
            ]
            if not definition_ids:
                return deactivated
            for definition_id in definition_ids:
                response = self._delegated_schedule_call(
                    "deactivate",
                    {"definition_id": definition_id},
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                )
                if not isinstance(response, dict):
                    raise RuntimeError("Viventium Scheduling Cortex returned invalid schedule data")
                deactivated += 1
            if len(definition_ids) < 100:
                return deactivated
        raise RuntimeError("Delegated workspace schedule cleanup did not converge")

    def list_recurring_schedules(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        worker_id: str | None = None,
        include_inactive: bool = False,
        limit: int = 100,
    ) -> list[dict]:
        if self.recurring_schedule_owner() == DELEGATED_RECURRENCE_OWNER:
            result = self._delegated_schedule_call(
                "list",
                {
                    "worker_id": worker_id or "",
                    "include_inactive": include_inactive,
                    "limit": max(1, min(int(limit), 100)),
                },
                tenant_id=tenant_id,
                owner_id=owner_id,
            )
            if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
                raise RuntimeError("Viventium Scheduling Cortex returned invalid schedule data")
            schedules = result
        elif worker_id:
            schedules = self.store.list_recurring_schedule_definitions(
                worker_id,
                tenant_id=tenant_id,
                owner_id=owner_id,
                include_inactive=include_inactive,
                limit=limit,
            )
        else:
            schedules = self.store.list_recurring_schedule_definitions_for_owner(
                tenant_id=tenant_id,
                owner_id=owner_id,
                include_inactive=include_inactive,
                limit=limit,
            )

        enriched: list[dict] = []
        for schedule in schedules:
            item = dict(schedule)
            if self.recurring_schedule_owner() != DELEGATED_RECURRENCE_OWNER:
                latest = self.store.list_recurring_schedule_occurrences(
                    str(item.get("definition_id") or ""),
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                    limit=1,
                )
                if latest:
                    occurrence = latest[0]
                    outcome = str(occurrence.get("outcome") or "").strip()
                    if outcome in {"", "pending", "manual_pending"}:
                        outcome = str(occurrence.get("state") or "pending").strip()
                    item["last_occurrence_at"] = occurrence.get("scheduled_for")
                    item["last_outcome"] = outcome
                    item["last_error"] = str(occurrence.get("last_error") or "")
            scheduled_worker_id = str(item.get("worker_id") or "")
            worker = self.store.get_worker(
                scheduled_worker_id,
                tenant_id=tenant_id or "local",
                owner_id=owner_id,
            )
            if worker:
                item["workspace_name"] = str(worker.get("name") or "")
            enriched.append(item)
        return enriched

    def get_recurring_schedule(
        self,
        definition_id: str,
        *,
        tenant_id: str,
        owner_id: str,
    ) -> dict | None:
        if self.recurring_schedule_owner() == DELEGATED_RECURRENCE_OWNER:
            result = self._delegated_schedule_call(
                "get",
                {"definition_id": definition_id},
                tenant_id=tenant_id,
                owner_id=owner_id,
            )
            definition = result if isinstance(result, dict) else None
        else:
            definition = self.store.get_recurring_schedule_definition(
                definition_id,
                tenant_id=tenant_id,
                owner_id=owner_id,
            )
        if not definition:
            return None
        enriched = dict(definition)
        if self.recurring_schedule_owner() != DELEGATED_RECURRENCE_OWNER:
            latest = self.store.list_recurring_schedule_occurrences(
                definition_id,
                tenant_id=tenant_id,
                owner_id=owner_id,
                limit=1,
            )
            if latest:
                occurrence = latest[0]
                outcome = str(occurrence.get("outcome") or "").strip()
                if outcome in {"", "pending", "manual_pending"}:
                    outcome = str(occurrence.get("state") or "pending").strip()
                enriched["last_occurrence_at"] = occurrence.get("scheduled_for")
                enriched["last_outcome"] = outcome
                enriched["last_error"] = str(occurrence.get("last_error") or "")
        worker = self.store.get_worker(
            str(enriched.get("worker_id") or ""),
            tenant_id=tenant_id or "local",
            owner_id=owner_id,
        )
        if worker:
            enriched["workspace_name"] = str(worker.get("name") or "")
        return enriched

    def list_recurring_schedule_occurrences(
        self,
        definition_id: str,
        *,
        tenant_id: str,
        owner_id: str,
        limit: int = 50,
    ) -> list[dict]:
        if self.recurring_schedule_owner() == DELEGATED_RECURRENCE_OWNER:
            result = self._delegated_schedule_call(
                "occurrences",
                {"definition_id": definition_id, "limit": max(1, min(int(limit), 100))},
                tenant_id=tenant_id,
                owner_id=owner_id,
            )
            if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
                raise RuntimeError("Viventium Scheduling Cortex returned invalid occurrence data")
            return result
        return self.store.list_recurring_schedule_occurrences(
            definition_id,
            tenant_id=tenant_id,
            owner_id=owner_id,
            limit=limit,
        )

    def create_recurring_schedule(
        self,
        worker_id: str,
        instruction: str,
        *,
        recurrence_type: str,
        interval_seconds: int | None = None,
        local_time: str = "",
        timezone_name: str = "UTC",
        dst_policy: str = "next_valid_earliest",
        first_run_at: str | None = None,
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
        schedule_text: str = "",
        runtime_bundle: dict | None = None,
    ) -> dict:
        schedule_owner = self.recurring_schedule_owner()
        normalized_instruction = str(instruction or "").strip()
        if not normalized_instruction:
            raise ValueError("instruction is required")
        worker = self.require_worker(worker_id)
        self._ensure_execution_allowed(worker)
        effective_starts_at = starts_at or (first_run_at if recurrence_type == "once" else None)
        spec = normalize_recurrence_spec(
            recurrence_type=recurrence_type,
            interval_seconds=interval_seconds,
            local_time=local_time,
            timezone_name=timezone_name,
            dst_policy=dst_policy,
            cron_expression=cron_expression,
            rrule=rrule,
            starts_at=effective_starts_at,
            ends_at=ends_at,
            enabled=enabled,
            overlap_policy=overlap_policy,
            misfire_grace_seconds=misfire_grace_seconds,
            catch_up_policy=catch_up_policy,
            max_catch_up_occurrences=max_catch_up_occurrences,
            jitter_seconds=jitter_seconds,
        )
        creation_time = datetime.now(timezone.utc).replace(microsecond=0)
        if not spec.get("starts_at") and spec["recurrence_type"] in {"cron", "rfc5545"}:
            spec["starts_at"] = creation_time.isoformat()
        first_occurrence = first_occurrence_at(
            spec,
            now=creation_time,
            first_run_at=first_run_at,
        )
        self._require_schedule_principal_authority(
            tenant_id=str(worker.get("tenant_id") or "local"),
            owner_id=str(worker.get("owner_id") or ""),
            establish=True,
        )
        if schedule_owner == DELEGATED_RECURRENCE_OWNER:
            effective_bundle = merge_bootstrap_bundle(
                self._bootstrap_bundle_for(worker),
                runtime_bundle,
            )
            delegated_payload = {
                "definition_id": f"rsd_{uuid.uuid4().hex}",
                "worker_id": worker_id,
                "project_id": str(worker.get("project_id") or ""),
                "instruction": normalized_instruction,
                "schedule_text": str(schedule_text or ""),
                "execution_mode": str(worker.get("execution_mode") or "docker"),
                "required_capability_servers": _required_capability_servers(effective_bundle),
                **spec,
                "next_run_at": first_occurrence.isoformat(),
            }
            result = self._delegated_schedule_call(
                "create",
                delegated_payload,
                tenant_id=str(worker.get("tenant_id") or "local"),
                owner_id=str(worker.get("owner_id") or ""),
            )
            if not isinstance(result, dict):
                raise RuntimeError("Viventium Scheduling Cortex returned invalid schedule data")
            try:
                self._require_schedule_principal_authority(
                    tenant_id=str(worker.get("tenant_id") or "local"),
                    owner_id=str(worker.get("owner_id") or ""),
                    establish=False,
                )
            except SchedulePrincipalAuthorityError:
                definition_id = str(result.get("definition_id") or "")
                if definition_id:
                    try:
                        self._delegated_schedule_call(
                            "deactivate",
                            {"definition_id": definition_id},
                            tenant_id=str(worker.get("tenant_id") or "local"),
                            owner_id=str(worker.get("owner_id") or ""),
                        )
                    except Exception as cleanup_error:
                        logger.warning(
                            "Could not compensate delegated schedule creation after authority revocation: %s",
                            cleanup_error,
                        )
                raise
            try:
                self._ensure_execution_allowed(self.require_worker(worker_id))
            except ControlPlaneConflict:
                definition_id = str(result.get("definition_id") or "")
                if definition_id:
                    try:
                        self._delegated_schedule_call(
                            "deactivate",
                            {"definition_id": definition_id},
                            tenant_id=str(worker.get("tenant_id") or "local"),
                            owner_id=str(worker.get("owner_id") or ""),
                        )
                    except Exception as cleanup_error:
                        logger.warning(
                            "Could not compensate delegated schedule creation after workspace closure: %s",
                            cleanup_error,
                        )
                        self.store.record_worker_termination_cleanup_failure(
                            worker_id,
                            "Delegated schedule cleanup failed after workspace closure",
                        )
                raise
            if runtime_bundle is not None:
                self.store.update_worker(
                    worker_id,
                    bootstrap_bundle_json=json.dumps(
                        merge_bootstrap_bundle(self._bootstrap_bundle_for(worker), runtime_bundle)
                    ),
                )
            self.store.add_event(
                str(worker.get("project_id") or ""),
                worker_id,
                None,
                "recurrence.created",
                f"Recurring schedule owned by {schedule_owner} starts at {first_occurrence.isoformat()}",
            )
            return result
        if runtime_bundle is not None:
            worker = self.store.update_worker(
                worker_id,
                bootstrap_bundle_json=json.dumps(
                    merge_bootstrap_bundle(self._bootstrap_bundle_for(worker), runtime_bundle)
                ),
            ) or worker
        try:
            definition = self.store.create_recurring_schedule_definition(
                worker_id=worker_id,
                project_id=str(worker.get("project_id") or ""),
                tenant_id=str(worker.get("tenant_id") or "local"),
                owner_id=str(worker.get("owner_id") or ""),
                scheduler_owner=recurrence_owner_storage_value(schedule_owner),
                instruction=normalized_instruction,
                schedule_text=str(schedule_text or ""),
                recurrence_type=str(spec["recurrence_type"]),
                interval_seconds=(int(spec["interval_seconds"]) if spec["interval_seconds"] is not None else None),
                local_time=str(spec["local_time"]),
                timezone_name=str(spec["timezone_name"]),
                dst_policy=str(spec["dst_policy"]),
                next_run_at=first_occurrence.isoformat(),
                cron_expression=str(spec["cron_expression"]),
                rrule=str(spec["rrule"]),
                starts_at=str(spec["starts_at"]) if spec["starts_at"] else None,
                ends_at=str(spec["ends_at"]) if spec["ends_at"] else None,
                enabled=bool(spec["enabled"]),
                overlap_policy=str(spec["overlap_policy"]),
                misfire_grace_seconds=int(spec["misfire_grace_seconds"]),
                catch_up_policy=str(spec["catch_up_policy"]),
                max_catch_up_occurrences=int(spec["max_catch_up_occurrences"]),
                jitter_seconds=int(spec["jitter_seconds"]),
                require_principal_authority=multi_user_security_enabled(),
            )
        except SchedulePrincipalAuthorityStoreError as exc:
            raise SchedulePrincipalAuthorityError(str(exc)) from exc
        except WorkerClosedStoreError as exc:
            raise ControlPlaneConflict(str(exc)) from exc
        self.store.add_event(
            str(worker.get("project_id") or ""),
            worker_id,
            None,
            "recurrence.created",
            f"Recurring schedule owned by {schedule_owner} starts at {first_occurrence.isoformat()}",
        )
        return definition

    def deactivate_recurring_schedule(
        self,
        definition_id: str,
        *,
        tenant_id: str,
        owner_id: str,
    ) -> dict | None:
        if self.recurring_schedule_owner() == DELEGATED_RECURRENCE_OWNER:
            result = self._delegated_schedule_call(
                "deactivate",
                {"definition_id": definition_id},
                tenant_id=tenant_id,
                owner_id=owner_id,
            )
            return result if isinstance(result, dict) else None
        return self.store.deactivate_recurring_schedule_definition(
            definition_id,
            tenant_id=tenant_id,
            owner_id=owner_id,
        )

    def update_recurring_schedule(
        self,
        definition_id: str,
        *,
        tenant_id: str,
        owner_id: str,
        updates: dict[str, object],
    ) -> dict | None:
        if updates.get("enabled") is True:
            self._require_schedule_principal_authority(
                tenant_id=tenant_id,
                owner_id=owner_id,
                establish=True,
            )
        if self.recurring_schedule_owner() == DELEGATED_RECURRENCE_OWNER:
            current = self._delegated_schedule_call(
                "get",
                {"definition_id": definition_id},
                tenant_id=tenant_id,
                owner_id=owner_id,
            )
            current_worker = (
                self.store.get_worker(
                    str(current.get("worker_id") or ""),
                    tenant_id=tenant_id or "local",
                    owner_id=owner_id,
                )
                if isinstance(current, dict)
                else None
            )
            if current_worker:
                self._ensure_execution_allowed(current_worker)
            result = self._delegated_schedule_call(
                "update",
                {"definition_id": definition_id, "updates": updates},
                tenant_id=tenant_id,
                owner_id=owner_id,
            )
            if updates.get("enabled") is True:
                try:
                    self._require_schedule_principal_authority(
                        tenant_id=tenant_id,
                        owner_id=owner_id,
                        establish=False,
                    )
                except SchedulePrincipalAuthorityError:
                    try:
                        self._delegated_schedule_call(
                            "deactivate",
                            {"definition_id": definition_id},
                            tenant_id=tenant_id,
                            owner_id=owner_id,
                        )
                    except Exception as cleanup_error:
                        logger.warning(
                            "Could not compensate delegated schedule enable after authority revocation: %s",
                            cleanup_error,
                        )
                    raise
            if current_worker:
                latest_worker = self.store.get_worker(
                    str(current_worker.get("worker_id") or ""),
                    tenant_id=tenant_id or "local",
                    owner_id=owner_id,
                )
                try:
                    if latest_worker:
                        self._ensure_execution_allowed(latest_worker)
                except ControlPlaneConflict:
                    if updates.get("enabled") is True:
                        try:
                            self._delegated_schedule_call(
                                "deactivate",
                                {"definition_id": definition_id},
                                tenant_id=tenant_id,
                                owner_id=owner_id,
                            )
                        except Exception as cleanup_error:
                            logger.warning(
                                "Could not compensate delegated schedule update after workspace close: %s",
                                cleanup_error,
                            )
                            self.store.record_worker_termination_cleanup_failure(
                                str(current_worker.get("worker_id") or ""),
                                "Delegated schedule cleanup failed after workspace closure",
                            )
                    raise
            return result if isinstance(result, dict) else None
        current = self.store.get_recurring_schedule_definition(
            definition_id,
            tenant_id=tenant_id,
            owner_id=owner_id,
        )
        if current is None:
            return None
        if current.get("retired_at"):
            raise ValueError("retired schedule cannot be changed")
        merged = {**current, **{key: value for key, value in updates.items() if value is not None}}
        spec = normalize_recurrence_spec(
            recurrence_type=str(merged.get("recurrence_type") or ""),
            interval_seconds=merged.get("interval_seconds"),
            local_time=str(merged.get("local_time") or ""),
            timezone_name=str(merged.get("timezone_name") or "UTC"),
            dst_policy=str(merged.get("dst_policy") or "next_valid_earliest"),
            cron_expression=str(merged.get("cron_expression") or ""),
            rrule=str(merged.get("rrule") or ""),
            starts_at=str(merged.get("starts_at") or "") or None,
            ends_at=str(merged.get("ends_at") or "") or None,
            enabled=bool(merged.get("enabled", True)),
            overlap_policy=str(merged.get("overlap_policy") or "skip"),
            misfire_grace_seconds=int(merged.get("misfire_grace_seconds") or 0),
            catch_up_policy=str(merged.get("catch_up_policy") or "skip"),
            max_catch_up_occurrences=int(merged.get("max_catch_up_occurrences") or 1),
            jitter_seconds=int(merged.get("jitter_seconds") or 0),
        )
        shape_fields = {
            "recurrence_type",
            "interval_seconds",
            "local_time",
            "timezone_name",
            "dst_policy",
            "cron_expression",
            "rrule",
            "starts_at",
            "ends_at",
        }
        next_run_at = str(current.get("next_run_at") or "")
        if bool(spec["enabled"]) and (shape_fields.intersection(updates) or not bool(current.get("enabled"))):
            next_run_at = first_occurrence_at(
                spec,
                now=datetime.now(timezone.utc).replace(microsecond=0),
            ).isoformat()
        normalized_updates = {
            "instruction": str(merged.get("instruction") or "").strip(),
            "schedule_text": str(merged.get("schedule_text") or ""),
            **spec,
            "next_run_at": next_run_at,
        }
        if not normalized_updates["instruction"]:
            raise ValueError("instruction is required")
        try:
            return self.store.update_recurring_schedule_definition(
                definition_id,
                tenant_id=tenant_id,
                owner_id=owner_id,
                fields=normalized_updates,
                require_principal_authority=(
                    bool(normalized_updates.get("enabled")) and multi_user_security_enabled()
                ),
            )
        except SchedulePrincipalAuthorityStoreError as exc:
            raise SchedulePrincipalAuthorityError(str(exc)) from exc
        except WorkerClosedStoreError as exc:
            raise ControlPlaneConflict(str(exc)) from exc

    def retire_recurring_schedule(
        self,
        definition_id: str,
        *,
        tenant_id: str,
        owner_id: str,
    ) -> dict | None:
        if self.recurring_schedule_owner() == DELEGATED_RECURRENCE_OWNER:
            result = self._delegated_schedule_call(
                "retire",
                {"definition_id": definition_id},
                tenant_id=tenant_id,
                owner_id=owner_id,
            )
            return result if isinstance(result, dict) else None
        return self.store.retire_recurring_schedule_definition(
            definition_id,
            tenant_id=tenant_id,
            owner_id=owner_id,
        )

    def run_recurring_schedule_now(
        self,
        definition_id: str,
        *,
        tenant_id: str,
        owner_id: str,
        idempotency_token: str,
    ) -> dict | None:
        self._require_schedule_principal_authority(
            tenant_id=tenant_id,
            owner_id=owner_id,
            establish=True,
        )
        if self.recurring_schedule_owner() == DELEGATED_RECURRENCE_OWNER:
            current = self._delegated_schedule_call(
                "get",
                {"definition_id": definition_id},
                tenant_id=tenant_id,
                owner_id=owner_id,
            )
            current_worker = (
                self.store.get_worker(
                    str(current.get("worker_id") or ""),
                    tenant_id=tenant_id or "local",
                    owner_id=owner_id,
                )
                if isinstance(current, dict)
                else None
            )
            if current_worker:
                self._ensure_execution_allowed(current_worker)
            result = self._delegated_schedule_call(
                "run_now",
                {"definition_id": definition_id, "idempotency_key": idempotency_token},
                tenant_id=tenant_id,
                owner_id=owner_id,
            )
            if current_worker:
                latest_worker = self.store.get_worker(
                    str(current_worker.get("worker_id") or ""),
                    tenant_id=tenant_id or "local",
                    owner_id=owner_id,
                )
                if latest_worker:
                    self._ensure_execution_allowed(latest_worker)
            return result if isinstance(result, dict) else None
        definition = self.store.get_recurring_schedule_definition(
            definition_id,
            tenant_id=tenant_id,
            owner_id=owner_id,
        )
        if definition is None or definition.get("retired_at"):
            return None
        self._revalidate_recurring_schedule_fire(definition)
        scheduled_for = datetime.now(timezone.utc).isoformat()
        try:
            schedule = self.store.create_recurring_schedule_run_now(
                definition_id,
                tenant_id=tenant_id,
                owner_id=owner_id,
                idempotency_token=idempotency_token,
                scheduled_for=scheduled_for,
                require_principal_authority=multi_user_security_enabled(),
            )
        except SchedulePrincipalAuthorityStoreError as exc:
            raise SchedulePrincipalAuthorityError(str(exc)) from exc
        except WorkerClosedStoreError as exc:
            raise ControlPlaneConflict(str(exc)) from exc
        if schedule is not None:
            schedule.update(
                {
                    "status": "scheduled",
                    "schedule_owner": NATIVE_RECURRENCE_OWNER,
                    "owner_action": "dispatch_here",
                }
            )
        return schedule

    def _revalidate_worker_schedule_fire(
        self,
        worker: dict,
        *,
        tenant_id: str,
        owner_id: str,
    ) -> dict:
        self._require_schedule_principal_authority(
            tenant_id=tenant_id,
            owner_id=owner_id,
            establish=False,
        )
        if str(worker.get("tenant_id") or "local") != str(tenant_id or "local"):
            raise ValueError("schedule tenant no longer matches its workspace")
        if str(worker.get("owner_id") or "") != str(owner_id or ""):
            raise ValueError("schedule owner no longer has access to its workspace")
        self._ensure_execution_allowed(worker)
        if str(worker.get("state") or "") == "failed":
            raise ValueError("scheduled workspace is unavailable")
        self._ensure_profile_allowed(str(worker.get("profile") or ""))
        if (
            str(os.environ.get("GLASSHIVE_SECURITY_MODE") or "").strip().lower() == "multi_user"
            and self.control_plane_store is None
        ):
            raise ValueError("standalone multi-user schedule revalidation is unavailable")
        if self.control_plane_store is None:
            return worker

        tenant_id = str(worker.get("tenant_id") or "local")
        owner_id = str(worker.get("owner_id") or "")
        selection = mission_provider_account_selection(worker)
        if selection is not None:
            account = self.control_plane_store.get_provider_account(
                account_id=selection.account_id,
                tenant_id=tenant_id,
                owner_id=owner_id,
            )
            if account is None or str(account.get("status") or "") != "ready":
                if selection.policy == "personal_required":
                    raise ScheduleActionRequiredError(
                        "provider_account_reconnect_required",
                        "The selected worker account needs to be reconnected before this schedule can run.",
                        "Open Connections, reconnect the account, and then try Run now again.",
                    )

        grants = self.control_plane_store.list_workspace_grants(
            tenant_id=tenant_id,
            owner_id=owner_id,
            worker_id=str(worker.get("worker_id") or ""),
        )
        accounts = {
            str(item.get("account_id") or ""): item
            for item in self.control_plane_store.list_provider_accounts(
                tenant_id=tenant_id,
                owner_id=owner_id,
            )
        }
        connections = {
            str(item.get("connection_id") or ""): item
            for item in self.control_plane_store.list_connections(
                tenant_id=tenant_id,
                owner_id=owner_id,
            )
        }
        for grant in grants:
            account_id = str(grant.get("account_id") or "")
            connection_id = str(grant.get("connection_id") or "")
            if account_id and str(accounts.get(account_id, {}).get("status") or "") != "ready":
                raise ScheduleActionRequiredError(
                    "capability_account_reconnect_required",
                    "A worker account used by this workspace needs to be reconnected.",
                    "Open Connections, reconnect the account, and then try Run now again.",
                )
            if connection_id and str(connections.get(connection_id, {}).get("status") or "") != "ready":
                raise ScheduleActionRequiredError(
                    "connection_reconnect_required",
                    "A connected service used by this workspace needs attention.",
                    "Open Connections, repair the service connection, and then try Run now again.",
                )
        return worker

    def _require_schedule_principal_authority(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        establish: bool,
    ) -> dict | None:
        if not multi_user_security_enabled():
            return None
        authority = self.store.get_schedule_principal_authority(
            tenant_id=tenant_id,
            owner_id=owner_id,
        )
        if authority is None and establish:
            authority = self.store.ensure_schedule_principal_authority(
                tenant_id=tenant_id,
                owner_id=owner_id,
            )
        if authority is None:
            raise SchedulePrincipalAuthorityError(
                "scheduled principal authority must be renewed before this schedule can run"
            )
        if not bool(authority.get("enabled")):
            raise SchedulePrincipalAuthorityError(
                "scheduled principal has been disabled"
            )
        return authority

    def set_schedule_principal_authority(
        self,
        *,
        tenant_id: str,
        owner_id: str,
        enabled: bool,
    ) -> dict:
        result = self.store.set_schedule_principal_authority(
            tenant_id=tenant_id,
            owner_id=owner_id,
            enabled=enabled,
        )
        result["deactivated_delegated_definitions"] = 0
        if not enabled and self.recurring_schedule_owner() == DELEGATED_RECURRENCE_OWNER:
            delegated = self._delegated_schedule_call(
                "deactivate_owner",
                {},
                tenant_id=tenant_id,
                owner_id=owner_id,
            )
            if not isinstance(delegated, dict):
                raise RuntimeError("Viventium Scheduling Cortex returned invalid authority data")
            result["deactivated_delegated_definitions"] = int(
                delegated.get("deactivated") or 0
            )
        return result

    def _revalidate_recurring_schedule_fire(self, definition: dict[str, object]) -> dict:
        worker = self.require_worker(str(definition.get("worker_id") or ""))
        return self._revalidate_worker_schedule_fire(
            worker,
            tenant_id=str(definition.get("tenant_id") or "local"),
            owner_id=str(definition.get("owner_id") or ""),
        )

    def revalidate_scheduling_cortex_workspace_fire(
        self,
        worker: dict,
        *,
        tenant_id: str,
        owner_id: str,
    ) -> dict:
        """Recheck the current reusable workspace authority before a delegated fire mutates state."""

        return self._revalidate_worker_schedule_fire(
            worker,
            tenant_id=tenant_id,
            owner_id=owner_id,
        )

    @staticmethod
    def _recurrence_dispatch_at(
        definition: dict[str, object],
        *,
        scheduled_for: datetime,
        detected_at: datetime,
    ) -> datetime:
        jitter_bound = max(0, int(definition.get("jitter_seconds") or 0))
        if jitter_bound == 0:
            return max(scheduled_for, detected_at)
        digest = hashlib.sha256(
            f"{definition.get('definition_id')}\0{scheduled_for.isoformat()}".encode("utf-8")
        ).digest()
        jitter = int.from_bytes(digest[:8], "big") % (jitter_bound + 1)
        return max(scheduled_for, detected_at) + timedelta(seconds=jitter)

    def _materialize_due_recurring_schedules(self, now: datetime) -> list[dict[str, object]]:
        try:
            owner = self.recurring_schedule_owner()
        except ValueError as exc:
            logger.error("GlassHive native recurrence is disabled by scheduler ownership configuration: %s", exc)
            return []
        if owner != NATIVE_RECURRENCE_OWNER:
            return []
        now_iso = now.astimezone(timezone.utc).isoformat()
        materialized: list[dict[str, object]] = []
        definitions = self.store.list_due_recurring_schedule_definitions(
            now_iso,
            scheduler_owner=recurrence_owner_storage_value(NATIVE_RECURRENCE_OWNER),
            limit=50,
        )
        for definition in definitions:
            try:
                occurrences, next_occurrence = due_occurrences_and_next(definition, now=now)
            except (TypeError, ValueError, OverflowError) as exc:
                logger.error(
                    "Skipped invalid recurring schedule definition %s: %s",
                    definition.get("definition_id"),
                    exc,
                )
                continue
            if not occurrences:
                continue
            expected_next = str(definition.get("next_run_at") or "")
            overlap = str(definition.get("overlap_policy") or "skip")
            for index, occurrence_decision in enumerate(occurrences):
                occurrence = occurrence_decision["scheduled_for"]
                assert isinstance(occurrence, datetime)
                state = str(occurrence_decision.get("state") or "pending")
                outcome = str(occurrence_decision.get("outcome") or "pending")
                if state == "pending" and overlap == "skip":
                    worker_id = str(definition.get("worker_id") or "")
                    if self.store.get_active_run(worker_id) or self.store.has_queued_runs(worker_id):
                        state, outcome = "skipped", "overlap_skipped"
                if state == "pending":
                    try:
                        self._revalidate_recurring_schedule_fire(definition)
                    except (KeyError, RuntimeError, ValueError) as exc:
                        state, outcome = "action_required", str(exc)
                following = (
                    occurrences[index + 1]["scheduled_for"]
                    if index + 1 < len(occurrences)
                    else next_occurrence
                )
                deactivate_after = following is None or state == "action_required"
                stored_next = occurrence if following is None else following
                assert isinstance(stored_next, datetime)
                dispatch_at = self._recurrence_dispatch_at(
                    definition,
                    scheduled_for=occurrence,
                    detected_at=now,
                )
                try:
                    schedule = self.store.materialize_recurring_schedule_occurrence(
                        str(definition.get("definition_id") or ""),
                        expected_next_run_at=expected_next,
                        scheduled_for=occurrence.isoformat(),
                        next_run_at=stored_next.isoformat(),
                        detected_at=now_iso,
                        dispatch_at=dispatch_at.isoformat(),
                        occurrence_state=state,
                        outcome=outcome,
                        deactivate_after=deactivate_after,
                    )
                except WorkerClosedStoreError:
                    break
                if schedule:
                    materialized.append(schedule)
                expected_next = stored_next.isoformat()
                if state == "action_required":
                    break
        return materialized

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
        self._require_schedule_principal_authority(
            tenant_id=str(worker.get("tenant_id") or "local"),
            owner_id=str(worker.get("owner_id") or ""),
            establish=True,
        )
        try:
            schedule = self.store.create_scheduled_run(
                worker_id=worker_id,
                project_id=str(worker.get("project_id") or ""),
                tenant_id=str(worker.get("tenant_id") or "local"),
                owner_id=str(worker.get("owner_id") or ""),
                instruction=instruction,
                schedule_text=str(schedule_text or ""),
                run_at=resolved_run_at,
                require_principal_authority=multi_user_security_enabled(),
            )
        except SchedulePrincipalAuthorityStoreError as exc:
            raise SchedulePrincipalAuthorityError(str(exc)) from exc
        except WorkerClosedStoreError as exc:
            raise ControlPlaneConflict(str(exc)) from exc
        self.store.add_event(
            str(worker.get("project_id") or ""),
            worker_id,
            None,
            "schedule.created",
            f"Scheduled run for {resolved_run_at}",
        )
        self._emit_callback(worker, "schedule.created", message=f"Scheduled run for {resolved_run_at}")
        return schedule

    def _recurring_schedule_capacity_issue(self, schedule: dict[str, object]) -> str:
        schedule_id = str(schedule.get("schedule_id") or "")
        if not self.store.recurring_occurrence_for_schedule(schedule_id):
            return ""
        tenant_id = str(schedule.get("tenant_id") or "local")
        owner_id = str(schedule.get("owner_id") or "")
        user_limit = _bounded_int_env(
            "GLASSHIVE_MAX_CONCURRENT_RECURRING_RUNS_PER_USER",
            4,
            min_value=1,
            max_value=1000,
        )
        tenant_limit = _bounded_int_env(
            "GLASSHIVE_MAX_CONCURRENT_RECURRING_RUNS_PER_TENANT",
            32,
            min_value=1,
            max_value=10000,
        )
        if self.store.count_active_runs(tenant_id=tenant_id, owner_id=owner_id) >= user_limit:
            return "user_concurrency_deferred"
        if self.store.count_active_runs(tenant_id=tenant_id) >= tenant_limit:
            return "tenant_concurrency_deferred"
        return ""

    def process_due_schedules_once(self, now_iso: str | None = None) -> list[dict[str, object]]:
        processed: list[dict[str, object]] = []
        now = (
            parse_aware_utc(now_iso, label="now_iso")
            if now_iso is not None
            else datetime.now(timezone.utc)
        )
        self.store.recover_stale_recurring_occurrence_claims(now.isoformat())
        self._materialize_due_recurring_schedules(now)
        due = self.store.list_due_schedules(now.isoformat(), limit=50)
        for item in due:
            schedule_id = str(item.get("schedule_id") or "")
            capacity_issue = self._recurring_schedule_capacity_issue(item)
            if capacity_issue:
                self.store.mark_recurring_occurrence_retryable(schedule_id, capacity_issue)
                continue
            claimed = self.store.claim_schedule(schedule_id)
            if not claimed:
                continue
            try:
                run = self.assign_scheduled_run(claimed)
                run_id = str(run.get("run_id") or "")
                updated = self.store.get_schedule(schedule_id)
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
            except SchedulePrincipalAuthorityError:
                processed.append(self.store.get_schedule(schedule_id) or claimed)
            except ControlPlaneConflict:
                processed.append(self.store.get_schedule(schedule_id) or claimed)
            except Exception as exc:
                updated = self.store.finalize_schedule(schedule_id, state="failed", last_error=str(exc))
                processed.append(updated or claimed)
        return processed

    def assign_scheduled_run(
        self,
        schedule: dict[str, object],
        *,
        runtime_bundle: dict | None = None,
    ) -> dict:
        """Create-or-get the one stable run reserved for a claimed schedule."""

        schedule_id = str(schedule.get("schedule_id") or "").strip()
        worker_id = str(schedule.get("worker_id") or "").strip()
        if not schedule_id or not worker_id:
            raise ValueError("Scheduled dispatch requires schedule and worker ids")
        worker = self.require_worker(worker_id)
        self._revalidate_worker_schedule_fire(
            worker,
            tenant_id=str(schedule.get("tenant_id") or "local"),
            owner_id=str(schedule.get("owner_id") or ""),
        )
        self._ensure_runtime_available(
            str(worker.get("profile") or ""),
            str(worker.get("execution_mode") or "docker"),
        )
        worker = self._refresh_worker_model_for_profile(worker)
        resumed = worker["state"] == "paused"
        try:
            run, created = self.store.create_or_get_run_for_schedule(
                schedule_id,
                runtime_bundle=runtime_bundle,
                require_principal_authority=multi_user_security_enabled(),
            )
        except SchedulePrincipalAuthorityStoreError as exc:
            raise SchedulePrincipalAuthorityError(str(exc)) from exc
        except WorkerClosedStoreError as exc:
            self.store.finalize_schedule(schedule_id, state="cancelled", last_error=str(exc))
            raise ControlPlaneConflict(str(exc)) from exc
        if resumed:
            self.store.add_event(
                worker["project_id"],
                worker_id,
                None,
                "worker.resumed",
                "Worker resume queued for the next run",
            )
            worker = self.store.get_worker(worker_id) or worker
        if created:
            instruction = str(schedule.get("instruction") or "")
            self.store.add_event(
                worker["project_id"],
                worker_id,
                run["run_id"],
                "schedule.queued",
                instruction,
            )
            self._emit_callback(
                worker,
                "schedule.queued",
                run=run,
                message=instruction,
            )
        self._ensure_worker_processor(worker_id)
        return run

    def duplicate_worker(
        self,
        source_worker_id: str,
        project_id: str,
        owner_id: str,
        name: str,
        role: str,
        reapproval_items: list[dict[str, object]] | None = None,
    ) -> dict:
        source_worker = self.require_worker(source_worker_id)
        required_items = [dict(item) for item in (reapproval_items or []) if isinstance(item, dict)]
        initial_report: dict[str, object] = {
            "duplication_state": "pending",
            "source_state": "pending",
            "copied_files": 0,
            "skipped_items": 0,
            "capabilities_requiring_reapproval": len(required_items),
            "reapproval_items": required_items,
        }
        bootstrap_bundle = _duplicate_bootstrap_bundle(self._bootstrap_bundle_for(source_worker))
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
            workspace_root=None,
            bootstrap_profile=str(source_worker.get("bootstrap_profile") or "") or None,
            bootstrap_bundle=bootstrap_bundle,
            workspace_kind="named",
            tags=normalize_workspace_tags(source_worker.get("tags") if isinstance(source_worker.get("tags"), list) else []),
            duplication_report=initial_report,
            start_synchronously=False,
        )
        try:
            copy_report = self._copy_workspace_contents(source_worker, duplicated)
        except Exception as exc:
            current_duplicate = self.store.get_worker(str(duplicated["worker_id"])) or duplicated
            cleanup_ready = False
            try:
                stopped = self.runtime.terminate_worker(current_duplicate)
                if stopped.pid:
                    raise RuntimeError("duplicate cleanup left worker compute active")
                self._apply_runtime_info(
                    str(duplicated["worker_id"]),
                    stopped,
                    state="failed",
                    last_error=str(exc),
                    compute_released_at=utc_now(),
                )
                cleanup_ready = True
            except Exception as cleanup_exc:
                cleanup_message = public_callback_message_text(str(cleanup_exc)) or "duplicate cleanup failed"
                self.store.update_worker(
                    duplicated["worker_id"],
                    state="failed",
                    last_error=f"{exc}; {cleanup_message}",
                )
            self.store.add_event(
                project_id,
                duplicated["worker_id"],
                None,
                "worker.duplicate_failed",
                str(exc),
            )
            if cleanup_ready:
                self.store.delete_unstarted_worker(
                    str(duplicated["worker_id"]),
                    project_id=project_id,
                    tenant_id=str(source_worker.get("tenant_id") or "local"),
                    owner_id=owner_id,
                )
            raise
        duplication_report = dict(copy_report)
        if required_items:
            duplication_report.update(
                {
                    "duplication_state": "complete",
                    "capabilities_requiring_reapproval": len(required_items),
                    "reapproval_items": required_items,
                }
            )
        self.store.update_worker(
            duplicated["worker_id"],
            duplication_report_json=json.dumps(duplication_report, sort_keys=True, separators=(",", ":")),
        )
        self.store.add_event(
            project_id,
            duplicated["worker_id"],
            None,
            "worker.duplicated",
            "Workspace duplicated: "
            f"{duplication_report['copied_files']} files copied, "
            f"{duplication_report['skipped_items']} items skipped",
        )
        updated = self.store.get_worker(duplicated["worker_id"]) or duplicated
        return {**updated, "duplication_report": duplication_report}

    def save_workspace_template(
        self,
        worker_id: str,
        *,
        tenant_id: str,
        owner_id: str,
        name: str,
        description: str = "",
        lineage_id: str | None = None,
    ) -> dict[str, object]:
        if self.control_plane_store is None:
            raise RuntimeError("Workspace templates require the user control plane")
        worker = self.require_worker(worker_id)
        if str(worker.get("tenant_id") or "local") != str(tenant_id or "local") or str(
            worker.get("owner_id") or ""
        ) != str(owner_id or ""):
            raise KeyError("Workspace not found")
        self.require_project(str(worker.get("project_id") or ""))
        library_refs = self.control_plane_store.workspace_template_library_refs(
            tenant_id=tenant_id,
            owner_id=owner_id,
            worker_id=worker_id,
        )
        source_bootstrap = self._bootstrap_bundle_for(worker)
        sanitized_bootstrap = _duplicate_bootstrap_bundle(source_bootstrap) or {}
        provider_account_ref: dict[str, str] | None = None
        raw_provider_ref = source_bootstrap.get("provider_account") if isinstance(source_bootstrap, dict) else None
        if isinstance(raw_provider_ref, dict):
            policy = str(raw_provider_ref.get("policy") or "").strip().lower()
            account_id = str(raw_provider_ref.get("account_id") or "").strip()
            if policy == "legacy" and not account_id:
                provider_account_ref = {"policy": "legacy"}
            elif policy in {"personal_preferred", "personal_required"}:
                if not account_id:
                    provider_account_ref = {"policy": policy}
                else:
                    account = self.control_plane_store.get_provider_account(
                        account_id=account_id,
                        tenant_id=tenant_id,
                        owner_id=owner_id,
                    )
                    supported = PROFILE_ACCOUNT_PROVIDERS.get(str(worker.get("profile") or ""), set())
                    if account is not None and str(account.get("provider") or "").lower() in supported:
                        provider_account_ref = {
                            "policy": policy,
                            "account_id": account_id,
                            "provider": str(account.get("provider") or ""),
                        }
        content: dict[str, object] = {
            "schema_version": 1,
            "project": {
                "title": str(name or "Workspace template").strip()[:200],
                "goal": str(description or "").strip()[:1000],
            },
            "worker": {
                "name": str(worker.get("name") or name).strip()[:160],
                "role": str(worker.get("role") or "main").strip()[:160],
                "profile": str(worker.get("profile") or "codex-cli").strip(),
                "execution_mode": str(worker.get("execution_mode") or "docker").strip(),
                "bootstrap_profile": str(worker.get("bootstrap_profile") or "").strip(),
                "bootstrap_bundle": sanitized_bootstrap,
                **({"provider_account_ref": provider_account_ref} if provider_account_ref else {}),
                "tags": normalize_workspace_tags(
                    worker.get("tags") if isinstance(worker.get("tags"), list) else []
                ),
            },
            "library_refs": library_refs,
        }
        return self.control_plane_store.create_workspace_template(
            tenant_id=tenant_id,
            owner_id=owner_id,
            name=name,
            description=description,
            content=content,
            lineage_id=lineage_id,
        )

    def list_workspace_templates(self, *, tenant_id: str, owner_id: str) -> list[dict[str, object]]:
        if self.control_plane_store is None:
            return []
        return self.control_plane_store.list_workspace_templates(
            tenant_id=tenant_id,
            owner_id=owner_id,
        )

    def instantiate_workspace_template(
        self,
        template_id: str,
        *,
        tenant_id: str,
        owner_id: str,
        idempotency_key: str,
        name: str | None = None,
    ) -> dict[str, object] | None:
        if self.control_plane_store is None:
            raise RuntimeError("Workspace templates require the user control plane")
        template = self.control_plane_store.get_workspace_template(
            template_id=template_id,
            tenant_id=tenant_id,
            owner_id=owner_id,
        )
        if template is None:
            return None
        content = template.get("content")
        if not isinstance(content, dict) or int(content.get("schema_version") or 0) != 1:
            raise ValueError("Workspace template schema is unsupported")
        project_spec = content.get("project")
        worker_spec = content.get("worker")
        library_refs = content.get("library_refs")
        if not isinstance(project_spec, dict) or not isinstance(worker_spec, dict) or not isinstance(library_refs, list):
            raise ValueError("Workspace template content is invalid")
        profile = str(worker_spec.get("profile") or "").strip()
        execution_mode = str(worker_spec.get("execution_mode") or "").strip()
        self._ensure_execution_allowed(execution_mode)
        self._ensure_profile_allowed(profile)
        approvals_required = self.control_plane_store.validate_workspace_template_libraries(
            library_refs=[dict(item) for item in library_refs if isinstance(item, dict)],
            profile=profile,
        )
        validated_library_refs = {
            str(item.get("library_id") or ""): item
            for item in approvals_required
            if isinstance(item, dict) and str(item.get("library_id") or "")
        }
        reapproval_items = [
            {
                "action_id": "rea_" + hashlib.sha256(
                    f"library_grant\0{str(reference.get('library_id') or '')}".encode("utf-8")
                ).hexdigest()[:24],
                "kind": "library",
                "resolution": "library_grant",
                "reference": str(reference.get("library_id") or ""),
                "label": str(
                    validated_library_refs.get(str(reference.get("library_id") or ""), {}).get("stable_id")
                    or reference.get("stable_id")
                    or "Library capability"
                )[:160],
                "route": "library",
                "scopes": sorted(
                    {str(scope) for scope in (reference.get("scopes") or []) if str(scope)}
                ),
            }
            for reference in library_refs
            if isinstance(reference, dict)
            and str(reference.get("library_id") or "") in validated_library_refs
        ]
        provider_account_ref = worker_spec.get("provider_account_ref")
        provider_account_selection: dict[str, str] | None = None
        if provider_account_ref is not None:
            if not isinstance(provider_account_ref, dict):
                raise ValueError("Workspace template provider account reference is invalid")
            policy = str(provider_account_ref.get("policy") or "").strip().lower()
            account_id = str(provider_account_ref.get("account_id") or "").strip()
            if policy not in WORKSPACE_ACCOUNT_POLICIES:
                raise ValueError("Workspace template provider account policy is invalid")
            if policy == "legacy":
                if account_id:
                    raise ValueError("Workspace template deployment account policy is invalid")
                provider_account_selection = {"policy": "legacy"}
            elif not account_id:
                if policy == "personal_required":
                    raise ValueError("Workspace template requires a selected personal provider account")
                provider_account_selection = {"policy": policy}
            else:
                account = self.control_plane_store.get_provider_account(
                    account_id=account_id,
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                )
                supported = PROFILE_ACCOUNT_PROVIDERS.get(profile, set())
                if account is None:
                    raise ValueError("Workspace template provider account is not available for this user")
                if str(account.get("provider") or "").strip().lower() not in supported:
                    raise ValueError("Workspace template provider account does not match the worker profile")
                if str(account.get("status") or "").strip().lower() != "ready":
                    raise ValueError("Workspace template provider account must be reconnected before use")
                provider_account_selection = {"policy": policy, "account_id": account_id}
        requested_name = str(name or worker_spec.get("name") or template.get("name") or "Workspace").strip()[:160]
        request_payload = {"template_id": template_id, "name": requested_name}
        reservation = self.control_plane_store.reserve_workspace_template_instantiation(
            template_id=template_id,
            tenant_id=tenant_id,
            owner_id=owner_id,
            idempotency_key=idempotency_key,
            request_payload=request_payload,
        )
        if reservation.get("idempotent_replay"):
            project = self.store.get_project(str(reservation.get("project_id") or ""))
            worker = self.store.get_worker(str(reservation.get("worker_id") or ""))
            if not project or not worker:
                raise RuntimeError("Completed template instantiation record is inconsistent")
            return {
                "template": {key: value for key, value in template.items() if key != "content"},
                "project": project,
                "workspace": worker,
                "approvals_required": approvals_required,
                "idempotent_replay": True,
            }
        reserved_project_id = str(reservation.get("project_id") or "").strip()
        if not reserved_project_id:
            raise RuntimeError("Template instantiation reservation has no project identity")
        try:
            project = self.create_project(
                owner_id,
                str(project_spec.get("title") or template.get("name") or "Workspace template")[:200],
                str(project_spec.get("goal") or "")[:10000],
                profile,
                tenant_id=tenant_id,
                project_id=reserved_project_id,
            )
            template_bootstrap = (
                dict(worker_spec.get("bootstrap_bundle"))
                if isinstance(worker_spec.get("bootstrap_bundle"), dict)
                else {}
            )
            if provider_account_selection is not None:
                template_bootstrap["provider_account"] = provider_account_selection
            worker = self.create_worker(
                project_id=str(project["project_id"]),
                tenant_id=tenant_id,
                owner_id=owner_id,
                name=requested_name,
                role=str(worker_spec.get("role") or "main")[:160],
                profile=profile,
                backend="",
                execution_mode=execution_mode,
                alias=None,
                workspace_root=None,
                bootstrap_profile=str(worker_spec.get("bootstrap_profile") or "") or None,
                bootstrap_bundle=template_bootstrap or None,
                workspace_kind="named",
                tags=normalize_workspace_tags(
                    worker_spec.get("tags") if isinstance(worker_spec.get("tags"), list) else []
                ),
                duplication_report={
                    "duplication_state": "complete",
                    "source_state": "template",
                    "copied_files": 0,
                    "skipped_items": 0,
                    "capabilities_requiring_reapproval": len(reapproval_items),
                    "reapproval_items": reapproval_items,
                },
                start_synchronously=False,
            )
            self.control_plane_store.complete_workspace_template_instantiation(
                tenant_id=tenant_id,
                owner_id=owner_id,
                idempotency_key=idempotency_key,
                project_id=str(project["project_id"]),
                worker_id=str(worker["worker_id"]),
            )
        except Exception:
            current_project = self.store.get_project(
                reserved_project_id,
                tenant_id=tenant_id,
                owner_id=owner_id,
            )
            current_workers = (
                self.store.list_workers(
                    reserved_project_id,
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                )
                if current_project
                else []
            )
            failed_worker_id = (
                str(current_workers[0].get("worker_id") or "")
                if len(current_workers) == 1
                else ""
            )
            try:
                self.control_plane_store.fail_workspace_template_instantiation(
                    tenant_id=tenant_id,
                    owner_id=owner_id,
                    idempotency_key=idempotency_key,
                    project_id=reserved_project_id,
                    worker_id=failed_worker_id or None,
                )
                worker_cleaned = not current_workers
                if failed_worker_id:
                    current_worker = current_workers[0]
                    stopped = self.runtime.terminate_worker(current_worker)
                    if not stopped.pid:
                        self._apply_runtime_info(
                            failed_worker_id,
                            stopped,
                            state="failed",
                            last_error="Template instantiation was rolled back",
                            compute_released_at=utc_now(),
                        )
                        worker_cleaned = self.store.delete_unstarted_worker(
                            failed_worker_id,
                            project_id=reserved_project_id,
                            tenant_id=tenant_id,
                            owner_id=owner_id,
                        )
                project_cleaned = current_project is None or (
                    worker_cleaned
                    and self.store.delete_project_if_empty(
                        reserved_project_id,
                        tenant_id=tenant_id,
                        owner_id=owner_id,
                    )
                )
                if worker_cleaned and project_cleaned:
                    self.control_plane_store.complete_workspace_template_cleanup(
                        tenant_id=tenant_id,
                        owner_id=owner_id,
                        idempotency_key=idempotency_key,
                        project_id=reserved_project_id,
                        worker_id=failed_worker_id or None,
                    )
            except Exception as cleanup_exc:
                logger.error(
                    "Template instantiation rollback could not be completed for project %s: %s",
                    reserved_project_id,
                    public_callback_message_text(str(cleanup_exc)) or "cleanup failed",
                )
            raise
        return {
            "template": {key: value for key, value in template.items() if key != "content"},
            "project": project,
            "workspace": worker,
            "approvals_required": approvals_required,
            "idempotent_replay": False,
        }

    def assign_run(
        self,
        worker_id: str,
        instruction: str,
        event_type: str = "run.queued",
        runtime_bundle: dict | None = None,
        start_processor: bool = True,
    ) -> dict:
        worker = self.require_worker(worker_id)
        self._ensure_execution_allowed(worker)
        self._ensure_runtime_available(
            str(worker.get("profile") or ""),
            str(worker.get("execution_mode") or "docker"),
        )
        worker = self._refresh_worker_model_for_profile(worker)
        if runtime_bundle is not None:
            worker = self.store.update_worker(
                worker_id,
                bootstrap_bundle_json=json.dumps(
                    merge_bootstrap_bundle(self._bootstrap_bundle_for(worker), runtime_bundle)
                ),
            ) or worker
        resumed = worker["state"] == "paused"
        try:
            run = self.store.create_run(
                worker_id,
                worker["project_id"],
                instruction,
                state="queued",
                resume_paused=True,
            )
        except WorkerClosedStoreError as exc:
            raise ControlPlaneConflict(str(exc)) from exc
        if resumed:
            self.store.add_event(
                worker["project_id"],
                worker_id,
                None,
                "worker.resumed",
                "Worker resume queued for the next run",
            )
            worker = self.store.get_worker(worker_id) or worker
        self.store.add_event(worker["project_id"], worker_id, run["run_id"], event_type, instruction)
        self._emit_callback(worker, event_type, run=run, message=instruction)
        if start_processor:
            self._ensure_worker_processor(worker_id)
        return run
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
        if str(worker.get("state") or "") in CLOSED_WORKER_STATES:
            raise RunActionError("worker_ended", "The workspace has ended.", status_code=409)

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
                self.interrupt_worker(worker_id, run_id=run_id)
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
            settled = self.store.get_run(run_id)
            settled_state = str((settled or {}).get("state") or "")
            if settled_state not in {"interrupted", "cancelled"}:
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
                result_code="cancellation_requested",
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


    @staticmethod
    def _runtime_worker_for_run(worker: dict, run: dict) -> dict:
        """Overlay one run's ephemeral authority without mutating the reusable workspace."""

        raw_bundle = str(run.get("runtime_bundle_json") or "").strip()
        if not raw_bundle:
            return worker
        try:
            runtime_bundle = json.loads(raw_bundle)
        except json.JSONDecodeError as exc:
            raise ValueError("Run-scoped bootstrap authority is invalid") from exc
        if not isinstance(runtime_bundle, dict):
            raise ValueError("Run-scoped bootstrap authority is invalid")
        try:
            persistent_bundle = json.loads(str(worker.get("bootstrap_bundle_json") or "{}"))
        except json.JSONDecodeError:
            persistent_bundle = {}
        if not isinstance(persistent_bundle, dict):
            persistent_bundle = {}
        runtime_worker = dict(worker)
        runtime_worker["bootstrap_bundle_json"] = json.dumps(
            merge_bootstrap_bundle(persistent_bundle, runtime_bundle) or {},
            sort_keys=True,
            separators=(",", ":"),
        )
        return runtime_worker

    def record_launch_failed(self, worker_id: str, reason: str) -> dict:
        worker = self.require_worker(worker_id)
        self._ensure_execution_allowed(worker)
        self.store.cancel_pending_runs(worker_id, error_text=reason, state="failed")
        updated = self.store.update_worker(worker_id, state="failed", last_error=reason)
        self.store.add_event(worker["project_id"], worker_id, None, "worker.launch_failed", reason)
        return updated or worker

    def send_message(self, worker_id: str, message: str) -> dict:
        instruction = self._instruction_for_message(message)
        return self.assign_run(worker_id, instruction, event_type="worker.message")

    def steer_worker(self, worker_id: str, message: str) -> dict:
        worker = self.require_worker(worker_id)
        self._ensure_execution_allowed(worker)
        active_run = self.store.get_active_run(worker_id)
        if active_run:
            interrupted = self.interrupt_worker(worker_id)
            worker = self.store.get_worker(worker_id) or interrupted or worker
            self.store.add_event(
                worker["project_id"],
                worker_id,
                active_run["run_id"],
                "worker.steer",
                "Active run interrupted so the workspace can follow the new steer instruction.",
            )
        instruction = self._instruction_for_steer(message)
        return self.assign_run(worker_id, instruction, event_type="worker.steer")

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
        active_run = self.store.get_active_run(worker_id)
        effective_run_id = str(run_id or (active_run or {}).get("run_id") or "").strip() or None
        if worker.get("state") != "running":
            self.store.update_worker_state(worker_id, "starting", last_error="")
        try:
            launched = self.runtime.desktop_action(
                worker,
                action,
                url=url,
                run_id=effective_run_id,
            )
        except TypeError as exc:
            if "run_id" not in str(exc):
                raise
            launched = self.runtime.desktop_action(worker, action, url=url)
        except Exception as exc:
            self.store.update_worker(worker_id, state=str(worker.get("state") or "failed"), last_error=str(exc))
            raise
        self._reject_closed_after_runtime_activity(
            worker_id,
            fallback_worker=worker,
            active_run_id=str((active_run or {}).get("run_id") or ""),
            context="a desktop action",
        )
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

    def pause_worker(self, worker_id: str) -> dict:
        worker = self.require_worker(worker_id)
        self._ensure_execution_allowed(worker)
        # Decide whether this is an already-started, freeze-capable run while
        # holding the same boundary lock used by the external runtime start.
        # Otherwise a start could publish run.started between this decision and
        # processor invalidation, turning a real running task into a stranded
        # pre-start pause.
        with self._runtime_start_lock(worker_id):
            worker = self.require_worker(worker_id)
            self._ensure_execution_allowed(worker)
            active_run = self.store.get_active_run(worker_id)
            run_started = bool(
                active_run
                and self.store.has_run_event(active_run["run_id"], "run.started")
            )
            if not run_started:
                self._invalidate_worker_processor(worker_id)
        runtime_worker = {
            **worker,
            "_active_run_id": str((active_run or {}).get("run_id") or ""),
        }
        info = self.runtime.pause_worker(runtime_worker)
        updated = self._apply_runtime_info(worker_id, info, state="paused", last_error=worker.get("last_error") or "")
        if updated and str(updated.get("state") or "") in CLOSED_WORKER_STATES:
            raise ControlPlaneConflict(
                "Workspace is closed; create a new workspace for new work"
            )
        paused_run = None
        if active_run and not run_started:
            paused_run = self.store.finalize_run_if_state(
                active_run["run_id"],
                "running",
                "paused",
                output_text=active_run.get("output_text", ""),
                error_text="Paused by operator",
            )
            if paused_run:
                self.store.finalize_schedule_for_run(
                    active_run["run_id"],
                    state="failed",
                    last_error="Paused by operator",
                )
        self._wake_host_capacity_waiters(updated or worker)
        self.store.add_event(worker["project_id"], worker_id, active_run["run_id"] if active_run else None, "worker.paused", "Worker paused")
        self._emit_callback(worker, "worker.paused", run=paused_run or active_run, message="Worker paused")
        return updated or worker

    def interrupt_worker(self, worker_id: str, run_id: str | None = None) -> dict:
        worker = self.require_worker(worker_id)
        self._ensure_execution_allowed(worker)
        self._invalidate_worker_processor(worker_id)
        with self._runtime_start_lock(worker_id):
            worker = self.require_worker(worker_id)
            self._ensure_execution_allowed(worker)
        active_run = self.store.get_active_run(worker_id)
        if run_id and (not active_run or str(active_run.get("run_id") or "") != str(run_id)):
            return worker
        try:
            info = self.runtime.interrupt_worker(
                worker,
                run_id=str(active_run["run_id"]) if active_run else None,
            )
        except TypeError as exc:
            if "run_id" not in str(exc):
                raise
            info = self.runtime.interrupt_worker(worker)
        updated = self._apply_runtime_info(worker_id, info, state="ready", last_error="")
        if updated and str(updated.get("state") or "") in CLOSED_WORKER_STATES:
            raise ControlPlaneConflict(
                "Workspace is closed; create a new workspace for new work"
            )
        interrupted_run = None
        if active_run:
            interrupted_run = self.store.finalize_run_if_state(
                active_run["run_id"],
                "running",
                "interrupted",
                output_text=active_run.get("output_text", ""),
                error_text="Interrupted by operator",
            )
            if interrupted_run is None:
                current = self.store.get_worker(worker_id)
                if current and str(current.get("state") or "") in CLOSED_WORKER_STATES:
                    raise ControlPlaneConflict(
                        "Workspace is closed; create a new workspace for new work"
                    )
        self._wake_host_capacity_waiters(updated or worker)
        self.store.add_event(worker["project_id"], worker_id, active_run["run_id"] if active_run else None, "worker.interrupted", "Worker interrupted")
        self._emit_callback(
            worker,
            "worker.interrupted",
            run=interrupted_run or active_run,
            message="Worker interrupted",
        )
        if interrupted_run:
            self.store.add_event(
                worker["project_id"],
                worker_id,
                interrupted_run["run_id"],
                "run.interrupted",
                "Run interruption accepted",
            )
            self._emit_callback(
                worker,
                "run.interrupted",
                run=interrupted_run,
                message="Run interruption accepted",
            )
        return updated or worker

    def cancel_run(self, worker_id: str, run_id: str) -> dict:
        """Cancel one queued or running run without affecting a newer turn."""

        worker = self.require_worker(worker_id)
        run = self.store.get_run(run_id)
        if not run or str(run.get("worker_id") or "") != str(worker_id):
            return worker
        state = str(run.get("state") or "")
        if state in {"completed", "failed", "cancelled", "interrupted"}:
            return worker

        cancelled = None
        released_running_capacity = state == "running"
        if state == "queued":
            cancelled = self.store.finalize_run_if_state(
                run_id,
                expected_state="queued",
                state="cancelled",
                error_text="Cancelled by provider client",
            )
            if not cancelled:
                run = self.store.get_run(run_id) or run
                state = str(run.get("state") or "")

        if state == "running":
            active_run = self.store.get_active_run(worker_id)
            if not active_run or str(active_run.get("run_id") or "") != str(run_id):
                return worker
            # Stop the processor generation before interrupting the host process. A late
            # runtime return can then never overwrite the durable cancellation with completion.
            self._invalidate_worker_processor(worker_id)
            try:
                try:
                    info = self.runtime.interrupt_worker(worker, run_id=run_id)
                except TypeError as exc:
                    if "run_id" not in str(exc):
                        raise
                    info = self.runtime.interrupt_worker(worker)
                worker = self._apply_runtime_info(
                    worker_id,
                    info,
                    state="ready",
                    last_error="",
                ) or worker
            finally:
                cancelled = self.store.finalize_run_if_state(
                    run_id,
                    expected_state="running",
                    state="cancelled",
                    output_text="",
                    error_text="Cancelled by provider client",
                )

        if cancelled:
            self.store.finalize_schedule_for_run(
                run_id,
                state="cancelled",
                last_error="Cancelled by provider client",
            )
            cancelled_run = {**run, **cancelled, "state": "cancelled"}
            self.store.add_event(
                worker["project_id"],
                worker_id,
                run_id,
                "run.cancelled",
                "Run cancelled by provider client",
            )
            self._emit_callback(
                worker,
                "run.cancelled",
                run=cancelled_run,
                message="Run cancelled by provider client",
            )
            if released_running_capacity:
                self._wake_host_capacity_waiters(self.store.get_worker(worker_id) or worker)
        if self.store.has_queued_runs(worker_id):
            self._ensure_worker_processor(worker_id)
        return self.store.get_worker(worker_id) or worker

    def resume_worker(self, worker_id: str) -> dict:
        worker = self.require_worker(worker_id)
        self._ensure_execution_allowed(worker)
        worker = self._refresh_worker_model_for_profile(worker)
        updated = self._start_worker_again(worker, event_type="worker.resumed", message="Worker resumed")
        active_run = self.store.get_active_run(worker_id)
        if active_run:
            refreshed = self.store.update_worker_state(worker_id, "running", last_error="")
            return refreshed or updated
        else:
            self._ensure_worker_processor(worker_id)
        return updated

    def terminate_worker(self, worker_id: str, *, _reclaim_existing: bool = False) -> dict:
        self._invalidate_worker_processor(worker_id)
        observed = self.require_worker(worker_id)
        if (
            not _reclaim_existing
            and str(observed.get("state") or "") in {"terminating", "terminated"}
        ):
            return observed
        with self._runtime_start_lock(worker_id):
            return self._terminate_worker_with_start_fence(
                worker_id,
                reclaim_existing=_reclaim_existing,
            )

    def _terminate_worker_with_start_fence(
        self,
        worker_id: str,
        *,
        reclaim_existing: bool,
    ) -> dict:
        worker = self.require_worker(worker_id)
        claimed_worker, owns_termination = self.store.begin_worker_termination(worker_id)
        worker = claimed_worker or worker
        if not owns_termination and not (
            reclaim_existing and str(worker.get("state") or "") == "terminating"
        ):
            return worker
        active_run = self.store.get_active_run(worker_id)
        runtime_worker = {
            **worker,
            "_active_run_id": str((active_run or {}).get("run_id") or ""),
        }
        self.store.cancel_pending_runs(worker_id, error_text="Worker terminated by operator", state="cancelled")
        try:
            info = self.runtime.terminate_worker(runtime_worker)
            if info.pid:
                raise RuntimeError(f"Worker compute is still active after termination (pid={info.pid})")
        except Exception as exc:
            message = public_callback_message_text(str(exc)) or "Worker compute termination failed"
            self.store.fail_worker_termination(worker_id, message)
            self.store.add_event(
                worker["project_id"],
                worker_id,
                None,
                "worker.termination_failed",
                message,
            )
            raise
        try:
            self._deactivate_delegated_schedules_for_closed_worker(worker)
        except Exception as exc:
            message = public_callback_message_text(str(exc)) or "Delegated schedule cleanup failed"
            self.store.fail_worker_termination(worker_id, message)
            self.store.add_event(
                worker["project_id"],
                worker_id,
                None,
                "worker.termination_failed",
                message,
            )
            raise
        updated = self.store.complete_worker_termination(
            worker_id,
            runtime=info.runtime,
            model=info.model,
            gateway_url=info.gateway_url,
            gateway_port=info.gateway_port,
            gateway_token=info.gateway_token,
            session_key=info.session_key,
            state_dir=info.state_dir,
            workspace_dir=info.workspace_dir,
            pid=info.pid,
            takeover_url=f"/ui/workers/{worker_id}",
            control_url=f"/ui/workers/{worker_id}",
            compute_released_at=utc_now(),
        )
        if updated and str(updated.get("state") or "") == "termination_failed":
            raise RuntimeErrorBase("Workspace close needs attention before cleanup can complete")
        self._wake_host_capacity_waiters(updated or worker)
        self.store.add_event(worker["project_id"], worker_id, None, "worker.terminated", "Worker terminated")
        revoke_signed_link_refs_for_worker(worker_id)
        self._emit_callback(worker, "worker.terminated", message="Worker terminated")
        return updated or worker

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

    def _reconcile_worker_row(self, worker: dict) -> None:
        if worker["state"] == "terminating":
            self.terminate_worker(str(worker["worker_id"]), _reclaim_existing=True)
            return
        if worker["state"] == "termination_failed":
            self.terminate_worker(str(worker["worker_id"]))
            return
        if worker["state"] in {"terminated", "failed"}:
            self._reconcile_terminated_worker_compute(worker)
            return
        active_run = self.store.get_active_run(worker["worker_id"])
        if active_run:
            recovered = self._collect_completed_run(worker, active_run)
            if recovered:
                self._apply_recovered_run(worker, active_run, recovered)
                return
        if not active_run and self.store.has_queued_runs(worker["worker_id"]):
            if worker["state"] == "paused":
                return
            # Queue processors are process-local, but queued work is durable. Restore the
            # starting state and processor after a service restart without claiming a second
            # run or synchronously preparing compute in an HTTP request.
            self.store.update_worker_state(worker["worker_id"], "starting", last_error="")
            self._ensure_worker_processor(worker["worker_id"])
            return
        if worker["state"] == "paused":
            if active_run:
                orphaned_run = self.store.finalize_run_if_state(
                    active_run["run_id"],
                    "running",
                    "interrupted",
                    error_text="Worker was paused during reconcile",
                )
                if orphaned_run:
                    self.store.add_event(
                        worker["project_id"],
                        worker["worker_id"],
                        active_run["run_id"],
                        "run.orphaned",
                        "Active run interrupted because the worker was paused during reconcile",
                    )
                    self._emit_callback(
                        worker,
                        "run.interrupted",
                        run=orphaned_run,
                        message="Worker was paused during reconcile",
                    )
            return
        info = self.runtime.reconcile_worker(worker)
        state = worker["state"]
        if state in {"running", "ready", "starting"}:
            process_per_run_host = str(worker.get("execution_mode") or "") == "host"
            state = "ready" if info.pid or process_per_run_host else "paused"
        if not info.pid:
            if active_run:
                orphaned_run = self.store.finalize_run_if_state(
                    active_run["run_id"],
                    "running",
                    "interrupted",
                    error_text="Worker process was not running during reconcile",
                )
                if orphaned_run:
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
        self._apply_runtime_info(worker["worker_id"], info, state=state, last_error=worker.get("last_error") or "")
        if state not in {"paused", "terminated", "failed"}:
            if active_run and info.pid:
                # Queue processors are process-local. Recreate a monitor after service restart
                # so any surviving host-native process can be finalized exactly once from its
                # durable transcript instead of occupying capacity forever.
                self._ensure_worker_processor(worker["worker_id"])
            elif self.store.has_queued_runs(worker["worker_id"]):
                self._ensure_worker_processor(worker["worker_id"])

    def require_project(self, project_id: str) -> dict:
        project = self.store.get_project(project_id)
        if not project:
            raise KeyError("Project not found")
        return project

    def require_worker(self, worker_id: str) -> dict:
        worker = self.store.get_worker(worker_id)
        if not worker:
            raise KeyError("Worker not found")
        if self.store.workspace_gc_claim_active(worker_id):
            raise RuntimeErrorBase("Workspace is being garbage-collected")
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

    def _run_usage(self, worker: dict, run_id: str) -> dict[str, int]:
        reader = getattr(self.runtime, "run_usage", None)
        if not callable(reader):
            return {}
        try:
            value = reader(worker, run_id)
        except (OSError, TypeError, ValueError):
            return {}
        return dict(value) if isinstance(value, dict) else {}

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
                "failure_user_message",
                "failure_recommended_recovery",
                "failure_diagnostic_summary",
            )
            if key in recovered
        }
        usage = recovered.get("usage") if isinstance(recovered.get("usage"), dict) else {}
        if state == "completed":
            finalized_run = self.store.finalize_run_if_state(
                run["run_id"],
                "running",
                "completed",
                output_text=output_text,
                usage=usage,
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
            self._wake_host_capacity_waiters(refreshed_worker)
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
                    usage=usage,
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
                self._wake_host_capacity_waiters(refreshed_worker)
                return self.store.get_worker(worker_id)
            finalized_run = self.store.finalize_run_if_state(
                run["run_id"],
                "running",
                "failed",
                error_text=error_text,
                usage=usage,
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
            self._wake_host_capacity_waiters(refreshed_worker)
        return self.store.get_worker(worker_id)

    def heal_worker(self, worker_id: str) -> dict | None:
        worker = self.store.get_worker(worker_id)
        if (
            not worker
            or worker.get("state") == "paused"
            or str(worker.get("state") or "") in CLOSED_WORKER_STATES
        ):
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
        if (
            refreshed
            and refreshed["state"] != "paused"
            and str(refreshed["state"] or "") not in CLOSED_WORKER_STATES
            and self.store.has_queued_runs(worker_id)
        ):
            self._ensure_worker_processor(worker_id)
        return refreshed

    def _start_worker_again(self, worker: dict, event_type: str, message: str) -> dict:
        starting = self.store.update_worker_unless_gc_claimed(worker["worker_id"], state="starting")
        if starting is None:
            current = self.store.get_worker(worker["worker_id"])
            if current and str(current.get("state") or "") in CLOSED_WORKER_STATES:
                raise ControlPlaneConflict(
                    "Workspace is closed; create a new workspace for new work"
                )
            raise RuntimeErrorBase("Workspace is being garbage-collected")
        worker = starting
        try:
            info = self._ensure_worker_ready_with_lifecycle_fence(worker)
        except Exception as exc:
            updated = self.store.update_worker(worker["worker_id"], state="failed", last_error=str(exc))
            if updated and str(updated.get("state") or "") in CLOSED_WORKER_STATES:
                self._reject_closed_after_runtime_activity(
                    worker["worker_id"],
                    fallback_worker=updated,
                    context="workspace readiness",
                )
            self.store.add_event(worker["project_id"], worker["worker_id"], None, "worker.failed", str(exc))
            return updated or worker
        return self._finalize_worker_ready_after_start(
            worker,
            info,
            event_type=event_type,
            message=message,
            context="workspace readiness",
        )

    def _apply_runtime_info(
        self,
        worker_id: str,
        info: RuntimeInfo,
        state: str,
        last_error: str,
        compute_released_at: str | None | object = _UNSET,
    ) -> dict | None:
        fields = {
            "runtime": info.runtime,
            "model": info.model,
            "state": state,
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
        if compute_released_at is not _UNSET:
            fields["compute_released_at"] = compute_released_at
        return self.store.update_worker(worker_id, **fields)

    def _bootstrap_bundle_for(self, worker: dict) -> dict | None:
        raw = str(worker.get("bootstrap_bundle_json") or "").strip()
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    def _copy_workspace_contents(self, source_worker: dict, target_worker: dict) -> dict[str, object]:
        source_root_raw = str(source_worker.get("workspace_dir") or "").strip()
        target_root_raw = str(target_worker.get("workspace_dir") or "").strip()
        if not source_root_raw or not target_root_raw:
            return {"source_state": "missing", "copied_files": 0, "skipped_items": 0}
        source_root = Path(source_root_raw)
        target_root = Path(target_root_raw)
        if target_root.is_symlink():
            raise ValueError("workspace duplicate target must not be a symlink")
        if source_root.exists():
            resolved_source = source_root.resolve(strict=True)
            resolved_target = target_root.resolve(strict=False)
            if resolved_target == resolved_source or resolved_source in resolved_target.parents:
                raise ValueError("workspace duplicate target must be separate from the source")
        files, skipped_items, source_state = _workspace_copy_plan(source_root)
        target_root.mkdir(parents=True, exist_ok=True)
        copied_files = 0
        copied_bytes = 0
        copied_targets: list[Path] = []
        max_bytes = _bounded_int_env(
            "GLASSHIVE_DUPLICATE_MAX_BYTES",
            512 * 1024 * 1024,
            min_value=1024,
            max_value=20 * 1024 * 1024 * 1024,
        )
        deadline = time.monotonic() + _bounded_float_env(
            "GLASSHIVE_DUPLICATE_TIMEOUT_SECONDS",
            30.0,
            min_value=1.0,
            max_value=300.0,
        )
        try:
            for source, relative in files:
                if time.monotonic() > deadline:
                    raise ValueError("workspace duplicate copy exceeded its time limit")
                target = target_root / relative
                copied_bytes += _copy_regular_workspace_file(
                    source,
                    target,
                    source_root,
                    max_bytes=max_bytes - copied_bytes,
                    deadline=deadline,
                )
                copied_targets.append(target)
                copied_files += 1
        except Exception:
            for copied_target in reversed(copied_targets):
                try:
                    copied_target.unlink()
                except FileNotFoundError:
                    pass
                parent = copied_target.parent
                while parent != target_root:
                    try:
                        parent.rmdir()
                    except OSError:
                        break
                    parent = parent.parent
            raise
        return {
            "source_state": source_state,
            "copied_files": copied_files,
            "skipped_items": skipped_items,
        }

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
        generation: int | None = None
        with self._processors_lock:
            if worker_id in self._active_processors:
                return
            generation = self._processor_generations.get(worker_id, 0) + 1
            self._processor_generations[worker_id] = generation
            self._active_processors.add(worker_id)
        worker = self.store.get_worker(worker_id) or {}
        bundle = self._bootstrap_bundle_for(worker) or {}
        executor = (
            self.conversation_executor
            if str(bundle.get("run_mode") or "mission").strip().lower() == "conversation"
            else self.executor
        )
        executor.submit(self._process_worker_queue, worker_id, generation)

    def _wake_host_capacity_waiters(self, released_worker: dict) -> None:
        """Wake one free host CLI/auth lane without disturbing unrelated queues."""

        if str(released_worker.get("execution_mode") or "docker").strip().lower() != "host":
            return
        # Capacity checks intentionally allow the currently registered worker to
        # re-enter its own lane. Probe as a distinct waiter so a stale/held slot is
        # not mistaken for released capacity.
        capacity_probe = {
            **released_worker,
            "worker_id": f"{released_worker.get('worker_id') or 'host'}:capacity-probe",
        }
        try:
            if self._runtime_capacity_error(capacity_probe) is not None:
                return
        except Exception as exc:
            logger.warning(
                "Failed to verify released host capacity for worker %s: %s",
                released_worker.get("worker_id") or "",
                exc,
            )
            return
        bundle = self._bootstrap_bundle_for(released_worker) or {}
        run_mode = (
            "conversation"
            if str(bundle.get("run_mode") or "").strip().lower() == "conversation"
            else "mission"
        )
        try:
            waiting_worker_ids = self.store.release_host_capacity_waiters(
                profile=str(released_worker.get("profile") or "").strip(),
                execution_mode="host",
                run_mode=run_mode,
            )
        except Exception as exc:
            logger.warning(
                "Failed to release queued host-capacity waiters for worker %s: %s",
                released_worker.get("worker_id") or "",
                exc,
            )
            return
        for waiting_worker_id in waiting_worker_ids:
            self._ensure_worker_processor(waiting_worker_id)

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

    def _process_worker_queue(self, worker_id: str, generation: int) -> None:
        try:
            while True:
                if self._shutdown_event.is_set():
                    return
                if not self._processor_is_current(worker_id, generation):
                    return
                worker = self.store.get_worker(worker_id)
                if not worker or worker["state"] in {"paused", "terminating", "termination_failed", "terminated"}:
                    return

                active_run = self.store.get_active_run(worker_id)
                if active_run:
                    recovered = self._collect_completed_run(worker, active_run)
                    if recovered:
                        self._apply_recovered_run(worker, active_run, recovered)
                        continue
                    info = self.runtime.reconcile_worker(worker)
                    if info.pid:
                        if self._shutdown_event.wait(0.2):
                            return
                        continue
                    orphaned_run = self.store.finalize_run_if_state(
                        active_run["run_id"],
                        "running",
                        "interrupted",
                        error_text="Worker process ended before restart recovery produced a complete result",
                    )
                    if orphaned_run:
                        self.store.update_worker_state(worker_id, "ready", last_error="")
                        self.store.add_event(
                            worker["project_id"],
                            worker_id,
                            active_run["run_id"],
                            "run.orphaned",
                            "Active run ended without a complete recoverable result",
                        )
                        self._emit_callback(
                            worker,
                            "run.interrupted",
                            run=orphaned_run,
                            message="Worker process ended before restart recovery produced a complete result",
                        )
                        self._wake_host_capacity_waiters(self.store.get_worker(worker_id) or worker)
                    continue

                queued_run = self.store.peek_next_queued_run(worker_id)
                if queued_run:
                    capacity_error = self._runtime_capacity_error(worker)
                    if capacity_error:
                        self._requeue_retryable_run(worker, queued_run, capacity_error)
                        return

                run = self.store.claim_next_queued_run(
                    worker_id,
                    require_schedule_principal_authority=multi_user_security_enabled(),
                )
                if not run:
                    self._schedule_worker_retry_after(worker_id, self.store.next_retry_after_for_worker(worker_id))
                    current = self.store.get_worker(worker_id)
                    if (
                        self._processor_is_current(worker_id, generation)
                        and current
                        and current["state"] not in {"paused", "failed"}
                        and str(current["state"] or "") not in CLOSED_WORKER_STATES
                        and not self.store.get_active_run(worker_id)
                    ):
                        self.store.update_worker_state(worker_id, "ready", last_error="")
                    return

                current = self.store.get_worker(worker_id)
                if (
                    not self._processor_is_current(worker_id, generation)
                    or not current
                    or str(current.get("state") or "") in CLOSED_WORKER_STATES
                ):
                    if current and str(current.get("state") or "") in CLOSED_WORKER_STATES:
                        self.store.finalize_run_if_state(
                            run["run_id"],
                            "running",
                            "cancelled",
                            error_text="workspace_closed",
                        )
                        self.store.finalize_schedule_for_run(
                            run["run_id"],
                            state="cancelled",
                            last_error="workspace_closed",
                        )
                    return

                worker = current
                capacity_error = self._runtime_capacity_error(worker)
                if capacity_error:
                    self._requeue_retryable_run(worker, run, capacity_error)
                    return
                if multi_user_security_enabled():
                    try:
                        self.store.require_schedule_principal_authority_for_run(run["run_id"])
                    except SchedulePrincipalAuthorityStoreError:
                        cancelled = self.store.finalize_run_if_state(
                            run["run_id"],
                            "running",
                            "cancelled",
                            error_text="principal_disabled",
                        )
                        if cancelled:
                            self.store.finalize_schedule_for_run(
                                run["run_id"],
                                state="cancelled",
                                last_error="principal_disabled",
                            )
                        continue
                worker = self._refresh_runtime_info(worker_id, state="running", last_error="") or self.store.get_worker(worker_id) or worker
                started_notified = Event()

                def notify_started() -> None:
                    if started_notified.is_set():
                        return
                    started_notified.set()
                    self.store.add_event(
                        worker["project_id"],
                        worker_id,
                        run["run_id"],
                        "run.started",
                        run["instruction"],
                    )
                    self._emit_callback(
                        worker,
                        "run.started",
                        run=run,
                        message=run["instruction"],
                    )

                def persist_runtime_info(info: RuntimeInfo) -> None:
                    self._apply_runtime_info(
                        worker_id,
                        info,
                        state="running",
                        last_error="",
                    )

                runtime_worker = {
                    **self._runtime_worker_for_run(worker, run),
                    "_runtime_start_guard": lambda: self._runtime_execution_start_guard(
                        worker_id,
                        generation,
                        run["run_id"],
                    ),
                    "_runtime_started_callback": notify_started,
                    "_runtime_info_callback": persist_runtime_info,
                }

                try:
                    try:
                        output = self.runtime.run_task(
                            runtime_worker,
                            run["instruction"],
                            run_id=run["run_id"],
                        )
                    except TypeError as exc:
                        if "run_id" not in str(exc):
                            raise
                        output = self.runtime.run_task(
                            runtime_worker,
                            run["instruction"],
                        )
                    notify_started()
                except WorkerPausedError as exc:
                    if not self._processor_is_current(worker_id, generation):
                        return
                    self.store.finalize_run(run["run_id"], state="paused", error_text=str(exc))
                    self.store.finalize_schedule_for_run(run["run_id"], state="failed", last_error=str(exc))
                    self.store.update_worker_state(worker_id, "paused", last_error="")
                    self.store.add_event(worker["project_id"], worker_id, run["run_id"], "run.paused", str(exc))
                    self._emit_callback(worker, "run.paused", run={**run, "state": "paused", "error_text": str(exc)}, message=str(exc))
                    self._wake_host_capacity_waiters(self.store.get_worker(worker_id) or worker)
                    return
                except WorkerInterruptedError as exc:
                    if not self._processor_is_current(worker_id, generation):
                        return
                    recovered = self._collect_completed_run(worker, run)
                    if recovered:
                        self._apply_recovered_run(worker, run, recovered)
                        continue
                    self.store.finalize_run(run["run_id"], state="interrupted", error_text=str(exc))
                    self.store.finalize_schedule_for_run(run["run_id"], state="failed", last_error=str(exc))
                    self.store.update_worker_state(worker_id, "ready", last_error="")
                    self.store.add_event(worker["project_id"], worker_id, run["run_id"], "run.interrupted", str(exc))
                    self._emit_callback(worker, "run.interrupted", run={**run, "state": "interrupted", "error_text": str(exc)}, message=str(exc))
                    self._wake_host_capacity_waiters(self.store.get_worker(worker_id) or worker)
                    continue
                except WorkerTerminatedError as exc:
                    if not self._processor_is_current(worker_id, generation):
                        return
                    recovered = self._collect_completed_run(worker, run)
                    if recovered:
                        self._apply_recovered_run(worker, run, recovered)
                        continue
                    self.store.finalize_run(run["run_id"], state="cancelled", error_text=str(exc))
                    self.store.finalize_schedule_for_run(run["run_id"], state="cancelled", last_error=str(exc))
                    self.store.update_worker_state(worker_id, "terminated", last_error=str(exc))
                    self.store.add_event(worker["project_id"], worker_id, run["run_id"], "run.cancelled", str(exc))
                    self._emit_callback(worker, "run.cancelled", run={**run, "state": "cancelled", "error_text": str(exc)}, message=str(exc))
                    self._wake_host_capacity_waiters(self.store.get_worker(worker_id) or worker)
                    return
                except RuntimeErrorBase as exc:
                    if not self._processor_is_current(worker_id, generation):
                        return
                    current_worker = self.store.get_worker(worker_id) or worker
                    worker_state = current_worker["state"]
                    final_state = "failed"
                    if worker_state == "paused":
                        final_state = "interrupted"
                    elif worker_state in CLOSED_WORKER_STATES:
                        final_state = "cancelled"
                    refreshed_worker = (
                        self._refresh_runtime_info(
                            worker_id,
                            state=(
                                worker_state
                                if worker_state == "paused" or worker_state in CLOSED_WORKER_STATES
                                else "ready"
                            ),
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
                        and str(failure_fields.get("failure_class") or "") == "host_worker_busy"
                    ):
                        self._requeue_retryable_run(refreshed_worker, run, exc, failure_fields=failure_fields)
                        return
                    self.store.finalize_run(
                        run["run_id"],
                        state=final_state,
                        error_text=str(exc),
                        usage=self._run_usage(refreshed_worker, run["run_id"]),
                        **failure_fields,
                    )
                    self.store.finalize_schedule_for_run(
                        run["run_id"],
                        state="cancelled" if final_state == "cancelled" else "failed",
                        last_error=str(exc),
                    )
                    self.store.update_worker_state(
                        worker_id,
                        worker_state
                        if worker_state == "paused" or worker_state in CLOSED_WORKER_STATES
                        else "ready",
                        last_error=str(exc),
                    )
                    self.store.add_event(worker["project_id"], worker_id, run["run_id"], f"run.{final_state}", str(exc))
                    failed_run = {**run, "state": final_state, "error_text": str(exc), **failure_fields}
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
                    self._wake_host_capacity_waiters(callback_worker)
                    if worker_state == "paused" or worker_state in CLOSED_WORKER_STATES:
                        return
                    continue
                except Exception as exc:
                    if not self._processor_is_current(worker_id, generation):
                        return
                    failure_fields = classify_runtime_error(
                        exc,
                        runtime_name=str(worker.get("profile") or worker.get("runtime") or "worker"),
                    ).as_store_fields()
                    self.store.finalize_run(
                        run["run_id"],
                        state="failed",
                        error_text=str(exc),
                        usage=self._run_usage(worker, run["run_id"]),
                        **failure_fields,
                    )
                    self.store.finalize_schedule_for_run(run["run_id"], state="failed", last_error=str(exc))
                    self.store.update_worker_state(worker_id, "ready", last_error=str(exc))
                    self.store.add_event(worker["project_id"], worker_id, run["run_id"], "run.failed", str(exc))
                    failed_run = {**run, "state": "failed", "error_text": str(exc), **failure_fields}
                    failure_message = runtime_failure_callback_message(failure_fields, str(exc))
                    self._emit_callback(worker, "run.failed", run=failed_run, message=failure_message)
                    self._wake_host_capacity_waiters(self.store.get_worker(worker_id) or worker)
                    continue

                if not self._processor_is_current(worker_id, generation):
                    return
                finalized_run = self.store.finalize_run(
                    run["run_id"],
                    state="completed",
                    output_text=output,
                    usage=self._run_usage(worker, run["run_id"]),
                )
                self._wake_host_capacity_waiters(worker)
                self.store.finalize_schedule_for_run(run["run_id"], state="completed")
                self.store.update_worker(worker_id, state="ready", last_error="", last_run_id=run["run_id"])
                message = terminal_callback_message(output)
                full_message = terminal_callback_full_message(output)
                self.store.add_event(worker["project_id"], worker_id, run["run_id"], "run.completed", message[:TERMINAL_CALLBACK_MESSAGE_LIMIT] or "Run completed")
                completed_run = {**run, **(finalized_run or {}), "state": "completed", "output_text": output}
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
        finally:
            if self._release_processor(worker_id, generation):
                pending = self.store.get_worker(worker_id)
                if pending and pending["state"] not in {"paused", "terminating", "termination_failed", "terminated"}:
                    if self.store.peek_next_queued_run(worker_id):
                        self._ensure_worker_processor(worker_id)
