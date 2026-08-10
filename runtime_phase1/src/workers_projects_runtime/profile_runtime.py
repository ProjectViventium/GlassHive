from __future__ import annotations

import json
import base64
import hashlib
import logging
import os
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import time
try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 compatibility
    import tomli as tomllib
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Lock, Thread

from .bootstrap import (
    GLASSHIVE_CRITICAL_OPERATING_INSTRUCTIONS,
    GLASSHIVE_NATIVE_CAPABILITY_INVENTORY,
    GLASSHIVE_SAFETY_CHECKPOINT_RULE,
    GLASSHIVE_WORKER_COMPLETION_CONTRACT,
    bootstrap_env_for,
    claude_project_mcp_payload_for_bundle,
    glasshive_project_claude_md,
    glasshive_project_codex_md,
    merge_glasshive_worker_instructions,
    refresh_project_runtime_files_for_worker,
    refresh_runtime_env_for_worker,
    resolve_bootstrap_source_path,
)
from .docker_sandbox import DockerSandboxManager
from .failure_classification import classify_cli_failure, classify_runtime_error
from .capability_broker import (
    GlassHiveCapabilityBroker,
    worker_with_ephemeral_capability_bundle,
)
from .inference_broker import (
    GlassHiveInferenceBroker,
    InferenceBrokerError,
    validated_codex_broker_projection,
)
from .mission_provider_accounts import (
    MissionProviderAccountBinder,
    apply_bound_provider_account_environment,
    mission_provider_account_selection,
)
from .openclaw_runtime import (
    RuntimeErrorBase,
    RuntimeDependencyMissingError,
    RuntimeInfo,
    WorkerInterruptedError,
    WorkerPausedError,
    WorkerRuntime,
    WorkerTerminatedError,
    _PROVIDER_ENV_KEYS,
    notify_runtime_started,
    runtime_start_boundary,
)
from .openclaw_release import reviewed_openclaw_env
from .runtime_requirements import host_runtime_requirement_issue
from .run_evidence import (
    build_constraint_ledger,
    build_run_evidence,
    write_constraint_ledger,
    write_run_evidence,
)
from .terminal_takeover import TerminalTarget


logger = logging.getLogger(__name__)

_CODEX_MCP_SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")
_HOST_CODEX_NATIVE_MCP_ALLOWLIST = ("computer-use", "node_repl")
_FALSEY_ENV_VALUES = {"0", "false", "no", "off", "none", "disabled"}
_CODEX_BROKER_CONFLICTING_ENV = {
    "CODEX_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_API_BASE",
    "OPENAI_REVERSE_PROXY",
    "PORTKEY_API_KEY",
    "PORTKEY_BASE_URL",
    "PORTKEY_PROVIDER",
    "PORTKEY_VIRTUAL_KEY",
    "PORTKEY_CONFIG",
}
_RUN_SCOPED_CREDENTIAL_ENV_KEYS = tuple(
    sorted(
        set(_PROVIDER_ENV_KEYS)
        | _CODEX_BROKER_CONFLICTING_ENV
        | {
            "ANTHROPIC_AUTH_TOKEN",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "AWS_BEARER_TOKEN_BEDROCK",
        }
    )
)
_MAX_TELEMETRY_LINE_BYTES = 1024 * 1024
_ACTIVE_RUN_STATUS_LOCK = Lock()
_ACTIVE_RUN_TERMINAL_STATES = frozenset(
    {"completed", "failed", "timeout", "paused", "interrupted", "terminated"}
)
_TELEMETRY_INTEGER_FIELDS = frozenset(
    {
        "duration_ms",
        "duration_api_ms",
        "ttft_ms",
        "ttft_stream_ms",
        "time_to_request_ms",
        "num_turns",
        "api_retry_count",
        "api_retry_delay_ms",
        "tool_call_count",
        "event_count",
        "assistant_event_count",
        "malformed_line_count",
        "oversized_line_count",
        "sample_sequence",
        "parsed_bytes",
        "log_bytes",
        "stream_input_tokens",
        "stream_output_tokens",
        "stream_cache_read_input_tokens",
        "stream_cache_creation_input_tokens",
    }
)
_TELEMETRY_TOKEN_FIELDS = frozenset(
    {
        "claude_code_version",
        "model",
        "service_tier",
        "speed",
        "result_state",
        "stop_reason",
        "telemetry_scope",
        "run_id",
    }
)
_TELEMETRY_TIMESTAMP_FIELDS = frozenset(
    {
        "first_timestamp",
        "last_timestamp",
        "sampled_at",
        "first_observed_at",
        "last_progress_at",
    }
)
_SAFE_TELEMETRY_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.:/+-]{0,63}$")
_TELEMETRY_TOKEN_PATTERNS = {
    "claude_code_version": re.compile(r"^\d+(?:\.\d+){1,3}(?:[-+][A-Za-z0-9.-]+)?$"),
    "model": re.compile(r"^(?:claude|anthropic)[A-Za-z0-9_.:/+-]{0,120}$"),
    "service_tier": re.compile(r"^(?:standard|priority|batch|flex|default)$"),
    "speed": re.compile(r"^(?:standard|fast|normal|default)$"),
    "result_state": re.compile(
        r"^(?:success|error|failed|cancelled|interrupted|timeout|error_[a-z0-9_]{1,48})$"
    ),
    "stop_reason": re.compile(
        r"^(?:end_turn|max_tokens|tool_use|stop_sequence|refusal|error|timeout|"
        r"cancelled|interrupted|terminated|paused|completed)$"
    ),
    "telemetry_scope": re.compile(
        r"^(?:full_active_run_incremental|full_active_run|console_tail|"
        r"active_run_unavailable)$"
    ),
    "run_id": re.compile(r"^(?:run[_-]?|r)[A-Za-z0-9_-]{0,120}$"),
}


def _safe_run_telemetry(value: object, *, run_id: str | None = None) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    safe: dict[str, object] = {}
    for key in _TELEMETRY_INTEGER_FIELDS:
        if key not in value or isinstance(value[key], bool):
            continue
        try:
            safe[key] = max(0, int(value[key]))
        except (TypeError, ValueError, OverflowError):
            continue
    for key in _TELEMETRY_TOKEN_FIELDS:
        text = str(value.get(key) or "").strip()
        if text and _TELEMETRY_TOKEN_PATTERNS[key].fullmatch(text):
            safe[key] = text
    for key in _TELEMETRY_TIMESTAMP_FIELDS:
        text = str(value.get(key) or "").strip()
        if not text:
            continue
        try:
            datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            continue
        safe[key] = text[:128]
    if isinstance(value.get("is_error"), bool):
        safe["is_error"] = value["is_error"]
    if isinstance(value.get("partial_line_present"), bool):
        safe["partial_line_present"] = value["partial_line_present"]
    for key in ("seconds_since_progress", "total_cost_usd"):
        if key not in value or isinstance(value[key], bool):
            continue
        try:
            number = float(value[key])
        except (TypeError, ValueError, OverflowError):
            continue
        if number == number and abs(number) != float("inf") and number >= 0:
            safe[key] = number
    statuses = value.get("api_retry_statuses")
    if isinstance(statuses, (list, tuple)):
        safe_statuses = sorted(
            {
                str(item).strip()
                for item in statuses[:20]
                if re.fullmatch(r"^[A-Za-z0-9_.:/+-]{1,32}$", str(item).strip())
            }
        )
        safe["api_retry_statuses"] = safe_statuses
    counts = value.get("tool_call_counts")
    if isinstance(counts, dict):
        safe_counts: dict[str, int] = {}
        for raw_name, raw_count in list(counts.items())[:100]:
            name = str(raw_name or "").strip()
            if not _SAFE_TELEMETRY_NAME.fullmatch(name) or isinstance(raw_count, bool):
                continue
            try:
                safe_counts[name] = max(0, int(raw_count))
            except (TypeError, ValueError, OverflowError):
                continue
        if safe_counts:
            safe["tool_call_counts"] = dict(sorted(safe_counts.items()))
    expected_run_id = str(run_id or "").strip()
    nested_run_id = str(safe.get("run_id") or "").strip()
    if expected_run_id:
        if nested_run_id and nested_run_id != expected_run_id:
            return {}
        safe["run_id"] = expected_run_id
    if safe:
        safe["schema"] = "glasshive.claude-run-telemetry.v1"
    return safe


def _write_private_json(path: Path, value: object) -> bool:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(value, sort_keys=True))
        temporary.chmod(0o600)
        os.replace(temporary, path)
        path.chmod(0o600)
        return True
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def _claude_effort_help_supports(help_text: str, effort: str) -> bool:
    marker = help_text.find("--effort")
    if marker < 0:
        return False
    if effort != "xhigh":
        return True
    return re.search(r"\bxhigh\b", help_text[marker : marker + 320], flags=re.IGNORECASE) is not None
_CLAUDE_AUTH_REFRESH_LOCK = Lock()


def _codex_mcp_section_server_name(section_name: str) -> str | None:
    section_name = section_name.strip()
    if not section_name.startswith("mcp_servers."):
        return None
    server = section_name[len("mcp_servers.") :].split(".", 1)[0].strip()
    return server.strip("\"'") or None


def _codex_mcp_server_names(config_text: str) -> set[str]:
    names: set[str] = set()
    for line in config_text.splitlines():
        match = _CODEX_MCP_SECTION_RE.match(line)
        if not match:
            continue
        server = _codex_mcp_section_server_name(match.group(1))
        if server:
            names.add(server)
    return names


def _select_codex_mcp_server_blocks(config_text: str, names: set[str]) -> str:
    if not config_text.strip() or not names:
        return ""
    output: list[str] = []
    keeping = False
    for line in config_text.splitlines():
        section = _CODEX_MCP_SECTION_RE.match(line)
        if section:
            server = _codex_mcp_section_server_name(section.group(1))
            keeping = server in names if server else False
        if keeping:
            output.append(line)
    return "\n".join(output).strip()


def _strip_codex_mcp_server_blocks(config_text: str, names: set[str]) -> str:
    if not config_text.strip() or not names:
        return config_text.rstrip()
    output: list[str] = []
    skipping = False
    for line in config_text.splitlines():
        section = _CODEX_MCP_SECTION_RE.match(line)
        if section:
            server = _codex_mcp_section_server_name(section.group(1))
            skipping = server in names if server else False
        if not skipping:
            output.append(line)
    return "\n".join(output).rstrip()


def _sanitize_malformed_codex_source_config(
    config_text: str,
    preserve_names: set[str],
    append_names: set[str],
) -> str:
    output: list[str] = []
    keeping = True
    for line in config_text.splitlines():
        section = _CODEX_MCP_SECTION_RE.match(line)
        if section:
            section_name = section.group(1).strip()
            if section_name == "mcp_servers" or section_name.startswith("mcp_servers."):
                server = _codex_mcp_section_server_name(section_name)
                keeping = bool(server and server in preserve_names and server not in append_names)
            else:
                keeping = True
        if keeping:
            output.append(line)
    return "\n".join(output).rstrip()


def _toml_string(value: object) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _path_entries(value: str) -> list[Path]:
    entries: list[Path] = []
    for raw in str(value or "").split(os.pathsep):
        raw = raw.strip()
        if not raw:
            continue
        entries.append(Path(raw).expanduser())
    return entries


def _append_unique_paths(existing: str, additions: list[Path]) -> str:
    parts = [part for part in str(existing or "").split(os.pathsep) if part]
    seen = set(parts)
    for path in additions:
        value = str(path)
        if value and value not in seen:
            parts.append(value)
            seen.add(value)
    return os.pathsep.join(parts)


def _existing_dirs_from_env(*names: str) -> list[Path]:
    found: list[Path] = []
    seen: set[str] = set()
    for name in names:
        for path in _path_entries(os.environ.get(name, "")):
            try:
                resolved = path.resolve()
            except OSError:
                resolved = path
            key = str(resolved)
            if key in seen or not path.is_dir():
                continue
            seen.add(key)
            found.append(path)
    return found


def _workspace_dependency_auto_discovery_enabled() -> bool:
    raw = (
        os.environ.get("GLASSHIVE_AUTO_DISCOVER_CODEX_WORKSPACE_DEPS", "").strip()
        or os.environ.get("WPR_AUTO_DISCOVER_CODEX_WORKSPACE_DEPS", "").strip()
    )
    return raw.lower() not in _FALSEY_ENV_VALUES


def _codex_workspace_dependency_roots() -> list[Path]:
    roots = _existing_dirs_from_env("GLASSHIVE_CODEX_WORKSPACE_DEPS_ROOT", "WPR_CODEX_WORKSPACE_DEPS_ROOT")
    if _workspace_dependency_auto_discovery_enabled():
        default_root = Path.home() / ".cache" / "codex-runtimes" / "codex-primary-runtime" / "dependencies"
        if default_root.is_dir():
            roots.append(default_root)
    deduped: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        try:
            key = str(root.resolve())
        except OSError:
            key = str(root)
        if key not in seen:
            seen.add(key)
            deduped.append(root)
    return deduped


def _workspace_dependency_paths() -> dict[str, list[Path]]:
    node_modules = _existing_dirs_from_env("GLASSHIVE_WORKSPACE_NODE_MODULES", "WPR_WORKSPACE_NODE_MODULES")
    bin_dirs = _existing_dirs_from_env("GLASSHIVE_WORKSPACE_BIN_DIRS", "WPR_WORKSPACE_BIN_DIRS")
    node_bins = _existing_dirs_from_env("GLASSHIVE_WORKSPACE_NODE_BIN", "WPR_WORKSPACE_NODE_BIN")
    python_bins = _existing_dirs_from_env("GLASSHIVE_WORKSPACE_PYTHON_BIN", "WPR_WORKSPACE_PYTHON_BIN")
    for root in _codex_workspace_dependency_roots():
        candidate_node_modules = root / "node" / "node_modules"
        if candidate_node_modules.is_dir():
            node_modules.append(candidate_node_modules)
        candidate_node_bin = root / "node" / "bin"
        if candidate_node_bin.is_dir():
            node_bins.append(candidate_node_bin)
        candidate_bin = root / "bin"
        if candidate_bin.is_dir():
            bin_dirs.append(candidate_bin)
        candidate_python_bin = root / "python" / "bin"
        if candidate_python_bin.is_dir():
            python_bins.append(candidate_python_bin)

    output: dict[str, list[Path]] = {}
    for key, values in {
        "node_modules": node_modules,
        "node_bins": node_bins,
        "python_bins": python_bins,
        "bin_dirs": bin_dirs,
    }.items():
        deduped: list[Path] = []
        seen: set[str] = set()
        for value in values:
            try:
                resolved = value.resolve()
            except OSError:
                resolved = value
            path_key = str(resolved)
            if path_key in seen or not value.is_dir():
                continue
            seen.add(path_key)
            deduped.append(value)
        output[key] = deduped
    return output


def _project_workspace_dependency_env(env: dict[str, str]) -> None:
    paths = _workspace_dependency_paths()
    executable_dirs = [
        *paths.get("node_bins", []),
        *paths.get("python_bins", []),
        *paths.get("bin_dirs", []),
    ]
    if executable_dirs:
        env["PATH"] = _append_unique_paths(env.get("PATH", ""), executable_dirs)
    node_modules = paths.get("node_modules", [])
    if node_modules:
        env["NODE_PATH"] = _append_unique_paths(env.get("NODE_PATH") or os.environ.get("NODE_PATH", ""), node_modules)
        env["GLASSHIVE_WORKSPACE_NODE_MODULES"] = os.pathsep.join(str(path) for path in node_modules)
    if paths.get("node_bins"):
        env["GLASSHIVE_WORKSPACE_NODE_BIN"] = os.pathsep.join(str(path) for path in paths["node_bins"])
    if paths.get("python_bins"):
        env["GLASSHIVE_WORKSPACE_PYTHON_BIN"] = os.pathsep.join(str(path) for path in paths["python_bins"])
    if paths.get("bin_dirs"):
        env["GLASSHIVE_WORKSPACE_BIN_DIRS"] = os.pathsep.join(str(path) for path in paths["bin_dirs"])


def _toml_value(value: object, *, manifest_dir: Path | None = None, key: str = "") -> str | None:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str):
        rendered = str(manifest_dir) if key == "cwd" and value == "." and manifest_dir else value
        return _toml_string(rendered)
    if isinstance(value, list):
        rendered_items: list[str] = []
        for item in value:
            item_rendered = _toml_value(item)
            if item_rendered is None:
                return None
            rendered_items.append(item_rendered)
        return "[" + ", ".join(rendered_items) + "]"
    return None


def _toml_table_name(name: str) -> str:
    return name if re.fullmatch(r"[A-Za-z0-9_-]+", name) else _toml_string(name)


def _render_codex_mcp_server_from_json(name: str, config: object, manifest_dir: Path) -> str:
    if not isinstance(config, dict):
        return ""
    root_lines = [f"[mcp_servers.{_toml_table_name(name)}]"]
    nested: list[tuple[str, dict[str, object]]] = []
    for key, value in config.items():
        key_text = str(key).strip()
        if not key_text:
            continue
        if isinstance(value, dict):
            nested.append((key_text, value))
            continue
        rendered = _toml_value(value, manifest_dir=manifest_dir, key=key_text)
        if rendered is not None:
            root_lines.append(f"{_toml_table_name(key_text)} = {rendered}")
    for nested_key, nested_values in nested:
        nested_lines = [f"[mcp_servers.{_toml_table_name(name)}.{_toml_table_name(nested_key)}]"]
        for key, value in nested_values.items():
            rendered = _toml_value(value)
            if rendered is not None:
                nested_lines.append(f"{_toml_table_name(str(key))} = {rendered}")
        if len(nested_lines) > 1:
            root_lines.extend(["", *nested_lines])
    return "\n".join(root_lines).strip()


def _render_toml_document(data: dict[str, object]) -> str:
    root_lines: list[str] = []
    table_blocks: list[str] = []
    for key, value in data.items():
        key_text = str(key).strip()
        if not key_text:
            continue
        if isinstance(value, dict):
            table_blocks.extend(_render_toml_table([key_text], value))
            continue
        rendered = _toml_value(value)
        if rendered is not None:
            root_lines.append(f"{_toml_table_name(key_text)} = {rendered}")
    blocks: list[str] = []
    if root_lines:
        blocks.append("\n".join(root_lines))
    blocks.extend(table_blocks)
    return "\n\n".join(block for block in blocks if block.strip()).strip()


_PLUGIN_ID_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]*@[A-Za-z0-9][A-Za-z0-9._-]*"
)


def _host_plugin_denylist() -> tuple[str, ...]:
    raw = (
        os.environ.get("GLASSHIVE_HOST_PLUGIN_DENYLIST", "").strip()
        or os.environ.get("WPR_HOST_PLUGIN_DENYLIST", "").strip()
    )
    values: list[str] = []
    for item in raw.split(","):
        plugin_id = item.strip()
        if not plugin_id:
            continue
        if not _PLUGIN_ID_RE.fullmatch(plugin_id):
            raise RuntimeErrorBase(
                "Host plugin denylist entries must use the canonical name@marketplace plugin ID"
            )
        if plugin_id not in values:
            values.append(plugin_id)
    return tuple(values)


def _host_codex_personality() -> str:
    personality = os.environ.get("WPR_CODEX_CLI_PERSONALITY", "inherit").strip().lower()
    if personality not in {"inherit", "none", "friendly", "pragmatic"}:
        raise RuntimeErrorBase(
            "Host Codex personality must be inherit, none, friendly, or pragmatic"
        )
    return personality


def _host_codex_conversation_project_instructions() -> str:
    mode = os.environ.get(
        "WPR_CODEX_CLI_CONVERSATION_PROJECT_INSTRUCTIONS",
        "inherit",
    ).strip().lower()
    if mode not in {"inherit", "exclude"}:
        raise RuntimeErrorBase(
            "Host Codex conversation project instructions must be inherit or exclude"
        )
    return mode


