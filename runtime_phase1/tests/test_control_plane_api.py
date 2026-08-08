from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import threading

from fastapi.testclient import TestClient

from workers_projects_runtime.api import create_app
from workers_projects_runtime.control_plane import ControlPlaneStore
from library_test_support import library_manifest, register_manifest


def test_pending_change_metadata_and_activity_are_owner_scoped(tmp_path, monkeypatch):
    monkeypatch.setenv("WPR_API_TOKEN", "service-token")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "first_party_assertion")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-public-safe")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "synthetic-signed-link-secret")
    database = tmp_path / "runtime.db"
    client = TestClient(create_app(db_path=str(database), runtime_backend="stub"))
    user_a = {
        "Authorization": "Bearer service-token",
        "X-Viventium-Tenant-Id": "tenant-public-safe",
        "X-Viventium-User-Id": "user-a",
    }
    user_b = {**user_a, "X-Viventium-User-Id": "user-b"}

    project = client.post(
        "/v1/projects",
        headers=user_a,
        json={"owner_id": "ignored", "title": "Research desk", "goal": "Synthetic scoped QA"},
    ).json()
    workspace = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        headers=user_a,
        json={
            "owner_id": "ignored",
            "name": "Research desk",
            "role": "main",
            "profile": "codex-cli",
            "workspace_kind": "named",
        },
    ).json()
    assert workspace["workspace_kind"] == "named"
    catalog_item = client.get("/v1/workspaces?kind=named", headers=user_a).json()["items"][0]
    assert catalog_item["worker_id"] == workspace["worker_id"]
    assert catalog_item["provider_readiness"] == {
        "readiness": "deployment_managed",
        "policy": "legacy",
    }
    assert catalog_item["capability_readiness"] == {
        "active_grants": 0,
        "unavailable_grants": 0,
        "readiness": "ready",
    }
    assert catalog_item["next_schedule_at"] == ""
    assert catalog_item["schedule_readiness"] == "ready"
    assert {
        "gateway_token",
        "gateway_url",
        "session_key",
        "state_dir",
        "workspace_dir",
        "workspace_root",
        "bootstrap_bundle_json",
        "last_error",
        "owner_id",
        "tenant_id",
    }.isdisjoint(catalog_item)
    renamed = client.patch(
        f"/v1/workspaces/{workspace['worker_id']}",
        headers=user_a,
        json={"name": "Renamed research desk"},
    )
    duplicated = client.post(
        f"/v1/workspaces/{workspace['worker_id']}/duplicate",
        headers=user_a,
        json={"idempotency_key": "duplicate-owner-scope-1", "name": "Research desk copy"},
    )
    assert renamed.status_code == 200
    assert duplicated.status_code == 201
    forbidden_workspace_fields = {
        "gateway_token",
        "gateway_url",
        "session_key",
        "state_dir",
        "workspace_dir",
        "bootstrap_bundle_json",
        "owner_id",
        "tenant_id",
    }
    assert forbidden_workspace_fields.isdisjoint(renamed.json())
    assert forbidden_workspace_fields.isdisjoint(duplicated.json()["workspace"])
    assert duplicated.json()["workspace"]["duplication_report"]["capabilities_requiring_reapproval"] == 0
    library = register_manifest(
        ControlPlaneStore(str(database)),
        library_manifest(stable_id="skill.synthetic.owner-scope", scopes=["documents:read"]),
    )
    pending = client.post(
        "/v1/pending-changes",
        headers=user_a,
        json={
            "change_type": "library_enable",
            "target_id": workspace["worker_id"],
            "payload": {"library_id": library["library_id"], "scopes": ["documents:read"]},
        },
    )
    assert pending.status_code == 201

    connection_prepare = client.post(
        "/v1/pending-changes",
        headers=user_a,
        json={
            "change_type": "workspace_grant",
            "target_id": workspace["worker_id"],
            "payload": {"connection_id": "conn_public_safe"},
        },
    )
    account_prepare = client.post(
        "/v1/pending-changes",
        headers=user_a,
        json={
            "change_type": "workspace_grant",
            "target_id": workspace["worker_id"],
            "payload": {"account_id": "acct_public_safe"},
        },
    )
    assert connection_prepare.status_code == 409
    assert "broker" in connection_prepare.json()["detail"].lower()
    assert account_prepare.status_code == 409
    assert "execution policy" in account_prepare.json()["detail"].lower()

    visible = client.get(f"/v1/pending-changes/{pending.json()['change_id']}", headers=user_a)
    hidden = client.get(f"/v1/pending-changes/{pending.json()['change_id']}", headers=user_b)
    activity = client.get("/v1/activity?limit=10", headers=user_a)
    other_activity = client.get("/v1/activity?limit=10", headers=user_b)

    assert visible.status_code == 200
    assert visible.json()["target_id"] == workspace["worker_id"]
    assert "confirmation_token" not in visible.json()
    assert hidden.status_code == 404
    assert activity.json()["items"]
    assert {event["worker_id"] for event in activity.json()["items"]} == {
        workspace["worker_id"],
        duplicated.json()["workspace"]["worker_id"],
    }
    assert all("message" not in event for event in activity.json()["items"])
    assert other_activity.json()["items"] == []


