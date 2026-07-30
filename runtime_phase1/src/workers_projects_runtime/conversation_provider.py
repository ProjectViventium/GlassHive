from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from fastapi import HTTPException, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .profile_runtime import _redact_text
from .service import WorkersProjectsService
from .store import Store

TERMINAL_RUN_STATES = {"completed", "failed", "cancelled", "interrupted"}
TERMINAL_REQUEST_STATES = {"completed", "failed", "cancelled"}
ACTIVITY_SUMMARIES = {
    "queued": "GlassHive queued the conversation turn.",
    "started": "The harness started working.",
    "reasoning-summary": "The harness updated its reasoning summary.",
    "plan": "The harness updated its plan.",
    "tool": "The harness used a tool.",
    "file": "The harness worked with a file.",
    "waiting": "The harness is waiting for capacity or a prerequisite.",
    "completed": "The harness completed the turn.",
    "failed": "The harness could not complete the turn.",
    "cancelled": "The harness turn was cancelled.",
}
MODEL_CREATED_AT = int(time.time())
DEFAULT_BOOTSTRAP_SIGNATURE_MAX_AGE_SECONDS = 5 * 60


@dataclass(frozen=True)
class HarnessModel:
    id: str
    display_name: str
    harness_profile: str
    native_model: str
    effort_choices: tuple[str, ...]
    recommended_effort: str
    context_window: int

    def api_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "object": "model",
            "created": MODEL_CREATED_AT,
            "owned_by": "glasshive",
            "display_name": self.display_name,
            "harness_profile": self.harness_profile,
            "native_model": self.native_model,
            "effort_choices": list(self.effort_choices),
            "recommended_effort": self.recommended_effort,
            "context_window": self.context_window,
            "readiness": _harness_readiness(self.harness_profile),
            "capabilities": {
                "main_chat": True,
                "cortex_execution": True,
                "phase_b_followup": True,
                "activation_classifier": False,
                "realtime_voice": False,
                "automatic_fallback_target": False,
                "workspace_binding": True,
                "conversation_session": True,
                "native_tools": True,
                "activity_stream": True,
                # Claude's stream-json transport currently exposes text deltas. Codex exec
                # --json exposes safe activity while working and the assistant text at completion.
                "incremental_text": self.harness_profile == "claude-code",
            },
        }


@dataclass(frozen=True)
class ProviderAuthContext:
    tenant_id: str
    principal_id: str
    trust_identity_headers: bool = False
    allow_full_access: bool = False
    default_access: Literal["full", "workspace"] = "workspace"


GLASSHIVE_MODELS: dict[str, HarnessModel] = {
    "codex-cli:gpt-5.6-sol": HarnessModel(
        id="codex-cli:gpt-5.6-sol",
        display_name="Codex / GPT-5.6 Sol",
        harness_profile="codex-cli",
        native_model="gpt-5.6-sol",
        effort_choices=("low", "medium", "high", "xhigh", "max", "ultra"),
        recommended_effort="medium",
        context_window=272_000,
    ),
    "claude-code:opus": HarnessModel(
        id="claude-code:opus",
        display_name="Claude / Opus",
        harness_profile="claude-code",
        native_model="opus",
        effort_choices=("low", "medium", "high", "xhigh", "max"),
        recommended_effort="max",
        context_window=200_000,
    ),
}


def _configured_binary(profile: str) -> str:
    env_name = "WPR_CODEX_BIN" if profile == "codex-cli" else "WPR_CLAUDE_CODE_BIN"
    fallback = "codex" if profile == "codex-cli" else "claude"
    configured = str(os.environ.get(env_name) or fallback).strip()
    if not configured:
        return ""
    path = Path(configured).expanduser()
    if path.is_absolute():
        return str(path) if path.is_file() and os.access(path, os.X_OK) else ""
    return str(shutil.which(configured) or "")


def _harness_auth_configured(profile: str) -> bool:
    if profile == "codex-cli":
        api_key = str(os.environ.get("OPENAI_API_KEY") or "").strip()
        if api_key and api_key != "user_provided" and "${" not in api_key:
            return True
        command = [_configured_binary(profile), "login", "status"]
    else:
        oauth_token = str(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or "").strip()
        if oauth_token and oauth_token != "user_provided" and "${" not in oauth_token:
            return True
        command = [_configured_binary(profile), "auth", "status"]
    if not command[0]:
        return False
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _harness_readiness(profile: str) -> dict[str, Any]:
    binary_ready = bool(_configured_binary(profile))
    auth_ready = _harness_auth_configured(profile)
    if binary_ready and auth_ready:
        status = "ready"
        detail = "Harness binary and local authentication are available."
    elif not binary_ready:
        status = "unavailable"
        detail = "Harness binary is not available on the GlassHive host."
    else:
        status = "authentication_required"
        detail = "Harness sign-in is required on the GlassHive host."
    return {
        "status": status,
        "binary_available": binary_ready,
        "authentication": "configured" if auth_ready else "required",
        "detail": detail,
    }


class WorkspaceBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["default", "life", "custom"] = "default"
    path: str | None = None

    @model_validator(mode="after")
    def validate_custom_path(self):
        if self.mode == "custom" and not str(self.path or "").strip():
            raise ValueError("A custom GlassHive workspace requires a server-side path")
        return self


class GlassHiveOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace: WorkspaceBinding = Field(default_factory=WorkspaceBinding)
    access: Literal["full", "workspace"] = "workspace"


class CompletionMetadata(BaseModel):
    model_config = ConfigDict(extra="allow")

    owner_id: str = ""
    conversation_id: str = ""
    agent_id: str = ""
    message_id: str = ""
    stream_id: str = ""
    surface: str = "web"
    input_mode: str = "text"
    idempotency_key: str = ""
    glasshive_options: GlassHiveOptions = Field(default_factory=GlassHiveOptions)
    bootstrap_bundle: dict[str, Any] = Field(default_factory=dict)


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: str
    content: Any = ""


class ChatStreamOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    include_usage: bool = False


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False
    stream_options: ChatStreamOptions | None = None
    # OpenAI's optional end-user identifier is accepted for wire compatibility,
    # but never treated as an authenticated GlassHive principal.
    user: str | None = None
    metadata: CompletionMetadata | None = None
    reasoning_effort: str | None = None
    # Standard Chat Completions tuning fields that harness-native models cannot honor are accepted
    # and intentionally ignored for wire portability. Shape-changing orchestration fields such as
    # tools/tool_choice/response_format remain forbidden and fail visibly.
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    max_tokens: int | None = Field(default=None, gt=0)
    max_completion_tokens: int | None = Field(default=None, gt=0)
    presence_penalty: float | None = Field(default=None, ge=-2, le=2)
    frequency_penalty: float | None = Field(default=None, ge=-2, le=2)
    seed: int | None = None
    stop: str | list[str] | None = None
    store: bool | None = None
    service_tier: str | None = None

    @model_validator(mode="after")
    def validate_stream_options(self):
        if self.stream_options is not None and not self.stream:
            raise ValueError("stream_options is only supported when stream is true")
        return self


