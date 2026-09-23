# Setup

<!-- trace:v1 id=doc.setup work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-XM327PK3 -->

```bash
git clone <carter-omp>
cd carter-omp

uv sync --all-extras
corepack enable
yarn --cwd=web install --immutable

cp .env.example .env
$EDITOR .env
```

1. Create a private GitHub App ("Carter OMP"); see `docs/github-app.md`.
2. Webhook URL `https://<host>/webhook/github` + secret.
3. Permissions: Metadata R, Contents RW, Issues RW, Pull requests RW
   (+ Actions R only when release diagnosis is enabled).
4. Install on **Only select repositories**.
5. Resolve Carter's immutable user ID:
   `gh api users/carterlasalle --jq '{id, login, type}'`.
6. Fill `CARTER_OMP_AUTHORIZED_USER_IDS`, `CARTER_OMP_REPO_IDS`, names for
   readability (`CARTER_OMP_AUTHORIZED_LOGINS`, `CARTER_OMP_REPOS`).
7. `openssl rand -hex 32` → `CARTER_OMP_GH_PROXY_HMAC_KEY`.
8. Configure `CARTER_OMP_MODEL` and the model gateway.
9. `docker compose up -d --build`
10. `docker compose exec carter-omp carter-omp doctor`
11. Add the `carter-omp` label to selected repos.
12. Test with a sandbox issue: open → nothing happens → add label →
    triage/fix/PR.

Only `/webhook/github` is public. Keep `/`, `/healthz`, `/readyz`,
`/events`, `/issues`, `/releases`, `/runs`, `/replay`, and the dashboard
API on localhost/VPN.
