# trace:exempt reason=prose-docs-no-behavior

# Agent notes
<!-- trace:v1 id=doc.agent-notes-2 work=WORK-CO-Q8Z1HJJJ -->

<!-- trace:v1 id=doc.agent-notes work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-XM327PK3 -->

Durable operational knowledge. Read this before touching Docker, CI, or trace config.

## Docker
<!-- trace:v1 id=doc.agent-notes-docker work=WORK-CO-Q8Z1HJJJ -->

- `python:*-slim` omits `curl` (and `unzip`). The pinned OMP/Bun downloads
  fail with exit 127 without them. Runtime `apt-get` block in `Dockerfile`
  installs `ca-certificates curl unzip git tini sqlite3` — all spec-required
  (§4) plus the download toolchain. Keep them together.
- `useradd -N` creates no group, so a later `chown user:user` fails with
  "invalid group". The `Dockerfile` uses `useradd -m -U`; do not "simplify"
  back to `-M -N`.
- If the local daemon socket is dead (`docker info` hangs, `docker images`
  times out), do not keep retrying locally — push and let CI's `docker` job
  prove the build. It builds amd64 in ~1 min.

## CI
<!-- trace:v1 id=doc.agent-notes-ci work=WORK-CO-Q8Z1HJJJ -->

- `ruff check` / `ruff format --check` are scoped to `src tests` (see
  `.github/workflows/ci.yml` and `pyproject.toml` `extend-exclude`). The
  vendored `vendor/omp-rpc` has its own style and `docs/*.md` embeds code
  snippets; running ruff over the whole tree fails on files we must not
  reformat.
- CI gates on **both** `ruff check` and `ruff format --check src tests`. `ruff
  check` passing is not enough: run `uv run ruff format src tests` before
  pushing or the `python` job fails on formatting alone.
- `vendor/` is also excluded from trace policy (`.trace/policy.toml` is
  gitignored, so this exclusion is local-only and must be re-applied on fresh
  checkouts if TL012 fires on vendored code).

## Trace
<!-- trace:v1 id=doc.agent-notes-trace work=WORK-CO-Q8Z1HJJJ -->

- `trace verify --changed` must pass before completing work. `THIRD-PARTY-NOTICES.md`
  is a copied license aggregate — never add `trace:v1` markers to it; it is
  covered by `docs/upstream.md` provenance instead.
- `Dockerfile` carries `# trace:exempt reason=deploy-packaging-no-runtime-behavior`.
  Prefer `reason=` form; bare `# trace:exempt <words>` does not satisfy TL012.

## Console (web/)
<!-- trace:v1 id=doc.agent-notes-console work=WORK-CO-Q8Z1HJJJ -->

- The served bundle is **`src/carter_omp/static/`** — that is what
  `dashboard.static_dir()` mounts and what the Docker `web-builder` stage
  copies into. The Vite plugin (`syncStaticBundle`) fans `web/dist/` there;
  a `src/static/` directory is the pre-extraction path and must not come back
  (a stale copy was committed once and shadowed nothing but confused tools).
- `yarn build` alone is the whole install step locally; the FastAPI process
  caches `index.html` at startup, so restart the server after a rebuild or `/`
  keeps serving the previous asset names.

## Proxy run tokens
<!-- trace:v1 id=doc.agent-notes-proxy-tokens work=WORK-CO-Q8Z1HJJJ -->

- The per-run token is minted before the agent starts, but a run's identity
  changes mid-flight: `classify_issue(branch_slug=…)` renames the branch and
  the run opens its own PR. The proxy therefore accepts any branch in the
  token's own `carter-omp/<hex>/` namespace and both threads the run owns
  (originating issue + `pull_request`), and `ToolBindings.refresh_run_token`
  re-mints from the live branch/PR. Tools that change either must refresh.
- Pinning the full branch name (not the namespace) is what made issue #14
  unable to publish; do not "tighten" `_require_run_branch` back to equality.

## Analyzers
<!-- trace:v1 id=doc.agent-notes-analyzers work=WORK-CO-Q8Z1HJJJ -->

- mypy baseline: 0 errors (lenient: `ignore_missing_imports`). The 2026-09-24
  baseline of 19 errors was cleared with real fixes (nullable narrowing, Row-type
  unions, a `release_retag` result payload that would have reached the agent
  without its `content` block).
  Do not chase zero speculatively — the strict run (43 errors) is mostly
  vendored-RPC generic variance. CI fails only if the count grows.
- bandit skips live in `pyproject.toml` with reasons; zero `nosec` in tree.
  B324 SHA-1 was fixed (SHA-256 in `_short_hex`), not skipped.
- vulture: `min_confidence = 90`, `cls` ignored (pydantic validators).
  60%-confidence hits are cross-module uses; verify with grep before touching.
- Coverage gate is 70% line floor; raise toward 85% after measuring per-file.

## Tests
<!-- trace:v1 id=doc.agent-notes-tests work=WORK-CO-Q8Z1HJJJ -->

- Full suite is ~110 s (`uv run pytest`, 720 passed / 3 skipped as of 2026-09-24).
  For iteration use focused files: `test_authorization.py`,
  `test_adversarial.py`, `test_proxy_server.py`, `test_tasks_directive.py`
  (<10 s combined).
- `tests/test_worker_smoke.py` is excluded from the default local run; CI runs
  the full suite including it.
