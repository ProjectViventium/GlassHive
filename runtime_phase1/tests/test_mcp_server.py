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


class TrackingApiClient(FakeApiClient):
    def __init__(self):
        self.calls: list[str] = []
        self.find_or_resume_payloads: list[dict] = []

    def list_projects(self, owner_id: str | None = None):
        self.calls.append("list_projects")
        return super().list_projects(owner_id)

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


def test_mcp_server_exposes_tools_and_resources():
    server = create_mcp_server(api_client=FakeApiClient())

    async def scenario():
        async with Client(server) as client:
            tools = await client.list_tools()
            tool_names = {tool.name for tool in tools}
            assert "project_create" in tool_names
            assert "worker_delegate_once" in tool_names
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
            assert "Do not call worker_live" in payload["main_agent_next_action"]
            assert "project_id" not in payload
            assert "worker_id" not in payload
            assert "run_id" not in payload
            assert "alias" not in payload

    asyncio.run(scenario())
    assert api_client.calls == ["create_project", "find_or_resume_worker", "assign_run"]


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

    asyncio.run(scenario())


def test_worker_delegate_once_requires_callback_context_by_default(monkeypatch):
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
            assert payload["status"] == "blocked"
            assert payload["callback_ready"] is False
            assert "conversation_id" in payload["missing_callback_fields"]

    asyncio.run(scenario())
    assert api_client.calls == []


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
