from __future__ import annotations

import time
from pathlib import Path
from threading import Event

from fastapi.testclient import TestClient

from workers_projects_runtime.api import create_app
from workers_projects_runtime.openclaw_runtime import RuntimeInfo, StubRuntime, WorkerInterruptedError, WorkerPausedError
from workers_projects_runtime.service import WorkersProjectsService
from workers_projects_runtime.store import Store


def wait_for_run(client: TestClient, run_id: str, timeout: float = 3.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = client.get(f"/v1/runs/{run_id}")
        assert response.status_code == 200
        run = response.json()
        if run["state"] in {"completed", "failed", "cancelled", "interrupted"}:
            return run
        time.sleep(0.05)
    raise AssertionError(f"Run {run_id} did not settle within {timeout}s")


def test_project_worker_lifecycle_with_stub_runtime(tmp_path):
    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=StubRuntime()))

    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert health.json()["runtime_backend"] == "stub"

    project_resp = client.post(
        "/v1/projects",
        json={
            "owner_id": "demo-owner",
            "title": "Project Alpha",
            "goal": "Validate the standalone OpenClaw worker control plane.",
            "default_worker_profile": "openclaw-general",
        },
    )
    assert project_resp.status_code == 201
    project = project_resp.json()
    project_id = project["project_id"]

    worker_resp = client.post(
        f"/v1/projects/{project_id}/workers",
        json={
            "owner_id": "demo-owner",
            "name": "Research Worker",
            "role": "research",
            "profile": "openclaw-general",
            "backend": "openclaw",
            "bootstrap_profile": "host-login",
            "bootstrap_bundle": {
                "system_instructions": "Follow the project goal and keep operator checkpoints explicit.",
            },
        },
    )
    assert worker_resp.status_code == 201
    worker = worker_resp.json()
    worker_id = worker["worker_id"]
    assert worker["state"] == "ready"
    assert worker["runtime"] == "openclaw-stub"
    assert worker["session_key"].endswith(worker_id)
    assert worker["bootstrap_profile"] == "host-login"

    assign_resp = client.post(
        f"/v1/workers/{worker_id}/assign",
        json={"instruction": "Research the best path for workers and projects."},
    )
    assert assign_resp.status_code == 202
    run = assign_resp.json()
    settled = wait_for_run(client, run["run_id"])
    assert settled["state"] == "completed"
    assert "STUB_OK" in settled["output_text"]

    pause_resp = client.post(f"/v1/workers/{worker_id}/pause")
    assert pause_resp.status_code == 202
    assert pause_resp.json()["state"] == "paused"

    resume_resp = client.post(f"/v1/workers/{worker_id}/resume")
    assert resume_resp.status_code == 202
    assert resume_resp.json()["state"] == "ready"

    message_resp = client.post(
        f"/v1/workers/{worker_id}/message",
        json={"message": "Shift focus to Codex and Claude worker design details."},
    )
    assert message_resp.status_code == 202
    message_run = wait_for_run(client, message_resp.json()["run_id"])
    assert message_run["state"] == "completed"
    assert "Operator message" in message_run["instruction"]

    events_resp = client.get(f"/v1/workers/{worker_id}/events")
    assert events_resp.status_code == 200
    assert len(events_resp.json()["items"]) >= 6

    terminate_resp = client.post(f"/v1/workers/{worker_id}/terminate")
    assert terminate_resp.status_code == 202
    assert terminate_resp.json()["state"] == "terminated"

    metrics_resp = client.get("/v1/metrics/summary")
    assert metrics_resp.status_code == 200
    metrics = metrics_resp.json()
    assert metrics["projects"] == 1
    assert metrics["workers"] == 1
    assert metrics["runs"] == 2
    assert metrics["queued_runs"] == 0
    assert metrics["events"] >= 7


