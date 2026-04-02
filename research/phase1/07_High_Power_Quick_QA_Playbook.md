# High Power Quick QA Playbook

Date: 2026-03-19

## Purpose

This playbook is the fastest way to validate whether the standalone `Workers & Projects Runtime` is behaving like a real workstation-grade worker system instead of a thin task queue.

It is intentionally biased toward the user experience bar:

- project-first flow
- worker-first control
- real workstation surfaces
- real interruption and resumption
- persistent workspace and session continuity
- honest artifact capture
- real operator takeover

Use this after meaningful runtime, UI, sandbox, or worker-profile changes.

## Current Phase-1 Scope

This playbook covers the current implemented phase-1 surface:

- `codex-cli` worker profile inside the workstation sandbox
- `claude-code` workstation surface launch inside the sandbox
- `openclaw` workstation surface launch inside the sandbox
- persistent Docker workstation image with noVNC live view
- terminal takeover
- pause, resume, interrupt, terminate
- project-first operator UI
- latest visual artifact preview

Important honesty line:

- `codex-cli` is the strongest fully-proven workstation-backed worker profile today.
- `claude-code` workstation launch is proven as a surface; live Claude task execution still depends on local auth state.
- `openclaw-general` now targets the same workstation substrate through OpenClaw's embedded local agent mode. Worker boot and workstation surfaces are proven; live in-container task execution is the remaining signoff item.

## Preconditions

Before running the playbook, confirm:

- Docker Desktop is running
- local service is up at `http://127.0.0.1:8766`
- the workstation image exists or can be built:
  - `workers-projects-runtime-workstation:phase1`
- `codex` local auth is valid on the host and seedable into the worker sandbox
- optional: `claude` local auth is valid on the host if testing Claude worker execution

Health check:

```bash
curl -s http://127.0.0.1:8766/health | jq .
```

## Fast Smoke: 5 Minutes

### 1. Open the project-first UI

Steps:

1. open `http://127.0.0.1:8766/ui`
2. create a project
3. create a `codex-cli` worker
4. land on the project workspace page

Expected:

- project is created immediately
- worker becomes `starting` then `ready`
- project page shows:
  - `Run Project`
  - `Selected Worker`
  - `Workstation Tools`
  - `Live View`
  - `Recent Worker Events`

Fail if:

- worker silently fails without readable cause
- there is no obvious takeover path from the project page

### 2. Verify workstation surfaces launch

Steps:

1. on the project page, press:
   - `Open Files`
   - `Open Browser`
   - `Open Codex`
   - `Open Claude`
   - `Open OpenClaw`
2. open the takeover page
3. confirm those surfaces are visible inside the same desktop

Expected:

- each action returns quickly
- same worker desktop URL remains stable
- browser, files, and terminals appear on the same desktop session

Fail if:

- buttons launch nothing
- desktop URL changes per action
- worker state flips to failed from a simple workstation launch

### 3. Run one simple Codex task

Prompt:

```text
Create a file named workstation_probe.md that briefly confirms the sandbox supports browser, Codex, Claude Code, OpenClaw, files, and shell surfaces. Then respond with exactly WORKSTATION_PROBE_OK.
```

Expected:

- run enters `running`
- run completes successfully
- latest output includes `WORKSTATION_PROBE_OK`
- file appears in workspace

Evidence:

- latest output on the project page
- workspace file list
- `Recent Worker Events`

## High Power Operator Test: 10 to 15 Minutes

### 4. Run a browser checkpoint task without submitting

Use a fresh `codex-cli` worker or the same one if clean.

Prompt:

```text
Inside this sandbox workspace, create and run a Python script named demo_form_checkpoint.py using Selenium remote Chrome at http://localhost:4444/wd/hub. Open a live demo form page, fill it with placeholder operator data, but DO NOT submit. After the form is filled and ready for human review, save a screenshot to demo_form_checkpoint.png and a summary text file to demo_form_checkpoint.txt, keep the browser visible and idle for 120 seconds so the operator can watch or take over, then exit and respond with exactly DEMO_FORM_CHECKPOINT_READY.
```

Expected:

- run becomes `running`
- browser visibly opens in the live workstation
- form becomes populated
- `demo_form_checkpoint.png` appears
- `demo_form_checkpoint.txt` appears
- project page shows `Latest Visual Artifact`
- operator can open desktop and inspect before submission

Fail if:

- browser action happens only headlessly with no visible desktop proof
- no screenshot artifact appears
- worker loses control surface during run

### 5. Pause and resume the same active run

Steps:

1. while the checkpoint task is still active, press `Pause`
2. confirm worker becomes `paused`
3. confirm latest run remains the same run id and remains `running` or in-flight rather than being finalized incorrectly
4. press `Resume`
5. confirm the same run id continues

Expected:

- pause freezes the worker
- resume continues the same run
- desktop view remains attached to the same worker
- workspace files remain present

