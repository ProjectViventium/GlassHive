from __future__ import annotations

import asyncio
import base64
import json
import os
from fastmcp import Client
from fastmcp.exceptions import ToolError
import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from workers_projects_runtime import mcp_server, runtime_env
from workers_projects_runtime.bootstrap import sign_bootstrap_source_path
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

    def find_worker_by_alias_across_projects(
        self,
        *,
        owner_id: str | None,
        alias: str,
        execution_mode: str | None = None,
    ):
        scoped = mcp_server._request_scoped_alias(alias)
        for project in self.list_projects(owner_id):
            for worker in self.list_workers(project["project_id"]):
                if worker.get("state") == "terminated":
                    continue
                if execution_mode and worker.get("execution_mode") and worker.get("execution_mode") != execution_mode:
                    continue
                if worker.get("alias") == scoped:
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
        execution_mode: str = "docker",
        alias: str | None = None,
        workspace_root: str | None = None,
        bootstrap_profile: str | None = None,
        bootstrap_bundle: dict | None = None,
        start_synchronously: bool = True,
    ):
        return {
            "worker_id": "wrk_new",
            "project_id": project_id,
            "owner_id": owner_id,
            "name": name,
            "role": role,
            "profile": profile,
            "backend": backend,
            "execution_mode": execution_mode,
            "alias": alias,
            "workspace_root": workspace_root,
            "state": "ready",
            "bootstrap_bundle": bootstrap_bundle,
            "start_synchronously": start_synchronously,
        }

    def find_or_resume_worker(self, **kwargs):
        payload = self.create_worker(**kwargs)
        payload["worker_id"] = "wrk_resumed"
        return payload

    def get_worker(self, worker_id: str):
        return {
            "worker_id": worker_id,
            "project_id": "prj_123",
            "tenant_id": "tenant-alpha",
            "owner_id": "demo-owner",
            "profile": "openclaw-general",
            "state": "ready",
        }

    def worker_live(self, worker_id: str):
        return {
            "worker": {
                "worker_id": worker_id,
                "project_id": "prj_123",
                "tenant_id": "tenant-alpha",
                "owner_id": "demo-owner",
                "state": "ready",
            },
            "runtime_details": {"view_url": "http://127.0.0.1:62310/?autoconnect=1"},
            "project_runs": [{"run_id": "run_123"}],
        }

    def list_artifacts(self, worker_id: str):
        return {
            "items": [
                {
                    "path": "index.html",
                    "name": "index.html",
                    "size": 128,
                    "download_url": f"/v1/workers/{worker_id}/artifacts/download?path=index.html",
                }
            ]
        }

    def worker_runs(self, worker_id: str):
        return [{"run_id": "run_123", "worker_id": worker_id, "state": "completed"}]

    def worker_events(self, worker_id: str):
        return [{"event_id": "evt_123", "worker_id": worker_id, "event_type": "worker.ready"}]

    def assign_run(self, worker_id: str, instruction: str):
        return {"run_id": "run_assign", "worker_id": worker_id, "instruction": instruction, "state": "queued"}

    def send_message(self, worker_id: str, message: str):
        return {"run_id": "run_msg", "worker_id": worker_id, "instruction": message, "state": "queued"}

    def schedule_run(self, worker_id: str, instruction: str, *, run_at: str | None = None, schedule_text: str | None = None, delay_seconds: int | None = None):
        return {
            "schedule_id": "sch_123",
            "worker_id": worker_id,
            "project_id": "prj_123",
            "instruction": instruction,
            "schedule_text": schedule_text or "",
            "run_at": run_at or "2026-05-23T19:00:00+00:00",
            "state": "pending",
            "delay_seconds": delay_seconds,
        }

    def worker_schedules(self, worker_id: str, include_done: bool = False):
        return [{"schedule_id": "sch_123", "worker_id": worker_id, "state": "pending", "include_done": include_done}]

    def get_schedule(self, schedule_id: str):
        return {"schedule_id": schedule_id, "worker_id": "wrk_123", "state": "pending"}

    def lifecycle(self, worker_id: str, action: str):
        return {"worker_id": worker_id, "state": "ready", "action": action}

    def desktop_action(self, worker_id: str, action: str, url: str | None = None):
        return {"worker_id": worker_id, "action": action, "url": url, "view_url": "http://127.0.0.1:62310/?autoconnect=1"}

    def takeover(self, worker_id: str):
        return {"supported": True, "url": f"http://127.0.0.1:8766/ui/workers/{worker_id}/view", "mode": "workstation-desktop"}

    def get_run(self, run_id: str):
        return {"run_id": run_id, "worker_id": "wrk_123", "project_id": "prj_123", "state": "completed", "output_text": "OK"}

    def metrics(self):
        return {"projects": 1, "workers": 1, "runs": 2, "queued_runs": 0, "active_runs": 0, "events": 3}


class TrackingApiClient(FakeApiClient):
    def __init__(self):
        self.calls: list[str] = []
        self.find_or_resume_payloads: list[dict] = []

    def list_projects(self, owner_id: str | None = None):
        self.calls.append("list_projects")
        return super().list_projects(owner_id)

    def list_workers(self, project_id: str):
        self.calls.append("list_workers")
        return super().list_workers(project_id)

    def create_project(self, **kwargs):
        self.calls.append("create_project")
        return super().create_project(**kwargs)

    def find_or_resume_worker(self, **kwargs):
        self.calls.append("find_or_resume_worker")
        self.find_or_resume_payloads.append(kwargs)
        return super().find_or_resume_worker(**kwargs)

    def assign_run(self, worker_id: str, instruction: str):
        self.calls.append("assign_run")
        return super().assign_run(worker_id, instruction)


class PollingApiClient(FakeApiClient):
    def __init__(self, states: list[str]):
        self.states = list(states)
        self.get_run_calls = 0

    def get_run(self, run_id: str):
        self.get_run_calls += 1
        state = self.states[min(self.get_run_calls - 1, len(self.states) - 1)]
        return {
            "run_id": run_id,
            "worker_id": "wrk_poll",
            "project_id": "prj_poll",
            "state": state,
            "output_text": "Poll completed" if state == "completed" else "",
            "error_text": "",
        }

    def worker_live(self, worker_id: str):
        return {
            "worker": {
                "worker_id": worker_id,
                "project_id": "prj_poll",
                "state": "ready",
                "owner_id": "demo-owner",
            },
            "runtime_details": {"view_url": "http://127.0.0.1:62310/?autoconnect=1"},
            "project_runs": [],
        }


def _tool_json(result) -> object:
    if result.structured_content is not None:
        return result.structured_content
    assert result.content, "Expected text content from MCP tool call"
    return json.loads(result.content[0].text)


def _callback_headers() -> dict[str, str]:
    return {
        "X-Viventium-User-Id": "user_public_safe",
        "X-Viventium-Conversation-Id": "conv_public_safe",
        "X-Viventium-Parent-Message-Id": "user_msg_public_safe",
        "X-Viventium-Message-Id": "assistant_msg_public_safe",
    }


