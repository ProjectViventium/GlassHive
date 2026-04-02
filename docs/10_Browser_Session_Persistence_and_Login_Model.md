<!-- VIVENTIUM START: GlassHive browser session persistence proposal -->
# GlassHive: Keeping the User Logged In (Browser Session Persistence)

## Purpose

This document is the source of truth for how browser sessions should persist inside GlassHive, what that means for named workers, and how parent systems should route long-lived personal browser tasks.

This is a proposal and review document only.

Do not implement the design in this document until it is explicitly approved.

## Terminology

- **Browser Session Persistence**: "Keeping the User Logged In". Making sure that if a worker logs into a website (like LinkedIn), it stays logged in the next time it runs.
- **Worker**: A background task runner that acts on the user's behalf.
- **Worker Alias**: A memorable, stable name for a specific worker (e.g., `demo-linkedin-primary`), so the system knows exactly which worker to wake up.
- **Sandbox**: The isolated, secure environment where the worker runs so it doesn't interfere with the main app.
- **Parent System**: The main application (like LibreChat or the Telegram bot) that gives tasks to the GlassHive workers.

## The Actual Question

If a user logs into Chrome or Chromium inside a GlassHive sandbox, will that browser session remain?

Practical answer:

1. yes, for the same worker, because the worker's home folder and profile storage are saved permanently.
2. no, not across every worker automatically (each worker has its own isolated profile).
3. the right user experience is to resume the same named worker (e.g., the "LinkedIn worker"), not to spawn a random new one each time.

## Current Code Truth

GlassHive already persists per-worker home and workspace state.

Evidence:

