# trace:exempt reason=prose-docs-no-behavior

# Changelog
<!-- trace:v1 id=doc.changelog-2 work=WORK-CO-Q8Z1HJJJ -->

<!-- trace:v1 id=doc.changelog work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-XM327PK3 -->

All notable changes to this project. Format follows Keep a Changelog;
versions are `Unreleased` until the first tagged release.

## [Unreleased]
<!-- trace:v1 id=doc.changelog-unreleased work=WORK-CO-Q8Z1HJJJ -->

### Added

- `report_pain_point`: any authorized run can now file the friction it hits in
  *the harness itself* — a host tool rejecting a valid call, an error message
  pointing at the wrong cause, a gate blocking work for the wrong reason, a run
  losing an hour to infrastructure. The report becomes a tracked issue on
  `CARTER_OMP_SELF_REPORT_REPO` (default `carterlasalle/carter-omp`), labelled
  `bot-report` plus the deployment's trigger label, over the new
  `POST /gh/v1/self-report` proxy route. Dedupe is two-layered: the
  orchestrator records `(sha256(title) → issue)` in `self_reports` and appends
  straight to that issue, falling back to an exact-title match on the
  `[bot-report]` marker over open issues. GitHub's issue *list* needs ~5s to
  show a just-created issue (measured: invisible at +0/+1/+3s, visible at +6s),
  so a list-only check filed a duplicate for every back-to-back report; the destination comes from proxy config and the provenance from the run
  token, so a report can never be redirected or misattributed. New capability
  `report_upstream` (granted to issue, review and release runs), new
  `CARTER_OMP_SELF_REPORT_REPO` knob (empty disables the tool), and prompt
  guidance to report instead of vanishing into a silent `abort_task`.

- Console **System** view (4th rail entry, dead-letter badge): spend for
  today/7 days/all-time (runs, fallback share, cost, cache cost, token
  breakdown), the live queue with each event's next attempt, dead letters with
  their burned retry budget, the last 12 runs with model/duration/cost/tokens,
  and per-repo issue-index freshness. Backed by a `system` block in
  `/api/status`; runtime now also reports owners, installation ids, model pool
  and fallback chain.

- Per-run telemetry: the worker writes model, fallback model, duration, cost,
  cache cost and token counts onto the event row on both success and failure
  (`cost_usd`-carrying rows are what the spend aggregates and the footer read).

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

- **Trigger label is consumed through the run-scoped client (#15).** After a
  successful label-triggered run, `_consume_trigger_label` called
  `inputs.github` — the shared HMAC-only client — instead of the run-token
  client on `bindings.github`. Every proxy label endpoint requires a run token,
  so the `remove_issue_label`/`add_issue_labels` calls answered 401 and the
  failure was swallowed at DEBUG: the one-shot trigger label stayed on the
  issue, and re-applying it (the documented re-trigger) fired no `labeled`
  event. The function now takes `bindings` and uses `bindings.github`, and logs
  failures at WARNING.

- Mention turns must end with a reply. `_needs_completion_reminder` only knew
  the triage, review and release task kinds, so a `handle_comment` turn had no
  terminal action at all: the 2026-10-07 run on `personal_website#64` called
  `todo`/`bash` for 84 seconds, stopped without posting anything, and the
  delivery was recorded `done` — the human saw the bot ignore them. Comment
  turns now require `gh_post_comment` (or `abort_task`), with their own
  `comment_completion_reminder` prompt.

- Ported the upstream `python/robomp`/`python/omp-rpc` fixes since extraction
  (see `docs/upstream.md`): `fetch_ref`/`fetch_pr_head` backfill only the
  missing tip-tree blobs instead of re-downloading the ref's history; a
  `submit_pr_review` reached through the `eval` bridge now counts as the
  terminal action (#13583); and `RpcClient.stop()` verifies teardown and
  retains unreaped survivors so a child stuck in uninterruptible sleep stays
  observable and reapable instead of pinning its concurrency slot.
- **Run tokens follow the run (#14).** `_attach_run_token` minted the proxy
  token before the agent started, pinning the pre-classification branch and the
  originating issue number. A triage run that classified with a `branch_slug`
  (renaming `carter-omp/<hex>/issue-N` → `carter-omp/<hex>/fix-…`) could commit
  its work and then never publish it: `gh_push_branch`/`gh_open_pr` failed with
  `403 run token branch mismatch`, and review requests on the PR the run had
  just opened failed with `403 run token thread mismatch`. Three changes:
  the token carries the run's own `pull_request`; the proxy accepts any branch
  in the token's own `carter-omp/<hex>/` namespace plus both threads the run
  owns (originating issue and its PR); and `ToolBindings.refresh_run_token`
  re-mints from the live branch/PR (called by `classify_issue` after a
  sanctioned rename and by `gh_open_pr` once the PR exists). Cross-run pushes
  and other threads are still rejected.

- The dashboard bundle installs into the path the server actually mounts.
  `web/vite.config.ts` fanned the build into `src/static/`, but
  `dashboard.static_dir()` and the Docker `web-builder` stage use
  `src/carter_omp/static/`, so `yarn build` alone never reached the served
  bundle (and a stale copy sat committed under `src/static/`). The plugin now
  writes the package directory, the leftover tracked bundle is gone.

- PR review bodies now carry the same model/duration/cost footer as comments
  and PR bodies (`_footer_suffix` applied at every bot-authored surface:
  `gh_post_comment`, `gh_open_pr`, `submit_pr_review`, the review 422/500
  fallback comment, and `mark_unable_to_reproduce`). Reviews were the one
  surface the footer had missed, so they showed no cost.

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
