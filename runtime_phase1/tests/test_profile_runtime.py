from __future__ import annotations

import json
import io
import logging
import os
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from workers_projects_runtime.bootstrap import GLASSHIVE_CRITICAL_OPERATING_INSTRUCTIONS, GLASSHIVE_SAFETY_CHECKPOINT_RULE, PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
from workers_projects_runtime.failure_classification import classify_cli_failure, classify_runtime_error
from workers_projects_runtime.openclaw_runtime import RunStartupRejectedError, RuntimeDependencyMissingError, RuntimeErrorBase, WorkerTerminatedError
from workers_projects_runtime.profile_runtime import BaseCliWorkerRuntime, ClaudeCodeRuntime, CodexCliRuntime, HostClaudeCodeRuntime, HostCodexCliRuntime, HostOpenClawRuntime, OpenClawWorkstationRuntime, ProfiledWorkerRuntime, _atomic_write_private_text, _host_native_web_access, _provider_process_exit_error, _redact_text
from workers_projects_runtime.run_evidence import build_constraint_ledger, write_constraint_ledger


def _patch_host_codex_requirement_probe(monkeypatch):
    monkeypatch.setattr(
        "workers_projects_runtime.runtime_requirements.subprocess.run",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args,
            returncode=0,
            stdout="codex-cli 0.144.1\n",
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


def test_atomic_private_state_write_never_exposes_partial_replacement(tmp_path, monkeypatch):
    target = tmp_path / "active-run.json"
    target.write_text(json.dumps({"state": "running", "sequence": 1}))
    real_replace = os.replace
    state_seen_before_publish: list[dict[str, object]] = []

    def inspect_then_replace(source, destination):
        state_seen_before_publish.append(json.loads(target.read_text()))
        real_replace(source, destination)

    monkeypatch.setattr("workers_projects_runtime.profile_runtime.os.replace", inspect_then_replace)

    _atomic_write_private_text(target, json.dumps({"state": "running", "sequence": 2}))

    assert state_seen_before_publish == [{"state": "running", "sequence": 1}]
    assert json.loads(target.read_text()) == {"state": "running", "sequence": 2}
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


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
                json.dumps(
                    {
                        "type": "response.failed",
                        "error": {
                            "message": "Too Many Requests",
                            "status_code": 429,
                            "headers": {"retry-after": "120"},
                        },
                    }
                ),
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
    assert recovered["provider_retry_after_s"] == 120
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


def test_classify_cli_failure_requires_structured_code_for_quota_capacity():
    failure = classify_cli_failure(
        stdout='{"type":"error","message":"You\'ve hit your usage limit. Try again after the reset."}\n'
        '{"type":"turn.failed","error":{"message":"You\'ve hit your usage limit."}}',
        stderr="",
        runtime_name="codex-cli",
        exit_code=1,
    )

    assert failure.failure_class == "provider_response_failed"
    assert failure.retryable is True
    assert failure.failure_class != "provider_quota_exhausted"


def test_classify_cli_failure_maps_clean_room_provider_auth_409_to_needs_input():
    stdout = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "thread_auth"}),
            json.dumps(
                {
                    "type": "error",
                    "message": (
                        "Reconnecting... 1/5 (unexpected status 409 Conflict: "
                        "The connected model account is unavailable for this mission., "
                        "url: http://provider-egress:8080/openai/v1/responses)"
                    ),
                }
            ),
            json.dumps(
                {
                    "type": "turn.failed",
                    "error": {
                        "message": (
                            "unexpected status 409 Conflict: The connected model account is "
                            "unavailable for this mission., url: "
                            "http://provider-egress:8080/openai/v1/responses"
                        )
                    },
                }
            ),
        ]
    )
    stderr = (
        "ERROR rmcp::transport::worker: Transport channel closed, when "
        'UnexpectedServerResponse("HTTP 502: ")\n'
    )

    failure = classify_cli_failure(
        stdout=stdout,
        stderr=stderr,
        runtime_name="codex-cli",
        exit_code=1,
    )

    assert failure.failure_class == "provider_auth_projection_unavailable"
    assert failure.retryable is False
    assert failure.structured is True
    assert "Connect or reauthorize" in failure.recommended_recovery


def test_collect_completed_run_preserves_provider_auth_409_as_needs_input(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_auth_needed",
        "name": "Auth Worker",
        "profile": "codex-cli",
        "model": "gpt-5.6-sol",
    }
    runtime._ensure_dirs(worker["worker_id"])

    run_id = "run_auth_needed"
    run_root = runtime._run_root(worker["worker_id"], run_id)
    run_root.mkdir(parents=True, exist_ok=True)
    (run_root / "stdout.log").write_text(
        json.dumps(
            {
                "type": "turn.failed",
                "error": {
                    "message": (
                        "unexpected status 409 Conflict: The connected model account is "
                        "unavailable for this mission., url: "
                        "http://provider-egress:8080/openai/v1/responses"
                    )
                },
            }
        )
        + "\n"
    )
    (run_root / "stderr.log").write_text(
        'UnexpectedServerResponse("HTTP 502: ")\n'
    )
    (run_root / "exit_code").write_text("1")

    recovered = runtime.collect_completed_run(worker, run_id=run_id)

    assert recovered is not None
    assert recovered["state"] == "needs_input"
    assert recovered["failure_class"] == "provider_auth_projection_unavailable"
    assert recovered["failure_retryable"] == 0
    assert recovered["failure_structured"] == 1
    assert "connected model account" in recovered["failure_user_message"]


def test_classify_cli_failure_does_not_infer_quota_from_prefixed_english_stderr():
    failure = classify_cli_failure(
        stdout="",
        stderr=(
            "INFO: starting native worker\n"
            "ERROR: You've hit your usage limit. Try again after the reset.\n"
        ),
        runtime_name="codex-cli",
        exit_code=1,
    )

    assert failure.failure_class != "provider_quota_exhausted"


def test_classify_cli_failure_does_not_treat_unstructured_usage_limit_prose_as_quota():
    failure = classify_cli_failure(
        stdout="The user's document discusses usage limit policy as a domain fact.",
        stderr="",
        runtime_name="codex-cli",
        exit_code=1,
    )

    assert failure.failure_class != "provider_quota_exhausted"


def test_classify_cli_failure_does_not_infer_capacity_from_unstructured_rate_limit_prose():
    failure = classify_cli_failure(
        stdout="",
        stderr="The provider returned 429 Too Many Requests.",
        runtime_name="codex-cli",
        exit_code=1,
    )

    assert failure.failure_class != "provider_rate_limited"
    assert failure.retry_after_s is None


@pytest.mark.parametrize(
    ("error_code", "expected_class"),
    [
        ("rate_limit_error", "provider_rate_limited"),
        ("resource_exhausted", "provider_rate_limited"),
        ("insufficient_quota", "provider_quota_exhausted"),
        ("usage_limit_reached", "provider_quota_exhausted"),
    ],
)
def test_classify_cli_failure_uses_structured_provider_capacity_codes(
    error_code,
    expected_class,
):
    failure = classify_cli_failure(
        stdout=json.dumps(
            {
                "type": "response.failed",
                "error": {
                    "error_code": error_code,
                    "message": "Provider request could not proceed.",
                },
            }
        ),
        stderr="",
        runtime_name="codex-cli",
        exit_code=1,
    )

    assert failure.failure_class == expected_class
    assert failure.retryable is True
    assert failure.structured is True


def test_classify_cli_failure_extracts_retry_after_only_from_structured_provider_json():
    structured = classify_cli_failure(
        stdout=json.dumps(
            {
                "type": "response.failed",
                "error": {
                    "status_code": 429,
                    "message": "Too Many Requests",
                    "retry_after_seconds": 75,
                },
            }
        ),
        stderr="",
        runtime_name="codex-cli",
        exit_code=1,
    )
    prose = classify_cli_failure(
        stdout="",
        stderr="429 Too Many Requests; Retry-After: 99999",
        runtime_name="codex-cli",
        exit_code=1,
    )

    assert structured.failure_class == "provider_rate_limited"
    assert structured.retry_after_s == 75
    assert prose.failure_class != "provider_rate_limited"
    assert prose.retry_after_s is None


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


def test_claude_conversation_parser_returns_structured_output_envelope(tmp_path):
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_claude_structured",
        "trusted_run_lane": "conversation",
        "name": "Synthetic worker",
        "profile": "claude-code",
        "model": "opus",
        "bootstrap_bundle_json": json.dumps(
            {"run_mode": "conversation", "agent_builder_control": {"enabled": True}}
        ),
    }
    stdout = "\n".join(
        [
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "Private schema work in progress."}
                        ]
                    },
                }
            ),
            json.dumps(
                {
                    "type": "result",
                    "session_id": "claude-session",
                    "structured_output": {
                        "type": "tool_call",
                        "content": "",
                        "tool_name": "lc_transfer_to_specialist",
                    },
                }
            ),
        ]
    )

    session_key, output = runtime._parse_output(
        worker, stdout, "", runtime._runtime_info(worker)
    )

    assert session_key == "claude-session"
    assert json.loads(output) == {
        "type": "tool_call",
        "content": "",
        "tool_name": "lc_transfer_to_specialist",
    }


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


@pytest.mark.parametrize(
    "marker",
    [
        "**FINAL REPORT:**",
        "## FINAL REPORT:",
        "> _FINAL REPORT:_",
    ],
)
def test_codex_parser_strips_markdown_final_report_section(tmp_path, marker):
    runtime = CodexCliRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_markdown_final_report",
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
                "text": f"Progress that should never reach chat.\n{marker}\nOnly this final result should be posted.",
            },
        }
    )

    _, output = runtime._parse_output(worker, stdout, "", runtime._runtime_info(worker))

    assert output == "Only this final result should be posted."


def test_codex_parser_preserves_bold_content_after_plain_final_report(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path))
    worker = {
        "worker_id": "wrk_bold_content_after_final_report",
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
                "text": "FINAL REPORT:\n\n**Neighborhood Book Swap**\nFive steps follow.",
            },
        }
    )

    _, output = runtime._parse_output(worker, stdout, "", runtime._runtime_info(worker))

    assert output == "**Neighborhood Book Swap**\nFive steps follow."


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


def test_codex_retry_cold_starts_when_durable_session_is_missing_from_native_store(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "private-state"))
    worker = {
        "worker_id": "wrk_codex_missing_native_session",
        "name": "Retry Worker",
        "profile": "codex-cli",
        "execution_mode": "docker",
    }
    runtime._ensure_dirs(worker["worker_id"])
    runtime._write_session_key(worker["worker_id"], "synthetic-missing-thread")
    codex_home = runtime._home_dir(worker["worker_id"]) / ".codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(codex_home / "state_5.sqlite") as connection:
        connection.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL)"
        )

    command, _env = runtime._build_command(
        worker,
        "Retry the same objective in the durable workspace.",
        runtime._runtime_info(worker),
    )

    assert command[1] == "exec"
    assert "resume" not in command
    assert "synthetic-missing-thread" not in command


def test_codex_retry_resumes_when_native_store_and_rollout_still_exist(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "private-state"))
    worker = {
        "worker_id": "wrk_codex_available_native_session",
        "name": "Retry Worker",
        "profile": "codex-cli",
        "execution_mode": "docker",
    }
    session_key = "synthetic-available-thread"
    runtime._ensure_dirs(worker["worker_id"])
    runtime._write_session_key(worker["worker_id"], session_key)
    codex_home = runtime._home_dir(worker["worker_id"]) / ".codex"
    rollout_path = codex_home / "sessions" / f"rollout-{session_key}.jsonl"
    rollout_path.parent.mkdir(parents=True, exist_ok=True)
    rollout_path.write_text("{}\n")
    with sqlite3.connect(codex_home / "state_5.sqlite") as connection:
        connection.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO threads(id, rollout_path) VALUES (?, ?)",
            (session_key, f"/home/seluser/.codex/sessions/{rollout_path.name}"),
        )

    command, _env = runtime._build_command(
        worker,
        "Continue the same objective in the durable workspace.",
        runtime._runtime_info(worker),
    )

    assert command[1:3] == ["exec", "resume"]
    assert session_key in command


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
    runtime.set_run_start_observer(lambda _payload: None)
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
            return subprocess.CompletedProcess(args, returncode=0, stdout="codex-cli 0.144.1\n", stderr="")
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
            stdout="codex-cli 0.144.1\n" if "--version" in args else "",
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


def test_host_cli_run_uses_stdin_pipe_for_private_instruction(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.set_run_start_observer(lambda _payload: None)
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
            stdout="codex-cli 0.144.1\n" if "--version" in args else "",
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
    runtime.set_run_start_observer(lambda _payload: None)
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
            stdout="codex-cli 0.144.1\n" if "--version" in args else "",
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
    runtime.set_run_start_observer(lambda _payload: None)
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
            stdout="codex-cli 0.144.1\n" if "--version" in args else "",
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
    runtime.set_run_start_observer(lambda _payload: None)
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
            stdout="codex-cli 0.144.1\n" if "--version" in args else "",
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
    runtime.set_run_start_observer(lambda _payload: None)
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
            stdout="codex-cli 0.144.1\n" if "--version" in args else "",
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
    runtime.set_run_start_observer(lambda _payload: None)
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
            stdout="codex-cli 0.144.1\n" if "--version" in args else "",
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
    runtime.set_run_start_observer(lambda _payload: None)
    runtime.binary = "/bin/echo"
    _patch_host_codex_requirement_probe(monkeypatch)
    workspace = tmp_path / "workspace"
    processes: list[object] = []

    class BlockingProcess:
        pid = 12345
        returncode = None

        def __init__(self, _command, **kwargs):
            self.terminated = False
            self.stdout = io.StringIO(
                "working before interrupt\n"
                "debug path /Users/example/private-workspace/tmp/preview.png\n"
            )
            self.stderr = io.StringIO("")
            self.stdin = io.StringIO()
            processes.append(self)

        def wait(self, timeout=None):
            deadline = time.time() + 10
            while not self.terminated and time.time() < deadline:
                time.sleep(0.01)
            if self.terminated:
                self.returncode = -15
                return -15
            raise subprocess.TimeoutExpired(["fake-codex"], timeout)

        def communicate(self, input=None, timeout=None):
            self.wait(timeout=timeout)
            return None, None

        def poll(self):
            if self.terminated:
                self.returncode = -15
            return self.returncode

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
            stdout="codex-cli 0.144.1\n" if "--version" in args else "",
            stderr="",
        ),
    )
    monkeypatch.setattr("workers_projects_runtime.profile_runtime.subprocess.Popen", BlockingProcess)
    monkeypatch.setattr("workers_projects_runtime.profile_runtime.os.getpgid", lambda pid: pid)
    monkeypatch.setattr("workers_projects_runtime.profile_runtime.os.killpg", fake_killpg)
    monkeypatch.setattr(
        runtime,
        "_process_start_identity",
        lambda _pid: "ps-lstart:synthetic-interrupt-generation",
    )
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

    active_session = runtime._read_active_session(worker["worker_id"]) or {}
    assert active_session, errors
    stdout_path = Path(str(active_session.get("stdout_path") or ""))
    # This test owns host-control evidence, not the separately-covered pipe
    # capture. Seed the provider transcript named by the exact active session.
    stdout_path.write_text(
        "working before interrupt\n"
        "debug path /Users/example/private-workspace/tmp/preview.png\n"
    )
    worker["_host_run_lease"] = {
        "worker_id": worker["worker_id"],
        "run_id": "run_interrupt_evidence",
        "status": "active",
        "startup_state": "confirmed",
        "startup_identity_kind": "host_process",
        "pid": active_session.get("process_pid"),
        "process_group": active_session.get("process_group"),
        "process_start_identity": active_session.get("process_start_identity"),
        "startup_session_id": active_session.get("session_name"),
    }

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
            stdout="codex-cli 0.144.1\n" if "--version" in args else "",
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


def test_host_codex_runtime_copies_auth_without_optional_bootstrap_bundle(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = "/bin/echo"
    _patch_host_codex_requirement_probe(monkeypatch)
    source_codex_home = tmp_path / "source-codex-home"
    source_codex_home.mkdir()
    (source_codex_home / "auth.json").write_text(
        '{"OPENAI_API_KEY":"synthetic-test-key"}'
    )
    monkeypatch.setenv("CODEX_HOME", str(source_codex_home))

    def fake_run(args, **_kwargs):
        return subprocess.CompletedProcess(
            args,
            returncode=0,
            stdout="codex-cli 0.144.1\n" if "--version" in args else "",
            stderr="",
        )

    monkeypatch.setattr("workers_projects_runtime.profile_runtime.subprocess.run", fake_run)
    worker = {
        "worker_id": "wrk_host_auth_baseline",
        "name": "Host Worker",
        "role": "general",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "workspaces"),
    }

    runtime.ensure_worker_ready(worker)

    target_auth = runtime._host_codex_home(worker) / "auth.json"
    assert json.loads(target_auth.read_text()) == {
        "OPENAI_API_KEY": "synthetic-test-key"
    }
    assert stat.S_IMODE(target_auth.stat().st_mode) == 0o600


def test_host_codex_runtime_never_copies_host_auth_in_enterprise_mode(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.binary = "/bin/echo"
    _patch_host_codex_requirement_probe(monkeypatch)
    source_codex_home = tmp_path / "source-codex-home"
    source_codex_home.mkdir()
    (source_codex_home / "auth.json").write_text(
        '{"OPENAI_API_KEY":"synthetic-test-key"}'
    )
    monkeypatch.setenv("CODEX_HOME", str(source_codex_home))
    monkeypatch.setenv("WPR_ENTERPRISE_MODE", "1")

    def fake_run(args, **_kwargs):
        return subprocess.CompletedProcess(
            args,
            returncode=0,
            stdout="codex-cli 0.144.1\n" if "--version" in args else "",
            stderr="",
        )

    monkeypatch.setattr("workers_projects_runtime.profile_runtime.subprocess.run", fake_run)
    worker = {
        "worker_id": "wrk_host_auth_enterprise",
        "name": "Enterprise Host Worker",
        "role": "general",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "workspaces"),
    }

    runtime.ensure_worker_ready(worker)

    assert not (runtime._host_codex_home(worker) / "auth.json").exists()


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
            stdout="codex-cli 0.144.1\n" if "--version" in args else "",
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


