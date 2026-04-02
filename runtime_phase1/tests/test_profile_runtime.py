from __future__ import annotations

import json

from workers_projects_runtime.profile_runtime import CodexCliRuntime


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
                json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "HELLO WORLD"}}),
            ]
        )
        + "\n"
    )
    (run_root / "stderr.log").write_text("")
    (run_root / "exit_code").write_text("0")

    runtime.reconcile_worker = lambda worker: runtime._runtime_info(worker, pid=1234)  # type: ignore[method-assign]

    recovered = runtime.collect_completed_run(worker)
    assert recovered is not None
    assert recovered["state"] == "completed"
    assert recovered["output_text"] == "HELLO WORLD"
    assert json.loads(runtime._session_meta_path(worker["worker_id"]).read_text())["session_key"] == "thread_123"
