from __future__ import annotations

import json
import zipfile
from pathlib import Path

from workers_projects_runtime.deliverables import candidate_artifact_paths
from workers_projects_runtime.runtime_identity import derive_legacy_backend_label
from workers_projects_runtime.run_evidence import (
    _text_artifact_payload,
    build_constraint_ledger,
    build_run_evidence,
    check_content_hygiene,
    summarize_run_evidence_result,
    write_constraint_ledger,
    write_run_evidence,
)


def test_constraint_ledger_extracts_generic_source_date_and_flag_rules(tmp_path):
    instruction = (
        "Research the target market using sources from January 2024 through May 2026 only.\n"
        "Include seed firms Alpha Capital and Beta Partners.\n"
        "Do not exclude possible platform partners; flag them instead.\n"
        "Deliver an XLSX and PDF report."
    )

    ledger = build_constraint_ledger(
        instruction=instruction,
        worker={"worker_id": "wrk_ledger", "profile": "codex-cli", "execution_mode": "host"},
        run_id="run_ledger",
    )

    assert ledger["run_id"] == "run_ledger"
    assert ledger["worker"]["profile"] == "codex-cli"
    assert any("January 2024 through May 2026 only" in item for item in ledger["constraints"]["date"])
    assert any("sources from" in item for item in ledger["constraints"]["source"])
    assert any("Do not exclude" in item for item in ledger["constraints"]["exclusion_or_flag"])
    assert any("XLSX and PDF" in item for item in ledger["outputs"]["required"])
    assert ledger["do_not_widen_or_soften"] is True

    latest = write_constraint_ledger(tmp_path, ledger, "run_ledger")
    assert latest == tmp_path / "glasshive-run" / "constraint-ledger.json"
    assert json.loads(latest.read_text())["run_id"] == "run_ledger"
    assert (tmp_path / "glasshive-run" / "runs" / "run_ledger" / "constraint-ledger.json").exists()


def test_run_evidence_recursively_validates_artifacts_and_flags_date_drift(tmp_path):
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "screen.csv").write_text("firm,notes\nAlpha,ok\n")
    (output_dir / "report.pdf").write_bytes(b"%PDF-1.7\n% synthetic\n")
    with zipfile.ZipFile(output_dir / "workbook.xlsx", "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types></Types>")
        archive.writestr("xl/workbook.xml", "<workbook></workbook>")
    research = tmp_path / "research"
    research.mkdir()
    (research / "SPEC.md").write_text("Use sources dated Jan 2024 - June 2026 wherever possible.\n")
    (tmp_path / "glasshive-run").mkdir()
    (tmp_path / "glasshive-run" / "internal.txt").write_text("not a user artifact")

    ledger = build_constraint_ledger(
        instruction="Use sources from January 2024 through May 2026 only.",
        worker={"worker_id": "wrk_artifacts", "profile": "claude-code", "execution_mode": "host"},
        run_id="run_artifacts",
    )

    evidence = build_run_evidence(
        worker={"worker_id": "wrk_artifacts", "profile": "claude-code", "execution_mode": "host"},
        run_id="run_artifacts",
        runtime_name="claude-code",
        model="claude-opus-4-8",
        command=["claude", "-p", "--effort", "max"],
        env={"GLASSHIVE_RUN_ID": "run_artifacts", "SECRET_TOKEN": "private"},
        workspace_dir=tmp_path,
        stdout_text="FINAL REPORT:\ncreated files",
        stderr_text="",
        output_text="FINAL REPORT:\ncreated files",
        error_text="",
        exit_code=0,
        timeout_seconds=None,
        stop_reason="",
        constraint_ledger=ledger,
    )

    artifact_paths = {item["path"] for item in evidence["artifacts"]["items"]}
    assert "output/screen.csv" in artifact_paths
    assert "output/report.pdf" in artifact_paths
    assert "output/workbook.xlsx" in artifact_paths
    assert "glasshive-run/internal.txt" not in artifact_paths
    workbook = next(item for item in evidence["artifacts"]["items"] if item["path"] == "output/workbook.xlsx")
    assert workbook["document_validation"]["valid"] is True
    assert "SECRET_TOKEN" not in evidence["env_keys"]
    assert evidence["final_output"]["has_final_report"] is True
    assert evidence["constraint_compliance"]["status"] == "fail"
    assert any("June 2026" in issue["text"] for issue in evidence["constraint_compliance"]["issues"])
    assert evidence["evidence_result"]["status"] == "fail"
    assert any(item["reason"] == "constraint compliance failed" for item in evidence["evidence_result"]["failure_reasons"])

    latest = write_run_evidence(tmp_path, evidence, "run_artifacts")
    assert json.loads(latest.read_text())["run_id"] == "run_artifacts"
    assert (tmp_path / "glasshive-run" / "runs" / "run_artifacts" / "evidence.json").exists()


