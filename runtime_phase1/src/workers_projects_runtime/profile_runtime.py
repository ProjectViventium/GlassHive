from __future__ import annotations

import json
import base64
import logging
import os
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import time
try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 compatibility
    import tomli as tomllib
from datetime import datetime
from pathlib import Path
from threading import Lock

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
from .failure_classification import classify_cli_failure
from .openclaw_runtime import (
    RuntimeErrorBase,
    RuntimeDependencyMissingError,
    RuntimeInfo,
    WorkerInterruptedError,
    WorkerPausedError,
    WorkerRuntime,
    WorkerTerminatedError,
    _PROVIDER_ENV_KEYS,
)
from .runtime_requirements import host_runtime_requirement_issue
from .terminal_takeover import TerminalTarget


logger = logging.getLogger(__name__)

_CODEX_MCP_SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")
_CODEX_TOP_LEVEL_MCP_ASSIGNMENT_RE = re.compile(r"^\s*mcp_servers(?:\.|\s*=)")
_HOST_CODEX_NATIVE_MCP_ALLOWLIST = ("computer-use", "node_repl")


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
        elif _CODEX_TOP_LEVEL_MCP_ASSIGNMENT_RE.match(line):
            keeping = False
            continue
        if keeping:
            output.append(line)
    return "\n".join(output).rstrip()


def _toml_string(value: object) -> str:
    return json.dumps(str(value), ensure_ascii=False)


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


def _instruction_with_completion_contract(instruction: str) -> str:
    body = str(instruction or "").strip()
    return f"{body}\n\n{_COMPLETION_CONTRACT}" if body else _COMPLETION_CONTRACT


