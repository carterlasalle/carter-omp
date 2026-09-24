"""`python -m carter_omp.proxy serve` — run the github-proxy FastAPI app."""

from __future__ import annotations

import sys

import click
import uvicorn

from carter_omp.config import Settings, load_proxy_settings
from carter_omp.logging_config import configure_logging
from carter_omp.proxy.server import create_proxy_app


def _settings_or_die() -> Settings:
    """Load proxy-only settings, surfacing config errors as exit code 2.

    Routes through `load_proxy_settings` (NOT the orchestrator `Settings()`
    ctor) so the github-proxy container only needs `GITHUB_TOKEN` +
    `CARTER_OMP_GH_PROXY_HMAC_KEY` — the orchestrator's webhook secret,
    bot_login, and proxy-URL fields are irrelevant here.
    """
    try:
        return load_proxy_settings()
    except Exception as exc:
        click.echo(f"github-proxy configuration error: {exc}", err=True)
        sys.exit(2)


@click.group()
def main() -> None:
    """github-proxy control surface."""


# trace:v1 id=impl.proxy-serve-app-mode work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-9N23MPRP
@main.command()
def serve() -> None:
    """Run the HMAC-authenticated GitHub proxy."""
    cfg = _settings_or_die()
    configure_logging(cfg.log_dir)
    cfg.ensure_paths()
    # `load_proxy_settings` already rejects blank values, but stay defensive
    # in case a caller constructs the Settings by hand. App mode needs no
    # GITHUB_TOKEN — the per-request installation token covers API + git.
    has_app = cfg.github_app_id is not None and cfg.github_app_private_key_file is not None
    if cfg.github_token is None and not has_app:
        click.echo("github-proxy: GITHUB_TOKEN or GitHub App credentials required", err=True)
        sys.exit(2)
    app = create_proxy_app(cfg)
    uvicorn.run(
        app,
        host=cfg.github_proxy_bind_host,
        port=cfg.github_proxy_bind_port,
        log_config=None,
    )


if __name__ == "__main__":
    main()
