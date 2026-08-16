from __future__ import annotations

import json
import base64
import fcntl
import hashlib
import logging
import math
import os
import re
import secrets
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from urllib.parse import urlsplit
try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 compatibility
    import tomli as tomllib
from datetime import datetime, timezone
from pathlib import Path
from threading import Event, Lock, Thread

from .agent_builder_control import graph_transfer_output_schema
from .bootstrap import (
    GLASSHIVE_CRITICAL_OPERATING_INSTRUCTIONS,
    GLASSHIVE_NATIVE_CAPABILITY_INVENTORY,
    GLASSHIVE_SAFETY_CHECKPOINT_RULE,
    GLASSHIVE_WORKER_COMPLETION_CONTRACT,
    PARALLEL_CLEAN_ROOM_EXECUTION_POLICY,
    bootstrap_bundle_for,
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
from .durable_capture import DurableSecretScrubber, scrub_durable_text_artifacts
from .failure_classification import classify_cli_failure, classify_runtime_error
from .native_team import project_native_events
from .openclaw_runtime import (
    HostCapacityError,
    ProviderRateLimitError,
    RuntimeErrorBase,
    RuntimeDependencyMissingError,
    RuntimeInfo,
    RunStartupRejectedError,
    WorkerInterruptedError,
    WorkerPausedError,
    WorkerRuntime,
    WorkerTerminatedError,
    _PROVIDER_ENV_KEYS,
)
from .runtime_requirements import host_runtime_requirement_issue
from .run_evidence import (
    FINAL_REPORT_PATTERN,
    build_constraint_ledger,
    build_run_evidence,
    write_constraint_ledger,
    write_run_evidence,
)
from .terminal_takeover import TerminalTarget


logger = logging.getLogger(__name__)


def _drain_scrubbed_provider_stream(
    stream: object,
    destination: object,
    scrubber: DurableSecretScrubber,
    errors: list[BaseException],
) -> None:
    """Copy one provider pipe into a durable transcript after structural scrubbing."""

    try:
        for line in stream:  # type: ignore[union-attr]
            destination.write(scrubber.scrub_text(str(line)))  # type: ignore[union-attr]
            destination.flush()  # type: ignore[union-attr]
    except BaseException as exc:  # pragma: no cover - defensive pipe/filesystem boundary
        errors.append(exc)
    finally:
        try:
            stream.close()  # type: ignore[union-attr]
        except (AttributeError, OSError):
            pass


def _scrub_provider_owned_artifacts(
    *,
    state_dir: Path,
    home_dir: Path,
    run_root: Path,
    workspace: Path,
    scrubber: DurableSecretScrubber,
) -> None:
    # Deliberately exclude the worker workspace: it can contain user-authored
    # files and deliverables whose contents GlassHive must never rewrite.
    roots = [state_dir, home_dir, run_root]
    harness_workspace = workspace / "glasshive-run"
    if harness_workspace.exists():
        roots.append(harness_workspace)
    scrub_durable_text_artifacts(roots, scrubber=scrubber)


def _provider_process_exit_error(
    *,
    runtime_name: str,
    exit_code: int,
    stdout: str,
    stderr: str,
    message: str,
) -> RuntimeErrorBase:
    classification = classify_cli_failure(
        stdout=stdout,
        stderr=stderr,
        runtime_name=runtime_name,
        exit_code=exit_code,
    )
    if (
        classification.failure_class == "provider_rate_limited"
        and classification.retry_after_s is not None
    ):
        return ProviderRateLimitError(
            message,
            retry_after_s=classification.retry_after_s,
        )
    error = RuntimeErrorBase(message)
    # Preserve the structured provider classification across the process-exit boundary. Downstream
    # recovery must not infer authentication state from localized CLI prose.
    if classification.structured:
        error.failure_class = classification.failure_class
        error.failure_retryable = classification.retryable
    return error

_CODEX_MCP_SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")
_HOST_CODEX_NATIVE_MCP_ALLOWLIST = ("computer-use", "node_repl")
_CODEX_NATIVE_WEB_LOCKDOWN_FEATURES = (
    "apps",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "computer_use",
    "in_app_browser",
    "plugins",
    "remote_plugin",
)
_FALSEY_ENV_VALUES = {"0", "false", "no", "off", "none", "disabled"}
_CLAUDE_KEYCHAIN_SERVICE = "Claude Code-credentials"
_CLAUDE_ACCESS_TOKEN_EXPIRY_BUFFER_MS = 60_000


def _usable_claude_oauth_token(value: object) -> str:
    token = str(value or "").strip()
    if not token or token == "user_provided" or "${" in token:
        return ""
    return token


def _usable_explicit_claude_oauth_token() -> str:
    return _usable_claude_oauth_token(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))


