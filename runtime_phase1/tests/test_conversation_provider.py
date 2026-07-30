from __future__ import annotations

import base64
import hashlib
import hmac
import json
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from workers_projects_runtime.api import create_app
from workers_projects_runtime.conversation_provider import (
    GLASSHIVE_MODELS,
    ChatMessage,
    ConversationProvider,
    StreamingRedactor,
    _harness_auth_configured,
    _history_instruction,
    _native_usage,
    _native_visible_text,
    _normalized_harness_activity,
    _system_snapshot,
)
from workers_projects_runtime.openclaw_runtime import StubRuntime
from workers_projects_runtime.service import WorkersProjectsService
from workers_projects_runtime.store import Store

AUTH = {
    "Authorization": "Bearer provider-test-token",
    "X-Viventium-User-Id": "owner-a",
}

BOOTSTRAP_SIGNATURE_SECRET = "synthetic-bootstrap-signature-secret"


def test_system_snapshot_preserves_all_current_request_instruction_messages():
    messages = [
        ChatMessage(role="system", content="Current agent instructions."),
        ChatMessage(role="user", content="Hello."),
        ChatMessage(role="system", content="Current conversation policy."),
    ]

    snapshot = _system_snapshot(messages)

    assert snapshot.count("Current agent instructions.") == 1
    assert snapshot.count("Current conversation policy.") == 1
    assert snapshot.index("Current agent instructions.") < snapshot.index(
        "Current conversation policy."
    )


def test_system_snapshot_deduplicates_identical_instruction_messages():
    messages = [
        ChatMessage(role="system", content="Shared instruction."),
        ChatMessage(role="system", content="Shared instruction."),
    ]

    assert _system_snapshot(messages) == "Shared instruction."


def test_system_snapshot_preserves_openai_developer_instructions():
    messages = [
        ChatMessage(role="developer", content="Application-owned instruction."),
        ChatMessage(role="user", content="Hello."),
    ]

    assert _system_snapshot(messages) == "Application-owned instruction."


def test_history_instruction_excludes_system_messages_from_visible_transcript():
    messages = [
        ChatMessage(role="system", content="Current agent instructions."),
        ChatMessage(role="user", content="Hello."),
        ChatMessage(role="developer", content="Application-owned instruction."),
        ChatMessage(role="system", content="Current conversation policy."),
    ]

    instruction = _history_instruction(messages)

    assert instruction.count("Current agent instructions.") == 1
    assert instruction.count("Current conversation policy.") == 1
    assert instruction.count("Application-owned instruction.") == 1
    assert instruction.count("[system]") == 1
    assert "[developer]" not in instruction
    assert "[user]\nHello." in instruction


def _signed_bundle_headers(bundle: dict, *, timestamp: int | None = None) -> dict[str, str]:
    encoded = base64.b64encode(json.dumps(bundle, separators=(",", ":")).encode()).decode()
    issued_at = str(timestamp if timestamp is not None else int(time.time()))
    signature = hmac.new(
        BOOTSTRAP_SIGNATURE_SECRET.encode(),
        f"v1\n{issued_at}\n{encoded}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return {
        "X-GlassHive-Bootstrap-Bundle-B64": encoded,
        "X-GlassHive-Bootstrap-Timestamp": issued_at,
        "X-GlassHive-Bootstrap-Signature": f"sha256={signature}",
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
    monkeypatch.setenv("WPR_API_TOKEN", "runtime-admin-token")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_API_KEY", "provider-test-token")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_PRINCIPAL_ID", "owner-a")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_TENANT_ID", "local")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_TRUST_IDENTITY_HEADERS", "1")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_ALLOW_FULL_ACCESS", "1")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_DEFAULT_ACCESS", "full")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_DEFAULT_WORKSPACE", str(tmp_path))
    monkeypatch.setenv(
        "VIVENTIUM_GLASSHIVE_CAPABILITY_BROKER_SECRET",
        BOOTSTRAP_SIGNATURE_SECRET,
    )
    monkeypatch.setenv("GLASSHIVE_HOST_WORKERS_ENABLED", "1")
    monkeypatch.setenv("GLASSHIVE_ALLOWED_WORKER_PROFILES", "codex-cli,claude-code")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_ALLOWED_WORKSPACE_ROOTS", str(tmp_path))
    return TestClient(
        create_app(
            str(tmp_path / "runtime.db"),
            runtime_backend="stub",
            runtime=runtime or StubRuntime(),
        )
    )


