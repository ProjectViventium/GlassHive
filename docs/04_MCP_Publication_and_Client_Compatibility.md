<!-- VIVENTIUM START: Glass Hive MCP publication -->
# Glass Hive: MCP Publication and Client Compatibility

## MCP Direction

Glass Hive should follow the current modern MCP shape:

- `streamable-http` as the primary remote transport
- `stdio` for local direct attachment
- `sse` only for compatibility where still needed
- stable tool names and structured outputs
- a thin MCP adapter over the control-plane API

## Why This Matches Current Practice

Current official guidance from Claude Code and modern MCP tooling points in the same direction:

- remote HTTP MCP is the recommended remote transport
- SSE is deprecated where HTTP is available
- clients benefit from dynamic tool updates through `list_changed`
- scope-aware MCP configuration matters for safety and portability

## Client Strategy

### LibreChat / Viventium / Standalone-Compatible Clients
Use Glass Hive as an external MCP server. The default enterprise path is config-only and does not
require LibreChat application-code changes.

For enterprise worker delegation, the host application injects a service-authenticated user
assertion and request context into the MCP call. The neutral standalone header set is:

- `X-GlassHive-Service-Token`
- `X-GlassHive-Tenant-Id`
- `X-GlassHive-User-Id`
- `X-GlassHive-User-Email`
- `X-GlassHive-User-Role`
- optional request context: `X-GlassHive-Agent-Id`, `X-GlassHive-Conversation-Id`,
  `X-GlassHive-Parent-Message-Id`, `X-GlassHive-Message-Id`, `X-GlassHive-Surface`,
  and `X-GlassHive-Input-Mode`
- optional upload context: `X-GlassHive-Request-Files`,
  `X-GlassHive-Request-Attachments`, `X-GlassHive-Tool-Resources`, and
  `X-GlassHive-File-Ids`

Viventium-prefixed headers and `X-WPR-Token` remain supported aliases for existing local
integrations, but new non-Viventium clients should use the neutral GlassHive names. The MCP adapter
folds request context into `bootstrap_bundle` without reading LibreChat internals. Upload metadata
is projected into `bootstrap_bundle.files` when a local path or extracted text is available; when the
chat model cannot see an uploaded file body, it should still launch the worker with the file
reference and requested outcome so GlassHive can use the trusted upload metadata supplied by the
host.

Callbacks are optional. Viventium can use signed callbacks for durable completion delivery, but
standalone/plain LibreChat deployments must still work without them by using:

- `workspace_status` for non-blocking follow-up checks
- `workspace_wait` for explicit blocking waits
- `workspace_artifacts` and `workspace_artifact_download` for generated files, default signed
  downloads, and preview/open links
- `workspace_preferences_get` and `workspace_preferences_set` for per-user worker and effort
  defaults
- the returned View / Steer URL for operator visibility and takeover

Default `workspace_launch`, `workspace_wait`, and `workspace_status` payloads are intentionally
compact. They return user-actionable state, result tools, output text, artifact short links, and a
View / Steer short link when available; raw project/worker/run ids and live diagnostic snapshots are
returned only when the caller explicitly requests diagnostics. MCP outputs must not expose raw
`gh_token` URLs or opaque signed-link tokens; they should expose `/r/{ref}` and `/v1/link-refs/{ref}`
indirection instead.

For generated file delivery, `signed_download_url`/`default_url` is the default chat-facing artifact
link and should be labeled `Download file`. The MCP payload should also preserve `signed_open_url`
or a View / Steer workspace link so the user can inspect previews and all workspace deliveries
without exposing raw worker paths, raw `gh_token` URLs, or whole generated file contents in chat.
Preview pages are part of that contract: their `Download file` action should download through a
scoped artifact short ref, and their `View workspace` action should use the same authenticated
`/r/{ref}` workspace short-ref path as chat View / Steer links, redirecting to a tokenless watch URL
after minting a bounded session cookie.

Short refs are the durable user-facing link contract. `GLASSHIVE_LINK_REF_TTL_SECONDS` defaults to
`0` (`never`/`none`/`disabled`/`off`/`false`/`no` are equivalent), so a completed artifact or
View / Steer link remains usable for the authenticated owner after the underlying signed token has
expired. Set a positive integer number of seconds only when the deployment deliberately wants
short refs to expire. Enterprise clients must treat `/r/{ref}` and `/v1/link-refs/{ref}` as
authenticated routes, not bearer links.

When MCP/runtime payloads emit `/r/{ref}` URLs whose host is the separate GlassHive UI service, the
runtime and UI must share `GLASSHIVE_LINK_REF_STATE_PATH` or otherwise route the ref to the process
that created it. This is a deployment contract, not a LibreChat behavior.

The MCP descriptions must make this non-callback path obvious to connected LLMs. A model should
launch work with `workspace_launch`, include `uploaded_files` when the user attached files, return
the View / Steer URL promptly, and use status/wait/artifact tools for follow-up and delivery. It
must not fall back to pasting whole generated files into chat or writing its own inferior local
code path when GlassHive has already produced a signed artifact link.