def test_duplicate_worker_copies_workspace_into_new_worker(tmp_path):
    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=StubRuntime()))

    source_project = client.post(
        "/v1/projects",
        json={
            "owner_id": "demo-owner",
            "title": "Source Workspace",
            "goal": "Provide a reusable workspace to duplicate.",
            "default_worker_profile": "codex-cli",
        },
    ).json()
    source_worker = client.post(
        f"/v1/projects/{source_project['project_id']}/workers",
        json={
            "owner_id": "demo-owner",
            "name": "Main Workspace",
            "role": "main",
            "profile": "codex-cli",
            "backend": "openclaw",
            "bootstrap_profile": "host-login",
            "bootstrap_bundle": {
                "files": [
                    {
                        "scope": "workspace",
                        "path": "notes/from-bootstrap.txt",
                        "content": "seeded",
                    }
                ]
            },
        },
    ).json()

    source_workspace = Path(source_worker["workspace_dir"])
    source_workspace.mkdir(parents=True, exist_ok=True)
    (source_workspace / "app.txt").write_text("copied from source")
    (source_workspace / ".mcp.json").write_text('{"seed":"source"}')

    target_project = client.post(
        "/v1/projects",
        json={
            "owner_id": "demo-owner",
            "title": "Duplicate Workspace",
            "goal": "Create a duplicated workspace",
            "default_worker_profile": "codex-cli",
        },
    ).json()

    duplicate = client.post(
        f"/v1/projects/{target_project['project_id']}/workers/duplicate",
        json={
            "owner_id": "demo-owner",
            "source_worker_id": source_worker["worker_id"],
            "name": "Main Workspace",
            "role": "main",
        },
    )
    assert duplicate.status_code == 201
    duplicated_worker = duplicate.json()
    duplicated_workspace = Path(duplicated_worker["workspace_dir"])

    assert (duplicated_workspace / "app.txt").read_text() == "copied from source"
    assert (duplicated_workspace / ".mcp.json").read_text() == '{"seed":"source"}'

    events = client.get(f"/v1/workers/{duplicated_worker['worker_id']}/events")
    assert events.status_code == 200
    assert any(item["event_type"] == "worker.duplicated" for item in events.json()["items"])


def test_duplicate_worker_does_not_copy_home_directory(tmp_path):
    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=StubRuntime()))

    source_project = client.post(
        "/v1/projects",
        json={
            "owner_id": "demo-owner",
            "title": "Source Workspace",
            "goal": "Provide a reusable workspace to duplicate.",
            "default_worker_profile": "codex-cli",
        },
    ).json()
    source_worker = client.post(
        f"/v1/projects/{source_project['project_id']}/workers",
        json={
            "owner_id": "demo-owner",
            "name": "Main Workspace",
            "role": "main",
            "profile": "codex-cli",
            "backend": "openclaw",
        },
    ).json()

    source_workspace = Path(source_worker["workspace_dir"])
    source_home = source_workspace.parent / "home"
    source_home.mkdir(parents=True, exist_ok=True)
    source_workspace.mkdir(parents=True, exist_ok=True)
    (source_home / ".qa-home-marker").write_text("home-only")
    (source_workspace / "qa_workspace_marker.txt").write_text("workspace-only")

    target_project = client.post(
        "/v1/projects",
        json={
            "owner_id": "demo-owner",
            "title": "Duplicate Workspace",
            "goal": "Create a duplicated workspace",
            "default_worker_profile": "codex-cli",
        },
    ).json()

    duplicate = client.post(
        f"/v1/projects/{target_project['project_id']}/workers/duplicate",
        json={
            "owner_id": "demo-owner",
            "source_worker_id": source_worker["worker_id"],
            "name": "Main Workspace",
            "role": "main",
        },
    )
    assert duplicate.status_code == 201
    duplicated_worker = duplicate.json()
    duplicated_workspace = Path(duplicated_worker["workspace_dir"])
    duplicated_home = duplicated_workspace.parent / "home"

    assert (duplicated_workspace / "qa_workspace_marker.txt").read_text() == "workspace-only"
    assert not (duplicated_home / ".qa-home-marker").exists()


def test_assign_run_on_paused_worker_resumes_before_queueing(tmp_path):
    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=StubRuntime()))

    project = client.post(
        "/v1/projects",
        json={
            "owner_id": "demo-owner",
            "title": "Resume Workspace",
            "goal": "Resume the paused workspace before queueing a new run.",
            "default_worker_profile": "codex-cli",
        },
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={
            "owner_id": "demo-owner",
            "name": "Main Workspace",
            "role": "main",
            "profile": "codex-cli",
            "backend": "openclaw",
        },
    ).json()

    pause_resp = client.post(f"/v1/workers/{worker['worker_id']}/pause")
    assert pause_resp.status_code == 202
    assert pause_resp.json()["state"] == "paused"

    assign_resp = client.post(
        f"/v1/workers/{worker['worker_id']}/assign",
        json={"instruction": "Resume this paused workspace and start a fresh run."},
    )
    assert assign_resp.status_code == 202

    events = client.get(f"/v1/workers/{worker['worker_id']}/events")
    assert events.status_code == 200
    event_types = [item["event_type"] for item in events.json()["items"]]
    assert "worker.paused" in event_types
    assert "worker.resumed" in event_types
    assert "run.queued" in event_types


