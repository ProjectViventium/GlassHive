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


def has_structured_failure_evidence(*texts: str) -> bool:
    return any(_collect_structured_failure_evidence(text or "") for text in texts)


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
    if _looks_like_rate_limit_failure(lowered):
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
    if (
        "request rejected" in lowered
        or "invalid_request_error" in lowered
        or "invalid request" in lowered
        or (_has_contextual_status_code(lowered, ("400",)) and "bad request" in lowered)
    ):
        return FailureClassification(
            failure_class="provider_request_rejected",
            retryable=False,
            user_message=(
                "The model provider rejected the worker request before it could finish."
            ),
            recommended_recovery=(
                "Inspect the provider diagnostic and continue the same workspace only after correcting "
                "the provider route, request shape, or unsupported option that caused the rejection."
            ),
            diagnostic_summary=diagnostic_summary,
        )
    if _looks_like_provider_service_failure(lowered, structured=bool(evidence)):
        return FailureClassification(
            failure_class="provider_response_failed",
            retryable=True,
            user_message=(
                "The model provider was temporarily unavailable or overloaded before the worker could finish."
            ),
            recommended_recovery=(
                "Use workspace_continue to resume from the same workspace after a short wait, preserving "
                "the original task and any files already produced."
            ),
            diagnostic_summary=diagnostic_summary,
        )
    if _looks_like_provider_auth_failure(lowered):
        return FailureClassification(
            failure_class="provider_auth_missing",
            retryable=False,
            user_message="The worker could not use the configured model provider credentials.",
            recommended_recovery=(
                "Fix the provider key, route configuration, or CLI login projected into this worker, "
                "then use workspace_continue to resume the same workspace."
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
    if _looks_like_sandbox_lifecycle_failure(lowered):
        return FailureClassification(
            failure_class="runtime_sandbox_unavailable",
            retryable=True,
            user_message=(
                "GlassHive could not prepare the selected worker sandbox/workstation before the run started."
            ),
            recommended_recovery=(
                "Use workspace_continue after the sandbox service recovers, or choose another available "
                "execution mode that still gives the worker its native capabilities."
            ),
            diagnostic_summary=diagnostic_summary,
        )
    if _looks_like_runtime_dependency_or_version_failure(lowered):
        return FailureClassification(
            failure_class="runtime_dependency_missing",
            retryable=False,
            user_message=(
                "GlassHive could not complete the run because the selected worker runtime has a "
                "missing, incompatible, or too-old local prerequisite."
            ),
            recommended_recovery=(
                "Use a configured managed dependency, choose another available worker profile, or "
                "use sandbox/workstation execution when that still satisfies the user's request. "
                "Ask the operator to change the host service runtime only when no configured "
                "recovery path is available."
            ),
            diagnostic_summary=diagnostic_summary,
        )
    if exit_code in {143, -15} or "sigterm" in lowered or "terminated" in lowered:
        return FailureClassification(
            failure_class="runtime_terminated",
            retryable=False,
            user_message=(
                "The worker process was terminated before it could finish and report a result."
            ),
            recommended_recovery=(
                "Open the View / Steer page to inspect any partial workspace state. Use "
                "workspace_continue only if the work should resume from that state."
            ),
            diagnostic_summary=diagnostic_summary,
        )
    if (
        "stdin is closed" in lowered
        or "write_stdin failed" in lowered
        or "rerun exec_command with tty=true" in lowered
    ):
        return FailureClassification(
            failure_class="runtime_io_failed",
            retryable=True,
            user_message=(
                "The worker made progress, but its command session closed before GlassHive could "
                "capture the final turn cleanly."
            ),
            recommended_recovery=(
                "Use workspace_continue to resume from the same workspace and ask the worker to "
                "verify or finish from the current files."
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
    dependency_label = str(getattr(exc, "dependency_label", "") or "").strip()
    binary_label = dependency_label or binary.replace("\\", "/").rstrip("/").split("/")[-1] or binary
    required_version = str(getattr(exc, "required_version", "") or "").strip()
    actual_version = str(getattr(exc, "actual_version", "") or "").strip()
    recovery_hint = str(getattr(exc, "recovery_hint", "") or "").strip()

    if _looks_like_provider_auth_failure(lowered):
        return FailureClassification(
            failure_class="provider_auth_missing",
            retryable=False,
            user_message="The worker could not use the configured model provider credentials.",
            recommended_recovery=(
                recovery_hint
                or "Fix the provider key, route, or CLI login projected into this worker, then use "
                "workspace_continue to resume the same workspace."
            ),
            diagnostic_summary=message,
        )
    if _looks_like_sandbox_lifecycle_failure(lowered):
        return FailureClassification(
            failure_class="runtime_sandbox_unavailable",
            retryable=True,
            user_message=(
                "GlassHive could not prepare the selected worker sandbox/workstation before the run started."
            ),
            recommended_recovery=(
                recovery_hint
                or "Use workspace_continue after the sandbox service recovers, or choose another available "
                "execution mode that still gives the worker its native capabilities."
            ),
            diagnostic_summary=message,
        )
    if required_version or "not installed" in lowered or "not on path" in lowered or _looks_like_runtime_dependency_or_version_failure(lowered):
        binary_hint = f" (`{binary_label}`)" if binary_label else ""
        profile_hint = f" for `{profile}`" if profile else ""
        version_hint = ""
        if required_version:
            version_hint = f" The required version is >= {required_version}."
            if actual_version:
                version_hint += f" Current version: {actual_version}."
        return FailureClassification(
            failure_class="runtime_dependency_missing",
            retryable=False,
            user_message=(
                f"GlassHive could not start the selected worker{profile_hint} because the required "
                f"host runtime dependency{binary_hint} is missing, unavailable, or incompatible."
                f"{version_hint}"
            ),
            recommended_recovery=(
                recovery_hint
                or "Use a configured managed dependency, choose another available worker profile, or use "
                "sandbox/workstation execution when that still satisfies the user's request. Ask the "
                "operator to change the host service runtime only when no configured recovery path is available."
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
    if (
        "stdin is closed" in lowered
        or "write_stdin failed" in lowered
        or "rerun exec_command with tty=true" in lowered
    ):
        return FailureClassification(
            failure_class="runtime_io_failed",
            retryable=True,
            user_message=(
                f"The {runtime_label} worker made progress, but its command session closed before "
                "GlassHive could capture the final turn cleanly."
            ),
            recommended_recovery=(
                "Use workspace_continue to resume from the same workspace and ask the worker to "
                "verify or finish from the current files."
            ),
            diagnostic_summary=message,
        )
    if "glasshive evidence check failed" in lowered:
        return FailureClassification(
            failure_class="glasshive_evidence_check_failed",
            retryable=True,
            user_message=(
                f"The {runtime_label} worker finished a provider turn, but GlassHive verification found "
                "that the result did not satisfy the generic completion or constraint contract."
            ),
            recommended_recovery=(
                "Open the View / Steer page and inspect the artifacts/evidence. Use workspace_continue "
                "to ask the same worker to repair the missing or invalid deliverables while preserving "
                "the original request and constraints."
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


def _extract_failure_strings(value: Any, *, path: str = "", failure_context: bool = False) -> list[str]:
    results: list[str] = []
    if isinstance(value, dict):
        event_type = str(value.get("type") or value.get("event") or value.get("name") or "")
        status = str(value.get("status") or value.get("code") or value.get("error_code") or "")
        event_is_failure = bool(event_type and _looks_failure_related(event_type))
        # Agent transcripts can contain failed exploratory sub-steps (for example a
        # shell command probing whether a path exists) even when the overall turn
        # completes successfully. Those are not provider/runtime failures; the
        # run evidence verifier separately checks final output, artifacts, and
        # constraints.
        status_is_failure = bool(
            status
            and _looks_failure_related(status)
            and not _is_agent_substep_status(value, event_type=event_type, path=path)
        )
        if event_is_failure:
            results.append(f"{path or 'event'} type={event_type}")
        if status_is_failure:
            results.append(f"{path or 'event'} status={status}")
        child_context = failure_context or event_is_failure or status_is_failure or _dict_has_failure_signal(value)
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            key_context = child_context or _failure_key_creates_context(key)
            if isinstance(child, str):
                if key_context and (_looks_failure_field(key) or _looks_failure_related(child)):
                    if _looks_failure_field(key) and not _failure_scalar_has_value(child):
                        continue
                    results.append(f"{child_path}: {child}")
            elif _looks_failure_field(key) and _failure_scalar_has_value(child):
                results.append(f"{child_path}: {child}")
            elif isinstance(child, (dict, list)):
                results.extend(_extract_failure_strings(child, path=child_path, failure_context=key_context))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            results.extend(_extract_failure_strings(child, path=f"{path}[{index}]", failure_context=failure_context))
    return results


def _is_agent_substep_status(value: dict[str, Any], *, event_type: str, path: str) -> bool:
    if not path:
        return False
    item_type = event_type.lower()
    if item_type not in {
        "command_execution",
        "file_change",
        "tool_call",
        "tool_result",
        "browser_action",
        "computer_action",
    }:
        return False
    if "item" not in path.split("."):
        return False
    # Keep structured provider errors visible even when nested.
    return not _dict_has_failure_signal(value)


def _dict_has_failure_signal(value: dict[str, Any]) -> bool:
    if value.get("is_error") is True:
        return True
    for key in ("api_error_status", "status_code", "error_code"):
        if _failure_scalar_has_value(value.get(key)):
            return True
    return any(key in value and _failure_scalar_has_value(value.get(key)) for key in ("error", "failure"))


def _failure_scalar_has_value(value: Any) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return bool(value.strip()) and value.strip().lower() not in {"false", "0", "no", "none", "null"}
    return True


def _failure_key_creates_context(key: str) -> bool:
    return key.lower() in {
        "api_error_status",
        "detail",
        "error",
        "error_code",
        "failure",
        "is_error",
        "status_code",
    }


def _looks_failure_field(key: str) -> bool:
    lowered = key.lower()
    return lowered in {
        "api_error_status",
        "detail",
        "error",
        "error_code",
        "failure",
        "is_error",
        "message",
        "result",
        "status_code",
    }


def _looks_like_provider_service_failure(lowered: str, *, structured: bool = False) -> bool:
    if (
        "api_error_status" in lowered and "529" in lowered
    ) or (
        "529" in lowered and "overloaded" in lowered
    ) or (
        "503" in lowered and ("service unavailable" in lowered or "temporarily unavailable" in lowered)
    ):
        return True
    if not structured:
        return False
    return (
        "overloaded" in lowered
        or "server-side issue" in lowered
        or "service unavailable" in lowered
        or "temporarily unavailable" in lowered
    )


def _has_contextual_status_code(lowered: str, codes: tuple[str, ...]) -> bool:
    for code in codes:
        status_after_label = re.search(
            rf"\b(?:api_error_status|status_code|status\s+code|http\s+status|response\s+status|http|status|code)"
            rf"\D{{0,24}}{re.escape(code)}\b",
            lowered,
        )
        status_before_reason = re.search(
            rf"\b{re.escape(code)}\b\D{{0,48}}"
            r"(?:unauthorized|forbidden|bad request|invalid request|too many requests|rate limit|"
            r"overloaded|service unavailable|temporarily unavailable)",
            lowered,
        )
        if status_after_label or status_before_reason:
            return True
    return False


def _looks_like_rate_limit_failure(lowered: str) -> bool:
    return (
        "too many requests" in lowered
        or "rate limit" in lowered
        or "rate_limit" in lowered
        or _has_contextual_status_code(lowered, ("429",))
    )


def _looks_like_provider_auth_failure(lowered: str) -> bool:
    return (
        "unauthorized" in lowered
        or "forbidden" in lowered
        or "invalid api key" in lowered
        or "not logged in" in lowered
        or "please run /login" in lowered
        or "please run login" in lowered
        or _has_contextual_status_code(lowered, ("401", "403"))
    )


def _looks_like_runtime_dependency_or_version_failure(lowered: str) -> bool:
    return (
        "requires node" in lowered
        or "needs node" in lowered
        or "node.js v" in lowered
        or "node v" in lowered
        or "unsupported engine" in lowered
        or "minimum node" in lowered
        or "minimum version" in lowered
        or "version mismatch" in lowered
        or "too old" in lowered
        or "exited with code 127" in lowered
        or "executable file not found" in lowered
        or "modulenotfounderror" in lowered
        or "no module named" in lowered
    )


def _looks_like_sandbox_lifecycle_failure(lowered: str) -> bool:
    return (
        "failed to prepare writable sandbox paths" in lowered
        or "failed to create worker sandbox" in lowered
        or "failed to start worker sandbox" in lowered
        or ("worker sandbox" in lowered and "is not running" in lowered)
        or "no such container" in lowered
        or ("docker daemon" in lowered and "not running" in lowered)
    )


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
        "unauthorized",
        "forbidden",
        "invalid api key",
        "response.failed",
        "turn.failed",
        "api error",
        "api_error_status",
        "overloaded",
        "server-side issue",
        "service unavailable",
        "temporarily unavailable",
    )
    return any(marker in lowered for marker in markers) or _has_contextual_status_code(
        lowered,
        ("400", "401", "403", "429", "503", "529"),
    )


_FAILURE_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{12,}"), r"\1[REDACTED]"),
    (re.compile(r"(?i)((?:api[_-]?key|token|secret|password|passwd|pwd)\s*[:=]\s*)[^\s\"']{6,}"), r"\1[REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"), "sk-[REDACTED]"),
    (re.compile(r"\b(?:wrk|run|prj)_[A-Za-z0-9_-]{6,}\b"), "[glasshive-id]"),
    (re.compile(r"\b[A-Za-z0-9_]{8,}:[A-Za-z0-9_./+=-]{20,}\b"), "[REDACTED_CREDENTIAL]"),
    (re.compile(r"(?:~\/|\/Users\/|\/home\/|\/private\/var\/|\/var\/folders\/|[A-Za-z]:\\Users\\)[^\s`'\"<>]+"), "[local path]"),
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