def test_host_plugin_denylist_disables_only_selected_codex_and_claude_plugin(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "synthetic-test-access")
    denied_plugin = "viventium-feelings@project-viventium"
    monkeypatch.setenv("GLASSHIVE_HOST_PLUGIN_DENYLIST", denied_plugin)
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", "0")

    source_codex_home = tmp_path / "source-codex-home"
    source_codex_home.mkdir()
    source_codex_config = source_codex_home / "config.toml"
    source_codex_config.write_text(
        '[plugins."viventium-feelings@project-viventium"]\n'
        'enabled = true\n\n'
        '[plugins."chrome@openai-bundled"]\n'
        'enabled = true\n'
    )
    monkeypatch.setenv("CODEX_HOME", str(source_codex_home))

    codex_runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "codex-state"))
    codex_config = tomllib.loads(codex_runtime._host_codex_worker_config(""))

    assert codex_config["plugins"][denied_plugin]["enabled"] is False
    assert codex_config["plugins"]["chrome@openai-bundled"]["enabled"] is True

    codex_worker = {
        "worker_id": "wrk_codex_plugin_policy",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "codex-workspace"),
        "bootstrap_bundle_json": json.dumps({"run_mode": "mission"}),
    }
    codex_workspace = codex_runtime._host_workspace_dir(codex_worker)
    codex_runtime._materialize_workspace(codex_worker, codex_workspace)
    _, codex_env = codex_runtime._build_command(
        codex_worker,
        "Complete the task.",
        codex_runtime._host_runtime_info(codex_worker),
    )
    worker_codex_config = tomllib.loads(
        (codex_runtime._host_codex_home(codex_worker) / "config.toml").read_text()
    )
    assert worker_codex_config["plugins"][denied_plugin]["enabled"] is False
    assert codex_env["CODEX_HOME"] == str(
        codex_runtime._host_codex_home(codex_worker)
    )
    assert denied_plugin not in codex_runtime._command_stdin_text(
        codex_worker,
        "Complete the task.",
        codex_runtime._host_runtime_info(codex_worker),
    )
    source_config = tomllib.loads(source_codex_config.read_text())
    assert source_config["plugins"][denied_plugin]["enabled"] is True
    assert source_config["plugins"]["chrome@openai-bundled"]["enabled"] is True

    claude_runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "claude-state"))
    worker = {
        "worker_id": "wrk_plugin_policy",
        "profile": "claude-code",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "workspace"),
        "model": "opus",
        "bootstrap_bundle_json": json.dumps({"run_mode": "mission"}),
    }
    command, _ = claude_runtime._build_command(
        worker,
        "Complete the task.",
        claude_runtime._host_runtime_info(worker),
    )

    settings = json.loads(command[command.index("--settings") + 1])
    assert settings == {"enabledPlugins": {denied_plugin: False}}
    assert denied_plugin not in claude_runtime._command_stdin_text(
        worker,
        "Complete the task.",
        claude_runtime._host_runtime_info(worker),
    )


def test_host_plugin_denylist_rejects_noncanonical_plugin_ids(tmp_path, monkeypatch):
    monkeypatch.setenv("GLASSHIVE_HOST_PLUGIN_DENYLIST", "missing-marketplace")
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "codex-state"))

    with pytest.raises(RuntimeErrorBase, match="canonical name@marketplace"):
        runtime._host_codex_worker_config("")


def test_host_codex_personality_is_optional_and_native_config_owned(tmp_path, monkeypatch):
    source_codex_home = tmp_path / "source-codex-home"
    source_codex_home.mkdir()
    (source_codex_home / "config.toml").write_text('personality = "pragmatic"\n')
    monkeypatch.setenv("CODEX_HOME", str(source_codex_home))
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "codex-state"))

    monkeypatch.setenv("WPR_CODEX_CLI_PERSONALITY", "inherit")
    inherited = tomllib.loads(runtime._host_codex_worker_config(""))
    monkeypatch.setenv("WPR_CODEX_CLI_PERSONALITY", "none")
    disabled = tomllib.loads(runtime._host_codex_worker_config(""))

    assert inherited["personality"] == "pragmatic"
    assert disabled["personality"] == "none"


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
        "trusted_run_lane": "conversation",
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


def test_standalone_glasshive_codex_personality_still_inherits_when_unconfigured(
    tmp_path, monkeypatch
):
    source_codex_home = tmp_path / "source-codex-home"
    source_codex_home.mkdir()
    (source_codex_home / "config.toml").write_text('personality = "pragmatic"\n')
    monkeypatch.setenv("CODEX_HOME", str(source_codex_home))
    monkeypatch.delenv("WPR_CODEX_CLI_PERSONALITY", raising=False)
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "codex-state"))

    inherited = tomllib.loads(runtime._host_codex_worker_config(""))

    assert inherited["personality"] == "pragmatic"


def test_host_codex_rejects_invalid_personality(tmp_path, monkeypatch):
    monkeypatch.setenv("WPR_CODEX_CLI_PERSONALITY", "warm")
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "codex-state"))

    with pytest.raises(RuntimeErrorBase, match="Codex personality"):
        runtime._host_codex_worker_config("")


def test_host_codex_plugin_denylist_fails_closed_if_worker_config_drifts(
    tmp_path, monkeypatch
):
    denied_plugin = "synthetic-policy@project-viventium"
    monkeypatch.setenv("GLASSHIVE_HOST_PLUGIN_DENYLIST", denied_plugin)
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "codex-state"))
    life = tmp_path / "Life"
    life.mkdir()
    worker = {
        "worker_id": "wrk_codex_policy_drift",
        "trusted_run_lane": "conversation",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(life),
        "bootstrap_bundle_json": json.dumps({"run_mode": "conversation"}),
    }
    runtime._materialize_workspace(worker, life)
    config_path = runtime._host_codex_home(worker) / "config.toml"
    config_path.write_text(
        f'[plugins."{denied_plugin}"]\n'
        "enabled = true\n"
    )

    with pytest.raises(RuntimeErrorBase, match="denylist policy"):
        runtime._build_command(
            worker,
            "Continue.",
            runtime._host_runtime_info(worker),
        )


def test_host_codex_personality_fails_closed_if_worker_config_drifts(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WPR_CODEX_CLI_PERSONALITY", "none")
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "codex-state"))
    life = tmp_path / "Life"
    life.mkdir()
    worker = {
        "worker_id": "wrk_codex_personality_drift",
        "trusted_run_lane": "conversation",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(life),
        "bootstrap_bundle_json": json.dumps({"run_mode": "conversation"}),
    }
    runtime._materialize_workspace(worker, life)
    config_path = runtime._host_codex_home(worker) / "config.toml"
    config_path.write_text('personality = "pragmatic"\n')

    with pytest.raises(RuntimeErrorBase, match="personality policy"):
        runtime._build_command(
            worker,
            "Continue.",
            runtime._host_runtime_info(worker),
        )


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
            stdout="codex-cli 0.144.1\n" if "--version" in args else "",
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


def test_host_codex_runtime_uses_canonical_binary_when_symlink_hides_companion(tmp_path, monkeypatch):
    bundle_cli = tmp_path / "Codex.app" / "Contents" / "Resources" / "codex"
    bundle_cli.parent.mkdir(parents=True)
    bundle_cli.write_text("#!/usr/bin/env bash\nexit 0\n")
    bundle_cli.chmod(0o755)
    companion = bundle_cli.parent / "codex-code-mode-host"
    companion.write_text("#!/usr/bin/env bash\nexit 0\n")
    companion.chmod(0o755)
    path_link = tmp_path / "bin" / "codex"
    path_link.parent.mkdir()
    path_link.symlink_to(bundle_cli)
    monkeypatch.setenv("WPR_CODEX_BIN", str(path_link))

    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))

    assert runtime.binary == str(bundle_cli)


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


def test_docker_sandbox_host_worker_tree_is_owner_only_and_never_world_writable(
    tmp_path, monkeypatch
):
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    paths = runtime.sandbox.paths("wrk-private")
    runtime.sandbox._ensure_host_dirs(paths)
    runtime_env = paths["home_dir"] / ".glasshive" / "runtime.env"
    auth_json = paths["home_dir"] / ".codex" / "auth.json"
    prompt = paths["workspace_dir"] / "private-prompt.txt"
    script = paths["home_dir"] / ".glasshive-runs" / "run-one" / "run.sh"
    for path in (runtime_env, auth_json, prompt, script):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic")
        path.chmod(0o777)
    script.chmod(0o755)

    captured: dict[str, object] = {}

    def fake_exec(_container, command, **_kwargs):
        captured["script"] = command[-1]
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(runtime.sandbox, "_docker_exec", fake_exec)
    runtime.sandbox._ensure_container_writable_paths(
        runtime.sandbox._container_name("wrk-private"),
        [runtime.sandbox.home_mount, runtime.sandbox.workspace_mount],
    )

    assert "a+rwX" not in str(captured["script"])
    assert "go-rwx" in str(captured["script"])
    for root, directories, files in os.walk(paths["worker_root"]):
        assert Path(root).stat().st_mode & 0o077 == 0
        for name in directories:
            assert (Path(root) / name).stat().st_mode & 0o077 == 0
        for name in files:
            assert (Path(root) / name).stat().st_mode & 0o077 == 0
    assert runtime_env.stat().st_mode & 0o777 == 0o600
    assert auth_json.stat().st_mode & 0o777 == 0o600
    assert prompt.stat().st_mode & 0o777 == 0o600
    assert script.stat().st_mode & 0o777 == 0o700

    # A later container repair must re-harden even when the durable migration
    # marker already exists.
    runtime_env.chmod(0o666)
    paths["home_dir"].chmod(0o777)
    runtime.sandbox._ensure_container_writable_paths(
        runtime.sandbox._container_name("wrk-private"),
        [runtime.sandbox.home_mount, runtime.sandbox.workspace_mount],
    )
    assert paths["home_dir"].stat().st_mode & 0o777 == 0o700
    assert runtime_env.stat().st_mode & 0o777 == 0o600


def test_docker_sandbox_hardener_never_follows_worker_created_symlinks(
    tmp_path, monkeypatch
):
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    paths = runtime.sandbox.paths("wrk-symlink")
    runtime.sandbox._ensure_host_dirs(paths)
    outside_file = tmp_path / "outside-secret.env"
    outside_dir = tmp_path / "outside-private"
    outside_file.write_text("synthetic")
    outside_file.chmod(0o640)
    outside_dir.mkdir(mode=0o750)
    (paths["workspace_dir"] / "outside-file-link").symlink_to(outside_file)
    (paths["workspace_dir"] / "outside-dir-link").symlink_to(
        outside_dir, target_is_directory=True
    )
    monkeypatch.setattr(
        runtime.sandbox,
        "_docker_exec",
        lambda _container, command, **_kwargs: subprocess.CompletedProcess(
            command, 0, "", ""
        ),
    )

    runtime.sandbox._ensure_container_writable_paths(
        runtime.sandbox._container_name("wrk-symlink"),
        [runtime.sandbox.home_mount, runtime.sandbox.workspace_mount],
    )

    assert outside_file.stat().st_mode & 0o777 == 0o640
    assert outside_dir.stat().st_mode & 0o777 == 0o750
    assert (paths["workspace_dir"] / "outside-file-link").is_symlink()
    assert (paths["workspace_dir"] / "outside-dir-link").is_symlink()


def test_docker_sandbox_rejects_preexisting_symlink_for_private_mount_root(tmp_path):
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    paths = runtime.sandbox.paths("wrk-root-symlink")
    paths["state_dir"].mkdir(parents=True)
    outside_dir = tmp_path / "outside-workspace"
    outside_dir.mkdir(mode=0o750)
    paths["workspace_dir"].symlink_to(outside_dir, target_is_directory=True)

    with pytest.raises(RuntimeError, match="not a real directory"):
        runtime.sandbox._ensure_host_dirs(paths)

    assert outside_dir.stat().st_mode & 0o777 == 0o750


def test_existing_running_docker_sandbox_is_migrated_before_fast_return(
    tmp_path, monkeypatch
):
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    paths = runtime.sandbox.paths("wrk-existing-insecure")
    paths["home_dir"].mkdir(parents=True)
    paths["workspace_dir"].mkdir(parents=True)
    runtime_env = paths["home_dir"] / ".glasshive" / "runtime.env"
    auth_json = paths["home_dir"] / ".codex" / "auth.json"
    upload = paths["workspace_dir"] / "uploads" / "private.txt"
    for path in (runtime_env, auth_json, upload):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic")
        path.chmod(0o666)
    for path in (
        paths["worker_root"],
        paths["state_dir"],
        paths["home_dir"],
        paths["workspace_dir"],
    ):
        path.chmod(0o777)
    sandbox = SimpleNamespace(
        container_name=runtime.sandbox._container_name("wrk-existing-insecure"),
        container_id="container-existing",
        state="running",
        pid=9002,
        security_options=(),
    )
    monkeypatch.setattr(runtime.sandbox, "_require_docker", lambda: None)
    monkeypatch.setattr(
        runtime.sandbox, "_seed_bootstrap", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(runtime.sandbox, "inspect", lambda _worker_id: sandbox)
    monkeypatch.setattr(runtime.sandbox, "_harden_secret_runtime_files", lambda *_args: None)

    result = runtime.sandbox.ensure_ready(
        {
            "worker_id": "wrk-existing-insecure",
            "state": "running",
            "container_id": "container-existing",
        },
        "claude-code",
    )

    assert result is sandbox
    assert paths["state_dir"].stat().st_mode & 0o777 == 0o700
    assert runtime_env.stat().st_mode & 0o777 == 0o600
    assert auth_json.stat().st_mode & 0o777 == 0o600
    assert upload.stat().st_mode & 0o777 == 0o600
    assert runtime.sandbox._worker_permissions_marker(paths["worker_root"]).is_file()


def test_docker_sandbox_rejects_broken_permission_marker_without_recursing(tmp_path):
    runtime = ClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    paths = runtime.sandbox.paths("wrk-broken-marker")
    runtime.sandbox._ensure_host_dirs(paths)
    marker = runtime.sandbox._worker_permissions_marker(paths["worker_root"])
    marker.symlink_to(tmp_path / "missing-marker-target")

    with pytest.raises(RuntimeError, match="marker is not trustworthy"):
        runtime.sandbox._ensure_worker_permissions_migrated(paths["worker_root"])


def _healthy_parallel_proxy_inspect_payload(
    *, container_name: str, role: str, aliases: list[str], network_name: str
) -> str:
    networks = {
        network_name: {"Aliases": aliases},
    }
    networks["provider-egress"] = {"Aliases": [container_name]}
    upstream = "http://host.docker.internal:3180"
    return json.dumps(
        [
            {
                "Id": ("a" if role == "provider-proxy" else "b") * 64,
                "Image": "sha256:" + ("d" * 64),
                "State": {"Running": True, "Health": {"Status": "healthy"}},
                "Config": {
                    "Image": "viventium-parallel-work-proxy:local",
                    "User": "glasshive",
                    "Entrypoint": ["python", "/app/proxy.py"],
                    "Cmd": None,
                    "Env": [
                        "PATH=/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin",
                        f"VIVENTIUM_PARALLEL_PROXY_ROLE={'provider' if role == 'provider-proxy' else 'broker'}",
                        f"VIVENTIUM_PARALLEL_PROXY_UPSTREAM={upstream}",
                        "VIVENTIUM_PARALLEL_PROXY_PORT=8080",
                    ],
                    "Labels": {
                        "com.viventium.parallel-clean-room.policy": (
                            PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
                        ),
                        "com.viventium.parallel-clean-room.role": role,
                    },
                },
                "HostConfig": {
                    "NetworkMode": "provider-egress",
                    "PidMode": "",
                    "IpcMode": "private",
                    "UTSMode": "",
                    "UsernsMode": "",
                    "CgroupnsMode": "private",
                    "ReadonlyRootfs": True,
                    "Privileged": False,
                    "CapAdd": None,
                    "CapDrop": ["ALL"],
                    "SecurityOpt": ["no-new-privileges:true"],
                    "ExtraHosts": ["host.docker.internal:host-gateway"],
                    "Tmpfs": {
                        "/tmp": "rw,nosuid,nodev,noexec,size=16m,mode=1777"
                    },
                    "PortBindings": {},
                    "Devices": [],
                    "DeviceRequests": None,
                    "Binds": None,
                    "PublishAllPorts": False,
                    "Memory": 134217728,
                    "NanoCpus": 500000000,
                    "PidsLimit": 64,
                },
                "Mounts": [],
                "NetworkSettings": {"Networks": networks},
            }
        ]
    )


def _parallel_proxy_image_inspect_payload() -> str:
    return json.dumps(
        [
            {
                "Id": "sha256:" + ("d" * 64),
                "Config": {
                    "User": "glasshive",
                    "Entrypoint": ["python", "/app/proxy.py"],
                    "Cmd": None,
                    "Env": [
                        "PATH=/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin"
                    ],
                },
            }
        ]
    )


def _parallel_network_inspect_payload(
    *, network_name: str, provider_container: str, broker_container: str
) -> str:
    return json.dumps(
        [
            {
                "Name": network_name,
                "Driver": "bridge",
                "Internal": True,
                "Labels": {
                    "com.viventium.parallel-clean-room.policy": (
                        PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
                    )
                },
                "Containers": {
                    "a" * 64: {"Name": provider_container},
                    "b" * 64: {"Name": broker_container},
                },
            }
        ]
    )


def test_parallel_readiness_rejects_http_proxy_without_docker_policy_attestation(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HTTP_PROXY", "http://ambient-proxy.example:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://ambient-proxy.example:8080")
    monkeypatch.delenv("WPR_PARALLEL_CLEAN_ROOM_NETWORK", raising=False)
    monkeypatch.delenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_CONTAINER", raising=False
    )
    monkeypatch.delenv(
        "WPR_PARALLEL_CLEAN_ROOM_BROKER_PROXY_CONTAINER", raising=False
    )
    runtime = ProfiledWorkerRuntime(base_dir=str(tmp_path / "data"))

    def fake_docker(args, **_kwargs):
        if args[:2] in (["info", "--format"], ["image", "inspect"]):
            return subprocess.CompletedProcess(args, 0, "synthetic-ready", "")
        raise AssertionError(f"unexpected Docker probe: {args}")

    monkeypatch.setattr(runtime.codex.sandbox, "_docker", fake_docker)
    monkeypatch.setattr(
        runtime,
        "isolated_resource_usage",
        lambda **_kwargs: {
            "process_probe_ok": True,
            "memory_probe_ok": True,
            "disk_probe_ok": True,
        },
    )

    readiness = runtime.refresh_isolated_parallel_readiness()

    assert readiness["ready"] is False
    assert readiness["reason"] == "parallel_clean_room_network_unconfigured"
    assert readiness["policy"] == PARALLEL_CLEAN_ROOM_EXECUTION_POLICY
    assert readiness["attestations"]["network"] == "unconfigured"


def test_parallel_readiness_attests_internal_network_and_two_healthy_proxies(
    tmp_path, monkeypatch
):
    network_name = "glasshive-parallel-clean-room"
    provider_container = "glasshive-provider-egress"
    broker_container = "glasshive-capability-broker-proxy"
    monkeypatch.setenv("WPR_PARALLEL_CLEAN_ROOM_NETWORK", network_name)
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_CONTAINER", provider_container
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL",
        "http://provider-egress:8080",
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_EGRESS_NETWORK", "provider-egress"
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_BROKER_PROXY_CONTAINER", broker_container
    )
    runtime = ProfiledWorkerRuntime(base_dir=str(tmp_path / "data"))
    seen: list[tuple[str, ...]] = []

    def fake_docker(args, **_kwargs):
        seen.append(tuple(args))
        if args[:2] == ["info", "--format"]:
            return subprocess.CompletedProcess(args, 0, "synthetic-ready", "")
        if args[:2] == ["image", "inspect"]:
            return subprocess.CompletedProcess(
                args, 0, _parallel_proxy_image_inspect_payload(), ""
            )
        if args[:2] == ["network", "inspect"]:
            return subprocess.CompletedProcess(
                args,
                0,
                _parallel_network_inspect_payload(
                    network_name=network_name,
                    provider_container=provider_container,
                    broker_container=broker_container,
                ),
                "",
            )
        if args == ["inspect", provider_container]:
            return subprocess.CompletedProcess(
                args,
                0,
                _healthy_parallel_proxy_inspect_payload(
                    container_name=provider_container,
                    role="provider-proxy",
                    aliases=[provider_container, "provider-egress"],
                    network_name=network_name,
                ),
                "",
            )
        if args == ["inspect", broker_container]:
            return subprocess.CompletedProcess(
                args,
                0,
                _healthy_parallel_proxy_inspect_payload(
                    container_name=broker_container,
                    role="broker-proxy",
                    aliases=[broker_container, "host.docker.internal"],
                    network_name=network_name,
                ),
                "",
            )
        raise AssertionError(f"unexpected Docker probe: {args}")

    monkeypatch.setattr(runtime.codex.sandbox, "_docker", fake_docker)
    monkeypatch.setattr(
        runtime,
        "isolated_resource_usage",
        lambda **_kwargs: {
            "process_probe_ok": True,
            "memory_probe_ok": True,
            "disk_probe_ok": True,
        },
    )

    readiness = runtime.refresh_isolated_parallel_readiness()

    assert readiness == {
        "ready": True,
        "reason": "",
        "policy": PARALLEL_CLEAN_ROOM_EXECUTION_POLICY,
        "attestations": {
            "network": "internal",
            "providerProxy": "healthy",
            "brokerProxy": "healthy",
        },
    }
    assert ("network", "inspect", network_name) in seen
    assert ("inspect", provider_container) in seen
    assert ("inspect", broker_container) in seen


