# carter-omp

<!-- trace:v1 id=doc.readme work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-XM327PK3 -->

Self-hosted GitHub coding agent. Drives [`omp --mode rpc`](https://github.com/can1357/oh-my-pi)
as a subprocess against a per-issue git worktree, then writes back to GitHub
through a sidecar that holds the credential.

**Explicit invocation only.** Ordinary GitHub activity (issue opened, comments,
PR opened/synchronized, reviews, CI) never starts the agent. Carter starts it
with one label or one mention:

- Add the `carter-omp` label to an issue/PR, or
- Comment `@carter-omp <directive>` (e.g. `@carter-omp fix the race and add a regression test`).

Everything else is untrusted context. See `docs/security.md` and `docs/triggers.md`.

## What an authorized run can do

Triage, reproduce bugs, edit code, run shell commands, LSP, tests, commit,
push a `carter-omp/*` branch, open a PR, respond to comments and reviews,
review incoming PRs (read-only), search issues/commits, request reviewers —
with persistent per-issue sessions, crash recovery, durable SQLite queue,
dashboard, and audited host-tool actions.

## Architecture

Two containers, one trust boundary:

- **carter-omp** — FastAPI + sqlite event queue + `WorkerPool` running `omp` in
  per-issue worktrees under `/data/workspaces/`. Holds the HMAC key, never
  the GitHub credential.
- **github-proxy** — sibling on an `internal: true` network. Holds the
  credential, verifies HMAC-signed requests from the orchestrator, executes
  REST + `git push`. Only egress to `api.github.com`.

Flow: webhook → HMAC verify → identify repo/actor by immutable IDs →
explicit-trigger check → capability derivation → sqlite `events`
(dedup on `X-GitHub-Delivery`) → `WorkerPool` claims per issue →
worktree on `carter-omp/<8hex>/<slug>` → `worker.run_task` spawns
`omp --mode rpc` with only the capability-gated host tools exposed.

## Setup

```bash
git clone <carter-omp>
cd carter-omp

uv sync --all-extras
corepack enable
yarn --cwd=web install --immutable

cp .env.example .env
$EDITOR .env
openssl rand -hex 32   # CARTER_OMP_GH_PROXY_HMAC_KEY
openssl rand -hex 32   # GITHUB_WEBHOOK_SECRET
```

Then:

1. Create a private GitHub App ("Carter OMP"), webhook URL + secret,
   permissions Contents/Issues/Pull requests RW + Metadata R
   (+ Actions R only for release diagnosis).
2. Install it on **Only select repositories**.
3. Resolve Carter's immutable user ID and fill `CARTER_OMP_AUTHORIZED_USER_IDS`.
4. Fill repo IDs (`CARTER_OMP_REPO_IDS`).
5. Configure the model gateway.
6. Start: `docker compose up -d --build`
7. Validate: `docker compose exec carter-omp carter-omp doctor`
8. Add the `carter-omp` label to selected repos.
9. Test with a sandbox issue.

See `docs/setup.md`, `docs/github-app.md`, `docs/triggers.md`,
`docs/architecture.md`, `docs/security.md`, `docs/upstream.md`.

## Development

```bash
uv sync --all-extras
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run carter-omp --help
```

Dashboard:

```bash
yarn --cwd=web install --immutable
yarn --cwd=web lint
yarn --cwd=web typecheck
yarn --cwd=web test
yarn --cwd=web build
```

## License

MIT — see `LICENSE`. Derived from `can1357/oh-my-pi` `python/robomp`;
see `docs/upstream.md` and `THIRD-PARTY-NOTICES.md`.