def test_openclaw_worker_exposes_operator_control_surface(tmp_path):
    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=StubRuntime()))

    project = client.post(
        "/v1/projects",
        json={"owner_id": "demo-owner", "title": "Control Surface", "goal": "View and control worker progress."},
    ).json()

    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={
            "owner_id": "demo-owner",
            "name": "Claude Worker",
            "role": "coder",
            "profile": "openclaw-claude",
            "backend": "openclaw",
        },
    ).json()

    takeover = client.get(f"/v1/workers/{worker['worker_id']}/takeover")
    assert takeover.status_code == 200
    data = takeover.json()
    assert data["supported"] is True
    assert data["mode"] == "web-terminal"
    assert data["url"].endswith(f"/ui/workers/{worker['worker_id']}/terminal")

    worker_ui = client.get(f"/ui/workers/{worker['worker_id']}")
    assert worker_ui.status_code == 200
    assert "Claude Worker" in worker_ui.text
    assert worker["session_key"] in worker_ui.text
    assert "Queue task" in worker_ui.text
    assert "Take over terminal" in worker_ui.text

    terminal_ui = client.get(f"/ui/workers/{worker['worker_id']}/terminal")
    assert terminal_ui.status_code == 200
    assert "Connecting to worker terminal" in terminal_ui.text
    assert f"const workerId = '{worker['worker_id']}'" in terminal_ui.text
    assert 'target="_top">Back to project workspace</a>' in terminal_ui.text
    assert 'target="_top">Worker console</a>' in terminal_ui.text


def test_project_workspace_ui_supports_simple_run_flow(tmp_path):
    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=StubRuntime()))

    project = client.post(
        "/v1/projects",
        json={
            "owner_id": "demo-owner",
            "title": "Simple Flow",
            "goal": "Keep the operator path easy: prompt, worker, run, control.",
            "default_worker_profile": "openclaw-general",
        },
    ).json()

    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={
            "owner_id": "demo-owner",
            "name": "Primary Worker",
            "role": "research",
            "profile": "openclaw-general",
            "backend": "openclaw",
        },
    ).json()

    home = client.get("/ui")
    assert home.status_code == 200
    assert "Open project workspace" in home.text

    project_ui = client.get(f"/ui/projects/{project['project_id']}")
    assert project_ui.status_code == 200
    assert "Run Project" in project_ui.text
    assert "Create worker only" in project_ui.text
    assert "Selected Worker" in project_ui.text
    assert worker["worker_id"] in project_ui.text
    assert "Take over terminal" in project_ui.text


def test_live_payload_promotes_workspace_html_as_deliverable(tmp_path):
    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=StubRuntime()))

    project = client.post(
        "/v1/projects",
        json={
            "owner_id": "demo-owner",
            "title": "Deliverable Detection",
            "goal": "Expose a presentable page result to the operator UI.",
            "default_worker_profile": "codex-cli",
        },
    ).json()

    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={
            "owner_id": "demo-owner",
            "name": "Page Worker",
            "role": "builder",
            "profile": "codex-cli",
            "backend": "openclaw",
        },
    ).json()

    workspace = Path(worker["workspace_dir"])
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "index.html").write_text("<!doctype html><h1>HELLO WORLD</h1>")

    live = client.get(f"/v1/workers/{worker['worker_id']}/live")
    assert live.status_code == 200
    payload = live.json()
    assert payload["deliverable"]["kind"] == "webpage"
    assert payload["deliverable"]["source"] == "workspace_html"
    assert payload["deliverable"]["browser_url"] == "file:///workspace/project/index.html"
    assert payload["deliverable"]["preferred_surface"] == "desktop"