class StreamingRedactor:
    """Redact bounded stream segments while retaining sensitive split-token prefixes."""

    def __init__(self, overlap: int = 64, max_buffer: int = 64 * 1024) -> None:
        self.overlap = max(1, int(overlap))
        self.max_buffer = max(self.overlap, int(max_buffer))
        self._buffer = ""

    def feed(self, value: str) -> str:
        self._buffer += str(value or "")
        if len(self._buffer) > self.max_buffer and not re.search(r"\s", self._buffer):
            self._buffer = ""
            return "[REDACTED_OVERSIZED_STREAM_SEGMENT]"

        stable_limit = max(0, len(self._buffer) - self.overlap)
        sensitive_tail = re.search(
            r"(?i)(?:/Users/|~/|bearer\s+|(?:api[_-]?key|token|secret|password|passwd|pwd)\s*[:=]?\s*|sk-|data:image/)[^\n]*$",
            self._buffer,
        )
        if sensitive_tail is not None:
            stable_limit = min(stable_limit, sensitive_tail.start())
        if stable_limit <= 0:
            return ""

        boundary = max(
            (match.end() for match in re.finditer(r"\s+", self._buffer[:stable_limit])),
            default=0,
        )
        if boundary <= 0:
            return ""
        stable = self._buffer[:boundary]
        self._buffer = self._buffer[boundary:]
        visible = _redact_text(stable)
        if len(self._buffer) > self.max_buffer and not re.search(r"\s", self._buffer):
            self._buffer = ""
            visible += "[REDACTED_OVERSIZED_STREAM_SEGMENT]"
        return visible

    def flush(self) -> str:
        result = _redact_text(self._buffer)
        self._buffer = ""
        return result


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content or "")
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "")
        if item_type in {"text", "input_text"}:
            parts.append(str(item.get("text") or item.get("input_text") or ""))
        elif item_type in {"image_url", "input_image", "file", "input_file"}:
            label = str(item.get("name") or item.get("filename") or item_type)
            parts.append(f"[Attached {label}]")
    return "\n".join(part for part in parts if part)


def _system_snapshot(messages: Iterable[ChatMessage]) -> str:
    instruction_parts: list[str] = []
    seen: set[str] = set()
    for message in messages:
        role = str(message.role or "").strip().lower()
        if role not in {"system", "developer"}:
            continue
        text = _message_text(message.content).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        instruction_parts.append(text)
    return "\n\n".join(instruction_parts)


def _history_instruction(messages: Iterable[ChatMessage], *, start_at: int = 0) -> str:
    all_messages = list(messages)
    current_system = _system_snapshot(all_messages)
    selected = [
        message
        for index, message in enumerate(all_messages)
        if index >= max(0, start_at)
        and str(message.role or "").strip().lower() not in {"system", "developer"}
    ]
    if not selected and not current_system:
        return "Continue the current conversation naturally."
    lines = [
        "Continue this conversation naturally. Honor AGENTS.md in the working folder as canonical instructions.",
        "Ask a concise clarifying question when the user's desired outcome genuinely cannot be inferred.",
        "Before any destructive, irreversible, externally consequential, or permission-expanding action, verify that it is explicitly within the user's request and pause for approval when it is not.",
    ]
    if current_system:
        lines.extend(
            [
                "The following single system snapshot is authoritative for this turn and supersedes every earlier system snapshot retained by the native session:",
                f"[system]\n{current_system}",
            ]
        )
    lines.append("Visible conversation context:")
    for message in selected:
        role = str(message.role or "user").strip().lower()
        text = _message_text(message.content).strip()
        if text:
            lines.append(f"[{role}]\n{text}")
    return "\n\n".join(lines).strip()


def _canonical_life_dir() -> Path:
    configured = str(os.environ.get("VIVENTIUM_LIFE_DIR") or "").strip()
    return Path(configured or "~/Documents/Viventium/Life").expanduser().resolve()


def _default_workspace_dir() -> Path:
    configured = str(
        os.environ.get("GLASSHIVE_PROVIDER_DEFAULT_WORKSPACE")
        or os.environ.get("VIVENTIUM_LIFE_DIR")
        or os.getcwd()
    ).strip()
    return Path(configured).expanduser().resolve()


def _header(request: Request, name: str) -> str:
    return str(request.headers.get(name) or "").strip()


def _decode_workspace_path(request: Request) -> str:
    encoded = _header(request, "x-glasshive-workspace-path-b64")
    if not encoded:
        return ""
    try:
        return base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid GlassHive workspace path header") from exc


def _decode_bootstrap_bundle(request: Request) -> dict[str, Any]:
    encoded = _header(request, "x-glasshive-bootstrap-bundle-b64")
    if not encoded:
        return {}
    signature_secret = str(
        os.environ.get("VIVENTIUM_GLASSHIVE_CAPABILITY_BROKER_SECRET") or ""
    ).strip()
    if not signature_secret:
        raise HTTPException(
            status_code=503,
            detail="GlassHive bootstrap signature verification is not configured",
        )
    issued_at = _header(request, "x-glasshive-bootstrap-timestamp")
    signature = _header(request, "x-glasshive-bootstrap-signature")
    try:
        issued_at_seconds = int(issued_at)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=403, detail="Invalid GlassHive bootstrap signature") from exc
    try:
        max_age_seconds = int(
            str(
                os.environ.get("GLASSHIVE_PROVIDER_BOOTSTRAP_SIGNATURE_MAX_AGE_SECONDS")
                or DEFAULT_BOOTSTRAP_SIGNATURE_MAX_AGE_SECONDS
            ).strip()
        )
    except ValueError:
        max_age_seconds = DEFAULT_BOOTSTRAP_SIGNATURE_MAX_AGE_SECONDS
    max_age_seconds = max(30, min(max_age_seconds, 3600))
    if abs(int(time.time()) - issued_at_seconds) > max_age_seconds:
        raise HTTPException(status_code=403, detail="Expired GlassHive bootstrap signature")
    expected = hmac.new(
        signature_secret.encode("utf-8"),
        f"v1\n{issued_at}\n{encoded}".encode(),
        hashlib.sha256,
    ).hexdigest()
    supplied = signature.removeprefix("sha256=")
    if not supplied or not hmac.compare_digest(expected, supplied):
        raise HTTPException(status_code=403, detail="Invalid GlassHive bootstrap signature")
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
        payload = json.loads(decoded)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid GlassHive bootstrap bundle header") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="GlassHive bootstrap bundle must be an object")
    return payload


