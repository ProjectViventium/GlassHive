<!-- VIVENTIUM START: Glass Hive root README -->
# Glass Hive

**Persistent workers. Resumable sandboxes. Live takeover.**

Glass Hive is a standalone workstation-sandbox runtime in the Viventium ecosystem.
It is designed to run portable `Workers` inside durable `Sandboxes`, organized around explicit `Projects`, while remaining usable from Viventium, LibreChat, Claude, Codex, ChatGPT-compatible MCP clients, or direct API calls.

## Why It Exists

Glass Hive exists so long-running agentic work does not have to live inside one chat client or one product runtime.

It gives us:

- persistent `Projects` with goals and success criteria
- resumable `Workers` that can pause, resume, interrupt, and terminate cleanly
- workstation-style `Sandboxes` with browser, terminal, filesystem, and tool surfaces
- live operator visibility and takeover
- a clean MCP surface for clients like Viventium / LibreChat
- a portable bootstrap model for auth, MCPs, instructions, context, memory, and callbacks

## Core Terms

- `Sandbox`: the persistent workstation environment backing a worker
- `Worker`: the AI runtime operating inside a sandbox
- `Project`: the operator-defined mission, success criteria, and continuity wrapper around one or more workers
- `Bootstrap Bundle`: a portable preset describing what should be projected into a sandbox at start time

## Current Phase-1 Scope

Implemented today:

- standalone FastAPI control plane
- SQLite persistence for projects, workers, runs, and events
- workstation-backed worker profiles for `codex-cli`, `claude-code`, and `openclaw-general`
- browser desktop view via noVNC
- terminal takeover via websocket bridge
- worker lifecycle controls: `pause`, `resume`, `interrupt`, `terminate`
- MCP wrapper with `streamable-http`, `stdio`, and compatibility `sse`
- portable worker bootstrap contract:
  - `bootstrap_profile`
  - `bootstrap_bundle`
- project-first operator UI

## Auth and Identity Direction

Glass Hive supports two safe patterns:

1. **Host projection**
   - copy or project local CLI login state into the sandbox in a tool-specific way
   - works today for Codex and Claude Code host state

2. **Brokered projection**
   - use an external system such as Viventium / LibreChat to materialize provider-specific auth into a sandbox without teaching Glass Hive about that product's database internals
   - this is the right long-term path for connected accounts

Important boundary:

- Glass Hive should not directly depend on LibreChat internals to function
- Viventium / LibreChat are clients and optional auth brokers, not the source of truth for the Glass Hive runtime itself

## Documentation

Release-oriented docs live in [`docs/`](./docs).

Start here:

1. [`docs/01_Vision_Requirements_and_Terminology.md`](./docs/01_Vision_Requirements_and_Terminology.md)
2. [`docs/02_Architecture_and_Components.md`](./docs/02_Architecture_and_Components.md)
3. [`docs/03_Bootstrap_Auth_and_Identity_Projection.md`](./docs/03_Bootstrap_Auth_and_Identity_Projection.md)
4. [`docs/04_MCP_Publication_and_Client_Compatibility.md`](./docs/04_MCP_Publication_and_Client_Compatibility.md)
5. [`docs/05_QA_Quick_Power_Playbook.md`](./docs/05_QA_Quick_Power_Playbook.md)
6. [`docs/06_References.md`](./docs/06_References.md)
7. [`docs/07_Minimal_Unified_Operator_UI.md`](./docs/07_Minimal_Unified_Operator_UI.md)
8. [`docs/08_Repository_Structure_and_Publication_Boundaries.md`](./docs/08_Repository_Structure_and_Publication_Boundaries.md)

Historical research and phase-1 planning docs from the earlier standalone package live under [`research/`](./research) for continuity.

## Runtime

The current runtime implementation lives under [`runtime_phase1/`](./runtime_phase1).

It intentionally keeps the existing Python package name `workers_projects_runtime` for compatibility with the already-wired Viventium MCP integration, while the project/product name is now **Glass Hive**.

## License

Glass Hive uses the same source-available posture as the main Viventium repo:

- Functional Source License 1.1
- Apache-2.0 future license conversion

See [`LICENSE`](./LICENSE).
