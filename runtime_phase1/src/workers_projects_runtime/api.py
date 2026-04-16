from __future__ import annotations

import os
import re
from contextlib import asynccontextmanager
from html import escape
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, Response

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
    SendMessageRequest,
    TakeoverInfo,
    WorkerResponse,
)
from .openclaw_runtime import StubRuntime, WorkerRuntime
from .profile_runtime import ProfiledWorkerRuntime
from .service import WorkersProjectsService
from .store import Store
from .terminal_takeover import TerminalTarget, bridge_terminal

DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "runtime_phase1.db"
SANDBOX_WORKSPACE_MOUNT = Path(os.environ.get("WPR_SANDBOX_WORKSPACE", "/workspace/project"))


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
    api_token = os.environ.get("WPR_API_TOKEN", "").strip()
    unauthenticated_prefixes = (
        "/health",
        "/docs",
        "/openapi.json",
        "/redoc",
        "/ui",
        "/favicon.ico",
    )

    @app.middleware("http")
    async def optional_bearer_auth(request: Request, call_next):
        if not api_token:
            return await call_next(request)
        if request.url.path.startswith(unauthenticated_prefixes):
            return await call_next(request)
        auth_header = request.headers.get("authorization", "")
        token = request.headers.get("x-wpr-token", "")
        bearer = auth_header.removeprefix("Bearer ").strip() if auth_header.startswith("Bearer ") else ""
        if token != api_token and bearer != api_token:
            return Response(status_code=401, content="Unauthorized")
        return await call_next(request)

    def require_project(project_id: str) -> dict:
        try:
            return service.require_project(project_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    def require_worker(worker_id: str) -> dict:
        try:
            service.heal_worker(worker_id)
            return service.require_worker(worker_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    def require_run(run_id: str) -> dict:
        try:
            return service.require_run(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    def absolute_ui_url(request: Request, worker_id: str) -> str:
        return f"{str(request.base_url).rstrip('/')}/ui/workers/{worker_id}"

    def absolute_view_url(request: Request, worker_id: str) -> str:
        return f"{str(request.base_url).rstrip('/')}/ui/workers/{worker_id}/view"

    def absolute_terminal_url(request: Request, worker_id: str) -> str:
        return f"{str(request.base_url).rstrip('/')}/ui/workers/{worker_id}/terminal"

    def _runtime_details(worker: dict) -> dict[str, object]:
        if hasattr(runtime_impl, "describe_worker"):
            return runtime_impl.describe_worker(worker)
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
            return handle.read().decode("utf-8", errors="replace")

    def _log_paths(worker: dict) -> tuple[Path, Path]:
        runtime_name = str(worker.get("runtime") or "")
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

    def _candidate_html_paths(worker: dict, max_entries: int = 20) -> list[Path]:
        raw_root = str(worker.get("workspace_dir") or "").strip()
        if not raw_root:
            return []
        root = Path(raw_root)
        if not root.exists():
            return []
        candidates: list[Path] = []
        for path in sorted(root.rglob("*.html")):
            try:
                rel = path.relative_to(root)
            except ValueError:
                continue
            if any(part in {".git", "node_modules", ".venv"} for part in rel.parts):
                continue
            candidates.append(path)
            if len(candidates) >= max_entries:
                break
        return candidates

    def _workspace_browser_url(path: Path, worker: dict) -> str | None:
        raw_root = str(worker.get("workspace_dir") or "").strip()
        if not raw_root:
            return None
        root = Path(raw_root)
        try:
            rel = path.relative_to(root)
        except ValueError:
            return None
        container_path = (SANDBOX_WORKSPACE_MOUNT / rel.as_posix()).as_posix()
        return f"file://{quote(container_path, safe='/:')}"

    def _extract_urls(*texts: str) -> list[str]:
        combined = "\n".join(texts)
        seen: set[str] = set()
        matches: list[str] = []
        for raw in re.findall(r"https?://[^\s<>'\"`]+", combined):
            cleaned = raw.rstrip(").,;")
            if cleaned in seen:
                continue
            seen.add(cleaned)
            matches.append(cleaned)
        return matches

    def _deliverable_payload(worker: dict, latest_run: dict | None, latest_output: str, stdout_text: str, stderr_text: str) -> dict[str, object] | None:
        html_candidates = _candidate_html_paths(worker)
        preferred_html = next((path for path in html_candidates if path.name.lower() == "index.html"), None)
        if preferred_html is None and html_candidates:
            preferred_html = html_candidates[0]

        urls = _extract_urls(latest_output, stdout_text, stderr_text)
        external_url = next(
            (
                url
                for url in urls
                if not re.search(r"://(?:127\.0\.0\.1|localhost|0\.0\.0\.0)(?:[:/]|$)", url, flags=re.IGNORECASE)
            ),
            None,
        )
        local_url = next(
            (
                url
                for url in urls
                if re.search(r"://(?:127\.0\.0\.1|localhost|0\.0\.0\.0)(?:[:/]|$)", url, flags=re.IGNORECASE)
            ),
            None,
        )

        if preferred_html is not None:
            browser_url = _workspace_browser_url(preferred_html, worker)
            if browser_url:
                return {
                    "kind": "webpage",
                    "state": "ready" if latest_run else "available",
                    "source": "workspace_html",
                    "label": preferred_html.name,
                    "browser_url": browser_url,
                    "preferred_surface": "desktop",
                    "workspace_path": preferred_html.name,
                }

        if local_url or external_url:
            browser_url = local_url or external_url
            return {
                "kind": "webpage",
                "state": "ready" if latest_run else "available",
                "source": "run_url",
                "label": browser_url,
                "browser_url": browser_url,
                "preferred_surface": "desktop",
                "workspace_path": None,
            }

        return None

    def _sanitize_worker(worker: dict) -> dict[str, object]:
        safe = dict(worker)
        safe.pop("gateway_token", None)
        safe.pop("bootstrap_bundle_json", None)
        return safe

    def _live_payload(worker_id: str) -> dict[str, object]:
        worker = require_worker(worker_id)
        runs = store.list_runs_for_worker(worker_id, limit=10)
        project_runs = store.list_runs_for_project(worker["project_id"], limit=12)
        events = store.list_events(worker_id)[-25:]
        latest_run = runs[0] if runs else None
        stdout_path, stderr_path = _log_paths(worker)
        stdout_text = _read_tail(stdout_path)
        stderr_text = _read_tail(stderr_path)
        latest_output = ""
        if latest_run:
            latest_output = str(latest_run.get("output_text") or latest_run.get("error_text") or "")
        latest_image = _latest_image_path(worker)
        deliverable = _deliverable_payload(worker, latest_run, latest_output, stdout_text, stderr_text)
        return {
            "worker": _sanitize_worker(worker),
            "latest_run": latest_run,
            "latest_output": latest_output,
            "runs": runs,
            "project_runs": project_runs,
            "events": events,
            "runtime_details": _runtime_details(worker),
            "console": {
                "stdout": stdout_text,
                "stderr": stderr_text,
            },
            "workspace": {
                "root": worker.get("workspace_dir") or "",
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
        metrics = store.metrics()
        return {
            "status": "ok",
            "version": app.version,
            "runtime_backend": resolved_runtime_backend,
            "metrics": metrics,
        }

    @app.get("/favicon.ico")
    def favicon() -> Response:
        return Response(status_code=204)

    @app.post("/v1/projects", response_model=ProjectResponse, status_code=201)
    def create_project(payload: CreateProjectRequest) -> ProjectResponse:
        return ProjectResponse(**service.create_project(payload.owner_id, payload.title, payload.goal, payload.default_worker_profile))

    @app.get("/v1/projects")
    def list_projects() -> dict[str, list[ProjectResponse]]:
        return {"items": [ProjectResponse(**item) for item in store.list_projects()]}

    @app.get("/v1/projects/{project_id}", response_model=ProjectResponse)
    def get_project(project_id: str) -> ProjectResponse:
        project = require_project(project_id)
        return ProjectResponse(**project)

    @app.get("/v1/projects/{project_id}/events")
    def list_project_events(project_id: str) -> dict[str, list[EventResponse]]:
        require_project(project_id)
        return {"items": [EventResponse(**item) for item in store.list_project_events(project_id)]}

    @app.get("/v1/projects/{project_id}/runs")
    def list_project_runs(project_id: str) -> dict[str, list[RunResponse]]:
        require_project(project_id)
        return {"items": [RunResponse(**item) for item in store.list_runs_for_project(project_id)]}

    @app.post("/v1/projects/{project_id}/workers", response_model=WorkerResponse, status_code=201)
    def create_worker(project_id: str, payload: CreateWorkerRequest) -> WorkerResponse:
        require_project(project_id)
        worker = service.create_worker(
            project_id,
            payload.owner_id,
            payload.name,
            payload.role,
            payload.profile,
            payload.backend,
            payload.bootstrap_profile,
            payload.bootstrap_bundle,
        )
        return WorkerResponse(**worker)

    @app.post("/v1/projects/{project_id}/workers/duplicate", response_model=WorkerResponse, status_code=201)
    def duplicate_worker(project_id: str, payload: DuplicateWorkerRequest) -> WorkerResponse:
        require_project(project_id)
        require_worker(payload.source_worker_id)
        worker = service.duplicate_worker(
            payload.source_worker_id,
            project_id,
            payload.owner_id,
            payload.name,
            payload.role,
        )
        return WorkerResponse(**worker)

    @app.get("/v1/projects/{project_id}/workers")
    def list_workers(project_id: str) -> dict[str, list[WorkerResponse]]:
        require_project(project_id)
        return {"items": [WorkerResponse(**item) for item in store.list_workers(project_id)]}

    @app.get("/v1/workers/{worker_id}", response_model=WorkerResponse)
    def get_worker(worker_id: str) -> WorkerResponse:
        worker = require_worker(worker_id)
        return WorkerResponse(**worker)

    @app.get("/v1/workers/{worker_id}/live")
    def worker_live(worker_id: str) -> dict[str, object]:
        return _live_payload(worker_id)

    @app.get("/v1/workers/{worker_id}/runs")
    def list_worker_runs(worker_id: str) -> dict[str, list[RunResponse]]:
        require_worker(worker_id)
        return {"items": [RunResponse(**item) for item in store.list_runs_for_worker(worker_id)]}

    @app.get("/v1/workers/{worker_id}/events")
    def list_worker_events(worker_id: str) -> dict[str, list[EventResponse]]:
        require_worker(worker_id)
        return {"items": [EventResponse(**item) for item in store.list_events(worker_id)]}

    @app.get("/v1/runs/{run_id}", response_model=RunResponse)
    def get_run(run_id: str) -> RunResponse:
        run = require_run(run_id)
        return RunResponse(**run)

    @app.post("/v1/workers/{worker_id}/assign", response_model=RunResponse, status_code=202)
    def assign(worker_id: str, payload: AssignRunRequest) -> RunResponse:
        require_worker(worker_id)
        run = service.assign_run(worker_id, payload.instruction)
        return RunResponse(**run)

    @app.post("/v1/workers/{worker_id}/message", response_model=RunResponse, status_code=202)
    def send_message(worker_id: str, payload: SendMessageRequest) -> RunResponse:
        require_worker(worker_id)
        run = service.send_message(worker_id, payload.message)
        return RunResponse(**run)

    @app.post("/v1/workers/{worker_id}/launch-failed", response_model=WorkerResponse, status_code=202)
    def launch_failed(worker_id: str, payload: LaunchFailureRequest) -> WorkerResponse:
        require_worker(worker_id)
        return WorkerResponse(**service.record_launch_failed(worker_id, payload.reason))

    @app.post("/v1/workers/{worker_id}/interrupt", response_model=WorkerResponse, status_code=202)
    def interrupt(worker_id: str) -> WorkerResponse:
        require_worker(worker_id)
        return WorkerResponse(**service.interrupt_worker(worker_id))

    @app.post("/v1/workers/{worker_id}/pause", response_model=WorkerResponse, status_code=202)
    def pause(worker_id: str) -> WorkerResponse:
        require_worker(worker_id)
        return WorkerResponse(**service.pause_worker(worker_id))

    @app.post("/v1/workers/{worker_id}/resume", response_model=WorkerResponse, status_code=202)
    def resume(worker_id: str) -> WorkerResponse:
        require_worker(worker_id)
        return WorkerResponse(**service.resume_worker(worker_id))

    @app.post("/v1/workers/{worker_id}/terminate", response_model=WorkerResponse, status_code=202)
    def terminate(worker_id: str) -> WorkerResponse:
        require_worker(worker_id)
        return WorkerResponse(**service.terminate_worker(worker_id))

    @app.post("/v1/workers/{worker_id}/desktop-action", response_model=DesktopActionResponse, status_code=202)
    def desktop_action(worker_id: str, payload: DesktopActionRequest, request: Request) -> DesktopActionResponse:
        worker = require_worker(worker_id)
        try:
            launched = service.desktop_action(worker_id, payload.action, url=payload.url, run_id=payload.run_id)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        runtime_details = _runtime_details(require_worker(worker_id))
        resolved_url = str(launched.get("url") or runtime_details.get("view_url") or absolute_view_url(request, worker_id))
        notes = str(launched.get("notes") or "")
        return DesktopActionResponse(
            action=str(launched.get("action") or payload.action),
            status=str(launched.get("status") or "launched"),
            mode=str(launched.get("mode") or runtime_details.get("mode") or "workstation-desktop"),
            url=resolved_url,
            view_url=str(runtime_details.get("view_url") or resolved_url),
            notes=notes or None,
        )

    @app.get("/v1/workers/{worker_id}/takeover", response_model=TakeoverInfo)
    def takeover(worker_id: str, request: Request) -> TakeoverInfo:
        worker = require_worker(worker_id)
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
    def latest_worker_image(worker_id: str) -> FileResponse:
        worker = require_worker(worker_id)
        latest = _latest_image_path(worker)
        if latest is None or not latest.exists():
            raise HTTPException(status_code=404, detail="No image artifacts found for this worker")
        return FileResponse(latest)

    @app.get("/v1/metrics/summary", response_model=MetricsSummary)
    def metrics() -> MetricsSummary:
        return MetricsSummary(**store.metrics())

    @app.post("/v1/admin/reconcile")
    def reconcile() -> dict[str, object]:
        service.reconcile_all_workers()
        return {
            "status": "ok",
            "workers": len(store.list_all_workers()),
            "message": "Worker runtime metadata reconciled",
        }

    @app.get("/ui", response_class=HTMLResponse)
    def ui_home(request: Request) -> str:
        projects = store.list_projects()
        project_items = []
        for project in projects:
            workers = store.list_workers(project["project_id"])
            active_workers = [worker for worker in workers if worker["state"] != "terminated"]
            open_target = f"/ui/projects/{escape(project['project_id'])}"
            project_items.append(
                "<section>"
                f"<h2>{escape(project['title'])}</h2>"
                f"<p><strong>Goal:</strong> {escape(project['goal'])}</p>"
                f"<p><strong>Project ID:</strong> {escape(project['project_id'])}</p>"
                f"<p><strong>Status:</strong> {escape(project['status'])}</p>"
                f"<p><strong>Workers:</strong> {len(workers)} total, {len(active_workers)} active</p>"
                f"<p><a href='{open_target}'>Open project workspace</a></p>"
                "</section>"
            )
        body = "".join(project_items) or "<p>No projects yet.</p>"
        return (
            "<html><head><title>Workers Projects Runtime</title>"
            "<style>body{font-family:system-ui,sans-serif;margin:2rem;max-width:1100px;}"
            "section{border:1px solid #ddd;padding:1rem;border-radius:12px;margin-bottom:1rem;}"
            "code,pre{background:#f6f6f6;padding:.2rem .4rem;border-radius:6px;}"
            "input,textarea,button{font:inherit;padding:.55rem;}"
            "</style></head><body>"
            "<h1>Workers Projects Runtime</h1>"
            "<p>Phase 1 standalone OpenClaw-backed control plane.</p>"
            f"<p><a href='{escape(str(request.base_url).rstrip('/'))}/docs'>OpenAPI docs</a></p>"
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
        project = require_project(project_id)
        workers = store.list_workers(project_id)
        selected_worker = None
        if worker_id:
            selected_worker = next((worker for worker in workers if worker["worker_id"] == worker_id), None)
        if selected_worker is None:
            selected_worker = next((worker for worker in workers if worker["state"] != "terminated"), None)

        selected_runs = store.list_runs_for_worker(selected_worker["worker_id"], limit=10) if selected_worker else []
        project_runs = store.list_runs_for_project(project_id, limit=12)
        selected_events = store.list_events(selected_worker["worker_id"]) if selected_worker else []
        selected_runtime_details = _runtime_details(selected_worker) if selected_worker else {}
        selected_view_url = str(selected_runtime_details.get("view_url") or "").strip()
        selected_latest_image_url = (
            f"/v1/workers/{selected_worker['worker_id']}/artifacts/latest-image" if selected_worker and _latest_image_path(selected_worker) else ""
        )
        latest_run = selected_runs[0] if selected_runs else None
        latest_run_marker = escape(str((latest_run or {}).get("ended_at") or (latest_run or {}).get("started_at") or ""), quote=True)
        selected_worker_id = selected_worker["worker_id"] if selected_worker else ""
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
            if selected_view_url:
                workstation_tools_card = f"""
                <div class="card">
                  <h2>Workstation Tools</h2>
                  <p class="muted">These launch real surfaces inside the same persistent worker sandbox.</p>
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
            selected_worker_panel = f"""
            <div class="card">
              <h2>Selected Worker</h2>
              <p><strong>Name:</strong> {escape(selected_worker['name'])}</p>
              <p><strong>State:</strong> <span id="selected-worker-state">{escape(selected_worker['state'])}</span></p>
              <p><strong>Runtime:</strong> <span id="selected-worker-runtime">{escape(selected_worker.get('runtime') or '')}</span></p>
              <p><strong>Profile:</strong> <span id="selected-worker-profile">{escape(selected_worker['profile'])}</span></p>
              <p><strong>Model:</strong> <span id="selected-worker-model">{escape(selected_worker.get('model') or '')}</span></p>
              <p><strong>Gateway:</strong> {escape(selected_worker.get('gateway_url') or '')}</p>
              <p><a href="/ui/workers/{escape(selected_worker['worker_id'])}">Open worker console</a> · <a href="/ui/workers/{escape(selected_worker['worker_id'])}/view">Open takeover page</a> · <a href="/ui/workers/{escape(selected_worker['worker_id'])}/terminal">Take over terminal</a></p>
              <div class="actions">
                <button onclick="workerAction('resume')">Resume</button>
                <button onclick="workerAction('pause')">Pause</button>
                <button onclick="workerAction('interrupt')">Interrupt</button>
                <button onclick="workerAction('terminate')">Terminate</button>
                <button onclick="window.location.reload()">Refresh</button>
              </div>
              <h3>Message Worker</h3>
              <textarea id="message" placeholder="Send a short operator message into the active worker session."></textarea>
              <p><button onclick="sendMessage()">Send message</button></p>
            </div>
            <div class="card">
              <h2>Latest Output</h2>
              <pre id="latest-output">{latest_output or 'No completed output yet.'}</pre>
            </div>
            {workstation_tools_card}
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
            <p><a href="/ui">Back to projects</a> · <a href="{escape(str(request.base_url).rstrip('/'))}/docs">API docs</a></p>
            <p><strong>Goal:</strong> {escape(project['goal'])}</p>
            <p class="muted">Simple flow: choose a worker, write the prompt, run it, then watch and control it.</p>

            <div class="grid">
              <div>
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
    def ui_worker(worker_id: str) -> str:
        worker = require_worker(worker_id)
        project = require_project(worker["project_id"])
        runs = store.list_runs_for_worker(worker_id, limit=10)
        events = store.list_events(worker_id)
        latest_run = runs[0] if runs else None
        live = _live_payload(worker_id)
        console = live["console"]
        workspace = live["workspace"]
        runtime_details = live["runtime_details"]
        artifacts = live["artifacts"]
        latest_run_marker = escape(str((latest_run or {}).get("ended_at") or (latest_run or {}).get("started_at") or ""), quote=True)
        run_items = "".join(
            "<li>"
            f"<strong>{escape(run['run_id'])}</strong> - {escape(run['state'])}<br/>"
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
        detail_items = "".join(
            f"<li><strong>{escape(str(key).replace('_', ' ').title())}:</strong> {escape(str(value))}</li>"
            for key, value in runtime_details.items()
            if value is not None and value != "" and value != []
        ) or "<li>No runtime details yet</li>"
        workstation_tools = ""
        if runtime_details.get("view_url"):
            workstation_tools = """
                <h3>Workstation Tools</h3>
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
                <p><strong>Worker ID:</strong> {escape(worker['worker_id'])}</p>
                <p><strong>Role:</strong> {escape(worker['role'])}</p>
                <p><strong>Profile:</strong> {escape(worker['profile'])}</p>
                <p><strong>Model:</strong> {escape(worker.get('model') or '')}</p>
                <p><strong>State:</strong> <span id="worker-state">{escape(worker['state'])}</span></p>
                <p><strong>Gateway:</strong> {escape(worker.get('gateway_url') or '')}</p>
                <p><strong>Session Key:</strong> <code>{escape(worker.get('session_key') or '')}</code></p>
                <p><strong>Workspace:</strong> <code id="workspace-root">{escape(worker.get('workspace_dir') or '')}</code></p>
                <p><strong>Last Error:</strong> <span id="worker-last-error">{last_error or 'None'}</span></p>
                <p><a href="/ui/workers/{escape(worker_id)}/view">Open takeover page</a> · <a href="/ui/workers/{escape(worker_id)}/terminal">Take over terminal</a></p>
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
                <pre id="stdout-console">{escape(console['stdout'] or 'No stdout yet.')}</pre>
              </div>
              <div class="card">
                <h2>Live Stderr</h2>
                <pre id="stderr-console">{escape(console['stderr'] or 'No stderr yet.')}</pre>
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
                  `<li><strong>${{escapeHtml(run.run_id)}}</strong> - ${{escapeHtml(run.state)}}<br/><span>${{escapeHtml(run.instruction || '')}}</span><br/><pre>${{escapeHtml(((run.output_text || run.error_text || '')).slice(0, 2000))}}</pre></li>`
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
                return entries.map(([key, value]) => `<li><strong>${{escapeHtml(key.replaceAll('_', ' '))}}:</strong> ${{escapeHtml(value)}}</li>`).join('');
              }}

              async function action(name) {{
                await fetch(`/v1/workers/{escape(worker_id)}/${{name}}`, {{ method: 'POST' }});
                window.location.reload();
              }}
              async function assignRun() {{
                const instruction = document.getElementById('instruction').value.trim();
                if (!instruction) return;
                await fetch(`/v1/workers/{escape(worker_id)}/assign`, {{
                  method: 'POST',
                  headers: {{ 'Content-Type': 'application/json' }},
                  body: JSON.stringify({{ instruction }})
                }});
                window.location.reload();
              }}
              async function sendMessage() {{
                const message = document.getElementById('message').value.trim();
                if (!message) return;
                await fetch(`/v1/workers/{escape(worker_id)}/message`, {{
                  method: 'POST',
                  headers: {{ 'Content-Type': 'application/json' }},
                  body: JSON.stringify({{ message }})
                }});
                window.location.reload();
              }}

              async function desktopAction(action, url='') {{
                const res = await fetch(`/v1/workers/{escape(worker_id)}/desktop-action`, {{
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

              async function refreshLive() {{
                try {{
                  const res = await fetch(`/v1/workers/{escape(worker_id)}/live`);
                  if (!res.ok) return;
                  const live = await res.json();
                  document.getElementById('worker-state').textContent = live.worker.state || '';
                  document.getElementById('worker-last-error').textContent = live.worker.last_error || 'None';
                  document.getElementById('workspace-root').textContent = live.workspace.root || '';
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
    def ui_worker_view(worker_id: str) -> str:
        worker = require_worker(worker_id)
        project = require_project(worker["project_id"])
        runtime_details = _runtime_details(worker)
        external_view_url = str(runtime_details.get("view_url") or "").strip()
        subtitle = escape(str(runtime_details.get("mode") or worker.get("runtime") or "worker view"))
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
                  <p><a href="/ui/projects/{escape(project['project_id'])}?worker_id={escape(worker_id)}" target="_top">Back to project workspace</a> · <a href="/ui/workers/{escape(worker_id)}/terminal" target="_top">Take over terminal</a></p>
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
                <div class="meta">{subtitle} · {escape(str(runtime_details.get('container_name') or worker.get('session_key') or worker_id))}</div>
                <div class="meta"><a href="/ui/projects/{escape(project['project_id'])}?worker_id={escape(worker_id)}" target="_top">Back to project workspace</a> · <a href="/ui/workers/{escape(worker_id)}" target="_top">Worker console</a> · <a href="/ui/workers/{escape(worker_id)}/terminal" target="_top">Terminal</a> · <a href="{escape(external_view_url, quote=True)}" target="_blank" rel="noreferrer">Desktop directly</a></div>
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
              async function action(name) {{
                await fetch(`/v1/workers/${{workerId}}/${{name}}`, {{ method: 'POST' }});
              }}
              async function pauseAndOpenDirect() {{
                await action('pause');
                window.open(directDesktopUrl, '_blank', 'noopener');
              }}
              async function desktopAction(name, url='') {{
                const res = await fetch(`/v1/workers/${{workerId}}/desktop-action`, {{
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
    def ui_worker_terminal(worker_id: str) -> str:
        worker = require_worker(worker_id)
        project = require_project(worker["project_id"])
        runtime_details = _runtime_details(worker)
        subtitle = escape(str(runtime_details.get("mode") or worker.get("runtime") or "worker terminal"))
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
                <div class="meta">{subtitle} · {escape(str(runtime_details.get('container_name') or worker.get('session_key') or worker['worker_id']))}</div>
                <div class="meta"><a href="/ui/projects/{escape(project['project_id'])}?worker_id={escape(worker_id)}" target="_top">Back to project workspace</a> · <a href="/ui/workers/{escape(worker_id)}" target="_top">Worker console</a></div>
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
                await fetch(`/v1/workers/${{workerId}}/${{name}}`, {{ method: 'POST' }});
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
                socket = new WebSocket(`${{protocol}}://${{window.location.host}}/ws/workers/${{workerId}}/terminal`);
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
        worker = require_worker(worker_id)
        if worker["state"] == "terminated":
            await websocket.close(code=4404)
            return
        target = _terminal_target(worker)
        await bridge_terminal(websocket, target)

    return app


app = create_app()
