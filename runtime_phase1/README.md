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

## Run

```bash
cd <workspace-root>/viventium_v0_4/GlassHive/runtime_phase1
uv sync
uv run uvicorn workers_projects_runtime.api:app --reload --port 8766
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
