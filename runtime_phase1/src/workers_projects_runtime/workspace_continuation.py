from __future__ import annotations

import re
from typing import Any

CONNECTED_ACCOUNT_NO_BROKER_NOTE = (
    "Connected-account content intent was requested, but this workspace did not receive a complete "
    "host-signed `glasshive-user-capabilities` broker grant/config in its bootstrap bundle. Do not "
    "claim brokered MCP access, brokered provider reachability, or brokered results. Use only tools "
    "that are actually available inside this worker session and label them accurately; if the needed "
    "provider, content, or auth scope is unavailable, report the blocker instead of filling gaps."
)


def _strip_instruction_note(instruction: str, note: str) -> str:
    clean_note = str(note or "").strip()
    if not clean_note:
        return str(instruction or "").strip()
    return re.sub(r"\n{0,2}" + re.escape(clean_note), "", str(instruction or "")).strip()


def continuation_instruction(
    *,
    previous_run: dict[str, Any],
    continuation_goal: str | None = None,
) -> str:
    text = str(previous_run.get("instruction") or "").strip()
    for _ in range(8):
        if not text.startswith("Continue this GlassHive workspace"):
            break
        marker = "Original task:\n"
        if marker not in text:
            break
        text = text.split(marker, 1)[1].strip()
        for stop_marker in (
            "\n\nPrevious failure classification:",
            "\n\nContinuation request:",
            "\n\nGlassHive completion contract:",
        ):
            index = text.find(stop_marker)
            if index >= 0:
                text = text[:index].strip()
                break
    original_instruction = _strip_instruction_note(text, CONNECTED_ACCOUNT_NO_BROKER_NOTE)
    failure_class = ""
    failure_retryable = False
    recovery = ""
    if str(previous_run.get("state") or "").strip().lower() == "failed":
        failure_class = str(previous_run.get("failure_class") or "").strip() or "unknown"
        failure_retryable = bool(previous_run.get("failure_retryable"))
        recovery = str(previous_run.get("failure_recommended_recovery") or "").strip()

    chunks = [
        "Continue this GlassHive workspace from its current files, browser state, notes, and partial outputs.",
        "Preserve the original user request, success criteria, response format, and any files already available in the workspace.",
        "Do not replace binary source files with text extracts unless the user explicitly asked for text extraction only.",
    ]
    if original_instruction:
        chunks.append(f"Original task:\n{original_instruction}")
    if failure_class:
        chunks.append(
            "Previous failure classification:\n"
            f"- class: {failure_class}\n"
            f"- retryable: {failure_retryable}\n"
            f"- recovery guidance: {recovery or 'Continue carefully.'}"
        )
    clean_goal = str(continuation_goal or "").strip()
    if clean_goal:
        chunks.append(f"Continuation request:\n{clean_goal}")
    else:
        chunks.append(
            "Continuation request:\nResume the original task from the current workspace state. "
            "Use available partial work, avoid repeating failed provider-heavy loops when possible, "
            "and produce the final requested deliverables."
        )
    return "\n\n".join(chunks)
