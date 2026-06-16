<!-- VIVENTIUM START: Glass Hive runtime README -->
# Glass Hive Runtime Phase 1

This is the current runnable runtime inside Glass Hive.

It keeps the Python package name `workers_projects_runtime` for compatibility, but it is now documented and packaged as part of **Glass Hive**.

## What It Does

- creates persistent `Projects` and `Workers`
- starts workstation-backed worker sandboxes for `codex-cli`, `claude-code`, and `openclaw-general`
- provides live desktop view, terminal takeover, runs, logs, events, and artifact visibility
- persists worker home and workspace across runs
- supports `pause`, `resume`, `interrupt`, and `terminate`
- exposes a thin MCP wrapper over the runtime API
- supports portable bootstrap seeding via:
  - `bootstrap_profile`
  - `bootstrap_bundle`

## Bootstrap Contract

Each worker can now carry:

- `bootstrap_profile`
  - examples: `clean-room`, `host-login`, `codex-host`, `claude-host`
- `bootstrap_bundle`
  - structured optional payload for:
    - `env`
    - `files`
    - `claude_project_mcp`
    - `claude_settings_local`
    - `codex_config_append`
    - `claude_md`
    - `agents_md`
    - `system_instructions`

Current behavior:

- existing host-projection defaults remain backward-compatible
- Glass Hive writes a non-secret bootstrap manifest inside the sandbox for inspection
- bundle environment is available to sandboxed runs and interactive shells
- broker/client config is additive over native worker capability: Codex/Claude host and workstation
  launches must not drop browser, computer/desktop, shell, file, or MCP capabilities just because
  GlassHive projected a broker MCP

## Host Runtime Requirements

Host workers are native AI-agent substrates. They must keep the worker's real CLI capabilities
available instead of launching stripped shells. GlassHive checks the configured host CLI before
dispatching a host worker and fails closed when the selected substrate cannot provide the required
native surface.

Host-native Codex preserves non-MCP Codex configuration so model/provider defaults and project trust
settings continue to behave like the user's real Codex environment. Private or non-brokered MCP
server blocks are stripped before worker config is materialized, then GlassHive appends only the
allowed native/broker MCP entries. Operators should choose host mode only when exposing host-local
Codex configuration to that host worker is intended; sandbox/workstation mode remains the safer
default when host-local state should not be shared.

Built-in minimums:

- Codex CLI: `0.140.0`
- Claude Code: `2.1.178` with native `--effort` support and, unless explicitly disabled,
  Chrome/browser support
- OpenClaw: `2026.6.6`

Override files or JSON may be supplied with `GLASSHIVE_HOST_RUNTIME_REQUIREMENTS_FILE`,
`WPR_HOST_RUNTIME_REQUIREMENTS_FILE`, `GLASSHIVE_HOST_RUNTIME_REQUIREMENTS_JSON`, or
`WPR_HOST_RUNTIME_REQUIREMENTS_JSON`. Invalid override configuration blocks host dispatch rather
than silently weakening worker capability.

When the default execution mode is host and the user did not explicitly choose host or a fixed
workspace path, GlassHive may recover a blocked first dispatch to sandbox/workstation execution.
`workspace_continue` is different: it preserves the same workspace files, browser state, notes, and
partial outputs. If that existing host workspace cannot be continued because the host CLI is missing
or too old, repair/update the host runtime and call `workspace_continue` again, or intentionally
start a fresh sandbox/workstation workspace if preserving host-local state is no longer required.

## Run

```bash
cd <workspace-root>/viventium_v0_4/GlassHive/runtime_phase1
uv sync
uv run uvicorn workers_projects_runtime.api:app --reload --port 8766 --no-access-log
```

Open:

- `http://127.0.0.1:8766/ui`
- `http://127.0.0.1:8766/docs`

Run MCP:

```bash
cd <workspace-root>/viventium_v0_4/GlassHive/runtime_phase1
uv run python -m workers_projects_runtime.mcp_server --transport streamable-http --port 8767
```

## Test

```bash
cd <workspace-root>/viventium_v0_4/GlassHive/runtime_phase1
uv run pytest -q
```

## Current Boundary

This phase proves the product/runtime shape well.

It does not yet prove a production connected-account broker from LibreChat into Glass Hive. The right design for that is a brokered provider projector, not direct dependence on LibreChat storage internals.
