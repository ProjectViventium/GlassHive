from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import shutil
import subprocess
import time
import base64
import binascii
import asyncio
import copy
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Literal
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .agent_builder_control import (
    LC_TRANSFER_TO_PREFIX,
    graph_transfer_control,
    parse_graph_transfer_output,
)
from .profile_runtime import (
    _claude_host_auth_available,
    _host_codex_conversation_project_instructions,
    _host_native_web_access,
    _host_codex_personality_policy_state,
    _host_plugin_denylist,
    _redact_text,
)
from .service import WorkersProjectsService
from .store import ProviderFamilyStoppedError, Store


TERMINAL_RUN_STATES = {"completed", "failed", "cancelled", "interrupted"}
TERMINAL_REQUEST_STATES = {"completed", "failed", "cancelled"}
SERIAL_FALLBACK_CLAIM_TIMEOUT_SEC = 120
PROVIDER_RESPONSE_DEADLINE_FAILURE_CLASS = "provider_response_deadline_exceeded"
BOOTSTRAP_BUNDLE_MAX_ENCODED_BYTES = 128 * 1024
BOOTSTRAP_SIGNATURE_MAX_AGE_SEC = 300
ACTIVITY_SUMMARIES = {
    "queued": "GlassHive queued the conversation turn.",
    "started": "The harness started working.",
    "reasoning-summary": "The harness updated its reasoning summary.",
    "plan": "The harness updated its plan.",
    "tool": "The harness used a tool.",
    "file": "The harness worked with a file.",
    "waiting": "The harness is waiting for capacity or a prerequisite.",
    "fallback": "GlassHive switched to the configured fallback model before authoring began.",
    "completed": "The harness completed the turn.",
    "failed": "The harness could not complete the turn.",
    "cancelled": "The harness turn was cancelled.",
}


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
                "automatic_fallback_target": True,
                "serial_model_fallback": True,
                "workspace_binding": True,
                "conversation_session": True,
                "worker_native_tools": True,
                "host_tools_transport": "broker_mcp",
                "activity_stream": True,
            },
        }


@dataclass(frozen=True)
class DeferredFallbackStart:
    request_record: dict[str, Any]
    run: dict[str, Any]


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
        display_name="Claude / Opus 5",
        harness_profile="claude-code",
        native_model="opus",
        effort_choices=("low", "medium", "high", "xhigh", "max"),
        recommended_effort="high",
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
        return _claude_host_auth_available(_configured_binary(profile))
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

    mode: Literal["life", "custom"] = "life"
    path: str | None = None

    @model_validator(mode="after")
    def validate_custom_path(self):
        if self.mode == "custom" and not str(self.path or "").strip():
            raise ValueError("A custom GlassHive workspace requires a server-side path")
        return self


class GlassHiveOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace: WorkspaceBinding = Field(default_factory=WorkspaceBinding)
    access: Literal["full", "workspace", "read_only"] = "full"


class CompletionMetadata(BaseModel):
    model_config = ConfigDict(extra="allow")

    owner_id: str = Field(min_length=1)
    conversation_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    message_id: str = ""
    stream_id: str = ""
    surface: str = "web"
    input_mode: str = "text"
    idempotency_key: str = ""
    glasshive_options: GlassHiveOptions = Field(default_factory=GlassHiveOptions)
    bootstrap_bundle: dict[str, Any] = Field(default_factory=dict)
    developer_instruction_tail: str = ""
    turn_context: str = Field(default="", max_length=16 * 1024)
    fallback_model: str = ""
    fallback_reasoning_effort: str = ""
    response_timeout_s: float | None = Field(default=None, gt=0)


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: str
    content: Any = ""


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False
    metadata: CompletionMetadata | None = None
    reasoning_effort: str | None = None
    tools: list[dict[str, Any]] | None = Field(default=None, max_length=128)
    tool_choice: Any = None


_PRIVATE_CITATION_REF_PATTERN = r"turn\d+[A-Za-z_][A-Za-z0-9_-]*?\d+"
_PRIVATE_CITATION_ANCHOR_PATTERN = (
    rf"(?:\\u[eE]202|\ue202)(?:{_PRIVATE_CITATION_REF_PATTERN})"
)
_PRIVATE_CITATION_RUN_RE = re.compile(rf"(?:{_PRIVATE_CITATION_ANCHOR_PATTERN})+")
_PRIVATE_CITATION_ANCHOR_RE = re.compile(
    rf"(?:\\u[eE]202|\ue202)({_PRIVATE_CITATION_REF_PATTERN})"
)
_PRIVATE_CITATION_WRAPPER_RE = re.compile(r"(?:\\u[eE]20[0-4]|[\ue200-\ue204])")


def _citation_source_map(sources: Iterable[dict[str, Any]]) -> dict[str, tuple[str, str]]:
    mapped: dict[str, tuple[str, str]] = {}
    for source in sources:
        if not isinstance(source, dict):
            continue
        ref_id = str(source.get("ref_id") or "").strip()
        url = str(source.get("url") or "").strip()
        try:
            parsed_url = urlsplit(url)
        except ValueError:
            continue
        if (
            not re.fullmatch(_PRIVATE_CITATION_REF_PATTERN, ref_id)
            or parsed_url.scheme not in {"http", "https"}
            or not parsed_url.netloc
        ):
            continue
        if any(character.isspace() or ord(character) < 32 for character in url):
            continue
        raw_title = str(source.get("title") or parsed_url.netloc)
        title = " ".join(raw_title.split())
        mapped[ref_id] = (title[:300] or parsed_url.netloc, url)
    return mapped


def _truncate_invalid_control_fragments(value: str) -> str:
    """Keep valid prose before a terminal-control fragment and discard the unsafe suffix."""

    normalized = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    safe_lines: list[str] = []
    for line in normalized.split("\n"):
        invalid_at = next(
            (
                index
                for index, character in enumerate(line)
                if (ord(character) < 32 and character != "\t")
                or 127 <= ord(character) <= 159
            ),
            -1,
        )
        safe_lines.append((line[:invalid_at] if invalid_at >= 0 else line).rstrip())
    return "\n".join(safe_lines)


def _sanitize_user_visible_text(
    value: str,
    citation_sources: Iterable[dict[str, Any]] = (),
) -> str:
    """Render native provenance when available and remove provider-private artifacts."""

    text = _truncate_invalid_control_fragments(value)
    sources = _citation_source_map(citation_sources)

    def render_citation_run(match: re.Match[str]) -> str:
        links: list[str] = []
        seen_urls: set[str] = set()
        for anchor in _PRIVATE_CITATION_ANCHOR_RE.finditer(match.group(0)):
            source = sources.get(anchor.group(1))
            if not source or source[1] in seen_urls:
                continue
            seen_urls.add(source[1])
            title = source[0].replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
            url = source[1].replace("\\", "%5C").replace(")", "%29")
            links.append(f"[{title}]({url})")
        return " ".join(links)

    text = _PRIVATE_CITATION_RUN_RE.sub(render_citation_run, text)
    text = _PRIVATE_CITATION_WRAPPER_RE.sub("", text)
    return re.sub(r"[ \t]+\n", "\n", text)


def _sanitize_provider_output(
    value: str,
    citation_sources: Iterable[dict[str, Any]] = (),
) -> str:
    """Sanitize plain completions and structured Agent Builder envelopes alike."""

    raw = str(value or "")
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return _sanitize_user_visible_text(raw, citation_sources).strip()
    if not isinstance(parsed, dict) or not isinstance(parsed.get("content"), str):
        return _sanitize_user_visible_text(raw, citation_sources).strip()
    parsed["content"] = _sanitize_user_visible_text(
        parsed["content"], citation_sources
    ).strip()
    return json.dumps(parsed, separators=(",", ":"), ensure_ascii=False)


class StreamingRedactor:
    """Redact complete logical lines without retaining an unbounded provider stream."""

    def __init__(self, overlap: int = 1024, max_buffer: int = 64 * 1024) -> None:
        self.overlap = max(1, int(overlap))
        self.max_buffer = max(self.overlap, int(max_buffer))
        self._buffer = ""

    def feed(
        self,
        value: str,
        citation_sources: Iterable[dict[str, Any]] = (),
    ) -> str:
        self._buffer += str(value or "")
        newline = self._buffer.rfind("\n")
        if newline < 0:
            if len(self._buffer) > self.max_buffer:
                self._buffer = ""
                return "[REDACTED_OVERSIZED_STREAM_SEGMENT]"
            return ""
        stable = self._buffer[: newline + 1]
        self._buffer = self._buffer[newline + 1 :]
        return _redact_text(_sanitize_user_visible_text(stable, citation_sources))

    def flush(self, citation_sources: Iterable[dict[str, Any]] = ()) -> str:
        result = _redact_text(_sanitize_user_visible_text(self._buffer, citation_sources))
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


def _developer_instruction_snapshot(payload: ChatCompletionRequest) -> str:
    snapshot = _system_snapshot(payload.messages)
    tail = str(payload.metadata.developer_instruction_tail or "").strip()
    if not tail:
        return snapshot
    if tail not in snapshot:
        raise HTTPException(
            status_code=400,
            detail="Declared developer instruction tail is absent from authority messages",
        )
    without_tail = "\n\n".join(
        part.strip() for part in snapshot.split(tail) if part.strip()
    )
    return "\n\n".join(part for part in (without_tail, tail) if part)


def _history_instruction(
    messages: Iterable[ChatMessage], *, start_at: int = 0, turn_context: str = ""
) -> str:
    all_messages = list(messages)
    selected = [
        message
        for index, message in enumerate(all_messages)
        if index >= max(0, start_at)
        and str(message.role or "").strip().lower() not in {"system", "developer"}
    ]
    if not selected:
        return "Continue the current conversation naturally."
    lines = [
        "Continue this Viventium conversation naturally.",
        "Ask a concise clarifying question when the user's desired outcome genuinely cannot be inferred.",
        "Before any destructive, irreversible, externally consequential, or permission-expanding action, verify that it is explicitly within the user's request and pause for approval when it is not.",
    ]
    if turn_context.strip():
        lines.extend(["Current runtime context:", turn_context.strip()])
    lines.append("Visible conversation context:")
    for message in selected:
        role = str(message.role or "user").strip().lower()
        text = _message_text(message.content).strip()
        if text:
            lines.append(f"[{role}]\n{text}")
    return "\n\n".join(lines).strip()