def test_pdf_render_sample_uses_temp_output_without_orphaning_artifact(tmp_path, monkeypatch):
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "report.pdf").write_bytes(b"%PDF-1.7\n% synthetic\n")

    def fake_run(command, *, check, capture_output, text, timeout):
        _ = check, capture_output, text, timeout
        Path(command[-1] + ".png").write_bytes(b"png")

        class Completed:
            returncode = 0
            stdout = "ok"
            stderr = ""

        return Completed()

    monkeypatch.setattr("workers_projects_runtime.run_evidence.shutil.which", lambda name: "/usr/bin/pdftoppm" if name == "pdftoppm" else None)
    monkeypatch.setattr("workers_projects_runtime.run_evidence.subprocess.run", fake_run)

    evidence = build_run_evidence(
        worker={"worker_id": "wrk_pdf", "profile": "codex-cli", "execution_mode": "host"},
        run_id="run_pdf",
        runtime_name="codex-cli",
        model="gpt-test",
        command=["codex", "exec", "Render PDF."],
        env={},
        workspace_dir=tmp_path,
        stdout_text="FINAL REPORT:\nDone",
        stderr_text="",
        output_text="Done",
        error_text="",
        exit_code=0,
        timeout_seconds=None,
        stop_reason="process_exit",
        constraint_ledger=None,
    )

    pdf = next(item for item in evidence["artifacts"]["items"] if item["path"] == "output/report.pdf")
    assert pdf["document_validation"]["render_sample"]["status"] == "pass"
    assert pdf["document_validation"]["render_sample"]["sample_created"] is True
    assert list(output_dir.glob("*glasshive-sample*.png")) == []


def test_content_hygiene_is_generic_and_advisory():
    clean = {"risk_notes": "VAR analysis, VaR limits, and VaR estimate = 0.05 are normal finance prose."}
    negative_context = {
        "quality_notes": "CSV cells are compact and exclude webpage navigation, cookie notices, scripts, and scraped page text."
    }
    long_report = {"output/report.md": "This is ordinary report prose with no crawl or navigation contamination. " * 25}
    long_structured = {"output/firms.csv": "x" * 901}
    dirty = {
        "one": "Please enable JavaScript to continue reading this article.",
        "two": "Subscribe to continue reading the full report.",
        "three": "window.dataLayer = window.dataLayer || [];",
        "four": "Cookie Settings Privacy Policy Terms of Use",
        "five": "Company&nbsp;overview &amp; leadership",
    }

    clean_result = check_content_hygiene(clean)
    negative_context_result = check_content_hygiene(negative_context)
    long_report_result = check_content_hygiene(long_report)
    long_structured_result = check_content_hygiene(long_structured)
    dirty_result = check_content_hygiene(dirty)

    assert clean_result["status"] == "pass"
    assert clean_result["failure_count"] == 0
    assert negative_context_result["status"] == "pass"
    assert long_report_result["status"] == "pass"
    assert long_structured_result["status"] == "warn"
    assert dirty_result["status"] == "warn"
    assert dirty_result["failure_count"] >= 5


def test_run_evidence_derives_backend_from_profile_not_stale_worker_field(tmp_path):
    worker = {
        "worker_id": "wrk_backend_truth",
        "profile": "codex-cli",
        "execution_mode": "host",
        "backend": "openclaw",
    }
    ledger = build_constraint_ledger(instruction="Do the work.", worker=worker, run_id="run_backend_truth")
    evidence = build_run_evidence(
        worker=worker,
        run_id="run_backend_truth",
        runtime_name="codex-cli",
        model="gpt-test",
        command=["codex", "exec", "Do the work."],
        env={},
        workspace_dir=tmp_path,
        stdout_text="FINAL REPORT:\nDone",
        stderr_text="",
        output_text="Done",
        error_text="",
        exit_code=0,
        timeout_seconds=None,
        stop_reason="process_exit",
        constraint_ledger=ledger,
    )

    assert ledger["worker"]["backend"] == "codex-cli"
    assert evidence["worker"]["backend"] == "codex-cli"
    assert derive_legacy_backend_label(profile="claude-code", backend="openclaw") == "claude-code"
    assert derive_legacy_backend_label(profile="custom-profile", runtime="custom-runtime", backend="openclaw") == "custom-runtime"


def test_run_evidence_records_effective_effort_from_command_and_env(tmp_path):
    codex = build_run_evidence(
        worker={"worker_id": "wrk_effort", "profile": "codex-cli", "execution_mode": "host"},
        run_id="run_effort_codex",
        runtime_name="codex-cli",
        model="gpt-test",
        command=["codex", "exec", "-c", 'model_reasoning_effort="xhigh"', "Do it."],
        env={},
        workspace_dir=tmp_path,
        stdout_text="FINAL REPORT:\nDone",
        stderr_text="",
        output_text="Done",
        error_text="",
        exit_code=0,
        timeout_seconds=None,
        stop_reason="process_exit",
        constraint_ledger=None,
    )
    claude = build_run_evidence(
        worker={"worker_id": "wrk_effort", "profile": "claude-code", "execution_mode": "host"},
        run_id="run_effort_claude",
        runtime_name="claude-code",
        model="claude-test",
        command=["claude", "-p", "--effort", "max", "Do it."],
        env={"WPR_CLAUDE_CODE_EFFORT": "max"},
        workspace_dir=tmp_path,
        stdout_text="FINAL REPORT:\nDone",
        stderr_text="",
        output_text="Done",
        error_text="",
        exit_code=0,
        timeout_seconds=None,
        stop_reason="process_exit",
        constraint_ledger=None,
    )

    assert codex["effort"] == "xhigh"
    assert claude["effort"] == "max"