`workspace_launch` is intentionally non-blocking. If the user asks to "wait", the model should call
`workspace_wait` with the returned completion wait timeout or rely on same-conversation scoped
recent-dispatch resolution. If the requested run is older than the worker's latest run,
diagnostic status/wait responses preserve both the requested run outcome and the latest effective
run so an operator can explain the lineage without exposing raw ids in normal user-facing payloads.
Blocking waits should stay below common chat/Redis/proxy idle windows. When a bounded wait reaches
its deadline while the worker is still running but user-facing files already exist in delivery
locations such as `output/`, `deliverables/`, `reports/`, `artifacts/`, or `out/report`/`out/data`,
`workspace_wait` may return `status=deliverable_ready` with signed links. That is a user-delivery
state, not a claim that the process row is completed; callers should deliver the files and use
`workspace_status` later only if the user asks for a final status refresh. MCP progress notifications
are best-effort and bounded; a stale progress/SSE channel must not block the tool's status payload.

Fresh `workspace_launch` calls must not accidentally resume stale workspaces. A supplied
`workspace_alias` is honored only when `reuse_existing_workspace=true`; otherwise GlassHive creates a
fresh one-off project/worker for the new request. Use explicit reuse only when the user asked to
resume or reuse that existing workspace. Deliberate operator-level reuse remains available through
`worker_find_or_resume`, `workspace_continue`, and explicit lifecycle tools.

The same rule applies to lower-level `worker_delegate_once` calls. A supplied `alias` or existing
`project_id` does not imply reuse for a fresh one-off task; GlassHive creates a fresh worker alias
unless `reuse_existing_workspace=true` is set. This keeps model-selected fallback paths from
reintroducing stale worker history after `workspace_launch` has already been made fresh-by-default.

When no `profile` is supplied, MCP tools must use the authenticated user's saved preference first
and then the deployment default from `GLASSHIVE_DEFAULT_WORKER_PROFILE`. Enterprise deployments
should keep Codex (`codex-cli`) as the default once its Responses-compatible route is validated.
OpenClaw and Claude Code remain selectable only when present in `GLASSHIVE_ALLOWED_WORKER_PROFILES`
and proven by the worker matrix.

For upload byte transfer, the host should provide every supported file/request header, including
`X-GlassHive-Request-Files`, `X-GlassHive-Request-Attachments`, `X-GlassHive-Tool-Resources`, and
`X-GlassHive-File-Ids`. In enterprise shared-storage deployments, file metadata should point to
owner-scoped virtual paths such as `/uploads/<authenticated-user-id>/<file>` so GlassHive can copy
the bytes into the worker workspace under `uploads/<safe-filename>`. When a host authenticates users
by SSO/email but stores uploads under an internal user id, it must also send
`X-GlassHive-Storage-User-Id` (or `X-Viventium-Storage-User-Id`) with the internal storage owner.
GlassHive uses that value only for upload byte lookup; the authenticated user id still owns the
workspace, callbacks, and signed links.

Some older LibreChat-compatible hosts can pass authenticated user/message headers but cannot project
`files` or `attachments` into MCP request headers without a LibreChat image upgrade. For those hosts,
GlassHive provides an opt-in compatibility fallback:
`GLASSHIVE_LIBRECHAT_UPLOAD_COMPAT_FALLBACK=true`. The fallback is disabled by default. When enabled,
it only runs in enterprise mode, only with `X-GlassHive-Storage-User-Id`, `X-GlassHive-Conversation-Id`,
and `X-GlassHive-Message-Id` present, and only scans that storage owner's upload folder under the
configured upload roots. It materializes files modified within
`GLASSHIVE_LIBRECHAT_UPLOAD_COMPAT_RECENT_SECONDS` (default `900`, clamped to 5 seconds through 24
hours), capped by `GLASSHIVE_LIBRECHAT_UPLOAD_COMPAT_MAX_FILES` (default `8`, max `32`). Prefer real
request-file headers whenever the host supports them; the compatibility fallback is for legacy
bridges, not the primary contract. Keep the fallback window close to expected upload-to-dispatch
latency and monitor its log event; long windows can pick unrelated recent uploads from the same
storage owner because the fallback cannot prove exact message membership.

### Claude / Claude Code
Support:

- stdio attachment for local use
- HTTP MCP for remote/local shared use
- project-scoped `.mcp.json` bootstrap where appropriate

### Codex / ChatGPT-compatible MCP consumers
Prefer remote HTTP MCP with auth in front of the server when not loopback-only.

## Publication Rules

For local-only use:

- loopback-only HTTP is acceptable
- bearer auth is optional but recommended

For broader publication:

- require auth in front of the MCP server
- keep write-capable tools explicit and well-described
- do not duplicate runtime logic inside the MCP layer
- `execution_mode=host` must be an explicit tool argument; the MCP server should not infer host
  execution from natural-language phrasing

For Azure enterprise VM mode, the default LibreChat integration remains config-only: LibreChat sends
the neutral `X-GlassHive-*` service, identity, request, and upload headers to the remote GlassHive
MCP endpoint over a locked-down channel. GlassHive trusts identity headers only after service-token
validation and ignores model/tool-supplied `owner_id`. Optional MCP OAuth/OIDC can be enabled for
enterprises that want a second consent flow, but it is not the seamless default. Until server-side
token validation is present, GlassHive runtime authorization stays `first_party_assertion` and
OAuth/OIDC auth modes fail closed by default.

## Compatibility Alias

The current Viventium stack already refers to the MCP integration as `workers_projects_runtime`.

Glass Hive should preserve compatibility while documenting the product name clearly.
