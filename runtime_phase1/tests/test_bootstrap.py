from __future__ import annotations

import json

import pytest

from workers_projects_runtime.bootstrap import apply_bootstrap, bootstrap_env_for, sign_bootstrap_source_path


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
                    "PRIVATE_INTERNAL_TOKEN": "must-not-project",
                }
            }
        )
    }

    env = bootstrap_env_for(worker)

    assert env["OPENAI_API_KEY"] == "bundle-openai"
    assert env["ANTHROPIC_BASE_URL"] == "https://anthropic.enterprise.example.com"
    assert env["PORTKEY_BASE_URL"] == "https://portkey.example.com"
    assert "PRIVATE_INTERNAL_TOKEN" not in env


def test_local_bootstrap_env_behavior_stays_unfiltered_by_default(monkeypatch):
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
                }
            }
        )
    }

    env = bootstrap_env_for(worker)

    assert env["OPENAI_API_KEY"] == "bundle-openai"
    assert env["PRIVATE_INTERNAL_TOKEN"] == "local-mode-keeps-existing-behavior"


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