def _scoped_client(
    tmp_path: Path,
    monkeypatch,
    runtime=None,
    *,
    trust_identity_headers: bool = False,
    allow_full_access: bool = False,
) -> TestClient:
    monkeypatch.setenv("WPR_API_TOKEN", "runtime-admin-token")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_API_KEY", "provider-test-token")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_PRINCIPAL_ID", "owner-a")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_TENANT_ID", "local")
    monkeypatch.setenv(
        "GLASSHIVE_PROVIDER_TRUST_IDENTITY_HEADERS",
        "1" if trust_identity_headers else "0",
    )
    monkeypatch.setenv(
        "GLASSHIVE_PROVIDER_ALLOW_FULL_ACCESS",
        "1" if allow_full_access else "0",
    )
    monkeypatch.setenv("GLASSHIVE_PROVIDER_DEFAULT_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("GLASSHIVE_PROVIDER_ALLOWED_WORKSPACE_ROOTS", str(tmp_path))
    monkeypatch.setenv(
        "VIVENTIUM_GLASSHIVE_CAPABILITY_BROKER_SECRET",
        BOOTSTRAP_SIGNATURE_SECRET,
    )
    monkeypatch.setenv("GLASSHIVE_HOST_WORKERS_ENABLED", "1")
    monkeypatch.setenv("GLASSHIVE_ALLOWED_WORKER_PROFILES", "codex-cli,claude-code")
    return TestClient(
        create_app(
            str(tmp_path / "runtime.db"),
            runtime_backend="stub",
            runtime=runtime or StubRuntime(),
        )
    )


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
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {"type": "agent_message", "text": "Activity answer."},
                        }
                    ),
                    json.dumps({"type": "turn.completed"}),
                ]
            ),
        )


class InterruptCountingRuntime(StubRuntime):
    def __init__(self):
        super().__init__()
        self.interrupt_calls: list[tuple[str, str | None]] = []

    def interrupt_worker(self, worker: dict, run_id: str | None = None):
        self.interrupt_calls.append((str(worker["worker_id"]), run_id))
        return super().interrupt_worker(worker)


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
        self.stdout += "\n" + json.dumps(
            {
                "type": "result",
                "result": "Before api_key=PUBLIC_FAKE_SECRET_VALUE after\n",
            }
        )
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
                    json.dumps({"type": "turn.completed"}),
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
    assert models["claude-code:opus"]["display_name"] == "Claude / Opus"
    assert models["claude-code:opus"]["effort_choices"] == [
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    ]
    assert all(model["capabilities"]["activity_stream"] for model in models.values())
    assert all(model["capabilities"]["conversation_session"] for model in models.values())
    assert all(model["capabilities"]["chat_completions"] for model in models.values())
    assert all(model["capabilities"]["responses_api"] for model in models.values())
    assert models["codex-cli:gpt-5.6-sol"]["capabilities"]["incremental_text"] is False
    assert models["claude-code:opus"]["capabilities"]["incremental_text"] is False
    assert all(model["readiness"]["status"] for model in models.values())
    assert all(isinstance(model["created"], int) and model["created"] > 0 for model in models.values())


