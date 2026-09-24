# Setup — from zero to a working bot

<!-- trace:v1 id=doc.setup work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-XM327PK3 -->

Takes about 30 minutes, most of it the GitHub App form. End state: an issue
labeled `carter-omp` gets triaged, fixed, and PR'd with zero manual code.

## 0. Prerequisites

<!-- trace:v1 id=doc.setup-prerequisites work=WORK-CO-Q8Z1HJJJ -->

| Tool | Version | Check |
|---|---|---|
| Python | 3.12 | `python3 --version` |
| uv | any recent | `uv --version` |
| Node | 22 | `node --version` |
| Corepack + Yarn | Yarn 4.9.2 (pinned in `web/package.json`) | `yarn --version` |
| Docker | any recent | `docker --version` |
| gh CLI | any recent, authed as you | `gh auth status` |
| OMP binary | 18.2.11 (pinned; Docker installs it, local dev needs it on PATH) | `omp --version` |
| A public HTTPS host | for the webhook (Tailscale/VPS/tunnel all fine) | — |

## 1. Clone and install

<!-- trace:v1 id=doc.setup-install work=WORK-CO-Q8Z1HJJJ -->

```bash
git clone https://github.com/carterlasalle/carter-omp
cd carter-omp

uv sync --all-extras
corepack enable
corepack prepare yarn@4.9.2 --activate   # repo pins 4.9.2; other versions refuse --immutable
yarn --cwd=web install --immutable

cp .env.example .env
```

Verify the toolchain before touching GitHub:

```bash
uv run pytest -q -p no:cacheprovider --ignore=tests/test_worker_smoke.py
uv run ruff check src tests
uv run carter-omp --help
```

## 2. Create the GitHub App

<!-- trace:v1 id=doc.setup-app work=WORK-CO-Q8Z1HJJJ -->

Follow [`docs/github-app.md`](github-app.md) field by field — it mirrors the
**Register new GitHub App** form exactly (name, webhook URL + secret,
permissions, events, install target). Five minutes of transcription, no
decisions. You will come out with:

- App ID
- Webhook secret (you generated it: `openssl rand -hex 32`)
- A private key `.pem` saved with mode `0400`

## 3. Install the App and collect IDs

<!-- trace:v1 id=doc.setup-install-ids work=WORK-CO-Q8Z1HJJJ -->

1. Install the App → **Only select repositories** → pick a sandbox repo first.
2. Resolve the immutable IDs (names are readability only — IDs authorize):
   ```bash
   gh api users/carterlasalle --jq '{id, login, type}'     # your user ID
   gh api repos/carterlasalle/<repo> --jq '{id, full_name}' # per repo
   # App + installation IDs are on the App settings pages
   ```

## 4. Fill `.env`

<!-- trace:v1 id=doc.setup-env work=WORK-CO-Q8Z1HJJJ -->

```dotenv
GITHUB_WEBHOOK_SECRET=<from step 2>
CARTER_OMP_BOT_LOGIN=<bot login, e.g. carter-omp[bot]>
CARTER_OMP_GIT_AUTHOR_NAME=Carter OMP
CARTER_OMP_GIT_AUTHOR_EMAIL=<noreply address>

CARTER_OMP_AUTHORIZED_USER_IDS=<your user ID>
CARTER_OMP_AUTHORIZED_LOGINS=carterlasalle
# Either list repos by ID, or trust your whole slug (past/present/future):
CARTER_OMP_REPO_OWNERS=carterlasalle
# CARTER_OMP_REPO_IDS=123,456
# CARTER_OMP_REPOS=carterlasalle/repo-a

CARTER_OMP_GITHUB_APP_ID=<app id>
CARTER_OMP_GITHUB_INSTALLATION_ID=<installation id>
CARTER_OMP_GITHUB_PRIVATE_KEY_FILE=/run/secrets/github-app.pem
CARTER_OMP_GH_PROXY_HMAC_KEY=<openssl rand -hex 32>
```

Mount the private key where the proxy expects it (see `compose.yaml`
secrets) — never paste it into `.env`.

## 5. Pick models
<!-- trace:v1 id=doc.setup-models work=WORK-CO-Q8Z1HJJJ -->

```bash
carter-omp init-models
```

Interactive picker: provider (openrouter, opencode-go, …), primary model,
ordered fallbacks. It lists `omp models ls <provider>`, verifies each
selector, then writes `~/.omp/agent/models.container.yml` as an OMP
**provider override** (mounted into the container as `models.yml`): the
container routes through a host-side auth gateway (`transport: pi-native`)
so raw provider credentials never enter the agent container. It prints the
`CARTER_OMP_MODEL` pool to paste into `.env` — model selection lives there,
not in the OMP file. API keys stay wherever OMP already keeps them.

## 6. VPS: from bare Ubuntu to TLS ingress

<!-- trace:v1 id=doc.setup-ingress work=WORK-CO-Q8Z1HJJJ -->

