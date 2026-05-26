from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FailureClassification:
    failure_class: str
    retryable: bool
    user_message: str
    recommended_recovery: str
    diagnostic_summary: str

    def as_store_fields(self) -> dict[str, Any]:
        return {
            "failure_class": self.failure_class,
            "failure_retryable": 1 if self.retryable else 0,
            "failure_user_message": self.user_message,
            "failure_recommended_recovery": self.recommended_recovery,
            "failure_diagnostic_summary": self.diagnostic_summary,
        }


def classify_cli_failure(
    *,
    stdout: str,
    stderr: str,
    runtime_name: str,
    exit_code: int | None = None,
) -> FailureClassification:
    """Classify CLI failure evidence without inspecting the user's task text."""
    evidence = _collect_structured_failure_evidence(stdout)
    if not evidence:
        evidence = _collect_structured_failure_evidence(stderr)
    diagnostic_source = "\n".join(evidence) if evidence else stderr or stdout
    diagnostic_summary = _redact_failure_text(diagnostic_source.strip(), max_chars=1200)
    lowered = diagnostic_summary.lower()

    if "content_filter" in lowered or "content filter" in lowered:
        return FailureClassification(
            failure_class="provider_content_filter",
            retryable=False,
            user_message=(
                "The worker was stopped by the model provider's safety filter before it could finish."
            ),
            recommended_recovery=(
                "Ask GlassHive to continue with a safer, narrower plan that preserves the original "
                "success criteria, or adjust the request if the filter was expected."
            ),
            diagnostic_summary=diagnostic_summary,
        )
    if "too many requests" in lowered or "rate limit" in lowered or "rate_limit" in lowered or "429" in lowered:
        return FailureClassification(
            failure_class="provider_rate_limited",
            retryable=True,
            user_message=(
                "The worker made progress, but the model or research provider rate-limited the run before it finished."
            ),
            recommended_recovery=(
                "Use workspace_continue to resume the same workspace after a short wait, preserving the "
                "original task and any files already produced."
            ),
            diagnostic_summary=diagnostic_summary,
        )
    if "401" in lowered or "403" in lowered or "unauthorized" in lowered or "invalid api key" in lowered:
        return FailureClassification(
            failure_class="provider_auth_missing",
            retryable=False,
            user_message="The worker could not use the configured model provider credentials.",
            recommended_recovery=(
                "Fix the provider key or route configuration, then use workspace_continue to resume the "
                "same workspace."
            ),
            diagnostic_summary=diagnostic_summary,
        )
    if "response.failed" in lowered or "turn.failed" in lowered:
        return FailureClassification(
            failure_class="provider_response_failed",
            retryable=True,
            user_message="The model provider ended the worker turn unexpectedly before the task finished.",
            recommended_recovery=(
                "Use workspace_continue to resume from the same workspace and ask the worker to continue "
                "from the current files and notes."
            ),
            diagnostic_summary=diagnostic_summary,
        )

    suffix = f" exited with code {exit_code}" if exit_code is not None else " failed"
    return FailureClassification(
        failure_class="unknown",
        retryable=False,
        user_message=f"The {runtime_name}{suffix}, but GlassHive could not classify the provider failure safely.",
        recommended_recovery=(
            "Open the View / Steer page for details, then use workspace_continue only if the partial "
            "workspace state is worth preserving."
        ),
        diagnostic_summary=diagnostic_summary,
    )