def test_provider_credential_is_scoped_and_standard_request_needs_no_viventium_headers(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GLASSHIVE_MCP_API_KEY", "mcp-test-token")
    client = _scoped_client(tmp_path, monkeypatch)

    models = client.get(
        "/v1/models",
        headers={"Authorization": "Bearer provider-test-token"},
    )
    admin_on_provider = client.get(
        "/v1/models",
        headers={"Authorization": "Bearer runtime-admin-token"},
    )
    mcp_on_provider = client.get(
        "/v1/models",
        headers={"Authorization": "Bearer mcp-test-token"},
    )
    provider_on_runtime = client.get(
        "/v1/projects",
        headers={"Authorization": "Bearer provider-test-token"},
    )
    completion = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer provider-test-token"},
        json={
            "model": "codex-cli:gpt-5.6-sol",
            "messages": [{"role": "user", "content": "Portable hello."}],
        },
    )

    assert models.status_code == 200
    assert admin_on_provider.status_code == 401
    assert mcp_on_provider.status_code == 401
    assert mcp_on_provider.json()["error"]["code"] == "invalid_api_key"
    assert provider_on_runtime.status_code == 401
    assert completion.status_code == 200, completion.text
    sessions = client.app.state.store.list_provider_sessions(owner_id="owner-a")
    assert len(sessions) == 1
    assert sessions[0]["tenant_id"] == "local"
    assert sessions[0]["workspace_dir"] == str(tmp_path.resolve())
    assert sessions[0]["access_mode"] == "workspace"


def test_standard_responses_request_uses_the_same_provider_core(tmp_path, monkeypatch):
    client = _scoped_client(tmp_path, monkeypatch)

    response = client.post(
        "/v1/responses",
        headers={"Authorization": "Bearer provider-test-token"},
        json={
            "model": "codex-cli:gpt-5.6-sol",
            "instructions": "Be concise.",
            "input": "Portable Responses hello.",
            "reasoning": {"effort": "medium"},
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"].startswith("resp_")
    assert body["object"] == "response"
    assert body["status"] == "completed"
    assert body["model"] == "codex-cli:gpt-5.6-sol"
    assert body["output"][0]["type"] == "message"
    assert body["output"][0]["role"] == "assistant"
    assert body["output"][0]["content"][0]["type"] == "output_text"
    assert "Portable Responses hello." in body["output_text"]
    assert body["usage"]["total_tokens"] > 0
    assert body["glasshive"]["activity_url"].endswith("/activity")
    sessions = client.app.state.store.list_provider_sessions(owner_id="owner-a")
    assert len(sessions) == 1
    request = client.app.state.store.get_provider_request(body["glasshive"]["request_id"])
    assert request["session_id"] == sessions[0]["session_id"]


def test_responses_previous_response_id_reuses_only_the_authenticated_session(
    tmp_path, monkeypatch
):
    client = _client(tmp_path, monkeypatch)
    first = client.post(
        "/v1/responses",
        headers=AUTH,
        json={
            "model": "codex-cli:gpt-5.6-sol",
            "input": [
                {"role": "developer", "content": "Answer naturally."},
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "First turn."}],
                },
            ],
        },
    )
    assert first.status_code == 200, first.text

    follow_up = client.post(
        "/v1/responses",
        headers=AUTH,
        json={
            "model": "codex-cli:gpt-5.6-sol",
            "input": "Second turn.",
            "previous_response_id": first.json()["id"],
        },
    )
    cross_owner = client.post(
        "/v1/responses",
        headers={
            "Authorization": "Bearer provider-test-token",
            "X-Viventium-User-Id": "owner-b",
        },
        json={
            "model": "codex-cli:gpt-5.6-sol",
            "input": "Attempt cross-owner resume.",
            "previous_response_id": first.json()["id"],
        },
    )

    assert follow_up.status_code == 200, follow_up.text
    first_request = client.app.state.store.get_provider_request(
        first.json()["glasshive"]["request_id"]
    )
    second_request = client.app.state.store.get_provider_request(
        follow_up.json()["glasshive"]["request_id"]
    )
    assert first_request["session_id"] == second_request["session_id"]
    assert follow_up.json()["previous_response_id"] == first.json()["id"]
    assert cross_owner.status_code == 403
    assert cross_owner.json()["error"]["code"] == "permission_denied"


