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

    urls = extract_urls(latest_output, stdout_text, stderr_text)
    external_url = next((url for url in urls if not LOCALHOST_URL_PATTERN.search(url)), None)
    local_url = next((url for url in urls if LOCALHOST_URL_PATTERN.search(url)), None)

    if preferred_html is not None:
        browser_url = workspace_browser_url(preferred_html, worker)
        payload: dict[str, object] = {
            "kind": "webpage",
            "state": "ready" if latest_run else "available",
            "source": "workspace_html",
            "label": preferred_html.name,
            "preferred_surface": "desktop",
            "workspace_path": preferred_html.name,
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

    return None
