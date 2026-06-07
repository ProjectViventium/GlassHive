from __future__ import annotations

import os
import re
import asyncio
import shlex
import sqlite3
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, parse_qsl, quote, urlencode, unquote, urlparse

import httpx
import websockets
from fastapi import Body, FastAPI, HTTPException, Request, WebSocket
from starlette.websockets import WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .prompt_template import (
    build_operator_brief,
    build_project_title,
    desktop_action_for_launch,
    initial_watch_surface_for_launch,
    normalize_launch_surface,
)
from .runtime_client import RuntimeClient
from .signed_links import sign_link_token, verify_signed_link_token

STATIC_DIR = Path(__file__).resolve().parent / "static"
SAFE_WORKER_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
SAFE_UPLOAD_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
NOVNC_VIEW_URL_CACHE_TTL_SECONDS = 15.0
NOVNC_ASSET_CACHE_TTL_SECONDS = 10 * 60.0
NOVNC_ASSET_CACHE_MAX_BYTES = 2 * 1024 * 1024
RUNTIME_ENV_KEYS = {
    "GLASSHIVE_ENTERPRISE_MODE",
    "WPR_ENTERPRISE_MODE",
    "GLASSHIVE_AUTH_MODE",
    "GLASSHIVE_ENTERPRISE_TENANT_ID",
    "WPR_ENTERPRISE_TENANT_ID",
    "GLASSHIVE_OPERATOR_BASE_URL",
    "GLASSHIVE_RUNTIME_BASE_URL",
    "GLASSHIVE_SIGNED_LINK_SECRET",
    "GLASSHIVE_TRUST_INBOUND_IDENTITY",
    "WPR_API_TOKEN",
}
_NOVNC_VIEW_URL_CACHE: dict[str, tuple[float, str]] = {}
_NOVNC_ASSET_CACHE: dict[str, tuple[float, int, bytes, str]] = {}
_NOVNC_HTTP_CLIENT: httpx.Client | None = None


def _watch_session_cap_seconds() -> int:
    raw = os.environ.get("GLASSHIVE_MAX_WATCH_SESSION_DURATION_S", "").strip()
    try:
        value = int(raw) if raw else 0
    except ValueError:
        value = 0
    return max(0, min(value, 24 * 3600))


def _watch_session_state_path() -> Path:
    raw = str(os.environ.get("GLASSHIVE_WATCH_SESSION_STATE_PATH") or "").strip()
    if raw:
        return Path(raw).expanduser()
    state_root = (
        Path(os.environ["XDG_STATE_HOME"]).expanduser()
        if os.environ.get("XDG_STATE_HOME")
        else Path.home() / ".local" / "state"
    )
    return state_root / "glasshive" / "watch_sessions.sqlite3"


