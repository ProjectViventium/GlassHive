---
name: connect-glasshive
description: Connect Codex or Claude Code to a user's hosted GlassHive account through the deployment's official OAuth-protected MCP URL, verify the connection, and use owner-scoped workspaces, schedules, accounts, connections, and Library capabilities. Use when a user asks an AI client to connect to GlassHive, manage their GlassHive workspaces through MCP, or prepare a skill/tool/connector for human approval.
---

# Connect GlassHive

GlassHive is one remote MCP integration. This skill is only the short usage guide; do not create a
second protocol, plugin, OAuth helper, callback listener, or token flow.

If GlassHive is already connected and its tools are callable, skip setup and verification. Go
straight to the user's outcome and call only the one tool needed for the user's request. Never
enumerate or summarize the tool catalog unless the user explicitly asks for it.
Seeing a GlassHive MCP tool in the current session is sufficient proof that it is connected. Do not
inspect config files, run shell checks, or repeat setup before using it.

## Connect once

1. Ask the user to open **Connections → Use GlassHive from another AI app → Automatic** in their
   signed-in GlassHive site and paste the copied instruction here.
2. Follow only the section for the client you are currently running. Never configure the other
   client. If the named server already exists, reuse it instead of creating a duplicate.
3. For Codex, add or update the supplied native MCP config exactly, including its persistent
   `scopes` values; this keeps ordinary Reconnect on the server's OAuth resource and keeps the login
   renewable. Restart the Codex/ChatGPT desktop app once after changing that config, then use the
   client's native sign-in exactly as instructed. Never construct an authorization URL,
   inspect or copy tokens, or open the displayed callback address yourself.
4. Verify with one `workspace_list` call only during first setup or reconnect verification.
5. If native sign-in fails, report the visible client or identity-provider error and stop. Return to
   the same GlassHive panel; do not improvise another auth flow.

## Use directly

Call only the MCP tool needed for the requested outcome:

- list saved workspaces: `workspace_list`
- start new work: `workspace_launch`
- rename or copy: `workspace_rename` or `workspace_duplicate`
- inspect or continue: `workspace_status` or `workspace_continue`
- inspect accounts, connected services, or reusable capabilities only when asked:
  `worker_accounts_list`, `connections_list`, or `library_list`

Use the human names and stable ids returned by GlassHive. Keep the user's goal intact and let the
workspace decide its own execution plan. If a tool returns a browser confirmation URL, show it and
wait for the signed-in user; never claim approval happened before GlassHive confirms it.

## Source and truth

GlassHive is source available under FSL-1.1-ALv2. Use the live repository and its MCP publication
guide as the implementation source of truth:

- <https://github.com/ProjectViventium/GlassHive>
- <https://github.com/ProjectViventium/GlassHive/blob/main/docs/04_MCP_Publication_and_Client_Compatibility.md>
- <https://github.com/ProjectViventium/GlassHive/blob/main/runtime_phase1/README.md#curated-library-registry>
