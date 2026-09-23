"""Command-line interface."""

from __future__ import annotations

import asyncio
import json
import sys

import click
import uvicorn

from carter_omp.config import Settings, get_settings
from carter_omp.db import get_database
from carter_omp.logging_config import configure_logging
from carter_omp.manual_triage import (
    InvalidIssueRef,
    ManualTriageError,
    ManualTriageTimeout,
    await_terminal_state,
    enqueue_manual_triage,
    parse_issue_ref,
)
from carter_omp.proxy_client import GitHubProxyClient
from carter_omp.sandbox import SandboxManager
from carter_omp.server import create_app


def _settings_or_die() -> Settings:
    try:
        return get_settings()
    except Exception as exc:
        click.echo(f"configuration error: {exc}", err=True)
        sys.exit(2)


def _require_proxy_mode(cfg: Settings) -> tuple[str, bytes]:
    if cfg.github_token is not None:
        raise SystemExit(
            "carter_omp orchestrator refuses to start with GITHUB_TOKEN set in env. "
            "The PAT must live only in the github-proxy container."
        )
    if cfg.github_proxy_url is None or cfg.github_proxy_hmac_key is None:
        raise SystemExit(
            "carter_omp orchestrator requires CARTER_OMP_GH_PROXY_URL and "
            "CARTER_OMP_GH_PROXY_HMAC_KEY (run github-proxy in a sibling container)."
        )
    return cfg.github_proxy_url, cfg.github_proxy_hmac_key.get_secret_value().encode("utf-8")


def _build_github(cfg: Settings) -> GitHubProxyClient:
    base_url, key = _require_proxy_mode(cfg)
    return GitHubProxyClient(base_url=base_url, hmac_key=key)


def _default_wait_timeout(cfg: Settings) -> float:
    return cfg.task_timeout_seconds + cfg.task_timeout_hard_grace_seconds + 30.0


@click.group()
def main() -> None:
    """carter-omp control surface."""


@main.command()
def serve() -> None:
    """Run the webhook receiver + worker pool."""
    cfg = _settings_or_die()
    configure_logging(cfg.log_dir)
    cfg.ensure_paths()
    app = create_app(cfg)
    uvicorn.run(app, host=cfg.bind_host, port=cfg.bind_port, log_config=None)


@main.command()
@click.argument("issue_ref")
@click.option(
    "--wait-timeout",
    type=click.FloatRange(min=0.1),
    default=None,
    help="Seconds to wait for a terminal state before returning non-zero (default: task timeout + hard grace + 30).",
)
def triage(issue_ref: str, wait_timeout: float | None) -> None:
    """Fetch a live issue and queue it as if a webhook arrived.

    ISSUE_REF is `owner/repo#NN`.
    """
    cfg = _settings_or_die()
    configure_logging(cfg.log_dir)
    cfg.ensure_paths()
    try:
        repo_full, number = parse_issue_ref(issue_ref)
    except InvalidIssueRef as exc:
        click.echo(str(exc), err=True)
        sys.exit(2)
    if not cfg.allows(repo_full):
        click.echo(f"refusing: {repo_full} not in CARTER_OMP_REPO_ALLOWLIST", err=True)
        sys.exit(2)

    async def _go() -> None:
        github = _build_github(cfg)
        db = get_database(cfg.sqlite_path)
        try:
            delivery = await enqueue_manual_triage(
                db=db,
                github=github,
                repo_full=repo_full,
                number=number,
            )
        except ManualTriageError as exc:
            click.echo(f"refusing: {exc}", err=True)
            sys.exit(2)
        # The dispatcher loop lives in the long-running `serve` process; we
        # only watch the row land in a terminal state. Wake latency is
        # bounded by `WorkerPool._dispatch_loop`'s 10s `_wakeup.wait()` fallback.
        click.echo(json.dumps({"delivery": delivery, "state": "queued"}, indent=2))
        timeout = wait_timeout if wait_timeout is not None else _default_wait_timeout(cfg)
        try:
            final = await await_terminal_state(db, delivery, timeout=timeout)
        except ManualTriageTimeout as exc:
            click.echo(
                json.dumps(
                    {"delivery": delivery, "state": exc.state, "timed_out": True, "error": str(exc)},
                    indent=2,
                ),
                err=True,
            )
            sys.exit(1)
        if final is None:
            click.echo(json.dumps({"delivery": delivery, "state": "missing"}, indent=2))
            return
        click.echo(
            json.dumps(
                {"delivery": delivery, "state": final.state, "error": final.last_error},
                indent=2,
            )
        )

    asyncio.run(_go())


