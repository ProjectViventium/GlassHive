<!-- VIVENTIUM START: Glass Hive architecture -->
# Glass Hive: Architecture and Components

## System Shape

Glass Hive is split into four layers:

1. `Control Plane`
2. `Worker Runtime`
3. `Execution Substrate`
4. `Client Adapters`

## 1. Control Plane

The control plane owns:

- projects
- workers
- runs
- events
- lifecycle orchestration
- live worker state

Current implementation:

- FastAPI API
- SQLite store
- project-first web UI

## 2. Worker Runtime

This layer decides how a worker actually executes tasks.

Current worker profiles:

- `codex-cli`
- `claude-code`
- `openclaw-general`

Design principle:

- all worker profiles should share the same project/run/lifecycle API whether they execute in a
  Docker workstation sandbox or in host-native mode

## 3. Execution Substrate

Current phase-1 substrate:

- Docker-backed workstation containers
- persistent home and workspace mounts
- noVNC desktop view
- terminal bridge

Approved host-native substrate:

- local process execution on the user's main computer
- no Docker and no sandbox
- local Codex, Claude, and OpenClaw CLIs
- user-scoped workspace root
- structured action audit and work-log visibility

Current honest boundary:

- good owner-controlled local isolation
- not yet a hostile multi-tenant boundary

## 4. Client Adapters

Glass Hive should be reachable through:

- direct HTTP API
- MCP
- future broker/integration adapters

Current implemented adapter:

- thin MCP wrapper using `streamable-http`, `stdio`, and compatibility `sse`

## Why This Shape

This shape preserves the important separation:

- Glass Hive owns worker runtime truth
- Viventium or LibreChat are clients
- provider auth and connected-account systems can be bridged in without changing the Glass Hive core model
