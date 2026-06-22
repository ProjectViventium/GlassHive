from __future__ import annotations

import json
import csv
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Iterable

from .deliverables import (
    ODF_ARTIFACT_EXTENSIONS,
    OLE_ARTIFACT_EXTENSIONS,
    OOXML_ARTIFACT_MARKERS,
    PROFESSIONAL_ARTIFACT_EXTENSIONS,
    candidate_artifact_paths,
    is_user_deliverable_relative_path,
    is_valid_professional_artifact,
)
from .runtime_identity import derive_legacy_backend_label


RUN_EVIDENCE_DIR_NAME = "glasshive-run"
_MAX_TEXT_SCAN_BYTES = 512_000
_MAX_TEXT_SCAN_FILES = 200
_SECRET_KEY_MARKERS = (
    "SECRET",
    "TOKEN",
    "KEY",
    "COOKIE",
    "PASSWORD",
    "PASSWD",
    "PWD",
    "CREDENTIAL",
    "AUTH",
)
_SECRET_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"/Users/[^/\s\"']+"), "~"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{12,}"), r"\1[REDACTED]"),
    (re.compile(r"(?i)((?:api[_-]?key|token|secret|password|passwd|pwd)\s*[:=]\s*)[^\s\"']{6,}"), r"\1[REDACTED]"),
    (re.compile(r"(?i)([?&](?:gh_token|gh_sig|gh_exp|gh_kind)=)([^&#\s\"']+)"), r"\1[REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"), "sk-[REDACTED]"),
    (re.compile(r"\b[A-Za-z0-9_]{8,}:[A-Za-z0-9_./+=-]{20,}\b"), "[REDACTED_CREDENTIAL]"),
)
_FINAL_REPORT_RE = re.compile(r"(?m)^[ \t]*FINAL REPORT:\s*")

_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}
_MONTH_PATTERN = (
    "jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    "aug(?:ust)?|sep(?:t(?:ember)?|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
)
_MONTH_YEAR_RE = re.compile(rf"\b({_MONTH_PATTERN})\s+(\d{{4}})\b", re.IGNORECASE)
_ISO_YEAR_MONTH_RE = re.compile(r"\b(20\d{2})-(0[1-9]|1[0-2])\b")
_DATE_LIMIT_RE = re.compile(
    rf"\b(?:through|until|no later than|ending|ended|before end of|up to|as of|by)\s+({_MONTH_PATTERN})\s+(\d{{4}})\b",
    re.IGNORECASE,
)

_CONTENT_HYGIENE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"skip\s+(?:to\s+)?(?:main\s+)?(?:content|navigation)", re.I), "navigation chrome"),
    (re.compile(r"cookie\s+(?:settings|preferences|policy|notice|banner)", re.I), "cookie banner"),
    (re.compile(r"privacy\s+policy|terms\s+(?:of\s+(?:use|service)|and\s+conditions)", re.I), "legal chrome"),
    (re.compile(r"all\s+rights\s+reserved|(?:read|view)\s+(?:more|all)", re.I), "page chrome"),
    (re.compile(r"please\s+enable\s+(?:java\s*)?script|enable\s+js", re.I), "javascript wall"),
    (re.compile(r"subscribe\s+to\s+continue|sign\s+in\s+to\s+continue|paywall", re.I), "paywall/auth wall"),
    (re.compile(r"are\s+you\s+(?:a\s+)?robot|captcha|bot\s+wall|403\s+forbidden|access\s+denied", re.I), "bot/access wall"),
    (re.compile(r"sourceurl=|__nuxt__|@layer|tailwindcss", re.I), "raw web asset"),
    (re.compile(r"\b(?:window|document)\.[A-Za-z_$]"), "javascript"),
    (re.compile(r"\bfunction\s+(?:[A-Za-z_$][\w$]*)?\s*\([^)]*\)\s*\{", re.I), "javascript"),
    (re.compile(r"\bvar\s+[A-Za-z_$][\w$]*\s*="), "javascript"),
    (re.compile(r"&(?:nbsp|amp|lt|gt|quot|apos);|&#\d+;|&#x[0-9a-f]+;", re.I), "html entity"),
)
_CONTENT_HYGIENE_NEGATIVE_CONTEXT_RE = re.compile(
    r"\b(?:exclude|excludes|excluded|free\s+of|without|no|not)\b[^.\n;]{0,120}$",
    re.I,
)
_TEXT_FILE_SUFFIXES = {".csv", ".json", ".jsonl", ".md", ".txt", ".html", ".htm", ".tsv"}
_CONTENT_HYGIENE_TEXT_SUFFIXES = {".csv", ".json", ".jsonl", ".md", ".txt", ".tsv"}
_HTML_FILE_SUFFIXES = {".html", ".htm"}
_OUTPUT_FORMAT_SUFFIXES = (
    "pdf",
    "xlsx",
    "xls",
    "csv",
    "html",
    "docx",
    "pptx",
    "txt",
    "md",
    "json",
    "tsv",
    "rtf",
    "odt",
    "ods",
    "odp",
)
_OUTPUT_FORMAT_ALIASES = {
    "xlsx": {"xlsx", "xls", "ods"},
    "xls": {"xlsx", "xls", "ods"},
    "csv": {"csv", "tsv"},
    "html": {"html", "htm"},
    "docx": {"docx", "doc", "odt", "rtf"},
    "pptx": {"pptx", "ppt", "odp"},
    "txt": {"txt"},
    "md": {"md"},
}
_OUTPUT_ACTION_RE = re.compile(
    r"\b(deliver|create|generate|produce|write|save|export|output|artifact|artifacts|deliverable|deliverables|report|reports|file|files)\b",
    re.I,
)
_OUTPUT_FORBIDDEN_RE = re.compile(
    r"\b(do\s+not|don't|must\s+not|should\s+not|without|avoid|forbidden|no)\b"
    r"[^.\n;]{0,80}\b(create|generate|produce|write|save|export|output|deliver|artifact|file|report|pdf|xlsx|xls|csv|html|docx|pptx|txt|md|json|tsv)\b",
    re.I,
)
_OUTPUT_FORBIDDEN_CLAUSE_RE = re.compile(
    r"\b(?:do\s+not|don't|must\s+not|should\s+not|without|avoid|forbidden|no)\b[^.\n;)]*",
    re.I,
)
_INPUT_FORMAT_CONTEXT_RE = re.compile(
    r"\b(attached|uploaded|input|source|provided|existing|read|compare|analy[sz]e|from|using|use)\b",
    re.I,
)
_SUPPORT_ARTIFACT_DIRS = {"research", "planning", "specs", "notes"}
_SEED_DESCRIPTOR_WORDS = {
    "companies",
    "entities",
    "examples",
    "firm",
    "files",
    "firms",
    "inputs",
    "items",
    "list",
    "targets",
    "topic",
    "topics",
}
_SKIP_SCAN_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    "chrome-user-data",
    "chromium-user-data",
    RUN_EVIDENCE_DIR_NAME,
    "node_modules",
}


