from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
import uuid

import pytest
from fastapi.testclient import TestClient

from workers_projects_runtime.api import create_app
from workers_projects_runtime.service import (
    ParallelExecutionIsolationError,
    WorkersProjectsService,
)
from workers_projects_runtime.service_assertions import verify_service_assertion
from workers_projects_runtime.signed_links import sign_link_params
from workers_projects_runtime.openclaw_runtime import StubRuntime


API_TOKEN = "test-glasshive-service-token"
ASSERTION_SECRET = "test-service-assertion-secret"
ASSERTION_AUDIENCE = "glasshive-account-api"


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def service_assertion(
    *,
    tenant_id: str = "tenant-a",
    owner_id: str = "owner-a",
    nonce: str | None = None,
    now: int | None = None,
    ttl: int = 60,
    aud: str = ASSERTION_AUDIENCE,
    canonical: bool = True,
    secret: str = ASSERTION_SECRET,
    omit: str | None = None,
) -> str:
    issued_at = int(time.time()) if now is None else int(now)
    claims: dict[str, object] = {
        "v": 1,
        "aud": aud,
        "tenant_id": tenant_id,
        "owner_id": owner_id,
        "iat": issued_at,
        "exp": issued_at + ttl,
        "nonce": nonce or f"nonce_{uuid.uuid4().hex}",
    }
    if omit:
        claims.pop(omit)
    if canonical:
        encoded_json = json.dumps(
            claims,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    else:
        encoded_json = json.dumps(claims, separators=(",", ":")).encode("utf-8")
    payload = _b64url(encoded_json)
    signature = hmac.new(secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).digest()
    return f"{payload}.{_b64url(signature)}"


def account_headers(
    *,
    tenant_id: str = "tenant-a",
    owner_id: str = "owner-a",
    nonce: str | None = None,
    assertion: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {API_TOKEN}",
        "X-Viventium-Service-Assertion": assertion
        or service_assertion(tenant_id=tenant_id, owner_id=owner_id, nonce=nonce),
    }
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def delegation_payload(*, title: str = "Research alpha", instruction: str = "Research alpha deeply") -> dict:
    return {
        "title": title,
        "goal": f"Complete {title}",
        "instruction": instruction,
        "profile": "codex-cli",
        "executionMode": "docker",
        "workerName": f"{title} worker",
        "workerRole": "General intelligent worker",
        "originSurface": "telegram",
        "bootstrapBundle": {
            "context": {"private_path": "/private/example", "secret": "must-not-leak"}
        },
    }


def delegation_payload_with_origin(
    *,
    origin_ref: str = "ghi_synthetic_origin_0001",
    title: str = "Research alpha",
) -> dict:
    payload = delegation_payload(title=title)
    payload["bootstrapBundle"] = {
        **payload["bootstrapBundle"],
        "callbacks": {
            "origin_ref": origin_ref,
            "events_webhook_url": "https://callback.example.invalid/events",
        },
    }
    return payload


def conversation_orchestrator_payload(*, title: str = "Isolated parallel mission") -> dict:
    payload = delegation_payload(title=title)
    broker_url = "http://host.docker.internal:3080/api/viventium/glasshive/capabilities/mcp"
    payload["bootstrapBundle"] = {
        **payload["bootstrapBundle"],
        "viventium_launch_authority": {
            "version": 1,
            "kind": "conversation_orchestrator",
            "execution_mode": "docker",
        },
        "glasshive_capability_broker": {
            "version": 1,
            "status": "pending_admission",
            "name": "glasshive-user-capabilities",
            "url": broker_url,
            "allowed_servers": [],
            "allowed_host_tools": [],
            "scopes": {"content_read": False},
            "projection": "all_user_enabled_policy_gated",
        },
        "claude_project_mcp": {
            "glasshive-user-capabilities": {
                "type": "http",
                "transport": "http",
                "url": broker_url,
                "headers": {
                    "Authorization": "Bearer ${GLASSHIVE_CAPABILITY_BROKER_TOKEN}"
                },
            }
        },
        "codex_config_append": (
            "[mcp_servers.glasshive-user-capabilities]\n"
            f'url = "{broker_url}"\n'
            'bearer_token_env_var = "GLASSHIVE_CAPABILITY_BROKER_TOKEN"'
        ),
        "env": {},
    }
    return payload


@pytest.fixture
def account_client(tmp_path, monkeypatch):
    monkeypatch.setenv("WPR_API_TOKEN", API_TOKEN)
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_SERVICE_ASSERTION_SECRET", ASSERTION_SECRET)
    app = create_app(
        db_path=str(tmp_path / "account-api.sqlite3"),
        runtime_backend="stub",
        runtime=StubRuntime(),
    )
    app.state.service.runtime.isolated_parallel_readiness = lambda: {
        "ready": True,
        "reason": "",
    }
    # Mission acceptance must not wait for provider startup. Keep rows queued so
    # each API assertion can inspect the durable reservation deterministically.
    app.state.service.start_assigned_run = lambda _worker_id: None
    app.state.service._ensure_worker_processor = lambda _worker_id: None
    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": f"Bearer {API_TOKEN}"},
        {
            "Authorization": "Bearer wrong-token",
            "X-Viventium-Service-Assertion": service_assertion(),
        },
    ],
)
def test_account_api_requires_bearer_and_service_assertion(account_client, headers):
    response = account_client.get("/v1/active-work", headers=headers)

    assert response.status_code == 401


@pytest.mark.parametrize(
    "assertion",
    [
        service_assertion(secret="wrong-secret"),
        service_assertion(aud="wrong-audience"),
        service_assertion(ttl=61),
        service_assertion(now=int(time.time()) - 61, ttl=60),
        service_assertion(omit="owner_id"),
        service_assertion(canonical=False),
    ],
)
def test_account_api_rejects_invalid_expired_or_noncanonical_assertions(account_client, assertion):
    response = account_client.get(
        "/v1/active-work",
        headers=account_headers(assertion=assertion),
    )

    assert response.status_code == 401
    assert response.json()["detail"]["code"].startswith("service_assertion_")


def test_mutating_service_assertion_nonce_is_durable_and_one_use(tmp_path, monkeypatch):
    monkeypatch.setenv("WPR_API_TOKEN", API_TOKEN)
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_SERVICE_ASSERTION_SECRET", ASSERTION_SECRET)
    db_path = str(tmp_path / "durable-replay.sqlite3")
    fixed = service_assertion(nonce="nonce_durable_replay")
    headers = account_headers(assertion=fixed, idempotency_key="delegation-durable-replay")

    first_app = create_app(db_path=db_path, runtime_backend="stub", runtime=StubRuntime())
    first_app.state.service.start_assigned_run = lambda _worker_id: None
    first_app.state.service._ensure_worker_processor = lambda _worker_id: None
    with TestClient(first_app) as first_client:
        first = first_client.post("/v1/delegations", headers=headers, json=delegation_payload())
        assert first.status_code == 202

    second_app = create_app(db_path=db_path, runtime_backend="stub", runtime=StubRuntime())
    second_app.state.service.start_assigned_run = lambda _worker_id: None
    second_app.state.service._ensure_worker_processor = lambda _worker_id: None
    with TestClient(second_app) as second_client:
        replay = second_client.post("/v1/delegations", headers=headers, json=delegation_payload())

    assert replay.status_code == 409
    assert replay.json()["detail"]["code"] == "service_assertion_replayed"


def test_delegation_atomically_reserves_one_project_worker_and_run(account_client):
    headers = account_headers(idempotency_key="delegation-alpha")

    response = account_client.post("/v1/delegations", headers=headers, json=delegation_payload())

    assert response.status_code == 202
    body = response.json()
    assert body["workRef"].startswith("work_")
    assert body["state"] == "accepted"
    assert body["actions"] == ["queue", "message", "steer", "pause", "stop"]
    assert body["idempotentReplay"] is False
    with sqlite3.connect(account_client.app.state.store.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM delegations").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM workers").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


def test_conversation_orchestrator_delegation_rejects_host_execution(account_client):
    payload = delegation_payload(title="Isolated parallel mission")
    payload["executionMode"] = "host"
    payload["bootstrapBundle"] = {
        **payload["bootstrapBundle"],
        "viventium_launch_authority": {
            "version": 1,
            "kind": "conversation_orchestrator",
            "execution_mode": "docker",
        },
    }

    rejected = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="parallel-host-must-be-isolated"),
        json=payload,
    )

    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "parallel_execution_isolation_required"
    with sqlite3.connect(account_client.app.state.store.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM delegations").fetchone()[0] == 0


def test_conversation_orchestrator_derives_server_owned_clean_room_policy(
    account_client, monkeypatch
):
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_ISOLATED_PARALLEL_POLICY", "1")

    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="parallel-clean-room-derived"),
        json=conversation_orchestrator_payload(),
    )

    assert accepted.status_code == 202
    with sqlite3.connect(account_client.app.state.store.db_path) as conn:
        conn.row_factory = sqlite3.Row
        worker = conn.execute(
            "SELECT bootstrap_profile, bootstrap_bundle_json, execution_mode FROM workers"
        ).fetchone()
    assert worker is not None
    assert worker["execution_mode"] == "docker"
    assert worker["bootstrap_profile"] == "clean-room"
    persisted_bundle = json.loads(worker["bootstrap_bundle_json"])
    assert persisted_bundle["execution_policy"] == "parallel-clean-room-v1"
    assert persisted_bundle["env"] == {}
    assert set(persisted_bundle["claude_project_mcp"]) == {
        "glasshive-user-capabilities"
    }


def test_parallel_clean_room_policy_cannot_be_replaced_by_a_later_runtime_bundle(
    account_client, monkeypatch
):
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_ISOLATED_PARALLEL_POLICY", "1")
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="parallel-clean-room-immutable"),
        json=conversation_orchestrator_payload(title="Immutable clean room"),
    )
    assert accepted.status_code == 202
    store = account_client.app.state.store
    delegation = store.get_delegation(
        accepted.json()["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )

    with pytest.raises(ParallelExecutionIsolationError, match="immutable"):
        account_client.app.state.service.assign_run(
            delegation["worker_id"],
            "Synthetic follow-up",
            runtime_bundle={"execution_policy": "host-login-v1"},
            start_processor=False,
        )

    worker = store.get_worker(delegation["worker_id"])
    assert json.loads(worker["bootstrap_bundle_json"])["execution_policy"] == (
        "parallel-clean-room-v1"
    )
    assert len(store.list_runs_for_worker(delegation["worker_id"])) == 1


@pytest.mark.parametrize(
    ("bootstrap_profile", "bundle_update"),
    [
        ("codex-host", {}),
        ("claude-host", {}),
        ("host-login", {}),
        ("full-local", {}),
        (
            None,
            {
                "files": [
                    {
                        "scope": "home",
                        "path": ".codex/auth.json",
                        "content": "synthetic-host-auth",
                    }
                ]
            },
        ),
        (
            None,
            {
                "files": [
                    {
                        "scope": "workspace",
                        "path": ".mcp.json",
                        "content": '{"mcpServers":{"caller":{"command":"unsafe"}}}',
                    }
                ]
            },
        ),
        (
            None,
            {"metadata": {"provider": {"api_key": "synthetic-provider-key"}}},
        ),
        (None, {"env": {"OPENAI_API_KEY": "synthetic-caller-provider-key"}}),
        (None, {"provider_credentials": {"api_key": "synthetic-provider-key"}}),
        (
            None,
            {
                "glasshive_capability_broker": {
                    "version": 1,
                    "status": "pending_admission",
                    "name": "glasshive-user-capabilities",
                    "url": "http://host.docker.internal:3080/api/viventium/glasshive/capabilities/mcp",
                    "grant_token": "synthetic-caller-broker-grant",
                },
                "claude_project_mcp": None,
                "codex_config_append": None,
            },
        ),
        (None, {"claude_settings_local": {"permissions": {"allow": ["*"]}}}),
        (
            None,
            {
                "claude_project_mcp": {
                    "caller-mcp": {"command": "synthetic-untrusted-command"}
                }
            },
        ),
        (
            None,
            {
                "codex_config_append": (
                    "[mcp_servers.caller-mcp]\n"
                    'command = "synthetic-untrusted-command"'
                )
            },
        ),
        (None, {"execution_policy": "parallel-clean-room-v1"}),
    ],
    ids=[
        "codex-host-profile",
        "claude-host-profile",
        "host-login-profile",
        "full-local-profile",
        "home-scoped-file",
        "workspace-authority-file",
        "nested-provider-credentials",
        "caller-env",
        "provider-credentials",
        "caller-broker-grant",
        "claude-settings",
        "caller-claude-mcp",
        "caller-codex-mcp",
        "caller-execution-policy",
    ],
)
def test_conversation_orchestrator_rejects_non_clean_room_authority_before_rows(
    account_client,
    monkeypatch,
    bootstrap_profile,
    bundle_update,
):
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_ISOLATED_PARALLEL_POLICY", "1")
    payload = conversation_orchestrator_payload(title="Reject unsafe bootstrap")
    payload["bootstrapBundle"].update(bundle_update)
    if bootstrap_profile is not None:
        payload["bootstrapProfile"] = bootstrap_profile

    rejected = account_client.post(
        "/v1/delegations",
        headers=account_headers(
            idempotency_key=f"parallel-reject-{uuid.uuid4().hex}"
        ),
        json=payload,
    )

    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "parallel_execution_isolation_required"
    if bundle_update == {"env": {"OPENAI_API_KEY": "synthetic-caller-provider-key"}}:
        assert rejected.json()["detail"]["reason"] == "caller_provider_credentials"
    with sqlite3.connect(account_client.app.state.store.db_path) as conn:
        for table in (
            "delegations",
            "projects",
            "workers",
            "runs",
            "events",
            "callback_outbox",
            "host_run_leases",
        ):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_conversation_orchestrator_launch_fails_closed_when_isolation_policy_is_off(
    account_client, monkeypatch
):
    monkeypatch.delenv("VIVENTIUM_GLASSHIVE_ISOLATED_PARALLEL_POLICY", raising=False)
    payload = delegation_payload(title="Policy must be live")
    payload["bootstrapBundle"] = {
        **payload["bootstrapBundle"],
        "viventium_launch_authority": {
            "version": 1,
            "kind": "conversation_orchestrator",
            "execution_mode": "docker",
        },
    }

    rejected = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="parallel-policy-must-be-live"),
        json=payload,
    )

    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "parallel_execution_isolation_required"
    assert account_client.app.state.store.list_all_workers() == []


