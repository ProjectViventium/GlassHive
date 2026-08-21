# Ultimate Phase 1 Release Notes

## User outcome

An authenticated user can:

1. connect a personal Codex or Claude account in **Connections**;
2. create or reuse one private, human-named, favorite workspace;
3. open the workspace's native AI harness with **Set up tools**;
4. connect and use provider-native services without connector-specific GlassHive code;
5. return after refresh or compute release and reuse the workspace, files, and native setup; and
6. control that same workspace from a fresh Codex or Claude MCP client with one launch and one
   bounded wait when completion is requested.

## Interaction contract

- The ordinary path shows one clear action. Internal IDs, callback plumbing, tool inventories, and
  raw provider output stay behind progressive disclosure or remain operator-only.
- Account names are prefilled and editable.
- Connections authenticate accounts. Workspaces own persistent work and native harness state.
  Library owns reusable non-secret packages. These surfaces do not duplicate each other.
- GlassHive uses the selected harness's native connector and plugin mechanisms. It does not add
  connector-specific frontend flows, copy connector credentials, or encode one provider or user in
  runtime logic.
- The host passes the user's goal and verified capabilities to the worker. The worker chooses the
  useful path.

## Isolation and lifecycle

- Provider credentials remain outside the workspace and are mounted only while an exact mission or
  interactive setup lease is active.
- The credential-bearing container is removed before the lease is released.
- Claude keeps native connector, plugin, trust, and onboarding state in the workspace home while
  only its selected account secure storage is projected during the lease.
- Codex preserves workspace-local state while temporarily overlaying only selected account
  authentication.
- A setup window and a mission cannot use the same account concurrently. The losing action fails
  before work is created and gives a clear recovery step.
- Remove, reconnect, cleanup retry, service restart, stale lease, and expired-session paths fail
  closed without exposing credentials or changing another user's route.

## Accepted evidence

Across the accepted installed runs, the evidence proved:

- personal Codex and Claude account readiness and missions;
- native connected-service reads without connector-specific GlassHive wiring;
- one favorite workspace retained files and connector state after refresh and compute release;
- a normal browser mission reused that workspace after native setup closed;
- fresh isolated Codex and Claude MCP clients reused the same workspace with one launch and one
  bounded wait; and
- removal of a stale personal connection completed and remained removed after refresh.

The current Codex rerun was blocked by the provider account's usage limit. It was not counted as
fresh current-run proof; the earlier accepted installed Codex mission and connected-service reuse
remain the evidence for that lane.

The product owner re-tested the installed flow and accepted this scope on 2026-08-21.

## Not claimed

This release does not claim completion of the full multi-user product roadmap. Two-owner isolation,
confirmed connected-service writes, revoke and renewal, automatic clock-fired recurrence, a real
Library package used by a worker, clean install, and full upgrade/restore remain separate gates.

## Regression triggers

Rerun the complete Ultimate Phase 1 case when changing provider-account setup or cleanup, worker
homes, sandbox mounts, native client packages, workspace alias/favorite resolution, MCP OAuth or
launch/wait behavior, the Connections or Workspaces UI, or release-state migration.
