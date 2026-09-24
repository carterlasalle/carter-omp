# trace:exempt reason=prose-docs-no-behavior

# Contributing
<!-- trace:v1 id=doc.contributing-2 work=WORK-CO-Q8Z1HJJJ -->

<!-- trace:v1 id=doc.contributing work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-XM327PK3 -->

## Quickstart
<!-- trace:v1 id=doc.contributing-quickstart work=WORK-CO-Q8Z1HJJJ -->

```bash
git clone <carter-omp>
cd carter-omp

uv sync --all-extras
corepack enable
yarn --cwd=web install --immutable

cp .env.example .env
$EDITOR .env
```

See `docs/setup.md` for the GitHub App wiring.

## Checks
<!-- trace:v1 id=doc.contributing-checks work=WORK-CO-Q8Z1HJJJ -->

Run before pushing:

```bash
uv run ruff check src tests
uv run ruff format --check src tests
uv run pytest
```

Dashboard:

```bash
yarn --cwd=web lint
yarn --cwd=web typecheck
yarn --cwd=web test
yarn --cwd=web build
```

CI (`.github/workflows/ci.yml`) runs the same plus `docker build -t carter-omp:ci .`.

## Conventions
<!-- trace:v1 id=doc.contributing-conventions work=WORK-CO-Q8Z1HJJJ -->

- Python 3.12, `uv` for env/deps (`uv sync --all-extras --frozen` in CI).
- `ruff` line-length 120; `src` and `tests` only — `vendor/omp-rpc` and
  `docs/*.md` snippets are out of scope (see `pyproject.toml`).
- Authorization logic lives in deterministic host code (`github_events.py`,
  `capabilities.py`), never in prompts. New triggers need a router case +
  an authorization-matrix test in `tests/test_authorization.py`.
- Every behavioral change carries a `trace:v1` marker or `trace:exempt`;
  `trace verify --changed` must pass. Never mark up `THIRD-PARTY-NOTICES.md`.
- Durable operator learnings go in `docs/agent-notes.md`.
- Commit subjects are imperative, ~50 chars, no period
  (e.g. `Fix useradd group creation`).
