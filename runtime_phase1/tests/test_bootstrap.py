from __future__ import annotations

import json

import pytest

from workers_projects_runtime.bootstrap import (
    GLASSHIVE_CRITICAL_OPERATING_INSTRUCTIONS,
    GLASSHIVE_SAFETY_CHECKPOINT_RULE,
    apply_bootstrap,
    bootstrap_env_for,
    refresh_project_runtime_files_for_worker,
    refresh_runtime_env_for_worker,
    sign_bootstrap_source_path,
)


def _clear_ambient_provider_env(monkeypatch):
    for key in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "PORTKEY_API_KEY",
        "PORTKEY_BASE_URL",
        "PORTKEY_PROVIDER",
        "PORTKEY_VIRTUAL_KEY",
        "PORTKEY_CONFIG",
    ):
            monkeypatch.delenv(key, raising=False)


def test_bootstrap_materializes_canonical_worker_operating_contract(tmp_path):
    apply_bootstrap(
        home_dir=tmp_path / "home",
        workspace_dir=tmp_path / "workspace",
        runtime_name="codex-cli",
        worker={"bootstrap_bundle_json": json.dumps({})},
        copy_file=lambda source, target: None,
        copy_tree=lambda source, target: None,
    )

    agents_text = (tmp_path / "workspace" / "AGENTS.md").read_text()
    assert GLASSHIVE_CRITICAL_OPERATING_INSTRUCTIONS in agents_text
    assert GLASSHIVE_SAFETY_CHECKPOINT_RULE in agents_text
    assert "FINAL REPORT:" in agents_text
    assert "polished ordinary end-user artifact" in agents_text
    assert "Do not force a download" in agents_text
    assert "Native capability inventory" in agents_text
    assert "Claude Code skill families" in agents_text
    assert "Codex skill families" in agents_text
    assert "daymade deep-research" in agents_text
    assert "openai pdf" in agents_text
    assert "anthropic document-skills" in agents_text
    assert "Use these capabilities when relevant" in agents_text


