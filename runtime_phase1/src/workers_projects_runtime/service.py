from __future__ import annotations

import json
import hashlib
import hmac
import logging
import os
import re
import shutil
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Lock, Thread, Timer
from urllib.parse import quote, urlencode

import httpx

from .deliverables import deliverable_payload, is_user_deliverable_relative_path
from .failure_classification import classify_runtime_error
from .models import utc_now
from .openclaw_runtime import (
    RuntimeErrorBase,
    RuntimeInfo,
    WorkerInterruptedError,
    WorkerPausedError,
    WorkerRuntime,
    WorkerTerminatedError,
)
from .operator_urls import surface_aware_watch_url
from .runtime_env import load_viventium_runtime_env
from .runtime_identity import derive_legacy_backend_label
from .signed_links import (
    append_signed_query,
    create_signed_link_ref,
    revoke_signed_link_refs_for_worker,
    sign_link_params,
    signed_link_ref_url,
    sign_link_token,
)
from .store import Store


logger = logging.getLogger(__name__)
TERMINAL_CALLBACK_MESSAGE_LIMIT = 4000
FINAL_REPORT_PATTERN = re.compile(r"(?m)^[ \t]*FINAL REPORT:\s*")
VIVENTIUM_CALLBACK_PATH = "/api/viventium/glasshive/callback"
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