def test_workspace_duplicate_idempotency_is_durable_request_scoped_and_owner_scoped(tmp_path, monkeypatch):
    monkeypatch.setenv("WPR_API_TOKEN", "service-token")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "true")
    monkeypatch.setenv("GLASSHIVE_AUTH_MODE", "first_party_assertion")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_TENANT_ID", "tenant-public-safe")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "synthetic-signed-link-secret")
    database = tmp_path / "runtime.db"
    user_a = {
        "Authorization": "Bearer service-token",
        "X-Viventium-Tenant-Id": "tenant-public-safe",
        "X-Viventium-User-Id": "user-a",
    }
    user_b = {**user_a, "X-Viventium-User-Id": "user-b"}

    with TestClient(create_app(db_path=str(database), runtime_backend="stub")) as client:
        def create_source(headers, title):
            project = client.post(
                "/v1/projects",
                headers=headers,
                json={"owner_id": "ignored", "title": title, "goal": "Synthetic duplicate QA"},
            ).json()
            return client.post(
                f"/v1/projects/{project['project_id']}/workers",
                headers=headers,
                json={
                    "owner_id": "ignored",
                    "name": title,
                    "role": "main",
                    "profile": "codex-cli",
                    "workspace_kind": "named",
                    "start_synchronously": False,
                },
            ).json()

        source_a = create_source(user_a, "Owner A source")
        source_b = create_source(user_b, "Owner B source")
        missing_key = client.post(
            f"/v1/workspaces/{source_a['worker_id']}/duplicate",
            headers=user_a,
            json={"name": "Owner A branch"},
        )
        short_key = client.post(
            f"/v1/workspaces/{source_a['worker_id']}/duplicate",
            headers=user_a,
            json={"idempotency_key": "short", "name": "Owner A branch"},
        )
        first = client.post(
            f"/v1/workspaces/{source_a['worker_id']}/duplicate",
            headers=user_a,
            json={"idempotency_key": "duplicate-shared-key-1", "name": "Owner A branch"},
        )

    assert missing_key.status_code == 422
    assert short_key.status_code == 422
    assert first.status_code == 201
    assert first.json()["idempotent_replay"] is False

    with TestClient(create_app(db_path=str(database), runtime_backend="stub")) as restarted:
        replay = restarted.post(
            f"/v1/workspaces/{source_a['worker_id']}/duplicate",
            headers=user_a,
            json={"idempotency_key": "duplicate-shared-key-1", "name": "Owner A branch"},
        )
        conflict = restarted.post(
            f"/v1/workspaces/{source_a['worker_id']}/duplicate",
            headers=user_a,
            json={"idempotency_key": "duplicate-shared-key-1", "name": "Different branch"},
        )
        other_owner = restarted.post(
            f"/v1/workspaces/{source_b['worker_id']}/duplicate",
            headers=user_b,
            json={"idempotency_key": "duplicate-shared-key-1", "name": "Owner B branch"},
        )

    assert replay.status_code == 201
    assert replay.json()["idempotent_replay"] is True
    assert replay.json()["project"]["project_id"] == first.json()["project"]["project_id"]
    assert replay.json()["workspace"]["worker_id"] == first.json()["workspace"]["worker_id"]
    assert conflict.status_code == 409
    assert "different workspace duplicate request" in conflict.json()["detail"].lower()
    assert other_owner.status_code == 201
    assert other_owner.json()["workspace"]["worker_id"] != first.json()["workspace"]["worker_id"]


