# GlassHive Enterprise Cost, Security, and Provider Guardrails

This document is public-safe product truth. It must not contain customer names, real domains,
subscription ids, signed links, secrets, screenshots, logs, or private chat data.

## Scope

GlassHive v1 enterprise VM mode is one GlassHive deployment per enterprise tenant. It is designed
for authenticated users inside that one tenant to run their own workers and workspaces without
cross-user contamination. Separate customers must receive separate deployments.

This keeps the v1 equation simple: app-level tenant/user scoping on one VM, Docker workspaces for
worker isolation, and operational guardrails for cost control. It is not a promise that one Docker
host safely contains mutually hostile tenants. Stronger hostile-tenant isolation belongs in a future
substrate such as ACI, per-user VMs, gVisor, Kata, or another hardened sandbox.

## Cost Controls

Every enterprise deployment must configure and QA these controls before users are enabled:

- `GLASSHIVE_IDLE_TERMINATE_AFTER_S`: stops idle ready/completed compute while preserving state.
- `GLASSHIVE_PAUSED_TERMINATE_AFTER_S`: stops compute for paused workspaces that are left open too
  long.
- `GLASSHIVE_IDLE_REAPER_INTERVAL_S`: lifecycle reaper interval.
- `GLASSHIVE_MAX_RUN_DURATION_S`: hard cap for long-running work; also feeds worker CLI timeout
  where supported.
- `GLASSHIVE_MAX_WATCH_SESSION_DURATION_S`: caps signed View / Steer link lifetime and the active
  websocket session for already-open watch tabs.
- `GLASSHIVE_WATCH_SESSION_STATE_PATH`: persists per-user/per-worker watch-session deadlines so a
  reconnect before expiry does not reset the active-session timer.
- `GLASSHIVE_MAX_ACTIVE_WORKERS_PER_USER`: concurrent active worker cap per user.
- `GLASSHIVE_MAX_ACTIVE_WORKERS_PER_TENANT`: concurrent active worker cap for the deployment.
- `GLASSHIVE_MAX_WORKSPACES_PER_USER`: retained workspace cap per user.
- `GLASSHIVE_MAX_WORKSPACES_PER_TENANT`: retained workspace cap for the deployment.
- `GLASSHIVE_ALLOWED_WORKER_PROFILES`: allowed worker types for the deployment.
- `WPR_SANDBOX_CPUS`, `WPR_SANDBOX_MEMORY`, `WPR_SANDBOX_MEMORY_SWAP`,
  `WPR_SANDBOX_PIDS_LIMIT`, and `WPR_SANDBOX_SHM_SIZE`: Docker worker resource caps.

The v1 SQLite deployment should run one runtime service process per GlassHive VM. The runtime uses
an in-process create lock for quota check plus worker insert; multi-process or multi-replica service
scale-out requires a DB-level transaction/constraint design before it is supported.

Example capacity math: a 4 vCPU / 16 GiB VM with workers capped at 2 vCPU / 3 GiB each can support
light shared dev use, but not eight heavy workers all saturating CPU at once. A reasonable dev
starting point for up to eight users is a per-user active cap of 2, a tenant active cap of 8, and
small retained-workspace caps such as 6 per user / 32 per tenant, with budget alerts and logs watched
during rollout. If users regularly run heavy high-effort work at the same time, either lower the
tenant cap or move to an 8 vCPU VM.

## Workspace State After Compute Stops

Stopping compute is not the same as deleting the workspace. Files, artifacts, browser profile data,
and status should remain available until the retention policy removes them. The UI should distinguish
these states clearly:

- `Running` or `Ready`: live compute may be available.
- `Paused`: the workspace is resumable, but compute may be stopped.
- `Completed`: the requested work finished; artifacts/status may be available even if the live
  desktop is no longer attached after a restart.

Example: if a worker creates `report.docx` and the service restarts later, the report download can
still work while the old live desktop window is unavailable. The correct user experience is
`Completed - artifact available`, with a resume/reopen action if the user wants a fresh desktop
session in the same workspace.