def test_conversation_orchestrator_launch_fails_closed_while_a_host_mission_exists(
    account_client, monkeypatch
):
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_ISOLATED_PARALLEL_POLICY", "1")
    store = account_client.app.state.store
    project = store.create_project(
        "owner-legacy", "Existing host mission", "Finish first", "codex-cli"
    )
    host_worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner-legacy",
        name="Existing host mission",
        role="worker",
        profile="codex-cli",
        backend="codex-cli",
        runtime="codex-cli",
        model="test",
        execution_mode="host",
    )
    store.create_run(host_worker["worker_id"], project["project_id"], "Still active")
    payload = delegation_payload(title="Blocked until host exits")
    payload["bootstrapBundle"] = {
        **payload["bootstrapBundle"],
        "viventium_launch_authority": {
            "version": 1,
            "kind": "conversation_orchestrator",
            "execution_mode": "docker",
        },
    }

    rejected = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="parallel-existing-host-blocks"),
        json=payload,
    )

    assert rejected.status_code == 409
    assert rejected.json()["detail"]["code"] == "parallel_execution_isolation_required"
    assert len(store.list_all_workers()) == 1


def test_orchestration_capabilities_are_service_asserted_and_report_global_host_gate(
    account_client, monkeypatch
):
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_ISOLATED_PARALLEL_POLICY", "1")

    unauthenticated = account_client.get("/v1/orchestration-capabilities")
    ready = account_client.get(
        "/v1/orchestration-capabilities", headers=account_headers()
    )

    assert unauthenticated.status_code == 401
    assert ready.status_code == 200
    assert ready.json() == {
        "policyVersion": 1,
        "isolatedParallelReady": True,
        "isolatedParallelReason": "",
        "hostMissionsAllowed": False,
        "hostMissionsActive": 0,
    }


def test_orchestration_capabilities_preserve_structured_isolation_failure_reason(
    account_client, monkeypatch
):
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_ISOLATED_PARALLEL_POLICY", "1")
    account_client.app.state.service.runtime.isolated_parallel_readiness = lambda **_kwargs: {
        "ready": False,
        "reason": "parallel_clean_room_network_unconfigured",
    }

    response = account_client.get(
        "/v1/orchestration-capabilities", headers=account_headers()
    )

    assert response.status_code == 200
    assert response.json() == {
        "policyVersion": 1,
        "isolatedParallelReady": False,
        "isolatedParallelReason": "parallel_clean_room_network_unconfigured",
        "hostMissionsAllowed": False,
        "hostMissionsActive": 0,
    }


def test_delegation_idempotency_returns_same_work_and_rejects_changed_request(account_client):
    first = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-same"),
        json=delegation_payload(),
    )
    replay = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-same"),
        json=delegation_payload(),
    )
    changed = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-same"),
        json=delegation_payload(instruction="A materially different instruction"),
    )

    assert first.status_code == 202
    assert replay.status_code == 202
    assert replay.json()["workRef"] == first.json()["workRef"]
    assert replay.json()["idempotentReplay"] is True
    assert changed.status_code == 409
    assert changed.json()["detail"]["code"] == "delegation_idempotency_conflict"
    with sqlite3.connect(account_client.app.state.store.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM delegations").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM workers").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


def test_delegation_replay_returns_committed_receipt_before_mutable_admission_checks(
    account_client, monkeypatch
):
    first = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-replay-before-admission"),
        json=delegation_payload(),
    )
    assert first.status_code == 202

    service = account_client.app.state.service
    mutable_checks: list[str] = []

    def reject(check: str):
        def _raise(*_args, **_kwargs):
            mutable_checks.append(check)
            raise RuntimeError(f"mutable {check} changed after commit")

        return _raise

    monkeypatch.setattr(service, "_ensure_profile_allowed", reject("profile"))
    monkeypatch.setattr(service, "_ensure_runtime_available", reject("runtime"))
    monkeypatch.setattr(service, "_enforce_worker_limits", reject("capacity"))

    replay = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-replay-before-admission"),
        json=delegation_payload(),
    )
    changed = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-replay-before-admission"),
        json=delegation_payload(instruction="Changed after the committed request"),
    )

    assert replay.status_code == 202
    assert replay.json()["workRef"] == first.json()["workRef"]
    assert replay.json()["idempotentReplay"] is True
    assert changed.status_code == 409
    assert changed.json()["detail"]["code"] == "delegation_idempotency_conflict"
    assert mutable_checks == []


def test_delegation_identity_separates_intentional_identical_objectives(account_client):
    goal_digest = hashlib.sha256(b"same objective").hexdigest()

    def identified_payload(*, ordinal: int, key: str) -> dict:
        payload = delegation_payload(title="Same objective")
        payload["bootstrapBundle"]["viventium_delegation_identity"] = {
            "version": 1,
            "idempotency_key": key,
            "goal_digest": goal_digest,
            "call_identity_digest": hashlib.sha256(
                f"provider-call-{ordinal}".encode()
            ).hexdigest(),
            "source_event_id": "telegram-update-synthetic-1",
            "objective_ordinal": ordinal,
        }
        return payload

    first_key = hashlib.sha256(b"objective ordinal zero").hexdigest()
    second_key = hashlib.sha256(b"objective ordinal one").hexdigest()
    first_payload = identified_payload(ordinal=0, key=first_key)
    second_payload = identified_payload(ordinal=1, key=second_key)
    first = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key=first_key),
        json=first_payload,
    )
    second = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key=second_key),
        json=second_payload,
    )
    replay = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key=first_key),
        json=first_payload,
    )

    assert first.status_code == second.status_code == replay.status_code == 202
    assert first.json()["workRef"] != second.json()["workRef"]
    assert replay.json()["workRef"] == first.json()["workRef"]
    assert replay.json()["idempotentReplay"] is True


def test_delegation_identity_header_binding_and_digest_fail_closed(account_client):
    key = hashlib.sha256(b"trusted identity key").hexdigest()
    other_key = hashlib.sha256(b"other identity key").hexdigest()
    payload = delegation_payload(title="Bound identity")
    payload["bootstrapBundle"]["viventium_delegation_identity"] = {
        "version": 1,
        "idempotency_key": key,
        "goal_digest": hashlib.sha256(b"goal A").hexdigest(),
        "call_identity_digest": hashlib.sha256(b"provider-call-bound").hexdigest(),
        "source_event_id": "telegram-update-synthetic-2",
        "objective_ordinal": 0,
    }
    mismatch = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key=other_key),
        json=payload,
    )
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key=key),
        json=payload,
    )
    changed = json.loads(json.dumps(payload))
    changed["bootstrapBundle"]["viventium_delegation_identity"]["goal_digest"] = (
        hashlib.sha256(b"goal B").hexdigest()
    )
    conflict = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key=key),
        json=changed,
    )

    assert mismatch.status_code == 400
    assert mismatch.json()["detail"]["code"] == "delegation_identity_invalid"
    assert accepted.status_code == 202
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "delegation_idempotency_conflict"


def test_account_delegation_accepts_the_core_verified_v2_launch_identity(account_client):
    key = hashlib.sha256(b"core verified v2 identity").hexdigest()
    payload = delegation_payload(title="Verified v2 launch")
    payload["bootstrapBundle"]["viventium_delegation_identity"] = {
        "version": 2,
        "idempotency_key": key,
        "goal_digest": hashlib.sha256(b"goal v2").hexdigest(),
        "launch_payload_digest": hashlib.sha256(b"final enriched launch").hexdigest(),
        "call_identity_digest": hashlib.sha256(b"provider-call-v2").hexdigest(),
        "source_event_id": "web-source-synthetic-v2",
        "objective_ordinal": 0,
    }
    payload["bootstrapBundle"]["viventium_delegation_assertion"] = "a" * 64

    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key=key),
        json=payload,
    )

    assert accepted.status_code == 202
    assert accepted.json()["workRef"].startswith("work_")


def test_delegation_identity_ordinal_is_not_part_of_atomic_conflict_digest(account_client):
    key = hashlib.sha256(b"stable provider call identity key").hexdigest()
    identity = {
        "version": 1,
        "idempotency_key": key,
        "goal_digest": hashlib.sha256(b"stable goal").hexdigest(),
        "call_identity_digest": hashlib.sha256(b"stable provider tool call").hexdigest(),
        "source_event_id": "telegram-update-synthetic-stable-call",
        "objective_ordinal": 0,
    }
    payload = delegation_payload(title="Stable reconstructed call")
    payload["bootstrapBundle"]["viventium_delegation_identity"] = identity
    first = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key=key),
        json=payload,
    )
    reordered = json.loads(json.dumps(payload))
    reordered["bootstrapBundle"]["viventium_delegation_identity"][
        "objective_ordinal"
    ] = 7
    replay = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key=key),
        json=reordered,
    )

    assert first.status_code == replay.status_code == 202
    assert replay.json()["workRef"] == first.json()["workRef"]
    assert replay.json()["idempotentReplay"] is True


def test_active_work_and_detail_are_assertion_scoped_and_do_not_leak_internal_ids(account_client):
    owner_a = account_client.post(
        "/v1/delegations",
        headers=account_headers(owner_id="owner-a", idempotency_key="delegation-owner-a"),
        json=delegation_payload(title="Owner A mission"),
    )
    owner_b = account_client.post(
        "/v1/delegations",
        headers=account_headers(owner_id="owner-b", idempotency_key="delegation-owner-b"),
        json=delegation_payload(title="Owner B mission"),
    )
    assert owner_a.status_code == owner_b.status_code == 202

    roster_headers = account_headers(owner_id="owner-a")
    roster_headers["X-Viventium-Owner-Id"] = "owner-b"
    roster_headers["X-GlassHive-User-Id"] = "owner-b"
    roster = account_client.get("/v1/active-work", headers=roster_headers)
    own_detail = account_client.get(
        f"/v1/work/{owner_a.json()['workRef']}",
        headers=account_headers(owner_id="owner-a"),
    )
    foreign_detail = account_client.get(
        f"/v1/work/{owner_b.json()['workRef']}",
        headers=account_headers(owner_id="owner-a"),
    )

    assert roster.status_code == 200
    assert roster.json()["snapshot"] == "fresh"
    assert roster.json()["overflowCount"] == 0
    assert [item["title"] for item in roster.json()["work"]] == ["Owner A mission"]
    assert own_detail.status_code == 200
    assert foreign_detail.status_code == 404
    assert own_detail.json()["originSurface"] == "telegram"
    assert own_detail.json()["provider"] == "codex"
    assert own_detail.json()["nativeTeam"] is None
    assert own_detail.json()["delivery"] == {
        "state": "pending",
        "unreadTerminal": False,
    }
    serialized = json.dumps({"roster": roster.json(), "detail": own_detail.json()})
    assert "prj_" not in serialized
    assert "wrk_" not in serialized
    assert "run_" not in serialized
    assert "Research alpha deeply" not in serialized
    assert "/private/example" not in serialized
    assert "must-not-leak" not in serialized


def test_active_work_native_team_is_null_until_child_projection_is_observed(account_client):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-native-team"),
        json=delegation_payload(title="Native team mission"),
    ).json()
    store = account_client.app.state.store
    record = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    run_id = record["current_run_id"]
    summary = {
        "observable": True,
        "provider": "codex",
        "sessionId": "thread_synthetic",
        "activeCount": 1,
        "children": [
            {
                "childRef": "child_synthetic",
                "role": "researcher",
                "state": "running",
                "updatedAt": "2099-01-01T00:00:00+00:00",
            },
            {
                "childRef": "child_private_path",
                "role": "/Users/example/private/reviewer@example.com",
                "state": "failed",
                "updatedAt": "2099-01-01T00:00:00+00:00",
            },
        ],
    }
    store.update_run(
        run_id,
        native_capabilities_json=json.dumps(
            {"providerStream": True, "childProjection": False}
        ),
        native_child_summary_json=json.dumps(summary),
    )

    session_only = account_client.get(
        f"/v1/work/{accepted['workRef']}", headers=account_headers()
    )
    assert session_only.status_code == 200
    assert session_only.json()["nativeTeam"] is None

    store.update_run(
        run_id,
        native_capabilities_json=json.dumps(
            {"providerStream": True, "childProjection": True}
        ),
    )
    child_observed = account_client.get(
        f"/v1/work/{accepted['workRef']}", headers=account_headers()
    )
    assert child_observed.status_code == 200
    assert child_observed.json()["nativeTeam"] == {
        "active": 1,
        "total": 2,
        "needsAttention": 1,
        "degraded": False,
        "topology": [
            {"role": "worker", "state": "failed", "count": 1},
            {"role": "researcher", "state": "running", "count": 1},
        ],
        "overflowCount": 0,
    }
    roster = account_client.get("/v1/active-work", headers=account_headers())
    roster_item = next(
        item for item in roster.json()["work"] if item["workRef"] == accepted["workRef"]
    )
    assert roster_item["nativeTeam"] == {
        "active": 1,
        "total": 2,
        "needsAttention": 1,
        "degraded": False,
    }
    serialized = json.dumps({"detail": child_observed.json(), "list": roster_item})
    assert "thread_synthetic" not in serialized
    assert "child_synthetic" not in serialized
    assert "/Users/" not in serialized
    assert "reviewer@example.com" not in serialized


