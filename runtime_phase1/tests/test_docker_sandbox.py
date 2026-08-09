from __future__ import annotations

import hashlib
import json
import os
import shlex
import stat
import subprocess
import time
from pathlib import Path

import pytest

from workers_projects_runtime.docker_sandbox import (
    AI_WORKER_APT_SNAPSHOT,
    AI_WORKER_PYTHON_LOCK_PATH,
    DockerSandboxManager,
    SandboxInfo,
    VNC_PASSWORD_ALPHABET,
    _ai_worker_browser_extension_check_script,
    _ai_worker_browser_native_host_bootstrap_script,
    _safe_docker_exec_env,
)
from workers_projects_runtime.bootstrap import (
    GLASSHIVE_CRITICAL_OPERATING_INSTRUCTIONS,
    GLASSHIVE_NATIVE_CAPABILITY_INVENTORY,
    GLASSHIVE_SAFETY_CHECKPOINT_RULE,
)
from workers_projects_runtime.openclaw_release import (
    OPENCLAW_RUNTIME_FAST_URI_VERSION,
    OPENCLAW_RUNTIME_LOCK_SHA256,
    OPENCLAW_RUNTIME_VERSION,
)


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


def test_safe_docker_exec_env_preserves_bound_provider_home_selectors():
    env = _safe_docker_exec_env(
        {
            "CODEX_HOME": "/workspace/.provider-account/codex",
            "CLAUDE_CONFIG_DIR": "/workspace/.provider-account/claude",
            "UNRELATED_SECRET": "must-not-pass",
        }
    )

    assert env["CODEX_HOME"] == "/workspace/.provider-account/codex"
    assert env["CLAUDE_CONFIG_DIR"] == "/workspace/.provider-account/claude"
    assert "UNRELATED_SECRET" not in env


def test_safe_docker_exec_env_preserves_bedrock_run_credentials_only():
    env = _safe_docker_exec_env(
        {
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "AWS_ACCESS_KEY_ID": "AKIAEXAMPLEONLY0000",
            "AWS_SECRET_ACCESS_KEY": "synthetic-secret-not-real",
            "AWS_REGION": "us-east-1",
            "AWS_EC2_METADATA_DISABLED": "true",
            "API_TIMEOUT_MS": "240000",
            "UNRELATED_SECRET": "must-not-pass",
        }
    )

    assert env["CLAUDE_CODE_USE_BEDROCK"] == "1"
    assert env["AWS_ACCESS_KEY_ID"] == "AKIAEXAMPLEONLY0000"
    assert env["AWS_SECRET_ACCESS_KEY"] == "synthetic-secret-not-real"
    assert env["AWS_REGION"] == "us-east-1"
    assert env["AWS_EC2_METADATA_DISABLED"] == "true"
    assert env["API_TIMEOUT_MS"] == "240000"
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
    assert GLASSHIVE_NATIVE_CAPABILITY_INVENTORY in agents_text
    assert "WebDriver/Selenium endpoint" in agents_text
    assert "Use the visible workstation surface" in agents_text
    assert "Less is more" in agents_text
    assert "Do not force a download" in agents_text
    assert "@AGENTS.md" in (workspace_dir / "CLAUDE.md").read_text()


def test_create_container_adds_host_gateway_alias_for_broker_reachability(tmp_path, monkeypatch):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    captured: list[list[str]] = []

    def fake_docker(args: list[str], **kwargs):
        captured.append(args)
        if args[:2] == ["network", "inspect"]:
            return subprocess.CompletedProcess(["docker", *args], returncode=1, stdout="", stderr="not found")
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
    assert command[command.index("--network") + 1] == manager._network_name_for_container("wpr-test")
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


