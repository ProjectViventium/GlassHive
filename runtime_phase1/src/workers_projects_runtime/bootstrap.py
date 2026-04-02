from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any, Callable


JsonDict = dict[str, Any]


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
    if not isinstance(raw, dict):
        return {}
    env: dict[str, str] = {}
    for key, value in raw.items():
        if value is None:
            continue
        env[str(key)] = str(value)
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
    profile = bootstrap_profile_for(worker, runtime_name)
    bundle = bootstrap_bundle_for(worker)

    if profile not in {"clean-room", "none"}:
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
    _write_project_files(home_dir, workspace_dir, bundle)
    _write_claude_project_files(workspace_dir, bundle)
    _write_codex_config(home_dir, bundle)
    _write_manifest(home_dir, profile, bundle)


def _write_runtime_env(home_dir: Path, env: dict[str, str]) -> None:
    glasshive_dir = home_dir / ".glasshive"
    glasshive_dir.mkdir(parents=True, exist_ok=True)
    runtime_env = glasshive_dir / "runtime.env"
    if env:
        lines = [f"export {key}={shlex.quote(value)}" for key, value in sorted(env.items())]
        runtime_env.write_text("\n".join(lines) + "\n")
    bashrc = home_dir / ".bashrc"
    source_line = 'if [ -f "$HOME/.glasshive/runtime.env" ]; then source "$HOME/.glasshive/runtime.env"; fi'
    existing = bashrc.read_text() if bashrc.exists() else ""
    if source_line not in existing:
        prefix = existing.rstrip() + ("\n" if existing.strip() else "")
        bashrc.write_text(prefix + source_line + "\n")


def _write_project_files(home_dir: Path, workspace_dir: Path, bundle: JsonDict) -> None:
    files = bundle.get("files")
    if not isinstance(files, list):
        return
    for entry in files:
        if not isinstance(entry, dict):
            continue
        scope = str(entry.get("scope") or "workspace").strip().lower()
        rel_path = str(entry.get("path") or "").strip().lstrip("/")
        if not rel_path:
            continue
        root = home_dir if scope == "home" else workspace_dir
        target = root / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(entry.get("content") or ""))


def _write_claude_project_files(workspace_dir: Path, bundle: JsonDict) -> None:
    settings_local = bundle.get("claude_settings_local")
    if isinstance(settings_local, dict):
        target = workspace_dir / ".claude" / "settings.local.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(settings_local, indent=2, sort_keys=True) + "\n")

    project_mcp = bundle.get("claude_project_mcp")
    if isinstance(project_mcp, dict):
        target = workspace_dir / ".mcp.json"
        target.write_text(json.dumps(project_mcp, indent=2, sort_keys=True) + "\n")

    claude_md = bundle.get("claude_md") or bundle.get("system_instructions")
    if isinstance(claude_md, str) and claude_md.strip():
        (workspace_dir / "CLAUDE.md").write_text(claude_md.rstrip() + "\n")

    agents_md = bundle.get("agents_md") or bundle.get("system_instructions")
    if isinstance(agents_md, str) and agents_md.strip():
        (workspace_dir / "AGENTS.md").write_text(agents_md.rstrip() + "\n")


def _write_codex_config(home_dir: Path, bundle: JsonDict) -> None:
    append = bundle.get("codex_config_append")
    if not isinstance(append, str) or not append.strip():
        return
    target = home_dir / ".codex" / "config.toml"
    target.parent.mkdir(parents=True, exist_ok=True)
    existing = target.read_text() if target.exists() else ""
    prefix = existing.rstrip() + ("\n\n" if existing.strip() else "")
    target.write_text(prefix + append.strip() + "\n")


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
