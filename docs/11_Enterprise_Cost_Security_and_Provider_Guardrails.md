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
- `GLASSHIVE_LINK_REF_TTL_SECONDS`: durable short-ref lifetime. Default `0` means `/r/{ref}` and
  `/v1/link-refs/{ref}` do not expire; values `never`, `none`, `disabled`, `off`, `false`, and
  `no` also mean no expiry. Set a positive integer number of seconds only for deployments that
  intentionally want user-facing short refs to expire.
- `GLASSHIVE_LINK_REF_STATE_PATH`: SQLite state file for short refs. If the runtime process creates
  View / Steer refs that redirect to a separate GlassHive UI service, both processes must point at
  the same state file on the same host or supported shared storage. Do not put SQLite WAL state on
  unsafe network filesystems.
- `GLASSHIVE_MAX_WATCH_SESSION_DURATION_S`: caps signed View / Steer session-token lifetime and
  the active websocket session for already-open watch tabs. This is separate from durable short
  refs: a user can reopen a durable owner-scoped link later, but the forgotten active tab does not
  have to keep an active session alive forever.
- `GLASSHIVE_WATCH_SESSION_STATE_PATH`: persists per-user/per-worker watch-session deadlines so a
  reconnect before expiry does not reset the active-session timer.
- `GLASSHIVE_MAX_ACTIVE_WORKERS_PER_USER`: concurrent active worker cap per user.
- `GLASSHIVE_MAX_ACTIVE_WORKERS_PER_TENANT`: concurrent active worker cap for the deployment.
- `GLASSHIVE_MAX_WORKSPACES_PER_USER`: retained workspace cap per user.
- `GLASSHIVE_MAX_WORKSPACES_PER_TENANT`: retained workspace cap for the deployment.
- `GLASSHIVE_ALLOWED_WORKER_PROFILES`: allowed worker types for the deployment.
- `WPR_SANDBOX_CPUS`, `WPR_SANDBOX_MEMORY`, `WPR_SANDBOX_MEMORY_SWAP`,
  `WPR_SANDBOX_PIDS_LIMIT`, and `WPR_SANDBOX_SHM_SIZE`: Docker worker resource caps.

Termination is verified, not assumed. Docker removal must be followed by a fresh inspect proving the
container is gone, the service rejects a termination result that still reports active compute, and
the always-on orphan reconciler removes any container left behind a terminated or failed worker
record, including paused or exited containers. Set `GLASSHIVE_ORPHAN_REAPER_ENABLED=false` only when
an external reconciler provides the same guarantee. Stop reasons are scoped to the active run so an
idle pause, interrupt, cleanup, or prior termination cannot poison a later resumed task. If a stop
marker races with successful process completion, GlassHive recovers the completed run evidence
before applying the stale marker.

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
- `WPR_CODEX_CLI_REASONING_EFFORT=high` after the active route is proven for high; use `medium`
  only when the provider route or workload QA requires it.
- `WPR_MODEL_CLAUDE_CODE=<claude-model>`
- `WPR_CLAUDE_CODE_USE_API_KEY=1`
- `WPR_CLAUDE_CODE_ENABLE_CHROME=1` unless this deployment is explicitly locked down
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
`none`, `minimal`, `low`, `medium`, `high`, or `xhigh`, subject to the active provider route's tested
allowlist. Enterprise deep-work deployments should prove and use `high` by default for Codex; `xhigh`
is reserved for explicitly hard work once direct route probes and real worker runs prove it.
Claude Code effort is selected by model/provider configuration plus the native `--effort` flag when
a run asks for `max` or `xhigh`. GlassHive must project the exact requested value through
`WPR_CLAUDE_CODE_EFFORT` in the worker bootstrap env for MCP, UI, and direct API assignments, the
generated worker command must preserve that value, and host-native preflight/command generation must
fail closed if the configured
Claude CLI does not expose `--effort`. Claude Code workers should also preserve native
Chrome/browser substrate with `--chrome` by default when the CLI supports it.
Codex feature lockdown is opt-in only: `WPR_CODEX_CLI_DISABLE_FEATURES` must be unset by default so
native app, multi-agent, plugin, browser/computer, workspace-dependency, and adjacent capability
surfaces remain available unless an operator intentionally locks them down with QA evidence.
GlassHive also stores per-user defaults for default worker profile and per-profile effort. The UI and MCP expose this
through authenticated preferences, so a user can make Codex, OpenClaw, or any other profile present
in `GLASSHIVE_ALLOWED_WORKER_PROFILES` the default without affecting other users. Per-run effort
overrides must stay allowlisted by runtime profile:
Codex accepts `none`/`minimal`/`low`/`medium`/`high`/`xhigh`; Claude Code accepts `default`, `max`, or
`xhigh` on the pinned current CLI; OpenClaw currently accepts `default`, `high`, or `max`.

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

