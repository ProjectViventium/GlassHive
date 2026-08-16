from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import shutil
import sqlite3
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient

from workers_projects_runtime.agent_builder_control import (
    graph_transfer_output_schema,
)
from workers_projects_runtime.api import create_app
from workers_projects_runtime.conversation_provider import (
    ACTIVITY_SUMMARIES,
    ChatCompletionRequest,
    ChatMessage,
    ConversationProvider,
    GLASSHIVE_MODELS,
    StreamingRedactor,
    _harness_auth_configured,
    _history_instruction,
    _idempotency_key,
    _developer_instruction_snapshot,
    _native_usage,
    _native_visible_text,
    _normalized_harness_activity,
    _system_snapshot,
)
from workers_projects_runtime.openclaw_runtime import StubRuntime, WorkerInterruptedError
from workers_projects_runtime.service import WorkersProjectsService
from workers_projects_runtime.store import Store


AUTH = {
    "Authorization": "Bearer provider-test-token",
    "X-Viventium-User-Id": "owner-a",
}
BOOTSTRAP_SECRET = "synthetic-bootstrap-signature-secret"
_TEST_CLIENTS: list[TestClient] = []


@pytest.fixture(autouse=True)
def _shutdown_clients_created_by_test():
    yield
    while _TEST_CLIENTS:
        client = _TEST_CLIENTS.pop()
        service = client.app.state.service
        if not service._shutdown_event.is_set():
            service.shutdown()
        client.close()


def _signed_bootstrap_headers(bundle: dict, *, issued_at: int | None = None) -> dict[str, str]:
    encoded = base64.b64encode(json.dumps(bundle).encode()).decode()
    timestamp = str(issued_at if issued_at is not None else int(time.time()))
    digest = hmac.new(
        BOOTSTRAP_SECRET.encode(),
        f"v1\n{timestamp}\n{encoded}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return {
        "X-GlassHive-Bootstrap-Bundle-B64": encoded,
        "X-GlassHive-Bootstrap-Timestamp": timestamp,
        "X-GlassHive-Bootstrap-Signature": f"sha256={digest}",
    }


def _payload(workspace: Path, *, model: str = "codex-cli:gpt-5.6-sol", stream: bool = False) -> dict:
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": "Be a thoughtful assistant."},
            {"role": "user", "content": "Hello from LIFE."},
        ],
        "stream": stream,
        "metadata": {
            "owner_id": "owner-a",
            "conversation_id": "conv-a",
            "agent_id": "agent-a",
            "message_id": "message-a",
            "stream_id": "stream-a",
            "surface": "web",
            "input_mode": "text",
            "idempotency_key": "idem-a",
            "glasshive_options": {
                "workspace": {"mode": "custom", "path": str(workspace)},
                "access": "workspace",
            },
        },
    }


def _client(tmp_path: Path, monkeypatch, runtime=None) -> TestClient:
    monkeypatch.setenv("WPR_API_TOKEN", "provider-test-token")
    monkeypatch.setenv("GLASSHIVE_HOST_WORKERS_ENABLED", "1")
    monkeypatch.setenv("GLASSHIVE_ALLOWED_WORKER_PROFILES", "codex-cli,claude-code")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_ALLOWED_WORKSPACE_ROOTS", str(tmp_path))
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_CAPABILITY_BROKER_SECRET", BOOTSTRAP_SECRET)
    client = TestClient(
        create_app(
            str(tmp_path / "runtime.db"),
            runtime_backend="stub",
            runtime=runtime or StubRuntime(),
        )
    )
    _TEST_CLIENTS.append(client)
    return client


class ActivityStubRuntime(StubRuntime):
    def provider_activity_log(self, worker: dict, run_id: str) -> tuple[str, str]:
        _ = worker, run_id
        return (
            "codex-cli",
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {"type": "command_execution", "status": "completed", "exit_code": 0},
                        }
                    ),
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {"type": "file_change", "status": "completed", "changes": [{}]},
                        }
                    ),
                ]
            ),
        )


class BrokerActivityStubRuntime(StubRuntime):
    def provider_activity_log(self, worker: dict, run_id: str) -> tuple[str, str]:
        _ = worker, run_id
        return (
            "codex-cli",
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "mcp_tool_call_end",
                        "call_id": "private-call-id",
                        "duration": 1.25,
                        "invocation": {
                            "server": "glasshive-user-capabilities",
                            "tool": "gh_scheduling_cortex__schedule_create",
                            "arguments": {"title": "private reminder title"},
                        },
                        "result": {"task_id": "private-task-id"},
                    },
                }
            ),
        )


class InterruptCountingRuntime(StubRuntime):
    def __init__(self):
        super().__init__()
        self.interrupt_calls: list[tuple[str, str | None]] = []

    def interrupt_worker(self, worker: dict, run_id: str | None = None):
        self.interrupt_calls.append((str(worker["worker_id"]), run_id))
        return super().interrupt_worker(worker)


class BrokerBundleCaptureRuntime(StubRuntime):
    def __init__(self):
        super().__init__()
        self.run_bundles: list[dict] = []

    def run_task(
        self,
        worker: dict,
        instruction: str,
        timeout_sec: float | None = None,
        run_id: str | None = None,
    ) -> str:
        _ = instruction, timeout_sec, run_id
        self.run_bundles.append(json.loads(str(worker["bootstrap_bundle_json"])))
        return "Broker-backed conversation completed."


class SplitSecretStreamingRuntime(StubRuntime):
    def __init__(self):
        super().__init__()
        self.stdout = ""

    def run_task(
        self,
        worker: dict,
        instruction: str,
        timeout_sec: float | None = None,
        run_id: str | None = None,
    ) -> str:
        _ = worker, instruction, timeout_sec, run_id
        first = {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": "Before api_key=PUBLIC_FAKE_"}]
            },
        }
        second = {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "SECRET_VALUE after\n"}]},
        }
        self.stdout = json.dumps(first)
        time.sleep(0.15)
        self.stdout += "\n" + json.dumps(second)
        time.sleep(0.15)
        return "Before api_key=PUBLIC_FAKE_SECRET_VALUE after\n"

    def provider_activity_log(self, worker: dict, run_id: str) -> tuple[str, str]:
        _ = worker, run_id
        return "claude-code", self.stdout


class NativeUsageRuntime(StubRuntime):
    def provider_activity_log(self, worker: dict, run_id: str) -> tuple[str, str]:
        _ = worker, run_id
        return (
            "codex-cli",
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {"type": "agent_message", "text": "Native answer."},
                        }
                    ),
                    json.dumps(
                        {
                            "type": "turn.completed",
                            "usage": {"input_tokens": 17, "output_tokens": 4},
                        }
                    ),
                ]
            ),
        )


class AgentBuilderTransferRuntime(StubRuntime):
    def __init__(self, tool_name: str = "lc_transfer_to_specialist"):
        super().__init__()
        self.tool_name = tool_name

    def run_task(
        self,
        worker: dict,
        instruction: str,
        timeout_sec: float | None = None,
        run_id: str | None = None,
    ) -> str:
        _ = worker, instruction, timeout_sec, run_id
        return json.dumps(
            {
                "type": "tool_call",
                "content": "",
                "tool_name": self.tool_name,
            },
            separators=(",", ":"),
        )


class AgentBuilderControlOutputRuntime(StubRuntime):
    def __init__(self, output: str):
        super().__init__()
        self.output = output

    def run_task(
        self,
        worker: dict,
        instruction: str,
        timeout_sec: float | None = None,
        run_id: str | None = None,
    ) -> str:
        _ = worker, instruction, timeout_sec, run_id
        return self.output


class CitationControlOutputRuntime(AgentBuilderControlOutputRuntime):
    def provider_citation_sources(self, worker: dict, run_id: str) -> list[dict[str, str]]:
        _ = worker, run_id
        return [
            {
                "ref_id": "turn0search0",
                "title": "Primary source",
                "url": "https://example.invalid/primary",
            }
        ]


class InterimAgentBuilderControlOutputRuntime(AgentBuilderControlOutputRuntime):
    def provider_activity_log(self, worker: dict, run_id: str) -> tuple[str, str]:
        _ = worker, run_id
        return (
            "codex-cli",
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {
                                "type": "agent_message",
                                "text": json.dumps(
                                    {
                                        "type": "assistant_response",
                                        "content": "I will inspect the available evidence first.",
                                        "tool_name": None,
                                    }
                                ),
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {"type": "command_execution", "status": "completed"},
                        }
                    ),
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {"type": "agent_message", "text": self.output},
                        }
                    ),
                ]
            ),
        )


class GraphRoundTripRuntime(StubRuntime):
    def __init__(self):
        super().__init__()
        self.worker_calls: dict[str, int] = {}

    def run_task(
        self,
        worker: dict,
        instruction: str,
        timeout_sec: float | None = None,
        run_id: str | None = None,
    ) -> str:
        _ = instruction, timeout_sec, run_id
        worker_id = str(worker["worker_id"])
        prior_calls = self.worker_calls.get(worker_id, 0)
        self.worker_calls[worker_id] = prior_calls + 1
        if prior_calls > 0:
            bundle = json.loads(worker["bootstrap_bundle_json"])
            if not bundle.get("agent_builder_control"):
                return "Main final after specialist return."
            return json.dumps(
                {
                    "type": "assistant_response",
                    "content": "Main final after specialist return.",
                    "tool_name": None,
                },
                separators=(",", ":"),
            )
        bundle = json.loads(worker["bootstrap_bundle_json"])
        tool_name = bundle["agent_builder_control"]["tools"][0]["name"]
        return json.dumps(
            {
                "type": "tool_call",
                "content": (
                    "Specialist evidence returned through shared graph state."
                    if tool_name == "lc_transfer_to_main"
                    else ""
                ),
                "tool_name": tool_name,
            },
            separators=(",", ":"),
        )


class AvailableGraphTransferRuntime(StubRuntime):
    def run_task(
        self,
        worker: dict,
        instruction: str,
        timeout_sec: float | None = None,
        run_id: str | None = None,
    ) -> str:
        _ = instruction, timeout_sec, run_id
        bundle = json.loads(worker["bootstrap_bundle_json"])
        control = bundle.get("agent_builder_control")
        if not control:
            return "Main final after each available consultant ran once."
        return json.dumps(
            {
                "type": "tool_call",
                "content": "",
                "tool_name": control["tools"][0]["name"],
            },
            separators=(",", ":"),
        )


class BlockingGraphTransferRuntime(AgentBuilderTransferRuntime):
    def __init__(self):
        super().__init__()
        self.started = Event()
        self.release = Event()
        self.interrupted = Event()

    def run_task(
        self,
        worker: dict,
        instruction: str,
        timeout_sec: float | None = None,
        run_id: str | None = None,
    ) -> str:
        self.started.set()
        if not self.release.wait(timeout=5):
            raise RuntimeError("Synthetic graph transfer was not released")
        return super().run_task(worker, instruction, timeout_sec, run_id)

    def interrupt_worker(self, worker: dict, run_id: str | None = None):
        self.interrupted.set()
        self.release.set()
        return super().interrupt_worker(worker, run_id=run_id)


class SequentialBlockingRuntime(StubRuntime):
    def __init__(self):
        super().__init__()
        self.started = Event()
        self.release = Event()
        self.instructions: list[str] = []

    def run_task(
        self,
        worker: dict,
        instruction: str,
        timeout_sec: float | None = None,
        run_id: str | None = None,
    ) -> str:
        self.instructions.append(instruction)
        self.started.set()
        if not self.release.wait(timeout=5):
            raise RuntimeError("Synthetic active turn was not released")
        return "First foreground turn completed."


class DeadlineLateGraphTransferRuntime(AgentBuilderTransferRuntime):
    """Return a late transfer even after termination to prove the provider fence wins."""

    def __init__(self):
        super().__init__()
        self.started = Event()
        self.release = Event()
        self.interrupted = Event()
        self.received_timeouts: list[float | None] = []

    def run_task(
        self,
        worker: dict,
        instruction: str,
        timeout_sec: float | None = None,
        run_id: str | None = None,
    ) -> str:
        self.received_timeouts.append(timeout_sec)
        self.started.set()
        if not self.release.wait(timeout=5):
            raise RuntimeError("Synthetic foreground consult was not terminated")
        return super().run_task(worker, instruction, timeout_sec, run_id)

    def interrupt_worker(self, worker: dict, run_id: str | None = None):
        self.interrupted.set()
        self.release.set()
        return super().interrupt_worker(worker, run_id=run_id)


class DeadlineInterruptedRuntime(DeadlineLateGraphTransferRuntime):
    def run_task(
        self,
        worker: dict,
        instruction: str,
        timeout_sec: float | None = None,
        run_id: str | None = None,
    ) -> str:
        self.received_timeouts.append(timeout_sec)
        self.started.set()
        if not self.release.wait(timeout=5):
            raise RuntimeError("Synthetic foreground consult was not interrupted")
        raise WorkerInterruptedError("Synthetic native interrupt completed")


class JustLateCompletionRuntime(StubRuntime):
    def __init__(self, delay_s: float = 0.03):
        super().__init__()
        self.delay_s = delay_s
        self.started = Event()
        self.received_timeouts: list[float | None] = []

    def run_task(
        self,
        worker: dict,
        instruction: str,
        timeout_sec: float | None = None,
        run_id: str | None = None,
    ) -> str:
        self.received_timeouts.append(timeout_sec)
        self.started.set()
        time.sleep(self.delay_s)
        return json.dumps(
            {
                "type": "agent_builder_transfer",
                "target": "specialist",
                "content": "Late specialist transfer.",
            }
        )


class SlowDeadlineCleanupRuntime(DeadlineLateGraphTransferRuntime):
    def __init__(self):
        super().__init__()
        self.cleanup_started = Event()
        self.finish_cleanup = Event()

    def interrupt_worker(self, worker: dict, run_id: str | None = None):
        self.cleanup_started.set()
        if not self.finish_cleanup.wait(timeout=5):
            raise RuntimeError("Synthetic deadline cleanup was not released")
        return super().interrupt_worker(worker, run_id=run_id)


class CompactedActivityRuntime(StubRuntime):
    def provider_activity_log(self, worker: dict, run_id: str) -> tuple[str, str]:
        _ = worker, run_id
        return (
            "codex-cli",
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "glasshive.log_compacted",
                            "excluded_prefix_bytes": 2048,
                        }
                    ),
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {"type": "agent_message", "text": "Tail answer."},
                        }
                    ),
                ]
            ),
        )


