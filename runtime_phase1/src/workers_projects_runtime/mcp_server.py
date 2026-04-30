from __future__ import annotations

import argparse
import base64
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

import httpx
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from .runtime_env import load_viventium_runtime_env

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
HEADER_USER_ID = "x-viventium-user-id"
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


def _default_execution_mode() -> str:
    if not _host_workers_enabled():
        return "docker"
    mode = os.environ.get("WPR_DEFAULT_EXECUTION_MODE", "docker").strip().lower()
    return mode if mode in {"docker", "host"} else "docker"


def _host_workers_enabled() -> bool:
    value = os.environ.get("GLASSHIVE_HOST_WORKERS_ENABLED", "true").strip().lower()
    return value not in {"0", "false", "no", "off", "disabled"}


def _resolve_execution_mode(value: str | None) -> str:
    mode = str(value or "").strip().lower()
    if not mode:
        mode = _default_execution_mode()
    if mode not in {"docker", "host"}:
        raise ValueError("execution_mode must be either 'docker' or 'host'")
    if mode == "host" and not _host_workers_enabled():
        raise ValueError("host-native GlassHive workers are disabled by Viventium config")
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


def _request_headers() -> dict[str, str]:
    if get_http_headers is None:
        return {}
    try:
        return _normalize_headers(get_http_headers())
    except Exception:
        return {}


def _request_owner_id(owner_id: str | None) -> str | None:
    explicit = _sanitize_context_value(owner_id)
    if explicit:
        return explicit
    return _sanitize_context_value(_request_headers().get(HEADER_USER_ID)) or None


def _sanitize_context_value(value: str | None) -> str:
    stripped = str(value or "").strip()
    if stripped.startswith("{{") and stripped.endswith("}}"):
        return ""
    if stripped.startswith("${") and stripped.endswith("}"):
        return ""
    return stripped


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


def _trusted_virtual_upload_source(value: str) -> str:
    root = os.environ.get("WPR_LIBRECHAT_UPLOADS_ROOT", "").strip()
    if not root or not value.startswith("/uploads/"):
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
    return str(Path(root).expanduser() / relative_path)


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


def _project_upload_file_entry(file_obj: dict[str, Any], index: int) -> dict[str, Any] | None:
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
    trusted_source = _trusted_virtual_upload_source(source_ref)
    if trusted_source:
        return {
            "scope": "workspace",
            "path": f"uploads/{filename}",
            "source_path": trusted_source,
            **metadata,
        }
    text = file_obj.get("text")
    if isinstance(text, str) and text.strip():
        text_filename = filename if filename.lower().endswith((".txt", ".md", ".csv", ".json")) else f"{filename}.txt"
        return {
            "scope": "workspace",
            "path": f"uploads/{text_filename}",
            "content": text,
            **metadata,
        }
    if not metadata:
        return None
    manifest_name = f"{filename}.metadata.json"
    return {
        "scope": "workspace",
        "path": f"uploads/{manifest_name}",
        "content": json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        **{key: value for key, value in metadata.items() if key != "source_ref"},
    }