Do this on the VPS over SSH. Assumes Ubuntu 22.04/24.04, a domain you control
(example `omp.example.com` below), and ports 80/443 reachable.

### 6a. Base packages

```bash
sudo apt update && sudo apt install -y git curl openssl ufw
# Docker: https://docs.docker.com/engine/install/ubuntu/
# uv: curl -LsSf https://astral.sh/uv/install.sh | sh
# gh: https://github.com/cli/cli/blob/trunk/docs/install_linux.md
# Node 22 via your preferred method (for corepack/yarn on the host)
```

### 6b. Firewall

```bash
sudo ufw allow OpenSSH
sudo ufw allow 80,443/tcp
sudo ufw enable
```

Leave port 6543 **closed** — `compose.yaml` publishes the orchestrator on
`127.0.0.1:6543` only, so nothing is directly reachable even from the host
network. Caddy (same host) is the sole ingress.

### 6c. DNS

At your registrar, add an `A` record: `omp.example.com` → the VPS public IP.
Verify: `dig +short omp.example.com` returns the IP before continuing.

### 6d. Clone and install (on the VPS)

```bash
git clone https://github.com/carterlasalle/carter-omp
cd carter-omp

uv sync --all-extras
corepack enable
corepack prepare yarn@4.9.2 --activate   # repo pins 4.9.2; other versions refuse --immutable
yarn --cwd=web install --immutable

cp .env.example .env
```

Work through steps 2–5 of this guide (App form, IDs, `.env`, `init-models`)
on the VPS — secrets are generated there with `openssl rand -hex 32` and
never leave the machine except into the GitHub App form.

### 6e. Caddy (TLS + webhook-only ingress)

```bash
# Install: https://caddyserver.com/docs/install (apt repo + apt install caddy)
sudo cp Caddyfile /etc/caddy/Caddyfile
sudo sed -i 's/omp\.example\.com/omp.YOURDOMAIN.com/' /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

The shipped `Caddyfile` gets a Let's Encrypt cert automatically, forwards
ONLY `/webhook/*` to `127.0.0.1:6543`, and 404s everything else (dashboard,
`/healthz`, `/events`, `/replay`). Verify:

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://omp.YOURDOMAIN.com/healthz   # expect 404
```

Use `https://omp.YOURDOMAIN.com/webhook/github` as the App's Webhook URL.
Without Caddy (or equivalent), GitHub cannot reach the webhook — it requires
HTTPS and the container does not terminate TLS itself.

## 7. Start and validate

<!-- trace:v1 id=doc.setup-start work=WORK-CO-Q8Z1HJJJ -->

```bash
docker compose up -d --build
docker compose exec carter-omp carter-omp doctor
```

`doctor` verifies the DB, OMP binary, trigger mode, every identity mapping,
the proxy channel, the model catalog + selectors, and the dashboard bundle —
and refuses (non-zero exit) on any mismatch. Do not proceed with failures.

## 8. First working run

<!-- trace:v1 id=doc.setup-first-run work=WORK-CO-Q8Z1HJJJ -->

1. Add the `carter-omp` label to the sandbox repo (Issues → Labels).
2. Open an issue: `doctor says hi` with body `Reply to this issue.`
   Nothing should happen — no comment, no run. (That silence *is* the
   security model working: no trigger, no compute.)
3. Add the `carter-omp` label to the issue.
4. Watch: `docker compose logs -f carter-omp`. Expect triage → comment →
   `carter-omp:running` label → `carter-omp:done` + PR on a `carter-omp/*`
   branch.
5. Follow up in the issue: `@carter-omp adjust the wording`. Expect the same
   session to resume (`--continue`) on the same branch.
6. Negative test: from another account (or a second login), comment
   `@carter-omp delete everything`. Expect nothing — `state=skipped`,
   `reason=actor_not_authorized`, auditable in the dashboard.

## 9. Go live on real repos

<!-- trace:v1 id=doc.setup-go-live work=WORK-CO-Q8Z1HJJJ -->

1. Install the App on each real repo (**Only select repositories**).
2. With `CARTER_OMP_REPO_OWNERS=carterlasalle` nothing else changes — new
   repos are covered the moment the App is installed. With ID lists, add
   each repo ID and restart.
3. Re-run `doctor` after every identity change.

## Reference

<!-- trace:v1 id=doc.setup-reference work=WORK-CO-Q8Z1HJJJ -->

- Full trigger semantics: [`triggers.md`](triggers.md) (including what every
  `CARTER_OMP_*` trigger variable does).
- Architecture: [`architecture.md`](architecture.md).
- Threat model: [`security.md`](security.md).
- Upstream parity: [`upstream-parity.md`](upstream-parity.md).
- Operator learnings: [`agent-notes.md`](agent-notes.md).

Only `/webhook/github` is public. Keep `/`, `/healthz`, `/readyz`,
`/events`, `/issues`, `/releases`, `/runs`, `/replay`, and the dashboard
API on localhost/VPN.