def test_responses_stream_emits_typed_monotonic_events(tmp_path, monkeypatch):
    client = _scoped_client(tmp_path, monkeypatch)

    with client.stream(
        "POST",
        "/v1/responses",
        headers={"Authorization": "Bearer provider-test-token"},
        json={
            "model": "codex-cli:gpt-5.6-sol",
            "input": "Portable Responses stream.",
            "stream": True,
        },
    ) as response:
        assert response.status_code == 200, response.text
        lines = [line for line in response.iter_lines() if line]

    events = []
    current_event = ""
    for line in lines:
        if line.startswith("event: "):
            current_event = line.removeprefix("event: ")
        elif line.startswith("data: "):
            payload = json.loads(line.removeprefix("data: "))
            assert payload["type"] == current_event
            events.append(payload)
    event_types = [event["type"] for event in events]
    assert event_types[0] == "response.created"
    assert "response.output_text.delta" in event_types
    assert event_types[-1] == "response.completed"
    assert [event["sequence_number"] for event in events] == list(range(len(events)))
    assert events[-1]["response"]["status"] == "completed"
    assert "Portable Responses stream." in events[-1]["response"]["output_text"]


def test_responses_unsupported_or_non_text_shapes_fail_visibly(tmp_path, monkeypatch):
    client = _scoped_client(tmp_path, monkeypatch)

    unsupported_tools = client.post(
        "/v1/responses",
        headers={"Authorization": "Bearer provider-test-token"},
        json={
            "model": "codex-cli:gpt-5.6-sol",
            "input": "Hello.",
            "tools": [{"type": "function", "name": "wrapper_tool"}],
        },
    )
    unsupported_item = client.post(
        "/v1/responses",
        headers={"Authorization": "Bearer provider-test-token"},
        json={
            "model": "codex-cli:gpt-5.6-sol",
            "input": [{"type": "computer_call_output", "call_id": "call-1", "output": "x"}],
        },
    )

    assert unsupported_tools.status_code == 400
    assert unsupported_tools.json()["error"]["code"] == "unsupported_parameter"
    assert unsupported_tools.json()["error"]["param"] == "tools"
    assert unsupported_item.status_code == 400
    assert unsupported_item.json()["error"]["code"] == "invalid_request"


def test_standard_stream_options_and_user_are_openai_compatible(tmp_path, monkeypatch):
    client = _scoped_client(tmp_path, monkeypatch)

    with client.stream(
        "POST",
        "/v1/chat/completions",
        headers={"Authorization": "Bearer provider-test-token"},
        json={
            "model": "codex-cli:gpt-5.6-sol",
            "messages": [{"role": "user", "content": "Portable stream."}],
            "stream": True,
            "stream_options": {"include_usage": True},
            "user": "portable-client-user",
        },
    ) as response:
        assert response.status_code == 200
        lines = [line for line in response.iter_lines() if line]

    chunks = [
        json.loads(line.removeprefix("data: "))
        for line in lines
        if line != "data: [DONE]"
    ]
    assert chunks[-1]["usage"]["total_tokens"] > 0
    assert chunks[-1]["choices"] == []
    assert lines[-1] == "data: [DONE]"


def test_identity_delegation_and_full_access_require_server_side_grants(tmp_path, monkeypatch):
    client = _scoped_client(tmp_path, monkeypatch)
    payload = _payload(tmp_path)
    payload["metadata"]["owner_id"] = "owner-b"

    impersonation = client.post(
        "/v1/chat/completions",
        headers={
            "Authorization": "Bearer provider-test-token",
            "X-Viventium-User-Id": "owner-b",
        },
        json=payload,
    )

    payload = _payload(tmp_path)
    payload["metadata"]["glasshive_options"]["access"] = "full"
    full_access = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer provider-test-token"},
        json=payload,
    )

    assert impersonation.status_code == 403
    assert full_access.status_code == 403
    assert impersonation.json()["error"]["code"] == "permission_denied"
    assert full_access.json()["error"]["code"] == "permission_denied"


def test_trusted_service_credential_can_delegate_owner_and_full_access(tmp_path, monkeypatch):
    client = _scoped_client(
        tmp_path,
        monkeypatch,
        trust_identity_headers=True,
        allow_full_access=True,
    )
    payload = _payload(tmp_path)
    payload["metadata"]["owner_id"] = "owner-b"
    payload["metadata"]["glasshive_options"]["access"] = "full"

    response = client.post(
        "/v1/chat/completions",
        headers={
            "Authorization": "Bearer provider-test-token",
            "X-Viventium-User-Id": "owner-b",
        },
        json=payload,
    )

    assert response.status_code == 200, response.text
    sessions = client.app.state.store.list_provider_sessions(owner_id="owner-b")
    assert len(sessions) == 1
    assert sessions[0]["access_mode"] == "full"


