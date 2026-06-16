from __future__ import annotations

import os
import shlex
from pathlib import Path


VIVENTIUM_RUNTIME_ENV_KEYS = {
    "GLASSHIVE_EVENTS_WEBHOOK_URL",
    "GLASSHIVE_EVENTS_HMAC_SECRET",
    "VIVENTIUM_GLASSHIVE_CALLBACK_URL",
    "VIVENTIUM_GLASSHIVE_CALLBACK_SECRET",
    "GLASSHIVE_OPERATOR_BASE_URL",
    "GLASSHIVE_HOST_WORKERS_ENABLED",
    "GLASSHIVE_ENTERPRISE_MODE",
    "GLASSHIVE_AUTH_MODE",
    "GLASSHIVE_ENTERPRISE_TENANT_ID",
    "GLASSHIVE_AUTH_USER_HEADER",
    "GLASSHIVE_AUTH_EMAIL_HEADER",
    "GLASSHIVE_AUTH_ROLE_HEADER",
    "GLASSHIVE_AUTH_TENANT_HEADER",
    "GLASSHIVE_IDLE_TERMINATE_AFTER_S",
    "GLASSHIVE_PAUSED_TERMINATE_AFTER_S",
    "GLASSHIVE_IDLE_REAPER_INTERVAL_S",
    "GLASSHIVE_MAX_RUN_DURATION_S",
    "GLASSHIVE_MAX_WATCH_SESSION_DURATION_S",
    "GLASSHIVE_ARTIFACT_DOWNLOAD_MAX_BYTES",
    "GLASSHIVE_ENABLE_ADMIN_API",
    "GLASSHIVE_PROJECT_PROVIDER_ENV",
    "GLASSHIVE_WORKER_SECRET_ENV_EXPOSURE",
    "GLASSHIVE_WORKER_SECRET_ENV_KEYS",
    "GLASSHIVE_SIGNED_LINK_SECRET",
    "GLASSHIVE_WORKER_ENV_ALLOWLIST",
    "WPR_HOST_WORKSPACE_ROOT",
    "WPR_DEFAULT_EXECUTION_MODE",
    "WPR_LIBRECHAT_UPLOADS_ROOT",
    "WPR_BOOTSTRAP_SOURCE_ROOTS",
    "WPR_API_TOKEN",
    "WPR_MCP_BASE_URL",
    "WPR_MCP_HOST",
    "WPR_MCP_PORT",
    "WPR_MCP_TIMEOUT_SEC",
    "WPR_DEFAULT_OWNER_ID",
    "WPR_RUNTIME_BACKEND",
    "WPR_DB_PATH",
    "WPR_CODEX_BIN",
    "WPR_CLAUDE_CODE_BIN",
    "WPR_OPENCLAW_BIN",
    "GLASSHIVE_HOST_RUNTIME_REQUIREMENTS_JSON",
    "GLASSHIVE_HOST_RUNTIME_REQUIREMENTS_FILE",
    "WPR_HOST_RUNTIME_REQUIREMENTS_JSON",
    "WPR_HOST_RUNTIME_REQUIREMENTS_FILE",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_API_URL",
    "ANTHROPIC_BASE_URL",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_API_BASE",
    "OPENAI_REVERSE_PROXY",
    "PORTKEY_API_KEY",
    "PORTKEY_BASE_URL",
    "PORTKEY_PROVIDER",
    "PORTKEY_VIRTUAL_KEY",
    "PORTKEY_CONFIG",
    "WPR_CLAUDE_CODE_USE_API_KEY",
    "WPR_MODEL_HOST_CODEX_CLI",
    "WPR_MODEL_CLAUDE_CODE",
    "WPR_MODEL_CODEX_CLI",
    "WPR_MODEL_OPENCLAW_GENERAL",
    "WPR_MODEL_OPENCLAW_CODEX",
    "WPR_MODEL_OPENCLAW_CLAUDE",
    "WPR_MODEL_OPENCLAW_DESKTOP",
    "WPR_CODEX_CLI_BASE_URL",
    "WPR_CODEX_CLI_ENV_KEY",
    "WPR_CODEX_CLI_MODEL_PROVIDER",
    "WPR_CODEX_CLI_MODEL_VERBOSITY",
    "WPR_CODEX_CLI_PROVIDER_NAME",
    "WPR_CODEX_CLI_ALLOWED_REASONING_EFFORTS",
    "WPR_CODEX_CLI_REASONING_EFFORT_FALLBACK",
    "WPR_CODEX_CLI_REASONING_EFFORT",
    "WPR_CODEX_CLI_USE_CUSTOM_PROVIDER",
    "WPR_CODEX_CLI_DISABLE_CUSTOM_PROVIDER",
    "WPR_CODEX_CLI_IGNORE_USER_CONFIG",
    "WPR_CODEX_CLI_DISABLE_FEATURES",
    "WPR_CODEX_CLI_WIRE_API",
    "GLASSHIVE_HOST_CODEX_NATIVE_MCP_ALLOWLIST",
    "WPR_HOST_CODEX_NATIVE_MCP_ALLOWLIST",
    "GLASSHIVE_HOST_CODEX_PLUGIN_CACHE",
    "WPR_HOST_CODEX_PLUGIN_CACHE",
    "WPR_CLAUDE_CODE_ENABLE_CHROME",
    "WPR_CLAUDE_CODE_EFFORT",
    "WPR_OPENCLAW_BASE_URL",
    "WPR_OPENCLAW_ENV_KEY",
    "WPR_OPENCLAW_MODEL_ID",
    "WPR_OPENCLAW_MODEL_NAME",
    "WPR_OPENCLAW_MODEL_PROVIDER",
    "WPR_OPENCLAW_USE_CUSTOM_PROVIDER",
    "WPR_OPENCLAW_WIRE_API",
    "GLASSHIVE_MAX_ACTIVE_WORKERS_PER_USER",
    "GLASSHIVE_MAX_ACTIVE_WORKERS_PER_TENANT",
    "GLASSHIVE_MAX_WORKSPACES_PER_USER",
    "GLASSHIVE_MAX_WORKSPACES_PER_TENANT",
    "GLASSHIVE_ALLOWED_WORKER_PROFILES",
    "GLASSHIVE_DEFAULT_WORKER_PROFILE",
    "WPR_ALLOWED_WORKER_PROFILES",
}


