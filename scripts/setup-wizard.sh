#!/usr/bin/env bash
# trace:exempt reason=deploy-packaging-no-runtime-behavior
# carter-omp setup wizard. Two modes:
#   ./scripts/setup-wizard.sh --domain omp.example.com   # full VPS run
#   ./scripts/setup-wizard.sh --resume                    # re-check everything,
#      report what is done vs missing, fix what is fixable, resume where stopped
# Idempotent: every step detects prior completion and skips or verifies.
set -euo pipefail

DOMAIN=""
REPO_OWNERS="carterlasalle"
RESUME=0
while [ $# -gt 0 ]; do
  case "$1" in
    --domain=*) DOMAIN="${1#--domain=}" ;;
    --domain) DOMAIN="${2:-}"; shift ;;
    --repo-owners=*) REPO_OWNERS="${1#--repo-owners=}" ;;
    --repo-owners) REPO_OWNERS="${2:-}"; shift ;;
    --resume) RESUME=1 ;;
  esac
  shift
done

pass() { printf '\033[32m[ok]\033[0m %s\n' "$*"; }
skip() { printf '\033[34m[skip]\033[0m %s\n' "$*"; }
todo() { printf '\033[33m[TODO]\033[0m %s\n' "$*"; }
step() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
pause() { # pause "WHY" "WHAT-TO-DO"
  printf '\n\033[33m[PASTE NEEDED] %s\033[0m\n%s\n' "$1" "$2"
  read -r -p "Press Enter when done... " _ </dev/tty
}
need() { command -v "$1" >/dev/null 2>&1 || { echo "missing: $1 ($2)"; exit 1; }; }
env_val() { grep "^$1=" .env 2>/dev/null | cut -d= -f2-; }
env_set() { # env_set KEY — true if non-empty
  [ -f .env ] && [ -n "$(env_val "$1")" ]
}
set_kv() { # set_kv KEY VALUE — replace empty value or append
  if grep -q "^$1=$" .env; then
    sed -i "s|^$1=$|$1=$2|" .env
  elif ! grep -q "^$1=" .env; then
    printf '%s=%s\n' "$1" "$2" >> .env
  fi
}

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
# (A standalone yarn binary shadowing the shim, e.g. ~/.local/bin/yarn,
# ignores this entirely: remove it so the shim resolves.)
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
step "2. Secrets (.env)"
[ -f .env ] || cp .env.example .env
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
  if getent hosts "$DOMAIN" >/dev/null 2>&1; then
    skip "DNS already resolves: $DOMAIN"
  else
    pause "Add this DNS record at your provider:" "  Type: A | Name: $DOMAIN | Value: $VPS_IP4 | TTL: 300 (or Auto)"
  fi
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
# Coexist with Tailscale Serve (binds 443 on tailnet IPs): bind Caddy to the
# VPS public IPv4 only. VPS_IP4 was detected in step 3.
if [ -n "${VPS_IP4:-}" ]; then
  sudo sed -i "s/{\$PUBLIC_IP:0.0.0.0}/$VPS_IP4/" /etc/caddy/Caddyfile
fi
if sudo systemctl is-active --quiet caddy 2>/dev/null; then
  sudo systemctl reload caddy 2>/dev/null || sudo caddy reload 2>/dev/null || { echo "caddy reload failed; check 'systemctl status caddy'"; exit 1; }
else
  # reload is a no-op on a failed/inactive unit — restart fresh so the new
  # Caddyfile (bind IP, webhook routes) actually loads.
  sudo systemctl restart caddy || { echo "caddy restart failed; check 'systemctl status caddy'"; exit 1; }
fi
pass "caddy serving (TLS automatic)"
sleep 3
CODE=$(curl -s -o /dev/null -w '%{http_code}' "https://$DOMAIN/healthz" || true)
[ "$CODE" = "404" ] && pass "ingress verified: /healthz → 404, /webhook/* proxied" || echo "[warn] /healthz → $CODE (want 404; cert may still be issuing — retry in a minute)"