## Provider Configuration

Enterprise deployments can configure OpenAI-compatible, Anthropic-compatible, and Portkey-compatible
providers with environment variables. Common defaults:

- `GLASSHIVE_DEFAULT_WORKER_PROFILE=codex-cli`
- `WPR_MODEL_CODEX_CLI=<codex-model>`
- `WPR_CODEX_CLI_BASE_URL=<openai-compatible-base-url>`
- `WPR_CODEX_CLI_ENV_KEY=<env-var-name-for-key>`
- `WPR_CODEX_CLI_WIRE_API=responses`
- `WPR_CODEX_CLI_REASONING_EFFORT=medium`
- `WPR_MODEL_CLAUDE_CODE=<claude-model>`
- `WPR_CLAUDE_CODE_USE_API_KEY=1`
- `WPR_MODEL_OPENCLAW_GENERAL=<model-or-route>`
- `WPR_OPENCLAW_BASE_URL=<openai-compatible-or-portkey-base-url>`
- `WPR_OPENCLAW_ENV_KEY=<env-var-name-for-key>`
- `WPR_OPENCLAW_WIRE_API=openai-completions`

Codex is the recommended enterprise default when a validated Responses-compatible route is
available, because it is the primary code/file/browser workspace worker. Do not switch the
deployment default to another profile just because that profile is the only route currently green;
instead, repair or reroute Codex, prove it live, and keep unsupported profiles out of the allowlist
until their own worker matrix passes.

Codex supports global default reasoning through `WPR_CODEX_CLI_REASONING_EFFORT` with values
`minimal`, `low`, `medium`, `high`, or `xhigh`. Claude Code effort is selected primarily by model
and provider configuration. GlassHive also stores per-user defaults for default worker profile and
per-profile effort. The UI and MCP expose this through authenticated preferences, so a user can make
Codex, OpenClaw, or any other profile present in `GLASSHIVE_ALLOWED_WORKER_PROFILES` the default
without affecting other users. Per-run effort overrides must stay allowlisted by runtime profile:
Codex accepts `minimal`/`low`/`medium`/`high`/`xhigh`; Claude Code currently accepts `default` or
`max`; OpenClaw currently accepts `default`, `high`, or `max`.

Portkey can be used in more than one shape, and each shape must be validated separately:

- Codex needs a Responses-compatible route (`/v1/responses`) and must prove the configured headers
  and model alias work through the Codex CLI custom provider path before becoming the default.
- OpenClaw uses an OpenAI-compatible/chat-completions style route and can use Portkey virtual keys
  when `PORTKEY_BASE_URL`, `PORTKEY_API_KEY`, and optional `PORTKEY_VIRTUAL_KEY` are configured.
- Claude Code needs a valid Anthropic Messages-compatible route and a real Claude Code worker run.
  If the Anthropic or Portkey Anthropic route returns `401`/invalid-key, keep `claude-code` out of
  `GLASSHIVE_ALLOWED_WORKER_PROFILES`.

Provider quota errors are different from GlassHive worker quotas. GlassHive quotas control how many
workers/workspaces can exist or run; provider quota errors mean the model route, billing, budget, or
rate limit rejected the request. Fix provider quota by changing the model route, budget, key, or
deployment capacity, not by raising GlassHive worker caps.

## Provider Secret Exposure

In enterprise mode, provider secrets should not be left in interactive shell startup files. The
runtime separates secret env vars into a run-only secret env file by default:

- non-secret settings stay in `$HOME/.glasshive/runtime.env`
- secret settings go into `$HOME/.glasshive/secret-runtime.env`
- secret files are written with owner-only file permissions
- the run script sources the secret file immediately before starting the worker command
- the secret file is removed before the worker command runs
- secret env vars are unset before the post-run takeover shell remains open

This reduces accidental key disclosure in View / Steer and terminal takeover. It is not a complete
defense against malicious prompts while the model process is running, because a process that is
given a shared provider key may still be induced to reveal it. Production deployments should prefer
short-lived per-user virtual keys, provider-brokered credentials, or a host-side provider broker
that never places the raw shared provider key inside the worker process.

