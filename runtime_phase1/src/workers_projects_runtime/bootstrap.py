from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import shlex
from pathlib import Path
from typing import Any, Callable


JsonDict = dict[str, Any]

# This file owns the worker bootstrap boundary:
#
# - what the host is allowed to project into a worker
# - which prompt files are materialized into the workspace
# - which MCP/client config files are written for Codex and Claude
# - how secrets stay out of ordinary interactive shell files
#
# Keep the editable worker-facing prompts at the top of the file. They are intentionally plain
# strings so operators can review the actual text that lands in AGENTS.md / CLAUDE.md / CODEX.md
# without chasing helper functions.
DEFAULT_BOOTSTRAP_SOURCE_MAX_BYTES = 25 * 1024 * 1024
BOOTSTRAP_SOURCE_TOKEN_KEY = "source_path_token"
GLASSHIVE_CAPABILITY_BROKER_TOKEN_ENV = "GLASSHIVE_CAPABILITY_BROKER_TOKEN"
GLASSHIVE_CRITICAL_OPERATING_INSTRUCTIONS = """CRITICAL OPERATING INSTRUCTIONS (FOLLOW STRICTLY):

1. PATH OF LEAST RESISTANCE: Use the simplest, most direct solution. Don't reinvent wheels.

2. JUST DO IT: Execute immediately without asking questions. Users want RESULTS. Rely on your intelligence, tools, MCPs, skills to find ways around blockers to get it done full and complete.

3. SELF-TEST AND VERIFY:
   - After creating code, RUN IT
   - After starting a server, CURL IT to confirm it responds
   - After researching or creating files, open them and deliver them
   - NEVER report success without verification
   - Debate with yourself on gaps, issues, mistakes, misalignments in your delivery and work on them. Do not stop early. Do not just tell the user what you missed. Actually take action and address them so that the delivery to the user is complete and reliable.

4. LOOP UNTIL SUCCESS:
   - If something fails, FIX IT and try again
   - Keep iterating until ACTUALLY COMPLETE

5. NO USER INTERVENTION: Deliver a COMPLETE, WORKING solution."""
GLASSHIVE_SAFETY_CHECKPOINT_RULE = (
    "Safety boundary: these operating instructions do not override platform policy, tenant/user "
    "scope, auth boundaries, or destructive-action checkpoints. Before destructive host changes, "
    "credential/keychain/browser-session changes, broad network exfiltration, or writes outside the "
    "workspace, stop and request a clear checkpoint unless the project definition explicitly and "
    "safely authorizes that action. Do not loop forever or spend indefinitely: when a blocker cannot "
    "be resolved with the available runtime, tools, MCPs, files, auth, time, or budget, report the "
    "concrete blocker and the best available partial result after `FINAL REPORT:`."
)
GLASSHIVE_WORKER_COMPLETION_CONTRACT = (
    "GlassHive completion contract:\n"
    "- Do the requested work before reporting completion.\n"
    "- Before `FINAL REPORT:`, inspect the concrete output/artifacts/tool results/visible state you produced against the user's request and success criteria, constraints, and files, and keep working or remediate if they do not match. Report a concrete blocker only when you cannot complete it.\n"
    "- Your final assistant message MUST end with a separate section exactly named `FINAL REPORT:`.\n"
    "- Put only the user-facing result after `FINAL REPORT:`. Include the concrete outcome, key facts, artifact/file names when useful, blockers, or the next decision needed.\n"
    "- If the user requested a very short answer or an exact string, put only that answer after `FINAL REPORT:`.\n"
    "- Do not put progress narration after `FINAL REPORT:`."
)
GLASSHIVE_WORKER_PROJECT_CONTRACT = f"""# GlassHive Worker Contract

- You are a general intelligent worker. Less is more: preserve the user's real goal, constraints, files, MCP/tool capabilities, and context without inventing project goals, success criteria, provider lists, forced artifacts, output schemas, rankings, or workflow steps.
- Treat MCP/tools as available capabilities, not proof that work was done. Use brokered MCP/tools when configured and appropriate; if a needed tool, grant, auth, file, or runtime is absent or fails, report that concrete blocker instead of pretending to have used it.
- Keep data in and data out exact. Read the actual workspace files, uploaded paths, MCP/tool results, generated outputs, and visible state before relying on them.
- Mention user-facing artifacts/files only when you intentionally created them, they are needed, or the user asked for them. Do not force a download when a concise chat answer satisfies the request.

{GLASSHIVE_CRITICAL_OPERATING_INSTRUCTIONS}

{GLASSHIVE_WORKER_COMPLETION_CONTRACT}

{GLASSHIVE_SAFETY_CHECKPOINT_RULE}
"""
DEFAULT_ENTERPRISE_WORKER_ENV_KEYS = {
    GLASSHIVE_CAPABILITY_BROKER_TOKEN_ENV,
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_API_BASE",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_API_URL",
    "PORTKEY_API_KEY",
    "PORTKEY_BASE_URL",
    "PORTKEY_PROVIDER",
    "PORTKEY_VIRTUAL_KEY",
    "PORTKEY_CONFIG",
    "WPR_CLAUDE_CODE_USE_API_KEY",
}
DEFAULT_SECRET_ENV_MARKERS = (
    "API_KEY",
    "AUTH_TOKEN",
    "BEARER",
    "CLIENT_SECRET",
    "CUSTOM_HEADERS",
    "HMAC",
    "PASSWORD",
    "PRIVATE_KEY",
    "SECRET",
    "TOKEN",
    "VIRTUAL_KEY",
)
USER_PROVIDER_SECRET_ENV_PREFIXES = (
    "GMAIL_",
    "GOOGLE_",
    "GOOGLE_WORKSPACE_",
    "MICROSOFT_",
    "MS365_",
    "MS_GRAPH_",
    "OUTLOOK_",
)
USER_PROVIDER_SECRET_ENV_MARKERS = (
    "ACCESS_TOKEN",
    "ID_TOKEN",
    "OAUTH",
    "REFRESH_TOKEN",
    "SESSION_TOKEN",
)


