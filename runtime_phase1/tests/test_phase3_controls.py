from __future__ import annotations

import json
from datetime import datetime, timezone
from threading import Event, Thread

import pytest

from workers_projects_runtime.openclaw_runtime import RuntimeErrorBase, RuntimeInfo, StubRuntime
from workers_projects_runtime.profile_runtime import HostCodexCliRuntime
from workers_projects_runtime.service import WorkersProjectsService, callback_run_state
from workers_projects_runtime.store import (
    ActiveWorkActionConflictError,
    Store,
    WorkAdmissionError,
)


def _worker_with_run(
    tmp_path,
    runtime: StubRuntime,
    *,
    run_state: str = "running",
    worker_state: str | None = None,
    execution_mode: str = "docker",
) -> tuple[Store, WorkersProjectsService, dict, dict]:
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    project = store.create_project(
        "owner-a", "Control mission", "Exercise exact lifecycle controls", "openclaw-general"
    )
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner-a",
        name="Control worker",
        role="research",
        profile="openclaw-general",
        backend="openclaw",
        runtime="openclaw-stub",
        model="stub-model",
        execution_mode=execution_mode,
    )
    run = store.create_run(
        worker["worker_id"],
        project["project_id"],
        "Keep this exact mission under control",
        state=run_state,
    )
    if run_state in {"running", "settling", "paused"}:
        run = store.update_run(
            run["run_id"], started_at=datetime.now(timezone.utc).isoformat()
        ) or run
    store.update_worker_state(
        worker["worker_id"], worker_state or ("paused" if run_state == "paused" else "running")
    )
    return store, service, store.get_worker(worker["worker_id"]) or worker, store.get_run(run["run_id"]) or run


class _BlockingControlRuntime(StubRuntime):
    def __init__(self, control: str) -> None:
        super().__init__()
        self.control = control
        self.entered = Event()
        self.release = Event()

    def _block(self, worker: dict) -> RuntimeInfo:
        self.entered.set()
        assert self.release.wait(2)
        return RuntimeInfo(
            runtime="stub",
            model="stub-model",
            gateway_url="",
            gateway_port=None,
            gateway_token=None,
            session_key=None,
            state_dir=worker.get("state_dir"),
            workspace_dir=worker.get("workspace_dir"),
            pid=None,
        )

    def pause_worker(self, worker: dict) -> RuntimeInfo:
        if self.control == "pause":
            return self._block(worker)
        return super().pause_worker(worker)

    def interrupt_worker(self, worker: dict, run_id: str | None = None) -> RuntimeInfo:
        if self.control == "interrupt":
            return self._block(worker)
        return super().interrupt_worker(worker, run_id=run_id)


@pytest.mark.parametrize(
    ("control", "expected_kind"),
    [("pause", "pause_run"), ("interrupt", "interrupt_run")],
)
def test_destructive_exact_run_controls_hold_durable_claim_and_terminal_write_wins(
    tmp_path, control: str, expected_kind: str
):
    runtime = _BlockingControlRuntime(control)
    store, service, worker, run = _worker_with_run(tmp_path, runtime)
    failures: list[BaseException] = []

    def invoke() -> None:
        try:
            if control == "pause":
                service.pause_worker(worker["worker_id"], run_id=run["run_id"])
            else:
                service.interrupt_worker(worker["worker_id"], run_id=run["run_id"])
        except BaseException as exc:  # preserved for the assertion thread
            failures.append(exc)

    thread = Thread(target=invoke)
    thread.start()
    try:
        assert runtime.entered.wait(2)
        claimed = store.get_worker(worker["worker_id"]) or {}
        assert claimed["compute_release_kind"] == expected_kind
        assert claimed["compute_release_target_run_id"] == run["run_id"]
        assert claimed["compute_release_token"]
        assert store.finalize_run_if_state(
            run["run_id"], "running", "completed", output_text="COMPLETION WON"
        )
    finally:
        runtime.release.set()
        thread.join(timeout=2)
        service.shutdown()

    assert not thread.is_alive()
    assert failures == []
    assert (store.get_run(run["run_id"]) or {})["state"] == "completed"


