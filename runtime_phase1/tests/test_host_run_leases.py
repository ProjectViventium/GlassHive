from __future__ import annotations

import json
import hashlib
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest

import workers_projects_runtime.service as service_module
from workers_projects_runtime.openclaw_runtime import (
    HostCapacityError,
    ProviderRateLimitError,
    RuntimeErrorBase,
    RuntimeInfo,
    RunStartupRejectedError,
    StubRuntime,
)
from workers_projects_runtime.profile_runtime import HostCodexCliRuntime
from workers_projects_runtime.service import HostResourceUsage, WorkersProjectsService
from workers_projects_runtime.service import ParallelExecutionIsolationError
from workers_projects_runtime.store import HostRunLeaseCapacityError, Store


def _active_worker_and_run(
    store: Store,
    suffix: str,
    *,
    execution_mode: str = "docker",
    run_state: str = "running",
    tenant_id: str = "local",
    owner_id: str = "owner-a",
):
    project = store.create_project(
        owner_id,
        f"Project {suffix}",
        f"Goal {suffix}",
        "codex-cli",
        tenant_id=tenant_id,
    )
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id=owner_id,
        name=f"Worker {suffix}",
        role="worker",
        profile="codex-cli",
        backend="codex-cli",
        runtime="codex-cli",
        model="test",
        execution_mode=execution_mode,
        tenant_id=tenant_id,
    )
    run = store.create_run(
        worker["worker_id"], project["project_id"], f"Instruction {suffix}", state=run_state
    )
    store.update_worker_state(worker["worker_id"], "running")
    return project, store.get_worker(worker["worker_id"]), run


def _lease(
    store: Store,
    suffix: str,
    *,
    lane: str = "mission",
    family: str = "codex",
    tenant_id: str = "tenant-a",
    owner_id: str = "owner-a",
    conversation_limit: int = 2,
    mission_limit: int = 3,
    account_limit: int = 4,
    tenant_limit: int = 12,
    now: datetime | None = None,
):
    cache = getattr(store, "_test_lease_subjects", {})
    subject = cache.get(suffix)
    if subject is None:
        _project, worker, run = _active_worker_and_run(
            store,
            f"lease-{suffix}",
            tenant_id=tenant_id,
            owner_id=owner_id,
        )
        subject = (worker["worker_id"], run["run_id"])
        cache[suffix] = subject
        store._test_lease_subjects = cache
    worker_id, run_id = subject
    return store.acquire_host_run_lease(
        runtime_family=family,
        lane=lane,
        tenant_id=tenant_id,
        owner_id=owner_id,
        worker_id=worker_id,
        run_id=run_id,
        executor_id=f"executor-{suffix}",
        conversation_limit=conversation_limit,
        mission_limit=mission_limit,
        account_mission_limit=account_limit,
        tenant_mission_limit=tenant_limit,
        lease_ttl_s=30,
        now=now,
    )


def test_persisted_host_leases_enforce_independent_lane_and_account_caps(tmp_path):
    store = Store(str(tmp_path / "leases.sqlite3"))

    for index in range(3):
        _lease(store, f"mission-{index}", owner_id=f"owner-{index}")
    with pytest.raises(HostRunLeaseCapacityError) as family_blocked:
        _lease(store, "mission-overflow", owner_id="owner-overflow")
    assert family_blocked.value.capacity_class == "family_lane"
    assert family_blocked.value.code == "host_capacity"

    # Conversation admission is an independent reserved lane for the same CLI.
    _lease(store, "conversation-1", lane="conversation")
    _lease(store, "conversation-2", lane="conversation")
    with pytest.raises(HostRunLeaseCapacityError) as conversation_blocked:
        _lease(store, "conversation-3", lane="conversation")
    assert conversation_blocked.value.capacity_class == "family_lane"

    # A single account cannot evade its top-level mission cap by switching CLI.
    isolated = Store(str(tmp_path / "account-cap.sqlite3"))
    for index, family in enumerate(("codex", "claude", "openclaw", "codex")):
        _lease(
            isolated,
            f"account-{index}",
            family=family,
            mission_limit=10,
            owner_id="same-owner",
        )
    with pytest.raises(HostRunLeaseCapacityError) as account_blocked:
        _lease(
            isolated,
            "account-overflow",
            family="claude",
            mission_limit=10,
            owner_id="same-owner",
        )
    assert account_blocked.value.capacity_class == "account"


def test_model_bootstrap_cannot_forge_the_reserved_conversation_lane(tmp_path):
    store = Store(str(tmp_path / "trusted-lane.sqlite3"))
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "runtime"))
    forged = {
        "worker_id": "wrk-forged",
        "profile": "codex-cli",
        "execution_mode": "host",
        "bootstrap_bundle_json": json.dumps({"run_mode": "conversation"}),
    }
    trusted = {**forged, "worker_id": "wrk-trusted", "trusted_run_lane": "conversation"}

    try:
        assert service._host_run_lane(forged) == "mission"
        assert service._host_run_lane(trusted) == "conversation"
        assert runtime._conversation_mode_from_worker(forged) is False
        assert runtime._conversation_mode_from_worker(trusted) is True
    finally:
        service.shutdown()


