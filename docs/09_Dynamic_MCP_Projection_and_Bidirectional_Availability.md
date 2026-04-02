<!-- VIVENTIUM START: GlassHive dynamic MCP projection proposal -->
# GlassHive: Sharing Tools with Workers (MCP Projection) and Two-Way Communication

## Purpose

This document is the source of truth for how parent systems such as Viventium / LibreChat should project MCP capability, auth, instructions, and callbacks into GlassHive workers without coupling GlassHive to parent internals.

This is a proposal and review document only.

Do not implement the design in this document until it is explicitly approved.

## Terminology

- **Bootstrap Bundle**: The "Startup Package". A JSON payload containing instructions, auth tokens, and tools that the parent system (LibreChat) hands to a GlassHive worker when it boots up.
- **MCP Projection**: "Tool Sharing". The process of taking tools that belong to LibreChat and passing them securely into the worker's environment.
- **Bidirectional Communication**: Allowing LibreChat to send tasks to the worker, and allowing the worker to send results/webhooks back to LibreChat.
- **Adapter Layer**: The bridge code in LibreChat that packages up the user's tools into the Bootstrap Bundle before starting the worker.
- **Sandbox**: The isolated, secure environment where the worker runs so it doesn't interfere with the main app.

## The Actual Problem To Solve

The real requirement is not abstract “bidirectional capability.” It is three concrete tasks:

1. take the MCP servers and user-linked auth that already exist in LibreChat and make selected ones available inside a GlassHive worker sandbox
2. let the worker report useful results back to the parent system without inventing a new mesh of server-to-server protocols
3. let parent channels such as Telegram route work into the right persistent worker without forcing GlassHive to know about Telegram internals

## Executive Recommendation

Path of least resistance:

1. keep GlassHive unchanged as the standalone runtime that consumes the startup package (bootstrap)
2. implement MCP projection (tool sharing) in the Viventium / LibreChat adapter layer
3. use the `bootstrap_bundle` (startup package) as the only way to pass data into the worker
4. use callback webhooks for outbound v1 worker reporting
5. keep Telegram routing in the Telegram bot / Viventium layer, not in GlassHive core

## Why This Is The Right Boundary

GlassHive already has the generic ingestion side.

Evidence:

