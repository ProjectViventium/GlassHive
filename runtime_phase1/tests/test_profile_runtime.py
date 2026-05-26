from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path

import pytest

from workers_projects_runtime.failure_classification import classify_runtime_error
from workers_projects_runtime.openclaw_runtime import RuntimeDependencyMissingError, RuntimeErrorBase, WorkerTerminatedError
from workers_projects_runtime.profile_runtime import BaseCliWorkerRuntime, ClaudeCodeRuntime, CodexCliRuntime, HostCodexCliRuntime, HostOpenClawRuntime, OpenClawWorkstationRuntime, ProfiledWorkerRuntime, _redact_text


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


def test_openclaw_command_includes_completion_contract(tmp_path):
    runtime = OpenClawWorkstationRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_openclaw_contract",
        "name": "Main Worker",
        "profile": "openclaw-general",
        "model": "openai/gpt-5.2",
    }
    runtime._ensure_dirs(worker["worker_id"])

    command, _env = runtime._build_command(worker, "do the work", runtime._runtime_info(worker))

    assert "-m" in command
    instruction = command[command.index("-m") + 1]
    assert instruction.startswith("do the work")
    assert "FINAL REPORT:" in instruction
    assert "Put only the user-facing result" in instruction


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
        },
    )
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


def test_host_codex_model_can_differ_from_docker_provider_model(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    monkeypatch.setenv("WPR_MODEL_CODEX_CLI", "gpt-5.2-chat")
    monkeypatch.setenv("WPR_MODEL_HOST_CODEX_CLI", "gpt-5.4")

    assert runtime.resolve_model("codex-cli") == "gpt-5.4"


def test_host_codex_defaults_to_local_codex_config_instead_of_provider_model(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    monkeypatch.setenv("WPR_MODEL_CODEX_CLI", "gpt-5.4")
    monkeypatch.delenv("WPR_MODEL_HOST_CODEX_CLI", raising=False)
    monkeypatch.delenv("CODEX_MODEL", raising=False)
    monkeypatch.delenv("GLASSHIVE_HOST_CODEX_INHERIT_PROVIDER_MODEL", raising=False)
    worker = {
        "worker_id": "wrk_host_model_default",
        "name": "Main Host Worker",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "workspaces"),
    }

    command, _env = runtime._build_command(worker, "create the marker", runtime._host_runtime_info(worker))

    assert runtime.resolve_model("codex-cli") == ""
    assert "-m" not in command


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

    command: list[str] = []
    runtime._append_codex_compatible_provider_config(command, {"worker_id": "wrk_effort"})

    joined = "\n".join(command)
    assert 'model_reasoning_effort="xhigh"' in joined


def test_codex_cli_provider_config_honors_per_run_reasoning_effort(tmp_path, monkeypatch):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    monkeypatch.setenv("WPR_CODEX_CLI_BASE_URL", "https://provider.example.com/v1")
    monkeypatch.setenv("WPR_CODEX_CLI_REASONING_EFFORT", "medium")
    worker = {
        "worker_id": "wrk_effort",
        "bootstrap_bundle_json": json.dumps({"env": {"WPR_CODEX_CLI_REASONING_EFFORT": "xhigh"}}),
    }

    command: list[str] = []
    runtime._append_codex_compatible_provider_config(command, worker)

    joined = "\n".join(command)
    assert 'model_reasoning_effort="xhigh"' in joined
    assert 'model_reasoning_effort="medium"' not in joined


def test_codex_cli_provider_config_disables_web_search_for_minimal_effort(tmp_path, monkeypatch):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    monkeypatch.setenv("WPR_CODEX_CLI_BASE_URL", "https://provider.example.com/v1")
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


def test_host_cli_run_closes_stdin_for_noninteractive_workers(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = "/bin/echo"
    captured: dict[str, object] = {}

    class FakeProcess:
        pid = 12345

        def __init__(self, command, **kwargs):
            captured["stdin"] = kwargs.get("stdin")
            stdout = kwargs["stdout"]
            stdout.write(
                '{"type":"item.completed","item":{"type":"agent_message","text":"FINAL REPORT:\\nDone"}}\n'
            )
            stdout.flush()

        def wait(self, timeout=None):
            return 0

            def poll(self):
                return 0

    monkeypatch.setattr(
        "workers_projects_runtime.profile_runtime.subprocess.run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, returncode=0, stdout="", stderr=""),
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


def test_docker_cli_runtime_sources_runtime_and_openclaw_env_files(tmp_path):
    class CaptureRuntime(BaseCliWorkerRuntime):
        runtime_name = "openclaw"
        worker_root_name = "capture_runtime"

        def resolve_model(self, profile: str) -> str:
            return "capture/model"

        def _build_command(self, worker, instruction, info):
            return ["printf", "ok"], {}

        def _parse_output(self, worker, stdout, stderr, info):
            return None, stdout

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
        assert "GLASSHIVE_ACTIVE_WORKER_ID=wrk_capture" in script
        (run_root / "stdout.log").write_text("ok")
        (run_root / "stderr.log").write_text("")
        (run_root / "exit_code").write_text("0")
        return subprocess.CompletedProcess(["screen"], returncode=0, stdout="", stderr="")

    runtime.sandbox.start_screen_session = fake_start_screen_session  # type: ignore[method-assign]

    assert runtime.run_task(worker, "do it", run_id=run_id) == "ok"
    assert writable_repairs == [
        [f"{runtime.sandbox.home_mount}/.glasshive-runs/{run_id}"],
        [runtime.sandbox.workspace_mount, f"{runtime.sandbox.home_mount}/.glasshive-runs/{run_id}"]
    ]


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

    assert "--ignore-user-config" in command
    assert command[command.index("--disable") + 1] == "apps"
    assert 'model_provider="glasshive_openai_compatible"' in command
    assert 'model_providers.glasshive_openai_compatible.base_url="https://models.example.test/openai/v1"' in command
    assert 'model_providers.glasshive_openai_compatible.env_key="OPENAI_API_KEY"' in command
    assert "model_providers.glasshive_openai_compatible.supports_websockets=false" in command
    assert 'model_verbosity="medium"' in command
    assert env["OPENAI_BASE_URL"] == "https://models.example.test/openai/v1"


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
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gateway.example")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "gateway-token")
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "x-portkey-provider: anthropic")
    monkeypatch.setenv("ANTHROPIC_DEFAULT_SONNET_MODEL", "claude-sonnet-test")

    command, env = runtime._build_command(worker, "Create the artifact.", runtime._runtime_info(worker))

    assert "--model" in command
    assert env["ANTHROPIC_API_KEY"] == "anthropic-test"
    assert env["ANTHROPIC_BASE_URL"] == "https://gateway.example"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "gateway-token"
    assert env["ANTHROPIC_CUSTOM_HEADERS"] == "x-portkey-provider: anthropic"
    assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "claude-sonnet-test"


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