class ProfiledWorkerRuntime:
    def __init__(self, base_dir: str | None = None) -> None:
        self.openclaw = OpenClawWorkstationRuntime(base_dir=base_dir)
        self.codex = CodexCliRuntime(base_dir=base_dir)
        self.claude = ClaudeCodeRuntime(base_dir=base_dir)
        self.host_openclaw = HostOpenClawRuntime(base_dir=base_dir)
        self.host_codex = HostCodexCliRuntime(base_dir=base_dir)
        self.host_claude = HostClaudeCodeRuntime(base_dir=base_dir)

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

    def pause_worker(self, worker: dict) -> RuntimeInfo:
        return self._runtime_for_worker(worker).pause_worker(worker)

    def terminate_worker(self, worker: dict) -> RuntimeInfo:
        return self._runtime_for_worker(worker).terminate_worker(worker)

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

    def worker_capacity_error(self, worker: dict) -> RuntimeErrorBase | None:
        runtime = self._runtime_for_worker(worker)
        checker = getattr(runtime, "worker_capacity_error", None)
        if callable(checker):
            return checker(worker)
        return None

    def reconcile_worker(self, worker: dict) -> RuntimeInfo:
        return self._runtime_for_worker(worker).reconcile_worker(worker)

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

    def collect_completed_run(self, worker: dict, run_id: str | None = None) -> dict[str, object] | None:
        runtime = self._runtime_for_worker(worker)
        if hasattr(runtime, "collect_completed_run"):
            try:
                return runtime.collect_completed_run(worker, run_id=run_id)
            except TypeError as exc:
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
        self.sandbox = DockerSandboxManager(base_dir=str(self.base_dir))

    def resolve_model(self, profile: str) -> str:
        raise NotImplementedError

    def preflight_worker_profile(self, profile: str, execution_mode: str = "docker") -> None:
        return None

    def _default_session_key(self, worker: dict) -> str | None:
        return worker.get("session_key") or f"worker:{worker['worker_id']}"

    def _instruction_with_completion_contract(self, instruction: str) -> str:
        return _instruction_with_completion_contract(instruction)

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

    def _read_active_session(self, worker_id: str) -> dict[str, str] | None:
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
        }

    def _write_active_session(self, worker_id: str, payload: dict[str, str]) -> None:
        path = self._active_session_meta_path(worker_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2))

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

    def pause_worker(self, worker: dict) -> RuntimeInfo:
        self.sandbox.pause(worker["worker_id"])
        return self._runtime_info(worker, pid=None)

    def interrupt_worker(self, worker: dict, run_id: str | None = None) -> RuntimeInfo:
        self._note_stop_reason(worker["worker_id"], "interrupted", run_id=run_id)
        self._stop_active_process(worker["worker_id"], worker=worker, run_id=run_id)
        sandbox = self.sandbox.inspect(worker["worker_id"])
        pid = sandbox.pid if sandbox and sandbox.state == "running" else None
        return self._runtime_info(worker, pid=pid)

    def terminate_worker(self, worker: dict) -> RuntimeInfo:
        self._note_stop_reason(worker["worker_id"], "terminated")
        self._stop_active_process(worker["worker_id"])
        self.sandbox.terminate(worker["worker_id"])
        return self._runtime_info(worker, pid=None)

    def reconcile_worker(self, worker: dict) -> RuntimeInfo:
        sandbox = self.sandbox.inspect(worker["worker_id"])
        active_pid = self._active_pid(worker["worker_id"])
        pid = active_pid or (sandbox.pid if sandbox and sandbox.state == "running" else None)
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
            "novnc_port": sandbox.get("novnc_port"),
            "selenium_port": sandbox.get("selenium_port"),
            "openclaw_port": sandbox.get("openclaw_port"),
            "pid": sandbox["pid"],
        }

    def collect_completed_run(self, worker: dict, run_id: str | None = None) -> dict[str, object] | None:
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
                "state": "failed",
                "output_text": "",
                "error_text": _redact_text(f"{self.runtime_name} exited with code {exit_code}: {detail}"),
                **classification.as_store_fields(),
            }
        try:
            session_key, output = self._parse_output(worker, stdout, stderr, self.reconcile_worker(worker))
        except RuntimeErrorBase as exc:
            return {
                "state": "failed",
                "output_text": "",
                "error_text": str(exc),
            }
        if session_key:
            self._write_session_key(worker["worker_id"], session_key)
        return {
            "state": "completed",
            "output_text": output.strip(),
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
        info = self.ensure_worker_ready(worker_for_run)
        refresh_runtime_env_for_worker(self._home_dir(worker_for_run["worker_id"]), worker_for_run)
        refresh_project_runtime_files_for_worker(
            self._home_dir(worker_for_run["worker_id"]),
            Path(str(info.workspace_dir or self._workspace_dir(worker_for_run["worker_id"]))),
            worker_for_run,
        )
        command, env = self._build_command(worker_for_run, instruction, info)
        stdout_path, stderr_path = self._log_paths(worker_for_run["worker_id"])
        with stderr_path.open("a") as handle:
            handle.write(f"$ {self.runtime_name} {shlex.join(command)}\n")

        run_root = self._run_root(worker_for_run["worker_id"], effective_run_id)
        run_root.mkdir(parents=True, exist_ok=True)

        host_stdout = run_root / "stdout.log"
        host_stderr = run_root / "stderr.log"
        host_exit = run_root / "exit_code"
        host_script = run_root / "run.sh"

        container_run_root = self._container_run_root(effective_run_id)
        container_stdout = f"{container_run_root}/stdout.log"
        container_stderr = f"{container_run_root}/stderr.log"
        container_exit = f"{container_run_root}/exit_code"
        container_script = f"{container_run_root}/run.sh"
        session_name = f"job-{effective_run_id[:12]}"

        script = "\n".join(
            [
                "#!/usr/bin/env bash",
                "set -o pipefail",
                f"mkdir -p {shlex.quote(container_run_root)}",
                (
                    "write_exit() { "
                    f"if [ ! -f {shlex.quote(container_exit)} ]; then "
                    f"printf '%s' \"$1\" > {shlex.quote(container_exit)}; "
                    "fi; "
                    "}"
                ),
                "abort_run() { write_exit \"${1:-130}\"; exit \"${1:-130}\"; }",
                "trap 'abort_run 130' HUP INT TERM",
                f"cd {shlex.quote(self.sandbox.workspace_mount)} || exit 1",
                f"export GLASSHIVE_ACTIVE_RUN_ID={shlex.quote(effective_run_id)}",
                f"export GLASSHIVE_ACTIVE_WORKER_ID={shlex.quote(worker_for_run['worker_id'])}",
                'if [ -f "$HOME/.glasshive/runtime.env" ]; then set -a; source "$HOME/.glasshive/runtime.env"; set +a; fi',
                'GLASSHIVE_SECRET_ENV_KEYS_FILE="$HOME/.glasshive/secret-runtime.keys"',
                'GLASSHIVE_SECRET_ENV_FILE="$HOME/.glasshive/secret-runtime.env"',
                'if [ -f "$GLASSHIVE_SECRET_ENV_FILE" ]; then set -a; source "$GLASSHIVE_SECRET_ENV_FILE"; set +a; rm -f "$GLASSHIVE_SECRET_ENV_FILE"; fi',
                'if [ -f "$HOME/.wpr-openclaw/openclaw.env" ]; then set -a; source "$HOME/.wpr-openclaw/openclaw.env"; set +a; fi',
                f"{shlex.join(command)} > >(tee -a {shlex.quote(container_stdout)}) 2> >(tee -a {shlex.quote(container_stderr)} >&2)",
                "status=$?",
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

        self._write_active_session(
            worker_for_run["worker_id"],
            {
                "session_name": session_name,
                "run_id": effective_run_id,
                "stdout_path": str(host_stdout),
                "stderr_path": str(host_stderr),
                "exit_path": str(host_exit),
            },
        )

        exit_code = self._wait_for_exit_code(
            worker_for_run["worker_id"],
            host_exit,
            self._run_timeout_sec(timeout_sec),
            run_id=effective_run_id,
            stdout_path=host_stdout,
        )
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

        self._finalize_stop_reason(worker_for_run["worker_id"], run_id=effective_run_id)

        if exit_code != 0:
            detail = (stderr or stdout or "").strip()[-2000:]
            raise RuntimeErrorBase(f"{self.runtime_name} exited with code {exit_code}: {detail}")

        session_key, output = self._parse_output(worker_for_run, stdout, stderr, info)
        if session_key:
            self._write_session_key(worker_for_run["worker_id"], session_key)
        return output.strip()


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
        env = self._sandbox_env()
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
        env = self._container_env(*_PROVIDER_ENV_KEYS)
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
        fast_sandbox = getattr(self.sandbox, "fast_sandbox_from_worker", lambda _worker: None)(worker)
        sandbox = fast_sandbox or self.sandbox.ensure_ready(worker, self.runtime_name)
        self._write_gateway_config(worker, self._gateway_token(worker))
        self._start_openclaw_gateway(worker, sandbox)
        return self._runtime_info(worker, pid=sandbox.pid)

    def pause_worker(self, worker: dict) -> RuntimeInfo:
        self.sandbox.pause(worker["worker_id"])
        return self._runtime_info(worker, pid=None)

    def interrupt_worker(self, worker: dict, run_id: str | None = None) -> RuntimeInfo:
        if str(worker.get("state") or "") == "running":
            self._note_stop_reason(worker["worker_id"], "interrupted", run_id=run_id)
        self._stop_active_process(worker["worker_id"], worker=worker, run_id=run_id)
        sandbox = self.sandbox.inspect(worker["worker_id"])
        pid = sandbox.pid if sandbox and sandbox.state == "running" else None
        return self._runtime_info(worker, pid=pid)

    def terminate_worker(self, worker: dict) -> RuntimeInfo:
        self._note_stop_reason(worker["worker_id"], "terminated")
        self._stop_active_process(worker["worker_id"])
        self.sandbox.terminate(worker["worker_id"])
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
        command = [
            "openclaw",
            "agent",
            "--local",
            "--session-id",
            session_id,
            "-m",
            self._instruction_with_completion_contract(instruction),
            "--json",
        ]
        return command, env

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

    def _ensure_git_workspace(self, workspace_dir: str) -> None:
        git_dir = Path(workspace_dir) / ".git"
        if git_dir.exists():
            return
        subprocess.run(["git", "init", "-q"], cwd=workspace_dir, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(
            ["git", "config", "user.email", "worker@workers-projects-runtime.local"],
            cwd=workspace_dir,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["git", "config", "user.name", "Workers Projects Runtime"],
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
        valid = {"none", "minimal", "low", "medium", "high", "xhigh"}
        if not raw:
            return set(valid)
        configured = {item.strip().lower() for item in raw.split(",") if item.strip()}
        return configured & valid or set(valid)

    def _compatible_provider_reasoning_effort_fallback(self, allowed: set[str]) -> str:
        configured = os.environ.get("WPR_CODEX_CLI_REASONING_EFFORT_FALLBACK", "medium").strip().lower()
        if configured in allowed:
            return configured
        if "medium" in allowed:
            return "medium"
        return sorted(allowed)[0] if allowed else ""

    def _append_codex_compatible_provider_config(self, command: list[str], worker: dict) -> None:
        if not self._compatible_provider_enabled():
            return
        base_url = self._compatible_provider_base_url()
        if not base_url:
            return
        provider_id = self._compatible_provider_id()
        provider_name = os.environ.get("WPR_CODEX_CLI_PROVIDER_NAME", "GlassHive OpenAI-compatible").strip()
        wire_api = os.environ.get("WPR_CODEX_CLI_WIRE_API", "responses").strip() or "responses"
        verbosity = os.environ.get("WPR_CODEX_CLI_MODEL_VERBOSITY", "medium").strip()
        reasoning_effort = (
            self._bootstrap_env_value(worker, "WPR_CODEX_CLI_REASONING_EFFORT")
            or os.environ.get("WPR_CODEX_CLI_REASONING_EFFORT", "")
        ).strip().lower()
        allowed_efforts = self._compatible_provider_allowed_reasoning_efforts()
        if reasoning_effort and reasoning_effort not in allowed_efforts:
            requested_effort = reasoning_effort
            reasoning_effort = self._compatible_provider_reasoning_effort_fallback(allowed_efforts)
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
        if self._env_flag("WPR_CODEX_CLI_IGNORE_USER_CONFIG", False):
            command.append("--ignore-user-config")
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
                f'model_providers.{provider_id}.env_key="{self._compatible_provider_env_key()}"',
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
        if reasoning_effort in {"none", "minimal", "low", "medium", "high", "xhigh"}:
            command.extend(["-c", f'model_reasoning_effort="{reasoning_effort}"'])
        if reasoning_effort == "minimal":
            command.extend(["-c", 'web_search="disabled"'])
            command.extend(["--disable", "image_generation"])

    def _build_command(self, worker: dict, instruction: str, info: RuntimeInfo) -> tuple[list[str], dict[str, str]]:
        existing_session = self._read_session_key(worker["worker_id"])
        model = worker.get("model") or self.resolve_model(worker.get("profile", "codex-cli"))
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
        self._append_codex_compatible_provider_config(command, worker)
        if dangerous_mode:
            if is_resume:
                command.append("--dangerously-bypass-approvals-and-sandbox")
            else:
                command.extend(["-s", "danger-full-access", "--dangerously-bypass-approvals-and-sandbox"])
        elif not is_resume:
            command.append("--full-auto")
        if is_resume:
            command.append(existing_session)
        command.append(_instruction_with_completion_contract(instruction))
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
        fallback = self._extract_plain_output(stdout, stderr)
        selected = _select_user_facing_agent_output([fallback])
        return session_key, (selected or fallback)[-4000:]


class ClaudeCodeRuntime(BaseCliWorkerRuntime):
    runtime_name = "claude-code"
    worker_root_name = "claude_code_runtime"
    binary_name = "claude"

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
            "json",
            "--model",
            model,
        ]
        if self._chrome_enabled():
            command.insert(2, "--chrome")
        effort = (
            self._bootstrap_env_value(worker, "WPR_CLAUDE_CODE_EFFORT")
            or os.environ.get("WPR_CLAUDE_CODE_EFFORT", "")
        ).strip().lower()
        if effort == "max":
            command.extend(["--effort", effort])
        elif effort and effort != "default":
            logger.warning(
                "Ignoring unsupported Claude Code effort",
                extra={"worker_id": str(worker.get("worker_id") or ""), "effort": effort},
            )
        if session_key and not session_key.startswith("claude-worker:"):
            command.extend(["--resume", session_key])
        command.append(_instruction_with_completion_contract(instruction))
        env = self._container_env(
            "ANTHROPIC_API_KEY",
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
            return info.session_key, (stderr.strip() or "")[-4000:]
        try:
            payload = json.loads(raw.splitlines()[-1])
        except json.JSONDecodeError:
            return info.session_key, raw[-4000:]
        session_key = str(payload.get("session_id") or info.session_key or "").strip() or None
        result = str(payload.get("result") or raw).strip()
        return session_key, _select_user_facing_agent_output([result]) or result


_SECRET_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{12,}"), r"\1[REDACTED]"),
    (re.compile(r"(?i)((?:api[_-]?key|token|secret|password|passwd|pwd)\s*[:=]\s*)[^\s\"']{6,}"), r"\1[REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"), "sk-[REDACTED]"),
    (re.compile(r"\b[A-Za-z0-9_]{8,}:[A-Za-z0-9_./+=-]{20,}\b"), "[REDACTED_CREDENTIAL]"),
    (re.compile(r"(?i)data:image/[a-z0-9.+-]+;base64,[A-Za-z0-9+/=\s]{256,}"), "[REDACTED_IMAGE_BASE64]"),
    (re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{512,}={0,2}(?![A-Za-z0-9+/=])"), "[REDACTED_LONG_BASE64]"),
)
_FINAL_REPORT_PATTERN = re.compile(r"(?m)^[ \t]*FINAL REPORT:\s*")
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


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip().lower()).strip("-._")
    return slug[:64] or "project"


class HostNativeCliMixin:
    execution_mode = "host"
    worker_root_name = "host_cli_runtime"
    _host_active_worker_id: str | None = None

    def _instruction_with_completion_contract(self, instruction: str) -> str:
        return _instruction_with_completion_contract(instruction)

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

    def _source_path_from_bootstrap_file(self, item: dict[str, object]) -> Path | None:
        for key in ("source_path", "local_path", "upload_path", "absolute_path", "filepath"):
            value = str(item.get(key) or "").strip()
            if value:
                return Path(value).expanduser()
        return None

    def _host_codex_home(self, worker: dict) -> Path:
        return self._home_dir(worker["worker_id"]) / ".codex"

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

    def _host_codex_worker_config(self, codex_config_append: str) -> str:
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
        if append_names:
            native = _strip_codex_mcp_server_blocks(native, append_names)
        return "\n\n".join(part for part in (native, append) if part.strip()).strip()

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
        if codex_config_append:
            codex_config = self._host_codex_worker_config(codex_config_append)
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

    def _materialize_workspace(self, worker: dict, workspace: Path) -> None:
        root = self._host_workspace_root(worker)
        root.mkdir(parents=True, exist_ok=True)
        if not os.access(root, os.W_OK):
            raise RuntimeErrorBase(f"Host workspace root is not writable: {root}")
        workspace.mkdir(parents=True, exist_ok=True)
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
        capture_helper = workspace / "glasshive-host-tools" / "capture-front-window.sh"
        try:
            capture_helper.chmod(0o755)
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
                self._write_workspace_bytes(workspace, path, decoded, overwrite=True)
                continue
            if "content" in item:
                self._write_workspace_file(workspace, path, str(item.get("content") or ""), overwrite=True)
                continue
            source = self._source_path_from_bootstrap_file(item)
            if source is not None:
                self._copy_workspace_source_file(workspace, path, source)
            else:
                self._write_workspace_file(workspace, path, "", overwrite=True)

    def _append_work_log(self, worker: dict, message: str) -> None:
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

    def pause_worker(self, worker: dict) -> RuntimeInfo:
        self._note_stop_reason(worker["worker_id"], "paused")
        self._stop_active_process(worker["worker_id"], worker=worker)
        self._append_work_log(worker, "Worker paused by operator.")
        return self._host_runtime_info(worker, pid=None)

    def interrupt_worker(self, worker: dict, run_id: str | None = None) -> RuntimeInfo:
        self._note_stop_reason(worker["worker_id"], "interrupted", run_id=run_id)
        self._stop_active_process(worker["worker_id"], worker=worker, run_id=run_id)
        self._append_work_log(worker, "Active run interrupted by operator.")
        return self._host_runtime_info(worker, pid=None)

    def terminate_worker(self, worker: dict) -> RuntimeInfo:
        self._note_stop_reason(worker["worker_id"], "terminated")
        self._stop_active_process(worker["worker_id"], worker=worker)
        self._append_work_log(worker, "Worker terminated by operator.")
        return self._host_runtime_info(worker, pid=None)

    def reconcile_worker(self, worker: dict) -> RuntimeInfo:
        return self._host_runtime_info(worker, pid=self._active_pid(worker["worker_id"]))

    def _stop_active_process(self, worker_id: str, *, worker: dict | None = None, run_id: str | None = None) -> None:
        with self._process_lock:
            process = self._active_processes.get(worker_id)
        if not process or process.poll() is not None:
            return
        try:
            pgid = os.getpgid(process.pid)
            os.killpg(pgid, signal.SIGTERM)
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except OSError:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        except OSError:
            try:
                process.terminate()
            except OSError:
                pass
        finally:
            self._clear_process(worker_id)
            if self._host_active_worker_id == worker_id:
                self._host_active_worker_id = None

    def _acquire_host_slot(self, worker: dict) -> None:
        if os.environ.get("WPR_HOST_ALLOW_CONCURRENT_SAME_CLI", "").strip().lower() in {"1", "true", "yes", "on"}:
            return
        worker_id = worker["worker_id"]
        with self._process_lock:
            error = self._host_capacity_error_locked(worker_id)
            if error is not None:
                raise error
            self._host_active_worker_id = worker_id

    def _host_capacity_error_locked(self, worker_id: str) -> RuntimeErrorBase | None:
        active = self._host_active_worker_id
        active_process = self._active_processes.get(active or "")
        if active and active != worker_id and (active_process is None or active_process.poll() is None):
            return RuntimeErrorBase(
                f"Host-native {self.runtime_name} already has an active worker ({active}); "
                "v1 allows one active host worker per CLI family."
            )
        return None

    def worker_capacity_error(self, worker: dict) -> RuntimeErrorBase | None:
        if os.environ.get("WPR_HOST_ALLOW_CONCURRENT_SAME_CLI", "").strip().lower() in {"1", "true", "yes", "on"}:
            return None
        with self._process_lock:
            return self._host_capacity_error_locked(str(worker["worker_id"]))

    def _release_host_slot(self, worker_id: str) -> None:
        with self._process_lock:
            if self._host_active_worker_id == worker_id:
                self._host_active_worker_id = None

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

    def run_task(self, worker: dict, instruction: str, timeout_sec: float | None = None, run_id: str | None = None) -> str:
        info = self.ensure_worker_ready(worker)
        effective_run_id = (run_id or secrets.token_hex(8)).strip()
        command, env = self._build_command(worker, instruction, info)
        env["GLASSHIVE_RUN_ID"] = effective_run_id
        workspace = Path(str(info.workspace_dir or self._host_workspace_dir(worker)))
        self._acquire_host_slot(worker)

        run_root = self._run_root(worker["worker_id"], effective_run_id)
        run_root.mkdir(parents=True, exist_ok=True)
        run_root.chmod(0o700)
        raw_stdout = run_root / "stdout.log"
        raw_stderr = run_root / "stderr.log"
        exit_path = run_root / "exit_code"
        stdout_path, stderr_path = self._log_paths(worker["worker_id"])
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_path.parent.mkdir(parents=True, exist_ok=True)

        command_display = shlex.join(command)
        self._append_work_log(worker, f"Run {effective_run_id} started with host-native {self.runtime_name}.")
        self._write_action_audit(
            worker,
            {
                "kind": "run.started",
                "run_id": effective_run_id,
                "cwd": str(workspace),
                "argv_redacted": [_redact_text(part) for part in command],
                "env_keys": sorted(env.keys()),
            },
        )

        with stderr_path.open("a") as aggregate:
            aggregate.write(f"$ host {self.runtime_name} {_redact_text(command_display)}\n")
            stderr_path.chmod(0o600)

        with raw_stdout.open("w") as stdout_handle, raw_stderr.open("w") as stderr_handle:
            raw_stdout.chmod(0o600)
            raw_stderr.chmod(0o600)
            process = subprocess.Popen(
                command,
                cwd=str(workspace),
                env=env,
                text=True,
                stdin=subprocess.DEVNULL,
                stdout=stdout_handle,
                stderr=stderr_handle,
                start_new_session=True,
            )
            self._register_process(worker["worker_id"], process)
            self._write_active_session(
                worker["worker_id"],
                {
                    "session_name": f"host-{effective_run_id[:12]}",
                    "run_id": effective_run_id,
                    "stdout_path": str(raw_stdout),
                    "stderr_path": str(raw_stderr),
                    "exit_path": str(exit_path),
                },
            )
            run_timeout_sec = self._host_run_timeout_sec(timeout_sec)
            try:
                exit_code = process.wait(timeout=run_timeout_sec)
            except subprocess.TimeoutExpired:
                self._note_stop_reason(worker["worker_id"], "terminated", run_id=effective_run_id)
                self._stop_active_process(worker["worker_id"], worker=worker, run_id=effective_run_id)
                self._append_work_log(
                    worker,
                    f"Run {effective_run_id} exceeded configured host timeout after {run_timeout_sec:g}s.",
                )
                raise RuntimeErrorBase(f"{self.runtime_name} timed out after {run_timeout_sec:g}s")
            finally:
                self._clear_process(worker["worker_id"])
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

        self._finalize_stop_reason(worker["worker_id"], run_id=effective_run_id)
        if exit_code != 0:
            detail = (redacted_stderr or redacted_stdout or "").strip()[-2000:]
            self._append_work_log(worker, f"Run {effective_run_id} failed with exit code {exit_code}.")
            raise RuntimeErrorBase(f"{self.runtime_name} exited with code {exit_code}: {detail}")

        session_key, output = self._parse_output(worker, stdout, stderr, info)
        if session_key:
            self._write_session_key(worker["worker_id"], session_key)
        if _FINAL_REPORT_PATTERN.search(stdout) and not _FINAL_REPORT_PATTERN.search(output):
            output = f"FINAL REPORT:\n{output.strip()}"
        redacted_output = _redact_text(output.strip())
        if len(redacted_output) > _HOST_RUN_OUTPUT_MAX_CHARS:
            redacted_output = f"{redacted_output[: _HOST_RUN_OUTPUT_MAX_CHARS - 3].rstrip()}..."
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
        if workspace.exists() and not (workspace / ".git").exists():
            self._ensure_git_workspace(str(workspace))
        return info

    def _build_command(self, worker: dict, instruction: str, info: RuntimeInfo) -> tuple[list[str], dict[str, str]]:
        existing_session = self._read_session_key(worker["worker_id"])
        model = worker.get("model") or self.resolve_model(worker.get("profile", "codex-cli"))
        is_resume = bool(existing_session and not existing_session.startswith("codex-worker:"))
        dangerous_mode = os.environ.get("WPR_CODEX_DANGEROUS", "1").strip().lower() in {"1", "true", "yes", "on"}
        if is_resume:
            command = [self.binary, "exec", "resume"]
        else:
            command = [self.binary, "exec", "--json", "--skip-git-repo-check", "-C", str(info.workspace_dir or ".")]
        if model:
            if is_resume:
                command.extend(["-c", f'model="{model}"'])
            else:
                command.extend(["-m", model])
        if dangerous_mode:
            if is_resume:
                command.append("--dangerously-bypass-approvals-and-sandbox")
            else:
                command.extend(["-s", "danger-full-access", "--dangerously-bypass-approvals-and-sandbox"])
        elif not is_resume:
            command.append("--full-auto")
        if is_resume:
            command.append(existing_session)
        command.append(self._instruction_with_completion_contract(instruction))
        env = self._host_env(worker)
        codex_home = self._host_codex_home(worker)
        if (codex_home / "config.toml").exists():
            env["CODEX_HOME"] = str(codex_home)
        return command, env


class HostClaudeCodeRuntime(HostNativeCliMixin, ClaudeCodeRuntime):
    worker_root_name = "host_claude_code_runtime"
    binary_env_var = "WPR_CLAUDE_CODE_BIN"

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
            "Claude Code workers requested `max` effort, but the configured Claude Code CLI "
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
            "json",
            "--model",
            model,
        ]
        if self._chrome_enabled():
            command.insert(2, "--chrome")
        effort = (
            self._bootstrap_env_value(worker, "WPR_CLAUDE_CODE_EFFORT")
            or os.environ.get("WPR_CLAUDE_CODE_EFFORT", "")
        ).strip().lower()
        if effort == "max":
            if not self._effort_supported():
                self._raise_missing_effort_support(str(worker.get("profile") or "claude-code"), "host")
            command.extend(["--effort", effort])
        elif effort and effort != "default":
            logger.warning(
                "Ignoring unsupported Claude Code effort",
                extra={"worker_id": str(worker.get("worker_id") or ""), "effort": effort},
            )
        if session_key and not session_key.startswith("claude-worker:"):
            command.extend(["--resume", session_key])
        command.append(self._instruction_with_completion_contract(instruction))
        env = self._host_env(worker)
        use_api_key = os.environ.get("WPR_CLAUDE_CODE_USE_API_KEY", "0").strip().lower() in {"1", "true", "yes", "on"}
        if not use_api_key:
            env.pop("ANTHROPIC_API_KEY", None)
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
        return [
            self.binary,
            "agent",
            "--local",
            "--session-id",
            session_id,
            "-m",
            self._instruction_with_completion_contract(instruction),
            "--json",
        ], env
