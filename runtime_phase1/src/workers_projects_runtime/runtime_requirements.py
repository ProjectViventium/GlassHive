from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RuntimeRequirementIssue:
    profile: str
    runtime_name: str
    binary: str
    label: str
    problem: str
    required_version: str = ""
    actual_version: str = ""
    diagnostic_summary: str = ""
    recovery_hint: str = ""

    @property
    def user_message(self) -> str:
        profile_hint = f" for `{self.profile}`" if self.profile else ""
        if self.problem == "version_mismatch":
            actual = f" Current version: {self.actual_version}." if self.actual_version else ""
            return (
                f"GlassHive cannot start the selected host worker{profile_hint} because {self.label} "
                f"must be >= {self.required_version}.{actual}"
            )
        if self.problem == "version_unverified":
            return (
                f"GlassHive cannot start the selected host worker{profile_hint} because it could not "
                f"verify the configured {self.label} version required by this host profile."
            )
        return (
            f"GlassHive cannot start the selected host worker{profile_hint} because the required "
            f"host dependency `{self.label}` is not installed or not available to the GlassHive service."
        )

    @property
    def recommended_recovery(self) -> str:
        if self.recovery_hint:
            return self.recovery_hint
        return (
            "Use a configured managed dependency, choose another available worker profile, or use "
            "sandbox/workstation execution when that still satisfies the user's request. Ask the "
            "operator to change the host service runtime only when no configured recovery path is "
            "available."
        )


def host_runtime_requirement_issue(profile: str, runtime_name: str) -> RuntimeRequirementIssue | None:
    for requirement in host_runtime_requirements_for(profile, runtime_name):
        issue = _evaluate_requirement(requirement, profile=profile, runtime_name=runtime_name)
        if issue is not None:
            return issue
    return None


def host_runtime_requirements_for(profile: str, runtime_name: str) -> list[dict[str, Any]]:
    profile = str(profile or "").strip()
    runtime_name = str(runtime_name or "").strip()
    payload = _load_requirements_payload()
    requirements: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        for key in (profile, runtime_name, "*"):
            value = payload.get(key)
            if isinstance(value, dict):
                requirements.append(value)
            elif isinstance(value, list):
                requirements.extend(item for item in value if isinstance(item, dict))
        shared = payload.get("requirements")
        if isinstance(shared, list):
            for item in shared:
                if not isinstance(item, dict):
                    continue
                profiles = _csv_or_list(item.get("profiles") or item.get("profile") or item.get("worker_profiles"))
                runtimes = _csv_or_list(item.get("runtimes") or item.get("runtime") or item.get("runtime_names"))
                if profiles and profile not in profiles:
                    continue
                if runtimes and runtime_name not in runtimes:
                    continue
                requirements.append(item)
    elif isinstance(payload, list):
        requirements.extend(item for item in payload if isinstance(item, dict))
    return [_normalize_requirement(item) for item in requirements]


def _load_requirements_payload() -> Any:
    for env_name in ("GLASSHIVE_HOST_RUNTIME_REQUIREMENTS_FILE", "WPR_HOST_RUNTIME_REQUIREMENTS_FILE"):
        path = os.environ.get(env_name, "").strip()
        if not path:
            continue
        try:
            return json.loads(Path(path).expanduser().read_text())
        except Exception:
            return {}
    for env_name in ("GLASSHIVE_HOST_RUNTIME_REQUIREMENTS_JSON", "WPR_HOST_RUNTIME_REQUIREMENTS_JSON"):
        raw = os.environ.get(env_name, "").strip()
        if not raw:
            continue
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return {}


def _normalize_requirement(requirement: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(requirement)
    label = str(normalized.get("label") or normalized.get("name") or normalized.get("binary") or "").strip()
    if label:
        normalized["label"] = label
    min_version = str(
        normalized.get("min_version")
        or normalized.get("minimum_version")
        or normalized.get("version_at_least")
        or ""
    ).strip()
    if min_version:
        normalized["min_version"] = min_version
    return normalized


def _evaluate_requirement(
    requirement: dict[str, Any],
    *,
    profile: str,
    runtime_name: str,
) -> RuntimeRequirementIssue | None:
    binary = str(requirement.get("binary") or requirement.get("command") or "").strip()
    if not binary:
        return None
    label = str(requirement.get("label") or binary).strip()
    recovery_hint = str(requirement.get("recovery_hint") or "").strip()
    resolved = shutil.which(binary)
    if not resolved:
        return RuntimeRequirementIssue(
            profile=profile,
            runtime_name=runtime_name,
            binary=binary,
            label=_label_for_binary(label),
            problem="missing",
            diagnostic_summary=f"Missing host runtime dependency {label} ({binary})",
            recovery_hint=recovery_hint,
        )

    min_version = str(requirement.get("min_version") or "").strip()
    if not min_version:
        return None

    args = _version_args(requirement)
    try:
        completed = subprocess.run(
            [resolved, *args],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=float(requirement.get("version_timeout_sec") or 5),
        )
    except Exception as exc:
        return RuntimeRequirementIssue(
            profile=profile,
            runtime_name=runtime_name,
            binary=binary,
            label=_label_for_binary(label),
            problem="version_unverified",
            required_version=min_version,
            diagnostic_summary=f"Could not verify {label} version: {type(exc).__name__}",
            recovery_hint=recovery_hint,
        )

    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part).strip()
    actual_version = _extract_version(output)
    if completed.returncode != 0 or not actual_version:
        return RuntimeRequirementIssue(
            profile=profile,
            runtime_name=runtime_name,
            binary=binary,
            label=_label_for_binary(label),
            problem="version_unverified",
            required_version=min_version,
            actual_version=actual_version,
            diagnostic_summary=_truncate(output or f"{label} version command exited {completed.returncode}"),
            recovery_hint=recovery_hint,
        )
    if _version_tuple(actual_version) < _version_tuple(min_version):
        return RuntimeRequirementIssue(
            profile=profile,
            runtime_name=runtime_name,
            binary=binary,
            label=_label_for_binary(label),
            problem="version_mismatch",
            required_version=min_version,
            actual_version=actual_version,
            diagnostic_summary=f"{label} version {actual_version} is below required {min_version}",
            recovery_hint=recovery_hint,
        )
    return None


def _version_args(requirement: dict[str, Any]) -> list[str]:
    value = requirement.get("version_args")
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    value = requirement.get("version_arg")
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return ["--version"]


def _csv_or_list(value: Any) -> set[str]:
    if isinstance(value, str):
        return {item.strip() for item in value.split(",") if item.strip()}
    if isinstance(value, list):
        return {str(item).strip() for item in value if str(item).strip()}
    return set()


def _extract_version(text: str) -> str:
    match = re.search(r"\bv?(\d+(?:\.\d+){0,3})\b", str(text or ""))
    return match.group(1) if match else ""


def _version_tuple(value: str) -> tuple[int, int, int, int]:
    parts = [int(part) for part in re.findall(r"\d+", str(value or ""))[:4]]
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts[:4])


def _label_for_binary(value: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        return "runtime"
    return clean.replace("\\", "/").rstrip("/").split("/")[-1] if "/" in clean else clean


def _truncate(value: str, *, limit: int = 600) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."