def test_workspace_duplicate_in_progress_retry_does_not_create_a_second_project(tmp_path, monkeypatch):
    app = create_app(db_path=str(tmp_path / "runtime.db"), runtime_backend="stub")
    service = app.state.service
    original_duplicate = service.duplicate_worker
    started = threading.Event()
    release = threading.Event()

    def blocking_duplicate(*args, **kwargs):
        started.set()
        assert release.wait(timeout=5)
        return original_duplicate(*args, **kwargs)

    monkeypatch.setattr(service, "duplicate_worker", blocking_duplicate)
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects",
            json={"owner_id": "demo-owner", "title": "Concurrency source", "goal": "Synthetic QA"},
        ).json()
        source = client.post(
            f"/v1/projects/{project['project_id']}/workers",
            json={
                "owner_id": "demo-owner",
                "name": "Concurrency source",
                "role": "main",
                "profile": "codex-cli",
                "workspace_kind": "named",
                "start_synchronously": False,
            },
        ).json()
        request = {
            "idempotency_key": "duplicate-concurrent-1",
            "name": "Concurrency branch",
        }
        with ThreadPoolExecutor(max_workers=1) as executor:
            first_future = executor.submit(
                client.post,
                f"/v1/workspaces/{source['worker_id']}/duplicate",
                json=request,
            )
            assert started.wait(timeout=5)
            retry = client.post(
                f"/v1/workspaces/{source['worker_id']}/duplicate",
                json=request,
            )
            projects_during_retry = client.get("/v1/projects").json()["items"]
            release.set()
            first = first_future.result(timeout=5)

    assert retry.status_code == 409
    assert "already in progress" in retry.json()["detail"].lower()
    assert len(projects_during_retry) == 2
    assert first.status_code == 201


def test_workspace_duplicate_failure_without_a_workspace_can_retry_without_hidden_projects(tmp_path, monkeypatch):
    app = create_app(db_path=str(tmp_path / "runtime.db"), runtime_backend="stub")
    service = app.state.service
    original_duplicate = service.duplicate_worker
    attempts = 0

    def fail_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("synthetic duplicate failure")
        return original_duplicate(*args, **kwargs)

    monkeypatch.setattr(service, "duplicate_worker", fail_once)
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects",
            json={"owner_id": "demo-owner", "title": "Failure source", "goal": "Synthetic QA"},
        ).json()
        source = client.post(
            f"/v1/projects/{project['project_id']}/workers",
            json={
                "owner_id": "demo-owner",
                "name": "Failure source",
                "role": "main",
                "profile": "codex-cli",
                "workspace_kind": "named",
                "start_synchronously": False,
            },
        ).json()
        request = {"idempotency_key": "duplicate-safe-retry-1", "name": "Recovered branch"}
        failed = client.post(
            f"/v1/workspaces/{source['worker_id']}/duplicate",
            json=request,
        )
        projects_after_failure = client.get("/v1/projects").json()["items"]
        retried = client.post(
            f"/v1/workspaces/{source['worker_id']}/duplicate",
            json=request,
        )
        projects_after_retry = client.get("/v1/projects").json()["items"]

    assert failed.status_code == 409
    assert "safe to retry" in failed.json()["detail"].lower()
    assert len(projects_after_failure) == 1
    assert retried.status_code == 201
    assert retried.json()["workspace"]["name"] == "Recovered branch"
    assert len(projects_after_retry) == 2