Fail if:

- resume starts a new hidden run
- pause finalizes the run as failed or completed
- browser state is lost

### 6. Take over live

Steps:

1. use `Pause + Open Desktop`
2. click inside the live desktop
3. verify you can interact with the browser window or terminal
4. return control with `Resume`

Expected:

- takeover is obvious and low-friction
- desktop is real and interactive
- resume hands control back without replacing the worker

Fail if:

- user must guess where to click
- view is stale or decorative only
- operator cannot meaningfully intervene

### 7. Interrupt instead of pause

Steps:

1. queue a longer-running task
2. press `Interrupt`
3. confirm worker returns to `ready`
4. confirm active run is marked `interrupted`

Expected:

- interrupt stops active work quickly
- worker becomes ready for redirection
- run history records interruption explicitly

Fail if:

- interrupt behaves like hidden terminate
- worker remains stuck in `running`
- run disappears from history

## Persistence and Recovery: 10 Minutes

### 8. Service restart recovery

Steps:

1. ensure a worker already has workspace files and a session
2. restart the runtime service
3. reopen the project page
4. reopen the worker console and desktop

Expected:

- project and worker records persist
- workspace files remain
- worker can be resumed or used again
- takeover URL is re-established

Fail if:

- worker becomes orphaned
- workspace is lost
- runtime metadata no longer matches actual sandbox state

### 9. Multi-worker isolation

Steps:

1. create two workers in one project
2. run different tasks in each
3. launch workstation surfaces in each
4. confirm each has distinct workspace and container identity

Expected:

- separate worker ids
- separate workspace roots
- separate desktop URLs or runtime identities
- no file bleed between workers

Fail if:

- files from one worker appear in the other
- operator actions target the wrong worker

## Deep Checks

### 10. Cold start honesty

Steps:

1. remove the workstation image if intentionally testing cold path:
   - `docker rmi workers-projects-runtime-workstation:phase1`
2. create a fresh `codex-cli` worker
3. measure time from create request to `ready`

Expected:

- startup may be slower, but the state should remain honest
- user should see `starting` until ready
- no false healthy or fake readiness

Regression this catches:

- Node or package-level breakage during image build
- silent image build failures
- fake readiness before the sandbox exists

### 11. Warm start performance

Steps:

1. create a second `codex-cli` worker after the image is already built
2. compare create-to-ready time against the cold start

Expected:

- materially faster than first cold build
- same functional behavior

### 12. Latest artifact preview usefulness

Steps:

1. run any task that produces `png`, `jpg`, `jpeg`, `webp`, or `gif`
2. confirm the project page shows `Latest Visual Artifact`
3. confirm it updates after a new image is written

Expected:

- operator sees the most recent meaningful frame without opening the raw workspace
- useful when the live desktop is now idle or has moved on

## Failure Classification

When a test fails, classify it immediately:

- `sandbox build failure`
  - image build, package manager, Node, Docker, base image
- `worker runtime failure`
  - Codex, Claude, OpenClaw execution behavior
- `operator UX failure`
  - controls unclear, takeover hidden, wrong next action
- `continuity failure`
  - pause/resume/restart breaks session truth
- `artifact failure`
  - screenshots or summaries missing despite visible work
- `isolation failure`
  - workers leak across workspaces or actions

## Minimum Phase-1 Exit Bar

Phase 1 is in acceptable shape only if all of the following are true:

1. a fresh `codex-cli` worker cold-starts successfully from a clean workstation image path
2. the project page exposes all major operator actions without digging into internals
3. a real worker task completes and leaves durable workspace artifacts
4. live workstation surfaces launch on demand in the same sandbox
5. pause and resume preserve the same run and same worker session truthfully
6. interrupt stops work cleanly and leaves a usable worker
7. latest visual artifact preview reflects real saved output
8. service restart does not destroy project/worker continuity

## Current Live Reference Proof

Fresh live proof created on 2026-03-19:

- project: `prj_9456a21400`
- worker: `wrk_da2c2a526e`
- project page:
  - `http://127.0.0.1:8766/ui/projects/prj_9456a21400?worker_id=wrk_da2c2a526e`
- takeover page:
  - `http://127.0.0.1:8766/ui/workers/wrk_da2c2a526e/view`
- direct desktop:
  - `http://127.0.0.1:62310/?autoconnect=1&resize=scale&reconnect=1`

Live proof outcomes:

- workstation image cold-start was fixed by upgrading the sandbox build path to Node 20+
- worker reached `ready` successfully on the new workstation image
- workstation actions launched:
  - files
  - codex
  - openclaw
  - browser
  - claude
  - terminal
- Codex run completed with `WORKSTATION_PROBE_OK`
- browser checkpoint artifacts were produced:
  - `browser_checkpoint.py`
  - `browser_checkpoint.png`
  - `browser_checkpoint.txt`
- pause and resume preserved the same active run id during the checkpoint flow
