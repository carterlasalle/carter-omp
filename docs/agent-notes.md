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

## Analyzers
<!-- trace:v1 id=doc.agent-notes-analyzers work=WORK-CO-Q8Z1HJJJ -->

- mypy baseline: 19 errors / 6 files (lenient: `ignore_missing_imports`).
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