def _now_epoch() -> float:
    return time.time()


def _redact_text(value: object, *, max_chars: int | None = None) -> str:
    text = str(value or "")
    for pattern, replacement in _SECRET_REDACTIONS:
        text = pattern.sub(replacement, text)
    if max_chars is not None and len(text) > max_chars:
        return text[-max_chars:]
    return text


def _redact_command_arg(value: object) -> str:
    text = str(value or "")
    if "\n" in text or len(text) > 600:
        digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]
        return f"[REDACTED_LONG_ARG chars={len(text)} sha256={digest}]"
    return _redact_text(text)


def _stdout_agent_has_final_report(stdout_text: str) -> bool:
    for line in str(stdout_text or "").splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if str(payload.get("type") or "") == "result":
            result_text = str(payload.get("result") or "")
            if _FINAL_REPORT_RE.search(result_text) or "FINAL REPORT:" in result_text:
                return True
        item = payload.get("item")
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "") not in {"agent_message", "assistant_message"}:
            continue
        text = str(item.get("text") or "")
        if _FINAL_REPORT_RE.search(text) or "FINAL REPORT:" in text:
            return True
    return False


def _safe_env_keys(env: dict[str, str] | None) -> list[str]:
    keys: list[str] = []
    for key in sorted((env or {}).keys()):
        upper = key.upper()
        if any(marker in upper for marker in _SECRET_KEY_MARKERS):
            continue
        keys.append(key)
    return keys


def _effective_effort(worker: dict[str, object], command: list[str], env: dict[str, str] | None) -> str:
    configured = str(worker.get("effort") or worker.get("reasoning_effort") or "").strip()
    if configured:
        return configured
    for key in ("WPR_CODEX_CLI_REASONING_EFFORT", "CODEX_REASONING_EFFORT", "WPR_CLAUDE_CODE_EFFORT"):
        value = str((env or {}).get(key) or "").strip()
        if value:
            return value
    for index, part in enumerate(command):
        text = str(part or "").strip()
        if text.startswith("model_reasoning_effort"):
            return text.split("=", 1)[1].strip().strip("\"'")
        if text == "-c" and index + 1 < len(command):
            candidate = str(command[index + 1] or "").strip()
            if candidate.startswith("model_reasoning_effort"):
                return candidate.split("=", 1)[1].strip().strip("\"'")
        if text == "--effort" and index + 1 < len(command):
            return str(command[index + 1] or "").strip()
    return ""


def _derived_legacy_backend(worker: dict[str, object]) -> str:
    return derive_legacy_backend_label(
        profile=worker.get("profile"),
        runtime=worker.get("runtime"),
        backend=worker.get("backend"),
    )


def _lines(text: str) -> list[str]:
    return [line.strip(" \t-*") for line in str(text or "").splitlines() if line.strip(" \t-*")]


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    lower = text.lower()
    return any(needle in lower for needle in needles)


def _bootstrap_seed_files(worker: dict[str, object]) -> list[str]:
    try:
        bundle = json.loads(str(worker.get("bootstrap_bundle_json") or "{}"))
    except json.JSONDecodeError:
        bundle = {}
    if not isinstance(bundle, dict):
        bundle = {}
    files: list[str] = []
    for key in ("files", "input_files", "uploads"):
        raw = bundle.get(key)
        if not isinstance(raw, list):
            continue
        for item in raw:
            if isinstance(item, dict):
                candidate = str(item.get("path") or item.get("name") or item.get("relative_path") or "").strip()
            else:
                candidate = str(item or "").strip()
            if candidate:
                files.append(candidate)
    return files


def build_constraint_ledger(
    *,
    instruction: str,
    worker: dict[str, object],
    run_id: str,
    created_at: float | None = None,
) -> dict[str, object]:
    """Build a generic, conservative ledger from the user's instruction and structured inputs."""

    instruction_text = str(instruction or "")
    source_lines: list[str] = []
    date_lines: list[str] = []
    auth_lines: list[str] = []
    scope_lines: list[str] = []
    exclusion_lines: list[str] = []
    required_output_lines: list[str] = []
    forbidden_output_lines: list[str] = []
    seed_lines: list[str] = []

    for line in _lines(instruction_text):
        lower = line.lower()
        if _contains_any(lower, ("date", "dated", "through", "until", "no later than", "from ", "between ")) or _MONTH_YEAR_RE.search(line):
            date_lines.append(line)
        if _contains_any(lower, ("source", "citation", "primary", "official", "public source", "web source", "evidence")):
            source_lines.append(line)
        if _contains_any(lower, ("auth", "login", "signed", "credential", "token", "session", "account")):
            auth_lines.append(line)
        if _contains_any(lower, ("only", "scope", "in scope", "out of scope", "do not", "must", "exclude", "include", "flag", "local", "cloud")):
            scope_lines.append(line)
        if _contains_any(lower, ("exclude", "do not exclude", "flag", "forbid", "forbidden", "must not", "not allowed")):
            exclusion_lines.append(line)
        if _contains_any(lower, ("seed", "seed file", "seed entity", "input file", "uploaded file")):
            seed_lines.append(line)
        if _line_has_required_output_context(line):
            required_output_lines.append(line)
        if _line_forbids_output(line):
            forbidden_output_lines.extend(_forbidden_output_fragments(line))

    def unique_redacted(values: list[str]) -> list[str]:
        return list(dict.fromkeys(_redact_text(item) for item in values))

    seeds = unique_redacted([*seed_lines, *_bootstrap_seed_files(worker)])
    do_not_widen_or_soften = bool(
        date_lines
        or source_lines
        or any(
            _contains_any(line.lower(), ("only", "do not", "must", "no later than", "through", "until", "scope", "flag"))
            for line in _lines(instruction_text)
        )
    )
    return {
        "schema": "glasshive.run.constraint-ledger.v1",
        "run_id": run_id,
        "created_at": created_at if created_at is not None else _now_epoch(),
        "worker": {
            "worker_id": str(worker.get("worker_id") or ""),
            "profile": str(worker.get("profile") or ""),
            "execution_mode": str(worker.get("execution_mode") or ""),
            "runtime": str(worker.get("runtime") or ""),
            "backend": _derived_legacy_backend(worker),
        },
        "original_request": _redact_text(instruction_text),
        "constraints": {
            "date": unique_redacted(date_lines),
            "source": unique_redacted(source_lines),
            "auth": unique_redacted(auth_lines),
            "scope": unique_redacted(scope_lines),
            "exclusion_or_flag": unique_redacted(exclusion_lines),
        },
        "outputs": {
            "required": unique_redacted(required_output_lines),
            "forbidden": unique_redacted(forbidden_output_lines),
            "format_expectations": _required_output_formats(required_output_lines),
            "forbidden_format_expectations": _output_formats(forbidden_output_lines),
        },
        "seed_entities_or_files": seeds,
        "do_not_widen_or_soften": do_not_widen_or_soften,
    }