class _ResumeStartupFailureRuntime(StubRuntime):
    def ensure_worker_ready(self, worker: dict) -> RuntimeInfo:
        raise RuntimeErrorBase("synthetic resume startup failure")


class _UncompensatedResumeFailureRuntime(_ResumeStartupFailureRuntime):
    requires_run_start_identity = True

    def pause_worker(self, worker: dict) -> RuntimeInfo:
        raise RuntimeErrorBase("synthetic compensating pause failure")


class _SteerControlFailureRuntime(StubRuntime):
    def interrupt_worker(self, worker: dict, run_id: str | None = None) -> RuntimeInfo:
        raise RuntimeErrorBase("synthetic steer control crash")


class _ResumeContainerGenerationRuntime(StubRuntime):
    requires_run_start_identity = True

    def __init__(self, before: str, after: str) -> None:
        super().__init__()
        self.container_id = before
        self.after = after

    def compute_identity(self, _worker: dict) -> dict[str, str]:
        return {"container_id": self.container_id}

    def ensure_worker_ready(self, worker: dict) -> RuntimeInfo:
        self.container_id = self.after
        return super().ensure_worker_ready(worker)


class _CountingControlRuntime(StubRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.pause_calls = 0
        self.resume_calls = 0
        self.interrupt_calls = 0

    def pause_worker(self, worker: dict) -> RuntimeInfo:
        self.pause_calls += 1
        return super().pause_worker(worker)

    def ensure_worker_ready(self, worker: dict) -> RuntimeInfo:
        self.resume_calls += 1
        return super().ensure_worker_ready(worker)

    def interrupt_worker(self, worker: dict, run_id: str | None = None) -> RuntimeInfo:
        self.interrupt_calls += 1
        return super().interrupt_worker(worker, run_id=run_id)


def test_resume_startup_failure_preserves_paused_truth_and_emits_no_resumed_event(tmp_path):
    store, service, worker, run = _worker_with_run(
        tmp_path,
        _ResumeStartupFailureRuntime(),
        run_state="paused",
        worker_state="paused",
    )
    try:
        with pytest.raises(RuntimeErrorBase, match="resume startup failure"):
            service.resume_worker(worker["worker_id"], run_id=run["run_id"])

        assert (store.get_worker(worker["worker_id"]) or {})["state"] == "paused"
        assert (store.get_run(run["run_id"]) or {})["state"] == "paused"
        assert not any(
            event["event_type"] in {"worker.resumed", "run.resumed"}
            for event in store.list_events(worker["worker_id"])
        )
    finally:
        service.shutdown()


def test_resume_partial_start_failure_keeps_claim_fenced_when_repause_unproven(tmp_path):
    store, service, worker, run = _worker_with_run(
        tmp_path,
        _UncompensatedResumeFailureRuntime(),
        run_state="paused",
        worker_state="paused",
    )
    try:
        with pytest.raises(RuntimeErrorBase, match="claim remains fenced"):
            service.resume_worker(worker["worker_id"], run_id=run["run_id"])
        durable_worker = store.get_worker(worker["worker_id"]) or {}
        assert durable_worker["state"] == "paused"
        assert durable_worker["compute_release_kind"] == "resume_run"
        assert durable_worker["compute_release_token"]
        assert (store.get_run(run["run_id"]) or {})["state"] == "paused"
        assert not any(
            event["event_type"] in {"worker.resumed", "run.resumed"}
            for event in store.list_events(worker["worker_id"])
        )
    finally:
        service.shutdown()


def test_reaped_paused_run_resumes_by_requeueing_same_run_generation(tmp_path):
    store, service, worker, run = _worker_with_run(
        tmp_path, StubRuntime(), run_state="paused", worker_state="paused"
    )
    service._ensure_worker_processor = lambda _worker_id: None  # type: ignore[method-assign]
    store.update_worker(
        worker["worker_id"],
        compute_released_at="2026-01-01T00:00:00+00:00",
        pid=None,
    )
    try:
        updated = service.resume_worker(worker["worker_id"], run_id=run["run_id"])
        durable = store.get_run(run["run_id"]) or {}
        assert durable["state"] == "queued"
        assert updated["state"] == "starting"
        assert updated["compute_released_at"] is None
        assert len(store.list_runs_for_worker(worker["worker_id"])) == 1
    finally:
        service.shutdown()


@pytest.mark.parametrize(
    ("before_container", "after_container"),
    [("", "container-new"), ("container-old", "container-new")],
)
def test_paused_run_with_missing_or_recreated_container_requeues_same_run(
    tmp_path, before_container: str, after_container: str
):
    runtime = _ResumeContainerGenerationRuntime(
        before_container, after_container
    )
    store, service, worker, run = _worker_with_run(
        tmp_path, runtime, run_state="paused", worker_state="paused"
    )
    processor_starts: list[str] = []
    service._ensure_worker_processor = processor_starts.append  # type: ignore[method-assign]
    try:
        updated = service.resume_worker(worker["worker_id"], run_id=run["run_id"])
        durable = store.get_run(run["run_id"]) or {}
        assert durable["state"] == "queued"
        assert updated["state"] == "starting"
        assert processor_starts == [worker["worker_id"]]
        assert [item["run_id"] for item in store.list_runs_for_worker(worker["worker_id"])] == [
            run["run_id"]
        ]
    finally:
        service.shutdown()


class _UnprovenHostControlRuntime(StubRuntime):
    requires_run_start_identity = True

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def pause_worker(self, worker: dict) -> RuntimeInfo:
        self.calls += 1
        return super().pause_worker(worker)

    def interrupt_worker(self, worker: dict, run_id: str | None = None) -> RuntimeInfo:
        self.calls += 1
        return super().interrupt_worker(worker, run_id=run_id)


@pytest.mark.parametrize("control", ["pause", "interrupt"])
def test_host_control_without_confirmed_exact_process_identity_fails_closed(tmp_path, control: str):
    runtime = _UnprovenHostControlRuntime()
    store, service, worker, run = _worker_with_run(
        tmp_path, runtime, execution_mode="host"
    )
    try:
        with pytest.raises(RuntimeErrorBase, match="identity.*not confirmed|confirmed.*identity"):
            if control == "pause":
                service.pause_worker(worker["worker_id"], run_id=run["run_id"])
            else:
                service.interrupt_worker(worker["worker_id"], run_id=run["run_id"])
        assert runtime.calls == 0
        assert (store.get_run(run["run_id"]) or {})["state"] == "running"
    finally:
        service.shutdown()


def test_public_queued_stop_sets_permanent_work_tombstone_and_cancels_full_cohort(tmp_path):
    store = Store(str(tmp_path / "runtime.db"))
    service = WorkersProjectsService(store, StubRuntime(), reconcile_on_startup=False)
    service.start_assigned_run = lambda _worker_id: None  # type: ignore[method-assign]
    try:
        delegation = service.reserve_delegation(
            tenant_id="tenant-a",
            owner_id="owner-a",
            idempotency_key="phase3-stop-delegation",
            request_digest="digest-a",
            origin_ref="origin-a",
            title="Stop queued mission",
            goal="Prove permanent Stop",
            instruction="First queued run",
            origin_surface="web",
            worker_name="Stop worker",
            worker_role="research",
            profile="openclaw-general",
            execution_mode="docker",
        )
        worker_id = str(delegation["worker_id"])
        first_run_id = str(delegation["initial_run_id"])
        sibling = service.assign_run(
            worker_id,
            "Queued sibling",
            start_processor=False,
            resume_paused_worker=False,
        )

        result = service.execute_active_work_action(
            store.get_delegation(
                str(delegation["work_ref"]), tenant_id="tenant-a", owner_id="owner-a"
            )
            or delegation,
            action="stop",
            idempotency_key="phase3-public-stop",
        )

        assert result["state"] == "cancelled"
        durable_worker = store.get_worker(worker_id) or {}
        assert durable_worker["work_stop_id"]
        assert durable_worker["work_stop_settled_at"]
        assert {str((store.get_run(run_id) or {})["state"]) for run_id in (first_run_id, sibling["run_id"])} == {
            "cancelled"
        }
        with pytest.raises(WorkAdmissionError) as blocked:
            service.assign_run(worker_id, "Must never reopen", start_processor=False)
        assert blocked.value.code == "work_stopped"
    finally:
        service.shutdown()


def test_assign_run_auto_resume_does_not_publish_resumed_truth_before_startup_succeeds(tmp_path):
    store, service, worker, paused_run = _worker_with_run(
        tmp_path,
        _ResumeStartupFailureRuntime(),
        run_state="paused",
        worker_state="paused",
    )
    service._ensure_worker_processor = lambda _worker_id: None  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeErrorBase, match="resume startup failure"):
            service.assign_run(worker["worker_id"], "New queued follow-up")

        assert (store.get_worker(worker["worker_id"]) or {})["state"] == "paused"
        assert (store.get_run(paused_run["run_id"]) or {})["state"] == "paused"
        assert len(store.list_runs_for_worker(worker["worker_id"])) == 1
        assert not any(
            event["event_type"] == "worker.resumed"
            for event in store.list_events(worker["worker_id"])
        )
    finally:
        service.shutdown()