def test_terminal_work_remains_pinned_until_dismissed(account_client):
    completed = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-completed"),
        json=delegation_payload(title="Completed mission"),
    ).json()
    failed = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-failed"),
        json=delegation_payload(title="Failed mission"),
    ).json()
    store = account_client.app.state.store
    completed_row = store.get_delegation(completed["workRef"], tenant_id="tenant-a", owner_id="owner-a")
    failed_row = store.get_delegation(failed["workRef"], tenant_id="tenant-a", owner_id="owner-a")
    store.finalize_run(completed_row["current_run_id"], "completed", output_text="All done")
    store.finalize_run(
        failed_row["current_run_id"],
        "failed",
        error_text="Provider unavailable",
        failure_class="provider_temporarily_unavailable",
        failure_retryable=1,
        failure_structured=1,
        failure_user_message="The provider is temporarily unavailable.",
    )

    roster = account_client.get("/v1/active-work", headers=account_headers())

    assert roster.status_code == 200
    assert [item["title"] for item in roster.json()["work"]] == [
        "Failed mission",
        "Completed mission",
    ]
    assert roster.json()["work"][0]["state"] == "failed"
    assert roster.json()["work"][0]["actions"] == ["retry", "dismiss"]
    assert roster.json()["work"][1]["delivery"]["unreadTerminal"] is True


def test_active_work_stop_is_exact_idempotent_and_owner_scoped(account_client):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-cancel"),
        json=delegation_payload(title="Cancelable mission"),
    ).json()
    work_ref = accepted["workRef"]

    stopped = account_client.post(
        f"/v1/work/{work_ref}/actions",
        headers=account_headers(),
        json={"action": "stop", "idempotencyKey": "action-stop-once"},
    )
    replay = account_client.post(
        f"/v1/work/{work_ref}/actions",
        headers=account_headers(),
        json={"action": "stop", "idempotencyKey": "action-stop-once"},
    )
    foreign = account_client.post(
        f"/v1/work/{work_ref}/actions",
        headers=account_headers(owner_id="owner-b"),
        json={"action": "stop", "idempotencyKey": "action-foreign"},
    )

    assert stopped.status_code == replay.status_code == 202
    assert stopped.json()["action"] == "stop"
    assert stopped.json()["state"] == "cancelled"
    assert replay.json()["idempotentReplay"] is True
    assert foreign.status_code == 404
    row = account_client.app.state.store.get_delegation(
        work_ref, tenant_id="tenant-a", owner_id="owner-a"
    )
    assert account_client.app.state.store.get_run(row["current_run_id"])["state"] == "cancelled"


def test_active_work_pause_and_resume_preserve_the_exact_queued_run(account_client):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-pause-queued"),
        json=delegation_payload(title="Pause queued mission"),
    ).json()
    work_ref = accepted["workRef"]
    store = account_client.app.state.store
    before = store.get_delegation(
        work_ref, tenant_id="tenant-a", owner_id="owner-a"
    )
    run_id = before["current_run_id"]

    paused = account_client.post(
        f"/v1/work/{work_ref}/actions",
        headers=account_headers(),
        json={"action": "pause", "idempotencyKey": "action-pause-queued"},
    )
    assert paused.status_code == 202
    assert paused.json()["state"] == "paused"
    assert store.get_run(run_id)["state"] == "paused"

    resumed = account_client.post(
        f"/v1/work/{work_ref}/actions",
        headers=account_headers(),
        json={"action": "resume", "idempotencyKey": "action-resume-queued"},
    )
    assert resumed.status_code == 202
    assert resumed.json()["state"] == "queued"
    assert store.get_run(run_id)["state"] == "queued"
    assert [run["run_id"] for run in store.list_runs_for_worker(before["worker_id"])] == [
        run_id
    ]


def test_active_work_stop_accepts_the_exact_paused_run(account_client):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-stop-paused"),
        json=delegation_payload(title="Stop paused mission"),
    ).json()
    work_ref = accepted["workRef"]
    store = account_client.app.state.store
    before = store.get_delegation(
        work_ref, tenant_id="tenant-a", owner_id="owner-a"
    )
    run_id = before["current_run_id"]

    assert account_client.post(
        f"/v1/work/{work_ref}/actions",
        headers=account_headers(),
        json={"action": "pause", "idempotencyKey": "action-pause-before-stop"},
    ).status_code == 202
    stopped = account_client.post(
        f"/v1/work/{work_ref}/actions",
        headers=account_headers(),
        json={"action": "stop", "idempotencyKey": "action-stop-after-pause"},
    )

    assert stopped.status_code == 202
    assert stopped.json()["state"] == "cancelled"
    assert store.get_run(run_id)["state"] == "cancelled"


def test_auth_attention_resume_accepts_bounded_core_reauthorization_and_reuses_run(
    account_client,
):
    payload = delegation_payload_with_origin(
        origin_ref="ghi_synthetic_reauthorization_origin",
        title="Reauthorize mission",
    )
    old_max = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    payload["bootstrapBundle"]["glasshive_capability_authorization"] = {
        "version": 1,
        "status": "pending_admission",
        "authorization_ref": "gha_synthetic_reauthorization_ref",
        "origin_ref": "ghi_synthetic_reauthorization_origin",
        "scope_fingerprint": "scope_synthetic_reauthorization",
        "max_expires_at": old_max,
    }
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-reauthorize"),
        json=payload,
    ).json()
    store = account_client.app.state.store
    record = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    run_id = record["current_run_id"]
    assert store.transition_run_if_state(
        run_id,
        "queued",
        "needs_input",
        error_text="Explicit authorization is required",
        failure_class="capability_authorization_horizon_expired",
        failure_user_message="Explicit authorization is required",
    )
    store.update_worker_state(
        record["worker_id"], "needs_input", last_error="Explicit authorization is required"
    )

    detail = account_client.get(
        f"/v1/work/{accepted['workRef']}", headers=account_headers()
    )
    assert detail.status_code == 200
    assert detail.json()["attention"]["kind"] == "auth"
    assert (
        detail.json()["attention"]["code"]
        == "capability_authorization_horizon_expired"
    )

    new_max = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    invalid = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json={
            "action": "resume",
            "idempotencyKey": "action-reauthorize-wrong-scope",
            "capabilityReauthorization": {
                "version": 1,
                "authorizationRef": "gha_synthetic_reauthorization_ref",
                "maxExpiresAt": new_max,
                "scopeFingerprint": "scope_changed_not_allowed",
            },
        },
    )
    assert invalid.status_code == 409
    assert invalid.json()["detail"]["code"] == "capability_reauthorization_invalid"
    assert store.get_run(run_id)["state"] == "needs_input"

    resumed = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json={
            "action": "resume",
            "idempotencyKey": "action-reauthorize-valid",
            "capabilityReauthorization": {
                "version": 1,
                "authorizationRef": "gha_synthetic_reauthorization_ref",
                "maxExpiresAt": new_max,
                "scopeFingerprint": "scope_synthetic_reauthorization",
            },
        },
    )

    assert resumed.status_code == 202
    assert resumed.json()["state"] == "queued"
    assert resumed.json()["resumeMode"] == "authorization_re_admission"
    assert store.get_run(run_id)["state"] == "queued"
    assert [item["run_id"] for item in store.list_runs_for_worker(record["worker_id"])] == [
        run_id
    ]
    worker = store.get_worker(record["worker_id"])
    persisted = json.loads(worker["bootstrap_bundle_json"])
    assert persisted["glasshive_capability_authorization"]["max_expires_at"] == new_max
    serialized_response = json.dumps(resumed.json())
    assert "gha_synthetic_reauthorization_ref" not in serialized_response
    assert "scope_synthetic_reauthorization" not in serialized_response


@pytest.mark.parametrize(
    "failure_code",
    [
        "capability_policy_denied",
        "capability_account_unavailable",
        "capability_registry_unavailable",
    ],
)
def test_non_horizon_needs_input_is_not_misreported_as_reauthorization(
    account_client,
    failure_code,
):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key=f"delegation-attention-{failure_code}"),
        json=delegation_payload(title=f"Attention {failure_code}"),
    ).json()
    store = account_client.app.state.store
    record = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    assert store.transition_run_if_state(
        record["current_run_id"],
        "queued",
        "needs_input",
        error_text="Input is required",
        failure_class=failure_code,
        failure_user_message="Input is required",
    )
    store.update_worker_state(record["worker_id"], "needs_input", last_error="")

    detail = account_client.get(
        f"/v1/work/{accepted['workRef']}", headers=account_headers()
    )

    assert detail.status_code == 200
    assert detail.json()["attention"]["kind"] == "input"
    assert detail.json()["attention"]["code"] == failure_code


def test_active_work_retry_requires_retryable_failure_and_updates_current_run(account_client):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-retry"),
        json=delegation_payload(title="Retry mission"),
    ).json()
    store = account_client.app.state.store
    before = store.get_delegation(accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a")
    store.finalize_run(
        before["current_run_id"],
        "failed",
        error_text="Temporary outage",
        failure_class="provider_temporarily_unavailable",
        failure_retryable=1,
        failure_structured=1,
        failure_user_message="Temporary outage",
    )

    retried = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json={"action": "retry", "idempotencyKey": "action-retry-once"},
    )

    assert retried.status_code == 202
    assert retried.json()["state"] == "queued"
    after = store.get_delegation(accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a")
    assert after["current_run_id"] != before["current_run_id"]
    assert store.get_run(after["current_run_id"])["state"] == "queued"


@pytest.mark.parametrize("action", ["message", "steer"])
def test_instruction_actions_require_a_nonempty_instruction(account_client, action):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key=f"delegation-{action}"),
        json=delegation_payload(title=f"{action.title()} mission"),
    ).json()

    response = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json={"action": action, "idempotencyKey": f"action-{action}-once"},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "active_work_instruction_required"


def test_active_work_queue_persists_a_followup_without_interrupting_current_run(account_client):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-queue"),
        json=delegation_payload(title="Queue mission"),
    ).json()
    store = account_client.app.state.store
    before = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )

    queued = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json={
            "action": "queue",
            "instruction": "Then compile the findings.",
            "idempotencyKey": "action-queue-once",
        },
    )

    assert queued.status_code == 202
    assert queued.json()["state"] == "queued"
    assert queued.json()["deliveryMode"] == "queued"
    after = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    assert after["current_run_id"] != before["current_run_id"]
    assert store.get_run(before["current_run_id"])["state"] == "queued"
    assert store.get_run(after["current_run_id"])["state"] == "queued"


@pytest.mark.parametrize("action", ["queue", "message", "steer"])
def test_active_work_rejects_nonterminal_actions_after_mission_completed(
    account_client,
    action,
):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key=f"delegation-terminal-{action}"),
        json=delegation_payload(title=f"Terminal {action}"),
    ).json()
    store = account_client.app.state.store
    record = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    store.finalize_run(record["current_run_id"], "completed", output_text="Done")

    response = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json={
            "action": action,
            "instruction": "This must not create a hidden continuation.",
            "idempotencyKey": f"terminal-{action}-forbidden",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "active_work_action_not_available"
    assert len(store.list_runs_for_worker(record["worker_id"])) == 1


def test_active_work_stop_cancels_running_mission_and_queued_followup_without_false_success(
    account_client,
):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-stop-with-followup"),
        json=delegation_payload(title="Stop mission with followup"),
    ).json()
    store = account_client.app.state.store
    record = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    source_run_id = record["current_run_id"]
    assert store.transition_run_if_state(
        source_run_id,
        "queued",
        "running",
        started_at=datetime.now(timezone.utc).isoformat(),
    )
    store.update_worker_state(record["worker_id"], "running", last_error="")
    queued = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json={
            "action": "queue",
            "instruction": "Follow up after the current run.",
            "idempotencyKey": "queue-before-mission-stop",
        },
    )
    assert queued.status_code == 202

    stopped = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json={"action": "stop", "idempotencyKey": "stop-whole-mission"},
    )

    assert stopped.status_code == 202, stopped.text
    assert stopped.json()["state"] == "cancelled"
    runs = store.list_runs_for_worker(record["worker_id"])
    assert {run["state"] for run in runs} == {"cancelled"}


def test_active_work_pause_targets_running_run_ahead_of_queued_followup(account_client):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-pause-running-followup"),
        json=delegation_payload(title="Pause running mission with followup"),
    ).json()
    service = account_client.app.state.service
    store = account_client.app.state.store
    record = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    source_run_id = record["current_run_id"]
    assert store.transition_run_if_state(
        source_run_id,
        "queued",
        "running",
        started_at=datetime.now(timezone.utc).isoformat(),
    )
    store.update_worker_state(record["worker_id"], "running", last_error="")
    queued = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json={
            "action": "queue",
            "instruction": "Run only after the active work resumes and completes.",
            "idempotencyKey": "queue-before-exact-pause",
        },
    )
    assert queued.status_code == 202, queued.text
    queued_run_id = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )["current_run_id"]
    pause_targets: list[str] = []
    original_pause = service.runtime.pause_worker

    def capture_pause(worker):
        pause_targets.append(str(worker.get("_active_run_id") or ""))
        return original_pause(worker)

    service.runtime.pause_worker = capture_pause
    paused = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json={"action": "pause", "idempotencyKey": "pause-exact-running"},
    )

    assert paused.status_code == 202, paused.text
    assert pause_targets == [source_run_id]
    assert store.get_run(source_run_id)["state"] == "paused"
    assert store.get_run(queued_run_id)["state"] == "queued"


def test_active_work_queue_does_not_resume_paused_run_and_resume_targets_it_exactly(
    account_client,
):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-resume-paused-followup"),
        json=delegation_payload(title="Resume paused mission with followup"),
    ).json()
    store = account_client.app.state.store
    record = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    source_run_id = record["current_run_id"]
    assert store.transition_run_if_state(
        source_run_id,
        "queued",
        "paused",
        started_at=datetime.now(timezone.utc).isoformat(),
        error_text="Paused by operator",
    )
    store.update_worker_state(record["worker_id"], "paused", last_error="")

    queued = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json={
            "action": "queue",
            "instruction": "Remain queued behind the paused exact run.",
            "idempotencyKey": "queue-behind-paused",
        },
    )
    assert queued.status_code == 202, queued.text
    queued_run_id = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )["current_run_id"]
    assert store.get_worker(record["worker_id"])["state"] == "paused"
    assert store.get_run(source_run_id)["state"] == "paused"
    assert store.get_run(queued_run_id)["state"] == "queued"

    resumed = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json={"action": "resume", "idempotencyKey": "resume-exact-paused"},
    )

    assert resumed.status_code == 202, resumed.text
    assert store.get_run(source_run_id)["state"] == "running"
    assert store.get_run(queued_run_id)["state"] == "queued"