def test_isolated_parallel_policy_rejects_every_untrusted_host_mission_admission(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_ISOLATED_PARALLEL_POLICY", "1")
    monkeypatch.setenv("GLASSHIVE_HOST_WORKERS_ENABLED", "true")
    store = Store(str(tmp_path / "policy.sqlite3"))
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    project = service.create_project(
        "owner-a", "Host policy", "Protect Main", "codex-cli", tenant_id="tenant-a"
    )

    try:
        with pytest.raises(ParallelExecutionIsolationError):
            service.create_worker(
                project_id=project["project_id"],
                owner_id="owner-a",
                name="Forged conversation worker",
                role="worker",
                profile="codex-cli",
                backend="",
                execution_mode="host",
                bootstrap_bundle={"run_mode": "conversation"},
                tenant_id="tenant-a",
                start_synchronously=False,
            )

        prepared = service.create_worker(
            project_id=project["project_id"],
            owner_id="owner-a",
            name="Main conversation worker",
            role="conversation-agent",
            profile="codex-cli",
            backend="",
            execution_mode="host",
            bootstrap_bundle={"run_mode": "mission"},
            tenant_id="tenant-a",
            start_synchronously=False,
            _trusted_run_lane="conversation",
        )
        # A trusted caller can prepare the row, but it remains a mission lane
        # until an actual provider_session binds it durably.
        assert prepared["trusted_run_lane"] == "mission"
        store.upsert_provider_session(
            tenant_id="tenant-a",
            owner_id="owner-a",
            conversation_id="conversation-a",
            agent_id="agent-a",
            model_id="model-a",
            project_id=project["project_id"],
            worker_id=prepared["worker_id"],
            workspace_dir=str(tmp_path / "conversation"),
            access_mode="workspace",
        )
        trusted = store.get_worker(prepared["worker_id"])
        assert trusted["trusted_run_lane"] == "conversation"

        with pytest.raises(ParallelExecutionIsolationError):
            service.reserve_delegation(
                tenant_id="tenant-a",
                owner_id="owner-a",
                idempotency_key="host-policy-delegation",
                request_digest="digest",
                origin_ref="",
                title="Forbidden host mission",
                goal="Do work",
                instruction="Do work",
                origin_surface="web",
                worker_name="Mission",
                worker_role="worker",
                profile="codex-cli",
                execution_mode="host",
            )
    finally:
        service.shutdown()


def test_conversation_worker_creation_crash_before_session_link_never_buys_lane(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_ISOLATED_PARALLEL_POLICY", "1")
    monkeypatch.setenv("GLASSHIVE_HOST_WORKERS_ENABLED", "true")
    store = Store(str(tmp_path / "create-race.sqlite3"))
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    project = service.create_project(
        "owner-a", "Conversation", "Main", "codex-cli", tenant_id="tenant-a"
    )
    try:
        prepared = service.create_worker(
            project_id=project["project_id"],
            owner_id="owner-a",
            name="Prepared Main",
            role="conversation-agent",
            profile="codex-cli",
            backend="",
            execution_mode="host",
            tenant_id="tenant-a",
            start_synchronously=False,
            _trusted_run_lane="conversation",
        )
        # Simulate process death before provider_sessions upsert.
        restarted = WorkersProjectsService(
            Store(str(tmp_path / "create-race.sqlite3")),
            StubRuntime(),
            reconcile_on_startup=False,
        )
        try:
            orphan = restarted.store.get_worker(prepared["worker_id"])
            assert restarted.store.get_provider_session_by_worker(prepared["worker_id"]) is None
            assert restarted._host_run_lane(orphan) == "mission"
            with pytest.raises(ParallelExecutionIsolationError):
                restarted._ensure_execution_allowed(orphan)
        finally:
            restarted.shutdown()
    finally:
        service.shutdown()


def test_docker_missions_use_the_same_persisted_family_and_account_admission(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WPR_HOST_MISSION_SLOTS_PER_CLI", "1")
    monkeypatch.setenv("WPR_HOST_ACCOUNT_ACTIVE_LIMIT", "4")
    monkeypatch.setenv("WPR_HOST_TENANT_ACTIVE_LIMIT", "12")
    store = Store(str(tmp_path / "docker-leases.sqlite3"))
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    monkeypatch.setattr(
        service_module,
        "host_resource_usage",
        lambda _leases: HostResourceUsage(
            child_processes=0,
            threads=0,
            available_memory_bytes=16 * 1024**3,
            available_disk_bytes=64 * 1024**3,
        ),
    )
    _first_project, first, first_run = _active_worker_and_run(
        store,
        "docker-one",
        execution_mode="docker",
        tenant_id="tenant-a",
        owner_id="owner-a",
    )
    _second_project, second, second_run = _active_worker_and_run(
        store,
        "docker-two",
        execution_mode="docker",
        tenant_id="tenant-a",
        owner_id="owner-b",
    )

    try:
        acquired = service._acquire_host_run_lease(first, first_run)
        assert acquired and acquired["runtime_family"] == "codex"
        with pytest.raises(HostCapacityError) as blocked:
            service._acquire_host_run_lease(second, second_run)
        assert blocked.value.capacity_class == "family_lane"
    finally:
        service._release_host_run_lease(first_run["run_id"], reason="test_complete")
        service.shutdown()


def test_docker_mission_fails_closed_on_global_resource_probe_then_recovers(
    tmp_path, monkeypatch
):
    store = Store(str(tmp_path / "docker-resource.sqlite3"))
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    _project, worker, run = _active_worker_and_run(
        store,
        "docker-resource",
        execution_mode="docker",
        tenant_id="tenant-a",
        owner_id="owner-a",
    )
    unhealthy = HostResourceUsage(
        child_processes=0,
        threads=0,
        available_memory_bytes=0,
        available_disk_bytes=0,
        process_probe_ok=False,
        memory_probe_ok=False,
        disk_probe_ok=False,
    )
    healthy = HostResourceUsage(
        child_processes=0,
        threads=0,
        available_memory_bytes=16 * 1024**3,
        available_disk_bytes=64 * 1024**3,
    )
    monkeypatch.setattr(service_module, "host_resource_usage", lambda _leases: unhealthy)
    try:
        with pytest.raises(HostCapacityError) as blocked:
            service._acquire_host_run_lease(worker, run)
        assert blocked.value.capacity_class == "resource_probe_unavailable"
        monkeypatch.setattr(service_module, "host_resource_usage", lambda _leases: healthy)
        admitted = service._acquire_host_run_lease(worker, run)
        assert admitted and admitted["status"] == "active"
    finally:
        service._release_host_run_lease(run["run_id"], reason="test_complete")
        service.shutdown()


@pytest.mark.parametrize(
    ("docker_usage", "expected_class"),
    [
        (
            {
                "child_processes": 65,
                "threads": 100,
                "available_memory_bytes": 16 * 1024**3,
                "available_disk_bytes": 64 * 1024**3,
                "running_worker_containers": 1,
                "process_probe_ok": True,
                "memory_probe_ok": True,
                "disk_probe_ok": True,
            },
            "resource_pressure",
        ),
        (
            {
                "child_processes": 0,
                "threads": 0,
                "available_memory_bytes": 0,
                "available_disk_bytes": 0,
                "running_worker_containers": 0,
                "process_probe_ok": False,
                "memory_probe_ok": False,
                "disk_probe_ok": False,
            },
            "resource_probe_unavailable",
        ),
    ],
)
def test_docker_container_side_resource_pressure_or_unknown_probe_blocks_admission(
    tmp_path, monkeypatch, docker_usage, expected_class
):
    class DockerMeasuredRuntime(StubRuntime):
        def isolated_resource_usage(self):
            return docker_usage

    store = Store(str(tmp_path / f"docker-measured-{expected_class}.sqlite3"))
    service = WorkersProjectsService(
        store, DockerMeasuredRuntime(), reconcile_on_startup=False
    )
    monkeypatch.setattr(
        service_module,
        "host_resource_usage",
        lambda _leases: HostResourceUsage(
            child_processes=0,
            threads=0,
            available_memory_bytes=16 * 1024**3,
            available_disk_bytes=64 * 1024**3,
        ),
    )
    worker = {
        "worker_id": "wrk-docker-measured",
        "tenant_id": "tenant-a",
        "owner_id": "owner-a",
        "profile": "codex-cli",
        "execution_mode": "docker",
        "trusted_run_lane": "mission",
    }
    try:
        with pytest.raises(HostCapacityError) as blocked:
            service._acquire_host_run_lease(
                worker, {"run_id": "run-docker-measured"}
            )
    finally:
        service.shutdown()

    assert blocked.value.capacity_class == expected_class


@pytest.mark.parametrize(
    ("prospective_worker_id", "expects_pressure"),
    [
        ("wrk-running", False),
        ("wrk-new", True),
    ],
)
def test_docker_admission_does_not_reserve_a_second_container_for_running_worker(
    tmp_path, monkeypatch, prospective_worker_id, expects_pressure
):
    class DockerMeasuredRuntime(StubRuntime):
        def isolated_resource_usage(self, *, cached_only=False):
            assert cached_only is True
            return {
                "child_processes": 10,
                "threads": 10,
                "available_memory_bytes": 4 * 1024**3,
                "available_disk_bytes": 64 * 1024**3,
                "running_worker_containers": 1,
                "running_worker_ids": ["wrk-running"],
                "process_probe_ok": True,
                "memory_probe_ok": True,
                "disk_probe_ok": True,
            }

    store = Store(str(tmp_path / f"docker-running-{prospective_worker_id}.sqlite3"))
    service = WorkersProjectsService(
        store, DockerMeasuredRuntime(), reconcile_on_startup=False
    )
    monkeypatch.setattr(
        service_module,
        "host_resource_usage",
        lambda _leases: HostResourceUsage(
            child_processes=0,
            threads=0,
            available_memory_bytes=16 * 1024**3,
            available_disk_bytes=64 * 1024**3,
        ),
    )
    worker = {
        "worker_id": prospective_worker_id,
        "tenant_id": "tenant-a",
        "owner_id": "owner-a",
        "profile": "codex-cli",
        "execution_mode": "docker",
        "trusted_run_lane": "mission",
    }
    try:
        pressure = service._host_resource_capacity_error(
            worker,
            docker_cached_only=True,
        )
    finally:
        service.shutdown()

    if expects_pressure:
        assert pressure is not None
        assert pressure.capacity_class == "resource_pressure"
    else:
        assert pressure is None


def test_idle_retained_docker_workstations_do_not_exhaust_active_run_process_budget(
    tmp_path, monkeypatch
):
    """Only active/prospective mission process trees consume the 64-child guard."""

    class DockerMeasuredRuntime(StubRuntime):
        def isolated_resource_usage(self, *, cached_only=False):
            assert cached_only is True
            return {
                "child_processes": 115,
                "threads": 115,
                "available_memory_bytes": 4 * 1024**3,
                "available_disk_bytes": 64 * 1024**3,
                "running_worker_containers": 7,
                "running_worker_ids": [
                    "wrk-prospective",
                    "wrk-idle-a",
                    "wrk-idle-b",
                    "wrk-idle-c",
                    "wrk-idle-d",
                    "wrk-idle-e",
                    "wrk-idle-f",
                ],
                "worker_process_counts": {
                    "wrk-prospective": {"child_processes": 15, "threads": 15},
                    "wrk-idle-a": {"child_processes": 16, "threads": 16},
                    "wrk-idle-b": {"child_processes": 16, "threads": 16},
                    "wrk-idle-c": {"child_processes": 17, "threads": 17},
                    "wrk-idle-d": {"child_processes": 17, "threads": 17},
                    "wrk-idle-e": {"child_processes": 17, "threads": 17},
                    "wrk-idle-f": {"child_processes": 17, "threads": 17},
                },
                "process_probe_ok": True,
                "memory_probe_ok": True,
                "disk_probe_ok": True,
            }

    store = Store(str(tmp_path / "docker-idle-retained.sqlite3"))
    service = WorkersProjectsService(
        store, DockerMeasuredRuntime(), reconcile_on_startup=False
    )
    monkeypatch.setattr(
        service_module,
        "host_resource_usage",
        lambda _leases: HostResourceUsage(
            child_processes=0,
            threads=0,
            available_memory_bytes=16 * 1024**3,
            available_disk_bytes=64 * 1024**3,
        ),
    )
    worker = {
        "worker_id": "wrk-prospective",
        "tenant_id": "tenant-a",
        "owner_id": "owner-a",
        "profile": "codex-cli",
        "execution_mode": "docker",
        "trusted_run_lane": "mission",
    }
    try:
        pressure = service._host_resource_capacity_error(
            worker,
            docker_cached_only=True,
        )
    finally:
        service.shutdown()

    assert pressure is None


def test_orchestration_readiness_stays_available_when_docker_capacity_is_unknown(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_ISOLATED_PARALLEL_POLICY", "1")

    class RuntimeWithUnknownDockerResources(StubRuntime):
        def isolated_parallel_readiness(self):
            return {"ready": True, "reason": ""}

        def isolated_resource_usage(self):
            return {
                "child_processes": 0,
                "threads": 0,
                "available_memory_bytes": 0,
                "available_disk_bytes": 0,
                "running_worker_containers": 0,
                "process_probe_ok": False,
                "memory_probe_ok": False,
                "disk_probe_ok": False,
            }

    store = Store(str(tmp_path / "readiness-resource-unknown.sqlite3"))
    service = WorkersProjectsService(
        store, RuntimeWithUnknownDockerResources(), reconcile_on_startup=False
    )
    monkeypatch.setattr(
        service_module,
        "host_resource_usage",
        lambda _leases: HostResourceUsage(
            child_processes=0,
            threads=0,
            available_memory_bytes=16 * 1024**3,
            available_disk_bytes=64 * 1024**3,
        ),
    )
    try:
        capabilities = service.orchestration_capabilities()
    finally:
        service.shutdown()

    # Isolation readiness describes whether automatic work can be accepted
    # safely. Momentary/unknown execution capacity is enforced by admission and
    # leaves the durable run queued; it must not disable the account toggle or
    # hide an existing board while another mission consumes the resource budget.
    assert capabilities == {
        "policyVersion": 1,
        "isolatedParallelReady": True,
        "isolatedParallelReason": "",
        "hostMissionsAllowed": False,
        "hostMissionsActive": 0,
    }


def test_docker_admission_uses_only_cached_resource_snapshot(tmp_path, monkeypatch):
    observed: list[bool] = []

    class CachedOnlyRuntime(StubRuntime):
        def isolated_resource_usage(self, *, cached_only=False):
            observed.append(cached_only)
            assert cached_only is True, "admission must not cold-run Docker CLI probes"
            return super().isolated_resource_usage()

    store = Store(str(tmp_path / "docker-cached-admission.sqlite3"))
    worker_row = store.create_project(
        "owner-a", "Cached", "Fast admission", "codex-cli", tenant_id="tenant-a"
    )
    worker = store.create_worker(
        project_id=worker_row["project_id"],
        owner_id="owner-a",
        name="Cached worker",
        role="worker",
        profile="codex-cli",
        backend="codex-cli",
        runtime="codex-cli",
        model="test",
        execution_mode="docker",
        tenant_id="tenant-a",
    )
    run = store.create_run(
        worker["worker_id"],
        worker_row["project_id"],
        "Exercise cached Docker admission",
        state="running",
    )
    service = WorkersProjectsService(
        store, CachedOnlyRuntime(), reconcile_on_startup=False
    )
    monkeypatch.setattr(
        service_module,
        "host_resource_usage",
        lambda _leases: HostResourceUsage(
            child_processes=0,
            threads=0,
            available_memory_bytes=16 * 1024**3,
            available_disk_bytes=64 * 1024**3,
        ),
    )
    try:
        lease = service._acquire_host_run_lease(worker, run)
    finally:
        service._release_host_run_lease(run["run_id"], reason="test_complete")
        service.shutdown()

    assert lease and observed == [True]


def test_orchestration_readiness_fails_closed_until_existing_host_mission_is_terminal(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_ISOLATED_PARALLEL_POLICY", "1")
    store = Store(str(tmp_path / "capabilities.sqlite3"))
    worker, run = _running_host_run(store, "existing")
    class RuntimeWithProvenAbsence(StubRuntime):
        def host_active_process_status(self, _worker):
            return {"state": "absent"}

        def isolated_parallel_readiness(self):
            return {"ready": True, "reason": ""}

    service = WorkersProjectsService(
        store, RuntimeWithProvenAbsence(), reconcile_on_startup=False
    )

    try:
        blocked = service.orchestration_capabilities()
        assert blocked == {
            "policyVersion": 1,
            "isolatedParallelReady": False,
            "isolatedParallelReason": "host_missions_active",
            "hostMissionsAllowed": False,
            "hostMissionsActive": 1,
        }
        store.finalize_run(run["run_id"], state="completed", output_text="done")
        ready = service.orchestration_capabilities()
        assert ready == {
            "policyVersion": 1,
            "isolatedParallelReady": True,
            "isolatedParallelReason": "",
            "hostMissionsAllowed": False,
            "hostMissionsActive": 0,
        }
    finally:
        service.shutdown()


@pytest.mark.parametrize("process_state", ["active", "uncertain"])
def test_orchestration_readiness_never_ignores_live_or_unprovable_host_process(
    tmp_path, monkeypatch, process_state
):
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_ISOLATED_PARALLEL_POLICY", "1")
    store = Store(str(tmp_path / f"orphan-{process_state}.sqlite3"))
    worker, run = _running_host_run(store, process_state)
    store.finalize_run(run["run_id"], state="completed", output_text="ledger terminal")

    class RuntimeWithOrphanStatus(StubRuntime):
        def host_active_process_status(self, candidate):
            assert candidate["worker_id"] == worker["worker_id"]
            return {"state": process_state, "run_id": run["run_id"]}

        def isolated_parallel_readiness(self):
            return {"ready": True, "reason": ""}

    service = WorkersProjectsService(
        store, RuntimeWithOrphanStatus(), reconcile_on_startup=False
    )
    try:
        capabilities = service.orchestration_capabilities()
    finally:
        service.shutdown()

    assert capabilities["isolatedParallelReady"] is False
    assert capabilities["hostMissionsActive"] == (1 if process_state == "active" else 0)


def test_orchestration_readiness_counts_stale_active_lease_until_reconciled(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_ISOLATED_PARALLEL_POLICY", "1")
    store = Store(str(tmp_path / "stale-lease-gate.sqlite3"))
    worker, run = _running_host_run(store, "stale-lease")
    lease = store.acquire_host_run_lease(
        runtime_family="codex",
        lane="mission",
        tenant_id="tenant-a",
        owner_id="owner-a",
        worker_id=worker["worker_id"],
        run_id=run["run_id"],
        executor_id="dead-executor",
        conversation_limit=2,
        mission_limit=3,
        account_mission_limit=4,
        tenant_mission_limit=12,
        lease_ttl_s=30,
        now=datetime.now(timezone.utc) - timedelta(minutes=10),
    )
    store.finalize_run(run["run_id"], state="completed", output_text="done")

    class RuntimeWithProvenAbsence(StubRuntime):
        def host_active_process_status(self, _worker):
            return {"state": "absent"}

        def isolated_parallel_readiness(self):
            return {"ready": True, "reason": ""}

    service = WorkersProjectsService(
        store, RuntimeWithProvenAbsence(), reconcile_on_startup=False
    )
    try:
        assert service.orchestration_capabilities()["isolatedParallelReady"] is False
        assert service.orchestration_capabilities()["hostMissionsActive"] == 1
        store.release_host_run_lease(
            lease["lease_id"], executor_id=None, reason="proven_dead"
        )
        assert service.orchestration_capabilities()["isolatedParallelReady"] is True
    finally:
        service.shutdown()


def test_orchestration_readiness_tracks_nonmutating_isolated_runtime_probe(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_ISOLATED_PARALLEL_POLICY", "1")
    store = Store(str(tmp_path / "isolated-probe.sqlite3"))

    class RecoveringRuntime(StubRuntime):
        available = False

        def host_active_process_status(self, _worker):
            return {"state": "absent"}

        def isolated_parallel_readiness(self):
            return {
                "ready": self.available,
                "reason": "" if self.available else "docker_unavailable",
            }

    runtime = RecoveringRuntime()
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    try:
        blocked = service.orchestration_capabilities()
        runtime.available = True
        recovered = service.orchestration_capabilities()
    finally:
        service.shutdown()

    assert blocked["isolatedParallelReady"] is False
    assert recovered["isolatedParallelReady"] is True


def test_legacy_host_lease_schema_migrates_before_mutation_scope_index(tmp_path):
    db_path = tmp_path / "legacy.sqlite3"
    # Create the full current schema, then reproduce the exact legacy table
    # shape by rebuilding host_run_leases without mutation_scope.
    Store(str(db_path))
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP INDEX IF EXISTS idx_host_run_leases_active_mutation_scope")
        conn.execute("ALTER TABLE host_run_leases DROP COLUMN mutation_scope")

    migrated = Store(str(db_path))
    with sqlite3.connect(db_path) as conn:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(host_run_leases)").fetchall()
        }
        indexes = {
            row[1] for row in conn.execute("PRAGMA index_list(host_run_leases)").fetchall()
        }

    assert "mutation_scope" in columns
    assert "idx_host_run_leases_active_mutation_scope" in indexes
    assert migrated.list_active_host_run_leases() == []


def test_host_lease_is_exact_run_idempotent_and_persists_process_identity(tmp_path):
    store = Store(str(tmp_path / "leases.sqlite3"))
    acquired = _lease(store, "exact")
    replay = _lease(store, "exact")

    assert replay["lease_id"] == acquired["lease_id"]
    assert replay["idempotent_replay"] is True

    updated = store.heartbeat_host_run_lease(
        acquired["lease_id"],
        executor_id="executor-exact",
        pid=12345,
        process_group=12345,
        process_start_identity="ps-lstart:synthetic",
        lease_ttl_s=60,
    )
    assert updated["pid"] == 12345
    assert updated["process_group"] == 12345
    assert updated["process_start_identity"] == "ps-lstart:synthetic"

    released = store.release_host_run_lease(
        acquired["lease_id"],
        executor_id="executor-exact",
        reason="run_terminal",
    )
    assert released["status"] == "released"
    assert store.list_active_host_run_leases() == []


def test_heartbeat_releases_terminal_run_lease_instead_of_renewing_it(tmp_path):
    store = Store(str(tmp_path / "terminal-heartbeat.sqlite3"))
    _project, worker, run = _active_worker_and_run(store, "terminal-heartbeat")
    service = WorkersProjectsService(
        store, StubRuntime(), reconcile_on_startup=False
    )
    try:
        lease = store.acquire_host_run_lease(
            runtime_family="codex",
            lane="mission",
            tenant_id="local",
            owner_id="owner-a",
            worker_id=worker["worker_id"],
            run_id=run["run_id"],
            executor_id=service.executor_id,
            conversation_limit=2,
            mission_limit=3,
            account_mission_limit=4,
            tenant_mission_limit=12,
            lease_ttl_s=30,
        )
        original_heartbeat = lease["heartbeat_at"]
        store.finalize_run(run["run_id"], state="completed", output_text="done")

        service._heartbeat_host_run_leases_once()

        durable = store.get_host_run_lease(lease["lease_id"])
        assert durable is not None
        assert durable["status"] == "released"
        assert durable["release_reason"] == "run_terminal"
        assert durable["heartbeat_at"] == original_heartbeat
    finally:
        service.shutdown()


def test_heartbeat_pass_logs_store_error_and_remains_callable(
    tmp_path, monkeypatch, caplog
):
    store = Store(str(tmp_path / "heartbeat-errors.sqlite3"))
    service = WorkersProjectsService(
        store, StubRuntime(), reconcile_on_startup=False
    )
    original = store.list_active_host_run_leases
    calls = 0

    def fail_once():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("synthetic heartbeat read failure")
        return original()

    monkeypatch.setattr(store, "list_active_host_run_leases", fail_once)
    try:
        with caplog.at_level(logging.ERROR):
            service._heartbeat_host_run_leases_once()
            service._heartbeat_host_run_leases_once()
    finally:
        service.shutdown()

    assert calls == 2
    assert "Host lease heartbeat pass failed" in caplog.text


def test_unexpected_processor_exception_requeues_run_releases_lease_and_logs(
    tmp_path, monkeypatch, caplog
):
    store = Store(str(tmp_path / "processor-unexpected.sqlite3"))
    _project, worker, run = _active_worker_and_run(
        store, "processor-unexpected", run_state="queued"
    )
    store.update_worker_state(worker["worker_id"], "ready")
    service = WorkersProjectsService(
        store, StubRuntime(), reconcile_on_startup=False
    )
    generation = 41
    with service._processors_lock:
        service._active_processors.add(worker["worker_id"])
        service._processor_generations[worker["worker_id"]] = generation

    def acquire_exact(worker_row, run_row):
        return store.acquire_host_run_lease(
            runtime_family="codex",
            lane="mission",
            tenant_id="local",
            owner_id="owner-a",
            worker_id=worker_row["worker_id"],
            run_id=run_row["run_id"],
            executor_id=service.executor_id,
            conversation_limit=2,
            mission_limit=3,
            account_mission_limit=4,
            tenant_mission_limit=12,
            lease_ttl_s=30,
        )

    monkeypatch.setattr(service, "_acquire_host_run_lease", acquire_exact)
    monkeypatch.setattr(
        service,
        "_run_start_callback_record",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("synthetic processor bookkeeping crash")
        ),
    )
    monkeypatch.setattr(service, "_ensure_worker_processor", lambda _worker_id: None)
    try:
        with caplog.at_level(logging.ERROR):
            service._process_worker_queue(worker["worker_id"], generation)
    finally:
        service.shutdown()

    durable_run = store.get_run(run["run_id"])
    assert durable_run is not None
    assert durable_run["state"] == "queued"
    assert durable_run["failure_class"] == "service_processor_unexpected"
    assert store.get_active_host_run_lease_for_run(run["run_id"]) is None
    assert "Unexpected GlassHive worker processor failure" in caplog.text


def test_docker_stop_failure_stays_pending_and_preserves_running_run(tmp_path):
    class FailingDockerStopRuntime(StubRuntime):
        def interrupt_worker(self, worker, run_id=None):
            raise RuntimeError("docker termination could not be confirmed")

    store = Store(str(tmp_path / "docker-stop-pending.sqlite3"))
    _project, worker, run = _active_worker_and_run(store, "docker-stop-pending")
    service = WorkersProjectsService(
        store, FailingDockerStopRuntime(), reconcile_on_startup=False
    )
    try:
        result = service.stop_run(worker["worker_id"], run["run_id"])
        service._reconcile_worker_row(store.get_worker(worker["worker_id"]))
    finally:
        service.shutdown()

    durable_worker = store.get_worker(worker["worker_id"])
    durable_run = store.get_run(run["run_id"])
    assert result["accepted"] is True
    assert result["confirmation_pending"] is True
    assert durable_worker is not None and durable_worker["state"] == "stopping"
    assert "could not be confirmed" in str(durable_worker["last_error"])
    assert durable_run is not None and durable_run["state"] == "running"


def test_docker_stop_success_cancels_only_after_runtime_confirms_exit(tmp_path):
    class ConfirmedDockerStopRuntime(StubRuntime):
        def interrupt_worker(self, worker, run_id=None):
            return RuntimeInfo(
                runtime="codex-cli",
                model="test",
                gateway_url="",
                gateway_port=None,
                gateway_token=None,
                session_key=None,
                state_dir="/synthetic/state",
                workspace_dir="/synthetic/workspace",
                pid=None,
            )

    store = Store(str(tmp_path / "docker-stop-confirmed.sqlite3"))
    _project, worker, run = _active_worker_and_run(store, "docker-stop-confirmed")
    service = WorkersProjectsService(
        store, ConfirmedDockerStopRuntime(), reconcile_on_startup=False
    )
    try:
        result = service.stop_run(worker["worker_id"], run["run_id"])
    finally:
        service.shutdown()

    assert result["accepted"] is True
    assert result["confirmation_pending"] is False
    assert store.get_run(run["run_id"])["state"] == "cancelled"


def test_restart_adopts_live_survivor_and_collects_its_terminal_result(
    tmp_path, monkeypatch
):
    class SurvivorRuntime(StubRuntime):
        def __init__(self):
            self.alive = True
            self.completed = False

        def reconcile_worker(self, worker):
            info = super().reconcile_worker(worker)
            return RuntimeInfo(**{**info.__dict__, "pid": 4242 if self.alive else None})

        def collect_completed_run(self, worker, run_id=None, instruction=""):
            if not self.completed:
                return None
            self.alive = False
            return {"state": "completed", "output_text": "survivor completed"}

    monkeypatch.setenv("WPR_SURVIVOR_MONITOR_INTERVAL_S", "0.02")
    store = Store(str(tmp_path / "restart-survivor.sqlite3"))
    _project, worker, run = _active_worker_and_run(store, "restart-survivor")
    runtime = SurvivorRuntime()
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=True)
    try:
        deadline = time.monotonic() + 2
        while (
            not service._local_processor_owns(worker["worker_id"])
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert service._local_processor_owns(worker["worker_id"])
        assert store.get_run(run["run_id"])["state"] == "running"

        runtime.completed = True
        deadline = time.monotonic() + 2
        while (
            store.get_run(run["run_id"])["state"] != "completed"
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
    finally:
        service.shutdown()

    durable = store.get_run(run["run_id"])
    assert durable is not None
    assert durable["state"] == "completed"
    assert durable["output_text"] == "survivor completed"


def test_restart_survivor_exit_without_terminal_evidence_requeues_exact_run(
    tmp_path, monkeypatch
):
    class VanishingSurvivorRuntime(StubRuntime):
        def __init__(self):
            self.alive = True

        def reconcile_worker(self, worker):
            info = super().reconcile_worker(worker)
            return RuntimeInfo(**{**info.__dict__, "pid": 4242 if self.alive else None})

        def collect_completed_run(self, worker, run_id=None, instruction=""):
            return None

    monkeypatch.setenv("WPR_SURVIVOR_MONITOR_INTERVAL_S", "0.02")
    store = Store(str(tmp_path / "restart-survivor-retry.sqlite3"))
    _project, worker, run = _active_worker_and_run(store, "restart-survivor-retry")
    runtime = VanishingSurvivorRuntime()
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=True)
    monkeypatch.setattr(service, "_ensure_worker_processor", lambda _worker_id: None)
    try:
        deadline = time.monotonic() + 2
        while (
            not service._local_processor_owns(worker["worker_id"])
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert service._local_processor_owns(worker["worker_id"])

        runtime.alive = False
        deadline = time.monotonic() + 2
        while (
            store.get_run(run["run_id"])["state"] != "queued"
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
    finally:
        service.shutdown()

    durable = store.get_run(run["run_id"])
    assert durable is not None
    assert durable["state"] == "queued"
    assert durable["failure_class"] == "provider_temporarily_unavailable"
    assert durable["retry_attempts"] == 1


def test_host_runtime_env_is_mission_isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "real-home"))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "shared-tmp"))
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "private-state"))
    first = {"worker_id": "wrk-one", "profile": "codex-cli", "execution_mode": "host"}
    second = {"worker_id": "wrk-two", "profile": "codex-cli", "execution_mode": "host"}

    first_env = runtime._host_env(first, "run-one")
    second_env = runtime._host_env(second, "run-two")

    for key in (
        "HOME",
        "CODEX_HOME",
        "CLAUDE_CONFIG_DIR",
        "TMPDIR",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "GLASSHIVE_LOG_DIR",
    ):
        assert first_env[key] != second_env[key]
        assert first["worker_id"] in first_env[key]
        assert second["worker_id"] in second_env[key]
        assert os.path.isdir(first_env[key])
    assert first_env["HOME"] != os.environ["HOME"]
    assert first_env["TMPDIR"] != os.environ["TMPDIR"]


def test_same_title_missions_never_share_a_workspace(tmp_path):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "private-state"))
    common = {
        "name": "Same mission title",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "missions"),
        "bootstrap_bundle_json": json.dumps({"run_mode": "mission"}),
    }

    first = runtime._host_workspace_dir({**common, "worker_id": "wrk-first-unique"})
    second = runtime._host_workspace_dir({**common, "worker_id": "wrk-second-unique"})

    assert first != second
    assert "wrk-first-unique" in first.name
    assert "wrk-second-unique" in second.name


