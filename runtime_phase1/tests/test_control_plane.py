from __future__ import annotations

import hashlib
import os
import json
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

import pytest

from workers_projects_runtime.control_plane import (
    ControlPlaneConflict,
    ControlPlaneError,
    ControlPlaneStore,
)
from library_test_support import library_manifest, register_manifest
from workers_projects_runtime import provider_accounts as provider_accounts_module
from workers_projects_runtime.provider_accounts import (
    ProviderAccountHomeManager,
    ProviderSetupManager,
    _provider_setup_guidance,
)
from workers_projects_runtime.provider_accounts import provider_platform_support
from workers_projects_runtime.schema_version import record_schema_version, require_compatible_schema


def _create_workspace_record(database, *, worker_id="wrk_public_safe", owner_id="user-a"):
    with sqlite3.connect(database) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS workers (
                worker_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                profile TEXT NOT NULL,
                bootstrap_bundle_json TEXT,
                duplication_report_json TEXT NOT NULL DEFAULT '{}',
                updated_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO workers
                (worker_id, tenant_id, owner_id, profile, bootstrap_bundle_json, updated_at)
            VALUES (?, 'tenant-a', ?, 'codex-cli', '{}', ?)
            """,
            (worker_id, owner_id, time.time()),
        )


def test_provider_accounts_are_owner_scoped_defaulted_and_secret_free(tmp_path):
    database = tmp_path / "runtime.db"
    store = ControlPlaneStore(str(database))

    first = store.create_provider_account(
        tenant_id="tenant-a",
        owner_id="user-a",
        provider="codex",
        label="Personal Codex",
        auth_method="subscription",
        platform_support="supported",
        secret_locator="native-home://codex-account-a",
        status="ready",
        make_default=True,
    )
    second = store.create_provider_account(
        tenant_id="tenant-a",
        owner_id="user-a",
        provider="codex",
        label="API route",
        auth_method="api_key",
        platform_support="supported",
        secret_locator="keychain://codex-api-route",
        make_default=True,
    )
    store.create_provider_account(
        tenant_id="tenant-a",
        owner_id="user-b",
        provider="codex",
        label="Other user",
        auth_method="subscription",
        platform_support="supported",
        secret_locator="native-home://codex-account-b",
    )

    accounts = store.list_provider_accounts(tenant_id="tenant-a", owner_id="user-a")
    assert [account["account_id"] for account in accounts] == [first["account_id"], second["account_id"]]
    assert [account["is_default"] for account in accounts] == [False, True]
    assert all("token" not in account and "api_key" not in account for account in accounts)
    database_bytes = database.read_bytes()
    assert b"native-home://codex-account-b" in database_bytes
    assert b"raw-secret-marker" not in database_bytes
    with pytest.raises(ControlPlaneError, match="secret locator"):
        store.create_provider_account(
            tenant_id="tenant-a",
            owner_id="user-a",
            provider="codex",
            label="Unsafe",
            auth_method="api_key",
            platform_support="supported",
            secret_locator="raw-secret-marker",
        )


def test_first_provider_account_becomes_the_default_without_an_extra_checkbox(tmp_path):
    store = ControlPlaneStore(str(tmp_path / "runtime.db"))

    first = store.create_provider_account(
        tenant_id="tenant-a",
        owner_id="user-a",
        provider="codex",
        label="Personal Codex",
        auth_method="subscription",
        platform_support="supported",
        secret_locator="native-home://auto",
    )
    second = store.create_provider_account(
        tenant_id="tenant-a",
        owner_id="user-a",
        provider="codex",
        label="Another Codex",
        auth_method="subscription",
        platform_support="supported",
        secret_locator="native-home://auto",
    )

    assert first["is_default"] is True
    assert second["is_default"] is False


def test_provider_account_lease_is_durable_exclusive_and_recovers_after_expiry(tmp_path):
    store = ControlPlaneStore(str(tmp_path / "runtime.db"))
    account = store.create_provider_account(
        tenant_id="tenant-a",
        owner_id="user-a",
        provider="codex",
        label="Personal Codex",
        auth_method="subscription",
        platform_support="supported",
        secret_locator="native-home://codex-account-a",
        status="ready",
    )
    now = time.time()
    first = store.acquire_provider_lease(
        account_id=account["account_id"],
        tenant_id="tenant-a",
        owner_id="user-a",
        lane="default",
        worker_id="wrk_a",
        run_id="run_a",
        ttl_seconds=60,
        now=now,
    )

    restarted_store = ControlPlaneStore(str(tmp_path / "runtime.db"))
    with pytest.raises(ControlPlaneConflict, match="already in use"):
        restarted_store.acquire_provider_lease(
            account_id=account["account_id"],
            tenant_id="tenant-a",
            owner_id="user-a",
            lane="default",
            worker_id="wrk_b",
            run_id="run_b",
            ttl_seconds=60,
            now=now + 1,
        )

    with pytest.raises(ControlPlaneConflict, match="already in use"):
        restarted_store.acquire_provider_lease(
            account_id=account["account_id"],
            tenant_id="tenant-a",
            owner_id="user-a",
            lane="different-operation",
            worker_id="wrk_c",
            run_id="run_c",
            ttl_seconds=60,
            now=now + 1,
        )

    recovered = restarted_store.acquire_provider_lease(
        account_id=account["account_id"],
        tenant_id="tenant-a",
        owner_id="user-a",
        lane="default",
        worker_id="wrk_b",
        run_id="run_b",
        ttl_seconds=60,
        now=now + 61,
    )
    assert recovered["lease_id"] != first["lease_id"]
    assert recovered["worker_id"] == "wrk_b"
    restarted_store.release_provider_lease(
        lease_id=recovered["lease_id"],
        tenant_id="tenant-a",
        owner_id="user-a",
        now=now + 62,
    )
    assert restarted_store.active_provider_lease(account["account_id"], "default", now=now + 63) is None


def test_provider_account_lease_heartbeat_extends_only_an_active_owner_scoped_lease(tmp_path):
    store = ControlPlaneStore(str(tmp_path / "runtime.db"))
    account = store.create_provider_account(
        tenant_id="tenant-a",
        owner_id="user-a",
        provider="codex",
        label="Personal Codex",
        auth_method="subscription",
        platform_support="supported",
        secret_locator="native-home://auto",
        status="ready",
    )
    lease = store.acquire_provider_lease(
        account_id=account["account_id"],
        tenant_id="tenant-a",
        owner_id="user-a",
        lane="mission",
        worker_id="wrk_public_safe",
        run_id="run_public_safe",
        ttl_seconds=15,
        now=100.0,
    )

    renewed = store.heartbeat_provider_lease(
        lease_id=lease["lease_id"],
        tenant_id="tenant-a",
        owner_id="user-a",
        ttl_seconds=30,
        now=110.0,
    )
    assert renewed["heartbeat_at"] == 110.0
    assert renewed["expires_at"] == 140.0

    with pytest.raises(ControlPlaneError, match="no longer active"):
        store.heartbeat_provider_lease(
            lease_id=lease["lease_id"],
            tenant_id="tenant-a",
            owner_id="user-b",
            ttl_seconds=30,
            now=111.0,
        )
    with pytest.raises(ControlPlaneError, match="no longer active"):
        store.heartbeat_provider_lease(
            lease_id=lease["lease_id"],
            tenant_id="tenant-a",
            owner_id="user-a",
            ttl_seconds=30,
            now=141.0,
        )


def test_provider_account_lease_fails_closed_when_account_is_not_ready(tmp_path):
    store = ControlPlaneStore(str(tmp_path / "control-plane.db"))
    account = store.create_provider_account(
        tenant_id="tenant-a",
        owner_id="user-a",
        provider="codex",
        label="Personal Codex",
        auth_method="subscription",
        platform_support="supported",
        secret_locator="native-home://lease-not-ready",
        status="ready",
    )
    store.update_provider_account_status(
        account_id=account["account_id"],
        tenant_id="tenant-a",
        owner_id="user-a",
        status="action_required",
        reconnect_reason="Synthetic renewal failure",
    )

    with pytest.raises(ControlPlaneConflict, match="not ready"):
        store.acquire_provider_lease(
            account_id=account["account_id"],
            tenant_id="tenant-a",
            owner_id="user-a",
            lane="codex-cli:mission",
            worker_id="wrk-one",
            run_id="run-one",
            ttl_seconds=60,
        )


def test_connection_library_grant_requires_single_use_human_confirmation(tmp_path):
    database = tmp_path / "runtime.db"
    store = ControlPlaneStore(str(database))
    _create_workspace_record(database)
    item = register_manifest(
        store,
        library_manifest(
            stable_id="skill.synthetic.summary",
            profiles=["codex-cli", "claude-code"],
            scopes=["documents:read"],
            files=[{"scope": "workspace", "path": "SKILL.md", "content": "synthetic"}],
        ),
    )
    pending = store.create_pending_change(
        tenant_id="tenant-a",
        owner_id="user-a",
        change_type="workspace_grant",
        target_id="wrk_public_safe",
        payload={"library_id": item["library_id"]},
        ttl_seconds=300,
    )

    browser_metadata = store.get_pending_change(
        change_id=pending["change_id"],
        tenant_id="tenant-a",
        owner_id="user-a",
    )
    assert browser_metadata["payload"]["library_id"] == item["library_id"]
    assert "confirmation_token" not in browser_metadata
    assert "confirmation_hash" not in browser_metadata
    with pytest.raises(ControlPlaneError, match="not found for this user"):
        store.get_pending_change(
            change_id=pending["change_id"],
            tenant_id="tenant-a",
            owner_id="user-b",
        )

    with pytest.raises(ControlPlaneError, match="confirmation"):
        store.confirm_pending_change(
            change_id=pending["change_id"],
            tenant_id="tenant-a",
            owner_id="user-a",
            confirmation_token="wrong-token",
        )
    confirmed = store.confirm_pending_change(
        change_id=pending["change_id"],
        tenant_id="tenant-a",
        owner_id="user-a",
        confirmation_token=pending["confirmation_token"],
    )
    assert confirmed["status"] == "confirmed"
    assert confirmed["applied"]["worker_id"] == "wrk_public_safe"
    assert confirmed["applied"]["library_id"] == item["library_id"]
    assert confirmed["applied"]["connection_id"] is None
    assert confirmed["applied"]["scopes"] == ["documents:read"]
    grants = store.list_workspace_grants(
        tenant_id="tenant-a",
        owner_id="user-a",
        worker_id="wrk_public_safe",
    )
    assert [grant["grant_id"] for grant in grants] == [confirmed["applied"]["grant_id"]]
    with sqlite3.connect(database) as conn:
        bundle = json.loads(conn.execute(
            "SELECT bootstrap_bundle_json FROM workers WHERE worker_id = 'wrk_public_safe'"
        ).fetchone()[0])
    assert bundle["files"][0]["path"] == "SKILL.md"
    assert "confirmation_token" not in confirmed
    with pytest.raises(ControlPlaneConflict, match="already resolved"):
        store.confirm_pending_change(
            change_id=pending["change_id"],
            tenant_id="tenant-a",
            owner_id="user-a",
            confirmation_token=pending["confirmation_token"],
        )


def test_pending_change_cannot_grant_another_users_connection(tmp_path):
    database = tmp_path / "runtime.db"
    store = ControlPlaneStore(str(database))
    _create_workspace_record(database)
    connection = store.create_connection(
        tenant_id="tenant-a",
        owner_id="other-user",
        kind="data",
        adapter="synthetic-broker",
        label="Other user's connection",
        status="ready",
        secret_locator="broker://other-user-connection",
        scopes=["documents:read"],
    )
    pending = store.create_pending_change(
        tenant_id="tenant-a",
        owner_id="user-a",
        change_type="workspace_grant",
        target_id="wrk_public_safe",
        payload={"connection_id": connection["connection_id"]},
        ttl_seconds=300,
    )

    with pytest.raises(ControlPlaneError, match="brokered workspace bundle"):
        store.confirm_pending_change(
            change_id=pending["change_id"],
            tenant_id="tenant-a",
            owner_id="user-a",
            confirmation_token=pending["confirmation_token"],
        )
    assert store.list_workspace_grants(
        tenant_id="tenant-a",
        owner_id="user-a",
        worker_id="wrk_public_safe",
    ) == []


def test_provider_account_homes_are_private_and_platform_policy_is_explicit(tmp_path):
    manager = ProviderAccountHomeManager(tmp_path / "accounts")
    codex_home = manager.ensure_home(
        tenant_id="tenant-a",
        owner_id="user-a",
        account_id="acct_public_safe",
        provider="codex",
    )
    environment = manager.runtime_environment(provider="codex", account_home=codex_home)

    assert environment == {"CODEX_HOME": str(codex_home / "codex")}
    assert os.stat(codex_home).st_mode & 0o077 == 0
    assert os.stat(codex_home / "codex").st_mode & 0o077 == 0
    for unsafe_account_id in (".", ".."):
        with pytest.raises(ControlPlaneError, match="account id is invalid"):
            manager.account_home_path(
                tenant_id="tenant-a",
                owner_id="user-a",
                account_id=unsafe_account_id,
            )
    with pytest.raises(ControlPlaneError, match="macOS host-native Claude"):
        manager.require_supported_route(
            provider="claude",
            auth_method="subscription",
            execution_mode="host",
            platform_name="darwin",
            hosted_consumer_auth_enabled=False,
        )
    with pytest.raises(ControlPlaneError, match="permission"):
        manager.require_supported_route(
            provider="claude",
            auth_method="subscription",
            execution_mode="docker",
            platform_name="linux",
            hosted_consumer_auth_enabled=False,
        )


def test_multi_user_subscription_support_requires_reviewed_container_isolation(monkeypatch):
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setenv("GLASSHIVE_ENABLE_CODEX_PERSONAL_ACCOUNTS", "true")

    assert provider_platform_support(
        provider="codex", auth_method="subscription", platform_name="linux"
    ) == "isolated_substrate_required"

    monkeypatch.setenv(
        "GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION", "per_worker_container"
    )
    assert provider_platform_support(
        provider="codex", auth_method="subscription", platform_name="linux"
    ) == "supported"


def test_provider_account_home_creation_rejects_symlink_components(tmp_path):
    root = tmp_path / "accounts"
    manager = ProviderAccountHomeManager(root)
    tenant_home = root / hashlib.sha256(b"tenant-a").hexdigest()[:24]
    tenant_home.symlink_to(tmp_path / "outside", target_is_directory=True)

    with pytest.raises(ControlPlaneError, match="not a safe managed directory"):
        manager.ensure_home(
            tenant_id="tenant-a",
            owner_id="user-a",
            account_id="acct_public_safe",
            provider="codex",
        )


def test_provider_setup_uses_isolated_native_home_and_never_inherits_global_credentials(tmp_path, monkeypatch):
    cli = tmp_path / "synthetic-codex"
    capture = tmp_path / "captured-env.json"
    cli_source = """#!/usr/bin/env python3
import json
import os
import sys
capture_path = __CAPTURE_PATH__
if sys.argv[1:] == ['login', '--device-auth']:
    os.makedirs(os.environ['CODEX_HOME'], exist_ok=True)
    auth_path = os.path.join(os.environ['CODEX_HOME'], 'auth.json')
    with open(auth_path, 'w', encoding='utf-8') as handle:
        json.dump({'synthetic': True}, handle)
    os.chmod(auth_path, 0o644)
    with open(capture_path, 'w', encoding='utf-8') as handle:
        json.dump({
            'CODEX_HOME': os.environ.get('CODEX_HOME'),
            'HOME': os.environ.get('HOME'),
            'OPENAI_API_KEY': os.environ.get('OPENAI_API_KEY'),
            'OPENAI_BASE_URL': os.environ.get('OPENAI_BASE_URL'),
            'WPR_API_TOKEN': os.environ.get('WPR_API_TOKEN'),
            'GLASSHIVE_INFERENCE_BROKER_SECRET': os.environ.get('GLASSHIVE_INFERENCE_BROKER_SECRET'),
            'VIVENTIUM_CALL_SESSION_SECRET': os.environ.get('VIVENTIUM_CALL_SESSION_SECRET'),
        }, handle)
    print('Open https://auth.openai.com/codex/device and enter one-time code SAFE-CODE', flush=True)
    raise SystemExit(0)
if sys.argv[1:] == ['login', 'status']:
    raise SystemExit(0)
if sys.argv[1:] == ['logout']:
    raise SystemExit(0)
raise SystemExit(2)
"""
    cli.write_text(
        cli_source.replace("__CAPTURE_PATH__", repr(str(capture))),
        encoding="utf-8",
    )
    cli.chmod(0o700)
    monkeypatch.setenv("WPR_CODEX_CLI_PATH", str(cli))
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-personal-login")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://global-route.invalid")
    monkeypatch.setenv("WPR_API_TOKEN", "must-not-reach-personal-login")
    monkeypatch.setenv("GLASSHIVE_INFERENCE_BROKER_SECRET", "must-not-reach-personal-login")
    monkeypatch.setenv("VIVENTIUM_CALL_SESSION_SECRET", "must-not-reach-personal-login")
    monkeypatch.setenv("GLASSHIVE_ENABLE_CODEX_PERSONAL_ACCOUNTS", "true")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION", "per_worker_container")
    store = ControlPlaneStore(str(tmp_path / "runtime.db"))
    account = store.create_provider_account(
        tenant_id="tenant-a",
        owner_id="user-a",
        provider="codex",
        label="Personal Codex",
        auth_method="subscription",
        platform_support="supported",
        secret_locator="native-home://auto",
    )
    reconciled: list[Path] = []

    def reconcile_under_lease(account_home: Path) -> None:
        assert store.active_provider_lease(account["account_id"], "provider-setup") is not None
        reconciled.append(account_home)

    setup = ProviderSetupManager(
        store=store,
        home_root=tmp_path / "provider-homes",
        reconcile_provider_account_binding=reconcile_under_lease,
    )

    started = setup.start(account_id=account["account_id"], tenant_id="tenant-a", owner_id="user-a")
    assert started["status"] == "connecting"
    result = started
    deadline = time.time() + 5
    while not result["complete"] and time.time() < deadline:
        time.sleep(0.03)
        result = setup.status(account_id=account["account_id"], tenant_id="tenant-a", owner_id="user-a")

    assert result["status"] == "ready"
    assert "SAFE-CODE" in result["instructions"]
    assert result["provider"] == "codex"
    assert result["setup_url"] == "https://auth.openai.com/codex/device"
    assert result["setup_code"] == "SAFE-CODE"
    assert result["help_url"] == "https://chatgpt.com/#settings/Security"
    captured = json.loads(capture.read_text(encoding="utf-8"))
    assert captured["OPENAI_API_KEY"] is None
    assert captured["OPENAI_BASE_URL"] is None
    assert captured["WPR_API_TOKEN"] is None
    assert captured["GLASSHIVE_INFERENCE_BROKER_SECRET"] is None
    assert captured["VIVENTIUM_CALL_SESSION_SECRET"] is None
    assert captured["CODEX_HOME"].startswith(str(tmp_path / "provider-homes"))
    assert captured["HOME"].startswith(str(tmp_path / "provider-homes"))
    assert os.stat(captured["CODEX_HOME"]).st_mode & 0o077 == 0
    assert os.stat(Path(captured["CODEX_HOME"]) / "auth.json").st_mode & 0o077 == 0
    assert len(reconciled) >= 2
    lease = store.acquire_provider_lease(
        account_id=account["account_id"],
        tenant_id="tenant-a",
        owner_id="user-a",
        lane="default",
        worker_id="wrk_public_safe",
        run_id="run_public_safe",
        ttl_seconds=60,
    )

    with pytest.raises(ControlPlaneConflict, match="already in use"):
        setup.start(
            account_id=account["account_id"],
            tenant_id="tenant-a",
            owner_id="user-a",
        )
    with pytest.raises(ControlPlaneConflict, match="already in use"):
        setup.disconnect(
            account_id=account["account_id"],
            tenant_id="tenant-a",
            owner_id="user-a",
        )

    assert Path(captured["CODEX_HOME"]).parent.exists()
    assert store.active_provider_lease(account["account_id"], "default") is not None
    store.release_provider_lease(
        lease_id=lease["lease_id"],
        tenant_id="tenant-a",
        owner_id="user-a",
    )

    disconnected = setup.disconnect(
        account_id=account["account_id"],
        tenant_id="tenant-a",
        owner_id="user-a",
    )

    assert disconnected["status"] == "disconnected"
    assert not Path(captured["CODEX_HOME"]).parent.exists()
    assert store.active_provider_lease(account["account_id"], "default") is None
    assert lease["lease_id"]


def test_provider_setup_guidance_extracts_only_reviewed_provider_destinations():
    codex = _provider_setup_guidance(
        "codex",
        """
Follow these steps to sign in with ChatGPT using device code authorization:
1. Open https://auth.openai.com/codex/device
2. You will need the one-time code shown in your browser.
3. Enter this one-time code: TEST-CODE1
""",
    )
    assert codex == {
        "provider": "codex",
        "setup_url": "https://auth.openai.com/codex/device",
        "setup_code": "TEST-CODE1",
        "help_url": "https://chatgpt.com/#settings/Security",
    }

    malicious = _provider_setup_guidance(
        "codex",
        "Open https://attacker.example/device and enter one-time code EVIL-CODE",
    )
    assert malicious["setup_url"] == ""
    assert malicious["setup_code"] == ""

    claude = _provider_setup_guidance(
        "claude",
        "Open https://claude.ai/oauth/authorize?code=true to continue",
    )
    assert claude == {
        "provider": "claude",
        "setup_url": "https://claude.ai/oauth/authorize?code=true",
        "setup_code": "",
        "help_url": "",
    }


def test_provider_disconnect_preserves_private_home_when_native_logout_fails(tmp_path, monkeypatch):
    store = ControlPlaneStore(str(tmp_path / "runtime.db"))
    account = store.create_provider_account(
        tenant_id="tenant-a",
        owner_id="user-a",
        provider="codex",
        label="Personal Codex",
        auth_method="subscription",
        platform_support="supported",
        secret_locator="native-home://auto",
        status="ready",
    )
    setup = ProviderSetupManager(store=store, home_root=tmp_path / "provider-homes")
    account_home = setup.homes.ensure_home(
        tenant_id="tenant-a",
        owner_id="user-a",
        account_id=account["account_id"],
        provider="codex",
    )
    monkeypatch.setenv("WPR_CODEX_CLI_PATH", "/synthetic/codex")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args=args[0], returncode=9),
    )

    with pytest.raises(ControlPlaneError, match="private account home was preserved"):
        setup.disconnect(
            account_id=account["account_id"],
            tenant_id="tenant-a",
            owner_id="user-a",
        )

    assert account_home.exists()
    updated = store.get_provider_account(
        account_id=account["account_id"], tenant_id="tenant-a", owner_id="user-a"
    )
    assert updated is not None
    assert updated["status"] == "action_required"
    assert store.active_provider_lease(account["account_id"], "provider-disconnect") is None


def test_disconnected_provider_accounts_can_be_forgotten_and_do_not_exhaust_active_quota(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GLASSHIVE_MAX_PROVIDER_ACCOUNTS_PER_USER", "1")
    store = ControlPlaneStore(str(tmp_path / "runtime.db"))
    disconnected = store.create_provider_account(
        tenant_id="tenant-a",
        owner_id="user-a",
        provider="codex",
        label="Old personal account",
        auth_method="subscription",
        platform_support="supported",
        secret_locator="native-home://auto",
        status="disconnected",
    )

    replacement = store.create_provider_account(
        tenant_id="tenant-a",
        owner_id="user-a",
        provider="codex",
        label="Replacement account",
        auth_method="subscription",
        platform_support="supported",
        secret_locator="native-home://auto",
        status="ready",
    )

    with pytest.raises(ControlPlaneConflict, match="Disconnect the provider account"):
        store.forget_provider_account(
            account_id=replacement["account_id"], tenant_id="tenant-a", owner_id="user-a"
        )
    forgotten = store.forget_provider_account(
        account_id=disconnected["account_id"], tenant_id="tenant-a", owner_id="user-a"
    )
    assert forgotten == {"account_id": disconnected["account_id"], "status": "forgotten"}
    assert store.get_provider_account(
        account_id=disconnected["account_id"], tenant_id="tenant-a", owner_id="user-a"
    ) is None


def test_provider_account_usage_records_only_completed_observations_not_lease_acquisition(tmp_path):
    store = ControlPlaneStore(str(tmp_path / "runtime.db"))
    account = store.create_provider_account(
        tenant_id="tenant-a",
        owner_id="user-a",
        provider="codex",
        label="Personal account",
        auth_method="subscription",
        platform_support="supported",
        secret_locator="native-home://auto",
        status="ready",
    )

    store.acquire_provider_lease(
        account_id=account["account_id"],
        tenant_id="tenant-a",
        owner_id="user-a",
        lane="codex-cli:mission",
        worker_id="wrk_public_safe",
        run_id="run_public_safe",
        ttl_seconds=60,
        now=1_700_000_000,
    )

    observed = store.get_provider_account(
        account_id=account["account_id"], tenant_id="tenant-a", owner_id="user-a"
    )
    assert observed is not None
    assert observed["last_used_at"] is None
    assert observed["observed_runs"] == 0
    assert observed["observed_failures"] == 0
    assert observed["observed_duration_seconds"] == 0
    assert observed["observed_input_tokens"] is None
    assert observed["observed_output_tokens"] is None

    first = store.record_provider_account_usage(
        account_id=account["account_id"],
        tenant_id="tenant-a",
        owner_id="user-a",
        succeeded=True,
        duration_seconds=12.5,
        input_tokens=120,
        output_tokens=30,
        now=1_700_000_010,
    )
    assert first["last_used_at"] == 1_700_000_010
    assert first["observed_runs"] == 1
    assert first["observed_failures"] == 0
    assert first["observed_duration_seconds"] == 12.5
    assert first["observed_input_tokens"] == 120
    assert first["observed_output_tokens"] == 30

    observed = store.record_provider_account_usage(
        account_id=account["account_id"],
        tenant_id="tenant-a",
        owner_id="user-a",
        succeeded=False,
        duration_seconds=3,
        now=1_700_000_020,
    )
    assert observed["observed_runs"] == 2
    assert observed["observed_failures"] == 1
    assert observed["observed_duration_seconds"] == 15.5
    assert observed["observed_input_tokens"] == 120
    assert observed["observed_output_tokens"] == 30
    assert "secret_locator" not in observed


@pytest.mark.parametrize(
    ("duration_seconds", "input_tokens", "output_tokens"),
    [(-1, None, None), (float("inf"), None, None), (1, -1, None), (1, None, True)],
)
def test_provider_account_usage_rejects_untruthful_or_invalid_observations(
    tmp_path, duration_seconds, input_tokens, output_tokens
):
    store = ControlPlaneStore(str(tmp_path / "runtime.db"))
    account = store.create_provider_account(
        tenant_id="tenant-a",
        owner_id="user-a",
        provider="codex",
        label="Personal account",
        auth_method="subscription",
        platform_support="supported",
        secret_locator="native-home://auto",
        status="ready",
    )

    with pytest.raises(ControlPlaneError, match="observed"):
        store.record_provider_account_usage(
            account_id=account["account_id"],
            tenant_id="tenant-a",
            owner_id="user-a",
            succeeded=True,
            duration_seconds=duration_seconds,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    unchanged = store.get_provider_account(
        account_id=account["account_id"], tenant_id="tenant-a", owner_id="user-a"
    )
    assert unchanged["observed_runs"] == 0


def test_control_plane_v2_migrates_provider_account_outcome_and_duration_counters(tmp_path):
    database = tmp_path / "runtime.db"
    with sqlite3.connect(database) as conn:
        require_compatible_schema(conn, component="control_plane", target_version=2)
        conn.execute(
            """
            CREATE TABLE provider_accounts (
                account_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                label TEXT NOT NULL,
                auth_method TEXT NOT NULL,
                platform_support TEXT NOT NULL,
                is_default INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                last_verified_at REAL,
                last_used_at REAL,
                reconnect_reason TEXT NOT NULL DEFAULT '',
                secret_locator TEXT NOT NULL,
                observed_runs INTEGER NOT NULL DEFAULT 0,
                observed_input_tokens INTEGER,
                observed_output_tokens INTEGER,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        record_schema_version(conn, component="control_plane", version=2)

    ControlPlaneStore(str(database))

    with sqlite3.connect(database) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(provider_accounts)")}
        assert {"observed_failures", "observed_duration_seconds"}.issubset(columns)
        assert require_compatible_schema(
            conn, component="control_plane", target_version=4
        ) == 4


def test_legacy_credential_cleanup_failure_migrates_to_structured_recovery(tmp_path):
    database = tmp_path / "runtime.db"
    with sqlite3.connect(database) as conn:
        conn.execute(
            """
            CREATE TABLE provider_accounts (
                account_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, owner_id TEXT NOT NULL,
                provider TEXT NOT NULL, label TEXT NOT NULL, auth_method TEXT NOT NULL,
                platform_support TEXT NOT NULL, is_default INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL, last_verified_at REAL, last_used_at REAL,
                reconnect_reason TEXT NOT NULL DEFAULT '', secret_locator TEXT NOT NULL,
                observed_runs INTEGER NOT NULL DEFAULT 0, observed_failures INTEGER NOT NULL DEFAULT 0,
                observed_duration_seconds REAL NOT NULL DEFAULT 0, observed_input_tokens INTEGER,
                observed_output_tokens INTEGER, created_at REAL NOT NULL, updated_at REAL NOT NULL
            )
            """
        )
        values = (
            "acct_cleanup", "tenant-a", "user-a", "codex", "Personal Codex", "subscription",
            "supported", 0, "action_required", None, None,
            "Provider credential cleanup failed; operator cleanup is required",
            "native-home://acct_cleanup", 1, 0, 1.0, None, None, 1.0, 1.0,
        )
        conn.execute("INSERT INTO provider_accounts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values)
        conn.execute(
            "INSERT INTO provider_accounts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "acct_other", "tenant-a", "user-b", "codex", "Other", "subscription",
                "supported", 0, "action_required", None, None, "Different reason",
                "native-home://acct_other", 0, 0, 0.0, None, None, 1.0, 1.0,
            ),
        )

    store = ControlPlaneStore(str(database))

    migrated = store.get_provider_account(
        account_id="acct_cleanup", tenant_id="tenant-a", owner_id="user-a"
    )
    untouched = store.get_provider_account(
        account_id="acct_other", tenant_id="tenant-a", owner_id="user-b"
    )
    assert migrated["recovery_code"] == "credential_cleanup_failed"
    assert untouched["recovery_code"] == ""


def test_transient_provider_status_preserves_recovery_until_verified(tmp_path):
    store = ControlPlaneStore(str(tmp_path / "runtime.db"))
    account = store.create_provider_account(
        tenant_id="tenant-a", owner_id="user-a", provider="codex", label="Personal Codex",
        auth_method="subscription", platform_support="supported",
        secret_locator="native-home://auto", status="action_required",
    )
    store.update_provider_account_status(
        account_id=account["account_id"], tenant_id="tenant-a", owner_id="user-a",
        status="action_required", reconnect_reason="Credentials need safe repair",
        recovery_code="credential_cleanup_failed",
    )

    connecting = store.update_provider_account_status(
        account_id=account["account_id"], tenant_id="tenant-a", owner_id="user-a",
        status="connecting",
    )
    assert connecting["recovery_code"] == "credential_cleanup_failed"

    ready = store.update_provider_account_status(
        account_id=account["account_id"], tenant_id="tenant-a", owner_id="user-a",
        status="ready", verified=True, recovery_code="",
    )
    assert ready["recovery_code"] == ""


def test_connection_check_repairs_under_verify_lease_without_new_sign_in(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENABLE_CODEX_PERSONAL_ACCOUNTS", "true")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION", "per_worker_container")
    store = ControlPlaneStore(str(tmp_path / "runtime.db"))
    account = store.create_provider_account(
        tenant_id="tenant-a", owner_id="user-a", provider="codex", label="Personal Codex",
        auth_method="subscription", platform_support="supported",
        secret_locator="native-home://auto", status="action_required",
    )
    store.update_provider_account_status(
        account_id=account["account_id"], tenant_id="tenant-a", owner_id="user-a",
        status="action_required", reconnect_reason="Credentials need safe repair",
        recovery_code="credential_cleanup_failed",
    )
    reconciled: list[Path] = []
    heartbeat_calls: list[str] = []
    original_heartbeat = store.heartbeat_provider_lease

    def heartbeat(**kwargs):
        heartbeat_calls.append(str(kwargs["lease_id"]))
        return original_heartbeat(**kwargs)

    monkeypatch.setattr(store, "heartbeat_provider_lease", heartbeat)

    def reconcile(account_home: Path) -> None:
        assert store.active_provider_lease(account["account_id"], "provider-verify") is not None
        if account_home.exists():
            wrapper = account_home / "codex" / "tmp" / "provider-wrapper"
            assert wrapper.is_symlink()
            wrapper.unlink()
        else:
            assert not reconciled
        reconciled.append(account_home)

    setup = ProviderSetupManager(
        store=store,
        home_root=tmp_path / "provider-homes",
        reconcile_provider_account_binding=reconcile,
    )
    def verify(**kwargs) -> bool:
        account_home = Path(kwargs["account_home"])
        wrapper = account_home / "codex" / "tmp" / "provider-wrapper"
        wrapper.parent.mkdir(parents=True)
        wrapper.symlink_to(tmp_path / "outside-wrapper")
        return True

    monkeypatch.setattr(setup, "_verify", verify)

    result = setup.status(
        account_id=account["account_id"], tenant_id="tenant-a", owner_id="user-a"
    )

    assert result["status"] == "ready"
    assert len(reconciled) == 2
    assert len(heartbeat_calls) >= 2
    assert store.active_provider_lease(account["account_id"], "provider-verify") is None
    recovered = store.get_provider_account(
        account_id=account["account_id"], tenant_id="tenant-a", owner_id="user-a"
    )
    assert recovered["recovery_code"] == ""


def test_connection_check_renews_lease_while_reconcile_is_in_flight(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENABLE_CODEX_PERSONAL_ACCOUNTS", "true")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION", "per_worker_container")
    monkeypatch.setattr(
        provider_accounts_module,
        "PROVIDER_VERIFY_HEARTBEAT_INTERVAL_SECONDS",
        0.01,
    )
    store = ControlPlaneStore(str(tmp_path / "runtime.db"))
    account = store.create_provider_account(
        tenant_id="tenant-a", owner_id="user-a", provider="codex", label="Personal Codex",
        auth_method="subscription", platform_support="supported",
        secret_locator="native-home://auto", status="action_required",
    )
    store.update_provider_account_status(
        account_id=account["account_id"], tenant_id="tenant-a", owner_id="user-a",
        status="action_required", reconnect_reason="Credentials need safe repair",
        recovery_code="credential_cleanup_failed",
    )
    reconcile_started = threading.Event()
    release_reconcile = threading.Event()
    background_heartbeat = threading.Event()
    heartbeat_calls = 0
    original_heartbeat = store.heartbeat_provider_lease

    def heartbeat(**kwargs):
        nonlocal heartbeat_calls
        heartbeat_calls += 1
        renewed = original_heartbeat(**kwargs)
        if heartbeat_calls >= 2:
            background_heartbeat.set()
        return renewed

    monkeypatch.setattr(store, "heartbeat_provider_lease", heartbeat)

    def reconcile(_account_home: Path) -> None:
        if not reconcile_started.is_set():
            reconcile_started.set()
            assert release_reconcile.wait(timeout=2)

    setup = ProviderSetupManager(
        store=store,
        home_root=tmp_path / "provider-homes",
        reconcile_provider_account_binding=reconcile,
    )
    monkeypatch.setattr(setup, "_verify", lambda **_kwargs: True)
    result: dict[str, object] = {}
    failure: list[BaseException] = []

    def run_status() -> None:
        try:
            result.update(
                setup.status(
                    account_id=account["account_id"],
                    tenant_id="tenant-a",
                    owner_id="user-a",
                )
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            failure.append(exc)

    status_thread = threading.Thread(target=run_status)
    status_thread.start()
    try:
        assert reconcile_started.wait(timeout=2)
        assert background_heartbeat.wait(timeout=2)
        with pytest.raises(ControlPlaneConflict, match="already in use"):
            store.acquire_provider_lease(
                account_id=account["account_id"],
                tenant_id="tenant-a",
                owner_id="user-a",
                lane="mission",
                worker_id="worker-competing",
                run_id="run-competing",
                ttl_seconds=60,
                allowed_statuses=("action_required",),
                required_recovery_code="credential_cleanup_failed",
            )
    finally:
        release_reconcile.set()
        status_thread.join(timeout=2)

    assert not status_thread.is_alive()
    assert failure == []
    assert result["status"] == "ready"
    assert heartbeat_calls >= 2
    assert store.active_provider_lease(account["account_id"], "provider-verify") is None


def test_connection_check_lease_loss_quarantines_and_releases(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENABLE_CODEX_PERSONAL_ACCOUNTS", "true")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION", "per_worker_container")
    store = ControlPlaneStore(str(tmp_path / "runtime.db"))
    account = store.create_provider_account(
        tenant_id="tenant-a", owner_id="user-a", provider="codex", label="Personal Codex",
        auth_method="subscription", platform_support="supported",
        secret_locator="native-home://auto", status="action_required",
    )
    store.update_provider_account_status(
        account_id=account["account_id"], tenant_id="tenant-a", owner_id="user-a",
        status="action_required", reconnect_reason="Credentials need safe repair",
        recovery_code="credential_cleanup_failed",
    )
    setup = ProviderSetupManager(
        store=store,
        home_root=tmp_path / "provider-homes",
        reconcile_provider_account_binding=lambda _account_home: None,
    )
    monkeypatch.setattr(setup, "_verify", lambda **_kwargs: True)
    original_heartbeat = store.heartbeat_provider_lease
    heartbeat_calls = 0

    def lose_final_heartbeat(**kwargs):
        nonlocal heartbeat_calls
        heartbeat_calls += 1
        if heartbeat_calls > 1:
            raise ControlPlaneError("Synthetic lease loss")
        return original_heartbeat(**kwargs)

    monkeypatch.setattr(store, "heartbeat_provider_lease", lose_final_heartbeat)

    with pytest.raises(ControlPlaneError, match="Synthetic lease loss"):
        setup.status(
            account_id=account["account_id"], tenant_id="tenant-a", owner_id="user-a"
        )

    assert store.active_provider_lease(account["account_id"], "provider-verify") is None
    quarantined = store.get_provider_account(
        account_id=account["account_id"], tenant_id="tenant-a", owner_id="user-a"
    )
    assert quarantined["status"] == "action_required"
    assert quarantined["recovery_code"] == "credential_cleanup_failed"


def test_setup_repair_failure_quarantines_account_and_releases_lease(tmp_path, monkeypatch):
    cli = tmp_path / "synthetic-codex"
    cli.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    cli.chmod(0o700)
    monkeypatch.setenv("WPR_CODEX_CLI_PATH", str(cli))
    monkeypatch.setenv("GLASSHIVE_ENABLE_CODEX_PERSONAL_ACCOUNTS", "true")
    monkeypatch.setenv("GLASSHIVE_PROVIDER_ACCOUNT_ISOLATION", "per_worker_container")
    store = ControlPlaneStore(str(tmp_path / "runtime.db"))
    account = store.create_provider_account(
        tenant_id="tenant-a", owner_id="user-a", provider="codex", label="Personal Codex",
        auth_method="subscription", platform_support="supported",
        secret_locator="native-home://auto", status="ready",
    )

    def reject_repair(account_home: Path) -> None:
        assert not account_home.exists()
        assert store.active_provider_lease(account["account_id"], "provider-setup") is not None
        raise ControlPlaneError("Synthetic safe repair failure")

    setup = ProviderSetupManager(
        store=store,
        home_root=tmp_path / "provider-homes",
        reconcile_provider_account_binding=reject_repair,
    )

    with pytest.raises(ControlPlaneError, match="safe repair failure"):
        setup.start(account_id=account["account_id"], tenant_id="tenant-a", owner_id="user-a")

    assert store.active_provider_lease(account["account_id"], "provider-setup") is None
    quarantined = store.get_provider_account(
        account_id=account["account_id"], tenant_id="tenant-a", owner_id="user-a"
    )
    assert quarantined["status"] == "action_required"
    assert quarantined["recovery_code"] == "credential_cleanup_failed"


@pytest.mark.parametrize("unsafe_kind", ["symlink", "hardlink", "fifo"])
def test_provider_home_tightening_rejects_unsafe_entries(tmp_path, unsafe_kind):
    manager = ProviderAccountHomeManager(tmp_path / "provider-homes")
    account_home = manager.ensure_home(
        tenant_id="tenant-a", owner_id="user-a", account_id="acct_safe", provider="codex"
    )
    unsafe = account_home / "codex" / "unsafe"
    target = tmp_path / "outside"
    target.write_text("synthetic", encoding="utf-8")
    if unsafe_kind == "symlink":
        unsafe.symlink_to(target)
    elif unsafe_kind == "hardlink":
        os.link(target, unsafe)
    else:
        os.mkfifo(unsafe)

    with pytest.raises(ControlPlaneError, match="unsafe"):
        manager.tighten_permissions(account_home=account_home)

    assert target.read_text(encoding="utf-8") == "synthetic"


def test_provider_setup_fails_closed_when_deployment_has_not_proven_support(tmp_path):
    store = ControlPlaneStore(str(tmp_path / "runtime.db"))
    account = store.create_provider_account(
        tenant_id="tenant-a",
        owner_id="user-a",
        provider="codex",
        label="Personal Codex",
        auth_method="subscription",
        platform_support="proof_required",
        secret_locator="native-home://auto",
    )
    setup = ProviderSetupManager(store=store, home_root=tmp_path / "provider-homes")

    with pytest.raises(ControlPlaneError, match="not supported"):
        setup.start(account_id=account["account_id"], tenant_id="tenant-a", owner_id="user-a")
    with pytest.raises(ControlPlaneError, match="not found"):
        setup.status(account_id=account["account_id"], tenant_id="tenant-a", owner_id="user-b")


def test_provider_setup_shutdown_terminates_login_process_and_releases_lock(
    tmp_path, monkeypatch
):
    cli = tmp_path / "synthetic-codex-wait"
    cli.write_text(
        "#!/bin/sh\nif [ \"$1 $2\" = \"login --device-auth\" ]; then sleep 60; fi\n",
        encoding="utf-8",
    )
    cli.chmod(0o700)
    monkeypatch.setenv("WPR_CODEX_CLI_PATH", str(cli))
    monkeypatch.setenv("GLASSHIVE_ENABLE_CODEX_PERSONAL_ACCOUNTS", "true")
    store = ControlPlaneStore(str(tmp_path / "runtime.db"))
    account = store.create_provider_account(
        tenant_id="tenant-a",
        owner_id="user-a",
        provider="codex",
        label="Personal Codex",
        auth_method="subscription",
        platform_support="supported",
        secret_locator="native-home://auto",
    )
    setup = ProviderSetupManager(store=store, home_root=tmp_path / "provider-homes")

    setup.start(
        account_id=account["account_id"], tenant_id="tenant-a", owner_id="user-a"
    )
    process = setup._sessions[account["account_id"]].process
    assert process.poll() is None
    with pytest.raises(ControlPlaneConflict, match="already in use"):
        store.acquire_provider_lease(
            account_id=account["account_id"],
            tenant_id="tenant-a",
            owner_id="user-a",
            lane="codex-cli:mission",
            worker_id="wrk-overlap",
            run_id="run-overlap",
            ttl_seconds=60,
        )

    setup.shutdown()

    assert process.poll() is not None
    assert setup._sessions == {}
    updated = store.get_provider_account_record(
        account_id=account["account_id"], tenant_id="tenant-a", owner_id="user-a"
    )
    assert updated["status"] == "action_required"


def test_workspace_capability_revoke_restores_prior_bundle_and_refuses_drift(tmp_path):
    database = tmp_path / "runtime.db"
    store = ControlPlaneStore(str(database))
    _create_workspace_record(database)
    item = register_manifest(
        store,
        library_manifest(
            stable_id="skill.synthetic.safe-revoke",
            scopes=["documents:read"],
        ),
    )

    def enable() -> dict:
        pending = store.create_pending_change(
            tenant_id="tenant-a",
            owner_id="user-a",
            change_type="library_enable",
            target_id="wrk_public_safe",
            payload={"library_id": item["library_id"]},
            ttl_seconds=300,
        )
        return store.confirm_pending_change(
            change_id=pending["change_id"],
            tenant_id="tenant-a",
            owner_id="user-a",
            confirmation_token=pending["confirmation_token"],
        )["applied"]

    grant = enable()
    revoked = store.revoke_workspace_grant(
        grant_id=grant["grant_id"],
        tenant_id="tenant-a",
        owner_id="user-a",
        worker_id="wrk_public_safe",
    )
    assert revoked["revoked_at"] is not None
    assert store.list_workspace_grants(
        tenant_id="tenant-a", owner_id="user-a", worker_id="wrk_public_safe"
    ) == []
    with sqlite3.connect(database) as conn:
        assert json.loads(conn.execute(
            "SELECT bootstrap_bundle_json FROM workers WHERE worker_id = 'wrk_public_safe'"
        ).fetchone()[0]) == {}

    drifted_grant = enable()
    with sqlite3.connect(database) as conn:
        conn.execute(
            "UPDATE workers SET bootstrap_bundle_json = ? WHERE worker_id = 'wrk_public_safe'",
            (json.dumps({"user_change": True}),),
        )
    with pytest.raises(ControlPlaneConflict, match="configuration changed"):
        store.revoke_workspace_grant(
            grant_id=drifted_grant["grant_id"],
            tenant_id="tenant-a",
            owner_id="user-a",
            worker_id="wrk_public_safe",
        )