def _hydrate_metadata(
    payload: ChatCompletionRequest,
    request: Request,
    auth: ProviderAuthContext,
) -> ChatCompletionRequest:
    """Apply authenticated defaults and optional trusted service context."""

    incoming = payload.metadata.model_dump(mode="python") if payload.metadata is not None else {}
    asserted_owner = _header(request, "x-viventium-user-id")
    incoming_owner = str(incoming.get("owner_id") or "").strip()
    if asserted_owner and incoming_owner and asserted_owner != incoming_owner:
        raise HTTPException(status_code=403, detail="Authenticated owner does not match completion metadata")
    requested_owner = asserted_owner or incoming_owner
    if requested_owner and requested_owner != auth.principal_id and not auth.trust_identity_headers:
        raise HTTPException(status_code=403, detail="Provider credential cannot delegate another owner")
    owner_id = requested_owner if auth.trust_identity_headers and requested_owner else auth.principal_id

    metadata = {
        **incoming,
        "owner_id": owner_id,
        "conversation_id": _header(request, "x-viventium-conversation-id")
        or str(incoming.get("conversation_id") or "").strip()
        or f"conversation-{uuid.uuid4().hex}",
        "agent_id": _header(request, "x-glasshive-agent-id")
        or str(incoming.get("agent_id") or "").strip()
        or "glasshive-direct",
        "message_id": _header(request, "x-viventium-message-id")
        or str(incoming.get("message_id") or "").strip(),
        "stream_id": _header(request, "x-viventium-stream-id")
        or str(incoming.get("stream_id") or "").strip(),
        "surface": _header(request, "x-viventium-surface")
        or str(incoming.get("surface") or "web").strip(),
        "input_mode": _header(request, "x-viventium-input-mode")
        or str(incoming.get("input_mode") or "text").strip(),
        "idempotency_key": _header(request, "x-glasshive-idempotency-key")
        or str(incoming.get("idempotency_key") or "").strip(),
        "bootstrap_bundle": _decode_bootstrap_bundle(request),
    }

    options = dict(incoming.get("glasshive_options") or {})
    workspace = dict(options.get("workspace") or {})
    workspace_mode = _header(request, "x-glasshive-workspace-mode")
    workspace_path = _decode_workspace_path(request)
    access = _header(request, "x-glasshive-access")
    if workspace_mode:
        workspace["mode"] = workspace_mode
    if workspace_path:
        workspace["path"] = workspace_path
    if workspace:
        options["workspace"] = workspace
    requested_access = str(access or options.get("access") or auth.default_access).strip().lower()
    if requested_access == "full" and not auth.allow_full_access:
        raise HTTPException(status_code=403, detail="Provider credential is not granted full host access")
    options["access"] = requested_access
    metadata["glasshive_options"] = options

    try:
        hydrated = CompletionMetadata.model_validate(metadata)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return payload.model_copy(update={"metadata": hydrated})


def _resolve_workspace(options: GlassHiveOptions) -> Path:
    if options.workspace.mode == "default":
        path = _default_workspace_dir()
    elif options.workspace.mode == "life":
        path = _canonical_life_dir()
    else:
        path = Path(str(options.workspace.path)).expanduser()
    if options.workspace.mode == "custom" and not path.is_absolute():
        raise HTTPException(
            status_code=400,
            detail="Custom GlassHive workspace must be an absolute server-side path",
        )
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError as exc:
        label = "GlassHive default workspace" if options.workspace.mode != "custom" else "Custom GlassHive workspace"
        raise HTTPException(status_code=409, detail=f"{label} does not exist on the GlassHive host: {path}") from exc
    if not resolved.is_dir():
        raise HTTPException(status_code=400, detail="GlassHive working folder must be a directory")
    if not os.access(resolved, os.R_OK | os.X_OK) or not os.access(resolved, os.W_OK):
        raise HTTPException(status_code=403, detail="GlassHive working folder is not readable and writable")
    if options.workspace.mode == "custom":
        configured_roots = [
            Path(value).expanduser().resolve()
            for value in str(os.environ.get("GLASSHIVE_PROVIDER_ALLOWED_WORKSPACE_ROOTS") or "").split(os.pathsep)
            if value.strip()
        ]
        allowed_roots = configured_roots or [_canonical_life_dir().parent.resolve()]
        if not any(resolved == root or resolved.is_relative_to(root) for root in allowed_roots):
            raise HTTPException(
                status_code=403,
                detail="Custom GlassHive workspace is outside the configured workspace roots",
            )
    return resolved


def _idempotency_key(payload: ChatCompletionRequest) -> str:
    explicit = str(payload.metadata.idempotency_key or payload.metadata.message_id or "").strip()
    if explicit:
        return explicit
    return f"request-{uuid.uuid4().hex}"