def test_standard_ignored_parameters_and_unsupported_shapes_use_openai_error_envelope(
    tmp_path, monkeypatch
):
    client = _scoped_client(tmp_path, monkeypatch)

    tolerated = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer provider-test-token"},
        json={
            "model": "codex-cli:gpt-5.6-sol",
            "messages": [{"role": "user", "content": "Hello."}],
            "temperature": 0.2,
            "top_p": 0.9,
            "max_tokens": 100,
            "presence_penalty": 0,
            "frequency_penalty": 0,
            "seed": 7,
            "store": False,
        },
    )
    unsupported = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer provider-test-token"},
        json={
            "model": "codex-cli:gpt-5.6-sol",
            "messages": [{"role": "user", "content": "Hello."}],
            "tools": [{"type": "function", "function": {"name": "unsafe_shape"}}],
        },
    )
    invalid = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer provider-test-token"},
        json={"model": "codex-cli:gpt-5.6-sol", "messages": []},
    )

    assert tolerated.status_code == 200, tolerated.text
    assert unsupported.status_code == 400
    assert unsupported.json()["error"]["type"] == "invalid_request_error"
    assert unsupported.json()["error"]["code"] == "unsupported_parameter"
    assert invalid.status_code == 400
    assert invalid.json()["error"]["type"] == "invalid_request_error"
    assert invalid.json()["error"]["code"] == "invalid_request"


def test_identical_requests_without_explicit_idempotency_start_distinct_runs(
    tmp_path, monkeypatch
):
    client = _scoped_client(tmp_path, monkeypatch)
    request = {
        "model": "codex-cli:gpt-5.6-sol",
        "messages": [{"role": "user", "content": "Run this twice."}],
    }
    headers = {"Authorization": "Bearer provider-test-token"}

    first = client.post("/v1/chat/completions", headers=headers, json=request)
    second = client.post("/v1/chat/completions", headers=headers, json=request)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["id"] != second.json()["id"]


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


def test_normal_resumed_turn_sends_only_new_visible_messages_to_native_session(tmp_path, monkeypatch):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    first_payload = _payload(workspace)
    first = client.post("/v1/chat/completions", headers=AUTH, json=first_payload)
    assert first.status_code == 200

    second_payload = _payload(workspace)
    second_payload["metadata"]["message_id"] = "message-normal-follow-up"
    second_payload["metadata"]["idempotency_key"] = "idem-normal-follow-up"
    second_payload["messages"] = [
        {"role": "system", "content": "Use only the current system snapshot."},
        first_payload["messages"][1],
        {"role": "assistant", "content": "Prior assistant answer."},
        {"role": "user", "content": "Only this correction is new."},
    ]

    second = client.post("/v1/chat/completions", headers=AUTH, json=second_payload)

    assert second.status_code == 200
    content = second.json()["choices"][0]["message"]["content"]
    assert "Use only the current system snapshot." in content
    assert "Be a thoughtful assistant." not in content
    assert "Only this correction is new." in content
    assert "Prior assistant answer." not in content
    assert "Hello from LIFE." not in content


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
    client = _client(tmp_path, monkeypatch)
    payload = _payload(workspace)
    broker_bundle = {
        "codex_config_append": "[mcp_servers.synthetic]",
        "env": {"SYNTHETIC_BROKER_TOKEN": "test-only"},
        "run_mode": "mission",
        "provider_capabilities": {"self_delegation": True},
    }
    headers = {
        **AUTH,
        **_signed_bundle_headers(broker_bundle),
    }

    response = client.post("/v1/chat/completions", headers=headers, json=payload)

    assert response.status_code == 200, response.text
    session = client.app.state.store.list_provider_sessions(owner_id="owner-a")[0]
    worker = client.app.state.store.get_worker(session["worker_id"])
    persisted = json.loads(worker["bootstrap_bundle_json"])
    assert persisted["codex_config_append"] == "[mcp_servers.synthetic]"
    assert persisted["env"]["SYNTHETIC_BROKER_TOKEN"] == "test-only"
    assert persisted["run_mode"] == "conversation"
    assert persisted["provider_capabilities"] == {
        "self_delegation": False,
        "native_tools": True,
    }