class ControllableRuntime:
    def __init__(self) -> None:
        self.running = Event()
        self.release = Event()
        self.interrupted = Event()
        self.paused = Event()
        self.interrupt_run_ids: list[str | None] = []

    def resolve_model(self, profile: str) -> str:
        return "controllable/test"

    def _info(self, worker: dict, pid: int | None = 1234) -> RuntimeInfo:
        return RuntimeInfo(
            runtime="controllable",
            model=worker.get("model") or self.resolve_model(worker.get("profile", "")),
            gateway_url="",
            gateway_port=None,
            gateway_token=None,
            session_key=worker.get("session_key") or f"controllable:{worker['worker_id']}",
            state_dir="/tmp/controllable/state",
            workspace_dir="/tmp/controllable/workspace",
            pid=pid,
        )

    def ensure_worker_ready(self, worker: dict) -> RuntimeInfo:
        self.paused.clear()
        return self._info(worker)

    def pause_worker(self, worker: dict) -> RuntimeInfo:
        self.paused.set()
        return self._info(worker, pid=None)

    def interrupt_worker(self, worker: dict, run_id: str | None = None) -> RuntimeInfo:
        self.interrupt_run_ids.append(run_id)
        self.interrupted.set()
        return self._info(worker)

    def terminate_worker(self, worker: dict) -> RuntimeInfo:
        self.interrupted.set()
        return self._info(worker, pid=None)

    def reconcile_worker(self, worker: dict) -> RuntimeInfo:
        return self._info(worker, pid=None if self.paused.is_set() else 1234)

    def run_task(self, worker: dict, instruction: str, timeout_sec: int = 300) -> str:
        self.running.set()
        deadline = time.time() + 5
        while time.time() < deadline:
            if self.interrupted.is_set():
                raise WorkerInterruptedError("Worker run was interrupted by the operator")
            if self.paused.is_set():
                time.sleep(0.05)
                continue
            if self.release.is_set():
                return "CONTROLLABLE_OK"
            time.sleep(0.05)
        raise AssertionError("ControllableRuntime timed out in test")


class SteerableControllableRuntime(ControllableRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.instructions: list[str] = []

    def ensure_worker_ready(self, worker: dict) -> RuntimeInfo:
        info = super().ensure_worker_ready(worker)
        if self.instructions:
            self.interrupted.clear()
        return info

    def run_task(self, worker: dict, instruction: str, timeout_sec: int = 300) -> str:
        self.instructions.append(instruction)
        if instruction.startswith("Operator steer instruction"):
            return "STEER_REDIRECT_OK"
        return super().run_task(worker, instruction, timeout_sec=timeout_sec)


def test_pause_resume_freezes_active_run_without_losing_it(tmp_path):
    db_path = tmp_path / "runtime.db"
    runtime = ControllableRuntime()
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=runtime))

    project = client.post(
        "/v1/projects",
        json={"owner_id": "demo-owner", "title": "Pause Resume", "goal": "Freeze and resume an active worker run."},
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={"owner_id": "demo-owner", "name": "Controllable Worker", "role": "coder"},
    ).json()

    run = client.post(
        f"/v1/workers/{worker['worker_id']}/assign",
        json={"instruction": "Do a long running task."},
    ).json()
    assert runtime.running.wait(timeout=2), "worker run never started"

    paused = client.post(f"/v1/workers/{worker['worker_id']}/pause")
    assert paused.status_code == 202
    assert paused.json()["state"] == "paused"

    run_during_pause = client.get(f"/v1/runs/{run['run_id']}").json()
    assert run_during_pause["state"] == "running"

    resumed = client.post(f"/v1/workers/{worker['worker_id']}/resume")
    assert resumed.status_code == 202
    assert resumed.json()["state"] == "running"

    runtime.release.set()
    settled = wait_for_run(client, run["run_id"], timeout=3.0)
    assert settled["state"] == "completed"
    assert settled["output_text"] == "CONTROLLABLE_OK"


class RaisingPauseRuntime:
    def __init__(self) -> None:
        self.running = Event()
        self.paused = Event()

    def resolve_model(self, profile: str) -> str:
        return "pause-raising/test"

    def _info(self, worker: dict, pid: int | None = 2222) -> RuntimeInfo:
        return RuntimeInfo(
            runtime="pause-raising",
            model=worker.get("model") or self.resolve_model(worker.get("profile", "")),
            gateway_url="",
            gateway_port=None,
            gateway_token=None,
            session_key=worker.get("session_key") or f"pause-raising:{worker['worker_id']}",
            state_dir="/tmp/pause-raising/state",
            workspace_dir="/tmp/pause-raising/workspace",
            pid=pid,
        )

    def ensure_worker_ready(self, worker: dict) -> RuntimeInfo:
        self.paused.clear()
        return self._info(worker)

    def pause_worker(self, worker: dict) -> RuntimeInfo:
        self.paused.set()
        return self._info(worker, pid=None)

    def interrupt_worker(self, worker: dict, run_id: str | None = None) -> RuntimeInfo:
        return self._info(worker)

    def terminate_worker(self, worker: dict) -> RuntimeInfo:
        return self._info(worker, pid=None)

    def reconcile_worker(self, worker: dict) -> RuntimeInfo:
        return self._info(worker, pid=None if self.paused.is_set() else 2222)

    def run_task(self, worker: dict, instruction: str, timeout_sec: int = 300) -> str:
        self.running.set()
        deadline = time.time() + 5
        while time.time() < deadline:
            if self.paused.is_set():
                raise WorkerPausedError("Worker was paused while a run was active")
            time.sleep(0.05)
        raise AssertionError("RaisingPauseRuntime timed out in test")


