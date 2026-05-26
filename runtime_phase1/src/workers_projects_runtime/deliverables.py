from __future__ import annotations

import os
import re
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
    "agents.md",
    "claude.md",
    "codex.md",
    "harness-prompt.md",
    "project-definition.md",
    "work-log.md",
}
NON_DELIVERABLE_DIR_NAMES = {
    ".git",
    ".venv",
    "__pycache__",
    "glasshive-host-tools",
    "node_modules",
}


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
        if any(part in {".git", "node_modules", ".venv"} for part in rel.parts):
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
        if path.name.lower() in NON_DELIVERABLE_FILE_NAMES:
            continue
        if any(part.lower() in NON_DELIVERABLE_DIR_NAMES for part in rel.parts):
            continue
        candidates.append(path)
        if len(candidates) >= max_entries:
            break
    return sorted(candidates, key=lambda item: item.stat().st_mtime, reverse=True)


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

    artifact_candidates = candidate_artifact_paths(worker)
    if artifact_candidates:
        artifact = artifact_candidates[0]
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

    return None