def _watch_session_conn() -> sqlite3.Connection:
    db_path = _watch_session_state_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS watch_sessions (
            tenant_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            worker_id TEXT NOT NULL,
            started_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            PRIMARY KEY (tenant_id, owner_id, worker_id)
        )
        """
    )
    return conn


def _watch_session_expires_at(worker_id: str, identity: dict[str, str] | None) -> int | None:
    cap_seconds = _watch_session_cap_seconds()
    if cap_seconds <= 0 or not identity:
        return None
    tenant_id = str(identity.get("tenant_id") or "").strip()
    owner_id = str(identity.get("user_id") or "").strip()
    if not tenant_id or not owner_id:
        return None
    now = int(time.time())
    with _watch_session_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM watch_sessions WHERE expires_at < ?", (now - 24 * 3600,))
        row = conn.execute(
            """
            SELECT expires_at FROM watch_sessions
            WHERE tenant_id = ? AND owner_id = ? AND worker_id = ?
            """,
            (tenant_id, owner_id, worker_id),
        ).fetchone()
        if row is not None and int(row[0]) > now:
            expires_at = int(row[0])
            conn.execute(
                """
                UPDATE watch_sessions
                SET updated_at = ?
                WHERE tenant_id = ? AND owner_id = ? AND worker_id = ?
                """,
                (now, tenant_id, owner_id, worker_id),
            )
            return expires_at
        expires_at = now + cap_seconds
        conn.execute(
            """
            INSERT INTO watch_sessions (tenant_id, owner_id, worker_id, started_at, expires_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(tenant_id, owner_id, worker_id) DO UPDATE SET
                started_at = excluded.started_at,
                expires_at = excluded.expires_at,
                updated_at = excluded.updated_at
            """,
            (tenant_id, owner_id, worker_id, now, expires_at, now),
        )
        return expires_at


def _existing_watch_session_expires_at(worker_id: str, identity: dict[str, str] | None) -> int | None:
    if _watch_session_cap_seconds() <= 0 or not identity:
        return None
    tenant_id = str(identity.get("tenant_id") or "").strip()
    owner_id = str(identity.get("user_id") or "").strip()
    if not tenant_id or not owner_id:
        return None
    with _watch_session_conn() as conn:
        row = conn.execute(
            """
            SELECT expires_at FROM watch_sessions
            WHERE tenant_id = ? AND owner_id = ? AND worker_id = ?
            """,
            (tenant_id, owner_id, worker_id),
        ).fetchone()
    return int(row[0]) if row is not None else None


def _ensure_signed_worker_watch_session(worker_id: str, payload: dict[str, object]) -> None:
    if str(payload.get("kind") or "") != "worker_view":
        return
    identity = {
        "tenant_id": str(payload.get("tenant_id") or "").strip(),
        "user_id": str(payload.get("owner_id") or "").strip(),
    }
    existing = _existing_watch_session_expires_at(worker_id, identity)
    now = int(time.time())
    if existing is not None and existing > now:
        return
    _watch_session_expires_at(worker_id, identity)


def _load_viventium_runtime_env() -> None:
    candidates: list[Path] = []
    explicit = os.environ.get("VIVENTIUM_ENV_FILE", "").strip()
    if explicit:
        candidates.append(Path(explicit).expanduser())
    if os.environ.get("VIVENTIUM_DISABLE_DEFAULT_RUNTIME_ENV", "").strip().lower() not in {"1", "true", "yes", "on"}:
        app_support = Path.home() / "Library" / "Application Support" / "Viventium" / "runtime"
        candidates.extend([app_support / "runtime.env", app_support / "runtime.local.env"])
    for env_path in candidates:
        try:
            lines = env_path.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            try:
                part = shlex.split(stripped, comments=True, posix=True)[0]
            except ValueError:
                continue
            key, _, value = part.partition("=")
            if key in RUNTIME_ENV_KEYS and not os.environ.get(key):
                os.environ[key] = value


def _novnc_http_client() -> httpx.Client:
    global _NOVNC_HTTP_CLIENT
    if _NOVNC_HTTP_CLIENT is None:
        _NOVNC_HTTP_CLIENT = httpx.Client(
            timeout=httpx.Timeout(30.0, connect=3.0),
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
        )
    return _NOVNC_HTTP_CLIENT


def _fetch_novnc_asset(target: str) -> httpx.Response:
    global _NOVNC_HTTP_CLIENT
    try:
        return _novnc_http_client().get(target)
    except httpx.HTTPError:
        if _NOVNC_HTTP_CLIENT is not None:
            close = getattr(_NOVNC_HTTP_CLIENT, "close", None)
            if callable(close):
                close()
            _NOVNC_HTTP_CLIENT = None
        return _novnc_http_client().get(target)


class UploadedFileRequest(BaseModel):
    name: str = Field(min_length=1)
    mime_type: str | None = None
    size: int | None = Field(default=None, ge=0)
    content_base64: str = Field(min_length=1)


class LaunchRequest(BaseModel):
    description: str = Field(min_length=1)
    success_criteria: str = Field(min_length=1)
    context: str | None = None
    workspace_option: str | None = None
    workspace_type: str | None = None
    worker_option: str | None = None
    launch_surface: str | None = None
    schedule_text: str | None = None
    effort: str | None = None
    files: list[UploadedFileRequest] = Field(default_factory=list)


class PreferencesRequest(BaseModel):
    default_worker_profile: str | None = None
    codex_reasoning_effort: str | None = None
    claude_effort: str | None = None
    openclaw_effort: str | None = None


class MessageRequest(BaseModel):
    message: str = Field(min_length=1)


class ActionRequest(BaseModel):
    url: str | None = None


class MetadataRequest(BaseModel):
    favorite: bool | None = None
    name: str | None = None


def _append_signed_worker_token(url: str, worker_id: str, identity: dict[str, str] | None) -> str:
    if not identity:
        return url
    ttl_seconds = None
    expires_at = _watch_session_expires_at(worker_id, identity)
    if expires_at is not None:
        ttl_seconds = max(1, expires_at - int(time.time()))
    token = sign_link_token(
        kind="worker_view",
        worker_id=worker_id,
        tenant_id=str(identity.get("tenant_id") or ""),
        owner_id=str(identity.get("user_id") or ""),
        ttl_seconds=ttl_seconds,
    )
    if not token:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}gh_token={quote(token)}"


def flatten_workspaces(client: RuntimeClient, identity: dict[str, str] | None = None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen_worker_ids: set[str] = set()
    active_states = {"created", "starting", "queued", "running", "resuming"}
    resumable_states = {"ready", "paused", "idle", "idle_terminated", "stopped"}
    for project in client.list_projects():
        project_id = str(project["project_id"])
        for worker in client.list_workers(project_id):
            worker_state = str(worker.get("state") or "").strip().lower()
            if worker_state == "terminated":
                continue
            project_title = str(project.get("title") or project_id)
            worker_name = str(worker.get("name") or worker["worker_id"])
            worker_id = str(worker["worker_id"])
            if worker_id in seen_worker_ids:
                continue
            seen_worker_ids.add(worker_id)
            is_active = worker_state in active_states
            is_resumable = worker_state in resumable_states
            state_label = "retained" if worker_state == "ready" else (worker.get("state") or "")
            watch_url = f"/watch/{worker_id}?project_id={project_id}&surface=desktop"
            project_url = f"/ui/projects/{project_id}?worker_id={worker_id}"
            desktop_url = f"/desktop/{worker_id}"
            api_url = f"/api/worker/{worker_id}"
            items.append(
                {
                    "project_id": project_id,
                    "project_title": project_title,
                    "worker_id": worker_id,
                    "name": worker_name,
                    "workspace_label": project_title or worker_name,
                    "profile": worker.get("profile") or "",
                    "state": worker.get("state") or "",
                    "favorite": bool(worker.get("favorite")),
                    "is_active": is_active,
                    "is_resumable": is_resumable,
                    "state_label": state_label,
                    "watch_url": _append_signed_worker_token(watch_url, worker_id, identity),
                    "project_url": _append_signed_worker_token(project_url, worker_id, identity),
                    "desktop_url": _append_signed_worker_token(desktop_url, worker_id, identity),
                    "api_url": _append_signed_worker_token(api_url, worker_id, identity),
                }
            )
    return items


def _project_title_for_worker(client: RuntimeClient, project_id: str) -> str:
    try:
        project = client.get_project(project_id)
        return str(project.get("title") or project_id)
    except Exception:
        return project_id


def _launch_browser_url(description: str) -> str | None:
    lowered = description.strip()
    match = re.search(r"https?://[^\s<>'\"`]+", lowered, flags=re.IGNORECASE)
    if match:
        return match.group(0).rstrip(").,;")
    domain = re.search(r"\b([a-z0-9-]+(?:\.[a-z0-9-]+)+)\b", lowered, flags=re.IGNORECASE)
    if domain:
        host = domain.group(1)
        if host.lower() not in {"localhost", "127.0.0.1", "0.0.0.0"}:
            return f"https://{host}"
    return None


def _env_flag(name: str, default: bool) -> bool:
    raw = str(os.environ.get(name, "")).strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _truthy_env(name: str) -> bool:
    return _env_flag(name, False)


def _validate_enterprise_startup() -> None:
    enterprise = _truthy_env("GLASSHIVE_ENTERPRISE_MODE") or _truthy_env("WPR_ENTERPRISE_MODE")
    if not enterprise:
        return
    api_token = str(os.environ.get("WPR_API_TOKEN") or "").strip()
    signed_link_secret = str(os.environ.get("GLASSHIVE_SIGNED_LINK_SECRET") or "").strip()
    if not api_token:
        raise RuntimeError("GlassHive enterprise UI requires WPR_API_TOKEN for runtime service auth")
    if not signed_link_secret:
        raise RuntimeError("GlassHive enterprise UI requires GLASSHIVE_SIGNED_LINK_SECRET")
    if signed_link_secret == api_token:
        raise RuntimeError("GlassHive enterprise UI requires GLASSHIVE_SIGNED_LINK_SECRET to differ from WPR_API_TOKEN")


def _default_launch_surface() -> str:
    return normalize_launch_surface(os.environ.get("GLASSHIVE_DEFAULT_LAUNCH_SURFACE", "desktop"))


def _show_live_terminal_in_desktop() -> bool:
    return _env_flag("GLASSHIVE_SHOW_LIVE_TERMINAL_IN_DESKTOP", True)


def _launch_surface_options() -> list[dict[str, str]]:
    return [
        {
            "value": "desktop",
            "label": "Live desktop",
            "description": "Open the workstation desktop first. This is the recommended default.",
        },
        {
            "value": "terminal",
            "label": "Exact live session",
            "description": "Open the raw live terminal session first instead of the desktop.",
        },
        {
            "value": "auto",
            "label": "Auto",
            "description": "Let GlassHive choose the initial surface from the task type.",
        },
    ]


def _workspace_type_options() -> list[dict[str, object]]:
    host_available = _env_flag("GLASSHIVE_HOST_WORKERS_ENABLED", True)
    options: list[dict[str, object]] = [
        {
            "value": "sandboxed",
            "label": "Sandboxed Workspace",
            "description": "Runs on managed GlassHive workspace compute with project files and browser state preserved for resume.",
            "disabled": False,
        }
    ]
    if host_available:
        options.append(
            {
                "value": "host",
                "label": "Your Computer",
                "description": "Runs on this computer with host-native tools. Not available in Azure enterprise mode.",
                "disabled": False,
            }
        )
    return options


def _default_workspace_type() -> str:
    default_mode = str(os.environ.get("WPR_DEFAULT_EXECUTION_MODE") or "docker").strip().lower()
    if default_mode == "host" and _env_flag("GLASSHIVE_HOST_WORKERS_ENABLED", True):
        return "host"
    return "sandboxed"


def _new_workspace_options() -> list[dict[str, str]]:
    options = [
        {"value": "new:codex-cli", "label": "Codex worker", "profile": "codex-cli"},
        {"value": "new:claude-code", "label": "Claude Code worker", "profile": "claude-code"},
        {"value": "new:openclaw-general", "label": "OpenClaw worker", "profile": "openclaw-general"},
    ]
    raw = (
        os.environ.get("GLASSHIVE_ALLOWED_WORKER_PROFILES", "").strip()
        or os.environ.get("WPR_ALLOWED_WORKER_PROFILES", "").strip()
    )
    if not raw:
        return options
    allowed = {item.strip() for item in raw.split(",") if item.strip()}
    filtered = [item for item in options if item["profile"] in allowed]
    if filtered:
        return filtered
    raise RuntimeError("GLASSHIVE_ALLOWED_WORKER_PROFILES must include at least one supported worker profile")


def _default_worker_profile() -> str:
    configured = str(os.environ.get("GLASSHIVE_DEFAULT_WORKER_PROFILE") or "").strip()
    profile = configured or "codex-cli"
    options = _new_workspace_options()
    available = {item["profile"] for item in options}
    if profile in available:
        return profile
    if configured:
        raise RuntimeError(
            "GLASSHIVE_DEFAULT_WORKER_PROFILE must be included in GLASSHIVE_ALLOWED_WORKER_PROFILES"
        )
    return str(options[0]["profile"]) if options else "codex-cli"


def _profile_allowed(profile: str) -> bool:
    if not profile:
        return False
    return profile in {item["profile"] for item in _new_workspace_options()}


def _default_workspace_option(preferences: dict[str, Any] | None = None) -> str:
    preferred = str((preferences or {}).get("default_worker_profile") or "").strip()
    profile = preferred if _profile_allowed(preferred) else _default_worker_profile()
    return f"new:{profile}"


def _effort_for_profile(profile: str, explicit_effort: str | None, preferences: dict[str, Any] | None) -> str:
    explicit = str(explicit_effort or "").strip().lower()
    if explicit:
        return explicit
    prefs = preferences or {}
    if profile == "codex-cli":
        return str(prefs.get("codex_reasoning_effort") or "").strip().lower()
    if profile == "claude-code":
        return str(prefs.get("claude_effort") or "").strip().lower()
    if profile == "openclaw-general":
        return str(prefs.get("openclaw_effort") or "").strip().lower()
    return ""


def _bootstrap_bundle_with_effort(bundle: dict[str, Any] | None, profile: str, effort: str) -> dict[str, Any] | None:
    clean_effort = str(effort or "").strip().lower()
    if not clean_effort:
        return bundle
    next_bundle: dict[str, Any] = dict(bundle or {})
    if profile == "codex-cli":
        if clean_effort not in {"minimal", "low", "medium", "high", "xhigh"}:
            raise HTTPException(status_code=400, detail="Codex effort must be minimal, low, medium, high, or xhigh")
        env = dict(next_bundle.get("env") or {})
        env["WPR_CODEX_CLI_REASONING_EFFORT"] = clean_effort
        next_bundle["env"] = env
        return next_bundle
    if profile == "claude-code":
        if clean_effort not in {"default", "max"}:
            raise HTTPException(status_code=400, detail="Claude effort must be default or max")
    elif profile == "openclaw-general":
        if clean_effort not in {"default", "high", "max"}:
            raise HTTPException(status_code=400, detail="OpenClaw effort must be default, high, or max")
    else:
        return next_bundle
    if clean_effort == "default":
        return next_bundle
    current = str(next_bundle.get("system_instructions") or "").strip()
    addition = f"Worker effort preference for this run: {clean_effort}."
    next_bundle["system_instructions"] = f"{current}\n\n{addition}".strip()
    return next_bundle


def _execution_mode_from_workspace_type(workspace_type: str | None) -> str:
    requested = str(workspace_type or _default_workspace_type()).strip().lower()
    if requested == "host":
        if _env_flag("GLASSHIVE_HOST_WORKERS_ENABLED", True):
            return "host"
        raise HTTPException(status_code=400, detail="Your Computer workspaces are not available in this GlassHive mode")
    return "docker"


def _format_launch_error(exc: Exception) -> str:
    detail = str(exc).strip()
    if detail:
        return detail
    return "The project launch failed before the first run could start."


def _safe_upload_name(name: str, fallback: str) -> str:
    raw = str(name or "").replace("\\", "/").rsplit("/", 1)[-1].strip() or fallback
    safe = SAFE_UPLOAD_NAME_RE.sub("-", raw).strip(".-")
    return safe[:160] or fallback


def _bootstrap_bundle_for_uploads(files: list[UploadedFileRequest]) -> dict[str, Any] | None:
    if not files:
        return None
    max_files = int(os.environ.get("GLASSHIVE_UI_UPLOAD_MAX_FILES", "12"))
    max_bytes = int(os.environ.get("GLASSHIVE_UI_UPLOAD_MAX_BYTES", str(20 * 1024 * 1024)))
    entries: list[dict[str, Any]] = []
    total_size = 0
    for index, upload in enumerate(files[:max(max_files, 0)], start=1):
        safe_name = _safe_upload_name(upload.name, f"upload-{index}")
        raw_content = str(upload.content_base64 or "").strip()
        declared_size = upload.size if upload.size is not None else int((len(raw_content) * 3) / 4)
        total_size += max(0, int(declared_size))
        if total_size > max_bytes:
            raise HTTPException(status_code=413, detail="Uploaded files exceed the configured GlassHive UI upload limit")
        entries.append(
            {
                "scope": "workspace",
                "path": f"uploads/{safe_name}",
                "encoding": "base64",
                "content_base64": raw_content,
                "filename": safe_name,
                "mime_type": upload.mime_type or "",
                "bytes": declared_size,
            }
        )
    if not entries:
        return None
    upload_list = "\n".join(f"- uploads/{entry['filename']}" for entry in entries)
    return {
        "files": entries,
        "system_instructions": (
            "The user attached files for this run. They are available inside the workspace under:\n"
            f"{upload_list}\n\n"
            "Use those files directly when they are relevant. Mention user-facing artifacts/files only "
            "when you intentionally create them, they are needed, or the user asked for them; do not "
            "force a downloadable file when a concise chat result satisfies the request."
        ),
    }


def _launch_desktop_surfaces(
    client: RuntimeClient,
    *,
    worker_id: str,
    profile: str,
    description: str,
    run_id: str,
    surface: str,
) -> None:
    if surface != "desktop":
        return
    action = desktop_action_for_launch(profile, description)
    browser_url = _launch_browser_url(description) if action == "browser" else None
    if action == "browser" and browser_url:
        client.desktop_action(worker_id, "browser", url=browser_url)
    if _show_live_terminal_in_desktop():
        client.desktop_action(worker_id, "terminal", run_id=run_id)


def create_app(runtime_client: RuntimeClient | None = None) -> FastAPI:
    _load_viventium_runtime_env()
    _validate_enterprise_startup()
    client = runtime_client or RuntimeClient()
    enterprise = _truthy_env("GLASSHIVE_ENTERPRISE_MODE") or _truthy_env("WPR_ENTERPRISE_MODE")
    app = FastAPI(
        title="GlassHive",
        version="0.1.0",
        docs_url=None if enterprise else "/docs",
        redoc_url=None if enterprise else "/redoc",
        openapi_url=None if enterprise else "/openapi.json",
    )
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    def _incoming_identity_header(request: Request, name: str) -> str:
        aliases = {
            "X-Viventium-Tenant-Id": ("X-GlassHive-Tenant-Id", "X-LibreChat-Tenant-Id"),
            "X-Viventium-User-Id": ("X-GlassHive-User-Id", "X-LibreChat-User-Id"),
            "X-Viventium-User-Email": ("X-GlassHive-User-Email", "X-LibreChat-User-Email"),
            "X-Viventium-User-Role": ("X-GlassHive-User-Role", "X-LibreChat-User-Role"),
        }
        for candidate in (name, *aliases.get(name, ())):
            value = str(request.headers.get(candidate) or "").strip()
            if value:
                return value
        return ""

    def _enterprise_mode_enabled() -> bool:
        return _truthy_env("GLASSHIVE_ENTERPRISE_MODE") or _truthy_env("WPR_ENTERPRISE_MODE")

    def _enterprise_tenant_id() -> str:
        return str(
            os.environ.get("GLASSHIVE_ENTERPRISE_TENANT_ID")
            or os.environ.get("WPR_ENTERPRISE_TENANT_ID")
            or ""
        ).strip()

    def _allow_default_owner() -> bool:
        if not _enterprise_mode_enabled():
            return True
        return _truthy_env("GLASSHIVE_ALLOW_LOCAL_DEMO_OWNER")

    def _worker_cookie_name(worker_id: str) -> str:
        if not SAFE_WORKER_ID_RE.match(str(worker_id or "")):
            raise HTTPException(status_code=400, detail="Invalid worker id")
        return f"glasshive_gh_token_{worker_id}"

    def _signed_token_from_request(request: Request) -> str:
        token = str(request.query_params.get("gh_token") or "").strip()
        if token:
            return token
        path = str(request.url.path or "")
        if path.startswith("/v1/signed-links/"):
            token = unquote(path.removeprefix("/v1/signed-links/")).strip()
            if token:
                return token
        worker_id = str(request.path_params.get("worker_id") or "").strip()
        if worker_id:
            token = str(request.cookies.get(_worker_cookie_name(worker_id)) or "").strip()
            if token:
                return token
        referer = str(request.headers.get("referer") or "").strip()
        if not referer:
            return ""
        parsed = urlparse(referer)
        return str(parse_qs(parsed.query).get("gh_token", [""])[0]).strip()

    def _request_uses_https(request: Request) -> bool:
        forwarded_proto = str(request.headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip().lower()
        return request.url.scheme == "https" or forwarded_proto == "https" or _truthy_env("GLASSHIVE_COOKIE_SECURE")

    def _set_signed_worker_cookie(response: Response, request: Request, worker_id: str) -> None:
        token = _signed_token_from_request(request)
        payload = verify_signed_link_token(token) if token else None
        if (
            payload
            and str(payload.get("kind") or "") == "worker_view"
            and str(payload.get("worker_id") or "").strip() == str(worker_id or "").strip()
        ):
            try:
                cookie_max_age = max(1, min(30 * 60, int(payload.get("exp") or 0) - int(time.time())))
            except (TypeError, ValueError):
                cookie_max_age = 30 * 60
            response.set_cookie(
                _worker_cookie_name(worker_id),
                token,
                max_age=cookie_max_age,
                httponly=True,
                samesite="lax",
                secure=_request_uses_https(request),
            )

    def _file_response_with_signed_cookie(request: Request, worker_id: str, path: Path) -> FileResponse:
        response = FileResponse(path)
        response.headers["Referrer-Policy"] = "same-origin"
        _set_signed_worker_cookie(response, request, worker_id)
        return response

    def _allowed_signed_link_kinds(request: Request | WebSocket) -> set[str]:
        path = str(request.url.path or "")
        if path.startswith("/v1/signed-links/"):
            return {"artifact_download", "artifact_open"}
        return {"worker_view"}

    def _signed_link_payload(request: Request | WebSocket, worker_id: str | None = None) -> dict[str, object] | None:
        token = _signed_token_from_request(request)
        if not token:
            return None
        payload = verify_signed_link_token(token)
        if not payload:
            raise HTTPException(status_code=401, detail="Invalid or expired GlassHive workspace link")
        if str(payload.get("kind") or "") not in _allowed_signed_link_kinds(request):
            raise HTTPException(status_code=403, detail="This GlassHive link cannot open a workspace")
        token_worker_id = str(payload.get("worker_id") or "").strip()
        if worker_id and token_worker_id != worker_id:
            raise HTTPException(status_code=403, detail="This GlassHive link is for a different workspace")
        token_tenant_id = str(payload.get("tenant_id") or "").strip()
        deployment_tenant_id = _enterprise_tenant_id()
        if _enterprise_mode_enabled() and deployment_tenant_id and token_tenant_id != deployment_tenant_id:
            raise HTTPException(status_code=401, detail="GlassHive workspace link is for a different tenant")
        if str(payload.get("kind") or "") == "worker_view" and token_worker_id:
            _ensure_signed_worker_watch_session(token_worker_id, payload)
        return payload

    def _signed_link_identity(request: Request | WebSocket, worker_id: str | None = None) -> dict[str, str] | None:
        payload = _signed_link_payload(request, worker_id)
        if not payload:
            return None
        token_tenant_id = str(payload.get("tenant_id") or "").strip()
        return {
            "tenant_id": token_tenant_id,
            "user_id": str(payload.get("owner_id") or "").strip(),
            "email": "",
            "role": "member",
        }

    def _watch_session_timeout_seconds(request: Request | WebSocket, worker_id: str) -> float:
        raw = os.environ.get("GLASSHIVE_MAX_WATCH_SESSION_DURATION_S", "").strip()
        try:
            configured = int(raw) if raw else 0
        except ValueError:
            configured = 0
        payload = _signed_link_payload(request, worker_id)
        now = int(time.time())
        signed_remaining = 0
        if payload:
            try:
                signed_remaining = int(payload.get("exp") or 0) - now
            except (TypeError, ValueError):
                signed_remaining = 0
        persisted_remaining = 0
        if payload and str(payload.get("kind") or "") == "worker_view":
            persisted = _existing_watch_session_expires_at(
                worker_id,
                {
                    "tenant_id": str(payload.get("tenant_id") or "").strip(),
                    "user_id": str(payload.get("owner_id") or "").strip(),
                },
            )
            if persisted is not None:
                persisted_remaining = persisted - now
        values = [value for value in (configured, signed_remaining, persisted_remaining) if value > 0]
        return float(max(1, min(values))) if values else 0.0

    def _request_identity(request: Request, worker_id: str | None = None) -> dict[str, str]:
        signed_identity = _signed_link_identity(request, worker_id)
        if signed_identity is not None:
            return signed_identity

        enterprise = _enterprise_mode_enabled()
        trust_inbound_identity = _truthy_env("GLASSHIVE_TRUST_INBOUND_IDENTITY")
        tenant_id = _enterprise_tenant_id()
        user_id = str(os.environ.get("GLASSHIVE_DEFAULT_OWNER_ID") or "demo-owner").strip()
        email = ""
        role = ""

        if trust_inbound_identity:
            asserted_tenant = _incoming_identity_header(request, "X-Viventium-Tenant-Id")
            asserted_user = _incoming_identity_header(request, "X-Viventium-User-Id")
            if enterprise and asserted_tenant and tenant_id and asserted_tenant != tenant_id:
                raise HTTPException(status_code=401, detail="GlassHive tenant assertion does not match this deployment")
            tenant_id = asserted_tenant or tenant_id
            user_id = asserted_user or (user_id if _allow_default_owner() else "")
            email = _incoming_identity_header(request, "X-Viventium-User-Email")
            role = _incoming_identity_header(request, "X-Viventium-User-Role")
        elif not _allow_default_owner():
            user_id = ""

        if enterprise and not user_id:
            raise HTTPException(
                status_code=401,
                detail="GlassHive enterprise UI requires an authenticated user assertion from the trusted proxy",
            )

        return {
            "tenant_id": tenant_id,
            "user_id": user_id,
            "email": email,
            "role": role,
        }

    def _runtime_headers_for_request(
        request: Request,
        worker_id: str | None = None,
        *,
        role_override: str | None = None,
    ) -> dict[str, str]:
        api_token = str(os.environ.get("WPR_API_TOKEN") or "").strip()
        if not api_token:
            if _enterprise_mode_enabled():
                raise HTTPException(status_code=503, detail="GlassHive enterprise UI is missing service authentication")
            return {}
        identity = _request_identity(request, worker_id)
        headers = {"X-WPR-Token": api_token}
        if identity["tenant_id"]:
            headers["X-Viventium-Tenant-Id"] = identity["tenant_id"]
        if identity["user_id"]:
            headers["X-Viventium-User-Id"] = identity["user_id"]
        if identity["email"]:
            headers["X-Viventium-User-Email"] = identity["email"]
        role = role_override or identity["role"]
        if role:
            headers["X-Viventium-User-Role"] = role
        return headers

    def _client_for_request(
        request: Request,
        worker_id: str | None = None,
        *,
        internal_details: bool = False,
    ) -> RuntimeClient:
        # The browser should never receive raw noVNC/runtime internals, but the
        # UI backend needs them to proxy the scoped desktop surface.
        role_override = "operator" if internal_details else None
        headers = _runtime_headers_for_request(request, worker_id, role_override=role_override)
        if not headers or not hasattr(client, "with_headers"):
            return client
        return client.with_headers(headers)

    def _require_ui_auth(request: Request, worker_id: str | None = None) -> None:
        _runtime_headers_for_request(request, worker_id)

    def _owner_id_for_request(request: Request) -> str:
        identity = _request_identity(request)
        return identity["user_id"] or os.environ.get("GLASSHIVE_DEFAULT_OWNER_ID", "demo-owner")

    def _browser_live_payload(payload: dict[str, Any]) -> dict[str, Any]:
        safe = dict(payload)
        worker = dict(safe.get("worker") or {})
        for key in ("gateway_url", "gateway_token", "session_key", "workspace_dir", "state_dir", "home_dir", "container_name"):
            worker.pop(key, None)
        safe["worker"] = worker
        runtime = dict(safe.get("runtime_details") or {})
        view_available = bool(runtime.get("view_url"))
        safe["runtime_details"] = {
            key: runtime.get(key)
            for key in ("mode", "runtime", "sandbox_state")
            if runtime.get(key) not in (None, "", [])
        }
        safe["runtime_details"]["view_available"] = view_available
        return safe

    def _worker_live_or_http(active_client: RuntimeClient, worker_id: str) -> dict[str, Any]:
        try:
            return active_client.worker_live(worker_id)
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code if exc.response is not None else 502
            raise HTTPException(status_code=status_code, detail="GlassHive worker is not available") from exc

    def _validated_novnc_asset_path(asset_path: str) -> str:
        normalized = str(asset_path or "").strip().lstrip("/")
        if (
            not normalized
            or normalized.startswith(".")
            or "\\" in normalized
            or ".." in Path(normalized).parts
            or not re.fullmatch(r"[A-Za-z0-9_./-]+", normalized)
        ):
            raise HTTPException(status_code=400, detail="Invalid noVNC asset path")
        return normalized

    def _validated_novnc_ws_path(path: str) -> str:
        normalized = str(path or "websockify").strip().lstrip("/") or "websockify"
        if "\\" in normalized or ".." in Path(normalized).parts or not re.fullmatch(r"[A-Za-z0-9_./-]+", normalized):
            raise HTTPException(status_code=400, detail="Invalid noVNC websocket path")
        return normalized

    def _runtime_view_url(active_client: RuntimeClient, worker_id: str, *, cache_key: str | None = None) -> str:
        now = time.monotonic()
        if cache_key:
            cached = _NOVNC_VIEW_URL_CACHE.get(cache_key)
            if cached and cached[0] > now:
                return cached[1]
        payload = _worker_live_or_http(active_client, worker_id)
        runtime = payload.get("runtime_details") or {}
        view_url = str(runtime.get("view_url") or "").strip()
        if not view_url:
            raise HTTPException(status_code=404, detail="No live desktop is available for this worker")
        if cache_key:
            _NOVNC_VIEW_URL_CACHE[cache_key] = (now + NOVNC_VIEW_URL_CACHE_TTL_SECONDS, view_url)
        return view_url

    def _novnc_view_cache_key(request: Request, worker_id: str) -> str:
        identity = _request_identity(request, worker_id)
        tenant_id = identity.get("tenant_id", "")
        user_id = identity.get("user_id", "")
        return f"{tenant_id}:{user_id}:{worker_id}"

    def _cached_novnc_asset(target: str) -> tuple[int, bytes, str] | None:
        cached = _NOVNC_ASSET_CACHE.get(target)
        if not cached:
            return None
        expires_at, status_code, content, content_type = cached
        if expires_at <= time.monotonic():
            _NOVNC_ASSET_CACHE.pop(target, None)
            return None
        return status_code, content, content_type

    def _store_novnc_asset(target: str, response: httpx.Response) -> None:
        content = response.content
        if response.status_code != 200 or len(content) > NOVNC_ASSET_CACHE_MAX_BYTES:
            return
        content_type = response.headers.get("content-type", "")
        _NOVNC_ASSET_CACHE[target] = (
            time.monotonic() + NOVNC_ASSET_CACHE_TTL_SECONDS,
            response.status_code,
            content,
            content_type,
        )

    def _novnc_asset_response(status_code: int, content: bytes, content_type: str) -> Response:
        response = Response(content=content, status_code=status_code, media_type=content_type or None)
        response.headers["Cache-Control"] = "private, max-age=3600"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def _runtime_status_detail(exc: httpx.HTTPStatusError, fallback: str) -> str:
        response = exc.response
        if response is None:
            return fallback
        try:
            body = response.json()
        except ValueError:
            return fallback
        detail = body.get("detail") if isinstance(body, dict) else None
        if isinstance(detail, str) and detail.strip():
            return detail.strip()
        return fallback

    def _runtime_proxy_base_url() -> str:
        return str(getattr(client, "base_url", "") or os.environ.get("GLASSHIVE_RUNTIME_BASE_URL", "http://127.0.0.1:8766")).rstrip("/")

    def _upstream_safe_query(raw_query: str) -> str:
        signed_query_keys = {"gh_token", "gh_sig", "gh_exp", "gh_kind"}
        pairs = [
            (key, value)
            for key, value in parse_qsl(str(raw_query or ""), keep_blank_values=True)
            if key not in signed_query_keys
        ]
        return urlencode(pairs, doseq=True)

    def _worker_id_from_runtime_proxy_path(path: str, request: Request) -> str | None:
        parts = [part for part in str(path or "").split("/") if part]
        if parts and parts[0] == "workers" and len(parts) >= 2:
            return parts[1]
        query_worker_id = str(request.query_params.get("worker_id") or "").strip()
        return query_worker_id or None

    async def _runtime_proxy(prefix: str, path: str, request: Request) -> Response:
        worker_id = _worker_id_from_runtime_proxy_path(path, request)
        auth_headers = _runtime_headers_for_request(request, worker_id)
        upstream_headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() in {"accept", "content-type"}
        }
        upstream_headers.update(auth_headers)
        target = f"{_runtime_proxy_base_url()}/{prefix}/{path}"
        upstream_query = _upstream_safe_query(str(request.url.query or ""))
        if upstream_query:
            target = f"{target}?{upstream_query}"
        try:
            async with httpx.AsyncClient(timeout=60.0, follow_redirects=False) as upstream:
                upstream_response = await upstream.request(
                    request.method,
                    target,
                    headers=upstream_headers,
                    content=await request.body(),
                )
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail="GlassHive runtime proxy failed") from exc
        response_headers = {
            key: value
            for key, value in upstream_response.headers.items()
            if key.lower() not in {"content-length", "connection", "transfer-encoding"}
        }
        response = Response(
            content=upstream_response.content,
            status_code=upstream_response.status_code,
            headers=response_headers,
            media_type=upstream_response.headers.get("content-type"),
        )
        if worker_id:
            _set_signed_worker_cookie(response, request, worker_id)
        return response

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "runtime": client.health()}

    @app.get("/favicon.ico")
    def favicon() -> FileResponse:
        return FileResponse(STATIC_DIR / "favicon.svg")

    @app.get("/api/bootstrap")
    def bootstrap(request: Request) -> dict[str, Any]:
        active_client = _client_for_request(request)
        identity = _request_identity(request)
        owner_id = identity["user_id"] or os.environ.get("GLASSHIVE_DEFAULT_OWNER_ID", "demo-owner")
        try:
            preferences = active_client.get_preferences()
        except Exception:
            preferences = {}
        existing_workspaces = flatten_workspaces(active_client, identity=identity)
        return {
            "owner_id": owner_id,
            "user_preferences": preferences,
            "default_workspace_option": _default_workspace_option(preferences),
            "deployment_default_workspace_option": f"new:{_default_worker_profile()}",
            "default_launch_surface": _default_launch_surface(),
            "launch_surface_options": _launch_surface_options(),
            "default_workspace_type": _default_workspace_type(),
            "workspace_type_options": _workspace_type_options(),
            "new_workspace_options": _new_workspace_options(),
            "existing_workspaces": existing_workspaces,
        }

    @app.patch("/api/preferences")
    def update_preferences(request: Request, payload: PreferencesRequest) -> dict[str, Any]:
        payload_dict = (
            payload.model_dump(exclude_none=True)
            if hasattr(payload, "model_dump")
            else payload.dict(exclude_none=True)
        )
        return _client_for_request(request).update_preferences(payload_dict)

    @app.post("/api/launch")
    def launch(request: Request, payload: LaunchRequest) -> dict[str, Any]:
        active_client = _client_for_request(request)
        identity = _request_identity(request)
        owner_id = identity["user_id"] or os.environ.get("GLASSHIVE_DEFAULT_OWNER_ID", "demo-owner")
        brief = build_operator_brief(payload.description, payload.success_criteria, payload.context)
        try:
            preferences = active_client.get_preferences()
        except Exception:
            preferences = {}
        workspace_option = payload.workspace_option or payload.worker_option or _default_workspace_option(preferences)
        schedule_text = str(payload.schedule_text or "").strip()
        bootstrap_bundle = _bootstrap_bundle_for_uploads(payload.files)
        execution_mode = _execution_mode_from_workspace_type(payload.workspace_type)
        project_id: str
        worker_id: str | None = None
        profile: str
        created_new_worker = False

        try:
            if workspace_option.startswith("open:") or workspace_option.startswith("existing:"):
                worker_id = workspace_option.split(":", 1)[1]
                worker = active_client.get_worker(worker_id)
                project_id = str(worker["project_id"])
                profile = str(worker.get("profile") or "codex-cli")
            elif workspace_option.startswith("duplicate:"):
                source_worker_id = workspace_option.split(":", 1)[1]
                source_worker = active_client.get_worker(source_worker_id)
                profile = str(source_worker.get("profile") or "codex-cli")
                project = active_client.create_project(owner_id, build_project_title(payload.description), payload.description.strip(), profile)
                project_id = str(project["project_id"])
                worker = active_client.duplicate_worker(project_id, source_worker_id, owner_id)
                worker_id = str(worker["worker_id"])
                created_new_worker = True
            else:
                profile = workspace_option.split(":", 1)[1] if ":" in workspace_option else _default_worker_profile()
                bootstrap_bundle = _bootstrap_bundle_with_effort(
                    bootstrap_bundle,
                    profile,
                    _effort_for_profile(profile, payload.effort, preferences),
                )
                project = active_client.create_project(owner_id, build_project_title(payload.description), payload.description.strip(), profile)
                project_id = str(project["project_id"])
                worker = active_client.create_worker(
                    project_id,
                    owner_id,
                    profile,
                    name=build_project_title(payload.description),
                    role=payload.success_criteria.strip()[:160] or "main",
                    bootstrap_bundle=bootstrap_bundle,
                    execution_mode=execution_mode,
                    start_synchronously=not bool(schedule_text),
                )
                worker_id = str(worker["worker_id"])
                created_new_worker = True
            scheduled = None
            run = None
            if schedule_text:
                scheduled = active_client.schedule_run(str(worker_id), brief, schedule_text=schedule_text)
            else:
                run = active_client.assign_run(str(worker_id), brief)
        except Exception as exc:
            reason = _format_launch_error(exc)
            if created_new_worker and worker_id:
                try:
                    active_client.launch_failed(str(worker_id), reason)
                except Exception:
                    pass
            raise HTTPException(status_code=502, detail=reason) from exc

        surface = initial_watch_surface_for_launch(
            profile,
            payload.description,
            launch_surface=payload.launch_surface or _default_launch_surface(),
        )
        if not scheduled:
            try:
                _launch_desktop_surfaces(
                    active_client,
                    worker_id=str(worker_id),
                    profile=profile,
                    description=payload.description,
                    run_id=str((run or {}).get("run_id") or ""),
                    surface=surface,
                )
            except Exception:
                pass

        watch_url = f"/watch/{worker_id}?project_id={project_id}&surface={surface}"
        return {
            "project_id": project_id,
            "worker_id": str(worker_id),
            "run_id": (run or {}).get("run_id"),
            "schedule_id": (scheduled or {}).get("schedule_id"),
            "scheduled_for": (scheduled or {}).get("run_at"),
            "status": "scheduled" if scheduled else "dispatched",
            "watch_url": _append_signed_worker_token(watch_url, str(worker_id), identity),
        }

    @app.get("/api/worker/{worker_id}/live")
    def worker_live(request: Request, worker_id: str) -> dict[str, Any]:
        active_client = _client_for_request(request, worker_id, internal_details=True)
        payload = _worker_live_or_http(active_client, worker_id)
        worker = payload.get("worker") or {}
        project_id = str(worker.get("project_id") or "")
        payload["project_title"] = _project_title_for_worker(active_client, project_id) if project_id else ""
        return _browser_live_payload(payload)

    @app.get("/novnc/{worker_id}/{asset_path:path}")
    def novnc_asset(request: Request, worker_id: str, asset_path: str) -> Response:
        active_client = _client_for_request(request, worker_id, internal_details=True)
        safe_asset_path = _validated_novnc_asset_path(asset_path)
        view_url = _runtime_view_url(
            active_client,
            worker_id,
            cache_key=_novnc_view_cache_key(request, worker_id),
        )
        parsed = urlparse(view_url)
        if not parsed.scheme or not parsed.netloc:
            raise HTTPException(status_code=400, detail="Invalid live desktop URL")
        target = f"{parsed.scheme}://{parsed.netloc}/{safe_asset_path}"
        cached = _cached_novnc_asset(target)
        if cached:
            return _novnc_asset_response(*cached)
        try:
            upstream_response = _fetch_novnc_asset(target)
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail="Live desktop asset proxy failed") from exc
        _store_novnc_asset(target, upstream_response)
        return _novnc_asset_response(
            upstream_response.status_code,
            upstream_response.content,
            upstream_response.headers.get("content-type", ""),
        )

    @app.websocket("/novnc/{worker_id}/websockify")
    async def novnc_websocket(websocket: WebSocket, worker_id: str) -> None:
        try:
            active_client = _client_for_request(websocket, worker_id, internal_details=True)
            view_url = _runtime_view_url(active_client, worker_id)
            parsed = urlparse(view_url)
            if not parsed.scheme or not parsed.netloc:
                raise HTTPException(status_code=400, detail="Invalid live desktop URL")
            query = parse_qs(parsed.query)
            ws_path = _validated_novnc_ws_path((query.get("path") or ["websockify"])[0])
            ws_scheme = "wss" if parsed.scheme == "https" else "ws"
            upstream_url = f"{ws_scheme}://{parsed.netloc}/{ws_path}"
            session_timeout = _watch_session_timeout_seconds(websocket, worker_id)
        except HTTPException:
            await websocket.close(code=1008)
            return

        await websocket.accept()
        try:
            async with websockets.connect(upstream_url, max_size=None) as upstream:
                async def browser_to_sandbox() -> None:
                    while True:
                        message = await websocket.receive()
                        message_type = message.get("type")
                        if message_type == "websocket.disconnect":
                            await upstream.close()
                            return
                        if message.get("bytes") is not None:
                            await upstream.send(message["bytes"])
                        elif message.get("text") is not None:
                            await upstream.send(message["text"])

                async def sandbox_to_browser() -> None:
                    async for message in upstream:
                        if isinstance(message, bytes):
                            await websocket.send_bytes(message)
                        else:
                            await websocket.send_text(str(message))

                async def enforce_session_timeout() -> None:
                    await asyncio.sleep(session_timeout)
                    await upstream.close()
                    await websocket.close(code=1008, reason="GlassHive watch session expired")

                tasks = {
                    asyncio.create_task(browser_to_sandbox()),
                    asyncio.create_task(sandbox_to_browser()),
                }
                if session_timeout > 0:
                    tasks.add(asyncio.create_task(enforce_session_timeout()))
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                for task in done:
                    task.result()
        except WebSocketDisconnect:
            return
        except Exception:
            try:
                await websocket.close(code=1011)
            except Exception:
                return

    @app.post("/api/worker/{worker_id}/message")
    def worker_message(request: Request, worker_id: str, payload: MessageRequest) -> dict[str, Any]:
        return _client_for_request(request, worker_id).message(worker_id, payload.message)

    @app.post("/api/worker/{worker_id}/steer")
    def worker_steer(request: Request, worker_id: str, payload: MessageRequest) -> dict[str, Any]:
        return _client_for_request(request, worker_id).steer(worker_id, payload.message)

    @app.api_route("/api/worker/{worker_id}/metadata", methods=["POST", "PATCH"])
    def worker_metadata(request: Request, worker_id: str, payload: MetadataRequest) -> dict[str, Any]:
        payload_dict = (
            payload.model_dump(exclude_none=True)
            if hasattr(payload, "model_dump")
            else payload.dict(exclude_none=True)
        )
        return _client_for_request(request, worker_id).update_worker_metadata(
            worker_id,
            payload_dict,
        )

    @app.post("/api/worker/{worker_id}/action/{action}")
    def worker_action(
        request: Request,
        worker_id: str,
        action: str,
        payload: ActionRequest | None = Body(default=None),
    ) -> dict[str, Any]:
        active_client = _client_for_request(request, worker_id)
        try:
            if action in {"pause", "resume", "interrupt", "terminate"}:
                return active_client.lifecycle(worker_id, action)
            if action in {"terminal", "files", "browser", "focus_browser", "codex", "claude", "openclaw"}:
                return active_client.desktop_action(worker_id, action, url=(payload.url if payload else None))
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code if exc.response is not None else 502
            raise HTTPException(
                status_code=status_code,
                detail=_runtime_status_detail(exc, "GlassHive could not apply that workspace action yet."),
            ) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail="GlassHive workspace action failed") from exc
        raise HTTPException(status_code=400, detail=f"Unsupported action: {action}")

    @app.api_route("/ui/{runtime_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
    async def runtime_ui_proxy(runtime_path: str, request: Request) -> Response:
        return await _runtime_proxy("ui", runtime_path, request)

    @app.api_route("/v1/{runtime_path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
    async def runtime_v1_proxy(runtime_path: str, request: Request) -> Response:
        return await _runtime_proxy("v1", runtime_path, request)

    @app.get("/")
    def home(request: Request) -> FileResponse:
        _require_ui_auth(request)
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/watch/{worker_id}")
    def watch(request: Request, worker_id: str) -> FileResponse:
        _require_ui_auth(request, worker_id)
        return _file_response_with_signed_cookie(request, worker_id, STATIC_DIR / "watch.html")

    @app.get("/desktop/{worker_id}")
    def desktop(request: Request, worker_id: str) -> FileResponse:
        _require_ui_auth(request, worker_id)
        return _file_response_with_signed_cookie(request, worker_id, STATIC_DIR / "desktop.html")

    return app


app = create_app()
