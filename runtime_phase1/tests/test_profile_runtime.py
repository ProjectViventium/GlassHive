from __future__ import annotations

import json
import logging
import os
import stat
import subprocess
import threading
import time
from pathlib import Path

import pytest

from workers_projects_runtime.bootstrap import GLASSHIVE_CRITICAL_OPERATING_INSTRUCTIONS, GLASSHIVE_SAFETY_CHECKPOINT_RULE
from workers_projects_runtime.failure_classification import classify_cli_failure, classify_runtime_error
from workers_projects_runtime.openclaw_runtime import RuntimeDependencyMissingError, RuntimeErrorBase, WorkerTerminatedError
from workers_projects_runtime.profile_runtime import BaseCliWorkerRuntime, ClaudeCodeRuntime, CodexCliRuntime, HostClaudeCodeRuntime, HostCodexCliRuntime, HostOpenClawRuntime, OpenClawWorkstationRuntime, ProfiledWorkerRuntime, _redact_text
from workers_projects_runtime.run_evidence import build_constraint_ledger, write_constraint_ledger


def _patch_host_codex_requirement_probe(monkeypatch):
    monkeypatch.setattr(
        "workers_projects_runtime.runtime_requirements.subprocess.run",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args,
            returncode=0,
            stdout="codex-cli 0.140.0\n",
            stderr="",
        ),
    )


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
            captured["command"] = list(command)
            captured["stdin_pipe"] = kwargs["stdin"] == subprocess.PIPE
            self.stdout_handle = kwargs["stdout"]

        def communicate(self, input=None, timeout=None):
            captured["stdin"] = input
            self.stdout_handle.write(
                json.dumps(
                    {
                        "finalAssistantVisibleText": "FINAL REPORT:\nDone.",
                        "completion": {"stopReason": "stop"},
                    }
                )
            )
            self.stdout_handle.flush()
            return None, None

        def wait(self, timeout=None):
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
    pointer = command[command.index("-m") + 1]
    assert "Sensitive OpenClaw task" not in pointer
    assert "run_host_openclaw_pointer/instruction.stdin" in pointer
    stdin_path = runtime._run_root(worker["worker_id"], "run_host_openclaw_pointer") / "instruction.stdin"
    assert stdin_path.exists()
    assert stdin_path.read_text().startswith("Sensitive OpenClaw task.")
    assert oct(stdin_path.stat().st_mode & 0o777) == "0o600"
    assert captured["stdin_pipe"] is True
    assert str(captured["stdin"]).startswith("Sensitive OpenClaw task.")


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


def test_host_codex_runtime_materializes_required_workspace_files(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = "/bin/echo"
    _patch_host_codex_requirement_probe(monkeypatch)
    xattr_calls = []

    def fake_run(args, **_kwargs):
        if "--version" in args:
            return subprocess.CompletedProcess(args, returncode=0, stdout="codex-cli 0.140.0\n", stderr="")
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
            stdout="codex-cli 0.140.0\n" if "--version" in args else "",
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
        "allowed": ["high", "low", "medium", "minimal", "none"],
        "route_proven": False,
        "fallback_reason": "xhigh_route_not_proven",
    }
    assert any(record.message == "Codex CLI reasoning effort clamped to provider-route fallback" for record in caplog.records)


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


