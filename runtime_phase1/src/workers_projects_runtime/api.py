from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
import hmac
import re
import time
from hashlib import sha256
from collections import deque
from contextlib import asynccontextmanager
from html import escape
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlparse

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

from .auth import (
    AuthContext,
    EnterpriseAuthSettings,
    GlassHiveAuthError,
    header_identity_value,
    owner_matches_auth_context,
    scoped_alias,
)
from .conversation_provider import install_conversation_provider_routes
from .deliverables import deliverable_payload, is_user_deliverable_relative_path
from .failure_classification import classify_runtime_error
from .models import (
    ActiveWorkActionRequest,
    AssignRunRequest,
    CallbackAssociationVerifyRequest,
    CreateDelegationRequest,
    CreateProjectRequest,
    CreateWorkerRequest,
    DesktopActionRequest,
    DesktopActionResponse,
    DuplicateWorkerRequest,
    EventResponse,
    LaunchFailureRequest,
    MetricsSummary,
    ProjectResponse,
    RunActionRequest,
    RunActionResponse,
    RunResponse,
    ScheduleResponse,
    ScheduleRunRequest,
    SendMessageRequest,
    TakeoverInfo,
    UpdateUserPreferencesRequest,
    UpdateWorkerMetadataRequest,
    UserPreferencesResponse,
    WorkerResponse,
)
from .openclaw_runtime import RuntimeDependencyMissingError, StubRuntime, WorkerRuntime
from .profile_runtime import ProfiledWorkerRuntime, _redact_text
from .runtime_env import load_viventium_runtime_env
from .runtime_identity import derive_legacy_backend_label
from .run_actions import ACTION_CAPABILITY_HEADER, ACTION_ENDPOINT, RunActionError
from .service_assertions import (
    SERVICE_ASSERTION_AUDIENCE,
    SERVICE_ASSERTION_HEADER,
    ServiceAssertionError,
    verify_service_assertion,
)
from .service import (
    GlassHiveProfileNotAllowedError,
    GlassHiveQuotaExceededError,
    HostWorkersDisabledError,
    ParallelExecutionIsolationError,
    WorkersProjectsService,
    allowed_worker_profiles,
    merge_bootstrap_bundle,
)
from .signed_links import (
    append_signed_query,
    create_signed_link_ref,
    install_sensitive_url_log_filter,
    resolve_signed_link_ref,
    sign_link_params,
    signed_link_ref_url,
    sign_link_token,
    verify_signed_link,
    verify_signed_link_token,
)
from .store import (
    ActiveWorkActionConflictError,
    DelegationIdempotencyConflictError,
    Store,
    WorkAdmissionError,
)
from .terminal_takeover import TerminalTarget, bridge_terminal

load_viventium_runtime_env()
install_sensitive_url_log_filter()

DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "runtime_phase1.db"
logger = logging.getLogger(__name__)
TEXT_ARTIFACT_PREVIEW_EXTENSIONS = {
    ".css",
    ".csv",
    ".htm",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".log",
    ".md",
    ".markdown",
    ".py",
    ".sh",
    ".sql",
    ".toml",
    ".ts",
    ".tsv",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
ARTIFACT_OPEN_SECURITY_HEADERS = {
    "Cache-Control": "no-store, no-cache, private, max-age=0",
    "Content-Security-Policy": "default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'self'",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "SAMEORIGIN",
}
ARTIFACT_DOWNLOAD_SECURITY_HEADERS = {
    "Cache-Control": "no-store, no-cache, private, max-age=0",
    "Pragma": "no-cache",
    "X-Content-Type-Options": "nosniff",
}
SIGNED_QUERY_KEYS = {"gh_token", "gh_sig", "gh_exp", "gh_kind"}


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
    reconcile_on_startup: bool | None = None,
) -> FastAPI:
    load_viventium_runtime_env()
    resolved_db_path = db_path or os.environ.get("WPR_DB_PATH", str(DEFAULT_DB_PATH))
    resolved_runtime_backend = (runtime_backend or os.environ.get("WPR_RUNTIME_BACKEND", "openclaw")).strip().lower()
    data_root = Path(resolved_db_path).resolve().parent
    store = Store(resolved_db_path)
    runtime_impl = _build_runtime(resolved_runtime_backend, resolved_db_path, runtime)
    service = WorkersProjectsService(
        store, runtime_impl, reconcile_on_startup=reconcile_on_startup
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.store = store
        app.state.service = service
        try:
            yield
        finally:
            service.shutdown()

    app = FastAPI(
        title="GlassHive Runtime",
        version="0.3.0",
        lifespan=lifespan,
    )
    app.state.store = store
    app.state.service = service

    @app.exception_handler(WorkAdmissionError)
    async def work_admission_handler(
        request: Request, exc: WorkAdmissionError
    ) -> JSONResponse:
        _ = request
        return JSONResponse(
            status_code=409,
            content={"detail": {"code": exc.code, "message": str(exc)}},
        )

    @app.exception_handler(HostWorkersDisabledError)
    async def host_workers_disabled_handler(request: Request, exc: HostWorkersDisabledError) -> JSONResponse:
        _ = request
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    @app.exception_handler(ParallelExecutionIsolationError)
    async def parallel_execution_isolation_handler(
        request: Request, exc: ParallelExecutionIsolationError
    ) -> JSONResponse:
        _ = request
        return JSONResponse(
            status_code=409,
            content={
                "detail": {
                    "code": "parallel_execution_isolation_required",
                    "message": str(exc),
                }
            },
        )

    @app.exception_handler(GlassHiveQuotaExceededError)
    async def quota_exceeded_handler(request: Request, exc: GlassHiveQuotaExceededError) -> JSONResponse:
        _ = request
        env_name = str(getattr(exc, "env_name", "") or "")
        is_workspace_quota = "MAX_WORKSPACES" in env_name
        retry_after: int | None = None
        if not is_workspace_quota:
            idle_release_after = int(os.environ.get("GLASSHIVE_IDLE_TERMINATE_AFTER_S", "0") or "0")
            if idle_release_after > 0:
                retry_after = max(30, min(idle_release_after, 3600))
            else:
                retry_after = max(30, min(int(os.environ.get("GLASSHIVE_IDLE_REAPER_INTERVAL_S", "60") or "60"), 300))
        options = list(getattr(exc, "available_workspace_options", []) or [])
        if is_workspace_quota:
            option_text = (
                "Use one of `available_workspace_options` when it fits the user's intent, ask the user which "
                "existing workspace to continue, terminate/archive an unneeded workspace, or ask the operator "
                "to raise the workspace quota. Waiting for idle release will not free a saved workspace slot."
                if options
                else "No reusable workspace options are visible in this user scope; terminate/archive an unneeded workspace or ask the operator to inspect capacity."
            )
            failure_user_message = (
                "GlassHive did not start a new workspace because the saved workspace quota is currently full."
            )
            main_agent_next_action = (
                "Review `available_workspace_options` and pick/reuse one that matches the user's task, "
                "or ask the user which listed workspace to continue. If none fits, ask the user/operator to "
                "terminate an unneeded workspace or raise quota. Do not retry this launch on a timer, and do "
                "not suggest switching profile or sandbox mode as the fix for this quota."
            )
        else:
            option_text = (
                "Use one of `available_workspace_options` when it fits the user's intent, ask the user which "
                "existing workspace to continue when needed, or wait for idle compute release before launching "
                "another workspace."
                if options
                else "No reusable workspace options are visible in this user scope; wait for idle compute release or ask the operator to inspect capacity."
            )
            failure_user_message = (
                "GlassHive did not start a new workspace because the active workspace limit is currently full."
            )
            main_agent_next_action = (
                "Review `available_workspace_options` and pick/reuse one that matches the user's task, "
                "or ask the user which listed workspace to continue. If none fits, wait for idle release "
                "or ask the operator to adjust capacity. Do not suggest switching profile or sandbox mode "
                "as the fix for this quota because active workers share the same cap."
            )
        headers = {"Retry-After": str(retry_after)} if retry_after is not None else {}
        return JSONResponse(
            status_code=429,
            headers=headers,
            content={
                "status": "blocked",
                "detail": str(exc),
                "failure_class": "glasshive_worker_quota_exceeded",
                "failure_retryable": 0 if is_workspace_quota else 1,
                "failure_user_message": failure_user_message,
                "failure_recommended_recovery": option_text,
                "failure_diagnostic_summary": str(exc),
                "quota": {
                    "env_name": env_name,
                    "label": getattr(exc, "label", ""),
                    "limit": getattr(exc, "limit", 0),
                    "current_count": getattr(exc, "current_count", 0),
                },
                "retry_after_seconds": retry_after,
                "available_workspace_options": options,
                "acknowledgement_guidance": (
                    "Explain that GlassHive capacity is full. Do not claim a workspace is running and do "
                    "not immediately relaunch the same request in a loop."
                ),
                "main_agent_next_action": main_agent_next_action,
            },
        )

    @app.exception_handler(GlassHiveProfileNotAllowedError)
    async def profile_not_allowed_handler(request: Request, exc: GlassHiveProfileNotAllowedError) -> JSONResponse:
        _ = request
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    @app.exception_handler(RuntimeDependencyMissingError)
    async def runtime_dependency_missing_handler(request: Request, exc: RuntimeDependencyMissingError) -> JSONResponse:
        _ = request
        failure = classify_runtime_error(
            exc,
            runtime_name=str(getattr(exc, "runtime_name", "") or "worker"),
        )
        return JSONResponse(
            status_code=409,
            content={
                "status": "blocked",
                "detail": failure.user_message,
                **failure.as_store_fields(),
            },
        )

    api_token = os.environ.get("WPR_API_TOKEN", "").strip()
    auth_settings = EnterpriseAuthSettings()
    auth_settings.validate_startup(api_token=api_token)
    unauthenticated_prefixes = (
        "/health",
        "/r/",
        "/w/",
        "/v1/signed-links",
        "/favicon.ico",
    ) if auth_settings.enterprise else (
        "/health",
        "/docs",
        "/openapi.json",
        "/redoc",
        "/r/",
        "/w/",
        "/ui",
        "/v1/link-refs",
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
            if kind in {"artifact_download", "artifact_open"} and request.method.upper() == "GET":
                expected_action = "download" if kind == "artifact_download" else "open"
                if path_parts[3:] != ["artifacts", expected_action]:
                    return None
                artifact_path = str(request.query_params.get("path") or "").strip().lstrip("/")
            elif (
                kind == "worker_view"
                and request.method.upper() == "GET"
                and path_parts[3:] == ["live"]
            ):
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
        # VIVENTIUM START: authoritative signed-link termination fence
        # Signed-link helpers may consult a separate ambient database or a
        # best-effort revocation sink. The FastAPI app's own Store is the
        # authoritative worker lifecycle source for every legacy query open.
        if not worker or str(worker.get("state") or "") == "terminated":
            return None
        # VIVENTIUM END: authoritative signed-link termination fence
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

    def _service_auth_context_from_headers(headers) -> AuthContext:
        normalized_headers = {
            str(key).lower(): value for key, value in headers.items()
        }
        if auth_settings.enterprise:
            return auth_settings.context_from_headers(normalized_headers)
        asserted_owner = header_identity_value(
            normalized_headers, auth_settings.user_header
        )
        asserted_tenant = header_identity_value(
            normalized_headers, auth_settings.tenant_header
        )
        return AuthContext(
            tenant_id=asserted_tenant or "local",
            user_id=asserted_owner,
            email=header_identity_value(
                normalized_headers, auth_settings.email_header
            ),
            role=header_identity_value(
                normalized_headers, auth_settings.role_header
            ),
            auth_mode="service_identity" if asserted_owner else "service",
            enterprise=False,
        )

    def _is_account_api_path(path: str) -> bool:
        return (
            path == "/v1/delegations"
            or path.startswith("/v1/delegations/by-origin/")
            or path == "/v1/orchestration-capabilities"
            or path == "/v1/active-work"
            or path.startswith("/v1/active-work/")
            or path.startswith("/v1/work/")
            or path == "/v1/callback-associations/verify"
        )

    def _account_auth_error(exc: ServiceAssertionError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": {"code": exc.code, "message": str(exc)}},
        )

    @app.middleware("http")
    async def optional_bearer_auth(request: Request, call_next):
        request.state.auth_context = AuthContext()
        if _is_account_api_path(request.url.path):
            assertion_secret = str(
                os.environ.get("VIVENTIUM_GLASSHIVE_SERVICE_ASSERTION_SECRET") or ""
            ).strip()
            if not api_token or not assertion_secret:
                return JSONResponse(
                    status_code=503,
                    content={
                        "detail": {
                            "code": "service_assertion_unavailable",
                            "message": "The GlassHive account API is not configured.",
                        }
                    },
                )
            authorization = str(request.headers.get("authorization") or "").strip()
            scheme, _, bearer = authorization.partition(" ")
            if scheme.lower() != "bearer" or not _token_matches(bearer.strip(), api_token):
                return JSONResponse(
                    status_code=401,
                    content={
                        "detail": {
                            "code": "service_auth_required",
                            "message": "A valid GlassHive service bearer token is required.",
                        }
                    },
                )
            try:
                claims = verify_service_assertion(
                    str(request.headers.get(SERVICE_ASSERTION_HEADER) or ""),
                    secret=assertion_secret,
                )
            except ServiceAssertionError as exc:
                return _account_auth_error(exc)
            if request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
                consumed = store.consume_service_assertion_nonce(
                    audience=SERVICE_ASSERTION_AUDIENCE,
                    tenant_id=str(claims["tenant_id"]),
                    owner_id=str(claims["owner_id"]),
                    nonce=str(claims["nonce"]),
                    issued_at_epoch=int(claims["iat"]),
                    expires_at_epoch=int(claims["exp"]),
                    request_method=request.method,
                    request_path=request.url.path,
                )
                if not consumed:
                    return JSONResponse(
                        status_code=409,
                        content={
                            "detail": {
                                "code": "service_assertion_replayed",
                                "message": "The Viventium service assertion nonce was already used.",
                            }
                        },
                    )
            request.state.service_assertion_claims = claims
            request.state.auth_context = AuthContext(
                tenant_id=str(claims["tenant_id"]),
                user_id=str(claims["owner_id"]),
                auth_mode="service_assertion",
                enterprise=True,
            )
            return await call_next(request)
        if request.method.upper() == "POST" and request.url.path == ACTION_ENDPOINT:
            capability = str(request.headers.get(ACTION_CAPABILITY_HEADER) or "").strip()
            if not capability:
                return JSONResponse(
                    status_code=401,
                    content={
                        "detail": {
                            "code": "capability_required",
                            "message": "A scoped action capability is required.",
                        }
                    },
                )
            try:
                claims = service.verify_action_capability(capability)
            except RunActionError as exc:
                return JSONResponse(
                    status_code=exc.status_code,
                    content={"detail": {"code": exc.code, "message": str(exc)}},
                )
            if auth_settings.enterprise and str(claims["tenantId"]) != auth_settings.tenant_id:
                return JSONResponse(
                    status_code=403,
                    content={
                        "detail": {
                            "code": "capability_scope_mismatch",
                            "message": "The action capability does not match this deployment tenant.",
                        }
                    },
                )
            request.state.run_action_claims = claims
            request.state.auth_context = AuthContext(
                tenant_id=str(claims["tenantId"]),
                user_id=str(claims["ownerId"]),
                auth_mode="action_capability",
                enterprise=auth_settings.enterprise,
            )
            return await call_next(request)
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
            request.state.auth_context = _service_auth_context_from_headers(
                request.headers
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
        return ctx.tenant_id if ctx.is_user_scoped else None

    def _owner_filter(ctx: AuthContext) -> str | None:
        return ctx.owner_id if ctx.is_user_scoped else None

    def _request_owner(ctx: AuthContext, requested: str) -> str:
        return ctx.owner_id if ctx.is_user_scoped else requested

    def _configured_default_worker_profile() -> str:
        configured = os.environ.get("GLASSHIVE_DEFAULT_WORKER_PROFILE", "").strip()
        allowed = allowed_worker_profiles()
        if configured:
            if allowed and configured not in allowed:
                raise HTTPException(
                    status_code=500,
                    detail="GLASSHIVE_DEFAULT_WORKER_PROFILE must be included in GLASSHIVE_ALLOWED_WORKER_PROFILES",
                )
            return configured
        if allowed:
            return "codex-cli" if "codex-cli" in allowed else sorted(allowed)[0]
        return "codex-cli"

    def _ui_show_legacy_openclaw_profile() -> bool:
        return os.environ.get("GLASSHIVE_UI_SHOW_LEGACY_OPENCLAW_PROFILE", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
            "enabled",
        }

    def _ui_worker_profile_options(selected_profile: str) -> str:
        selected = (selected_profile or "").strip() or _configured_default_worker_profile()
        allowed_profiles = set(allowed_worker_profiles() or ["codex-cli", "claude-code", "openclaw-general"])
        profile_labels = {
            "codex-cli": "Codex CLI",
            "claude-code": "Claude Code",
            "openclaw-general": "OpenClaw",
        }
        profiles: list[str] = []
        for profile in ("codex-cli", "claude-code", "openclaw-general"):
            if profile.startswith("openclaw") and profile != selected and not _ui_show_legacy_openclaw_profile():
                continue
            if profile in allowed_profiles or profile == selected:
                profiles.append(profile)
        return "".join(
            f"<option value='{escape(profile)}'{' selected' if profile == selected else ''}>{escape(profile_labels.get(profile, profile))}</option>"
            for profile in profiles
        )

    def _preference_owner(ctx: AuthContext) -> str:
        if ctx.is_user_scoped:
            return ctx.owner_id
        return os.environ.get("WPR_DEFAULT_OWNER_ID", "").strip() or "demo-owner"

    def _blank_preferences(tenant_id: str, owner_id: str) -> dict:
        return {
            "tenant_id": tenant_id or "local",
            "owner_id": owner_id,
            "default_worker_profile": "",
            "codex_reasoning_effort": "",
            "claude_effort": "",
            "openclaw_effort": "",
            "updated_at": "",
        }

    def _normalize_preference_payload(payload: UpdateUserPreferencesRequest) -> dict[str, str | None]:
        normalized: dict[str, str | None] = {}
        if payload.default_worker_profile is not None:
            profile = payload.default_worker_profile.strip()
            allowed = allowed_worker_profiles()
            if profile and allowed and profile not in allowed:
                raise HTTPException(
                    status_code=400,
                    detail="default_worker_profile is not allowed by GLASSHIVE_ALLOWED_WORKER_PROFILES",
                )
            normalized["default_worker_profile"] = profile
        if payload.codex_reasoning_effort is not None:
            effort = payload.codex_reasoning_effort.strip().lower()
            if effort and effort not in {"none", "minimal", "low", "medium", "high", "xhigh"}:
                raise HTTPException(status_code=400, detail="codex_reasoning_effort must be none, minimal, low, medium, high, or xhigh")
            normalized["codex_reasoning_effort"] = effort
        if payload.claude_effort is not None:
            effort = payload.claude_effort.strip().lower()
            if effort and effort not in {"default", "max"}:
                raise HTTPException(status_code=400, detail="claude_effort must be default or max")
            normalized["claude_effort"] = "" if effort == "default" else effort
        if payload.openclaw_effort is not None:
            effort = payload.openclaw_effort.strip().lower()
            if effort and effort not in {"default", "high", "max"}:
                raise HTTPException(status_code=400, detail="openclaw_effort must be default, high, or max")
            normalized["openclaw_effort"] = "" if effort == "default" else effort
        return normalized

    def _assign_effort_bundle(worker: dict, effort_value: str | None) -> dict | None:
        effort = str(effort_value or "").strip().lower()
        if not effort:
            return None
        profile = str(worker.get("profile") or "").strip()
        if profile == "codex-cli":
            if effort not in {"none", "minimal", "low", "medium", "high", "xhigh"}:
                raise HTTPException(status_code=400, detail="Codex effort must be none, minimal, low, medium, high, or xhigh")
            return {"env": {"WPR_CODEX_CLI_REASONING_EFFORT": effort}}
        if profile == "claude-code":
            if effort not in {"default", "max"}:
                raise HTTPException(status_code=400, detail="Claude effort must be default or max")
            if effort == "default":
                return None
            return {"env": {"WPR_CLAUDE_CODE_EFFORT": effort}}
        elif profile == "openclaw-general":
            if effort not in {"default", "high", "max"}:
                raise HTTPException(status_code=400, detail="OpenClaw effort must be default, high, or max")
            if effort == "default":
                return None
        else:
            raise HTTPException(status_code=400, detail="effort override is not supported for this worker profile")
        return {"system_instructions": f"Worker effort preference for this run: {effort}."}

    def merge_runtime_bundles(first: dict | None, second: dict | None) -> dict | None:
        if first is None:
            return second
        if second is None:
            return first
        return merge_bootstrap_bundle(first, second)

    def _token_matches(candidate: str, expected: str) -> bool:
        return bool(candidate and expected and hmac.compare_digest(candidate, expected))

    def require_project(project_id: str, request: Request | None = None) -> dict:
        ctx = _auth_context(request)
        try:
            project = service.require_project(project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if ctx.is_user_scoped and (
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
        if ctx.is_user_scoped and (
            worker.get("tenant_id") != ctx.tenant_id or worker.get("owner_id") != ctx.owner_id
        ):
            raise HTTPException(status_code=404, detail="Worker not found")
        # VIVENTIUM START: authoritative signed-link termination fence
        # A signed read must not heal/mutate worker state and must never rely
        # on ambient WPR_DB_PATH to decide whether a terminal worker is live.
        if ctx.auth_mode == "signed_link":
            if str(worker.get("state") or "") == "terminated":
                raise HTTPException(status_code=404, detail="Worker not found")
            return worker
        # VIVENTIUM END: authoritative signed-link termination fence
        service.heal_worker(worker_id)
        return service.require_worker(worker_id)

    # VIVENTIUM START: authoritative signed-link termination fence
    def _require_authoritative_signed_link_worker(worker_id: str) -> dict:
        """Resolve signed-link authority only from this app's bound Store."""

        worker = store.get_worker(str(worker_id or "").strip())
        if not worker or str(worker.get("state") or "") == "terminated":
            raise HTTPException(status_code=404, detail="Worker not found")
        return worker
    # VIVENTIUM END: authoritative signed-link termination fence

    def require_run(run_id: str, request: Request | None = None) -> dict:
        ctx = _auth_context(request)
        try:
            run = service.require_run(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if ctx.is_user_scoped:
            worker = store.get_worker(str(run.get("worker_id") or ""))
            if not worker or worker.get("tenant_id") != ctx.tenant_id or worker.get("owner_id") != ctx.owner_id:
                raise HTTPException(status_code=404, detail="Run not found")
        return run

    def _profile_for_project(project: dict, requested_profile: str | None) -> str:
        return (
            str(requested_profile or "").strip()
            or str(project.get("default_worker_profile") or "").strip()
            or _configured_default_worker_profile()
        )

    def _configured_default_execution_mode() -> str:
        mode = (
            os.environ.get("GLASSHIVE_DEFAULT_EXECUTION_MODE", "").strip().lower()
            or os.environ.get("WPR_DEFAULT_EXECUTION_MODE", "docker").strip().lower()
        )
        return mode if mode in {"docker", "host"} else "docker"

    def _execution_mode_for_request(requested_mode: str | None) -> str:
        mode = str(requested_mode or "").strip().lower() or _configured_default_execution_mode()
        if mode not in {"docker", "host"}:
            raise HTTPException(status_code=400, detail="execution_mode must be docker or host")
        return mode

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

    def _strip_signed_query_params(url: str) -> str:
        parsed = urlparse(str(url or ""))
        query = urlencode(
            [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key not in SIGNED_QUERY_KEYS]
        )
        return parsed._replace(query=query).geturl()

    def _configured_redirect_hosts(request: Request) -> set[str]:
        hosts = {str(request.url.netloc or "").lower(), str(request.base_url.netloc or "").lower()}
        for name in (
            "GLASSHIVE_OPERATOR_BASE_URL",
            "WPR_OPERATOR_BASE_URL",
            "GLASSHIVE_RUNTIME_BASE_URL",
            "GLASSHIVE_RUNTIME_PUBLIC_BASE_URL",
            "GLASSHIVE_ARTIFACT_BASE_URL",
        ):
            value = str(os.environ.get(name) or "").strip()
            if value:
                parsed = urlparse(value)
                if parsed.netloc:
                    hosts.add(parsed.netloc.lower())
        for name in ("GLASSHIVE_ALLOWED_REDIRECT_HOSTS", "WPR_ALLOWED_REDIRECT_HOSTS"):
            raw = str(os.environ.get(name) or "").strip()
            for item in raw.split(","):
                value = item.strip()
                if not value:
                    continue
                parsed = urlparse(value)
                hosts.add((parsed.netloc or value).strip().rstrip("/").lower())
        return {host for host in hosts if host}

    def _validate_short_ref_redirect_target(target_url: str, request: Request) -> str:
        target = str(target_url or "").strip()
        if "\\" in target or target.startswith("//"):
            raise HTTPException(status_code=400, detail="GlassHive workspace link target path is not allowed")
        parsed = urlparse(target)
        if not parsed.scheme and not parsed.netloc:
            return target
        if parsed.scheme.lower() not in {"http", "https"}:
            raise HTTPException(status_code=400, detail="GlassHive workspace link target scheme is not allowed")
        if str(parsed.netloc or "").lower() not in _configured_redirect_hosts(request):
            raise HTTPException(status_code=403, detail="GlassHive workspace link target is not allowed")
        return target

    def _worker_cookie_name(worker_id: str) -> str:
        clean = str(worker_id or "").strip()
        if not clean or any(char in clean for char in "/\\;\x00"):
            raise HTTPException(status_code=400, detail="Invalid worker id")
        digest = sha256(clean.encode("utf-8")).hexdigest()[:24]
        return f"glasshive_gh_token_{digest}"

    def _request_uses_https(request: Request) -> bool:
        forwarded_proto = str(request.headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip().lower()
        return request.url.scheme == "https" or forwarded_proto == "https"

    def _set_signed_worker_cookie(
        response: Response,
        request: Request,
        *,
        worker_id: str,
        token: str,
        payload: dict[str, object],
    ) -> None:
        try:
            cookie_max_age = max(1, min(30 * 60, int(payload.get("exp") or 0) - int(time.time())))
        except (TypeError, ValueError):
            cookie_max_age = 30 * 60
        response.set_cookie(
            _worker_cookie_name(worker_id),
            str(token or ""),
            max_age=cookie_max_age,
            httponly=True,
            samesite="lax",
            secure=_request_uses_https(request),
        )

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

    def _profile_runtime_label(worker: dict) -> str:
        return derive_legacy_backend_label(
            profile=worker.get("profile"),
            runtime=worker.get("runtime"),
            backend=worker.get("backend"),
        )

    def _log_paths(worker: dict) -> tuple[Path, Path]:
        runtime_name = _profile_runtime_label(worker)
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
        pending: deque[Path] = deque([root])
        while pending:
            current_path = pending.popleft()
            try:
                entries = sorted(os.scandir(current_path), key=lambda entry: entry.name)
            except OSError:
                continue
            next_dirs: list[Path] = []
            for entry in entries:
                path = Path(entry.path)
                try:
                    rel = path.relative_to(root)
                except ValueError:
                    continue
                if not is_user_deliverable_relative_path(rel) or len(rel.parts) > max_depth:
                    continue
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                    stat = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                items.append(
                    {
                        "path": rel.as_posix(),
                        "is_dir": is_dir,
                        "size": None if is_dir else stat.st_size,
                        "modified_at": stat.st_mtime,
                    }
                )
                if len(items) >= max_entries:
                    return items
                if is_dir and len(rel.parts) < max_depth:
                    next_dirs.append(path)
            pending.extend(next_dirs)
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
            if not is_user_deliverable_relative_path(rel):
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
        if not is_user_deliverable_relative_path(rel):
            raise HTTPException(status_code=400, detail="Artifact path is not downloadable")
        if not target.exists() or not target.is_file():
            raise HTTPException(status_code=404, detail="Artifact not found")
        max_bytes = int(os.environ.get("GLASSHIVE_ARTIFACT_DOWNLOAD_MAX_BYTES", str(100 * 1024 * 1024)))
        if max_bytes >= 0 and target.stat().st_size > max_bytes:
            raise HTTPException(status_code=413, detail="Artifact is larger than the configured download limit")
        return target

    def _artifact_query_url(worker_id: str, action: str, relative_path: str) -> str:
        return f"/v1/workers/{quote(worker_id)}/artifacts/{action}?path={quote(str(relative_path or ''), safe='')}"

    def _signed_artifact_action_url(worker: dict, relative_path: str, *, kind: str, fallback_action: str) -> str:
        if str(worker.get("state") or "") == "terminated":
            return ""
        worker_id = str(worker.get("worker_id") or "")
        token = sign_link_token(
            kind=kind,
            worker_id=worker_id,
            tenant_id=str(worker.get("tenant_id") or ""),
            owner_id=str(worker.get("owner_id") or ""),
            path=str(relative_path or "").strip().lstrip("/"),
        )
        if token:
            ref_id = create_signed_link_ref(token=token)
            return signed_link_ref_url("", ref_id) if ref_id else ""
        return _artifact_query_url(worker_id, fallback_action, relative_path)

    def _signed_watch_action_url(worker: dict) -> str:
        if str(worker.get("state") or "") == "terminated":
            return ""
        worker_id = str(worker.get("worker_id") or "")
        project_id = str(worker.get("project_id") or "")
        token = sign_link_token(
            kind="worker_view",
            worker_id=worker_id,
            tenant_id=str(worker.get("tenant_id") or ""),
            owner_id=str(worker.get("owner_id") or ""),
        )
        if token:
            watch_query = {"surface": "desktop"}
            if project_id:
                watch_query["project_id"] = project_id
            target_url = append_signed_query(
                f"/watch/{quote(worker_id, safe='')}?{urlencode(watch_query)}",
                {"gh_token": token},
            )
            ref_id = create_signed_link_ref(token=token, target_url=target_url)
            return signed_link_ref_url("", ref_id, route="/r") if ref_id else ""
        return f"/ui/workers/{quote(worker_id)}/view?project_id={quote(project_id)}"

    def _deliverable_with_action_urls(worker: dict, deliverable: dict[str, object] | None) -> dict[str, object] | None:
        if not deliverable:
            return None
        payload = dict(deliverable)
        workspace_path = str(payload.get("workspace_path") or "").strip().lstrip("/")
        if payload.get("kind") == "file" and workspace_path and is_user_deliverable_relative_path(workspace_path):
            payload["open_url"] = _signed_artifact_action_url(
                worker,
                workspace_path,
                kind="artifact_open",
                fallback_action="open",
            )
            payload["download_url"] = _signed_artifact_action_url(
                worker,
                workspace_path,
                kind="artifact_download",
                fallback_action="download",
            )
        return payload

    def _artifact_items_with_action_urls(
        worker: dict,
        workspace_items: list[dict[str, object]] | None = None,
        *,
        max_entries: int = 100,
    ) -> list[dict[str, object]]:
        items: list[dict[str, object]] = []
        source_items = workspace_items if workspace_items is not None else _workspace_items(worker, max_entries=max_entries, max_depth=8)
        for item in source_items:
            if item.get("is_dir"):
                continue
            workspace_path = str(item.get("path") or "").strip().lstrip("/")
            if not workspace_path or not is_user_deliverable_relative_path(workspace_path):
                continue
            items.append(
                {
                    **item,
                    "open_url": _signed_artifact_action_url(
                        worker,
                        workspace_path,
                        kind="artifact_open",
                        fallback_action="open",
                    ),
                    "download_url": _signed_artifact_action_url(
                        worker,
                        workspace_path,
                        kind="artifact_download",
                        fallback_action="download",
                    ),
                }
            )
            if len(items) >= max_entries:
                break
        return items

    def _artifact_mime_type(target: Path) -> str:
        guessed, _ = mimetypes.guess_type(target.name)
        if guessed:
            return guessed
        if target.suffix.lower() in TEXT_ARTIFACT_PREVIEW_EXTENSIONS:
            return "text/plain"
        return "application/octet-stream"

    def _is_text_preview_artifact(target: Path, media_type: str) -> bool:
        suffix = target.suffix.lower()
        return (
            suffix in TEXT_ARTIFACT_PREVIEW_EXTENSIONS
            or media_type.startswith("text/")
            or media_type in {"application/json", "application/xml", "application/x-yaml"}
        )

    def _read_artifact_text_preview(target: Path) -> tuple[str, bool]:
        max_bytes = int(os.environ.get("GLASSHIVE_ARTIFACT_PREVIEW_MAX_BYTES", str(512 * 1024)))
        max_bytes = max(4096, min(max_bytes, 5 * 1024 * 1024))
        with target.open("rb") as handle:
            visible = handle.read(max_bytes)
            truncated = bool(handle.read(1))
        return visible.decode("utf-8", errors="replace"), truncated

    def _artifact_open_page(worker: dict, target: Path, relative_path: str, request: Request) -> HTMLResponse:
        worker_id = str(worker.get("worker_id") or "")
        download_url = _signed_artifact_action_url(
            worker,
            relative_path,
            kind="artifact_download",
            fallback_action="download",
        )
        workspace_url = _signed_watch_action_url(worker)
        media_type = _artifact_mime_type(target)
        size = target.stat().st_size
        preview = ""
        if _is_text_preview_artifact(target, media_type):
            text, truncated = _read_artifact_text_preview(target)
            truncated_note = (
                "<p class=\"notice\">Preview is truncated. Use Download file for the complete artifact.</p>"
                if truncated
                else ""
            )
            preview = f"""
              {truncated_note}
              <pre class="artifact-preview">{escape(text)}</pre>
            """
        elif media_type.startswith("image/") and media_type != "image/svg+xml" and size <= 2 * 1024 * 1024:
            raw = target.read_bytes()
            encoded = base64.b64encode(raw).decode("ascii")
            preview = f'<img class="image-preview" src="data:{escape(media_type, quote=True)};base64,{encoded}" alt="{escape(target.name, quote=True)}" />'
        else:
            preview = (
                "<div class=\"no-preview\">"
                "<h2>File is ready</h2>"
                "<p>This artifact type is best opened by downloading it with the button above.</p>"
                "</div>"
            )
        html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>GlassHive file - {escape(target.name)}</title>
  <style>
    :root {{ color-scheme: dark; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    body {{ margin: 0; background: #07090d; color: #eef2f7; }}
    .shell {{ min-height: 100vh; padding: 32px; box-sizing: border-box; }}
    .topbar {{ display: flex; gap: 12px; align-items: center; justify-content: space-between; margin-bottom: 24px; }}
    .brand {{ border: 1px solid rgba(255,255,255,0.18); border-radius: 999px; padding: 8px 14px; letter-spacing: 0.16em; font-size: 12px; text-transform: uppercase; }}
    .actions {{ display: flex; gap: 10px; flex-wrap: wrap; }}
    a.button {{ color: #0b0d11; background: #f5f7fb; text-decoration: none; border-radius: 999px; padding: 10px 14px; font-weight: 700; }}
    a.secondary {{ color: #eef2f7; background: #171b22; border: 1px solid rgba(255,255,255,0.16); }}
    h1 {{ margin: 0 0 8px; font-size: clamp(28px, 5vw, 56px); line-height: 1; overflow-wrap: anywhere; }}
    .meta {{ color: #a9b2c0; margin-bottom: 24px; }}
    .artifact-preview {{ margin: 0; padding: 24px; border: 1px solid rgba(255,255,255,0.14); border-radius: 14px; background: #10141b; color: #eef2f7; white-space: pre-wrap; overflow-wrap: anywhere; line-height: 1.45; font-size: 14px; }}
    .notice, .no-preview {{ border: 1px solid rgba(255,255,255,0.14); border-radius: 14px; background: #10141b; padding: 18px; color: #cbd3df; }}
    .image-preview {{ max-width: 100%; border-radius: 14px; border: 1px solid rgba(255,255,255,0.14); background: #10141b; }}
  </style>
</head>
<body>
  <main class="shell">
    <div class="topbar">
      <div class="brand">GlassHive</div>
      <div class="actions">
        <a class="button" href="{escape(download_url, quote=True)}">Download file</a>
        <a class="button secondary" href="{escape(workspace_url, quote=True)}" target="_top" rel="noopener noreferrer">View workspace</a>
      </div>
    </div>
    <h1>{escape(target.name)}</h1>
    <div class="meta">{escape(media_type)} &middot; {size:,} bytes</div>
    {preview}
  </main>
</body>
</html>"""
        return HTMLResponse(html, headers=ARTIFACT_OPEN_SECURITY_HEADERS)

    def _sanitize_worker(worker: dict) -> dict[str, object]:
        safe = dict(worker)
        backend = derive_legacy_backend_label(
            profile=safe.get("profile"),
            runtime=safe.get("runtime"),
            backend=safe.get("backend"),
        )
        if backend:
            safe["backend"] = backend
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

    def _runtime_detail_key_is_pathlike(key: object) -> bool:
        lowered = str(key or "").strip().lower()
        return any(
            marker in lowered
            for marker in (
                "path",
                "paths",
                "dir",
                "directory",
                "root",
                "home",
                "workspace",
                "log",
                "logs",
            )
        )

    def _runtime_detail_value_is_local_path(value: object) -> bool:
        text = str(value or "").strip()
        return text.startswith(("/", "~/")) or text.startswith("file:///")

    def _runtime_details_without_paths(value: object, *, key: object = "") -> object:
        if _runtime_detail_key_is_pathlike(key):
            return None
        if isinstance(value, dict):
            cleaned: dict[str, object] = {}
            for nested_key, nested_value in value.items():
                safe_value = _runtime_details_without_paths(nested_value, key=nested_key)
                if safe_value is not None and safe_value != "" and safe_value != [] and safe_value != {}:
                    cleaned[str(nested_key)] = safe_value
            return cleaned
        if isinstance(value, list):
            cleaned_items = [
                item
                for item in (_runtime_details_without_paths(item, key=key) for item in value)
                if item is not None and item != "" and item != [] and item != {}
            ]
            return cleaned_items
        if isinstance(value, str) and _runtime_detail_value_is_local_path(value):
            return None
        return value

    def _runtime_details_for_display(details: dict[str, object]) -> dict[str, object]:
        cleaned = _runtime_details_without_paths(details)
        return cleaned if isinstance(cleaned, dict) else {}

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
        deliverable = _deliverable_with_action_urls(
            worker,
            deliverable_payload(worker, latest_run, latest_output, stdout_text, stderr_text),
        )
        show_internal = _can_show_internal_details(ctx)
        workspace_items = _workspace_items(worker, max_entries=120, max_depth=8)
        workspace_summary_items = [
            item
            for item in workspace_items
            if len(str(item.get("path") or "").split("/")) <= 3
        ][:120]
        show_diagnostics = show_internal and request is not None and str(request.query_params.get("diagnostics") or "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        runtime_details_payload = (
            runtime_details
            if show_diagnostics
            else _runtime_details_for_display(runtime_details)
            if show_internal
            else _redact_runtime_details(runtime_details)
        )
        return {
            "worker": _sanitize_worker(worker) if show_internal else _redact_worker_for_member(worker),
            "latest_run": latest_run if show_internal or latest_run is None else _redact_run_for_member(latest_run),
            "latest_output": latest_output,
            "runs": runs if show_internal else [_redact_run_for_member(run) for run in runs],
            "project_runs": project_runs if show_internal else [_redact_run_for_member(run) for run in project_runs],
            "events": events if show_internal else [_redact_event_for_member(event) for event in events],
            "runtime_details": runtime_details_payload,
            "console": {
                "stdout": stdout_text if show_internal else "",
                "stderr": stderr_text if show_internal else "",
            },
            **(host_visibility if show_internal and show_diagnostics else {}),
            "workspace": {
                "root": worker.get("workspace_dir") or "" if show_diagnostics else "",
                "items": workspace_summary_items,
            },
            "artifacts": {
                "latest_image_name": latest_image.name if latest_image else None,
                "latest_image_url": f"/v1/workers/{worker_id}/artifacts/latest-image" if latest_image else None,
                "items": _artifact_items_with_action_urls(worker, workspace_items),
            },
            "deliverable": deliverable,
        }

    app.state.conversation_provider = install_conversation_provider_routes(
        app,
        store=store,
        service=service,
        tenant_for_request=lambda request: (
            _auth_context(request).tenant_id if _auth_context(request).enterprise else "local"
        ),
        provider_token=api_token,
    )

    def _account_scope(request: Request) -> tuple[str, str]:
        ctx = _auth_context(request)
        if ctx.auth_mode != "service_assertion" or not ctx.tenant_id or not ctx.owner_id:
            raise HTTPException(
                status_code=401,
                detail={
                    "code": "service_assertion_required",
                    "message": "A Viventium account-service assertion is required.",
                },
            )
        return ctx.tenant_id, ctx.owner_id

    def _canonical_digest(value: object) -> str:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return sha256(encoded).hexdigest()

    def _validated_idempotency_key(request: Request) -> str:
        value = str(request.headers.get("idempotency-key") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@-]{7,191}", value):
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "idempotency_key_required",
                    "message": "A valid trusted Idempotency-Key header is required.",
                },
            )
        return value

    def _encode_active_work_cursor(record: dict) -> str:
        cursor_payload = {
            "c": str(record.get("created_at") or ""),
            "u": str(record.get("updated_at") or ""),
            "v": 1,
            "w": str(record.get("work_ref") or ""),
        }
        payload_segment = base64.urlsafe_b64encode(
            json.dumps(
                cursor_payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).decode("ascii").rstrip("=")
        secret = str(
            os.environ.get("VIVENTIUM_GLASSHIVE_SERVICE_ASSERTION_SECRET") or ""
        )
        signature = hmac.new(
            secret.encode("utf-8"), payload_segment.encode("ascii"), sha256
        ).digest()
        signature_segment = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
        return f"{payload_segment}.{signature_segment}"

    def _decode_active_work_cursor(value: str) -> tuple[str, str, str]:
        invalid = HTTPException(
            status_code=400,
            detail={
                "code": "active_work_cursor_invalid",
                "message": "The active-work cursor is invalid.",
            },
        )
        token = str(value or "").strip()
        if not token or len(token) > 2048 or token.count(".") != 1:
            raise invalid
        payload_segment, signature_segment = token.split(".", 1)
        if not re.fullmatch(r"[A-Za-z0-9_-]+", payload_segment) or not re.fullmatch(
            r"[A-Za-z0-9_-]+", signature_segment
        ):
            raise invalid
        secret = str(
            os.environ.get("VIVENTIUM_GLASSHIVE_SERVICE_ASSERTION_SECRET") or ""
        )
        expected = hmac.new(
            secret.encode("utf-8"), payload_segment.encode("ascii"), sha256
        ).digest()
        try:
            supplied = base64.urlsafe_b64decode(
                signature_segment + "=" * (-len(signature_segment) % 4)
            )
            payload_bytes = base64.urlsafe_b64decode(
                payload_segment + "=" * (-len(payload_segment) % 4)
            )
            decoded = json.loads(payload_bytes)
        except (ValueError, TypeError, json.JSONDecodeError):
            raise invalid
        if not hmac.compare_digest(supplied, expected):
            raise invalid
        if not isinstance(decoded, dict) or set(decoded) != {"c", "u", "v", "w"}:
            raise invalid
        if decoded.get("v") != 1:
            raise invalid
        canonical = json.dumps(
            decoded, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        if canonical != payload_bytes:
            raise invalid
        created_at = str(decoded.get("c") or "")
        updated_at = str(decoded.get("u") or "")
        work_ref = str(decoded.get("w") or "")
        if (
            not created_at
            or len(created_at) > 64
            or not updated_at
            or len(updated_at) > 64
            or not re.fullmatch(r"work_[A-Za-z0-9_-]{8,180}", work_ref)
        ):
            raise invalid
        return updated_at, created_at, work_ref

    def _delegation_origin_ref(payload: CreateDelegationRequest) -> str:
        explicit = str(payload.origin_ref or "").strip()
        bundle = payload.bootstrap_bundle if isinstance(payload.bootstrap_bundle, dict) else {}
        callbacks = bundle.get("callbacks") if isinstance(bundle, dict) else None
        nested_value = callbacks.get("origin_ref") if isinstance(callbacks, dict) else None
        if nested_value is not None and not isinstance(nested_value, str):
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "delegation_origin_ref_invalid",
                    "message": "The delegation origin reference is invalid.",
                },
            )
        nested = str(nested_value or "").strip()
        if nested and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@-]{7,191}", nested):
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "delegation_origin_ref_invalid",
                    "message": "The delegation origin reference is invalid.",
                },
            )
        if explicit and nested and explicit != nested:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "delegation_origin_ref_conflict",
                    "message": "The explicit and callback origin references do not match.",
                },
            )
        return explicit or nested

    def _validated_delegation_identity(
        bundle: dict[str, object] | None,
        *,
        idempotency_key: str,
    ) -> dict[str, object] | None:
        raw = bundle.get("viventium_delegation_identity") if isinstance(bundle, dict) else None
        if raw is None:
            return None
        invalid = HTTPException(
            status_code=400,
            detail={
                "code": "delegation_identity_invalid",
                "message": "The trusted delegation identity is invalid.",
            },
        )
        if not isinstance(raw, dict):
            raise invalid
        version = raw.get("version")
        expected_fields = {
            "version",
            "idempotency_key",
            "goal_digest",
            "call_identity_digest",
            "source_event_id",
            "objective_ordinal",
        }
        if version == 2:
            expected_fields.add("launch_payload_digest")
        if set(raw) != expected_fields:
            raise invalid
        identity_key = str(raw.get("idempotency_key") or "")
        goal_digest = str(raw.get("goal_digest") or "")
        launch_payload_digest = str(raw.get("launch_payload_digest") or "")
        call_identity_digest = str(raw.get("call_identity_digest") or "")
        source_event_id = str(raw.get("source_event_id") or "")
        ordinal = raw.get("objective_ordinal")
        if (
            isinstance(version, bool)
            or version not in {1, 2}
            or not re.fullmatch(r"[0-9a-f]{64}", identity_key)
            or not re.fullmatch(r"[0-9a-f]{64}", goal_digest)
            or (
                version == 2
                and not re.fullmatch(r"[0-9a-f]{64}", launch_payload_digest)
            )
            or not re.fullmatch(r"[0-9a-f]{64}", call_identity_digest)
            or not source_event_id
            or len(source_event_id) > 512
            or any(ord(char) < 32 or ord(char) == 127 for char in source_event_id)
            or isinstance(ordinal, bool)
            or not isinstance(ordinal, int)
            or ordinal < 0
            or ordinal > 1_000_000
            or not hmac.compare_digest(identity_key, idempotency_key)
        ):
            raise invalid
        return dict(raw)

    def _delegation_digest_bundle(
        bundle: dict[str, object] | None,
    ) -> dict[str, object] | None:
        """Exclude presentation-only objective order from atomic identity."""

        if not isinstance(bundle, dict):
            return bundle
        identity = bundle.get("viventium_delegation_identity")
        if not isinstance(identity, dict):
            return bundle
        digest_identity = {
            key: value for key, value in identity.items() if key != "objective_ordinal"
        }
        return {**bundle, "viventium_delegation_identity": digest_identity}

    def _active_work_state(record: dict) -> str:
        worker_state = str(record.get("worker_state") or "")
        run_state = str(record.get("run_state") or "")
        if worker_state == "stopping":
            return "stopping"
        if worker_state == "paused" and run_state in {"queued", "running", "settling", "paused"}:
            return "paused"
        if run_state == "queued" and worker_state == "created":
            return "accepted"
        if run_state == "queued" and worker_state in {"starting", "resuming"}:
            return "starting"
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
        if run_state == "interrupted":
            return "cancelled"
        return "failed" if worker_state == "failed" else "queued"

    def _active_work_actions(record: dict, state: str) -> list[str]:
        if state in {"accepted", "queued", "starting"}:
            return ["queue", "message", "steer", "pause", "stop"]
        if state == "running":
            return ["queue", "message", "steer", "pause", "stop"]
        if state == "settling":
            return ["queue", "message", "stop"]
        if state == "paused":
            return ["queue", "message", "resume", "stop"]
        if state == "needs_input":
            return ["queue", "message", "resume", "stop"]
        if state == "failed" and bool(record.get("run_failure_retryable")):
            return ["retry", "dismiss"]
        if state in {"completed", "failed", "cancelled"}:
            return ["dismiss"]
        return []

    def _active_work_status(record: dict, state: str) -> str:
        labels = {
            "accepted": "Accepted",
            "queued": "Queued",
            "starting": "Starting",
            "running": "Running",
            "settling": "Settling native team",
            "paused": "Paused",
            "needs_input": "Needs input",
            "stopping": "Stopping",
            "completed": "Completed",
            "cancelled": "Cancelled",
            "failed": "Failed",
        }
        label = labels.get(state, state.replace("_", " ").title())
        if state == "queued" and str(record.get("run_failure_class") or "") == "host_capacity":
            return "Queued — waiting for host capacity"
        if state not in {"failed", "needs_input"}:
            return label
        user_message = " ".join(
            str(record.get("run_failure_user_message") or "").split()
        ).strip()
        safe_message = _redact_text(user_message, max_chars=500) if user_message else ""
        return f"{label}: {safe_message}" if safe_message else label

    def _active_work_view_ref(record: dict) -> str | None:
        worker_id = str(record.get("worker_id") or "")
        token = sign_link_token(
            kind="worker_view",
            worker_id=worker_id,
            tenant_id=str(record.get("tenant_id") or ""),
            owner_id=str(record.get("owner_id") or ""),
        )
        if not token:
            return None
        ref_id = create_signed_link_ref(token=token)
        if not ref_id:
            return None
        base = (
            os.environ.get("GLASSHIVE_OPERATOR_BASE_URL", "").strip()
            or os.environ.get("WPR_OPERATOR_BASE_URL", "").strip()
            or os.environ.get("GLASSHIVE_RUNTIME_PUBLIC_BASE_URL", "").strip()
            or os.environ.get("WPR_PUBLIC_BASE_URL", "").strip()
        ).rstrip("/")
        return f"{base}/w/{ref_id}" if base else f"/w/{ref_id}"

    def _active_work_provider(record: dict) -> str:
        profile = str(record.get("worker_profile") or "").strip()
        lowered = profile.lower()
        if lowered.startswith("codex"):
            return "codex"
        if lowered.startswith("claude"):
            return "claude"
        return profile or "unknown"

    def _active_work_native_team(
        capabilities: dict[str, object],
        summary: dict[str, object],
        *,
        detail: bool,
    ) -> dict[str, object] | None:
        """Project provider telemetry into a bounded, identifier-free public shape."""

        if capabilities.get("childProjection") is not True:
            return None
        children = summary.get("children")
        if not isinstance(children, list):
            children = []
        live_states = {"accepted", "pending", "queued", "starting", "running", "paused"}
        attention_states = {"paused", "failed", "unknown"}
        allowed_states = live_states | {
            "completed",
            "failed",
            "stopped",
            "cancelled",
            "unknown",
        }
        active = 0
        needs_attention = 0
        total = 0
        topology_counts: dict[tuple[str, str], int] = {}
        for raw_child in children[:10_000]:
            if not isinstance(raw_child, dict):
                continue
            state = str(raw_child.get("state") or "unknown").strip().lower()
            if state not in allowed_states:
                state = "unknown"
            role_text = " ".join(str(raw_child.get("role") or "worker").split())
            # Provider role fields can contain an agent path or arbitrary
            # account text. Only publish a small human label; paths, emails,
            # punctuation-rich identifiers, and controls collapse to worker.
            role = (
                role_text
                if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _-]{0,79}", role_text)
                else "worker"
            )
            total += 1
            active += int(state in live_states)
            needs_attention += int(state in attention_states)
            key = (role, state)
            topology_counts[key] = topology_counts.get(key, 0) + 1
        projected: dict[str, object] = {
            "active": active,
            "total": total,
            "needsAttention": needs_attention,
            "degraded": summary.get("degraded") is True
            or any(state == "unknown" for _, state in topology_counts),
        }
        if detail:
            groups = sorted(
                (
                    {"role": role, "state": state, "count": count}
                    for (role, state), count in topology_counts.items()
                ),
                key=lambda item: (str(item["state"]), str(item["role"])),
            )
            visible_groups = groups[:16]
            projected["topology"] = visible_groups
            projected["overflowCount"] = sum(
                int(item["count"]) for item in groups[len(visible_groups) :]
            )
        return projected

    def _active_work_payload(record: dict, *, detail: bool = False) -> dict[str, object]:
        state = _active_work_state(record)
        native_capabilities: dict[str, object] = {}
        native_summary: dict[str, object] = {}
        try:
            decoded_capabilities = json.loads(
                str(record.get("run_native_capabilities_json") or "{}")
            )
            if isinstance(decoded_capabilities, dict):
                native_capabilities = decoded_capabilities
            decoded_summary = json.loads(
                str(record.get("run_native_child_summary_json") or "{}")
            )
            if isinstance(decoded_summary, dict):
                native_summary = decoded_summary
        except (TypeError, json.JSONDecodeError):
            native_capabilities = {}
            native_summary = {}
        native_team = _active_work_native_team(
            native_capabilities,
            native_summary,
            detail=detail,
        )
        payload: dict[str, object] = {
            "workRef": str(record.get("work_ref") or ""),
            "title": str(record.get("title") or ""),
            "state": state,
            "statusSummary": _active_work_status(record, state),
            "provider": _active_work_provider(record),
            "originSurface": str(record.get("origin_surface") or "web"),
            # Native child projection is deliberately capability-gated. Null is
            # truthful until a provider adapter can observe the whole team.
            "nativeTeam": native_team,
            "delivery": {
                # GlassHive knows callback transport acceptance, not user-surface
                # delivery. Core enriches this pending projection from its ledger.
                "state": "pending",
                "unreadTerminal": state in {"completed", "failed", "cancelled"}
                and not bool(record.get("dismissed_at")),
            },
            "createdAt": str(record.get("created_at") or ""),
            "updatedAt": str(
                record.get("run_ended_at")
                or record.get("run_started_at")
                or record.get("updated_at")
                or ""
            ),
            "actions": _active_work_actions(record, state),
        }
        view_ref = _active_work_view_ref(record)
        if view_ref:
            payload["viewRef"] = view_ref
        if state == "failed" and not bool(record.get("run_failure_retryable")):
            payload["attention"] = {
                "kind": "input",
                "summary": _active_work_status(record, state),
            }
        if state == "needs_input":
            attention_code = str(record.get("run_failure_class") or "needs_input")
            payload["attention"] = {
                "kind": (
                    "auth"
                    if attention_code == "capability_authorization_horizon_expired"
                    else "input"
                ),
                "code": attention_code,
                "summary": _active_work_status(record, state),
            }
        if detail:
            payload["executionMode"] = str(record.get("worker_execution_mode") or "")
            payload["lifecycle"] = {
                "queuedAt": str(record.get("run_queued_at") or ""),
                "startedAt": str(record.get("run_started_at") or "") or None,
                "endedAt": str(record.get("run_ended_at") or "") or None,
            }
        return payload

    @app.post("/v1/delegations", status_code=202)
    def create_delegation(
        payload: CreateDelegationRequest,
        request: Request,
    ) -> dict[str, object]:
        tenant_id, owner_id = _account_scope(request)
        idempotency_key = _validated_idempotency_key(request)
        title = payload.title.strip()
        goal = payload.goal.strip()
        instruction = payload.instruction.strip()
        if not title or not goal or not instruction:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "delegation_request_invalid",
                    "message": "Delegation title, goal, and instruction must not be blank.",
                },
            )
        profile = payload.profile.strip() or _configured_default_worker_profile()
        execution_mode = _execution_mode_for_request(payload.execution_mode)
        origin_ref = _delegation_origin_ref(payload)
        _validated_delegation_identity(
            payload.bootstrap_bundle,
            idempotency_key=idempotency_key,
        )
        canonical_request = {
            "title": title,
            "goal": goal,
            "instruction": instruction,
            "profile": profile,
            "executionMode": execution_mode,
            "workerName": payload.worker_name.strip() or title,
            "workerRole": payload.worker_role.strip() or "General intelligent worker",
            "workspaceRoot": payload.workspace_root,
            "bootstrapProfile": payload.bootstrap_profile,
            "bootstrapBundle": _delegation_digest_bundle(payload.bootstrap_bundle),
            "originRef": origin_ref or None,
            "originSurface": payload.origin_surface,
        }
        try:
            record = service.reserve_delegation(
                tenant_id=tenant_id,
                owner_id=owner_id,
                idempotency_key=idempotency_key,
                request_digest=_canonical_digest(canonical_request),
                origin_ref=origin_ref,
                title=title,
                goal=goal,
                instruction=instruction,
                origin_surface=payload.origin_surface,
                worker_name=str(canonical_request["workerName"]),
                worker_role=str(canonical_request["workerRole"]),
                profile=profile,
                execution_mode=execution_mode,
                workspace_root=payload.workspace_root,
                bootstrap_profile=payload.bootstrap_profile,
                bootstrap_bundle=payload.bootstrap_bundle,
            )
        except DelegationIdempotencyConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": "delegation_idempotency_conflict", "message": str(exc)},
            ) from exc
        except ParallelExecutionIsolationError as exc:
            reason_code = str(getattr(exc, "reason_code", "") or "").strip()
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "parallel_execution_isolation_required",
                    "message": str(exc),
                    **({"reason": reason_code} if reason_code else {}),
                },
            ) from exc
        response = _active_work_payload(record)
        response["idempotentReplay"] = bool(record.get("idempotent_replay"))
        return response

    @app.get("/v1/active-work")
    def list_active_work(
        request: Request,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, object]:
        tenant_id, owner_id = _account_scope(request)
        bounded_limit = max(1, min(int(limit), 100))
        before = _decode_active_work_cursor(cursor) if cursor else None
        items = store.list_active_delegations(
            tenant_id=tenant_id,
            owner_id=owner_id,
            limit=bounded_limit,
            before=before,
        )
        total = store.count_active_delegations(
            tenant_id=tenant_id,
            owner_id=owner_id,
            before=before,
        )
        overflow_count = max(0, total - len(items))
        response: dict[str, object] = {
            "snapshot": "fresh",
            "work": [_active_work_payload(item) for item in items],
            "overflowCount": overflow_count,
        }
        if items and overflow_count:
            response["cursor"] = _encode_active_work_cursor(items[-1])
        return response

    @app.get("/v1/orchestration-capabilities")
    def get_orchestration_capabilities(
        request: Request, response: Response
    ) -> dict[str, object]:
        # The assertion establishes the trusted Core control plane even though
        # host-process exclusion is intentionally global to this Unix runtime.
        _account_scope(request)
        response.headers["Cache-Control"] = "no-store"
        return service.orchestration_capabilities()

    @app.post("/v1/callback-associations/verify")
    def verify_callback_association(
        payload: CallbackAssociationVerifyRequest,
        request: Request,
    ) -> dict[str, object]:
        tenant_id, owner_id = _account_scope(request)
        association = store.verify_callback_association(
            tenant_id=tenant_id,
            owner_id=owner_id,
            origin_ref=payload.origin_ref,
            work_ref=payload.work_ref,
            worker_id=payload.worker_id,
            run_id=payload.run_id,
        )
        if not association:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "callback_association_not_found",
                    "message": "The callback association was not found.",
                },
            )
        return {
            "valid": True,
            "originRef": str(association.get("origin_ref") or ""),
            "workRef": str(association.get("work_ref") or ""),
        }

    @app.get("/v1/delegations/by-origin/{origin_ref}")
    def get_delegation_by_origin(origin_ref: str, request: Request) -> dict[str, object]:
        tenant_id, owner_id = _account_scope(request)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@-]{7,191}", origin_ref):
            record = None
        else:
            record = store.get_delegation_by_origin(
                origin_ref,
                tenant_id=tenant_id,
                owner_id=owner_id,
            )
        if not record:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "delegation_not_found",
                    "message": "The delegation was not found.",
                },
            )
        return {
            "workRef": str(record.get("work_ref") or ""),
            "state": _active_work_state(record),
        }

    @app.get("/v1/active-work/{work_ref}", include_in_schema=False)
    @app.get("/v1/work/{work_ref}")
    def get_active_work(work_ref: str, request: Request) -> dict[str, object]:
        tenant_id, owner_id = _account_scope(request)
        record = store.get_delegation(work_ref, tenant_id=tenant_id, owner_id=owner_id)
        if not record:
            raise HTTPException(status_code=404, detail="Active work not found")
        return _active_work_payload(record, detail=True)

    @app.post("/v1/active-work/{work_ref}/actions", status_code=202, include_in_schema=False)
    @app.post("/v1/work/{work_ref}/actions", status_code=202)
    def active_work_action(
        work_ref: str,
        payload: ActiveWorkActionRequest,
        request: Request,
    ) -> dict[str, object]:
        tenant_id, owner_id = _account_scope(request)
        record = store.get_delegation(work_ref, tenant_id=tenant_id, owner_id=owner_id)
        if not record:
            raise HTTPException(status_code=404, detail="Active work not found")
        instruction = str(payload.instruction or "").strip()
        if payload.action in {"queue", "message", "steer"} and not instruction:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "active_work_instruction_required",
                    "message": "This active-work action requires an instruction.",
                },
            )
        capability_reauthorization = (
            payload.capability_reauthorization.model_dump()
            if payload.capability_reauthorization is not None
            else None
        )
        if capability_reauthorization is not None and payload.action != "resume":
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "capability_reauthorization_invalid",
                    "message": "Capability reauthorization is valid only for an authorization-attention resume.",
                },
            )
        action_request = {
            "action": payload.action,
            "instruction": instruction,
            "capabilityReauthorization": capability_reauthorization,
        }
        try:
            reservation = store.reserve_active_work_action(
                tenant_id=tenant_id,
                owner_id=owner_id,
                work_ref=work_ref,
                idempotency_key=payload.idempotency_key,
                action=payload.action,
                payload_digest=_canonical_digest(action_request),
                expected_current_run_id=str(record.get("current_run_id") or ""),
                expected_source_run_id=str(record.get("run_id") or ""),
                expected_source_state=str(record.get("run_state") or ""),
                expected_source_started_at=str(record.get("run_started_at") or ""),
                executor_id=service.executor_id,
                lease_seconds=float(
                    os.environ.get("WPR_ACTIVE_WORK_ACTION_LEASE_S", "30") or "30"
                ),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Active work not found") from exc
        except ActiveWorkActionConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": str(
                        getattr(exc, "code", "active_work_idempotency_conflict")
                    ),
                    "message": str(exc),
                },
            ) from exc
        should_execute = bool(reservation.get("should_execute"))
        recovery_takeover = bool(reservation.get("recovery_takeover"))
        if (
            bool(reservation.get("idempotent_replay"))
            and str(reservation.get("status") or "") == "failed"
            and str(reservation.get("response_json") or "").strip()
        ):
            try:
                failure_response = json.loads(str(reservation["response_json"]))
                failure_status = int(failure_response["statusCode"])
                failure_detail = failure_response["detail"]
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "active_work_action_result_unavailable",
                        "message": "The prior active-work action result is unavailable.",
                    },
                ) from exc
            raise HTTPException(status_code=failure_status, detail=failure_detail)
        if (
            bool(reservation.get("idempotent_replay"))
            and str(reservation.get("status") or "") != "completed"
            and payload.action in {"pause", "resume", "steer", "stop"}
            and not str(reservation.get("lifecycle_operation_id") or "").strip()
            and should_execute
        ):
            failed = store.fail_unbound_active_work_control(
                str(reservation.get("action_use_id") or ""),
                executor_id=service.executor_id,
            )
            if not failed:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "active_work_action_ownership_changed",
                        "message": "The active-work action owner changed; refresh before retrying.",
                    },
                )
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "active_work_action_binding_unavailable",
                    "message": (
                        "This unfinished control predates durable lifecycle binding. "
                        "Refresh and reissue it with a new idempotency key."
                    ),
                },
            )
        if not should_execute or recovery_takeover:
            if str(reservation.get("status") or "") != "completed":
                reconciled = service.reconcile_active_work_action(
                    record,
                    action=payload.action,
                    instruction=instruction,
                    idempotency_key=payload.idempotency_key,
                    source_run_id=str(reservation.get("source_run_id") or ""),
                    capability_reauthorization=capability_reauthorization,
                    action_use_id=str(reservation.get("action_use_id") or ""),
                )
                if reconciled is None and not should_execute:
                    exact_pending = service.active_work_action_claim_is_pending(
                        reservation
                    )
                    if exact_pending:
                        return {
                            "workRef": work_ref,
                            "action": payload.action,
                            "status": "pending",
                            "state": (
                                "stopping"
                                if payload.action == "stop"
                                else _active_work_state(record)
                            ),
                            "confirmationPending": True,
                            "idempotentReplay": True,
                            "updatedAt": str(record.get("updated_at") or ""),
                        }
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "active_work_action_in_progress",
                            "message": "The matching active-work action is still in progress.",
                            "retryAfterSeconds": 1,
                        },
                    )
                if reconciled is None:
                    # A stale/released execution lease with no committed effect
                    # is safe to take over below using the same operation ID.
                    if service.active_work_action_claim_is_pending(reservation):
                        return {
                            "workRef": work_ref,
                            "action": payload.action,
                            "status": "pending",
                            "state": (
                                "stopping"
                                if payload.action == "stop"
                                else _active_work_state(record)
                            ),
                            "confirmationPending": True,
                            "idempotentReplay": True,
                            "updatedAt": str(record.get("updated_at") or ""),
                        }
                else:
                    reconciled_run_id = str(reconciled.get("run_id") or "")
                    prior: dict[str, object] = {
                        "workRef": work_ref,
                        "action": payload.action,
                        "status": str(reconciled.get("status") or "accepted"),
                        "state": str(reconciled.get("state") or _active_work_state(record)),
                        "confirmationPending": bool(
                            reconciled.get("confirmation_pending")
                        ),
                        "idempotentReplay": True,
                        "updatedAt": str(record.get("updated_at") or ""),
                    }
                    if reconciled.get("resume_mode"):
                        prior["resumeMode"] = str(reconciled["resume_mode"])
                    if reconciled.get("delivery_mode"):
                        prior["deliveryMode"] = str(reconciled["delivery_mode"])
                    if reconciled.get("control_outcome"):
                        prior["controlOutcome"] = str(
                            reconciled["control_outcome"]
                        )
                        prior["runId"] = reconciled_run_id
                    finished_action = store.finish_active_work_action(
                        str(reservation.get("action_use_id") or ""),
                        response={**prior, "idempotentReplay": False},
                        current_run_id=(
                            reconciled_run_id
                            if payload.action in {"queue", "message", "steer", "retry"}
                            and bool(reconciled.get("advance_current_run", True))
                            else None
                        ),
                        executor_id=service.executor_id,
                    )
                    if not finished_action:
                        raise HTTPException(
                            status_code=409,
                            detail={
                                "code": "active_work_action_ownership_changed",
                                "message": "The active-work action owner changed; refresh before retrying.",
                            },
                        )
                    persisted_response = json.loads(
                        str(finished_action.get("response_json") or "{}")
                    )
                    prior["updatedAt"] = str(
                        persisted_response.get("updatedAt") or prior["updatedAt"]
                    )
                    return prior
            elif not should_execute:
                try:
                    prior = json.loads(str(reservation.get("response_json") or "{}"))
                except json.JSONDecodeError as exc:
                    raise HTTPException(
                        status_code=409,
                        detail={
                            "code": "active_work_action_result_unavailable",
                            "message": "The prior active-work action result is unavailable.",
                        },
                    ) from exc
                prior["idempotentReplay"] = True
                return prior
        action_use_id = str(reservation.get("action_use_id") or "")
        try:
            result = service.execute_active_work_action(
                record,
                action=payload.action,
                instruction=instruction,
                idempotency_key=payload.idempotency_key,
                capability_reauthorization=capability_reauthorization,
                action_use_id=action_use_id,
            )
            new_run_id = str(result.get("run_id") or "")
            response: dict[str, object] = {
                "workRef": work_ref,
                "action": payload.action,
                "status": str(result.get("status") or "accepted"),
                "state": str(result.get("state") or _active_work_state(record)),
                "confirmationPending": bool(result.get("confirmation_pending")),
                "idempotentReplay": bool(reservation.get("idempotent_replay")),
                "updatedAt": str(record.get("updated_at") or ""),
            }
            if result.get("resume_mode"):
                response["resumeMode"] = str(result["resume_mode"])
            if result.get("delivery_mode"):
                response["deliveryMode"] = str(result["delivery_mode"])
            if result.get("control_outcome"):
                response["controlOutcome"] = str(result["control_outcome"])
                response["runId"] = new_run_id
            finished_action = store.finish_active_work_action(
                action_use_id,
                response=response,
                current_run_id=(
                    new_run_id
                    if payload.action in {"queue", "message", "steer", "retry"}
                    and str(result.get("control_outcome") or "") != "terminal_won"
                    else None
                ),
                executor_id=service.executor_id,
            )
            if not finished_action:
                raise RuntimeError("active_work_action_ownership_changed")
            persisted_response = json.loads(
                str(finished_action.get("response_json") or "{}")
            )
            response["updatedAt"] = str(
                persisted_response.get("updatedAt") or response["updatedAt"]
            )
            return response
        except ValueError as exc:
            code = str(exc)
            detail = {"code": code, "message": code.replace("_", " ")}
            store.fail_active_work_action(
                action_use_id,
                code,
                executor_id=service.executor_id,
                failure_response={"statusCode": 400, "detail": detail},
            )
            raise HTTPException(
                status_code=400,
                detail=detail,
            ) from exc
        except RuntimeError as exc:
            code = str(exc)
            detail = {"code": code, "message": code.replace("_", " ")}
            store.fail_active_work_action(
                action_use_id,
                code,
                executor_id=service.executor_id,
                failure_response={"statusCode": 409, "detail": detail},
            )
            raise HTTPException(
                status_code=409,
                detail=detail,
            ) from exc
        except Exception as exc:
            store.fail_active_work_action(
                action_use_id, str(exc), executor_id=service.executor_id
            )
            raise

    @app.get("/health")
    def health() -> dict[str, object]:
        default_profile = _configured_default_worker_profile()
        visible_runtime_backend = resolved_runtime_backend
        if resolved_runtime_backend == "openclaw":
            visible_runtime_backend = (
                _profile_runtime_label({"profile": default_profile, "runtime": resolved_runtime_backend})
                or resolved_runtime_backend
            )
        payload: dict[str, object] = {
            "status": "ok",
            "version": app.version,
            "runtime_backend": visible_runtime_backend,
            "default_worker_profile": default_profile,
            "allowed_worker_profiles": allowed_worker_profiles(),
        }
        if not auth_settings.enterprise:
            payload["metrics"] = store.metrics()
        return payload

    @app.get("/favicon.ico")
    def favicon() -> Response:
        return Response(status_code=204)

    @app.get("/v1/preferences", response_model=UserPreferencesResponse)
    def get_preferences(request: Request) -> UserPreferencesResponse:
        ctx = _auth_context(request)
        tenant_id = ctx.tenant_id if ctx.is_user_scoped else "local"
        owner_id = _preference_owner(ctx)
        if ctx.enterprise and not owner_id:
            raise HTTPException(status_code=401, detail="Missing authenticated user assertion")
        prefs = store.get_user_preferences(tenant_id, owner_id) or _blank_preferences(tenant_id, owner_id)
        return UserPreferencesResponse(**prefs)

    @app.patch("/v1/preferences", response_model=UserPreferencesResponse)
    def update_preferences(payload: UpdateUserPreferencesRequest, request: Request) -> UserPreferencesResponse:
        ctx = _auth_context(request)
        tenant_id = ctx.tenant_id if ctx.is_user_scoped else "local"
        owner_id = _preference_owner(ctx)
        if ctx.enterprise and not owner_id:
            raise HTTPException(status_code=401, detail="Missing authenticated user assertion")
        normalized = _normalize_preference_payload(payload)
        prefs = store.upsert_user_preferences(tenant_id=tenant_id, owner_id=owner_id, **normalized)
        return UserPreferencesResponse(**prefs)

    @app.post("/v1/projects", response_model=ProjectResponse, status_code=201)
    def create_project(payload: CreateProjectRequest, request: Request) -> ProjectResponse:
        ctx = _auth_context(request)
        owner_id = _request_owner(ctx, payload.owner_id)
        tenant_id = ctx.tenant_id if ctx.is_user_scoped else "local"
        profile = payload.default_worker_profile.strip() or _configured_default_worker_profile()
        return ProjectResponse(**service.create_project(owner_id, payload.title, payload.goal, profile, tenant_id=tenant_id))

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
        tenant_id = str(
            project.get("tenant_id")
            or (ctx.tenant_id if ctx.is_user_scoped else "local")
        )
        profile = _profile_for_project(project, payload.profile)
        execution_mode = _execution_mode_for_request(payload.execution_mode)
        worker = service.create_worker(
            project_id=project_id,
            owner_id=owner_id,
            name=payload.name,
            role=payload.role,
            profile=profile,
            backend=payload.backend,
            execution_mode=execution_mode,
            alias=scoped_alias(ctx, payload.alias or payload.name)
            if ctx.is_user_scoped
            else payload.alias,
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
        tenant_id = str(
            project.get("tenant_id")
            or (ctx.tenant_id if ctx.is_user_scoped else "local")
        )
        profile = _profile_for_project(project, payload.profile)
        execution_mode = _execution_mode_for_request(payload.execution_mode)
        alias = (payload.alias or payload.name or profile).strip()
        if ctx.is_user_scoped:
            alias = scoped_alias(ctx, alias)
        worker = service.find_or_create_worker(
            project_id=project_id,
            owner_id=owner_id,
            name=payload.name,
            role=payload.role,
            profile=profile,
            backend=payload.backend,
            alias=alias,
            execution_mode=execution_mode,
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
        ctx = _auth_context(request)
        if ctx.auth_mode == "signed_link" and payload.bootstrap_bundle:
            raise HTTPException(status_code=403, detail="Signed workspace links cannot modify worker bootstrap context")
        worker = require_worker(worker_id, request)
        run = service.assign_run(
            worker_id,
            payload.instruction,
            runtime_bundle=merge_runtime_bundles(
                _assign_effort_bundle(worker, payload.effort),
                payload.bootstrap_bundle,
            ),
            idempotency_key=str(request.headers.get("x-glasshive-idempotency-key") or "").strip()
            or None,
        )
        return RunResponse(**run)

    @app.get("/v1/workers/{worker_id}/assignments/by-idempotency/{idempotency_key}", response_model=RunResponse)
    def get_assignment_by_idempotency(
        worker_id: str,
        idempotency_key: str,
        request: Request,
    ) -> RunResponse:
        require_worker(worker_id, request)
        run_id = "run_idem_" + sha256(f"{worker_id}\0{idempotency_key}".encode("utf-8")).hexdigest()[:32]
        run = store.get_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Assignment not found")
        return RunResponse(**run)

    @app.post(ACTION_ENDPOINT, response_model=RunActionResponse, status_code=202)
    def execute_run_action(
        payload: RunActionRequest,
        request: Request,
    ) -> RunActionResponse:
        claims = getattr(request.state, "run_action_claims", None)
        if not isinstance(claims, dict):
            raise HTTPException(
                status_code=401,
                detail={"code": "capability_required", "message": "A scoped action capability is required."},
            )
        try:
            result = service.execute_run_action(
                claims,
                capability_id=payload.capabilityId,
                action=payload.action,
                project_id=payload.projectId,
                worker_id=payload.workerId,
                run_id=payload.runId,
                idempotency_key=payload.idempotencyKey,
            )
        except RunActionError as exc:
            if exc.code == "run_already_completed" and payload.action == "cancel":
                return JSONResponse(
                    status_code=409,
                    content={
                        "version": 1,
                        "status": "already_completed",
                        "action": "cancel",
                        "projectId": payload.projectId,
                        "workerId": payload.workerId,
                        "sourceRunId": payload.runId,
                        "state": "completed",
                    },
                )
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc
        return RunActionResponse(**result)

    @app.post("/v1/workers/{worker_id}/message", response_model=RunResponse, status_code=202)
    def send_message(worker_id: str, payload: SendMessageRequest, request: Request) -> RunResponse:
        require_worker(worker_id, request)
        run = service.send_message(worker_id, payload.message)
        return RunResponse(**run)

    @app.post("/v1/workers/{worker_id}/schedule", response_model=ScheduleResponse, status_code=202)
    def schedule_worker_run(worker_id: str, payload: ScheduleRunRequest, request: Request) -> ScheduleResponse:
        ctx = _auth_context(request)
        if ctx.auth_mode == "signed_link" and payload.bootstrap_bundle:
            raise HTTPException(status_code=403, detail="Signed workspace links cannot modify worker bootstrap context")
        require_worker(worker_id, request)
        try:
            schedule = service.schedule_run(
                worker_id,
                payload.instruction,
                run_at=payload.run_at,
                schedule_text=payload.schedule_text,
                delay_seconds=payload.delay_seconds,
                runtime_bundle=payload.bootstrap_bundle,
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

    @app.post("/v1/workers/{worker_id}/view-opened", status_code=204)
    def worker_view_opened(worker_id: str, request: Request) -> Response:
        worker = require_worker(worker_id, request)
        # VIVENTIUM START: authoritative operator-view revocation
        # The UI uses this scoped runtime response as its final authorization
        # gate, so terminal workers must fail before any redirect/cookie.
        if str(worker.get("state") or "") == "terminated":
            raise HTTPException(status_code=404, detail="Worker not found")
        # VIVENTIUM END: authoritative operator-view revocation
        store.add_event(worker["project_id"], worker_id, None, "worker.view_opened", "Worker view opened")
        return Response(status_code=204)

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
                notes="GlassHive takeover exposes the worker workstation desktop through a live browser view, with terminal control still available as a secondary surface.",
            )
        return TakeoverInfo(
            supported=True,
            url=absolute_terminal_url(request, worker_id),
            mode="web-terminal",
            notes="GlassHive takeover is a real terminal session in the worker runtime. Desktop streaming stays deferred for this worker type.",
        )

    @app.get("/v1/workers/{worker_id}/artifacts/latest-image")
    def latest_worker_image(worker_id: str, request: Request) -> FileResponse:
        worker = require_worker(worker_id, request)
        latest = _latest_image_path(worker)
        if latest is None or not latest.exists():
            raise HTTPException(status_code=404, detail="No image artifacts found for this worker")
        return FileResponse(latest)

    def _require_authenticated_link_ref_scope(payload: dict[str, object], request: Request) -> None:
        ctx = _auth_context(request)
        if not ctx.enterprise:
            return
        tenant_id = str(payload.get("tenant_id") or "")
        owner_id = str(payload.get("owner_id") or "")
        if tenant_id != ctx.tenant_id or not owner_matches_auth_context(owner_id, ctx):
            raise HTTPException(status_code=404, detail="GlassHive link not found for this user")

    def _require_authenticated_short_ref_scope_if_asserted(payload: dict[str, object], request: Request) -> None:
        if not auth_settings.enterprise:
            return
        headers = {str(key).lower(): value for key, value in request.headers.items()}
        has_identity_assertion = any(
            header_identity_value(headers, name)
            for name in (
                auth_settings.tenant_header,
                auth_settings.user_header,
                auth_settings.email_header,
                auth_settings.role_header,
            )
        )
        if not has_identity_assertion:
            raise HTTPException(
                status_code=401,
                detail="GlassHive enterprise runtime requires an authenticated user assertion from the trusted proxy",
            )
        try:
            ctx = auth_settings.context_from_headers(headers)
        except GlassHiveAuthError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        tenant_id = str(payload.get("tenant_id") or "")
        owner_id = str(payload.get("owner_id") or "")
        if tenant_id != ctx.tenant_id or not owner_matches_auth_context(owner_id, ctx):
            raise HTTPException(status_code=404, detail="GlassHive workspace link not found for this user")

    def _fresh_worker_view_token(payload: dict[str, object]) -> tuple[str, dict[str, object]]:
        worker_id = str(payload.get("worker_id") or "").strip()
        _require_authoritative_signed_link_worker(worker_id)
        token = sign_link_token(
            kind="worker_view",
            worker_id=worker_id,
            tenant_id=str(payload.get("tenant_id") or ""),
            owner_id=str(payload.get("owner_id") or ""),
            path=str(payload.get("path") or ""),
        )
        refreshed_payload = verify_signed_link_token(token) if token else None
        if not token or not isinstance(refreshed_payload, dict):
            raise HTTPException(status_code=500, detail="GlassHive workspace session could not be refreshed")
        return token, refreshed_payload

    def _open_verified_signed_link(payload: dict[str, object], request: Request) -> Response:
        worker_id = str(payload.get("worker_id") or "").strip()
        worker = _require_authoritative_signed_link_worker(worker_id)
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
        if kind in {"artifact_download", "artifact_open"}:
            # Re-read immediately before resolving/serving bytes so a token
            # verified against an earlier live snapshot cannot outlive it.
            worker = _require_authoritative_signed_link_worker(worker_id)
            path = str(payload.get("path") or "").strip().lstrip("/")
            target = _artifact_path(worker, path)
            if kind == "artifact_open":
                worker = _require_authoritative_signed_link_worker(worker_id)
                store.add_event(worker["project_id"], worker_id, None, "worker.artifact_opened", target.name)
                return _artifact_open_page(worker, target, path, request)
            worker = _require_authoritative_signed_link_worker(worker_id)
            store.add_event(worker["project_id"], worker_id, None, "worker.artifact_downloaded", target.name)
            return FileResponse(target, filename=target.name, headers=ARTIFACT_DOWNLOAD_SECURITY_HEADERS)
        raise HTTPException(status_code=400, detail="Signed link kind is not supported")

    @app.get("/v1/signed-links/{token}")
    def open_signed_link(token: str, request: Request) -> Response:
        payload = verify_signed_link_token(token)
        if not payload:
            raise HTTPException(status_code=401, detail="Signed link is invalid or expired")
        return _open_verified_signed_link(payload, request)

    @app.get("/v1/link-refs/{ref_id}")
    def open_signed_link_ref(ref_id: str, request: Request) -> Response:
        record = resolve_signed_link_ref(ref_id)
        if not record:
            raise HTTPException(status_code=401, detail="Signed link reference is invalid or expired")
        payload = record.get("payload")
        if not isinstance(payload, dict):
            raise HTTPException(status_code=401, detail="Signed link reference is invalid or expired")
        _require_authenticated_link_ref_scope(payload, request)
        return _open_verified_signed_link(payload, request)

    @app.get("/r/{ref_id}")
    def open_relative_signed_link_ref(ref_id: str, request: Request) -> Response:
        record = resolve_signed_link_ref(ref_id)
        if not record:
            raise HTTPException(status_code=401, detail="Signed link reference is invalid or expired")
        payload = record.get("payload")
        if not isinstance(payload, dict) or str(payload.get("kind") or "") != "worker_view":
            raise HTTPException(status_code=403, detail="This GlassHive link cannot open a workspace")
        _require_authenticated_short_ref_scope_if_asserted(payload, request)
        worker_id = str(payload.get("worker_id") or "").strip()
        if not worker_id:
            raise HTTPException(status_code=401, detail="Signed link reference is invalid or expired")
        target_url = _validate_short_ref_redirect_target(
            _strip_signed_query_params(str(record.get("target_url") or "").strip()),
            request,
        )
        if not target_url:
            raise HTTPException(status_code=400, detail="Signed link reference has no target")
        worker = _require_authoritative_signed_link_worker(worker_id)
        if (
            str(payload.get("tenant_id") or "") != str(worker.get("tenant_id") or "")
            or str(payload.get("owner_id") or "") != str(worker.get("owner_id") or "")
        ):
            raise HTTPException(status_code=404, detail="GlassHive workspace link not found")
        store.add_event(worker["project_id"], worker_id, None, "worker.view_opened", "Worker view opened")
        response = RedirectResponse(target_url, status_code=307)
        session_token, session_payload = _fresh_worker_view_token(payload)
        _set_signed_worker_cookie(
            response,
            request,
            worker_id=worker_id,
            token=session_token,
            payload=session_payload,
        )
        return response

    def _require_worker_view_ref(ref_id: str, request: Request) -> tuple[dict[str, object], dict]:
        record = resolve_signed_link_ref(ref_id)
        if not record:
            raise HTTPException(status_code=401, detail="GlassHive workspace link is invalid or expired")
        payload = record.get("payload")
        if not isinstance(payload, dict) or str(payload.get("kind") or "") != "worker_view":
            raise HTTPException(status_code=403, detail="This GlassHive link cannot open a workspace")
        _require_authenticated_short_ref_scope_if_asserted(payload, request)
        worker_id = str(payload.get("worker_id") or "").strip()
        if not worker_id:
            raise HTTPException(status_code=401, detail="GlassHive workspace link is invalid or expired")
        worker = _require_authoritative_signed_link_worker(worker_id)
        tenant_id = str(payload.get("tenant_id") or "")
        owner_id = str(payload.get("owner_id") or "")
        if tenant_id != str(worker.get("tenant_id") or "") or owner_id != str(worker.get("owner_id") or ""):
            raise HTTPException(status_code=404, detail="GlassHive workspace link not found")
        request.state.auth_context = AuthContext(
            tenant_id=str(worker.get("tenant_id") or "local"),
            user_id=str(worker.get("owner_id") or ""),
            auth_mode="signed_link",
            enterprise=auth_settings.enterprise,
        )
        return payload, worker

    def _response_with_worker_view_cookie(
        response: Response,
        request: Request,
        *,
        payload: dict[str, object],
        worker_id: str,
    ) -> Response:
        session_token, session_payload = _fresh_worker_view_token(payload)
        _set_signed_worker_cookie(
            response,
            request,
            worker_id=worker_id,
            token=session_token,
            payload=session_payload,
        )
        return response

    @app.get("/w/{ref_id}", response_class=HTMLResponse)
    def open_ref_workspace_view(ref_id: str, request: Request) -> Response:
        payload, worker = _require_worker_view_ref(ref_id, request)
        worker_id = str(worker.get("worker_id") or "")
        store.add_event(worker["project_id"], worker_id, None, "worker.view_opened", "Worker view opened")
        runtime_details = _runtime_details(worker)
        # Public worker-view references are intentionally presentation-only.
        # Control flows use owner-scoped service assertions or one-use exact-run
        # action capabilities; a leaked view reference must never be upgraded.
        return HTMLResponse(
            f"""
            <html><head><title>{escape(worker['name'])} mission view</title>
            <style>
              body {{ font-family: system-ui,sans-serif; margin:0; background:#0f172a; color:#e5e7eb; }}
              main {{ max-width:760px; margin:8vh auto; padding:1.5rem; }}
              .card {{ border:1px solid rgba(255,255,255,.14); border-radius:16px; padding:1.5rem; background:#111827; }}
              .meta {{ color:#cbd5e1; }} .notice {{ color:#94a3b8; border-top:1px solid rgba(255,255,255,.12); padding-top:1rem; }}
            </style></head><body><main><div class="card">
              <h1>{escape(worker['name'])}</h1>
              <p class="meta">{escape(str(runtime_details.get('mode') or worker.get('runtime') or 'worker'))} · {escape(str(worker.get('state') or 'unknown'))}</p>
              <p>Read-only mission view</p>
              <p class="notice">Mission controls require an authenticated, action-scoped, one-use capability.</p>
            </div></main></body></html>
            """,
            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
        )
        external_view_url = str(runtime_details.get("view_url") or "").strip()
        subtitle = escape(str(runtime_details.get("mode") or worker.get("runtime") or "worker view"))
        openclaw_action_button = (
            '<button onclick="desktopAction(\'openclaw\')">OpenClaw</button>'
            if str(worker.get("profile") or "").startswith("openclaw")
            else ""
        )
        ref_route = f"/w/{escape(ref_id, quote=True)}"
        desktop_route = f"{ref_route}/desktop"
        desktop_frame_route = f"{ref_route}/desktop-frame"
        if not external_view_url:
            response = HTMLResponse(
                f"""
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
                    </div>
                  </body>
                </html>
                """
            )
            return _response_with_worker_view_cookie(response, request, payload=payload, worker_id=worker_id)
        html = f"""
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
              iframe {{ width: 100%; height: calc(100vh - 98px); border: 0; background: #020617; }}
            </style>
          </head>
          <body>
            <header>
              <div>
                <div><strong>{escape(worker['name'])}</strong></div>
                <div class="meta">{subtitle} · managed workspace</div>
                <div class="meta"><a href="{desktop_route}" target="_blank" rel="noreferrer">Desktop directly</a></div>
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
                {openclaw_action_button}
              </div>
            </header>
            <iframe src="{desktop_frame_route}" loading="eager"></iframe>
            <script>
              const refRoute = {ref_route!r};
              const directDesktopUrl = {desktop_route!r};
              async function action(name) {{
                await fetch(`${{refRoute}}/actions/${{encodeURIComponent(name)}}`, {{ method: 'POST' }});
              }}
              async function pauseAndOpenDirect() {{
                await action('pause');
                window.open(directDesktopUrl, '_blank', 'noopener');
              }}
              async function desktopAction(name, url='') {{
                const res = await fetch(`${{refRoute}}/desktop-action`, {{
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
        response = HTMLResponse(html)
        return _response_with_worker_view_cookie(response, request, payload=payload, worker_id=worker_id)

    @app.get("/w/{ref_id}/desktop", response_class=HTMLResponse)
    def open_ref_workspace_desktop(ref_id: str, request: Request) -> Response:
        payload, worker = _require_worker_view_ref(ref_id, request)
        raise HTTPException(status_code=403, detail="Read-only view links cannot open an interactive desktop")
        worker_id = str(worker.get("worker_id") or "")
        runtime_details = _runtime_details(worker)
        external_view_url = str(runtime_details.get("view_url") or "").strip()
        if not external_view_url:
            raise HTTPException(status_code=404, detail="GlassHive desktop view is not available")
        ref_route = f"/w/{escape(ref_id, quote=True)}"
        desktop_frame_route = f"{ref_route}/desktop-frame"
        response = HTMLResponse(
            f"""
            <html>
              <head>
                <title>{escape(worker['name'])} desktop</title>
                <style>
                  body {{ margin: 0; background: #020617; color: #e5e7eb; font-family: system-ui, sans-serif; }}
                  header {{ padding: .75rem 1rem; border-bottom: 1px solid rgba(255,255,255,.12); display: flex; justify-content: space-between; align-items: center; gap: 1rem; }}
                  a {{ color: #93c5fd; }}
                  iframe {{ width: 100%; height: calc(100vh - 54px); border: 0; background: #020617; }}
                </style>
              </head>
              <body>
                <header>
                  <strong>{escape(worker['name'])}</strong>
                  <a href="{ref_route}" target="_top">Back to workspace controls</a>
                </header>
                <iframe src="{desktop_frame_route}" loading="eager"></iframe>
              </body>
            </html>
            """
        )
        return _response_with_worker_view_cookie(response, request, payload=payload, worker_id=worker_id)

    @app.get("/w/{ref_id}/desktop-frame")
    def open_ref_workspace_desktop_frame(ref_id: str, request: Request) -> Response:
        payload, worker = _require_worker_view_ref(ref_id, request)
        raise HTTPException(status_code=403, detail="Read-only view links cannot open an interactive desktop")
        worker_id = str(worker.get("worker_id") or "")
        runtime_details = _runtime_details(worker)
        external_view_url = str(runtime_details.get("view_url") or "").strip()
        if not external_view_url:
            raise HTTPException(status_code=404, detail="GlassHive desktop view is not available")
        response = RedirectResponse(external_view_url, status_code=307)
        return _response_with_worker_view_cookie(response, request, payload=payload, worker_id=worker_id)

    @app.post("/w/{ref_id}/actions/{action_name}", status_code=202)
    def ref_workspace_action(ref_id: str, action_name: str, request: Request) -> dict[str, object]:
        payload, worker = _require_worker_view_ref(ref_id, request)
        raise HTTPException(status_code=403, detail="Read-only view links cannot control work")
        worker_id = str(worker.get("worker_id") or "")
        action = str(action_name or "").strip().lower()
        if action == "resume":
            updated = service.resume_worker(worker_id)
        elif action == "pause":
            updated = service.pause_worker(worker_id)
        elif action == "interrupt":
            updated = service.interrupt_worker(worker_id)
        elif action == "terminate":
            updated = service.terminate_worker(worker_id)
        else:
            raise HTTPException(status_code=400, detail="Unsupported workspace action")
        response = {"status": "ok", "state": str(updated.get("state") or "")}
        _ = payload
        return response

    @app.post("/w/{ref_id}/desktop-action", response_model=DesktopActionResponse, status_code=202)
    def ref_workspace_desktop_action(ref_id: str, payload: DesktopActionRequest, request: Request) -> DesktopActionResponse:
        _ref_payload, worker = _require_worker_view_ref(ref_id, request)
        raise HTTPException(status_code=403, detail="Read-only view links cannot control a desktop")
        worker_id = str(worker.get("worker_id") or "")
        try:
            launched = service.desktop_action(worker_id, payload.action, url=payload.url, run_id=payload.run_id)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        resolved_url = str(launched.get("url") or launched.get("view_url") or f"/w/{quote(ref_id, safe='')}/desktop")
        notes = str(launched.get("notes") or "")
        return DesktopActionResponse(
            action=str(launched.get("action") or payload.action),
            status=str(launched.get("status") or "launched"),
            mode=str(launched.get("mode") or "workstation-desktop"),
            url=resolved_url,
            view_url=str(launched.get("view_url") or resolved_url),
            notes=notes or None,
        )

    @app.get("/v1/workers/{worker_id}/artifacts")
    def list_worker_artifacts(worker_id: str, request: Request) -> dict[str, object]:
        worker = require_worker(worker_id, request)
        items = [
            {
                **item,
                "open_url": _artifact_query_url(worker_id, "open", str(item["path"])),
                "download_url": _artifact_query_url(worker_id, "download", str(item["path"])),
            }
            for item in _workspace_items(worker, max_entries=500, max_depth=8)
            if not item.get("is_dir")
        ]
        store.add_event(worker["project_id"], worker_id, None, "worker.artifacts_listed", "Workspace artifacts listed")
        return {"items": items}

    @app.get("/v1/workers/{worker_id}/artifacts/open")
    def open_worker_artifact(worker_id: str, path: str, request: Request) -> HTMLResponse:
        worker = require_worker(worker_id, request)
        if _auth_context(request).auth_mode == "signed_link":
            worker = _require_authoritative_signed_link_worker(worker_id)
        target = _artifact_path(worker, path)
        if _auth_context(request).auth_mode == "signed_link":
            worker = _require_authoritative_signed_link_worker(worker_id)
        store.add_event(worker["project_id"], worker_id, None, "worker.artifact_opened", target.name)
        return _artifact_open_page(worker, target, path, request)

    @app.get("/v1/workers/{worker_id}/artifacts/download")
    def download_worker_artifact(worker_id: str, path: str, request: Request) -> FileResponse:
        worker = require_worker(worker_id, request)
        if _auth_context(request).auth_mode == "signed_link":
            worker = _require_authoritative_signed_link_worker(worker_id)
        target = _artifact_path(worker, path)
        if _auth_context(request).auth_mode == "signed_link":
            worker = _require_authoritative_signed_link_worker(worker_id)
        store.add_event(worker["project_id"], worker_id, None, "worker.artifact_downloaded", target.name)
        return FileResponse(target, filename=target.name, headers=ARTIFACT_DOWNLOAD_SECURITY_HEADERS)

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
        default_profile = _configured_default_worker_profile()
        profile_options = _ui_worker_profile_options(default_profile)
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
            "<html><head><title>GlassHive Workspace Runtime</title>"
            "<style>body{font-family:system-ui,sans-serif;margin:2rem;max-width:1100px;}"
            "section{border:1px solid #ddd;padding:1rem;border-radius:12px;margin-bottom:1rem;}"
            "code,pre{background:#f6f6f6;padding:.2rem .4rem;border-radius:6px;}"
            "input,textarea,button{font:inherit;padding:.55rem;}"
            "</style></head><body>"
            "<h1>GlassHive Workspace Runtime</h1>"
            f"<p>Standalone workspace control plane. Default worker profile: {escape(default_profile)}.</p>"
            f"{docs_link}"
            "<section>"
            "<h2>Create project</h2>"
            "<p><input id='project-owner' placeholder='Owner ID' value='demo-owner'/></p>"
            "<p><input id='project-title' placeholder='Project title' value='New Project'/></p>"
            "<p><textarea id='project-goal' placeholder='Project goal' style='width:100%;min-height:90px;'>Describe the goal for this worker project.</textarea></p>"
            "<p><select id='project-profile'>"
            f"{profile_options}"
            "</select></p>"
            "<p><button onclick='createProject()'>Create project</button></p>"
            "</section>"
            f"{body}"
            "<script>"
            "async function createProject(){"
            "const owner_id=document.getElementById('project-owner').value.trim();"
            "const title=document.getElementById('project-title').value.trim();"
            "const goal=document.getElementById('project-goal').value.trim();"
            f"const default_worker_profile=document.getElementById('project-profile').value.trim()||'{escape(default_profile)}';"
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
        project_default_profile = (
            str(project.get("default_worker_profile") or "").strip()
            or _configured_default_worker_profile()
        )
        project_default_profile_json = json.dumps(project_default_profile)
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
                openclaw_action_button = (
                    '<button onclick="desktopAction(\'openclaw\')">Open OpenClaw</button>'
                    if str(selected_worker.get("profile") or "").startswith("openclaw")
                    else ""
                )
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
	                    {openclaw_action_button}
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
	                      {_ui_worker_profile_options(project_default_profile)}
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
                const profile = document.getElementById('worker-profile').value.trim() || {project_default_profile_json};
                if (!owner_id || !name) {{
                  alert('worker owner and name are required');
                  return '';
                }}
                const res = await fetch(`/v1/projects/${{projectId}}/workers`, {{
                  method: 'POST',
                  headers: {{ 'Content-Type': 'application/json' }},
                  body: JSON.stringify({{ owner_id, name, role, profile }})
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
        show_raw_paths = show_internal and str(request.query_params.get("diagnostics") or "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        is_host_worker = str(worker.get("execution_mode") or "docker") == "host"
        artifacts = live["artifacts"]
        latest_run_marker = escape(str((latest_run or {}).get("ended_at") or (latest_run or {}).get("started_at") or ""), quote=True)
        signed_query_suffix = ""
        signed_query_json = json.dumps("")
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

        runtime_details_for_page = dict(runtime_details) if show_raw_paths else _runtime_details_for_display(runtime_details)
        detail_items = "".join(
            f"<li><strong>{escape(str(key).replace('_', ' ').title())}:</strong> {detail_value_html(value)}</li>"
            for key, value in runtime_details_for_page.items()
            if value is not None and value != "" and value != []
        ) or "<li>No runtime details yet</li>"
        worker_id_row = f"<p><strong>Worker ID:</strong> {escape(worker['worker_id'])}</p>" if show_internal else ""
        workspace_row = (
            f"<p><strong>Workspace:</strong> <code id=\"workspace-root\">{escape(worker.get('workspace_dir') or '')}</code></p>"
            if show_raw_paths
            else '<p><strong>Workspace:</strong> <span id="workspace-root">Managed by GlassHive</span></p>'
        )
        diagnostic_rows = (
            f"""
                <p><strong>Gateway:</strong> {escape(worker.get('gateway_url') or '')}</p>
                <p><strong>Session Key:</strong> <code>{escape(worker.get('session_key') or '')}</code></p>
                {workspace_row}
            """
            if show_internal
            else workspace_row
        )
        stdout_console = escape(console["stdout"] or "No stdout yet.") if show_internal else "Diagnostics hidden in enterprise member view."
        stderr_console = escape(console["stderr"] or "No stderr yet.") if show_internal else "Diagnostics hidden in enterprise member view."
        workstation_tools = ""
        tools_label = "Host Computer Tools" if is_host_worker else "Workstation Tools"
        openclaw_action_button = (
            '<button onclick="desktopAction(\'openclaw\')">Open OpenClaw</button>'
            if str(worker.get("profile") or "").startswith("openclaw")
            else ""
        )
        workstation_tools = f"""
                <h3>{tools_label}</h3>
                <div class="actions">
                  <button onclick="desktopAction('terminal')">Open Shell</button>
                  <button onclick="desktopAction('files')">Open Files</button>
                  <button onclick="desktopAction('browser')">Open Browser</button>
                  <button onclick="desktopAction('codex')">Open Codex</button>
                  <button onclick="desktopAction('claude')">Open Claude</button>
                  {openclaw_action_button}
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
              const diagnosticsEnabled = {str(bool(show_raw_paths)).lower()};
              function withSignedQuery(url) {{
                if (!signedQuery) return url;
                return `${{url}}${{url.includes('?') ? '&' : '?'}}${{signedQuery}}`;
              }}

              async function refreshLive() {{
                try {{
                  const liveUrl = diagnosticsEnabled ? `/v1/workers/{escape(worker_id)}/live?diagnostics=1` : `/v1/workers/{escape(worker_id)}/live`;
                  const res = await fetch(withSignedQuery(liveUrl));
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
        signed_query_suffix = ""
        signed_query_json = json.dumps("")
        openclaw_action_button = (
            '<button onclick="desktopAction(\'openclaw\')">OpenClaw</button>'
            if str(worker.get("profile") or "").startswith("openclaw")
            else ""
        )
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
                {openclaw_action_button}
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
        signed_query_suffix = ""
        signed_query_json = json.dumps("")
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
            # A read-only worker_view token is never terminal authority. The
            # interactive shell requires normal authenticated operator access.
            _ = signed_worker
            token = _service_token_from_headers(websocket.headers)
            auth_header = websocket.headers.get("authorization", "")
            bearer = auth_header.removeprefix("Bearer ").strip() if auth_header.startswith("Bearer ") else ""
            if not (_token_matches(token, api_token) or _token_matches(bearer, api_token)):
                await websocket.close(code=4401)
                return
            try:
                ctx = _service_auth_context_from_headers(websocket.headers)
            except GlassHiveAuthError:
                await websocket.close(code=4401)
                return
        worker = store.get_worker(
            worker_id,
            tenant_id=ctx.tenant_id if ctx.is_user_scoped else None,
            owner_id=ctx.owner_id if ctx.is_user_scoped else None,
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
