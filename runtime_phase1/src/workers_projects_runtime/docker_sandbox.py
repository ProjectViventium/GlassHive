from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from urllib.parse import quote, urlencode

from .bootstrap import apply_bootstrap, bootstrap_env_for


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


class DockerSandboxManager:
    _build_lock = Lock()
    _default_image = "workers-projects-runtime-workstation:phase1-node22"

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

    def _env_flag(self, name: str, default: bool) -> bool:
        raw = str(os.environ.get(name, "")).strip().lower()
        if not raw:
            return default
        return raw in {"1", "true", "yes", "on"}

    def ensure_ready(self, worker: dict, runtime_name: str, *, start_if_paused: bool = True) -> SandboxInfo:
        self._require_docker()
        self._ensure_image()
        paths = self._paths(worker["worker_id"])
        self._ensure_host_dirs(paths)
        self._seed_bootstrap(paths["home_dir"], paths["workspace_dir"], runtime_name, worker)
        container_name = self._container_name(worker["worker_id"])
        sandbox = self.inspect(worker["worker_id"])
        needs_idle_prime = False
        if sandbox is None:
            self._create_container(container_name, paths)
            sandbox = self.inspect(worker["worker_id"])
            needs_idle_prime = True
        if sandbox is None:
            raise RuntimeError("Failed to create worker sandbox")
        if sandbox.state == "paused" and start_if_paused:
            self._docker(["unpause", container_name])
            sandbox = self.inspect(worker["worker_id"])
        elif sandbox.state in {"created", "exited", "dead"}:
            self._docker(["start", container_name])
            sandbox = self.inspect(worker["worker_id"])
            needs_idle_prime = True
        if sandbox is None:
            raise RuntimeError("Failed to start worker sandbox")
        self._set_plain_background(sandbox.container_name)
        if needs_idle_prime and self._env_flag("WPR_IDLE_DESKTOP_PRIME_BROWSER", True):
            self._prime_idle_desktop(sandbox.container_name)
        return sandbox

    def inspect(self, worker_id: str) -> SandboxInfo | None:
        container_name = self._container_name(worker_id)
        result = self._docker(["inspect", container_name], check=False, capture_output=True)
        if result.returncode != 0:
            return None
        try:
            payload = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            return None
        if not payload:
            return None
        entry = payload[0]
        state = entry.get("State") or {}
        status = str(state.get("Status") or "unknown")
        if bool(state.get("Paused")):
            status = "paused"
        pid = state.get("Pid")
        ports = entry.get("NetworkSettings", {}).get("Ports") or {}
        return SandboxInfo(
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
        return self.inspect(worker_id) or sandbox

    def terminate(self, worker_id: str) -> SandboxInfo:
        sandbox = self.inspect(worker_id)
        if sandbox is not None:
            self._docker(["rm", "-f", sandbox.container_name], check=False)
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
        sandbox = self.ensure_ready(resolved_worker, runtime_name=runtime_name)
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
        merged_env = {**bootstrap_env_for(resolved_worker), **(env or {})}
        for key, value in sorted(merged_env.items()):
            if value is None:
                continue
            docker_command.extend(["-e", f"{key}={value}"])
        docker_command.append(sandbox.container_name)
        docker_command.extend(command)
        return docker_command

    def terminal_attach_command(self, worker_id: str, runtime_name: str, session_name: str = "operator") -> list[str]:
        sandbox = self.ensure_ready({"worker_id": worker_id}, runtime_name=runtime_name)
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
            sandbox.container_name,
            "screen",
            "-xRR",
            session_name,
        ]

    def list_screen_sessions(self, worker_id: str, runtime_name: str, *, worker: dict | None = None) -> list[str]:
        resolved_worker = worker or {"worker_id": worker_id}
        sandbox = self.ensure_ready(resolved_worker, runtime_name=runtime_name)
        result = self._docker_exec(
            sandbox.container_name,
            ["bash", "-lc", "screen -ls || true"],
            env={
                "HOME": self.home_mount,
                "TERM": self.term_value,
                "DISPLAY": self.display_value,
            },
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
        sandbox = self.ensure_ready(resolved_worker, runtime_name=runtime_name)
        merged_env = {
            "HOME": self.home_mount,
            "TERM": self.term_value,
            "DISPLAY": self.display_value,
            **bootstrap_env_for(resolved_worker),
            **(env or {}),
        }
        self.stop_screen_session(worker_id, runtime_name, session_name, worker=resolved_worker, missing_ok=True)
        return self._docker_exec(
            sandbox.container_name,
            ["screen", "-DmS", session_name, *command],
            env=merged_env,
            cwd=self.workspace_mount,
        )

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
        sandbox = self.ensure_ready(resolved_worker, runtime_name=runtime_name)
        result = self._docker_exec(
            sandbox.container_name,
            ["bash", "-c", f"screen -S {shlex.quote(session_name)} -X quit >/dev/null 2>&1 || true"],
            env={
                "HOME": self.home_mount,
                "TERM": self.term_value,
                "DISPLAY": self.display_value,
            },
            cwd=self.workspace_mount,
        )
        if result.returncode != 0 and not missing_ok:
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
        sandbox = self.ensure_ready(resolved_worker, runtime_name=runtime_name)
        run_root = f"{self.home_mount}/.glasshive-runs/{run_id}"
        script = (
            f"needle={shlex.quote(run_root)}; "
            "pids=$(ps -eo pid=,ppid=,args= | awk -v needle=\"$needle\" 'index($0, needle) > 0 { print $1 }'); "
            "if [ -z \"$pids\" ]; then exit 0; fi; "
            "for pid in $pids; do pkill -TERM -P \"$pid\" >/dev/null 2>&1 || true; kill -TERM \"$pid\" >/dev/null 2>&1 || true; done; "
            "sleep 1; "
            "for pid in $pids; do pkill -KILL -P \"$pid\" >/dev/null 2>&1 || true; kill -KILL \"$pid\" >/dev/null 2>&1 || true; done"
        )
        self._docker_exec(
            sandbox.container_name,
            ["bash", "-lc", script],
            env={
                "HOME": self.home_mount,
                "TERM": self.term_value,
                "DISPLAY": self.display_value,
            },
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
        sandbox = self.ensure_ready(resolved_worker, runtime_name=runtime_name)
        normalized = action.strip().lower().replace("-", "_")
        command = self._desktop_action_command(normalized, url=url, session_name=session_name)
        if not command:
            raise ValueError(f"Unsupported desktop action: {action}")
        merged_env = {
            "HOME": self.home_mount,
            "TERM": self.term_value,
            "DISPLAY": self.display_value,
            **bootstrap_env_for(resolved_worker),
        }
        result = self._docker_exec(
            sandbox.container_name,
            command,
            env=merged_env,
            cwd=self.workspace_mount,
            detach=True,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[-1200:]
            raise RuntimeError(f"Desktop action {action} failed: {detail}")
        return {
            "action": normalized,
            "container_name": sandbox.container_name,
            "view_url": self.view_url(worker_id),
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

    def _container_name(self, worker_id: str) -> str:
        token = worker_id.replace("_", "-").lower()
        return f"wpr-{token}"

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
        if self._docker(["image", "inspect", self.image], check=False).returncode == 0:
            return
        with self._build_lock:
            if self._docker(["image", "inspect", self.image], check=False).returncode == 0:
                return
            dockerfile = self.build_root / "Dockerfile"
            dockerfile.write_text(
                "\n".join(
                    [
                        "FROM selenium/standalone-chromium:latest",
                        "USER root",
                        "RUN apt-get update && apt-get install -y --no-install-recommends bash ca-certificates curl git gnupg jq less nano openssh-client pcmanfm procps python-is-python3 python3-pip ripgrep screen tmux tree vim wmctrl x11-utils xdotool xterm && rm -rf /var/lib/apt/lists/*",
                        "RUN mkdir -p /etc/apt/keyrings && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg && echo 'deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main' > /etc/apt/sources.list.d/nodesource.list",
                        "RUN apt-get update && apt-get install -y --no-install-recommends nodejs && node --version && npm --version && rm -rf /var/lib/apt/lists/*",
                        "RUN npm install -g @openai/codex @anthropic-ai/claude-code openclaw@latest",
                        "RUN pip3 install --no-cache-dir selenium",
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
            result = self._docker(["build", "-t", self.image, str(self.build_root)], check=False, capture_output=True)
            if result.returncode != 0:
                raise RuntimeError(f"Failed to build sandbox image {self.image}: {(result.stderr or result.stdout or '').strip()[-2000:]}")

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
            f"SE_VNC_NO_PASSWORD={'1' if self.vnc_no_password else '0'}",
            "-p",
            f"127.0.0.1::{self.novnc_container_port}",
            "-p",
            f"127.0.0.1::{self.selenium_container_port}",
            "-p",
            f"127.0.0.1::{self.openclaw_container_port}",
            "--shm-size",
            os.environ.get("WPR_SANDBOX_SHM_SIZE", "2g"),
            "-v",
            f"{paths['workspace_dir']}:{self.workspace_mount}",
            "-v",
            f"{paths['home_dir']}:{self.home_mount}",
            self.image,
        ]
        result = self._docker(command, check=False, capture_output=True)
        if result.returncode != 0:
            raise RuntimeError(f"Failed to create worker sandbox {container_name}: {(result.stderr or result.stdout or '').strip()[-2000:]}")

    def _docker(self, args: list[str], *, check: bool = True, capture_output: bool = False) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["docker", *args],
            check=check,
            text=True,
            stdout=subprocess.PIPE if capture_output else subprocess.DEVNULL,
            stderr=subprocess.PIPE if capture_output else subprocess.DEVNULL,
        )

    def _docker_exec(
        self,
        container_name: str,
        command: list[str],
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        detach: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        args = ["exec"]
        if detach:
            args.append("-d")
        args.extend(["-u", self.user])
        if cwd:
            args.extend(["-w", cwd])
        for key, value in sorted((env or {}).items()):
            args.extend(["-e", f"{key}={value}"])
        args.append(container_name)
        args.extend(command)
        return self._docker(args, check=False, capture_output=True)

    def _set_plain_background(self, container_name: str) -> None:
        self._docker_exec(
            container_name,
            ["bash", "-c", "xsetroot -solid black >/dev/null 2>&1 || true"],
            env={
                "HOME": self.home_mount,
                "TERM": self.term_value,
                "DISPLAY": self.display_value,
            },
            cwd=self.workspace_mount,
        )

    def _prime_idle_desktop(self, container_name: str) -> None:
        safe_url = shlex.quote(self._default_browser_url())
        self._docker_exec(
            container_name,
            [
                "bash",
                "-lc",
                (
                    f"(chromium --no-sandbox --disable-dev-shm-usage --new-window {safe_url} >/dev/null 2>&1 &) ; "
                    "sleep 1; "
                    "wmctrl -xa chromium.Chromium || wmctrl -a Chromium || true"
                ),
            ],
            env={
                "HOME": self.home_mount,
                "TERM": self.term_value,
                "DISPLAY": self.display_value,
            },
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