1. persistent `workspace_dir` and `home_dir` are created in [docker_sandbox.py](<workspace-root>/viventium_v0_4/GlassHive/runtime_phase1/src/workers_projects_runtime/docker_sandbox.py#L360)
2. those directories are mounted into the worker container in [docker_sandbox.py](<workspace-root>/viventium_v0_4/GlassHive/runtime_phase1/src/workers_projects_runtime/docker_sandbox.py#L435)
3. container `HOME` is pointed at the persistent mount in [docker_sandbox.py](<workspace-root>/viventium_v0_4/GlassHive/runtime_phase1/src/workers_projects_runtime/docker_sandbox.py#L447)
4. browser launch currently uses Chromium in [docker_sandbox.py](<workspace-root>/viventium_v0_4/GlassHive/runtime_phase1/src/workers_projects_runtime/docker_sandbox.py#L546)

That means browser profile data written under the worker home persists for that worker.

## What This Means In Practice

If you log into LinkedIn, Gmail, or another site inside a given worker sandbox, that browser session should remain available when that same worker is paused and resumed later.

This is the correct product model for long-lived personal assistance.

## What It Does Not Mean

It does not mean:

1. one login automatically becomes available in every worker
2. a disposable worker is the right home for personal browser work
3. GlassHive should merge all browser identities into one shared global profile

That would create security and continuity problems.

## The Right UX Model

Use stable, named personal workers for tasks that require logging into websites.

Examples:

1. `demo-linkedin-primary`
2. `demo-gmail-browser`
3. `demo-sales-replies`

Parent systems should resume these workers instead of recreating them.

## Lifecycle Model

### Recommended States

Use the following mental model for browser workers:

1. `active`
   - sandbox running
   - browser profile available
   - work may be in progress
2. `paused`
   - sandbox stopped or frozen
   - filesystem and browser profile preserved
   - resumable without re-login
3. `archived`
   - intentionally dormant but preserved
   - not shown as hot/default, still resumable
4. `destroyed`
   - sandbox data intentionally removed
   - browser session gone

### Recommended Transitions

1. `active -> paused`
2. `paused -> active`
3. `paused -> archived`
4. `archived -> active`
5. `active | paused | archived -> destroyed`

The default user path should be `pause/resume`, not `destroy/recreate`.

## Telegram / Messaging Scenario

For a user request such as:

```text
yo give me a list of people i need to reply to on my linkedin
```

the correct product flow is:

1. Viventium or the Telegram bot recognizes this as a task that requires being logged into a browser.
2. it looks up the user's stable LinkedIn worker alias (e.g., `demo-linkedin-primary`).
3. it asks GlassHive to find or wake up (resume) that specific worker.
4. the worker opens LinkedIn with its existing, saved browser session.
5. it collects the required information
6. it returns a concise result to the user and keeps the browser session intact for next time

Current Viventium / Telegram path that should own this orchestration:

1. [bot.py](<workspace-root>/viventium_v0_4/telegram-viventium/TelegramVivBot/bot.py#L258)
2. [librechat_bridge.py](<workspace-root>/viventium_v0_4/telegram-viventium/TelegramVivBot/utils/librechat_bridge.py#L1544)
3. LibreChat route `/api/viventium/telegram/chat`

## The Missing Capability For Smooth Routing

The parent system (LibreChat/Telegram) needs a simple way to say "find this specific worker, or create it if it doesn't exist".

Least-resistance options:

### Option A: Parent-Side Lookup + Existing APIs (Recommended)

1. parent keeps a map of `alias -> worker_id` (e.g., "linkedin worker" -> ID 123).
2. if the mapping exists, call `resume` to wake it up and assign work.
3. if not, create a new worker and save the mapping for next time.

### Option B: GlassHive Convenience Tool (Optional)

Add a convenience tool later such as:

```text
worker_find_or_resume(project_id, name, profile, role, bootstrap_bundle)
```

This is useful, but it is optional. The parent can do this with current primitives.

### Recommended V1 Choice

Start with Option A.

Reason:

1. it keeps GlassHive unchanged
2. it keeps user/service routing in the parent where that context already exists
3. it matches the least-resistance path for Telegram, LibreChat, and future parent systems

Only add a GlassHive-side helper if repeated parent-side lookup noise becomes measurable.

## Browser Profile Strategy

### V1 Recommendation

Keep one browser profile per worker.

That is the simplest reliable model.

### What The Parent Should Persist

The parent should persist:

1. worker alias
2. associated GlassHive `worker_id`
3. service label such as `linkedin`, `gmail-browser`, or `sales`
4. last-known lifecycle state

That is enough to provide a smooth `create or resume` experience without making GlassHive learn parent-specific identity rules.

### Why

1. login state remains stable
2. cookie and session data are predictable
3. user takeover stays understandable
4. support/debugging stays tractable

### Do Not Start With

1. shared cross-worker profiles
2. multiple profiles per worker
3. per-site browser-profile switching

Those are future extensions, not v1 requirements.

## Website-Side Reality

Even if GlassHive preserves the local browser profile, the website can still invalidate the session.

Examples:

1. provider-side logout
2. forced MFA re-check
3. suspicious-login detection
4. cookie expiry
5. region/IP/device checks

So the correct product promise is:

1. GlassHive preserves the local browser profile for the worker
2. user rarely has to log in again if the site accepts the preserved session
3. some sites may still require re-authentication

## Recommendation For Chrome vs Chromium

Current capability already supports real browser automation and browsing through Chromium.

For most Playwright-style or browser-task flows, Chromium is sufficient.

If a future requirement depends on Chrome-specific behavior, then add explicit Chrome support as an optional runtime profile.

Do not make Chrome a blocker for the broader session-persistence architecture.

## Exact Responsibilities By Layer

### GlassHive

Owns:

1. persistent per-worker home/profile storage
2. pause/resume of the same worker
3. desktop takeover and browser visibility
4. worker lifecycle state

### Viventium / Telegram / Parent Layer

Owns:

1. deciding which named worker should handle a browser task
2. worker alias mapping
3. deciding create vs resume
4. returning the resulting answer to the user

Current likely file owners:

1. [bot.py](<workspace-root>/viventium_v0_4/telegram-viventium/TelegramVivBot/bot.py#L258)
2. [librechat_bridge.py](<workspace-root>/viventium_v0_4/telegram-viventium/TelegramVivBot/utils/librechat_bridge.py#L1544)
3. Viventium agent instructions under `LibreChat/viventium/source_of_truth/`

## Least-Resistance Implementation Sequence

1. treat browser-authenticated work as stable named workers
2. keep browser profile persistence worker-scoped
3. add or use find-or-resume semantics from the parent side
4. default to pause/resume after successful tasks
5. reserve destroy for explicit teardown

## Acceptance Criteria For This Proposal

A future implementation based on this document should satisfy all of these:

1. a user can log into a site once inside a worker and keep using that session later through the same worker
2. a parent system can target the right worker without tracking random ephemeral sandboxes manually
3. pause/resume preserves browser state for the same worker
4. destroy is the only lifecycle action that intentionally clears the session
5. the model remains compatible with Telegram, LibreChat, and other parent systems
6. the parent can answer “find or resume my LinkedIn worker” without depending on ephemeral UUIDs in the user-facing flow

## Non-Goals

This proposal does not approve:

1. global browser-session sharing across all workers
2. GlassHive learning Telegram-specific routing logic
3. promising that third-party websites will never invalidate a session
4. forcing Chrome-specific behavior into the core architecture

## Recommended Next Review Questions

Before implementation approval, decide:

1. should worker alias lookup live only in the parent, or also as a GlassHive convenience tool?
2. do we want an explicit `archived` state in v1, or can `paused` cover the initial product need?
3. which browser-authenticated workflows deserve dedicated named workers by default?
4. should browser workers auto-pause after success, or remain active for a configurable warm window?
