from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from workers_projects_runtime.docker_sandbox import DockerSandboxManager
from workers_projects_runtime.openclaw_runtime import RuntimeErrorBase, RuntimeInfo, StubRuntime
from workers_projects_runtime.profile_runtime import CodexCliRuntime, OpenClawWorkstationRuntime
from workers_projects_runtime.service import WorkersProjectsService
from workers_projects_runtime.store import Store


class _ContainerSwapControlRuntime(StubRuntime):
    requires_run_start_identity = True

    def __init__(self) -> None:
        super().__init__()
        self.identity_calls = 0
        self.destructive_calls = 0

    def compute_identity(self, _worker: dict) -> dict[str, str]:
        self.identity_calls += 1
        return {
            "container_id": (
                "container-captured" if self.identity_calls == 1 else "container-replacement"
            )
        }

    def pause_worker(self, worker: dict) -> RuntimeInfo:
        self.destructive_calls += 1
        return super().pause_worker(worker)

    def interrupt_worker(self, worker: dict, run_id: str | None = None) -> RuntimeInfo:
        self.destructive_calls += 1
        return super().interrupt_worker(worker, run_id=run_id)


@pytest.mark.parametrize(
    ("control", "claim_kind"),
    [
        ("pause", "pause_run"),
        ("interrupt", "interrupt_run"),
        ("steer", "steer_run"),
    ],
)
def test_container_generation_swap_before_control_rpc_keeps_exact_claim_fenced(
    tmp_path,
    monkeypatch,
    control: str,
    claim_kind: str,
):
    monkeypatch.setenv("GLASSHIVE_IDLE_REAPER_INTERVAL_S", "3600")
    store = Store(str(tmp_path / "runtime.db"))
    runtime = _ContainerSwapControlRuntime()
    service = WorkersProjectsService(store, runtime, reconcile_on_startup=False)
    service._ensure_worker_processor = lambda _worker_id: None  # type: ignore[method-assign]
    project = store.create_project(
        "owner-a",
        "Exact Docker control",
        "Never act on a replacement container generation",
        "openclaw-general",
    )
    worker = store.create_worker(
        project_id=project["project_id"],
        owner_id="owner-a",
        name="Exact Docker worker",
        role="research",
        profile="openclaw-general",
        backend="openclaw",
        runtime="openclaw-stub",
        model="stub-model",
        execution_mode="docker",
    )
    run = store.create_run(
        worker["worker_id"],
        project["project_id"],
        "Keep this exact run under control",
        state="running",
    )
    run = store.update_run(
        run["run_id"], started_at=datetime.now(timezone.utc).isoformat()
    ) or run
    store.update_worker_state(worker["worker_id"], "running")

    try:
        with pytest.raises(RuntimeErrorBase, match="sandbox generation changed"):
            if control == "pause":
                service.pause_worker(worker["worker_id"], run_id=run["run_id"])
            elif control == "interrupt":
                service.interrupt_worker(worker["worker_id"], run_id=run["run_id"])
            else:
                service.steer_worker(
                    worker["worker_id"],
                    "Use the corrected objective",
                    run_id=run["run_id"],
                    idempotency_key="exact-docker-steer",
                )

        durable_worker = store.get_worker(worker["worker_id"]) or {}
        assert runtime.destructive_calls == 0
        assert durable_worker["compute_release_kind"] == claim_kind
        assert durable_worker["compute_release_container_id"] == "container-captured"
        assert durable_worker["compute_release_token"]
        assert (store.get_run(run["run_id"]) or {})["state"] == "running"
    finally:
        service.shutdown()


def _docker_inspect_payload(container_id: str, *, paused: bool = False) -> str:
    return json.dumps(
        [
            {
                "Id": container_id,
                "State": {
                    "Status": "running",
                    "Paused": paused,
                    "Pid": 4242,
                },
                "HostConfig": {},
                "NetworkSettings": {"Ports": {}},
            }
        ]
    )