def test_workspace_duplicate_project_creation_failure_reuses_preallocated_reservation(tmp_path, monkeypatch):
    database = tmp_path / "runtime.db"
    app = create_app(db_path=str(database), runtime_backend="stub")
    service = app.state.service
    original_create_project = service.create_project
    attempts = 0

    def fail_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("synthetic project creation failure")
        return original_create_project(*args, **kwargs)

    with TestClient(app) as client:
        project = client.post(
            "/v1/projects",
            json={"owner_id": "demo-owner", "title": "Creation source", "goal": "Synthetic QA"},
        ).json()
        source = client.post(
            f"/v1/projects/{project['project_id']}/workers",
            json={
                "owner_id": "demo-owner",
                "name": "Creation source",
                "role": "main",
                "profile": "codex-cli",
                "workspace_kind": "named",
                "start_synchronously": False,
            },
        ).json()
        monkeypatch.setattr(service, "create_project", fail_once)
        request = {"idempotency_key": "duplicate-create-failure-1", "name": "Recovered creation"}
        failed = client.post(f"/v1/workspaces/{source['worker_id']}/duplicate", json=request)
        retried = client.post(f"/v1/workspaces/{source['worker_id']}/duplicate", json=request)
        projects = client.get("/v1/projects").json()["items"]

    assert failed.status_code == 409
    assert "safe to retry" in failed.json()["detail"].lower()
    assert retried.status_code == 201
    assert len(projects) == 2
    with sqlite3.connect(database) as conn:
        reservation = conn.execute(
            """
            SELECT status, project_id FROM workspace_duplications
            WHERE tenant_id = 'local' AND owner_id = 'demo-owner' AND idempotency_key = ?
            """,
            (request["idempotency_key"],),
        ).fetchone()
    assert reservation == ("completed", retried.json()["project"]["project_id"])


def test_workspace_duplicate_failure_after_workspace_creation_rolls_back_and_retries(tmp_path, monkeypatch):
    app = create_app(db_path=str(tmp_path / "runtime.db"), runtime_backend="stub")
    service = app.state.service
    original_copy = service._copy_workspace_contents
    copy_attempts = 0

    def fail_copy_once(*args, **kwargs):
        nonlocal copy_attempts
        copy_attempts += 1
        if copy_attempts == 1:
            raise RuntimeError("synthetic copy failure")
        return original_copy(*args, **kwargs)

    monkeypatch.setattr(service, "_copy_workspace_contents", fail_copy_once)
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects",
            json={"owner_id": "demo-owner", "title": "Failed-state source", "goal": "Synthetic QA"},
        ).json()
        source = client.post(
            f"/v1/projects/{project['project_id']}/workers",
            json={
                "owner_id": "demo-owner",
                "name": "Failed-state source",
                "role": "main",
                "profile": "codex-cli",
                "workspace_kind": "named",
                "start_synchronously": False,
            },
        ).json()
        request = {"idempotency_key": "duplicate-failed-state-1", "name": "Failed branch"}
        failed = client.post(
            f"/v1/workspaces/{source['worker_id']}/duplicate",
            json=request,
        )
        projects_after_failure = client.get("/v1/projects").json()["items"]
        workspaces_after_failure = client.get("/v1/workspaces?kind=named").json()["items"]
        retried = client.post(
            f"/v1/workspaces/{source['worker_id']}/duplicate",
            json=request,
        )
        projects = client.get("/v1/projects").json()["items"]
        workspaces = client.get("/v1/workspaces?kind=named").json()["items"]

    assert failed.status_code == 409
    assert "safe to retry" in failed.json()["detail"].lower()
    assert len(projects_after_failure) == 1
    assert len(workspaces_after_failure) == 1
    assert retried.status_code == 201
    assert retried.json()["idempotent_replay"] is False
    assert copy_attempts == 2
    assert len(projects) == 2
    assert len(workspaces) == 2
    assert next(item for item in workspaces if item["worker_id"] != source["worker_id"])["state"] == "paused"


