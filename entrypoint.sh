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
if [ "${1:-}" = "python" ] && [ "${2:-}" = "-m" ] && [[ "${3:-}" == carter_omp.proxy* ]]; then
    is_proxy_role=1
elif [[ "${1:-}" == *"carter_omp.proxy"* ]]; then
    is_proxy_role=1
fi

# The image runs as root; user/group creation must succeed. If /etc is
# read-only or shadow files are corrupt, fail with a message (not a restart
# loop under `set -e` swallowing the cause).
/usr/sbin/groupadd -f -g 2000 omp 2>/dev/null || {
  echo "carter-omp: cannot write /etc/group (read-only /etc or corrupt shadow?). Fix the mount/image, not the retry loop." >&2
  exit 1
}
max_slots="${CARTER_OMP_MAX_CONCURRENCY:-8}"
for i in $(seq 1 "$max_slots"); do
    user="omp-$i"
    slot_group="omp-$i"
    slot_id=$((2000 + i))
    /usr/sbin/groupadd -f -g "$slot_id" "$slot_group"
    id -u "$user" >/dev/null 2>&1 || /usr/sbin/useradd -u "$slot_id" -g "$slot_group" -G omp -M -N -s /usr/sbin/nologin "$user"
    /usr/sbin/usermod -g "$slot_group" -a -G omp "$user"
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
