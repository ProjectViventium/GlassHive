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
from pathlib import Path
from threading import Event, Lock, Thread

import httpx

from .deliverables import deliverable_payload
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
from .store import Store


logger = logging.getLogger(__name__)
TERMINAL_CALLBACK_MESSAGE_LIMIT = 2400
FINAL_REPORT_PATTERN = re.compile(r"(?m)^[ \t]*FINAL REPORT:\s*")


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


def host_workers_enabled() -> bool:
    value = os.environ.get("GLASSHIVE_HOST_WORKERS_ENABLED", "true").strip().lower()
    return value not in {"0", "false", "no", "off", "disabled"}


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

    if not has_final_report:
        paragraphs = [paragraph.strip() for paragraph in text.split("\n\n") if paragraph.strip()]
        if len(paragraphs) > 1 and len(paragraphs[-1]) <= TERMINAL_CALLBACK_MESSAGE_LIMIT:
            return paragraphs[-1]
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if len(lines) > 1 and len(lines[-1]) <= min(1000, TERMINAL_CALLBACK_MESSAGE_LIMIT):
            return lines[-1]

    if len(text) <= TERMINAL_CALLBACK_MESSAGE_LIMIT:
        return text or fallback

    if has_final_report:
        return f"{text[: TERMINAL_CALLBACK_MESSAGE_LIMIT - 3].rstrip()}..."

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if lines and len(lines[-1]) <= min(1000, TERMINAL_CALLBACK_MESSAGE_LIMIT):
        return lines[-1]

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
        self.reconcile_all_workers()
        self.executor.submit(self._replay_pending_callbacks)
        self._callback_retry_thread = Thread(
            target=self._callback_retry_loop,
            name="wpr-callback-retry",
            daemon=True,
        )
        self._callback_retry_thread.start()

    def shutdown(self) -> None:
        self._shutdown_event.set()
        self.executor.shutdown(wait=False, cancel_futures=False)

    def _callback_config_for(self, worker: dict) -> dict:
        bundle = self._bootstrap_bundle_for(worker) or {}
        callbacks = bundle.get("callbacks")
        if not isinstance(callbacks, dict) or not callbacks:
            return {}
        resolved = dict(callbacks)
        load_viventium_runtime_env()
        callback_url = os.environ.get("VIVENTIUM_GLASSHIVE_CALLBACK_URL", "").strip()
        callback_secret = os.environ.get("VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", "").strip()
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

    def _deliver_callback_record(self, worker: dict, record: dict, callbacks: dict) -> None:
        url = str(callbacks.get("events_webhook_url") or callbacks.get("url") or record.get("url") or "").strip()
        if not url:
            self.store.mark_callback_pending(
                str(record.get("callback_id") or ""),
                attempts=0,
                payload_json=str(record.get("payload_json") or "{}"),
                last_error="missing callback url",
            )
            return
        try:
            payload = json.loads(str(record.get("payload_json") or "{}"))
        except json.JSONDecodeError:
            self.store.mark_callback_pending(
                str(record.get("callback_id") or ""),
                attempts=0,
                payload_json=str(record.get("payload_json") or "{}"),
                last_error="invalid callback payload json",
            )
            return
        if not isinstance(payload, dict):
            payload = {}
        payload["callback_id"] = str(record.get("callback_id") or payload.get("callback_id") or f"cb_{uuid.uuid4().hex}")

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
                if 400 <= status_code < 500 and status_code not in {408, 409, 425, 429}:
                    break
            except Exception as exc:
                last_exc = exc
            if attempt < retry_attempts - 1 and retry_base_delay_s > 0:
                time.sleep(retry_base_delay_s * (attempt + 1))
        self.store.mark_callback_pending(
            payload["callback_id"],
            attempts=attempts,
            payload_json=payload_json,
            last_error=str(last_exc or "callback delivery failed"),
        )
        try:
            self.store.add_event(
                str(record.get("project_id") or worker.get("project_id") or ""),
                str(record.get("worker_id") or worker.get("worker_id") or ""),
                record.get("run_id"),
                "callback.failed",
                f"{record.get('event_type')}: {last_exc}",
            )
        except Exception:
            pass

    def _replay_pending_callbacks(self) -> None:
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
        operator_url = surface_aware_watch_url(
            str(worker.get("worker_id") or ""),
            str(worker.get("project_id") or ""),
            request_surface=str(callbacks.get("surface") or ""),
            watch_surface="desktop",
        )
        payload = {
            "callback_id": f"cb_{uuid.uuid4().hex}",
            "callback_ts": int(time.time()),
            "event": event_type,
            "project_id": worker.get("project_id"),
            "worker_id": worker.get("worker_id"),
            "run_id": (run or {}).get("run_id"),
            "run_state": (run or {}).get("state"),
            "message": message,
            "full_message": full_message,
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
        existing_events = self.store.list_events(worker_id)
        if any(
            event.get("event_type") == "deliverable.opened"
            and str(event.get("message") or "") == promotion_key
            for event in existing_events
        ):
            return
        if not hasattr(self.runtime, "desktop_action"):
            return
        try:
            self.desktop_action(worker_id, "browser", url=browser_url, run_id=run_id)
            self.store.add_event(project_id, worker_id, run_id, "deliverable.opened", promotion_key)
        except Exception as exc:
            logger.warning("Failed to promote GlassHive deliverable %s: %s", promotion_key, exc)

    def create_project(self, owner_id: str, title: str, goal: str, default_worker_profile: str) -> dict:
        return self.store.create_project(owner_id, title, goal, default_worker_profile)

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
    ) -> dict:
        self._ensure_execution_allowed(execution_mode)
        model = self.runtime.resolve_model(profile)
        worker = self.store.create_worker(
            project_id=project_id,
            owner_id=owner_id,
            name=name,
            role=role,
            profile=profile,
            backend=backend,
            runtime="openclaw",
            model=model,
            execution_mode=execution_mode,
            alias=alias,
            workspace_root=workspace_root,
            bootstrap_profile=bootstrap_profile,
            bootstrap_bundle=bootstrap_bundle,
        )
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
        updated = self._apply_runtime_info(worker["worker_id"], info, state="ready", last_error="")
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
    ) -> dict:
        self._ensure_execution_allowed(execution_mode)
        existing = self.store.find_worker_by_alias(project_id, owner_id, alias, execution_mode=execution_mode)
        if existing and existing.get("state") != "terminated":
            updates: dict[str, object] = {
                "name": name,
                "role": role,
                "profile": profile,
                "backend": backend,
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
            self._emit_callback(existing, "worker.resumed_by_alias", message=f"Reusing worker alias {alias}")
            return existing
        return self.create_worker(
            project_id=project_id,
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
        )

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
        duplicated = self.create_worker(
            project_id=project_id,
            owner_id=owner_id,
            name=name,
            role=role,
            profile=str(source_worker.get("profile") or "codex-cli"),
            backend=str(source_worker.get("backend") or "openclaw"),
            execution_mode=str(source_worker.get("execution_mode") or "docker"),
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

    def assign_run(self, worker_id: str, instruction: str, event_type: str = "run.queued") -> dict:
        worker = self.require_worker(worker_id)
        self._ensure_execution_allowed(worker)
        if worker["state"] == "paused":
            worker = self.resume_worker(worker_id)
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
        try:
            launched = self.runtime.desktop_action(worker, action, url=url, run_id=run_id)
        except TypeError as exc:
            if "run_id" not in str(exc):
                raise
            launched = self.runtime.desktop_action(worker, action, url=url)
        self.store.add_event(
            worker["project_id"],
            worker_id,
            self.store.get_active_run(worker_id)["run_id"] if self.store.get_active_run(worker_id) else None,
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
        updated = self._apply_runtime_info(worker_id, info, state="terminated", last_error="")
        self.store.add_event(worker["project_id"], worker_id, None, "worker.terminated", "Worker terminated")
        self._emit_callback(worker, "worker.terminated", message="Worker terminated")
        return updated or worker

    def reconcile_all_workers(self) -> None:
        for worker in self.store.list_all_workers():
            if worker["state"] == "terminated":
                continue
            info = self.runtime.reconcile_worker(worker)
            state = worker["state"]
            if state in {"running", "ready", "starting"}:
                state = "ready" if info.pid else "paused"
            if not info.pid:
                active_run = self.store.get_active_run(worker["worker_id"])
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

    def _collect_completed_run(self, worker: dict, run: dict) -> dict[str, str] | None:
        if not hasattr(self.runtime, "collect_completed_run"):
            return None
        try:
            return self.runtime.collect_completed_run(worker, run_id=run["run_id"])
        except TypeError as exc:
            if "run_id" not in str(exc):
                raise
            return self.runtime.collect_completed_run(worker)

    def _apply_recovered_run(self, worker: dict, run: dict, recovered: dict[str, str]) -> dict | None:
        worker_id = worker["worker_id"]
        state = str(recovered.get("state") or "failed")
        output_text = str(recovered.get("output_text") or "")
        error_text = str(recovered.get("error_text") or "")
        if state == "completed":
            self.store.finalize_run(run["run_id"], state="completed", output_text=output_text)
            self.store.update_worker(worker_id, state="ready", last_error="", last_run_id=run["run_id"])
            message = terminal_callback_message(output_text)
            full_message = terminal_callback_full_message(output_text)
            self.store.add_event(worker["project_id"], worker_id, run["run_id"], "run.completed", message[:TERMINAL_CALLBACK_MESSAGE_LIMIT] or "Run completed")
            recovered_run = {**run, "state": "completed", "output_text": output_text}
            refreshed_worker = self.store.get_worker(worker_id) or worker
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
            self._refresh_runtime_info(worker_id, state="ready", last_error="")
        else:
            self.store.finalize_run(run["run_id"], state="failed", error_text=error_text)
            self.store.update_worker(worker_id, state="ready", last_error=error_text, last_run_id=run["run_id"])
            self.store.add_event(worker["project_id"], worker_id, run["run_id"], "run.failed", error_text or "Run failed")
            recovered_run = {**run, "state": "failed", "error_text": error_text}
            self._emit_callback(worker, "run.failed", run=recovered_run, message=error_text or "Run failed")
            self._refresh_runtime_info(worker_id, state="ready", last_error=error_text)
        return self.store.get_worker(worker_id)

    def heal_worker(self, worker_id: str) -> dict | None:
        worker = self.store.get_worker(worker_id)
        if not worker or worker.get("state") in {"paused"}:
            return worker
        active_run = self.store.get_active_run(worker_id)
        if not active_run:
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
        updated = self._apply_runtime_info(worker["worker_id"], info, state="ready", last_error="")
        self.store.add_event(worker["project_id"], worker["worker_id"], None, event_type, message)
        return updated or worker

    def _apply_runtime_info(self, worker_id: str, info: RuntimeInfo, state: str, last_error: str) -> dict | None:
        return self.store.update_worker(
            worker_id,
            runtime=info.runtime,
            model=info.model,
            state=state,
            gateway_url=info.gateway_url,
            gateway_port=info.gateway_port,
            gateway_token=info.gateway_token,
            session_key=info.session_key,
            state_dir=info.state_dir,
            workspace_dir=info.workspace_dir,
            pid=info.pid,
            takeover_url=f"/ui/workers/{worker_id}",
            control_url=f"/ui/workers/{worker_id}",
            last_error=last_error,
        )

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

                run = self.store.claim_next_queued_run(worker_id)
                if not run:
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
                self.store.update_worker_state(worker_id, "running", last_error="")
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
                    self.store.update_worker_state(worker_id, "paused", last_error="")
                    self.store.add_event(worker["project_id"], worker_id, run["run_id"], "run.paused", str(exc))
                    self._emit_callback(worker, "run.paused", run=run, message=str(exc))
                    return
                except WorkerInterruptedError as exc:
                    if not self._processor_is_current(worker_id, generation):
                        return
                    self.store.finalize_run(run["run_id"], state="interrupted", error_text=str(exc))
                    self.store.update_worker_state(worker_id, "ready", last_error="")
                    self.store.add_event(worker["project_id"], worker_id, run["run_id"], "run.interrupted", str(exc))
                    self._emit_callback(worker, "run.interrupted", run=run, message=str(exc))
                    continue
                except WorkerTerminatedError as exc:
                    if not self._processor_is_current(worker_id, generation):
                        return
                    recovered = self._collect_completed_run(worker, run)
                    if recovered:
                        self._apply_recovered_run(worker, run, recovered)
                        continue
                    self.store.finalize_run(run["run_id"], state="cancelled", error_text=str(exc))
                    self.store.update_worker_state(worker_id, "terminated", last_error=str(exc))
                    self.store.add_event(worker["project_id"], worker_id, run["run_id"], "run.cancelled", str(exc))
                    self._emit_callback(worker, "run.cancelled", run=run, message=str(exc))
                    return
                except RuntimeErrorBase as exc:
                    if not self._processor_is_current(worker_id, generation):
                        return
                    worker_state = (self.store.get_worker(worker_id) or worker)["state"]
                    final_state = "failed"
                    if worker_state == "paused":
                        final_state = "interrupted"
                    elif worker_state == "terminated":
                        final_state = "cancelled"
                    self.store.finalize_run(run["run_id"], state=final_state, error_text=str(exc))
                    self.store.update_worker_state(worker_id, worker_state if worker_state in {"paused", "terminated"} else "ready", last_error=str(exc))
                    self.store.add_event(worker["project_id"], worker_id, run["run_id"], f"run.{final_state}", str(exc))
                    self._emit_callback(worker, f"run.{final_state}", run=run, message=str(exc))
                    if worker_state in {"paused", "terminated"}:
                        return
                    continue
                except Exception as exc:
                    if not self._processor_is_current(worker_id, generation):
                        return
                    self.store.finalize_run(run["run_id"], state="failed", error_text=str(exc))
                    self.store.update_worker_state(worker_id, "ready", last_error=str(exc))
                    self.store.add_event(worker["project_id"], worker_id, run["run_id"], "run.failed", str(exc))
                    self._emit_callback(worker, "run.failed", run=run, message=str(exc))
                    continue

                if not self._processor_is_current(worker_id, generation):
                    return
                self.store.finalize_run(run["run_id"], state="completed", output_text=output)
                self.store.update_worker(worker_id, state="ready", last_error="", last_run_id=run["run_id"])
                message = terminal_callback_message(output)
                full_message = terminal_callback_full_message(output)
                self.store.add_event(worker["project_id"], worker_id, run["run_id"], "run.completed", message[:TERMINAL_CALLBACK_MESSAGE_LIMIT] or "Run completed")
                completed_run = {**run, "state": "completed", "output_text": output}
                refreshed_worker = self.store.get_worker(worker_id) or worker
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
                self._refresh_runtime_info(worker_id, state="ready", last_error="")
        finally:
            if not self._release_processor(worker_id, generation):
                return
            pending = self.store.get_worker(worker_id)
            if pending and pending["state"] not in {"paused", "terminated"}:
                queued = next((item for item in self.store.list_runs_for_worker(worker_id, limit=10) if item["state"] == "queued"), None)
                if queued:
                    self._ensure_worker_processor(worker_id)