def test_run_evidence_redacts_long_command_prompt_and_local_home_path(tmp_path):
    long_prompt = "Private benchmark prompt\n" + ("sensitive details " * 80)
    evidence = build_run_evidence(
        worker={"worker_id": "wrk_redact", "profile": "codex-cli", "execution_mode": "host"},
        run_id="run_redact",
        runtime_name="codex-cli",
        model="gpt-test",
        command=["/Users/example/bin/codex", "exec", "-C", "/Users/example/private-workspace", long_prompt],
        env={},
        workspace_dir=tmp_path,
        stdout_text="FINAL REPORT:\nDone",
        stderr_text="",
        output_text="Done",
        error_text="",
        exit_code=0,
        timeout_seconds=None,
        stop_reason="process_exit",
        constraint_ledger=None,
    )

    argv = evidence["command"]["argv_redacted"]
    display = evidence["command"]["display_redacted"]

    assert argv[0] == "~/bin/codex"
    assert argv[3] == "~/private-workspace"
    assert argv[-1].startswith("[REDACTED_LONG_ARG chars=")
    assert "sensitive details" not in display
    assert "/Users/example" not in display


def test_run_evidence_final_report_ignores_instruction_only_transcript_marker(tmp_path):
    evidence = build_run_evidence(
        worker={"worker_id": "wrk_final_marker", "profile": "codex-cli", "execution_mode": "host"},
        run_id="run_final_marker",
        runtime_name="codex-cli",
        model="gpt-test",
        command=["codex", "exec", "Do it."],
        env={},
        workspace_dir=tmp_path,
        stdout_text="The instructions say to end with FINAL REPORT:, but no final answer was captured.",
        stderr_text="",
        output_text="",
        error_text="Interrupted by operator",
        exit_code=None,
        timeout_seconds=None,
        stop_reason="interrupted",
        constraint_ledger=None,
    )

    assert evidence["final_output"]["has_final_report"] is False
    assert evidence["final_output"]["status"] == "failed"


def test_run_evidence_detects_claude_result_final_report_marker(tmp_path):
    stdout_text = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "result": "All files verified.\n\nFINAL REPORT:\nCreated output/summary.csv.",
        }
    )

    evidence = build_run_evidence(
        worker={"worker_id": "wrk_claude_final", "profile": "claude-code", "execution_mode": "host"},
        run_id="run_claude_final",
        runtime_name="claude-code",
        model="claude-test",
        command=["claude", "-p", "--effort", "max", "Do it."],
        env={"WPR_CLAUDE_CODE_EFFORT": "max"},
        workspace_dir=tmp_path,
        stdout_text=stdout_text,
        stderr_text="",
        output_text="Created output/summary.csv.",
        error_text="",
        exit_code=0,
        timeout_seconds=None,
        stop_reason="process_exit",
        constraint_ledger=None,
    )

    assert evidence["final_output"]["has_final_report"] is True
    assert evidence["completion_compliance"]["status"] == "pass"


def test_run_evidence_detects_stdout_final_report_when_output_text_missing(tmp_path):
    stdout_text = json.dumps(
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": "FINAL REPORT:\nCreated output/report.md.",
            },
        }
    )

    evidence = build_run_evidence(
        worker={"worker_id": "wrk_stdout_final", "profile": "codex-cli", "execution_mode": "host"},
        run_id="run_stdout_final",
        runtime_name="codex-cli",
        model="gpt-test",
        command=["codex", "exec", "Do it."],
        env={},
        workspace_dir=tmp_path,
        stdout_text=stdout_text,
        stderr_text="",
        output_text="",
        error_text="",
        exit_code=0,
        timeout_seconds=None,
        stop_reason="process_exit",
        constraint_ledger=None,
    )

    assert evidence["final_output"]["has_final_report"] is True


def test_constraint_compliance_avoids_generic_future_and_approximate_false_positives(tmp_path):
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "report.md").write_text(
        "The project plans a March 2027 launch and approximately 5% growth.\n"
        "The analysis cites official sources through May 2026.\n"
    )
    research_dir = tmp_path / "research"
    research_dir.mkdir()
    (research_dir / "notes.md").write_text("Best effort given available public sources.\n")
    ledger = build_constraint_ledger(
        instruction="Use sources from January 2024 through May 2026 only.",
        worker={"worker_id": "wrk_constraints", "profile": "codex-cli", "execution_mode": "host"},
        run_id="run_constraints",
    )

    evidence = build_run_evidence(
        worker={"worker_id": "wrk_constraints", "profile": "codex-cli", "execution_mode": "host"},
        run_id="run_constraints",
        runtime_name="codex-cli",
        model="gpt-test",
        command=["codex", "exec", "Analyze."],
        env={},
        workspace_dir=tmp_path,
        stdout_text="FINAL REPORT:\nDone",
        stderr_text="",
        output_text="Done",
        error_text="",
        exit_code=0,
        timeout_seconds=300,
        stop_reason="process_exit",
        constraint_ledger=ledger,
    )

    assert evidence["timeout"]["exit_source"] == "process"
    assert evidence["constraint_compliance"]["status"] == "pass"