def test_no_run_pause_targets_running_generation_before_newer_queued_sibling(tmp_path):
    runtime = _CountingControlRuntime()
    store, service, worker, active = _worker_with_run(tmp_path, runtime)
    queued = store.create_run(
        worker["worker_id"],
        worker["project_id"],
        "Queued follow-up must remain untouched",
        state="queued",
    )
    try:
        service.pause_worker(worker["worker_id"])
        assert runtime.pause_calls == 1
        assert (store.get_run(active["run_id"]) or {})["state"] == "paused"
        assert (store.get_run(queued["run_id"]) or {})["state"] == "queued"
    finally:
        service.shutdown()


def test_no_run_resume_targets_paused_generation_before_newer_queued_sibling(tmp_path):
    runtime = _CountingControlRuntime()
    store, service, worker, paused = _worker_with_run(
        tmp_path, runtime, run_state="paused", worker_state="paused"
    )
    queued = store.create_run(
        worker["worker_id"],
        worker["project_id"],
        "Queued follow-up must remain untouched",
        state="queued",
    )
    service._ensure_worker_processor = lambda _worker_id: None  # type: ignore[method-assign]
    try:
        service.resume_worker(worker["worker_id"])
        assert runtime.resume_calls == 1
        assert (store.get_run(paused["run_id"]) or {})["state"] == "running"
        assert (store.get_run(queued["run_id"]) or {})["state"] == "queued"
    finally:
        service.shutdown()