def _read_claude_keychain_oauth() -> dict[str, object]:
    if sys.platform != "darwin" or not shutil.which("security"):
        return {}
    try:
        completed = subprocess.run(
            ["security", "find-generic-password", "-s", _CLAUDE_KEYCHAIN_SERVICE, "-w"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
        credential = json.loads(completed.stdout) if completed.returncode == 0 else {}
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return {}
    oauth = credential.get("claudeAiOauth") if isinstance(credential, dict) else {}
    return oauth if isinstance(oauth, dict) else {}


def _claude_keychain_access_token_is_fresh(
    oauth: dict[str, object], *, now_ms: int | None = None
) -> bool:
    access_token = str(oauth.get("accessToken") or "").strip()
    if not access_token:
        return False
    try:
        expires_at = float(oauth.get("expiresAt"))
    except (TypeError, ValueError):
        return False
    if not expires_at or not math.isfinite(expires_at):
        return False
    current_ms = int(time.time() * 1000) if now_ms is None else now_ms
    return expires_at > current_ms + _CLAUDE_ACCESS_TOKEN_EXPIRY_BUFFER_MS


def _claude_cli_managed_auth_available(
    binary: str, *, child_env: dict[str, str] | None
) -> bool:
    if child_env is None:
        return False
    resolved_binary = str(binary or "").strip()
    if not resolved_binary:
        return False
    if not Path(resolved_binary).is_absolute():
        resolved_binary = str(shutil.which(resolved_binary) or "")
    if not resolved_binary:
        return False
    try:
        completed = subprocess.run(
            [resolved_binary, "auth", "status"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
            env=dict(child_env),
        )
        payload = json.loads(completed.stdout) if completed.returncode == 0 else {}
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("loggedIn") is True


def _claude_host_auth_available(
    binary: str, *, child_env: dict[str, str] | None = None
) -> bool:
    if child_env and _usable_claude_oauth_token(child_env.get("CLAUDE_CODE_OAUTH_TOKEN")):
        return True
    if _usable_explicit_claude_oauth_token():
        return True
    keychain_oauth = _read_claude_keychain_oauth()
    if _claude_keychain_access_token_is_fresh(keychain_oauth):
        return True
    return _claude_cli_managed_auth_available(binary, child_env=child_env)


def _atomic_write_private_text(path: Path, text: str) -> None:
    """Publish private runtime state without exposing partial cross-process reads."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        handle = os.fdopen(descriptor, "w")
        descriptor = -1  # fdopen owns the descriptor from this point onward.
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
    except BaseException:
        try:
            if descriptor >= 0:
                os.close(descriptor)
        except OSError:
            pass
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


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


def _host_native_web_access() -> str:
    mode = (
        os.environ.get("WPR_HOST_NATIVE_WEB_ACCESS", "").strip()
        or os.environ.get("GLASSHIVE_HOST_NATIVE_WEB_ACCESS", "inherit").strip()
    ).lower()
    if mode not in {"inherit", "disabled"}:
        raise RuntimeErrorBase(
            "Host native web access must be inherit or disabled"
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


def _apply_codex_plugin_denylist(config_text: str, plugin_ids: tuple[str, ...]) -> str:
    if not plugin_ids:
        return config_text.strip()
    try:
        parsed = tomllib.loads(config_text) if config_text.strip() else {}
    except Exception as exc:
        raise RuntimeErrorBase(
            "Cannot apply the host plugin denylist because the worker Codex config is invalid"
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
    requires_run_start_identity = True

    def __init__(self, base_dir: str | None = None) -> None:
        self.openclaw = OpenClawWorkstationRuntime(base_dir=base_dir)
        self.codex = CodexCliRuntime(base_dir=base_dir)
        self.claude = ClaudeCodeRuntime(base_dir=base_dir)
        self.host_openclaw = HostOpenClawRuntime(base_dir=base_dir)
        self.host_codex = HostCodexCliRuntime(base_dir=base_dir)
        self.host_claude = HostClaudeCodeRuntime(base_dir=base_dir)
        self._provider_log_cache: dict[tuple[str, str], dict[str, object]] = {}
        self._provider_log_cache_lock = Lock()
        self._isolated_readiness_cache: tuple[float, dict[str, object]] | None = None
        self._isolated_readiness_lock = Lock()

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

    def ensure_worker_ready(self, worker: dict) -> RuntimeInfo:
        return self._runtime_for_worker(worker).ensure_worker_ready(worker)

    def prepare_run_authority_context(
        self, worker: dict, run_id: str | None = None
    ) -> dict[str, str]:
        """Return an exact live container generation before Core mints a run bearer."""

        if (
            str(worker.get("execution_mode") or "docker") != "docker"
            or str(bootstrap_bundle_for(worker).get("execution_policy") or "").strip()
            != PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
        ):
            raise RuntimeErrorBase(
                "Run-local capability authority requires the Parallel clean-room policy"
            )
        runtime = self._runtime_for_worker(worker)
        runtime.ensure_worker_ready(worker)
        sandbox_manager = getattr(runtime, "sandbox", None)
        if sandbox_manager is None:
            raise RuntimeErrorBase(
                "The exact mission container generation is unavailable"
            )
        inspection = sandbox_manager.inspect_fresh(str(worker.get("worker_id") or ""))
        sandbox = inspection.sandbox
        matches_policy = getattr(
            sandbox_manager, "_sandbox_matches_parallel_clean_room_policy", None
        )
        if (
            inspection.status != "present"
            or sandbox is None
            or str(sandbox.state or "").lower() != "running"
            or not callable(matches_policy)
            or not matches_policy(sandbox)
        ):
            raise RuntimeErrorBase(
                "The exact mission container generation is unavailable"
            )
        container_id = str(sandbox.container_id or "").strip()
        if not re.fullmatch(r"[a-f0-9]{64}", container_id):
            raise RuntimeErrorBase(
                "The exact mission container generation is unavailable"
            )
        return {"container_generation_id": container_id}

    def repair_parallel_clean_room_mission_networks(self) -> tuple[str, ...]:
        return self.codex.sandbox.repair_parallel_clean_room_mission_networks()

    def pause_worker(self, worker: dict) -> RuntimeInfo:
        return self._runtime_for_worker(worker).pause_worker(worker)

    def terminate_worker(self, worker: dict) -> RuntimeInfo:
        return self._runtime_for_worker(worker).terminate_worker(worker)

    def compute_identity(self, worker: dict) -> dict[str, str]:
        runtime = self._runtime_for_worker(worker)
        if str(worker.get("execution_mode") or "docker") != "docker":
            return {"container_id": ""}
        sandbox = getattr(runtime, "sandbox", None)
        if sandbox is None:
            return {"container_id": ""}
        worker_id = str(worker.get("worker_id") or "")
        inspect_fresh = getattr(sandbox, "inspect_fresh", None)
        if callable(inspect_fresh):
            inspection = inspect_fresh(worker_id)
            status = str(getattr(inspection, "status", "") or "")
            if status == "unavailable":
                raise RuntimeErrorBase(
                    "The exact Docker generation could not be inspected"
                )
            inspected = (
                getattr(inspection, "sandbox", None)
                if status == "present"
                else None
            )
        else:
            inspected = sandbox.inspect(worker_id)
        return {
            "container_id": str(
                getattr(inspected, "container_id", None) if inspected else ""
            ).strip()
        }

    def interrupt_worker(self, worker: dict, run_id: str | None = None) -> RuntimeInfo:
        runtime = self._runtime_for_worker(worker)
        if hasattr(runtime, "interrupt_worker"):
            try:
                return runtime.interrupt_worker(worker, run_id=run_id)
            except TypeError as exc:
                if "run_id" not in str(exc):
                    raise
                return runtime.interrupt_worker(worker)
        return runtime.pause_worker(worker)

    def run_task(self, worker: dict, instruction: str, timeout_sec: float | None = None, run_id: str | None = None) -> str:
        return self._runtime_for_worker(worker).run_task(worker, instruction, timeout_sec=timeout_sec, run_id=run_id)

    def clear_run_local_capability_grant(self, worker: dict) -> None:
        cleaner = getattr(
            self._runtime_for_worker(worker),
            "clear_run_local_capability_grant",
            None,
        )
        if callable(cleaner):
            cleaner(worker)

    def worker_capacity_error(self, worker: dict) -> RuntimeErrorBase | None:
        runtime = self._runtime_for_worker(worker)
        checker = getattr(runtime, "worker_capacity_error", None)
        if callable(checker):
            return checker(worker)
        return None

    def set_host_process_observer(self, observer) -> None:
        # The persisted admission ledger now bounds both host-native and
        # Docker mission roots. Docker therefore needs the same exact-run
        # process observation used to retain/reconcile a lease after restart.
        for runtime in (
            self.openclaw,
            self.codex,
            self.claude,
            self.host_openclaw,
            self.host_codex,
            self.host_claude,
        ):
            setter = getattr(runtime, "set_host_process_observer", None)
            if callable(setter):
                setter(observer)

    def set_run_start_observer(self, observer) -> None:
        for runtime in (
            self.openclaw,
            self.codex,
            self.claude,
            self.host_openclaw,
            self.host_codex,
            self.host_claude,
        ):
            setter = getattr(runtime, "set_run_start_observer", None)
            if callable(setter):
                setter(observer)

    def set_native_event_observer(self, observer) -> None:
        for runtime in (
            self.codex,
            self.claude,
            self.host_openclaw,
            self.host_codex,
            self.host_claude,
        ):
            setter = getattr(runtime, "set_native_event_observer", None)
            if callable(setter):
                setter(observer)

    def host_process_identity(self, worker: dict, run_id: str) -> dict[str, object] | None:
        runtime = self._runtime_for_worker(worker)
        reader = getattr(runtime, "host_process_identity", None)
        if not callable(reader):
            return None
        return reader(worker, run_id)

    def host_process_absence(self, worker: dict, run_id: str) -> bool:
        runtime = self._runtime_for_worker(worker)
        reader = getattr(runtime, "host_process_absence", None)
        if not callable(reader):
            return False
        return reader(worker, run_id) is True

    def host_active_process_status(self, worker: dict) -> dict[str, object]:
        runtime = self._runtime_for_worker(worker)
        reader = getattr(runtime, "host_active_process_status", None)
        if not callable(reader):
            return {"state": "uncertain"}
        return reader(worker)

    def isolated_parallel_readiness(
        self, *, cached_only: bool = False
    ) -> dict[str, object]:
        """Bounded read-only proof that the Docker execution substrate exists.

        This deliberately never builds or starts an image. Authoritative worker
        creation still performs its full profile-specific preflight.
        """

        with self._isolated_readiness_lock:
            cached = self._isolated_readiness_cache
        if cached and cached[0] + 30.0 > time.monotonic():
            return dict(cached[1])
        if cached_only:
            return {"ready": False, "reason": "docker_readiness_snapshot_unavailable"}
        return self.refresh_isolated_parallel_readiness()

    def refresh_isolated_parallel_readiness(self) -> dict[str, object]:
        def store_result(result: dict[str, object]) -> dict[str, object]:
            with self._isolated_readiness_lock:
                self._isolated_readiness_cache = (time.monotonic(), result)
            return dict(result)

        sandbox = getattr(self.codex, "sandbox", None)
        if sandbox is None:
            return store_result(
                {"ready": False, "reason": "docker_runtime_unavailable"}
            )
        try:
            daemon = sandbox._docker(
                ["info", "--format", "{{.ServerVersion}}"],
                check=False,
                capture_output=True,
                timeout_sec=2,
            )
            if daemon.returncode != 0:
                return store_result(
                    {"ready": False, "reason": "docker_unavailable"}
                )
            image = sandbox._docker(
                ["image", "inspect", str(sandbox.image)],
                check=False,
                capture_output=True,
                timeout_sec=2,
            )
            if image.returncode != 0:
                return store_result(
                    {"ready": False, "reason": "workspace_image_unavailable"}
                )
        except Exception:
            return store_result(
                {"ready": False, "reason": "docker_probe_unavailable"}
            )

        policy_probe = getattr(sandbox, "parallel_clean_room_readiness", None)
        if not callable(policy_probe):
            return store_result(
                {
                    "ready": False,
                    "reason": "parallel_clean_room_policy_probe_unavailable",
                }
            )
        try:
            policy_result = policy_probe()
        except Exception:
            return store_result(
                {
                    "ready": False,
                    "reason": "parallel_clean_room_policy_probe_unavailable",
                }
            )
        if not isinstance(policy_result, dict) or policy_result.get("ready") is not True:
            result = (
                dict(policy_result)
                if isinstance(policy_result, dict)
                else {
                    "ready": False,
                    "reason": "parallel_clean_room_policy_probe_unavailable",
                }
            )
            result["ready"] = False
            return store_result(result)

        resource_usage = self.isolated_resource_usage(cached_only=False)
        if not (
            resource_usage.get("process_probe_ok")
            and resource_usage.get("memory_probe_ok")
            and resource_usage.get("disk_probe_ok")
        ):
            result = {
                **policy_result,
                "ready": False,
                "reason": "docker_resource_probe_unavailable",
            }
        else:
            result = dict(policy_result)
        return store_result(result)

    def isolated_resource_usage(
        self, *, cached_only: bool = False
    ) -> dict[str, object]:
        usage = (
            self.codex.sandbox.cached_resource_usage(max_age_seconds=30.0)
            if cached_only
            else self.codex.sandbox.resource_usage()
        )
        if usage is None:
            return {
                "child_processes": 0,
                "threads": 0,
                "available_memory_bytes": 0,
                "available_disk_bytes": 0,
                "running_worker_containers": 0,
                "running_worker_ids": [],
                "process_probe_ok": False,
                "memory_probe_ok": False,
                "disk_probe_ok": False,
            }
        return {
            "child_processes": usage.child_processes,
            "threads": usage.threads,
            "available_memory_bytes": usage.available_memory_bytes,
            "available_disk_bytes": usage.available_disk_bytes,
            "running_worker_containers": usage.running_worker_containers,
            "running_worker_ids": list(usage.running_worker_ids),
            "worker_process_counts": {
                worker_id: {
                    "child_processes": child_processes,
                    "threads": threads,
                }
                for worker_id, child_processes, threads in usage.worker_process_counts
            },
            "process_probe_ok": usage.process_probe_ok,
            "memory_probe_ok": usage.memory_probe_ok,
            "disk_probe_ok": usage.disk_probe_ok,
        }

    def reconcile_worker(self, worker: dict) -> RuntimeInfo:
        return self._runtime_for_worker(worker).reconcile_worker(worker)

    def cleanup_orphaned_run(self, worker: dict, run_id: str) -> RuntimeInfo | None:
        runtime = self._runtime_for_worker(worker)
        cleanup = getattr(runtime, "cleanup_orphaned_run", None)
        if not callable(cleanup):
            return None
        return cleanup(worker, run_id)

    def cleanup_unconfirmed_run_start(
        self,
        worker: dict,
        run_id: str,
        lease_identity: dict[str, object],
    ) -> bool:
        runtime = self._runtime_for_worker(worker)
        cleanup = getattr(runtime, "cleanup_unconfirmed_run_start", None)
        if not callable(cleanup):
            return False
        return bool(cleanup(worker, run_id, lease_identity))

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

    def provider_citation_sources(self, worker: dict, run_id: str) -> list[dict[str, str]]:
        """Return structured public provenance retained by the native runtime."""

        runtime = self._runtime_for_worker(worker)
        collector = getattr(runtime, "provider_citation_sources", None)
        if not callable(collector):
            return []
        sources = collector(worker, run_id)
        return [dict(source) for source in sources if isinstance(source, dict)]

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
    requires_run_start_identity = True
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
        self._host_process_observer = None
        self._run_start_observer = None
        self._native_event_observer = None
        self.sandbox = DockerSandboxManager(base_dir=str(self.base_dir))

    def resolve_model(self, profile: str) -> str:
        raise NotImplementedError

    def _agent_type(self) -> str:
        if self.runtime_name == "codex-cli":
            return "codex"
        if self.runtime_name == "claude-code":
            return "claude"
        return "openclaw"

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

    @contextmanager
    def _active_session_file_lock(self, worker_id: str):
        lock_path = self._state_dir(worker_id) / "active_terminal_session.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+") as handle:
            lock_path.chmod(0o600)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _run_root(self, worker_id: str, run_id: str) -> Path:
        return self._home_dir(worker_id) / ".glasshive-runs" / run_id

    def _container_run_root(self, run_id: str) -> str:
        return f"{self.sandbox.home_mount}/.glasshive-runs/{run_id}"

    def _ensure_dirs(self, worker_id: str) -> None:
        self._workspace_dir(worker_id).mkdir(parents=True, exist_ok=True)
        self._home_dir(worker_id).mkdir(parents=True, exist_ok=True)

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
        _atomic_write_private_text(path, json.dumps({"session_key": session_key}, indent=2))

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
            "process_group": data.get("process_group"),
            "process_start_identity": str(data.get("process_start_identity") or "").strip(),
            "lease_pid": data.get("lease_pid"),
            "lease_process_group": data.get("lease_process_group"),
            "lease_process_start_identity": str(
                data.get("lease_process_start_identity") or ""
            ).strip(),
            "owner_pid": data.get("owner_pid"),
            "heartbeat_path": str(data.get("heartbeat_path") or "").strip(),
            "timeout_seconds": data.get("timeout_seconds"),
            "run_mode": str(data.get("run_mode") or "").strip(),
            "native_session_id": str(data.get("native_session_id") or "").strip(),
            "instruction_redacted": bool(data.get("instruction_redacted")),
            "termination_unconfirmed": bool(data.get("termination_unconfirmed")),
            "container_id": str(data.get("container_id") or "").strip(),
            "startup_token_digest": str(
                data.get("startup_token_digest") or ""
            ).strip(),
        }

    def _write_active_session(
        self,
        worker_id: str,
        payload: dict[str, object],
        *,
        expected_session: dict[str, object] | None = None,
        publish_run_start: bool = False,
        worker: dict | None = None,
        spawned_process: subprocess.Popen[str] | None = None,
    ) -> bool:
        path = self._active_session_meta_path(worker_id)
        safe_payload = dict(payload)
        if publish_run_start and isinstance(worker, dict):
            token_digest = str(
                worker.get("_run_startup_token_digest") or ""
            ).strip()
            if re.fullmatch(r"[0-9a-f]{64}", token_digest):
                safe_payload["startup_token_digest"] = token_digest
        if "instruction" in safe_payload:
            safe_payload.pop("instruction", None)
            safe_payload["instruction_redacted"] = True
        with self._active_session_file_lock(worker_id):
            if expected_session is not None:
                current = self._read_active_session(worker_id)
                if self._active_session_fingerprint(
                    current
                ) != self._active_session_fingerprint(expected_session):
                    return False
            _atomic_write_private_text(path, json.dumps(safe_payload, indent=2))
        observer = self._host_process_observer
        if callable(observer):
            try:
                pid = int(
                    safe_payload.get("lease_pid")
                    or safe_payload.get("process_pid")
                    or 0
                )
                process_group = int(
                    safe_payload.get("lease_process_group")
                    or safe_payload.get("process_group")
                    or 0
                )
            except (TypeError, ValueError):
                pid = 0
                process_group = 0
            run_id = str(safe_payload.get("run_id") or "").strip()
            start_identity = str(
                safe_payload.get("lease_process_start_identity")
                or safe_payload.get("process_start_identity")
                or ""
            ).strip()
            if run_id and pid > 0 and start_identity:
                container_id = str(safe_payload.get("container_id") or "").strip()
                session_id = str(safe_payload.get("session_name") or "").strip()
                try:
                    observer(
                        {
                            "worker_id": worker_id,
                            "run_id": run_id,
                            "identity_kind": (
                                "docker_session" if container_id else "host_process"
                            ),
                            "pid": pid,
                            "process_group": process_group or pid,
                            "process_start_identity": start_identity,
                            "container_id": container_id,
                            "session_id": session_id,
                        }
                    )
                except Exception:
                    logger.exception(
                        "Host process observer failed for worker %s run %s",
                        worker_id,
                        run_id,
                    )
        if publish_run_start:
            durable_session = self._read_active_session(worker_id)
            if self._active_session_fingerprint(
                durable_session
            ) != self._active_session_fingerprint(safe_payload):
                raise RunStartupRejectedError(
                    "The durable run startup identity changed before publication.",
                    termination_confirmed=False,
                )
            start_observer = self._run_start_observer
            if not callable(start_observer):
                self._cleanup_rejected_run_start(
                    worker_id=worker_id,
                    expected_session=durable_session,
                    expected_container_id=str(
                        durable_session.get("container_id") or ""
                    ).strip(),
                    spawned_process=spawned_process,
                    worker=worker,
                )
                raise RunStartupRejectedError(
                    "No durable run startup observer is configured.",
                    termination_confirmed=True,
                )
            assert durable_session is not None
            container_id = str(durable_session.get("container_id") or "").strip()
            try:
                pid = int(
                    durable_session.get("lease_pid")
                    or durable_session.get("process_pid")
                    or 0
                )
                process_group = int(
                    durable_session.get("lease_process_group")
                    or durable_session.get("process_group")
                    or pid
                    or 0
                )
            except (TypeError, ValueError):
                pid = 0
                process_group = 0
            start_identity = str(
                durable_session.get("lease_process_start_identity")
                or durable_session.get("process_start_identity")
                or ""
            ).strip()
            payload = {
                "worker_id": worker_id,
                "run_id": str(durable_session.get("run_id") or ""),
                "identity_kind": (
                    "docker_session" if container_id else "host_process"
                ),
                "pid": pid,
                "process_group": process_group,
                "process_start_identity": start_identity,
                "container_id": container_id,
                "session_id": str(
                    durable_session.get("session_name") or ""
                ),
            }
            try:
                start_observer(payload)
            except Exception as exc:
                self._cleanup_rejected_run_start(
                    worker_id=worker_id,
                    expected_session=durable_session,
                    expected_container_id=container_id,
                    spawned_process=spawned_process,
                    worker=worker,
                )
                raise RunStartupRejectedError(
                    str(exc), termination_confirmed=True
                ) from exc
        return True

    def set_host_process_observer(self, observer) -> None:
        self._host_process_observer = observer

    def set_run_start_observer(self, observer) -> None:
        self._run_start_observer = observer

    def _mark_rejected_run_start_unconfirmed(
        self,
        worker_id: str,
        expected_session: dict[str, object],
    ) -> None:
        current = self._read_active_session(worker_id)
        if self._active_session_fingerprint(
            current
        ) != self._active_session_fingerprint(expected_session):
            return
        self._write_active_session(
            worker_id,
            {**expected_session, "termination_unconfirmed": True},
            expected_session=expected_session,
        )

    def _cleanup_rejected_run_start(
        self,
        *,
        worker_id: str,
        expected_session: dict[str, object],
        expected_container_id: str = "",
        spawned_process: subprocess.Popen[str] | None = None,
        worker: dict | None = None,
    ) -> None:
        """Stop only the exact just-published generation or fail closed."""

        try:
            if expected_container_id:
                self.sandbox.stop_screen_session(
                    worker_id,
                    self.runtime_name,
                    str(expected_session.get("session_name") or ""),
                    worker=worker,
                    missing_ok=True,
                    expected_container_id=expected_container_id,
                )
                self.sandbox.terminate_run_processes(
                    worker_id,
                    self.runtime_name,
                    str(expected_session.get("run_id") or ""),
                    worker=worker,
                    missing_ok=True,
                    expected_container_id=expected_container_id,
                )
            else:
                try:
                    expected_pid = int(expected_session.get("process_pid") or 0)
                    expected_group = int(
                        expected_session.get("process_group") or expected_pid or 0
                    )
                except (TypeError, ValueError):
                    expected_pid = 0
                    expected_group = 0
                expected_identity = str(
                    expected_session.get("process_start_identity") or ""
                ).strip()
                if (
                    spawned_process is None
                    or spawned_process.pid != expected_pid
                    or expected_pid <= 0
                    or not expected_identity
                ):
                    raise RuntimeErrorBase(
                        "The exact host startup process identity is ambiguous."
                    )
                if spawned_process.poll() is None:
                    current_identity = self._process_start_identity(expected_pid)
                    if current_identity != expected_identity:
                        raise RuntimeErrorBase(
                            "The exact host startup process identity changed."
                        )

                    def signal_exact(sig: signal.Signals) -> None:
                        try:
                            if expected_group and expected_group != os.getpgrp():
                                os.killpg(expected_group, sig)
                            else:
                                os.kill(expected_pid, sig)
                        except ProcessLookupError:
                            return

                    signal_exact(signal.SIGTERM)
                    try:
                        spawned_process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        signal_exact(signal.SIGKILL)
                        spawned_process.wait(timeout=2)
                if spawned_process.poll() is None:
                    raise RuntimeErrorBase(
                        "The exact host startup process did not stop."
                    )
                if not self._clear_process(
                    worker_id, expected_process=spawned_process
                ):
                    raise RuntimeErrorBase(
                        "Host startup process ownership changed during cleanup."
                    )
            if not self._clear_active_session(
                worker_id, expected_session=expected_session
            ):
                raise RuntimeErrorBase(
                    "Run startup ownership changed during cleanup."
                )
            if not expected_container_id:
                release_slot = getattr(self, "_release_host_slot", None)
                if callable(release_slot):
                    release_slot(worker_id)
        except Exception as exc:
            self._mark_rejected_run_start_unconfirmed(
                worker_id, expected_session
            )
            raise RunStartupRejectedError(
                str(exc), termination_confirmed=False
            ) from exc

    def set_native_event_observer(self, observer) -> None:
        self._native_event_observer = observer

    def host_process_identity(self, worker: dict, run_id: str) -> dict[str, object] | None:
        """Verify an exact Docker run without relying on an executor process.

        Docker screen PIDs live in the container namespace, while the lease
        ledger needs a stable restart identity. The exact run/session mapping
        plus the immutable container id is the process-start identity; a live
        screen lookup proves the session still exists before a stale lease is
        renewed.
        """

        worker_id = str(worker.get("worker_id") or "").strip()
        expected_run_id = str(run_id or "").strip()
        active_session = self._read_active_session(worker_id)
        if (
            not worker_id
            or not expected_run_id
            or str((active_session or {}).get("run_id") or "").strip()
            != expected_run_id
        ):
            return None
        session_name = str((active_session or {}).get("session_name") or "").strip()
        if not session_name:
            return None
        try:
            sandbox = self.sandbox.inspect(worker_id)
            if (
                sandbox is None
                or str(sandbox.state or "").lower() != "running"
                or not str(sandbox.container_id or "").strip()
            ):
                return None
            screen_pid = self.sandbox.screen_session_pid(
                worker_id,
                self.runtime_name,
                session_name,
                worker=worker,
            )
        except Exception:
            return None
        try:
            current_screen_pid = int(screen_pid or 0)
            recorded_screen_pid = int(
                (active_session or {}).get("process_pid") or 0
            )
        except (TypeError, ValueError):
            return None
        if current_screen_pid <= 0 or (
            recorded_screen_pid > 0 and recorded_screen_pid != current_screen_pid
        ):
            return None
        container_id = str(sandbox.container_id).strip()
        identity = (
            f"docker:{container_id}:{session_name}:"
            f"{expected_run_id}:{current_screen_pid}"
        )
        recorded_identity = str(
            (active_session or {}).get("lease_process_start_identity")
            or ""
        ).strip()
        if recorded_identity and recorded_identity != identity:
            return None
        lease_pid = int(sandbox.pid or 0) or current_screen_pid
        return {
            "identity_kind": "docker_session",
            "pid": lease_pid,
            "process_group": lease_pid,
            "process_start_identity": identity,
            "container_id": container_id,
            "session_id": session_name,
            "startup_token_digest": str(
                (active_session or {}).get("startup_token_digest") or ""
            ),
            "verified": True,
        }

    def host_process_absence(self, worker: dict, run_id: str) -> bool:
        """Prove that the canonical Docker generation is absent without guessing.

        This is intentionally narrower than a failed liveness read: timeouts,
        malformed inspect output, a present container, and every host-mode
        worker remain unproven so their durable safety fence stays active.
        """

        if str(worker.get("execution_mode") or "docker") != "docker" or not str(
            run_id or ""
        ).strip():
            return False
        try:
            inspection = self.sandbox.inspect_fresh(str(worker.get("worker_id") or ""))
        except Exception:
            return False
        return str(getattr(inspection, "status", "") or "") == "confirmed_absent"

    def cleanup_unconfirmed_run_start(
        self,
        worker: dict,
        run_id: str,
        lease_identity: dict[str, object],
    ) -> bool:
        """Clean only a pre-confirmation generation captured by the durable lease.

        A replacement active-session file is never cleanup authority. Docker is
        targeted by immutable container id + exact screen/run identity; host mode
        is targeted by PID start identity. Absence of that old generation is a
        successful cleanup proof, while ambiguity fails closed.
        """

        worker_id = str(worker.get("worker_id") or "").strip()
        expected_run_id = str(run_id or "").strip()
        identity_kind = str(lease_identity.get("identity_kind") or "").strip()
        try:
            expected_pid = int(lease_identity.get("pid") or 0)
            expected_group = int(
                lease_identity.get("process_group") or expected_pid or 0
            )
        except (TypeError, ValueError):
            return False
        expected_start_identity = str(
            lease_identity.get("process_start_identity") or ""
        ).strip()
        if not worker_id or not expected_run_id or not expected_start_identity:
            return False

        if identity_kind == "docker_session":
            container_id = str(lease_identity.get("container_id") or "").strip()
            session_id = str(lease_identity.get("session_id") or "").strip()
            if not container_id or not session_id:
                return False
            self.sandbox.stop_screen_session(
                worker_id,
                self.runtime_name,
                session_id,
                worker=worker,
                missing_ok=True,
                expected_container_id=container_id,
            )
            self.sandbox.terminate_run_processes(
                worker_id,
                self.runtime_name,
                expected_run_id,
                worker=worker,
                missing_ok=True,
                expected_container_id=container_id,
            )
        elif identity_kind == "host_process":
            if expected_pid <= 0 or not expected_start_identity.startswith("ps-lstart:"):
                return False
            if self._recorded_process_is_running(expected_pid, expected_start_identity):

                def signal_exact(sig: signal.Signals) -> None:
                    current_identity = self._process_start_identity(expected_pid)
                    if current_identity != expected_start_identity:
                        return
                    try:
                        if expected_group > 0 and expected_group != os.getpgrp():
                            os.killpg(expected_group, sig)
                        else:
                            os.kill(expected_pid, sig)
                    except ProcessLookupError:
                        return

                signal_exact(signal.SIGTERM)
                if not self._wait_for_recorded_process_exit(
                    expected_pid, expected_start_identity, timeout=5.0
                ):
                    signal_exact(signal.SIGKILL)
                    if not self._wait_for_recorded_process_exit(
                        expected_pid, expected_start_identity, timeout=2.0
                    ):
                        return False
            # A failed liveness probe is not itself death proof: permissions,
            # transient ps failure, or an unreadable fingerprint must keep the
            # startup fence. Only ESRCH/zombie or a different PID incarnation
            # proves the captured generation is gone.
            if not self._recorded_pid_is_proven_gone(
                expected_pid, expected_start_identity
            ):
                return False
        else:
            return False

        current = self._read_active_session(worker_id)
        if current:
            try:
                current_pid = int(
                    current.get("lease_pid")
                    or current.get("process_pid")
                    or 0
                )
            except (TypeError, ValueError):
                current_pid = 0
            current_identity = str(
                current.get("lease_process_start_identity")
                or current.get("process_start_identity")
                or ""
            ).strip()
            current_matches_old = bool(
                str(current.get("run_id") or "") == expected_run_id
                and current_pid == expected_pid
                and current_identity == expected_start_identity
            )
            if identity_kind == "docker_session":
                current_matches_old = bool(
                    current_matches_old
                    and str(current.get("container_id") or "")
                    == str(lease_identity.get("container_id") or "")
                    and str(current.get("session_name") or "")
                    == str(lease_identity.get("session_id") or "")
                )
            if current_matches_old and not self._clear_active_session(
                worker_id, expected_session=current
            ):
                return False
        if identity_kind == "host_process":
            release_slot = getattr(self, "_release_host_slot", None)
            if callable(release_slot):
                release_slot(worker_id)
        return True

    @staticmethod
    def _native_session_id_from_event_line(line: str) -> str:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return ""
        if not isinstance(event, dict):
            return ""
        if str(event.get("type") or "") == "thread.started":
            session_id = str(event.get("thread_id") or "").strip()
        else:
            session_id = str(event.get("session_id") or "").strip()
        if not session_id or len(session_id) > 256 or any(ord(char) < 32 for char in session_id):
            return ""
        return session_id

    def _observe_native_session_events(
        self,
        worker_id: str,
        stdout_path: Path,
        stop_event: Event,
        run_id: str | None = None,
    ) -> None:
        """Tail native JSONL and durably publish session identity as soon as it appears."""

        offset = 0
        buffered = ""

        def observe_line(line: str) -> None:
            session_id = self._native_session_id_from_event_line(line)
            if session_id:
                self._write_session_key(worker_id, session_id)
                active_session = self._read_active_session(worker_id)
                if active_session and active_session.get("native_session_id") != session_id:
                    self._write_active_session(
                        worker_id,
                        {**active_session, "native_session_id": session_id},
                        expected_session=active_session,
                    )
            observer = self._native_event_observer
            if not callable(observer):
                return
            active_session = self._read_active_session(worker_id)
            effective_run_id = str(
                run_id or (active_session or {}).get("run_id") or ""
            ).strip()
            if not effective_run_id:
                return
            provider = self._agent_type()
            for event in project_native_events(provider, line):
                try:
                    observer(
                        {
                            "worker_id": worker_id,
                            "run_id": effective_run_id,
                            "provider": provider,
                            "event": event,
                        }
                    )
                except Exception:
                    logger.exception(
                        "Native event observer failed for worker %s run %s",
                        worker_id,
                        effective_run_id,
                    )

        while True:
            chunk = ""
            try:
                with stdout_path.open("r") as handle:
                    handle.seek(offset)
                    chunk = handle.read()
                    offset = handle.tell()
            except FileNotFoundError:
                pass
            if chunk:
                buffered += chunk
                complete = buffered.split("\n")
                buffered = complete.pop()
                for line in complete:
                    observe_line(line.strip())
            if stop_event.is_set():
                if buffered.strip():
                    observe_line(buffered.strip())
                return
            stop_event.wait(0.05)

    @staticmethod
    def _active_session_fingerprint(session: dict[str, object] | None) -> tuple[object, ...]:
        if not session:
            return ()
        return (
            str(session.get("run_id") or ""),
            str(session.get("session_name") or ""),
            str(session.get("exit_path") or ""),
            session.get("process_pid"),
            str(session.get("process_start_identity") or ""),
            session.get("owner_pid"),
            session.get("lease_pid"),
            str(session.get("lease_process_start_identity") or ""),
            str(session.get("container_id") or ""),
            str(session.get("startup_token_digest") or ""),
        )

    def _clear_active_session(
        self,
        worker_id: str,
        *,
        expected_session: dict[str, object] | None = None,
    ) -> bool:
        path = self._active_session_meta_path(worker_id)
        with self._active_session_file_lock(worker_id):
            if expected_session is not None:
                current = self._read_active_session(worker_id)
                if current is None and not path.exists():
                    return True
                if self._active_session_fingerprint(current) != self._active_session_fingerprint(
                    expected_session
                ):
                    return False
            try:
                path.unlink()
            except FileNotFoundError:
                return True
        return True

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

    def _infer_active_session(self, worker: dict, run_id: str | None = None) -> dict[str, str] | None:
        current = self._read_active_session(worker["worker_id"])
        if current and (run_id is None or current.get("run_id") == run_id):
            return current
        screen_sessions = set(self.sandbox.list_screen_sessions(worker["worker_id"], self.runtime_name, worker=worker))
        candidate_run_ids = [run_id] if run_id else [run_root.name for run_root in self._run_root_candidates(worker["worker_id"])]
        for candidate_run_id in candidate_run_ids:
            if not candidate_run_id:
                continue
            session_name = self._session_name_for_run_id(candidate_run_id)
            if session_name not in screen_sessions:
                continue
            payload = self._run_payload(worker["worker_id"], candidate_run_id)
            if payload:
                return payload
        return None

    def _latest_completed_run_payload(self, worker_id: str, run_id: str | None = None) -> dict[str, str] | None:
        current = self._read_active_session(worker_id)
        if current and (run_id is None or current.get("run_id") == run_id):
            return current
        if run_id:
            payload = self._run_payload(worker_id, run_id)
            if payload and Path(str(payload.get("exit_path") or "")).exists():
                return payload
            return None
        for run_root in self._run_root_candidates(worker_id):
            exit_path = run_root / "exit_code"
            if not exit_path.exists():
                continue
            return {
                "session_name": self._session_name_for_run_id(run_root.name),
                "run_id": run_root.name,
                "stdout_path": str(run_root / "stdout.log"),
                "stderr_path": str(run_root / "stderr.log"),
                "exit_path": str(exit_path),
            }
        return None

    @staticmethod
    def _pid_is_live(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            # A process owned by another account cannot normally be a Viventium
            # owner, but it is live. The owner PID + heartbeat checks still have
            # to pass before the child is accepted.
            return True
        except OSError:
            return False
        return True

    def _recorded_pid_is_proven_gone(
        self,
        pid: int,
        start_identity: str = "",
    ) -> bool:
        """Accept only affirmative death or a proven PID-incarnation mismatch."""

        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except (PermissionError, OSError):
            return False
        if self._pid_is_zombie(pid):
            return True
        recorded_identity = str(start_identity or "").strip()
        if not recorded_identity.startswith("ps-lstart:"):
            return False
        current_identity = self._process_start_identity(pid)
        return bool(current_identity and current_identity != recorded_identity)

    @staticmethod
    def _process_start_identity(pid: int) -> str:
        """Return a stable identity for one PID incarnation, not merely the PID."""

        if pid <= 0:
            return ""
        try:
            completed = subprocess.run(
                ["ps", "-o", "lstart=", "-p", str(pid)],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ""
        started = " ".join(completed.stdout.split())
        return f"ps-lstart:{started}" if completed.returncode == 0 and started else ""

    @staticmethod
    def _process_group_identity(pid: int) -> int:
        """Capture a new-session process group without failing on a fast exit."""

        try:
            return os.getpgid(pid)
        except (OSError, ProcessLookupError):
            # Host subprocesses are always launched with start_new_session=True,
            # so their initial PGID is their PID. A missing start identity keeps
            # this fallback from ever authorizing a later kill of a reused PID.
            return pid

    @staticmethod
    def _pid_is_zombie(pid: int) -> bool:
        try:
            completed = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(pid)],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        state = completed.stdout.strip().upper()
        return completed.returncode == 0 and state.startswith("Z")

    def _recorded_process_is_running(self, pid: int, start_identity: str) -> bool:
        if pid <= 0 or not start_identity or not self._pid_is_live(pid) or self._pid_is_zombie(pid):
            return False
        return self._process_start_identity(pid) == start_identity

    def _wait_for_recorded_process_exit(
        self,
        pid: int,
        start_identity: str,
        *,
        timeout: float,
    ) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while self._recorded_process_is_running(pid, start_identity):
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)
        return True

    @staticmethod
    def _active_session_heartbeat_stale_seconds() -> float:
        raw = os.environ.get("GLASSHIVE_ACTIVE_SESSION_HEARTBEAT_STALE_S", "").strip()
        try:
            parsed = float(raw) if raw else 20.0
        except ValueError:
            parsed = 20.0
        return max(10.0, parsed)

    def _durable_active_session_pid(
        self,
        worker_id: str,
        *,
        expected_run_id: str | None = None,
    ) -> int | None:
        active_session = self._read_active_session(worker_id)
        recorded_run_id = str((active_session or {}).get("run_id") or "").strip()
        if not active_session or not recorded_run_id:
            return None
        if expected_run_id and recorded_run_id != expected_run_id:
            return None
        try:
            recorded_pid = int(active_session.get("process_pid") or 0)
            owner_pid = int(active_session.get("owner_pid") or 0)
        except (TypeError, ValueError):
            return None
        # The child may have exited while its live owner is parsing and committing
        # the terminal result. A fresh owner heartbeat is the short finalization
        # lease; child liveness alone is neither necessary nor sufficient.
        if recorded_pid <= 0 or not self._pid_is_live(owner_pid):
            return None

        recorded_start_identity = str(active_session.get("process_start_identity") or "").strip()
        if recorded_start_identity and not self._recorded_process_is_running(
            recorded_pid, recorded_start_identity
        ):
            return None

        raw_heartbeat_path = str(active_session.get("heartbeat_path") or "").strip()
        if not raw_heartbeat_path:
            return None
        try:
            heartbeat = json.loads(Path(raw_heartbeat_path).read_text())
            heartbeat_run_id = str(heartbeat.get("run_id") or "").strip()
            heartbeat_state = str(heartbeat.get("state") or "").strip()
            heartbeat_pid = int(heartbeat.get("process_pid") or 0)
            heartbeat_at = datetime.fromisoformat(
                str(heartbeat.get("last_heartbeat_at") or "").replace("Z", "+00:00")
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None
        if heartbeat_at.tzinfo is None:
            heartbeat_at = heartbeat_at.replace(tzinfo=timezone.utc)
        age_seconds = max(
            0.0,
            datetime.now(timezone.utc).timestamp() - heartbeat_at.astimezone(timezone.utc).timestamp(),
        )
        if (
            heartbeat_run_id != recorded_run_id
            or heartbeat_state != "running"
            or heartbeat_pid != recorded_pid
            or age_seconds > self._active_session_heartbeat_stale_seconds()
        ):
            return None
        return recorded_pid

    def _active_pid(self, worker_id: str, expected_run_id: str | None = None) -> int | None:
        with self._process_lock:
            process = self._active_processes.get(worker_id)
            if process and process.poll() is None:
                if not expected_run_id:
                    return process.pid
                active_session = self._read_active_session(worker_id)
                if str((active_session or {}).get("run_id") or "").strip() == expected_run_id:
                    return process.pid
                return None
        # Host CLI runs are owned by the service process that launched them, but
        # reconciliation can run in another service process sharing the same
        # runtime root and database. The active-session record is the durable
        # cross-process ownership signal; do not orphan a run whose recorded host
        # process is still alive merely because it is absent from this instance's
        # in-memory Popen map.
        return self._durable_active_session_pid(
            worker_id,
            expected_run_id=expected_run_id,
        )

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

    def _clear_process(
        self,
        worker_id: str,
        *,
        expected_process: subprocess.Popen[str] | None = None,
    ) -> bool:
        with self._process_lock:
            if (
                expected_process is not None
                and self._active_processes.get(worker_id) is not expected_process
            ):
                return False
            self._active_processes.pop(worker_id, None)
        return True

    def _stop_active_process(
        self,
        worker_id: str,
        *,
        worker: dict | None = None,
        run_id: str | None = None,
        allow_stale_terminal_session: bool = False,
    ) -> bool:
        expected_container_id = str(
            (worker or {}).get("_compute_release_container_id") or ""
        ).strip()
        exact_container_absence = bool(
            worker is not None
            and "_compute_release_container_id" in worker
            and not expected_container_id
        )
        active_session = self._read_active_session(worker_id)
        with self._process_lock:
            captured_process = self._active_processes.get(worker_id)
        if active_session and run_id and active_session.get("run_id") != run_id:
            if exact_container_absence:
                raise RuntimeErrorBase(
                    "Docker run ownership changed during exact-run stop"
                )
            active_session = None
            captured_process = None
        if exact_container_absence and active_session:
            if not (
                allow_stale_terminal_session
                and self._stale_terminal_session_is_proven_dead(
                    worker_id,
                    active_session,
                    expected_run_id=run_id,
                )
            ):
                if not self._write_active_session(
                    worker_id,
                    {**active_session, "termination_unconfirmed": True},
                    expected_session=active_session,
                ):
                    raise RuntimeErrorBase(
                        "Docker run ownership changed during exact-run stop"
                    )
                raise RuntimeErrorBase(
                    "Docker run termination could not be confirmed"
                )
            if not self._clear_active_session(worker_id, expected_session=active_session):
                raise RuntimeErrorBase(
                    "Docker run ownership changed during exact-run stop"
                )
            active_session = None
        if not active_session and not exact_container_absence:
            active_session = self._infer_active_session(worker or {"worker_id": worker_id}, run_id=run_id)
        if not active_session and run_id and not exact_container_absence:
            active_session = self._run_payload(worker_id, run_id)
        if (
            active_session
            and allow_stale_terminal_session
            and self._stale_terminal_session_is_proven_dead(
                worker_id,
                active_session,
                expected_run_id=run_id,
            )
        ):
            if not self._clear_active_session(
                worker_id,
                expected_session=active_session,
            ):
                raise RuntimeErrorBase(
                    "Docker run ownership changed during terminal cleanup"
                )
            active_session = None
        if active_session and expected_container_id:
            # A needs-input or paused cleanup can be recovered after the exact
            # Docker generation has already exited.  Docker exec is impossible
            # in that state, but a fresh inspect of the captured immutable
            # container id is sufficient proof that no run process remains.
            # Probe uncertainty or a replacement generation stays fenced.
            try:
                inspection = self.sandbox.inspect_fresh(
                    worker_id,
                    require_configured_image=False,
                )
            except Exception:
                inspection = None
            inspection_status = str(
                getattr(inspection, "status", "") or ""
            ).lower()
            inspected_sandbox = getattr(inspection, "sandbox", None)
            exact_generation_inactive = (
                inspection_status == "present"
                and str(getattr(inspected_sandbox, "container_id", "") or "")
                == expected_container_id
                and str(getattr(inspected_sandbox, "state", "") or "").lower()
                in {"dead", "exited"}
            )
            if exact_generation_inactive:
                if not self._clear_active_session(
                    worker_id,
                    expected_session=active_session,
                ):
                    raise RuntimeErrorBase(
                        "Docker run ownership changed during inactive-generation cleanup"
                    )
                active_session = None
        if active_session:
            stop_errors: list[Exception] = []
            try:
                stop_kwargs: dict[str, object] = {
                    "worker": worker,
                    "missing_ok": True,
                }
                if expected_container_id:
                    stop_kwargs["expected_container_id"] = expected_container_id
                self.sandbox.stop_screen_session(
                    worker_id,
                    self.runtime_name,
                    active_session["session_name"],
                    **stop_kwargs,
                )
            except Exception as exc:
                stop_errors.append(exc)
            try:
                terminate_kwargs: dict[str, object] = {"worker": worker}
                if expected_container_id:
                    terminate_kwargs["expected_container_id"] = expected_container_id
                self.sandbox.terminate_run_processes(
                    worker_id,
                    self.runtime_name,
                    active_session["run_id"],
                    **terminate_kwargs,
                )
            except Exception as exc:
                stop_errors.append(exc)
            if stop_errors:
                # The active-session record is the durable exact-run ownership
                # handle. Keep it when either Docker termination primitive fails
                # so reconciliation cannot mistake an unproven stop for success.
                if not self._write_active_session(
                    worker_id,
                    {**active_session, "termination_unconfirmed": True},
                    expected_session=active_session,
                ):
                    raise RuntimeErrorBase(
                        "Docker run ownership changed during exact-run stop"
                    )
                raise RuntimeErrorBase(
                    "Docker run termination could not be confirmed"
                ) from stop_errors[0]
        process = captured_process
        if not process or process.poll() is not None:
            if process is not None:
                self._clear_process(worker_id, expected_process=process)
            if active_session and not self._clear_active_session(
                worker_id, expected_session=active_session
            ):
                raise RuntimeErrorBase(
                    "Docker run ownership changed during exact-run stop"
                )
            return True
        try:
            process.terminate()
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                raise RuntimeErrorBase(
                    "Docker run termination could not be confirmed"
                )
        except OSError as exc:
            raise RuntimeErrorBase(
                "Docker run termination could not be confirmed"
            ) from exc
        if process.poll() is None:
            raise RuntimeErrorBase("Docker run termination could not be confirmed")
        if not self._clear_process(worker_id, expected_process=process):
            raise RuntimeErrorBase(
                "Docker run ownership changed during exact-run stop"
            )
        if active_session and not self._clear_active_session(
            worker_id, expected_session=active_session
        ):
            raise RuntimeErrorBase(
                "Docker run ownership changed during exact-run stop"
            )
        return True

    def _stale_terminal_session_is_proven_dead(
        self,
        worker_id: str,
        active_session: dict[str, object],
        *,
        expected_run_id: str | None,
    ) -> bool:
        """Prove a completed Docker session is stale without trusting workspace paths."""

        run_id = str(active_session.get("run_id") or "").strip()
        if (
            not expected_run_id
            or run_id != expected_run_id
            or str(active_session.get("session_name") or "").strip()
            != self._session_name_for_run_id(run_id)
        ):
            return False

        canonical_exit = self._run_root(worker_id, run_id) / "exit_code"
        recorded_exit = Path(str(active_session.get("exit_path") or "").strip())
        try:
            if recorded_exit.resolve(strict=True) != canonical_exit.resolve(strict=True):
                return False
            raw_exit_code = canonical_exit.read_text().strip()
            exit_code = int(raw_exit_code)
        except (OSError, TypeError, ValueError):
            return False
        if str(exit_code) != raw_exit_code or not 0 <= exit_code <= 255:
            return False

        with self._process_lock:
            process = self._active_processes.get(worker_id)
        if process and process.poll() is None:
            return False

        lease_identity = str(
            active_session.get("lease_process_start_identity") or ""
        ).strip()
        identity_parts = lease_identity.split(":")
        if len(identity_parts) == 5 and identity_parts[0] == "docker":
            _, container_id, identity_session, identity_run, identity_screen_pid = (
                identity_parts
            )
            try:
                recorded_screen_pid = int(active_session.get("process_pid") or 0)
                recorded_owner_pid = int(active_session.get("owner_pid") or 0)
                recorded_container_pid = int(active_session.get("lease_pid") or 0)
                identity_screen_pid_value = int(identity_screen_pid or 0)
            except (TypeError, ValueError):
                return False
            if (
                not container_id
                or identity_session != str(active_session.get("session_name") or "")
                or identity_run != run_id
                or recorded_screen_pid <= 0
                or identity_screen_pid_value != recorded_screen_pid
                or recorded_owner_pid <= 0
                or recorded_container_pid <= 0
            ):
                return False
            try:
                sandbox = self.sandbox.inspect(worker_id)
            except Exception:
                return False
            if sandbox is not None:
                if (
                    str(sandbox.container_id or "").strip() != container_id
                    or str(sandbox.state or "").lower() != "running"
                    or int(sandbox.pid or 0) != recorded_container_pid
                ):
                    return False
                try:
                    live_screen_pid = self.sandbox.screen_session_pid(
                        worker_id,
                        self.runtime_name,
                        identity_session,
                        worker={"worker_id": worker_id, "state": "running"},
                    )
                except Exception:
                    return False
                if int(live_screen_pid or 0) > 0:
                    return False
                return self._recorded_pid_is_proven_gone(recorded_owner_pid)

        recorded_processes: list[tuple[int, str]] = []
        for key, identity_key in (
            ("process_pid", "process_start_identity"),
            ("owner_pid", ""),
            ("lease_pid", "lease_process_start_identity"),
        ):
            try:
                pid = int(active_session.get(key) or 0)
            except (TypeError, ValueError):
                return False
            if pid <= 0:
                return False
            start_identity = (
                str(active_session.get(identity_key) or "").strip()
                if identity_key
                else ""
            )
            recorded_processes.append((pid, start_identity))
        return all(
            self._recorded_pid_is_proven_gone(pid, start_identity)
            for pid, start_identity in recorded_processes
        )

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
        fast_sandbox = (
            None
            if self._uses_parallel_clean_room(worker)
            else getattr(
                self.sandbox, "fast_sandbox_from_worker", lambda _worker: None
            )(worker)
        )
        sandbox = fast_sandbox or self.sandbox.ensure_ready(worker, self.runtime_name)
        return self._runtime_info(worker, pid=sandbox.pid)

    def clear_run_local_capability_grant(self, worker: dict) -> None:
        """Rewrite run env from durable metadata so an admitted bearer cannot linger."""

        refresh_runtime_env_for_worker(self._home_dir(str(worker["worker_id"])), worker)

    def pause_worker(self, worker: dict) -> RuntimeInfo:
        expected_container_id = str(
            worker.get("_compute_release_container_id") or ""
        ).strip()
        sandbox = (
            self.sandbox.pause(
                worker["worker_id"],
                expected_container_id=expected_container_id,
            )
            if expected_container_id
            else self.sandbox.pause(worker["worker_id"])
        )
        if str(sandbox.state or "").lower() != "paused":
            raise RuntimeErrorBase("Docker pause could not be confirmed")
        return self._runtime_info(worker, pid=None)

    def interrupt_worker(self, worker: dict, run_id: str | None = None) -> RuntimeInfo:
        self._note_stop_reason(worker["worker_id"], "interrupted", run_id=run_id)
        confirmed = self._stop_active_process(
            worker["worker_id"], worker=worker, run_id=run_id
        )
        if not confirmed:
            raise RuntimeErrorBase("Docker run termination could not be confirmed")
        return self._runtime_info(worker, pid=None)

    def terminate_worker(self, worker: dict) -> RuntimeInfo:
        self._note_stop_reason(worker["worker_id"], "terminated")
        terminal_run_id = str(worker.get("_terminal_run_id") or "").strip()
        expected_container_id = str(
            worker.get("_compute_release_container_id") or ""
        ).strip()
        expected_container_absence = bool(
            "_compute_release_container_id" in worker
            and not expected_container_id
        )
        terminate_kwargs = {}
        if self._uses_parallel_clean_room(worker):
            terminate_kwargs["execution_policy"] = (
                PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
            )
        self._stop_active_process(
            worker["worker_id"],
            worker=worker,
            run_id=terminal_run_id or None,
            allow_stale_terminal_session=bool(terminal_run_id),
        )
        if expected_container_absence:
            self.sandbox.terminate(
                worker["worker_id"],
                expected_absent=True,
                **terminate_kwargs,
            )
        else:
            if expected_container_id:
                self.sandbox.terminate(
                    worker["worker_id"],
                    expected_container_id=expected_container_id,
                    **terminate_kwargs,
                )
            else:
                self.sandbox.terminate(worker["worker_id"], **terminate_kwargs)
        return self._runtime_info(worker, pid=None)

    def reconcile_worker(self, worker: dict) -> RuntimeInfo:
        sandbox = self.sandbox.inspect(worker["worker_id"])
        active_run_id = str(worker.get("_active_run_id") or "").strip()
        if active_run_id:
            active_session = self._read_active_session(worker["worker_id"])
            if (
                active_session
                and str(active_session.get("run_id") or "") == active_run_id
                and bool(active_session.get("termination_unconfirmed"))
            ):
                pending_pid = (
                    sandbox.pid
                    if sandbox and str(sandbox.state or "").lower() == "running"
                    else None
                )
                return self._runtime_info(worker, pid=pending_pid)
            identity = self.host_process_identity(worker, active_run_id)
            pid = (
                int(identity.get("pid") or 0) or None
                if identity and bool(identity.get("verified"))
                else None
            )
        else:
            pid = sandbox.pid if sandbox and sandbox.state == "running" else None
        return self._runtime_info(worker, pid=pid)

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
            if exit_path.exists():
                try:
                    return int(exit_path.read_text().strip() or "0")
                except ValueError:
                    return 1
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

    @staticmethod
    def _uses_parallel_clean_room(worker: dict) -> bool:
        return (
            str(bootstrap_bundle_for(worker).get("execution_policy") or "").strip()
            == PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
        )

    def _container_env_for_worker(
        self, worker: dict, *legacy_ambient_keys: str
    ) -> dict[str, str]:
        if not self._uses_parallel_clean_room(worker):
            return self._container_env(*legacy_ambient_keys)

        configuration, reason = self.sandbox._parallel_clean_room_configuration(
            require_proxy_containers=False
        )
        if configuration is None:
            raise RuntimeErrorBase(
                "Parallel clean-room provider proxy configuration is unavailable "
                f"({reason})"
            )
        # Automatic missions receive no ambient provider login/key/base-URL
        # authority. The internal network and its attested egress proxy own the
        # provider route; the capability grant is loaded from the run-only
        # private secret projection inside the script, never a docker-exec arg.
        env = self._container_env()
        provider_hostname = str(configuration["provider_proxy_hostname"])
        env.update(
            {
                "HTTP_PROXY": configuration["provider_proxy_url"],
                "HTTPS_PROXY": configuration["provider_proxy_url"],
                "NO_PROXY": (
                    f"{provider_hostname},host.docker.internal,localhost,127.0.0.1"
                ),
            }
        )
        return env

    def _parallel_clean_room_provider_base_url(
        self, worker: dict, provider: str
    ) -> str:
        if not self._uses_parallel_clean_room(worker):
            return ""
        configuration, reason = self.sandbox._parallel_clean_room_configuration(
            require_proxy_containers=False
        )
        if configuration is None:
            raise RuntimeErrorBase(
                "Parallel clean-room provider proxy configuration is unavailable "
                f"({reason})"
            )
        provider_path = {
            "openai": "openai/v1",
            "anthropic": "anthropic",
        }.get(str(provider or "").strip().lower())
        if not provider_path:
            raise RuntimeErrorBase("Parallel clean-room provider route is unsupported")
        return f"{str(configuration['provider_proxy_url']).rstrip('/')}/{provider_path}"

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
        if not exit_path.exists():
            if not self._stdout_has_complete_response(stdout_path):
                return None
            try:
                exit_path.write_text("0")
            except OSError:
                return None
            self._stop_active_process(worker["worker_id"], worker=worker, run_id=run_id)
        stdout = stdout_path.read_text() if stdout_path.exists() else ""
        stderr = stderr_path.read_text() if stderr_path.exists() else ""
        try:
            exit_code = int(exit_path.read_text().strip() or "0")
        except ValueError:
            exit_code = 1
        if exit_code != 0:
            classification = classify_cli_failure(
                stdout=stdout,
                stderr=stderr,
                runtime_name=self.runtime_name,
                exit_code=exit_code,
            )
            detail = _redact_text((stderr or stdout or "").strip(), max_chars=2000)
            return {
                "state": (
                    "needs_input"
                    if classification.failure_class
                    == "provider_auth_projection_unavailable"
                    else "failed"
                ),
                "output_text": "",
                "error_text": _redact_text(f"{self.runtime_name} exited with code {exit_code}: {detail}"),
                **classification.as_store_fields(),
                **(
                    {"provider_retry_after_s": classification.retry_after_s}
                    if classification.retry_after_s is not None
                    else {}
                ),
            }
        info = self.reconcile_worker(worker)
        try:
            session_key, output = self._parse_output(worker, stdout, stderr, info)
        except RuntimeErrorBase as exc:
            return {
                "state": "failed",
                "output_text": "",
                "error_text": str(exc),
            }
        if session_key:
            self._write_session_key(worker["worker_id"], session_key)
        workspace = Path(str(info.workspace_dir or self._workspace_dir(worker["worker_id"])))
        effective_run_id = str(run_id or active_session.get("run_id") or "").strip()
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
        clean_room = self._uses_parallel_clean_room(worker_for_run)
        info = self.ensure_worker_ready(worker_for_run)
        run_secret_paths: dict[str, str] | None = None
        run_secret_container_id = ""
        if clean_room:
            run_sandbox_inspection = self.sandbox.inspect_fresh(
                worker_for_run["worker_id"]
            )
            run_sandbox = run_sandbox_inspection.sandbox
            if (
                run_sandbox_inspection.status != "present"
                or run_sandbox is None
                or not str(run_sandbox.container_id or "").strip()
            ):
                raise RuntimeErrorBase(
                    "Parallel clean-room sandbox generation is unavailable for run authority"
                )
            run_secret_container_id = str(run_sandbox.container_id or "").strip()
            binding = worker_for_run.get("_run_local_capability_binding")
            bound_container_id = str(
                binding.get("containerGenerationId")
                if isinstance(binding, dict)
                else ""
            ).strip()
            if (
                not re.fullmatch(r"[a-f0-9]{64}", bound_container_id)
                or bound_container_id != run_secret_container_id
            ):
                raise RuntimeErrorBase(
                    "The run-local capability grant does not match the exact sandbox generation"
                )
            secret_root = f"/run/glasshive/{effective_run_id}"
            run_secret_paths = {
                "env_file": f"{secret_root}/secret-runtime.env",
                "keys_file": f"{secret_root}/secret-runtime.keys",
            }
        refresh_runtime_env_for_worker(
            self._home_dir(worker_for_run["worker_id"]), worker_for_run
        )
        workspace = Path(str(info.workspace_dir or self._workspace_dir(worker_for_run["worker_id"])))
        refresh_project_runtime_files_for_worker(
            self._home_dir(worker_for_run["worker_id"]),
            workspace,
            worker_for_run,
        )
        command, env = self._build_command(worker_for_run, instruction, info)
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

        host_stdout = run_root / "stdout.log"
        host_stderr = run_root / "stderr.log"
        host_exit = run_root / "exit_code"
        host_script = run_root / "run.sh"
        host_stdin = run_root / "instruction.stdin"

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
                "umask 077",
                f"mkdir -p {shlex.quote(container_run_root)}",
                (
                    "write_exit() { "
                    f"if [ ! -f {shlex.quote(container_exit)} ]; then "
                    f"printf '%s' \"$1\" > {shlex.quote(container_exit)}; "
                    "fi; "
                    "}"
                ),
                (
                    "GLASSHIVE_SECRET_ENV_KEYS_FILE="
                    + shlex.quote(str(run_secret_paths["keys_file"]))
                    if run_secret_paths is not None
                    else 'GLASSHIVE_SECRET_ENV_KEYS_FILE="$HOME/.glasshive/secret-runtime.keys"'
                ),
                (
                    "GLASSHIVE_SECRET_ENV_FILE="
                    + shlex.quote(str(run_secret_paths["env_file"]))
                    if run_secret_paths is not None
                    else 'GLASSHIVE_SECRET_ENV_FILE="$HOME/.glasshive/secret-runtime.env"'
                ),
                (
                    "GLASSHIVE_SECRET_ENV_DIR="
                    + shlex.quote(str(Path(run_secret_paths["env_file"]).parent))
                    if run_secret_paths is not None
                    else "GLASSHIVE_SECRET_ENV_DIR=''"
                ),
                (
                    "scrub_run_secrets() { "
                    'if [ -f "$GLASSHIVE_SECRET_ENV_KEYS_FILE" ]; then '
                    'while IFS= read -r key; do [ -n "$key" ] && unset "$key"; done '
                    '< "$GLASSHIVE_SECRET_ENV_KEYS_FILE"; fi; '
                    'rm -f "$GLASSHIVE_SECRET_ENV_FILE" "$GLASSHIVE_SECRET_ENV_KEYS_FILE"; '
                    'if [ -n "$GLASSHIVE_SECRET_ENV_DIR" ]; then '
                    'rmdir "$GLASSHIVE_SECRET_ENV_DIR" 2>/dev/null || true; fi; '
                    "}"
                ),
                "abort_run() { scrub_run_secrets; write_exit \"${1:-130}\"; exit \"${1:-130}\"; }",
                "trap 'abort_run 130' HUP INT TERM",
                f"cd {shlex.quote(self.sandbox.workspace_mount)} || exit 1",
                f"export GLASSHIVE_ACTIVE_RUN_ID={shlex.quote(effective_run_id)}",
                f"export GLASSHIVE_ACTIVE_WORKER_ID={shlex.quote(worker_for_run['worker_id'])}",
                'if [ -f "$HOME/.glasshive/runtime.env" ]; then set -a; source "$HOME/.glasshive/runtime.env"; set +a; fi',
                'if [ -f "$GLASSHIVE_SECRET_ENV_FILE" ]; then set -a; source "$GLASSHIVE_SECRET_ENV_FILE"; set +a; rm -f "$GLASSHIVE_SECRET_ENV_FILE"; fi',
                *(
                    [
                        ': "${GLASSHIVE_CAPABILITY_BROKER_TOKEN:?missing run capability grant}"',
                        'export OPENAI_API_KEY="$GLASSHIVE_CAPABILITY_BROKER_TOKEN"',
                        'export ANTHROPIC_API_KEY="$GLASSHIVE_CAPABILITY_BROKER_TOKEN"',
                        'export ANTHROPIC_AUTH_TOKEN="$GLASSHIVE_CAPABILITY_BROKER_TOKEN"',
                        (
                            "export OPENAI_BASE_URL="
                            + shlex.quote(
                                self._parallel_clean_room_provider_base_url(
                                    worker_for_run, "openai"
                                )
                            )
                        ),
                        (
                            "export ANTHROPIC_BASE_URL="
                            + shlex.quote(
                                self._parallel_clean_room_provider_base_url(
                                    worker_for_run, "anthropic"
                                )
                            )
                        ),
                    ]
                    if clean_room
                    else []
                ),
                'if [ -f "$HOME/.wpr-openclaw/openclaw.env" ]; then set -a; source "$HOME/.wpr-openclaw/openclaw.env"; set +a; fi',
                f"{command_invocation} > >(tee -a {shlex.quote(container_stdout)}) 2> >(tee -a {shlex.quote(container_stderr)} >&2)",
                "status=$?",
                "scrub_run_secrets",
                "write_exit \"$status\"",
                *(
                    [
                        "printf '\\n[glasshive] run finished with exit code %s; credential-free session exiting.\\n' \"$status\"",
                        'exit "$status"',
                    ]
                    if clean_room
                    else [
                        "printf '\\n[glasshive] run finished with exit code %s. Interactive shell remains open for takeover.\\n' \"$status\"",
                        "exec bash --noprofile --norc",
                    ]
                ),
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

        self._stop_active_process(worker_for_run["worker_id"], worker=worker_for_run)
        run_authority_projected = False
        if clean_room:
            projected_paths = self.sandbox.project_parallel_clean_room_run_secrets(
                worker_for_run["worker_id"],
                expected_container_id=run_secret_container_id,
                run_id=effective_run_id,
                env=bootstrap_env_for(worker_for_run),
            )
            if projected_paths != run_secret_paths:
                raise RuntimeErrorBase(
                    "Parallel clean-room run authority projection path is invalid"
                )
            run_authority_projected = True
        try:
            start_result = self.sandbox.start_screen_session(
                worker_for_run["worker_id"],
                self.runtime_name,
                session_name,
                ["bash", "--noprofile", "--norc", container_script],
                env=env,
                worker=worker_for_run,
            )
        except Exception:
            if run_authority_projected:
                self.sandbox.clear_parallel_clean_room_run_secrets(
                    worker_for_run["worker_id"],
                    expected_container_id=run_secret_container_id,
                    run_id=effective_run_id,
                )
            raise
        if start_result.returncode != 0:
            if run_authority_projected:
                self.sandbox.clear_parallel_clean_room_run_secrets(
                    worker_for_run["worker_id"],
                    expected_container_id=run_secret_container_id,
                    run_id=effective_run_id,
                )
            detail = (start_result.stderr or start_result.stdout or "").strip()[-1600:]
            raise RuntimeErrorBase(f"Failed to start attached {self.runtime_name} session: {detail}")
        process_pid = self.sandbox.screen_session_pid(
            worker_for_run["worker_id"],
            self.runtime_name,
            session_name,
            worker=worker_for_run,
        )
        try:
            sandbox_identity = self.sandbox.inspect(worker_for_run["worker_id"])
        except Exception:
            # The owning executor still heartbeats the lease. A transient
            # Docker inspect failure must not abandon an already-started run;
            # restart reconciliation will fail closed unless it can prove the
            # exact container/session later.
            sandbox_identity = None
        container_id = str(
            (getattr(sandbox_identity, "container_id", None) if sandbox_identity else None)
            or ""
        ).strip()
        try:
            screen_pid = int(process_pid or 0)
            container_pid = int(
                (getattr(sandbox_identity, "pid", None) if sandbox_identity else None)
                or 0
            )
        except (TypeError, ValueError):
            screen_pid = 0
            container_pid = 0
        lease_process_identity = (
            f"docker:{container_id}:{session_name}:"
            f"{effective_run_id}:{screen_pid}"
            if container_id and screen_pid > 0
            else ""
        )

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
        native_session_stop = Event()
        native_session_thread: Thread | None = None
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
                "lease_pid": container_pid or screen_pid or None,
                "lease_process_group": container_pid or screen_pid or None,
                "lease_process_start_identity": lease_process_identity,
                "container_id": container_id,
                "owner_pid": os.getpid(),
                "heartbeat_path": str(heartbeat_path),
                "timeout_seconds": run_timeout_sec,
                "instruction": instruction,
            },
            publish_run_start=True,
            worker=worker_for_run,
        )
        native_session_thread = Thread(
            target=self._observe_native_session_events,
            args=(
                worker_for_run["worker_id"],
                host_stdout,
                native_session_stop,
                effective_run_id,
            ),
            name=f"glasshive-docker-native-session-{effective_run_id[:12]}",
            daemon=True,
        )
        native_session_thread.start()
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
            native_session_stop.set()
            if native_session_thread:
                native_session_thread.join(timeout=1)
            heartbeat_stop.set()
            if heartbeat_thread:
                heartbeat_thread.join(timeout=1)
            if run_authority_projected:
                self.sandbox.clear_parallel_clean_room_run_secrets(
                    worker_for_run["worker_id"],
                    expected_container_id=run_secret_container_id,
                    run_id=effective_run_id,
                )
            try:
                self.sandbox.harden_worker_host_tree(
                    worker_for_run["worker_id"]
                )
            except Exception as exc:
                raise RuntimeErrorBase(
                    "Docker worker host permissions could not be secured after the run."
                ) from exc
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
        with stderr_path.open("a") as handle:
            if stderr:
                handle.write(stderr)
                if not stderr.endswith("\n"):
                    handle.write("\n")

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
            raise _provider_process_exit_error(
                runtime_name=self.runtime_name,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                message=f"{self.runtime_name} exited with code {exit_code}: {detail}",
            )

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
        ]
        self._openclaw_env_path(worker_id).write_text("\n".join(env_lines) + "\n")

    def _gateway_enabled(self) -> bool:
        return self._env_flag("WPR_OPENCLAW_START_GATEWAY", False)

    def _gateway_env(self, worker: dict) -> dict[str, str]:
        env = self._sandbox_env(worker)
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

    def _sandbox_env(self, worker: dict) -> dict[str, str]:
        env = self._container_env_for_worker(worker, *_PROVIDER_ENV_KEYS)
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
        fast_sandbox = (
            None
            if self._uses_parallel_clean_room(worker)
            else getattr(
                self.sandbox, "fast_sandbox_from_worker", lambda _worker: None
            )(worker)
        )
        sandbox = fast_sandbox or self.sandbox.ensure_ready(worker, self.runtime_name)
        self._write_gateway_config(worker, self._gateway_token(worker))
        self._start_openclaw_gateway(worker, sandbox)
        return self._runtime_info(worker, pid=sandbox.pid)

    def pause_worker(self, worker: dict) -> RuntimeInfo:
        expected_container_id = str(
            worker.get("_compute_release_container_id") or ""
        ).strip()
        sandbox = (
            self.sandbox.pause(
                worker["worker_id"],
                expected_container_id=expected_container_id,
            )
            if expected_container_id
            else self.sandbox.pause(worker["worker_id"])
        )
        if str(sandbox.state or "").lower() != "paused":
            raise RuntimeErrorBase("Docker pause could not be confirmed")
        return self._runtime_info(worker, pid=None)

    def interrupt_worker(self, worker: dict, run_id: str | None = None) -> RuntimeInfo:
        if str(worker.get("state") or "") == "running":
            self._note_stop_reason(worker["worker_id"], "interrupted", run_id=run_id)
        confirmed = self._stop_active_process(
            worker["worker_id"], worker=worker, run_id=run_id
        )
        if not confirmed:
            raise RuntimeErrorBase("Docker run termination could not be confirmed")
        return self._runtime_info(worker, pid=None)

    def terminate_worker(self, worker: dict) -> RuntimeInfo:
        self._note_stop_reason(worker["worker_id"], "terminated")
        terminal_run_id = str(worker.get("_terminal_run_id") or "").strip()
        expected_container_id = str(
            worker.get("_compute_release_container_id") or ""
        ).strip()
        expected_container_absence = bool(
            "_compute_release_container_id" in worker
            and not expected_container_id
        )
        terminate_kwargs = {}
        if self._uses_parallel_clean_room(worker):
            terminate_kwargs["execution_policy"] = (
                PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
            )
        self._stop_active_process(
            worker["worker_id"],
            worker=worker,
            run_id=terminal_run_id or None,
            allow_stale_terminal_session=bool(terminal_run_id),
        )
        if expected_container_absence:
            self.sandbox.terminate(
                worker["worker_id"],
                expected_absent=True,
                **terminate_kwargs,
            )
        else:
            if expected_container_id:
                self.sandbox.terminate(
                    worker["worker_id"],
                    expected_container_id=expected_container_id,
                    **terminate_kwargs,
                )
            else:
                self.sandbox.terminate(worker["worker_id"], **terminate_kwargs)
        return self._runtime_info(worker, pid=None)

    def reconcile_worker(self, worker: dict) -> RuntimeInfo:
        sandbox = self.sandbox.inspect(worker["worker_id"])
        if sandbox is None:
            return self._runtime_info(worker, pid=None)
        if sandbox.state == "paused":
            return self._runtime_info(worker, pid=None)
        active_run_id = str(worker.get("_active_run_id") or "").strip()
        if active_run_id:
            active_session = self._read_active_session(worker["worker_id"])
            if (
                active_session
                and str(active_session.get("run_id") or "") == active_run_id
                and bool(active_session.get("termination_unconfirmed"))
            ):
                return self._runtime_info(worker, pid=sandbox.pid)
            identity = self.host_process_identity(worker, active_run_id)
            pid = (
                int(identity.get("pid") or 0) or None
                if identity and bool(identity.get("verified"))
                else None
            )
            return self._runtime_info(worker, pid=pid)
        return self._runtime_info(worker, pid=sandbox.pid)

    def _build_command(self, worker: dict, instruction: str, info: RuntimeInfo) -> tuple[list[str], dict[str, str]]:
        session_id = info.session_key or self._default_session_key(worker) or f"agent:main:wpr:worker:{worker['worker_id']}"
        self._neutralize_default_openclaw_bootstrap(worker)
        env = self._sandbox_env(worker)
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

    def _codex_native_session_is_available(
        self,
        worker_id: str,
        session_key: str,
    ) -> bool:
        """Return false only when local Codex state proves a resume target is gone."""

        codex_home = self._home_dir(worker_id) / ".codex"
        state_databases = sorted(codex_home.glob("state_*.sqlite"))
        queried_native_store = False
        for database_path in state_databases:
            connection: sqlite3.Connection | None = None
            try:
                connection = sqlite3.connect(
                    f"{database_path.resolve().as_uri()}?mode=ro",
                    uri=True,
                    timeout=0.1,
                )
                row = connection.execute(
                    "SELECT rollout_path FROM threads WHERE id = ? LIMIT 1",
                    (session_key,),
                ).fetchone()
                queried_native_store = True
            except (OSError, sqlite3.Error):
                continue
            finally:
                if connection is not None:
                    connection.close()
            if row is None:
                continue
            rollout_value = str(row[0] or "").strip()
            if not rollout_value:
                return False
            rollout_path = Path(rollout_value)
            if rollout_path.is_file():
                return True
            codex_marker = f"{os.sep}.codex{os.sep}"
            if codex_marker in rollout_value:
                relative_rollout = rollout_value.split(codex_marker, 1)[1]
                return (codex_home / relative_rollout).is_file()
            # A row in a compatible future store is stronger evidence than a
            # host-side path that this runtime does not know how to translate.
            return True
        if queried_native_store:
            return False

        legacy_sessions = codex_home / "sessions"
        if legacy_sessions.is_dir():
            try:
                return any(legacy_sessions.rglob(f"*{session_key}.jsonl"))
            except OSError:
                return True
        # Older or externally managed Codex installations may not expose a
        # local index that GlassHive can safely inspect. Preserve resume there.
        return True

    def _resumable_codex_session_key(self, worker: dict) -> str:
        session_key = str(self._read_session_key(worker["worker_id"]) or "").strip()
        if not session_key or session_key.startswith("codex-worker:"):
            return ""
        if self._codex_native_session_is_available(worker["worker_id"], session_key):
            return session_key
        logger.warning(
            "Codex native session is unavailable; starting fresh in the durable workspace",
            extra={"worker_id": str(worker.get("worker_id") or "")},
        )
        return ""

    def provider_citation_sources(self, worker: dict, run_id: str) -> list[dict[str, str]]:
        """Resolve cited public URLs from the private Codex rollout ledger.

        ``codex exec --json`` exposes citation anchors in the assistant message but keeps the
        corresponding URL/title records in its private session rollout. Export only that small
        provenance tuple; snippets and other native-session content stay private.
        """

        clean_run_id = str(run_id or "").strip()
        if (
            not clean_run_id
            or "/" in clean_run_id
            or "\\" in clean_run_id
            or clean_run_id in {".", ".."}
        ):
            raise ValueError("invalid run id")
        stdout_path = self._run_root(str(worker["worker_id"]), clean_run_id) / "stdout.log"
        if not stdout_path.is_file():
            return []
        thread_id = ""
        try:
            with stdout_path.open(errors="ignore") as handle:
                for line in handle:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(event, dict) and event.get("type") == "thread.started":
                        thread_id = str(event.get("thread_id") or "").strip()
                        if thread_id:
                            break
        except OSError:
            return []
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", thread_id):
            return []

        sessions_root = self._home_dir(str(worker["worker_id"])) / ".codex" / "sessions"
        if not sessions_root.is_dir():
            return []
        try:
            candidates = list(sessions_root.rglob(f"*{thread_id}.jsonl"))
        except OSError:
            return []
        if not candidates:
            return []
        try:
            rollout_path = max(candidates, key=lambda path: path.stat().st_mtime_ns)
        except OSError:
            return []

        sources: dict[str, dict[str, str]] = {}
        try:
            with rollout_path.open(errors="ignore") as handle:
                for line in handle:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event, dict) or event.get("type") != "event_msg":
                        continue
                    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
                    if payload.get("type") != "web_search_end":
                        continue
                    results = payload.get("results") if isinstance(payload.get("results"), list) else []
                    for result in results:
                        if not isinstance(result, dict):
                            continue
                        ref_id = str(result.get("ref_id") or "").strip()
                        url = str(result.get("url") or "").strip()
                        try:
                            parsed_url = urlsplit(url)
                        except ValueError:
                            continue
                        if (
                            not re.fullmatch(r"turn\d+[A-Za-z_][A-Za-z0-9_-]*?\d+", ref_id)
                            or parsed_url.scheme not in {"http", "https"}
                            or not parsed_url.netloc
                            or any(character.isspace() or ord(character) < 32 for character in url)
                        ):
                            continue
                        raw_title = str(result.get("title") or result.get("domain") or parsed_url.netloc)
                        title = " ".join(raw_title.split())
                        sources[ref_id] = {
                            "ref_id": ref_id,
                            "title": title[:300] or parsed_url.netloc,
                            "url": url,
                        }
        except OSError:
            return []
        return list(sources.values())

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

    def _compatible_provider_base_url(self) -> str:
        return (
            os.environ.get("WPR_CODEX_CLI_BASE_URL", "").strip()
            or os.environ.get("OPENAI_BASE_URL", "").strip()
            or os.environ.get("OPENAI_API_BASE", "").strip()
            or os.environ.get("OPENAI_REVERSE_PROXY", "").strip()
            or os.environ.get("PORTKEY_BASE_URL", "").strip()
        ).rstrip("/")

    def _compatible_provider_enabled(self) -> bool:
        if self._env_flag("WPR_CODEX_CLI_DISABLE_CUSTOM_PROVIDER", False):
            return False
        if self._env_flag("WPR_CODEX_CLI_USE_CUSTOM_PROVIDER", False):
            return True
        return bool(self._compatible_provider_base_url())

    def _compatible_provider_id(self) -> str:
        raw = os.environ.get("WPR_CODEX_CLI_MODEL_PROVIDER", "glasshive_openai_compatible").strip()
        return re.sub(r"[^A-Za-z0-9_-]+", "_", raw).strip("_") or "glasshive_openai_compatible"

    def _compatible_provider_env_key(self) -> str:
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
        clean_room = self._uses_parallel_clean_room(worker)
        if not clean_room and not self._compatible_provider_enabled():
            return
        base_url = (
            self._parallel_clean_room_provider_base_url(worker, "openai")
            if clean_room
            else self._compatible_provider_base_url()
        )
        if not base_url:
            return
        provider_id = self._compatible_provider_id()
        provider_name = os.environ.get("WPR_CODEX_CLI_PROVIDER_NAME", "GlassHive OpenAI-compatible").strip()
        wire_api = os.environ.get("WPR_CODEX_CLI_WIRE_API", "responses").strip() or "responses"
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
                (
                    f'model_providers.{provider_id}.env_key="'
                    + (
                        "GLASSHIVE_CAPABILITY_BROKER_TOKEN"
                        if clean_room
                        else self._compatible_provider_env_key()
                    )
                    + '"'
                ),
                "-c",
                f'model_providers.{provider_id}.wire_api="{wire_api}"',
                "-c",
                f"model_providers.{provider_id}.requires_openai_auth=false",
                "-c",
                f"model_providers.{provider_id}.supports_websockets=false",
            ]
        )
        if verbosity:
            command.extend(["-c", f'model_verbosity="{verbosity}"'])
        if include_reasoning_effort:
            self._append_codex_reasoning_effort_config(command, worker)

    def _build_command(self, worker: dict, instruction: str, info: RuntimeInfo) -> tuple[list[str], dict[str, str]]:
        existing_session = self._resumable_codex_session_key(worker)
        model = self._codex_model_for_worker(worker, "WPR_MODEL_CODEX_CLI")
        is_resume = bool(existing_session)
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
        self._append_codex_compatible_provider_config(command, worker, include_reasoning_effort=False)
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
        env = self._container_env_for_worker(
            worker,
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
    _workspace_effort_support_cache: dict[tuple[str, str], bool] = {}

    def resolve_model(self, profile: str) -> str:
        return os.environ.get("WPR_MODEL_CLAUDE_CODE", "claude-sonnet-4-6")

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
        if self._effort_for_worker(worker) != "max":
            return
        cache_key = (str(self.sandbox.image), self.binary)
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
                "Claude Code max effort could not be preflighted in the GlassHive workspace image",
                binary=self.binary,
                runtime_name=self.runtime_name,
                profile=str(worker.get("profile") or "claude-code"),
                execution_mode="docker",
                dependency_label="Claude Code --effort support",
                recovery_hint=(
                    "Use a GlassHive workspace image with a Claude Code CLI that supports `--effort`, "
                    "or run this worker without `max` effort until the image is upgraded."
                ),
            ) from exc
        help_text = f"{result.stdout or ''}\n{result.stderr or ''}"
        if result.returncode != 0 or "--effort" not in help_text:
            actual = (help_text.strip() or f"exit {result.returncode}")[-400:]
            raise RuntimeDependencyMissingError(
                "Claude Code max effort requires workspace image support for `claude --effort`",
                binary=self.binary,
                runtime_name=self.runtime_name,
                profile=str(worker.get("profile") or "claude-code"),
                execution_mode="docker",
                required_version="Claude Code CLI with --effort support",
                actual_version=actual,
                dependency_label="Claude Code --effort support",
                recovery_hint=(
                    "Upgrade the GlassHive workspace image or use default Claude effort for this run. "
                    "Do not silently project `max` when the active image cannot prove support."
                ),
            )
        self._workspace_effort_support_cache[cache_key] = True

    def run_task(self, worker: dict, instruction: str, timeout_sec: float | None = None, run_id: str | None = None) -> str:
        self._preflight_workspace_effort_support(worker)
        return super().run_task(worker, instruction, timeout_sec=timeout_sec, run_id=run_id)

    def _build_command(self, worker: dict, instruction: str, info: RuntimeInfo) -> tuple[list[str], dict[str, str]]:
        session_key = self._read_session_key(worker["worker_id"])
        model = worker.get("model") or self.resolve_model(worker.get("profile", "claude-code"))
        permission_mode = os.environ.get("WPR_CLAUDE_CODE_PERMISSION_MODE", "bypassPermissions")
        command = [
            self.binary,
            "-p",
            "--permission-mode",
            permission_mode,
            "--output-format",
            "stream-json",
            "--model",
            model,
        ]
        command.append("--verbose")
        if self._chrome_enabled():
            command.insert(2, "--chrome")
        effort = self._effort_for_worker(worker)
        if effort == "max":
            command.extend(["--effort", effort])
        elif effort and effort != "default":
            logger.warning(
                "Ignoring unsupported Claude Code effort",
                extra={"worker_id": str(worker.get("worker_id") or ""), "effort": effort},
            )
        if session_key and not session_key.startswith("claude-worker:"):
            command.extend(["--resume", session_key])
        env = self._container_env_for_worker(
            worker,
            "ANTHROPIC_API_KEY",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_CUSTOM_HEADERS",
            "ANTHROPIC_DEFAULT_SONNET_MODEL",
            "ANTHROPIC_DEFAULT_OPUS_MODEL",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL",
            "HTTPS_PROXY",
            "HTTP_PROXY",
            "NO_PROXY",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
        )
        use_api_key = os.environ.get("WPR_CLAUDE_CODE_USE_API_KEY", "0").strip().lower() in {"1", "true", "yes", "on"}
        if not use_api_key:
            env.pop("ANTHROPIC_API_KEY", None)
        return command, env

    def _parse_output(self, worker: dict, stdout: str, stderr: str, info: RuntimeInfo) -> tuple[str | None, str]:
        raw = stdout.strip()
        if not raw:
            if getattr(self, "_conversation_mode_from_worker", lambda _worker: False)(worker):
                return info.session_key, "The harness completed without a user-facing response."
            return info.session_key, (stderr.strip() or "")[-4000:]
        stream_events: list[dict[str, object]] = []
        for line in raw.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                stream_events.append(event)
        if getattr(self, "_conversation_mode_from_worker", lambda _worker: False)(worker) or len(stream_events) > 1:
            session_key = info.session_key
            assistant_parts: list[str] = []
            result_parts: list[str] = []
            structured_parts: list[str] = []
            for event in stream_events:
                maybe_session = str(event.get("session_id") or "").strip()
                if maybe_session:
                    session_key = maybe_session
                if str(event.get("type") or "") == "result":
                    structured = event.get(
                        "structured_output", event.get("structuredOutput")
                    )
                    if isinstance(structured, dict):
                        structured_parts.append(
                            json.dumps(
                                structured,
                                sort_keys=True,
                                separators=(",", ":"),
                            )
                        )
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
            selected = _select_user_facing_agent_output(
                structured_parts or result_parts or assistant_parts
            )
            return session_key, selected or "The harness completed without a user-facing response."
        try:
            payload = json.loads(raw.splitlines()[-1])
        except json.JSONDecodeError:
            return info.session_key, raw[-4000:]
        session_key = str(payload.get("session_id") or info.session_key or "").strip() or None
        result = str(payload.get("result") or raw).strip()
        return session_key, _select_user_facing_agent_output([result]) or result


_SECRET_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"/Users/[^/\s\"'`]+(?:/[^\s\"'`]*)*"), "[REDACTED_LOCAL_PATH]"),
    (re.compile(r"~/[^\s\"'`]+(?:/[^\s\"'`]*)*"), "[REDACTED_LOCAL_PATH]"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{12,}"), r"\1[REDACTED]"),
    (re.compile(r"(?i)((?:api[_-]?key|token|secret|password|passwd|pwd)\s*[:=]\s*)[^\s\"']{6,}"), r"\1[REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"), "sk-[REDACTED]"),
    (re.compile(r"\b[A-Za-z0-9_]{8,}:[A-Za-z0-9_./+=-]{20,}\b"), "[REDACTED_CREDENTIAL]"),
    (re.compile(r"(?i)data:image/[a-z0-9.+-]+;base64,[A-Za-z0-9+/=\s]{256,}"), "[REDACTED_IMAGE_BASE64]"),
    (re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{512,}={0,2}(?![A-Za-z0-9+/=])"), "[REDACTED_LONG_BASE64]"),
)
_HOST_RUN_OUTPUT_MAX_CHARS = 64000


def _select_user_facing_agent_output(output_parts: list[str]) -> str:
    """Prefer an explicit final report; otherwise use the latest assistant result."""
    cleaned = [part.strip() for part in output_parts if str(part or "").strip()]
    if not cleaned:
        return ""
    for part in reversed(cleaned):
        marker_matches = list(FINAL_REPORT_PATTERN.finditer(part))
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
        path.parent.mkdir(parents=True, exist_ok=True)
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
        _atomic_write_private_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
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


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip().lower()).strip("-._")
    return slug[:64] or "project"


class HostNativeCliMixin:
    execution_mode = "host"
    worker_root_name = "host_cli_runtime"

    def _host_active_slots(self) -> dict[str, object]:
        slots = self.__dict__.get("_viventium_host_active_slots")
        if not isinstance(slots, dict):
            slots = {}
            self.__dict__["_viventium_host_active_slots"] = slots
        return slots

    def _host_worker_lanes(self) -> dict[str, str]:
        lanes = self.__dict__.get("_viventium_host_worker_lanes")
        if not isinstance(lanes, dict):
            lanes = {}
            self.__dict__["_viventium_host_worker_lanes"] = lanes
        return lanes

    def _host_capacity_lane(self, worker: dict | None) -> str:
        return "conversation" if self._conversation_mode_from_worker(worker) else "mission"

    def host_active_process_status(self, worker: dict) -> dict[str, object]:
        active_session = self._read_active_session(
            str(worker.get("worker_id") or "")
        )
        if not active_session:
            return {"state": "absent"}
        try:
            pid = int(active_session.get("process_pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        start_identity = str(
            active_session.get("process_start_identity") or ""
        ).strip()
        run_id = str(active_session.get("run_id") or "").strip()
        if pid <= 0 or not start_identity:
            return {"state": "uncertain", "run_id": run_id}
        if not self._pid_is_live(pid) or self._pid_is_zombie(pid):
            return {"state": "absent", "run_id": run_id}
        current_identity = self._process_start_identity(pid)
        if not current_identity:
            return {"state": "uncertain", "run_id": run_id}
        if current_identity != start_identity:
            return {"state": "absent", "run_id": run_id}
        return {
            "state": "active",
            "run_id": run_id,
            "pid": pid,
            "process_start_identity": start_identity,
        }

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
        return (
            "conversation"
            if str(worker.get("trusted_run_lane") or "").strip().lower()
            == "conversation"
            else "mission"
        )

    def _conversation_mode_from_worker(self, worker: dict | None) -> bool:
        return self._run_mode_from_worker(worker) == "conversation"

    def _conversation_evidence_workspace(self, worker: dict, run_id: str) -> Path:
        path = self._run_root(str(worker["worker_id"]), run_id) / "private-evidence"
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o700)
        return path

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
        worker_suffix = _safe_slug(str(worker.get("worker_id") or "worker"))
        return root / self._agent_type() / f"{date_prefix}-{slug}-{worker_suffix}"

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

    def _agent_builder_output_schema(self, worker: dict) -> dict[str, object] | None:
        if not self._conversation_mode_from_worker(worker):
            return None
        bundle = self._bootstrap_bundle_for_worker(worker)
        schema = graph_transfer_output_schema(bundle.get("agent_builder_control"))
        return schema if isinstance(schema, dict) else None

    def _agent_builder_output_schema_path(
        self,
        worker: dict,
    ) -> Path | None:
        schema = self._agent_builder_output_schema(worker)
        if not schema:
            return None
        path = self._state_dir(str(worker["worker_id"])) / "agent-builder-output-schema.json"
        _atomic_write_private_text(
            path,
            json.dumps(schema, separators=(",", ":"), sort_keys=True),
        )
        return path

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
        native_web_locked = _host_native_web_access() == "disabled"
        preserve_names = (
            set() if native_web_locked else self._host_codex_native_mcp_allowlist() - append_names
        )
        source_config_path = self._source_host_codex_home() / "config.toml"
        preserved = ""
        if (
            not native_web_locked
            and source_config_path.exists()
            and source_config_path.is_file()
        ):
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
        if (
            not denied_plugins
            and personality == "inherit"
            and developer_instructions is None
        ):
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
            self._copy_host_codex_auth(codex_home)

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
            self._copy_host_codex_auth(codex_home)
            return

        if profile != "claude-code":
            return
        claude_home = self._home_dir(str(worker["worker_id"])) / ".claude"
        claude_home.mkdir(parents=True, exist_ok=True)
        claude_home.chmod(0o700)
        project_mcp = bundle.get("claude_project_mcp")
        state_dir = self._state_dir(str(worker["worker_id"]))
        state_dir.mkdir(parents=True, exist_ok=True)
        state_dir.chmod(0o700)
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
        env.setdefault("PATH", "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin")
        env.setdefault("SHELL", os.environ.get("SHELL", "/bin/zsh"))
        env.update(bootstrap_env_for(worker))
        # Service-only authority must never cross into a mission process even if
        # a deployment accidentally lists it in a worker bootstrap allowlist.
        env.pop("VIVENTIUM_GLASSHIVE_SERVICE_ASSERTION_SECRET", None)
        env.pop("VIVENTIUM_GLASSHIVE_ADMISSION_URL", None)
        env.pop("VIVENTIUM_GLASSHIVE_ADMISSION_SECRET", None)
        worker_id = str(worker.get("worker_id") or "unknown")
        home = self._home_dir(worker_id)
        isolated_dirs = {
            "HOME": home,
            "CODEX_HOME": home / ".codex",
            "CLAUDE_CONFIG_DIR": home / ".claude",
            "TMPDIR": home / ".tmp",
            "XDG_CACHE_HOME": home / ".cache",
            "XDG_CONFIG_HOME": home / ".config",
            "XDG_STATE_HOME": home / ".local" / "state",
            "GLASSHIVE_LOG_DIR": self._state_dir(worker_id) / "logs",
        }
        for key, path in isolated_dirs.items():
            path.mkdir(parents=True, exist_ok=True)
            try:
                path.chmod(0o700)
            except OSError:
                pass
            env[key] = str(path)
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

    def _host_control_receipt_path(self, worker_id: str) -> Path:
        return self._state_dir(worker_id) / "host_control_receipt.json"

    def _read_host_control_receipt(
        self, worker_id: str
    ) -> dict[str, object] | None:
        path = self._host_control_receipt_path(worker_id)
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _write_host_control_receipt(
        self,
        worker: dict,
        *,
        active_session: dict[str, object],
        run_id: str,
        operation: str,
        confirmed: bool,
    ) -> dict[str, object]:
        """Persist exact host-control intent before signaling its process.

        The receipt contains only runtime identity/evidence metadata already
        present in the private active-session record.  It lets crash recovery
        distinguish a proven-dead exact generation from an empty identity.
        """

        worker_id = str(worker["worker_id"])
        prior = self._read_host_control_receipt(worker_id) or {}
        same_generation = bool(
            str(prior.get("run_id") or "") == run_id
            and str(prior.get("operation") or "") == operation
            and str(prior.get("process_start_identity") or "")
            == str(active_session.get("process_start_identity") or "")
        )
        status = (
            "confirmed"
            if confirmed or (same_generation and prior.get("status") == "confirmed")
            else "requested"
        )
        payload: dict[str, object] = {
            "version": 1,
            "worker_id": worker_id,
            "run_id": run_id,
            "operation": operation,
            "status": status,
            "session_name": str(active_session.get("session_name") or ""),
            "process_pid": active_session.get("process_pid"),
            "process_group": active_session.get("process_group"),
            "process_start_identity": str(
                active_session.get("process_start_identity") or ""
            ),
            "requested_at": str(prior.get("requested_at") or "") or _utc_iso(),
            "confirmed_at": _utc_iso() if status == "confirmed" else "",
            "session": dict(active_session),
        }
        _atomic_write_private_text(
            self._host_control_receipt_path(worker_id),
            json.dumps(payload, indent=2, sort_keys=True),
        )
        return payload

    @staticmethod
    def _host_control_generation_matches(
        *,
        worker: dict,
        lease: dict,
        session: dict[str, object],
        run_id: str,
    ) -> bool:
        try:
            lease_pid = int(lease.get("pid") or 0)
            lease_group = int(lease.get("process_group") or 0)
            session_pid = int(session.get("process_pid") or 0)
            session_group = int(session.get("process_group") or 0)
        except (TypeError, ValueError):
            return False
        return bool(
            str(lease.get("worker_id") or "") == str(worker["worker_id"])
            and str(lease.get("run_id") or "") == run_id
            and str(lease.get("status") or "") == "active"
            and str(lease.get("startup_state") or "") == "confirmed"
            and str(lease.get("startup_identity_kind") or "") == "host_process"
            and str(session.get("run_id") or "") == run_id
            and str(session.get("session_name") or "")
            == str(lease.get("startup_session_id") or "")
            and lease_pid > 0
            and lease_pid == session_pid
            and lease_group > 0
            and lease_group == session_group
            and str(lease.get("process_start_identity") or "").strip()
            and str(lease.get("process_start_identity") or "").strip()
            == str(session.get("process_start_identity") or "").strip()
        )

    def _confirmed_host_control_session(
        self,
        worker: dict,
        *,
        run_id: str,
        operation: str,
    ) -> dict[str, object]:
        """Bind a host control to the service-confirmed lease and session file."""

        lease = worker.get("_host_run_lease")
        active_session = self._read_active_session(str(worker["worker_id"]))
        if not isinstance(lease, dict):
            raise RuntimeErrorBase(
                "The exact host process identity is not confirmed"
            )
        if isinstance(active_session, dict):
            if not self._host_control_generation_matches(
                worker=worker,
                lease=lease,
                session=active_session,
                run_id=run_id,
            ):
                raise RuntimeErrorBase(
                    "The exact host process identity is not confirmed"
                )
            return active_session

        receipt = self._read_host_control_receipt(str(worker["worker_id"]))
        receipt_session = (
            receipt.get("session") if isinstance(receipt, dict) else None
        )
        if (
            not isinstance(receipt, dict)
            or not isinstance(receipt_session, dict)
            or str(receipt.get("operation") or "") != operation
            or str(receipt.get("status") or "") not in {"requested", "confirmed"}
            or not self._host_control_generation_matches(
                worker=worker,
                lease=lease,
                session=receipt_session,
                run_id=run_id,
            )
        ):
            raise RuntimeErrorBase(
                "The exact host process identity is not confirmed"
            )
        return dict(receipt_session)

    def pause_worker(self, worker: dict) -> RuntimeInfo:
        run_id = str(worker.get("_active_run_id") or "").strip()
        active_session = self._confirmed_host_control_session(
            worker, run_id=run_id, operation="pause_run"
        )
        self._write_host_control_receipt(
            worker,
            active_session=active_session,
            run_id=run_id,
            operation="pause_run",
            confirmed=False,
        )
        self._note_stop_reason(worker["worker_id"], "paused")
        confirmed = self._stop_active_process(
            worker["worker_id"], worker=worker, run_id=run_id
        )
        if not confirmed:
            raise RuntimeErrorBase("Host run termination could not be confirmed")
        self._write_host_control_receipt(
            worker,
            active_session=active_session,
            run_id=run_id,
            operation="pause_run",
            confirmed=True,
        )
        self._write_stopped_active_run_evidence(
            worker,
            active_session=active_session,
            stop_reason="paused",
            error_text="Worker was paused by the operator",
        )
        self._append_work_log(worker, "Worker paused by operator.")
        return self._host_runtime_info(worker, pid=None)

    def interrupt_worker(self, worker: dict, run_id: str | None = None) -> RuntimeInfo:
        exact_run_id = str(run_id or worker.get("_active_run_id") or "").strip()
        operation = (
            "steer_run"
            if str(worker.get("compute_release_kind") or "") == "steer_run"
            else "interrupt_run"
        )
        active_session = self._confirmed_host_control_session(
            worker, run_id=exact_run_id, operation=operation
        )
        self._write_host_control_receipt(
            worker,
            active_session=active_session,
            run_id=exact_run_id,
            operation=operation,
            confirmed=False,
        )
        self._note_stop_reason(worker["worker_id"], "interrupted", run_id=run_id)
        confirmed = self._stop_active_process(
            worker["worker_id"], worker=worker, run_id=exact_run_id
        )
        if not confirmed:
            raise RuntimeErrorBase("Host run termination could not be confirmed")
        self._write_host_control_receipt(
            worker,
            active_session=active_session,
            run_id=exact_run_id,
            operation=operation,
            confirmed=True,
        )
        self._write_stopped_active_run_evidence(
            worker,
            active_session=active_session,
            stop_reason="interrupted",
            error_text="Worker run was interrupted by the operator",
        )
        self._append_work_log(worker, "Active run interrupted by operator.")
        pending_pid = None
        return self._host_runtime_info(worker, pid=pending_pid)

    def terminate_worker(self, worker: dict) -> RuntimeInfo:
        run_id = str(worker.get("_active_run_id") or "").strip()
        operation = str(worker.get("compute_release_kind") or "").strip() or "terminate_worker"
        active_session = (
            self._confirmed_host_control_session(
                worker, run_id=run_id, operation=operation
            )
            if run_id
            else self._read_active_session(worker["worker_id"])
        )
        if not run_id and active_session:
            raise RuntimeErrorBase(
                "The exact host process identity is not confirmed"
            )
        if run_id and active_session:
            self._write_host_control_receipt(
                worker,
                active_session=active_session,
                run_id=run_id,
                operation=operation,
                confirmed=False,
            )
        self._note_stop_reason(worker["worker_id"], "terminated")
        confirmed = self._stop_active_process(
            worker["worker_id"], worker=worker, run_id=run_id or None
        )
        if not confirmed:
            raise RuntimeErrorBase("Host run termination could not be confirmed")
        if run_id and active_session:
            self._write_host_control_receipt(
                worker,
                active_session=active_session,
                run_id=run_id,
                operation=operation,
                confirmed=True,
            )
        self._write_stopped_active_run_evidence(
            worker,
            active_session=active_session,
            stop_reason="terminated",
            error_text="Worker was terminated by the operator",
        )
        self._append_work_log(worker, "Worker terminated by operator.")
        return self._host_runtime_info(worker, pid=None)

    def reconcile_worker(self, worker: dict) -> RuntimeInfo:
        return self._host_runtime_info(
            worker,
            pid=self._active_pid(
                worker["worker_id"],
                str(worker.get("_active_run_id") or "").strip() or None,
            ),
        )

    def host_process_identity(self, worker: dict, run_id: str) -> dict[str, object] | None:
        active_session = self._read_active_session(str(worker.get("worker_id") or ""))
        if str((active_session or {}).get("run_id") or "") != str(run_id):
            return None
        try:
            pid = int((active_session or {}).get("process_pid") or 0)
            process_group = int((active_session or {}).get("process_group") or 0)
        except (TypeError, ValueError):
            return None
        start_identity = str(
            (active_session or {}).get("process_start_identity") or ""
        ).strip()
        verified = self._recorded_process_is_running(pid, start_identity)
        if not verified:
            return None
        return {
            "identity_kind": "host_process",
            "pid": pid,
            "process_group": process_group or pid,
            "process_start_identity": start_identity,
            "container_id": "",
            "session_id": str((active_session or {}).get("session_name") or ""),
            "startup_token_digest": str(
                (active_session or {}).get("startup_token_digest") or ""
            ),
            "verified": True,
        }

    def cleanup_orphaned_run(self, worker: dict, run_id: str) -> RuntimeInfo:
        worker_id = str(worker["worker_id"])
        active_session = self._read_active_session(worker_id)
        if str((active_session or {}).get("run_id") or "").strip() != str(run_id):
            return self._host_runtime_info(worker, pid=None)
        try:
            process_pid = int((active_session or {}).get("process_pid") or 0)
        except (TypeError, ValueError):
            process_pid = 0
        if process_pid > 0 and process_pid != os.getpid() and self._pid_is_live(process_pid):
            try:
                process_group = os.getpgid(process_pid)
                if process_group != os.getpgrp():
                    os.killpg(process_group, signal.SIGTERM)
                else:
                    os.kill(process_pid, signal.SIGTERM)
            except OSError:
                pass
        self._clear_active_session(worker_id, expected_session=active_session)
        self._release_host_slot(worker_id)
        return self._host_runtime_info(worker, pid=None)

    def _stop_active_process(
        self,
        worker_id: str,
        *,
        worker: dict | None = None,
        run_id: str | None = None,
    ) -> bool:
        active_session = self._read_active_session(worker_id)
        lease = worker.get("_host_run_lease") if isinstance(worker, dict) else None
        if (
            active_session
            and run_id
            and str(active_session.get("run_id") or "") != run_id
        ):
            return False
        if (
            isinstance(lease, dict)
            and run_id
            and str(lease.get("run_id") or "") != run_id
        ):
            return False
        with self._process_lock:
            process = self._active_processes.get(worker_id)
        local_process = process if process and process.poll() is None else None

        try:
            recorded_pid = int(
                (active_session or {}).get("process_pid")
                or (lease or {}).get("pid")
                or 0
            )
            recorded_group = int(
                (active_session or {}).get("process_group")
                or (lease or {}).get("process_group")
                or 0
            )
        except (TypeError, ValueError):
            recorded_pid = 0
            recorded_group = 0
        recorded_identity = str(
            (active_session or {}).get("process_start_identity")
            or (lease or {}).get("process_start_identity")
            or ""
        ).strip()

        if local_process is not None:
            target_pid = local_process.pid
            target_identity = recorded_identity or self._process_start_identity(target_pid)
        elif recorded_pid > 0 and recorded_identity:
            if not self._recorded_process_is_running(recorded_pid, recorded_identity):
                if active_session and not self._clear_active_session(
                    worker_id, expected_session=active_session
                ):
                    raise RuntimeErrorBase(
                        "Host run ownership changed during exact-run stop"
                    )
                self._release_host_slot(worker_id)
                return True
            target_pid = recorded_pid
            target_identity = recorded_identity
        elif active_session or lease:
            # A foreign process may only act on a persisted PID when the PID's
            # start identity is present. Legacy PID-only records fail closed.
            return False
        else:
            return True

        def signal_process_group(sig: signal.Signals) -> None:
            try:
                process_group = recorded_group or os.getpgid(target_pid)
                if process_group != os.getpgrp():
                    os.killpg(process_group, sig)
                else:
                    os.kill(target_pid, sig)
            except OSError:
                pass

        try:
            signal_process_group(signal.SIGTERM)
            if local_process is not None:
                try:
                    local_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    signal_process_group(signal.SIGKILL)
                    try:
                        local_process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
                confirmed = local_process.poll() is not None
            else:
                confirmed = self._wait_for_recorded_process_exit(
                    target_pid, target_identity, timeout=5
                )
                if not confirmed:
                    signal_process_group(signal.SIGKILL)
                    confirmed = self._wait_for_recorded_process_exit(
                        target_pid, target_identity, timeout=2
                    )
        finally:
            if local_process is not None and local_process.poll() is not None:
                self._clear_process(worker_id, expected_process=local_process)
        if confirmed:
            if active_session and not self._clear_active_session(
                worker_id, expected_session=active_session
            ):
                raise RuntimeErrorBase(
                    "Host run ownership changed during exact-run stop"
                )
            self._release_host_slot(worker_id)
        return confirmed

    def _acquire_host_slot(self, worker: dict) -> None:
        worker_id = worker["worker_id"]
        lane = self._host_capacity_lane(worker)
        with self._process_lock:
            error = self._host_capacity_error_locked(worker_id, lane)
            if error is not None:
                raise error
            active = self._host_active_slots().get(lane)
            if isinstance(active, set):
                workers = active
            elif isinstance(active, (list, tuple)):
                workers = {str(item) for item in active if str(item)}
            elif active:
                workers = {str(active)}
            else:
                workers = set()
            workers.add(worker_id)
            self._host_active_slots()[lane] = workers
            self._host_worker_lanes()[worker_id] = lane

    def _host_capacity_error_locked(
        self,
        worker_id: str,
        lane: str = "mission",
    ) -> RuntimeErrorBase | None:
        raw_active = self._host_active_slots().get(lane)
        if isinstance(raw_active, set):
            candidates = set(raw_active)
        elif isinstance(raw_active, (list, tuple)):
            candidates = {str(item) for item in raw_active if str(item)}
        elif raw_active:
            candidates = {str(raw_active)}
        else:
            candidates = set()
        live: set[str] = set()
        for candidate in candidates:
            process = self._active_processes.get(candidate)
            if process is None or process.poll() is None:
                live.add(candidate)
        live.discard(worker_id)
        limit_name = (
            "WPR_HOST_CONVERSATION_SLOTS_PER_CLI"
            if lane == "conversation"
            else "WPR_HOST_MISSION_SLOTS_PER_CLI"
        )
        default = 2 if lane == "conversation" else 3
        try:
            limit = max(1, int(str(os.environ.get(limit_name, default))))
        except ValueError:
            limit = default
        if len(live) >= limit:
            return HostCapacityError(
                f"Host-native {self.runtime_name} {lane} lane is at capacity.",
                capacity_class="family_lane",
            )
        return None

    def worker_capacity_error(self, worker: dict) -> RuntimeErrorBase | None:
        with self._process_lock:
            return self._host_capacity_error_locked(
                str(worker["worker_id"]),
                self._host_capacity_lane(worker),
            )

    def _release_host_slot(self, worker_id: str) -> None:
        with self._process_lock:
            lane = self._host_worker_lanes().pop(worker_id, None)
            if lane:
                raw_active = self._host_active_slots().get(lane)
                if isinstance(raw_active, set):
                    raw_active.discard(worker_id)
                    if not raw_active:
                        self._host_active_slots().pop(lane, None)
                elif raw_active == worker_id:
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
        started_at_iso = _utc_iso()
        heartbeat_path = run_root / "active-run.json"
        heartbeat_stop = Event()
        heartbeat_thread: Thread | None = None
        transcript_paths = {
            "stdout": str(raw_stdout),
            "stderr": str(raw_stderr),
            "exit_code": str(exit_path),
        }
        scrubber = DurableSecretScrubber(
            exact_values=(env.get("GLASSHIVE_CAPABILITY_BROKER_TOKEN", ""),)
        )
        process: subprocess.Popen[str] | None = None
        owned_session: dict[str, object] | None = None
        startup_cleanup_unconfirmed = False
        capture_threads: list[Thread] = []
        capture_errors: list[BaseException] = []
        try:
            with raw_stdout.open("w") as stdout_handle, raw_stderr.open("w") as stderr_handle:
                raw_stdout.chmod(0o600)
                raw_stderr.chmod(0o600)
                process = subprocess.Popen(
                    command,
                    cwd=str(workspace),
                    env=env,
                    text=True,
                    stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=1,
                    start_new_session=True,
                )
                if process.stdout is None or process.stderr is None:  # pragma: no cover
                    raise RuntimeErrorBase("Provider transcript pipes were not created")
                capture_threads = [
                    Thread(
                        target=_drain_scrubbed_provider_stream,
                        args=(process.stdout, stdout_handle, scrubber, capture_errors),
                        name=f"glasshive-conversation-stdout-{effective_run_id[:12]}",
                        daemon=True,
                    ),
                    Thread(
                        target=_drain_scrubbed_provider_stream,
                        args=(process.stderr, stderr_handle, scrubber, capture_errors),
                        name=f"glasshive-conversation-stderr-{effective_run_id[:12]}",
                        daemon=True,
                    ),
                ]
                for capture_thread in capture_threads:
                    capture_thread.start()
                self._register_process(str(worker["worker_id"]), process)
                owned_session = {
                    "session_name": f"conversation-{effective_run_id[:12]}",
                    "run_id": effective_run_id,
                    "stdout_path": str(raw_stdout),
                    "stderr_path": str(raw_stderr),
                    "exit_path": str(exit_path),
                    "model": str(info.model or ""),
                    "argv_for_evidence_json": json.dumps(
                        [_redact_command_arg(part) for part in command]
                    ),
                    "started_at": started_at_iso,
                    "process_pid": process.pid,
                    "process_group": self._process_group_identity(process.pid),
                    "process_start_identity": self._process_start_identity(process.pid),
                    "owner_pid": os.getpid(),
                    "heartbeat_path": str(heartbeat_path),
                    "timeout_seconds": run_timeout_sec,
                    "instruction": instruction,
                    "run_mode": "conversation",
                }
                try:
                    self._write_active_session(
                        str(worker["worker_id"]),
                        owned_session,
                        publish_run_start=True,
                        worker=worker,
                        spawned_process=process,
                    )
                except RunStartupRejectedError as exc:
                    startup_cleanup_unconfirmed = not exc.termination_confirmed
                    raise
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
                    if stdin_text is not None:
                        try:
                            if process.stdin is not None:
                                process.stdin.write(stdin_text)
                                process.stdin.flush()
                        except BrokenPipeError:
                            pass
                        finally:
                            if process.stdin is not None:
                                process.stdin.close()
                    exit_code = process.wait(timeout=run_timeout_sec)
                except subprocess.TimeoutExpired as exc:
                    self._note_stop_reason(str(worker["worker_id"]), "terminated", run_id=effective_run_id)
                    self._stop_active_process(str(worker["worker_id"]), worker=worker, run_id=effective_run_id)
                    _write_active_run_status(
                        path=heartbeat_path,
                        worker=worker,
                        run_id=effective_run_id,
                        runtime_name=self.runtime_name,
                        model=str(info.model or ""),
                        state="timeout",
                        transcript_paths=transcript_paths,
                        started_at=started_at_iso,
                        process_pid=None,
                        timeout_seconds=run_timeout_sec,
                        stop_reason="timeout",
                    )
                    raise RuntimeErrorBase(f"{self.runtime_name} timed out after {run_timeout_sec:g}s") from exc
                finally:
                    for capture_thread in capture_threads:
                        capture_thread.join(timeout=2)
                if capture_errors:
                    raise RuntimeErrorBase(
                        f"Could not safely capture {self.runtime_name} provider output"
                    ) from capture_errors[0]

            exit_path.write_text(str(exit_code))
            exit_path.chmod(0o600)
            stdout = raw_stdout.read_text() if raw_stdout.exists() else ""
            stderr = raw_stderr.read_text() if raw_stderr.exists() else ""
            self._finalize_stop_reason(str(worker["worker_id"]), run_id=effective_run_id)
            if exit_code != 0:
                detail = (_redact_text(stderr, max_chars=2000) or _redact_text(stdout, max_chars=2000)).strip()
                _write_active_run_status(
                    path=heartbeat_path,
                    worker=worker,
                    run_id=effective_run_id,
                    runtime_name=self.runtime_name,
                    model=str(info.model or ""),
                    state="failed",
                    transcript_paths=transcript_paths,
                    started_at=started_at_iso,
                    process_pid=None,
                    timeout_seconds=run_timeout_sec,
                    exit_code=exit_code,
                    stop_reason="process_exit",
                )
                self._write_action_audit(
                    worker,
                    {
                        "kind": "conversation.failed",
                        "run_id": effective_run_id,
                        "exit_code": exit_code,
                        "detail": detail,
                    },
                )
                raise _provider_process_exit_error(
                    runtime_name=self.runtime_name,
                    exit_code=exit_code,
                    stdout=stdout,
                    stderr=stderr,
                    message=f"{self.runtime_name} exited with code {exit_code}: {detail}",
                )

            session_key, output = self._parse_output(worker, stdout, stderr, info)
            if session_key:
                self._write_session_key(str(worker["worker_id"]), session_key)
            redacted_output = _redact_text(str(output or "").strip())
            if len(redacted_output) > _HOST_RUN_OUTPUT_MAX_CHARS:
                redacted_output = f"{redacted_output[: _HOST_RUN_OUTPUT_MAX_CHARS - 3].rstrip()}..."
            _write_active_run_status(
                path=heartbeat_path,
                worker=worker,
                run_id=effective_run_id,
                runtime_name=self.runtime_name,
                model=str(info.model or ""),
                state="completed",
                transcript_paths=transcript_paths,
                started_at=started_at_iso,
                process_pid=None,
                timeout_seconds=run_timeout_sec,
                exit_code=exit_code,
                stop_reason="process_exit",
            )
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
        finally:
            if not startup_cleanup_unconfirmed:
                if process is not None and process.poll() is None:
                    self._note_stop_reason(
                        str(worker["worker_id"]),
                        "terminated",
                        run_id=effective_run_id,
                    )
                    self._stop_active_process(
                        str(worker["worker_id"]),
                        worker=worker,
                        run_id=effective_run_id,
                    )
                heartbeat_stop.set()
                if heartbeat_thread:
                    heartbeat_thread.join(timeout=1)
                for capture_thread in capture_threads:
                    capture_thread.join(timeout=1)
                _scrub_provider_owned_artifacts(
                    state_dir=self._state_dir(str(worker["worker_id"])),
                    home_dir=self._home_dir(str(worker["worker_id"])),
                    run_root=run_root,
                    workspace=workspace,
                    scrubber=scrubber,
                )
                if process is not None:
                    self._clear_process(
                        str(worker["worker_id"]), expected_process=process
                    )
                self._release_host_slot(str(worker["worker_id"]))
                if owned_session is not None:
                    current_session = self._read_active_session(
                        str(worker["worker_id"])
                    )
                    if (
                        current_session
                        and str(current_session.get("run_id") or "")
                        == effective_run_id
                    ):
                        owned_session = current_session
                    self._clear_active_session(
                        str(worker["worker_id"]),
                        expected_session=owned_session,
                    )

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
        native_session_stop = Event()
        native_session_thread: Thread | None = None
        with raw_stdout.open("w") as stdout_handle, raw_stderr.open("w") as stderr_handle:
            raw_stdout.chmod(0o600)
            raw_stderr.chmod(0o600)
            process = subprocess.Popen(
                command,
                cwd=str(workspace),
                env=env,
                text=True,
                stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                stdout=stdout_handle,
                stderr=stderr_handle,
                start_new_session=True,
            )
            self._register_process(worker["worker_id"], process)
            process_pid = process.pid
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
                    "process_group": self._process_group_identity(process.pid),
                    "process_start_identity": self._process_start_identity(process.pid),
                    "owner_pid": os.getpid(),
                    "heartbeat_path": str(heartbeat_path),
                    "timeout_seconds": run_timeout_sec,
                    "instruction": instruction,
                },
                publish_run_start=True,
                worker=worker,
                spawned_process=process,
            )
            native_session_thread = Thread(
                target=self._observe_native_session_events,
                args=(worker["worker_id"], raw_stdout, native_session_stop, effective_run_id),
                name=f"glasshive-native-session-{effective_run_id[:12]}",
                daemon=True,
            )
            native_session_thread.start()
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
                if stdin_text is not None:
                    process.communicate(stdin_text, timeout=run_timeout_sec)
                    exit_code = process.returncode
                else:
                    exit_code = process.wait(timeout=run_timeout_sec)
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
                heartbeat_stop.set()
                if heartbeat_thread:
                    heartbeat_thread.join(timeout=1)
                native_session_stop.set()
                if native_session_thread:
                    native_session_thread.join(timeout=1)
                self._clear_process(worker["worker_id"], expected_process=process)
                self._release_host_slot(worker["worker_id"])

        exit_path.write_text(str(exit_code))
        exit_path.chmod(0o600)
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
            raise _provider_process_exit_error(
                runtime_name=self.runtime_name,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                message=f"{self.runtime_name} exited with code {exit_code}: {detail}",
            )

        session_key, output = self._parse_output(worker, stdout, stderr, info)
        if session_key:
            self._write_session_key(worker["worker_id"], session_key)
        if FINAL_REPORT_PATTERN.search(stdout) and not FINAL_REPORT_PATTERN.search(output):
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
        command = ["bash", "-lc", f"cd {shlex.quote(str(info.workspace_dir or ''))} && tail -n 80 -f {shlex.quote(stdout)}"] if stdout else ["bash", "-lc", f"cd {shlex.quote(str(info.workspace_dir or ''))} && exec ${SHELL:-/bin/bash}"]
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


def _codex_binary_with_discoverable_companion(binary: str) -> str:
    """Resolve a Codex symlink only when its canonical bundle carries the required host."""
    resolved = shutil.which(binary)
    if not resolved:
        return binary
    invoked = Path(resolved)
    if not invoked.is_symlink():
        return binary
    canonical = invoked.resolve()
    companion = canonical.parent / "codex-code-mode-host"
    if (
        canonical.is_file()
        and os.access(canonical, os.X_OK)
        and companion.is_file()
        and os.access(companion, os.X_OK)
    ):
        return str(canonical)
    return binary


class HostCodexCliRuntime(HostNativeCliMixin, CodexCliRuntime):
    worker_root_name = "host_codex_cli_runtime"
    binary_env_var = "WPR_CODEX_BIN"

    def __init__(self, base_dir: str | None = None) -> None:
        super().__init__(base_dir=base_dir)
        # Standalone GlassHive installs may not pass through Viventium's config
        # compiler. Keep the runtime's own host-worker boundary capability-aware.
        self.binary = _codex_binary_with_discoverable_companion(self.binary)

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
        # An explicit local host Codex worker has an isolated CODEX_HOME, so it
        # needs the same owner-local login baseline even when no optional MCP,
        # personality, or developer-instruction bundle was supplied.  Automatic
        # Parallel work is Docker-only and enterprise deployments must project
        # server-owned credentials instead of copying host authority.
        if not (
            self._env_flag("GLASSHIVE_ENTERPRISE_MODE", False)
            or self._env_flag("WPR_ENTERPRISE_MODE", False)
        ):
            self._copy_host_codex_auth(self._host_codex_home(worker))
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
        existing_session = self._resumable_codex_session_key(worker)
        model = self._codex_model_for_worker(worker, "WPR_MODEL_HOST_CODEX_CLI")
        is_resume = bool(existing_session)
        dangerous_mode = os.environ.get("WPR_CODEX_DANGEROUS", "1").strip().lower() in {"1", "true", "yes", "on"}
        access_mode = "full"
        if self._conversation_mode_from_worker(worker):
            bundle = self._bootstrap_bundle_for_worker(worker)
            access_mode = str(bundle.get("access_mode") or "full").strip().lower()
            dangerous_mode = access_mode == "full"
        read_only_mode = access_mode == "read_only"
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
        self._append_codex_reasoning_effort_config(command, worker)
        native_web_locked = _host_native_web_access() == "disabled"
        if native_web_locked:
            command.extend(["-c", 'web_search="disabled"'])
            for feature in _CODEX_NATIVE_WEB_LOCKDOWN_FEATURES:
                command.extend(["--disable", feature])
            command.extend(
                [
                    "-c",
                    f'sandbox_mode="{"read-only" if read_only_mode else "workspace-write"}"',
                    "-c",
                    'approval_policy="never"',
                ]
            )
            if not read_only_mode:
                command.extend(
                    ["-c", "sandbox_workspace_write.network_access=false"]
                )
        if native_web_locked:
            pass
        elif read_only_mode:
            command.extend(
                [
                    "-c",
                    'sandbox_mode="read-only"',
                    "-c",
                    'approval_policy="never"',
                ]
            )
        elif dangerous_mode:
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
        output_schema_path = self._agent_builder_output_schema_path(worker)
        if output_schema_path:
            command.extend(["--output-schema", str(output_schema_path)])
        if is_resume:
            command.append(existing_session)
        command.append("-")
        env = self._host_env(worker)
        codex_home = self._host_codex_home(worker)
        if conversation_mode or (codex_home / "config.toml").exists():
            env["CODEX_HOME"] = str(codex_home)
        return command, env


class HostClaudeCodeRuntime(HostNativeCliMixin, ClaudeCodeRuntime):
    worker_root_name = "host_claude_code_runtime"
    binary_env_var = "WPR_CLAUDE_CODE_BIN"

    def _agent_builder_output_schema(self, worker: dict) -> dict[str, object] | None:
        schema = super()._agent_builder_output_schema(worker)
        if not schema:
            return None
        # Claude Code validates an unqualified JSON Schema with its bundled
        # dialect. Supplying the canonical 2020-12 metaschema URI makes the CLI
        # reject the request before execution. Remove only that declaration;
        # every control-envelope constraint remains identical to Codex.
        projected_schema = dict(schema)
        projected_schema.pop("$schema", None)
        return projected_schema

    def _chrome_supported(self) -> bool:
        return self._help_supports("--chrome")

    def _effort_supported(self) -> bool:
        return self._help_supports("--effort")

    def _help_supports(self, flag: str) -> bool:
        resolved = shutil.which(self.binary)
        if not resolved:
            return False
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
            return False
        return flag in f"{completed.stdout}\n{completed.stderr}"

    def _requires_max_effort(self, worker: dict | None = None) -> bool:
        worker = worker or {}
        effort = (
            self._bootstrap_env_value(worker, "WPR_CLAUDE_CODE_EFFORT")
            or os.environ.get("WPR_CLAUDE_CODE_EFFORT", "")
        ).strip().lower()
        return effort == "max"

    def _raise_missing_effort_support(self, profile: str, execution_mode: str) -> None:
        raise RuntimeDependencyMissingError(
            "Claude Code workers requested an explicit effort level, but the configured Claude Code CLI "
            "does not expose the native --effort flag.",
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
        if os.environ.get("WPR_CLAUDE_CODE_EFFORT", "").strip().lower() == "max" and not self._effort_supported():
            self._raise_missing_effort_support(profile, execution_mode)
        if (
            _host_native_web_access() != "disabled"
            and self._chrome_enabled()
            and not self._chrome_supported()
        ):
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
        """Project access-only Claude auth without copying or refreshing user credentials."""
        env.pop("CLAUDE_CODE_OAUTH_REFRESH_TOKEN", None)
        projected_access_token = _usable_claude_oauth_token(
            env.get("CLAUDE_CODE_OAUTH_TOKEN")
        )
        if projected_access_token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = projected_access_token
            return

        explicit_access_token = _usable_explicit_claude_oauth_token()
        if explicit_access_token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = explicit_access_token
            return

        keychain_oauth = _read_claude_keychain_oauth()
        if _claude_keychain_access_token_is_fresh(keychain_oauth):
            env["CLAUDE_CODE_OAUTH_TOKEN"] = str(keychain_oauth.get("accessToken") or "").strip()
            return

        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        if _claude_cli_managed_auth_available(self.binary, child_env=env):
            return
        raise RuntimeErrorBase(
            "Claude Code authentication is unavailable for this host worker. "
            "Run `claude auth login` for managed local authentication or provision a supported "
            "headless token with `claude setup-token`, then try again."
        )

    def _build_command(self, worker: dict, instruction: str, info: RuntimeInfo) -> tuple[list[str], dict[str, str]]:
        session_key = self._read_session_key(worker["worker_id"])
        model = worker.get("model") or self.resolve_model(worker.get("profile", "claude-code"))
        native_web_locked = _host_native_web_access() == "disabled"
        permission_mode = os.environ.get("WPR_CLAUDE_CODE_PERMISSION_MODE", "bypassPermissions")
        if self._conversation_mode_from_worker(worker):
            bundle = self._bootstrap_bundle_for_worker(worker)
            access_mode = str(bundle.get("access_mode") or "full").strip().lower()
            if access_mode == "full":
                permission_mode = "bypassPermissions"
            elif access_mode == "read_only":
                permission_mode = "plan"
            else:
                permission_mode = "acceptEdits"
        if native_web_locked:
            permission_mode = "acceptEdits"
        output_format = "stream-json"
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
        command.append("--verbose")
        if self._conversation_mode_from_worker(worker):
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
        if native_web_locked:
            sandbox = settings.setdefault("sandbox", {})
            if isinstance(sandbox, dict):
                sandbox.update(
                    {
                        "enabled": True,
                        "failIfUnavailable": True,
                        "allowUnsandboxedCommands": False,
                        "network": {
                            "allowedDomains": [],
                            "strictAllowlist": True,
                        },
                    }
                )
            command.extend(["--setting-sources", ""])
        if settings:
            command.extend(
                ["--settings", json.dumps(settings, separators=(",", ":"))]
            )
        if native_web_locked:
            command.extend(["--disallowedTools", "WebSearch", "WebFetch"])
            command.insert(2, "--no-chrome")
        elif self._chrome_enabled():
            command.insert(2, "--chrome")
        effort = (
            self._bootstrap_env_value(worker, "WPR_CLAUDE_CODE_EFFORT")
            or os.environ.get("WPR_CLAUDE_CODE_EFFORT", "")
        ).strip().lower()
        if effort and effort != "default":
            if not self._effort_supported():
                self._raise_missing_effort_support(str(worker.get("profile") or "claude-code"), "host")
            command.extend(["--effort", effort])
        output_schema = self._agent_builder_output_schema(worker)
        if output_schema:
            command.extend(
                [
                    "--json-schema",
                    json.dumps(output_schema, separators=(",", ":"), sort_keys=True),
                ]
            )
        if session_key and not session_key.startswith("claude-worker:"):
            command.extend(["--resume", session_key])
        env = self._host_env(worker)
        env.pop("CLAUDE_CODE_OAUTH_REFRESH_TOKEN", None)
        use_api_key = os.environ.get("WPR_CLAUDE_CODE_USE_API_KEY", "0").strip().lower() in {"1", "true", "yes", "on"}
        if not use_api_key:
            env.pop("ANTHROPIC_API_KEY", None)
        enterprise_mode = any(
            str(os.environ.get(name, "")).strip().lower()
            in {"1", "true", "yes", "on"}
            for name in ("GLASSHIVE_ENTERPRISE_MODE", "WPR_ENTERPRISE_MODE")
        )
        if not enterprise_mode:
            # Every host Claude worker runs with an isolated CLAUDE_CONFIG_DIR.
            # Local mission roots therefore need the same access-only owner auth
            # projection as the conversation lane even when no optional bundle
            # exists. Automatic Parallel work remains Docker-only.
            self._inject_private_subscription_auth(env)
        else:
            # Enterprise host workers may consume only server-projected access
            # authority. Never fall back to the workstation owner's Keychain or
            # managed local login from this boundary.
            projected_access_token = _usable_claude_oauth_token(
                env.get("CLAUDE_CODE_OAUTH_TOKEN")
            )
            projected_api_key = str(env.get("ANTHROPIC_API_KEY") or "").strip()
            if projected_access_token:
                env["CLAUDE_CODE_OAUTH_TOKEN"] = projected_access_token
            elif not (use_api_key and projected_api_key):
                env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
                raise RuntimeErrorBase(
                    "Enterprise host worker server-owned Claude Code authentication is unavailable."
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
        env = self._host_env(worker)
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