def test_workspace_duplicate_unsafe_tree_rolls_back_and_same_key_retries(tmp_path):
    app = create_app(db_path=str(tmp_path / "runtime.db"), runtime_backend="stub")
    service = app.state.service
    with TestClient(app) as client:
        project = client.post(
            "/v1/projects",
            json={"owner_id": "demo-owner", "title": "Unsafe source", "goal": "Synthetic QA"},
        ).json()
        source = client.post(
            f"/v1/projects/{project['project_id']}/workers",
            json={
                "owner_id": "demo-owner",
                "name": "Unsafe source",
                "role": "main",
                "profile": "codex-cli",
                "workspace_kind": "named",
                "start_synchronously": False,
            },
        ).json()
        source_workspace = tmp_path / "source-workspace"
        source_workspace.mkdir()
        (source_workspace / "approved.txt").write_text("synthetic")
        outside = tmp_path / "outside.txt"
        outside.write_text("must not copy")
        unsafe_link = source_workspace / "unsafe-link"
        unsafe_link.symlink_to(outside)
        service.store.update_worker(source["worker_id"], workspace_dir=str(source_workspace))

        request = {"idempotency_key": "duplicate-unsafe-retry-1", "name": "Recovered branch"}
        failed = client.post(f"/v1/workspaces/{source['worker_id']}/duplicate", json=request)
        projects_after_failure = client.get("/v1/projects").json()["items"]
        workspaces_after_failure = client.get("/v1/workspaces?kind=named").json()["items"]

        unsafe_link.unlink()
        retried = client.post(f"/v1/workspaces/{source['worker_id']}/duplicate", json=request)
        projects_after_retry = client.get("/v1/projects").json()["items"]
        workspaces_after_retry = client.get("/v1/workspaces?kind=named").json()["items"]

    assert failed.status_code == 409
    assert "safe to retry" in failed.json()["detail"].lower()
    assert [item["project_id"] for item in projects_after_failure] == [source["project_id"]]
    assert [item["worker_id"] for item in workspaces_after_failure] == [source["worker_id"]]
    assert retried.status_code == 201, retried.text
    assert retried.json()["idempotent_replay"] is False
    assert len(projects_after_retry) == 2
    assert len(workspaces_after_retry) == 2
    duplicate_workspace = service.store.get_worker(retried.json()["workspace"]["worker_id"])
    assert duplicate_workspace is not None
    assert Path(str(duplicate_workspace["workspace_dir"]), "approved.txt").read_text() == "synthetic"


def test_stale_workspace_duplicate_recovers_only_proven_completion_and_fails_closed_otherwise(tmp_path):
    database = tmp_path / "runtime.db"
    with TestClient(create_app(db_path=str(database), runtime_backend="stub")) as client:
        project = client.post(
            "/v1/projects",
            json={"owner_id": "demo-owner", "title": "Crash source", "goal": "Synthetic QA"},
        ).json()
        source = client.post(
            f"/v1/projects/{project['project_id']}/workers",
            json={
                "owner_id": "demo-owner",
                "name": "Crash source",
                "role": "main",
                "profile": "codex-cli",
                "workspace_kind": "named",
                "start_synchronously": False,
            },
        ).json()
        request = {"idempotency_key": "duplicate-crash-recovery-1", "name": "Recovered crash branch"}
        first = client.post(
            f"/v1/workspaces/{source['worker_id']}/duplicate",
            json=request,
        )
    assert first.status_code == 201

    with sqlite3.connect(database) as conn:
        conn.execute(
            """
            UPDATE workspace_duplications
            SET status = 'pending', worker_id = NULL, response_json = '{}',
                completed_at = NULL, updated_at = 0
            WHERE tenant_id = 'local' AND owner_id = 'demo-owner' AND idempotency_key = ?
            """,
            (request["idempotency_key"],),
        )

    with TestClient(create_app(db_path=str(database), runtime_backend="stub")) as restarted:
        recovered = restarted.post(
            f"/v1/workspaces/{source['worker_id']}/duplicate",
            json=request,
        )
        projects_after_recovery = restarted.get("/v1/projects").json()["items"]

        ControlPlaneStore(str(database)).reserve_workspace_duplication(
            tenant_id="local",
            owner_id="demo-owner",
            idempotency_key="duplicate-ambiguous-crash-1",
            source_worker_id=source["worker_id"],
            requested_name="Ambiguous crash branch",
        )
        with sqlite3.connect(database) as conn:
            conn.execute(
                """
                UPDATE workspace_duplications SET updated_at = 0
                WHERE tenant_id = 'local' AND owner_id = 'demo-owner' AND idempotency_key = ?
                """,
                ("duplicate-ambiguous-crash-1",),
            )
        ambiguous = restarted.post(
            f"/v1/workspaces/{source['worker_id']}/duplicate",
            json={
                "idempotency_key": "duplicate-ambiguous-crash-1",
                "name": "Ambiguous crash branch",
            },
        )
        projects_after_ambiguous = restarted.get("/v1/projects").json()["items"]

    assert recovered.status_code == 201
    assert recovered.json()["idempotent_replay"] is True
    assert recovered.json()["project"]["project_id"] == first.json()["project"]["project_id"]
    assert recovered.json()["workspace"]["worker_id"] == first.json()["workspace"]["worker_id"]
    assert len(projects_after_recovery) == 2
    assert ambiguous.status_code == 409
    assert "stale workspace duplication" in ambiguous.json()["detail"].lower()
    assert "safe to retry" in ambiguous.json()["detail"].lower()
    assert len(projects_after_ambiguous) == 2