def test_parallel_readiness_rejects_critical_alias_owned_by_rogue_endpoint(
    tmp_path, monkeypatch
):
    network_name = "glasshive-parallel-clean-room"
    provider_container = "glasshive-provider-egress"
    broker_container = "glasshive-capability-broker-proxy"
    rogue_container = "synthetic-rogue-endpoint"
    monkeypatch.setenv("WPR_PARALLEL_CLEAN_ROOM_NETWORK", network_name)
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_CONTAINER", provider_container
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL",
        "http://provider-egress:8080",
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_EGRESS_NETWORK", "provider-egress"
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_BROKER_PROXY_CONTAINER", broker_container
    )
    runtime = ProfiledWorkerRuntime(base_dir=str(tmp_path / "data"))

    def fake_docker(args, **_kwargs):
        if args[:2] == ["image", "inspect"]:
            return subprocess.CompletedProcess(
                args, 0, _parallel_proxy_image_inspect_payload(), ""
            )
        if args[:2] == ["network", "inspect"]:
            payload = json.loads(
                _parallel_network_inspect_payload(
                    network_name=network_name,
                    provider_container=provider_container,
                    broker_container=broker_container,
                )
            )
            payload[0]["Containers"]["c" * 64] = {"Name": rogue_container}
            return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")
        if args == ["inspect", provider_container]:
            output = _healthy_parallel_proxy_inspect_payload(
                container_name=provider_container,
                role="provider-proxy",
                aliases=[provider_container, "provider-egress"],
                network_name=network_name,
            )
            return subprocess.CompletedProcess(args, 0, output, "")
        if args == ["inspect", broker_container]:
            output = _healthy_parallel_proxy_inspect_payload(
                container_name=broker_container,
                role="broker-proxy",
                aliases=[broker_container, "host.docker.internal"],
                network_name=network_name,
            )
            return subprocess.CompletedProcess(args, 0, output, "")
        if args == ["inspect", "c" * 64]:
            return subprocess.CompletedProcess(
                args,
                0,
                json.dumps(
                    [
                        {
                            "Id": "c" * 64,
                            "NetworkSettings": {
                                "Networks": {
                                    network_name: {
                                        "Aliases": [
                                            rogue_container,
                                            "provider-egress",
                                            "host.docker.internal",
                                        ]
                                    }
                                }
                            },
                        }
                    ]
                ),
                "",
            )
        raise AssertionError(f"unexpected Docker probe: {args}")

    monkeypatch.setattr(runtime.codex.sandbox, "_docker", fake_docker)
    readiness = runtime.codex.sandbox.parallel_clean_room_readiness()

    assert readiness["ready"] is False
    assert readiness["reason"] == "parallel_clean_room_network_alias_ambiguous"


@pytest.mark.parametrize(
    ("role", "extra_network", "expected_reason"),
    [
        (
            "provider-proxy",
            "unreviewed-third-network",
            "parallel_clean_room_provider_proxy_network_set_mismatch",
        ),
        (
            "broker-proxy",
            "direct-egress-network",
            "parallel_clean_room_broker_proxy_network_set_mismatch",
        ),
    ],
)
def test_parallel_readiness_rejects_proxy_network_expansion(
    tmp_path, monkeypatch, role, extra_network, expected_reason
):
    network_name = "glasshive-parallel-clean-room"
    provider_container = "glasshive-provider-egress"
    broker_container = "glasshive-capability-broker-proxy"
    monkeypatch.setenv("WPR_PARALLEL_CLEAN_ROOM_NETWORK", network_name)
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_CONTAINER", provider_container
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL",
        "http://provider-egress:8080",
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_EGRESS_NETWORK", "provider-egress"
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_BROKER_PROXY_CONTAINER", broker_container
    )
    runtime = ProfiledWorkerRuntime(base_dir=str(tmp_path / "data"))

    def fake_docker(args, **_kwargs):
        if args[:2] == ["image", "inspect"]:
            return subprocess.CompletedProcess(
                args, 0, _parallel_proxy_image_inspect_payload(), ""
            )
        if args[:2] == ["network", "inspect"]:
            return subprocess.CompletedProcess(
                args,
                0,
                _parallel_network_inspect_payload(
                    network_name=network_name,
                    provider_container=provider_container,
                    broker_container=broker_container,
                ),
                "",
            )
        if args[0] == "inspect":
            container_name = args[1]
            container_role = (
                "provider-proxy"
                if container_name == provider_container
                else "broker-proxy"
            )
            aliases = (
                [container_name, "provider-egress"]
                if container_role == "provider-proxy"
                else [container_name, "host.docker.internal"]
            )
            payload = json.loads(
                _healthy_parallel_proxy_inspect_payload(
                    container_name=container_name,
                    role=container_role,
                    aliases=aliases,
                    network_name=network_name,
                )
            )
            if container_role == role:
                payload[0]["NetworkSettings"]["Networks"][extra_network] = {
                    "Aliases": [container_name]
                }
            return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")
        raise AssertionError(f"unexpected Docker probe: {args}")

    monkeypatch.setattr(runtime.codex.sandbox, "_docker", fake_docker)
    readiness = runtime.codex.sandbox.parallel_clean_room_readiness()

    assert readiness["ready"] is False
    assert readiness["reason"] == expected_reason


@pytest.mark.parametrize(
    "drift",
    [
        "wrong_image",
        "root_user",
        "privileged",
        "cap_add",
        "writable_root",
        "unexpected_mount",
        "ambient_secret",
        "published_port",
    ],
)
def test_parallel_readiness_rejects_untrusted_proxy_substrate(
    tmp_path, monkeypatch, drift
):
    network_name = "glasshive-parallel-clean-room"
    provider_container = "glasshive-provider-egress"
    broker_container = "glasshive-capability-broker-proxy"
    monkeypatch.setenv("WPR_PARALLEL_CLEAN_ROOM_NETWORK", network_name)
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_CONTAINER", provider_container
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL",
        "http://provider-egress:8080",
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_EGRESS_NETWORK", "provider-egress"
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_BROKER_PROXY_CONTAINER", broker_container
    )
    monkeypatch.setenv("VIVENTIUM_LC_API_PORT", "3180")
    runtime = ProfiledWorkerRuntime(base_dir=str(tmp_path / "data"))

    def fake_docker(args, **_kwargs):
        if args[:2] == ["image", "inspect"]:
            return subprocess.CompletedProcess(
                args, 0, _parallel_proxy_image_inspect_payload(), ""
            )
        if args[:2] == ["network", "inspect"]:
            return subprocess.CompletedProcess(
                args,
                0,
                _parallel_network_inspect_payload(
                    network_name=network_name,
                    provider_container=provider_container,
                    broker_container=broker_container,
                ),
                "",
            )
        if args[0] == "inspect":
            container_name = args[1]
            role = (
                "provider-proxy"
                if container_name == provider_container
                else "broker-proxy"
            )
            aliases = (
                [container_name, "provider-egress"]
                if role == "provider-proxy"
                else [container_name, "host.docker.internal"]
            )
            payload = json.loads(
                _healthy_parallel_proxy_inspect_payload(
                    container_name=container_name,
                    role=role,
                    aliases=aliases,
                    network_name=network_name,
                )
            )
            if role == "provider-proxy":
                entry = payload[0]
                if drift == "wrong_image":
                    entry["Image"] = "sha256:" + ("e" * 64)
                elif drift == "root_user":
                    entry["Config"]["User"] = "root"
                elif drift == "privileged":
                    entry["HostConfig"]["Privileged"] = True
                elif drift == "cap_add":
                    entry["HostConfig"]["CapAdd"] = ["SYS_ADMIN"]
                elif drift == "writable_root":
                    entry["HostConfig"]["ReadonlyRootfs"] = False
                elif drift == "unexpected_mount":
                    entry["Mounts"] = [
                        {
                            "Type": "bind",
                            "Source": "/var/run/docker.sock",
                            "Destination": "/var/run/docker.sock",
                        }
                    ]
                elif drift == "ambient_secret":
                    entry["Config"]["Env"].append(
                        "OPENAI_API_KEY=synthetic-forbidden-secret"
                    )
                elif drift == "published_port":
                    entry["HostConfig"]["PortBindings"] = {
                        "8080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8080"}]
                    }
            return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")
        raise AssertionError(f"unexpected Docker probe: {args}")

    monkeypatch.setattr(runtime.codex.sandbox, "_docker", fake_docker)

    readiness = runtime.codex.sandbox.parallel_clean_room_readiness()

    assert readiness["ready"] is False
    assert readiness["reason"] == "parallel_clean_room_provider_proxy_policy_mismatch"


def test_parallel_readiness_fails_closed_when_attested_proxy_is_unhealthy(
    tmp_path, monkeypatch
):
    network_name = "glasshive-parallel-clean-room"
    provider_container = "glasshive-provider-egress"
    broker_container = "glasshive-capability-broker-proxy"
    monkeypatch.setenv("WPR_PARALLEL_CLEAN_ROOM_NETWORK", network_name)
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_CONTAINER", provider_container
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL",
        "http://provider-egress:8080",
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_EGRESS_NETWORK", "provider-egress"
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_BROKER_PROXY_CONTAINER", broker_container
    )
    runtime = ProfiledWorkerRuntime(base_dir=str(tmp_path / "data"))

    def fake_docker(args, **_kwargs):
        if args[:2] == ["info", "--format"]:
            return subprocess.CompletedProcess(args, 0, "synthetic-ready", "")
        if args[:2] == ["image", "inspect"]:
            return subprocess.CompletedProcess(
                args, 0, _parallel_proxy_image_inspect_payload(), ""
            )
        if args[:2] == ["network", "inspect"]:
            return subprocess.CompletedProcess(
                args,
                0,
                _parallel_network_inspect_payload(
                    network_name=network_name,
                    provider_container=provider_container,
                    broker_container=broker_container,
                ),
                "",
            )
        if args == ["inspect", provider_container]:
            payload = json.loads(
                _healthy_parallel_proxy_inspect_payload(
                    container_name=provider_container,
                    role="provider-proxy",
                    aliases=[provider_container, "provider-egress"],
                    network_name=network_name,
                )
            )
            payload[0]["State"]["Health"]["Status"] = "unhealthy"
            return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")
        raise AssertionError(f"unexpected Docker probe: {args}")

    monkeypatch.setattr(runtime.codex.sandbox, "_docker", fake_docker)

    readiness = runtime.refresh_isolated_parallel_readiness()

    assert readiness["ready"] is False
    assert readiness["reason"] == "parallel_clean_room_provider_proxy_unhealthy"
    assert readiness["attestations"]["network"] == "internal"
    assert readiness["attestations"]["providerProxy"] == "unhealthy"
    assert readiness["attestations"]["brokerProxy"] == "unverified"


def test_docker_resource_probe_measures_vm_and_container_processes_not_host_ps(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WPR_DOCKER_DISK_BUDGET_MB", "65536")
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.sandbox.paths("wrk-measured")["worker_root"].mkdir(parents=True)
    container_name = runtime.sandbox._container_name("wrk-measured")

    def fake_docker(args, **_kwargs):
        if args[:2] == ["info", "--format"]:
            output = json.dumps(
                {"MemTotal": 16 * 1024**3, "DockerRootDir": "/not/host-visible"}
            )
        elif args[:2] == ["ps", "--format"]:
            output = f"{container_name}\nunrelated-container\n"
        elif args[:2] == ["stats", "--no-stream"]:
            output = "\n".join(
                (
                    json.dumps({"Name": container_name, "MemUsage": "2GiB / 16GiB"}),
                    json.dumps({"Name": "unrelated-container", "MemUsage": "1GiB / 16GiB"}),
                )
            )
        elif args[:3] == ["system", "df", "--format"]:
            output = json.dumps({"Type": "Images", "Size": "4GB"})
        elif args == ["top", container_name, "-eo", "pid,tid"]:
            # Docker Desktop rejects the GNU-style ``pid=,tid=`` format but
            # accepts this header-bearing portable form.
            output = "PID TID\n1 1\n1 2\n2 3\n"
        elif args[0] == "exec":
            output = "1B-blocks Used Available\n68719476736 4000000000 64719476736\n"
        else:  # pragma: no cover - makes unexpected Docker probes visible
            raise AssertionError(args)
        return subprocess.CompletedProcess(args, 0, output, "")

    monkeypatch.setattr(runtime.sandbox, "_docker", fake_docker)

    usage = runtime.sandbox.resource_usage()

    assert usage.child_processes == 2
    assert usage.threads == 3
    assert usage.running_worker_containers == 1
    assert usage.worker_process_counts == (("wrk-measured", 2, 3),)
    assert usage.available_memory_bytes == 13 * 1024**3
    assert 0 < usage.available_disk_bytes <= 64_719_476_736
    assert usage.process_probe_ok is True
    assert usage.memory_probe_ok is True
    assert usage.disk_probe_ok is True


def test_docker_resource_probe_fails_closed_when_container_processes_are_unknown(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WPR_DOCKER_DISK_BUDGET_MB", "65536")
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    runtime.sandbox.paths("wrk-unmeasured")["worker_root"].mkdir(parents=True)
    container_name = runtime.sandbox._container_name("wrk-unmeasured")

    def fake_docker(args, **_kwargs):
        if args[:2] == ["info", "--format"]:
            output = json.dumps(
                {"MemTotal": 16 * 1024**3, "DockerRootDir": "/not/host-visible"}
            )
            return subprocess.CompletedProcess(args, 0, output, "")
        if args[:2] == ["ps", "--format"]:
            return subprocess.CompletedProcess(args, 0, container_name, "")
        if args[:2] == ["stats", "--no-stream"]:
            return subprocess.CompletedProcess(
                args, 0, json.dumps({"MemUsage": "1GiB / 16GiB"}), ""
            )
        if args[:3] == ["system", "df", "--format"]:
            return subprocess.CompletedProcess(
                args, 0, json.dumps({"Size": "1GB"}), ""
            )
        if args == ["top", container_name, "-eo", "pid,tid"]:
            return subprocess.CompletedProcess(args, 1, "", "unavailable")
        if args[0] == "exec":
            return subprocess.CompletedProcess(
                args, 0, "1B-blocks Used Available\n68719476736 1 64719476735\n", ""
            )
        raise AssertionError(args)

    monkeypatch.setattr(runtime.sandbox, "_docker", fake_docker)

    usage = runtime.sandbox.resource_usage()

    assert usage.process_probe_ok is False


def test_docker_worker_default_pid_ceiling_matches_reserved_thread_envelope(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))

    assert runtime.sandbox.pids_limit == "512"


def test_docker_exact_run_identity_survives_executor_restart_only_while_session_is_live(
    tmp_path, monkeypatch
):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk-docker-reconcile",
        "profile": "codex-cli",
        "execution_mode": "docker",
    }
    runtime._state_dir(worker["worker_id"]).mkdir(parents=True)
    identity = "docker:container-immutable:job-run-reconcile:run-reconcile:4321"
    runtime._write_active_session(
        worker["worker_id"],
        {
            "session_name": "job-run-reconcile",
            "run_id": "run-reconcile",
            "process_pid": 4321,
            "lease_pid": 9001,
            "lease_process_group": 9001,
            "lease_process_start_identity": identity,
        },
    )
    monkeypatch.setattr(
        runtime.sandbox,
        "inspect",
        lambda _worker_id: SimpleNamespace(
            state="running", container_id="container-immutable", pid=9001
        ),
    )
    monkeypatch.setattr(
        runtime.sandbox,
        "screen_session_pid",
        lambda *_args, **_kwargs: 4321,
    )

    assert runtime.host_process_identity(worker, "run-reconcile") == {
        "identity_kind": "docker_session",
        "pid": 9001,
        "process_group": 9001,
        "process_start_identity": identity,
        "container_id": "container-immutable",
        "session_id": "job-run-reconcile",
        "startup_token_digest": "",
        "verified": True,
    }
    assert runtime.host_process_identity(worker, "run-other") is None

    monkeypatch.setattr(
        runtime.sandbox,
        "screen_session_pid",
        lambda *_args, **_kwargs: 9876,
    )
    assert runtime.host_process_identity(worker, "run-reconcile") is None


