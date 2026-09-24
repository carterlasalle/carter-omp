# GitHub App ("Carter OMP")

<!-- trace:v1 id=doc.github-app work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-T692W95P -->

Production authentication is a private GitHub App. Never bake security logic
around its display name or slug — identity is the immutable App/user/
installation/repo IDs, verified at startup and on every webhook.

Fill the **Register new GitHub App** form (Settings → Developer settings →
GitHub Apps → New GitHub App) field by field as below. Nothing on this form
can be auto-provisioned — it is one manual pass, about five minutes.

## 1. Basics

<!-- trace:v1 id=doc.github-app-basics work=WORK-CO-Q8Z1HJJJ -->

- **GitHub App name:** `Carter OMP`. If taken, any nearby variant works —
  the slug is cosmetic; authorization keys off immutable IDs, never the name.
- **Homepage URL:** `https://github.com/carterlasalle/carter-omp`

## 2. Identifying and authorizing users (leave alone)

<!-- trace:v1 id=doc.github-app-user-auth work=WORK-CO-Q8Z1HJJJ -->

carter-omp authenticates as the *installation*, never via a user OAuth flow,
so this whole section stays at its defaults:

- **Redirect URI:** leave empty. Do not add one.
- **Allow wildcard matching:** off.
- **Expire user authorization tokens:** leave at the default.
- **Request user authorization (OAuth) during installation:** leave
  **unchecked**.
- **Enable Device Flow:** off.
- **Setup URL:** leave empty.
- **Redirect on update:** off.

## 3. Webhook

<!-- trace:v1 id=doc.github-app-webhook work=WORK-CO-Q8Z1HJJJ -->

- **Active:** checked.
- **Webhook URL:** `https://<host>/webhook/github` — this exact path.
  Only `/webhook/github` is public; everything else (dashboard, `/healthz`,
  `/readyz`, `/events`, `/replay`) stays on localhost/VPN.
- **Secret:** generate and paste one value, then keep it:
  `openssl rand -hex 32`. The same value becomes `GITHUB_WEBHOOK_SECRET`
  in `.env`.

## 4. Repository permissions (set exactly these)

<!-- trace:v1 id=doc.github-app-permissions work=WORK-CO-Q8Z1HJJJ -->

| Permission | Setting |
|---|---|
| Contents | Read and write |
| Issues | Read and write |
| Pull requests | Read and write |
| Metadata | Read-only (mandatory default) |
| Actions | Read-only — **only** when release diagnosis is enabled; otherwise No access |
| Everything else (Administration, Secrets, Deployments, Members, Workflows, …) | No access |

Workflows (write) only for installations that need workflow-file edits.
Never grant repository administration, secrets, deployments, or members
unless a concrete carter-omp operation requires one.

**Organization permissions:** none. **Account permissions:** none.
**Enterprise permissions:** none.

## 5. Events and install target

<!-- trace:v1 id=doc.github-app-events work=WORK-CO-Q8Z1HJJJ -->

Under **Subscribe to events**, check exactly:

- Issues
- Issue comment
- Pull request
- Pull request review comment
- Workflow run — **only** when the release sentinel is explicitly enabled

Leave Installation target / Meta / Security advisory as GitHub sets them;
do not add anything else.

Under **Where can this GitHub App be installed?** select
**Only on this account**.

## 6. After clicking Create

<!-- trace:v1 id=doc.github-app-after-create work=WORK-CO-Q8Z1HJJJ -->

1. Note the **App ID** → `CARTER_OMP_GITHUB_APP_ID`.
2. Generate a **private key** (button on the App page) and save the `.pem`
   with mode `0400`, visible only to the github-proxy container:
   `CARTER_OMP_GITHUB_PRIVATE_KEY_FILE=/run/secrets/github-app.pem`.
   Never paste the key into `.env`.
3. **Install** the App → **Only select repositories** → pick the repos.
4. Record the **installation ID**, the bot's user ID/login
   (`gh api users/<bot-login> --jq '{id, login, type}'`), and each
   repository ID into `CARTER_OMP_AUTHORIZED_USER_IDS`,
   `CARTER_OMP_REPO_IDS` (plus readable `*_LOGINS` / `CARTER_OMP_REPOS`).
5. `docker compose up -d --build`, then
   `docker compose exec carter-omp carter-omp doctor` — it verifies every
   ID mapping and refuses to start on mismatch.

## Startup validation

<!-- trace:v1 id=doc.github-app-startup work=WORK-CO-Q8Z1HJJJ -->

The orchestrator refuses to start when: no authorized user/repo IDs are
configured, the App identity cannot be verified, repo ID/name mappings
mismatch, the bot is in the operator allowlist unintentionally, strict mode
has ambient triggers enabled, the private key is insecurely permissioned,
a GitHub credential is visible to the orchestrator in proxy-only mode, the
proxy signing key is absent, or the OMP version is RPC-incompatible.
