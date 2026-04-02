# QA, Acceptance, and Exit Criteria

Date: 2026-03-18

## Status Update: 2026-03-19

Validated in this environment:

- fast API/UI suite passes
- live `Codex CLI` worker runs inside persistent Docker sandbox and resumes across turns
- worker terminal takeover websocket works end to end
- sandboxed worker workspace persists files across runs
- `OpenClaw` worker boot now succeeds on the same workstation substrate used by `codex-cli` and `claude-code`
- sandboxed browser-desktop live view works through the service UI and direct noVNC URL
- live pause and resume semantics preserve the same active worker run instead of falsely restarting it
- a real `Codex CLI` browser worker visibly opened a live demo form, filled it, clicked submit, held the browser open for observation, and saved result artifacts in the worker workspace
- the demo-form result was captured honestly as the site's inline error state, not masked as a success
- the standalone MCP wrapper is implemented and wired into Viventium / LibreChat source-of-truth configs

Current external blocker:

- `Claude Code` live runtime is implemented but cannot be fully signed off on this machine without valid Anthropic login or sufficient API credits
- `openclaw-general` now uses OpenClaw's embedded `agent --local` path inside the workstation sandbox, but the live in-container task execution step still needs final signoff before this profile can be called fully complete
- the live demo-form submit endpoint currently returns HTTP `403` for the submission path used by both host-browser and sandbox-browser automation, so a successful booking cannot be claimed from this environment without further site-specific allowance or a different submission path

## Evidence Snapshot: 2026-03-20

Live proof worker:

- project: `prj_628f867ee7`
- worker: `wrk_36c6a9936c`
- run: `run_c475ac8409`

Saved artifacts:

- `<private-research-root>/workers_projects_runtime/runtime_phase1/data/docker_sandboxes/workers/wrk_36c6a9936c/state/workspace/demo_form_run.py`
- `<private-research-root>/workers_projects_runtime/runtime_phase1/data/docker_sandboxes/workers/wrk_36c6a9936c/state/workspace/demo_form_result.txt`
- `<private-research-root>/workers_projects_runtime/runtime_phase1/data/docker_sandboxes/workers/wrk_36c6a9936c/state/workspace/demo_form_result.png`

Observable proof points:

- live desktop view showed the real demo form with fields filled by the sandboxed worker
- live desktop view showed the site's red inline `Something went wrong` result state after submit
- worker event timeline recorded `run.started`, then `worker.paused`, then `worker.resumed`, then `run.completed` on the same run id
- run output included the worker's own step-by-step reasoning plus the final one-line status:
  - `DEMO_FORM_DONE: Submitted the live form in visible Chromium via Selenium, saved demo_form_run.py, demo_form_result.txt, and demo_form_result.png, and the site ended in its inline error state after the 90-second observation window.`

Additional workstation proof from the generalized sandbox path:

- project: `prj_9456a21400`
- worker: `wrk_da2c2a526e`
- workstation image: `workers-projects-runtime-workstation:phase1`
- delivered UI proof:
  - project page now exposes `Workstation Tools`
  - project page now exposes `Latest Visual Artifact`
  - takeover page now exposes direct launch buttons for shell, files, browser, Codex, Claude Code, and OpenClaw
- delivered runtime proof:
  - cold-start worker image build was fixed by upgrading the workstation build path to Node 20+
  - fresh worker reached `ready` after the image rebuild
  - `WORKSTATION_PROBE_OK` completed on the fresh worker
  - `demo_form_checkpoint.py`, `demo_form_checkpoint.png`, and `demo_form_checkpoint.txt` were created in the worker workspace
  - pause and resume preserved the same active run id on the checkpoint flow

## QA Philosophy

Phase 1 should not be called successful because the API exists.

Phase 1 is only successful if the system proves:

- it can control real workers
- it can recover from real failure
- it can persist and resume real work
- it gives users honest operational visibility
- it supports intervention without corrupting state

Operator execution playbook:

- `<private-research-root>/workers_projects_runtime/07_High_Power_Quick_QA_Playbook.md`

## Primary QA Categories

### 1. Functional lifecycle QA

Validate:

- create project
- spawn worker
- assign task
- interrupt worker
- pause worker
- resume worker
- terminate worker
- fetch events and artifacts
- recover after process restart

### 2. Backend adapter QA

Validate per adapter:

- worker creation succeeds
- task dispatch succeeds
- status mapping is accurate
- control operations map correctly to backend semantics
- artifact collection works
- error conditions are surfaced cleanly
- sandbox boundary is real and inspectable for CLI workers