def test_server_instructions_advertise_mcp_owned_usage_contract(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    monkeypatch.setenv("WPR_HOST_MENTION_CODEX", "@codex")
    monkeypatch.setenv("WPR_HOST_MENTION_CLAUDE", "@claude")
    monkeypatch.setenv("WPR_HOST_MENTION_OPENCLAW", "@openclaw")
    server = create_mcp_server(api_client=FakeApiClient())
    instructions = server.instructions.lower()

    for phrase in [
        "persistent projects",
        "resumable workers",
        "workstation sandboxes",
        "host-native workers",
        "real browser",
        "desktop",
        "local files/projects",
        "installed clis",
        "workspace_launch",
        "description, required success_criteria, and optional context",
        "worker_delegate_once",
        "callbacks are an optional host-app delivery enhancement",
        "neutral glasshive/librechat headers",
        "do not refuse solely because your own model context lacks file contents",
        "workspace_status",
        "workspace_wait",
        "view / steer",
        "follow_up_context",
        "own voice",
        "should not expose raw worker/run/provider/queue plumbing",
        "@codex",
        "@claude",
        "@openclaw",
    ]:
        assert phrase in instructions


def test_server_instructions_reflect_disabled_host_workers(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_HOST_WORKERS_ENABLED", "false")
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    server = create_mcp_server(api_client=FakeApiClient())
    instructions = server.instructions.lower()

    assert "host-native workers are disabled" in instructions
    assert "configured default 'docker'" in instructions
    assert "do not request execution_mode='host'" in instructions


def test_enterprise_mcp_http_auth_middleware_gates_transport_requests(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("WPR_API_TOKEN", "service-token")

    async def ok(request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/mcp", ok, methods=["POST"])])
    app.add_middleware(mcp_server.EnterpriseMcpHttpAuthMiddleware)
    client = TestClient(app)
    good_headers = {
        "X-GlassHive-Service-Token": "service-token",
        "X-GlassHive-Tenant-Id": "tenant-alpha",
        "X-GlassHive-User-Id": "user-a",
    }

    assert client.post("/mcp").status_code == 401
    assert client.post("/mcp", headers={"X-WPR-Token": "wrong"}).status_code == 401
    assert client.post("/mcp", headers={"X-WPR-Token": "service-token"}).status_code == 401
    assert client.post(
        "/mcp",
        headers={
            "X-GlassHive-Service-Token": "service-token",
            "X-GlassHive-Tenant-Id": "tenant-alpha",
        },
    ).status_code == 401
    assert client.post("/mcp", headers={**good_headers, "X-GlassHive-Tenant-Id": "tenant-beta"}).status_code == 401
    assert client.post("/mcp", headers=good_headers).status_code == 200
    assert client.post("/mcp", headers={**good_headers, "Authorization": "Bearer service-token"}).status_code == 200


def test_enterprise_mcp_requires_service_authentication(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setattr(mcp_server, "DEFAULT_API_TOKEN", "service-token")

    with pytest.raises(PermissionError):
        mcp_server._require_enterprise_mcp_service_auth(
            {
                "x-viventium-tenant-id": "tenant",
                "x-viventium-user-id": "forged-user",
            }
        )

    mcp_server._require_enterprise_mcp_service_auth(
        {
            "x-wpr-token": "service-token",
            "x-viventium-tenant-id": "tenant-alpha",
            "x-viventium-user-id": "user-a",
        }
    )
    mcp_server._require_enterprise_mcp_service_auth(
        {
            "x-glasshive-service-token": "service-token",
            "x-glasshive-tenant-id": "tenant-alpha",
            "x-glasshive-user-id": "user-a",
        }
    )
    mcp_server._require_enterprise_mcp_service_auth({"authorization": "Bearer service-token"})

    with pytest.raises(PermissionError, match="tenant assertion"):
        mcp_server._require_enterprise_mcp_identity_assertion(
            {
                "x-wpr-token": "service-token",
                "x-viventium-tenant-id": "tenant-beta",
                "x-viventium-user-id": "user-a",
            }
        )
    with pytest.raises(PermissionError, match="user assertion"):
        mcp_server._require_enterprise_mcp_identity_assertion(
            {
                "x-wpr-token": "service-token",
                "x-viventium-tenant-id": "tenant-alpha",
            }
        )
    mcp_server._require_enterprise_mcp_identity_assertion(
        {
            "x-wpr-token": "service-token",
            "x-viventium-tenant-id": "tenant-alpha",
            "x-viventium-user-id": "user-a",
        }
    )


def test_enterprise_owner_and_alias_accept_generic_glasshive_identity_headers(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setattr(mcp_server, "DEFAULT_API_TOKEN", "service-token")
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-GlassHive-Service-Token": "service-token",
            "X-GlassHive-Tenant-Id": "tenant-alpha",
            "X-GlassHive-User-Id": "user-a",
        },
    )

    assert mcp_server._request_owner_id("spoofed-owner") == "user-a"
    assert mcp_server._request_scoped_alias("Shared Workspace") == "tenant-alpha--user-a--shared-workspace"


def test_enterprise_worker_delegate_once_rejects_before_preflight_without_service_auth(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setattr(mcp_server, "DEFAULT_API_TOKEN", "service-token")
    server = create_mcp_server(api_client=FakeApiClient())

    async def scenario():
        async with Client(server) as client:
            with pytest.raises(ToolError):
                await client.call_tool(
                    "worker_delegate_once",
                    {
                        "title": "No auth",
                        "instruction": "This should not reach callback preflight or create work.",
                        "profile": "codex-cli",
                    },
                )

    asyncio.run(scenario())


def test_mcp_transport_security_allows_configured_public_host(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_MCP_URL", "http://glasshive.localtest.me:8877/mcp")
    server = create_mcp_server(host="127.0.0.1", port=8877, api_client=FakeApiClient())

    settings = server.settings.transport_security
    assert settings is not None
    assert settings.enable_dns_rebinding_protection is True
    assert "127.0.0.1:*" in settings.allowed_hosts
    assert "glasshive.localtest.me:8877" in settings.allowed_hosts
    assert "glasshive.localtest.me:*" in settings.allowed_hosts


def test_mcp_transport_security_keeps_unknown_hosts_closed(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_MCP_URL", "http://glasshive.localtest.me:8877/mcp")
    server = create_mcp_server(host="127.0.0.1", port=8877, api_client=FakeApiClient())

    settings = server.settings.transport_security
    assert settings is not None
    assert "evil.example.com:8877" not in settings.allowed_hosts


def test_mcp_server_exposes_tools_and_resources(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "http://127.0.0.1:8780")
    server = create_mcp_server(api_client=FakeApiClient())

    async def scenario():
        async with Client(server) as client:
            tools = await client.list_tools()
            tool_names = {tool.name for tool in tools}
            assert "project_create" in tool_names
            assert "workspace_launch" in tool_names
            assert "workspace_schedule" in tool_names
            assert "worker_delegate_once" in tool_names
            assert "worker_schedule" in tool_names
            assert "worker_schedules" in tool_names
            assert "worker_takeover" in tool_names
            assert "workspace_artifacts" in tool_names
            assert "workspace_artifact_download" in tool_names
            assert "worker_find_or_resume" in tool_names

            created = await client.call_tool(
                "project_create",
                {"owner_id": "demo-owner", "title": "Inbox Zero", "goal": "Triage open loops"},
            )
            created_payload = _tool_json(created)
            assert created_payload["project_id"] == "prj_new"

            worker = await client.call_tool(
                "worker_find_or_resume",
                {
                    "project_id": "prj_new",
                    "owner_id": "demo-owner",
                    "name": "Codex Host",
                    "role": "coding",
                    "alias": "codex-main",
                    "profile": "codex-cli",
                    "execution_mode": "host",
                },
            )
            worker_payload = _tool_json(worker)
            assert worker_payload["worker_id"] == "wrk_resumed"
            assert worker_payload["execution_mode"] == "host"

            takeover = await client.call_tool("worker_takeover", {"worker_id": "wrk_123"})
            takeover_payload = _tool_json(takeover)
            assert takeover_payload["takeover"]["supported"] is True
            assert takeover_payload["operator_url"] == (
                "http://127.0.0.1:8780/watch/wrk_123?surface=desktop&project_id=prj_123"
            )
            assert takeover_payload["watch_url"] == takeover_payload["operator_url"]
            assert takeover_payload["view_url"] == takeover_payload["operator_url"]
            assert takeover_payload["direct_desktop_url"].startswith("http://127.0.0.1:")
            assert takeover_payload["runtime_takeover_url"].endswith("/ui/workers/wrk_123/view")

            live_resource = await client.read_resource("wpr://workers/wrk_123/live")
            assert live_resource[0].text is not None
            live_payload = json.loads(live_resource[0].text)
            assert live_payload["worker"]["worker_id"] == "wrk_123"

    asyncio.run(scenario())

def test_workspace_artifacts_returns_signed_download_links(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ARTIFACT_BASE_URL", "https://glasshive.example.test")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "public-safe-signed-link-secret")
    server = create_mcp_server(api_client=FakeApiClient())

    async def scenario():
        async with Client(server) as client:
            listed = await client.call_tool("workspace_artifacts", {"worker_id": "wrk_123"})
            payload = _tool_json(listed)
            assert payload["status"] == "ok"
            assert payload["items"][0]["path"] == "index.html"
            assert payload["items"][0]["signed_download_url"].startswith(
                "https://glasshive.example.test/v1/signed-links/"
            )
            assert "127.0.0.1" not in payload["items"][0]["signed_download_url"]

            download = await client.call_tool(
                "workspace_artifact_download",
                {"worker_id": "wrk_123", "path": "index.html"},
            )
            download_payload = _tool_json(download)
            assert download_payload["status"] == "ok"
            assert download_payload["signed_download_url"].startswith(
                "https://glasshive.example.test/v1/signed-links/"
            )
            assert download_payload["path"] == "index.html"

    asyncio.run(scenario())


def test_workspace_artifact_download_rejects_traversal_before_signing(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ARTIFACT_BASE_URL", "https://glasshive.example.test")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "public-safe-signed-link-secret")
    server = create_mcp_server(api_client=FakeApiClient())

    async def scenario():
        async with Client(server) as client:
            for path in ("../runtime_phase1.db", "/etc/passwd", "outputs/../secret.txt", "outputs\\.git/config"):
                with pytest.raises(ToolError):
                    await client.call_tool(
                        "workspace_artifact_download",
                        {"worker_id": "wrk_123", "path": path},
                    )

    asyncio.run(scenario())


@pytest.mark.parametrize("surface", ["librechat", "glasshive"])
def test_workspace_status_returns_view_steer_link_for_web_mcp_surfaces(monkeypatch, surface):
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "https://glasshive.example.test")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "public-safe-signed-link-secret")
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {"X-GlassHive-Surface": surface})
    api_client = PollingApiClient(["completed"])
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            status = await client.call_tool(
                "workspace_status",
                {"run_id": "run_poll", "worker_id": "wrk_poll"},
            )
            payload = _tool_json(status)
            assert payload["view_steer_url"].startswith(
                "https://glasshive.example.test/watch/wrk_poll?surface=desktop&project_id=prj_poll"
            )
            assert "gh_token=" in payload["view_steer_url"]
            assert payload["view_steer"]["include_in_response"] is True

    asyncio.run(scenario())


def test_enterprise_view_steer_url_does_not_fall_back_to_unsigned(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "https://glasshive.example.test")
    monkeypatch.delenv("GLASSHIVE_SIGNED_LINK_SECRET", raising=False)
    monkeypatch.delenv("WPR_API_TOKEN", raising=False)

    url = mcp_server._signed_view_steer_url(
        {
            "worker_id": "wrk_local",
            "project_id": "prj_local",
            "tenant_id": "local",
            "owner_id": "owner-a",
        },
        "prj_local",
        "librechat",
    )

    assert url is None


def test_enterprise_mcp_refuses_non_http_transport(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")

    with pytest.raises(RuntimeError, match="streamable-http"):
        mcp_server._require_enterprise_mcp_transport("stdio")
    with pytest.raises(RuntimeError, match="streamable-http"):
        mcp_server._require_enterprise_mcp_transport("sse")
    mcp_server._require_enterprise_mcp_transport("streamable-http")


@pytest.mark.parametrize("surface", ["telegram", "voice", "unknown-surface"])
def test_worker_takeover_omits_operator_url_for_non_web_surface(monkeypatch, surface):
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "http://127.0.0.1:8780")
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {"X-Viventium-Surface": surface})
    server = create_mcp_server(api_client=FakeApiClient())

    async def scenario():
        async with Client(server) as client:
            takeover = await client.call_tool("worker_takeover", {"worker_id": "wrk_123"})
            payload = _tool_json(takeover)
            assert payload["operator_url"] is None
            assert payload["watch_url"] is None
            assert payload["operator_url_available"] is False
            assert payload["operator_url_surface"] == surface
            assert payload["view_url"] is None
            assert payload["runtime_takeover_url"] is None
            assert payload["direct_desktop_url"] is None
            assert payload["terminal_url"] is None
            assert payload["worker_url"] is None
            assert payload["takeover"]["url_available"] is False
            assert "url" not in payload["takeover"]
            serialized = json.dumps(payload)
            assert "127.0.0.1" not in serialized
            assert "localhost" not in serialized
            assert "noVNC" not in serialized

    asyncio.run(scenario())


def test_worker_delegate_once_creates_resumes_and_runs_without_listing(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_CALLBACK_URL", "http://127.0.0.1:3180/api/viventium/glasshive/callback")
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", "public-safe-test-secret")
    monkeypatch.setattr(mcp_server, "get_http_headers", _callback_headers)
    api_client = TrackingApiClient()
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            delegated = await client.call_tool(
                "worker_delegate_once",
                {
                    "owner_id": "demo-owner",
                    "title": "Host Page Title QA",
                    "goal": "Open a local page and report the title.",
                    "instruction": "Open the local QA page and reply with the page title.",
                    "profile": "codex-cli",
                    "execution_mode": "host",
                    "bootstrap_bundle_json": {
                        "files": {
                            "project-definition.md": "# Host Page Title QA\n\nReport the page title.\n",
                        }
                    },
                },
            )
            payload = _tool_json(delegated)
            assert payload["status"] == "dispatched"
            assert payload["callback_ready"] is True
            assert payload["view_steer_url"].startswith(
                "http://127.0.0.1:8780/watch/wrk_resumed?surface=desktop&project_id=prj_new"
            )
            assert payload["view_steer"]["include_in_acknowledgement"] is True
            assert payload["follow_up_context"]["worker_id"] == "wrk_resumed"
            assert payload["follow_up_context"]["run_id"] == "run_assign"
            assert payload["follow_up_context"]["project_id"] == "prj_new"
            assert payload["follow_up_context"]["status_tool"] == "workspace_status"
            assert payload["follow_up_context"]["blocking_wait_tool"] == "workspace_wait"
            assert "user_status" not in payload
            assert "own voice" in payload["acknowledgement_guidance"]
            assert "canned template" in payload["acknowledgement_guidance"]
            assert "Do not call workspace_status" in payload["main_agent_next_action"]
            assert "follow_up_context.run_id" in payload["main_agent_next_action"]
            assert payload["delegation_audit"]["title"] == "Host Page Title QA"
            assert "Open the local QA page" in payload["delegation_audit"]["instruction_preview"]
            assert "project_id" not in payload
            assert "worker_id" not in payload
            assert "run_id" not in payload
            assert "alias" not in payload

    asyncio.run(scenario())


def test_worker_schedule_and_workspace_schedule_are_glasshive_native(monkeypatch):
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_CALLBACK_URL", "http://127.0.0.1:3180/api/viventium/glasshive/callback")
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", "public-safe-test-secret")
    monkeypatch.setattr(mcp_server, "get_http_headers", _callback_headers)
    api_client = TrackingApiClient()
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            scheduled = await client.call_tool(
                "worker_schedule",
                {
                    "worker_id": "wrk_123",
                    "instruction": "Create scheduled-proof.txt",
                    "schedule_text": "in 20 minutes",
                },
            )
            scheduled_payload = _tool_json(scheduled)
            assert scheduled_payload["schedule_id"] == "sch_123"
            assert scheduled_payload["schedule_text"] == "in 20 minutes"

            workspace = await client.call_tool(
                "workspace_schedule",
                {
                    "description": "Check the workspace later",
                    "success_criteria": "A scheduled run is accepted",
                    "schedule_text": "in 20 minutes",
                    "profile": "codex-cli",
                    "execution_mode": "docker",
                    "expose_diagnostics": True,
                },
            )
            workspace_payload = _tool_json(workspace)
            assert workspace_payload["status"] == "scheduled"
            assert workspace_payload["schedule_id"] == "sch_123"
            assert workspace_payload["callback_ready"] is True

    asyncio.run(scenario())
    assert api_client.calls == ["create_project", "find_or_resume_worker"]
    assert api_client.find_or_resume_payloads[0]["start_synchronously"] is False


def test_worker_delegate_once_exposes_diagnostics_only_when_requested(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_CALLBACK_URL", "http://127.0.0.1:3180/api/viventium/glasshive/callback")
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", "public-safe-test-secret")
    monkeypatch.setattr(mcp_server, "get_http_headers", _callback_headers)
    server = create_mcp_server(api_client=TrackingApiClient())

    async def scenario():
        async with Client(server) as client:
            delegated = await client.call_tool(
                "worker_delegate_once",
                {
                    "owner_id": "demo-owner",
                    "title": "Diagnostic host task",
                    "instruction": "Run a diagnostic host task.",
                    "profile": "codex-cli",
                    "execution_mode": "host",
                    "expose_diagnostics": True,
                },
            )
            payload = _tool_json(delegated)
            assert payload["project_id"] == "prj_new"
            assert payload["worker_id"] == "wrk_resumed"
            assert payload["run_id"] == "run_assign"
            assert payload["execution_mode"] == "host"
            assert payload["alias"] == "codex-cli-diagnostic-host-task"
            assert payload["submitted_instruction"] == "Run a diagnostic host task."

    asyncio.run(scenario())


def test_worker_delegate_once_dispatches_without_callback_by_default(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    monkeypatch.delenv("VIVENTIUM_GLASSHIVE_CALLBACK_URL", raising=False)
    monkeypatch.delenv("VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", raising=False)
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {})
    api_client = TrackingApiClient()
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            delegated = await client.call_tool(
                "worker_delegate_once",
                {
                    "owner_id": "demo-owner",
                    "title": "Host Page Title QA",
                    "instruction": "Open the local QA page and reply with the page title.",
                    "profile": "codex-cli",
                    "execution_mode": "host",
                },
            )
            payload = _tool_json(delegated)
            assert payload["status"] == "dispatched"
            assert payload["callback_ready"] is False
            assert payload["callback_delivery"] == "not_configured_standalone_polling_available"
            assert payload["follow_up_context"]["status_tool"] == "workspace_status"
            assert payload["follow_up_context"]["blocking_wait_tool"] == "workspace_wait"
            assert "user_status" not in payload
            assert "ask for status" in payload["acknowledgement_guidance"]
            assert "workspace_status" in payload["main_agent_next_action"]
            assert payload["missing_callback_fields"] == []

    asyncio.run(scenario())
    assert api_client.calls == ["create_project", "find_or_resume_worker", "assign_run"]


def test_worker_delegate_once_can_require_callback_for_host_apps(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    monkeypatch.delenv("VIVENTIUM_GLASSHIVE_CALLBACK_URL", raising=False)
    monkeypatch.delenv("VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", raising=False)
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {})
    api_client = TrackingApiClient()
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            delegated = await client.call_tool(
                "worker_delegate_once",
                {
                    "owner_id": "demo-owner",
                    "title": "Host callback-required QA",
                    "instruction": "Only dispatch when host callback context is present.",
                    "profile": "codex-cli",
                    "execution_mode": "host",
                    "require_callback": True,
                },
            )
            payload = _tool_json(delegated)
            assert payload["status"] == "blocked"
            assert payload["callback_ready"] is False
            assert "conversation_id" in payload["missing_callback_fields"]

    asyncio.run(scenario())
    assert api_client.calls == []


def test_workspace_status_and_wait_are_standalone_mcp_followup_tools(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "http://127.0.0.1:8780")
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {"X-Viventium-Surface": "web"})
    api_client = PollingApiClient(["queued", "running", "completed"])
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            status = await client.call_tool(
                "workspace_status",
                {"run_id": "run_poll", "worker_id": "wrk_poll"},
            )
            status_payload = _tool_json(status)
            assert status_payload["mode"] == "non_blocking"
            assert status_payload["terminal"] is False
            assert status_payload["run_state"] == "queued"
            assert status_payload["worker_state"] == "ready"
            assert status_payload["view_steer_url"].startswith(
                "http://127.0.0.1:8780/watch/wrk_poll?surface=desktop&project_id=prj_poll"
            )

            waited = await client.call_tool(
                "workspace_wait",
                {
                    "run_id": "run_poll",
                    "worker_id": "wrk_poll",
                    "timeout_seconds": 1,
                    "poll_interval_seconds": 0.01,
                },
            )
            waited_payload = _tool_json(waited)
            assert waited_payload["mode"] == "blocking_wait"
            assert waited_payload["status"] == "completed"
            assert waited_payload["terminal"] is True
            assert waited_payload["output_text"] == "Poll completed"
            assert waited_payload["attempts"] == 2

    asyncio.run(scenario())
    assert api_client.get_run_calls == 3


def test_workspace_wait_returns_timeout_without_callback(monkeypatch):
    monkeypatch.setattr(mcp_server, "get_http_headers", lambda: {})
    api_client = PollingApiClient(["running"])
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            waited = await client.call_tool(
                "workspace_wait",
                {
                    "run_id": "run_poll",
                    "timeout_seconds": 0,
                    "poll_interval_seconds": 0.01,
                    "include_live": False,
                },
            )
            payload = _tool_json(waited)
            assert payload["status"] == "timeout"
            assert payload["timed_out"] is True
            assert payload["terminal"] is False
            assert "workspace_status" in payload["next_action_guidance"]

    asyncio.run(scenario())
    assert api_client.get_run_calls == 1


def test_worker_delegate_once_merges_upload_headers(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_CALLBACK_URL", "http://127.0.0.1:3180/api/viventium/glasshive/callback")
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", "public-safe-test-secret")
    files = [
        {
            "file_id": "file-999",
            "filename": "brief.txt",
            "text": "Synthetic upload brief.",
            "source": "local",
            "context": "message_attachment",
        }
    ]
    encoded_files = "b64:" + base64.b64encode(json.dumps(files).encode()).decode()
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            **_callback_headers(),
            "X-Viventium-Request-Files": encoded_files,
        },
    )
    api_client = TrackingApiClient()
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            delegated = await client.call_tool(
                "worker_delegate_once",
                {
                    "owner_id": "demo-owner",
                    "title": "Host File QA",
                    "instruction": "Read the attached brief and summarize it.",
                    "profile": "codex-cli",
                    "execution_mode": "host",
                },
            )
            payload = _tool_json(delegated)
            assert payload["status"] == "dispatched"

    asyncio.run(scenario())
    worker_payload = api_client.find_or_resume_payloads[0]
    bundle = worker_payload["bootstrap_bundle"]
    assert bundle["project_definition"].startswith("# Host File QA")
    assert bundle["files"][0]["path"] == "uploads/brief.txt"
    assert bundle["files"][0]["content"] == "Synthetic upload brief."


def test_worker_delegate_once_materializes_explicit_uploaded_files(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-GlassHive-Tenant-Id": "tenant-alpha",
            "X-GlassHive-User-Id": "user-123",
        },
    )
    api_client = TrackingApiClient()
    server = create_mcp_server(api_client=api_client)

    async def scenario():
        async with Client(server) as client:
            delegated = await client.call_tool(
                "worker_delegate_once",
                {
                    "owner_id": "ignored-by-test-client",
                    "title": "Upload Roundtrip QA",
                    "instruction": "Read uploads/client_upload.txt and write upload_roundtrip_result.txt.",
                    "profile": "codex-cli",
                    "execution_mode": "docker",
                    "uploaded_files": [
                        {
                            "file_id": "file-explicit-1",
                            "filename": "client_upload.txt",
                            "text": "CLIENT_UPLOAD_SMOKE_20260524\n",
                            "source": "librechat_model_context",
                            "context": "message_attachment",
                        }
                    ],
                },
            )
            payload = _tool_json(delegated)
            assert payload["status"] == "dispatched"

    asyncio.run(scenario())
    bundle = api_client.find_or_resume_payloads[0]["bootstrap_bundle"]
    assert bundle["glasshive_upload_context"]["tool_uploaded_files"][0]["file_id"] == "file-explicit-1"
    assert bundle["viventium_upload_context"]["tool_uploaded_files"][0]["filename"] == "client_upload.txt"
    assert bundle["files"][0]["path"] == "uploads/client_upload.txt"
    assert bundle["files"][0]["content"] == "CLIENT_UPLOAD_SMOKE_20260524\n"
    assert "## Attached workspace files" in bundle["project_definition"]
    assert "`uploads/client_upload.txt`" in bundle["project_definition"]
    assert "Do not ask the user to re-attach" in bundle["project_definition"]


def test_worker_tools_use_configured_default_execution_mode(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    server = create_mcp_server(api_client=FakeApiClient())

    async def scenario():
        async with Client(server) as client:
            worker = await client.call_tool(
                "worker_find_or_resume",
                {
                    "project_id": "prj_new",
                    "owner_id": "demo-owner",
                    "name": "Codex Host",
                    "role": "coding",
                    "alias": "codex-main",
                    "profile": "codex-cli",
                },
            )
            worker_payload = _tool_json(worker)
            assert worker_payload["execution_mode"] == "host"

    asyncio.run(scenario())


def test_worker_create_accepts_structured_bootstrap_bundle_files_mapping(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    server = create_mcp_server(api_client=FakeApiClient())

    async def scenario():
        async with Client(server) as client:
            worker = await client.call_tool(
                "worker_create",
                {
                    "project_id": "prj_new",
                    "owner_id": "demo-owner",
                    "name": "Host Browser QA",
                    "role": "Open a host browser and report the page title.",
                    "profile": "codex-cli",
                    "execution_mode": "host",
                    "bootstrap_bundle_json": {
                        "files": {
                            "project-definition.md": "# Host Browser QA\n\nOpen a local page and report the title.\n",
                            "notes/context.md": "Synthetic QA context.\n",
                        }
                    },
                },
            )
            worker_payload = _tool_json(worker)
            bundle = worker_payload["bootstrap_bundle"]
            assert bundle["project_definition"] == "# Host Browser QA\n\nOpen a local page and report the title.\n"
            assert bundle["files"] == [
                {
                    "scope": "workspace",
                    "path": "project-definition.md",
                    "content": "# Host Browser QA\n\nOpen a local page and report the title.\n",
                },
                {
                    "scope": "workspace",
                    "path": "notes/context.md",
                    "content": "Synthetic QA context.\n",
                },
            ]

    asyncio.run(scenario())


def test_worker_create_merges_structured_bootstrap_bundle_with_upload_headers(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    files = [
        {
            "file_id": "file-789",
            "filename": "brief.txt",
            "text": "Synthetic upload brief.",
            "source": "local",
            "context": "message_attachment",
        }
    ]
    encoded_files = "b64:" + base64.b64encode(json.dumps(files).encode()).decode()
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-Viventium-Request-Files": encoded_files,
        },
    )
    server = create_mcp_server(api_client=FakeApiClient())

    async def scenario():
        async with Client(server) as client:
            worker = await client.call_tool(
                "worker_create",
                {
                    "project_id": "prj_new",
                    "owner_id": "demo-owner",
                    "name": "Host File QA",
                    "role": "Read the uploaded brief on the host worker.",
                    "profile": "codex-cli",
                    "execution_mode": "host",
                    "bootstrap_bundle_json": {
                        "files": {
                            "project-definition.md": "# Host File QA\n\nRead the attached brief.\n",
                        }
                    },
                },
            )
            worker_payload = _tool_json(worker)
            bundle = worker_payload["bootstrap_bundle"]
            assert bundle["project_definition"] == "# Host File QA\n\nRead the attached brief.\n"
            paths = [item["path"] for item in bundle["files"]]
            assert paths == ["project-definition.md", "uploads/brief.txt"]
            assert bundle["files"][1]["content"] == "Synthetic upload brief."

    asyncio.run(scenario())


def test_worker_find_or_resume_keeps_json_string_bootstrap_bundle(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    server = create_mcp_server(api_client=FakeApiClient())

    async def scenario():
        async with Client(server) as client:
            worker = await client.call_tool(
                "worker_find_or_resume",
                {
                    "project_id": "prj_new",
                    "owner_id": "demo-owner",
                    "name": "Host Browser QA",
                    "role": "Open a host browser and report the page title.",
                    "alias": "host-browser-qa",
                    "profile": "codex-cli",
                    "execution_mode": "host",
                    "bootstrap_bundle_json": json.dumps(
                        {
                            "project_definition": "Open a local page and report the title.",
                            "files": [{"scope": "workspace", "path": "notes/context.md", "content": "QA context.\n"}],
                        }
                    ),
                },
            )
            worker_payload = _tool_json(worker)
            bundle = worker_payload["bootstrap_bundle"]
            assert bundle["project_definition"] == "Open a local page and report the title."
            assert bundle["files"][0]["path"] == "notes/context.md"

    asyncio.run(scenario())


def test_worker_find_or_resume_accepts_structured_bootstrap_bundle(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    server = create_mcp_server(api_client=FakeApiClient())

    async def scenario():
        async with Client(server) as client:
            worker = await client.call_tool(
                "worker_find_or_resume",
                {
                    "project_id": "prj_new",
                    "owner_id": "demo-owner",
                    "name": "Host Browser QA",
                    "role": "Open a host browser and report the page title.",
                    "alias": "host-browser-qa",
                    "profile": "codex-cli",
                    "execution_mode": "host",
                    "bootstrap_bundle_json": {
                        "files": {
                            "project-definition.md": "# Host Browser QA\n\nReport the page title.\n",
                        }
                    },
                },
            )
            worker_payload = _tool_json(worker)
            bundle = worker_payload["bootstrap_bundle"]
            assert bundle["project_definition"] == "# Host Browser QA\n\nReport the page title.\n"
            assert bundle["files"][0]["path"] == "project-definition.md"

    asyncio.run(scenario())


def test_normalize_bootstrap_bundle_ignores_malformed_files_value():
    bundle = mcp_server._normalize_bootstrap_bundle({"project_definition": "Do the work.", "files": "oops"})

    assert bundle == {"project_definition": "Do the work.", "files": []}


def test_worker_tool_schemas_advertise_host_native_execution(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    server = create_mcp_server(api_client=FakeApiClient())

    async def scenario():
        async with Client(server) as client:
            tools = {tool.name: tool.model_dump() for tool in await client.list_tools()}
            worker_create = tools["worker_create"]
            worker_resume = tools["worker_find_or_resume"]
            desktop_action = tools["worker_desktop_action"]
            worker_delegate = tools["worker_delegate_once"]
            workspace_launch = tools["workspace_launch"]

            for tool in (worker_create, worker_resume):
                description = tool["description"]
                assert "execution_mode='host'" in description
                schema = tool["inputSchema"]["properties"]
                execution_schema = schema["execution_mode"]
                assert execution_schema["anyOf"][0]["enum"] == ["docker", "host"]
                assert "real computer/session" in execution_schema["description"]
                assert "codex-cli" in schema["profile"]["description"]
                bootstrap_schema = schema["bootstrap_bundle_json"]
                bootstrap_types = {
                    variant.get("type")
                    for variant in bootstrap_schema.get("anyOf", [])
                    if isinstance(variant, dict)
                }
                assert {"string", "object", "null"}.issubset(bootstrap_types)

            action_schema = desktop_action["inputSchema"]["properties"]["action"]
            assert action_schema["enum"] == [
                "terminal",
                "files",
                "browser",
                "focus_browser",
                "codex",
                "claude",
                "openclaw",
            ]
            for tool in (worker_delegate, workspace_launch):
                upload_schema = tool["inputSchema"]["properties"]["uploaded_files"]
                upload_description = upload_schema["description"].lower()
                assert "attached/uploaded files" in upload_description
                assert "chat host does not project upload metadata" in upload_description

    asyncio.run(scenario())


def test_workspace_launch_uses_documented_ui_fields_without_low_level_chain(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")
    api = TrackingApiClient()
    server = create_mcp_server(api_client=api)

    async def scenario():
        async with Client(server) as client:
            result = await client.call_tool(
                "workspace_launch",
                {
                    "description": "Build a small evidence file from the uploaded report.",
                    "success_criteria": "The workspace creates summary.txt and reports where it is.",
                    "context": "Use the attached file and keep the workspace resumable.",
                    "profile": "codex-cli",
                    "require_callback": False,
                },
            )
            payload = _tool_json(result)
            assert payload["status"] == "dispatched"

    asyncio.run(scenario())

    assert api.calls == ["create_project", "find_or_resume_worker", "assign_run"]
    assert api.find_or_resume_payloads[-1]["profile"] == "codex-cli"


def test_workspace_launch_reuses_existing_workspace_alias_across_projects(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")

    class ExistingAliasApi(TrackingApiClient):
        def list_projects(self, owner_id: str | None = None):
            self.calls.append("list_projects")
            return [{"project_id": "prj_existing", "owner_id": "demo-owner", "title": "Marketing sandbox"}]

        def list_workers(self, project_id: str):
            self.calls.append("list_workers")
            return [
                {
                    "worker_id": "wrk_existing",
                    "project_id": project_id,
                    "owner_id": "demo-owner",
                    "name": "JohnDoe",
                    "role": "Marketing",
                    "profile": "codex-cli",
                    "backend": "openclaw",
                    "execution_mode": "docker",
                    "alias": "marketing-sandbox",
                    "state": "paused",
                }
            ]

    api = ExistingAliasApi()
    server = create_mcp_server(api_client=api)

    async def scenario():
        async with Client(server) as client:
            result = await client.call_tool(
                "workspace_launch",
                {
                    "description": "Use my Marketing sandbox to update the campaign note.",
                    "success_criteria": "The existing named workspace is reused.",
                    "workspace_alias": "marketing-sandbox",
                    "profile": "codex-cli",
                    "require_callback": False,
                },
            )
            payload = _tool_json(result)
            assert payload["status"] == "dispatched"

    asyncio.run(scenario())

    assert "create_project" not in api.calls
    assert api.find_or_resume_payloads[-1]["project_id"] == "prj_existing"
    assert api.find_or_resume_payloads[-1]["alias"] == "marketing-sandbox"


def test_workspace_launch_reuses_enterprise_scoped_workspace_alias(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "docker")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "first_party_assertion")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-alpha")
    monkeypatch.setenv("WPR_API_TOKEN", "service-token")
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-WPR-Token": "service-token",
            "X-Viventium-Tenant-Id": "tenant-alpha",
            "X-Viventium-User-Id": "user-a",
            "X-Viventium-User-Role": "member",
        },
    )

    class ExistingEnterpriseAliasApi(TrackingApiClient):
        def list_projects(self, owner_id: str | None = None):
            self.calls.append("list_projects")
            return [{"project_id": "prj_existing", "owner_id": "user-a", "title": "Marketing sandbox"}]

        def list_workers(self, project_id: str):
            self.calls.append("list_workers")
            return [
                {
                    "worker_id": "wrk_existing",
                    "project_id": project_id,
                    "owner_id": "user-a",
                    "name": "JohnDoe",
                    "role": "Marketing",
                    "profile": "codex-cli",
                    "backend": "openclaw",
                    "execution_mode": "docker",
                    "alias": "tenant-alpha--user-a--marketing-sandbox",
                    "state": "paused",
                }
            ]

    api = ExistingEnterpriseAliasApi()
    server = create_mcp_server(api_client=api)

    async def scenario():
        async with Client(server) as client:
            result = await client.call_tool(
                "workspace_launch",
                {
                    "description": "Tell JohnDoe to use my Marketing sandbox for the next task.",
                    "success_criteria": "The enterprise-scoped workspace alias is reused.",
                    "workspace_alias": "marketing-sandbox",
                    "profile": "codex-cli",
                    "require_callback": False,
                },
            )
            payload = _tool_json(result)
            assert payload["status"] == "dispatched"

    asyncio.run(scenario())

    assert "create_project" not in api.calls
    assert api.find_or_resume_payloads[-1]["project_id"] == "prj_existing"
    assert api.find_or_resume_payloads[-1]["alias"] == "marketing-sandbox"


def test_tool_descriptions_advertise_mcp_owned_usage_contract(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    server = create_mcp_server(api_client=FakeApiClient())

    public_action_tools = {
        "projects_list",
        "project_create",
        "project_get",
        "workspace_launch",
        "worker_delegate_once",
        "project_runs",
        "project_events",
        "workers_list",
        "worker_create",
        "worker_find_or_resume",
        "worker_get",
        "worker_live",
        "worker_run",
        "worker_message",
        "worker_pause",
        "worker_resume",
        "worker_interrupt",
        "worker_terminate",
        "worker_desktop_action",
        "worker_takeover",
        "run_get",
        "workspace_status",
        "workspace_wait",
        "workspace_artifacts",
        "workspace_artifact_download",
        "metrics_summary",
    }

    async def scenario():
        async with Client(server) as client:
            tools = {tool.name: tool.model_dump() for tool in await client.list_tools()}
            assert public_action_tools.issubset(set(tools))

            for tool_name in public_action_tools:
                description = tools[tool_name]["description"]
                normalized = description.lower()
                assert "use" in normalized, tool_name
                assert "return" in normalized, tool_name
                assert any(marker in normalized for marker in ("do not", "prefer", "only when", "instead")), tool_name
                assert len(description.split()) >= 20, tool_name

            delegate_description = tools["worker_delegate_once"]["description"]
            workspace_description = tools["workspace_launch"]["description"]
            assert "description" in workspace_description
            assert "success_criteria" in workspace_description
            assert "optional context" in workspace_description
            assert "Do not chain project_create" in workspace_description
            assert "uploaded-file requests" in workspace_description

            assert "callbacks are optional" in delegate_description.lower()
            assert "uploaded-file tasks" in delegate_description
            assert "workspace_status" in delegate_description
            assert "workspace_wait" in delegate_description
            assert "write your own short acknowledgement" in delegate_description.lower()
            assert "delegation_audit" in delegate_description
            assert "View / Steer link" in delegate_description
            assert "follow_up_context" in delegate_description
            assert "worker_id/run_id" in delegate_description

            desktop_description = tools["worker_desktop_action"]["description"]
            assert "raw desktop URLs are diagnostic" in desktop_description

    asyncio.run(scenario())


def test_host_worker_disabled_forces_docker_default_and_rejects_host(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_HOST_WORKERS_ENABLED", "false")
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    server = create_mcp_server(api_client=FakeApiClient())

    async def scenario():
        async with Client(server) as client:
            worker = await client.call_tool(
                "worker_find_or_resume",
                {
                    "project_id": "prj_new",
                    "owner_id": "demo-owner",
                    "name": "Docker Worker",
                    "role": "coding",
                    "alias": "docker-main",
                    "profile": "codex-cli",
                },
            )
            worker_payload = _tool_json(worker)
            assert worker_payload["execution_mode"] == "docker"

            with pytest.raises(ToolError, match="host-native GlassHive workers are disabled"):
                await client.call_tool(
                    "worker_find_or_resume",
                    {
                        "project_id": "prj_new",
                        "owner_id": "demo-owner",
                        "name": "Codex Host",
                        "role": "coding",
                        "alias": "codex-main",
                        "profile": "codex-cli",
                        "execution_mode": "host",
                    },
                )

    asyncio.run(scenario())


def test_worker_tools_default_owner_from_request_headers(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-Viventium-User-Id": "user-from-request",
        },
    )
    server = create_mcp_server(api_client=FakeApiClient())

    async def scenario():
        async with Client(server) as client:
            project = await client.call_tool(
                "project_create",
                {
                    "title": "Host Browser QA",
                    "goal": "Open a host browser.",
                    "default_worker_profile": "codex-cli",
                },
            )
            project_payload = _tool_json(project)
            assert project_payload["owner_id"] == "user-from-request"

            worker = await client.call_tool(
                "worker_find_or_resume",
                {
                    "project_id": "prj_new",
                    "name": "Codex Host",
                    "role": "host browser control",
                    "alias": "codex-main",
                    "profile": "codex-cli",
                    "execution_mode": "host",
                },
            )
            worker_payload = _tool_json(worker)
            assert worker_payload["owner_id"] == "user-from-request"
            assert worker_payload["execution_mode"] == "host"

    asyncio.run(scenario())


def test_merge_request_context_adds_callback_metadata(monkeypatch):
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_CALLBACK_URL", "http://localhost:3080/api/viventium/glasshive/callback")
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", "callback-secret")
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-Viventium-User-Id": "user-123",
            "X-Viventium-Agent-Id": "agent-main",
            "X-Viventium-Conversation-Id": "conv-123",
            "X-Viventium-Parent-Message-Id": "msg-parent",
            "X-Viventium-Message-Id": "msg-current",
            "X-Viventium-Surface": "telegram",
            "X-Viventium-Input-Mode": "voice_note",
            "X-Viventium-Stream-Id": "stream-123",
            "X-Viventium-Voice-Call-Session-Id": "call-123",
            "X-Viventium-Voice-Request-Id": "voice-req-123",
            "X-Viventium-Telegram-Chat-Id": "chat-123",
            "X-Viventium-Telegram-User-Id": "tg-user-123",
            "X-Viventium-Telegram-Message-Id": "tg-msg-123",
        },
    )

    bundle = mcp_server._merge_request_context({"project_definition": "Do the work"})

    assert bundle is not None
    callbacks = bundle["callbacks"]
    assert callbacks["events_webhook_url"].endswith("/api/viventium/glasshive/callback")
    assert callbacks["hmac_secret"] == "callback-secret"
    assert callbacks["conversation_id"] == "conv-123"
    assert callbacks["parent_message_id"] == "msg-parent"
    assert callbacks["surface"] == "telegram"
    assert callbacks["stream_id"] == "stream-123"
    assert callbacks["voice_call_session_id"] == "call-123"
    assert callbacks["telegram_chat_id"] == "chat-123"
    assert bundle["viventium_context"]["user_id"] == "user-123"


def test_merge_request_context_loads_callback_metadata_from_runtime_env(tmp_path, monkeypatch):
    runtime_env = tmp_path / "runtime.env"
    runtime_env.write_text(
        "\n".join(
            [
                "VIVENTIUM_GLASSHIVE_CALLBACK_URL=http://localhost:3080/api/viventium/glasshive/callback",
                "VIVENTIUM_GLASSHIVE_CALLBACK_SECRET=runtime-secret",
            ]
        )
        + "\n"
    )
    monkeypatch.setenv("VIVENTIUM_ENV_FILE", str(runtime_env))
    monkeypatch.delenv("VIVENTIUM_GLASSHIVE_CALLBACK_URL", raising=False)
    monkeypatch.delenv("VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", raising=False)
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-Viventium-User-Id": "user-123",
            "X-Viventium-Conversation-Id": "conv-123",
            "X-Viventium-Parent-Message-Id": "msg-parent",
            "X-Viventium-Message-Id": "msg-current",
        },
    )

    bundle = mcp_server._merge_request_context({"project_definition": "Do the work"})

    assert bundle is not None
    callbacks = bundle["callbacks"]
    assert callbacks["events_webhook_url"].endswith("/api/viventium/glasshive/callback")
    assert callbacks["hmac_secret"] == "runtime-secret"


def test_merge_request_context_does_not_auto_attach_incomplete_parent_callback(monkeypatch):
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_CALLBACK_URL", "http://localhost:3080/api/viventium/glasshive/callback")
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", "callback-secret")
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-Viventium-User-Id": "user-123",
            "X-Viventium-Conversation-Id": "conv-123",
        },
    )

    bundle = mcp_server._merge_request_context({"project_definition": "Do the work"})

    assert bundle is not None
    assert "callbacks" not in bundle
    assert bundle["viventium_context"]["user_id"] == "user-123"
    assert bundle["viventium_context"]["conversation_id"] == "conv-123"


def test_merge_request_context_projects_uploaded_file_headers(monkeypatch):
    monkeypatch.setattr(mcp_server, "load_viventium_runtime_env", lambda: {})
    monkeypatch.delenv("WPR_LIBRECHAT_UPLOADS_ROOT", raising=False)
    files = [
        {
            "file_id": "file-123",
            "filename": "brief.txt",
            "filepath": "/uploads/user/brief.txt",
            "source": "local",
            "context": "message_attachment",
        }
    ]
    encoded_files = "b64:" + base64.b64encode(json.dumps(files).encode()).decode()
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-Viventium-Request-Files": encoded_files,
        },
    )

    bundle = mcp_server._merge_request_context({"project_definition": "Read the attachment."})

    assert bundle is not None
    assert bundle["viventium_upload_context"]["request_files"][0]["file_id"] == "file-123"
    assert bundle["files"][0]["path"] == "uploads/brief.txt.metadata.json"
    assert "source_path" not in bundle["files"][0]
    manifest = json.loads(bundle["files"][0]["content"])
    assert manifest["file_id"] == "file-123"
    assert manifest["source_ref"] == "/uploads/user/brief.txt"