def _output_formats(lines: list[str]) -> list[str]:
    found: list[str] = []
    for suffix in _OUTPUT_FORMAT_SUFFIXES:
        if any(re.search(rf"\b{re.escape(suffix)}\b", line, re.I) for line in lines):
            found.append(suffix)
    return found


def _line_forbids_output(line: str) -> bool:
    return bool(_forbidden_output_fragments(line))


def _forbidden_output_fragments(line: str) -> list[str]:
    text = str(line or "")
    fragments: list[str] = []
    for match in _OUTPUT_FORBIDDEN_CLAUSE_RE.finditer(text):
        fragment = match.group(0).strip(" \t\n\r:;,.")
        if not fragment:
            continue
        if _OUTPUT_FORBIDDEN_RE.search(fragment) or _output_formats([fragment]):
            fragments.append(fragment)
    if not fragments:
        fallback = _OUTPUT_FORBIDDEN_RE.search(text)
        if fallback:
            fragments.append(fallback.group(0).strip(" \t\n\r:;,."))
    return list(dict.fromkeys(fragments))


def _line_has_required_output_context(line: str) -> bool:
    text = str(line or "")
    required_fragment = _OUTPUT_FORBIDDEN_CLAUSE_RE.sub(" ", text)
    return bool(_OUTPUT_ACTION_RE.search(required_fragment))


def _format_mentions(line: str, suffix: str) -> Iterable[re.Match[str]]:
    return re.finditer(rf"\b{re.escape(suffix)}\b", str(line or ""), re.I)


def _is_input_format_mention(line: str, match: re.Match[str]) -> bool:
    line_text = str(line or "")
    before = line_text[max(0, match.start() - 60) : match.start()]
    after = line_text[match.end() : min(len(line_text), match.end() + 60)]
    nearby = f"{before} {after}"
    immediate = f"{before[-45:]} {after[:20]}"
    if re.search(r"\b(attached|uploaded|input|source|provided|existing)\b", immediate, re.I):
        return True
    if re.search(r"\b(output|outputs|artifact|artifacts|deliverable|deliverables|report|reports)\b", nearby, re.I):
        return False
    return bool(_INPUT_FORMAT_CONTEXT_RE.search(nearby))


def _is_forbidden_format_mention(line: str, match: re.Match[str]) -> bool:
    start = max(0, match.start() - 80)
    end = min(len(str(line or "")), match.end() + 80)
    return bool(_OUTPUT_FORBIDDEN_RE.search(str(line or "")[start:end]))


def _required_output_formats(lines: list[str]) -> list[str]:
    found: list[str] = []
    for line in lines:
        if not _line_has_required_output_context(line):
            continue
        scan_line = _OUTPUT_FORBIDDEN_CLAUSE_RE.sub(" ", str(line or ""))
        for suffix in _OUTPUT_FORMAT_SUFFIXES:
            for match in _format_mentions(scan_line, suffix):
                if _is_forbidden_format_mention(scan_line, match) or _is_input_format_mention(scan_line, match):
                    continue
                found.append(suffix)
                break
    return list(dict.fromkeys(found))


def write_constraint_ledger(workspace_dir: Path | str, ledger: dict[str, object], run_id: str) -> Path:
    run_dir = Path(workspace_dir) / RUN_EVIDENCE_DIR_NAME
    latest_path = run_dir / "constraint-ledger.json"
    per_run_path = run_dir / "runs" / run_id / "constraint-ledger.json"
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    per_run_path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(ledger, indent=2, sort_keys=True)
    latest_path.write_text(body + "\n")
    per_run_path.write_text(body + "\n")
    return latest_path


def _walk_values(value: object, prefix: str = "") -> Iterable[tuple[str, str]]:
    if isinstance(value, dict):
        for key, nested in value.items():
            next_prefix = f"{prefix}.{key}" if prefix else str(key)
            yield from _walk_values(nested, next_prefix)
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            yield from _walk_values(nested, f"{prefix}[{index}]")
    else:
        yield prefix or "$", str(value or "")


def check_content_hygiene(value: object) -> dict[str, object]:
    issues: list[dict[str, str]] = []
    for path, raw_text in _walk_values(value):
        text = " ".join(raw_text.split())
        if not text:
            continue
        for pattern, reason in _CONTENT_HYGIENE_PATTERNS:
            match = pattern.search(text)
            if not match:
                continue
            if _CONTENT_HYGIENE_NEGATIVE_CONTEXT_RE.search(text[max(0, match.start() - 120) : match.start()]):
                continue
            issues.append(
                {
                    "path": path,
                    "reason": reason,
                    "match": match.group(0),
                    "text": _redact_text(text[:240]),
                }
            )
            break
        structured_path = bool(re.search(r"(\.csv|\.tsv|\.jsonl?|[\].][A-Za-z0-9_-]+)$", path, re.I)) and not re.search(
            r"\.(md|txt)$", path, re.I
        )
        if (
            len(text) > 900
            and structured_path
            and not re.search(
                r"(source|evidence|notes|summary|description|rationale|transcript|narrative|profile|case|pain|"
                r"synthesis|assessment|sequence|angle|hook|recommendation|positioning|outreach|finding)",
                path,
                re.I,
            )
        ):
            issues.append(
                {
                    "path": path,
                    "reason": "overlong structured field",
                    "match": "overlong structured field",
                    "text": _redact_text(text[:240]),
                }
            )
    return {
        "status": "warn" if issues else "pass",
        "failure_count": len(issues),
        "issues": issues[:100],
    }