def test_host_unconfirmed_restart_cleanup_fences_when_pid_death_is_not_proven(
    tmp_path, monkeypatch
):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "private-state"))
    worker = {
        "worker_id": "wrk-host-start-cleanup-uncertain",
        "profile": "codex-cli",
        "execution_mode": "host",
    }
    identity = {
        "identity_kind": "host_process",
        "pid": 7411,
        "process_group": 7411,
        "process_start_identity": "ps-lstart:captured-host-generation",
        "container_id": "",
        "session_id": "host-captured",
    }
    monkeypatch.setattr(runtime, "_recorded_process_is_running", lambda *_args: False)
    monkeypatch.setattr(runtime, "_recorded_pid_is_proven_gone", lambda *_args: False)

    assert runtime.cleanup_unconfirmed_run_start(
        worker,
        "run-host-cleanup-uncertain",
        identity,
    ) is False


def test_host_unconfirmed_restart_cleanup_accepts_only_proven_old_generation_absence(
    tmp_path, monkeypatch
):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "private-state"))
    worker = {
        "worker_id": "wrk-host-start-cleanup-absent",
        "profile": "codex-cli",
        "execution_mode": "host",
    }
    identity = {
        "identity_kind": "host_process",
        "pid": 7421,
        "process_group": 7421,
        "process_start_identity": "ps-lstart:old-host-generation",
        "container_id": "",
        "session_id": "host-old",
    }
    monkeypatch.setattr(runtime, "_recorded_process_is_running", lambda *_args: False)
    monkeypatch.setattr(runtime, "_recorded_pid_is_proven_gone", lambda *_args: True)

    assert runtime.cleanup_unconfirmed_run_start(
        worker,
        "run-host-cleanup-absent",
        identity,
    ) is True


def test_profile_runtime_observes_docker_processes_for_persisted_leases(tmp_path):
    runtime = ProfiledWorkerRuntime(base_dir=str(tmp_path / "data"))
    observer = lambda _payload: None
    start_observer = lambda _payload: None

    runtime.set_host_process_observer(observer)
    runtime.set_run_start_observer(start_observer)

    assert runtime.codex._host_process_observer is observer
    assert runtime.claude._host_process_observer is observer
    assert runtime.openclaw._host_process_observer is observer
    assert runtime.host_codex._host_process_observer is observer
    for child in (
        runtime.openclaw,
        runtime.codex,
        runtime.claude,
        runtime.host_openclaw,
        runtime.host_codex,
        runtime.host_claude,
    ):
        assert child._run_start_observer is start_observer


def test_docker_start_observer_rejection_stops_exact_session_before_continuation(
    tmp_path,
):
    class CaptureRuntime(BaseCliWorkerRuntime):
        runtime_name = "openclaw"
        worker_root_name = "startup_rejection_runtime"

        def resolve_model(self, profile: str) -> str:
            return "capture/model"

        def _build_command(self, worker, instruction, info):
            return ["printf", "must-not-continue"], {}

        def _parse_output(self, worker, stdout, stderr, info):
            raise AssertionError("provider continuation must not run after start rejection")

    runtime = CaptureRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_start_rejected",
        "name": "Rejected startup worker",
        "profile": "openclaw-general",
    }
    run_id = "run_start_rejected"

    class FakeSandbox:
        container_name = "wpr-start-rejected"
        container_id = "container-start-rejected"
        pid = 9001
        state = "running"

    runtime.sandbox.ensure_ready = lambda *args, **kwargs: FakeSandbox()  # type: ignore[method-assign]
    runtime.sandbox.inspect = lambda *_args, **_kwargs: FakeSandbox()  # type: ignore[method-assign]
    runtime.sandbox.list_screen_sessions = lambda *args, **kwargs: []  # type: ignore[method-assign]
    runtime.sandbox._ensure_container_writable_paths = lambda *args, **kwargs: None  # type: ignore[method-assign]
    runtime.sandbox.ensure_container_writable_paths = lambda *args, **kwargs: None  # type: ignore[method-assign]
    runtime.sandbox.screen_session_pid = lambda *args, **kwargs: 4321  # type: ignore[method-assign]
    stopped: list[tuple[str, str, str]] = []
    runtime.sandbox.stop_screen_session = (  # type: ignore[method-assign]
        lambda _worker_id, _runtime_name, session_name, **kwargs: stopped.append(
            ("screen", session_name, str(kwargs.get("expected_container_id") or ""))
        )
    )
    runtime.sandbox.terminate_run_processes = (  # type: ignore[method-assign]
        lambda _worker_id, _runtime_name, exact_run_id, **kwargs: stopped.append(
            ("run", exact_run_id, str(kwargs.get("expected_container_id") or ""))
        )
    )

    def fake_start_screen_session(
        worker_id, runtime_name, session_name, command, *, env=None, worker=None
    ):
        return subprocess.CompletedProcess(command, 0, "", "")

    runtime.sandbox.start_screen_session = fake_start_screen_session  # type: ignore[method-assign]
    observed: list[dict[str, object]] = []

    def reject_start(payload):
        observed.append(dict(payload))
        raise RuntimeErrorBase("durable startup ownership was rejected")

    runtime.set_run_start_observer(reject_start)

    with pytest.raises(RuntimeErrorBase, match="startup ownership was rejected"):
        runtime.run_task(worker, "must not continue", run_id=run_id)

    assert observed == [
        {
            "worker_id": worker["worker_id"],
            "run_id": run_id,
            "identity_kind": "docker_session",
            "pid": 9001,
            "process_group": 9001,
            "process_start_identity": (
                "docker:container-start-rejected:job-run_start_re:"
                "run_start_rejected:4321"
            ),
            "container_id": "container-start-rejected",
            "session_id": "job-run_start_re",
        }
    ]
    assert stopped == [
        ("screen", "job-run_start_re", "container-start-rejected"),
        ("run", run_id, "container-start-rejected"),
    ]
    assert runtime._read_active_session(worker["worker_id"]) is None


def test_docker_start_observer_cleanup_failure_keeps_exact_session_unconfirmed(
    tmp_path,
):
    class CaptureRuntime(BaseCliWorkerRuntime):
        runtime_name = "openclaw"
        worker_root_name = "startup_rejection_failure_runtime"

        def resolve_model(self, profile: str) -> str:
            return "capture/model"

    runtime = CaptureRuntime(base_dir=str(tmp_path / "data"))
    worker_id = "wrk_start_cleanup_ambiguous"
    session = {
        "session_name": "job-run-ambiguous",
        "run_id": "run_cleanup_ambiguous",
        "exit_path": "/synthetic/run/exit_code",
        "process_pid": 4321,
        "lease_pid": 9001,
        "lease_process_start_identity": (
            "docker:container-ambiguous:job-run-ambiguous:"
            "run_cleanup_ambiguous:4321"
        ),
        "container_id": "container-ambiguous",
        "owner_pid": 111,
    }
    runtime._write_active_session(worker_id, session)
    runtime.sandbox.stop_screen_session = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("docker stop was ambiguous")
        )
    )

    with pytest.raises(RunStartupRejectedError) as error:
        runtime._cleanup_rejected_run_start(
            worker_id=worker_id,
            expected_session=runtime._read_active_session(worker_id) or {},
            expected_container_id="container-ambiguous",
            worker={"worker_id": worker_id},
        )

    assert error.value.termination_confirmed is False
    retained = runtime._read_active_session(worker_id) or {}
    assert retained["run_id"] == "run_cleanup_ambiguous"
    assert retained["container_id"] == "container-ambiguous"
    assert retained["termination_unconfirmed"] is True


def test_docker_start_publication_without_observer_cleans_exact_session(
    tmp_path,
):
    class CaptureRuntime(BaseCliWorkerRuntime):
        runtime_name = "openclaw"
        worker_root_name = "startup_missing_observer_runtime"

        def resolve_model(self, profile: str) -> str:
            return "capture/model"

    runtime = CaptureRuntime(base_dir=str(tmp_path / "data"))
    worker_id = "wrk_start_missing_observer"
    run_id = "run_start_missing_observer"
    session_name = "job-run-start-missing"
    container_id = "container-start-missing-observer"
    session = {
        "session_name": session_name,
        "run_id": run_id,
        "exit_path": "/synthetic/run/exit_code",
        "process_pid": 4321,
        "lease_pid": 9001,
        "lease_process_start_identity": (
            f"docker:{container_id}:{session_name}:{run_id}:4321"
        ),
        "container_id": container_id,
        "owner_pid": 111,
    }
    stopped: list[tuple[str, str]] = []
    runtime.sandbox.stop_screen_session = (  # type: ignore[method-assign]
        lambda _worker_id, _runtime_name, exact_session_name, **kwargs: stopped.append(
            (exact_session_name, str(kwargs.get("expected_container_id") or ""))
        )
    )
    runtime.sandbox.terminate_run_processes = (  # type: ignore[method-assign]
        lambda _worker_id, _runtime_name, exact_run_id, **kwargs: stopped.append(
            (exact_run_id, str(kwargs.get("expected_container_id") or ""))
        )
    )

    with pytest.raises(RunStartupRejectedError) as error:
        runtime._write_active_session(
            worker_id,
            session,
            publish_run_start=True,
            worker={"worker_id": worker_id},
        )

    assert error.value.termination_confirmed is True
    assert stopped == [
        (session_name, container_id),
        (run_id, container_id),
    ]
    assert runtime._read_active_session(worker_id) is None


def test_host_start_rejection_targets_captured_popen_not_replacement(
    tmp_path, monkeypatch
):
    class CaptureRuntime(BaseCliWorkerRuntime):
        runtime_name = "codex-cli"
        worker_root_name = "host_startup_rejection_runtime"

        def resolve_model(self, profile: str) -> str:
            return "capture/model"

    class FakeProcess:
        def __init__(self, pid: int) -> None:
            self.pid = pid
            self.returncode = None
            self.wait_calls = 0

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.wait_calls += 1
            self.returncode = -15
            return self.returncode

    runtime = CaptureRuntime(base_dir=str(tmp_path / "data"))
    worker_id = "wrk_host_start_rejected"
    captured = FakeProcess(4101)
    replacement = FakeProcess(4202)
    expected = {
        "session_name": "host-run-captured",
        "run_id": "run_host_captured",
        "exit_path": "/synthetic/captured/exit_code",
        "process_pid": captured.pid,
        "process_group": captured.pid,
        "process_start_identity": "start-captured",
        "owner_pid": 111,
    }
    replacement_session = {
        **expected,
        "session_name": "host-run-replacement",
        "run_id": "run_host_replacement",
        "exit_path": "/synthetic/replacement/exit_code",
        "process_pid": replacement.pid,
        "process_group": replacement.pid,
        "process_start_identity": "start-replacement",
    }
    runtime._write_active_session(worker_id, expected)
    expected = runtime._read_active_session(worker_id) or {}
    runtime._register_process(worker_id, captured)  # type: ignore[arg-type]
    runtime._register_process(worker_id, replacement)  # type: ignore[arg-type]
    runtime._write_active_session(
        worker_id, replacement_session, expected_session=expected
    )
    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(
        runtime,
        "_process_start_identity",
        lambda pid: "start-captured" if pid == captured.pid else "start-replacement",
    )
    monkeypatch.setattr(
        os,
        "killpg",
        lambda group, sig: signals.append((group, sig)),
    )

    with pytest.raises(RunStartupRejectedError) as error:
        runtime._cleanup_rejected_run_start(
            worker_id=worker_id,
            expected_session=expected,
            spawned_process=captured,  # type: ignore[arg-type]
        )

    assert error.value.termination_confirmed is False
    assert captured.wait_calls == 1
    assert replacement.wait_calls == 0
    assert signals == [(captured.pid, signal.SIGTERM)]
    assert runtime._active_processes[worker_id] is replacement
    retained = runtime._read_active_session(worker_id) or {}
    assert retained["run_id"] == "run_host_replacement"
    assert retained["process_pid"] == replacement.pid


def test_host_conversation_ambiguous_start_cleanup_preserves_session_slot_and_process(
    tmp_path, monkeypatch
):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "private-state"))
    life = tmp_path / "Life"
    life.mkdir()
    worker = {
        "worker_id": "wrk_conversation_start_ambiguous",
        "trusted_run_lane": "conversation",
        "name": "Synthetic conversation worker",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(life),
        "model": "synthetic-model",
        "bootstrap_bundle_json": json.dumps({"run_mode": "conversation"}),
    }
    monkeypatch.setattr(
        runtime,
        "ensure_worker_ready",
        lambda current: runtime._host_runtime_info(current),
    )
    monkeypatch.setattr(
        runtime,
        "_build_command",
        lambda *_args, **_kwargs: (
            [sys.executable, "-c", "import time; time.sleep(30)"],
            dict(os.environ),
        ),
    )
    runtime.set_run_start_observer(
        lambda _payload: (_ for _ in ()).throw(
            RuntimeErrorBase("startup confirmation rejected")
        )
    )

    def ambiguous_cleanup(**kwargs):
        runtime._mark_rejected_run_start_unconfirmed(
            kwargs["worker_id"], kwargs["expected_session"]
        )
        raise RunStartupRejectedError(
            "exact host cleanup could not be confirmed",
            termination_confirmed=False,
        )

    monkeypatch.setattr(runtime, "_cleanup_rejected_run_start", ambiguous_cleanup)
    try:
        with pytest.raises(RunStartupRejectedError) as error:
            runtime.run_task(
                worker,
                "Synthetic prompt",
                run_id="run_conversation_start_ambiguous",
            )

        assert error.value.termination_confirmed is False
        retained = runtime._read_active_session(worker["worker_id"]) or {}
        process = runtime._active_processes.get(worker["worker_id"])
        assert retained["run_id"] == "run_conversation_start_ambiguous"
        assert retained["termination_unconfirmed"] is True
        assert process is not None and process.poll() is None
        assert worker["worker_id"] in runtime._host_active_slots()["conversation"]
    finally:
        process = runtime._active_processes.get(worker["worker_id"])
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        runtime._clear_process(worker["worker_id"], expected_process=process)
        runtime._release_host_slot(worker["worker_id"])
        runtime._clear_active_session(worker["worker_id"])


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
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "synthetic-test-access")
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
        "model": "claude-opus-5",
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
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "synthetic-test-access")
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


def test_host_cli_runtime_enforces_configured_mission_slots_per_family(tmp_path, monkeypatch):
    monkeypatch.setenv("WPR_HOST_MISSION_SLOTS_PER_CLI", "1")
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
        with pytest.raises(RuntimeErrorBase, match="mission lane is at capacity"):
            runtime._acquire_host_slot(second)
    finally:
        runtime._release_host_slot(first["worker_id"])

    runtime._acquire_host_slot(second)
    runtime._release_host_slot(second["worker_id"])


def test_host_cli_runtime_reserves_a_separate_interactive_conversation_lane(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WPR_HOST_CONVERSATION_SLOTS_PER_CLI", "1")
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    mission = {
        "worker_id": "wrk_mission_lane",
        "profile": "codex-cli",
        "execution_mode": "host",
        "bootstrap_bundle_json": json.dumps({"run_mode": "mission"}),
    }
    conversation = {
        "worker_id": "wrk_conversation_lane",
        "trusted_run_lane": "conversation",
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
        with pytest.raises(RuntimeErrorBase, match="conversation lane is at capacity"):
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


def test_docker_cli_runtime_clears_active_session_only_after_confirmed_stop(tmp_path):
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

    confirmed = runtime._stop_active_process(
        worker_id, worker={"worker_id": worker_id}
    )

    assert confirmed is True
    assert calls == [("screen", "job-run_stop_meta"), ("terminate", "run_stop_meta")]
    assert not runtime._active_session_meta_path(worker_id).exists()


def test_docker_cli_runtime_clears_exact_session_when_container_is_already_exited(
    tmp_path,
):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    worker_id = "wrk_stop_exited"
    container_id = "a" * 64
    runtime._ensure_dirs(worker_id)
    runtime._write_active_session(
        worker_id,
        {
            "session_name": "job-run_stop_exited",
            "run_id": "run_stop_exited",
            "stdout_path": str(tmp_path / "stdout.log"),
            "stderr_path": str(tmp_path / "stderr.log"),
            "exit_path": str(tmp_path / "exit_code"),
        },
    )
    runtime.sandbox.inspect_fresh = lambda *_args, **_kwargs: SimpleNamespace(  # type: ignore[method-assign]
        status="present",
        sandbox=SimpleNamespace(container_id=container_id, state="exited"),
    )
    runtime.sandbox.stop_screen_session = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("an exited exact container must not receive docker exec")
        )
    )
    runtime.sandbox.terminate_run_processes = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("an exited exact container has no run processes to signal")
        )
    )

    assert runtime._stop_active_process(
        worker_id,
        worker={
            "worker_id": worker_id,
            "_compute_release_container_id": container_id,
        },
        run_id="run_stop_exited",
    ) is True
    assert runtime._read_active_session(worker_id) is None


def test_docker_cli_runtime_preserves_active_session_and_surfaces_stop_failure(
    tmp_path,
):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    worker_id = "wrk_stop_failure"
    runtime._ensure_dirs(worker_id)
    runtime._write_active_session(
        worker_id,
        {
            "session_name": "job-run_stop_failure",
            "run_id": "run_stop_failure",
            "stdout_path": str(tmp_path / "stdout.log"),
            "stderr_path": str(tmp_path / "stderr.log"),
            "exit_path": str(tmp_path / "exit_code"),
        },
    )
    runtime.sandbox.stop_screen_session = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("docker exec refused the stop")
        )
    )
    runtime.sandbox.terminate_run_processes = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: None
    )

    with pytest.raises(RuntimeErrorBase, match="could not be confirmed"):
        runtime._stop_active_process(
            worker_id,
            worker={"worker_id": worker_id},
            run_id="run_stop_failure",
        )

    active = runtime._read_active_session(worker_id)
    assert active is not None
    assert active["run_id"] == "run_stop_failure"
    assert active["termination_unconfirmed"] is True

    runtime.sandbox.stop_screen_session = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    assert runtime._stop_active_process(
        worker_id,
        worker={"worker_id": worker_id},
        run_id="run_stop_failure",
    ) is True
    assert runtime._read_active_session(worker_id) is None