def test_merge_request_context_accepts_generic_glasshive_headers(monkeypatch):
    monkeypatch.setattr(mcp_server, "load_viventium_runtime_env", lambda: {})
    files = [
        {
            "file_id": "file-123",
            "filename": "brief.txt",
            "filepath": "/uploads/user/brief.txt",
            "source": "local",
            "context": "message_attachment",
        }
    ]
    encoded_files = "b64:" + base64.b64encode(json.dumps(files).encode()).decode()
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-GlassHive-Tenant-Id": "tenant-alpha",
            "X-GlassHive-User-Id": "user-123",
            "X-GlassHive-Conversation-Id": "conv-123",
            "X-GlassHive-Request-Files": encoded_files,
            "X-LibreChat-Tool-Resources": encoded_files,
        },
    )

    bundle = mcp_server._merge_request_context({"project_definition": "Read the attachment."})

    assert bundle is not None
    assert bundle["glasshive_context"]["tenant_id"] == "tenant-alpha"
    assert bundle["glasshive_context"]["user_id"] == "user-123"
    assert bundle["viventium_context"]["conversation_id"] == "conv-123"
    assert bundle["glasshive_upload_context"]["request_files"][0]["file_id"] == "file-123"
    assert bundle["viventium_upload_context"]["tool_resources"][0]["file_id"] == "file-123"
    assert bundle["files"][0]["path"] == "uploads/brief.txt.metadata.json"


