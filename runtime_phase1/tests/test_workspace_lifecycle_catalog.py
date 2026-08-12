from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock, Thread
import json
import shutil
import time

import pytest

from workers_projects_runtime.control_plane import ControlPlaneConflict, ControlPlaneStore
from workers_projects_runtime.openclaw_runtime import RuntimeErrorBase, RuntimeInfo, StubRuntime
from workers_projects_runtime.service import WorkersProjectsService
from workers_projects_runtime.signed_links import (
    create_signed_link_ref,
    resolve_signed_link_ref,
    sign_link_token,
)
from workers_projects_runtime.store import Store


class PersistentFixtureRuntime(StubRuntime):
    def __init__(self, root: Path) -> None:
        self.root = root

    def _runtime_info(self, worker: dict, *, pid: int | None) -> RuntimeInfo:
        worker_root = self.root / str(worker["worker_id"])
        (worker_root / "state").mkdir(parents=True, exist_ok=True)
        (worker_root / "state" / "workspace").mkdir(parents=True, exist_ok=True)
        return RuntimeInfo(
            runtime="fixture-runtime",
            model="fixture/model",
            gateway_url=f"http://127.0.0.1/fixture/{worker['worker_id']}",
            gateway_port=None,
            gateway_token=None,
            session_key=f"fixture:{worker['worker_id']}",
            state_dir=str(worker_root / "state"),
            workspace_dir=str(worker_root / "state" / "workspace"),
            pid=pid,
        )

    def managed_worker_root(self, worker: dict) -> Path:
        return self.root / str(worker["worker_id"])


def _project(store: Store, *, title: str = "Synthetic workspace") -> dict:
    return store.create_project(
        owner_id="user-a",
        title=title,
        goal="Exercise safe workspace lifecycle behavior.",
        default_worker_profile="codex-cli",
        tenant_id="tenant-a",
    )


def _worker(
    service: WorkersProjectsService,
    project: dict,
    *,
    name: str,
    workspace_kind: str,
    execution_mode: str = "docker",
    workspace_root: str | None = None,
) -> dict:
    return service.create_worker(
        project_id=project["project_id"],
        tenant_id="tenant-a",
        owner_id="user-a",
        name=name,
        role="main",
        profile="codex-cli",
        backend="codex-cli",
        workspace_kind=workspace_kind,
        execution_mode=execution_mode,
        workspace_root=workspace_root,
    )


def _expire_worker(store: Store, worker_id: str) -> str:
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with store._connect() as conn:
        conn.execute(
            "UPDATE workers SET state = 'ready', updated_at = ? WHERE worker_id = ?",
            (old, worker_id),
        )
    return old