@pytest.mark.parametrize("action", ["pause", "steer", "stop"])
def test_active_work_action_reservation_binds_observed_exact_run_generation(
    tmp_path, action: str
):
    store = Store(str(tmp_path / "runtime.db"))
    work = store.reserve_delegation(
        tenant_id="tenant-a",
        owner_id="owner-a",
        idempotency_key=f"phase3-bind-{action}",
        request_digest="digest",
        origin_ref="origin",
        title="Bind action generation",
        goal="Never silently retarget a control",
        instruction="Initial run",
        origin_surface="web",
        worker_name="Binding worker",
        worker_role="research",
        profile="openclaw-general",
        backend="openclaw",
        runtime="stub",
        model="stub-model",
        execution_mode="docker",
    )
    observed_run_id = str(work["run_id"])
    observed_state = str(work["run_state"])
    observed_started_at = str(work.get("run_started_at") or "")
    store.create_run(
        str(work["worker_id"]),
        str(work["project_id"]),
        "Newer queued generation",
        state="queued",
    )
    with pytest.raises(ActiveWorkActionConflictError) as stale:
        store.reserve_active_work_action(
            tenant_id="tenant-a",
            owner_id="owner-a",
            work_ref=str(work["work_ref"]),
            idempotency_key=f"phase3-stale-{action}",
            action=action,
            payload_digest="payload",
            expected_current_run_id=observed_run_id,
            expected_source_run_id=observed_run_id,
            expected_source_state=observed_state,
            expected_source_started_at=observed_started_at,
        )
    assert stale.value.code == "active_work_generation_changed"


