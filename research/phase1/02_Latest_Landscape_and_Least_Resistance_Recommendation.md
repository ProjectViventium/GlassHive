# Latest Landscape and Least-Resistance Recommendation

Date: 2026-03-18

## Executive Summary

The least-resistance path is not to build a custom VM platform first.

The least-resistance path is:

- standalone control plane for projects and workers
- OpenClaw as the default worker/session engine
- Codex and Claude Code as first-class worker profiles inside that engine
- OpenProse and Lobster for higher-level workflow composition
- display-backed sandbox profiles for view/control/interrupt UX
- stronger sandboxing as a backend concern, not the product foundation

This gives us a product that is powerful, honest, and extensible without forcing every task into a full desktop sandbox.

## What The Current Landscape Suggests

### 1. OpenClaw is now strong enough to be the default worker runtime

OpenClaw now provides the primitives we would otherwise be tempted to invent ourselves:

- session management
- worker spawning and message routing
- sandbox backends
- workflow plugins
- deterministic multi-step tools
- multiple agent images including Codex and Claude-oriented flows

That makes it a strong fit for the base worker/session layer.

### 2. OpenProse reduces the need for a custom workflow DSL

OpenProse gives a markdown-first way to describe reusable multi-agent workflows.

That matters because `Projects` will eventually need:

- project templates
- operating playbooks
- reusable multi-step task graphs
- approval points
- consistent worker handoff patterns

We should use that before inventing our own internal workflow language.

### 3. Lobster reduces the need for a custom deterministic task engine

Lobster is relevant where we want:

- multi-step deterministic flows
- explicit approvals and pauses
- stateful progression through a known task graph
- auditable execution behavior

That makes it a good fit for "controlled operations" within a project.

### 4. OpenShell is the interesting hardening layer, not the entire product answer

OpenShell matters because it adds policy and sandboxing value around agent execution.

But it does not replace the need for:

- project objects
- worker lifecycle and UX
- artifact and memory management
- specialized runtime selection
- client-facing control APIs

So it belongs as an execution-hardening backend, not the top-level product architecture.

### 5. OpenClaw already spans more of the requirement than it did before

The newest OpenClaw surfaces matter here because they cover more than just chat or tools:

- guided setup and control UI via Dashboard
- session and subagent primitives
- configurable sandbox backends
- macOS companion and node model
- macOS VM guidance for isolated display-backed operation
- UI-oriented surfaces like Canvas and Peekaboo Bridge on macOS

That means the core system can be built around OpenClaw itself instead of around a patchwork of unrelated worker backends.

## Decision Matrix

| Option | What it gives us | What it costs us | Recommendation |
|---|---|---|---|
| Build custom VM-first worker platform | Maximum control in theory | Massive engineering and QA burden; delays product truth | Reject for phase 1 |
| Make full desktop VM the default for every worker | Watchability and takeover | Overkill for many tasks; slower and more expensive | Reject as default |
| Use OpenClaw as the primary worker/session layer | Native spawning, sessions, profiles, workflows | Need honest trust-boundary design | Adopt |
| Use OpenShell as the default mandatory runtime immediately | Stronger local hardening path | Adds backend complexity before product model is proven | Phase 2 |
| Build the system around OpenClaw + Codex/Claude profiles + display-backed sandbox profiles | One coherent worker/runtime model | Needs strong control-plane design | Adopt |
| Tight-couple the system into Viventium first | Faster perceived integration | Harder to reason about independently; more regression risk | Reject for phase 1 |

## Recommended Product Shape

### Control plane

Build a new standalone service called, for now, the `Workers & Projects Runtime`.

Its job is to manage:

- projects
- workers
- runs
- lifecycle
- artifacts
- summaries
- operator controls
- metrics and audit events

### Default worker engine

Use OpenClaw as the default worker engine for phase 1.

Why:

- it already knows how to manage sessions and spawn work
- it already supports multiple agent images and tools
- it already has a path for sandboxed execution
- it already exposes concepts that map naturally to workers