def _usage(messages: list[ChatMessage], output: str) -> dict[str, int]:
    prompt_chars = sum(len(_message_text(message.content)) for message in messages)
    completion_chars = len(output)
    prompt_tokens = max(1, (prompt_chars + 3) // 4)
    completion_tokens = max(1, (completion_chars + 3) // 4)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


def _native_visible_text(profile: str, stdout: str) -> str:
    """Extract only user-visible assistant text from complete native JSONL events."""

    assistant_parts: list[str] = []
    partial_parts: list[str] = []
    result_parts: list[str] = []
    for raw_line in str(stdout or "").splitlines():
        try:
            event = json.loads(raw_line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        if profile == "codex-cli":
            item = event.get("item") if isinstance(event.get("item"), dict) else {}
            if event.get("type") == "item.completed" and item.get("type") == "agent_message":
                text = str(item.get("text") or "").strip()
                if text:
                    assistant_parts.append(text)
            continue
        if profile != "claude-code":
            continue
        if event.get("type") == "stream_event":
            stream_event = event.get("event") if isinstance(event.get("event"), dict) else {}
            delta = stream_event.get("delta") if isinstance(stream_event.get("delta"), dict) else {}
            if stream_event.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
                text = str(delta.get("text") or "")
                if text:
                    partial_parts.append(text)
            continue
        if event.get("type") == "result":
            text = str(event.get("result") or "").strip()
            if text:
                result_parts.append(text)
            continue
        if event.get("type") != "assistant":
            continue
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        content = message.get("content") if isinstance(message.get("content"), list) else []
        text = "".join(
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
        if text:
            assistant_parts.append(text)
    if profile == "claude-code":
        return "".join(partial_parts) or "".join(assistant_parts) or (result_parts[-1] if result_parts else "")
    return "".join(assistant_parts)


def _native_usage(profile: str, stdout: str) -> dict[str, int] | None:
    latest: dict[str, Any] | None = None
    for raw_line in str(stdout or "").splitlines():
        try:
            event = json.loads(raw_line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        candidate = event.get("usage")
        if profile == "codex-cli" and event.get("type") == "turn.completed":
            candidate = event.get("usage")
        if isinstance(candidate, dict):
            latest = candidate
    if not latest:
        return None
    prompt_tokens = int(
        latest.get("input_tokens")
        or latest.get("prompt_tokens")
        or 0
    )
    completion_tokens = int(
        latest.get("output_tokens")
        or latest.get("completion_tokens")
        or 0
    )
    if prompt_tokens <= 0 and completion_tokens <= 0:
        return None
    return {
        "prompt_tokens": max(0, prompt_tokens),
        "completion_tokens": max(0, completion_tokens),
        "total_tokens": max(0, prompt_tokens) + max(0, completion_tokens),
    }


def _native_log_excluded_prefix_bytes(stdout: str) -> int:
    first_line = str(stdout or "").splitlines()[0] if str(stdout or "") else ""
    try:
        event = json.loads(first_line)
    except (json.JSONDecodeError, TypeError):
        return 0
    if not isinstance(event, dict) or event.get("type") != "glasshive.log_compacted":
        return 0
    try:
        return max(0, int(event.get("excluded_prefix_bytes") or 0))
    except (TypeError, ValueError):
        return 0


def _activity_status(value: Any) -> str:
    status = str(value or "").strip().lower()
    return status if status in {"started", "running", "completed", "failed", "cancelled"} else ""


def _normalized_harness_activity(profile: str, stdout: str) -> list[dict[str, Any]]:
    """Convert native JSONL into safe observable steps, never model chain-of-thought or tool inputs."""

    normalized: list[dict[str, Any]] = []
    absolute_offset = 0
    for raw_segment in str(stdout or "").splitlines(keepends=True):
        raw_line = raw_segment.rstrip("\r\n")
        try:
            event = json.loads(raw_line)
        except (json.JSONDecodeError, TypeError):
            absolute_offset += len(raw_segment.encode("utf-8"))
            continue
        if not isinstance(event, dict):
            absolute_offset += len(raw_segment.encode("utf-8"))
            continue
        if event.get("type") == "glasshive.log_compacted":
            absolute_offset = _native_log_excluded_prefix_bytes(raw_line)
            continue
        source_line_id = hashlib.sha256(
            f"{absolute_offset}:".encode() + raw_line.encode("utf-8")
        ).hexdigest()[:20]
        absolute_offset += len(raw_segment.encode("utf-8"))

        if profile == "codex-cli":
            if str(event.get("type") or "") != "item.completed":
                continue
            item = event.get("item") if isinstance(event.get("item"), dict) else {}
            item_type = str(item.get("type") or "").strip().lower()
            event_type = ""
            summary = ""
            payload: dict[str, Any] = {}
            if item_type == "reasoning":
                event_type = "reasoning-summary"
                summary = "The harness completed a reasoning step."
            elif item_type in {"todo_list", "plan"}:
                event_type = "plan"
                summary = "The harness updated its plan."
                entries = item.get("items") or item.get("steps") or []
                if isinstance(entries, list):
                    payload["step_count"] = len(entries)
            elif item_type == "command_execution":
                event_type = "tool"
                summary = "The harness ran a shell command."
                payload["tool"] = "shell"
                status = _activity_status(item.get("status"))
                if status:
                    payload["status"] = status
                if isinstance(item.get("exit_code"), int):
                    payload["exit_code"] = item["exit_code"]
            elif item_type in {"mcp_tool_call", "dynamic_tool_call"}:
                event_type = "tool"
                summary = "The harness used a connected tool."
                payload["tool"] = "connected_tool"
                status = _activity_status(item.get("status"))
                if status:
                    payload["status"] = status
            elif item_type in {"web_search", "web_search_call"}:
                event_type = "tool"
                summary = "The harness searched the web."
                payload["tool"] = "web_search"
            elif item_type in {"file_change", "file_changes"}:
                event_type = "file"
                summary = "The harness updated workspace files."
                payload["tool"] = "file"
                changes = item.get("changes") or []
                if isinstance(changes, list):
                    payload["change_count"] = len(changes)
                status = _activity_status(item.get("status"))
                if status:
                    payload["status"] = status
            if event_type:
                normalized.append(
                    {
                        "event_type": event_type,
                        "summary": summary,
                        "payload": {
                            "source_event_id": f"codex-cli:{source_line_id}:0",
                            **payload,
                        },
                    }
                )
            continue

        if profile != "claude-code" or str(event.get("type") or "") != "assistant":
            continue
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        content = message.get("content") if isinstance(message.get("content"), list) else []
        for content_index, block in enumerate(content):
            if not isinstance(block, dict) or str(block.get("type") or "") != "tool_use":
                continue
            tool_name = str(block.get("name") or "").strip().lower()
            if tool_name in {"edit", "multiedit", "write", "notebookedit", "read", "glob", "grep"}:
                event_type = "file"
                summary = "The harness used a file tool."
                tool_category = "file"
            elif tool_name in {"websearch", "webfetch"}:
                event_type = "tool"
                summary = "The harness used a web tool."
                tool_category = "web_search"
            elif tool_name in {"bash", "shell"}:
                event_type = "tool"
                summary = "The harness ran a shell command."
                tool_category = "shell"
            else:
                event_type = "tool"
                summary = "The harness used a connected tool."
                tool_category = "connected_tool"
            normalized.append(
                {
                    "event_type": event_type,
                    "summary": summary,
                    "payload": {
                        "source_event_id": f"claude-code:{source_line_id}:{content_index}",
                        "tool": tool_category,
                    },
                }
            )
    return normalized


class ConversationProvider:
    def __init__(self, store: Store, service: WorkersProjectsService) -> None:
        self.store = store
        self.service = service
        # The provider runs as one local API process. Serialize session reservation and request
        # creation so concurrent transport retries cannot create orphan workers before SQLite's
        # idempotency uniqueness check runs.
        self._start_lock = threading.RLock()
        self._prestart_cancellations: dict[tuple[str, str, str], float] = {}
        self._last_retention_monotonic = 0.0
        self._apply_retention_policy()

    def _prune_prestart_cancellations(self) -> None:
        cutoff = time.monotonic() - 600
        self._prestart_cancellations = {
            key: created_at
            for key, created_at in self._prestart_cancellations.items()
            if created_at >= cutoff
        }

    def _apply_retention_policy(self) -> None:
        """Bound private provider state without ever pruning an active conversation turn."""

        try:
            request_days = max(
                1,
                int(os.environ.get("GLASSHIVE_PROVIDER_REQUEST_RETENTION_DAYS", "30") or "30"),
            )
            session_days = max(
                request_days,
                int(os.environ.get("GLASSHIVE_PROVIDER_SESSION_RETENTION_DAYS", "90") or "90"),
            )
        except ValueError:
            request_days, session_days = 30, 90

        now = datetime.now(UTC)
        request_cutoff = (now - timedelta(days=request_days)).isoformat()
        session_cutoff = (now - timedelta(days=session_days)).isoformat()
        try:
            self.store.prune_terminal_provider_requests(updated_before=request_cutoff)
            stale_sessions = self.store.list_stale_provider_sessions(updated_before=session_cutoff)
            removable: list[str] = []
            for session in stale_sessions:
                worker_id = str(session.get("worker_id") or "").strip()
                worker = self.store.get_worker(worker_id) if worker_id else None
                if worker and str(worker.get("state") or "") != "terminated":
                    self.service.terminate_worker(worker_id)
                removable.append(str(session["session_id"]))
            self.store.delete_provider_sessions(removable)
        except (OSError, RuntimeError, ValueError):
            # Retention is housekeeping and must not make the authenticated provider unavailable.
            pass
        finally:
            self._last_retention_monotonic = time.monotonic()

    def _maybe_apply_retention_policy(self) -> None:
        try:
            interval = max(
                60,
                int(os.environ.get("GLASSHIVE_PROVIDER_RETENTION_INTERVAL_SECONDS", "3600") or "3600"),
            )
        except ValueError:
            interval = 3600
        if time.monotonic() - self._last_retention_monotonic >= interval:
            self._apply_retention_policy()

    def models_payload(self) -> dict[str, Any]:
        return {"object": "list", "data": [model.api_payload() for model in GLASSHIVE_MODELS.values()]}

    def _model(self, model_id: str) -> HarnessModel:
        model = GLASSHIVE_MODELS.get(str(model_id or "").strip())
        if model is None:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported GlassHive model '{model_id}'. Select an exact ID from GET /v1/models.",
            )
        return model

    def _effort(self, payload: ChatCompletionRequest, model: HarnessModel) -> str:
        effort = str(payload.reasoning_effort or model.recommended_effort).strip().lower()
        if effort not in model.effort_choices:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported effort '{effort}' for {model.id}; choose one of {', '.join(model.effort_choices)}.",
            )
        return effort

    def _native_bundle(
        self,
        payload: ChatCompletionRequest,
        model: HarnessModel,
        effort: str,
    ) -> dict[str, Any]:
        incoming = dict(payload.metadata.bootstrap_bundle or {})
        incoming_env = incoming.get("env") if isinstance(incoming.get("env"), dict) else {}
        effort_env = (
            {"WPR_CODEX_CLI_REASONING_EFFORT": effort}
            if model.harness_profile == "codex-cli"
            else {"WPR_CLAUDE_CODE_EFFORT": effort}
        )
        return {
            **incoming,
            "run_mode": "conversation",
            "provider_model": model.native_model,
            "access_mode": payload.metadata.glasshive_options.access,
            "env": {**incoming_env, **effort_env},
            "provider_capabilities": {
                "self_delegation": False,
                "native_tools": True,
            },
        }

    @staticmethod
    def _session_manifest(session: dict[str, Any] | None) -> dict[str, Any]:
        try:
            value = json.loads(str((session or {}).get("context_manifest_json") or "{}"))
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}

    def assert_request_owner(self, request_id: str, owner_id: str) -> dict[str, Any]:
        record = self.store.get_provider_request(request_id)
        if not record:
            raise HTTPException(status_code=404, detail="GlassHive request not found")
        if str(record.get("owner_id") or "") != str(owner_id or "").strip():
            raise HTTPException(status_code=403, detail="GlassHive request belongs to another owner")
        return record

    def _create_native_session(
        self,
        payload: ChatCompletionRequest,
        model: HarnessModel,
        workspace: Path,
        effort: str,
        *,
        tenant_id: str,
    ) -> dict[str, Any]:
        metadata = payload.metadata
        project = self.service.create_project(
            metadata.owner_id,
            f"GlassHive conversation {metadata.conversation_id}",
            "Persistent GlassHive conversation session",
            model.harness_profile,
            tenant_id=tenant_id,
        )
        bundle = self._native_bundle(payload, model, effort)
        worker = self.service.create_worker(
            project_id=project["project_id"],
            owner_id=metadata.owner_id,
            name=f"GlassHive {metadata.agent_id}",
            role="conversation-agent",
            profile=model.harness_profile,
            backend="",
            execution_mode="host",
            alias=f"conversation-{metadata.conversation_id}-{metadata.agent_id}",
            workspace_root=str(workspace),
            bootstrap_profile="glasshive-conversation-v1",
            bootstrap_bundle=bundle,
            tenant_id=tenant_id,
            start_synchronously=True,
        )
        if str(worker.get("state") or "") == "failed":
            raise HTTPException(status_code=409, detail=str(worker.get("last_error") or "GlassHive harness is not ready"))
        worker = self.store.update_worker(
            str(worker["worker_id"]),
            model=model.native_model,
            workspace_dir=str(workspace),
        ) or worker
        return self.store.upsert_provider_session(
            tenant_id=tenant_id,
            owner_id=metadata.owner_id,
            conversation_id=metadata.conversation_id,
            agent_id=metadata.agent_id,
            model_id=model.id,
            project_id=project["project_id"],
            worker_id=worker["worker_id"],
            workspace_dir=str(workspace),
            access_mode=metadata.glasshive_options.access,
            history_count=0,
            context_manifest={
                "messages": 0,
                "excluded": [],
                "compactions": [],
                "effort": effort,
                "system_snapshot_sha256": hashlib.sha256(
                    _system_snapshot(payload.messages).encode("utf-8")
                ).hexdigest(),
            },
        )

    def _session(
        self,
        payload: ChatCompletionRequest,
        model: HarnessModel,
        workspace: Path,
        effort: str,
        *,
        tenant_id: str,
    ) -> tuple[dict[str, Any], bool]:
        metadata = payload.metadata
        existing = self.store.get_provider_session(
            tenant_id=tenant_id,
            owner_id=metadata.owner_id,
            conversation_id=metadata.conversation_id,
            agent_id=metadata.agent_id,
        )
        expected_access = metadata.glasshive_options.access
        existing_worker = (
            self.store.get_worker(str(existing["worker_id"])) if existing else None
        )
        existing_manifest = self._session_manifest(existing)
        binding_changed = bool(
            existing
            and (
                existing["model_id"] != model.id
                or Path(str(existing["workspace_dir"])).resolve() != workspace
                or existing["access_mode"] != expected_access
                or not existing_worker
                or str(existing_worker.get("state") or "") in {"failed", "terminated"}
            )
        )
        if existing and not binding_changed:
            bundle = self._native_bundle(payload, model, effort)
            self.store.update_worker(
                str(existing["worker_id"]),
                bootstrap_bundle_json=json.dumps(bundle, sort_keys=True),
                model=model.native_model,
                workspace_dir=str(workspace),
            )
            current_manifest = {
                **existing_manifest,
                "effort": effort,
                "system_snapshot_sha256": hashlib.sha256(
                    _system_snapshot(payload.messages).encode("utf-8")
                ).hexdigest(),
            }
            updated_session = self.store.update_provider_session_history(
                str(existing["session_id"]),
                history_count=int(existing.get("history_count") or 0),
                context_manifest=current_manifest,
            )
            return updated_session or existing, False
        if existing:
            old_worker = self.store.get_worker(str(existing["worker_id"]))
            if old_worker and old_worker.get("state") != "terminated":
                self.service.terminate_worker(str(existing["worker_id"]))
        return self._create_native_session(payload, model, workspace, effort, tenant_id=tenant_id), True

    def start(self, payload: ChatCompletionRequest, *, tenant_id: str = "local") -> dict[str, Any]:
        with self._start_lock:
            self._maybe_apply_retention_policy()
            model = self._model(payload.model)
            effort = self._effort(payload, model)
            idempotency_key = _idempotency_key(payload)
            self._prune_prestart_cancellations()
            cancellation_key = (tenant_id, payload.metadata.owner_id, idempotency_key)
            if self._prestart_cancellations.pop(cancellation_key, None) is not None:
                raise HTTPException(
                    status_code=409,
                    detail="GlassHive request was cancelled before native execution started",
                )
            duplicate = self.store.get_provider_request(
                tenant_id=tenant_id,
                owner_id=payload.metadata.owner_id,
                idempotency_key=idempotency_key,
            )
            if duplicate:
                return duplicate
            workspace = _resolve_workspace(payload.metadata.glasshive_options)
            session, new_native_session = self._session(
                payload,
                model,
                workspace,
                effort,
                tenant_id=tenant_id,
            )
            request_record, created = self.store.create_provider_request(
                tenant_id=tenant_id,
                owner_id=payload.metadata.owner_id,
                session_id=session["session_id"],
                idempotency_key=idempotency_key,
                message_id=payload.metadata.message_id,
                stream_id=payload.metadata.stream_id,
                requested_history_count=len(payload.messages),
            )
            if not created:
                return request_record
            self.store.add_provider_activity(
                request_record["request_id"],
                "queued",
                ACTIVITY_SUMMARIES["queued"],
                {"surface": payload.metadata.surface, "input_mode": payload.metadata.input_mode},
            )
            previous_history_count = int(session.get("history_count") or 0)
            start_at = (
                previous_history_count
                if not new_native_session and len(payload.messages) > previous_history_count
                else 0
            )
            instruction = _history_instruction(payload.messages, start_at=start_at)
            try:
                run = self.service.assign_run(str(session["worker_id"]), instruction)
            except Exception as exc:
                self.store.update_provider_request(
                    str(request_record["request_id"]),
                    state="failed",
                )
                self.store.add_provider_activity(
                    str(request_record["request_id"]),
                    "failed",
                    ACTIVITY_SUMMARIES["failed"],
                    {"failure_class": type(exc).__name__},
                )
                raise
            return self.store.update_provider_request(
                request_record["request_id"],
                run_id=run["run_id"],
                state="queued",
            ) or request_record

    def _sync(self, request_record: dict[str, Any]) -> dict[str, Any]:
        request_id = str(request_record["request_id"])
        run_id = str(request_record.get("run_id") or "")
        if not run_id:
            return request_record
        run = self.store.get_run(run_id)
        if not run:
            return request_record
        activities = self.store.list_provider_activity(request_id)
        activity_types = {str(item["event_type"]) for item in activities}
        run_state = str(run.get("state") or "queued")
        execution_started = bool(run.get("started_at") or run_state != "queued")
        if execution_started and "started" not in activity_types:
            self.store.add_provider_activity(request_id, "started", ACTIVITY_SUMMARIES["started"])
        # A harness log can become readable just before the processor's durable
        # run-state update is visible. Never publish native tool/file activity
        # ahead of the normalized `started` event.
        if execution_started:
            self._sync_native_activity(request_record, run)
        activities = self.store.list_provider_activity(request_id)
        activity_types = {str(item["event_type"]) for item in activities}
        if run_state == "queued" and run.get("retry_after") and "waiting" not in activity_types:
            self.store.add_provider_activity(
                request_id,
                "waiting",
                ACTIVITY_SUMMARIES["waiting"],
                {"retry_after": run.get("retry_after")},
            )
        if run_state not in TERMINAL_RUN_STATES:
            return self.store.update_provider_request(request_id, state="running" if run_state == "running" else "queued") or request_record

        final_state = "completed" if run_state == "completed" else ("cancelled" if run_state in {"cancelled", "interrupted"} else "failed")
        if final_state not in activity_types:
            summary = ACTIVITY_SUMMARIES[final_state]
            payload = {"failure_class": str(run.get("failure_class") or "")} if final_state == "failed" else {}
            self.store.add_provider_activity(request_id, final_state, summary, payload)
        updated = self.store.update_provider_request(request_id, state=final_state) or request_record
        if final_state == "completed":
            current_session = self.store.get_provider_session_by_id(
                str(request_record["session_id"])
            ) or {}
            visible_history_count = max(
                int(current_session.get("history_count") or 0),
                int(request_record.get("requested_history_count") or 0) + 1,
            )
            current_manifest = self._session_manifest(current_session)
            self.store.update_provider_session_history(
                str(request_record["session_id"]),
                history_count=visible_history_count,
                context_manifest={
                    **current_manifest,
                    "messages": visible_history_count,
                    "last_request_id": request_id,
                    "effort": current_manifest.get("effort", ""),
                    "system_snapshot_sha256": current_manifest.get("system_snapshot_sha256", ""),
                },
            )
        return updated

    def _sync_native_activity(self, request_record: dict[str, Any], run: dict[str, Any]) -> None:
        collector = getattr(self.service.runtime, "provider_activity_log", None)
        if not callable(collector):
            return
        session = self.store.get_provider_session_by_id(str(request_record["session_id"]))
        if not session:
            return
        worker = self.store.get_worker(str(session["worker_id"]))
        if not worker:
            return
        try:
            profile, stdout = collector(worker, str(run.get("run_id") or ""))
        except (OSError, RuntimeError, ValueError):
            return
        excluded_prefix_bytes = _native_log_excluded_prefix_bytes(str(stdout or ""))
        if excluded_prefix_bytes:
            manifest = self._session_manifest(session)
            compactions = list(manifest.get("compactions") or [])
            compaction = {
                "kind": "native_log_window",
                "excluded_prefix_bytes": excluded_prefix_bytes,
            }
            if not compactions or compactions[-1] != compaction:
                compactions.append(compaction)
                self.store.update_provider_session_history(
                    str(session["session_id"]),
                    history_count=int(session.get("history_count") or 0),
                    context_manifest={**manifest, "compactions": compactions[-20:]},
                )
        existing_source_ids: set[str] = set()
        for activity in self.store.list_provider_activity(str(request_record["request_id"])):
            try:
                activity_payload = json.loads(str(activity.get("payload_json") or "{}"))
            except json.JSONDecodeError:
                continue
            if isinstance(activity_payload, dict) and activity_payload.get("source_event_id"):
                existing_source_ids.add(str(activity_payload["source_event_id"]))
        for event in _normalized_harness_activity(str(profile or ""), str(stdout or "")):
            source_event_id = str(event["payload"].get("source_event_id") or "")
            if not source_event_id or source_event_id in existing_source_ids:
                continue
            self.store.add_provider_activity(
                str(request_record["request_id"]),
                str(event["event_type"]),
                str(event["summary"]),
                dict(event["payload"]),
            )
            existing_source_ids.add(source_event_id)

    def _native_output_snapshot(
        self,
        request_record: dict[str, Any],
        run: dict[str, Any],
    ) -> str:
        collector = getattr(self.service.runtime, "provider_activity_log", None)
        if not callable(collector):
            return ""
        session = self.store.get_provider_session_by_id(str(request_record["session_id"]))
        if not session:
            return ""
        worker = self.store.get_worker(str(session["worker_id"]))
        if not worker:
            return ""
        try:
            profile, stdout = collector(worker, str(run.get("run_id") or ""))
        except (OSError, RuntimeError, ValueError):
            return ""
        return _native_visible_text(str(profile or ""), str(stdout or ""))

    def _native_usage_snapshot(
        self,
        request_record: dict[str, Any],
        run: dict[str, Any],
    ) -> dict[str, int] | None:
        collector = getattr(self.service.runtime, "provider_activity_log", None)
        if not callable(collector):
            return None
        session = self.store.get_provider_session_by_id(str(request_record["session_id"]))
        if not session:
            return None
        worker = self.store.get_worker(str(session["worker_id"]))
        if not worker:
            return None
        try:
            profile, stdout = collector(worker, str(run.get("run_id") or ""))
        except (OSError, RuntimeError, ValueError):
            return None
        return _native_usage(str(profile or ""), str(stdout or ""))

    def _conversation_output(
        self,
        request_record: dict[str, Any],
        run: dict[str, Any],
    ) -> str:
        native = self._native_output_snapshot(request_record, run)
        return _redact_text(native or str(run.get("output_text") or ""))

    def _completion_usage(
        self,
        request_record: dict[str, Any],
        run: dict[str, Any],
        payload: ChatCompletionRequest,
        output: str,
    ) -> tuple[dict[str, int], str]:
        native = self._native_usage_snapshot(request_record, run)
        if native:
            return native, "native"
        return _usage(payload.messages, output), "estimated"

    def wait(self, request_id: str, *, timeout: float | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
        timeout = timeout or float(os.environ.get("GLASSHIVE_PROVIDER_RESPONSE_TIMEOUT_S", "660") or "660")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            record = self.store.get_provider_request(request_id)
            if not record:
                raise HTTPException(status_code=404, detail="GlassHive request not found")
            record = self._sync(record)
            if record["state"] in TERMINAL_REQUEST_STATES:
                run = self.store.get_run(str(record.get("run_id") or "")) or {}
                return record, run
            time.sleep(0.2)
        raise HTTPException(status_code=504, detail="GlassHive request is still running; reconnect with the same idempotency key")

    def response_payload(
        self,
        request_record: dict[str, Any],
        run: dict[str, Any],
        payload: ChatCompletionRequest,
    ) -> dict[str, Any]:
        cached = str(request_record.get("response_json") or "").strip()
        if cached:
            try:
                parsed = json.loads(cached)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass
        if request_record["state"] != "completed":
            detail = str(run.get("failure_user_message") or run.get("error_text") or "GlassHive harness run failed")
            raise HTTPException(status_code=502, detail=_redact_text(detail))
        output = self._conversation_output(request_record, run)
        usage, usage_source = self._completion_usage(request_record, run, payload, output)
        response = {
            "id": request_record["request_id"],
            "object": "chat.completion",
            "created": int(time.time()),
            "model": payload.model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": output, "reasoning_content": ""},
                    "finish_reason": "stop",
                }
            ],
            "usage": usage,
            "glasshive": {
                "request_id": request_record["request_id"],
                "activity_url": f"/v1/requests/{request_record['request_id']}/activity",
                "usage_source": usage_source,
            },
        }
        self.store.update_provider_request(
            str(request_record["request_id"]),
            response_json=json.dumps(response, separators=(",", ":")),
        )
        return response

    async def stream(
        self,
        request_record: dict[str, Any],
        payload: ChatCompletionRequest,
        request: Request,
    ):
        request_id = str(request_record["request_id"])
        created = int(time.time())
        initial = {
            "id": request_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": payload.model,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        }
        yield f"data: {json.dumps(initial, separators=(',', ':'))}\n\n"
        emitted_activities: set[int] = set()
        redactor = StreamingRedactor()
        native_snapshot = ""
        emitted_content = ""
        execution_started_seen = False
        last_heartbeat = time.monotonic()
        while True:
            if await request.is_disconnected():
                # A browser/SSE disconnect is not cancellation. The same idempotency key can
                # reattach while the native run continues.
                return
            record = await asyncio.to_thread(self.store.get_provider_request, request_id)
            if not record:
                break
            record = await asyncio.to_thread(self._sync, record)
            run = await asyncio.to_thread(
                self.store.get_run,
                str(record.get("run_id") or ""),
            ) or {}
            latest_native = await asyncio.to_thread(
                self._native_output_snapshot,
                record,
                run,
            )
            if latest_native.startswith(native_snapshot):
                raw_delta = latest_native[len(native_snapshot) :]
                native_snapshot = latest_native
                visible_delta = redactor.feed(raw_delta)
                if visible_delta:
                    emitted_content += visible_delta
                    content_chunk = {
                        "id": request_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": payload.model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": visible_delta},
                                "finish_reason": None,
                            }
                        ],
                    }
                    yield f"data: {json.dumps(content_chunk, separators=(',', ':'))}\n\n"
                    last_heartbeat = time.monotonic()
            for event in await asyncio.to_thread(self.store.list_provider_activity, request_id):
                sequence = int(event["sequence_id"])
                event_type = str(event["event_type"])
                if sequence in emitted_activities or event_type in {"completed", "failed", "cancelled"}:
                    continue
                emitted_activities.add(sequence)
                if event_type == "started":
                    execution_started_seen = True
                if not execution_started_seen or event_type in {"queued", "waiting"}:
                    # The dedicated activity endpoint retains pre-start queue/wait visibility.
                    # The chat reasoning channel begins only at native execution start so its
                    # first delta is a truthful duplicate-author/fallback commit point.
                    continue
                summary = f"{_redact_text(str(event['summary']))}\n"
                activity_delta: dict[str, Any] = {
                    "reasoning_content": summary,
                }
                summary_chunk = {
                    "id": request_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": payload.model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": activity_delta,
                            "finish_reason": None,
                        }
                    ],
                }
                yield f"data: {json.dumps(summary_chunk, separators=(',', ':'))}\n\n"
                last_heartbeat = time.monotonic()
            if record["state"] in TERMINAL_REQUEST_STATES:
                if record["state"] == "completed":
                    output = await asyncio.to_thread(self._conversation_output, record, run)
                    flushed = redactor.flush()
                    if flushed:
                        emitted_content += flushed
                    remaining = output[len(emitted_content) :] if output.startswith(emitted_content) else ""
                    terminal_delta = flushed + remaining
                    if emitted_content and not output.startswith(emitted_content):
                        terminal_delta = (
                            "\n\n[The harness corrected its final response after terminal reconciliation.]\n"
                            + output
                        )
                    if terminal_delta:
                        content_chunk = {
                            "id": request_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": payload.model,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"content": terminal_delta},
                                    "finish_reason": None,
                                }
                            ],
                        }
                        yield f"data: {json.dumps(content_chunk, separators=(',', ':'))}\n\n"
                    finish_reason = "stop"
                else:
                    error = _redact_text(str(run.get("failure_user_message") or run.get("error_text") or "GlassHive run failed"))
                    error_chunk = {
                        "id": request_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": payload.model,
                        "error": {"message": error, "type": "glasshive_runtime_error"},
                        "choices": [],
                    }
                    yield f"data: {json.dumps(error_chunk, separators=(',', ':'))}\n\n"
                    finish_reason = "stop"
                usage, usage_source = await asyncio.to_thread(
                    self._completion_usage,
                    record,
                    run,
                    payload,
                    output if record["state"] == "completed" else "",
                )
                final_chunk = {
                    "id": request_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": payload.model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
                }
                yield f"data: {json.dumps(final_chunk, separators=(',', ':'))}\n\n"
                if payload.stream_options and payload.stream_options.include_usage:
                    usage_chunk = {
                        "id": request_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": payload.model,
                        "choices": [],
                        "usage": usage,
                        "glasshive": {"usage_source": usage_source},
                    }
                    yield f"data: {json.dumps(usage_chunk, separators=(',', ':'))}\n\n"
                yield "data: [DONE]\n\n"
                return
            if time.monotonic() - last_heartbeat >= 15:
                yield ": heartbeat\n\n"
                last_heartbeat = time.monotonic()
            await asyncio.sleep(0.2)

    def activity_payload(self, request_id: str, *, after_sequence: int = 0) -> dict[str, Any]:
        record = self.store.get_provider_request(request_id)
        if not record:
            raise HTTPException(status_code=404, detail="GlassHive request not found")
        self._sync(record)
        data = []
        for item in self.store.list_provider_activity(request_id, after_sequence=after_sequence):
            try:
                payload = json.loads(str(item.get("payload_json") or "{}"))
            except json.JSONDecodeError:
                payload = {}
            if isinstance(payload, dict):
                payload.pop("source_event_id", None)
            data.append(
                {
                    "id": int(item["sequence_id"]),
                    "event": item["event_type"],
                    "summary": _redact_text(str(item["summary"])),
                    "data": _redact_json_value(payload) if isinstance(payload, dict) else {},
                    "created_at": item["created_at"],
                }
            )
        return {"object": "list", "request_id": request_id, "data": data}

    def cancel(self, request_id: str) -> dict[str, Any]:
        record = self.store.get_provider_request(request_id)
        if not record:
            raise HTTPException(status_code=404, detail="GlassHive request not found")
        record = self._sync(record)
        if record["state"] in TERMINAL_REQUEST_STATES:
            return record
        session = self.store.get_provider_session_by_id(str(record["session_id"]))
        run_id = str(record.get("run_id") or "").strip()
        if session and run_id:
            self.service.interrupt_worker(
                str(session["worker_id"]),
                run_id=run_id,
            )
        updated = self.store.update_provider_request(request_id, state="cancelled") or record
        existing_types = {item["event_type"] for item in self.store.list_provider_activity(request_id)}
        if "cancelled" not in existing_types:
            self.store.add_provider_activity(request_id, "cancelled", ACTIVITY_SUMMARIES["cancelled"])
        return updated

    def cancel_by_idempotency(
        self,
        idempotency_key: str,
        owner_id: str,
        *,
        tenant_id: str = "local",
    ) -> dict[str, Any]:
        owner_id = str(owner_id or "").strip()
        normalized_key = str(idempotency_key or "").strip()
        if not normalized_key:
            raise HTTPException(status_code=400, detail="GlassHive idempotency key is required")
        with self._start_lock:
            record = self.store.get_provider_request(
                tenant_id=tenant_id,
                owner_id=owner_id,
                idempotency_key=normalized_key,
            )
            if record:
                return self.cancel(str(record["request_id"]))
            self._prune_prestart_cancellations()
            self._prestart_cancellations[(tenant_id, owner_id, normalized_key)] = time.monotonic()
            return {"request_id": "", "state": "cancelled"}