def _instruction_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _unique_instruction_parts(*values: Any) -> list[str]:
    parts: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _instruction_text(value)
        if not text or text in seen:
            continue
        parts.append(text)
        seen.add(text)
    return parts


def merge_glasshive_worker_instructions(*values: Any) -> str:
    extras = [
        text
        for text in _unique_instruction_parts(*values)
        if text.strip() != GLASSHIVE_WORKER_PROJECT_CONTRACT.strip()
    ]
    body = GLASSHIVE_WORKER_PROJECT_CONTRACT.rstrip()
    if not extras:
        return body + "\n"
    return body + "\n\nHost-provided instructions:\n" + "\n\n".join(extras).rstrip() + "\n"


def glasshive_project_agents_md(bundle: JsonDict) -> str:
    return merge_glasshive_worker_instructions(bundle.get("agents_md"), bundle.get("system_instructions"))


def glasshive_project_claude_md(bundle: JsonDict) -> str:
    explicit = _instruction_text(bundle.get("claude_md"))
    body = (
        "@AGENTS.md\n\n"
        "Claude worker context. Treat AGENTS.md as the canonical GlassHive project instruction source."
    )
    if explicit and explicit != "@AGENTS.md":
        body += "\n\nClaude-specific host instructions:\n" + explicit
    return body.rstrip() + "\n"


def glasshive_project_codex_md(bundle: JsonDict) -> str:
    explicit = _instruction_text(bundle.get("codex_md"))
    body = "Codex worker context. AGENTS.md is the canonical GlassHive project instruction source."
    if explicit:
        body += "\n\nCodex-specific host instructions:\n" + explicit
    return body.rstrip() + "\n"


def _env_flag(name: str, default: bool = False) -> bool:
    raw = str(os.environ.get(name, "")).strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "enabled"}


def _enterprise_mode_enabled() -> bool:
    return _env_flag("GLASSHIVE_ENTERPRISE_MODE") or _env_flag("WPR_ENTERPRISE_MODE")


