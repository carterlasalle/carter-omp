#!/usr/bin/env bash
# trace:exempt reason=deploy-packaging-no-runtime-behavior
# Deploy carter-omp on this host: pull, wait for in-flight work, rebuild, recreate.
#
# `docker compose up -d` (or a rebuild that replaces the running container) cuts
# off whatever the orchestrator is executing. The queue does recover: on the next
# boot `reset_stuck_running` requeues interrupted events and the agent resumes
# its omp session (`--continue`), which is why a run can look like it "reacted,
# then vanished, then continued". But the person watching GitHub sees a stalled
# run, and every deploy costs the work in flight. This waits for `running` to
# drain first, so a deploy never cuts a run short
# (carterlasalle/personal_website#81: a tag started a run, a deploy cut it off).
#
# Usage:
#   scripts/deploy.sh                  pull, wait up to 20 min, build, recreate, doctor
#   scripts/deploy.sh --check          report in-flight work and exit (no changes)
#   scripts/deploy.sh --force          deploy now even with runs in flight
#   scripts/deploy.sh --timeout 600    bound the wait, in seconds
#   scripts/deploy.sh --no-pull        deploy the checkout as-is
set -euo pipefail

cd "$(dirname "$0")/.."

timeout_s=1200
force=0
check_only=0
pull=1
while [ $# -gt 0 ]; do
  case "$1" in
    --check) check_only=1 ;;
    --force) force=1 ;;
    --no-pull) pull=0 ;;
    --timeout) shift; timeout_s="${1:?--timeout needs seconds}" ;;
    -h | --help)
      sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 2
      ;;
  esac
  shift
done

# In-flight work, read from the orchestrator's own DB so this agrees with what
# the dispatcher believes. Prints "running=<n>" then one line per event; the
# first line is machine-read, the rest are shown verbatim (never re-split).
inflight() {
  docker compose exec -T carter-omp python3 - <<'PY' 2>/dev/null || echo "running=0"
import os
import sqlite3

path = os.environ.get("CARTER_OMP_SQLITE_PATH", "/data/carter-omp.sqlite")
try:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    rows = conn.execute(
        "SELECT issue_key, substr(delivery_id, 1, 12), state, attempts FROM events "
        "WHERE state IN ('queued', 'running') ORDER BY state DESC, rowid"
    ).fetchall()
except Exception:
    rows = []

print(f"running={sum(1 for row in rows if row[2] == 'running')}")
for key, delivery, state, attempts in rows:
    print(f"{key or delivery} ({state}, attempt {attempts})")
PY
}

report_inflight() {
  snapshot="$1"
  running="$(printf '%s\n' "$snapshot" | sed -n 's/^running=//p' | head -1)"
  [ -z "$running" ] && running=0
  if [ "$running" = "0" ]; then
    echo "no runs in flight"
    return
  fi
  echo "$running run(s) in flight:"
  printf '%s\n' "$snapshot" | sed '1d' | while IFS= read -r line; do
    [ -n "$line" ] && printf '  %s\n' "$line"
  done
}

count_running() {
  printf '%s\n' "$1" | sed -n 's/^running=//p' | head -1
}

snapshot="$(inflight)"
running="$(count_running "$snapshot")"
report_inflight "$snapshot"

if [ "$check_only" = "1" ]; then
  exit 0
fi

if [ "${running:-0}" != "0" ] && [ "$force" != "1" ]; then
  echo "waiting for them to finish (--timeout ${timeout_s}s, --force to deploy anyway)"
  deadline=$((SECONDS + timeout_s))
  while [ "$SECONDS" -lt "$deadline" ]; do
    sleep 15
    snapshot="$(inflight)"
    running="${snapshot%%|*}"
    [ "${running:-0}" = "0" ] && break
  done
  if [ "${running:-0}" != "0" ]; then
    echo "still ${running} run(s) in flight after ${timeout_s}s; not deploying." >&2
    echo "Re-run with --force to interrupt them (they resume after the restart," >&2
    echo "losing only the work in flight), or wait and re-run." >&2
    exit 1
  fi
  echo "in-flight work finished"
fi

if [ "$pull" = "1" ]; then
  git pull --ff-only
fi

docker compose build
docker compose up -d
sleep 5
docker compose exec -T carter-omp carter-omp doctor
echo "deployed $(git rev-parse --short HEAD)"
