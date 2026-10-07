"""Per-task RpcClient driver.

The orchestrator calls `run_task(...)` from within an asyncio loop. The
function spins up `RpcClient` on a worker thread, drives the kickoff/follow-up
prompt, and returns when the agent emits `agent_end`.

Host tools call back into the orchestrator's GitHub client and DB. Because the
RpcClient runs in its own subprocess and the host-tool callbacks are dispatched
on the RpcClient's stdout-reader thread, the callbacks block until coroutines
scheduled onto the parent loop complete (`asyncio.run_coroutine_threadsafe`).
"""

from __future__ import annotations

import asyncio
import grp
import json
import logging
import os
import shutil
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omp_rpc import (
    HostToolCompletedEvent,
    MessageUpdateEvent,
    RpcClient,
    RpcError,
    RpcProcessExitError,
    ToolExecutionEndEvent,
)

from carter_omp import host_tools, persona, pragmas
from carter_omp.cancellation import register_cancel_hook, unregister_cancel_hook
from carter_omp.capabilities import OMP_BUILTIN_TOOLS, capabilities_for
from carter_omp.config import Settings
from carter_omp.db import Database, issue_key
from carter_omp.git_ops import DirtyState, inspect_dirty_state
from carter_omp.github_backend import GitHubBackend
from carter_omp.github_client import CommentInfo, IssueInfo, PullRequestInfo, RepoInfo
from carter_omp.host_tools import AbortController, ReleaseToolContext, ToolBindings, _git_identity_env
from carter_omp.natives_cache import NativesCache
from carter_omp.natives_cache import compute_key as natives_compute_key
from carter_omp.sandbox import GitTransport, Workspace, _prepare_slot_runtime_env, _safe_directory_env

log = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class ReleaseTaskContext:
    """Release verdict and failure context supplied to an agent round."""

    tag: str
    version: str
    round: int
    max_rounds: int
    head_sha: str
    default_branch: str
    failures_text: str
    run_urls: tuple[str, ...]


@dataclass(slots=True)
class TaskInputs:
    """Common context shared by every task type."""

    settings: Settings
    db: Database
    github: GitHubBackend
    git_transport: GitTransport
    repo: RepoInfo
    workspace: Workspace
    delivery_id: str
    attempts: int = 0
    slot_uid: int | None = None
    natives_cache: NativesCache | None = None
    issue: IssueInfo | None = None
    release: ReleaseTaskContext | None = None
    trigger: object | None = None


@dataclass(slots=True, frozen=True)
class ThreadMessage:
    """One entry in the conversation a directive carries to the agent."""

    kind: str  # issue_body | pr_body | comment | review_comment | review
    author: str
    body: str
    created_at: str
    path: str | None = None  # review_comment only
    line: int | None = None  # review_comment only
    state: str | None = None  # review only (APPROVED / CHANGES_REQUESTED / COMMENTED)


@dataclass(slots=True, frozen=True)
class DirectiveInfo:
    """A maintainer's `@bot` mention captured as an authoritative instruction.

    `thread` is the full conversation context (issue/PR body + every prior
    comment + every review) up to the moment the directive fired.
    """

    body: str
    author: str
    thread: tuple[ThreadMessage, ...] = ()
    pragmas: tuple[tuple[str, str], ...] = ()
    authorizes_impl: bool = False


def _resolve_pragma_overrides(
    directive: DirectiveInfo | None,
    settings: Settings,
) -> tuple[str | None, pragmas.ThinkingLevel | None]:
    """Return `(model_override, thinking_override)` for the current directive.

    `None` for either means "no override, use the settings default". Aliases
    that don't match anything in the pool / level set are dropped (caller logs
    the discard at the callsite that has access to issue_key).
    """
    if directive is None or not directive.pragmas:
        return None, None
    model_value = pragmas.pragma_value(directive.pragmas, "model")
    thinking_value = pragmas.pragma_value(directive.pragmas, "thinking")
    model_override = pragmas.resolve_model_alias(model_value, settings.model_pool) if model_value else None
    thinking_override = pragmas.resolve_thinking_level(thinking_value) if thinking_value else None
    return model_override, thinking_override


_SCRUBBED_ENV_KEYS: tuple[str, ...] = (
    # Secrets that MUST NOT reach the agent subprocess; an agent with the
    # `bash` tool could otherwise `printenv` them out of carter-omp's env.
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "GITHUB_APP_PRIVATE_KEY",
    "GITHUB_APP_CLIENT_SECRET",
    "GITHUB_WEBHOOK_SECRET",
    "CARTER_OMP_REPLAY_TOKEN",
    "CARTER_OMP_GH_PROXY_HMAC_KEY",
    "CARTER_OMP_GITHUB_APP_PRIVATE_KEY",
    "SSH_AUTH_SOCK",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "AZURE_CLIENT_SECRET",
    # NOTE: OPENCODE_API_KEY is intentionally NOT scrubbed: with no host-side
    # auth gateway, it is the agent OMP's provider credential (see compose).
    # OPENROUTER_API_KEY is the same for the cross-provider fallback chain.
)

# Prefixes scrubbed dynamically (cloud credential families).
_SCRUBBED_ENV_PREFIXES: tuple[str, ...] = ("AWS_", "GCP_", "AZURE_", "GOOGLE_")

_AGENT_HOME = Path("/srv/agent-home")
_AGENT_HOME_STAGE = Path("/srv/agent-home-stage")


def _stage_agent_home() -> None:
    """Copy late-appearing staged agent config into the runtime HOME."""
    if not _AGENT_HOME_STAGE.exists():
        return

    for rel in (Path(".agent"), Path(".omp/agent")):
        src = _AGENT_HOME_STAGE / rel
        if not src.exists():
            continue

        dst = _AGENT_HOME / rel
        try:
            if os.path.lexists(dst):
                if dst.is_dir() and not dst.is_symlink():
                    shutil.rmtree(dst)
                else:
                    dst.unlink()
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src, dst, dirs_exist_ok=True)
        except OSError as exc:
            log.warning("Failed to stage agent home path %s: %s", rel, exc)

    if not _AGENT_HOME.exists():
        return

    chown_to_root = os.geteuid() == 0
    for root, dirs, files in os.walk(_AGENT_HOME):
        root_path = Path(root)
        if root_path == _AGENT_HOME / ".omp":
            # ~/.omp/run is slot-writable daemon presence state, not template
            # config; keep it out of the read-only normalization below.
            dirs[:] = [d for d in dirs if d != "run"]
        try:
            root_path.chmod(0o755)
            if chown_to_root:
                os.chown(root_path, 0, 0)
        except OSError as exc:
            log.warning("Failed to normalize agent home directory %s: %s", root_path, exc)

        for name in dirs:
            path = root_path / name
            try:
                path.chmod(0o755)
                if chown_to_root:
                    os.chown(path, 0, 0)
            except OSError as exc:
                log.warning("Failed to normalize agent home directory %s: %s", path, exc)

        for name in files:
            path = root_path / name
            try:
                path.chmod(0o644)
                if chown_to_root:
                    os.chown(path, 0, 0)
            except OSError as exc:
                log.warning("Failed to normalize agent home file %s: %s", path, exc)


