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

### Viventium / LibreChat
Use Glass Hive as an external MCP server.

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

## Compatibility Alias

The current Viventium stack already refers to the MCP integration as `workers_projects_runtime`.

Glass Hive should preserve compatibility while documenting the product name clearly.
