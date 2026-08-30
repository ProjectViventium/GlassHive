# GlassHive Component

GlassHive is a separate git and instruction root. When this checkout lives under Viventium and
`../../AGENTS.md` exists, read it before Viventium-specific work; it owns shared scope, public/private
safety, delivery, and verification rules.

## Worker Runtime Boundary

- Apply the parent host/worker brokerage invariant. GlassHive workers are general intelligent
  workers: give them the real goal, constraints, files, capabilities, and tool results, then let
  them choose the path.
- Do not predict or hardcode the provider, account, tool, artifact, or workflow unless the user
  explicitly selected it or verified structured evidence requires it.
- Harness/runtime owns reliable data in/out, prerequisite recovery, authorization boundaries,
  cancellation, persistence, and observable completion. Model judgment owns planning and tool choice.
- Solve reliability with typed contracts, capability metadata, receipts, logs, and tests. Never route
  from prompt text, human-facing names, tool substrings, provider labels, or one user's wording.

## Component Safety

- Keep prompts and evidence public-safe. Never place credentials, private user data, raw exports,
  private paths, or owner-machine state in tracked fixtures, logs, docs, or reviewer handoffs.
- Preserve unrelated changes. Do not commit, push, or update the parent component pin unless the user
  requested those actions.
- A source change here is not shipped until this component commit, the parent pin, any built artifact,
  and the installed/running artifact agree where applicable.
- Run the smallest relevant component tests. For Viventium user-visible paths, follow the parent QA
  contract; worker output or unit tests alone do not replace the affected user surface.
