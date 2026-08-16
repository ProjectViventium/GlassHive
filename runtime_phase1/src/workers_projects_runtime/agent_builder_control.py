from __future__ import annotations

import json
import re
from typing import Any, Iterable, Mapping


# Canonical LibreChat Agent Builder graph protocol prefix. This is a structural
# tool namespace, never a user-intent or agent-name routing heuristic.
LC_TRANSFER_TO_PREFIX = "lc_transfer_to_"
MAX_GRAPH_TRANSFER_TOOLS = 16
MAX_GRAPH_TRANSFER_NAME_LENGTH = 128
MAX_GRAPH_TRANSFER_DESCRIPTION_LENGTH = 1024
CONTROL_VERSION = 1

_TRANSFER_NAME = re.compile(r"^lc_transfer_to_[A-Za-z0-9_-]+$")


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _tool_choice_name(tool_choice: Any) -> str:
    choice = _mapping(tool_choice)
    function = _mapping(choice.get("function"))
    return str(function.get("name") or "").strip()


def _zero_input_object_schema(value: Any) -> bool:
    schema = _mapping(value)
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    return (
        schema.get("type") == "object"
        and isinstance(properties, Mapping)
        and len(properties) == 0
        and isinstance(required, list)
        and len(required) == 0
        and schema.get("additionalProperties") in (None, False)
    )


def graph_transfer_control(
    tools: Iterable[Any] | None,
    tool_choice: Any = None,
) -> dict[str, Any] | None:
    """Return a bounded request-scoped control spec for LC zero-input handoffs.

    Other OpenAI tools deliberately stay outside this bridge. They continue to use
    GlassHive's signed host-capability broker and cannot gain graph authority here.
    """

    if isinstance(tool_choice, str) and tool_choice.strip().lower() == "none":
        return None

    selected: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw_tool in tools or []:
        tool = _mapping(raw_tool)
        function = _mapping(tool.get("function"))
        name = str(function.get("name") or "").strip()
        if tool.get("type") != "function" or not name.startswith(
            LC_TRANSFER_TO_PREFIX
        ):
            continue
        if (
            len(name) > MAX_GRAPH_TRANSFER_NAME_LENGTH
            or not _TRANSFER_NAME.fullmatch(name)
        ):
            raise ValueError("Invalid Agent Builder graph transfer tool name")
        if not _zero_input_object_schema(function.get("parameters")):
            # Agent Builder may bind legacy or specialist transfers that require
            # caller-authored arguments alongside the zero-input graph transfers
            # supported by this bridge. Keep those outside the native control
            # envelope; LibreChat remains their only execution authority.
            continue
        if name in seen:
            raise ValueError("Duplicate Agent Builder graph transfer tool name")
        seen.add(name)
        description = " ".join(
            str(function.get("description") or "Transfer control in the current Agent Builder graph")
            .replace("\x00", "")
            .split()
        )[:MAX_GRAPH_TRANSFER_DESCRIPTION_LENGTH]
        selected.append({"name": name, "description": description})

    if len(selected) > MAX_GRAPH_TRANSFER_TOOLS:
        raise ValueError("Too many Agent Builder graph transfer tools")

    forced_name = _tool_choice_name(tool_choice)
    if forced_name:
        if forced_name.startswith(LC_TRANSFER_TO_PREFIX):
            selected = [tool for tool in selected if tool["name"] == forced_name]
            if not selected:
                raise ValueError("Forced Agent Builder graph transfer tool is not available")
        else:
            return None

    if not selected:
        return None
    return {"version": CONTROL_VERSION, "tools": selected}


def normalized_graph_transfer_control(value: Any) -> dict[str, Any] | None:
    control = _mapping(value)
    if control.get("version") != CONTROL_VERSION:
        return None
    tools = control.get("tools")
    if not isinstance(tools, list) or not tools or len(tools) > MAX_GRAPH_TRANSFER_TOOLS:
        return None
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw_tool in tools:
        tool = _mapping(raw_tool)
        name = str(tool.get("name") or "").strip()
        if (
            not name
            or name in seen
            or len(name) > MAX_GRAPH_TRANSFER_NAME_LENGTH
            or not _TRANSFER_NAME.fullmatch(name)
        ):
            return None
        seen.add(name)
        description = " ".join(
            str(tool.get("description") or "Transfer control in the current Agent Builder graph")
            .replace("\x00", "")
            .split()
        )[:MAX_GRAPH_TRANSFER_DESCRIPTION_LENGTH]
        normalized.append({"name": name, "description": description})
    return {"version": CONTROL_VERSION, "tools": normalized}


def graph_transfer_output_schema(control: Any) -> dict[str, Any] | None:
    normalized = normalized_graph_transfer_control(control)
    if not normalized:
        return None
    names = [tool["name"] for tool in normalized["tools"]]
    descriptions = "; ".join(
        f"{tool['name']}: {tool['description']}" for tool in normalized["tools"]
    )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "description": (
            "Choose the next result for the current Agent Builder graph turn. "
            "Use type=tool_call only when a listed transfer is the best next action; "
            "the host graph will execute it with shared state. When starting a consult, "
            "use empty content; the shared graph already carries the request and context. "
            "Only when returning completed specialist work, put that result in content "
            "so the receiving agent can judge it from shared state. Otherwise use "
            "type=assistant_response and put the complete user-facing answer in content. "
            f"Available transfers: {descriptions}"
        ),
        "properties": {
            "type": {
                "type": "string",
                "enum": ["assistant_response", "tool_call"],
            },
            "content": {"type": "string"},
            "tool_name": {
                "type": ["string", "null"],
                "enum": [None, *names],
            },
        },
        "required": ["type", "content", "tool_name"],
    }


def parse_graph_transfer_output(output: str, control: Any) -> dict[str, Any]:
    normalized = normalized_graph_transfer_control(control)
    if not normalized:
        return {"type": "assistant_response", "content": str(output or "")}
    try:
        payload = json.loads(str(output or "").strip())
    except json.JSONDecodeError as exc:
        raise ValueError("Native harness returned malformed Agent Builder control output") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "type",
        "content",
        "tool_name",
    }:
        raise ValueError("Native harness returned an invalid Agent Builder control envelope")
    action = payload.get("type")
    content = payload.get("content")
    tool_name = payload.get("tool_name")
    if action == "assistant_response":
        if not isinstance(content, str) or tool_name is not None:
            raise ValueError("Native harness returned an invalid assistant response envelope")
        return {"type": action, "content": content}
    allowed = {tool["name"] for tool in normalized["tools"]}
    if action != "tool_call" or not isinstance(content, str):
        raise ValueError("Native harness returned an invalid graph transfer envelope")
    if not isinstance(tool_name, str) or tool_name not in allowed:
        raise ValueError("Native harness selected an unavailable graph transfer tool")
    return {"type": action, "content": content, "tool_name": tool_name}
