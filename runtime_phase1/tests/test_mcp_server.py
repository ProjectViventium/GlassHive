from __future__ import annotations

import asyncio
import json

from fastmcp import Client

from workers_projects_runtime.mcp_server import create_mcp_server


class FakeApiClient:
    def list_projects(self, owner_id: str | None = None):
        items = [
            {"project_id": "prj_123", "owner_id": "demo-owner", "title": "Inbox Zero", "goal": "Triage open loops"},
            {"project_id": "prj_999", "owner_id": "other", "title": "Other", "goal": "Ignore me"},
        ]
        if owner_id:
            return [item for item in items if item["owner_id"] == owner_id]
        return items

    def create_project(self, *, owner_id: str | None, title: str, goal: str, default_worker_profile: str = "openclaw-general"):
        return {
            "project_id": "prj_new",
            "owner_id": owner_id,
            "title": title,
            "goal": goal,
            "default_worker_profile": default_worker_profile,
        }

    def get_project(self, project_id: str):
        return {"project_id": project_id, "owner_id": "demo-owner", "title": "Inbox Zero", "goal": "Triage open loops"}

    def list_project_runs(self, project_id: str):
        return [{"run_id": "run_123", "project_id": project_id, "state": "completed"}]

    def list_project_events(self, project_id: str):
        return [{"event_id": "evt_123", "project_id": project_id, "event_type": "run.completed"}]

    def list_workers(self, project_id: str):
        return [{"worker_id": "wrk_123", "project_id": project_id, "profile": "openclaw-general", "state": "ready"}]

    def create_worker(self, *, project_id: str, owner_id: str | None, name: str, role: str, profile: str = "openclaw-general", backend: str = "openclaw"):
        return {
            "worker_id": "wrk_new",
            "project_id": project_id,
            "owner_id": owner_id,
            "name": name,
            "role": role,
            "profile": profile,
            "backend": backend,
            "state": "ready",
        }

    def get_worker(self, worker_id: str):
        return {"worker_id": worker_id, "project_id": "prj_123", "profile": "openclaw-general", "state": "ready"}

    def worker_live(self, worker_id: str):
        return {
            "worker": {"worker_id": worker_id, "state": "ready"},
            "runtime_details": {"view_url": "http://127.0.0.1:62310/?autoconnect=1"},
            "project_runs": [{"run_id": "run_123"}],
        }

    def worker_runs(self, worker_id: str):
        return [{"run_id": "run_123", "worker_id": worker_id, "state": "completed"}]

    def worker_events(self, worker_id: str):
        return [{"event_id": "evt_123", "worker_id": worker_id, "event_type": "worker.ready"}]

    def assign_run(self, worker_id: str, instruction: str):
        return {"run_id": "run_assign", "worker_id": worker_id, "instruction": instruction, "state": "queued"}

    def send_message(self, worker_id: str, message: str):
        return {"run_id": "run_msg", "worker_id": worker_id, "instruction": message, "state": "queued"}

    def lifecycle(self, worker_id: str, action: str):
        return {"worker_id": worker_id, "state": "ready", "action": action}

    def desktop_action(self, worker_id: str, action: str, url: str | None = None):
        return {"worker_id": worker_id, "action": action, "url": url, "view_url": "http://127.0.0.1:62310/?autoconnect=1"}

    def takeover(self, worker_id: str):
        return {"supported": True, "url": f"http://127.0.0.1:8766/ui/workers/{worker_id}/view", "mode": "workstation-desktop"}

    def get_run(self, run_id: str):
        return {"run_id": run_id, "state": "completed", "output_text": "OK"}

    def metrics(self):
        return {"projects": 1, "workers": 1, "runs": 2, "queued_runs": 0, "active_runs": 0, "events": 3}


def _tool_json(result) -> object:
    if result.structured_content is not None:
        return result.structured_content
    assert result.content, "Expected text content from MCP tool call"
    return json.loads(result.content[0].text)


def test_mcp_server_exposes_tools_and_resources():
    server = create_mcp_server(api_client=FakeApiClient())

    async def scenario():
        async with Client(server) as client:
            tools = await client.list_tools()
            tool_names = {tool.name for tool in tools}
            assert "project_create" in tool_names
            assert "worker_takeover" in tool_names

            created = await client.call_tool(
                "project_create",
                {"owner_id": "demo-owner", "title": "Inbox Zero", "goal": "Triage open loops"},
            )
            created_payload = _tool_json(created)
            assert created_payload["project_id"] == "prj_new"

            takeover = await client.call_tool("worker_takeover", {"worker_id": "wrk_123"})
            takeover_payload = _tool_json(takeover)
            assert takeover_payload["takeover"]["supported"] is True
            assert takeover_payload["view_url"].startswith("http://127.0.0.1:")

            live_resource = await client.read_resource("wpr://workers/wrk_123/live")
            assert live_resource[0].text is not None
            live_payload = json.loads(live_resource[0].text)
            assert live_payload["worker"]["worker_id"] == "wrk_123"

    asyncio.run(scenario())
