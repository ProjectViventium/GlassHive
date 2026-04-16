<!-- VIVENTIUM START: Glass Hive QA playbook -->
# Glass Hive: Quick Power QA Playbook

Use this playbook to rapidly test whether Glass Hive is behaving like a real powerful workstation-sandbox runtime instead of a toy demo.

## 1. Core Project Flow

1. Create a project with a real goal and success criteria.
2. Create a worker with a chosen profile.
3. Run a task.
4. Confirm live worker state appears immediately.
5. Confirm the worker can be paused, resumed, interrupted, and terminated.
6. Confirm the project-first watch flow lands on the desktop by default.
7. Confirm the active live terminal is visible inside that desktop by default.
8. Confirm a fresh worker desktop does not default to the Selenium splash screen.

Pass if:

- the operator path feels simple
- the worker is resumable
- the run/event history is coherent
- the desktop-first view still shows the real live run terminal
- fresh workers open on a GlassHive-owned surface instead of Selenium branding

## 2. Sandbox Persistence

1. Create a file inside the worker workspace.
2. Pause the worker.
3. Resume the worker.
4. Confirm the file still exists.
5. Confirm the worker can continue from the same sandbox.

Pass if:

- workspace survives
- home/config survives
- session continuity is believable

## 3. Terminal + Desktop Takeover

1. Open the live worker desktop.
2. Open the live terminal takeover.
3. Launch shell, files, browser, Codex, Claude, and OpenClaw surfaces.
4. Pause the worker and take over.
5. Resume it.

Pass if:

- takeover is obvious
- the operator can intervene without guessing
- control returns cleanly to the worker

## 4. Codex Worker

1. Start a `codex-cli` worker.
2. Ask it to create files, inspect code, and continue a second turn.
3. Confirm it reuses the same sandbox and session continuity.

Pass if:

- first turn works
- second turn resumes instead of restarting from scratch
- artifacts are saved in the same workspace

## 5. Claude Worker

1. Start a `claude-code` worker.
2. Verify the sandbox is preconfigured with the expected Claude state.
3. Run a task requiring the local login or configured auth mode.
4. Continue it with a second turn.

Pass if:

- configuration is present
- the worker can use its projected auth/state
- project-scoped settings and `.mcp.json` are honored

## 6. OpenClaw Worker

1. Start an `openclaw-general` worker.
2. Verify it uses the same workstation substrate.
3. Launch the OpenClaw desktop surface.
4. Run a real task and confirm live operator visibility.

Pass if:

- it behaves like a workstation-backed worker, not a separate hidden runtime
- takeover and lifecycle semantics match the other profiles

## 7. Bootstrap Bundle

1. Create a worker with `bootstrap_profile=clean-room`.
2. Pass a `bootstrap_bundle` containing:
   - env
   - project files
   - `claude_project_mcp`
   - `claude_settings_local`
   - `system_instructions`
3. Inspect the sandbox.

Pass if:

- the expected files exist
- env is present in shell/task execution
- instructions and MCP config land in the right scope
- no raw secrets are echoed in API responses

## 8. Connected-Account Broker Readiness

1. Confirm Glass Hive does not require direct coupling to LibreChat internals.
2. Confirm a future client could provide provider auth through a brokered bundle.
3. Confirm the runtime can still run without any client-specific broker.

Pass if:

- the runtime remains standalone
- client-specific integration is optional
- the bootstrap contract is sufficient to carry auth/context/memory/callbacks later