def test_native_process_observer_receives_exact_start_identity_immediately(tmp_path):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "private-state"))
    runtime._state_dir("wrk-observed").mkdir(parents=True)
    observed: list[dict[str, object]] = []
    runtime.set_host_process_observer(lambda payload: observed.append(payload))

    runtime._write_active_session(
        "wrk-observed",
        {
            "session_name": "host-run-observed",
            "run_id": "run-observed",
            "process_pid": 4321,
            "process_group": 4321,
            "process_start_identity": "ps-lstart:observed",
            "started_at": "2026-08-12T00:00:00+00:00",
        },
    )

    assert observed == [
        {
            "worker_id": "wrk-observed",
            "run_id": "run-observed",
            "identity_kind": "host_process",
            "pid": 4321,
            "process_group": 4321,
            "process_start_identity": "ps-lstart:observed",
            "container_id": "",
            "session_id": "host-run-observed",
        }
    ]


def test_resource_pressure_is_structured_capacity_not_a_terminal_failure(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WPR_HOST_MAX_CHILD_PROCESSES", "64")
    monkeypatch.setenv("WPR_HOST_MAX_THREADS", "2048")
    monkeypatch.setenv("WPR_HOST_MIN_AVAILABLE_MEMORY_MB", "2048")
    store = Store(str(tmp_path / "runtime.sqlite3"))
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    monkeypatch.setattr(
        service_module,
        "host_resource_usage",
        lambda _leases: HostResourceUsage(
            child_processes=65,
            threads=128,
            available_memory_bytes=8 * 1024**3,
            available_disk_bytes=16 * 1024**3,
        ),
    )
    try:
        error = service._host_resource_capacity_error()
    finally:
        service.shutdown()

    assert isinstance(error, HostCapacityError)
    assert error.code == "host_capacity"
    assert error.capacity_class == "resource_pressure"
    assert error.retryable is True


def test_disk_headroom_is_a_structured_capacity_wait(tmp_path, monkeypatch):
    monkeypatch.setenv("WPR_HOST_MAX_CHILD_PROCESSES", "64")
    monkeypatch.setenv("WPR_HOST_MAX_THREADS", "2048")
    monkeypatch.setenv("WPR_HOST_MIN_AVAILABLE_MEMORY_MB", "2048")
    monkeypatch.setenv("WPR_HOST_MIN_AVAILABLE_DISK_MB", "4096")
    store = Store(str(tmp_path / "runtime.sqlite3"))
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    monkeypatch.setattr(
        service_module,
        "host_resource_usage",
        lambda _leases: HostResourceUsage(
            child_processes=2,
            threads=32,
            available_memory_bytes=8 * 1024**3,
            available_disk_bytes=1024**3,
        ),
    )
    try:
        error = service._host_resource_capacity_error()
    finally:
        service.shutdown()

    assert isinstance(error, HostCapacityError)
    assert error.code == "host_capacity"
    assert error.capacity_class == "resource_pressure"


@pytest.mark.parametrize(
    "failed_probe",
    ["process_probe_ok", "memory_probe_ok", "disk_probe_ok"],
)
def test_unknown_host_resource_probe_queues_fail_closed_and_recovers(
    tmp_path, monkeypatch, failed_probe
):
    store = Store(str(tmp_path / f"runtime-{failed_probe}.sqlite3"))
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    healthy = {
        "child_processes": 0,
        "threads": 0,
        "available_memory_bytes": 16 * 1024**3,
        "available_disk_bytes": 32 * 1024**3,
        "process_probe_ok": True,
        "memory_probe_ok": True,
        "disk_probe_ok": True,
    }
    unavailable = {**healthy, failed_probe: False}
    readings = iter((HostResourceUsage(**unavailable), HostResourceUsage(**healthy)))
    monkeypatch.setattr(service_module, "host_resource_usage", lambda _leases: next(readings))
    try:
        blocked = service._host_resource_capacity_error()
        recovered = service._host_resource_capacity_error()
    finally:
        service.shutdown()

    assert isinstance(blocked, HostCapacityError)
    assert blocked.code == "host_capacity"
    assert blocked.capacity_class == "resource_probe_unavailable"
    assert recovered is None


def test_host_resource_probe_errors_are_never_fabricated_as_infinite_capacity(
    monkeypatch,
):
    monkeypatch.setattr(
        service_module.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("probe unavailable")),
    )
    monkeypatch.setattr(
        service_module.shutil,
        "disk_usage",
        lambda _path: (_ for _ in ()).throw(OSError("disk probe unavailable")),
    )

    usage = service_module.host_resource_usage([{"pid": 4242}])

    assert usage.process_probe_ok is False
    assert usage.memory_probe_ok is False
    assert usage.disk_probe_ok is False
    assert usage.available_memory_bytes == 0
    assert usage.available_disk_bytes == 0


