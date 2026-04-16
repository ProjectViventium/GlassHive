from __future__ import annotations

import json
import subprocess

from workers_projects_runtime.docker_sandbox import DockerSandboxManager


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

    def fake_docker(args: list[str], *, check: bool = True, capture_output: bool = False):
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


def test_seed_bootstrap_writes_project_scope_files(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    home_dir = tmp_path / "home"
    workspace_dir = tmp_path / "workspace"
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
                        "transport": "http",
                        "url": "http://127.0.0.1:8767/mcp",
                    }
                },
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
                    }
                ],
            }
        ),
    }

    manager._seed_bootstrap(home_dir, workspace_dir, "claude-code", worker)

    assert (workspace_dir / "CLAUDE.md").read_text().strip() == "Use operator checkpoints before risky actions."
    assert (workspace_dir / "AGENTS.md").read_text().strip() == "Use operator checkpoints before risky actions."
    assert json.loads((workspace_dir / ".mcp.json").read_text())["glass-hive"]["url"] == "http://127.0.0.1:8767/mcp"
    assert json.loads((workspace_dir / ".claude" / "settings.local.json").read_text())["permissions"]["allow"] == ["Bash(ls *)"]
    assert (workspace_dir / "notes" / "bootstrap.txt").read_text() == "Bootstrapped"
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
    manager._set_plain_background = lambda container_name: calls.append("background")  # type: ignore[method-assign]
    manager._prime_idle_desktop = lambda container_name: calls.append("prime")  # type: ignore[method-assign]
    manager.inspect = lambda worker_id: sandbox_states.pop(0)  # type: ignore[method-assign]

    manager.ensure_ready({"worker_id": "wrk_test"}, "codex-cli")
    assert calls == ["create", "background", "prime"]
