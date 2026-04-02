# Phase 1 Standalone System Plan

Date: 2026-03-18

## Status Update: 2026-03-19

Phase 1 is now materially implemented as a standalone local service under:

- `<private-research-root>/workers_projects_runtime/runtime_phase1`

What is delivered:

- FastAPI control plane
- SQLite durable state for phase 1
- real `OpenClaw` worker profile
- real `Codex CLI` worker profile inside a persistent Docker sandbox
- `Claude Code` worker profile inside the same sandbox model
- thin MCP wrapper over the standalone runtime API
- project-first web UI
- worker detail console
- real terminal takeover over websocket
- real workstation desktop view through noVNC
- workstation-launch actions for shell, files, browser, Codex, Claude Code, and OpenClaw
- latest visual artifact preview on the project page
- pause, resume, interrupt, terminate
- resumable worker workspace and home directories

What remains outside phase-1 completion:

- end-to-end deployed Viventium / LibreChat integration verification
- stronger tenant/auth policy layers
- tighter sandbox hardening beyond the current local owner-controlled workstation model
- reliable live `Claude Code` task validation without external auth or credit blockers
- final live signoff for `openclaw-general` task execution inside the workstation sandbox using the embedded local agent path

## Goal

Deliver a standalone phase-1 `Workers & Projects Runtime` that proves the product model before any Viventium integration work.

Phase 1 should be self-contained and useful on its own.

An initial local control-plane skeleton has been created under:

- `<private-research-root>/workers_projects_runtime/runtime_phase1`

## Phase-1 Deliverable

A local standalone service with:

- REST API
- durable project and worker state
- at least one real worker backend with resumable execution
- at least one real operator control surface with view and intervention semantics
- interrupt, pause, resume, terminate controls
- event logs, artifacts, summaries, and metrics
- clear QA evidence of persistence, control, and recovery

## Architecture

### Core services

1. `control-plane-api`
   - FastAPI service
   - owns projects, workers, runs, lifecycle, summaries, metrics

2. `runtime-adapter-layer`
   - adapter and profile interface for OpenClaw, Codex-oriented workers, Claude-oriented workers, and later OpenShell-backed display profiles

3. `state-store`
   - SQLite accepted for implemented phase 1
   - local filesystem artifact store for phase 1

4. `event-stream`
   - SSE first for simplicity
   - every worker emits lifecycle and progress events

5. `operator-console`
   - minimal local web UI or simple API-friendly dashboard
   - list projects, list workers, view runs, interrupt, resume, inspect logs, open takeover links

## Why Not Another Workflow Engine Yet

Do not add Temporal or a custom orchestration engine in phase 1.

Reason:

- OpenClaw already gives us session and workflow primitives
- the main unknown is product shape and runtime UX, not generic workflow infrastructure
- adding a second orchestration layer too early increases complexity without answering the core user question

## Worker Profiles

Phase 1 supports these profiles:

1. `openclaw-general`
   - default worker
   - general tool and conversation execution

2. `openclaw-codex`
   - Codex-oriented worker profile via OpenClaw image/runtime choices

3. `openclaw-claude`
   - Claude-oriented worker profile via OpenClaw image/runtime choices

4. `openclaw-desktop`
   - reserved profile for later graphical takeover work

5. `codex-cli`
   - persistent Docker sandbox
   - persistent worker home and workspace
   - resumable Codex session id capture

6. `claude-code`
   - persistent Docker sandbox
   - persistent worker home and workspace
   - resumable Claude session id capture when auth is valid

## Worker Lifecycle

Every worker must implement the same logical lifecycle:

- `created`
- `starting`
- `ready`
- `running`
- `interrupting`
- `paused`
- `resuming`
- `completed`
- `failed`
- `terminated`

The control plane owns this model even if backends have different native states.

## Human Control Model

Every worker must support:

- status query
- interrupt
- pause if backend supports resumable state
- resume
- terminate
- add operator message or redirect
- view recent events
- inspect artifacts

Display-backed workers must also support:

- live watch link
- takeover link or operator handoff if backend supports it

For non-display workers, the equivalent of takeover is:

- send operator instruction
- view logs
- inspect workspace diff and artifacts
- resume or redirect from the same session

## Phase-1 Build Sequence

### Milestone 1: lock contracts and state model

Deliver:

- data model
- lifecycle state machine
- API contract draft
- worker profile matrix

### Milestone 2: build standalone control plane

Deliver:

- FastAPI service
- SQLite models and migrations
- artifact directory layout
- event log model
- project and worker CRUD

### Milestone 3: implement OpenClaw adapter

Deliver:

- worker creation
- session/routing mapping
- assign task
- interrupt and resume flows
- status and event polling
- project summary materialization from worker outputs

### Milestone 4: implement operator control surface

Deliver:

- worker detail control page
- live event stream view
- operator message path
- view/control URL return
- real terminal takeover for sandboxed workers

### Milestone 5: operator console

Deliver:

- simple local dashboard
- project list
- worker list
- run detail page
- event stream view
- interrupt/resume/terminate controls

### Milestone 6: QA and benchmarks

Deliver:

- persistence and recovery tests
- warm/cold behavior benchmarks
- manual operator journey QA
- reliability report

## Proposed Directory Shape For The Standalone Service

```text
workers-projects-runtime/
  apps/
    api/
    mcp/
    console/
  packages/
    core/
    adapters/
    schemas/
  migrations/
  artifacts/
  tests/
    unit/
    integration/
    e2e/
  docker-compose.yml
  README.md
```

## Phase-1 Success Criteria

1. A user can create at least two projects and at least two workers per project.
2. A worker can run, be interrupted, and be resumed without losing its project context.
3. A worker exposes a live control surface path.
4. After service restart, project and worker state reconcile correctly.
5. Metrics capture warm vs cold latency and lifecycle timings.
6. A standalone MCP client can drive the service without any Viventium-specific code.

## Phase-1 Deferrals

- billing and org administration
- cross-region deployment
- full enterprise RBAC
- universal desktop takeover
- large-scale hosted scheduler farm
- deep Viventium UX wiring

## Why This Phase-1 Shape Is Safe

- it validates the product model before integration cost explodes
- it keeps the runtime reusable outside Viventium
- it leverages existing worker technologies instead of replacing them
- it gives us a hard QA gate before touching the main product
