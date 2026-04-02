<!-- VIVENTIUM START: Glass Hive references -->
# Glass Hive: References

## Local Viventium Documents

- `<workspace-root>/docs/requirements_and_learnings/01_Key_Principles.md`
- `<workspace-root>/docs/requirements_and_learnings/35_OAuth_Subscription_Auth.md`
- `<workspace-root>/docs/requirements_and_learnings/53_Workers_Projects_Runtime_MCP.md`
- `<workspace-root>/docs/requirements_and_learnings/54_Glass_Hive_Standalone_Runtime.md`

## Local Runtime Evidence

Validated local footprints on this machine:

- `~/.codex/auth.json`
- `~/.codex/config.toml`
- `~/.claude.json`
- `~/.claude/settings.json`

These confirm that Codex and Claude Code already have distinct local state surfaces that can be projected into worker sandboxes without forcing Glass Hive to depend on LibreChat storage internals.

## Official Claude Code Docs

- Claude Code settings and scope model:
  - `https://code.claude.com/docs/en/settings`
- Claude Code MCP docs:
  - `https://code.claude.com/docs/en/mcp`

Key relevant points validated from the docs:

- user, project, and local scopes are distinct
- Claude Code uses `~/.claude.json`, `.mcp.json`, and `.claude/settings.local.json`
- remote HTTP MCP is recommended
- SSE is deprecated where HTTP is available
- `list_changed` is supported for dynamic capability refresh

## Official OpenAI Docs

- Codex CLI docs:
  - `https://developers.openai.com/codex/cli`
- OpenAI MCP docs:
  - `https://developers.openai.com/api/docs/mcp`

Key relevant points validated from the docs:

- Codex CLI prompts for sign-in on first use with a ChatGPT account or API key
- MCP is a first-class integration path for OpenAI ecosystem tooling

## Official MCP Spec

- Model Context Protocol transport guidance:
  - `https://modelcontextprotocol.io/specification/2025-11-25/basic/transports`

Key relevant point:

- modern MCP transport direction favors HTTP-style remote transports over legacy SSE
