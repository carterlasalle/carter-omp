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

Reviewed against extraction `3d3ec7e` through upstream `fd4542b`
(2026-10-03; `packages/coding-agent/package.json` → `18.5.0`). Ports target the
paths this repo extracted (`python/robomp/src` → `src/carter_omp`,
`python/robomp/tests` → `tests`, `python/robomp/web` → `web`,
`python/omp-rpc` → `vendor/omp-rpc`) with the package/env/branch renames
(`robomp` → `carter_omp`, `ROBOMP_*` → `CARTER_OMP_*`, `farm/*` →
`carter-omp/*`, `gh-proxy` → `github-proxy`) applied.

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

### Reviewed, not ported

- `6647b0bf145cbc409a4d37d7911e52e1d660ad84` (2026-09-24) `chore: branding` —
  `oh-my-pi` → `omp` rename in `python/robomp` docs and `.env.example`.
  Intentional divergence: Carter naming, pinned prebuilt OMP binary, no
  monorepo mount.
- `ff8a98362e3eecf2b60290eefbef45c8803f1138` (2026-10-01) `docs(robomp):
  document local omp-rpc install` — monorepo `bun run robomp:install` recipe;
  carter already resolves `omp-rpc` locally via `[tool.uv.sources]` in
  `pyproject.toml`. No carter counterpart.
- `1459332dc8f48b65124fcd0b831f28881e02bb1c` (2026-09-24) prompt results /
  session-settled frames / headless host mode, `c867895e83b1672db67244d4455ebbb2cefcaf30`
  (2026-09-29) queued groups + Python binding, `b493cf68658b0fce88e78c745514d10c2775c14b`
  (2026-09-29) queued-message snapshot, `10a0846131fe86d0da3252cbe405a3ec6528027a`
  (2026-09-30) `promote_queued_message`, `0f1e064d01ccef2667440948f3b729e8ec12c9ed`
  (2026-09-30) aborted-refresh billed usage + `set_cache_warming`,
  `cb94bcb5eb198cbdf7ddb905cd181aaad05f9d7b` (2026-10-02) `fork` command,
  `c2ccbee9a27575c683ece74b793d25fab486cd7f` (2026-10-03) goal
  mode/subagents/live voice, `c4d3204151aaafdc4fc4b91045789ff1fed84c9f`
  (2026-10-03) generated wire schema + moved `sdk/python/omp-rpc` — RPC client
  and wire features from OMP 18.3–18.5. They bind to the 18.5.0 wire protocol;
  carter pins the OMP binary at `18.2.11` (`Dockerfile`) and keeps
  `vendor/omp-rpc` in sync with that binary, so incorporating them requires a
  deliberate `OMP_VERSION` bump first.