def _redact_json_value(value: Any) -> Any:
    """Redact every string in an activity payload before it crosses the provider boundary."""
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, list):
        return [_redact_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _redact_json_value(item) for key, item in value.items()}
    return value


def _is_provider_path(path: str) -> bool:
    return path in {"/v1/models", "/v1/chat/completions"} or path.startswith("/v1/requests/")


def _openai_error(status_code: int, message: str, code: str, *, param: str | None = None) -> JSONResponse:
    error_type = "invalid_request_error" if status_code < 500 else "server_error"
    if status_code == 401:
        error_type = "authentication_error"
    elif status_code == 403:
        error_type = "permission_error"
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "message": _redact_text(str(message or "Request failed")),
                "type": error_type,
                "param": param,
                "code": code,
            }
        },
    )


def _http_error_code(status_code: int) -> str:
    return {
        400: "invalid_request",
        401: "invalid_api_key",
        403: "permission_denied",
        404: "not_found",
        409: "conflict",
        429: "rate_limit_exceeded",
        503: "service_unavailable",
    }.get(status_code, "server_error" if status_code >= 500 else "invalid_request")


def install_conversation_provider_routes(
    app,
    *,
    store: Store,
    service: WorkersProjectsService,
    provider_token: str,
    provider_principal_id: str = "glasshive-local",
    provider_tenant_id: str = "local",
    trust_identity_headers: bool = False,
    allow_full_access: bool = False,
    default_access: Literal["full", "workspace"] = "workspace",
) -> ConversationProvider:
    provider = ConversationProvider(store, service)
    auth_context = ProviderAuthContext(
        tenant_id=str(provider_tenant_id or "local").strip() or "local",
        principal_id=str(provider_principal_id or "glasshive-local").strip() or "glasshive-local",
        trust_identity_headers=bool(trust_identity_headers),
        allow_full_access=bool(allow_full_access),
        default_access="full" if default_access == "full" else "workspace",
    )

    @app.exception_handler(HTTPException)
    async def provider_http_exception_handler(request: Request, exc: HTTPException):
        if not _is_provider_path(request.url.path):
            return await http_exception_handler(request, exc)
        return _openai_error(
            exc.status_code,
            str(exc.detail),
            _http_error_code(exc.status_code),
        )

    @app.exception_handler(RequestValidationError)
    async def provider_validation_exception_handler(request: Request, exc: RequestValidationError):
        if not _is_provider_path(request.url.path):
            return await request_validation_exception_handler(request, exc)
        errors = exc.errors()
        extra = next((error for error in errors if error.get("type") == "extra_forbidden"), None)
        if extra is not None:
            param = str(extra.get("loc", [""])[-1] or "")
            return _openai_error(
                400,
                f"Unsupported parameter '{param}'",
                "unsupported_parameter",
                param=param,
            )
        return _openai_error(400, "Invalid chat completion request", "invalid_request")

    def require_provider_auth(request: Request) -> ProviderAuthContext:
        expected = str(provider_token or "").strip()
        if not expected:
            raise HTTPException(
                status_code=503,
                detail="GlassHive provider authentication is not configured",
            )
        auth_header = str(request.headers.get("authorization") or "")
        supplied = auth_header.removeprefix("Bearer ").strip() if auth_header.startswith("Bearer ") else ""
        if not supplied or not hmac.compare_digest(supplied, expected):
            raise HTTPException(status_code=401, detail="Unauthorized GlassHive provider request")
        return auth_context

    def owner_for_request(request: Request, auth: ProviderAuthContext) -> str:
        asserted = _header(request, "x-viventium-user-id")
        if asserted and asserted != auth.principal_id and not auth.trust_identity_headers:
            raise HTTPException(status_code=403, detail="Provider credential cannot delegate another owner")
        return asserted if asserted and auth.trust_identity_headers else auth.principal_id

    @app.get("/v1/models")
    def glasshive_models(request: Request) -> dict[str, Any]:
        require_provider_auth(request)
        return provider.models_payload()

    @app.post("/v1/chat/completions")
    async def glasshive_chat_completions(payload: ChatCompletionRequest, request: Request):
        auth = require_provider_auth(request)
        payload = _hydrate_metadata(payload, request, auth)
        record = await asyncio.to_thread(provider.start, payload, tenant_id=auth.tenant_id)
        if payload.stream:
            return StreamingResponse(
                provider.stream(record, payload, request),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache, no-transform",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                    "X-GlassHive-Request-Id": str(record["request_id"]),
                },
            )
        record, run = await asyncio.to_thread(provider.wait, str(record["request_id"]))
        return JSONResponse(provider.response_payload(record, run, payload))

    @app.get("/v1/requests/{request_id}/activity")
    async def glasshive_activity(request_id: str, request: Request):
        auth = require_provider_auth(request)
        provider.assert_request_owner(request_id, owner_for_request(request, auth))
        raw_after = str(request.headers.get("last-event-id") or "0").strip()
        try:
            after = max(0, int(raw_after))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Last-Event-ID must be a monotonic integer") from exc
        if "text/event-stream" not in str(request.headers.get("accept") or ""):
            return await asyncio.to_thread(
                provider.activity_payload,
                request_id,
                after_sequence=after,
            )

        async def event_stream():
            cursor = after
            last_heartbeat = time.monotonic()
            while True:
                if await request.is_disconnected():
                    return
                payload = await asyncio.to_thread(
                    provider.activity_payload,
                    request_id,
                    after_sequence=cursor,
                )
                for event in payload["data"]:
                    cursor = int(event["id"])
                    yield f"id: {cursor}\nevent: {event['event']}\ndata: {json.dumps(event, separators=(',', ':'))}\n\n"
                record = store.get_provider_request(request_id)
                if record and record["state"] in TERMINAL_REQUEST_STATES and not payload["data"]:
                    return
                if time.monotonic() - last_heartbeat >= 15:
                    yield ": heartbeat\n\n"
                    last_heartbeat = time.monotonic()
                await asyncio.sleep(0.1)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    @app.post("/v1/requests/{request_id}/cancel")
    def glasshive_cancel(request_id: str, request: Request) -> dict[str, Any]:
        auth = require_provider_auth(request)
        provider.assert_request_owner(request_id, owner_for_request(request, auth))
        record = provider.cancel(request_id)
        return {"id": request_id, "object": "glasshive.request", "state": record["state"]}

    @app.post("/v1/requests/by-idempotency/{idempotency_key}/cancel")
    def glasshive_cancel_by_idempotency(
        idempotency_key: str,
        request: Request,
    ) -> dict[str, Any]:
        auth = require_provider_auth(request)
        record = provider.cancel_by_idempotency(
            idempotency_key,
            owner_for_request(request, auth),
            tenant_id=auth.tenant_id,
        )
        return {
            "id": str(record["request_id"]),
            "object": "glasshive.request",
            "state": record["state"],
        }

    return provider