def test_worker_paused_error_finalizes_run_as_paused(tmp_path):
    db_path = tmp_path / "runtime.db"
    runtime = RaisingPauseRuntime()
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=runtime))

    project = client.post(
        "/v1/projects",
        json={"owner_id": "demo-owner", "title": "Pause Finalize", "goal": "Finalize paused runs cleanly."},
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={"owner_id": "demo-owner", "name": "Pause Finalize Worker", "role": "coder"},
    ).json()

    run = client.post(
        f"/v1/workers/{worker['worker_id']}/assign",
        json={"instruction": "Begin a run that will raise WorkerPausedError when paused."},
    ).json()
    assert runtime.running.wait(timeout=2), "worker run never started"

    paused = client.post(f"/v1/workers/{worker['worker_id']}/pause")
    assert paused.status_code == 202
    assert paused.json()["state"] == "paused"

    deadline = time.time() + 3.0
    settled = None
    while time.time() < deadline:
        response = client.get(f"/v1/runs/{run['run_id']}")
        assert response.status_code == 200
        candidate = response.json()
        if candidate["state"] == "paused":
            settled = candidate
            break
        time.sleep(0.05)

    assert settled is not None, "run did not settle into paused state"
    assert settled["state"] == "paused"
    assert "paused while a run was active" in settled["error_text"]


def test_interrupt_stops_active_run_and_keeps_worker_ready(tmp_path):
    db_path = tmp_path / "runtime.db"
    runtime = ControllableRuntime()
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=runtime))

    project = client.post(
        "/v1/projects",
        json={"owner_id": "demo-owner", "title": "Interrupt", "goal": "Stop the current worker task cleanly."},
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={"owner_id": "demo-owner", "name": "Interrupt Worker", "role": "coder"},
    ).json()

    run = client.post(
        f"/v1/workers/{worker['worker_id']}/assign",
        json={"instruction": "Long task to be interrupted."},
    ).json()
    assert runtime.running.wait(timeout=2), "worker run never started"

    interrupted = client.post(f"/v1/workers/{worker['worker_id']}/interrupt")
    assert interrupted.status_code == 202
    assert interrupted.json()["state"] == "ready"
    assert runtime.interrupt_run_ids == [run["run_id"]]

    settled = wait_for_run(client, run["run_id"], timeout=3.0)
    assert settled["state"] == "interrupted"

    worker_after = client.get(f"/v1/workers/{worker['worker_id']}").json()
    assert worker_after["state"] == "ready"


def test_steer_interrupts_active_run_and_redirects_to_new_instruction(tmp_path):
    db_path = tmp_path / "runtime.db"
    runtime = SteerableControllableRuntime()
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=runtime))

    project = client.post(
        "/v1/projects",
        json={"owner_id": "demo-owner", "title": "Steer Redirect", "goal": "Redirect an active run immediately."},
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={"owner_id": "demo-owner", "name": "Steer Worker", "role": "coder"},
    ).json()

    first_run = client.post(
        f"/v1/workers/{worker['worker_id']}/assign",
        json={"instruction": "Do the original long-running task."},
    ).json()
    assert runtime.running.wait(timeout=2), "worker run never started"

    steer_resp = client.post(
        f"/v1/workers/{worker['worker_id']}/steer",
        json={"message": "Switch immediately to the new operator direction."},
    )
    assert steer_resp.status_code == 202
    steer_run = steer_resp.json()
    assert steer_run["state"] == "queued"
    assert runtime.interrupt_run_ids == [first_run["run_id"]]

    interrupted = wait_for_run(client, first_run["run_id"], timeout=3.0)
    assert interrupted["state"] == "interrupted"
    redirected = wait_for_run(client, steer_run["run_id"], timeout=3.0)
    assert redirected["state"] == "completed"
    assert redirected["output_text"] == "STEER_REDIRECT_OK"

    events = client.get(f"/v1/workers/{worker['worker_id']}/events").json()["items"]
    event_types = [event["event_type"] for event in events]
    assert "worker.interrupted" in event_types
    assert "worker.steer" in event_types
    assert runtime.instructions[1].startswith("Operator steer instruction")
    assert "Do not stop at an acknowledgement" in runtime.instructions[1]