def test_active_work_action_stale_executor_cannot_checkpoint_finish_or_fail_after_takeover(
    tmp_path,
):
    store = Store(str(tmp_path / "runtime.db"))
    work = store.reserve_delegation(
        tenant_id="tenant-a",
        owner_id="owner-a",
        idempotency_key="phase3-action-owner",
        request_digest="digest",
        origin_ref="origin",
        title="Action owner CAS",
        goal="Fence stale action executors",
        instruction="Initial run",
        origin_surface="web",
        worker_name="Owner worker",
        worker_role="research",
        profile="openclaw-general",
        backend="openclaw",
        runtime="stub",
        model="stub-model",
        execution_mode="docker",
    )
    reserved = store.reserve_active_work_action(
        tenant_id="tenant-a",
        owner_id="owner-a",
        work_ref=str(work["work_ref"]),
        idempotency_key="phase3-owner-action",
        action="pause",
        payload_digest="payload",
        executor_id="executor-old",
        lease_seconds=1,
    )
    with store._connect() as conn:
        conn.execute(
            "UPDATE active_work_action_uses SET lease_expires_at = ? WHERE action_use_id = ?",
            ("2000-01-01T00:00:00+00:00", reserved["action_use_id"]),
        )
    takeover = store.reserve_active_work_action(
        tenant_id="tenant-a",
        owner_id="owner-a",
        work_ref=str(work["work_ref"]),
        idempotency_key="phase3-owner-action",
        action="pause",
        payload_digest="payload",
        executor_id="executor-new",
        lease_seconds=30,
    )
    assert takeover["recovery_takeover"] is True
    assert store.checkpoint_active_work_action(
        str(reserved["action_use_id"]),
        "stale",
        executor_id="executor-old",
    ) is None
    assert store.finish_active_work_action(
        str(reserved["action_use_id"]),
        response={"state": "paused"},
        executor_id="executor-old",
    ) is None
    assert store.fail_active_work_action(
        str(reserved["action_use_id"]),
        "stale failure",
        executor_id="executor-old",
    ) is False
    current = store.get_active_work_action(str(reserved["action_use_id"])) or {}
    assert current["executor_id"] == "executor-new"
    assert current["status"] == "pending"
    assert current["effect_phase"] == ""


def test_steer_holds_one_exact_claim_until_interruption_and_replacement_commit(tmp_path):
    runtime = _BlockingControlRuntime("interrupt")
    store, service, worker, run = _worker_with_run(tmp_path, runtime)
    service._ensure_worker_processor = lambda _worker_id: None  # type: ignore[method-assign]
    results: list[dict] = []
    failures: list[BaseException] = []

    def invoke() -> None:
        try:
            results.append(
                service.steer_worker(
                    worker["worker_id"],
                    "Use the replacement direction",
                    run_id=run["run_id"],
                    idempotency_key="phase3-steer-once",
                )
            )
        except BaseException as exc:
            failures.append(exc)

    thread = Thread(target=invoke)
    thread.start()
    try:
        assert runtime.entered.wait(2)
        claimed = store.get_worker(worker["worker_id"]) or {}
        assert claimed["compute_release_kind"] == "steer_run"
        assert claimed["compute_release_target_run_id"] == run["run_id"]
        replacement_run_id = str(claimed["compute_release_replacement_run_id"])
        assert replacement_run_id
        durable_replacement = store.get_run(replacement_run_id) or {}
        assert durable_replacement["state"] == "queued"
        assert store.claim_next_queued_run(worker["worker_id"]) is None
    finally:
        runtime.release.set()
        thread.join(timeout=2)
        service.shutdown()

    assert not thread.is_alive()
    assert failures == []
    durable_runs = store.list_runs_for_worker(worker["worker_id"])
    assert len(durable_runs) == 2
    assert (store.get_run(run["run_id"]) or {})["state"] == "interrupted"
    replacement = results[0]
    assert replacement["state"] == "queued"
    assert (store.get_run(replacement["run_id"]) or {})["state"] == "queued"
    delegation_state, terminal = service._delegation_callback_state(
        {"worker_id": worker["worker_id"], "current_run_id": replacement["run_id"]},
        callback_run=store.get_run(run["run_id"]),
    )
    assert (delegation_state, terminal) == ("queued", False)
    assert callback_run_state(
        "run.interrupted", store.get_run(run["run_id"])
    ) == "cancelled"


