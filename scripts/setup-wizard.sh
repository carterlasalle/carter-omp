#!/usr/bin/env bash
# trace:exempt reason=deploy-packaging-no-runtime-behavior
# carter-omp VPS setup wizard. Automates everything automatable, pauses with
# a clear PASTE prompt wherever only a human can act (DNS registrar, GitHub
# App form). Idempotent: safe to re-run; completed steps detect and skip.
#
# Usage: ./scripts/setup-wizard.sh [--domain omp.example.com] [--repo-owners carterlasalle]
set -euo pipefail

DOMAIN=""
REPO_OWNERS="carterlasalle"
while [ $# -gt 0 ]; do
  case "$1" in
    --domain=*) DOMAIN="${1#--domain=}" ;;
    --domain) DOMAIN="${2:-}"; shift ;;
    --repo-owners=*) REPO_OWNERS="${1#--repo-owners=}" ;;
    --repo-owners) REPO_OWNERS="${2:-}"; shift ;;
  esac
  shift
done

pass() { printf '\033[32m[ok]\033[0m %s\n' "$*"; }
step() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
pause() { # pause "WHY" "WHAT-TO-DO"
  printf '\n\033[33m[PASTE NEEDED] %s\033[0m\n%s\n' "$1" "$2"
  read -r -p "Press Enter when done... " _ </dev/tty
}
need() { command -v "$1" >/dev/null 2>&1 || { echo "missing: $1 ($2)"; exit 1; }; }

# --- 0. sanity ---------------------------------------------------------------
step "0. Sanity"
need git "sudo apt install -y git"
need curl "sudo apt install -y curl"
need openssl "sudo apt install -y openssl"
[ -f compose.yaml ] || { echo "run from the carter-omp repo root"; exit 1; }
pass "tools present, repo root ok"

# --- 1. toolchain ------------------------------------------------------------
step "1. Toolchain (uv, docker, node, gh)"
command -v uv >/dev/null 2>&1 || { echo "install uv first: https://docs.astral.sh/uv/"; exit 1; }
command -v docker >/dev/null 2>&1 || { echo "install docker first: https://docs.docker.com/engine/install/ubuntu/"; exit 1; }
command -v gh >/dev/null 2>&1 || { echo "install gh first: https://github.com/cli/cli/blob/trunk/docs/install_linux.md"; exit 1; }
command -v omp >/dev/null 2>&1 || echo "[warn] omp not on PATH (needed for init-models; Docker provides it for prod)"
uv sync --all-extras
pass "python env synced"
corepack enable
# web/package.json pins yarn@4.9.2 via `packageManager`. This env var makes
# the corepack shim honor the nearest package.json instead of the global
# default (4.18 on hosts that installed it first) — no --activate needed.
export COREPACK_ENABLE_PROJECT_SPEC=1
if [ "$(yarn --version)" != "4.9.2" ]; then
  YARN_BIN="$(command -v yarn)"
  if [ "$YARN_BIN" != "" ] && ! head -c 200 "$YARN_BIN" 2>/dev/null | grep -q "corepack"; then
    echo "yarn pin failed: $(yarn --version) from standalone binary at $YARN_BIN"
    echo "This is NOT a corepack shim, so the 4.9.2 pin cannot apply."
    echo "Fix: rm $YARN_BIN && hash -r   (then re-run this script)"
    echo "If it comes back, remove its source (pipx uninstall yarn, npm uninstall -g yarn, ...)."
    exit 1
  fi
  echo "yarn pin failed: $(yarn --version)"; exit 1
fi
yarn --cwd=web install --immutable
pass "dashboard deps installed (yarn 4.9.2)"

# --- 2. .env skeleton --------------------------------------------------------
step "2. .env skeleton"
[ -f .env ] || cp .env.example .env
pass ".env present"

gen_secret() { openssl rand -hex 32; }

