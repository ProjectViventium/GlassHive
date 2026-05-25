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
- `workspace_artifacts` and `workspace_artifact_download` for generated files and signed downloads
- `workspace_preferences_get` and `workspace_preferences_set` for per-user worker and effort
  defaults
- the returned View / Steer URL for operator visibility and takeover

The MCP descriptions must make this non-callback path obvious to connected LLMs. A model should
launch work with `workspace_launch`, include `uploaded_files` when the user attached files, return
the View / Steer URL promptly, and use status/wait/artifact tools for follow-up and delivery. It
must not fall back to pasting whole generated files into chat or writing its own inferior local
code path when GlassHive has already produced a signed artifact link.

`workspace_launch` is intentionally non-blocking. If the user asks to "wait", the model should call
`workspace_wait` with the returned follow-up context. If the requested run is older than the
worker's latest run, status/wait responses preserve both the requested run outcome and the latest
effective run so the model can say, for example, "the run you asked about failed, but the same
workspace later completed and produced these artifacts" instead of falsely reporting failure.

When no `profile` is supplied, MCP tools must use the authenticated user's saved preference first
and then the deployment default from `GLASSHIVE_DEFAULT_WORKER_PROFILE`. Enterprise deployments
should keep Codex (`codex-cli`) as the default once its Responses-compatible route is validated.
OpenClaw and Claude Code remain selectable only when present in `GLASSHIVE_ALLOWED_WORKER_PROFILES`
and proven by the worker matrix.

For upload byte transfer, the host should provide every supported file/request header, including
`X-GlassHive-Request-Files`, `X-GlassHive-Request-Attachments`, `X-GlassHive-Tool-Resources`, and
`X-GlassHive-File-Ids`. In enterprise shared-storage deployments, file metadata should point to
owner-scoped virtual paths such as `/uploads/<authenticated-user-id>/<file>` so GlassHive can copy
the bytes into the worker workspace under `uploads/<safe-filename>`.

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