def test_steer_terminal_wins_without_creating_replacement(tmp_path):
    store, service, worker, run = _worker_with_run(tmp_path, StubRuntime())
    try:
        assert store.finalize_run_if_state(
            run["run_id"], "running", "completed", output_text="Completion won"
        )
        with pytest.raises(RuntimeErrorBase, match="No exact active run"):
            service.steer_worker(
                worker["worker_id"],
                "Too late to replace",
                run_id=run["run_id"],
                idempotency_key="phase3-steer-too-late",
            )
        assert len(store.list_runs_for_worker(worker["worker_id"])) == 1
        assert (store.get_run(run["run_id"]) or {})["state"] == "completed"
    finally:
        service.shutdown()


def test_inflight_steer_terminal_wins_and_does_not_report_queued_success(tmp_path):
    runtime = _BlockingControlRuntime("interrupt")
    store, service, worker, run = _worker_with_run(tmp_path, runtime)
    service._ensure_worker_processor = lambda _worker_id: None  # type: ignore[method-assign]
    results: list[dict] = []

    thread = Thread(
        target=lambda: results.append(
            service.steer_worker(
                worker["worker_id"],
                "Replacement must be suppressed",
                run_id=run["run_id"],
                idempotency_key="phase3-steer-completion-wins",
            )
        )
    )
    thread.start()
    try:
        assert runtime.entered.wait(2)
        assert store.finalize_run_if_state(
            run["run_id"], "running", "completed", output_text="COMPLETION WON"
        )
    finally:
        runtime.release.set()
        thread.join(timeout=2)
        service.shutdown()

    assert results[0]["_control_outcome"] == "terminal_won"
    assert results[0]["state"] == "completed"
    runs = store.list_runs_for_worker(worker["worker_id"])
    replacement = next(item for item in runs if item["run_id"] != run["run_id"])
    assert replacement["state"] == "cancelled"


