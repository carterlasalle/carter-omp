# syntax=docker/dockerfile:1.7-labs
###############################################################################
# trace:exempt reason=deploy-packaging-no-runtime-behavior
# carter-omp — explicitly-invoked GitHub coding-agent orchestrator.
#
# Standalone image: no oh-my-pi monorepo checkout required. OMP arrives as a
# pinned prebuilt binary (verified by sha256); the compatible `omp-rpc`
# Python package is vendored under `vendor/omp-rpc`. The SolidJS dashboard
# is built in the `web-builder` stage (Yarn) and copied into the Python
# package before install.
#
# Build:
#     docker build -t carter-omp:dev .
#
# Run (recommended):
#     cp .env.example .env
#     docker compose up -d --build
###############################################################################

# Pinned OMP release. Bump deliberately; keep vendor/omp-rpc in sync.
# Checksums from the v18.2.11 SHA256SUMS.txt release asset.
ARG OMP_VERSION=18.2.11
ARG OMP_SHA256_X64=97cf39557bf3d98327dd4c9814380b7e6bcb76e41169edd9b0ff30424c733a2a
ARG OMP_SHA256_ARM64=c115f95a0a0081d3724a3c878231fb8c3b2fdb25ad5ad6c3b30d152a82b3939e
ARG BUN_VERSION=1.3.12
ARG PYTHON_VERSION=3.12
ARG YARN_VERSION=4.9.2

############################
# 1) web-builder — Yarn + Vite, builds the SolidJS dashboard bundle.
############################
FROM node:22-slim AS web-builder
ARG YARN_VERSION
WORKDIR /work
ENV COREPACK_ENABLE_PROJECT_SPEC=1
COPY web/package.json web/yarn.lock* ./web/
COPY web/tsconfig.json web/vite.config.ts ./web/
RUN corepack enable && yarn --cwd=web install --immutable
COPY web/ ./web/
RUN yarn --cwd=web build

############################
# 2) runtime — Python + uv + pinned OMP + carter-omp
############################
FROM python:${PYTHON_VERSION}-slim AS runtime

ARG OMP_VERSION
ARG OMP_SHA256_X64
ARG OMP_SHA256_ARM64
ARG BUN_VERSION
ARG YARN_VERSION
ARG TARGETARCH

# curl + CA certs for the pinned binary downloads below (python-slim
# omits both). No `curl | sh`; every download is checksum-verified.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
    ca-certificates curl unzip git tini sqlite3 \
 && rm -rf /var/lib/apt/lists/*
# Pinned OMP prebuilt binary (no `curl | sh`; checksum-verified download).
# Release assets: omp-linux-x64 / omp-linux-arm64 (bare binaries, not zips).
RUN case "${TARGETARCH:-amd64}" in \
        arm64) OMP_ASSET=omp-linux-arm64; OMP_SHA256="${OMP_SHA256_ARM64}" ;; \
        *) OMP_ASSET=omp-linux-x64; OMP_SHA256="${OMP_SHA256_X64}" ;; \
    esac \
    && curl -fsSL -o /tmp/omp \
        "https://github.com/can1357/oh-my-pi/releases/download/v${OMP_VERSION}/${OMP_ASSET}" \
    && echo "${OMP_SHA256}  /tmp/omp" | sha256sum -c - \
    && mv /tmp/omp /usr/local/bin/omp \
    && chmod +x /usr/local/bin/omp \
    && omp --version

# Bun is required only as the OMP runtime fallback (pinned npm package path);
# carter-omp's own JS development uses Yarn (see web/).
RUN case "${TARGETARCH:-amd64}" in \
        arm64) BUN_ASSET=bun-linux-aarch64 ;; \
        *) BUN_ASSET=bun-linux-x64 ;; \
    esac \
    && curl -fsSL "https://github.com/oven-sh/bun/releases/download/bun-v${BUN_VERSION}/${BUN_ASSET}.zip" \
        -o /tmp/bun.zip \
    && unzip -o /tmp/bun.zip -d /tmp/bun-extract \
    && mv "/tmp/bun-extract/${BUN_ASSET}/bun" /usr/local/bin/bun \
    && chmod +x /usr/local/bin/bun \
    && rm -rf /tmp/bun.zip /tmp/bun-extract \
    && bun --version

WORKDIR /app

COPY pyproject.toml uv.lock* ./
COPY vendor/ ./vendor/
COPY src/ ./src/
COPY --from=web-builder /work/web/dist/ ./src/carter_omp/static/

RUN pip install --no-cache-dir uv \
    && uv sync --frozen --no-dev \
    && uv pip install --no-deps .
# uv sync created /app/.venv holding the deps + project, but the image's
# default `python` is system python. Point PATH at the venv so runtime
# `python -m carter_omp` and the `carter-omp` console script resolve there.
ENV VIRTUAL_ENV=/app/.venv
ENV PATH="/app/.venv/bin:${PATH}"

# Host agent config is mounted read-only under /srv/agent-home-stage with
# host-controlled permissions (see compose.yaml). The entrypoint copies it
# into root-owned world-readable files under /srv/agent-home; the agent
# subprocess runs with HOME=/srv/agent-home.
RUN mkdir -p /srv/agent-home/.agent /srv/agent-home/.omp/agent \
    && mkdir -p /srv/agent-home-stage/.agent /srv/agent-home-stage/.omp/agent

COPY entrypoint.sh /usr/local/bin/carter-omp-entrypoint
RUN chmod +x /usr/local/bin/carter-omp-entrypoint

ARG OMP_SLOT_COUNT=32
ENV CARTER_OMP_BAKED_SLOT_COUNT=${OMP_SLOT_COUNT}
# Slot identities are baked into the image: /etc is immutable runtime config,
# /data is mutable runtime state. The entrypoint only validates them.
RUN set -eux; \
    groupadd --gid 2000 omp; \
    i=1; \
    while [ "$i" -le "$OMP_SLOT_COUNT" ]; do \
        slot_id=$((2000 + i)); \
        groupadd --gid "$slot_id" "omp-$i"; \
        useradd --uid "$slot_id" --gid "$slot_id" --groups omp \
            --no-create-home --no-user-group --shell /usr/sbin/nologin "omp-$i"; \
        i=$((i + 1)); \
    done; \
    useradd -u 10000 -m -U -s /usr/sbin/nologin carter-omp \
    && mkdir -p /data/workspaces /data/logs \
    && chown -R carter-omp:carter-omp /app /data

VOLUME ["/data"]
EXPOSE 8080
EXPOSE 8081

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/carter-omp-entrypoint"]
CMD ["python", "-m", "carter_omp", "serve"]
