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
