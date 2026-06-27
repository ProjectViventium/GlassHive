from __future__ import annotations

import os
import re
import zipfile
from pathlib import Path
from urllib.parse import quote


SANDBOX_WORKSPACE_MOUNT = Path(os.environ.get("WPR_SANDBOX_WORKSPACE", "/workspace/project"))
LOCALHOST_URL_PATTERN = re.compile(
    r"://(?:127\.0\.0\.1|localhost|0\.0\.0\.0)(?:[:/]|$)",
    flags=re.IGNORECASE,
)
NON_DELIVERABLE_URL_HOST_PATTERN = re.compile(
    r"(^|\.)(api\.openai\.com|api\.anthropic\.com|api\.portkey\.ai|openai\.azure\.com|cognitiveservices\.azure\.com|services\.ai\.azure\.com)$",
    flags=re.IGNORECASE,
)
NON_DELIVERABLE_URL_PATH_PATTERN = re.compile(
    r"/(?:openai|anthropic|chat/completions|responses)(?:/|$)",
    flags=re.IGNORECASE,
)
NON_DELIVERABLE_FILE_NAMES = {
    ".mcp.json",
    "account web data",
    "account web data-journal",
    "agents.md",
    "claude.md",
    "codex.md",
    "chrome-default-cookies.sqlite",
    "cookies",
    "cookies-journal",
    "harness-prompt.md",
    "local state",
    "login data",
    "login data-journal",
    "project-definition.md",
    "web data",
    "web data-journal",
    "work-log.md",
}
NON_DELIVERABLE_DIR_NAMES = {
    ".codex",
    ".git",
    ".glasshive",
    ".venv",
    "__pycache__",
    "chrome-user-data",
    "chromium-user-data",
    "glasshive-run",
    "glasshive-host-tools",
    "node_modules",
}
NON_DELIVERABLE_PATH_PREFIXES = {
    ("scheduled-prompt",),
    ("tmp",),
    ("uploads",),
}
USER_DELIVERABLE_DIR_PRIORITY = {
    "artifacts": 0,
    "reports": 1,
    "output": 2,
}
SUPPORT_ARTIFACT_DIR_NAMES = {"research", "planning", "specs", "notes"}
PROFESSIONAL_ARTIFACT_EXTENSIONS = {
    ".doc",
    ".docx",
    ".odp",
    ".ods",
    ".odt",
    ".pdf",
    ".ppt",
    ".pptx",
    ".rtf",
    ".xls",
    ".xlsx",
}

OOXML_ARTIFACT_MARKERS = {
    ".docx": "word/document.xml",
    ".pptx": "ppt/presentation.xml",
    ".xlsx": "xl/workbook.xml",
}
ODF_ARTIFACT_EXTENSIONS = {".odp", ".ods", ".odt"}
OLE_ARTIFACT_EXTENSIONS = {".doc", ".ppt", ".xls"}
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def is_user_deliverable_relative_path(relative_path: Path | str) -> bool:
    try:
        rel = Path(str(relative_path))
    except TypeError:
        return False
    if not rel.parts or rel.is_absolute():
        return False
    parts = [part for part in rel.parts if part]
    if any(part in {".", ".."} for part in parts):
        return False
    lowered_parts = [part.lower() for part in parts]
    if any(part in NON_DELIVERABLE_DIR_NAMES for part in lowered_parts):
        return False
    if any(tuple(lowered_parts[: len(prefix)]) == prefix for prefix in NON_DELIVERABLE_PATH_PREFIXES):
        return False
    if any(part.startswith(".") for part in lowered_parts):
        return False
    return rel.name.lower() not in NON_DELIVERABLE_FILE_NAMES


def candidate_html_paths(worker: dict, max_entries: int = 20) -> list[Path]:
    raw_root = str(worker.get("workspace_dir") or "").strip()
    if not raw_root:
        return []
    root = Path(raw_root)
    if not root.exists():
        return []
    candidates: list[Path] = []
    for path in sorted(root.rglob("*.html")):
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        try:
            if path.stat().st_size <= 0:
                continue
        except OSError:
            continue
        if not is_user_deliverable_relative_path(rel):
            continue
        candidates.append(path)
        if len(candidates) >= max_entries:
            break
    return candidates


def candidate_artifact_paths(worker: dict, max_entries: int = 50) -> list[Path]:
    raw_root = str(worker.get("workspace_dir") or "").strip()
    if not raw_root:
        return []
    root = Path(raw_root)
    if not root.exists():
        return []
    candidates: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        try:
            if path.stat().st_size <= 0:
                continue
        except OSError:
            continue
        if not is_user_deliverable_relative_path(rel):
            continue
        candidates.append(path)

    def priority(path: Path) -> tuple[int, float, str]:
        try:
            rel = path.relative_to(root)
        except ValueError:
            return (99, -path.stat().st_mtime, path.as_posix())
        first_part = rel.parts[0].lower() if rel.parts else ""
        directory_priority = USER_DELIVERABLE_DIR_PRIORITY.get(first_part)
        if directory_priority is None:
            directory_priority = 3 if len(rel.parts) == 1 else 4
        return (directory_priority, -path.stat().st_mtime, rel.as_posix())

    return sorted(candidates, key=priority)[:max_entries]


