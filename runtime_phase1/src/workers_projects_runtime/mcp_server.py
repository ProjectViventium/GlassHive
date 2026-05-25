from __future__ import annotations

import argparse
import base64
import hmac
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal
from urllib.parse import quote, urlparse

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .auth import AuthContext, scoped_alias
from .bootstrap import BOOTSTRAP_SOURCE_TOKEN_KEY, sign_bootstrap_source_path
from .operator_urls import surface_aware_watch_url, surface_can_open_operator_url
from .runtime_env import load_viventium_runtime_env
from .signed_links import append_signed_query, sign_link_token, signed_link_ttl_seconds

try:
    from fastmcp.server.dependencies import get_http_headers
except Exception:  # pragma: no cover - optional dependency path differs by FastMCP package
    get_http_headers = None  # type: ignore[assignment]

load_viventium_runtime_env()

DEFAULT_BASE_URL = os.environ.get("WPR_MCP_BASE_URL", "http://127.0.0.1:8766").rstrip("/")
DEFAULT_HOST = os.environ.get("WPR_MCP_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("WPR_MCP_PORT", "8767"))
DEFAULT_TIMEOUT_SEC = float(os.environ.get("WPR_MCP_TIMEOUT_SEC", "120"))
DEFAULT_OWNER_ID = os.environ.get("WPR_DEFAULT_OWNER_ID", "").strip()
DEFAULT_API_TOKEN = os.environ.get("WPR_API_TOKEN", "").strip()


def _configured_default_worker_profile() -> str:
    raw_configured = os.environ.get("GLASSHIVE_DEFAULT_WORKER_PROFILE", "").strip()
    configured = raw_configured or "codex-cli"
    raw_allowed = (
        os.environ.get("GLASSHIVE_ALLOWED_WORKER_PROFILES", "").strip()
        or os.environ.get("WPR_ALLOWED_WORKER_PROFILES", "").strip()
    )
    allowed = [item.strip() for item in raw_allowed.split(",") if item.strip()]
    if not allowed or configured in allowed:
        return configured
    if raw_configured:
        raise RuntimeError(
            "GLASSHIVE_DEFAULT_WORKER_PROFILE must be included in GLASSHIVE_ALLOWED_WORKER_PROFILES"
        )
    return allowed[0]


HEADER_USER_ID = "x-viventium-user-id"
HEADER_TENANT_ID = "x-viventium-tenant-id"
HEADER_USER_EMAIL = "x-viventium-user-email"
HEADER_USER_ROLE = "x-viventium-user-role"
HEADER_AGENT_ID = "x-viventium-agent-id"
HEADER_CONVERSATION_ID = "x-viventium-conversation-id"
HEADER_PARENT_MESSAGE_ID = "x-viventium-parent-message-id"
HEADER_MESSAGE_ID = "x-viventium-message-id"
HEADER_SURFACE = "x-viventium-surface"
HEADER_INPUT_MODE = "x-viventium-input-mode"
HEADER_STREAM_ID = "x-viventium-stream-id"
HEADER_VOICE_CALL_SESSION_ID = "x-viventium-voice-call-session-id"
HEADER_VOICE_REQUEST_ID = "x-viventium-voice-request-id"
HEADER_TELEGRAM_CHAT_ID = "x-viventium-telegram-chat-id"
HEADER_TELEGRAM_USER_ID = "x-viventium-telegram-user-id"
HEADER_TELEGRAM_MESSAGE_ID = "x-viventium-telegram-message-id"
HEADER_REQUEST_FILES = "x-viventium-request-files"
HEADER_REQUEST_ATTACHMENTS = "x-viventium-request-attachments"
HEADER_TOOL_RESOURCES = "x-viventium-tool-resources"
HEADER_FILE_IDS = "x-viventium-file-ids"
HEADER_SERVICE_TOKEN = "x-wpr-token"
HEADER_ALIASES = {
    HEADER_USER_ID: ("x-glasshive-user-id", "x-librechat-user-id"),
    HEADER_TENANT_ID: ("x-glasshive-tenant-id", "x-librechat-tenant-id"),
    HEADER_USER_EMAIL: ("x-glasshive-user-email", "x-librechat-user-email"),
    HEADER_USER_ROLE: ("x-glasshive-user-role", "x-librechat-user-role"),
    HEADER_AGENT_ID: ("x-glasshive-agent-id", "x-librechat-agent-id"),
    HEADER_CONVERSATION_ID: ("x-glasshive-conversation-id", "x-librechat-conversation-id"),
    HEADER_PARENT_MESSAGE_ID: ("x-glasshive-parent-message-id", "x-librechat-parent-message-id"),
    HEADER_MESSAGE_ID: ("x-glasshive-message-id", "x-librechat-message-id"),
    HEADER_SURFACE: ("x-glasshive-surface", "x-librechat-surface"),
    HEADER_INPUT_MODE: ("x-glasshive-input-mode", "x-librechat-input-mode"),
    HEADER_STREAM_ID: ("x-glasshive-stream-id", "x-librechat-stream-id"),
    HEADER_VOICE_CALL_SESSION_ID: ("x-glasshive-voice-call-session-id", "x-librechat-voice-call-session-id"),
    HEADER_VOICE_REQUEST_ID: ("x-glasshive-voice-request-id", "x-librechat-voice-request-id"),
    HEADER_TELEGRAM_CHAT_ID: ("x-glasshive-telegram-chat-id",),
    HEADER_TELEGRAM_USER_ID: ("x-glasshive-telegram-user-id",),
    HEADER_TELEGRAM_MESSAGE_ID: ("x-glasshive-telegram-message-id",),
    HEADER_REQUEST_FILES: ("x-glasshive-request-files", "x-librechat-request-files"),
    HEADER_REQUEST_ATTACHMENTS: ("x-glasshive-request-attachments", "x-librechat-request-attachments"),
    HEADER_TOOL_RESOURCES: ("x-glasshive-tool-resources", "x-librechat-tool-resources"),
    HEADER_FILE_IDS: ("x-glasshive-file-ids", "x-librechat-file-ids"),
    HEADER_SERVICE_TOKEN: ("x-glasshive-service-token", "x-glasshive-mcp-service-token"),
}
CALLBACK_REQUIRED_CONTEXT_KEYS = ("user_id", "conversation_id", "parent_message_id", "message_id")

ExecutionModeParam = Annotated[
    Literal["docker", "host"] | None,
    Field(
        description=(
            "Execution surface. Use 'host' for the user's real computer/session: local browser profile, "
            "desktop apps, local files/projects, installed CLIs, or OS tools. Use 'docker' for isolated "
            "sandbox/disposable/risky work. Omit only when the configured default is correct."
        )
    ),
]
ProfileParam = Annotated[
    str,
    Field(
        description=(
            "Worker CLI profile. Prefer 'codex-cli' for host browser/desktop/file/code execution when "
            "Codex is installed, 'claude-code' when Claude is explicitly requested, and "
            "'openclaw-general' only when OpenClaw is installed or explicitly requested."
        )
    ),
]
BackendParam = Annotated[
    str,
    Field(description="Worker backend. Current GlassHive workers use 'openclaw'."),
]
DesktopActionParam = Annotated[
    Literal["terminal", "files", "browser", "focus_browser", "codex", "claude", "openclaw"],
    Field(description="Worker surface/action to open or focus for takeover/visibility."),
]
BootstrapBundleParam = Annotated[
    str | dict[str, Any] | None,
    Field(
        description=(
            "Optional bootstrap bundle as either a JSON string or structured object. Use it to seed "
            "project_definition, prompt files, MCP config, env, and workspace files. For convenience, "
            "files may be either a list of file entries or an object mapping relative paths to content, "
            "for example {'project-definition.md': '# Task'}."
        )
    ),
]
UploadedFilesParam = Annotated[
    list[dict[str, Any]] | dict[str, Any] | None,
    Field(
        description=(
            "Optional attached/uploaded files to materialize in the workspace when the chat host does "
            "not project upload metadata through MCP headers. Use this only with file content or file "
            "references already visible in the current user request/model context. Prefer entries like "
            "[{'filename': 'brief.txt', 'text': '...'}]. If only a file name/id/reference is visible, "
            "pass that metadata so GlassHive can create an upload manifest and the worker can report a "
            "real blocker instead of pretending the file was read."
        )
    ),
]


def _default_execution_mode() -> str:
    if not _host_workers_enabled():
        return "docker"
    mode = os.environ.get("WPR_DEFAULT_EXECUTION_MODE", "docker").strip().lower()
    return mode if mode in {"docker", "host"} else "docker"


def _allowed_worker_profiles() -> set[str]:
    raw = (
        os.environ.get("GLASSHIVE_ALLOWED_WORKER_PROFILES", "").strip()
        or os.environ.get("WPR_ALLOWED_WORKER_PROFILES", "").strip()
    )
    return {item.strip() for item in raw.split(",") if item.strip()}


def _profile_allowed(profile: str) -> bool:
    allowed = _allowed_worker_profiles()
    return bool(profile and (not allowed or profile in allowed))


def _normalize_preferences(raw: dict[str, Any] | None) -> dict[str, str]:
    data = raw or {}
    return {
        "default_worker_profile": str(data.get("default_worker_profile") or "").strip(),
        "codex_reasoning_effort": str(data.get("codex_reasoning_effort") or "").strip().lower(),
        "claude_effort": str(data.get("claude_effort") or "").strip().lower(),
        "openclaw_effort": str(data.get("openclaw_effort") or "").strip().lower(),
    }


def _resolve_profile_from_preferences(profile: str | None, preferences: dict[str, str] | None) -> str:
    requested = str(profile or "").strip()
    if requested:
        return requested
    preferred = str((preferences or {}).get("default_worker_profile") or "").strip()
    if _profile_allowed(preferred):
        return preferred
    return _configured_default_worker_profile()


def _resolve_effort_for_profile(
    profile: str,
    effort: str | None,
    preferences: dict[str, str] | None,
) -> str:
    requested = str(effort or "").strip().lower()
    if requested:
        return requested
    prefs = preferences or {}
    if profile == "codex-cli":
        return prefs.get("codex_reasoning_effort", "")
    if profile == "claude-code":
        return prefs.get("claude_effort", "")
    if profile == "openclaw-general":
        return prefs.get("openclaw_effort", "")
    return ""


def _apply_effort_to_bundle(bundle: dict[str, Any], *, profile: str, effort: str) -> dict[str, Any]:
    clean_effort = str(effort or "").strip().lower()
    if not clean_effort:
        return bundle
    next_bundle = dict(bundle or {})
    if profile == "codex-cli":
        if clean_effort not in {"minimal", "low", "medium", "high", "xhigh"}:
            raise ValueError("Codex effort must be minimal, low, medium, high, or xhigh")
        env = dict(next_bundle.get("env") or {})
        env["WPR_CODEX_CLI_REASONING_EFFORT"] = clean_effort
        next_bundle["env"] = env
        return next_bundle
    if profile == "claude-code":
        if clean_effort not in {"default", "max"}:
            raise ValueError("Claude effort must be default or max")
    elif profile == "openclaw-general":
        if clean_effort not in {"default", "high", "max"}:
            raise ValueError("OpenClaw effort must be default, high, or max")
    else:
        return next_bundle
    if clean_effort == "default":
        return next_bundle
    current = str(next_bundle.get("system_instructions") or "").strip()
    addition = f"Worker effort preference for this run: {clean_effort}."
    next_bundle["system_instructions"] = f"{current}\n\n{addition}".strip()
    return next_bundle


def _host_workers_enabled() -> bool:
    value = os.environ.get("GLASSHIVE_HOST_WORKERS_ENABLED", "true").strip().lower()
    return value not in {"0", "false", "no", "off", "disabled"}


def _enterprise_mode_enabled() -> bool:
    value = os.environ.get("GLASSHIVE_ENTERPRISE_MODE", os.environ.get("WPR_ENTERPRISE_MODE", "")).strip().lower()
    return value in {"1", "true", "yes", "on", "enabled"}


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            deduped.append(text)
    return deduped


def _host_for_header(hostname: str) -> str:
    host = str(hostname or "").strip().lower()
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def _allowed_host_values_from_setting(value: str) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    if "://" in text:
        parsed = urlparse(text)
        if not parsed.hostname:
            return []
        host = _host_for_header(parsed.hostname)
        if parsed.port:
            return [f"{host}:{parsed.port}", f"{host}:*"]
        return [host, f"{host}:*"]
    if text.endswith(":*") or ":" in text.rsplit("@", 1)[-1]:
        return [text]
    return [text, f"{text}:*"]


def _allowed_origin_values_from_url(value: str) -> list[str]:
    parsed = urlparse(str(value or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return []
    host = _host_for_header(parsed.hostname)
    origin = f"{parsed.scheme}://{host}"
    if parsed.port:
        return [f"{origin}:{parsed.port}", f"{origin}:*"]
    return [origin, f"{origin}:*"]


def _csv_env_values(*names: str) -> list[str]:
    values: list[str] = []
    for name in names:
        raw = os.environ.get(name, "")
        values.extend(part.strip() for part in raw.split(",") if part.strip())
    return values


def _mcp_transport_security_settings(host: str, port: int) -> TransportSecuritySettings:
    allowed_hosts = [
        "127.0.0.1:*",
        "localhost:*",
        "[::1]:*",
        "testserver",
    ]
    allowed_origins = [
        "http://127.0.0.1:*",
        "http://localhost:*",
        "http://[::1]:*",
    ]

    listen_host = _host_for_header(host)
    if listen_host and listen_host not in {"0.0.0.0", "::", "[::]"}:
        allowed_hosts.extend([f"{listen_host}:{port}", f"{listen_host}:*"])

    for configured in _csv_env_values("WPR_MCP_ALLOWED_HOSTS", "GLASSHIVE_MCP_ALLOWED_HOSTS"):
        allowed_hosts.extend(_allowed_host_values_from_setting(configured))

    for configured_url in _csv_env_values("GLASSHIVE_MCP_URL", "WPR_MCP_PUBLIC_URL"):
        allowed_hosts.extend(_allowed_host_values_from_setting(configured_url))
        allowed_origins.extend(_allowed_origin_values_from_url(configured_url))

    for configured_url in _csv_env_values("LIBRECHAT_ENDPOINT", "VIVENTIUM_FRONTEND_URL", "GLASSHIVE_OPERATOR_BASE_URL"):
        allowed_origins.extend(_allowed_origin_values_from_url(configured_url))

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_dedupe(allowed_hosts),
        allowed_origins=_dedupe(allowed_origins),
    )


def _host_worker_mentions() -> tuple[str, str, str]:
    return (
        os.environ.get("WPR_HOST_MENTION_CODEX", "@codex").strip() or "@codex",
        os.environ.get("WPR_HOST_MENTION_CLAUDE", "@claude").strip() or "@claude",
        os.environ.get("WPR_HOST_MENTION_OPENCLAW", "@openclaw").strip() or "@openclaw",
    )


def glasshive_workers_server_instructions() -> str:
    if _host_workers_enabled():
        if _default_execution_mode() == "host":
            execution_instruction = (
                "Default to host-native execution for the user's real Chrome/browser profile, "
                "desktop apps, OS tools, host files, local projects, and installed CLIs."
            )
        else:
            execution_instruction = (
                f"When execution_mode is omitted, MCP worker tools use the configured default "
                f"'{_default_execution_mode()}'. Set execution_mode='host' when the task depends "
                "on the user's real computer/session: logged-in browser profile, desktop apps, "
                "local files/projects, installed CLIs, or OS/window control."
            )
    else:
        execution_instruction = (
            "Host-native workers are disabled by GlassHive config; configured default 'docker'; "
            "do not request execution_mode='host'."
        )

    codex_mention, claude_mention, openclaw_mention = _host_worker_mentions()
    return (
        "GlassHive owns persistent projects, resumable workers, host-native workers for browser and desktop action, "
        "local files/projects, installed CLIs, workstation sandboxes, and live operator takeover. "
        "Use it when the user asks the host assistant to act in a real browser, desktop app, local file, "
        "local project, installed tool, or current computer session; the user does not need to say "
        "GlassHive, Codex, Computer Use, or local machine. Do not answer from memory or inference when "
        "real browser/desktop/local state must be inspected or changed. "
        f"{execution_instruction} "
        "Use Docker/workstation mode only for isolated sandbox, disposable browser, risky untrusted "
        "browsing, or explicit sandbox requests. "
        f"For configured mentions {codex_mention}, {claude_mention}, and {openclaw_mention}, create "
        "or resume a host worker with the matching profile semantics; prefer codex-cli for available "
        "host browser/desktop/file/code execution, claude-code when Claude is explicitly requested, "
        "and openclaw-general only when installed or explicitly requested. Current request upload "
        "metadata is projected through neutral GlassHive/LibreChat headers when the host supplies "
        "it. Some standalone LibreChat builds do not expose upload metadata to MCP headers; when "
        "attached-file text is visible in the current model context, pass it through uploaded_files "
        "on workspace_launch or worker_delegate_once so GlassHive can materialize it under uploads/ "
        "before the worker starts. Do not tell the worker to pass the file through again; tell it "
        "to read the workspace-relative uploads/<filename> file directly. "
        "If only file names/references are visible, still pass those names/references and the "
        "requested outcome so the worker can report a real blocker instead of pretending it read "
        "content. Preserve existing file references in bootstrap_bundle_json. If the user refers "
        "to an attached or uploaded file and your model context cannot read the file body directly, "
        "still call workspace_launch or worker_delegate_once; do not refuse solely because your own "
        "model context lacks file contents. For fresh one-off "
        "host/browser/desktop/local tasks, prefer workspace_launch, whose fields mirror the "
        "documented GlassHive UI: description, required success_criteria, and optional context. "
        "When the user asks to make a worker or effort the default, use workspace_preferences_set; "
        "when profile or effort is omitted, workspace_launch and worker_delegate_once honor the "
        "authenticated user's saved defaults before falling back to deployment defaults. "
        "worker_delegate_once is the lower-level one-call fallback when the caller already has a "
        "precise instruction/title. These high-level tools create or resume the "
        "project/worker, includes optional callback/upload context, queues the run in one call, and "
        "returns a View / Steer link plus follow_up_context for later status/result questions. "
        "GlassHive must work standalone: callbacks are an optional host-app delivery enhancement, "
        "not a requirement. After workspace_launch or worker_delegate_once, write one short "
        "outcome-focused acknowledgement in the assistant's own voice and include the View / Steer "
        "link when present on web/browser surfaces. If callback_ready=false, say the work is running "
        "and can be checked with the standalone MCP status tools. If the user asks whether it is "
        "done or what happened, use follow_up_context run_id/worker_id with workspace_status for a "
        "non-blocking check or workspace_wait for a blocking wait before answering. After lower-level worker_run, queued only means accepted; do not "
        "report it as done. Preserve the user's success condition and response-format constraints in "
        "worker instructions. User-facing responses should not expose raw worker/run/provider/queue "
        "plumbing outside the View / Steer link unless diagnostics were requested."
    )


def _resolve_execution_mode(value: str | None) -> str:
    mode = str(value or "").strip().lower()
    if not mode:
        mode = _default_execution_mode()
    if mode not in {"docker", "host"}:
        raise ValueError("execution_mode must be either 'docker' or 'host'")
    if mode == "host" and not _host_workers_enabled():
        raise ValueError("host-native GlassHive workers are disabled by GlassHive config")
    return mode


def _normalize_headers(raw_headers: object) -> dict[str, str]:
    if raw_headers is None:
        return {}
    if hasattr(raw_headers, "items"):
        items = raw_headers.items()
    elif isinstance(raw_headers, list):
        items = raw_headers
    else:
        return {}
    return {str(key).lower(): str(value) for key, value in items}


def _header_value(headers: dict[str, str], primary: str) -> str:
    for name in (primary, *HEADER_ALIASES.get(primary, ())):
        value = _sanitize_context_value(headers.get(name))
        if value:
            return value
    return ""


def _request_headers() -> dict[str, str]:
    if get_http_headers is None:
        return {}
    try:
        return _normalize_headers(get_http_headers())
    except Exception:
        return {}


def _request_owner_id(owner_id: str | None) -> str | None:
    if _enterprise_mode_enabled():
        headers = _request_headers()
        _require_enterprise_mcp_service_auth(headers)
        _require_enterprise_mcp_identity_assertion(headers)
        return _header_value(headers, HEADER_USER_ID) or DEFAULT_OWNER_ID or None
    explicit = _sanitize_context_value(owner_id)
    if explicit:
        return explicit
    return _header_value(_request_headers(), HEADER_USER_ID) or None


def _request_scoped_alias(alias: str) -> str:
    clean_alias = alias.strip()
    if not clean_alias or not _enterprise_mode_enabled():
        return clean_alias
    headers = _request_headers()
    _require_enterprise_mcp_service_auth(headers)
    _require_enterprise_mcp_identity_assertion(headers)
    tenant_id = (
        _header_value(headers, HEADER_TENANT_ID)
        or _configured_enterprise_tenant_id()
    )
    user_id = _header_value(headers, HEADER_USER_ID) or DEFAULT_OWNER_ID
    return scoped_alias(AuthContext(tenant_id=tenant_id, user_id=user_id, enterprise=True), clean_alias)


def _sanitize_context_value(value: str | None) -> str:
    stripped = str(value or "").strip()
    if stripped.startswith("{{") and stripped.endswith("}}"):
        return ""
    if stripped.startswith("${") and stripped.endswith("}"):
        return ""
    return stripped


def _token_matches(candidate: str | None, expected: str | None) -> bool:
    candidate_text = str(candidate or "").strip()
    expected_text = str(expected or "").strip()
    if not candidate_text or not expected_text:
        return False
    return hmac.compare_digest(candidate_text, expected_text)


def _require_enterprise_mcp_service_auth(headers: dict[str, str]) -> None:
    if not _enterprise_mode_enabled():
        return
    expected = str(DEFAULT_API_TOKEN or os.environ.get("WPR_API_TOKEN", "")).strip()
    if not expected:
        raise PermissionError("GlassHive MCP service authentication is not configured")
    auth_header = str(headers.get("authorization") or "").strip()
    bearer = auth_header.removeprefix("Bearer ").strip() if auth_header.lower().startswith("bearer ") else ""
    header_token = _header_value(headers, HEADER_SERVICE_TOKEN)
    if not (_token_matches(header_token, expected) or _token_matches(bearer, expected)):
        raise PermissionError("GlassHive MCP service authentication is required")


def _configured_enterprise_tenant_id() -> str:
    return (
        _sanitize_context_value(os.environ.get("GLASSHIVE_ENTERPRISE_TENANT_ID"))
        or _sanitize_context_value(os.environ.get("WPR_ENTERPRISE_TENANT_ID"))
    )


def _require_enterprise_mcp_identity_assertion(headers: dict[str, str]) -> None:
    if not _enterprise_mode_enabled():
        return
    configured_tenant = _configured_enterprise_tenant_id()
    asserted_tenant = _header_value(headers, HEADER_TENANT_ID)
    if not configured_tenant:
        raise PermissionError("GlassHive MCP enterprise tenant is not configured")
    if asserted_tenant and asserted_tenant != configured_tenant:
        raise PermissionError("GlassHive MCP tenant assertion does not match this deployment")
    if not _header_value(headers, HEADER_USER_ID):
        raise PermissionError("GlassHive MCP requires an authenticated user assertion")


class EnterpriseMcpHttpAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if _enterprise_mode_enabled():
            headers = {key.lower(): value for key, value in request.headers.items()}
            try:
                _require_enterprise_mcp_service_auth(headers)
                _require_enterprise_mcp_identity_assertion(headers)
            except PermissionError as exc:
                return JSONResponse(status_code=401, content={"detail": str(exc)})
        return await call_next(request)


def _require_enterprise_mcp_transport(transport: str) -> None:
    if _enterprise_mode_enabled() and transport != "streamable-http":
        raise RuntimeError("GlassHive MCP enterprise mode requires streamable-http transport")


def _audit_preview(value: str, *, max_chars: int = 700) -> str:
    text = str(value or "")
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{12,}", r"\1[REDACTED]", text)
    text = re.sub(
        r"(?i)((?:api[_-]?key|token|secret|password|passwd|pwd)\s*[:=]\s*)[^\s\"']{6,}",
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "sk-[REDACTED]", text)
    text = re.sub(
        r"(?:~\/|\/Users\/|\/home\/|\/private\/var\/|\/var\/folders\/|[A-Za-z]:\\Users\\)[^\s`'\"<>]+",
        "[local path]",
        text,
    )
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:
        return f"{text[: max_chars - 3].rstrip()}..."
    return text


def _decode_json_header(value: str | None) -> Any:
    sanitized = _sanitize_context_value(value)
    if not sanitized:
        return None
    raw = sanitized
    if raw.startswith("b64:"):
        try:
            raw = base64.b64decode(raw[4:]).decode("utf-8")
        except Exception:
            return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _safe_upload_filename(value: object, fallback: str) -> str:
    name = str(value or "").replace("\\", "/").rsplit("/", 1)[-1].strip() or fallback
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-")
    return safe or fallback


def _path_list_env(name: str) -> list[Path]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return []
    paths: list[Path] = []
    for item in raw.split(os.pathsep):
        item = item.strip()
        if item:
            paths.append(Path(item).expanduser())
    return paths


def _upload_root_candidates() -> list[Path]:
    roots: list[Path] = []
    configured = os.environ.get("WPR_LIBRECHAT_UPLOADS_ROOT", "").strip()
    if configured:
        roots.append(Path(configured).expanduser())
    roots.extend(_path_list_env("WPR_BOOTSTRAP_SOURCE_ROOTS"))
    deduped: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = os.fspath(root)
        if key not in seen:
            seen.add(key)
            deduped.append(root)
    return deduped


def _trusted_virtual_upload_source(value: str, *, owner_id: str | None = None) -> str:
    roots = _upload_root_candidates()
    if not roots or not value.startswith("/uploads/"):
        return ""
    clean_value = value.split("?", 1)[0]
    relative = clean_value.split("/uploads/", 1)[1].strip("/")
    if not relative:
        return ""
    relative_path = os.path.normpath(relative)
    if relative_path == "." or relative_path.startswith("..") or os.path.isabs(relative_path):
        return ""
    if ".." in relative_path.split(os.path.sep):
        return ""
    if _enterprise_mode_enabled():
        clean_owner = _safe_owner_path_component(owner_id)
        first_part = relative_path.split(os.path.sep, 1)[0]
        if not clean_owner or first_part != clean_owner:
            return ""
    candidates = [root / relative_path for root in roots]
    for candidate in candidates:
        if candidate.exists():
            return os.fspath(candidate)
    return ""


def _normalized_upload_filename_key(value: object) -> str:
    name = str(value or "").replace("\\", "/").rsplit("/", 1)[-1].strip().lower()
    if "__" in name:
        name = name.split("__", 1)[1]
    return re.sub(r"[^a-z0-9]+", "", name)


def _safe_owner_path_component(value: object) -> str:
    clean = str(value or "").strip()
    if not clean or clean in {".", ".."}:
        return ""
    if "\x00" in clean or "/" in clean or "\\" in clean or ".." in clean:
        return ""
    return clean


def _owner_scoped_upload_source_for_filename(filename: str, *, owner_id: str | None = None) -> str:
    roots = _upload_root_candidates()
    clean_owner = _safe_owner_path_component(owner_id)
    target_key = _normalized_upload_filename_key(filename)
    if not roots or not clean_owner or not target_key:
        return ""
    candidates: list[Path] = []
    for root in roots:
        owner_root = root / clean_owner
        if not owner_root.exists() or not owner_root.is_dir():
            continue
        try:
            for candidate in owner_root.rglob("*"):
                if not candidate.is_file():
                    continue
                if _normalized_upload_filename_key(candidate.name) == target_key:
                    candidates.append(candidate)
        except OSError:
            continue
    if not candidates:
        return ""
    candidates.sort(key=lambda item: (item.stat().st_mtime_ns, os.fspath(item)), reverse=True)
    return os.fspath(candidates[0])


def _signed_view_steer_url(worker: dict[str, Any], project_id: str | None, request_surface: str | None) -> str | None:
    worker_id = str(worker.get("worker_id") or "").strip()
    if not worker_id:
        return None
    url = surface_aware_watch_url(
        worker_id,
        project_id,
        request_surface=request_surface,
        watch_surface="desktop",
    )
    if not url:
        return None
    token = sign_link_token(
        kind="worker_view",
        worker_id=worker_id,
        tenant_id=str(worker.get("tenant_id") or ""),
        owner_id=str(worker.get("owner_id") or ""),
    )
    if token:
        return append_signed_query(url, {"gh_token": token})
    if _enterprise_mode_enabled():
        return None
    if str(worker.get("tenant_id") or "") not in {"", "local"}:
        return None
    return url


def _public_artifact_base_url() -> str:
    return (
        os.environ.get("GLASSHIVE_ARTIFACT_BASE_URL", "").strip()
        or os.environ.get("GLASSHIVE_OPERATOR_BASE_URL", "").strip()
        or os.environ.get("WPR_MCP_PUBLIC_URL", "").strip()
        or DEFAULT_BASE_URL
    ).rstrip("/")


def _clean_artifact_relative_path(path: str) -> str:
    raw = str(path or "").strip().replace("\\", "/")
    if raw.startswith("/"):
        return ""
    clean_path = raw.lstrip("/")
    if not clean_path:
        return ""
    relative = PurePosixPath(clean_path)
    if relative.is_absolute():
        return ""
    if any(part in {"", ".", "..", ".git"} for part in relative.parts):
        return ""
    return relative.as_posix()


def _signed_artifact_download_url(worker: dict[str, Any], path: str) -> str | None:
    worker_id = str(worker.get("worker_id") or "").strip()
    clean_path = _clean_artifact_relative_path(path)
    if not worker_id or not clean_path:
        return None
    token = sign_link_token(
        kind="artifact_download",
        worker_id=worker_id,
        tenant_id=str(worker.get("tenant_id") or ""),
        owner_id=str(worker.get("owner_id") or ""),
        path=clean_path,
    )
    public_base = _public_artifact_base_url()
    if token:
        return f"{public_base}/v1/signed-links/{quote(token)}"
    if _enterprise_mode_enabled():
        return None
    return f"{public_base}/v1/workers/{quote(worker_id)}/artifacts/download?path={quote(clean_path)}"


def _artifact_listing_payload(
    *,
    worker: dict[str, Any],
    artifacts: dict[str, Any],
    include_download_links: bool = True,
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    raw_items = artifacts.get("items", []) if isinstance(artifacts, dict) else []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        path = _clean_artifact_relative_path(str(item.get("path") or ""))
        if include_download_links and path:
            item["signed_download_url"] = _signed_artifact_download_url(worker, path)
        items.append(item)
    return {
        "status": "ok",
        "worker_id": str(worker.get("worker_id") or "").strip() or None,
        "items": items,
        "download_links_signed": bool(include_download_links),
        "download_link_ttl_seconds": signed_link_ttl_seconds(),
        "next_action_guidance": (
            "Return the relevant signed_download_url to the user as a download link when present. "
            "Do not paste whole generated files into chat when GlassHive provides a signed download link."
        ),
    }


def _dispatch_follow_up_context(
    *,
    worker: dict[str, Any],
    project_id: str,
    run: dict[str, Any],
    request_surface: str | None,
) -> dict[str, Any]:
    worker_id = str(worker.get("worker_id") or "").strip()
    run_id = str(run.get("run_id") or "").strip()
    view_steer_url = _signed_view_steer_url(worker, project_id, request_surface)
    follow_up_context: dict[str, Any] = {
        "project_id": project_id,
        "worker_id": worker_id,
        "run_id": run_id,
        "run_state": run.get("state"),
        "status_tool": "workspace_status",
        "blocking_wait_tool": "workspace_wait",
        "run_status_tool": "run_get",
        "live_tool": "worker_live",
        "takeover_tool": "worker_takeover",
        "main_agent_rule": (
            "For follow-up result/status questions, call workspace_status with run_id and/or "
            "worker_id for a non-blocking check, or workspace_wait with run_id when the user "
            "explicitly wants you to wait. Do not guess from the acknowledgement."
        ),
        "user_facing_id_policy": "Do not show raw project_id/worker_id/run_id unless the user asks for diagnostics.",
    }
    if view_steer_url:
        follow_up_context["view_steer_url"] = view_steer_url
    follow_up_context["artifact_tool"] = "workspace_artifacts"
    follow_up_context["artifact_download_tool"] = "workspace_artifact_download"
    return {
        "view_steer_url": view_steer_url,
        "view_steer": {
            "label": "View / Steer GlassHive workspace",
            "url": view_steer_url,
            "include_in_acknowledgement": bool(view_steer_url),
        },
        "follow_up_context": follow_up_context,
    }


def _iter_upload_file_objects(value: Any):
    if isinstance(value, list):
        for item in value:
            yield from _iter_upload_file_objects(item)
        return
    if not isinstance(value, dict):
        return
    if any(key in value for key in ("file_id", "filename", "filepath", "source_path", "local_path", "text")):
        yield value
    for child in value.values():
        if isinstance(child, (dict, list)):
            yield from _iter_upload_file_objects(child)


def _project_upload_file_entry(
    file_obj: dict[str, Any],
    index: int,
    *,
    tenant_id: str | None = None,
    owner_id: str | None = None,
) -> dict[str, Any] | None:
    file_id = str(file_obj.get("file_id") or file_obj.get("id") or "").strip()
    filename = _safe_upload_filename(
        file_obj.get("filename") or file_obj.get("name") or file_obj.get("filepath") or file_id,
        f"upload-{index}",
    )
    metadata = {
        key: file_obj.get(key)
        for key in ("file_id", "filename", "source", "context", "type", "bytes")
        if file_obj.get(key) is not None
    }
    source_ref = ""
    for key in ("filepath", "path", "local_path", "upload_path", "absolute_path", "url", "uri"):
        candidate = str(file_obj.get(key) or "").strip()
        if candidate:
            source_ref = candidate
            break
    if source_ref:
        metadata["source_ref"] = source_ref
    trusted_source = _trusted_virtual_upload_source(source_ref, owner_id=owner_id)
    if not trusted_source:
        trusted_source = _owner_scoped_upload_source_for_filename(filename, owner_id=owner_id)
    if trusted_source:
        token = sign_bootstrap_source_path(trusted_source, tenant_id=tenant_id, owner_id=owner_id)
        return {
            "scope": "workspace",
            "path": f"uploads/{filename}",
            "source_path": trusted_source,
            **({BOOTSTRAP_SOURCE_TOKEN_KEY: token} if token else {}),
            **metadata,
        }
    text = file_obj.get("text")
    if isinstance(text, str) and text.strip():
        if filename.lower().endswith((".txt", ".md", ".csv", ".json", ".jsonl", ".tsv", ".yaml", ".yml", ".xml", ".html", ".htm", ".log")):
            return {
                "scope": "workspace",
                "path": f"uploads/{filename}",
                "content": text,
                **metadata,
            }
        metadata = {
            **metadata,
            "filename": metadata.get("filename") or filename,
            "source_status": "original_bytes_unavailable",
            "blocker": (
                "Original uploaded file bytes were not safely available to GlassHive. "
                "Do not substitute extracted text for this file unless the user explicitly asked for text extraction."
            ),
            "extracted_text_available": True,
        }
    if not metadata:
        return None
    manifest_name = f"{filename}.metadata.json"
    return {
        "scope": "workspace",
        "path": f"uploads/{manifest_name}",
        "content": json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        **({"upload_blocker": metadata.get("blocker")} if metadata.get("blocker") else {}),
        **{key: value for key, value in metadata.items() if key != "source_ref"},
    }


def _project_upload_files(
    upload_context: dict[str, Any],
    *,
    tenant_id: str | None = None,
    owner_id: str | None = None,
) -> list[dict[str, Any]]:
    projected: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for file_obj in _iter_upload_file_objects(upload_context):
        entry = _project_upload_file_entry(
            file_obj,
            len(projected) + 1,
            tenant_id=tenant_id,
            owner_id=owner_id,
        )
        if not entry:
            continue
        key = (str(entry.get("file_id") or ""), str(entry.get("source_path") or entry.get("path") or ""))
        if key in seen:
            continue
        seen.add(key)
        projected.append(entry)
    return projected


def _merge_bundle_files(existing: Any, projected: list[dict[str, Any]]) -> list[Any]:
    files = list(existing) if isinstance(existing, list) else []
    seen_file_ids = {str(item.get("file_id")) for item in files if isinstance(item, dict) and item.get("file_id")}
    seen_sources = {str(item.get("source_path")) for item in files if isinstance(item, dict) and item.get("source_path")}
    seen_paths = {str(item.get("path")) for item in files if isinstance(item, dict) and item.get("path")}
    for entry in projected:
        file_id = str(entry.get("file_id") or "")
        source_path = str(entry.get("source_path") or "")
        path = str(entry.get("path") or "")
        if (file_id and file_id in seen_file_ids) or (source_path and source_path in seen_sources) or (path and path in seen_paths):
            continue
        files.append(entry)
        if file_id:
            seen_file_ids.add(file_id)
        if source_path:
            seen_sources.add(source_path)
        if path:
            seen_paths.add(path)
    return files


def _append_materialized_uploads_instruction(
    bundle: dict[str, Any],
    projected: list[dict[str, Any]],
) -> None:
    paths = []
    blocker_paths = []
    for entry in projected:
        if not isinstance(entry, dict):
            continue
        path = str(entry.get("path") or "").strip().lstrip("/")
        if path and path not in paths:
            paths.append(path)
        if path and entry.get("upload_blocker") and path not in blocker_paths:
            blocker_paths.append(path)
    if not paths:
        return

    existing = _safe_text_content(bundle.get("project_definition")).rstrip()
    if "## Attached workspace files" in existing:
        return
    lines = [
        "",
        "## Attached workspace files",
        "",
        "GlassHive has already prepared the uploaded/attached file records before this worker starts.",
        "Read materialized files directly from these workspace-relative paths. Metadata manifests mean the original bytes were not safely available; treat those as blockers for file-preserving work and report that plainly instead of substituting extracted text. Do not ask the user to re-attach unless a listed file is missing, unreadable, or inconsistent with the user's request.",
    ]
    lines.extend(f"- `{path}`" for path in paths)
    if blocker_paths:
        lines.extend(
            [
                "",
                "The following attachment records are metadata/blocker manifests, not source files:",
            ]
        )
        lines.extend(f"- `{path}`" for path in blocker_paths)
    bundle["project_definition"] = (existing + "\n".join(lines) + "\n").lstrip()


def _safe_text_content(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, indent=2, sort_keys=True) + "\n"
    return str(value)


def _coerce_bundle_file_entries(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if not isinstance(value, dict):
        return []
    if any(key in value for key in ("path", "content", "source_path", "local_path", "upload_path", "absolute_path", "filepath")):
        return [value]
    entries: list[dict[str, Any]] = []
    for raw_path, raw_entry in value.items():
        path = str(raw_path or "").strip().lstrip("/")
        if not path:
            continue
        if isinstance(raw_entry, dict):
            entry = dict(raw_entry)
            entry.setdefault("scope", "workspace")
            entry.setdefault("path", path)
        else:
            entry = {"scope": "workspace", "path": path, "content": _safe_text_content(raw_entry)}
        entries.append(entry)
    return entries


def _normalize_bootstrap_bundle(value: Any) -> dict[str, Any] | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        parsed = json.loads(stripped)
    elif isinstance(value, dict):
        parsed = dict(value)
    else:
        raise ValueError("bootstrap_bundle_json must be a JSON object or JSON string")
    if not isinstance(parsed, dict):
        raise ValueError("bootstrap_bundle_json must decode to a JSON object")

    files = _coerce_bundle_file_entries(parsed.get("files"))
    if "files" in parsed:
        parsed["files"] = files
    if files:
        for entry in files:
            if not isinstance(entry, dict):
                continue
            path = str(entry.get("path") or "").strip().lstrip("/").lower()
            if path in {"project-definition.md", "project_definition.md"} and "project_definition" not in parsed:
                parsed["project_definition"] = _safe_text_content(entry.get("content"))
                break
    return parsed


def _slugify_alias(*parts: str) -> str:
    raw = "-".join(part for part in parts if str(part or "").strip())
    slug = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
    return slug[:80] or "glasshive-task"


def _default_project_definition(*, title: str, goal: str, instruction: str) -> str:
    sections = [f"# {title.strip() or 'GlassHive Task'}"]
    clean_goal = goal.strip()
    clean_instruction = instruction.strip()
    if clean_goal:
        sections.extend(["", clean_goal])
    if clean_instruction and clean_instruction != clean_goal:
        sections.extend(["", "## Task", "", clean_instruction])
    return "\n".join(sections).strip() + "\n"


def _merge_request_context(bundle: dict[str, Any] | None) -> dict[str, Any] | None:
    load_viventium_runtime_env()
    headers = _request_headers()
    callback_url = (
        os.environ.get("GLASSHIVE_EVENTS_WEBHOOK_URL", "").strip()
        or os.environ.get("VIVENTIUM_GLASSHIVE_CALLBACK_URL", "").strip()
    )
    callback_secret = (
        os.environ.get("GLASSHIVE_EVENTS_HMAC_SECRET", "").strip()
        or os.environ.get("VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", "").strip()
    )
    context = {
        "tenant_id": _header_value(headers, HEADER_TENANT_ID),
        "user_id": _header_value(headers, HEADER_USER_ID),
        "agent_id": _header_value(headers, HEADER_AGENT_ID),
        "conversation_id": _header_value(headers, HEADER_CONVERSATION_ID),
        "parent_message_id": _header_value(headers, HEADER_PARENT_MESSAGE_ID),
        "message_id": _header_value(headers, HEADER_MESSAGE_ID),
        "surface": _header_value(headers, HEADER_SURFACE),
        "input_mode": _header_value(headers, HEADER_INPUT_MODE),
        "stream_id": _header_value(headers, HEADER_STREAM_ID),
        "voice_call_session_id": _header_value(headers, HEADER_VOICE_CALL_SESSION_ID),
        "voice_request_id": _header_value(headers, HEADER_VOICE_REQUEST_ID),
        "telegram_chat_id": _header_value(headers, HEADER_TELEGRAM_CHAT_ID),
        "telegram_user_id": _header_value(headers, HEADER_TELEGRAM_USER_ID),
        "telegram_message_id": _header_value(headers, HEADER_TELEGRAM_MESSAGE_ID),
    }
    context = {key: value for key, value in context.items() if value}
    upload_context = {
        "request_files": _decode_json_header(_header_value(headers, HEADER_REQUEST_FILES)),
        "request_attachments": _decode_json_header(_header_value(headers, HEADER_REQUEST_ATTACHMENTS)),
        "tool_resources": _decode_json_header(_header_value(headers, HEADER_TOOL_RESOURCES)),
        "file_ids": _decode_json_header(_header_value(headers, HEADER_FILE_IDS)),
    }
    upload_context = {key: value for key, value in upload_context.items() if value not in (None, "", [], {})}
    if not context and not callback_url and not upload_context:
        return bundle
    merged: dict[str, Any] = dict(bundle or {})
    existing_callbacks = merged.get("callbacks")
    has_existing_callbacks = isinstance(existing_callbacks, dict) and bool(existing_callbacks)
    callbacks = dict(existing_callbacks) if has_existing_callbacks else {}
    callback_context = dict(callbacks)
    callback_context.update({key: value for key, value in context.items() if value})
    has_callback_anchor = all(
        str(callback_context.get(key) or "").strip() for key in CALLBACK_REQUIRED_CONTEXT_KEYS
    )
    should_auto_attach_callback = bool(callback_url and callback_secret and has_callback_anchor)
    if has_existing_callbacks or should_auto_attach_callback:
        callbacks.update({key: value for key, value in context.items() if value})
    if should_auto_attach_callback:
        callbacks.setdefault("events_webhook_url", callback_url)
        callbacks.setdefault("hmac_secret", callback_secret)
    if has_existing_callbacks or should_auto_attach_callback:
        merged["callbacks"] = callbacks
    if context:
        merged.setdefault("glasshive_context", context)
        merged.setdefault("viventium_context", context)
    if upload_context:
        merged["glasshive_upload_context"] = upload_context
        merged["viventium_upload_context"] = upload_context
        projected = _project_upload_files(
            upload_context,
            tenant_id=context.get("tenant_id"),
            owner_id=context.get("user_id"),
        )
        if projected:
            merged["files"] = _merge_bundle_files(merged.get("files"), projected)
            _append_materialized_uploads_instruction(merged, projected)
    return merged


def _merge_explicit_uploaded_files(
    bundle: dict[str, Any] | None,
    uploaded_files: Any,
    *,
    tenant_id: str | None = None,
    owner_id: str | None = None,
) -> dict[str, Any]:
    if uploaded_files in (None, "", [], {}):
        return dict(bundle or {})
    merged: dict[str, Any] = dict(bundle or {})
    upload_context = {"tool_uploaded_files": uploaded_files}

    for key in ("glasshive_upload_context", "viventium_upload_context"):
        existing = merged.get(key)
        context = dict(existing) if isinstance(existing, dict) else {}
        context["tool_uploaded_files"] = uploaded_files
        merged[key] = context

    projected = _project_upload_files(
        upload_context,
        tenant_id=tenant_id,
        owner_id=owner_id,
    )
    if projected:
        for entry in projected:
            if isinstance(entry, dict):
                entry.setdefault("source", "model_context_uploaded_files")
                entry.setdefault("materialized_from", "model_context_uploaded_files")
        merged["files"] = _merge_bundle_files(merged.get("files"), projected)
        _append_materialized_uploads_instruction(merged, projected)
    return merged


def _callback_missing_fields(bundle: dict[str, Any] | None) -> list[str]:
    callbacks = bundle.get("callbacks") if isinstance(bundle, dict) else None
    if not isinstance(callbacks, dict):
        callbacks = {}
    required = {
        "events_webhook_url": callbacks.get("events_webhook_url") or callbacks.get("url"),
        "hmac_secret": callbacks.get("hmac_secret") or callbacks.get("secret"),
        "user_id": callbacks.get("user_id"),
        "conversation_id": callbacks.get("conversation_id"),
        "parent_message_id": callbacks.get("parent_message_id"),
        "message_id": callbacks.get("message_id"),
    }
    return [key for key, value in required.items() if not str(value or "").strip()]


def _callback_state(bundle: dict[str, Any] | None, *, required: bool) -> tuple[bool, list[str]]:
    callbacks = bundle.get("callbacks") if isinstance(bundle, dict) else None
    callback_configured = isinstance(callbacks, dict) and bool(callbacks)
    if not callback_configured and not required:
        return False, []
    missing = _callback_missing_fields(bundle)
    return callback_configured and not missing, missing


@dataclass
class WorkersProjectsApiClient:
    base_url: str = DEFAULT_BASE_URL
    timeout_sec: float = DEFAULT_TIMEOUT_SEC
    api_token: str = DEFAULT_API_TOKEN

    def _request(self, method: str, path: str, *, json_body: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        headers: dict[str, str] = {}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        request_headers = _request_headers()
        _require_enterprise_mcp_service_auth(request_headers)
        for name in (HEADER_USER_ID, HEADER_TENANT_ID, HEADER_USER_EMAIL, HEADER_USER_ROLE):
            value = _header_value(request_headers, name)
            if value:
                headers[name] = value
        with httpx.Client(timeout=self.timeout_sec) as client:
            response = client.request(method, url, json=json_body, headers=headers)
            response.raise_for_status()
            if response.headers.get("content-type", "").startswith("application/json"):
                return response.json()
            return response.text

    def _owner_id(self, owner_id: str | None) -> str:
        resolved = (owner_id or DEFAULT_OWNER_ID).strip()
        if not resolved:
            raise ValueError("owner_id is required for this operation")
        return resolved

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health")

    def get_preferences(self) -> dict[str, Any]:
        return self._request("GET", "/v1/preferences")

    def update_preferences(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("PATCH", "/v1/preferences", json_body=payload)

    def list_projects(self, owner_id: str | None = None) -> list[dict[str, Any]]:
        projects = self._request("GET", "/v1/projects").get("items", [])
        if owner_id:
            return [project for project in projects if project.get("owner_id") == owner_id]
        return projects

    def create_project(self, *, owner_id: str | None, title: str, goal: str, default_worker_profile: str = "openclaw-general") -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/projects",
            json_body={
                "owner_id": self._owner_id(owner_id),
                "title": title,
                "goal": goal,
                "default_worker_profile": default_worker_profile,
            },
        )

    def get_project(self, project_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/projects/{project_id}")

    def list_project_runs(self, project_id: str) -> list[dict[str, Any]]:
        return self._request("GET", f"/v1/projects/{project_id}/runs").get("items", [])

    def list_project_events(self, project_id: str) -> list[dict[str, Any]]:
        return self._request("GET", f"/v1/projects/{project_id}/events").get("items", [])

    def list_workers(self, project_id: str) -> list[dict[str, Any]]:
        return self._request("GET", f"/v1/projects/{project_id}/workers").get("items", [])

    def find_worker_by_alias_across_projects(
        self,
        *,
        owner_id: str | None,
        alias: str,
        execution_mode: str | None = None,
    ) -> dict[str, Any] | None:
        scoped = _request_scoped_alias(alias)
        if not scoped:
            return None
        for project in self.list_projects(owner_id):
            project_id = str(project.get("project_id") or "").strip()
            if not project_id:
                continue
            for worker in self.list_workers(project_id):
                if worker.get("state") == "terminated":
                    continue
                if execution_mode and worker.get("execution_mode") and worker.get("execution_mode") != execution_mode:
                    continue
                if str(worker.get("alias") or "").strip() == scoped:
                    return {"project": project, "worker": worker}
        return None

    def create_worker(
        self,
        *,
        project_id: str,
        owner_id: str | None,
        name: str,
        role: str,
        profile: str = "openclaw-general",
        backend: str = "openclaw",
        execution_mode: str | None = None,
        alias: str | None = None,
        workspace_root: str | None = None,
        bootstrap_profile: str | None = None,
        bootstrap_bundle: dict[str, Any] | None = None,
        start_synchronously: bool = True,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/projects/{project_id}/workers",
            json_body={
                "owner_id": self._owner_id(owner_id),
                "name": name,
                "role": role,
                "profile": profile,
                "backend": backend,
                "execution_mode": _resolve_execution_mode(execution_mode),
                "alias": alias,
                "workspace_root": workspace_root,
                "bootstrap_profile": bootstrap_profile,
                "bootstrap_bundle": bootstrap_bundle,
                "start_synchronously": start_synchronously,
            },
        )

    def find_or_resume_worker(
        self,
        *,
        project_id: str,
        owner_id: str | None,
        name: str,
        role: str,
        alias: str,
        profile: str = "openclaw-general",
        backend: str = "openclaw",
        execution_mode: str | None = None,
        workspace_root: str | None = None,
        bootstrap_profile: str | None = None,
        bootstrap_bundle: dict[str, Any] | None = None,
        start_synchronously: bool = True,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/projects/{project_id}/workers/find-or-resume",
            json_body={
                "owner_id": self._owner_id(owner_id),
                "name": name,
                "role": role,
                "profile": profile,
                "backend": backend,
                "execution_mode": _resolve_execution_mode(execution_mode),
                "alias": alias,
                "workspace_root": workspace_root,
                "bootstrap_profile": bootstrap_profile,
                "bootstrap_bundle": bootstrap_bundle,
                "start_synchronously": start_synchronously,
            },
        )

    def get_worker(self, worker_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/workers/{worker_id}")

    def worker_live(self, worker_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/workers/{worker_id}/live")

    def list_artifacts(self, worker_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/workers/{worker_id}/artifacts")

    def worker_runs(self, worker_id: str) -> list[dict[str, Any]]:
        return self._request("GET", f"/v1/workers/{worker_id}/runs").get("items", [])

    def worker_events(self, worker_id: str) -> list[dict[str, Any]]:
        return self._request("GET", f"/v1/workers/{worker_id}/events").get("items", [])

    def assign_run(self, worker_id: str, instruction: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/workers/{worker_id}/assign", json_body={"instruction": instruction})

    def send_message(self, worker_id: str, message: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/workers/{worker_id}/message", json_body={"message": message})

    def schedule_run(
        self,
        worker_id: str,
        instruction: str,
        *,
        run_at: str | None = None,
        schedule_text: str | None = None,
        delay_seconds: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"instruction": instruction}
        if run_at:
            payload["run_at"] = run_at
        if schedule_text:
            payload["schedule_text"] = schedule_text
        if delay_seconds is not None:
            payload["delay_seconds"] = delay_seconds
        return self._request("POST", f"/v1/workers/{worker_id}/schedule", json_body=payload)

    def worker_schedules(self, worker_id: str, include_done: bool = False) -> list[dict[str, Any]]:
        suffix = "?include_done=true" if include_done else ""
        return self._request("GET", f"/v1/workers/{worker_id}/schedules{suffix}").get("items", [])

    def get_schedule(self, schedule_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/schedules/{schedule_id}")

    def lifecycle(self, worker_id: str, action: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/workers/{worker_id}/{action}")

    def desktop_action(self, worker_id: str, action: str, url: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"action": action}
        if url:
            payload["url"] = url
        return self._request("POST", f"/v1/workers/{worker_id}/desktop-action", json_body=payload)

    def takeover(self, worker_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/workers/{worker_id}/takeover")

    def get_run(self, run_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/runs/{run_id}")

    def metrics(self) -> dict[str, Any]:
        return self._request("GET", "/v1/metrics/summary")


def create_mcp_server(
    *,
    base_url: str = DEFAULT_BASE_URL,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    api_client: WorkersProjectsApiClient | None = None,
) -> FastMCP:
    client = api_client or WorkersProjectsApiClient(base_url=base_url)
    server = FastMCP(
        name="glass-hive",
        instructions=glasshive_workers_server_instructions(),
        host=host,
        port=port,
        streamable_http_path="/mcp",
        transport_security=_mcp_transport_security_settings(host, port),
    )

    @server.tool(
        name="projects_list",
        title="List Projects",
        description=(
            "List current projects from the standalone Workers & Projects runtime. Use for explicit status, audit, or resume requests. "
            "For a fresh delegation, prefer worker_delegate_once instead of listing every project or chaining low-level tools. "
            "Returns project records with project_id, owner, title, goal, and runtime metadata when available."
        ),
        structured_output=True,
    )
    def projects_list(owner_id: str | None = None) -> list[dict[str, Any]]:
        return client.list_projects(owner_id=owner_id)

    @server.tool(
        name="project_create",
        title="Create Project",
        description=(
            "Low-level compatibility tool to create a GlassHive project record with title and goal. Use only when the user explicitly asks to set up a reusable project record "
            "or when a diagnostic/orchestration workflow already chose low-level project and worker IDs. Do not use this for normal LibreChat task delegation, file work, browser work, "
            "desktop work, or workspace launch; prefer workspace_launch, then worker_delegate_once. Requires title and goal, optionally owner_id and default_worker_profile. "
            "Returns the project record including project_id for later worker or run tools. If project creation fails, surface the blocker instead of inventing a project id."
        ),
        structured_output=True,
    )
    def project_create(
        title: str,
        goal: str,
        owner_id: str | None = None,
        default_worker_profile: str = "",
    ) -> dict[str, Any]:
        return client.create_project(
            owner_id=_request_owner_id(owner_id),
            title=title,
            goal=goal,
            default_worker_profile=default_worker_profile.strip() or _configured_default_worker_profile(),
        )

    @server.tool(
        name="workspace_preferences_get",
        title="Get GlassHive Preferences",
        description=(
            "Return the authenticated user's saved GlassHive defaults: preferred worker profile and "
            "per-profile effort settings. Use before changing defaults or when a user asks what worker "
            "GlassHive will use."
        ),
        structured_output=True,
    )
    def workspace_preferences_get() -> dict[str, Any]:
        return client.get_preferences()

    @server.tool(
        name="workspace_preferences_set",
        title="Set GlassHive Preferences",
        description=(
            "Save the authenticated user's default GlassHive worker and effort preferences. Use this "
            "when the user says to make Codex, Claude Code, or OpenClaw their default, or asks future "
            "GlassHive runs to use a specific effort. Allowed Codex efforts: minimal, low, medium, "
            "high, xhigh. Claude: max. OpenClaw: high or max."
        ),
        structured_output=True,
    )
    def workspace_preferences_set(
        default_worker_profile: Annotated[
            str | None,
            Field(description="Optional default worker profile: codex-cli, claude-code, or openclaw-general. Empty clears the saved default."),
        ] = None,
        codex_reasoning_effort: Annotated[
            str | None,
            Field(description="Optional Codex default effort: minimal, low, medium, high, xhigh, or empty to use deployment default."),
        ] = None,
        claude_effort: Annotated[
            str | None,
            Field(description="Optional Claude Code default effort: max, or empty/default to use deployment default."),
        ] = None,
        openclaw_effort: Annotated[
            str | None,
            Field(description="Optional OpenClaw default effort: high, max, or empty/default to use deployment default."),
        ] = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if default_worker_profile is not None:
            payload["default_worker_profile"] = default_worker_profile
        if codex_reasoning_effort is not None:
            payload["codex_reasoning_effort"] = codex_reasoning_effort
        if claude_effort is not None:
            payload["claude_effort"] = claude_effort
        if openclaw_effort is not None:
            payload["openclaw_effort"] = openclaw_effort
        return client.update_preferences(payload)

    @server.tool(
        name="project_get",
        title="Get Project",
        description=(
            "Fetch a single GlassHive project by project_id. Use for explicit inspection, status, audit, or resume flows when the project id is already known. "
            "Do not use as the first step for ordinary fresh delegation; prefer worker_delegate_once. Returns the project record and high-signal ownership/title/goal fields."
        ),
        structured_output=True,
    )
    def project_get(project_id: str) -> dict[str, Any]:
        return client.get_project(project_id)

    @server.tool(
        name="worker_delegate_once",
        title="Delegate Task",
        description=(
            "One-call task delegation for GlassHive when the caller already has a precise title and instruction. For normal user-facing launches, prefer workspace_launch because it mirrors the documented GlassHive UI fields. Use this for new host/browser/desktop/local-file tasks "
            "instead of manually listing projects and chaining project_create, worker_create, and worker_run. "
            "It creates a human-named project when project_id is omitted, finds or resumes a worker by alias, "
            "merges optional callback/upload context, queues the run, and returns one clean non-blocking dispatch result. "
            "Callbacks are optional; plain LibreChat or standalone deployments can use workspace_status for non-blocking checks and workspace_wait when the user explicitly wants to wait. "
            "For attached/uploaded-file tasks, use uploaded_files when the file content is visible in the current model context, for example [{'filename':'brief.txt','text':'...'}]. "
            "If the chat model cannot read the file body, still call this with file names/references and requested transformation so GlassHive can use request upload metadata supplied by the host or return an honest blocker. "
            "Write your own short acknowledgement, include the View / Steer link when view_steer_url is present, and keep follow_up_context for later user status/result questions; it contains the worker_id/run_id needed to poll without exposing raw IDs to the user. "
            "Preserve the user's requested final-answer format in the instruction, especially short/exact-answer constraints. "
            "Use delegation_audit to self-check the dispatched instruction, but do not expose it unless diagnostics were requested. "
            "Use execution_mode='host' for the user's real computer/session and 'docker' for isolated work. "
            "Set expose_diagnostics=true only when the user explicitly asked for run/project/worker diagnostics."
        ),
        structured_output=True,
    )
    def worker_delegate_once(
        title: str,
        instruction: str,
        goal: str | None = None,
        project_id: str | None = None,
        owner_id: str | None = None,
        worker_name: str | None = None,
        worker_role: str | None = None,
        alias: str | None = None,
        profile: ProfileParam = "",
        backend: BackendParam = "openclaw",
        execution_mode: ExecutionModeParam = None,
        workspace_root: str | None = None,
        bootstrap_profile: str | None = None,
        bootstrap_bundle_json: BootstrapBundleParam = None,
        uploaded_files: UploadedFilesParam = None,
        effort: Annotated[
            str | None,
            Field(
                description=(
                    "Optional per-run effort override. Codex accepts minimal/low/medium/high/xhigh; "
                    "Claude Code accepts max; OpenClaw accepts high/max. Omit to use the user's saved default."
                )
            ),
        ] = None,
        require_callback: bool = False,
        expose_diagnostics: bool = False,
    ) -> dict[str, Any]:
        resolved_owner_id = _request_owner_id(owner_id)
        resolved_execution_mode = _resolve_execution_mode(execution_mode)
        try:
            preferences = _normalize_preferences(client.get_preferences())
        except Exception:
            preferences = {}
        resolved_profile = _resolve_profile_from_preferences(profile, preferences)
        resolved_effort = _resolve_effort_for_profile(resolved_profile, effort, preferences)
        clean_title = title.strip() or "GlassHive task"
        clean_goal = (goal or instruction).strip()
        clean_instruction = instruction.strip()
        if not clean_instruction:
            raise ValueError("instruction is required")

        bundle = _normalize_bootstrap_bundle(bootstrap_bundle_json) or {}
        bundle.setdefault(
            "project_definition",
            _default_project_definition(title=clean_title, goal=clean_goal, instruction=clean_instruction),
        )
        bundle = _merge_request_context(bundle)
        request_context = bundle.get("glasshive_context") if isinstance(bundle, dict) else None
        context_tenant_id = request_context.get("tenant_id") if isinstance(request_context, dict) else None
        context_owner_id = request_context.get("user_id") if isinstance(request_context, dict) else None
        bundle = _merge_explicit_uploaded_files(
            bundle,
            uploaded_files,
            tenant_id=context_tenant_id,
            owner_id=context_owner_id or resolved_owner_id,
        )
        bundle = _apply_effort_to_bundle(bundle, profile=resolved_profile, effort=resolved_effort)
        callback_ready, missing_callback_fields = _callback_state(bundle, required=require_callback)
        if require_callback and not callback_ready:
            return {
                "status": "blocked",
                "acknowledgement_guidance": (
                    "Explain in your own voice that this cannot be background-dispatched yet, "
                    "because the callback context is incomplete. Name the missing callback fields "
                    "without exposing internal worker/run/project ids."
                ),
                "main_agent_next_action": (
                    "Write one short blocked-status reply in your own voice using "
                    "missing_callback_fields. Do not quote a canned template."
                ),
                "execution_mode": resolved_execution_mode,
                "profile": resolved_profile,
                "effort": resolved_effort,
                "alias": (alias or _slugify_alias(resolved_profile, clean_title)).strip(),
                "callback_ready": False,
                "missing_callback_fields": missing_callback_fields,
            }

        existing_workspace = None
        resolved_alias = (alias or _slugify_alias(resolved_profile, clean_title)).strip()
        if not project_id and alias:
            existing_workspace = client.find_worker_by_alias_across_projects(
                owner_id=resolved_owner_id,
                alias=resolved_alias,
                execution_mode=resolved_execution_mode,
            )
        project = (
            existing_workspace["project"]
            if existing_workspace
            else client.get_project(project_id)
            if project_id
            else client.create_project(
                owner_id=resolved_owner_id,
                title=clean_title,
                goal=clean_goal,
                default_worker_profile=resolved_profile,
            )
        )
        resolved_project_id = str(project.get("project_id") or project_id or "").strip()
        if not resolved_project_id:
            raise ValueError("GlassHive project creation did not return project_id")

        worker = client.find_or_resume_worker(
            project_id=resolved_project_id,
            owner_id=resolved_owner_id,
            name=(worker_name or clean_title).strip(),
            role=(worker_role or clean_goal or clean_instruction).strip(),
            alias=resolved_alias,
            profile=resolved_profile,
            backend=backend,
            execution_mode=resolved_execution_mode,
            workspace_root=workspace_root,
            bootstrap_profile=bootstrap_profile,
            bootstrap_bundle=bundle,
            start_synchronously=False,
        )
        worker_id = str(worker.get("worker_id") or "").strip()
        if not worker_id:
            raise ValueError("GlassHive worker create/resume did not return worker_id")

        run = client.assign_run(worker_id, clean_instruction)
        request_surface = _header_value(_request_headers(), HEADER_SURFACE)
        dispatch_context = _dispatch_follow_up_context(
            worker=worker,
            project_id=resolved_project_id,
            run=run,
            request_surface=request_surface,
        )
        result: dict[str, Any] = {
            "status": "dispatched",
            "acknowledgement_guidance": (
                "Write one short acknowledgement in your own voice that the task has started. "
                "Include the View / Steer link when view_steer_url is present. If callback_ready is "
                "false, say the user can ask for status and you will check GlassHive through MCP; do "
                "not claim an automatic chat callback is required. Do not quote a canned template or "
                "expose raw worker/run IDs."
            ),
            "main_agent_next_action": (
                "Send one short acknowledgement in your own voice, include view_steer_url when "
                "available, and stop this chat turn unless the user explicitly asked you to wait. "
                "Do not call workspace_status, worker_live, run_get, or workspace_wait in the same "
                "turn unless the user asked for diagnostics/live status/blocking completion. On later "
                "follow-up status/result questions, use follow_up_context.run_id/worker_id with "
                "workspace_status for non-blocking status or workspace_wait for a blocking wait before answering."
            ),
            "callback_ready": callback_ready,
            "callback_delivery": "optional" if callback_ready else "not_configured_standalone_polling_available",
            "missing_callback_fields": missing_callback_fields,
            **dispatch_context,
            "delegation_audit": {
                "title": _audit_preview(clean_title, max_chars=180),
                "goal": _audit_preview(clean_goal, max_chars=360),
                "instruction_preview": _audit_preview(clean_instruction),
                "use_for": "self-check only; do not show the user unless diagnostics were requested",
            },
        }
        if expose_diagnostics:
            result.update(
                {
                    "project_id": resolved_project_id,
                    "worker_id": worker_id,
                    "run_id": run.get("run_id"),
                    "run_state": run.get("state"),
                    "execution_mode": resolved_execution_mode,
                    "profile": resolved_profile,
                    "effort": resolved_effort,
                    "alias": resolved_alias,
                    "submitted_instruction": clean_instruction,
                }
            )
        return result

    @server.tool(
        name="workspace_launch",
        title="Launch GlassHive Workspace",
        description=(
            "Primary user-facing GlassHive launch tool. Use this for ordinary LibreChat requests that need a resumable workspace, browser/desktop work, local files, generated artifacts, or a long-running worker. "
            "Its required inputs intentionally mirror the documented GlassHive UI: description, required success_criteria, and optional context. "
            "Do not chain project_create, worker_create, and worker_run for routine tasks. Do not expose project/worker/run IDs unless expose_diagnostics is true. "
            "Returns a clean non-blocking dispatch result with view_steer_url and follow_up_context. "
            "Callbacks are optional; for plain LibreChat or standalone deployments without callback wiring, "
            "use workspace_status for non-blocking follow-up checks and workspace_wait when the user "
            "explicitly wants a blocking wait. For attached/uploaded-file requests, set uploaded_files "
            "when file text is visible to the current model context, and include file names/references "
            "in context so GlassHive can also use request upload metadata when the host supplies it."
        ),
        structured_output=True,
    )
    def workspace_launch(
        description: Annotated[str, Field(description="Describe your project or task in the user's own outcome language.")],
        success_criteria: Annotated[
            str,
            Field(description="Required acceptance criteria. Treat these as hard gates before reporting completion."),
        ],
        context: Annotated[
            str | None,
            Field(description="Optional background, constraints, uploaded file names/references, links, and preferences that help the worker execute well."),
        ] = None,
        workspace_alias: Annotated[
            str | None,
            Field(description="Optional stable workspace alias to resume. Omit for a new one-off workspace."),
        ] = None,
        profile: ProfileParam = "",
        execution_mode: ExecutionModeParam = None,
        bootstrap_bundle_json: BootstrapBundleParam = None,
        uploaded_files: UploadedFilesParam = None,
        effort: Annotated[
            str | None,
            Field(
                description=(
                    "Optional per-run effort override. Codex accepts minimal/low/medium/high/xhigh; "
                    "Claude Code accepts max; OpenClaw accepts high/max. Omit to use saved user preferences or deployment default."
                )
            ),
        ] = None,
        require_callback: bool = False,
        expose_diagnostics: bool = False,
    ) -> dict[str, Any]:
        clean_description = description.strip()
        clean_success_criteria = success_criteria.strip()
        clean_context = (context or "").strip()
        if not clean_description:
            raise ValueError("description is required")
        if not clean_success_criteria:
            raise ValueError("success_criteria is required")
        brief_sections = [
            "Project description:",
            clean_description,
            "",
            "Success criteria:",
            clean_success_criteria,
        ]
        if clean_context:
            brief_sections.extend(["", "Context:", clean_context])
        brief_sections.extend(
            [
                "",
                "Execution rules:",
                "- Treat the success criteria as hard acceptance gates.",
                "- Keep working until the criteria are satisfied or a real blocker appears.",
                "- Preserve files, browser state, and workspace continuity for follow-up work.",
                "- If the result is visual or browser-visible, open it in the workspace browser before finishing.",
                "- Finish with a concise FINAL REPORT that states the outcome, artifacts, and any blocker.",
            ]
        )
        title = clean_description.splitlines()[0].strip()[:120] or "GlassHive workspace"
        return worker_delegate_once(
            title=title,
            instruction="\n".join(brief_sections),
            goal=clean_success_criteria,
            alias=workspace_alias,
            profile=profile,
            execution_mode=execution_mode,
            bootstrap_bundle_json=bootstrap_bundle_json,
            uploaded_files=uploaded_files,
            effort=effort,
            require_callback=require_callback,
            expose_diagnostics=expose_diagnostics,
        )

    @server.tool(
        name="workspace_schedule",
        title="Schedule GlassHive Workspace",
        description=(
            "Schedule a GlassHive workspace to run later without relying on LibreChat Scheduling Cortex. "
            "Use this when the user asks GlassHive or MCP to do work in 20 minutes, on a weekday, or at a specific run_at time. "
            "Inputs mirror workspace_launch plus schedule_text/run_at/delay_seconds. It creates or resumes the worker now, persists the schedule in GlassHive, and queues the run when due. "
            "For scheduled attached-file work, set uploaded_files when visible file text needs to be materialized into the future workspace. "
            "Callbacks are optional; the schedule can be checked later through GlassHive MCP status tools."
        ),
        structured_output=True,
    )
    def workspace_schedule(
        description: Annotated[str, Field(description="Describe the later project or task in the user's outcome language.")],
        success_criteria: Annotated[
            str,
            Field(description="Required acceptance criteria. Treat these as hard gates before reporting completion."),
        ],
        schedule_text: Annotated[
            str | None,
            Field(description="Human schedule expression, for example 'in 20 minutes' or 'on Mondays'."),
        ] = None,
        run_at: Annotated[
            str | None,
            Field(description="Optional ISO datetime. Prefer this when the caller already resolved the due time."),
        ] = None,
        delay_seconds: Annotated[int | None, Field(description="Optional relative delay in seconds for deterministic automation/QA.")] = None,
        context: Annotated[
            str | None,
            Field(description="Optional background, constraints, files, links, and preferences that help the worker execute well."),
        ] = None,
        workspace_alias: Annotated[
            str | None,
            Field(description="Optional stable workspace alias to resume. Omit for a new one-off scheduled workspace."),
        ] = None,
        profile: ProfileParam = "",
        execution_mode: ExecutionModeParam = None,
        bootstrap_bundle_json: BootstrapBundleParam = None,
        uploaded_files: UploadedFilesParam = None,
        effort: Annotated[
            str | None,
            Field(description="Optional per-run effort override for the scheduled workspace."),
        ] = None,
        require_callback: bool = False,
        expose_diagnostics: bool = False,
    ) -> dict[str, Any]:
        clean_description = description.strip()
        clean_success_criteria = success_criteria.strip()
        clean_context = (context or "").strip()
        if not clean_description:
            raise ValueError("description is required")
        if not clean_success_criteria:
            raise ValueError("success_criteria is required")
        brief_sections = [
            "Scheduled project description:",
            clean_description,
            "",
            "Success criteria:",
            clean_success_criteria,
        ]
        if clean_context:
            brief_sections.extend(["", "Context:", clean_context])
        brief_sections.extend(
            [
                "",
                "Execution rules:",
                "- Treat the success criteria as hard acceptance gates.",
                "- Keep working until the criteria are satisfied or a real blocker appears.",
                "- Preserve files, browser state, and workspace continuity for follow-up work.",
                "- Finish with a concise FINAL REPORT that states the outcome, artifacts, and any blocker.",
            ]
        )

        resolved_owner_id = _request_owner_id(None)
        resolved_execution_mode = _resolve_execution_mode(execution_mode)
        try:
            preferences = _normalize_preferences(client.get_preferences())
        except Exception:
            preferences = {}
        resolved_profile = _resolve_profile_from_preferences(profile, preferences)
        resolved_effort = _resolve_effort_for_profile(resolved_profile, effort, preferences)
        title = clean_description.splitlines()[0].strip()[:120] or "GlassHive scheduled workspace"
        bundle = _normalize_bootstrap_bundle(bootstrap_bundle_json) or {}
        bundle.setdefault(
            "project_definition",
            _default_project_definition(title=title, goal=clean_success_criteria, instruction="\n".join(brief_sections)),
        )
        bundle = _merge_request_context(bundle)
        request_context = bundle.get("glasshive_context") if isinstance(bundle, dict) else None
        context_tenant_id = request_context.get("tenant_id") if isinstance(request_context, dict) else None
        context_owner_id = request_context.get("user_id") if isinstance(request_context, dict) else None
        bundle = _merge_explicit_uploaded_files(
            bundle,
            uploaded_files,
            tenant_id=context_tenant_id,
            owner_id=context_owner_id or resolved_owner_id,
        )
        bundle = _apply_effort_to_bundle(bundle, profile=resolved_profile, effort=resolved_effort)
        callback_ready, missing_callback_fields = _callback_state(bundle, required=require_callback)
        if require_callback and not callback_ready:
            return {
                "status": "blocked",
                "callback_ready": False,
                "missing_callback_fields": missing_callback_fields,
                "acknowledgement_guidance": (
                    "Explain in your own voice that the scheduled workspace cannot be accepted yet "
                    "because the callback context is incomplete."
                ),
            }

        resolved_alias = (workspace_alias or _slugify_alias(resolved_profile, title)).strip()
        existing_workspace = None
        if workspace_alias:
            existing_workspace = client.find_worker_by_alias_across_projects(
                owner_id=resolved_owner_id,
                alias=resolved_alias,
                execution_mode=resolved_execution_mode,
            )
        project = existing_workspace["project"] if existing_workspace else client.create_project(
            owner_id=resolved_owner_id,
            title=title,
            goal=clean_success_criteria,
            default_worker_profile=resolved_profile,
        )
        project_id = str(project.get("project_id") or "")
        worker = client.find_or_resume_worker(
            project_id=project_id,
            owner_id=resolved_owner_id,
            name=title,
            role=clean_success_criteria,
            alias=resolved_alias,
            profile=resolved_profile,
            backend="openclaw",
            execution_mode=resolved_execution_mode,
            bootstrap_bundle=bundle,
            start_synchronously=False,
        )
        worker_id = str(worker.get("worker_id") or "")
        if not worker_id:
            raise ValueError("GlassHive worker create/resume did not return worker_id")
        schedule = client.schedule_run(
            worker_id,
            "\n".join(brief_sections),
            run_at=run_at,
            schedule_text=schedule_text,
            delay_seconds=delay_seconds,
        )
        result: dict[str, Any] = {
            "status": "scheduled",
            "callback_ready": callback_ready,
            "callback_delivery": "optional" if callback_ready else "not_configured_standalone_polling_available",
            "missing_callback_fields": missing_callback_fields,
            "scheduled_for": schedule.get("run_at"),
            "schedule_state": schedule.get("state"),
            "acknowledgement_guidance": (
                "Write one short acknowledgement in your own voice that the workspace is scheduled. "
                "Callbacks are optional; status can be checked through GlassHive MCP. Do not expose "
                "worker/run/provider plumbing unless diagnostics were requested."
            ),
            "delegation_audit": {
                "title": _audit_preview(title, max_chars=180),
                "schedule": _audit_preview(schedule_text or run_at or str(delay_seconds or ""), max_chars=180),
                "instruction_preview": _audit_preview("\n".join(brief_sections)),
                "use_for": "self-check only; do not show the user unless diagnostics were requested",
            },
        }
        if expose_diagnostics:
            result.update(
                {
                    "project_id": project_id,
                    "worker_id": worker_id,
                    "schedule_id": schedule.get("schedule_id"),
                    "execution_mode": resolved_execution_mode,
                    "profile": resolved_profile,
                    "effort": resolved_effort,
                }
            )
        return result

    @server.tool(
        name="project_runs",
        title="Project Runs",
        description=(
            "List recent runs for a project. Use for diagnostics, audit, or explicit status requests about an existing project. "
            "For ordinary follow-up on a previously launched task, prefer workspace_status or workspace_wait. "
            "Returns run ids, states, and recent execution metadata."
        ),
        structured_output=True,
    )
    def project_runs(project_id: str) -> list[dict[str, Any]]:
        return client.list_project_runs(project_id)

    @server.tool(
        name="project_events",
        title="Project Events",
        description=(
            "List recent lifecycle events for a project. Use only for diagnostics, audit trails, or explicit investigation of what happened. "
            "Do not include raw event logs in a normal user-facing answer unless they explain a blocker. Returns event ids, event types, and project-linked timestamps/details when available."
        ),
        structured_output=True,
    )
    def project_events(project_id: str) -> list[dict[str, Any]]:
        return client.list_project_events(project_id)

    @server.tool(
        name="workers_list",
        title="List Workers",
        description=(
            "List workers belonging to a project. Use for explicit worker inventory, resume, or diagnostic requests on an existing project. "
            "Do not list workers as boilerplate before a fresh task; prefer worker_delegate_once. Returns worker ids, states, profiles, aliases, and execution metadata when available."
        ),
        structured_output=True,
    )
    def workers_list(project_id: str) -> list[dict[str, Any]]:
        return client.list_workers(project_id)

    @server.tool(
        name="worker_create",
        title="Create Worker",
        description=(
            "Create a new worker in an existing project. Optionally pass bootstrap_profile and "
            "bootstrap_bundle_json as a JSON string or object to seed auth, MCP config, instructions, env, and project files. "
            "Use this lower-level tool for explicit orchestration or diagnostics; for a fresh one-off task, prefer worker_delegate_once. "
            "Use execution_mode='host' for the user's real computer/session and 'docker' for isolated work. "
            "Returns the worker record with worker_id, execution mode, profile, alias, and bootstrap result metadata."
        ),
        structured_output=True,
    )
    def worker_create(
        project_id: str,
        name: str,
        role: str,
        owner_id: str | None = None,
        profile: ProfileParam = "",
        backend: BackendParam = "openclaw",
        execution_mode: ExecutionModeParam = None,
        alias: str | None = None,
        workspace_root: str | None = None,
        bootstrap_profile: str | None = None,
        bootstrap_bundle_json: BootstrapBundleParam = None,
    ) -> dict[str, Any]:
        parsed_bundle = _normalize_bootstrap_bundle(bootstrap_bundle_json)
        parsed_bundle = _merge_request_context(parsed_bundle)
        resolved_execution_mode = _resolve_execution_mode(execution_mode)
        return client.create_worker(
            project_id=project_id,
            owner_id=_request_owner_id(owner_id),
            name=name,
            role=role,
            profile=profile.strip() or _configured_default_worker_profile(),
            backend=backend,
            execution_mode=resolved_execution_mode,
            alias=alias,
            workspace_root=workspace_root,
            bootstrap_profile=bootstrap_profile,
            bootstrap_bundle=parsed_bundle,
        )

    @server.tool(
        name="worker_find_or_resume",
        title="Find Or Resume Worker",
        description=(
            "Find an existing non-terminated worker by alias for a project/owner, or create one. "
            "Use execution_mode='host' for tasks on the user's real computer/session: signed-in browser profile, desktop apps, local files/projects, installed CLIs, or OS/window control. "
            "Use execution_mode='docker' for isolated sandbox, disposable browser, or risky untrusted work. "
            "Do not use for fresh one-off tasks when worker_delegate_once can create/resume and queue the run in one call. "
            "bootstrap_bundle_json may be a JSON string or object. Returns the existing or newly created worker record."
        ),
        structured_output=True,
    )
    def worker_find_or_resume(
        project_id: str,
        name: str,
        role: str,
        alias: str,
        owner_id: str | None = None,
        profile: ProfileParam = "",
        backend: BackendParam = "openclaw",
        execution_mode: ExecutionModeParam = None,
        workspace_root: str | None = None,
        bootstrap_profile: str | None = None,
        bootstrap_bundle_json: BootstrapBundleParam = None,
    ) -> dict[str, Any]:
        parsed_bundle = _normalize_bootstrap_bundle(bootstrap_bundle_json)
        parsed_bundle = _merge_request_context(parsed_bundle)
        resolved_execution_mode = _resolve_execution_mode(execution_mode)
        return client.find_or_resume_worker(
            project_id=project_id,
            owner_id=_request_owner_id(owner_id),
            name=name,
            role=role,
            alias=alias,
            profile=profile.strip() or _configured_default_worker_profile(),
            backend=backend,
            execution_mode=resolved_execution_mode,
            workspace_root=workspace_root,
            bootstrap_profile=bootstrap_profile,
            bootstrap_bundle=parsed_bundle,
        )

    @server.tool(
        name="worker_get",
        title="Get Worker",
        description=(
            "Fetch a worker by worker_id. Use for explicit worker inspection or when a previous tool result already supplied the id. "
            "Do not expose worker ids to the user unless diagnostics were requested. Returns the worker record with project, state, profile, alias, and execution mode when available."
        ),
        structured_output=True,
    )
    def worker_get(worker_id: str) -> dict[str, Any]:
        return client.get_worker(worker_id)

    @server.tool(
        name="worker_live",
        title="Worker Live State",
        description=(
            "Fetch rich live worker diagnostics, including runtime details, runs, logs, and artifacts. "
            "Use only when the user asks for status, diagnostics, takeover detail, or live workspace evidence. "
            "Returns worker state, runtime details, recent runs, events, artifacts, and blocker evidence when available."
        ),
        structured_output=True,
    )
    def worker_live(worker_id: str) -> dict[str, Any]:
        return client.worker_live(worker_id)

    @server.tool(
        name="worker_run",
        title="Queue Worker Run",
        description=(
            "Queue a new instruction for an existing worker. Use for explicit steering/resume/reuse. "
            "For fresh one-off host/browser/desktop/local tasks, prefer worker_delegate_once. "
            "Returns the queued run record with run_id/state and later completion delivered by callback when configured."
        ),
        structured_output=True,
    )
    def worker_run(worker_id: str, instruction: str) -> dict[str, Any]:
        return client.assign_run(worker_id, instruction)

    @server.tool(
        name="worker_schedule",
        title="Schedule Worker Run",
        description=(
            "Schedule a run for an existing GlassHive worker using GlassHive's own scheduler. "
            "Use when the user asks raw MCP/GlassHive to do something later. Provide run_at, schedule_text such as 'in 20 minutes', or delay_seconds."
        ),
        structured_output=True,
    )
    def worker_schedule(
        worker_id: str,
        instruction: str,
        schedule_text: str | None = None,
        run_at: str | None = None,
        delay_seconds: int | None = None,
    ) -> dict[str, Any]:
        return client.schedule_run(
            worker_id,
            instruction,
            run_at=run_at,
            schedule_text=schedule_text,
            delay_seconds=delay_seconds,
        )

    @server.tool(
        name="worker_schedules",
        title="List Worker Schedules",
        description="List pending or completed GlassHive-native schedules for a worker. Use for explicit scheduling status or QA.",
        structured_output=True,
    )
    def worker_schedules(worker_id: str, include_done: bool = False) -> list[dict[str, Any]]:
        return client.worker_schedules(worker_id, include_done=include_done)

    @server.tool(
        name="worker_message",
        title="Send Worker Message",
        description=(
            "Send an operator message into the current worker session. Use when the user gives follow-up guidance, approval, correction, or an answer to a worker blocker. "
            "Do not use for a fresh task; prefer worker_delegate_once or worker_run depending on whether a reusable worker already exists. Returns the queued message/run state."
        ),
        structured_output=True,
    )
    def worker_message(worker_id: str, message: str) -> dict[str, Any]:
        return client.send_message(worker_id, message)

    @server.tool(
        name="worker_pause",
        title="Pause Worker",
        description=(
            "Pause a worker. Use only when the user asks to pause/hold work or when an explicit safety checkpoint requires stopping active execution. "
            "Docker workers are frozen; host-native workers stop the active process. Returns the lifecycle state. Do not pause routine completed or callback-ready work."
        ),
        structured_output=True,
    )
    def worker_pause(worker_id: str) -> dict[str, Any]:
        return client.lifecycle(worker_id, "pause")

    @server.tool(
        name="worker_resume",
        title="Resume Worker",
        description=(
            "Resume a paused persistent worker. Use when the user explicitly wants held work to continue or after a confirmed checkpoint. "
            "Do not create a new worker for the same task if a paused worker is the intended continuation. Returns the lifecycle state and any blocker from the runtime."
        ),
        structured_output=True,
    )
    def worker_resume(worker_id: str) -> dict[str, Any]:
        return client.lifecycle(worker_id, "resume")

    @server.tool(
        name="worker_interrupt",
        title="Interrupt Worker",
        description=(
            "Interrupt the active task while keeping the worker available. Use when the user changes direction, cancels the current step, or requests immediate steering without destroying the worker. "
            "Do not terminate persistent context unless the user asks. Returns lifecycle state and runtime blocker details when available."
        ),
        structured_output=True,
    )
    def worker_interrupt(worker_id: str) -> dict[str, Any]:
        return client.lifecycle(worker_id, "interrupt")

    @server.tool(
        name="worker_terminate",
        title="Terminate Worker",
        description=(
            "Terminate a worker and cancel active or queued runs. Use only for explicit stop/shutdown/delete-style requests or unrecoverable diagnostics. "
            "Do not terminate when pause, interrupt, or resume would preserve useful context. Returns the final lifecycle state."
        ),
        structured_output=True,
    )
    def worker_terminate(worker_id: str) -> dict[str, Any]:
        return client.lifecycle(worker_id, "terminate")

    @server.tool(
        name="worker_desktop_action",
        title="Launch Worker Desktop Action",
        description=(
            "Launch or focus a worker surface such as terminal, files, browser, codex, claude, or openclaw inside a sandbox or on the host computer. "
            "Use for explicit live viewing, steering, approval, browser opening, or workstation diagnostics. Do not add this to routine worker_delegate_once handoffs. "
            "Returns surface URLs or launch state; raw desktop URLs are diagnostic and should not be user-facing unless a watch/takeover surface is requested."
        ),
        structured_output=True,
    )
    def worker_desktop_action(worker_id: str, action: DesktopActionParam, url: str | None = None) -> dict[str, Any]:
        return client.desktop_action(worker_id, action, url=url)

    @server.tool(
        name="worker_takeover",
        title="Get Worker Takeover URLs",
        description=(
            "Return human-facing GlassHive operator URLs for watch, steer, and takeover. "
            "Use only when the user asks to watch, steer, approve, or take over live work; do not call for routine background delegation. "
            "Use operator_url/watch_url when present; direct_desktop_url is raw diagnostic noVNC only."
        ),
        structured_output=True,
    )
    def worker_takeover(worker_id: str) -> dict[str, Any]:
        live = client.worker_live(worker_id)
        takeover = client.takeover(worker_id)
        runtime_details = live.get("runtime_details", {})
        worker = live.get("worker") if isinstance(live.get("worker"), dict) else {}
        project_id = str((worker or {}).get("project_id") or "").strip()
        request_surface = _header_value(_request_headers(), HEADER_SURFACE)
        can_return_local_urls = surface_can_open_operator_url(request_surface)
        operator_url = surface_aware_watch_url(
            worker_id,
            project_id,
            request_surface=request_surface,
            watch_surface="desktop",
        )
        runtime_takeover_url = takeover.get("url") if can_return_local_urls else None
        direct_desktop_url = runtime_details.get("view_url") if can_return_local_urls else None
        view_url = operator_url or runtime_takeover_url or direct_desktop_url
        takeover_payload = (
            takeover
            if can_return_local_urls
            else {
                "supported": bool(takeover.get("supported")),
                "mode": takeover.get("mode"),
                "url_available": False,
            }
        )
        return {
            "takeover": takeover_payload,
            "operator_url": operator_url,
            "watch_url": operator_url,
            "view_url": view_url,
            "runtime_takeover_url": runtime_takeover_url,
            "direct_desktop_url": direct_desktop_url,
            "terminal_url": f"{base_url}/ui/workers/{worker_id}/terminal" if can_return_local_urls else None,
            "worker_url": f"{base_url}/ui/workers/{worker_id}" if can_return_local_urls else None,
            "project_runs": live.get("project_runs", []),
            "operator_url_available": bool(operator_url),
            "operator_url_surface": request_surface or "web",
        }

    @server.tool(
        name="run_get",
        title="Get Run",
        description=(
            "Fetch an individual run by run_id. Use for explicit diagnostics, status, or result inspection when the run id came from a previous tool result. "
            "For user follow-up, prefer workspace_status for a non-blocking check or workspace_wait for a blocking wait. "
            "Returns run state, output, and blocker/error details."
        ),
        structured_output=True,
    )
    def run_get(run_id: str) -> dict[str, Any]:
        return client.get_run(run_id)

    def _terminal_run_state(run: dict[str, Any]) -> bool:
        return str(run.get("state") or "").strip().lower() in {
            "completed",
            "failed",
            "cancelled",
            "canceled",
            "interrupted",
            "timed_out",
            "timeout",
        }

    def _run_order_key(run: dict[str, Any] | None) -> tuple[float, str]:
        if not run:
            return (0.0, "")
        for key in ("queued_at", "started_at", "ended_at", "created_at", "updated_at"):
            value = str(run.get(key) or "").strip()
            if value:
                try:
                    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    return (parsed.astimezone(timezone.utc).timestamp(), str(run.get("run_id") or "").strip())
                except ValueError:
                    continue
        return (0.0, str(run.get("run_id") or "").strip())

    def _run_is_newer(candidate: dict[str, Any] | None, requested_run: dict[str, Any] | None) -> bool:
        if not candidate:
            return False
        if not requested_run:
            return True
        candidate_id = str(candidate.get("run_id") or "").strip()
        requested_id = str(requested_run.get("run_id") or "").strip()
        return bool(candidate_id and candidate_id != requested_id and _run_order_key(candidate) > _run_order_key(requested_run))

    def _newer_worker_run(
        *,
        requested_run: dict[str, Any] | None,
        worker: dict[str, Any],
        live: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        latest_run_id = str((worker or {}).get("last_run_id") or "").strip()
        if latest_run_id and latest_run_id != str((requested_run or {}).get("run_id") or "").strip():
            try:
                latest = client.get_run(latest_run_id)
            except Exception:
                latest = None
            if _run_is_newer(latest, requested_run):
                return latest

        for key in ("runs", "project_runs"):
            raw_runs = (live or {}).get(key) if isinstance(live, dict) else None
            if not isinstance(raw_runs, list):
                continue
            for item in raw_runs:
                if not isinstance(item, dict):
                    continue
                candidate_id = str(item.get("run_id") or "").strip()
                if not candidate_id:
                    continue
                if candidate_id == str((requested_run or {}).get("run_id") or "").strip():
                    continue
                try:
                    candidate = client.get_run(candidate_id)
                except Exception:
                    candidate = item
                if _run_is_newer(candidate, requested_run):
                    return candidate
        return None

    def _workspace_status_payload(
        *,
        run_id: str | None = None,
        worker_id: str | None = None,
        include_live: bool = True,
    ) -> dict[str, Any]:
        clean_run_id = str(run_id or "").strip()
        clean_worker_id = str(worker_id or "").strip()
        if not clean_run_id and not clean_worker_id:
            raise ValueError("run_id or worker_id is required")
        run: dict[str, Any] | None = client.get_run(clean_run_id) if clean_run_id else None
        if run and not clean_worker_id:
            clean_worker_id = str(run.get("worker_id") or "").strip()
        live: dict[str, Any] | None = None
        if include_live and clean_worker_id:
            live = client.worker_live(clean_worker_id)
        worker = live.get("worker") if isinstance(live, dict) and isinstance(live.get("worker"), dict) else {}
        newer_run = _newer_worker_run(requested_run=run, worker=worker, live=live) if clean_worker_id else None
        requested_run_stale = bool(run and _run_is_newer(newer_run, run))
        if run and _terminal_run_state(run) and newer_run and not _terminal_run_state(newer_run):
            effective_run = run
        elif newer_run and (not run or _run_is_newer(newer_run, run)):
            effective_run = newer_run
        else:
            effective_run = run
        if effective_run and not clean_worker_id:
            clean_worker_id = str(effective_run.get("worker_id") or "").strip()
        project_id = str((worker or {}).get("project_id") or (effective_run or run or {}).get("project_id") or "").strip()
        view_steer_url = None
        if clean_worker_id:
            worker_for_link = worker if worker else {"worker_id": clean_worker_id}
            view_steer_url = _signed_view_steer_url(
                worker_for_link,
                project_id or None,
                _header_value(_request_headers(), HEADER_SURFACE),
            )
        run_state = str((effective_run or {}).get("state") or "").strip() or None
        worker_state = str((worker or {}).get("state") or "").strip() or None
        terminal = bool(effective_run and _terminal_run_state(effective_run))
        artifact_links: dict[str, Any] | None = None
        if terminal and clean_worker_id:
            try:
                artifact_worker = worker if worker else client.get_worker(clean_worker_id)
                artifacts = client.list_artifacts(clean_worker_id)
                artifact_links = _artifact_listing_payload(worker=artifact_worker, artifacts=artifacts)
            except Exception as exc:
                artifact_links = {
                    "status": "unavailable",
                    "items": [],
                    "error": _audit_preview(str(exc), max_chars=220),
                }
        return {
            "status": "ok",
            "mode": "non_blocking",
            "terminal": terminal,
            "run_id": str((effective_run or {}).get("run_id") or clean_run_id or "").strip() or None,
            "requested_run_id": clean_run_id or None,
            "requested_run_state": str((run or {}).get("state") or "").strip() or None,
            "requested_run_stale": requested_run_stale,
            "latest_run_id": str((newer_run or effective_run or {}).get("run_id") or "").strip() or None,
            "latest_run_state": str((newer_run or effective_run or {}).get("state") or "").strip() or None,
            "worker_id": clean_worker_id or None,
            "project_id": project_id or None,
            "run_state": run_state,
            "worker_state": worker_state,
            "output_text": (effective_run or {}).get("output_text") if effective_run else None,
            "error_text": (effective_run or {}).get("error_text") if effective_run else None,
            "view_steer_url": view_steer_url,
            "view_steer": {
                "label": "View / Steer GlassHive workspace",
                "url": view_steer_url,
                "include_in_response": bool(view_steer_url),
            },
            "artifact_links": artifact_links,
            "run": effective_run,
            "requested_run": run,
            "latest_run": newer_run or effective_run,
            "worker_live": live,
            "next_action_guidance": (
                "If requested_run_stale is true, acknowledge the requested run outcome first, then answer from the effective/latest run fields. "
                "If terminal is true, answer from output_text/error_text and include relevant "
                "artifact_links signed_download_url values when present, plus the "
                "View / Steer link when useful. If terminal is false and the user wants you to wait, "
                "call workspace_wait with run_id; otherwise give a brief status and the View / Steer link."
            ),
        }

    @server.tool(
        name="workspace_status",
        title="Check GlassHive Workspace Status",
        description=(
            "Standalone non-blocking status/result check for GlassHive. Use this after workspace_launch, "
            "worker_delegate_once, or worker_run when the user asks whether the work is done, wants the "
            "latest result, or needs a quick status check. Do not call it immediately after launch "
            "unless the user asked for status/diagnostics. Provide run_id from follow_up_context when "
            "available, and worker_id when a live workspace snapshot is useful. This does not require "
            "LibreChat or host-app callback wiring. Returns run state, worker state, output/error text, "
            "and View / Steer link data when available."
        ),
        structured_output=True,
    )
    def workspace_status(
        run_id: Annotated[str | None, Field(description="Run id from follow_up_context. Preferred for result/status checks.")] = None,
        worker_id: Annotated[str | None, Field(description="Worker id from follow_up_context. Include for live state and View / Steer link.")] = None,
        include_live: Annotated[bool, Field(description="Include worker live details when worker_id is available.")] = True,
    ) -> dict[str, Any]:
        return _workspace_status_payload(run_id=run_id, worker_id=worker_id, include_live=include_live)

    @server.tool(
        name="workspace_wait",
        title="Wait For GlassHive Workspace Result",
        description=(
            "Standalone blocking wait for a GlassHive run. Use only when the user explicitly asks you "
            "to wait/check until done, or when a single-turn answer is more important than returning "
            "immediately. For normal long-running work, launch non-blocking first and use "
            "workspace_status later. This does not require LibreChat or host-app callback wiring."
        ),
        structured_output=True,
    )
    def workspace_wait(
        run_id: Annotated[str, Field(description="Run id from workspace_launch/worker_delegate_once follow_up_context.")],
        worker_id: Annotated[str | None, Field(description="Optional worker id for live state and View / Steer link.")] = None,
        timeout_seconds: Annotated[float, Field(description="Maximum seconds to block before returning timeout status.")] = 120.0,
        poll_interval_seconds: Annotated[float, Field(description="Polling interval in seconds.")] = 2.0,
        include_live: Annotated[bool, Field(description="Include worker live details in the final response when available.")] = True,
    ) -> dict[str, Any]:
        clean_run_id = str(run_id or "").strip()
        if not clean_run_id:
            raise ValueError("run_id is required")
        max_wait = float(os.environ.get("WPR_MCP_BLOCKING_WAIT_MAX_SEC", "900") or "900")
        timeout = max(0.0, min(float(timeout_seconds), max_wait))
        interval = max(0.25, min(float(poll_interval_seconds), 10.0))
        deadline = time.monotonic() + timeout
        attempts = 0
        while True:
            attempts += 1
            payload = _workspace_status_payload(
                run_id=clean_run_id,
                worker_id=worker_id,
                include_live=include_live,
            )
            if payload.get("terminal"):
                payload.update(
                    {
                        "status": "completed" if payload.get("run_state") == "completed" else "terminal",
                        "mode": "blocking_wait",
                        "waited": True,
                        "attempts": attempts,
                        "timed_out": False,
                    }
                )
                return payload
            if time.monotonic() >= deadline:
                payload.update(
                    {
                        "status": "timeout",
                        "mode": "blocking_wait",
                        "waited": True,
                        "attempts": attempts,
                        "timed_out": True,
                        "next_action_guidance": (
                            "Tell the user GlassHive is still running, include the View / Steer link "
                            "when present, and offer to check again with workspace_status or workspace_wait."
                        ),
                    }
                )
                return payload
            time.sleep(min(interval, max(0.0, deadline - time.monotonic())))

    @server.tool(
        name="workspace_artifacts",
        title="List GlassHive Workspace Artifacts",
        description=(
            "Use after workspace_status/workspace_wait or when the user asks for generated files, "
            "downloads, artifacts, or delivery files from a GlassHive worker. Returns workspace file "
            "artifacts with short-lived signed_download_url values when GlassHive can safely expose "
            "them. Prefer this instead of pasting generated file contents into chat. Do not use it "
            "before the worker has produced files unless the user asks for diagnostics."
        ),
        structured_output=True,
    )
    def workspace_artifacts(
        worker_id: Annotated[str, Field(description="Worker id from workspace_launch/worker_delegate_once follow_up_context.")],
        include_download_links: Annotated[bool, Field(description="Include short-lived signed download URLs for each file artifact.")] = True,
    ) -> dict[str, Any]:
        worker = client.get_worker(worker_id)
        artifacts = client.list_artifacts(worker_id)
        return _artifact_listing_payload(
            worker=worker,
            artifacts=artifacts,
            include_download_links=include_download_links,
        )

    @server.tool(
        name="workspace_artifact_download",
        title="Get GlassHive Artifact Download Link",
        description=(
            "Use when the user asks to download, open, receive, or inspect one specific file generated "
            "inside a GlassHive workspace. Returns a short-lived signed download URL scoped to that "
            "worker, tenant, user, and path. Prefer workspace_artifacts first when the path is unknown. "
            "Do not expose local filesystem paths or ask the user to manually save pasted content when "
            "a signed download URL is available."
        ),
        structured_output=True,
    )
    def workspace_artifact_download(
        worker_id: Annotated[str, Field(description="Worker id from GlassHive follow_up_context.")],
        path: Annotated[str, Field(description="Workspace-relative file path, for example index.html or output/report.pdf.")],
    ) -> dict[str, Any]:
        worker = client.get_worker(worker_id)
        clean_path = _clean_artifact_relative_path(path)
        if not clean_path:
            raise ValueError("path is required or invalid")
        download_url = _signed_artifact_download_url(worker, clean_path)
        return {
            "status": "ok" if download_url else "unavailable",
            "worker_id": worker_id,
            "path": clean_path,
            "signed_download_url": download_url,
            "download_link_ttl_seconds": signed_link_ttl_seconds(),
            "next_action_guidance": (
                "Give the user the signed_download_url as the GlassHive download link when present. "
                "If unavailable, call workspace_artifacts or include the View / Steer link for manual access."
            ),
        }

    @server.tool(
        name="metrics_summary",
        title="Metrics Summary",
        description=(
            "Fetch runtime-level project, worker, run, and event counts. Use for diagnostics, health checks, capacity checks, or admin-style visibility. "
            "Do not use for ordinary task delegation or user-facing task completion. Returns aggregate counts only, not project content or worker outputs."
        ),
        structured_output=True,
    )
    def metrics_summary() -> dict[str, Any]:
        return client.metrics()

    @server.resource(
        "wpr://projects",
        name="projects",
        title="Workers Projects Runtime Projects",
        description="Current projects visible to the MCP server.",
        mime_type="application/json",
    )
    def projects_resource() -> str:
        return json.dumps(client.list_projects(), indent=2)

    @server.resource(
        "wpr://projects/{project_id}",
        name="project",
        title="Workers Projects Runtime Project",
        description="A single project record.",
        mime_type="application/json",
    )
    def project_resource(project_id: str) -> str:
        return json.dumps(client.get_project(project_id), indent=2)

    @server.resource(
        "wpr://projects/{project_id}/workers",
        name="project-workers",
        title="Workers For Project",
        description="Workers belonging to a project.",
        mime_type="application/json",
    )
    def project_workers_resource(project_id: str) -> str:
        return json.dumps(client.list_workers(project_id), indent=2)

    @server.resource(
        "wpr://workers/{worker_id}",
        name="worker",
        title="Worker Record",
        description="The current worker record.",
        mime_type="application/json",
    )
    def worker_resource(worker_id: str) -> str:
        return json.dumps(client.get_worker(worker_id), indent=2)

    @server.resource(
        "wpr://workers/{worker_id}/live",
        name="worker-live",
        title="Worker Live State",
        description="Rich live state for a worker, including recent runs, events, and runtime details.",
        mime_type="application/json",
    )
    def worker_live_resource(worker_id: str) -> str:
        return json.dumps(client.worker_live(worker_id), indent=2)

    @server.resource(
        "wpr://runs/{run_id}",
        name="run",
        title="Run Record",
        description="A single run record.",
        mime_type="application/json",
    )
    def run_resource(run_id: str) -> str:
        return json.dumps(client.get_run(run_id), indent=2)

    @server.resource(
        "wpr://schedules/{schedule_id}",
        name="schedule",
        title="Schedule Record",
        description="A single GlassHive-native schedule record.",
        mime_type="application/json",
    )
    def schedule_resource(schedule_id: str) -> str:
        return json.dumps(client.get_schedule(schedule_id), indent=2)

    @server.resource(
        "wpr://metrics/summary",
        name="metrics-summary",
        title="Runtime Metrics Summary",
        description="Runtime-level metrics snapshot.",
        mime_type="application/json",
    )
    def metrics_resource() -> str:
        return json.dumps(client.metrics(), indent=2)

    @server.prompt(
        name="delegate_project_goal",
        title="Delegate Project Goal",
        description="Generate an operator-ready brief for a project/worker delegation run.",
    )
    def delegate_project_goal(
        project_id: str,
        worker_id: str,
        task: str,
        checkpoint_instruction: str | None = None,
    ) -> str:
        project = client.get_project(project_id)
        worker = client.get_worker(worker_id)
        checkpoint = checkpoint_instruction or "Pause before risky external writes so a human can review."
        return (
            f"Project: {project['title']}\n"
            f"Goal: {project['goal']}\n"
            f"Worker: {worker['name']} ({worker['profile']})\n"
            f"Task: {task}\n"
            f"Checkpoint: {checkpoint}\n"
            "When useful, fetch worker_live or worker_takeover first so you can monitor the worker and hand off control."
        )

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="Workers & Projects Runtime MCP wrapper")
    parser.add_argument("--transport", choices=["stdio", "streamable-http", "sse"], default=os.environ.get("WPR_MCP_TRANSPORT", "stdio"))
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    _require_enterprise_mcp_transport(args.transport)
    server = create_mcp_server(base_url=args.base_url.rstrip("/"), host=args.host, port=args.port)
    if args.transport == "streamable-http" and _enterprise_mode_enabled():
        import uvicorn

        app = server.streamable_http_app()
        app.add_middleware(EnterpriseMcpHttpAuthMiddleware)
        uvicorn.run(app, host=args.host, port=args.port, access_log=False)
        return
    server.run(transport=args.transport)


if __name__ == "__main__":
    main()
