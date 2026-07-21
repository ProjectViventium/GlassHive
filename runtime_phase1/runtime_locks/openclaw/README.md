# GlassHive OpenClaw reviewed runtime lock

This npm lock freezes the dependency graph selected for OpenClaw `2026.7.1-2`. The root package is
an exact integrity-pinned tarball, and `fast-uri@3.1.3` narrowly replaces the vulnerable 3.1.2
transitive selected by the unmodified graph.

Generate and verify with the exact supported runtime used for this review:

```sh
npm ci --omit=dev
node_modules/.bin/openclaw --version
npm audit --omit=dev
```

Expected Node is `22.23.1`, npm is `10.9.8`, and OpenClaw reports `2026.7.1-2 (0790d9f)`. The
2026-07-21 production audit reports 0 critical, 0 high, and 6 moderate vulnerable packages. This
lock does not authorize a GlassHive runtime entrypoint to fall back to another version. The owning
runtime contract is documented in `../../README.md`.