def test_merge_request_context_maps_virtual_uploads_to_trusted_local_source(monkeypatch, tmp_path):
    uploads_root = tmp_path / "uploads"
    upload_path = uploads_root / "user-123" / "brief.txt"
    upload_path.parent.mkdir(parents=True)
    upload_path.write_text("Use this brief.")
    monkeypatch.setenv("WPR_API_TOKEN", "service-secret")
    monkeypatch.setenv("WPR_LIBRECHAT_UPLOADS_ROOT", str(uploads_root))
    files = [
        {
            "file_id": "file-123",
            "filename": "brief.txt",
            "filepath": "/uploads/user-123/brief.txt",
            "source": "local",
            "context": "message_attachment",
        }
    ]
    encoded_files = "b64:" + base64.b64encode(json.dumps(files).encode()).decode()
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-Viventium-Tenant-Id": "tenant-alpha",
            "X-Viventium-User-Id": "user-123",
            "X-Viventium-Request-Files": encoded_files,
        },
    )

    bundle = mcp_server._merge_request_context({"project_definition": "Read the attachment."})

    assert bundle is not None
    assert bundle["files"][0]["path"] == "uploads/brief.txt"
    assert bundle["files"][0]["source_path"] == str(upload_path)
    assert bundle["files"][0]["source_path_token"] == sign_bootstrap_source_path(
        upload_path,
        tenant_id="tenant-alpha",
        owner_id="user-123",
    )