@pytest.mark.parametrize(
    ("probe_error", "expected"),
    [
        (ProcessLookupError(), True),
        (PermissionError(), False),
        (OSError("ambiguous process probe"), False),
    ],
)
def test_recorded_pid_gone_probe_only_accepts_unambiguous_absence(
    tmp_path,
    monkeypatch,
    probe_error,
    expected,
):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))

    def fail_probe(_pid, _signal):
        raise probe_error

    monkeypatch.setattr(os, "kill", fail_probe)

    assert runtime._recorded_pid_is_proven_gone(44001) is expected


def test_terminal_stale_session_bypass_requires_durable_service_context(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    worker_id = "wrk_terminal_context"
    run_id = "run_terminal_context"
    runtime._ensure_dirs(worker_id)
    run_root = runtime._run_root(worker_id, run_id)
    run_root.mkdir(parents=True, exist_ok=True)
    exit_path = run_root / "exit_code"
    exit_path.write_text("0")
    runtime._write_active_session(
        worker_id,
        {
            "session_name": runtime._session_name_for_run_id(run_id),
            "run_id": run_id,
            "exit_path": str(exit_path),
            "process_pid": 44001,
            "owner_pid": 44002,
            "lease_pid": 44003,
        },
    )
    runtime._pid_is_live = lambda _pid: False  # type: ignore[method-assign]
    runtime.sandbox.stop_screen_session = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("exact-run stop could not be confirmed")
        )
    )
    runtime.sandbox.terminate_run_processes = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    terminated: list[str] = []
    runtime.sandbox.terminate = lambda target: terminated.append(target)  # type: ignore[method-assign]

    with pytest.raises(RuntimeErrorBase, match="could not be confirmed"):
        runtime.terminate_worker(
            {
                "worker_id": worker_id,
                "state": "completed",
                "last_run_id": run_id,
            }
        )

    assert terminated == []


def test_terminal_stale_session_compare_delete_preserves_concurrent_new_run(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    worker_id = "wrk_terminal_session_cas"
    stale_run_id = "run_stale_session"
    new_run_id = "run_new_session"
    runtime._ensure_dirs(worker_id)
    stale_root = runtime._run_root(worker_id, stale_run_id)
    stale_root.mkdir(parents=True, exist_ok=True)
    stale_exit = stale_root / "exit_code"
    stale_exit.write_text("0")
    stale_session = {
        "session_name": runtime._session_name_for_run_id(stale_run_id),
        "run_id": stale_run_id,
        "exit_path": str(stale_exit),
        "process_pid": 45001,
        "owner_pid": 45002,
        "lease_pid": 45003,
    }
    runtime._write_active_session(worker_id, stale_session)
    runtime._recorded_pid_is_proven_gone = lambda _pid, _identity="": True  # type: ignore[method-assign]
    original_proof = runtime._stale_terminal_session_is_proven_dead

    def prove_then_replace(*args, **kwargs):
        assert original_proof(*args, **kwargs) is True
        runtime._write_active_session(
            worker_id,
            {
                "session_name": runtime._session_name_for_run_id(new_run_id),
                "run_id": new_run_id,
                "exit_path": str(runtime._run_root(worker_id, new_run_id) / "exit_code"),
                "process_pid": 45101,
                "owner_pid": 45102,
                "lease_pid": 45103,
            },
        )
        return True

    runtime._stale_terminal_session_is_proven_dead = prove_then_replace  # type: ignore[method-assign]
    terminated: list[str] = []
    runtime.sandbox.terminate = lambda target: terminated.append(target)  # type: ignore[method-assign]

    with pytest.raises(RuntimeErrorBase, match="ownership changed"):
        runtime.terminate_worker(
            {"worker_id": worker_id, "_terminal_run_id": stale_run_id}
        )

    assert terminated == []
    assert (runtime._read_active_session(worker_id) or {})["run_id"] == new_run_id


def test_exact_stop_compare_delete_preserves_replacement_session_and_process(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    worker_id = "wrk_exact_stop_session_cas"
    old_run_id = "run_exact_stop_old"
    new_run_id = "run_exact_stop_new"
    runtime._ensure_dirs(worker_id)
    runtime._write_active_session(
        worker_id,
        {
            "session_name": runtime._session_name_for_run_id(old_run_id),
            "run_id": old_run_id,
            "process_pid": 46001,
            "owner_pid": 46002,
            "lease_pid": 46003,
        },
    )

    class ReplacementProcess:
        pid = 46101

        def __init__(self) -> None:
            self.terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            raise AssertionError("replacement process must not be waited")

    replacement = ReplacementProcess()

    def replace_ownership(*_args, **_kwargs):
        runtime._write_active_session(
            worker_id,
            {
                "session_name": runtime._session_name_for_run_id(new_run_id),
                "run_id": new_run_id,
                "process_pid": replacement.pid,
                "owner_pid": 46102,
                "lease_pid": 46103,
            },
        )
        runtime._register_process(worker_id, replacement)  # type: ignore[arg-type]

    runtime.sandbox.stop_screen_session = replace_ownership  # type: ignore[method-assign]
    runtime.sandbox.terminate_run_processes = lambda *_args, **_kwargs: None  # type: ignore[method-assign]

    with pytest.raises(RuntimeErrorBase, match="ownership changed"):
        runtime.interrupt_worker(
            {"worker_id": worker_id},
            run_id=old_run_id,
        )

    assert replacement.terminated is False
    assert (runtime._read_active_session(worker_id) or {})["run_id"] == new_run_id


def test_exact_stop_failure_marker_does_not_overwrite_replacement_session(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    worker_id = "wrk_exact_stop_failure_cas"
    old_run_id = "run_exact_stop_failure_old"
    new_run_id = "run_exact_stop_failure_new"
    runtime._ensure_dirs(worker_id)
    runtime._write_active_session(
        worker_id,
        {
            "session_name": runtime._session_name_for_run_id(old_run_id),
            "run_id": old_run_id,
            "process_pid": 47001,
            "owner_pid": 47002,
            "lease_pid": 47003,
        },
    )

    def replace_then_fail(*_args, **_kwargs):
        runtime._write_active_session(
            worker_id,
            {
                "session_name": runtime._session_name_for_run_id(new_run_id),
                "run_id": new_run_id,
                "process_pid": 47101,
                "owner_pid": 47102,
                "lease_pid": 47103,
            },
        )
        raise RuntimeError("old exact stop failed")

    runtime.sandbox.stop_screen_session = replace_then_fail  # type: ignore[method-assign]
    runtime.sandbox.terminate_run_processes = lambda *_args, **_kwargs: None  # type: ignore[method-assign]

    with pytest.raises(RuntimeErrorBase, match="ownership changed"):
        runtime.interrupt_worker(
            {"worker_id": worker_id},
            run_id=old_run_id,
        )

    replacement = runtime._read_active_session(worker_id) or {}
    assert replacement["run_id"] == new_run_id
    assert replacement["termination_unconfirmed"] is False


def test_host_exact_stop_preserves_replacement_session_and_process(
    tmp_path, monkeypatch
):
    _patch_host_codex_requirement_probe(monkeypatch)
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    worker_id = "wrk_host_stop_session_cas"
    old_run_id = "run_host_stop_old"
    new_run_id = "run_host_stop_new"
    runtime._ensure_dirs(worker_id)

    class Process:
        def __init__(self, pid: int) -> None:
            self.pid = pid
            self.done = False

        def poll(self):
            return 0 if self.done else None

        def wait(self, timeout=None):
            self.done = True
            return 0

    old_process = Process(50001)
    replacement_process = Process(50101)
    old_session = {
        "session_name": f"host-{old_run_id[:12]}",
        "run_id": old_run_id,
        "process_pid": old_process.pid,
        "process_group": old_process.pid,
        "process_start_identity": "ps-lstart:old-process",
        "owner_pid": 50002,
    }
    runtime._write_active_session(worker_id, old_session)
    runtime._register_process(worker_id, old_process)  # type: ignore[arg-type]
    replaced = False

    def replace_on_old_signal(_group, _signal):
        nonlocal replaced
        if replaced:
            return
        replaced = True
        runtime._write_active_session(
            worker_id,
            {
                "session_name": f"host-{new_run_id[:12]}",
                "run_id": new_run_id,
                "process_pid": replacement_process.pid,
                "process_group": replacement_process.pid,
                "process_start_identity": "ps-lstart:new-process",
                "owner_pid": 50102,
            },
        )
        runtime._register_process(worker_id, replacement_process)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "killpg", replace_on_old_signal)
    monkeypatch.setattr(os, "getpgrp", lambda: 99999)

    with pytest.raises(RuntimeErrorBase, match="ownership changed"):
        runtime.interrupt_worker(
            {
                "worker_id": worker_id,
                "_host_run_lease": {
                    "worker_id": worker_id,
                    "run_id": old_run_id,
                    "status": "active",
                    "startup_state": "confirmed",
                    "startup_identity_kind": "host_process",
                    "pid": old_process.pid,
                    "process_group": old_process.pid,
                    "process_start_identity": "ps-lstart:old-process",
                    "startup_session_id": old_session["session_name"],
                },
            },
            run_id=old_run_id,
        )

    assert replaced is True
    assert replacement_process.done is False
    assert (runtime._read_active_session(worker_id) or {})["run_id"] == new_run_id
    with runtime._process_lock:
        assert runtime._active_processes[worker_id] is replacement_process


def test_openclaw_terminal_release_uses_proven_stale_session_parity(tmp_path):
    runtime = OpenClawWorkstationRuntime(base_dir=str(tmp_path / "data"))
    worker_id = "wrk_openclaw_terminal_release"
    run_id = "run_openclaw_terminal_release"
    runtime._ensure_dirs(worker_id)
    run_root = runtime._run_root(worker_id, run_id)
    run_root.mkdir(parents=True, exist_ok=True)
    exit_path = run_root / "exit_code"
    exit_path.write_text("0")
    runtime._write_active_session(
        worker_id,
        {
            "session_name": runtime._session_name_for_run_id(run_id),
            "run_id": run_id,
            "exit_path": str(exit_path),
            "process_pid": 48001,
            "owner_pid": 48002,
            "lease_pid": 48003,
        },
    )
    runtime._recorded_pid_is_proven_gone = lambda _pid, _identity="": True  # type: ignore[method-assign]
    runtime.sandbox.stop_screen_session = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("proven terminal session must be cleared before exact stop")
        )
    )
    terminated: list[tuple[str, str]] = []
    runtime.sandbox.terminate = (  # type: ignore[method-assign]
        lambda target, *, expected_container_id=None: terminated.append(
            (target, str(expected_container_id or ""))
        )
    )

    runtime.terminate_worker(
        {
            "worker_id": worker_id,
            "_terminal_run_id": run_id,
            "_compute_release_container_id": "container-openclaw-generation",
        }
    )

    assert runtime._read_active_session(worker_id) is None
    assert terminated == [(worker_id, "container-openclaw-generation")]


def test_terminal_release_passes_exact_claim_into_nonmutating_session_probe(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_exact_release_probe",
        "_terminal_run_id": "run_terminal",
        "_compute_release_container_id": "a" * 64,
    }
    probes: list[dict | None] = []
    runtime._stop_active_process = (  # type: ignore[method-assign]
        lambda _worker_id, **kwargs: probes.append(kwargs.get("worker")) or True
    )
    runtime.sandbox.terminate = lambda *_args, **_kwargs: None  # type: ignore[method-assign]

    runtime.terminate_worker(worker)

    assert probes == [worker]


@pytest.mark.parametrize("runtime_cls", [CodexCliRuntime, OpenClawWorkstationRuntime])
def test_terminal_release_with_confirmed_absent_generation_avoids_name_based_control(
    tmp_path, runtime_cls
):
    runtime = runtime_cls(base_dir=str(tmp_path / "data"))
    worker_id = "wrk_exact_absent_release"
    run_id = "run_exact_absent_release"
    runtime._ensure_dirs(worker_id)
    run_root = runtime._run_root(worker_id, run_id)
    run_root.mkdir(parents=True, exist_ok=True)
    (run_root / "exit_code").write_text("0")
    runtime.sandbox.list_screen_sessions = (  # type: ignore[method-assign]
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("confirmed absence must not probe a mutable container name")
        )
    )
    runtime.sandbox.stop_screen_session = runtime.sandbox.list_screen_sessions  # type: ignore[method-assign]
    runtime.sandbox.terminate_run_processes = runtime.sandbox.list_screen_sessions  # type: ignore[method-assign]
    events: list[tuple[str, bool]] = []
    runtime.sandbox.terminate = (  # type: ignore[method-assign]
        lambda _worker_id, *, expected_absent=False, **_kwargs: events.append(
            ("sandbox_absence", bool(expected_absent))
        )
    )

    runtime.terminate_worker(
        {
            "worker_id": worker_id,
            "state": "completed",
            "_terminal_run_id": run_id,
            "_compute_release_container_id": "",
            "bootstrap_profile": "clean-room",
            "bootstrap_bundle_json": json.dumps(
                {"execution_policy": PARALLEL_CLEAN_ROOM_EXECUTION_POLICY}
            ),
        }
    )

    assert events == [("sandbox_absence", True)]


@pytest.mark.parametrize("runtime_cls", [CodexCliRuntime, OpenClawWorkstationRuntime])
def test_parallel_clean_room_termination_carries_policy_to_sandbox_cleanup(
    tmp_path, runtime_cls
):
    runtime = runtime_cls(base_dir=str(tmp_path / "data"))
    runtime._stop_active_process = lambda *_args, **_kwargs: True  # type: ignore[method-assign]
    calls: list[tuple[str, str]] = []
    runtime.sandbox.terminate = (  # type: ignore[method-assign]
        lambda target, *, expected_container_id=None, execution_policy="": calls.append(
            (target, str(execution_policy or ""))
        )
    )

    runtime.terminate_worker(
        {
            "worker_id": "wrk_clean_terminate",
            "bootstrap_profile": "clean-room",
            "bootstrap_bundle_json": json.dumps(
                {"execution_policy": PARALLEL_CLEAN_ROOM_EXECUTION_POLICY}
            ),
        }
    )

    assert calls == [
        ("wrk_clean_terminate", PARALLEL_CLEAN_ROOM_EXECUTION_POLICY)
    ]


def test_terminal_stale_session_accepts_live_matching_container_init_pid(tmp_path):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    worker_id = "wrk_terminal_retained_container"
    run_id = "run_terminal_retained_container"
    runtime._ensure_dirs(worker_id)
    run_root = runtime._run_root(worker_id, run_id)
    run_root.mkdir(parents=True, exist_ok=True)
    exit_path = run_root / "exit_code"
    exit_path.write_text("0")
    container_id = "container-retained-generation"
    session_name = runtime._session_name_for_run_id(run_id)
    active_session = {
        "session_name": session_name,
        "run_id": run_id,
        "exit_path": str(exit_path),
        "process_pid": 49001,
        "owner_pid": 49002,
        "lease_pid": 49003,
        "lease_process_start_identity": (
            f"docker:{container_id}:{session_name}:{run_id}:49001"
        ),
    }
    runtime._write_active_session(worker_id, active_session)
    runtime._recorded_pid_is_proven_gone = (  # type: ignore[method-assign]
        lambda pid, _identity="": pid in {49001, 49002}
    )
    runtime.sandbox.inspect = lambda _worker_id: SimpleNamespace(  # type: ignore[method-assign]
        state="running", container_id=container_id, pid=49003
    )
    runtime.sandbox.screen_session_pid = lambda *_args, **_kwargs: None  # type: ignore[method-assign]

    assert runtime._stale_terminal_session_is_proven_dead(
        worker_id,
        active_session,
        expected_run_id=run_id,
    ) is True


def test_docker_cli_runtime_uses_configured_run_timeout(tmp_path, monkeypatch):
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))

    monkeypatch.setenv("GLASSHIVE_RUN_TIMEOUT_SEC", "1200")

    assert runtime._run_timeout_sec() == 1200


def test_parallel_clean_room_container_env_rejects_ambient_provider_authority(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-ambient-openai-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-ambient-anthropic-secret")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "synthetic-ambient-oauth-secret")
    monkeypatch.setenv("PORTKEY_API_KEY", "synthetic-ambient-portkey-secret")
    monkeypatch.setenv("HTTP_PROXY", "http://ambient-proxy.example:8888")
    monkeypatch.setenv("HTTPS_PROXY", "http://ambient-proxy.example:8888")
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_NETWORK", "glasshive-parallel-clean-room"
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL",
        "http://provider-egress:8080",
    )
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_clean_room_env",
        "bootstrap_profile": "clean-room",
        "bootstrap_bundle_json": json.dumps(
            {
                "execution_policy": PARALLEL_CLEAN_ROOM_EXECUTION_POLICY,
                "env": {
                    "GLASSHIVE_CAPABILITY_BROKER_TOKEN": "synthetic-run-grant"
                },
            }
        ),
    }

    env = runtime._container_env_for_worker(
        worker,
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "PORTKEY_API_KEY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
    )

    assert env["HTTP_PROXY"] == "http://provider-egress:8080"
    assert env["HTTPS_PROXY"] == "http://provider-egress:8080"
    assert env["NO_PROXY"] == (
        "provider-egress,host.docker.internal,localhost,127.0.0.1"
    )
    assert "OPENAI_API_KEY" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert "PORTKEY_API_KEY" not in env
    assert "GLASSHIVE_CAPABILITY_BROKER_TOKEN" not in env