def _env_flag(name: str) -> bool:
    value = str(os.environ.get(name, "")).strip().lower()
    return value in {"1", "true", "yes", "on", "enabled"}


def _candidate_env_files() -> list[Path]:
    candidates: list[Path] = []
    explicit = os.environ.get("VIVENTIUM_ENV_FILE", "").strip()
    if explicit:
        candidates.append(Path(explicit).expanduser())
    if _env_flag("VIVENTIUM_DISABLE_DEFAULT_RUNTIME_ENV"):
        return candidates
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


def _local_checkout_librechat_uploads_root() -> Path | None:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "LibreChat" / "uploads"
        if candidate.is_dir():
            return candidate
    return None


def _path_list(raw: str) -> list[str]:
    return [item.strip() for item in str(raw or "").split(os.pathsep) if item.strip()]


def _append_path_list_value(raw: str, value: Path) -> str:
    resolved = os.fspath(value)
    items = _path_list(raw)
    if resolved not in items:
        items.append(resolved)
    return os.pathsep.join(items)


def _repair_local_upload_roots(loaded: dict[str, str]) -> None:
    fallback_root = _local_checkout_librechat_uploads_root()
    if fallback_root is None:
        return

    configured_root = os.environ.get("WPR_LIBRECHAT_UPLOADS_ROOT", "").strip()
    configured_path = Path(configured_root).expanduser() if configured_root else None
    if configured_path is None or not configured_path.is_dir():
        os.environ["WPR_LIBRECHAT_UPLOADS_ROOT"] = os.fspath(fallback_root)
        loaded["WPR_LIBRECHAT_UPLOADS_ROOT"] = os.fspath(fallback_root)

    source_roots = os.environ.get("WPR_BOOTSTRAP_SOURCE_ROOTS", "").strip()
    repaired_roots = _append_path_list_value(source_roots, fallback_root)
    if repaired_roots != source_roots:
        os.environ["WPR_BOOTSTRAP_SOURCE_ROOTS"] = repaired_roots
        loaded["WPR_BOOTSTRAP_SOURCE_ROOTS"] = repaired_roots


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
    if {"WPR_LIBRECHAT_UPLOADS_ROOT", "WPR_BOOTSTRAP_SOURCE_ROOTS"} & wanted:
        _repair_local_upload_roots(loaded)
    return loaded
