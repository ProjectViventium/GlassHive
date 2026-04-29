from __future__ import annotations

import os
import shlex
from pathlib import Path


VIVENTIUM_RUNTIME_ENV_KEYS = {
    "VIVENTIUM_GLASSHIVE_CALLBACK_URL",
    "VIVENTIUM_GLASSHIVE_CALLBACK_SECRET",
    "WPR_HOST_WORKSPACE_ROOT",
    "WPR_DEFAULT_EXECUTION_MODE",
    "WPR_LIBRECHAT_UPLOADS_ROOT",
    "WPR_API_TOKEN",
    "WPR_MCP_BASE_URL",
    "WPR_MCP_HOST",
    "WPR_MCP_PORT",
    "WPR_MCP_TIMEOUT_SEC",
    "WPR_DEFAULT_OWNER_ID",
    "WPR_RUNTIME_BACKEND",
    "WPR_DB_PATH",
}


def _candidate_env_files() -> list[Path]:
    candidates: list[Path] = []
    explicit = os.environ.get("VIVENTIUM_ENV_FILE", "").strip()
    if explicit:
        candidates.append(Path(explicit).expanduser())
    app_support = Path.home() / "Library" / "Application Support" / "Viventium" / "runtime"
    candidates.append(app_support / "runtime.env")
    candidates.append(app_support / "runtime.local.env")

    deduped: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key not in seen:
            seen.add(key)
            deduped.append(candidate)
    return deduped


def _parse_env_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    try:
        parts = shlex.split(stripped, comments=True, posix=True)
    except ValueError:
        return None
    if not parts:
        return None
    key, _, value = parts[0].partition("=")
    key = key.strip()
    if not key:
        return None
    return key, value


def load_viventium_runtime_env(keys: set[str] | None = None) -> dict[str, str]:
    wanted = keys or VIVENTIUM_RUNTIME_ENV_KEYS
    loaded: dict[str, str] = {}
    for env_file in _candidate_env_files():
        try:
            lines = env_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            parsed = _parse_env_line(line)
            if not parsed:
                continue
            key, value = parsed
            if key not in wanted or os.environ.get(key):
                continue
            os.environ[key] = value
            loaded[key] = value
    return loaded