def test_models_expose_exact_harness_registry(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    response = client.get("/v1/models", headers=AUTH)

    assert response.status_code == 200
    models = {item["id"]: item for item in response.json()["data"]}
    assert set(models) == {"codex-cli:gpt-5.6-sol", "claude-code:opus"}
    assert models["codex-cli:gpt-5.6-sol"]["display_name"] == "Codex / GPT-5.6 Sol"
    assert models["codex-cli:gpt-5.6-sol"]["recommended_effort"] == "medium"
    assert models["codex-cli:gpt-5.6-sol"]["context_window"] == 272000
    assert models["claude-code:opus"]["display_name"] == "Claude / Opus 5"
    assert models["claude-code:opus"]["recommended_effort"] == "high"
    assert models["claude-code:opus"]["effort_choices"] == [
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    ]
    assert all(model["capabilities"]["automatic_fallback_target"] for model in models.values())
    assert all(model["capabilities"]["activity_stream"] for model in models.values())
    assert all(model["capabilities"]["conversation_session"] for model in models.values())
    assert all(model["readiness"]["status"] for model in models.values())


def _failed_quota_request(
    tmp_path: Path,
    *,
    blocked_activity: str = "",
    failure_class: str = "provider_quota_exhausted",
    failure_structured: bool = True,
    runtime: StubRuntime | None = None,
    broker_bearer: str = "",
) -> tuple[Store, WorkersProjectsService, ConversationProvider, dict, dict, dict]:
    store = Store(str(tmp_path / "fallback-runtime.db"))
    service = WorkersProjectsService(store, runtime or StubRuntime(), reconcile_on_startup=False)
    provider = ConversationProvider(store, service)
    workspace = tmp_path / "Life"
    workspace.mkdir(exist_ok=True)
    project = service.create_project(
        "owner-a",
        "Synthetic conversation",
        "Serial fallback regression",
        "codex-cli",
    )
    worker = service.create_worker(
        project_id=project["project_id"],
        owner_id="owner-a",
        name="Synthetic conversation worker",
        role="conversation-agent",
        profile="codex-cli",
        backend="",
        execution_mode="host",
        workspace_root=str(workspace),
        bootstrap_profile="viventium-conversation-v1",
        bootstrap_bundle={
            "run_mode": "conversation",
            "provider_model": "gpt-5.6-sol",
            "developer_instructions": "Synthetic stable authority.",
            "env": {"WPR_CODEX_CLI_REASONING_EFFORT": "medium"},
            **(
                {
                    "glasshive_capability_broker": {
                        "authority_kind": "conversation_orchestrator",
                        "allowed_host_tools": ["active_work_action"],
                    },
                    "provider_capabilities": {
                        "host_tools_transport": "broker_mcp",
                        "host_tools": ["active_work_action"],
                    },
                }
                if broker_bearer
                else {}
            ),
        },
        start_synchronously=True,
    )
    session = store.upsert_provider_session(
        tenant_id="local",
        owner_id="owner-a",
        conversation_id="conv-a",
        agent_id="agent-a",
        model_id="codex-cli:gpt-5.6-sol",
        project_id=project["project_id"],
        worker_id=worker["worker_id"],
        workspace_dir=str(workspace),
        access_mode="full",
        history_count=4,
        context_manifest={"messages": 4, "effort": "medium"},
    )
    request, _ = store.create_provider_request(
        tenant_id="local",
        owner_id="owner-a",
        session_id=session["session_id"],
        idempotency_key="main:agent-a:message-a",
        message_id="message-a",
        stream_id="stream-a",
        requested_history_count=5,
        fallback_model_id="claude-code:opus",
        fallback_reasoning_effort="max",
        fallback_instruction=(
            "User: First visible request.\n\nAssistant: Earlier visible answer.\n\n"
            "User: Current visible request."
        ),
    )
    run = store.create_run(
        worker["worker_id"],
        project["project_id"],
        "User: Current visible request.",
        state="running",
    )
    run = store.finalize_run(
        run["run_id"],
        state="failed",
        failure_class=failure_class,
        failure_retryable=1,
        failure_structured=failure_structured,
        failure_user_message="The selected model quota was exhausted before authoring began.",
        failure_recommended_recovery="Use the configured fallback model.",
        failure_diagnostic_summary="Synthetic quota admission rejection.",
    )
    request = store.update_provider_request(
        request["request_id"],
        run_id=run["run_id"],
        state="running",
    )
    store.add_provider_activity(request["request_id"], "queued", ACTIVITY_SUMMARIES["queued"])
    store.add_provider_activity(request["request_id"], "started", ACTIVITY_SUMMARIES["started"])
    if blocked_activity:
        store.add_provider_activity(
            request["request_id"],
            blocked_activity,
            ACTIVITY_SUMMARIES[blocked_activity],
        )
    if broker_bearer:
        provider._remember_request_local_bundle(
            str(request["request_id"]),
            str(run["run_id"]),
            {
                "glasshive_capability_broker": {
                    "authority_kind": "conversation_orchestrator",
                    "allowed_host_tools": ["active_work_action"],
                },
                "env": {"GLASSHIVE_CAPABILITY_BROKER_TOKEN": broker_bearer},
            },
        )
    return store, service, provider, request, worker, session


def test_quota_admission_failure_switches_once_to_configured_claude_model(tmp_path):
    store, service, provider, request, old_worker, old_session = _failed_quota_request(tmp_path)
    try:
        switched = provider._sync(request)

        assert switched["request_id"] == request["request_id"]
        assert switched["idempotency_key"] == request["idempotency_key"]
        assert switched["session_id"] == old_session["session_id"]
        assert switched["run_id"] != request["run_id"]
        assert switched["fallback_state"] == "started"
        assert store.get_worker(old_worker["worker_id"])["state"] == "terminated"
        current_session = store.get_provider_session_by_id(old_session["session_id"])
        assert current_session["model_id"] == "claude-code:opus"
        assert current_session["worker_id"] != old_worker["worker_id"]
        fallback_run = store.get_run(switched["run_id"])
        assert "First visible request" in fallback_run["instruction"]
        assert "Earlier visible answer" in fallback_run["instruction"]
        assert "Current visible request" in fallback_run["instruction"]
        activities = store.list_provider_activity(request["request_id"])
        assert [item["event_type"] for item in activities].count("fallback") == 1
    finally:
        service.shutdown()


def test_structured_rate_limit_admission_failure_switches_to_configured_fallback(tmp_path):
    _store, service, provider, request, _, _ = _failed_quota_request(
        tmp_path,
        failure_class="provider_rate_limited",
        failure_structured=True,
    )
    try:
        switched = provider._sync(request)

        assert switched["fallback_state"] == "started"
        assert switched["run_id"] != request["run_id"]
    finally:
        service.shutdown()


def test_serial_fallback_reattaches_invocation_local_broker_bearer_without_persistence(
    tmp_path,
):
    runtime = BrokerBundleCaptureRuntime()
    bearer = "synthetic-primary-fallback-invocation-bearer"
    store, service, provider, request, old_worker, _ = _failed_quota_request(
        tmp_path,
        runtime=runtime,
        broker_bearer=bearer,
    )
    try:
        switched = provider._sync(request)

        assert switched["fallback_state"] == "started"
        deadline = time.time() + 2
        while time.time() < deadline and not runtime.run_bundles:
            time.sleep(0.01)
        assert runtime.run_bundles
        fallback_bundle = runtime.run_bundles[-1]
        assert fallback_bundle["provider_model"] == "opus"
        assert fallback_bundle["env"]["WPR_CLAUDE_CODE_EFFORT"] == "max"
        assert fallback_bundle["env"]["GLASSHIVE_CAPABILITY_BROKER_TOKEN"] == bearer
        assert fallback_bundle["provider_capabilities"]["host_tools_transport"] == "broker_mcp"

        database = (tmp_path / "fallback-runtime.db").read_bytes().decode(
            "utf-8", errors="ignore"
        )
        assert bearer not in database
        current_session = store.list_provider_sessions(owner_id="owner-a")[0]
        for worker in (
            store.get_worker(str(current_session["worker_id"])),
            store.get_worker(str(old_worker["worker_id"])),
        ):
            if not worker:
                continue
            assert bearer not in str(worker.get("bootstrap_bundle_json") or "")
    finally:
        service.shutdown()


def test_restarted_serial_fallback_without_transient_bearer_fails_needs_input(
    tmp_path,
):
    runtime = BrokerBundleCaptureRuntime()
    bearer = "synthetic-bearer-lost-during-restart"
    store, service, provider, request, old_worker, _ = _failed_quota_request(
        tmp_path,
        runtime=runtime,
        broker_bearer=bearer,
    )
    restarted_provider = ConversationProvider(store, service)
    try:
        terminal = restarted_provider._sync(request)

        assert terminal["state"] == "failed"
        assert terminal["fallback_state"] == "needs_input"
        primary = store.get_run(str(request["run_id"]))
        assert primary["failure_class"] == "conversation_capability_grant_required"
        assert "authorization" in primary["failure_user_message"].lower()
        assert store.get_worker(str(old_worker["worker_id"]))["state"] != "terminated"
        assert runtime.run_bundles == []
        assert bearer not in (tmp_path / "fallback-runtime.db").read_bytes().decode(
            "utf-8", errors="ignore"
        )
    finally:
        service.shutdown()


def test_unstructured_rate_limit_diagnostic_does_not_switch_models(tmp_path):
    store, service, provider, request, old_worker, _ = _failed_quota_request(
        tmp_path,
        failure_class="provider_rate_limited",
        failure_structured=False,
    )
    try:
        terminal = provider._sync(request)

        assert terminal["state"] == "failed"
        assert terminal["fallback_state"] == ""
        assert store.get_worker(old_worker["worker_id"])["state"] != "terminated"
        assert all(
            item["event_type"] != "fallback"
            for item in store.list_provider_activity(request["request_id"])
        )
    finally:
        service.shutdown()


def test_concurrent_quota_observers_start_exactly_one_fallback(tmp_path):
    store, service, first_provider, request, _, _ = _failed_quota_request(tmp_path)
    second_provider = ConversationProvider(store, service)
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(
                executor.map(
                    lambda provider: provider._sync(request),
                    (first_provider, second_provider),
                )
            )

        current = store.get_provider_request(request["request_id"])
        assert current["fallback_state"] in {"started", "completed"}
        assert all(result["request_id"] == request["request_id"] for result in results)
        activities = store.list_provider_activity(request["request_id"])
        assert [item["event_type"] for item in activities].count("fallback") == 1
        sessions = store.list_provider_sessions(owner_id="owner-a")
        assert len(sessions) == 1
        assert sessions[0]["model_id"] == "claude-code:opus"
    finally:
        service.shutdown()


def test_serial_fallback_setup_does_not_hold_provider_start_cancel_lock(
    tmp_path,
    monkeypatch,
):
    store, service, provider, request, _, _ = _failed_quota_request(tmp_path)
    primary_before = store.get_run(request["run_id"])
    setup_started = Event()
    finish_setup = Event()

    def slow_create_worker(*args, **kwargs):
        _ = args, kwargs
        setup_started.set()
        if not finish_setup.wait(timeout=5):
            raise RuntimeError("Synthetic fallback setup was not released")
        raise RuntimeError("Synthetic fallback setup failed after concurrent Stop")

    monkeypatch.setattr(service, "create_worker", slow_create_worker)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            fallback = executor.submit(provider._sync, request)
            assert setup_started.wait(timeout=2)
            started_at = time.monotonic()
            cancelled = provider.cancel(request["request_id"])
            lock_wait = time.monotonic() - started_at
            finish_setup.set()
            fallback_result = fallback.result(timeout=2)

        assert lock_wait < 0.5
        assert cancelled["state"] == "cancelled"
        assert fallback_result["state"] == "cancelled"
        assert store.get_provider_request(request["request_id"])["state"] == "cancelled"
        primary_after = store.get_run(request["run_id"])
        assert (
            primary_after["failure_user_message"]
            == primary_before["failure_user_message"]
        )
        assert not any(
            item["event_type"] in {"fallback", "failed"}
            for item in store.list_provider_activity(request["request_id"])
        )
    finally:
        finish_setup.set()
        service.shutdown()


def test_fallback_setup_failure_cannot_overwrite_concurrent_deadline(
    tmp_path,
    monkeypatch,
):
    store, service, provider, request, _, _ = _failed_quota_request(tmp_path)
    request = store.update_provider_request(
        request["request_id"],
        response_timeout_s=0.01,
        response_deadline_at=(
            datetime.now(timezone.utc) - timedelta(seconds=0.1)
        ).isoformat(),
    )
    setup_started = Event()
    finish_setup = Event()

    def failing_create_worker(*args, **kwargs):
        _ = args, kwargs
        setup_started.set()
        if not finish_setup.wait(timeout=5):
            raise RuntimeError("Synthetic fallback setup was not released")
        raise RuntimeError("Synthetic fallback setup failed after deadline")

    monkeypatch.setattr(service, "create_worker", failing_create_worker)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            fallback = executor.submit(provider._sync, request)
            assert setup_started.wait(timeout=2)
            deadline_request, deadline_run = provider._expire_response_deadline(
                store.get_provider_request(request["request_id"])
            )
            finish_setup.set()
            fallback_result = fallback.result(timeout=2)

        assert deadline_request["state"] == "failed"
        assert deadline_request["fallback_state"] == "deadline_exceeded"
        assert deadline_run["failure_class"] == "provider_response_deadline_exceeded"
        assert fallback_result["fallback_state"] == "deadline_exceeded"
        primary_after = store.get_run(request["run_id"])
        assert primary_after["failure_class"] == "provider_response_deadline_exceeded"
        assert "deadline" in primary_after["failure_user_message"].lower()
        activities = store.list_provider_activity(request["request_id"])
        assert [item["event_type"] for item in activities].count("failed") == 1
        assert not any(item["event_type"] == "fallback" for item in activities)
    finally:
        finish_setup.set()
        service.shutdown()


def test_cancel_claim_wins_before_quota_fallback_sync(tmp_path):
    store, service, provider, request, old_worker, _ = _failed_quota_request(tmp_path)
    try:
        cancelled = provider.cancel(request["request_id"])
        after_sync = provider._sync(request)

        assert cancelled["state"] == "cancelled"
        assert cancelled["fallback_state"] == "cancelled"
        assert after_sync["state"] == "cancelled"
        assert store.get_worker(old_worker["worker_id"])["state"] != "terminated"
        activities = store.list_provider_activity(request["request_id"])
        assert [item["event_type"] for item in activities].count("fallback") == 0
        assert [item["event_type"] for item in activities].count("cancelled") == 1
    finally:
        service.shutdown()


def test_stop_atomically_cancels_exact_busy_worker_queued_run_before_execution(
    tmp_path,
    monkeypatch,
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    runtime = SequentialBlockingRuntime()
    client = _client(tmp_path, monkeypatch, runtime=runtime)
    first_payload = _payload(workspace)
    second_payload = json.loads(json.dumps(first_payload))
    second_payload["metadata"].update(
        {
            "idempotency_key": "idem-b",
            "message_id": "message-b",
            "stream_id": "stream-b",
        }
    )
    second_payload["messages"].append(
        {"role": "user", "content": "This queued turn must never execute."}
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            client.post,
            "/v1/chat/completions",
            headers=AUTH,
            json=first_payload,
        )
        assert runtime.started.wait(timeout=2)
        second = executor.submit(
            client.post,
            "/v1/chat/completions",
            headers=AUTH,
            json=second_payload,
        )
        store = client.app.state.store
        deadline = time.monotonic() + 2
        queued_request = None
        while time.monotonic() < deadline:
            family = store.list_provider_requests_by_idempotency_family(
                tenant_id="local",
                owner_id="owner-a",
                base_idempotency_key="idem-b",
            )
            if family and family[0].get("run_id"):
                queued_request = family[0]
                break
            time.sleep(0.01)
        assert queued_request is not None
        queued_run = store.get_run(queued_request["run_id"])
        assert queued_run["state"] == "queued"

        cancelled = client.post(
            f"/v1/requests/{queued_request['request_id']}/cancel",
            headers=AUTH,
        )
        runtime.release.set()
        first_response = first.result(timeout=2)
        second_response = second.result(timeout=2)

    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["state"] == "cancelled"
    assert first_response.status_code == 200, first_response.text
    assert second_response.status_code == 502, second_response.text
    assert store.get_run(queued_run["run_id"])["state"] == "cancelled"
    assert len(runtime.instructions) == 1


def test_stop_atomically_cancels_queued_fallback_run_and_schedule(
    tmp_path,
    monkeypatch,
):
    store, service, provider, request, _, _ = _failed_quota_request(tmp_path)
    monkeypatch.setattr(service, "_ensure_worker_processor", lambda worker_id: None)
    try:
        switched = provider._sync(request)
        fallback_run = store.get_run(switched["run_id"])
        assert fallback_run["state"] == "queued"
        schedule = store.create_scheduled_run(
            project_id=fallback_run["project_id"],
            worker_id=fallback_run["worker_id"],
            owner_id="owner-a",
            instruction="Synthetic queued fallback schedule.",
            run_at=datetime.now(timezone.utc).isoformat(),
        )
        store.finalize_schedule(
            schedule["schedule_id"],
            state="queued",
            queued_run_id=fallback_run["run_id"],
        )

        cancelled = provider.cancel(request["request_id"])

        assert cancelled["state"] == "cancelled"
        assert store.get_run(fallback_run["run_id"])["state"] == "cancelled"
        assert store.get_schedule(schedule["schedule_id"])["state"] == "cancelled"
    finally:
        service.shutdown()


def test_cross_process_stop_cannot_be_overwritten_by_stale_terminal_sync(
    tmp_path,
    monkeypatch,
):
    store, service, provider, request, _, _ = _failed_quota_request(tmp_path)
    request = store.update_provider_request(
        request["request_id"],
        fallback_model_id="",
    )
    original_sync_native = provider._sync_native_activity

    def stop_between_read_and_terminal_cas(request_record, run):
        store.claim_provider_request_cancel(request_record["request_id"])
        return original_sync_native(request_record, run)

    monkeypatch.setattr(provider, "_sync_native_activity", stop_between_read_and_terminal_cas)
    try:
        terminal = provider._sync(request)

        assert terminal["state"] == "cancelled"
        assert store.get_provider_request(request["request_id"])["state"] == "cancelled"
        assert not any(
            item["event_type"] in {"completed", "failed"}
            for item in store.list_provider_activity(request["request_id"])
        )
    finally:
        service.shutdown()


def test_involuntary_interruption_surfaces_as_recoverable_provider_failure(tmp_path):
    store, service, provider, request, _, _ = _failed_quota_request(
        tmp_path,
        failure_class="provider_temporarily_unavailable",
    )
    try:
        store.update_run(
            request["run_id"],
            state="interrupted",
            failure_class="provider_temporarily_unavailable",
            failure_retryable=1,
            failure_structured=1,
            failure_user_message="The provider worker stopped unexpectedly before completing.",
        )

        terminal = provider._sync(request)

        assert terminal["state"] == "failed"
        assert terminal["fallback_state"] == ""
        activities = store.list_provider_activity(request["request_id"])
        assert any(item["event_type"] == "failed" for item in activities)
        assert not any(item["event_type"] == "cancelled" for item in activities)
    finally:
        service.shutdown()


def test_stale_quota_fallback_claim_fails_honestly_instead_of_hanging(
    tmp_path, monkeypatch
):
    store, service, provider, request, _, _ = _failed_quota_request(tmp_path)
    try:
        claimed = store.claim_provider_request_fallback(
            request["request_id"],
            expected_run_id=request["run_id"],
        )
        assert claimed is not None and claimed["fallback_state"] == "claimed"
        monkeypatch.setattr(
            "workers_projects_runtime.conversation_provider.SERIAL_FALLBACK_CLAIM_TIMEOUT_SEC",
            -1,
        )

        terminal = provider._sync(request)

        assert terminal["state"] == "failed"
        assert terminal["fallback_state"] == "failed"
        primary_run = store.get_run(request["run_id"])
        assert "fallback" in primary_run["failure_user_message"].lower()
        activities = store.list_provider_activity(request["request_id"])
        assert [item["event_type"] for item in activities].count("failed") == 1
    finally:
        service.shutdown()


def test_quota_fallback_stream_stays_open_until_claude_result(tmp_path):
    store, service, provider, request, _, session = _failed_quota_request(tmp_path)

    class ConnectedRequest:
        async def is_disconnected(self):
            return False

    payload = ChatCompletionRequest(
        model="codex-cli:gpt-5.6-sol",
        messages=[ChatMessage(role="user", content="Current visible request.")],
        stream=True,
    )

    async def collect() -> list[str]:
        return [line async for line in provider.stream(request, payload, ConnectedRequest())]

    try:
        lines = asyncio.run(collect())
        chunks = [
            json.loads(line.removeprefix("data: "))
            for line in lines
            if line.startswith("data: {")
        ]

        assert not any("error" in chunk for chunk in chunks)
        assert not any(
            chunk["choices"][0]["delta"].get("reasoning_content")
            for chunk in chunks
            if chunk.get("choices")
        )
        assert any(
            "Current visible request" in chunk["choices"][0]["delta"].get("content", "")
            for chunk in chunks
            if chunk.get("choices")
        )
        assert lines[-1] == "data: [DONE]\n\n"
        current = store.get_provider_request(request["request_id"])
        assert current["state"] == "completed"
        assert current["session_id"] == session["session_id"]
        activities = store.list_provider_activity(request["request_id"])
        assert [item["event_type"] for item in activities].count("fallback") == 1
    finally:
        service.shutdown()


@pytest.mark.parametrize("blocked_activity", ["reasoning-summary", "plan", "tool", "file"])
def test_quota_failure_does_not_switch_after_native_authoring_activity(
    tmp_path, blocked_activity
):
    store, service, provider, request, old_worker, _ = _failed_quota_request(
        tmp_path,
        blocked_activity=blocked_activity,
    )
    try:
        terminal = provider._sync(request)

        assert terminal["state"] == "failed"
        assert terminal["run_id"] == request["run_id"]
        assert store.get_worker(old_worker["worker_id"])["state"] != "terminated"
        assert all(
            item["event_type"] != "fallback"
            for item in store.list_provider_activity(request["request_id"])
        )
    finally:
        service.shutdown()


def test_readiness_does_not_treat_placeholder_provider_tokens_as_authentication(
    tmp_path, monkeypatch
):
    fake_binary = tmp_path / "harness"
    fake_binary.write_text("#!/bin/sh\nexit 1\n")
    fake_binary.chmod(0o755)
    monkeypatch.setenv("WPR_CODEX_BIN", str(fake_binary))
    monkeypatch.setenv("WPR_CLAUDE_CODE_BIN", str(fake_binary))
    monkeypatch.setenv("OPENAI_API_KEY", "user_provided")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "${CLAUDE_CODE_OAUTH_TOKEN}")
    monkeypatch.setattr(
        "workers_projects_runtime.conversation_provider.subprocess.run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 1),
    )

    assert _harness_auth_configured("codex-cli") is False
    assert _harness_auth_configured("claude-code") is False


def test_claude_readiness_rejects_expired_keychain_when_status_is_not_logged_in(
    tmp_path, monkeypatch
):
    fake_binary = tmp_path / "claude"
    fake_binary.write_text("#!/bin/sh\nexit 0\n")
    fake_binary.chmod(0o755)
    monkeypatch.setenv("WPR_CLAUDE_CODE_BIN", str(fake_binary))
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_REFRESH_TOKEN", raising=False)
    monkeypatch.setattr("workers_projects_runtime.profile_runtime.sys.platform", "darwin")
    real_which = shutil.which

    def fake_which(binary):
        if binary == "security":
            return "/usr/bin/security"
        return real_which(binary)

    monkeypatch.setattr("workers_projects_runtime.profile_runtime.shutil.which", fake_which)

    def fake_run(command, **_kwargs):
        if command[0] == "security":
            return subprocess.CompletedProcess(
                command,
                returncode=0,
                stdout=json.dumps(
                    {
                        "claudeAiOauth": {
                            "accessToken": "synthetic-expired-access",
                            "expiresAt": int((time.time() - 60) * 1000),
                        }
                    }
                ),
                stderr="",
            )
        assert command == [str(fake_binary), "auth", "status"]
        return subprocess.CompletedProcess(
            command,
            returncode=0,
            stdout=json.dumps({"loggedIn": False}),
            stderr="",
        )

    monkeypatch.setattr("workers_projects_runtime.profile_runtime.subprocess.run", fake_run)

    assert _harness_auth_configured("claude-code") is False


def test_non_streaming_completion_reuses_one_native_session_and_idempotency(tmp_path, monkeypatch):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    payload = _payload(workspace)

    first = client.post("/v1/chat/completions", headers=AUTH, json=payload)
    duplicate = client.post("/v1/chat/completions", headers=AUTH, json=payload)

    assert first.status_code == 200, first.text
    assert duplicate.status_code == 200, duplicate.text
    assert duplicate.json()["id"] == first.json()["id"]
    content = first.json()["choices"][0]["message"]["content"]
    assert "Hello from LIFE." in content
    assert "FINAL REPORT" not in content
    store = client.app.state.store
    sessions = store.list_provider_sessions(owner_id="owner-a")
    assert len(sessions) == 1
    assert sessions[0]["model_id"] == "codex-cli:gpt-5.6-sol"
    worker = store.get_worker(sessions[0]["worker_id"])
    assert worker is not None
    assert worker["workspace_dir"] == str(workspace.resolve())
    assert json.loads(worker["bootstrap_bundle_json"])["run_mode"] == "conversation"
    assert len(store.list_runs_for_worker(worker["worker_id"])) == 1


def test_zero_input_agent_builder_transfer_returns_openai_tool_call_once(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    runtime = AgentBuilderTransferRuntime()
    client = _client(tmp_path, monkeypatch, runtime=runtime)
    payload = _payload(workspace)
    payload["tools"] = [
        {
            "type": "function",
            "function": {
                "name": "lc_transfer_to_specialist",
                "description": "Consult the specialist using shared graph state.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "ordinary_host_tool",
                "description": "Must remain behind the signed capability broker.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
    ]

    first = client.post("/v1/chat/completions", headers=AUTH, json=payload)
    duplicate = client.post("/v1/chat/completions", headers=AUTH, json=payload)

    assert first.status_code == 200, first.text
    assert duplicate.status_code == 200, duplicate.text
    assert duplicate.json() == first.json()
    choice = first.json()["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] is None
    assert choice["message"]["tool_calls"] == [
        {
            "id": choice["message"]["tool_calls"][0]["id"],
            "type": "function",
            "function": {
                "name": "lc_transfer_to_specialist",
                "arguments": "{}",
            },
        }
    ]
    store = client.app.state.store
    sessions = store.list_provider_sessions(owner_id="owner-a")
    worker = store.get_worker(sessions[0]["worker_id"])
    assert worker is not None
    bundle = json.loads(worker["bootstrap_bundle_json"])
    control = bundle["agent_builder_control"]
    assert [tool["name"] for tool in control["tools"]] == [
        "lc_transfer_to_specialist"
    ]
    assert bundle["provider_capabilities"]["graph_control_tools"] == [
        "lc_transfer_to_specialist"
    ]
    assert bundle["provider_capabilities"]["host_tools"] == []
    assert len(store.list_runs_for_worker(worker["worker_id"])) == 1


def test_graph_control_schema_does_not_ask_main_to_recap_when_starting_consult():
    schema = graph_transfer_output_schema(
        {
            "version": 1,
            "tools": [
                {
                    "name": "lc_transfer_to_specialist",
                    "description": "Consult the specialist using shared graph state.",
                }
            ],
        }
    )

    assert schema is not None
    description = schema["description"].lower()
    assert "when starting a consult, use empty content" in description
    assert "only when returning completed specialist work" in description
    assert "the shared graph already carries the request and context" in description


def test_reentered_graph_agent_gets_new_history_scoped_attempt_and_stable_retry(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(
        tmp_path,
        monkeypatch,
        runtime=AgentBuilderTransferRuntime(),
    )
    first_payload = _payload(workspace)
    first_payload["tools"] = [
        {
            "type": "function",
            "function": {
                "name": "lc_transfer_to_specialist",
                "description": "Consult the specialist using shared graph state.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }
    ]

    first = client.post("/v1/chat/completions", headers=AUTH, json=first_payload)
    first_retry = client.post(
        "/v1/chat/completions", headers=AUTH, json=first_payload
    )
    reentry_payload = json.loads(json.dumps(first_payload))
    reentry_payload["messages"].append(
        {
            "role": "assistant",
            "content": "Specialist evidence returned through shared graph state.",
        }
    )
    reentry = client.post(
        "/v1/chat/completions", headers=AUTH, json=reentry_payload
    )
    reentry_retry = client.post(
        "/v1/chat/completions", headers=AUTH, json=reentry_payload
    )

    assert first.status_code == 200, first.text
    assert reentry.status_code == 200, reentry.text
    assert first_retry.json()["id"] == first.json()["id"]
    assert reentry_retry.json()["id"] == reentry.json()["id"]
    assert reentry.json()["id"] != first.json()["id"]
    store = client.app.state.store
    family = store.list_provider_requests_by_idempotency_family(
        tenant_id="local",
        owner_id="owner-a",
        base_idempotency_key="idem-a",
    )
    assert len(family) == 2
    assert len({record["idempotency_key"] for record in family}) == 2
    assert all(":graph:" in record["idempotency_key"] for record in family)
    session = store.list_provider_sessions(owner_id="owner-a")[0]
    assert len(store.list_runs_for_worker(session["worker_id"])) == 2

    cancelled_family = client.post(
        "/v1/requests/by-idempotency/idem-a/cancel",
        headers=AUTH,
    )
    assert cancelled_family.status_code == 200, cancelled_family.text
    third_payload = json.loads(json.dumps(reentry_payload))
    third_payload["messages"].append(
        {"role": "assistant", "content": "One more graph-state update."}
    )
    third = client.post("/v1/chat/completions", headers=AUTH, json=third_payload)
    new_turn_payload = json.loads(json.dumps(third_payload))
    new_turn_payload["metadata"]["idempotency_key"] = "idem-b"
    new_turn = client.post(
        "/v1/chat/completions", headers=AUTH, json=new_turn_payload
    )

    assert third.status_code == 409, third.text
    assert "cancelled before native execution" in third.text
    assert new_turn.status_code == 200, new_turn.text


def test_request_scoped_cancel_does_not_tombstone_a_completed_graph_family(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(
        tmp_path,
        monkeypatch,
        runtime=AgentBuilderTransferRuntime(),
    )
    payload = _payload(workspace)
    payload["tools"] = [
        {
            "type": "function",
            "function": {
                "name": "lc_transfer_to_specialist",
                "description": "Consult the specialist using shared graph state.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }
    ]

    first = client.post("/v1/chat/completions", headers=AUTH, json=payload)
    cancelled = client.post(
        f"/v1/requests/{first.json()['id']}/cancel",
        headers=AUTH,
    )
    reentry_payload = json.loads(json.dumps(payload))
    reentry_payload["messages"].append(
        {"role": "assistant", "content": "New graph state after a completed request."}
    )
    reentry = client.post(
        "/v1/chat/completions", headers=AUTH, json=reentry_payload
    )

    assert first.status_code == 200, first.text
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["state"] == "completed"
    assert reentry.status_code == 200, reentry.text
    with client.app.state.store._connect() as conn:
        tombstone_count = conn.execute(
            "SELECT COUNT(*) FROM provider_stop_tombstones"
        ).fetchone()[0]
    assert tombstone_count == 0


def test_graph_execution_key_covers_effective_model_effort_and_tool_choice(tmp_path):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    raw = _payload(workspace)
    raw["tools"] = [
        {
            "type": "function",
            "function": {
                "name": "lc_transfer_to_specialist",
                "description": "Consult the specialist using shared graph state.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }
    ]

    default_key = _idempotency_key(ChatCompletionRequest.model_validate(raw))
    explicit_auto = json.loads(json.dumps(raw))
    explicit_auto["tool_choice"] = "AUTO"
    high_effort = json.loads(json.dumps(raw))
    high_effort["reasoning_effort"] = "high"
    required_tool = json.loads(json.dumps(raw))
    required_tool["tool_choice"] = "required"
    claude_model = json.loads(json.dumps(raw))
    claude_model["model"] = "claude-code:opus"

    assert _idempotency_key(ChatCompletionRequest.model_validate(explicit_auto)) == default_key
    assert len(
        {
            default_key,
            _idempotency_key(ChatCompletionRequest.model_validate(high_effort)),
            _idempotency_key(ChatCompletionRequest.model_validate(required_tool)),
            _idempotency_key(ChatCompletionRequest.model_validate(claude_model)),
        }
    ) == 4


def test_graph_family_stop_cancels_active_child_and_blocks_late_reentry(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    runtime = BlockingGraphTransferRuntime()
    client = _client(tmp_path, monkeypatch, runtime=runtime)
    payload = _payload(workspace)
    payload["tools"] = [
        {
            "type": "function",
            "function": {
                "name": "lc_transfer_to_specialist",
                "description": "Consult the specialist using shared graph state.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }
    ]

    with ThreadPoolExecutor(max_workers=1) as executor:
        active = executor.submit(
            client.post,
            "/v1/chat/completions",
            headers=AUTH,
            json=payload,
        )
        assert runtime.started.wait(timeout=5)
        cancelled = client.post(
            "/v1/requests/by-idempotency/idem-a/cancel",
            headers=AUTH,
        )
        active.result(timeout=5)

    late_payload = json.loads(json.dumps(payload))
    late_payload["messages"].append(
        {"role": "assistant", "content": "Late specialist graph state."}
    )
    late = client.post("/v1/chat/completions", headers=AUTH, json=late_payload)

    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["state"] == "cancelled"
    assert runtime.interrupted.is_set()
    assert late.status_code == 409, late.text
    assert "cancelled before native execution" in late.text


def test_non_streaming_foreground_deadline_fails_openai_request_and_fences_late_transfer(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    runtime = DeadlineLateGraphTransferRuntime()
    client = _client(tmp_path, monkeypatch, runtime=runtime)
    payload = _payload(workspace)
    response_timeout_s = 0.5
    payload["metadata"]["response_timeout_s"] = response_timeout_s
    payload["tools"] = [
        {
            "type": "function",
            "function": {
                "name": "lc_transfer_to_specialist",
                "description": "Consult the specialist using shared graph state.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }
    ]

    started_at = time.monotonic()
    response = client.post("/v1/chat/completions", headers=AUTH, json=payload)
    elapsed = time.monotonic() - started_at

    assert response.status_code == 504, response.text
    assert 0.3 <= elapsed < 2
    error = response.json()["error"]
    assert error["type"] == "glasshive_timeout_error"
    assert error["code"] == "provider_response_deadline_exceeded"
    assert error["request_id"].startswith("chatcmpl-gh-")
    assert error["timeout_seconds"] == response_timeout_s
    assert runtime.started.wait(timeout=2)
    assert runtime.interrupted.wait(timeout=2)
    assert runtime.received_timeouts == [None]

    store = client.app.state.store
    request = store.get_provider_request(error["request_id"])
    run = store.get_run(request["run_id"])
    worker = store.get_provider_session_by_id(request["session_id"])
    deadline_delta = (
        datetime.fromisoformat(request["response_deadline_at"])
        - datetime.fromisoformat(request["created_at"])
    ).total_seconds()
    assert request["response_timeout_s"] == response_timeout_s
    assert deadline_delta <= response_timeout_s
    assert request["state"] == "failed"
    assert run["state"] == "failed"
    assert run["failure_class"] == "provider_response_deadline_exceeded"
    assert store.get_worker(worker["worker_id"])["state"] != "terminated"
    activities = store.list_provider_activity(request["request_id"])
    assert [item["event_type"] for item in activities].count("failed") == 1
    assert not any(item["event_type"] == "completed" for item in activities)

    retry = client.post("/v1/chat/completions", headers=AUTH, json=payload)
    assert retry.status_code == 504, retry.text
    assert retry.json()["error"]["request_id"] == request["request_id"]
    assert len(
        store.list_provider_requests_by_idempotency_family(
            tenant_id="local",
            owner_id="owner-a",
            base_idempotency_key="idem-a",
        )
    ) == 1


def test_streaming_foreground_deadline_uses_config_and_emits_no_late_tool_call(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    response_timeout_s = 0.5
    monkeypatch.setenv(
        "GLASSHIVE_PROVIDER_RESPONSE_TIMEOUT_S",
        str(response_timeout_s),
    )
    runtime = DeadlineLateGraphTransferRuntime()
    client = _client(tmp_path, monkeypatch, runtime=runtime)
    payload = _payload(workspace, stream=True)
    payload["tools"] = [
        {
            "type": "function",
            "function": {
                "name": "lc_transfer_to_specialist",
                "description": "Consult the specialist using shared graph state.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }
    ]

    started_at = time.monotonic()
    with client.stream(
        "POST", "/v1/chat/completions", headers=AUTH, json=payload
    ) as response:
        assert response.status_code == 200, response.text
        lines = [line for line in response.iter_lines() if line]
    elapsed = time.monotonic() - started_at

    assert 0.3 <= elapsed < 2
    assert lines[-1] == "data: [DONE]"
    chunks = [
        json.loads(line.removeprefix("data: "))
        for line in lines
        if line.startswith("data: {")
    ]
    error_chunks = [chunk for chunk in chunks if chunk.get("error")]
    assert len(error_chunks) == 1
    assert error_chunks[0]["error"]["type"] == "glasshive_timeout_error"
    assert (
        error_chunks[0]["error"]["code"]
        == "provider_response_deadline_exceeded"
    )
    assert error_chunks[0]["error"]["timeout_seconds"] == response_timeout_s
    assert not any(
        chunk.get("choices")
        and chunk["choices"][0]["delta"].get("tool_calls")
        for chunk in chunks
    )
    assert runtime.started.wait(timeout=2)
    assert runtime.interrupted.wait(timeout=2)
    assert runtime.received_timeouts == [None]

    store = client.app.state.store
    request_id = chunks[0]["id"]
    request = store.get_provider_request(request_id)
    run = store.get_run(request["run_id"])
    time.sleep(0.1)
    assert store.get_provider_request(request_id)["state"] == "failed"
    assert store.get_run(run["run_id"])["state"] == "failed"
    assert len(
        store.list_provider_requests_by_idempotency_family(
            tenant_id="local",
            owner_id="owner-a",
            base_idempotency_key="idem-a",
        )
    ) == 1
    assert not any(
        item["event_type"] == "completed"
        for item in store.list_provider_activity(request_id)
    )


@pytest.mark.parametrize("stream", [False, True])
def test_foreground_deadline_beats_native_completion_just_after_deadline(
    tmp_path,
    monkeypatch,
    stream,
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    runtime = JustLateCompletionRuntime(delay_s=0.75)
    client = _client(tmp_path, monkeypatch, runtime=runtime)
    payload = _payload(workspace, stream=stream)
    payload["metadata"]["response_timeout_s"] = 0.5
    payload["tools"] = [
        {
            "type": "function",
            "function": {
                "name": "lc_transfer_to_specialist",
                "description": "Consult the specialist using shared graph state.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }
    ]

    if stream:
        with client.stream(
            "POST", "/v1/chat/completions", headers=AUTH, json=payload
        ) as response:
            assert response.status_code == 200, response.text
            lines = [line for line in response.iter_lines() if line]
        chunks = [
            json.loads(line.removeprefix("data: "))
            for line in lines
            if line.startswith("data: {")
        ]
        request_id = chunks[0]["id"]
        assert any(
            chunk.get("error", {}).get("code")
            == "provider_response_deadline_exceeded"
            for chunk in chunks
        )
        assert not any(
            chunk.get("choices")
            and chunk["choices"][0]["delta"].get("tool_calls")
            for chunk in chunks
        )
    else:
        response = client.post("/v1/chat/completions", headers=AUTH, json=payload)
        assert response.status_code == 504, response.text
        request_id = response.json()["error"]["request_id"]

    store = client.app.state.store
    request_record = store.get_provider_request(request_id)
    run = store.get_run(request_record["run_id"])
    assert request_record["state"] == "failed"
    assert run["state"] == "failed"
    assert run["failure_class"] == "provider_response_deadline_exceeded"
    assert runtime.started.wait(timeout=2)
    assert runtime.received_timeouts == [None]


def test_foreground_polling_avoids_deadline_writer_lock_until_terminal(
    tmp_path,
    monkeypatch,
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    runtime = JustLateCompletionRuntime()
    client = _client(tmp_path, monkeypatch, runtime=runtime)
    payload = _payload(workspace)
    payload["metadata"]["response_timeout_s"] = 1
    store = client.app.state.store
    original_arbitrate = store.arbitrate_provider_request_deadline
    arbitration_calls = 0

    def counted_arbitration(*args, **kwargs):
        nonlocal arbitration_calls
        arbitration_calls += 1
        return original_arbitrate(*args, **kwargs)

    monkeypatch.setattr(
        store,
        "arbitrate_provider_request_deadline",
        counted_arbitration,
    )

    response = client.post("/v1/chat/completions", headers=AUTH, json=payload)

    assert response.status_code == 200, response.text
    assert arbitration_calls <= 2


def test_legacy_active_request_backfills_deadline_from_created_at_on_reconnect(
    tmp_path,
    monkeypatch,
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    monkeypatch.setenv("GLASSHIVE_PROVIDER_RESPONSE_TIMEOUT_S", "100")
    runtime = DeadlineLateGraphTransferRuntime()
    client = _client(tmp_path, monkeypatch, runtime=runtime)
    payload = _payload(workspace)

    class OwnerRequest:
        headers = {"x-viventium-user-id": "owner-a"}

    store = client.app.state.store
    request_record = client.app.state.conversation_provider.start(
        ChatCompletionRequest.model_validate(payload),
        OwnerRequest(),
    )
    assert request_record.get("run_id")
    assert runtime.started.wait(timeout=2)
    created_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    with store._connect() as conn:
        conn.execute(
            """
            UPDATE provider_requests
            SET response_timeout_s = NULL,
                response_deadline_at = '',
                created_at = ?
            WHERE request_id = ?
            """,
            (created_at, request_record["request_id"]),
        )
    monkeypatch.setenv("GLASSHIVE_PROVIDER_RESPONSE_TIMEOUT_S", "0.05")
    restarted_provider = ConversationProvider(
        store,
        client.app.state.service,
    )
    terminal_request, terminal_run = restarted_provider.wait(
        request_record["request_id"]
    )

    assert terminal_request["state"] == "failed"
    assert terminal_run["state"] == "failed"
    assert (
        restarted_provider.deadline_error_payload(terminal_request, terminal_run)[
            "error"
        ]["code"]
        == "provider_response_deadline_exceeded"
    )
    migrated = store.get_provider_request(request_record["request_id"])
    assert migrated["response_timeout_s"] == 0.05
    assert (
        datetime.fromisoformat(migrated["response_deadline_at"])
        - datetime.fromisoformat(migrated["created_at"])
    ).total_seconds() == pytest.approx(0.05)


def test_foreground_deadline_survives_native_worker_interrupted_exception(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    runtime = DeadlineInterruptedRuntime()
    client = _client(tmp_path, monkeypatch, runtime=runtime)
    payload = _payload(workspace)
    payload["metadata"]["response_timeout_s"] = 0.5
    payload["tools"] = [
        {
            "type": "function",
            "function": {
                "name": "lc_transfer_to_specialist",
                "description": "Consult the specialist using shared graph state.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }
    ]

    response = client.post("/v1/chat/completions", headers=AUTH, json=payload)

    assert response.status_code == 504, response.text
    store = client.app.state.store
    request_id = response.json()["error"]["request_id"]
    assert runtime.started.wait(timeout=2)
    deadline = time.monotonic() + 2
    request = None
    run = None
    while time.monotonic() < deadline:
        request = store.get_provider_request(request_id)
        if request is not None and request["run_id"]:
            run = store.get_run(request["run_id"])
            worker = store.get_provider_session_by_id(request["session_id"])
            if (
                run is not None
                and run["state"] == "failed"
                and not client.app.state.service._local_processor_owns(
                    worker["worker_id"]
                )
            ):
                break
        time.sleep(0.01)
    assert request is not None
    assert run is not None
    assert run["state"] == "failed"
    assert run["failure_class"] == "provider_response_deadline_exceeded"
    assert not any(
        item["event_type"] in {"completed", "cancelled"}
        for item in store.list_provider_activity(request["request_id"])
    )


def test_foreground_deadline_cleanup_does_not_hold_provider_start_cancel_lock(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    runtime = SlowDeadlineCleanupRuntime()
    client = _client(tmp_path, monkeypatch, runtime=runtime)
    payload = _payload(workspace)
    payload["metadata"]["response_timeout_s"] = 0.5
    payload["tools"] = [
        {
            "type": "function",
            "function": {
                "name": "lc_transfer_to_specialist",
                "description": "Consult the specialist using shared graph state.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }
    ]

    with ThreadPoolExecutor(max_workers=1) as executor:
        timed_out = executor.submit(
            client.post,
            "/v1/chat/completions",
            headers=AUTH,
            json=payload,
        )
        assert runtime.started.wait(timeout=2)
        assert runtime.cleanup_started.wait(timeout=2)
        started_at = time.monotonic()
        unrelated_stop = client.post(
            "/v1/requests/by-idempotency/unrelated-turn/cancel",
            headers=AUTH,
        )
        lock_wait = time.monotonic() - started_at
        runtime.finish_cleanup.set()
        response = timed_out.result(timeout=2)

    assert unrelated_stop.status_code == 200, unrelated_stop.text
    assert unrelated_stop.json()["state"] == "cancelled"
    assert lock_wait < 0.5
    assert response.status_code == 504, response.text


def test_family_stop_does_not_hold_provider_lock_during_native_interrupt(
    tmp_path,
    monkeypatch,
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    runtime = SlowDeadlineCleanupRuntime()
    client = _client(tmp_path, monkeypatch, runtime=runtime)
    payload = _payload(workspace)

    with ThreadPoolExecutor(max_workers=2) as executor:
        active = executor.submit(
            client.post,
            "/v1/chat/completions",
            headers=AUTH,
            json=payload,
        )
        assert runtime.started.wait(timeout=2)
        stopping = executor.submit(
            client.post,
            "/v1/requests/by-idempotency/idem-a/cancel",
            headers=AUTH,
        )
        assert runtime.cleanup_started.wait(timeout=2)
        started_at = time.monotonic()
        unrelated_stop = client.post(
            "/v1/requests/by-idempotency/unrelated-family/cancel",
            headers=AUTH,
        )
        lock_wait = time.monotonic() - started_at
        runtime.finish_cleanup.set()
        stopped = stopping.result(timeout=2)
        active_response = active.result(timeout=2)

    assert unrelated_stop.status_code == 200, unrelated_stop.text
    assert unrelated_stop.json()["state"] == "cancelled"
    assert lock_wait < 0.5
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["state"] == "cancelled"
    assert active_response.status_code == 502, active_response.text


def test_foreground_provider_has_no_implicit_deadline_and_honors_explicit_metadata(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    monkeypatch.delenv("GLASSHIVE_PROVIDER_RESPONSE_TIMEOUT_S", raising=False)
    client = _client(tmp_path, monkeypatch)
    default_payload = _payload(workspace)

    default_response = client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json=default_payload,
    )
    longer_payload = json.loads(json.dumps(default_payload))
    longer_payload["metadata"].update(
        {
            "idempotency_key": "idem-longer",
            "response_timeout_s": 600,
        }
    )
    longer_response = client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json=longer_payload,
    )

    assert default_response.status_code == 200, default_response.text
    assert longer_response.status_code == 200, longer_response.text
    store = client.app.state.store
    default_request = store.get_provider_request(default_response.json()["id"])
    longer_request = store.get_provider_request(longer_response.json()["id"])
    assert default_request["response_timeout_s"] is None
    assert default_request["response_deadline_at"] == ""
    assert longer_request["response_timeout_s"] == 600
    assert longer_request["response_deadline_at"]


def test_foreground_provider_configured_deadline_caps_request_metadata(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    monkeypatch.setenv("GLASSHIVE_PROVIDER_RESPONSE_TIMEOUT_S", "180")
    client = _client(tmp_path, monkeypatch)
    payload = _payload(workspace)
    payload["metadata"]["response_timeout_s"] = 600

    response = client.post("/v1/chat/completions", headers=AUTH, json=payload)

    assert response.status_code == 200, response.text
    request = client.app.state.store.get_provider_request(response.json()["id"])
    assert request["response_timeout_s"] == 180


def test_foreground_deadline_budget_is_anchored_before_session_setup(
    tmp_path,
    monkeypatch,
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    provider = client.app.state.conversation_provider
    original_session = provider._session

    def delayed_session(*args, **kwargs):
        time.sleep(0.05)
        return original_session(*args, **kwargs)

    monkeypatch.setattr(provider, "_session", delayed_session)
    payload = _payload(workspace)
    payload["metadata"]["response_timeout_s"] = 1

    response = client.post("/v1/chat/completions", headers=AUTH, json=payload)

    assert response.status_code == 200, response.text
    request_record = client.app.state.store.get_provider_request(response.json()["id"])
    remaining_at_row_creation = (
        datetime.fromisoformat(request_record["response_deadline_at"])
        - datetime.fromisoformat(request_record["created_at"])
    ).total_seconds()
    assert 0 < remaining_at_row_creation < 0.98


def test_session_setup_returning_after_ingress_deadline_assigns_no_native_run(
    tmp_path,
    monkeypatch,
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    runtime = JustLateCompletionRuntime()
    client = _client(tmp_path, monkeypatch, runtime=runtime)
    provider = client.app.state.conversation_provider
    original_session = provider._session

    def delayed_session(*args, **kwargs):
        time.sleep(0.08)
        return original_session(*args, **kwargs)

    monkeypatch.setattr(provider, "_session", delayed_session)
    payload = _payload(workspace)
    payload["metadata"]["response_timeout_s"] = 0.05

    response = client.post("/v1/chat/completions", headers=AUTH, json=payload)

    assert response.status_code == 504, response.text
    request_record = client.app.state.store.get_provider_request(
        response.json()["error"]["request_id"]
    )
    assert request_record["state"] == "failed"
    assert request_record["run_id"] is None
    assert runtime.received_timeouts == []


@pytest.mark.parametrize("configured_timeout", ["0", "nan", "inf", "not-a-number"])
def test_foreground_provider_rejects_invalid_timeout_config(
    tmp_path,
    monkeypatch,
    configured_timeout,
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    monkeypatch.setenv(
        "GLASSHIVE_PROVIDER_RESPONSE_TIMEOUT_S",
        configured_timeout,
    )
    client = _client(tmp_path, monkeypatch)

    response = client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json=_payload(workspace),
    )

    assert response.status_code == 503, response.text
    assert "must be a positive number" in response.text


def test_foreground_deadline_columns_migrate_additively(tmp_path):
    db_path = tmp_path / "pre-deadline.sqlite3"
    seed = Store(str(db_path))
    project = seed.create_project(
        "legacy-owner",
        "Legacy provider request",
        "Exercise the nullable run-id migration",
        "codex-cli",
    )
    worker = seed.create_worker(
        project_id=project["project_id"],
        owner_id="legacy-owner",
        name="Legacy provider worker",
        role="conversation-agent",
        profile="codex-cli",
        backend="codex-cli",
        runtime="codex-cli",
        model="test",
        execution_mode="host",
    )
    run = seed.create_run(
        worker["worker_id"],
        project["project_id"],
        "Legacy provider request",
        state="completed",
    )
    session = seed.upsert_provider_session(
        tenant_id="local",
        owner_id="legacy-owner",
        conversation_id="legacy-conversation",
        agent_id="legacy-agent",
        model_id="codex-cli:test",
        project_id=project["project_id"],
        worker_id=worker["worker_id"],
        workspace_dir=str(tmp_path),
        access_mode="full",
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TABLE provider_requests")
        conn.execute(
            """
            CREATE TABLE provider_requests (
                request_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                message_id TEXT NOT NULL,
                stream_id TEXT NOT NULL,
                state TEXT NOT NULL,
                requested_history_count INTEGER NOT NULL DEFAULT 0,
                response_json TEXT NOT NULL DEFAULT '',
                fallback_model_id TEXT NOT NULL DEFAULT '',
                fallback_reasoning_effort TEXT NOT NULL DEFAULT '',
                fallback_instruction TEXT NOT NULL DEFAULT '',
                fallback_state TEXT NOT NULL DEFAULT '',
                fallback_from_run_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (tenant_id, owner_id, idempotency_key),
                FOREIGN KEY(session_id) REFERENCES provider_sessions(session_id),
                FOREIGN KEY(run_id) REFERENCES runs(run_id)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO provider_requests (
                request_id, tenant_id, owner_id, session_id, run_id,
                idempotency_key, message_id, stream_id, state,
                requested_history_count, response_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-request",
                "local",
                "legacy-owner",
                session["session_id"],
                run["run_id"],
                "legacy-idempotency",
                "legacy-message",
                "legacy-stream",
                "completed",
                2,
                "{}",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:01:00+00:00",
            ),
        )
        conn.execute(
            """
            INSERT INTO provider_activity (
                request_id, event_type, summary, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                "legacy-request",
                "provider.response.completed",
                "Legacy request completed",
                "{}",
                "2026-01-01T00:01:00+00:00",
            ),
        )

    store = Store(str(db_path))

    with sqlite3.connect(db_path) as conn:
        columns = {
            str(row[1]): {"type": str(row[2]), "notnull": int(row[3])}
            for row in conn.execute("PRAGMA table_info(provider_requests)")
        }
        indexes = {
            str(row[1]) for row in conn.execute("PRAGMA index_list(provider_requests)")
        }
        foreign_targets = {
            str(row[2]) for row in conn.execute("PRAGMA foreign_key_list(provider_requests)")
        }
        tombstone_columns = {
            str(row[1]) for row in conn.execute(
                "PRAGMA table_info(provider_stop_tombstones)"
            )
        }
        tombstone_indexes = {
            str(row[1])
            for row in conn.execute("PRAGMA index_list(provider_stop_tombstones)")
        }
    assert columns["run_id"]["notnull"] == 0
    assert columns["response_timeout_s"]["type"] == "REAL"
    assert columns["response_deadline_at"]["type"] == "TEXT"
    assert "idx_provider_requests_session" in indexes
    assert foreign_targets == {"provider_sessions", "runs"}
    assert tombstone_columns == {
        "tenant_id",
        "owner_id",
        "base_idempotency_key",
        "created_at",
        "expires_at",
    }
    assert "idx_provider_stop_tombstones_expiry" in tombstone_indexes
    assert store.get_provider_request("legacy-request")["run_id"] == run["run_id"]
    with store._connect() as conn:
        legacy_activity_count = conn.execute(
            "SELECT COUNT(*) FROM provider_activity WHERE request_id = ?",
            ("legacy-request",),
        ).fetchone()[0]
    assert legacy_activity_count == 1

    created, was_created = store.create_provider_request(
        tenant_id="local",
        owner_id="legacy-owner",
        session_id=session["session_id"],
        idempotency_key="new-idempotency",
        message_id="new-message",
        stream_id="new-stream",
        requested_history_count=1,
        response_timeout_s=180,
        response_deadline_at="2026-01-01T01:00:00+00:00",
    )
    assert was_created is True
    assert created["run_id"] is None


def test_main_specialist_main_round_trip_has_three_unique_provider_executions(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    runtime = GraphRoundTripRuntime()
    client = _client(tmp_path, monkeypatch, runtime=runtime)
    main_payload = _payload(workspace)
    main_payload["tools"] = [
        {
            "type": "function",
            "function": {
                "name": "lc_transfer_to_specialist",
                "description": "Consult the specialist using shared graph state.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }
    ]

    main_first = client.post("/v1/chat/completions", headers=AUTH, json=main_payload)
    main_first_retry = client.post(
        "/v1/chat/completions", headers=AUTH, json=main_payload
    )
    specialist_payload = json.loads(json.dumps(main_payload))
    specialist_payload["metadata"].update(
        {
            "agent_id": "agent-specialist",
            "idempotency_key": "idem-specialist",
        }
    )
    specialist_payload["tools"][0]["function"].update(
        {
            "name": "lc_transfer_to_main",
            "description": "Return evidence to Main using shared graph state.",
        }
    )
    specialist_payload["messages"].append(
        {"role": "assistant", "content": "Main requested specialist evidence."}
    )
    specialist = client.post(
        "/v1/chat/completions", headers=AUTH, json=specialist_payload
    )
    specialist_retry = client.post(
        "/v1/chat/completions", headers=AUTH, json=specialist_payload
    )
    main_return_payload = json.loads(json.dumps(main_payload))
    main_return_payload["messages"].append(
        {
            "role": "assistant",
            "content": "Specialist evidence returned through shared graph state.",
        }
    )
    main_return = client.post(
        "/v1/chat/completions", headers=AUTH, json=main_return_payload
    )
    main_return_retry = client.post(
        "/v1/chat/completions", headers=AUTH, json=main_return_payload
    )

    assert main_first.status_code == 200, main_first.text
    assert specialist.status_code == 200, specialist.text
    assert main_return.status_code == 200, main_return.text
    assert main_first.json()["choices"][0]["finish_reason"] == "tool_calls"
    assert specialist.json()["choices"][0]["finish_reason"] == "tool_calls"
    assert specialist.json()["choices"][0]["message"]["content"] == (
        "Specialist evidence returned through shared graph state."
    )
    assert main_return.json()["choices"][0]["finish_reason"] == "stop"
    assert main_return.json()["choices"][0]["message"]["content"] == (
        "Main final after specialist return."
    )
    assert main_first_retry.json()["id"] == main_first.json()["id"]
    assert specialist_retry.json()["id"] == specialist.json()["id"]
    assert main_return_retry.json()["id"] == main_return.json()["id"]
    assert len(
        {
            main_first.json()["id"],
            specialist.json()["id"],
            main_return.json()["id"],
        }
    ) == 3
    store = client.app.state.store
    main_family = store.list_provider_requests_by_idempotency_family(
        tenant_id="local",
        owner_id="owner-a",
        base_idempotency_key="idem-a",
    )
    specialist_family = store.list_provider_requests_by_idempotency_family(
        tenant_id="local",
        owner_id="owner-a",
        base_idempotency_key="idem-specialist",
    )
    all_attempts = [*main_family, *specialist_family]
    assert len(all_attempts) == 3
    assert len({record["idempotency_key"] for record in all_attempts}) == 3
    sessions = store.list_provider_sessions(owner_id="owner-a")
    assert len(sessions) == 2
    assert sum(
        len(store.list_runs_for_worker(session["worker_id"])) for session in sessions
    ) == 3


def test_same_agent_transfer_targets_are_offered_at_most_once_per_user_turn(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch, runtime=AvailableGraphTransferRuntime())
    payload = _payload(workspace)
    payload["tools"] = [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": f"Consult {name} using shared graph state.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }
        for name in ("lc_transfer_to_reality", "lc_transfer_to_red_team")
    ]

    first = client.post("/v1/chat/completions", headers=AUTH, json=payload)
    first_retry = client.post("/v1/chat/completions", headers=AUTH, json=payload)
    second_payload = json.loads(json.dumps(payload))
    second_payload["messages"].append(
        {"role": "assistant", "content": "Reality returned evidence."}
    )
    second = client.post(
        "/v1/chat/completions", headers=AUTH, json=second_payload
    )
    second_retry = client.post(
        "/v1/chat/completions", headers=AUTH, json=second_payload
    )
    final_payload = json.loads(json.dumps(second_payload))
    final_payload["messages"].append(
        {"role": "assistant", "content": "Red Team returned its challenge."}
    )
    final = client.post("/v1/chat/completions", headers=AUTH, json=final_payload)
    final_retry = client.post(
        "/v1/chat/completions", headers=AUTH, json=final_payload
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert final.status_code == 200, final.text
    assert first.json()["choices"][0]["message"]["tool_calls"][0]["function"][
        "name"
    ] == "lc_transfer_to_reality"
    assert second.json()["choices"][0]["message"]["tool_calls"][0]["function"][
        "name"
    ] == "lc_transfer_to_red_team"
    assert final.json()["choices"][0]["finish_reason"] == "stop"
    assert final.json()["choices"][0]["message"]["content"] == (
        "Main final after each available consultant ran once."
    )
    assert first_retry.json() == first.json()
    assert second_retry.json() == second.json()
    assert final_retry.json() == final.json()
    family = client.app.state.store.list_provider_requests_by_idempotency_family(
        tenant_id="local",
        owner_id="owner-a",
        base_idempotency_key="idem-a",
    )
    assert len(family) == 3


def test_existing_native_session_refreshes_graph_control_before_next_run(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(
        tmp_path,
        monkeypatch,
        runtime=AgentBuilderTransferRuntime(),
    )
    plain_payload = _payload(workspace)
    first = client.post("/v1/chat/completions", headers=AUTH, json=plain_payload)
    assert first.status_code == 200, first.text
    store = client.app.state.store
    initial_session = store.list_provider_sessions(owner_id="owner-a")[0]

    graph_payload = _payload(workspace)
    graph_payload["metadata"]["message_id"] = "message-graph-control"
    graph_payload["metadata"]["idempotency_key"] = "idem-graph-control"
    graph_payload["tools"] = [
        {
            "type": "function",
            "function": {
                "name": "lc_transfer_to_specialist",
                "description": "Consult the specialist using shared graph state.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }
    ]

    second = client.post("/v1/chat/completions", headers=AUTH, json=graph_payload)

    assert second.status_code == 200, second.text
    assert second.json()["choices"][0]["finish_reason"] == "tool_calls"
    refreshed_session = store.list_provider_sessions(owner_id="owner-a")[0]
    assert refreshed_session["worker_id"] == initial_session["worker_id"]
    worker = store.get_worker(refreshed_session["worker_id"])
    assert worker is not None
    bundle = json.loads(worker["bootstrap_bundle_json"])
    assert [
        tool["name"] for tool in bundle["agent_builder_control"]["tools"]
    ] == ["lc_transfer_to_specialist"]
    assert len(store.list_runs_for_worker(worker["worker_id"])) == 2


def test_base_cancel_prevents_late_graph_execution_child_from_starting(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch, runtime=AgentBuilderTransferRuntime())
    cancelled = client.post(
        "/v1/requests/by-idempotency/idem-a/cancel",
        headers=AUTH,
    )
    payload = _payload(workspace)
    payload["tools"] = [
        {
            "type": "function",
            "function": {
                "name": "lc_transfer_to_specialist",
                "description": "Consult the specialist using shared graph state.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }
    ]

    response = client.post("/v1/chat/completions", headers=AUTH, json=payload)

    assert cancelled.status_code == 200, cancelled.text
    assert response.status_code == 409
    assert "cancelled before native execution" in response.json()["detail"]


def test_agent_builder_control_ignores_input_bearing_transfer_but_keeps_zero_input_transfer(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(
        tmp_path,
        monkeypatch,
        runtime=AgentBuilderTransferRuntime("lc_transfer_to_specialist"),
    )
    payload = _payload(workspace)
    payload["tools"] = [
        {
            "type": "function",
            "function": {
                "name": "lc_transfer_to_with_input",
                "description": "This transfer requires manual input.",
                "parameters": {
                    "type": "object",
                    "properties": {"instructions": {"type": "string"}},
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "lc_transfer_to_specialist",
                "description": "Consult the specialist using shared graph state.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
        },
    ]

    response = client.post("/v1/chat/completions", headers=AUTH, json=payload)

    assert response.status_code == 200, response.text
    tool_call = response.json()["choices"][0]["message"]["tool_calls"][0]
    assert tool_call["function"]["name"] == "lc_transfer_to_specialist"
    session = client.app.state.store.list_provider_sessions(owner_id="owner-a")[0]
    worker = client.app.state.store.get_worker(session["worker_id"])
    assert worker is not None
    bundle = json.loads(worker["bootstrap_bundle_json"])
    assert [
        tool["name"] for tool in bundle["agent_builder_control"]["tools"]
    ] == ["lc_transfer_to_specialist"]


def test_agent_builder_control_rejects_explicitly_forced_input_bearing_transfer(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    payload = _payload(workspace)
    payload["tools"] = [
        {
            "type": "function",
            "function": {
                "name": "lc_transfer_to_with_input",
                "description": "This transfer requires manual input.",
                "parameters": {
                    "type": "object",
                    "properties": {"instructions": {"type": "string"}},
                    "required": [],
                },
            },
        }
    ]
    payload["tool_choice"] = {
        "type": "function",
        "function": {"name": "lc_transfer_to_with_input"},
    }

    response = client.post("/v1/chat/completions", headers=AUTH, json=payload)

    assert response.status_code == 400
    assert "Forced Agent Builder graph transfer tool is not available" in response.json()[
        "detail"
    ]


def test_agent_builder_control_preserves_plain_content_when_transfer_is_declined(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    runtime = AgentBuilderControlOutputRuntime(
        json.dumps(
            {
                "type": "assistant_response",
                "content": "A direct answer without a handoff.",
                "tool_name": None,
            }
        )
    )
    client = _client(tmp_path, monkeypatch, runtime=runtime)
    payload = _payload(workspace)
    payload["tools"] = [
        {
            "type": "function",
            "function": {
                "name": "lc_transfer_to_specialist",
                "description": "Consult the specialist when useful.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }
    ]

    response = client.post("/v1/chat/completions", headers=AUTH, json=payload)

    assert response.status_code == 200, response.text
    choice = response.json()["choices"][0]
    assert choice["finish_reason"] == "stop"
    assert choice["message"]["content"] == "A direct answer without a handoff."
    assert "tool_calls" not in choice["message"]


def test_streaming_graph_control_preserves_complete_plain_answer_without_envelope(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    runtime = AgentBuilderControlOutputRuntime(
        json.dumps(
            {
                "type": "assistant_response",
                "content": "A complete ordinary answer.",
                "tool_name": None,
            }
        )
    )
    client = _client(tmp_path, monkeypatch, runtime=runtime)
    payload = _payload(workspace, stream=True)
    payload["tools"] = [
        {
            "type": "function",
            "function": {
                "name": "lc_transfer_to_specialist",
                "description": "Consult the specialist when useful.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }
    ]

    with client.stream(
        "POST", "/v1/chat/completions", headers=AUTH, json=payload
    ) as response:
        assert response.status_code == 200, response.text
        lines = [line for line in response.iter_lines() if line]

    serialized = "\n".join(lines)
    chunks = [
        json.loads(line.removeprefix("data: "))
        for line in lines
        if line != "data: [DONE]"
    ]
    content = "".join(
        str(chunk["choices"][0]["delta"].get("content") or "")
        for chunk in chunks
        if chunk.get("choices")
    )
    assert content == "A complete ordinary answer."
    assert '"assistant_response"' not in serialized
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert lines[-1] == "data: [DONE]"


@pytest.mark.parametrize("stream", [False, True])
def test_graph_control_renders_available_native_provenance_and_drops_private_controls(
    tmp_path, monkeypatch, stream
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    runtime = CitationControlOutputRuntime(
        json.dumps(
            {
                "type": "assistant_response",
                "content": (
                    "Grounded claim. \\ue202turn0search0"
                    "\\ue202turn9view9\x1b[? provider-private artifact"
                ),
                "tool_name": None,
            }
        )
    )
    client = _client(tmp_path, monkeypatch, runtime=runtime)
    payload = _payload(workspace, stream=stream)
    payload["tools"] = [
        {
            "type": "function",
            "function": {
                "name": "lc_transfer_to_specialist",
                "description": "Consult the specialist when useful.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }
    ]

    response = client.post("/v1/chat/completions", headers=AUTH, json=payload)

    assert response.status_code == 200, response.text
    if stream:
        lines = [line for line in response.text.splitlines() if line]
        chunks = [
            json.loads(line.removeprefix("data: "))
            for line in lines
            if line != "data: [DONE]"
        ]
        visible = "".join(
            str(chunk["choices"][0]["delta"].get("content") or "")
            for chunk in chunks
            if chunk.get("choices")
        )
    else:
        visible = response.json()["choices"][0]["message"]["content"]

    assert visible == "Grounded claim. [Primary source](https://example.invalid/primary)"
    assert "turn0search0" not in visible
    assert "turn9view9" not in visible
    assert "\x1b" not in visible
    assert "provider-private artifact" not in visible


@pytest.mark.parametrize("stream", [False, True])
def test_graph_control_uses_final_validated_run_output_after_interim_native_message(
    tmp_path, monkeypatch, stream
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    final_output = json.dumps(
        {
            "type": "assistant_response",
            "content": "The final grounded answer.",
            "tool_name": None,
        }
    )
    runtime = InterimAgentBuilderControlOutputRuntime(final_output)
    client = _client(tmp_path, monkeypatch, runtime=runtime)
    payload = _payload(workspace, stream=stream)
    payload["tools"] = [
        {
            "type": "function",
            "function": {
                "name": "lc_transfer_to_specialist",
                "description": "Consult the specialist when useful.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }
    ]

    response = client.post("/v1/chat/completions", headers=AUTH, json=payload)

    assert response.status_code == 200, response.text
    if not stream:
        choice = response.json()["choices"][0]
        assert choice["finish_reason"] == "stop"
        assert choice["message"]["content"] == "The final grounded answer."
        return

    lines = [line for line in response.text.splitlines() if line]
    chunks = [
        json.loads(line.removeprefix("data: "))
        for line in lines
        if line != "data: [DONE]"
    ]
    content = "".join(
        str(chunk["choices"][0]["delta"].get("content") or "")
        for chunk in chunks
        if chunk.get("choices")
    )
    assert content == "The final grounded answer."
    assert lines[-1] == "data: [DONE]"


@pytest.mark.parametrize(
    "native_output",
    [
        "not-json",
        json.dumps(
            {
                "type": "tool_call",
                "content": "",
                "tool_name": "lc_transfer_to_unknown",
            }
        ),
        json.dumps(
            {
                "type": "tool_call",
                "content": "",
                "tool_name": "lc_transfer_to_specialist",
                "unexpected": "must fail closed",
            }
        ),
    ],
)
def test_agent_builder_control_fails_closed_on_malformed_or_unknown_native_choice(
    tmp_path, monkeypatch, native_output
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(
        tmp_path,
        monkeypatch,
        runtime=AgentBuilderControlOutputRuntime(native_output),
    )
    payload = _payload(workspace)
    payload["tools"] = [
        {
            "type": "function",
            "function": {
                "name": "lc_transfer_to_specialist",
                "description": "Consult the specialist when useful.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }
    ]

    response = client.post("/v1/chat/completions", headers=AUTH, json=payload)

    assert response.status_code == 502
    assert response.json()["detail"] == (
        "GlassHive harness returned invalid Agent Builder graph control output"
    )
    assert native_output not in response.text


def test_tool_choice_none_keeps_graph_control_out_of_native_bundle(tmp_path, monkeypatch):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    payload = _payload(workspace)
    payload["tools"] = [
        {
            "type": "function",
            "function": {
                "name": "lc_transfer_to_specialist",
                "description": "Consult the specialist when useful.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }
    ]
    payload["tool_choice"] = "none"

    response = client.post("/v1/chat/completions", headers=AUTH, json=payload)

    assert response.status_code == 200, response.text
    session = client.app.state.store.list_provider_sessions(owner_id="owner-a")[0]
    worker = client.app.state.store.get_worker(session["worker_id"])
    assert worker is not None
    bundle = json.loads(worker["bootstrap_bundle_json"])
    assert "agent_builder_control" not in bundle
    assert bundle["provider_capabilities"]["graph_control_tools"] == []


def test_streaming_agent_builder_transfer_emits_only_openai_tool_call_delta(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(
        tmp_path,
        monkeypatch,
        runtime=AgentBuilderTransferRuntime(),
    )
    payload = _payload(workspace, stream=True)
    payload["tools"] = [
        {
            "type": "function",
            "function": {
                "name": "lc_transfer_to_specialist",
                "description": "Consult the specialist using shared graph state.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }
    ]

    with client.stream(
        "POST", "/v1/chat/completions", headers=AUTH, json=payload
    ) as response:
        assert response.status_code == 200, response.text
        lines = [line for line in response.iter_lines() if line]

    chunks = [
        json.loads(line.removeprefix("data: "))
        for line in lines
        if line != "data: [DONE]"
    ]
    visible_content = "".join(
        str(chunk["choices"][0]["delta"].get("content") or "")
        for chunk in chunks
        if chunk.get("choices")
    )
    assert visible_content == ""
    tool_chunks = [
        chunk["choices"][0]["delta"]["tool_calls"]
        for chunk in chunks
        if chunk.get("choices")
        and "tool_calls" in chunk["choices"][0]["delta"]
    ]
    assert len(tool_chunks) == 1
    assert tool_chunks[0][0]["function"] == {
        "name": "lc_transfer_to_specialist",
        "arguments": "{}",
    }
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"
    assert lines[-1] == "data: [DONE]"


def test_model_change_supersedes_native_session_and_seeds_visible_history(tmp_path, monkeypatch):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    first_payload = _payload(workspace)
    first = client.post("/v1/chat/completions", headers=AUTH, json=first_payload)
    assert first.status_code == 200
    first_session = client.app.state.store.list_provider_sessions(owner_id="owner-a")[0]

    second_payload = _payload(workspace, model="claude-code:opus")
    second_payload["metadata"]["message_id"] = "message-b"
    second_payload["metadata"]["idempotency_key"] = "idem-b"
    second_payload["messages"].extend(
        [
            {"role": "assistant", "content": "Earlier answer."},
            {"role": "user", "content": "Please correct it."},
        ]
    )
    second = client.post("/v1/chat/completions", headers=AUTH, json=second_payload)

    assert second.status_code == 200, second.text
    current = client.app.state.store.list_provider_sessions(owner_id="owner-a")[0]
    assert current["model_id"] == "claude-code:opus"
    assert current["worker_id"] != first_session["worker_id"]
    old_worker = client.app.state.store.get_worker(first_session["worker_id"])
    assert old_worker is not None and old_worker["state"] == "terminated"
    assert "Earlier answer." in second.json()["choices"][0]["message"]["content"]
    assert "Please correct it." in second.json()["choices"][0]["message"]["content"]


def test_native_policy_change_supersedes_contaminated_session_and_seeds_visible_history(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("GLASSHIVE_HOST_PLUGIN_DENYLIST", raising=False)
    monkeypatch.delenv("WPR_HOST_PLUGIN_DENYLIST", raising=False)
    monkeypatch.setenv("WPR_CODEX_CLI_PERSONALITY", "inherit")
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    first_payload = _payload(workspace)
    first = client.post("/v1/chat/completions", headers=AUTH, json=first_payload)
    assert first.status_code == 200
    first_session = client.app.state.store.list_provider_sessions(owner_id="owner-a")[0]

    monkeypatch.setenv(
        "GLASSHIVE_HOST_PLUGIN_DENYLIST",
        "synthetic-policy@project-viventium",
    )
    second_payload = _payload(workspace)
    second_payload["metadata"]["message_id"] = "message-policy-change"
    second_payload["metadata"]["idempotency_key"] = "idem-policy-change"
    second_payload["messages"].extend(
        [
            {"role": "assistant", "content": "Earlier visible answer."},
            {"role": "user", "content": "Continue after the policy update."},
        ]
    )
    second = client.post("/v1/chat/completions", headers=AUTH, json=second_payload)

    assert second.status_code == 200, second.text
    current = client.app.state.store.list_provider_sessions(owner_id="owner-a")[0]
    assert current["worker_id"] != first_session["worker_id"]
    old_worker = client.app.state.store.get_worker(first_session["worker_id"])
    assert old_worker is not None and old_worker["state"] == "terminated"
    content = second.json()["choices"][0]["message"]["content"]
    assert "Earlier visible answer." in content
    assert "Continue after the policy update." in content


def test_native_web_access_policy_change_supersedes_session_and_seeds_visible_history(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WPR_HOST_NATIVE_WEB_ACCESS", "inherit")
    monkeypatch.delenv("GLASSHIVE_HOST_NATIVE_WEB_ACCESS", raising=False)
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    first_payload = _payload(workspace)
    first = client.post("/v1/chat/completions", headers=AUTH, json=first_payload)
    assert first.status_code == 200
    first_session = client.app.state.store.list_provider_sessions(owner_id="owner-a")[0]

    monkeypatch.setenv("WPR_HOST_NATIVE_WEB_ACCESS", "disabled")
    monkeypatch.setenv("GLASSHIVE_HOST_NATIVE_WEB_ACCESS", "inherit")
    second_payload = _payload(workspace)
    second_payload["metadata"]["message_id"] = "message-native-web-policy"
    second_payload["metadata"]["idempotency_key"] = "idem-native-web-policy"
    second_payload["messages"].extend(
        [
            {"role": "assistant", "content": "Earlier visible answer."},
            {"role": "user", "content": "Continue through the declared capability boundary."},
        ]
    )
    second = client.post("/v1/chat/completions", headers=AUTH, json=second_payload)

    assert second.status_code == 200, second.text
    current = client.app.state.store.list_provider_sessions(owner_id="owner-a")[0]
    assert current["worker_id"] != first_session["worker_id"]
    old_worker = client.app.state.store.get_worker(first_session["worker_id"])
    assert old_worker is not None and old_worker["state"] == "terminated"
    content = second.json()["choices"][0]["message"]["content"]
    assert "Earlier visible answer." in content
    assert "Continue through the declared capability boundary." in content


def test_system_state_change_supersedes_session_and_uses_native_developer_authority(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    first_payload = _payload(workspace)
    first_payload["messages"][0]["content"] = "Quiet Feeling capsule."
    first = client.post("/v1/chat/completions", headers=AUTH, json=first_payload)
    assert first.status_code == 200, first.text
    first_session = client.app.state.store.list_provider_sessions(owner_id="owner-a")[0]
    first_worker = client.app.state.store.get_worker(first_session["worker_id"])
    assert first_worker is not None
    assert json.loads(first_worker["bootstrap_bundle_json"])["developer_instructions"] == (
        "Quiet Feeling capsule."
    )

    second_payload = _payload(workspace)
    second_payload["metadata"]["message_id"] = "message-feeling-change"
    second_payload["metadata"]["idempotency_key"] = "idem-feeling-change"
    second_payload["messages"] = [
        {"role": "system", "content": "Joyful Feeling capsule."},
        {"role": "user", "content": "Hello from LIFE."},
        {"role": "assistant", "content": "Earlier visible answer."},
        {"role": "user", "content": "Continue with the current state."},
    ]
    second = client.post("/v1/chat/completions", headers=AUTH, json=second_payload)

    assert second.status_code == 200, second.text
    current = client.app.state.store.list_provider_sessions(owner_id="owner-a")[0]
    assert current["worker_id"] != first_session["worker_id"]
    old_worker = client.app.state.store.get_worker(first_session["worker_id"])
    assert old_worker is not None and old_worker["state"] == "terminated"
    current_worker = client.app.state.store.get_worker(current["worker_id"])
    assert current_worker is not None
    current_bundle = json.loads(current_worker["bootstrap_bundle_json"])
    assert current_bundle["developer_instructions"] == "Joyful Feeling capsule."
    content = second.json()["choices"][0]["message"]["content"]
    assert "Earlier visible answer." in content
    assert "Continue with the current state." in content
    assert "Quiet Feeling capsule." not in content
    assert "Joyful Feeling capsule." not in content


def test_turn_context_refreshes_without_replacing_unchanged_native_authority(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    first_payload = _payload(workspace)
    first_payload["metadata"]["turn_context"] = "Current time: 4:05 PM."
    first = client.post("/v1/chat/completions", headers=AUTH, json=first_payload)
    assert first.status_code == 200, first.text
    first_session = client.app.state.store.list_provider_sessions(owner_id="owner-a")[0]

    second_payload = _payload(workspace)
    second_payload["metadata"]["message_id"] = "message-turn-context"
    second_payload["metadata"]["idempotency_key"] = "idem-turn-context"
    second_payload["metadata"]["turn_context"] = "Current time: 4:06 PM."
    second_payload["messages"].extend(
        [
            {"role": "assistant", "content": "Earlier visible answer."},
            {"role": "user", "content": "Continue now."},
        ]
    )
    second = client.post("/v1/chat/completions", headers=AUTH, json=second_payload)

    assert second.status_code == 200, second.text
    current = client.app.state.store.list_provider_sessions(owner_id="owner-a")[0]
    assert current["worker_id"] == first_session["worker_id"]
    worker = client.app.state.store.get_worker(current["worker_id"])
    assert worker is not None
    bundle = json.loads(worker["bootstrap_bundle_json"])
    assert bundle["developer_instructions"] == "Be a thoughtful assistant."
    assert "Current time:" not in bundle["developer_instructions"]
    runs = client.app.state.store.list_runs_for_worker(current["worker_id"])
    assert len(runs) == 2
    assert "Current time: 4:06 PM." in runs[0]["instruction"]
    assert "Current time: 4:05 PM." not in runs[0]["instruction"]
    assert "Continue now." in runs[0]["instruction"]


def test_history_instruction_never_flattens_system_or_developer_roles_into_user_text():
    instruction = _history_instruction(
        [
            ChatMessage(role="system", content="System authority."),
            ChatMessage(role="developer", content="Developer authority."),
            ChatMessage(role="user", content="Visible request."),
        ]
    )

    assert "System authority." not in instruction
    assert "Developer authority." not in instruction
    assert "Visible request." in instruction
    assert "Honor AGENTS.md" not in instruction


def test_authority_snapshot_keeps_the_final_feeling_capsule_in_native_developer_authority():
    capsule = (
        "<viventium_feeling_state>\n"
        "Synthetic private causal state.\n"
        "</viventium_feeling_state>"
    )
    snapshot = _system_snapshot(
        [
            ChatMessage(role="system", content="Stable Viventium identity."),
            ChatMessage(role="developer", content="Structural delivery contract."),
            ChatMessage(role="developer", content="Structural delivery contract."),
            ChatMessage(role="system", content=capsule),
            ChatMessage(role="user", content="Visible request."),
        ]
    )

    assert snapshot.endswith(capsule)
    assert snapshot.count(capsule) == 1
    assert snapshot.count("Structural delivery contract.") == 1
    assert "Visible request." not in snapshot


def test_declared_dynamic_authority_tail_moves_after_later_structural_developer_text():
    capsule = (
        "<viventium_feeling_state>\n"
        "Synthetic bright and playful private causal state.\n"
        "</viventium_feeling_state>"
    )
    payload = ChatCompletionRequest.model_validate(
        {
            "model": "codex-cli:gpt-5.6-sol",
            "messages": [
                {"role": "system", "content": f"Stable identity.\n\n{capsule}"},
                {"role": "developer", "content": "Structural capability contract."},
                {"role": "user", "content": "Visible request."},
            ],
            "metadata": {
                "owner_id": "owner-a",
                "conversation_id": "conv-a",
                "agent_id": "agent-a",
                "developer_instruction_tail": capsule,
            },
        }
    )

    snapshot = _developer_instruction_snapshot(payload)

    assert snapshot.endswith(capsule)
    assert snapshot.count(capsule) == 1
    assert snapshot.index("Structural capability contract.") < snapshot.index(capsule)
    assert "Visible request." not in snapshot


def test_declared_dynamic_authority_tail_must_already_exist_in_authority_messages():
    payload = ChatCompletionRequest.model_validate(
        {
            "model": "codex-cli:gpt-5.6-sol",
            "messages": [
                {"role": "system", "content": "Stable identity."},
                {"role": "user", "content": "Visible request."},
            ],
            "metadata": {
                "owner_id": "owner-a",
                "conversation_id": "conv-a",
                "agent_id": "agent-a",
                "developer_instruction_tail": "Undeclared hidden authority.",
            },
        }
    )

    with pytest.raises(Exception, match="tail is absent"):
        _developer_instruction_snapshot(payload)


def test_provider_header_pins_dynamic_authority_after_structural_messages(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    capsule = (
        "<viventium_feeling_state>\n"
        "Synthetic bright and playful private causal state.\n"
        "</viventium_feeling_state>"
    )
    payload = _payload(workspace)
    payload["messages"] = [
        {"role": "system", "content": f"Stable identity.\n\n{capsule}"},
        {"role": "developer", "content": "Structural capability contract."},
        {"role": "user", "content": "Visible request."},
    ]
    headers = {
        **AUTH,
        "X-GlassHive-Developer-Instruction-Tail-B64": base64.b64encode(
            capsule.encode("utf-8")
        ).decode("ascii"),
    }

    response = client.post("/v1/chat/completions", headers=headers, json=payload)

    assert response.status_code == 200, response.text
    session = client.app.state.store.list_provider_sessions(owner_id="owner-a")[0]
    worker = client.app.state.store.get_worker(session["worker_id"])
    developer_instructions = json.loads(worker["bootstrap_bundle_json"])[
        "developer_instructions"
    ]
    assert developer_instructions.endswith(capsule)
    assert developer_instructions.count(capsule) == 1
    assert developer_instructions.index("Structural capability contract.") < (
        developer_instructions.index(capsule)
    )


def test_inherited_codex_personality_change_supersedes_native_session(
    tmp_path, monkeypatch
):
    source_codex_home = tmp_path / "source-codex-home"
    source_codex_home.mkdir()
    source_config = source_codex_home / "config.toml"
    source_config.write_text('personality = "pragmatic"\n')
    monkeypatch.setenv("CODEX_HOME", str(source_codex_home))
    monkeypatch.setenv("WPR_CODEX_CLI_PERSONALITY", "inherit")
    monkeypatch.delenv("GLASSHIVE_HOST_PLUGIN_DENYLIST", raising=False)
    monkeypatch.delenv("WPR_HOST_PLUGIN_DENYLIST", raising=False)
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    first_payload = _payload(workspace)
    first = client.post("/v1/chat/completions", headers=AUTH, json=first_payload)
    assert first.status_code == 200
    first_session = client.app.state.store.list_provider_sessions(owner_id="owner-a")[0]

    source_config.write_text('personality = "none"\n')
    second_payload = _payload(workspace)
    second_payload["metadata"]["message_id"] = "message-personality-change"
    second_payload["metadata"]["idempotency_key"] = "idem-personality-change"
    second = client.post("/v1/chat/completions", headers=AUTH, json=second_payload)

    assert second.status_code == 200, second.text
    current = client.app.state.store.list_provider_sessions(owner_id="owner-a")[0]
    assert current["worker_id"] != first_session["worker_id"]
    old_worker = client.app.state.store.get_worker(first_session["worker_id"])
    assert old_worker is not None and old_worker["state"] == "terminated"


def test_phase_b_style_short_prompt_reuses_session_without_losing_visible_history_count(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    first_payload = _payload(workspace)
    first_payload["messages"].extend(
        [
            {"role": "assistant", "content": "Initial answer."},
            {"role": "user", "content": "One correction."},
        ]
    )
    first = client.post("/v1/chat/completions", headers=AUTH, json=first_payload)
    assert first.status_code == 200
    initial_session = client.app.state.store.list_provider_sessions(owner_id="owner-a")[0]
    assert initial_session["history_count"] == 5

    follow_up = _payload(workspace)
    follow_up["metadata"]["message_id"] = "message-phase-b"
    follow_up["metadata"]["idempotency_key"] = "idem-phase-b"
    follow_up["messages"] = [{"role": "user", "content": "Synthesize the cortex insight now."}]
    response = client.post("/v1/chat/completions", headers=AUTH, json=follow_up)

    assert response.status_code == 200, response.text
    assert "Synthesize the cortex insight now." in response.json()["choices"][0]["message"]["content"]
    current_session = client.app.state.store.list_provider_sessions(owner_id="owner-a")[0]
    assert current_session["session_id"] == initial_session["session_id"]
    assert current_session["worker_id"] == initial_session["worker_id"]
    assert current_session["history_count"] == 5
    worker = client.app.state.store.get_worker(current_session["worker_id"])
    assert worker is not None
    assert json.loads(worker["bootstrap_bundle_json"])["developer_instructions"] == (
        "Be a thoughtful assistant."
    )


def test_normal_resumed_turn_sends_only_new_visible_messages_to_native_session(tmp_path, monkeypatch):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    first_payload = _payload(workspace)
    first = client.post("/v1/chat/completions", headers=AUTH, json=first_payload)
    assert first.status_code == 200
    first_session = client.app.state.store.list_provider_sessions(owner_id="owner-a")[0]

    second_payload = _payload(workspace)
    second_payload["metadata"]["message_id"] = "message-normal-follow-up"
    second_payload["metadata"]["idempotency_key"] = "idem-normal-follow-up"
    second_payload["messages"] = [
        {"role": "system", "content": "Be a thoughtful assistant."},
        first_payload["messages"][1],
        {"role": "assistant", "content": "Prior assistant answer."},
        {"role": "user", "content": "Only this correction is new."},
    ]

    second = client.post("/v1/chat/completions", headers=AUTH, json=second_payload)

    assert second.status_code == 200
    content = second.json()["choices"][0]["message"]["content"]
    assert "Be a thoughtful assistant." not in content
    assert "Only this correction is new." in content
    assert "Prior assistant answer." not in content
    assert "Hello from LIFE." not in content
    current = client.app.state.store.list_provider_sessions(owner_id="owner-a")[0]
    assert current["worker_id"] == first_session["worker_id"]


def test_effort_change_updates_the_existing_native_session_without_replacing_it(tmp_path, monkeypatch):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    first_payload = _payload(workspace)
    first = client.post("/v1/chat/completions", headers=AUTH, json=first_payload)
    assert first.status_code == 200
    first_session = client.app.state.store.list_provider_sessions(owner_id="owner-a")[0]

    second_payload = _payload(workspace)
    second_payload["reasoning_effort"] = "high"
    second_payload["metadata"]["message_id"] = "message-effort-change"
    second_payload["metadata"]["idempotency_key"] = "idem-effort-change"
    second = client.post("/v1/chat/completions", headers=AUTH, json=second_payload)

    assert second.status_code == 200, second.text
    current = client.app.state.store.list_provider_sessions(owner_id="owner-a")[0]
    assert current["worker_id"] == first_session["worker_id"]
    manifest = json.loads(current["context_manifest_json"])
    assert manifest["effort"] == "high"


def test_all_declared_codex_efforts_validate_without_replacing_the_session(tmp_path, monkeypatch):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    worker_ids = []

    for index, effort in enumerate(("low", "medium", "high", "xhigh", "max", "ultra")):
        payload = _payload(workspace)
        payload["reasoning_effort"] = effort
        payload["metadata"]["message_id"] = f"message-effort-{index}"
        payload["metadata"]["idempotency_key"] = f"idem-effort-{index}"
        response = client.post("/v1/chat/completions", headers=AUTH, json=payload)
        assert response.status_code == 200, response.text
        worker_ids.append(client.app.state.store.list_provider_sessions(owner_id="owner-a")[0]["worker_id"])

    assert len(set(worker_ids)) == 1


def test_failed_native_worker_is_replaced_before_the_next_turn(tmp_path, monkeypatch):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    first = client.post("/v1/chat/completions", headers=AUTH, json=_payload(workspace))
    assert first.status_code == 200
    store = client.app.state.store
    first_session = store.list_provider_sessions(owner_id="owner-a")[0]
    store.update_worker_state(first_session["worker_id"], "failed", last_error="synthetic crash")

    follow_up = _payload(workspace)
    follow_up["metadata"]["message_id"] = "message-after-crash"
    follow_up["metadata"]["idempotency_key"] = "idem-after-crash"
    second = client.post("/v1/chat/completions", headers=AUTH, json=follow_up)

    assert second.status_code == 200, second.text
    current = store.list_provider_sessions(owner_id="owner-a")[0]
    assert current["worker_id"] != first_session["worker_id"]


def test_authenticated_broker_bundle_is_forwarded_and_conversation_policy_is_forced(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    runtime = BrokerBundleCaptureRuntime()
    client = _client(tmp_path, monkeypatch, runtime=runtime)
    payload = _payload(workspace)
    bearer = "conversation-broker-secret-value"
    broker_bundle = {
        "codex_config_append": (
            "[mcp_servers.glasshive-user-capabilities]\n"
            "bearer_token_env_var = \"GLASSHIVE_CAPABILITY_BROKER_TOKEN\""
        ),
        "claude_project_mcp": {
            "glasshive-user-capabilities": {
                "type": "http",
                "url": "http://127.0.0.1:3180/mcp",
                "headers": {
                    "Authorization": "Bearer ${GLASSHIVE_CAPABILITY_BROKER_TOKEN}"
                },
            }
        },
        "env": {"GLASSHIVE_CAPABILITY_BROKER_TOKEN": bearer},
        "glasshive_capability_broker": {
            "allowed_host_tools": ["file_search"],
            "authority_kind": "conversation_orchestrator",
        },
        "run_mode": "mission",
        "provider_capabilities": {"self_delegation": True},
    }
    headers = {**AUTH, **_signed_bootstrap_headers(broker_bundle)}

    response = client.post("/v1/chat/completions", headers=headers, json=payload)

    assert response.status_code == 200, response.text
    session = client.app.state.store.list_provider_sessions(owner_id="owner-a")[0]
    worker = client.app.state.store.get_worker(session["worker_id"])
    persisted = json.loads(worker["bootstrap_bundle_json"])
    assert persisted["codex_config_append"] == broker_bundle["codex_config_append"]
    assert persisted["claude_project_mcp"] == broker_bundle["claude_project_mcp"]
    assert "GLASSHIVE_CAPABILITY_BROKER_TOKEN" not in persisted["env"]
    assert runtime.run_bundles[-1]["env"]["GLASSHIVE_CAPABILITY_BROKER_TOKEN"] == bearer
    assert persisted["run_mode"] == "conversation"
    assert persisted["provider_capabilities"] == {
        "self_delegation": False,
        "worker_native_tools": True,
        "host_tools_transport": "broker_mcp",
        "host_tools": ["file_search"],
        "graph_control_transport": "none",
        "graph_control_tools": [],
    }
    assert bearer not in (tmp_path / "runtime.db").read_bytes().decode("utf-8", errors="ignore")
    with sqlite3.connect(tmp_path / "runtime.db") as conn:
        for table in (
            "workers",
            "runs",
            "events",
            "callback_outbox",
            "run_action_uses",
            "active_work_action_uses",
            "provider_sessions",
            "provider_requests",
            "provider_activity",
        ):
            rows = conn.execute(f'SELECT * FROM "{table}"').fetchall()
            assert bearer not in json.dumps(rows, default=str), table
    for path in workspace.rglob("*"):
        if path.is_file():
            assert bearer not in path.read_bytes().decode("utf-8", errors="ignore"), path


def test_duplicate_after_restart_refreshes_queued_conversation_bearer_without_persistence(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    first_runtime = BrokerBundleCaptureRuntime()
    first_client = _client(tmp_path, monkeypatch, runtime=first_runtime)
    first_client.app.state.service._ensure_worker_processor = lambda _worker_id: None
    payload = _payload(workspace)
    first_bearer = "first-invocation-only-bearer"
    bundle = {
        "env": {"GLASSHIVE_CAPABILITY_BROKER_TOKEN": first_bearer},
        "glasshive_capability_broker": {
            "authority_kind": "conversation_orchestrator",
            "allowed_host_tools": ["active_work"],
        },
    }
    payload["metadata"]["bootstrap_bundle"] = bundle
    accepted = first_client.app.state.conversation_provider.start(
        ChatCompletionRequest.model_validate(payload),
        type("SyntheticRequest", (), {"headers": {"x-viventium-user-id": "owner-a"}})(),
    )
    assert accepted["state"] == "queued"
    request_record = first_client.app.state.store.get_provider_request(
        tenant_id="local", owner_id="owner-a", idempotency_key="idem-a"
    )
    run_id = str(request_record["run_id"])
    assert first_client.app.state.store.get_run(run_id)["state"] == "queued"
    first_client.app.state.service.shutdown()

    second_bearer = "fresh-retry-only-bearer"
    fresh_bundle = {
        **bundle,
        "env": {"GLASSHIVE_CAPABILITY_BROKER_TOKEN": second_bearer},
    }
    payload["metadata"]["bootstrap_bundle"] = fresh_bundle
    second_runtime = BrokerBundleCaptureRuntime()
    restarted = _client(tmp_path, monkeypatch, runtime=second_runtime)
    retried = restarted.app.state.conversation_provider.start(
        ChatCompletionRequest.model_validate(payload),
        type("SyntheticRequest", (), {"headers": {"x-viventium-user-id": "owner-a"}})(),
    )

    assert retried["request_id"] == request_record["request_id"]
    deadline = time.time() + 2
    while time.time() < deadline and not second_runtime.run_bundles:
        time.sleep(0.01)
    assert second_runtime.run_bundles, {
        "run": restarted.app.state.store.get_run(run_id),
        "worker": restarted.app.state.store.get_worker(
            restarted.app.state.store.get_provider_session_by_id(
                request_record["session_id"]
            )["worker_id"]
        ),
        "active_processors": list(restarted.app.state.service._active_processors),
    }
    assert second_runtime.run_bundles[-1]["env"][
        "GLASSHIVE_CAPABILITY_BROKER_TOKEN"
    ] == second_bearer
    database = (tmp_path / "runtime.db").read_bytes().decode("utf-8", errors="ignore")
    assert first_bearer not in database
    assert second_bearer not in database


def test_restarted_queued_conversation_never_runs_without_fresh_broker_grant(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    runtime = BrokerBundleCaptureRuntime()
    client = _client(tmp_path, monkeypatch, runtime=runtime)
    client.app.state.service._ensure_worker_processor = lambda _worker_id: None
    payload = _payload(workspace)
    bearer = "lost-on-crash-bearer"
    bundle = {
        "env": {"GLASSHIVE_CAPABILITY_BROKER_TOKEN": bearer},
        "glasshive_capability_broker": {
            "authority_kind": "conversation_orchestrator",
            "allowed_host_tools": ["active_work"],
        },
    }
    payload["metadata"]["bootstrap_bundle"] = bundle
    accepted = client.app.state.conversation_provider.start(
        ChatCompletionRequest.model_validate(payload),
        type("SyntheticRequest", (), {"headers": {"x-viventium-user-id": "owner-a"}})(),
    )
    assert accepted["state"] == "queued"
    request_record = client.app.state.store.get_provider_request(
        tenant_id="local", owner_id="owner-a", idempotency_key="idem-a"
    )
    run_id = str(request_record["run_id"])
    worker = client.app.state.store.get_provider_session_by_id(
        request_record["session_id"]
    )
    client.app.state.service.shutdown()

    restarted_service = WorkersProjectsService(
        Store(str(tmp_path / "runtime.db")), runtime, reconcile_on_startup=False
    )
    try:
        restarted_service.start_assigned_run(str(worker["worker_id"]))
        deadline = time.time() + 2
        while time.time() < deadline:
            durable = restarted_service.store.get_run(run_id)
            durable_worker = restarted_service.store.get_worker(
                str(worker["worker_id"])
            )
            if (
                durable
                and durable["state"] == "needs_input"
                and durable_worker
                and durable_worker["state"] == "needs_input"
            ):
                break
            time.sleep(0.01)
        durable = restarted_service.store.get_run(run_id)
        assert durable["state"] == "needs_input"
        assert durable["failure_class"] == "conversation_capability_grant_required"
        assert durable["failure_retryable"] == 0
        assert "refreshed" in durable["failure_user_message"].lower()
        assert restarted_service.store.get_worker(str(worker["worker_id"]))["state"] == "needs_input"
        assert runtime.run_bundles == []
    finally:
        restarted_service.shutdown()


def test_store_hardens_existing_database_permissions(tmp_path):
    db_path = tmp_path / "existing-runtime.db"
    sqlite3.connect(db_path).close()
    os.chmod(db_path, 0o644)

    Store(str(db_path))

    assert db_path.stat().st_mode & 0o777 == 0o600


def test_bootstrap_bundle_rejects_missing_tampered_stale_and_oversized_signatures(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    payload = _payload(workspace)
    bundle = {"env": {"SYNTHETIC_VALUE": "safe"}}
    encoded = base64.b64encode(json.dumps(bundle).encode()).decode()

    missing = client.post(
        "/v1/chat/completions",
        headers={**AUTH, "X-GlassHive-Bootstrap-Bundle-B64": encoded},
        json=payload,
    )
    tampered = client.post(
        "/v1/chat/completions",
        headers={
            **AUTH,
            **_signed_bootstrap_headers(bundle),
            "X-GlassHive-Bootstrap-Signature": "sha256=" + ("0" * 64),
        },
        json=payload,
    )
    stale = client.post(
        "/v1/chat/completions",
        headers={**AUTH, **_signed_bootstrap_headers(bundle, issued_at=int(time.time()) - 601)},
        json=payload,
    )
    oversized_bundle = {"padding": "x" * (129 * 1024)}
    oversized = client.post(
        "/v1/chat/completions",
        headers={**AUTH, **_signed_bootstrap_headers(oversized_bundle)},
        json=payload,
    )

    assert missing.status_code == 401
    assert tampered.status_code == 401
    assert stale.status_code == 401
    assert oversized.status_code == 400


def test_streaming_completion_and_activity_recovery(tmp_path, monkeypatch):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    payload = _payload(workspace, stream=True)

    with client.stream("POST", "/v1/chat/completions", headers=AUTH, json=payload) as response:
        assert response.status_code == 200, response.text
        lines = [line for line in response.iter_lines() if line]

    chunks = [json.loads(line.removeprefix("data: ")) for line in lines if line != "data: [DONE]"]
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    reasoning = [
        chunk["choices"][0]["delta"]["reasoning_content"]
        for chunk in chunks
        if "reasoning_content" in chunk["choices"][0]["delta"]
    ]
    assert reasoning == []
    request_id = chunks[0]["id"]
    assert any("Hello from LIFE." in chunk["choices"][0]["delta"].get("content", "") for chunk in chunks)
    assert lines[-1] == "data: [DONE]"

    activity = client.get(f"/v1/requests/{request_id}/activity", headers=AUTH)
    assert activity.status_code == 200
    events = activity.json()["data"]
    assert [event["event"] for event in events][:2] == ["queued", "started"]
    assert events[-1]["event"] == "completed"
    after_first = client.get(
        f"/v1/requests/{request_id}/activity",
        headers={**AUTH, "Last-Event-ID": str(events[0]["id"])},
    )
    assert all(event["id"] > events[0]["id"] for event in after_first.json()["data"])


def test_streaming_completion_carries_safe_broker_tool_receipt(tmp_path, monkeypatch):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch, runtime=BrokerActivityStubRuntime())
    payload = _payload(workspace, stream=True)

    with client.stream("POST", "/v1/chat/completions", headers=AUTH, json=payload) as response:
        assert response.status_code == 200, response.text
        lines = [line for line in response.iter_lines() if line]

    chunks = [json.loads(line.removeprefix("data: ")) for line in lines if line != "data: [DONE]"]
    reasoning = [
        chunk["choices"][0]["delta"]["reasoning_content"]
        for chunk in chunks
        if "reasoning_content" in chunk["choices"][0]["delta"]
    ]
    assert reasoning == ["Connected tool completed: schedule create.\n"]
    serialized = json.dumps(chunks)
    for forbidden in [
        "glasshive-user-capabilities",
        "gh_scheduling_cortex",
        "private-call-id",
        "private reminder title",
        "private-task-id",
    ]:
        assert forbidden not in serialized


def test_non_streaming_completion_uses_native_usage_when_the_harness_reports_it(tmp_path, monkeypatch):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch, runtime=NativeUsageRuntime())

    response = client.post("/v1/chat/completions", headers=AUTH, json=_payload(workspace))

    assert response.status_code == 200, response.text
    assert response.json()["choices"][0]["message"]["content"] == "Native answer."
    assert response.json()["usage"] == {
        "prompt_tokens": 17,
        "completion_tokens": 4,
        "total_tokens": 21,
    }
    assert response.json()["glasshive"]["usage_source"] == "native"


def test_native_log_window_compaction_is_recorded_in_the_private_session_manifest(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch, runtime=CompactedActivityRuntime())

    response = client.post("/v1/chat/completions", headers=AUTH, json=_payload(workspace))

    assert response.status_code == 200, response.text
    session = client.app.state.store.list_provider_sessions(owner_id="owner-a")[0]
    manifest = json.loads(session["context_manifest_json"])
    assert manifest["compactions"] == [
        {"kind": "native_log_window", "excluded_prefix_bytes": 2048}
    ]
    assert manifest["last_request_id"] == response.json()["id"]


def test_dedicated_activity_sse_recovers_after_last_event_id(tmp_path, monkeypatch):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch, runtime=ActivityStubRuntime())
    completion = client.post("/v1/chat/completions", headers=AUTH, json=_payload(workspace))
    request_id = completion.json()["id"]
    events = client.get(f"/v1/requests/{request_id}/activity", headers=AUTH).json()["data"]

    with client.stream(
        "GET",
        f"/v1/requests/{request_id}/activity",
        headers={
            **AUTH,
            "Accept": "text/event-stream",
            "Last-Event-ID": str(events[0]["id"]),
        },
    ) as response:
        assert response.status_code == 200
        lines = [line for line in response.iter_lines() if line]

    recovered_ids = [int(line.removeprefix("id: ")) for line in lines if line.startswith("id: ")]
    assert recovered_ids == [event["id"] for event in events[1:]]
    assert any(line == "event: completed" for line in lines)


def test_completion_persists_native_activity_once_and_hides_internal_source_ids(tmp_path, monkeypatch):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch, runtime=ActivityStubRuntime())

    response = client.post("/v1/chat/completions", headers=AUTH, json=_payload(workspace))
    request_id = response.json()["id"]
    first = client.get(f"/v1/requests/{request_id}/activity", headers=AUTH).json()["data"]
    second = client.get(f"/v1/requests/{request_id}/activity", headers=AUTH).json()["data"]

    assert [event["event"] for event in first] == ["queued", "started", "tool", "file", "completed"]
    assert len(second) == len(first)
    assert "source_event_id" not in json.dumps(first)


def test_invalid_model_and_missing_authenticated_metadata_fail_loudly(tmp_path, monkeypatch):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    payload = _payload(workspace)
    payload["model"] = "gpt-made-up"

    invalid_model = client.post("/v1/chat/completions", headers=AUTH, json=payload)
    assert invalid_model.status_code == 400
    assert "Unsupported GlassHive model" in invalid_model.text

    payload = _payload(workspace)
    del payload["metadata"]["agent_id"]
    missing = client.post("/v1/chat/completions", headers=AUTH, json=payload)
    assert missing.status_code == 422
    assert "agent_id" in missing.text


def test_relative_custom_workspace_fails_loudly_before_native_execution(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    payload = _payload(tmp_path)
    payload["metadata"]["glasshive_options"]["workspace"]["path"] = "relative/Life"

    response = client.post("/v1/chat/completions", headers=AUTH, json=payload)

    assert response.status_code == 400
    assert "absolute server-side path" in response.text
    assert client.app.state.store.list_provider_sessions(owner_id="owner-a") == []


def test_provider_routes_fail_closed_when_service_authentication_is_not_configured(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("WPR_API_TOKEN", raising=False)
    monkeypatch.setenv("GLASSHIVE_HOST_WORKERS_ENABLED", "1")
    client = TestClient(
        create_app(
            str(tmp_path / "runtime.db"),
            runtime_backend="stub",
            runtime=AgentBuilderTransferRuntime(),
        )
    )

    response = client.get("/v1/models", headers=AUTH)

    assert response.status_code == 503
    assert response.json()["detail"] == "GlassHive provider authentication is not configured"


def test_openai_compatible_request_hydrates_structured_metadata_from_headers(tmp_path, monkeypatch):
    workspace = tmp_path / "Life folder"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    payload = {
        "model": "codex-cli:gpt-5.6-sol",
        "messages": [{"role": "user", "content": "Use the configured workspace."}],
        "stream": False,
        "reasoning_effort": "medium",
    }
    headers = {
        **AUTH,
        "X-Viventium-Conversation-Id": "conv-header",
        "X-GlassHive-Agent-Id": "agent-header",
        "X-Viventium-Message-Id": "message-header",
        "X-Viventium-Stream-Id": "stream-header",
        "X-Viventium-Surface": "telegram",
        "X-Viventium-Input-Mode": "voice_note",
        "X-GlassHive-Idempotency-Key": "idem-header",
        "X-GlassHive-Workspace-Mode": "custom",
        "X-GlassHive-Workspace-Path-B64": base64.b64encode(str(workspace).encode()).decode(),
        "X-GlassHive-Access": "full",
    }

    response = client.post("/v1/chat/completions", headers=headers, json=payload)

    assert response.status_code == 200, response.text
    session = client.app.state.store.list_provider_sessions(owner_id="owner-a")[0]
    assert session["conversation_id"] == "conv-header"
    assert session["agent_id"] == "agent-header"
    assert session["workspace_dir"] == str(workspace.resolve())
    assert session["access_mode"] == "full"


def test_openai_compatible_request_accepts_server_owned_read_only_cortex_access(tmp_path, monkeypatch):
    workspace = tmp_path / "Life folder"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    payload = {
        "model": "codex-cli:gpt-5.6-sol",
        "messages": [{"role": "user", "content": "Inspect without modifying the workspace."}],
        "stream": False,
        "reasoning_effort": "medium",
    }
    headers = {
        **AUTH,
        "X-Viventium-Conversation-Id": "conv-cortex-read-only",
        "X-GlassHive-Agent-Id": "agent-cortex-read-only",
        "X-Viventium-Message-Id": "message-cortex-read-only",
        "X-GlassHive-Idempotency-Key": "idem-cortex-read-only",
        "X-GlassHive-Workspace-Mode": "custom",
        "X-GlassHive-Workspace-Path-B64": base64.b64encode(str(workspace).encode()).decode(),
        "X-GlassHive-Access": "read_only",
    }

    response = client.post("/v1/chat/completions", headers=headers, json=payload)

    assert response.status_code == 200, response.text
    session = client.app.state.store.list_provider_sessions(owner_id="owner-a")[0]
    assert session["access_mode"] == "read_only"


def test_activity_and_cancel_are_owner_scoped(tmp_path, monkeypatch):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    response = client.post("/v1/chat/completions", headers=AUTH, json=_payload(workspace))
    request_id = response.json()["id"]

    denied = client.get(
        f"/v1/requests/{request_id}/activity",
        headers={**AUTH, "X-Viventium-User-Id": "owner-b"},
    )

    assert denied.status_code == 403


def test_cancel_by_idempotency_uses_authenticated_owner_scope(tmp_path, monkeypatch):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    response = client.post("/v1/chat/completions", headers=AUTH, json=_payload(workspace))
    assert response.status_code == 200

    cancelled = client.post("/v1/requests/by-idempotency/idem-a/cancel", headers=AUTH)
    denied = client.post(
        "/v1/requests/by-idempotency/idem-a/cancel",
        headers={**AUTH, "X-Viventium-User-Id": "owner-b"},
    )

    assert cancelled.status_code == 200
    assert cancelled.json()["id"] == response.json()["id"]
    assert cancelled.json()["state"] in {"completed", "cancelled"}
    assert denied.status_code == 200
    assert denied.json() == {"id": "", "object": "glasshive.request", "state": "cancelled"}
    assert client.app.state.store.get_provider_request(response.json()["id"])["owner_id"] == "owner-a"


def test_cancel_by_idempotency_before_request_prevents_a_late_native_start(tmp_path, monkeypatch):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)

    cancelled = client.post("/v1/requests/by-idempotency/idem-a/cancel", headers=AUTH)
    late_start = client.post("/v1/chat/completions", headers=AUTH, json=_payload(workspace))

    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "cancelled"
    assert late_start.status_code == 409
    assert "cancelled before native execution" in late_start.text
    assert client.app.state.store.list_provider_sessions(owner_id="owner-a") == []


def test_graph_family_prestart_stop_blocks_two_late_execution_digests(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    payload = _payload(workspace)
    payload["tools"] = [
        {
            "type": "function",
            "function": {
                "name": "lc_transfer_to_specialist",
                "description": "Consult the specialist using shared graph state.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }
    ]

    cancelled = client.post("/v1/requests/by-idempotency/idem-a/cancel", headers=AUTH)
    first_late = client.post("/v1/chat/completions", headers=AUTH, json=payload)
    second_payload = json.loads(json.dumps(payload))
    second_payload["messages"].append(
        {"role": "assistant", "content": "A later graph-node history."}
    )
    second_late = client.post(
        "/v1/chat/completions", headers=AUTH, json=second_payload
    )

    assert cancelled.status_code == 200, cancelled.text
    assert first_late.status_code == 409, first_late.text
    assert second_late.status_code == 409, second_late.text
    assert client.app.state.store.list_provider_sessions(owner_id="owner-a") == []


def test_graph_family_stop_survives_provider_restart_and_repeated_late_children(
    tmp_path,
    monkeypatch,
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    first_client = _client(tmp_path, monkeypatch)
    payload = _payload(workspace)
    payload["tools"] = [
        {
            "type": "function",
            "function": {
                "name": "lc_transfer_to_specialist",
                "description": "Consult the specialist using shared graph state.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        }
    ]

    stopped = first_client.post(
        "/v1/requests/by-idempotency/idem-a/cancel",
        headers=AUTH,
    )
    restarted_client = TestClient(
        create_app(
            str(tmp_path / "runtime.db"),
            runtime_backend="stub",
            runtime=AgentBuilderTransferRuntime(),
        )
    )
    first_late = restarted_client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json=payload,
    )
    second_payload = json.loads(json.dumps(payload))
    second_payload["messages"].append(
        {"role": "assistant", "content": "A second late graph child after restart."}
    )
    second_late = restarted_client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json=second_payload,
    )
    new_turn_payload = json.loads(json.dumps(second_payload))
    new_turn_payload["metadata"]["idempotency_key"] = "idem-b"
    new_turn = restarted_client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json=new_turn_payload,
    )

    assert stopped.status_code == 200, stopped.text
    assert first_late.status_code == 409, first_late.text
    assert second_late.status_code == 409, second_late.text
    assert new_turn.status_code == 200, new_turn.text


def test_graph_family_stop_tombstone_is_owner_and_tenant_isolated(
    tmp_path,
    monkeypatch,
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)

    stopped = client.post(
        "/v1/requests/by-idempotency/idem-a/cancel",
        headers=AUTH,
    )
    owner_b_payload = _payload(workspace)
    owner_b_payload["metadata"]["owner_id"] = "owner-b"
    owner_b = client.post(
        "/v1/chat/completions",
        headers={**AUTH, "X-Viventium-User-Id": "owner-b"},
        json=owner_b_payload,
    )
    store = client.app.state.store

    assert stopped.status_code == 200, stopped.text
    assert owner_b.status_code == 200, owner_b.text
    assert store.is_provider_stop_tombstone_active(
        tenant_id="local",
        owner_id="owner-a",
        idempotency_keys=("idem-a",),
    )
    assert not store.is_provider_stop_tombstone_active(
        tenant_id="local",
        owner_id="owner-b",
        idempotency_keys=("idem-a",),
    )
    assert not store.is_provider_stop_tombstone_active(
        tenant_id="tenant-b",
        owner_id="owner-a",
        idempotency_keys=("idem-a",),
    )


def test_graph_family_stop_tombstone_expires_after_ttl(
    tmp_path,
    monkeypatch,
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    stopped = client.post(
        "/v1/requests/by-idempotency/idem-a/cancel",
        headers=AUTH,
    )
    with client.app.state.store._connect() as conn:
        conn.execute(
            """
            UPDATE provider_stop_tombstones
            SET expires_at = ?
            WHERE tenant_id = 'local'
              AND owner_id = 'owner-a'
              AND base_idempotency_key = 'idem-a'
            """,
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),),
        )
    restarted_client = TestClient(
        create_app(
            str(tmp_path / "runtime.db"),
            runtime_backend="stub",
            runtime=StubRuntime(),
        )
    )
    after_ttl = restarted_client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json=_payload(workspace),
    )

    assert stopped.status_code == 200, stopped.text
    assert after_ttl.status_code == 200, after_ttl.text


def test_repeated_graph_family_stop_renews_request_retention_lifecycle(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("GLASSHIVE_PROVIDER_REQUEST_RETENTION_DAYS", "2")
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)

    first = client.post(
        "/v1/requests/by-idempotency/idem-a/cancel",
        headers=AUTH,
    )
    with client.app.state.store._connect() as conn:
        first_row = conn.execute(
            "SELECT * FROM provider_stop_tombstones WHERE owner_id = 'owner-a'"
        ).fetchone()
    time.sleep(0.01)
    second = client.post(
        "/v1/requests/by-idempotency/idem-a/cancel",
        headers=AUTH,
    )
    with client.app.state.store._connect() as conn:
        rows = conn.execute(
            "SELECT * FROM provider_stop_tombstones WHERE owner_id = 'owner-a'"
        ).fetchall()

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert len(rows) == 1
    assert datetime.fromisoformat(rows[0]["expires_at"]) > datetime.fromisoformat(
        first_row["expires_at"]
    )
    assert (
        datetime.fromisoformat(rows[0]["expires_at"])
        - datetime.fromisoformat(rows[0]["created_at"])
    ).total_seconds() == pytest.approx(2 * 24 * 60 * 60)


def test_family_stop_database_failure_cannot_return_success(
    tmp_path,
    monkeypatch,
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    provider = client.app.state.conversation_provider

    class OwnerRequest:
        headers = {"x-viventium-user-id": "owner-a"}

    def fail_tombstone_write(**kwargs):
        _ = kwargs
        raise sqlite3.OperationalError("Synthetic tombstone persistence failure")

    monkeypatch.setattr(
        client.app.state.store,
        "upsert_provider_stop_tombstone",
        fail_tombstone_write,
    )

    with pytest.raises(sqlite3.OperationalError, match="persistence failure"):
        provider.cancel_by_idempotency("idem-a", OwnerRequest())


def test_tombstone_race_rejects_start_and_cleans_only_new_native_session(
    tmp_path,
    monkeypatch,
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    store = client.app.state.store
    original_create = store.create_provider_request

    def stop_after_session_creation(**kwargs):
        store.upsert_provider_stop_tombstone(
            tenant_id=str(kwargs["tenant_id"]),
            owner_id=str(kwargs["owner_id"]),
            base_idempotency_key=str(kwargs["base_idempotency_key"]),
            ttl_seconds=30 * 24 * 60 * 60,
        )
        return original_create(**kwargs)

    monkeypatch.setattr(store, "create_provider_request", stop_after_session_creation)

    response = client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json=_payload(workspace),
    )

    assert response.status_code == 409, response.text
    assert store.list_provider_sessions(owner_id="owner-a") == []
    workers = store.list_all_workers()
    assert len(workers) == 1
    assert workers[0]["state"] == "terminated"
    assert store.list_provider_requests_by_idempotency_family(
        tenant_id="local",
        owner_id="owner-a",
        base_idempotency_key="idem-a",
    ) == []


def test_cross_provider_stop_between_request_insert_and_run_assignment_stays_final(
    tmp_path,
    monkeypatch,
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    runtime = JustLateCompletionRuntime()
    client = _client(tmp_path, monkeypatch, runtime=runtime)
    service = client.app.state.service
    store = client.app.state.store
    request_inserted = Event()
    continue_assignment = Event()
    original_assign_run = service.assign_run

    def delayed_assign_run(*args, **kwargs):
        request_inserted.set()
        if not continue_assignment.wait(timeout=5):
            raise RuntimeError("Synthetic run assignment was not released")
        return original_assign_run(*args, **kwargs)

    monkeypatch.setattr(service, "assign_run", delayed_assign_run)

    class OwnerRequest:
        headers = {"x-viventium-user-id": "owner-a"}

    second_provider = ConversationProvider(store, service)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            starting = executor.submit(
                client.post,
                "/v1/chat/completions",
                headers=AUTH,
                json=_payload(workspace),
            )
            assert request_inserted.wait(timeout=2)
            stopped = second_provider.cancel_by_idempotency(
                "idem-a",
                OwnerRequest(),
            )
            continue_assignment.set()
            response = starting.result(timeout=2)

        requests = store.list_provider_requests_by_idempotency_family(
            tenant_id="local",
            owner_id="owner-a",
            base_idempotency_key="idem-a",
        )
        assert stopped["state"] == "cancelled"
        assert response.status_code == 502, response.text
        assert len(requests) == 1
        assert requests[0]["state"] == "cancelled"
        session = store.list_provider_sessions(owner_id="owner-a")[0]
        runs = store.list_runs_for_worker(session["worker_id"])
        assert len(runs) == 1
        assert runs[0]["state"] == "cancelled"
        assert runtime.received_timeouts == []
    finally:
        continue_assignment.set()


def test_run_scoped_interrupt_cannot_cancel_a_newer_active_turn(tmp_path):
    store = Store(str(tmp_path / "runtime.db"))
    runtime = InterruptCountingRuntime()
    service = WorkersProjectsService(store, runtime)
    try:
        project = store.create_project(
            "owner-a",
            "Synthetic conversation",
            "Cancellation scope regression",
            "codex-cli",
        )
        worker = store.create_worker(
            project_id=project["project_id"],
            owner_id="owner-a",
            name="Synthetic worker",
            role="conversation-agent",
            profile="codex-cli",
            backend="",
            runtime="codex-cli",
            model="gpt-5.6-sol",
        )
        active = store.create_run(
            worker["worker_id"],
            project["project_id"],
            "newer turn",
            state="running",
        )

        service.interrupt_worker(worker["worker_id"], run_id="older-request-run")

        assert runtime.interrupt_calls == []
        assert store.get_run(active["run_id"])["state"] == "running"
    finally:
        service.shutdown()


def test_activity_payload_recursively_redacts_private_strings(tmp_path, monkeypatch):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    response = client.post("/v1/chat/completions", headers=AUTH, json=_payload(workspace))
    request_id = response.json()["id"]
    client.app.state.store.add_provider_activity(
        request_id,
        "file",
        "A file changed.",
        {
            "path": "/Users/private-person/Documents/secret-project/file.txt",
            "nested": ["token=super-secret-value"],
        },
    )

    activity = client.get(f"/v1/requests/{request_id}/activity", headers=AUTH)
    serialized = json.dumps(activity.json())

    assert activity.status_code == 200
    assert "private-person" not in serialized
    assert "super-secret-value" not in serialized
    assert "[REDACTED" in serialized


def test_streaming_redactor_handles_secrets_split_across_chunks():
    redactor = StreamingRedactor(overlap=32)

    visible = redactor.feed("Before api_key=PUBLIC_FAKE_")
    visible += redactor.feed("SECRET_VALUE after")
    visible += redactor.flush()

    assert "PUBLIC_FAKE_SECRET_VALUE" not in visible
    assert "[REDACTED]" in visible
    assert GLASSHIVE_MODELS["codex-cli:gpt-5.6-sol"].recommended_effort == "medium"
    assert GLASSHIVE_MODELS["claude-code:opus"].recommended_effort == "high"


def test_streaming_redactor_preserves_visible_line_boundaries_while_sanitizing():
    redactor = StreamingRedactor()

    visible = redactor.feed("First line.\n")
    visible += redactor.feed("Second line.\n")
    visible += redactor.flush()

    assert visible == "First line.\nSecond line.\n"


def test_agent_builder_fallback_key_starts_a_distinct_claude_provider_attempt(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    primary_payload = _payload(workspace, model="codex-cli:gpt-5.6-sol", stream=False)
    common_headers = {
        **AUTH,
        "X-Viventium-Conversation-Id": "conv-fallback",
        "X-GlassHive-Agent-Id": "agent-main",
        "X-Viventium-Message-Id": "response-1",
        "X-GlassHive-Fallback-Model": "",
        "X-GlassHive-Fallback-Reasoning-Effort": "",
    }

    primary = client.post(
        "/v1/chat/completions",
        headers={**common_headers, "X-GlassHive-Idempotency-Key": "main:response-1"},
        json=primary_payload,
    )
    fallback_payload = _payload(workspace, model="claude-code:opus", stream=False)
    fallback_payload["reasoning_effort"] = "max"
    fallback = client.post(
        "/v1/chat/completions",
        headers={
            **common_headers,
            "X-GlassHive-Idempotency-Key": "main-fallback:response-1",
        },
        json=fallback_payload,
    )

    assert primary.status_code == 200, primary.text
    assert fallback.status_code == 200, fallback.text
    assert primary.json()["id"] != fallback.json()["id"]
    requests = [
        client.app.state.store.get_provider_request(primary.json()["id"]),
        client.app.state.store.get_provider_request(fallback.json()["id"]),
    ]
    assert {request["idempotency_key"] for request in requests} == {
        "main:response-1",
        "main-fallback:response-1",
    }
    assert all(not request["fallback_model_id"] for request in requests)
    sessions = client.app.state.store.list_provider_sessions(owner_id="owner-a")
    assert len(sessions) == 1
    assert sessions[0]["model_id"] == "claude-code:opus"


def test_streaming_redactor_redacts_newline_split_home_path_and_bounds_long_lines():
    redactor = StreamingRedactor(overlap=16, max_buffer=64)

    visible = redactor.feed("Path /Users/synthetic/\nDocuments/private.txt\n")
    visible += redactor.feed("x" * 65)
    visible += redactor.flush()

    assert "/Users/synthetic" not in visible
    assert "[REDACTED_LOCAL_PATH]" in visible
    assert "[REDACTED_OVERSIZED_STREAM_SEGMENT]" in visible


def test_production_sse_redacts_a_secret_split_across_native_stream_snapshots(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch, runtime=SplitSecretStreamingRuntime())
    payload = _payload(workspace, model="claude-code:opus", stream=True)

    with client.stream("POST", "/v1/chat/completions", headers=AUTH, json=payload) as response:
        assert response.status_code == 200, response.text
        serialized = "\n".join(line for line in response.iter_lines() if line)

    assert "PUBLIC_FAKE_SECRET_VALUE" not in serialized
    assert "PUBLIC_FAKE_" not in serialized
    assert "[REDACTED]" in serialized
    assert serialized.endswith("data: [DONE]")


def test_native_visible_text_never_falls_back_to_raw_ndjson_or_thinking():
    raw = "\n".join(
        [
            "not-json token=synthetic-private-value",
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "thinking", "thinking": "hidden private reasoning"},
                            {"type": "tool_use", "name": "Bash", "input": {"command": "secret"}},
                            {"type": "text", "text": "Safe visible answer."},
                        ]
                    },
                }
            ),
        ]
    )

    assert _native_visible_text("claude-code", raw) == "Safe visible answer."
    assert _native_visible_text("unknown-profile", raw) == ""


def test_native_visible_text_prefers_claude_structured_output_result():
    raw = "\n".join(
        [
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "Private schema work in progress."}
                        ]
                    },
                }
            ),
            json.dumps(
                {
                    "type": "result",
                    "structured_output": {
                        "type": "tool_call",
                        "content": "",
                        "tool_name": "lc_transfer_to_specialist",
                    },
                }
            ),
        ]
    )

    assert json.loads(_native_visible_text("claude-code", raw)) == {
        "type": "tool_call",
        "content": "",
        "tool_name": "lc_transfer_to_specialist",
    }


def test_native_usage_ignores_non_usage_events_and_normalizes_counts():
    stdout = "\n".join(
        [
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "Hi"}}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 11, "output_tokens": 3}}),
        ]
    )

    assert _native_usage("codex-cli", stdout) == {
        "prompt_tokens": 11,
        "completion_tokens": 3,
        "total_tokens": 14,
    }


def test_provider_startup_prunes_only_old_terminal_requests_and_idle_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_PROVIDER_REQUEST_RETENTION_DAYS", "30")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_SESSION_RETENTION_DAYS", "90")
    store = Store(str(tmp_path / "runtime.db"))
    runtime = InterruptCountingRuntime()
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    try:
        project = store.create_project("owner-a", "Old conversation", "retention", "codex-cli")
        worker = store.create_worker(
            project_id=project["project_id"],
            owner_id="owner-a",
            name="Old worker",
            role="conversation-agent",
            profile="codex-cli",
            backend="",
            runtime="codex-cli",
            model="gpt-5.6-sol",
        )
        session = store.upsert_provider_session(
            tenant_id="local",
            owner_id="owner-a",
            conversation_id="conv-old",
            agent_id="agent-a",
            model_id="codex-cli:gpt-5.6-sol",
            project_id=project["project_id"],
            worker_id=worker["worker_id"],
            workspace_dir=str(tmp_path),
            access_mode="full",
            history_count=2,
            context_manifest={"messages": 2},
        )
        request, _ = store.create_provider_request(
            tenant_id="local",
            owner_id="owner-a",
            session_id=session["session_id"],
            idempotency_key="old-terminal",
            message_id="message-old",
            stream_id="stream-old",
            requested_history_count=1,
        )
        store.update_provider_request(request["request_id"], state="completed")
        store.add_provider_activity(request["request_id"], "completed", "Completed")
        old = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
        with store._connect() as conn:
            conn.execute(
                "UPDATE provider_requests SET created_at = ?, updated_at = ? WHERE request_id = ?",
                (old, old, request["request_id"]),
            )
            conn.execute(
                "UPDATE provider_sessions SET created_at = ?, updated_at = ? WHERE session_id = ?",
                (old, old, session["session_id"]),
            )

        ConversationProvider(store, service)

        assert store.get_provider_request(request["request_id"]) is None
        assert store.list_provider_sessions(owner_id="owner-a") == []
        assert store.get_worker(worker["worker_id"])["state"] == "terminated"
    finally:
        service.shutdown()


def test_codex_native_events_become_safe_tool_file_plan_and_reasoning_activity():
    stdout = "\n".join(
        [
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "reasoning", "text": "private hidden reasoning"},
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "todo_list",
                        "items": [{"text": "Inspect private records", "completed": True}],
                    },
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "command_execution",
                        "command": "curl -H 'Authorization: Bearer synthetic-secret-value' example.invalid",
                        "aggregated_output": "token=synthetic-secret-value",
                        "exit_code": 0,
                        "status": "completed",
                    },
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "file_change",
                        "changes": [{"path": "/Users/private-person/Documents/private.txt"}],
                        "status": "completed",
                    },
                }
            ),
        ]
    )

    events = _normalized_harness_activity("codex-cli", stdout)

    assert [event["event_type"] for event in events] == [
        "reasoning-summary",
        "plan",
        "tool",
        "file",
    ]
    serialized = json.dumps(events)
    assert "private hidden reasoning" not in serialized
    assert "synthetic-secret-value" not in serialized
    assert "private-person" not in serialized
    assert events[2]["payload"]["source_event_id"].startswith("codex-cli:")
    assert events[2]["payload"]["source_event_id"].endswith(":0")
    assert {key: value for key, value in events[2]["payload"].items() if key != "source_event_id"} == {
        "tool": "shell",
        "status": "completed",
        "exit_code": 0,
    }
    assert events[3]["payload"]["change_count"] == 1


def test_codex_broker_tool_completion_exposes_only_safe_task_and_status():
    stdout = json.dumps(
        {
            "type": "event_msg",
            "payload": {
                "type": "mcp_tool_call_end",
                "call_id": "private-call-id",
                "duration": 1.25,
                "invocation": {
                    "server": "glasshive-user-capabilities",
                    "tool": "gh_scheduling_cortex__schedule_create",
                    "arguments": {
                        "title": "private reminder title",
                        "token": "synthetic-secret-value",
                    },
                },
                "result": {
                    "Ok": {
                        "content": [{"type": "text", "text": "private result"}],
                        "isError": False,
                    }
                },
            },
        }
    )

    events = _normalized_harness_activity("codex-cli", stdout)

    assert len(events) == 1
    assert events[0]["event_type"] == "tool"
    assert events[0]["summary"] == "Connected tool completed: schedule create."
    assert {
        key: value
        for key, value in events[0]["payload"].items()
        if key != "source_event_id"
    } == {
        "tool": "connected_tool",
        "task": "schedule create",
        "status": "completed",
    }
    serialized = json.dumps(events)
    for forbidden in [
        "glasshive-user-capabilities",
        "gh_scheduling_cortex",
        "private-call-id",
        "private reminder title",
        "synthetic-secret-value",
        "private-task-id",
    ]:
        assert forbidden not in serialized


@pytest.mark.parametrize(
    "result",
    [
        {"Ok": {"content": [], "isError": True}},
        {"Ok": {"content": [], "is_error": True}},
        {"Err": "synthetic provider failure"},
    ],
)
def test_codex_broker_tool_real_result_failures_emit_failed_receipts(result):
    stdout = json.dumps(
        {
            "type": "event_msg",
            "payload": {
                "type": "mcp_tool_call_end",
                "call_id": "private-call-id",
                "invocation": {
                    "server": "glasshive-user-capabilities",
                    "tool": "gh_scheduling_cortex__schedule_create",
                    "arguments": {"title": "private reminder title"},
                },
                "result": result,
            },
        }
    )

    events = _normalized_harness_activity("codex-cli", stdout)

    assert len(events) == 1
    assert events[0]["summary"] == "Connected tool failed: schedule create."
    assert events[0]["payload"]["status"] == "failed"
    serialized = json.dumps(events)
    assert "synthetic provider failure" not in serialized
    assert "private reminder title" not in serialized


@pytest.mark.parametrize(
    "result",
    [
        {
            "content": [
                {
                    "type": "text",
                    "text": (
                        "1 validation error for call\n"
                        "schedule.type\nField required [type=missing, input_value={}, input_type=dict]\n"
                        "For further information visit https://errors.pydantic.dev/2.12/v/missing"
                    ),
                }
            ],
            "structured_content": None,
        },
        {
            "content": [{"type": "text", "text": "Private blocked result"}],
            "structured_content": {
                "status": "blocked",
                "reason": "private broker reason",
            },
        },
        {
            "status": "completed",
            "content": [{"type": "text", "text": "Private blocked result"}],
            "structured_content": {
                "status": "blocked",
                "reason": "private broker reason",
            },
        },
    ],
)
def test_codex_completed_item_preserves_structured_mcp_failure_truth(result):
    stdout = json.dumps(
        {
            "type": "item.completed",
            "item": {
                "id": "private-call-id",
                "type": "mcp_tool_call",
                "tool": "gh_scheduling_cortex__schedule_create",
                "status": "completed",
                "arguments": {"private": "synthetic-secret-value"},
                "result": result,
            },
        }
    )

    events = _normalized_harness_activity("codex-cli", stdout)

    assert len(events) == 1
    assert events[0]["summary"] == "Connected tool failed: schedule create."
    assert events[0]["payload"]["status"] == "failed"
    serialized = json.dumps(events)
    assert "synthetic-secret-value" not in serialized
    assert "private broker reason" not in serialized


@pytest.mark.parametrize(
    "result",
    [
        {
            "isError": False,
            "content": [{"type": "text", "text": "Private successful retrieval"}],
            "structured_content": {"error": "domain evidence, not an execution error"},
        },
        {
            "success": True,
            "content": [
                {
                    "type": "text",
                    "text": (
                        "Retrieved document: 1 validation error for call\n"
                        "field\nField required [type=missing]\n"
                        "https://errors.pydantic.dev/2.12/v/missing"
                    ),
                }
            ],
        },
    ],
)
def test_codex_explicit_success_is_not_overridden_by_domain_result_data(result):
    stdout = json.dumps(
        {
            "type": "item.completed",
            "item": {
                "id": "private-call-id",
                "type": "mcp_tool_call",
                "tool": "gh_retrieval__read",
                "status": "completed",
                "result": result,
            },
        }
    )

    events = _normalized_harness_activity("codex-cli", stdout)

    assert len(events) == 1
    assert events[0]["summary"] == "Connected tool completed: read."
    assert events[0]["payload"]["status"] == "completed"
    serialized = json.dumps(events)
    assert "domain evidence" not in serialized
    assert "validation error" not in serialized


def test_codex_duplicate_native_views_emit_one_terminal_tool_receipt():
    stdout = "\n".join(
        [
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "mcp_tool_call_end",
                        "call_id": "shared-private-call-id",
                        "invocation": {
                            "server": "glasshive-user-capabilities",
                            "tool": "gh_scheduling_cortex__schedule_create",
                            "arguments": {},
                        },
                        "result": {"ok": True},
                    },
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "shared-private-call-id",
                        "type": "mcp_tool_call",
                        "tool": "gh_scheduling_cortex__schedule_create",
                        "status": "completed",
                    },
                }
            ),
        ]
    )

    events = _normalized_harness_activity("codex-cli", stdout)

    terminal = [event for event in events if event["payload"].get("status") == "completed"]
    assert len(terminal) == 1
    assert terminal[0]["summary"] == "Connected tool completed: schedule create."


def test_claude_broker_tool_result_emits_one_safe_terminal_receipt():
    stdout = "\n".join(
        [
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "private-tool-use-id",
                                "name": (
                                    "mcp__glasshive-user-capabilities__"
                                    "gh_scheduling_cortex__schedule_create"
                                ),
                                "input": {"title": "private reminder title"},
                            }
                        ]
                    },
                }
            ),
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "private-tool-use-id",
                                "content": "private result with synthetic-secret-value",
                                "is_error": False,
                            }
                        ]
                    },
                }
            ),
        ]
    )

    events = _normalized_harness_activity("claude-code", stdout)

    terminal = [event for event in events if event["payload"].get("status") == "completed"]
    assert len(terminal) == 1
    assert terminal[0]["summary"] == "Connected tool completed: schedule create."
    assert terminal[0]["payload"]["task"] == "schedule create"
    serialized = json.dumps(events)
    for forbidden in [
        "glasshive-user-capabilities",
        "gh_scheduling_cortex",
        "private-tool-use-id",
        "private reminder title",
        "synthetic-secret-value",
    ]:
        assert forbidden not in serialized


@pytest.mark.parametrize("is_error, expected_status", [(False, "completed"), (True, "failed")])
def test_claude_duplicate_tool_results_emit_one_terminal_receipt(is_error, expected_status):
    tool_use_id = "private-tool-use-id"
    tool_result = {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": "private result",
                    "is_error": is_error,
                }
            ]
        },
    }
    stdout = "\n".join(
        [
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": tool_use_id,
                                "name": (
                                    "mcp__glasshive-user-capabilities__"
                                    "gh_scheduling_cortex__schedule_create"
                                ),
                                "input": {"title": "private reminder title"},
                            }
                        ]
                    },
                }
            ),
            json.dumps(tool_result),
            json.dumps(tool_result),
        ]
    )

    events = _normalized_harness_activity("claude-code", stdout)

    terminal = [
        event
        for event in events
        if event["payload"].get("status") in {"completed", "failed", "cancelled"}
    ]
    assert len(terminal) == 1
    assert terminal[0]["payload"]["status"] == expected_status
    assert tool_use_id not in json.dumps(events)


def test_identical_native_tool_events_keep_distinct_stable_source_ids():
    event = json.dumps(
        {
            "type": "item.completed",
            "item": {"type": "command_execution", "status": "completed", "exit_code": 0},
        }
    )

    events = _normalized_harness_activity("codex-cli", f"{event}\n{event}\n")

    assert len(events) == 2
    assert events[0]["payload"]["source_event_id"] != events[1]["payload"]["source_event_id"]


def test_claude_stream_events_expose_tool_categories_without_tool_inputs_or_thinking():
    stdout = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "hidden internal reasoning"},
                    {
                        "type": "tool_use",
                        "name": "Edit",
                        "input": {"file_path": "/Users/private-person/Documents/private.txt"},
                    },
                    {
                        "type": "tool_use",
                        "name": "WebSearch",
                        "input": {"query": "private query"},
                    },
                ]
            },
        }
    )

    events = _normalized_harness_activity("claude-code", stdout)

    assert [event["event_type"] for event in events] == ["file", "tool"]
    assert events[0]["payload"]["tool"] == "file"
    assert events[1]["payload"]["tool"] == "web_search"
    serialized = json.dumps(events)
    assert "hidden internal reasoning" not in serialized
    assert "private-person" not in serialized
    assert "private query" not in serialized