def _bootstrap_source_secret() -> str:
    for name in ("GLASSHIVE_BOOTSTRAP_SOURCE_SECRET", "VIVENTIUM_GLASSHIVE_CALLBACK_SECRET", "WPR_API_TOKEN"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def _canonical_source_for_token(source: Path | str) -> str:
    return os.fspath(Path(os.path.abspath(os.fspath(Path(source).expanduser()))))


def sign_bootstrap_source_path(source: Path | str, *, tenant_id: str | None = None, owner_id: str | None = None) -> str:
    secret = _bootstrap_source_secret()
    if not secret:
        return ""
    message = "\0".join(
        (
            "v1",
            _canonical_source_for_token(source),
            str(tenant_id or ""),
            str(owner_id or ""),
        )
    )
    digest = hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"v1:{digest}"


def _source_token_is_valid(entry: dict[str, Any], source: Path | str, worker: dict[str, Any]) -> bool:
    expected = sign_bootstrap_source_path(
        source,
        tenant_id=str(worker.get("tenant_id") or ""),
        owner_id=str(worker.get("owner_id") or ""),
    )
    token = str(entry.get(BOOTSTRAP_SOURCE_TOKEN_KEY) or "").strip()
    return bool(expected and token and hmac.compare_digest(token, expected))


def _worker_env_allowlist() -> set[str]:
    raw = os.environ.get("GLASSHIVE_WORKER_ENV_ALLOWLIST", "").strip()
    if not raw:
        return set(DEFAULT_ENTERPRISE_WORKER_ENV_KEYS)
    values = {item.strip() for item in raw.split(",") if item.strip()}
    disallowed = sorted(key for key in values if _looks_like_user_provider_secret_env_key(key))
    if disallowed:
        raise RuntimeError(
            "GLASSHIVE_WORKER_ENV_ALLOWLIST must not include user provider OAuth/session token keys: "
            + ", ".join(disallowed)
        )
    return values | DEFAULT_ENTERPRISE_WORKER_ENV_KEYS


def _looks_like_user_provider_secret_env_key(key: str) -> bool:
    upper = key.upper()
    return any(upper.startswith(prefix) for prefix in USER_PROVIDER_SECRET_ENV_PREFIXES) and any(
        marker in upper for marker in USER_PROVIDER_SECRET_ENV_MARKERS
    )


def _worker_secret_env_keys(env: dict[str, str]) -> set[str]:
    raw = os.environ.get("GLASSHIVE_WORKER_SECRET_ENV_KEYS", "").strip()
    configured = {item.strip() for item in raw.split(",") if item.strip()}
    detected = {
        key
        for key in env
        if any(marker in key.upper() for marker in DEFAULT_SECRET_ENV_MARKERS)
    }
    return configured | detected


def _worker_secret_env_exposure_mode() -> str:
    raw = os.environ.get("GLASSHIVE_WORKER_SECRET_ENV_EXPOSURE", "").strip().lower()
    if raw in {"shell", "runtime", "legacy"}:
        return "shell"
    return "run-only"


def bootstrap_profile_for(worker: dict[str, Any], runtime_name: str) -> str:
    configured = str(worker.get("bootstrap_profile") or "").strip()
    if configured:
        return configured
    if runtime_name == "codex-cli":
        return "codex-host"
    if runtime_name == "claude-code":
        return "claude-host"
    return "host-login"


def bootstrap_bundle_for(worker: dict[str, Any]) -> JsonDict:
    """Return the structured bootstrap bundle attached to a worker row.

    Typical shape, abbreviated:

        {
            "project_definition": "# User goal...",
            "files": [{"scope": "workspace", "path": "uploads/brief.pdf", "source_path": "..."}],
            "claude_project_mcp": {"glasshive-user-capabilities": {...}},
            "codex_config_append": "[mcp_servers.glasshive-user-capabilities]...",
            "env": {"GLASSHIVE_CAPABILITY_BROKER_TOKEN": "..."}
        }

    The bundle is the host-to-worker data plane. It should carry real files, real scoped grants,
    and real configuration only; missing capabilities are represented as missing data, not invented
    prompt text.
    """
    raw = worker.get("bootstrap_bundle_json")
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(str(raw))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def bootstrap_env_for(worker: dict[str, Any]) -> dict[str, str]:
    bundle = bootstrap_bundle_for(worker)
    raw = bundle.get("env")
    enterprise = _enterprise_mode_enabled()
    allowed = _worker_env_allowlist() if enterprise else None
    if not isinstance(raw, dict):
        env = {}
    else:
        env = {}
        for key, value in raw.items():
            if value is None:
                continue
            env_key = str(key)
            if allowed is not None and env_key not in allowed:
                continue
            env[env_key] = str(value)
    if _env_flag("GLASSHIVE_PROJECT_PROVIDER_ENV", default=enterprise):
        for key in _worker_env_allowlist():
            value = os.environ.get(key)
            if value and key not in env:
                env[key] = value
    return env


def apply_bootstrap(
    *,
    home_dir: Path,
    workspace_dir: Path,
    runtime_name: str,
    worker: dict[str, Any],
    copy_file: Callable[[Path, Path], None],
    copy_tree: Callable[[Path, Path], None],
) -> None:
    """Materialize login/config/files for a fresh sandbox worker.

    Local developer mode may copy existing CLI auth so the worker can run with the owner's tools.
    Enterprise mode does not copy host auth files; it projects only the scoped bundle/env allowed by
    policy and writes MCP grants into owner-only files.
    """
    profile = bootstrap_profile_for(worker, runtime_name)
    bundle = bootstrap_bundle_for(worker)

    if profile not in {"clean-room", "none"} and not _enterprise_mode_enabled():
        if profile in {"host-login", "full-local", "codex-host"} or runtime_name in {"codex-cli", "openclaw"}:
            copy_file(Path.home() / ".codex" / "auth.json", home_dir / ".codex" / "auth.json")
        if profile in {"host-login", "full-local", "claude-host"} or runtime_name in {"claude-code", "openclaw"}:
            copy_file(Path.home() / ".claude.json", home_dir / ".claude.json")
        if profile in {"host-login", "full-local", "claude-host"} and runtime_name == "claude-code":
            copy_tree(Path.home() / ".claude", home_dir / ".claude")
        if profile in {"host-login", "full-local", "claude-host"} and runtime_name == "openclaw":
            copy_file(Path.home() / ".claude" / "settings.json", home_dir / ".claude" / "settings.json")
        if profile in {"host-login", "full-local", "codex-host", "claude-host"}:
            copy_file(Path.home() / ".gitconfig", home_dir / ".gitconfig")

    _write_runtime_env(home_dir, bootstrap_env_for(worker))
    _write_project_files(home_dir, workspace_dir, bundle, worker, copy_file, copy_tree)
    _write_claude_project_files(workspace_dir, bundle)
    _write_codex_config(home_dir, bundle)
    _write_manifest(home_dir, profile, bundle)


def refresh_runtime_env_for_worker(home_dir: Path, worker: dict[str, Any]) -> None:
    """Refresh per-run environment projection without rewriting project files."""
    _write_runtime_env(home_dir, bootstrap_env_for(worker))


def refresh_project_runtime_files_for_worker(home_dir: Path, workspace_dir: Path, worker: dict[str, Any]) -> None:
    """Refresh run-scoped MCP/client config without copying host auth or user files."""
    bundle = bootstrap_bundle_for(worker)
    _write_claude_project_files(workspace_dir, bundle)
    _write_codex_config(home_dir, bundle)


def _atomic_write_text(path: Path, text: str, *, mode: int = 0o644) -> None:
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(text)
    tmp_path.chmod(mode)
    tmp_path.replace(path)


def _write_env_file(path: Path, env: dict[str, str], *, mode: int = 0o644) -> None:
    if env:
        lines = [f"export {key}={shlex.quote(value)}" for key, value in sorted(env.items())]
        _atomic_write_text(path, "\n".join(lines) + "\n", mode=mode)
        return
    if path.exists():
        path.unlink()


def _write_runtime_env(home_dir: Path, env: dict[str, str]) -> None:
    glasshive_dir = home_dir / ".glasshive"
    glasshive_dir.mkdir(parents=True, exist_ok=True)
    runtime_env = glasshive_dir / "runtime.env"
    secret_env = glasshive_dir / "secret-runtime.env"
    secret_keys_path = glasshive_dir / "secret-runtime.keys"
    secret_env_values: dict[str, str] = {}
    shell_env_values = dict(env)
    if _enterprise_mode_enabled() and _worker_secret_env_exposure_mode() == "run-only":
        secret_keys = _worker_secret_env_keys(env)
        secret_env_values = {key: value for key, value in env.items() if key in secret_keys}
        shell_env_values = {key: value for key, value in env.items() if key not in secret_keys}
    runtime_env_mode = 0o600 if _worker_secret_env_keys(shell_env_values) else 0o644
    _write_env_file(runtime_env, shell_env_values, mode=runtime_env_mode)
    _write_env_file(secret_env, secret_env_values, mode=0o600)
    if secret_env_values:
        _atomic_write_text(secret_keys_path, "\n".join(sorted(secret_env_values)) + "\n", mode=0o600)
    elif secret_keys_path.exists():
        secret_keys_path.unlink()
    bashrc = home_dir / ".bashrc"
    source_line = 'if [ -f "$HOME/.glasshive/runtime.env" ]; then source "$HOME/.glasshive/runtime.env"; fi'
    existing = bashrc.read_text() if bashrc.exists() else ""
    if source_line not in existing:
        prefix = existing.rstrip() + ("\n" if existing.strip() else "")
        bashrc.write_text(prefix + source_line + "\n")


def _safe_relative_path(raw_path: str) -> Path:
    relative = Path(raw_path.strip().lstrip("/"))
    if relative.is_absolute() or ".." in relative.parts or not str(relative):
        raise ValueError(f"Unsafe bootstrap path: {raw_path}")
    return relative


def _source_path_from_entry(entry: dict[str, Any]) -> Path | None:
    for key in ("source_path", "local_path", "upload_path", "absolute_path", "filepath"):
        value = str(entry.get(key) or "").strip()
        if value:
            return Path(value).expanduser()
    return None


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _allowed_bootstrap_source_roots() -> list[tuple[Path, Path]]:
    raw = os.environ.get("WPR_BOOTSTRAP_SOURCE_ROOTS", "").strip()
    if not raw:
        return []
    roots: list[tuple[Path, Path]] = []
    for item in raw.split(os.pathsep):
        item = item.strip()
        if not item:
            continue
        lexical = Path(os.path.abspath(os.fspath(Path(item).expanduser())))
        try:
            roots.append((lexical, lexical.resolve(strict=True)))
        except FileNotFoundError:
            continue
    return roots


def _bootstrap_source_max_bytes() -> int:
    raw = os.environ.get("WPR_BOOTSTRAP_SOURCE_MAX_BYTES", "").strip()
    if not raw:
        return DEFAULT_BOOTSTRAP_SOURCE_MAX_BYTES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_BOOTSTRAP_SOURCE_MAX_BYTES
    return max(value, 0)


def _path_has_symlink_component(path: Path, root: Path) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    current = root
    for part in relative.parts:
        current = current / part
        try:
            if current.is_symlink():
                return True
        except OSError:
            return True
    return False


def _assert_source_size(path: Path, max_bytes: int) -> None:
    if path.is_dir():
        total = 0
        for child in path.rglob("*"):
            if child.is_symlink():
                raise PermissionError(f"Bootstrap source path must not contain symlinks: {child}")
            if child.is_file():
                total += child.stat().st_size
                if total > max_bytes:
                    raise PermissionError(f"Bootstrap source path exceeds size limit: {path}")
        return
    if path.stat().st_size > max_bytes:
        raise PermissionError(f"Bootstrap source path exceeds size limit: {path}")


def resolve_bootstrap_source_path(source: Path | str) -> Path:
    raw = Path(source).expanduser()
    if not raw.is_absolute():
        raise PermissionError(f"Bootstrap source path must be absolute: {source}")
    lexical = Path(os.path.abspath(os.fspath(raw)))
    roots = _allowed_bootstrap_source_roots()
    if not roots:
        raise PermissionError("Bootstrap source_path is disabled until WPR_BOOTSTRAP_SOURCE_ROOTS allows trusted roots")
    try:
        resolved = lexical.resolve(strict=True)
    except FileNotFoundError:
        raise FileNotFoundError(f"Bootstrap source file not found: {source}") from None
    allowed_root: Path | None = None
    for lexical_root, resolved_root in roots:
        lexical_allowed = _is_relative_to(lexical, lexical_root) or _is_relative_to(lexical, resolved_root)
        if lexical_allowed and _is_relative_to(resolved, resolved_root):
            allowed_root = lexical_root if _is_relative_to(lexical, lexical_root) else resolved_root
            break
    if allowed_root is None:
        raise PermissionError(f"Bootstrap source path is outside trusted roots: {source}")
    if _path_has_symlink_component(lexical, allowed_root):
        raise PermissionError(f"Bootstrap source path must not use symlinks: {source}")
    _assert_source_size(resolved, _bootstrap_source_max_bytes())
    return resolved


def _write_project_files(
    home_dir: Path,
    workspace_dir: Path,
    bundle: JsonDict,
    worker: dict[str, Any],
    copy_file: Callable[[Path, Path], None],
    copy_tree: Callable[[Path, Path], None],
) -> None:
    """Materialize `bundle["files"]` into the worker home or workspace.

    Supported entries:
    - inline text: `{"path": "uploads/note.txt", "content": "..."}`
    - inline bytes: `{"path": "uploads/file.pdf", "encoding": "base64", "content_base64": "..."}`
    - trusted source copy: `{"path": "uploads/file.pdf", "source_path": "/trusted/file.pdf"}`

    Enterprise source copies require a signed path token scoped to the same tenant/user.
    """
    files = bundle.get("files")
    if not isinstance(files, list):
        return
    for entry in files:
        if not isinstance(entry, dict):
            continue
        scope = str(entry.get("scope") or "workspace").strip().lower()
        raw_path = str(entry.get("path") or "").strip()
        if not raw_path:
            filename = str(entry.get("filename") or entry.get("file_id") or "").strip()
            raw_path = f"uploads/{filename}" if filename else ""
        rel_path = raw_path.strip().lstrip("/")
        if not rel_path:
            continue
        root = home_dir if scope == "home" else workspace_dir
        target = root / _safe_relative_path(rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if str(entry.get("encoding") or "").strip().lower() == "base64" or "content_base64" in entry:
            raw = str(entry.get("content_base64") or entry.get("content") or "")
            try:
                target.write_bytes(base64.b64decode(raw, validate=True))
            except Exception as exc:
                raise ValueError(f"Invalid base64 bootstrap content for {rel_path}") from exc
            continue
        if "content" in entry:
            target.write_text(str(entry.get("content") or ""))
            continue
        source = _source_path_from_entry(entry)
        if source is None:
            target.write_text("")
            continue
        if _enterprise_mode_enabled() and not _source_token_is_valid(entry, source, worker):
            raise PermissionError("Bootstrap source_path is not authorized for this enterprise user")
        source = resolve_bootstrap_source_path(source)
        if not source.exists():
            raise FileNotFoundError(f"Bootstrap source file not found: {source}")
        if source.is_dir():
            copy_tree(source, target)
        else:
            copy_file(source, target)


def _write_claude_project_files(workspace_dir: Path, bundle: JsonDict) -> None:
    """Write project-scoped Claude/Codex instruction and MCP files.

    Claude reads `.mcp.json` and `.claude/settings.local.json` from the project. Codex reads
    `AGENTS.md` and, for MCP, the worker-specific `.codex/config.toml` written under the worker
    home. The lower-case files are compatibility mirrors for older agents/tools.
    """
    workspace_dir.mkdir(parents=True, exist_ok=True)
    settings_local = bundle.get("claude_settings_local")
    if isinstance(settings_local, dict):
        target = workspace_dir / ".claude" / "settings.local.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(settings_local, indent=2, sort_keys=True) + "\n")
        target.chmod(0o600)

    project_mcp = bundle.get("claude_project_mcp")
    if isinstance(project_mcp, dict):
        payload = _claude_project_mcp_payload(bundle, project_mcp)
        target = workspace_dir / ".mcp.json"
        target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        target.chmod(0o600)

    agents_md = glasshive_project_agents_md(bundle)
    claude_md = glasshive_project_claude_md(bundle)
    codex_md = glasshive_project_codex_md(bundle)
    for filename, content in (
        ("agents.md", agents_md),
        ("AGENTS.md", agents_md),
        ("claude.md", claude_md),
        ("CLAUDE.md", claude_md),
        ("codex.md", codex_md),
        ("CODEX.md", codex_md),
    ):
        (workspace_dir / filename).write_text(content)


def _claude_project_mcp_payload(bundle: JsonDict, project_mcp: dict[str, Any]) -> JsonDict:
    """Normalize Claude MCP config and avoid embedding broker tokens in `.mcp.json`.

    Hosts may construct a Claude MCP payload with a literal bearer grant for convenience. Before the
    file hits disk, replace the literal grant with `${GLASSHIVE_CAPABILITY_BROKER_TOKEN}` whenever
    the same token is present in the scoped bootstrap env.
    """
    payload = project_mcp if isinstance(project_mcp.get("mcpServers"), dict) else {"mcpServers": project_mcp}
    payload = json.loads(json.dumps(payload))
    env = bootstrap_env_for({"bootstrap_bundle_json": bundle})
    grant = str(env.get(GLASSHIVE_CAPABILITY_BROKER_TOKEN_ENV) or "").strip()
    if not grant:
        return payload
    env_auth = f"Bearer ${{{GLASSHIVE_CAPABILITY_BROKER_TOKEN_ENV}}}"
    literal_auth = f"Bearer {grant}"
    servers = payload.get("mcpServers")
    if not isinstance(servers, dict):
        return payload
    for config in servers.values():
        if not isinstance(config, dict):
            continue
        headers = config.get("headers")
        if not isinstance(headers, dict):
            continue
        if str(headers.get("Authorization") or "").strip() == literal_auth:
            headers["Authorization"] = env_auth
    return payload


def claude_project_mcp_payload_for_bundle(bundle: JsonDict, project_mcp: dict[str, Any]) -> JsonDict:
    """Public wrapper for host and sandbox bootstrap paths that write Claude `.mcp.json` files."""
    return _claude_project_mcp_payload(bundle, project_mcp)


def _write_codex_config(home_dir: Path, bundle: JsonDict) -> None:
    """Append/refresh worker-local Codex MCP config without duplicating old server blocks."""
    append = bundle.get("codex_config_append")
    if not isinstance(append, str) or not append.strip():
        return
    target = home_dir / ".codex" / "config.toml"
    target.parent.mkdir(parents=True, exist_ok=True)
    existing = target.read_text() if target.exists() else ""
    mcp_names = _codex_mcp_server_names(append)
    if mcp_names:
        existing = _strip_codex_mcp_server_blocks(existing, mcp_names)
    prefix = existing.rstrip() + ("\n\n" if existing.strip() else "")
    target.write_text(prefix + append.strip() + "\n")
    target.chmod(0o600)


def _codex_mcp_server_names(config_text: str) -> set[str]:
    return {
        match.group(1).strip()
        for match in re.finditer(r"(?m)^\s*\[mcp_servers\.([^\]\s]+)\]\s*$", config_text)
        if match.group(1).strip()
    }


def _strip_codex_mcp_server_blocks(config_text: str, names: set[str]) -> str:
    if not config_text.strip() or not names:
        return config_text.rstrip()
    output: list[str] = []
    skipping = False
    for line in config_text.splitlines():
        section = re.match(r"^\s*\[([^\]]+)\]\s*$", line)
        if section:
            section_name = section.group(1).strip()
            skipping = section_name.startswith("mcp_servers.") and section_name[len("mcp_servers.") :] in names
        if not skipping:
            output.append(line)
    return "\n".join(output).rstrip()


def _write_manifest(home_dir: Path, profile: str, bundle: JsonDict) -> None:
    glasshive_dir = home_dir / ".glasshive"
    glasshive_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "bootstrap_profile": profile,
        "bundle_keys": sorted(bundle.keys()),
        "env_keys": sorted(bootstrap_env_for({"bootstrap_bundle_json": bundle}).keys()),
        "file_count": len(bundle.get("files") or []) if isinstance(bundle.get("files"), list) else 0,
        "has_claude_project_mcp": isinstance(bundle.get("claude_project_mcp"), dict),
        "has_claude_settings_local": isinstance(bundle.get("claude_settings_local"), dict),
        "has_codex_config_append": bool(str(bundle.get("codex_config_append") or "").strip()),
    }
    (glasshive_dir / "bootstrap-manifest.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
