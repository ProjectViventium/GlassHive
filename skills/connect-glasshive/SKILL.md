---
name: connect-glasshive
description: Connect Codex or Claude Code to a user's hosted GlassHive account through the deployment's official OAuth-protected MCP URL, verify the connection, and use owner-scoped workspaces, schedules, accounts, connections, and Library capabilities. Use when a user asks an AI client to connect to GlassHive, manage their GlassHive workspaces through MCP, or prepare a skill/tool/connector for human approval.
---

# Connect GlassHive

Use GlassHive's existing remote MCP interface. Do not create another protocol, copy credentials, or
guess a deployment URL.

## Connect

1. Ask the user to sign in to their GlassHive site and open **Connect AI**.
2. If **Connect AI** says `action_required`, stop and report that the deployment has no complete
   pre-registered client. Ask its administrator to register and allowlist the client, delegated
   scopes, token audience, and exact callback URI shown by the deployment. Do not invent a command.
3. Use only the exact deployment-generated add and login commands shown for the selected client.
   They carry the registered client id, canonical public MCP resource, and fixed callback settings
   required by that deployment. Do not reconstruct a command from only the MCP URL or substitute a
   generic example from this skill.
4. Run the displayed commands in their displayed order. For Claude Code, follow the accompanying
   `/mcp` sign-in note when shown. Hosted multi-user MCP URLs must be HTTPS.
5. Let the client open its OAuth login/consent flow. Never ask the user to paste an access token into
   chat, a workspace, this skill, or a client command.
6. Verify the configured `glasshive` server and list its tools. If OAuth, URL, or discovery fails,
   report the exact failing state and return to **Connect AI**; do not fall back to a static bearer
   token or a guessed localhost address.

## Use

Use the MCP tools as the authenticated user:

- Find persisted workspaces with `workspace_list`; use human names and stable ids from its result.
- Create work through the existing workspace/worker tools; rename or safely duplicate when asked.
- Inspect `worker_accounts_list`, `connections_list`, and `library_list` before choosing a capability.
- Use `workspace_capability_prepare` to propose one curated Library item. Present its
  `confirmation_url` to the user. The AI must never claim the capability is enabled until the
  signed-in human reviews and confirms it in the browser. Connected services use the deployment's
  brokered workspace bundle; personal worker accounts are selected by workspace execution policy.
- When the requested item is not in `library_list`, let the selected native worker inspect the
  public source and prepare a complete non-secret Library manifest using the Curated Library
  contract in `runtime_phase1/README.md`. Submit it with `library_manifest_propose`. This creates an
  administrator review item only: the AI cannot publish, confirm, install, or widen its own scopes.
  Never translate install instructions into shell activation, embed credentials, replace worker
  authority files, or claim the proposal is already available.
- For an already enabled item with a newer curated version, use
  `workspace_capability_upgrade_prepare` with the current grant id. Retain or narrow the existing
  scopes, then present its browser confirmation URL. A scope-widening upgrade is intentionally
  rejected; the user can instead review a separate explicit capability grant.
- Remove only the newest active capability with `workspace_capability_remove`. If GlassHive reports
  configuration drift or a newer grant, stop and show that exact recovery message rather than
  deleting workspace files.
- Create and inspect recurring work through the structured recurring-schedule tools. Do not convert
  unsupported recurrence syntax into an invented schedule.

Keep the user's original goal and constraints intact when delegating. GlassHive workers choose the
execution plan; this client should not manufacture provider choices, workflows, or success evidence.

## Source and truth

GlassHive is source available under FSL-1.1-ALv2. Use the live repository and its MCP publication
guide as the implementation source of truth:

- <https://github.com/ProjectViventium/GlassHive>
- <https://github.com/ProjectViventium/GlassHive/blob/main/docs/04_MCP_Publication_and_Client_Compatibility.md>
- <https://github.com/ProjectViventium/GlassHive/blob/main/runtime_phase1/README.md#curated-library-registry>
