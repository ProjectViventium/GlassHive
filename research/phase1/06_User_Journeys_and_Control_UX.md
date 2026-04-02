# User Journeys and Control UX

Date: 2026-03-18

## Status Update: 2026-03-19

The implemented phase-1 UX is now intentionally centered on one simple operator path:

1. create or open project
2. select existing worker or create new worker
3. enter project prompt
4. run
5. launch workstation surfaces, pause, interrupt, resume, or take over live

The project page is primary.
The worker detail page is secondary.
The live workstation page is now the primary takeover surface.
The terminal page remains the fallback surface.

## Purpose

This document defines the operator experience for the standalone `Workers & Projects Runtime`.

The goal is to make the system feel powerful without feeling mysterious.

## UX Principles

1. Every project and worker must be visible, named, and inspectable.
2. Every state transition must be explicit.
3. The user must always know what can be done next.
4. Interrupt and recovery must feel safer than starting over.
5. View and control are mandatory for display-backed or high-risk work and optional for lighter worker types.
6. Resumption must show prior context, not silently restart from scratch.
7. Failure messages must explain whether the next action should be retry, reconfigure, redirect, or take over.

## Core Screens or Views

### Project list

Must show:

- project title
- owner
- status
- active workers count
- last activity
- next recommended action

### Project detail

Must show:

- goal
- worker selector
- project prompt box
- latest output
- recent timeline
- recent project runs
- immediate control path to the selected worker

### Worker detail

Must show:

- worker name and role
- backend and profile
- current state
- current or last run
- recent event stream
- available controls
- takeover/watch link when supported
- runtime boundary details when sandboxed

## Required Controls

Every worker detail view must expose:

- `assign work`
- `interrupt`
- `pause`
- `resume`
- `terminate`
- `send message`
- `view artifacts`
- `view raw events`

If supported, also expose:

- `take over terminal`
- `watch live desktop`
- `open files`
- `open browser`
- `open codex`
- `open claude`
- `open openclaw`

## User Journeys

### Journey 1: create a new project

Expected flow:

1. User creates a project with title, goal, and optional default profile.
2. System returns project id and status immediately.
3. User sees recommended next step: create worker.

Acceptance:

- project creation feels instant
- no hidden background work is required to understand what happened

### Journey 2: spawn a worker and assign first task

Expected flow:

1. User selects project.
2. User chooses worker role and backend profile.
3. System creates worker and transitions it through `starting` to `ready`.
4. User assigns first task.
5. Live status and events appear within seconds.

Acceptance:

- backend profile is visible
- worker state is visible
- event stream starts without page refresh guessing
- worker terminal takeover is one click away from the project page

### Journey 3: interrupt and redirect

Expected flow:

1. User notices incorrect direction.
2. User clicks interrupt.
3. System acknowledges interrupt quickly.
4. Worker stops active execution and enters `paused` or `ready`.
5. User sends a redirect message.
6. Worker resumes with preserved context.

Acceptance:

- interrupt feels trustworthy
- user never wonders whether the old task is still secretly running

### Journey 4: resume next day

Expected flow:

1. User returns after hours or a full day.
2. Project summary explains recent history.
3. Worker detail shows current state and last successful checkpoint.
4. User resumes from prior state instead of starting over.

Acceptance:

- summary is current enough to re-enter fast
- resume is explicit and references prior session/workspace

### Journey 5: display-backed worker with live control surface

Expected flow:

1. User opens a sandboxed `codex-cli` or `claude-code` worker.
2. User clicks `take over` or opens the live workstation page.
3. System shows the real desktop tied to the exact worker sandbox.
4. User can launch browser, files, Codex, Claude Code, OpenClaw, or shell inside that same workstation.
5. User can pause the worker, take over, and later resume the same run.

Acceptance:

- control URL is discoverable
- control surface is tied to the exact worker/run being viewed
- desktop interaction is real rather than decorative
- terminal interaction survives page reload because the shell is backed by persistent `screen`

### Journey 6: failure and recovery

Expected flow:

1. Worker fails due to backend, auth, network, or task issue.
2. System marks failure clearly.
3. UI explains likely recovery paths.
4. User can retry, redirect, or replace the worker.

Acceptance:

- failure does not disappear into logs only
- user gets a clear next step

### Journey 7: compare workers in one project

Expected flow:

1. Project has multiple workers with different roles.
2. User can see which worker is doing what.
3. Summaries and artifacts remain attributable to the correct worker.

Acceptance:

- multi-worker work never becomes anonymous clutter
- project timeline is readable

## Timing Expectations

The UX should feel responsive according to these targets:

- status update after control action: under 2 seconds
- first visible events after assignment: under 5 seconds
- resume visibility after request: under 5 seconds for warm paths
- control URL availability: under 10 seconds for supported profiles
- terminal takeover attach: under 5 seconds on warm workers

## Anti-Patterns

Do not ship a UX where:

- a worker exists but there is no visible backend or role
- `resume` actually creates a new hidden worker
- `interrupt` is best-effort but presented as guaranteed
- control surfaces are hidden in logs instead of first-class UI
- takeover opens a page that is only a fake log tail instead of a real shell
- failures require reading backend internals to understand next steps

## What Viventium Should Eventually Consume

When integrated later, Viventium should consume this system at the level of:

- project creation and selection
- worker creation and selection
- worker control actions
- summaries
- artifacts
- metrics

Viventium should not need to know backend-specific process details to use the system well.
