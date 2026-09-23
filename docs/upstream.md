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

None yet. Record them here with commit hash, date, and what changed.
