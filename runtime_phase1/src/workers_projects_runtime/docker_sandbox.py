from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from urllib.parse import quote, urlencode

from .bootstrap import apply_bootstrap


SAFE_DOCKER_EXEC_ENV_KEYS = {
    "PATH",
    "SHELL",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LC_MESSAGES",
    "PYTHONIOENCODING",
    # Provider keys are run-scoped: the worker launch script unsets them before
    # handing control to the post-run interactive shell.
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_API_BASE",
    "OPENAI_REVERSE_PROXY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "PORTKEY_API_KEY",
    "PORTKEY_BASE_URL",
    "PORTKEY_VIRTUAL_KEY",
    "PORTKEY_CONFIG",
    "WPR_CODEX_CLI_BASE_URL",
    "WPR_CODEX_CLI_ENV_KEY",
    "WPR_CODEX_CLI_MODEL_PROVIDER",
    "WPR_CODEX_CLI_USE_CUSTOM_PROVIDER",
    "WPR_CODEX_CLI_WIRE_API",
    "WPR_OPENCLAW_BASE_URL",
    "WPR_OPENCLAW_ENV_KEY",
    "WPR_OPENCLAW_MODEL_PROVIDER",
    "WPR_OPENCLAW_USE_CUSTOM_PROVIDER",
    "WPR_OPENCLAW_WIRE_API",
    "TMPDIR",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
}

AI_WORKER_BROWSER_EXTENSION_UPDATE_URL = "https://clients2.google.com/service/update2/crx"
AI_WORKER_BROWSER_EXTENSIONS = {
    "claude": "fcoeoabgfenejglbffodgkkbkcdhcgfn",
    "codex": "hehggadaopoacecdllhhajmbjkdcmajg",
}
AI_WORKER_BROWSER_EXTENSION_POLICY_PATHS = (
    "/etc/chromium/policies/managed/glasshive-ai-worker-extensions.json",
    "/etc/opt/chrome/policies/managed/glasshive-ai-worker-extensions.json",
)
AI_WORKER_CODEX_NPM_SPEC = os.environ.get("WPR_SANDBOX_CODEX_NPM_SPEC", "@openai/codex@0.140.0").strip() or "@openai/codex@0.140.0"
AI_WORKER_CLAUDE_CODE_NPM_SPEC = (
    os.environ.get("WPR_SANDBOX_CLAUDE_CODE_NPM_SPEC", "@anthropic-ai/claude-code@2.1.178").strip()
    or "@anthropic-ai/claude-code@2.1.178"
)
AI_WORKER_OPENCLAW_NPM_SPEC = os.environ.get("WPR_SANDBOX_OPENCLAW_NPM_SPEC", "openclaw@latest").strip() or "openclaw@latest"


def _ai_worker_browser_extension_policy_json() -> str:
    return json.dumps(
        {
            "ExtensionInstallForcelist": [
                f"{extension_id};{AI_WORKER_BROWSER_EXTENSION_UPDATE_URL}"
                for extension_id in AI_WORKER_BROWSER_EXTENSIONS.values()
            ]
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _ai_worker_browser_extension_check_script() -> str:
    extension_ids = " ".join(shlex.quote(extension_id) for extension_id in AI_WORKER_BROWSER_EXTENSIONS.values())
    policy_paths = " ".join(shlex.quote(path) for path in AI_WORKER_BROWSER_EXTENSION_POLICY_PATHS)
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            f"extension_ids=({extension_ids})",
            f"policy_paths=({policy_paths})",
            "require_profile=0",
            'if [ "${1:-}" = "--require-profile" ]; then require_profile=1; fi',
            'for policy in "${policy_paths[@]}"; do',
            '  test -f "$policy"',
            '  grep -Fq "ExtensionInstallForcelist" "$policy"',
            '  for extension_id in "${extension_ids[@]}"; do',
            f'    grep -Fq "${{extension_id}};{AI_WORKER_BROWSER_EXTENSION_UPDATE_URL}" "$policy"',
            "  done",
            "done",
            'profile_root="${CHROME_USER_DATA_DIR:-${HOME:-/workspace/.wpr-home}/.config/chromium}"',
            "missing=0",
            'for extension_id in "${extension_ids[@]}"; do',
            '  if [ -d "$profile_root/Default/Extensions/$extension_id" ] || [ -d "$profile_root/Extensions/$extension_id" ]; then',
            '    printf "%s profile-installed\\n" "$extension_id"',
            "  else",
            '    printf "%s policy-present profile-pending\\n" "$extension_id"',
            "    missing=1",
            "  fi",
            "done",
            'if [ "$require_profile" = "1" ] && [ "$missing" = "1" ]; then exit 2; fi',
            'printf "glasshive browser extension policy ok\\n"',
        ]
    )


@dataclass
class SandboxInfo:
    container_name: str
    container_id: str | None
    state: str
    workspace_dir: str
    home_dir: str
    pid: int | None
    image: str
    novnc_port: int | None = None
    selenium_port: int | None = None
    openclaw_port: int | None = None


def _safe_docker_exec_env(env: dict[str, str] | None) -> dict[str, str]:
    return {
        key: str(value)
        for key, value in (env or {}).items()
        if value is not None and (key in SAFE_DOCKER_EXEC_ENV_KEYS or key.startswith("LC_"))
    }


