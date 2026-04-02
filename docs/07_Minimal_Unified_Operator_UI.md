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
8. There must be an optional smooth worker selector for:
   - `New main worker`
   - existing workers
9. On submit:
   - create or reuse the selected worker
   - send a hardened operator brief
   - redirect automatically to the live watch screen
10. The watch screen must prioritize the live sandbox view.
11. For webpage, app, and browser-visible deliverables, the watch screen must end on the delivered result itself rather than a raw terminal transcript.
12. The raw live terminal session must still exist and stay available as a secondary surface for takeover and debugging.
11. The main controls must be simple and obvious:
   - pause
   - resume/play
   - interrupt
   - more menu
   - delete/shut down available from more menu
13. The latest result must be visible in the top ribbon and expandable without leaving the live view.
14. The steering box must remain visible across normal desktop widths and mobile-safe layouts.
15. A non-technical user should be able to understand the flow with near-zero explanation.

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
8. The exact live terminal session remains available from the same watch screen.
9. The UI feels simple enough for a non-technical user.

## Current Phase-1 Clipboard Note

The sandbox desktop currently relies on noVNC. Clipboard support is available through the noVNC clipboard controls and browser clipboard permissions, but truly seamless automatic browser-clipboard synchronization is still constrained by browser security rules and is not yet elevated into a custom first-class GlassHive clipboard bridge.
