from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import Body, FastAPI, HTTPException
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

STATIC_DIR = Path(__file__).resolve().parent / "static"


class LaunchRequest(BaseModel):
    description: str = Field(min_length=1)
    success_criteria: str = Field(min_length=1)
    context: str | None = None
    workspace_option: str | None = None
    worker_option: str | None = None
    launch_surface: str | None = None


class MessageRequest(BaseModel):
    message: str = Field(min_length=1)


class ActionRequest(BaseModel):
    url: str | None = None


def flatten_workspaces(client: RuntimeClient) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for project in client.list_projects():
        project_id = str(project["project_id"])
        for worker in client.list_workers(project_id):
            if str(worker.get("state") or "").strip().lower() == "terminated":
                continue
            project_title = str(project.get("title") or project_id)
            worker_name = str(worker.get("name") or worker["worker_id"])
            items.append(
                {
                    "project_id": project_id,
                    "project_title": project_title,
                    "worker_id": worker["worker_id"],
                    "name": worker_name,
                    "workspace_label": project_title or worker_name,
                    "profile": worker.get("profile") or "",
                    "state": worker.get("state") or "",
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


def _format_launch_error(exc: Exception) -> str:
    detail = str(exc).strip()
    if detail:
        return detail
    return "The project launch failed before the first run could start."


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
    client = runtime_client or RuntimeClient()
    app = FastAPI(title="GlassHive", version="0.1.0")
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "runtime": client.health()}

    @app.get("/api/bootstrap")
    def bootstrap() -> dict[str, Any]:
        owner_id = os.environ.get("GLASSHIVE_DEFAULT_OWNER_ID", "demo-owner")
        existing_workspaces = flatten_workspaces(client)
        return {
            "owner_id": owner_id,
            "default_workspace_option": "new:codex-cli",
            "default_launch_surface": _default_launch_surface(),
            "launch_surface_options": _launch_surface_options(),
            "new_workspace_options": [
                {"value": "new:codex-cli", "label": "New workspace · Codex", "profile": "codex-cli"},
                {"value": "new:claude-code", "label": "New workspace · Claude Code", "profile": "claude-code"},
                {"value": "new:openclaw-general", "label": "New workspace · OpenClaw", "profile": "openclaw-general"},
            ],
            "existing_workspaces": existing_workspaces,
        }

    @app.post("/api/launch")
    def launch(payload: LaunchRequest) -> dict[str, Any]:
        owner_id = os.environ.get("GLASSHIVE_DEFAULT_OWNER_ID", "demo-owner")
        brief = build_operator_brief(payload.description, payload.success_criteria, payload.context)
        workspace_option = payload.workspace_option or payload.worker_option or "new:codex-cli"
        project_id: str
        worker_id: str | None = None
        profile: str
        created_new_worker = False

        try:
            if workspace_option.startswith("open:") or workspace_option.startswith("existing:"):
                worker_id = workspace_option.split(":", 1)[1]
                worker = client.get_worker(worker_id)
                project_id = str(worker["project_id"])
                profile = str(worker.get("profile") or "codex-cli")
            elif workspace_option.startswith("duplicate:"):
                source_worker_id = workspace_option.split(":", 1)[1]
                source_worker = client.get_worker(source_worker_id)
                profile = str(source_worker.get("profile") or "codex-cli")
                project = client.create_project(owner_id, build_project_title(payload.description), payload.description.strip(), profile)
                project_id = str(project["project_id"])
                worker = client.duplicate_worker(project_id, source_worker_id, owner_id)
                worker_id = str(worker["worker_id"])
                created_new_worker = True
            else:
                profile = workspace_option.split(":", 1)[1] if ":" in workspace_option else "codex-cli"
                project = client.create_project(owner_id, build_project_title(payload.description), payload.description.strip(), profile)
                project_id = str(project["project_id"])
                worker = client.create_worker(project_id, owner_id, profile)
                worker_id = str(worker["worker_id"])
                created_new_worker = True
            run = client.assign_run(str(worker_id), brief)
        except Exception as exc:
            reason = _format_launch_error(exc)
            if created_new_worker and worker_id:
                try:
                    client.launch_failed(str(worker_id), reason)
                except Exception:
                    pass
            raise HTTPException(status_code=502, detail=reason) from exc

        surface = initial_watch_surface_for_launch(
            profile,
            payload.description,
            launch_surface=payload.launch_surface or _default_launch_surface(),
        )
        try:
            _launch_desktop_surfaces(
                client,
                worker_id=str(worker_id),
                profile=profile,
                description=payload.description,
                run_id=str(run["run_id"]),
                surface=surface,
            )
        except Exception:
            pass

        return {
            "project_id": project_id,
            "worker_id": str(worker_id),
            "run_id": run["run_id"],
            "watch_url": f"/watch/{worker_id}?project_id={project_id}&surface={surface}",
        }

    @app.get("/api/worker/{worker_id}/live")
    def worker_live(worker_id: str) -> dict[str, Any]:
        payload = client.worker_live(worker_id)
        worker = payload.get("worker") or {}
        project_id = str(worker.get("project_id") or "")
        payload["project_title"] = _project_title_for_worker(client, project_id) if project_id else ""
        return payload

    @app.get("/novnc/{worker_id}/{asset_path:path}")
    def novnc_asset(worker_id: str, asset_path: str) -> Response:
        payload = client.worker_live(worker_id)
        runtime = payload.get("runtime_details") or {}
        view_url = str(runtime.get("view_url") or "").strip()
        if not view_url:
            raise HTTPException(status_code=404, detail="No live desktop is available for this worker")
        parsed = urlparse(view_url)
        if not parsed.scheme or not parsed.netloc:
            raise HTTPException(status_code=400, detail="Invalid live desktop URL")
        target = f"{parsed.scheme}://{parsed.netloc}/{asset_path.lstrip('/')}"
        with httpx.Client(timeout=30.0) as upstream:
            upstream_response = upstream.get(target)
        return Response(
            content=upstream_response.content,
            status_code=upstream_response.status_code,
            media_type=upstream_response.headers.get("content-type"),
        )

    @app.post("/api/worker/{worker_id}/message")
    def worker_message(worker_id: str, payload: MessageRequest) -> dict[str, Any]:
        return client.message(worker_id, payload.message)

    @app.post("/api/worker/{worker_id}/steer")
    def worker_steer(worker_id: str, payload: MessageRequest) -> dict[str, Any]:
        return client.steer(worker_id, payload.message)

    @app.post("/api/worker/{worker_id}/action/{action}")
    def worker_action(worker_id: str, action: str, payload: ActionRequest | None = Body(default=None)) -> dict[str, Any]:
        if action in {"pause", "resume", "interrupt", "terminate"}:
            return client.lifecycle(worker_id, action)
        if action in {"terminal", "files", "browser", "focus_browser", "codex", "claude", "openclaw"}:
            return client.desktop_action(worker_id, action, url=(payload.url if payload else None))
        raise HTTPException(status_code=400, detail=f"Unsupported action: {action}")

    @app.get("/")
    def home() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/watch/{worker_id}")
    def watch(worker_id: str) -> FileResponse:
        return FileResponse(STATIC_DIR / "watch.html")

    @app.get("/desktop/{worker_id}")
    def desktop(worker_id: str) -> FileResponse:
        return FileResponse(STATIC_DIR / "desktop.html")

    return app


app = create_app()