def test_shared_target_repository_mutation_scope_serializes_host_missions(tmp_path):
    store = Store(str(tmp_path / "leases.sqlite3"))
    _first_project, first_worker, first_run = _active_worker_and_run(
        store,
        "shared-target-first",
        tenant_id="tenant-a",
        owner_id="owner-a",
    )
    _second_project, second_worker, second_run = _active_worker_and_run(
        store,
        "shared-target-second",
        tenant_id="tenant-a",
        owner_id="owner-b",
    )
    target = tmp_path / "shared-repository"
    target.mkdir()
    mutation_scope = __import__("hashlib").sha256(
        f"repo:{target.resolve()}".encode("utf-8")
    ).hexdigest()

    first = store.acquire_host_run_lease(
        runtime_family="codex",
        lane="mission",
        tenant_id="tenant-a",
        owner_id="owner-a",
        worker_id=first_worker["worker_id"],
        run_id=first_run["run_id"],
        executor_id="executor-first",
        conversation_limit=2,
        mission_limit=3,
        account_mission_limit=4,
        tenant_mission_limit=12,
        mutation_scope=mutation_scope,
        lease_ttl_s=30,
    )
    with pytest.raises(HostRunLeaseCapacityError) as blocked:
        store.acquire_host_run_lease(
            runtime_family="claude",
            lane="mission",
            tenant_id="tenant-a",
            owner_id="owner-b",
            worker_id=second_worker["worker_id"],
            run_id=second_run["run_id"],
            executor_id="executor-second",
            conversation_limit=2,
            mission_limit=3,
            account_mission_limit=4,
            tenant_mission_limit=12,
            mutation_scope=mutation_scope,
            lease_ttl_s=30,
        )
    assert blocked.value.capacity_class == "mutation_scope"

    store.release_host_run_lease(
        first["lease_id"], executor_id="executor-first", reason="run_terminal"
    )
    second = store.acquire_host_run_lease(
        runtime_family="claude",
        lane="mission",
        tenant_id="tenant-a",
        owner_id="owner-b",
        worker_id=second_worker["worker_id"],
        run_id=second_run["run_id"],
        executor_id="executor-second",
        conversation_limit=2,
        mission_limit=3,
        account_mission_limit=4,
        tenant_mission_limit=12,
        mutation_scope=mutation_scope,
        lease_ttl_s=30,
    )
    assert second["mutation_scope"] == mutation_scope


