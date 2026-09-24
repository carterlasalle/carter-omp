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

- `6647b0bf145cbc409a4d37d7911e52e1d660ad84` (2026-09-24; reviewed
  2026-09-24): `chore: branding` — `oh-my-pi` → `omp` rename in
  `python/robomp/.env.example`, `AGENTS.md`, `README.md`. Only change to the
  extracted paths since extraction `3d3ec7e`; reviewed, not ported —
  intentional divergence (pinned prebuilt OMP binary, no monorepo mount,
  Carter naming). Upstream HEAD for `python/robomp` + `python/omp-rpc`.
- Issue #3's other 19 listed commits all predate extraction `3d3ec7e` and are
  already incorporated (review-anchor validation, 5xx retry, `blocker` todo
  field, reopened handling).