1. worker creation already accepts `bootstrap_profile` and `bootstrap_bundle_json` in [mcp_server.py](<workspace-root>/viventium_v0_4/GlassHive/runtime_phase1/src/workers_projects_runtime/mcp_server.py#L205)
2. bootstrap handling already projects env, files, Claude config, Codex config, and instructions in [bootstrap.py](<workspace-root>/viventium_v0_4/GlassHive/runtime_phase1/src/workers_projects_runtime/bootstrap.py#L12)
3. GlassHive is intentionally portable and should not parse LibreChat’s token persistence format directly

That means the missing work is mostly on the parent side.

## A. LibreChat MCP Projection Into Sandboxes

### The Correct Place To Implement It

Implement this in the Viventium / LibreChat adapter layer, not in GlassHive.

Recommended home:

- `viventium_v0_4/LibreChat/api/server/services/viventium/` as a focused adapter module

Current nearby files that already own adjacent responsibilities:

1. [initializeMCPs.js](<workspace-root>/viventium_v0_4/LibreChat/api/server/services/initializeMCPs.js)
2. [MCP.js](<workspace-root>/viventium_v0_4/LibreChat/api/server/services/MCP.js)
3. [request.js](<workspace-root>/viventium_v0_4/LibreChat/api/server/controllers/agents/request.js)
4. [responses.js](<workspace-root>/viventium_v0_4/LibreChat/api/server/controllers/agents/responses.js)

Suggested file names:

1. `glasshiveProjectionService.js`
2. `glasshiveBootstrapBuilder.js`
3. `glasshiveMcpResolver.js`

### What The Adapter Must Do

For a given LibreChat user and worker request:

1. read the active MCP server definitions from the loaded LibreChat config
2. determine which MCP servers are actually available to that user
3. resolve per-user auth or token-bearing connection details where needed
4. package those results into a versioned `bootstrap_bundle` (startup package)
5. call GlassHive `worker_create` or `worker_find_or_resume` with that bundle

### Concrete Flow

```text
LibreChat / Viventium request
  -> load active mcpServers from current app config
  -> filter to allowed / selected / user-available servers
  -> resolve auth material per server
  -> package these into a startup payload (bootstrap_bundle)
  -> call GlassHive worker_create or worker_find_or_resume with the bundle
  -> worker sandbox materializes .mcp.json / env / CLAUDE.md / AGENTS.md
```

### Exact Adapter Inputs And Outputs

The adapter should be a narrow transformer with explicit inputs and outputs.

Input:

1. current requesting user id
2. current agent id
3. current loaded MCP config
4. requested projection mode
5. optional explicit server allowlist
6. optional callback URLs

Output:

1. `bootstrap_profile`
2. versioned `bootstrap_bundle` (startup package)
3. optional worker alias or reuse hint

The adapter should not own worker execution. It should only build portable GlassHive input.

### Least-Resistance V1 Resolver Rules

#### 1. Server Source

Use the already-loaded LibreChat MCP config, not a second YAML parser if avoidable.

If needed, use the same internal config surface the backend already uses for MCP initialization.

#### 2. Selection Modes

Support three simple projection modes:

1. `explicit_list`: caller passes exact server names
2. `inherit_agent_context`: project the MCP servers already available to the requesting agent
3. `all_user_enabled`: project every server available to that user

Do not build a more complex policy engine for v1.

#### 3. Auth Resolution Rules

For each selected server:

1. API-key or static-header servers:
   - resolve concrete URL, headers, and env-backed values
   - emit into bootstrap bundle as either `.mcp.json` or environment variables
2. OAuth-backed MCP servers:
   - resolve the user’s token material from the parent system
   - do not make GlassHive read Mongo directly
   - parent resolves the token-bearing or proxy-ready form and passes it in the bundle

### OAuth Detail

For LibreChat-managed OAuth MCPs, the parent-side adapter should be responsible for reading the user’s current connection state.

This is the likely source of truth in local Viventium:

- LibreChat Mongo token docs keyed under patterns like `mcp:<server>` and related refresh/client records, as documented in [07_MCPs.md](<workspace-root>/docs/requirements_and_learnings/07_MCPs.md)

GlassHive should not know that schema.

The adapter resolves it and emits a portable result.

### User Availability Rules

Do not project every configured server blindly.

For each selected server, the adapter should answer:

1. is this server configured globally?
2. is it enabled for this user or agent context?
3. does this user have the required auth state?
4. is this server allowed to be projected into a worker by current policy?

If any answer is no, omit it and record that omission in the projection result.

## Bootstrap Bundle Contract

### Required V1 Shape

Use a versioned bundle.

```json
{
  "version": "2026-03-27.v1",
  "projection_mode": "inherit_agent_context",
  "projected_mcp_servers": ["google_workspace", "ms-365", "glasshive-workers-projects"],
  "claude_project_mcp": {
    "mcpServers": {}
  },
  "env": {},
  "files": [],
  "claude_md": "",
  "agents_md": "",
  "callbacks": {
    "events_webhook_url": "https://...",
    "artifacts_webhook_url": "https://..."
  }
}
```

### Materialization Rules

Inside GlassHive, the existing bootstrap system should continue to materialize:

1. `.mcp.json` for Claude-oriented workers
2. environment variables for API-key or tool-specific workers
3. `CLAUDE.md` and `AGENTS.md` for instructions
4. seeded files for project context and callback config

## B. Outbound From Sandbox Back To Viventium

### V1 Recommendation

Use callback webhooks.

Do not build reverse-MCP registration or a live multi-hop control mesh in v1.

### Why

Webhooks are enough for the first real product flows:

1. run completed
2. run failed
3. artifact produced
4. checkpoint reached
5. needs human takeover

### V1 Outbound Contract

Parent provides callback URLs in the `bootstrap_bundle`.

Current likely Viventium / LibreChat-side homes for callback receiver logic:

1. `viventium_v0_4/LibreChat/api/server/services/viventium/`
2. `viventium_v0_4/LibreChat/api/server/routes/`
3. `viventium_v0_4/LibreChat/api/server/controllers/agents/`

Worker instructions tell the agent or worker wrapper to POST events to those URLs.

Suggested event types:

1. `run.started`
2. `run.completed`
3. `run.failed`
4. `checkpoint.ready`
5. `artifact.created`
6. `takeover.requested`

Suggested payload fields:

1. `project_id`
2. `worker_id`
3. `run_id`
4. `event_type`
5. `timestamp`
6. `summary`
7. `artifact_refs`
8. `resume_token` or `worker_url` when relevant

## C. Telegram To Worker Routing

### The Correct Place To Implement It

This belongs in the Telegram bot / Viventium layer, not GlassHive core.

GlassHive should not know about Telegram chat resolution logic.

Current Telegram-side entry points:

1. [bot.py](<workspace-root>/viventium_v0_4/telegram-viventium/TelegramVivBot/bot.py#L258)
2. [librechat_bridge.py](<workspace-root>/viventium_v0_4/telegram-viventium/TelegramVivBot/utils/librechat_bridge.py#L1544)

### Least-Resistance Model

Use a parent-side lookup table or deterministic alias rule:

1. map user + service + intent to a stable worker alias
2. use create-or-resume behavior against GlassHive
3. send the result back through the parent channel

Example aliases:

1. `demo-linkedin-primary`
2. `demo-outbound-sales`
3. `demo-browser-admin`

### Concrete Telegram Flow

```text
Telegram message
  -> Telegram bot classifies this as browser / worker work
  -> resolve worker alias for that user
  -> call GlassHive: find or resume that worker
  -> optionally project MCP/auth/context via bootstrap_bundle (startup package)
  -> assign the task
  -> receive completion via polling or callback webhook
  -> send user-facing answer back through Telegram
```

### What GlassHive Needs For This

Only generic capability:

1. find worker by alias or name
2. resume worker
3. assign task
4. expose status and takeover URL

That is enough.

## Proposed First Implementation Slice

Do these in order:

1. add a LibreChat-side `glasshiveProjectionService`
2. implement `explicit_list` and `inherit_agent_context` projection modes
3. resolve static/API-key MCPs first
4. resolve OAuth-backed MCPs next using parent-side token lookup
5. pass callback URLs through `bootstrap_bundle`
6. add Telegram-side user-to-worker alias resolution

Do not start with generic “bidirectional MCP federation.”

### Tightened V1 Sequence By Repo

#### LibreChat / Viventium

1. read active `mcpServers` from the loaded app config
2. build `glasshiveProjectionService`
3. support `explicit_list` first
4. support `inherit_agent_context` second
5. resolve OAuth-backed servers using parent-side token records
6. emit versioned `bootstrap_bundle` (startup package)
7. call GlassHive create-or-resume path
8. accept callback webhooks and surface concise results back to the parent agent

#### Telegram Bot

1. classify when a request should route to GlassHive-capable agent behavior
2. maintain or derive stable worker aliases per user/service
3. send the request through the existing LibreChat bridge
4. return summarized result or takeover link back to Telegram

#### GlassHive

1. remain unchanged as the consumer of `bootstrap_bundle`
2. optionally add convenience find-or-resume semantics later if parent-side lookup becomes noisy

## Exact Responsibilities By Layer

### GlassHive

Owns:

1. standalone worker runtime
2. bootstrap bundle consumption
3. sandbox materialization
4. run execution
5. live status and takeover

Does not own:

1. LibreChat YAML parsing rules as a product contract
2. LibreChat Mongo token schema
3. Telegram identity routing

### Viventium / LibreChat Adapter

Owns:

1. reading active MCP config
2. resolving per-user availability
3. resolving per-user auth material
4. building projection bundles
5. receiving callbacks

### Telegram Layer

Owns:

1. user intent routing
2. worker alias mapping
3. create-or-resume decisions
4. channel delivery back to the user

## Acceptance Criteria For This Proposal

A future implementation based on this doc should satisfy all of these:

1. parent can create a worker with projected MCPs without changing GlassHive core contracts
2. OAuth-backed parent MCPs are resolved in the parent, not by GlassHive scraping parent storage
3. workers can send structured completion and artifact events back through callbacks
4. Telegram can target stable named workers without GlassHive learning Telegram-specific logic
5. GlassHive remains usable by non-Viventium parents such as Claude/Codex-style orchestrators
6. a projected worker can be reconstructed from logged inputs without hidden dependence on LibreChat internals
7. omitted MCPs are explained deterministically rather than failing silently

## Non-Goals

This proposal does not approve:

1. direct Mongo scraping from GlassHive into LibreChat
2. global reverse registration of arbitrary sandbox MCP servers into the parent UI
3. dynamic cross-process capability federation without explicit contracts
4. making GlassHive require Viventium in order to run

## Recommended Next Review Questions

Before implementation approval, decide:

1. should v1 project all user-enabled MCPs or only explicit / agent-context selections?
2. should OAuth-based projection pass tokens, proxy URLs, or both?
3. should parent callbacks be required for browser workers, or optional?
4. what exact worker aliasing rule should Telegram use by default?