def test_docker_pause_refuses_replacement_generation_without_destructive_command(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    commands: list[list[str]] = []

    def fake_docker(args: list[str], **_kwargs):
        commands.append(args)
        if args[:1] == ["inspect"]:
            return subprocess.CompletedProcess(
                ["docker", *args],
                0,
                _docker_inspect_payload("container-replacement"),
                "",
            )
        raise AssertionError(f"unexpected destructive Docker command: {args}")

    manager._docker = fake_docker  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="generation changed"):
        manager.pause("wrk_test", expected_container_id="container-captured")

    assert commands == [["inspect", "wpr-wrk-test"]]


def test_docker_pause_addresses_and_confirms_captured_container_id(tmp_path):
    manager = DockerSandboxManager(base_dir=str(tmp_path))
    paused = False
    commands: list[list[str]] = []

    def fake_docker(args: list[str], **_kwargs):
        nonlocal paused
        commands.append(args)
        if args[:1] == ["inspect"]:
            return subprocess.CompletedProcess(
                ["docker", *args],
                0,
                _docker_inspect_payload("container-captured", paused=paused),
                "",
            )
        if args[:1] == ["pause"]:
            assert args == ["pause", "container-captured"]
            paused = True
            return subprocess.CompletedProcess(["docker", *args], 0, "", "")
        raise AssertionError(args)

    manager._docker = fake_docker  # type: ignore[method-assign]

    result = manager.pause("wrk_test", expected_container_id="container-captured")

    assert result.state == "paused"
    assert commands == [
        ["inspect", "wpr-wrk-test"],
        ["pause", "container-captured"],
        ["inspect", "wpr-wrk-test"],
    ]


@pytest.mark.parametrize("runtime_type", [CodexCliRuntime, OpenClawWorkstationRuntime])
def test_docker_runtime_pause_passes_captured_container_id(tmp_path, runtime_type):
    runtime = runtime_type(base_dir=str(tmp_path / "runtime"))
    calls: list[tuple[str, str]] = []
    runtime.sandbox.pause = (  # type: ignore[method-assign]
        lambda worker_id, *, expected_container_id=None: (
            calls.append((worker_id, str(expected_container_id or "")))
            or SimpleNamespace(state="paused")
        )
    )

    runtime.pause_worker(
        {
            "worker_id": "wrk_pause_exact",
            "profile": "codex-cli" if runtime_type is CodexCliRuntime else "openclaw-general",
            "model": "synthetic-model",
            "_compute_release_container_id": "container-captured",
        }
    )

    assert calls == [("wrk_pause_exact", "container-captured")]


@pytest.mark.parametrize("runtime_type", [CodexCliRuntime, OpenClawWorkstationRuntime])
def test_docker_runtime_interrupt_targets_captured_container_for_each_destructive_primitive(
    tmp_path,
    runtime_type,
):
    runtime = runtime_type(base_dir=str(tmp_path / "runtime"))
    worker_id = "wrk_interrupt_exact"
    run_id = "run_interrupt_exact"
    runtime._ensure_dirs(worker_id)
    runtime._write_active_session(
        worker_id,
        {
            "session_name": "job-run_interrupt_exact",
            "run_id": run_id,
            "process_pid": 4242,
        },
    )
    calls: list[tuple[str, str]] = []
    runtime.sandbox.stop_screen_session = (  # type: ignore[method-assign]
        lambda _worker_id, _runtime_name, _session_name, **kwargs: calls.append(
            ("screen", str(kwargs.get("expected_container_id") or ""))
        )
    )
    runtime.sandbox.terminate_run_processes = (  # type: ignore[method-assign]
        lambda _worker_id, _runtime_name, _run_id, **kwargs: calls.append(
            ("run", str(kwargs.get("expected_container_id") or ""))
        )
    )

    runtime.interrupt_worker(
        {
            "worker_id": worker_id,
            "state": "running",
            "profile": "codex-cli" if runtime_type is CodexCliRuntime else "openclaw-general",
            "model": "synthetic-model",
            "_compute_release_container_id": "container-captured",
        },
        run_id=run_id,
    )

    assert calls == [
        ("screen", "container-captured"),
        ("run", "container-captured"),
    ]
