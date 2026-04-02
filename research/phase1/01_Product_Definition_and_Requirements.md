# Workers & Projects Runtime

Date: 2026-03-18

Project root: `<private-research-root>/workers_projects_runtime`

## Project Definition

Build a standalone `Workers & Projects` system that can later plug into Viventium, LibreChat, Telegram, or any other client surface, while remaining useful on its own.

The system must let a user treat AI workers like reliable teammates:

- create projects with goals, context, assets, and workspaces
- spawn specialized workers into those projects
- pause, resume, interrupt, redirect, and terminate workers cleanly
- preserve state, artifacts, and progress across restarts
- support multiple worker styles without forcing a single vendor or agent stack
- expose honest status, logs, artifacts, and takeover paths where applicable
- remain modular enough that Viventium is just one client and one manager, not the only way to use it

## User Intent Snapshot

This system is meant to satisfy the following owner expectations:

- separate service, not tightly embedded in Viventium
- resumable, controllable, interruptible, efficient, persistent, secure, and powerful
- works for Codex, Claude Code, and OpenClaw users inside one coherent system
- ready-state behavior so workers/projects resume quickly instead of cold booting every time
- minimal reinvention of execution, sandboxing, sessioning, workflow primitives, and operator control surfaces
- powerful but honest UX: visible state, live progress, intervention when needed, and clear failure modes

## Product Thesis

The product is not "a VM farm".

The product is a control plane for long-lived AI work.

That control plane manages:

- project state
- worker identity
- execution backends
- memory and artifacts
- human intervention
- policy and isolation
- observability and resumption

This distinction matters because not every worker needs a full desktop VM. Some workers need:

- a general OpenClaw session
- a Codex-oriented coding profile
- a Claude Code-oriented coding profile
- a display-backed sandbox profile for richer view and takeover
- a deterministic workflow runner

The system should unify those under one product model instead of forcing everything through one oversized runtime.

## Core Product Requirements

| ID | Requirement | Why it exists | Phase 1 Target |
|---|---|---|---|
| R1 | A user can create multiple projects | Projects are the durable unit of work | Required |
| R2 | A project can have multiple workers | Delegation is the core product value | Required |
| R3 | Workers can be started, interrupted, paused, resumed, and terminated | Users need control, not black boxes | Required |
| R4 | Worker state persists across service restarts | Reliability requires durable resumption | Required |
| R5 | Workspace and artifacts persist per project and worker | Outputs must survive beyond a single run | Required |
| R6 | Multiple execution backends are supported | No vendor lock-in and no forced one-size-fits-all runtime | Required |
| R7 | Users can see honest live status and event history | UX must reduce anxiety and hidden failure | Required |
| R8 | Human intervention is possible when needed | Real work needs interrupts, approvals, and takeovers | Required |
| R9 | Isolation is enforced per user trust boundary and worker policy | Security cannot be an afterthought | Required |
| R10 | The system is standalone and integration-friendly | Viventium is one client, not the only client | Required |
| R11 | Workers can be resumed from warm or ready state when supported by backend | Efficiency matters for long-running work | Required |
| R12 | The system supports Codex, Claude Code, and OpenClaw-style workflows | This is a first-class product requirement | Required |
| R13 | Workers with display-backed or high-risk tasks have a watchable control path | Human oversight matters most when the worker can drift or needs intervention | Required |
| R14 | The system exposes one consistent API/MCP surface for clients | Clients should not orchestrate raw runtime details | Required |
| R15 | The system records timing, failures, and resource usage | We need to know if it actually performs well | Required |

## User Experience Requirements

| ID | Requirement | Acceptance intent |
|---|---|---|
| UX1 | Creating a project feels instant and clear | User sees project created with status and next actions |
| UX2 | Spawning a worker does not feel like "something disappeared" | User sees state transitions and live logs within seconds |
| UX3 | Interrupt is fast and trustworthy | User can stop bad behavior without guessing |
| UX4 | Resume is explicit and stateful | User sees what is being resumed and from where |
| UX5 | Every worker has a visible role and backend | No anonymous generic agent threads |
| UX6 | Browser or desktop class workers offer a watch/takeover path when supported | Human can intervene instead of starting over |
| UX7 | Failures are actionable | Error output tells the user whether to retry, reconfigure, or take over |
| UX8 | Project summaries stay current | User can return later and know what happened quickly |
| UX9 | The control plane feels vendor-neutral | User chooses worker profile, not hidden provider magic |
| UX10 | A worker can be redirected midstream | User can adjust course without losing context |

## Security and Trust Requirements

| ID | Requirement | Acceptance intent |
|---|---|---|
| S1 | One user trust boundary must not leak data into another | No cross-user workspace, session, or artifact leakage |
| S2 | Worker permissions must be profile-driven | Different workers need different tool and network policies |
| S3 | Shared gateway/session infrastructure must not pretend to be strong isolation | Architecture must reflect actual trust boundaries |
| S4 | Secrets stay out of project artifacts by default | Logs and summaries should not casually exfiltrate credentials |
| S5 | Human takeover links must be scoped and revocable | Visibility features cannot become open doors |

## Operational Requirements

| ID | Requirement | Acceptance intent |
|---|---|---|
| O1 | The service runs standalone locally first | Phase 1 must not require Viventium integration |
| O2 | The service provides health, status, and metrics surfaces | Operators need visibility |
| O3 | Recovery after crash or reboot is deterministic | Projects and workers reconcile on startup |
| O4 | Backend adapters are pluggable | New worker engines should not force control-plane rewrites |
| O5 | The system can benchmark warm vs cold behavior | Efficiency claims must be measured |

## Non-Goals For Phase 1

- full Viventium integration
- polished end-user product UI inside LibreChat
- enterprise-grade hosted multi-tenancy with billing and org management
- universal full-desktop takeover for every worker type on day one
- replacing existing specialist systems where reuse is better
- inventing a new LLM agent framework from scratch

## Phase-1 Product Boundaries

Phase 1 should prove the standalone system can:

- define projects and workers cleanly
- run real work through at least one strong default backend
- persist and resume real sessions
- expose live events and control actions
- support at least one browser-visible backend
- benchmark and report whether the design is actually efficient

## Product Principles

1. Reuse existing agent/runtime systems before building new ones.
2. Treat Viventium as a client and manager, not the hidden runtime for everything.
3. Separate control plane from worker execution.
4. Model trust boundaries honestly.
5. Make status, logs, and human control first-class.
6. Prefer profiles and adapters over hardcoded agent assumptions.
7. Build phase 1 to validate product truth, not to chase every edge case up front.