def test_capability_grants_and_provider_disconnect_have_complete_api_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_ENABLE_CODEX_PERSONAL_ACCOUNTS", "true")
    database = tmp_path / "runtime.db"
    client = TestClient(create_app(db_path=str(database), runtime_backend="stub"))
    project = client.post(
        "/v1/projects",
        json={"owner_id": "demo-owner", "title": "Capability desk", "goal": "Synthetic QA"},
    ).json()
    workspace = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={
            "owner_id": "demo-owner",
            "name": "Capability desk",
            "role": "main",
            "profile": "codex-cli",
            "workspace_kind": "named",
            "start_synchronously": False,
        },
    ).json()
    library = register_manifest(
        ControlPlaneStore(str(database)),
        library_manifest(stable_id="skill.synthetic.api-lifecycle", scopes=["documents:read"]),
    )
    pending = client.post(
        "/v1/pending-changes",
        json={
            "change_type": "library_enable",
            "target_id": workspace["worker_id"],
            "payload": {"library_id": library["library_id"]},
        },
    ).json()
    confirmed = client.post(
        f"/v1/pending-changes/{pending['change_id']}/confirm",
        json={"confirmation_token": pending["confirmation_token"]},
    )
    grant_id = confirmed.json()["applied"]["grant_id"]

    grants = client.get(f"/v1/workspaces/{workspace['worker_id']}/capability-grants")
    revoked = client.delete(
        f"/v1/workspaces/{workspace['worker_id']}/capability-grants/{grant_id}"
    )
    assert confirmed.status_code == 200
    assert [item["grant_id"] for item in grants.json()["items"]] == [grant_id]
    assert revoked.status_code == 200
    assert revoked.json()["revoked_at"] is not None

    second_pending = client.post(
        "/v1/pending-changes",
        json={
            "change_type": "library_enable",
            "target_id": workspace["worker_id"],
            "payload": {"library_id": library["library_id"]},
        },
    ).json()
    assert client.post(
        f"/v1/pending-changes/{second_pending['change_id']}/confirm",
        json={"confirmation_token": second_pending["confirmation_token"]},
    ).status_code == 200
    copied = client.post(
        f"/v1/workspaces/{workspace['worker_id']}/duplicate",
        json={"idempotency_key": "duplicate-capability-1", "name": "Capability desk copy"},
    ).json()
    copied_worker_id = copied["workspace"]["worker_id"]
    assert copied["workspace"]["duplication_report"]["capabilities_requiring_reapproval"] == 1
    assert client.get(
        f"/v1/workspaces/{copied_worker_id}/capability-grants"
    ).json()["items"] == []

    account = client.post(
        "/v1/provider-accounts",
        json={
            "provider": "codex",
            "label": "Synthetic personal account",
            "auth_method": "subscription",
            "platform_support": "ignored-by-server",
            "secret_locator": "native-home://auto",
        },
    )
    disconnected = client.post(
        f"/v1/provider-accounts/{account.json()['account_id']}/disconnect"
    )
    assert account.status_code == 201
    assert disconnected.status_code == 200
    assert disconnected.json()["status"] == "disconnected"
    forgotten = client.delete(
        f"/v1/provider-accounts/{account.json()['account_id']}"
    )
    assert forgotten.status_code == 200
    assert forgotten.json()["status"] == "forgotten"
    assert client.get("/v1/provider-accounts").json()["items"] == []


