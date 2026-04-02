<!-- VIVENTIUM START: Glass Hive vision and requirements -->
# Glass Hive: Vision, Requirements, and Terminology

## Vision

Glass Hive is a standalone runtime for powerful AI workers that can be resumed, watched, interrupted, and steered in real time.

It is meant to feel like this:

- define a `Project`
- choose or create a `Worker`
- run it inside a persistent `Sandbox`
- watch what it is doing live
- interrupt or take over when needed
- resume later without losing the work surface

## Product Requirements

Glass Hive should provide:

1. a project-first operator flow
2. persistent sandbox state
3. fast warm resume
4. controllable workers
5. explicit human takeover
6. compatibility with multiple worker runtimes
7. client independence
8. strong local-first posture
9. safe publication through MCP and direct HTTP APIs
10. a clean path for auth, MCP, memory, context, and callback injection

## Non-Goals

Glass Hive is not:

- a LibreChat-only feature
- a Skyvern replacement
- a provider-specific wrapper tied only to Anthropic or OpenAI
- a promise of hostile multi-tenant security from phase-1 Docker alone

## Terminology

### Sandbox
A persistent workstation-like execution environment that holds the worker's home directory, workspace, processes, and live control surfaces.

### Worker
The active AI runtime inside a sandbox.
Examples:

- `codex-cli`
- `claude-code`
- `openclaw-general`

### Project
The durable task definition around one or more workers, including goal, success criteria, continuity, and audit trail.

### Bootstrap Bundle
A portable preset describing what should be projected into a worker sandbox when it starts or resumes.

## Client Compatibility Goal

Glass Hive should work with:

- Viventium / LibreChat
- Claude / Claude Code
- Codex
- ChatGPT-compatible MCP clients
- direct operator API usage

The runtime must remain usable even when none of those clients are present.