def test_model_authored_mutation_scopes_cannot_bypass_conservative_serialization(
    tmp_path,
):
    store = Store(str(tmp_path / "runtime.sqlite3"))
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    target = tmp_path / "target-repository"
    target.mkdir()
    base = {
        "worker_id": "wrk-structured-target",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "scratch"),
        "bootstrap_bundle_json": json.dumps(
            {"run_mode": "mission", "target_repository_root": str(target)}
        ),
    }
    try:
        first = service._host_mutation_scope(
            {**base, "name": "Research without mutation words"}
        )
        second = service._host_mutation_scope(
            {**base, "name": "EDIT DELETE COMMIT MUTATE"}
        )
        unscoped_first = service._host_mutation_scope(
            {
                **base,
                "bootstrap_bundle_json": json.dumps({"run_mode": "mission"}),
                "name": "EDIT DELETE COMMIT MUTATE",
            }
        )
        unscoped_second = service._host_mutation_scope(
            {
                **base,
                "worker_id": "wrk-other-unscoped",
                "bootstrap_bundle_json": json.dumps({"run_mode": "mission"}),
                "name": "Read-only sounding words cannot weaken the guard",
            }
        )
    finally:
        service.shutdown()

    forged_explicit = service._host_mutation_scope(
        {
            **base,
            "worker_id": "wrk-forged-explicit",
            "bootstrap_bundle_json": json.dumps(
                {"run_mode": "mission", "host_mutation_scope": "unique-forged-scope"}
            ),
        }
    )

    # No caller currently provides authenticated target provenance. Both
    # structured fields are therefore untrusted model/bootstrap data and may
    # not buy a separate mutation lane.
    assert first == second == unscoped_first == unscoped_second == forged_explicit
    assert unscoped_first


@pytest.mark.parametrize(
    ("failure_class", "error"),
    [
        ("host_worker_busy", RuntimeErrorBase("host worker busy")),
        (
            "provider_rate_limited",
            ProviderRateLimitError("provider throttled", retry_after_s=0.1),
        ),
    ],
)
def test_structural_capacity_waits_remain_queued_beyond_retry_budget(
    tmp_path, monkeypatch, failure_class, error
):
    monkeypatch.setenv("GLASSHIVE_MAX_CAPACITY_RETRY_ATTEMPTS", "1")
    monkeypatch.setenv("GLASSHIVE_RETRY_BASE_DELAY_S", "0.1")
    monkeypatch.setenv("GLASSHIVE_HOST_BUSY_RETRY_BASE_DELAY_S", "0.1")
    store = Store(str(tmp_path / f"indefinite-{failure_class}.sqlite3"))
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    project = store.create_project("owner-a", "Capacity", "Wait", "codex-cli")
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner-a",
        name="Capacity worker",
        role="worker",
        profile="codex-cli",
        backend="codex-cli",
        runtime="codex-cli",
        model="test",
    )
    run = store.create_run(
        worker["worker_id"], project["project_id"], "Wait durably", state="running"
    )
    fields = {
        "failure_class": failure_class,
        "failure_retryable": 1,
        "failure_structured": 1,
        "failure_user_message": "Waiting for structural capacity.",
        "failure_recommended_recovery": "Wait.",
        "failure_diagnostic_summary": "Synthetic persistent capacity wait.",
    }
    try:
        for _ in range(8):
            current = store.get_run(run["run_id"])
            if current["state"] == "queued":
                assert store.transition_run_if_state(
                    run["run_id"], "queued", "running", retry_after=None
                )
                current = store.get_run(run["run_id"])
            service._requeue_retryable_run(
                worker, current, error, failure_fields=fields
            )
        durable = store.get_run(run["run_id"])
    finally:
        service.shutdown()

    assert durable["state"] == "queued"
    assert durable["retry_attempts"] == 0
    assert durable["failure_retryable"] == 1
    assert not [
        event
        for event in store.list_events(worker["worker_id"])
        if event["event_type"] == "run.failed"
    ]


def test_structural_waits_do_not_consume_a_later_execution_retry_budget(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GLASSHIVE_MAX_CAPACITY_RETRY_ATTEMPTS", "1")
    monkeypatch.setenv("GLASSHIVE_RETRY_BASE_DELAY_S", "0.01")
    store = Store(str(tmp_path / "wait-then-runtime-retry.sqlite3"))
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    project = store.create_project("owner-a", "Retry budget", "Preserve it", "codex-cli")
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner-a",
        name="Retry budget worker",
        role="worker",
        profile="codex-cli",
        backend="codex-cli",
        runtime="codex-cli",
        model="test",
    )
    run = store.create_run(
        worker["worker_id"], project["project_id"], "Wait, then retry", state="running"
    )
    capacity_fields = {
        "failure_class": "host_capacity",
        "failure_retryable": 1,
        "failure_structured": 1,
        "failure_user_message": "Waiting for capacity.",
        "failure_recommended_recovery": "Wait.",
        "failure_diagnostic_summary": "Synthetic capacity wait.",
    }
    runtime_fields = {
        **capacity_fields,
        "failure_class": "provider_temporarily_unavailable",
        "failure_diagnostic_summary": "Synthetic ordinary retryable failure.",
    }
    try:
        for _ in range(4):
            current = store.get_run(run["run_id"])
            if current["state"] == "queued":
                assert store.transition_run_if_state(
                    run["run_id"], "queued", "running", retry_after=None
                )
                current = store.get_run(run["run_id"])
            service._requeue_retryable_run(
                worker,
                current,
                RuntimeErrorBase("synthetic capacity wait"),
                failure_fields=capacity_fields,
            )

        after_waits = store.get_run(run["run_id"])
        assert after_waits["retry_attempts"] == 0
        assert store.transition_run_if_state(
            run["run_id"], "queued", "running", retry_after=None
        )
        service._requeue_retryable_run(
            worker,
            store.get_run(run["run_id"]),
            RuntimeErrorBase("synthetic execution retry"),
            failure_fields=runtime_fields,
        )
        after_first_execution_failure = store.get_run(run["run_id"])
        assert after_first_execution_failure["state"] == "queued"
        assert after_first_execution_failure["retry_attempts"] == 1

        assert store.transition_run_if_state(
            run["run_id"], "queued", "running", retry_after=None
        )
        service._requeue_retryable_run(
            worker,
            store.get_run(run["run_id"]),
            RuntimeErrorBase("synthetic execution retry again"),
            failure_fields=runtime_fields,
        )
        exhausted = store.get_run(run["run_id"])
    finally:
        service.shutdown()

    assert exhausted["state"] == "failed"
    assert exhausted["retry_attempts"] == 1


class _ReconcilingRuntime(StubRuntime):
    def __init__(self):
        self.identities: dict[str, dict[str, object]] = {}
        self.observer = None

    def set_host_process_observer(self, observer):
        self.observer = observer

    def host_process_identity(self, worker: dict, run_id: str):
        return self.identities.get(run_id)

    def reconcile_worker(self, worker: dict) -> RuntimeInfo:
        identity = self.identities.get(str(worker.get("_active_run_id") or "")) or {}
        return RuntimeInfo(
            runtime="codex-cli",
            model="test",
            gateway_url="",
            gateway_port=None,
            gateway_token=None,
            session_key=None,
            state_dir=None,
            workspace_dir=None,
            pid=int(identity.get("pid") or 0) or None,
        )


