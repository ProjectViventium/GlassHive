# Changelog

## Unreleased

### Added

- User-scoped personal Codex and Claude accounts with isolated native credential leases.
- Private, human-named, favorite workspaces that retain files and native tool, plugin, connector,
  and trust state across browser and MCP reuse.
- Thin native Codex and Claude packages that share one concise GlassHive MCP skill.
- One-action workspace reuse from fresh Codex and Claude MCP clients.

### Changed

- **Set up tools** uses the selected personal account while the workspace remains the owner of its
  native configuration.
- Provider names are prefilled and remain editable.
- External-client guidance selects one matching GlassHive action and keeps callback and tool-catalog
  details out of the ordinary user flow.

### Fixed

- Personal-account removal is retryable and does not leave a stuck connection row or credential
  container.
- Expired personal sessions return an actionable reconnect state.
- Native Claude authentication composes with workspace-local connector state instead of replacing it.
- Incidental external URLs in worker prose are not treated as delivered pages or opened automatically.

### Verified scope

The installed Ultimate Phase 1 path passed personal Codex and Claude work, native connected-service
reuse, favorite-workspace refresh and reuse, and control of the same workspace from fresh Codex and
Claude MCP clients. See [the release notes](docs/12_Ultimate_Phase_1_Release_Notes.md). Broader
two-owner, confirmed-write, clean-install, and full restore testing remains outside this accepted
scope.
