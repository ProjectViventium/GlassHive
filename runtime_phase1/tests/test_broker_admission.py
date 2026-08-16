from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

import httpx
import pytest

from workers_projects_runtime.broker_admission import (
    BrokerAdmissionError,
    admit_capability_grant,
    mint_admission_header,
    revoke_capability_grant,
)
from workers_projects_runtime.openclaw_runtime import RuntimeInfo, StubRuntime
from workers_projects_runtime.service import WorkersProjectsService
from workers_projects_runtime.store import Store


BODY = {
    "authorizationRef": "gha_synthetic_authorization_0001",
    "containerGenerationId": "a" * 64,
    "originRef": "ghi_synthetic_origin_0001",
    "runId": "run_synthetic_0001",
    "workRef": "work_synthetic_0001",
    "workerId": "wrk_synthetic_0001",
}
SECRET = "synthetic-admission-secret"


def _success_body(**overrides):
    payload = {
        "status": "authorized",
        **BODY,
        "scopeFingerprint": "scope_synthetic_0001",
        "brokerUrl": "http://127.0.0.1:3180/api/viventium/glasshive/capabilities/mcp",
        "grantToken": "synthetic-run-local-grant",
        "grant": {
            "grantId": "grant_synthetic_0001",
            "expiresAt": int(time.time()) + 60,
            "allowedServers": ["google_workspace"],
            "allowedHostTools": ["browser"],
            "scopes": {"content_read": True},
        },
        "maxExpiresAt": "2099-01-01T00:00:00+00:00",
    }
    payload.update(overrides)
    return payload


def test_admission_header_cross_language_vector_is_canonical_base64url_hmac():
    header = mint_admission_header(
        BODY,
        secret=SECRET,
        timestamp=1_786_543_200,
        nonce="nonce_synthetic_0001",
    )
    canonical = json.dumps(BODY, sort_keys=True, separators=(",", ":"))
    signing_input = f"v1\n1786543200\nnonce_synthetic_0001\n{canonical}"
    expected_digest = base64.urlsafe_b64encode(
        hmac.new(SECRET.encode(), signing_input.encode(), hashlib.sha256).digest()
    ).decode().rstrip("=")

    assert header == f"v1:1786543200:nonce_synthetic_0001:{expected_digest}"
    assert header == (
        "v1:1786543200:nonce_synthetic_0001:"
        "t_xPS_Gucm9tJ7dWfvNDW2EdVCViQq4SpRSUbfdOEss"
    )