class DockerSandboxManager:
    _build_lock = Lock()
    _default_image = "workers-projects-runtime-workstation:phase1-node22-docs4"

    def __init__(self, base_dir: str | None = None) -> None:
        self.base_dir = Path(base_dir) if base_dir else Path(__file__).resolve().parents[2] / "data"
        self.runtime_root = self.base_dir / "docker_sandboxes"
        self.build_root = self.runtime_root / "build"
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.build_root.mkdir(parents=True, exist_ok=True)
        self.image = os.environ.get("WPR_SANDBOX_IMAGE", self._default_image)
        self.user = os.environ.get("WPR_SANDBOX_USER", "seluser")
        self.home_mount = os.environ.get("WPR_SANDBOX_HOME", "/workspace/.wpr-home")
        self.workspace_mount = os.environ.get("WPR_SANDBOX_WORKSPACE", "/workspace/project")
        self.term_value = os.environ.get("WPR_SANDBOX_TERM", "xterm-256color")
        self.display_value = os.environ.get("WPR_SANDBOX_DISPLAY", ":99.0")
        self.novnc_container_port = int(os.environ.get("WPR_SANDBOX_NOVNC_PORT", "7900"))
        self.selenium_container_port = int(os.environ.get("WPR_SANDBOX_SELENIUM_PORT", "4444"))
        self.openclaw_container_port = int(os.environ.get("WPR_SANDBOX_OPENCLAW_PORT", "18789"))
        self.vnc_password = os.environ.get("WPR_SANDBOX_VNC_PASSWORD", "secret")
        self.vnc_no_password = os.environ.get("WPR_SANDBOX_VNC_NO_PASSWORD", "1").strip().lower() in {"1", "true", "yes", "on"}
        self.memory_limit = os.environ.get("WPR_SANDBOX_MEMORY", "3g").strip()
        self.memory_swap_limit = os.environ.get("WPR_SANDBOX_MEMORY_SWAP", self.memory_limit).strip()
        self.cpu_limit = os.environ.get("WPR_SANDBOX_CPUS", "2").strip()
        self.pids_limit = os.environ.get("WPR_SANDBOX_PIDS_LIMIT", "4096").strip()
        self.inspect_timeout_sec = float(os.environ.get("WPR_DOCKER_INSPECT_TIMEOUT_SEC", "5") or "5")
        self.inspect_cache_ttl_sec = float(os.environ.get("WPR_DOCKER_INSPECT_CACHE_TTL_SEC", "5") or "5")
        self.inspect_stale_ttl_sec = float(os.environ.get("WPR_DOCKER_INSPECT_STALE_TTL_SEC", "60") or "60")
        self._inspect_cache: dict[str, tuple[float, SandboxInfo]] = {}
        self.image_inspect_timeout_sec = float(os.environ.get("WPR_DOCKER_IMAGE_INSPECT_TIMEOUT_SEC", "15") or "15")
        self.image_build_timeout_sec = float(os.environ.get("WPR_DOCKER_IMAGE_BUILD_TIMEOUT_SEC", "900") or "900")
        self.image_check_ttl_sec = float(os.environ.get("WPR_DOCKER_IMAGE_CHECK_TTL_SEC", "300") or "300")
        self._image_checked_at: float = 0.0

    def _invalidate_inspect_cache(self, worker_id: str) -> None:
        self._inspect_cache.pop(worker_id, None)

    def _env_flag(self, name: str, default: bool) -> bool:
        raw = str(os.environ.get(name, "")).strip().lower()
        if not raw:
            return default
        return raw in {"1", "true", "yes", "on"}

    def ensure_ready(
        self,
        worker: dict,
        runtime_name: str,
        *,
        start_if_paused: bool = True,
        repair_paths: bool = True,
    ) -> SandboxInfo:
        self._require_docker()
        paths = self._paths(worker["worker_id"])
        self._ensure_host_dirs(paths)
        self._seed_bootstrap(paths["home_dir"], paths["workspace_dir"], runtime_name, worker)
        container_name = self._container_name(worker["worker_id"])
        sandbox = self.inspect(worker["worker_id"])
        needs_idle_prime = False
        needs_path_repair = False
        if sandbox is None:
            fast_sandbox = self.fast_sandbox_from_worker(worker)
            if fast_sandbox is not None:
                return fast_sandbox
            self._ensure_image()
            self._invalidate_inspect_cache(worker["worker_id"])
            self._create_container(container_name, paths)
            self._invalidate_inspect_cache(worker["worker_id"])
            sandbox = self.inspect(worker["worker_id"])
            needs_idle_prime = True
            needs_path_repair = True
        if sandbox is None:
            raise RuntimeError("Failed to create worker sandbox")
        if sandbox.state == "paused" and start_if_paused:
            self._invalidate_inspect_cache(worker["worker_id"])
            self._docker(["unpause", container_name])
            self._invalidate_inspect_cache(worker["worker_id"])
            sandbox = self.inspect(worker["worker_id"])
        elif sandbox.state in {"created", "exited", "dead"}:
            self._invalidate_inspect_cache(worker["worker_id"])
            self._docker(["start", container_name])
            self._invalidate_inspect_cache(worker["worker_id"])
            sandbox = self.inspect(worker["worker_id"])
            needs_idle_prime = True
            needs_path_repair = True
        if sandbox is None:
            raise RuntimeError("Failed to start worker sandbox")
        if needs_path_repair or (repair_paths and self._env_flag("WPR_REPAIR_RUNNING_CONTAINER_ROOTS", False)):
            self._ensure_container_writable_paths(sandbox.container_name, self._default_writable_container_paths())
        self._harden_secret_runtime_files(sandbox.container_name)
        if needs_idle_prime:
            self._set_plain_background(sandbox.container_name)
        if needs_idle_prime and self._env_flag("WPR_IDLE_DESKTOP_PRIME_BROWSER", True):
            self._prime_idle_desktop(sandbox.container_name)
        return sandbox

    def inspect(self, worker_id: str) -> SandboxInfo | None:
        now = time.monotonic()
        cached = self._inspect_cache.get(worker_id)
        if cached and cached[0] + self.inspect_cache_ttl_sec > now:
            return cached[1]
        container_name = self._container_name(worker_id)
        result = self._docker(
            ["inspect", container_name],
            check=False,
            capture_output=True,
            timeout_sec=self.inspect_timeout_sec,
        )
        if result.returncode != 0:
            if cached and cached[0] + self.inspect_stale_ttl_sec > now:
                return cached[1]
            return None
        try:
            payload = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            if cached and cached[0] + self.inspect_stale_ttl_sec > now:
                return cached[1]
            return None
        if not payload:
            if cached and cached[0] + self.inspect_stale_ttl_sec > now:
                return cached[1]
            return None
        entry = payload[0]
        state = entry.get("State") or {}
        status = str(state.get("Status") or "unknown")
        if bool(state.get("Paused")):
            status = "paused"
        pid = state.get("Pid")
        ports = entry.get("NetworkSettings", {}).get("Ports") or {}
        sandbox = SandboxInfo(
            container_name=container_name,
            container_id=str(entry.get("Id") or "").strip() or None,
            state=status,
            workspace_dir=str(self._paths(worker_id)["workspace_dir"]),
            home_dir=str(self._paths(worker_id)["home_dir"]),
            pid=int(pid) if isinstance(pid, int) and pid > 0 and status == "running" else None,
            image=self.image,
            novnc_port=self._host_port_for(ports, self.novnc_container_port),
            selenium_port=self._host_port_for(ports, self.selenium_container_port),
            openclaw_port=self._host_port_for(ports, self.openclaw_container_port),
        )
        self._inspect_cache[worker_id] = (now, sandbox)
        return sandbox

    def pause(self, worker_id: str) -> SandboxInfo:
        sandbox = self.inspect(worker_id)
        if sandbox is None:
            return SandboxInfo(
                container_name=self._container_name(worker_id),
                container_id=None,
                state="missing",
                workspace_dir=str(self._paths(worker_id)["workspace_dir"]),
                home_dir=str(self._paths(worker_id)["home_dir"]),
                pid=None,
                image=self.image,
                openclaw_port=None,
            )
        if sandbox.state == "running":
            self._docker(["pause", sandbox.container_name], check=False)
            self._invalidate_inspect_cache(worker_id)
        return self.inspect(worker_id) or sandbox

    def terminate(self, worker_id: str) -> SandboxInfo:
        sandbox = self.inspect(worker_id)
        if sandbox is not None:
            self._docker(["rm", "-f", sandbox.container_name], check=False)
            self._invalidate_inspect_cache(worker_id)
        return SandboxInfo(
            container_name=self._container_name(worker_id),
            container_id=None,
            state="terminated",
            workspace_dir=str(self._paths(worker_id)["workspace_dir"]),
            home_dir=str(self._paths(worker_id)["home_dir"]),
            pid=None,
            image=self.image,
            openclaw_port=None,
        )

    def exec_command(
        self,
        worker_id: str,
        runtime_name: str,
        command: list[str],
        env: dict[str, str] | None = None,
        worker: dict | None = None,
    ) -> list[str]:
        resolved_worker = worker or {"worker_id": worker_id}
        sandbox = self.ensure_ready(resolved_worker, runtime_name=runtime_name, repair_paths=False)
        docker_command = [
            "docker",
            "exec",
            "-i",
            "-u",
            self.user,
            "-w",
            self.workspace_mount,
            "-e",
            f"HOME={self.home_mount}",
            "-e",
            f"TERM={self.term_value}",
        ]
        merged_env = _safe_docker_exec_env(env)
        for key, value in sorted(merged_env.items()):
            if value is None:
                continue
            docker_command.extend(["-e", f"{key}={value}"])
        docker_command.append(sandbox.container_name)
        docker_command.extend(command)
        return docker_command

    def terminal_attach_command(self, worker_id: str, runtime_name: str, session_name: str = "operator") -> list[str]:
        sandbox = self.ensure_ready({"worker_id": worker_id}, runtime_name=runtime_name)
        self._ensure_screen_runtime_dir(sandbox.container_name)
        return [
            "docker",
            "exec",
            "-it",
            "-u",
            self.user,
            "-w",
            self.workspace_mount,
            "-e",
            f"HOME={self.home_mount}",
            "-e",
            f"TERM={self.term_value}",
            "-e",
            f"TMPDIR={self._browser_tmp_dir()}",
            "-e",
            f"XDG_CACHE_HOME={self._browser_cache_dir()}",
            "-e",
            f"XDG_CONFIG_HOME={self._browser_config_dir()}",
            sandbox.container_name,
            "screen",
            "-xRR",
            session_name,
        ]

    def list_screen_sessions(self, worker_id: str, runtime_name: str, *, worker: dict | None = None) -> list[str]:
        resolved_worker = worker or {"worker_id": worker_id}
        sandbox = self.ensure_ready(resolved_worker, runtime_name=runtime_name, repair_paths=False)
        self._ensure_screen_runtime_dir(sandbox.container_name)
        result = self._docker_exec(
            sandbox.container_name,
            ["bash", "-c", "screen -ls || true"],
            env=self._desktop_env(),
            cwd=self.workspace_mount,
        )
        output = "\n".join(filter(None, [(result.stdout or "").strip(), (result.stderr or "").strip()]))
        sessions: list[str] = []
        for raw_line in output.splitlines():
            line = raw_line.strip()
            if "\t(" not in line or "." not in line:
                continue
            head = line.split("\t", 1)[0].strip()
            if "." not in head:
                continue
            sessions.append(head.split(".", 1)[1].strip())
        return sessions

    def start_screen_session(
        self,
        worker_id: str,
        runtime_name: str,
        session_name: str,
        command: list[str],
        *,
        env: dict[str, str] | None = None,
        worker: dict | None = None,
    ) -> subprocess.CompletedProcess[str]:
        resolved_worker = worker or {"worker_id": worker_id}
        sandbox = self.fast_sandbox_from_worker(resolved_worker) or self.ensure_ready(resolved_worker, runtime_name=runtime_name)
        self._ensure_screen_runtime_dir(sandbox.container_name)
        merged_env = {
            **self._desktop_env(),
            **_safe_docker_exec_env(env),
        }
        self.stop_screen_session(worker_id, runtime_name, session_name, worker=resolved_worker, missing_ok=True)
        return self._docker_exec(
            sandbox.container_name,
            ["screen", "-DmS", session_name, *command],
            env=merged_env,
            cwd=self.workspace_mount,
            detach=True,
        )

    def ensure_container_writable_paths(
        self,
        worker_id: str,
        runtime_name: str,
        container_paths: list[str],
        *,
        worker: dict | None = None,
    ) -> None:
        if not container_paths:
            return
        resolved_worker = worker or {"worker_id": worker_id}
        sandbox = (
            self.fast_sandbox_from_worker(resolved_worker)
            or self.inspect(worker_id)
            or self.ensure_ready(resolved_worker, runtime_name=runtime_name)
        )
        self._ensure_container_writable_paths(sandbox.container_name, container_paths)

    def stop_screen_session(
        self,
        worker_id: str,
        runtime_name: str,
        session_name: str,
        *,
        worker: dict | None = None,
        missing_ok: bool = False,
    ) -> None:
        resolved_worker = worker or {"worker_id": worker_id}
        container_name = self._container_name(worker_id)
        if not self._worker_state_allows_fast_exec(resolved_worker):
            sandbox = self.inspect(worker_id)
            if sandbox is None:
                if missing_ok:
                    return
                raise RuntimeError(f"Worker sandbox {container_name} is not running")
            container_name = sandbox.container_name
        script = "\n".join(
            [
                "target=$1",
                "sockets=$(screen -ls | awk -v target=\"$target\" '",
                "  /^[[:space:]]*[0-9]+[.]/ {",
                "    socket=$1;",
                "    name=socket;",
                "    sub(/^[0-9]+[.]/, \"\", name);",
                "    if (name == target) print socket;",
                "  }",
                "')",
                "if [ -z \"$sockets\" ]; then exit 42; fi",
                "status=0",
                "for socket in $sockets; do",
                "  screen -S \"$socket\" -X quit >/dev/null 2>&1 || status=$?",
                "done",
                "exit \"$status\"",
            ]
        )
        result = self._docker_exec(
            container_name,
            ["bash", "-c", script, "glasshive-stop-screen", session_name],
            env=self._desktop_env(),
            cwd=self.workspace_mount,
        )
        if result.returncode != 0 and not (missing_ok and result.returncode == 42):
            detail = (result.stderr or result.stdout or "").strip()[-1200:]
            raise RuntimeError(f"Failed to stop screen session {session_name}: {detail}")

    def terminate_run_processes(
        self,
        worker_id: str,
        runtime_name: str,
        run_id: str,
        *,
        worker: dict | None = None,
    ) -> None:
        resolved_worker = worker or {"worker_id": worker_id}
        container_name = self._container_name(worker_id)
        if not self._worker_state_allows_fast_exec(resolved_worker):
            sandbox = self.inspect(worker_id)
            if sandbox is None:
                raise RuntimeError(f"Worker sandbox {container_name} is not running")
            container_name = sandbox.container_name
        run_root = f"{self.home_mount}/.glasshive-runs/{run_id}"
        script = "\n".join(
            [
                f"needle={shlex.quote(run_root)}",
                f"run_id={shlex.quote(run_id)}",
                "arg_pids=$(ps -eo pid=,ppid=,args= | awk -v needle=\"$needle\" 'index($0, needle) > 0 { print $1 }')",
                "env_pids=$(for env in /proc/[0-9]*/environ; do "
                "pid=${env#/proc/}; pid=${pid%%/*}; "
                "tr '\\0' '\\n' < \"$env\" 2>/dev/null | grep -Fxq \"GLASSHIVE_ACTIVE_RUN_ID=$run_id\" && printf '%s\\n' \"$pid\"; "
                "done)",
                "pids=$(printf '%s\\n%s\\n' \"$arg_pids\" \"$env_pids\" | awk 'NF' | sort -u)",
                "if [ -z \"$pids\" ]; then exit 0; fi",
                "descendants() { "
                "for parent in \"$@\"; do "
                "children=$(ps -eo pid=,ppid= | awk -v p=\"$parent\" '$2 == p { print $1 }'); "
                "if [ -n \"$children\" ]; then descendants $children; fi; "
                "printf '%s\\n' \"$parent\"; "
                "done; "
                "}",
                "targets=$(descendants $pids | awk 'NF' | sort -u)",
                "for pid in $targets; do kill -TERM \"$pid\" >/dev/null 2>&1 || true; done",
                "sleep 1",
                "for pid in $targets; do kill -KILL \"$pid\" >/dev/null 2>&1 || true; done",
            ]
        )
        self._docker_exec(
            container_name,
            ["bash", "-c", script],
            env=self._desktop_env(),
            cwd=self.workspace_mount,
        )

    def desktop_action(
        self,
        worker_id: str,
        runtime_name: str,
        action: str,
        *,
        url: str | None = None,
        session_name: str | None = None,
        worker: dict | None = None,
    ) -> dict[str, object]:
        resolved_worker = worker or {"worker_id": worker_id}
        sandbox = self.fast_sandbox_from_worker(resolved_worker) or self.ensure_ready(
            resolved_worker,
            runtime_name=runtime_name,
            repair_paths=False,
        )
        normalized = action.strip().lower().replace("-", "_")
        command = self._desktop_action_command(normalized, url=url, session_name=session_name)
        if not command:
            raise ValueError(f"Unsupported desktop action: {action}")
        merged_env = {
            **self._desktop_env(),
        }
        result = self._docker_exec(
            sandbox.container_name,
            command,
            env=merged_env,
            cwd=self.workspace_mount,
            detach=True,
            fire_and_forget=True,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[-1200:]
            raise RuntimeError(f"Desktop action {action} failed: {detail}")
        return {
            "action": normalized,
            "container_name": sandbox.container_name,
            "view_url": self._view_url_from_sandbox(sandbox),
            "status": "launched",
        }

    def describe(self, worker_id: str) -> dict[str, object]:
        sandbox = self.inspect(worker_id)
        paths = self._paths(worker_id)
        return {
            "driver": "docker",
            "image": sandbox.image if sandbox else self.image,
            "container_name": self._container_name(worker_id),
            "container_id": sandbox.container_id if sandbox else None,
            "state": sandbox.state if sandbox else "missing",
            "workspace_dir": str(paths["workspace_dir"]),
            "home_dir": str(paths["home_dir"]),
            "pid": sandbox.pid if sandbox else None,
            "novnc_port": sandbox.novnc_port if sandbox else None,
            "selenium_port": sandbox.selenium_port if sandbox else None,
            "openclaw_port": sandbox.openclaw_port if sandbox else None,
            "view_url": self.view_url(worker_id),
        }

    def view_url(self, worker_id: str) -> str | None:
        sandbox = self.inspect(worker_id)
        return self._view_url_from_sandbox(sandbox)

    def _view_url_from_sandbox(self, sandbox: SandboxInfo | None) -> str | None:
        if sandbox is None or sandbox.novnc_port is None:
            return None
        query = urlencode(
            {
                **({"password": self.vnc_password} if not self.vnc_no_password else {}),
                "autoconnect": "1",
                "resize": "scale",
                "reconnect": "1",
                "show_dot": "1",
            }
        )
        return f"http://127.0.0.1:{sandbox.novnc_port}/?{query}"

    def _default_browser_url(self) -> str:
        html = (
            "<!doctype html><html><head><meta charset='utf-8' />"
            "<style>"
            "html,body{height:100%;margin:0;background:#000;color:#e8ebef;"
            "font-family:system-ui,-apple-system,sans-serif}"
            "body{display:grid;place-items:center}"
            ".wrap{max-width:540px;padding:24px;text-align:center}"
            "h1{font-size:clamp(28px,4vw,48px);margin:0 0 10px;letter-spacing:-.04em}"
            "p{margin:0;color:rgba(232,235,239,.74);font-size:16px;line-height:1.5}"
            "</style></head><body><div class='wrap'>"
            "<h1>GlassHive</h1>"
            "<p>Your worker is preparing the result. This view will become the delivered page when it is ready.</p>"
            "</div></body></html>"
        )
        return f"data:text/html,{quote(html)}"

    def _browser_tmp_dir(self) -> str:
        return f"{self.home_mount}/tmp"

    def _browser_cache_dir(self) -> str:
        return f"{self.home_mount}/.cache"

    def _browser_config_dir(self) -> str:
        return f"{self.home_mount}/.config"

    def _default_writable_container_paths(self) -> list[str]:
        return [
            self.workspace_mount,
            self.home_mount,
            self._browser_tmp_dir(),
            self._browser_cache_dir(),
            self._browser_config_dir(),
        ]

    def _desktop_env(self) -> dict[str, str]:
        return {
            "HOME": self.home_mount,
            "TERM": self.term_value,
            "DISPLAY": self.display_value,
            "TMPDIR": self._browser_tmp_dir(),
            "XDG_CACHE_HOME": self._browser_cache_dir(),
            "XDG_CONFIG_HOME": self._browser_config_dir(),
        }

    def _container_name(self, worker_id: str) -> str:
        token = worker_id.replace("_", "-").lower()
        return f"wpr-{token}"

    @staticmethod
    def _worker_state_allows_fast_exec(worker: dict | None) -> bool:
        state = str((worker or {}).get("state") or "").strip().lower()
        return state in {"ready", "running", "failed", "cancelled", "interrupted"}

    def fast_sandbox_from_worker(self, worker: dict | None) -> SandboxInfo | None:
        if not worker or not self._worker_state_allows_fast_exec(worker):
            return None
        worker_id = str(worker.get("worker_id") or "").strip()
        if not worker_id:
            return None
        # State/workspace directories are projected before container startup so
        # operators can inspect paths early. They are not evidence that Docker
        # has created the workstation. Only use the shortcut when the caller has
        # real container evidence, then validate it through inspect/cache.
        if not str(worker.get("container_id") or "").strip():
            return None
        return self.inspect(worker_id)

    def paths(self, worker_id: str) -> dict[str, Path]:
        worker_root = self.runtime_root / "workers" / worker_id
        state_dir = worker_root / "state"
        workspace_dir = state_dir / "workspace"
        home_dir = state_dir / "home"
        return {
            "worker_root": worker_root,
            "state_dir": state_dir,
            "workspace_dir": workspace_dir,
            "home_dir": home_dir,
        }

    def _paths(self, worker_id: str) -> dict[str, Path]:
        return self.paths(worker_id)

    def _ensure_host_dirs(self, paths: dict[str, Path]) -> None:
        paths["workspace_dir"].mkdir(parents=True, exist_ok=True)
        paths["home_dir"].mkdir(parents=True, exist_ok=True)

    def _seed_bootstrap(self, home_dir: Path, workspace_dir: Path, runtime_name: str, worker: dict) -> None:
        apply_bootstrap(
            home_dir=home_dir,
            workspace_dir=workspace_dir,
            runtime_name=runtime_name,
            worker=worker,
            copy_file=self._copy_file,
            copy_tree=self._copy_tree,
        )

    def _copy_file(self, src: Path, dest: Path) -> None:
        if not src.exists() or dest.exists():
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)

    def _copy_tree(self, src: Path, dest: Path) -> None:
        if not src.exists() or dest.exists():
            return
        shutil.copytree(src, dest, dirs_exist_ok=True)

    def _require_docker(self) -> None:
        if shutil.which("docker") is None:
            raise RuntimeError("Docker CLI is required for sandboxed workers but was not found on PATH")

    def _ensure_image(self) -> None:
        now = time.monotonic()
        if self._image_checked_at and self._image_checked_at + self.image_check_ttl_sec > now:
            return
        if self._docker(["image", "inspect", self.image], check=False, timeout_sec=self.image_inspect_timeout_sec).returncode == 0:
            self._image_checked_at = now
            return
        with self._build_lock:
            now = time.monotonic()
            if self._image_checked_at and self._image_checked_at + self.image_check_ttl_sec > now:
                return
            if self._docker(["image", "inspect", self.image], check=False, timeout_sec=self.image_inspect_timeout_sec).returncode == 0:
                self._image_checked_at = now
                return
            dockerfile = self.build_root / "Dockerfile"
            extension_policy = _ai_worker_browser_extension_policy_json()
            extension_policy_source = AI_WORKER_BROWSER_EXTENSION_POLICY_PATHS[0]
            extension_policy_dirs = " ".join(
                shlex.quote(str(Path(path).parent))
                for path in AI_WORKER_BROWSER_EXTENSION_POLICY_PATHS
            )
            extension_policy_writes = " && ".join(
                [
                    f"printf '%s\\n' {shlex.quote(extension_policy)} > {shlex.quote(extension_policy_source)}",
                    *(
                        f"cp {shlex.quote(extension_policy_source)} {shlex.quote(path)}"
                        for path in AI_WORKER_BROWSER_EXTENSION_POLICY_PATHS[1:]
                    ),
                ]
            )
            extension_check_script_lines = " ".join(
                shlex.quote(line)
                for line in _ai_worker_browser_extension_check_script().splitlines()
            )
            npm_worker_specs = " ".join(
                shlex.quote(spec)
                for spec in (
                    AI_WORKER_CODEX_NPM_SPEC,
                    AI_WORKER_CLAUDE_CODE_NPM_SPEC,
                    AI_WORKER_OPENCLAW_NPM_SPEC,
                )
            )
            dockerfile.write_text(
                "\n".join(
                    [
                        "FROM selenium/standalone-chromium:latest",
                        "USER root",
                        "RUN apt-get update && apt-get install -y --no-install-recommends bash ca-certificates curl file fonts-dejavu git gnupg jq less libreoffice-calc libreoffice-impress libreoffice-writer nano openssh-client pandoc pcmanfm poppler-utils procps python-is-python3 python3-pip ripgrep screen tmux tree vim wmctrl x11-utils xdotool xterm && rm -rf /var/lib/apt/lists/*",
                        "RUN if [ ! -x /usr/bin/locale-check ]; then printf '%s\\n' '#!/bin/sh' 'locale_value=${1:-C.UTF-8}' 'echo LANG=$locale_value' 'echo LC_ALL=$locale_value' > /usr/bin/locale-check && chmod +x /usr/bin/locale-check; fi",
                        "RUN mkdir -p /etc/apt/keyrings && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg && echo 'deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main' > /etc/apt/sources.list.d/nodesource.list",
                        "RUN apt-get update && apt-get install -y --no-install-recommends nodejs && node --version && npm --version && rm -rf /var/lib/apt/lists/*",
                        f"RUN npm install -g {npm_worker_specs}",
                        "RUN pip3 install --no-cache-dir selenium beautifulsoup4 markdown matplotlib openpyxl pdf2image pillow PyMuPDF PyPDF2 python-docx python-pptx reportlab xlsxwriter",
                        f"RUN mkdir -p {extension_policy_dirs} && {extension_policy_writes}",
                        f"RUN printf '%s\\n' {extension_check_script_lines} > /usr/local/bin/glasshive-browser-extension-check && chmod +x /usr/local/bin/glasshive-browser-extension-check && glasshive-browser-extension-check",
                        "RUN mkdir -p /workspace/project /workspace/.wpr-home",
                        "USER seluser",
                        "WORKDIR /workspace/project",
                        "ENV SHELL=/bin/bash",
                        "ENV DISPLAY=:99.0",
                        "ENV TERM=xterm-256color",
                        "",
                    ]
                )
            )
            result = self._docker(
                ["build", "-t", self.image, str(self.build_root)],
                check=False,
                capture_output=True,
                timeout_sec=self.image_build_timeout_sec,
            )
            if result.returncode != 0:
                raise RuntimeError(f"Failed to build sandbox image {self.image}: {(result.stderr or result.stdout or '').strip()[-2000:]}")
            self._image_checked_at = time.monotonic()

    def _create_container(self, container_name: str, paths: dict[str, Path]) -> None:
        command = [
            "run",
            "-d",
            "--init",
            "--name",
            container_name,
            "--hostname",
            container_name,
            "--workdir",
            self.workspace_mount,
            "-e",
            f"HOME={self.home_mount}",
            "-e",
            f"TERM={self.term_value}",
            "-e",
            f"TMPDIR={self._browser_tmp_dir()}",
            "-e",
            f"XDG_CACHE_HOME={self._browser_cache_dir()}",
            "-e",
            f"XDG_CONFIG_HOME={self._browser_config_dir()}",
            "-e",
            f"SE_VNC_NO_PASSWORD={'1' if self.vnc_no_password else '0'}",
            *self._host_gateway_args(),
            "-p",
            f"127.0.0.1::{self.novnc_container_port}",
            "-p",
            f"127.0.0.1::{self.selenium_container_port}",
            "-p",
            f"127.0.0.1::{self.openclaw_container_port}",
            "--shm-size",
            os.environ.get("WPR_SANDBOX_SHM_SIZE", "1g"),
            "-v",
            f"{paths['workspace_dir']}:{self.workspace_mount}",
            "-v",
            f"{paths['home_dir']}:{self.home_mount}",
            self.image,
        ]
        self._insert_resource_limits(command)
        result = self._docker(command, check=False, capture_output=True)
        if result.returncode != 0:
            raise RuntimeError(f"Failed to create worker sandbox {container_name}: {(result.stderr or result.stdout or '').strip()[-2000:]}")

    def _host_gateway_args(self) -> list[str]:
        if not self._env_flag("WPR_SANDBOX_ADD_HOST_GATEWAY", True):
            return []
        return ["--add-host", "host.docker.internal:host-gateway"]

    def _insert_resource_limits(self, command: list[str]) -> None:
        resource_args: list[str] = []
        if self.memory_limit:
            resource_args.extend(["--memory", self.memory_limit])
        if self.memory_swap_limit:
            resource_args.extend(["--memory-swap", self.memory_swap_limit])
        if self.cpu_limit:
            resource_args.extend(["--cpus", self.cpu_limit])
        if self.pids_limit:
            resource_args.extend(["--pids-limit", self.pids_limit])
        if not resource_args:
            return
        image_index = len(command) - 1
        command[image_index:image_index] = resource_args

    def _docker(
        self,
        args: list[str],
        *,
        check: bool = True,
        capture_output: bool = False,
        timeout_sec: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = ["docker", *args]
        raw_timeout = os.environ.get("WPR_DOCKER_COMMAND_TIMEOUT_SEC", "60").strip()
        if timeout_sec is None:
            try:
                timeout_sec = float(raw_timeout)
            except ValueError:
                timeout_sec = 60.0
        timeout_sec = timeout_sec if timeout_sec and timeout_sec > 0 else None
        try:
            return subprocess.run(
                command,
                check=check,
                text=True,
                stdout=subprocess.PIPE if capture_output else subprocess.DEVNULL,
                stderr=subprocess.PIPE if capture_output else subprocess.DEVNULL,
                timeout=timeout_sec,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout if isinstance(exc.stdout, str) else ""
            stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            stderr = (stderr + f"\nDocker command timed out after {timeout_sec:g}s").strip()
            if check:
                raise RuntimeError(stderr) from exc
            return subprocess.CompletedProcess(command, returncode=124, stdout=stdout, stderr=stderr)

    def _docker_exec(
        self,
        container_name: str,
        command: list[str],
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        detach: bool = False,
        fire_and_forget: bool = False,
        user: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        args = ["exec"]
        if detach:
            args.append("-d")
        args.extend(["-u", user or self.user])
        if cwd:
            args.extend(["-w", cwd])
        for key, value in sorted((env or {}).items()):
            args.extend(["-e", f"{key}={value}"])
        args.append(container_name)
        args.extend(command)
        raw_timeout = os.environ.get("WPR_DOCKER_EXEC_TIMEOUT_SEC", "15").strip()
        try:
            timeout_sec = float(raw_timeout) if raw_timeout else None
        except ValueError:
            timeout_sec = None
        if detach and fire_and_forget:
            full_command = ["docker", *args]
            self._spawn_detached_docker_exec(full_command)
            return subprocess.CompletedProcess(full_command, returncode=0, stdout="", stderr="")
        return self._docker(args, check=False, capture_output=True, timeout_sec=timeout_sec)

    @staticmethod
    def _spawn_detached_docker_exec(full_command: list[str]) -> None:
        # Start a tiny shell trampoline instead of invoking the Docker CLI inside
        # the request path. Docker Desktop can take seconds to accept an
        # interactive exec; the HTTP/UI path must return immediately.
        launch = ["sh", "-lc", f"sleep 0.1; exec {shlex.join(full_command)}"]
        try:
            subprocess.Popen(launch, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError:
            return

    def _ensure_container_writable_paths(self, container_name: str, container_paths: list[str]) -> None:
        safe_paths = [path for path in container_paths if path and path.startswith("/")]
        if not safe_paths:
            return
        quoted_paths = " ".join(shlex.quote(path) for path in safe_paths)
        container_user = shlex.quote(self.user.split(":", 1)[0] or self.user)
        host_uid = shlex.quote(str(os.getuid()))
        script = (
            "set -e; "
            f"mkdir -p {quoted_paths}; "
            "if command -v setfacl >/dev/null 2>&1 "
            f"&& setfacl -R -m u:{container_user}:rwX,u:{host_uid}:rwX {quoted_paths} 2>/dev/null; then "
            f"find {quoted_paths} -type d -exec setfacl -m d:u:{container_user}:rwX,d:u:{host_uid}:rwX {{}} + 2>/dev/null || true; "
            "else "
            f"chmod -R a+rwX {quoted_paths} 2>/dev/null || true; "
            "fi"
        )
        result = self._docker_exec(
            container_name,
            ["bash", "-c", script],
            env={
                "HOME": self.home_mount,
                "TERM": self.term_value,
            },
            cwd=self.workspace_mount,
            user="root",
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[-1200:]
            raise RuntimeError(f"Failed to prepare writable sandbox paths in {container_name}: {detail}")

    def _harden_secret_runtime_files(self, container_name: str) -> None:
        user = shlex.quote(self.user)
        secret_dir = shlex.quote(f"{self.home_mount}/.glasshive")
        script = (
            "set -e; "
            f"for file in {secret_dir}/secret-runtime.env {secret_dir}/secret-runtime.keys; do "
            '[ -e "$file" ] || continue; '
            f"chown {user} \"$file\" 2>/dev/null || true; "
            'chmod 600 "$file" 2>/dev/null || true; '
            "done"
        )
        self._docker_exec(
            container_name,
            ["bash", "-c", script],
            env={
                "HOME": self.home_mount,
                "TERM": self.term_value,
            },
            cwd=self.workspace_mount,
            user="root",
        )

    def _ensure_screen_runtime_dir(self, container_name: str) -> None:
        screen_user = self.user.split(":", 1)[0] or self.user
        screen_dir = f"/run/screen/S-{screen_user}"
        script = (
            "set -e; "
            "mkdir -p /run/screen "
            f"{shlex.quote(screen_dir)}; "
            "chmod 1777 /run/screen; "
            f"chown {shlex.quote(self.user)} {shlex.quote(screen_dir)} 2>/dev/null || true; "
            f"chmod 700 {shlex.quote(screen_dir)}"
        )
        result = self._docker_exec(
            container_name,
            ["bash", "-c", script],
            env={
                "HOME": self.home_mount,
                "TERM": self.term_value,
            },
            cwd=self.workspace_mount,
            user="root",
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[-1200:]
            raise RuntimeError(f"Failed to prepare screen runtime directory in {container_name}: {detail}")

    def _set_plain_background(self, container_name: str) -> None:
        script = (
            "for i in $(seq 1 60); do "
            f"DISPLAY={shlex.quote(self.display_value)} timeout 2s xsetroot -solid black >/dev/null 2>&1 || true; "
            "sleep 0.5; "
            "done"
        )
        self._docker_exec(
            container_name,
            ["bash", "-c", script],
            env=self._desktop_env(),
            cwd=self.workspace_mount,
            detach=True,
            fire_and_forget=True,
        )

    def _prime_idle_desktop(self, container_name: str) -> None:
        safe_url = shlex.quote(self._default_browser_url())
        self._docker_exec(
            container_name,
            [
                "bash",
                "-c",
                (
                    f"(chromium --no-sandbox --disable-dev-shm-usage --new-window {safe_url} >/dev/null 2>&1 &) ; "
                    "sleep 1; "
                    "wmctrl -xa chromium.Chromium || wmctrl -a Chromium || true"
                ),
            ],
            env=self._desktop_env(),
            cwd=self.workspace_mount,
        )

    def _desktop_action_command(
        self,
        action: str,
        *,
        url: str | None = None,
        session_name: str | None = None,
    ) -> list[str] | None:
        safe_url = (url or "").strip() or self._default_browser_url()
        workspace = shlex.quote(self.workspace_mount)
        title = {
            "terminal": "WPR Shell",
            "files": "WPR Files",
            "codex": "Codex CLI",
            "claude": "Claude Code",
            "openclaw": "OpenClaw CLI",
        }
        if action == "terminal":
            attach_script = f"cd {workspace}; exec bash --noprofile --norc"
            if session_name:
                session_literal = shlex.quote(session_name)
                attach_script = (
                    f"cd {workspace}; "
                    f"SESSION={session_literal}; "
                    "for _ in $(seq 1 180); do "
                    "if screen -ls | grep -Fq \".${SESSION}\"; then exec screen -xRR \"$SESSION\"; fi; "
                    "sleep 1; "
                    "done; "
                    "printf '\\nLive session %s was not found. Opening a shell instead.\\n' \"$SESSION\"; "
                    "exec bash --noprofile --norc"
                )
            return [
                "xterm",
                "-bg",
                "black",
                "-fg",
                "#f5f5f5",
                "-fa",
                "Monospace",
                "-fs",
                "11",
                "-geometry",
                "140x40",
                "-T",
                "WPR Live Run" if session_name else title["terminal"],
                "-e",
                "bash",
                "--noprofile",
                "--norc",
                "-c",
                attach_script,
            ]
        if action == "files":
            return ["pcmanfm", self.workspace_mount]
        if action == "browser":
            return [
                "chromium",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--start-maximized",
                "--new-tab",
                safe_url,
            ]
        if action == "focus_browser":
            return [
                "bash",
                "-lc",
                "wmctrl -xa chromium.Chromium || wmctrl -a Chromium || xdotool search --onlyvisible --class chromium windowactivate || true",
            ]
        if action == "codex":
            return [
                "xterm",
                "-fa",
                "Monospace",
                "-fs",
                "11",
                "-geometry",
                "150x44",
                "-bg",
                "black",
                "-fg",
                "#f5f5f5",
                "-T",
                title["codex"],
                "-e",
                "bash",
                "--noprofile",
                "--norc",
                "-c",
                f"cd {workspace}; exec codex",
            ]
        if action == "claude":
            return [
                "xterm",
                "-fa",
                "Monospace",
                "-fs",
                "11",
                "-geometry",
                "150x44",
                "-bg",
                "black",
                "-fg",
                "#f5f5f5",
                "-T",
                title["claude"],
                "-e",
                "bash",
                "--noprofile",
                "--norc",
                "-c",
                f"cd {workspace}; exec claude --dangerously-skip-permissions",
            ]
        if action == "openclaw":
            return [
                "xterm",
                "-fa",
                "Monospace",
                "-fs",
                "11",
                "-geometry",
                "150x44",
                "-bg",
                "black",
                "-fg",
                "#f5f5f5",
                "-T",
                title["openclaw"],
                "-e",
                "bash",
                "--noprofile",
                "--norc",
                "-c",
                (
                    f"cd {workspace}; "
                    "if [ -f \"$HOME/.wpr-openclaw/openclaw.env\" ]; then "
                    "source \"$HOME/.wpr-openclaw/openclaw.env\"; "
                    "fi; "
                    "echo 'OpenClaw workstation shell ready.'; "
                    "echo 'Useful commands: openclaw status | openclaw sessions | openclaw tui'; "
                    "exec bash"
                ),
            ]
        return None

    def _host_port_for(self, ports: dict[str, object], container_port: int) -> int | None:
        binding = ports.get(f"{container_port}/tcp")
        if not binding or not isinstance(binding, list):
            return None
        first = binding[0] or {}
        host_port = str(first.get("HostPort") or "").strip()
        return int(host_port) if host_port.isdigit() else None