### 3. UX QA

Validate:

- project list is readable and current
- worker state changes are visible within seconds
- logs do not stall silently
- errors include actionable next steps
- takeover/watch links are exposed when supported
- summaries are updated after work completes or fails
- terminal takeover opens a usable interactive shell, not just logs

### 4. Reliability QA

Validate:

- service restart with active and paused workers
- network blip to backend
- backend crash while run is active
- duplicate interrupt call
- duplicate resume call
- stale takeover link
- orphaned worker reconciliation

### 5. Security QA

Validate:

- project A cannot access project B artifacts without permission
- user A cannot list user B workers
- sandbox profile policy is applied per worker
- logs and summaries do not casually persist raw secrets
- revoked takeover links stop working

## Acceptance Test Matrix

| ID | Scenario | Expected result |
|---|---|---|
| A1 | Create `Project Alpha` and `Project Beta` | Distinct project ids and storage roots |
| A2 | Spawn 2 workers under one project | Both visible and independently addressable |
| A3 | Assign work to worker 1 | Run enters `running` and events stream |
| A4 | Interrupt worker 1 mid-run | Worker transitions out of active execution quickly |
| A5 | Resume worker 1 | Worker continues from prior session when supported |
| A6 | Restart control plane with paused worker | Worker and project states reconcile correctly |
| A7 | Worker exposes control surface URL | Operator can open control view successfully |
| A7b | Sandboxed worker exposes terminal takeover URL | Operator can open real interactive terminal successfully |
| A7c | Sandboxed browser worker exposes desktop live view | Operator can watch the real browser session while the worker acts |
| A8 | Terminate worker 2 | Resources and state close cleanly |
| A9 | Metrics endpoint after several runs | Timings and counts are populated |
| A10 | Failed backend task | Failure is visible with actionable context |
| A11 | Pause active browser run | Same run remains recoverable and the live sandbox freezes |
| A12 | Resume paused browser run | Same run continues and completes without starting a fresh session |

## Benchmark Targets

These are phase-1 targets, not promises for every future backend.

| Metric | Target |
|---|---|
| Project creation | under 500 ms local |
| Warm worker attach or resume | under 5 seconds |
| Cold worker ready for code profile | under 20 seconds |
| Worker control surface ready | under 10 seconds |
| Terminal websocket attach | under 5 seconds |
| Interrupt acknowledgement | under 2 seconds |
| Event propagation p95 | under 1 second |
| Control-plane restart recovery scan | under 10 seconds for small local workloads |

## Required Automated Tests

### Unit

- project model validation
- worker lifecycle transition rules
- adapter interface conformance
- state reconciliation rules
- artifact path generation

### Integration

- SQLite persistence
- OpenClaw adapter lifecycle
- control-surface URL and state sync
- sandboxed Codex lifecycle and resume
- paused Docker sandbox reconciliation
- restart recovery path

### End-to-end

- create project, spawn worker, assign task, interrupt, resume, complete
- create display-backed worker, open control surface, pause, resume, continue
- create display-backed worker, observe real browser automation result and saved artifacts
- restart service and continue existing project

## Manual Operator QA

The owner should manually validate these journeys before phase-1 signoff:

1. Start service from clean local state.
2. Create a project from the console or API.
3. Spawn a default OpenClaw worker.
4. Assign a real task.
5. Interrupt it midstream.
6. Redirect it with a new instruction.
7. Resume and verify continuity.
8. Create a sandboxed Codex worker and open the desktop live view.
9. Restart the service and confirm project and worker history remain intact.
10. Review artifact and metrics output.

## Exit Criteria For Phase 1

Phase 1 is complete only when all of the following are true:

1. The service works standalone without any Viventium code dependency.
2. The OpenClaw adapter supports real worker lifecycle and resumption.
3. A real operator control surface is wired and testable.
4. API surface is stable enough for an external client to use.
5. Persistence and restart recovery are proven.
6. Benchmarks and QA evidence are recorded in one place.
7. The remaining work is mostly integration, `openclaw-general` live signoff, Claude auth hardening, and broader sandbox hardening, not product-definition confusion.

## Explicit Failure Conditions

Do not call phase 1 done if any of these remain true:

- workers only work through ad hoc shell commands
- resume actually means "start fresh and pretend"
- worker control/view exists only in theory
- state is lost on restart
- clients must know backend-specific implementation details
- QA evidence is anecdotal instead of repeatable