# --- 5. identity (.env values, each explained) ----------------------------------
step "5. Identity — who may trigger, where, and as what"
echo "Each value below says what it is, why the bot needs it, and where it goes."
ask_kv() { # ask_kv KEY PROMPT WHY [FETCH_HINT]
  local key="$1" prompt="$2" why="$3" hint="${4:-}"
  if env_set "$key"; then
    if [ "$key" = "CARTER_OMP_GITHUB_PRIVATE_KEY_FILE" ] && [ ! -f "$(env_val "$key")" ]; then
      echo ""
      todo "$key is '$(env_val "$key")' but that file does not exist — re-enter it below."
    else
      skip "$key already set ($(env_val "$key" | cut -c1-24))"
      return 0
    fi
  fi
  echo ""
  echo "  $key — $why"
  [ -n "$hint" ] && echo "  Find it: $hint"
  local val=""
  read -r -p "  $prompt: " val </dev/tty
  [ -n "$val" ] && set_kv "$key" "$val" && pass "$key set" || { todo "$key left empty — re-run with --resume"; return 1; }
}

ask_kv CARTER_OMP_BOT_LOGIN "Bot login" \
  "Github-App bot identity. The router rejects events sent BY the bot (loop protection) and matches @mentions against it." \
  "App settings URL slug: github.com/settings/apps/<slug> → <slug>[bot]; verify: gh api \"/users/<slug>[bot]\" --jq .login"

ask_kv CARTER_OMP_AUTHORIZED_USER_IDS "Your GitHub user ID" \
  "Immutable operator identity. ONLY this sender.id can trigger runs — logins can be renamed/reused, IDs cannot." \
  "gh api users/<you> --jq .id"

if ! env_set CARTER_OMP_AUTHORIZED_LOGINS; then
  echo ""
  echo "  CARTER_OMP_AUTHORIZED_LOGINS — readability twin of the ID above (diagnostics only, never authority). Startup refuses when login/ID disagree (username-reuse protection)."
  set_kv CARTER_OMP_AUTHORIZED_LOGINS "$(gh api users 2>/dev/null --jq .login || echo carterlasalle)"
fi

echo ""
echo "  Repo scope — pick ONE:"
echo "    (a) CARTER_OMP_REPO_OWNERS=$REPO_OWNERS — every repo under your slug, past/present/future, zero bookkeeping. The App-install list stays the real boundary."
echo "    (b) CARTER_OMP_REPO_IDS — explicit immutable repo IDs (gh api repos/<o>/<r> --jq .id), maximal control."
if env_set CARTER_OMP_REPO_OWNERS || env_set CARTER_OMP_REPO_IDS; then
  skip "repo scope already set"
else
  set_kv CARTER_OMP_REPO_OWNERS "$REPO_OWNERS" && pass "repo scope: owner $REPO_OWNERS"
fi

ask_kv CARTER_OMP_GITHUB_APP_ID "App ID" \
  "Identifies the GitHub App for JWT minting (proxy side). Public, not secret." \
  "App settings page → App ID (numeric, e.g. 5055738)"

ask_kv CARTER_OMP_GITHUB_INSTALLATION_ID "Installation ID" \
  "Which install of the App (your account). Scopes tokens to your repos. Rotates on reinstall — re-run --resume if webhooks 401." \
  "Install page URL ends /installations/<ID>, or JWT + GET /app/installations"

ask_kv CARTER_OMP_GITHUB_PRIVATE_KEY_FILE "Private key .pem path on this VPS" \
  "Signs installation-token requests. Mode 0400, visible ONLY to the github-proxy container — never pasted into .env." \
  "scp the .pem here, then chmod 0400 <path>"

if [ -n "$(env_val CARTER_OMP_GITHUB_PRIVATE_KEY_FILE)" ]; then
  chmod 0400 "$(env_val CARTER_OMP_GITHUB_PRIVATE_KEY_FILE)" 2>/dev/null || echo "[warn] could not chmod 0400 the key"
fi

if ! env_set CARTER_OMP_GIT_AUTHOR_EMAIL; then
  echo ""
  echo "  CARTER_OMP_GIT_AUTHOR_EMAIL — commit author on bot-pushed branches (display only; proves nothing about authorization)."
  echo "  Default: carter-omp[bot]@users.noreply.github.com — press Enter to accept."
  read -r -p "  Commit author email: " EMAIL </dev/tty
  set_kv CARTER_OMP_GIT_AUTHOR_EMAIL "${EMAIL:-carter-omp[bot]@users.noreply.github.com}"