def _project_upload_files(upload_context: dict[str, Any]) -> list[dict[str, Any]]:
    projected: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for file_obj in _iter_upload_file_objects(upload_context):
        entry = _project_upload_file_entry(file_obj, len(projected) + 1)
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
    callback_url = os.environ.get("VIVENTIUM_GLASSHIVE_CALLBACK_URL", "").strip()
    callback_secret = os.environ.get("VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", "").strip()
    context = {
        "user_id": _sanitize_context_value(headers.get(HEADER_USER_ID)),
        "agent_id": _sanitize_context_value(headers.get(HEADER_AGENT_ID)),
        "conversation_id": _sanitize_context_value(headers.get(HEADER_CONVERSATION_ID)),
        "parent_message_id": _sanitize_context_value(headers.get(HEADER_PARENT_MESSAGE_ID)),
        "message_id": _sanitize_context_value(headers.get(HEADER_MESSAGE_ID)),
        "surface": _sanitize_context_value(headers.get(HEADER_SURFACE)),
        "input_mode": _sanitize_context_value(headers.get(HEADER_INPUT_MODE)),
        "stream_id": _sanitize_context_value(headers.get(HEADER_STREAM_ID)),
        "voice_call_session_id": _sanitize_context_value(headers.get(HEADER_VOICE_CALL_SESSION_ID)),
        "voice_request_id": _sanitize_context_value(headers.get(HEADER_VOICE_REQUEST_ID)),
        "telegram_chat_id": _sanitize_context_value(headers.get(HEADER_TELEGRAM_CHAT_ID)),
        "telegram_user_id": _sanitize_context_value(headers.get(HEADER_TELEGRAM_USER_ID)),
        "telegram_message_id": _sanitize_context_value(headers.get(HEADER_TELEGRAM_MESSAGE_ID)),
    }
    context = {key: value for key, value in context.items() if value}
    upload_context = {
        "request_files": _decode_json_header(headers.get(HEADER_REQUEST_FILES)),
        "request_attachments": _decode_json_header(headers.get(HEADER_REQUEST_ATTACHMENTS)),
        "tool_resources": _decode_json_header(headers.get(HEADER_TOOL_RESOURCES)),
        "file_ids": _decode_json_header(headers.get(HEADER_FILE_IDS)),
    }
    upload_context = {key: value for key, value in upload_context.items() if value not in (None, "", [], {})}
    if not context and not callback_url and not upload_context:
        return bundle
    merged: dict[str, Any] = dict(bundle or {})
    existing_callbacks = merged.get("callbacks")
    callbacks = dict(existing_callbacks) if isinstance(existing_callbacks, dict) else {}
    callbacks.update({key: value for key, value in context.items() if value})
    if callback_url:
        callbacks.setdefault("events_webhook_url", callback_url)
    if callback_secret:
        callbacks.setdefault("hmac_secret", callback_secret)
    if callbacks:
        merged["callbacks"] = callbacks
    if context:
        merged.setdefault("viventium_context", context)
    if upload_context:
        merged["viventium_upload_context"] = upload_context
        projected = _project_upload_files(upload_context)
        if projected:
            merged["files"] = _merge_bundle_files(merged.get("files"), projected)
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
            },
        )

    def get_worker(self, worker_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/workers/{worker_id}")

    def worker_live(self, worker_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v1/workers/{worker_id}/live")

    def worker_runs(self, worker_id: str) -> list[dict[str, Any]]:
        return self._request("GET", f"/v1/workers/{worker_id}/runs").get("items", [])

    def worker_events(self, worker_id: str) -> list[dict[str, Any]]:
        return self._request("GET", f"/v1/workers/{worker_id}/events").get("items", [])

    def assign_run(self, worker_id: str, instruction: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/workers/{worker_id}/assign", json_body={"instruction": instruction})

    def send_message(self, worker_id: str, message: str) -> dict[str, Any]:
        return self._request("POST", f"/v1/workers/{worker_id}/message", json_body={"message": message})

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
    host_instruction = (
        "Set execution_mode='host' when the task depends on the user's real computer/session: logged-in browser profile, desktop apps, local files/projects, installed CLIs, or OS/window control. "
        if _host_workers_enabled()
        else "Host-native workers are disabled by Viventium config; do not request execution_mode='host'. "
    )
    server = FastMCP(
        name="glass-hive",
        instructions=(
            "Use this server to manage persistent projects, resumable workers, workstation sandboxes, and host-native workers in Glass Hive. "
            f"When execution_mode is omitted, MCP worker tools use the configured default '{_default_execution_mode()}'. "
            f"{host_instruction}"
            "If the user asks to open, navigate, click, type, inspect, or read from a real browser/desktop/local app, dispatch the action through a worker instead of answering from memory or inference. "
            "Prefer codex-cli for available host browser/desktop/file/code execution, claude-code when Claude is explicitly requested, and openclaw-general only when installed or explicitly requested. "
            "For fresh one-off host/browser/desktop/local tasks, prefer worker_delegate_once instead of manually listing projects and chaining low-level project/worker tools. "
            "When worker_delegate_once returns callback_ready=true, do not immediately call worker_live or run_get in the same chat turn unless the user explicitly asked for diagnostics or live status; acknowledge briefly and let the callback deliver completion or blockers. "
            "Preserve the user's success condition and output constraints in worker instructions; if the user asks for a short or exact answer, do not add screenshots, logs, IDs, artifact paths, or extra evidence to the user-visible final result unless needed to explain a blocker. "
            "If acknowledging delegation to the user, use a short outcome-focused status and avoid worker/run/provider/queue jargon unless diagnostics were requested. "
            "Use worker_takeover or worker_desktop_action for explicit diagnostics, explicit live takeover, or concrete checkpoint/approval moments; do not add them to routine worker_delegate_once handoffs."
        ),
        host=host,
        port=port,
        streamable_http_path="/mcp",
    )

    @server.tool(
        name="projects_list",
        title="List Projects",
        description=(
            "List current projects from the standalone Workers & Projects runtime. Use for explicit status, audit, or resume requests. "
            "For a fresh delegation, prefer worker_delegate_once instead of listing every project or chaining low-level tools."
        ),
        structured_output=True,
    )
    def projects_list(owner_id: str | None = None) -> list[dict[str, Any]]:
        return client.list_projects(owner_id=owner_id)

    @server.tool(
        name="project_create",
        title="Create Project",
        description="Create a new project with a goal and default worker profile.",
        structured_output=True,
    )
    def project_create(
        title: str,
        goal: str,
        owner_id: str | None = None,
        default_worker_profile: str = "openclaw-general",
    ) -> dict[str, Any]:
        return client.create_project(
            owner_id=_request_owner_id(owner_id),
            title=title,
            goal=goal,
            default_worker_profile=default_worker_profile,
        )

    @server.tool(name="project_get", title="Get Project", description="Fetch a single project by project_id.", structured_output=True)
    def project_get(project_id: str) -> dict[str, Any]:
        return client.get_project(project_id)

    @server.tool(
        name="worker_delegate_once",
        title="Delegate Task",
        description=(
            "One-call fresh-task delegation for GlassHive. Use this for new host/browser/desktop/local-file tasks "
            "instead of manually listing projects and chaining project_create, worker_create, and worker_run. "
            "It creates a human-named project when project_id is omitted, finds or resumes a worker by alias, "
            "merges callback/upload context, queues the run, and returns one clean dispatch result. "
            "When callback_ready=true, use the returned user_status and stop the chat turn; do not immediately call worker_live or run_get unless the user explicitly asked for diagnostics or live status. "
            "Preserve the user's requested final-answer format in the instruction, especially short/exact-answer constraints. "
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
        profile: ProfileParam = "codex-cli",
        backend: BackendParam = "openclaw",
        execution_mode: ExecutionModeParam = None,
        workspace_root: str | None = None,
        bootstrap_profile: str | None = None,
        bootstrap_bundle_json: BootstrapBundleParam = None,
        require_callback: bool = True,
        expose_diagnostics: bool = False,
    ) -> dict[str, Any]:
        resolved_owner_id = _request_owner_id(owner_id)
        resolved_execution_mode = _resolve_execution_mode(execution_mode)
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
        missing_callback_fields = _callback_missing_fields(bundle)
        callback_ready = not missing_callback_fields
        if require_callback and not callback_ready:
            return {
                "status": "blocked",
                "user_status": (
                    "I can't start that as background work yet because I do not have a reliable way "
                    "to send the result back here."
                ),
                "execution_mode": resolved_execution_mode,
                "profile": profile,
                "alias": (alias or _slugify_alias(profile, clean_title)).strip(),
                "callback_ready": False,
                "missing_callback_fields": missing_callback_fields,
            }

        project = client.get_project(project_id) if project_id else client.create_project(
            owner_id=resolved_owner_id,
            title=clean_title,
            goal=clean_goal,
            default_worker_profile=profile,
        )
        resolved_project_id = str(project.get("project_id") or project_id or "").strip()
        if not resolved_project_id:
            raise ValueError("GlassHive project creation did not return project_id")

        resolved_alias = (alias or _slugify_alias(profile, clean_title)).strip()
        worker = client.find_or_resume_worker(
            project_id=resolved_project_id,
            owner_id=resolved_owner_id,
            name=(worker_name or clean_title).strip(),
            role=(worker_role or clean_goal or clean_instruction).strip(),
            alias=resolved_alias,
            profile=profile,
            backend=backend,
            execution_mode=resolved_execution_mode,
            workspace_root=workspace_root,
            bootstrap_profile=bootstrap_profile,
            bootstrap_bundle=bundle,
        )
        worker_id = str(worker.get("worker_id") or "").strip()
        if not worker_id:
            raise ValueError("GlassHive worker create/resume did not return worker_id")

        run = client.assign_run(worker_id, clean_instruction)
        result: dict[str, Any] = {
            "status": "dispatched",
            "user_status": "On it - working on that now. I'll send the result here.",
            "main_agent_next_action": (
                "Send user_status and stop this chat turn. Do not call worker_live or run_get unless "
                "the user explicitly asked for diagnostics/live status, or callback_ready is false."
            ),
            "callback_ready": callback_ready,
        }
        if expose_diagnostics:
            result.update(
                {
                    "project_id": resolved_project_id,
                    "worker_id": worker_id,
                    "run_id": run.get("run_id"),
                    "run_state": run.get("state"),
                    "execution_mode": resolved_execution_mode,
                    "profile": profile,
                    "alias": resolved_alias,
                }
            )
        return result

    @server.tool(name="project_runs", title="Project Runs", description="List recent runs for a project.", structured_output=True)
    def project_runs(project_id: str) -> list[dict[str, Any]]:
        return client.list_project_runs(project_id)

    @server.tool(name="project_events", title="Project Events", description="List recent events for a project.", structured_output=True)
    def project_events(project_id: str) -> list[dict[str, Any]]:
        return client.list_project_events(project_id)

    @server.tool(name="workers_list", title="List Workers", description="List workers belonging to a project.", structured_output=True)
    def workers_list(project_id: str) -> list[dict[str, Any]]:
        return client.list_workers(project_id)

    @server.tool(
        name="worker_create",
        title="Create Worker",
        description=(
            "Create a new worker in an existing project. Optionally pass bootstrap_profile and "
            "bootstrap_bundle_json as a JSON string or object to seed auth, MCP config, instructions, env, and project files. "
            "Use this lower-level tool for explicit orchestration or diagnostics; for a fresh one-off task, prefer worker_delegate_once. "
            "Use execution_mode='host' for the user's real computer/session and 'docker' for isolated work."
        ),
        structured_output=True,
    )
    def worker_create(
        project_id: str,
        name: str,
        role: str,
        owner_id: str | None = None,
        profile: ProfileParam = "openclaw-general",
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
            profile=profile,
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
            "bootstrap_bundle_json may be a JSON string or object."
        ),
        structured_output=True,
    )
    def worker_find_or_resume(
        project_id: str,
        name: str,
        role: str,
        alias: str,
        owner_id: str | None = None,
        profile: ProfileParam = "openclaw-general",
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
            profile=profile,
            backend=backend,
            execution_mode=resolved_execution_mode,
            workspace_root=workspace_root,
            bootstrap_profile=bootstrap_profile,
            bootstrap_bundle=parsed_bundle,
        )

    @server.tool(name="worker_get", title="Get Worker", description="Fetch a worker by worker_id.", structured_output=True)
    def worker_get(worker_id: str) -> dict[str, Any]:
        return client.get_worker(worker_id)

    @server.tool(
        name="worker_live",
        title="Worker Live State",
        description=(
            "Fetch rich live worker diagnostics, including runtime details, runs, logs, and artifacts. "
            "Use only when the user asks for status/diagnostics or when callback_ready is false; do not poll immediately after worker_delegate_once."
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
            "For fresh one-off host/browser/desktop/local tasks, prefer worker_delegate_once."
        ),
        structured_output=True,
    )
    def worker_run(worker_id: str, instruction: str) -> dict[str, Any]:
        return client.assign_run(worker_id, instruction)

    @server.tool(name="worker_message", title="Send Worker Message", description="Send an operator message into the current worker session.", structured_output=True)
    def worker_message(worker_id: str, message: str) -> dict[str, Any]:
        return client.send_message(worker_id, message)

    @server.tool(name="worker_pause", title="Pause Worker", description="Pause a worker. Docker workers are frozen; host-native workers stop the active process.", structured_output=True)
    def worker_pause(worker_id: str) -> dict[str, Any]:
        return client.lifecycle(worker_id, "pause")

    @server.tool(name="worker_resume", title="Resume Worker", description="Resume a paused persistent worker.", structured_output=True)
    def worker_resume(worker_id: str) -> dict[str, Any]:
        return client.lifecycle(worker_id, "resume")

    @server.tool(name="worker_interrupt", title="Interrupt Worker", description="Interrupt the active task while keeping the worker available.", structured_output=True)
    def worker_interrupt(worker_id: str) -> dict[str, Any]:
        return client.lifecycle(worker_id, "interrupt")

    @server.tool(name="worker_terminate", title="Terminate Worker", description="Terminate a worker and cancel any active or queued runs.", structured_output=True)
    def worker_terminate(worker_id: str) -> dict[str, Any]:
        return client.lifecycle(worker_id, "terminate")

    @server.tool(
        name="worker_desktop_action",
        title="Launch Worker Desktop Action",
        description="Launch a worker surface such as terminal, files, browser, codex, claude, or openclaw inside a sandbox or on the host computer.",
        structured_output=True,
    )
    def worker_desktop_action(worker_id: str, action: DesktopActionParam, url: str | None = None) -> dict[str, Any]:
        return client.desktop_action(worker_id, action, url=url)

    @server.tool(
        name="worker_takeover",
        title="Get Worker Takeover URLs",
        description="Return takeover URLs for the worker desktop and terminal surfaces so a human can watch or intervene.",
        structured_output=True,
    )
    def worker_takeover(worker_id: str) -> dict[str, Any]:
        live = client.worker_live(worker_id)
        takeover = client.takeover(worker_id)
        runtime_details = live.get("runtime_details", {})
        return {
            "takeover": takeover,
            "view_url": runtime_details.get("view_url") or takeover.get("url"),
            "terminal_url": f"{base_url}/ui/workers/{worker_id}/terminal",
            "worker_url": f"{base_url}/ui/workers/{worker_id}",
            "project_runs": live.get("project_runs", []),
        }

    @server.tool(name="run_get", title="Get Run", description="Fetch an individual run by run_id.", structured_output=True)
    def run_get(run_id: str) -> dict[str, Any]:
        return client.get_run(run_id)

    @server.tool(name="metrics_summary", title="Metrics Summary", description="Fetch runtime-level project, worker, run, and event counts.", structured_output=True)
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

    server = create_mcp_server(base_url=args.base_url.rstrip("/"), host=args.host, port=args.port)
    server.run(transport=args.transport)


if __name__ == "__main__":
    main()