def test_enterprise_bootstrap_filters_worker_env_and_projects_provider_env(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_PROJECT_PROVIDER_ENV", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "process-openai")
    monkeypatch.setenv("PORTKEY_BASE_URL", "https://portkey.example.com")

    worker = {
        "bootstrap_bundle_json": json.dumps(
            {
                "env": {
                    "OPENAI_API_KEY": "bundle-openai",
                    "ANTHROPIC_BASE_URL": "https://anthropic.enterprise.example.com",
                    "GLASSHIVE_CAPABILITY_BROKER_TOKEN": "public-safe-broker-grant",
                    "PRIVATE_INTERNAL_TOKEN": "must-not-project",
                }
            }
        )
    }

    env = bootstrap_env_for(worker)

    assert env["OPENAI_API_KEY"] == "bundle-openai"
    assert env["ANTHROPIC_BASE_URL"] == "https://anthropic.enterprise.example.com"
    assert env["GLASSHIVE_CAPABILITY_BROKER_TOKEN"] == "public-safe-broker-grant"
    assert env["PORTKEY_BASE_URL"] == "https://portkey.example.com"
    assert "PRIVATE_INTERNAL_TOKEN" not in env


def test_enterprise_worker_env_allowlist_rejects_user_provider_tokens(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_WORKER_ENV_ALLOWLIST", "GOOGLE_REFRESH_TOKEN")

    worker = {
        "bootstrap_bundle_json": json.dumps(
            {
                "env": {
                    "GOOGLE_REFRESH_TOKEN": "provider-token-must-not-project",
                }
            }
        )
    }

    with pytest.raises(RuntimeError, match="must not include user provider"):
        bootstrap_env_for(worker)


def test_local_bootstrap_env_filters_user_provider_tokens_without_blocking_provider_keys(monkeypatch):
    monkeypatch.delenv("GLASSHIVE_ENTERPRISE_MODE", raising=False)
    monkeypatch.delenv("WPR_ENTERPRISE_MODE", raising=False)
    monkeypatch.delenv("GLASSHIVE_PROJECT_PROVIDER_ENV", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "process-openai")

    worker = {
        "bootstrap_bundle_json": json.dumps(
            {
                "env": {
                    "OPENAI_API_KEY": "bundle-openai",
                    "PRIVATE_INTERNAL_TOKEN": "local-mode-keeps-existing-behavior",
                    "GOOGLE_REFRESH_TOKEN": "must-not-project",
                    "GOOGLE_OAUTH_CLIENT_SECRET": "must-not-project",
                    "MS365_ACCESS_TOKEN": "must-not-project",
                }
            }
        )
    }

    env = bootstrap_env_for(worker)

    assert env["OPENAI_API_KEY"] == "bundle-openai"
    assert env["PRIVATE_INTERNAL_TOKEN"] == "local-mode-keeps-existing-behavior"
    assert "GOOGLE_REFRESH_TOKEN" not in env
    assert "GOOGLE_OAUTH_CLIENT_SECRET" not in env
    assert "MS365_ACCESS_TOKEN" not in env


def test_enterprise_bootstrap_does_not_copy_host_auth_or_identity_files(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")

    copied_files: list[tuple[str, str]] = []
    copied_trees: list[tuple[str, str]] = []

    apply_bootstrap(
        home_dir=tmp_path / "home",
        workspace_dir=tmp_path / "workspace",
        runtime_name="claude-code",
        worker={"bootstrap_bundle_json": json.dumps({})},
        copy_file=lambda source, target: copied_files.append((str(source), str(target))),
        copy_tree=lambda source, target: copied_trees.append((str(source), str(target))),
    )

    assert copied_files == []
    assert copied_trees == []


def test_enterprise_bootstrap_keeps_provider_secrets_out_of_interactive_runtime_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    _clear_ambient_provider_env(monkeypatch)
    worker = {
        "bootstrap_bundle_json": json.dumps(
            {
                "env": {
                    "OPENAI_API_KEY": "synthetic-openai-key-not-for-shell",
                    "PORTKEY_VIRTUAL_KEY": "pk-test-not-for-shell",
                    "OPENAI_BASE_URL": "https://provider.example.com/v1",
                }
            }
        )
    }

    apply_bootstrap(
        home_dir=tmp_path / "home",
        workspace_dir=tmp_path / "workspace",
        runtime_name="codex-cli",
        worker=worker,
        copy_file=lambda source, target: None,
        copy_tree=lambda source, target: None,
    )

    runtime_env = (tmp_path / "home" / ".glasshive" / "runtime.env").read_text()
    secret_env = (tmp_path / "home" / ".glasshive" / "secret-runtime.env").read_text()
    secret_keys = (tmp_path / "home" / ".glasshive" / "secret-runtime.keys").read_text().splitlines()

    assert "OPENAI_BASE_URL" in runtime_env
    assert "OPENAI_API_KEY" not in runtime_env
    assert "PORTKEY_VIRTUAL_KEY" not in runtime_env
    assert "OPENAI_API_KEY" in secret_env
    assert "PORTKEY_VIRTUAL_KEY" in secret_env
    assert set(secret_keys) == {"OPENAI_API_KEY", "PORTKEY_VIRTUAL_KEY"}
    assert oct((tmp_path / "home" / ".glasshive" / "secret-runtime.env").stat().st_mode & 0o777) == "0o600"
    assert oct((tmp_path / "home" / ".glasshive" / "secret-runtime.keys").stat().st_mode & 0o777) == "0o600"


def test_enterprise_bootstrap_replaces_persisted_sandbox_owned_secret_file(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    _clear_ambient_provider_env(monkeypatch)
    home_dir = tmp_path / "home"
    glasshive_dir = home_dir / ".glasshive"
    glasshive_dir.mkdir(parents=True)
    stale_secret = glasshive_dir / "secret-runtime.env"
    stale_secret.write_text("stale")
    stale_secret.chmod(0o400)
    stale_keys = glasshive_dir / "secret-runtime.keys"
    stale_keys.write_text("STALE_KEY\n")
    stale_keys.chmod(0o400)
    worker = {
        "bootstrap_bundle_json": json.dumps(
            {
                "env": {
                    "OPENAI_API_KEY": "replacement-key",
                    "OPENAI_BASE_URL": "https://provider.example.com/v1",
                }
            }
        )
    }

    apply_bootstrap(
        home_dir=home_dir,
        workspace_dir=tmp_path / "workspace",
        runtime_name="codex-cli",
        worker=worker,
        copy_file=lambda source, target: None,
        copy_tree=lambda source, target: None,
    )

    assert "replacement-key" in stale_secret.read_text()
    assert stale_keys.read_text().splitlines() == ["OPENAI_API_KEY"]
    assert oct(stale_secret.stat().st_mode & 0o777) == "0o600"
    assert oct(stale_keys.stat().st_mode & 0o777) == "0o600"


def test_enterprise_run_only_secrets_are_refreshed_for_each_run(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    _clear_ambient_provider_env(monkeypatch)
    worker = {
        "bootstrap_bundle_json": json.dumps(
            {
                "env": {
                    "OPENAI_API_KEY": "synthetic-openai-key-not-for-shell",
                    "OPENAI_BASE_URL": "https://provider.example.com/v1",
                }
            }
        )
    }
    home_dir = tmp_path / "home"

    apply_bootstrap(
        home_dir=home_dir,
        workspace_dir=tmp_path / "workspace",
        runtime_name="codex-cli",
        worker=worker,
        copy_file=lambda source, target: None,
        copy_tree=lambda source, target: None,
    )
    secret_env = home_dir / ".glasshive" / "secret-runtime.env"
    runtime_env = home_dir / ".glasshive" / "runtime.env"
    assert "OPENAI_API_KEY" in secret_env.read_text()
    assert "OPENAI_API_KEY" not in runtime_env.read_text()

    secret_env.unlink()
    refresh_runtime_env_for_worker(home_dir, worker)

    assert "OPENAI_API_KEY" in secret_env.read_text()
    assert "OPENAI_API_KEY" not in runtime_env.read_text()
    assert oct(secret_env.stat().st_mode & 0o777) == "0o600"


def test_refresh_project_runtime_files_rotates_broker_mcp_configs(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    home_dir = tmp_path / "home"
    workspace_dir = tmp_path / "workspace"

    def worker_for(token: str, url: str) -> dict:
        return {
            "bootstrap_bundle_json": json.dumps(
                {
                    "claude_project_mcp": {
                        "glasshive-user-capabilities": {
                            "type": "http",
                            "url": url,
                            "headers": {"Authorization": f"Bearer {token}"},
                        }
                    },
                    "codex_config_append": (
                        "[mcp_servers.glasshive-user-capabilities]\n"
                        f'url = "{url}"\n'
                        'bearer_token_env_var = "GLASSHIVE_CAPABILITY_BROKER_TOKEN"'
                    ),
                    "env": {"GLASSHIVE_CAPABILITY_BROKER_TOKEN": token},
                }
            )
        }

    apply_bootstrap(
        home_dir=home_dir,
        workspace_dir=workspace_dir,
        runtime_name="codex-cli",
        worker=worker_for("old-grant", "http://broker-old.example/mcp"),
        copy_file=lambda source, target: None,
        copy_tree=lambda source, target: None,
    )
    refresh_project_runtime_files_for_worker(
        home_dir,
        workspace_dir,
        worker_for("new-grant", "http://broker-new.example/mcp"),
    )

    project_mcp = json.loads((workspace_dir / ".mcp.json").read_text())
    codex_config = (home_dir / ".codex" / "config.toml").read_text()

    assert project_mcp["mcpServers"]["glasshive-user-capabilities"]["headers"]["Authorization"] == (
        "Bearer ${GLASSHIVE_CAPABILITY_BROKER_TOKEN}"
    )
    assert "new-grant" not in (workspace_dir / ".mcp.json").read_text()
    assert "old-grant" not in (workspace_dir / ".mcp.json").read_text()
    assert "http://broker-new.example/mcp" in codex_config
    assert "http://broker-old.example/mcp" not in codex_config
    assert codex_config.count("[mcp_servers.glasshive-user-capabilities]") == 1
    assert oct((workspace_dir / ".mcp.json").stat().st_mode & 0o777) == "0o600"
    assert oct((home_dir / ".codex" / "config.toml").stat().st_mode & 0o777) == "0o600"


def test_enterprise_ambient_provider_keys_are_run_only(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    _clear_ambient_provider_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-ambient-key-not-for-shell")

    home_dir = tmp_path / "home"
    apply_bootstrap(
        home_dir=home_dir,
        workspace_dir=tmp_path / "workspace",
        runtime_name="codex-cli",
        worker={"bootstrap_bundle_json": json.dumps({"env": {}})},
        copy_file=lambda source, target: None,
        copy_tree=lambda source, target: None,
    )

    glasshive_dir = home_dir / ".glasshive"
    runtime_env = glasshive_dir / "runtime.env"
    secret_env = glasshive_dir / "secret-runtime.env"
    secret_keys = glasshive_dir / "secret-runtime.keys"

    assert "OPENAI_API_KEY" in secret_env.read_text()
    assert "OPENAI_API_KEY" in secret_keys.read_text()
    assert not runtime_env.exists() or "OPENAI_API_KEY" not in runtime_env.read_text()


def test_local_bootstrap_keeps_legacy_interactive_runtime_env_behavior(tmp_path, monkeypatch):
    monkeypatch.delenv("GLASSHIVE_ENTERPRISE_MODE", raising=False)
    worker = {"bootstrap_bundle_json": json.dumps({"env": {"OPENAI_API_KEY": "local-dev-key"}})}

    apply_bootstrap(
        home_dir=tmp_path / "home",
        workspace_dir=tmp_path / "workspace",
        runtime_name="codex-cli",
        worker=worker,
        copy_file=lambda source, target: None,
        copy_tree=lambda source, target: None,
    )

    runtime_env = (tmp_path / "home" / ".glasshive" / "runtime.env").read_text()
    assert "OPENAI_API_KEY" in runtime_env
    assert oct((tmp_path / "home" / ".glasshive" / "runtime.env").stat().st_mode & 0o777) == "0o600"
    assert not (tmp_path / "home" / ".glasshive" / "secret-runtime.env").exists()


def test_enterprise_bootstrap_rejects_unsigned_source_path(tmp_path, monkeypatch):
    uploads_root = tmp_path / "uploads"
    other_user_file = uploads_root / "user-b" / "brief.txt"
    other_user_file.parent.mkdir(parents=True)
    other_user_file.write_text("other user's data")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("WPR_API_TOKEN", "service-secret")
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", str(uploads_root))

    worker = {
        "tenant_id": "tenant-alpha",
        "owner_id": "user-a",
        "bootstrap_bundle_json": json.dumps(
            {
                "files": [
                    {
                        "scope": "workspace",
                        "path": "uploads/brief.txt",
                        "source_path": str(other_user_file),
                    }
                ]
            }
        ),
    }

    with pytest.raises(PermissionError, match="not authorized"):
        apply_bootstrap(
            home_dir=tmp_path / "home",
            workspace_dir=tmp_path / "workspace",
            runtime_name="codex-cli",
            worker=worker,
            copy_file=lambda source, target: target.write_text(source.read_text()),
            copy_tree=lambda source, target: None,
        )


def test_enterprise_bootstrap_accepts_signed_source_path_for_same_user(tmp_path, monkeypatch):
    uploads_root = tmp_path / "uploads"
    user_file = uploads_root / "user-a" / "brief.txt"
    user_file.parent.mkdir(parents=True)
    user_file.write_text("same user's data")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("WPR_API_TOKEN", "service-secret")
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", str(uploads_root))
    token = sign_bootstrap_source_path(user_file, tenant_id="tenant-alpha", owner_id="user-a")

    worker = {
        "tenant_id": "tenant-alpha",
        "owner_id": "user-a",
        "bootstrap_bundle_json": json.dumps(
            {
                "files": [
                    {
                        "scope": "workspace",
                        "path": "uploads/brief.txt",
                        "source_path": str(user_file),
                        "source_path_token": token,
                    }
                ]
            }
        ),
    }
    workspace = tmp_path / "workspace"

    apply_bootstrap(
        home_dir=tmp_path / "home",
        workspace_dir=workspace,
        runtime_name="codex-cli",
        worker=worker,
        copy_file=lambda source, target: target.write_text(source.read_text()),
        copy_tree=lambda source, target: None,
    )

    assert (workspace / "uploads" / "brief.txt").read_text() == "same user's data"


def test_bootstrap_materializes_base64_uploaded_file(tmp_path):
    workspace = tmp_path / "workspace"

    apply_bootstrap(
        home_dir=tmp_path / "home",
        workspace_dir=workspace,
        runtime_name="codex-cli",
        worker={
            "bootstrap_bundle_json": json.dumps(
                {
                    "files": [
                        {
                            "scope": "workspace",
                            "path": "uploads/report.bin",
                            "encoding": "base64",
                            "content_base64": "AAECSGVsbG8=",
                        }
                    ]
                }
            )
        },
        copy_file=lambda source, target: None,
        copy_tree=lambda source, target: None,
    )

    assert (workspace / "uploads" / "report.bin").read_bytes() == b"\x00\x01\x02Hello"