def _host_codex_personality_policy_state() -> str:
    configured = _host_codex_personality()
    if configured != "inherit":
        return configured
    source_config = Path(
        os.environ.get("CODEX_HOME") or (Path.home() / ".codex")
    ).expanduser() / "config.toml"
    try:
        parsed = tomllib.loads(source_config.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return "inherit"
    inherited = str(parsed.get("personality") or "").strip().lower()
    if inherited in {"none", "friendly", "pragmatic"}:
        return f"inherit:{inherited}"
    return "inherit"


def _apply_codex_plugin_denylist(config_text: str, plugin_ids: tuple[str, ...]) -> str:
    if not plugin_ids:
        return config_text.strip()
    try:
        parsed = tomllib.loads(config_text) if config_text.strip() else {}
    except Exception as exc:
        raise RuntimeErrorBase(
            "Cannot apply the host plugin denylist because the Codex worker config is invalid"
        ) from exc
    if not isinstance(parsed, dict):
        parsed = {}
    plugins = parsed.get("plugins")
    if not isinstance(plugins, dict):
        plugins = {}
        parsed["plugins"] = plugins
    for plugin_id in plugin_ids:
        existing = plugins.get(plugin_id)
        entry = dict(existing) if isinstance(existing, dict) else {}
        entry["enabled"] = False
        plugins[plugin_id] = entry
    return _render_toml_document(parsed)


def _apply_codex_personality(config_text: str, personality: str) -> str:
    if personality == "inherit":
        return config_text.strip()
    try:
        parsed = tomllib.loads(config_text) if config_text.strip() else {}
    except Exception as exc:
        raise RuntimeErrorBase(
            "Cannot apply the host Codex personality because the worker config is invalid"
        ) from exc
    if not isinstance(parsed, dict):
        parsed = {}
    parsed["personality"] = personality
    return _render_toml_document(parsed)


def _apply_codex_developer_instructions(
    config_text: str,
    developer_instructions: str | None,
) -> str:
    if developer_instructions is None:
        return config_text.strip()
    try:
        parsed = tomllib.loads(config_text) if config_text.strip() else {}
    except Exception as exc:
        raise RuntimeErrorBase(
            "Cannot apply host Codex developer instructions because the worker config is invalid"
        ) from exc
    if not isinstance(parsed, dict):
        parsed = {}
    if developer_instructions:
        parsed["developer_instructions"] = developer_instructions
    else:
        parsed.pop("developer_instructions", None)
    return _render_toml_document(parsed)


def _assert_codex_worker_policy(
    config_text: str,
    *,
    plugin_ids: tuple[str, ...],
    personality: str,
    developer_instructions: str | None = None,
) -> None:
    try:
        parsed = tomllib.loads(config_text) if config_text.strip() else {}
    except Exception as exc:
        raise RuntimeErrorBase("Host Codex worker policy config is invalid") from exc
    plugins = parsed.get("plugins") if isinstance(parsed, dict) else None
    for plugin_id in plugin_ids:
        entry = plugins.get(plugin_id) if isinstance(plugins, dict) else None
        if not isinstance(entry, dict) or entry.get("enabled") is not False:
            raise RuntimeErrorBase(
                "Host Codex plugin denylist policy was not materialized; refusing to launch"
            )
    if personality != "inherit" and (
        not isinstance(parsed, dict) or parsed.get("personality") != personality
    ):
        raise RuntimeErrorBase(
            "Host Codex personality policy was not materialized; refusing to launch"
        )
    if developer_instructions is not None and str(
        parsed.get("developer_instructions") or ""
    ) != developer_instructions:
        raise RuntimeErrorBase(
            "Host Codex developer instruction authority was not materialized; refusing to launch"
        )


def _render_toml_table(path: list[str], table: dict[str, object]) -> list[str]:
    scalar_lines: list[str] = []
    nested_blocks: list[str] = []
    for key, value in table.items():
        key_text = str(key).strip()
        if not key_text:
            continue
        if isinstance(value, dict):
            nested_blocks.extend(_render_toml_table([*path, key_text], value))
            continue
        rendered = _toml_value(value)
        if rendered is not None:
            scalar_lines.append(f"{_toml_table_name(key_text)} = {rendered}")
    blocks: list[str] = []
    if scalar_lines:
        table_name = ".".join(_toml_table_name(part) for part in path)
        blocks.append("\n".join([f"[{table_name}]", *scalar_lines]))
    blocks.extend(nested_blocks)
    return blocks


def _sanitize_codex_source_config(config_text: str, preserve_names: set[str], append_names: set[str]) -> str:
    if not config_text.strip():
        return ""
    try:
        parsed = tomllib.loads(config_text)
    except Exception:
        return _sanitize_malformed_codex_source_config(config_text, preserve_names, append_names)
    if not isinstance(parsed, dict):
        return ""
    sanitized: dict[str, object] = {
        str(key): value
        for key, value in parsed.items()
        if str(key) != "mcp_servers"
    }
    mcp_servers = parsed.get("mcp_servers")
    if isinstance(mcp_servers, dict):
        kept_servers = {
            str(name): value
            for name, value in mcp_servers.items()
            if str(name) in preserve_names and str(name) not in append_names
        }
        if kept_servers:
            sanitized["mcp_servers"] = kept_servers
    return _render_toml_document(sanitized)


# Keep prompt templates near the top. Host-native workers read these through real files in their
# workspace (`harness-prompt.md`, `AGENTS.md`, `CLAUDE.md`, `CODEX.md`) and through the command-line
# instruction wrapper. The constants live here so future edits do not require spelunking through the
# host runtime implementation.
_COMPLETION_CONTRACT = GLASSHIVE_WORKER_COMPLETION_CONTRACT
HOST_NATIVE_HARNESS_PROMPT = f"""# GlassHive Host-Native Harness

You are running directly on the user's main computer, not inside a sandbox.
You may use the local browser, filesystem, shell, and installed OS tools.
Default execution is no-approval/full-access for this worker class.

{GLASSHIVE_CRITICAL_OPERATING_INSTRUCTIONS}

Operational requirements:
- Treat the workspace directory as the primary project root.
- Keep `work-log.md` current with concise progress, blockers, and completion notes.
- Write files for the task inside the workspace unless the project definition explicitly requires another path.
- Before destructive host changes, stop and emit a clear checkpoint request instead of guessing.
- Destructive host changes include writes outside the workspace, git push, global installs, launch agents, cron, SSH/keychain/browser credentials, killing unrelated processes, and broad network exfiltration.
- Do not print credentials, tokens, cookies, personal data, or private local paths unless absolutely required for the local operator.
- When invoking local `.sh` helper scripts from mounted or downloaded tool folders, run them through `bash /path/to/script.sh ...` so macOS quarantine/provenance metadata cannot block direct execution.
- For screen evidence on macOS, prefer the workspace helper `glasshive-host-tools/capture-front-window.sh` and invoke it with `bash`.
- For web research or document-generation tasks, prefer `python3 glasshive-host-tools/content-hygiene.py readable <html-file>` before putting page text into structured files, and run `python3 glasshive-host-tools/content-hygiene.py check <csv-or-json-file>...` before final delivery when the output contains sourced research fields.
- If you create research plans, specs, subagent prompts, or delegation notes, carry the user's source/date/auth/scope constraints forward exactly instead of widening, weakening, or rewriting them.
- Keep source publication/evidence dates distinct from retrieval/access timestamps; an access date must not widen or replace a user-limited source window.
- When `glasshive-run/constraint-ledger.json` exists, treat it as the canonical constraint reminder for this run and compare plans, delegated prompts, artifacts, and the final report against it before final delivery.
- `glasshive-run/` is internal harness evidence, not a user-facing artifact directory.
- For host browser or desktop tasks, first use the user's existing local app/session when the task asks for the main computer, Chrome, browser profile, local files, or installed OS tools. Do not claim host control is unavailable until you have checked the available local shell/desktop/browser automation paths.

{GLASSHIVE_NATIVE_CAPABILITY_INVENTORY}

{GLASSHIVE_WORKER_COMPLETION_CONTRACT}

{GLASSHIVE_SAFETY_CHECKPOINT_RULE}

Required context files in this workspace:
- project-definition.md
- work-log.md
- harness-prompt.md
- AGENTS.md (canonical project instructions for Codex-style workers)
- agents.md (compatibility mirror)
- CLAUDE.md / claude.md (Claude Code compatibility; should import or mirror AGENTS.md when possible)
- CODEX.md / codex.md (legacy compatibility mirror only)
"""
HOST_DEFAULT_AGENTS_MD = (
    "Follow these AGENTS.md project instructions and keep `work-log.md` updated.\n"
    "When the task involves the host browser, desktop, files, shell, or installed apps, operate on the real local machine session unless the project definition explicitly says sandbox.\n"
    "If `glasshive-run/constraint-ledger.json` exists, use it as the canonical run constraint reminder and keep `glasshive-run/` out of user-facing deliverables.\n"
    "For sourced work, keep source publication/evidence dates distinct from retrieval/access timestamps; an access date must not widen or replace a user-limited source window.\n"
    f"{GLASSHIVE_NATIVE_CAPABILITY_INVENTORY}\n"
    "Before `FINAL REPORT:`, inspect the concrete output, files/artifacts, tool results, or visible state you produced; compare it with the user's request and success criteria; then continue, fix, or report the exact blocker.\n"
    "End with `FINAL REPORT:` containing the user-facing result in the user's requested form; mention artifacts only when you intentionally created user-facing files and blockers only when they remain.\n"
)
HOST_DEFAULT_CLAUDE_MD = (
    "Claude host worker context. Treat AGENTS.md as the canonical project instruction source and use bypass permission mode only for this GlassHive workspace.\n"
    "For host browser/desktop tasks, check local automation paths before reporting unavailable. Before `FINAL REPORT:`, inspect the result against the user's request and success criteria. End with `FINAL REPORT:`."
)
HOST_DEFAULT_CODEX_MD = (
    "Codex host worker context. AGENTS.md is the canonical project instruction source; this file is a compatibility mirror.\n"
    "For host browser/desktop tasks, check local automation paths before reporting unavailable. Before `FINAL REPORT:`, inspect the result against the user's request and success criteria. End with `FINAL REPORT:`."
)
HOST_CONTENT_HYGIENE_TOOL = r'''#!/usr/bin/env python3
"""Small, dependency-free helper for research artifact hygiene.

Usage:
  python3 glasshive-host-tools/content-hygiene.py readable page.html [output.txt]
  python3 glasshive-host-tools/content-hygiene.py check file.csv [file.json ...]

The helper is generic on purpose: it strips common page chrome/script noise from HTML and flags
structured cells that still look like navigation, cookie banners, CSS, JavaScript, or raw page dumps.
"""
from __future__ import annotations

import csv
import html
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable


BAD_TEXT_RE = re.compile(
    r"skip\s+(?:to\s+)?(?:main\s+)?(?:content|navigation)"
    r"|cookie\s+(?:settings|preferences|policy)"
    r"|privacy\s+policy"
    r"|terms\s+(?:of\s+(?:use|service)|and\s+conditions)"
    r"|all\s+rights\s+reserved"
    r"|(?:read|view)\s+(?:more|all)"
    r"|please\s+enable\s+(?:java\s*)?script|enable\s+js"
    r"|subscribe\s+to\s+continue|sign\s+in\s+to\s+continue|paywall"
    r"|are\s+you\s+(?:a\s+)?robot|captcha|bot\s+wall|403\s+forbidden|access\s+denied"
    r"|lp\s+login|dataroom"
    r"|sourceurl=|__nuxt__|@layer|tailwindcss"
    r"|\b(?:window|document)\.[A-Za-z_$]"
    r"|\bfunction\s+(?:[A-Za-z_$][\w$]*)?\s*\([^)]*\)\s*\{"
    r"|\bvar\s+[A-Za-z_$][\w$]*\s*="
    r"|&(?:nbsp|amp|lt|gt|quot|apos);|&#\d+;|&#x[0-9a-f]+;",
    re.IGNORECASE,
)
CHROME_LINE_RE = re.compile(r"^(?:menu|close|follow us:?|linkedin|facebook|instagram|x)$", re.IGNORECASE)


class ReadableHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg", "template"}:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg", "template"} and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = re.sub(r"\s+", " ", html.unescape(data)).strip()
        if len(text) >= 2:
            self._chunks.append(text)

    def text(self) -> str:
        lines: list[str] = []
        previous = ""
        for chunk in self._chunks:
            if chunk == previous:
                continue
            previous = chunk
            if CHROME_LINE_RE.fullmatch(chunk.strip()):
                continue
            if BAD_TEXT_RE.search(chunk) and len(chunk) < 90:
                continue
            lines.append(chunk)
        return "\n".join(lines).strip() + ("\n" if lines else "")


def readable(path: Path) -> str:
    parser = ReadableHTMLParser()
    parser.feed(path.read_text(encoding="utf-8", errors="ignore"))
    return parser.text()


def iter_structured_values(path: Path) -> Iterable[tuple[str, int, str, str]]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open(newline="", encoding="utf-8-sig") as handle:
            for row_index, row in enumerate(csv.DictReader(handle), start=2):
                for field, value in row.items():
                    yield (path.name, row_index, str(field), str(value or ""))
        return
    if suffix in {".json", ".jsonl"}:
        text = path.read_text(encoding="utf-8", errors="ignore")
        records = [json.loads(line) for line in text.splitlines() if line.strip()] if suffix == ".jsonl" else [json.loads(text)]
        for row_index, record in enumerate(records, start=1):
            yield from _walk_json(path.name, row_index, "", record)


def _walk_json(name: str, row_index: int, prefix: str, value: object) -> Iterable[tuple[str, int, str, str]]:
    if isinstance(value, dict):
        for key, nested in value.items():
            next_prefix = f"{prefix}.{key}" if prefix else str(key)
            yield from _walk_json(name, row_index, next_prefix, nested)
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            yield from _walk_json(name, row_index, f"{prefix}[{index}]", nested)
    else:
        yield (name, row_index, prefix, str(value or ""))


def check(paths: list[Path]) -> int:
    failures: list[dict[str, object]] = []
    for path in paths:
        for name, row_index, field, value in iter_structured_values(path):
            text = " ".join(value.split())
            if not text:
                continue
            match = BAD_TEXT_RE.search(text)
            if match:
                failures.append(
                    {
                        "file": name,
                        "row": row_index,
                        "field": field,
                        "match": match.group(0),
                        "sample": text[:180],
                    }
                )
            elif len(text) > 900 and not re.search(r"(source|evidence|notes|summary|description|rationale)", field, re.I):
                failures.append(
                    {
                        "file": name,
                        "row": row_index,
                        "field": field,
                        "match": "overlong structured cell",
                        "sample": text[:180],
                    }
                )
    print(json.dumps({"ok": not failures, "failures": failures[:50], "failure_count": len(failures)}, indent=2))
    return 1 if failures else 0


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[1] not in {"readable", "check"}:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    command = argv[1]
    paths = [Path(arg) for arg in argv[2:]]
    if command == "readable":
        text = readable(paths[0])
        if len(paths) > 1:
            paths[1].write_text(text, encoding="utf-8")
        else:
            print(text, end="")
        return 0
    return check(paths)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
'''


def _instruction_with_completion_contract(instruction: str) -> str:
    body = str(instruction or "").strip()
    return f"{body}\n\n{_COMPLETION_CONTRACT}" if body else _COMPLETION_CONTRACT


def _instruction_file_pointer_message(path: str) -> str:
    return "\n".join(
        [
            "Read the full GlassHive task instruction from this local file and follow it exactly:",
            path,
            "",
            "The file contains the user's task and the GlassHive completion contract.",
            "If the file is unavailable, report a clear runtime setup failure instead of guessing.",
        ]
    )


class ProfiledWorkerRuntime:
    def __init__(
        self,
        base_dir: str | None = None,
        provider_account_db_path: str | None = None,
    ) -> None:
        self.openclaw = OpenClawWorkstationRuntime(base_dir=base_dir)
        self.codex = CodexCliRuntime(base_dir=base_dir)
        self.claude = ClaudeCodeRuntime(base_dir=base_dir)
        self.host_openclaw = HostOpenClawRuntime(base_dir=base_dir)
        self.host_codex = HostCodexCliRuntime(base_dir=base_dir)
        self.host_claude = HostClaudeCodeRuntime(base_dir=base_dir)
        self._provider_log_cache: dict[tuple[str, str], dict[str, object]] = {}
        self._provider_log_cache_lock = Lock()
        provider_home_root = Path(
            os.environ.get("GLASSHIVE_PROVIDER_ACCOUNT_HOME_ROOT")
            or (self.codex.base_dir / "provider_accounts")
        ).expanduser()
        self.provider_account_binder = MissionProviderAccountBinder(
            db_path=provider_account_db_path,
            home_root=provider_home_root,
        )
        self.inference_broker = GlassHiveInferenceBroker.from_environment()
        self.capability_broker = GlassHiveCapabilityBroker.from_environment()

    def _runtime_for_profile(self, profile: str, execution_mode: str = "docker") -> WorkerRuntime:
        if execution_mode == "host":
            if profile == "codex-cli":
                return self.host_codex
            if profile == "claude-code":
                return self.host_claude
            return self.host_openclaw
        if profile == "codex-cli":
            return self.codex
        if profile == "claude-code":
            return self.claude
        return self.openclaw

    def _runtime_for_worker(self, worker: dict) -> WorkerRuntime:
        return self._runtime_for_profile(
            str(worker.get("profile") or "openclaw-general"),
            str(worker.get("execution_mode") or "docker"),
        )

    def resolve_model(self, profile: str, execution_mode: str = "docker") -> str:
        return self._runtime_for_profile(profile, execution_mode).resolve_model(profile)

    def preflight_worker_profile(self, profile: str, execution_mode: str = "docker") -> None:
        runtime = self._runtime_for_profile(profile, execution_mode)
        if hasattr(runtime, "preflight_worker_profile"):
            runtime.preflight_worker_profile(profile, execution_mode)

    def reconcile_provider_account_binding(self, account_home: Path) -> None:
        """Use the shared Docker substrate to repair an account before setup or verification."""

        self.codex.reconcile_provider_account_binding(account_home)

    def ensure_worker_ready(self, worker: dict) -> RuntimeInfo:
        return self._runtime_for_worker(worker).ensure_worker_ready(worker)

    def prepare_worker_workspace(self, worker: dict) -> RuntimeInfo:
        runtime = self._runtime_for_worker(worker)
        prepare = getattr(runtime, "prepare_worker_workspace", None)
        if not callable(prepare):
            raise RuntimeErrorBase("The selected worker runtime cannot prepare a workspace without starting compute")
        return prepare(worker)

    def pause_worker(self, worker: dict) -> RuntimeInfo:
        try:
            return self._runtime_for_worker(worker).pause_worker(worker)
        finally:
            try:
                self._revoke_active_capability_grant(worker)
            finally:
                self._revoke_active_inference_grant(worker)

    def terminate_worker(self, worker: dict) -> RuntimeInfo:
        runtime = self._runtime_for_worker(worker)
        try:
            return runtime.terminate_worker(worker)
        finally:
            try:
                self._revoke_active_capability_grant(worker)
            finally:
                self._revoke_active_inference_grant(worker)

    def interrupt_worker(self, worker: dict, run_id: str | None = None) -> RuntimeInfo:
        runtime = self._runtime_for_worker(worker)
        try:
            if hasattr(runtime, "interrupt_worker"):
                try:
                    return runtime.interrupt_worker(worker, run_id=run_id)
                except TypeError as exc:
                    if "run_id" not in str(exc):
                        raise
                    return runtime.interrupt_worker(worker)
            return runtime.pause_worker(worker)
        finally:
            try:
                self._revoke_active_capability_grant(worker, run_id=run_id)
            finally:
                self._revoke_active_inference_grant(worker, run_id=run_id)

    def _revoke_active_capability_grant(
        self,
        worker: dict,
        *,
        run_id: str | None = None,
    ) -> None:
        self.capability_broker.revoke_active(
            tenant_id=str(worker.get("tenant_id") or "local").strip() or "local",
            owner_id=str(worker.get("owner_id") or "").strip(),
            worker_id=str(worker.get("worker_id") or "").strip(),
            run_id=str(run_id).strip() if run_id else None,
        )

    def _revoke_active_inference_grant(
        self,
        worker: dict,
        *,
        run_id: str | None = None,
    ) -> None:
        self.inference_broker.revoke_active(
            tenant_id=str(worker.get("tenant_id") or "local").strip() or "local",
            owner_id=str(worker.get("owner_id") or "").strip(),
            worker_id=str(worker.get("worker_id") or "").strip(),
            run_id=str(run_id).strip() if run_id else None,
        )

    def _run_observed_provider_account_task(
        self,
        *,
        runtime: WorkerRuntime,
        worker: dict,
        instruction: str,
        timeout_sec: float | None,
        run_id: str,
        account_id: str,
    ) -> str:
        """Run with a selected account and persist only telemetry the harness observed."""

        started_at = time.monotonic()
        succeeded = False
        try:
            result = runtime.run_task(
                worker,
                instruction,
                timeout_sec=timeout_sec,
                run_id=run_id,
            )
            succeeded = True
            return result
        finally:
            usage: dict[str, object] = {}
            reader = getattr(runtime, "run_usage", None)
            if callable(reader):
                try:
                    reported = reader(worker, run_id)
                    if isinstance(reported, dict):
                        usage = reported
                except Exception:
                    logger.warning(
                        "Could not read worker-reported provider account usage",
                        exc_info=True,
                        extra={"worker_id": worker.get("worker_id"), "run_id": run_id},
                    )

            def reported_token(name: str) -> int | None:
                value = usage.get(name)
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    return None
                return value

            store = self.provider_account_binder.store
            if store is not None:
                try:
                    store.record_provider_account_usage(
                        account_id=account_id,
                        tenant_id=str(worker.get("tenant_id") or "local").strip() or "local",
                        owner_id=str(worker.get("owner_id") or "").strip(),
                        succeeded=succeeded,
                        duration_seconds=max(0.0, time.monotonic() - started_at),
                        input_tokens=reported_token("input_tokens"),
                        output_tokens=reported_token("output_tokens"),
                    )
                except Exception:
                    # Usage accounting must not replace the worker's result or original failure.
                    logger.warning(
                        "Could not persist GlassHive-observed provider account usage",
                        exc_info=True,
                        extra={"worker_id": worker.get("worker_id"), "run_id": run_id},
                    )

    def _run_with_inference_broker(
        self,
        *,
        runtime: WorkerRuntime,
        worker: dict,
        instruction: str,
        timeout_sec: float | None,
        run_id: str,
        account: dict,
        selection,
        preferred: bool,
    ) -> str:
        runtime_name = str(
            getattr(runtime, "runtime_name", "") or worker.get("profile") or ""
        ).strip()
        provider = str(account.get("provider") or "").strip().lower()
        status = str(account.get("status") or "").strip().lower()
        if runtime_name != "codex-cli" or provider not in {"codex", "openai"}:
            raise RuntimeErrorBase(
                "Selected provider account does not match this worker profile"
            )
        if status not in {"ready", "action_required", "unavailable", "error"}:
            if preferred:
                return runtime.run_task(
                    {
                        **worker,
                        "_glasshive_provider_account_preferred_fallback": True,
                    },
                    instruction,
                    timeout_sec=timeout_sec,
                    run_id=run_id,
                )
            raise RuntimeErrorBase(
                "Selected OpenAI connection is not ready; reconnect or verify it before running"
            )
        model = str(
            worker.get("model")
            or runtime.resolve_model(str(worker.get("profile") or "codex-cli"))
            or ""
        ).strip()
        try:
            with self.inference_broker.bind_run(
                tenant_id=str(worker.get("tenant_id") or "local").strip() or "local",
                owner_id=str(worker.get("owner_id") or "").strip(),
                worker_id=str(worker.get("worker_id") or "").strip(),
                run_id=run_id,
                auth_method=str(account.get("auth_method") or "").strip(),
                models=[model],
            ) as projection:
                if status != "ready":
                    self.provider_account_binder.update_selected_account_status(
                        worker,
                        selection,
                        status="ready",
                    )
                bound_worker = {
                    **worker,
                    "model": model,
                    "_glasshive_inference_broker_bound": True,
                    "_glasshive_inference_broker": projection,
                }
                return self._run_observed_provider_account_task(
                    runtime=runtime,
                    worker=bound_worker,
                    instruction=instruction,
                    timeout_sec=timeout_sec,
                    run_id=run_id,
                    account_id=str(account.get("account_id") or selection.account_id),
                )
        except InferenceBrokerError as exc:
            unavailable_codes = {
                "broker_unavailable",
                "enterprise_route_unavailable",
                "proxy_route_unavailable",
            }
            self.provider_account_binder.update_selected_account_status(
                worker,
                selection,
                status="unavailable" if exc.code in unavailable_codes else "action_required",
                reconnect_reason=str(exc),
            )
            if not preferred:
                raise
            return runtime.run_task(
                {
                    **worker,
                    "_glasshive_provider_account_preferred_fallback": True,
                },
                instruction,
                timeout_sec=timeout_sec,
                run_id=run_id,
            )

    def _run_task_with_provider_account(
        self,
        worker: dict,
        instruction: str,
        *,
        timeout_sec: float | None,
        run_id: str,
    ) -> str:
        runtime = self._runtime_for_worker(worker)
        selection = mission_provider_account_selection(worker)
        if selection is None:
            return runtime.run_task(
                worker,
                instruction,
                timeout_sec=timeout_sec,
                run_id=run_id,
            )
        effective_run_id = run_id
        runtime_name = str(
            getattr(runtime, "runtime_name", "")
            or worker.get("profile")
            or ""
        ).strip()
        account = self.provider_account_binder.selected_account_record(worker, selection)
        if account is not None and str(account.get("auth_method") or "").strip().lower() in {
            "api_key",
            "enterprise_route",
        }:
            return self._run_with_inference_broker(
                runtime=runtime,
                worker=worker,
                instruction=instruction,
                timeout_sec=timeout_sec,
                run_id=effective_run_id,
                account=account,
                selection=selection,
                preferred=selection.policy == "personal_preferred",
            )
        with self.provider_account_binder.bind(
            worker,
            runtime_name=runtime_name,
            run_id=effective_run_id,
            timeout_sec=timeout_sec,
            release_binding=(
                getattr(runtime, "release_provider_account_binding", None)
                if str(worker.get("execution_mode") or "docker").strip().lower() == "docker"
                else None
            ),
            abort_binding=lambda bound_worker: runtime.terminate_worker(bound_worker),
            reconcile_binding=(
                getattr(runtime, "reconcile_provider_account_binding", None)
                if str(worker.get("execution_mode") or "docker").strip().lower() == "docker"
                else None
            ),
        ) as bound_worker:
            if bound_worker.get("_glasshive_provider_account_bound"):
                return self._run_observed_provider_account_task(
                    runtime=runtime,
                    worker=bound_worker,
                    instruction=instruction,
                    timeout_sec=timeout_sec,
                    run_id=effective_run_id,
                    account_id=selection.account_id,
                )
            return runtime.run_task(
                bound_worker,
                instruction,
                timeout_sec=timeout_sec,
                run_id=effective_run_id,
            )

    def _run_mode_from_worker(self, worker: dict) -> str:
        raw_bundle = worker.get("bootstrap_bundle_json")
        if isinstance(raw_bundle, str) and raw_bundle.strip():
            try:
                parsed = json.loads(raw_bundle)
            except json.JSONDecodeError:
                parsed = {}
        else:
            candidate = worker.get("bootstrap_bundle")
            parsed = candidate if isinstance(candidate, dict) else {}
        if not isinstance(parsed, dict):
            return "mission"
        return str(parsed.get("run_mode") or "mission").strip().lower()

    def run_task(
        self,
        worker: dict,
        instruction: str,
        timeout_sec: float | None = None,
        run_id: str | None = None,
    ) -> str:
        """Bind fresh connected capabilities for every UI, scheduled, or MCP mission.

        The grant bundle exists only in this call stack. It is never written back to the worker,
        run database, public API response, or reusable workspace record. Direct Conversation
        workers already carry their signed, request-scoped broker bundle from LibreChat and must
        not be wrapped by the standalone mission issuer a second time.
        """

        effective_run_id = (run_id or secrets.token_hex(8)).strip()
        if self._run_mode_from_worker(worker) == "conversation":
            return self._run_task_with_provider_account(
                worker,
                instruction,
                timeout_sec=timeout_sec,
                run_id=effective_run_id,
            )
        tenant_id = str(worker.get("tenant_id") or "local").strip() or "local"
        owner_id = str(worker.get("owner_id") or "").strip()
        worker_id = str(worker.get("worker_id") or "").strip()
        execution_mode = str(worker.get("execution_mode") or "docker").strip().lower()
        with self.capability_broker.bind_run(
            tenant_id=tenant_id,
            owner_id=owner_id,
            worker_id=worker_id,
            run_id=effective_run_id,
            execution_mode=execution_mode,
        ) as (bundle, _readiness):
            projected_worker = (
                worker_with_ephemeral_capability_bundle(worker, bundle)
                if bundle
                else worker
            )
            return self._run_task_with_provider_account(
                projected_worker,
                instruction,
                timeout_sec=timeout_sec,
                run_id=effective_run_id,
            )

    def run_usage(self, worker: dict, run_id: str) -> dict[str, int]:
        runtime = self._runtime_for_worker(worker)
        reader = getattr(runtime, "run_usage", None)
        return dict(reader(worker, run_id)) if callable(reader) else {}

    def run_telemetry(self, worker: dict, run_id: str) -> dict[str, object]:
        runtime = self._runtime_for_worker(worker)
        reader = getattr(runtime, "run_telemetry", None)
        return dict(reader(worker, run_id)) if callable(reader) else {}

    def live_telemetry(
        self,
        worker: dict,
        stdout: str,
        run_id: str | None = None,
    ) -> dict[str, object]:
        runtime = self._runtime_for_worker(worker)
        reader = getattr(runtime, "live_telemetry", None)
        if not callable(reader):
            return {}
        try:
            return dict(reader(worker, stdout, run_id=run_id))
        except TypeError as exc:
            if "run_id" not in str(exc):
                raise
            if run_id:
                return {
                    "schema": "glasshive.claude-run-telemetry.v1",
                    "run_id": str(run_id),
                    "telemetry_scope": "active_run_unavailable",
                }
            return dict(reader(worker, stdout))

    def worker_capacity_error(self, worker: dict) -> RuntimeErrorBase | None:
        runtime = self._runtime_for_worker(worker)
        checker = getattr(runtime, "worker_capacity_error", None)
        if callable(checker):
            return checker(worker)
        return None

    def reconcile_worker(self, worker: dict) -> RuntimeInfo:
        return self._runtime_for_worker(worker).reconcile_worker(worker)

    def worker_compute_present(self, worker: dict) -> bool:
        runtime = self._runtime_for_worker(worker)
        checker = getattr(runtime, "worker_compute_present", None)
        if callable(checker):
            return bool(checker(worker))
        return bool(runtime.reconcile_worker(worker).pid)

    def terminal_target(self, worker: dict) -> TerminalTarget:
        runtime = self._runtime_for_worker(worker)
        if hasattr(runtime, "terminal_target"):
            return runtime.terminal_target(worker)
        workspace_dir = str(worker.get("workspace_dir") or "")
        return TerminalTarget(
            command=["screen", "-xRR", f"wpr-{worker['worker_id']}"],
            cwd=workspace_dir,
            env={"TERM": "xterm-256color"},
            title=f"{worker['name']} terminal",
            subtitle="Host workspace terminal",
        )

    def describe_worker(self, worker: dict) -> dict[str, object]:
        runtime = self._runtime_for_worker(worker)
        if hasattr(runtime, "describe_worker"):
            return runtime.describe_worker(worker)
        return {
            "mode": "host-process",
            "runtime": str(worker.get("runtime") or "openclaw"),
            "workspace_dir": str(worker.get("workspace_dir") or ""),
            "state_dir": str(worker.get("state_dir") or ""),
        }

    def effort_projection_for_worker(self, worker: dict) -> dict[str, object]:
        runtime = self._runtime_for_worker(worker)
        resolver = getattr(runtime, "effort_projection_for_worker", None)
        if not callable(resolver):
            return {}
        projection = resolver(worker)
        return dict(projection) if isinstance(projection, dict) else {}

    def provider_activity_log(self, worker: dict, run_id: str) -> tuple[str, str]:
        """Return the private native JSONL log used for provider activity normalization."""

        clean_run_id = str(run_id or "").strip()
        if not clean_run_id or "/" in clean_run_id or "\\" in clean_run_id or clean_run_id in {".", ".."}:
            raise ValueError("invalid run id")
        runtime = self._runtime_for_worker(worker)
        run_root = runtime._run_root(str(worker["worker_id"]), clean_run_id)
        stdout_path = run_root / "stdout.log"
        if not stdout_path.is_file():
            return str(worker.get("profile") or ""), ""
        try:
            max_bytes = max(
                1024,
                int(
                    os.environ.get(
                        "GLASSHIVE_PROVIDER_LOG_WINDOW_BYTES",
                        str(8 * 1024 * 1024),
                    )
                    or str(8 * 1024 * 1024)
                ),
            )
        except ValueError:
            max_bytes = 8 * 1024 * 1024
        cache_key = (str(worker["worker_id"]), clean_run_id)
        stat = stdout_path.stat()
        with self._provider_log_cache_lock:
            cached = self._provider_log_cache.get(cache_key)
            if (
                cached
                and int(cached.get("size") or -1) == stat.st_size
                and int(cached.get("mtime_ns") or -1) == stat.st_mtime_ns
            ):
                return str(worker.get("profile") or ""), str(cached.get("rendered") or "")

            data = b""
            previous_size = int(cached.get("size") or 0) if cached else 0
            previous_data = cached.get("data") if cached else b""
            if (
                cached
                and isinstance(previous_data, bytes)
                and previous_size < stat.st_size
                and stat.st_size - previous_size <= max_bytes
            ):
                with stdout_path.open("rb") as handle:
                    handle.seek(previous_size)
                    data = previous_data + handle.read()
            else:
                with stdout_path.open("rb") as handle:
                    handle.seek(max(0, stat.st_size - max_bytes))
                    data = handle.read(max_bytes)

            if len(data) > max_bytes:
                data = data[-max_bytes:]
            excluded_bytes = max(0, stat.st_size - len(data))
            if excluded_bytes:
                newline = data.find(b"\n")
                if newline >= 0:
                    data = data[newline + 1 :]
                else:
                    data = b""
                excluded_bytes = stat.st_size - len(data)
            text = data.decode("utf-8", errors="ignore")
            if excluded_bytes:
                marker = json.dumps(
                    {
                        "type": "glasshive.log_compacted",
                        "excluded_prefix_bytes": excluded_bytes,
                    },
                    separators=(",", ":"),
                )
                text = f"{marker}\n{text}"
            self._provider_log_cache[cache_key] = {
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "data": data,
                "rendered": text,
            }
            while len(self._provider_log_cache) > 16:
                self._provider_log_cache.pop(next(iter(self._provider_log_cache)))
        return str(worker.get("profile") or ""), text

    def collect_completed_run(
        self,
        worker: dict,
        run_id: str | None = None,
        instruction: str | None = None,
    ) -> dict[str, object] | None:
        runtime = self._runtime_for_worker(worker)
        if hasattr(runtime, "collect_completed_run"):
            try:
                return runtime.collect_completed_run(worker, run_id=run_id, instruction=instruction)
            except TypeError as exc:
                if "instruction" in str(exc):
                    try:
                        return runtime.collect_completed_run(worker, run_id=run_id)
                    except TypeError as run_id_exc:
                        if "run_id" not in str(run_id_exc):
                            raise
                        return runtime.collect_completed_run(worker)
                if "run_id" not in str(exc):
                    raise
                return runtime.collect_completed_run(worker)
        return None

    def desktop_action(
        self,
        worker: dict,
        action: str,
        *,
        url: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, object]:
        runtime = self._runtime_for_worker(worker)
        if hasattr(runtime, "desktop_action"):
            return runtime.desktop_action(worker, action, url=url, run_id=run_id)
        raise RuntimeErrorBase(f"Desktop actions are not supported for profile {worker.get('profile') or 'unknown'}")


class BaseCliWorkerRuntime:
    runtime_name = "cli"
    worker_root_name = "cli_runtime"
    binary_env_var = ""
    binary_name = ""

    def __init__(self, base_dir: str | None = None) -> None:
        self.base_dir = Path(base_dir) if base_dir else Path(__file__).resolve().parents[2] / "data"
        self.runtime_root = self.base_dir / self.worker_root_name
        self.logs_dir = self.runtime_root / "logs"
        self.workers_dir = self.runtime_root / "workers"
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.workers_dir.mkdir(parents=True, exist_ok=True)
        self.binary = os.environ.get(self.binary_env_var, self.binary_name)
        self._process_lock = Lock()
        self._active_processes: dict[str, subprocess.Popen[str]] = {}
        self._stop_reasons: dict[tuple[str, str | None], str] = {}
        self._live_telemetry_lock = Lock()
        self._live_telemetry_cache: dict[tuple[str, str], dict[str, object]] = {}
        self.sandbox = DockerSandboxManager(base_dir=str(self.base_dir))

    def resolve_model(self, profile: str) -> str:
        raise NotImplementedError

    def preflight_worker_profile(self, profile: str, execution_mode: str = "docker") -> None:
        return None

    def _default_session_key(self, worker: dict) -> str | None:
        return worker.get("session_key") or f"worker:{worker['worker_id']}"

    def _instruction_with_completion_contract(self, instruction: str) -> str:
        return _instruction_with_completion_contract(instruction)

    def _command_stdin_text(self, worker: dict, instruction: str, info: RuntimeInfo) -> str | None:
        return None

    def _worker_root(self, worker_id: str) -> Path:
        return self.sandbox.paths(worker_id)["worker_root"]

    def _state_dir(self, worker_id: str) -> Path:
        return self.sandbox.paths(worker_id)["state_dir"]

    def _workspace_dir(self, worker_id: str) -> Path:
        return self.sandbox.paths(worker_id)["workspace_dir"]

    def _home_dir(self, worker_id: str) -> Path:
        return self.sandbox.paths(worker_id)["home_dir"]

    def _session_meta_path(self, worker_id: str) -> Path:
        return self._state_dir(worker_id) / "session.json"

    def _active_session_meta_path(self, worker_id: str) -> Path:
        return self._state_dir(worker_id) / "active_terminal_session.json"

    def _run_root(self, worker_id: str, run_id: str) -> Path:
        return self._home_dir(worker_id) / ".glasshive-runs" / run_id

    def _container_run_root(self, run_id: str) -> str:
        return f"{self.sandbox.home_mount}/.glasshive-runs/{run_id}"

    def _ensure_dirs(self, worker_id: str) -> None:
        self._workspace_dir(worker_id).mkdir(parents=True, exist_ok=True)
        self._home_dir(worker_id).mkdir(parents=True, exist_ok=True)

    def _usage_from_output(self, stdout: str) -> dict[str, int]:
        _ = stdout
        return {}

    def _telemetry_from_output(self, stdout: str) -> dict[str, object]:
        _ = stdout
        return {}

    def _record_run_usage(self, worker_id: str, run_id: str, stdout: str) -> dict[str, int]:
        usage = self._usage_from_output(stdout)
        if usage:
            path = self._run_root(worker_id, run_id) / "usage.json"
            _write_private_json(path, usage)
        return usage

    def _record_run_telemetry(self, worker_id: str, run_id: str, stdout: str) -> dict[str, object]:
        telemetry = _safe_run_telemetry(self._telemetry_from_output(stdout), run_id=run_id)
        if telemetry:
            path = self._run_root(worker_id, run_id) / "telemetry.json"
            if not _write_private_json(path, telemetry):
                logger.warning(
                    "Failed to persist GlassHive run telemetry",
                    extra={"worker_id": worker_id, "run_id": run_id},
                )
        return telemetry

    def _record_run_metrics(
        self,
        worker_id: str,
        run_id: str,
        stdout: str,
    ) -> tuple[dict[str, int], dict[str, object]]:
        return (
            self._record_run_usage(worker_id, run_id, stdout),
            self._record_run_telemetry(worker_id, run_id, stdout),
        )

    def run_usage(self, worker: dict, run_id: str) -> dict[str, int]:
        path = self._run_root(str(worker["worker_id"]), str(run_id)) / "usage.json"
        if not path.is_file():
            return {}
        try:
            value = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        return {str(key): int(token_count) for key, token_count in value.items()} if isinstance(value, dict) else {}

    def run_telemetry(self, worker: dict, run_id: str) -> dict[str, object]:
        path = self._run_root(str(worker["worker_id"]), str(run_id)) / "telemetry.json"
        if not path.is_file():
            return {}
        try:
            value = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        return _safe_run_telemetry(value, run_id=str(run_id))

    def live_telemetry(
        self,
        worker: dict,
        stdout: str,
        run_id: str | None = None,
    ) -> dict[str, object]:
        telemetry_source = stdout
        scope = "console_tail"
        if run_id:
            run_stdout = self._run_root(str(worker["worker_id"]), str(run_id)) / "stdout.log"
            try:
                telemetry_source = run_stdout.read_text(errors="replace")
                scope = "full_active_run"
            except OSError:
                return {
                    "schema": "glasshive.claude-run-telemetry.v1",
                    "run_id": str(run_id),
                    "telemetry_scope": "active_run_unavailable",
                }
        telemetry = self._telemetry_from_output(telemetry_source)
        if telemetry:
            telemetry["telemetry_scope"] = scope
            if run_id:
                telemetry["run_id"] = str(run_id)
        return telemetry

    def _read_session_key(self, worker_id: str) -> str | None:
        path = self._session_meta_path(worker_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
        except Exception:
            return None
        value = str(data.get("session_key") or "").strip()
        return value or None

    def _write_session_key(self, worker_id: str, session_key: str) -> None:
        path = self._session_meta_path(worker_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"session_key": session_key}, indent=2))

    def _read_active_session(self, worker_id: str) -> dict[str, object] | None:
        path = self._active_session_meta_path(worker_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
        except Exception:
            return None
        session_name = str(data.get("session_name") or "").strip()
        if not session_name:
            return None
        return {
            "session_name": session_name,
            "run_id": str(data.get("run_id") or "").strip(),
            "stdout_path": str(data.get("stdout_path") or "").strip(),
            "stderr_path": str(data.get("stderr_path") or "").strip(),
            "exit_path": str(data.get("exit_path") or "").strip(),
            "constraint_ledger_path": str(data.get("constraint_ledger_path") or "").strip(),
            "model": str(data.get("model") or "").strip(),
            "argv_for_evidence_json": str(data.get("argv_for_evidence_json") or "").strip(),
            "started_at": str(data.get("started_at") or "").strip(),
            "process_pid": data.get("process_pid"),
            "process_identity_sha256": str(data.get("process_identity_sha256") or "").strip(),
            "heartbeat_path": str(data.get("heartbeat_path") or "").strip(),
            "timeout_seconds": data.get("timeout_seconds"),
            "instruction_redacted": bool(data.get("instruction_redacted")),
            "run_mode": str(data.get("run_mode") or "").strip(),
        }

    def _write_active_session(self, worker_id: str, payload: dict[str, object]) -> None:
        path = self._active_session_meta_path(worker_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        safe_payload = dict(payload)
        if safe_payload.get("process_pid") and not safe_payload.get("process_identity_sha256"):
            try:
                process_pid = int(safe_payload["process_pid"])
            except (TypeError, ValueError):
                process_pid = 0
            process_identity = self._process_identity_sha256(process_pid)
            if process_identity:
                safe_payload["process_identity_sha256"] = process_identity
        if "instruction" in safe_payload:
            safe_payload.pop("instruction", None)
            safe_payload["instruction_redacted"] = True
        temp_path = path.with_name(
            f"{path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}"
        )
        try:
            temp_path.write_text(json.dumps(safe_payload, indent=2))
            temp_path.chmod(0o600)
            os.replace(temp_path, path)
            path.chmod(0o600)
        finally:
            try:
                temp_path.unlink()
            except OSError:
                pass

    def _process_identity_sha256(self, pid: int) -> str | None:
        """Return a non-secret birth/command fingerprint for safe post-restart PID recovery."""
        if pid <= 1:
            return None
        try:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "lstart=", "-o", "command="],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
                env={**os.environ, "LC_ALL": "C"},
            )
        except (OSError, subprocess.SubprocessError):
            return None
        identity = result.stdout.strip() if result.returncode == 0 else ""
        if not identity:
            return None
        # Persist only the digest: a harness command line may contain private arguments.
        return hashlib.sha256(f"{pid}\n{identity}".encode("utf-8", errors="replace")).hexdigest()

    def _clear_active_session(self, worker_id: str) -> None:
        path = self._active_session_meta_path(worker_id)
        try:
            path.unlink()
        except FileNotFoundError:
            return

    def _run_root_candidates(self, worker_id: str) -> list[Path]:
        root = self._home_dir(worker_id) / ".glasshive-runs"
        if not root.exists():
            return []
        candidates = [path for path in root.iterdir() if path.is_dir() and path.name.startswith("run_")]
        return sorted(candidates, key=lambda item: item.stat().st_mtime, reverse=True)

    def _latest_run_root(self, worker_id: str) -> Path | None:
        candidates = self._run_root_candidates(worker_id)
        return candidates[0] if candidates else None

    def _session_name_for_run_id(self, run_id: str) -> str:
        return f"job-{run_id[:12]}"

    def _run_payload(self, worker_id: str, run_id: str) -> dict[str, str] | None:
        run_root = self._run_root(worker_id, run_id)
        if not run_root.exists():
            return None
        return {
            "session_name": self._session_name_for_run_id(run_id),
            "run_id": run_id,
            "stdout_path": str(run_root / "stdout.log"),
            "stderr_path": str(run_root / "stderr.log"),
            "exit_path": str(run_root / "exit_code"),
        }

    @staticmethod
    def _read_completed_exit_code(exit_path: Path) -> int | None:
        """Return a durable exit code, treating a private empty marker as unfinished."""

        try:
            raw_exit_code = exit_path.read_text().strip()
        except FileNotFoundError:
            return None
        if not raw_exit_code:
            return None
        try:
            return int(raw_exit_code)
        except ValueError:
            return 1

    def _infer_active_session(self, worker: dict, run_id: str | None = None) -> dict[str, str] | None:
        current = self._read_active_session(worker["worker_id"])
        if current and (run_id is None or current.get("run_id") == run_id):
            return current
        candidate_run_ids = [run_id] if run_id else [run_root.name for run_root in self._run_root_candidates(worker["worker_id"])]
        candidates: list[dict[str, str]] = []
        for candidate_run_id in candidate_run_ids:
            if not candidate_run_id:
                continue
            payload = self._run_payload(worker["worker_id"], candidate_run_id)
            if payload is not None:
                candidates.append(payload)
        if not candidates:
            # Session discovery must never materialize or repair a Docker sandbox merely because a
            # caller is terminating a synthetic, already-cleaned, or crash-recovered run.
            return None
        screen_sessions = set(self.sandbox.list_screen_sessions(worker["worker_id"], self.runtime_name, worker=worker))
        for payload in candidates:
            session_name = payload["session_name"]
            if session_name not in screen_sessions:
                continue
            return payload
        return None

    def _latest_completed_run_payload(self, worker_id: str, run_id: str | None = None) -> dict[str, str] | None:
        current = self._read_active_session(worker_id)
        if current and (run_id is None or current.get("run_id") == run_id):
            current_exit = Path(str(current.get("exit_path") or ""))
            current_stdout = Path(str(current.get("stdout_path") or ""))
            if self._read_completed_exit_code(current_exit) is not None or self._stdout_has_complete_response(
                current_stdout
            ):
                return current
        if run_id:
            payload = self._run_payload(worker_id, run_id)
            if payload:
                exit_path = Path(str(payload.get("exit_path") or ""))
                stdout_path = Path(str(payload.get("stdout_path") or ""))
                if self._read_completed_exit_code(exit_path) is not None or self._stdout_has_complete_response(
                    stdout_path
                ):
                    return payload
            return None
        for run_root in self._run_root_candidates(worker_id):
            exit_path = run_root / "exit_code"
            stdout_path = run_root / "stdout.log"
            if self._read_completed_exit_code(exit_path) is None and not self._stdout_has_complete_response(stdout_path):
                continue
            return {
                "session_name": self._session_name_for_run_id(run_root.name),
                "run_id": run_root.name,
                "stdout_path": str(run_root / "stdout.log"),
                "stderr_path": str(run_root / "stderr.log"),
                "exit_path": str(exit_path),
            }
        return None

    def _active_pid(self, worker_id: str) -> int | None:
        with self._process_lock:
            process = self._active_processes.get(worker_id)
            if process and process.poll() is None:
                return process.pid
            return None

    def _note_stop_reason(self, worker_id: str, reason: str, run_id: str | None = None) -> None:
        with self._process_lock:
            self._stop_reasons[(worker_id, run_id)] = reason

    def _pop_stop_reason(self, worker_id: str, run_id: str | None = None) -> str | None:
        with self._process_lock:
            if run_id is not None:
                reason = self._stop_reasons.pop((worker_id, run_id), None)
                if reason:
                    return reason
            return self._stop_reasons.pop((worker_id, None), None)

    def _register_process(self, worker_id: str, process: subprocess.Popen[str]) -> None:
        with self._process_lock:
            self._active_processes[worker_id] = process

    def _clear_process(self, worker_id: str) -> None:
        with self._process_lock:
            self._active_processes.pop(worker_id, None)

    def _stop_active_process(self, worker_id: str, *, worker: dict | None = None, run_id: str | None = None) -> None:
        active_session = self._read_active_session(worker_id)
        if active_session and run_id and active_session.get("run_id") != run_id:
            active_session = None
        if not active_session:
            active_session = self._infer_active_session(worker or {"worker_id": worker_id}, run_id=run_id)
        if not active_session and run_id:
            active_session = self._run_payload(worker_id, run_id)
        if active_session:
            try:
                self.sandbox.stop_screen_session(
                    worker_id,
                    self.runtime_name,
                    active_session["session_name"],
                    worker=worker,
                    missing_ok=True,
                )
            except Exception:
                pass
            try:
                self.sandbox.terminate_run_processes(
                    worker_id,
                    self.runtime_name,
                    active_session["run_id"],
                    worker=worker,
                )
            except Exception:
                pass
            self._clear_active_session(worker_id)
        with self._process_lock:
            process = self._active_processes.get(worker_id)
        if not process or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        except OSError:
            return

    def _runtime_info(self, worker: dict, *, pid: int | None = None) -> RuntimeInfo:
        worker_id = worker["worker_id"]
        self._ensure_dirs(worker_id)
        session_key = self._read_session_key(worker_id) or worker.get("session_key") or self._default_session_key(worker)
        if session_key:
            self._write_session_key(worker_id, session_key)
        return RuntimeInfo(
            runtime=self.runtime_name,
            model=worker.get("model") or self.resolve_model(worker.get("profile", "")),
            gateway_url="",
            gateway_port=None,
            gateway_token=None,
            session_key=session_key,
            state_dir=str(self._state_dir(worker_id)),
            workspace_dir=str(self._workspace_dir(worker_id)),
            pid=pid,
        )

    def ensure_worker_ready(self, worker: dict) -> RuntimeInfo:
        fast_sandbox = getattr(self.sandbox, "fast_sandbox_from_worker", lambda _worker: None)(worker)
        sandbox = fast_sandbox or self.sandbox.ensure_ready(worker, self.runtime_name)
        return self._runtime_info(worker, pid=sandbox.pid)

    def prepare_worker_workspace(self, worker: dict) -> RuntimeInfo:
        """Materialize private worker directories without starting container compute."""
        return self._runtime_info(worker, pid=None)

    def pause_worker(self, worker: dict) -> RuntimeInfo:
        self.sandbox.pause(worker["worker_id"])
        return self._runtime_info(worker, pid=None)

    def interrupt_worker(self, worker: dict, run_id: str | None = None) -> RuntimeInfo:
        worker_id = worker["worker_id"]
        active_run_id = str(run_id or worker.get("_active_run_id") or "").strip() or None
        if active_run_id or self._active_pid(worker_id):
            self._note_stop_reason(worker_id, "interrupted", run_id=active_run_id)
            self._stop_active_process(worker_id, worker=worker, run_id=active_run_id)
        sandbox = self.sandbox.inspect(worker["worker_id"])
        pid = sandbox.pid if sandbox and sandbox.state == "running" else None
        return self._runtime_info(worker, pid=pid)

    def terminate_worker(self, worker: dict) -> RuntimeInfo:
        worker_id = worker["worker_id"]
        active_run_id = str(worker.get("_active_run_id") or "").strip() or None
        if active_run_id or self._active_pid(worker_id):
            self._note_stop_reason(worker_id, "terminated", run_id=active_run_id)
        self._stop_active_process(worker_id, worker=worker, run_id=active_run_id)
        self.sandbox.terminate(worker_id)
        return self._runtime_info(worker, pid=None)

    def release_provider_account_binding(self, worker: dict) -> None:
        """Remove the container that carries a mission-scoped credential bind mount."""
        worker_id = str(worker.get("worker_id") or "").strip()
        if not worker_id:
            raise RuntimeErrorBase("Provider credential cleanup requires a worker id")
        self._stop_active_process(worker_id, worker=worker, run_id=str(worker.get("_active_run_id") or "") or None)
        self.sandbox.terminate(worker_id)
        raw_account_home = str(worker.get("_glasshive_provider_account_mount_host") or "").strip()
        if not raw_account_home:
            raise RuntimeErrorBase("Provider credential cleanup requires its private account home")
        self.sandbox.repair_provider_account_access(Path(raw_account_home))

    def reconcile_provider_account_binding(self, account_home: Path) -> None:
        self.sandbox.terminate_containers_mounting_provider_account(account_home)
        self.sandbox.repair_provider_account_access(account_home)

    def reconcile_worker(self, worker: dict) -> RuntimeInfo:
        sandbox = self.sandbox.inspect(worker["worker_id"])
        active_pid = self._active_pid(worker["worker_id"])
        pid = active_pid or (sandbox.pid if sandbox and sandbox.state == "running" else None)
        return self._runtime_info(worker, pid=pid)

    def worker_compute_present(self, worker: dict) -> bool:
        return self.sandbox.inspect(worker["worker_id"]) is not None

    def _log_paths(self, worker_id: str) -> tuple[Path, Path]:
        return (
            self.logs_dir / f"{worker_id}.stdout.log",
            self.logs_dir / f"{worker_id}.stderr.log",
        )

    def _build_command(self, worker: dict, instruction: str, info: RuntimeInfo) -> tuple[list[str], dict[str, str]]:
        raise NotImplementedError

    def _wait_for_exit_code(
        self,
        worker_id: str,
        exit_path: Path,
        timeout_sec: float | None,
        run_id: str | None = None,
        stdout_path: Path | None = None,
    ) -> int:
        deadline = time.monotonic() + float(timeout_sec) if timeout_sec and timeout_sec > 0 else None
        completed_seen_at: float | None = None
        early_grace_sec = self._early_completion_grace_sec()
        raw_inspect_interval = os.environ.get("WPR_RUN_WAIT_INSPECT_INTERVAL_SEC", "10").strip()
        try:
            inspect_interval_sec = max(float(raw_inspect_interval), 0.0) if raw_inspect_interval else 10.0
        except ValueError:
            inspect_interval_sec = 10.0
        next_inspect_at = 0.0
        paused = False
        while True:
            completed_exit_code = self._read_completed_exit_code(exit_path)
            if completed_exit_code is not None:
                return completed_exit_code
            if stdout_path and self._stdout_has_complete_response(stdout_path):
                now = time.monotonic()
                if completed_seen_at is None:
                    completed_seen_at = now
                elif now - completed_seen_at >= early_grace_sec:
                    exit_path.write_text("0")
                    self._stop_active_process(worker_id, run_id=run_id)
                    return 0
            else:
                completed_seen_at = None
            now = time.monotonic()
            if inspect_interval_sec == 0 or now >= next_inspect_at:
                sandbox = self.sandbox.inspect(worker_id)
                paused = bool(sandbox and sandbox.state == "paused")
                next_inspect_at = now + inspect_interval_sec
            if paused:
                time.sleep(0.25)
                continue
            time.sleep(0.25)
            if deadline is not None and time.monotonic() >= deadline:
                break
        self._note_stop_reason(worker_id, "terminated", run_id=run_id)
        self._stop_active_process(worker_id, run_id=run_id)
        raise RuntimeErrorBase(f"{self.runtime_name} timed out after {timeout_sec}s")

    def _early_completion_grace_sec(self) -> float:
        raw = (
            os.environ.get("GLASSHIVE_EARLY_COMPLETION_GRACE_SEC", "").strip()
            or os.environ.get("WPR_EARLY_COMPLETION_GRACE_SEC", "").strip()
        )
        if not raw:
            return 1.5
        try:
            parsed = float(raw)
        except ValueError:
            return 1.5
        return max(parsed, 0.0)

    def _stdout_has_complete_response(self, stdout_path: Path) -> bool:
        _ = stdout_path
        return False

    def _run_timeout_sec(self, timeout_sec: float | None = None) -> float | None:
        raw = (
            os.environ.get("GLASSHIVE_RUN_TIMEOUT_SEC", "").strip()
            or os.environ.get("GLASSHIVE_MAX_RUN_DURATION_S", "").strip()
            or os.environ.get("WPR_RUN_TIMEOUT_SEC", "").strip()
        )
        if not raw:
            return timeout_sec if timeout_sec and timeout_sec > 0 else None
        if raw.lower() in {"0", "none", "off", "false", "disabled"}:
            return None
        try:
            parsed = float(raw)
        except ValueError:
            return timeout_sec if timeout_sec and timeout_sec > 0 else None
        return parsed if parsed > 0 else None

    def _parse_output(self, worker: dict, stdout: str, stderr: str, info: RuntimeInfo) -> tuple[str | None, str]:
        raise NotImplementedError

    def _bootstrap_env_value(self, worker: dict, name: str) -> str:
        try:
            bundle = json.loads(str(worker.get("bootstrap_bundle_json") or "{}"))
        except json.JSONDecodeError:
            return ""
        if not isinstance(bundle, dict):
            return ""
        env = bundle.get("env")
        if not isinstance(env, dict):
            return ""
        return str(env.get(name) or "").strip()

    def _container_env(self, *keys: str) -> dict[str, str]:
        env: dict[str, str] = {
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "SHELL": "/bin/bash",
            "USER": "worker",
            "LOGNAME": "worker",
        }
        if os.environ.get("LANG"):
            env["LANG"] = str(os.environ["LANG"])
        for key, value in os.environ.items():
            if key.startswith("LC_"):
                env[key] = value
        for key in keys:
            value = os.environ.get(key)
            if value:
                env[key] = value
        return env

    def terminal_target(self, worker: dict) -> TerminalTarget:
        self.ensure_worker_ready(worker)
        active_session = self._infer_active_session(worker)
        session_name = str((active_session or {}).get("session_name") or "operator").strip() or "operator"
        return TerminalTarget(
            command=self.sandbox.terminal_attach_command(worker["worker_id"], self.runtime_name, session_name=session_name),
            cwd=str(self._workspace_dir(worker["worker_id"])),
            title=f"{worker['name']} live session" if active_session else f"{worker['name']} terminal",
            subtitle=f"{self.runtime_name} active run" if active_session else f"{self.runtime_name} sandbox",
        )

    def desktop_action(
        self,
        worker: dict,
        action: str,
        *,
        url: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, object]:
        session_name = self._session_name_for_run_id(run_id) if action == "terminal" and run_id else None
        launched = self.sandbox.desktop_action(
            worker["worker_id"],
            self.runtime_name,
            action,
            url=url,
            session_name=session_name,
            worker=worker,
        )
        notes = {
            "terminal": (
                "Opened the exact live worker terminal session inside the workstation desktop."
                if session_name
                else "Opened a workstation shell inside the worker sandbox."
            ),
            "files": "Opened the workspace file manager inside the worker sandbox.",
            "browser": "Opened the sandbox browser in the live workstation.",
            "focus_browser": "Tried to raise the existing browser window to the front.",
            "codex": "Opened an interactive Codex CLI window inside the worker sandbox.",
            "claude": "Opened an interactive Claude Code window inside the worker sandbox.",
            "openclaw": "Opened an interactive OpenClaw terminal surface inside the worker sandbox.",
        }
        return {
            "action": action,
            "status": "launched",
            "mode": "workstation-desktop",
            "url": launched.get("view_url"),
            "view_url": launched.get("view_url"),
            "notes": notes.get(action, "Opened the requested workstation surface."),
        }

    def describe_worker(self, worker: dict) -> dict[str, object]:
        sandbox = self.sandbox.describe(worker["worker_id"])
        return {
            "mode": "workstation-desktop" if sandbox.get("view_url") else "docker-workstation",
            "runtime": self.runtime_name,
            "workspace_dir": sandbox["workspace_dir"],
            "home_dir": sandbox["home_dir"],
            "container_name": sandbox["container_name"],
            "container_id": sandbox["container_id"],
            "sandbox_state": sandbox["state"],
            "sandbox_image": sandbox["image"],
            "view_url": sandbox.get("view_url"),
            "view_available": bool(sandbox.get("view_available") or sandbox.get("view_url")),
            "view_health": sandbox.get("view_health"),
            "novnc_port": sandbox.get("novnc_port"),
            "selenium_port": sandbox.get("selenium_port"),
            "openclaw_port": sandbox.get("openclaw_port"),
            "desktop_prime": sandbox.get("desktop_prime"),
            "pid": sandbox["pid"],
        }

    def _active_session_argv_for_evidence(self, active_session: dict[str, object] | None) -> list[str]:
        raw = str((active_session or {}).get("argv_for_evidence_json") or "").strip()
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    return [str(item or "") for item in parsed]
            except json.JSONDecodeError:
                pass
        return [self.runtime_name]

    def collect_completed_run(
        self,
        worker: dict,
        run_id: str | None = None,
        instruction: str | None = None,
    ) -> dict[str, object] | None:
        active_session = self._latest_completed_run_payload(worker["worker_id"], run_id=run_id)
        if not active_session:
            return None
        exit_path = Path(str(active_session.get("exit_path") or "").strip())
        stdout_path = Path(str(active_session.get("stdout_path") or "").strip())
        stderr_path = Path(str(active_session.get("stderr_path") or "").strip())
        exit_code = self._read_completed_exit_code(exit_path)
        if exit_code is None:
            if not self._stdout_has_complete_response(stdout_path):
                return None
            try:
                exit_path.write_text("0")
            except OSError:
                return None
            exit_code = 0
            self._stop_active_process(worker["worker_id"], worker=worker, run_id=run_id)
        stdout = stdout_path.read_text() if stdout_path.exists() else ""
        stderr = stderr_path.read_text() if stderr_path.exists() else ""
        effective_run_id = str(run_id or active_session.get("run_id") or "").strip()
        usage, telemetry = self._record_run_metrics(worker["worker_id"], effective_run_id, stdout)
        if exit_code != 0:
            classification = classify_cli_failure(
                stdout=stdout,
                stderr=stderr,
                runtime_name=self.runtime_name,
                exit_code=exit_code,
            )
            failure_fields = classification.as_store_fields()
            if self.runtime_name == "claude-code":
                # stream-json stdout is the full model/tool transcript. It remains in the
                # operator-private run files, but must never become a durable/public job error.
                detail = classification.user_message
                failure_fields["failure_diagnostic_summary"] = (
                    f"class={classification.failure_class}; exit_code={exit_code}"
                )
            else:
                detail = _redact_text((stderr or stdout or "").strip(), max_chars=2000)
            return {
                "state": "failed",
                "output_text": "",
                "error_text": _redact_text(f"{self.runtime_name} exited with code {exit_code}: {detail}"),
                "usage": usage,
                "telemetry": telemetry,
                **failure_fields,
            }
        info = self.reconcile_worker(worker)
        try:
            session_key, output = self._parse_output(worker, stdout, stderr, info)
        except RuntimeErrorBase as exc:
            return {
                "state": "failed",
                "output_text": "",
                "error_text": str(exc),
                "usage": usage,
                "telemetry": telemetry,
            }
        if session_key:
            self._write_session_key(worker["worker_id"], session_key)
        workspace = Path(str(info.workspace_dir or self._workspace_dir(worker["worker_id"])))
        warning_message = ""
        run_mode = str(active_session.get("run_mode") or "").strip().lower()
        conversation_recovery = run_mode == "conversation" or bool(
            getattr(self, "_conversation_mode_from_worker", lambda _worker: False)(worker)
        )
        if not conversation_recovery:
            try:
                _status, warning_message = _ensure_recovered_success_evidence(
                    worker=worker,
                    run_id=effective_run_id,
                    runtime_name=self.runtime_name,
                    model=str(active_session.get("model") or worker.get("model") or self.resolve_model(str(worker.get("profile") or ""))),
                    command=self._active_session_argv_for_evidence(active_session),
                    workspace=workspace,
                    stdout_text=stdout,
                    stderr_text=stderr,
                    output_text=output,
                    exit_code=exit_code,
                    active_session=active_session,
                    instruction=str(instruction or "").strip(),
                )
            except RuntimeErrorBase as exc:
                classification = classify_runtime_error(exc, runtime_name=self.runtime_name)
                return {
                    "state": "failed",
                    "output_text": "",
                    "error_text": str(exc),
                    **classification.as_store_fields(),
                }
        output_text = output.strip()
        if warning_message:
            suffix = f"\n\n{warning_message}"
            if len(output_text) + len(suffix) <= _HOST_RUN_OUTPUT_MAX_CHARS:
                output_text = f"{output_text}{suffix}"
        return {
            "state": "completed",
            "output_text": output_text,
            "error_text": "",
            "usage": usage,
            "telemetry": telemetry,
        }

    def _finalize_stop_reason(self, worker_id: str, run_id: str | None = None) -> None:
        reason = self._pop_stop_reason(worker_id, run_id=run_id)
        if reason == "paused":
            raise WorkerPausedError("Worker was paused while a run was active")
        if reason == "interrupted":
            raise WorkerInterruptedError("Worker run was interrupted by the operator")
        if reason == "terminated":
            raise WorkerTerminatedError("Worker was terminated while a run was active")

    def run_task(self, worker: dict, instruction: str, timeout_sec: float | None = None, run_id: str | None = None) -> str:
        effective_run_id = (run_id or secrets.token_hex(8)).strip()
        worker_for_run = {
            **worker,
            "_active_run_id": effective_run_id,
            "_glasshive_task_run": True,
        }
        info = self.ensure_worker_ready(worker_for_run)
        refresh_runtime_env_for_worker(self._home_dir(worker_for_run["worker_id"]), worker_for_run)
        workspace = Path(str(info.workspace_dir or self._workspace_dir(worker_for_run["worker_id"])))
        refresh_project_runtime_files_for_worker(
            self._home_dir(worker_for_run["worker_id"]),
            workspace,
            worker_for_run,
        )
        command, env = self._build_command(worker_for_run, instruction, info)
        apply_bound_provider_account_environment(
            worker_for_run,
            env,
            runtime_name=self.runtime_name,
        )
        stdin_text = self._command_stdin_text(worker_for_run, instruction, info)
        constraint_ledger, constraint_ledger_path = _write_constraint_ledger_for_run(
            worker=worker_for_run,
            instruction=instruction,
            workspace=workspace,
            run_id=effective_run_id,
        )
        stdout_path, stderr_path = self._log_paths(worker_for_run["worker_id"])
        with stderr_path.open("a") as handle:
            handle.write(f"$ {self.runtime_name} {_redacted_command_display(command)}\n")

        run_root = self._run_root(worker_for_run["worker_id"], effective_run_id)
        run_root.mkdir(parents=True, exist_ok=True)
        run_root.chmod(0o700)

        host_stdout = run_root / "stdout.log"
        host_stderr = run_root / "stderr.log"
        host_exit = run_root / "exit_code"
        host_script = run_root / "run.sh"
        host_stdin = run_root / "instruction.stdin"
        # Preserve host ownership across the bind mount so the verifier can read
        # worker output without widening access to workspace or profile data.
        # The worker-specific ACL repairs below grant the container user write
        # access while the empty exit marker remains incomplete until populated.
        for transcript_path in (host_stdout, host_stderr, host_exit):
            transcript_path.touch(exist_ok=True)
            transcript_path.chmod(0o600)

        container_run_root = self._container_run_root(effective_run_id)
        container_stdout = f"{container_run_root}/stdout.log"
        container_stderr = f"{container_run_root}/stderr.log"
        container_exit = f"{container_run_root}/exit_code"
        container_script = f"{container_run_root}/run.sh"
        container_stdin = f"{container_run_root}/instruction.stdin"
        session_name = f"job-{effective_run_id[:12]}"
        if stdin_text is not None:
            host_stdin.write_text(stdin_text)
            host_stdin.chmod(0o600)
        command_invocation = shlex.join(command)
        if stdin_text is not None:
            command_invocation = f"{command_invocation} < {shlex.quote(container_stdin)}"

        script = "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -o pipefail",
                f"mkdir -p {shlex.quote(container_run_root)}",
                (
                    "write_exit() { "
                    f"if [ ! -s {shlex.quote(container_exit)} ]; then "
                    f"printf '%s' \"$1\" > {shlex.quote(container_exit)}; "
                    "fi; "
                    "}"
                ),
                "abort_run() { write_exit \"${1:-130}\"; exit \"${1:-130}\"; }",
                "trap 'abort_run 130' HUP INT TERM",
                f"cd {shlex.quote(self.sandbox.workspace_mount)} || exit 1",
                'if [ -f "$HOME/.glasshive/runtime.env" ]; then set -a; source "$HOME/.glasshive/runtime.env"; set +a; fi',
                'GLASSHIVE_SECRET_ENV_KEYS_FILE="$HOME/.glasshive/secret-runtime.keys"',
                'GLASSHIVE_SECRET_ENV_FILE="$HOME/.glasshive/secret-runtime.env"',
                'if [ -f "$GLASSHIVE_SECRET_ENV_FILE" ]; then set -a; source "$GLASSHIVE_SECRET_ENV_FILE"; set +a; rm -f "$GLASSHIVE_SECRET_ENV_FILE"; fi',
                'if [ -f "$HOME/.wpr-openclaw/openclaw.env" ]; then set -a; source "$HOME/.wpr-openclaw/openclaw.env"; set +a; fi',
                f"export GLASSHIVE_ACTIVE_RUN_ID={shlex.quote(effective_run_id)}",
                f"export GLASSHIVE_RUN_ID={shlex.quote(effective_run_id)}",
                f"export GLASSHIVE_ACTIVE_WORKER_ID={shlex.quote(worker_for_run['worker_id'])}",
                f"{command_invocation} > >(tee -a {shlex.quote(container_stdout)}) 2> >(tee -a {shlex.quote(container_stderr)} >&2)",
                "status=$?",
                "unset " + " ".join(_RUN_SCOPED_CREDENTIAL_ENV_KEYS),
                'if [ -f "$GLASSHIVE_SECRET_ENV_KEYS_FILE" ]; then while IFS= read -r key; do [ -n "$key" ] && unset "$key"; done < "$GLASSHIVE_SECRET_ENV_KEYS_FILE"; rm -f "$GLASSHIVE_SECRET_ENV_KEYS_FILE"; fi',
                "write_exit \"$status\"",
                "printf '\\n[glasshive] run finished with exit code %s. Interactive shell remains open for takeover.\\n' \"$status\"",
                "exec bash --noprofile --norc",
            ]
        )
        host_script.write_text(script + "\n")
        host_script.chmod(0o755)
        self.sandbox.ensure_container_writable_paths(
            worker_for_run["worker_id"],
            self.runtime_name,
            [container_run_root],
            worker=worker_for_run,
        )

        with runtime_start_boundary(worker_for_run):
            self._stop_active_process(worker_for_run["worker_id"], worker=worker_for_run)
            start_result = self.sandbox.start_screen_session(
                worker_for_run["worker_id"],
                self.runtime_name,
                session_name,
                ["bash", "--noprofile", "--norc", container_script],
                env=env,
                worker=worker_for_run,
            )
            if start_result.returncode != 0:
                detail = (start_result.stderr or start_result.stdout or "").strip()[-1600:]
                raise RuntimeErrorBase(f"Failed to start attached {self.runtime_name} session: {detail}")
            process_pid = self.sandbox.screen_session_pid(
                worker_for_run["worker_id"],
                self.runtime_name,
                session_name,
                worker=worker_for_run,
            )
            notify_runtime_started(worker_for_run)

        run_timeout_sec = self._run_timeout_sec(timeout_sec)
        transcript_paths = {
            "stdout": str(host_stdout),
            "stderr": str(host_stderr),
            "exit_code": str(host_exit),
            "constraint_ledger": constraint_ledger_path,
        }
        started_at = time.time()
        started_at_iso = _utc_iso()
        heartbeat_path = _active_run_status_path(workspace, effective_run_id)
        heartbeat_stop = Event()
        heartbeat_thread: Thread | None = None
        self._write_active_session(
            worker_for_run["worker_id"],
            {
                "session_name": session_name,
                "run_id": effective_run_id,
                "stdout_path": str(host_stdout),
                "stderr_path": str(host_stderr),
                "exit_path": str(host_exit),
                "constraint_ledger_path": constraint_ledger_path,
                "model": str(info.model or ""),
                "argv_for_evidence_json": json.dumps([_redact_command_arg(part) for part in command]),
                "started_at": started_at_iso,
                "process_pid": process_pid,
                "heartbeat_path": str(heartbeat_path),
                "timeout_seconds": run_timeout_sec,
                "instruction": instruction,
            },
        )
        _write_active_run_status(
            path=heartbeat_path,
            worker=worker_for_run,
            run_id=effective_run_id,
            runtime_name=self.runtime_name,
            model=str(info.model or ""),
            state="running",
            transcript_paths=transcript_paths,
            started_at=started_at_iso,
            process_pid=process_pid,
            timeout_seconds=run_timeout_sec,
        )
        heartbeat_thread = _start_active_run_heartbeat(
            path=heartbeat_path,
            worker=worker_for_run,
            run_id=effective_run_id,
            runtime_name=self.runtime_name,
            model=str(info.model or ""),
            transcript_paths=transcript_paths,
            started_at=started_at_iso,
            process_pid=process_pid,
            timeout_seconds=run_timeout_sec,
            stop_event=heartbeat_stop,
        )
        try:
            exit_code = self._wait_for_exit_code(
                worker_for_run["worker_id"],
                host_exit,
                run_timeout_sec,
                run_id=effective_run_id,
                stdout_path=host_stdout,
            )
        except Exception as exc:
            stdout = host_stdout.read_text() if host_stdout.exists() else ""
            stderr = host_stderr.read_text() if host_stderr.exists() else ""
            self._record_run_metrics(worker_for_run["worker_id"], effective_run_id, stdout)
            stop_reason = "timeout" if "timed out" in str(exc).lower() else "error"
            evidence_path = _write_evidence_for_run(
                worker=worker_for_run,
                run_id=effective_run_id,
                runtime_name=self.runtime_name,
                model=str(info.model or ""),
                command=command,
                env=env,
                workspace=workspace,
                stdout_text=stdout,
                stderr_text=stderr,
                output_text="",
                error_text=str(exc),
                exit_code=None,
                timeout_seconds=run_timeout_sec,
                stop_reason=stop_reason,
                constraint_ledger=constraint_ledger,
                transcript_paths=transcript_paths,
                started_at=started_at,
            )
            _stop_active_run_heartbeat(heartbeat_stop, heartbeat_thread)
            _write_active_run_status(
                path=heartbeat_path,
                worker=worker_for_run,
                run_id=effective_run_id,
                runtime_name=self.runtime_name,
                model=str(info.model or ""),
                state="timeout" if stop_reason == "timeout" else "failed",
                transcript_paths=transcript_paths,
                started_at=started_at_iso,
                process_pid=process_pid,
                timeout_seconds=run_timeout_sec,
                stop_reason=stop_reason,
                evidence_path=evidence_path,
            )
            raise
        finally:
            _stop_active_run_heartbeat(heartbeat_stop, heartbeat_thread)
        self.sandbox.ensure_container_writable_paths(
            worker_for_run["worker_id"],
            self.runtime_name,
            [self.sandbox.workspace_mount, container_run_root],
            worker=worker_for_run,
        )
        stdout = host_stdout.read_text() if host_stdout.exists() else ""
        stderr = host_stderr.read_text() if host_stderr.exists() else ""

        with stdout_path.open("a") as handle:
            if stdout:
                handle.write(stdout)
                if not stdout.endswith("\n"):
                    handle.write("\n")
            stdout_path.chmod(0o600)
        with stderr_path.open("a") as handle:
            if stderr:
                handle.write(stderr)
                if not stderr.endswith("\n"):
                    handle.write("\n")
            stderr_path.chmod(0o600)
        for transcript_path in (host_stdout, host_stderr):
            if transcript_path.exists():
                transcript_path.chmod(0o600)

        self._record_run_metrics(worker_for_run["worker_id"], effective_run_id, stdout)
        try:
            self._finalize_stop_reason(worker_for_run["worker_id"], run_id=effective_run_id)
        except RuntimeErrorBase as exc:
            if isinstance(exc, WorkerPausedError):
                active_state = "paused"
            elif isinstance(exc, WorkerInterruptedError):
                active_state = "interrupted"
            elif isinstance(exc, WorkerTerminatedError):
                active_state = "terminated"
            else:
                active_state = "failed"
            evidence_path = _write_evidence_for_run(
                worker=worker_for_run,
                run_id=effective_run_id,
                runtime_name=self.runtime_name,
                model=str(info.model or ""),
                command=command,
                env=env,
                workspace=workspace,
                stdout_text=stdout,
                stderr_text=stderr,
                output_text="",
                error_text=str(exc),
                exit_code=exit_code,
                timeout_seconds=run_timeout_sec,
                stop_reason=exc.__class__.__name__,
                constraint_ledger=constraint_ledger,
                transcript_paths=transcript_paths,
                started_at=started_at,
            )
            _write_active_run_status(
                path=heartbeat_path,
                worker=worker_for_run,
                run_id=effective_run_id,
                runtime_name=self.runtime_name,
                model=str(info.model or ""),
                state=active_state,
                transcript_paths=transcript_paths,
                started_at=started_at_iso,
                process_pid=process_pid,
                timeout_seconds=run_timeout_sec,
                exit_code=exit_code,
                stop_reason=exc.__class__.__name__,
                evidence_path=evidence_path,
            )
            raise

        if exit_code != 0:
            if self.runtime_name == "claude-code":
                classification = classify_cli_failure(
                    stdout=stdout,
                    stderr=stderr,
                    runtime_name=self.runtime_name,
                    exit_code=exit_code,
                )
                detail = classification.user_message
            else:
                detail = (stderr or stdout or "").strip()[-2000:]
            error_text = f"{self.runtime_name} exited with code {exit_code}: {detail}"
            evidence_path = _write_evidence_for_run(
                worker=worker_for_run,
                run_id=effective_run_id,
                runtime_name=self.runtime_name,
                model=str(info.model or ""),
                command=command,
                env=env,
                workspace=workspace,
                stdout_text=stdout,
                stderr_text=stderr,
                output_text="",
                error_text=error_text,
                exit_code=exit_code,
                timeout_seconds=run_timeout_sec,
                stop_reason="process_exit",
                constraint_ledger=constraint_ledger,
                transcript_paths=transcript_paths,
                started_at=started_at,
            )
            _write_active_run_status(
                path=heartbeat_path,
                worker=worker_for_run,
                run_id=effective_run_id,
                runtime_name=self.runtime_name,
                model=str(info.model or ""),
                state="failed",
                transcript_paths=transcript_paths,
                started_at=started_at_iso,
                process_pid=process_pid,
                timeout_seconds=run_timeout_sec,
                exit_code=exit_code,
                stop_reason="process_exit",
                evidence_path=evidence_path,
            )
            raise RuntimeErrorBase(f"{self.runtime_name} exited with code {exit_code}: {detail}")

        session_key, output = self._parse_output(worker_for_run, stdout, stderr, info)
        if session_key:
            self._write_session_key(worker_for_run["worker_id"], session_key)
        evidence_path = _write_evidence_for_run(
            worker=worker_for_run,
            run_id=effective_run_id,
            runtime_name=self.runtime_name,
            model=str(info.model or ""),
            command=command,
            env=env,
            workspace=workspace,
            stdout_text=stdout,
            stderr_text=stderr,
            output_text=output.strip(),
            error_text="",
            exit_code=exit_code,
            timeout_seconds=run_timeout_sec,
            stop_reason="process_exit",
            constraint_ledger=constraint_ledger,
            transcript_paths=transcript_paths,
            started_at=started_at,
        )
        try:
            _status, warning_message = _require_successful_run_evidence(
                workspace=workspace,
                evidence_path=evidence_path,
                constraint_ledger_path=constraint_ledger_path,
                run_id=effective_run_id,
            )
        except RuntimeErrorBase:
            _write_active_run_status(
                path=heartbeat_path,
                worker=worker_for_run,
                run_id=effective_run_id,
                runtime_name=self.runtime_name,
                model=str(info.model or ""),
                state="failed",
                transcript_paths=transcript_paths,
                started_at=started_at_iso,
                process_pid=process_pid,
                timeout_seconds=run_timeout_sec,
                exit_code=exit_code,
                stop_reason="evidence_check_failed",
                evidence_path=evidence_path,
            )
            raise
        output_text = output.strip()
        if warning_message:
            suffix = f"\n\n{warning_message}"
            if len(output_text) + len(suffix) <= _HOST_RUN_OUTPUT_MAX_CHARS:
                output_text = f"{output_text}{suffix}"
        _write_active_run_status(
            path=heartbeat_path,
            worker=worker_for_run,
            run_id=effective_run_id,
            runtime_name=self.runtime_name,
            model=str(info.model or ""),
            state="completed",
            transcript_paths=transcript_paths,
            started_at=started_at_iso,
            process_pid=process_pid,
            timeout_seconds=run_timeout_sec,
            exit_code=exit_code,
            stop_reason="process_exit",
            evidence_path=evidence_path,
        )
        return output_text