def _relative_path(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


def _document_validation(path: Path) -> dict[str, object]:
    suffix = path.suffix.lower()
    if suffix not in PROFESSIONAL_ARTIFACT_EXTENSIONS:
        return {"applicable": False, "valid": None, "method": "not_applicable"}
    validation: dict[str, object] = {
        "applicable": True,
        "valid": is_valid_professional_artifact(path),
        "method": "structural",
    }
    if suffix == ".pdf":
        validation["method"] = "pdf_header"
        validation["pdfinfo"] = _pdfinfo_validation(path)
        validation["render_sample"] = _pdf_render_validation(path)
        return validation
    if suffix in OOXML_ARTIFACT_MARKERS:
        validation["method"] = "ooxml_marker"
        validation["required_marker"] = OOXML_ARTIFACT_MARKERS[suffix]
        validation["library_check"] = _ooxml_library_validation(path, suffix)
        return validation
    if suffix in ODF_ARTIFACT_EXTENSIONS:
        validation["method"] = "odf_marker"
        return validation
    if suffix in OLE_ARTIFACT_EXTENSIONS:
        validation["method"] = "ole_magic"
        return validation
    return validation


def _pdfinfo_validation(path: Path) -> dict[str, object]:
    binary = shutil.which("pdfinfo")
    if not binary:
        return {"status": "unavailable"}
    try:
        completed = subprocess.run(
            [binary, str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:  # pragma: no cover - defensive evidence path
        return {"status": "error", "error": _redact_text(exc)}
    output = completed.stdout or completed.stderr or ""
    pages_match = re.search(r"^Pages:\s*(\d+)", output, re.MULTILINE)
    return {
        "status": "pass" if completed.returncode == 0 else "warn",
        "returncode": completed.returncode,
        "pages": int(pages_match.group(1)) if pages_match else None,
        "tail": _redact_text(output, max_chars=1000),
    }


def _pdf_render_validation(path: Path) -> dict[str, object]:
    binary = shutil.which("pdftoppm")
    if not binary:
        return {"status": "unavailable"}
    try:
        with tempfile.TemporaryDirectory(prefix="glasshive-pdf-render-") as temp_dir:
            output_prefix = Path(temp_dir) / "sample"
            sample_path = output_prefix.with_suffix(".png")
            completed = subprocess.run(
                [binary, "-singlefile", "-png", "-f", "1", "-l", "1", str(path), str(output_prefix)],
                check=False,
                capture_output=True,
                text=True,
                timeout=20,
            )
            exists = sample_path.exists()
    except Exception as exc:  # pragma: no cover - defensive evidence path
        return {"status": "error", "error": _redact_text(exc)}
    return {
        "status": "pass" if completed.returncode == 0 and exists else "warn",
        "returncode": completed.returncode,
        "sample_created": exists,
        "tail": _redact_text((completed.stdout or completed.stderr or ""), max_chars=1000),
    }


def _ooxml_library_validation(path: Path, suffix: str) -> dict[str, object]:
    if suffix == ".xlsx":
        try:
            import openpyxl  # type: ignore
        except Exception:
            return {"status": "unavailable", "library": "openpyxl"}
        try:
            workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
            sheet_count = len(workbook.sheetnames)
            workbook.close()
            return {"status": "pass", "library": "openpyxl", "sheet_count": sheet_count}
        except Exception as exc:
            return {"status": "warn", "library": "openpyxl", "error": _redact_text(exc)}
    if suffix == ".docx":
        try:
            import docx  # type: ignore
        except Exception:
            return {"status": "unavailable", "library": "python-docx"}
        try:
            document = docx.Document(str(path))
            return {"status": "pass", "library": "python-docx", "paragraph_count": len(document.paragraphs)}
        except Exception as exc:
            return {"status": "warn", "library": "python-docx", "error": _redact_text(exc)}
    if suffix == ".pptx":
        try:
            import pptx  # type: ignore
        except Exception:
            return {"status": "unavailable", "library": "python-pptx"}
        try:
            presentation = pptx.Presentation(str(path))
            return {"status": "pass", "library": "python-pptx", "slide_count": len(presentation.slides)}
        except Exception as exc:
            return {"status": "warn", "library": "python-pptx", "error": _redact_text(exc)}
    return {"status": "not_applicable"}


def _artifact_inventory(workspace_dir: Path) -> dict[str, object]:
    items: list[dict[str, object]] = []
    for path in candidate_artifact_paths({"workspace_dir": str(workspace_dir)}, max_entries=200):
        try:
            stat = path.stat()
        except OSError:
            continue
        items.append(
            {
                "path": _relative_path(path, workspace_dir),
                "bytes": stat.st_size,
                "mtime": stat.st_mtime,
                "suffix": path.suffix.lower(),
                "document_validation": _document_validation(path),
            }
        )
        if path.suffix.lower() in _HTML_FILE_SUFFIXES:
            items[-1]["html_validation"] = _html_browser_validation(path)
    return {
        "count": len(items),
        "items": items,
    }


def _html_browser_validation(path: Path) -> dict[str, object]:
    enabled = os.environ.get("GLASSHIVE_EVIDENCE_HTML_PLAYWRIGHT", "").strip().lower()
    if enabled not in {"1", "true", "yes", "on"}:
        return {
            "status": "unavailable",
            "reason": "disabled; set GLASSHIVE_EVIDENCE_HTML_PLAYWRIGHT=1",
        }
    node = shutil.which("node")
    if not node:
        return {"status": "unavailable", "reason": "node not found"}
    script = r"""
const target = process.argv[1];
(async () => {
  let chromium;
  try {
    chromium = require("playwright").chromium;
  } catch (error) {
    console.log(JSON.stringify({ status: "unavailable", reason: "playwright not installed" }));
    return;
  }
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  const consoleMessages = [];
  page.on("console", (message) => {
    if (["error", "warning"].includes(message.type())) {
      consoleMessages.push({ type: message.type(), text: message.text().slice(0, 240) });
    }
  });
  page.on("pageerror", (error) => {
    consoleMessages.push({ type: "pageerror", text: String(error).slice(0, 240) });
  });
  await page.goto(target, { waitUntil: "load", timeout: 10000 });
  const bodyText = await page.locator("body").innerText({ timeout: 5000 }).catch(() => "");
  const title = await page.title();
  await browser.close();
  console.log(JSON.stringify({
    status: consoleMessages.some((item) => item.type === "error" || item.type === "pageerror") ? "warn" : "pass",
    title,
    body_chars: bodyText.length,
    console: consoleMessages.slice(0, 20),
  }));
})().catch((error) => {
  console.log(JSON.stringify({ status: "warn", error: String(error).slice(0, 500) }));
  process.exitCode = 1;
});
"""
    try:
        completed = subprocess.run(
            [node, "-e", script, path.resolve().as_uri()],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except Exception as exc:  # pragma: no cover - defensive evidence path
        return {"status": "error", "error": _redact_text(exc)}
    raw = (completed.stdout or completed.stderr or "").strip()
    try:
        payload = json.loads(raw.splitlines()[-1]) if raw else {}
    except json.JSONDecodeError:
        payload = {"status": "warn", "tail": _redact_text(raw, max_chars=1000)}
    payload.setdefault("returncode", completed.returncode)
    return payload


def _visual_render_summary(artifacts: dict[str, object]) -> dict[str, object]:
    items = artifacts.get("items") if isinstance(artifacts, dict) else []
    evidence: list[dict[str, object]] = []
    if not isinstance(items, list):
        return {"items": evidence, "status": "not_applicable"}
    for item in items:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "")
        document_validation = item.get("document_validation")
        if isinstance(document_validation, dict):
            render_sample = document_validation.get("render_sample")
            if isinstance(render_sample, dict) and render_sample.get("status") != "unavailable":
                evidence.append({"path": path, "kind": "pdf_render_sample", **render_sample})
        html_validation = item.get("html_validation")
        if isinstance(html_validation, dict):
            evidence.append({"path": path, "kind": "html_browser_smoke", **html_validation})
    if not evidence:
        return {"items": evidence, "status": "not_available"}
    if any(str(item.get("status") or "").lower() in {"warn", "error"} for item in evidence):
        return {"items": evidence, "status": "warn"}
    if any(str(item.get("status") or "").lower() == "pass" for item in evidence):
        return {"items": evidence, "status": "pass"}
    return {"items": evidence, "status": "unavailable"}


def _text_artifact_payload(workspace_dir: Path) -> dict[str, str]:
    payload: dict[str, str] = {}
    for path in candidate_artifact_paths({"workspace_dir": str(workspace_dir)}, max_entries=100):
        if path.suffix.lower() not in _CONTENT_HYGIENE_TEXT_SUFFIXES:
            continue
        try:
            if path.stat().st_size > _MAX_TEXT_SCAN_BYTES:
                continue
            rel = _relative_path(path, workspace_dir)
            text = path.read_text(encoding="utf-8", errors="ignore")
            suffix = path.suffix.lower()
            if suffix in {".csv", ".tsv"}:
                delimiter = "\t" if suffix == ".tsv" else ","
                try:
                    reader = csv.reader(text.splitlines(), delimiter=delimiter)
                    for row_no, row in enumerate(reader, start=1):
                        if row_no > 1000:
                            break
                        for col_no, cell in enumerate(row, start=1):
                            if col_no > 100:
                                break
                            if str(cell or "").strip():
                                payload[f"{rel}:R{row_no}C{col_no}"] = cell
                    continue
                except csv.Error:
                    payload[rel] = text
                    continue
            if suffix == ".json":
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    payload[rel] = text
                    continue
                for index, (nested_path, value) in enumerate(_walk_values(parsed)):
                    if index >= 1000:
                        break
                    payload[f"{rel}:{nested_path}"] = value
                continue
            if suffix == ".jsonl":
                parsed_any = False
                for line_no, line in enumerate(text.splitlines(), start=1):
                    if line_no > 1000:
                        break
                    if not line.strip():
                        continue
                    try:
                        parsed = json.loads(line)
                    except json.JSONDecodeError:
                        payload[f"{rel}:{line_no}"] = line
                        continue
                    parsed_any = True
                    for nested_path, value in _walk_values(parsed):
                        payload[f"{rel}:{line_no}:{nested_path}"] = value
                if parsed_any:
                    continue
            payload[rel] = text
        except OSError:
            continue
    return payload


def _iter_scan_files(workspace_dir: Path) -> Iterable[Path]:
    count = 0
    for path in workspace_dir.rglob("*"):
        if count >= _MAX_TEXT_SCAN_FILES:
            return
        if not path.is_file() or path.suffix.lower() not in _TEXT_FILE_SUFFIXES:
            continue
        try:
            rel = path.relative_to(workspace_dir)
        except ValueError:
            continue
        lowered_parts = [part.lower() for part in rel.parts]
        if any(part in _SKIP_SCAN_DIRS for part in lowered_parts):
            continue
        if not is_user_deliverable_relative_path(rel) and rel.parts[0].lower() not in {"research", "planning", "specs", "notes"}:
            continue
        try:
            if path.stat().st_size > _MAX_TEXT_SCAN_BYTES:
                continue
        except OSError:
            continue
        count += 1
        yield path


def _extract_latest_date_limit(ledger: dict[str, object]) -> tuple[int, int] | None:
    fragments: list[str] = []
    request = str(ledger.get("original_request") or "")
    fragments.append(request)
    constraints = ledger.get("constraints")
    if isinstance(constraints, dict):
        for value in constraints.values():
            if isinstance(value, list):
                fragments.extend(str(item) for item in value)
    limit: tuple[int, int] | None = None
    for fragment in fragments:
        for match in _DATE_LIMIT_RE.finditer(fragment):
            month = _MONTHS.get(match.group(1).lower()[:3]) or _MONTHS.get(match.group(1).lower())
            year = int(match.group(2))
            if month:
                candidate = (year, month)
                if limit is None or candidate > limit:
                    limit = candidate
    return limit


def _month_year_mentions(text: str) -> Iterable[tuple[int, int, str]]:
    for match in _MONTH_YEAR_RE.finditer(text):
        month = _MONTHS.get(match.group(1).lower()[:3]) or _MONTHS.get(match.group(1).lower())
        if month:
            yield int(match.group(2)), month, match.group(0)
    for match in _ISO_YEAR_MONTH_RE.finditer(text):
        yield int(match.group(1)), int(match.group(2)), match.group(0)


_REJECTED_OR_OUT_OF_SCOPE_RE = re.compile(
    r"\b("
    r"rejected|discarded|out[- ]of[- ]scope|outside scope|excluded from evidence|not used|do not use|"
    r"not relied upon|not supporting|does not support|removed from (?:facts|scoring|deliverables)"
    r")\b",
    re.I,
)
_CONSTRAINT_PLANNING_DIRS = {"research", "planning", "specs", "notes"}
_CONSTRAINT_PLANNING_NAMES = {"spec", "plan", "prompt", "subagent", "delegation", "constraint", "ledger"}
_SOFTENING_RE = re.compile(r"\b(wherever possible|if possible|best effort|when available|roughly|approximately)\b", re.I)
_CONSTRAINT_CONTEXT_RE = re.compile(
    r"\b(constraint|must|only|through|until|no later than|required|forbidden|exclude|flag)\b",
    re.I,
)


def _line_for_span(text: str, start: int, end: int) -> str:
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    if line_end == -1:
        line_end = len(text)
    return text[line_start:line_end]


def _month_year_mentions_with_context(text: str) -> Iterable[tuple[int, int, str, str]]:
    for match in _MONTH_YEAR_RE.finditer(text):
        month = _MONTHS.get(match.group(1).lower()[:3]) or _MONTHS.get(match.group(1).lower())
        if month:
            yield int(match.group(2)), month, match.group(0), _line_for_span(text, match.start(), match.end())
    for match in _ISO_YEAR_MONTH_RE.finditer(text):
        yield int(match.group(1)), int(match.group(2)), match.group(0), _line_for_span(text, match.start(), match.end())


def _is_rejected_or_out_of_scope_line(line: str) -> bool:
    return bool(_REJECTED_OR_OUT_OF_SCOPE_RE.search(line))


def _line_uses_date_as_source_evidence(line: str, mention: str) -> bool:
    escaped = re.escape(str(mention or ""))
    if not escaped:
        return False
    text = str(line or "")
    source_word = r"(?:source|sources|sourced|data source|citation|cited|evidence|dataset|primary source)"
    date_action = r"(?:published|dated|accessed|released|filed|retrieved)"
    patterns = (
        rf"\b{source_word}\b[^.\n;]{{0,80}}\b{date_action}\b[^.\n;]{{0,40}}\b{escaped}\b",
        rf"\b{date_action}\b[^.\n;]{{0,40}}\b{escaped}\b",
    )
    return any(re.search(pattern, text, re.I) for pattern in patterns)


def _is_constraint_planning_file(rel_path: str) -> bool:
    path = Path(rel_path)
    lowered_parts = [part.lower() for part in path.parts]
    stem = path.stem.lower()
    return bool(
        any(part in _CONSTRAINT_PLANNING_DIRS for part in lowered_parts)
        or any(token in stem for token in _CONSTRAINT_PLANNING_NAMES)
    )


def _softening_lines(text: str, *, rel_path: str) -> list[str]:
    lines: list[str] = []
    if not _is_constraint_planning_file(rel_path):
        return lines
    for line in text.splitlines():
        if not _SOFTENING_RE.search(line):
            continue
        if _CONSTRAINT_CONTEXT_RE.search(line):
            lines.append(line.strip())
    return lines


def _constraint_compliance(workspace_dir: Path, ledger: dict[str, object] | None) -> dict[str, object]:
    if not ledger:
        return {"status": "not_available", "issues": []}
    issues: list[dict[str, str]] = []
    latest_limit = _extract_latest_date_limit(ledger)
    strict = bool(ledger.get("do_not_widen_or_soften"))
    for path in _iter_scan_files(workspace_dir):
        rel = _relative_path(path, workspace_dir)
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if latest_limit:
            for year, month, mention, line in _month_year_mentions_with_context(text):
                if (
                    (year, month) > latest_limit
                    and _line_uses_date_as_source_evidence(line, mention)
                    and not _is_rejected_or_out_of_scope_line(line)
                ):
                    issues.append(
                        {
                            "path": rel,
                            "reason": "date/source window widened past ledger limit",
                            "text": _redact_text(line.strip() or mention),
                        }
                    )
        if strict:
            for line in _softening_lines(text, rel_path=rel):
                issues.append(
                    {
                        "path": rel,
                        "reason": "strict constraint softened in workspace file",
                        "text": _redact_text(line, max_chars=240),
                    }
                )
        if len(issues) >= 100:
            break
    return {
        "status": "fail" if issues else "pass",
        "issues": issues,
    }


def _required_artifact_types(ledger: dict[str, object] | None) -> list[str]:
    if not ledger:
        return []
    outputs = ledger.get("outputs")
    if not isinstance(outputs, dict):
        return []
    required: list[str] = []
    raw_formats = outputs.get("format_expectations")
    if isinstance(raw_formats, list):
        required.extend(str(item or "").strip().lower().lstrip(".") for item in raw_formats)
    raw_lines = outputs.get("required")
    if isinstance(raw_lines, list):
        required.extend(_required_output_formats([str(item or "") for item in raw_lines]))
    forbidden: list[str] = []
    raw_forbidden_formats = outputs.get("forbidden_format_expectations")
    if isinstance(raw_forbidden_formats, list):
        forbidden.extend(str(item or "").strip().lower().lstrip(".") for item in raw_forbidden_formats)
    raw_forbidden_lines = outputs.get("forbidden")
    if isinstance(raw_forbidden_lines, list):
        forbidden.extend(_output_formats([str(item or "") for item in raw_forbidden_lines]))
    forbidden_aliases: set[str] = set()
    for item in forbidden:
        forbidden_aliases.update(_format_aliases(item))
    return [item for item in dict.fromkeys(required) if item and not (_format_aliases(item) & forbidden_aliases)]


def _format_aliases(format_name: str) -> set[str]:
    normalized = str(format_name or "").strip().lower().lstrip(".")
    return _OUTPUT_FORMAT_ALIASES.get(normalized, {normalized})


def _delivered_artifact_types(artifacts: dict[str, object]) -> list[str]:
    items = artifacts.get("items") if isinstance(artifacts, dict) else []
    delivered: list[str] = []
    if not isinstance(items, list):
        return delivered
    for item in items:
        if not isinstance(item, dict):
            continue
        suffix = str(item.get("suffix") or "").strip().lower().lstrip(".")
        if suffix:
            delivered.append(suffix)
    return sorted(dict.fromkeys(delivered))


def _is_support_artifact_path(path: str) -> bool:
    parts = Path(str(path or "")).parts
    return bool(parts and parts[0].lower() in _SUPPORT_ARTIFACT_DIRS)


def _artifact_path_summary(artifacts: dict[str, object]) -> dict[str, object]:
    items = artifacts.get("items") if isinstance(artifacts, dict) else []
    if not isinstance(items, list):
        items = []
    support_paths: list[str] = []
    deliverable_paths: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        rel_path = str(item.get("path") or "")
        if _is_support_artifact_path(rel_path):
            support_paths.append(rel_path)
        else:
            deliverable_paths.append(rel_path)
    return {
        "support_artifact_count": len(support_paths),
        "deliverable_artifact_count": len(deliverable_paths),
        "support_artifact_paths": support_paths[:50],
        "deliverable_artifact_paths": deliverable_paths[:50],
        "notes_only": bool(items) and not deliverable_paths and bool(support_paths),
    }


def _clean_seed_term(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip(" \t\n\r:;,.()[]{}")
    text = re.sub(
        r"(?i)\b(?:in|with|for)\s+(?:the\s+)?(?:final\s+)?(?:report|output|artifact|deliverable)s?.*$",
        "",
        text,
    ).strip(" \t\n\r:;,.()[]{}")
    words = text.split()
    if words and words[0].lower().strip(":;,.") in _SEED_DESCRIPTOR_WORDS:
        text = " ".join(words[1:]).strip(" \t\n\r:;,.()[]{}")
    return text


def _seed_terms_from_line(line: str) -> list[str]:
    text = re.sub(r"\s+", " ", str(line or "")).strip()
    if not text:
        return []
    candidates: list[str] = []
    quoted = re.findall(r"[\"']([^\"']{2,80})[\"']", text)
    candidates.extend(quoted)

    body = ""
    seed_match = re.search(r"(?i)\bseed\b\s*(.*)$", text)
    if seed_match:
        body = seed_match.group(1)
    else:
        include_match = re.search(r"(?i)\b(?:must\s+)?(?:include|cover)\b\s*(.*)$", text)
        if include_match:
            body = include_match.group(1)
    if body:
        body = re.split(r"(?i)\b(?:deliver|create|output|artifact|artifacts|report|reports|source|sources)\b", body, maxsplit=1)[0]
        body = body.split(".", 1)[0]
        for part in re.split(r"\s*(?:,|;|\band\b)\s*", body):
            cleaned = _clean_seed_term(part)
            if 2 <= len(cleaned) <= 80 and len(cleaned.split()) <= 8:
                candidates.append(cleaned)

    if not candidates and ("/" in text or "\\" in text):
        if re.search(r"\b(?:seed|input|uploaded|attached|source)\s+file\b", text, re.I):
            for token in re.findall(r"[^\s,;:]+[/\\][^\s,;:]+", text):
                path = Path(token.strip(" \t\n\r:;,.()[]{}\"'"))
                if path.name:
                    candidates.append(path.name)
                    if path.stem and path.stem != path.name:
                        candidates.append(path.stem)

    terms: list[str] = []
    for candidate in candidates:
        cleaned = _clean_seed_term(candidate)
        if not cleaned:
            continue
        lower = cleaned.lower()
        if lower in _SEED_DESCRIPTOR_WORDS or lower in {"seed", "include", "cover"}:
            continue
        terms.append(cleaned)
    return list(dict.fromkeys(terms))


def _seed_terms_from_ledger(ledger: dict[str, object] | None) -> list[str]:
    if not ledger:
        return []
    raw = ledger.get("seed_entities_or_files")
    if not isinstance(raw, list):
        return []
    terms: list[str] = []
    for item in raw:
        terms.extend(_seed_terms_from_line(str(item or "")))
    return list(dict.fromkeys(terms))


def _term_in_text(term: str, text: str) -> bool:
    normalized_term = " ".join(str(term or "").lower().split())
    normalized_text = " ".join(str(text or "").lower().split())
    return bool(normalized_term and normalized_term in normalized_text)


def _seed_entity_coverage(workspace_dir: Path, ledger: dict[str, object] | None, output_text: str) -> dict[str, object]:
    terms = _seed_terms_from_ledger(ledger)
    if not terms:
        return {
            "status": "not_applicable",
            "total": 0,
            "mentioned_count": 0,
            "missing": [],
            "scanned_file_count": 0,
        }
    fragments = [str(output_text or "")]
    scanned_paths: list[str] = []
    for path in _iter_scan_files(workspace_dir):
        try:
            fragments.append(path.read_text(encoding="utf-8", errors="ignore"))
            scanned_paths.append(_relative_path(path, workspace_dir))
        except OSError:
            continue
    combined = "\n".join(fragments)
    mentioned = [term for term in terms if _term_in_text(term, combined)]
    missing = [term for term in terms if term not in mentioned]
    return {
        "status": "pass" if not missing else "warn",
        "total": len(terms),
        "mentioned_count": len(mentioned),
        "missing": [_redact_text(term) for term in missing[:50]],
        "scanned_file_count": len(scanned_paths),
        "scanned_paths": scanned_paths[:50],
    }


def _completion_compliance(
    workspace_dir: Path,
    artifacts: dict[str, object],
    ledger: dict[str, object] | None,
    *,
    has_final_report: bool,
    output_text: str,
) -> dict[str, object]:
    required = _required_artifact_types(ledger)
    delivered = _delivered_artifact_types(artifacts)
    delivered_set = set(delivered)
    missing = [fmt for fmt in required if not (_format_aliases(fmt) & delivered_set)]
    path_summary = _artifact_path_summary(artifacts)
    seed_coverage = _seed_entity_coverage(workspace_dir, ledger, output_text)
    notes_only = bool(path_summary["notes_only"])
    issues: list[dict[str, object]] = []
    if not has_final_report:
        issues.append({"reason": "final report marker missing"})
    if missing:
        issues.append(
            {
                "reason": "required artifact types missing",
                "missing_required_artifact_types": missing,
            }
        )
    if notes_only and (required or not has_final_report):
        issues.append({"reason": "only support notes/planning artifacts were found"})
    if seed_coverage.get("status") == "warn":
        issues.append(
            {
                "reason": "seed entities/files were not all found in scanned outputs",
                "missing": seed_coverage.get("missing") or [],
            }
        )

    fail_reasons = {"final report marker missing", "required artifact types missing", "only support notes/planning artifacts were found"}
    status = "pass"
    if any(str(issue.get("reason") or "") in fail_reasons for issue in issues):
        status = "fail"
    elif issues:
        status = "warn"
    return {
        "status": status,
        "required_artifact_types": required,
        "delivered_artifact_types": delivered,
        "missing_required_artifact_types": missing,
        "notes_only": notes_only,
        **path_summary,
        "seed_entity_coverage": seed_coverage,
        "issues": issues[:100],
    }


def _invalid_professional_artifacts(artifacts: dict[str, object]) -> list[dict[str, object]]:
    items = artifacts.get("items") if isinstance(artifacts, dict) else []
    if not isinstance(items, list):
        return []
    invalid: list[dict[str, object]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        suffix = str(item.get("suffix") or "").lower()
        if suffix not in PROFESSIONAL_ARTIFACT_EXTENSIONS:
            continue
        validation = item.get("document_validation")
        if not isinstance(validation, dict):
            continue
        if validation.get("valid") is False:
            invalid.append(
                {
                    "path": _redact_text(item.get("path") or ""),
                    "suffix": suffix,
                    "reason": _redact_text(validation.get("reason") or validation.get("library_check") or "invalid document"),
                }
            )
    return invalid


def summarize_run_evidence_result(evidence: dict[str, object]) -> dict[str, object]:
    """Summarize generic evidence checks for user-visible status/failure handling."""
    failure_reasons: list[dict[str, object]] = []
    warning_reasons: list[dict[str, object]] = []

    final_output = evidence.get("final_output")
    if isinstance(final_output, dict) and not final_output.get("has_final_report"):
        failure_reasons.append({"reason": "final report marker missing"})

    completion = evidence.get("completion_compliance")
    if isinstance(completion, dict):
        completion_status = str(completion.get("status") or "").lower()
        issues = completion.get("issues") if isinstance(completion.get("issues"), list) else []
        if completion_status == "fail":
            failure_reasons.append({"reason": "completion compliance failed", "issues": issues[:10]})
        elif completion_status == "warn":
            warning_reasons.append({"reason": "completion compliance warning", "issues": issues[:10]})

    constraint = evidence.get("constraint_compliance")
    if isinstance(constraint, dict):
        constraint_status = str(constraint.get("status") or "").lower()
        issues = constraint.get("issues") if isinstance(constraint.get("issues"), list) else []
        if constraint_status == "fail":
            failure_reasons.append({"reason": "constraint compliance failed", "issues": issues[:10]})
        elif constraint_status == "warn":
            warning_reasons.append({"reason": "constraint compliance warning", "issues": issues[:10]})

    invalid_artifacts = _invalid_professional_artifacts(
        evidence.get("artifacts") if isinstance(evidence.get("artifacts"), dict) else {}
    )
    if invalid_artifacts:
        failure_reasons.append(
            {
                "reason": "professional artifact validation failed",
                "artifacts": invalid_artifacts[:20],
            }
        )

    hygiene = evidence.get("content_hygiene")
    if isinstance(hygiene, dict) and str(hygiene.get("status") or "").lower() in {"warn", "fail"}:
        warning_reasons.append(
            {
                "reason": "content hygiene warning",
                "failure_count": hygiene.get("failure_count"),
                "items": (hygiene.get("items") if isinstance(hygiene.get("items"), list) else [])[:10],
            }
        )

    status = "pass"
    if failure_reasons:
        status = "fail"
    elif warning_reasons:
        status = "warn"
    return {
        "status": status,
        "failure_reasons": failure_reasons[:20],
        "warning_reasons": warning_reasons[:20],
    }


def build_run_evidence(
    *,
    worker: dict[str, object],
    run_id: str,
    runtime_name: str,
    model: str,
    command: list[str],
    env: dict[str, str] | None,
    workspace_dir: Path | str,
    stdout_text: str,
    stderr_text: str,
    output_text: str,
    error_text: str,
    exit_code: int | None,
    timeout_seconds: float | None,
    stop_reason: str,
    constraint_ledger: dict[str, object] | None,
    started_at: float | None = None,
    ended_at: float | None = None,
    transcript_paths: dict[str, str] | None = None,
) -> dict[str, object]:
    workspace = Path(workspace_dir)
    artifacts = _artifact_inventory(workspace)
    normalized_stop_reason = str(stop_reason or "").strip()
    if normalized_stop_reason == "timeout":
        exit_source = "timeout"
    elif exit_code is not None:
        exit_source = "process"
    else:
        exit_source = normalized_stop_reason or "unknown"
    has_final_report = bool(
        _FINAL_REPORT_RE.search(output_text)
        or "FINAL REPORT:" in str(output_text or "")
        or _stdout_agent_has_final_report(stdout_text)
    )
    final_output = {
        "has_final_report": has_final_report,
        "output_chars": len(output_text or ""),
        "error_present": bool(str(error_text or "").strip()),
        "status": "ok" if exit_code == 0 and not str(error_text or "").strip() else "failed",
    }
    evidence: dict[str, object] = {
        "schema": "glasshive.run.evidence.v1",
        "run_id": run_id,
        "created_at": _now_epoch(),
        "started_at": started_at,
        "ended_at": ended_at if ended_at is not None else _now_epoch(),
        "worker": {
            "worker_id": str(worker.get("worker_id") or ""),
            "profile": str(worker.get("profile") or ""),
            "execution_mode": str(worker.get("execution_mode") or ""),
            "runtime": str(worker.get("runtime") or ""),
            "backend": _derived_legacy_backend(worker),
        },
        "runtime": runtime_name,
        "model": model,
        "effort": _effective_effort(worker, command, env),
        "command": {
            "argv_redacted": [_redact_command_arg(part) for part in command],
            "display_redacted": " ".join(_redact_command_arg(part) for part in command),
        },
        "env_keys": _safe_env_keys(env),
        "redacted_env_key_count": len((env or {}).keys()) - len(_safe_env_keys(env)),
        "timeout": {
            "seconds": timeout_seconds,
            "exit_source": exit_source,
            "stop_reason": normalized_stop_reason,
        },
        "transcript": {
            "paths": transcript_paths or {},
            "stdout_tail": _redact_text(stdout_text, max_chars=4000),
            "stderr_tail": _redact_text(stderr_text, max_chars=4000),
        },
        "artifacts": artifacts,
        "visual_render_evidence": _visual_render_summary(artifacts),
        "content_hygiene": check_content_hygiene(_text_artifact_payload(workspace)),
        "constraint_compliance": _constraint_compliance(workspace, constraint_ledger),
        "completion_compliance": _completion_compliance(
            workspace,
            artifacts,
            constraint_ledger,
            has_final_report=has_final_report,
            output_text=output_text,
        ),
        "final_output": final_output,
        "exit_code": exit_code,
    }
    evidence["evidence_result"] = summarize_run_evidence_result(evidence)
    return evidence


def write_run_evidence(workspace_dir: Path | str, evidence: dict[str, object], run_id: str) -> Path:
    run_dir = Path(workspace_dir) / RUN_EVIDENCE_DIR_NAME
    latest_path = run_dir / "evidence.json"
    per_run_path = run_dir / "runs" / run_id / "evidence.json"
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    per_run_path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(evidence, indent=2, sort_keys=True)
    latest_path.write_text(body + "\n")
    per_run_path.write_text(body + "\n")
    return latest_path
