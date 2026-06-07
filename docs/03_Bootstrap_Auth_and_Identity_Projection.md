<!-- VIVENTIUM START: Glass Hive bootstrap and auth -->
# Glass Hive: Bootstrap, Auth, and Identity Projection

## Answer to the Core Question

Yes, Glass Hive can project the same effective access a user already has into spawned sandboxes.

But the right implementation differs by source:

1. local CLI login state
2. direct API keys
3. connected accounts managed by another product such as Viventium / LibreChat

These should not be treated as the same thing.

## The Wrong Pattern

Do not make Glass Hive depend directly on another app's private storage layout or database tables.

Examples of what to avoid as the product contract:

- scraping LibreChat DB records directly inside sandbox boot
- hard-coding Anthropic/OpenAI token table formats into Glass Hive
- copying an entire host home directory into every sandbox

Those approaches are fragile, unsafe, and make the runtime non-portable.

## The Right Pattern

Use a portable worker bootstrap contract:

- `bootstrap_profile`
- `bootstrap_bundle`

### bootstrap_profile
A high-level preset that selects a default projection style.

Examples:

- `clean-room`
- `host-login`
- `codex-host`
- `claude-host`

### bootstrap_bundle
A structured optional payload for additive projection.

Current phase-1 fields supported by the runtime:

- `env`
- `files`
- `claude_project_mcp`
- `claude_settings_local`
- `codex_config_append`
- `claude_md`
- `codex_md`
- `agents_md`
- `system_instructions`
- `callbacks`
- `project_definition`

Host-native workers also materialize visible prompt/context files in the host workspace:

- `project-definition.md`
- `work-log.md`
- `harness-prompt.md`
- `AGENTS.md` as the canonical Codex-style project instruction file
- `agents.md` as a compatibility mirror
- `claude.md` / `CLAUDE.md` as Claude Code compatibility files that import or mirror `AGENTS.md`
- `codex.md` / `CODEX.md` as legacy compatibility mirrors only

Bootstrap-projected instructions must include a general completion self-check. Before a worker writes
its final user-facing report, it should inspect the concrete result it produced, compare that result
with the user's request, success criteria, constraints, and relevant files/artifacts/tool output, and
continue or repair when it can. This must stay universal: the bootstrap should not hardcode one
LibreChat prompt, one QA case, one provider, or one file type.

The projection contract is sparse by design. The host may advertise MCP/tool capability, broker
grants, uploads, and retrieved context, but it must not invent goals, success criteria, tool results,
downloadable artifacts, or provider-specific workflows. The worker receives real capability context
and decides the best path.

When a trusted host client passes existing upload metadata, GlassHive reuses the existing file path
contract instead of adding a second upload route:

- virtual `/uploads/...` paths can map to `WPR_LIBRECHAT_UPLOADS_ROOT`
- owner-scoped uploaded bytes can be resolved by original filename when the host model exposes only
  filename/text context
- extracted text can be materialized directly
- metadata-only attachments become an `uploads/*.metadata.json` manifest

Any actual `source_path` copy is still gated by `WPR_BOOTSTRAP_SOURCE_ROOTS`, symlink rejection, and
the bootstrap size limit. Request-derived metadata must never become an arbitrary host-file read.
GlassHive must be file-type agnostic: PDF, DOCX, XLSX, PPTX, media, archives, and unknown extensions
are treated as user artifacts for the wrapped worker to reason about. Extracted text is a convenience
input, not a replacement for the original bytes when the user asks for layout-preserving redaction,
editing, analysis, conversion, or returned downloadable files. If the original bytes cannot be
projected safely, GlassHive should emit a metadata/blocker manifest and report the blocker rather
than silently converting the problem into a `.txt` task.

Enterprise LibreChat deployments that use the normal LibreChat upload/file-transfer flow should
mount the same upload storage read-only into the GlassHive VM and point both
`WPR_LIBRECHAT_UPLOADS_ROOT` and `WPR_BOOTSTRAP_SOURCE_ROOTS` at that mount. For Azure Container
Apps plus VM deployments, this usually means mounting the LibreChat Azure Files `uploads` share at
the GlassHive upload root. LibreChat can then stay config-only: it passes
`{{LIBRECHAT_BODY_FILES_JSON_B64}}` metadata to GlassHive, GlassHive resolves the virtual
`/uploads/<user>/<file>` path against the trusted mounted share, and the worker receives a copied
workspace file before launch. GlassHive also amends the project instructions with the canonical
`uploads/<safe-filename>` paths so a worker does not depend on user-visible filenames that contain
spaces or unsafe characters.