class OpenClawWorkstationRuntime(BaseCliWorkerRuntime):
    runtime_name = "openclaw"
    worker_root_name = "openclaw_runtime"
    gateway_container_port = 18789

    def resolve_model(self, profile: str) -> str:
        general_default = os.environ.get("WPR_MODEL_OPENCLAW_GENERAL", "").strip() or self._preferred_general_model()
        desktop_default = os.environ.get("WPR_MODEL_OPENCLAW_DESKTOP", general_default)
        defaults = {
            "openclaw-general": general_default,
            "openclaw-codex": os.environ.get("WPR_MODEL_OPENCLAW_CODEX", "openai-codex/gpt-5.3-codex"),
            "openclaw-claude": os.environ.get("WPR_MODEL_OPENCLAW_CLAUDE", "anthropic/claude-sonnet-4-6"),
            "openclaw-desktop": desktop_default,
        }
        return defaults.get(profile, defaults["openclaw-general"])

    def _preferred_general_model(self) -> str:
        if self._compatible_provider_base_url():
            for env_name in ("OPENAI_MODELS", "WPR_MODEL_CODEX_CLI", "OTUC_LLM_MODEL"):
                configured = str(os.environ.get(env_name, "")).strip()
                if configured:
                    return configured.split(",", 1)[0].strip()
        if os.environ.get("OPENAI_API_KEY", "").strip():
            return "openai/gpt-5.2"
        if os.environ.get("ANTHROPIC_API_KEY", "").strip():
            return "anthropic/claude-sonnet-4-6"
        if (Path.home() / ".codex" / "auth.json").exists():
            return "openai/gpt-5.2"
        if (Path.home() / ".claude").exists() or (Path.home() / ".claude.json").exists():
            return "anthropic/claude-sonnet-4-6"
        return "openai/gpt-5.2"

    def _default_session_key(self, worker: dict) -> str | None:
        scope = os.environ.get("WPR_OPENCLAW_SESSION_SCOPE", "worker").strip().lower()
        run_id = str(worker.get("_active_run_id") or "").strip()
        if scope in {"run", "per-run", "per_run"} and run_id:
            return f"wpr-worker-{worker['worker_id']}-{run_id}"
        candidate = str(self._read_session_key(worker["worker_id"]) or worker.get("session_key") or "").strip()
        if candidate and re.fullmatch(r"[A-Za-z0-9_.-]{1,160}", candidate):
            return candidate
        return f"wpr-worker-{worker['worker_id']}"

    def _env_flag(self, name: str, default: bool = False) -> bool:
        raw = str(os.environ.get(name, "")).strip().lower()
        if not raw:
            return default
        return raw in {"1", "true", "yes", "on", "enabled"}

    def _compatible_provider_base_url(self) -> str:
        return (
            os.environ.get("WPR_OPENCLAW_BASE_URL", "").strip()
            or os.environ.get("OPENAI_BASE_URL", "").strip()
            or os.environ.get("OPENAI_API_BASE", "").strip()
            or os.environ.get("OPENAI_REVERSE_PROXY", "").strip()
            or os.environ.get("PORTKEY_BASE_URL", "").strip()
        ).rstrip("/")

    def _compatible_provider_enabled(self) -> bool:
        if self._env_flag("WPR_OPENCLAW_DISABLE_CUSTOM_PROVIDER", False):
            return False
        if self._env_flag("WPR_OPENCLAW_USE_CUSTOM_PROVIDER", False):
            return True
        return bool(self._compatible_provider_base_url())

    def _compatible_provider_id(self) -> str:
        default = "glasshive-portkey-compatible" if self._compatible_provider_env_key() == "PORTKEY_API_KEY" else "glasshive-openai-compatible"
        raw = os.environ.get("WPR_OPENCLAW_MODEL_PROVIDER", default).strip()
        provider_id = re.sub(r"[^a-zA-Z0-9_-]+", "-", raw).strip("-_").lower()
        return provider_id or default

    def _compatible_provider_env_key(self) -> str:
        configured = os.environ.get("WPR_OPENCLAW_ENV_KEY", "").strip()
        if configured:
            return configured
        if os.environ.get("PORTKEY_BASE_URL", "").strip() and not (
            os.environ.get("OPENAI_BASE_URL", "").strip()
            or os.environ.get("OPENAI_API_BASE", "").strip()
            or os.environ.get("OPENAI_REVERSE_PROXY", "").strip()
            or os.environ.get("WPR_OPENCLAW_BASE_URL", "").strip()
        ):
            return "PORTKEY_API_KEY"
        return "OPENAI_API_KEY"

    def _compatible_provider_wire_api(self) -> str:
        return os.environ.get("WPR_OPENCLAW_WIRE_API", "openai-completions").strip() or "openai-completions"

    def _compatible_provider_model_compat(self) -> dict[str, object]:
        compat: dict[str, object] = {}
        max_tokens_field = (
            os.environ.get("WPR_OPENCLAW_MAX_TOKENS_FIELD", "").strip()
            or os.environ.get("WPR_OPENCLAW_COMPAT_MAX_TOKENS_FIELD", "").strip()
        )
        if max_tokens_field in {"max_completion_tokens", "max_tokens"}:
            compat["maxTokensField"] = max_tokens_field
        elif max_tokens_field:
            logger.warning("Ignoring unsupported WPR_OPENCLAW_MAX_TOKENS_FIELD value: %s", max_tokens_field)
        return compat

    def _compatible_model_local_id(self, model: str) -> str:
        configured = os.environ.get("WPR_OPENCLAW_MODEL_ID", "").strip()
        if configured:
            return configured
        provider_id = self._compatible_provider_id()
        if model.startswith(f"{provider_id}/"):
            return model[len(provider_id) + 1 :]
        if self._compatible_provider_env_key() != "PORTKEY_API_KEY" and (
            model.startswith("openai/") or model.startswith("openai-codex/")
        ):
            return model.split("/", 1)[1]
        return model

    def _openclaw_model_for_worker(self, worker: dict) -> str:
        model = str(worker.get("model") or self.resolve_model(worker.get("profile", "openclaw-general"))).strip()
        if not model or not self._compatible_provider_enabled() or not self._compatible_provider_base_url():
            return model
        provider_id = self._compatible_provider_id()
        if model.startswith(f"{provider_id}/"):
            return model
        return f"{provider_id}/{self._compatible_model_local_id(model)}"

    def _compatible_provider_config(self, model: str) -> dict[str, object] | None:
        if not self._compatible_provider_enabled():
            return None
        base_url = self._compatible_provider_base_url()
        if not base_url:
            return None
        local_model = self._compatible_model_local_id(model)
        env_key = self._compatible_provider_env_key()
        model_entry: dict[str, object] = {
            "id": local_model,
            "name": os.environ.get("WPR_OPENCLAW_MODEL_NAME", local_model).strip() or local_model,
            "api": self._compatible_provider_wire_api(),
            "reasoning": self._env_flag("WPR_OPENCLAW_MODEL_REASONING", False),
            "input": ["text", "image"] if self._env_flag("WPR_OPENCLAW_MODEL_IMAGE_INPUT", False) else ["text"],
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
            "contextWindow": int(os.environ.get("WPR_OPENCLAW_CONTEXT_WINDOW", "128000")),
            "maxTokens": int(os.environ.get("WPR_OPENCLAW_MAX_TOKENS", "32000")),
        }
        compat = self._compatible_provider_model_compat()
        if compat:
            model_entry["compat"] = compat
        provider: dict[str, object] = {
            "baseUrl": base_url,
            "apiKey": {"source": "env", "provider": "default", "id": env_key},
            "api": self._compatible_provider_wire_api(),
            "authHeader": True,
            "timeoutSeconds": int(os.environ.get("WPR_OPENCLAW_PROVIDER_TIMEOUT_SECONDS", "300")),
            "models": [model_entry],
        }
        headers: dict[str, object] = {}
        if env_key == "PORTKEY_API_KEY":
            for env_name, header_name in (
                ("PORTKEY_VIRTUAL_KEY", "x-portkey-virtual-key"),
                ("PORTKEY_CONFIG", "x-portkey-config"),
                ("PORTKEY_PROVIDER", "x-portkey-provider"),
            ):
                if os.environ.get(env_name, "").strip():
                    headers[header_name] = {"source": "env", "provider": "default", "id": env_name}
        if headers:
            provider["headers"] = headers
        return provider

    def _openclaw_root(self, worker_id: str) -> Path:
        return self._home_dir(worker_id) / ".wpr-openclaw"

    def _container_openclaw_root(self) -> str:
        return f"{self.sandbox.home_mount}/.wpr-openclaw"

    def _container_openclaw_state_dir(self) -> str:
        return f"{self._container_openclaw_root()}/state"

    def _container_openclaw_config_path(self) -> str:
        return f"{self._container_openclaw_root()}/openclaw.json"

    def _openclaw_state_dir(self, worker_id: str) -> Path:
        return self._openclaw_root(worker_id) / "state"

    def _openclaw_config_path(self, worker_id: str) -> Path:
        return self._openclaw_root(worker_id) / "openclaw.json"

    def _openclaw_env_path(self, worker_id: str) -> Path:
        return self._openclaw_root(worker_id) / "openclaw.env"

    def _gateway_token(self, worker: dict) -> str:
        return str(worker.get("gateway_token") or "").strip() or secrets.token_urlsafe(24)

    def _ensure_openclaw_dirs(self, worker_id: str) -> None:
        self._openclaw_state_dir(worker_id).mkdir(parents=True, exist_ok=True)

    def _write_gateway_config(self, worker: dict, token: str) -> None:
        worker_id = worker["worker_id"]
        self._ensure_openclaw_dirs(worker_id)
        model = self._openclaw_model_for_worker(worker)
        config = {
            "gateway": {
                "mode": "local",
                "bind": "loopback",
                "port": self.gateway_container_port,
                "auth": {"mode": "none"},
            },
            "agents": {
                "defaults": {
                    "workspace": self.sandbox.workspace_mount,
                    "repoRoot": self.sandbox.workspace_mount,
                    "model": {"primary": model},
                    "cliBackends": {
                        "claude-cli": {"command": "claude"},
                        "codex-cli": {"command": "codex"},
                    },
                    "sandbox": {
                        "mode": "off",
                    },
                }
            },
            "session": {"dmScope": "per-channel-peer"},
            "tools": {
                "fs": {"workspaceOnly": True},
                "exec": {"applyPatch": {"workspaceOnly": True}},
                "elevated": {"enabled": False},
            },
            "plugins": {"enabled": True},
        }
        provider_config = self._compatible_provider_config(model)
        if provider_config:
            config["models"] = {
                "mode": "merge",
                "providers": {self._compatible_provider_id(): provider_config},
            }
        self._openclaw_config_path(worker_id).write_text(json.dumps(config, indent=2))
        env_lines = [
            f"export OPENCLAW_STATE_DIR={shlex.quote(self._container_openclaw_state_dir())}",
            f"export OPENCLAW_CONFIG_PATH={shlex.quote(self._container_openclaw_config_path())}",
            f"export OPENCLAW_MODEL={shlex.quote(model)}",
            f"export OPENCLAW_SESSION_ID={shlex.quote(self._default_session_key(worker) or worker_id)}",
            "export OPENCLAW_DISABLE_BONJOUR=1",
        ]
        self._openclaw_env_path(worker_id).write_text("\n".join(env_lines) + "\n")

    def _gateway_enabled(self) -> bool:
        return self._env_flag("WPR_OPENCLAW_START_GATEWAY", False)

    def _gateway_env(self, worker: dict) -> dict[str, str]:
        env = reviewed_openclaw_env(self._sandbox_env())
        env["OPENCLAW_STATE_DIR"] = self._container_openclaw_state_dir()
        env["OPENCLAW_CONFIG_PATH"] = self._container_openclaw_config_path()
        env["OPENCLAW_MODEL"] = self._openclaw_model_for_worker(worker)
        env["OPENCLAW_SESSION_ID"] = self._default_session_key(worker) or worker["worker_id"]
        return env

    def _start_openclaw_gateway(self, worker: dict, sandbox: object) -> None:
        if worker.get("_glasshive_task_run"):
            return
        if not self._gateway_enabled():
            return
        env = self._gateway_env(worker)
        result = self.sandbox.start_screen_session(
            worker["worker_id"],
            self.runtime_name,
            "openclaw-gateway",
            [
                "bash",
                "-lc",
                (
                    f"openclaw gateway --port {self.gateway_container_port} --bind loopback "
                    "--auth none --allow-unconfigured --force"
                ),
            ],
            env=env,
            worker=worker,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[-500:]
            logger.warning("OpenClaw gateway screen session failed for %s: %s", worker.get("worker_id"), detail)
            return
        container_name = str(getattr(sandbox, "container_name", "") or "")
        if not container_name:
            return
        wait_result = self.sandbox._docker_exec(
            container_name,
            [
                "bash",
                "-lc",
                (
                    f"for i in $(seq 1 20); do "
                    f"(echo >/dev/tcp/127.0.0.1/{self.gateway_container_port}) >/dev/null 2>&1 && exit 0; "
                    "sleep 0.25; "
                    "done; exit 1"
                ),
            ],
            env=env,
            cwd=self.sandbox.workspace_mount,
        )
        if wait_result.returncode != 0:
            detail = (wait_result.stderr or wait_result.stdout or "").strip()[-500:]
            logger.warning("OpenClaw gateway did not become ready for %s: %s", worker.get("worker_id"), detail)

    def _sandbox_env(self) -> dict[str, str]:
        env = reviewed_openclaw_env(self._container_env(*_PROVIDER_ENV_KEYS))
        env["HOME"] = self.sandbox.home_mount
        env["TERM"] = self.sandbox.term_value
        env["DISPLAY"] = self.sandbox.display_value
        return env

    def _runtime_info(self, worker: dict, *, pid: int | None = None) -> RuntimeInfo:
        session_key = self._default_session_key(worker)
        if session_key:
            self._write_session_key(worker["worker_id"], session_key)
        return RuntimeInfo(
            runtime=self.runtime_name,
            model=self._openclaw_model_for_worker(worker),
            gateway_url="",
            gateway_port=None,
            gateway_token=None,
            session_key=session_key,
            state_dir=str(self._openclaw_state_dir(worker["worker_id"])),
            workspace_dir=str(self._workspace_dir(worker["worker_id"])),
            pid=pid,
        )

    def _neutralize_default_openclaw_bootstrap(self, worker: dict) -> None:
        bootstrap_path = self._workspace_dir(worker["worker_id"]) / "BOOTSTRAP.md"
        task_mode_text = "\n".join(
            [
                "# GlassHive Task Mode",
                "",
                "This workspace is running an assigned GlassHive task.",
                "Follow the latest runtime-provided instruction, success criteria, and AGENTS.md.",
                "Do not start first-run identity onboarding unless the operator explicitly asks for it.",
                "For local browser verification, prefer localhost HTTP URLs over file:// URLs because some worker browser tools block local file protocols.",
                "",
            ]
        )
        if not bootstrap_path.exists():
            bootstrap_path.parent.mkdir(parents=True, exist_ok=True)
            bootstrap_path.write_text(task_mode_text)
            return
        try:
            text = bootstrap_path.read_text(errors="ignore")
        except OSError:
            return
        default_markers = (
            "# BOOTSTRAP.md - Hello, World",
            "You just woke up. Time to figure out who you are.",
            'Start with something like:\n\n> "Hey. I just came online. Who am I? Who are you?"',
        )
        if not all(marker in text for marker in default_markers):
            return
        archive_dir = bootstrap_path.parent / ".glasshive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_path = archive_dir / "archived-openclaw-default-bootstrap.md"
        if not archive_path.exists():
            archive_path.write_text(text)
        bootstrap_path.write_text(task_mode_text)

    def ensure_worker_ready(self, worker: dict) -> RuntimeInfo:
        require_reviewed_image = getattr(self.sandbox, "require_reviewed_openclaw_image", None)
        if callable(require_reviewed_image):
            require_reviewed_image()
        fast_sandbox = getattr(self.sandbox, "fast_sandbox_from_worker", lambda _worker: None)(worker)
        sandbox = fast_sandbox or self.sandbox.ensure_ready(worker, self.runtime_name)
        require_reviewed = getattr(self.sandbox, "require_reviewed_openclaw", None)
        if callable(require_reviewed):
            require_reviewed(sandbox.container_name)
        self._write_gateway_config(worker, self._gateway_token(worker))
        self._start_openclaw_gateway(worker, sandbox)
        return self._runtime_info(worker, pid=sandbox.pid)

    def pause_worker(self, worker: dict) -> RuntimeInfo:
        self.sandbox.pause(worker["worker_id"])
        return self._runtime_info(worker, pid=None)

    def interrupt_worker(self, worker: dict, run_id: str | None = None) -> RuntimeInfo:
        worker_id = worker["worker_id"]
        active_run_id = str(run_id or worker.get("_active_run_id") or "").strip() or None
        if active_run_id or self._active_pid(worker_id):
            self._note_stop_reason(worker_id, "interrupted", run_id=active_run_id)
            self._stop_active_process(worker_id, worker=worker, run_id=active_run_id)
        sandbox = self.sandbox.inspect(worker["worker_id"])
        pid = sandbox.pid if sandbox and sandbox.state == "running" else None
        return self._runtime_info(worker, pid=pid)

    def terminate_worker(self, worker: dict) -> RuntimeInfo:
        worker_id = worker["worker_id"]
        active_run_id = str(worker.get("_active_run_id") or "").strip() or None
        if active_run_id or self._active_pid(worker_id):
            self._note_stop_reason(worker_id, "terminated", run_id=active_run_id)
        self._stop_active_process(worker_id, worker=worker, run_id=active_run_id)
        self.sandbox.terminate(worker_id)
        return self._runtime_info(worker, pid=None)

    def reconcile_worker(self, worker: dict) -> RuntimeInfo:
        sandbox = self.sandbox.inspect(worker["worker_id"])
        if sandbox is None:
            return self._runtime_info(worker, pid=None)
        if sandbox.state == "paused":
            return self._runtime_info(worker, pid=None)
        return self._runtime_info(worker, pid=sandbox.pid)

    def _build_command(self, worker: dict, instruction: str, info: RuntimeInfo) -> tuple[list[str], dict[str, str]]:
        session_id = info.session_key or self._default_session_key(worker) or f"agent:main:wpr:worker:{worker['worker_id']}"
        self._neutralize_default_openclaw_bootstrap(worker)
        env = self._sandbox_env()
        env["OPENCLAW_STATE_DIR"] = self._container_openclaw_state_dir()
        env["OPENCLAW_CONFIG_PATH"] = self._container_openclaw_config_path()
        env["OPENCLAW_MODEL"] = self._openclaw_model_for_worker(worker)
        run_id = str(worker.get("_active_run_id") or "").strip()
        instruction_path = (
            f"{self._container_run_root(run_id)}/instruction.stdin"
            if run_id
            else f"{self.sandbox.home_mount}/.glasshive/latest-instruction.stdin"
        )
        command = [
            "openclaw",
            "agent",
            "--local",
            "--session-id",
            session_id,
            "-m",
            _instruction_file_pointer_message(instruction_path),
            "--json",
        ]
        return command, env

    def _command_stdin_text(self, worker: dict, instruction: str, info: RuntimeInfo) -> str | None:
        return self._instruction_with_completion_contract(instruction)

    def _openclaw_json_payload(self, raw: str) -> dict[str, object]:
        text = raw.strip()
        if not text:
            return {}
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}")
            if start < 0 or end < start:
                return {}
            try:
                value = json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return {}
        return value if isinstance(value, dict) else {}

    def _openclaw_final_text(self, data: dict[str, object]) -> str:
        direct = str(data.get("finalAssistantVisibleText") or data.get("finalAssistantRawText") or "").strip()
        if direct:
            return direct
        meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
        return str(meta.get("finalAssistantVisibleText") or meta.get("finalAssistantRawText") or "").strip()

    def _openclaw_stop_reason(self, data: dict[str, object]) -> str:
        completion = data.get("completion") if isinstance(data.get("completion"), dict) else {}
        direct = str(completion.get("stopReason") or data.get("stopReason") or "").strip()
        if direct:
            return direct
        meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
        meta_completion = meta.get("completion") if isinstance(meta.get("completion"), dict) else {}
        return str(meta_completion.get("stopReason") or meta.get("stopReason") or "").strip()

    def _stdout_has_complete_response(self, stdout_path: Path) -> bool:
        if not stdout_path.exists():
            return False
        try:
            data = self._openclaw_json_payload(stdout_path.read_text(errors="ignore"))
        except OSError:
            return False
        if not self._openclaw_final_text(data):
            return False
        return self._openclaw_stop_reason(data).lower() == "stop"

    def _parse_output(self, worker: dict, stdout: str, stderr: str, info: RuntimeInfo) -> tuple[str | None, str]:
        raw = stdout.strip()
        if not raw:
            detail = (stderr or "").strip()[-1000:]
            raise RuntimeErrorBase(f"OpenClaw returned no output{': ' + detail if detail else ''}")
        data = self._openclaw_json_payload(raw)
        if not data:
            raise RuntimeErrorBase(f"OpenClaw returned invalid JSON: {raw[-800:]}")
        output_parts: list[str] = []
        final_text = self._openclaw_final_text(data)
        if final_text:
            output_parts.append(final_text)
        else:
            for item in data.get("output", []):
                if item.get("type") == "message":
                    for content in item.get("content", []):
                        if content.get("type") == "output_text":
                            text = str(content.get("text") or "").strip()
                            if text:
                                output_parts.append(text)
                elif item.get("type") == "function_call":
                    name = str(item.get("name") or "function").strip()
                    output_parts.append(f"[Tool call: {name}]")
            for payload in data.get("payloads", []):
                text = str(payload.get("text") or "").strip()
                if text:
                    output_parts.append(text)
        output = _select_user_facing_agent_output(output_parts) or json.dumps(data, indent=2)
        session_id = str(((data.get("meta") or {}).get("agentMeta") or {}).get("sessionId") or info.session_key or "").strip() or None
        return session_id, output


