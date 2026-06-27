from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
import time
from pathlib import Path

from workers_projects_runtime.docker_sandbox import (
    DockerSandboxManager,
    SandboxInfo,
    _ai_worker_browser_extension_check_script,
    _ai_worker_browser_native_host_bootstrap_script,
    _safe_docker_exec_env,
)
from workers_projects_runtime.bootstrap import GLASSHIVE_CRITICAL_OPERATING_INSTRUCTIONS, GLASSHIVE_SAFETY_CHECKPOINT_RULE


def test_safe_docker_exec_env_preserves_claude_headless_oauth_only():
    env = _safe_docker_exec_env(
        {
            "CLAUDE_CODE_OAUTH_TOKEN": "oauth-token",
            "ANTHROPIC_API_KEY": "api-key",
            "UNRELATED_SECRET": "must-not-pass",
        }
    )

    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-token"
    assert env["ANTHROPIC_API_KEY"] == "api-key"
    assert "UNRELATED_SECRET" not in env


def test_seed_bootstrap_writes_default_worker_contract_without_bundle(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    home_dir = tmp_path / "home"
    workspace_dir = tmp_path / "workspace"
    home_dir.mkdir()
    workspace_dir.mkdir()

    manager._seed_bootstrap(home_dir, workspace_dir, "codex-cli", {"worker_id": "wrk_contract"})

    agents_text = (workspace_dir / "AGENTS.md").read_text()
    assert "GlassHive Worker Contract" in agents_text
    assert GLASSHIVE_CRITICAL_OPERATING_INSTRUCTIONS in agents_text
    assert GLASSHIVE_SAFETY_CHECKPOINT_RULE in agents_text
    assert "Less is more" in agents_text
    assert "Do not force a download" in agents_text
    assert "@AGENTS.md" in (workspace_dir / "CLAUDE.md").read_text()


def test_create_container_adds_host_gateway_alias_for_broker_reachability(tmp_path, monkeypatch):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    captured: list[list[str]] = []

    def fake_docker(args: list[str], **kwargs):
        captured.append(args)
        return subprocess.CompletedProcess(["docker", *args], returncode=0, stdout="cid", stderr="")

    manager._docker = fake_docker  # type: ignore[method-assign]
    manager._create_container(
        "wpr-test",
        {
            "workspace_dir": tmp_path / "workspace",
            "home_dir": tmp_path / "home",
        },
    )

    command = captured[-1]
    assert "--add-host" in command
    assert "host.docker.internal:host-gateway" in command
    assert "--security-opt" in command
    assert "seccomp=unconfined" in command
    assert f"TMPDIR={manager.service_tmp_dir}" in command
    assert f"TMPDIR={manager._browser_tmp_dir()}" not in command
    assert f"XDG_CACHE_HOME={manager._browser_cache_dir()}" in command
    assert f"XDG_CONFIG_HOME={manager._browser_config_dir()}" in command

    monkeypatch.setenv("WPR_SANDBOX_ADD_HOST_GATEWAY", "0")
    manager_without_alias = DockerSandboxManager(base_dir=str(tmp_path / "disabled"))
    captured.clear()
    manager_without_alias._docker = fake_docker  # type: ignore[method-assign]
    manager_without_alias._create_container(
        "wpr-test",
        {
            "workspace_dir": tmp_path / "workspace",
            "home_dir": tmp_path / "home",
        },
    )

    assert "--add-host" not in captured[-1]
    assert "--security-opt" in captured[-1]
    assert "seccomp=unconfined" in captured[-1]

    monkeypatch.setenv("WPR_SANDBOX_ALLOW_CHROMIUM_USERNS", "0")
    manager_without_chromium_userns = DockerSandboxManager(base_dir=str(tmp_path / "no-userns"))
    captured.clear()
    manager_without_chromium_userns._docker = fake_docker  # type: ignore[method-assign]
    manager_without_chromium_userns._create_container(
        "wpr-test",
        {
            "workspace_dir": tmp_path / "workspace",
            "home_dir": tmp_path / "home",
        },
    )

    assert "--security-opt" not in captured[-1]
    assert "seccomp=unconfined" not in captured[-1]


def test_describe_self_heals_novnc_when_service_port_resets(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    calls: list[tuple[str, object]] = []

    sandbox = SandboxInfo(
        container_name="wpr-wrk-test",
        container_id="cid",
        state="running",
        workspace_dir=str(tmp_path / "workspace"),
        home_dir=str(tmp_path / "home"),
        pid=1234,
        image="img",
        novnc_port=57900,
        selenium_port=57901,
        openclaw_port=57902,
    )
    manager.inspect = lambda worker_id: sandbox  # type: ignore[method-assign]

    readiness = iter([False, True])
    manager._novnc_http_ready = lambda port: next(readiness)  # type: ignore[method-assign]

    def fake_exec(container_name, command, *, env=None, cwd=None, detach=False, fire_and_forget=False, user=None):
        calls.append(("exec", command))
        return subprocess.CompletedProcess(["docker"], returncode=0, stdout="", stderr="")

    manager._docker_exec = fake_exec  # type: ignore[method-assign]

    details = manager.describe("wrk_test")

    assert details["view_available"] is True
    assert details["view_url"] == "http://127.0.0.1:57900/?autoconnect=1&resize=scale&reconnect=1&show_dot=1"
    assert details["view_health"] == {"healthy": True, "repaired": True, "reason": "ok"}
    assert calls
    repair_script = str(calls[0][1])
    assert "TMPDIR=/tmp" in repair_script
    assert manager._browser_tmp_dir() not in repair_script


def test_inspect_reports_paused_when_docker_state_is_paused(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))

    payload = [
        {
            "Id": "abc123",
            "State": {"Status": "running", "Paused": True, "Pid": 4242},
            "NetworkSettings": {
                "Ports": {
                    "7900/tcp": [{"HostIp": "127.0.0.1", "HostPort": "58100"}],
                    "4444/tcp": [{"HostIp": "127.0.0.1", "HostPort": "58101"}],
                }
            },
        }
    ]

    def fake_docker(args: list[str], *, check: bool = True, capture_output: bool = False, **kwargs):
        return subprocess.CompletedProcess(
            ["docker", *args],
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )

    manager._docker = fake_docker  # type: ignore[method-assign]

    sandbox = manager.inspect("wrk_test")
    assert sandbox is not None
    assert sandbox.state == "paused"
    assert sandbox.pid is None
    assert sandbox.novnc_port == 58100
    assert sandbox.selenium_port == 58101


def test_fast_sandbox_does_not_treat_projected_paths_as_container_evidence(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))

    def missing_docker(args: list[str], *, check: bool = True, capture_output: bool = False, **kwargs):
        return subprocess.CompletedProcess(["docker", *args], returncode=1, stdout="", stderr="No such container")

    manager._docker = missing_docker  # type: ignore[method-assign]

    worker = {
        "worker_id": "wrk_projected",
        "state": "ready",
        "state_dir": str(tmp_path / "state"),
        "workspace_dir": str(tmp_path / "workspace"),
    }

    assert manager.fast_sandbox_from_worker(worker) is None


