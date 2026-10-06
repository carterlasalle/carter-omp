# trace:exempt reason=deploy-packaging-no-runtime-behavior
#!/usr/bin/env bash
# carter-omp container entrypoint. No per-boot pip installs — everything is baked
# into the image; we only sanity-check the runtime mount and create state dirs.
#
# Used by both the orchestrator (CMD: `python -m carter_omp serve`) and the
# sibling github-proxy (compose command: `python -m carter_omp.proxy serve`). The
# proxy role does NOT need a $PI_ROOT pi checkout — it never runs omp.
set -euo pipefail

# Shared git metadata under /data/workspaces/_pool is intentionally group
# writable by the `omp` group so interrupted work can resume on a different
# slot user. Keep new files and directories compatible with that model.
umask 0002

# Detect the proxy role by inspecting the command. Compose passes `command:`
# as $@ here (after tini --), so $1=python, $2=-m, $3=carter_omp.proxy is the
# canonical shape; we also accept a single concatenated arg for safety.
is_proxy_role=0
case "${3:-} ${1:-}" in
  carter_omp.proxy*|*"carter_omp.proxy"*) is_proxy_role=1 ;;
esac

# Slot identities are baked into the image (see Dockerfile); /etc is immutable
# runtime config here. Validate instead of mutating.
max_slots="${CARTER_OMP_MAX_CONCURRENCY:-8}"
baked_slots="${CARTER_OMP_BAKED_SLOT_COUNT:-32}"
case "$max_slots" in
    ''|*[!0-9]*)
        echo "carter-omp: CARTER_OMP_MAX_CONCURRENCY must be a positive integer" >&2
        exit 1
        ;;
esac
if [ "$max_slots" -lt 1 ] || [ "$max_slots" -gt "$baked_slots" ]; then
    echo "carter-omp: concurrency $max_slots exceeds $baked_slots baked sandbox slots" >&2
    exit 1
fi
if [ "$(getent group omp | cut -d: -f3)" != "2000" ]; then
    echo "carter-omp: expected omp group gid 2000" >&2
    exit 1
fi
for i in $(seq 1 "$max_slots"); do
    expected=$((2000 + i))
    if [ "$(id -u "omp-$i" 2>/dev/null)" != "$expected" ]; then
        echo "carter-omp: omp-$i missing or has wrong uid" >&2
        exit 1
    fi
    if [ "$(id -g "omp-$i" 2>/dev/null)" != "$expected" ]; then
        echo "carter-omp: omp-$i has wrong primary gid" >&2
        exit 1
    fi
    if ! id -G "omp-$i" | tr ' ' '\n' | grep -qx 2000; then
        echo "carter-omp: omp-$i is not a member of shared omp group" >&2
        exit 1
    fi
done

if [ "$is_proxy_role" -eq 1 ]; then
    exec "$@"
fi

# OMP is a pinned prebuilt binary baked into the image (see Dockerfile);
# no oh-my-pi monorepo checkout is mounted or required.
command -v omp >/dev/null 2>&1 || {
    echo "carter-omp: omp binary not found on PATH" >&2
    exit 1
}

mkdir -p /data/workspaces /data/workspaces/_pool /data/logs
# Persistent build caches under the /data volume. CARGO_HOME,
# CARGO_TARGET_DIR, and RUSTUP_HOME are pinned to these paths in the image ENV
# so every per-issue worktree shares one cargo target/toolchain. Bun install
# cache is workspace-private; a shared cache is unsafe across slot users
# because bun may chmod/chown its cache root to the first writer.
mkdir -p /data/cache/cargo /data/cache/cargo-target /data/cache/rustup /data/cache/pi-natives
chown -R root:omp /data/cache /data/workspaces/_pool
find /data/cache /data/workspaces/_pool -type d -exec chmod 2770 {} +
find /data/cache /data/workspaces/_pool -type f -perm /111 -exec chmod 0770 {} +
find /data/cache /data/workspaces/_pool -type f ! -perm /111 -exec chmod 0660 {} +
chmod 0700 /data/logs


rm -rf /srv/agent-home/.agent /srv/agent-home/.omp/agent
mkdir -p /srv/agent-home/.agent /srv/agent-home/.omp/agent
if [ -e /srv/agent-home-stage/.agent ]; then
    cp -a /srv/agent-home-stage/.agent/. /srv/agent-home/.agent/
fi
if [ -e /srv/agent-home-stage/.omp/agent ]; then
    cp -a /srv/agent-home-stage/.omp/agent/. /srv/agent-home/.omp/agent/
fi
chown -R root:root /srv/agent-home || true
find /srv/agent-home -type d -exec chmod 0755 {} +
find /srv/agent-home -type f -exec chmod 0644 {} +

# omp registers daemon project presence under ~/.omp/run at startup, nesting
# per-project dirs (daemons/<hash>/clients) that any slot user must be able to
# create and enter regardless of which slot first made them: setgid + group
# omp keeps the whole tree group-writable (entrypoint umask 0002 carries into
# slot processes, so new entries stay group-writable too).
mkdir -p /srv/agent-home/.omp/run
chgrp -R omp /srv/agent-home/.omp/run
chmod -R g+rwX /srv/agent-home/.omp/run
find /srv/agent-home/.omp/run -type d -exec chmod g+s {} +
chmod 2770 /srv/agent-home/.omp/run

touch /data/carter-omp.sqlite
chown root:root /data/carter-omp.sqlite
chmod 0600 /data/carter-omp.sqlite
for db_file in /data/carter-omp.sqlite-wal /data/carter-omp.sqlite-shm; do
    if [ -e "$db_file" ]; then
        chown root:root "$db_file"
        chmod 0600 "$db_file"
    fi
done

exec "$@"