def test_bootstrap_bundle_requires_a_fresh_valid_service_signature(tmp_path, monkeypatch):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    payload = _payload(workspace)
    bundle = {"env": {"SYNTHETIC_BROKER_TOKEN": "test-only"}}
    encoded = base64.b64encode(json.dumps(bundle).encode()).decode()

    unsigned = client.post(
        "/v1/chat/completions",
        headers={**AUTH, "X-GlassHive-Bootstrap-Bundle-B64": encoded},
        json=payload,
    )
    invalid = client.post(
        "/v1/chat/completions",
        headers={
            **AUTH,
            **_signed_bundle_headers(bundle),
            "X-GlassHive-Bootstrap-Signature": "sha256=invalid",
        },
        json=payload,
    )
    stale = client.post(
        "/v1/chat/completions",
        headers={**AUTH, **_signed_bundle_headers(bundle, timestamp=int(time.time()) - 601)},
        json=payload,
    )

    assert unsigned.status_code == 403
    assert invalid.status_code == 403
    assert stale.status_code == 403
    assert unsigned.json()["error"]["code"] == "permission_denied"


def test_bootstrap_bundle_fails_closed_when_signature_verification_is_unconfigured(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch)
    headers = {**AUTH, **_signed_bundle_headers({"env": {"SYNTHETIC": "value"}})}
    monkeypatch.delenv("VIVENTIUM_GLASSHIVE_CAPABILITY_BROKER_SECRET")

    response = client.post(
        "/v1/chat/completions",
        headers=headers,
        json=_payload(workspace),
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"


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
    assert reasoning[0] == "The harness started working.\n"
    assert all("queued" not in item.lower() and "waiting" not in item.lower() for item in reasoning)
    assert all("content" not in chunk["choices"][0]["delta"] for chunk in chunks if "reasoning_content" in chunk["choices"][0]["delta"])
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


def test_invalid_model_fails_loudly_and_missing_optional_agent_metadata_is_defaulted(
    tmp_path, monkeypatch
):
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
    assert missing.status_code == 200, missing.text
    sessions = client.app.state.store.list_provider_sessions(owner_id="owner-a")
    assert any(session["agent_id"] == "glasshive-direct" for session in sessions)


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
    monkeypatch.delenv("GLASSHIVE_PROVIDER_API_KEY", raising=False)
    monkeypatch.setenv("GLASSHIVE_HOST_WORKERS_ENABLED", "1")
    client = TestClient(
        create_app(
            str(tmp_path / "runtime.db"),
            runtime_backend="stub",
            runtime=StubRuntime(),
        )
    )

    response = client.get("/v1/models", headers=AUTH)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"
    assert response.json()["error"]["message"] == "GlassHive provider authentication is not configured"


@pytest.mark.parametrize(
    ("first_name", "second_name", "message"),
    [
        (
            "GLASSHIVE_PROVIDER_API_KEY",
            "GLASSHIVE_MCP_API_KEY",
            "GLASSHIVE_MCP_API_KEY must be distinct from GLASSHIVE_PROVIDER_API_KEY",
        ),
        (
            "WPR_API_TOKEN",
            "GLASSHIVE_MCP_API_KEY",
            "GLASSHIVE_MCP_API_KEY must be distinct from WPR_API_TOKEN",
        ),
    ],
)
def test_runtime_rejects_shared_provider_mcp_or_admin_credentials(
    tmp_path, monkeypatch, first_name, second_name, message
):
    monkeypatch.setenv("WPR_API_TOKEN", "runtime-admin-token")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_API_KEY", "provider-test-token")
    monkeypatch.setenv("GLASSHIVE_MCP_API_KEY", "mcp-test-token")
    monkeypatch.setenv(first_name, "shared-token")
    monkeypatch.setenv(second_name, "shared-token")

    with pytest.raises(RuntimeError, match=message):
        create_app(
            str(tmp_path / "runtime.db"),
            runtime_backend="stub",
            runtime=StubRuntime(),
        )


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


def test_provider_cancel_marks_a_queued_run_cancelled_before_native_execution(tmp_path):
    store = Store(str(tmp_path / "runtime.db"))
    runtime = InterruptCountingRuntime()
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    try:
        project = store.create_project(
            "owner-a",
            "Synthetic conversation",
            "Queued cancellation regression",
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
        session = store.upsert_provider_session(
            tenant_id="local",
            owner_id="owner-a",
            conversation_id="conv-cancel",
            agent_id="agent-cancel",
            model_id="codex-cli:gpt-5.6-sol",
            project_id=project["project_id"],
            worker_id=worker["worker_id"],
            workspace_dir=str(tmp_path),
            access_mode="workspace",
        )
        run = store.create_run(
            worker["worker_id"],
            project["project_id"],
            "never execute this",
            state="queued",
        )
        request, _ = store.create_provider_request(
            tenant_id="local",
            owner_id="owner-a",
            session_id=session["session_id"],
            idempotency_key="queued-cancel",
            message_id="message-cancel",
            stream_id="stream-cancel",
            requested_history_count=0,
        )
        store.update_provider_request(request["request_id"], run_id=run["run_id"])
        provider = ConversationProvider(store, service)

        cancelled = provider.cancel(request["request_id"])

        assert cancelled["state"] == "cancelled"
        assert store.get_run(run["run_id"])["state"] == "cancelled"
        assert runtime.interrupt_calls == []
        assert provider._sync(store.get_provider_request(request["request_id"]))["state"] == "cancelled"
    finally:
        service.shutdown()


def test_sync_never_resurrects_a_cancelled_provider_request(tmp_path):
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(
        store,
        InterruptCountingRuntime(),
        reconcile_on_startup=False,
    )
    try:
        project = store.create_project(
            "owner-a", "Synthetic conversation", "Cancellation race", "codex-cli"
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
        session = store.upsert_provider_session(
            tenant_id="local",
            owner_id="owner-a",
            conversation_id="conv-race",
            agent_id="agent-race",
            model_id="codex-cli:gpt-5.6-sol",
            project_id=project["project_id"],
            worker_id=worker["worker_id"],
            workspace_dir=str(tmp_path),
            access_mode="workspace",
        )
        run = store.create_run(
            worker["worker_id"], project["project_id"], "late completion", state="running"
        )
        request, _ = store.create_provider_request(
            tenant_id="local",
            owner_id="owner-a",
            session_id=session["session_id"],
            idempotency_key="cancel-race",
            message_id="message-race",
            stream_id="stream-race",
            requested_history_count=0,
        )
        store.update_provider_request(
            request["request_id"], run_id=run["run_id"], state="cancelled"
        )
        store.finalize_run(run["run_id"], state="completed", output_text="late answer")
        provider = ConversationProvider(store, service)

        synced = provider._sync(store.get_provider_request(request["request_id"]))

        assert synced["state"] == "cancelled"
        assert all(
            event["event_type"] != "completed"
            for event in store.list_provider_activity(request["request_id"])
        )
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


def test_streaming_redactor_emits_safe_text_before_newline_or_terminal_flush():
    redactor = StreamingRedactor(overlap=16)

    first = redactor.feed("A safe response can arrive word by word without waiting for a newline")
    final = redactor.flush()

    assert first
    assert first + final == "A safe response can arrive word by word without waiting for a newline"


def test_streaming_redactor_default_emits_an_ordinary_short_answer_incrementally():
    redactor = StreamingRedactor()

    first = redactor.feed(
        "This is an ordinary safe conversational answer that should appear before completion. "
        "It is intentionally far shorter than one kilobyte."
    )

    assert first


def test_streaming_redactor_redacts_newline_split_home_path_and_bounds_long_lines():
    redactor = StreamingRedactor(overlap=16, max_buffer=64)

    visible = redactor.feed("Path /Users/synthetic/\nDocuments/private.txt\n")
    visible += redactor.feed("x" * 65)
    visible += redactor.flush()

    assert "/Users/synthetic" not in visible
    assert "[REDACTED_LOCAL_PATH]" in visible
    assert "[REDACTED_OVERSIZED_STREAM_SEGMENT]" in visible


def test_streaming_redactor_holds_and_redacts_split_common_credentials():
    redactor = StreamingRedactor(overlap=16, max_buffer=512)

    visible = redactor.feed("token ghp_synthetic")
    visible += redactor.feed("githubcredential then xoxb-synthetic-")
    visible += redactor.feed("slack-credential done\n")
    visible += redactor.flush()

    assert "syntheticgithubcredential" not in visible
    assert "synthetic-slack-credential" not in visible
    assert "[REDACTED]" in visible


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
            json.dumps({"type": "result", "result": "Safe visible answer."}),
        ]
    )

    assert _native_visible_text("claude-code", raw) == "Safe visible answer."
    assert _native_visible_text("unknown-profile", raw) == ""


def test_native_visible_text_waits_for_claude_result_and_excludes_working_preamble():
    raw = "\n".join(
        [
            json.dumps(
                {
                    "type": "stream_event",
                    "event": {
                        "type": "content_block_delta",
                        "delta": {"type": "thinking_delta", "thinking": "hidden"},
                    },
                }
            ),
            json.dumps(
                {
                    "type": "stream_event",
                    "event": {
                        "type": "content_block_delta",
                        "delta": {"type": "text_delta", "text": "I will inspect the file."},
                    },
                }
            ),
            json.dumps(
                {
                    "type": "stream_event",
                    "event": {
                        "type": "content_block_delta",
                        "delta": {"type": "text_delta", "text": "Exact final answer."},
                    },
                }
            ),
            json.dumps({"type": "result", "result": "Exact final answer."}),
        ]
    )

    assert _native_visible_text("claude-code", raw) == "Exact final answer."
    assert _native_visible_text("claude-code", "\n".join(raw.splitlines()[:-1])) == ""


def test_native_visible_text_waits_for_codex_turn_and_returns_only_latest_agent_message():
    lines = [
        json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "I will inspect the file."},
            }
        ),
        json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "Exact final answer."},
            }
        ),
    ]

    assert _native_visible_text("codex-cli", "\n".join(lines)) == ""
    assert _native_visible_text(
        "codex-cli",
        "\n".join([*lines, json.dumps({"type": "turn.completed"})]),
    ) == "Exact final answer."