@main.command()
@click.argument("delivery_id")
@click.option(
    "--wait-timeout",
    type=click.FloatRange(min=0.1),
    default=None,
    help="Seconds to wait for a terminal state before returning non-zero (default: task timeout + hard grace + 30).",
)
def replay(delivery_id: str, wait_timeout: float | None) -> None:
    """Re-enqueue a stored event so the running `serve` pool can pick it up."""
    cfg = _settings_or_die()
    configure_logging(cfg.log_dir)
    cfg.ensure_paths()
    db = get_database(cfg.sqlite_path)
    row = db.get_event(delivery_id)
    if row is None:
        click.echo(f"unknown delivery: {delivery_id}", err=True)
        sys.exit(2)
    if row.state == "skipped" or "_carter_omp_trigger" not in row.payload:
        click.echo(f"delivery {delivery_id} was never authorized; replay refused", err=True)
        sys.exit(2)
    if not db.requeue_event(delivery_id, from_states=("done", "failed")):
        click.echo(
            f"delivery {delivery_id} is {row.state}; only done/failed events can be replayed",
            err=True,
        )
        sys.exit(2)

    async def _wait() -> None:
        timeout = wait_timeout if wait_timeout is not None else _default_wait_timeout(cfg)
        try:
            final = await await_terminal_state(db, delivery_id, timeout=timeout)
        except ManualTriageTimeout as exc:
            click.echo(
                json.dumps(
                    {"delivery": delivery_id, "state": exc.state, "timed_out": True, "error": str(exc)},
                    indent=2,
                ),
                err=True,
            )
            sys.exit(1)
        if final is None:
            click.echo(json.dumps({"delivery": delivery_id, "state": "missing"}, indent=2))
            return
        click.echo(
            json.dumps(
                {"delivery": delivery_id, "state": final.state, "error": final.last_error},
                indent=2,
            )
        )

    asyncio.run(_wait())


@main.command()
def status() -> None:
    """Dump issue and release state."""
    cfg = _settings_or_die()
    cfg.ensure_paths()
    db = get_database(cfg.sqlite_path)
    issue_rows = db.list_issues()
    for row in issue_rows:
        click.echo(
            f"{row.key:<40} state={row.state:<12} pr={row.pr_number or '-'} "
            f"branch={row.branch or '-'} updated={row.updated_at}"
        )
    release_rows = db.list_releases()
    if release_rows:
        if issue_rows:
            click.echo()
        click.echo("Releases:")
    for row in release_rows:
        error = f" error={row.last_error}" if row.last_error else ""
        click.echo(
            f"{row.key:<40} state={row.state:<12} rounds={row.rounds:<2} "
            f"sha={row.current_sha[:12]} updated={row.updated_at}{error}"
        )


@main.command()
@click.argument("issue_key")
def cleanup(issue_key: str) -> None:
    """Force-remove the workspace for an issue (does not touch the remote)."""
    cfg = _settings_or_die()
    cfg.ensure_paths()
    db = get_database(cfg.sqlite_path)
    row = db.get_issue(issue_key)
    if row is None:
        click.echo(f"unknown issue: {issue_key}", err=True)
        sys.exit(2)
    sandbox = SandboxManager(cfg.workspace_root)
    sandbox.remove_workspace(repo=row.repo, number=row.number)
    db.set_issue_state(issue_key, "abandoned")
    click.echo(f"cleaned up {issue_key}")


