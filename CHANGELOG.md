# trace:exempt reason=prose-docs-no-behavior

# Changelog
<!-- trace:v1 id=doc.changelog-2 work=WORK-CO-Q8Z1HJJJ -->

<!-- trace:v1 id=doc.changelog work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-XM327PK3 -->

All notable changes to this project. Format follows Keep a Changelog;
versions are `Unreleased` until the first tagged release.

## [Unreleased]
<!-- trace:v1 id=doc.changelog-unreleased work=WORK-CO-Q8Z1HJJJ -->

### Added

- `carter-omp add-org <owner>`: onboards another account/org in one step —
  prints the App's install link, resolves the new installation id (App JWT,
  `/orgs/<org>/installation` then `/users/<login>/installation`), and appends it
  to `CARTER_OMP_GITHUB_INSTALLATION_ID` + `CARTER_OMP_REPO_OWNERS` in `.env`.
  Idempotent, comment-preserving, `--dry-run` supported.

- Multi-organization support: `CARTER_OMP_GITHUB_INSTALLATION_ID` takes a
  comma-separated list of installation ids and `CARTER_OMP_REPO_OWNERS` already
  took a list of owners, so one deployment serves several accounts/orgs. The
  proxy resolves each repo's installation from the repo
  (`AppTokenProvider.installation_for_repo`) instead of assuming a single
  configured one, and the issue index + dashboard picker now follow owner scope
  instead of the exact allowlist.

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
- Triggers carry the whole comment thread inline by default: label/assign runs
  (`triage_issue`) and mention runs (`handle_comment`) now embed the issue body
  and every comment chronologically instead of relying on the agent to call
  `fetch_issue_thread`. Mention runs drop the comment that triggered them (it is
  quoted separately); PR runs already inlined their thread.
- Assignment trigger: an authorized sender assigning **the bot itself**
  (`issues.assigned` / `pull_request.assigned`) queues the same work a label
  would. Gated like the label — authorized `sender.id`, installation, repo
  scope — and off-switchable with `CARTER_OMP_ASSIGN_TRIGGERS`. Assigning a
  human never triggers, and neither does `unassigned`.

### Fixed

- Shared git pool kept its group across slots again: the containers were
  missing `CAP_FSETID`, so `chmod 2770` silently dropped the setgid bit (exit 0,
  no bit) and every slot-created file under `/data/workspaces/_pool` inherited
  that slot's own group. One slot's `npm ci`/`bun install` running husky's
  `prepare` (`git config core.hooksPath`) rewrote the pool's `.git/config` as
  `omp-8:omp-8` and every other slot lost the repo mid-run
  (`fatal: unable to access '.git/config': Permission denied`). Added `FSETID`
  to both services and set `HUSKY=0`/`HUSKY_SKIP_INSTALL=1` for the agent, so
  lifecycle scripts cannot touch shared metadata at all.

- Dependency bootstrap now installs for **any** lockfile bun can read
  (`package-lock.json`/`yarn.lock`/`pnpm-lock.yaml`, not just `bun.lock`), using
  bun as the only installer the image ships. An npm repo used to get no install
  at all, so its own `check` script died at the first binary
  (`prettier: command not found`, exit 127) and every `gh_push_branch` was
  refused with `bun check failed before push` — the run then aborted with the
  commit stranded locally. The `bun.lock` bun writes while importing a foreign
  lockfile is removed again, so the pre-publish dirty check stays clean.
- An agent-side `abort_task` now records the delivery as **failed** with the
  agent's own reason instead of `done` (only the log line showed it). A run that
  published nothing no longer looks green in `status`/dashboards.

- Per-run proxy tokens now live as long as the run's own budget
  (`worker._run_token_ttl`: max task/release timeout + hard-stop grace + 5 min)
  instead of a fixed 600 s. Runs last 10–40 minutes, so the old TTL expired
  mid-run and every later push/comment/PR call 401'd
  (`{"detail":"missing or invalid run token"}`) — the agent's own transcript
  recorded commits finished locally with nothing published.
- `@carter-omp status?` (and `stop!`, …) now reaches the deterministic control
  path: trailing punctuation on a bare command line no longer turns a canned DB
  answer into a full model run.

- `github-proxy` enforced a parallel allowlist that ignored
  `CARTER_OMP_REPO_OWNERS`, so owner-scoped repos (e.g. `mac_messages_mcp`)
  were rejected with `repo not in proxy allowlist`. Both processes now share
  one scope predicate.
- The pickup reaction was dead on arrival: it called the reaction endpoint on
  the unscoped client (HMAC only, no run token) and the resulting 401 was
  swallowed. It now runs after run-token attachment, at the single choke point
  covering issue comments, PR conversations, and label triggers.
