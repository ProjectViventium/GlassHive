<!-- VIVENTIUM START: Glass Hive unified operator UI -->
# Glass Hive: Minimal Unified Operator UI

## Purpose

Define a separate, minimal, modern operator UI for Glass Hive that does not alter the existing runtime UI and does not mix frontend concerns into Glass Hive core runtime code.

## Visual Thesis

Calm dark glass over a bright signal core, like a modern appliance or Tesla control surface rather than a developer dashboard.

## Content Plan

1. Entry composer
2. Automatic transition into live watch mode
3. Minimal ribbon controls
4. Hidden advanced controls only when needed

## Interaction Thesis

- the centered project composer should feel like the only thing the user needs to understand
- after submit, the interface should hand off immediately into live watch mode
- advanced controls stay tucked away until the user explicitly asks for them
- the default watch handoff should land on the live desktop, not the raw terminal page
- user-facing language should call long-lived personal environments `Workspaces`, not `workers` or `sandboxes`

## Hard Requirements

1. Do not edit or replace the existing Glass Hive runtime UI.
2. Do not mix this frontend into Glass Hive core runtime code.
3. The new UI must live in a separate service/surface.
4. The entry screen must present one centered project definition box.
5. That box must contain exactly three fields:
   - `Describe your project`
   - `Success Criteria`
   - `Context`
6. `Success Criteria` is required.
7. `Context` is optional.
8. There must be an optional smooth workspace selector for:
   - `New workspace`
   - existing named workspaces
9. On submit:
   - create or reuse the selected workspace's underlying worker
   - send a hardened operator brief
   - redirect automatically to the live watch screen
10. The watch screen must prioritize the live sandbox view.
11. For webpage, app, and browser-visible deliverables, the watch screen must end on the delivered result itself rather than a raw terminal transcript.
12. The raw live terminal session must still exist and stay available as a secondary surface for takeover and debugging.
13. When desktop-first watch is enabled, the active worker terminal must also be visible inside the desktop itself by default so the operator can watch the real live session without leaving desktop view.
14. The watch screen must expose a direct, obvious path to the full runtime project workspace for operators who want the richer dashboard and control plane.
15. The launch composer may expose the initial watch surface only as an advanced control. The primary project box must remain the same three-field experience.
16. If project launch fails after a worker was created, the UI/runtime must record that launch as an explicit failure instead of leaving a healthy-looking but runless worker behind.
17. The main controls must be simple and obvious:
   - pause
   - resume/play
   - interrupt
   - more menu
   - delete/shut down available from more menu
18. The latest result must be visible in the top ribbon and expandable without leaving the live view.
19. The steering box must remain visible across normal desktop widths and mobile-safe layouts.
20. A non-technical user should be able to understand the flow with near-zero explanation.

## User-Facing Workspace Model

- In the user-facing Glass Hive UI, a persistent personal execution environment is called a
  `Workspace`.
- Internally, Glass Hive runtime terms remain:
  - `worker` for the AI runtime identity
  - `sandbox` for the isolated workstation container
- A workspace is therefore a friendly label over:
  - one stable worker alias
  - one persistent home directory
  - one persistent project workspace
  - one browser profile / website-login state surface

## Least-Resistance V1 UX

The easiest non-technical user flow should be:

1. show recent named workspaces first, not raw worker IDs
2. make `Open workspace` the primary reuse action
3. make `New workspace` the only primary creation action
4. auto-reuse the matching workspace when the parent system already knows the stable alias for the
   task or service
5. land directly in the desktop-first watch view after open or create
6. keep advanced lifecycle or debugging controls behind the existing ribbon / more menu

`Open workspace` should automatically resume a paused workspace. Non-technical users should not
need to choose between `open` and `resume`.

### V1 Action Set

Primary:

1. `Open workspace`
2. `New workspace`

Secondary:

1. `Rename workspace`
2. `Pause`
3. `Resume`
4. `Interrupt`
5. `Delete`

### Not A V1 Primary Action

Do not make `Duplicate workspace` part of the default v1 flow.

Reason:

1. the docs support stable named workspace reuse, not browser-profile cloning
2. blindly duplicating a browser profile can clone cookies and active sessions in surprising ways
3. the simplest user story is reuse-or-new, not branching into clone semantics

If duplication is added later, it should default to copying project files and bootstrap context
without copying browser-session state unless the product explicitly defines and approves that
behavior.

## Rename Semantics

- A workspace should have:
  - a stable internal alias for parent-side routing
  - a user-facing display label for the glossy UI
- `Rename workspace` in the glossy UI should update the display label, not silently rewrite the
  stable routing alias.
- If the product later supports alias editing, that must be a separate explicit flow because it can
  affect parent-side auto-reuse behavior.

## Derived Operator Brief Template

A canonical master prompt template was not found in the current repo search.

For this UI, the phase-1 operator brief should be:

- project description
- success criteria
- optional context
- execution rules:
  - treat success criteria as hard acceptance gates
  - keep working and researching until criteria are satisfied or a real blocker appears
  - keep asking: have I achieved this successfully?
  - pause before risky or irreversible external actions
  - if the deliverable is a webpage or app, open the final result in the sandbox browser and leave it visible
  - if the result is a simple static page, prefer opening the final HTML file directly instead of relying on a temporary localhost server

## Architecture Decision

Use a separate frontend service that:

- serves the new UI
- proxies requests to the existing Glass Hive runtime API
- keeps CORS and browser-origin concerns out of the runtime
- leaves the current runtime UI untouched

## Success Criteria

1. Existing Glass Hive runtime UI still works unchanged.
2. The new UI works as a separate service.
3. A user can launch a project from the new UI with one primary interaction flow.
4. A user is redirected into a live watch surface automatically.
5. A worker can be paused, resumed, interrupted, and deleted from the new UI.
6. The live desktop remains the dominant surface.
7. A hello-world landing page task ends on the rendered page in the sandbox browser.
8. The active worker terminal is visible inside the desktop-first watch flow by default.
9. The exact live terminal session remains available from the same watch screen.
10. The watch screen provides a first-class link to the runtime project workspace without forcing the operator to discover it inside an embedded iframe.
11. Launches that fail after worker creation leave an explicit failure trail instead of a silent orphan worker.
12. The UI feels simple enough for a non-technical user.
13. A non-technical user can understand that reopening a named workspace returns them to the same files, browser setup, and login state.
14. The primary action set is reuse-or-new; duplicate behavior is not required for v1 acceptance.
15. `Open workspace` is the single reuse verb and automatically resumes paused workspaces.
16. Renaming a workspace changes the user-facing label without breaking the stable parent-side routing alias.

## Current Phase-1 Clipboard Note

The sandbox desktop currently relies on noVNC. Clipboard support is available through the noVNC clipboard controls and browser clipboard permissions, but truly seamless automatic browser-clipboard synchronization is still constrained by browser security rules and is not yet elevated into a custom first-class GlassHive clipboard bridge.