def test_constraint_compliance_allows_official_future_subject_when_sources_remain_in_window(tmp_path):
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "report.md").write_text(
        "Per the official roadmap summarized from in-window sources, the product plans a March 2027 launch.\n"
        "A July 2027 forecast appears in the source dated May 2026.\n"
        "Sources used were published through May 2026.\n"
    )
    ledger = build_constraint_ledger(
        instruction="Use sources from January 2024 through May 2026 only.",
        worker={"worker_id": "wrk_future_subject", "profile": "codex-cli", "execution_mode": "host"},
        run_id="run_future_subject",
    )

    evidence = build_run_evidence(
        worker={"worker_id": "wrk_future_subject", "profile": "codex-cli", "execution_mode": "host"},
        run_id="run_future_subject",
        runtime_name="codex-cli",
        model="gpt-test",
        command=["codex", "exec", "Analyze."],
        env={},
        workspace_dir=tmp_path,
        stdout_text="FINAL REPORT:\nDone",
        stderr_text="",
        output_text="Done",
        error_text="",
        exit_code=0,
        timeout_seconds=300,
        stop_reason="process_exit",
        constraint_ledger=ledger,
    )

    assert evidence["constraint_compliance"]["status"] == "pass"


def test_constraint_compliance_allows_explicitly_rejected_out_of_scope_sources(tmp_path):
    research_dir = tmp_path / "research"
    research_dir.mkdir()
    (research_dir / "rejected-sources.md").write_text(
        "Rejected out-of-scope evidence: Example source published June 2026; not used for facts, scoring, or deliverables.\n"
    )
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "report.md").write_text("FINAL REPORT:\nUsed only in-window evidence.\n")
    ledger = build_constraint_ledger(
        instruction="Use sources from January 2024 through May 2026 only.",
        worker={"worker_id": "wrk_rejected_source", "profile": "codex-cli", "execution_mode": "host"},
        run_id="run_rejected_source",
    )

    evidence = build_run_evidence(
        worker={"worker_id": "wrk_rejected_source", "profile": "codex-cli", "execution_mode": "host"},
        run_id="run_rejected_source",
        runtime_name="codex-cli",
        model="gpt-test",
        command=["codex", "exec", "Analyze."],
        env={},
        workspace_dir=tmp_path,
        stdout_text="FINAL REPORT:\nDone",
        stderr_text="",
        output_text="Done",
        error_text="",
        exit_code=0,
        timeout_seconds=300,
        stop_reason="process_exit",
        constraint_ledger=ledger,
    )

    assert evidence["constraint_compliance"]["status"] == "pass"


def test_constraint_compliance_still_flags_out_of_window_sources_only_marked_flagged(tmp_path):
    research_dir = tmp_path / "research"
    research_dir.mkdir()
    (research_dir / "notes.md").write_text("Flagged source published June 2026 appears in analysis notes.\n")
    ledger = build_constraint_ledger(
        instruction="Use sources from January 2024 through May 2026 only.",
        worker={"worker_id": "wrk_flagged_source", "profile": "codex-cli", "execution_mode": "host"},
        run_id="run_flagged_source",
    )

    evidence = build_run_evidence(
        worker={"worker_id": "wrk_flagged_source", "profile": "codex-cli", "execution_mode": "host"},
        run_id="run_flagged_source",
        runtime_name="codex-cli",
        model="gpt-test",
        command=["codex", "exec", "Analyze."],
        env={},
        workspace_dir=tmp_path,
        stdout_text="FINAL REPORT:\nDone",
        stderr_text="",
        output_text="Done",
        error_text="",
        exit_code=0,
        timeout_seconds=300,
        stop_reason="process_exit",
        constraint_ledger=ledger,
    )

    assert evidence["constraint_compliance"]["status"] == "fail"
    assert "June 2026" in evidence["constraint_compliance"]["issues"][0]["text"]


def test_candidate_artifact_paths_sorts_before_max_entry_cap(tmp_path):
    for index in range(25):
        (tmp_path / f"root-{index:02d}.txt").write_text("lower priority")
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    priority_artifact = output_dir / "final.csv"
    priority_artifact.write_text("name,value\nAlpha,1\n")

    paths = candidate_artifact_paths({"workspace_dir": str(tmp_path)}, max_entries=1)

    assert paths == [priority_artifact]