def test_parallel_clean_room_codex_uses_run_grant_for_the_attested_provider_route(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_NETWORK", "glasshive-parallel-clean-room"
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL",
        "http://provider-egress:8080",
    )
    for name in (
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_API_BASE",
        "OPENAI_REVERSE_PROXY",
        "PORTKEY_API_KEY",
        "PORTKEY_BASE_URL",
        "WPR_CODEX_CLI_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_clean_room_codex_provider",
        "profile": "codex-cli",
        "bootstrap_profile": "clean-room",
        "bootstrap_bundle_json": json.dumps(
            {
                "execution_policy": PARALLEL_CLEAN_ROOM_EXECUTION_POLICY,
                "env": {
                    "GLASSHIVE_CAPABILITY_BROKER_TOKEN": "synthetic-run-grant"
                },
            }
        ),
    }
    info = SimpleNamespace(
        runtime="codex-cli",
        model="synthetic-model",
        workspace_dir=str(tmp_path / "workspace"),
        home_dir=str(tmp_path / "home"),
        session_key=None,
    )

    command, env = runtime._build_command(worker, "Do it.", info)

    assert (
        'model_providers.glasshive_openai_compatible.base_url="http://provider-egress:8080/openai/v1"'
        in command
    )
    assert (
        'model_providers.glasshive_openai_compatible.env_key="GLASSHIVE_CAPABILITY_BROKER_TOKEN"'
        in command
    )
    assert "synthetic-run-grant" not in command
    assert "synthetic-run-grant" not in env.values()
    assert "OPENAI_API_KEY" not in env


def test_parallel_clean_room_run_rejects_replaced_generation_before_authority_projection(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_NETWORK", "glasshive-parallel-clean-room"
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL",
        "http://provider-egress:8080",
    )
    runtime = CodexCliRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_replaced_after_grant",
        "profile": "codex-cli",
        "execution_mode": "docker",
        "bootstrap_profile": "clean-room",
        "bootstrap_bundle_json": json.dumps(
            {
                "execution_policy": PARALLEL_CLEAN_ROOM_EXECUTION_POLICY,
                "env": {
                    "GLASSHIVE_CAPABILITY_BROKER_TOKEN": "synthetic-run-grant"
                },
            }
        ),
        "_run_local_capability_binding": {
            "containerGenerationId": "a" * 64,
        },
    }

    class ReplacementSandbox:
        container_name = "wpr-replaced-after-grant"
        container_id = "b" * 64
        pid = 123
        state = "running"

    runtime.sandbox.ensure_ready = lambda *_args, **_kwargs: ReplacementSandbox()  # type: ignore[method-assign]
    runtime.sandbox.inspect_fresh = lambda *_args, **_kwargs: SimpleNamespace(  # type: ignore[method-assign]
        status="present", sandbox=ReplacementSandbox()
    )

    with pytest.raises(
        RuntimeErrorBase,
        match="capability grant does not match the exact sandbox generation",
    ):
        runtime.run_task(worker, "Do it.", run_id="run-replaced-after-grant")


@pytest.mark.parametrize("runtime_type", [CodexCliRuntime, OpenClawWorkstationRuntime])
def test_parallel_clean_room_ready_check_never_uses_cached_fast_sandbox(
    tmp_path, runtime_type
):
    runtime = runtime_type(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_clean_room_fresh_boundary",
        "state": "running",
        "profile": "codex-cli",
        "container_id": "cached-container-generation",
        "bootstrap_bundle_json": json.dumps(
            {"execution_policy": PARALLEL_CLEAN_ROOM_EXECUTION_POLICY}
        ),
    }
    cached = SimpleNamespace(pid=9911, state="running")
    runtime.sandbox.fast_sandbox_from_worker = lambda _worker: cached  # type: ignore[method-assign]
    runtime.sandbox.ensure_ready = lambda *_args, **_kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        RuntimeError("strict clean-room boundary unavailable")
    )
    if isinstance(runtime, OpenClawWorkstationRuntime):
        runtime._write_gateway_config = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
        runtime._start_openclaw_gateway = lambda *_args, **_kwargs: None  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="strict clean-room boundary unavailable"):
        runtime.ensure_worker_ready(worker)


def test_parallel_clean_room_run_exits_after_secret_scrub_without_takeover_shell(
    tmp_path, monkeypatch
):
    class CaptureRuntime(BaseCliWorkerRuntime):
        runtime_name = "codex-cli"
        worker_root_name = "parallel_clean_room_capture"

        def resolve_model(self, profile: str) -> str:
            return "capture/model"

        def _build_command(self, worker, instruction, info):
            return ["printf", "ok"], self._container_env_for_worker(
                worker, "OPENAI_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN"
            )

        def _parse_output(self, worker, stdout, stderr, info):
            return None, stdout.strip()

    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-ambient-provider-secret")
    monkeypatch.setenv(
        "CLAUDE_CODE_OAUTH_TOKEN", "synthetic-ambient-subscription-secret"
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_NETWORK", "glasshive-parallel-clean-room"
    )
    monkeypatch.setenv(
        "WPR_PARALLEL_CLEAN_ROOM_PROVIDER_PROXY_URL",
        "http://provider-egress:8080",
    )
    runtime = CaptureRuntime(base_dir=str(tmp_path / "data"))
    runtime.set_run_start_observer(lambda _payload: None)
    run_id = "run_clean_room_exit"
    worker = {
        "worker_id": "wrk_clean_room_exit",
        "name": "Clean Room Worker",
        "profile": "codex-cli",
        "execution_mode": "docker",
        "bootstrap_profile": "clean-room",
        "bootstrap_bundle_json": json.dumps(
            {
                "execution_policy": PARALLEL_CLEAN_ROOM_EXECUTION_POLICY,
                "env": {
                    "GLASSHIVE_CAPABILITY_BROKER_TOKEN": "synthetic-run-grant"
                },
            }
        ),
        "_run_local_capability_binding": {
            "containerGenerationId": "d" * 64,
        },
    }

    class FakeSandbox:
        container_name = "wpr-clean-room-exit"
        container_id = "d" * 64
        pid = 123
        state = "running"

    runtime.sandbox.ensure_ready = lambda *_args, **_kwargs: FakeSandbox()  # type: ignore[method-assign]
    runtime.sandbox.inspect = lambda *_args, **_kwargs: FakeSandbox()  # type: ignore[method-assign]
    runtime.sandbox.inspect_fresh = lambda *_args, **_kwargs: SimpleNamespace(  # type: ignore[method-assign]
        status="present", sandbox=FakeSandbox()
    )
    runtime.sandbox.list_screen_sessions = lambda *_args, **_kwargs: []  # type: ignore[method-assign]
    runtime.sandbox._ensure_container_writable_paths = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    runtime.sandbox.ensure_container_writable_paths = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    projected: list[dict] = []
    cleared: list[dict] = []

    def project_run_secrets(worker_id, **kwargs):
        projected.append({"worker_id": worker_id, **kwargs})
        return {
            "env_file": f"/run/glasshive/{run_id}/secret-runtime.env",
            "keys_file": f"/run/glasshive/{run_id}/secret-runtime.keys",
        }

    runtime.sandbox.project_parallel_clean_room_run_secrets = project_run_secrets  # type: ignore[method-assign]
    runtime.sandbox.clear_parallel_clean_room_run_secrets = (  # type: ignore[method-assign]
        lambda worker_id, **kwargs: cleared.append(
            {"worker_id": worker_id, **kwargs}
        )
    )

    def fake_start_screen_session(
        worker_id, runtime_name, session_name, command, *, env=None, worker=None
    ):
        script = (runtime._run_root(worker_id, run_id) / "run.sh").read_text()
        assert "exec bash --noprofile --norc" not in script
        assert "Interactive shell remains open for takeover" not in script
        assert "credential-free session exiting" in script
        assert 'exit "$status"' in script
        assert (
            'export OPENAI_API_KEY="$GLASSHIVE_CAPABILITY_BROKER_TOKEN"'
            in script
        )
        assert (
            'export ANTHROPIC_AUTH_TOKEN="$GLASSHIVE_CAPABILITY_BROKER_TOKEN"'
            in script
        )
        assert "synthetic-run-grant" not in script
        assert f"/run/glasshive/{run_id}/secret-runtime.env" in script
        assert '$HOME/.glasshive/secret-runtime.env' not in script
        assert "scrub_run_secrets()" in script
        assert 'abort_run() { scrub_run_secrets; write_exit "${1:-130}"' in script
        assert env["HTTP_PROXY"] == "http://provider-egress:8080"
        assert "OPENAI_API_KEY" not in env
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
        run_root = runtime._run_root(worker_id, run_id)
        (run_root / "stdout.log").write_text("FINAL REPORT:\nok")
        (run_root / "stderr.log").write_text("")
        (run_root / "exit_code").write_text("0")
        return subprocess.CompletedProcess(
            ["screen"], returncode=0, stdout="", stderr=""
        )

    runtime.sandbox.start_screen_session = fake_start_screen_session  # type: ignore[method-assign]
    runtime.sandbox.screen_session_pid = lambda *_args, **_kwargs: 4321  # type: ignore[method-assign]

    assert runtime.run_task(worker, "Do it.", run_id=run_id) == "FINAL REPORT:\nok"
    assert projected == [
        {
            "worker_id": "wrk_clean_room_exit",
            "expected_container_id": "d" * 64,
            "run_id": run_id,
            "env": {
                "GLASSHIVE_CAPABILITY_BROKER_TOKEN": "synthetic-run-grant"
            },
        }
    ]
    assert cleared == [
        {
            "worker_id": "wrk_clean_room_exit",
            "expected_container_id": "d" * 64,
            "run_id": run_id,
        }
    ]


def test_profiled_runtime_prepares_authority_from_fresh_exact_clean_room_generation(
    tmp_path,
):
    runtime = ProfiledWorkerRuntime(base_dir=str(tmp_path / "data"))
    sandbox = SimpleNamespace(container_id="a" * 64, state="running")
    calls: list[str] = []
    fake_sandbox = SimpleNamespace(
        inspect_fresh=lambda worker_id: (
            calls.append(f"inspect:{worker_id}")
            or SimpleNamespace(status="present", sandbox=sandbox)
        ),
        _sandbox_matches_parallel_clean_room_policy=lambda candidate: candidate is sandbox,
    )
    fake_runtime = SimpleNamespace(
        sandbox=fake_sandbox,
        ensure_worker_ready=lambda worker: calls.append(
            f"ensure:{worker['worker_id']}"
        ),
    )
    runtime._runtime_for_worker = lambda _worker: fake_runtime  # type: ignore[method-assign]
    worker = {
        "worker_id": "wrk_generation_authority",
        "execution_mode": "docker",
        "profile": "codex-cli",
        "bootstrap_bundle_json": json.dumps(
            {"execution_policy": PARALLEL_CLEAN_ROOM_EXECUTION_POLICY}
        ),
    }

    assert runtime.prepare_run_authority_context(worker, run_id="run-exact") == {
        "container_generation_id": "a" * 64
    }
    assert calls == [
        "ensure:wrk_generation_authority",
        "inspect:wrk_generation_authority",
    ]


@pytest.mark.parametrize(
    ("status", "state", "container_id", "matches"),
    [
        ("unavailable", "running", "a" * 64, True),
        ("present", "exited", "a" * 64, True),
        ("present", "running", "not-an-exact-generation", True),
        ("present", "running", "a" * 64, False),
    ],
)
def test_profiled_runtime_refuses_unproven_generation_before_broker_admission(
    tmp_path, status, state, container_id, matches
):
    runtime = ProfiledWorkerRuntime(base_dir=str(tmp_path / "data"))
    sandbox = SimpleNamespace(container_id=container_id, state=state)
    fake_runtime = SimpleNamespace(
        sandbox=SimpleNamespace(
            inspect_fresh=lambda _worker_id: SimpleNamespace(
                status=status, sandbox=sandbox
            ),
            _sandbox_matches_parallel_clean_room_policy=lambda _candidate: matches,
        ),
        ensure_worker_ready=lambda _worker: None,
    )
    runtime._runtime_for_worker = lambda _worker: fake_runtime  # type: ignore[method-assign]
    worker = {
        "worker_id": "wrk_unproven_generation",
        "execution_mode": "docker",
        "profile": "codex-cli",
        "bootstrap_bundle_json": json.dumps(
            {"execution_policy": PARALLEL_CLEAN_ROOM_EXECUTION_POLICY}
        ),
    }

    with pytest.raises(RuntimeErrorBase, match="exact mission container generation"):
        runtime.prepare_run_authority_context(worker, run_id="run-unproven")


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
    runtime.set_run_start_observer(lambda _payload: None)
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
    runtime.set_run_start_observer(lambda _payload: None)
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
    runtime.set_run_start_observer(lambda _payload: None)
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
    runtime.set_run_start_observer(lambda _payload: None)
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
    runtime.set_run_start_observer(lambda _payload: None)
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
    runtime.set_run_start_observer(lambda _payload: None)
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


def test_host_codex_native_web_access_policy_disables_unbrokered_search_on_native_route(
    tmp_path, monkeypatch
):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_native_web_locked",
        "name": "Locked Native Web Worker",
        "profile": "codex-cli",
        "model": "gpt-5.6-sol",
    }
    runtime._ensure_dirs(worker["worker_id"])
    monkeypatch.setenv("WPR_HOST_NATIVE_WEB_ACCESS", "disabled")
    monkeypatch.setenv("GLASSHIVE_HOST_NATIVE_WEB_ACCESS", "inherit")

    command, _ = runtime._build_command(
        worker,
        "Use the assigned brokered research capability.",
        runtime._runtime_info(worker),
    )

    assert 'web_search="disabled"' in command
    assert "--dangerously-bypass-approvals-and-sandbox" not in command
    assert "danger-full-access" not in command
    assert 'sandbox_mode="workspace-write"' in command
    assert 'approval_policy="never"' in command
    assert "sandbox_workspace_write.network_access=false" in command
    joined = "\n".join(command)
    for native_escape in (
        "apps",
        "browser_use",
        "browser_use_external",
        "browser_use_full_cdp_access",
        "computer_use",
        "in_app_browser",
        "plugins",
        "remote_plugin",
    ):
        assert f"--disable\n{native_escape}" in joined
    assert "--disable\nweb_search" not in "\n".join(command)


def test_host_codex_native_web_lockdown_keeps_only_declared_broker_mcp(
    tmp_path, monkeypatch
):
    source_home = tmp_path / "source-codex-home"
    source_home.mkdir()
    (source_home / "config.toml").write_text(
        'notify = ["synthetic-notifier"]\n\n'
        "[apps.synthetic]\nenabled = true\n\n"
        "[mcp_servers.node_repl]\ncommand = \"node-repl\"\n\n"
        "[plugins.\"browser@openai-bundled\"]\nenabled = true\n"
    )
    monkeypatch.setenv("CODEX_HOME", str(source_home))
    monkeypatch.setenv("WPR_HOST_NATIVE_WEB_ACCESS", "disabled")
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))

    config = runtime._host_codex_worker_config(
        "[mcp_servers.glasshive-user-capabilities]\n"
        'url = "http://127.0.0.1:8180/api/viventium/glasshive/capabilities/mcp"'
    )

    assert "mcp_servers.glasshive-user-capabilities" in config
    assert "mcp_servers.node_repl" not in config
    assert "synthetic-notifier" not in config
    assert "apps.synthetic" not in config
    assert "plugins" not in config


def test_host_codex_native_web_lockdown_launches_declared_loopback_broker_transport(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WPR_HOST_NATIVE_WEB_ACCESS", "disabled")
    life = tmp_path / "Life"
    life.mkdir()
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "private-state"))
    worker = {
        "worker_id": "wrk_locked_loopback_broker",
        "trusted_run_lane": "conversation",
        "name": "Locked Loopback Broker Worker",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(life),
        "model": "gpt-5.6-sol",
        "bootstrap_bundle_json": json.dumps(
            {
                "run_mode": "conversation",
                "provider_model": "gpt-5.6-sol",
                "access_mode": "full",
                "codex_config_append": (
                    "[mcp_servers.glasshive-user-capabilities]\n"
                    'url = "http://127.0.0.1:18180/api/capabilities/mcp"'
                ),
            }
        ),
    }
    workspace = runtime._host_workspace_dir(worker)
    runtime._materialize_workspace(worker, workspace)

    command, env = runtime._build_command(
        worker,
        "Use the declared read-only broker capability.",
        runtime._host_runtime_info(worker),
    )

    config_path = runtime._host_codex_home(worker) / "config.toml"
    config = config_path.read_text()
    assert "mcp_servers.glasshive-user-capabilities" in config
    assert "http://127.0.0.1:18180/api/capabilities/mcp" in config
    assert env["CODEX_HOME"] == str(config_path.parent)
    assert 'approval_policy="never"' in command
    assert 'sandbox_mode="workspace-write"' in command
    assert "sandbox_workspace_write.network_access=false" in command
    assert "--dangerously-bypass-approvals-and-sandbox" not in command
    assert "danger-full-access" not in command


def test_host_native_web_access_uses_standalone_alias_only_without_compiled_policy(
    tmp_path, monkeypatch
):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_native_web_alias_fallback",
        "name": "Standalone Locked Native Web Worker",
        "profile": "codex-cli",
        "model": "gpt-5.6-sol",
    }
    runtime._ensure_dirs(worker["worker_id"])
    monkeypatch.delenv("WPR_HOST_NATIVE_WEB_ACCESS", raising=False)
    monkeypatch.setenv("GLASSHIVE_HOST_NATIVE_WEB_ACCESS", "disabled")

    command, _ = runtime._build_command(
        worker,
        "Use the assigned brokered research capability.",
        runtime._runtime_info(worker),
    )

    assert 'web_search="disabled"' in command


@pytest.mark.parametrize(
    ("compiled", "standalone", "expected"),
    [
        ("disabled", "inherit", "disabled"),
        ("inherit", "disabled", "inherit"),
        (None, "disabled", "disabled"),
    ],
)
def test_host_native_web_access_resolver_honors_compiled_precedence(
    compiled, standalone, expected, monkeypatch
):
    if compiled is None:
        monkeypatch.delenv("WPR_HOST_NATIVE_WEB_ACCESS", raising=False)
    else:
        monkeypatch.setenv("WPR_HOST_NATIVE_WEB_ACCESS", compiled)
    monkeypatch.setenv("GLASSHIVE_HOST_NATIVE_WEB_ACCESS", standalone)

    assert _host_native_web_access() == expected