### Codex and Claude Code path

Use Codex-oriented and Claude-oriented worker profiles as first-class options inside the same system.

Why:

- this matches the actual user requirement
- it avoids forcing users into a generic agent flavor when they want a specific coding style
- it keeps the product centered on one worker runtime and one control plane

### View and control path

Use display-backed sandbox profiles and OpenClaw control surfaces for visibility, communication, and interruption.

Why:

- the user requirement is not just task execution
- the user wants to watch, communicate, interrupt, and resume
- that is a control-surface requirement, not a separate browser-product requirement

## Recommended Trust Model

One of the most important architecture decisions is trust-boundary honesty.

Recommended rule:

- one user trust boundary gets its own OpenClaw gateway or equivalent runtime boundary
- many workers can exist inside that user's boundary
- workers are not themselves assumed to be hard security boundaries unless a stronger sandbox profile is selected

This prevents us from pretending that session IDs alone are security isolation.

## Recommendation For Phase 1

Build the standalone control plane with these runtime layers in order:

1. `openclaw` adapter
2. `openclaw-codex` worker profile
3. `openclaw-claude` worker profile
4. `openclaw-desktop` or display-backed sandbox profile
5. `openshell` hardening profile
6. optional macOS VM profile when local mac-only capabilities are needed

## Why This Is The Path Of Least Resistance

- We reuse real OSS primitives instead of creating our own agent/session/workflow runtime.
- We preserve optionality across agent styles.
- We keep product semantics in one place.
- We can validate UX and reliability before deep Viventium integration.
- We avoid forcing every future capability into one overpowered VM abstraction.

## What We Should Explicitly Avoid

- inventing a new session and worker protocol when OpenClaw already provides one
- building a generic workflow DSL before testing OpenProse and Lobster properly
- making Viventium the only control surface before the runtime is stable on its own
- pretending view/control requires a separate browser platform when the core runtime can own it
- treating a shared gateway as hostile multi-tenant isolation

## Sources Used

- OpenClaw security: https://docs.openclaw.ai/gateway/security
- OpenClaw sandboxing: https://docs.openclaw.ai/gateway/sandboxing
- OpenClaw session tool docs: https://docs.openclaw.ai/session-tool
- OpenClaw subagents docs: https://docs.openclaw.ai/tools/subagents
- OpenClaw OpenProse docs: https://docs.openclaw.ai/prose
- OpenClaw Lobster docs: https://docs.openclaw.ai/tools/lobster
- OpenClaw Docker install docs: https://docs.openclaw.ai/install/docker
- OpenClaw onboarding and dashboard: https://docs.openclaw.ai/start/wizard
- OpenClaw macOS app: https://docs.openclaw.ai/platforms/macos
- OpenClaw macOS VMs: https://docs.openclaw.ai/platforms/macos-vm
- OpenClaw Canvas: https://docs.openclaw.ai/mac/canvas
- OpenClaw Peekaboo Bridge: https://docs.openclaw.ai/platforms/mac/peekaboo
- OpenClaw repository: https://github.com/openclaw/openclaw
- NVIDIA OpenShell repository: https://github.com/NVIDIA/OpenShell
- NVIDIA OpenShell build page: https://build.nvidia.com/nvidia/openshell

## Repo Context Used

- `<workspace-root>/docs/requirements_and_learnings/01_Key_Principles.md`
- `<workspace-root>/docs/requirements_and_learnings/02_Background_Agents.md`
- `<workspace-root>/docs/requirements_and_learnings/20_Memory_System.md`
- `<workspace-root>/docs/requirements_and_learnings/28_OpenClaw_Integration.md`
- `<workspace-root>/docs/requirements_and_learnings/46_Scheduled_Self_Continuity_Temporal_Freshness_2026-03-16.md`
- `<workspace-root>/docs/requirements_and_learnings/49_Workspace_Git_Push_and_Branching.md`