Provider-account cards show only what GlassHive directly observed: account-bound worker runs,
failed outcomes, elapsed worker-dispatch time, and—only when the worker harness reports them—input
and output tokens. Lease acquisition does not count as a run, and the same accounting boundary
applies to native subscription homes and brokered API-key or enterprise routes. These counters do
not claim provider-side subscription usage, remaining quota, billing, or rate-limit state.

## Per-user LibreChat inference broker

An optional LibreChat inference broker can let a Codex mission worker use the authenticated user's
encrypted OpenAI API key or an approved enterprise OpenAI route without exposing that credential to
GlassHive. GlassHive stores only a provider-account reference. When a run actually starts, it mints
a 60-second issuer assertion, receives an in-memory grant bound to the exact broker tenant, user,
worker, run, adapter, route, and model list, and revokes that grant on completion, failure,
interruption, or termination. Workspaces, templates, schedules, the control-plane database, and run
evidence must never contain a broker grant.

The deployment owns these settings:

- `GLASSHIVE_INFERENCE_BROKER_URL`: fixed HTTPS issuer/revocation endpoint
- `GLASSHIVE_INFERENCE_BROKER_PROXY_BASE_URL`: fixed HTTPS broker route prefix when different
  from the issuer URL (for example, `https://host.example/api/viventium/glasshive/inference`).
  Do not append `/openai/v1`; the reviewed adapter adds that fixed suffix itself.
- `GLASSHIVE_INFERENCE_BROKER_SECRET`: shared signing secret, at least 32 characters
- `GLASSHIVE_INFERENCE_BROKER_TENANT_ID`: LibreChat broker tenant
- `GLASSHIVE_INFERENCE_BROKER_OWNER_BINDINGS_JSON`: reviewed canonical GlassHive-owner to
  LibreChat-user mappings. Each list entry declares `glasshive_tenant_id`, `glasshive_owner_id`,
  `librechat_user_id`, and either `operator_verified` or `shared_oidc_subject` proof.

The enterprise OpenAI-compatible origin is a trusted deployment setting, not user input. It may be
an approved private enterprise gateway, so public-IP-only validation would break legitimate
deployments; operators must review its DNS, TLS, network egress, and credential boundary before
enabling it. Browser, MCP, worker, and grant requests cannot override the origin, suffix, adapter,
authorization header, or extra upstream headers. The broker uses a fixed suffix, disables automatic
redirect following, and rejects every upstream `3xx` response. Personal API-key traffic always uses
the fixed OpenAI API origin and ignores stored or caller-supplied base URLs and headers.

