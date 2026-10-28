# Upstream provenance

<!-- trace:v1 id=doc.upstream work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-XM327PK3 -->

carter-omp is extracted from the `python/robomp` implementation in the
`can1357/oh-my-pi` monorepo.

- Upstream repository: `https://github.com/can1357/oh-my-pi`
- Upstream path: `python/robomp`
- Upstream commit used for extraction: `3d3ec7e907acfd74d650f6fd632e98d7a820ff66`
- Date extracted: 2026-09-23
- `omp-rpc` vendored from upstream `python/omp-rpc` at the same commit
  (see `vendor/omp-rpc`; upstream has no published `omp-rpc` PyPI release).

## Files substantially copied

- `src/carter_omp/*` from upstream `src/*` (renamed package `robomp` →
  `carter_omp`, env `ROBOMP_*` → `CARTER_OMP_*`, branches `farm/*` →
  `carter-omp/*`, `gh-proxy` → `github-proxy`)
- `tests/*` from upstream `tests/*` (updated to the explicit-trigger contract)
- `web/*` from upstream `web/*` (Bun/`catalog:` → standalone Yarn + pinned versions)
- `scripts/ping.sh`, `entrypoint.sh`, `.gitignore` adapted
- `LICENSE` preserved (MIT, Stencil Labs, Inc.)
- `THIRD-PARTY-NOTICES.md` copied from the upstream aggregate
  (`THIRD-PARTY-NOTICES.txt` at the monorepo root)

## Later upstream commits cherry-picked or manually incorporated

