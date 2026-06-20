<!-- VIVENTIUM START: Glass Hive vision and requirements -->
# Glass Hive: Vision, Requirements, and Terminology

## Vision

Glass Hive is a standalone runtime for powerful AI workers that can be resumed, watched, interrupted, and steered in real time.

It is meant to feel like this:

- define a `Project`
- choose or create a `Worker`
- run it inside a persistent `Sandbox` or, when explicitly selected, on the `Host Computer`
- watch what it is doing live
- interrupt or take over when needed
- resume later without losing the work surface

## Product Requirements

Glass Hive should provide:

1. a project-first operator flow
2. persistent sandbox or host workspace state
3. fast warm resume
4. controllable workers
5. explicit human takeover
6. compatibility with multiple worker runtimes
7. client independence
8. strong local-first posture
9. safe publication through MCP and direct HTTP APIs
10. a clean path for auth, MCP, memory, context, and callback injection
11. a universal worker completion self-check before any user-facing final report
12. harness-level prerequisite preflight and safe recovery before asking the user to fix local runtime substrate
13. sparse, faithful delegation: pass goals, constraints, files, MCP/tool capability context, and
    explicit success conditions without inventing plans, rubrics, artifacts, provider lists, or tool
    results for the worker

## Core Worker Operating Instructions

The public source of truth is `docs/requirements_and_learnings/48_GlassHive_Workstation_Sandbox_Runtime.md`.
The nested GlassHive runtime follows the same canonical worker contract:

```
CRITICAL OPERATING INSTRUCTIONS (FOLLOW STRICTLY):

1. PATH OF LEAST RESISTANCE: Use the simplest, most direct solution. Don't reinvent wheels.

2. JUST DO IT: Execute immediately without asking questions. Users want RESULTS. Rely on your intelligence, tools, MCPs, skills to find ways around blockers to get it done full and complete.

3. SELF-TEST AND VERIFY:
   - After creating code, RUN IT
   - After starting a server, CURL IT to confirm it responds
   - After researching or creating files, open them and deliver them
   - NEVER report success without verification
   - Debate with yourself on gaps, issues, mistakes, misalignments in your delivery and work on them. Do not stop early. Do not just tell the user what you missed. Actually take action and address them so that the delivery to the user is complete and reliable.

4. LOOP UNTIL SUCCESS:
   - If something fails, FIX IT and try again
   - Keep iterating until ACTUALLY COMPLETE

5. NO USER INTERVENTION: Deliver a COMPLETE, WORKING solution.
```

This is bounded by the runtime safety model: it does not override tenant/user scope, auth boundaries,
destructive host-action checkpoints, or cost/time controls. Workers should fix what they can and
report a concrete blocker instead of looping indefinitely.

## Non-Goals

Glass Hive is not:

- a LibreChat-only feature
- a Skyvern replacement
- a provider-specific wrapper tied only to Anthropic or OpenAI
- a promise of hostile multi-tenant security from phase-1 Docker alone

## Terminology

### Sandbox
A persistent workstation-like execution environment that holds the worker's home directory, workspace, processes, and live control surfaces.

### Host Computer
A no-sandbox execution mode where the worker uses local CLIs and OS/browser/filesystem access on
the user's main machine. This mode is intentionally powerful and must be selected explicitly.

### Worker
The active AI runtime inside a sandbox or host workspace.
Examples:

- `codex-cli`
- `claude-code`
- `openclaw-general`

The worker profile plus execution mode is the runtime selector. The legacy API field
`backend=openclaw` may still appear for compatibility, but it must not be presented as the product
backend or used to override `codex-cli`, `claude-code`, or `openclaw-general` profile selection.

There are three separate OpenClaw concepts that must not be collapsed:

- `integrations.openclaw` is a lab/integration toggle.
- `@openclaw` is a mention/activation alias.
- `openclaw-general` is a worker profile, selected the same way as `codex-cli` or `claude-code`.

### Project
The durable task definition around one or more workers, including goal, success criteria, continuity, and audit trail.

### Bootstrap Bundle
A portable preset describing what should be projected into a worker when it starts or resumes.
It must be able to carry general instructions that tell the worker to verify its own deliverable
against the user's request and success criteria without overfitting to one client, file type, or task.
It should preserve real data in and data out: uploaded bytes/references, broker grants,
capabilities, and retrieved context are projected when present; missing data is represented as a
blocker or unavailable capability, not filled in by the host assistant.

## Client Compatibility Goal

Glass Hive should work with:

- Viventium / LibreChat
- Claude / Claude Code
- Codex
- ChatGPT-compatible MCP clients
- direct operator API usage

The runtime must remain usable even when none of those clients are present.