def _running_host_run(store: Store, suffix: str):
    project = store.create_project(
        "owner-a", f"Project {suffix}", "Goal", "codex-cli", tenant_id="tenant-a"
    )
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner-a",
        name=f"Worker {suffix}",
        role="worker",
        profile="codex-cli",
        backend="codex-cli",
        runtime="codex-cli",
        model="test",
        execution_mode="host",
        tenant_id="tenant-a",
    )
    run = store.create_run(worker["worker_id"], project["project_id"], "Do it")
    run = store.claim_next_queued_run(worker["worker_id"])
    return worker, run


def test_host_lease_startup_confirmation_is_exact_durable_and_idempotent(tmp_path):
    store = Store(str(tmp_path / "runtime.sqlite3"))
    worker, run = _running_host_run(store, "startup-confirm")
    lease = store.acquire_host_run_lease(
        runtime_family="codex",
        lane="mission",
        tenant_id="tenant-a",
        owner_id="owner-a",
        worker_id=worker["worker_id"],
        run_id=run["run_id"],
        executor_id="executor-a",
        conversation_limit=2,
        mission_limit=3,
        account_mission_limit=4,
        tenant_mission_limit=12,
        lease_ttl_s=30,
    )
    assert lease["startup_token"]
    assert lease["startup_state"] == "reserved"
    assert lease["startup_confirmed_at"] is None

    confirmed = store.confirm_host_run_start(
        worker_id=worker["worker_id"],
        run_id=run["run_id"],
        run_started_at=str(run["started_at"]),
        lease_id=lease["lease_id"],
        startup_token=lease["startup_token"],
        executor_id="executor-a",
        identity_kind="host_process",
        pid=4242,
        process_group=4242,
        process_start_identity="synthetic-start-identity",
        container_id="",
        session_id="host-run-startup",
    )
    assert confirmed is not None
    assert confirmed["lease"]["startup_state"] == "confirmed"
    assert confirmed["lease"]["startup_confirmed_at"]
    assert confirmed["event"]["event_type"] == "run.started"
    replay = store.confirm_host_run_start(
        worker_id=worker["worker_id"],
        run_id=run["run_id"],
        run_started_at=str(run["started_at"]),
        lease_id=lease["lease_id"],
        startup_token=lease["startup_token"],
        executor_id="executor-a",
        identity_kind="host_process",
        pid=4242,
        process_group=4242,
        process_start_identity="synthetic-start-identity",
        container_id="",
        session_id="host-run-startup",
    )
    assert replay is not None and replay["idempotent_replay"] is True
    started_events = [
        item
        for item in store.list_events(worker["worker_id"])
        if item["event_type"] == "run.started"
        and item["run_id"] == run["run_id"]
    ]
    assert len(started_events) == 1
    assert store.confirm_host_run_start(
        worker_id=worker["worker_id"],
        run_id=run["run_id"],
        run_started_at=str(run["started_at"]),
        lease_id=lease["lease_id"],
        startup_token="stale-startup-token",
        executor_id="executor-a",
        identity_kind="host_process",
        pid=4242,
        process_group=4242,
        process_start_identity="synthetic-start-identity",
        container_id="",
        session_id="host-run-startup",
    ) is None


@pytest.mark.parametrize(
    ("requires_identity", "execution_mode", "identity_kind"),
    [
        (True, "host", "in_process"),
        (True, "docker", "host_process"),
        (False, "docker", "docker_session"),
    ],
)
def test_run_start_observer_rejects_runtime_mode_identity_grade_mismatch_before_store(
    tmp_path,
    requires_identity,
    execution_mode,
    identity_kind,
):
    class IdentityGradeRuntime(StubRuntime):
        requires_run_start_identity = requires_identity

    store = Store(str(tmp_path / "runtime.sqlite3"))
    service = WorkersProjectsService(
        store,
        IdentityGradeRuntime(),
        reconcile_on_startup=False,
    )
    project = store.create_project(
        "owner-a",
        "Identity grade mismatch",
        "Reject a lower or cross-substrate startup identity",
        "codex-cli",
    )
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner-a",
        name="Identity grade worker",
        role="worker",
        profile="codex-cli",
        backend="codex-cli",
        runtime="codex-cli",
        model="test",
        execution_mode=execution_mode,
    )
    run = store.create_run(
        worker["worker_id"],
        project["project_id"],
        "Start with the exact permitted identity grade",
        state="running",
    )
    store.update_worker_state(worker["worker_id"], "running")
    lease = store.acquire_host_run_lease(
        runtime_family="codex",
        lane="mission",
        tenant_id="local",
        owner_id="owner-a",
        worker_id=worker["worker_id"],
        run_id=run["run_id"],
        executor_id=service._executor_id,
        conversation_limit=2,
        mission_limit=3,
        account_mission_limit=4,
        tenant_mission_limit=12,
        lease_ttl_s=30,
    )
    with service._pending_run_starts_lock:
        service._pending_run_starts[run["run_id"]] = {
            "run_id": run["run_id"],
            "run_started_at": run["started_at"],
            "worker_id": worker["worker_id"],
            "worker": worker,
            "lease_id": lease["lease_id"],
            "startup_token": lease["startup_token"],
        }
    if identity_kind == "in_process":
        reported_identity = {
            "pid": 0,
            "process_group": 0,
            "process_start_identity": "",
            "container_id": "",
            "session_id": "in-process",
        }
    elif identity_kind == "host_process":
        reported_identity = {
            "pid": 4242,
            "process_group": 4242,
            "process_start_identity": "ps-lstart:cross-grade-host",
            "container_id": "",
            "session_id": "cross-grade-host",
        }
    else:
        reported_identity = {
            "pid": 4242,
            "process_group": 4242,
            "process_start_identity": (
                f"docker:cross-grade-container:cross-grade-session:{run['run_id']}:4242"
            ),
            "container_id": "cross-grade-container",
            "session_id": "cross-grade-session",
        }
    try:
        with pytest.raises(RunStartupRejectedError, match="identity grade"):
            service._observe_run_start(
                {
                    "worker_id": worker["worker_id"],
                    "run_id": run["run_id"],
                    "identity_kind": identity_kind,
                    **reported_identity,
                }
            )
        durable_lease = store.get_host_run_lease(lease["lease_id"]) or {}
        assert durable_lease["startup_state"] == "reserved"
        assert not [
            event
            for event in store.list_events(worker["worker_id"])
            if event["event_type"] == "run.started"
        ]
    finally:
        service.shutdown()


@pytest.mark.parametrize(
    ("pid", "start_identity"),
    [
        (0, "docker:container-a:session-a:run-placeholder:42"),
        (42, ""),
        (42, "docker:container-b:session-a:run-placeholder:42"),
    ],
)
def test_docker_startup_confirmation_requires_generation_bound_identity(
    tmp_path, pid, start_identity
):
    store = Store(str(tmp_path / "runtime.sqlite3"))
    worker, run = _running_host_run(store, f"docker-start-{pid}")
    lease = store.acquire_host_run_lease(
        runtime_family="codex",
        lane="mission",
        tenant_id="tenant-a",
        owner_id="owner-a",
        worker_id=worker["worker_id"],
        run_id=run["run_id"],
        executor_id="executor-a",
        conversation_limit=2,
        mission_limit=3,
        account_mission_limit=4,
        tenant_mission_limit=12,
        lease_ttl_s=30,
    )
    identity = start_identity.replace("run-placeholder", run["run_id"])

    with pytest.raises(ValueError, match="exact identity"):
        store.confirm_host_run_start(
            worker_id=worker["worker_id"],
            run_id=run["run_id"],
            run_started_at=str(run["started_at"]),
            lease_id=lease["lease_id"],
            startup_token=lease["startup_token"],
            executor_id="executor-a",
            identity_kind="docker_session",
            pid=pid,
            process_group=pid,
            process_start_identity=identity,
            container_id="container-a",
            session_id="session-a",
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"process_group": 999},
        {"pid": 999},
        {"process_start_identity": "unexpected-start"},
        {"container_id": "unexpected-container"},
        {"session_id": "unexpected-session"},
    ],
)
def test_in_process_startup_confirmation_requires_the_complete_exact_tuple(
    tmp_path, overrides
):
    store = Store(str(tmp_path / "runtime.sqlite3"))
    worker, run = _running_host_run(store, "in-process-exact-tuple")
    lease = store.acquire_host_run_lease(
        runtime_family="codex",
        lane="mission",
        tenant_id="tenant-a",
        owner_id="owner-a",
        worker_id=worker["worker_id"],
        run_id=run["run_id"],
        executor_id="executor-a",
        conversation_limit=2,
        mission_limit=3,
        account_mission_limit=4,
        tenant_mission_limit=12,
        lease_ttl_s=30,
    )
    identity = {
        "identity_kind": "in_process",
        "pid": 0,
        "process_group": 0,
        "process_start_identity": "",
        "container_id": "",
        "session_id": "in-process",
        **overrides,
    }

    with pytest.raises(ValueError, match="exact identity"):
        store.confirm_host_run_start(
            worker_id=worker["worker_id"],
            run_id=run["run_id"],
            run_started_at=str(run["started_at"]),
            lease_id=lease["lease_id"],
            startup_token=lease["startup_token"],
            executor_id="executor-a",
            **identity,
        )
    assert (store.get_host_run_lease(lease["lease_id"]) or {})[
        "startup_state"
    ] == "reserved"
    assert not [
        event
        for event in store.list_events(worker["worker_id"])
        if event["event_type"] == "run.started"
    ]


def test_compute_release_claim_fences_concurrent_queue_until_token_finalize(tmp_path):
    store = Store(str(tmp_path / "runtime.sqlite3"))
    other_store = Store(str(tmp_path / "runtime.sqlite3"))
    project = store.create_project("owner-a", "Release fence", "Goal", "codex-cli")
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner-a",
        name="Release fence worker",
        role="worker",
        profile="codex-cli",
        backend="codex-cli",
        runtime="codex-cli",
        model="test",
        execution_mode="docker",
    )
    terminal = store.create_run(
        worker["worker_id"], project["project_id"], "Finished", state="completed"
    )
    store.update_worker(worker["worker_id"], state="completed", last_run_id=terminal["run_id"])
    snapshot = store.get_worker(worker["worker_id"])

    claim = store.try_claim_worker_compute_release(
        worker["worker_id"],
        expected_updated_at=snapshot["updated_at"],
        expected_last_run_id=terminal["run_id"],
        expected_state="completed",
        expected_container_id="container-a",
        owner="reaper-a",
        ttl_s=300,
    )
    assert claim is not None
    queued = other_store.create_run(
        worker["worker_id"], project["project_id"], "Concurrent follow-up"
    )

    assert other_store.claim_next_queued_run(worker["worker_id"]) is None
    assert store.finalize_worker_compute_release(
        worker["worker_id"],
        str(claim["token"]),
        int(claim["epoch"]),
        expected_kind="idle",
        compute_released_at="2026-08-13T00:00:00+00:00",
        runtime_fields={"runtime": "codex-cli", "pid": None},
        idle_state="completed",
    ) is not None
    claimed = other_store.claim_next_queued_run(worker["worker_id"])
    assert claimed is not None
    assert claimed["run_id"] == queued["run_id"]


