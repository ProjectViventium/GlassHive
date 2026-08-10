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

## Workspace Catalog And Retention

Workspace discovery is owner-scoped and cursor-paginated. The primary catalog contains `named`
workspaces; one-off `ephemeral` runs and migrated `legacy` rows require their explicit filters.
Catalog rows expose only rediscovery metadata, including normalized tags, provider/capability
readiness, and the next scheduled occurrence.

Named workspace files and private state survive compute reaping and runtime restart. Expired
Docker-managed ephemeral workspaces are garbage-collected after seven days by default. Configure
`GLASSHIVE_EPHEMERAL_RETENTION_S` (60 seconds to 365 days) or disable the policy with
`GLASSHIVE_EPHEMERAL_GC_ENABLED=false`. The reaper fails closed for active work, future schedules,
active account leases, pending confirmations, idempotency records, host/user workspace roots, or
unrecognized storage layouts; it never deletes named or legacy state.

The first provider account for an owner and provider becomes that provider's default automatically.
Explicitly selecting a later default retains the existing one-default-per-provider contract, and
disconnecting a default clears it so a newly connected replacement can become the default.

## Recurring Schedule Ownership

Legacy one-shot schedules remain GlassHive-native and retain their existing API and database
contract. Standalone recurrence is additive: durable definitions are stored separately from
immutable occurrence rows, and each occurrence is atomically materialized into the existing
one-shot queue.

- Standalone GlassHive defaults `GLASSHIVE_RECURRING_SCHEDULE_OWNER` to `glasshive_native`.
  The legacy `native` value remains accepted without rewriting stored definitions.
- Viventium deployments must use `viventium_cortex`; the legacy `scheduling_cortex` value remains
  accepted. GlassHive delegates recurrence CRUD and polling to Scheduling Cortex over an
  authenticated owner boundary and does not store a second local recurrence definition. Cortex
  dispatches each occurrence into the existing one-shot queue with a stable idempotency key.
  Owner configuration or availability failures fail closed before any local recurrence row is
  written, and Viventium runtime markers reject standalone-native ownership.
- Interval recurrence is elapsed UTC time. Daily recurrence uses an explicit IANA timezone.
- `next_valid_earliest` advances nonexistent spring-forward wall times to the first valid minute and
  chooses the earlier fall-back instant. `next_valid_latest` uses the later fall-back instant.
- A delayed tick materializes only the latest eligible occurrence and records it before advancing,
  so retries and concurrent ticks do not duplicate a firing.

Hosted migration rehearsals set `GLASSHIVE_BACKGROUND_CONSUMERS_ENABLED=false`. This passive mode
disables startup queue reconciliation, both immediate and replay/retry callback delivery, lifecycle
reapers, and the scheduler while a cloned database is inspected. Newly emitted callback records stay
durably pending for the live phase. The live single-runtime service sets the flag back to `true`; a
passive rehearsal must never deliver callbacks or execute copied queued/scheduled work.

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

Bootstrap `files` are evidence seeds by default. A reference, tool, or Skill
file that supports the worker but is not expected to appear in the final
deliverable can set `"evidence_seed": false`; the file is still copied into the
workspace but is excluded from output seed-coverage warnings.

Current behavior:

- existing host-projection defaults remain backward-compatible
- Glass Hive writes a non-secret bootstrap manifest inside the sandbox for inspection
- bundle environment is available to sandboxed runs and interactive shells
- broker/client config is additive over native worker capability: Codex/Claude host and workstation
  launches must not drop browser, computer/desktop, shell, file, or MCP capabilities just because
  GlassHive projected a broker MCP

## Claude Code on Amazon Bedrock

Claude workers can use Amazon Bedrock without changing the logical model recorded in GlassHive
evidence. Set `WPR_MODEL_CLAUDE_CODE` to the audited Claude model name and
`WPR_CLAUDE_CODE_PROVIDER_MODEL` to the Bedrock application inference profile ARN used by the CLI.
Provide the standard Claude Code Bedrock environment (`CLAUDE_CODE_USE_BEDROCK=1`, an AWS region,
and one supported AWS credential method). GlassHive projects those values only into the active run
and removes Anthropic API/OAuth credentials whenever Bedrock mode is enabled, preventing an
unintended provider fallback.

The provider-specific model value is intentionally separate from the logical model: callers can
continue to attest `claude-opus-4-8`, while Claude Code invokes the metered application inference
profile selected by the administrator. Do not place AWS credentials in bootstrap instructions,
worker metadata, source control, or logs.

## Universal AI Endpoint

GlassHive exposes an authenticated OpenAI-compatible conversation surface that is independently
callable by LibreChat or any ordinary Chat Completions client:

- `GET /v1/models` publishes exact model IDs, readiness, effort/context metadata, native
  capabilities, and whether the harness exposes incremental assistant text
- `POST /v1/chat/completions` accepts standard bearer authentication plus `model`, `messages`, and
  optional streaming; common tuning fields are tolerated for portability while unsupported
  client-orchestrated tools and response shapes fail visibly
- `POST /v1/responses` exposes the same authenticated model/session core using the standard
  Responses request, response, streaming-event, and `previous_response_id` shapes
- provider usage-quota and rate-limit failures remain explicit across both APIs: non-streaming
  requests return OpenAI-compatible HTTP `429` errors and streams emit `rate_limit_exceeded`
  failure events; GlassHive never substitutes another model or silently retries an authoring turn
