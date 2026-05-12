from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from workers_projects_runtime.openclaw_runtime import RuntimeErrorBase, WorkerTerminatedError
from workers_projects_runtime.profile_runtime import CodexCliRuntime, HostCodexCliRuntime, HostOpenClawRuntime, _redact_text


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
    recovered = runtime.collect_completed_run(worker, run_id=active_run_id)
    assert recovered is not None
    assert recovered["state"] == "completed"
    assert recovered["output_text"] == "NEW"


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


def test_host_codex_runtime_materializes_required_workspace_files(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = "/bin/echo"
    xattr_calls = []

    def fake_run(args, **_kwargs):
        xattr_calls.append(args)

        class Result:
            returncode = 0

        return Result()

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
    assert (workspace_dir / "work-log.md").exists()
    assert (workspace_dir / "agents.md").read_text() == "Agent context"
    assert (workspace_dir / "AGENTS.md").read_text() == "Agent context"
    assert (workspace_dir / "claude.md").read_text() == "Claude context"
    assert (workspace_dir / "codex.md").read_text() == "Codex context"
    assert (workspace_dir / "glasshive-host-tools" / "capture-front-window.sh").exists()
    assert xattr_calls
    assert xattr_calls[0][:3] == ["/usr/bin/xattr", "-d", "com.apple.quarantine"]
    assert (workspace_dir / "uploads" / "uploaded-brief.txt").read_text() == "Uploaded brief"
    assert (tmp_path / "data" / "host_codex_cli_runtime" / "workers" / "wrk_host" / "state" / "action-audit.jsonl").exists()


def test_host_codex_runtime_default_prompts_require_final_report(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = "/bin/echo"

    def fake_run(args, **_kwargs):
        class Result:
            returncode = 0

        return Result()

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


def test_host_runtime_live_description_refreshes_stale_prompt_files(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = "/bin/echo"

    def fake_run(args, **_kwargs):
        class Result:
            returncode = 0

        return Result()

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
    assert command[-1].startswith("do the work")
    assert "FINAL REPORT:" in command[-1]
    assert "Put only the user-facing result" in command[-1]
    assert env["GLASSHIVE_EXECUTION_MODE"] == "host"
    assert env["GLASSHIVE_WORKSPACE_DIR"] == str(info.workspace_dir)


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


def test_docker_cli_runtime_uses_configured_run_timeout(tmp_path, monkeypatch):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))

    monkeypatch.setenv("GLASSHIVE_RUN_TIMEOUT_SEC", "1200")

    assert runtime._run_timeout_sec() == 1200


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

    assert command[-1].startswith("Make the page red.")
    assert "FINAL REPORT:" in command[-1]


def test_host_env_strips_parent_secrets_and_keeps_minimal_runtime_context(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    monkeypatch.setenv("VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", "callback-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "fake-openai-key")
    monkeypatch.setenv("LIBRECHAT_SECRET", "librechat-secret")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
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

    with pytest.raises(RuntimeErrorBase, match="definitely-missing-openclaw CLI is not installed"):
        runtime.ensure_worker_ready(worker)


def test_redact_text_masks_parent_visible_secret_shapes():
    synthetic_openai_token = "sk-" + "abc123456789xyz"
    redacted = _redact_text(f"Authorization: Bearer abcdefghijklmnopqrstuvwxyz token=super-secret-value {synthetic_openai_token}")
    assert "abcdefghijklmnopqrstuvwxyz" not in redacted
    assert "super-secret-value" not in redacted
    assert synthetic_openai_token not in redacted
    assert "[REDACTED]" in redacted


def test_redact_text_masks_parent_visible_image_payloads():
    base64_png = "iVBORw0KGgo" + ("A" * 900) + "=="
    redacted = _redact_text(
        '{"type":"tool_result","content":[{"type":"image","mimeType":"image/png","data":"'
        + base64_png
        + '"}]}'
    )

    assert base64_png not in redacted
    assert "[REDACTED_LONG_BASE64]" in redacted