def test_merge_request_context_uses_existing_source_root_when_upload_root_is_stale(monkeypatch, tmp_path):
    stale_root = tmp_path / "stale-uploads"
    fallback_root = tmp_path / "repo-uploads"
    upload_path = fallback_root / "user-123" / "brief.txt"
    upload_path.parent.mkdir(parents=True)
    upload_path.write_text("Use this brief.")
    monkeypatch.setenv("WPR_API_TOKEN", "service-secret")
    monkeypatch.setenv("WPR_LIBRECHAT_UPLOADS_ROOT", str(stale_root))
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", os.pathsep.join([str(stale_root), str(fallback_root)]))
    files = [
        {
            "file_id": "file-123",
            "filename": "brief.txt",
            "filepath": "/uploads/user-123/brief.txt",
            "source": "local",
            "context": "message_attachment",
        }
    ]
    encoded_files = "b64:" + base64.b64encode(json.dumps(files).encode()).decode()
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-Viventium-Tenant-Id": "tenant-alpha",
            "X-Viventium-User-Id": "user-123",
            "X-Viventium-Request-Files": encoded_files,
        },
    )

    bundle = mcp_server._merge_request_context({"project_definition": "Read the attachment."})

    assert bundle is not None
    assert bundle["files"][0]["source_path"] == str(upload_path)
    assert bundle["files"][0]["source_path_token"] == sign_bootstrap_source_path(
        upload_path,
        tenant_id="tenant-alpha",
        owner_id="user-123",
    )