# trace:v1 id=impl.worker-run-dir work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-9N23MPRP
def _ensure_agent_run_dir() -> None:
    """Keep ``~/.omp/run`` writable by every sandbox slot.

    omp registers daemon project presence under ``~/.omp/run`` at startup,
    nesting per-project dirs (``daemons/<hash>/clients``) that any slot user
    must be able to create or enter regardless of which slot made them first.
    The tree stays group ``omp``, setgid, group-writable; slot subprocesses
    spawn with umask 0002 so their entries inherit group write.
    """
    if os.geteuid() != 0:
        return
    run_dir = _AGENT_HOME / ".omp" / "run"
    try:
        gid = grp.getgrnam("omp").gr_gid
    except KeyError:
        return
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        for root, dirs, files in os.walk(run_dir):
            root_path = Path(root)
            os.chown(root_path, -1, gid)
            root_path.chmod(0o2770)
            for name in dirs + files:
                child = root_path / name
                if child.is_dir() and not child.is_symlink():
                    os.chown(child, -1, gid)
                    child.chmod(0o2770)
                elif child.is_file():
                    os.chown(child, -1, gid)
                    child.chmod(0o660)
    except OSError as exc:
        log.warning("Failed to prepare agent run dir %s: %s", run_dir, exc)


# trace:v1 id=impl.worker-fallback-chains work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-9N23MPRP
def _write_fallback_chains(settings: Settings) -> Path | None:
    """Materialize the OMP `retry.fallbackChains` overlay from settings.

    OMP walks a fallback chain when a turn's model fails with a retryable
    provider error (dead provider, quota exhaustion), so a configured
    OpenRouter chain keeps the task alive instead of failing the delivery.
    The `default` key catches every active model, whatever the pool or a
    pragma picked.

    Returns the overlay path, or None when unset/unwritable (no `--config`).
    """
    chain = settings.fallback_models
    if not chain:
        return None
    rendered = ", ".join(json.dumps(selector) for selector in chain)
    body = (
        "# Generated by carter-omp from CARTER_OMP_FALLBACK_MODEL.\n"
        "# OMP overlay (`--config`): retry recovery across providers.\n"
        "retry:\n"
        "  fallbackChains:\n"
        "    default: ["
        f"{rendered}]\n"
    )
    path = _AGENT_HOME / ".omp" / "agent" / "carter-omp-fallback.yml"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.read_text(encoding="utf-8") != body:
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(body, encoding="utf-8")
            os.chmod(tmp, 0o644)
            os.replace(tmp, path)
    except OSError as exc:
        log.warning("fallback chain overlay unwritable", extra={"path": str(path), "err": str(exc)[:160]})
        return None
    return path


# trace:v1 id=impl.worker-agent-env work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-9N23MPRP
def _build_extra_env(settings: Settings) -> dict[str, str]:
    """Build the env overlay passed to the omp subprocess.

    `omp_rpc` merges this dict on top of `os.environ`, so overlaying empty
    strings for the sensitive keys is what actually masks them in the
    child — `del` on the parent's env would not help us here.
    """
    del settings  # kept for future hooks (model-specific env, etc.)
    _stage_agent_home()
    _ensure_agent_run_dir()
    env = dict.fromkeys(_SCRUBBED_ENV_KEYS, "")
    for key in os.environ:
        if key.startswith(_SCRUBBED_ENV_PREFIXES):
            env[key] = ""
    # Usage attribution: the pi-native gateway transport forwards this label
    # (x-omp-app) so broker-side per-client burn tracking shows `carter_omp`
    # instead of an anonymous gateway client.
    env["OMP_APP_NAME"] = "carter_omp"
    # Repo lifecycle scripts must not touch SHARED git metadata: husky's
    # `prepare` runs `git config core.hooksPath`, which rewrites the pool's
    # `.git/config` as the invoking slot's uid:gid and locks every other slot
    # out of that repo mid-run (`fatal: unable to access '.git/config'`).
    # Git hooks are useless in a sandboxed worktree anyway.
    env["HUSKY"] = "0"
    env["HUSKY_SKIP_INSTALL"] = "1"
    # omp's bash tool allocates a PTY by default. In this harness the child
    # occasionally dies mid-turn with `EPIPE: broken pipe, write` from Bun's
    # stream teardown (observed right after bash/edit tool calls, exit 1, no
    # cancel or timeout involved), which fails the event and re-runs the whole
    # task. Nothing here is interactive, so run bash without a PTY — omp's own
    # escape hatch for non-interactive/daemon contexts.
    env["PI_NO_PTY"] = "1"
    if _AGENT_HOME.is_dir():
        env["HOME"] = str(_AGENT_HOME)
    return env


_TERMINAL_TRIAGE_TOOLS: frozenset[str] = frozenset({"gh_open_pr", "mark_unable_to_reproduce", "abort_task"})
_TERMINAL_REVIEW_TOOLS: frozenset[str] = frozenset({"submit_pr_review", "abort_task"})
_TERMINAL_RELEASE_TOOLS: frozenset[str] = frozenset({"release_retag", "abort_task"})
# A mention's deliverable *is* the reply: without it the human is left with a
# run that consumed tokens and said nothing (see the 2026-10-07 run on
# personal_website#64, which ended after todo/bash calls with no comment).
_TERMINAL_COMMENT_TOOLS: frozenset[str] = frozenset({"gh_post_comment", "abort_task"})
_PR_REQUIRING_CLASSIFICATIONS: frozenset[str] = frozenset({"bug", "documentation"})


def _task_timeout(settings: Settings, task_kind: str) -> float:
    """Return the turn timeout for a task kind."""
    return settings.release_task_timeout_seconds if task_kind == "handle_release_ci" else settings.task_timeout_seconds