def test_active_work_stop_closes_needs_input_run_and_queued_followup(account_client):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-stop-needs-input-followup"),
        json=delegation_payload(title="Stop needs-input mission with followup"),
    ).json()
    store = account_client.app.state.store
    record = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    assert store.transition_run_if_state(
        record["current_run_id"],
        "queued",
        "needs_input",
        error_text="Authorization required",
        failure_class="capability_authorization_horizon_expired",
    )
    store.update_worker_state(record["worker_id"], "needs_input", last_error="Authorization required")
    queued = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json={
            "action": "queue",
            "instruction": "Queued after authorization.",
            "idempotencyKey": "queue-after-needs-input",
        },
    )
    assert queued.status_code == 202, queued.text

    stopped = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json={"action": "stop", "idempotencyKey": "stop-needs-input-work"},
    )

    assert stopped.status_code == 202, stopped.text
    assert {run["state"] for run in store.list_runs_for_worker(record["worker_id"])} == {
        "cancelled"
    }


def test_queued_followup_does_not_hide_or_overtake_needs_input_source(account_client):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-needs-input-followup-order"),
        json=delegation_payload(title="Needs-input source before follow-up"),
    ).json()
    store = account_client.app.state.store
    record = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    source_run_id = record["current_run_id"]
    assert store.transition_run_if_state(
        source_run_id,
        "queued",
        "needs_input",
        error_text="Provider authorization projection is unavailable",
        failure_class="provider_auth_projection_unavailable",
        failure_user_message="Reconnect the provider account and resume.",
    )
    store.update_worker_state(
        record["worker_id"],
        "needs_input",
        last_error="Provider authorization projection is unavailable",
    )

    queued = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json={
            "action": "queue",
            "instruction": "Run only after the source objective can resume.",
            "idempotencyKey": "queue-behind-needs-input",
        },
    )
    assert queued.status_code == 202, queued.text
    followup_run_id = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )["current_run_id"]
    assert followup_run_id != source_run_id

    detail = account_client.get(
        f"/v1/work/{accepted['workRef']}", headers=account_headers()
    )
    assert detail.status_code == 200
    assert detail.json()["state"] == "needs_input"
    assert "resume" in detail.json()["actions"]
    assert "steer" not in detail.json()["actions"]

    resumed = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json={
            "action": "resume",
            "idempotencyKey": "resume-needs-input-before-followup",
        },
    )
    assert resumed.status_code == 202, resumed.text
    with sqlite3.connect(store.db_path) as conn:
        action_use_id = conn.execute(
            "SELECT action_use_id FROM active_work_action_uses WHERE idempotency_key = ?",
            ("resume-needs-input-before-followup",),
        ).fetchone()[0]
    action_row = store.get_active_work_action(action_use_id)
    assert action_row["lifecycle_target_run_id"] == source_run_id
    assert store.get_run(source_run_id)["state"] == "queued"
    assert store.get_run(followup_run_id)["state"] == "queued"


def test_run_terminal_callback_marks_work_nonterminal_while_followup_is_queued(
    account_client,
):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-callback-followup"),
        json=delegation_payload_with_origin(
            origin_ref="ghi_synthetic_callback_followup",
            title="Callback work truth",
        ),
    ).json()
    service = account_client.app.state.service
    store = account_client.app.state.store
    record = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    source = store.get_run(record["current_run_id"])
    followup = service.assign_run(
        record["worker_id"],
        "Queued sibling",
        start_processor=False,
        idempotency_key="callback-followup-sibling",
    )
    completed = store.finalize_run(source["run_id"], "completed", output_text="First done")
    worker = store.get_worker(record["worker_id"])
    service._emit_callback(worker, "run.completed", run=completed, message="First done")
    with sqlite3.connect(store.db_path) as conn:
        payload = json.loads(
            conn.execute(
                "SELECT payload_json FROM callback_outbox WHERE run_id = ? ORDER BY created_at DESC LIMIT 1",
                (source["run_id"],),
            ).fetchone()[0]
        )
    assert payload["work_ref"] == accepted["workRef"]
    assert payload["work_state"] == "queued"
    assert payload["work_terminal"] is False

    terminal = store.finalize_run(followup["run_id"], "completed", output_text="All done")
    service._emit_callback(worker, "run.completed", run=terminal, message="All done")
    with sqlite3.connect(store.db_path) as conn:
        final_payload = json.loads(
            conn.execute(
                "SELECT payload_json FROM callback_outbox WHERE run_id = ? ORDER BY created_at DESC LIMIT 1",
                (followup["run_id"],),
            ).fetchone()[0]
        )
    assert final_payload["work_state"] == "completed"
    assert final_payload["work_terminal"] is True


def test_active_work_dismiss_hides_terminal_card_without_deleting_history(account_client):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-dismiss"),
        json=delegation_payload(title="Dismiss mission"),
    ).json()
    store = account_client.app.state.store
    row = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    store.finalize_run(row["current_run_id"], "completed", output_text="Done")

    dismissed = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json={"action": "dismiss", "idempotencyKey": "action-dismiss-once"},
    )
    roster = account_client.get("/v1/active-work", headers=account_headers())

    assert dismissed.status_code == 202
    assert dismissed.json()["state"] == "completed"
    assert roster.json()["work"] == []
    assert store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )["dismissed_at"]


def test_active_work_dismiss_remains_available_after_permanent_work_stop(account_client):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-stop-then-dismiss"),
        json=delegation_payload(title="Stopped mission to dismiss"),
    ).json()

    stopped = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json={"action": "stop", "idempotencyKey": "action-stop-before-dismiss"},
    )
    dismissed = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json={"action": "dismiss", "idempotencyKey": "action-dismiss-after-stop"},
    )

    assert stopped.status_code == 202
    assert stopped.json()["state"] == "cancelled"
    assert dismissed.status_code == 202
    assert dismissed.json()["state"] == "cancelled"
    assert account_client.get("/v1/active-work", headers=account_headers()).json()["work"] == []