def test_create_container_mounts_only_a_trusted_bound_provider_account_home(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    account_home = tmp_path / "provider-accounts" / "acct-safe"
    account_home.mkdir(parents=True)
    captured: list[list[str]] = []

    def fake_docker(args: list[str], **kwargs):
        captured.append(args)
        if args[:2] == ["network", "inspect"]:
            return subprocess.CompletedProcess(["docker", *args], 1, "", "not found")
        return subprocess.CompletedProcess(["docker", *args], 0, "cid", "")

    manager._docker = fake_docker  # type: ignore[method-assign]
    manager._create_container(
        "wpr-test",
        {
            "workspace_dir": tmp_path / "workspace",
            "home_dir": tmp_path / "home",
        },
        worker={
            "_glasshive_provider_account_bound": True,
            "_glasshive_provider_account_mount_host": str(account_home),
            "_glasshive_provider_account_mount_target": "/workspace/.provider-account",
        },
    )

    command = captured[-1]
    assert (
        f"{account_home.resolve()}:/workspace/.provider-account"
        in command
    )


def test_bound_provider_account_mount_grants_and_verifies_only_worker_user_access(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    account_home = tmp_path / "provider-accounts" / "acct-safe"
    (account_home / "codex").mkdir(parents=True)
    calls: list[tuple[str | None, list[str]]] = []

    def fake_docker_exec(
        container_name,
        command,
        *,
        env=None,
        cwd=None,
        detach=False,
        fire_and_forget=False,
        user=None,
    ):
        calls.append((user, command))
        return subprocess.CompletedProcess(["docker"], 0, "", "")

    manager._docker_exec = fake_docker_exec  # type: ignore[method-assign]
    worker = {
        "_glasshive_provider_account_bound": True,
        "_glasshive_provider_account_mount_host": str(account_home),
        "_glasshive_provider_account_mount_target": "/workspace/.provider-account",
        "_glasshive_provider_account_env": {
            "CODEX_HOME": "/workspace/.provider-account/codex"
        },
    }

    manager._grant_provider_account_access("wpr-test", worker)  # type: ignore[attr-defined]

    assert len(calls) == 2
    assert calls[0][0] == "root"
    grant_script = calls[0][1][-1]
    assert "command -v setfacl" in grant_script
    assert "setfacl -R -m u:seluser:rwX" in grant_script
    assert "chmod" not in grant_script
    assert calls[1][0] == "seluser"
    verify_script = calls[1][1][-1]
    assert "test -r /workspace/.provider-account/codex" in verify_script
    assert "test -w /workspace/.provider-account/codex" in verify_script


def test_provider_account_mount_requires_private_binder_marker(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    account_home = tmp_path / "provider-accounts" / "acct-untrusted"
    account_home.mkdir(parents=True)

    with pytest.raises(RuntimeError, match="validated by the GlassHive control plane"):
        manager._provider_account_mount(  # type: ignore[attr-defined]
            {
                "_glasshive_provider_account_mount_host": str(account_home),
                "_glasshive_provider_account_mount_target": "/workspace/.provider-account",
            }
        )


def test_sandbox_network_isolation_detects_default_bridge_and_accepts_shared_icc_disabled_network(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    common = {
        "container_name": "wpr-wrk-test",
        "container_id": "cid",
        "state": "ready",
        "workspace_dir": str(tmp_path / "workspace"),
        "home_dir": str(tmp_path / "home"),
        "pid": 123,
        "image": "img",
    }

    assert manager._sandbox_needs_network_recreate(  # type: ignore[attr-defined]
        "wrk_test", SandboxInfo(**common, networks=("bridge",))
    ) is True
    assert manager._sandbox_needs_network_recreate(  # type: ignore[attr-defined]
        "wrk_test",
        SandboxInfo(
            **common,
            networks=(manager._network_name_for_container("wpr-wrk-test"),),
        ),
    ) is False
    assert manager._network_name_for_container("wpr-one") == manager._network_name_for_container("wpr-two")


def test_stale_provider_account_mount_is_removed_before_reuse(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    account_home = tmp_path / "provider-accounts" / "acct-safe"
    account_home.mkdir(parents=True)
    commands: list[list[str]] = []

    def fake_docker(args: list[str], *, check: bool = True, capture_output: bool = False, **kwargs):
        commands.append(list(args))
        if args[:2] == ["ps", "-aq"]:
            return subprocess.CompletedProcess(["docker", *args], 0, stdout="cid-stale\n", stderr="")
        if args and args[0] == "inspect":
            return subprocess.CompletedProcess(
                ["docker", *args],
                0,
                stdout=json.dumps(
                    [
                        {
                            "Id": "cid-stale",
                            "Name": "/wpr-stale-worker",
                            "Mounts": [
                                {
                                    "Source": str(account_home.resolve()),
                                    "Destination": "/workspace/.provider-account",
                                }
                            ],
                        }
                    ]
                ),
                stderr="",
            )
        return subprocess.CompletedProcess(["docker", *args], 0, stdout="", stderr="")

    manager._docker = fake_docker  # type: ignore[method-assign]

    removed = manager.terminate_containers_mounting_provider_account(account_home)

    assert removed == ["wpr-stale-worker"]
    assert ["rm", "-f", "cid-stale"] in commands
    assert any(command[:2] == ["network", "rm"] for command in commands)


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
    view_url = str(details["view_url"])
    assert view_url.startswith("http://127.0.0.1:57900/?")
    assert "autoconnect=1" in view_url
    assert "password=" in view_url
    assert details["view_health"] == {"healthy": True, "repaired": True, "reason": "ok"}
    assert calls
    repair_script = str(calls[0][1])
    assert "TMPDIR=/tmp" in repair_script
    assert manager._browser_tmp_dir() not in repair_script


def test_container_uses_unique_required_desktop_and_grid_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    commands: list[list[str]] = []
    secret_environments: list[dict[str, str]] = []

    def fake_docker(args: list[str], *, check: bool = True, capture_output: bool = False, **kwargs):
        commands.append(list(args))
        if args and args[0] == "run":
            env_path = Path(args[args.index("--env-file") + 1])
            assert env_path.stat().st_mode & 0o777 == 0o600
            secret_environments.append(
                dict(line.split("=", 1) for line in env_path.read_text(encoding="utf-8").splitlines())
            )
        return subprocess.CompletedProcess(["docker", *args], returncode=0, stdout="container-id", stderr="")

    manager._docker = fake_docker  # type: ignore[method-assign]
    manager._ensure_isolated_network = lambda _name: "wpr-wrk-one-net"  # type: ignore[method-assign]
    paths_one = manager.paths("wrk_one")
    paths_two = manager.paths("wrk_two")
    manager._ensure_host_dirs(paths_one)  # type: ignore[attr-defined]
    manager._ensure_host_dirs(paths_two)  # type: ignore[attr-defined]

    for paths in (paths_one, paths_two):
        for key in ("worker_root", "state_dir", "workspace_dir", "home_dir"):
            assert paths[key].stat().st_mode & 0o777 == 0o700

    manager._create_container("wpr-wrk-one", paths_one, worker={"worker_id": "wrk_one"})  # type: ignore[attr-defined]
    manager._create_container("wpr-wrk-two", paths_two, worker={"worker_id": "wrk_two"})  # type: ignore[attr-defined]

    run_commands = [command for command in commands if command and command[0] == "run"]
    assert len(run_commands) == 2

    def env_values(command: list[str]) -> dict[str, str]:
        result: dict[str, str] = {}
        for index, value in enumerate(command[:-1]):
            if value == "-e" and "=" in command[index + 1]:
                key, raw = command[index + 1].split("=", 1)
                result[key] = raw
        return result

    first = env_values(run_commands[0])
    assert first["SE_VNC_NO_PASSWORD"] == "0"
    assert first["SE_ROUTER_USERNAME"]
    assert first["SE_MASK_SECRETS"] == "true"
    assert "SE_VNC_PASSWORD" not in first
    assert "SE_ROUTER_PASSWORD" not in first
    assert all("SE_VNC_PASSWORD=" not in value for value in run_commands[0])
    assert all("SE_ROUTER_PASSWORD=" not in value for value in run_commands[0])
    assert secret_environments[0]["SE_VNC_PASSWORD"] != secret_environments[1]["SE_VNC_PASSWORD"]
    assert len(secret_environments[0]["SE_VNC_PASSWORD"]) == 8
    assert set(secret_environments[0]["SE_VNC_PASSWORD"]).issubset(set(VNC_PASSWORD_ALPHABET))
    assert len(set(VNC_PASSWORD_ALPHABET)) >= 64
    assert secret_environments[0]["SE_ROUTER_PASSWORD"] != secret_environments[1]["SE_ROUTER_PASSWORD"]
    assert not Path(run_commands[0][run_commands[0].index("--env-file") + 1]).exists()
    assert (paths_one["state_dir"] / "desktop-credentials.json").stat().st_mode & 0o777 == 0o600
    assert (paths_one["home_dir"] / ".vnc").is_dir()
    assert (paths_one["home_dir"] / ".vnc").stat().st_mode & 0o777 == 0o700


def test_local_running_sandbox_readiness_keeps_container_acl_mask(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "local")
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    paths = manager.paths("wrk_local")
    paths["workspace_dir"].mkdir(parents=True)
    paths["home_dir"].mkdir(parents=True)
    for key in ("worker_root", "state_dir", "workspace_dir", "home_dir"):
        paths[key].chmod(0o770)

    sandbox = SandboxInfo(
        container_name="wpr-wrk-local",
        container_id="cid",
        state="running",
        workspace_dir=str(paths["workspace_dir"]),
        home_dir=str(paths["home_dir"]),
        pid=1234,
        image="img",
    )
    manager._require_docker = lambda: None  # type: ignore[method-assign]
    manager._seed_bootstrap = lambda *args, **kwargs: None  # type: ignore[method-assign]
    manager.inspect = lambda worker_id: sandbox  # type: ignore[method-assign]
    manager._sandbox_needs_chromium_userns_recreate = lambda resolved: False  # type: ignore[method-assign]
    manager._sandbox_needs_network_recreate = lambda worker_id, resolved: False  # type: ignore[method-assign]
    manager._sandbox_needs_provider_mount_recreate = lambda worker, resolved: False  # type: ignore[method-assign]
    manager._sandbox_needs_desktop_auth_recreate = lambda worker_id, resolved: False  # type: ignore[method-assign]
    manager._ensure_container_writable_paths = lambda *args: pytest.fail("local running sandbox was unexpectedly repaired")  # type: ignore[method-assign]
    manager._repair_provider_temp_ownership = lambda container_name: None  # type: ignore[method-assign]
    manager._harden_secret_runtime_files = lambda container_name: None  # type: ignore[method-assign]

    resolved = manager.ensure_ready({"worker_id": "wrk_local"}, "codex-cli")

    assert resolved is sandbox
    for key in ("worker_root", "state_dir", "workspace_dir", "home_dir"):
        assert paths[key].stat().st_mode & 0o777 == 0o770
    assert (paths["home_dir"] / ".vnc").stat().st_mode & 0o777 == 0o700


def test_passwordless_desktop_requires_explicit_insecure_local_opt_in(tmp_path, monkeypatch):
    monkeypatch.setenv("WPR_SANDBOX_VNC_NO_PASSWORD", "true")
    with pytest.raises(RuntimeError, match="passwordless desktop"):
        DockerSandboxManager(base_dir=str(tmp_path))

    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "single_user")
    monkeypatch.setenv("WPR_ALLOW_INSECURE_LOCAL_DESKTOP", "true")
    manager = DockerSandboxManager(base_dir=str(tmp_path / "allowed"))
    assert manager.vnc_no_password is True


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
    manager._create_container = lambda container_name, paths, worker=None: created.append(container_name)  # type: ignore[method-assign]
    manager._ensure_container_writable_paths = lambda container_name, paths: None  # type: ignore[method-assign]
    manager._repair_provider_temp_ownership = lambda container_name: None  # type: ignore[method-assign]
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
        if args[:2] == ["network", "rm"]:
            calls.append("network-rm")
            return subprocess.CompletedProcess(["docker", *args], returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected docker call: {args}")

    def fake_create_container(container_name, paths, worker=None):
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
    manager._repair_provider_temp_ownership = lambda container_name: calls.append("provider_tmp")  # type: ignore[method-assign]
    manager._harden_secret_runtime_files = lambda container_name: calls.append("harden")  # type: ignore[method-assign]
    manager._set_plain_background = lambda container_name: calls.append("background")  # type: ignore[method-assign]
    manager._prime_idle_desktop = lambda container_name: calls.append("prime")  # type: ignore[method-assign]

    sandbox = manager.ensure_ready({"worker_id": "wrk_test", "state": "ready", "container_id": "container-old"}, "codex-cli")

    assert sandbox.container_id == "container-new"
    assert sandbox.security_options == ("seccomp=unconfined",)
    assert calls == [
        "require",
        "host_dirs",
            "seed",
            "rm",
            "network-rm",
            "image",
        "create:wpr-wrk-test",
        "writable",
        "provider_tmp",
        "harden",
        "background",
        "prime",
    ]


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
        if args[:2] == ["network", "rm"]:
            calls.append("network-rm")
            return subprocess.CompletedProcess(["docker", *args], returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected docker call: {args}")

    def fake_create_container(container_name, paths, worker=None):
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
    manager._repair_provider_temp_ownership = lambda container_name: calls.append("provider_tmp")  # type: ignore[method-assign]
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


def test_terminate_fails_if_docker_leaves_the_container_running(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    running = json.dumps(
        [
            {
                "Id": "abc123",
                "State": {"Status": "running", "Paused": False, "Pid": 4242},
                "HostConfig": {},
                "NetworkSettings": {"Ports": {}},
            }
        ]
    )

    def fake_docker(args: list[str], *, check: bool = True, capture_output: bool = False, **kwargs):
        _ = check, capture_output, kwargs
        if args[:1] == ["inspect"]:
            return subprocess.CompletedProcess(["docker", *args], 0, running, "")
        if args[:2] == ["rm", "-f"]:
            return subprocess.CompletedProcess(["docker", *args], 1, "", "removal failed")
        raise AssertionError(f"unexpected docker call: {args}")

    manager._docker = fake_docker  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="still running after termination"):
        manager.terminate("wrk_test")


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


def test_docker_exec_keeps_secret_environment_values_out_of_argv(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    observed: dict[str, object] = {}

    def fake_docker(args: list[str], **kwargs):
        observed["args"] = list(args)
        env_path = Path(args[args.index("--env-file") + 1])
        observed["env_path"] = env_path
        observed["env_text"] = env_path.read_text(encoding="utf-8")
        assert env_path.stat().st_mode & 0o777 == 0o600
        return subprocess.CompletedProcess(["docker", *args], returncode=0, stdout="", stderr="")

    manager._docker = fake_docker  # type: ignore[method-assign]
    result = manager._docker_exec(
        "wpr-test",
        ["bash", "-lc", "true"],
        env={"OPENAI_API_KEY": "synthetic-secret-value", "HOME": "/workspace/.wpr-home"},
    )

    assert result.returncode == 0
    assert "synthetic-secret-value" in str(observed["env_text"])
    assert all("synthetic-secret-value" not in value for value in observed["args"])
    assert not Path(observed["env_path"]).exists()


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
    assert "sleep 0.1; docker exec -d -u seluser" in calls[0][2]
    assert "--env-file" in calls[0][2]
    assert "HOME=/workspace/.wpr-home" not in calls[0][2]
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
    manager._repair_provider_temp_ownership = lambda container_name: calls.append("provider_tmp")  # type: ignore[method-assign]
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
    assert calls == ["provider_tmp", "harden", "exec:True:True:bash"]
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
    manager._repair_provider_temp_ownership = lambda container_name: calls.append("provider_tmp")  # type: ignore[method-assign]
    manager._harden_secret_runtime_files = lambda container_name: calls.append("harden")  # type: ignore[method-assign]

    sandbox = manager.ensure_ready({"worker_id": "wrk_test"}, "codex-cli")

    assert sandbox.container_name == "wpr-test"
    assert calls == ["require", "host_dirs", "seed", "provider_tmp", "harden"]


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
    manager._create_container = lambda container_name, paths, worker=None: calls.append(f"create:{container_name}")  # type: ignore[method-assign]
    manager._ensure_container_writable_paths = lambda *args, **kwargs: calls.append("writable")  # type: ignore[method-assign]
    manager._repair_provider_temp_ownership = lambda container_name: calls.append("provider_tmp")  # type: ignore[method-assign]
    manager._harden_secret_runtime_files = lambda container_name: calls.append("harden")  # type: ignore[method-assign]
    manager._set_plain_background = lambda container_name: calls.append("background")  # type: ignore[method-assign]
    manager._prime_idle_desktop = lambda container_name: calls.append("prime")  # type: ignore[method-assign]

    sandbox = manager.ensure_ready({"worker_id": "wrk_test", "state": "running", "state_dir": str(tmp_path / "state")}, "openclaw")

    assert sandbox.container_name == "wpr-wrk-test"
    assert sandbox.state == "running"
    assert calls == [
        "require",
        "host_dirs",
        "seed",
        "image",
        "create:wpr-wrk-test",
        "writable",
        "provider_tmp",
        "harden",
        "background",
        "prime",
    ]


def test_ensure_image_uses_short_probe_and_caches_success(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    calls: list[tuple[list[str], float | None]] = []

    def fake_docker(args: list[str], *, check: bool = True, capture_output: bool = False, timeout_sec=None):
        calls.append((args, timeout_sec))
        payload = [{"Config": {"Labels": manager._expected_image_provenance()}}]
        return subprocess.CompletedProcess(
            ["docker", *args], returncode=0, stdout=json.dumps(payload), stderr=""
        )

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
    requirements_lock = (manager.build_root / "workstation-requirements.lock").read_text()
    assert "libreoffice-writer" in dockerfile
    assert "libreoffice-impress" in dockerfile
    assert "pandoc" in dockerfile
    assert "poppler-utils" in dockerfile
    assert "acl" in dockerfile
    assert "python-docx==" in requirements_lock
    assert "python-pptx==" in requirements_lock
    assert "reportlab==" in requirements_lock
    assert "requests==" in requirements_lock
    assert "pymupdf==" in requirements_lock
    assert "--require-hashes --no-deps" in dockerfile
    assert "/usr/bin/locale-check" in dockerfile


def test_ensure_image_retry_replaces_read_only_reviewed_requirements_lock(tmp_path):
    def fake_docker(
        args: list[str],
        *,
        check: bool = True,
        capture_output: bool = False,
        timeout_sec=None,
    ):
        if args[:2] == ["image", "inspect"]:
            return subprocess.CompletedProcess(["docker", *args], returncode=1, stdout="", stderr="")
        return subprocess.CompletedProcess(["docker", *args], returncode=0, stdout="", stderr="")

    first = DockerSandboxManager(base_dir=str(tmp_path))
    first._docker = fake_docker  # type: ignore[method-assign]
    first._ensure_image()

    requirements_lock = first.build_root / "workstation-requirements.lock"
    requirements_lock.chmod(0o444)

    retried = DockerSandboxManager(base_dir=str(tmp_path))
    retried._docker = fake_docker  # type: ignore[method-assign]
    retried._ensure_image()

    assert requirements_lock.read_bytes() == AI_WORKER_PYTHON_LOCK_PATH.read_bytes()


def test_ensure_image_defaults_to_no_forced_ai_worker_browser_extensions(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))

    def fake_docker(args: list[str], *, check: bool = True, capture_output: bool = False, timeout_sec=None):
        if args[:2] == ["image", "inspect"]:
            return subprocess.CompletedProcess(["docker", *args], returncode=1, stdout="", stderr="")
        return subprocess.CompletedProcess(["docker", *args], returncode=0, stdout="", stderr="")

    manager._docker = fake_docker  # type: ignore[method-assign]

    manager._ensure_image()

    dockerfile = (manager.build_root / "Dockerfile").read_text()
    assert manager.image.endswith(":phase1-node22-docs8-openclaw2026.7.1-5")
    assert "FROM selenium/standalone-chromium:4.46.0-20260707@sha256:" in dockerfile
    assert "com.glasshive.workstation.provenance=reviewed-v1" in dockerfile
    assert "com.glasshive.workstation.provider-account-acl=required-v1" in dockerfile
    assert AI_WORKER_APT_SNAPSHOT == "20260801T000000Z"
    assert f"com.glasshive.workstation.apt-snapshot={AI_WORKER_APT_SNAPSHOT}" in dockerfile
    assert f"snapshot.ubuntu.com/ubuntu/{AI_WORKER_APT_SNAPSHOT}" in dockerfile
    assert "nodejs_22.23.2-1nodesource1_${arch}.deb" in dockerfile
    assert "sha256sum -c -" in dockerfile
    assert "@openai/codex@0.146.1" in dockerfile
    assert "@anthropic-ai/claude-code@2.1.223" in dockerfile
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


def test_custom_worker_image_without_reviewed_provenance_fails_closed(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WPR_SANDBOX_IMAGE", "example.invalid/custom-worker:test")
    manager = DockerSandboxManager(base_dir=str(tmp_path))

    def fake_docker(args: list[str], **kwargs):
        payload = [{"Config": {"Labels": {}}}]
        return subprocess.CompletedProcess(
            ["docker", *args], returncode=0, stdout=json.dumps(payload), stderr=""
        )

    manager._docker = fake_docker  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="provenance labels"):
        manager._ensure_image()


def test_ensure_image_consumes_reviewed_openclaw_lock_and_disables_bonjour(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))

    def fake_docker(args: list[str], *, check: bool = True, capture_output: bool = False, timeout_sec=None):
        if args[:2] == ["image", "inspect"]:
            return subprocess.CompletedProcess(["docker", *args], returncode=1, stdout="", stderr="")
        return subprocess.CompletedProcess(["docker", *args], returncode=0, stdout="", stderr="")

    manager._docker = fake_docker  # type: ignore[method-assign]

    manager._ensure_image()

    dockerfile = (manager.build_root / "Dockerfile").read_text()
    staged_lock = manager.build_root / "openclaw-runtime-lock" / "package-lock.json"
    assert staged_lock.is_file()
    assert hashlib.sha256(staged_lock.read_bytes()).hexdigest() == OPENCLAW_RUNTIME_LOCK_SHA256
    assert "COPY openclaw-runtime-lock/ /opt/glasshive-openclaw-runtime/" in dockerfile
    assert "npm ci --omit=dev" in dockerfile
    assert "node_modules/openclaw/package.json" in dockerfile
    assert "node_modules/fast-uri/package.json" in dockerfile
    assert OPENCLAW_RUNTIME_VERSION in dockerfile
    assert OPENCLAW_RUNTIME_FAST_URI_VERSION in dockerfile
    assert f"grep -Fq 'OpenClaw {OPENCLAW_RUNTIME_VERSION} ('" in dockerfile
    assert "/usr/local/bin/openclaw" in dockerfile
    assert "ENV OPENCLAW_DISABLE_BONJOUR=1" in dockerfile
    assert "openclaw@latest" not in dockerfile


def test_ensure_image_fails_closed_when_reviewed_openclaw_lock_drifts(tmp_path, monkeypatch):
    invalid_lock_root = tmp_path / "invalid-openclaw-lock"
    invalid_lock_root.mkdir()
    (invalid_lock_root / "package.json").write_text("{}\n")
    (invalid_lock_root / "package-lock.json").write_text("{}\n")
    monkeypatch.setattr(
        "workers_projects_runtime.openclaw_release.OPENCLAW_RUNTIME_LOCK_DIR",
        invalid_lock_root,
    )
    manager = DockerSandboxManager(base_dir=str(tmp_path / "data"))

    def fake_docker(args: list[str], *, check: bool = True, capture_output: bool = False, timeout_sec=None):
        if args[:2] == ["image", "inspect"]:
            return subprocess.CompletedProcess(["docker", *args], returncode=1, stdout="", stderr="")
        pytest.fail("Docker build must not start with an unreviewed OpenClaw lock")

    manager._docker = fake_docker  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="OpenClaw runtime lock"):
        manager._ensure_image()


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
        env={
            "OPENAI_API_KEY": "secret",
            "API_TIMEOUT_MS": "240000",
            "PATH": "/usr/bin:/bin",
        },
    )

    assert calls[0][0] == "root"
    assert "mkdir -p /run/screen" in calls[0][1][-1]
    assert calls[1][1][:2] == ["screen", "-DmS"]
    assert calls[1][2] is True
    assert calls[1][3]["PATH"] == "/usr/bin:/bin"
    assert calls[1][3]["OPENAI_API_KEY"] == "secret"
    assert calls[1][3]["API_TIMEOUT_MS"] == "240000"


def test_screen_session_pid_reads_matching_screen_socket(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    calls: list[list[str]] = []

    class FakeSandbox:
        container_name = "wpr-test"

    manager.inspect = lambda worker_id: FakeSandbox()  # type: ignore[method-assign]

    def fake_docker_exec(container_name, command, *, env=None, cwd=None, detach=False, fire_and_forget=False, user=None):
        calls.append(command)
        if command[:2] == ["bash", "-lc"]:
            assert command[-1] == "job-run_123456"
            return subprocess.CompletedProcess(["docker"], returncode=0, stdout="12345\n", stderr="")
        return subprocess.CompletedProcess(["docker"], returncode=0, stdout="", stderr="")

    manager._docker_exec = fake_docker_exec  # type: ignore[method-assign]

    assert manager.screen_session_pid("wrk_test", "codex-cli", "job-run_123456") == 12345
    assert any("mkdir -p /run/screen" in command[-1] for command in calls)


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
    assert calls[2].startswith("root:set -e; runtime_uid=$(id -u seluser)")
    assert calls[3].startswith("root:set -e; for file in /workspace/.wpr-home/.glasshive/secret-runtime.env")
    assert calls[4:] == ["background", "prime"]


@pytest.mark.parametrize("projected", [False, True])
def test_multi_user_ensure_ready_always_repairs_private_roots(tmp_path, monkeypatch, projected):
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    calls: list[tuple[str, object]] = []

    class FakeSandbox:
        container_name = "wpr-test"
        container_id = "cid"
        state = "running"
        workspace_dir = str(tmp_path / "workspace")
        home_dir = str(tmp_path / "home")
        pid = 1234
        image = "img"
        novnc_port = 57900
        selenium_port = 57901
        openclaw_port = 57902

    sandbox = FakeSandbox()
    manager._require_docker = lambda: None  # type: ignore[method-assign]
    manager._ensure_host_dirs = lambda paths: calls.append(("host", paths))  # type: ignore[method-assign]
    manager._seed_bootstrap = lambda *args, **kwargs: calls.append(("seed", args))  # type: ignore[method-assign]
    manager.inspect = lambda worker_id: None if projected else sandbox  # type: ignore[method-assign]
    manager.fast_sandbox_from_worker = lambda worker: sandbox if projected else None  # type: ignore[method-assign]
    manager._ensure_container_writable_paths = lambda container_name, paths: calls.append(("repair", paths))  # type: ignore[method-assign]
    manager._repair_provider_temp_ownership = lambda container_name: None  # type: ignore[method-assign]
    manager._harden_secret_runtime_files = lambda container_name: None  # type: ignore[method-assign]

    resolved = manager.ensure_ready(
        {"worker_id": "wrk_test", "container_id": "cid" if projected else ""},
        "codex-cli",
    )

    assert resolved is sandbox
    assert ("repair", manager._default_writable_container_paths()) in calls


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


@pytest.mark.parametrize(
    ("environment_name", "environment_value"),
    [
        ("GLASSHIVE_ENTERPRISE_MODE", "true"),
        ("GLASSHIVE_ENTERPRISE_MODE", "enabled"),
        ("WPR_ENTERPRISE_MODE", "1"),
        ("WPR_ENTERPRISE_MODE", "enabled"),
        ("GLASSHIVE_SECURITY_MODE", "multi_user"),
    ],
)
def test_enterprise_writable_path_repair_fails_closed_without_posix_acl(
    tmp_path,
    monkeypatch,
    environment_name,
    environment_value,
):
    monkeypatch.setenv(environment_name, environment_value)
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    calls: list[list[str]] = []

    def fake_docker_exec(container_name, command, *, env=None, cwd=None, detach=False, fire_and_forget=False, user=None):
        calls.append(command)
        return subprocess.CompletedProcess(["docker"], returncode=1, stdout="", stderr="setfacl unavailable")

    manager._docker_exec = fake_docker_exec  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="POSIX ACL"):
        manager._ensure_container_writable_paths(
            "wpr-test",
            ["/workspace/.wpr-home/.glasshive-runs/run_123"],
        )

    assert len(calls) == 1
    repair_script = calls[0][-1]
    assert "command -v setfacl" in repair_script
    assert "u:root:rwX" in repair_script
    assert "g::---,o::---,m::rwX" in repair_script
    assert "d:u:root:rwX" in repair_script
    assert "d:g::---,d:o::---,d:m::rwX" in repair_script
    assert "chmod -R a+rwX" not in repair_script


def test_explicit_single_user_security_mode_keeps_local_acl_compatibility(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "local")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "enabled")
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    calls: list[list[str]] = []

    def fake_docker_exec(container_name, command, *, env=None, cwd=None, detach=False, fire_and_forget=False, user=None):
        calls.append(command)
        return subprocess.CompletedProcess(["docker"], returncode=0, stdout="", stderr="")

    manager._docker_exec = fake_docker_exec  # type: ignore[method-assign]

    manager._ensure_container_writable_paths(
        "wpr-test",
        ["/workspace/.wpr-home/.glasshive-runs/run_123"],
    )

    assert "chmod -R a+rwX" in calls[0][-1]


def test_repair_provider_temp_ownership_restores_claude_runtime_directory(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    calls: list[tuple[str | None, list[str]]] = []

    def fake_docker_exec(container_name, command, *, env=None, cwd=None, detach=False, fire_and_forget=False, user=None):
        calls.append((user, command))
        return subprocess.CompletedProcess(["docker"], returncode=0, stdout="", stderr="")

    manager._docker_exec = fake_docker_exec  # type: ignore[method-assign]

    manager._repair_provider_temp_ownership("wpr-test")

    assert len(calls) == 1
    assert calls[0][0] == "root"
    script = calls[0][1][-1]
    assert "provider_tmp=/workspace/.wpr-home/tmp/claude-${runtime_uid}" in script
    assert 'chown -R "${runtime_uid}:${runtime_gid}" "$provider_tmp"' in script
    assert 'chmod 700 "$provider_tmp"' in script


def test_create_container_applies_default_resource_caps(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    commands: list[list[str]] = []

    def fake_docker(args: list[str], *, check: bool = True, capture_output: bool = False, **kwargs):
        commands.append(args)
        if args[:2] == ["network", "inspect"]:
            return subprocess.CompletedProcess(["docker", *args], returncode=1, stdout="", stderr="not found")
        return subprocess.CompletedProcess(["docker", *args], returncode=0, stdout="cid", stderr="")

    manager._docker = fake_docker  # type: ignore[method-assign]

    manager._create_container(
        "wpr-test",
        {
            "workspace_dir": tmp_path / "workspace",
            "home_dir": tmp_path / "home",
        },
    )

    command = next(item for item in commands if item and item[0] == "run")
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
    manager._repair_provider_temp_ownership = lambda container_name: calls.append("provider_tmp")  # type: ignore[method-assign]
    manager._harden_secret_runtime_files = lambda container_name: calls.append("harden")  # type: ignore[method-assign]
    manager._set_plain_background = lambda container_name: calls.append("background")  # type: ignore[method-assign]
    manager._prime_idle_desktop = lambda container_name: calls.append("prime")  # type: ignore[method-assign]
    manager.inspect = lambda worker_id: sandbox_states.pop(0)  # type: ignore[method-assign]

    manager.ensure_ready({"worker_id": "wrk_test"}, "codex-cli")
    assert calls == ["create", "writable", "provider_tmp", "harden", "background", "prime"]
    marker = json.loads((manager._paths("wrk_test")["state_dir"] / "desktop-prime.json").read_text())
    assert marker["schema"] == "glasshive.desktop_prime.v1"
    assert marker["status"] == "launched"
    assert marker["container_name"] == "wpr-test"
    assert marker["default_browser_url"].startswith("data:text/html")


def test_ensure_ready_records_idle_desktop_prime_failure(tmp_path):
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
    manager._repair_provider_temp_ownership = lambda container_name: calls.append("provider_tmp")  # type: ignore[method-assign]
    manager._harden_secret_runtime_files = lambda container_name: calls.append("harden")  # type: ignore[method-assign]
    manager._set_plain_background = lambda container_name: calls.append("background")  # type: ignore[method-assign]

    def fail_prime(container_name):
        calls.append("prime")
        raise RuntimeError("prime failed")

    manager._prime_idle_desktop = fail_prime  # type: ignore[method-assign]
    manager.inspect = lambda worker_id: sandbox_states.pop(0)  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="prime failed"):
        manager.ensure_ready({"worker_id": "wrk_test"}, "codex-cli")

    marker = json.loads((manager._paths("wrk_test")["state_dir"] / "desktop-prime.json").read_text())
    assert calls == ["create", "writable", "provider_tmp", "harden", "background", "prime"]
    assert marker["status"] == "failed"
    assert "prime failed" in marker["detail"]


def test_ensure_ready_records_idle_desktop_prime_nonzero_return_as_failure(tmp_path):
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
    manager._repair_provider_temp_ownership = lambda container_name: calls.append("provider_tmp")  # type: ignore[method-assign]
    manager._harden_secret_runtime_files = lambda container_name: calls.append("harden")  # type: ignore[method-assign]
    manager._set_plain_background = lambda container_name: calls.append("background")  # type: ignore[method-assign]
    manager.inspect = lambda worker_id: sandbox_states.pop(0)  # type: ignore[method-assign]

    def fake_docker_exec(container_name, command, *, env=None, cwd=None, detach=False, fire_and_forget=False, user=None):
        calls.append("prime")
        return subprocess.CompletedProcess(["docker"], returncode=42, stdout="", stderr="wmctrl failed")

    manager._docker_exec = fake_docker_exec  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="Idle desktop prime failed"):
        manager.ensure_ready({"worker_id": "wrk_test"}, "codex-cli")

    marker = json.loads((manager._paths("wrk_test")["state_dir"] / "desktop-prime.json").read_text())
    assert calls == ["create", "writable", "provider_tmp", "harden", "background", "prime"]
    assert marker["status"] == "failed"
    assert "wmctrl failed" in marker["detail"]