fi

# --- 6. trigger policy (safe defaults, explained) -------------------------------
step "6. Trigger policy — what may start the bot"
echo "Defaults are production-safe (explicit invocation only). Each variable is"
echo "explained in docs/triggers.md. Press Enter to accept each default."
ask_default() { # ask_default KEY DEFAULT WHY
  local key="$1" default="$2" why="$3" cur val
  cur="$(env_val "$key")"
  if [ -n "$cur" ]; then skip "$key=$cur"; return 0; fi
  echo ""
  echo "  $key (default: $default) — $why"
  read -r -p "  Value [$default]: " val </dev/tty
  set_kv "$key" "${val:-$default}"
}
ask_default CARTER_OMP_TRIGGER_MODE strict "strict = only your label/mention runs; legacy = old ambient behavior (tests only)."
ask_default CARTER_OMP_TRIGGER_LABEL carter-omp "Label name that acts as the one-shot button. The bot consumes it on accept."
ask_default CARTER_OMP_LABEL_TRIGGERS true "Master switch for label triggers. false disables even yours."
ask_default CARTER_OMP_MENTION_TRIGGERS true "Master switch for @mention triggers. false disables even yours."
ask_default CARTER_OMP_AUTO_ISSUE_TRIAGE false "true = every opened issue auto-runs (ambient; startup refuses it in strict mode)."
ask_default CARTER_OMP_AUTO_PR_REVIEW false "true = every opened PR auto-reviews (ambient; refused in strict)."
ask_default CARTER_OMP_AUTO_COMMENT_FOLLOWUPS false "true = any comment resumes the session (ambient; refused in strict)."
ask_default CARTER_OMP_REVIEWER_BOTS "" "Bot logins whose comments authorize without a mention. Empty = reviews are context only."
ask_default CARTER_OMP_RELEASE_SENTINEL_ENABLED false "true = CI completions self-start release repair (dangerous opt-in)."
ask_default CARTER_OMP_QUESTION_AUTOCLOSE_ENABLED false "Keeps question-autoclose machinery available (never starts a model run)."

# --- 7. models -----------------------------------------------------------------
step "7. Models"
if command -v omp >/dev/null 2>&1; then
  if [ -f ~/.omp/agent/models.container.yml ]; then
    skip "model catalog present (~/.omp/agent/models.container.yml)"
  else
    uv run carter-omp init-models || echo "[warn] init-models skipped; set CARTER_OMP_MODEL manually"
  fi
else
  echo "[warn] omp not found; skipping init-models (set CARTER_OMP_MODEL manually)"
fi
if ! env_set CARTER_OMP_MODEL; then
  echo ""
  echo "  CARTER_OMP_MODEL — single selector or comma pool (random pick per task), e.g. opencode-go/muse-spark-1.3-contributor,openrouter/qwen/qwen3.7-flash."
  read -r -p "  Model pool: " POOL </dev/tty
  [ -n "$POOL" ] && set_kv CARTER_OMP_MODEL "$POOL" || todo "CARTER_OMP_MODEL empty — set it before starting"
fi
if ! env_set CARTER_OMP_THINKING; then
  set_kv CARTER_OMP_THINKING high && pass "thinking=high (default)"
fi

# --- 8. start + report -----------------------------------------------------------
step "8. Start and report"
if docker compose up -d --build 2>&1 | tail -1; then
  pass "containers up"
else
  todo "compose failed — fix the error above, then re-run with --resume"
  exit 1
fi
echo ""
echo "--- doctor ---"
if docker compose exec carter-omp carter-omp doctor; then
  pass "doctor green — bot is live"
else
  echo ""
  todo "doctor reported failures (listed above). Fix .env values, then: ./scripts/setup-wizard.sh --resume"
  exit 1
fi

cat <<EOF

Done. Next (docs/setup.md step 8):
  1. Add the 'carter-omp' label to a sandbox repo.
  2. Open an issue → expect silence (no trigger, no compute).
  3. Label it carter-omp → expect triage → PR.
Webhook URL (for reference): https://$DOMAIN/webhook/github
Re-check anything any time: ./scripts/setup-wizard.sh --resume
EOF
