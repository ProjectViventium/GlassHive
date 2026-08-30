from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from datetime import datetime, timezone

from email.utils import parsedate_to_datetime


USER_RESUMABLE_FAILURE_CLASSES = frozenset(
    {
        "provider_auth_missing",
        "provider_connected_account_reconnect_required",
        "provider_unauthorized",
        "provider_context_limit_exceeded",
    }
)

def is_user_resumable_failure(*, failure_class: object, retryable: object) -> bool:
    """Return whether an explicit user Retry may continue the durable workspace.

    Authentication repair is not an automatic retry condition: it needs a real
    reconnect first.  It is nevertheless explicitly resumable once the user has
    repaired that external prerequisite, so Active Work must not strand the
    existing workspace behind a Dismiss-only terminal card.
    """

    return bool(retryable) or str(failure_class or "").strip().lower() in (
        USER_RESUMABLE_FAILURE_CLASSES
    )

@dataclass(frozen=True)
class FailureClassification:
    failure_class: str
    retryable: bool
    user_message: str
    recommended_recovery: str
    diagnostic_summary: str
    personal_account_reconnect: bool = False
    structured: bool = False
    retry_after_s: float | None = None
    provider_event_source: str = ""

    def as_store_fields(self) -> dict[str, Any]:
        return {
            "failure_class": self.failure_class,
            "failure_retryable": 1 if self.retryable else 0,
            "failure_structured": 1 if self.structured else 0,
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
    structured_authentication_failed = _has_structured_authentication_failed(
        stdout,
        stderr,
    )

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
                "The selected model provider rejected the worker turn because its usage quota or "
                "rate limit was reached."
            ),
            recommended_recovery=(
                "Retry after the provider-reported reset, restore provider quota, or authenticate an "
                "account with available quota; then use workspace_continue to resume the same workspace."
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
    if _looks_like_provider_auth_failure(
        lowered,
        structured_authentication_failed=structured_authentication_failed,
    ):
        return FailureClassification(
            failure_class="provider_auth_missing",
            retryable=False,
            user_message="The worker could not use the configured model provider credentials.",
            recommended_recovery=(
                "Fix the provider key, route configuration, or CLI login projected into this worker, "
                "then use workspace_continue to resume the same workspace."
            ),
            diagnostic_summary=diagnostic_summary,
            personal_account_reconnect=structured_authentication_failed,
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
    embedded = getattr(exc, "failure_classification", None)
    if isinstance(embedded, FailureClassification):
        return embedded
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

    structured_failure_class = str(getattr(exc, "failure_class", "") or "")
    if structured_failure_class == "host_capacity":
        capacity_class = str(getattr(exc, "capacity_class", "host") or "host")
        return FailureClassification(
            failure_class="host_capacity",
            retryable=True,
            user_message="The worker is waiting for host capacity and will retry.",
            recommended_recovery=(
                "No action is required. GlassHive will continue through the durable capacity queue; "
                "the exact work can also be stopped explicitly."
            ),
            diagnostic_summary=f"host capacity class={capacity_class}: {message}",
            structured=True,
        )
    if structured_failure_class == "provider_rate_limited":
        return FailureClassification(
            failure_class="provider_rate_limited",
            retryable=True,
            user_message=(
                "The model or research provider rate-limited the worker before it could finish."
            ),
            recommended_recovery=(
                "No action is required while GlassHive honors the provider retry window and retries "
                "the same workspace without changing the configured model or effort."
            ),
            diagnostic_summary=message,
            structured=True,
            retry_after_s=float(getattr(exc, "retry_after_s", 0) or 0) or None,
        )
    if structured_failure_class == "provider_quota_exhausted":
        return FailureClassification(
            failure_class="provider_quota_exhausted",
            retryable=True,
            user_message=(
                "The selected model provider quota was exhausted before the worker could finish."
            ),
            recommended_recovery=(
                "GlassHive will continue the same untouched mission with its configured fallback "
                "worker when one is available."
            ),
            diagnostic_summary=message,
            structured=True,
        )
    if structured_failure_class == "provider_auth_missing":
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
            structured=True,
        )
    structured_provider_failures = {
        "provider_unavailable": (
            True,
            "The configured model provider is temporarily unavailable.",
            "Continue the same workspace after the provider recovers.",
        ),
        "provider_auth_projection_unavailable": (
            True,
            "The model account authorization is temporarily unavailable for this mission.",
            "Retry the same durable workspace after Core can read the existing authorization.",
        ),
        "provider_connected_account_reconnect_required": (
            False,
            "The connected model account must be reconnected before this mission can continue.",
            "Reconnect the same model account, then resume this durable workspace.",
        ),
        "provider_unauthorized": (
            False,
            "The model provider rejected the configured credentials.",
            "Repair the configured credentials, then resume this durable workspace.",
        ),
        "provider_upstream_unavailable": (
            True,
            "The connected model provider is temporarily unavailable.",
            "Retry the same durable workspace after the provider recovers.",
        ),
        "provider_response_failed": (
            True,
            "The model provider ended the worker continuation unexpectedly before it could finish.",
            "Use workspace_continue to resume from the same durable workspace; GlassHive "
            "preserved the worker session, files, and completed research.",
        ),
        "provider_request_rejected": (
            False,
            "The model provider rejected the worker request before it could finish.",
            "Inspect the provider diagnostic and continue the same workspace only after correcting "
            "the provider route, request shape, or unsupported option.",
        ),
        "provider_content_filter": (
            False,
            "The worker was stopped by the model provider's safety filter before it could finish.",
            "Ask GlassHive to continue with a safer, narrower plan that preserves the original "
            "success criteria, or adjust the request if the filter was expected.",
        ),
        "provider_context_limit_exceeded": (
            False,
            "The worker's model request exceeded the provider context capacity before it could finish.",
            "Reduce the projected tool or evidence context, then resume the same durable workspace "
            "without rewriting the user's request.",
        ),
    }
    if structured_failure_class in structured_provider_failures:
        retryable, user_message, recommended_recovery = structured_provider_failures[
            structured_failure_class
        ]
        return FailureClassification(
            failure_class=structured_failure_class,
            retryable=retryable,
            user_message=user_message,
            recommended_recovery=recommended_recovery,
            diagnostic_summary=message,
            structured=True,
        )

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


def _has_structured_authentication_failed(*texts: str) -> bool:
    """Recognize the native provider result code, never echoed worker transcript text."""

    for text in texts:
        previous_auth_failure = False
        for line in str(text or "").splitlines():
            raw = line.strip()
            if not raw.startswith("{"):
                continue
            try:
                item = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict):
                continue
            exact_auth_failure = any(
                str(item.get(key) or "").strip().lower() == "authentication_failed"
                for key in ("error", "error_code", "api_error_status")
            )
            if item.get("is_error") is True and (
                exact_auth_failure
                or (previous_auth_failure and item.get("type") == "result")
            ):
                return True
            previous_auth_failure = (
                item.get("type") == "assistant" and exact_auth_failure
            )
    return False


