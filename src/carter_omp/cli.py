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


# trace:v1 id=impl.cli-doctor work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-XM327PK3
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
    click.echo(
        f"allowed repo ids: {sorted(cfg.allowed_repo_ids) or 'NONE'} owners={sorted(cfg.allowed_repo_owners) or 'NONE'}"
    )
    if not cfg.allowed_repo_ids and not cfg.allowed_repo_owners:
        problems.append("no CARTER_OMP_REPO_IDS or CARTER_OMP_REPO_OWNERS configured")
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
    _doctor_models(cfg, omp, problems)
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


# trace:v1 id=impl.cli-doctor-models work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-XM327PK3
def _doctor_models(cfg: Settings, omp: str | None, problems: list[str]) -> None:
    """Verify the OMP model catalog mount and every CARTER_OMP_MODEL selector."""
    import json
    import subprocess
    from pathlib import Path

    catalog = Path.home() / ".omp" / "agent" / "models.container.yml"
    click.echo(f"model catalog: {catalog} {'present' if catalog.is_file() else 'MISSING (run carter-omp init-models)'}")
    if not catalog.is_file():
        problems.append("model catalog missing (run carter-omp init-models)")
        return
    click.echo(f"model pool: {','.join(cfg.model_pool)} thinking={cfg.thinking_level}")
    if omp is None:
        return
    for selector in cfg.model_pool:
        provider = selector.split("/")[0] if "/" in selector else ""
        try:
            proc = subprocess.run(
                [omp, "models", "ls", provider, "--json"] if provider else [omp, "models", "ls", "--json"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            known = {m["selector"] for m in json.loads(proc.stdout).get("models", [])}
        except Exception as exc:
            click.echo(f"model {selector}: unverifiable ({exc})")
            continue
        if selector in known:
            click.echo(f"model {selector}: ok")
        else:
            problems.append(f"model selector unknown to omp: {selector}")
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


# trace:v1 id=impl.cli-policy-explain work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-XM327PK3
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

    # trace:v1 id=impl.cli-init-models work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-XM327PK3


@main.command("init-models")
@click.option("--provider", default=None, help="Provider id (e.g. openrouter, opencode-go, anthropic).")
@click.option("--model", default=None, help="Primary model selector (provider/id form).")
@click.option(
    "--fallback",
    multiple=True,
    help="Fallback model selector, repeatable. Tried in order when the primary fails.",
)
@click.option("--thinking", default="high", help="Thinking level: off, low, medium, high, xhigh, max.")
@click.option("--non-interactive", is_flag=True, help="Fail instead of prompting for missing values.")
def init_models(
    provider: str | None, model: str | None, fallback: tuple[str, ...], thinking: str, non_interactive: bool
) -> None:
    """Generate ~/.omp/agent/models.container.yml and matching .env model settings."""
    import shutil

    omp = shutil.which("omp")
    if omp is None:
        click.echo("omp binary not found on PATH; install OMP first.", err=True)
        sys.exit(2)

    # trace:exempt reason=internal-detail
    def ask(prompt: str, default: str | None = None) -> str:
        if non_interactive:
            if default is None:
                click.echo(f"missing required value for: {prompt}", err=True)
                sys.exit(2)
            return default
        suffix = f" [{default}]" if default else ""
        value = click.prompt(prompt + suffix, default=default or "", show_default=False).strip()
        if not value and default:
            return default
        if not value:
            click.echo("value required.", err=True)
            sys.exit(2)
        return value

    provider = provider or ask("Provider (openrouter, opencode-go, anthropic, ...)", "openrouter")
    provider = provider.strip().lower()
    if not model:
        _list_provider_models(omp, provider)
        model = ask(f"Primary model (e.g. {provider}/<id>)")
    pool = [model.strip()]
    for extra in fallback:
        extra = extra.strip()
        if extra and extra not in pool:
            pool.append(extra)
    if not fallback and not non_interactive and click.confirm("Add a fallback model?", default=True):
        _list_provider_models(omp, provider)
        second = click.prompt("Fallback model (empty to skip)", default="", show_default=False).strip()
        if second and second not in pool:
            pool.append(second)
    thinking = (thinking or "high").strip().lower()
    if thinking not in ("off", "low", "medium", "high", "xhigh", "max"):
        click.echo(f"invalid thinking level: {thinking}", err=True)
        sys.exit(2)

    for selector in pool:
        _verify_selector(omp, selector)

    from pathlib import Path

    target = Path.home() / ".omp" / "agent" / "models.container.yml"
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Generated by `carter-omp init-models`. Selectors only — API keys",
        "# stay wherever OMP already keeps them (env / gateway), never here.",
        f"# Primary: {pool[0]}",
        "models:",
        f"  primary: {pool[0]}",
    ]
    if len(pool) > 1:
        lines.append("  fallbacks:")
        lines.extend(f"    - {selector}" for selector in pool[1:])
    lines.append(f"thinking: {thinking}")
    lines.append("")
    target.write_text("\n".join(lines), encoding="utf-8")
    click.echo(f"wrote {target}")
    click.echo("")
    click.echo("Put this in .env:")
    click.echo(f"CARTER_OMP_MODEL={','.join(pool)}")
    click.echo(f"CARTER_OMP_THINKING={thinking}")
    if provider:
        click.echo(f"# optional: CARTER_OMP_PROVIDER={provider}")


# trace:v1 id=impl.cli-list-provider-models work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-XM327PK3
def _list_provider_models(omp: str, provider: str) -> None:
    """Print up to 40 model selectors for a provider (best-effort)."""
    import json
    import subprocess

    try:
        import os

        env = {k: v for k, v in os.environ.items() if k != "HOME"}
        raw = subprocess.run(
            [omp, "models", "ls", provider, "--json"], capture_output=True, text=True, timeout=30, env=env
        ).stdout
        selectors = [m["selector"] for m in json.loads(raw).get("models", [])][:40]
    except Exception:
        selectors = []
    if selectors:
        click.echo(f"Available {provider} models:")
        for selector in selectors:
            click.echo(f"  {selector}")
    else:
        click.echo(f"(could not list {provider} models; enter a {provider}/<id> selector manually)")


# trace:v1 id=impl.cli-verify-selector work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-XM327PK3
def _verify_selector(omp: str, selector: str) -> None:
    """Fail closed when a selector matches nothing `omp` knows."""
    import json
    import subprocess

    provider = selector.split("/")[0] if "/" in selector else ""
    try:
        import os

        # OMP resolves its model catalog relative to HOME; the wizard must
        # query the operator's real catalog, never a sandboxed HOME override.
        env = {k: v for k, v in os.environ.items() if k != "HOME"}
        proc = subprocess.run(
            [omp, "models", "ls", provider, "--json"] if provider else [omp, "models", "ls", "--json"],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        known = {m["selector"] for m in json.loads(proc.stdout).get("models", [])}
        if proc.returncode != 0 or not known:
            raise ValueError(f"`omp models ls {provider}` returned no usable model list")
    except SystemExit:
        raise
    except Exception as exc:
        click.echo(f"warning: could not verify {selector}: {exc}", err=True)
        return
    if selector not in known:
        click.echo(f"unknown model selector: {selector} (not in `omp models ls {provider}`)", err=True)
        sys.exit(2)
    click.echo(f"verified: {selector}")


if __name__ == "__main__":
    main()