@main.command()
def doctor() -> None:
    """Verify DB, OMP binary, config identity, proxy channel, and dashboard bundle."""
    import shutil
    import sqlite3

    cfg = _settings_or_die()
    problems: list[str] = []

    try:
        cfg.ensure_paths()
        conn = sqlite3.connect(cfg.sqlite_path)
        conn.execute("SELECT 1")
        conn.close()
        click.echo(f"db writable: {cfg.sqlite_path}")
    except Exception as exc:
        problems.append(f"db: {exc}")
    omp = shutil.which(cfg.omp_command)
    click.echo(f"omp binary: {omp or 'NOT FOUND'}")
    if omp is None:
        problems.append("omp binary not found on PATH")
    click.echo(f"trigger mode: {cfg.trigger_mode} label={cfg.trigger_label}")
    click.echo(f"authorized user ids: {sorted(cfg.authorized_user_ids) or 'NONE (refuses to start)'}")
    if not cfg.authorized_user_ids:
        problems.append("no CARTER_OMP_AUTHORIZED_USER_IDS configured")
    click.echo(f"allowed repo ids: {sorted(cfg.allowed_repo_ids) or 'NONE (refuses to start)'}")
    if not cfg.allowed_repo_ids:
        problems.append("no CARTER_OMP_REPO_IDS configured")
    if cfg.github_app_id is not None:
        click.echo(f"github app id: {cfg.github_app_id} installation={cfg.github_installation_id}")
        key_file = cfg.github_app_private_key_file
        if key_file is None or not key_file.is_file():
            problems.append("github app private key file missing")
        else:
            import stat as _stat

            mode = _stat.S_IMODE(key_file.stat().st_mode)
            click.echo(f"app key file: {key_file} mode={oct(mode)}")
            if mode & 0o077:
                problems.append(f"app private key file too permissive ({oct(mode)}; want 0400)")
    elif cfg.github_token is None:
        click.echo("credential: github-proxy holds PAT or App key (orchestrator holds neither)")
    if cfg.github_proxy_url is None or cfg.github_proxy_hmac_key is None:
        problems.append("proxy channel not configured")
    else:
        click.echo(f"proxy: {cfg.github_proxy_url}")
    from carter_omp.dashboard import static_dir

    index = static_dir() / "index.html"
    click.echo(f"dashboard: {'present' if index.exists() else 'MISSING'} ({index})")
    if not index.exists():
        problems.append("dashboard bundle missing (run yarn --cwd=web build)")
    if problems:
        for p in problems:
            click.echo(f"FAIL: {p}", err=True)
        sys.exit(1)
    click.echo("doctor: ok")


@main.group()
def auth() -> None:
    """Authorization helpers."""


@auth.command("check")
def auth_check() -> None:
    """Print the configured immutable authorization identity."""
    cfg = _settings_or_die()
    click.echo(f"authorized user ids: {sorted(cfg.authorized_user_ids)}")
    click.echo(f"authorized logins (readability only): {sorted(cfg.authorized_logins)}")
    click.echo(f"allowed repo ids: {sorted(cfg.allowed_repo_ids)}")
    click.echo(f"allowed repo names (readability only): {sorted(cfg.allowed_repo_names)}")
    click.echo(f"installation id: {cfg.github_installation_id}")


@main.group()
def policy() -> None:
    """Policy helpers."""


@policy.command("explain")
@click.argument("delivery_id")
def policy_explain(delivery_id: str) -> None:
    """Print why a webhook delivery was or was not admitted (no OMP run)."""
    cfg = _settings_or_die()
    cfg.ensure_paths()
    db = get_database(cfg.sqlite_path)
    row = db.get_event(delivery_id)
    if row is None:
        click.echo(f"unknown delivery: {delivery_id}", err=True)
        sys.exit(2)
    from carter_omp.github_events import TriggerContext

    raw = row.payload.get("_carter_omp_trigger")
    click.echo(f"delivery: {row.delivery_id} event={row.event_type} state={row.state}")
    click.echo(f"reason: {row.last_error}")
    if isinstance(raw, dict):
        try:
            trigger = TriggerContext.from_record(raw)
        except Exception as exc:
            click.echo(f"trigger record unreadable: {exc}", err=True)
            sys.exit(2)
        click.echo(f"actor: {trigger.actor_login} ({trigger.actor_id}) via {trigger.trigger_kind}")
        click.echo(f"capabilities: {sorted(c.value for c in trigger.capabilities)}")
        click.echo(f"policy: {trigger.policy_version} at {trigger.authorized_at.isoformat()}")
    else:
        click.echo("trigger: none (event was never authorized)")


if __name__ == "__main__":
    main()
