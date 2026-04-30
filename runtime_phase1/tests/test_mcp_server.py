from __future__ import annotations

import asyncio
import base64
import json

from fastmcp import Client
from fastmcp.exceptions import ToolError
import pytest

from workers_projects_runtime import mcp_server
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
        }

    def find_or_resume_worker(self, **kwargs):
        payload = self.create_worker(**kwargs)
        payload["worker_id"] = "wrk_resumed"
        return payload

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
            assert takeover_payload["view_url"].startswith("http://127.0.0.1:")

            live_resource = await client.read_resource("wpr://workers/wrk_123/live")
            assert live_resource[0].text is not None
            live_payload = json.loads(live_resource[0].text)
            assert live_payload["worker"]["worker_id"] == "wrk_123"

    asyncio.run(scenario())


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


def test_worker_tool_schemas_advertise_host_native_execution(monkeypatch):
    monkeypatch.setenv("WPR_DEFAULT_EXECUTION_MODE", "host")
    server = create_mcp_server(api_client=FakeApiClient())

    async def scenario():
        async with Client(server) as client:
            tools = {tool.name: tool.model_dump() for tool in await client.list_tools()}
            worker_create = tools["worker_create"]
            worker_resume = tools["worker_find_or_resume"]
            desktop_action = tools["worker_desktop_action"]

            for tool in (worker_create, worker_resume):
                description = tool["description"]
                assert "execution_mode='host'" in description
                schema = tool["inputSchema"]["properties"]
                execution_schema = schema["execution_mode"]
                assert execution_schema["anyOf"][0]["enum"] == ["docker", "host"]
                assert "real computer/session" in execution_schema["description"]
                assert "codex-cli" in schema["profile"]["description"]

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
        },
    )

    bundle = mcp_server._merge_request_context({"project_definition": "Do the work"})

    assert bundle is not None
    callbacks = bundle["callbacks"]
    assert callbacks["events_webhook_url"].endswith("/api/viventium/glasshive/callback")
    assert callbacks["hmac_secret"] == "runtime-secret"


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


def test_merge_request_context_maps_virtual_uploads_to_trusted_local_source(monkeypatch, tmp_path):
    uploads_root = tmp_path / "uploads"
    upload_path = uploads_root / "user-123" / "brief.txt"
    upload_path.parent.mkdir(parents=True)
    upload_path.write_text("Use this brief.")
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
            "X-Viventium-Request-Files": encoded_files,
        },
    )

    bundle = mcp_server._merge_request_context({"project_definition": "Read the attachment."})

    assert bundle is not None
    assert bundle["files"][0]["path"] == "uploads/brief.txt"
    assert bundle["files"][0]["source_path"] == str(upload_path)


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