def test_run_evidence_records_html_browser_smoke_prerequisite(tmp_path, monkeypatch):
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "index.html").write_text(
        "<!doctype html><title>Smoke</title><main>Ready&nbsp;now</main><script>window.app=function(){return true}</script>"
    )
    monkeypatch.setattr("workers_projects_runtime.run_evidence.shutil.which", lambda _name: None)

    evidence = build_run_evidence(
        worker={"worker_id": "wrk_html", "profile": "codex-cli", "execution_mode": "host"},
        run_id="run_html",
        runtime_name="codex-cli",
        model="gpt-test",
        command=["codex", "exec", "Create HTML."],
        env={},
        workspace_dir=tmp_path,
        stdout_text="FINAL REPORT:\nCreated HTML",
        stderr_text="",
        output_text="Created HTML",
        error_text="",
        exit_code=0,
        timeout_seconds=None,
        stop_reason="process_exit",
        constraint_ledger=None,
    )

    html = next(item for item in evidence["artifacts"]["items"] if item["path"] == "output/index.html")
    assert html["html_validation"]["status"] == "unavailable"
    assert evidence["visual_render_evidence"]["status"] == "unavailable"
    assert evidence["content_hygiene"]["status"] == "pass"


def test_run_evidence_flags_notes_only_when_required_artifacts_are_missing(tmp_path):
    research_dir = tmp_path / "research"
    research_dir.mkdir()
    (research_dir / "batch-01.md").write_text("Research notes only.\nAlpha Capital appears here.\n")
    ledger = build_constraint_ledger(
        instruction="Include seed firms Alpha Capital and Beta Partners. Deliver PDF and XLSX artifacts.",
        worker={"worker_id": "wrk_notes_only", "profile": "claude-code", "execution_mode": "host"},
        run_id="run_notes_only",
    )

    evidence = build_run_evidence(
        worker={"worker_id": "wrk_notes_only", "profile": "claude-code", "execution_mode": "host"},
        run_id="run_notes_only",
        runtime_name="claude-code",
        model="claude-opus-4-8",
        command=["claude", "-p", "--effort", "max", "Analyze."],
        env={},
        workspace_dir=tmp_path,
        stdout_text="",
        stderr_text="Interrupted",
        output_text="",
        error_text="Interrupted",
        exit_code=None,
        timeout_seconds=300,
        stop_reason="timeout",
        constraint_ledger=ledger,
    )

    completion = evidence["completion_compliance"]
    assert completion["status"] == "fail"
    assert completion["notes_only"] is True
    assert set(completion["missing_required_artifact_types"]) == {"pdf", "xlsx"}
    assert completion["seed_entity_coverage"]["mentioned_count"] == 1
    assert "Beta Partners" in completion["seed_entity_coverage"]["missing"]
    assert evidence["evidence_result"]["status"] == "fail"


def test_run_evidence_completion_compliance_passes_for_requested_deliverables(tmp_path):
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "summary.csv").write_text("name,notes\nAlpha Capital,done\nBeta Partners,done\n")
    (output_dir / "report.pdf").write_bytes(b"%PDF-1.7\n% synthetic\n")
    with zipfile.ZipFile(output_dir / "screen.xlsx", "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types></Types>")
        archive.writestr("xl/workbook.xml", "<workbook></workbook>")
    ledger = build_constraint_ledger(
        instruction="Include seed firms Alpha Capital and Beta Partners. Deliver PDF, XLSX, and CSV artifacts.",
        worker={"worker_id": "wrk_complete", "profile": "codex-cli", "execution_mode": "host"},
        run_id="run_complete",
    )

    evidence = build_run_evidence(
        worker={"worker_id": "wrk_complete", "profile": "codex-cli", "execution_mode": "host"},
        run_id="run_complete",
        runtime_name="codex-cli",
        model="gpt-test",
        command=["codex", "exec", "-c", 'model_reasoning_effort="xhigh"', "Analyze."],
        env={},
        workspace_dir=tmp_path,
        stdout_text="FINAL REPORT:\nDone",
        stderr_text="",
        output_text="FINAL REPORT:\nDone",
        error_text="",
        exit_code=0,
        timeout_seconds=300,
        stop_reason="process_exit",
        constraint_ledger=ledger,
    )

    completion = evidence["completion_compliance"]
    assert completion["status"] == "pass"
    assert completion["missing_required_artifact_types"] == []
    assert completion["notes_only"] is False
    assert completion["seed_entity_coverage"]["missing"] == []
    assert evidence["evidence_result"]["status"] == "pass"


def test_run_evidence_hygiene_scans_csv_cells_not_whole_file(tmp_path):
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    rows = ["id,entity,finding,rationale"]
    for index in range(1, 80):
        rows.append(f"{index},Alpha Robotics,Compact clean finding {index},Short rationale {index}")
    (output_dir / "screen.csv").write_text("\n".join(rows) + "\n")

    evidence = build_run_evidence(
        worker={"worker_id": "wrk_csv_cells", "profile": "codex-cli", "execution_mode": "host"},
        run_id="run_csv_cells",
        runtime_name="codex-cli",
        model="gpt-test",
        command=["codex", "exec", "Create CSV."],
        env={},
        workspace_dir=tmp_path,
        stdout_text="FINAL REPORT:\nDone",
        stderr_text="",
        output_text="FINAL REPORT:\nDone",
        error_text="",
        exit_code=0,
        timeout_seconds=300,
        stop_reason="process_exit",
        constraint_ledger=None,
    )

    assert evidence["content_hygiene"]["status"] == "pass"
    assert evidence["evidence_result"]["status"] == "pass"