def test_ensure_ready_creates_container_when_only_projected_paths_exist(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    created: list[str] = []

    def fake_inspect(worker_id: str):
        if created:
            return SandboxInfo(
                container_name="wpr-wrk-projected",
                container_id="container123",
                state="running",
                workspace_dir=str(tmp_path / "docker_sandboxes" / "workers" / worker_id / "state" / "workspace"),
                home_dir=str(tmp_path / "docker_sandboxes" / "workers" / worker_id / "state" / "home"),
                pid=4242,
                image=manager.image,
            )
        return None

    manager._require_docker = lambda: None  # type: ignore[method-assign]
    manager._ensure_image = lambda: None  # type: ignore[method-assign]
    manager.inspect = fake_inspect  # type: ignore[method-assign]
    manager._create_container = lambda container_name, paths: created.append(container_name)  # type: ignore[method-assign]
    manager._ensure_container_writable_paths = lambda container_name, paths: None  # type: ignore[method-assign]
    manager._harden_secret_runtime_files = lambda container_name: None  # type: ignore[method-assign]
    manager._set_plain_background = lambda container_name: None  # type: ignore[method-assign]
    manager._prime_idle_desktop = lambda container_name: None  # type: ignore[method-assign]

    worker = {
        "worker_id": "wrk_projected",
        "state": "ready",
        "state_dir": str(tmp_path / "state"),
        "workspace_dir": str(tmp_path / "workspace"),
    }

    sandbox = manager.ensure_ready(worker, runtime_name="codex-cli")

    assert created == ["wpr-wrk-projected"]
    assert sandbox.container_id == "container123"


def test_ensure_ready_recreates_ready_container_missing_chromium_userns(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    calls: list[str] = []
    removed = False
    created = False

    def fake_inspect(worker_id: str):
        if created:
            return SandboxInfo(
                container_name="wpr-wrk-test",
                container_id="container-new",
                state="running",
                workspace_dir=str(tmp_path / "workspace"),
                home_dir=str(tmp_path / "home"),
                pid=4242,
                image=manager.image,
                security_options=("seccomp=unconfined",),
            )
        if removed:
            return None
        return SandboxInfo(
            container_name="wpr-wrk-test",
            container_id="container-old",
            state="running",
            workspace_dir=str(tmp_path / "workspace"),
            home_dir=str(tmp_path / "home"),
            pid=4242,
            image=manager.image,
            security_options=(),
        )

    def fake_docker(args: list[str], **kwargs):
        nonlocal removed
        if args[:2] == ["rm", "-f"]:
            calls.append("rm")
            removed = True
            return subprocess.CompletedProcess(["docker", *args], returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected docker call: {args}")

    def fake_create_container(container_name, paths):
        nonlocal created
        calls.append(f"create:{container_name}")
        created = True

    manager._require_docker = lambda: calls.append("require")  # type: ignore[method-assign]
    manager._ensure_host_dirs = lambda paths: calls.append("host_dirs")  # type: ignore[method-assign]
    manager._seed_bootstrap = lambda *args, **kwargs: calls.append("seed")  # type: ignore[method-assign]
    manager.inspect = fake_inspect  # type: ignore[method-assign]
    manager._docker = fake_docker  # type: ignore[method-assign]
    manager._ensure_image = lambda: calls.append("image")  # type: ignore[method-assign]
    manager._create_container = fake_create_container  # type: ignore[method-assign]
    manager._ensure_container_writable_paths = lambda *args, **kwargs: calls.append("writable")  # type: ignore[method-assign]
    manager._harden_secret_runtime_files = lambda container_name: calls.append("harden")  # type: ignore[method-assign]
    manager._set_plain_background = lambda container_name: calls.append("background")  # type: ignore[method-assign]
    manager._prime_idle_desktop = lambda container_name: calls.append("prime")  # type: ignore[method-assign]

    sandbox = manager.ensure_ready({"worker_id": "wrk_test", "state": "ready", "container_id": "container-old"}, "codex-cli")

    assert sandbox.container_id == "container-new"
    assert sandbox.security_options == ("seccomp=unconfined",)
    assert calls == ["require", "host_dirs", "seed", "rm", "image", "create:wpr-wrk-test", "writable", "harden", "background", "prime"]


def test_inspect_uses_short_cache_and_stale_fallback(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    manager.inspect_cache_ttl_sec = 60
    calls = 0
    payload = [
        {
            "Id": "abc123",
            "State": {"Status": "running", "Paused": False, "Pid": 4242},
            "NetworkSettings": {"Ports": {"7900/tcp": [{"HostPort": "58100"}]}},
        }
    ]

    def fake_docker(args: list[str], *, check: bool = True, capture_output: bool = False, **kwargs):
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(["docker", *args], returncode=0, stdout=json.dumps(payload), stderr="")

    manager._docker = fake_docker  # type: ignore[method-assign]

    first = manager.inspect("wrk_test")
    second = manager.inspect("wrk_test")

    assert first is not None
    assert second is first
    assert calls == 1

    manager.inspect_cache_ttl_sec = -1

    def timeout_docker(args: list[str], *, check: bool = True, capture_output: bool = False, **kwargs):
        return subprocess.CompletedProcess(["docker", *args], returncode=124, stdout="", stderr="timed out")

    manager._docker = timeout_docker  # type: ignore[method-assign]

    assert manager.inspect("wrk_test") is first


def test_terminate_invalidates_inspect_cache_before_idle_resume(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    manager.inspect_cache_ttl_sec = 60
    exists = True
    calls: list[str] = []

    def running_payload() -> str:
        return json.dumps(
            [
                {
                    "Id": "abc123",
                    "State": {"Status": "running", "Paused": False, "Pid": 4242},
                    "NetworkSettings": {"Ports": {}},
                }
            ]
        )

    def fake_docker(args: list[str], *, check: bool = True, capture_output: bool = False, **kwargs):
        nonlocal exists
        if args[:1] == ["inspect"]:
            calls.append("inspect")
            return subprocess.CompletedProcess(["docker", *args], returncode=0 if exists else 1, stdout=running_payload() if exists else "", stderr="")
        if args[:2] == ["rm", "-f"]:
            calls.append("rm")
            exists = False
            return subprocess.CompletedProcess(["docker", *args], returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected docker call: {args}")

    def fake_create_container(container_name, paths):
        nonlocal exists
        calls.append("create")
        exists = True

    manager._docker = fake_docker  # type: ignore[method-assign]
    manager._require_docker = lambda: calls.append("require")  # type: ignore[method-assign]
    manager._ensure_host_dirs = lambda paths: calls.append("host_dirs")  # type: ignore[method-assign]
    manager._seed_bootstrap = lambda *args, **kwargs: calls.append("seed")  # type: ignore[method-assign]
    manager._ensure_image = lambda: calls.append("image")  # type: ignore[method-assign]
    manager._create_container = fake_create_container  # type: ignore[method-assign]
    manager._ensure_container_writable_paths = lambda *args, **kwargs: calls.append("repair")  # type: ignore[method-assign]
    manager._harden_secret_runtime_files = lambda container_name: calls.append("harden")  # type: ignore[method-assign]
    manager._set_plain_background = lambda container_name: calls.append("background")  # type: ignore[method-assign]
    manager._prime_idle_desktop = lambda container_name: calls.append("prime")  # type: ignore[method-assign]

    cached = manager.inspect("wrk_test")
    assert cached is not None
    manager.terminate("wrk_test")

    resumed = manager.ensure_ready({"worker_id": "wrk_test", "state": "starting"}, "codex-cli")

    assert resumed.container_name == "wpr-wrk-test"
    assert "create" in calls
    assert calls.count("inspect") >= 3


def test_docker_exec_timeout_returns_failed_result(tmp_path, monkeypatch):
    manager = DockerSandboxManager(base_dir=str(tmp_path))

    def fake_run(*args, **kwargs):
        _ = args, kwargs
        raise subprocess.TimeoutExpired(["docker", "exec"], timeout=2, output="", stderr="")

    monkeypatch.setenv("WPR_DOCKER_EXEC_TIMEOUT_SEC", "2")
    monkeypatch.setattr(subprocess, "run", fake_run)

    result = manager._docker_exec("wpr-test", ["bash", "-c", "sleep 99"])

    assert result.returncode == 124
    assert "timed out after 2s" in result.stderr


def test_docker_exec_detach_uses_popen_without_waiting(tmp_path, monkeypatch):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    calls: list[list[str]] = []

    class FakePopen:
        def __init__(self, command, *, stdout=None, stderr=None, **kwargs):
            calls.append(command)
            self.stdout = stdout
            self.stderr = stderr

    monkeypatch.setattr(subprocess, "Popen", FakePopen)

    result = manager._docker_exec(
        "wpr-test",
        ["bash", "-lc", "sleep 60"],
        env={"HOME": "/workspace/.wpr-home"},
        cwd="/workspace/project",
        detach=True,
        fire_and_forget=True,
    )

    assert result.returncode == 0
    deadline = time.time() + 1
    while not calls and time.time() < deadline:
        time.sleep(0.01)
    assert len(calls) == 1
    assert calls[0][:2] == ["sh", "-lc"]
    assert calls[0][2].startswith("sleep 0.1; exec docker exec -d -u seluser")
    assert "wpr-test bash -lc 'sleep 60'" in calls[0][2]


def test_docker_exec_detach_confirms_docker_accepts_command(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_docker(args: list[str], **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(["docker", *args], returncode=0, stdout="", stderr="")

    manager._docker = fake_docker  # type: ignore[method-assign]

    result = manager._docker_exec(
        "wpr-test",
        ["screen", "-DmS", "job-run_123", "bash", "run.sh"],
        env={"HOME": "/workspace/.wpr-home"},
        cwd="/workspace/project",
        detach=True,
    )

    assert result.returncode == 0
    assert calls[0][0][:2] == ["exec", "-d"]
    assert calls[0][1]["capture_output"] is True
    assert calls[0][1]["check"] is False
    assert "job-run_123" in calls[0][0]


def test_plain_background_retries_while_desktop_starts(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    captured: dict[str, object] = {}

    def fake_docker_exec(container_name, command, **kwargs):
        captured["container_name"] = container_name
        captured["command"] = command
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(["docker", "exec"], returncode=0, stdout="", stderr="")

    manager._docker_exec = fake_docker_exec  # type: ignore[method-assign]

    manager._set_plain_background("wpr-test")

    assert captured["container_name"] == "wpr-test"
    command = captured["command"]
    assert isinstance(command, list)
    assert command[:2] == ["bash", "-c"]
    assert "seq 1 60" in command[2]
    assert "xsetroot -solid black" in command[2]
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["detach"] is True
    assert kwargs["fire_and_forget"] is True
    assert kwargs["env"]["DISPLAY"] == manager.display_value


def test_seed_bootstrap_writes_project_scope_files(tmp_path, monkeypatch):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    home_dir = tmp_path / "home"
    workspace_dir = tmp_path / "workspace"
    upload_source = tmp_path / "uploaded.txt"
    upload_source.write_text("Uploaded content")
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", str(tmp_path))
    home_dir.mkdir(parents=True)
    workspace_dir.mkdir(parents=True)

    worker = {
        "worker_id": "wrk_test",
        "bootstrap_profile": "clean-room",
        "bootstrap_bundle_json": json.dumps(
            {
                "env": {"TEST_FLAG": "1"},
                "system_instructions": "Use operator checkpoints before risky actions.",
                "claude_project_mcp": {
                    "glass-hive": {
                        "type": "http",
                        "transport": "http",
                        "url": "http://127.0.0.1:8767/mcp",
                    }
                },
                "codex_config_append": "[mcp_servers.glass-hive]\nurl = \"http://127.0.0.1:8767/mcp\"",
                "claude_settings_local": {
                    "permissions": {
                        "allow": ["Bash(ls *)"],
                    }
                },
                "files": [
                    {
                        "scope": "workspace",
                        "path": "notes/bootstrap.txt",
                        "content": "Bootstrapped",
                    },
                    {
                        "scope": "workspace",
                        "path": "uploads/uploaded.txt",
                        "source_path": str(upload_source),
                    }
                ],
            }
        ),
    }

    manager._seed_bootstrap(home_dir, workspace_dir, "claude-code", worker)

    agents_text = (workspace_dir / "AGENTS.md").read_text()
    claude_text = (workspace_dir / "CLAUDE.md").read_text()
    assert "GlassHive Worker Contract" in agents_text
    assert "Use operator checkpoints before risky actions." in agents_text
    assert "@AGENTS.md" in claude_text
    assert "Use operator checkpoints before risky actions." in agents_text
    assert json.loads((workspace_dir / ".mcp.json").read_text())["mcpServers"]["glass-hive"]["url"] == "http://127.0.0.1:8767/mcp"
    assert json.loads((workspace_dir / ".claude" / "settings.local.json").read_text())["permissions"]["allow"] == ["Bash(ls *)"]
    assert "glass-hive" in (home_dir / ".codex" / "config.toml").read_text()
    assert stat.S_IMODE((workspace_dir / ".mcp.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((workspace_dir / ".claude" / "settings.local.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((home_dir / ".codex" / "config.toml").stat().st_mode) == 0o600
    assert (workspace_dir / "notes" / "bootstrap.txt").read_text() == "Bootstrapped"
    assert (workspace_dir / "uploads" / "uploaded.txt").read_text() == "Uploaded content"
    assert "TEST_FLAG" in (home_dir / ".glasshive" / "runtime.env").read_text()
    manifest = json.loads((home_dir / ".glasshive" / "bootstrap-manifest.json").read_text())
    assert manifest["bootstrap_profile"] == "clean-room"
    assert "claude_project_mcp" in manifest["bundle_keys"]


def test_terminal_desktop_action_waits_for_live_session(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    command = manager._desktop_action_command("terminal", session_name="job-run_123456")
    assert command is not None
    assert command[0] == "xterm"
    assert "WPR Live Run" in command
    assert "screen -xRR" in command[-1]
    assert "job-run_123456" in command[-1]


def test_browser_desktop_action_uses_clean_chromium_profile_and_no_no_sandbox(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    command = manager._desktop_action_command("browser", url="https://example.test/report")
    assert command is not None
    assert command[:2] == ["bash", "-lc"]
    launch_script = command[-1]
    syntax = subprocess.run(["bash", "-n"], input=launch_script, text=True, capture_output=True)
    assert syntax.returncode == 0, syntax.stderr
    assert "--no-sandbox" not in launch_script
    assert "--disable-dev-shm-usage" in launch_script
    assert "--no-first-run" in launch_script
    assert "--no-default-browser-check" in launch_script
    assert "/usr/bin/chromium-base" in launch_script
    assert "glasshive-browser-native-host-bootstrap" in launch_script
    assert "--start-maximized" in launch_script
    assert "--new-tab" in launch_script
    assert "bookmark_bar" in launch_script
    assert "show_on_all_tabs" in launch_script
    assert "https://example.test/report" in launch_script


def test_prime_idle_desktop_uses_clean_chromium_profile_and_no_no_sandbox(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    calls: list[tuple[str, list[str]]] = []

    def fake_docker_exec(container_name, command, *, env=None, cwd=None, detach=False, fire_and_forget=False, user=None):
        calls.append((container_name, command))
        return subprocess.CompletedProcess(["docker"], returncode=0, stdout="", stderr="")

    manager._docker_exec = fake_docker_exec  # type: ignore[method-assign]

    manager._prime_idle_desktop("wpr-test")

    assert calls
    script = calls[-1][1][-1]
    assert calls[-1][1][:2] == ["bash", "-lc"]
    syntax = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
    assert syntax.returncode == 0, syntax.stderr
    assert "--no-sandbox" not in script
    assert "--disable-dev-shm-usage" in script
    assert "--new-window" in script
    assert "nohup /usr/bin/chromium-base" in script
    assert "glasshive-browser-native-host-bootstrap" in script
    assert "bookmark_bar" in script
    assert "show_on_all_tabs" in script
    assert "wmctrl -xa chromium.Chromium" in script


def test_desktop_action_skips_heavy_path_repair_for_running_container(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    calls: list[str] = []
    envs: list[dict] = []

    class FakeSandbox:
        container_name = "wpr-test"
        state = "running"
        container_id = "cid"
        pid = 1234
        image = "img"
        novnc_port = 57900
        selenium_port = 57901
        openclaw_port = 57902

    manager._require_docker = lambda: None  # type: ignore[method-assign]
    manager._ensure_image = lambda: None  # type: ignore[method-assign]
    manager._ensure_host_dirs = lambda paths: None  # type: ignore[method-assign]
    manager._seed_bootstrap = lambda *args, **kwargs: None  # type: ignore[method-assign]
    manager._ensure_container_writable_paths = lambda *args, **kwargs: calls.append("writable")  # type: ignore[method-assign]
    manager._harden_secret_runtime_files = lambda container_name: calls.append("harden")  # type: ignore[method-assign]
    manager._set_plain_background = lambda container_name: calls.append("background")  # type: ignore[method-assign]
    manager.inspect = lambda worker_id: FakeSandbox()  # type: ignore[method-assign]

    def fake_docker_exec(container_name, command, *, env=None, cwd=None, detach=False, fire_and_forget=False, user=None):
        calls.append(f"exec:{detach}:{fire_and_forget}:{command[0]}")
        envs.append(dict(env or {}))
        return subprocess.CompletedProcess(["docker"], returncode=0, stdout="", stderr="")

    manager._docker_exec = fake_docker_exec  # type: ignore[method-assign]

    launched = manager.desktop_action("wrk_test", "codex-cli", "focus_browser")

    assert launched["status"] == "launched"
    assert "writable" not in calls
    assert calls == ["harden", "exec:True:True:bash"]
    assert envs[-1]["TMPDIR"] == manager._browser_tmp_dir()
    assert envs[-1]["XDG_CACHE_HOME"] == manager._browser_cache_dir()
    assert envs[-1]["XDG_CONFIG_HOME"] == manager._browser_config_dir()


def test_desktop_action_revalidates_projected_worker_without_container_evidence(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    calls: list[str] = []

    sandbox = SandboxInfo(
        container_name="wpr-wrk-test",
        container_id="container123",
        state="running",
        workspace_dir=str(tmp_path / "workspace"),
        home_dir=str(tmp_path / "home"),
        pid=1234,
        image="img",
        novnc_port=None,
    )
    manager.ensure_ready = lambda *args, **kwargs: calls.append("ensure") or sandbox  # type: ignore[method-assign]

    def fake_docker_exec(container_name, command, *, env=None, cwd=None, detach=False, fire_and_forget=False, user=None):
        calls.append(f"{container_name}:{detach}:{fire_and_forget}:{command[0]}")
        return subprocess.CompletedProcess(["docker"], returncode=0, stdout="", stderr="")

    manager._docker_exec = fake_docker_exec  # type: ignore[method-assign]

    launched = manager.desktop_action(
        "wrk_test",
        "codex-cli",
        "focus_browser",
        worker={"worker_id": "wrk_test", "state": "ready", "state_dir": str(tmp_path / "state")},
    )

    assert launched["status"] == "launched"
    assert launched["view_url"] is None
    assert calls == ["ensure", "wpr-wrk-test:True:True:bash"]


def test_ensure_ready_skips_image_probe_for_existing_container(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    calls: list[str] = []

    class FakeSandbox:
        container_name = "wpr-test"
        state = "running"
        container_id = "cid"
        workspace_dir = str(tmp_path / "workspace")
        home_dir = str(tmp_path / "home")
        pid = 1234
        image = "img"
        novnc_port = 57900
        selenium_port = 57901
        openclaw_port = 57902

    manager._require_docker = lambda: calls.append("require")  # type: ignore[method-assign]
    manager._ensure_image = lambda: calls.append("image")  # type: ignore[method-assign]
    manager._ensure_host_dirs = lambda paths: calls.append("host_dirs")  # type: ignore[method-assign]
    manager._seed_bootstrap = lambda *args, **kwargs: calls.append("seed")  # type: ignore[method-assign]
    manager.inspect = lambda worker_id: FakeSandbox()  # type: ignore[method-assign]

    sandbox = manager.ensure_ready({"worker_id": "wrk_test"}, "codex-cli")

    assert sandbox.container_name == "wpr-test"
    assert calls == ["require", "host_dirs", "seed"]


def test_ensure_ready_builds_container_when_projected_worker_inspect_misses(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    calls: list[str] = []

    inspect_calls = 0

    def fake_inspect(worker_id: str):
        nonlocal inspect_calls
        inspect_calls += 1
        if inspect_calls == 1:
            return None
        return SandboxInfo(
            container_name="wpr-wrk-test",
            container_id="container123",
            state="running",
            workspace_dir=str(tmp_path / "workspace"),
            home_dir=str(tmp_path / "home"),
            pid=1234,
            image="img",
        )

    manager._require_docker = lambda: calls.append("require")  # type: ignore[method-assign]
    manager._ensure_host_dirs = lambda paths: calls.append("host_dirs")  # type: ignore[method-assign]
    manager._seed_bootstrap = lambda *args, **kwargs: calls.append("seed")  # type: ignore[method-assign]
    manager.inspect = fake_inspect  # type: ignore[method-assign]
    manager._ensure_image = lambda: calls.append("image")  # type: ignore[method-assign]
    manager._create_container = lambda container_name, paths: calls.append(f"create:{container_name}")  # type: ignore[method-assign]
    manager._ensure_container_writable_paths = lambda *args, **kwargs: calls.append("writable")  # type: ignore[method-assign]
    manager._harden_secret_runtime_files = lambda container_name: calls.append("harden")  # type: ignore[method-assign]
    manager._set_plain_background = lambda container_name: calls.append("background")  # type: ignore[method-assign]
    manager._prime_idle_desktop = lambda container_name: calls.append("prime")  # type: ignore[method-assign]

    sandbox = manager.ensure_ready({"worker_id": "wrk_test", "state": "running", "state_dir": str(tmp_path / "state")}, "openclaw")

    assert sandbox.container_name == "wpr-wrk-test"
    assert sandbox.state == "running"
    assert calls == ["require", "host_dirs", "seed", "image", "create:wpr-wrk-test", "writable", "harden", "background", "prime"]


def test_ensure_image_uses_short_probe_and_caches_success(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    calls: list[tuple[list[str], float | None]] = []

    def fake_docker(args: list[str], *, check: bool = True, capture_output: bool = False, timeout_sec=None):
        calls.append((args, timeout_sec))
        return subprocess.CompletedProcess(["docker", *args], returncode=0, stdout="", stderr="")

    manager._docker = fake_docker  # type: ignore[method-assign]

    manager._ensure_image()
    manager._ensure_image()

    assert calls == [(["image", "inspect", manager.image], manager.image_inspect_timeout_sec)]


def test_ensure_image_uses_dedicated_long_build_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("WPR_DOCKER_IMAGE_BUILD_TIMEOUT_SEC", "777")
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    calls: list[tuple[list[str], float | None]] = []

    def fake_docker(args: list[str], *, check: bool = True, capture_output: bool = False, timeout_sec=None):
        calls.append((args, timeout_sec))
        if args[:2] == ["image", "inspect"]:
            return subprocess.CompletedProcess(["docker", *args], returncode=1, stdout="", stderr="")
        return subprocess.CompletedProcess(["docker", *args], returncode=0, stdout="", stderr="")

    manager._docker = fake_docker  # type: ignore[method-assign]

    manager._ensure_image()

    assert calls[-1][0][:2] == ["build", "-t"]
    assert calls[-1][1] == 777


def test_ensure_image_includes_document_delivery_toolchain(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))

    def fake_docker(args: list[str], *, check: bool = True, capture_output: bool = False, timeout_sec=None):
        if args[:2] == ["image", "inspect"]:
            return subprocess.CompletedProcess(["docker", *args], returncode=1, stdout="", stderr="")
        return subprocess.CompletedProcess(["docker", *args], returncode=0, stdout="", stderr="")

    manager._docker = fake_docker  # type: ignore[method-assign]

    manager._ensure_image()

    dockerfile = (manager.build_root / "Dockerfile").read_text()
    assert "libreoffice-writer" in dockerfile
    assert "libreoffice-impress" in dockerfile
    assert "pandoc" in dockerfile
    assert "poppler-utils" in dockerfile
    assert "python-docx" in dockerfile
    assert "python-pptx" in dockerfile
    assert "reportlab" in dockerfile
    assert "requests" in dockerfile
    assert "PyMuPDF" in dockerfile
    assert "/usr/bin/locale-check" in dockerfile


def test_ensure_image_defaults_to_no_forced_ai_worker_browser_extensions(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))

    def fake_docker(args: list[str], *, check: bool = True, capture_output: bool = False, timeout_sec=None):
        if args[:2] == ["image", "inspect"]:
            return subprocess.CompletedProcess(["docker", *args], returncode=1, stdout="", stderr="")
        return subprocess.CompletedProcess(["docker", *args], returncode=0, stdout="", stderr="")

    manager._docker = fake_docker  # type: ignore[method-assign]

    manager._ensure_image()

    dockerfile = (manager.build_root / "Dockerfile").read_text()
    assert manager.image.endswith(":phase1-node22-docs7")
    assert "@openai/codex@0.142.0" in dockerfile
    assert "@anthropic-ai/claude-code@2.1.186" in dockerfile
    assert "--cache /tmp/glasshive-npm-cache" in dockerfile
    assert "rm -rf /tmp/glasshive-npm-cache /root/.npm /home/seluser/.npm" in dockerfile
    assert "/etc/chromium/policies/managed/glasshive-ai-worker-extensions.json" in dockerfile
    assert "/etc/opt/chrome/policies/managed/glasshive-ai-worker-extensions.json" in dockerfile
    assert "ExtensionInstallForcelist" in dockerfile
    assert "ExtensionInstallForcelist\":[]" in dockerfile
    assert "fcoeoabgfenejglbffodgkkbkcdhcgfn;https://clients2.google.com/service/update2/crx" not in dockerfile
    assert "hehggadaopoacecdllhhajmbjkdcmajg;https://clients2.google.com/service/update2/crx" not in dockerfile
    assert "glasshive-browser-extension-check" in dockerfile
    assert "glasshive-browser-native-host-bootstrap" in dockerfile


def test_ensure_image_can_opt_in_to_ai_worker_browser_extension_policy(monkeypatch, tmp_path):
    monkeypatch.setenv("WPR_AI_WORKER_BROWSER_EXTENSIONS", "all")
    manager = DockerSandboxManager(base_dir=str(tmp_path))

    def fake_docker(args: list[str], *, check: bool = True, capture_output: bool = False, timeout_sec=None):
        if args[:2] == ["image", "inspect"]:
            return subprocess.CompletedProcess(["docker", *args], returncode=1, stdout="", stderr="")
        return subprocess.CompletedProcess(["docker", *args], returncode=0, stdout="", stderr="")

    manager._docker = fake_docker  # type: ignore[method-assign]

    manager._ensure_image()

    dockerfile = (manager.build_root / "Dockerfile").read_text()
    assert "fcoeoabgfenejglbffodgkkbkcdhcgfn;https://clients2.google.com/service/update2/crx" in dockerfile
    assert "hehggadaopoacecdllhhajmbjkdcmajg;https://clients2.google.com/service/update2/crx" in dockerfile
    assert "glasshive-browser-extension-check" in dockerfile
    assert "glasshive-browser-native-host-bootstrap" in dockerfile
    assert "com.anthropic.claude_code_browser_extension" in dockerfile
    assert "com.openai.codexextension" in dockerfile


def test_ai_worker_browser_native_host_scripts_default_to_disabled(tmp_path):
    bootstrap_script = _ai_worker_browser_native_host_bootstrap_script()
    check_script = _ai_worker_browser_extension_check_script()
    for script in (bootstrap_script, check_script):
        syntax = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
        assert syntax.returncode == 0, syntax.stderr

    result = subprocess.run(
        ["bash", "-c", bootstrap_script],
        env={
            **os.environ,
            "HOME": str(tmp_path / "home"),
        },
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert "claude-code native-host disabled" in result.stdout
    assert "codex native-host disabled" in result.stdout
    assert not (tmp_path / "home" / ".config" / "chromium" / "NativeMessagingHosts").exists()


def test_ai_worker_browser_native_host_scripts_remove_disabled_managed_extension_state(tmp_path):
    bootstrap_script = _ai_worker_browser_native_host_bootstrap_script()
    home = tmp_path / "home"
    for extension_id in ("fcoeoabgfenejglbffodgkkbkcdhcgfn", "hehggadaopoacecdllhhajmbjkdcmajg"):
        stale = home / ".config" / "chromium" / "Default" / "Extensions" / extension_id / "1.0_0"
        stale.mkdir(parents=True)
        (stale / "manifest.json").write_text("{}\n")
    native_dir = home / ".config" / "chromium" / "NativeMessagingHosts"
    native_dir.mkdir(parents=True)
    for host in ("com.anthropic.claude_code_browser_extension", "com.openai.codexextension"):
        (native_dir / f"{host}.json").write_text("{}\n")

    result = subprocess.run(
        ["bash", "-c", bootstrap_script],
        env={
            **os.environ,
            "HOME": str(home),
        },
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert not (home / ".config" / "chromium" / "Default" / "Extensions" / "fcoeoabgfenejglbffodgkkbkcdhcgfn").exists()
    assert not (home / ".config" / "chromium" / "Default" / "Extensions" / "hehggadaopoacecdllhhajmbjkdcmajg").exists()
    assert not (native_dir / "com.anthropic.claude_code_browser_extension.json").exists()
    assert not (native_dir / "com.openai.codexextension.json").exists()


def test_ai_worker_browser_native_host_scripts_are_valid_and_install_claude_manifest(monkeypatch, tmp_path):
    monkeypatch.setenv("WPR_AI_WORKER_BROWSER_EXTENSIONS", "claude,codex")
    bootstrap_script = _ai_worker_browser_native_host_bootstrap_script()
    check_script = _ai_worker_browser_extension_check_script()
    for script in (bootstrap_script, check_script):
        syntax = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
        assert syntax.returncode == 0, syntax.stderr
        script_path = tmp_path / "roundtrip-script"
        dockerfile_lines = " ".join(shlex.quote(line) for line in script.splitlines())
        roundtrip = subprocess.run(
            ["bash", "-lc", f"printf '%s\\n' {dockerfile_lines} > {shlex.quote(str(script_path))}; bash -n {shlex.quote(str(script_path))}"],
            text=True,
            capture_output=True,
        )
        assert roundtrip.returncode == 0, roundtrip.stderr

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_claude = fake_bin / "claude"
    fake_claude.write_text("#!/usr/bin/env sh\nexit 0\n")
    fake_claude.chmod(0o755)

    result = subprocess.run(
        ["bash", "-c", bootstrap_script],
        env={
            **os.environ,
            "HOME": str(tmp_path / "home"),
            "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
        },
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert "claude-code native-host installed" in result.stdout

    wrapper = tmp_path / "home" / ".claude" / "chrome" / "chrome-native-host"
    assert wrapper.exists()
    assert os.access(wrapper, os.X_OK)
    assert str(fake_claude) in wrapper.read_text()
    assert "--chrome-native-host" in wrapper.read_text()

    for browser in ("chromium", "google-chrome"):
        manifest = (
            tmp_path
            / "home"
            / ".config"
            / browser
            / "NativeMessagingHosts"
            / "com.anthropic.claude_code_browser_extension.json"
        )
        data = json.loads(manifest.read_text())
        assert data["name"] == "com.anthropic.claude_code_browser_extension"
        assert data["path"] == str(wrapper)
        assert data["type"] == "stdio"
        assert data["allowed_origins"] == ["chrome-extension://fcoeoabgfenejglbffodgkkbkcdhcgfn/"]

    codex_manifest = (
        tmp_path
        / "home"
        / ".config"
        / "chromium"
        / "NativeMessagingHosts"
        / "com.openai.codexextension.json"
    )
    assert not codex_manifest.exists()
    assert "codex native-host pending: extension-host bundle not found" in result.stdout


def test_desktop_env_forwards_codex_native_host_provisioning(monkeypatch, tmp_path):
    monkeypatch.setenv("WPR_CODEX_CHROME_PLUGIN_ROOT", "/opt/codex-chrome")
    monkeypatch.setenv("CODEX_CHROME_PLUGIN_ROOT", "/workspace/.wpr-home/.codex/chrome")
    monkeypatch.setenv("WPR_CODEX_NODE_REPL_PATH", "/opt/codex-node-repl")
    monkeypatch.setenv("CODEX_NODE_REPL_PATH", "/workspace/.wpr-home/.codex/node_repl")

    env = DockerSandboxManager(base_dir=str(tmp_path))._desktop_env()

    assert env["WPR_CODEX_CHROME_PLUGIN_ROOT"] == "/opt/codex-chrome"
    assert env["CODEX_CHROME_PLUGIN_ROOT"] == "/workspace/.wpr-home/.codex/chrome"
    assert env["WPR_CODEX_NODE_REPL_PATH"] == "/opt/codex-node-repl"
    assert env["CODEX_NODE_REPL_PATH"] == "/workspace/.wpr-home/.codex/node_repl"


def test_ai_worker_browser_native_host_bootstrap_installs_codex_manifest_when_bundle_exists(monkeypatch, tmp_path):
    monkeypatch.setenv("WPR_AI_WORKER_BROWSER_EXTENSIONS", "claude,codex")
    bootstrap_script = _ai_worker_browser_native_host_bootstrap_script()
    syntax = subprocess.run(["bash", "-n"], input=bootstrap_script, text=True, capture_output=True)
    assert syntax.returncode == 0, syntax.stderr

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name in ("claude", "codex", "node"):
        fake = fake_bin / name
        fake.write_text("#!/usr/bin/env sh\nexit 0\n")
        fake.chmod(0o755)
    fake_node_repl = tmp_path / "node_repl"
    fake_node_repl.write_text("#!/usr/bin/env sh\nexit 0\n")
    fake_node_repl.chmod(0o755)

    plugin_root = tmp_path / "home" / ".codex" / "plugins" / "cache" / "openai-bundled" / "chrome" / "26.616.71553"
    for arch in ("arm64", "x64"):
        extension_host = plugin_root / "extension-host" / "linux" / arch / "extension-host"
        extension_host.parent.mkdir(parents=True, exist_ok=True)
        extension_host.write_text("#!/usr/bin/env sh\nexit 0\n")
        extension_host.chmod(0o755)
    (plugin_root / "scripts").mkdir(parents=True, exist_ok=True)
    (plugin_root / "scripts" / "browser-client.mjs").write_text("export {};\n")

    result = subprocess.run(
        ["bash", "-c", bootstrap_script],
        env={
            **os.environ,
            "HOME": str(tmp_path / "home"),
            "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "WPR_CODEX_NODE_REPL_PATH": str(fake_node_repl),
        },
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert "codex native-host installed" in result.stdout

    manifest = (
        tmp_path
        / "home"
        / ".config"
        / "chromium"
        / "NativeMessagingHosts"
        / "com.openai.codexextension.json"
    )
    data = json.loads(manifest.read_text())
    assert data["name"] == "com.openai.codexextension"
    assert data["type"] == "stdio"
    assert data["allowed_origins"] == ["chrome-extension://hehggadaopoacecdllhhajmbjkdcmajg/"]
    assert data["path"].startswith(str(plugin_root / "extension-host" / "linux"))

    config = json.loads((Path(data["path"]).with_name("extension-host-config.json")).read_text())
    assert config["schemaVersion"] == 1
    assert config["browserClientPath"] == str(plugin_root / "scripts" / "browser-client.mjs")
    assert config["codexCliPath"] == str(fake_bin / "codex")
    assert config["nodePath"] == str(fake_bin / "node")
    assert config["nodeReplPath"] == str(fake_node_repl)
    assert config["extensionId"] == "hehggadaopoacecdllhhajmbjkdcmajg"


def test_start_screen_session_prepares_runtime_dir_and_detaches(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    calls: list[tuple[str | None, list[str], bool, dict | None]] = []

    class FakeSandbox:
        container_name = "wpr-test"

    manager.ensure_ready = lambda *args, **kwargs: FakeSandbox()  # type: ignore[method-assign]
    manager.stop_screen_session = lambda *args, **kwargs: None  # type: ignore[method-assign]

    def fake_docker_exec(container_name, command, *, env=None, cwd=None, detach=False, fire_and_forget=False, user=None):
        calls.append((user, command, detach, env))
        return subprocess.CompletedProcess(["docker"], returncode=0, stdout="", stderr="")

    manager._docker_exec = fake_docker_exec  # type: ignore[method-assign]

    manager.start_screen_session(
        "wrk_test",
        "codex-cli",
        "job-run_123456",
        ["echo", "ok"],
        env={"OPENAI_API_KEY": "secret", "PATH": "/usr/bin:/bin"},
    )

    assert calls[0][0] == "root"
    assert "mkdir -p /run/screen" in calls[0][1][-1]
    assert calls[1][1][:2] == ["screen", "-DmS"]
    assert calls[1][2] is True
    assert calls[1][3]["PATH"] == "/usr/bin:/bin"
    assert calls[1][3]["OPENAI_API_KEY"] == "secret"


def test_start_screen_session_revalidates_projected_worker_without_container_evidence(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    calls: list[tuple[str, list[str], bool, bool]] = []

    sandbox = SandboxInfo(
        container_name="wpr-wrk-test",
        container_id="container123",
        state="running",
        workspace_dir=str(tmp_path / "workspace"),
        home_dir=str(tmp_path / "home"),
        pid=1234,
        image="img",
    )
    manager.ensure_ready = lambda *args, **kwargs: calls.append(("ensure", [], False, False)) or sandbox  # type: ignore[method-assign]
    manager.stop_screen_session = lambda *args, **kwargs: None  # type: ignore[method-assign]

    def fake_docker_exec(container_name, command, *, env=None, cwd=None, detach=False, fire_and_forget=False, user=None):
        calls.append((container_name, command, detach, fire_and_forget))
        return subprocess.CompletedProcess(["docker"], returncode=0, stdout="", stderr="")

    manager._docker_exec = fake_docker_exec  # type: ignore[method-assign]

    manager.start_screen_session(
        "wrk_test",
        "codex-cli",
        "job-run_fast",
        ["echo", "ok"],
        worker={"worker_id": "wrk_test", "state": "running", "state_dir": str(tmp_path / "state")},
    )

    assert calls[0] == ("ensure", [], False, False)
    assert calls[1][0] == "wpr-wrk-test"
    assert "mkdir -p /run/screen" in calls[1][1][-1]
    assert calls[2] == ("wpr-wrk-test", ["screen", "-DmS", "job-run_fast", "echo", "ok"], True, False)


def test_stop_screen_session_targets_all_exact_duplicate_sockets(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    calls: list[list[str]] = []

    class FakeSandbox:
        container_name = "wpr-test"

    manager.ensure_ready = lambda *args, **kwargs: FakeSandbox()  # type: ignore[method-assign]

    def fake_docker_exec(container_name, command, *, env=None, cwd=None, detach=False, fire_and_forget=False, user=None):
        calls.append(command)
        return subprocess.CompletedProcess(["docker"], returncode=0, stdout="", stderr="")

    manager._docker_exec = fake_docker_exec  # type: ignore[method-assign]

    manager.stop_screen_session(
        "wrk_test",
        "openclaw",
        "openclaw-gateway",
        worker={"worker_id": "wrk_test", "state": "running", "state_dir": str(tmp_path / "state")},
    )

    script = calls[0][2]
    assert calls[0][-1] == "openclaw-gateway"
    assert "sockets=$(screen -ls | awk" in script
    assert "if (name == target) print socket" in script
    assert 'screen -S "$socket" -X quit' in script


def test_terminate_run_processes_targets_run_env_and_descendants(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    calls: list[list[str]] = []

    class FakeSandbox:
        container_name = "wpr-test"

    manager.ensure_ready = lambda *args, **kwargs: FakeSandbox()  # type: ignore[method-assign]

    def fake_docker_exec(container_name, command, *, env=None, cwd=None, detach=False, fire_and_forget=False, user=None):
        calls.append(command)
        return subprocess.CompletedProcess(["docker"], returncode=0, stdout="", stderr="")

    manager._docker_exec = fake_docker_exec  # type: ignore[method-assign]

    manager.terminate_run_processes(
        "wrk_test",
        "openclaw",
        "run_123",
        worker={"worker_id": "wrk_test", "state": "running", "state_dir": str(tmp_path / "state")},
    )

    script = calls[0][-1]
    assert "GLASSHIVE_ACTIVE_RUN_ID=$run_id" in script
    assert "descendants()" in script
    assert "/workspace/.wpr-home/.glasshive-runs/run_123" in script


def test_ensure_ready_repairs_bind_mount_ownership_before_prime(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    calls: list[str] = []

    class FakeSandbox:
        def __init__(self, state: str):
            self.container_name = "wpr-test"
            self.state = state
            self.container_id = "cid"
            self.workspace_dir = str(tmp_path / "workspace")
            self.home_dir = str(tmp_path / "home")
            self.pid = 1234
            self.image = "img"
            self.novnc_port = 57900
            self.selenium_port = 57901
            self.openclaw_port = 57902

    sandbox_states = [None, FakeSandbox("running")]

    manager._require_docker = lambda: None  # type: ignore[method-assign]
    manager._ensure_image = lambda: None  # type: ignore[method-assign]
    manager._ensure_host_dirs = lambda paths: None  # type: ignore[method-assign]
    manager._seed_bootstrap = lambda *args, **kwargs: None  # type: ignore[method-assign]
    manager._create_container = lambda *args, **kwargs: calls.append("create")  # type: ignore[method-assign]
    manager._set_plain_background = lambda container_name: calls.append("background")  # type: ignore[method-assign]
    manager._prime_idle_desktop = lambda container_name: calls.append("prime")  # type: ignore[method-assign]
    manager.inspect = lambda worker_id: sandbox_states.pop(0)  # type: ignore[method-assign]

    def fake_docker_exec(container_name, command, *, env=None, cwd=None, detach=False, fire_and_forget=False, user=None):
        calls.append(f"{user}:{command[-1]}")
        return subprocess.CompletedProcess(["docker"], returncode=0, stdout="", stderr="")

    manager._docker_exec = fake_docker_exec  # type: ignore[method-assign]

    manager.ensure_ready({"worker_id": "wrk_test"}, "codex-cli")

    assert calls[0] == "create"
    assert calls[1].startswith("root:")
    assert "setfacl -R -m u:seluser:rwX" in calls[1]
    assert (
        "find /workspace/project /workspace/.wpr-home /workspace/.wpr-home/tmp "
        "/workspace/.wpr-home/.cache /workspace/.wpr-home/.config -type d -exec setfacl"
    ) in calls[1]
    assert calls[2].startswith("root:set -e; for file in /workspace/.wpr-home/.glasshive/secret-runtime.env")
    assert calls[3:] == ["background", "prime"]


def test_ensure_container_writable_paths_repairs_specific_run_dir(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    calls: list[tuple[str | None, list[str]]] = []

    class FakeSandbox:
        container_name = "wpr-test"

    manager.inspect = lambda worker_id: FakeSandbox()  # type: ignore[method-assign]

    def fake_docker_exec(container_name, command, *, env=None, cwd=None, detach=False, fire_and_forget=False, user=None):
        calls.append((user, command))
        return subprocess.CompletedProcess(["docker"], returncode=0, stdout="", stderr="")

    manager._docker_exec = fake_docker_exec  # type: ignore[method-assign]

    manager.ensure_container_writable_paths(
        "wrk_test",
        "codex-cli",
        ["/workspace/.wpr-home/.glasshive-runs/run_123"],
    )

    assert calls == [
        (
            "root",
            [
                "bash",
                "-c",
                "set -e; mkdir -p /workspace/.wpr-home/.glasshive-runs/run_123; "
                "if command -v setfacl >/dev/null 2>&1 "
                f"&& setfacl -R -m u:seluser:rwX,u:{os.getuid()}:rwX /workspace/.wpr-home/.glasshive-runs/run_123 2>/dev/null; then "
                f"find /workspace/.wpr-home/.glasshive-runs/run_123 -type d -exec setfacl -m d:u:seluser:rwX,d:u:{os.getuid()}:rwX {{}} + 2>/dev/null || true; "
                "else chmod -R a+rwX /workspace/.wpr-home/.glasshive-runs/run_123 2>/dev/null || true; fi",
            ],
        )
    ]


def test_create_container_applies_default_resource_caps(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    commands: list[list[str]] = []

    def fake_docker(args: list[str], *, check: bool = True, capture_output: bool = False, **kwargs):
        commands.append(args)
        return subprocess.CompletedProcess(["docker", *args], returncode=0, stdout="cid", stderr="")

    manager._docker = fake_docker  # type: ignore[method-assign]

    manager._create_container(
        "wpr-test",
        {
            "workspace_dir": tmp_path / "workspace",
            "home_dir": tmp_path / "home",
        },
    )

    command = commands[0]
    assert command[command.index("--shm-size") + 1] == "1g"
    assert command[command.index("--memory") + 1] == "3g"
    assert command[command.index("--memory-swap") + 1] == "3g"
    assert command[command.index("--cpus") + 1] == "2"
    assert command[command.index("--pids-limit") + 1] == "4096"
    assert command[-1] == manager.image


def test_ensure_ready_primes_idle_desktop_when_container_is_new(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    calls: list[str] = []

    class FakeSandbox:
        def __init__(self, state: str):
            self.container_name = "wpr-test"
            self.state = state
            self.container_id = "cid"
            self.workspace_dir = str(tmp_path / "workspace")
            self.home_dir = str(tmp_path / "home")
            self.pid = 1234
            self.image = "img"
            self.novnc_port = 57900
            self.selenium_port = 57901
            self.openclaw_port = 57902

    sandbox_states = [None, FakeSandbox("running")]

    manager._require_docker = lambda: None  # type: ignore[method-assign]
    manager._ensure_image = lambda: None  # type: ignore[method-assign]
    manager._ensure_host_dirs = lambda paths: None  # type: ignore[method-assign]
    manager._seed_bootstrap = lambda *args, **kwargs: None  # type: ignore[method-assign]
    manager._create_container = lambda *args, **kwargs: calls.append("create")  # type: ignore[method-assign]
    manager._ensure_container_writable_paths = lambda *args, **kwargs: calls.append("writable")  # type: ignore[method-assign]
    manager._set_plain_background = lambda container_name: calls.append("background")  # type: ignore[method-assign]
    manager._prime_idle_desktop = lambda container_name: calls.append("prime")  # type: ignore[method-assign]
    manager.inspect = lambda worker_id: sandbox_states.pop(0)  # type: ignore[method-assign]

    manager.ensure_ready({"worker_id": "wrk_test"}, "codex-cli")
    assert calls == ["create", "writable", "background", "prime"]