def is_valid_professional_artifact(path: Path) -> bool:
    suffix = path.suffix.lower()
    if suffix not in PROFESSIONAL_ARTIFACT_EXTENSIONS:
        return False
    try:
        if not path.is_file() or path.stat().st_size <= 0:
            return False
        with path.open("rb") as handle:
            header = handle.read(16)
    except OSError:
        return False
    if suffix == ".pdf":
        return header.startswith(b"%PDF-")
    if suffix == ".rtf":
        return header.startswith(b"{\\rtf")
    if suffix in OLE_ARTIFACT_EXTENSIONS:
        return header.startswith(OLE_MAGIC)
    if suffix in OOXML_ARTIFACT_MARKERS:
        try:
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
        except (OSError, zipfile.BadZipFile):
            return False
        return "[Content_Types].xml" in names and OOXML_ARTIFACT_MARKERS[suffix] in names
    if suffix in ODF_ARTIFACT_EXTENSIONS:
        try:
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
        except (OSError, zipfile.BadZipFile):
            return False
        return "mimetype" in names and "content.xml" in names
    return True


def workspace_browser_url(path: Path, worker: dict) -> str | None:
    raw_root = str(worker.get("workspace_dir") or "").strip()
    if not raw_root:
        return None
    root = Path(raw_root)
    try:
        rel = path.relative_to(root)
    except ValueError:
        return None
    if str(worker.get("execution_mode") or "docker") == "host":
        return None
    container_path = (SANDBOX_WORKSPACE_MOUNT / rel.as_posix()).as_posix()
    return f"file://{quote(container_path, safe='/:')}"


def extract_urls(*texts: str) -> list[str]:
    combined = "\n".join(texts)
    seen: set[str] = set()
    matches: list[str] = []
    for raw in re.findall(r"https?://[^\s<>'\"`]+", combined):
        cleaned = raw.rstrip(").,;")
        if cleaned in seen:
            continue
        seen.add(cleaned)
        matches.append(cleaned)
    return matches


def is_deliverable_url(url: str) -> bool:
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
    except ValueError:
        return False
    if NON_DELIVERABLE_URL_HOST_PATTERN.search(host):
        return False
    if NON_DELIVERABLE_URL_PATH_PATTERN.search(parsed.path or "") and (
        "azure" in host.lower() or "openai" in host.lower() or "anthropic" in host.lower()
    ):
        return False
    return True


def deliverable_payload(
    worker: dict,
    latest_run: dict | None,
    latest_output: str,
    stdout_text: str = "",
    stderr_text: str = "",
) -> dict[str, object] | None:
    execution_mode = str(worker.get("execution_mode") or "docker")
    artifact_candidates = candidate_artifact_paths(worker)
    valid_artifact_candidates = [
        path
        for path in artifact_candidates
        if path.suffix.lower() not in PROFESSIONAL_ARTIFACT_EXTENSIONS
        or is_valid_professional_artifact(path)
    ]
    preferred_artifact = next(
        (path for path in valid_artifact_candidates if path.suffix.lower() in PROFESSIONAL_ARTIFACT_EXTENSIONS),
        None,
    )
    if preferred_artifact is not None:
        raw_root = str(worker.get("workspace_dir") or "").strip()
        try:
            rel = preferred_artifact.relative_to(Path(raw_root))
        except ValueError:
            rel = Path(preferred_artifact.name)
        return {
            "kind": "file",
            "state": "ready" if latest_run else "available",
            "source": "workspace_file",
            "label": preferred_artifact.name,
            "preferred_surface": "download",
            "workspace_path": rel.as_posix(),
        }

    html_candidates = candidate_html_paths(worker)
    preferred_html = next((path for path in html_candidates if path.name.lower() == "index.html"), None)
    if preferred_html is None and html_candidates:
        preferred_html = html_candidates[0]

    urls = [url for url in extract_urls(latest_output, stdout_text, stderr_text) if is_deliverable_url(url)]
    external_url = next((url for url in urls if not LOCALHOST_URL_PATTERN.search(url)), None)
    local_url = next((url for url in urls if LOCALHOST_URL_PATTERN.search(url)), None)

    if preferred_html is not None:
        browser_url = workspace_browser_url(preferred_html, worker)
        raw_root = str(worker.get("workspace_dir") or "").strip()
        try:
            workspace_path = preferred_html.relative_to(Path(raw_root)).as_posix() if raw_root else preferred_html.name
        except ValueError:
            workspace_path = preferred_html.name
        payload: dict[str, object] = {
            "kind": "webpage",
            "state": "ready" if latest_run else "available",
            "source": "workspace_html",
            "label": preferred_html.name,
            "preferred_surface": "desktop",
            "workspace_path": workspace_path,
        }
        if browser_url:
            payload["browser_url"] = browser_url
        else:
            payload["browser_url_available"] = False
        return payload

    if valid_artifact_candidates:
        artifact = valid_artifact_candidates[0]
        raw_root = str(worker.get("workspace_dir") or "").strip()
        try:
            rel = artifact.relative_to(Path(raw_root))
        except ValueError:
            rel = Path(artifact.name)
        return {
            "kind": "file",
            "state": "ready" if latest_run else "available",
            "source": "workspace_file",
            "label": artifact.name,
            "preferred_surface": "download",
            "workspace_path": rel.as_posix(),
        }

    if local_url or external_url:
        if execution_mode == "host":
            return {
                "kind": "webpage",
                "state": "ready" if latest_run else "available",
                "source": "run_url",
                "label": "URL output available",
                "preferred_surface": "desktop",
                "workspace_path": None,
                "browser_url_available": False,
            }
        browser_url = local_url or external_url
        return {
            "kind": "webpage",
            "state": "ready" if latest_run else "available",
            "source": "run_url",
            "label": browser_url,
            "browser_url": browser_url,
            "preferred_surface": "desktop",
            "workspace_path": None,
        }

    return None