def test_host_cli_run_uses_stdin_pipe_for_private_instruction(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = "/bin/echo"
    _patch_host_codex_requirement_probe(monkeypatch)
    captured: dict[str, object] = {}

    class FakeProcess:
        pid = 12345
        returncode = 0

        def __init__(self, command, **kwargs):
            captured["stdin"] = kwargs.get("stdin")
            stdout = kwargs["stdout"]
            stdout.write(
                '{"type":"item.completed","item":{"type":"agent_message","text":"FINAL REPORT:\\nDone"}}\n'
            )
            stdout.flush()

        def wait(self, timeout=None):
            return 0

        def communicate(self, input=None, timeout=None):
            captured["input"] = input
            return None, None

        def poll(self):
            return 0

    monkeypatch.setattr(
        "workers_projects_runtime.profile_runtime.subprocess.run",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args,
            returncode=0,
            stdout="codex-cli 0.140.0\n" if "--version" in args else "",
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
    assert captured["stdin"] is subprocess.PIPE
    assert str(captured["input"]).startswith("create marker")


def test_host_cli_run_writes_constraint_ledger_and_evidence(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = "/bin/echo"
    _patch_host_codex_requirement_probe(monkeypatch)
    workspace = tmp_path / "workspace"

    class FakeProcess:
        pid = 12345
        returncode = 0

        def __init__(self, _command, **kwargs):
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
            stdout="codex-cli 0.140.0\n" if "--version" in args else "",
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
    assert active_status["process_pid"] is None
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

        def __init__(self, _command, **kwargs):
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
            stdout="codex-cli 0.140.0\n" if "--version" in args else "",
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
    _patch_host_codex_requirement_probe(monkeypatch)
    workspace = tmp_path / "workspace"

    class TimeoutProcess:
        pid = 12345

        def __init__(self, _command, **kwargs):
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
            stdout="codex-cli 0.140.0\n" if "--version" in args else "",
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


def test_host_cli_timeout_preserves_foreground_server_transcript(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = "/bin/echo"
    _patch_host_codex_requirement_probe(monkeypatch)
    workspace = tmp_path / "workspace"

    class ForegroundServerProcess:
        pid = 12345

        def __init__(self, _command, **kwargs):
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
            stdout="codex-cli 0.140.0\n" if "--version" in args else "",
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
            captured["command"] = list(command)
            captured["stdin_pipe"] = kwargs["stdin"] == subprocess.PIPE
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
            captured["stdin"] = input
            return None, None

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
            stdout="codex-cli 0.140.0\n" if "--version" in args else "",
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
    assert captured["stdin_pipe"] is True
    assert str(captured["stdin"]).startswith("Sensitive private instruction.")
    evidence = json.loads((workspace / "glasshive-run" / "evidence.json").read_text())
    assert all("Sensitive private instruction" not in arg for arg in evidence["command"]["argv_redacted"])
    assert evidence["command"]["argv_redacted"][0] == "echo"
    assert "/bin/echo" not in evidence["command"]["display_redacted"]


def test_host_cli_interrupt_writes_run_evidence(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = "/bin/echo"
    _patch_host_codex_requirement_probe(monkeypatch)
    workspace = tmp_path / "workspace"
    processes: list[object] = []

    class BlockingProcess:
        pid = 12345

        def __init__(self, _command, **kwargs):
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
            stdout="codex-cli 0.140.0\n" if "--version" in args else "",
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


def test_host_codex_runtime_default_prompts_require_final_report(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = "/bin/echo"
    _patch_host_codex_requirement_probe(monkeypatch)

    def fake_run(args, **_kwargs):
        return subprocess.CompletedProcess(
            args,
            returncode=0,
            stdout="codex-cli 0.140.0\n" if "--version" in args else "",
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
            stdout="codex-cli 0.140.0\n" if "--version" in args else "",
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
            stdout="codex-cli 0.140.0\n" if "--version" in args else "",
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
        lambda *args, **kwargs: subprocess.CompletedProcess(args, returncode=0, stdout="Usage: claude [options] --effort\n", stderr=""),
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


def test_host_claude_command_enables_chrome_by_default(tmp_path, monkeypatch):
    fake_claude = tmp_path / "claude"
    fake_claude.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$1\" == \"--help\" ]]; then echo 'Usage: claude [options] --effort --chrome'; exit 0; fi\n"
        "echo '2.1.178 (Claude Code)'\n"
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
        assert "GLASSHIVE_ACTIVE_WORKER_ID=wrk_capture" in script
        (run_root / "stdout.log").write_text("FINAL REPORT:\nok")
        (run_root / "stderr.log").write_text("")
        (run_root / "exit_code").write_text("0")
        return subprocess.CompletedProcess(["screen"], returncode=0, stdout="", stderr="")

    runtime.sandbox.start_screen_session = fake_start_screen_session  # type: ignore[method-assign]

    assert runtime.run_task(worker, "do it", run_id=run_id) == "FINAL REPORT:\nok"
    assert writable_repairs == [
        [f"{runtime.sandbox.home_mount}/.glasshive-runs/{run_id}"],
        [runtime.sandbox.workspace_mount, f"{runtime.sandbox.home_mount}/.glasshive-runs/{run_id}"]
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


def test_docker_claude_command_enables_chrome_and_projects_completion_contract_to_stdin(tmp_path, monkeypatch):
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

    info = runtime._runtime_info(worker)
    command, _ = runtime._build_command(worker, "Make the page red.", info)
    stdin_text = runtime._command_stdin_text(worker, "Make the page red.", info)

    assert "--chrome" in command
    assert "Make the page red." not in command
    assert stdin_text is not None
    assert stdin_text.startswith("Make the page red.")
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
        "echo 'codex-cli 0.140.0'\n"
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
        "if [[ \"$1\" == \"--version\" ]]; then echo '2.1.178 (Claude Code)'; exit 0; fi\n"
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
        "if [[ \"$1\" == \"--version\" ]]; then echo '2.1.178 (Claude Code)'; exit 0; fi\n"
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
        "echo '2.1.178 (Claude Code)'\n"
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
        "if [[ \"$1\" == \"--version\" ]]; then echo 'codex-cli 0.140.0'; exit 0; fi\n"
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
    redacted = _redact_text(
        f"Authorization: {'Bearer'} {synthetic_bearer} token=super-secret-value {synthetic_openai_token}"
    )
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