@pytest.mark.parametrize(
    ("action", "instruction"),
    [
        ("queue", "Queue the durable follow-up."),
        ("message", "Send the durable message."),
        ("steer", "Steer to the durable objective."),
        ("pause", None),
        ("resume", None),
        ("stop", None),
        ("retry", None),
        ("dismiss", None),
    ],
)
def test_active_work_action_reconciles_crash_after_effect_without_duplicate(
    tmp_path,
    monkeypatch,
    action,
    instruction,
):
    monkeypatch.setenv("WPR_API_TOKEN", API_TOKEN)
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_SERVICE_ASSERTION_SECRET", ASSERTION_SECRET)
    db_path = str(tmp_path / f"action-crash-{action}.sqlite3")
    app = create_app(
        db_path=db_path,
        runtime_backend="stub",
        runtime=StubRuntime(),
        reconcile_on_startup=False,
    )
    app.state.service.start_assigned_run = lambda _worker_id: None
    app.state.service._ensure_worker_processor = lambda _worker_id: None
    with TestClient(app) as client:
        accepted = client.post(
            "/v1/delegations",
            headers=account_headers(idempotency_key=f"delegation-crash-{action}"),
            json=delegation_payload(title=f"Crash {action} mission"),
        ).json()
        store = app.state.store
        before = store.get_delegation(
            accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
        )
        if action == "resume":
            assert store.transition_run_if_state(
                before["current_run_id"],
                "queued",
                "paused",
                error_text="Paused",
                started_at=datetime.now(timezone.utc).isoformat(),
            )
            store.update_worker_state(before["worker_id"], "paused", last_error="")
        elif action == "retry":
            store.finalize_run(
                before["current_run_id"],
                "failed",
                error_text="Temporary outage",
                failure_class="provider_temporarily_unavailable",
                failure_retryable=1,
                failure_structured=1,
                failure_user_message="Temporary outage",
            )
        elif action == "dismiss":
            store.finalize_run(before["current_run_id"], "completed", output_text="Done")

        request_body = {
            "action": action,
            "idempotencyKey": f"action-crash-{action}",
        }
        if instruction is not None:
            request_body["instruction"] = instruction
        action_request = {
            "action": action,
            "instruction": instruction or "",
            "capabilityReauthorization": None,
        }
        reservation = store.reserve_active_work_action(
            tenant_id="tenant-a",
            owner_id="owner-a",
            work_ref=accepted["workRef"],
            idempotency_key=f"action-crash-{action}",
            action=action,
            payload_digest=hashlib.sha256(
                json.dumps(
                    action_request,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest(),
            executor_id=app.state.service.executor_id,
        )
        result = app.state.service.execute_active_work_action(
            before,
            action=action,
            instruction=instruction or "",
            idempotency_key=f"action-crash-{action}",
            action_use_id=reservation["action_use_id"],
        )
        assert result["run_id"]
        # Simulate a hard process exit after the durable effect and before the
        # separate action-ledger completion write.
        with sqlite3.connect(store.db_path) as conn:
            row = conn.execute(
                "SELECT status FROM active_work_action_uses WHERE action_use_id = ?",
                (reservation["action_use_id"],),
            ).fetchone()
        assert row == ("pending",)

    restarted = create_app(
        db_path=db_path,
        runtime_backend="stub",
        runtime=StubRuntime(),
        reconcile_on_startup=False,
    )
    restarted.state.service.start_assigned_run = lambda _worker_id: None
    restarted.state.service._ensure_worker_processor = lambda _worker_id: None
    with TestClient(restarted) as client:
        replay = client.post(
            f"/v1/work/{accepted['workRef']}/actions",
            headers=account_headers(),
            json=request_body,
        )
        assert replay.status_code == 202, replay.text
        assert replay.json()["idempotentReplay"] is True
        if action in {"queue", "message"}:
            assert replay.json()["deliveryMode"] == (
                "queued_next_boundary" if action == "message" else "queued"
            )
        with sqlite3.connect(restarted.state.store.db_path) as conn:
            action_rows = conn.execute(
                "SELECT status FROM active_work_action_uses WHERE work_ref = ?",
                (accepted["workRef"],),
            ).fetchall()
        assert action_rows == [("completed",)]
        if action in {"queue", "message", "steer", "retry"}:
            after = restarted.state.store.get_delegation(
                accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
            )
            runs = restarted.state.store.list_runs_for_worker(after["worker_id"])
            assert len(runs) == 2


def test_steer_terminal_winner_lost_response_replays_authoritative_source(
    tmp_path,
    monkeypatch,
):
    """A cancelled steer reservation must never replace terminal source truth."""

    monkeypatch.setenv("WPR_API_TOKEN", API_TOKEN)
    monkeypatch.setenv(
        "VIVENTIUM_GLASSHIVE_SERVICE_ASSERTION_SECRET", ASSERTION_SECRET
    )
    db_path = str(tmp_path / "steer-terminal-winner-lost-response.sqlite3")
    runtime = StubRuntime()
    app = create_app(
        db_path=db_path,
        runtime_backend="stub",
        runtime=runtime,
        reconcile_on_startup=False,
    )
    app.state.service.start_assigned_run = lambda _worker_id: None
    app.state.service._ensure_worker_processor = lambda _worker_id: None
    with TestClient(app) as client:
        accepted = client.post(
            "/v1/delegations",
            headers=account_headers(
                idempotency_key="delegation-steer-terminal-lost-response"
            ),
            json=delegation_payload(title="Steer terminal winner lost response"),
        ).json()
        store = app.state.store
        source = store.get_delegation(
            accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
        )
        started_at = datetime.now(timezone.utc).isoformat()
        assert store.transition_run_if_state(
            source["run_id"], "queued", "running", started_at=started_at
        )
        store.update_worker_state(source["worker_id"], "running", last_error="")
        source = store.get_delegation(
            accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
        )

        instruction = "Use the corrected terminal-safe objective."
        idempotency_key = "action-steer-terminal-lost-response"
        action_request = {
            "action": "steer",
            "instruction": instruction,
            "capabilityReauthorization": None,
        }
        reservation = store.reserve_active_work_action(
            tenant_id="tenant-a",
            owner_id="owner-a",
            work_ref=accepted["workRef"],
            idempotency_key=idempotency_key,
            action="steer",
            payload_digest=hashlib.sha256(
                json.dumps(
                    action_request,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest(),
            expected_current_run_id=str(source["current_run_id"]),
            expected_source_run_id=str(source["run_id"]),
            expected_source_state=str(source["run_state"]),
            expected_source_started_at=str(source["run_started_at"] or ""),
            executor_id=app.state.service.executor_id,
        )

        original_interrupt = runtime.interrupt_worker
        interrupt_calls = 0

        def complete_during_interrupt(*args, **kwargs):
            nonlocal interrupt_calls
            interrupt_calls += 1
            assert store.finalize_run_if_state(
                source["run_id"],
                "running",
                "completed",
                output_text="Authoritative completion",
            )
            return original_interrupt(*args, **kwargs)

        runtime.interrupt_worker = complete_during_interrupt
        direct = app.state.service.execute_active_work_action(
            source,
            action="steer",
            instruction=instruction,
            idempotency_key=idempotency_key,
            action_use_id=reservation["action_use_id"],
        )
        replacement_run_id = app.state.service.active_work_effect_run_id(
            source, idempotency_key=idempotency_key
        )
        assert direct["control_outcome"] == "terminal_won"
        assert direct["run_id"] == source["run_id"]
        assert direct["state"] == "completed"
        assert store.get_run(replacement_run_id)["state"] == "cancelled"
        assert store.get_delegation(
            accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
        )["current_run_id"] == source["run_id"]
        assert store.get_active_work_action(reservation["action_use_id"])[
            "status"
        ] == "pending"
        with sqlite3.connect(store.db_path) as conn:
            conn.execute(
                "UPDATE active_work_action_uses SET lease_expires_at = ? WHERE action_use_id = ?",
                ("2000-01-01T00:00:00+00:00", reservation["action_use_id"]),
            )

    restarted = create_app(
        db_path=db_path,
        runtime_backend="stub",
        runtime=runtime,
        reconcile_on_startup=False,
    )
    restarted.state.service.start_assigned_run = lambda _worker_id: None
    restarted.state.service._ensure_worker_processor = lambda _worker_id: None
    with TestClient(restarted) as client:
        current = restarted.state.store.get_delegation(
            accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
        )
        reconciled = restarted.state.service.reconcile_active_work_action(
            current,
            action="steer",
            instruction=instruction,
            idempotency_key=idempotency_key,
            source_run_id=source["run_id"],
            action_use_id=reservation["action_use_id"],
        )
        assert reconciled["control_outcome"] == "terminal_won"
        assert reconciled["run_id"] == source["run_id"]
        assert reconciled["state"] == "completed"

        body = {
            "action": "steer",
            "instruction": instruction,
            "idempotencyKey": idempotency_key,
        }
        replay = client.post(
            f"/v1/work/{accepted['workRef']}/actions",
            headers=account_headers(),
            json=body,
        )
        repeated = client.post(
            f"/v1/work/{accepted['workRef']}/actions",
            headers=account_headers(),
            json=body,
        )

        assert replay.status_code == 202, replay.text
        assert replay.json()["state"] == "completed"
        assert replay.json()["idempotentReplay"] is True
        assert repeated.status_code == 202
        assert repeated.json() == replay.json()
        assert interrupt_calls == 1
        authoritative = restarted.state.store.get_delegation(
            accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
        )
        assert authoritative["current_run_id"] == source["run_id"]
        assert authoritative["run_state"] == "completed"
        assert restarted.state.store.get_run(replacement_run_id)["state"] == "cancelled"
        action_row = restarted.state.store.get_active_work_action(
            reservation["action_use_id"]
        )
        assert action_row["status"] == "completed"
        assert json.loads(action_row["response_json"])["state"] == "completed"


@pytest.mark.parametrize("action", ["pause", "resume"])
def test_pause_resume_terminal_winner_lost_response_replays_authoritative_source(
    tmp_path,
    monkeypatch,
    action,
):
    """A terminal commit during control remains the replayed public truth."""

    monkeypatch.setenv("WPR_API_TOKEN", API_TOKEN)
    monkeypatch.setenv(
        "VIVENTIUM_GLASSHIVE_SERVICE_ASSERTION_SECRET", ASSERTION_SECRET
    )
    db_path = str(tmp_path / f"{action}-terminal-winner-lost-response.sqlite3")
    runtime = StubRuntime()
    app = create_app(
        db_path=db_path,
        runtime_backend="stub",
        runtime=runtime,
        reconcile_on_startup=False,
    )
    app.state.service.start_assigned_run = lambda _worker_id: None
    app.state.service._ensure_worker_processor = lambda _worker_id: None
    with TestClient(app) as client:
        accepted = client.post(
            "/v1/delegations",
            headers=account_headers(
                idempotency_key=f"delegation-{action}-terminal-lost-response"
            ),
            json=delegation_payload(title=f"{action.title()} terminal winner"),
        ).json()
        store = app.state.store
        source = store.get_delegation(
            accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
        )
        started_at = datetime.now(timezone.utc).isoformat()
        assert store.transition_run_if_state(
            source["run_id"],
            "queued",
            "running" if action == "pause" else "paused",
            started_at=started_at,
        )
        store.update_worker_state(
            source["worker_id"],
            "running" if action == "pause" else "paused",
            last_error="",
        )
        source = store.get_delegation(
            accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
        )
        idempotency_key = f"action-{action}-terminal-lost-response"
        action_request = {
            "action": action,
            "instruction": "",
            "capabilityReauthorization": None,
        }
        reservation = store.reserve_active_work_action(
            tenant_id="tenant-a",
            owner_id="owner-a",
            work_ref=accepted["workRef"],
            idempotency_key=idempotency_key,
            action=action,
            payload_digest=hashlib.sha256(
                json.dumps(
                    action_request,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest(),
            expected_current_run_id=str(source["current_run_id"]),
            expected_source_run_id=str(source["run_id"]),
            expected_source_state=str(source["run_state"]),
            expected_source_started_at=str(source["run_started_at"]),
            executor_id=app.state.service.executor_id,
        )

        runtime_calls = 0
        original_runtime_control = (
            runtime.pause_worker
            if action == "pause"
            else runtime.ensure_worker_ready
        )

        def complete_during_control(*args, **kwargs):
            nonlocal runtime_calls
            runtime_calls += 1
            assert store.finalize_run_if_state(
                source["run_id"],
                "running" if action == "pause" else "paused",
                "completed",
                output_text="Authoritative completion",
            )
            return original_runtime_control(*args, **kwargs)

        if action == "pause":
            runtime.pause_worker = complete_during_control
        else:
            runtime.ensure_worker_ready = complete_during_control
        direct = app.state.service.execute_active_work_action(
            source,
            action=action,
            idempotency_key=idempotency_key,
            action_use_id=reservation["action_use_id"],
        )
        assert direct["control_outcome"] == "terminal_won"
        assert direct["run_id"] == source["run_id"]
        assert direct["state"] == "completed"
        assert store.get_active_work_action(reservation["action_use_id"])[
            "status"
        ] == "pending"
        with sqlite3.connect(store.db_path) as conn:
            conn.execute(
                "UPDATE active_work_action_uses SET lease_expires_at = ? WHERE action_use_id = ?",
                ("2000-01-01T00:00:00+00:00", reservation["action_use_id"]),
            )

    restarted = create_app(
        db_path=db_path,
        runtime_backend="stub",
        runtime=runtime,
        reconcile_on_startup=False,
    )
    restarted.state.service.start_assigned_run = lambda _worker_id: None
    restarted.state.service._ensure_worker_processor = lambda _worker_id: None
    with TestClient(restarted) as client:
        body = {"action": action, "idempotencyKey": idempotency_key}
        replay = client.post(
            f"/v1/work/{accepted['workRef']}/actions",
            headers=account_headers(),
            json=body,
        )
        repeated = client.post(
            f"/v1/work/{accepted['workRef']}/actions",
            headers=account_headers(),
            json=body,
        )

        assert replay.status_code == 202, replay.text
        assert replay.json()["state"] == "completed"
        assert replay.json()["controlOutcome"] == "terminal_won"
        assert replay.json()["runId"] == source["run_id"]
        assert replay.json()["idempotentReplay"] is True
        assert repeated.json() == replay.json()
        assert runtime_calls == 1
        authoritative = restarted.state.store.get_delegation(
            accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
        )
        assert authoritative["current_run_id"] == source["run_id"]
        assert authoritative["run_state"] == "completed"


def test_stop_completion_winner_lost_response_replays_settled_work_tombstone(
    tmp_path,
    monkeypatch,
):
    """Public Stop succeeds when completion wins and replays without a second RPC."""

    monkeypatch.setenv("WPR_API_TOKEN", API_TOKEN)
    monkeypatch.setenv(
        "VIVENTIUM_GLASSHIVE_SERVICE_ASSERTION_SECRET", ASSERTION_SECRET
    )
    db_path = str(tmp_path / "stop-completion-winner-lost-response.sqlite3")
    runtime = StubRuntime()
    app = create_app(
        db_path=db_path,
        runtime_backend="stub",
        runtime=runtime,
        reconcile_on_startup=False,
    )
    app.state.service.start_assigned_run = lambda _worker_id: None
    app.state.service._ensure_worker_processor = lambda _worker_id: None
    with TestClient(app) as client:
        accepted = client.post(
            "/v1/delegations",
            headers=account_headers(
                idempotency_key="delegation-stop-completion-lost-response"
            ),
            json=delegation_payload(title="Stop completion winner"),
        ).json()
        store = app.state.store
        source = store.get_delegation(
            accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
        )
        started_at = datetime.now(timezone.utc).isoformat()
        assert store.transition_run_if_state(
            source["run_id"], "queued", "running", started_at=started_at
        )
        store.update_worker_state(source["worker_id"], "running", last_error="")
        sibling = store.create_run(
            source["worker_id"],
            source["project_id"],
            "Queued sibling must be settled",
            state="queued",
        )
        source = store.get_delegation(
            accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
        )
        idempotency_key = "action-stop-completion-lost-response"
        action_request = {
            "action": "stop",
            "instruction": "",
            "capabilityReauthorization": None,
        }
        reservation = store.reserve_active_work_action(
            tenant_id="tenant-a",
            owner_id="owner-a",
            work_ref=accepted["workRef"],
            idempotency_key=idempotency_key,
            action="stop",
            payload_digest=hashlib.sha256(
                json.dumps(
                    action_request,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest(),
            expected_current_run_id=str(source["current_run_id"]),
            expected_source_run_id=str(source["run_id"]),
            expected_source_state=str(source["run_state"]),
            expected_source_started_at=str(source["run_started_at"]),
            executor_id=app.state.service.executor_id,
        )
        interrupt_calls = 0
        original_interrupt = runtime.interrupt_worker

        def complete_during_interrupt(*args, **kwargs):
            nonlocal interrupt_calls
            interrupt_calls += 1
            assert store.finalize_run_if_state(
                source["run_id"],
                "running",
                "completed",
                output_text="Authoritative completion",
            )
            return original_interrupt(*args, **kwargs)

        runtime.interrupt_worker = complete_during_interrupt
        direct = app.state.service.execute_active_work_action(
            source,
            action="stop",
            idempotency_key=idempotency_key,
            action_use_id=reservation["action_use_id"],
        )
        worker = store.get_worker(source["worker_id"])
        action_row = store.get_active_work_action(reservation["action_use_id"])
        assert direct["control_outcome"] == "terminal_won"
        assert direct["state"] == "completed"
        assert worker["work_stop_id"] == action_row["lifecycle_operation_id"]
        assert worker["work_stop_settled_at"]
        assert worker["work_stop_outcome"] == "completion_won"
        assert store.get_run(sibling["run_id"])["state"] == "cancelled"
        assert action_row["status"] == "pending"
        with sqlite3.connect(store.db_path) as conn:
            conn.execute(
                "UPDATE active_work_action_uses SET lease_expires_at = ? WHERE action_use_id = ?",
                ("2000-01-01T00:00:00+00:00", reservation["action_use_id"]),
            )

    restarted = create_app(
        db_path=db_path,
        runtime_backend="stub",
        runtime=runtime,
        reconcile_on_startup=False,
    )
    restarted.state.service.start_assigned_run = lambda _worker_id: None
    restarted.state.service._ensure_worker_processor = lambda _worker_id: None
    with TestClient(restarted) as client:
        body = {"action": "stop", "idempotencyKey": idempotency_key}
        replay = client.post(
            f"/v1/work/{accepted['workRef']}/actions",
            headers=account_headers(),
            json=body,
        )
        repeated = client.post(
            f"/v1/work/{accepted['workRef']}/actions",
            headers=account_headers(),
            json=body,
        )

        assert replay.status_code == 202, replay.text
        assert replay.json()["state"] == "completed"
        assert replay.json()["controlOutcome"] == "terminal_won"
        assert replay.json()["runId"] == source["run_id"]
        assert replay.json()["idempotentReplay"] is True
        assert repeated.json() == replay.json()
        assert interrupt_calls == 1
        authoritative = restarted.state.store.get_delegation(
            accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
        )
        assert authoritative["current_run_id"] == source["run_id"]
        assert authoritative["run_state"] == "completed"


def test_needs_input_resume_lost_response_repairs_from_atomic_action_proof(
    tmp_path,
    monkeypatch,
):
    """Queued state alone is insufficient; replay uses the atomic resume receipt."""

    monkeypatch.setenv("WPR_API_TOKEN", API_TOKEN)
    monkeypatch.setenv(
        "VIVENTIUM_GLASSHIVE_SERVICE_ASSERTION_SECRET", ASSERTION_SECRET
    )
    # Hold the normal queued-run scheduler so this case isolates the crash seam
    # before any processor wake. Reconciliation below owns the recovery wake.
    monkeypatch.setattr(
        WorkersProjectsService,
        "_scheduler_loop",
        lambda self: self._shutdown_event.wait(),
    )
    db_path = str(tmp_path / "needs-input-resume-lost-response.sqlite3")
    app = create_app(
        db_path=db_path,
        runtime_backend="stub",
        runtime=StubRuntime(),
        reconcile_on_startup=False,
    )
    app.state.service.start_assigned_run = lambda _worker_id: None
    app.state.service._ensure_worker_processor = lambda _worker_id: None
    with TestClient(app) as client:
        accepted = client.post(
            "/v1/delegations",
            headers=account_headers(
                idempotency_key="delegation-needs-input-resume-lost-response"
            ),
            json=delegation_payload(title="Needs input resume lost response"),
        ).json()
        store = app.state.store
        source = store.get_delegation(
            accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
        )
        assert store.transition_run_if_state(
            source["run_id"],
            "queued",
            "needs_input",
            error_text="Input required",
            failure_class="capability_policy_denied",
            failure_user_message="Input required",
        )
        store.update_worker_state(source["worker_id"], "needs_input", last_error="")
        source = store.get_delegation(
            accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
        )
        idempotency_key = "action-needs-input-resume-lost-response"
        action_request = {
            "action": "resume",
            "instruction": "",
            "capabilityReauthorization": None,
        }
        reservation = store.reserve_active_work_action(
            tenant_id="tenant-a",
            owner_id="owner-a",
            work_ref=accepted["workRef"],
            idempotency_key=idempotency_key,
            action="resume",
            payload_digest=hashlib.sha256(
                json.dumps(
                    action_request,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest(),
            expected_current_run_id=str(source["current_run_id"]),
            expected_source_run_id=str(source["run_id"]),
            expected_source_state="needs_input",
            expected_source_started_at=str(source["run_started_at"] or ""),
            executor_id=app.state.service.executor_id,
        )
        # Simulate process death at the durable seam: the store transaction has
        # re-admitted the exact run, but the API response/action finish is lost.
        effect = store.resume_needs_input_active_work_action(
            reservation["action_use_id"],
            worker_id=source["worker_id"],
            run_id=source["run_id"],
            executor_id=app.state.service.executor_id,
        )
        assert effect
        assert store.get_run(source["run_id"])["state"] == "queued"
        assert store.get_worker(source["worker_id"])["state"] == "starting"
        action_row = store.get_active_work_action(reservation["action_use_id"])
        assert action_row["status"] == "pending"
        assert action_row["effect_phase"] == "authorization_re_admitted"
        with sqlite3.connect(store.db_path) as conn:
            event_count = conn.execute(
                "SELECT COUNT(*) FROM events WHERE run_id = ? AND event_type = 'run.authorization_resumed'",
                (source["run_id"],),
            ).fetchone()[0]
            conn.execute(
                "UPDATE active_work_action_uses SET lease_expires_at = ? WHERE action_use_id = ?",
                ("2000-01-01T00:00:00+00:00", reservation["action_use_id"]),
            )
        assert event_count == 1

    restarted = create_app(
        db_path=db_path,
        runtime_backend="stub",
        runtime=StubRuntime(),
        reconcile_on_startup=False,
    )
    restarted.state.service.start_assigned_run = lambda _worker_id: None
    processor_wakes: list[str] = []
    restarted.state.service._ensure_worker_processor = processor_wakes.append
    with TestClient(restarted) as client:
        body = {"action": "resume", "idempotencyKey": idempotency_key}
        replay = client.post(
            f"/v1/work/{accepted['workRef']}/actions",
            headers=account_headers(),
            json=body,
        )
        repeated = client.post(
            f"/v1/work/{accepted['workRef']}/actions",
            headers=account_headers(),
            json=body,
        )

        assert replay.status_code == 202, replay.text
        assert replay.json()["state"] == "queued"
        assert replay.json()["resumeMode"] == "authorization_re_admission"
        assert replay.json()["idempotentReplay"] is True
        assert repeated.json() == replay.json()
        assert processor_wakes == [source["worker_id"]]
        with sqlite3.connect(restarted.state.store.db_path) as conn:
            event_count = conn.execute(
                "SELECT COUNT(*) FROM events WHERE run_id = ? AND event_type = 'run.authorization_resumed'",
                (source["run_id"],),
            ).fetchone()[0]
        assert event_count == 1
        assert restarted.state.store.get_active_work_action(
            reservation["action_use_id"]
        )["status"] == "completed"


def test_stop_reconciliation_requires_bound_settled_work_tombstone(
    account_client,
):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-stop-proof"),
        json=delegation_payload(title="Stop proof"),
    ).json()
    store = account_client.app.state.store
    record = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    sibling = store.create_run(
        record["worker_id"], record["project_id"], "Queued sibling", state="queued"
    )
    reservation = store.reserve_active_work_action(
        tenant_id="tenant-a",
        owner_id="owner-a",
        work_ref=accepted["workRef"],
        idempotency_key="action-stop-proof",
        action="stop",
        payload_digest=hashlib.sha256(
            json.dumps(
                {
                    "action": "stop",
                    "instruction": "",
                    "capabilityReauthorization": None,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
        executor_id=account_client.app.state.service.executor_id,
    )
    assert store.transition_run_if_state(record["run_id"], "queued", "cancelled")

    reconciled = account_client.app.state.service.reconcile_active_work_action(
        record,
        action="stop",
        idempotency_key="action-stop-proof",
        source_run_id=record["run_id"],
        action_use_id=reservation["action_use_id"],
    )

    assert reconciled is None
    assert store.get_run(sibling["run_id"])["state"] == "queued"
    assert not store.get_worker(record["worker_id"])["work_stop_id"]
    assert store.get_active_work_action(reservation["action_use_id"])[
        "status"
    ] == "pending"


def test_stop_does_not_accept_an_unrelated_live_control_claim(account_client):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-stop-other-claim"),
        json=delegation_payload(title="Stop other claim"),
    ).json()
    store = account_client.app.state.store
    record = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    started_at = datetime.now(timezone.utc).isoformat()
    assert store.transition_run_if_state(
        record["run_id"], "queued", "running", started_at=started_at
    )
    store.update_worker_state(record["worker_id"], "running", last_error="")
    current_worker = store.get_worker(record["worker_id"])
    claim = store.try_claim_worker_compute_release(
        record["worker_id"],
        expected_updated_at=current_worker["updated_at"],
        expected_last_run_id=str(current_worker.get("last_run_id") or ""),
        expected_state="running",
        expected_container_id="",
        owner="other-control",
        ttl_s=300,
        kind="pause_run",
        target_run_id=record["run_id"],
        expected_target_started_at=started_at,
    )
    assert claim

    response = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json={"action": "stop", "idempotencyKey": "action-stop-other-claim"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "active_work_stop_not_accepted"
    with sqlite3.connect(store.db_path) as conn:
        action_status = conn.execute(
            "SELECT status FROM active_work_action_uses WHERE work_ref = ?",
            (accepted["workRef"],),
        ).fetchone()[0]
    assert action_status != "completed"
    assert store.get_run(record["run_id"])["state"] == "running"
    durable_worker = store.get_worker(record["worker_id"])
    assert durable_worker["compute_release_kind"] == "pause_run"
    assert not durable_worker["work_stop_id"]


def test_active_work_action_concurrent_replay_stays_pending_and_changed_request_conflicts(
    account_client,
):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-action-race"),
        json=delegation_payload(title="Action race mission"),
    ).json()
    service = account_client.app.state.service
    original_execute = service.execute_active_work_action
    entered = threading.Event()
    release = threading.Event()

    def blocked_execute(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return original_execute(*args, **kwargs)

    service.execute_active_work_action = blocked_execute
    result: dict[str, object] = {}

    def first_request():
        result["response"] = account_client.post(
            f"/v1/work/{accepted['workRef']}/actions",
            headers=account_headers(),
            json={
                "action": "queue",
                "instruction": "First exact follow-up.",
                "idempotencyKey": "action-race-shared",
            },
        )

    thread = threading.Thread(target=first_request)
    thread.start()
    assert entered.wait(timeout=5), getattr(result.get("response"), "text", result)
    replay = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json={
            "action": "queue",
            "instruction": "First exact follow-up.",
            "idempotencyKey": "action-race-shared",
        },
    )
    changed = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json={
            "action": "queue",
            "instruction": "Different follow-up must conflict.",
            "idempotencyKey": "action-race-shared",
        },
    )
    release.set()
    thread.join(timeout=5)
    service.execute_active_work_action = original_execute

    assert replay.status_code == 409
    assert replay.json()["detail"]["code"] == "active_work_action_in_progress"
    assert changed.status_code == 409
    assert changed.json()["detail"]["code"] == "active_work_idempotency_conflict"
    assert result["response"].status_code == 202


def test_definitive_action_failure_replays_the_same_receipt_without_reexecution(
    account_client, monkeypatch
):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-action-definitive-failure"),
        json=delegation_payload(title="Definitive action failure"),
    ).json()
    service = account_client.app.state.service
    calls = 0

    def generation_changed(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise RuntimeError("active_work_generation_changed")

    monkeypatch.setattr(service, "execute_active_work_action", generation_changed)
    body = {
        "action": "pause",
        "idempotencyKey": "action-definitive-failure-shared",
    }

    first = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json=body,
    )
    replay = account_client.post(
        f"/v1/work/{accepted['workRef']}/actions",
        headers=account_headers(),
        json=body,
    )

    assert first.status_code == 409
    assert replay.status_code == 409
    assert replay.json() == first.json()
    assert first.json()["detail"] == {
        "code": "active_work_generation_changed",
        "message": "active work generation changed",
    }
    assert calls == 1


@pytest.mark.parametrize("action", ["steer", "pause", "resume"])
def test_active_work_action_recovers_crash_between_internal_subeffects(
    tmp_path,
    monkeypatch,
    action,
):
    """A committed first subeffect must be finished, never repeated, after restart."""

    monkeypatch.setenv("WPR_API_TOKEN", API_TOKEN)
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_SERVICE_ASSERTION_SECRET", ASSERTION_SECRET)
    db_path = str(tmp_path / f"action-internal-crash-{action}.sqlite3")
    runtime = StubRuntime()
    runtime_calls = {"interrupt": 0, "pause": 0, "resume": 0}
    original_interrupt = runtime.interrupt_worker
    original_pause = runtime.pause_worker
    original_resume = runtime.ensure_worker_ready

    def counted_interrupt(*args, **kwargs):
        runtime_calls["interrupt"] += 1
        return original_interrupt(*args, **kwargs)

    def counted_pause(*args, **kwargs):
        runtime_calls["pause"] += 1
        return original_pause(*args, **kwargs)

    def counted_resume(*args, **kwargs):
        runtime_calls["resume"] += 1
        return original_resume(*args, **kwargs)

    runtime.interrupt_worker = counted_interrupt
    runtime.pause_worker = counted_pause
    runtime.ensure_worker_ready = counted_resume
    app = create_app(
        db_path=db_path,
        runtime_backend="stub",
        runtime=runtime,
        reconcile_on_startup=False,
    )
    app.state.service.start_assigned_run = lambda _worker_id: None
    app.state.service._ensure_worker_processor = lambda _worker_id: None
    with TestClient(app) as client:
        accepted = client.post(
            "/v1/delegations",
            headers=account_headers(idempotency_key=f"delegation-internal-{action}"),
            json=delegation_payload(title=f"Internal {action} crash"),
        ).json()
        store = app.state.store
        record = store.get_delegation(
            accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
        )
        run_id = record["current_run_id"]
        if action in {"steer", "pause"}:
            assert store.transition_run_if_state(
                run_id,
                "queued",
                "running",
                started_at=datetime.now(timezone.utc).isoformat(),
            )
            store.update_worker_state(record["worker_id"], "running", last_error="")
        else:
            assert store.transition_run_if_state(
                run_id,
                "queued",
                "paused",
                error_text="Paused",
                started_at=datetime.now(timezone.utc).isoformat(),
            )
            store.update_worker_state(record["worker_id"], "paused", last_error="")

        instruction = "Use the corrected objective." if action == "steer" else ""
        action_request = {
            "action": action,
            "instruction": instruction,
            "capabilityReauthorization": None,
        }
        reservation = store.reserve_active_work_action(
            tenant_id="tenant-a",
            owner_id="owner-a",
            work_ref=accepted["workRef"],
            idempotency_key=f"action-internal-{action}",
            action=action,
            payload_digest=hashlib.sha256(
                json.dumps(
                    action_request,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest(),
            executor_id=app.state.service.executor_id,
        )

        original_run_finalizer = store.finalize_worker_run_control_claim
        original_steer_finalizer = store.finalize_worker_steer_claim

        def crash_run_finalizer(*args, **kwargs):
            raise SystemExit("simulated crash after runtime receipt and before run finalizer")

        def crash_steer_finalizer(*args, **kwargs):
            raise SystemExit("simulated crash after runtime receipt and before steer finalizer")

        if action == "steer":
            store.finalize_worker_steer_claim = crash_steer_finalizer
        else:
            store.finalize_worker_run_control_claim = crash_run_finalizer
        with pytest.raises(SystemExit):
            app.state.service.execute_active_work_action(
                record,
                action=action,
                instruction=instruction,
                idempotency_key=f"action-internal-{action}",
                action_use_id=reservation["action_use_id"],
            )
        store.finalize_worker_steer_claim = original_steer_finalizer
        store.finalize_worker_run_control_claim = original_run_finalizer

        with sqlite3.connect(store.db_path) as conn:
            pending = conn.execute(
                "SELECT status, effect_phase FROM active_work_action_uses WHERE action_use_id = ?",
                (reservation["action_use_id"],),
            ).fetchone()
        assert pending[0] == "pending"
        assert pending[1] or (
            store.get_worker(record["worker_id"])["compute_release_runtime_confirmed_at"]
        )
        # A different process may take over only after both durable owner
        # leases expire. The runtime receipt then lets it finalize without
        # repeating the provider action.
        with sqlite3.connect(store.db_path) as conn:
            conn.execute(
                "UPDATE active_work_action_uses SET lease_expires_at = ? WHERE action_use_id = ?",
                ("2000-01-01T00:00:00+00:00", reservation["action_use_id"]),
            )
            conn.execute(
                "UPDATE workers SET compute_release_expires_at = ? WHERE worker_id = ?",
                ("2000-01-01T00:00:00+00:00", record["worker_id"]),
            )

    restarted = create_app(
        db_path=db_path,
        runtime_backend="stub",
        runtime=runtime,
        reconcile_on_startup=False,
    )
    restarted.state.service.start_assigned_run = lambda _worker_id: None
    restarted.state.service._ensure_worker_processor = lambda _worker_id: None
    with TestClient(restarted) as client:
        body = {
            "action": action,
            "idempotencyKey": f"action-internal-{action}",
        }
        if instruction:
            body["instruction"] = instruction
        replay = client.post(
            f"/v1/work/{accepted['workRef']}/actions",
            headers=account_headers(),
            json=body,
        )
        assert replay.status_code == 202, replay.text
        assert replay.json()["idempotentReplay"] is True
        if replay.json()["confirmationPending"]:
            # Startup recovery and the replay request may race to claim the
            # same expired operation.  The public contract truthfully returns
            # the exact bound pending receipt; wait for that owner to settle,
            # then prove the same key converges without another runtime call.
            for _ in range(200):
                if not restarted.state.store.get_worker(record["worker_id"])[
                    "compute_release_token"
                ]:
                    break
                time.sleep(0.01)
            replay = client.post(
                f"/v1/work/{accepted['workRef']}/actions",
                headers=account_headers(),
                json=body,
            )
            assert replay.status_code == 202, replay.text
            assert replay.json()["idempotentReplay"] is True
        assert replay.json()["confirmationPending"] is False
        durable = restarted.state.store.get_run(run_id)
        assert durable["state"] == (
            "interrupted" if action == "steer" else "paused" if action == "pause" else "running"
        ) or (action == "resume" and durable["state"] == "queued")
        if action == "steer":
            worker_runs = restarted.state.store.list_runs_for_worker(record["worker_id"])
            assert len(worker_runs) == 2
            assert runtime_calls["interrupt"] == 1
        elif action == "pause":
            assert runtime_calls["pause"] == 1
        else:
            assert runtime_calls["resume"] == 1


@pytest.mark.parametrize("action", ["pause", "resume", "steer", "stop"])
def test_control_claim_and_action_binding_survive_immediate_process_exit(
    tmp_path,
    monkeypatch,
    action,
):
    """Claim publication and receipt binding are one durable transaction."""

    class TrackingRuntime(StubRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.pause_calls = 0
            self.resume_calls = 0
            self.interrupt_calls = 0

        def pause_worker(self, worker):
            self.pause_calls += 1
            return super().pause_worker(worker)

        def ensure_worker_ready(self, worker):
            self.resume_calls += 1
            return super().ensure_worker_ready(worker)

        def interrupt_worker(self, worker, run_id=None):
            self.interrupt_calls += 1
            return super().interrupt_worker(worker, run_id=run_id)

    monkeypatch.setenv("WPR_API_TOKEN", API_TOKEN)
    monkeypatch.setenv(
        "VIVENTIUM_GLASSHIVE_SERVICE_ASSERTION_SECRET", ASSERTION_SECRET
    )
    monkeypatch.setattr(
        WorkersProjectsService,
        "_scheduler_loop",
        lambda self: self._shutdown_event.wait(),
    )
    db_path = str(tmp_path / f"atomic-claim-binding-{action}.sqlite3")
    runtime = TrackingRuntime()
    app = create_app(
        db_path=db_path,
        runtime_backend="stub",
        runtime=runtime,
        reconcile_on_startup=False,
    )
    app.state.service.start_assigned_run = lambda _worker_id: None
    app.state.service._ensure_worker_processor = lambda _worker_id: None
    instruction = "Use the atomic replacement objective." if action == "steer" else ""
    idempotency_key = f"action-atomic-claim-binding-{action}"
    with TestClient(app) as client:
        accepted = client.post(
            "/v1/delegations",
            headers=account_headers(
                idempotency_key=f"delegation-atomic-claim-binding-{action}"
            ),
            json=delegation_payload(title=f"Atomic claim binding {action}"),
        ).json()
        store = app.state.store
        source = store.get_delegation(
            accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
        )
        if action == "resume":
            assert store.transition_run_if_state(
                source["run_id"], "queued", "paused"
            )
            store.update_worker_state(source["worker_id"], "paused", last_error="")
        source = store.get_delegation(
            accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
        )
        action_request = {
            "action": action,
            "instruction": instruction,
            "capabilityReauthorization": None,
        }
        reservation = store.reserve_active_work_action(
            tenant_id="tenant-a",
            owner_id="owner-a",
            work_ref=accepted["workRef"],
            idempotency_key=idempotency_key,
            action=action,
            payload_digest=hashlib.sha256(
                json.dumps(
                    action_request,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest(),
            expected_current_run_id=str(source["current_run_id"]),
            expected_source_run_id=str(source["run_id"]),
            expected_source_state=str(source["run_state"]),
            expected_source_started_at=str(source["run_started_at"] or ""),
            executor_id=app.state.service.executor_id,
        )

        if action == "stop":
            original_claim = store.try_claim_worker_compute_release

            def crash_after_claim(*args, **kwargs):
                claim = original_claim(*args, **kwargs)
                assert claim
                raise SystemExit("simulated exit immediately after claim transaction")

            store.try_claim_worker_compute_release = crash_after_claim
        else:
            original_claim = app.state.service._claim_exact_run_control

            def crash_after_claim(*args, **kwargs):
                claim = original_claim(*args, **kwargs)
                assert claim
                raise SystemExit("simulated exit immediately after claim transaction")

            app.state.service._claim_exact_run_control = crash_after_claim
        with pytest.raises(SystemExit):
            app.state.service.execute_active_work_action(
                source,
                action=action,
                instruction=instruction,
                idempotency_key=idempotency_key,
                action_use_id=reservation["action_use_id"],
            )
        action_row = store.get_active_work_action(reservation["action_use_id"])
        claimed_worker = store.get_worker(source["worker_id"])
        assert action_row["status"] == "pending"
        assert action_row["lifecycle_operation_id"]
        assert (
            action_row["lifecycle_operation_id"]
            == claimed_worker["compute_release_operation_id"]
        )
        assert action_row["lifecycle_target_run_id"] == source["run_id"]
        with sqlite3.connect(store.db_path) as conn:
            conn.execute(
                "UPDATE active_work_action_uses SET lease_expires_at = ? WHERE action_use_id = ?",
                ("2000-01-01T00:00:00+00:00", reservation["action_use_id"]),
            )
            conn.execute(
                "UPDATE workers SET compute_release_expires_at = ? WHERE worker_id = ?",
                ("2000-01-01T00:00:00+00:00", source["worker_id"]),
            )

    restarted = create_app(
        db_path=db_path,
        runtime_backend="stub",
        runtime=runtime,
        reconcile_on_startup=False,
    )
    restarted.state.service.start_assigned_run = lambda _worker_id: None
    restarted.state.service._ensure_worker_processor = lambda _worker_id: None
    with TestClient(restarted) as client:
        body = {"action": action, "idempotencyKey": idempotency_key}
        if instruction:
            body["instruction"] = instruction
        replay = client.post(
            f"/v1/work/{accepted['workRef']}/actions",
            headers=account_headers(),
            json=body,
        )
        assert replay.status_code == 202, replay.text
        assert replay.json()["idempotentReplay"] is True
        if replay.json()["confirmationPending"]:
            for _ in range(200):
                if not restarted.state.store.get_worker(source["worker_id"])[
                    "compute_release_token"
                ]:
                    break
                time.sleep(0.01)
            replay = client.post(
                f"/v1/work/{accepted['workRef']}/actions",
                headers=account_headers(),
                json=body,
            )
        repeated = client.post(
            f"/v1/work/{accepted['workRef']}/actions",
            headers=account_headers(),
            json=body,
        )

        assert replay.status_code == 202, replay.text
        assert replay.json()["confirmationPending"] is False
        assert replay.json()["idempotentReplay"] is True
        assert repeated.json() == replay.json()
        assert restarted.state.store.get_active_work_action(
            reservation["action_use_id"]
        )["status"] == "completed"
        assert not restarted.state.store.get_worker(source["worker_id"])[
            "compute_release_token"
        ]
        if action == "pause":
            assert replay.json()["state"] == "paused"
            assert runtime.pause_calls == 0
        elif action == "resume":
            assert replay.json()["state"] in {"queued", "running"}
            assert runtime.resume_calls == 1
        elif action == "steer":
            assert replay.json()["state"] == "queued"
            assert runtime.interrupt_calls == 0
        else:
            assert replay.json()["state"] == "cancelled"
            assert runtime.interrupt_calls == 0


def test_legacy_active_work_action_schema_migrates_source_run_receipt(tmp_path):
    db_path = tmp_path / "legacy-action-ledger.sqlite3"
    app = create_app(db_path=str(db_path), runtime_backend="stub", runtime=StubRuntime())
    app.state.service.shutdown()
    with sqlite3.connect(db_path) as conn:
        conn.execute("ALTER TABLE active_work_action_uses DROP COLUMN source_run_id")
        conn.execute("ALTER TABLE active_work_action_uses DROP COLUMN effect_phase")

    migrated = create_app(
        db_path=str(db_path), runtime_backend="stub", runtime=StubRuntime()
    )
    migrated.state.service.shutdown()
    with sqlite3.connect(db_path) as conn:
        columns = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(active_work_action_uses)"
            ).fetchall()
        }
    assert "source_run_id" in columns
    assert "effect_phase" in columns


def test_legacy_pending_control_receipt_without_operation_proof_fails_closed(
    tmp_path,
    monkeypatch,
):
    """Migration defaults must not turn unrelated paused state into replay success."""

    monkeypatch.setenv("WPR_API_TOKEN", API_TOKEN)
    monkeypatch.setenv(
        "VIVENTIUM_GLASSHIVE_SERVICE_ASSERTION_SECRET", ASSERTION_SECRET
    )
    db_path = str(tmp_path / "legacy-control-operation-receipt.sqlite3")
    app = create_app(
        db_path=db_path,
        runtime_backend="stub",
        runtime=StubRuntime(),
        reconcile_on_startup=False,
    )
    app.state.service.start_assigned_run = lambda _worker_id: None
    app.state.service._ensure_worker_processor = lambda _worker_id: None
    with TestClient(app) as client:
        accepted = client.post(
            "/v1/delegations",
            headers=account_headers(
                idempotency_key="delegation-legacy-control-operation"
            ),
            json=delegation_payload(title="Legacy operation receipt"),
        ).json()
        store = app.state.store
        source = store.get_delegation(
            accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
        )
        action_request = {
            "action": "pause",
            "instruction": "",
            "capabilityReauthorization": None,
        }
        reservation = store.reserve_active_work_action(
            tenant_id="tenant-a",
            owner_id="owner-a",
            work_ref=accepted["workRef"],
            idempotency_key="action-legacy-control-operation",
            action="pause",
            payload_digest=hashlib.sha256(
                json.dumps(
                    action_request,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest(),
            expected_current_run_id=str(source["current_run_id"]),
            expected_source_run_id=str(source["run_id"]),
            expected_source_state=str(source["run_state"]),
            expected_source_started_at=str(source["run_started_at"] or ""),
            executor_id=app.state.service.executor_id,
        )
        # This paused state is deliberately not causally owned by the receipt.
        assert store.transition_run_if_state(source["run_id"], "queued", "paused")
        store.update_worker_state(source["worker_id"], "paused", last_error="")

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "ALTER TABLE active_work_action_uses DROP COLUMN lifecycle_operation_id"
        )
        conn.execute(
            "ALTER TABLE active_work_action_uses DROP COLUMN lifecycle_operation_kind"
        )
        conn.execute(
            "ALTER TABLE active_work_action_uses DROP COLUMN lifecycle_target_run_id"
        )

    migrated = create_app(
        db_path=db_path,
        runtime_backend="stub",
        runtime=StubRuntime(),
        reconcile_on_startup=False,
    )
    migrated.state.service.start_assigned_run = lambda _worker_id: None
    migrated.state.service._ensure_worker_processor = lambda _worker_id: None
    with TestClient(migrated):
        migrated_action = migrated.state.store.get_active_work_action(
            reservation["action_use_id"]
        )
        assert migrated_action["status"] == "pending"
        assert migrated_action["lifecycle_operation_id"] == ""
        assert migrated_action["lifecycle_operation_kind"] == ""
        assert migrated_action["lifecycle_target_run_id"] == ""
        current = migrated.state.store.get_delegation(
            accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
        )
        assert migrated.state.service.reconcile_active_work_action(
            current,
            action="pause",
            idempotency_key="action-legacy-control-operation",
            source_run_id=source["run_id"],
            action_use_id=reservation["action_use_id"],
        ) is None


@pytest.mark.parametrize("action", ["pause", "resume", "steer"])
def test_unbound_control_receipt_pending_and_failed_replays_require_reissue(
    tmp_path,
    monkeypatch,
    action,
):
    """An unbound legacy control receipt never infers or re-executes an effect."""

    class TrackingRuntime(StubRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.pause_calls = 0
            self.resume_calls = 0
            self.interrupt_calls = 0

        def pause_worker(self, worker):
            self.pause_calls += 1
            return super().pause_worker(worker)

        def ensure_worker_ready(self, worker):
            self.resume_calls += 1
            return super().ensure_worker_ready(worker)

        def interrupt_worker(self, worker, run_id=None):
            self.interrupt_calls += 1
            return super().interrupt_worker(worker, run_id=run_id)

    monkeypatch.setenv("WPR_API_TOKEN", API_TOKEN)
    monkeypatch.setenv(
        "VIVENTIUM_GLASSHIVE_SERVICE_ASSERTION_SECRET", ASSERTION_SECRET
    )
    monkeypatch.setattr(
        WorkersProjectsService,
        "_scheduler_loop",
        lambda self: self._shutdown_event.wait(),
    )
    db_path = str(tmp_path / f"unbound-{action}-receipt.sqlite3")
    runtime = TrackingRuntime()
    app = create_app(
        db_path=db_path,
        runtime_backend="stub",
        runtime=runtime,
        reconcile_on_startup=False,
    )
    app.state.service.start_assigned_run = lambda _worker_id: None
    app.state.service._ensure_worker_processor = lambda _worker_id: None
    instruction = "Use the replacement objective." if action == "steer" else ""
    idempotency_key = f"action-unbound-{action}"
    with TestClient(app) as client:
        accepted = client.post(
            "/v1/delegations",
            headers=account_headers(idempotency_key=f"delegation-unbound-{action}"),
            json=delegation_payload(title=f"Unbound {action} receipt"),
        ).json()
        store = app.state.store
        source = store.get_delegation(
            accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
        )
        if action == "resume":
            assert store.transition_run_if_state(
                source["run_id"], "queued", "paused"
            )
            store.update_worker_state(source["worker_id"], "paused", last_error="")
        elif action == "steer":
            started_at = datetime.now(timezone.utc).isoformat()
            assert store.transition_run_if_state(
                source["run_id"], "queued", "running", started_at=started_at
            )
            store.update_worker_state(source["worker_id"], "running", last_error="")
        source = store.get_delegation(
            accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
        )
        action_request = {
            "action": action,
            "instruction": instruction,
            "capabilityReauthorization": None,
        }
        reservation = store.reserve_active_work_action(
            tenant_id="tenant-a",
            owner_id="owner-a",
            work_ref=accepted["workRef"],
            idempotency_key=idempotency_key,
            action=action,
            payload_digest=hashlib.sha256(
                json.dumps(
                    action_request,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest(),
            expected_current_run_id=str(source["current_run_id"]),
            expected_source_run_id=str(source["run_id"]),
            expected_source_state=str(source["run_state"]),
            expected_source_started_at=str(source["run_started_at"] or ""),
            executor_id=app.state.service.executor_id,
        )
        if action == "pause":
            assert store.transition_run_if_state(
                source["run_id"], "queued", "paused"
            )
            store.update_worker_state(source["worker_id"], "paused", last_error="")
        elif action == "resume":
            assert store.transition_run_if_state(
                source["run_id"],
                "paused",
                "running",
                started_at=datetime.now(timezone.utc).isoformat(),
            )
            store.update_worker_state(source["worker_id"], "running", last_error="")
        else:
            replacement_run_id = app.state.service.active_work_effect_run_id(
                source, idempotency_key=idempotency_key
            )
            store.create_idempotent_run(
                run_id=replacement_run_id,
                worker_id=source["worker_id"],
                project_id=source["project_id"],
                instruction=app.state.service._instruction_for_steer(instruction),
            )
            assert store.transition_run_if_state(
                source["run_id"], "running", "interrupted"
            )
        assert app.state.service.reconcile_active_work_action(
            source,
            action=action,
            instruction=instruction,
            idempotency_key=idempotency_key,
            source_run_id=source["run_id"],
            action_use_id=reservation["action_use_id"],
        ) is None
        with sqlite3.connect(store.db_path) as conn:
            conn.execute(
                "UPDATE active_work_action_uses SET lease_expires_at = ? WHERE action_use_id = ?",
                ("2000-01-01T00:00:00+00:00", reservation["action_use_id"]),
            )
        before_states = {
            run["run_id"]: run["state"]
            for run in store.list_runs_for_worker(source["worker_id"])
        }

    restarted = create_app(
        db_path=db_path,
        runtime_backend="stub",
        runtime=runtime,
        reconcile_on_startup=False,
    )
    restarted.state.service.start_assigned_run = lambda _worker_id: None
    restarted.state.service._ensure_worker_processor = lambda _worker_id: None
    with TestClient(restarted) as client:
        body = {"action": action, "idempotencyKey": idempotency_key}
        if instruction:
            body["instruction"] = instruction
        pending_replay = client.post(
            f"/v1/work/{accepted['workRef']}/actions",
            headers=account_headers(),
            json=body,
        )
        failed_replay = client.post(
            f"/v1/work/{accepted['workRef']}/actions",
            headers=account_headers(),
            json=body,
        )

        assert pending_replay.status_code == 409
        assert failed_replay.status_code == 409
        assert pending_replay.json()["detail"]["code"] == (
            "active_work_action_binding_unavailable"
        )
        assert failed_replay.json()["detail"] == pending_replay.json()["detail"]
        action_row = restarted.state.store.get_active_work_action(
            reservation["action_use_id"]
        )
        assert action_row["status"] == "failed"
        assert action_row["last_error"] == "active_work_action_binding_unavailable"
        assert {
            run["run_id"]: run["state"]
            for run in restarted.state.store.list_runs_for_worker(source["worker_id"])
        } == before_states
        assert runtime.pause_calls == 0
        assert runtime.resume_calls == 0
        assert runtime.interrupt_calls == 0


def test_active_work_roster_caps_rows_and_reports_overflow(account_client):
    for index in range(3):
        account_client.post(
            "/v1/delegations",
            headers=account_headers(idempotency_key=f"delegation-overflow-{index}"),
            json=delegation_payload(title=f"Overflow {index}"),
        )

    roster = account_client.get("/v1/active-work?limit=2", headers=account_headers())

    assert roster.status_code == 200
    assert roster.json()["snapshot"] == "fresh"
    assert len(roster.json()["work"]) == 2
    assert roster.json()["overflowCount"] == 1


def test_active_work_cursor_pagination_is_stable_and_complete(account_client):
    for index in range(4):
        response = account_client.post(
            "/v1/delegations",
            headers=account_headers(idempotency_key=f"delegation-cursor-{index}"),
            json=delegation_payload(title=f"Cursor {index}"),
        )
        assert response.status_code == 202

    # Force identical timestamps so the opaque cursor must use workRef as its
    # deterministic final key, not rely on insertion-order accidents.
    with sqlite3.connect(account_client.app.state.store.db_path) as conn:
        conn.execute(
            "UPDATE delegations SET updated_at = ?, created_at = ?",
            ("2026-08-12T12:00:00+00:00", "2026-08-12T12:00:00+00:00"),
        )

    collected: list[str] = []
    cursor: str | None = None
    remaining = [3, 2, 1, 0]
    for expected_overflow in remaining:
        suffix = f"&cursor={cursor}" if cursor else ""
        page = account_client.get(
            f"/v1/active-work?limit=1{suffix}",
            headers=account_headers(),
        )
        assert page.status_code == 200
        body = page.json()
        assert len(body["work"]) == 1
        assert body["overflowCount"] == expected_overflow
        collected.append(body["work"][0]["workRef"])
        cursor = body.get("cursor")
        assert bool(cursor) is (expected_overflow > 0)

    assert len(collected) == len(set(collected)) == 4

    invalid = account_client.get(
        "/v1/active-work?limit=1&cursor=not-a-valid-cursor",
        headers=account_headers(),
    )
    assert invalid.status_code == 400
    assert invalid.json()["detail"]["code"] == "active_work_cursor_invalid"


def test_callback_association_is_authoritative_owner_scoped_and_non_oracular(account_client):
    service = account_client.app.state.service
    service.executor.submit = lambda *_args, **_kwargs: None
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-callback-association"),
        json=delegation_payload_with_origin(),
    )
    assert accepted.status_code == 202

    store = account_client.app.state.store
    delegation = store.get_delegation(
        accepted.json()["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    initial_run_id = delegation["initial_run_id"]
    request_body = {
        "originRef": "ghi_synthetic_origin_0001",
        "workRef": accepted.json()["workRef"],
        "workerId": delegation["worker_id"],
        "runId": initial_run_id,
    }
    verified = account_client.post(
        "/v1/callback-associations/verify",
        headers=account_headers(),
        json=request_body,
    )
    assert verified.status_code == 200
    assert verified.json() == {
        "valid": True,
        "originRef": "ghi_synthetic_origin_0001",
        "workRef": accepted.json()["workRef"],
    }

    linked_run = store.create_run(
        delegation["worker_id"], delegation["project_id"], "Linked continuation"
    )
    linked = account_client.post(
        "/v1/callback-associations/verify",
        headers=account_headers(),
        json={**request_body, "runId": linked_run["run_id"]},
    )
    assert linked.status_code == 200

    mismatches = [
        {**request_body, "originRef": "ghi_synthetic_origin_wrong"},
        {**request_body, "workRef": "work_synthetic_wrong"},
        {**request_body, "workerId": "wrk_synthetic_wrong"},
        {**request_body, "runId": "run_synthetic_wrong"},
    ]
    for mismatch in mismatches:
        response = account_client.post(
            "/v1/callback-associations/verify",
            headers=account_headers(),
            json=mismatch,
        )
        assert response.status_code == 404
        assert response.json() == {
            "detail": {
                "code": "callback_association_not_found",
                "message": "The callback association was not found.",
            }
        }

    foreign = account_client.post(
        "/v1/callback-associations/verify",
        headers=account_headers(owner_id="owner-b"),
        json=request_body,
    )
    assert foreign.status_code == 404
    assert foreign.json() == {
        "detail": {
            "code": "callback_association_not_found",
            "message": "The callback association was not found.",
        }
    }

    with sqlite3.connect(store.db_path) as conn:
        row = conn.execute(
            "SELECT payload_json FROM callback_outbox ORDER BY created_at ASC LIMIT 1"
        ).fetchone()
    callback_payload = json.loads(row[0])
    assert callback_payload["origin_ref"] == "ghi_synthetic_origin_0001"
    assert callback_payload["work_ref"] == accepted.json()["workRef"]


def test_viventium_callback_uses_durable_origin_binding_without_parent_identity(account_client):
    service = account_client.app.state.service
    service.executor.submit = lambda *_args, **_kwargs: None
    payload = delegation_payload_with_origin(
        origin_ref="ghi_synthetic_opaque_callback_origin",
        title="Opaque callback routing",
    )
    payload["bootstrapBundle"]["callbacks"]["events_webhook_url"] = (
        "http://localhost:3080/api/viventium/glasshive/callback"
    )
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-opaque-callback-routing"),
        json=payload,
    )
    assert accepted.status_code == 202, accepted.text

    store = account_client.app.state.store
    delegation = store.get_delegation(
        accepted.json()["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    worker = store.get_worker(delegation["worker_id"])
    run = store.get_run(delegation["initial_run_id"])

    record = service._emit_callback(
        worker,
        "run.needs_input",
        run=run,
        message="Connected account authorization is required.",
        submit_delivery=False,
    )

    assert record is not None
    callback_payload = json.loads(record["payload_json"])
    assert callback_payload["origin_ref"] == "ghi_synthetic_opaque_callback_origin"
    assert callback_payload["work_ref"] == accepted.json()["workRef"]
    assert callback_payload["user_id"] is None
    assert callback_payload["conversation_id"] is None
    assert callback_payload["parent_message_id"] is None
    assert callback_payload["message_id"] is None

    forged_worker = dict(worker)
    forged_bundle = json.loads(forged_worker["bootstrap_bundle_json"])
    forged_bundle["callbacks"]["origin_ref"] = "ghi_synthetic_foreign_origin"
    forged_worker["bootstrap_bundle_json"] = json.dumps(forged_bundle)
    assert (
        service._emit_callback(
            forged_worker,
            "run.failed",
            run=run,
            message="This callback is not bound to the durable delegation.",
            submit_delivery=False,
        )
        is None
    )


def test_delegation_rejects_conflicting_explicit_and_callback_origin_refs(account_client):
    payload = delegation_payload_with_origin(origin_ref="ghi_synthetic_origin_callback")
    payload["originRef"] = "ghi_synthetic_origin_explicit"
    response = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-origin-conflict"),
        json=payload,
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "delegation_origin_ref_conflict"


def test_delegation_can_be_reconciled_by_origin_without_identity_oracle(account_client):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-origin-reconcile"),
        json=delegation_payload_with_origin(origin_ref="ghi_synthetic_reconcile_0001"),
    )
    assert accepted.status_code == 202

    found = account_client.get(
        "/v1/delegations/by-origin/ghi_synthetic_reconcile_0001",
        headers=account_headers(),
    )
    foreign = account_client.get(
        "/v1/delegations/by-origin/ghi_synthetic_reconcile_0001",
        headers=account_headers(owner_id="owner-b"),
    )
    absent = account_client.get(
        "/v1/delegations/by-origin/ghi_synthetic_reconcile_absent",
        headers=account_headers(),
    )

    assert found.status_code == 200
    assert found.json() == {
        "workRef": accepted.json()["workRef"],
        "state": "accepted",
    }
    for response in (foreign, absent):
        assert response.status_code == 404
        assert response.json() == {
            "detail": {
                "code": "delegation_not_found",
                "message": "The delegation was not found.",
            }
        }


def test_service_assertion_cross_language_canonical_vector():
    # Fixed vector shared with the Core signer: JSON keys are UTF-8 canonical
    # lexicographic order with no insignificant whitespace.
    secret = "synthetic-cross-language-secret"
    claims = {
        "aud": "glasshive-account-api",
        "exp": 1786543260,
        "iat": 1786543200,
        "nonce": "nonce_cross_language_0001",
        "owner_id": "owner-synthetic",
        "tenant_id": "tenant-synthetic",
        "v": 1,
    }
    canonical = json.dumps(
        claims, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    payload_segment = _b64url(canonical)
    signature = _b64url(
        hmac.new(secret.encode(), payload_segment.encode(), hashlib.sha256).digest()
    )
    vector = f"{payload_segment}.{signature}"

    assert vector == (
        "eyJhdWQiOiJnbGFzc2hpdmUtYWNjb3VudC1hcGkiLCJleHAiOjE3ODY1NDMyNjAsImlhdCI6"
        "MTc4NjU0MzIwMCwibm9uY2UiOiJub25jZV9jcm9zc19sYW5ndWFnZV8wMDAxIiwib3duZXJf"
        "aWQiOiJvd25lci1zeW50aGV0aWMiLCJ0ZW5hbnRfaWQiOiJ0ZW5hbnQtc3ludGhldGljIiwidiI6MX0."
        "sw7mRhWFP9xCoWtZpOzZKq65w3itqWE4-YsIpe92FNM"
    )
    assert verify_service_assertion(vector, secret=secret, now_epoch=1786543201) == claims


@pytest.mark.parametrize(
    ("method", "suffix", "json_body"),
    [
        ("post", "assign", {"instruction": "malicious assign"}),
        ("post", "message", {"message": "malicious message"}),
        ("post", "interrupt", None),
        ("post", "pause", None),
        ("post", "resume", None),
        ("post", "terminate", None),
        ("post", "desktop-action", {"action": "terminal"}),
    ],
)
def test_worker_view_token_cannot_call_legacy_mutations(
    account_client,
    method,
    suffix,
    json_body,
):
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key=f"delegation-view-abuse-{suffix}"),
        json=delegation_payload(title=f"View abuse {suffix}"),
    ).json()
    store = account_client.app.state.store
    delegation = store.get_delegation(
        accepted["workRef"], tenant_id="tenant-a", owner_id="owner-a"
    )
    params = sign_link_params(
        kind="worker_view",
        worker_id=delegation["worker_id"],
        tenant_id="tenant-a",
        owner_id="owner-a",
    )

    response = getattr(account_client, method)(
        f"/v1/workers/{delegation['worker_id']}/{suffix}",
        params=params,
        json=json_body,
    )

    assert response.status_code in {401, 403}
    assert store.get_worker(delegation["worker_id"])["state"] == "created"
    assert len(store.list_runs_for_worker(delegation["worker_id"])) == 1


def test_public_view_ref_is_absolute_read_only_and_cannot_control_workspace(account_client, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_OPERATOR_BASE_URL", "https://glasshive.example.test")
    accepted = account_client.post(
        "/v1/delegations",
        headers=account_headers(idempotency_key="delegation-read-only-view"),
        json=delegation_payload(title="Read-only view mission"),
    ).json()
    view_ref = accepted["viewRef"]
    parsed = urlsplit(view_ref)

    assert parsed.scheme == "https"
    assert parsed.netloc == "glasshive.example.test"
    assert parsed.path.startswith("/w/ghr_")

    view = account_client.get(parsed.path)
    ref_id = parsed.path.rsplit("/", 1)[-1]
    terminate = account_client.post(f"/w/{ref_id}/actions/terminate")
    desktop_action = account_client.post(
        f"/w/{ref_id}/desktop-action",
        json={"action": "terminal"},
    )

    assert view.status_code == 200
    assert "Read-only mission view" in view.text
    assert "Terminate" not in view.text
    assert "Pause" not in view.text
    assert terminate.status_code == 403
    assert desktop_action.status_code == 403
