<!-- VIVENTIUM START: GlassHive UI README -->
# GlassHive UI

A separate, minimal operator UI for GlassHive.

This service does not replace the existing GlassHive runtime UI. It sits beside it and talks to the runtime over HTTP.

## Purpose

- one centered project composer
- automatic redirect into live watch mode
- minimal ribbon controls
- full-screen sandbox watch surface
- result-first delivery for webpage/app tasks
- exact live session still available as a secondary takeover surface

## Current Behavior

- page/app prompts default to the desktop watch surface
- successful webpage deliverables are promoted into the sandbox browser automatically
- the top ribbon shows the latest result and lets the operator expand it without leaving the watch screen
- the exact attached terminal session is still available from the menu

Clipboard note:

- desktop takeover currently relies on noVNC clipboard support and browser clipboard permissions
- copy/paste is available, but a custom always-on browser clipboard bridge is not yet implemented

## Run

```bash
cd <workspace-root>/viventium_v0_4/GlassHive/frontends/glass-drive-ui
uv sync
uv run uvicorn glass_drive_ui.server:app --host 127.0.0.1 --port 8780
```

Env:

- `GLASSHIVE_RUNTIME_BASE_URL` default: `http://127.0.0.1:8766`
- `GLASSHIVE_DEFAULT_OWNER_ID` default: `demo-owner`

## Test

```bash
cd <workspace-root>/viventium_v0_4/GlassHive/frontends/glass-drive-ui
uv run pytest -q
```