# trace:v1 id=impl.worker-needs-completion-reminder work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-9N23MPRP
def _needs_completion_reminder(
    *,
    task_kind: str,
    inputs: TaskInputs,
    bindings: ToolBindings,
    tools_called: set[str],
) -> bool:
    """True iff a task turn ended before reaching its terminal tool."""
    if bindings.abort is not None and bindings.abort.triggered:
        return False
    if task_kind == "review_pr":
        return not (tools_called & _TERMINAL_REVIEW_TOOLS)
    if task_kind == "handle_release_ci":
        return not (tools_called & _TERMINAL_RELEASE_TOOLS)
    if task_kind == "handle_comment":
        return not (tools_called & _TERMINAL_COMMENT_TOOLS)
    if task_kind != "triage_issue":
        return False
    row = inputs.db.get_issue(bindings.issue_key)
    if row is None or row.classification not in _PR_REQUIRING_CLASSIFICATIONS:
        return False
    return not (tools_called & _TERMINAL_TRIAGE_TOOLS)


def _probe_workspace_dirty(workspace: Workspace, slot_uid: int | None) -> DirtyState:
    """Return the workspace's dirty state, swallowing inspection errors.

    `inspect_dirty_state` already swallows individual git failures, but the
    function itself can still raise if e.g. the workspace was wiped between
    `_drive_turn` and the post-turn check. The reminder loop treats any
    exception as "clean enough" so a corrupted workspace doesn't pin the
    agent in a reminder loop.
    """
    try:
        return inspect_dirty_state(
            workspace.repo_dir,
            slot_uid=slot_uid,
            safe_directory=workspace.repo_dir,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort post-turn probe
        log.debug("workspace dirty probe failed", extra={"error": str(exc)})
        return DirtyState(uncommitted=0, unpushed=0, summary="")


# trace:v1 id=impl.worker-drive-turn work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-9N23MPRP
def _drive_turn(
    client: RpcClient,
    initial_prompt: str,
    *,
    task_kind: str,
    inputs: TaskInputs,
    bindings: ToolBindings,
    tools_called: set[str],
) -> Any:
    """Run the initial prompt and, if the agent stopped early, send reminders.

    Returns the final `Turn` (last `prompt_and_wait` result), or `None` when
    the agent intentionally pulled the plug via `abort_task`.
    """
    settings = inputs.settings
    max_reminders = settings.task_completion_max_reminders

    def _run(prompt: str) -> Any:
        try:
            return client.prompt_and_wait(prompt, timeout=_task_timeout(settings, task_kind))
        except (RpcError, RpcProcessExitError):
            # Did the agent intentionally pull the plug via `abort_task`?
            # If so, swallow — the abort path is a clean exit, not a
            # failure that should surface in the dashboard or trigger
            # a comment to the reporter. Anything else propagates.
            if bindings.abort is not None and bindings.abort.triggered:
                log.info(
                    "rpc_aborted_by_tool",
                    extra={"issue": bindings.issue_key, "task": task_kind, "reason": bindings.abort.reason},
                )
                return None
            raise

    turn = _run(initial_prompt)
    if turn is None:
        return None

    reminders_used = 0
    while reminders_used < max_reminders:
        needs_completion = _needs_completion_reminder(
            task_kind=task_kind, inputs=inputs, bindings=bindings, tools_called=tools_called
        )
        if not needs_completion:
            if task_kind == "review_pr":
                break
            dirty = _probe_workspace_dirty(inputs.workspace, inputs.slot_uid)
            if not dirty.is_dirty:
                break
        reminders_used += 1
        if needs_completion:
            log.warning(
                "rpc_completion_reminder",
                extra={
                    "issue": bindings.issue_key,
                    "task": task_kind,
                    "attempt": reminders_used,
                    "max": max_reminders,
                },
            )
            if task_kind == "handle_release_ci":
                assert inputs.release is not None
                reminder = persona.followup_release(
                    repo=inputs.repo,
                    release=inputs.release,
                    workspace=inputs.workspace,
                )
            elif task_kind == "review_pr":
                assert inputs.issue is not None
                reminder = persona.review_completion_reminder(
                    repo=inputs.repo,
                    issue=inputs.issue,
                    workspace=inputs.workspace,
                )
            elif task_kind == "handle_comment":
                assert inputs.issue is not None
                reminder = persona.comment_completion_reminder(
                    repo=inputs.repo,
                    issue=inputs.issue,
                    workspace=inputs.workspace,
                )
            else:
                assert inputs.issue is not None
                reminder = persona.completion_reminder(
                    repo=inputs.repo,
                    issue=inputs.issue,
                    workspace=inputs.workspace,
                )
        else:
            assert dirty is not None
            log.warning(
                "rpc_dirty_state_reminder",
                extra={
                    "issue": bindings.issue_key,
                    "task": task_kind,
                    "attempt": reminders_used,
                    "max": max_reminders,
                    "uncommitted": dirty.uncommitted,
                    "unpushed": dirty.unpushed,
                },
            )
            if task_kind == "handle_release_ci":
                assert inputs.release is not None
                reminder = persona.followup_release(
                    repo=inputs.repo,
                    release=inputs.release,
                    workspace=inputs.workspace,
                )
            else:
                assert inputs.issue is not None
                reminder = persona.dirty_state_reminder(
                    repo=inputs.repo,
                    issue=inputs.issue,
                    workspace=inputs.workspace,
                    dirty=dirty,
                )
        next_turn = _run(reminder)
        if next_turn is None:
            return None
        turn = next_turn

    if reminders_used and _needs_completion_reminder(
        task_kind=task_kind, inputs=inputs, bindings=bindings, tools_called=tools_called
    ):
        log.warning(
            "rpc_completion_unfinished",
            extra={
                "issue": bindings.issue_key,
                "task": task_kind,
                "reminders": reminders_used,
                "tools_called": sorted(tools_called),
            },
        )
    if reminders_used and task_kind != "review_pr":
        final_dirty = _probe_workspace_dirty(inputs.workspace, inputs.slot_uid)
        if final_dirty.is_dirty:
            log.warning(
                "rpc_dirty_state_unfinished",
                extra={
                    "issue": bindings.issue_key,
                    "task": task_kind,
                    "reminders": reminders_used,
                    "uncommitted": final_dirty.uncommitted,
                    "unpushed": final_dirty.unpushed,
                },
            )
    return turn


def _has_prior_session(session_dir: Path) -> bool:
    """Return True iff `session_dir` already contains an omp JSONL transcript.

    pi's `coding-agent` writes one `*.jsonl` per session into `--session-dir`.
    The presence of any such file is the signal that `--continue` will pick
    up the most recent transcript (`SessionManager.continueRecent`) rather
    than starting fresh.
    """
    try:
        return any(session_dir.glob("*.jsonl"))
    except OSError:
        return False


# trace:v1 id=impl.worker-run-token-ttl work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-9N23MPRP
def _run_token_ttl(settings: Settings) -> int:
    """Seconds a per-run proxy token must stay valid.

    The token is minted once per run and used by every mutation in it, so it
    has to outlive the whole task budget — not one round-trip. An expired
    token 401s every push/comment/PR for the rest of the run, which is how a
    run can end with the work committed and nothing published.
    """
    return int(
        max(settings.task_timeout_seconds, settings.release_task_timeout_seconds)
        + settings.task_timeout_hard_grace_seconds
        + 300.0
    )


# trace:v1 id=impl.worker-attach-token work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-9N23MPRP
def _attach_run_token(inputs: TaskInputs, bindings: ToolBindings) -> None:
    """Mint a per-run proxy token and scope the GitHub client to it.

    ToolBindings is frozen, so the scoped clients are attached via
    object.__setattr__. Without a TriggerContext (legacy/manual paths) the
    shared HMAC-only clients are left untouched.

    The mint happens *before* the agent runs, but the run's identity does not
    exist yet: `classify_issue(branch_slug=…)` renames the workspace branch and
    the run opens its own PR later. The proxy pins both, so a token minted once
    would 403 the run's own push/PR/review-request (issue #14). `bindings
    .refresh_run_token` re-mints from the live branch/PR and re-scopes both
    clients; the tools that change either call it.
    """
    from carter_omp.github_events import TriggerContext
    from carter_omp.proxy_client import GitHubProxyClient, ProxyGitTransport
    from carter_omp.run_token import mint_run_token

    trigger = inputs.trigger
    if not isinstance(trigger, TriggerContext):
        return
    key = inputs.settings.github_proxy_hmac_key
    if key is None:
        return
    thread = trigger.pull_request_number if trigger.pull_request_number is not None else trigger.issue_number
    secret = key.get_secret_value().encode("utf-8")
    capabilities = frozenset(c.value for c in trigger.capabilities)
    ttl_seconds = _run_token_ttl(inputs.settings)
    # Live claims, not a snapshot: `refresh` mutates these and re-mints.
    claims: dict[str, str | int | None] = {
        "branch": inputs.workspace.branch,
        "pull_request": trigger.pull_request_number,
    }

    # trace:v1 id=impl.worker-run-token-apply work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-9N23MPRP
    def _apply(token: str) -> None:
        if isinstance(inputs.github, GitHubProxyClient):
            object.__setattr__(bindings, "github", inputs.github.with_run_token(token))
        if isinstance(inputs.git_transport, ProxyGitTransport):
            object.__setattr__(bindings, "git_transport", inputs.git_transport.with_run_token(token))

    # trace:v1 id=impl.worker-run-token-mint work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-9N23MPRP
    def mint() -> str:
        return mint_run_token(
            key=secret,
            run_id=trigger.run_id,
            repo_id=trigger.repository_id,
            repo=trigger.repository_full_name,
            issue=thread,
            pull_request=claims["pull_request"] if isinstance(claims["pull_request"], int) else None,
            workspace=None,
            branch=claims["branch"] if isinstance(claims["branch"], str) else None,
            capabilities=capabilities,
            ttl_seconds=ttl_seconds,
        )

    # trace:v1 id=impl.worker-run-token-refresh work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-9N23MPRP
    def refresh_run_token(branch: str | None, pull_request: int | None) -> None:
        """Re-mint with the run's current branch/PR and re-scope both clients."""
        if branch is not None:
            claims["branch"] = branch
        if pull_request is not None:
            claims["pull_request"] = pull_request
        _apply(mint())

    _apply(mint())
    object.__setattr__(bindings, "refresh_run_token", refresh_run_token)


# trace:v1 id=impl.worker-pickup-ack work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-9N23MPRP
async def _ack_pickup_reaction(inputs: TaskInputs, bindings: ToolBindings) -> None:
    """Eyes-react to the trigger through the run-token channel.

    Runs after `_attach_run_token`, so `bindings.github` already carries the
    per-run token the proxy's reaction endpoints require. Mention triggers
    react to the comment; label triggers react to the issue itself.
    Best-effort UX: failures are swallowed — admission already happened.
    """
    from carter_omp.github_events import TriggerContext

    trigger = inputs.trigger
    if not isinstance(trigger, TriggerContext):
        return
    try:
        if trigger.trigger_kind == "mention" and trigger.trigger_object_id is not None:
            await bindings.github.add_comment_reaction(trigger.repository_full_name, trigger.trigger_object_id, "eyes")
        elif trigger.trigger_kind in ("label", "assign"):
            number = trigger.issue_number or trigger.pull_request_number
            if number is None:
                return
            await bindings.github.add_issue_reaction(trigger.repository_full_name, number, "eyes")
    except Exception as exc:
        log.debug("pickup ack failed", extra={"delivery": inputs.delivery_id, "err": str(exc)[:120]})


# trace:v1 id=impl.worker-build-prompt work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-BKNZHMZ0
def _build_prompt(
    task_kind: str,
    inputs: TaskInputs,
    *,
    comment: CommentInfo | None,
    pr_number: int | None,
    review_payload: dict[str, Any] | None,
    pr: PullRequestInfo | None = None,
    directive: DirectiveInfo | None = None,
    thread: tuple[ThreadMessage, ...] = (),
    resuming: bool = False,
) -> str:
    if task_kind == "handle_release_ci":
        assert inputs.release is not None
        renderer = persona.followup_release if resuming else persona.kickoff_release
        return renderer(repo=inputs.repo, release=inputs.release, workspace=inputs.workspace)
    if task_kind == "triage_issue":
        assert inputs.issue is not None
        if resuming:
            return persona.resume_triage(repo=inputs.repo, issue=inputs.issue, workspace=inputs.workspace)
        if directive is not None:
            return persona.kickoff_directive(
                repo=inputs.repo,
                issue=inputs.issue,
                workspace=inputs.workspace,
                directive=directive,
            )
        return persona.kickoff(repo=inputs.repo, issue=inputs.issue, workspace=inputs.workspace, thread=thread)
    if task_kind == "review_pr":
        assert inputs.issue is not None
        assert pr is not None
        return persona.kickoff_pr_review(repo=inputs.repo, pr=pr, workspace=inputs.workspace)
    if task_kind == "handle_comment":
        assert inputs.issue is not None
        assert comment is not None
        issue_row = inputs.db.get_issue(issue_key(inputs.repo.full_name, inputs.issue.number))
        if issue_row is None:
            pr_status = "no PR opened yet"
        elif issue_row.pr_number is None:
            pr_status = "no PR opened yet"
        elif issue_row.state == "merged":
            pr_status = f"PR #{issue_row.pr_number} was merged"
        elif issue_row.state in ("closed", "abandoned"):
            pr_status = f"PR #{issue_row.pr_number} was closed without merge"
        else:
            pr_status = f"PR #{issue_row.pr_number} is open"
        if directive is not None:
            return persona.directive(
                repo=inputs.repo,
                issue=inputs.issue,
                workspace=inputs.workspace,
                comment=comment,
                directive=directive,
                pr_status=pr_status,
                pr_number=pr_number,
            )
        return persona.followup_comment(
            repo=inputs.repo,
            issue=inputs.issue,
            workspace=inputs.workspace,
            comment=comment,
            pr_status=pr_status,
            pr_number=pr_number,
            thread=thread,
        )
    if task_kind == "handle_review":
        assert review_payload is not None
        path = str(review_payload.get("path") or "")
        start = review_payload.get("start_line") or review_payload.get("line")
        end = review_payload.get("line") or review_payload.get("original_line")
        if isinstance(start, int) and isinstance(end, int) and start != end:
            line_range = f":L{start}-L{end}"
        elif isinstance(end, int):
            line_range = f":L{end}"
        else:
            line_range = ""
        body = str(review_payload.get("body") or "")
        author = str(review_payload.get("author") or "")
        return persona.followup_review(
            repo=inputs.repo,
            workspace=inputs.workspace,
            pr_number=int(pr_number or 0),
            comment_author=author,
            comment_body=body,
            comment_path=path,
            comment_line_range=line_range,
        )
    raise ValueError(f"unknown task kind: {task_kind!r}")


# trace:v1 id=impl.worker-agent-stalled work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-9N23MPRP
class AgentStalledError(TimeoutError):
    """The agent stopped emitting events for longer than the silence budget.

    A `TimeoutError` subclass so callers that already treat a timed-out turn as
    transient keep working — but the message names the silence, because a bare
    "timed out waiting for agent_end" reads as "still working" when in fact
    nothing had happened for half an hour.
    """


# trace:v1 id=impl.worker-agent-activity-watch work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-9N23MPRP
class _AgentActivityWatch:
    """Restartable deadline on agent *silence*.

    `prompt_and_wait` bounds the whole turn; nothing bounded silence, so a hung
    provider stream (no message deltas, no tool events) burned the full task
    budget and each retry repeated it — 2026-10-07, an issue comment on
    `carterlasalle/scc#21` spent four 40-minute attempts, ~32 of those minutes
    silent, and the traceback only ever said `Timed out waiting for
    agent_end`. Every agent event re-arms the watch; on expiry the turn is
    stopped and the failure names how long the silence lasted and what the
    last event was.

    `clock` and `interval` are injectable and `check()` is the whole loop body,
    so the behaviour is testable without sleeping; `seconds <= 0` disables it.
    """

    # trace:v1 id=impl.worker-activity-watch-init work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-9N23MPRP
    def __init__(
        self,
        *,
        seconds: float,
        on_stall: Callable[[], None],
        clock: Callable[[], float] = time.monotonic,
        interval: float | None = None,
    ) -> None:
        self._seconds = seconds
        self._on_stall = on_stall
        self._clock = clock
        # Poll often enough that a short budget still fires promptly, but never
        # busier than 5s in production (the budget is minutes there).
        self._interval = interval if interval is not None else min(5.0, max(seconds / 3.0, 0.01))
        self._lock = threading.Lock()
        self._last_at = clock()
        self._last_kind = "turn start"
        self._fired = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # trace:v1 id=impl.worker-activity-watch-enabled work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-9N23MPRP
    @property
    def enabled(self) -> bool:
        return self._seconds > 0

    # trace:v1 id=impl.worker-activity-watch-fired work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-9N23MPRP
    @property
    def fired(self) -> bool:
        return self._fired.is_set()

    # trace:v1 id=impl.worker-activity-watch-touch work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-9N23MPRP
    def touch(self, kind: str) -> None:
        with self._lock:
            self._last_at = self._clock()
            self._last_kind = kind

    # trace:v1 id=impl.worker-activity-watch-reason work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-9N23MPRP
    def reason(self) -> str:
        with self._lock:
            silent = self._clock() - self._last_at
            kind = self._last_kind
        return f"no agent activity for {int(silent)}s (last event: {kind})"

    # trace:v1 id=impl.worker-agent-activity-check work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-9N23MPRP
    def check(self) -> bool:
        """Fire once the silence exceeds the budget. Also the watcher's loop body."""
        if self.fired or not self.enabled:
            return self.fired
        with self._lock:
            silent = self._clock() - self._last_at
        if silent < self._seconds:
            return False
        self._fired.set()
        try:
            self._on_stall()
        except Exception:  # noqa: BLE001 — the caller still reports the stall
            log.exception("stall stop failed")
        return True

    # trace:v1 id=impl.worker-activity-watch-start work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-9N23MPRP
    def start(self) -> None:
        if not self.enabled:
            return
        self._thread = threading.Thread(target=self._watch, name="agent-activity-watch", daemon=True)
        self._thread.start()

    # trace:v1 id=impl.worker-activity-watch-stop work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-9N23MPRP
    def stop(self) -> None:
        self._stop.set()

    # trace:v1 id=impl.worker-activity-watch-watch work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-9N23MPRP
    def _watch(self) -> None:
        while not self._stop.wait(self._interval):
            if self.check():
                return


# trace:v1 id=impl.worker-rpc-blocking work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-9N23MPRP
def _run_rpc_blocking(
    inputs: TaskInputs,
    *,
    task_kind: str,
    prompt: str,
    bindings: ToolBindings,
    directive: DirectiveInfo | None = None,
) -> str | None:
    """Run a full RPC turn synchronously. Returns final assistant text (or None)."""
    settings = inputs.settings

    tools_called: set[str] = set()

    def _on_tool_end(event: ToolExecutionEndEvent) -> None:
        tool_name = event.tool_name
        # `tool_name` is transport-normalized by omp_rpc for top-level and
        # xd:// device dispatches, but an eval-bridged host tool only surfaces
        # as the enclosing `eval`; terminal-action detection therefore relies
        # on `_on_host_tool_completed`. A failed execution (`is_error`) does
        # not count — a rejected submit must still trigger the reminder.
        ok = event.result is not None and not event.is_error
        if ok:
            tools_called.add(tool_name)
        log.info(
            "tool_end",
            extra={
                "issue": bindings.issue_key,
                "tool": tool_name,
                "ok": ok,
            },
        )

    # trace:v1 id=impl.worker-host-tool-completed work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-9N23MPRP
    def _on_host_tool_completed(event: HostToolCompletedEvent) -> None:
        # Fires for every dispatch path (top-level, `write xd://X`, eval
        # bridge) once the host tool's `execute()` returned, so a terminal
        # action reached from inside `eval` still ends the task (#13583).
        tools_called.add(event.tool_name)
        log.info(
            "host_tool_completed",
            extra={"issue": bindings.issue_key, "tool": event.tool_name},
        )

    def _on_msg(event: MessageUpdateEvent) -> None:
        ev = event.assistant_message_event
        if isinstance(ev, dict) and ev.get("type") == "text_delta":
            log.debug("delta", extra={"issue": bindings.issue_key, "delta": str(ev.get("delta", ""))[:200]})

    rpc_env = _build_extra_env(settings)
    rpc_env.update(_prepare_slot_runtime_env(inputs.workspace, inputs.slot_uid))
    rpc_env.update(_safe_directory_env(bindings.workspace.repo_dir))
    rpc_env.update(_git_identity_env(inputs.settings.resolved_author_name, inputs.settings.git_author_email))
    # Bare worktrees have no node_modules; install (idempotently) so the agent
    # can resolve workspace packages (@oh-my-pi/pi-*) and actually run tests.
    host_tools.ensure_workspace_dependencies(bindings)
    resuming = _has_prior_session(bindings.workspace.session_dir)
    extra_args: tuple[str, ...] = ("--continue",) if resuming else ()
    # Repo-provided extension/hook code (`<repo>/.omp/hooks/pre|post/*.ts`) is
    # discovered from the workspace and executed in-process by omp. Our runs
    # need none of it — every GitHub side effect goes through host tools — and
    # a PR could otherwise run arbitrary code inside the agent, so keep ambient
    # discovery off.
    extra_args += ("--no-extensions",)
    overlay = _write_fallback_chains(settings)
    if overlay is not None:
        extra_args += ("--config", str(overlay))
    log.info(
        "rpc_resume",
        extra={
            "issue": bindings.issue_key,
            "task": task_kind,
            "resuming": resuming,
            "session_dir": str(bindings.workspace.session_dir),
            "attempts": inputs.attempts,
        },
    )
    model_override, thinking_override = _resolve_pragma_overrides(directive, settings)
    chosen_model = model_override or (
        settings.pick_release_model() if task_kind == "handle_release_ci" else settings.pick_model()
    )
    chosen_thinking = thinking_override or settings.thinking_level
    log.info(
        "rpc_model_pick",
        extra={
            "issue": bindings.issue_key,
            "model": chosen_model,
            "pool": list(settings.release_model_pool if task_kind == "handle_release_ci" else settings.model_pool),
            "thinking": chosen_thinking,
            "pragma_model": model_override,
            "pragma_thinking": thinking_override,
        },
    )
    inputs.db.set_event_model(inputs.delivery_id, chosen_model)
    if task_kind == "handle_release_ci":
        assert inputs.release is not None
        append_system_prompt = persona.system_append_release(
            repo=inputs.repo,
            release=inputs.release,
            workspace=inputs.workspace,
            release_commit_prefix=inputs.settings.release_commit_prefix,
        )
    elif task_kind == "review_pr":
        assert inputs.issue is not None
        append_system_prompt = persona.system_append_pr_review(
            repo=inputs.repo,
            issue=inputs.issue,
            workspace=inputs.workspace,
            bot_login=inputs.settings.bot_login,
        )
    else:
        assert inputs.issue is not None
        append_system_prompt = persona.system_append(
            repo=inputs.repo,
            issue=inputs.issue,
            workspace=inputs.workspace,
            bot_login=inputs.settings.bot_login,
        )

    # Telemetry for the GitHub message footer: which model ran, how long it has
    # been going, and what it has cost so far (accumulated from the
    # `message_end` usage events below), so any comment or PR body can report
    # all three.
    stats = host_tools.RunStats(model=chosen_model, started_monotonic=time.monotonic())
    object.__setattr__(bindings, "stats", stats)

    with RpcClient(
        executable=settings.omp_command,
        cwd=bindings.workspace.repo_dir,
        session_dir=bindings.workspace.session_dir,
        env=rpc_env,
        no_session=False,
        no_title=True,
        model=chosen_model,
        provider=settings.provider,
        thinking=chosen_thinking if chosen_thinking != "off" else None,
        append_system_prompt=append_system_prompt,
        tools=list(OMP_BUILTIN_TOOLS),
        custom_tools=host_tools.build(bindings),
        request_timeout=settings.request_timeout_seconds,
        startup_timeout=60.0,
        max_event_history=50_000,
        extra_args=extra_args,
        user=inputs.slot_uid,
        group=inputs.slot_uid if inputs.slot_uid is not None else None,
        extra_groups=["omp"] if inputs.slot_uid is not None else None,
    ) as client:
        # Arm cancellation: from this point the API can kill the omp subprocess
        # out from under us, which makes `prompt_and_wait` raise an `RpcError`
        # we'll let propagate. The `with` exit calls `client.stop()` again, but
        # it's idempotent.
        #
        # NOTE: omp_rpc.RpcClient.stop() has a bug where it sets `_stopping=True`
        # before the stdout reader loop notices the closed pipe, so the reader's
        # `if not self._stopping` guard skips `_mark_closed()` entirely.
        # `_wait_for_agent_end` then blocks on `_event_condition` until the hard
        # timeout because `_closed_error` is never set. We work around it here
        # by calling `_mark_closed()` ourselves after stop returns — this is
        # idempotent (it no-ops when `_closed_error` is already set).
        def _cancel_hook() -> None:
            try:
                client.stop()
            finally:
                # Private API, but the only way to unblock `_wait_for_agent_end`
                # without waiting for the request timeout. Idempotent.
                client._mark_closed(  # noqa: SLF001
                    RpcProcessExitError("cancelled by operator")
                )

        if bindings.abort is not None:
            bindings.abort.stop = _cancel_hook
        register_cancel_hook(_cancel_hook)
        try:
            client.install_headless_ui()
            client.on_tool_execution_end(_on_tool_end)
            client.on_host_tool_completed(_on_host_tool_completed)
            client.on_message_update(_on_msg)

            # trace:v1 id=impl.worker-on-message-end work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-9N23MPRP
            def _on_message_end(event: Any) -> None:
                message = getattr(event, "message", None)
                if not isinstance(message, Mapping) or message.get("role") != "assistant":
                    return
                stats.add_usage(message.get("usage"))
                # Ground truth for the footer: omp silently switches providers
                # when the configured one fails, so the answering model comes
                # from the message, not from our own pick.
                stats.note_answered_model(message.get("provider"), message.get("model"))

            def _on_fallback_applied(event: Any) -> None:
                from_model = getattr(event, "from_model", None)
                to_model = getattr(event, "to_model", None)
                log.warning(
                    "model fallback applied",
                    extra={"issue": bindings.issue_key, "task": task_kind, "from": from_model, "to": to_model},
                )
                if isinstance(to_model, str) and to_model:
                    stats.fallback_model = to_model

            def _on_fallback_succeeded(event: Any) -> None:
                log.info(
                    "model fallback succeeded",
                    extra={"issue": bindings.issue_key, "task": task_kind, "model": getattr(event, "model", None)},
                )

            client.on_message_end(_on_message_end)
            client.on_retry_fallback_applied(_on_fallback_applied)
            client.on_retry_fallback_succeeded(_on_fallback_succeeded)

            phases = persona.seed_phases(task_kind)
            if phases:
                try:
                    if task_kind in ("triage_issue", "review_pr") and not resuming:
                        # Fresh kickoff tasks seed the full plan.
                        client.set_todos(phases)
                    elif task_kind in ("triage_issue", "review_pr"):
                        # Resumed kickoff tasks keep prior todo state from the
                        # JSONL transcript; re-seeding would clobber progress.
                        log.info(
                            "set_todos skipped (resume)",
                            extra={"issue": bindings.issue_key, "task": task_kind},
                        )
                    else:
                        # Follow-up: keep prior phases (e.g. Reproduce / Fix / PR)
                        # so the agent still sees the context, but append the
                        # follow-up phase at the end.
                        existing = list(client.get_todos())
                        merged = [
                            {
                                "id": p.id,
                                "name": p.name,
                                "tasks": [
                                    {
                                        "id": t.id,
                                        "content": t.content,
                                        "status": t.status,
                                        "notes": t.notes,
                                        "details": t.details,
                                        "blocker": t.blocker,
                                    }
                                    for t in p.tasks
                                ],
                            }
                            for p in existing
                        ] + phases
                        client.set_todos(merged)
                except RpcError as exc:
                    log.warning("set_todos failed", extra={"err": str(exc)})

            log.info(
                "rpc_start",
                extra={"issue": bindings.issue_key, "task": task_kind, "branch": bindings.workspace.branch},
            )

            # Silence detection. `hard_timeout` below bounds the whole turn;
            # this bounds *silence*, because a hung provider stream otherwise
            # looks like work until the budget runs out and every retry repeats
            # it (see `_AgentActivityWatch`). Any agent event re-arms it.
            # trace:v1 id=impl.worker-activity-watch-on-stall work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-9N23MPRP
            def _on_stall() -> None:
                log.warning(
                    "rpc_stalled",
                    extra={"issue": bindings.issue_key, "task": task_kind, "reason": watch.reason()},
                )
                try:
                    # Same kill path as the hard timeout / operator cancel: stops
                    # the child and unblocks `_wait_for_agent_end`.
                    _cancel_hook()
                except Exception:
                    log.exception("stall stop failed", extra={"issue": bindings.issue_key, "task": task_kind})

            watch = _AgentActivityWatch(seconds=settings.task_stall_seconds, on_stall=_on_stall)
            client.on_event(lambda event: watch.touch(event.type))
            watch.start()

            hard_timeout_seconds = _task_timeout(settings, task_kind) + settings.task_timeout_hard_grace_seconds
            hard_timeout_fired = threading.Event()

            def _hard_stop() -> None:
                hard_timeout_fired.set()
                log.warning(
                    "rpc_hard_timeout",
                    extra={"issue": bindings.issue_key, "task": task_kind, "timeout": hard_timeout_seconds},
                )
                try:
                    _cancel_hook()
                except Exception:
                    log.exception(
                        "rpc hard timeout stop failed", extra={"issue": bindings.issue_key, "task": task_kind}
                    )

            hard_timer = threading.Timer(hard_timeout_seconds, _hard_stop)
            hard_timer.daemon = True
            hard_timer.start()
            try:
                turn = _drive_turn(
                    client,
                    prompt,
                    task_kind=task_kind,
                    inputs=inputs,
                    bindings=bindings,
                    tools_called=tools_called,
                )
                if turn is None:
                    return None
            except BaseException as exc:
                # The stall kill surfaces as an RPC error; report the silence
                # instead, which is the actionable fact.
                if watch.fired:
                    raise AgentStalledError(watch.reason()) from exc
                raise
            finally:
                hard_timer.cancel()
                watch.stop()
            assert turn is not None  # returned above when None; narrows for LSP
            if watch.fired:
                raise AgentStalledError(watch.reason())
            if hard_timeout_fired.is_set():
                raise TimeoutError("omp task exceeded hard timeout")
            if turn.assistant_message is not None:
                stop_reason = turn.assistant_message.get("stopReason")
                if stop_reason == "error":
                    error_msg = turn.assistant_message.get("errorMessage") or "model returned error"
                    raise RuntimeError(f"omp agent error (stopReason=error): {error_msg}")
            log.info(
                "rpc_done",
                extra={
                    "issue": bindings.issue_key,
                    "task": task_kind,
                    "messages": len(turn.messages),
                    "events": len(turn.events),
                },
            )
            return turn.assistant_text
        finally:
            unregister_cancel_hook()


# trace:v1 id=impl.worker-run-task work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-9N23MPRP
async def run_task(
    *,
    task_kind: str,
    inputs: TaskInputs,
    comment: CommentInfo | None = None,
    pr_number: int | None = None,
    review_payload: dict[str, Any] | None = None,
    pr: PullRequestInfo | None = None,
    directive: DirectiveInfo | None = None,
    thread: tuple[ThreadMessage, ...] = (),
) -> str | None:
    """Async wrapper that runs the synchronous RPC driver on a worker thread."""
    review_mode = task_kind == "review_pr" or inputs.workspace.branch.startswith("review/pr-")
    loop = asyncio.get_running_loop()
    release_binding: ReleaseToolContext | None = None
    if inputs.release is not None:
        release_key = f"{inputs.repo.full_name}#{inputs.release.tag}"
        release_row = inputs.db.get_release(release_key)
        if release_row is None:
            raise RuntimeError(f"release state missing for {release_key}")
        release_binding = ReleaseToolContext(
            repo=inputs.repo.full_name,
            tag=inputs.release.tag,
            version=inputs.release.version,
            key=release_key,
            expected_sha=release_row.current_sha,
            default_branch=inputs.release.default_branch,
        )
    # The trigger is the authoritative capability record minted by trusted
    # routing code, and it is already the source for the run token's scopes.
    # Bindings must mirror that exact set: operator-granted extras (notably
    # SKIP_CHECKS from `/allow-skip-checks`) live only on the trigger, so
    # building from `capabilities_for(task_kind)` would silently drop them and
    # leave the pre-publish gates unreachable. Legacy/manual paths without a
    # trigger keep the task-kind profile.
    from carter_omp.github_events import TriggerContext

    capabilities = (
        inputs.trigger.capabilities if isinstance(inputs.trigger, TriggerContext) else capabilities_for(task_kind)
    )
    bindings = ToolBindings(
        db=inputs.db,
        github=inputs.github,
        git_transport=inputs.git_transport,
        repo=inputs.repo,
        issue=inputs.issue,
        workspace=inputs.workspace,
        loop=loop,
        settings=inputs.settings,
        author_name=inputs.settings.resolved_author_name,
        author_email=inputs.settings.git_author_email,
        inbound_thread_number=pr_number,
        inbound_is_pr=pr_number is not None,
        review_mode=review_mode,
        capabilities=capabilities,
        trigger=inputs.trigger,
        impl_authorized=bool(directive is not None and directive.authorizes_impl),
        slot_uid=inputs.slot_uid,
        abort=AbortController(),
        release=release_binding,
    )
    _attach_run_token(inputs, bindings)
    await _ack_pickup_reaction(inputs, bindings)
    resuming = _has_prior_session(inputs.workspace.session_dir)
    prompt = _build_prompt(
        task_kind,
        inputs,
        comment=comment,
        pr_number=pr_number,
        review_payload=review_payload,
        pr=pr,
        directive=directive,
        thread=thread,
        resuming=resuming,
    )
    try:
        result = await asyncio.to_thread(
            _run_rpc_blocking,
            inputs,
            task_kind=task_kind,
            prompt=prompt,
            bindings=bindings,
            directive=directive,
        )
    except BaseException:
        # Failed/aborted task: NEVER capture, the artifacts may be inconsistent
        # with the source state and would poison the cache. Telemetry is still
        # recorded: a failed run spent real money, and that spend is exactly
        # what the console needs to show.
        _record_run_telemetry(inputs, bindings)
        raise
    else:
        await asyncio.to_thread(_capture_natives_cache, inputs)
        await _consume_trigger_label(inputs, bindings)
        _record_run_telemetry(inputs, bindings)
        _record_agent_abort(inputs, bindings)
        return result


# trace:v1 id=impl.worker-record-run-telemetry work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-9N23MPRP
def _record_run_telemetry(inputs: TaskInputs, bindings: ToolBindings) -> None:
    """Persist the run's telemetry (spend, tokens, fallback) on its event row.

    Best-effort observability: a missing or failing write must never change the
    run's outcome, and it never raises.
    """
    stats = bindings.stats
    if stats is None:
        return
    try:
        inputs.db.set_event_telemetry(
            inputs.delivery_id,
            fallback_model=stats.fallback_model,
            duration_ms=int(stats.elapsed_seconds() * 1000),
            cost_usd=stats.cost_usd,
            cache_cost_usd=stats.cost_cache_usd,
            miss_tokens=stats.miss_tokens,
            output_tokens=stats.output_tokens,
            cache_read_tokens=stats.cache_read_tokens,
            cache_write_tokens=stats.cache_write_tokens,
        )
    except Exception as exc:  # noqa: BLE001 — observability must never fail a run
        log.debug(
            "run telemetry write failed",
            extra={"delivery": inputs.delivery_id, "err": str(exc)[:120]},
        )


# trace:v1 id=impl.worker-record-agent-abort work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-9N23MPRP
def _record_agent_abort(inputs: TaskInputs, bindings: ToolBindings) -> None:
    """Mark the delivery failed when the agent pulled its own plug.

    `abort_task` is a legitimate stop ("needs info", "harness fault"), but it
    used to leave the delivery marked `done`, so a run that published nothing
    still looked green in `status`/dashboards and nobody went looking. Record
    the agent's own reason instead. Terminal — nothing auto-retries, because a
    30-minute task is not worth blind-retrying; the operator re-triggers.
    """
    abort = bindings.abort
    if abort is None or not abort.triggered:
        return
    reason = (abort.reason or "no reason given").strip()[:500]
    inputs.db.mark_event(inputs.delivery_id, "failed", error=f"agent aborted: {reason}")
    log.warning(
        "event marked failed",
        extra={"delivery": inputs.delivery_id, "issue": bindings.issue_key, "reason": reason[:200]},
    )


# trace:v1 id=impl.worker-consume-trigger-label work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-9N23MPRP
async def _consume_trigger_label(inputs: TaskInputs, bindings: ToolBindings) -> None:
    """Consume the one-shot trigger label after an authorized label run.

    Remove `trigger_label`, add `<label>:running` at start is handled by
    queue state; here we mark terminal state via labels best-effort.
    Must use `bindings.github`: the proxy's label endpoints require the
    per-run token, so the unscoped `inputs.github` client 401s.
    Failures are swallowed: labels are UX, never the security boundary
    (replay uses the stored TriggerContext, never current labels).
    """
    from carter_omp.github_events import TriggerContext

    trigger = inputs.trigger
    if not isinstance(trigger, TriggerContext) or trigger.trigger_kind != "label":
        return
    if inputs.issue is None:
        return
    label = inputs.settings.trigger_label
    number = trigger.pull_request_number if trigger.pull_request_number is not None else trigger.issue_number
    if number is None:
        return
    try:
        await bindings.github.remove_issue_label(trigger.repository_full_name, number, label)
        await bindings.github.add_issue_labels(trigger.repository_full_name, number, [f"{label}:running"])
    except Exception as exc:
        log.warning("trigger label consume failed", extra={"delivery": inputs.delivery_id, "err": str(exc)[:120]})


def _capture_natives_cache(inputs: TaskInputs) -> None:
    """Best-effort: store the workspace's fresh natives under its current key.
    Runs after a successful task on a worker thread. ANY failure is logged
    and swallowed — cache errors NEVER fail a task.
    """
    cache = inputs.natives_cache
    if cache is None:
        return
    workspace = inputs.workspace
    native_dir = workspace.repo_dir / "packages" / "natives" / "native"
    if not native_dir.exists():
        return
    try:
        key = natives_compute_key(workspace.repo_dir)
    except Exception as exc:
        log.debug(
            "natives_cache capture key compute failed",
            extra={"workspace": workspace.workspace_key, "err": str(exc)},
        )
        return
    try:
        stored = cache.capture(
            workspace.repo_full_name,
            key,
            native_dir,
            source_workspace=workspace.workspace_key,
        )
    except Exception as exc:
        log.warning(
            "natives_cache capture failed",
            extra={"workspace": workspace.workspace_key, "key": key, "err": str(exc)},
        )
        return
    log.info(
        "natives_cache",
        extra={
            "action": "stored" if stored is not None else "skip",
            "workspace": workspace.workspace_key,
            "repo": workspace.repo_full_name,
            "key": key,
            "cache_dir": str(stored) if stored else None,
        },
    )


__all__ = ["DirectiveInfo", "ReleaseTaskContext", "TaskInputs", "ThreadMessage", "run_task"]