def test_host_native_web_access_compiled_inherit_overrides_standalone_alias(
    tmp_path, monkeypatch
):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_native_web_compiled_inherit",
        "name": "Compiled Full Native Web Worker",
        "profile": "codex-cli",
        "model": "gpt-5.6-sol",
    }
    runtime._ensure_dirs(worker["worker_id"])
    monkeypatch.setenv("WPR_HOST_NATIVE_WEB_ACCESS", "inherit")
    monkeypatch.setenv("GLASSHIVE_HOST_NATIVE_WEB_ACCESS", "disabled")

    command, _ = runtime._build_command(
        worker,
        "Research with the best available capability.",
        runtime._runtime_info(worker),
    )

    assert 'web_search="disabled"' not in command


def test_host_codex_native_web_access_defaults_to_inherit(tmp_path, monkeypatch):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_native_web_inherited",
        "name": "Full Native Worker",
        "profile": "codex-cli",
        "model": "gpt-5.6-sol",
    }
    runtime._ensure_dirs(worker["worker_id"])
    monkeypatch.delenv("GLASSHIVE_HOST_NATIVE_WEB_ACCESS", raising=False)
    monkeypatch.delenv("WPR_HOST_NATIVE_WEB_ACCESS", raising=False)

    command, _ = runtime._build_command(
        worker,
        "Research with the best available capability.",
        runtime._runtime_info(worker),
    )

    assert 'web_search="disabled"' not in command


def test_host_claude_native_web_access_policy_disables_web_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "synthetic-test-access")
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "data"))
    worker = {
        "worker_id": "wrk_claude_native_web_locked",
        "name": "Locked Native Web Worker",
        "profile": "claude-code",
        "model": "opus",
    }
    runtime._ensure_dirs(worker["worker_id"])
    monkeypatch.setenv("WPR_HOST_NATIVE_WEB_ACCESS", "disabled")
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", "1")

    command, _ = runtime._build_command(
        worker,
        "Use the assigned brokered research capability.",
        runtime._runtime_info(worker),
    )

    assert "--disallowedTools" in command
    denied_index = command.index("--disallowedTools")
    assert command[denied_index + 1 : denied_index + 3] == ["WebSearch", "WebFetch"]
    assert "--chrome" not in command
    assert "--no-chrome" in command
    sources_index = command.index("--setting-sources")
    assert command[sources_index + 1] == ""
    settings_index = command.index("--settings")
    settings = json.loads(command[settings_index + 1])
    assert settings["sandbox"] == {
        "enabled": True,
        "failIfUnavailable": True,
        "allowUnsandboxedCommands": False,
        "network": {
            "allowedDomains": [],
            "strictAllowlist": True,
        },
    }


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
    monkeypatch.setenv(
        "VIVENTIUM_GLASSHIVE_SERVICE_ASSERTION_SECRET",
        "service-assertion-secret",
    )
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
    assert "VIVENTIUM_GLASSHIVE_SERVICE_ASSERTION_SECRET" not in env
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


def test_host_runtime_preflight_rejects_codex_too_old_for_default_automation_model(
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
        "echo 'codex-cli 0.144.1'\n"
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
        "if [[ \"$1\" == \"--version\" ]]; then echo 'codex-cli 0.144.1'; exit 0; fi\n"
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
                "api_error_status": 401,
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


def test_runtime_error_does_not_infer_authentication_from_provider_prose():
    failure = classify_runtime_error(
        RuntimeErrorBase('claude-code exited with code 1: {"result":"Not logged in · Please run /login"}'),
        runtime_name="claude-code",
    )

    assert failure.failure_class == "runtime_error"


def test_runtime_error_preserves_structured_provider_authentication_class():
    error = RuntimeErrorBase("claude-code exited with a structured provider failure")
    error.failure_class = "provider_auth_missing"

    failure = classify_runtime_error(error, runtime_name="claude-code")

    assert failure.failure_class == "provider_auth_missing"
    assert failure.retryable is False
    assert failure.structured is True


def test_provider_process_exit_preserves_structured_authentication_class():
    error = _provider_process_exit_error(
        runtime_name="claude-code",
        exit_code=1,
        stdout=json.dumps(
            {
                "type": "result",
                "is_error": True,
                "api_error_status": 401,
                "result": "localized provider diagnostic",
            }
        ),
        stderr="",
        message="claude-code exited with code 1",
    )

    assert error.failure_class == "provider_auth_missing"


def test_cli_failure_does_not_infer_authentication_from_unstructured_prose():
    failure = classify_cli_failure(
        stdout="",
        stderr="ERROR: unauthorized wording from a user-controlled provider response",
        runtime_name="claude-code",
        exit_code=1,
    )

    assert failure.failure_class == "unknown"


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


def test_redact_text_masks_service_assertion_secret_value():
    redacted = _redact_text(
        "VIVENTIUM_GLASSHIVE_SERVICE_ASSERTION_SECRET=synthetic-sensitive-value"
    )

    assert "synthetic-sensitive-value" not in redacted
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


def test_host_conversation_mode_uses_exact_workspace_without_scaffolding(tmp_path):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "private-state"))
    life = tmp_path / "Life"
    life.mkdir()
    canonical_agents = "# Personal LIFE instructions\n"
    (life / "AGENTS.md").write_text(canonical_agents)
    worker = {
        "worker_id": "wrk_conversation",
        "trusted_run_lane": "conversation",
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


def test_host_conversation_run_publishes_owned_heartbeat_and_clears_active_session(
    tmp_path, monkeypatch
):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "private-state"))
    runtime.set_run_start_observer(lambda _payload: None)
    life = tmp_path / "Life"
    life.mkdir()
    worker = {
        "worker_id": "wrk_conversation_lease",
        "trusted_run_lane": "conversation",
        "name": "Viventium Main",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(life),
        "model": "gpt-5.6-sol",
        "bootstrap_bundle_json": json.dumps({"run_mode": "conversation"}),
    }
    monkeypatch.setattr(
        runtime,
        "ensure_worker_ready",
        lambda current: runtime._host_runtime_info(current),
    )
    monkeypatch.setattr(
        runtime,
        "_build_command",
        lambda *_args, **_kwargs: (["bash", "-lc", "sleep 0.4; printf LEASE_OK"], dict(os.environ)),
    )
    monkeypatch.setattr(
        runtime,
        "_parse_output",
        lambda _worker, stdout, _stderr, _info: (None, stdout.strip()),
    )
    result: list[str] = []
    errors: list[Exception] = []

    def run_conversation() -> None:
        try:
            result.append(runtime.run_task(worker, "Wait briefly.", run_id="run_conversation_lease"))
        except Exception as exc:  # pragma: no cover - assertion reports the captured error
            errors.append(exc)

    thread = threading.Thread(target=run_conversation)
    thread.start()
    deadline = time.time() + 2
    active_session = None
    while time.time() < deadline:
        active_session = runtime._read_active_session(worker["worker_id"])
        if active_session and active_session.get("heartbeat_path"):
            break
        time.sleep(0.01)

    assert active_session is not None
    assert active_session["run_id"] == "run_conversation_lease"
    assert active_session["owner_pid"] == os.getpid()
    heartbeat_path = Path(str(active_session["heartbeat_path"]))
    heartbeat_deadline = time.time() + 2
    while not heartbeat_path.exists() and time.time() < heartbeat_deadline:
        time.sleep(0.01)
    heartbeat = json.loads(heartbeat_path.read_text())
    assert heartbeat["run_id"] == "run_conversation_lease"
    assert heartbeat["state"] == "running"
    assert heartbeat["process_pid"] == active_session["process_pid"]
    assert runtime._active_pid(worker["worker_id"], "different-run") is None

    thread.join(timeout=3)
    assert not thread.is_alive()
    assert errors == []
    assert result == ["LEASE_OK"]
    assert runtime._read_active_session(worker["worker_id"]) is None


def test_host_conversation_capture_scrubs_operation_token_and_bearer_before_durable_write(
    tmp_path, monkeypatch
):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "private-state"))
    runtime.set_run_start_observer(lambda _payload: None)
    life = tmp_path / "Life"
    life.mkdir()
    operation_token = "synthetic-exact-native-operation-token"
    broker_bearer = "synthetic-exact-invocation-broker-bearer"
    token_field = "_viventium_operation_token"
    prepared = json.dumps(
        {
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {"status": "prepared", token_field: operation_token}
                            ),
                        }
                    ]
                },
            },
        }
    )
    committed = json.dumps(
        {
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "arguments": {token_field: operation_token},
                "result": {"status": "ok"},
            },
        }
    )
    worker = {
        "worker_id": "wrk_conversation_secret_capture",
        "trusted_run_lane": "conversation",
        "name": "Viventium Main",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(life),
        "model": "gpt-5.6-sol",
        "bootstrap_bundle_json": json.dumps({"run_mode": "conversation"}),
    }
    user_deliverable = life / "provider-session-events.jsonl"
    user_deliverable.write_text(f"user-owned exact text: {operation_token}\n")
    command_script = (
        "import os, time\n"
        "print(os.environ['SYNTHETIC_PREPARED'], flush=True)\n"
        "print(os.environ['SYNTHETIC_COMMITTED'], flush=True)\n"
        "print('later echo ' + os.environ['SYNTHETIC_OPERATION_TOKEN'] + ' ' + "
        "os.environ['GLASSHIVE_CAPABILITY_BROKER_TOKEN'], flush=True)\n"
        "time.sleep(0.4)\n"
    )
    command_env = {
        **os.environ,
        "SYNTHETIC_PREPARED": prepared,
        "SYNTHETIC_COMMITTED": committed,
        "SYNTHETIC_OPERATION_TOKEN": operation_token,
        "GLASSHIVE_CAPABILITY_BROKER_TOKEN": broker_bearer,
    }
    monkeypatch.setattr(
        runtime,
        "ensure_worker_ready",
        lambda current: runtime._host_runtime_info(current),
    )
    monkeypatch.setattr(
        runtime,
        "_build_command",
        lambda *_args, **_kwargs: ([sys.executable, "-c", command_script], command_env),
    )
    parsed_stdout: list[str] = []

    def parse_output(_worker, stdout, _stderr, _info):
        parsed_stdout.append(stdout)
        return None, "safe completion"

    monkeypatch.setattr(runtime, "_parse_output", parse_output)
    raw_stdout = runtime._run_root(
        worker["worker_id"], "run_conversation_secret_capture"
    ) / "stdout.log"
    outputs: list[str] = []
    errors: list[Exception] = []

    def run_conversation() -> None:
        try:
            outputs.append(
                runtime.run_task(
                    worker,
                    "Exercise the native prepare/commit bridge.",
                    run_id="run_conversation_secret_capture",
                )
            )
        except Exception as exc:  # pragma: no cover - assertion reports details
            errors.append(exc)

    thread = threading.Thread(target=run_conversation)
    thread.start()
    deadline = time.time() + 2
    live_capture = ""
    while time.time() < deadline:
        live_capture = raw_stdout.read_text() if raw_stdout.is_file() else ""
        if "[REDACTED_BROKER_TOKEN]" in live_capture:
            break
        time.sleep(0.01)

    assert thread.is_alive()
    assert operation_token not in live_capture
    assert broker_bearer not in live_capture
    assert "[REDACTED_OPERATION_TOKEN]" in live_capture
    assert "[REDACTED_BROKER_TOKEN]" in live_capture

    thread.join(timeout=3)
    assert not thread.is_alive()
    assert errors == []
    assert outputs == ["safe completion"]
    durable = raw_stdout.read_text()
    assert durable == live_capture
    assert operation_token not in parsed_stdout[0]
    assert broker_bearer not in parsed_stdout[0]
    assert "[REDACTED_OPERATION_TOKEN]" in durable
    assert "[REDACTED_BROKER_TOKEN]" in durable
    assert operation_token in user_deliverable.read_text()


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
        "trusted_run_lane": "conversation",
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


def test_host_codex_conversation_inherits_workspace_project_instructions_by_default(
    tmp_path, monkeypatch
):
    monkeypatch.delenv(
        "WPR_CODEX_CLI_CONVERSATION_PROJECT_INSTRUCTIONS",
        raising=False,
    )
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "private-state"))
    life = tmp_path / "Life"
    life.mkdir()
    worker = {
        "worker_id": "wrk_conversation_with_project_instructions",
        "trusted_run_lane": "conversation",
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

    assert command[command.index("-C") + 1] == str(life)
    assert "--add-dir" not in command


def test_host_capacity_reserves_an_independent_interactive_lane_per_cli_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("WPR_HOST_CONVERSATION_SLOTS_PER_CLI", "1")
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
        "trusted_run_lane": "conversation",
        "bootstrap_bundle_json": json.dumps({"run_mode": "conversation"}),
    }
    runtime._host_active_slots()["mission"] = mission["worker_id"]
    runtime._active_processes[mission["worker_id"]] = ActiveProcess()

    assert runtime.worker_capacity_error(conversation) is None

    runtime._host_active_slots()["conversation"] = "wrk_conversation_active"
    runtime._active_processes["wrk_conversation_active"] = ActiveProcess()
    error = runtime.worker_capacity_error(conversation)
    assert error is not None
    assert error.failure_class == "host_capacity"
    assert error.capacity_class == "family_lane"


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


def test_provider_citation_sources_reads_only_structured_codex_rollout_provenance(tmp_path):
    runtime = ProfiledWorkerRuntime(base_dir=str(tmp_path / "private-state"))
    worker = {
        "worker_id": "wrk_provider_sources",
        "profile": "codex-cli",
        "execution_mode": "host",
    }
    run_id = "run-provider-sources"
    thread_id = "019fe-test-thread"
    run_root = runtime.host_codex._run_root(worker["worker_id"], run_id)
    run_root.mkdir(parents=True)
    (run_root / "stdout.log").write_text(
        json.dumps({"type": "thread.started", "thread_id": thread_id}) + "\n"
    )
    rollout = (
        runtime.host_codex._home_dir(worker["worker_id"])
        / ".codex"
        / "sessions"
        / "2026"
        / "08"
        / "10"
        / f"rollout-{thread_id}.jsonl"
    )
    rollout.parent.mkdir(parents=True)
    rollout.write_text(
        json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "web_search_end",
                    "results": [
                        {
                            "ref_id": "turn0search0",
                            "title": "Primary source",
                            "url": "https://example.invalid/primary",
                            "snippet": "Private result text must not leave the native ledger.",
                        },
                        {
                            "ref_id": "turn0view1",
                            "title": "Unsafe scheme",
                            "url": "file:///private/source",
                        },
                    ],
                },
            }
        )
        + "\n"
    )

    assert runtime.provider_citation_sources(worker, run_id) == [
        {
            "ref_id": "turn0search0",
            "title": "Primary source",
            "url": "https://example.invalid/primary",
        }
    ]


def test_host_conversation_broker_config_stays_in_private_worker_state(tmp_path, monkeypatch):
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", "0")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "synthetic-test-access")
    life = tmp_path / "Life"
    life.mkdir()
    codex_runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "codex-private-state"))
    codex_worker = {
        "worker_id": "wrk_conversation_codex_broker",
        "trusted_run_lane": "conversation",
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
    assert Path(codex_command[0]).name == "codex"
    assert codex_command[1:4] == ["exec", "--json", "--skip-git-repo-check"]
    assert not (life / ".codex").exists()

    claude_runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "claude-private-state"))
    claude_worker = {
        "worker_id": "wrk_conversation_claude_broker",
        "trusted_run_lane": "conversation",
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
    assert claude_env["CLAUDE_CONFIG_DIR"].startswith(str(tmp_path / "claude-private-state"))
    assert not (life / ".mcp.json").exists()
    assert not (life / ".claude").exists()


def test_host_conversation_projects_agent_builder_control_schema_to_both_native_clis(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", "0")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "synthetic-test-access")
    life = tmp_path / "Life"
    life.mkdir()
    control = {
        "version": 1,
        "tools": [
            {
                "name": "lc_transfer_to_specialist",
                "description": "Consult the specialist using shared graph state.",
            }
        ],
    }
    codex_runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "codex-private-state"))
    codex_worker = {
        "worker_id": "wrk_codex_graph_control",
        "trusted_run_lane": "conversation",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(life),
        "bootstrap_bundle_json": json.dumps(
            {
                "run_mode": "conversation",
                "provider_model": "gpt-5.6-sol",
                "access_mode": "full",
                "agent_builder_control": control,
            }
        ),
    }

    codex_command, _ = codex_runtime._build_command(
        codex_worker,
        "Choose the next graph action.",
        codex_runtime._host_runtime_info(codex_worker),
    )

    assert "--output-schema" in codex_command
    codex_schema_path = Path(codex_command[codex_command.index("--output-schema") + 1])
    assert codex_schema_path.is_file()
    codex_schema = json.loads(codex_schema_path.read_text())
    assert codex_schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert codex_schema["additionalProperties"] is False
    assert codex_schema["required"] == ["type", "content", "tool_name"]
    assert codex_schema["properties"]["tool_name"]["enum"] == [
        None,
        "lc_transfer_to_specialist",
    ]
    assert not (life / codex_schema_path.name).exists()
    codex_runtime._write_session_key(
        codex_worker["worker_id"],
        "synthetic-codex-session",
    )
    codex_resume_command, _ = codex_runtime._build_command(
        codex_worker,
        "Return to the graph.",
        codex_runtime._host_runtime_info(codex_worker),
    )
    assert codex_resume_command[1:3] == ["exec", "resume"]
    assert codex_resume_command.index("--output-schema") < codex_resume_command.index(
        "synthetic-codex-session"
    )

    claude_runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "claude-private-state"))
    claude_worker = {
        **codex_worker,
        "worker_id": "wrk_claude_graph_control",
        "profile": "claude-code",
        "model": "opus",
        "bootstrap_bundle_json": json.dumps(
            {
                "run_mode": "conversation",
                "provider_model": "opus",
                "access_mode": "full",
                "agent_builder_control": control,
            }
        ),
    }

    claude_command, _ = claude_runtime._build_command(
        claude_worker,
        "Choose the next graph action.",
        claude_runtime._host_runtime_info(claude_worker),
    )

    assert "--json-schema" in claude_command
    claude_schema = json.loads(
        claude_command[claude_command.index("--json-schema") + 1]
    )
    assert "$schema" not in claude_schema
    assert claude_schema == {
        key: value for key, value in codex_schema.items() if key != "$schema"
    }
    assert claude_schema["additionalProperties"] is False
    assert claude_schema["required"] == ["type", "content", "tool_name"]
    assert claude_schema["properties"]["tool_name"]["enum"] == [
        None,
        "lc_transfer_to_specialist",
    ]
    claude_runtime._write_session_key(
        claude_worker["worker_id"],
        "synthetic-claude-session",
    )
    claude_resume_command, _ = claude_runtime._build_command(
        claude_worker,
        "Return to the graph.",
        claude_runtime._host_runtime_info(claude_worker),
    )
    assert claude_resume_command.index("--json-schema") < claude_resume_command.index(
        "--resume"
    )
    assert claude_resume_command[claude_resume_command.index("--resume") + 1] == (
        "synthetic-claude-session"
    )
    claude_resume_schema = json.loads(
        claude_resume_command[claude_resume_command.index("--json-schema") + 1]
    )
    assert claude_resume_schema == claude_schema