def test_run_evidence_result_fails_invalid_professional_artifact(tmp_path):
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "report.pdf").write_text("not a pdf")

    evidence = build_run_evidence(
        worker={"worker_id": "wrk_invalid_artifact", "profile": "codex-cli", "execution_mode": "host"},
        run_id="run_invalid_artifact",
        runtime_name="codex-cli",
        model="gpt-test",
        command=["codex", "exec", "Create a PDF."],
        env={},
        workspace_dir=tmp_path,
        stdout_text="FINAL REPORT:\nDone",
        stderr_text="",
        output_text="FINAL REPORT:\nDone",
        error_text="",
        exit_code=0,
        timeout_seconds=300,
        stop_reason="process_exit",
        constraint_ledger=None,
    )

    summary = summarize_run_evidence_result(evidence)
    assert evidence["evidence_result"]["status"] == "fail"
    assert summary["status"] == "fail"
    assert any(item["reason"] == "professional artifact validation failed" for item in summary["failure_reasons"])


def test_constraint_ledger_does_not_treat_general_must_include_as_seed_entities():
    ledger = build_constraint_ledger(
        instruction=(
            "Create output/summary.csv and output/table.xlsx. "
            "The CSV and XLSX must include columns name, product, target_sector, score, note "
            "and two rows for Alpha Storage and Beta Grid."
        ),
        worker={"worker_id": "wrk_seed_false_positive", "profile": "codex-cli", "execution_mode": "host"},
        run_id="run_seed_false_positive",
    )

    assert ledger["outputs"]["format_expectations"] == ["xlsx", "csv"]
    assert ledger["seed_entities_or_files"] == []


def test_completion_compliance_does_not_require_attached_input_format_as_output(tmp_path):
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "answer.md").write_text("FINAL REPORT:\nCompared the attached spreadsheet and answered in chat.\n")
    ledger = build_constraint_ledger(
        instruction="Compare the attached CSV and answer in chat. Do not create extra files.",
        worker={"worker_id": "wrk_input_csv", "profile": "codex-cli", "execution_mode": "host"},
        run_id="run_input_csv",
    )

    evidence = build_run_evidence(
        worker={"worker_id": "wrk_input_csv", "profile": "codex-cli", "execution_mode": "host"},
        run_id="run_input_csv",
        runtime_name="codex-cli",
        model="gpt-test",
        command=["codex", "exec", "Compare."],
        env={},
        workspace_dir=tmp_path,
        stdout_text="FINAL REPORT:\nDone",
        stderr_text="",
        output_text="FINAL REPORT:\nDone",
        error_text="",
        exit_code=0,
        timeout_seconds=300,
        stop_reason="process_exit",
        constraint_ledger=ledger,
    )

    assert ledger["outputs"]["format_expectations"] == []
    assert "csv" not in evidence["completion_compliance"]["required_artifact_types"]
    assert evidence["completion_compliance"]["status"] == "pass"
    assert evidence["evidence_result"]["status"] == "pass"


def test_completion_compliance_subtracts_explicitly_forbidden_output_format(tmp_path):
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "answer.md").write_text("FINAL REPORT:\nInline answer only.\n")
    ledger = build_constraint_ledger(
        instruction="Do not create a PDF; answer inline as Markdown text.",
        worker={"worker_id": "wrk_no_pdf", "profile": "codex-cli", "execution_mode": "host"},
        run_id="run_no_pdf",
    )

    evidence = build_run_evidence(
        worker={"worker_id": "wrk_no_pdf", "profile": "codex-cli", "execution_mode": "host"},
        run_id="run_no_pdf",
        runtime_name="codex-cli",
        model="gpt-test",
        command=["codex", "exec", "Answer inline."],
        env={},
        workspace_dir=tmp_path,
        stdout_text="FINAL REPORT:\nDone",
        stderr_text="",
        output_text="FINAL REPORT:\nDone",
        error_text="",
        exit_code=0,
        timeout_seconds=300,
        stop_reason="process_exit",
        constraint_ledger=ledger,
    )

    assert ledger["outputs"]["format_expectations"] == []
    assert "pdf" in ledger["outputs"]["forbidden_format_expectations"]
    assert "pdf" not in evidence["completion_compliance"]["required_artifact_types"]
    assert evidence["completion_compliance"]["status"] == "pass"
    assert evidence["evidence_result"]["status"] == "pass"


def test_completion_compliance_requires_output_format_but_not_uploaded_input_format(tmp_path):
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "brief.pdf").write_bytes(b"%PDF-1.7\n% synthetic\n")
    ledger = build_constraint_ledger(
        instruction="Create a PDF report from the uploaded CSV.",
        worker={"worker_id": "wrk_pdf_from_csv", "profile": "codex-cli", "execution_mode": "host"},
        run_id="run_pdf_from_csv",
    )

    evidence = build_run_evidence(
        worker={"worker_id": "wrk_pdf_from_csv", "profile": "codex-cli", "execution_mode": "host"},
        run_id="run_pdf_from_csv",
        runtime_name="codex-cli",
        model="gpt-test",
        command=["codex", "exec", "Create report."],
        env={},
        workspace_dir=tmp_path,
        stdout_text="FINAL REPORT:\nDone",
        stderr_text="",
        output_text="FINAL REPORT:\nDone",
        error_text="",
        exit_code=0,
        timeout_seconds=300,
        stop_reason="process_exit",
        constraint_ledger=ledger,
    )

    assert evidence["completion_compliance"]["required_artifact_types"] == ["pdf"]
    assert "csv" not in evidence["completion_compliance"]["required_artifact_types"]
    assert evidence["completion_compliance"]["missing_required_artifact_types"] == []