def test_admission_uses_fresh_nonce_and_exact_canonical_body(monkeypatch):
    calls: list[dict] = []

    def fake_post(url, *, content, headers, timeout):
        calls.append(
            {"url": url, "content": content, "headers": dict(headers), "timeout": timeout}
        )
        return httpx.Response(
            200,
            json=_success_body(),
            headers={"Cache-Control": "private, no-store"},
        )

    monkeypatch.setattr("workers_projects_runtime.broker_admission.httpx.post", fake_post)
    first = admit_capability_grant(
        "http://127.0.0.1:3180/internal/admission",
        secret=SECRET,
        body=BODY,
        expected_scope_fingerprint="scope_synthetic_0001",
    )
    second = admit_capability_grant(
        "http://127.0.0.1:3180/internal/admission",
        secret=SECRET,
        body=BODY,
        expected_scope_fingerprint="scope_synthetic_0001",
    )

    assert first.grant_token == second.grant_token == "synthetic-run-local-grant"
    assert calls[0]["content"] == json.dumps(
        BODY, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    assert calls[0]["headers"]["Content-Type"] == "application/json"
    assert calls[0]["headers"]["X-Viventium-GlassHive-Admission"] != calls[1][
        "headers"
    ]["X-Viventium-GlassHive-Admission"]


@pytest.mark.parametrize("status_code", [401, 403, 409, 410])
def test_structured_needs_input_error_is_preserved(status_code, monkeypatch):
    monkeypatch.setattr(
        "workers_projects_runtime.broker_admission.httpx.post",
        lambda *_args, **_kwargs: httpx.Response(
            status_code,
            json={
                "error": {
                    "code": "connected_account_reauthorization_required",
                    "message": "Reconnect the account to continue.",
                    "needsInput": True,
                }
            },
            headers={"Cache-Control": "no-store"},
        ),
    )

    with pytest.raises(BrokerAdmissionError) as captured:
        admit_capability_grant(
            "http://127.0.0.1:3180/internal/admission",
            secret=SECRET,
            body=BODY,
            expected_scope_fingerprint="scope_synthetic_0001",
        )

    assert captured.value.needs_input is True
    assert captured.value.retryable is False
    assert captured.value.code == "connected_account_reauthorization_required"


def test_non_needs_input_control_plane_failure_is_not_misreported(monkeypatch):
    monkeypatch.setattr(
        "workers_projects_runtime.broker_admission.httpx.post",
        lambda *_args, **_kwargs: httpx.Response(
            503,
            json={
                "error": {
                    "code": "admission_temporarily_unavailable",
                    "message": "Admission is temporarily unavailable.",
                    "needsInput": False,
                }
            },
            headers={"Cache-Control": "no-store"},
        ),
    )

    with pytest.raises(BrokerAdmissionError) as captured:
        admit_capability_grant(
            "http://127.0.0.1:3180/internal/admission",
            secret=SECRET,
            body=BODY,
            expected_scope_fingerprint="scope_synthetic_0001",
        )

    assert captured.value.needs_input is False
    assert captured.value.retryable is True


@pytest.mark.parametrize(
    "response_override",
    [
        {"runId": "run_wrong"},
        {"originRef": "ghi_wrong"},
        {"scopeFingerprint": "scope_wrong"},
        {"grantToken": ""},
        {"brokerUrl": "file:///tmp/not-a-broker"},
    ],
)
def test_success_fails_closed_on_binding_scope_or_grant_mismatch(
    response_override, monkeypatch
):
    monkeypatch.setattr(
        "workers_projects_runtime.broker_admission.httpx.post",
        lambda *_args, **_kwargs: httpx.Response(
            200,
            json=_success_body(**response_override),
            headers={"Cache-Control": "no-store"},
        ),
    )
    with pytest.raises(BrokerAdmissionError, match="invalid"):
        admit_capability_grant(
            "http://127.0.0.1:3180/internal/admission",
            secret=SECRET,
            body=BODY,
            expected_scope_fingerprint="scope_synthetic_0001",
        )


class _CapturingHostRuntime(StubRuntime):
    def __init__(self):
        self.run_workers: list[dict] = []
        self.process_observer = None

    def set_host_process_observer(self, observer):
        self.process_observer = observer

    def reconcile_worker(self, worker):
        return RuntimeInfo(
            runtime="codex-cli",
            model="test",
            gateway_url="",
            gateway_port=None,
            gateway_token=None,
            session_key=None,
            state_dir=None,
            workspace_dir=None,
            pid=None,
        )

    def prepare_run_authority_context(self, worker, run_id=None):
        return {"container_generation_id": BODY["containerGenerationId"]}

    def run_task(self, worker, instruction, timeout_sec=None, run_id=None):
        self.run_workers.append(dict(worker))
        return "FINAL REPORT:\nAuthorized result"


class _UnavailableBrokerHostRuntime(_CapturingHostRuntime):
    def prepare_run_authority_context(self, worker, run_id=None):
        raise BrokerAdmissionError(
            "broker_admission_unavailable",
            "The capability broker is temporarily unavailable.",
            retryable=True,
        )


def _reserve_pending_admission(store: Store):
    bundle = {
        "run_mode": "mission",
        "glasshive_capability_authorization": {
            "version": 1,
            "status": "pending_admission",
            "authorization_ref": BODY["authorizationRef"],
            "origin_ref": BODY["originRef"],
            "max_expires_at": "2099-01-01T00:00:00+00:00",
            "scope_fingerprint": "scope_synthetic_0001",
        },
    }
    return store.reserve_delegation(
        tenant_id="tenant-a",
        owner_id="owner-a",
        idempotency_key="delegation-admission-test",
        request_digest="digest-admission-test",
        origin_ref=BODY["originRef"],
        title="Admission mission",
        goal="Prove deferred admission",
        instruction="Use connected account data.",
        origin_surface="telegram",
        worker_name="Admission worker",
        worker_role="worker",
        profile="codex-cli",
        backend="codex-cli",
        runtime="codex-cli",
        model="test",
        execution_mode="host",
        bootstrap_bundle=bundle,
    )


def _run_processor(service: WorkersProjectsService, worker_id: str) -> None:
    with service._processors_lock:
        service._active_processors.add(worker_id)
        service._processor_generations[worker_id] = 1
    service._process_worker_queue(worker_id, 1)


def test_core_pending_authorization_schema_is_accepted_exactly(tmp_path):
    store = Store(str(tmp_path / "runtime.sqlite3"))
    record = _reserve_pending_admission(store)
    service = WorkersProjectsService(
        store, _CapturingHostRuntime(), reconcile_on_startup=False
    )
    try:
        worker = store.get_worker(record["worker_id"])
        assert service._deferred_capability_authorization(worker) == {
            "authorization_ref": BODY["authorizationRef"],
            "origin_ref": BODY["originRef"],
            "max_expires_at": "2099-01-01T00:00:00+00:00",
            "scope_fingerprint": "scope_synthetic_0001",
        }
        bundle = json.loads(worker["bootstrap_bundle_json"])
        bundle["glasshive_capability_authorization"]["status"] = "authorized"
        worker["bootstrap_bundle_json"] = json.dumps(bundle)
        with pytest.raises(BrokerAdmissionError) as captured:
            service._deferred_capability_authorization(worker)
        assert captured.value.code == "broker_authorization_invalid"
    finally:
        service.shutdown()


def test_automatic_clean_room_missing_deferred_authorization_fails_before_runtime(
    tmp_path,
):
    store = Store(str(tmp_path / "runtime.sqlite3"))
    record = _reserve_pending_admission(store)
    service = WorkersProjectsService(
        store, _CapturingHostRuntime(), reconcile_on_startup=False
    )
    try:
        worker = store.get_worker(record["worker_id"])
        run = store.get_run(record["run_id"])
        bundle = json.loads(worker["bootstrap_bundle_json"])
        bundle.pop("glasshive_capability_authorization")
        bundle["execution_policy"] = "parallel-clean-room-v1"
        bundle["viventium_launch_authority"] = {
            "version": 1,
            "kind": "conversation_orchestrator",
            "execution_mode": "docker",
        }
        worker["execution_mode"] = "docker"
        worker["bootstrap_bundle_json"] = json.dumps(bundle)

        with pytest.raises(BrokerAdmissionError) as captured:
            service._run_local_admitted_worker(worker, run)

        assert captured.value.code == "capability_authorization_missing"
        assert captured.value.needs_input is True
        assert captured.value.retryable is False
        assert service.runtime.run_workers == []
    finally:
        service.shutdown()


def test_expired_pending_authorization_uses_canonical_reauthorization_code(tmp_path):
    store = Store(str(tmp_path / "runtime.sqlite3"))
    record = _reserve_pending_admission(store)
    service = WorkersProjectsService(
        store, _CapturingHostRuntime(), reconcile_on_startup=False
    )
    try:
        worker = store.get_worker(record["worker_id"])
        bundle = json.loads(worker["bootstrap_bundle_json"])
        bundle["glasshive_capability_authorization"]["max_expires_at"] = (
            "2000-01-01T00:00:00+00:00"
        )
        worker["bootstrap_bundle_json"] = json.dumps(bundle)

        with pytest.raises(BrokerAdmissionError) as captured:
            service._deferred_capability_authorization(worker)

        assert captured.value.code == "capability_authorization_horizon_expired"
        assert captured.value.needs_input is True
        assert captured.value.retryable is False
    finally:
        service.shutdown()


def test_deferred_admission_overlays_grant_only_for_exact_started_run(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "VIVENTIUM_GLASSHIVE_ADMISSION_URL",
        "http://127.0.0.1:3180/api/viventium/glasshive/capabilities/admit",
    )
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_ADMISSION_SECRET", SECRET)
    captured_requests: list[tuple[str, dict]] = []

    def fake_post(_url, *, content, headers, timeout):
        captured_request = json.loads(content)
        captured_requests.append((_url, captured_request))
        assert "synthetic-run-local-grant" not in content.decode()
        if _url.endswith("/revoke"):
            return httpx.Response(204, headers={"Cache-Control": "no-store"})
        return httpx.Response(
            200,
            json=_success_body(
                runId=captured_request["runId"],
                workRef=captured_request["workRef"],
                workerId=captured_request["workerId"],
            ),
            headers={"Cache-Control": "no-store"},
        )

    monkeypatch.setattr("workers_projects_runtime.broker_admission.httpx.post", fake_post)
    store = Store(str(tmp_path / "runtime.sqlite3"))
    runtime = _CapturingHostRuntime()
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    service._emit_callback = lambda *_args, **_kwargs: None
    record = _reserve_pending_admission(store)
    before = store.get_worker(record["worker_id"])
    assert "synthetic-run-local-grant" not in str(before["bootstrap_bundle_json"])

    try:
        _run_processor(service, record["worker_id"])
    finally:
        service.shutdown()

    assert captured_requests[0][1] == {
        **BODY,
        "runId": record["initial_run_id"],
        "workRef": record["work_ref"],
        "workerId": record["worker_id"],
    }
    assert captured_requests[1][0].endswith("/revoke")
    assert captured_requests[1][1] == {
        **captured_requests[0][1],
        "grantId": "grant_synthetic_0001",
    }
    assert len(runtime.run_workers) == 1
    run_bundle = json.loads(runtime.run_workers[0]["bootstrap_bundle_json"])
    assert run_bundle["env"]["GLASSHIVE_CAPABILITY_BROKER_TOKEN"] == (
        "synthetic-run-local-grant"
    )
    assert run_bundle["glasshive_capability_broker"]["scope_fingerprint"] == (
        "scope_synthetic_0001"
    )
    persisted = store.get_worker(record["worker_id"])
    assert "synthetic-run-local-grant" not in str(persisted["bootstrap_bundle_json"])
    assert store.get_run(record["initial_run_id"])["state"] == "completed"
    assert store.list_active_host_run_leases() == []


def test_failed_exact_grant_revocation_is_durable_and_recovers_after_restart(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "VIVENTIUM_GLASSHIVE_ADMISSION_URL",
        "http://127.0.0.1:3180/api/viventium/glasshive/capabilities/admit",
    )
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_ADMISSION_SECRET", SECRET)
    calls: list[str] = []
    fail_revoke = True

    def fake_post(url, *, content, headers, timeout):
        nonlocal fail_revoke
        calls.append(url)
        body = json.loads(content)
        if url.endswith("/revoke"):
            if fail_revoke:
                raise httpx.ConnectError("synthetic unavailable")
            return httpx.Response(204, headers={"Cache-Control": "no-store"})
        return httpx.Response(
            200,
            json=_success_body(
                runId=body["runId"],
                workRef=body["workRef"],
                workerId=body["workerId"],
            ),
            headers={"Cache-Control": "no-store"},
        )

    monkeypatch.setattr("workers_projects_runtime.broker_admission.httpx.post", fake_post)
    store = Store(str(tmp_path / "runtime.sqlite3"))
    runtime = _CapturingHostRuntime()
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    service._emit_callback = lambda *_args, **_kwargs: None
    service._capability_revocation_retry_delay_s = lambda _record: 0.0
    record = _reserve_pending_admission(store)
    try:
        _run_processor(service, record["worker_id"])
    finally:
        service.shutdown()

    pending = store.list_capability_grant_revocations()
    assert len(pending) == 1
    assert {
        key: pending[0][key]
        for key in (
            "authorization_ref",
            "origin_ref",
            "work_ref",
            "worker_id",
            "run_id",
            "grant_id",
            "container_generation_id",
            "status",
            "attempts",
            "last_error_code",
        )
    } == {
        "authorization_ref": BODY["authorizationRef"],
        "origin_ref": BODY["originRef"],
        "work_ref": record["work_ref"],
        "worker_id": record["worker_id"],
        "run_id": record["initial_run_id"],
        "grant_id": "grant_synthetic_0001",
        "container_generation_id": BODY["containerGenerationId"],
        "status": "pending",
        "attempts": 1,
        "last_error_code": "broker_revocation_unavailable",
    }

    fail_revoke = False
    recovered = WorkersProjectsService(
        store, _CapturingHostRuntime(), reconcile_on_startup=False
    )
    try:
        deadline = time.time() + 2
        while time.time() < deadline:
            current = store.list_capability_grant_revocations()[0]
            if current["status"] == "applied":
                break
            time.sleep(0.01)
        assert current["status"] == "applied"
        assert current["attempts"] == 2
    finally:
        recovered.shutdown()
    assert sum(url.endswith("/revoke") for url in calls) == 2


def test_exact_generation_revocation_uses_a_fresh_signed_request(monkeypatch):
    calls: list[dict] = []

    def fake_post(url, *, content, headers, timeout):
        calls.append(
            {"url": url, "content": content, "headers": dict(headers), "timeout": timeout}
        )
        return httpx.Response(204, headers={"Cache-Control": "private, no-store"})

    monkeypatch.setattr("workers_projects_runtime.broker_admission.httpx.post", fake_post)
    revoke_capability_grant(
        "http://127.0.0.1:3180/api/viventium/glasshive/capabilities/admit",
        secret=SECRET,
        body={**BODY, "grantId": "grant_synthetic_0001"},
    )

    assert calls[0]["url"].endswith("/api/viventium/glasshive/capabilities/revoke")
    assert calls[0]["content"] == json.dumps(
        {**BODY, "grantId": "grant_synthetic_0001"},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    assert calls[0]["headers"]["X-Viventium-GlassHive-Admission"].startswith("v1:")


def test_needs_input_admission_does_not_start_provider_and_releases_lease(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "VIVENTIUM_GLASSHIVE_ADMISSION_URL",
        "http://127.0.0.1:3180/internal/admission",
    )
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_ADMISSION_SECRET", SECRET)
    monkeypatch.setattr(
        "workers_projects_runtime.broker_admission.httpx.post",
        lambda *_args, **_kwargs: httpx.Response(
            410,
            json={
                "error": {
                    "code": "connected_account_reauthorization_required",
                    "message": "Reconnect the account to continue.",
                    "needsInput": True,
                }
            },
            headers={"Cache-Control": "no-store"},
        ),
    )
    store = Store(str(tmp_path / "runtime.sqlite3"))
    runtime = _CapturingHostRuntime()
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    service._emit_callback = lambda *_args, **_kwargs: None
    record = _reserve_pending_admission(store)
    try:
        _run_processor(service, record["worker_id"])
    finally:
        service.shutdown()

    assert runtime.run_workers == []
    run = store.get_run(record["initial_run_id"])
    worker = store.get_worker(record["worker_id"])
    assert run["state"] == "needs_input"
    assert run["failure_class"] == "connected_account_reauthorization_required"
    assert worker["state"] == "needs_input"
    assert store.list_active_host_run_leases() == []


def test_retryable_admission_failure_requeues_without_failed_event_or_callback(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GLASSHIVE_RETRY_BASE_DELAY_S", "300")
    monkeypatch.setenv("GLASSHIVE_RETRY_MAX_DELAY_S", "300")
    store = Store(str(tmp_path / "runtime.sqlite3"))
    runtime = _UnavailableBrokerHostRuntime()
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    callbacks: list[str] = []
    service._emit_callback = (
        lambda _worker, event_type, **_kwargs: callbacks.append(event_type)
    )
    record = _reserve_pending_admission(store)

    try:
        _run_processor(service, record["worker_id"])
    finally:
        service.shutdown()

    run = store.get_run(record["initial_run_id"])
    worker = store.get_worker(record["worker_id"])
    event_types = {
        event["event_type"] for event in store.list_events(record["worker_id"])
    }
    assert runtime.run_workers == []
    assert run["state"] == "queued"
    assert run["failure_class"] == "broker_admission_unavailable"
    assert run["failure_retryable"] == 1
    assert run["retry_after"]
    assert worker["state"] == "ready"
    assert store.list_active_host_run_leases() == []
    assert "run.waiting_on_capacity" in event_types
    assert "run.failed" not in event_types
    assert callbacks == ["run.waiting_on_capacity"]


def test_required_protected_capability_unavailable_needs_input_without_provider_start(
    tmp_path,
):
    store = Store(str(tmp_path / "runtime.sqlite3"))
    runtime = _CapturingHostRuntime()
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    service._emit_callback = lambda *_args, **_kwargs: None
    record = _reserve_pending_admission(store)
    worker = store.get_worker(record["worker_id"])
    bundle = json.loads(worker["bootstrap_bundle_json"])
    bundle.pop("glasshive_capability_authorization", None)
    bundle["glasshive_capability_requirement"] = {
        "version": 1,
        "required": True,
        "status": "unavailable",
        "reason": "broker_config_unavailable",
    }
    store.update_worker(
        record["worker_id"], bootstrap_bundle_json=json.dumps(bundle, sort_keys=True)
    )

    try:
        _run_processor(service, record["worker_id"])
    finally:
        service.shutdown()

    assert runtime.run_workers == []
    run = store.get_run(record["initial_run_id"])
    worker = store.get_worker(record["worker_id"])
    assert run["state"] == "needs_input"
    assert run["failure_class"] == "required_capability_unavailable"
    assert worker["state"] == "needs_input"
    assert store.list_active_host_run_leases() == []