class HealRecoveryRuntime:
    def __init__(self) -> None:
        self.collect_run_ids: list[str | None] = []

    def resolve_model(self, profile: str) -> str:
        return "heal-recovery/test"

    def _info(self, worker: dict, pid: int | None = 4242) -> RuntimeInfo:
        return RuntimeInfo(
            runtime="heal-recovery",
            model=worker.get("model") or self.resolve_model(worker.get("profile", "")),
            gateway_url="",
            gateway_port=None,
            gateway_token=None,
            session_key=worker.get("session_key") or f"heal-recovery:{worker['worker_id']}",
            state_dir="/tmp/heal-recovery/state",
            workspace_dir="/tmp/heal-recovery/workspace",
            pid=pid,
        )

    def ensure_worker_ready(self, worker: dict) -> RuntimeInfo:
        return self._info(worker)

    def pause_worker(self, worker: dict) -> RuntimeInfo:
        return self._info(worker, pid=None)

    def interrupt_worker(self, worker: dict, run_id: str | None = None) -> RuntimeInfo:
        return self._info(worker)

    def terminate_worker(self, worker: dict) -> RuntimeInfo:
        return self._info(worker, pid=None)

    def reconcile_worker(self, worker: dict) -> RuntimeInfo:
        return self._info(worker)

    def collect_completed_run(self, worker: dict, run_id: str | None = None) -> dict[str, str] | None:
        self.collect_run_ids.append(run_id)
        return {
            "state": "completed",
            "output_text": "HEAL_COMPLETED_OK",
            "error_text": "",
        }


def test_heal_worker_restarts_processor_when_queued_runs_remain(tmp_path):
    db_path = tmp_path / "runtime.db"
    store = Store(str(db_path))
    runtime = HealRecoveryRuntime()
    service = WorkersProjectsService(store, runtime)

    project = service.create_project("demo-owner", "Heal Queue", "Recover completion and continue queued runs.", "codex-cli")
    worker = service.create_worker(
        project_id=project["project_id"],
        owner_id="demo-owner",
        name="Heal Queue Worker",
        role="coder",
        profile="codex-cli",
        backend="openclaw",
    )

    running = store.create_run(worker["worker_id"], project["project_id"], "original run", state="running")
    queued = store.create_run(worker["worker_id"], project["project_id"], "queued follow-up", state="queued")
    store.update_worker(worker["worker_id"], state="running")

    restart_requests: list[str] = []
    service._ensure_worker_processor = lambda worker_id: restart_requests.append(worker_id)  # type: ignore[method-assign]
    service._active_processors.add(worker["worker_id"])

    healed = service.heal_worker(worker["worker_id"])

    assert healed is not None
    assert store.get_run(running["run_id"])["state"] == "completed"
    assert store.get_run(queued["run_id"])["state"] == "queued"
    assert restart_requests == [worker["worker_id"]]
    assert runtime.collect_run_ids == [running["run_id"]]


class HealingRaceRuntime:
    def __init__(self) -> None:
        self.initial_started = Event()
        self.release_initial = Event()
        self.queued_started = Event()
        self.release_queued = Event()
        self.collect_run_ids: list[str | None] = []

    def resolve_model(self, profile: str) -> str:
        return "healing-race/test"

    def _info(self, worker: dict, pid: int | None = 4242) -> RuntimeInfo:
        return RuntimeInfo(
            runtime="healing-race",
            model=worker.get("model") or self.resolve_model(worker.get("profile", "")),
            gateway_url="",
            gateway_port=None,
            gateway_token=None,
            session_key=f"healing-race:{worker['worker_id']}",
            state_dir=f"/tmp/{worker['worker_id']}/state",
            workspace_dir=f"/tmp/{worker['worker_id']}/workspace",
            pid=pid,
        )

    def ensure_worker_ready(self, worker: dict) -> RuntimeInfo:
        return self._info(worker)

    def pause_worker(self, worker: dict) -> RuntimeInfo:
        return self._info(worker, pid=None)

    def interrupt_worker(self, worker: dict, run_id: str | None = None) -> RuntimeInfo:
        return self._info(worker)

    def terminate_worker(self, worker: dict) -> RuntimeInfo:
        return self._info(worker, pid=None)

    def reconcile_worker(self, worker: dict) -> RuntimeInfo:
        return self._info(worker)

    def collect_completed_run(self, worker: dict, run_id: str | None = None) -> dict[str, str] | None:
        self.collect_run_ids.append(run_id)
        return {
            "state": "completed",
            "output_text": "HEAL_RECOVERED_INITIAL",
            "error_text": "",
        }

    def run_task(self, worker: dict, instruction: str, timeout_sec: int = 300, run_id: str | None = None) -> str:
        if "initial" in instruction:
            self.initial_started.set()
            assert self.release_initial.wait(timeout=3)
            return "INITIAL_RETURNED_LATE"
        self.queued_started.set()
        assert self.release_queued.wait(timeout=3)
        return "QUEUED_COMPLETED_OK"