The mapping is mandatory and exact; GlassHive fails closed rather than assuming that similarly
named accounts are the same principal. The Codex route advertises only `openai_responses_v1` and
projects the grant through the provider API-key environment plus fixed worker/run headers. Current
Codex accepts only the Responses wire protocol for custom providers; a Chat Completions broker
adapter may remain available to separately verified harnesses but must not be projected into Codex.
See the [official Codex configuration reference](https://developers.openai.com/codex/config-reference).
This route does not imply or advertise Claude consumer OAuth or Codex/ChatGPT subscription OAuth.

## Native personal-account container boundary

Multi-user native subscription missions fail closed unless the deployment declares the reviewed
`per_worker_container` isolation mode. In that mode GlassHive mounts only the selected account home
at `/workspace/.provider-account`, points Codex or Claude at its provider-specific child directory,
and removes conflicting deployment gateway/key environment variables. Rootless Docker maps the
container's non-root worker to a subordinate host uid, so a correct bind mount of the host-owned
`0700`/`0600` tree is not sufficient by itself. The reviewed workstation image includes POSIX ACL
support; startup grants and then verifies access for only the container worker user. There is no
world-writable fallback. GlassHive removes the credential-bearing container and tightens the
credential tree again before releasing the short, heartbeated exclusive mission lease.

If the ACL grant/access check, stale-mount reconciliation, container removal, permission tightening,
or lease heartbeat fails, the mission stops and the account becomes action-required. Deployments
without this exact substrate keep reporting `isolated_substrate_required` instead of falling back to
the deployment-wide provider route.

Direct connected-service capabilities use a separate issuer endpoint but the same no-token-copy
boundary. In `shared_oidc_subject` mode, GlassHive and LibreChat must use the exact same configured
OIDC issuer and principal claim. LibreChat derives and uniquely indexes GlassHive's opaque
`usr_<issuer+subject hash>` only during authenticated OIDC login; existing users backfill on
re-login, a missing/wrong issuer or claim creates no link, and email is never a fallback. An
`operator_verified` deployment instead uses the explicit owner mapping above. Direct assertions
are tenant/action bound, grant and revoke are worker/run bound, and each signed nonce is checked in
the shared replay cache before user or grant work. Status is read-only, grant identity is
deterministic for the exact user/worker/run, and revoke is idempotent, so even a simultaneous cache
race cannot widen scope or duplicate a distinct grant.

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

Raw signed View / Steer and artifact tokens must be short-lived and scoped to the worker owner.
Public tool payloads, callbacks, preview pages, and member UI actions should expose only short link
references such as `/r/{ref}` or `/v1/link-refs/{ref}`. Those short refs are durable authenticated
pointers by default (`GLASSHIVE_LINK_REF_TTL_SECONDS=0`), not bearer credentials and not compute
leases. Enterprise `/r/{ref}` and `/v1/link-refs/{ref}` handlers must require the authenticated
tenant/user to match the ref payload before minting a fresh bounded worker cookie or returning an
artifact. Raw `gh_token` URLs and opaque `/v1/signed-links/{token}` targets are credential-bearing
implementation details and must stay server-side or legacy inbound-only. `/r/{ref}` handlers should
redirect to a tokenless watch/project/desktop URL, so the browser address bar and follow-up UI/API
polling do not retain `gh_token`. Do not store valid signed links in reports, logs, or public
artifacts.

The shipped GlassHive browser surfaces use local assets and system font stacks; authenticated or
opaque-reference pages must not fetch third-party font resources. Log redaction applies to both
plain strings and structured URL objects supplied by HTTP clients, so an opaque ref cannot bypass
the filter merely because a logging library preserved it as a URL instance.

Artifact delivery should expose the scoped download short ref as the default chat-facing file link,
labeled `Download file`, while preserving a preview/open short ref or View / Steer workspace link
for inspection and all-deliveries access. A direct download default is a UX choice, not a weaker
security boundary: enterprise `/v1/link-refs/{ref}` download routes still require the authenticated
tenant/user to match the signed ref payload before returning bytes.

Signed-link TTL is not enough by itself. If a user opens a watch session before the link expires,
the browser can keep an active websocket open. `GLASSHIVE_MAX_WATCH_SESSION_DURATION_S` must also
close that live websocket after the configured duration. In plain terms: link expiry stops a new
door from opening; active session timeout also closes the door that is already open.

When `GLASSHIVE_WATCH_SESSION_STATE_PATH` is configured, the UI records a
tenant/user/worker-specific watch deadline. Re-minting a worker session before expiry keeps the
original deadline; after expiry, the old active token and websocket session fail closed. Opening the
same durable authenticated short ref later can mint a new bounded session for the same owner and
reattach to the retained workspace/artifacts without treating the inactive time as active compute.

Upload/data-plane QA is part of the enterprise contract, not a separate convenience feature. For
LibreChat integrations, the worker must receive actual uploaded bytes through a read-only shared
upload mount such as `WPR_LIBRECHAT_UPLOADS_ROOT`, and enterprise mode requires owner-scoped
metadata paths shaped like `/uploads/<authenticated-user-id>/<file>`. Filename metadata alone is
not a passing upload result for binary/PDF/workbook work. If the host authenticates a human by
email/SSO while the upload share is keyed by an internal LibreChat user id, the host must send the
internal id as `X-GlassHive-Storage-User-Id` or `X-Viventium-Storage-User-Id`. That storage identity
is authorized only for upload byte lookup and must not replace the authenticated owner used for
workspace/link access.

If a legacy host cannot project request `files`/`attachments` without changing its LibreChat image,
`GLASSHIVE_LIBRECHAT_UPLOAD_COMPAT_FALLBACK=true` may be enabled as a bounded compatibility mode.
It must stay owner-scoped to the storage id, request-context-gated by conversation/message headers,
and time-bounded by `GLASSHIVE_LIBRECHAT_UPLOAD_COMPAT_RECENT_SECONDS` (default `900`). Without real
file metadata or an approved DB resolver, the fallback cannot prove per-message file membership, so
the normal upload-header contract remains the preferred architecture. It must never scan a global
upload folder, pick another owner's file, or replace the normal upload-header contract when that
contract is available. Keep the time window close to real upload-to-dispatch latency and require an
operator-visible fallback-use log; broad windows can copy unrelated same-owner uploads into a worker
and violate literal file-input expectations.

Status/wait QA must also cover stale requested runs. If a user asks about an older failed run after
the same worker later completed, GlassHive must preserve the requested run outcome while surfacing
the latest effective run and artifacts.

Metadata blocking should be continuously probed, not only manually checked at deployment. A scheduled
probe starts a disposable worker-network container and tries to reach Azure metadata endpoints. The
probe must first prove Docker/curl can run, then fail if the endpoint returns either HTTP success or
HTTP error, because any HTTP response means the endpoint is reachable. If metadata reachability ever
succeeds, firewall drift has occurred and operators should be alerted.

## Content-safe run telemetry

Telemetry is bound to the active `run_id`; a newer queued run cannot replace the executing run in
operator metrics. Claude stream telemetry stores counters and timings only, never prompts, reasoning,
tool inputs, invoice content, or credentials.

Active JSONL streams are consumed incrementally in bounded chunks. Only complete appended lines are
counted. An ordinary unfinished line remains buffered, while an unterminated record larger than
1 MiB is counted as malformed and discarded until its newline so monitoring memory cannot grow
with untrusted stream content. Each sample reports its run identity, scope, sequence, parsed bytes,
log bytes, oversized/malformed counts, and last observed progress. A missing requested run is
reported as unavailable instead of silently substituting a console tail.

`GET /v1/workers/{worker_id}/telemetry` is the lightweight monitoring endpoint. The full `/live`
endpoint supports `compact=1` during active polling to skip recursive workspace, image, and
deliverable discovery. Runtime snapshots are written before success, process failure, timeout,
pause, interruption, or termination so terminal evidence survives an operator cancellation.
Persisted and public telemetry are strict, bounded allowlists of counters, timestamps, and runtime
identifiers. Snapshot writes are atomic and best-effort: an observability I/O failure cannot turn a
completed workload into a failed one.

The telemetry endpoint's active/latest run references are also limited to run identity, state, and
timestamps. Prompts, instructions, model output, and error text remain available only through the
explicitly operator-private detailed surfaces and are never part of the lightweight telemetry
contract.

## QA Requirements

The Standard GlassHive QA cases must include:

- two-user cross-contamination probes
- unauthenticated/wrong-token/wrong-tenant probes
- artifact traversal probes
- signed-link tamper and expiry probes
- lifecycle reaper checks for idle, paused, max-duration, and terminal-worker orphan compute,
  including failed records and non-running containers with timeout thresholds otherwise disabled
- active-run termination race checks proving a background processor cannot resurrect a terminated
  worker or leak a pause, interrupt, or termination reason into a later run; browser, API, and MCP
  writes against a closed workspace must return the same actionable conflict and persist no run or
  schedule, including when permanent closure wins a concurrent reservation race; teardown failure
  remains a durable closed state that startup may retry, cached desktop/terminal access is rejected,
  active desktop/terminal streams are revoked, recurring definitions cannot be re-enabled or remain
  active in a delegated owner, the external runtime start is fenced with durable runtime identity,
  truncated provider streams fail, and stale runtime writers cannot reopen the workspace
- active telemetry checks proving run identity cannot drift to a newer queued run, counters remain
  monotonic across partial JSONL writes, oversized unterminated records remain memory-bounded, and
  large unchanged logs are not reparsed
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
