from __future__ import annotations

import os
import json
import hmac
from contextlib import asynccontextmanager
from html import escape
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

from .auth import AuthContext, EnterpriseAuthSettings, GlassHiveAuthError, scoped_alias
from .deliverables import deliverable_payload
from .models import (
    AssignRunRequest,
    CreateProjectRequest,
    CreateWorkerRequest,
    DesktopActionRequest,
    DesktopActionResponse,
    DuplicateWorkerRequest,
    EventResponse,
    LaunchFailureRequest,
    MetricsSummary,
    ProjectResponse,
    RunResponse,
    ScheduleResponse,
    ScheduleRunRequest,
    SendMessageRequest,
    TakeoverInfo,
    UpdateWorkerMetadataRequest,
    WorkerResponse,
)
from .openclaw_runtime import StubRuntime, WorkerRuntime
from .profile_runtime import ProfiledWorkerRuntime, _redact_text
from .runtime_env import load_viventium_runtime_env
from .service import GlassHiveProfileNotAllowedError, GlassHiveQuotaExceededError, HostWorkersDisabledError, WorkersProjectsService
from .signed_links import append_signed_query, sign_link_params, verify_signed_link, verify_signed_link_token
from .store import Store
from .terminal_takeover import TerminalTarget, bridge_terminal

load_viventium_runtime_env()

DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "runtime_phase1.db"


def _build_runtime(runtime_backend: str, db_path: str, runtime: WorkerRuntime | None) -> WorkerRuntime:
    if runtime is not None:
        return runtime
    if runtime_backend == "stub":
        return StubRuntime()
    return ProfiledWorkerRuntime(base_dir=str(Path(db_path).resolve().parent))