def test_compute_release_finalize_token_mismatch_cannot_clear_newer_claim(tmp_path):
    store = Store(str(tmp_path / "runtime.sqlite3"))
    project = store.create_project("owner-a", "Release CAS", "Goal", "codex-cli")
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner-a",
        name="Release CAS worker",
        role="worker",
        profile="codex-cli",
        backend="codex-cli",
        runtime="codex-cli",
        model="test",
        execution_mode="docker",
    )
    snapshot = store.get_worker(worker["worker_id"])
    claim = store.try_claim_worker_compute_release(
        worker["worker_id"],
        expected_updated_at=snapshot["updated_at"],
        expected_last_run_id="",
        expected_state=str(snapshot["state"]),
        expected_container_id="container-a",
        owner="reaper-new",
        ttl_s=300,
    )
    assert claim is not None

    assert store.finalize_worker_compute_release(
        worker["worker_id"],
        "obsolete-token",
        int(claim["epoch"]),
        expected_kind="idle",
        compute_released_at="2026-08-13T00:00:00+00:00",
        runtime_fields={"runtime": "codex-cli", "pid": None},
        idle_state=str(snapshot["state"]),
    ) is None
    refreshed = store.get_worker(worker["worker_id"])
    assert refreshed["compute_release_token"] == claim["token"]
    assert refreshed["compute_released_at"] is None


def test_stale_lease_reconciliation_keeps_verified_process_and_releases_dead_owner(tmp_path):
    store = Store(str(tmp_path / "runtime.sqlite3"))
    runtime = _ReconcilingRuntime()
    live_worker, live_run = _running_host_run(store, "live")
    dead_worker, dead_run = _running_host_run(store, "dead")
    old = datetime.now(timezone.utc) - timedelta(minutes=5)
    live_lease = store.acquire_host_run_lease(
        runtime_family="codex",
        lane="mission",
        tenant_id="tenant-a",
        owner_id="owner-a",
        worker_id=live_worker["worker_id"],
        run_id=live_run["run_id"],
        executor_id="dead-executor-live",
        conversation_limit=2,
        mission_limit=3,
        account_mission_limit=4,
        tenant_mission_limit=12,
        lease_ttl_s=30,
        now=old,
    )
    dead_lease = store.acquire_host_run_lease(
        runtime_family="codex",
        lane="mission",
        tenant_id="tenant-a",
        owner_id="owner-a",
        worker_id=dead_worker["worker_id"],
        run_id=dead_run["run_id"],
        executor_id="dead-executor-dead",
        conversation_limit=2,
        mission_limit=3,
        account_mission_limit=4,
        tenant_mission_limit=12,
        lease_ttl_s=30,
        now=old,
    )
    for lease, worker, run, pid, identity, session_id in (
        (
            live_lease,
            live_worker,
            live_run,
            777,
            "ps-lstart:live",
            "host-live",
        ),
        (
            dead_lease,
            dead_worker,
            dead_run,
            778,
            "ps-lstart:dead",
            "host-dead",
        ),
    ):
        assert store.confirm_host_run_start(
            worker_id=worker["worker_id"],
            run_id=run["run_id"],
            run_started_at=str(run["started_at"]),
            lease_id=lease["lease_id"],
            startup_token=lease["startup_token"],
            executor_id=lease["executor_id"],
            identity_kind="host_process",
            pid=pid,
            process_group=pid,
            process_start_identity=identity,
            container_id="",
            session_id=session_id,
        )
    runtime.identities[live_run["run_id"]] = {
        "pid": 777,
        "process_group": 777,
        "process_start_identity": "ps-lstart:live",
        "verified": True,
    }
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    try:
        result = service.reconcile_host_run_leases(stale_after_s=0)
    finally:
        service.shutdown()

    assert result == {"renewed": 1, "released": 1, "unchanged": 0}
    assert store.get_host_run_lease(live_lease["lease_id"])["status"] == "active"
    assert store.get_host_run_lease(live_lease["lease_id"])["process_start_identity"] == "ps-lstart:live"
    assert store.get_host_run_lease(dead_lease["lease_id"])["status"] == "released"


def test_stale_unconfirmed_start_lease_remains_fenced_without_death_proof(tmp_path):
    store = Store(str(tmp_path / "runtime.sqlite3"))
    runtime = _ReconcilingRuntime()
    worker, run = _running_host_run(store, "unconfirmed-start")
    old = datetime.now(timezone.utc) - timedelta(minutes=5)
    lease = store.acquire_host_run_lease(
        runtime_family="codex",
        lane="mission",
        tenant_id="tenant-a",
        owner_id="owner-a",
        worker_id=worker["worker_id"],
        run_id=run["run_id"],
        executor_id="dead-executor-unconfirmed",
        conversation_limit=2,
        mission_limit=3,
        account_mission_limit=4,
        tenant_mission_limit=12,
        lease_ttl_s=30,
        now=old,
    )
    assert store.mark_host_run_start_termination_unconfirmed(
        lease_id=lease["lease_id"],
        run_id=run["run_id"],
        executor_id="dead-executor-unconfirmed",
        startup_token=lease["startup_token"],
    )
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    try:
        result = service.reconcile_host_run_leases(stale_after_s=0)
    finally:
        service.shutdown()

    assert result == {"renewed": 0, "released": 0, "unchanged": 1}
    retained = store.get_host_run_lease(lease["lease_id"]) or {}
    assert retained["status"] == "active"
    assert retained["startup_state"] == "termination_unconfirmed"
    assert store.has_unconfirmed_host_run_start(worker["worker_id"]) is True


def test_stale_unconfirmed_start_lease_releases_only_with_exact_absence_proof(tmp_path):
    class ConfirmedAbsentRuntime(_ReconcilingRuntime):
        def host_process_absence(self, worker, run_id):
            return worker["worker_id"].startswith("wrk_") and bool(run_id)

    store = Store(str(tmp_path / "runtime.sqlite3"))
    runtime = ConfirmedAbsentRuntime()
    worker, run = _running_host_run(store, "unconfirmed-start-absent")
    old = datetime.now(timezone.utc) - timedelta(minutes=5)
    lease = store.acquire_host_run_lease(
        runtime_family="codex",
        lane="mission",
        tenant_id="tenant-a",
        owner_id="owner-a",
        worker_id=worker["worker_id"],
        run_id=run["run_id"],
        executor_id="dead-executor-unconfirmed",
        conversation_limit=2,
        mission_limit=3,
        account_mission_limit=4,
        tenant_mission_limit=12,
        lease_ttl_s=30,
        now=old,
    )
    assert store.mark_host_run_start_termination_unconfirmed(
        lease_id=lease["lease_id"],
        run_id=run["run_id"],
        executor_id="dead-executor-unconfirmed",
        startup_token=lease["startup_token"],
    )
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    try:
        result = service.reconcile_host_run_leases(stale_after_s=0)
    finally:
        service.shutdown()

    assert result == {"renewed": 0, "released": 1, "unchanged": 0}
    released = store.get_host_run_lease(lease["lease_id"]) or {}
    assert released["status"] == "released"
    assert released["release_reason"] == "startup_generation_cleaned"
    assert store.has_unconfirmed_host_run_start(worker["worker_id"]) is False
    assert (store.get_run(run["run_id"]) or {})["state"] == "queued"


class _ReservedStartupCrashRuntime(StubRuntime):
    """Synthetic durable-session reader for the reserve/publish/confirm crash window."""

    def __init__(self) -> None:
        self.identities: dict[str, dict[str, object]] = {}
        self.cleanup_calls: list[tuple[str, str, dict[str, object]]] = []
        self.cleanup_confirmed = True

    def host_process_identity(self, worker: dict, run_id: str):
        return self.identities.get(run_id)

    def cleanup_unconfirmed_run_start(
        self, worker: dict, run_id: str, lease_identity: dict[str, object]
    ) -> bool:
        self.cleanup_calls.append(
            (str(worker["worker_id"]), str(run_id), dict(lease_identity))
        )
        return self.cleanup_confirmed

    def reconcile_worker(self, worker: dict) -> RuntimeInfo:
        identity = self.identities.get(str(worker.get("_active_run_id") or "")) or {}
        return RuntimeInfo(
            runtime="codex-cli",
            model="test",
            gateway_url="",
            gateway_port=None,
            gateway_token=None,
            session_key=None,
            state_dir=None,
            workspace_dir=None,
            pid=int(identity.get("pid") or 0) or None,
        )


def _reserved_startup_crash_fixture(
    tmp_path,
    *,
    execution_mode: str,
    captured_identity: dict[str, object],
):
    store = Store(str(tmp_path / f"{execution_mode}-startup-crash.sqlite3"))
    project = store.create_project(
        "owner-a", f"{execution_mode} startup crash", "Recover exact start", "codex-cli"
    )
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner-a",
        name=f"{execution_mode} crash worker",
        role="worker",
        profile="codex-cli",
        backend="codex-cli",
        runtime="codex-cli",
        model="test",
        execution_mode=execution_mode,
        bootstrap_bundle={
            "callbacks": {
                "events_webhook_url": "http://callback.invalid/glasshive",
                "conversation_id": "conv-startup-crash",
                "parent_message_id": "msg-user",
                "message_id": "msg-assistant",
            }
        },
    )
    run = store.create_run(worker["worker_id"], project["project_id"], "Recover me")
    run = store.claim_next_queued_run(worker["worker_id"])
    old = datetime.now(timezone.utc) - timedelta(minutes=5)
    lease = store.acquire_host_run_lease(
        runtime_family="codex",
        lane="mission",
        tenant_id="local",
        owner_id="owner-a",
        worker_id=worker["worker_id"],
        run_id=run["run_id"],
        executor_id="dead-executor",
        conversation_limit=2,
        mission_limit=3,
        account_mission_limit=4,
        tenant_mission_limit=12,
        lease_ttl_s=30,
        now=old,
    )
    identity = dict(captured_identity)
    identity["process_start_identity"] = str(
        identity.get("process_start_identity") or ""
    ).replace("{run_id}", str(run["run_id"]))
    store.heartbeat_host_run_lease(
        lease["lease_id"],
        executor_id="dead-executor",
        pid=int(identity.get("pid") or 0) or None,
        process_group=int(identity.get("process_group") or 0) or None,
        process_start_identity=str(identity["process_start_identity"]),
        startup_identity_kind=str(identity.get("identity_kind") or ""),
        startup_container_id=str(identity.get("container_id") or ""),
        startup_session_id=str(identity.get("session_id") or ""),
        lease_ttl_s=30,
        now=old,
    )
    identity["startup_token_digest"] = hashlib.sha256(
        str(lease["startup_token"]).encode("utf-8")
    ).hexdigest()
    return store, store.get_worker(worker["worker_id"]), run, store.get_host_run_lease(
        lease["lease_id"]
    ), identity