- `/v1/requests/{request_id}/activity` and `/cancel` add resumable activity and explicit lifecycle
  control without making those extensions prerequisites for a standard client
- provider, MCP, capability-broker, and runtime administrator credentials are separate
- streamable-HTTP MCP requires its configured MCP service credential in local and enterprise
  deployments; stdio remains process-local and does not add an HTTP authentication layer

Viventium may attach owner/session/workspace metadata and a brokered capability bundle. Any bundle
that can project environment or harness configuration must carry a fresh HMAC signature generated
with `VIVENTIUM_GLASSHIVE_CAPABILITY_BROKER_SECRET`; a provider bearer alone cannot authorize it.
The default generic endpoint identity is workspace-only. A trusted local deployment can explicitly
grant full access, which disables harness sandbox and approval gates.

Conversation streams never expose hidden chain-of-thought. Claude stream-json currently provides
assistant-text deltas. Codex exec JSON currently provides safe activity while running and the
assistant message on completion; this is declared as `incremental_text: false` in model metadata.

## Curated Library Registry

Library items are versioned, non-secret bootstrap extensions for native Codex and Claude workers.
They do not add a second plugin runtime and cannot execute installer shell commands. A manifest must
pin its stable id, semantic version, activation hash, structured HTTPS/curated provenance, supported
profiles, requested scopes, closed non-secret JSON configuration schema, exact dependencies,
declarative `bootstrap_contract` health probe, and safe upgrade/remove behavior. The profile adapter
accepts only workspace files and the native profile's reviewed project configuration fields.

The supported lifecycle is:

1. An authenticated user or MCP worker may submit a validated proposal at
   `POST /v1/library/proposals`; this does not publish or install anything.
2. A tenant administrator with the separately enabled admin API reviews the tenant queue and uses
   `POST /v1/admin/library/proposals/{proposal_id}/review`, or a trusted registry publishes an
   already-reviewed manifest with `POST /v1/admin/library`.
3. A user or worker prepares a workspace enable/upgrade. The authenticated human reviews an
   immutable, time-bounded pending change in the browser.
4. Confirmation revalidates the complete dependency graph, runs the profile adapter and health
   probes, then atomically updates the reusable bootstrap and records the grant/probe evidence.
   Any adapter or probe failure rolls the transaction back and leaves the confirmation pending.
5. Removing the newest grant restores the exact prior bootstrap. Catalog disable/removal is a
   soft, audited lifecycle operation and fails closed while active grants or available dependents
   remain. Removed versions cannot be restored; publish a new version instead.

MCP intentionally exposes proposal, browse, prepare-upgrade, and remove-grant tools, but no publish
or confirmation tool. Scope widening during an upgrade is rejected even if proposed by a worker.

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
export GLASSHIVE_MCP_API_KEY="<dedicated-mcp-service-token>"
uv run python -m workers_projects_runtime.mcp_server --transport streamable-http --port 8767
```

## OpenClaw Runtime Release Contract

GlassHive's workstation image installs OpenClaw `2026.7.1-2` through the committed
`runtime_locks/openclaw/package-lock.json`, not through a mutable global package spec. The reviewed
lock pins the npm tarball integrity and applies the production `fast-uri@3.1.3` override. Image
generation validates the lock checksum before Docker starts, uses `npm ci --omit=dev`, verifies the
installed OpenClaw and `fast-uri` versions, and then exposes the locked CLI.

GlassHive also verifies the OpenClaw version when a workstation container is selected. Host-native
OpenClaw workers require exactly `2026.7.1-2`; older, newer, missing, and unverified installations
fail closed with recovery guidance. Both workstation and host launch environments force
`OPENCLAW_DISABLE_BONJOUR=1`, because loopback gateway binding alone does not suppress native mDNS
advertising.

The 2026-07-21 reviewed production graph reports 0 critical, 0 high, and 6 moderate npm audit
findings. A custom workstation image is supported only when it contains the same reviewed OpenClaw
runtime; GlassHive checks it before starting a worker.

## Link Lifetime Defaults

- `GLASSHIVE_LINK_REF_TTL_SECONDS` default: `0`, so `/r/{ref}` and `/v1/link-refs/{ref}` short
  links do not expire by default. Positive values expire short refs after that many seconds.
- `GLASSHIVE_LINK_REF_STATE_PATH` default: `<state-root>/glasshive/link_refs.sqlite3`. When the
  runtime emits `/r/{ref}` links that point at the separate GlassHive UI service, the runtime and UI
  must use the same local link-ref state path on the same host or supported shared storage.
- `GLASSHIVE_SIGNED_LINK_TTL_S` default: `900` seconds for raw signed-token compatibility URLs.
- Enterprise short refs are authenticated owner-scoped routes. Opening a durable `/r/{ref}` mints a
  fresh bounded worker-view session cookie; the durable ref itself is not an active compute lease.

## Test

```bash
cd <workspace-root>/viventium_v0_4/GlassHive/runtime_phase1
uv run pytest -q
```

## Current Boundary

This runtime owns the universal harness/provider boundary. It does not depend on LibreChat storage
internals. Connected capabilities are projected by an authenticated broker bundle; GlassHive
remains the native execution owner and LibreChat remains one optional conversation/UI consumer.