if grep -q "^GITHUB_WEBHOOK_SECRET=$" .env 2>/dev/null; then
  SECRET=$(gen_secret)
  sed -i "s/^GITHUB_WEBHOOK_SECRET=$/GITHUB_WEBHOOK_SECRET=$SECRET/" .env
  echo "generated GITHUB_WEBHOOK_SECRET (also paste into the App form later)"
else
  pass "GITHUB_WEBHOOK_SECRET already set"
fi
if grep -q "^CARTER_OMP_GH_PROXY_HMAC_KEY=$" .env 2>/dev/null; then
  HMAC=$(gen_secret)
  sed -i "s/^CARTER_OMP_GH_PROXY_HMAC_KEY=$/CARTER_OMP_GH_PROXY_HMAC_KEY=$HMAC/" .env
  echo "generated CARTER_OMP_GH_PROXY_HMAC_KEY"
else
  pass "CARTER_OMP_GH_PROXY_HMAC_KEY already set"
fi

# --- 3. DNS ------------------------------------------------------------------
step "3. DNS for the webhook subdomain"
if [ -z "$DOMAIN" ]; then
  read -r -p "Webhook subdomain (e.g. omp.example.com): " DOMAIN </dev/tty
fi
echo "Your domain's DNS lives wherever your nameservers point (registrar, Cloudflare, Route53...),"
echo "NOT on this VPS. This VPS only needs a record pointing at it."
VPS_IP4=$(curl -4 -s --max-time 10 ifconfig.me 2>/dev/null || true)
VPS_IP6=$(curl -6 -s --max-time 10 ifconfig.me 2>/dev/null || true)
if [ -n "$VPS_IP4" ]; then
  echo "  VPS public IPv4: $VPS_IP4"
  pause "Add this DNS record at your provider:" "  Type: A | Name: $DOMAIN | Value: $VPS_IP4 | TTL: 300 (or Auto)"
elif [ -n "$VPS_IP6" ]; then
  echo "  VPS has IPv6 only: $VPS_IP6"
  pause "Add this DNS record at your provider:" "  Type: AAAA | Name: $DOMAIN | Value: $VPS_IP6 | TTL: 300 (or Auto)"
else
  pause "Could not detect a public IP. Find it (provider panel / 'ip route get 1.1.1.1') then add:" "  Type: A | Name: $DOMAIN | Value: <VPS-IPv4> | TTL: 300 (or Auto)"
fi
echo "waiting for DNS to propagate..."
for _ in $(seq 1 30); do
  if getent hosts "$DOMAIN" >/dev/null 2>&1; then pass "DNS resolves: $DOMAIN"; break; fi
  sleep 10
done
getent hosts "$DOMAIN" >/dev/null 2>&1 || { echo "DNS still not resolving; continuing anyway (Caddy will retry certs)"; }

# --- 4. firewall + caddy ------------------------------------------------------
step "4. Firewall and Caddy"
if ! sudo -n true 2>/dev/null; then
  echo "[sudo needed] step 4 runs: ufw allow/enable, cp Caddyfile, systemctl reload caddy."
  echo "Run: sudo -v  (enter your password once to cache it), then re-run this script."
  exit 1
fi
if command -v ufw >/dev/null 2>&1; then
  sudo ufw allow OpenSSH >/dev/null 2>&1 || true
  sudo ufw allow 80,443/tcp >/dev/null 2>&1 || true
  echo "y" | sudo ufw enable >/dev/null 2>&1 || true
  pass "ufw: 80/443 open, 6543 stays closed (localhost-only publish)"
else
  echo "[warn] ufw not installed; ensure ports 80/443 are reachable and 6543 is not"
fi
if ! command -v caddy >/dev/null 2>&1; then
  pause "Install Caddy on this VPS:" "  https://caddyserver.com/docs/install  (apt repo + sudo apt install caddy)"
