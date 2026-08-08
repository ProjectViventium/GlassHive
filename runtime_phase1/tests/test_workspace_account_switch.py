from __future__ import annotations

from fastapi.testclient import TestClient

from workers_projects_runtime.api import create_app
from workers_projects_runtime.control_plane import ControlPlaneStore


def _workspace(client: TestClient, *, profile: str = "codex-cli") -> dict:
    project = client.post(
        "/v1/projects",
        json={"owner_id": "demo-owner", "title": "Account switch desk", "goal": "Synthetic QA"},
    ).json()
    return client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={
            "owner_id": "demo-owner",
            "name": "Account switch desk",
            "role": "main",
            "profile": profile,
            "workspace_kind": "named",
            "start_synchronously": False,
        },
    ).json()


def _ready_account(store: ControlPlaneStore, *, provider: str = "codex") -> dict:
    account = store.create_provider_account(
        tenant_id="local",
        owner_id="demo-owner",
        provider=provider,
        label="Synthetic private account",
        auth_method="subscription",
        platform_support="supported",
        secret_locator="native-home://auto",
    )
    return store.update_provider_account_status(
        account_id=str(account["account_id"]),
        tenant_id="local",
        owner_id="demo-owner",
        status="ready",
        verified=True,
    )


def test_workspace_account_switch_is_confirmed_owner_scoped_and_future_only(tmp_path):
    database = tmp_path / "runtime.db"
    client = TestClient(create_app(db_path=str(database), runtime_backend="stub"))
    workspace = _workspace(client)
    store = ControlPlaneStore(str(database))
    account = _ready_account(store)

    prepared = client.post(
        "/v1/pending-changes",
        json={
            "change_type": "workspace_provider_account",
            "target_id": workspace["worker_id"],
            "payload": {
                "policy": "personal_required",
                "account_id": account["account_id"],
            },
        },
    )
    assert prepared.status_code == 201
    assert prepared.json()["payload"]["account_snapshot"]["label"] == "Synthetic private account"

    confirmed = client.post(
        f"/v1/pending-changes/{prepared.json()['change_id']}/confirm",
        json={"confirmation_token": prepared.json()["confirmation_token"]},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["applied"] == {
        "worker_id": workspace["worker_id"],
        "provider_account": {
            "policy": "personal_required",
            "account_id": account["account_id"],
        },
        "account": {
            "account_id": account["account_id"],
            "provider": "codex",
            "label": "Synthetic private account",
        },
        "applies_to": "future_runs",
    }
    catalog = client.get("/v1/workspaces?kind=named").json()["items"]
    selected = next(item for item in catalog if item["worker_id"] == workspace["worker_id"])
    assert selected["provider_account"] == {
        "policy": "personal_required",
        "account_id": account["account_id"],
    }
    assert "bootstrap_bundle_json" not in selected


def test_workspace_account_switch_revalidates_profile_status_and_active_lease(tmp_path):
    database = tmp_path / "runtime.db"
    client = TestClient(create_app(db_path=str(database), runtime_backend="stub"))
    workspace = _workspace(client)
    store = ControlPlaneStore(str(database))
    wrong_provider = _ready_account(store, provider="claude")
    mismatch = client.post(
        "/v1/pending-changes",
        json={
            "change_type": "workspace_provider_account",
            "target_id": workspace["worker_id"],
            "payload": {
                "policy": "personal_required",
                "account_id": wrong_provider["account_id"],
            },
        },
    )
    assert mismatch.status_code == 400
    assert "profile" in mismatch.json()["detail"].lower()

    account = _ready_account(store)
    prepared = client.post(
        "/v1/pending-changes",
        json={
            "change_type": "workspace_provider_account",
            "target_id": workspace["worker_id"],
            "payload": {"policy": "personal_required", "account_id": account["account_id"]},
        },
    ).json()
    lease = store.acquire_provider_lease(
        account_id=str(account["account_id"]),
        tenant_id="local",
        owner_id="demo-owner",
        lane="mission",
        worker_id=str(workspace["worker_id"]),
        run_id="run_synthetic_active",
        ttl_seconds=60,
    )
    blocked = client.post(
        f"/v1/pending-changes/{prepared['change_id']}/confirm",
        json={"confirmation_token": prepared["confirmation_token"]},
    )
    assert blocked.status_code == 409
    assert "finish" in blocked.json()["detail"].lower()
    store.release_provider_lease(
        lease_id=str(lease["lease_id"]), tenant_id="local", owner_id="demo-owner"
    )

    store.update_provider_account_status(
        account_id=str(account["account_id"]),
        tenant_id="local",
        owner_id="demo-owner",
        status="action_required",
        reconnect_reason="Synthetic reconnect",
    )
    stale = client.post(
        f"/v1/pending-changes/{prepared['change_id']}/confirm",
        json={"confirmation_token": prepared["confirmation_token"]},
    )
    assert stale.status_code == 409
    assert "reconnected" in stale.json()["detail"].lower()


def test_workspace_account_switch_does_not_leak_cross_owner_accounts(tmp_path, monkeypatch):
    monkeypatch.setenv("WPR_API_TOKEN", "service-token")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "first_party_assertion")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-public-safe")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "synthetic-signed-link-secret")
    database = tmp_path / "runtime.db"
    client = TestClient(create_app(db_path=str(database), runtime_backend="stub"))
    headers_a = {
        "Authorization": "Bearer service-token",
        "X-Viventium-Tenant-Id": "tenant-public-safe",
        "X-Viventium-User-Id": "user-a",
    }
    headers_b = {**headers_a, "X-Viventium-User-Id": "user-b"}
    project = client.post(
        "/v1/projects",
        headers=headers_a,
        json={"owner_id": "ignored", "title": "Private desk", "goal": "Synthetic QA"},
    ).json()
    workspace = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        headers=headers_a,
        json={"owner_id": "ignored", "name": "Private desk", "role": "main", "profile": "codex-cli"},
    ).json()
    store = ControlPlaneStore(str(database))
    account = store.create_provider_account(
        tenant_id="tenant-public-safe",
        owner_id="user-a",
        provider="codex",
        label="Private A",
        auth_method="subscription",
        platform_support="supported",
        secret_locator="native-home://auto",
        status="ready",
    )
    hidden = client.post(
        "/v1/pending-changes",
        headers=headers_b,
        json={
            "change_type": "workspace_provider_account",
            "target_id": workspace["worker_id"],
            "payload": {"policy": "personal_required", "account_id": account["account_id"]},
        },
    )
    assert hidden.status_code == 404
    assert "private a" not in hidden.text.lower()
    assert str(account["account_id"]) not in hidden.text