def create_app(
    db_path: str | None = None,
    runtime_backend: str | None = None,
    runtime: WorkerRuntime | None = None,
) -> FastAPI:
    load_viventium_runtime_env()
    resolved_db_path = db_path or os.environ.get("WPR_DB_PATH", str(DEFAULT_DB_PATH))
    resolved_runtime_backend = (runtime_backend or os.environ.get("WPR_RUNTIME_BACKEND", "openclaw")).strip().lower()
    data_root = Path(resolved_db_path).resolve().parent
    store = Store(resolved_db_path)
    runtime_impl = _build_runtime(resolved_runtime_backend, resolved_db_path, runtime)
    service = WorkersProjectsService(store, runtime_impl)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.store = store
        app.state.service = service
        try:
            yield
        finally:
            service.shutdown()

    app = FastAPI(
        title="Glass Hive Runtime Phase 1",
        version="0.3.0",
        lifespan=lifespan,
    )

    @app.exception_handler(HostWorkersDisabledError)
    async def host_workers_disabled_handler(request: Request, exc: HostWorkersDisabledError) -> JSONResponse:
        _ = request
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    @app.exception_handler(GlassHiveQuotaExceededError)
    async def quota_exceeded_handler(request: Request, exc: GlassHiveQuotaExceededError) -> JSONResponse:
        _ = request
        return JSONResponse(status_code=429, content={"detail": str(exc)})

    @app.exception_handler(GlassHiveProfileNotAllowedError)
    async def profile_not_allowed_handler(request: Request, exc: GlassHiveProfileNotAllowedError) -> JSONResponse:
        _ = request
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    api_token = os.environ.get("WPR_API_TOKEN", "").strip()
    auth_settings = EnterpriseAuthSettings()
    auth_settings.validate_startup(api_token=api_token)
    unauthenticated_prefixes = (
        "/health",
        "/v1/signed-links",
        "/favicon.ico",
    ) if auth_settings.enterprise else (
        "/health",
        "/docs",
        "/openapi.json",
        "/redoc",
        "/ui",
        "/v1/signed-links",
        "/favicon.ico",
    )

    def _signed_link_context(request: Request) -> AuthContext | None:
        kind = str(request.query_params.get("gh_kind") or "").strip()
        expires_at = str(request.query_params.get("gh_exp") or "").strip()
        signature = str(request.query_params.get("gh_sig") or "").strip()
        if not kind or not expires_at or not signature:
            return None

        path_parts = [part for part in request.url.path.split("/") if part]
        worker_id = ""
        artifact_path = ""
        if len(path_parts) >= 3 and path_parts[0] == "v1" and path_parts[1] == "workers":
            worker_id = path_parts[2]
            if kind == "artifact_download" and request.method.upper() == "GET" and path_parts[3:] == ["artifacts", "download"]:
                artifact_path = str(request.query_params.get("path") or "").strip().lstrip("/")
            elif kind == "worker_view" and path_parts[3:] and path_parts[3] in {
                "assign",
                "desktop-action",
                "interrupt",
                "live",
                "message",
                "pause",
                "resume",
                "terminate",
            }:
                artifact_path = ""
            else:
                return None
        elif len(path_parts) >= 3 and path_parts[0] == "ui" and path_parts[1] == "workers":
            worker_id = path_parts[2]
            if kind != "worker_view":
                return None
        else:
            return None

        worker = store.get_worker(worker_id)
        if not worker:
            return None
        if not verify_signed_link(
            kind=kind,
            worker_id=worker_id,
            tenant_id=str(worker.get("tenant_id") or ""),
            owner_id=str(worker.get("owner_id") or ""),
            path=artifact_path,
            expires_at=expires_at,
            signature=signature,
        ):
            return None
        return AuthContext(
            tenant_id=str(worker.get("tenant_id") or "local"),
            user_id=str(worker.get("owner_id") or ""),
            auth_mode="signed_link",
            enterprise=auth_settings.enterprise,
        )

    def _service_token_from_headers(headers) -> str:
        for name in ("x-wpr-token", "x-glasshive-service-token", "x-glasshive-mcp-service-token"):
            token = str(headers.get(name) or "").strip()
            if token:
                return token
        return ""

    @app.middleware("http")
    async def optional_bearer_auth(request: Request, call_next):
        request.state.auth_context = AuthContext()
        if not api_token:
            return await call_next(request)
        if request.url.path.startswith(unauthenticated_prefixes):
            return await call_next(request)
        signed_context = _signed_link_context(request)
        if signed_context is not None:
            request.state.auth_context = signed_context
            return await call_next(request)
        auth_header = request.headers.get("authorization", "")
        token = _service_token_from_headers(request.headers)
        bearer = auth_header.removeprefix("Bearer ").strip() if auth_header.startswith("Bearer ") else ""
        if not (_token_matches(token, api_token) or _token_matches(bearer, api_token)):
            return Response(status_code=401, content="Unauthorized")
        try:
            request.state.auth_context = auth_settings.context_from_headers(
                {str(key).lower(): value for key, value in request.headers.items()}
            )
        except GlassHiveAuthError as exc:
            return JSONResponse(status_code=401, content={"detail": str(exc)})
        return await call_next(request)

    def _auth_context(request: Request | None = None) -> AuthContext:
        if request is None:
            return AuthContext()
        value = getattr(request.state, "auth_context", None)
        return value if isinstance(value, AuthContext) else AuthContext()

    def _tenant_filter(ctx: AuthContext) -> str | None:
        return ctx.tenant_id if ctx.enterprise else None

    def _owner_filter(ctx: AuthContext) -> str | None:
        return ctx.owner_id if ctx.enterprise else None

    def _request_owner(ctx: AuthContext, requested: str) -> str:
        return ctx.owner_id if ctx.enterprise else requested

    def _token_matches(candidate: str, expected: str) -> bool:
        return bool(candidate and expected and hmac.compare_digest(candidate, expected))

    def require_project(project_id: str, request: Request | None = None) -> dict:
        ctx = _auth_context(request)
        try:
            project = service.require_project(project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if ctx.enterprise and (
            project.get("tenant_id") != ctx.tenant_id or project.get("owner_id") != ctx.owner_id
        ):
            raise HTTPException(status_code=404, detail="Project not found")
        return project

    def require_worker(worker_id: str, request: Request | None = None) -> dict:
        ctx = _auth_context(request)
        try:
            worker = service.require_worker(worker_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if ctx.enterprise and (
            worker.get("tenant_id") != ctx.tenant_id or worker.get("owner_id") != ctx.owner_id
        ):
            raise HTTPException(status_code=404, detail="Worker not found")
        service.heal_worker(worker_id)
        return service.require_worker(worker_id)

    def require_run(run_id: str, request: Request | None = None) -> dict:
        ctx = _auth_context(request)
        try:
            run = service.require_run(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if ctx.enterprise:
            worker = store.get_worker(str(run.get("worker_id") or ""))
            if not worker or worker.get("tenant_id") != ctx.tenant_id or worker.get("owner_id") != ctx.owner_id:
                raise HTTPException(status_code=404, detail="Run not found")
        return run

    def absolute_ui_url(request: Request, worker_id: str) -> str:
        return f"{str(request.base_url).rstrip('/')}/ui/workers/{worker_id}"

    def absolute_view_url(request: Request, worker_id: str) -> str:
        return f"{str(request.base_url).rstrip('/')}/ui/workers/{worker_id}/view"

    def absolute_terminal_url(request: Request, worker_id: str) -> str:
        return f"{str(request.base_url).rstrip('/')}/ui/workers/{worker_id}/terminal"

    def _request_signed_link_params(request: Request) -> dict[str, str]:
        opaque_token = str(request.query_params.get("gh_token") or "").strip()
        if opaque_token:
            return {"gh_token": opaque_token}
        legacy = {
            "gh_kind": str(request.query_params.get("gh_kind") or "").strip(),
            "gh_exp": str(request.query_params.get("gh_exp") or "").strip(),
            "gh_sig": str(request.query_params.get("gh_sig") or "").strip(),
        }
        return legacy if all(legacy.values()) else {}

    def _runtime_details(worker: dict) -> dict[str, object]:
        if hasattr(runtime_impl, "describe_worker"):
            try:
                return runtime_impl.describe_worker(worker)
            except Exception:
                return {
                    "mode": "unavailable",
                    "runtime": str(worker.get("runtime") or ""),
                    "sandbox_state": "compute_unavailable",
                }
        return {
            "mode": "unknown",
            "runtime": str(worker.get("runtime") or ""),
            "workspace_dir": str(worker.get("workspace_dir") or ""),
            "state_dir": str(worker.get("state_dir") or ""),
        }

    def _terminal_target(worker: dict) -> TerminalTarget:
        if hasattr(runtime_impl, "terminal_target"):
            return runtime_impl.terminal_target(worker)
        workspace_dir = str(worker.get("workspace_dir") or data_root)
        return TerminalTarget(
            command=["screen", "-xRR", f"wpr-{worker['worker_id']}"],
            cwd=workspace_dir,
            env={"TERM": "xterm-256color"},
            title=f"{worker['name']} terminal",
            subtitle="Worker terminal",
        )

    def _read_tail(path: Path, max_bytes: int = 16000) -> str:
        if not path.exists():
            return ""
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(size - max_bytes, 0))
            return _redact_text(handle.read().decode("utf-8", errors="replace"))

    def _log_paths(worker: dict) -> tuple[Path, Path]:
        runtime_name = str(worker.get("runtime") or "")
        if str(worker.get("execution_mode") or "docker") == "host":
            root_map = {
                "openclaw": "host_openclaw_runtime",
                "codex-cli": "host_codex_cli_runtime",
                "claude-code": "host_claude_code_runtime",
            }
        else:
            root_map = {
                "openclaw": "openclaw_runtime",
                "openclaw-stub": "openclaw_runtime",
                "codex-cli": "codex_cli_runtime",
                "claude-code": "claude_code_runtime",
            }
        runtime_root = data_root / root_map.get(runtime_name, "openclaw_runtime")
        return (
            runtime_root / "logs" / f"{worker['worker_id']}.stdout.log",
            runtime_root / "logs" / f"{worker['worker_id']}.stderr.log",
        )

    def _read_jsonl_tail(path: Path, limit: int = 25) -> list[dict[str, object]]:
        if not path.exists():
            return []
        lines = path.read_text(errors="replace").splitlines()[-limit:]
        items: list[dict[str, object]] = []
        for line in lines:
            try:
                value = json.loads(line)
            except Exception:
                continue
            if isinstance(value, dict):
                items.append(value)
        return items

    def _host_visibility(worker: dict, runtime_details: dict[str, object]) -> dict[str, object]:
        workspace = Path(str(worker.get("workspace_dir") or ""))
        state_dir = Path(str(worker.get("state_dir") or ""))
        prompt_paths = runtime_details.get("prompt_paths") if isinstance(runtime_details.get("prompt_paths"), dict) else {}
        work_log_path = workspace / "work-log.md"
        return {
            "work_log_tail": _read_tail(work_log_path, max_bytes=8000),
            "action_audit_tail": _read_jsonl_tail(state_dir / "action-audit.jsonl"),
            "prompt_paths": prompt_paths,
        }

    def _workspace_items(worker: dict, max_entries: int = 120, max_depth: int = 3) -> list[dict[str, object]]:
        raw_root = str(worker.get("workspace_dir") or "").strip()
        if not raw_root:
            return []
        root = Path(raw_root)
        if not root.exists():
            return []
        items: list[dict[str, object]] = []
        for path in sorted(root.rglob("*")):
            try:
                rel = path.relative_to(root)
            except ValueError:
                continue
            if any(part == ".git" for part in rel.parts):
                continue
            if len(rel.parts) > max_depth:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            items.append(
                {
                    "path": rel.as_posix(),
                    "is_dir": path.is_dir(),
                    "size": None if path.is_dir() else stat.st_size,
                    "modified_at": stat.st_mtime,
                }
            )
            if len(items) >= max_entries:
                break
        return items

    def _latest_image_path(worker: dict) -> Path | None:
        raw_root = str(worker.get("workspace_dir") or "").strip()
        if not raw_root:
            return None
        root = Path(raw_root)
        if not root.exists():
            return None
        candidates: list[Path] = []
        for pattern in ("*.png", "*.jpg", "*.jpeg", "*.webp", "*.gif"):
            candidates.extend(root.rglob(pattern))
        visible = []
        for path in candidates:
            try:
                rel = path.relative_to(root)
            except ValueError:
                continue
            if any(part == ".git" for part in rel.parts):
                continue
            visible.append(path)
        if not visible:
            return None
        return max(visible, key=lambda item: item.stat().st_mtime)

    def _artifact_path(worker: dict, relative_path: str) -> Path:
        raw_root = str(worker.get("workspace_dir") or "").strip()
        if not raw_root:
            raise HTTPException(status_code=404, detail="Worker workspace is not available")
        root = Path(raw_root).resolve()
        target = (root / relative_path.strip().lstrip("/")).resolve()
        try:
            rel = target.relative_to(root)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Artifact path is outside the worker workspace") from exc
        if any(part == ".git" for part in rel.parts):
            raise HTTPException(status_code=400, detail="Artifact path is not downloadable")
        if not target.exists() or not target.is_file():
            raise HTTPException(status_code=404, detail="Artifact not found")
        max_bytes = int(os.environ.get("GLASSHIVE_ARTIFACT_DOWNLOAD_MAX_BYTES", str(100 * 1024 * 1024)))
        if max_bytes >= 0 and target.stat().st_size > max_bytes:
            raise HTTPException(status_code=413, detail="Artifact is larger than the configured download limit")
        return target

    def _sanitize_worker(worker: dict) -> dict[str, object]:
        safe = dict(worker)
        safe.pop("gateway_token", None)
        safe.pop("bootstrap_bundle_json", None)
        return safe

    def _can_show_internal_details(ctx: AuthContext) -> bool:
        if not auth_settings.enterprise and not ctx.enterprise:
            return True
        if ctx.auth_mode == "signed_link":
            return False
        return ctx.role.strip().lower() in {"admin", "operator", "owner"}

    def _redact_worker_for_member(worker: dict) -> dict[str, object]:
        safe = _sanitize_worker(worker)
        for key in (
            "gateway_url",
            "session_key",
            "workspace_dir",
            "state_dir",
            "home_dir",
            "container_name",
            "pid",
        ):
            safe.pop(key, None)
        return safe

    def _redact_runtime_details(details: dict[str, object]) -> dict[str, object]:
        allowed = {"mode", "runtime", "sandbox_state"}
        return {
            key: value
            for key, value in details.items()
            if key in allowed and value is not None and value != "" and value != []
        }

    def _redact_run_for_member(run: dict) -> dict[str, object]:
        return {
            key: value
            for key, value in run.items()
            if key
            in {
                "state",
                "instruction",
                "output_text",
                "error_text",
                "started_at",
                "ended_at",
                "created_at",
                "updated_at",
            }
        }

    def _redact_event_for_member(event: dict) -> dict[str, object]:
        return {
            key: value
            for key, value in event.items()
            if key in {"event_type", "message", "created_at"}
        }

    def _admin_api_enabled() -> bool:
        if not auth_settings.enterprise:
            return True
        return os.environ.get("GLASSHIVE_ENABLE_ADMIN_API", "").strip().lower() in {"1", "true", "yes", "on"}

    def _require_admin_api(ctx: AuthContext) -> None:
        if auth_settings.enterprise and not _admin_api_enabled():
            raise HTTPException(status_code=404, detail="Not found")
        if ctx.enterprise and ctx.role.strip().lower() not in {"admin", "owner", "operator"}:
            raise HTTPException(status_code=403, detail="Admin role required")

    def _live_payload(worker_id: str, request: Request | None = None) -> dict[str, object]:
        ctx = _auth_context(request)
        worker = require_worker(worker_id, request)
        runs = store.list_runs_for_worker(worker_id, limit=10, tenant_id=_tenant_filter(ctx))
        project_runs = store.list_runs_for_project(worker["project_id"], limit=12, tenant_id=_tenant_filter(ctx))
        events = store.list_events(worker_id, _tenant_filter(ctx))[-25:]
        latest_run = runs[0] if runs else None
        stdout_path, stderr_path = _log_paths(worker)
        stdout_text = _read_tail(stdout_path)
        stderr_text = _read_tail(stderr_path)
        runtime_details = _runtime_details(worker)
        host_visibility = _host_visibility(worker, runtime_details) if str(worker.get("execution_mode") or "docker") == "host" else {}
        latest_output = ""
        if latest_run:
            latest_output = str(latest_run.get("output_text") or latest_run.get("error_text") or "")
        latest_image = _latest_image_path(worker)
        deliverable = deliverable_payload(worker, latest_run, latest_output, stdout_text, stderr_text)
        show_internal = _can_show_internal_details(ctx)
        return {
            "worker": _sanitize_worker(worker) if show_internal else _redact_worker_for_member(worker),
            "latest_run": latest_run if show_internal or latest_run is None else _redact_run_for_member(latest_run),
            "latest_output": latest_output,
            "runs": runs if show_internal else [_redact_run_for_member(run) for run in runs],
            "project_runs": project_runs if show_internal else [_redact_run_for_member(run) for run in project_runs],
            "events": events if show_internal else [_redact_event_for_member(event) for event in events],
            "runtime_details": runtime_details if show_internal else _redact_runtime_details(runtime_details),
            "console": {
                "stdout": stdout_text if show_internal else "",
                "stderr": stderr_text if show_internal else "",
            },
            **(host_visibility if show_internal else {}),
            "workspace": {
                "root": worker.get("workspace_dir") or "" if show_internal else "",
                "items": _workspace_items(worker),
            },
            "artifacts": {
                "latest_image_name": latest_image.name if latest_image else None,
                "latest_image_url": f"/v1/workers/{worker_id}/artifacts/latest-image" if latest_image else None,
            },
            "deliverable": deliverable,
        }

    @app.get("/health")
    def health() -> dict[str, object]:
        payload: dict[str, object] = {
            "status": "ok",
            "version": app.version,
            "runtime_backend": resolved_runtime_backend,
        }
        if not auth_settings.enterprise:
            payload["metrics"] = store.metrics()
        return payload

    @app.get("/favicon.ico")
    def favicon() -> Response:
        return Response(status_code=204)

    @app.post("/v1/projects", response_model=ProjectResponse, status_code=201)
    def create_project(payload: CreateProjectRequest, request: Request) -> ProjectResponse:
        ctx = _auth_context(request)
        owner_id = _request_owner(ctx, payload.owner_id)
        tenant_id = ctx.tenant_id if ctx.enterprise else "local"
        return ProjectResponse(**service.create_project(owner_id, payload.title, payload.goal, payload.default_worker_profile, tenant_id=tenant_id))

    @app.get("/v1/projects")
    def list_projects(request: Request) -> dict[str, list[ProjectResponse]]:
        ctx = _auth_context(request)
        return {"items": [ProjectResponse(**item) for item in store.list_projects(_tenant_filter(ctx), _owner_filter(ctx))]}

    @app.get("/v1/projects/{project_id}", response_model=ProjectResponse)
    def get_project(project_id: str, request: Request) -> ProjectResponse:
        project = require_project(project_id, request)
        return ProjectResponse(**project)

    @app.get("/v1/projects/{project_id}/events")
    def list_project_events(project_id: str, request: Request) -> dict[str, list[EventResponse]]:
        ctx = _auth_context(request)
        require_project(project_id, request)
        return {"items": [EventResponse(**item) for item in store.list_project_events(project_id, _tenant_filter(ctx))]}

    @app.get("/v1/projects/{project_id}/runs")
    def list_project_runs(project_id: str, request: Request) -> dict[str, list[RunResponse]]:
        ctx = _auth_context(request)
        require_project(project_id, request)
        return {"items": [RunResponse(**item) for item in store.list_runs_for_project(project_id, tenant_id=_tenant_filter(ctx))]}

    @app.post("/v1/projects/{project_id}/workers", response_model=WorkerResponse, status_code=201)
    def create_worker(project_id: str, payload: CreateWorkerRequest, request: Request) -> WorkerResponse:
        ctx = _auth_context(request)
        project = require_project(project_id, request)
        owner_id = _request_owner(ctx, payload.owner_id)
        tenant_id = str(project.get("tenant_id") or ctx.tenant_id if ctx.enterprise else "local")
        worker = service.create_worker(
            project_id=project_id,
            owner_id=owner_id,
            name=payload.name,
            role=payload.role,
            profile=payload.profile,
            backend=payload.backend,
            execution_mode=payload.execution_mode,
            alias=scoped_alias(ctx, payload.alias or payload.name) if ctx.enterprise else payload.alias,
            workspace_root=payload.workspace_root,
            bootstrap_profile=payload.bootstrap_profile,
            bootstrap_bundle=payload.bootstrap_bundle,
            tenant_id=tenant_id,
            start_synchronously=payload.start_synchronously,
        )
        return WorkerResponse(**worker)

    @app.post("/v1/projects/{project_id}/workers/find-or-resume", response_model=WorkerResponse, status_code=200)
    def find_or_resume_worker(project_id: str, payload: CreateWorkerRequest, request: Request) -> WorkerResponse:
        ctx = _auth_context(request)
        project = require_project(project_id, request)
        owner_id = _request_owner(ctx, payload.owner_id)
        tenant_id = str(project.get("tenant_id") or ctx.tenant_id if ctx.enterprise else "local")
        alias = (payload.alias or payload.name or payload.profile).strip()
        if ctx.enterprise:
            alias = scoped_alias(ctx, alias)
        worker = service.find_or_create_worker(
            project_id=project_id,
            owner_id=owner_id,
            name=payload.name,
            role=payload.role,
            profile=payload.profile,
            backend=payload.backend,
            alias=alias,
            execution_mode=payload.execution_mode,
            workspace_root=payload.workspace_root,
            bootstrap_profile=payload.bootstrap_profile,
            bootstrap_bundle=payload.bootstrap_bundle,
            tenant_id=tenant_id,
            start_synchronously=payload.start_synchronously,
        )
        return WorkerResponse(**worker)

    @app.post("/v1/projects/{project_id}/workers/duplicate", response_model=WorkerResponse, status_code=201)
    def duplicate_worker(project_id: str, payload: DuplicateWorkerRequest, request: Request) -> WorkerResponse:
        ctx = _auth_context(request)
        require_project(project_id, request)
        source_worker = require_worker(payload.source_worker_id, request)
        worker = service.duplicate_worker(
            payload.source_worker_id,
            project_id,
            _request_owner(ctx, payload.owner_id or str(source_worker.get("owner_id") or "")),
            payload.name,
            payload.role,
        )
        return WorkerResponse(**worker)

    @app.get("/v1/projects/{project_id}/workers")
    def list_workers(project_id: str, request: Request) -> dict[str, list[WorkerResponse]]:
        ctx = _auth_context(request)
        require_project(project_id, request)
        return {"items": [WorkerResponse(**item) for item in store.list_workers(project_id, _tenant_filter(ctx), _owner_filter(ctx))]}

    @app.get("/v1/workers/{worker_id}", response_model=WorkerResponse)
    def get_worker(worker_id: str, request: Request) -> WorkerResponse:
        worker = require_worker(worker_id, request)
        return WorkerResponse(**worker)

    @app.patch("/v1/workers/{worker_id}", response_model=WorkerResponse)
    def update_worker_metadata(
        worker_id: str,
        payload: UpdateWorkerMetadataRequest,
        request: Request,
    ) -> WorkerResponse:
        require_worker(worker_id, request)
        try:
            worker = service.update_worker_metadata(worker_id, favorite=payload.favorite, name=payload.name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return WorkerResponse(**worker)

    @app.get("/v1/workers/{worker_id}/live")
    def worker_live(worker_id: str, request: Request) -> dict[str, object]:
        require_worker(worker_id, request)
        return _live_payload(worker_id, request)

    @app.get("/v1/workers/{worker_id}/runs")
    def list_worker_runs(worker_id: str, request: Request) -> dict[str, list[RunResponse]]:
        ctx = _auth_context(request)
        require_worker(worker_id, request)
        return {"items": [RunResponse(**item) for item in store.list_runs_for_worker(worker_id, tenant_id=_tenant_filter(ctx))]}

    @app.get("/v1/workers/{worker_id}/events")
    def list_worker_events(worker_id: str, request: Request) -> dict[str, list[EventResponse]]:
        ctx = _auth_context(request)
        require_worker(worker_id, request)
        return {"items": [EventResponse(**item) for item in store.list_events(worker_id, _tenant_filter(ctx))]}

    @app.get("/v1/workers/{worker_id}/schedules")
    def list_worker_schedules(
        worker_id: str,
        request: Request,
        include_done: bool = False,
    ) -> dict[str, list[ScheduleResponse]]:
        ctx = _auth_context(request)
        require_worker(worker_id, request)
        schedules = store.list_schedules_for_worker(
            worker_id,
            tenant_id=_tenant_filter(ctx),
            owner_id=_owner_filter(ctx),
            include_done=include_done,
        )
        return {"items": [ScheduleResponse(**item) for item in schedules]}

    @app.get("/v1/runs/{run_id}", response_model=RunResponse)
    def get_run(run_id: str, request: Request) -> RunResponse:
        run = require_run(run_id, request)
        return RunResponse(**run)

    @app.get("/v1/schedules/{schedule_id}", response_model=ScheduleResponse)
    def get_schedule(schedule_id: str, request: Request) -> ScheduleResponse:
        ctx = _auth_context(request)
        schedule = store.get_schedule(schedule_id, _tenant_filter(ctx), _owner_filter(ctx))
        if not schedule:
            raise HTTPException(status_code=404, detail="Schedule not found")
        return ScheduleResponse(**schedule)

    @app.post("/v1/workers/{worker_id}/assign", response_model=RunResponse, status_code=202)
    def assign(worker_id: str, payload: AssignRunRequest, request: Request) -> RunResponse:
        require_worker(worker_id, request)
        run = service.assign_run(worker_id, payload.instruction)
        return RunResponse(**run)

    @app.post("/v1/workers/{worker_id}/message", response_model=RunResponse, status_code=202)
    def send_message(worker_id: str, payload: SendMessageRequest, request: Request) -> RunResponse:
        require_worker(worker_id, request)
        run = service.send_message(worker_id, payload.message)
        return RunResponse(**run)

    @app.post("/v1/workers/{worker_id}/schedule", response_model=ScheduleResponse, status_code=202)
    def schedule_worker_run(worker_id: str, payload: ScheduleRunRequest, request: Request) -> ScheduleResponse:
        require_worker(worker_id, request)
        try:
            schedule = service.schedule_run(
                worker_id,
                payload.instruction,
                run_at=payload.run_at,
                schedule_text=payload.schedule_text,
                delay_seconds=payload.delay_seconds,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return ScheduleResponse(**schedule)

    @app.post("/v1/workers/{worker_id}/steer", response_model=RunResponse, status_code=202)
    def steer_worker(worker_id: str, payload: SendMessageRequest, request: Request) -> RunResponse:
        require_worker(worker_id, request)
        run = service.steer_worker(worker_id, payload.message)
        return RunResponse(**run)

    @app.post("/v1/workers/{worker_id}/launch-failed", response_model=WorkerResponse, status_code=202)
    def launch_failed(worker_id: str, payload: LaunchFailureRequest, request: Request) -> WorkerResponse:
        require_worker(worker_id, request)
        return WorkerResponse(**service.record_launch_failed(worker_id, payload.reason))

    @app.post("/v1/workers/{worker_id}/interrupt", response_model=WorkerResponse, status_code=202)
    def interrupt(worker_id: str, request: Request) -> WorkerResponse:
        require_worker(worker_id, request)
        return WorkerResponse(**service.interrupt_worker(worker_id))

    @app.post("/v1/workers/{worker_id}/pause", response_model=WorkerResponse, status_code=202)
    def pause(worker_id: str, request: Request) -> WorkerResponse:
        require_worker(worker_id, request)
        return WorkerResponse(**service.pause_worker(worker_id))

    @app.post("/v1/workers/{worker_id}/resume", response_model=WorkerResponse, status_code=202)
    def resume(worker_id: str, request: Request) -> WorkerResponse:
        require_worker(worker_id, request)
        return WorkerResponse(**service.resume_worker(worker_id))

    @app.post("/v1/workers/{worker_id}/terminate", response_model=WorkerResponse, status_code=202)
    def terminate(worker_id: str, request: Request) -> WorkerResponse:
        require_worker(worker_id, request)
        return WorkerResponse(**service.terminate_worker(worker_id))

    @app.post("/v1/workers/{worker_id}/desktop-action", response_model=DesktopActionResponse, status_code=202)
    def desktop_action(worker_id: str, payload: DesktopActionRequest, request: Request) -> DesktopActionResponse:
        worker = require_worker(worker_id, request)
        try:
            launched = service.desktop_action(worker_id, payload.action, url=payload.url, run_id=payload.run_id)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        resolved_url = str(launched.get("url") or launched.get("view_url") or absolute_view_url(request, worker_id))
        notes = str(launched.get("notes") or "")
        return DesktopActionResponse(
            action=str(launched.get("action") or payload.action),
            status=str(launched.get("status") or "launched"),
            mode=str(launched.get("mode") or "workstation-desktop"),
            url=resolved_url,
            view_url=str(launched.get("view_url") or resolved_url),
            notes=notes or None,
        )

    @app.get("/v1/workers/{worker_id}/takeover", response_model=TakeoverInfo)
    def takeover(worker_id: str, request: Request) -> TakeoverInfo:
        worker = require_worker(worker_id, request)
        store.add_event(worker["project_id"], worker_id, None, "worker.takeover_requested", "Operator takeover URL requested")
        runtime_details = _runtime_details(worker)
        if runtime_details.get("view_url"):
            return TakeoverInfo(
                supported=True,
                url=absolute_view_url(request, worker_id),
                mode="workstation-desktop",
                notes="Phase 1 takeover exposes the worker workstation desktop through a live browser view, with terminal control still available as a secondary surface.",
            )
        return TakeoverInfo(
            supported=True,
            url=absolute_terminal_url(request, worker_id),
            mode="web-terminal",
            notes="Phase 1 takeover is a real terminal session in the worker runtime. Desktop streaming stays deferred for this worker type.",
        )

    @app.get("/v1/workers/{worker_id}/artifacts/latest-image")
    def latest_worker_image(worker_id: str, request: Request) -> FileResponse:
        worker = require_worker(worker_id, request)
        latest = _latest_image_path(worker)
        if latest is None or not latest.exists():
            raise HTTPException(status_code=404, detail="No image artifacts found for this worker")
        return FileResponse(latest)

    @app.get("/v1/signed-links/{token}")
    def open_signed_link(token: str, request: Request) -> Response:
        payload = verify_signed_link_token(token)
        if not payload:
            raise HTTPException(status_code=401, detail="Signed link is invalid or expired")
        worker_id = str(payload.get("worker_id") or "").strip()
        worker = store.get_worker(worker_id)
        if not worker:
            raise HTTPException(status_code=404, detail="Worker not found")
        tenant_id = str(payload.get("tenant_id") or "")
        owner_id = str(payload.get("owner_id") or "")
        if tenant_id != str(worker.get("tenant_id") or "") or owner_id != str(worker.get("owner_id") or ""):
            raise HTTPException(status_code=401, detail="Signed link does not match this worker")
        request.state.auth_context = AuthContext(
            tenant_id=str(worker.get("tenant_id") or "local"),
            user_id=str(worker.get("owner_id") or ""),
            auth_mode="signed_link",
            enterprise=auth_settings.enterprise,
        )
        kind = str(payload.get("kind") or "")
        if kind == "artifact_download":
            path = str(payload.get("path") or "").strip().lstrip("/")
            target = _artifact_path(worker, path)
            store.add_event(worker["project_id"], worker_id, None, "worker.artifact_downloaded", target.name)
            return FileResponse(target, filename=target.name)
        if kind == "worker_view":
            expires_at = int(payload.get("exp") or 0)
            signed = sign_link_params(
                kind="worker_view",
                worker_id=worker_id,
                tenant_id=str(worker.get("tenant_id") or ""),
                owner_id=str(worker.get("owner_id") or ""),
                expires_at=expires_at,
            )
            url = f"/ui/workers/{quote(worker_id)}?surface=desktop&project_id={quote(str(worker.get('project_id') or ''))}"
            return RedirectResponse(append_signed_query(url, signed), status_code=302)
        raise HTTPException(status_code=400, detail="Signed link kind is not supported")

    @app.get("/v1/workers/{worker_id}/artifacts")
    def list_worker_artifacts(worker_id: str, request: Request) -> dict[str, object]:
        worker = require_worker(worker_id, request)
        items = [
            {
                **item,
                "download_url": f"/v1/workers/{worker_id}/artifacts/download?path={quote(str(item['path']))}",
            }
            for item in _workspace_items(worker, max_entries=500, max_depth=8)
            if not item.get("is_dir")
        ]
        store.add_event(worker["project_id"], worker_id, None, "worker.artifacts_listed", "Workspace artifacts listed")
        return {"items": items}

    @app.get("/v1/workers/{worker_id}/artifacts/download")
    def download_worker_artifact(worker_id: str, path: str, request: Request) -> FileResponse:
        worker = require_worker(worker_id, request)
        target = _artifact_path(worker, path)
        store.add_event(worker["project_id"], worker_id, None, "worker.artifact_downloaded", target.name)
        return FileResponse(target, filename=target.name)

    @app.get("/v1/metrics/summary", response_model=MetricsSummary)
    def metrics(request: Request) -> MetricsSummary:
        ctx = _auth_context(request)
        return MetricsSummary(**store.metrics(_tenant_filter(ctx), _owner_filter(ctx)))

    @app.post("/v1/admin/reconcile")
    def reconcile(request: Request) -> dict[str, object]:
        ctx = _auth_context(request)
        _require_admin_api(ctx)
        service.reconcile_all_workers()
        return {
            "status": "ok",
            "workers": len(store.list_all_workers()),
            "message": "Worker runtime metadata reconciled",
        }

    @app.post("/v1/admin/schedules/run-due")
    def run_due_schedules(request: Request) -> dict[str, object]:
        ctx = _auth_context(request)
        _require_admin_api(ctx)
        processed = service.process_due_schedules_once()
        return {"status": "ok", "processed": processed}

    @app.get("/ui", response_class=HTMLResponse)
    def ui_home(request: Request) -> str:
        ctx = _auth_context(request)
        show_internal = _can_show_internal_details(ctx)
        projects = store.list_projects(_tenant_filter(ctx), _owner_filter(ctx))
        project_items = []
        for project in projects:
            workers = store.list_workers(project["project_id"], _tenant_filter(ctx), _owner_filter(ctx))
            active_workers = [worker for worker in workers if worker["state"] != "terminated"]
            open_target = f"/ui/projects/{escape(project['project_id'])}"
            project_id_row = f"<p><strong>Project ID:</strong> {escape(project['project_id'])}</p>" if show_internal else ""
            project_items.append(
                "<section>"
                f"<h2>{escape(project['title'])}</h2>"
                f"<p><strong>Goal:</strong> {escape(project['goal'])}</p>"
                f"{project_id_row}"
                f"<p><strong>Status:</strong> {escape(project['status'])}</p>"
                f"<p><strong>Workers:</strong> {len(workers)} total, {len(active_workers)} active</p>"
                f"<p><a href='{open_target}'>Open project workspace</a></p>"
                "</section>"
            )
        body = "".join(project_items) or "<p>No projects yet.</p>"
        docs_link = f"<p><a href='{escape(str(request.base_url).rstrip('/'))}/docs'>OpenAPI docs</a></p>" if show_internal else ""
        return (
            "<html><head><title>Workers Projects Runtime</title>"
            "<style>body{font-family:system-ui,sans-serif;margin:2rem;max-width:1100px;}"
            "section{border:1px solid #ddd;padding:1rem;border-radius:12px;margin-bottom:1rem;}"
            "code,pre{background:#f6f6f6;padding:.2rem .4rem;border-radius:6px;}"
            "input,textarea,button{font:inherit;padding:.55rem;}"
            "</style></head><body>"
            "<h1>Workers Projects Runtime</h1>"
            "<p>Phase 1 standalone OpenClaw-backed control plane.</p>"
            f"{docs_link}"
            "<section>"
            "<h2>Create project</h2>"
            "<p><input id='project-owner' placeholder='Owner ID' value='demo-owner'/></p>"
            "<p><input id='project-title' placeholder='Project title' value='New Project'/></p>"
            "<p><textarea id='project-goal' placeholder='Project goal' style='width:100%;min-height:90px;'>Describe the goal for this worker project.</textarea></p>"
            "<p><select id='project-profile'>"
            "<option value='openclaw-general' selected>OpenClaw</option>"
            "<option value='claude-code'>Claude Code</option>"
            "<option value='codex-cli'>Codex CLI</option>"
            "</select></p>"
            "<p><button onclick='createProject()'>Create project</button></p>"
            "</section>"
            f"{body}"
            "<script>"
            "async function createProject(){"
            "const owner_id=document.getElementById('project-owner').value.trim();"
            "const title=document.getElementById('project-title').value.trim();"
            "const goal=document.getElementById('project-goal').value.trim();"
            "const default_worker_profile=document.getElementById('project-profile').value.trim()||'openclaw-general';"
            "if(!owner_id||!title||!goal){alert('owner, title, and goal are required');return;}"
            "const res=await fetch('/v1/projects',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({owner_id,title,goal,default_worker_profile})});"
            "if(!res.ok){alert(await res.text());return;}"
            "const project=await res.json();"
            "window.location.href=`/ui/projects/${project.project_id}`;"
            "}"
            "</script>"
            "</body></html>"
        )

    @app.get("/ui/projects/{project_id}", response_class=HTMLResponse)
    def ui_project(project_id: str, request: Request, worker_id: str | None = None) -> str:
        ctx = _auth_context(request)
        show_internal = _can_show_internal_details(ctx)
        project = require_project(project_id, request)
        workers = store.list_workers(project_id, _tenant_filter(ctx), _owner_filter(ctx))
        selected_worker = None
        if worker_id:
            selected_worker = next((worker for worker in workers if worker["worker_id"] == worker_id), None)
        if selected_worker is None:
            selected_worker = next((worker for worker in workers if worker["state"] != "terminated"), None)

        selected_runs = store.list_runs_for_worker(selected_worker["worker_id"], limit=10, tenant_id=_tenant_filter(ctx)) if selected_worker else []
        project_runs = store.list_runs_for_project(project_id, limit=12, tenant_id=_tenant_filter(ctx))
        selected_events = store.list_events(selected_worker["worker_id"], _tenant_filter(ctx)) if selected_worker else []
        selected_runtime_details = _runtime_details(selected_worker) if selected_worker else {}
        selected_view_url = str(selected_runtime_details.get("view_url") or "").strip()
        selected_is_host = str((selected_worker or {}).get("execution_mode") or "docker") == "host"
        selected_latest_image_url = (
            f"/v1/workers/{selected_worker['worker_id']}/artifacts/latest-image" if selected_worker and _latest_image_path(selected_worker) else ""
        )
        latest_run = selected_runs[0] if selected_runs else None
        latest_run_marker = escape(str((latest_run or {}).get("ended_at") or (latest_run or {}).get("started_at") or ""), quote=True)
        selected_worker_id = selected_worker["worker_id"] if selected_worker else ""
        selected_takeover_url = (
            f"/watch/{escape(selected_worker_id)}?project_id={escape(project_id, quote=True)}&surface=desktop"
            if selected_worker and ctx.enterprise and not show_internal
            else f"/ui/workers/{escape(selected_worker_id)}/view"
            if selected_worker
            else ""
        )
        if selected_takeover_url:
            selected_takeover_url = append_signed_query(selected_takeover_url, _request_signed_link_params(request))
        selected_desktop_url = (
            f"/desktop/{escape(selected_worker_id)}"
            if selected_worker and ctx.enterprise and not show_internal
            else selected_view_url
        )
        if selected_desktop_url:
            selected_desktop_url = append_signed_query(selected_desktop_url, _request_signed_link_params(request))
        project_worker_options = "".join(
            (
                f"<option value='{escape(worker['worker_id'])}'"
                f"{' selected' if selected_worker and worker['worker_id'] == selected_worker['worker_id'] else ''}>"
                f"{escape(worker['name'])} ({escape(worker['state'])})"
                "</option>"
            )
            for worker in workers
            if worker["state"] != "terminated"
        )
        worker_select = (
            f"{project_worker_options}<option value='__new__'>Create new worker...</option>"
            if workers
            else "<option value='__new__' selected>Create new worker...</option>"
        )
        latest_output = escape((latest_run or {}).get("output_text") or (latest_run or {}).get("error_text") or "")
        selected_event_items = "".join(
            f"<li><code>{escape(event['event_type'])}</code> - {escape(event['message'])}</li>"
            for event in selected_events[-20:]
        ) or "<li>No events yet</li>"
        project_run_items = "".join(
            "<li>"
            f"<strong>{escape(run['state'])}</strong> - {escape(run['instruction'][:140])}"
            f"<br/><small>{escape(run['worker_id'])}</small>"
            "</li>"
            for run in project_runs
        ) or "<li>No runs yet</li>"

        selected_worker_panel = ""
        if selected_worker:
            live_view_card = ""
            if selected_view_url:
                if ctx.enterprise and not show_internal:
                    live_view_card = f"""
                    <div class="card">
                      <h2>Live View</h2>
                      <p><strong>Best takeover flow:</strong> use the managed GlassHive takeover page, then pause or resume the worker from the controls.</p>
                      <p><a href="{selected_takeover_url}">Open full workspace</a></p>
                      <div class="actions">
                        <button onclick="pauseAndOpenDesktop('{escape(selected_desktop_url, quote=True)}')">Pause + Open Desktop</button>
                        <button onclick="window.open('{escape(selected_desktop_url, quote=True)}', '_blank', 'noopener')">Open Desktop In New Tab</button>
                      </div>
                      <iframe src="{escape(selected_desktop_url, quote=True)}" style="width:100%;height:520px;border:1px solid #d1d5db;border-radius:12px;background:#0f172a;" loading="eager"></iframe>
                    </div>
                    """
                else:
                    live_view_card = f"""
                    <div class="card">
                      <h2>Live View</h2>
                      <p><strong>Best takeover flow:</strong> press <code>Pause</code>, then open the desktop directly and click inside it to control the worker yourself. Use <code>Resume</code> when you want the worker to continue.</p>
                      <p><a href="/ui/workers/{escape(selected_worker['worker_id'])}/view">Open takeover page</a> · <a href="{escape(selected_view_url, quote=True)}" target="_blank" rel="noreferrer">Open desktop directly</a> · <a href="/ui/workers/{escape(selected_worker['worker_id'])}/terminal">Take over terminal</a></p>
                      <div class="actions">
                        <button onclick="pauseAndOpenDesktop('{escape(selected_view_url, quote=True)}')">Pause + Open Desktop</button>
                        <button onclick="window.open('{escape(selected_view_url, quote=True)}', '_blank', 'noopener')">Open Desktop In New Tab</button>
                      </div>
                      <iframe src="{escape(selected_view_url, quote=True)}" style="width:100%;height:520px;border:1px solid #d1d5db;border-radius:12px;background:#0f172a;" loading="eager"></iframe>
                    </div>
                    """
            workstation_tools_card = ""
            if selected_worker:
                tools_label = "Host Computer Tools" if selected_is_host else "Workstation Tools"
                tools_description = (
                    "These request real surfaces on the host computer for this worker."
                    if selected_is_host
                    else "These launch real surfaces inside the same persistent worker sandbox."
                )
                workstation_tools_card = f"""
                <div class="card">
                  <h2>{tools_label}</h2>
                  <p class="muted">{tools_description}</p>
                  <div class="actions">
                    <button onclick="desktopAction('terminal')">Open Shell</button>
                    <button onclick="desktopAction('files')">Open Files</button>
                    <button onclick="desktopAction('browser')">Open Browser</button>
                    <button onclick="desktopAction('codex')">Open Codex</button>
                    <button onclick="desktopAction('claude')">Open Claude</button>
                    <button onclick="desktopAction('openclaw')">Open OpenClaw</button>
                    <button onclick="desktopAction('focus_browser')">Raise Browser</button>
                  </div>
                </div>
                """
            latest_artifact_card = ""
            if selected_latest_image_url:
                latest_artifact_card = f"""
                <div class="card">
                  <h2>Latest Visual Artifact</h2>
                  <p class="muted">This helps you see the last meaningful frame even if the live desktop is now idle.</p>
                  <p><a href="{escape(selected_latest_image_url, quote=True)}" target="_blank" rel="noreferrer">Open latest image</a></p>
                  <img id="latest-artifact-image" src="{escape(selected_latest_image_url, quote=True)}?ts={latest_run_marker}" alt="Latest worker artifact" style="width:100%;max-height:520px;object-fit:contain;border:1px solid #ddd;border-radius:12px;background:#111827;" />
                </div>
                """
            worker_links = (
                f'<a href="/ui/workers/{escape(selected_worker["worker_id"])}">Open worker console</a> · '
                f'<a href="{selected_takeover_url}">Open takeover page</a> · '
                f'<a href="/ui/workers/{escape(selected_worker["worker_id"])}/terminal">Take over terminal</a>'
                if show_internal
                else f'<a href="{selected_takeover_url}">Open full workspace</a>'
            )
            lifecycle_buttons = (
                """
                <button onclick="workerAction('resume')">Resume</button>
                <button onclick="workerAction('pause')">Pause</button>
                <button onclick="workerAction('interrupt')">Interrupt</button>
                <button onclick="workerAction('terminate')">Terminate</button>
                <button onclick="window.location.reload()">Refresh</button>
                """
                if show_internal
                else """
                <button onclick="window.location.reload()">Refresh</button>
                """
            )
            message_worker_block = (
                """
              <h3>Message Worker</h3>
              <textarea id="message" placeholder="Send a short operator message into the active worker session."></textarea>
              <p><button onclick="sendMessage()">Send message</button></p>
                """
                if show_internal
                else ""
            )
            selected_worker_panel = f"""
            <div class="card">
              <h2>Selected Worker</h2>
              <p><strong>Name:</strong> {escape(selected_worker['name'])}</p>
              <p><strong>State:</strong> <span id="selected-worker-state">{escape(selected_worker['state'])}</span></p>
              <p><strong>Runtime:</strong> <span id="selected-worker-runtime">{escape(selected_worker.get('runtime') or '')}</span></p>
              <p><strong>Execution:</strong> <span id="selected-worker-execution">{escape(selected_worker.get('execution_mode') or 'docker')}</span></p>
              <p><strong>Profile:</strong> <span id="selected-worker-profile">{escape(selected_worker['profile'])}</span></p>
              <p><strong>Model:</strong> <span id="selected-worker-model">{escape(selected_worker.get('model') or '')}</span></p>
              {f"<p><strong>Gateway:</strong> {escape(selected_worker.get('gateway_url') or '')}</p>" if show_internal else ""}
              <p>{worker_links}</p>
              <div class="actions">
                {lifecycle_buttons}
              </div>
              {message_worker_block}
            </div>
            <div class="card">
              <h2>Latest Output</h2>
              <pre id="latest-output">{latest_output or 'No completed output yet.'}</pre>
            </div>
            {workstation_tools_card if show_internal else ''}
            {latest_artifact_card}
            {live_view_card}
            """
        else:
            selected_worker_panel = """
            <div class="card">
              <h2>No Worker Yet</h2>
              <p>Create your first worker below, then run the project prompt.</p>
            </div>
            <div class="card">
              <h2>Latest Output</h2>
              <pre>No worker selected yet.</pre>
            </div>
            """

        project_control_panel = f"""
                <div class="card">
                  <h2>Run Project</h2>
                  <p><strong>Worker</strong></p>
                  <select id="worker-select" onchange="workerSelectionChanged()">
                    {worker_select}
                  </select>
                  <div id="new-worker-fields" style="display:{'none' if selected_worker else 'block'}; margin-top: .75rem;">
                    <p><input id="worker-name" placeholder="Worker name" value="New Worker"/></p>
                    <p><input id="worker-owner" placeholder="Owner ID" value="{escape(project['owner_id'])}"/></p>
                    <p><input id="worker-role" placeholder="Role" value="research"/></p>
                    <p><select id="worker-profile">
                      <option value="openclaw-general"{' selected' if (project.get('default_worker_profile') or 'openclaw-general') == 'openclaw-general' else ''}>OpenClaw</option>
                      <option value="claude-code"{' selected' if (project.get('default_worker_profile') or '') == 'claude-code' else ''}>Claude Code</option>
                      <option value="codex-cli"{' selected' if (project.get('default_worker_profile') or '') == 'codex-cli' else ''}>Codex CLI</option>
                    </select></p>
                  </div>
                  <p><strong>Project Prompt</strong></p>
                  <textarea id="instruction" placeholder="Describe what this worker should do right now."></textarea>
                  <div class="actions">
                    <button onclick="runProject()">Run</button>
                    <button onclick="createWorkerOnly()">Create worker only</button>
                  </div>
                </div>
        """
        if not show_internal:
            project_control_panel = f"""
                <div class="card">
                  <h2>Project Workspace</h2>
                  <p class="muted">This fallback page is read-only for signed workspace links. Use the main GlassHive workspace to steer or resume work.</p>
                  {f'<p><a href="{selected_takeover_url}">Open full workspace</a></p>' if selected_takeover_url else ''}
                </div>
            """

        return f"""
        <html>
          <head>
            <title>{escape(project['title'])}</title>
            <style>
              body {{ font-family: system-ui, sans-serif; margin: 2rem; max-width: 1200px; }}
              .grid {{ display: grid; grid-template-columns: 1.1fr .9fr; gap: 1rem; }}
              .card {{ border: 1px solid #ddd; border-radius: 12px; padding: 1rem; margin-bottom: 1rem; }}
              textarea, input, select, button {{ font: inherit; padding: .6rem; }}
              textarea {{ width: 100%; min-height: 120px; }}
              input, select {{ width: 100%; box-sizing: border-box; }}
              pre {{ white-space: pre-wrap; background: #f6f6f6; padding: .75rem; border-radius: 8px; }}
              .actions {{ display: flex; gap: .5rem; flex-wrap: wrap; margin: .75rem 0; }}
              .muted {{ color: #666; }}
            </style>
          </head>
          <body>
            <h1>{escape(project['title'])}</h1>
            <p><a href="/ui">Back to projects</a>{f" · <a href='{escape(str(request.base_url).rstrip('/'))}/docs'>API docs</a>" if show_internal else ""}</p>
            <p><strong>Goal:</strong> {escape(project['goal'])}</p>
            <p class="muted">Simple flow: choose a worker, write the prompt, run it, then watch and control it.</p>

            <div class="grid">
              <div>
                {project_control_panel}
                <div class="card">
                  <h2>Recent Project Runs</h2>
                  <ul id="project-runs-list">{project_run_items}</ul>
                </div>
              </div>
              <div>
                {selected_worker_panel}
                <div class="card">
                  <h2>Recent Worker Events</h2>
                  <ul id="selected-worker-events">{selected_event_items}</ul>
                </div>
              </div>
            </div>

            <script>
              const projectId = {project_id!r};
              const currentWorkerId = {selected_worker_id!r};

              function selectedWorkerValue() {{
                return document.getElementById('worker-select').value;
              }}

              function workerSelectionChanged() {{
                const value = selectedWorkerValue();
                const newFields = document.getElementById('new-worker-fields');
                if (value === '__new__') {{
                  newFields.style.display = 'block';
                  return;
                }}
                newFields.style.display = 'none';
                window.location.href = `/ui/projects/${{projectId}}?worker_id=${{encodeURIComponent(value)}}`;
              }}

              async function ensureWorker() {{
                const value = selectedWorkerValue();
                if (value !== '__new__') return value;
                const owner_id = document.getElementById('worker-owner').value.trim();
                const name = document.getElementById('worker-name').value.trim();
                const role = document.getElementById('worker-role').value.trim() || 'research';
                const profile = document.getElementById('worker-profile').value.trim() || 'openclaw-general';
                if (!owner_id || !name) {{
                  alert('worker owner and name are required');
                  return '';
                }}
                const res = await fetch(`/v1/projects/${{projectId}}/workers`, {{
                  method: 'POST',
                  headers: {{ 'Content-Type': 'application/json' }},
                  body: JSON.stringify({{ owner_id, name, role, profile, backend: 'openclaw' }})
                }});
                if (!res.ok) {{
                  alert(await res.text());
                  return '';
                }}
                const worker = await res.json();
                return worker.worker_id;
              }}

              async function createWorkerOnly() {{
                const workerId = await ensureWorker();
                if (!workerId) return;
                window.location.href = `/ui/projects/${{projectId}}?worker_id=${{encodeURIComponent(workerId)}}`;
              }}

              async function runProject() {{
                const workerId = await ensureWorker();
                const instruction = document.getElementById('instruction').value.trim();
                if (!workerId || !instruction) {{
                  alert('choose or create a worker and enter a prompt');
                  return;
                }}
                const res = await fetch(`/v1/workers/${{workerId}}/assign`, {{
                  method: 'POST',
                  headers: {{ 'Content-Type': 'application/json' }},
                  body: JSON.stringify({{ instruction }})
                }});
                if (!res.ok) {{
                  alert(await res.text());
                  return;
                }}
                window.location.href = `/ui/projects/${{projectId}}?worker_id=${{encodeURIComponent(workerId)}}`;
              }}

              async function sendMessage() {{
                const messageEl = document.getElementById('message');
                if (!messageEl) return;
                const message = messageEl.value.trim();
                if (!currentWorkerId || !message) return;
                const res = await fetch(`/v1/workers/${{currentWorkerId}}/message`, {{
                  method: 'POST',
                  headers: {{ 'Content-Type': 'application/json' }},
                  body: JSON.stringify({{ message }})
                }});
                if (!res.ok) {{
                  alert(await res.text());
                  return;
                }}
                window.location.reload();
              }}

              async function workerAction(action) {{
                if (!currentWorkerId) return;
                const res = await fetch(`/v1/workers/${{currentWorkerId}}/${{action}}`, {{ method: 'POST' }});
                if (!res.ok) {{
                  alert(await res.text());
                  return;
                }}
                window.location.reload();
              }}

              async function pauseAndOpenDesktop(url) {{
                if (!currentWorkerId) return;
                const res = await fetch(`/v1/workers/${{currentWorkerId}}/pause`, {{ method: 'POST' }});
                if (!res.ok) {{
                  alert(await res.text());
                  return;
                }}
                window.open(url, '_blank', 'noopener');
                window.location.reload();
              }}

              async function desktopAction(action, url='') {{
                if (!currentWorkerId) return;
                const res = await fetch(`/v1/workers/${{currentWorkerId}}/desktop-action`, {{
                  method: 'POST',
                  headers: {{ 'Content-Type': 'application/json' }},
                  body: JSON.stringify({{ action, url: url || undefined }})
                }});
                if (!res.ok) {{
                  alert(await res.text());
                  return;
                }}
                const payload = await res.json();
                if (payload.url) {{
                  window.open(payload.url, '_blank', 'noopener');
                }}
                window.setTimeout(refreshProjectView, 700);
              }}

              function escapeHtml(value) {{
                return String(value ?? '')
                  .replaceAll('&', '&amp;')
                  .replaceAll('<', '&lt;')
                  .replaceAll('>', '&gt;')
                  .replaceAll('\"', '&quot;')
                  .replaceAll(\"'\", '&#39;');
              }}

              function renderProjectRuns(items) {{
                if (!items || !items.length) return '<li>No runs yet</li>';
                return items.map((run) =>
                  `<li><strong>${{escapeHtml(run.state)}}</strong> - ${{escapeHtml(String(run.instruction || '').slice(0, 140))}}<br/><small>${{escapeHtml(run.worker_id)}}</small></li>`
                ).join('');
              }}

              function renderEvents(items) {{
                if (!items || !items.length) return '<li>No events yet</li>';
                return items.map((event) =>
                  `<li><code>${{escapeHtml(event.event_type)}}</code> - ${{escapeHtml(event.message)}}</li>`
                ).join('');
              }}

              async function refreshProjectView() {{
                if (!currentWorkerId) return;
                try {{
                  const res = await fetch(`/v1/workers/${{currentWorkerId}}/live`);
                  if (!res.ok) return;
                  const live = await res.json();
                  document.getElementById('selected-worker-state').textContent = live.worker.state || '';
                  document.getElementById('selected-worker-runtime').textContent = live.worker.runtime || '';
                  document.getElementById('selected-worker-execution').textContent = live.worker.execution_mode || 'docker';
                  document.getElementById('selected-worker-profile').textContent = live.worker.profile || '';
                  document.getElementById('selected-worker-model').textContent = live.worker.model || '';
                  document.getElementById('latest-output').textContent = live.latest_output || 'No completed output yet.';
                  document.getElementById('selected-worker-events').innerHTML = renderEvents(live.events || []);
                  document.getElementById('project-runs-list').innerHTML = renderProjectRuns(live.project_runs || []);
                  const artifactImage = document.getElementById('latest-artifact-image');
                  if (artifactImage && live.artifacts && live.artifacts.latest_image_url) {{
                    artifactImage.src = `${{live.artifacts.latest_image_url}}?ts=${{Date.now()}}`;
                  }}
                }} catch (error) {{
                  console.debug('project live refresh failed', error);
                }}
              }}

              if (currentWorkerId) {{
                window.setInterval(refreshProjectView, 2500);
              }}
            </script>
          </body>
        </html>
        """

    @app.get("/ui/workers/{worker_id}", response_class=HTMLResponse)
    def ui_worker(worker_id: str, request: Request) -> str:
        ctx = _auth_context(request)
        worker = require_worker(worker_id, request)
        project = require_project(worker["project_id"], request)
        runs = store.list_runs_for_worker(worker_id, limit=10, tenant_id=_tenant_filter(ctx))
        events = store.list_events(worker_id, _tenant_filter(ctx))
        latest_run = runs[0] if runs else None
        live = _live_payload(worker_id, request)
        console = live["console"]
        workspace = live["workspace"]
        runtime_details = live["runtime_details"]
        show_internal = _can_show_internal_details(ctx)
        is_host_worker = str(worker.get("execution_mode") or "docker") == "host"
        artifacts = live["artifacts"]
        latest_run_marker = escape(str((latest_run or {}).get("ended_at") or (latest_run or {}).get("started_at") or ""), quote=True)
        signed_query = str(request.url.query or "") if "gh_sig=" in str(request.url.query or "") else ""
        signed_query_suffix = f"?{escape(signed_query, quote=True)}" if signed_query else ""
        signed_query_json = json.dumps(signed_query)
        if show_internal:
            run_items = "".join(
                "<li>"
                f"<strong>{escape(run['run_id'])}</strong> - {escape(run['state'])}<br/>"
                f"<span>{escape(run['instruction'])}</span><br/>"
                f"<pre>{escape((run.get('output_text') or run.get('error_text') or '')[:2000])}</pre>"
                "</li>"
                for run in runs
            ) or "<li>No runs yet</li>"
        else:
            run_items = "".join(
                "<li>"
                f"<strong>{escape(run['state'])}</strong><br/>"
                f"<span>{escape(run['instruction'])}</span><br/>"
                f"<pre>{escape((run.get('output_text') or run.get('error_text') or '')[:2000])}</pre>"
                "</li>"
                for run in runs
            ) or "<li>No runs yet</li>"
        event_items = "".join(
            f"<li><code>{escape(event['event_type'])}</code> - {escape(event['message'])}</li>"
            for event in events[-25:]
        ) or "<li>No events yet</li>"
        last_error = escape(worker.get("last_error") or "")
        latest_output = escape((latest_run or {}).get("output_text", "")) if latest_run else ""
        workspace_items = "".join(
            f"<li><code>{escape(str(item['path']))}</code>"
            f"{' <em>(dir)</em>' if item['is_dir'] else ''}"
            f"{'' if item['is_dir'] else ' <small>' + escape(str(item.get('size') or 0)) + ' bytes</small>'}"
            "</li>"
            for item in workspace["items"]
        ) or "<li>Workspace is empty</li>"
        def detail_value_html(value: object) -> str:
            if isinstance(value, dict):
                nested = "".join(
                    f"<div><code>{escape(str(nested_key))}</code>: <code>{escape(str(nested_value))}</code></div>"
                    for nested_key, nested_value in value.items()
                    if nested_value is not None and nested_value != ""
                )
                return f'<div class="detail-map">{nested or "None"}</div>'
            if isinstance(value, list):
                nested = "".join(f"<li>{escape(str(item))}</li>" for item in value)
                return f"<ul>{nested}</ul>" if nested else "None"
            return escape(str(value))

        detail_items = "".join(
            f"<li><strong>{escape(str(key).replace('_', ' ').title())}:</strong> {detail_value_html(value)}</li>"
            for key, value in runtime_details.items()
            if value is not None and value != "" and value != []
        ) or "<li>No runtime details yet</li>"
        worker_id_row = f"<p><strong>Worker ID:</strong> {escape(worker['worker_id'])}</p>" if show_internal else ""
        diagnostic_rows = (
            f"""
                <p><strong>Gateway:</strong> {escape(worker.get('gateway_url') or '')}</p>
                <p><strong>Session Key:</strong> <code>{escape(worker.get('session_key') or '')}</code></p>
                <p><strong>Workspace:</strong> <code id="workspace-root">{escape(worker.get('workspace_dir') or '')}</code></p>
            """
            if show_internal
            else '<p><strong>Workspace:</strong> <span id="workspace-root">Managed by GlassHive</span></p>'
        )
        stdout_console = escape(console["stdout"] or "No stdout yet.") if show_internal else "Diagnostics hidden in enterprise member view."
        stderr_console = escape(console["stderr"] or "No stderr yet.") if show_internal else "Diagnostics hidden in enterprise member view."
        workstation_tools = ""
        tools_label = "Host Computer Tools" if is_host_worker else "Workstation Tools"
        workstation_tools = f"""
                <h3>{tools_label}</h3>
                <div class="actions">
                  <button onclick="desktopAction('terminal')">Open Shell</button>
                  <button onclick="desktopAction('files')">Open Files</button>
                  <button onclick="desktopAction('browser')">Open Browser</button>
                  <button onclick="desktopAction('codex')">Open Codex</button>
                  <button onclick="desktopAction('claude')">Open Claude</button>
                  <button onclick="desktopAction('openclaw')">Open OpenClaw</button>
                  <button onclick="desktopAction('focus_browser')">Raise Browser</button>
                </div>
            """
        latest_artifact_panel = ""
        if artifacts.get("latest_image_url"):
            latest_artifact_panel = f"""
              <div class="card">
                <h2>Latest Visual Artifact</h2>
                <p><a href="{escape(str(artifacts['latest_image_url']), quote=True)}" target="_blank" rel="noreferrer">Open latest image</a></p>
                <img id="latest-artifact-image" src="{escape(str(artifacts['latest_image_url']), quote=True)}?ts={latest_run_marker}" alt="Latest worker artifact" style="width:100%;max-height:520px;object-fit:contain;border:1px solid #ddd;border-radius:12px;background:#111827;" />
              </div>
            """
        return f"""
        <html>
          <head>
            <title>{escape(worker['name'])}</title>
            <style>
              body {{ font-family: system-ui, sans-serif; margin: 2rem; max-width: 1100px; }}
              .grid {{ display: grid; grid-template-columns: 1.1fr .9fr; gap: 1rem; }}
              .card {{ border: 1px solid #ddd; border-radius: 12px; padding: 1rem; }}
              pre {{ white-space: pre-wrap; background: #f6f6f6; padding: .75rem; border-radius: 8px; }}
              textarea {{ width: 100%; min-height: 120px; }}
              input, textarea, button {{ font: inherit; padding: .55rem; }}
              .actions {{ display: flex; gap: .5rem; flex-wrap: wrap; margin-bottom: .75rem; }}
              .console-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-top: 1rem; }}
            </style>
          </head>
          <body>
            <h1>{escape(worker['name'])}</h1>
            <p><a href="/ui">Back to projects</a> · <a href="/ui/projects/{escape(project['project_id'])}?worker_id={escape(worker_id)}">Back to project workspace</a></p>
            <div class="grid">
              <div class="card">
                <h2>Worker</h2>
                {worker_id_row}
                <p><strong>Role:</strong> {escape(worker['role'])}</p>
                <p><strong>Profile:</strong> {escape(worker['profile'])}</p>
                <p><strong>Execution:</strong> {escape(worker.get('execution_mode') or 'docker')}</p>
                <p><strong>Model:</strong> {escape(worker.get('model') or '')}</p>
                <p><strong>State:</strong> <span id="worker-state">{escape(worker['state'])}</span></p>
                {diagnostic_rows}
                <p><strong>Last Error:</strong> <span id="worker-last-error">{last_error or 'None'}</span></p>
                <p><a href="/ui/workers/{escape(worker_id)}/view{signed_query_suffix}">Open takeover page</a> · <a href="/ui/workers/{escape(worker_id)}/terminal{signed_query_suffix}">Take over terminal</a></p>
                <div class="actions">
                  <button onclick="action('resume')">Resume</button>
                  <button onclick="action('pause')">Pause</button>
                  <button onclick="action('interrupt')">Interrupt</button>
                  <button onclick="action('terminate')">Terminate</button>
                  <button onclick="window.location.reload()">Refresh</button>
                </div>
                <h3>Assign Run</h3>
                <textarea id="instruction" placeholder="Give this worker a concrete task."></textarea>
                <p><button onclick="assignRun()">Queue task</button></p>
                <h3>Communicate</h3>
                <textarea id="message" placeholder="Send an operator message into the worker session."></textarea>
                <p><button onclick="sendMessage()">Send message</button></p>
                {workstation_tools}
              </div>
              <div class="card">
                <h2>Latest Output</h2>
                <pre id="latest-output">{latest_output or 'No completed output yet.'}</pre>
              </div>
            </div>
            <div class="grid" style="margin-top:1rem;">
              <div class="card">
                <h2>Recent Runs</h2>
                <ul id="run-list">{run_items}</ul>
              </div>
              <div class="card">
                <h2>Recent Events</h2>
                <ul id="event-list">{event_items}</ul>
              </div>
            </div>
            <div class="grid" style="margin-top:1rem;">
              {latest_artifact_panel}
            </div>
            <div class="console-grid">
              <div class="card">
                <h2>Live Stdout</h2>
                <pre id="stdout-console">{stdout_console}</pre>
              </div>
              <div class="card">
                <h2>Live Stderr</h2>
                <pre id="stderr-console">{stderr_console}</pre>
              </div>
            </div>
            <div class="grid" style="margin-top:1rem;">
              <div class="card">
                <h2>Live View</h2>
                {(
                    f'<p><strong>Best takeover flow:</strong> press <code>Pause</code>, then <a href="{escape(str(runtime_details.get("view_url") or ""), quote=True)}" target="_blank" rel="noreferrer">open desktop directly</a> and click inside the desktop to take control. Use <code>Resume</code> to hand control back.</p>'
                    f'<p><a href="/ui/workers/{escape(worker_id)}/view">Open takeover page</a> · <a href="{escape(str(runtime_details.get("view_url") or ""), quote=True)}" target="_blank" rel="noreferrer">Open desktop directly</a></p>'
                    f'<iframe src="{escape(str(runtime_details.get("view_url") or ""), quote=True)}" style="width:100%;height:520px;border:1px solid #d1d5db;border-radius:12px;background:#0f172a;" loading="eager"></iframe>'
                ) if runtime_details.get("view_url") else '<p>No desktop view is available for this worker. Terminal takeover is still available.</p>'}
              </div>
            </div>
            <div class="grid" style="margin-top:1rem;">
              <div class="card">
                <h2>Workspace Files</h2>
                <ul id="workspace-items">{workspace_items}</ul>
              </div>
              <div class="card">
                <h2>Runtime Boundary</h2>
                <ul id="runtime-details">{detail_items}</ul>
              </div>
            </div>
            <script>
              function escapeHtml(value) {{
                return String(value ?? '')
                  .replaceAll('&', '&amp;')
                  .replaceAll('<', '&lt;')
                  .replaceAll('>', '&gt;')
                  .replaceAll('\"', '&quot;')
                  .replaceAll(\"'\", '&#39;');
              }}

              function renderRuns(items) {{
                if (!items || !items.length) return '<li>No runs yet</li>';
                return items.map((run) =>
                  run.run_id
                    ? `<li><strong>${{escapeHtml(run.run_id)}}</strong> - ${{escapeHtml(run.state)}}<br/><span>${{escapeHtml(run.instruction || '')}}</span><br/><pre>${{escapeHtml(((run.output_text || run.error_text || '')).slice(0, 2000))}}</pre></li>`
                    : `<li><strong>${{escapeHtml(run.state)}}</strong><br/><span>${{escapeHtml(run.instruction || '')}}</span><br/><pre>${{escapeHtml(((run.output_text || run.error_text || '')).slice(0, 2000))}}</pre></li>`
                ).join('');
              }}

              function renderEvents(items) {{
                if (!items || !items.length) return '<li>No events yet</li>';
                return items.map((event) =>
                  `<li><code>${{escapeHtml(event.event_type)}}</code> - ${{escapeHtml(event.message)}}</li>`
                ).join('');
              }}

              function renderWorkspace(items) {{
                if (!items || !items.length) return '<li>Workspace is empty</li>';
                return items.map((item) =>
                  `<li><code>${{escapeHtml(item.path)}}</code>${{item.is_dir ? ' <em>(dir)</em>' : ` <small>${{escapeHtml(item.size ?? 0)}} bytes</small>`}}</li>`
                ).join('');
              }}

              function renderDetails(details) {{
                const entries = Object.entries(details || {{}}).filter(([, value]) => value !== null && value !== '' && !(Array.isArray(value) && value.length === 0));
                if (!entries.length) return '<li>No runtime details yet</li>';
                return entries.map(([key, value]) => `<li><strong>${{escapeHtml(key.replaceAll('_', ' '))}}:</strong> ${{renderDetailValue(value)}}</li>`).join('');
              }}

              function renderDetailValue(value) {{
                if (Array.isArray(value)) {{
                  return value.length ? `<ul>${{value.map((item) => `<li>${{escapeHtml(item)}}</li>`).join('')}}</ul>` : 'None';
                }}
                if (value && typeof value === 'object') {{
                  const rows = Object.entries(value)
                    .filter(([, nestedValue]) => nestedValue !== null && nestedValue !== '')
                    .map(([nestedKey, nestedValue]) => `<div><code>${{escapeHtml(nestedKey)}}</code>: <code>${{escapeHtml(nestedValue)}}</code></div>`)
                    .join('');
                  return `<div class="detail-map">${{rows || 'None'}}</div>`;
                }}
                return escapeHtml(value);
              }}

              async function action(name) {{
                await fetch(withSignedQuery(`/v1/workers/{escape(worker_id)}/${{name}}`), {{ method: 'POST' }});
                window.location.reload();
              }}
              async function assignRun() {{
                const instruction = document.getElementById('instruction').value.trim();
                if (!instruction) return;
                await fetch(withSignedQuery(`/v1/workers/{escape(worker_id)}/assign`), {{
                  method: 'POST',
                  headers: {{ 'Content-Type': 'application/json' }},
                  body: JSON.stringify({{ instruction }})
                }});
                window.location.reload();
              }}
              async function sendMessage() {{
                const message = document.getElementById('message').value.trim();
                if (!message) return;
                await fetch(withSignedQuery(`/v1/workers/{escape(worker_id)}/message`), {{
                  method: 'POST',
                  headers: {{ 'Content-Type': 'application/json' }},
                  body: JSON.stringify({{ message }})
                }});
                window.location.reload();
              }}

              async function desktopAction(action, url='') {{
                const res = await fetch(withSignedQuery(`/v1/workers/{escape(worker_id)}/desktop-action`), {{
                  method: 'POST',
                  headers: {{ 'Content-Type': 'application/json' }},
                  body: JSON.stringify({{ action, url: url || undefined }})
                }});
                if (!res.ok) {{
                  alert(await res.text());
                  return;
                }}
                const payload = await res.json();
                if (payload.url) {{
                  window.open(payload.url, '_blank', 'noopener');
                }}
              }}

              const signedQuery = {signed_query_json};
              function withSignedQuery(url) {{
                if (!signedQuery) return url;
                return `${{url}}${{url.includes('?') ? '&' : '?'}}${{signedQuery}}`;
              }}

              async function refreshLive() {{
                try {{
                  const res = await fetch(withSignedQuery(`/v1/workers/{escape(worker_id)}/live`));
                  if (!res.ok) return;
                  const live = await res.json();
                  document.getElementById('worker-state').textContent = live.worker.state || '';
                  document.getElementById('worker-last-error').textContent = live.worker.last_error || 'None';
                  const workspaceRoot = document.getElementById('workspace-root');
                  if (workspaceRoot && live.workspace && live.workspace.root) workspaceRoot.textContent = live.workspace.root;
                  document.getElementById('latest-output').textContent = live.latest_output || 'No completed output yet.';
                  document.getElementById('stdout-console').textContent = live.console.stdout || 'No stdout yet.';
                  document.getElementById('stderr-console').textContent = live.console.stderr || 'No stderr yet.';
                  document.getElementById('run-list').innerHTML = renderRuns(live.runs || []);
                  document.getElementById('event-list').innerHTML = renderEvents(live.events || []);
                  document.getElementById('workspace-items').innerHTML = renderWorkspace((live.workspace || {{}}).items || []);
                  document.getElementById('runtime-details').innerHTML = renderDetails(live.runtime_details || {{}});
                  const artifactImage = document.getElementById('latest-artifact-image');
                  if (artifactImage && live.artifacts && live.artifacts.latest_image_url) {{
                    artifactImage.src = `${{live.artifacts.latest_image_url}}?ts=${{Date.now()}}`;
                  }}
                }} catch (error) {{
                  console.debug('worker live refresh failed', error);
                }}
              }}

              window.setInterval(refreshLive, 2500);
            </script>
          </body>
        </html>
        """

    @app.get("/ui/workers/{worker_id}/view", response_class=HTMLResponse)
    def ui_worker_view(worker_id: str, request: Request) -> str:
        ctx = _auth_context(request)
        worker = require_worker(worker_id, request)
        project = require_project(worker["project_id"], request)
        runtime_details = _runtime_details(worker)
        show_internal = _can_show_internal_details(ctx)
        external_view_url = str(runtime_details.get("view_url") or "").strip()
        subtitle = escape(str(runtime_details.get("mode") or worker.get("runtime") or "worker view"))
        signed_query = str(request.url.query or "") if "gh_sig=" in str(request.url.query or "") else ""
        signed_query_suffix = f"?{escape(signed_query, quote=True)}" if signed_query else ""
        signed_query_json = json.dumps(signed_query)
        meta_identity = (
            escape(str(runtime_details.get("container_name") or worker.get("session_key") or worker_id))
            if show_internal
            else "managed workspace"
        )
        if not external_view_url:
            return f"""
            <html>
              <head>
                <title>{escape(worker['name'])} live view</title>
                <style>
                  body {{ font-family: system-ui, sans-serif; margin: 2rem; max-width: 900px; }}
                  .card {{ border: 1px solid #ddd; border-radius: 12px; padding: 1rem; }}
                </style>
              </head>
              <body>
                <div class="card">
                  <h1>{escape(worker['name'])}</h1>
                  <p>{subtitle}</p>
                  <p>No workstation desktop view is available for this worker right now.</p>
                  <p><a href="/ui/projects/{escape(project['project_id'])}?worker_id={escape(worker_id)}" target="_top">Back to project workspace</a> · <a href="/ui/workers/{escape(worker_id)}/terminal{signed_query_suffix}" target="_top">Take over terminal</a></p>
                </div>
              </body>
            </html>
            """
        return f"""
        <html>
          <head>
            <title>{escape(worker['name'])} live view</title>
            <style>
              body {{ font-family: system-ui, sans-serif; margin: 0; background: #0f172a; color: #e5e7eb; }}
              header {{ padding: 1rem 1.25rem; border-bottom: 1px solid rgba(255,255,255,.12); display: flex; justify-content: space-between; gap: 1rem; align-items: center; flex-wrap: wrap; }}
              a {{ color: #93c5fd; }}
              .actions {{ display: flex; gap: .5rem; flex-wrap: wrap; }}
              button {{ font: inherit; padding: .55rem .85rem; border-radius: 8px; border: 1px solid rgba(255,255,255,.14); background: #111827; color: #f9fafb; }}
              .meta {{ color: #cbd5e1; font-size: .95rem; }}
              .notice {{ padding: .85rem 1.25rem; border-bottom: 1px solid rgba(255,255,255,.08); background: rgba(15,23,42,.92); color: #cbd5e1; }}
              .notice strong {{ color: #f8fafc; }}
              .notice code {{ background: rgba(255,255,255,.08); padding: .15rem .35rem; border-radius: 6px; }}
              iframe {{ width: 100%; height: calc(100vh - 180px); border: 0; background: #020617; }}
            </style>
          </head>
          <body>
            <header>
              <div>
                <div><strong>{escape(worker['name'])}</strong></div>
                <div class="meta">{subtitle} · {meta_identity}</div>
                <div class="meta"><a href="/ui/projects/{escape(project['project_id'])}?worker_id={escape(worker_id)}" target="_top">Back to project workspace</a> · <a href="/ui/workers/{escape(worker_id)}{signed_query_suffix}" target="_top">Worker console</a> · <a href="/ui/workers/{escape(worker_id)}/terminal{signed_query_suffix}" target="_top">Terminal</a> · <a href="{escape(external_view_url, quote=True)}" target="_blank" rel="noreferrer">Desktop directly</a></div>
              </div>
              <div class="actions">
                <button onclick="action('resume')">Resume</button>
                <button onclick="action('pause')">Pause</button>
                <button onclick="action('interrupt')">Interrupt</button>
                <button onclick="action('terminate')">Terminate</button>
                <button onclick="pauseAndOpenDirect()">Pause + Open Desktop</button>
                <button onclick="desktopAction('terminal')">Shell</button>
                <button onclick="desktopAction('files')">Files</button>
                <button onclick="desktopAction('browser')">Browser</button>
                <button onclick="desktopAction('codex')">Codex</button>
                <button onclick="desktopAction('claude')">Claude</button>
                <button onclick="desktopAction('openclaw')">OpenClaw</button>
              </div>
            </header>
            <div class="notice">
              <strong>How to take over:</strong> press <code>Pause</code> to freeze the worker, then click inside the embedded desktop below or open <a href="{escape(external_view_url, quote=True)}" target="_blank" rel="noreferrer">Desktop directly</a> in its own tab. Use <code>Resume</code> to hand control back to the worker. Use <code>Interrupt</code> to stop the current task instead of freezing it.
            </div>
            <iframe src="{escape(external_view_url, quote=True)}" loading="eager"></iframe>
            <script>
              const workerId = {worker_id!r};
              const directDesktopUrl = {external_view_url!r};
              const signedQuery = {signed_query_json};
              function withSignedQuery(url) {{
                if (!signedQuery) return url;
                return `${{url}}${{url.includes('?') ? '&' : '?'}}${{signedQuery}}`;
              }}
              async function action(name) {{
                await fetch(withSignedQuery(`/v1/workers/${{workerId}}/${{name}}`), {{ method: 'POST' }});
              }}
              async function pauseAndOpenDirect() {{
                await action('pause');
                window.open(directDesktopUrl, '_blank', 'noopener');
              }}
              async function desktopAction(name, url='') {{
                const res = await fetch(withSignedQuery(`/v1/workers/${{workerId}}/desktop-action`), {{
                  method: 'POST',
                  headers: {{ 'Content-Type': 'application/json' }},
                  body: JSON.stringify({{ action: name, url: url || undefined }})
                }});
                if (!res.ok) {{
                  alert(await res.text());
                  return;
                }}
                window.open(directDesktopUrl, '_blank', 'noopener');
              }}
            </script>
          </body>
        </html>
        """

    @app.get("/ui/workers/{worker_id}/terminal", response_class=HTMLResponse)
    def ui_worker_terminal(worker_id: str, request: Request) -> str:
        ctx = _auth_context(request)
        worker = require_worker(worker_id, request)
        project = require_project(worker["project_id"], request)
        runtime_details = _runtime_details(worker)
        show_internal = _can_show_internal_details(ctx)
        subtitle = escape(str(runtime_details.get("mode") or worker.get("runtime") or "worker terminal"))
        signed_query = str(request.url.query or "") if "gh_sig=" in str(request.url.query or "") else ""
        signed_query_suffix = f"?{escape(signed_query, quote=True)}" if signed_query else ""
        signed_query_json = json.dumps(signed_query)
        meta_identity = (
            escape(str(runtime_details.get("container_name") or worker.get("session_key") or worker["worker_id"]))
            if show_internal
            else "managed workspace"
        )
        return f"""
        <html>
          <head>
            <title>{escape(worker['name'])} terminal</title>
            <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/css/xterm.css" />
            <style>
              body {{ font-family: system-ui, sans-serif; margin: 0; background: #0f172a; color: #e5e7eb; }}
              header {{ padding: 1rem 1.25rem; border-bottom: 1px solid rgba(255,255,255,.12); display: flex; justify-content: space-between; gap: 1rem; align-items: center; flex-wrap: wrap; }}
              a {{ color: #93c5fd; }}
              .actions {{ display: flex; gap: .5rem; flex-wrap: wrap; }}
              button {{ font: inherit; padding: .55rem .85rem; border-radius: 8px; border: 1px solid rgba(255,255,255,.14); background: #111827; color: #f9fafb; }}
              .meta {{ color: #cbd5e1; font-size: .95rem; }}
              #terminal {{ height: calc(100vh - 110px); padding: 1rem; box-sizing: border-box; }}
            </style>
          </head>
          <body>
            <header>
              <div>
                <div><strong>{escape(worker['name'])}</strong></div>
                <div class="meta">{subtitle} · {meta_identity}</div>
                <div class="meta"><a href="/ui/projects/{escape(project['project_id'])}?worker_id={escape(worker_id)}" target="_top">Back to project workspace</a> · <a href="/ui/workers/{escape(worker_id)}{signed_query_suffix}" target="_top">Worker console</a></div>
              </div>
              <div class="actions">
                <button onclick="action('resume')">Resume</button>
                <button onclick="action('pause')">Pause</button>
                <button onclick="action('interrupt')">Interrupt</button>
                <button onclick="action('terminate')">Terminate</button>
              </div>
            </header>
            <div id="terminal"></div>
            <script src="https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/lib/xterm.js"></script>
            <script>
              const workerId = {worker_id!r};
              const initialState = {worker['state']!r};
              const signedQuery = {signed_query_json};
              function withSignedQuery(url) {{
                if (!signedQuery) return url;
                return `${{url}}${{url.includes('?') ? '&' : '?'}}${{signedQuery}}`;
              }}
              let socket;
              const terminal = new Terminal({{
                convertEol: true,
                cursorBlink: true,
                fontFamily: 'Menlo, Monaco, Consolas, monospace',
                fontSize: 14,
                theme: {{ background: '#0b1020', foreground: '#e5e7eb' }}
              }});
              terminal.open(document.getElementById('terminal'));
              terminal.writeln('Connecting to worker terminal...');

              async function action(name) {{
                await fetch(withSignedQuery(`/v1/workers/${{workerId}}/${{name}}`), {{ method: 'POST' }});
              }}

              function sendResize() {{
                if (!socket || socket.readyState !== WebSocket.OPEN) return;
                const cols = Math.max(80, Math.floor(window.innerWidth / 9));
                const rows = Math.max(24, Math.floor((window.innerHeight - 120) / 18));
                socket.send(JSON.stringify({{ type: 'resize', cols, rows }}));
              }}

              async function start() {{
                if (initialState === 'paused') {{
                  await action('resume');
                }}
                const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
                socket = new WebSocket(`${{protocol}}://${{window.location.host}}${{withSignedQuery(`/ws/workers/${{workerId}}/terminal`)}}`);
                socket.onopen = () => {{
                  terminal.clear();
                  sendResize();
                  terminal.focus();
                }};
                socket.onmessage = (event) => terminal.write(event.data);
                socket.onclose = () => terminal.writeln('\\r\\n[terminal disconnected]');
                socket.onerror = () => terminal.writeln('\\r\\n[terminal error]');
                terminal.onData((data) => {{
                  if (socket && socket.readyState === WebSocket.OPEN) {{
                    socket.send(JSON.stringify({{ type: 'input', data }}));
                  }}
                }});
                window.addEventListener('resize', sendResize);
              }}

              start();
            </script>
          </body>
        </html>
        """

    @app.websocket("/ws/workers/{worker_id}/terminal")
    async def worker_terminal_socket(worker_id: str, websocket: WebSocket) -> None:
        ctx = AuthContext()
        if api_token:
            signed_worker = store.get_worker(worker_id)
            if signed_worker and verify_signed_link(
                kind=str(websocket.query_params.get("gh_kind") or ""),
                worker_id=worker_id,
                tenant_id=str(signed_worker.get("tenant_id") or ""),
                owner_id=str(signed_worker.get("owner_id") or ""),
                expires_at=str(websocket.query_params.get("gh_exp") or ""),
                signature=str(websocket.query_params.get("gh_sig") or ""),
            ):
                ctx = AuthContext(
                    tenant_id=str(signed_worker.get("tenant_id") or "local"),
                    user_id=str(signed_worker.get("owner_id") or ""),
                    auth_mode="signed_link",
                    enterprise=auth_settings.enterprise,
                )
            else:
                token = _service_token_from_headers(websocket.headers)
                auth_header = websocket.headers.get("authorization", "")
                bearer = auth_header.removeprefix("Bearer ").strip() if auth_header.startswith("Bearer ") else ""
                if not (_token_matches(token, api_token) or _token_matches(bearer, api_token)):
                    await websocket.close(code=4401)
                    return
                try:
                    ctx = auth_settings.context_from_headers(
                        {str(key).lower(): value for key, value in websocket.headers.items()}
                    )
                except GlassHiveAuthError:
                    await websocket.close(code=4401)
                    return
        worker = store.get_worker(
            worker_id,
            tenant_id=ctx.tenant_id if ctx.enterprise else None,
            owner_id=ctx.owner_id if ctx.enterprise else None,
        )
        if not worker:
            await websocket.close(code=4404)
            return
        if worker["state"] == "terminated":
            await websocket.close(code=4404)
            return
        target = _terminal_target(worker)
        await bridge_terminal(websocket, target)

    return app


app = create_app()
