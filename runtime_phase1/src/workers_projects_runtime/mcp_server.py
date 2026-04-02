from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP


DEFAULT_BASE_URL = os.environ.get("WPR_MCP_BASE_URL", "http://127.0.0.1:8766").rstrip("/")
DEFAULT_HOST = os.environ.get("WPR_MCP_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("WPR_MCP_PORT", "8767"))
DEFAULT_TIMEOUT_SEC = float(os.environ.get("WPR_MCP_TIMEOUT_SEC", "120"))
DEFAULT_OWNER_ID = os.environ.get("WPR_DEFAULT_OWNER_ID", "").strip()
DEFAULT_API_TOKEN = os.environ.get("WPR_API_TOKEN", "").strip()


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
    server = FastMCP(
        name="glass-hive",
        instructions=(
            "Use this server to manage persistent projects, resumable workers, and workstation sandboxes in Glass Hive. "
            "Read status before mutating when possible. Use worker_takeover or worker_desktop_action before risky browser "
            "or desktop actions so a human can intervene."
        ),
        host=host,
        port=port,
        streamable_http_path="/mcp",
    )

    @server.tool(
        name="projects_list",
        title="List Projects",
        description="List current projects from the standalone Workers & Projects runtime. Optionally filter by owner_id.",
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
            owner_id=owner_id,
            title=title,
            goal=goal,
            default_worker_profile=default_worker_profile,
        )

    @server.tool(name="project_get", title="Get Project", description="Fetch a single project by project_id.", structured_output=True)
    def project_get(project_id: str) -> dict[str, Any]:
        return client.get_project(project_id)

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
            "bootstrap_bundle_json to seed auth, MCP config, instructions, env, and project files."
        ),
        structured_output=True,
    )
    def worker_create(
        project_id: str,
        name: str,
        role: str,
        owner_id: str | None = None,
        profile: str = "openclaw-general",
        backend: str = "openclaw",
        bootstrap_profile: str | None = None,
        bootstrap_bundle_json: str | None = None,
    ) -> dict[str, Any]:
        parsed_bundle: dict[str, Any] | None = None
        if bootstrap_bundle_json:
            parsed_bundle = json.loads(bootstrap_bundle_json)
        return client.create_worker(
            project_id=project_id,
            owner_id=owner_id,
            name=name,
            role=role,
            profile=profile,
            backend=backend,
            bootstrap_profile=bootstrap_profile,
            bootstrap_bundle=parsed_bundle,
        )

    @server.tool(name="worker_get", title="Get Worker", description="Fetch a worker by worker_id.", structured_output=True)
    def worker_get(worker_id: str) -> dict[str, Any]:
        return client.get_worker(worker_id)

    @server.tool(name="worker_live", title="Worker Live State", description="Fetch the rich live state for a worker, including runtime details, runs, logs, and artifacts.", structured_output=True)
    def worker_live(worker_id: str) -> dict[str, Any]:
        return client.worker_live(worker_id)

    @server.tool(name="worker_run", title="Queue Worker Run", description="Queue a new instruction for a worker.", structured_output=True)
    def worker_run(worker_id: str, instruction: str) -> dict[str, Any]:
        return client.assign_run(worker_id, instruction)

    @server.tool(name="worker_message", title="Send Worker Message", description="Send an operator message into the current worker session.", structured_output=True)
    def worker_message(worker_id: str, message: str) -> dict[str, Any]:
        return client.send_message(worker_id, message)

    @server.tool(name="worker_pause", title="Pause Worker", description="Pause a worker and freeze its sandbox.", structured_output=True)
    def worker_pause(worker_id: str) -> dict[str, Any]:
        return client.lifecycle(worker_id, "pause")

    @server.tool(name="worker_resume", title="Resume Worker", description="Resume a paused worker from its persistent sandbox.", structured_output=True)
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
        description="Launch a workstation surface such as terminal, files, browser, codex, claude, or openclaw inside a worker sandbox.",
        structured_output=True,
    )
    def worker_desktop_action(worker_id: str, action: str, url: str | None = None) -> dict[str, Any]:
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