## Network Guardrails

Enterprise VM deployments must block worker containers from Azure metadata endpoints:

- `169.254.169.254/32` Azure Instance Metadata Service
- `168.63.129.16/32` Azure platform endpoint, except DNS when required

Use host firewall rules in the Docker `DOCKER-USER` chain so container traffic is blocked even if a
worker process tries to fetch managed identity or platform metadata. This is an operations control
and must be verified live with a synthetic container/network probe.

## Auth and Isolation

Enterprise mode must:

- require service authentication before trusting user assertion headers
- derive `tenant_id` and `owner_id` server-side from `AuthContext`
- ignore model/tool-supplied `owner_id`
- scope project, worker, run, artifact, watch, and UI queries by tenant/user
- fail closed on missing, ambiguous, forged, expired, or wrong-tenant auth
- keep non-health routes, docs, UI, MCP, watch, artifact, and websocket surfaces gated

Signed View / Steer and artifact links must be short-lived and scoped to the worker owner. Do not
store valid signed links in reports, logs, or public artifacts.

Signed-link TTL is not enough by itself. If a user opens a watch session before the link expires,
the browser can keep an active websocket open. `GLASSHIVE_MAX_WATCH_SESSION_DURATION_S` must also
close that live websocket after the configured duration. In plain terms: link expiry stops a new
door from opening; active session timeout also closes the door that is already open.

When `GLASSHIVE_WATCH_SESSION_STATE_PATH` is configured, the UI records a
tenant/user/worker-specific watch deadline. Re-minting the same worker link before expiry keeps the
original deadline; after expiry, the old link and websocket session fail closed. A newly minted
owner-scoped link from the authenticated GlassHive UI or MCP/status flow can start a fresh watch
session for that same owner.

Upload/data-plane QA is part of the enterprise contract, not a separate convenience feature. For
LibreChat integrations, the worker must receive actual uploaded bytes through a read-only shared
upload mount such as `WPR_LIBRECHAT_UPLOADS_ROOT`, and enterprise mode requires owner-scoped
metadata paths shaped like `/uploads/<authenticated-user-id>/<file>`. Filename metadata alone is
not a passing upload result for binary/PDF/workbook work.

Status/wait QA must also cover stale requested runs. If a user asks about an older failed run after
the same worker later completed, GlassHive must preserve the requested run outcome while surfacing
the latest effective run and artifacts.

Metadata blocking should be continuously probed, not only manually checked at deployment. A scheduled
probe starts a disposable worker-network container and tries to reach Azure metadata endpoints. The
probe must first prove Docker/curl can run, then fail if the endpoint returns either HTTP success or
HTTP error, because any HTTP response means the endpoint is reachable. If metadata reachability ever
succeeds, firewall drift has occurred and operators should be alerted.

## QA Requirements

The Standard GlassHive QA cases must include:

- two-user cross-contamination probes
- unauthenticated/wrong-token/wrong-tenant probes
- artifact traversal probes
- signed-link tamper and expiry probes
- lifecycle reaper checks for idle, paused, and max-duration runs
- resource quota checks for per-user, per-tenant, and profile allowlist limits
- provider secret exposure checks that interactive shell startup files do not contain raw keys
- Azure metadata blocking checks for Docker workers in cloud deployments
- scheduled metadata-block drift probe checks
- active View / Steer websocket lifetime checks, not only signed-link creation checks
- persisted watch-session deadline checks so reconnects and runtime callback links reuse the
  existing deadline before expiry and fail after expiry
- per-user worker/default effort preference checks through UI and MCP
- deployment-default checks proving `workspace_launch` without `profile` uses the configured
  `GLASSHIVE_DEFAULT_WORKER_PROFILE`
- visible user-path checks for launch, View / Steer, status/wait, upload, artifact download, stop,
  resume, refresh/restart, and completed-state wording

Logs, DB rows, source inspection, and automated tests are supporting evidence. Browser/user-path QA
is still required for browser-visible behavior.