def test_reserved_restart_reconstructs_file_published_generation_before_lease_observer(
    tmp_path,
):
    store, worker, run, lease, identity = _reserved_startup_crash_fixture(
        tmp_path,
        execution_mode="host",
        captured_identity={
            "identity_kind": "host_process",
            "pid": 951,
            "process_group": 951,
            "process_start_identity": "ps-lstart:file-published",
            "container_id": "",
            "session_id": "host-file-published",
            "verified": True,
        },
    )
    # Simulate a crash after active_session publication but before its observer
    # could copy the exact generation into the lease row.
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE host_run_leases SET pid = NULL, process_group = NULL, "
            "process_start_identity = '', startup_identity_kind = '', "
            "startup_container_id = '', startup_session_id = '' WHERE lease_id = ?",
            (lease["lease_id"],),
        )
    runtime = _ReservedStartupCrashRuntime()
    runtime.identities[run["run_id"]] = identity
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    service._deliver_callback_record = lambda *args, **kwargs: None
    try:
        result = service.reconcile_host_run_leases(stale_after_s=0)
    finally:
        service.shutdown()

    assert result == {"renewed": 1, "released": 0, "unchanged": 0}
    confirmed = store.get_host_run_lease(lease["lease_id"]) or {}
    assert confirmed["startup_state"] == "confirmed"
    assert confirmed["process_start_identity"] == "ps-lstart:file-published"


@pytest.mark.parametrize(
    ("execution_mode", "captured_identity"),
    [
        (
            "docker",
            {
                "identity_kind": "docker_session",
                "pid": 501,
                "process_group": 501,
                "process_start_identity": "docker:container-old:job-crash:{run_id}:71",
                "container_id": "container-old",
                "session_id": "job-crash",
                "verified": True,
            },
        ),
        (
            "host",
            {
                "identity_kind": "host_process",
                "pid": 601,
                "process_group": 601,
                "process_start_identity": "ps-lstart:synthetic-old-generation",
                "container_id": "",
                "session_id": "host-crash",
                "verified": True,
            },
        ),
    ],
)
def test_reserved_verified_restart_confirms_original_generation_exactly_once(
    tmp_path, execution_mode, captured_identity
):
    store, worker, run, lease, identity = _reserved_startup_crash_fixture(
        tmp_path,
        execution_mode=execution_mode,
        captured_identity=captured_identity,
    )
    runtime = _ReservedStartupCrashRuntime()
    runtime.identities[run["run_id"]] = identity
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    service._deliver_callback_record = lambda *args, **kwargs: None
    try:
        first = service.reconcile_host_run_leases(stale_after_s=0)
        second = service.reconcile_host_run_leases(stale_after_s=0)
    finally:
        service.shutdown()

    confirmed = store.get_host_run_lease(lease["lease_id"]) or {}
    assert first == {"renewed": 1, "released": 0, "unchanged": 0}
    assert second == {"renewed": 1, "released": 0, "unchanged": 0}
    assert confirmed["startup_state"] == "confirmed"
    assert confirmed["startup_token"] == lease["startup_token"]
    assert confirmed["startup_identity_kind"] == identity["identity_kind"]
    assert confirmed["startup_container_id"] == identity["container_id"]
    assert confirmed["startup_session_id"] == identity["session_id"]
    started = [
        event
        for event in store.list_events(worker["worker_id"])
        if event["event_type"] == "run.started" and event["run_id"] == run["run_id"]
    ]
    assert len(started) == 1
    with sqlite3.connect(store.db_path) as conn:
        callbacks = conn.execute(
            "SELECT callback_id, event_type, payload_json FROM callback_outbox "
            "WHERE run_id = ? AND event_type = 'run.started'",
            (run["run_id"],),
        ).fetchall()
    assert len(callbacks) == 1
    payload = json.loads(callbacks[0][2])
    assert payload["event"] == "run.started"
    assert payload["run_id"] == run["run_id"]
    assert "startup_token" not in payload
    assert len(callbacks[0][2].encode("utf-8")) < 16_384


@pytest.mark.parametrize(
    ("execution_mode", "captured", "replacement"),
    [
        (
            "host",
            {
                "identity_kind": "host_process",
                "pid": 701,
                "process_group": 701,
                "process_start_identity": "ps-lstart:captured-old-generation",
                "container_id": "",
                "session_id": "host-old",
                "verified": True,
            },
            {
                "pid": 702,
                "process_group": 702,
                "process_start_identity": "ps-lstart:replacement-generation",
                "session_id": "host-replacement",
            },
        ),
        (
            "docker",
            {
                "identity_kind": "docker_session",
                "pid": 801,
                "process_group": 801,
                "process_start_identity": "docker:container-old:job-old:{run_id}:81",
                "container_id": "container-old",
                "session_id": "job-old",
                "verified": True,
            },
            {
                "pid": 802,
                "process_group": 802,
                "process_start_identity": "docker:container-new:job-new:{run_id}:82",
                "container_id": "container-new",
                "session_id": "job-new",
            },
        ),
    ],
)
def test_reserved_restart_rejects_replacement_generation_and_requeues_after_exact_cleanup(
    tmp_path, execution_mode, captured, replacement
):
    store, worker, run, lease, captured = _reserved_startup_crash_fixture(
        tmp_path,
        execution_mode=execution_mode,
        captured_identity=captured,
    )
    runtime = _ReservedStartupCrashRuntime()
    runtime.identities[run["run_id"]] = {
        **captured,
        **{
            key: (
                str(value).replace("{run_id}", str(run["run_id"]))
                if isinstance(value, str)
                else value
            )
            for key, value in replacement.items()
        },
    }
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    service._deliver_callback_record = lambda *args, **kwargs: None
    try:
        result = service.reconcile_host_run_leases(stale_after_s=0)
    finally:
        service.shutdown()

    assert result == {"renewed": 0, "released": 1, "unchanged": 0}
    assert runtime.cleanup_calls == [(worker["worker_id"], run["run_id"], {
        key: captured[key]
        for key in (
            "identity_kind",
            "pid",
            "process_group",
            "process_start_identity",
            "container_id",
            "session_id",
        )
    })]
    assert (store.get_host_run_lease(lease["lease_id"]) or {})["status"] == "released"
    recovered_run = store.get_run(run["run_id"]) or {}
    assert recovered_run["state"] == "queued"
    assert recovered_run["failure_class"] == "service_startup_fenced"
    assert not [
        event
        for event in store.list_events(worker["worker_id"])
        if event["event_type"] == "run.started" and event["run_id"] == run["run_id"]
    ]


def test_reserved_restart_cleanup_uncertainty_keeps_durable_start_fence(tmp_path):
    captured = {
        "identity_kind": "host_process",
        "pid": 901,
        "process_group": 901,
        "process_start_identity": "ps-lstart:captured-uncertain",
        "container_id": "",
        "session_id": "host-uncertain",
        "verified": True,
    }
    store, worker, run, lease, captured = _reserved_startup_crash_fixture(
        tmp_path,
        execution_mode="host",
        captured_identity=captured,
    )
    runtime = _ReservedStartupCrashRuntime()
    runtime.cleanup_confirmed = False
    runtime.identities[run["run_id"]] = {
        **captured,
        "pid": 902,
        "process_group": 902,
        "process_start_identity": "ps-lstart:replacement-uncertain",
        "session_id": "host-replacement",
    }
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    try:
        result = service.reconcile_host_run_leases(stale_after_s=0)
    finally:
        service.shutdown()

    assert result == {"renewed": 0, "released": 0, "unchanged": 1}
    retained = store.get_host_run_lease(lease["lease_id"]) or {}
    assert retained["status"] == "active"
    assert retained["startup_state"] == "termination_unconfirmed"
    assert (store.get_run(run["run_id"]) or {})["state"] == "running"


def test_termination_unconfirmed_retries_exact_cleanup_and_requeues_once_proven(
    tmp_path,
):
    captured = {
        "identity_kind": "host_process",
        "pid": 911,
        "process_group": 911,
        "process_start_identity": "ps-lstart:captured-retry-cleanup",
        "container_id": "",
        "session_id": "host-retry-cleanup",
        "verified": True,
    }
    store, worker, run, lease, captured = _reserved_startup_crash_fixture(
        tmp_path,
        execution_mode="host",
        captured_identity=captured,
    )
    runtime = _ReservedStartupCrashRuntime()
    runtime.cleanup_confirmed = False
    runtime.identities[run["run_id"]] = {
        **captured,
        "pid": 912,
        "process_group": 912,
        "process_start_identity": "ps-lstart:replacement-retry-cleanup",
        "session_id": "host-replacement-retry-cleanup",
    }
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    try:
        first = service.reconcile_host_run_leases(stale_after_s=0)
        assert first == {"renewed": 0, "released": 0, "unchanged": 1}
        assert (store.get_host_run_lease(lease["lease_id"]) or {})[
            "startup_state"
        ] == "termination_unconfirmed"

        runtime.cleanup_confirmed = True
        second = service.reconcile_host_run_leases(stale_after_s=0)
    finally:
        service.shutdown()

    assert second == {"renewed": 0, "released": 1, "unchanged": 0}
    released = store.get_host_run_lease(lease["lease_id"]) or {}
    assert released["status"] == "released"
    assert released["release_reason"] == "startup_generation_cleaned"
    recovered = store.get_run(run["run_id"]) or {}
    assert recovered["state"] == "queued"
    assert recovered["failure_class"] == "service_startup_fenced"
    cleanup_identity = {
        key: captured[key]
        for key in (
            "identity_kind",
            "pid",
            "process_group",
            "process_start_identity",
            "container_id",
            "session_id",
        )
    }
    assert runtime.cleanup_calls == [
        (worker["worker_id"], run["run_id"], cleanup_identity),
        (worker["worker_id"], run["run_id"], cleanup_identity),
    ]