def test_completion_compliance_ignores_seed_section_heading(tmp_path):
    workspace = tmp_path / "workspace"
    output = workspace / "output"
    output.mkdir(parents=True)
    (output / "report.md").write_text("Alpha Capital and Beta Partners are covered.\n")
    ledger = build_constraint_ledger(
        instruction="### SEED FIRM LIST\n(Expand beyond this list.)\nAlpha Capital, Beta Partners\nDeliver a report.",
        worker={"worker_id": "wrk_seed_heading", "profile": "codex-cli", "execution_mode": "host"},
        run_id="run_seed_heading",
    )

    evidence = build_run_evidence(
        worker={"worker_id": "wrk_seed_heading", "profile": "codex-cli", "execution_mode": "host"},
        run_id="run_seed_heading",
        runtime_name="codex-cli",
        model="gpt-test",
        command=["codex", "exec"],
        env={},
        workspace_dir=workspace,
        stdout_text='{"type":"item.completed","item":{"type":"agent_message","text":"FINAL REPORT:\\nDone"}}',
        stderr_text="",
        output_text="FINAL REPORT:\nDone",
        error_text="",
        exit_code=0,
        timeout_seconds=None,
        stop_reason="process_exit",
        constraint_ledger=ledger,
    )

    completion = evidence["completion_compliance"]
    assert completion["seed_entity_coverage"]["missing"] == []
    assert not any("FIRM LIST" in str(issue) for issue in completion["issues"])


def test_completion_compliance_handles_seed_topic_descriptor(tmp_path):
    workspace = tmp_path / "workspace"
    output = workspace / "output"
    output.mkdir(parents=True)
    (output / "brief.md").write_text(
        "Container sandbox controls and local MCP transport auth are both covered.\n"
    )
    ledger = build_constraint_ledger(
        instruction=(
            "Seed topics: container sandbox controls; local MCP transport auth. "
            "Deliver Markdown brief."
        ),
        worker={"worker_id": "wrk_seed_topic", "profile": "codex-cli", "execution_mode": "host"},
        run_id="run_seed_topic",
    )

    evidence = build_run_evidence(
        worker={"worker_id": "wrk_seed_topic", "profile": "codex-cli", "execution_mode": "host"},
        run_id="run_seed_topic",
        runtime_name="codex-cli",
        model="gpt-test",
        command=["codex", "exec"],
        env={},
        workspace_dir=workspace,
        stdout_text='{"type":"item.completed","item":{"type":"agent_message","text":"FINAL REPORT:\\nDone"}}',
        stderr_text="",
        output_text="FINAL REPORT:\nDone",
        error_text="",
        exit_code=0,
        timeout_seconds=None,
        stop_reason="process_exit",
        constraint_ledger=ledger,
    )

    completion = evidence["completion_compliance"]
    assert completion["seed_entity_coverage"]["missing"] == []
    assert completion["status"] == "pass"
    assert evidence["evidence_result"]["status"] == "pass"


def test_completion_compliance_does_not_treat_seed_reference_sentence_as_seed_path(tmp_path):
    workspace = tmp_path / "workspace"
    output = workspace / "output"
    output.mkdir(parents=True)
    (output / "report.md").write_text("Alpha Robotics and Beta Energy are covered.\n")
    (output / "screen.csv").write_text("entity\nAlpha Robotics\nBeta Energy\n")
    ledger = build_constraint_ledger(
        instruction=(
            "Create output/report.md and output/screen.csv. "
            "The report and CSV must mention both seed entities. Do not create a PDF."
        ),
        worker={"worker_id": "wrk_seed_reference", "profile": "codex-cli", "execution_mode": "host"},
        run_id="run_seed_reference",
    )

    evidence = build_run_evidence(
        worker={"worker_id": "wrk_seed_reference", "profile": "codex-cli", "execution_mode": "host"},
        run_id="run_seed_reference",
        runtime_name="codex-cli",
        model="gpt-test",
        command=["codex", "exec"],
        env={},
        workspace_dir=workspace,
        stdout_text='{"type":"item.completed","item":{"type":"agent_message","text":"FINAL REPORT:\\nDone"}}',
        stderr_text="",
        output_text="FINAL REPORT:\nDone",
        error_text="",
        exit_code=0,
        timeout_seconds=None,
        stop_reason="process_exit",
        constraint_ledger=ledger,
    )

    coverage = evidence["completion_compliance"]["seed_entity_coverage"]
    assert coverage["status"] == "not_applicable"
    assert evidence["completion_compliance"]["missing_required_artifact_types"] == []
    assert evidence["evidence_result"]["status"] == "pass"