def test_subscription_provider_account_verify_rechecks_native_status(tmp_path, monkeypatch):
    cli = tmp_path / "synthetic-codex"
    cli.write_text(
        "#!/bin/sh\nif [ \"$1 $2\" = \"login status\" ]; then exit 0; fi\nexit 2\n",
        encoding="utf-8",
    )
    cli.chmod(0o700)
    monkeypatch.setenv("WPR_CODEX_CLI_PATH", str(cli))
    monkeypatch.setenv("GLASSHIVE_ENABLE_CODEX_PERSONAL_ACCOUNTS", "true")
    client = TestClient(
        create_app(db_path=str(tmp_path / "runtime.db"), runtime_backend="stub")
    )
    created = client.post(
        "/v1/provider-accounts",
        json={
            "provider": "codex",
            "label": "Synthetic personal account",
            "auth_method": "subscription",
            "platform_support": "ignored-by-server",
            "secret_locator": "native-home://auto",
        },
    )
    assert created.status_code == 201, created.text
    account = created.json()

    verified = client.post(f"/v1/provider-accounts/{account['account_id']}/verify")

    assert verified.status_code == 200
    assert verified.json()["status"] == "ready"
    ControlPlaneStore(str(tmp_path / "runtime.db")).record_provider_account_usage(
        account_id=account["account_id"],
        tenant_id="local",
        owner_id="demo-owner",
        succeeded=False,
        duration_seconds=2.5,
        input_tokens=10,
        output_tokens=4,
    )
    listed = client.get("/v1/provider-accounts").json()["items"][0]
    assert listed["last_verified_at"] is not None
    assert listed["observed_runs"] == 1
    assert listed["observed_failures"] == 1
    assert listed["observed_duration_seconds"] == 2.5
    assert listed["observed_input_tokens"] == 10
    assert listed["observed_output_tokens"] == 4
    assert "secret_locator" not in listed


def test_library_confirmation_is_bound_to_reviewed_immutable_snapshot(tmp_path):
    database = tmp_path / "runtime.db"
    client = TestClient(create_app(db_path=str(database), runtime_backend="stub"))
    project = client.post(
        "/v1/projects",
        json={"owner_id": "demo-owner", "title": "Snapshot desk", "goal": "Synthetic QA"},
    ).json()
    workspace = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={
            "owner_id": "demo-owner",
            "name": "Snapshot desk",
            "role": "main",
            "profile": "codex-cli",
            "workspace_kind": "named",
            "start_synchronously": False,
        },
    ).json()
    library = register_manifest(
        ControlPlaneStore(str(database)),
        library_manifest(
            stable_id="skill.synthetic.snapshot",
            scopes=["documents:read"],
            files=[{"path": "SKILL.md", "content": "reviewed"}],
            label="Reviewed synthetic skill",
        ),
    )
    pending = client.post(
        "/v1/pending-changes",
        json={
            "change_type": "library_enable",
            "target_id": workspace["worker_id"],
            "payload": {"library_id": library["library_id"]},
        },
    ).json()
    reviewed = client.get(f"/v1/pending-changes/{pending['change_id']}").json()
    snapshot = reviewed["payload"]["library_snapshot"]
    assert snapshot["stable_id"] == "skill.synthetic.snapshot"
    assert snapshot["display_label"] == "Reviewed synthetic skill"
    assert reviewed["payload"]["scopes"] == ["documents:read"]

    changed_manifest = {
        "label": "Changed after review",
        "activation": {
            "type": "bootstrap_bundle",
            "bundle": {"files": [{"path": "SKILL.md", "content": "changed"}]},
        },
    }
    with sqlite3.connect(database) as conn:
        conn.execute(
            "UPDATE control_plane_library SET manifest_json = ?, updated_at = updated_at + 1 WHERE library_id = ?",
            (json.dumps(changed_manifest, sort_keys=True, separators=(",", ":")), library["library_id"]),
        )

    confirmed = client.post(
        f"/v1/pending-changes/{pending['change_id']}/confirm",
        json={"confirmation_token": pending["confirmation_token"]},
    )
    assert confirmed.status_code == 409
    assert "changed since review" in confirmed.json()["detail"].lower()
    assert client.get(
        f"/v1/workspaces/{workspace['worker_id']}/capability-grants"
    ).json()["items"] == []