def test_heal_worker_replacement_processor_keeps_running_state_while_follow_up_executes(tmp_path):
    db_path = tmp_path / "runtime.db"
    store = Store(str(db_path))
    runtime = HealingRaceRuntime()
    service = WorkersProjectsService(store, runtime)

    project = service.create_project("demo-owner", "Queue Race", "Ensure healed processors cannot overwrite a replacement run.", "codex-cli")
    worker = service.create_worker(
        project_id=project["project_id"],
        owner_id="demo-owner",
        name="Queue Race Worker",
        role="coder",
        profile="codex-cli",
        backend="openclaw",
    )

    initial = service.assign_run(worker["worker_id"], "initial run that will be healed")
    assert runtime.initial_started.wait(timeout=2)

    queued = service.assign_run(worker["worker_id"], "queued follow-up that must keep running")
    healed = service.heal_worker(worker["worker_id"])
    assert healed is not None
    assert runtime.collect_run_ids == [initial["run_id"]]
    assert runtime.queued_started.wait(timeout=2)

    runtime.release_initial.set()

    deadline = time.time() + 2
    while time.time() < deadline:
        refreshed_worker = store.get_worker(worker["worker_id"])
        active_run = store.get_active_run(worker["worker_id"])
        if refreshed_worker and refreshed_worker["state"] == "running" and active_run and active_run["run_id"] == queued["run_id"]:
            break
        time.sleep(0.05)
    else:
        raise AssertionError("Replacement processor did not keep the queued follow-up marked as running")

    runtime.release_queued.set()

    deadline = time.time() + 2
    while time.time() < deadline:
        queued_run = store.get_run(queued["run_id"])
        refreshed_worker = store.get_worker(worker["worker_id"])
        if queued_run and queued_run["state"] == "completed" and refreshed_worker and refreshed_worker["state"] == "ready":
            break
        time.sleep(0.05)
    else:
        raise AssertionError("Queued follow-up did not complete cleanly")

    assert store.get_run(initial["run_id"])["state"] == "completed"
    assert store.get_run(queued["run_id"])["output_text"] == "QUEUED_COMPLETED_OK"
    assert store.get_worker(worker["worker_id"])["last_run_id"] == queued["run_id"]


