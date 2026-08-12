from __future__ import annotations

import json

import pytest
from workers_projects_runtime.agent_builder_control import (
    graph_transfer_control,
    graph_transfer_output_schema,
    parse_graph_transfer_output,
)
from workers_projects_runtime.conversation_provider import (
    GLASSHIVE_MODELS,
    ChatCompletionRequest,
    ConversationProvider,
)


def _transfer(name: str, *, with_input: bool = False) -> dict:
    properties = {"query": {"type": "string"}} if with_input else {}
    required = ["query"] if with_input else []
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "Transfer through the current graph.",
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


def _payload(*, stream: bool = False) -> ChatCompletionRequest:
    return ChatCompletionRequest.model_validate(
        {
            "model": "codex-cli:gpt-5.6-sol",
            "messages": [{"role": "user", "content": "Choose the best next graph step."}],
            "stream": stream,
            "metadata": {
                "owner_id": "owner-example",
                "conversation_id": "conversation-example",
                "agent_id": "agent-example",
                "idempotency_key": "message-example",
                "glasshive_options": {
                    "workspace": {"mode": "default"},
                    "access": "workspace",
                },
            },
            "tools": [
                _transfer("lc_transfer_to_specialist"),
                _transfer("lc_transfer_to_requires_input", with_input=True),
                {
                    "type": "function",
                    "function": {
                        "name": "external_side_effect",
                        "parameters": {"type": "object", "properties": {}},
                    },
                },
            ],
        }
    )


def test_graph_control_admits_only_zero_input_structural_transfers():
    payload = _payload()
    control = graph_transfer_control(payload.tools, payload.tool_choice)

    assert control == {
        "version": 1,
        "tools": [
            {
                "name": "lc_transfer_to_specialist",
                "description": "Transfer through the current graph.",
            }
        ],
    }
    with pytest.raises(ValueError, match="not available"):
        graph_transfer_control(
            payload.tools,
            {
                "type": "function",
                "function": {"name": "lc_transfer_to_requires_input"},
            },
        )


def test_graph_control_schema_and_native_choice_fail_closed():
    control = graph_transfer_control(_payload().tools)
    schema = graph_transfer_output_schema(control)

    assert schema["additionalProperties"] is False
    assert schema["properties"]["tool_name"]["enum"] == [
        None,
        "lc_transfer_to_specialist",
    ]
    assert parse_graph_transfer_output(
        json.dumps(
            {
                "type": "tool_call",
                "content": "",
                "tool_name": "lc_transfer_to_specialist",
            }
        ),
        control,
    )["type"] == "tool_call"
    with pytest.raises(ValueError, match="unavailable"):
        parse_graph_transfer_output(
            json.dumps(
                {
                    "type": "tool_call",
                    "content": "",
                    "tool_name": "lc_transfer_to_unknown",
                }
            ),
            control,
        )


def test_conversation_bundle_projects_graph_control_without_broad_tool_authority():
    provider = ConversationProvider.__new__(ConversationProvider)
    payload = _payload()
    model = GLASSHIVE_MODELS[payload.model]

    bundle = provider._native_bundle(payload, model, model.recommended_effort)

    assert bundle["agent_builder_control"]["tools"] == [
        {
            "name": "lc_transfer_to_specialist",
            "description": "Transfer through the current graph.",
        }
    ]
    assert bundle["provider_capabilities"]["graph_control_transport"] == "openai_tool_call"
    assert bundle["provider_capabilities"]["graph_control_tools"] == [
        "lc_transfer_to_specialist"
    ]
    assert "external_side_effect" not in json.dumps(bundle)


def test_non_streaming_graph_choice_returns_one_openai_tool_call(monkeypatch):
    class RecordingStore:
        response_json = ""

        def update_provider_request(self, request_id: str, **fields):
            assert request_id == "request-example"
            self.response_json = fields["response_json"]

    provider = ConversationProvider.__new__(ConversationProvider)
    provider.store = RecordingStore()
    payload = _payload()
    monkeypatch.setattr(
        provider,
        "_conversation_output",
        lambda request_record, run: json.dumps(
            {
                "type": "tool_call",
                "content": "",
                "tool_name": "lc_transfer_to_specialist",
            }
        ),
    )
    monkeypatch.setattr(
        provider,
        "_completion_usage",
        lambda request_record, run, completion, output: (
            {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            "estimated",
        ),
    )

    response = provider.response_payload(
        {"request_id": "request-example", "state": "completed", "response_json": ""},
        {"state": "completed"},
        payload,
    )

    choice = response["choices"][0]
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
    assert json.loads(provider.store.response_json) == response
