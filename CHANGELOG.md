# trace:exempt reason=prose-docs-no-behavior

# Changelog
<!-- trace:v1 id=doc.changelog-2 work=WORK-CO-Q8Z1HJJJ -->

<!-- trace:v1 id=doc.changelog work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-XM327PK3 -->

All notable changes to this project. Format follows Keep a Changelog;
versions are `Unreleased` until the first tagged release.

## [Unreleased]
<!-- trace:v1 id=doc.changelog-unreleased work=WORK-CO-Q8Z1HJJJ -->

### Added

- Initial extraction of `carter-omp` from `can1357/oh-my-pi` `python/robomp`
  (see `docs/upstream.md`).
- Explicit-trigger authorization model: strict label/mention triggers keyed on
  immutable GitHub user/repo IDs, `TriggerContext`, capability-bound host tools.
- GitHub App authentication with per-run capability tokens at the proxy.
- Standalone Yarn dashboard (no monorepo `catalog:` deps).
- Pinned prebuilt OMP binary in Docker (no monorepo mount, no `curl | sh`).
- CI: Python (ruff + pytest), web (lint/typecheck/test/build), Docker build.
- Docs: `architecture.md`, `security.md`, `setup.md`, `triggers.md`,
  `github-app.md`, `upstream.md`, `upstream-parity.md`, `agent-notes.md`.
- Baseline: `CONTRIBUTING.md`, `SECURITY.md`, Dependabot, CodeQL, secret scan.
- Cross-provider model fallback: `CARTER_OMP_FALLBACK_MODEL` is a
  comma-separated chain rendered into a per-run `omp --config` overlay
  (`retry.fallbackChains.default`), so a dead or quota-exhausted primary
  provider degrades to the chain (e.g. OpenRouter) instead of failing the run.
- Pickup acknowledgement: the bot reacts 👀 to the triggering comment, or to
  the issue itself for label triggers, through the per-run capability token.

### Fixed

- `github-proxy` enforced a parallel allowlist that ignored
  `CARTER_OMP_REPO_OWNERS`, so owner-scoped repos (e.g. `mac_messages_mcp`)
  were rejected with `repo not in proxy allowlist`. Both processes now share
  one scope predicate.
- The pickup reaction was dead on arrival: it called the reaction endpoint on
  the unscoped client (HMAC only, no run token) and the resulting 401 was
  swallowed. It now runs after run-token attachment, at the single choke point
  covering issue comments, PR conversations, and label triggers.
