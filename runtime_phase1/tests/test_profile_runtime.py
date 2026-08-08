from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import subprocess
import sys
import threading
import time
import tomllib
from datetime import datetime
from pathlib import Path

import pytest

import workers_projects_runtime.profile_runtime as profile_runtime_module
from workers_projects_runtime.bootstrap import GLASSHIVE_CRITICAL_OPERATING_INSTRUCTIONS, GLASSHIVE_SAFETY_CHECKPOINT_RULE
from workers_projects_runtime.failure_classification import classify_cli_failure, classify_runtime_error
from workers_projects_runtime.openclaw_runtime import RuntimeDependencyMissingError, RuntimeErrorBase, WorkerTerminatedError
from workers_projects_runtime.profile_runtime import BaseCliWorkerRuntime, ClaudeCodeRuntime, CodexCliRuntime, HostClaudeCodeRuntime, HostCodexCliRuntime, HostOpenClawRuntime, OpenClawWorkstationRuntime, ProfiledWorkerRuntime, _redact_text
from workers_projects_runtime.run_evidence import build_constraint_ledger, write_constraint_ledger


@pytest.fixture(autouse=True)
def isolate_host_claude_auth_from_unit_tests(monkeypatch):
    """Never let command-construction tests read or rotate a developer's real Claude login."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "synthetic-test-oauth-token")


def _patch_host_codex_requirement_probe(monkeypatch):
    monkeypatch.setattr(
        "workers_projects_runtime.runtime_requirements.subprocess.run",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args,
            returncode=0,
            stdout="codex-cli 0.146.1\n",
            stderr="",
        ),
    )


def _mark_fake_host_supervisor_ready(command: list[str], pid: int) -> None:
    """Make legacy Popen fakes honor the native supervisor readiness contract."""
    if len(command) < 5 or Path(command[1]).name != "native-process-supervisor.py":
        return
    ready_path = Path(command[4])
    ready_path.write_text(f"{pid}\n")
    ready_path.chmod(0o600)


def _write_pass_evidence(runtime, worker_id: str, run_id: str) -> None:
    evidence_dir = runtime._workspace_dir(worker_id) / "glasshive-run" / "runs" / run_id
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "constraint-ledger.json").write_text(
        json.dumps(
            {
                "schema": "glasshive.run.constraint-ledger.v1",
                "run_id": run_id,
                "worker": {"worker_id": worker_id, "profile": "codex-cli", "execution_mode": "host"},
                "original_request": "Synthetic recovered run test.",
                "constraints": {"date": [], "source": [], "auth": [], "scope": [], "exclusion_or_flag": []},
                "outputs": {
                    "required": [],
                    "forbidden": [],
                    "format_expectations": [],
                    "forbidden_format_expectations": [],
                },
                "seed_entities_or_files": [],
                "do_not_widen_or_soften": False,
            }
        )
        + "\n"
    )
    (evidence_dir / "evidence.json").write_text(json.dumps({"evidence_result": {"status": "pass"}}) + "\n")


def test_terminal_target_uses_inferred_job_session_when_metadata_missing(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_test",
        "name": "Main Worker",
        "profile": "codex-cli",
    }
    runtime._ensure_dirs(worker["worker_id"])
    run_id = "run_123456789abc"
    run_root = runtime._run_root(worker["worker_id"], run_id)
    run_root.mkdir(parents=True, exist_ok=True)

    session_name = runtime._session_name_for_run_id(run_id)

    runtime.ensure_worker_ready = lambda worker: runtime._runtime_info(worker, pid=1234)  # type: ignore[method-assign]
    runtime.sandbox.list_screen_sessions = lambda worker_id, runtime_name, worker=None: [session_name]  # type: ignore[method-assign]
    runtime.sandbox.terminal_attach_command = (  # type: ignore[method-assign]
        lambda worker_id, runtime_name, session_name="operator": ["attach", session_name]
    )

    target = runtime.terminal_target(worker)
    assert target.command == ["attach", session_name]
    assert target.title == "Main Worker live session"
    assert target.subtitle == "codex-cli active run"


def test_host_terminal_target_preserves_shell_fallback_expression(tmp_path):
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_host_terminal",
        "name": "Host Claude",
        "profile": "claude-code",
        "execution_mode": "host",
    }
    runtime.ensure_worker_ready = lambda worker: runtime._runtime_info(worker, pid=1234)  # type: ignore[method-assign]
    runtime._infer_active_session = lambda worker: None  # type: ignore[method-assign]

    target = runtime.terminal_target(worker)

    assert target.command[-1].endswith("exec ${SHELL:-/bin/bash}")
    assert target.title == "Host Claude host terminal"


def test_host_runtime_recovers_and_stops_a_persisted_process_after_api_restart(tmp_path):
    runtime_before_restart = HostCodexCliRuntime(base_dir=str(tmp_path))
    runtime_after_restart = HostCodexCliRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_host_restart",
        "name": "Host Codex",
        "profile": "codex-cli",
        "execution_mode": "host",
    }
    process = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
    try:
        runtime_before_restart._write_active_session(
            worker["worker_id"],
            {
                "session_name": "conversation-run_restart",
                "run_id": "run_restart",
                "stdout_path": str(tmp_path / "stdout.log"),
                "stderr_path": str(tmp_path / "stderr.log"),
                "exit_path": str(tmp_path / "exit_code"),
                "model": "gpt-5.6-sol",
                "process_pid": process.pid,
                "started_at": datetime.now().astimezone().isoformat(),
            },
        )

        assert runtime_after_restart.reconcile_worker(worker).pid == process.pid

        runtime_after_restart._stop_active_process(
            worker["worker_id"],
            worker=worker,
            run_id="run_restart",
        )
        process.wait(timeout=3)

        assert process.returncode is not None
        assert runtime_after_restart.reconcile_worker(worker).pid is None
        assert not runtime_after_restart._active_session_meta_path(worker["worker_id"]).exists()
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=3)


def test_host_runtime_rejects_recycled_pid_identity_without_stopping_the_process(tmp_path):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_host_recycled_pid",
        "name": "Host Codex",
        "profile": "codex-cli",
        "execution_mode": "host",
    }
    unrelated_process = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
    try:
        runtime._write_active_session(
            worker["worker_id"],
            {
                "session_name": "conversation-run_recycled",
                "run_id": "run_recycled",
                "stdout_path": str(tmp_path / "stdout.log"),
                "stderr_path": str(tmp_path / "stderr.log"),
                "exit_path": str(tmp_path / "exit_code"),
                "model": "gpt-5.6-sol",
                "process_pid": unrelated_process.pid,
                "process_identity_sha256": "0" * 64,
                "started_at": datetime.now().astimezone().isoformat(),
            },
        )

        assert runtime.reconcile_worker(worker).pid is None
        runtime._stop_active_process(
            worker["worker_id"],
            worker=worker,
            run_id="run_recycled",
        )

        assert unrelated_process.poll() is None
    finally:
        if unrelated_process.poll() is None:
            unrelated_process.terminate()
            unrelated_process.wait(timeout=3)


@pytest.mark.parametrize(
    ("runtime_class", "profile", "model", "stdout_payload", "expected_output", "expected_session"),
    [
        (
            HostCodexCliRuntime,
            "codex-cli",
            "gpt-5.6-sol",
            "\n".join(
                [
                    json.dumps({"type": "thread.started", "thread_id": "thread-recovered"}),
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {
                                "type": "agent_message",
                                "text": "Recovered Codex conversation.",
                            },
                        }
                    ),
                ]
            ),
            "Recovered Codex conversation.",
            "thread-recovered",
        ),
        (
            HostClaudeCodeRuntime,
            "claude-code",
            "opus",
            json.dumps(
                {
                    "type": "result",
                    "result": "Recovered Claude conversation.",
                    "session_id": "session-recovered",
                }
            ),
            "Recovered Claude conversation.",
            "session-recovered",
        ),
    ],
)
def test_host_native_child_persists_completion_for_restart_recovery_without_duplicate_authoring(
    tmp_path,
    runtime_class,
    profile,
    model,
    stdout_payload,
    expected_output,
    expected_session,
):
    private_state = tmp_path / "private-state"
    runtime_before_restart = runtime_class(base_dir=str(private_state))
    runtime_after_restart = runtime_class(base_dir=str(private_state))
    life = tmp_path / "Life"
    life.mkdir()
    worker = {
        "worker_id": f"wrk_recover_{profile}",
        "name": "Synthetic conversation worker",
        "profile": profile,
        "execution_mode": "host",
        "workspace_root": str(life),
        "model": model,
        "bootstrap_bundle_json": json.dumps({"run_mode": "conversation"}),
    }
    run_id = f"run_recover_{profile}"
    run_root = runtime_before_restart._run_root(worker["worker_id"], run_id)
    run_root.mkdir(parents=True, exist_ok=True)
    run_root.chmod(0o700)
    stdout_path = run_root / "stdout.log"
    stderr_path = run_root / "stderr.log"
    exit_path = run_root / "exit_code"
    launch_count_path = run_root / "launch-count.log"
    stdin_hash_path = run_root / "stdin.sha256"
    instruction_path = run_root / "instruction.stdin"
    private_instruction = "Complete durable restart prompt.\n" + ("synthetic-context " * 2048)
    instruction_path.write_text(private_instruction)
    instruction_path.chmod(0o600)
    expected_stdin_hash = hashlib.sha256(private_instruction.encode()).hexdigest()
    child_code = (
        "import hashlib,pathlib,sys,time; "
        "pathlib.Path(sys.argv[1]).open('a').write('launch\\n'); "
        "stdin_text=sys.stdin.read(); "
        "stdin_hash=hashlib.sha256(stdin_text.encode()).hexdigest(); "
        "pathlib.Path(sys.argv[2]).write_text(stdin_hash); "
        "sys.exit(61) if stdin_hash != sys.argv[3] else None; "
        "time.sleep(0.2); print(sys.argv[4], flush=True)"
    )
    command = [
        sys.executable,
        "-c",
        child_code,
        str(launch_count_path),
        str(stdin_hash_path),
        expected_stdin_hash,
        stdout_payload,
    ]
    process_command = runtime_before_restart._durable_host_process_command(
        command,
        run_root=run_root,
        exit_path=exit_path,
        stdin_path=instruction_path,
    )

    process: subprocess.Popen[str] | None = None
    try:
        with stdout_path.open("w") as stdout_handle, stderr_path.open("w") as stderr_handle:
            process = subprocess.Popen(
                process_command,
                cwd=str(life),
                text=True,
                stdin=subprocess.DEVNULL,
                stdout=stdout_handle,
                stderr=stderr_handle,
                start_new_session=True,
            )
        runtime_before_restart._wait_for_durable_host_supervisor(
            process,
            run_root=run_root,
        )
        runtime_before_restart._write_active_session(
            worker["worker_id"],
            {
                "session_name": f"conversation-{run_id[:12]}",
                "run_id": run_id,
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
                "exit_path": str(exit_path),
                "model": model,
                "argv_for_evidence_json": json.dumps(command),
                "started_at": datetime.now().astimezone().isoformat(),
                "process_pid": process.pid,
                "run_mode": "conversation",
            },
        )
        time.sleep(0.1)
        assert not launch_count_path.exists()
        assert not exit_path.exists()
        persisted_session = runtime_after_restart._read_active_session(worker["worker_id"])
        assert persisted_session is not None
        assert persisted_session["process_pid"] == process.pid
        assert persisted_session["process_identity_sha256"]
        assert persisted_session["run_mode"] == "conversation"
        assert stat.S_IMODE(
            runtime_after_restart._active_session_meta_path(worker["worker_id"]).stat().st_mode
        ) == 0o600
        assert list(
            runtime_after_restart._active_session_meta_path(worker["worker_id"]).parent.glob(
                "active-session.json.tmp-*"
            )
        ) == []

        # A fresh API instance observes the durable metadata and releases the
        # pre-authoring handshake exactly once.
        assert runtime_after_restart.reconcile_worker(worker).pid == process.pid

        deadline = time.monotonic() + 5
        while not exit_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert exit_path.read_text().strip() == "0"

        recovered = runtime_after_restart.collect_completed_run(worker, run_id=run_id)
        recovered_again = runtime_after_restart.collect_completed_run(worker, run_id=run_id)

        assert recovered is not None
        assert recovered["state"] == "completed"
        assert recovered["output_text"] == expected_output
        assert recovered_again is not None
        assert recovered_again["state"] == "completed"
        assert recovered_again["output_text"] == expected_output
        assert json.loads(
            runtime_after_restart._session_meta_path(worker["worker_id"]).read_text()
        )["session_key"] == expected_session
        assert launch_count_path.read_text().splitlines() == ["launch"]
        assert stdin_hash_path.read_text() == expected_stdin_hash
        assert stat.S_IMODE(exit_path.stat().st_mode) == 0o600
        assert stat.S_IMODE((run_root / "native-process-supervisor.py").stat().st_mode) == 0o700
        assert list(run_root.glob("exit_code.tmp.*")) == []
        assert not (life / "glasshive-run").exists()
    finally:
        if process is not None:
            process.wait(timeout=5)


def test_host_native_restart_cancellation_stops_process_group_and_persists_terminal_marker(
    tmp_path,
):
    private_state = tmp_path / "private-state"
    runtime_before_restart = HostCodexCliRuntime(base_dir=str(private_state))
    runtime_after_restart = HostCodexCliRuntime(base_dir=str(private_state))
    worker = {
        "worker_id": "wrk_cancel_after_restart",
        "name": "Synthetic cancellation worker",
        "profile": "codex-cli",
        "execution_mode": "host",
    }
    run_id = "run_cancel_after_restart"
    run_root = runtime_before_restart._run_root(worker["worker_id"], run_id)
    run_root.mkdir(parents=True, exist_ok=True)
    exit_path = run_root / "exit_code"
    child_pid_path = run_root / "child.pid"
    process_command = runtime_before_restart._durable_host_process_command(
        [
            sys.executable,
            "-c",
            (
                "import os,pathlib,sys,time; "
                "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
                "time.sleep(30)"
            ),
            str(child_pid_path),
        ],
        run_root=run_root,
        exit_path=exit_path,
    )
    process = subprocess.Popen(
        process_command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        runtime_before_restart._wait_for_durable_host_supervisor(
            process,
            run_root=run_root,
        )
        runtime_before_restart._write_active_session(
            worker["worker_id"],
            {
                "session_name": f"conversation-{run_id[:12]}",
                "run_id": run_id,
                "stdout_path": str(run_root / "stdout.log"),
                "stderr_path": str(run_root / "stderr.log"),
                "exit_path": str(exit_path),
                "model": "gpt-5.6-sol",
                "process_pid": process.pid,
                "run_mode": "conversation",
            },
        )
        assert runtime_after_restart.reconcile_worker(worker).pid == process.pid
        deadline = time.monotonic() + 5
        while not child_pid_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert child_pid_path.exists()
        child_pid = int(child_pid_path.read_text())

        runtime_after_restart._stop_active_process(
            worker["worker_id"],
            worker=worker,
            run_id=run_id,
        )
        process.wait(timeout=5)

        assert exit_path.read_text().strip() == "143"
        assert stat.S_IMODE(exit_path.stat().st_mode) == 0o600
        assert list(run_root.glob("exit_code.tmp.*")) == []
        assert not runtime_after_restart._active_session_meta_path(worker["worker_id"]).exists()
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
        with pytest.raises(ProcessLookupError):
            os.killpg(process.pid, 0)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, 9)
            process.wait(timeout=5)


def test_host_native_nonzero_exit_is_durably_recovered_with_precise_failure(tmp_path):
    private_state = tmp_path / "private-state"
    runtime_before_restart = HostCodexCliRuntime(base_dir=str(private_state))
    runtime_after_restart = HostCodexCliRuntime(base_dir=str(private_state))
    life = tmp_path / "Life"
    life.mkdir()
    worker = {
        "worker_id": "wrk_nonzero_after_restart",
        "name": "Synthetic nonzero worker",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(life),
        "model": "gpt-5.6-sol",
        "bootstrap_bundle_json": json.dumps({"run_mode": "conversation"}),
    }
    run_id = "run_nonzero_after_restart"
    run_root = runtime_before_restart._run_root(worker["worker_id"], run_id)
    run_root.mkdir(parents=True, exist_ok=True)
    stdout_path = run_root / "stdout.log"
    stderr_path = run_root / "stderr.log"
    exit_path = run_root / "exit_code"
    command = [
        sys.executable,
        "-c",
        "import sys; sys.stderr.write('synthetic durable child failure\\n'); sys.exit(23)",
    ]
    process_command = runtime_before_restart._durable_host_process_command(
        command,
        run_root=run_root,
        exit_path=exit_path,
    )
    with stdout_path.open("w") as stdout_handle, stderr_path.open("w") as stderr_handle:
        process = subprocess.Popen(
            process_command,
            cwd=str(life),
            stdin=subprocess.DEVNULL,
            stdout=stdout_handle,
            stderr=stderr_handle,
            start_new_session=True,
        )
    try:
        runtime_before_restart._wait_for_durable_host_supervisor(
            process,
            run_root=run_root,
        )
        runtime_before_restart._write_active_session(
            worker["worker_id"],
            {
                "session_name": f"conversation-{run_id[:12]}",
                "run_id": run_id,
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
                "exit_path": str(exit_path),
                "model": "gpt-5.6-sol",
                "process_pid": process.pid,
                "run_mode": "conversation",
            },
        )
        assert runtime_after_restart.reconcile_worker(worker).pid == process.pid

        deadline = time.monotonic() + 5
        while not exit_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        process.wait(timeout=5)
        recovered = runtime_after_restart.collect_completed_run(worker, run_id=run_id)

        assert exit_path.read_text().strip() == "23"
        assert process.returncode == 23
        assert recovered is not None
        assert recovered["state"] == "failed"
        assert "exited with code 23" in recovered["error_text"]
        assert "synthetic durable child failure" in recovered["error_text"]
        assert not (life / "glasshive-run").exists()
    finally:
        if process.poll() is None:
            os.killpg(process.pid, 9)
            process.wait(timeout=5)


def test_host_native_configured_timeout_survives_api_restart_and_stops_child_group(tmp_path):
    private_state = tmp_path / "private-state"
    runtime_before_restart = HostCodexCliRuntime(base_dir=str(private_state))
    runtime_after_restart = HostCodexCliRuntime(base_dir=str(private_state))
    life = tmp_path / "Life"
    life.mkdir()
    worker = {
        "worker_id": "wrk_timeout_after_restart",
        "name": "Synthetic timeout worker",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(life),
        "model": "gpt-5.6-sol",
        "bootstrap_bundle_json": json.dumps({"run_mode": "conversation"}),
    }
    run_id = "run_timeout_after_restart"
    run_root = runtime_before_restart._run_root(worker["worker_id"], run_id)
    run_root.mkdir(parents=True, exist_ok=True)
    stdout_path = run_root / "stdout.log"
    stderr_path = run_root / "stderr.log"
    exit_path = run_root / "exit_code"
    child_pid_path = run_root / "child.pid"
    command = [
        sys.executable,
        "-c",
        (
            "import os,pathlib,sys,time; "
            "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
            "print('child started', flush=True); time.sleep(30)"
        ),
        str(child_pid_path),
    ]
    process_command = runtime_before_restart._durable_host_process_command(
        command,
        run_root=run_root,
        exit_path=exit_path,
        timeout_sec=1.0,
    )
    with stdout_path.open("w") as stdout_handle, stderr_path.open("w") as stderr_handle:
        process = subprocess.Popen(
            process_command,
            cwd=str(life),
            stdin=subprocess.DEVNULL,
            stdout=stdout_handle,
            stderr=stderr_handle,
            start_new_session=True,
        )
    try:
        runtime_before_restart._wait_for_durable_host_supervisor(
            process,
            run_root=run_root,
        )
        runtime_before_restart._write_active_session(
            worker["worker_id"],
            {
                "session_name": f"conversation-{run_id[:12]}",
                "run_id": run_id,
                "stdout_path": str(stdout_path),
                "stderr_path": str(stderr_path),
                "exit_path": str(exit_path),
                "model": "gpt-5.6-sol",
                "process_pid": process.pid,
                "timeout_seconds": 1.0,
                "run_mode": "conversation",
            },
        )
        started = time.monotonic()
        assert runtime_after_restart.reconcile_worker(worker).pid == process.pid
        child_start_deadline = time.monotonic() + 0.5
        while not child_pid_path.exists() and time.monotonic() < child_start_deadline:
            time.sleep(0.01)
        assert child_pid_path.exists()

        deadline = time.monotonic() + 5
        while not exit_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        process.wait(timeout=5)
        elapsed = time.monotonic() - started
        recovered = runtime_after_restart.collect_completed_run(worker, run_id=run_id)

        assert exit_path.read_text().strip() == "124"
        assert process.returncode == 124
        assert elapsed < 3
        assert child_pid_path.exists()
        child_pid = int(child_pid_path.read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
        assert recovered is not None
        assert recovered["state"] == "failed"
        assert "exited with code 124" in recovered["error_text"]
        assert "timed out after 1s" in recovered["error_text"]
        assert not (life / "glasshive-run").exists()
    finally:
        if process.poll() is None:
            os.killpg(process.pid, 9)
            process.wait(timeout=5)


def test_collect_completed_run_recovers_from_latest_run_artifacts(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_test",
        "name": "Main Worker",
        "profile": "codex-cli",
        "model": "gpt-5.4",
    }
    runtime._ensure_dirs(worker["worker_id"])

    run_id = "run_abcdef123456"
    run_root = runtime._run_root(worker["worker_id"], run_id)
    run_root.mkdir(parents=True, exist_ok=True)
    (run_root / "stdout.log").write_text(
        "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "thread_123"}),
                json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "FINAL REPORT:\nHELLO WORLD"}}),
            ]
        )
        + "\n"
    )
    (run_root / "stderr.log").write_text("")
    (run_root / "exit_code").write_text("0")
    _write_pass_evidence(runtime, worker["worker_id"], run_id)

    runtime.reconcile_worker = lambda worker: runtime._runtime_info(worker, pid=1234)  # type: ignore[method-assign]

    recovered = runtime.collect_completed_run(worker)
    assert recovered is not None
    assert recovered["state"] == "completed"
    assert recovered["output_text"] == "HELLO WORLD"
    assert json.loads(runtime._session_meta_path(worker["worker_id"]).read_text())["session_key"] == "thread_123"


def test_collect_completed_run_fails_when_recovered_success_missing_constraint_ledger(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_missing_ledger",
        "name": "Main Worker",
        "profile": "codex-cli",
        "model": "gpt-5.4",
    }
    runtime._ensure_dirs(worker["worker_id"])

    run_id = "run_missingledger"
    run_root = runtime._run_root(worker["worker_id"], run_id)
    run_root.mkdir(parents=True, exist_ok=True)
    (run_root / "stdout.log").write_text(
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "FINAL REPORT:\nDone"}}) + "\n"
    )
    (run_root / "stderr.log").write_text("")
    (run_root / "exit_code").write_text("0")
    _write_pass_evidence(runtime, worker["worker_id"], run_id)
    (runtime._workspace_dir(worker["worker_id"]) / "glasshive-run" / "runs" / run_id / "constraint-ledger.json").unlink()

    runtime.reconcile_worker = lambda worker: runtime._runtime_info(worker, pid=1234)  # type: ignore[method-assign]

    recovered = runtime.collect_completed_run(worker, run_id=run_id)
    assert recovered is not None
    assert recovered["state"] == "failed"
    assert "constraint ledger was not readable" in recovered["error_text"]
    assert recovered["failure_class"] == "glasshive_evidence_check_failed"
    assert recovered["failure_retryable"] == 1


def test_collect_completed_run_preserves_evidence_warning(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_warn_recovery",
        "name": "Main Worker",
        "profile": "codex-cli",
        "model": "gpt-5.4",
    }
    runtime._ensure_dirs(worker["worker_id"])

    run_id = "run_warnrecovery"
    run_root = runtime._run_root(worker["worker_id"], run_id)
    run_root.mkdir(parents=True, exist_ok=True)
    (run_root / "stdout.log").write_text(
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "FINAL REPORT:\nDone"}}) + "\n"
    )
    (run_root / "stderr.log").write_text("")
    (run_root / "exit_code").write_text("0")
    _write_pass_evidence(runtime, worker["worker_id"], run_id)
    evidence_path = runtime._workspace_dir(worker["worker_id"]) / "glasshive-run" / "runs" / run_id / "evidence.json"
    evidence_path.write_text(
        json.dumps(
            {
                "evidence_result": {
                    "status": "warn",
                    "warning_reasons": [{"reason": "content hygiene warning", "failure_count": 1}],
                }
            }
        )
        + "\n"
    )

    runtime.reconcile_worker = lambda worker: runtime._runtime_info(worker, pid=1234)  # type: ignore[method-assign]

    recovered = runtime.collect_completed_run(worker, run_id=run_id)
    assert recovered is not None
    assert recovered["state"] == "completed"
    assert recovered["output_text"].startswith("Done")
    assert "GlassHive evidence check warning: content hygiene warning" in recovered["output_text"]


def test_collect_completed_run_rejects_hollow_constraint_ledger(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_hollow_ledger",
        "name": "Main Worker",
        "profile": "codex-cli",
        "model": "gpt-5.4",
    }
    runtime._ensure_dirs(worker["worker_id"])

    run_id = "run_hollowledger"
    run_root = runtime._run_root(worker["worker_id"], run_id)
    run_root.mkdir(parents=True, exist_ok=True)
    (run_root / "stdout.log").write_text(
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "FINAL REPORT:\nDone"}}) + "\n"
    )
    (run_root / "stderr.log").write_text("")
    (run_root / "exit_code").write_text("0")
    _write_pass_evidence(runtime, worker["worker_id"], run_id)
    ledger_path = runtime._workspace_dir(worker["worker_id"]) / "glasshive-run" / "runs" / run_id / "constraint-ledger.json"
    ledger_path.write_text("{}\n")

    runtime.reconcile_worker = lambda worker: runtime._runtime_info(worker, pid=1234)  # type: ignore[method-assign]

    recovered = runtime.collect_completed_run(worker, run_id=run_id)
    assert recovered is not None
    assert recovered["state"] == "failed"
    assert recovered["failure_class"] == "glasshive_evidence_check_failed"
    assert "canonical schema" in recovered["error_text"]


def test_collect_completed_run_classifies_and_redacts_provider_rate_limit(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_rate_limit",
        "name": "Main Worker",
        "profile": "codex-cli",
        "model": "gpt-5.4",
    }
    runtime._ensure_dirs(worker["worker_id"])

    run_id = "run_rate12345"
    run_root = runtime._run_root(worker["worker_id"], run_id)
    run_root.mkdir(parents=True, exist_ok=True)
    (run_root / "stdout.log").write_text(
        "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "thread_rate"}),
                json.dumps({"type": "response.failed", "error": {"message": "Too Many Requests"}}),
                json.dumps({"type": "turn.failed", "error": {"message": "response.failed event received"}}),
            ]
        )
        + "\n"
    )
    (run_root / "stderr.log").write_text("api_key=PUBLIC_FAKE_API_KEY_VALUE token=PUBLIC_FAKE_TOKEN_VALUE\n")
    (run_root / "exit_code").write_text("1")

    recovered = runtime.collect_completed_run(worker, run_id=run_id)

    assert recovered is not None
    assert recovered["state"] == "failed"
    assert recovered["failure_class"] == "provider_rate_limited"
    assert recovered["failure_retryable"] == 1
    assert "workspace_continue" in recovered["failure_recommended_recovery"]
    assert "Too Many Requests" in recovered["failure_diagnostic_summary"]
    assert "PUBLIC_FAKE_API_KEY_VALUE" not in recovered["error_text"]
    assert "PUBLIC_FAKE_TOKEN_VALUE" not in recovered["error_text"]


def test_cli_failure_classifies_codex_usage_quota_as_provider_rate_limit():
    stdout = "\n".join(
        [
            json.dumps(
                {
                    "type": "error",
                    "message": (
                        "You've hit your usage limit. To get more access now, "
                        "review your provider plan."
                    ),
                }
            ),
            json.dumps({"type": "turn.failed"}),
        ]
    )

    failure = classify_cli_failure(
        stdout=stdout,
        stderr="",
        runtime_name="codex-cli",
        exit_code=1,
    )

    assert failure.failure_class == "provider_rate_limited"
    assert failure.retryable is True
    assert "quota or rate limit" in failure.user_message
    assert "provider-reported reset" in failure.recommended_recovery


def test_classify_cli_failure_maps_structured_provider_overload():
    failure = classify_cli_failure(
        stdout=json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": True,
                "api_error_status": 529,
                "result": "API Error: 529 Overloaded. This is a server-side issue, usually temporary.",
            }
        )
        + "\n",
        stderr="",
        runtime_name="claude-code",
        exit_code=1,
    )

    assert failure.failure_class == "provider_response_failed"
    assert failure.retryable is True
    assert "workspace_continue" in failure.recommended_recovery
    assert "api_error_status: 529" in failure.diagnostic_summary
    assert "Overloaded" in failure.diagnostic_summary


def test_classify_cli_failure_does_not_treat_unstructured_overloaded_prose_as_provider_outage():
    failure = classify_cli_failure(
        stdout="",
        stderr="The worker wrote a draft saying the market is overloaded with generic options.",
        runtime_name="codex-cli",
        exit_code=1,
    )

    assert failure.failure_class == "unknown"
    assert failure.retryable is False


def test_collect_completed_run_prefers_stdout_provider_failure_over_stale_stderr(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_response_failed",
        "name": "Main Worker",
        "profile": "codex-cli",
        "model": "gpt-5.4",
    }
    runtime._ensure_dirs(worker["worker_id"])

    run_id = "run_response_failed"
    run_root = runtime._run_root(worker["worker_id"], run_id)
    run_root.mkdir(parents=True, exist_ok=True)
    (run_root / "stdout.log").write_text(
        "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "thread_response_failed"}),
                "I wrote partial reports before the provider stream disconnected.",
                json.dumps({"type": "response.failed", "error": {"message": "stream disconnected before completion"}}),
                json.dumps({"type": "turn.failed", "error": {"message": "response.failed event received"}}),
            ]
        )
        + "\n"
    )
    (run_root / "stderr.log").write_text(
        "write_stdin failed: stdin is closed for this session; rerun exec_command with tty=true\n"
    )
    (run_root / "exit_code").write_text("1")

    recovered = runtime.collect_completed_run(worker, run_id=run_id)

    assert recovered is not None
    assert recovered["state"] == "failed"
    assert recovered["failure_class"] == "provider_response_failed"
    assert recovered["failure_retryable"] == 1
    assert "response.failed" in recovered["failure_diagnostic_summary"]
    assert "workspace_continue" in recovered["failure_recommended_recovery"]


def test_collect_completed_run_classifies_stdin_closed_as_retryable_runtime_io(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_stdin_closed",
        "name": "Main Worker",
        "profile": "codex-cli",
        "model": "gpt-5.4",
    }
    runtime._ensure_dirs(worker["worker_id"])

    run_id = "run_stdin_closed"
    run_root = runtime._run_root(worker["worker_id"], run_id)
    run_root.mkdir(parents=True, exist_ok=True)
    (run_root / "stdout.log").write_text("The worker wrote useful files before the session closed.\n")
    (run_root / "stderr.log").write_text(
        "write_stdin failed: stdin is closed for this session; rerun exec_command with tty=true\n"
    )
    (run_root / "exit_code").write_text("1")

    recovered = runtime.collect_completed_run(worker, run_id=run_id)

    assert recovered is not None
    assert recovered["state"] == "failed"
    assert recovered["failure_class"] == "runtime_io_failed"
    assert recovered["failure_retryable"] == 1
    assert "workspace_continue" in recovered["failure_recommended_recovery"]


def test_collect_completed_run_classifies_content_filter_as_not_retryable(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_filter",
        "name": "Main Worker",
        "profile": "codex-cli",
        "model": "gpt-5.4",
    }
    runtime._ensure_dirs(worker["worker_id"])

    run_id = "run_filter123"
    run_root = runtime._run_root(worker["worker_id"], run_id)
    run_root.mkdir(parents=True, exist_ok=True)
    (run_root / "stdout.log").write_text(
        json.dumps({"type": "turn.failed", "error": {"message": "content_filter"}}) + "\n"
    )
    (run_root / "stderr.log").write_text("")
    (run_root / "exit_code").write_text("1")

    recovered = runtime.collect_completed_run(worker, run_id=run_id)

    assert recovered is not None
    assert recovered["state"] == "failed"
    assert recovered["failure_class"] == "provider_content_filter"
    assert recovered["failure_retryable"] == 0
    assert "safety filter" in recovered["failure_user_message"]


def test_codex_parser_returns_latest_assistant_result_not_progress_chatter(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_progress",
        "name": "Main Worker",
        "profile": "codex-cli",
        "model": "gpt-5.4",
    }
    runtime._ensure_dirs(worker["worker_id"])
    stdout = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "thread_progress"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "I am scrolling and checking the page."},
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": "The page is loaded. The result is visible.",
                    },
                }
            ),
        ]
    )

    session_key, output = runtime._parse_output(worker, stdout, "", runtime._runtime_info(worker))

    assert session_key == "thread_progress"
    assert output == "The page is loaded. The result is visible."


def test_codex_parser_prefers_final_report_section(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_final_report",
        "name": "Main Worker",
        "profile": "codex-cli",
        "model": "gpt-5.4",
    }
    runtime._ensure_dirs(worker["worker_id"])
    stdout = "\n".join(
        [
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": "Progress that should never reach chat."},
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": "Done.\n\nFINAL REPORT:\nOnly this final result should be posted.",
                    },
                }
            ),
        ]
    )

    _, output = runtime._parse_output(worker, stdout, "", runtime._runtime_info(worker))

    assert output == "Only this final result should be posted."


def test_codex_parser_accepts_inline_final_report_section(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_inline_final_report",
        "name": "Main Worker",
        "profile": "codex-cli",
        "model": "gpt-5.4",
    }
    runtime._ensure_dirs(worker["worker_id"])
    stdout = json.dumps(
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": "Done.\nFINAL REPORT: Only this inline result should be posted.",
            },
        }
    )

    _, output = runtime._parse_output(worker, stdout, "", runtime._runtime_info(worker))

    assert output == "Only this inline result should be posted."


def test_codex_parser_accepts_backtick_wrapped_final_report_section(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_backtick_final_report",
        "name": "Main Worker",
        "profile": "codex-cli",
        "model": "gpt-5.4",
    }
    runtime._ensure_dirs(worker["worker_id"])
    stdout = json.dumps(
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": "Done.\n\n`FINAL REPORT:`\n\nOnly this final result should be posted.",
            },
        }
    )

    _, output = runtime._parse_output(worker, stdout, "", runtime._runtime_info(worker))

    assert output == "Only this final result should be posted."


def test_codex_parser_strips_plain_resume_final_report(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_plain_final_report",
        "name": "Main Worker",
        "profile": "codex-cli",
        "model": "gpt-5.4",
    }
    runtime._ensure_dirs(worker["worker_id"])
    stdout = "Progress line that should not reach chat.\nFINAL REPORT:\nMade the background red."

    _, output = runtime._parse_output(worker, stdout, "", runtime._runtime_info(worker))

    assert output == "Made the background red."


def test_codex_parser_ignores_agent_message_after_final_report(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_trailing_after_final_report",
        "name": "Main Worker",
        "profile": "codex-cli",
        "model": "gpt-5.4",
    }
    runtime._ensure_dirs(worker["worker_id"])
    stdout = "\n".join(
        [
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": "Done.\nFINAL REPORT:\nOnly the final answer.",
                    },
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": "Late progress should not be posted.",
                    },
                }
            ),
        ]
    )

    _, output = runtime._parse_output(worker, stdout, "", runtime._runtime_info(worker))

    assert output == "Only the final answer."


def test_collect_completed_run_with_explicit_run_id_ignores_previous_finished_run(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_test",
        "name": "Main Worker",
        "profile": "codex-cli",
        "model": "gpt-5.4",
    }
    runtime._ensure_dirs(worker["worker_id"])

    older_run_id = "run_older12345"
    older_root = runtime._run_root(worker["worker_id"], older_run_id)
    older_root.mkdir(parents=True, exist_ok=True)
    (older_root / "stdout.log").write_text(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "OLD"}}) + "\n")
    (older_root / "stderr.log").write_text("")
    (older_root / "exit_code").write_text("0")

    active_run_id = "run_active1234"
    active_root = runtime._run_root(worker["worker_id"], active_run_id)
    active_root.mkdir(parents=True, exist_ok=True)
    (active_root / "stdout.log").write_text(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "NEW"}}) + "\n")
    (active_root / "stderr.log").write_text("")

    runtime.reconcile_worker = lambda worker: runtime._runtime_info(worker, pid=1234)  # type: ignore[method-assign]

    assert runtime.collect_completed_run(worker, run_id=active_run_id) is None

    (active_root / "exit_code").write_text("0")
    _write_pass_evidence(runtime, worker["worker_id"], active_run_id)
    recovered = runtime.collect_completed_run(worker, run_id=active_run_id)
    assert recovered is not None
    assert recovered["state"] == "completed"
    assert recovered["output_text"] == "NEW"


def test_openclaw_command_uses_private_instruction_file_pointer(tmp_path):
    runtime = OpenClawWorkstationRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_openclaw_contract",
        "name": "Main Worker",
        "profile": "openclaw-general",
        "model": "openai/gpt-5.2",
        "_active_run_id": "run_openclaw_contract",
    }
    runtime._ensure_dirs(worker["worker_id"])

    command, _env = runtime._build_command(worker, "do the work", runtime._runtime_info(worker))

    assert "-m" in command
    pointer = command[command.index("-m") + 1]
    assert "do the work" not in pointer
    assert "FINAL REPORT:" not in pointer
    assert "/workspace/.wpr-home/.glasshive-runs/run_openclaw_contract/instruction.stdin" in pointer
    stdin_text = runtime._command_stdin_text(worker, "do the work", runtime._runtime_info(worker))
    assert stdin_text and stdin_text.startswith("do the work")
    assert "FINAL REPORT:" in stdin_text
    assert "Put only the user-facing result" in stdin_text


def test_host_openclaw_command_uses_private_instruction_file_pointer(tmp_path):
    runtime = HostOpenClawRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_host_openclaw_contract",
        "name": "Host OpenClaw Worker",
        "profile": "openclaw-general",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "workspaces"),
        "_active_run_id": "run_host_openclaw_contract",
    }
    runtime._ensure_dirs(worker["worker_id"])

    command, _env = runtime._build_command(worker, "do the private work", runtime._host_runtime_info(worker))

    assert "-m" in command
    pointer = command[command.index("-m") + 1]
    assert "do the private work" not in pointer
    assert "FINAL REPORT:" not in pointer
    assert "run_host_openclaw_contract/instruction.stdin" in pointer
    stdin_text = runtime._command_stdin_text(worker, "do the private work", runtime._host_runtime_info(worker))
    assert stdin_text and stdin_text.startswith("do the private work")
    assert "FINAL REPORT:" in stdin_text


def test_host_openclaw_run_writes_private_instruction_file_for_pointer(tmp_path, monkeypatch):
    runtime = HostOpenClawRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = "/bin/echo"
    monkeypatch.setattr("workers_projects_runtime.profile_runtime.host_runtime_requirement_issue", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "workers_projects_runtime.profile_runtime.subprocess.run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, returncode=0, stdout="", stderr=""),
    )
    captured: dict[str, object] = {}

    class OpenClawProcess:
        pid = 24680
        returncode = 0

        def __init__(self, command, **kwargs):
            _mark_fake_host_supervisor_ready(list(command), self.pid)
            captured["command"] = list(command)
            captured["stdin_is_devnull"] = kwargs["stdin"] == subprocess.DEVNULL
            self.stdout_handle = kwargs["stdout"]
            self.wrote_output = False

        def communicate(self, input=None, timeout=None):
            raise AssertionError("API process must not own supervisor stdin")

        def wait(self, timeout=None):
            if not self.wrote_output:
                self.stdout_handle.write(
                    json.dumps(
                        {
                            "finalAssistantVisibleText": "FINAL REPORT:\nDone.",
                            "completion": {"stopReason": "stop"},
                        }
                    )
                )
                self.stdout_handle.flush()
                self.wrote_output = True
            return self.returncode

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 130

        def kill(self):
            self.returncode = 130

    monkeypatch.setattr("workers_projects_runtime.profile_runtime.subprocess.Popen", OpenClawProcess)
    worker = {
        "worker_id": "wrk_host_openclaw_run_pointer",
        "name": "Host OpenClaw Run Pointer",
        "profile": "openclaw-general",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "workspaces"),
    }

    output = runtime.run_task(worker, "Sensitive OpenClaw task.", timeout_sec=5, run_id="run_host_openclaw_pointer")

    assert output == "Done."
    command = captured["command"]
    assert isinstance(command, list)
    assert captured["stdin_is_devnull"] is True
    assert Path(command[6]).read_text().startswith("Sensitive OpenClaw task.")
    pointer = command[command.index("-m") + 1]
    assert "Sensitive OpenClaw task" not in pointer
    assert "run_host_openclaw_pointer/instruction.stdin" in pointer
    stdin_path = runtime._run_root(worker["worker_id"], "run_host_openclaw_pointer") / "instruction.stdin"
    assert stdin_path.exists()
    assert stdin_path.read_text().startswith("Sensitive OpenClaw task.")
    assert oct(stdin_path.stat().st_mode & 0o777) == "0o600"


def test_openclaw_parser_prefers_final_visible_text(tmp_path):
    runtime = OpenClawWorkstationRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_openclaw_final",
        "name": "Main Worker",
        "profile": "openclaw-general",
        "model": "openai/gpt-5.2",
    }
    runtime._ensure_dirs(worker["worker_id"])
    stdout = json.dumps(
        {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "Progress that should not win."}],
                }
            ],
            "finalAssistantVisibleText": "FINAL REPORT:\nThe artifact is ready.",
            "completion": {"stopReason": "stop"},
            "meta": {"agentMeta": {"sessionId": "wpr-worker-wrk_openclaw_final"}},
        }
    )

    session_key, output = runtime._parse_output(worker, stdout, "", runtime._runtime_info(worker))

    assert session_key == "wpr-worker-wrk_openclaw_final"
    assert output == "The artifact is ready."


def test_openclaw_parser_accepts_nested_final_visible_text(tmp_path):
    runtime = OpenClawWorkstationRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_openclaw_nested_final",
        "name": "Main Worker",
        "profile": "openclaw-general",
        "model": "openai/gpt-5.2",
    }
    runtime._ensure_dirs(worker["worker_id"])
    stdout = json.dumps(
        {
            "payloads": [{"text": "Progress that should not win."}],
            "meta": {
                "finalAssistantVisibleText": "FINAL REPORT:\nNested result.",
                "completion": {"stopReason": "stop"},
                "agentMeta": {"sessionId": "wpr-worker-wrk_openclaw_nested_final"},
            },
        }
    )

    assert runtime._stdout_has_complete_response(Path("/missing")) is False
    path = tmp_path / "nested-openclaw-stdout.json"
    path.write_text(stdout)
    assert runtime._stdout_has_complete_response(path) is True
    session_key, output = runtime._parse_output(worker, stdout, "", runtime._runtime_info(worker))

    assert session_key == "wpr-worker-wrk_openclaw_nested_final"
    assert output == "Nested result."


def test_openclaw_collect_completed_run_recovers_final_json_without_exit_file(tmp_path):
    runtime = OpenClawWorkstationRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_openclaw_recover",
        "name": "Main Worker",
        "profile": "openclaw-general",
        "model": "openai/gpt-5.2",
    }
    runtime._ensure_dirs(worker["worker_id"])
    run_id = "run_openclaw123"
    run_root = runtime._run_root(worker["worker_id"], run_id)
    run_root.mkdir(parents=True, exist_ok=True)
    (run_root / "stdout.log").write_text(
        json.dumps(
            {
                "finalAssistantVisibleText": "FINAL REPORT:\nRecovered result.",
                "completion": {"stopReason": "stop"},
                "meta": {"agentMeta": {"sessionId": "wpr-worker-wrk_openclaw_recover"}},
            }
        )
    )
    (run_root / "stderr.log").write_text("")
    runtime._write_active_session(
        worker["worker_id"],
        {
            "session_name": runtime._session_name_for_run_id(run_id),
            "run_id": run_id,
            "stdout_path": str(run_root / "stdout.log"),
            "stderr_path": str(run_root / "stderr.log"),
            "exit_path": str(run_root / "exit_code"),
            "constraint_ledger_path": f"glasshive-run/runs/{run_id}/constraint-ledger.json",
            "instruction": "Create a recovered final report.",
        },
    )
    active_session_text = runtime._active_session_meta_path(worker["worker_id"]).read_text()
    assert "Create a recovered final report." not in active_session_text
    assert json.loads(active_session_text)["instruction_redacted"] is True
    ledger = build_constraint_ledger(
        instruction="Create a recovered final report.",
        worker=worker,
        run_id=run_id,
    )
    write_constraint_ledger(runtime._workspace_dir(worker["worker_id"]), ledger, run_id)
    stopped: list[str] = []
    terminated: list[str] = []
    runtime.sandbox.stop_screen_session = (  # type: ignore[method-assign]
        lambda worker_id, runtime_name, session_name, worker=None, missing_ok=False: stopped.append(session_name)
    )
    runtime.sandbox.terminate_run_processes = (  # type: ignore[method-assign]
        lambda worker_id, runtime_name, run_id, worker=None: terminated.append(run_id)
    )
    runtime.sandbox.inspect = lambda worker_id: type("SandboxInfo", (), {"pid": 4321, "state": "running"})()  # type: ignore[method-assign]

    recovered = runtime.collect_completed_run(worker, run_id=run_id)

    assert recovered is not None
    assert recovered["state"] == "completed"
    assert recovered["output_text"] == "Recovered result."
    assert (run_root / "exit_code").read_text() == "0"
    assert (runtime._workspace_dir(worker["worker_id"]) / "glasshive-run" / "runs" / run_id / "constraint-ledger.json").exists()
    evidence = json.loads((runtime._workspace_dir(worker["worker_id"]) / "glasshive-run" / "runs" / run_id / "evidence.json").read_text())
    assert evidence["evidence_result"]["status"] == "pass"
    assert stopped == [runtime._session_name_for_run_id(run_id)]
    assert terminated == [run_id]


def test_interrupt_worker_stops_exact_run_session_when_metadata_is_missing(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_test",
        "name": "Main Worker",
        "profile": "codex-cli",
        "model": "gpt-5.4",
        "state": "running",
    }
    runtime._ensure_dirs(worker["worker_id"])

    run_id = "run_123456789abc"
    run_root = runtime._run_root(worker["worker_id"], run_id)
    run_root.mkdir(parents=True, exist_ok=True)

    stopped: list[str] = []
    terminated: list[str] = []
    runtime.sandbox.list_screen_sessions = lambda worker_id, runtime_name, worker=None: [runtime._session_name_for_run_id(run_id)]  # type: ignore[method-assign]
    runtime.sandbox.stop_screen_session = (  # type: ignore[method-assign]
        lambda worker_id, runtime_name, session_name, worker=None, missing_ok=False: stopped.append(session_name)
    )
    runtime.sandbox.terminate_run_processes = (  # type: ignore[method-assign]
        lambda worker_id, runtime_name, run_id, worker=None: terminated.append(run_id)
    )
    runtime.sandbox.inspect = lambda worker_id: type("SandboxInfo", (), {"pid": 4321, "state": "running"})()  # type: ignore[method-assign]

    runtime.interrupt_worker(worker, run_id=run_id)
    assert stopped == [runtime._session_name_for_run_id(run_id)]
    assert terminated == [run_id]


def test_run_scoped_stop_reason_does_not_poison_later_run(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path))

    runtime._note_stop_reason("wrk_test", "terminated", run_id="run_old")
    runtime._finalize_stop_reason("wrk_test", run_id="run_new")

    with pytest.raises(WorkerTerminatedError):
        runtime._finalize_stop_reason("wrk_test", run_id="run_old")


def test_global_stop_reason_still_applies_to_current_run(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path))

    runtime._note_stop_reason("wrk_test", "terminated")

    with pytest.raises(WorkerTerminatedError):
        runtime._finalize_stop_reason("wrk_test", run_id="run_any")


def test_idle_worker_termination_does_not_poison_later_run(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path))
    runtime.sandbox.terminate = lambda worker_id: None  # type: ignore[method-assign]
    worker = {
        "worker_id": "wrk_test",
        "name": "Idle Worker",
        "profile": "codex-cli",
        "model": "gpt-5.4",
        "state": "ready",
    }

    runtime.terminate_worker(worker)

    runtime._finalize_stop_reason(worker["worker_id"], run_id="run_later")


def test_idle_worker_interrupt_does_not_poison_later_run(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path))
    runtime.sandbox.inspect = lambda worker_id: None  # type: ignore[method-assign]
    worker = {
        "worker_id": "wrk_test",
        "name": "Idle Worker",
        "profile": "codex-cli",
        "model": "gpt-5.4",
        "state": "ready",
    }

    runtime.interrupt_worker(worker)

    runtime._finalize_stop_reason(worker["worker_id"], run_id="run_later")


def test_worker_termination_reason_is_scoped_to_active_run(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path))
    runtime.sandbox.terminate = lambda worker_id: None  # type: ignore[method-assign]
    worker = {
        "worker_id": "wrk_test",
        "name": "Active Worker",
        "profile": "codex-cli",
        "model": "gpt-5.4",
        "state": "running",
        "_active_run_id": "run_active",
    }

    runtime.terminate_worker(worker)

    runtime._finalize_stop_reason(worker["worker_id"], run_id="run_later")
    with pytest.raises(WorkerTerminatedError):
        runtime._finalize_stop_reason(worker["worker_id"], run_id="run_active")


def test_host_codex_runtime_materializes_required_workspace_files(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = "/bin/echo"
    _patch_host_codex_requirement_probe(monkeypatch)
    xattr_calls = []

    def fake_run(args, **_kwargs):
        if "--version" in args:
            return subprocess.CompletedProcess(args, returncode=0, stdout="codex-cli 0.146.1\n", stderr="")
        xattr_calls.append(args)
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("workers_projects_runtime.profile_runtime.subprocess.run", fake_run)
    upload_source = tmp_path / "uploaded-brief.txt"
    upload_source.write_text("Uploaded brief")
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", str(tmp_path))
    worker = {
        "worker_id": "wrk_host",
        "name": "Main Host Worker",
        "role": "coding",
        "profile": "codex-cli",
        "execution_mode": "host",
        "alias": "Launch App",
        "workspace_root": str(tmp_path / "workspaces"),
        "bootstrap_bundle_json": json.dumps(
            {
                "project_definition": "# Project\n\nBuild the launch app.",
                "system_instructions": "Keep the operator informed through work-log.md.",
                "agents_md": "Agent context",
                "claude_md": "Claude context",
                "codex_md": "Codex context",
                "files": [
                    {
                        "scope": "workspace",
                        "path": "uploads/uploaded-brief.txt",
                        "source_path": str(upload_source),
                    }
                ],
            }
        ),
    }

    info = runtime.ensure_worker_ready(worker)
    workspace = tmp_path / "workspaces" / "codex"
    assert str(info.workspace_dir).startswith(str(workspace))
    workspace_dir = workspace / next(workspace.iterdir()).name
    assert (workspace_dir / "project-definition.md").read_text() == "# Project\n\nBuild the launch app."
    assert "main computer" in (workspace_dir / "harness-prompt.md").read_text()
    assert "bash /path/to/script.sh" in (workspace_dir / "harness-prompt.md").read_text()
    assert GLASSHIVE_CRITICAL_OPERATING_INSTRUCTIONS in (workspace_dir / "harness-prompt.md").read_text()
    assert GLASSHIVE_SAFETY_CHECKPOINT_RULE in (workspace_dir / "harness-prompt.md").read_text()
    assert (workspace_dir / "work-log.md").exists()
    agents_text = (workspace_dir / "AGENTS.md").read_text()
    assert "GlassHive Worker Contract" in agents_text
    assert GLASSHIVE_CRITICAL_OPERATING_INSTRUCTIONS in agents_text
    assert GLASSHIVE_SAFETY_CHECKPOINT_RULE in agents_text
    assert "Agent context" in agents_text
    assert "real local machine session" in agents_text
    assert (workspace_dir / "agents.md").read_text() == agents_text
    assert "@AGENTS.md" in (workspace_dir / "claude.md").read_text()
    assert "Claude context" in (workspace_dir / "claude.md").read_text()
    assert "Codex context" in (workspace_dir / "codex.md").read_text()
    assert (workspace_dir / "glasshive-host-tools" / "capture-front-window.sh").exists()
    content_hygiene = workspace_dir / "glasshive-host-tools" / "content-hygiene.py"
    assert content_hygiene.exists()
    assert "content-hygiene.py check" in (workspace_dir / "harness-prompt.md").read_text()
    assert xattr_calls
    assert xattr_calls[0][:3] == ["/usr/bin/xattr", "-d", "com.apple.quarantine"]
    assert (workspace_dir / "uploads" / "uploaded-brief.txt").read_text() == "Uploaded brief"
    assert (tmp_path / "data" / "host_codex_cli_runtime" / "workers" / "wrk_host" / "state" / "action-audit.jsonl").exists()


def test_host_runtime_content_hygiene_helper_strips_and_flags_page_chrome(tmp_path, monkeypatch):
    real_subprocess_run = subprocess.run
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = "/bin/echo"
    _patch_host_codex_requirement_probe(monkeypatch)

    def fake_run(args, **_kwargs):
        return subprocess.CompletedProcess(
            args,
            returncode=0,
            stdout="codex-cli 0.146.1\n" if "--version" in args else "",
            stderr="",
        )

    monkeypatch.setattr("workers_projects_runtime.profile_runtime.subprocess.run", fake_run)
    worker = {
        "worker_id": "wrk_host_hygiene",
        "name": "Host Hygiene Worker",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "workspaces"),
    }
    info = runtime.ensure_worker_ready(worker)
    workspace_dir = Path(info.workspace_dir)
    helper = workspace_dir / "glasshive-host-tools" / "content-hygiene.py"
    html_path = workspace_dir / "page.html"
    html_path.write_text(
        "<html><head><style>.nav{}</style><script>window.bad=true</script></head>"
        "<body><nav>Skip to Content</nav><button>MENU</button><button>CLOSE</button>"
        "<main><h1>Useful finding</h1>"
        "<p>AI workflow evidence for a regulated services business.</p></main></body></html>"
    )
    csv_path = workspace_dir / "output.csv"
    csv_path.write_text(
        "firm_name,sector_notes\n"
        "Example Capital,\"Skip to Content Cookie Settings window.bad=true\"\n"
        "Normal Capital,\"Value-creation function (post-closing) and first-wave outreach window.\"\n"
    )

    readable = real_subprocess_run(
        ["python3", str(helper), "readable", str(html_path)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "Useful finding" in readable
    assert "MENU" not in readable
    assert "CLOSE" not in readable
    assert "window.bad" not in readable

    checked = real_subprocess_run(
        ["python3", str(helper), "check", str(csv_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert checked.returncode == 1
    assert "failure_count" in checked.stdout
    assert "Skip to Content" in checked.stdout
    assert "function (post-closing)" not in checked.stdout
    assert "outreach window" not in checked.stdout
    assert "carry the user's source/date/auth/scope constraints forward exactly" in (
        workspace_dir / "harness-prompt.md"
    ).read_text()
    assert "source publication/evidence dates distinct from retrieval/access timestamps" in (
        workspace_dir / "harness-prompt.md"
    ).read_text()


def test_host_codex_model_can_differ_from_docker_provider_model(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    monkeypatch.setenv("WPR_MODEL_CODEX_CLI", "gpt-5.2-chat")
    monkeypatch.setenv("WPR_MODEL_HOST_CODEX_CLI", "gpt-5.4")

    assert runtime.resolve_model("codex-cli") == "gpt-5.4"


def test_host_codex_does_not_invent_automation_model_or_effort(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    monkeypatch.setenv("WPR_MODEL_CODEX_CLI", "gpt-5.4")
    monkeypatch.delenv("WPR_MODEL_HOST_CODEX_CLI", raising=False)
    monkeypatch.delenv("CODEX_MODEL", raising=False)
    monkeypatch.delenv("WPR_CODEX_CLI_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("WPR_CODEX_CLI_DEFAULT_REASONING_EFFORT", raising=False)
    monkeypatch.delenv("GLASSHIVE_HOST_CODEX_INHERIT_PROVIDER_MODEL", raising=False)
    worker = {
        "worker_id": "wrk_host_model_default",
        "name": "Main Host Worker",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "workspaces"),
    }

    command, _env = runtime._build_command(worker, "create the marker", runtime._host_runtime_info(worker))

    joined = "\n".join(command)
    assert runtime.resolve_model("codex-cli") == ""
    assert "-m" not in command
    assert "model_reasoning_effort" not in joined


def test_host_codex_can_explicitly_inherit_provider_model_when_configured(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    monkeypatch.setenv("WPR_MODEL_CODEX_CLI", "gpt-5.4")
    monkeypatch.setenv("GLASSHIVE_HOST_CODEX_INHERIT_PROVIDER_MODEL", "true")
    monkeypatch.delenv("WPR_MODEL_HOST_CODEX_CLI", raising=False)
    monkeypatch.delenv("CODEX_MODEL", raising=False)

    assert runtime.resolve_model("codex-cli") == "gpt-5.4"


def test_host_codex_honors_codex_model_env_before_local_config(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    monkeypatch.setenv("WPR_MODEL_CODEX_CLI", "gpt-5.4")
    monkeypatch.setenv("CODEX_MODEL", "gpt-5.5")
    monkeypatch.delenv("WPR_MODEL_HOST_CODEX_CLI", raising=False)

    assert runtime.resolve_model("codex-cli") == "gpt-5.5"


def test_host_codex_command_honors_per_run_reasoning_effort(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    monkeypatch.setenv("WPR_MODEL_HOST_CODEX_CLI", "gpt-5.4")
    monkeypatch.setenv("WPR_CODEX_CLI_XHIGH_ROUTE_PROVEN", "true")
    worker = {
        "worker_id": "wrk_host_effort",
        "name": "Host Effort Worker",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "workspaces"),
        "bootstrap_bundle_json": json.dumps({"env": {"WPR_CODEX_CLI_REASONING_EFFORT": "xhigh"}}),
    }

    command, _env = runtime._build_command(worker, "create the marker", runtime._host_runtime_info(worker))

    joined = "\n".join(command)
    assert 'model_reasoning_effort="xhigh"' in joined
    assert "-m\ngpt-5.4" in joined


def test_host_codex_command_projects_managed_bootstrap_tuple_and_ignores_user_config(
    tmp_path, monkeypatch
):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    monkeypatch.delenv("WPR_MODEL_HOST_CODEX_CLI", raising=False)
    monkeypatch.delenv("WPR_CODEX_CLI_REASONING_EFFORT", raising=False)
    monkeypatch.setenv("WPR_CODEX_CLI_XHIGH_ROUTE_PROVEN", "true")
    worker = {
        "worker_id": "wrk_host_effort_default",
        "name": "Host Effort Default Worker",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "workspaces"),
        "bootstrap_bundle_json": json.dumps(
            {
                "env": {
                    "WPR_MODEL_HOST_CODEX_CLI": "gpt-managed-test",
                    "WPR_CODEX_CLI_REASONING_EFFORT": "xhigh",
                    "WPR_CODEX_CLI_IGNORE_USER_CONFIG": "true",
                }
            }
        ),
    }

    command, _env = runtime._build_command(
        worker,
        "create the marker",
        runtime._host_runtime_info(worker),
    )

    joined = "\n".join(command)
    assert 'model_reasoning_effort="xhigh"' in joined
    assert "-m\ngpt-managed-test" in joined
    assert "--ignore-user-config" in command


def test_docker_codex_bootstrap_can_ignore_user_config_without_custom_provider(
    tmp_path, monkeypatch
):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_managed_bootstrap",
        "name": "Managed Worker",
        "profile": "codex-cli",
        "model": "gpt-test",
        "bootstrap_bundle_json": json.dumps(
            {"env": {"WPR_CODEX_CLI_IGNORE_USER_CONFIG": "true"}}
        ),
    }
    runtime._ensure_dirs(worker["worker_id"])
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("WPR_CODEX_CLI_BASE_URL", raising=False)
    monkeypatch.delenv("WPR_CODEX_CLI_IGNORE_USER_CONFIG", raising=False)

    command, _env = runtime._build_command(
        worker,
        "Create the artifact.",
        runtime._runtime_info(worker),
    )

    assert "--ignore-user-config" in command


def test_profiled_runtime_resolves_host_codex_model_by_execution_mode(tmp_path, monkeypatch):
    runtime = ProfiledWorkerRuntime(base_dir=str(tmp_path / "data"))
    monkeypatch.setenv("WPR_MODEL_CODEX_CLI", "gpt-5.2-chat")
    monkeypatch.setenv("WPR_MODEL_HOST_CODEX_CLI", "gpt-5.4")

    assert runtime.resolve_model("codex-cli", execution_mode="docker") == "gpt-5.2-chat"
    assert runtime.resolve_model("codex-cli", execution_mode="host") == "gpt-5.4"


def test_codex_cli_provider_config_honors_reasoning_effort_env(tmp_path, monkeypatch):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    monkeypatch.setenv("WPR_CODEX_CLI_BASE_URL", "https://provider.example.com/v1")
    monkeypatch.setenv("WPR_CODEX_CLI_REASONING_EFFORT", "xhigh")
    monkeypatch.setenv("WPR_CODEX_CLI_XHIGH_ROUTE_PROVEN", "true")

    command: list[str] = []
    runtime._append_codex_compatible_provider_config(command, {"worker_id": "wrk_effort"})

    joined = "\n".join(command)
    assert 'model_reasoning_effort="xhigh"' in joined


def test_codex_cli_provider_config_honors_per_run_reasoning_effort(tmp_path, monkeypatch):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    monkeypatch.setenv("WPR_CODEX_CLI_BASE_URL", "https://provider.example.com/v1")
    monkeypatch.setenv("WPR_CODEX_CLI_REASONING_EFFORT", "medium")
    monkeypatch.setenv("WPR_CODEX_CLI_XHIGH_ROUTE_PROVEN", "true")
    worker = {
        "worker_id": "wrk_effort",
        "bootstrap_bundle_json": json.dumps({"env": {"WPR_CODEX_CLI_REASONING_EFFORT": "xhigh"}}),
    }

    command: list[str] = []
    runtime._append_codex_compatible_provider_config(command, worker)

    joined = "\n".join(command)
    assert 'model_reasoning_effort="xhigh"' in joined
    assert 'model_reasoning_effort="medium"' not in joined


def test_codex_cli_provider_config_clamps_xhigh_without_route_proof(tmp_path, monkeypatch, caplog):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    monkeypatch.setenv("WPR_CODEX_CLI_BASE_URL", "https://provider.example.com/v1")
    monkeypatch.setenv("WPR_CODEX_CLI_REASONING_EFFORT", "xhigh")

    command: list[str] = []
    worker = {"worker_id": "wrk_effort", "profile": "codex-cli"}
    caplog.set_level(logging.WARNING, logger="workers_projects_runtime.profile_runtime")
    runtime._append_codex_compatible_provider_config(command, worker)

    joined = "\n".join(command)
    assert 'model_reasoning_effort="medium"' in joined
    assert 'model_reasoning_effort="xhigh"' not in joined
    assert worker["_effort_projection"] == {
        "requested": "xhigh",
        "effective": "medium",
        "allowed": ["high", "low", "medium", "none"],
        "route_proven": False,
        "fallback_reason": "xhigh_route_not_proven",
    }
    assert any(record.message == "Codex CLI reasoning effort clamped to provider-route fallback" for record in caplog.records)


def test_codex_cli_provider_config_disables_web_search_for_minimal_effort(tmp_path, monkeypatch):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    monkeypatch.setenv("WPR_CODEX_CLI_BASE_URL", "https://provider.example.com/v1")
    monkeypatch.setenv("WPR_CODEX_CLI_ALLOWED_REASONING_EFFORTS", "none,minimal,low,medium,high")
    worker = {
        "worker_id": "wrk_effort",
        "bootstrap_bundle_json": json.dumps({"env": {"WPR_CODEX_CLI_REASONING_EFFORT": "minimal"}}),
    }

    command: list[str] = []
    runtime._append_codex_compatible_provider_config(command, worker)

    joined = "\n".join(command)
    assert 'model_reasoning_effort="minimal"' in joined
    assert 'web_search="disabled"' in joined
    assert "--disable\nimage_generation" in joined
    assert "--disable\nweb_search" not in joined


def test_codex_cli_provider_config_clamps_minimal_without_route_allowlist(tmp_path, monkeypatch):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    monkeypatch.setenv("WPR_CODEX_CLI_BASE_URL", "https://provider.example.com/v1")
    worker = {
        "worker_id": "wrk_effort",
        "profile": "codex-cli",
        "bootstrap_bundle_json": json.dumps({"env": {"WPR_CODEX_CLI_REASONING_EFFORT": "minimal"}}),
    }

    command: list[str] = []
    runtime._append_codex_compatible_provider_config(command, worker)

    joined = "\n".join(command)
    assert 'model_reasoning_effort="medium"' in joined
    assert 'model_reasoning_effort="minimal"' not in joined
    assert 'web_search="disabled"' not in joined
    assert worker["_effort_projection"] == {
        "requested": "minimal",
        "effective": "medium",
        "allowed": ["high", "low", "medium", "none"],
        "route_proven": False,
        "fallback_reason": "requested_effort_not_allowed",
    }


def test_codex_cli_provider_config_supports_none_reasoning_effort(tmp_path, monkeypatch):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    monkeypatch.setenv("WPR_CODEX_CLI_BASE_URL", "https://provider.example.com/v1")
    worker = {
        "worker_id": "wrk_effort",
        "bootstrap_bundle_json": json.dumps({"env": {"WPR_CODEX_CLI_REASONING_EFFORT": "none"}}),
    }

    command: list[str] = []
    runtime._append_codex_compatible_provider_config(command, worker)

    joined = "\n".join(command)
    assert 'model_reasoning_effort="none"' in joined
    assert 'web_search="disabled"' not in joined


def test_codex_effort_projection_reports_requested_and_effective_values(tmp_path, monkeypatch):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_effort",
        "profile": "codex-cli",
        "bootstrap_bundle_json": json.dumps(
            {"env": {"WPR_CODEX_CLI_REASONING_EFFORT": "xhigh"}}
        ),
    }

    projection = runtime.effort_projection_for_worker(worker)

    assert projection["requested"] == "xhigh"
    assert projection["effective"] == "medium"
    assert projection["fallback_reason"] == "xhigh_route_not_proven"


def test_profiled_runtime_delegates_codex_effort_projection(tmp_path, monkeypatch):
    runtime = ProfiledWorkerRuntime(base_dir=str(tmp_path / "data"))
    monkeypatch.setenv("WPR_CODEX_CLI_XHIGH_ROUTE_PROVEN", "1")
    worker = {
        "worker_id": "wrk_effort",
        "profile": "codex-cli",
        "execution_mode": "host",
        "bootstrap_bundle_json": json.dumps(
            {"env": {"WPR_CODEX_CLI_REASONING_EFFORT": "xhigh"}}
        ),
    }

    projection = runtime.effort_projection_for_worker(worker)

    assert projection["requested"] == "xhigh"
    assert projection["effective"] == "xhigh"
    assert projection["fallback_reason"] == ""


def test_codex_cli_provider_config_coerces_unsupported_reasoning_effort(tmp_path, monkeypatch):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    monkeypatch.setenv("WPR_CODEX_CLI_BASE_URL", "https://provider.example.com/v1")
    monkeypatch.setenv("WPR_CODEX_CLI_ALLOWED_REASONING_EFFORTS", "medium")
    worker = {
        "worker_id": "wrk_effort",
        "bootstrap_bundle_json": json.dumps({"env": {"WPR_CODEX_CLI_REASONING_EFFORT": "minimal"}}),
    }

    command: list[str] = []
    runtime._append_codex_compatible_provider_config(command, worker)

    joined = "\n".join(command)
    assert 'model_reasoning_effort="medium"' in joined
    assert 'model_reasoning_effort="minimal"' not in joined
    assert 'web_search="disabled"' not in joined


def test_codex_cli_provider_config_coerces_high_effort_when_route_allows_medium_only(
    tmp_path,
    monkeypatch,
    caplog,
):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    monkeypatch.setenv("WPR_CODEX_CLI_BASE_URL", "https://provider.example.com/v1")
    monkeypatch.setenv("WPR_CODEX_CLI_ALLOWED_REASONING_EFFORTS", "medium")
    monkeypatch.setenv("WPR_CODEX_CLI_REASONING_EFFORT_FALLBACK", "medium")
    worker = {
        "worker_id": "wrk_effort",
        "profile": "codex-cli",
        "model": "gpt-5.2-chat",
        "bootstrap_bundle_json": json.dumps({"env": {"WPR_CODEX_CLI_REASONING_EFFORT": "high"}}),
    }

    command: list[str] = []
    caplog.set_level(logging.WARNING, logger="workers_projects_runtime.profile_runtime")
    runtime._append_codex_compatible_provider_config(command, worker)

    joined = "\n".join(command)
    assert 'model_reasoning_effort="medium"' in joined
    assert 'model_reasoning_effort="high"' not in joined
    clamp_records = [
        record
        for record in caplog.records
        if record.message == "Codex CLI reasoning effort clamped to provider-route fallback"
    ]
    assert len(clamp_records) == 1
    assert clamp_records[0].requested_effort == "high"
    assert clamp_records[0].effective_effort == "medium"
    assert clamp_records[0].allowed_efforts == "medium"


def test_codex_cli_provider_config_honors_reasoning_effort_fallback(tmp_path, monkeypatch):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    monkeypatch.setenv("WPR_CODEX_CLI_BASE_URL", "https://provider.example.com/v1")
    monkeypatch.setenv("WPR_CODEX_CLI_ALLOWED_REASONING_EFFORTS", "medium,high")
    monkeypatch.setenv("WPR_CODEX_CLI_REASONING_EFFORT_FALLBACK", "high")
    worker = {
        "worker_id": "wrk_effort",
        "bootstrap_bundle_json": json.dumps({"env": {"WPR_CODEX_CLI_REASONING_EFFORT": "minimal"}}),
    }

    command: list[str] = []
    runtime._append_codex_compatible_provider_config(command, worker)

    joined = "\n".join(command)
    assert 'model_reasoning_effort="high"' in joined
    assert 'model_reasoning_effort="minimal"' not in joined


def test_codex_cli_provider_config_ignores_invalid_allowed_reasoning_efforts(tmp_path, monkeypatch):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    monkeypatch.setenv("WPR_CODEX_CLI_BASE_URL", "https://provider.example.com/v1")
    monkeypatch.setenv("WPR_CODEX_CLI_ALLOWED_REASONING_EFFORTS", "banana")
    worker = {
        "worker_id": "wrk_effort",
        "bootstrap_bundle_json": json.dumps({"env": {"WPR_CODEX_CLI_REASONING_EFFORT": "low"}}),
    }

    command: list[str] = []
    runtime._append_codex_compatible_provider_config(command, worker)

    joined = "\n".join(command)
    assert 'model_reasoning_effort="low"' in joined


def test_host_cli_run_gives_supervisor_private_instruction_file(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = "/bin/echo"
    _patch_host_codex_requirement_probe(monkeypatch)
    captured: dict[str, object] = {}

    class FakeProcess:
        pid = 12345
        returncode = 0

        def __init__(self, command, **kwargs):
            _mark_fake_host_supervisor_ready(list(command), self.pid)
            captured["stdin"] = kwargs.get("stdin")
            captured["command"] = list(command)
            stdout = kwargs["stdout"]
            stdout.write(
                '{"type":"item.completed","item":{"type":"agent_message","text":"FINAL REPORT:\\nDone"}}\n'
            )
            stdout.flush()

        def wait(self, timeout=None):
            return 0

        def communicate(self, input=None, timeout=None):
            raise AssertionError("API process must not own supervisor stdin")

        def poll(self):
            return 0

    monkeypatch.setattr(
        "workers_projects_runtime.profile_runtime.subprocess.run",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args,
            returncode=0,
            stdout="codex-cli 0.146.1\n" if "--version" in args else "",
            stderr="",
        ),
    )
    monkeypatch.setattr("workers_projects_runtime.profile_runtime.subprocess.Popen", FakeProcess)
    worker = {
        "worker_id": "wrk_no_stdin",
        "name": "No stdin Worker",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "workspaces"),
    }

    assert runtime.run_task(worker, "create marker", run_id="run_no_stdin") == "Done"
    assert captured["stdin"] is subprocess.DEVNULL
    supervisor_stdin = Path(captured["command"][6])  # type: ignore[index]
    assert supervisor_stdin.stat().st_mode & 0o777 == 0o600
    assert supervisor_stdin.read_text().startswith("create marker")


def test_host_cli_run_writes_constraint_ledger_and_evidence(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = "/bin/echo"
    _patch_host_codex_requirement_probe(monkeypatch)
    workspace = tmp_path / "workspace"

    class FakeProcess:
        pid = 12345
        returncode = 0

        def __init__(self, command, **kwargs):
            _mark_fake_host_supervisor_ready(list(command), self.pid)
            cwd = Path(kwargs["cwd"])
            output_dir = cwd / "output"
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "result.csv").write_text("name,status\nsynthetic,ok\n")
            stdout = kwargs["stdout"]
            stdout.write(
                '{"type":"item.completed","item":{"type":"agent_message","text":"FINAL REPORT:\\nDone"}}\n'
            )
            stdout.flush()

        def wait(self, timeout=None):
            return 0

        def communicate(self, input=None, timeout=None):
            return None, None

        def poll(self):
            return 0

    monkeypatch.setattr(
        "workers_projects_runtime.profile_runtime.subprocess.run",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args,
            returncode=0,
            stdout="codex-cli 0.146.1\n" if "--version" in args else "",
            stderr="",
        ),
    )
    monkeypatch.setattr("workers_projects_runtime.profile_runtime.subprocess.Popen", FakeProcess)
    worker = {
        "worker_id": "wrk_evidence",
        "name": "Evidence Worker",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_dir": str(workspace),
    }

    result = runtime.run_task(
        worker,
        "Use sources from January 2024 through May 2026 only.\nDeliver a CSV report.",
        run_id="run_evidence",
    )

    assert result == "Done"
    ledger = json.loads((workspace / "glasshive-run" / "constraint-ledger.json").read_text())
    evidence = json.loads((workspace / "glasshive-run" / "evidence.json").read_text())
    active_status = json.loads((workspace / "glasshive-run" / "runs" / "run_evidence" / "active-run.json").read_text())
    assert ledger["run_id"] == "run_evidence"
    assert any("May 2026" in item for item in ledger["constraints"]["date"])
    assert evidence["run_id"] == "run_evidence"
    assert evidence["worker"]["profile"] == "codex-cli"
    assert evidence["final_output"]["has_final_report"] is True
    assert "output/result.csv" in {item["path"] for item in evidence["artifacts"]["items"]}
    assert "glasshive-run/constraint-ledger.json" not in {item["path"] for item in evidence["artifacts"]["items"]}
    assert active_status["state"] == "completed"
    assert active_status["run_id"] == "run_evidence"
    assert active_status["process_pid"] == 12345
    assert active_status["transcript_paths"]["stdout"].endswith("/stdout.log")
    assert active_status["evidence_path"] == "glasshive-run/evidence.json"


def test_host_cli_run_fails_when_evidence_contract_fails(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = "/bin/echo"
    _patch_host_codex_requirement_probe(monkeypatch)
    workspace = tmp_path / "workspace"

    class FakeProcess:
        pid = 12345
        returncode = 0

        def __init__(self, command, **kwargs):
            _mark_fake_host_supervisor_ready(list(command), self.pid)
            stdout = kwargs["stdout"]
            stdout.write(
                '{"type":"item.completed","item":{"type":"agent_message","text":"FINAL REPORT:\\nDone"}}\n'
            )
            stdout.flush()

        def wait(self, timeout=None):
            return 0

        def communicate(self, input=None, timeout=None):
            return None, None

        def poll(self):
            return 0

    monkeypatch.setattr(
        "workers_projects_runtime.profile_runtime.subprocess.run",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args,
            returncode=0,
            stdout="codex-cli 0.146.1\n" if "--version" in args else "",
            stderr="",
        ),
    )
    monkeypatch.setattr("workers_projects_runtime.profile_runtime.subprocess.Popen", FakeProcess)
    worker = {
        "worker_id": "wrk_evidence_fail",
        "name": "Evidence Fail Worker",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_dir": str(workspace),
    }

    with pytest.raises(RuntimeErrorBase, match="GlassHive evidence check failed"):
        runtime.run_task(worker, "Deliver a PDF report.", run_id="run_evidence_fail")

    evidence = json.loads((workspace / "glasshive-run" / "evidence.json").read_text())
    active_status = json.loads((workspace / "glasshive-run" / "runs" / "run_evidence_fail" / "active-run.json").read_text())
    assert evidence["evidence_result"]["status"] == "fail"
    assert evidence["completion_compliance"]["missing_required_artifact_types"] == ["pdf"]
    assert active_status["state"] == "failed"
    assert active_status["stop_reason"] == "evidence_check_failed"


def test_host_cli_timeout_writes_truthful_evidence(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = "/bin/echo"
    recorded_metrics: list[tuple[str, str, str]] = []

    def record_metrics(worker_id, run_id, stdout):
        recorded_metrics.append((worker_id, run_id, stdout))
        return {}, {}

    runtime._record_run_metrics = record_metrics  # type: ignore[method-assign]
    _patch_host_codex_requirement_probe(monkeypatch)
    workspace = tmp_path / "workspace"

    class TimeoutProcess:
        pid = 12345

        def __init__(self, command, **kwargs):
            _mark_fake_host_supervisor_ready(list(command), self.pid)
            self.terminated = False
            stdout = kwargs["stdout"]
            stdout.write("working before timeout\n")
            stdout.flush()

        def wait(self, timeout=None):
            if self.terminated:
                return 130
            raise subprocess.TimeoutExpired(["fake-codex"], timeout)

        def communicate(self, input=None, timeout=None):
            self.wait(timeout=timeout)
            return None, None

        def poll(self):
            return 130 if self.terminated else None

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.terminated = True

    monkeypatch.setattr(
        "workers_projects_runtime.profile_runtime.subprocess.run",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args,
            returncode=0,
            stdout="codex-cli 0.146.1\n" if "--version" in args else "",
            stderr="",
        ),
    )
    monkeypatch.setattr("workers_projects_runtime.profile_runtime.subprocess.Popen", TimeoutProcess)
    worker = {
        "worker_id": "wrk_timeout_evidence",
        "name": "Timeout Evidence Worker",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_dir": str(workspace),
    }

    with pytest.raises(RuntimeErrorBase, match="timed out"):
        runtime.run_task(worker, "Do long work.", timeout_sec=0.01, run_id="run_timeout_evidence")

    evidence = json.loads((workspace / "glasshive-run" / "evidence.json").read_text())
    active_status = json.loads((workspace / "glasshive-run" / "runs" / "run_timeout_evidence" / "active-run.json").read_text())
    assert evidence["run_id"] == "run_timeout_evidence"
    assert evidence["exit_code"] is None
    assert evidence["timeout"]["exit_source"] == "timeout"
    assert evidence["timeout"]["stop_reason"] == "timeout"
    assert evidence["transcript"]["stdout_tail"].strip() == "working before timeout"
    assert evidence["transcript"]["metadata"]["stdout"]["exists"] is True
    assert evidence["transcript"]["metadata"]["stdout"]["bytes"] > 0
    assert evidence["final_output"]["status"] == "failed"
    assert active_status["state"] == "timeout"
    assert active_status["stop_reason"] == "timeout"
    assert active_status["timeout_seconds"] == 0.01
    assert active_status["heartbeat_sequence"] >= 1
    assert active_status["transcript_progress"]["files"]["stdout"]["bytes"] > 0
    assert active_status["transcript_progress"]["last_output_at"]
    assert active_status["transcript_progress"]["quiet_seconds"] is not None
    assert recorded_metrics == [
        ("wrk_timeout_evidence", "run_timeout_evidence", "working before timeout\n")
    ]


def test_host_cli_timeout_preserves_foreground_server_transcript(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = "/bin/echo"
    _patch_host_codex_requirement_probe(monkeypatch)
    workspace = tmp_path / "workspace"

    class ForegroundServerProcess:
        pid = 12345

        def __init__(self, command, **kwargs):
            _mark_fake_host_supervisor_ready(list(command), self.pid)
            self.terminated = False
            stdout = kwargs["stdout"]
            stderr = kwargs["stderr"]
            stdout.write("Serving HTTP on 127.0.0.1 port 8000 ...\n")
            stderr.write("OSError: [Errno 48] Address already in use\n")
            stdout.flush()
            stderr.flush()

        def wait(self, timeout=None):
            if self.terminated:
                return 130
            raise subprocess.TimeoutExpired(["fake-codex"], timeout)

        def communicate(self, input=None, timeout=None):
            self.wait(timeout=timeout)
            return None, None

        def poll(self):
            return 130 if self.terminated else None

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.terminated = True

    monkeypatch.setattr(
        "workers_projects_runtime.profile_runtime.subprocess.run",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args,
            returncode=0,
            stdout="codex-cli 0.146.1\n" if "--version" in args else "",
            stderr="",
        ),
    )
    monkeypatch.setattr("workers_projects_runtime.profile_runtime.subprocess.Popen", ForegroundServerProcess)
    worker = {
        "worker_id": "wrk_foreground_server_evidence",
        "name": "Foreground Server Evidence Worker",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_dir": str(workspace),
    }

    with pytest.raises(RuntimeErrorBase, match="timed out"):
        runtime.run_task(worker, "Create and inspect a local HTML artifact.", timeout_sec=0.01, run_id="run_foreground_server_evidence")

    evidence = json.loads((workspace / "glasshive-run" / "evidence.json").read_text())
    active_status = json.loads(
        (workspace / "glasshive-run" / "runs" / "run_foreground_server_evidence" / "active-run.json").read_text()
    )
    assert evidence["timeout"]["exit_source"] == "timeout"
    assert "Serving HTTP" in evidence["transcript"]["stdout_tail"]
    assert "Address already in use" in evidence["transcript"]["stderr_tail"]
    assert evidence["transcript"]["metadata"]["stderr"]["bytes"] > 0
    assert evidence["final_output"]["status"] == "failed"
    assert active_status["state"] == "timeout"
    assert active_status["transcript_progress"]["files"]["stdout"]["bytes"] > 0
    assert active_status["transcript_progress"]["files"]["stderr"]["bytes"] > 0
    assert active_status["transcript_progress"]["files"]["stdout"]["tail_sha256"]
    assert active_status["transcript_progress"]["last_output_at"]


def test_host_codex_run_sends_instruction_via_stdin_not_argv(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = "/bin/echo"
    _patch_host_codex_requirement_probe(monkeypatch)
    workspace = tmp_path / "workspace"
    captured: dict[str, object] = {}

    class StdinProcess:
        pid = 12345
        returncode = 0

        def __init__(self, command, **kwargs):
            _mark_fake_host_supervisor_ready(list(command), self.pid)
            captured["command"] = list(command)
            captured["stdin_is_devnull"] = kwargs["stdin"] == subprocess.DEVNULL
            stdout = kwargs["stdout"]
            stdout.write(
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "FINAL REPORT:\nDone."},
                    }
                )
                + "\n"
            )
            stdout.flush()

        def communicate(self, input=None, timeout=None):
            raise AssertionError("API process must not own supervisor stdin")

        def wait(self, timeout=None):
            return self.returncode

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 130

        def kill(self):
            self.returncode = 130

    monkeypatch.setattr(
        "workers_projects_runtime.profile_runtime.subprocess.run",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args,
            returncode=0,
            stdout="codex-cli 0.146.1\n" if "--version" in args else "",
            stderr="",
        ),
    )
    monkeypatch.setattr("workers_projects_runtime.profile_runtime.subprocess.Popen", StdinProcess)
    worker = {
        "worker_id": "wrk_stdin_privacy",
        "name": "Stdin Privacy Worker",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_dir": str(workspace),
    }

    output = runtime.run_task(worker, "Sensitive private instruction.", timeout_sec=5, run_id="run_stdin_privacy")

    assert output == "Done."
    command_text = " ".join(captured["command"])  # type: ignore[arg-type]
    assert "Sensitive private instruction" not in command_text
    assert str(captured["command"][-1]) == "-"  # type: ignore[index]
    assert captured["stdin_is_devnull"] is True
    supervisor_stdin = Path(captured["command"][6])  # type: ignore[index]
    assert supervisor_stdin.stat().st_mode & 0o777 == 0o600
    assert supervisor_stdin.read_text().startswith("Sensitive private instruction.")
    evidence = json.loads((workspace / "glasshive-run" / "evidence.json").read_text())
    assert all("Sensitive private instruction" not in arg for arg in evidence["command"]["argv_redacted"])
    assert evidence["command"]["argv_redacted"][0] == "echo"
    assert "/bin/echo" not in evidence["command"]["display_redacted"]


def test_host_cli_interrupt_writes_run_evidence(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = "/bin/echo"
    recorded_metrics: list[tuple[str, str, str]] = []

    def record_metrics(worker_id, run_id, stdout):
        recorded_metrics.append((worker_id, run_id, stdout))
        return {}, {}

    runtime._record_run_metrics = record_metrics  # type: ignore[method-assign]
    _patch_host_codex_requirement_probe(monkeypatch)
    workspace = tmp_path / "workspace"
    processes: list[object] = []

    class BlockingProcess:
        pid = 12345

        def __init__(self, command, **kwargs):
            _mark_fake_host_supervisor_ready(list(command), self.pid)
            self.terminated = False
            processes.append(self)
            stdout = kwargs["stdout"]
            stdout.write("working before interrupt\n")
            stdout.write("debug path /Users/example/private-workspace/tmp/preview.png\n")
            stdout.flush()

        def wait(self, timeout=None):
            deadline = time.time() + 2
            while not self.terminated and time.time() < deadline:
                time.sleep(0.01)
            if self.terminated:
                return -15
            raise subprocess.TimeoutExpired(["fake-codex"], timeout)

        def communicate(self, input=None, timeout=None):
            self.wait(timeout=timeout)
            return None, None

        def poll(self):
            return -15 if self.terminated else None

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.terminated = True

    def fake_killpg(_pgid, _signal):
        for process in processes:
            process.terminate()

    monkeypatch.setattr(
        "workers_projects_runtime.profile_runtime.subprocess.run",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args,
            returncode=0,
            stdout="codex-cli 0.146.1\n" if "--version" in args else "",
            stderr="",
        ),
    )
    monkeypatch.setattr("workers_projects_runtime.profile_runtime.subprocess.Popen", BlockingProcess)
    monkeypatch.setattr("workers_projects_runtime.profile_runtime.os.getpgid", lambda pid: pid)
    monkeypatch.setattr("workers_projects_runtime.profile_runtime.os.killpg", fake_killpg)
    worker = {
        "worker_id": "wrk_interrupt_evidence",
        "name": "Interrupt Evidence Worker",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_dir": str(workspace),
    }
    errors: list[Exception] = []

    def run_worker():
        try:
            runtime.run_task(
                worker,
                "Do long work.\n" + ("synthetic sensitive segment " * 80),
                timeout_sec=60,
                run_id="run_interrupt_evidence",
            )
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=run_worker)
    thread.start()
    deadline = time.time() + 2
    while runtime._read_active_session(worker["worker_id"]) is None and time.time() < deadline:
        time.sleep(0.01)

    runtime.interrupt_worker(worker, run_id="run_interrupt_evidence")
    thread.join(timeout=3)

    assert not thread.is_alive()
    assert errors
    evidence = json.loads((workspace / "glasshive-run" / "evidence.json").read_text())
    active_status = json.loads((workspace / "glasshive-run" / "runs" / "run_interrupt_evidence" / "active-run.json").read_text())
    assert evidence["run_id"] == "run_interrupt_evidence"
    assert evidence["final_output"]["status"] == "failed"
    assert evidence["timeout"]["seconds"] == 60
    assert "working before interrupt" in evidence["transcript"]["stdout_tail"]
    assert "[REDACTED_LOCAL_PATH]" in evidence["transcript"]["stdout_tail"]
    assert "/Users/example" not in evidence["transcript"]["stdout_tail"]
    assert evidence["transcript"]["metadata"]["stdout"]["exists"] is True
    assert evidence["artifacts"]["count"] == 0
    display = evidence["command"]["display_redacted"]
    assert "synthetic sensitive segment" not in display
    assert display.endswith(" -")
    assert active_status["state"] == "interrupted"
    assert active_status["stop_reason"] in {"interrupted", "WorkerInterruptedError"}
    assert recorded_metrics
    assert all(
        item[:2] == ("wrk_interrupt_evidence", "run_interrupt_evidence")
        for item in recorded_metrics
    )
    assert "working before interrupt" in recorded_metrics[-1][2]


def test_host_codex_runtime_default_prompts_require_final_report(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = "/bin/echo"
    _patch_host_codex_requirement_probe(monkeypatch)

    def fake_run(args, **_kwargs):
        return subprocess.CompletedProcess(
            args,
            returncode=0,
            stdout="codex-cli 0.146.1\n" if "--version" in args else "",
            stderr="",
        )

    monkeypatch.setattr("workers_projects_runtime.profile_runtime.subprocess.run", fake_run)
    worker = {
        "worker_id": "wrk_host_final_report",
        "name": "Main Host Worker",
        "role": "browser task",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "workspaces"),
    }

    info = runtime.ensure_worker_ready(worker)
    workspace_dir = Path(info.workspace_dir)

    for filename in ("harness-prompt.md", "agents.md", "AGENTS.md", "claude.md", "CLAUDE.md", "codex.md", "CODEX.md"):
        content = (workspace_dir / filename).read_text()
        assert "FINAL REPORT:" in content
        assert "inspect" in content.lower()
        assert "request and success criteria" in content.lower()
        if filename in {"harness-prompt.md", "agents.md", "AGENTS.md"}:
            assert GLASSHIVE_CRITICAL_OPERATING_INSTRUCTIONS in content
            assert GLASSHIVE_SAFETY_CHECKPOINT_RULE in content
    assert "canonical project instruction source" in (workspace_dir / "CLAUDE.md").read_text()
    assert "@AGENTS.md" in (workspace_dir / "CLAUDE.md").read_text()


def test_host_runtime_materializes_project_mcp_bootstrap_with_owner_only_files(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = "/bin/echo"
    _patch_host_codex_requirement_probe(monkeypatch)
    source_codex_home = tmp_path / "source-codex-home"
    source_codex_home.mkdir()
    (source_codex_home / "skills" / "synthetic-skill").mkdir(parents=True)
    (source_codex_home / "skills" / "synthetic-skill" / "SKILL.md").write_text(
        "# Synthetic skill\n"
    )
    (source_codex_home / "auth.json").write_text('{"OPENAI_API_KEY":"redacted-test-key"}')
    (source_codex_home / "config.toml").write_text(
        'model = "gpt-local-public-safe"\n'
        'model_provider = "local_provider"\n\n'
        '[model_providers.local_provider]\n'
        'name = "Local Provider"\n'
        'base_url = "https://models.example.test/v1"\n\n'
        '[plugins."computer-use@openai-bundled"]\n'
        "enabled = true\n\n"
        "[mcp_servers.private-mail]\n"
        "url = \"https://private.example.test/mcp\"\n"
        "bearer_token_env_var = \"PRIVATE_TOKEN\"\n\n"
        "[mcp_servers.node_repl]\n"
        "command = \"/Applications/Codex.app/Contents/Resources/cua_node/bin/node_repl\"\n"
        "args = []\n\n"
        "[mcp_servers.node_repl.env]\n"
        "NODE_REPL_TRUSTED_CODE_PATHS = \"/tmp/public-safe\"\n"
    )
    computer_use_manifest = (
        source_codex_home
        / "plugins"
        / "cache"
        / "openai-bundled"
        / "computer-use"
        / "1.0.0"
        / ".mcp.json"
    )
    computer_use_manifest.parent.mkdir(parents=True)
    computer_use_manifest.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "computer-use": {
                        "command": "./Codex Computer Use.app/Contents/SharedSupport/SkyComputerUseClient.app/Contents/MacOS/SkyComputerUseClient",
                        "args": ["mcp"],
                        "cwd": ".",
                    }
                }
            }
        )
    )
    monkeypatch.setenv("CODEX_HOME", str(source_codex_home))

    def fake_run(args, **_kwargs):
        return subprocess.CompletedProcess(
            args,
            returncode=0,
            stdout="codex-cli 0.146.1\n" if "--version" in args else "",
            stderr="",
        )

    monkeypatch.setattr("workers_projects_runtime.profile_runtime.subprocess.run", fake_run)
    worker = {
        "worker_id": "wrk_host_mcp_bootstrap",
        "name": "Brokered Host Worker",
        "role": "connected account task",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "workspaces"),
        "bootstrap_bundle_json": json.dumps(
            {
                "claude_project_mcp": {
                    "glasshive-user-capabilities": {
                        "type": "http",
                        "transport": "http",
                        "url": "http://127.0.0.1:3080/api/viventium/glasshive/capabilities/mcp",
                        "headers": {"Authorization": f"{'Bearer'} broker-grant"},
                    }
                },
                "claude_settings_local": {"permissions": {"allow": ["Bash(ls *)"]}},
                "codex_config_append": (
                    "[mcp_servers.glasshive-user-capabilities]\n"
                    "url = \"http://127.0.0.1:3080/api/viventium/glasshive/capabilities/mcp\"\n"
                    "bearer_token_env_var = \"GLASSHIVE_CAPABILITY_BROKER_TOKEN\""
                ),
                "env": {"GLASSHIVE_CAPABILITY_BROKER_TOKEN": "broker-grant"},
            }
        ),
    }

    info = runtime.ensure_worker_ready(worker)
    workspace_dir = Path(info.workspace_dir)

    mcp_text = (workspace_dir / ".mcp.json").read_text()
    assert "broker-grant" not in mcp_text
    assert json.loads(mcp_text)["mcpServers"]["glasshive-user-capabilities"]["headers"]["Authorization"] == "Bearer ${GLASSHIVE_CAPABILITY_BROKER_TOKEN}"
    assert json.loads((workspace_dir / ".claude" / "settings.local.json").read_text())["permissions"]["allow"] == ["Bash(ls *)"]
    worker_codex_home = runtime._host_codex_home(worker)
    workspace_codex_config = (workspace_dir / ".codex" / "config.toml").read_text()
    worker_codex_config = (worker_codex_home / "config.toml").read_text()
    assert "glasshive-user-capabilities" in workspace_codex_config
    assert "glasshive-user-capabilities" in worker_codex_config
    assert 'model = "gpt-local-public-safe"' in worker_codex_config
    assert 'model_provider = "local_provider"' in worker_codex_config
    assert "[model_providers.local_provider]" in worker_codex_config
    assert '[plugins."computer-use@openai-bundled"]' in worker_codex_config
    assert "mcp_servers.node_repl" in worker_codex_config
    assert "mcp_servers.node_repl.env" in worker_codex_config
    assert "mcp_servers.computer-use" in worker_codex_config
    assert str(computer_use_manifest.parent) in worker_codex_config
    assert "private-mail" not in worker_codex_config
    assert "PRIVATE_TOKEN" not in worker_codex_config
    assert json.loads((worker_codex_home / "auth.json").read_text())["OPENAI_API_KEY"] == "redacted-test-key"
    assert (worker_codex_home / "skills").is_symlink()
    assert (worker_codex_home / "skills").resolve() == (source_codex_home / "skills").resolve()
    assert (worker_codex_home / "plugins" / "cache").is_symlink()
    assert (worker_codex_home / "plugins" / "cache").resolve() == (
        source_codex_home / "plugins" / "cache"
    ).resolve()
    assert not (worker_codex_home / "plugins" / "data").exists()
    command, env = runtime._build_command(worker, "Use the broker", info)
    assert env["CODEX_HOME"] == str(worker_codex_home)
    assert env["GLASSHIVE_CAPABILITY_BROKER_TOKEN"] == "broker-grant"
    assert "broker-grant" not in " ".join(command)
    assert stat.S_IMODE((workspace_dir / ".mcp.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((workspace_dir / ".claude" / "settings.local.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((workspace_dir / ".codex" / "config.toml").stat().st_mode) == 0o600
    assert stat.S_IMODE((worker_codex_home / "config.toml").stat().st_mode) == 0o600
    assert stat.S_IMODE((worker_codex_home / "auth.json").stat().st_mode) == 0o600


def test_host_codex_preserves_known_computer_use_client_when_manifest_is_absent(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    source_codex_home = tmp_path / "source-codex-home"
    computer_use_client = (
        source_codex_home
        / "computer-use"
        / "Codex Computer Use.app"
        / "Contents"
        / "SharedSupport"
        / "SkyComputerUseClient.app"
        / "Contents"
        / "MacOS"
        / "SkyComputerUseClient"
    )
    computer_use_client.parent.mkdir(parents=True)
    computer_use_client.write_text("#!/usr/bin/env bash\n")
    computer_use_client.chmod(0o755)
    monkeypatch.setenv("CODEX_HOME", str(source_codex_home))

    config = runtime._host_codex_worker_config(
        "[mcp_servers.glasshive-user-capabilities]\n"
        "url = \"http://127.0.0.1:3190/api/viventium/glasshive/capabilities/mcp\"\n"
        "bearer_token_env_var = \"GLASSHIVE_CAPABILITY_BROKER_TOKEN\""
    )

    assert "[mcp_servers.computer-use]" in config
    assert str(computer_use_client) in config
    assert "glasshive-user-capabilities" in config


def test_host_codex_conversation_developer_instructions_are_exact_and_worker_local(
    tmp_path, monkeypatch
):
    source_codex_home = tmp_path / "source-codex-home"
    source_codex_home.mkdir()
    (source_codex_home / "config.toml").write_text(
        'developer_instructions = "Stale inherited instructions."\n'
    )
    monkeypatch.setenv("CODEX_HOME", str(source_codex_home))
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "codex-state"))
    life = tmp_path / "Life"
    life.mkdir()
    worker = {
        "worker_id": "wrk_codex_developer_authority",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(life),
        "bootstrap_bundle_json": json.dumps(
            {
                "run_mode": "conversation",
                "developer_instructions": "Current Feeling capsule.",
            }
        ),
    }

    runtime._materialize_workspace(worker, life)
    worker_config_path = runtime._host_codex_home(worker) / "config.toml"
    worker_config = tomllib.loads(worker_config_path.read_text())
    assert worker_config["developer_instructions"] == "Current Feeling capsule."
    assert not (life / ".codex").exists()

    worker_config_path.write_text(
        'developer_instructions = "Stale inherited instructions."\n'
    )
    with pytest.raises(RuntimeErrorBase, match="developer instruction authority"):
        runtime._build_command(
            worker,
            "Continue.",
            runtime._host_runtime_info(worker),
        )


def test_host_codex_nonconversation_worker_keeps_inherited_developer_instructions(
    tmp_path, monkeypatch
):
    source_codex_home = tmp_path / "source-codex-home"
    source_codex_home.mkdir()
    (source_codex_home / "config.toml").write_text(
        'developer_instructions = "Standalone instructions."\n'
    )
    monkeypatch.setenv("CODEX_HOME", str(source_codex_home))
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "codex-state"))

    config = tomllib.loads(runtime._host_codex_worker_config(""))

    assert config["developer_instructions"] == "Standalone instructions."


def test_host_codex_plugin_denylist_and_personality_are_worker_local(
    tmp_path, monkeypatch
):
    denied_plugin = "viventium-feelings@project-viventium"
    source_codex_home = tmp_path / "source-codex-home"
    source_codex_home.mkdir()
    source_config = source_codex_home / "config.toml"
    source_config.write_text(
        'personality = "pragmatic"\n'
        f'[plugins."{denied_plugin}"]\n'
        "enabled = true\n\n"
        '[plugins."chrome@openai-bundled"]\n'
        "enabled = true\n"
    )
    monkeypatch.setenv("CODEX_HOME", str(source_codex_home))
    monkeypatch.setenv("GLASSHIVE_HOST_PLUGIN_DENYLIST", denied_plugin)
    monkeypatch.setenv("WPR_CODEX_CLI_PERSONALITY", "none")
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "codex-state"))

    worker_config = tomllib.loads(runtime._host_codex_worker_config(""))
    source_after = tomllib.loads(source_config.read_text())

    assert worker_config["personality"] == "none"
    assert worker_config["plugins"][denied_plugin]["enabled"] is False
    assert worker_config["plugins"]["chrome@openai-bundled"]["enabled"] is True
    assert source_after["personality"] == "pragmatic"
    assert source_after["plugins"][denied_plugin]["enabled"] is True


def test_host_codex_strips_noncanonical_private_mcp_tables(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    source_codex_home = tmp_path / "source-codex-home"
    source_codex_home.mkdir()
    (source_codex_home / "config.toml").write_text(
        'model = "gpt-local-public-safe"\n'
        'model_provider = "local_provider"\n\n'
        '[model_providers.local_provider]\n'
        'base_url = "https://models.example.test/v1"\n\n'
        "[mcp_servers]\n"
        'private_mail = { command = "/bin/private-mail", env = { PRIVATE_TOKEN = "secret" } }\n'
        'node_repl = { command = "/bin/node-repl", args = [] }\n'
        '"computer-use" = { command = "/bin/computer-use", args = ["mcp"] }\n'
        '\n[projects."/tmp/\U0001f4a1"]\n'
        'trust_level = "trusted"\n'
    )
    monkeypatch.setenv("CODEX_HOME", str(source_codex_home))

    config = runtime._host_codex_worker_config(
        "[mcp_servers.glasshive-user-capabilities]\n"
        "url = \"http://127.0.0.1:3190/api/viventium/glasshive/capabilities/mcp\"\n"
        "bearer_token_env_var = \"GLASSHIVE_CAPABILITY_BROKER_TOKEN\""
    )

    assert 'model = "gpt-local-public-safe"' in config
    assert "[model_providers.local_provider]" in config
    assert "[projects.\"/tmp/\U0001f4a1\"]" in config
    assert "\\ud" not in config.lower()
    assert "[mcp_servers.node_repl]" in config
    assert "[mcp_servers.computer-use]" in config
    assert "glasshive-user-capabilities" in config
    assert "private_mail" not in config
    assert "PRIVATE_TOKEN" not in config
    assert "secret" not in config


def test_host_codex_malformed_config_strips_inline_private_mcp_tables(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    source_codex_home = tmp_path / "source-codex-home"
    source_codex_home.mkdir()
    (source_codex_home / "config.toml").write_text(
        'model = "gpt-local-public-safe"\n\n'
        "[mcp_servers]\n"
        'private_mail = { command = "/bin/private-mail", env = { PRIVATE_TOKEN = "secret" }\n'
        'node_repl = { command = "/bin/node-repl", args = [] }\n\n'
        "[mcp_servers.computer-use]\n"
        'command = "/bin/computer-use"\n'
        'args = ["mcp"]\n\n'
        "[projects.example]\n"
        'trust_level = "trusted"\n'
    )
    monkeypatch.setenv("CODEX_HOME", str(source_codex_home))

    config = runtime._host_codex_worker_config(
        "[mcp_servers.glasshive-user-capabilities]\n"
        "url = \"http://127.0.0.1:3190/api/viventium/glasshive/capabilities/mcp\"\n"
        "bearer_token_env_var = \"GLASSHIVE_CAPABILITY_BROKER_TOKEN\""
    )

    assert 'model = "gpt-local-public-safe"' in config
    assert "[projects.example]" in config
    assert "[mcp_servers.computer-use]" in config
    assert "glasshive-user-capabilities" in config
    assert "[mcp_servers]" not in config
    assert "private_mail" not in config
    assert "PRIVATE_TOKEN" not in config
    assert "secret" not in config


def test_host_runtime_live_description_refreshes_stale_prompt_files(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = "/bin/echo"
    _patch_host_codex_requirement_probe(monkeypatch)

    def fake_run(args, **_kwargs):
        return subprocess.CompletedProcess(
            args,
            returncode=0,
            stdout="codex-cli 0.146.1\n" if "--version" in args else "",
            stderr="",
        )

    monkeypatch.setattr("workers_projects_runtime.profile_runtime.subprocess.run", fake_run)
    worker = {
        "worker_id": "wrk_host_live_refresh",
        "name": "Main Host Worker",
        "role": "browser task",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "workspaces"),
    }

    info = runtime.ensure_worker_ready(worker)
    workspace_dir = Path(info.workspace_dir)
    (workspace_dir / "harness-prompt.md").write_text("old prompt without terminal report contract")
    (workspace_dir / "AGENTS.md").write_text("old agent instructions")

    details = runtime.describe_worker(worker)

    assert details["prompt_paths"]["harness_prompt"] == str(workspace_dir / "harness-prompt.md")
    assert "FINAL REPORT:" in (workspace_dir / "harness-prompt.md").read_text()
    assert "FINAL REPORT:" in (workspace_dir / "AGENTS.md").read_text()
    assert "inspect the concrete output" in (workspace_dir / "harness-prompt.md").read_text()
    assert "inspect the concrete output" in (workspace_dir / "AGENTS.md").read_text()


def test_host_codex_runtime_rejects_untrusted_source_paths(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = "/bin/echo"
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside trusted root")
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", str(trusted))
    worker = {
        "worker_id": "wrk_host",
        "name": "Main Host Worker",
        "role": "coding",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "workspaces"),
        "bootstrap_bundle_json": json.dumps(
            {
                "files": [
                    {
                        "scope": "workspace",
                        "path": "uploads/outside.txt",
                        "source_path": str(outside),
                    }
                ],
            }
        ),
    }

    with pytest.raises((PermissionError, RuntimeErrorBase)):
        runtime.ensure_worker_ready(worker)


def test_host_codex_runtime_rejects_symlink_source_paths(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = "/bin/echo"
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside trusted root")
    symlink = trusted / "linked.txt"
    symlink.symlink_to(outside)
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", str(trusted))
    worker = {
        "worker_id": "wrk_host",
        "name": "Main Host Worker",
        "role": "coding",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "workspaces"),
        "bootstrap_bundle_json": json.dumps(
            {
                "files": [
                    {
                        "scope": "workspace",
                        "path": "uploads/linked.txt",
                        "source_path": str(symlink),
                    }
                ],
            }
        ),
    }

    with pytest.raises((PermissionError, RuntimeErrorBase)):
        runtime.ensure_worker_ready(worker)


def test_host_codex_runtime_rejects_file_entry_without_content_or_source(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = "/bin/echo"
    _patch_host_codex_requirement_probe(monkeypatch)
    worker = {
        "worker_id": "wrk_host_missing_file",
        "name": "Main Host Worker",
        "role": "coding",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "workspaces"),
        "bootstrap_bundle_json": json.dumps(
            {
                "files": [
                    {
                        "scope": "workspace",
                        "path": "uploads/missing.txt",
                    }
                ],
            }
        ),
    }

    with pytest.raises(RuntimeErrorBase, match="missing content or source_path"):
        runtime.ensure_worker_ready(worker)


def test_host_codex_runtime_rejects_empty_projected_source_file(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = "/bin/echo"
    _patch_host_codex_requirement_probe(monkeypatch)
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    empty = trusted / "empty.txt"
    empty.write_text("")
    monkeypatch.setenv("WPR_BOOTSTRAP_SOURCE_ROOTS", str(trusted))
    worker = {
        "worker_id": "wrk_host_empty_file",
        "name": "Main Host Worker",
        "role": "coding",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "workspaces"),
        "bootstrap_bundle_json": json.dumps(
            {
                "files": [
                    {
                        "scope": "workspace",
                        "path": "uploads/empty.txt",
                        "source_path": str(empty),
                    }
                ],
            }
        ),
    }

    with pytest.raises(RuntimeErrorBase, match="empty"):
        runtime.ensure_worker_ready(worker)


def test_host_codex_command_uses_host_workspace_and_dangerous_mode(tmp_path):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = "codex"
    worker = {
        "worker_id": "wrk_host",
        "name": "Main Host Worker",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "workspaces"),
    }
    info = runtime._host_runtime_info(worker)

    command, env = runtime._build_command(worker, "do the work", info)

    assert command[:4] == ["codex", "exec", "--json", "--skip-git-repo-check"]
    assert "-C" in command
    assert str(info.workspace_dir) in command
    assert "danger-full-access" in command
    assert "--dangerously-bypass-approvals-and-sandbox" in command
    assert command[-1] == "-"
    assert "do the work" not in " ".join(command)
    stdin_text = runtime._command_stdin_text(worker, "do the work", info)
    assert stdin_text and stdin_text.startswith("do the work")
    assert "FINAL REPORT:" in stdin_text
    assert "Put only the user-facing result" in stdin_text
    assert env["GLASSHIVE_EXECUTION_MODE"] == "host"
    assert env["GLASSHIVE_WORKSPACE_DIR"] == str(info.workspace_dir)


def test_host_env_projects_codex_desktop_workspace_dependencies(tmp_path, monkeypatch):
    home = tmp_path / "home"
    deps_root = home / ".cache" / "codex-runtimes" / "codex-primary-runtime" / "dependencies"
    node_bin = deps_root / "node" / "bin"
    node_modules = deps_root / "node" / "node_modules"
    native_bin = deps_root / "bin"
    python_bin = deps_root / "python" / "bin"
    for path in (node_bin, node_modules / "@oai" / "artifact-tool", native_bin, python_bin):
        path.mkdir(parents=True)
    (node_bin / "node").write_text("#!/usr/bin/env sh\n")
    (python_bin / "python3").write_text("#!/usr/bin/env sh\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.delenv("NODE_PATH", raising=False)

    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_host_deps",
        "name": "Main Host Worker",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "workspaces"),
    }

    env = runtime._host_env(worker)

    assert env["PATH"].split(os.pathsep)[:1] == ["/usr/bin"]
    for expected in (node_bin, python_bin, native_bin):
        assert str(expected) in env["PATH"].split(os.pathsep)
    assert env["NODE_PATH"] == str(node_modules)
    assert env["GLASSHIVE_WORKSPACE_NODE_MODULES"] == str(node_modules)
    assert env["GLASSHIVE_WORKSPACE_NODE_BIN"] == str(node_bin)
    assert env["GLASSHIVE_WORKSPACE_PYTHON_BIN"] == str(python_bin)
    assert env["GLASSHIVE_WORKSPACE_BIN_DIRS"] == str(native_bin)


def test_host_env_respects_explicit_workspace_dependency_paths(tmp_path, monkeypatch):
    node_modules = tmp_path / "modules"
    node_modules.mkdir()
    node_bin = tmp_path / "node-bin"
    node_bin.mkdir()
    monkeypatch.setenv("GLASSHIVE_WORKSPACE_NODE_MODULES", str(node_modules))
    monkeypatch.setenv("GLASSHIVE_WORKSPACE_NODE_BIN", str(node_bin))
    monkeypatch.setenv("GLASSHIVE_AUTO_DISCOVER_CODEX_WORKSPACE_DEPS", "false")
    monkeypatch.setenv("NODE_PATH", "/existing/modules")
    monkeypatch.setenv("PATH", "/usr/bin")

    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_host_explicit_deps",
        "name": "Claude Host Worker",
        "profile": "claude-code",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "workspaces"),
    }

    env = runtime._host_env(worker)

    assert env["NODE_PATH"].split(os.pathsep) == ["/existing/modules", str(node_modules)]
    assert env["PATH"].split(os.pathsep) == ["/usr/bin", str(node_bin)]


def test_host_env_can_disable_codex_workspace_dependency_auto_discovery(tmp_path, monkeypatch):
    home = tmp_path / "home"
    node_modules = home / ".cache" / "codex-runtimes" / "codex-primary-runtime" / "dependencies" / "node" / "node_modules"
    node_modules.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GLASSHIVE_AUTO_DISCOVER_CODEX_WORKSPACE_DEPS", "false")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.delenv("NODE_PATH", raising=False)

    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_host_no_auto_deps",
        "name": "Main Host Worker",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "workspaces"),
    }

    env = runtime._host_env(worker)

    assert "NODE_PATH" not in env
    assert "GLASSHIVE_WORKSPACE_NODE_MODULES" not in env


def test_workspace_codex_command_ignores_host_binary_override(tmp_path, monkeypatch):
    monkeypatch.setenv("WPR_CODEX_BIN", "/Applications/Codex.app/Contents/Resources/codex")
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_workspace_codex",
        "name": "Workspace Codex Worker",
        "profile": "codex-cli",
        "execution_mode": "docker",
    }
    info = runtime._runtime_info(worker)

    command, _ = runtime._build_command(worker, "do the work", info)

    assert runtime.binary == "codex"
    assert command[0] == "codex"
    assert "/Applications/Codex.app" not in " ".join(command)


def test_workspace_codex_command_honors_per_run_effort_without_custom_provider(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_BASE", raising=False)
    monkeypatch.delenv("OPENAI_REVERSE_PROXY", raising=False)
    monkeypatch.delenv("WPR_CODEX_CLI_BASE_URL", raising=False)
    monkeypatch.setenv("WPR_CODEX_CLI_XHIGH_ROUTE_PROVEN", "1")
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_workspace_codex_effort",
        "name": "Workspace Codex Worker",
        "profile": "codex-cli",
        "execution_mode": "docker",
        "bootstrap_bundle_json": json.dumps({"env": {"WPR_CODEX_CLI_REASONING_EFFORT": "xhigh"}}),
    }
    info = runtime._runtime_info(worker)

    command, _ = runtime._build_command(worker, "do the work", info)

    assert '-c' in command
    assert 'model_reasoning_effort="xhigh"' in command


def test_workspace_claude_command_ignores_host_binary_override(tmp_path, monkeypatch):
    monkeypatch.setenv("WPR_CLAUDE_CODE_BIN", "/opt/homebrew/bin/claude")
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_workspace_claude",
        "name": "Workspace Claude Worker",
        "profile": "claude-code",
        "execution_mode": "docker",
        "model": "claude-sonnet-test",
    }
    info = runtime._runtime_info(worker)

    command, _ = runtime._build_command(worker, "do the work", info)

    assert runtime.binary == "claude"
    assert command[0] == "claude"
    assert "/opt/homebrew/bin/claude" not in " ".join(command)
    assert command[command.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in command


def test_workspace_claude_command_passes_configured_api_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("API_TIMEOUT_MS", "900000")
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_workspace_claude_timeout",
        "name": "Workspace Claude Worker",
        "profile": "claude-code",
        "execution_mode": "docker",
        "model": "claude-opus-test",
    }

    _command, env = runtime._build_command(worker, "do the work", runtime._runtime_info(worker))

    assert env["API_TIMEOUT_MS"] == "900000"


def test_claude_usage_parser_preserves_input_output_and_cache_tokens(tmp_path):
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    stdout = json.dumps(
        {
            "type": "result",
            "session_id": "session-usage",
            "result": "done",
            "usage": {
                "input_tokens": 58,
                "output_tokens": 108055,
                "cache_read_input_tokens": 5236386,
                "cache_creation_input_tokens": 241709,
            },
        }
    )

    assert runtime._usage_from_output(stdout) == {
        "input_tokens": 58,
        "output_tokens": 108055,
        "cache_read_input_tokens": 5236386,
        "cache_creation_input_tokens": 241709,
    }


def test_claude_usage_parser_rejects_negative_boolean_and_malformed_values(tmp_path):
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    stdout = json.dumps(
        {
            "type": "result",
            "usage": {
                "input_tokens": -1,
                "output_tokens": True,
                "cache_read_input_tokens": "120",
                "cache_creation_input_tokens": None,
            },
        }
    )

    assert runtime._usage_from_output(stdout) == {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 120,
        "cache_creation_input_tokens": 0,
    }


def test_claude_stream_telemetry_is_compact_and_content_free(tmp_path):
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    stdout = "\n".join(
        [
            json.dumps(
                {
                    "type": "system",
                    "subtype": "init",
                    "claude_code_version": "2.1.207",
                    "model": "claude-opus-test",
                }
            ),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "id": "msg-telemetry-1",
                        "usage": {
                            "input_tokens": 12,
                            "output_tokens": 7,
                            "cache_read_input_tokens": 40,
                            "cache_creation_input_tokens": 3,
                        },
                        "content": [
                            {"type": "thinking", "thinking": "private reasoning"},
                            {
                                "type": "tool_use",
                                "name": "Read",
                                "input": {"file_path": "/private/invoice.pdf"},
                            },
                        ]
                    },
                }
            ),
            json.dumps(
                {
                    "type": "user",
                    "timestamp": "2026-07-24T10:11:12.000Z",
                    "tool_use_result": "sensitive invoice content",
                }
            ),
            json.dumps(
                {
                    "type": "api_retry",
                    "retry_delay_ms": 1500,
                    "error_status": 529,
                }
            ),
            "{not valid json",
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "session_id": "session-telemetry",
                    "duration_ms": 125000,
                    "duration_api_ms": 121000,
                    "num_turns": 8,
                    "is_error": False,
                    "stop_reason": "end_turn",
                    "ttft_ms": 2424,
                    "ttft_stream_ms": 1411,
                    "time_to_request_ms": 24,
                    "total_cost_usd": 3.25,
                    "usage": {
                        "input_tokens": 58,
                        "output_tokens": 100,
                        "cache_read_input_tokens": 800,
                        "cache_creation_input_tokens": 75,
                        "service_tier": "standard",
                        "speed": "standard",
                    },
                }
            ),
        ]
    )

    telemetry = runtime._telemetry_from_output(stdout)
    first_timestamp = telemetry.pop("first_timestamp")
    last_timestamp = telemetry.pop("last_timestamp")

    assert telemetry == {
        "schema": "glasshive.claude-run-telemetry.v1",
        "claude_code_version": "2.1.207",
        "model": "claude-opus-test",
        "service_tier": "standard",
        "speed": "standard",
        "result_state": "success",
        "is_error": False,
        "stop_reason": "end_turn",
        "duration_ms": 125000,
        "duration_api_ms": 121000,
        "duration_non_api_ms": 4000,
        "ttft_ms": 2424,
        "ttft_stream_ms": 1411,
        "time_to_request_ms": 24,
        "num_turns": 8,
        "api_retry_count": 1,
        "api_retry_delay_ms": 1500,
        "api_retry_statuses": ["529"],
        "tool_call_count": 1,
        "tool_call_counts": {"Read": 1},
        "event_count": 5,
        "malformed_line_count": 1,
        "oversized_line_count": 0,
        "stream_input_tokens": 12,
        "stream_output_tokens": 7,
        "stream_cache_read_input_tokens": 40,
        "stream_cache_creation_input_tokens": 3,
        "total_cost_usd": 3.25,
    }
    assert datetime.fromisoformat(first_timestamp)
    assert datetime.fromisoformat(last_timestamp)
    encoded = json.dumps(telemetry)
    assert "private reasoning" not in encoded
    assert "invoice.pdf" not in encoded
    assert "sensitive invoice content" not in encoded


def test_claude_stream_usage_is_counted_once_per_message_id_and_error_subtype_is_preserved(
    tmp_path,
):
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    assistant = {
        "type": "assistant",
        "message": {
            "id": "msg-duplicate",
            "usage": {
                "input_tokens": 20,
                "output_tokens": 8,
                "cache_read_input_tokens": 100,
                "cache_creation_input_tokens": 4,
            },
            "content": [],
        },
    }
    stdout = "\n".join(
        [
            json.dumps(assistant),
            json.dumps(assistant),
            json.dumps({"type": "result", "subtype": "error_max_turns"}),
        ]
    )

    telemetry = runtime._telemetry_from_output(stdout)

    assert telemetry["stream_input_tokens"] == 20
    assert telemetry["stream_output_tokens"] == 8
    assert telemetry["stream_cache_read_input_tokens"] == 100
    assert telemetry["stream_cache_creation_input_tokens"] == 4
    assert telemetry["result_state"] == "error_max_turns"
    assert telemetry["is_error"] is True


def test_claude_live_telemetry_reads_the_complete_active_run(tmp_path):
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_live_telemetry",
        "name": "Invoice Worker",
        "profile": "claude-code",
        "model": "claude-opus-test",
    }
    runtime._ensure_dirs(worker["worker_id"])
    run_id = "run_live_telemetry"
    run_root = runtime._run_root(worker["worker_id"], run_id)
    run_root.mkdir(parents=True, exist_ok=True)
    full_stream = "\n".join(
        [
            json.dumps(
                {
                    "type": "system",
                    "subtype": "init",
                    "model": "claude-opus-test",
                    "claude_code_version": "2.1.207",
                }
            ),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "tool_use", "name": "Read", "input": {"file_path": "a"}}
                        ]
                    },
                }
            ),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "tool_use", "name": "Bash", "input": {"command": "true"}}
                        ]
                    },
                }
            ),
        ]
    )
    (run_root / "stdout.log").write_text(full_stream + "\n", encoding="utf-8")

    telemetry = runtime.live_telemetry(
        worker,
        full_stream.splitlines()[-1],
        run_id=run_id,
    )

    assert telemetry["telemetry_scope"] == "full_active_run_incremental"
    assert telemetry["run_id"] == run_id
    assert telemetry["event_count"] == 3
    assert telemetry["tool_call_count"] == 2
    assert telemetry["tool_call_counts"] == {"Bash": 1, "Read": 1}
    assert telemetry["last_stream_activity_at"] == telemetry["last_progress_at"]
    assert telemetry["seconds_since_stream_activity"] == telemetry["seconds_since_progress"]


def test_claude_live_telemetry_consumes_only_complete_appended_lines(tmp_path):
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_incremental_telemetry",
        "name": "Invoice Worker",
        "profile": "claude-code",
        "model": "claude-opus-test",
    }
    runtime._ensure_dirs(worker["worker_id"])
    run_id = "run_incremental_telemetry"
    run_root = runtime._run_root(worker["worker_id"], run_id)
    run_root.mkdir(parents=True, exist_ok=True)
    stdout_path = run_root / "stdout.log"
    init_line = json.dumps({"type": "system", "subtype": "init", "model": "claude-opus-test"})
    assistant_line = json.dumps(
        {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "Read", "input": {}}]},
        }
    )
    split_at = len(assistant_line) // 2
    stdout_path.write_text(init_line + "\n" + assistant_line[:split_at], encoding="utf-8")

    first = runtime.live_telemetry(worker, "", run_id=run_id)

    assert first["event_count"] == 1
    assert first["malformed_line_count"] == 0
    assert first["partial_line_present"] is True
    assert first["sample_sequence"] == 1

    with stdout_path.open("a", encoding="utf-8") as handle:
        handle.write(assistant_line[split_at:] + "\n{not-json}\n")
    second = runtime.live_telemetry(worker, "", run_id=run_id)
    third = runtime.live_telemetry(worker, "", run_id=run_id)

    assert second["event_count"] == 2
    assert second["tool_call_counts"] == {"Read": 1}
    assert second["malformed_line_count"] == 1
    assert second["partial_line_present"] is False
    assert second["parsed_bytes"] == second["log_bytes"]
    assert third["event_count"] == second["event_count"]
    assert third["malformed_line_count"] == second["malformed_line_count"]
    assert third["sample_sequence"] == 3


def test_claude_live_telemetry_deduplicates_tool_calls_by_id(tmp_path):
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_deduplicated_tools",
        "profile": "claude-code",
        "model": "claude-opus-test",
    }
    runtime._ensure_dirs(worker["worker_id"])
    run_id = "run_deduplicated_tools"
    run_root = runtime._run_root(worker["worker_id"], run_id)
    run_root.mkdir(parents=True, exist_ok=True)
    repeated = json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "id": "toolu_123", "name": "Read", "input": {}}
                ]
            },
        }
    )
    (run_root / "stdout.log").write_text(repeated + "\n" + repeated + "\n")

    telemetry = runtime.live_telemetry(worker, "", run_id=run_id)

    assert telemetry["event_count"] == 2
    assert telemetry["tool_call_count"] == 1
    assert telemetry["tool_call_counts"] == {"Read": 1}


def test_claude_live_telemetry_does_not_substitute_console_tail_for_missing_run(tmp_path):
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    worker = {"worker_id": "wrk_missing_run", "profile": "claude-code"}
    runtime._ensure_dirs(worker["worker_id"])

    telemetry = runtime.live_telemetry(
        worker,
        json.dumps({"type": "assistant", "message": {"content": []}}),
        run_id="run_missing",
    )

    assert telemetry == {
        "schema": "glasshive.claude-run-telemetry.v1",
        "run_id": "run_missing",
        "telemetry_scope": "active_run_unavailable",
    }


def test_claude_live_telemetry_bounds_an_unterminated_oversized_record(tmp_path):
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_oversized_telemetry",
        "profile": "claude-code",
        "model": "claude-opus-test",
    }
    runtime._ensure_dirs(worker["worker_id"])
    run_id = "run_oversized"
    run_root = runtime._run_root(worker["worker_id"], run_id)
    run_root.mkdir(parents=True, exist_ok=True)
    stdout_path = run_root / "stdout.log"
    stdout_path.write_bytes(b"x" * (5 * 1024 * 1024))

    first = runtime.live_telemetry(worker, "", run_id=run_id)
    cached = runtime._live_telemetry_cache[(worker["worker_id"], run_id)]

    assert first["malformed_line_count"] == 1
    assert first["oversized_line_count"] == 1
    assert first["partial_line_present"] is True
    assert len(cached["partial"]) == 0
    assert cached["discarding_oversized_line"] is True

    with stdout_path.open("ab") as handle:
        handle.write(
            b"\n"
            + json.dumps(
                {"type": "system", "subtype": "init", "model": "claude-opus-test"}
            ).encode()
            + b"\n"
        )
    second = runtime.live_telemetry(worker, "", run_id=run_id)

    assert second["event_count"] == 1
    assert second["malformed_line_count"] == 1
    assert second["oversized_line_count"] == 1
    assert second["partial_line_present"] is False


def test_active_run_terminal_status_is_atomic_and_cannot_be_downgraded_by_heartbeat(tmp_path):
    status_path = tmp_path / "active-run.json"
    worker = {
        "worker_id": "wrk_status_race",
        "profile": "claude-code",
        "execution_mode": "docker",
    }
    arguments = {
        "path": status_path,
        "worker": worker,
        "run_id": "run_status_race",
        "runtime_name": "claude-code",
        "model": "claude-opus-test",
        "transcript_paths": {},
        "started_at": "2026-07-24T12:00:00Z",
        "process_pid": 123,
        "timeout_seconds": 30.0,
    }
    profile_runtime_module._write_active_run_status(state="running", **arguments)

    start = threading.Event()

    def heartbeat_writer():
        start.wait()
        for _ in range(200):
            profile_runtime_module._write_active_run_status(state="running", **arguments)

    heartbeat = threading.Thread(target=heartbeat_writer)
    heartbeat.start()
    start.set()
    profile_runtime_module._write_active_run_status(
        state="timeout",
        stop_reason="timeout",
        **arguments,
    )
    heartbeat.join()
    profile_runtime_module._write_active_run_status(state="running", **arguments)

    status = json.loads(status_path.read_text())
    assert status["run_id"] == "run_status_race"
    assert status["state"] == "timeout"
    assert status["stop_reason"] == "timeout"


def test_persisted_run_telemetry_is_atomic_and_content_allowlisted(tmp_path, monkeypatch):
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    worker = {"worker_id": "wrk_safe_telemetry", "profile": "claude-code"}
    runtime._ensure_dirs(worker["worker_id"])
    run_id = "run_safe"
    path = runtime._run_root(worker["worker_id"], run_id) / "telemetry.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "event_count": 7,
                "model": "SECRET-INVOICE-CONTENT",
                "stop_reason": "customer-name",
                "prompt": "private invoice line",
            }
        )
    )

    assert runtime.run_telemetry(worker, run_id) == {
        "schema": "glasshive.claude-run-telemetry.v1",
        "run_id": run_id,
        "event_count": 7,
    }

    monkeypatch.setattr(os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("disk")))
    telemetry = runtime._record_run_telemetry(
        worker["worker_id"],
        "run_atomic_failure",
        json.dumps({"type": "system", "subtype": "init", "model": "claude-opus-test"}),
    )
    assert telemetry["run_id"] == "run_atomic_failure"


def test_claude_failed_stream_never_promotes_transcript_content_to_public_error_fields(tmp_path):
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_failed_claude_stream",
        "name": "Invoice Worker",
        "profile": "claude-code",
        "model": "claude-opus-test",
    }
    runtime._ensure_dirs(worker["worker_id"])
    run_id = "run_failed_claude_stream"
    run_root = runtime._run_root(worker["worker_id"], run_id)
    run_root.mkdir(parents=True, exist_ok=True)
    sensitive = "INVOICE 999999 PRIVATE-LINE-CONTENT"
    (run_root / "stdout.log").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": sensitive}]},
                    }
                ),
                json.dumps(
                    {
                        "type": "result",
                        "subtype": "error",
                        "is_error": True,
                        "error_status": 529,
                        "result": "API Error: 529 Overloaded",
                    }
                ),
            ]
        )
        + "\n"
    )
    (run_root / "stderr.log").write_text("")
    (run_root / "exit_code").write_text("1")

    recovered = runtime.collect_completed_run(worker, run_id=run_id)

    assert recovered is not None
    assert recovered["state"] == "failed"
    assert recovered["failure_class"] == "provider_response_failed"
    assert sensitive not in recovered["error_text"]
    assert sensitive not in recovered["failure_diagnostic_summary"]
    assert "Overloaded" not in recovered["failure_diagnostic_summary"]
    assert recovered["telemetry"]["api_retry_statuses"] == []


def test_claude_run_telemetry_is_recorded_and_read_back(tmp_path):
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    worker_id = "wrk_telemetry"
    run_id = "run_telemetry"
    stdout = json.dumps(
        {
            "type": "result",
            "subtype": "error",
            "duration_ms": 4000,
            "duration_api_ms": 3900,
            "num_turns": 2,
            "is_error": True,
            "stop_reason": "max_tokens",
        }
    )

    recorded = runtime._record_run_telemetry(worker_id, run_id, stdout)

    assert recorded["result_state"] == "error"
    assert recorded["is_error"] is True
    assert runtime.run_telemetry({"worker_id": worker_id}, run_id) == recorded
    telemetry_path = runtime._run_root(worker_id, run_id) / "telemetry.json"
    assert telemetry_path.stat().st_mode & 0o777 == 0o600


def test_workspace_claude_command_honors_per_run_max_effort(tmp_path, monkeypatch):
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_workspace_claude_effort",
        "name": "Workspace Claude Worker",
        "profile": "claude-code",
        "execution_mode": "docker",
        "model": "claude-sonnet-test",
        "bootstrap_bundle_json": json.dumps({"env": {"WPR_CLAUDE_CODE_EFFORT": "max"}}),
    }
    info = runtime._runtime_info(worker)

    command, _ = runtime._build_command(worker, "do the work", info)

    assert command[command.index("--effort") + 1] == "max"


def test_workspace_claude_command_honors_per_run_xhigh_effort(tmp_path):
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_workspace_claude_xhigh",
        "name": "Workspace Claude Worker",
        "profile": "claude-code",
        "execution_mode": "docker",
        "model": "claude-opus-4-8",
        "bootstrap_bundle_json": json.dumps({"env": {"WPR_CLAUDE_CODE_EFFORT": "xhigh"}}),
    }

    command, _ = runtime._build_command(worker, "do the work", runtime._runtime_info(worker))

    assert command[command.index("--effort") + 1] == "xhigh"


def test_workspace_claude_max_effort_preflight_requires_effort_support(tmp_path, monkeypatch):
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    ClaudeCodeRuntime._workspace_effort_support_cache.clear()
    monkeypatch.setattr(runtime.sandbox, "_ensure_image", lambda: None)
    monkeypatch.setattr(
        runtime.sandbox,
        "_docker",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, returncode=0, stdout="Usage: claude [options]\n", stderr=""),
    )
    worker = {
        "worker_id": "wrk_workspace_claude_effort",
        "profile": "claude-code",
        "execution_mode": "docker",
        "bootstrap_bundle_json": json.dumps({"env": {"WPR_CLAUDE_CODE_EFFORT": "max"}}),
    }

    with pytest.raises(RuntimeDependencyMissingError, match="--effort"):
        runtime._preflight_workspace_effort_support(worker)


def test_workspace_claude_max_effort_preflight_accepts_effort_support(tmp_path, monkeypatch):
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    ClaudeCodeRuntime._workspace_effort_support_cache.clear()
    calls: list[object] = []
    monkeypatch.setattr(runtime.sandbox, "_ensure_image", lambda: calls.append("image"))
    monkeypatch.setattr(
        runtime.sandbox,
        "_docker",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args,
            returncode=0,
            stdout="Usage: claude [options] --effort <level> (low, medium, high, xhigh, max)\n",
            stderr="",
        ),
    )
    worker = {
        "worker_id": "wrk_workspace_claude_effort",
        "profile": "claude-code",
        "execution_mode": "docker",
        "bootstrap_bundle_json": json.dumps({"env": {"WPR_CLAUDE_CODE_EFFORT": "max"}}),
    }

    runtime._preflight_workspace_effort_support(worker)
    runtime._preflight_workspace_effort_support(worker)

    assert calls == ["image"]


def test_workspace_claude_xhigh_effort_preflight_rejects_older_effort_contract(tmp_path, monkeypatch):
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    ClaudeCodeRuntime._workspace_effort_support_cache.clear()
    monkeypatch.setattr(runtime.sandbox, "_ensure_image", lambda: None)
    monkeypatch.setattr(
        runtime.sandbox,
        "_docker",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args,
            returncode=0,
            stdout="Usage: claude [options] --effort <level> (low, medium, high, max)\n",
            stderr="",
        ),
    )
    worker = {
        "worker_id": "wrk_workspace_claude_xhigh_unsupported",
        "profile": "claude-code",
        "execution_mode": "docker",
        "bootstrap_bundle_json": json.dumps({"env": {"WPR_CLAUDE_CODE_EFFORT": "xhigh"}}),
    }

    with pytest.raises(RuntimeDependencyMissingError, match="xhigh"):
        runtime._preflight_workspace_effort_support(worker)


def test_host_claude_command_enables_chrome_by_default(tmp_path, monkeypatch):
    fake_claude = tmp_path / "claude"
    fake_claude.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$1\" == \"--help\" ]]; then echo 'Usage: claude [options] --effort --chrome'; exit 0; fi\n"
        "echo '2.1.223 (Claude Code)'\n"
    )
    fake_claude.chmod(0o755)
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = str(fake_claude)
    monkeypatch.delenv("WPR_CLAUDE_CODE_ENABLE_CHROME", raising=False)
    monkeypatch.setenv("WPR_CLAUDE_CODE_EFFORT", "max")
    worker = {
        "worker_id": "wrk_host_claude",
        "name": "Main Host Claude Worker",
        "profile": "claude-code",
        "execution_mode": "host",
        "model": "claude-opus-4-8",
        "workspace_root": str(tmp_path / "workspaces"),
    }
    info = runtime._host_runtime_info(worker)

    command, _ = runtime._build_command(worker, "do the work", info)

    assert "--chrome" in command
    assert command[command.index("--effort") + 1] == "max"
    assert "do the work" not in " ".join(command)
    stdin_text = runtime._command_stdin_text(worker, "do the work", info)
    assert stdin_text and stdin_text.startswith("do the work")
    assert "FINAL REPORT:" in stdin_text


def test_host_claude_command_honors_xhigh_effort(tmp_path, monkeypatch):
    fake_claude = tmp_path / "claude"
    fake_claude.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$1\" == \"--help\" ]]; then echo 'Usage: claude [options] --effort <level> (low, medium, high, xhigh, max)'; exit 0; fi\n"
        "echo '2.1.207 (Claude Code)'\n"
    )
    fake_claude.chmod(0o755)
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = str(fake_claude)
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", "0")
    monkeypatch.setenv("WPR_CLAUDE_CODE_EFFORT", "xhigh")
    worker = {
        "worker_id": "wrk_host_claude_xhigh",
        "name": "Host Claude Worker",
        "profile": "claude-code",
        "execution_mode": "host",
        "model": "claude-opus-4-8",
        "workspace_root": str(tmp_path / "workspaces"),
    }

    command, _ = runtime._build_command(worker, "do the work", runtime._host_runtime_info(worker))

    assert command[command.index("--effort") + 1] == "xhigh"


def test_host_claude_xhigh_effort_rejects_older_effort_contract(tmp_path, monkeypatch):
    fake_claude = tmp_path / "claude"
    fake_claude.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$1\" == \"--help\" ]]; then echo 'Usage: claude [options] --effort <level> (low, medium, high, max)'; exit 0; fi\n"
        "echo '2.1.223 (Claude Code)'\n"
    )
    fake_claude.chmod(0o755)
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = str(fake_claude)
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", "0")
    monkeypatch.setenv("WPR_CLAUDE_CODE_EFFORT", "xhigh")
    worker = {
        "worker_id": "wrk_host_claude_xhigh_unsupported",
        "name": "Host Claude Worker",
        "profile": "claude-code",
        "execution_mode": "host",
        "model": "claude-opus-4-8",
        "workspace_root": str(tmp_path / "workspaces"),
    }

    with pytest.raises(RuntimeDependencyMissingError, match="xhigh"):
        runtime._build_command(worker, "do the work", runtime._host_runtime_info(worker))


def test_host_claude_chrome_can_be_explicitly_disabled(tmp_path, monkeypatch):
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = "claude"
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", "0")
    worker = {
        "worker_id": "wrk_host_claude_no_chrome",
        "name": "Main Host Claude Worker",
        "profile": "claude-code",
        "execution_mode": "host",
        "model": "claude-sonnet-test",
        "workspace_root": str(tmp_path / "workspaces"),
    }
    info = runtime._host_runtime_info(worker)

    command, _ = runtime._build_command(worker, "do the work", info)

    assert "--chrome" not in command


def test_host_cli_runtime_allows_one_active_worker_per_family(tmp_path):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    first = {
        "worker_id": "wrk_host_one",
        "name": "First Host Worker",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "workspaces"),
    }
    second = {
        "worker_id": "wrk_host_two",
        "name": "Second Host Worker",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "workspaces"),
    }

    runtime._acquire_host_slot(first)
    try:
        with pytest.raises(RuntimeErrorBase, match="one active host worker per CLI family"):
            runtime._acquire_host_slot(second)
    finally:
        runtime._release_host_slot(first["worker_id"])

    runtime._acquire_host_slot(second)
    runtime._release_host_slot(second["worker_id"])


def test_host_cli_runtime_reserves_a_separate_interactive_conversation_lane(tmp_path):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    mission = {
        "worker_id": "wrk_mission_lane",
        "profile": "codex-cli",
        "execution_mode": "host",
        "bootstrap_bundle_json": json.dumps({"run_mode": "mission"}),
    }
    conversation = {
        "worker_id": "wrk_conversation_lane",
        "profile": "codex-cli",
        "execution_mode": "host",
        "bootstrap_bundle_json": json.dumps({"run_mode": "conversation"}),
    }
    second_conversation = {
        **conversation,
        "worker_id": "wrk_conversation_lane_two",
    }

    runtime._acquire_host_slot(mission)
    runtime._acquire_host_slot(conversation)
    try:
        with pytest.raises(RuntimeErrorBase, match="active conversation worker"):
            runtime._acquire_host_slot(second_conversation)
    finally:
        runtime._release_host_slot(conversation["worker_id"])
        runtime._release_host_slot(mission["worker_id"])


def test_host_cli_runtime_has_no_default_hard_run_timeout(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))

    monkeypatch.delenv("GLASSHIVE_HOST_RUN_TIMEOUT_SEC", raising=False)
    monkeypatch.delenv("WPR_HOST_RUN_TIMEOUT_SEC", raising=False)

    assert runtime._host_run_timeout_sec() is None


@pytest.mark.parametrize("value", ["0", "none", "off", "false", "disabled", "-1"])
def test_host_cli_runtime_timeout_can_be_disabled_explicitly(tmp_path, monkeypatch, value):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))

    monkeypatch.setenv("GLASSHIVE_HOST_RUN_TIMEOUT_SEC", value)

    assert runtime._host_run_timeout_sec() is None


def test_host_cli_runtime_uses_configured_timeout_when_set(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))

    monkeypatch.setenv("GLASSHIVE_HOST_RUN_TIMEOUT_SEC", "900")

    assert runtime._host_run_timeout_sec() == 900


def test_host_cli_runtime_honors_caller_timeout_when_no_env_override(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))

    monkeypatch.delenv("GLASSHIVE_HOST_RUN_TIMEOUT_SEC", raising=False)
    monkeypatch.delenv("WPR_HOST_RUN_TIMEOUT_SEC", raising=False)
    monkeypatch.delenv("GLASSHIVE_RUN_TIMEOUT_SEC", raising=False)
    monkeypatch.delenv("WPR_RUN_TIMEOUT_SEC", raising=False)

    assert runtime._host_run_timeout_sec(42) == 42


def test_docker_cli_runtime_accepts_no_default_run_timeout(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    exit_path = tmp_path / "exit_code"
    runtime.sandbox.inspect = lambda worker_id: None  # type: ignore[method-assign]

    def finish_run():
        time.sleep(0.05)
        exit_path.write_text("0")

    thread = threading.Thread(target=finish_run)
    thread.start()
    try:
        assert runtime._wait_for_exit_code("wrk_test", exit_path, None) == 0
    finally:
        thread.join(timeout=1)


def test_docker_cli_runtime_throttles_wait_loop_inspect(tmp_path, monkeypatch):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    exit_path = tmp_path / "exit_code"
    inspect_calls = 0
    monkeypatch.setenv("WPR_RUN_WAIT_INSPECT_INTERVAL_SEC", "60")

    def inspect_once(worker_id):
        nonlocal inspect_calls
        inspect_calls += 1
        return None

    runtime.sandbox.inspect = inspect_once  # type: ignore[method-assign]

    def finish_run():
        time.sleep(0.2)
        exit_path.write_text("0")

    thread = threading.Thread(target=finish_run)
    thread.start()
    try:
        assert runtime._wait_for_exit_code("wrk_test", exit_path, None) == 0
    finally:
        thread.join(timeout=1)
    assert inspect_calls == 1


def test_docker_cli_runtime_clears_active_session_after_stop(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    worker_id = "wrk_stop_meta"
    runtime._ensure_dirs(worker_id)
    runtime._write_active_session(
        worker_id,
        {
            "session_name": "job-run_stop_meta",
            "run_id": "run_stop_meta",
            "stdout_path": str(tmp_path / "stdout.log"),
            "stderr_path": str(tmp_path / "stderr.log"),
            "exit_path": str(tmp_path / "exit_code"),
        },
    )
    calls: list[tuple[str, str]] = []
    runtime.sandbox.stop_screen_session = lambda worker_id, runtime_name, session_name, **kwargs: calls.append(("screen", session_name))  # type: ignore[method-assign]
    runtime.sandbox.terminate_run_processes = lambda worker_id, runtime_name, run_id, **kwargs: calls.append(("terminate", run_id))  # type: ignore[method-assign]

    runtime._stop_active_process(worker_id, worker={"worker_id": worker_id})

    assert calls == [("screen", "job-run_stop_meta"), ("terminate", "run_stop_meta")]
    assert not runtime._active_session_meta_path(worker_id).exists()


def test_docker_cli_runtime_uses_configured_run_timeout(tmp_path, monkeypatch):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))

    monkeypatch.setenv("GLASSHIVE_RUN_TIMEOUT_SEC", "1200")

    assert runtime._run_timeout_sec() == 1200


def test_docker_cli_runtime_description_exposes_desktop_prime_marker(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    worker = {"worker_id": "wrk_describe_prime", "name": "Prime Worker", "profile": "codex-cli"}
    runtime.sandbox.describe = lambda worker_id: {  # type: ignore[method-assign]
        "workspace_dir": str(tmp_path / "workspace"),
        "home_dir": str(tmp_path / "home"),
        "container_name": "wpr-describe-prime",
        "container_id": "cid",
        "state": "running",
        "image": "workers-projects-runtime-workstation:phase1-node22-docs7",
        "view_url": "http://127.0.0.1:7900",
        "view_available": True,
        "view_health": {"healthy": True},
        "novnc_port": 57900,
        "selenium_port": 57901,
        "openclaw_port": 57902,
        "desktop_prime": {"schema": "glasshive.desktop_prime.v1", "status": "launched"},
        "pid": 1234,
    }

    details = runtime.describe_worker(worker)

    assert details["desktop_prime"] == {"schema": "glasshive.desktop_prime.v1", "status": "launched"}


def test_docker_cli_runtime_sources_runtime_and_openclaw_env_files(tmp_path):
    class CaptureRuntime(BaseCliWorkerRuntime):
        runtime_name = "openclaw"
        worker_root_name = "capture_runtime"

        def resolve_model(self, profile: str) -> str:
            return "capture/model"

        def _build_command(self, worker, instruction, info):
            return ["printf", "ok"], {}

        def _parse_output(self, worker, stdout, stderr, info):
            return None, stdout.strip()

    runtime = CaptureRuntime(base_dir=str(tmp_path / "data"))
    worker = {"worker_id": "wrk_capture", "name": "Capture Worker", "profile": "openclaw-general"}
    run_id = "run_capture"

    class FakeSandbox:
        container_name = "wpr-capture"
        pid = 123

    def fake_ensure_ready(worker, runtime_name, **kwargs):
        assert worker["_glasshive_task_run"] is True
        assert worker["_active_run_id"] == run_id
        return FakeSandbox()

    runtime.sandbox.ensure_ready = fake_ensure_ready  # type: ignore[method-assign]
    runtime.sandbox.inspect = lambda worker_id: None  # type: ignore[method-assign]
    runtime.sandbox.list_screen_sessions = lambda *args, **kwargs: []  # type: ignore[method-assign]
    runtime.sandbox._ensure_container_writable_paths = lambda *args, **kwargs: None  # type: ignore[method-assign]
    writable_repairs: list[list[str]] = []
    runtime.sandbox.ensure_container_writable_paths = lambda *args, **kwargs: writable_repairs.append(args[2])  # type: ignore[method-assign]

    def fake_start_screen_session(worker_id, runtime_name, session_name, command, *, env=None, worker=None):
        run_root = runtime._run_root(worker_id, run_id)
        script = (run_root / "run.sh").read_text()
        assert "if [ ! -f /workspace/.wpr-home/.glasshive-runs/run_capture/exit_code ]; then" in script
        assert '$HOME/.glasshive/runtime.env' in script
        assert '$HOME/.wpr-openclaw/openclaw.env' in script
        assert "GLASSHIVE_ACTIVE_RUN_ID=run_capture" in script
        assert "GLASSHIVE_RUN_ID=run_capture" in script
        assert "GLASSHIVE_ACTIVE_WORKER_ID=wrk_capture" in script
        assert "unset " in script
        assert "OPENAI_API_KEY" in script
        assert "CLAUDE_CODE_OAUTH_TOKEN" in script
        (run_root / "stdout.log").write_text("FINAL REPORT:\nok")
        (run_root / "stderr.log").write_text("")
        (run_root / "exit_code").write_text("0")
        return subprocess.CompletedProcess(["screen"], returncode=0, stdout="", stderr="")

    runtime.sandbox.start_screen_session = fake_start_screen_session  # type: ignore[method-assign]
    runtime.sandbox.screen_session_pid = lambda *args, **kwargs: 4321  # type: ignore[method-assign]

    assert runtime.run_task(worker, "do it", run_id=run_id) == "FINAL REPORT:\nok"
    assert writable_repairs == [
        [f"{runtime.sandbox.home_mount}/.glasshive-runs/{run_id}"],
        [runtime.sandbox.workspace_mount, f"{runtime.sandbox.home_mount}/.glasshive-runs/{run_id}"]
    ]
    workspace = runtime._workspace_dir(worker["worker_id"])
    active_status = json.loads((workspace / "glasshive-run" / "runs" / run_id / "active-run.json").read_text())
    assert active_status["state"] == "completed"
    assert active_status["runtime"] == "openclaw"
    assert active_status["worker"]["execution_mode"] == ""
    assert active_status["process_pid"] == 4321
    assert active_status["heartbeat_sequence"] >= 1
    assert active_status["transcript_progress"]["files"]["stdout"]["exists"] is True
    assert active_status["transcript_progress"]["files"]["stdout"]["bytes"] > 0
    assert active_status["evidence_path"] == "glasshive-run/evidence.json"
    active_session_text = runtime._active_session_meta_path(worker["worker_id"]).read_text()
    assert "do it" not in active_session_text
    active_session = json.loads(active_session_text)
    assert active_session["instruction_redacted"] is True
    assert active_session["process_pid"] == 4321


def test_docker_cli_run_writes_timeout_active_run_status(tmp_path, monkeypatch):
    class CaptureRuntime(BaseCliWorkerRuntime):
        runtime_name = "openclaw"
        worker_root_name = "capture_runtime"

        def resolve_model(self, profile: str) -> str:
            return "capture/model"

        def _build_command(self, worker, instruction, info):
            return ["sleep", "60"], {}

        def _parse_output(self, worker, stdout, stderr, info):
            return None, stdout.strip()

    runtime = CaptureRuntime(base_dir=str(tmp_path / "data"))
    recorded_metrics: list[tuple[str, str, str]] = []

    def record_metrics(worker_id, recorded_run_id, stdout):
        recorded_metrics.append((worker_id, recorded_run_id, stdout))
        return {}, {}

    runtime._record_run_metrics = record_metrics  # type: ignore[method-assign]
    worker = {"worker_id": "wrk_docker_timeout", "name": "Timeout Worker", "profile": "openclaw-general"}
    run_id = "run_docker_timeout"

    class FakeSandbox:
        container_name = "wpr-timeout"
        pid = 123
        state = "running"

    runtime.sandbox.ensure_ready = lambda worker, runtime_name, **kwargs: FakeSandbox()  # type: ignore[method-assign]
    runtime.sandbox.inspect = lambda worker_id: FakeSandbox()  # type: ignore[method-assign]
    runtime.sandbox.list_screen_sessions = lambda *args, **kwargs: []  # type: ignore[method-assign]
    runtime.sandbox._ensure_container_writable_paths = lambda *args, **kwargs: None  # type: ignore[method-assign]
    runtime.sandbox.ensure_container_writable_paths = lambda *args, **kwargs: None  # type: ignore[method-assign]
    runtime.sandbox.stop_screen_session = lambda *args, **kwargs: None  # type: ignore[method-assign]
    runtime.sandbox.terminate_run_processes = lambda *args, **kwargs: None  # type: ignore[method-assign]
    runtime.sandbox.screen_session_pid = lambda *args, **kwargs: 9876  # type: ignore[method-assign]
    monkeypatch.setenv("WPR_RUN_WAIT_INSPECT_INTERVAL_SEC", "60")

    def fake_start_screen_session(worker_id, runtime_name, session_name, command, *, env=None, worker=None):
        run_root = runtime._run_root(worker_id, run_id)
        (run_root / "stdout.log").write_text("Started but still working.\n")
        (run_root / "stderr.log").write_text("")
        return subprocess.CompletedProcess(["screen"], returncode=0, stdout="", stderr="")

    runtime.sandbox.start_screen_session = fake_start_screen_session  # type: ignore[method-assign]

    with pytest.raises(RuntimeErrorBase, match="timed out"):
        runtime.run_task(worker, "Do long work.", timeout_sec=0.01, run_id=run_id)

    active_status = json.loads(
        (runtime._workspace_dir(worker["worker_id"]) / "glasshive-run" / "runs" / run_id / "active-run.json").read_text()
    )
    assert active_status["state"] == "timeout"
    assert active_status["stop_reason"] == "timeout"
    assert active_status["process_pid"] == 9876
    assert active_status["transcript_progress"]["files"]["stdout"]["exists"] is True
    assert active_status["evidence_path"] == "glasshive-run/evidence.json"
    assert recorded_metrics == [
        ("wrk_docker_timeout", run_id, "Started but still working.\n")
    ]


def test_docker_cli_runtime_redirects_private_instruction_from_stdin_file(tmp_path):
    class StdinRuntime(BaseCliWorkerRuntime):
        runtime_name = "codex-cli"
        worker_root_name = "stdin_runtime"

        def resolve_model(self, profile: str) -> str:
            return "capture/model"

        def _build_command(self, worker, instruction, info):
            return ["fake-cli", "-"], {}

        def _command_stdin_text(self, worker, instruction, info):
            return self._instruction_with_completion_contract(instruction)

        def _parse_output(self, worker, stdout, stderr, info):
            return None, stdout.strip()

    runtime = StdinRuntime(base_dir=str(tmp_path / "data"))
    worker = {"worker_id": "wrk_docker_stdin", "name": "Stdin Worker", "profile": "codex-cli"}
    run_id = "run_docker_stdin"

    class FakeSandbox:
        container_name = "wpr-capture"
        pid = 123

    runtime.sandbox.ensure_ready = lambda worker, runtime_name, **kwargs: FakeSandbox()  # type: ignore[method-assign]
    runtime.sandbox.inspect = lambda worker_id: None  # type: ignore[method-assign]
    runtime.sandbox.list_screen_sessions = lambda *args, **kwargs: []  # type: ignore[method-assign]
    runtime.sandbox._ensure_container_writable_paths = lambda *args, **kwargs: None  # type: ignore[method-assign]
    runtime.sandbox.ensure_container_writable_paths = lambda *args, **kwargs: None  # type: ignore[method-assign]

    def fake_start_screen_session(worker_id, runtime_name, session_name, command, *, env=None, worker=None):
        run_root = runtime._run_root(worker_id, run_id)
        script = (run_root / "run.sh").read_text()
        stdin_path = run_root / "instruction.stdin"
        assert (run_root / "stdout.log").is_file()
        assert (run_root / "stderr.log").is_file()
        assert stdin_path.exists()
        assert stdin_path.read_text().startswith("Sensitive docker instruction.")
        assert oct(stdin_path.stat().st_mode & 0o777) == "0o600"
        assert "Sensitive docker instruction" not in script
        assert f"fake-cli - < {runtime.sandbox.home_mount}/.glasshive-runs/{run_id}/instruction.stdin" in script
        (run_root / "stdout.log").write_text("FINAL REPORT:\nok")
        (run_root / "stderr.log").write_text("")
        (run_root / "exit_code").write_text("0")
        return subprocess.CompletedProcess(["screen"], returncode=0, stdout="", stderr="")

    runtime.sandbox.start_screen_session = fake_start_screen_session  # type: ignore[method-assign]
    runtime.sandbox.screen_session_pid = lambda *args, **kwargs: 2468  # type: ignore[method-assign]

    assert runtime.run_task(worker, "Sensitive docker instruction.", run_id=run_id) == "FINAL REPORT:\nok"


def _install_fake_successful_docker_run(runtime: BaseCliWorkerRuntime, run_id: str, stdout_text: str) -> None:
    class FakeSandbox:
        container_name = "wpr-capture"
        pid = 123

    runtime.sandbox.ensure_ready = lambda worker, runtime_name, **kwargs: FakeSandbox()  # type: ignore[method-assign]
    runtime.sandbox.inspect = lambda worker_id: None  # type: ignore[method-assign]
    runtime.sandbox.list_screen_sessions = lambda *args, **kwargs: []  # type: ignore[method-assign]
    runtime.sandbox._ensure_container_writable_paths = lambda *args, **kwargs: None  # type: ignore[method-assign]
    runtime.sandbox.ensure_container_writable_paths = lambda *args, **kwargs: None  # type: ignore[method-assign]

    def fake_start_screen_session(worker_id, runtime_name, session_name, command, *, env=None, worker=None):
        run_root = runtime._run_root(worker_id, run_id)
        (run_root / "stdout.log").write_text(stdout_text)
        (run_root / "stderr.log").write_text("")
        (run_root / "exit_code").write_text("0")
        return subprocess.CompletedProcess(["screen"], returncode=0, stdout="", stderr="")

    runtime.sandbox.start_screen_session = fake_start_screen_session  # type: ignore[method-assign]
    runtime.sandbox.screen_session_pid = lambda *args, **kwargs: 1357  # type: ignore[method-assign]


def test_docker_cli_run_fails_when_evidence_contract_fails(tmp_path):
    class CaptureRuntime(BaseCliWorkerRuntime):
        runtime_name = "openclaw"
        worker_root_name = "capture_runtime"

        def resolve_model(self, profile: str) -> str:
            return "capture/model"

        def _build_command(self, worker, instruction, info):
            return ["printf", "ok"], {}

        def _parse_output(self, worker, stdout, stderr, info):
            return None, "Done"

    runtime = CaptureRuntime(base_dir=str(tmp_path / "data"))
    run_id = "run_docker_evidence_fail"
    _install_fake_successful_docker_run(runtime, run_id, "FINAL REPORT:\nDone\n")
    worker = {"worker_id": "wrk_docker_evidence_fail", "name": "Capture Worker", "profile": "openclaw-general"}

    with pytest.raises(RuntimeErrorBase, match="GlassHive evidence check failed"):
        runtime.run_task(worker, "Deliver a PDF report.", run_id=run_id)

    evidence = json.loads((runtime._workspace_dir(worker["worker_id"]) / "glasshive-run" / "evidence.json").read_text())
    assert evidence["evidence_result"]["status"] == "fail"
    assert evidence["completion_compliance"]["missing_required_artifact_types"] == ["pdf"]


def test_docker_cli_run_fails_when_success_evidence_cannot_be_written(tmp_path, monkeypatch):
    class CaptureRuntime(BaseCliWorkerRuntime):
        runtime_name = "openclaw"
        worker_root_name = "capture_runtime"

        def resolve_model(self, profile: str) -> str:
            return "capture/model"

        def _build_command(self, worker, instruction, info):
            return ["printf", "ok"], {}

        def _parse_output(self, worker, stdout, stderr, info):
            return None, "Done"

    runtime = CaptureRuntime(base_dir=str(tmp_path / "data"))
    run_id = "run_docker_evidence_write_fail"
    _install_fake_successful_docker_run(runtime, run_id, "FINAL REPORT:\nDone\n")
    monkeypatch.setattr(
        "workers_projects_runtime.profile_runtime.write_run_evidence",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("synthetic evidence write failure")),
    )
    worker = {"worker_id": "wrk_docker_evidence_write_fail", "name": "Capture Worker", "profile": "openclaw-general"}

    with pytest.raises(RuntimeErrorBase, match="run evidence was not written"):
        runtime.run_task(worker, "Do the work.", run_id=run_id)


def test_docker_cli_run_fails_when_success_constraint_ledger_cannot_be_written(tmp_path, monkeypatch):
    class CaptureRuntime(BaseCliWorkerRuntime):
        runtime_name = "openclaw"
        worker_root_name = "capture_runtime"

        def resolve_model(self, profile: str) -> str:
            return "capture/model"

        def _build_command(self, worker, instruction, info):
            return ["printf", "ok"], {}

        def _parse_output(self, worker, stdout, stderr, info):
            return None, "Done"

    runtime = CaptureRuntime(base_dir=str(tmp_path / "data"))
    run_id = "run_docker_ledger_write_fail"
    _install_fake_successful_docker_run(runtime, run_id, "FINAL REPORT:\nDone\n")
    monkeypatch.setattr(
        "workers_projects_runtime.profile_runtime.write_constraint_ledger",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("synthetic ledger write failure")),
    )
    worker = {"worker_id": "wrk_docker_ledger_write_fail", "name": "Capture Worker", "profile": "openclaw-general"}

    with pytest.raises(RuntimeErrorBase, match="constraint ledger was not written"):
        runtime.run_task(worker, "Do the work.", run_id=run_id)


def test_docker_codex_command_appends_completion_contract(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_contract",
        "name": "Main Worker",
        "profile": "codex-cli",
        "model": "gpt-5.4",
    }
    runtime._ensure_dirs(worker["worker_id"])

    command, _ = runtime._build_command(worker, "Make the page red.", runtime._runtime_info(worker))

    assert command[-1] == "-"
    assert "Make the page red." not in " ".join(command)
    stdin_text = runtime._command_stdin_text(worker, "Make the page red.", runtime._runtime_info(worker))
    assert stdin_text and stdin_text.startswith("Make the page red.")
    assert "FINAL REPORT:" in stdin_text


def test_docker_claude_command_enables_chrome_and_appends_completion_contract(tmp_path, monkeypatch):
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    monkeypatch.delenv("WPR_CLAUDE_CODE_ENABLE_CHROME", raising=False)
    worker = {
        "worker_id": "wrk_claude_contract",
        "name": "Main Worker",
        "profile": "claude-code",
        "model": "claude-sonnet-4-6",
    }
    runtime._ensure_dirs(worker["worker_id"])

    command, _ = runtime._build_command(worker, "Make the page red.", runtime._runtime_info(worker))

    assert "--chrome" in command
    assert "Make the page red." not in " ".join(command)
    stdin_text = runtime._command_stdin_text(worker, "Make the page red.", runtime._runtime_info(worker))
    assert stdin_text and stdin_text.startswith("Make the page red.")
    assert "FINAL REPORT:" in stdin_text


def test_docker_claude_chrome_can_be_explicitly_disabled(tmp_path, monkeypatch):
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", "0")
    worker = {
        "worker_id": "wrk_claude_no_chrome",
        "name": "Main Worker",
        "profile": "claude-code",
        "model": "claude-sonnet-4-6",
    }
    runtime._ensure_dirs(worker["worker_id"])

    command, _ = runtime._build_command(worker, "Make the page red.", runtime._runtime_info(worker))

    assert "--chrome" not in command


def test_docker_codex_command_projects_openai_compatible_provider(tmp_path, monkeypatch):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_provider",
        "name": "Main Worker",
        "profile": "codex-cli",
        "model": "gpt-5.2-chat",
    }
    runtime._ensure_dirs(worker["worker_id"])
    monkeypatch.setenv("OPENAI_BASE_URL", "https://models.example.test/openai/v1")

    command, env = runtime._build_command(worker, "Create the artifact.", runtime._runtime_info(worker))

    assert "--ignore-user-config" not in command
    joined = "\n".join(command)
    assert "--disable" not in command
    for native_feature in ("apps", "multi_agent", "plugins", "browser_use", "computer_use"):
        assert f"--disable\n{native_feature}" not in joined
    assert 'model_provider="glasshive_openai_compatible"' in command
    assert 'model_providers.glasshive_openai_compatible.base_url="https://models.example.test/openai/v1"' in command
    assert 'model_providers.glasshive_openai_compatible.env_key="OPENAI_API_KEY"' in command
    assert "model_providers.glasshive_openai_compatible.supports_websockets=false" in command
    assert 'model_verbosity="medium"' in command
    assert env["OPENAI_BASE_URL"] == "https://models.example.test/openai/v1"


def test_bound_docker_codex_subscription_does_not_use_deployment_provider(tmp_path, monkeypatch):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_personal_provider",
        "name": "Personal Codex Worker",
        "profile": "codex-cli",
        "model": "gpt-5.2-chat",
        "bootstrap_bundle_json": json.dumps(
            {
                "provider_account": {
                    "policy": "personal_required",
                    "account_id": "acct_personal",
                }
            }
        ),
        "_glasshive_provider_account_bound": True,
        "_glasshive_provider_account_env": {
            "CODEX_HOME": "/workspace/.provider-account/codex",
        },
    }
    runtime._ensure_dirs(worker["worker_id"])
    monkeypatch.setenv("OPENAI_BASE_URL", "https://deployment-gateway.example.test/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-deployment-key")

    command, env = runtime._build_command(
        worker,
        "Use my subscription.",
        runtime._runtime_info(worker),
    )

    joined = "\n".join(command)
    assert 'model_provider="glasshive_openai_compatible"' not in joined
    assert "deployment-gateway.example.test" not in joined
    assert env["CODEX_HOME"] == "/workspace/.provider-account/codex"
    assert "OPENAI_API_KEY" not in env
    assert "OPENAI_BASE_URL" not in env


def test_codex_cli_provider_can_explicitly_lock_down_user_config_and_native_features(tmp_path, monkeypatch):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_locked_down_provider",
        "name": "Locked Down Worker",
        "profile": "codex-cli",
        "model": "gpt-5.2-chat",
    }
    runtime._ensure_dirs(worker["worker_id"])
    monkeypatch.setenv("OPENAI_BASE_URL", "https://models.example.test/openai/v1")
    monkeypatch.setenv("WPR_CODEX_CLI_IGNORE_USER_CONFIG", "1")
    monkeypatch.setenv("WPR_CODEX_CLI_DISABLE_FEATURES", "browser_use,computer_use")

    command, _ = runtime._build_command(worker, "Create the artifact.", runtime._runtime_info(worker))

    joined = "\n".join(command)
    assert "--ignore-user-config" in command
    assert "--disable\nbrowser_use" in joined
    assert "--disable\ncomputer_use" in joined


def test_claude_code_runtime_passes_gateway_headers(tmp_path, monkeypatch):
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_claude_gateway",
        "name": "Claude Worker",
        "profile": "claude-code",
    }
    runtime._ensure_dirs(worker["worker_id"])
    monkeypatch.setenv("WPR_CLAUDE_CODE_USE_API_KEY", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-test")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "claude-oauth-test")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gateway.example")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "gateway-token")
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "x-portkey-provider: anthropic")
    monkeypatch.setenv("ANTHROPIC_DEFAULT_SONNET_MODEL", "claude-sonnet-test")

    command, env = runtime._build_command(worker, "Create the artifact.", runtime._runtime_info(worker))

    assert "--model" in command
    assert env["ANTHROPIC_API_KEY"] == "anthropic-test"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "claude-oauth-test"
    assert env["ANTHROPIC_BASE_URL"] == "https://gateway.example"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "gateway-token"
    assert env["ANTHROPIC_CUSTOM_HEADERS"] == "x-portkey-provider: anthropic"
    assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "claude-sonnet-test"


def test_claude_code_runtime_passes_headless_oauth_without_api_key_mode(tmp_path, monkeypatch):
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_claude_oauth",
        "name": "Claude Worker",
        "profile": "claude-code",
    }
    runtime._ensure_dirs(worker["worker_id"])
    monkeypatch.delenv("WPR_CLAUDE_CODE_USE_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-test")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "claude-oauth-test")

    _command, env = runtime._build_command(worker, "Create the artifact.", runtime._runtime_info(worker))

    assert "ANTHROPIC_API_KEY" not in env
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "claude-oauth-test"


def test_claude_code_runtime_uses_bedrock_provider_model_without_oauth(tmp_path, monkeypatch):
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_claude_bedrock",
        "name": "Claude Bedrock Worker",
        "profile": "claude-code",
        "model": "claude-opus-4-8",
    }
    runtime._ensure_dirs(worker["worker_id"])
    provider_model = (
        "arn:aws:bedrock:us-east-1:123456789012:"
        "application-inference-profile/opus-48-test"
    )
    monkeypatch.setenv("WPR_CLAUDE_CODE_PROVIDER_MODEL", provider_model)
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLEONLY0000")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "synthetic-secret-not-real")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "must-not-pass")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-pass")

    command, env = runtime._build_command(
        worker, "Create the artifact.", runtime._runtime_info(worker)
    )

    assert command[command.index("--model") + 1] == provider_model
    assert env["CLAUDE_CODE_USE_BEDROCK"] == "1"
    assert env["AWS_ACCESS_KEY_ID"] == "AKIAEXAMPLEONLY0000"
    assert env["AWS_SECRET_ACCESS_KEY"] == "synthetic-secret-not-real"
    assert env["AWS_REGION"] == "us-east-1"
    assert env["AWS_EC2_METADATA_DISABLED"] == "true"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert "ANTHROPIC_API_KEY" not in env


def test_host_env_strips_parent_secrets_and_keeps_minimal_runtime_context(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", "callback-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "fake-openai-key")
    monkeypatch.setenv("LIBRECHAT_SECRET", "librechat-secret")
    monkeypatch.setenv("GLASSHIVE_AUTO_DISCOVER_CODEX_WORKSPACE_DEPS", "false")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("USER", "testuser")
    monkeypatch.setenv("LOGNAME", "testuser")
    worker = {
        "worker_id": "wrk_host",
        "name": "Main Host Worker",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "workspaces"),
    }

    env = runtime._host_env(worker, run_id="run-123")

    assert env["PATH"] == "/usr/bin:/bin"
    assert env["GLASSHIVE_WORKER_ID"] == "wrk_host"
    assert env["GLASSHIVE_RUN_ID"] == "run-123"
    assert "VIVENTIUM_GLASSHIVE_CALLBACK_SECRET" not in env
    assert "OPENAI_API_KEY" not in env
    assert "LIBRECHAT_SECRET" not in env
    # USER/LOGNAME must pass through: macOS Keychain-backed CLIs (claude-code's
    # subscription auth) resolve the keychain item by user and report "Not logged in"
    # without them. They are identity, not secrets, so this does not weaken stripping.
    assert env["USER"] == "testuser"
    assert env["LOGNAME"] == "testuser"


def test_host_openclaw_missing_cli_reports_named_binary(tmp_path):
    runtime = HostOpenClawRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = "definitely-missing-openclaw"
    worker = {
        "worker_id": "wrk_openclaw",
        "name": "OpenClaw Host Worker",
        "profile": "openclaw-general",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "workspaces"),
    }

    with pytest.raises(RuntimeDependencyMissingError, match="definitely-missing-openclaw CLI is not installed") as captured:
        runtime.ensure_worker_ready(worker)
    assert captured.value.binary == "definitely-missing-openclaw"
    assert captured.value.profile == "openclaw-general"
    assert captured.value.execution_mode == "host"


def test_runtime_dependency_missing_classification_is_structured_and_sanitized():
    failure = classify_runtime_error(
        RuntimeDependencyMissingError(
            "codex CLI is not installed or not on PATH for host-native codex-cli",
            binary="/private/tmp/secret-path/codex",
            runtime_name="codex-cli",
            profile="codex-cli",
            execution_mode="host",
        ),
        runtime_name="codex-cli",
    )

    assert failure.failure_class == "runtime_dependency_missing"
    assert failure.retryable is False
    assert "`codex`" in failure.user_message
    assert "/private/tmp" not in failure.user_message
    assert "sandbox/workstation" in failure.recommended_recovery


def test_host_runtime_preflight_rejects_configured_version_mismatch(tmp_path, monkeypatch):
    fake_node = tmp_path / "node"
    fake_node.write_text("#!/usr/bin/env bash\necho 'v20.20.2'\n")
    fake_node.chmod(0o755)
    monkeypatch.setenv(
        "GLASSHIVE_HOST_RUNTIME_REQUIREMENTS_JSON",
        json.dumps(
            {
                "codex-cli": [
                    {
                        "binary": str(fake_node),
                        "label": "Node.js",
                        "min_version": "22.19.0",
                    }
                ]
            }
        ),
    )
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = "/bin/echo"

    with pytest.raises(RuntimeDependencyMissingError, match="Node.js") as captured:
        runtime.preflight_worker_profile("codex-cli", "host")

    assert captured.value.required_version == "22.19.0"
    assert captured.value.actual_version == "20.20.2"
    assert captured.value.dependency_label == "Node.js"


def test_host_runtime_preflight_accepts_configured_version(tmp_path, monkeypatch):
    fake_node = tmp_path / "node"
    fake_node.write_text("#!/usr/bin/env bash\necho 'v22.19.0'\n")
    fake_node.chmod(0o755)
    monkeypatch.setenv(
        "GLASSHIVE_HOST_RUNTIME_REQUIREMENTS_JSON",
        json.dumps({"codex-cli": [{"binary": str(fake_node), "label": "Node.js", "min_version": "22.19.0"}]}),
    )
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = "/bin/echo"

    runtime.preflight_worker_profile("codex-cli", "host")


def test_host_runtime_preflight_rejects_codex_below_reviewed_compatibility_floor(
    tmp_path, monkeypatch
):
    fake_codex = tmp_path / "codex"
    fake_codex.write_text("#!/usr/bin/env bash\necho 'codex-cli 0.140.0'\n")
    fake_codex.chmod(0o755)
    monkeypatch.setenv("WPR_CODEX_BIN", str(fake_codex))
    monkeypatch.delenv("GLASSHIVE_HOST_RUNTIME_REQUIREMENTS_JSON", raising=False)
    monkeypatch.delenv("WPR_HOST_RUNTIME_REQUIREMENTS_JSON", raising=False)
    monkeypatch.delenv("GLASSHIVE_HOST_RUNTIME_REQUIREMENTS_FILE", raising=False)
    monkeypatch.delenv("WPR_HOST_RUNTIME_REQUIREMENTS_FILE", raising=False)
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))

    with pytest.raises(RuntimeDependencyMissingError, match="Codex CLI") as captured:
        runtime.preflight_worker_profile("codex-cli", "host")

    assert captured.value.required_version == "0.144.1"
    assert captured.value.actual_version == "0.140.0"
    assert "codex update" in captured.value.recovery_hint


def test_host_runtime_preflight_rejects_default_version_mismatch(tmp_path, monkeypatch):
    fake_claude = tmp_path / "claude"
    fake_claude.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$1\" == \"--version\" ]]; then echo '2.1.100 (Claude Code)'; exit 0; fi\n"
        "echo 'Usage: claude [options] --effort --chrome'\n"
    )
    fake_claude.chmod(0o755)
    monkeypatch.setenv("WPR_CLAUDE_CODE_BIN", str(fake_claude))
    monkeypatch.delenv("GLASSHIVE_HOST_RUNTIME_REQUIREMENTS_JSON", raising=False)
    monkeypatch.delenv("WPR_HOST_RUNTIME_REQUIREMENTS_JSON", raising=False)
    monkeypatch.delenv("GLASSHIVE_HOST_RUNTIME_REQUIREMENTS_FILE", raising=False)
    monkeypatch.delenv("WPR_HOST_RUNTIME_REQUIREMENTS_FILE", raising=False)
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "data"))

    with pytest.raises(RuntimeDependencyMissingError, match="Claude Code") as captured:
        runtime.preflight_worker_profile("claude-code", "host")

    assert captured.value.required_version == "2.1.178"
    assert captured.value.actual_version == "2.1.100"


def test_host_runtime_preflight_rejects_missing_help_capability(tmp_path, monkeypatch):
    fake_claude = tmp_path / "claude"
    fake_claude.write_text("#!/usr/bin/env bash\necho 'Usage: claude [options]'\n")
    fake_claude.chmod(0o755)
    monkeypatch.setenv(
        "GLASSHIVE_HOST_RUNTIME_REQUIREMENTS_JSON",
        json.dumps(
            {
                "claude-code": [
                    {
                        "binary": str(fake_claude),
                        "label": "Claude Code",
                        "required_help_flags": ["--chrome"],
                    }
                ]
            }
        ),
    )
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = "/bin/echo"

    with pytest.raises(RuntimeDependencyMissingError, match="native capability") as captured:
        runtime.preflight_worker_profile("claude-code", "host")

    assert captured.value.dependency_label == "Claude Code"


def test_host_runtime_preflight_accepts_required_mcp_capability(tmp_path, monkeypatch):
    fake_codex = tmp_path / "codex"
    fake_codex.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$1\" == \"mcp\" && \"$2\" == \"list\" ]]; then\n"
        "  echo 'computer-use enabled'\n"
        "  echo 'node_repl enabled'\n"
        "  exit 0\n"
        "fi\n"
        "echo 'codex-cli 0.146.1'\n"
    )
    fake_codex.chmod(0o755)
    monkeypatch.setenv(
        "GLASSHIVE_HOST_RUNTIME_REQUIREMENTS_JSON",
        json.dumps(
            {
                "codex-cli": [
                    {
                        "binary": str(fake_codex),
                        "label": "Codex CLI",
                        "required_mcp_servers": ["computer-use", "node_repl"],
                    }
                ]
            }
        ),
    )
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = "/bin/echo"

    runtime.preflight_worker_profile("codex-cli", "host")


def test_host_claude_preflight_rejects_cli_without_chrome_support(tmp_path, monkeypatch):
    fake_claude = tmp_path / "claude"
    fake_claude.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$1\" == \"--version\" ]]; then echo '2.1.223 (Claude Code)'; exit 0; fi\n"
        "echo 'Usage: claude [options] --effort'\n"
    )
    fake_claude.chmod(0o755)
    monkeypatch.setenv("WPR_CLAUDE_CODE_BIN", str(fake_claude))
    monkeypatch.delenv("GLASSHIVE_HOST_RUNTIME_REQUIREMENTS_JSON", raising=False)
    monkeypatch.delenv("WPR_HOST_RUNTIME_REQUIREMENTS_JSON", raising=False)
    monkeypatch.delenv("WPR_CLAUDE_CODE_ENABLE_CHROME", raising=False)

    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "data"))

    with pytest.raises(RuntimeDependencyMissingError, match="supports --chrome") as captured:
        runtime.preflight_worker_profile("claude-code", "host")

    assert captured.value.dependency_label == "Claude Code"


def test_host_claude_preflight_allows_explicit_chrome_lockdown(tmp_path, monkeypatch):
    fake_claude = tmp_path / "claude"
    fake_claude.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$1\" == \"--version\" ]]; then echo '2.1.223 (Claude Code)'; exit 0; fi\n"
        "echo 'Usage: claude [options] --effort'\n"
    )
    fake_claude.chmod(0o755)
    monkeypatch.setenv("WPR_CLAUDE_CODE_BIN", str(fake_claude))
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", "0")
    monkeypatch.delenv("GLASSHIVE_HOST_RUNTIME_REQUIREMENTS_JSON", raising=False)
    monkeypatch.delenv("WPR_HOST_RUNTIME_REQUIREMENTS_JSON", raising=False)

    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "data"))

    runtime.preflight_worker_profile("claude-code", "host")


def test_host_claude_preflight_rejects_max_effort_without_effort_support(tmp_path, monkeypatch):
    fake_claude = tmp_path / "claude"
    fake_claude.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$1\" == \"--help\" ]]; then echo 'Usage: claude [options] --chrome'; exit 0; fi\n"
        "echo '2.1.223 (Claude Code)'\n"
    )
    fake_claude.chmod(0o755)
    monkeypatch.setenv("WPR_CLAUDE_CODE_BIN", str(fake_claude))
    monkeypatch.setenv("WPR_CLAUDE_CODE_EFFORT", "max")
    monkeypatch.setenv(
        "GLASSHIVE_HOST_RUNTIME_REQUIREMENTS_JSON",
        json.dumps(
            {
                "claude-code": [
                    {
                        "binary": str(fake_claude),
                        "label": "Claude Code",
                        "required_help_flags": ["--chrome"],
                    }
                ]
            }
        ),
    )

    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "data"))

    with pytest.raises(RuntimeDependencyMissingError, match="native --effort") as captured:
        runtime.preflight_worker_profile("claude-code", "host")

    assert captured.value.dependency_label == "Claude Code"


def test_host_codex_runtime_uses_configured_binary_path(tmp_path, monkeypatch):
    fake_codex = tmp_path / "codex"
    fake_codex.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$1\" == \"--version\" ]]; then echo 'codex-cli 0.146.1'; exit 0; fi\n"
        "echo 'codex test'\n"
    )
    fake_codex.chmod(0o755)
    monkeypatch.setenv("WPR_CODEX_BIN", str(fake_codex))
    monkeypatch.delenv("GLASSHIVE_HOST_RUNTIME_REQUIREMENTS_JSON", raising=False)

    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))

    assert runtime.binary == str(fake_codex)
    runtime.preflight_worker_profile("codex-cli", "host")


def test_cli_failure_classifies_runtime_version_substrate():
    failure = classify_cli_failure(
        stdout="",
        stderr="It failed. The local worker runtime needs Node.js v22.19+ and this machine is on v20.20.2.",
        runtime_name="codex-cli",
        exit_code=1,
    )

    assert failure.failure_class == "runtime_dependency_missing"
    assert failure.retryable is False
    assert "sandbox/workstation" in failure.recommended_recovery


def test_cli_failure_classifies_missing_executable_substrate():
    failure = classify_cli_failure(
        stdout="",
        stderr=(
            "codex-cli exited with code 127: "
            "/workspace/.wpr-home/.glasshive-runs/run_demo/run.sh: line 15: "
            "/Applications/Codex.app/Contents/Resources/codex: No such file or directory"
        ),
        runtime_name="codex-cli",
        exit_code=127,
    )

    assert failure.failure_class == "runtime_dependency_missing"
    assert failure.retryable is False
    assert "configured managed dependency" in failure.recommended_recovery


def test_cli_failure_classifies_not_logged_in_provider_session():
    failure = classify_cli_failure(
        stdout=json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": True,
                "result": "Not logged in · Please run /login",
            }
        ),
        stderr="",
        runtime_name="claude-code",
        exit_code=1,
    )

    assert failure.failure_class == "provider_auth_missing"
    assert failure.retryable is False
    assert "provider credentials" in failure.user_message
    assert "CLI login" in failure.recommended_recovery


def test_runtime_error_classifies_missing_executable_substrate():
    failure = classify_runtime_error(
        RuntimeErrorBase(
            "codex-cli exited with code 127: "
            "/workspace/.wpr-home/.glasshive-runs/run_demo/run.sh: line 15: "
            "/Applications/Codex.app/Contents/Resources/codex: No such file or directory"
        ),
        runtime_name="codex-cli",
    )

    assert failure.failure_class == "runtime_dependency_missing"
    assert failure.retryable is False
    assert "missing, unavailable, or incompatible" in failure.user_message


def test_runtime_error_classifies_not_logged_in_provider_session():
    failure = classify_runtime_error(
        RuntimeErrorBase('claude-code exited with code 1: {"result":"Not logged in · Please run /login"}'),
        runtime_name="claude-code",
    )

    assert failure.failure_class == "provider_auth_missing"
    assert failure.retryable is False
    assert "CLI login" in failure.recommended_recovery


def test_runtime_error_classifies_unsupported_runtime_configuration():
    failure = classify_runtime_error(
        RuntimeErrorBase("host-native workers are disabled in this deployment"),
        runtime_name="codex-cli",
    )

    assert failure.failure_class == "unsupported_runtime_configuration"
    assert failure.retryable is False
    assert "host-native workers are disabled" in failure.user_message


def test_cli_failure_does_not_classify_generic_file_not_found_as_runtime_dependency():
    failure = classify_cli_failure(
        stdout="",
        stderr="The requested uploaded source file was missing: No such file or directory",
        runtime_name="codex-cli",
        exit_code=1,
    )

    assert failure.failure_class == "unknown"
    assert failure.retryable is False


def test_cli_failure_classifies_missing_python_module_as_runtime_dependency():
    failure = classify_cli_failure(
        stdout=(
            "Traceback (most recent call last):\n"
            "  File \"<stdin>\", line 1, in <module>\n"
            "ModuleNotFoundError: No module named 'requests'\n"
        ),
        stderr="",
        runtime_name="codex-cli",
        exit_code=1,
    )

    assert failure.failure_class == "runtime_dependency_missing"
    assert failure.retryable is False
    assert "managed dependency" in failure.recommended_recovery


def test_runtime_error_does_not_classify_generic_file_not_found_as_runtime_dependency():
    failure = classify_runtime_error(
        FileNotFoundError("Bootstrap source file not found: /Users/example/private-upload.pdf"),
        runtime_name="codex-cli",
    )

    assert failure.failure_class == "runtime_error"
    assert failure.retryable is False
    assert "/Users/example" not in failure.diagnostic_summary
    assert "[local path]" in failure.diagnostic_summary


def test_runtime_error_classifies_glasshive_evidence_failure():
    failure = classify_runtime_error(
        RuntimeErrorBase("GlassHive evidence check failed: completion compliance failed: missing pdf"),
        runtime_name="codex-cli",
    )

    assert failure.failure_class == "glasshive_evidence_check_failed"
    assert failure.retryable is True
    assert "workspace_continue" in failure.recommended_recovery


def test_runtime_error_classifies_sandbox_lifecycle_failure():
    failure = classify_runtime_error(
        RuntimeErrorBase(
            "Failed to prepare writable sandbox paths in wpr-wrk-example: "
            "Error response from daemon: No such container: wpr-wrk-example"
        ),
        runtime_name="codex-cli",
    )

    assert failure.failure_class == "runtime_sandbox_unavailable"
    assert failure.retryable is True
    assert "sandbox/workstation" in failure.user_message


def test_cli_failure_classifies_sigterm_as_runtime_terminated():
    failure = classify_cli_failure(
        stdout="",
        stderr="",
        runtime_name="claude-code",
        exit_code=143,
    )

    assert failure.failure_class == "runtime_terminated"
    assert failure.retryable is False
    assert "workspace_continue" in failure.recommended_recovery


def test_openclaw_session_id_is_cli_safe_when_worker_session_key_uses_glasshive_colons(tmp_path):
    runtime = OpenClawWorkstationRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_openclaw",
        "name": "OpenClaw Worker",
        "profile": "openclaw-general",
        "session_key": "agent:main:wpr:worker:wrk_openclaw",
    }

    assert runtime._default_session_key(worker) == "wpr-worker-wrk_openclaw"

    info = runtime._runtime_info(worker)
    command, env = runtime._build_command(worker, "Create a file.", info)

    assert command[command.index("--session-id") + 1] == "wpr-worker-wrk_openclaw"
    assert env["OPENCLAW_MODEL"]


def test_openclaw_can_scope_session_key_per_run(tmp_path, monkeypatch):
    runtime = OpenClawWorkstationRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_openclaw",
        "name": "OpenClaw Worker",
        "profile": "openclaw-general",
        "_active_run_id": "run_abc123",
    }
    monkeypatch.setenv("WPR_OPENCLAW_SESSION_SCOPE", "run")

    assert runtime._default_session_key(worker) == "wpr-worker-wrk_openclaw-run_abc123"

    info = runtime._runtime_info(worker)
    command, env = runtime._build_command(worker, "Create a file.", info)

    assert command[command.index("--session-id") + 1] == "wpr-worker-wrk_openclaw-run_abc123"
    assert env


def test_openclaw_neutralizes_default_onboarding_bootstrap_for_task_runs(tmp_path):
    runtime = OpenClawWorkstationRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_openclaw_bootstrap",
        "name": "OpenClaw Worker",
        "profile": "openclaw-general",
    }
    workspace = runtime._workspace_dir(worker["worker_id"])
    workspace.mkdir(parents=True)
    bootstrap_path = workspace / "BOOTSTRAP.md"
    bootstrap_path.write_text(
        "\n".join(
            [
                "# BOOTSTRAP.md - Hello, World",
                "",
                "_You just woke up. Time to figure out who you are._",
                "",
                "Start with something like:",
                "",
                '> "Hey. I just came online. Who am I? Who are you?"',
                "",
            ]
        )
    )

    runtime._build_command(worker, "Create the requested artifact.", runtime._runtime_info(worker))

    rewritten = bootstrap_path.read_text()
    assert "GlassHive Task Mode" in rewritten
    assert "Do not start first-run identity onboarding" in rewritten
    assert "prefer localhost HTTP URLs over file:// URLs" in rewritten
    archived = workspace / ".glasshive" / "archived-openclaw-default-bootstrap.md"
    assert archived.exists()
    assert "Hello, World" in archived.read_text()


def test_openclaw_provisions_task_bootstrap_before_cli_can_create_onboarding(tmp_path):
    runtime = OpenClawWorkstationRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_openclaw_no_bootstrap",
        "name": "OpenClaw Worker",
        "profile": "openclaw-general",
    }
    bootstrap_path = runtime._workspace_dir(worker["worker_id"]) / "BOOTSTRAP.md"
    assert not bootstrap_path.exists()

    runtime._build_command(worker, "Create the requested artifact.", runtime._runtime_info(worker))

    text = bootstrap_path.read_text()
    assert "GlassHive Task Mode" in text
    assert "Follow the latest runtime-provided instruction" in text
    assert "prefer localhost HTTP URLs over file:// URLs" in text


def test_openclaw_starts_gateway_screen_session_for_browser_tools(tmp_path, monkeypatch):
    class FakeSandbox:
        home_mount = "/workspace/.wpr-home"
        workspace_mount = "/workspace/project"
        term_value = "xterm-256color"
        display_value = ":99.0"

        def __init__(self) -> None:
            self.started: list[dict[str, object]] = []
            self.execs: list[dict[str, object]] = []

        def paths(self, worker_id: str) -> dict[str, Path]:
            root = tmp_path / "data" / "docker_sandboxes" / "workers" / worker_id / "state"
            return {
                "state_dir": root,
                "workspace_dir": root / "workspace",
                "home_dir": root / "home",
                "worker_root": root.parent,
            }

        def start_screen_session(self, worker_id, runtime_name, session_name, command, *, env=None, worker=None):
            self.started.append(
                {
                    "worker_id": worker_id,
                    "runtime_name": runtime_name,
                    "session_name": session_name,
                    "command": command,
                    "env": env,
                    "worker": worker,
                }
            )
            return subprocess.CompletedProcess(command, 0, "", "")

        def _docker_exec(self, container_name, command, *, env=None, cwd=None, **kwargs):
            self.execs.append({"container_name": container_name, "command": command, "env": env, "cwd": cwd, "kwargs": kwargs})
            return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setenv("OPENAI_BASE_URL", "https://models.example.test/openai/v1")
    monkeypatch.setenv("WPR_OPENCLAW_START_GATEWAY", "true")
    runtime = OpenClawWorkstationRuntime(base_dir=str(tmp_path / "data"))
    fake = FakeSandbox()
    runtime.sandbox = fake
    worker = {"worker_id": "wrk_openclaw_gateway", "name": "OpenClaw Worker", "profile": "openclaw-general"}
    sandbox_info = type("SandboxInfo", (), {"container_name": "wpr-wrk-openclaw-gateway"})()

    runtime._start_openclaw_gateway(worker, sandbox_info)

    assert fake.started[0]["session_name"] == "openclaw-gateway"
    assert "openclaw gateway --port 18789" in " ".join(fake.started[0]["command"])
    assert fake.started[0]["env"]["OPENCLAW_CONFIG_PATH"] == "/workspace/.wpr-home/.wpr-openclaw/openclaw.json"
    assert fake.execs[0]["container_name"] == "wpr-wrk-openclaw-gateway"


def test_openclaw_task_runs_do_not_start_gateway(tmp_path, monkeypatch):
    class FakeSandbox:
        home_mount = "/workspace/.wpr-home"
        workspace_mount = "/workspace/project"
        term_value = "xterm-256color"
        display_value = ":99.0"

        def __init__(self) -> None:
            self.started: list[dict[str, object]] = []

        def paths(self, worker_id: str) -> dict[str, Path]:
            root = tmp_path / "data" / "docker_sandboxes" / "workers" / worker_id / "state"
            return {
                "state_dir": root,
                "workspace_dir": root / "workspace",
                "home_dir": root / "home",
                "worker_root": root.parent,
            }

        def ensure_ready(self, worker, runtime_name, **kwargs):
            return type("SandboxInfo", (), {"container_name": "wpr-wrk-openclaw-task", "pid": 123})()

        def start_screen_session(self, worker_id, runtime_name, session_name, command, *, env=None, worker=None):
            self.started.append({"session_name": session_name, "command": command, "worker": worker})
            return subprocess.CompletedProcess(command, 0, "", "")

    runtime = OpenClawWorkstationRuntime(base_dir=str(tmp_path / "data"))
    fake = FakeSandbox()
    runtime.sandbox = fake
    worker = {"worker_id": "wrk_openclaw_task", "name": "OpenClaw Worker", "profile": "openclaw-general"}

    info = runtime.ensure_worker_ready({**worker, "_glasshive_task_run": True})

    assert info.runtime == "openclaw"
    assert fake.started == []


def test_openclaw_gateway_is_opt_in_for_worker_readiness(tmp_path):
    class FakeSandbox:
        home_mount = "/workspace/.wpr-home"
        workspace_mount = "/workspace/project"
        term_value = "xterm-256color"
        display_value = ":99.0"

        def __init__(self) -> None:
            self.started: list[dict[str, object]] = []

        def paths(self, worker_id: str) -> dict[str, Path]:
            root = tmp_path / "data" / "docker_sandboxes" / "workers" / worker_id / "state"
            return {
                "state_dir": root,
                "workspace_dir": root / "workspace",
                "home_dir": root / "home",
                "worker_root": root.parent,
            }

        def ensure_ready(self, worker, runtime_name, **kwargs):
            return type("SandboxInfo", (), {"container_name": "wpr-wrk-openclaw-ready", "pid": 123})()

        def start_screen_session(self, worker_id, runtime_name, session_name, command, *, env=None, worker=None):
            self.started.append({"session_name": session_name, "command": command})
            return subprocess.CompletedProcess(command, 0, "", "")

    runtime = OpenClawWorkstationRuntime(base_dir=str(tmp_path / "data"))
    fake = FakeSandbox()
    runtime.sandbox = fake

    runtime.ensure_worker_ready({"worker_id": "wrk_openclaw_ready", "name": "OpenClaw Worker", "profile": "openclaw-general"})

    assert fake.started == []


def test_openclaw_desktop_action_does_not_start_gateway(tmp_path):
    class FakeSandbox:
        def __init__(self) -> None:
            self.ensure_calls: list[dict[str, object]] = []
            self.desktop_actions: list[dict[str, object]] = []

        def ensure_ready(self, worker, runtime_name, **kwargs):
            self.ensure_calls.append({"worker": worker, "runtime_name": runtime_name, **kwargs})
            return type("SandboxInfo", (), {"container_name": "wpr-wrk-openclaw-action", "pid": 123})()

        def desktop_action(self, worker_id, runtime_name, action, *, url=None, session_name=None, worker=None):
            self.desktop_actions.append(
                {
                    "worker_id": worker_id,
                    "runtime_name": runtime_name,
                    "action": action,
                    "url": url,
                    "session_name": session_name,
                    "worker": worker,
                }
            )
            return {"action": action, "status": "launched", "view_url": "http://127.0.0.1:7900"}

        def start_screen_session(self, *args, **kwargs):  # pragma: no cover - failure path
            raise AssertionError("desktop_action must not start the OpenClaw gateway")

    runtime = OpenClawWorkstationRuntime(base_dir=str(tmp_path / "data"))
    fake = FakeSandbox()
    runtime.sandbox = fake
    worker = {"worker_id": "wrk_openclaw_action", "name": "OpenClaw Worker", "profile": "openclaw-general"}

    launched = runtime.desktop_action(worker, "browser", url="about:blank")

    assert launched["status"] == "launched"
    assert fake.ensure_calls == []
    assert fake.desktop_actions[0]["action"] == "browser"
    assert fake.desktop_actions[0]["url"] == "about:blank"


def test_openclaw_projects_openai_compatible_provider_without_storing_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://models.example.test/openai/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret-test-value")
    runtime = OpenClawWorkstationRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_openclaw_provider",
        "name": "OpenClaw Worker",
        "profile": "openclaw-general",
        "model": "openai/gpt-5.2",
    }

    runtime._write_gateway_config(worker, "token")
    config = json.loads(runtime._openclaw_config_path(worker["worker_id"]).read_text())

    assert config["gateway"] == {"mode": "local", "bind": "loopback", "port": 18789, "auth": {"mode": "none"}}
    assert config["agents"]["defaults"]["workspace"] == "/workspace/project"
    assert config["agents"]["defaults"]["repoRoot"] == "/workspace/project"
    assert config["agents"]["defaults"]["model"]["primary"] == "glasshive-openai-compatible/gpt-5.2"
    provider = config["models"]["providers"]["glasshive-openai-compatible"]
    assert provider["baseUrl"] == "https://models.example.test/openai/v1"
    assert provider["api"] == "openai-completions"
    assert provider["apiKey"] == {"source": "env", "provider": "default", "id": "OPENAI_API_KEY"}
    assert provider["models"][0]["id"] == "gpt-5.2"
    assert "openai-secret-test-value" not in json.dumps(config)

    info = runtime._runtime_info(worker)
    command, env = runtime._build_command(worker, "Create a file.", info)

    assert env["OPENCLAW_MODEL"] == "glasshive-openai-compatible/gpt-5.2"
    assert env["OPENAI_BASE_URL"] == "https://models.example.test/openai/v1"
    assert env["OPENAI_API_KEY"] == "openai-secret-test-value"
    assert command[command.index("--session-id") + 1] == "wpr-worker-wrk_openclaw_provider"


def test_openclaw_uses_configured_openai_models_for_compatible_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://models.example.test/openai/v1")
    monkeypatch.setenv("OPENAI_MODELS", "gpt-5.2-chat,gpt-5.2")
    runtime = OpenClawWorkstationRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_openclaw_models",
        "name": "OpenClaw Worker",
        "profile": "openclaw-general",
    }

    assert runtime._openclaw_model_for_worker(worker) == "glasshive-openai-compatible/gpt-5.2-chat"

    runtime._write_gateway_config(worker, "token")
    config = json.loads(runtime._openclaw_config_path(worker["worker_id"]).read_text())

    assert config["agents"]["defaults"]["model"]["primary"] == "glasshive-openai-compatible/gpt-5.2-chat"
    assert config["models"]["providers"]["glasshive-openai-compatible"]["models"][0]["id"] == "gpt-5.2-chat"


def test_openclaw_projects_portkey_headers_as_secret_refs(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTKEY_BASE_URL", "https://api.portkey.example/v1")
    monkeypatch.setenv("PORTKEY_API_KEY", "portkey-secret-test-value")
    monkeypatch.setenv("PORTKEY_VIRTUAL_KEY", "virtual-key-secret")
    runtime = OpenClawWorkstationRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_openclaw_portkey",
        "name": "OpenClaw Worker",
        "profile": "openclaw-general",
        "model": "anthropic/claude-sonnet-4-6",
    }

    runtime._write_gateway_config(worker, "token")
    config = json.loads(runtime._openclaw_config_path(worker["worker_id"]).read_text())

    assert config["agents"]["defaults"]["model"]["primary"] == (
        "glasshive-portkey-compatible/anthropic/claude-sonnet-4-6"
    )
    provider = config["models"]["providers"]["glasshive-portkey-compatible"]
    assert provider["apiKey"] == {"source": "env", "provider": "default", "id": "PORTKEY_API_KEY"}
    assert provider["headers"]["x-portkey-virtual-key"] == {
        "source": "env",
        "provider": "default",
        "id": "PORTKEY_VIRTUAL_KEY",
    }
    serialized = json.dumps(config)
    assert "portkey-secret-test-value" not in serialized
    assert "virtual-key-secret" not in serialized


@pytest.mark.parametrize("max_tokens_field", ["max_completion_tokens", "max_tokens"])
def test_openclaw_projects_can_configure_openai_compat_max_token_field(tmp_path, monkeypatch, max_tokens_field):
    monkeypatch.setenv("PORTKEY_BASE_URL", "https://api.portkey.example/v1")
    monkeypatch.setenv("PORTKEY_API_KEY", "portkey-secret-test-value")
    monkeypatch.setenv("WPR_OPENCLAW_MODEL_ID", "@example/gpt-deployment-chat")
    monkeypatch.setenv("WPR_OPENCLAW_MODEL_NAME", "@example/gpt-deployment-chat")
    monkeypatch.setenv("WPR_OPENCLAW_COMPAT_MAX_TOKENS_FIELD", max_tokens_field)
    runtime = OpenClawWorkstationRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_openclaw_portkey_azure",
        "name": "OpenClaw Worker",
        "profile": "openclaw-general",
        "model": "@example/gpt-deployment-chat",
    }

    runtime._write_gateway_config(worker, "token")
    config = json.loads(runtime._openclaw_config_path(worker["worker_id"]).read_text())

    assert config["agents"]["defaults"]["model"]["primary"] == (
        "glasshive-portkey-compatible/@example/gpt-deployment-chat"
    )
    model_entry = config["models"]["providers"]["glasshive-portkey-compatible"]["models"][0]
    assert model_entry["id"] == "@example/gpt-deployment-chat"
    assert model_entry["name"] == "@example/gpt-deployment-chat"
    assert model_entry["compat"]["maxTokensField"] == max_tokens_field
    assert "portkey-secret-test-value" not in json.dumps(config)


def test_openclaw_projects_ignore_unknown_compat_max_token_field(tmp_path, monkeypatch):
    monkeypatch.setenv("PORTKEY_BASE_URL", "https://api.portkey.example/v1")
    monkeypatch.setenv("PORTKEY_API_KEY", "portkey-secret-test-value")
    monkeypatch.setenv("WPR_OPENCLAW_MODEL_ID", "@example/gpt-deployment-chat")
    monkeypatch.setenv("WPR_OPENCLAW_MODEL_NAME", "@example/gpt-deployment-chat")
    monkeypatch.setenv("WPR_OPENCLAW_COMPAT_MAX_TOKENS_FIELD", "bogus")
    runtime = OpenClawWorkstationRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_openclaw_portkey_invalid_compat",
        "name": "OpenClaw Worker",
        "profile": "openclaw-general",
        "model": "@example/gpt-deployment-chat",
    }

    runtime._write_gateway_config(worker, "token")
    config = json.loads(runtime._openclaw_config_path(worker["worker_id"]).read_text())

    model_entry = config["models"]["providers"]["glasshive-portkey-compatible"]["models"][0]
    assert "compat" not in model_entry
    assert "portkey-secret-test-value" not in json.dumps(config)


def test_redact_text_masks_parent_visible_secret_shapes():
    synthetic_openai_token = "sk-" + "abc123456789xyz"
    synthetic_bearer = "abcdef" + "ghijklmnopqrstuvwxyz"
    synthetic_aws_access_key = "AKIA" + "EXAMPLEONLY00000"
    redacted = _redact_text(
        f"Authorization: {'Bearer'} {synthetic_bearer} token=super-secret-value "
        f"{synthetic_openai_token} {synthetic_aws_access_key}"
    )
    assert "abcdefghijklmnopqrstuvwxyz" not in redacted
    assert "super-secret-value" not in redacted
    assert synthetic_openai_token not in redacted
    assert synthetic_aws_access_key not in redacted
    assert "[REDACTED_AWS_ACCESS_KEY]" in redacted
    assert "[REDACTED]" in redacted


def test_redact_text_masks_common_host_paths_and_credential_families():
    private_key = (
        "-----BEGIN "
        "PRIVATE KEY-----\n"
        "c3ludGhldGljLXByaXZhdGUta2V5LW1hdGVyaWFs\n"
        "-----END PRIVATE KEY-----"
    )
    raw = " ".join(
        [
            "/home/synthetic/private.txt",
            "/root/private.txt",
            "/Volumes/Private/private.txt",
            "/private/var/synthetic/private.txt",
            "ghp_syntheticgithubcredential",
            "xoxb-synthetic-slack-credential",
            "eyJhbGciOiJIUzI1NiJ9.c3ludGhldGlj.c2lnbmF0dXJl",
            private_key,
        ]
    )

    redacted = _redact_text(raw)

    for forbidden in (
        "/home/synthetic",
        "/root/private.txt",
        "/Volumes/Private",
        "/private/var/synthetic",
        "syntheticgithubcredential",
        "synthetic-slack-credential",
        "c3ludGhldGlj",
        "c3ludGhldGljLXByaXZhdGUta2V5LW1hdGVyaWFs",
    ):
        assert forbidden not in redacted


def test_redact_text_fails_closed_for_an_unterminated_private_key():
    private_key_body = "A" * 120
    redacted = _redact_text(
        "Safe prefix.\n-----BEGIN PRIVATE KEY-----\n"
        f"{private_key_body}\n"
        "untrusted trailing text"
    )

    assert "Safe prefix." in redacted
    assert "BEGIN PRIVATE KEY" not in redacted
    assert private_key_body not in redacted
    assert "untrusted trailing text" not in redacted
    assert "[REDACTED_PRIVATE_KEY]" in redacted


def test_redact_text_masks_parent_visible_image_payloads():
    base64_png = "iVBORw0KGgo" + ("A" * 900) + "=="
    redacted = _redact_text(
        '{"type":"tool_result","content":[{"type":"image","mimeType":"image/png","data":"'
        + base64_png
        + '"}]}'
    )

    assert base64_png not in redacted
    assert "[REDACTED_LONG_BASE64]" in redacted


def test_host_conversation_mode_uses_exact_workspace_without_scaffolding(tmp_path):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "private-state"))
    life = tmp_path / "Life"
    life.mkdir()
    canonical_agents = "# Personal LIFE instructions\n"
    (life / "AGENTS.md").write_text(canonical_agents)
    worker = {
        "worker_id": "wrk_conversation",
        "name": "Viventium Main",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(life),
        "bootstrap_bundle_json": json.dumps(
            {"run_mode": "conversation", "provider_model": "gpt-5.6-sol", "access_mode": "full"}
        ),
    }

    workspace = runtime._host_workspace_dir(worker)
    runtime._materialize_workspace(worker, workspace)
    instruction = runtime._command_stdin_text(worker, "Could you help me think?", runtime._host_runtime_info(worker))

    assert workspace == life
    assert instruction == "Could you help me think?"
    assert (life / "AGENTS.md").read_text() == canonical_agents
    assert sorted(path.name for path in life.iterdir()) == ["AGENTS.md"]
    for forbidden in ("CLAUDE.md", "CODEX.md", "project-definition.md", "work-log.md", "harness-prompt.md", ".git", "glasshive-run"):
        assert not (life / forbidden).exists()


def test_host_codex_conversation_can_exclude_workspace_project_instructions(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "WPR_CODEX_CLI_CONVERSATION_PROJECT_INSTRUCTIONS",
        "exclude",
    )
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "private-state"))
    life = tmp_path / "Life"
    life.mkdir()
    (life / "AGENTS.md").write_text("Mission-only project instructions.\n")
    worker = {
        "worker_id": "wrk_conversation_without_project_instructions",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(life),
        "bootstrap_bundle_json": json.dumps(
            {
                "run_mode": "conversation",
                "provider_model": "gpt-5.6-sol",
                "access_mode": "full",
            }
        ),
    }

    command, _ = runtime._build_command(
        worker,
        "Talk naturally.",
        runtime._host_runtime_info(worker),
    )

    primary = Path(command[command.index("-C") + 1])
    assert primary != life
    assert primary.is_dir()
    assert not (primary / "AGENTS.md").exists()
    assert command[command.index("--add-dir") + 1] == str(life)


def test_host_capacity_reserves_an_independent_interactive_lane_per_cli_profile(tmp_path):
    class ActiveProcess:
        @staticmethod
        def poll():
            return None

    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "private-state"))
    mission = {
        "worker_id": "wrk_mission_busy",
        "bootstrap_bundle_json": json.dumps({"run_mode": "mission"}),
    }
    conversation = {
        "worker_id": "wrk_conversation_waiting",
        "bootstrap_bundle_json": json.dumps({"run_mode": "conversation"}),
    }
    runtime._host_active_slots()["mission"] = mission["worker_id"]
    runtime._active_processes[mission["worker_id"]] = ActiveProcess()

    assert runtime.worker_capacity_error(conversation) is None

    runtime._host_active_slots()["conversation"] = "wrk_conversation_active"
    runtime._active_processes["wrk_conversation_active"] = ActiveProcess()
    error = runtime.worker_capacity_error(conversation)
    assert error is not None
    assert "active conversation worker" in str(error)


def test_provider_activity_log_reads_incrementally_and_marks_a_bounded_tail(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_PROVIDER_LOG_WINDOW_BYTES", "1024")
    runtime = ProfiledWorkerRuntime(base_dir=str(tmp_path / "private-state"))
    worker = {
        "worker_id": "wrk_provider_log",
        "profile": "codex-cli",
        "execution_mode": "host",
    }
    run_id = "run-provider-log"
    run_root = runtime.host_codex._run_root(worker["worker_id"], run_id)
    run_root.mkdir(parents=True)
    stdout = run_root / "stdout.log"
    stdout.write_text("\n".join(json.dumps({"type": "event", "index": i}) for i in range(80)) + "\n")

    profile, first = runtime.provider_activity_log(worker, run_id)
    stdout.write_text(stdout.read_text() + json.dumps({"type": "turn.completed"}) + "\n")
    _, second = runtime.provider_activity_log(worker, run_id)
    _, cached = runtime.provider_activity_log(worker, run_id)

    assert profile == "codex-cli"
    assert json.loads(first.splitlines()[0])["type"] == "glasshive.log_compacted"
    assert "turn.completed" in second
    assert cached == second


def test_host_conversation_broker_config_stays_in_private_worker_state(tmp_path, monkeypatch):
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", "0")
    source_codex_home = tmp_path / "source-codex"
    (source_codex_home / "skills").mkdir(parents=True)
    (source_codex_home / "plugins" / "cache").mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(source_codex_home))
    source_claude_home = tmp_path / "source-claude"
    (source_claude_home / "plugins" / "cache").mkdir(parents=True)
    (source_claude_home / "plugins" / "marketplaces").mkdir(parents=True)
    (source_claude_home / "plugins" / "installed_plugins.json").write_text("{}\n")
    monkeypatch.setenv("GLASSHIVE_HOST_CLAUDE_CONFIG", str(source_claude_home))
    life = tmp_path / "Life"
    life.mkdir()
    codex_runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "codex-private-state"))
    codex_worker = {
        "worker_id": "wrk_conversation_codex_broker",
        "name": "Viventium Main",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(life),
        "bootstrap_bundle_json": json.dumps(
            {
                "run_mode": "conversation",
                "provider_model": "gpt-5.6-sol",
                "access_mode": "full",
                "codex_config_append": "[mcp_servers.synthetic]\nurl = \"http://127.0.0.1.invalid/mcp\"",
            }
        ),
    }
    codex_workspace = codex_runtime._host_workspace_dir(codex_worker)
    codex_runtime._materialize_workspace(codex_worker, codex_workspace)
    codex_command, codex_env = codex_runtime._build_command(
        codex_worker,
        "Use the declared tool.",
        codex_runtime._host_runtime_info(codex_worker),
    )

    codex_config = codex_runtime._host_codex_home(codex_worker) / "config.toml"
    assert codex_config.is_file()
    assert "mcp_servers.synthetic" in codex_config.read_text()
    assert codex_env["CODEX_HOME"] == str(codex_config.parent)
    assert (codex_config.parent / "skills").resolve() == (source_codex_home / "skills").resolve()
    assert (codex_config.parent / "plugins" / "cache").resolve() == (
        source_codex_home / "plugins" / "cache"
    ).resolve()
    assert codex_command[:4] == ["codex", "exec", "--json", "--skip-git-repo-check"]
    assert not (life / ".codex").exists()

    claude_runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "claude-private-state"))
    claude_worker = {
        "worker_id": "wrk_conversation_claude_broker",
        "name": "Viventium Main",
        "profile": "claude-code",
        "execution_mode": "host",
        "workspace_root": str(life),
        "model": "opus",
        "bootstrap_bundle_json": json.dumps(
            {
                "run_mode": "conversation",
                "provider_model": "opus",
                "access_mode": "full",
                "developer_instructions": "Current Viventium authority.",
                "claude_project_mcp": {
                    "synthetic": {"type": "http", "url": "http://127.0.0.1.invalid/mcp"}
                },
            }
        ),
    }
    claude_workspace = claude_runtime._host_workspace_dir(claude_worker)
    claude_runtime._materialize_workspace(claude_worker, claude_workspace)
    claude_command, claude_env = claude_runtime._build_command(
        claude_worker,
        "Use the declared tool.",
        claude_runtime._host_runtime_info(claude_worker),
    )
    mcp_path = claude_runtime._state_dir(claude_worker["worker_id"]) / "conversation-mcp.json"

    assert mcp_path.is_file()
    assert claude_command[claude_command.index("--mcp-config") + 1] == str(mcp_path)
    assert "--strict-mcp-config" in claude_command
    authority_path = (
        claude_runtime._state_dir(claude_worker["worker_id"])
        / "conversation-developer-instructions.md"
    )
    assert claude_command[claude_command.index("--append-system-prompt-file") + 1] == str(
        authority_path
    )
    assert authority_path.read_text() == "Current Viventium authority.\n"
    assert authority_path.stat().st_mode & 0o777 == 0o600
    assert claude_env["CLAUDE_CONFIG_DIR"].startswith(str(tmp_path / "claude-private-state"))
    claude_home = Path(claude_env["CLAUDE_CONFIG_DIR"])
    assert (claude_home / "plugins" / "cache").resolve() == (
        source_claude_home / "plugins" / "cache"
    ).resolve()
    assert (claude_home / "plugins" / "marketplaces").resolve() == (
        source_claude_home / "plugins" / "marketplaces"
    ).resolve()
    assert json.loads((claude_home / "plugins" / "installed_plugins.json").read_text()) == {}
    assert not (claude_home / "plugins" / "data").exists()
    assert not (life / ".mcp.json").exists()
    assert not (life / ".claude").exists()


def test_host_capability_projection_adds_missing_entries_to_existing_worker_catalogs(
    tmp_path,
    monkeypatch,
):
    source_codex_home = tmp_path / "source-codex"
    source_skill = source_codex_home / "skills" / "synthetic-skill"
    source_skill.mkdir(parents=True)
    (source_skill / "SKILL.md").write_text("# Synthetic skill\n")
    source_plugin = source_codex_home / "plugins" / "cache" / "synthetic-plugin-family"
    source_plugin.mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(source_codex_home))

    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "private-state"))
    worker = {
        "worker_id": "wrk_existing_catalog",
        "name": "Existing Main",
        "profile": "codex-cli",
        "execution_mode": "host",
    }
    target_home = runtime._host_codex_home(worker)
    existing_skill = target_home / "skills" / ".system"
    existing_skill.mkdir(parents=True)
    (existing_skill / "local-marker").write_text("preserve\n")
    existing_plugin = target_home / "plugins" / "cache" / "worker-local-family"
    existing_plugin.mkdir(parents=True)

    runtime._project_host_codex_capability_roots(target_home)

    assert (existing_skill / "local-marker").read_text() == "preserve\n"
    assert existing_plugin.is_dir()
    assert (target_home / "skills" / "synthetic-skill").is_symlink()
    assert (target_home / "skills" / "synthetic-skill").resolve() == source_skill.resolve()
    assert (target_home / "plugins" / "cache" / "synthetic-plugin-family").is_symlink()
    assert (
        target_home / "plugins" / "cache" / "synthetic-plugin-family"
    ).resolve() == source_plugin.resolve()


def test_claude_capability_projection_merges_registries_without_replacing_worker_choices(
    tmp_path,
    monkeypatch,
):
    source_home = tmp_path / "source-claude"
    source_plugins = source_home / "plugins"
    source_plugins.mkdir(parents=True)
    (source_plugins / "installed_plugins.json").write_text(
        json.dumps(
            {
                "version": 2,
                "plugins": {
                    "host-only": {"enabled": True},
                    "shared": {"source": "host"},
                },
            }
        )
    )
    (source_plugins / "known_marketplaces.json").write_text(
        json.dumps({"host-market": {"path": "/synthetic/host-market"}})
    )
    monkeypatch.setenv("GLASSHIVE_HOST_CLAUDE_CONFIG", str(source_home))

    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "private-state"))
    target_home = tmp_path / "worker-claude"
    target_plugins = target_home / "plugins"
    target_plugins.mkdir(parents=True)
    (target_plugins / "installed_plugins.json").write_text(
        json.dumps(
            {
                "version": 1,
                "plugins": {
                    "worker-only": {"enabled": True},
                    "shared": {"source": "worker"},
                },
            }
        )
    )
    (target_plugins / "known_marketplaces.json").write_text(
        json.dumps({"worker-market": {"path": "/synthetic/worker-market"}})
    )

    runtime._project_host_claude_capability_roots(target_home)

    installed = json.loads((target_plugins / "installed_plugins.json").read_text())
    marketplaces = json.loads((target_plugins / "known_marketplaces.json").read_text())
    assert installed["version"] == 1
    assert installed["plugins"]["shared"] == {"source": "worker"}
    assert installed["plugins"]["worker-only"] == {"enabled": True}
    assert installed["plugins"]["host-only"] == {"enabled": True}
    assert marketplaces == {
        "host-market": {"path": "/synthetic/host-market"},
        "worker-market": {"path": "/synthetic/worker-market"},
    }


def test_codex_resume_flags_change_only_for_conversation_mode(tmp_path):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "private-state"))
    life = tmp_path / "Life"
    life.mkdir()
    conversation_worker = {
        "worker_id": "wrk_codex_conversation_resume",
        "name": "Viventium Main",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(life),
        "bootstrap_bundle_json": json.dumps(
            {"run_mode": "conversation", "provider_model": "gpt-5.6-sol", "access_mode": "full"}
        ),
    }
    mission_worker = {
        **conversation_worker,
        "worker_id": "wrk_codex_mission_resume",
        "bootstrap_bundle_json": json.dumps({"run_mode": "mission"}),
    }
    runtime._ensure_dirs(conversation_worker["worker_id"])
    runtime._ensure_dirs(mission_worker["worker_id"])
    runtime._write_session_key(conversation_worker["worker_id"], "session-conversation")
    runtime._write_session_key(mission_worker["worker_id"], "session-mission")

    conversation_command, _ = runtime._build_command(
        conversation_worker,
        "Continue naturally.",
        runtime._host_runtime_info(conversation_worker),
    )
    mission_command, _ = runtime._build_command(
        mission_worker,
        "Continue the mission.",
        runtime._host_runtime_info(mission_worker),
    )

    assert conversation_command[:5] == [
        "codex",
        "exec",
        "resume",
        "--json",
        "--skip-git-repo-check",
    ]
    assert mission_command[:3] == ["codex", "exec", "resume"]
    assert "--json" not in mission_command
    assert "--skip-git-repo-check" not in mission_command


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max", "ultra"])
def test_host_codex_conversation_mode_honors_each_declared_effort(tmp_path, effort):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "private-state"))
    life = tmp_path / "Life"
    life.mkdir()
    worker = {
        "worker_id": f"wrk_codex_effort_{effort}",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(life),
        "bootstrap_bundle_json": json.dumps(
            {
                "run_mode": "conversation",
                "provider_model": "gpt-5.6-sol",
                "access_mode": "full",
                "env": {"WPR_CODEX_CLI_REASONING_EFFORT": effort},
            }
        ),
    }

    command, _ = runtime._build_command(
        worker,
        "Talk naturally.",
        runtime._host_runtime_info(worker),
    )

    assert f'model_reasoning_effort="{effort}"' in command


def test_host_codex_workspace_access_limits_writes_without_full_bypass(tmp_path):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "private-state"))
    life = tmp_path / "Life"
    life.mkdir()
    worker = {
        "worker_id": "wrk_codex_workspace_access",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(life),
        "bootstrap_bundle_json": json.dumps(
            {
                "run_mode": "conversation",
                "provider_model": "gpt-5.6-sol",
                "access_mode": "workspace",
            }
        ),
    }

    first_command, _ = runtime._build_command(
        worker,
        "Talk naturally.",
        runtime._host_runtime_info(worker),
    )
    runtime._ensure_dirs(worker["worker_id"])
    runtime._write_session_key(worker["worker_id"], "session-workspace")
    resumed_command, _ = runtime._build_command(
        worker,
        "Continue naturally.",
        runtime._host_runtime_info(worker),
    )

    assert "--full-auto" in first_command
    assert "--dangerously-bypass-approvals-and-sandbox" not in first_command
    assert 'sandbox_mode="workspace-write"' in resumed_command
    assert 'approval_policy="never"' in resumed_command
    assert "--dangerously-bypass-approvals-and-sandbox" not in resumed_command


def test_host_claude_conversation_mode_uses_native_stream_json_without_changing_mission_mode(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", "0")
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "private-state"))
    life = tmp_path / "Life"
    life.mkdir()
    conversation_worker = {
        "worker_id": "wrk_claude_conversation",
        "name": "Viventium Main",
        "profile": "claude-code",
        "execution_mode": "host",
        "workspace_root": str(life),
        "model": "opus",
        "bootstrap_bundle_json": json.dumps(
            {"run_mode": "conversation", "provider_model": "opus", "access_mode": "full"}
        ),
    }
    mission_worker = {
        **conversation_worker,
        "worker_id": "wrk_claude_mission",
        "bootstrap_bundle_json": json.dumps({"run_mode": "mission"}),
    }

    conversation_command, _ = runtime._build_command(
        conversation_worker,
        "Talk naturally.",
        runtime._host_runtime_info(conversation_worker),
    )
    mission_command, _ = runtime._build_command(
        mission_worker,
        "Run the mission.",
        runtime._host_runtime_info(mission_worker),
    )

    assert conversation_command[conversation_command.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in conversation_command
    assert "--include-partial-messages" in conversation_command
    assert mission_command[mission_command.index("--output-format") + 1] == "json"
    assert "--verbose" not in mission_command
    assert "--include-partial-messages" not in mission_command


def test_host_claude_conversation_removes_cli_marker_created_in_life(tmp_path, monkeypatch):
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "private-state"))
    life = tmp_path / "Life"
    life.mkdir()
    worker = {
        "worker_id": "wrk_claude_conversation_marker",
        "name": "Viventium Main",
        "profile": "claude-code",
        "execution_mode": "host",
        "workspace_root": str(life),
        "model": "opus",
        "bootstrap_bundle_json": json.dumps(
            {"run_mode": "conversation", "provider_model": "opus", "access_mode": "full"}
        ),
    }

    class MarkerCreatingProcess:
        pid = 12345
        returncode = 0

        def __init__(self, command, **kwargs):
            _mark_fake_host_supervisor_ready(list(command), self.pid)
            workspace = Path(kwargs["cwd"])
            (workspace / ".claude" / ".cc-writes").mkdir(parents=True)
            stdout = kwargs["stdout"]
            stdout.write(
                json.dumps(
                    {
                        "type": "result",
                        "result": "Conversation complete.",
                        "session_id": "session-marker-cleanup",
                    }
                )
                + "\n"
            )
            stdout.flush()

        def communicate(self, input=None, timeout=None):
            return None, None

        def wait(self, timeout=None):
            return self.returncode

        def poll(self):
            return self.returncode

    runtime.ensure_worker_ready = lambda _worker: runtime._host_runtime_info(worker)  # type: ignore[method-assign]
    runtime._build_command = lambda _worker, _instruction, _info: (["claude"], {})  # type: ignore[method-assign]
    runtime._process_identity_sha256 = lambda _pid: "1" * 64  # type: ignore[method-assign]
    monkeypatch.setattr(
        "workers_projects_runtime.profile_runtime.subprocess.Popen", MarkerCreatingProcess
    )

    result = runtime.run_task(
        worker,
        "Talk naturally.",
        timeout_sec=5,
        run_id="run_marker_cleanup",
    )

    assert result == "Conversation complete."
    assert not (life / ".claude").exists()


def test_host_claude_conversation_preserves_preexisting_workspace_content(tmp_path, monkeypatch):
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "private-state"))
    life = tmp_path / "Life"
    marker = life / ".claude" / ".cc-writes"
    marker.mkdir(parents=True)
    user_file = marker / "user-owned.txt"
    user_file.write_text("preserve me\n")
    worker = {
        "worker_id": "wrk_claude_conversation_preexisting",
        "profile": "claude-code",
        "execution_mode": "host",
        "workspace_root": str(life),
        "model": "opus",
        "bootstrap_bundle_json": json.dumps({"run_mode": "conversation"}),
    }

    class SuccessfulProcess:
        pid = 12345
        returncode = 0

        def __init__(self, command, **kwargs):
            _mark_fake_host_supervisor_ready(list(command), self.pid)
            stdout = kwargs["stdout"]
            stdout.write(
                json.dumps(
                    {
                        "type": "result",
                        "result": "Conversation complete.",
                        "session_id": "session-preserve-workspace",
                    }
                )
                + "\n"
            )
            stdout.flush()

        def communicate(self, input=None, timeout=None):
            return None, None

        def wait(self, timeout=None):
            return self.returncode

        def poll(self):
            return self.returncode

    runtime.ensure_worker_ready = lambda _worker: runtime._host_runtime_info(worker)  # type: ignore[method-assign]
    runtime._build_command = lambda _worker, _instruction, _info: (["claude"], {})  # type: ignore[method-assign]
    runtime._process_identity_sha256 = lambda _pid: "1" * 64  # type: ignore[method-assign]
    monkeypatch.setattr(
        "workers_projects_runtime.profile_runtime.subprocess.Popen", SuccessfulProcess
    )

    runtime.run_task(worker, "Talk naturally.", run_id="run_preserve_workspace")

    assert user_file.read_text() == "preserve me\n"


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
def test_host_claude_conversation_mode_honors_each_declared_effort(
    tmp_path, monkeypatch, effort
):
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", "0")
    monkeypatch.setattr(
        HostClaudeCodeRuntime,
        "_effort_supported",
        lambda _self, _effort="": True,
    )
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "private-state"))
    life = tmp_path / "Life"
    life.mkdir()
    worker = {
        "worker_id": f"wrk_claude_effort_{effort}",
        "profile": "claude-code",
        "execution_mode": "host",
        "workspace_root": str(life),
        "model": "opus",
        "bootstrap_bundle_json": json.dumps(
            {
                "run_mode": "conversation",
                "provider_model": "opus",
                "access_mode": "full",
                "env": {"WPR_CLAUDE_CODE_EFFORT": effort},
            }
        ),
    }

    command, _ = runtime._build_command(
        worker,
        "Talk naturally.",
        runtime._host_runtime_info(worker),
    )

    assert command[command.index("--effort") + 1] == effort


def test_host_claude_workspace_access_fails_closed_into_native_sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", "0")
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "private-state"))
    life = tmp_path / "Life"
    life.mkdir()
    worker = {
        "worker_id": "wrk_claude_workspace_access",
        "profile": "claude-code",
        "execution_mode": "host",
        "workspace_root": str(life),
        "model": "opus",
        "bootstrap_bundle_json": json.dumps(
            {
                "run_mode": "conversation",
                "provider_model": "opus",
                "access_mode": "workspace",
            }
        ),
    }

    command, _ = runtime._build_command(
        worker,
        "Talk naturally.",
        runtime._host_runtime_info(worker),
    )
    settings = json.loads(command[command.index("--settings") + 1])

    assert command[command.index("--permission-mode") + 1] == "acceptEdits"
    assert settings["sandbox"]["enabled"] is True
    assert settings["sandbox"]["failIfUnavailable"] is True
    assert settings["sandbox"]["allowUnsandboxedCommands"] is False
    assert settings["sandbox"]["filesystem"]["allowRead"] == [str(life.resolve())]


def test_host_claude_private_config_receives_subscription_auth_without_copying_user_config(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", "0")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_REFRESH_TOKEN", raising=False)
    monkeypatch.setattr(
        "workers_projects_runtime.profile_runtime.sys.platform", "darwin"
    )
    monkeypatch.setattr(
        "workers_projects_runtime.profile_runtime.shutil.which", lambda binary: f"/usr/bin/{binary}"
    )

    def fake_run(command, **_kwargs):
        assert command[:4] == ["security", "find-generic-password", "-s", "Claude Code-credentials"]
        return subprocess.CompletedProcess(
            command,
            returncode=0,
            stdout=json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": "synthetic-access-token",
                        "refreshToken": "synthetic-refresh-token",
                        "scopes": ["user:profile", "user:inference"],
                        "expiresAt": int(time.time() * 1000) + 3_600_000,
                    }
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(
        "workers_projects_runtime.profile_runtime.subprocess.run", fake_run
    )
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "private-state"))
    life = tmp_path / "Life"
    life.mkdir()
    worker = {
        "worker_id": "wrk_claude_private_auth",
        "profile": "claude-code",
        "execution_mode": "host",
        "workspace_root": str(life),
        "model": "opus",
        "bootstrap_bundle_json": json.dumps(
            {"run_mode": "conversation", "provider_model": "opus", "access_mode": "full"}
        ),
    }

    _, env = runtime._build_command(
        worker,
        "Talk naturally.",
        runtime._host_runtime_info(worker),
    )

    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "synthetic-access-token"
    assert env["CLAUDE_CODE_OAUTH_REFRESH_TOKEN"] == "synthetic-refresh-token"
    assert env["CLAUDE_CODE_OAUTH_SCOPES"] == "user:profile user:inference"
    assert env["CLAUDE_CONFIG_DIR"].startswith(str(tmp_path / "private-state"))
    assert not (life / ".claude").exists()


def test_host_claude_private_auth_prefers_explicit_environment_tokens(tmp_path, monkeypatch):
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", "0")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "synthetic-env-access")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_REFRESH_TOKEN", "synthetic-env-refresh")
    def reject_security_query(*_args, **_kwargs):
        raise AssertionError("Keychain must not be queried when explicit auth is configured")

    monkeypatch.setattr(
        "workers_projects_runtime.profile_runtime.subprocess.run", reject_security_query
    )
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "private-state"))
    worker = {
        "worker_id": "wrk_claude_env_auth",
        "profile": "claude-code",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "Life"),
        "model": "opus",
        "bootstrap_bundle_json": json.dumps({"run_mode": "conversation"}),
    }

    _, env = runtime._build_command(
        worker,
        "Talk naturally.",
        runtime._host_runtime_info(worker),
    )

    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "synthetic-env-access"
    assert env["CLAUDE_CODE_OAUTH_REFRESH_TOKEN"] == "synthetic-env-refresh"


def test_host_claude_private_auth_refreshes_expired_keychain_token_into_isolated_config(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", "0")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_REFRESH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_SCOPES", raising=False)
    monkeypatch.setattr(
        "workers_projects_runtime.profile_runtime.sys.platform", "darwin"
    )
    monkeypatch.setattr(
        "workers_projects_runtime.profile_runtime.shutil.which", lambda binary: f"/usr/bin/{binary}"
    )
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command[0] == "security":
            refreshed = len([call for call, _ in calls if call[0] == "security"]) > 1
            return subprocess.CompletedProcess(
                command,
                returncode=0,
                stdout=json.dumps(
                    {
                        "claudeAiOauth": {
                            "accessToken": (
                                "synthetic-refreshed-access"
                                if refreshed
                                else "synthetic-expired-access"
                            ),
                            "refreshToken": "synthetic-refresh-token",
                            "scopes": ["user:profile", "user:inference"],
                            "expiresAt": int(time.time() * 1000) + (3_600_000 if refreshed else -1),
                        }
                    }
                ),
                stderr="",
            )
        assert command[-2:] == ["auth", "login"]
        login_env = kwargs["env"]
        assert "CLAUDE_CONFIG_DIR" not in login_env
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in login_env
        assert login_env["CLAUDE_CODE_OAUTH_REFRESH_TOKEN"] == "synthetic-refresh-token"
        assert login_env["CLAUDE_CODE_OAUTH_SCOPES"] == "user:profile user:inference"
        return subprocess.CompletedProcess(command, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        "workers_projects_runtime.profile_runtime.subprocess.run", fake_run
    )
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "private-state"))
    worker = {
        "worker_id": "wrk_claude_expired_auth",
        "profile": "claude-code",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "Life"),
        "model": "opus",
        "bootstrap_bundle_json": json.dumps({"run_mode": "conversation"}),
    }

    _, env = runtime._build_command(
        worker,
        "Talk naturally.",
        runtime._host_runtime_info(worker),
    )

    assert len(calls) == 3
    assert calls[1][0][-2:] == ["auth", "login"]
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "synthetic-refreshed-access"
    assert env["CLAUDE_CODE_OAUTH_REFRESH_TOKEN"] == "synthetic-refresh-token"
    assert env["CLAUDE_CODE_OAUTH_SCOPES"] == "user:profile user:inference"


def test_host_mission_mode_retains_workspace_and_completion_contract(tmp_path):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "private-state"))
    mission_root = tmp_path / "missions"
    worker = {
        "worker_id": "wrk_mission",
        "name": "Research Brief",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(mission_root),
        "bootstrap_bundle_json": json.dumps({"run_mode": "mission"}),
    }

    workspace = runtime._host_workspace_dir(worker)
    runtime._materialize_workspace(worker, workspace)
    instruction = runtime._command_stdin_text(worker, "Create the brief.", runtime._host_runtime_info(worker))

    assert workspace != mission_root
    assert workspace.is_relative_to(mission_root)
    assert (workspace / "project-definition.md").exists()
    assert (workspace / "work-log.md").exists()
    assert (workspace / "AGENTS.md").exists()
    assert "FINAL REPORT" in instruction