def test_ephemeral_reaper_removes_only_expired_managed_one_off_state(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_EPHEMERAL_GC_ENABLED", "true")
    monkeypatch.setenv("GLASSHIVE_EPHEMERAL_RETENTION_S", "60")
    store = Store(str(tmp_path / "runtime.db"))
    runtime_root = tmp_path / "managed-runtime"
    runtime = PersistentFixtureRuntime(runtime_root)
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    try:
        named_project = _project(store, title="Saved project")
        named = _worker(service, named_project, name="Saved workspace", workspace_kind="named")
        ephemeral_project = _project(store, title="One-off project")
        ephemeral = _worker(service, ephemeral_project, name="One-off workspace", workspace_kind="ephemeral")
        named_file = Path(named["workspace_dir"]) / "keep.txt"
        ephemeral_file = Path(ephemeral["workspace_dir"]) / "discard.txt"
        named_file.write_text("persistent", encoding="utf-8")
        ephemeral_file.write_text("temporary", encoding="utf-8")
        old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        with store._connect() as conn:
            conn.execute("UPDATE workers SET state = 'ready', updated_at = ?", (old,))

        reaped = service.reap_ephemeral_workspaces_once(now=datetime.now(timezone.utc))
    finally:
        service.shutdown()

    assert [item["worker_id"] for item in reaped] == [ephemeral["worker_id"]]
    assert store.get_worker(ephemeral["worker_id"]) is None
    assert not (runtime_root / ephemeral["worker_id"]).exists()
    assert store.get_project(ephemeral_project["project_id"]) is None
    assert store.get_worker(named["worker_id"]) is not None
    assert store.get_project(named_project["project_id"]) is not None
    assert named_file.read_text(encoding="utf-8") == "persistent"


def test_ephemeral_reaper_fails_closed_for_active_or_scheduled_work(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_EPHEMERAL_GC_ENABLED", "true")
    monkeypatch.setenv("GLASSHIVE_EPHEMERAL_RETENTION_S", "60")
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(
        store,
        PersistentFixtureRuntime(tmp_path / "managed-runtime"),
        reconcile_on_startup=False,
    )
    try:
        active_project = _project(store, title="Active project")
        active = _worker(service, active_project, name="Active one-off", workspace_kind="ephemeral")
        scheduled_project = _project(store, title="Scheduled project")
        scheduled = _worker(service, scheduled_project, name="Scheduled one-off", workspace_kind="ephemeral")
        store.create_run(active["worker_id"], active_project["project_id"], "Still running", state="running")
        store.create_scheduled_run(
            worker_id=scheduled["worker_id"],
            project_id=scheduled_project["project_id"],
            tenant_id="tenant-a",
            owner_id="user-a",
            instruction="Run later",
            run_at="2099-01-01T00:00:00+00:00",
        )
        old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        with store._connect() as conn:
            conn.execute("UPDATE workers SET state = 'ready', updated_at = ?", (old,))

        assert service.reap_ephemeral_workspaces_once(now=datetime.now(timezone.utc)) == []
    finally:
        service.shutdown()

    assert store.get_worker(active["worker_id"]) is not None
    assert store.get_worker(scheduled["worker_id"]) is not None


@pytest.mark.parametrize("execution_mode", ["docker", "host"])
def test_ephemeral_reaper_reclaims_unmanaged_metadata_without_touching_user_files_or_quota(
    tmp_path,
    monkeypatch,
    execution_mode,
):
    monkeypatch.setenv("GLASSHIVE_EPHEMERAL_GC_ENABLED", "true")
    monkeypatch.setenv("GLASSHIVE_EPHEMERAL_RETENTION_S", "60")
    monkeypatch.setenv("GLASSHIVE_MAX_WORKSPACES_PER_USER", "1")
    monkeypatch.setenv("GLASSHIVE_HOST_WORKERS_ENABLED", "true")
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(
        store,
        PersistentFixtureRuntime(tmp_path / "managed-runtime"),
        reconcile_on_startup=False,
    )
    external_root = tmp_path / "user-managed-source"
    external_root.mkdir()
    protected_file = external_root / "must-not-delete.txt"
    protected_file.write_text("user managed", encoding="utf-8")
    try:
        project = _project(store, title=f"{execution_mode} one-off")
        worker = _worker(
            service,
            project,
            name=f"{execution_mode} one-off",
            workspace_kind="ephemeral",
            execution_mode=execution_mode,
            workspace_root=str(external_root),
        )
        old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        with store._connect() as conn:
            conn.execute(
                "UPDATE workers SET state = 'ready', updated_at = ? WHERE worker_id = ?",
                (old, worker["worker_id"]),
            )

        reaped = service.reap_ephemeral_workspaces_once(now=datetime.now(timezone.utc))
        replacement_project = _project(store, title="Replacement one-off")
        replacement = _worker(
            service,
            replacement_project,
            name="Replacement one-off",
            workspace_kind="ephemeral",
        )
    finally:
        service.shutdown()

    assert [item["worker_id"] for item in reaped] == [worker["worker_id"]]
    assert store.get_worker(worker["worker_id"]) is None
    assert store.get_project(project["project_id"]) is None
    assert store.get_worker(replacement["worker_id"]) is not None
    assert protected_file.read_text(encoding="utf-8") == "user managed"


def test_named_compute_reap_restart_and_resume_preserve_identity_and_files(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_EPHEMERAL_GC_ENABLED", "true")
    monkeypatch.setenv("GLASSHIVE_IDLE_TERMINATE_AFTER_S", "60")
    store = Store(str(tmp_path / "runtime.db"))
    runtime_root = tmp_path / "managed-runtime"
    service = WorkersProjectsService(
        store,
        PersistentFixtureRuntime(runtime_root),
        reconcile_on_startup=False,
    )
    project = _project(store, title="Persistent project")
    named = _worker(service, project, name="Persistent workspace", workspace_kind="named")
    workspace_file = Path(named["workspace_dir"]) / "continuity.txt"
    workspace_file.write_text("preserved across compute lifecycle", encoding="utf-8")
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    with store._connect() as conn:
        conn.execute(
            "UPDATE workers SET state = 'ready', updated_at = ?, compute_released_at = NULL WHERE worker_id = ?",
            (old, named["worker_id"]),
        )

    reaped = service.reap_idle_workers_once()
    service.shutdown()
    after_reap = store.get_worker(named["worker_id"])
    assert [item["worker_id"] for item in reaped] == [named["worker_id"]]
    assert after_reap is not None
    assert after_reap["state"] == "paused"
    assert after_reap["compute_released_at"]
    assert workspace_file.read_text(encoding="utf-8") == "preserved across compute lifecycle"

    restarted_store = Store(str(tmp_path / "runtime.db"))
    restarted = WorkersProjectsService(
        restarted_store,
        PersistentFixtureRuntime(runtime_root),
        reconcile_on_startup=False,
    )
    try:
        resumed = restarted.resume_worker(named["worker_id"])
    finally:
        restarted.shutdown()

    assert resumed["worker_id"] == named["worker_id"]
    assert resumed["state"] == "ready"
    assert workspace_file.read_text(encoding="utf-8") == "preserved across compute lifecycle"


def test_catalog_reports_saved_account_capability_and_next_schedule_readiness(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_EPHEMERAL_GC_ENABLED", "false")
    database = tmp_path / "runtime.db"
    store = Store(str(database))
    control_plane = ControlPlaneStore(str(database))
    account = control_plane.create_provider_account(
        tenant_id="tenant-a",
        owner_id="user-a",
        provider="codex",
        label="Personal Codex",
        auth_method="subscription",
        platform_support="supported",
        secret_locator="native-home://auto",
        status="action_required",
    )
    connection = control_plane.create_connection(
        tenant_id="tenant-a",
        owner_id="user-a",
        kind="documents",
        adapter="brokered-mcp",
        label="Document source",
        status="action_required",
        secret_locator="broker://synthetic-connection",
        scopes=["documents:read"],
    )
    service = WorkersProjectsService(
        store,
        PersistentFixtureRuntime(tmp_path / "managed-runtime"),
        control_plane_store=control_plane,
        reconcile_on_startup=False,
    )
    try:
        project = _project(store, title="Readiness project")
        workspace = service.create_worker(
            project_id=project["project_id"],
            tenant_id="tenant-a",
            owner_id="user-a",
            name="Readiness workspace",
            role="main",
            profile="codex-cli",
            backend="codex-cli",
            workspace_kind="named",
            tags=["Finance"],
            bootstrap_bundle={
                "provider_account": {
                    "policy": "personal_required",
                    "account_id": account["account_id"],
                }
            },
        )
        with control_plane._connect() as conn:
            conn.execute(
                """
                INSERT INTO workspace_capability_grants
                    (grant_id, tenant_id, owner_id, worker_id, connection_id, scopes_json,
                     prior_bootstrap_bundle_json, applied_bootstrap_bundle_json,
                     installation_plan_json, probe_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, '{}', '{}', '[]', '{}', ?)
                """,
                (
                    "grant_synthetic_readiness",
                    "tenant-a",
                    "user-a",
                    workspace["worker_id"],
                    connection["connection_id"],
                    json.dumps(["documents:read"]),
                    time.time(),
                ),
            )
        store.create_scheduled_run(
            worker_id=workspace["worker_id"],
            project_id=project["project_id"],
            tenant_id="tenant-a",
            owner_id="user-a",
            instruction="Run the next review",
            run_at="2099-02-03T04:05:00+00:00",
        )

        catalog = service.list_workspace_catalog(
            tenant_id="tenant-a",
            owner_id="user-a",
            workspace_kinds={"named"},
        )
    finally:
        service.shutdown()

    item = catalog["items"][0]
    assert item["tags"] == ["finance"]
    assert item["provider_readiness"] == {
        "readiness": "action_required",
        "policy": "personal_required",
        "account_id": account["account_id"],
        "provider": "codex",
        "label": "Personal Codex",
        "status": "action_required",
    }
    assert item["capability_readiness"] == {
        "active_grants": 1,
        "unavailable_grants": 1,
        "readiness": "action_required",
    }
    assert item["next_schedule_at"] == "2099-02-03T04:05:00+00:00"
    assert item["schedule_readiness"] == "ready"


def test_catalog_reports_personal_preferred_without_account_as_truthful_deployment_fallback(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GLASSHIVE_EPHEMERAL_GC_ENABLED", "false")
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(
        store,
        PersistentFixtureRuntime(tmp_path / "managed-runtime"),
        control_plane_store=ControlPlaneStore(str(tmp_path / "runtime.db")),
        reconcile_on_startup=False,
    )
    try:
        project = _project(store, title="Fallback project")
        service.create_worker(
            project_id=project["project_id"],
            tenant_id="tenant-a",
            owner_id="user-a",
            name="Fallback workspace",
            role="main",
            profile="codex-cli",
            backend="codex-cli",
            workspace_kind="named",
            bootstrap_bundle={"provider_account": {"policy": "personal_preferred"}},
        )
        item = service.list_workspace_catalog(
            tenant_id="tenant-a",
            owner_id="user-a",
            workspace_kinds={"named"},
        )["items"][0]
    finally:
        service.shutdown()

    assert item["provider_readiness"] == {
        "readiness": "deployment_managed",
        "policy": "personal_preferred",
        "fallback": True,
    }


def test_hosted_codex_catalog_requires_an_effective_deployment_provider_route(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    for name in (
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_API_BASE",
        "OPENAI_REVERSE_PROXY",
        "PORTKEY_API_KEY",
        "PORTKEY_BASE_URL",
        "WPR_CODEX_CLI_BASE_URL",
        "WPR_CODEX_CLI_ENV_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(
        store,
        PersistentFixtureRuntime(tmp_path / "managed-runtime"),
        control_plane_store=ControlPlaneStore(str(tmp_path / "runtime.db")),
        reconcile_on_startup=False,
    )
    try:
        project = _project(store, title="Deployment route project")
        service.create_worker(
            project_id=project["project_id"],
            tenant_id="tenant-a",
            owner_id="user-a",
            name="Deployment route workspace",
            role="main",
            profile="codex-cli",
            backend="codex-cli",
            workspace_kind="named",
            bootstrap_bundle={"provider_account": {"policy": "legacy"}},
        )

        unavailable = service.list_workspace_catalog(
            tenant_id="tenant-a",
            owner_id="user-a",
            workspace_kinds={"named"},
        )["items"][0]["provider_readiness"]
        monkeypatch.setenv("OPENAI_BASE_URL", "https://provider.example.test/openai/v1")
        monkeypatch.setenv("OPENAI_API_KEY", "synthetic-deployment-key")
        ready = service.list_workspace_catalog(
            tenant_id="tenant-a",
            owner_id="user-a",
            workspace_kinds={"named"},
        )["items"][0]["provider_readiness"]
    finally:
        service.shutdown()

    assert unavailable == {
        "readiness": "action_required",
        "policy": "legacy",
        "status": "deployment_provider_unavailable",
    }
    assert ready == {
        "readiness": "deployment_managed",
        "policy": "legacy",
    }


def test_hosted_deployment_route_blocks_dispatch_before_run_or_runtime_mutation(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setenv("GLASSHIVE_EPHEMERAL_GC_ENABLED", "false")
    for name in (
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_API_BASE",
        "OPENAI_REVERSE_PROXY",
        "PORTKEY_API_KEY",
        "PORTKEY_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    store = Store(str(tmp_path / "runtime.db"))
    runtime = PersistentFixtureRuntime(tmp_path / "managed-runtime")
    service = WorkersProjectsService(
        store,
        runtime,
        control_plane_store=ControlPlaneStore(str(tmp_path / "runtime.db")),
        reconcile_on_startup=False,
    )
    service._ensure_worker_processor = lambda _worker_id: None  # type: ignore[method-assign]
    try:
        project = _project(store, title="Dispatch gate project")
        worker = service.create_worker(
            project_id=project["project_id"],
            tenant_id="tenant-a",
            owner_id="user-a",
            name="Dispatch gate workspace",
            role="main",
            profile="codex-cli",
            backend="codex-cli",
            workspace_kind="named",
            bootstrap_bundle={"provider_account": {"policy": "legacy"}},
        )
        before = service.require_worker(worker["worker_id"])
        with pytest.raises(ControlPlaneConflict, match="Work AI is not set up"):
            service.assign_run(worker["worker_id"], "Must not dispatch")
        after = service.require_worker(worker["worker_id"])
        assert store.list_runs_for_worker(worker["worker_id"]) == []
        assert after["state"] == before["state"]
        assert after["last_run_id"] == before["last_run_id"]

        monkeypatch.setenv("OPENAI_API_KEY", "synthetic-standard-openai-key")
        run = service.assign_run(worker["worker_id"], "Now dispatch")
        assert run["state"] == "queued"
        assert len(store.list_runs_for_worker(worker["worker_id"])) == 1
    finally:
        service.shutdown()


def test_hosted_steer_checks_replacement_route_before_interrupting_active_work(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setenv("GLASSHIVE_EPHEMERAL_GC_ENABLED", "false")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-standard-openai-key")
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(
        store,
        PersistentFixtureRuntime(tmp_path / "managed-runtime"),
        control_plane_store=ControlPlaneStore(str(tmp_path / "runtime.db")),
        reconcile_on_startup=False,
    )
    service._ensure_worker_processor = lambda _worker_id: None  # type: ignore[method-assign]
    try:
        project = _project(store, title="Steer gate project")
        worker = service.create_worker(
            project_id=project["project_id"],
            tenant_id="tenant-a",
            owner_id="user-a",
            name="Steer gate workspace",
            role="main",
            profile="codex-cli",
            backend="codex-cli",
            workspace_kind="named",
            bootstrap_bundle={"provider_account": {"policy": "legacy"}},
        )
        active = service.assign_run(worker["worker_id"], "Keep this work active")
        store.update_run(active["run_id"], state="running")
        store.update_worker_state(worker["worker_id"], "running")
        monkeypatch.delenv("OPENAI_API_KEY")

        with pytest.raises(ControlPlaneConflict, match="Work AI is not set up"):
            service.steer_worker(worker["worker_id"], "Do not interrupt unless replacement can run")

        assert store.get_run(active["run_id"])["state"] == "running"
        assert [item["run_id"] for item in store.list_runs_for_worker(worker["worker_id"])] == [
            active["run_id"]
        ]

        interrupted = service.interrupt_worker(worker["worker_id"])
        assert interrupted["worker_id"] == worker["worker_id"]
        assert store.get_run(active["run_id"])["state"] == "interrupted"
    finally:
        service.shutdown()


def test_busy_preferred_account_requires_ready_deployment_fallback_before_dispatch_or_steer(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    monkeypatch.setenv("GLASSHIVE_EPHEMERAL_GC_ENABLED", "false")
    for name in (
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_API_BASE",
        "OPENAI_REVERSE_PROXY",
        "PORTKEY_API_KEY",
        "PORTKEY_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    database = tmp_path / "runtime.db"
    store = Store(str(database))
    control = ControlPlaneStore(str(database))
    account = control.create_provider_account(
        tenant_id="tenant-a",
        owner_id="user-a",
        provider="codex",
        label="Synthetic preferred account",
        auth_method="subscription",
        platform_support="supported",
        secret_locator="native-home://synthetic",
        status="ready",
    )
    service = WorkersProjectsService(
        store,
        PersistentFixtureRuntime(tmp_path / "managed-runtime"),
        control_plane_store=control,
        reconcile_on_startup=False,
    )
    service._ensure_worker_processor = lambda _worker_id: None  # type: ignore[method-assign]
    try:
        project = _project(store, title="Busy preferred account")
        lease_owner = service.create_worker(
            project_id=project["project_id"],
            tenant_id="tenant-a",
            owner_id="user-a",
            name="Lease owner",
            role="main",
            profile="codex-cli",
            backend="codex-cli",
            workspace_kind="named",
            bootstrap_bundle={"provider_account": {"policy": "legacy"}},
        )
        preferred = service.create_worker(
            project_id=project["project_id"],
            tenant_id="tenant-a",
            owner_id="user-a",
            name="Preferred fallback",
            role="main",
            profile="codex-cli",
            backend="codex-cli",
            workspace_kind="named",
            bootstrap_bundle={
                "provider_account": {
                    "policy": "personal_preferred",
                    "account_id": account["account_id"],
                }
            },
        )
        busy_lease = control.acquire_provider_lease(
            account_id=account["account_id"],
            tenant_id="tenant-a",
            owner_id="user-a",
            lane="provider-verify",
            worker_id=lease_owner["worker_id"],
            run_id="run_synthetic_busy_account",
            ttl_seconds=180,
        )

        with pytest.raises(ControlPlaneConflict, match="Work AI is not set up"):
            service.assign_run(preferred["worker_id"], "Must not queue doomed fallback work")
        assert store.list_runs_for_worker(preferred["worker_id"]) == []

        monkeypatch.setenv("OPENAI_API_KEY", "synthetic-standard-openai-key")
        active = service.assign_run(preferred["worker_id"], "Keep this work active")
        store.update_run(active["run_id"], state="running")
        store.update_worker_state(preferred["worker_id"], "running")
        monkeypatch.delenv("OPENAI_API_KEY")

        with pytest.raises(ControlPlaneConflict, match="Work AI is not set up"):
            service.steer_worker(
                preferred["worker_id"],
                "Do not interrupt unless the fallback route can run",
            )
        assert store.get_run(active["run_id"])["state"] == "running"
        assert [item["run_id"] for item in store.list_runs_for_worker(preferred["worker_id"])] == [
            active["run_id"]
        ]

        control.release_provider_lease(
            lease_id=busy_lease["lease_id"],
            tenant_id="tenant-a",
            owner_id="user-a",
        )
        control.acquire_provider_lease(
            account_id=account["account_id"],
            tenant_id="tenant-a",
            owner_id="user-a",
            lane="codex-cli:mission",
            worker_id=preferred["worker_id"],
            run_id=active["run_id"],
            ttl_seconds=180,
        )
        # Steering this worker's own active personal mission is allowed because its
        # binder releases this exact run lease as interruption completes.
        service._ensure_dispatch_provider_ready(service.require_worker(preferred["worker_id"]))
    finally:
        service.shutdown()


def test_dispatch_gate_validates_effective_run_bundle_before_persisting_or_queueing(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GLASSHIVE_SECURITY_MODE", "multi_user")
    for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_BASE"):
        monkeypatch.delenv(name, raising=False)
    database = tmp_path / "runtime.db"
    store = Store(str(database))
    control = ControlPlaneStore(str(database))
    account = control.create_provider_account(
        tenant_id="tenant-a",
        owner_id="user-a",
        provider="codex",
        label="Synthetic personal account",
        auth_method="subscription",
        platform_support="supported",
        secret_locator="native-home://synthetic",
        status="ready",
    )
    service = WorkersProjectsService(
        store,
        PersistentFixtureRuntime(tmp_path / "managed-runtime"),
        control_plane_store=control,
        reconcile_on_startup=False,
    )
    service._ensure_worker_processor = lambda _worker_id: None  # type: ignore[method-assign]
    try:
        project = _project(store, title="Effective bundle project")
        legacy = service.create_worker(
            project_id=project["project_id"],
            tenant_id="tenant-a",
            owner_id="user-a",
            name="Legacy to personal",
            role="main",
            profile="codex-cli",
            backend="codex-cli",
            workspace_kind="named",
            bootstrap_bundle={"provider_account": {"policy": "legacy"}},
        )
        run = service.assign_run(
            legacy["worker_id"],
            "Use selected account",
            runtime_bundle={
                "provider_account": {
                    "policy": "personal_required",
                    "account_id": account["account_id"],
                }
            },
        )
        assert run["state"] == "queued"

        personal = service.create_worker(
            project_id=project["project_id"],
            tenant_id="tenant-a",
            owner_id="user-a",
            name="Personal to legacy",
            role="main",
            profile="codex-cli",
            backend="codex-cli",
            workspace_kind="named",
            bootstrap_bundle={
                "provider_account": {
                    "policy": "personal_required",
                    "account_id": account["account_id"],
                }
            },
        )
        before_bundle = personal["bootstrap_bundle_json"]
        with pytest.raises(ControlPlaneConflict, match="Work AI is not set up"):
            service.assign_run(
                personal["worker_id"],
                "Must use deployment route",
                runtime_bundle={"provider_account": None},
            )
        assert store.list_runs_for_worker(personal["worker_id"]) == []
        assert service.require_worker(personal["worker_id"])["bootstrap_bundle_json"] == before_bundle
    finally:
        service.shutdown()


def test_gc_corrupt_persisted_paths_are_metadata_only_and_never_delete_arbitrary_files(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_EPHEMERAL_RETENTION_S", "60")
    store = Store(str(tmp_path / "runtime.db"))
    runtime_root = tmp_path / "managed-runtime"
    service = WorkersProjectsService(store, PersistentFixtureRuntime(runtime_root), reconcile_on_startup=False)
    external_root = tmp_path / "unrelated-data"
    (external_root / "state" / "workspace").mkdir(parents=True)
    protected = external_root / "must-survive.txt"
    protected.write_text("not GlassHive storage", encoding="utf-8")
    try:
        project = _project(store, title="Corrupt row")
        worker = _worker(service, project, name="Corrupt row", workspace_kind="ephemeral")
        managed_file = runtime_root / worker["worker_id"] / "managed.txt"
        managed_file.write_text("left for manual recovery", encoding="utf-8")
        with store._connect() as conn:
            conn.execute(
                "UPDATE workers SET state_dir = ?, workspace_dir = ? WHERE worker_id = ?",
                (
                    str(external_root / "state"),
                    str(external_root / "state" / "workspace"),
                    worker["worker_id"],
                ),
            )
        _expire_worker(store, worker["worker_id"])
        reaped = service.reap_ephemeral_workspaces_once()
    finally:
        service.shutdown()

    assert [item["worker_id"] for item in reaped] == [worker["worker_id"]]
    assert protected.read_text(encoding="utf-8") == "not GlassHive storage"
    assert managed_file.read_text(encoding="utf-8") == "left for manual recovery"
    tombstone = store.get_workspace_gc_tombstone(worker["worker_id"])
    assert tombstone and tombstone["phase"] == "completed"
    assert tombstone["managed_storage_root"] == ""


def test_gc_rejects_symlinked_managed_worker_root_without_following_it(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_EPHEMERAL_RETENTION_S", "60")
    store = Store(str(tmp_path / "runtime.db"))
    runtime_root = tmp_path / "managed-runtime"
    service = WorkersProjectsService(store, PersistentFixtureRuntime(runtime_root), reconcile_on_startup=False)
    external_root = tmp_path / "external-target"
    (external_root / "state" / "workspace").mkdir(parents=True)
    protected = external_root / "must-survive.txt"
    protected.write_text("symlink target", encoding="utf-8")
    try:
        project = _project(store, title="Symlink row")
        worker = _worker(service, project, name="Symlink row", workspace_kind="ephemeral")
        worker_root = runtime_root / worker["worker_id"]
        worker_root.rename(tmp_path / "detached-managed-state")
        worker_root.symlink_to(external_root, target_is_directory=True)
        _expire_worker(store, worker["worker_id"])
        service.reap_ephemeral_workspaces_once()
    finally:
        service.shutdown()

    assert worker_root.is_symlink()
    assert protected.read_text(encoding="utf-8") == "symlink target"
    assert store.get_workspace_gc_tombstone(worker["worker_id"])["managed_storage_root"] == ""


def test_completed_history_does_not_retain_ephemeral_source_and_replay_survives(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_EPHEMERAL_RETENTION_S", "60")
    monkeypatch.setenv("GLASSHIVE_MAX_WORKSPACES_PER_USER", "1")
    database = tmp_path / "runtime.db"
    store = Store(str(database))
    control_plane = ControlPlaneStore(str(database))
    service = WorkersProjectsService(
        store,
        PersistentFixtureRuntime(tmp_path / "managed-runtime"),
        control_plane_store=control_plane,
        reconcile_on_startup=False,
    )
    try:
        project = _project(store, title="Replay source")
        worker = _worker(service, project, name="Replay source", workspace_kind="ephemeral")
        reservation = control_plane.reserve_workspace_duplication(
            tenant_id="tenant-a",
            owner_id="user-a",
            idempotency_key="dup-history",
            source_worker_id=worker["worker_id"],
            requested_name="Saved copy",
        )
        control_plane.complete_workspace_duplication(
            tenant_id="tenant-a",
            owner_id="user-a",
            idempotency_key="dup-history",
            project_id=str(reservation["project_id"]),
            worker_id="wrk_saved_copy",
            response={"workspace": {"worker_id": "wrk_saved_copy"}},
        )
        failed_reservation = control_plane.reserve_workspace_duplication(
            tenant_id="tenant-a",
            owner_id="user-a",
            idempotency_key="dup-failed-history",
            source_worker_id=worker["worker_id"],
            requested_name="Failed copy",
        )
        control_plane.fail_workspace_duplication(
            tenant_id="tenant-a",
            owner_id="user-a",
            idempotency_key="dup-failed-history",
            error_text="synthetic failure",
            project_id=str(failed_reservation["project_id"]),
        )
        with control_plane._connect() as conn:
            conn.execute(
                """
                INSERT INTO workspace_template_instantiations (
                    tenant_id, owner_id, idempotency_key, template_id, request_hash,
                    status, project_id, worker_id, created_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, 'completed', ?, ?, ?, ?)
                """,
                (
                    "tenant-a",
                    "user-a",
                    "template-history",
                    "tpl_history",
                    "synthetic-hash",
                    project["project_id"],
                    worker["worker_id"],
                    time.time(),
                    time.time(),
                ),
            )
            conn.execute(
                """
                INSERT INTO workspace_template_instantiations (
                    tenant_id, owner_id, idempotency_key, template_id, request_hash,
                    status, project_id, worker_id, created_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, 'failed', ?, ?, ?, NULL)
                """,
                (
                    "tenant-a",
                    "user-a",
                    "template-failed-history",
                    "tpl_failed_history",
                    "synthetic-failed-hash",
                    project["project_id"],
                    worker["worker_id"],
                    time.time(),
                ),
            )
        _expire_worker(store, worker["worker_id"])
        assert service.reap_ephemeral_workspaces_once()
        replay = control_plane.reserve_workspace_duplication(
            tenant_id="tenant-a",
            owner_id="user-a",
            idempotency_key="dup-history",
            source_worker_id=worker["worker_id"],
            requested_name="Saved copy",
        )
        failed_replay = control_plane.reserve_workspace_duplication(
            tenant_id="tenant-a",
            owner_id="user-a",
            idempotency_key="dup-failed-history",
            source_worker_id=worker["worker_id"],
            requested_name="Failed copy",
        )
        replacement_project = _project(store, title="Quota replacement")
        replacement = _worker(service, replacement_project, name="Replacement", workspace_kind="ephemeral")
    finally:
        service.shutdown()

    assert replay["idempotent_replay"] is True
    assert replay["response"]["workspace"]["worker_id"] == "wrk_saved_copy"
    assert failed_replay["idempotent_replay"] is True
    assert failed_replay["failed_replay"] is True
    assert store.get_worker(replacement["worker_id"]) is not None
    with control_plane._connect() as conn:
        assert conn.execute(
            "SELECT status FROM workspace_duplications WHERE idempotency_key = 'dup-history'"
        ).fetchone()["status"] == "completed"
        assert conn.execute(
            "SELECT status FROM workspace_template_instantiations WHERE idempotency_key = 'template-history'"
        ).fetchone()["status"] == "completed"
        assert conn.execute(
            "SELECT status FROM workspace_template_instantiations WHERE idempotency_key = 'template-failed-history'"
        ).fetchone()["status"] == "failed"


def test_expired_confirmation_does_not_block_ephemeral_gc(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_EPHEMERAL_RETENTION_S", "60")
    database = tmp_path / "runtime.db"
    store = Store(str(database))
    control_plane = ControlPlaneStore(str(database))
    service = WorkersProjectsService(
        store,
        PersistentFixtureRuntime(tmp_path / "managed-runtime"),
        control_plane_store=control_plane,
        reconcile_on_startup=False,
    )
    try:
        project = _project(store, title="Expired confirmation")
        worker = _worker(service, project, name="Expired confirmation", workspace_kind="ephemeral")
        with control_plane._connect() as conn:
            conn.execute(
                """
                INSERT INTO control_plane_pending_changes (
                    change_id, tenant_id, owner_id, change_type, target_id, payload_json,
                    confirmation_hash, status, expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?, '{}', ?, 'pending', ?, ?)
                """,
                (
                    "chg_expired",
                    "tenant-a",
                    "user-a",
                    "workspace_update",
                    worker["worker_id"],
                    "synthetic-hash",
                    time.time() - 10,
                    time.time() - 20,
                ),
            )
        _expire_worker(store, worker["worker_id"])
        reaped = service.reap_ephemeral_workspaces_once()
    finally:
        service.shutdown()

    assert [item["worker_id"] for item in reaped] == [worker["worker_id"]]
    assert store.get_worker(worker["worker_id"]) is None


def test_durable_gc_claim_blocks_resume_assign_and_keep(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_EPHEMERAL_RETENTION_S", "60")
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(
        store,
        PersistentFixtureRuntime(tmp_path / "managed-runtime"),
        reconcile_on_startup=False,
    )
    try:
        project = _project(store, title="Claim race")
        worker = _worker(service, project, name="Claim race", workspace_kind="ephemeral")
        old = _expire_worker(store, worker["worker_id"])
        refreshed = store.get_worker(worker["worker_id"])
        storage_root = service._managed_ephemeral_storage_root(refreshed)
        claimed = store.claim_ephemeral_workspace_gc(
            worker["worker_id"],
            updated_before=datetime.now(timezone.utc).isoformat(),
            now_epoch=time.time(),
            claim_token="gc_test_claim",
            claim_ttl_s=60,
            managed_storage_root=str(storage_root or ""),
        )
        assert claimed and claimed["updated_at"] == old
        with pytest.raises(RuntimeErrorBase, match="garbage-collected"):
            service.resume_worker(worker["worker_id"])
        with pytest.raises(RuntimeErrorBase, match="garbage-collected"):
            service.assign_run(worker["worker_id"], "Do not run")
        with pytest.raises(RuntimeErrorBase, match="garbage-collected"):
            service.update_worker_metadata(worker["worker_id"], workspace_kind="named")
    finally:
        store.release_ephemeral_workspace_gc_claim(worker["worker_id"], claim_token="gc_test_claim")
        service.shutdown()


def test_dual_reapers_share_one_durable_claim(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_EPHEMERAL_RETENTION_S", "60")

    class CountingRuntime(PersistentFixtureRuntime):
        def __init__(self, root: Path) -> None:
            super().__init__(root)
            self.lock = Lock()
            self.terminations = 0

        def terminate_worker(self, worker: dict) -> RuntimeInfo:
            with self.lock:
                self.terminations += 1
            time.sleep(0.05)
            return self._runtime_info(worker, pid=None)

    database = tmp_path / "runtime.db"
    runtime = CountingRuntime(tmp_path / "managed-runtime")
    store_a = Store(str(database))
    service_a = WorkersProjectsService(store_a, runtime, reconcile_on_startup=False)
    project = _project(store_a, title="Dual reaper")
    worker = _worker(service_a, project, name="Dual reaper", workspace_kind="ephemeral")
    _expire_worker(store_a, worker["worker_id"])
    store_b = Store(str(database))
    service_b = WorkersProjectsService(store_b, runtime, reconcile_on_startup=False)
    results: list[list[dict[str, object]]] = []
    errors: list[BaseException] = []

    def run_reaper(service: WorkersProjectsService) -> None:
        try:
            results.append(service.reap_ephemeral_workspaces_once())
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [Thread(target=run_reaper, args=(service,)) for service in (service_a, service_b)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    service_a.shutdown()
    service_b.shutdown()

    assert errors == []
    assert runtime.terminations == 1
    assert sum(len(result) for result in results) == 1
    assert store_a.get_workspace_gc_tombstone(worker["worker_id"])["phase"] == "completed"


def test_cleanup_failure_is_retryable_after_service_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_EPHEMERAL_RETENTION_S", "60")
    database = tmp_path / "runtime.db"
    runtime_root = tmp_path / "managed-runtime"
    runtime = PersistentFixtureRuntime(runtime_root)
    store = Store(str(database))
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    project = _project(store, title="Retry cleanup")
    worker = _worker(service, project, name="Retry cleanup", workspace_kind="ephemeral")
    _expire_worker(store, worker["worker_id"])
    real_rmtree = shutil.rmtree
    calls = 0

    def fail_once(path, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("synthetic cleanup failure")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr("workers_projects_runtime.service.shutil.rmtree", fail_once)
    first = service.reap_ephemeral_workspaces_once()
    service.shutdown()
    pending = store.get_workspace_gc_tombstone(worker["worker_id"])
    assert first and pending["phase"] == "cleanup_pending"
    assert pending["cleanup_attempts"] == 1
    assert (runtime_root / worker["worker_id"]).exists()

    restarted_store = Store(str(database))
    restarted = WorkersProjectsService(restarted_store, runtime, reconcile_on_startup=False)
    try:
        assert restarted.reap_ephemeral_workspaces_once() == []
    finally:
        restarted.shutdown()

    completed = restarted_store.get_workspace_gc_tombstone(worker["worker_id"])
    assert completed["phase"] == "completed"
    assert completed["cleanup_attempts"] == 2
    assert not (runtime_root / worker["worker_id"]).exists()


def test_expired_gc_claim_is_reconciled_after_service_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_EPHEMERAL_RETENTION_S", "60")
    database = tmp_path / "runtime.db"
    runtime_root = tmp_path / "managed-runtime"
    runtime = PersistentFixtureRuntime(runtime_root)
    store = Store(str(database))
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    project = _project(store, title="Claim recovery")
    worker = _worker(service, project, name="Claim recovery", workspace_kind="ephemeral")
    _expire_worker(store, worker["worker_id"])
    refreshed = store.get_worker(worker["worker_id"])
    storage_root = service._managed_ephemeral_storage_root(refreshed)
    stale_now = time.time() - 120
    assert store.claim_ephemeral_workspace_gc(
        worker["worker_id"],
        updated_before=datetime.now(timezone.utc).isoformat(),
        now_epoch=stale_now,
        claim_token="gc_crashed_process",
        claim_ttl_s=10,
        managed_storage_root=str(storage_root or ""),
    )
    service.shutdown()

    restarted_store = Store(str(database))
    restarted = WorkersProjectsService(restarted_store, runtime, reconcile_on_startup=False)
    try:
        reaped = restarted.reap_ephemeral_workspaces_once()
    finally:
        restarted.shutdown()

    assert [item["worker_id"] for item in reaped] == [worker["worker_id"]]
    assert restarted_store.get_worker(worker["worker_id"]) is None
    assert restarted_store.get_workspace_gc_tombstone(worker["worker_id"])["phase"] == "completed"


def test_ephemeral_gc_keeps_project_that_has_another_worker(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_EPHEMERAL_RETENTION_S", "60")
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(
        store,
        PersistentFixtureRuntime(tmp_path / "managed-runtime"),
        reconcile_on_startup=False,
    )
    try:
        project = _project(store, title="Shared project")
        ephemeral = _worker(service, project, name="One-off", workspace_kind="ephemeral")
        named = _worker(service, project, name="Saved", workspace_kind="named")
        _expire_worker(store, ephemeral["worker_id"])
        service.reap_ephemeral_workspaces_once()
    finally:
        service.shutdown()

    assert store.get_worker(ephemeral["worker_id"]) is None
    assert store.get_worker(named["worker_id"]) is not None
    assert store.get_project(project["project_id"]) is not None


def test_ephemeral_gc_revokes_signed_worker_links(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_EPHEMERAL_RETENTION_S", "60")
    monkeypatch.setenv("GLASSHIVE_SIGNED_LINK_SECRET", "synthetic-test-secret")
    monkeypatch.setenv("GLASSHIVE_LINK_REF_STATE_PATH", str(tmp_path / "link-refs.sqlite3"))
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(
        store,
        PersistentFixtureRuntime(tmp_path / "managed-runtime"),
        reconcile_on_startup=False,
    )
    try:
        project = _project(store, title="Signed link")
        worker = _worker(service, project, name="Signed link", workspace_kind="ephemeral")
        token = sign_link_token(
            kind="worker_view",
            worker_id=worker["worker_id"],
            tenant_id="tenant-a",
            owner_id="user-a",
        )
        ref_id = create_signed_link_ref(token=token, target_url=f"/watch/{worker['worker_id']}")
        assert resolve_signed_link_ref(ref_id) is not None
        _expire_worker(store, worker["worker_id"])
        service.reap_ephemeral_workspaces_once()
    finally:
        service.shutdown()

    assert resolve_signed_link_ref(ref_id) is None