def test_host_mission_and_plain_conversation_commands_do_not_gain_graph_control_schema(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", "0")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "synthetic-test-access")
    life = tmp_path / "Life"
    life.mkdir()
    runtimes_and_flags = [
        (HostCodexCliRuntime(base_dir=str(tmp_path / "codex-state")), "--output-schema"),
        (HostClaudeCodeRuntime(base_dir=str(tmp_path / "claude-state")), "--json-schema"),
    ]
    for index, (runtime, flag) in enumerate(runtimes_and_flags):
        profile = "codex-cli" if isinstance(runtime, HostCodexCliRuntime) else "claude-code"
        for run_mode in ("mission", "conversation"):
            worker = {
                "worker_id": f"wrk_no_graph_control_{index}_{run_mode}",
                "trusted_run_lane": run_mode,
                "profile": profile,
                "execution_mode": "host",
                "workspace_root": str(life),
                "model": "gpt-5.6-sol" if profile == "codex-cli" else "opus",
                "bootstrap_bundle_json": json.dumps({"run_mode": run_mode}),
            }
            command, _ = runtime._build_command(
                worker,
                "Continue normally.",
                runtime._host_runtime_info(worker),
            )
            assert flag not in command


def test_codex_resume_flags_change_only_for_conversation_mode(tmp_path):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "private-state"))
    life = tmp_path / "Life"
    life.mkdir()
    conversation_worker = {
        "worker_id": "wrk_codex_conversation_resume",
        "trusted_run_lane": "conversation",
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
        "trusted_run_lane": "mission",
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

    assert Path(conversation_command[0]).name == "codex"
    assert conversation_command[1:5] == [
        "exec",
        "resume",
        "--json",
        "--skip-git-repo-check",
    ]
    assert Path(mission_command[0]).name == "codex"
    assert mission_command[1:3] == ["exec", "resume"]
    assert "--json" not in mission_command
    assert "--skip-git-repo-check" not in mission_command


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max", "ultra"])
def test_host_codex_conversation_mode_honors_each_declared_effort(tmp_path, effort):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "private-state"))
    life = tmp_path / "Life"
    life.mkdir()
    worker = {
        "worker_id": f"wrk_codex_effort_{effort}",
        "trusted_run_lane": "conversation",
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
        "trusted_run_lane": "conversation",
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


def test_host_codex_read_only_cortex_denies_workspace_mutation_on_new_and_resumed_runs(tmp_path):
    runtime = HostCodexCliRuntime(base_dir=str(tmp_path / "private-state"))
    life = tmp_path / "Life"
    life.mkdir()
    worker = {
        "worker_id": "wrk_codex_read_only_cortex",
        "trusted_run_lane": "conversation",
        "profile": "codex-cli",
        "execution_mode": "host",
        "workspace_root": str(life),
        "bootstrap_bundle_json": json.dumps(
            {
                "run_mode": "conversation",
                "provider_model": "gpt-5.6-sol",
                "access_mode": "read_only",
            }
        ),
    }

    first_command, _ = runtime._build_command(
        worker,
        "Inspect without changing files.",
        runtime._host_runtime_info(worker),
    )
    runtime._ensure_dirs(worker["worker_id"])
    runtime._write_session_key(worker["worker_id"], "session-read-only")
    resumed_command, _ = runtime._build_command(
        worker,
        "Continue inspecting without changes.",
        runtime._host_runtime_info(worker),
    )

    for command in (first_command, resumed_command):
        assert 'sandbox_mode="read-only"' in command
        assert 'approval_policy="never"' in command
        assert "--full-auto" not in command
        assert "--dangerously-bypass-approvals-and-sandbox" not in command


def test_host_claude_conversation_and_mission_modes_use_native_stream_json(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", "0")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "synthetic-test-access")
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "private-state"))
    life = tmp_path / "Life"
    life.mkdir()
    conversation_worker = {
        "worker_id": "wrk_claude_conversation",
        "trusted_run_lane": "conversation",
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
        "trusted_run_lane": "mission",
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
    assert "--include-partial-messages" not in conversation_command
    assert mission_command[mission_command.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in mission_command


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
def test_host_claude_conversation_mode_honors_each_declared_effort(
    tmp_path, monkeypatch, effort
):
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", "0")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "synthetic-test-access")
    monkeypatch.setattr(HostClaudeCodeRuntime, "_effort_supported", lambda _self: True)
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "private-state"))
    life = tmp_path / "Life"
    life.mkdir()
    worker = {
        "worker_id": f"wrk_claude_effort_{effort}",
        "trusted_run_lane": "conversation",
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
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "synthetic-test-access")
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "private-state"))
    life = tmp_path / "Life"
    life.mkdir()
    worker = {
        "worker_id": "wrk_claude_workspace_access",
        "trusted_run_lane": "conversation",
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


def test_host_claude_read_only_cortex_uses_plan_permission_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", "0")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "synthetic-test-access")
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "private-state"))
    life = tmp_path / "Life"
    life.mkdir()
    worker = {
        "worker_id": "wrk_claude_read_only_cortex",
        "trusted_run_lane": "conversation",
        "profile": "claude-code",
        "execution_mode": "host",
        "workspace_root": str(life),
        "model": "opus",
        "bootstrap_bundle_json": json.dumps(
            {
                "run_mode": "conversation",
                "provider_model": "opus",
                "access_mode": "read_only",
            }
        ),
    }

    command, _ = runtime._build_command(
        worker,
        "Inspect without changing files.",
        runtime._host_runtime_info(worker),
    )

    assert command[command.index("--permission-mode") + 1] == "plan"
    assert "--dangerously-skip-permissions" not in command


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
                        "expiresAt": int((time.time() + 3600) * 1000),
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
        "trusted_run_lane": "conversation",
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
    assert "CLAUDE_CODE_OAUTH_REFRESH_TOKEN" not in env
    assert env["CLAUDE_CONFIG_DIR"].startswith(str(tmp_path / "private-state"))
    assert not (life / ".claude").exists()


def test_host_claude_mission_receives_subscription_auth_without_optional_conversation_bundle(
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
                        "accessToken": "synthetic-mission-access-token",
                        "refreshToken": "synthetic-mission-refresh-token",
                        "expiresAt": int((time.time() + 3600) * 1000),
                    }
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(
        "workers_projects_runtime.profile_runtime.subprocess.run", fake_run
    )
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "private-state"))
    worker = {
        "worker_id": "wrk_claude_mission_auth",
        "profile": "claude-code",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "missions"),
        "model": "opus",
    }

    _, env = runtime._build_command(
        worker,
        "Complete the mission.",
        runtime._host_runtime_info(worker),
    )

    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "synthetic-mission-access-token"
    assert "CLAUDE_CODE_OAUTH_REFRESH_TOKEN" not in env
    assert env["CLAUDE_CONFIG_DIR"].startswith(str(tmp_path / "private-state"))


def test_host_claude_enterprise_mission_never_discovers_owner_local_auth(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", "0")
    monkeypatch.setenv("GLASSHIVE_ENTERPRISE_MODE", "1")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_REFRESH_TOKEN", raising=False)

    def reject_owner_auth_discovery(*_args, **_kwargs):
        raise AssertionError("Enterprise host missions must not query owner-local auth")

    monkeypatch.setattr(
        "workers_projects_runtime.profile_runtime.subprocess.run",
        reject_owner_auth_discovery,
    )
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "private-state"))
    worker = {
        "worker_id": "wrk_claude_enterprise_mission_auth",
        "profile": "claude-code",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "missions"),
        "model": "opus",
    }

    with pytest.raises(RuntimeErrorBase, match="server-owned Claude Code authentication"):
        runtime._build_command(
            worker,
            "Complete the mission.",
            runtime._host_runtime_info(worker),
        )


@pytest.mark.parametrize("session_key", [None, "synthetic-resume-session"])
@pytest.mark.parametrize("use_api_key", [False, True])
def test_host_claude_expired_keychain_token_uses_managed_auth_without_stale_override(
    tmp_path, monkeypatch, session_key, use_api_key
):
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", "0")
    monkeypatch.setenv("WPR_CLAUDE_CODE_USE_API_KEY", "1" if use_api_key else "0")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_REFRESH_TOKEN", raising=False)
    monkeypatch.setattr("workers_projects_runtime.profile_runtime.sys.platform", "darwin")
    monkeypatch.setattr(
        "workers_projects_runtime.profile_runtime.shutil.which", lambda binary: f"/usr/bin/{binary}"
    )
    status_envs: list[dict[str, str]] = []

    def fake_run(command, **kwargs):
        if command[:4] == ["security", "find-generic-password", "-s", "Claude Code-credentials"]:
            return subprocess.CompletedProcess(
                command,
                returncode=0,
                stdout=json.dumps(
                    {
                        "claudeAiOauth": {
                            "accessToken": "synthetic-expired-access",
                            "refreshToken": "synthetic-valid-refresh",
                            "expiresAt": int((time.time() - 60) * 1000),
                        }
                    }
                ),
                stderr="",
            )
        assert command == ["/usr/bin/claude", "auth", "status"]
        status_envs.append(dict(kwargs["env"]))
        return subprocess.CompletedProcess(
            command,
            returncode=0,
            stdout=json.dumps(
                {
                    "loggedIn": ("ANTHROPIC_API_KEY" in kwargs["env"]) is use_api_key,
                    "authMethod": "claude.ai",
                }
            ),
            stderr="",
        )

    monkeypatch.setattr("workers_projects_runtime.profile_runtime.subprocess.run", fake_run)
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "private-state"))
    monkeypatch.setattr(runtime, "_read_session_key", lambda _worker_id: session_key)
    worker = {
        "worker_id": "wrk_claude_expired_managed",
        "trusted_run_lane": "conversation",
        "profile": "claude-code",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "Life"),
        "model": "opus",
        "bootstrap_bundle_json": json.dumps(
            {
                "run_mode": "conversation",
                "env": {"ANTHROPIC_API_KEY": "synthetic-anthropic-key"},
            }
        ),
    }

    command, env = runtime._build_command(
        worker,
        "Talk naturally.",
        runtime._host_runtime_info(worker),
    )

    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert "CLAUDE_CODE_OAUTH_REFRESH_TOKEN" not in env
    assert ("--resume" in command) is bool(session_key)
    assert status_envs == [env]
    assert status_envs[0]["CLAUDE_CONFIG_DIR"].startswith(str(tmp_path / "private-state"))
    assert ("ANTHROPIC_API_KEY" in env) is use_api_key


def test_host_claude_expired_keychain_token_without_managed_auth_fails_before_run(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", "0")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_REFRESH_TOKEN", raising=False)
    monkeypatch.setattr("workers_projects_runtime.profile_runtime.sys.platform", "darwin")
    monkeypatch.setattr(
        "workers_projects_runtime.profile_runtime.shutil.which", lambda binary: f"/usr/bin/{binary}"
    )
    status_envs: list[dict[str, str]] = []

    def fake_run(command, **kwargs):
        if command[0] == "security":
            return subprocess.CompletedProcess(
                command,
                returncode=0,
                stdout=json.dumps(
                    {
                        "claudeAiOauth": {
                            "accessToken": "synthetic-expired-access",
                            "refreshToken": "synthetic-refresh",
                            "expiresAt": int((time.time() - 60) * 1000),
                        }
                    }
                ),
                stderr="",
            )
        status_envs.append(dict(kwargs["env"]))
        return subprocess.CompletedProcess(
            command,
            returncode=1,
            stdout=json.dumps({"loggedIn": False}),
            stderr="",
        )

    monkeypatch.setattr("workers_projects_runtime.profile_runtime.subprocess.run", fake_run)
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "private-state"))
    worker = {
        "worker_id": "wrk_claude_expired_unmanaged",
        "trusted_run_lane": "conversation",
        "profile": "claude-code",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "Life"),
        "model": "opus",
        "bootstrap_bundle_json": json.dumps({"run_mode": "conversation"}),
    }

    with pytest.raises(RuntimeErrorBase) as exc_info:
        runtime._build_command(worker, "Talk naturally.", runtime._host_runtime_info(worker))

    message = str(exc_info.value)
    assert "claude setup-token" in message
    assert "claude auth login" in message
    assert "synthetic-expired-access" not in message
    assert "synthetic-refresh" not in message
    assert len(status_envs) == 1
    assert status_envs[0]["CLAUDE_CONFIG_DIR"].startswith(str(tmp_path / "private-state"))
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in status_envs[0]
    assert "CLAUDE_CODE_OAUTH_REFRESH_TOKEN" not in status_envs[0]


@pytest.mark.parametrize("keychain_payload", ["not-json", json.dumps({})])
def test_host_claude_unusable_keychain_payload_fails_closed_without_managed_auth(
    tmp_path, monkeypatch, keychain_payload
):
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", "0")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_REFRESH_TOKEN", raising=False)
    monkeypatch.setattr("workers_projects_runtime.profile_runtime.sys.platform", "darwin")
    monkeypatch.setattr(
        "workers_projects_runtime.profile_runtime.shutil.which", lambda binary: f"/usr/bin/{binary}"
    )

    def fake_run(command, **_kwargs):
        if command[0] == "security":
            return subprocess.CompletedProcess(
                command,
                returncode=0,
                stdout=keychain_payload,
                stderr="",
            )
        return subprocess.CompletedProcess(
            command,
            returncode=1,
            stdout=json.dumps({"loggedIn": False}),
            stderr="",
        )

    monkeypatch.setattr("workers_projects_runtime.profile_runtime.subprocess.run", fake_run)
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "private-state"))
    worker = {
        "worker_id": "wrk_claude_unusable_keychain",
        "trusted_run_lane": "conversation",
        "profile": "claude-code",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "Life"),
        "model": "opus",
        "bootstrap_bundle_json": json.dumps({"run_mode": "conversation"}),
    }

    with pytest.raises(RuntimeErrorBase, match="claude setup-token"):
        runtime._build_command(worker, "Talk naturally.", runtime._host_runtime_info(worker))


def test_host_claude_malformed_managed_auth_status_fails_closed_under_child_env(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", "0")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_REFRESH_TOKEN", raising=False)
    monkeypatch.setattr("workers_projects_runtime.profile_runtime.sys.platform", "darwin")
    monkeypatch.setattr(
        "workers_projects_runtime.profile_runtime.shutil.which", lambda binary: f"/usr/bin/{binary}"
    )
    status_envs: list[dict[str, str]] = []

    def fake_run(command, **kwargs):
        if command[0] == "security":
            return subprocess.CompletedProcess(command, returncode=1, stdout="", stderr="")
        status_envs.append(dict(kwargs["env"]))
        return subprocess.CompletedProcess(
            command,
            returncode=0,
            stdout="not-json",
            stderr="",
        )

    monkeypatch.setattr("workers_projects_runtime.profile_runtime.subprocess.run", fake_run)
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "private-state"))
    worker = {
        "worker_id": "wrk_claude_malformed_managed_auth",
        "trusted_run_lane": "conversation",
        "profile": "claude-code",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "Life"),
        "model": "opus",
        "bootstrap_bundle_json": json.dumps({"run_mode": "conversation"}),
    }

    with pytest.raises(RuntimeErrorBase, match="claude setup-token"):
        runtime._build_command(worker, "Talk naturally.", runtime._host_runtime_info(worker))

    assert len(status_envs) == 1
    assert status_envs[0]["CLAUDE_CONFIG_DIR"].startswith(str(tmp_path / "private-state"))


@pytest.mark.parametrize("session_key", [None, "synthetic-resume-session"])
def test_host_claude_signed_bootstrap_access_token_wins_without_auth_discovery(
    tmp_path, monkeypatch, session_key
):
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", "0")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "synthetic-ambient-access")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_REFRESH_TOKEN", "synthetic-ambient-refresh")

    def reject_auth_discovery(*_args, **_kwargs):
        raise AssertionError("Signed bootstrap auth must not query Keychain or Claude auth status")

    monkeypatch.setattr(
        "workers_projects_runtime.profile_runtime.subprocess.run", reject_auth_discovery
    )
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "private-state"))
    monkeypatch.setattr(runtime, "_read_session_key", lambda _worker_id: session_key)
    worker = {
        "worker_id": "wrk_claude_signed_bootstrap_auth",
        "trusted_run_lane": "conversation",
        "profile": "claude-code",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "Life"),
        "model": "opus",
        "bootstrap_bundle_json": json.dumps(
            {
                "run_mode": "conversation",
                "env": {"CLAUDE_CODE_OAUTH_TOKEN": "synthetic-signed-bootstrap-access"},
            }
        ),
    }

    command, env = runtime._build_command(
        worker,
        "Talk naturally.",
        runtime._host_runtime_info(worker),
    )

    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "synthetic-signed-bootstrap-access"
    assert "CLAUDE_CODE_OAUTH_REFRESH_TOKEN" not in env
    assert ("--resume" in command) is bool(session_key)


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
        "trusted_run_lane": "conversation",
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
    assert "CLAUDE_CODE_OAUTH_REFRESH_TOKEN" not in env


def test_host_claude_mission_strips_projected_refresh_token_unconditionally(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "synthetic-test-access")
    monkeypatch.setenv("WPR_CLAUDE_CODE_ENABLE_CHROME", "0")
    runtime = HostClaudeCodeRuntime(base_dir=str(tmp_path / "private-state"))
    worker = {
        "worker_id": "wrk_claude_mission_refresh_boundary",
        "profile": "claude-code",
        "execution_mode": "host",
        "workspace_root": str(tmp_path / "Life"),
        "model": "opus",
        "bootstrap_bundle_json": json.dumps(
            {
                "run_mode": "mission",
                "env": {
                    "CLAUDE_CODE_OAUTH_REFRESH_TOKEN": "synthetic-refresh-must-not-project"
                },
            }
        ),
    }

    _command, env = runtime._build_command(
        worker,
        "Complete the mission.",
        runtime._host_runtime_info(worker),
    )

    assert "CLAUDE_CODE_OAUTH_REFRESH_TOKEN" not in env


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