def test_active_work_pause_terminal_winner_is_returned_instead_of_false_paused(tmp_path):
    runtime = _BlockingControlRuntime("pause")
    store, service, worker, run = _worker_with_run(tmp_path, runtime)
    delegation = {
        "work_ref": "work_terminal_pause",
        "tenant_id": "local",
        "owner_id": "owner-a",
        "worker_id": worker["worker_id"],
        "project_id": worker["project_id"],
        "current_run_id": run["run_id"],
        "run_id": run["run_id"],
    }
    store.update_worker(worker["worker_id"], last_run_id=run["run_id"])
    # Use the real public action selection via a synthetic delegation row.
    with store._connect() as conn:
        conn.execute(
            """
            INSERT INTO delegations (
                work_ref, tenant_id, owner_id, idempotency_key, request_digest,
                origin_ref, title, origin_surface, project_id, worker_id,
                initial_run_id, current_run_id, created_at, updated_at
            ) VALUES (?, 'local', 'owner-a', ?, ?, '', ?, 'web', ?, ?, ?, ?, ?, ?)
            """,
            (
                delegation["work_ref"],
                "phase3-terminal-pause",
                "digest",
                "Terminal pause",
                worker["project_id"],
                worker["worker_id"],
                run["run_id"],
                run["run_id"],
                datetime.now(timezone.utc).isoformat(),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
    result: list[dict] = []
    thread = Thread(
        target=lambda: result.append(
            service.execute_active_work_action(
                delegation,
                action="pause",
                idempotency_key="terminal-pause-action",
            )
        )
    )
    thread.start()
    try:
        assert runtime.entered.wait(2)
        assert store.finalize_run_if_state(run["run_id"], "running", "completed")
    finally:
        runtime.release.set()
        thread.join(timeout=2)
        service.shutdown()
    assert result[0]["state"] == "completed"
    assert result[0]["control_outcome"] == "terminal_won"


def test_expired_pause_claim_recovery_replays_exact_idempotent_control_and_finalizes(tmp_path):
    store, service, worker, run = _worker_with_run(tmp_path, StubRuntime())
    claim = store.try_claim_worker_compute_release(
        worker["worker_id"],
        expected_updated_at=str(worker["updated_at"]),
        expected_last_run_id=str(worker["last_run_id"]),
        expected_state=str(worker["state"]),
        expected_container_id="",
        owner="crashed-service",
        ttl_s=30,
        kind="pause_run",
        target_run_id=str(run["run_id"]),
        expected_target_started_at=str(run["started_at"]),
    )
    assert claim
    with store._connect() as conn:  # synthetic crash clock advance
        conn.execute(
            "UPDATE workers SET compute_release_expires_at = ? WHERE worker_id = ?",
            ("2000-01-01T00:00:00+00:00", worker["worker_id"]),
        )

    try:
        recovered = service.recover_expired_compute_release_claims_once()
        assert [item["kind"] for item in recovered] == ["pause_run"]
        assert (store.get_run(run["run_id"]) or {})["state"] == "paused"
        durable_worker = store.get_worker(worker["worker_id"]) or {}
        assert durable_worker["state"] == "paused"
        assert durable_worker["compute_release_token"] == ""
    finally:
        service.shutdown()


def test_expired_interrupt_claim_terminal_winner_clears_fence_without_runtime_action(tmp_path):
    runtime = _CountingControlRuntime()
    store, service, worker, run = _worker_with_run(tmp_path, runtime)
    claim = store.try_claim_worker_compute_release(
        worker["worker_id"],
        expected_updated_at=str(worker["updated_at"]),
        expected_last_run_id=str(worker["last_run_id"]),
        expected_state=str(worker["state"]),
        expected_container_id="",
        owner="crashed-service",
        ttl_s=30,
        kind="interrupt_run",
        target_run_id=str(run["run_id"]),
        expected_target_started_at=str(run["started_at"]),
    )
    assert claim
    assert store.finalize_run_if_state(
        run["run_id"], "running", "completed", output_text="COMPLETION WON"
    )
    with store._connect() as conn:
        conn.execute(
            "UPDATE workers SET compute_release_expires_at = ? WHERE worker_id = ?",
            ("2000-01-01T00:00:00+00:00", worker["worker_id"]),
        )
    try:
        recovered = service.recover_expired_compute_release_claims_once()
        assert recovered[0]["terminal_won"] is True
        assert runtime.interrupt_calls == 0
        assert (store.get_run(run["run_id"]) or {})["state"] == "completed"
        assert (store.get_worker(worker["worker_id"]) or {})[
            "compute_release_token"
        ] == ""
    finally:
        service.shutdown()


def test_control_callback_effect_replay_is_insert_once_and_preserves_delivered_row(
    tmp_path, monkeypatch
):
    runtime = _CountingControlRuntime()
    store, service, worker, run = _worker_with_run(tmp_path, runtime)
    store.update_worker(
        worker["worker_id"],
        bootstrap_bundle_json=json.dumps(
            {"callbacks": {"events_webhook_url": "http://callback.local/events"}}
        ),
    )
    original_replay = service._replay_pending_lifecycle_effects
    monkeypatch.setattr(service, "_replay_pending_lifecycle_effects", lambda: None)
    monkeypatch.setattr(service.executor, "submit", lambda *_args, **_kwargs: None)
    try:
        service.pause_worker(worker["worker_id"], run_id=run["run_id"])
        effects = store.list_lifecycle_operation_effects(worker_id=worker["worker_id"])
        assert len(effects) == 1
        assert effects[0]["status"] == "pending"

        monkeypatch.setattr(service, "_replay_pending_lifecycle_effects", original_replay)
        real_mark = store.mark_lifecycle_effect_applied

        def crash_after_materialize(effect_id: str, executor_id: str, *, lease_epoch: int):
            assert store.retry_lifecycle_effect(
                effect_id,
                executor_id,
                lease_epoch=lease_epoch,
                error_code="callback_enqueue_failed",
            )
            raise RuntimeError("synthetic crash after callback materialization")

        monkeypatch.setattr(store, "mark_lifecycle_effect_applied", crash_after_materialize)
        claim = store.claim_next_lifecycle_effect(
            service._executor_id,
            effect_kinds=("callback.run_paused",),
        )
        assert claim and claim["effect_id"] == effects[0]["effect_id"]
        with pytest.raises(RuntimeError, match="synthetic crash"):
            service._apply_lifecycle_effect(claim)

        with store._connect() as conn:
            callback = conn.execute(
                "SELECT * FROM callback_outbox WHERE worker_id = ?",
                (worker["worker_id"],),
            ).fetchone()
        assert callback is not None
        original_payload = str(callback["payload_json"])
        assert store.mark_callback_http_accepted(
            str(callback["callback_id"]), attempts=1, payload_json=original_payload
        )

        monkeypatch.setattr(store, "mark_lifecycle_effect_applied", real_mark)
        original_replay()
        with store._connect() as conn:
            callbacks = conn.execute(
                "SELECT * FROM callback_outbox WHERE worker_id = ?",
                (worker["worker_id"],),
            ).fetchall()
        assert len(callbacks) == 1
        assert callbacks[0]["status"] == "http_accepted"
        assert callbacks[0]["payload_json"] == original_payload
        assert store.list_lifecycle_operation_effects(worker_id=worker["worker_id"])[0][
            "status"
        ] == "applied"
    finally:
        service.shutdown()


def test_expired_steer_claim_recovers_only_its_durable_fenced_replacement(tmp_path):
    store, service, worker, run = _worker_with_run(
        tmp_path, _SteerControlFailureRuntime()
    )
    service._ensure_worker_processor = lambda _worker_id: None  # type: ignore[method-assign]
    with pytest.raises(RuntimeErrorBase, match="steer control crash"):
        service.steer_worker(
            worker["worker_id"],
            "Recover this exact replacement",
            run_id=run["run_id"],
            idempotency_key="phase3-steer-recovery",
        )
    claimed = store.get_worker(worker["worker_id"]) or {}
    replacement_run_id = str(claimed["compute_release_replacement_run_id"])
    assert replacement_run_id
    assert (store.get_run(replacement_run_id) or {})["state"] == "queued"
    with store._connect() as conn:
        conn.execute(
            "UPDATE workers SET compute_release_expires_at = ? WHERE worker_id = ?",
            ("2000-01-01T00:00:00+00:00", worker["worker_id"]),
        )
    service.runtime = StubRuntime()

    try:
        recovered = service.recover_expired_compute_release_claims_once()
        assert [item["kind"] for item in recovered] == ["steer_run"]
        assert (store.get_run(run["run_id"]) or {})["state"] == "interrupted"
        assert (store.get_run(replacement_run_id) or {})["state"] == "queued"
        assert (store.get_worker(worker["worker_id"]) or {})[
            "compute_release_token"
        ] == ""
    finally:
        service.shutdown()


def test_host_control_recovery_requires_matching_receipt_and_proven_exact_generation(
    tmp_path,
):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "host-runtime"))
    worker_id = "wrk_receipt_recovery"
    run_id = "run_receipt_recovery"
    session = {
        "session_name": "host-session-receipt",
        "run_id": run_id,
        "process_pid": 51001,
        "process_group": 51001,
        "process_start_identity": "ps-lstart:synthetic-receipt-generation",
    }
    lease = {
        "worker_id": worker_id,
        "run_id": run_id,
        "status": "active",
        "startup_state": "confirmed",
        "startup_identity_kind": "host_process",
        "pid": 51001,
        "process_group": 51001,
        "process_start_identity": "ps-lstart:synthetic-receipt-generation",
        "startup_session_id": "host-session-receipt",
    }
    worker = {
        "worker_id": worker_id,
        "project_id": "prj_receipt",
        "profile": "codex-cli",
        "model": "synthetic-model",
        "workspace_dir": str(tmp_path / "workspace"),
        "_active_run_id": run_id,
        "_host_run_lease": lease,
        "compute_release_kind": "pause_run",
    }
    runtime._write_host_control_receipt(
        worker,
        active_session=session,
        run_id=run_id,
        operation="pause_run",
        confirmed=False,
    )
    runtime._recorded_process_is_running = (  # type: ignore[method-assign]
        lambda _pid, _identity: False
    )
    runtime._write_stopped_active_run_evidence = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: None
    )

    info = runtime.pause_worker(worker)

    assert info.pid is None
    assert (runtime._read_host_control_receipt(worker_id) or {})["status"] == "confirmed"
    mismatched = {**worker, "_active_run_id": "run_other"}
    with pytest.raises(RuntimeErrorBase, match="identity is not confirmed"):
        runtime.pause_worker(mismatched)