def classify_runtime_error(
    exc: BaseException,
    *,
    runtime_name: str,
) -> FailureClassification:
    """Classify runtime/control-plane failures without inspecting the user's task text."""
    message = _redact_failure_text(str(exc or "").strip(), max_chars=1200)
    lowered = message.lower()
    runtime_label = str(getattr(exc, "runtime_name", "") or runtime_name or "worker").strip()
    profile = str(getattr(exc, "profile", "") or "").strip()
    binary = str(getattr(exc, "binary", "") or "").strip()
    binary_label = binary.replace("\\", "/").rstrip("/").split("/")[-1] or binary

    if "not installed" in lowered or "not on path" in lowered:
        binary_hint = f" (`{binary_label}`)" if binary_label else ""
        profile_hint = f" for `{profile}`" if profile else ""
        return FailureClassification(
            failure_class="runtime_dependency_missing",
            retryable=False,
            user_message=(
                f"GlassHive could not start the selected worker{profile_hint} because the required "
                f"host CLI{binary_hint} is not installed or not available to the GlassHive service."
            ),
            recommended_recovery=(
                "Choose an available worker profile or sandbox/workstation execution mode, or ask the "
                "operator to install/configure the missing CLI on the GlassHive service PATH."
            ),
            diagnostic_summary=message,
        )
    if "host-native workers are disabled" in lowered:
        return FailureClassification(
            failure_class="unsupported_runtime_configuration",
            retryable=False,
            user_message="GlassHive host-native workers are disabled in this deployment.",
            recommended_recovery=(
                "Use a sandbox/workstation workspace, or ask the operator to enable host-native workers "
                "for this deployment."
            ),
            diagnostic_summary=message,
        )
    if "already has an active worker" in lowered or "one active host worker" in lowered:
        return FailureClassification(
            failure_class="host_worker_busy",
            retryable=True,
            user_message=(
                f"The {runtime_label} host worker is already busy with another active workspace."
            ),
            recommended_recovery=(
                "Wait for the active worker to finish, use workspace_status/workspace_wait to check it, "
                "or launch the task in a sandbox/workstation workspace."
            ),
            diagnostic_summary=message,
        )

    return FailureClassification(
        failure_class="runtime_error",
        retryable=False,
        user_message=f"The {runtime_label} worker failed before GlassHive could complete the task.",
        recommended_recovery=(
            "Open the View / Steer page for details, then use workspace_continue only if the partial "
            "workspace state is worth preserving."
        ),
        diagnostic_summary=message,
    )


def _collect_structured_failure_evidence(text: str) -> list[str]:
    evidence: list[str] = []
    for line in text.splitlines():
        raw = line.strip()
        if not raw or not raw.startswith("{"):
            continue
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            evidence.extend(_extract_failure_strings(item))
    return evidence


def _extract_failure_strings(value: Any, *, path: str = "") -> list[str]:
    results: list[str] = []
    if isinstance(value, dict):
        event_type = str(value.get("type") or value.get("event") or value.get("name") or "")
        status = str(value.get("status") or value.get("code") or value.get("error_code") or "")
        if event_type and _looks_failure_related(event_type):
            results.append(f"{path or 'event'} type={event_type}")
        if status and _looks_failure_related(status):
            results.append(f"{path or 'event'} status={status}")
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if isinstance(child, str):
                if _looks_failure_related(child) or _looks_failure_field(key):
                    results.append(f"{child_path}: {child}")
            elif isinstance(child, (dict, list)):
                results.extend(_extract_failure_strings(child, path=child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            results.extend(_extract_failure_strings(child, path=f"{path}[{index}]"))
    return results


def _looks_failure_field(key: str) -> bool:
    lowered = key.lower()
    return lowered in {"error", "error_code", "message", "failure", "status_code", "detail"}


def _looks_failure_related(value: str) -> bool:
    lowered = value.lower()
    markers = (
        "error",
        "failed",
        "failure",
        "content_filter",
        "content filter",
        "too many requests",
        "rate limit",
        "rate_limit",
        "429",
        "401",
        "403",
        "unauthorized",
        "invalid api key",
        "response.failed",
        "turn.failed",
    )
    return any(marker in lowered for marker in markers)


_FAILURE_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{12,}"), r"\1[REDACTED]"),
    (re.compile(r"(?i)((?:api[_-]?key|token|secret|password|passwd|pwd)\s*[:=]\s*)[^\s\"']{6,}"), r"\1[REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"), "sk-[REDACTED]"),
    (re.compile(r"\b[A-Za-z0-9_]{8,}:[A-Za-z0-9_./+=-]{20,}\b"), "[REDACTED_CREDENTIAL]"),
    (re.compile(r"(?i)data:image/[a-z0-9.+-]+;base64,[A-Za-z0-9+/=\s]{256,}"), "[REDACTED_IMAGE_BASE64]"),
    (re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{512,}={0,2}(?![A-Za-z0-9+/=])"), "[REDACTED_LONG_BASE64]"),
)


def _redact_failure_text(value: str, max_chars: int | None = None) -> str:
    text = value
    for pattern, replacement in _FAILURE_REDACTIONS:
        text = pattern.sub(replacement, text)
    if max_chars is not None and len(text) > max_chars:
        return text[-max_chars:]
    return text