Reviewed against extraction `3d3ec7e` through upstream `6e6b130d`
(2026-10-07; `fix(robomp): kept failed pr reviews out of the review-once
ledger` (#14859)), covering the watch notice for `fd4542b` (2026-10-03;
merge of #13586 on top of the evaluated 18.5.0 wire-schema move `c4d3204`,
this issue). Ports target the paths this repo extracted
(`python/robomp/src` → `src/carter_omp`, `python/robomp/tests` →
`tests`, `python/robomp/web` → `web`, `python/omp-rpc` → `vendor/omp-rpc`)
with the package/env/branch renames (`robomp` → `carter_omp`, `ROBOMP_*` →
`CARTER_OMP_*`, `farm/*` → `carter-omp/*`, `gh-proxy` → `github-proxy`)
applied. The notice's ten listed commits: nine pre-date extraction
`3d3ec7e` (verified ancestors, already in the extracted tree — release
tracking/recovery `4aa8ce59`, PR test gate `3f6173b8`, compose filename
`c5642a5d`, anchor validation `2d883769`, usage attribution `20912497`,
license `b3303620`, work-items fixture URL `5bd31c02`, paused-pool
`db98a1aa` / dispatcher-isolation `c5cb91b8` server tests); the tenth is
`fd4542b` itself, recorded below. Watch notice for `c4d3204`
(carter-omp#79): nine of its ten listed commits are ancestors of the
extraction point (`git merge-base --is-ancestor` against `3d3ec7e`, all
yes), so they were already in the extracted tree — no port involved. The
tenth is `c4d3204` itself, recorded below.

### Ported

- `baf63aa197e1d59206ebca692915f1546c519b5f` (2026-09-26) `feat(python/robomp):
  enabled backfill of missing blobs for fetch operations` — `fetch_ref` /
  `fetch_pr_head` now fetch the ref and then backfill only the missing tip-tree
  blobs (`_backfill_tip_blobs`: `rev-list --missing=print` piped to
  `git fetch --stdin`) instead of `--refetch --no-filter` re-downloading the
  ref's whole history on every call. Ported to `src/carter_omp/git_ops.py` and
  `tests/test_sandbox.py`.
- `5c951924d44395d2991c550ce769da3d4e21ff8e` (2026-09-22) `fix(omp-rpc):
  verify teardown and retain a survivor handle for reaping` — `stop()` reports
  whether teardown is confirmed and retains a child that outlives the
  SIGTERM→SIGKILL escalation so a later `stop()`/`reap()` can re-attempt and
  the survivor stays observable (`survivor_pid`). Ported to `vendor/omp-rpc`.
- `8395f7a8834e52364320756088532ce30d22ccac` (2026-10-02) `fix(omp-rpc):
  settled the group after SIGKILL and kept every unreaped survivor` — post-KILL
  settle poll before declaring a survivor, dead-group probe before re-signalling,
  and a survivor *list* (`survivor_pids`) so a restart never drops an earlier
  live child. Ported to `vendor/omp-rpc`.
- `08a2e8073bea4c79fc1912c00efd662d9ea4044a` (2026-09-28) `fix(robomp): counted
  eval-bridged host tools as terminal actions` (#13583) —
  `RpcClient.on_host_tool_completed` / `HostToolCompletedEvent` fire for every
  host-tool dispatch path (including the eval bridge, which `tool_execution_end`
  surfaces only as the enclosing `eval`), and the worker now feeds that signal
  into its terminal-action gate. Ported to `vendor/omp-rpc` and
  `src/carter_omp/worker.py` (`_on_host_tool_completed`).
- `41484928c495d1cf7770a701df0aa05dc2b8f5ed` (2026-09-28) `test(robomp):
  injected prompt hooks instead of patching fake client methods` — test-only;
  `_FakeRpcClient` keeps registered listeners per instance
  (`emit_tool_end` / `emit_host_tool_completed`) and `_install_prompt_hook`
  injects the prompt hook through the `worker.RpcClient` seam. Ported to
  `tests/test_worker.py`.
- `8cf9d2ecdda5a2bfdd37fb615fdc1aec1bc567ca` (2026-09-17) `test(robomp): isolate
  host tools and git config` — test-only; unique clone dir per call, `slots`/
  `GIT_CONFIG_GLOBAL`/`GIT_CONFIG_SYSTEM`/`GIT_CONFIG_NOSYSTEM` isolation, and
  `sleep` resolved via `shutil.which` instead of `/bin/sleep`. Ported to
  `tests/test_sandbox.py` and `tests/test_natives_cache.py`.
- `aa5aab0ffb42f01f3a1973957ebb563ed3cb1ead` (2026-09-27) `test: remove useless
  tests` — only the `python/robomp/web` part applies: two duplicate live-event
  ordering tests dropped from `work-items.test.ts`. Ported; the `packages/**`
  TypeScript removals have no carter counterpart.
- `6e6b130d705bfb6c26c7350c82d17ac90ee30865` (2026-10-07) `fix(robomp): kept
  failed pr reviews out of the review-once ledger` (#14859) — dropped anchors
  no longer emit an early error-free `submit_pr_review` audit row; they ride on
  the terminal success/fallback row (`dropped` is now the anchor list), so a
  rejected POST no longer satisfies `has_successful_tool_call`. Ported to
  `src/carter_omp/host_tools.py` and `tests/test_host_tools.py`.
- `c4d3204151aaafdc4fc4b91045789ff1fed84c9f` (2026-10-03) `feat: added
  machine-readable RPC wire schema and multi-language client SDKs` — the
  commit's own `python/robomp`-scoped delta is two hunks: the follow-up
  `set_todos` merge simplification (pass live phases straight through,
  `set_todos([*get_todos(), *phases])`, instead of rebuilding phase dicts by
  hand) and a one-line `AGENTS.md` test-recipe path update
  (`python/omp-rpc/tests` → `sdk/python/omp-rpc/tests`). The worker hunk is
  ported to `src/carter_omp/worker.py` (safe: the vendored 18.2.11 client
  round-trips live `TodoPhase` objects through `_normalize_todo_phases` with
  ids/status intact); the `AGENTS.md` hunk has no carter counterpart (no
  monorepo root, no `bun run test:py`). The rest of the commit moves
  `python/omp-rpc` → `sdk/python/omp-rpc` and adds wire features
  (`prompt_result` / `session_settled`, queued-message snapshot +
  `promote_queued_message`, `open_session` / `set_event_filter`,
  `set_cache_warming`, `fork`, goal mode/subagents/live voice, generated
  `_wire.py`), which stay deferred behind the deliberate `OMP_VERSION` pin
  at `18.2.11` (`Dockerfile`); `vendor/omp-rpc` carries none of those symbols
  via `08a2e807` / `41484928`.)
- `aa0b278776080ccde989622df403440923f725d4` (2026-10-26) `fix(robomp): scoped
  pre-publish gates to gh_open_pr base override` (#16967) — new `_publish_base`
  helper (`gh_open_pr`'s `base` override, else the repo default) scopes the
  formatter-amend probe, the message-repair/identity hints, and the PR create
  call, so a non-default base no longer drags its whole history into the
  bot-only checks. Ported to `src/carter_omp/host_tools.py` plus the gitflow
  regression test in `tests/test_host_tools.py`. Carter adaptation: the
  identity scan and message rewrite themselves stay on `_unpushed_rev_range`
  (already excludes adopted review bases and everything remote-reachable — a
  superset of the upstream fix); only the formatter probe, both reword hints,
  and the `gh_open_pr` base take the requested base. Fixes carter-omp#90.

### Reviewed, not ported

- `6647b0bf145cbc409a4d37d7911e52e1d660ad84` (2026-09-24) `chore: branding` —
  `oh-my-pi` → `omp` rename in `python/robomp` docs and `.env.example`.
  Intentional divergence: Carter naming, pinned prebuilt OMP binary, no
  monorepo mount.
- `ff8a98362e3eecf2b60290eefbef45c8803f1138` (2026-10-01) `docs(robomp):
  document local omp-rpc install` — monorepo `bun run robomp:install` recipe;
  carter already resolves `omp-rpc` locally via `[tool.uv.sources]` in
  `pyproject.toml`. No carter counterpart.
- `f7055447f2021d20e718f76510ba1bda0da09287` (2026-10-01, merge of #12358 on
  top of `e0e356fe`) `test: make fixtures portable across non-FHS hosts` —
  test-only: `GIT_CONFIG_GLOBAL`/`GIT_CONFIG_SYSTEM`/`GIT_CONFIG_NOSYSTEM`
  isolation and `shutil.which("sleep")` in `tests/test_sandbox.py` /
  `tests/test_natives_cache.py`; the companion `e0e356fe` local-rpc-install
  doc is already satisfied by carter's `[tool.uv.sources]` resolution. Both
  hunks already present in the carter tree (via the earlier `8cf9d2ec`
  port); reviewed, no new diff.
- `e0e356fe67088b780f20b980deea2f8a7cf0fc26` (2026-10-01, merge of #12463)
  `fix(robomp): install local rpc dependency` — the merge's only
  `python/robomp`-scoped hunk is the `AGENTS.md` local-install recipe
  (carried from `ff8a98362e`); carter already resolves `omp-rpc` locally via
  `[tool.uv.sources]` in `pyproject.toml`. Reviewed, no new diff.
- `cbaab046652af69d356da1a3b93f3cb1ecb5a995` (2026-10-01, merge of main into
  the #12358 branch on top of `06ca9883f7`, itself 2026-10-01 main) — the
  tree diff against extraction `3d3ec7e` for the extracted paths is identical
  in substance to the already-reviewed `fd4542b` (2026-10-03): the delta comes
  from the main-side parent, not from new `python/robomp` work. It resolves
  to (a) the already-ported `baf63aa1` backfill (`_backfill_tip_blobs`,
  `input=` on `_run_git`, tip-only `--missing=print -n1` fixture assertions),
  now visible in `src/carter_omp/git_ops.py` and `tests/test_sandbox.py` with
  the `oh-my-pi#1818` comment intact, plus one retained `live-older` test
  matching carter's `web/src/work-items.test.ts`; (b) the already-reviewed
  `6647b0bf` branding divergence (`oh-my-pi` → `omp` in `.env.example` /
  `AGENTS.md` / `README.md`), intentionally not adopted; (c) the already-
  deferred 18.3–18.5 RPC/wire features (`prompt_result` /
  `session_settled`, queued-message snapshot + `promote_queued_message`,
  `open_session` / `set_event_filter`, `set_cache_warming`, `fork`, goal
  mode/subagents/live voice, `sdk/python/omp-rpc` move), still blocked on a
  deliberate `OMP_VERSION` bump past the pinned `18.2.11` (`Dockerfile`) and
  absent from `vendor/omp-rpc` by design; and (d) the already-reviewed
  `aa5aab0f` duplicate-test removals, with only the older duplicates gone and
  the newest duplicate sibling kept in both trees. The second parent also
  reverts `worker.py` / `tests/test_worker.py` to the pre-`fd4542b` state
  (manual todo-merge loop, no `on_host_tool_completed` / eval-bridge
  paragraph) — carter already ports the newer `fd4542b` side (`_on_host_tool_completed`
  at `src/carter_omp/worker.py:1145`, registered at `:1355`), so there is no
  regression to adopt. Nothing to port; no new diff.
- `1459332dc8f48b65124fcd0b831f28881e02bb1c` (2026-09-24) prompt results /
  session-settled frames / headless host mode, `c867895e83b1672db67244d4455ebbb2cefcaf30`
  (2026-09-12) queued groups + Python binding, `b493cf68658b0fce88e78c745514d10c2775c14b`
  (2026-09-24) queued-message snapshot, `10a0846131fe86d0da3252cbe405a3ec6528027a`
  (2026-09-23) `promote_queued_message`, `0f1e064d01ccef2667440948f3b729e8ec12c9ed`
  (2026-09-30) aborted-refresh billed usage + `set_cache_warming`,
  `cb94bcb5eb198cbdf7ddb905cd181aaad05f9d7b` (2026-10-02) `fork` command,
  `c2ccbee9a27575c683ece74b793d25fab486cd7f` (2026-10-03) goal
  mode/subagents/live voice, `c4d3204151aaafdc4fc4b91045789ff1fed84c9f`
  (2026-10-03) generated wire schema + moved `sdk/python/omp-rpc` — RPC client
  and wire features from OMP 18.3–18.5. They bind to the 18.5.0 wire protocol;
  carter pins the OMP binary at `18.2.11` (`Dockerfile`) and keeps
  `vendor/omp-rpc` in sync with that binary, so incorporating them requires a
  deliberate `OMP_VERSION` bump first.
- `fd4542b21d503007d9c589cd3b6beac9a2f254d2` (2026-10-03, merge of #13586;
  this watch notice) — second parent `41484928` (prompt-hook test seam) and
  the merge's own `worker.py` / `test_worker.py` hunks (`HostToolCompletedEvent`
  / `on_host_tool_completed` eval-bridge fix `08a2e807`, plus the `c4d3204`
  `set_todos` simplification on the main side) were already ported via
  `08a2e807` / `41484928` / `c4d3204` (see Ported above); `e305c2748` and
  `c2ccbee9` in range touch only `python/omp-rpc` pre-move sources whose
  ported equivalents already live in `vendor/omp-rpc` and the carter worker.
  `python/robomp`-scoped tree diff against extraction `3d3ec7e` is the
  backfill, branding, eval-bridge/prompt-hook, duplicate-test, and
  portable-fixture hunks already ported or deliberately diverged — no new
  `python/robomp` work. Newer `sdk/python/omp-rpc/_wire.py` symbols past
  `6e6b130d` (`live_steered` `19703f92`, `provider_replay_through_entry_id`
  `2cc3af08`, `resumed_tool_call_ids` `818c3974`, `narration` `50ec66f7`)
  stay deferred behind the `OMP_VERSION` 18.2.11 pin. Nothing to port; no
  new diff. Fixes carter-omp#87.