In enterprise mode, GlassHive treats the first segment after `/uploads/` as the authenticated
owner/user id. That is the cross-user safety boundary for shared upload storage. If a LibreChat
deployment stores upload metadata as `/uploads/<conversation-id>/...`, `/uploads/<file-id>/...`, or
any other layout where the first segment is not the authenticated user id, GlassHive will not copy
the bytes and will fall back to a metadata manifest until the deployment provides an owner-scoped
upload projection. When only model-visible attachment text is available, GlassHive may search only
the authenticated owner's mounted upload directory for a normalized original filename match and use
the newest matching file; it must never search other users' directories or the whole host filesystem.

## Source-Specific Best Practice

### A. Host CLI login projection

Best when the host machine already has working local Codex and Claude Code logins.

For Docker sandboxes, minimal host CLI auth is copied into the worker home according to
`bootstrap_profile`.

For host-native workers, the CLI runs on the host and uses the host's existing CLI/browser/OS
session directly. v1 caps active host workers to one per CLI family unless isolated CLI homes are
explicitly enabled later.

Host-native CLI subprocesses receive a minimal runtime environment. Parent process secrets, provider
API keys, callback secrets, and LibreChat internals are not inherited by default.

Current validated local footprints on this machine:

- Codex auth state exists in `~/.codex/auth.json`
- Codex configuration exists in `~/.codex/config.toml`
- Claude Code user state exists in `~/.claude.json`
- Claude settings exist in `~/.claude/settings.json`
- Claude project/local settings are officially supported through `.claude/settings.json`, `.claude/settings.local.json`, and `.mcp.json`

Best practice:

- project only the minimal provider-specific files needed
- keep project-scoped MCP and instructions in the workspace, not only in the user home
- prefer `clean-room + bootstrap_bundle` when repeatability matters more than inheriting the host personality

### B. Direct API-key projection

Best when a client or operator wants an explicit provider key available inside the worker sandbox.

Best practice:

- inject via `bootstrap_bundle.env`
- keep the env set explicit and minimal
- never write raw secrets into committed docs or repo config

### C. Connected-account projection from LibreChat-compatible hosts

This is the important one.

Best practice:

- Glass Hive should **not** directly consume LibreChat internal token storage as its product contract
- instead, a host application such as LibreChat, Viventium, or another compatible client should act as an
  **optional auth broker**
- the broker should resolve the user's connected account and materialize a provider-specific projection for the sandbox
- that projection can be:
  - ephemeral env
  - a short-lived auth file
  - a provider-specific CLI login projection
  - a runtime-local callback or refresh helper

This keeps Glass Hive independent and publishable outside the Viventium stack.

2026-05 preferred connected-account projection for LibreChat-compatible hosts:

- the host projects a single `glasshive-user-capabilities` broker MCP through `bootstrap_bundle`
- provider OAuth/API tokens stay in the host and are never copied into the GlassHive workspace
- the worker receives only a short-lived broker grant scoped to user/conversation/worker/run and
  source-of-truth-approved server names
- the broker dynamically re-exports native typed tools where possible, so the worker can decide
  which connected-account tool to call without the host chat model choosing for it
- grant-bearing MCP config, Claude local settings, and Codex config files are written with
  owner-only permissions in both host-native and sandbox materialization paths
- `context` may mention the broker compactly, but large schemas, token material, and provider
  credential state belong in bootstrap/tool results, not prompt text

## Why This Does Not Break Stable LibreChat

Glass Hive remains a separate service.

- it does not require changes to the current working LibreChat runtime to exist
- it does not need to mutate existing connected-account storage
- integration can be added through a broker or MCP client layer later

## Current Phase-1 Runtime Support

The current Glass Hive runtime now supports:

- worker-level `bootstrap_profile`
- worker-level `bootstrap_bundle`
- sandbox seeding of Claude project MCP config via `.mcp.json`
- sandbox seeding of Claude local settings via `.claude/settings.local.json`
- host-native seeding of project MCP config and local settings from the same bootstrap fields
- workspace instructions via `CLAUDE.md` and `AGENTS.md`
- generic file seeding
- runtime env projection into shells and task runs
- a non-secret bootstrap manifest written into the sandbox for auditability

## Best-Practice Summary

A. Do not touch the stable host app unless the client explicitly opts into Glass Hive auth brokering.

B. Treat auth projection as a provider-specific plugin behind a universal Glass Hive bootstrap contract.

C. Keep the Glass Hive runtime independently usable by any client that can supply bootstrap data or call its API/MCP surface.