def test_completion_compliance_keeps_required_formats_when_line_also_forbids_pdf(tmp_path):
    workspace = tmp_path / "workspace"
    output = workspace / "output"
    output.mkdir(parents=True)
    (output / "report.md").write_text("Done.\n")
    (output / "screen.csv").write_text("entity\nAlpha Robotics\n")
    ledger = build_constraint_ledger(
        instruction="Deliver output/report.md and output/screen.csv. Do not create a PDF.",
        worker={"worker_id": "wrk_mixed_formats", "profile": "codex-cli", "execution_mode": "host"},
        run_id="run_mixed_formats",
    )

    evidence = build_run_evidence(
        worker={"worker_id": "wrk_mixed_formats", "profile": "codex-cli", "execution_mode": "host"},
        run_id="run_mixed_formats",
        runtime_name="codex-cli",
        model="gpt-test",
        command=["codex", "exec"],
        env={},
        workspace_dir=workspace,
        stdout_text='{"type":"item.completed","item":{"type":"agent_message","text":"FINAL REPORT:\\nDone"}}',
        stderr_text="",
        output_text="FINAL REPORT:\nDone",
        error_text="",
        exit_code=0,
        timeout_seconds=None,
        stop_reason="process_exit",
        constraint_ledger=ledger,
    )

    completion = evidence["completion_compliance"]
    assert completion["required_artifact_types"] == ["csv", "md"]
    assert completion["missing_required_artifact_types"] == []
    assert "pdf" not in completion["required_artifact_types"]
    assert evidence["evidence_result"]["status"] == "pass"


def test_completion_compliance_keeps_required_format_after_parenthetical_no_pdf(tmp_path):
    workspace = tmp_path / "workspace"
    output = workspace / "output"
    output.mkdir(parents=True)
    (output / "screen.csv").write_text("entity\nAlpha Robotics\n")
    ledger = build_constraint_ledger(
        instruction="Deliver a report (no PDF) in CSV.",
        worker={"worker_id": "wrk_parenthetical_formats", "profile": "codex-cli", "execution_mode": "host"},
        run_id="run_parenthetical_formats",
    )

    evidence = build_run_evidence(
        worker={"worker_id": "wrk_parenthetical_formats", "profile": "codex-cli", "execution_mode": "host"},
        run_id="run_parenthetical_formats",
        runtime_name="codex-cli",
        model="gpt-test",
        command=["codex", "exec"],
        env={},
        workspace_dir=workspace,
        stdout_text='{"type":"item.completed","item":{"type":"agent_message","text":"FINAL REPORT:\\nDone"}}',
        stderr_text="",
        output_text="FINAL REPORT:\nDone",
        error_text="",
        exit_code=0,
        timeout_seconds=None,
        stop_reason="process_exit",
        constraint_ledger=ledger,
    )

    completion = evidence["completion_compliance"]
    assert completion["required_artifact_types"] == ["csv"]
    assert completion["missing_required_artifact_types"] == []
    assert "pdf" not in completion["required_artifact_types"]
    assert evidence["evidence_result"]["status"] == "pass"


def test_constraint_softening_only_scans_planning_files(tmp_path):
    workspace = tmp_path / "workspace"
    output = workspace / "output"
    notes = workspace / "notes"
    output.mkdir(parents=True)
    notes.mkdir(parents=True)
    (output / "report.md").write_text("This is not a broad transformation program; it is a focused result.\n")
    (notes / "plan.md").write_text("Use sources through May 2026 wherever possible.\n")
    ledger = build_constraint_ledger(
        instruction="Use sources through May 2026 only.",
        worker={"worker_id": "wrk_softening", "profile": "codex-cli", "execution_mode": "host"},
        run_id="run_softening",
    )

    evidence = build_run_evidence(
        worker={"worker_id": "wrk_softening", "profile": "codex-cli", "execution_mode": "host"},
        run_id="run_softening",
        runtime_name="codex-cli",
        model="gpt-test",
        command=["codex", "exec"],
        env={},
        workspace_dir=workspace,
        stdout_text='{"type":"item.completed","item":{"type":"agent_message","text":"FINAL REPORT:\\nDone"}}',
        stderr_text="",
        output_text="FINAL REPORT:\nDone",
        error_text="",
        exit_code=0,
        timeout_seconds=None,
        stop_reason="process_exit",
        constraint_ledger=ledger,
    )

    issues = evidence["constraint_compliance"]["issues"]
    assert len(issues) == 1
    assert issues[0]["path"] == "notes/plan.md"


def test_text_artifact_payload_walks_json_leaves_for_hygiene(tmp_path):
    workspace = tmp_path / "workspace"
    research = workspace / "research"
    research.mkdir(parents=True)
    rows = [
        {
            "firm": f"Firm {index}",
            "notes": "normal sourced row",
            "structuralCase": "ordinary narrative profile prose " * 50,
        }
        for index in range(80)
    ]
    (research / "dataset.json").write_text(json.dumps(rows))

    payload = _text_artifact_payload(workspace)
    result = check_content_hygiene(payload)

    assert "research/dataset.json" not in payload
    assert result["status"] == "pass"