@pytest.mark.parametrize("profile", ["codex-cli", "claude-code"])
def test_completed_native_harness_without_terminal_answer_fails_loudly(
    tmp_path, monkeypatch, profile
):
    class MissingTerminalRuntime(StubRuntime):
        def run_task(
            self,
            worker: dict,
            instruction: str,
            timeout_sec: float | None = None,
            run_id: str | None = None,
        ) -> str:
            _ = worker, instruction, timeout_sec, run_id
            return "I am still working on it."

        def provider_activity_log(self, worker: dict, run_id: str) -> tuple[str, str]:
            _ = worker, run_id
            if profile == "codex-cli":
                return profile, json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "type": "agent_message",
                            "text": "I am still working on it.",
                        },
                    }
                )
            return profile, json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "I am still working on it."}
                        ]
                    },
                }
            )

    workspace = tmp_path / "Life"
    workspace.mkdir()
    client = _client(tmp_path, monkeypatch, runtime=MissingTerminalRuntime())
    model = "codex-cli:gpt-5.6-sol" if profile == "codex-cli" else "claude-code:opus"

    response = client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json=_payload(workspace, model=model),
    )

    assert response.status_code == 502
    assert "terminal authored response" in response.text
    assert "I am still working on it" not in response.text


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
        old = (datetime.now(UTC) - timedelta(days=120)).isoformat()
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


def test_provider_reapplies_retention_during_a_long_lived_process(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "runtime.db"))
    runtime = InterruptCountingRuntime()
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    try:
        provider = ConversationProvider(store, service)
        calls = 0
        original = provider._apply_retention_policy

        def counted_retention():
            nonlocal calls
            calls += 1
            original()

        provider._apply_retention_policy = counted_retention
        provider._last_retention_monotonic = time.monotonic() - 7200
        provider._maybe_apply_retention_policy()

        assert calls == 1
        assert time.monotonic() - provider._last_retention_monotonic < 2
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