class DesktopStubRuntime:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.last_desktop_action: dict[str, object] | None = None

    def resolve_model(self, profile: str) -> str:
        return "desktop-stub/test"

    def _worker_paths(self, worker_id: str) -> tuple[Path, Path]:
        state_dir = self.root / worker_id / "state"
        workspace_dir = state_dir / "workspace"
        workspace_dir.mkdir(parents=True, exist_ok=True)
        return state_dir, workspace_dir

    def _info(self, worker: dict, pid: int | None = 4242) -> RuntimeInfo:
        state_dir, workspace_dir = self._worker_paths(worker["worker_id"])
        return RuntimeInfo(
            runtime="desktop-stub",
            model=worker.get("model") or self.resolve_model(worker.get("profile", "")),
            gateway_url="",
            gateway_port=None,
            gateway_token=None,
            session_key=worker.get("session_key") or f"desktop:{worker['worker_id']}",
            state_dir=str(state_dir),
            workspace_dir=str(workspace_dir),
            pid=pid,
        )

    def ensure_worker_ready(self, worker: dict) -> RuntimeInfo:
        return self._info(worker)

    def pause_worker(self, worker: dict) -> RuntimeInfo:
        return self._info(worker, pid=None)

    def interrupt_worker(self, worker: dict, run_id: str | None = None) -> RuntimeInfo:
        return self._info(worker)

    def terminate_worker(self, worker: dict) -> RuntimeInfo:
        return self._info(worker, pid=None)

    def run_task(self, worker: dict, instruction: str, timeout_sec: int = 300) -> str:
        return f"DESKTOP_OK: {instruction}"

    def reconcile_worker(self, worker: dict) -> RuntimeInfo:
        return self._info(worker)

    def describe_worker(self, worker: dict) -> dict[str, object]:
        _, workspace_dir = self._worker_paths(worker["worker_id"])
        return {
            "mode": "workstation-desktop",
            "runtime": "desktop-stub",
            "workspace_dir": str(workspace_dir),
            "state_dir": str(workspace_dir.parent),
            "container_name": f"wpr-{worker['worker_id']}",
            "view_url": "http://127.0.0.1:57906/?autoconnect=1",
        }

    def desktop_action(
        self,
        worker: dict,
        action: str,
        *,
        url: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, object]:
        self.last_desktop_action = {
            "worker_id": worker["worker_id"],
            "action": action,
            "url": url,
            "run_id": run_id,
        }
        return {
            "action": action,
            "status": "launched",
            "mode": "workstation-desktop",
            "url": "http://127.0.0.1:57906/?autoconnect=1",
            "view_url": "http://127.0.0.1:57906/?autoconnect=1",
            "notes": f"{action} launched",
        }


def test_desktop_action_and_artifact_preview_surface_in_project_ui(tmp_path):
    db_path = tmp_path / "runtime.db"
    runtime = DesktopStubRuntime(tmp_path / "desktop")
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=runtime))

    project = client.post(
        "/v1/projects",
        json={"owner_id": "demo-owner", "title": "Desktop UX", "goal": "Expose workstation controls and artifact previews."},
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={"owner_id": "demo-owner", "name": "Desktop Worker", "role": "operator", "profile": "codex-cli"},
    ).json()

    workspace_dir = Path(worker["workspace_dir"])
    png_path = workspace_dir / "latest-proof.png"
    png_path.write_bytes(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d49444154789c6360606060000000040001f61738550000000049454e44ae426082"
        )
    )

    action = client.post(
        f"/v1/workers/{worker['worker_id']}/desktop-action",
        json={"action": "codex", "run_id": "run_demo123"},
    )
    assert action.status_code == 202
    action_payload = action.json()
    assert action_payload["mode"] == "workstation-desktop"
    assert action_payload["status"] == "launched"
    assert action_payload["url"].startswith("http://127.0.0.1:57906/")
    assert runtime.last_desktop_action == {
        "worker_id": worker["worker_id"],
        "action": "codex",
        "url": None,
        "run_id": "run_demo123",
    }

    artifact = client.get(f"/v1/workers/{worker['worker_id']}/artifacts/latest-image")
    assert artifact.status_code == 200
    assert artifact.headers["content-type"] == "image/png"

    project_ui = client.get(f"/ui/projects/{project['project_id']}?worker_id={worker['worker_id']}")
    assert project_ui.status_code == 200
    assert "Workstation Tools" in project_ui.text
    assert "Open Codex" in project_ui.text
    assert "Latest Visual Artifact" in project_ui.text
    assert f"/v1/workers/{worker['worker_id']}/artifacts/latest-image" in project_ui.text


def test_launch_failed_endpoint_marks_worker_failed(tmp_path):
    db_path = tmp_path / "runtime.db"
    client = TestClient(create_app(str(db_path), runtime_backend="stub", runtime=StubRuntime()))

    project = client.post(
        "/v1/projects",
        json={"owner_id": "demo-owner", "title": "Launch Failure", "goal": "Record a failed launch clearly."},
    ).json()
    worker = client.post(
        f"/v1/projects/{project['project_id']}/workers",
        json={"owner_id": "demo-owner", "name": "Launch Worker", "role": "operator", "profile": "codex-cli"},
    ).json()

    response = client.post(
        f"/v1/workers/{worker['worker_id']}/launch-failed",
        json={"reason": "assign failed during launch"},
    )
    assert response.status_code == 202
    payload = response.json()
    assert payload["state"] == "failed"
    assert payload["last_error"] == "assign failed during launch"

    events = client.get(f"/v1/workers/{worker['worker_id']}/events").json()["items"]
    assert any(event["event_type"] == "worker.launch_failed" for event in events)