def extract_structured_retry_after_seconds(*texts: str) -> float | None:
    """Read provider Retry-After only from JSONL control events, never prose."""

    delays: list[float] = []

    def parse_delay(value: object) -> float | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            delay = float(value)
        elif isinstance(value, str):
            clean = value.strip()
            try:
                delay = float(clean)
            except ValueError:
                try:
                    parsed = parsedate_to_datetime(clean)
                except (TypeError, ValueError, OverflowError):
                    return None
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                delay = (
                    parsed.astimezone(timezone.utc) - datetime.now(timezone.utc)
                ).total_seconds()
        else:
            return None
        if not (delay > 0):
            return None
        return min(delay, 86_400.0)

    def visit(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                normalized = re.sub(r"[-_]", "", str(key)).lower()
                if normalized in {"retryafter", "retryafterseconds"}:
                    parsed = parse_delay(child)
                    if parsed is not None:
                        delays.append(parsed)
                if isinstance(child, (dict, list)):
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for text in texts:
        for line in str(text or "").splitlines():
            raw = line.strip()
            if not raw.startswith("{"):
                continue
            try:
                decoded = json.loads(raw)
            except json.JSONDecodeError:
                continue
            trusted = _trusted_provider_control_event(decoded)
            if trusted is not None:
                visit(trusted)
    return max(delays) if delays else None

_STRUCTURED_RATE_LIMIT_CODES = frozenset(
    {
        "rate_limit",
        "rate_limit_error",
        "rate_limit_exceeded",
        "resource_exhausted",
        "throttled",
        "throttling_error",
    }
)

_STRUCTURED_QUOTA_CODES = frozenset(
    {
        "insufficient_quota",
        "plan_limit_reached",
        "quota_exhausted",
        "usage_limit_reached",
        "weekly_limit_reached",
    }
)

_STRUCTURED_PROVIDER_OUTAGE_CODES = frozenset(
    {
        "overloaded",
        "overloaded_error",
        "provider_unavailable",
        "service_unavailable",
    }
)

_STRUCTURED_PROVIDER_AUTH_CODES = frozenset(
    {
        "authentication_error",
        "authentication_required",
        "expired_token",
        "invalid_api_key",
        "invalid_authentication",
        "invalid_token",
        "not_authenticated",
        "oauth_required",
        "permission_denied",
        "provider_auth_missing",
        "provider_connected_account_reconnect_required",
        "provider_unauthorized",
        "unauthorized",
    }
)

_STRUCTURED_PROVIDER_REQUEST_CODES = frozenset(
    {
        "bad_request",
        "invalid_request",
        "invalid_request_error",
        "request_rejected",
        "unsupported_option",
    }
)

_STRUCTURED_PROVIDER_CONTENT_FILTER_CODES = frozenset(
    {
        "content_filter",
        "content_filter_error",
        "safety_filter",
    }
)

_STRUCTURED_STATUS_KEYS = frozenset(
    {"apierrorstatus", "httpstatus", "statuscode"}
)

_STRUCTURED_CODE_KEYS = frozenset(
    {"code", "errorcode", "errortype", "type"}
)

_TRUSTED_PROVIDER_CONTROL_EVENT_TYPES = frozenset(
    {"response.failed", "turn.failed"}
)

def _normalized_structured_code(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")

def _trusted_provider_control_event(value: object) -> dict[str, Any] | None:
    """Accept only native terminal/provider-failure schemas, never arbitrary JSON."""

    if not isinstance(value, dict):
        return None
    event_type = str(value.get("type") or "").strip().lower()
    if event_type in _TRUSTED_PROVIDER_CONTROL_EVENT_TYPES:
        error = value.get("error")
        return value if isinstance(error, dict) else None
    if (
        event_type == "result"
        and value.get("is_error") is True
        and str(value.get("terminal_reason") or "").strip().lower() == "api_error"
    ):
        try:
            status = int(value.get("api_error_status") or 0)
        except (TypeError, ValueError):
            return None
        return value if 400 <= status <= 599 else None
    return None

def _structured_provider_signals(*texts: str) -> tuple[set[int], set[str]]:
    """Extract exact provider control fields from JSONL without reading prose."""

    statuses: set[int] = set()
    codes: set[str] = set()

    def visit(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                normalized_key = re.sub(r"[^a-z0-9]", "", str(key).lower())
                if normalized_key in _STRUCTURED_STATUS_KEYS and not isinstance(child, bool):
                    try:
                        status = int(child)
                    except (TypeError, ValueError):
                        status = 0
                    if 100 <= status <= 599:
                        statuses.add(status)
                if normalized_key in _STRUCTURED_CODE_KEYS:
                    code = _normalized_structured_code(child)
                    if code:
                        codes.add(code)
                if isinstance(child, (dict, list)):
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for text in texts:
        for line in str(text or "").splitlines():
            raw = line.strip()
            if not raw.startswith("{"):
                continue
            try:
                decoded = json.loads(raw)
            except json.JSONDecodeError:
                continue
            trusted = _trusted_provider_control_event(decoded)
            if trusted is not None:
                visit(trusted)
    return statuses, codes

def _structured_provider_capacity_class(*texts: str) -> str:
    """Map only exact provider/runtime controls into capacity classes.

    Human-readable messages, stderr prose, and user/task output are deliberately
    excluded. Unknown structured failures remain generic provider failures.
    """

    statuses, codes = _structured_provider_signals(*texts)
    if codes & _STRUCTURED_QUOTA_CODES:
        return "provider_quota_exhausted"
    if 429 in statuses or codes & _STRUCTURED_RATE_LIMIT_CODES:
        return "provider_rate_limited"
    if statuses & {503, 529} or codes & _STRUCTURED_PROVIDER_OUTAGE_CODES:
        return "provider_response_failed"
    return ""

def _structured_provider_auth_failure(*texts: str) -> bool:
    """Recognize provider authentication only from exact JSONL control fields."""

    statuses, codes = _structured_provider_signals(*texts)
    return bool(statuses & {401, 403} or codes & _STRUCTURED_PROVIDER_AUTH_CODES)

def _terminal_native_provider_error(*texts: str) -> dict[str, Any] | None:
    """Return the final native result error without inspecting assistant or task prose."""

    terminal_result: dict[str, Any] | None = None
    for text in texts:
        for line in str(text or "").splitlines():
            raw = line.strip()
            if not raw.startswith("{"):
                continue
            try:
                decoded = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if (
                isinstance(decoded, dict)
                and str(decoded.get("type") or "").strip().lower() == "result"
            ):
                terminal_result = decoded
    if not terminal_result or terminal_result.get("is_error") is not True:
        return None
    if str(terminal_result.get("terminal_reason") or "").strip().lower() != "api_error":
        return None
    try:
        status = int(terminal_result.get("api_error_status") or 0)
    except (TypeError, ValueError):
        return None
    return terminal_result if 400 <= status <= 599 else None

def _terminal_native_context_limit(*texts: str) -> dict[str, Any] | None:
    """Recognize a final native prompt-capacity stop over stale earlier provider failures."""

    terminal_result: dict[str, Any] | None = None
    for text in texts:
        for line in str(text or "").splitlines():
            raw = line.strip()
            if not raw.startswith("{"):
                continue
            try:
                decoded = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if (
                isinstance(decoded, dict)
                and str(decoded.get("type") or "").strip().lower() == "result"
            ):
                terminal_result = decoded
    if not terminal_result or terminal_result.get("is_error") is not True:
        return None
    terminal_reason = str(terminal_result.get("terminal_reason") or "").strip().lower()
    result = " ".join(str(terminal_result.get("result") or "").lower().split())
    if terminal_reason == "blocking_limit" and result == "prompt is too long":
        return terminal_result
    return None

def _latest_native_attempt(stdout: str, stderr: str) -> tuple[str, str]:
    """Scope multi-invocation native logs to the last structured attempt.

    Codex emits a new ``thread.started`` record for each native invocation but no terminal
    ``result`` envelope. When a harness appends a fallback attempt to the same capture, earlier
    provider failures must not override the final attempt's recovery truth.
    """

    lines = str(stdout or "").splitlines()
    starts: list[int] = []
    for index, line in enumerate(lines):
        raw = line.strip()
        if not raw.startswith("{"):
            continue
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(decoded, dict)
            and str(decoded.get("type") or "").strip().lower() == "thread.started"
        ):
            starts.append(index)
    if len(starts) < 2:
        return stdout, stderr
    # Codex attempt boundaries are emitted on stdout.  stderr has no matching boundary marker, so
    # discarding it loses final-attempt structured auth/capacity evidence and can strand a durable
    # workspace behind an unknown, non-resumable failure.  Keep stderr; scoped final stdout remains
    # the first source consulted by the classifier.
    return "\n".join(lines[starts[-1] :]), stderr

def _classify_terminal_native_provider_error(
    terminal_result: dict[str, Any],
    *,
    diagnostic_summary: str,
) -> FailureClassification:
    """Classify the final provider attempt without inheriting stale earlier failures."""

    try:
        status = int(terminal_result.get("api_error_status") or 0)
    except (TypeError, ValueError):
        status = 0
    _statuses, codes = _structured_provider_signals(json.dumps(terminal_result))

    if status in {401, 403} or codes & _STRUCTURED_PROVIDER_AUTH_CODES:
        return FailureClassification(
            failure_class="provider_auth_missing",
            retryable=False,
            user_message="The worker could not use the configured model provider credentials.",
            recommended_recovery=(
                "Fix the provider key, route configuration, or CLI login projected into this "
                "worker, then use workspace_continue to resume the same workspace."
            ),
            diagnostic_summary=diagnostic_summary,
            structured=True,
            provider_event_source="provider_native",
        )
    if codes & _STRUCTURED_PROVIDER_CONTENT_FILTER_CODES:
        return FailureClassification(
            failure_class="provider_content_filter",
            retryable=False,
            user_message=(
                "The worker was stopped by the model provider's safety filter before it could "
                "finish."
            ),
            recommended_recovery=(
                "Ask GlassHive to continue with a safer, narrower plan that preserves the original "
                "success criteria, or adjust the request if the filter was expected."
            ),
            diagnostic_summary=diagnostic_summary,
            structured=True,
            provider_event_source="provider_native",
        )
    if codes & _STRUCTURED_QUOTA_CODES:
        return FailureClassification(
            failure_class="provider_quota_exhausted",
            retryable=True,
            user_message=(
                "The selected model provider quota was exhausted before the worker could finish."
            ),
            recommended_recovery=(
                "Keep the same configured model and effort queued until its provider quota resets, "
                "unless the user explicitly chooses a different model."
            ),
            diagnostic_summary=diagnostic_summary,
            structured=True,
            provider_event_source="provider_native",
        )
    if status == 429 or codes & _STRUCTURED_RATE_LIMIT_CODES:
        return FailureClassification(
            failure_class="provider_rate_limited",
            retryable=True,
            user_message=(
                "The model or research provider rate-limited the worker before it could finish."
            ),
            recommended_recovery=(
                "Use workspace_continue to resume the same workspace after a short wait, preserving "
                "the original task and any files already produced."
            ),
            diagnostic_summary=diagnostic_summary,
            structured=True,
            provider_event_source="provider_native",
        )
    if codes & _STRUCTURED_PROVIDER_REQUEST_CODES:
        return FailureClassification(
            failure_class="provider_request_rejected",
            retryable=False,
            user_message=(
                "The model provider rejected the worker request before it could finish."
            ),
            recommended_recovery=(
                "Inspect the provider diagnostic and continue the same workspace only after "
                "correcting the provider route, request shape, or unsupported option."
            ),
            diagnostic_summary=diagnostic_summary,
            structured=True,
        )

    return FailureClassification(
        failure_class="provider_response_failed",
        retryable=True,
        user_message=(
            "The model provider ended the worker continuation unexpectedly before it could finish."
        ),
        recommended_recovery=(
            "Use workspace_continue to resume from the same durable workspace; GlassHive preserved "
            "the worker session, files, and completed research."
        ),
        diagnostic_summary=diagnostic_summary,
        structured=True,
    )

_CORE_PROVIDER_PROXY_MESSAGES = {
    (409, "connect the configured model account, then resume this work."): (
        "provider_auth_missing",
        False,
        "The worker could not use the configured model provider credentials.",
        "Connect the configured model account, then resume this durable workspace.",
    ),
    (409, "reconnect the connected model account, then resume this work."): (
        "provider_connected_account_reconnect_required",
        False,
        "The connected model account must be reconnected before this mission can continue.",
        "Reconnect the same model account, then resume this durable workspace.",
    ),
    (409, "the model provider rejected the configured credentials."): (
        "provider_unauthorized",
        False,
        "The model provider rejected the configured credentials.",
        "Repair the configured credentials, then resume this durable workspace.",
    ),
    (503, "the model account authorization could not be read for this mission."): (
        "provider_auth_projection_unavailable",
        True,
        "The model account authorization is temporarily unavailable for this mission.",
        "GlassHive will retry the same durable workspace automatically after Core can read the "
        "existing authorization.",
    ),
    (502, "the connected model provider is temporarily unavailable."): (
        "provider_upstream_unavailable",
        True,
        "The connected model provider is temporarily unavailable.",
        "GlassHive will retry the same durable workspace after the provider recovers.",
    ),
}

def _classify_core_provider_proxy_failure(
    terminal_result: dict[str, Any] | None,
    *,
    diagnostic_summary: str,
) -> FailureClassification | None:
    """Preserve Core's exact provider contract from the final native result only."""

    if not terminal_result:
        return None
    try:
        status = int(terminal_result.get("api_error_status") or 0)
    except (TypeError, ValueError):
        return None
    result = " ".join(str(terminal_result.get("result") or "").strip().lower().split())
    for prefix in (f"api error: {status} ", f"api error: {status}: "):
        if result.startswith(prefix):
            result = result[len(prefix) :]
            break
    contract = _CORE_PROVIDER_PROXY_MESSAGES.get((status, result))
    if contract is None:
        return None
    failure_class, retryable, user_message, recommended_recovery = contract
    return FailureClassification(
        failure_class=failure_class,
        retryable=retryable,
        user_message=user_message,
        recommended_recovery=recommended_recovery,
        diagnostic_summary=diagnostic_summary,
        structured=True,
        provider_event_source="core_provider_proxy",
    )

def _structured_provider_auth_projection_unavailable(*texts: str) -> bool:
    """Recognize Core's exact clean-room auth blocker in a native result envelope.

    A fallback profile can append its result after the primary provider's quota event in the same
    durable run log. The terminal native result is authoritative; ordinary assistant/task prose is
    never inspected by this classifier.
    """

    terminal_result = _terminal_native_provider_error(*texts)
    if not terminal_result:
        return False
    try:
        status = int(terminal_result.get("api_error_status") or 0)
    except (TypeError, ValueError):
        return False
    result = " ".join(str(terminal_result.get("result") or "").lower().split())
    return (
        status == 409
        and str(terminal_result.get("terminal_reason") or "").strip().lower() == "api_error"
        and result
        == "api error: 409 the connected model account is unavailable for this mission."
    )

def _collect_prefixed_cli_stderr_failure_evidence(text: str) -> list[str]:
    """Collect native-CLI control errors without treating ordinary task prose as evidence."""

    return [line.strip() for line in text.splitlines() if line.lstrip().startswith("ERROR:")]

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
    for key in ("error_status", "api_error_status", "status_code", "error_code"):
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
        "error_status",
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
        "error_status",
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
        ("error_status" in lowered or "api_error_status" in lowered)
        and "529" in lowered
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
            rf"\b(?:error_status|api_error_status|status_code|status\s+code|http\s+status|response\s+status|http|status|code)"
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
        or "usage limit" in lowered
        or "usage_limit" in lowered
        or "quota exceeded" in lowered
        or "quota_exceeded" in lowered
        or "insufficient_quota" in lowered
        or _has_contextual_status_code(lowered, ("429",))
    )


def _looks_like_provider_auth_failure(
    lowered: str,
    *,
    structured_authentication_failed: bool = False,
) -> bool:
    return (
        "unauthorized" in lowered
        or "forbidden" in lowered
        or structured_authentication_failed
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
        "usage limit",
        "usage_limit",
        "quota exceeded",
        "quota_exceeded",
        "insufficient_quota",
        "unauthorized",
        "forbidden",
        "invalid api key",
        "response.failed",
        "turn.failed",
        "api error",
        "error_status",
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