class WorkersProjectsService:
    def __init__(self, store: Store, runtime: WorkerRuntime, max_workers: int = 8) -> None:
        self.store = store
        self.runtime = runtime
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="wpr-runner")
        self._shutdown_event = Event()
        self._processors_lock = Lock()
        self._active_processors: set[str] = set()
        self._processor_generations: dict[str, int] = {}
        self._worker_create_lock = Lock()
        self._deliverable_promotions_lock = Lock()
        self._deliverable_promotions: set[str] = set()
        self.reconcile_all_workers()
        self.executor.submit(self._replay_pending_callbacks)
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

    def shutdown(self) -> None:
        self._shutdown_event.set()
        for thread in (self._callback_retry_thread, self._idle_reaper_thread, self._scheduler_thread):
            if thread and thread.is_alive():
                thread.join(timeout=2)
        self.executor.shutdown(wait=True, cancel_futures=False)

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

        retry_attempts = _bounded_int_env("GLASSHIVE_CALLBACK_RETRY_ATTEMPTS", 3, min_value=1, max_value=25)
        retry_base_delay_s = _bounded_float_env(
            "GLASSHIVE_CALLBACK_RETRY_BASE_DELAY_S",
            0.5,
            min_value=0.0,
            max_value=60.0,
        )
        last_exc: Exception | None = None
        attempts = 0
        payload_json = json.dumps(payload, ensure_ascii=False)
        for attempt in range(retry_attempts):
            attempts += 1
            payload["callback_ts"] = int(time.time())
            encoded = self._encode_callback_payload(payload)
            headers = self._callback_headers(callbacks, payload, encoded)
            payload_json = json.dumps(payload, ensure_ascii=False)
            try:
                response = httpx.post(url, content=encoded, headers=headers, timeout=5.0)
                if response.status_code == 409:
                    self.store.mark_callback_delivered(payload["callback_id"], attempts=attempts, payload_json=payload_json)
                    return
                response.raise_for_status()
                self.store.mark_callback_delivered(payload["callback_id"], attempts=attempts, payload_json=payload_json)
                return
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                status_code = exc.response.status_code if exc.response is not None else 0
                if status_code in CALLBACK_DEAD_LETTER_IMMEDIATE_STATUS_CODES:
                    self._dead_letter_callback(
                        worker,
                        record,
                        callback_id=payload["callback_id"],
                        attempts=attempts,
                        payload_json=payload_json,
                        reason=f"callback endpoint returned terminal HTTP {status_code}",
                    )
                    return
                if 400 <= status_code < 500 and status_code not in CALLBACK_RETRYABLE_STATUS_CODES:
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
            payload_json=payload_json,
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
            self._replay_pending_callbacks()

    def _ensure_execution_allowed(self, worker_or_mode: dict | str) -> None:
        execution_mode = (
            str(worker_or_mode.get("execution_mode") or "docker")
            if isinstance(worker_or_mode, dict)
            else str(worker_or_mode or "docker")
        )
        if execution_mode == "host" and not host_workers_enabled():
            raise HostWorkersDisabledError(
                "GlassHive host-native workers are disabled by Viventium config"
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
        try:
            resolved_model = self._resolve_worker_model(
                profile,
                str(worker.get("execution_mode") or "docker"),
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
            open_url = self._signed_artifact_open_url(worker, workspace_path)
            if open_url:
                links.append(f"File: [Open GlassHive file]({open_url})")
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
        callbacks = self._callback_config_for(worker)
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

    def _lifecycle_reaper_enabled(self) -> bool:
        return (
            self._idle_terminate_after_s() > 0
            or self._paused_terminate_after_s() > 0
            or self._max_run_duration_s() > 0
        )

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
            if not worker_id or worker.get("state") in {"terminated", "paused", "running", "starting"}:
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
            self.reap_idle_workers_once()
            self.reap_paused_workers_once()
            self.reap_expired_runs_once()

    def _scheduler_interval_s(self) -> int:
        return _bounded_int_env("GLASSHIVE_SCHEDULER_INTERVAL_S", 5, min_value=1, max_value=3600)

    def _scheduler_loop(self) -> None:
        interval = self._scheduler_interval_s()
        while not self._shutdown_event.wait(interval):
            self.process_due_schedules_once()

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
        max_attempts = self._capacity_retry_max_attempts()
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
    ) -> dict:
        return self.store.create_project(owner_id, title, goal, default_worker_profile, tenant_id=tenant_id)

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
        self.store.update_worker_state(worker["worker_id"], "starting")
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
        if worker["state"] == "paused":
            self.store.update_worker_state(worker_id, "starting", last_error="")
            self.store.add_event(
                worker["project_id"],
                worker_id,
                None,
                "worker.resumed",
                "Worker resume queued for the next run",
            )
            worker = self.store.get_worker(worker_id) or worker
        run = self.store.create_run(worker_id, worker["project_id"], instruction, state="queued")
        self.store.add_event(worker["project_id"], worker_id, run["run_id"], event_type, instruction)
        self._emit_callback(worker, event_type, run=run, message=instruction)
        self._ensure_worker_processor(worker_id)
        return run

    def record_launch_failed(self, worker_id: str, reason: str) -> dict:
        worker = self.require_worker(worker_id)
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
        if worker.get("state") != "running":
            self.store.update_worker_state(worker_id, "starting", last_error="")
        try:
            launched = self.runtime.desktop_action(worker, action, url=url, run_id=run_id)
        except TypeError as exc:
            if "run_id" not in str(exc):
                raise
            launched = self.runtime.desktop_action(worker, action, url=url)
        except Exception as exc:
            self.store.update_worker(worker_id, state=str(worker.get("state") or "failed"), last_error=str(exc))
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

    def pause_worker(self, worker_id: str) -> dict:
        worker = self.require_worker(worker_id)
        info = self.runtime.pause_worker(worker)
        updated = self._apply_runtime_info(worker_id, info, state="paused", last_error=worker.get("last_error") or "")
        active_run = self.store.get_active_run(worker_id)
        self.store.add_event(worker["project_id"], worker_id, active_run["run_id"] if active_run else None, "worker.paused", "Worker paused")
        self._emit_callback(worker, "worker.paused", run=active_run, message="Worker paused")
        return updated or worker

    def interrupt_worker(self, worker_id: str) -> dict:
        worker = self.require_worker(worker_id)
        active_run = self.store.get_active_run(worker_id)
        try:
            info = self.runtime.interrupt_worker(worker, run_id=active_run["run_id"] if active_run else None)
        except TypeError as exc:
            if "run_id" not in str(exc):
                raise
            info = self.runtime.interrupt_worker(worker)
        updated = self._apply_runtime_info(worker_id, info, state="ready", last_error="")
        if active_run:
            self.store.finalize_run(
                active_run["run_id"],
                state="interrupted",
                output_text=active_run.get("output_text", ""),
                error_text="Interrupted by operator",
            )
        self.store.add_event(worker["project_id"], worker_id, active_run["run_id"] if active_run else None, "worker.interrupted", "Worker interrupted")
        self._emit_callback(worker, "worker.interrupted", run=active_run, message="Worker interrupted")
        return updated or worker

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

    def terminate_worker(self, worker_id: str) -> dict:
        worker = self.require_worker(worker_id)
        info = self.runtime.terminate_worker(worker)
        self.store.cancel_pending_runs(worker_id, error_text="Worker terminated by operator", state="cancelled")
        updated = self._apply_runtime_info(
            worker_id,
            info,
            state="terminated",
            last_error="",
            compute_released_at=utc_now(),
        )
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
        if worker["state"] == "terminated":
            return
        if worker["state"] == "paused":
            active_run = self.store.get_active_run(worker["worker_id"])
            if active_run:
                recovered = self._collect_completed_run(worker, active_run)
                if recovered:
                    self._apply_recovered_run(worker, active_run, recovered)
                else:
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
            state = "ready" if info.pid else "paused"
        if not info.pid:
            active_run = self.store.get_active_run(worker["worker_id"])
            if active_run:
                recovered = self._collect_completed_run(worker, active_run)
                if recovered:
                    self._apply_recovered_run(worker, active_run, recovered)
                else:
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
            return self.runtime.collect_completed_run(worker, run_id=run["run_id"])
        except TypeError as exc:
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
        first_part = workspace_path.parts[0].lower()
        if first_part != "artifacts" and workspace_path.name.lower() != "index.html":
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
        started_at = str(run.get("started_at") or run.get("queued_at") or "").strip()
        if not started_at:
            return False
        try:
            started = datetime.fromisoformat(started_at.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return False
        return artifact_path.stat().st_mtime >= started - 5

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
        if state == "completed":
            finalized_run = self.store.finalize_run_if_state(
                run["run_id"],
                "running",
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
        self.store.update_worker_state(worker["worker_id"], "starting")
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
        generation: int | None = None
        with self._processors_lock:
            if worker_id in self._active_processors:
                return
            generation = self._processor_generations.get(worker_id, 0) + 1
            self._processor_generations[worker_id] = generation
            self._active_processors.add(worker_id)
        self.executor.submit(self._process_worker_queue, worker_id, generation)

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
                if not self._processor_is_current(worker_id, generation):
                    return
                worker = self.store.get_worker(worker_id)
                if not worker or worker["state"] in {"paused", "terminated"}:
                    return

                queued_run = self.store.peek_next_queued_run(worker_id)
                if queued_run:
                    capacity_error = self._runtime_capacity_error(worker)
                    if capacity_error:
                        self._requeue_retryable_run(worker, queued_run, capacity_error)
                        return

                run = self.store.claim_next_queued_run(worker_id)
                if not run:
                    self._schedule_worker_retry_after(worker_id, self.store.next_retry_after_for_worker(worker_id))
                    current = self.store.get_worker(worker_id)
                    if (
                        self._processor_is_current(worker_id, generation)
                        and current
                        and current["state"] not in {"paused", "terminated", "failed"}
                        and not self.store.get_active_run(worker_id)
                    ):
                        self.store.update_worker_state(worker_id, "ready", last_error="")
                    return

                worker = self.store.get_worker(worker_id) or worker
                capacity_error = self._runtime_capacity_error(worker)
                if capacity_error:
                    self._requeue_retryable_run(worker, run, capacity_error)
                    return
                worker = self._refresh_runtime_info(worker_id, state="running", last_error="") or self.store.get_worker(worker_id) or worker
                self.store.add_event(worker["project_id"], worker_id, run["run_id"], "run.started", run["instruction"])
                self._emit_callback(worker, "run.started", run=run, message=run["instruction"])

                try:
                    try:
                        output = self.runtime.run_task(worker, run["instruction"], run_id=run["run_id"])
                    except TypeError as exc:
                        if "run_id" not in str(exc):
                            raise
                        output = self.runtime.run_task(worker, run["instruction"])
                except WorkerPausedError as exc:
                    if not self._processor_is_current(worker_id, generation):
                        return
                    self.store.finalize_run(run["run_id"], state="paused", error_text=str(exc))
                    self.store.finalize_schedule_for_run(run["run_id"], state="failed", last_error=str(exc))
                    self.store.update_worker_state(worker_id, "paused", last_error="")
                    self.store.add_event(worker["project_id"], worker_id, run["run_id"], "run.paused", str(exc))
                    self._emit_callback(worker, "run.paused", run={**run, "state": "paused", "error_text": str(exc)}, message=str(exc))
                    return
                except WorkerInterruptedError as exc:
                    if not self._processor_is_current(worker_id, generation):
                        return
                    self.store.finalize_run(run["run_id"], state="interrupted", error_text=str(exc))
                    self.store.finalize_schedule_for_run(run["run_id"], state="failed", last_error=str(exc))
                    self.store.update_worker_state(worker_id, "ready", last_error="")
                    self.store.add_event(worker["project_id"], worker_id, run["run_id"], "run.interrupted", str(exc))
                    self._emit_callback(worker, "run.interrupted", run={**run, "state": "interrupted", "error_text": str(exc)}, message=str(exc))
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
                    if final_state == "failed":
                        recovered = self._collect_completed_run(refreshed_worker, run)
                        if recovered:
                            self._apply_recovered_run(refreshed_worker, run, recovered)
                            continue
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
                        and bool(failure_fields.get("failure_retryable"))
                        and str(failure_fields.get("failure_class") or "") == "host_worker_busy"
                    ):
                        self._requeue_retryable_run(refreshed_worker, run, exc, failure_fields=failure_fields)
                        return
                    self.store.finalize_run(
                        run["run_id"],
                        state=final_state,
                        error_text=str(exc),
                        **failure_fields,
                    )
                    self.store.finalize_schedule_for_run(
                        run["run_id"],
                        state="cancelled" if final_state == "cancelled" else "failed",
                        last_error=str(exc),
                    )
                    self.store.update_worker_state(worker_id, worker_state if worker_state in {"paused", "terminated"} else "ready", last_error=str(exc))
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
                    self.store.finalize_run(run["run_id"], state="failed", error_text=str(exc), **failure_fields)
                    self.store.finalize_schedule_for_run(run["run_id"], state="failed", last_error=str(exc))
                    self.store.update_worker_state(worker_id, "ready", last_error=str(exc))
                    self.store.add_event(worker["project_id"], worker_id, run["run_id"], "run.failed", str(exc))
                    failed_run = {**run, "state": "failed", "error_text": str(exc), **failure_fields}
                    failure_message = runtime_failure_callback_message(failure_fields, str(exc))
                    self._emit_callback(worker, "run.failed", run=failed_run, message=failure_message)
                    continue

                if not self._processor_is_current(worker_id, generation):
                    return
                self.store.finalize_run(run["run_id"], state="completed", output_text=output)
                self.store.finalize_schedule_for_run(run["run_id"], state="completed")
                self.store.update_worker(worker_id, state="ready", last_error="", last_run_id=run["run_id"])
                message = terminal_callback_message(output)
                full_message = terminal_callback_full_message(output)
                self.store.add_event(worker["project_id"], worker_id, run["run_id"], "run.completed", message[:TERMINAL_CALLBACK_MESSAGE_LIMIT] or "Run completed")
                completed_run = {**run, "state": "completed", "output_text": output}
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
                if pending and pending["state"] not in {"paused", "terminated"}:
                    queued = next((item for item in self.store.list_runs_for_worker(worker_id, limit=10) if item["state"] == "queued"), None)
                    if queued:
                        self._ensure_worker_processor(worker_id)