def _native_policy_state(model: HarnessModel) -> dict[str, Any]:
    state: dict[str, Any] = {
        "plugin_denylist": list(_host_plugin_denylist()),
        "native_web_access": _host_native_web_access(),
    }
    if model.harness_profile == "codex-cli":
        state["codex_personality"] = _host_codex_personality_policy_state()
        state["codex_conversation_project_instructions"] = (
            _host_codex_conversation_project_instructions()
        )
    return state


def _native_policy_sha256(model: HarnessModel) -> str:
    payload = json.dumps(
        _native_policy_state(model),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _native_policy_is_default(model: HarnessModel) -> bool:
    state = _native_policy_state(model)
    return (
        not state["plugin_denylist"]
        and state["native_web_access"] == "inherit"
        and state.get("codex_personality", "inherit") == "inherit"
        and state.get("codex_conversation_project_instructions", "inherit")
        == "inherit"
    )


def _canonical_life_dir() -> Path:
    configured = str(os.environ.get("VIVENTIUM_LIFE_DIR") or "").strip()
    return Path(configured or "~/Documents/Viventium/Life").expanduser().resolve()


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
    if len(encoded) > BOOTSTRAP_BUNDLE_MAX_ENCODED_BYTES:
        raise HTTPException(status_code=400, detail="GlassHive bootstrap bundle is too large")
    timestamp = _header(request, "x-glasshive-bootstrap-timestamp")
    signature = _header(request, "x-glasshive-bootstrap-signature")
    secret = str(os.environ.get("VIVENTIUM_GLASSHIVE_CAPABILITY_BROKER_SECRET") or "").strip()
    if not timestamp or not signature or not secret:
        raise HTTPException(status_code=401, detail="GlassHive bootstrap signature is required")
    try:
        issued_at = int(timestamp)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Invalid GlassHive bootstrap timestamp") from exc
    if abs(int(time.time()) - issued_at) > BOOTSTRAP_SIGNATURE_MAX_AGE_SEC:
        raise HTTPException(status_code=401, detail="Expired GlassHive bootstrap signature")
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"),
        f"v1\n{timestamp}\n{encoded}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(status_code=401, detail="Invalid GlassHive bootstrap signature")
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
        payload = json.loads(decoded)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid GlassHive bootstrap bundle header") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="GlassHive bootstrap bundle must be an object")
    return payload


def _decode_developer_instruction_tail(request: Request) -> str:
    encoded = _header(request, "x-glasshive-developer-instruction-tail-b64")
    if not encoded:
        return ""
    if len(encoded) > 128 * 1024:
        raise HTTPException(status_code=400, detail="GlassHive developer instruction tail is too large")
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise HTTPException(
            status_code=400,
            detail="Invalid GlassHive developer instruction tail header",
        ) from exc
    return decoded.strip()


def _decode_turn_context(request: Request) -> str:
    encoded = _header(request, "x-glasshive-turn-context-b64")
    if not encoded:
        return ""
    if len(encoded) > 32 * 1024:
        raise HTTPException(status_code=400, detail="GlassHive turn context is too large")
    try:
        return base64.b64decode(encoded, validate=True).decode("utf-8").strip()
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid GlassHive turn context header") from exc


def _hydrate_metadata(payload: ChatCompletionRequest, request: Request) -> ChatCompletionRequest:
    """Merge trusted per-request/provider headers into the OpenAI-compatible request shape."""

    incoming = payload.metadata.model_dump(mode="python") if payload.metadata is not None else {}
    asserted_owner = _header(request, "x-viventium-user-id")
    incoming_owner = str(incoming.get("owner_id") or "").strip()
    if asserted_owner and incoming_owner and asserted_owner != incoming_owner:
        raise HTTPException(status_code=403, detail="Authenticated owner does not match completion metadata")

    metadata = {
        **incoming,
        "owner_id": asserted_owner or incoming_owner,
        "conversation_id": _header(request, "x-viventium-conversation-id")
        or str(incoming.get("conversation_id") or "").strip(),
        "agent_id": _header(request, "x-glasshive-agent-id")
        or str(incoming.get("agent_id") or "").strip(),
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
        "developer_instruction_tail": _decode_developer_instruction_tail(request)
        or str(incoming.get("developer_instruction_tail") or "").strip(),
        "turn_context": _decode_turn_context(request)
        or str(incoming.get("turn_context") or "").strip(),
        "fallback_model": _header(request, "x-glasshive-fallback-model")
        or str(incoming.get("fallback_model") or "").strip(),
        "fallback_reasoning_effort": _header(
            request,
            "x-glasshive-fallback-reasoning-effort",
        )
        or str(incoming.get("fallback_reasoning_effort") or "").strip(),
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
    if access:
        options["access"] = access
    metadata["glasshive_options"] = options

    try:
        hydrated = CompletionMetadata.model_validate(metadata)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return payload.model_copy(update={"metadata": hydrated})


def _resolve_workspace(options: GlassHiveOptions) -> Path:
    path = _canonical_life_dir() if options.workspace.mode == "life" else Path(str(options.workspace.path)).expanduser()
    if options.workspace.mode == "custom" and not path.is_absolute():
        raise HTTPException(
            status_code=400,
            detail="Custom GlassHive workspace must be an absolute server-side path",
        )
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError as exc:
        label = "Viventium LIFE" if options.workspace.mode == "life" else "Custom GlassHive workspace"
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
                detail="Custom GlassHive workspace is outside the configured Viventium workspace roots",
            )
    return resolved


def _base_idempotency_key(payload: ChatCompletionRequest) -> str:
    explicit = str(payload.metadata.idempotency_key or payload.metadata.message_id or "").strip()
    if explicit:
        return explicit
    canonical = json.dumps(
        {
            "owner": payload.metadata.owner_id,
            "conversation": payload.metadata.conversation_id,
            "agent": payload.metadata.agent_id,
            "model": payload.model,
            "messages": [message.model_dump(mode="json") for message in payload.messages],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _idempotency_key(payload: ChatCompletionRequest) -> str:
    base_key = _base_idempotency_key(payload)
    control = graph_transfer_control(payload.tools, payload.tool_choice)
    if not control or not str(payload.metadata.idempotency_key or "").strip():
        return base_key
    model = GLASSHIVE_MODELS.get(str(payload.model or "").strip())
    reasoning_effort = str(
        payload.reasoning_effort
        or (model.recommended_effort if model else "")
    ).strip().lower()
    raw_tool_choice = payload.tool_choice
    if raw_tool_choice is None or (
        isinstance(raw_tool_choice, str)
        and raw_tool_choice.strip().lower() == "auto"
    ):
        normalized_tool_choice: Any = "auto"
    elif isinstance(raw_tool_choice, str):
        normalized_tool_choice = raw_tool_choice.strip().lower()
    elif isinstance(raw_tool_choice, dict):
        function = raw_tool_choice.get("function")
        normalized_tool_choice = {
            "type": str(raw_tool_choice.get("type") or "").strip().lower(),
            "function": {
                "name": str(
                    function.get("name") if isinstance(function, dict) else ""
                ).strip()
            },
        }
    else:
        normalized_tool_choice = str(raw_tool_choice)
    canonical = json.dumps(
        {
            "messages": [message.model_dump(mode="json") for message in payload.messages],
            "model": str(payload.model or "").strip(),
            "reasoning_effort": reasoning_effort,
            "tool_choice": normalized_tool_choice,
            "transfer_tools": [tool["name"] for tool in control["tools"]],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    execution_digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20]
    return f"{base_key}:graph:{execution_digest}"


def _completed_graph_transfer_names(
    store: Store,
    records: Iterable[dict[str, Any]],
    *,
    before_created_at: str = "",
) -> set[str]:
    """Return structurally valid transfers already selected in this agent/turn family."""

    selected: set[str] = set()
    for record in records:
        created_at = str(record.get("created_at") or "")
        if before_created_at and created_at >= before_created_at:
            continue
        run = store.get_run(str(record.get("run_id") or "")) or {}
        if str(run.get("state") or "") != "completed":
            continue
        try:
            output = json.loads(str(run.get("output_text") or ""))
        except json.JSONDecodeError:
            continue
        if not isinstance(output, dict) or output.get("type") != "tool_call":
            continue
        tool_name = str(output.get("tool_name") or "").strip()
        if tool_name.startswith(LC_TRANSFER_TO_PREFIX):
            selected.add(tool_name)
    return selected


def _without_completed_graph_transfers(
    tools: list[dict[str, Any]] | None,
    completed_names: set[str],
) -> list[dict[str, Any]] | None:
    if not tools or not completed_names:
        return tools
    filtered: list[dict[str, Any]] = []
    for tool in tools:
        function = tool.get("function") if isinstance(tool, dict) else None
        name = str(function.get("name") if isinstance(function, dict) else "").strip()
        if (
            tool.get("type") == "function"
            and name.startswith(LC_TRANSFER_TO_PREFIX)
            and name in completed_names
        ):
            continue
        filtered.append(tool)
    return filtered


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
    result_parts: list[str] = []
    structured_parts: list[str] = []
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
        if event.get("type") == "result":
            structured = event.get("structured_output", event.get("structuredOutput"))
            if isinstance(structured, dict):
                structured_parts.append(
                    json.dumps(structured, sort_keys=True, separators=(",", ":"))
                )
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
        if structured_parts:
            return structured_parts[-1]
        return "".join(assistant_parts) or (result_parts[-1] if result_parts else "")
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


def _public_connected_tool_task(value: Any) -> str:
    """Return a bounded product-language operation without broker/provider plumbing."""

    raw = value if isinstance(value, str) else ""
    candidate = raw.strip()
    if not candidate:
        return "connected operation"
    if "_mcp_" in candidate:
        candidate = candidate.split("_mcp_", 1)[0]
    for delimiter in ("__", "/", ":"):
        if delimiter in candidate:
            candidate = candidate.rsplit(delimiter, 1)[-1]
    candidate = re.sub(r"[^A-Za-z0-9]+", " ", candidate).strip().lower()
    if not candidate:
        return "connected operation"
    return candidate[:80].rstrip()


def _connected_tool_activity_summary(task: str, status: str) -> str:
    action = {
        "started": "invoked",
        "running": "running",
        "completed": "completed",
        "failed": "failed",
        "cancelled": "cancelled",
    }.get(status, "used")
    return f"Connected tool {action}: {task}."


def _native_tool_result_failed(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    if "Err" in result or "err" in result:
        return True
    ok = result.get("Ok", result.get("ok"))
    envelope = ok if isinstance(ok, dict) else result
    structured = envelope.get("structured_content", envelope.get("structuredContent"))
    structured = structured if isinstance(structured, dict) else {}
    failure_statuses = {"blocked", "cancelled", "denied", "error", "failed", "rejected"}
    envelope_status = str(envelope.get("status") or "").strip().lower()
    structured_status = str(structured.get("status") or "").strip().lower()
    if envelope_status in failure_statuses or structured_status in failure_statuses:
        return True
    if (
        envelope.get("isError") is True
        or envelope.get("is_error") is True
        or envelope.get("success") is False
        or structured.get("isError") is True
        or structured.get("is_error") is True
        or structured.get("success") is False
    ):
        return True
    if (
        envelope.get("isError") is False
        or envelope.get("is_error") is False
        or envelope.get("success") is True
        or structured.get("isError") is False
        or structured.get("is_error") is False
        or structured.get("success") is True
    ):
        return False
    if (
        ("error" in envelope and envelope.get("error") not in (None, "", False))
        or ("error" in structured and structured.get("error") not in (None, "", False))
    ):
        return True
    for block in envelope.get("content") or []:
        if not isinstance(block, dict) or str(block.get("type") or "") != "text":
            continue
        text = str(block.get("text") or "")
        if (
            re.search(r"\b\d+ validation errors? for call\b", text, re.IGNORECASE)
            and re.search(r"\[type=[a-z_]+", text, re.IGNORECASE)
            and "errors.pydantic.dev/" in text
        ):
            return True
    return False


def _normalized_harness_activity(profile: str, stdout: str) -> list[dict[str, Any]]:
    """Convert native JSONL into safe observable steps, never model chain-of-thought or tool inputs."""

    normalized: list[dict[str, Any]] = []
    claude_tool_tasks: dict[str, str] = {}
    claude_terminal_tool_calls: set[str] = set()
    codex_terminal_tool_calls: set[str] = set()
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
            f"{absolute_offset}:".encode("utf-8") + raw_line.encode("utf-8")
        ).hexdigest()[:20]
        absolute_offset += len(raw_segment.encode("utf-8"))

        if profile == "codex-cli":
            if str(event.get("type") or "") == "event_msg":
                payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
                payload_type = str(payload.get("type") or "").strip().lower()
                if payload_type in {"mcp_tool_call_begin", "mcp_tool_call_end"}:
                    invocation = (
                        payload.get("invocation")
                        if isinstance(payload.get("invocation"), dict)
                        else {}
                    )
                    task = _public_connected_tool_task(
                        invocation.get("tool") or payload.get("tool")
                    )
                    call_id = str(payload.get("call_id") or "").strip()
                    status = (
                        "failed"
                        if payload_type == "mcp_tool_call_end"
                        and _native_tool_result_failed(payload.get("result"))
                        else "completed"
                        if payload_type == "mcp_tool_call_end"
                        else "started"
                    )
                    if status in {"completed", "failed", "cancelled"} and call_id:
                        if call_id in codex_terminal_tool_calls:
                            continue
                        codex_terminal_tool_calls.add(call_id)
                    normalized.append(
                        {
                            "event_type": "tool",
                            "summary": _connected_tool_activity_summary(task, status),
                            "payload": {
                                "source_event_id": f"codex-cli:{source_line_id}:0",
                                "tool": "connected_tool",
                                "task": task,
                                "status": status,
                            },
                        }
                    )
                continue
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
                task = _public_connected_tool_task(item.get("tool") or item.get("name"))
                status = (
                    "failed"
                    if _native_tool_result_failed(item.get("result"))
                    else _activity_status(item.get("status"))
                )
                if not status:
                    status = "failed" if item.get("error") else "completed"
                summary = _connected_tool_activity_summary(task, status)
                payload["tool"] = "connected_tool"
                payload["task"] = task
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
                if payload.get("tool") == "connected_tool" and payload.get("status") in {
                    "completed",
                    "failed",
                    "cancelled",
                }:
                    call_id = str(item.get("call_id") or item.get("id") or "").strip()
                    if call_id:
                        if call_id in codex_terminal_tool_calls:
                            continue
                        codex_terminal_tool_calls.add(call_id)
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

        if profile != "claude-code":
            continue
        native_event_type = str(event.get("type") or "")
        message = event.get("message") if isinstance(event.get("message"), dict) else {}
        content = message.get("content") if isinstance(message.get("content"), list) else []
        if native_event_type == "user":
            for content_index, block in enumerate(content):
                if not isinstance(block, dict) or str(block.get("type") or "") != "tool_result":
                    continue
                tool_use_id = str(block.get("tool_use_id") or "").strip()
                if tool_use_id and tool_use_id in claude_terminal_tool_calls:
                    continue
                task = claude_tool_tasks.get(tool_use_id, "connected operation")
                status = "failed" if block.get("is_error") is True else "completed"
                if tool_use_id:
                    claude_terminal_tool_calls.add(tool_use_id)
                normalized.append(
                    {
                        "event_type": "tool",
                        "summary": _connected_tool_activity_summary(task, status),
                        "payload": {
                            "source_event_id": f"claude-code:{source_line_id}:{content_index}",
                            "tool": "connected_tool",
                            "task": task,
                            "status": status,
                        },
                    }
                )
            continue
        if native_event_type != "assistant":
            continue
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
                task = _public_connected_tool_task(tool_name)
                summary = _connected_tool_activity_summary(task, "started")
                tool_category = "connected_tool"
                tool_use_id = str(block.get("id") or "").strip()
                if tool_use_id:
                    claude_tool_tasks[tool_use_id] = task
            normalized.append(
                {
                    "event_type": event_type,
                    "summary": summary,
                    "payload": {
                        "source_event_id": f"claude-code:{source_line_id}:{content_index}",
                        "tool": tool_category,
                        **(
                            {"task": task, "status": "started"}
                            if tool_category == "connected_tool"
                            else {}
                        ),
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
        # Broker bearers are invocation-local authority. Keep an ephemeral copy
        # only long enough for an immediate serial fallback; never persist it in
        # a worker, request, run, event, or provider-session row.
        self._request_local_bundles: dict[str, tuple[str, dict[str, Any]]] = {}
        self._apply_retention_policy()

    def _remember_request_local_bundle(
        self,
        request_id: str,
        run_id: str,
        bundle: dict[str, Any] | None,
    ) -> None:
        clean_request_id = str(request_id or "").strip()
        clean_run_id = str(run_id or "").strip()
        if not clean_request_id or not clean_run_id or not isinstance(bundle, dict):
            return
        with self._start_lock:
            self._request_local_bundles[clean_request_id] = (
                clean_run_id,
                copy.deepcopy(bundle),
            )

    def _request_local_bundle(
        self,
        request_id: str,
        *,
        expected_run_id: str,
    ) -> dict[str, Any] | None:
        with self._start_lock:
            entry = self._request_local_bundles.get(str(request_id or "").strip())
            if not entry or entry[0] != str(expected_run_id or "").strip():
                return None
            return copy.deepcopy(entry[1])

    def _forget_request_local_bundle(self, request_id: str) -> None:
        with self._start_lock:
            self._request_local_bundles.pop(str(request_id or "").strip(), None)

    def _apply_retention_policy(self) -> None:
        """Bound private provider state without ever pruning an active conversation turn."""

        try:
            request_days = self._configured_request_retention_days()
            session_days = max(
                request_days,
                int(os.environ.get("GLASSHIVE_PROVIDER_SESSION_RETENTION_DAYS", "90") or "90"),
            )
        except ValueError:
            request_days, session_days = 30, 90

        now = datetime.now(timezone.utc)
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
            return

    @staticmethod
    def _configured_request_retention_days() -> int:
        """Return the lifecycle that owns both request records and same-turn Stop fences."""

        return max(
            1,
            int(os.environ.get("GLASSHIVE_PROVIDER_REQUEST_RETENTION_DAYS", "30") or "30"),
        )

    def models_payload(self) -> dict[str, Any]:
        return {"object": "list", "data": [model.api_payload() for model in GLASSHIVE_MODELS.values()]}

    @staticmethod
    def _configured_response_timeout_seconds() -> float | None:
        raw = str(os.environ.get("GLASSHIVE_PROVIDER_RESPONSE_TIMEOUT_S") or "").strip()
        if not raw:
            return None
        try:
            value = float(raw)
        except ValueError as exc:
            raise HTTPException(
                status_code=503,
                detail="GLASSHIVE_PROVIDER_RESPONSE_TIMEOUT_S must be a positive number",
            ) from exc
        if not math.isfinite(value) or value <= 0:
            raise HTTPException(
                status_code=503,
                detail="GLASSHIVE_PROVIDER_RESPONSE_TIMEOUT_S must be a positive number",
            )
        return value

    def _response_timeout_seconds(self, payload: ChatCompletionRequest) -> float | None:
        configured = self._configured_response_timeout_seconds()
        requested = payload.metadata.response_timeout_s if payload.metadata else None
        if configured is None:
            return float(requested) if requested is not None else None
        return min(configured, float(requested)) if requested is not None else configured

    @staticmethod
    def _deadline_timestamp(
        timeout_seconds: float | None,
        *,
        started_at: datetime | None = None,
    ) -> str:
        if timeout_seconds is None:
            return ""
        return (
            (started_at or datetime.now(timezone.utc))
            + timedelta(seconds=float(timeout_seconds))
        ).isoformat()

    @staticmethod
    def _request_timeout_seconds(request_record: dict[str, Any]) -> float | None:
        try:
            stored_timeout = float(request_record.get("response_timeout_s"))
        except (TypeError, ValueError):
            stored_timeout = 0.0
        if stored_timeout > 0:
            return stored_timeout
        try:
            created_at = datetime.fromisoformat(str(request_record.get("created_at") or ""))
            deadline_at = datetime.fromisoformat(
                str(request_record.get("response_deadline_at") or "")
            )
        except ValueError:
            return None
        return max(0.0, (deadline_at - created_at).total_seconds())

    @staticmethod
    def _deadline_reached(request_record: dict[str, Any]) -> bool:
        raw = str(request_record.get("response_deadline_at") or "").strip()
        if not raw:
            return False
        try:
            deadline = datetime.fromisoformat(raw)
        except ValueError:
            return True
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) >= deadline

    def _deadline_arbitration_needed(
        self,
        request_record: dict[str, Any],
        *,
        timeout_seconds: float | None,
    ) -> bool:
        effective_timeout = self._request_timeout_seconds(request_record) or timeout_seconds
        if effective_timeout is None or effective_timeout <= 0:
            return False
        if not str(request_record.get("response_deadline_at") or "").strip():
            return True
        if self._deadline_reached(request_record):
            return True
        if str(request_record.get("state") or "") == "completed":
            return True
        run_id = str(request_record.get("run_id") or "").strip()
        run = self.store.get_run(run_id) if run_id else None
        return str((run or {}).get("state") or "") in TERMINAL_RUN_STATES

    def _arbitrate_deadline_if_needed(
        self,
        request_record: dict[str, Any],
        *,
        timeout_seconds: float,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if self._deadline_arbitration_needed(
            request_record,
            timeout_seconds=timeout_seconds,
        ):
            return self._expire_response_deadline(
                request_record,
                timeout_seconds=timeout_seconds,
            )
        run_id = str(request_record.get("run_id") or "").strip()
        run = self.store.get_run(run_id) if run_id else None
        return request_record, run or {}

    @staticmethod
    def _deadline_message(timeout_seconds: float | None) -> str:
        rendered = f"{float(timeout_seconds):g}" if timeout_seconds is not None else "configured"
        return (
            f"The GlassHive foreground response exceeded its {rendered}-second deadline. "
            "Native work was stopped; retry the same turn."
        )

    def _expire_response_deadline(
        self,
        request_record: dict[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Fail one provider turn durably, then stop only its exact native run."""

        request_id = str(request_record["request_id"])
        with self._start_lock:
            current = self.store.get_provider_request(request_id) or request_record
            effective_timeout = self._request_timeout_seconds(current)
            if effective_timeout is None or effective_timeout <= 0:
                effective_timeout = timeout_seconds
            if effective_timeout is None or effective_timeout <= 0:
                effective_timeout = self._configured_response_timeout_seconds()
            if effective_timeout is None or effective_timeout <= 0:
                run_id = str(current.get("run_id") or "").strip()
                run = self.store.get_run(run_id) if run_id else None
                return current, run or {}
            message = self._deadline_message(effective_timeout)
            recommended_recovery = (
                "Retry the same turn. If the timeout repeats, inspect the native provider "
                "and connected-tool health before increasing the foreground response budget."
            )
            diagnostic_summary = (
                "The provider response deadline expired before a terminal native result."
            )
            arbitration = self.store.arbitrate_provider_request_deadline(
                request_id,
                default_timeout_s=effective_timeout,
                failure_class=PROVIDER_RESPONSE_DEADLINE_FAILURE_CLASS,
                failure_user_message=message,
                failure_recommended_recovery=recommended_recovery,
                failure_diagnostic_summary=diagnostic_summary,
            )
            claimed = arbitration.get("request") or current
            run = arbitration.get("run") or {}
            if not arbitration.get("deadline_exceeded"):
                return claimed, run
            newly_expired = bool(arbitration.get("newly_expired"))
            run_id = str(claimed.get("run_id") or "").strip()
            session = self.store.get_provider_session_by_id(
                str(claimed.get("session_id") or "")
            )
            worker = (
                self.store.get_worker(str(session.get("worker_id") or ""))
                if session
                else None
            )

        # Native process teardown can take seconds on a stuck CLI. The durable
        # request/run terminal claims above fence late output; do not hold the
        # provider-wide start/cancel lock while waiting for process cleanup.
        cleanup_succeeded = False
        if newly_expired and worker and run_id:
            try:
                try:
                    self.service.runtime.interrupt_worker(worker, run_id=run_id)
                except TypeError as exc:
                    if "run_id" not in str(exc):
                        raise
                    self.service.runtime.interrupt_worker(worker)
                cleanup_succeeded = True
            except Exception:
                self.store.update_worker_state(
                    str(worker["worker_id"]),
                    "failed",
                    last_error="GlassHive could not confirm native deadline cleanup",
                )

        existing_types = (
            {
                str(item["event_type"])
                for item in self.store.list_provider_activity(request_id)
            }
            if newly_expired
            else set()
        )
        if newly_expired and "failed" not in existing_types:
            self.store.add_provider_activity(
                request_id,
                "failed",
                ACTIVITY_SUMMARIES["failed"],
                {
                    "failure_class": PROVIDER_RESPONSE_DEADLINE_FAILURE_CLASS,
                    "timeout_seconds": effective_timeout,
                    "native_cleanup": "accepted" if cleanup_succeeded else "unconfirmed",
                },
            )
        return claimed, run

    def deadline_error_payload(
        self,
        request_record: dict[str, Any],
        run: dict[str, Any],
    ) -> dict[str, Any]:
        timeout_seconds = self._request_timeout_seconds(request_record)
        return {
            "error": {
                "message": _redact_text(
                    str(run.get("failure_user_message") or self._deadline_message(timeout_seconds))
                ),
                "type": "glasshive_timeout_error",
                "code": PROVIDER_RESPONSE_DEADLINE_FAILURE_CLASS,
                "request_id": str(request_record["request_id"]),
                "timeout_seconds": timeout_seconds,
            }
        }

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

    def _fallback_selection(
        self,
        payload: ChatCompletionRequest,
        primary_model: HarnessModel,
    ) -> tuple[HarnessModel | None, str]:
        model_id = str(payload.metadata.fallback_model or "").strip()
        if not model_id:
            return None, ""
        fallback_model = self._model(model_id)
        if fallback_model.id == primary_model.id:
            raise HTTPException(
                status_code=400,
                detail="GlassHive fallback model must differ from the primary model",
            )
        effort = str(
            payload.metadata.fallback_reasoning_effort
            or fallback_model.recommended_effort
        ).strip().lower()
        if effort not in fallback_model.effort_choices:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Unsupported fallback effort '{effort}' for {fallback_model.id}; choose one of "
                    f"{', '.join(fallback_model.effort_choices)}."
                ),
            )
        return fallback_model, effort

    def _assert_owner(self, payload: ChatCompletionRequest, request: Request) -> None:
        asserted = str(request.headers.get("x-viventium-user-id") or "").strip()
        if not asserted:
            raise HTTPException(status_code=401, detail="X-Viventium-User-Id is required")
        if asserted != payload.metadata.owner_id:
            raise HTTPException(status_code=403, detail="Authenticated owner does not match completion metadata")

    def _native_bundle(
        self,
        payload: ChatCompletionRequest,
        model: HarnessModel,
        effort: str,
    ) -> dict[str, Any]:
        incoming = dict(payload.metadata.bootstrap_bundle or {})
        incoming_env = dict(incoming.get("env")) if isinstance(incoming.get("env"), dict) else {}
        # Core signs this bundle, but the conversation broker bearer is deliberately
        # invocation-local. Descriptor/config fields reference the env name and are
        # safe to persist; the bearer itself must never enter worker/session storage.
        incoming_env.pop("GLASSHIVE_CAPABILITY_BROKER_TOKEN", None)
        effort_env = (
            {"WPR_CODEX_CLI_REASONING_EFFORT": effort}
            if model.harness_profile == "codex-cli"
            else {"WPR_CLAUDE_CODE_EFFORT": effort}
        )
        try:
            agent_builder_control = graph_transfer_control(
                payload.tools,
                payload.tool_choice,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        bundle = {
            **incoming,
            "run_mode": "conversation",
            "provider_model": model.native_model,
            "access_mode": payload.metadata.glasshive_options.access,
            # Mutable Viventium authority stays in Codex's native developer role. It must never be
            # flattened into the user-authored conversation instruction.
            "developer_instructions": _developer_instruction_snapshot(payload),
            "env": {**incoming_env, **effort_env},
            "provider_capabilities": {
                "self_delegation": False,
                "worker_native_tools": True,
                "host_tools_transport": (
                    "broker_mcp"
                    if incoming.get("glasshive_capability_broker")
                    else "none"
                ),
                "host_tools": list(
                    (
                        incoming.get("glasshive_capability_broker")
                        if isinstance(incoming.get("glasshive_capability_broker"), dict)
                        else {}
                    ).get("allowed_host_tools")
                    or []
                ),
                "graph_control_transport": (
                    "openai_tool_call" if agent_builder_control else "none"
                ),
                "graph_control_tools": [
                    tool["name"] for tool in (agent_builder_control or {}).get("tools", [])
                ],
            },
        }
        if agent_builder_control:
            bundle["agent_builder_control"] = agent_builder_control
        else:
            bundle.pop("agent_builder_control", None)
        return bundle

    def _run_local_native_bundle(
        self,
        payload: ChatCompletionRequest,
        model: HarnessModel,
        effort: str,
    ) -> dict[str, Any] | None:
        incoming = payload.metadata.bootstrap_bundle or {}
        incoming_env = incoming.get("env") if isinstance(incoming.get("env"), dict) else {}
        bearer = str(incoming_env.get("GLASSHIVE_CAPABILITY_BROKER_TOKEN") or "").strip()
        if not bearer:
            return None
        bundle = self._native_bundle(payload, model, effort)
        bundle["env"] = {
            **(bundle.get("env") if isinstance(bundle.get("env"), dict) else {}),
            "GLASSHIVE_CAPABILITY_BROKER_TOKEN": bearer,
        }
        return bundle

    @staticmethod
    def _session_manifest(session: dict[str, Any] | None) -> dict[str, Any]:
        try:
            value = json.loads(str((session or {}).get("context_manifest_json") or "{}"))
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}

    def assert_request_owner(self, request_id: str, request: Request) -> dict[str, Any]:
        asserted = _header(request, "x-viventium-user-id")
        if not asserted:
            raise HTTPException(status_code=401, detail="X-Viventium-User-Id is required")
        record = self.store.get_provider_request(request_id)
        if not record:
            raise HTTPException(status_code=404, detail="GlassHive request not found")
        if str(record.get("owner_id") or "") != asserted:
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
            f"Viventium conversation {metadata.conversation_id}",
            "Persistent Viventium conversation session",
            model.harness_profile,
            tenant_id=tenant_id,
        )
        bundle = self._native_bundle(payload, model, effort)
        worker = self.service.create_worker(
            project_id=project["project_id"],
            owner_id=metadata.owner_id,
            name=f"Viventium {metadata.agent_id}",
            role="conversation-agent",
            profile=model.harness_profile,
            backend="",
            execution_mode="host",
            alias=f"conversation-{metadata.conversation_id}-{metadata.agent_id}",
            workspace_root=str(workspace),
            bootstrap_profile="viventium-conversation-v1",
            bootstrap_bundle=bundle,
            tenant_id=tenant_id,
            start_synchronously=False,
            _trusted_run_lane="conversation",
        )
        if str(worker.get("state") or "") == "failed":
            raise HTTPException(status_code=409, detail=str(worker.get("last_error") or "GlassHive harness is not ready"))
        worker = self.store.update_worker(
            str(worker["worker_id"]),
            model=model.native_model,
            workspace_dir=str(workspace),
        ) or worker
        session = self.store.upsert_provider_session(
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
                    _developer_instruction_snapshot(payload).encode("utf-8")
                ).hexdigest(),
                "native_policy_sha256": _native_policy_sha256(model),
            },
        )
        activated = self.service.activate_prepared_conversation_worker(
            str(worker["worker_id"])
        )
        if str(activated.get("state") or "") == "failed":
            raise HTTPException(
                status_code=409,
                detail=str(
                    activated.get("last_error")
                    or "GlassHive conversation harness is not ready"
                ),
            )
        return session

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
        current_policy_sha256 = _native_policy_sha256(model)
        previous_policy_sha256 = str(
            existing_manifest.get("native_policy_sha256") or ""
        ).strip()
        requested_system_snapshot = _developer_instruction_snapshot(payload)
        authority_update_present = bool(requested_system_snapshot)
        previous_system_sha256 = str(
            existing_manifest.get("system_snapshot_sha256") or ""
        ).strip()
        current_system_sha256 = (
            hashlib.sha256(requested_system_snapshot.encode("utf-8")).hexdigest()
            if authority_update_present
            else previous_system_sha256
            or hashlib.sha256(b"").hexdigest()
        )
        policy_changed = bool(
            existing
            and (
                previous_policy_sha256 != current_policy_sha256
                if previous_policy_sha256
                else not _native_policy_is_default(model)
            )
        )
        system_state_changed = bool(
            existing
            and authority_update_present
            and previous_system_sha256 != current_system_sha256
        )
        binding_changed = bool(
            existing
            and (
                existing["model_id"] != model.id
                or Path(str(existing["workspace_dir"])).resolve() != workspace
                or existing["access_mode"] != expected_access
                or not existing_worker
                or str(existing_worker.get("state") or "") in {"failed", "terminated"}
                or policy_changed
                or system_state_changed
            )
        )
        if existing and not binding_changed:
            bundle = self._native_bundle(payload, model, effort)
            if not authority_update_present and existing_worker:
                try:
                    existing_bundle = json.loads(
                        str(existing_worker.get("bootstrap_bundle_json") or "{}")
                    )
                except json.JSONDecodeError:
                    existing_bundle = {}
                bundle["developer_instructions"] = str(
                    existing_bundle.get("developer_instructions") or ""
                )
            self.store.update_worker(
                str(existing["worker_id"]),
                bootstrap_bundle_json=json.dumps(bundle, sort_keys=True),
                model=model.native_model,
                workspace_dir=str(workspace),
            )
            current_manifest = {
                **existing_manifest,
                "effort": effort,
                "system_snapshot_sha256": current_system_sha256,
                "native_policy_sha256": current_policy_sha256,
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

    def start(self, payload: ChatCompletionRequest, request: Request, *, tenant_id: str = "local") -> dict[str, Any]:
        response_started_at = datetime.now(timezone.utc)
        with self._start_lock:
            self._assert_owner(payload, request)
            model = self._model(payload.model)
            effort = self._effort(payload, model)
            response_timeout_seconds = self._response_timeout_seconds(payload)
            fallback_model, fallback_effort = self._fallback_selection(payload, model)
            try:
                base_idempotency_key = _base_idempotency_key(payload)
                idempotency_key = _idempotency_key(payload)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            if self.store.is_provider_stop_tombstone_active(
                tenant_id=tenant_id,
                owner_id=payload.metadata.owner_id,
                idempotency_keys=(idempotency_key, base_idempotency_key),
            ):
                raise HTTPException(
                    status_code=409,
                    detail="GlassHive request was cancelled before native execution started",
                )
            duplicate = self.store.get_provider_request(
                tenant_id=tenant_id,
                owner_id=payload.metadata.owner_id,
                idempotency_key=idempotency_key,
            )
            family = self.store.list_provider_requests_by_idempotency_family(
                tenant_id=tenant_id,
                owner_id=payload.metadata.owner_id,
                base_idempotency_key=base_idempotency_key,
            )
            completed_transfers = _completed_graph_transfer_names(
                self.store,
                family,
                before_created_at=str(duplicate.get("created_at") or "")
                if duplicate
                else "",
            )
            payload.tools = _without_completed_graph_transfers(
                payload.tools,
                completed_transfers,
            )
            if duplicate:
                duplicate_run_id = str(duplicate.get("run_id") or "")
                duplicate_run = (
                    self.store.get_run(duplicate_run_id)
                    if duplicate_run_id
                    else None
                )
                run_local_bundle = self._run_local_native_bundle(
                    payload, model, effort
                )
                # After restart the scheduler may have claimed the queued row
                # just before this retry arrives. The service accepts a bearer
                # for `running` only while that exact run is still in its
                # pre-provider admission wait; an actual provider turn can
                # never have authority changed underneath it.
                if (
                    duplicate_run
                    and str(duplicate_run.get("state") or "")
                    in {"queued", "running", "needs_input"}
                    and self.service.attach_run_local_bundle(
                        duplicate_run_id, run_local_bundle
                    )
                ):
                    self._remember_request_local_bundle(
                        str(duplicate["request_id"]),
                        duplicate_run_id,
                        run_local_bundle,
                    )
                    session = self.store.get_provider_session_by_id(
                        str(duplicate.get("session_id") or "")
                    )
                    if session:
                        duplicate_worker = self.store.get_worker(
                            str(session["worker_id"])
                        )
                        if duplicate_worker and str(
                            duplicate_worker.get("state") or ""
                        ) in {"created", "failed", "paused", "needs_input"}:
                            self.service.activate_prepared_conversation_worker(
                                str(session["worker_id"])
                            )
                        self.service.start_assigned_run(str(session["worker_id"]))
                return duplicate
            workspace = _resolve_workspace(payload.metadata.glasshive_options)
            session, new_native_session = self._session(
                payload,
                model,
                workspace,
                effort,
                tenant_id=tenant_id,
            )
            previous_history_count = int(session.get("history_count") or 0)
            start_at = (
                previous_history_count
                if not new_native_session and len(payload.messages) > previous_history_count
                else 0
            )
            instruction = _history_instruction(
                payload.messages,
                start_at=start_at,
                turn_context=payload.metadata.turn_context,
            )
            fallback_instruction = (
                _history_instruction(
                    payload.messages,
                    start_at=0,
                    turn_context=payload.metadata.turn_context,
                )
                if fallback_model
                else ""
            )
            try:
                request_record, created = self.store.create_provider_request(
                    tenant_id=tenant_id,
                    owner_id=payload.metadata.owner_id,
                    session_id=session["session_id"],
                    idempotency_key=idempotency_key,
                    message_id=payload.metadata.message_id,
                    stream_id=payload.metadata.stream_id,
                    requested_history_count=len(payload.messages),
                    fallback_model_id=fallback_model.id if fallback_model else "",
                    fallback_reasoning_effort=fallback_effort,
                    fallback_instruction=fallback_instruction,
                    response_timeout_s=response_timeout_seconds,
                    response_deadline_at=self._deadline_timestamp(
                        response_timeout_seconds,
                        started_at=response_started_at,
                    ),
                    base_idempotency_key=base_idempotency_key,
                )
            except ProviderFamilyStoppedError as exc:
                if new_native_session:
                    worker_id = str(session.get("worker_id") or "")
                    if worker_id:
                        self.service.terminate_worker(worker_id)
                    self.store.delete_provider_sessions([str(session["session_id"])])
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            if not created:
                return request_record
            self.store.add_provider_activity(
                request_record["request_id"],
                "queued",
                ACTIVITY_SUMMARIES["queued"],
                {"surface": payload.metadata.surface, "input_mode": payload.metadata.input_mode},
            )
            if self._deadline_reached(request_record):
                expired, _ = self._expire_response_deadline(request_record)
                return expired
            run_local_bundle = self._run_local_native_bundle(
                payload, model, effort
            )
            try:
                run = self.service.assign_run(
                    str(session["worker_id"]),
                    instruction,
                    start_processor=False,
                    run_local_bundle=run_local_bundle,
                )
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
            attached = self.store.update_provider_request_if_state(
                request_record["request_id"],
                ("queued", "running"),
                run_id=run["run_id"],
                state="queued",
            )
            if not attached:
                self.store.finalize_run_if_state(
                    str(run["run_id"]),
                    "queued",
                    "cancelled",
                    error_text="Cancelled before native execution started",
                )
                self.store.finalize_schedule_for_run(
                    str(run["run_id"]),
                    state="cancelled",
                    last_error="Cancelled before native execution started",
                )
                self.service.discard_run_local_bundle(str(run["run_id"]))
                return (
                    self.store.get_provider_request(request_record["request_id"])
                    or request_record
                )
            self._remember_request_local_bundle(
                str(request_record["request_id"]),
                str(run["run_id"]),
                run_local_bundle,
            )
            self.service.start_assigned_run(str(session["worker_id"]))
            return attached

    def _serial_fallback_eligible(
        self,
        request_record: dict[str, Any],
        run: dict[str, Any],
        activity_types: set[str],
    ) -> bool:
        fallback_model_id = str(request_record.get("fallback_model_id") or "").strip()
        if (
            not fallback_model_id
            or str(request_record.get("fallback_state") or "")
            or str(request_record.get("state") or "") == "cancelled"
            or str(run.get("state") or "") != "failed"
            or str(run.get("failure_class") or "")
            not in {"provider_rate_limited", "provider_quota_exhausted"}
            or not bool(run.get("failure_retryable"))
            or not bool(run.get("failure_structured"))
            or int(run.get("retry_attempts") or 0) != 0
            or str(run.get("output_text") or "").strip()
            or str(request_record.get("response_json") or "").strip()
            or not str(request_record.get("fallback_instruction") or "").strip()
        ):
            return False
        if activity_types.intersection(
            {"reasoning-summary", "plan", "tool", "file", "completed", "failed", "cancelled", "fallback"}
        ):
            return False
        session = self.store.get_provider_session_by_id(str(request_record.get("session_id") or ""))
        if not session or str(session.get("model_id") or "") == fallback_model_id:
            return False
        try:
            self._model(fallback_model_id)
        except HTTPException:
            return False
        return not self._native_output_snapshot(request_record, run).strip()

    @staticmethod
    def _fallback_bundle(
        worker: dict[str, Any],
        model: HarnessModel,
        effort: str,
    ) -> dict[str, Any]:
        try:
            bundle = json.loads(str(worker.get("bootstrap_bundle_json") or "{}"))
        except json.JSONDecodeError:
            bundle = {}
        if not isinstance(bundle, dict):
            bundle = {}
        incoming_env = bundle.get("env") if isinstance(bundle.get("env"), dict) else {}
        env = dict(incoming_env)
        env.pop("WPR_CODEX_CLI_REASONING_EFFORT", None)
        env.pop("WPR_CLAUDE_CODE_EFFORT", None)
        if model.harness_profile == "codex-cli":
            env["WPR_CODEX_CLI_REASONING_EFFORT"] = effort
        else:
            env["WPR_CLAUDE_CODE_EFFORT"] = effort
        return {
            **bundle,
            "run_mode": "conversation",
            "provider_model": model.native_model,
            "env": env,
        }

    @staticmethod
    def _worker_requires_invocation_bearer(worker: dict[str, Any]) -> bool:
        try:
            bundle = json.loads(str(worker.get("bootstrap_bundle_json") or "{}"))
        except json.JSONDecodeError:
            return False
        broker = bundle.get("glasshive_capability_broker") if isinstance(bundle, dict) else None
        return bool(
            isinstance(broker, dict)
            and str(broker.get("authority_kind") or "").strip()
            == "conversation_orchestrator"
        )

    @classmethod
    def _fallback_run_local_bundle(
        cls,
        worker: dict[str, Any],
        transient: dict[str, Any] | None,
        model: HarnessModel,
        effort: str,
    ) -> dict[str, Any] | None:
        if not isinstance(transient, dict):
            return None
        persistent = cls._fallback_bundle(worker, model, effort)
        persistent_env = (
            persistent.get("env") if isinstance(persistent.get("env"), dict) else {}
        )
        transient_env = (
            transient.get("env") if isinstance(transient.get("env"), dict) else {}
        )
        merged = {
            **persistent,
            **copy.deepcopy(transient),
            "env": {**persistent_env, **transient_env},
        }
        return cls._fallback_bundle(
            {"bootstrap_bundle_json": json.dumps(merged, ensure_ascii=False)},
            model,
            effort,
        )

    def _fail_fallback_needs_fresh_grant(
        self,
        request_id: str,
        primary_run_id: str,
        claimed: dict[str, Any],
    ) -> dict[str, Any]:
        message = (
            "The primary model quota was unavailable, and the connected-tool authorization "
            "must be refreshed before GlassHive can start the fallback model."
        )
        failed = self.store.update_provider_request_if_state(
            request_id,
            ("queued", "running"),
            state="failed",
            fallback_state="needs_input",
        )
        if not failed:
            return self.store.get_provider_request(request_id) or claimed
        self.store.update_run(
            primary_run_id,
            error_text=message,
            failure_class="conversation_capability_grant_required",
            failure_retryable=0,
            failure_structured=1,
            failure_user_message=message,
            failure_recommended_recovery=(
                "Retry the turn so Viventium can provide a fresh connected-tool authorization."
            ),
            failure_diagnostic_summary=(
                "The invocation-local broker bearer was unavailable after the primary run ended."
            ),
        )
        self.store.add_provider_activity(
            request_id,
            "failed",
            ACTIVITY_SUMMARIES["failed"],
            {
                "failure_class": "conversation_capability_grant_required",
                "needs_input": True,
            },
        )
        self._forget_request_local_bundle(request_id)
        return failed

    def _start_serial_fallback(
        self,
        request_record: dict[str, Any],
        run: dict[str, Any],
        *,
        claimed_request: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        request_id = str(request_record["request_id"])
        primary_run_id = str(run["run_id"])
        claimed = claimed_request or self.store.claim_provider_request_fallback(
            request_id,
            expected_run_id=primary_run_id,
        )
        if not claimed:
            return self.store.get_provider_request(request_id) or request_record
        fallback_model = self._model(str(claimed["fallback_model_id"]))
        fallback_effort = str(
            claimed.get("fallback_reasoning_effort")
            or fallback_model.recommended_effort
        ).strip()
        session = self.store.get_provider_session_by_id(str(claimed["session_id"]))
        old_worker = (
            self.store.get_worker(str(session.get("worker_id") or "")) if session else None
        )
        new_worker: dict[str, Any] | None = None
        try:
            if not session or not old_worker:
                raise RuntimeError("GlassHive could not load the primary native session")
            transient_bundle = self._request_local_bundle(
                request_id,
                expected_run_id=primary_run_id,
            )
            requires_invocation_bearer = self._worker_requires_invocation_bearer(
                old_worker
            )
            fallback_run_local_bundle = self._fallback_run_local_bundle(
                old_worker,
                transient_bundle,
                fallback_model,
                fallback_effort,
            )
            fallback_env = (
                fallback_run_local_bundle.get("env")
                if isinstance(fallback_run_local_bundle, dict)
                and isinstance(fallback_run_local_bundle.get("env"), dict)
                else {}
            )
            has_invocation_bearer = bool(
                str(fallback_env.get("GLASSHIVE_CAPABILITY_BROKER_TOKEN") or "").strip()
            )
            if requires_invocation_bearer and not has_invocation_bearer:
                return self._fail_fallback_needs_fresh_grant(
                    request_id,
                    primary_run_id,
                    claimed,
                )
            if str(old_worker.get("state") or "") != "terminated":
                self.service.terminate_worker(str(old_worker["worker_id"]))
            project = self.service.create_project(
                str(session["owner_id"]),
                f"Viventium conversation {session['conversation_id']}",
                "Persistent Viventium conversation session",
                fallback_model.harness_profile,
                tenant_id=str(session.get("tenant_id") or "local"),
            )
            bundle = self._fallback_bundle(old_worker, fallback_model, fallback_effort)
            new_worker = self.service.create_worker(
                project_id=project["project_id"],
                owner_id=str(session["owner_id"]),
                name=f"Viventium {session['agent_id']}",
                role="conversation-agent",
                profile=fallback_model.harness_profile,
                backend="",
                execution_mode="host",
                alias=f"conversation-{session['conversation_id']}-{session['agent_id']}",
                workspace_root=str(session["workspace_dir"]),
                bootstrap_profile=str(old_worker.get("bootstrap_profile") or "viventium-conversation-v1"),
                bootstrap_bundle=bundle,
                tenant_id=str(session.get("tenant_id") or "local"),
                start_synchronously=False,
                _trusted_run_lane="conversation",
            )
            if str(new_worker.get("state") or "") == "failed":
                raise RuntimeError(
                    str(new_worker.get("last_error") or "GlassHive fallback harness is not ready")
                )
            new_worker = self.store.update_worker(
                str(new_worker["worker_id"]),
                model=fallback_model.native_model,
                workspace_dir=str(session["workspace_dir"]),
            ) or new_worker
            current_manifest = self._session_manifest(session)
            fallback_session = self.store.upsert_provider_session(
                tenant_id=str(session.get("tenant_id") or "local"),
                owner_id=str(session["owner_id"]),
                conversation_id=str(session["conversation_id"]),
                agent_id=str(session["agent_id"]),
                model_id=fallback_model.id,
                project_id=str(project["project_id"]),
                worker_id=str(new_worker["worker_id"]),
                workspace_dir=str(session["workspace_dir"]),
                access_mode=str(session["access_mode"]),
                history_count=0,
                context_manifest={
                    **current_manifest,
                    "messages": 0,
                    "effort": fallback_effort,
                    "serial_fallback_from_model": str(session.get("model_id") or ""),
                    "serial_fallback_from_run_id": primary_run_id,
                },
            )
            activated = self.service.activate_prepared_conversation_worker(
                str(new_worker["worker_id"])
            )
            if str(activated.get("state") or "") == "failed":
                raise RuntimeError(
                    str(
                        activated.get("last_error")
                        or "GlassHive fallback harness is not ready"
                    )
                )
            fallback_run = self.service.assign_run(
                str(new_worker["worker_id"]),
                str(claimed["fallback_instruction"]),
                start_processor=False,
                run_local_bundle=fallback_run_local_bundle,
            )
            started = self.store.start_provider_request_fallback(
                request_id,
                expected_run_id=primary_run_id,
                fallback_run_id=str(fallback_run["run_id"]),
                session_id=str(fallback_session["session_id"]),
            )
            if not started:
                self.store.finalize_run_if_state(
                    str(fallback_run["run_id"]),
                    "queued",
                    "cancelled",
                    error_text="Cancelled before native fallback execution started",
                )
                self.service.discard_run_local_bundle(str(fallback_run["run_id"]))
                self.service.terminate_worker(str(new_worker["worker_id"]))
                self._forget_request_local_bundle(request_id)
                return self.store.get_provider_request(request_id) or claimed
            self.service.start_assigned_run(str(new_worker["worker_id"]))
            self._forget_request_local_bundle(request_id)
            self.store.add_provider_activity(
                request_id,
                "fallback",
                ACTIVITY_SUMMARIES["fallback"],
                {
                    "failure_class": str(run.get("failure_class") or ""),
                    "model": fallback_model.id,
                },
            )
            return started
        except Exception as exc:
            self._forget_request_local_bundle(request_id)
            if new_worker and str(new_worker.get("state") or "") != "terminated":
                try:
                    self.service.terminate_worker(str(new_worker["worker_id"]))
                except Exception:
                    pass
            combined_message = (
                "The primary model quota was unavailable, and the configured GlassHive fallback "
                "model could not start. Check the fallback harness sign-in/readiness, then try again."
            )
            failed = self.store.update_provider_request_if_state(
                request_id,
                ("queued", "running"),
                state="failed",
                fallback_state="failed",
            )
            if not failed:
                return self.store.get_provider_request(request_id) or claimed
            self.store.update_run(
                primary_run_id,
                failure_user_message=combined_message,
                failure_recommended_recovery=(
                    "Restore the configured fallback harness authentication/readiness or wait for the "
                    "primary provider quota to reset."
                ),
            )
            self.store.add_provider_activity(
                request_id,
                "failed",
                ACTIVITY_SUMMARIES["failed"],
                {"failure_class": type(exc).__name__, "fallback_start_failed": True},
            )
            return failed

    def _sync(self, request_record: dict[str, Any]) -> dict[str, Any]:
        with self._start_lock:
            outcome = self._sync_locked(request_record, defer_fallback=True)
        if isinstance(outcome, DeferredFallbackStart):
            return self._start_serial_fallback(
                outcome.request_record,
                outcome.run,
                claimed_request=outcome.request_record,
            )
        return outcome

    def _sync_locked(
        self,
        request_record: dict[str, Any],
        *,
        defer_fallback: bool = False,
    ) -> dict[str, Any] | DeferredFallbackStart:
        request_id = str(request_record["request_id"])
        current = self.store.get_provider_request(request_id) or request_record
        if str(current.get("state") or "") in TERMINAL_REQUEST_STATES:
            self._forget_request_local_bundle(request_id)
            return current
        request_record = current
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
            return self.store.update_provider_request_if_state(
                request_id,
                ("queued", "running"),
                state="running" if run_state == "running" else "queued",
            ) or self.store.get_provider_request(request_id) or request_record

        # Another provider process can observe the failed primary after the durable
        # compare-and-swap winner has claimed the serial fallback but before it has
        # attached the replacement run. Leave the request non-terminal so only the
        # winner can complete that transition.
        if str(request_record.get("fallback_state") or "") == "claimed":
            claimed_before = (
                datetime.now(timezone.utc)
                - timedelta(seconds=SERIAL_FALLBACK_CLAIM_TIMEOUT_SEC)
            ).isoformat()
            stale = self.store.fail_stale_provider_request_fallback(
                request_id,
                claimed_before=claimed_before,
            )
            if stale:
                self.store.update_run(
                    run_id,
                    failure_user_message=(
                        "The primary model quota was unavailable, and the configured GlassHive "
                        "fallback did not finish starting. Please retry the turn."
                    ),
                    failure_recommended_recovery=(
                        "Retry the same turn. GlassHive abandoned the stale fallback start without "
                        "allowing a second concurrent author."
                    ),
                )
                if "failed" not in activity_types:
                    self.store.add_provider_activity(
                        request_id,
                        "failed",
                        ACTIVITY_SUMMARIES["failed"],
                        {"failure_class": "fallback_start_abandoned"},
                    )
                return stale
            return request_record

        if (
            run_state == "failed"
            and self._serial_fallback_eligible(request_record, run, activity_types)
        ):
            if defer_fallback:
                claimed = self.store.claim_provider_request_fallback(
                    request_id,
                    expected_run_id=run_id,
                )
                if not claimed:
                    return self.store.get_provider_request(request_id) or request_record
                return DeferredFallbackStart(claimed, run)
            return self._start_serial_fallback(request_record, run)

        involuntary_interruption = (
            run_state == "interrupted"
            and bool(run.get("failure_retryable"))
            and bool(run.get("failure_structured"))
        )
        final_state = (
            "completed"
            if run_state == "completed"
            else "failed"
            if involuntary_interruption
            else "cancelled"
            if run_state in {"cancelled", "interrupted"}
            else "failed"
        )
        fallback_state = str(request_record.get("fallback_state") or "")
        fallback_terminal = (
            "completed"
            if fallback_state == "started" and final_state == "completed"
            else "exhausted"
            if fallback_state == "started" and final_state == "failed"
            else fallback_state
        )
        updated = self.store.update_provider_request_if_state(
            request_id,
            ("queued", "running"),
            state=final_state,
            fallback_state=fallback_terminal,
        ) or self.store.get_provider_request(request_id) or request_record
        if str(updated.get("state") or "") != final_state:
            return updated
        if final_state not in activity_types:
            summary = ACTIVITY_SUMMARIES[final_state]
            payload = (
                {"failure_class": str(run.get("failure_class") or "")}
                if final_state == "failed"
                else {}
            )
            self.store.add_provider_activity(request_id, final_state, summary, payload)
        if final_state == "completed" and updated.get("state") == "completed":
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
        self._forget_request_local_bundle(request_id)
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

    def _native_citation_sources_snapshot(
        self,
        request_record: dict[str, Any],
        run: dict[str, Any],
    ) -> list[dict[str, Any]]:
        collector = getattr(self.service.runtime, "provider_citation_sources", None)
        if not callable(collector):
            return []
        session = self.store.get_provider_session_by_id(str(request_record["session_id"]))
        if not session:
            return []
        worker = self.store.get_worker(str(session["worker_id"]))
        if not worker:
            return []
        try:
            sources = collector(worker, str(run.get("run_id") or ""))
        except (OSError, RuntimeError, ValueError):
            return []
        return [dict(source) for source in sources if isinstance(source, dict)]

    def _conversation_output(
        self,
        request_record: dict[str, Any],
        run: dict[str, Any],
    ) -> str:
        native = self._native_output_snapshot(request_record, run)
        sources = self._native_citation_sources_snapshot(request_record, run)
        return _redact_text(
            _sanitize_provider_output(
                native or str(run.get("output_text") or ""),
                sources,
            )
        )

    def _graph_control_output(
        self,
        request_record: dict[str, Any],
        run: dict[str, Any],
    ) -> str:
        """Return the runtime's terminal structured result for graph decisions.

        Native JSONL may contain several completed assistant items as a worker
        investigates a request. The runtime parser already selects and stores the
        terminal structured result in ``run.output_text``; concatenating earlier
        progress items produces an invalid graph envelope.
        """

        final_output = str(run.get("output_text") or "").strip()
        if final_output:
            sources = self._native_citation_sources_snapshot(request_record, run)
            return _redact_text(_sanitize_provider_output(final_output, sources))
        return self._conversation_output(request_record, run)

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
        legacy_timeout = (
            float(timeout)
            if timeout is not None
            else self._configured_response_timeout_seconds()
        )
        while True:
            record = self.store.get_provider_request(request_id)
            if not record:
                raise HTTPException(status_code=404, detail="GlassHive request not found")
            record, run = self._arbitrate_deadline_if_needed(
                record,
                timeout_seconds=legacy_timeout,
            )
            record = self._sync(record)
            record, run = self._arbitrate_deadline_if_needed(
                record,
                timeout_seconds=legacy_timeout,
            )
            if record["state"] in TERMINAL_REQUEST_STATES:
                return record, run
            time.sleep(0.05)

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
        control = graph_transfer_control(payload.tools, payload.tool_choice)
        output = (
            self._graph_control_output(request_record, run)
            if control
            else self._conversation_output(request_record, run)
        )
        try:
            decision = parse_graph_transfer_output(output, control)
        except ValueError as exc:
            raise HTTPException(
                status_code=502,
                detail="GlassHive harness returned invalid Agent Builder graph control output",
            ) from exc
        visible_output = str(decision.get("content") or "")
        usage, usage_source = self._completion_usage(
            request_record,
            run,
            payload,
            visible_output,
        )
        if decision["type"] == "tool_call":
            tool_name = str(decision["tool_name"])
            message = {
                "role": "assistant",
                "content": visible_output or None,
                "reasoning_content": "",
                "tool_calls": [
                    {
                        "id": self._graph_transfer_call_id(request_record, tool_name),
                        "type": "function",
                        "function": {"name": tool_name, "arguments": "{}"},
                    }
                ],
            }
            finish_reason = "tool_calls"
        else:
            message = {
                "role": "assistant",
                "content": visible_output,
                "reasoning_content": "",
            }
            finish_reason = "stop"
        response = {
            "id": request_record["request_id"],
            "object": "chat.completion",
            "created": int(time.time()),
            "model": payload.model,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": finish_reason,
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

    @staticmethod
    def _graph_transfer_call_id(
        request_record: dict[str, Any],
        tool_name: str,
    ) -> str:
        digest = hashlib.sha256(
            f"{request_record['request_id']}\n{tool_name}".encode("utf-8")
        ).hexdigest()[:24]
        return f"call_{digest}"

    async def stream(
        self,
        request_record: dict[str, Any],
        payload: ChatCompletionRequest,
        request: Request,
    ):
        request_id = str(request_record["request_id"])
        try:
            agent_builder_control = graph_transfer_control(
                payload.tools,
                payload.tool_choice,
            )
        except ValueError:
            agent_builder_control = None
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
        latest_citation_sources: list[dict[str, Any]] = []
        execution_started_seen = False
        last_heartbeat = time.monotonic()
        legacy_timeout = self._configured_response_timeout_seconds()
        while True:
            if await request.is_disconnected():
                # A browser/SSE disconnect is not cancellation. The same idempotency key can
                # reattach while the native run continues.
                return
            record = await asyncio.to_thread(self.store.get_provider_request, request_id)
            if not record:
                break
            record, run = await asyncio.to_thread(
                self._arbitrate_deadline_if_needed,
                record,
                timeout_seconds=legacy_timeout,
            )
            if record["state"] not in TERMINAL_REQUEST_STATES:
                record = await asyncio.to_thread(self._sync, record)
                record, run = await asyncio.to_thread(
                    self._arbitrate_deadline_if_needed,
                    record,
                    timeout_seconds=legacy_timeout,
                )
            latest_native = await asyncio.to_thread(
                self._native_output_snapshot,
                record,
                run,
            )
            if not agent_builder_control and latest_native.startswith(native_snapshot):
                raw_delta = latest_native[len(native_snapshot) :]
                native_snapshot = latest_native
                if raw_delta:
                    latest_citation_sources = await asyncio.to_thread(
                        self._native_citation_sources_snapshot,
                        record,
                        run,
                    )
                visible_delta = redactor.feed(raw_delta, latest_citation_sources)
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
                if not execution_started_seen or event_type in {
                    "queued",
                    "waiting",
                    "started",
                    "fallback",
                }:
                    # The dedicated activity endpoint retains lifecycle visibility. Queue,
                    # wait, process-start, and provider-switch events are not authored
                    # reasoning, so they stay out of the chat channel and cannot
                    # incorrectly lock host fallback.
                    continue
                summary_chunk = {
                    "id": request_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": payload.model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "reasoning_content": f"{_redact_text(str(event['summary']))}\n"
                            },
                            "finish_reason": None,
                        }
                    ],
                }
                yield f"data: {json.dumps(summary_chunk, separators=(',', ':'))}\n\n"
                last_heartbeat = time.monotonic()
            if record["state"] in TERMINAL_REQUEST_STATES:
                if record["state"] == "completed":
                    output = await asyncio.to_thread(
                        self._graph_control_output
                        if agent_builder_control
                        else self._conversation_output,
                        record,
                        run,
                    )
                    if agent_builder_control:
                        try:
                            decision = parse_graph_transfer_output(
                                output,
                                agent_builder_control,
                            )
                        except ValueError:
                            error_chunk = {
                                "id": request_id,
                                "object": "chat.completion.chunk",
                                "created": created,
                                "model": payload.model,
                                "error": {
                                    "message": "GlassHive harness returned invalid Agent Builder graph control output",
                                    "type": "glasshive_runtime_error",
                                    "code": "invalid_agent_builder_control_output",
                                    "failure_class": "invalid_agent_builder_control_output",
                                },
                                "choices": [],
                            }
                            yield f"data: {json.dumps(error_chunk, separators=(',', ':'))}\n\n"
                            decision = {"type": "assistant_response", "content": ""}
                            output = ""
                            finish_reason = "stop"
                        else:
                            output = str(decision.get("content") or "")
                            if decision["type"] == "tool_call":
                                tool_name = str(decision["tool_name"])
                                if output:
                                    content_chunk = {
                                        "id": request_id,
                                        "object": "chat.completion.chunk",
                                        "created": created,
                                        "model": payload.model,
                                        "choices": [
                                            {
                                                "index": 0,
                                                "delta": {"content": output},
                                                "finish_reason": None,
                                            }
                                        ],
                                    }
                                    yield f"data: {json.dumps(content_chunk, separators=(',', ':'))}\n\n"
                                tool_chunk = {
                                    "id": request_id,
                                    "object": "chat.completion.chunk",
                                    "created": created,
                                    "model": payload.model,
                                    "choices": [
                                        {
                                            "index": 0,
                                            "delta": {
                                                "tool_calls": [
                                                    {
                                                        "index": 0,
                                                        "id": self._graph_transfer_call_id(
                                                            record,
                                                            tool_name,
                                                        ),
                                                        "type": "function",
                                                        "function": {
                                                            "name": tool_name,
                                                            "arguments": "{}",
                                                        },
                                                    }
                                                ]
                                            },
                                            "finish_reason": None,
                                        }
                                    ],
                                }
                                yield f"data: {json.dumps(tool_chunk, separators=(',', ':'))}\n\n"
                                finish_reason = "tool_calls"
                            else:
                                if output:
                                    content_chunk = {
                                        "id": request_id,
                                        "object": "chat.completion.chunk",
                                        "created": created,
                                        "model": payload.model,
                                        "choices": [
                                            {
                                                "index": 0,
                                                "delta": {"content": output},
                                                "finish_reason": None,
                                            }
                                        ],
                                    }
                                    yield f"data: {json.dumps(content_chunk, separators=(',', ':'))}\n\n"
                                finish_reason = "stop"
                    else:
                        latest_citation_sources = await asyncio.to_thread(
                            self._native_citation_sources_snapshot,
                            record,
                            run,
                        )
                        flushed = redactor.flush(latest_citation_sources)
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
                    failure_class = str(run.get("failure_class") or "")
                    deadline_failure = (
                        failure_class == PROVIDER_RESPONSE_DEADLINE_FAILURE_CLASS
                        or str(record.get("fallback_state") or "")
                        == "deadline_exceeded"
                    )
                    if deadline_failure:
                        failure_class = PROVIDER_RESPONSE_DEADLINE_FAILURE_CLASS
                        error = _redact_text(
                            str(
                                run.get("failure_user_message")
                                or self._deadline_message(
                                    self._request_timeout_seconds(record)
                                )
                            )
                        )
                    error_chunk = {
                        "id": request_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": payload.model,
                        "error": {
                            "message": error,
                            "type": (
                                "glasshive_timeout_error"
                                if deadline_failure
                                else "glasshive_runtime_error"
                            ),
                            "code": failure_class,
                            "failure_class": failure_class,
                            **(
                                {
                                    "timeout_seconds": self._request_timeout_seconds(
                                        record
                                    )
                                }
                                if deadline_failure
                                else {}
                            ),
                        },
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
                    "usage": usage,
                    "glasshive": {"usage_source": usage_source},
                }
                yield f"data: {json.dumps(final_chunk, separators=(',', ':'))}\n\n"
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
        should_interrupt = False
        worker_id = ""
        run_id = ""
        with self._start_lock:
            record = self.store.get_provider_request(request_id)
            if not record:
                raise HTTPException(status_code=404, detail="GlassHive request not found")
            if record["state"] in TERMINAL_REQUEST_STATES:
                return record
            run = self.store.get_run(str(record.get("run_id") or "")) or {}
            if str(run.get("state") or "") == "completed":
                return self._sync_locked(record)
            cancelled = self.store.claim_provider_request_cancel(request_id)
            if not cancelled:
                return self.store.get_provider_request(request_id) or record
            session = self.store.get_provider_session_by_id(str(cancelled["session_id"]))
            run_id = str(cancelled.get("run_id") or "").strip()
            run = self.store.get_run(run_id) if run_id else None
            worker_id = str(session.get("worker_id") or "") if session else ""
            should_interrupt = bool(
                worker_id and run_id and str((run or {}).get("state") or "") == "running"
            )
            existing_types = {
                item["event_type"] for item in self.store.list_provider_activity(request_id)
            }
            if "cancelled" not in existing_types:
                self.store.add_provider_activity(
                    request_id,
                    "cancelled",
                    ACTIVITY_SUMMARIES["cancelled"],
                )
            self._forget_request_local_bundle(request_id)
        # A native interrupt may block while a stuck CLI tears down. The
        # request/queued-run Stop claim above is already durable, so never hold
        # the provider-wide start/cancel lock across that cleanup.
        if should_interrupt:
            self.service.interrupt_worker(worker_id, run_id=run_id)
        return cancelled

    def cancel_by_idempotency(
        self,
        idempotency_key: str,
        request: Request,
        *,
        tenant_id: str = "local",
    ) -> dict[str, Any]:
        owner_id = _header(request, "x-viventium-user-id")
        if not owner_id:
            raise HTTPException(status_code=401, detail="X-Viventium-User-Id is required")
        normalized_key = str(idempotency_key or "").strip()
        if not normalized_key:
            raise HTTPException(status_code=400, detail="GlassHive idempotency key is required")
        with self._start_lock:
            self.store.upsert_provider_stop_tombstone(
                tenant_id=tenant_id,
                owner_id=owner_id,
                base_idempotency_key=normalized_key,
                ttl_seconds=self._configured_request_retention_days() * 24 * 60 * 60,
            )
            records = self.store.list_provider_requests_by_idempotency_family(
                tenant_id=tenant_id,
                owner_id=owner_id,
                base_idempotency_key=normalized_key,
            )
            # This family endpoint represents an explicit user Stop for the whole
            # participant turn. A participant's current child may already be
            # terminal while another participant is still being cancelled; the
            # graph can otherwise re-enter this family after Stop. Keep one
            # owner-scoped base tombstone until TTL even for an all-terminal
            # participant family. A new user turn has a different base key.
        if records:
            latest = records[0]
            for record in records:
                if str(record.get("state") or "") not in TERMINAL_REQUEST_STATES:
                    latest = self.cancel(str(record["request_id"]))
            return latest
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


def install_conversation_provider_routes(
    app,
    *,
    store: Store,
    service: WorkersProjectsService,
    tenant_for_request,
    provider_token: str,
) -> ConversationProvider:
    provider = ConversationProvider(store, service)

    def require_provider_auth(request: Request) -> None:
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

    @app.get("/v1/models")
    def glasshive_models(request: Request) -> dict[str, Any]:
        require_provider_auth(request)
        return provider.models_payload()

    @app.post("/v1/chat/completions")
    async def glasshive_chat_completions(payload: ChatCompletionRequest, request: Request):
        require_provider_auth(request)
        payload = _hydrate_metadata(payload, request)
        tenant_id = str(tenant_for_request(request) or "local")
        record = await asyncio.to_thread(provider.start, payload, request, tenant_id=tenant_id)
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
        if (
            str(run.get("failure_class") or "")
            == PROVIDER_RESPONSE_DEADLINE_FAILURE_CLASS
            or str(record.get("fallback_state") or "") == "deadline_exceeded"
        ):
            return JSONResponse(
                status_code=504,
                content=provider.deadline_error_payload(record, run),
                headers={"X-GlassHive-Request-Id": str(record["request_id"])},
            )
        return JSONResponse(provider.response_payload(record, run, payload))

    @app.get("/v1/requests/{request_id}/activity")
    async def glasshive_activity(request_id: str, request: Request):
        require_provider_auth(request)
        provider.assert_request_owner(request_id, request)
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
        require_provider_auth(request)
        provider.assert_request_owner(request_id, request)
        record = provider.cancel(request_id)
        return {"id": request_id, "object": "glasshive.request", "state": record["state"]}

    @app.post("/v1/requests/by-idempotency/{idempotency_key}/cancel")
    def glasshive_cancel_by_idempotency(
        idempotency_key: str,
        request: Request,
    ) -> dict[str, Any]:
        require_provider_auth(request)
        tenant_id = str(tenant_for_request(request) or "local")
        record = provider.cancel_by_idempotency(
            idempotency_key,
            request,
            tenant_id=tenant_id,
        )
        return {
            "id": str(record["request_id"]),
            "object": "glasshive.request",
            "state": record["state"],
        }

    return provider