class CodexCliRuntime(BaseCliWorkerRuntime):
    runtime_name = "codex-cli"
    worker_root_name = "codex_cli_runtime"
    binary_name = "codex"
    _default_compatible_provider_disabled_features: tuple[str, ...] = ()

    def resolve_model(self, profile: str) -> str:
        if profile == "codex-cli":
            return os.environ.get("WPR_MODEL_CODEX_CLI", "gpt-5.4")
        return os.environ.get("WPR_MODEL_OPENCLAW_CODEX", "openai-codex/gpt-5.3-codex")

    def _default_session_key(self, worker: dict) -> str | None:
        return self._read_session_key(worker["worker_id"]) or worker.get("session_key") or f"codex-worker:{worker['worker_id']}"

    def _command_stdin_text(self, worker: dict, instruction: str, info: RuntimeInfo) -> str | None:
        return _instruction_with_completion_contract(instruction)

    def _ensure_git_workspace(self, workspace_dir: str) -> None:
        git_dir = Path(workspace_dir) / ".git"
        if git_dir.exists():
            return
        subprocess.run(["git", "init", "-q"], cwd=workspace_dir, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(
            ["git", "config", "user.email", "worker@glasshive.local"],
            cwd=workspace_dir,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["git", "config", "user.name", "GlassHive Runtime"],
            cwd=workspace_dir,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def ensure_worker_ready(self, worker: dict) -> RuntimeInfo:
        info = super().ensure_worker_ready(worker)
        self._ensure_git_workspace(info.workspace_dir)
        return info

    def _env_flag(self, name: str, default: bool = False) -> bool:
        raw = str(os.environ.get(name, "")).strip().lower()
        if not raw:
            return default
        return raw in {"1", "true", "yes", "on", "enabled"}

    def _worker_env_flag(self, worker: dict, name: str, default: bool = False) -> bool:
        raw = (
            self._bootstrap_env_value(worker, name)
            or str(os.environ.get(name, ""))
        ).strip().lower()
        if not raw:
            return default
        return raw in {"1", "true", "yes", "on", "enabled"}

    def _codex_model_for_worker(self, worker: dict, env_name: str) -> str:
        return str(
            self._bootstrap_env_value(worker, env_name)
            or worker.get("model")
            or self.resolve_model(worker.get("profile", "codex-cli"))
            or ""
        ).strip()

    def _compatible_provider_base_url(self, worker: dict | None = None) -> str:
        if worker is not None:
            projection = validated_codex_broker_projection(worker)
            if projection is not None:
                return str(projection["base_url"])
        return (
            os.environ.get("WPR_CODEX_CLI_BASE_URL", "").strip()
            or os.environ.get("OPENAI_BASE_URL", "").strip()
            or os.environ.get("OPENAI_API_BASE", "").strip()
            or os.environ.get("OPENAI_REVERSE_PROXY", "").strip()
            or os.environ.get("PORTKEY_BASE_URL", "").strip()
        ).rstrip("/")

    def _compatible_provider_enabled(self, worker: dict | None = None) -> bool:
        if worker is not None and validated_codex_broker_projection(worker) is not None:
            return True
        if self._env_flag("WPR_CODEX_CLI_DISABLE_CUSTOM_PROVIDER", False):
            return False
        if self._env_flag("WPR_CODEX_CLI_USE_CUSTOM_PROVIDER", False):
            return True
        return bool(self._compatible_provider_base_url())

    def _compatible_provider_id(self, worker: dict | None = None) -> str:
        if worker is not None and validated_codex_broker_projection(worker) is not None:
            return "glasshive_run_broker"
        raw = os.environ.get("WPR_CODEX_CLI_MODEL_PROVIDER", "glasshive_openai_compatible").strip()
        return re.sub(r"[^A-Za-z0-9_-]+", "_", raw).strip("_") or "glasshive_openai_compatible"

    def _compatible_provider_env_key(self, worker: dict | None = None) -> str:
        if worker is not None and validated_codex_broker_projection(worker) is not None:
            return "OPENAI_API_KEY"
        configured = os.environ.get("WPR_CODEX_CLI_ENV_KEY", "").strip()
        if configured:
            return configured
        if os.environ.get("PORTKEY_BASE_URL", "").strip() and not os.environ.get("OPENAI_BASE_URL", "").strip():
            return "PORTKEY_API_KEY"
        return "OPENAI_API_KEY"

    def _compatible_provider_disabled_features(self) -> list[str]:
        raw = os.environ.get("WPR_CODEX_CLI_DISABLE_FEATURES", "").strip()
        if raw:
            return [item.strip() for item in raw.split(",") if item.strip()]
        return list(self._default_compatible_provider_disabled_features)

    def _compatible_provider_allowed_reasoning_efforts(self) -> set[str]:
        raw = os.environ.get("WPR_CODEX_CLI_ALLOWED_REASONING_EFFORTS", "").strip()
        valid = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
        if not raw:
            # Keep the generic OpenAI-compatible route conservative. The GlassHive core
            # provider uses the native host CLI path and advertises its own richer effort set;
            # compatible gateways must explicitly declare anything beyond this proven set.
            allowed = {"none", "low", "medium", "high"}
            if self._codex_xhigh_route_proven():
                allowed.add("xhigh")
            return allowed
        configured = {item.strip().lower() for item in raw.split(",") if item.strip()}
        return configured & valid or set(valid)

    def _codex_xhigh_route_proven(self) -> bool:
        return self._env_flag("WPR_CODEX_CLI_XHIGH_ROUTE_PROVEN", False) or self._env_flag(
            "GLASSHIVE_CODEX_XHIGH_ROUTE_PROVEN",
            False,
        )

    def _compatible_provider_reasoning_effort_fallback(self, allowed: set[str]) -> str:
        configured = os.environ.get("WPR_CODEX_CLI_REASONING_EFFORT_FALLBACK", "medium").strip().lower()
        if configured in allowed:
            return configured
        if "medium" in allowed:
            return "medium"
        return sorted(allowed)[0] if allowed else ""

    def _codex_reasoning_effort_for_worker(self, worker: dict) -> str:
        reasoning_effort = (
            self._bootstrap_env_value(worker, "WPR_CODEX_CLI_REASONING_EFFORT")
            or os.environ.get("WPR_CODEX_CLI_REASONING_EFFORT", "")
            or os.environ.get("WPR_CODEX_CLI_DEFAULT_REASONING_EFFORT", "")
        ).strip().lower()
        requested_effort = reasoning_effort
        allowed_efforts = self._compatible_provider_allowed_reasoning_efforts()
        fallback_reason = ""
        if reasoning_effort and reasoning_effort not in allowed_efforts:
            reasoning_effort = self._compatible_provider_reasoning_effort_fallback(allowed_efforts)
            fallback_reason = (
                "xhigh_route_not_proven"
                if requested_effort == "xhigh" and not self._codex_xhigh_route_proven()
                else "requested_effort_not_allowed"
            )
            logger.warning(
                "Codex CLI reasoning effort clamped to provider-route fallback",
                extra={
                    "worker_id": str(worker.get("worker_id") or ""),
                    "profile": str(worker.get("profile") or "codex-cli"),
                    "model": str(worker.get("model") or self.resolve_model(worker.get("profile", "codex-cli"))),
                    "requested_effort": requested_effort,
                    "effective_effort": reasoning_effort,
                    "allowed_efforts": ",".join(sorted(allowed_efforts)),
                },
            )
        if requested_effort or reasoning_effort:
            worker["_effort_projection"] = {
                "requested": requested_effort or reasoning_effort,
                "effective": reasoning_effort,
                "allowed": sorted(allowed_efforts),
                "route_proven": self._codex_xhigh_route_proven(),
                "fallback_reason": fallback_reason,
            }
        return reasoning_effort

    def effort_projection_for_worker(self, worker: dict) -> dict[str, object]:
        if str(worker.get("profile") or "").strip() != "codex-cli":
            return {}
        candidate = dict(worker)
        self._codex_reasoning_effort_for_worker(candidate)
        projection = candidate.get("_effort_projection")
        return dict(projection) if isinstance(projection, dict) else {}

    def _append_codex_reasoning_effort_config(self, command: list[str], worker: dict) -> None:
        reasoning_effort = self._codex_reasoning_effort_for_worker(worker)
        if reasoning_effort in {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}:
            command.extend(["-c", f'model_reasoning_effort="{reasoning_effort}"'])
        if reasoning_effort == "minimal":
            command.extend(["-c", 'web_search="disabled"'])
            command.extend(["--disable", "image_generation"])

    def _append_codex_user_config_policy(self, command: list[str], worker: dict) -> None:
        if self._worker_env_flag(worker, "WPR_CODEX_CLI_IGNORE_USER_CONFIG", False):
            command.append("--ignore-user-config")

    def _append_codex_compatible_provider_config(
        self,
        command: list[str],
        worker: dict,
        *,
        include_reasoning_effort: bool = True,
    ) -> None:
        projection = validated_codex_broker_projection(worker)
        if not self._compatible_provider_enabled(worker):
            return
        base_url = self._compatible_provider_base_url(worker)
        if not base_url:
            return
        provider_id = self._compatible_provider_id(worker)
        provider_name = (
            "GlassHive run broker"
            if projection is not None
            else os.environ.get("WPR_CODEX_CLI_PROVIDER_NAME", "GlassHive OpenAI-compatible").strip()
        )
        wire_api = (
            "responses"
            if projection is not None
            else os.environ.get("WPR_CODEX_CLI_WIRE_API", "responses").strip() or "responses"
        )
        verbosity = os.environ.get("WPR_CODEX_CLI_MODEL_VERBOSITY", "medium").strip()
        for feature in self._compatible_provider_disabled_features():
            command.extend(["--disable", feature])
        command.extend(
            [
                "-c",
                f'model_provider="{provider_id}"',
                "-c",
                f'model_providers.{provider_id}.name="{provider_name}"',
                "-c",
                f'model_providers.{provider_id}.base_url="{base_url}"',
                "-c",
                f'model_providers.{provider_id}.env_key="{self._compatible_provider_env_key(worker)}"',
                "-c",
                f'model_providers.{provider_id}.wire_api="{wire_api}"',
                "-c",
                f"model_providers.{provider_id}.requires_openai_auth=false",
                "-c",
                f"model_providers.{provider_id}.supports_websockets=false",
            ]
        )
        if projection is not None:
            worker_id = _toml_string(str(projection["worker_id"]))
            run_id = _toml_string(str(projection["run_id"]))
            command.extend(
                [
                    "-c",
                    (
                        f'model_providers.{provider_id}.http_headers='
                        f'{{ "X-GlassHive-Worker-Id" = {worker_id}, '
                        f'"X-GlassHive-Run-Id" = {run_id} }}'
                    ),
                ]
            )
        if verbosity:
            command.extend(["-c", f'model_verbosity="{verbosity}"'])
        if include_reasoning_effort:
            self._append_codex_reasoning_effort_config(command, worker)

    def _build_command(self, worker: dict, instruction: str, info: RuntimeInfo) -> tuple[list[str], dict[str, str]]:
        existing_session = self._read_session_key(worker["worker_id"])
        model = self._codex_model_for_worker(worker, "WPR_MODEL_CODEX_CLI")
        is_resume = bool(existing_session and not existing_session.startswith("codex-worker:"))
        dangerous_mode = os.environ.get("WPR_CODEX_DANGEROUS", "1").strip().lower() in {"1", "true", "yes", "on"}
        if is_resume:
            command = [self.binary, "exec", "resume"]
        else:
            command = [self.binary, "exec", "--json", "--skip-git-repo-check", "-C", self.sandbox.workspace_mount]
        if model:
            if is_resume:
                command.extend(["-c", f'model="{model}"'])
            else:
                command.extend(["-m", model])
        self._append_codex_user_config_policy(command, worker)
        if not worker.get("_glasshive_provider_account_bound"):
            self._append_codex_compatible_provider_config(
                command,
                worker,
                include_reasoning_effort=False,
            )
        self._append_codex_reasoning_effort_config(command, worker)
        if dangerous_mode:
            if is_resume:
                command.append("--dangerously-bypass-approvals-and-sandbox")
            else:
                command.extend(["-s", "danger-full-access", "--dangerously-bypass-approvals-and-sandbox"])
        elif not is_resume:
            command.append("--full-auto")
        if is_resume:
            command.append(existing_session)
        command.append("-")
        env = self._container_env(
            "OPENAI_API_KEY",
            "OPENAI_BASE_URL",
            "OPENAI_API_BASE",
            "OPENAI_REVERSE_PROXY",
            "PORTKEY_API_KEY",
            "PORTKEY_BASE_URL",
            "PORTKEY_VIRTUAL_KEY",
            "PORTKEY_CONFIG",
            "HTTPS_PROXY",
            "HTTP_PROXY",
            "NO_PROXY",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
        )
        projection = validated_codex_broker_projection(worker)
        if projection is not None:
            for key in _CODEX_BROKER_CONFLICTING_ENV:
                env.pop(key, None)
            env["OPENAI_API_KEY"] = str(projection["grant_token"])
        apply_bound_provider_account_environment(
            worker,
            env,
            runtime_name=self.runtime_name,
        )
        return command, env

    def _extract_plain_output(self, stdout: str, stderr: str) -> str:
        stripped = [line.strip() for line in stdout.splitlines() if line.strip()]
        if not stripped:
            return (stderr.strip() or stdout.strip())[-4000:]

        assistant_index = max((idx for idx, line in enumerate(stripped) if line.lower() == "codex"), default=-1)
        if assistant_index >= 0:
            assistant_lines: list[str] = []
            for line in stripped[assistant_index + 1 :]:
                lowered = line.lower()
                if lowered == "tokens used":
                    break
                if line.isdigit():
                    continue
                assistant_lines.append(line)
            if assistant_lines:
                deduped: list[str] = []
                for line in assistant_lines:
                    if not deduped or deduped[-1] != line:
                        deduped.append(line)
                return "\n".join(deduped)[-4000:]

        filtered: list[str] = []
        skip_prefixes = (
            "openai codex",
            "workdir:",
            "model:",
            "provider:",
            "approval:",
            "sandbox:",
            "reasoning effort:",
            "reasoning summaries:",
            "session id:",
            "mcp:",
            "mcp startup:",
        )
        for line in stripped:
            lowered = line.lower()
            if line == "--------" or line.isdigit() or lowered == "user" or lowered == "tokens used":
                continue
            if any(lowered.startswith(prefix) for prefix in skip_prefixes):
                continue
            filtered.append(line)

        if filtered:
            deduped: list[str] = []
            for line in filtered:
                if not deduped or deduped[-1] != line:
                    deduped.append(line)
            return "\n".join(deduped)[-4000:]

        return (stdout.strip() or stderr.strip())[-4000:]

    def _parse_output(self, worker: dict, stdout: str, stderr: str, info: RuntimeInfo) -> tuple[str | None, str]:
        session_key = self._read_session_key(worker["worker_id"]) or info.session_key
        output_parts: list[str] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if payload.get("type") == "thread.started":
                maybe_session = str(payload.get("thread_id") or "").strip()
                if maybe_session:
                    session_key = maybe_session
            item = payload.get("item") or {}
            if payload.get("type") == "item.completed" and item.get("type") == "agent_message":
                text = str(item.get("text") or "").strip()
                if text:
                    output_parts.append(text)
        if output_parts:
            return session_key, _select_user_facing_agent_output(output_parts)
        if getattr(self, "_conversation_mode_from_worker", lambda _worker: False)(worker):
            return session_key, "The harness completed without a user-facing response."
        fallback = self._extract_plain_output(stdout, stderr)
        selected = _select_user_facing_agent_output([fallback])
        return session_key, (selected or fallback)[-4000:]


class ClaudeCodeRuntime(BaseCliWorkerRuntime):
    runtime_name = "claude-code"
    worker_root_name = "claude_code_runtime"
    binary_name = "claude"
    _workspace_effort_support_cache: dict[tuple[str, str, str], bool] = {}

    def resolve_model(self, profile: str) -> str:
        return os.environ.get("WPR_MODEL_CLAUDE_CODE", "claude-sonnet-4-6")

    def _provider_model_for_worker(self, worker: dict) -> str:
        logical_model = worker.get("model") or self.resolve_model(
            worker.get("profile", "claude-code")
        )
        return os.environ.get("WPR_CLAUDE_CODE_PROVIDER_MODEL", "").strip() or str(
            logical_model
        )

    @staticmethod
    def _bedrock_enabled() -> bool:
        return os.environ.get("CLAUDE_CODE_USE_BEDROCK", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    def _remove_conflicting_anthropic_credentials(self, env: dict[str, str]) -> None:
        if self._bedrock_enabled():
            env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
            env.pop("ANTHROPIC_API_KEY", None)

    def _default_session_key(self, worker: dict) -> str | None:
        existing = self._read_session_key(worker["worker_id"])
        if existing:
            return existing
        if worker.get("session_key") and not str(worker.get("session_key")).startswith("worker:"):
            return str(worker.get("session_key"))
        return f"claude-worker:{worker['worker_id']}"

    def _chrome_enabled(self) -> bool:
        raw = os.environ.get("WPR_CLAUDE_CODE_ENABLE_CHROME", "").strip().lower()
        return raw not in {"0", "false", "no", "off", "disabled"}

    def _effort_for_worker(self, worker: dict) -> str:
        return (
            self._bootstrap_env_value(worker, "WPR_CLAUDE_CODE_EFFORT")
            or os.environ.get("WPR_CLAUDE_CODE_EFFORT", "")
        ).strip().lower()

    def _command_stdin_text(self, worker: dict, instruction: str, info: RuntimeInfo) -> str | None:
        return _instruction_with_completion_contract(instruction)

    def _preflight_workspace_effort_support(self, worker: dict) -> None:
        if str(worker.get("execution_mode") or "docker") != "docker":
            return
        effort = self._effort_for_worker(worker)
        if effort not in {"max", "xhigh"}:
            return
        cache_key = (str(self.sandbox.image), self.binary, effort)
        if self._workspace_effort_support_cache.get(cache_key):
            return
        try:
            self.sandbox._ensure_image()
            result = self.sandbox._docker(
                ["run", "--rm", "--entrypoint", self.binary, self.sandbox.image, "--help"],
                check=False,
                capture_output=True,
                timeout_sec=20,
            )
        except Exception as exc:
            raise RuntimeDependencyMissingError(
                f"Claude Code {effort} effort could not be preflighted in the GlassHive workspace image",
                binary=self.binary,
                runtime_name=self.runtime_name,
                profile=str(worker.get("profile") or "claude-code"),
                execution_mode="docker",
                dependency_label="Claude Code --effort support",
                recovery_hint=(
                    "Use a GlassHive workspace image with a Claude Code CLI that supports `--effort`, "
                    "or use default effort until the image is upgraded."
                ),
            ) from exc
        help_text = f"{result.stdout or ''}\n{result.stderr or ''}"
        if result.returncode != 0 or not _claude_effort_help_supports(help_text, effort):
            actual = (help_text.strip() or f"exit {result.returncode}")[-400:]
            raise RuntimeDependencyMissingError(
                f"Claude Code {effort} effort requires workspace image support for `claude --effort`",
                binary=self.binary,
                runtime_name=self.runtime_name,
                profile=str(worker.get("profile") or "claude-code"),
                execution_mode="docker",
                required_version="Claude Code CLI with --effort support",
                actual_version=actual,
                dependency_label="Claude Code --effort support",
                recovery_hint=(
                    "Upgrade the GlassHive workspace image or use default Claude effort for this run. "
                    "Do not silently project a native effort when the active image cannot prove support."
                ),
            )
        self._workspace_effort_support_cache[cache_key] = True

    def run_task(self, worker: dict, instruction: str, timeout_sec: float | None = None, run_id: str | None = None) -> str:
        self._preflight_workspace_effort_support(worker)
        return super().run_task(worker, instruction, timeout_sec=timeout_sec, run_id=run_id)

    def _build_command(self, worker: dict, instruction: str, info: RuntimeInfo) -> tuple[list[str], dict[str, str]]:
        session_key = self._read_session_key(worker["worker_id"])
        model = self._provider_model_for_worker(worker)
        permission_mode = os.environ.get("WPR_CLAUDE_CODE_PERMISSION_MODE", "bypassPermissions")
        command = [
            self.binary,
            "-p",
            "--permission-mode",
            permission_mode,
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            model,
        ]
        if self._chrome_enabled():
            command.insert(2, "--chrome")
        effort = self._effort_for_worker(worker)
        if effort in {"max", "xhigh"}:
            command.extend(["--effort", effort])
        elif effort and effort != "default":
            logger.warning(
                "Ignoring unsupported Claude Code effort",
                extra={"worker_id": str(worker.get("worker_id") or ""), "effort": effort},
            )
        if session_key and not session_key.startswith("claude-worker:"):
            command.extend(["--resume", session_key])
        env = self._container_env(
            "ANTHROPIC_API_KEY",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "CLAUDE_CODE_USE_BEDROCK",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "AWS_BEARER_TOKEN_BEDROCK",
            "AWS_REGION",
            "AWS_DEFAULT_REGION",
            "AWS_EC2_METADATA_DISABLED",
            "ANTHROPIC_MODEL",
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_CUSTOM_HEADERS",
            "ANTHROPIC_DEFAULT_SONNET_MODEL",
            "ANTHROPIC_DEFAULT_OPUS_MODEL",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL",
            "ANTHROPIC_SMALL_FAST_MODEL_AWS_REGION",
            "ANTHROPIC_BEDROCK_BASE_URL",
            "ANTHROPIC_BEDROCK_SERVICE_TIER",
            "API_TIMEOUT_MS",
            "HTTPS_PROXY",
            "HTTP_PROXY",
            "NO_PROXY",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
        )
        use_api_key = os.environ.get("WPR_CLAUDE_CODE_USE_API_KEY", "0").strip().lower() in {"1", "true", "yes", "on"}
        if not use_api_key:
            env.pop("ANTHROPIC_API_KEY", None)
        self._remove_conflicting_anthropic_credentials(env)
        return command, env

    def _parse_output(self, worker: dict, stdout: str, stderr: str, info: RuntimeInfo) -> tuple[str | None, str]:
        raw = stdout.strip()
        if not raw:
            if getattr(self, "_conversation_mode_from_worker", lambda _worker: False)(worker):
                return info.session_key, "The harness completed without a user-facing response."
            return info.session_key, (stderr.strip() or "")[-4000:]
        if getattr(self, "_conversation_mode_from_worker", lambda _worker: False)(worker):
            session_key = info.session_key
            assistant_parts: list[str] = []
            result_parts: list[str] = []
            for line in raw.splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                maybe_session = str(event.get("session_id") or "").strip()
                if maybe_session:
                    session_key = maybe_session
                if str(event.get("type") or "") == "result":
                    result = str(event.get("result") or "").strip()
                    if result:
                        result_parts.append(result)
                if str(event.get("type") or "") != "assistant":
                    continue
                message = event.get("message") if isinstance(event.get("message"), dict) else {}
                content = message.get("content") if isinstance(message.get("content"), list) else []
                text = "".join(
                    str(block.get("text") or "")
                    for block in content
                    if isinstance(block, dict) and str(block.get("type") or "") == "text"
                ).strip()
                if text:
                    assistant_parts.append(text)
            selected = _select_user_facing_agent_output(result_parts or assistant_parts)
            return session_key, selected or "The harness completed without a user-facing response."
        try:
            payload = json.loads(raw.splitlines()[-1])
        except json.JSONDecodeError:
            return info.session_key, raw[-4000:]
        session_key = str(payload.get("session_id") or info.session_key or "").strip() or None
        result = str(payload.get("result") or raw).strip()
        return session_key, _select_user_facing_agent_output([result]) or result

    def _usage_from_output(self, stdout: str) -> dict[str, int]:
        raw = stdout.strip()
        try:
            payload = json.loads(raw.splitlines()[-1]) if raw else {}
        except json.JSONDecodeError:
            payload = {}
        source = payload.get("usage") if isinstance(payload, dict) else {}
        usage = source if isinstance(source, dict) else {}
        normalized: dict[str, int] = {}
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        ):
            value = usage.get(key, 0)
            if isinstance(value, bool):
                value = 0
            try:
                parsed = int(value)
            except (TypeError, ValueError, OverflowError):
                parsed = 0
            normalized[key] = max(0, parsed)
        return normalized

    @staticmethod
    def _nonnegative_telemetry_int(value: object) -> int:
        if isinstance(value, bool):
            return 0
        try:
            return max(int(value or 0), 0)
        except (TypeError, ValueError, OverflowError):
            return 0

    @staticmethod
    def _finite_telemetry_float(value: object) -> float | None:
        if isinstance(value, bool):
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return parsed if parsed == parsed and abs(parsed) != float("inf") else None

    @staticmethod
    def _new_telemetry_state() -> dict[str, object]:
        return {
            "init": {},
            "result": {},
            "event_count": 0,
            "assistant_event_count": 0,
            "malformed_line_count": 0,
            "oversized_line_count": 0,
            "api_retry_count": 0,
            "api_retry_delay_ms": 0,
            "api_retry_statuses": set(),
            "tool_call_counts": {},
            "seen_tool_call_ids": set(),
            "seen_usage_message_ids": set(),
            "stream_input_tokens": 0,
            "stream_output_tokens": 0,
            "stream_cache_read_input_tokens": 0,
            "stream_cache_creation_input_tokens": 0,
            "first_timestamp": "",
            "last_timestamp": "",
        }

    def _consume_telemetry_line(self, state: dict[str, object], line: str) -> bool:
        if not line.strip():
            return False
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            state["malformed_line_count"] = int(state["malformed_line_count"]) + 1
            return True
        if not isinstance(value, dict):
            return True

        observed_at = datetime.now(timezone.utc).isoformat()
        if not state["first_timestamp"]:
            state["first_timestamp"] = observed_at
        state["last_timestamp"] = observed_at
        state["event_count"] = int(state["event_count"]) + 1
        event_type = value.get("type")
        subtype = value.get("subtype")
        if event_type == "system" and subtype == "init" and not state["init"]:
            state["init"] = {
                "claude_code_version": str(value.get("claude_code_version") or "").strip(),
                "model": str(value.get("model") or "").strip(),
            }
        if event_type == "result":
            usage = value.get("usage") if isinstance(value.get("usage"), dict) else {}
            result_state = str(value.get("subtype") or "").strip()
            explicit_error = value.get("is_error")
            state["result"] = {
                "subtype": result_state,
                "is_error": (
                    explicit_error
                    if isinstance(explicit_error, bool)
                    else result_state.startswith("error_")
                    or result_state in {"error", "failed", "timeout"}
                ),
                "stop_reason": str(value.get("stop_reason") or "").strip(),
                "duration_ms": value.get("duration_ms"),
                "duration_api_ms": value.get("duration_api_ms"),
                "ttft_ms": value.get("ttft_ms"),
                "ttft_stream_ms": value.get("ttft_stream_ms"),
                "time_to_request_ms": value.get("time_to_request_ms"),
                "num_turns": value.get("num_turns"),
                "total_cost_usd": value.get("total_cost_usd"),
                "service_tier": str(usage.get("service_tier") or "").strip(),
                "speed": str(usage.get("speed") or "").strip(),
            }
        if event_type == "api_retry" or subtype == "api_retry":
            state["api_retry_count"] = int(state["api_retry_count"]) + 1
            state["api_retry_delay_ms"] = int(state["api_retry_delay_ms"]) + self._nonnegative_telemetry_int(
                value.get("retry_delay_ms") or value.get("delay_ms")
            )
            status = str(
                value.get("error_status")
                or value.get("api_error_status")
                or value.get("status")
                or ""
            ).strip()
            if status:
                statuses = state["api_retry_statuses"]
                if isinstance(statuses, set):
                    statuses.add(status)
        if event_type == "assistant":
            state["assistant_event_count"] = int(state["assistant_event_count"]) + 1
            message = value.get("message")
            if isinstance(message, dict):
                message_id = str(message.get("id") or "").strip()
                seen_ids = state["seen_usage_message_ids"]
                if message_id and isinstance(seen_ids, set) and message_id not in seen_ids:
                    seen_ids.add(message_id)
                    usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
                    for source_key, state_key in (
                        ("input_tokens", "stream_input_tokens"),
                        ("output_tokens", "stream_output_tokens"),
                        ("cache_read_input_tokens", "stream_cache_read_input_tokens"),
                        ("cache_creation_input_tokens", "stream_cache_creation_input_tokens"),
                    ):
                        state[state_key] = int(state[state_key]) + self._nonnegative_telemetry_int(
                            usage.get(source_key)
                        )
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, list):
                counts = state["tool_call_counts"]
                if isinstance(counts, dict):
                    for item in content:
                        if not isinstance(item, dict) or item.get("type") != "tool_use":
                            continue
                        tool_call_id = str(item.get("id") or "").strip()
                        seen_tool_call_ids = state["seen_tool_call_ids"]
                        if (
                            tool_call_id
                            and isinstance(seen_tool_call_ids, set)
                            and tool_call_id in seen_tool_call_ids
                        ):
                            continue
                        if tool_call_id and isinstance(seen_tool_call_ids, set):
                            seen_tool_call_ids.add(tool_call_id)
                        name = str(item.get("name") or "unknown").strip() or "unknown"
                        counts[name] = int(counts.get(name, 0)) + 1
        return True

    def _telemetry_from_state(self, state: dict[str, object]) -> dict[str, object]:
        if (
            int(state["event_count"]) <= 0
            and int(state["malformed_line_count"]) <= 0
            and int(state["oversized_line_count"]) <= 0
        ):
            return {}
        init = state["init"] if isinstance(state["init"], dict) else {}
        result = state["result"] if isinstance(state["result"], dict) else {}
        tool_call_counts = (
            state["tool_call_counts"] if isinstance(state["tool_call_counts"], dict) else {}
        )
        retry_statuses = (
            sorted(str(item) for item in state["api_retry_statuses"])
            if isinstance(state["api_retry_statuses"], set)
            else []
        )
        telemetry: dict[str, object] = {
            "schema": "glasshive.claude-run-telemetry.v1",
            "claude_code_version": str(init.get("claude_code_version") or "").strip(),
            "model": str(init.get("model") or "").strip(),
            "service_tier": str(result.get("service_tier") or "").strip(),
            "speed": str(result.get("speed") or "").strip(),
            "result_state": str(result.get("subtype") or "").strip(),
            "is_error": bool(result.get("is_error")) if isinstance(result.get("is_error"), bool) else False,
            "stop_reason": str(result.get("stop_reason") or "").strip(),
            "duration_ms": self._nonnegative_telemetry_int(result.get("duration_ms")),
            "duration_api_ms": self._nonnegative_telemetry_int(result.get("duration_api_ms")),
            "ttft_ms": self._nonnegative_telemetry_int(result.get("ttft_ms")),
            "ttft_stream_ms": self._nonnegative_telemetry_int(result.get("ttft_stream_ms")),
            "time_to_request_ms": self._nonnegative_telemetry_int(result.get("time_to_request_ms")),
            "num_turns": self._nonnegative_telemetry_int(result.get("num_turns")),
            "api_retry_count": int(state["api_retry_count"]),
            "api_retry_delay_ms": int(state["api_retry_delay_ms"]),
            "api_retry_statuses": retry_statuses,
            "tool_call_count": sum(tool_call_counts.values()),
            "tool_call_counts": dict(sorted(tool_call_counts.items())),
            "event_count": int(state["event_count"]),
            "malformed_line_count": int(state["malformed_line_count"]),
            "oversized_line_count": int(state["oversized_line_count"]),
            "stream_input_tokens": int(state["stream_input_tokens"]),
            "stream_output_tokens": int(state["stream_output_tokens"]),
            "stream_cache_read_input_tokens": int(state["stream_cache_read_input_tokens"]),
            "stream_cache_creation_input_tokens": int(
                state["stream_cache_creation_input_tokens"]
            ),
            "first_timestamp": str(state["first_timestamp"]),
            "last_timestamp": str(state["last_timestamp"]),
        }
        total_cost_usd = self._finite_telemetry_float(result.get("total_cost_usd"))
        if total_cost_usd is not None:
            telemetry["total_cost_usd"] = total_cost_usd
        if telemetry["duration_ms"]:
            telemetry["duration_non_api_ms"] = max(
                int(telemetry["duration_ms"]) - int(telemetry["duration_api_ms"]),
                0,
            )
        return telemetry

    def _telemetry_from_output(self, stdout: str) -> dict[str, object]:
        state = self._new_telemetry_state()
        for line in stdout.splitlines():
            self._consume_telemetry_line(state, line)
        return self._telemetry_from_state(state)

    def live_telemetry(
        self,
        worker: dict,
        stdout: str,
        run_id: str | None = None,
    ) -> dict[str, object]:
        if not run_id:
            telemetry = self._telemetry_from_output(stdout)
            if telemetry:
                telemetry["telemetry_scope"] = "console_tail"
            return telemetry

        worker_id = str(worker["worker_id"])
        run_id = str(run_id)
        run_stdout = self._run_root(worker_id, run_id) / "stdout.log"
        key = (worker_id, run_id)
        sampled_at = datetime.now(timezone.utc)
        with self._live_telemetry_lock:
            try:
                stat = run_stdout.stat()
            except OSError:
                return {
                    "schema": "glasshive.claude-run-telemetry.v1",
                    "run_id": run_id,
                    "telemetry_scope": "active_run_unavailable",
                }
            cached = self._live_telemetry_cache.get(key)
            inode = int(getattr(stat, "st_ino", 0))
            if (
                cached is None
                or int(cached.get("inode") or -1) != inode
                or int(cached.get("offset") or 0) > stat.st_size
            ):
                cached = {
                    "inode": inode,
                    "offset": 0,
                    "partial": b"",
                    "discarding_oversized_line": False,
                    "state": self._new_telemetry_state(),
                    "sample_sequence": 0,
                    "first_observed_at": sampled_at,
                    "last_progress_at": None,
                }
                self._live_telemetry_cache[key] = cached

            offset = int(cached["offset"])
            state = cached["state"]
            pending = bytes(cached["partial"])
            discarding_oversized_line = bool(cached.get("discarding_oversized_line"))
            consumed_progress = False
            with run_stdout.open("rb") as handle:
                handle.seek(offset)
                while True:
                    appended = handle.read(1024 * 1024)
                    if not appended:
                        break
                    offset += len(appended)
                    if discarding_oversized_line:
                        newline = appended.find(b"\n")
                        if newline < 0:
                            continue
                        appended = appended[newline + 1 :]
                        discarding_oversized_line = False
                        if not appended:
                            continue
                    pieces = (pending + appended).split(b"\n")
                    pending = pieces.pop()
                    if isinstance(state, dict):
                        for raw_line in pieces:
                            if len(raw_line) > _MAX_TELEMETRY_LINE_BYTES:
                                state["malformed_line_count"] = (
                                    int(state["malformed_line_count"]) + 1
                                )
                                state["oversized_line_count"] = (
                                    int(state.get("oversized_line_count") or 0) + 1
                                )
                                consumed_progress = True
                                continue
                            consumed_progress = (
                                self._consume_telemetry_line(
                                    state,
                                    raw_line.decode("utf-8", errors="replace"),
                                )
                                or consumed_progress
                            )
                    if len(pending) > _MAX_TELEMETRY_LINE_BYTES:
                        if isinstance(state, dict):
                            state["malformed_line_count"] = int(state["malformed_line_count"]) + 1
                            state["oversized_line_count"] = (
                                int(state.get("oversized_line_count") or 0) + 1
                            )
                        pending = b""
                        discarding_oversized_line = True
                        consumed_progress = True
            cached["offset"] = offset
            cached["partial"] = pending
            cached["discarding_oversized_line"] = discarding_oversized_line
            if consumed_progress:
                cached["last_progress_at"] = sampled_at
            cached["sample_sequence"] = int(cached["sample_sequence"]) + 1

            telemetry = self._telemetry_from_state(state) if isinstance(state, dict) else {}
            try:
                current_log_bytes = max(offset, int(run_stdout.stat().st_size))
            except OSError:
                current_log_bytes = offset
            telemetry.update(
                {
                    "schema": "glasshive.claude-run-telemetry.v1",
                    "run_id": run_id,
                    "telemetry_scope": "full_active_run_incremental",
                    "sampled_at": sampled_at.isoformat().replace("+00:00", "Z"),
                    "sample_sequence": int(cached["sample_sequence"]),
                    "parsed_bytes": (
                        int(cached["offset"])
                        if cached["discarding_oversized_line"]
                        else int(cached["offset"]) - len(bytes(cached["partial"]))
                    ),
                    "log_bytes": current_log_bytes,
                    "partial_line_present": bool(cached["partial"])
                    or bool(cached["discarding_oversized_line"]),
                    "first_observed_at": cached["first_observed_at"].isoformat().replace("+00:00", "Z"),
                }
            )
            last_progress_at = cached["last_progress_at"]
            if isinstance(last_progress_at, datetime):
                telemetry["last_progress_at"] = last_progress_at.isoformat().replace("+00:00", "Z")
                telemetry["seconds_since_progress"] = max(
                    0.0,
                    round((sampled_at - last_progress_at).total_seconds(), 3),
                )
                telemetry["last_stream_activity_at"] = telemetry["last_progress_at"]
                telemetry["seconds_since_stream_activity"] = telemetry[
                    "seconds_since_progress"
                ]

            if len(self._live_telemetry_cache) > 128:
                oldest_key = min(
                    self._live_telemetry_cache,
                    key=lambda item: (
                        self._live_telemetry_cache[item].get("first_observed_at")
                        or sampled_at
                    ),
                )
                if oldest_key != key:
                    self._live_telemetry_cache.pop(oldest_key, None)
            return telemetry


_SECRET_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"/Users/[^/\s\"'`]+(?:/[^\s\"'`]*)*"), "[REDACTED_LOCAL_PATH]"),
    (re.compile(r"/(?:home|root|Volumes|private/var)/[^\s\"'`]+(?:/[^\s\"'`]*)*"), "[REDACTED_LOCAL_PATH]"),
    (re.compile(r"~/[^\s\"'`]+(?:/[^\s\"'`]*)*"), "[REDACTED_LOCAL_PATH]"),
    (re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), "[REDACTED_AWS_ACCESS_KEY]"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{12,}"), r"\1[REDACTED]"),
    (re.compile(r"(?i)((?:api[_-]?key|token|secret|password|passwd|pwd)\s*[:=]\s*)[^\s\"']{6,}"), r"\1[REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"), "sk-[REDACTED]"),
    (re.compile(r"\bghp_[A-Za-z0-9_]{8,}\b"), "ghp_[REDACTED]"),
    (re.compile(r"\bxoxb-[A-Za-z0-9-]{8,}\b"), "xoxb-[REDACTED]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"), "[REDACTED_JWT]"),
    (re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----[\s\S]*?-----END (?:[A-Z ]+ )?PRIVATE KEY-----"), "[REDACTED_PRIVATE_KEY]"),
    (re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----[\s\S]*\Z"), "[REDACTED_PRIVATE_KEY]"),
    (re.compile(r"\b[A-Za-z0-9_]{8,}:[A-Za-z0-9_./+=-]{20,}\b"), "[REDACTED_CREDENTIAL]"),
    (re.compile(r"(?i)data:image/[a-z0-9.+-]+;base64,[A-Za-z0-9+/=\s]{256,}"), "[REDACTED_IMAGE_BASE64]"),
    (re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{512,}={0,2}(?![A-Za-z0-9+/=])"), "[REDACTED_LONG_BASE64]"),
)
_FINAL_REPORT_PATTERN = re.compile(
    r"(?mi)^[ \t]*(?:#{1,6}[ \t]+|>[ \t]*)?"
    r"(?:(?:[*_]{1,3}|`{1,3})[ \t]*)?FINAL REPORT\s*:\s*"
    r"(?:(?:[*_]{1,3}|`{1,3})[ \t]*)?"
)
_HOST_RUN_OUTPUT_MAX_CHARS = 64000


def _select_user_facing_agent_output(output_parts: list[str]) -> str:
    """Prefer an explicit final report; otherwise use the latest assistant result."""
    cleaned = [part.strip() for part in output_parts if str(part or "").strip()]
    if not cleaned:
        return ""
    for part in reversed(cleaned):
        marker_matches = list(_FINAL_REPORT_PATTERN.finditer(part))
        if marker_matches:
            return part[marker_matches[-1].end() :].strip()
    return cleaned[-1]


def _redact_text(value: str, max_chars: int | None = None) -> str:
    text = value
    for pattern, replacement in _SECRET_REDACTIONS:
        text = pattern.sub(replacement, text)
    if max_chars is not None and len(text) > max_chars:
        return text[-max_chars:]
    return text


def _redact_command_arg(value: object) -> str:
    text = str(value or "")
    if "\n" in text or len(text) > 600:
        digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]
        return f"[REDACTED_LONG_ARG chars={len(text)} sha256={digest}]"
    if text.startswith("/") or text.startswith("~/"):
        return Path(text).name or "[REDACTED_PATH]"
    return _redact_text(text)


def _redacted_command_display(command: list[str]) -> str:
    return " ".join(shlex.quote(_redact_command_arg(part)) for part in command)


def _effort_projection_for_audit(worker: dict) -> dict[str, object]:
    raw = worker.get("_effort_projection")
    if not isinstance(raw, dict):
        raw = worker.get("effort_projection")
    if not isinstance(raw, dict):
        return {}
    allowed = raw.get("allowed")
    return {
        "requested": str(raw.get("requested") or ""),
        "effective": str(raw.get("effective") or ""),
        "allowed": [str(item) for item in allowed] if isinstance(allowed, list) else [],
        "route_proven": bool(raw.get("route_proven")),
        "fallback_reason": str(raw.get("fallback_reason") or ""),
    }


def _write_constraint_ledger_for_run(
    *,
    worker: dict,
    instruction: str,
    workspace: Path,
    run_id: str,
) -> tuple[dict[str, object] | None, str]:
    try:
        ledger = build_constraint_ledger(instruction=instruction, worker=worker, run_id=run_id)
        path = write_constraint_ledger(workspace, ledger, run_id)
        try:
            relative = path.relative_to(workspace).as_posix()
        except ValueError:
            relative = str(path)
        return ledger, relative
    except Exception as exc:  # pragma: no cover - evidence must not mask the real worker result
        logger.warning(
            "Failed to write GlassHive constraint ledger",
            extra={"worker_id": str(worker.get("worker_id") or ""), "run_id": run_id, "error": str(exc)},
        )
        return None, ""


def _write_evidence_for_run(
    *,
    worker: dict,
    run_id: str,
    runtime_name: str,
    model: str,
    command: list[str],
    env: dict[str, str],
    workspace: Path,
    stdout_text: str,
    stderr_text: str,
    output_text: str,
    error_text: str,
    exit_code: int | None,
    timeout_seconds: float | None,
    stop_reason: str,
    constraint_ledger: dict[str, object] | None,
    transcript_paths: dict[str, str],
    started_at: float | None = None,
    ended_at: float | None = None,
) -> str:
    try:
        evidence = build_run_evidence(
            worker=worker,
            run_id=run_id,
            runtime_name=runtime_name,
            model=model,
            command=command,
            env=env,
            workspace_dir=workspace,
            stdout_text=stdout_text,
            stderr_text=stderr_text,
            output_text=output_text,
            error_text=error_text,
            exit_code=exit_code,
            timeout_seconds=timeout_seconds,
            stop_reason=stop_reason,
            constraint_ledger=constraint_ledger,
            started_at=started_at,
            ended_at=ended_at,
            transcript_paths=transcript_paths,
        )
        path = write_run_evidence(workspace, evidence, run_id)
        try:
            return path.relative_to(workspace).as_posix()
        except ValueError:
            return str(path)
    except Exception as exc:  # pragma: no cover - evidence must not mask the real worker result
        logger.warning(
            "Failed to write GlassHive run evidence",
            extra={"worker_id": str(worker.get("worker_id") or ""), "run_id": run_id, "error": str(exc)},
        )
        return ""


def _read_workspace_json_object(workspace: Path, relative_path: str) -> dict[str, object] | None:
    if not relative_path:
        return None
    target = (workspace / relative_path).resolve()
    try:
        target.relative_to(workspace.resolve())
    except ValueError:
        return None
    if not target.is_file():
        return None
    try:
        payload = json.loads(target.read_text())
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _read_valid_constraint_ledger(
    workspace: Path,
    relative_path: str,
    *,
    run_id: str = "",
) -> dict[str, object]:
    payload = _read_workspace_json_object(workspace, relative_path)
    if payload is None:
        raise RuntimeErrorBase("GlassHive evidence check failed: constraint ledger was not readable")
    if str(payload.get("schema") or "") != "glasshive.run.constraint-ledger.v1":
        raise RuntimeErrorBase("GlassHive evidence check failed: constraint ledger is missing its canonical schema")
    if run_id and str(payload.get("run_id") or "") != run_id:
        raise RuntimeErrorBase("GlassHive evidence check failed: constraint ledger belongs to a different run")
    if not isinstance(payload.get("worker"), dict):
        raise RuntimeErrorBase("GlassHive evidence check failed: constraint ledger is missing worker metadata")
    if "original_request" not in payload:
        raise RuntimeErrorBase("GlassHive evidence check failed: constraint ledger is missing the original request")
    if not isinstance(payload.get("constraints"), dict) or not isinstance(payload.get("outputs"), dict):
        raise RuntimeErrorBase("GlassHive evidence check failed: constraint ledger is missing constraint/output sections")
    return payload


def _read_run_evidence_result(workspace: Path, evidence_path: str) -> dict[str, object]:
    payload = _read_workspace_json_object(workspace, evidence_path)
    if not payload:
        return {}
    result = payload.get("evidence_result") if isinstance(payload, dict) else {}
    return result if isinstance(result, dict) else {}


def _require_successful_run_evidence(
    *,
    workspace: Path,
    evidence_path: str,
    constraint_ledger_path: str,
    run_id: str = "",
) -> tuple[str, str]:
    """Fail a successful worker process when its completion evidence is not usable."""
    if not constraint_ledger_path:
        raise RuntimeErrorBase("GlassHive evidence check failed: constraint ledger was not written")
    if not evidence_path:
        raise RuntimeErrorBase("GlassHive evidence check failed: run evidence was not written")
    _read_valid_constraint_ledger(workspace, constraint_ledger_path, run_id=run_id)
    result = _read_run_evidence_result(workspace, evidence_path)
    if not result:
        raise RuntimeErrorBase("GlassHive evidence check failed: run evidence was not readable")
    status = str(result.get("status") or "").strip().lower()
    if status == "fail":
        raise RuntimeErrorBase(_evidence_result_message(result, failed=True))
    if status == "warn":
        return status, _evidence_result_message(result, failed=False)
    if status != "pass":
        raise RuntimeErrorBase("GlassHive evidence check failed: run evidence result is missing or invalid")
    return status, ""


def _session_timeout_seconds(active_session: dict[str, object] | None) -> float | None:
    try:
        value = (active_session or {}).get("timeout_seconds")
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _session_started_at_epoch(active_session: dict[str, object] | None) -> float | None:
    raw = str((active_session or {}).get("started_at") or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _transcript_paths_from_active_session(active_session: dict[str, object] | None) -> dict[str, str]:
    return {
        "stdout": str((active_session or {}).get("stdout_path") or ""),
        "stderr": str((active_session or {}).get("stderr_path") or ""),
        "exit_code": str((active_session or {}).get("exit_path") or ""),
        "constraint_ledger": str((active_session or {}).get("constraint_ledger_path") or ""),
    }


def _default_constraint_ledger_path(run_id: str) -> str:
    return f"glasshive-run/runs/{run_id}/constraint-ledger.json" if run_id else "glasshive-run/constraint-ledger.json"


def _default_evidence_path(run_id: str) -> str:
    return f"glasshive-run/runs/{run_id}/evidence.json" if run_id else "glasshive-run/evidence.json"


def _ensure_recovered_success_evidence(
    *,
    worker: dict,
    run_id: str,
    runtime_name: str,
    model: str,
    command: list[str],
    workspace: Path,
    stdout_text: str,
    stderr_text: str,
    output_text: str,
    exit_code: int | None,
    active_session: dict[str, object] | None,
    instruction: str,
) -> tuple[str, str]:
    if not run_id:
        raise RuntimeErrorBase("GlassHive evidence check failed: recovered run id was not available")
    constraint_ledger_path = str((active_session or {}).get("constraint_ledger_path") or "").strip()
    if not constraint_ledger_path:
        constraint_ledger_path = _default_constraint_ledger_path(run_id)
    try:
        constraint_ledger = _read_valid_constraint_ledger(workspace, constraint_ledger_path, run_id=run_id)
    except RuntimeErrorBase as ledger_exc:
        if not instruction.strip():
            raise ledger_exc
        constraint_ledger, constraint_ledger_path = _write_constraint_ledger_for_run(
            worker=worker,
            instruction=instruction,
            workspace=workspace,
            run_id=run_id,
        )
        if constraint_ledger is None or not constraint_ledger_path:
            raise RuntimeErrorBase("GlassHive evidence check failed: constraint ledger was not written")
    evidence_path = _default_evidence_path(run_id)
    if not _read_run_evidence_result(workspace, evidence_path):
        transcript_paths = _transcript_paths_from_active_session(active_session)
        if constraint_ledger_path:
            transcript_paths["constraint_ledger"] = constraint_ledger_path
        evidence_path = _write_evidence_for_run(
            worker=worker,
            run_id=run_id,
            runtime_name=runtime_name,
            model=model,
            command=command,
            env={},
            workspace=workspace,
            stdout_text=stdout_text,
            stderr_text=stderr_text,
            output_text=output_text,
            error_text="",
            exit_code=exit_code,
            timeout_seconds=_session_timeout_seconds(active_session),
            stop_reason="process_exit",
            constraint_ledger=constraint_ledger,
            transcript_paths=transcript_paths,
            started_at=_session_started_at_epoch(active_session),
        )
        if not evidence_path:
            raise RuntimeErrorBase("GlassHive evidence check failed: run evidence was not written")
    return _require_successful_run_evidence(
        workspace=workspace,
        evidence_path=evidence_path,
        constraint_ledger_path=constraint_ledger_path,
        run_id=run_id,
    )


def _evidence_reason_preview(reason: object) -> str:
    if isinstance(reason, dict):
        label = str(reason.get("reason") or "").strip()
        extras: list[str] = []
        issues = reason.get("issues")
        if isinstance(issues, list):
            for issue in issues[:3]:
                if isinstance(issue, dict):
                    issue_reason = str(issue.get("reason") or "").strip()
                    if issue_reason:
                        extras.append(issue_reason)
                    missing = issue.get("missing_required_artifact_types")
                    if isinstance(missing, list) and missing:
                        extras.append("missing " + ", ".join(str(item) for item in missing[:5]))
                elif issue:
                    extras.append(str(issue))
        artifacts = reason.get("artifacts")
        if isinstance(artifacts, list):
            paths = [str(item.get("path") or "") for item in artifacts if isinstance(item, dict)]
            if paths:
                extras.append("invalid " + ", ".join(paths[:5]))
        if extras:
            return f"{label}: {'; '.join(extras)}"
        return label
    return str(reason or "").strip()


def _evidence_result_message(result: dict[str, object], *, failed: bool) -> str:
    key = "failure_reasons" if failed else "warning_reasons"
    reasons = result.get(key) if isinstance(result.get(key), list) else []
    previews = [_evidence_reason_preview(item) for item in reasons[:5]]
    previews = [item for item in previews if item]
    prefix = "GlassHive evidence check failed" if failed else "GlassHive evidence check warning"
    detail = "; ".join(previews) if previews else "see glasshive-run/evidence.json"
    return f"{prefix}: {detail}"


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _active_run_status_path(workspace: Path, run_id: str) -> Path:
    return workspace / "glasshive-run" / "runs" / run_id / "active-run.json"


def _active_run_workspace_from_status_path(path: Path) -> Path:
    try:
        return path.parents[3]
    except IndexError:
        return path.parent


def _active_run_resolve_transcript_path(raw_path: str, *, workspace: Path) -> Path:
    candidate = Path(str(raw_path or ""))
    if candidate.is_absolute():
        return candidate
    return workspace / candidate


def _active_run_tail_hash(path: Path, *, tail_bytes: int = 4096) -> str | None:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > tail_bytes:
                handle.seek(size - tail_bytes)
            return hashlib.sha256(handle.read(tail_bytes)).hexdigest()
    except OSError:
        return None


def _active_run_heartbeat_sequence(path: Path) -> int:
    try:
        existing = json.loads(path.read_text())
    except Exception:
        return 1
    try:
        return int(existing.get("heartbeat_sequence") or 0) + 1
    except Exception:
        return 1


def _active_run_transcript_progress(path: Path, transcript_paths: dict[str, str]) -> dict[str, object]:
    workspace = _active_run_workspace_from_status_path(path)
    now = time.time()
    files: dict[str, object] = {}
    latest_output_mtime: float | None = None
    for key, raw_path in sorted((transcript_paths or {}).items()):
        if key not in {"stdout", "stderr", "exit_code", "constraint_ledger"}:
            continue
        resolved = _active_run_resolve_transcript_path(str(raw_path or ""), workspace=workspace)
        try:
            stat = resolved.stat()
        except OSError:
            files[key] = {"exists": False, "bytes": 0}
            continue
        if key in {"stdout", "stderr"} and stat.st_size > 0:
            latest_output_mtime = max(latest_output_mtime or stat.st_mtime, stat.st_mtime)
        files[key] = {
            "exists": True,
            "bytes": stat.st_size,
            "mtime": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "age_seconds": round(max(0.0, now - stat.st_mtime), 3),
            "tail_sha256": _active_run_tail_hash(resolved),
        }
    latest_output_at = (
        datetime.fromtimestamp(latest_output_mtime, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        if latest_output_mtime is not None
        else None
    )
    return {
        "files": files,
        "last_output_at": latest_output_at,
        "quiet_seconds": round(max(0.0, now - latest_output_mtime), 3) if latest_output_mtime is not None else None,
    }


def _write_active_run_status(
    *,
    path: Path,
    worker: dict,
    run_id: str,
    runtime_name: str,
    model: str,
    state: str,
    transcript_paths: dict[str, str],
    started_at: str,
    process_pid: int | None,
    timeout_seconds: float | None,
    exit_code: int | None = None,
    stop_reason: str = "",
    evidence_path: str = "",
) -> None:
    try:
        with _ACTIVE_RUN_STATUS_LOCK:
            if state == "running":
                try:
                    previous = json.loads(path.read_text())
                except (OSError, json.JSONDecodeError):
                    previous = {}
                if (
                    isinstance(previous, dict)
                    and str(previous.get("run_id") or "") == run_id
                    and str(previous.get("state") or "") in _ACTIVE_RUN_TERMINAL_STATES
                ):
                    return
            payload = {
                "schema": "glasshive.active_run.v1",
                "run_id": run_id,
                "state": state,
                "started_at": started_at,
                "last_heartbeat_at": _utc_iso(),
                "heartbeat_sequence": _active_run_heartbeat_sequence(path),
                "worker": {
                    "worker_id": str(worker.get("worker_id") or ""),
                    "profile": str(worker.get("profile") or ""),
                    "execution_mode": str(worker.get("execution_mode") or ""),
                    "runtime": str(worker.get("runtime") or ""),
                },
                "runtime": runtime_name,
                "model": model,
                "process_pid": process_pid,
                "timeout_seconds": timeout_seconds,
                "exit_code": exit_code,
                "stop_reason": stop_reason,
                "transcript_paths": transcript_paths,
                "transcript_progress": _active_run_transcript_progress(path, transcript_paths),
                "evidence_path": evidence_path,
            }
            if not _write_private_json(path, payload):
                raise OSError("active run status write failed")
    except Exception as exc:  # pragma: no cover - heartbeat must not mask the real worker result
        logger.warning(
            "Failed to write GlassHive active run status",
            extra={"worker_id": str(worker.get("worker_id") or ""), "run_id": run_id, "error": str(exc)},
        )


def _start_active_run_heartbeat(
    *,
    path: Path,
    worker: dict,
    run_id: str,
    runtime_name: str,
    model: str,
    transcript_paths: dict[str, str],
    started_at: str,
    process_pid: int | None,
    timeout_seconds: float | None,
    stop_event: Event,
    interval_sec: float = 5.0,
) -> Thread:
    def beat() -> None:
        while not stop_event.wait(interval_sec):
            _write_active_run_status(
                path=path,
                worker=worker,
                run_id=run_id,
                runtime_name=runtime_name,
                model=model,
                state="running",
                transcript_paths=transcript_paths,
                started_at=started_at,
                process_pid=process_pid,
                timeout_seconds=timeout_seconds,
            )

    thread = Thread(target=beat, name=f"glasshive-run-heartbeat-{run_id[:12]}", daemon=True)
    thread.start()
    return thread


def _stop_active_run_heartbeat(stop_event: Event, thread: Thread | None) -> None:
    stop_event.set()
    if thread:
        thread.join()


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip().lower()).strip("-._")
    return slug[:64] or "project"


class HostNativeCliMixin:
    execution_mode = "host"
    worker_root_name = "host_cli_runtime"

    def _host_active_slots(self) -> dict[str, str]:
        slots = self.__dict__.get("_viventium_host_active_slots")
        if not isinstance(slots, dict):
            slots = {}
            self.__dict__["_viventium_host_active_slots"] = slots
        return slots

    def _durable_host_process_command(
        self,
        command: list[str],
        *,
        run_root: Path,
        exit_path: Path,
        timeout_sec: float | None = None,
        stdin_path: Path | None = None,
    ) -> list[str]:
        """Wrap a native CLI so the surviving process owns its terminal marker."""
        wrapper_path = run_root / "native-process-supervisor.py"
        wrapper_path.write_text(
            """#!/usr/bin/env python3
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path


status_path = Path(sys.argv[1])
start_path = Path(sys.argv[2])
ready_path = Path(sys.argv[3])
timeout_sec = float(sys.argv[4]) if sys.argv[4] else None
stdin_path = Path(sys.argv[5]) if sys.argv[5] else None
command = sys.argv[6:]
child: subprocess.Popen[bytes] | None = None
requested_signal = 0


class SupervisorSignal(Exception):
    pass


def write_exit(exit_code: int) -> None:
    temp_path = status_path.with_name(f"{status_path.name}.tmp.{os.getpid()}")
    try:
        temp_path.write_text(f"{exit_code}\\n")
        temp_path.chmod(0o600)
        os.replace(temp_path, status_path)
        status_path.chmod(0o600)
    finally:
        try:
            temp_path.unlink()
        except OSError:
            pass


def stop_child(signum: int) -> None:
    if child is None or child.poll() is not None:
        return
    try:
        os.killpg(child.pid, signum)
    except ProcessLookupError:
        return


def await_child_stop() -> None:
    if child is None or child.poll() is not None:
        return
    try:
        child.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        child.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


def handle_signal(signum: int, _frame: object) -> None:
    global requested_signal
    requested_signal = signum
    raise SupervisorSignal


for handled_signal in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
    signal.signal(handled_signal, handle_signal)

exit_code = 70
try:
    ready_temp_path = ready_path.with_name(f"{ready_path.name}.tmp.{os.getpid()}")
    ready_temp_path.write_text(f"{os.getpid()}\\n")
    ready_temp_path.chmod(0o600)
    os.replace(ready_temp_path, ready_path)
    ready_path.chmod(0o600)
    handshake_deadline = time.monotonic() + 30
    while not start_path.exists():
        if time.monotonic() >= handshake_deadline:
            sys.stderr.write("GlassHive host-native launch handshake timed out before authoring began.\\n")
            exit_code = 75
            break
        time.sleep(0.05)
    else:
        stdin_handle = stdin_path.open("rb") if stdin_path is not None else open(os.devnull, "rb")
        try:
            child = subprocess.Popen(command, stdin=stdin_handle, process_group=0)
            try:
                exit_code = child.wait(timeout=timeout_sec)
            except subprocess.TimeoutExpired:
                sys.stderr.write(
                    f"GlassHive host-native process timed out after {timeout_sec:g}s.\\n"
                )
                sys.stderr.flush()
                stop_child(signal.SIGTERM)
                await_child_stop()
                exit_code = 124
        finally:
            stdin_handle.close()
except SupervisorSignal:
    for handled in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        signal.signal(handled, signal.SIG_IGN)
    stop_child(requested_signal)
    await_child_stop()
    exit_code = {
        signal.SIGHUP: 129,
        signal.SIGINT: 130,
        signal.SIGTERM: 143,
    }.get(requested_signal, 128 + requested_signal)
finally:
    write_exit(exit_code)

raise SystemExit(exit_code)
"""
        )
        wrapper_path.chmod(0o700)
        return [
            sys.executable,
            str(wrapper_path),
            str(exit_path),
            str(run_root / "start-permit"),
            str(run_root / "supervisor-ready"),
            f"{timeout_sec:g}" if timeout_sec is not None else "",
            str(stdin_path) if stdin_path is not None else "",
            *command,
        ]

    def _wait_for_durable_host_supervisor(
        self,
        process: subprocess.Popen[str],
        *,
        run_root: Path,
    ) -> None:
        def abort_unready(message: str) -> None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except OSError:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    pass
            raise RuntimeErrorBase(message)

        ready_path = run_root / "supervisor-ready"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                ready_pid = int(ready_path.read_text().strip())
            except (OSError, ValueError):
                ready_pid = 0
            if ready_pid == process.pid:
                return
            return_code = process.poll()
            if return_code is not None:
                raise RuntimeErrorBase(
                    f"Host-native process supervisor exited before launch handshake (code {return_code})"
                )
            time.sleep(0.01)
        abort_unready("Host-native process supervisor did not become ready")

    def _release_durable_host_process(self, active_session: dict[str, object]) -> None:
        raw_exit_path = str(active_session.get("exit_path") or "").strip()
        if not raw_exit_path:
            raise RuntimeErrorBase("Host-native process metadata is missing its terminal path")
        exit_path = Path(raw_exit_path)
        if exit_path.exists():
            return
        start_path = exit_path.with_name("start-permit")
        if start_path.exists():
            return
        temp_path = start_path.with_name(
            f"{start_path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}"
        )
        try:
            temp_path.write_text("start\n")
            temp_path.chmod(0o600)
            os.replace(temp_path, start_path)
            start_path.chmod(0o600)
        finally:
            try:
                temp_path.unlink()
            except OSError:
                pass

    def _read_durable_host_exit_code(self, exit_path: Path) -> int | None:
        try:
            return int(exit_path.read_text().strip())
        except (OSError, ValueError):
            return None

    def _write_durable_host_exit_code(self, exit_path: Path, exit_code: int) -> None:
        """Atomically backfill a marker when the API process outlives the wrapper write."""
        temp_path = exit_path.with_name(
            f"{exit_path.name}.tmp.api-{os.getpid()}-{secrets.token_hex(4)}"
        )
        try:
            temp_path.write_text(f"{exit_code}\n")
            temp_path.chmod(0o600)
            os.replace(temp_path, exit_path)
            exit_path.chmod(0o600)
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass

    def _final_host_exit_code(self, exit_path: Path, process_exit_code: int) -> int:
        durable_exit_code = self._read_durable_host_exit_code(exit_path)
        if durable_exit_code is not None:
            return durable_exit_code
        self._write_durable_host_exit_code(exit_path, process_exit_code)
        return process_exit_code

    def _host_worker_lanes(self) -> dict[str, str]:
        lanes = self.__dict__.get("_viventium_host_worker_lanes")
        if not isinstance(lanes, dict):
            lanes = {}
            self.__dict__["_viventium_host_worker_lanes"] = lanes
        return lanes

    def _host_capacity_lane(self, worker: dict | None) -> str:
        return "conversation" if self._conversation_mode_from_worker(worker) else "mission"

    def _instruction_with_completion_contract(self, instruction: str) -> str:
        return _instruction_with_completion_contract(instruction)

    def _command_stdin_text(self, worker: dict, instruction: str, info: RuntimeInfo) -> str | None:
        _ = info
        if self._conversation_mode_from_worker(worker):
            return str(instruction or "").strip()
        return self._instruction_with_completion_contract(instruction)

    def _run_mode_from_worker(self, worker: dict | None) -> str:
        if not isinstance(worker, dict):
            return "mission"
        bundle = self._bootstrap_bundle_for_worker(worker)
        return str(bundle.get("run_mode") or "mission").strip().lower()

    def _conversation_mode_from_worker(self, worker: dict | None) -> bool:
        return self._run_mode_from_worker(worker) == "conversation"

    def _conversation_evidence_workspace(self, worker: dict, run_id: str) -> Path:
        path = self._run_root(str(worker["worker_id"]), run_id) / "private-evidence"
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o700)
        return path

    def _conversation_workspace_side_effect_state(self, workspace: Path) -> dict[str, bool]:
        _ = workspace
        return {}

    def _cleanup_conversation_workspace_side_effects(
        self,
        workspace: Path,
        state: dict[str, bool],
    ) -> None:
        _ = (workspace, state)

    def _agent_type(self) -> str:
        if self.runtime_name == "codex-cli":
            return "codex"
        if self.runtime_name == "claude-code":
            return "claude"
        return "openclaw"

    def _state_dir(self, worker_id: str) -> Path:
        return self.workers_dir / worker_id / "state"

    def _home_dir(self, worker_id: str) -> Path:
        return self.workers_dir / worker_id / "home"

    def _workspace_dir(self, worker_id: str) -> Path:
        return self.workers_dir / worker_id / "workspace"

    def _worker_root(self, worker_id: str) -> Path:
        return self.workers_dir / worker_id

    def _container_run_root(self, run_id: str) -> str:
        return str(self._home_dir("unknown") / ".glasshive-runs" / run_id)

    def _host_workspace_root(self, worker: dict) -> Path:
        raw = (
            str(worker.get("workspace_root") or "").strip()
            or os.environ.get("WPR_HOST_WORKSPACE_ROOT", "").strip()
            or "~/viventium"
        )
        return Path(raw).expanduser()

    def _host_workspace_dir(self, worker: dict) -> Path:
        existing = str(worker.get("workspace_dir") or "").strip()
        if existing:
            return Path(existing).expanduser()
        root = self._host_workspace_root(worker)
        if self._conversation_mode_from_worker(worker):
            return root
        date_prefix = datetime.now().strftime("%Y-%m-%d")
        alias = str(worker.get("alias") or worker.get("name") or worker.get("worker_id") or "project")
        slug = _safe_slug(alias)
        return root / self._agent_type() / f"{date_prefix}-{slug}"

    def _host_project_definition(self, worker: dict) -> str:
        bundle = self._bootstrap_bundle_for_worker(worker)
        candidate = (
            bundle.get("project_definition")
            or bundle.get("task")
            or bundle.get("goal")
            or bundle.get("system_instructions")
            or ""
        )
        body = str(candidate or "").strip()
        if body:
            return body
        return (
            f"# {worker.get('name') or 'GlassHive host worker'}\n\n"
            f"- Worker: {worker.get('worker_id')}\n"
            f"- Agent type: {self._agent_type()}\n"
            f"- Role: {worker.get('role') or 'worker'}\n"
        )

    def _host_harness_prompt(self, worker: dict) -> str:
        bundle = self._bootstrap_bundle_for_worker(worker)
        extra = str(bundle.get("system_instructions") or "").strip()
        prompt = HOST_NATIVE_HARNESS_PROMPT.rstrip()
        if extra:
            prompt += "\n\nHost-provided instructions:\n" + extra
        return prompt.strip() + "\n"

    def _bootstrap_bundle_for_worker(self, worker: dict) -> dict[str, object]:
        raw = worker.get("bootstrap_bundle_json")
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
                return parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                return {}
        raw_bundle = worker.get("bootstrap_bundle")
        return raw_bundle if isinstance(raw_bundle, dict) else {}

    def _write_workspace_file(self, workspace: Path, relative_path: str, content: str, *, overwrite: bool = True) -> None:
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeErrorBase(f"Unsafe bootstrap path: {relative_path}")
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if overwrite or not target.exists():
            target.write_text(content)

    def _write_workspace_bytes(self, workspace: Path, relative_path: str, content: bytes, *, overwrite: bool = True) -> None:
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeErrorBase(f"Unsafe bootstrap path: {relative_path}")
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if overwrite or not target.exists():
            target.write_bytes(content)

    def _copy_workspace_source_file(self, workspace: Path, relative_path: str, source: Path) -> None:
        relative = Path(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise RuntimeErrorBase(f"Unsafe bootstrap path: {relative_path}")
        source = resolve_bootstrap_source_path(source)
        if not source.exists():
            raise RuntimeErrorBase(f"Bootstrap source file not found: {source}")
        target = workspace / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(source, target, dirs_exist_ok=True)
        else:
            shutil.copy2(source, target)

    def _bootstrap_file_allows_empty(self, item: dict[str, object]) -> bool:
        value = item.get("allow_empty")
        if isinstance(value, bool):
            return value
        return str(value or "").strip().lower() in {"1", "true", "yes", "on"}

    def _require_non_empty_bootstrap_file(self, path: str, size: int, item: dict[str, object]) -> None:
        if size <= 0 and not self._bootstrap_file_allows_empty(item):
            raise RuntimeErrorBase(f"Bootstrap file {path} is empty; set allow_empty=true to materialize an empty file")

    def _source_path_from_bootstrap_file(self, item: dict[str, object]) -> Path | None:
        for key in ("source_path", "local_path", "upload_path", "absolute_path", "filepath"):
            value = str(item.get(key) or "").strip()
            if value:
                return Path(value).expanduser()
        return None

    def _host_codex_home(self, worker: dict) -> Path:
        return self._home_dir(worker["worker_id"]) / ".codex"

    def _host_plugin_denylist(self) -> tuple[str, ...]:
        return _host_plugin_denylist()

    def _host_codex_personality(self) -> str:
        return _host_codex_personality()

    def _source_host_codex_home(self) -> Path:
        return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")).expanduser()

    def _copy_host_codex_auth(self, target_codex_home: Path) -> None:
        source_auth = self._source_host_codex_home() / "auth.json"
        if not source_auth.exists() or not source_auth.is_file():
            return
        target_codex_home.mkdir(parents=True, exist_ok=True)
        target_auth = target_codex_home / "auth.json"
        shutil.copy2(source_auth, target_auth)
        target_auth.chmod(0o600)

    def _project_private_capability_directory(self, source: Path, target: Path) -> None:
        """Expose an installed, read-mostly capability catalog without copying it into LIFE.

        Existing worker-local state wins. A stale derived symlink is repaired, while a real file or
        directory is never replaced. The containing worker home is owner-only.
        """
        if not source.exists() or not source.is_dir():
            return
        resolved_source = source.resolve()
        if target.is_symlink():
            try:
                if target.resolve() == resolved_source:
                    return
            except OSError:
                pass
            target.unlink()
        elif target.exists():
            if not target.is_dir():
                return
            # Older worker homes may already contain harness-created catalog entries
            # (for example Codex's `.system` skills). Preserve those entries and project
            # only missing host capabilities so an upgrade enriches the worker instead of
            # replacing or silently skipping its local catalog.
            try:
                source_entries = sorted(source.iterdir(), key=lambda item: item.name)
            except OSError:
                return
            for source_entry in source_entries:
                target_entry = target / source_entry.name
                resolved_entry = source_entry.resolve()
                if target_entry.is_symlink():
                    try:
                        if target_entry.resolve() == resolved_entry:
                            continue
                    except OSError:
                        pass
                    target_entry.unlink()
                elif target_entry.exists():
                    continue
                target_entry.symlink_to(
                    resolved_entry,
                    target_is_directory=source_entry.is_dir(),
                )
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(resolved_source, target_is_directory=True)

    def _project_host_codex_capability_roots(self, target_codex_home: Path) -> None:
        source_home = self._source_host_codex_home()
        self._project_private_capability_directory(
            source_home / "skills", target_codex_home / "skills"
        )
        self._project_private_capability_directory(
            source_home / "plugins" / "cache",
            target_codex_home / "plugins" / "cache",
        )

    def _source_host_claude_home(self) -> Path:
        raw = os.environ.get("GLASSHIVE_HOST_CLAUDE_CONFIG", "").strip()
        return Path(raw).expanduser() if raw else Path.home() / ".claude"

    def _merge_private_capability_registry(self, source: Path, target: Path) -> None:
        """Add host registry entries without replacing worker-local selections."""
        if not source.exists() or not source.is_file():
            return
        if target.is_symlink():
            target.unlink()
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            target.chmod(0o600)
            return
        if not target.is_file():
            return
        try:
            source_payload = json.loads(source.read_text())
            target_payload = json.loads(target.read_text())
        except (OSError, json.JSONDecodeError):
            return

        def merge_additive(current: object, incoming: object) -> object:
            if isinstance(current, dict) and isinstance(incoming, dict):
                merged = dict(current)
                for key, value in incoming.items():
                    merged[key] = merge_additive(merged[key], value) if key in merged else value
                return merged
            if isinstance(current, list) and isinstance(incoming, list):
                merged = list(current)
                for value in incoming:
                    if value not in merged:
                        merged.append(value)
                return merged
            return current

        merged_payload = merge_additive(target_payload, source_payload)
        if merged_payload == target_payload:
            return
        target.write_text(json.dumps(merged_payload, indent=2, sort_keys=True) + "\n")
        target.chmod(0o600)

    def _project_host_claude_capability_roots(self, target_claude_home: Path) -> None:
        source_home = self._source_host_claude_home()
        for relative in (Path("plugins/cache"), Path("plugins/marketplaces"), Path("skills")):
            self._project_private_capability_directory(
                source_home / relative,
                target_claude_home / relative,
            )
        for filename in ("installed_plugins.json", "known_marketplaces.json"):
            source = source_home / "plugins" / filename
            target = target_claude_home / "plugins" / filename
            self._merge_private_capability_registry(source, target)

    def _host_codex_native_mcp_allowlist(self) -> set[str]:
        raw = os.environ.get(
            "GLASSHIVE_HOST_CODEX_NATIVE_MCP_ALLOWLIST",
            os.environ.get("WPR_HOST_CODEX_NATIVE_MCP_ALLOWLIST", ""),
        ).strip()
        if not raw:
            return set(_HOST_CODEX_NATIVE_MCP_ALLOWLIST)
        if raw.lower() in {"0", "false", "no", "off", "none", "disabled"}:
            return set()
        return {
            item.strip()
            for item in raw.split(",")
            if item.strip() and re.fullmatch(r"[A-Za-z0-9_.-]+", item.strip())
        }

    def _host_codex_plugin_cache_root(self) -> Path:
        raw = os.environ.get(
            "GLASSHIVE_HOST_CODEX_PLUGIN_CACHE",
            os.environ.get("WPR_HOST_CODEX_PLUGIN_CACHE", ""),
        ).strip()
        if raw:
            return Path(raw).expanduser()
        return self._source_host_codex_home() / "plugins" / "cache"

    def _host_codex_bundled_mcp_config(self, names: set[str]) -> str:
        if not names:
            return ""
        cache_root = self._host_codex_plugin_cache_root()
        if not cache_root.exists():
            return ""
        blocks: list[str] = []
        found: set[str] = set()
        for manifest in sorted(cache_root.rglob(".mcp.json")):
            try:
                payload = json.loads(manifest.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            servers = payload.get("mcpServers") if isinstance(payload, dict) else None
            if not isinstance(servers, dict):
                continue
            for name in sorted(names - found):
                if name not in servers:
                    continue
                rendered = _render_codex_mcp_server_from_json(name, servers[name], manifest.parent)
                if rendered:
                    blocks.append(rendered)
                    found.add(name)
            if found >= names:
                break
        return "\n\n".join(blocks).strip()

    def _host_codex_known_native_mcp_config(self, names: set[str]) -> str:
        blocks: list[str] = []
        if "computer-use" in names:
            computer_use_client = (
                self._source_host_codex_home()
                / "computer-use"
                / "Codex Computer Use.app"
                / "Contents"
                / "SharedSupport"
                / "SkyComputerUseClient.app"
                / "Contents"
                / "MacOS"
                / "SkyComputerUseClient"
            )
            if computer_use_client.exists():
                blocks.append(
                    "[mcp_servers.computer-use]\n"
                    f"command = {_toml_string(computer_use_client)}\n"
                    "args = [\"mcp\"]"
                )
        return "\n\n".join(blocks).strip()

    def _host_codex_worker_config(
        self,
        codex_config_append: str,
        *,
        developer_instructions: str | None = None,
    ) -> str:
        append = codex_config_append.strip()
        append_names = _codex_mcp_server_names(append)
        preserve_names = self._host_codex_native_mcp_allowlist() - append_names
        source_config_path = self._source_host_codex_home() / "config.toml"
        preserved = ""
        if source_config_path.exists() and source_config_path.is_file():
            try:
                source_config = source_config_path.read_text()
            except OSError:
                source_config = ""
            preserved = _sanitize_codex_source_config(source_config, preserve_names, append_names)
        preserved_names = _codex_mcp_server_names(preserved)
        plugin_preserved = self._host_codex_bundled_mcp_config(preserve_names - preserved_names)
        plugin_names = _codex_mcp_server_names(plugin_preserved)
        known_native = self._host_codex_known_native_mcp_config(
            preserve_names - preserved_names - plugin_names
        )
        native = "\n\n".join(
            part for part in (preserved, plugin_preserved, known_native) if part.strip()
        ).strip()
        personality = self._host_codex_personality()
        native = _apply_codex_personality(native, personality)
        native = _apply_codex_developer_instructions(native, developer_instructions)
        denied_plugins = self._host_plugin_denylist()
        native = _apply_codex_plugin_denylist(native, denied_plugins)
        if append_names:
            native = _strip_codex_mcp_server_blocks(native, append_names)
        config = "\n\n".join(part for part in (native, append) if part.strip()).strip()
        _assert_codex_worker_policy(
            config,
            plugin_ids=denied_plugins,
            personality=personality,
            developer_instructions=developer_instructions,
        )
        return config

    def _assert_host_codex_worker_policy(self, worker: dict) -> None:
        denied_plugins = self._host_plugin_denylist()
        personality = self._host_codex_personality()
        bundle = self._bootstrap_bundle_for_worker(worker)
        developer_instructions = (
            str(bundle.get("developer_instructions") or "")
            if self._conversation_mode_from_worker(worker)
            and "developer_instructions" in bundle
            else None
        )
        if not denied_plugins and personality == "inherit" and developer_instructions is None:
            return
        config_path = self._host_codex_home(worker) / "config.toml"
        if not config_path.is_file():
            raise RuntimeErrorBase(
                "Host Codex worker policy config is missing; refusing to launch"
            )
        try:
            config_text = config_path.read_text()
        except OSError as exc:
            raise RuntimeErrorBase(
                "Host Codex worker policy config is unreadable; refusing to launch"
            ) from exc
        _assert_codex_worker_policy(
            config_text,
            plugin_ids=denied_plugins,
            personality=personality,
            developer_instructions=developer_instructions,
        )

    def _write_host_project_mcp_files(self, worker: dict, workspace: Path, bundle: dict[str, object]) -> None:
        """Project scoped MCP/client config for host-native workers.

        Example broker projection:

            {
                "claude_project_mcp": {"glasshive-user-capabilities": {"url": "..."}},
                "codex_config_append": "[mcp_servers.glasshive-user-capabilities]...",
                "env": {"GLASSHIVE_CAPABILITY_BROKER_TOKEN": "..."}
            }

        Files are owner-only because they can contain scoped broker grants or local CLI config.
        """
        project_mcp = bundle.get("claude_project_mcp")
        if isinstance(project_mcp, dict):
            payload = claude_project_mcp_payload_for_bundle(bundle, project_mcp)
            target = workspace / ".mcp.json"
            target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            target.chmod(0o600)

        settings_local = bundle.get("claude_settings_local")
        if isinstance(settings_local, dict):
            target = workspace / ".claude" / "settings.local.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(settings_local, indent=2, sort_keys=True) + "\n")
            target.chmod(0o600)

        codex_config_append = str(bundle.get("codex_config_append") or "").strip()
        developer_instructions = (
            str(bundle.get("developer_instructions") or "")
            if "developer_instructions" in bundle
            else None
        )
        if (
            codex_config_append
            or self._host_plugin_denylist()
            or self._host_codex_personality() != "inherit"
            or developer_instructions is not None
        ):
            codex_config = self._host_codex_worker_config(
                codex_config_append,
                developer_instructions=developer_instructions,
            )
            target = workspace / ".codex" / "config.toml"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(codex_config + "\n")
            target.chmod(0o600)
            codex_home = self._host_codex_home(worker)
            codex_home.mkdir(parents=True, exist_ok=True)
            codex_home.chmod(0o700)
            codex_target = codex_home / "config.toml"
            codex_target.write_text(codex_config + "\n")
            codex_target.chmod(0o600)
            if not worker.get("_glasshive_provider_account_bound"):
                self._copy_host_codex_auth(codex_home)
            self._project_host_codex_capability_roots(codex_home)

    def _write_conversation_runtime_files(self, worker: dict, bundle: dict[str, object]) -> None:
        """Write harness configuration under private worker state, never inside LIFE."""

        profile = str(worker.get("profile") or "").strip()
        if profile == "codex-cli":
            codex_home = self._host_codex_home(worker)
            codex_home.mkdir(parents=True, exist_ok=True)
            codex_home.chmod(0o700)
            codex_config = self._host_codex_worker_config(
                str(bundle.get("codex_config_append") or ""),
                developer_instructions=(
                    str(bundle.get("developer_instructions") or "")
                    if "developer_instructions" in bundle
                    else None
                ),
            )
            config_path = codex_home / "config.toml"
            config_path.write_text((codex_config.rstrip() + "\n") if codex_config else "")
            config_path.chmod(0o600)
            if not worker.get("_glasshive_provider_account_bound"):
                self._copy_host_codex_auth(codex_home)
            self._project_host_codex_capability_roots(codex_home)
            return

        if profile != "claude-code":
            return
        claude_home = self._home_dir(str(worker["worker_id"])) / ".claude"
        claude_home.mkdir(parents=True, exist_ok=True)
        claude_home.chmod(0o700)
        self._project_host_claude_capability_roots(claude_home)
        project_mcp = bundle.get("claude_project_mcp")
        state_dir = self._state_dir(str(worker["worker_id"]))
        state_dir.mkdir(parents=True, exist_ok=True)
        state_dir.chmod(0o700)
        authority_path = state_dir / "conversation-developer-instructions.md"
        developer_instructions = str(bundle.get("developer_instructions") or "").strip()
        if developer_instructions:
            authority_path.write_text(developer_instructions + "\n")
            authority_path.chmod(0o600)
        else:
            try:
                authority_path.unlink()
            except FileNotFoundError:
                pass
        mcp_path = state_dir / "conversation-mcp.json"
        if isinstance(project_mcp, dict):
            payload = claude_project_mcp_payload_for_bundle(bundle, project_mcp)
            mcp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            mcp_path.chmod(0o600)
        else:
            try:
                mcp_path.unlink()
            except FileNotFoundError:
                pass

    def _materialize_workspace(self, worker: dict, workspace: Path) -> None:
        root = self._host_workspace_root(worker)
        root.mkdir(parents=True, exist_ok=True)
        if not os.access(root, os.W_OK):
            raise RuntimeErrorBase(f"Host workspace root is not writable: {root}")
        workspace.mkdir(parents=True, exist_ok=True)
        if self._conversation_mode_from_worker(worker):
            self._write_conversation_runtime_files(
                worker,
                self._bootstrap_bundle_for_worker(worker),
            )
            return
        bundle = self._bootstrap_bundle_for_worker(worker)
        self._write_workspace_file(workspace, "project-definition.md", self._host_project_definition(worker), overwrite=False)
        if not (workspace / "work-log.md").exists():
            self._write_workspace_file(
                workspace,
                "work-log.md",
                f"# Work Log\n\n- {datetime.now().isoformat(timespec='seconds')}: Workspace initialized for {self._agent_type()}.\n",
                overwrite=False,
            )
        self._write_workspace_file(workspace, "harness-prompt.md", self._host_harness_prompt(worker), overwrite=True)

        agents_md = merge_glasshive_worker_instructions(HOST_DEFAULT_AGENTS_MD, bundle.get("agents_md"))
        claude_bundle = dict(bundle)
        if not str(claude_bundle.get("claude_md") or "").strip():
            claude_bundle["claude_md"] = HOST_DEFAULT_CLAUDE_MD
        claude_md = glasshive_project_claude_md(claude_bundle)
        codex_bundle = dict(bundle)
        if not str(codex_bundle.get("codex_md") or "").strip():
            codex_bundle["codex_md"] = HOST_DEFAULT_CODEX_MD
        codex_md = glasshive_project_codex_md(codex_bundle)
        for name, content in (
            ("agents.md", agents_md),
            ("AGENTS.md", agents_md),
            ("claude.md", claude_md),
            ("CLAUDE.md", claude_md),
            ("codex.md", codex_md),
            ("CODEX.md", codex_md),
        ):
            self._write_workspace_file(workspace, name, content, overwrite=True)
        self._write_host_project_mcp_files(worker, workspace, bundle)

        self._write_workspace_file(
            workspace,
            "glasshive-host-tools/capture-front-window.sh",
            "\n".join(
                [
                    "#!/usr/bin/env bash",
                    "set -euo pipefail",
                    "if [[ $# -lt 1 || $# -gt 2 ]]; then",
                    '  echo "Usage: $0 <output-path> [app-name]" >&2',
                    "  exit 1",
                    "fi",
                    "OUT_PATH=$1",
                    "APP_NAME=${2:-}",
                    'if [[ -z "$APP_NAME" ]]; then',
                    "  APP_NAME=$(/usr/bin/osascript -e 'tell application \"System Events\" to get name of first application process whose frontmost is true')",
                    "fi",
                    "BOUNDS=$(",
                    '  /usr/bin/osascript - "$APP_NAME" <<\'APPLESCRIPT\'',
                    "on run argv",
                    "  set appName to item 1 of argv",
                    '  tell application "System Events"',
                    "    tell process appName",
                    "      set frontmost to true",
                    "      set p to position of window 1",
                    "      set s to size of window 1",
                    '      return (item 1 of p as text) & "," & (item 2 of p as text) & "," & (item 1 of s as text) & "," & (item 2 of s as text)',
                    "    end tell",
                    "  end tell",
                    "end run",
                    "APPLESCRIPT",
                    ")",
                    'IFS=, read -r X Y W H <<<"$BOUNDS"',
                    'mkdir -p "$(dirname "$OUT_PATH")"',
                    'if [[ "${H:-0}" -lt 100 || "${W:-0}" -lt 100 ]]; then',
                    '  /usr/sbin/screencapture "$OUT_PATH"',
                    '  echo "captured full screen to $OUT_PATH (window bounds looked invalid for $APP_NAME: $BOUNDS)"',
                    "  exit 0",
                    "fi",
                    '/usr/sbin/screencapture -R"${X},${Y},${W},${H}" "$OUT_PATH"',
                    'echo "captured $APP_NAME window to $OUT_PATH"',
                ]
            )
            + "\n",
            overwrite=True,
        )
        self._write_workspace_file(
            workspace,
            "glasshive-host-tools/content-hygiene.py",
            HOST_CONTENT_HYGIENE_TOOL,
            overwrite=True,
        )
        capture_helper = workspace / "glasshive-host-tools" / "capture-front-window.sh"
        content_hygiene_helper = workspace / "glasshive-host-tools" / "content-hygiene.py"
        try:
            capture_helper.chmod(0o755)
            content_hygiene_helper.chmod(0o755)
        except OSError as exc:
            self._append_work_log(worker, f"WARNING: capture helper chmod failed: {exc}")
        try:
            subprocess.run(
                ["/usr/bin/xattr", "-d", "com.apple.quarantine", str(capture_helper)],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            self._append_work_log(worker, "WARNING: capture helper quarantine cleanup could not run; invoke it through bash.")

        for item in bundle.get("files", []) if isinstance(bundle.get("files"), list) else []:
            if not isinstance(item, dict):
                continue
            if str(item.get("scope") or "workspace") != "workspace":
                continue
            path = str(item.get("path") or "").strip()
            if not path:
                continue
            if str(item.get("encoding") or "").strip().lower() == "base64" or "content_base64" in item:
                raw = str(item.get("content_base64") or item.get("content") or "")
                try:
                    decoded = base64.b64decode(raw, validate=True)
                except Exception as exc:
                    raise RuntimeErrorBase(f"Invalid base64 bootstrap content for {path}") from exc
                self._require_non_empty_bootstrap_file(path, len(decoded), item)
                self._write_workspace_bytes(workspace, path, decoded, overwrite=True)
                continue
            if "content" in item:
                content = str(item.get("content") or "")
                self._require_non_empty_bootstrap_file(path, len(content.encode("utf-8")), item)
                self._write_workspace_file(workspace, path, content, overwrite=True)
                continue
            source = self._source_path_from_bootstrap_file(item)
            if source is not None:
                resolved_source = resolve_bootstrap_source_path(source)
                if resolved_source.is_file():
                    self._require_non_empty_bootstrap_file(path, resolved_source.stat().st_size, item)
                self._copy_workspace_source_file(workspace, path, source)
            else:
                raise RuntimeErrorBase(f"Bootstrap file {path} is missing content or source_path")

    def _append_work_log(self, worker: dict, message: str) -> None:
        if self._conversation_mode_from_worker(worker):
            return
        path = self._host_workspace_dir(worker) / "work-log.md"
        try:
            with path.open("a") as handle:
                handle.write(f"- {datetime.now().isoformat(timespec='seconds')}: {message}\n")
        except OSError:
            return

    def _action_audit_path(self, worker_id: str) -> Path:
        return self._state_dir(worker_id) / "action-audit.jsonl"

    def _write_action_audit(self, worker: dict, payload: dict[str, object]) -> None:
        worker_id = worker["worker_id"]
        path = self._action_audit_path(worker_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "worker_id": worker_id,
            "runtime": self.runtime_name,
            "execution_mode": "host",
            **payload,
        }
        with path.open("a") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
        path.chmod(0o600)

    def _host_env(self, worker: dict, run_id: str | None = None) -> dict[str, str]:
        env: dict[str, str] = {}
        # USER/LOGNAME are required for macOS Keychain-backed CLIs (e.g. claude-code's
        # subscription auth resolves the keychain item by user); without them the worker
        # reports "Not logged in". Codex is unaffected because it uses a copied auth.json.
        for key in ("HOME", "PATH", "SHELL", "TERM", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "USER", "LOGNAME"):
            value = os.environ.get(key)
            if value:
                env[key] = value
        for key, value in os.environ.items():
            if key.startswith("LC_") and value:
                env[key] = value
        env.setdefault("HOME", str(Path.home()))
        env.setdefault("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin")
        env.setdefault("SHELL", os.environ.get("SHELL", "/bin/zsh"))
        env.update(bootstrap_env_for(worker))
        workspace = self._host_workspace_dir(worker)
        env["GLASSHIVE_WORKER_ID"] = str(worker.get("worker_id") or "")
        env["GLASSHIVE_WORKER_RUNTIME"] = self.runtime_name
        env["GLASSHIVE_EXECUTION_MODE"] = "host"
        env["GLASSHIVE_WORKSPACE_DIR"] = str(workspace)
        _project_workspace_dependency_env(env)
        if run_id:
            env["GLASSHIVE_RUN_ID"] = run_id
        return env

    def _host_runtime_info(self, worker: dict, *, pid: int | None = None) -> RuntimeInfo:
        worker_id = worker["worker_id"]
        session_key = self._read_session_key(worker_id) or worker.get("session_key") or self._default_session_key(worker)
        if session_key:
            self._write_session_key(worker_id, session_key)
        workspace = self._host_workspace_dir(worker)
        return RuntimeInfo(
            runtime=self.runtime_name,
            model=worker.get("model") or self.resolve_model(worker.get("profile", "")),
            gateway_url="",
            gateway_port=None,
            gateway_token=None,
            session_key=session_key,
            state_dir=str(self._state_dir(worker_id)),
            workspace_dir=str(workspace),
            pid=pid,
        )

    def preflight_worker_profile(self, profile: str, execution_mode: str = "host") -> None:
        if shutil.which(self.binary) is None:
            raise RuntimeDependencyMissingError(
                f"{self.binary} CLI is not installed or not on PATH for host-native {self.runtime_name}",
                binary=self.binary,
                runtime_name=self.runtime_name,
                profile=profile,
                execution_mode=execution_mode,
            )
        issue = host_runtime_requirement_issue(profile, self.runtime_name, configured_binary=self.binary)
        if issue is not None:
            raise RuntimeDependencyMissingError(
                issue.user_message,
                binary=issue.binary,
                runtime_name=self.runtime_name,
                profile=profile,
                execution_mode=execution_mode,
                required_version=issue.required_version,
                actual_version=issue.actual_version,
                dependency_label=issue.label,
                recovery_hint=issue.recommended_recovery,
            )

    def ensure_worker_ready(self, worker: dict) -> RuntimeInfo:
        self.preflight_worker_profile(
            str(worker.get("profile") or ""),
            str(worker.get("execution_mode") or "host"),
        )
        worker_id = worker["worker_id"]
        self._state_dir(worker_id).mkdir(parents=True, exist_ok=True)
        self._home_dir(worker_id).mkdir(parents=True, exist_ok=True)
        workspace = self._host_workspace_dir(worker)
        self._materialize_workspace(worker, workspace)
        self._write_action_audit(
            worker,
            {
                "kind": "worker.ready",
                "cwd": str(workspace),
                "env_keys": [],
                "message": f"Host-native {self.runtime_name} workspace ready.",
            },
        )
        return self._host_runtime_info(worker, pid=self._active_pid(worker_id))

    def prepare_worker_workspace(self, worker: dict) -> RuntimeInfo:
        """Materialize a host workspace without launching an agent process."""
        worker_id = str(worker["worker_id"])
        self._state_dir(worker_id).mkdir(parents=True, exist_ok=True)
        self._home_dir(worker_id).mkdir(parents=True, exist_ok=True)
        workspace = self._host_workspace_dir(worker)
        self._materialize_workspace(worker, workspace)
        return self._host_runtime_info(worker, pid=None)

    def _active_session_argv_for_evidence(self, active_session: dict[str, object] | None) -> list[str]:
        raw = str((active_session or {}).get("argv_for_evidence_json") or "").strip()
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    return [str(item or "") for item in parsed]
            except json.JSONDecodeError:
                pass
        return [self.runtime_name]

    def _active_session_constraint_ledger(
        self,
        *,
        workspace: Path,
        active_session: dict[str, object] | None,
    ) -> dict[str, object] | None:
        raw = str((active_session or {}).get("constraint_ledger_path") or "").strip()
        if not raw:
            return None
        path = Path(raw)
        if not path.is_absolute():
            path = workspace / raw
        try:
            payload = json.loads(path.read_text())
        except Exception:
            return None
        return payload if isinstance(payload, dict) else None

    def _write_stopped_active_run_evidence(
        self,
        worker: dict,
        *,
        active_session: dict[str, object] | None,
        stop_reason: str,
        error_text: str,
    ) -> None:
        if self._conversation_mode_from_worker(worker) or not active_session:
            return
        run_id = str(active_session.get("run_id") or "").strip()
        if not run_id:
            return
        workspace = Path(str(worker.get("workspace_dir") or self._host_workspace_dir(worker)))
        raw_stdout_path = str(active_session.get("stdout_path") or "").strip()
        raw_stderr_path = str(active_session.get("stderr_path") or "").strip()
        raw_exit_path = str(active_session.get("exit_path") or "").strip()
        stdout_path = Path(raw_stdout_path) if raw_stdout_path else None
        stderr_path = Path(raw_stderr_path) if raw_stderr_path else None
        exit_path = Path(raw_exit_path) if raw_exit_path else None
        stdout = stdout_path.read_text() if stdout_path and stdout_path.is_file() else ""
        stderr = stderr_path.read_text() if stderr_path and stderr_path.is_file() else ""
        self._record_run_metrics(worker["worker_id"], run_id, stdout)
        exit_code: int | None = None
        if exit_path and exit_path.is_file():
            try:
                exit_code = int(exit_path.read_text().strip())
            except ValueError:
                exit_code = None
        transcript_paths = {
            "stdout": raw_stdout_path,
            "stderr": raw_stderr_path,
            "exit_code": raw_exit_path,
            "constraint_ledger": str(active_session.get("constraint_ledger_path") or ""),
        }
        try:
            timeout_seconds = (
                float(active_session["timeout_seconds"])
                if active_session.get("timeout_seconds") is not None
                else None
            )
        except (TypeError, ValueError):
            timeout_seconds = None
        evidence_path = _write_evidence_for_run(
            worker=worker,
            run_id=run_id,
            runtime_name=self.runtime_name,
            model=str(active_session.get("model") or worker.get("model") or self.resolve_model(str(worker.get("profile") or ""))),
            command=self._active_session_argv_for_evidence(active_session),
            env={},
            workspace=workspace,
            stdout_text=stdout,
            stderr_text=stderr,
            output_text="",
            error_text=error_text,
            exit_code=exit_code,
            timeout_seconds=timeout_seconds,
            stop_reason=stop_reason,
            constraint_ledger=self._active_session_constraint_ledger(workspace=workspace, active_session=active_session),
            transcript_paths=transcript_paths,
        )
        raw_heartbeat_path = str(active_session.get("heartbeat_path") or "").strip()
        heartbeat_path = Path(raw_heartbeat_path) if raw_heartbeat_path else _active_run_status_path(workspace, run_id)
        started_at = str(active_session.get("started_at") or "").strip() or _utc_iso()
        _write_active_run_status(
            path=heartbeat_path,
            worker=worker,
            run_id=run_id,
            runtime_name=self.runtime_name,
            model=str(active_session.get("model") or worker.get("model") or self.resolve_model(str(worker.get("profile") or ""))),
            state=stop_reason,
            transcript_paths=transcript_paths,
            started_at=started_at,
            process_pid=None,
            timeout_seconds=timeout_seconds,
            exit_code=exit_code,
            stop_reason=stop_reason,
            evidence_path=evidence_path,
        )

    def pause_worker(self, worker: dict) -> RuntimeInfo:
        active_session = self._read_active_session(worker["worker_id"])
        active_run_id = str(worker.get("_active_run_id") or "").strip() or None
        if not active_run_id and active_session and self._active_pid(worker["worker_id"]):
            active_run_id = str(active_session.get("run_id") or "").strip() or None
        if active_run_id:
            self._note_stop_reason(worker["worker_id"], "paused", run_id=active_run_id)
        self._stop_active_process(worker["worker_id"], worker=worker, run_id=active_run_id)
        self._write_stopped_active_run_evidence(
            worker,
            active_session=active_session,
            stop_reason="paused",
            error_text="Worker was paused by the operator",
        )
        self._append_work_log(worker, "Worker paused by operator.")
        return self._host_runtime_info(worker, pid=None)

    def interrupt_worker(self, worker: dict, run_id: str | None = None) -> RuntimeInfo:
        active_session = self._read_active_session(worker["worker_id"])
        if active_session and run_id and active_session.get("run_id") != run_id:
            active_session = None
        active_run_id = str(run_id or worker.get("_active_run_id") or "").strip() or None
        if not active_run_id and active_session and self._active_pid(worker["worker_id"]):
            active_run_id = str(active_session.get("run_id") or "").strip() or None
        if active_run_id or self._active_pid(worker["worker_id"]):
            self._note_stop_reason(worker["worker_id"], "interrupted", run_id=active_run_id)
            self._stop_active_process(worker["worker_id"], worker=worker, run_id=active_run_id)
        self._write_stopped_active_run_evidence(
            worker,
            active_session=active_session,
            stop_reason="interrupted",
            error_text="Worker run was interrupted by the operator",
        )
        self._append_work_log(worker, "Active run interrupted by operator.")
        return self._host_runtime_info(worker, pid=None)

    def terminate_worker(self, worker: dict) -> RuntimeInfo:
        active_session = self._read_active_session(worker["worker_id"])
        active_run_id = str(worker.get("_active_run_id") or "").strip() or None
        if not active_run_id and active_session and self._active_pid(worker["worker_id"]):
            active_run_id = str(active_session.get("run_id") or "").strip() or None
        if active_run_id:
            self._note_stop_reason(worker["worker_id"], "terminated", run_id=active_run_id)
        self._stop_active_process(worker["worker_id"], worker=worker, run_id=active_run_id)
        self._write_stopped_active_run_evidence(
            worker,
            active_session=active_session,
            stop_reason="terminated",
            error_text="Worker was terminated by the operator",
        )
        self._append_work_log(worker, "Worker terminated by operator.")
        return self._host_runtime_info(worker, pid=None)

    def reconcile_worker(self, worker: dict) -> RuntimeInfo:
        worker_id = str(worker["worker_id"])
        pid = self._active_pid(worker_id)
        if pid:
            active_session = self._read_active_session(worker_id)
            if active_session:
                try:
                    self._release_durable_host_process(active_session)
                except (OSError, RuntimeErrorBase) as exc:
                    logger.warning(
                        "Failed to release a recovered host-native process handshake",
                        extra={"worker_id": worker_id, "error": str(exc)},
                    )
        with self._process_lock:
            if pid:
                lane = self._host_capacity_lane(worker)
                active = self._host_active_slots().get(lane)
                if not active or active == worker_id:
                    self._host_active_slots()[lane] = worker_id
                    self._host_worker_lanes()[worker_id] = lane
            else:
                lane = self._host_worker_lanes().pop(worker_id, None)
                if lane and self._host_active_slots().get(lane) == worker_id:
                    self._host_active_slots().pop(lane, None)
        return self._host_runtime_info(worker, pid=pid)

    def worker_compute_present(self, worker: dict) -> bool:
        return self._active_pid(worker["worker_id"]) is not None

    def _persisted_active_pid(self, worker_id: str) -> int | None:
        active_session = self._read_active_session(worker_id)
        expected_identity = str((active_session or {}).get("process_identity_sha256") or "").strip()
        if not expected_identity:
            # Legacy metadata cannot prove that a recycled PID is still the harness process.
            return None
        try:
            pid = int((active_session or {}).get("process_pid") or 0)
        except (TypeError, ValueError):
            return None
        if pid <= 1:
            return None
        try:
            # Host-native runs always launch with start_new_session=True. Requiring the
            # persisted PID to remain its own process-group leader prevents a recycled PID
            # from being mistaken for the prior harness process after an API restart.
            if os.getpgid(pid) != pid:
                return None
            os.kill(pid, 0)
        except (OSError, ProcessLookupError):
            return None
        if self._process_identity_sha256(pid) != expected_identity:
            return None
        return pid

    def _active_pid(self, worker_id: str) -> int | None:
        current = super()._active_pid(worker_id)
        return current if current is not None else self._persisted_active_pid(worker_id)

    def _stop_active_process(self, worker_id: str, *, worker: dict | None = None, run_id: str | None = None) -> None:
        active_session = self._read_active_session(worker_id)
        if active_session and run_id and str(active_session.get("run_id") or "") != str(run_id):
            active_session = None
        with self._process_lock:
            process = self._active_processes.get(worker_id)
        persisted_pid = self._persisted_active_pid(worker_id) if active_session else None
        pid = process.pid if process and process.poll() is None else persisted_pid
        if pid:
            try:
                os.killpg(pid, signal.SIGTERM)
            except OSError:
                pass
            if process and process.pid == pid:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except OSError:
                        pass
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
            else:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and self._persisted_active_pid(worker_id) == pid:
                    time.sleep(0.05)
                if self._persisted_active_pid(worker_id) == pid:
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except OSError:
                        pass
        if active_session:
            self._clear_active_session(worker_id)
        self._clear_process(worker_id)
        self._release_host_slot(worker_id)

    def _acquire_host_slot(self, worker: dict) -> None:
        if os.environ.get("WPR_HOST_ALLOW_CONCURRENT_SAME_CLI", "").strip().lower() in {"1", "true", "yes", "on"}:
            return
        worker_id = worker["worker_id"]
        lane = self._host_capacity_lane(worker)
        with self._process_lock:
            error = self._host_capacity_error_locked(worker_id, lane)
            if error is not None:
                raise error
            self._host_active_slots()[lane] = worker_id
            self._host_worker_lanes()[worker_id] = lane

    def _host_capacity_error_locked(
        self,
        worker_id: str,
        lane: str = "mission",
    ) -> RuntimeErrorBase | None:
        active = self._host_active_slots().get(lane)
        active_process = self._active_processes.get(active or "")
        if active and active != worker_id and (active_process is None or active_process.poll() is None):
            return RuntimeErrorBase(
                f"Host-native {self.runtime_name} already has an active {lane} worker ({active}); "
                f"v1 allows one active host worker per CLI family and {lane} lane."
            )
        return None

    def worker_capacity_error(self, worker: dict) -> RuntimeErrorBase | None:
        if os.environ.get("WPR_HOST_ALLOW_CONCURRENT_SAME_CLI", "").strip().lower() in {"1", "true", "yes", "on"}:
            return None
        with self._process_lock:
            return self._host_capacity_error_locked(
                str(worker["worker_id"]),
                self._host_capacity_lane(worker),
            )

    def _release_host_slot(self, worker_id: str) -> None:
        with self._process_lock:
            lane = self._host_worker_lanes().pop(worker_id, None)
            if lane and self._host_active_slots().get(lane) == worker_id:
                self._host_active_slots().pop(lane, None)

    def _build_command(self, worker: dict, instruction: str, info: RuntimeInfo) -> tuple[list[str], dict[str, str]]:
        raise NotImplementedError

    def _host_run_timeout_sec(self, timeout_sec: float | None = None) -> float | None:
        raw = (
            os.environ.get("GLASSHIVE_HOST_RUN_TIMEOUT_SEC", "").strip()
            or os.environ.get("WPR_HOST_RUN_TIMEOUT_SEC", "").strip()
            or os.environ.get("GLASSHIVE_RUN_TIMEOUT_SEC", "").strip()
            or os.environ.get("GLASSHIVE_MAX_RUN_DURATION_S", "").strip()
            or os.environ.get("WPR_RUN_TIMEOUT_SEC", "").strip()
        )
        if not raw:
            return timeout_sec if timeout_sec and timeout_sec > 0 else None
        if raw.lower() in {"0", "none", "off", "false", "disabled"}:
            return None
        try:
            parsed = float(raw)
        except ValueError:
            return None
        return parsed if parsed > 0 else None

    def _run_conversation_task(
        self,
        worker: dict,
        instruction: str,
        *,
        timeout_sec: float | None = None,
        run_id: str | None = None,
    ) -> str:
        """Run a natural harness turn without mission scaffolding or LIFE-side runtime files."""
        effective_run_id = (run_id or secrets.token_hex(8)).strip()
        worker = {**worker, "_active_run_id": effective_run_id, "_glasshive_conversation_run": True}
        info = self.ensure_worker_ready(worker)
        command, env = self._build_command(worker, instruction, info)
        stdin_text = self._command_stdin_text(worker, instruction, info)
        env["GLASSHIVE_RUN_ID"] = effective_run_id
        env["GLASSHIVE_RUN_MODE"] = "conversation"
        workspace = Path(str(info.workspace_dir or self._host_workspace_dir(worker)))
        workspace_side_effect_state = self._conversation_workspace_side_effect_state(workspace)
        self._acquire_host_slot(worker)

        run_root = self._run_root(str(worker["worker_id"]), effective_run_id)
        run_root.mkdir(parents=True, exist_ok=True)
        run_root.chmod(0o700)
        raw_stdout = run_root / "stdout.log"
        raw_stderr = run_root / "stderr.log"
        host_stdin = run_root / "instruction.stdin"
        exit_path = run_root / "exit_code"
        if stdin_text is not None:
            host_stdin.write_text(stdin_text)
            host_stdin.chmod(0o600)

        self._write_action_audit(
            worker,
            {
                "kind": "conversation.started",
                "run_id": effective_run_id,
                "cwd": str(workspace),
                "argv_redacted": [_redact_command_arg(part) for part in command],
                "env_keys": sorted(env.keys()),
                "effort_projection": _effort_projection_for_audit(worker),
            },
        )
        run_timeout_sec = self._host_run_timeout_sec(timeout_sec)
        process: subprocess.Popen[str] | None = None
        try:
            with raw_stdout.open("w") as stdout_handle, raw_stderr.open("w") as stderr_handle:
                raw_stdout.chmod(0o600)
                raw_stderr.chmod(0o600)
                process_command = self._durable_host_process_command(
                    command,
                    run_root=run_root,
                    exit_path=exit_path,
                    timeout_sec=run_timeout_sec,
                    stdin_path=host_stdin if stdin_text is not None else None,
                )
                process = subprocess.Popen(
                    process_command,
                    cwd=str(workspace),
                    env=env,
                    text=True,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    start_new_session=True,
                )
                self._register_process(str(worker["worker_id"]), process)
                self._wait_for_durable_host_supervisor(process, run_root=run_root)
                self._write_active_session(
                    str(worker["worker_id"]),
                    {
                        "session_name": f"conversation-{effective_run_id[:12]}",
                        "run_id": effective_run_id,
                        "stdout_path": str(raw_stdout),
                        "stderr_path": str(raw_stderr),
                        "exit_path": str(exit_path),
                        "model": str(info.model or ""),
                        "argv_for_evidence_json": json.dumps([_redact_command_arg(part) for part in command]),
                        "started_at": _utc_iso(),
                        "process_pid": process.pid,
                        "timeout_seconds": run_timeout_sec,
                        "instruction": instruction,
                        "run_mode": "conversation",
                    },
                )
                active_session = self._read_active_session(str(worker["worker_id"]))
                if not active_session:
                    self._stop_active_process(
                        str(worker["worker_id"]),
                        worker=worker,
                        run_id=effective_run_id,
                    )
                    raise RuntimeErrorBase(
                        "Host-native process metadata was not durably recorded before launch"
                    )
                with runtime_start_boundary(worker):
                    self._release_durable_host_process(active_session)
                    notify_runtime_started(worker)
                try:
                    exit_code = process.wait()
                except subprocess.TimeoutExpired as exc:
                    self._note_stop_reason(str(worker["worker_id"]), "terminated", run_id=effective_run_id)
                    self._stop_active_process(str(worker["worker_id"]), worker=worker, run_id=effective_run_id)
                    raise RuntimeErrorBase(f"{self.runtime_name} timed out after {run_timeout_sec:g}s") from exc
        finally:
            self._clear_process(str(worker["worker_id"]))
            self._release_host_slot(str(worker["worker_id"]))
            try:
                self._cleanup_conversation_workspace_side_effects(
                    workspace,
                    workspace_side_effect_state,
                )
            except OSError as exc:
                logger.warning(
                    "Failed to clean harness conversation workspace side effect",
                    extra={
                        "worker_id": str(worker.get("worker_id") or ""),
                        "workspace": str(workspace),
                        "error": str(exc),
                    },
                )

        exit_code = self._final_host_exit_code(exit_path, exit_code)
        stdout = raw_stdout.read_text() if raw_stdout.exists() else ""
        stderr = raw_stderr.read_text() if raw_stderr.exists() else ""
        self._finalize_stop_reason(str(worker["worker_id"]), run_id=effective_run_id)
        if exit_code != 0:
            detail = (_redact_text(stderr, max_chars=2000) or _redact_text(stdout, max_chars=2000)).strip()
            self._write_action_audit(
                worker,
                {
                    "kind": "conversation.failed",
                    "run_id": effective_run_id,
                    "exit_code": exit_code,
                    "detail": detail,
                },
            )
            raise RuntimeErrorBase(f"{self.runtime_name} exited with code {exit_code}: {detail}")

        session_key, output = self._parse_output(worker, stdout, stderr, info)
        if session_key:
            self._write_session_key(str(worker["worker_id"]), session_key)
        redacted_output = _redact_text(str(output or "").strip())
        if len(redacted_output) > _HOST_RUN_OUTPUT_MAX_CHARS:
            redacted_output = f"{redacted_output[: _HOST_RUN_OUTPUT_MAX_CHARS - 3].rstrip()}..."
        self._write_action_audit(
            worker,
            {
                "kind": "conversation.completed",
                "run_id": effective_run_id,
                "exit_code": exit_code,
                "output_chars": len(redacted_output),
            },
        )
        return redacted_output

    def run_task(self, worker: dict, instruction: str, timeout_sec: float | None = None, run_id: str | None = None) -> str:
        if self._conversation_mode_from_worker(worker):
            return self._run_conversation_task(worker, instruction, timeout_sec=timeout_sec, run_id=run_id)
        effective_run_id = (run_id or secrets.token_hex(8)).strip()
        worker = {
            **worker,
            "_active_run_id": effective_run_id,
            "_glasshive_task_run": True,
        }
        info = self.ensure_worker_ready(worker)
        command, env = self._build_command(worker, instruction, info)
        stdin_text = self._command_stdin_text(worker, instruction, info)
        env["GLASSHIVE_RUN_ID"] = effective_run_id
        workspace = Path(str(info.workspace_dir or self._host_workspace_dir(worker)))
        constraint_ledger, constraint_ledger_path = _write_constraint_ledger_for_run(
            worker=worker,
            instruction=instruction,
            workspace=workspace,
            run_id=effective_run_id,
        )
        self._acquire_host_slot(worker)

        run_root = self._run_root(worker["worker_id"], effective_run_id)
        run_root.mkdir(parents=True, exist_ok=True)
        run_root.chmod(0o700)
        raw_stdout = run_root / "stdout.log"
        raw_stderr = run_root / "stderr.log"
        host_stdin = run_root / "instruction.stdin"
        exit_path = run_root / "exit_code"
        stdout_path, stderr_path = self._log_paths(worker["worker_id"])
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        if stdin_text is not None:
            host_stdin.write_text(stdin_text)
            host_stdin.chmod(0o600)

        command_display = _redacted_command_display(command)
        self._append_work_log(worker, f"Run {effective_run_id} started with host-native {self.runtime_name}.")
        self._write_action_audit(
            worker,
            {
                "kind": "run.started",
                "run_id": effective_run_id,
                "cwd": str(workspace),
                "argv_redacted": [_redact_command_arg(part) for part in command],
                "env_keys": sorted(env.keys()),
                "constraint_ledger_path": constraint_ledger_path,
                "effort_projection": _effort_projection_for_audit(worker),
            },
        )

        with stderr_path.open("a") as aggregate:
            aggregate.write(f"$ host {self.runtime_name} {command_display}\n")
            stderr_path.chmod(0o600)

        transcript_paths = {
            "stdout": str(raw_stdout),
            "stderr": str(raw_stderr),
            "exit_code": str(exit_path),
            "constraint_ledger": constraint_ledger_path,
        }
        started_at = time.time()
        started_at_iso = _utc_iso()
        run_timeout_sec = self._host_run_timeout_sec(timeout_sec)
        heartbeat_path = _active_run_status_path(workspace, effective_run_id)
        heartbeat_stop = Event()
        heartbeat_thread: Thread | None = None
        with raw_stdout.open("w") as stdout_handle, raw_stderr.open("w") as stderr_handle:
            raw_stdout.chmod(0o600)
            raw_stderr.chmod(0o600)
            process_command = self._durable_host_process_command(
                command,
                run_root=run_root,
                exit_path=exit_path,
                timeout_sec=run_timeout_sec,
                stdin_path=host_stdin if stdin_text is not None else None,
            )
            process = subprocess.Popen(
                process_command,
                cwd=str(workspace),
                env=env,
                text=True,
                stdin=subprocess.DEVNULL,
                stdout=stdout_handle,
                stderr=stderr_handle,
                start_new_session=True,
            )
            self._register_process(worker["worker_id"], process)
            process_pid = process.pid
            self._wait_for_durable_host_supervisor(process, run_root=run_root)
            self._write_active_session(
                worker["worker_id"],
                {
                    "session_name": f"host-{effective_run_id[:12]}",
                    "run_id": effective_run_id,
                    "stdout_path": str(raw_stdout),
                    "stderr_path": str(raw_stderr),
                    "exit_path": str(exit_path),
                    "constraint_ledger_path": constraint_ledger_path,
                    "model": str(info.model or ""),
                    "argv_for_evidence_json": json.dumps([_redact_command_arg(part) for part in command]),
                    "started_at": started_at_iso,
                    "process_pid": process.pid,
                    "heartbeat_path": str(heartbeat_path),
                    "timeout_seconds": run_timeout_sec,
                    "instruction": instruction,
                },
            )
            active_session = self._read_active_session(str(worker["worker_id"]))
            if not active_session:
                self._stop_active_process(
                    str(worker["worker_id"]),
                    worker=worker,
                    run_id=effective_run_id,
                )
                raise RuntimeErrorBase(
                    "Host-native process metadata was not durably recorded before launch"
                )
            with runtime_start_boundary(worker):
                self._release_durable_host_process(active_session)
                notify_runtime_started(worker)
            _write_active_run_status(
                path=heartbeat_path,
                worker=worker,
                run_id=effective_run_id,
                runtime_name=self.runtime_name,
                model=str(info.model or ""),
                state="running",
                transcript_paths=transcript_paths,
                started_at=started_at_iso,
                process_pid=process.pid,
                timeout_seconds=run_timeout_sec,
            )
            heartbeat_thread = _start_active_run_heartbeat(
                path=heartbeat_path,
                worker=worker,
                run_id=effective_run_id,
                runtime_name=self.runtime_name,
                model=str(info.model or ""),
                transcript_paths=transcript_paths,
                started_at=started_at_iso,
                process_pid=process.pid,
                timeout_seconds=run_timeout_sec,
                stop_event=heartbeat_stop,
            )
            try:
                exit_code = process.wait()
            except subprocess.TimeoutExpired:
                self._note_stop_reason(worker["worker_id"], "terminated", run_id=effective_run_id)
                self._stop_active_process(worker["worker_id"], worker=worker, run_id=effective_run_id)
                try:
                    stdout_handle.flush()
                    stderr_handle.flush()
                except OSError:
                    pass
                timeout_stdout = raw_stdout.read_text() if raw_stdout.exists() else ""
                timeout_stderr = raw_stderr.read_text() if raw_stderr.exists() else ""
                self._record_run_metrics(worker["worker_id"], effective_run_id, timeout_stdout)
                evidence_path = _write_evidence_for_run(
                    worker=worker,
                    run_id=effective_run_id,
                    runtime_name=self.runtime_name,
                    model=str(info.model or ""),
                    command=command,
                    env=env,
                    workspace=workspace,
                    stdout_text=timeout_stdout,
                    stderr_text=timeout_stderr,
                    output_text="",
                    error_text=f"{self.runtime_name} timed out after {run_timeout_sec:g}s",
                    exit_code=None,
                    timeout_seconds=run_timeout_sec,
                    stop_reason="timeout",
                    constraint_ledger=constraint_ledger,
                    transcript_paths=transcript_paths,
                    started_at=started_at,
                )
                _stop_active_run_heartbeat(heartbeat_stop, heartbeat_thread)
                _write_active_run_status(
                    path=heartbeat_path,
                    worker=worker,
                    run_id=effective_run_id,
                    runtime_name=self.runtime_name,
                    model=str(info.model or ""),
                    state="timeout",
                    transcript_paths=transcript_paths,
                    started_at=started_at_iso,
                    process_pid=process.pid,
                    timeout_seconds=run_timeout_sec,
                    stop_reason="timeout",
                    evidence_path=evidence_path,
                )
                self._append_work_log(
                    worker,
                    f"Run {effective_run_id} exceeded configured host timeout after {run_timeout_sec:g}s.",
                )
                raise RuntimeErrorBase(f"{self.runtime_name} timed out after {run_timeout_sec:g}s")
            finally:
                _stop_active_run_heartbeat(heartbeat_stop, heartbeat_thread)
                self._clear_process(worker["worker_id"])
                self._release_host_slot(worker["worker_id"])

        exit_code = self._final_host_exit_code(exit_path, exit_code)
        stdout = raw_stdout.read_text() if raw_stdout.exists() else ""
        stderr = raw_stderr.read_text() if raw_stderr.exists() else ""
        redacted_stdout = _redact_text(stdout, max_chars=16000)
        redacted_stderr = _redact_text(stderr, max_chars=16000)
        with stdout_path.open("a") as aggregate:
            if redacted_stdout:
                aggregate.write(redacted_stdout)
                if not redacted_stdout.endswith("\n"):
                    aggregate.write("\n")
            stdout_path.chmod(0o600)
        with stderr_path.open("a") as aggregate:
            if redacted_stderr:
                aggregate.write(redacted_stderr)
                if not redacted_stderr.endswith("\n"):
                    aggregate.write("\n")
            stderr_path.chmod(0o600)

        self._write_action_audit(
            worker,
            {
                "kind": "run.completed" if exit_code == 0 else "run.failed",
                "run_id": effective_run_id,
                "cwd": str(workspace),
                "exit_code": exit_code,
                "stdout_tail": redacted_stdout[-2000:],
                "stderr_tail": redacted_stderr[-2000:],
            },
        )

        self._record_run_metrics(worker["worker_id"], effective_run_id, stdout)
        try:
            self._finalize_stop_reason(worker["worker_id"], run_id=effective_run_id)
        except RuntimeErrorBase as exc:
            if isinstance(exc, WorkerPausedError):
                active_state = "paused"
            elif isinstance(exc, WorkerInterruptedError):
                active_state = "interrupted"
            elif isinstance(exc, WorkerTerminatedError):
                active_state = "terminated"
            else:
                active_state = "failed"
            evidence_path = _write_evidence_for_run(
                worker=worker,
                run_id=effective_run_id,
                runtime_name=self.runtime_name,
                model=str(info.model or ""),
                command=command,
                env=env,
                workspace=workspace,
                stdout_text=stdout,
                stderr_text=stderr,
                output_text="",
                error_text=str(exc),
                exit_code=exit_code,
                timeout_seconds=run_timeout_sec,
                stop_reason=exc.__class__.__name__,
                constraint_ledger=constraint_ledger,
                transcript_paths=transcript_paths,
                started_at=started_at,
            )
            _write_active_run_status(
                path=heartbeat_path,
                worker=worker,
                run_id=effective_run_id,
                runtime_name=self.runtime_name,
                model=str(info.model or ""),
                state=active_state,
                transcript_paths=transcript_paths,
                started_at=started_at_iso,
                process_pid=process_pid,
                timeout_seconds=run_timeout_sec,
                exit_code=exit_code,
                stop_reason=exc.__class__.__name__,
                evidence_path=evidence_path,
            )
            raise
        if exit_code != 0:
            if self.runtime_name == "claude-code":
                classification = classify_cli_failure(
                    stdout=stdout,
                    stderr=stderr,
                    runtime_name=self.runtime_name,
                    exit_code=exit_code,
                )
                detail = classification.user_message
            else:
                detail = (redacted_stderr or redacted_stdout or "").strip()[-2000:]
            self._append_work_log(worker, f"Run {effective_run_id} failed with exit code {exit_code}.")
            error_text = f"{self.runtime_name} exited with code {exit_code}: {detail}"
            evidence_path = _write_evidence_for_run(
                worker=worker,
                run_id=effective_run_id,
                runtime_name=self.runtime_name,
                model=str(info.model or ""),
                command=command,
                env=env,
                workspace=workspace,
                stdout_text=stdout,
                stderr_text=stderr,
                output_text="",
                error_text=error_text,
                exit_code=exit_code,
                timeout_seconds=run_timeout_sec,
                stop_reason="process_exit",
                constraint_ledger=constraint_ledger,
                transcript_paths=transcript_paths,
                started_at=started_at,
            )
            _write_active_run_status(
                path=heartbeat_path,
                worker=worker,
                run_id=effective_run_id,
                runtime_name=self.runtime_name,
                model=str(info.model or ""),
                state="failed",
                transcript_paths=transcript_paths,
                started_at=started_at_iso,
                process_pid=process_pid,
                timeout_seconds=run_timeout_sec,
                exit_code=exit_code,
                stop_reason="process_exit",
                evidence_path=evidence_path,
            )
            raise RuntimeErrorBase(f"{self.runtime_name} exited with code {exit_code}: {detail}")

        session_key, output = self._parse_output(worker, stdout, stderr, info)
        if session_key:
            self._write_session_key(worker["worker_id"], session_key)
        if _FINAL_REPORT_PATTERN.search(stdout) and not _FINAL_REPORT_PATTERN.search(output):
            output = f"FINAL REPORT:\n{output.strip()}"
        redacted_output = _redact_text(output.strip())
        if len(redacted_output) > _HOST_RUN_OUTPUT_MAX_CHARS:
            redacted_output = f"{redacted_output[: _HOST_RUN_OUTPUT_MAX_CHARS - 3].rstrip()}..."
        evidence_path = _write_evidence_for_run(
            worker=worker,
            run_id=effective_run_id,
            runtime_name=self.runtime_name,
            model=str(info.model or ""),
            command=command,
            env=env,
            workspace=workspace,
            stdout_text=stdout,
            stderr_text=stderr,
            output_text=redacted_output,
            error_text="",
            exit_code=exit_code,
            timeout_seconds=run_timeout_sec,
            stop_reason="process_exit",
            constraint_ledger=constraint_ledger,
            transcript_paths=transcript_paths,
            started_at=started_at,
        )
        try:
            evidence_status, warning_message = _require_successful_run_evidence(
                workspace=workspace,
                evidence_path=evidence_path,
                constraint_ledger_path=constraint_ledger_path,
                run_id=effective_run_id,
            )
        except RuntimeErrorBase as exc:
            evidence_message = str(exc)
            if evidence_path:
                self._write_action_audit(
                    worker,
                    {
                        "kind": "run.evidence_failed",
                        "run_id": effective_run_id,
                        "evidence_path": evidence_path,
                        "message": evidence_message,
                    },
                )
            _write_active_run_status(
                path=heartbeat_path,
                worker=worker,
                run_id=effective_run_id,
                runtime_name=self.runtime_name,
                model=str(info.model or ""),
                state="failed",
                transcript_paths=transcript_paths,
                started_at=started_at_iso,
                process_pid=process_pid,
                timeout_seconds=run_timeout_sec,
                exit_code=exit_code,
                stop_reason="evidence_check_failed",
                evidence_path=evidence_path,
            )
            self._append_work_log(worker, f"Run {effective_run_id} failed evidence check.")
            raise
        if evidence_status == "warn":
            warning_suffix = f"\n\n{warning_message}"
            if len(redacted_output) + len(warning_suffix) <= _HOST_RUN_OUTPUT_MAX_CHARS:
                redacted_output = f"{redacted_output}{warning_suffix}"
            self._append_work_log(worker, f"Run {effective_run_id} completed with evidence warning.")
        if evidence_path:
            self._write_action_audit(
                worker,
                {
                    "kind": "run.evidence",
                    "run_id": effective_run_id,
                    "evidence_path": evidence_path,
                },
            )
        _write_active_run_status(
            path=heartbeat_path,
            worker=worker,
            run_id=effective_run_id,
            runtime_name=self.runtime_name,
            model=str(info.model or ""),
            state="completed",
            transcript_paths=transcript_paths,
            started_at=started_at_iso,
            process_pid=process_pid,
            timeout_seconds=run_timeout_sec,
            exit_code=exit_code,
            stop_reason="process_exit",
            evidence_path=evidence_path,
        )
        self._append_work_log(worker, f"Run {effective_run_id} completed.")
        return redacted_output

    def terminal_target(self, worker: dict) -> TerminalTarget:
        info = self.ensure_worker_ready(worker)
        active = self._infer_active_session(worker)
        stdout = str((active or {}).get("stdout_path") or "")
        command = ["bash", "-lc", f"cd {shlex.quote(str(info.workspace_dir or ''))} && tail -n 80 -f {shlex.quote(stdout)}"] if stdout else ["bash", "-lc", f"cd {shlex.quote(str(info.workspace_dir or ''))} && exec ${{SHELL:-/bin/bash}}"]
        return TerminalTarget(
            command=command,
            cwd=str(info.workspace_dir or ""),
            env={"TERM": "xterm-256color"},
            title=f"{worker['name']} host session" if active else f"{worker['name']} host terminal",
            subtitle=f"{self.runtime_name} on host computer",
        )

    def desktop_action(
        self,
        worker: dict,
        action: str,
        *,
        url: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, object]:
        info = self.ensure_worker_ready(worker)
        notes = {
            "terminal": "Host-native workers expose terminal takeover through the local terminal target.",
            "files": "Opened the host workspace in the system file browser.",
            "browser": "Opened the requested URL in the host browser.",
            "focus_browser": "Requested the host browser to open or focus.",
            "codex": "Host-native Codex runs use the installed Codex CLI on the main computer.",
            "claude": "Host-native Claude runs use the installed Claude CLI on the main computer.",
            "openclaw": "Host-native OpenClaw runs use the installed OpenClaw CLI on the main computer.",
        }
        if action == "files":
            subprocess.run(["open", str(info.workspace_dir or "")], check=False)
        elif action in {"browser", "focus_browser"} and url:
            subprocess.run(["open", url], check=False)
        self._write_action_audit(
            worker,
            {
                "kind": "desktop_action",
                "action": action,
                "url": _redact_text(url or ""),
                "cwd": str(info.workspace_dir or ""),
            },
        )
        return {
            "action": action,
            "status": "launched" if action in {"files", "browser", "focus_browser"} else "available",
            "mode": "host-computer",
            "url": url,
            "view_url": None,
            "notes": notes.get(action, "Host-native worker action recorded."),
        }

    def describe_worker(self, worker: dict) -> dict[str, object]:
        self._materialize_workspace(worker, self._host_workspace_dir(worker))
        info = self.reconcile_worker(worker)
        return {
            "mode": "host-computer",
            "runtime": self.runtime_name,
            "execution_mode": "host",
            "workspace_dir": info.workspace_dir or "",
            "state_dir": info.state_dir or "",
            "pid": info.pid,
            "host_workspace_root": str(self._host_workspace_root(worker)),
            "prompt_paths": {
                "project_definition": str(Path(info.workspace_dir or "") / "project-definition.md"),
                "work_log": str(Path(info.workspace_dir or "") / "work-log.md"),
                "harness_prompt": str(Path(info.workspace_dir or "") / "harness-prompt.md"),
                "agents_md": str(Path(info.workspace_dir or "") / "AGENTS.md"),
                "claude_md": str(Path(info.workspace_dir or "") / "CLAUDE.md"),
                "codex_md": str(Path(info.workspace_dir or "") / "CODEX.md"),
            },
        }


class HostCodexCliRuntime(HostNativeCliMixin, CodexCliRuntime):
    worker_root_name = "host_codex_cli_runtime"
    binary_env_var = "WPR_CODEX_BIN"

    def resolve_model(self, profile: str) -> str:
        if profile != "codex-cli":
            return super().resolve_model(profile)
        host_model = os.environ.get("WPR_MODEL_HOST_CODEX_CLI", "").strip()
        if host_model:
            return host_model
        codex_model = os.environ.get("CODEX_MODEL", "").strip()
        if codex_model:
            return codex_model
        inherit_provider_model = os.environ.get(
            "GLASSHIVE_HOST_CODEX_INHERIT_PROVIDER_MODEL",
            os.environ.get("WPR_HOST_CODEX_INHERIT_PROVIDER_MODEL", ""),
        ).strip().lower() in {"1", "true", "yes", "on"}
        if inherit_provider_model:
            return os.environ.get("WPR_MODEL_CODEX_CLI", "").strip()
        return ""

    def ensure_worker_ready(self, worker: dict) -> RuntimeInfo:
        info = HostNativeCliMixin.ensure_worker_ready(self, worker)
        workspace = Path(str(info.workspace_dir or ""))
        if (
            not self._conversation_mode_from_worker(worker)
            and workspace.exists()
            and not (workspace / ".git").exists()
        ):
            self._ensure_git_workspace(str(workspace))
        return info

    def _codex_reasoning_effort_for_worker(self, worker: dict) -> str:
        if not self._conversation_mode_from_worker(worker):
            return super()._codex_reasoning_effort_for_worker(worker)
        requested = (
            self._bootstrap_env_value(worker, "WPR_CODEX_CLI_REASONING_EFFORT")
            or os.environ.get("WPR_CODEX_CLI_REASONING_EFFORT", "")
            or os.environ.get("WPR_CODEX_CLI_DEFAULT_REASONING_EFFORT", "")
        ).strip().lower()
        allowed = {"low", "medium", "high", "xhigh", "max", "ultra"}
        if requested and requested not in allowed:
            raise RuntimeErrorBase(
                f"Unsupported native Codex conversation effort: {requested}"
            )
        if requested:
            worker["_effort_projection"] = {
                "requested": requested,
                "effective": requested,
                "allowed": sorted(allowed),
                "route_proven": True,
                "fallback_reason": "",
            }
        return requested

    def _conversation_primary_workspace(
        self,
        worker: dict,
        workspace: str,
    ) -> tuple[str, str]:
        if (
            not self._conversation_mode_from_worker(worker)
            or _host_codex_conversation_project_instructions() == "inherit"
        ):
            return workspace, ""
        primary = self._state_dir(str(worker["worker_id"])) / "conversation-workspace"
        primary.mkdir(parents=True, exist_ok=True)
        primary.chmod(0o700)
        return str(primary), workspace

    def _build_command(self, worker: dict, instruction: str, info: RuntimeInfo) -> tuple[list[str], dict[str, str]]:
        self._assert_host_codex_worker_policy(worker)
        existing_session = self._read_session_key(worker["worker_id"])
        model = self._codex_model_for_worker(worker, "WPR_MODEL_HOST_CODEX_CLI")
        is_resume = bool(existing_session and not existing_session.startswith("codex-worker:"))
        dangerous_mode = os.environ.get("WPR_CODEX_DANGEROUS", "1").strip().lower() in {"1", "true", "yes", "on"}
        if self._conversation_mode_from_worker(worker):
            bundle = self._bootstrap_bundle_for_worker(worker)
            dangerous_mode = str(bundle.get("access_mode") or "full").strip().lower() == "full"
        conversation_mode = self._conversation_mode_from_worker(worker)
        workspace = str(info.workspace_dir or ".")
        primary_workspace, additional_workspace = self._conversation_primary_workspace(
            worker,
            workspace,
        )
        if is_resume:
            command = [self.binary, "exec", "resume"]
            if conversation_mode:
                command.extend(["--json", "--skip-git-repo-check"])
        else:
            command = [
                self.binary,
                "exec",
                "--json",
                "--skip-git-repo-check",
                "-C",
                primary_workspace,
            ]
            if additional_workspace:
                command.extend(["--add-dir", additional_workspace])
        if model:
            if is_resume:
                command.extend(["-c", f'model="{model}"'])
            else:
                command.extend(["-m", model])
        self._append_codex_user_config_policy(command, worker)
        if validated_codex_broker_projection(worker) is not None:
            self._append_codex_compatible_provider_config(
                command,
                worker,
                include_reasoning_effort=False,
            )
        self._append_codex_reasoning_effort_config(command, worker)
        if dangerous_mode:
            if is_resume:
                command.append("--dangerously-bypass-approvals-and-sandbox")
            else:
                command.extend(["-s", "danger-full-access", "--dangerously-bypass-approvals-and-sandbox"])
        elif is_resume:
            command.extend(
                [
                    "-c",
                    'sandbox_mode="workspace-write"',
                    "-c",
                    'approval_policy="never"',
                ]
            )
        else:
            command.append("--full-auto")
        if is_resume:
            command.append(existing_session)
        command.append("-")
        env = self._host_env(worker)
        codex_home = self._host_codex_home(worker)
        if conversation_mode or (codex_home / "config.toml").exists():
            env["CODEX_HOME"] = str(codex_home)
        apply_bound_provider_account_environment(
            worker,
            env,
            runtime_name=self.runtime_name,
        )
        projection = validated_codex_broker_projection(worker)
        if projection is not None:
            for key in _CODEX_BROKER_CONFLICTING_ENV:
                env.pop(key, None)
            env["OPENAI_API_KEY"] = str(projection["grant_token"])
        return command, env


class HostClaudeCodeRuntime(HostNativeCliMixin, ClaudeCodeRuntime):
    worker_root_name = "host_claude_code_runtime"
    binary_env_var = "WPR_CLAUDE_CODE_BIN"

    def _conversation_workspace_side_effect_state(self, workspace: Path) -> dict[str, bool]:
        claude_dir = workspace / ".claude"
        marker = claude_dir / ".cc-writes"
        return {
            "claude_dir_existed": claude_dir.exists() or claude_dir.is_symlink(),
            "cc_writes_existed": marker.exists() or marker.is_symlink(),
        }

    def _cleanup_conversation_workspace_side_effects(
        self,
        workspace: Path,
        state: dict[str, bool],
    ) -> None:
        if state.get("cc_writes_existed"):
            return
        claude_dir = workspace / ".claude"
        marker = claude_dir / ".cc-writes"
        if marker.is_symlink() or marker.is_file():
            marker.unlink()
        elif marker.is_dir():
            shutil.rmtree(marker)
        if not state.get("claude_dir_existed"):
            try:
                claude_dir.rmdir()
            except FileNotFoundError:
                pass

    def _chrome_supported(self) -> bool:
        return self._help_supports("--chrome")

    def _effort_supported(self, effort: str = "") -> bool:
        help_text = self._help_text()
        return bool(help_text) and _claude_effort_help_supports(help_text, effort)

    def _help_text(self) -> str:
        resolved = shutil.which(self.binary)
        if not resolved:
            return ""
        try:
            completed = subprocess.run(
                [resolved, "--help"],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5,
            )
        except Exception:
            return ""
        return f"{completed.stdout}\n{completed.stderr}"

    def _help_supports(self, flag: str) -> bool:
        return flag in self._help_text()

    def _requires_max_effort(self, worker: dict | None = None) -> bool:
        worker = worker or {}
        effort = (
            self._bootstrap_env_value(worker, "WPR_CLAUDE_CODE_EFFORT")
            or os.environ.get("WPR_CLAUDE_CODE_EFFORT", "")
        ).strip().lower()
        return effort == "max"

    def _raise_missing_effort_support(self, profile: str, execution_mode: str, effort: str) -> None:
        raise RuntimeDependencyMissingError(
            f"Claude Code workers requested `{effort}` effort, but the configured Claude Code CLI "
            "does not advertise that native --effort capability.",
            binary=self.binary,
            runtime_name=self.runtime_name,
            profile=profile,
            execution_mode=execution_mode,
            dependency_label="Claude Code",
            recovery_hint=(
                "Update Claude Code to a version with native --effort support, or use default "
                "Claude effort only when that lower-effort mode is intended."
            ),
        )

    def preflight_worker_profile(self, profile: str, execution_mode: str = "host") -> None:
        super().preflight_worker_profile(profile, execution_mode)
        effort = os.environ.get("WPR_CLAUDE_CODE_EFFORT", "").strip().lower()
        if effort and effort != "default" and not self._effort_supported(effort):
            self._raise_missing_effort_support(profile, execution_mode, effort)
        if self._chrome_enabled() and not self._chrome_supported():
            raise RuntimeDependencyMissingError(
                "Claude Code host workers require a Claude Code CLI that supports --chrome, "
                "or WPR_CLAUDE_CODE_ENABLE_CHROME=0 for an explicit locked-down launch.",
                binary=self.binary,
                runtime_name=self.runtime_name,
                profile=profile,
                execution_mode=execution_mode,
                dependency_label="Claude Code",
                recovery_hint=(
                    "Update Claude Code to a version with Chrome integration, or explicitly disable "
                    "host Claude Chrome support only when that locked-down mode is intended."
                ),
            )

    def _inject_private_subscription_auth(self, env: dict[str, str]) -> None:
        """Give an isolated Claude config the host subscription without copying user config."""
        def read_macos_keychain_oauth() -> dict[str, object]:
            if sys.platform != "darwin" or not shutil.which("security"):
                return {}
            completed = subprocess.run(
                [
                    "security",
                    "find-generic-password",
                    "-s",
                    "Claude Code-credentials",
                    "-w",
                ],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=5,
            )
            credential = json.loads(completed.stdout) if completed.returncode == 0 else {}
            oauth = credential.get("claudeAiOauth") if isinstance(credential, dict) else {}
            return oauth if isinstance(oauth, dict) else {}

        def unpack_oauth(oauth: dict[str, object]) -> tuple[str, str, str, int]:
            raw_scopes = oauth.get("scopes")
            if isinstance(raw_scopes, list):
                scope_text = " ".join(
                    str(scope).strip() for scope in raw_scopes if str(scope).strip()
                )
            else:
                scope_text = str(raw_scopes or "").strip()
            try:
                expiry = int(oauth.get("expiresAt") or 0)
            except (TypeError, ValueError):
                expiry = 0
            return (
                str(oauth.get("accessToken") or "").strip(),
                str(oauth.get("refreshToken") or "").strip(),
                scope_text,
                expiry,
            )

        access_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
        refresh_token = os.environ.get("CLAUDE_CODE_OAUTH_REFRESH_TOKEN", "").strip()
        scopes = os.environ.get("CLAUDE_CODE_OAUTH_SCOPES", "").strip()
        expires_at_ms = 0
        explicit_auth = bool(access_token or refresh_token)
        if not access_token and sys.platform == "darwin" and shutil.which("security"):
            try:
                access_token, refresh_token, scopes, expires_at_ms = unpack_oauth(
                    read_macos_keychain_oauth()
                )
            except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
                return
        token_expires_soon = bool(
            access_token
            and expires_at_ms
            and expires_at_ms <= int(time.time() * 1000) + 300_000
        )
        if token_expires_soon and refresh_token and scopes and not explicit_auth:
            login_env = dict(env)
            # Refresh the canonical host credential in place. Never rotate the host refresh token
            # into a one-off worker-specific Keychain service.
            login_env.pop("CLAUDE_CONFIG_DIR", None)
            login_env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
            login_env["CLAUDE_CODE_OAUTH_REFRESH_TOKEN"] = refresh_token
            login_env["CLAUDE_CODE_OAUTH_SCOPES"] = scopes
            try:
                with _CLAUDE_AUTH_REFRESH_LOCK:
                    completed = subprocess.run(
                        [self.binary, "auth", "login"],
                        check=False,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        env=login_env,
                        timeout=30,
                    )
            except (OSError, subprocess.TimeoutExpired):
                completed = None
            if completed is None or completed.returncode != 0:
                raise RuntimeErrorBase(
                    "Claude Code isolated worker authentication refresh failed; run `claude auth login` "
                    "on the GlassHive host or configure a long-lived CLAUDE_CODE_OAUTH_TOKEN."
                )
            try:
                access_token, refresh_token, scopes, expires_at_ms = unpack_oauth(
                    read_macos_keychain_oauth()
                )
            except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
                raise RuntimeErrorBase(
                    "Claude Code refreshed the host login but GlassHive could not read the refreshed credential."
                ) from exc
            if not access_token or (
                expires_at_ms and expires_at_ms <= int(time.time() * 1000) + 60_000
            ):
                raise RuntimeErrorBase(
                    "Claude Code host authentication refresh did not produce a usable access token."
                )
        if access_token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = access_token
        if refresh_token:
            env["CLAUDE_CODE_OAUTH_REFRESH_TOKEN"] = refresh_token
        if scopes:
            env["CLAUDE_CODE_OAUTH_SCOPES"] = scopes

    def _build_command(self, worker: dict, instruction: str, info: RuntimeInfo) -> tuple[list[str], dict[str, str]]:
        session_key = self._read_session_key(worker["worker_id"])
        model = self._provider_model_for_worker(worker)
        permission_mode = os.environ.get("WPR_CLAUDE_CODE_PERMISSION_MODE", "bypassPermissions")
        if self._conversation_mode_from_worker(worker):
            bundle = self._bootstrap_bundle_for_worker(worker)
            permission_mode = (
                "bypassPermissions"
                if str(bundle.get("access_mode") or "full").strip().lower() == "full"
                else "acceptEdits"
            )
        output_format = "stream-json" if self._conversation_mode_from_worker(worker) else "json"
        command = [
            self.binary,
            "-p",
            "--permission-mode",
            permission_mode,
            "--output-format",
            output_format,
            "--model",
            model,
        ]
        settings: dict[str, object] = {}
        denied_plugins = self._host_plugin_denylist()
        if denied_plugins:
            settings["enabledPlugins"] = {
                plugin_id: False for plugin_id in denied_plugins
            }
        if self._conversation_mode_from_worker(worker):
            command.extend(["--verbose", "--include-partial-messages"])
            developer_instructions = str(
                bundle.get("developer_instructions") or ""
            ).strip()
            if developer_instructions:
                # Claude Code exposes request-time application authority through its native
                # system-prompt channel. Keep it private and out of both process argv and the
                # user-authored visible history.
                authority_path = (
                    self._state_dir(str(worker["worker_id"]))
                    / "conversation-developer-instructions.md"
                )
                expected = developer_instructions + "\n"
                try:
                    actual = authority_path.read_text()
                except OSError as exc:
                    raise RuntimeErrorBase(
                        "Claude Code conversation developer instruction authority is unavailable"
                    ) from exc
                if actual != expected:
                    raise RuntimeErrorBase(
                        "Claude Code conversation developer instruction authority is stale"
                    )
                command.extend(["--append-system-prompt-file", str(authority_path)])
            mcp_path = self._state_dir(str(worker["worker_id"])) / "conversation-mcp.json"
            if mcp_path.is_file():
                command.extend(["--mcp-config", str(mcp_path), "--strict-mcp-config"])
            bundle = self._bootstrap_bundle_for_worker(worker)
            if str(bundle.get("access_mode") or "full").strip().lower() == "workspace":
                workspace = str(Path(str(info.workspace_dir or ".")).resolve())
                settings.update(
                    {
                        "permissions": {"defaultMode": "acceptEdits"},
                        "sandbox": {
                            "enabled": True,
                            "failIfUnavailable": True,
                            "allowUnsandboxedCommands": False,
                            "filesystem": {
                                "denyRead": [str(Path.home().resolve())],
                                "allowRead": [workspace],
                            },
                        },
                    }
                )
        if settings:
            command.extend(
                ["--settings", json.dumps(settings, separators=(",", ":"))]
            )
        if self._chrome_enabled():
            command.insert(2, "--chrome")
        effort = (
            self._bootstrap_env_value(worker, "WPR_CLAUDE_CODE_EFFORT")
            or os.environ.get("WPR_CLAUDE_CODE_EFFORT", "")
        ).strip().lower()
        if effort and effort != "default":
            if not self._effort_supported(effort):
                self._raise_missing_effort_support(
                    str(worker.get("profile") or "claude-code"), "host", effort
                )
            command.extend(["--effort", effort])
        if session_key and not session_key.startswith("claude-worker:"):
            command.extend(["--resume", session_key])
        env = self._host_env(worker)
        if self._conversation_mode_from_worker(worker):
            env["CLAUDE_CONFIG_DIR"] = str(
                self._home_dir(str(worker["worker_id"])) / ".claude"
            )
            self._inject_private_subscription_auth(env)
        use_api_key = os.environ.get("WPR_CLAUDE_CODE_USE_API_KEY", "0").strip().lower() in {"1", "true", "yes", "on"}
        if not use_api_key:
            env.pop("ANTHROPIC_API_KEY", None)
        self._remove_conflicting_anthropic_credentials(env)
        apply_bound_provider_account_environment(
            worker,
            env,
            runtime_name=self.runtime_name,
        )
        return command, env


class HostOpenClawRuntime(HostNativeCliMixin, OpenClawWorkstationRuntime):
    worker_root_name = "host_openclaw_runtime"
    binary_env_var = "WPR_OPENCLAW_BIN"
    binary_name = "openclaw"

    def _build_command(self, worker: dict, instruction: str, info: RuntimeInfo) -> tuple[list[str], dict[str, str]]:
        session_id = info.session_key or self._default_session_key(worker) or f"agent:main:wpr:worker:{worker['worker_id']}"
        model = worker.get("model") or self.resolve_model(worker.get("profile", "openclaw-general"))
        state_dir = self._state_dir(worker["worker_id"]) / "openclaw"
        state_dir.mkdir(parents=True, exist_ok=True)
        config_path = state_dir / "openclaw.json"
        config_path.write_text(
            json.dumps(
                {
                    "agents": {
                        "defaults": {
                            "model": {"primary": model},
                            "cliBackends": {
                                "claude-cli": {"command": "claude"},
                                "codex-cli": {"command": "codex"},
                            },
                            "sandbox": {"mode": "off"},
                        }
                    },
                    "session": {"dmScope": "per-channel-peer"},
                    "tools": {
                        "fs": {"workspaceOnly": False},
                        "exec": {"applyPatch": {"workspaceOnly": False}},
                        "elevated": {"enabled": True},
                    },
                    "plugins": {"enabled": True},
                },
                indent=2,
            )
        )
        config_path.chmod(0o600)
        env = reviewed_openclaw_env(self._host_env(worker))
        env["OPENCLAW_STATE_DIR"] = str(state_dir)
        env["OPENCLAW_CONFIG_PATH"] = str(config_path)
        env["OPENCLAW_MODEL"] = model
        env["OPENCLAW_SESSION_ID"] = session_id
        run_id = str(worker.get("_active_run_id") or "").strip()
        instruction_path = (
            self._run_root(worker["worker_id"], run_id) / "instruction.stdin"
            if run_id
            else state_dir / "latest-instruction.stdin"
        )
        return [
            self.binary,
            "agent",
            "--local",
            "--session-id",
            session_id,
            "-m",
            _instruction_file_pointer_message(str(instruction_path)),
            "--json",
        ], env
