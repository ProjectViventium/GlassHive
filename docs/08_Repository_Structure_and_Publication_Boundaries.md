<!-- VIVENTIUM START: Glass Hive repo structure and publication boundaries -->
# Glass Hive: Repository Structure and Publication Boundaries

## Purpose

Keep Glass Hive publish-ready while preserving a strict line between:

- the standalone product repo
- private user-specific state
- external client integrations such as Viventium / LibreChat

## Repository Structure

The repository is organized as follows:

- `README.md`
  - project entry point
- `docs/`
  - current source-of-truth product docs
- `research/`
  - historical planning and exploratory phase documents
- `contracts/`
  - API and example payloads
- `runtime_phase1/`
  - current standalone runtime implementation
- `frontends/`
  - separate frontend/operator surfaces

## Boundary Rules

What belongs in the Glass Hive repo:

- product code
- publish-oriented docs
- tests
- API contracts
- example payloads
- neutral defaults that are safe to share

What does not belong in the Glass Hive repo:

- live secrets
- connected-account tokens
- personal prompts
- owner-private notes
- machine-specific runtime captures
- private bootstrap bundles containing real credentials

## Private Companion Location

Owner-specific or confidential Glass Hive state should live under the dedicated private companion repo, in a clearly named Glass Hive subtree.

Recommended categories there:

- private bootstrap bundles
- provider presets
- personal prompts
- private QA captures
- machine-specific notes

## Viventium Integration Boundary

Glass Hive may be consumed by Viventium / LibreChat, but it must remain independently runnable and publishable.

That means:

- Viventium integration docs may reference Glass Hive
- Glass Hive may expose MCP and HTTP APIs for Viventium
- Glass Hive should not require Viventium internals to run
- Viventium-specific local configs should stay in the Viventium workspace, not in Glass Hive core