def test_runtime_env_repairs_missing_upload_root_to_local_checkout(monkeypatch, tmp_path):
    fallback_root = tmp_path / "repo-uploads"
    fallback_root.mkdir()
    runtime_file = tmp_path / "runtime.env"
    runtime_file.write_text(
        "\n".join(
            [
                f"WPR_LIBRECHAT_UPLOADS_ROOT={tmp_path / 'missing-uploads'}",
                f"WPR_BOOTSTRAP_SOURCE_ROOTS={tmp_path / 'missing-uploads'}",
            ]
        )
        + "\n"
    )
    monkeypatch.setenv("VIVENTIUM_ENV_FILE", str(runtime_file))
    monkeypatch.delenv("WPR_LIBRECHAT_UPLOADS_ROOT", raising=False)
    monkeypatch.delenv("WPR_BOOTSTRAP_SOURCE_ROOTS", raising=False)
    monkeypatch.setattr(runtime_env, "_local_checkout_librechat_uploads_root", lambda: fallback_root)

    loaded = runtime_env.load_viventium_runtime_env({"WPR_LIBRECHAT_UPLOADS_ROOT", "WPR_BOOTSTRAP_SOURCE_ROOTS"})

    assert loaded["WPR_LIBRECHAT_UPLOADS_ROOT"] == str(fallback_root)
    assert os.environ["WPR_LIBRECHAT_UPLOADS_ROOT"] == str(fallback_root)
    assert str(fallback_root) in os.environ["WPR_BOOTSTRAP_SOURCE_ROOTS"].split(os.pathsep)


def test_merge_request_context_projects_extracted_upload_text(monkeypatch):
    files = [
        {
            "file_id": "file-456",
            "filename": "brief.txt",
            "text": "Use this brief.",
            "source": "local",
            "context": "message_attachment",
        }
    ]
    encoded_files = "b64:" + base64.b64encode(json.dumps(files).encode()).decode()
    monkeypatch.setattr(
        mcp_server,
        "get_http_headers",
        lambda: {
            "X-Viventium-Request-Files": encoded_files,
        },
    )

    bundle = mcp_server._merge_request_context({"project_definition": "Read the attachment."})

    assert bundle is not None
    assert bundle["files"][0]["path"] == "uploads/brief.txt"
    assert bundle["files"][0]["content"] == "Use this brief."