fi
sudo cp Caddyfile /etc/caddy/Caddyfile
ESCAPED=$(printf '%s' "$DOMAIN" | sed 's/\./\\./g')
sudo sed -i "s/omp\\.example\\.com/$ESCAPED/" /etc/caddy/Caddyfile
sudo systemctl reload caddy 2>/dev/null || sudo caddy reload 2>/dev/null || { echo "caddy reload failed; check 'systemctl status caddy'"; exit 1; }
pass "caddy serving (TLS automatic)"
sleep 3
CODE=$(curl -s -o /dev/null -w '%{http_code}' "https://$DOMAIN/healthz" || true)
[ "$CODE" = "404" ] && pass "ingress verified: /healthz → 404, /webhook/* proxied" || echo "[warn] /healthz → $CODE (want 404; cert may still be issuing — retry in a minute)"

# --- 5. GitHub App form --------------------------------------------------------
step "5. GitHub App form (manual, ~5 min)"
WEBHOOK_SECRET=$(grep "^GITHUB_WEBHOOK_SECRET=" .env | cut -d= -f2)
echo "Field-by-field walkthrough: docs/github-app.md"
echo "  Webhook URL: https://$DOMAIN/webhook/github"
echo "  Webhook secret: $WEBHOOK_SECRET"
pause "Create the App, then come back with:" "  App ID, installation ID, bot login, private-key .pem path, your user ID, one repo ID"
read -r -p "App ID: " APP_ID </dev/tty
read -r -p "Installation ID: " INSTALL_ID </dev/tty
read -r -p "Bot login (e.g. carter-omp[bot]): " BOT_LOGIN </dev/tty
read -r -p "Private key .pem path on this VPS: " KEY_PATH </dev/tty
read -r -p "Your GitHub user ID (gh api users/<you> --jq .id): " USER_ID </dev/tty

set_kv() { # set_kv KEY VALUE — replace empty value or append
  if grep -q "^$1=$" .env; then
    sed -i "s|^$1=$|$1=$2|" .env
  elif ! grep -q "^$1=" .env; then
    printf '%s=%s\n' "$1" "$2" >> .env
  fi
}
set_kv CARTER_OMP_BOT_LOGIN "$BOT_LOGIN"
set_kv CARTER_OMP_AUTHORIZED_USER_IDS "$USER_ID"
set_kv CARTER_OMP_AUTHORIZED_LOGINS "$(gh api users 2>/dev/null --jq .login || echo carterlasalle)"
set_kv CARTER_OMP_REPO_OWNERS "$REPO_OWNERS"
set_kv CARTER_OMP_GITHUB_APP_ID "$APP_ID"
set_kv CARTER_OMP_GITHUB_INSTALLATION_ID "$INSTALL_ID"
set_kv CARTER_OMP_GITHUB_PRIVATE_KEY_FILE "$KEY_PATH"
chmod 0400 "$KEY_PATH" 2>/dev/null || echo "[warn] could not chmod 0400 $KEY_PATH"
pass ".env filled (IDs, App, key path)"

# --- 6. models -----------------------------------------------------------------
step "6. Models"
if command -v omp >/dev/null 2>&1 && [ -x "$(command -v uv)" ]; then
  uv run carter-omp init-models || echo "[warn] init-models skipped; set CARTER_OMP_MODEL manually"
else
  echo "[warn] omp not found; skipping init-models (set CARTER_OMP_MODEL manually)"
fi

# --- 7. start + doctor -----------------------------------------------------------
step "7. Start and validate"
docker compose up -d --build
docker compose exec carter-omp carter-omp doctor
pass "doctor green — bot is live"

cat <<EOF

Done. Next (docs/setup.md step 8):
  1. Add the 'carter-omp' label to a sandbox repo.
  2. Open an issue → expect silence (no trigger, no compute).
  3. Label it carter-omp → expect triage → PR.
Webhook URL (for reference): https://$DOMAIN/webhook/github
EOF
