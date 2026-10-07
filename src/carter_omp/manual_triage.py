"""Manually enqueue an issue as if a webhook arrived.

Shared by the `carter_omp triage` CLI and the dashboard's POST /api/trigger.
"""

from __future__ import annotations

import asyncio
import re
import time
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from carter_omp.capabilities import POLICY_VERSION, Capability, capabilities_for
from carter_omp.db import INACTIVE_EVENT_STATES, Database, EventRow, issue_key
from carter_omp.github_backend import GitHubBackend
from carter_omp.github_events import TriggerContext

_ISSUE_REF = re.compile(r"^(?P<owner>[^/\s]+)/(?P<repo>[^#\s]+)#(?P<number>\d+)$")
_ISSUE_URL = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/"
    r"(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)/issues/(?P<number>\d+)"
    r"(?:[/?#].*)?$"
)


class InvalidIssueRef(ValueError):
    """Raised when the user-supplied issue reference can't be parsed."""


class ManualTriageError(ValueError):
    """Raised when a live GitHub issue cannot be manually triaged."""


class ManualTriageConflict(RuntimeError):
    """Raised when a stable manual delivery id is already active."""

    def __init__(self, delivery_id: str, state: str) -> None:
        self.delivery_id = delivery_id
        self.state = state
        super().__init__(f"{delivery_id} is already {state}")


class ManualTriageTimeout(TimeoutError):
    """Raised when a manual CLI waiter stops before terminal state."""

    def __init__(self, delivery_id: str, state: str, timeout_seconds: float) -> None:
        self.delivery_id = delivery_id
        self.state = state
        self.timeout_seconds = timeout_seconds
        super().__init__(f"{delivery_id} did not reach a terminal state within {timeout_seconds:g}s (state={state})")


def parse_issue_ref(ref: str) -> tuple[str, int]:
    """Parse `owner/repo#NN` or a github issue url into `("owner/repo", NN)`."""
    cleaned = ref.strip()
    match = _ISSUE_REF.match(cleaned) or _ISSUE_URL.match(cleaned)
    if match is None:
        raise InvalidIssueRef(f"expected owner/repo#NN or https://github.com/owner/repo/issues/NN, got {ref!r}")
    return f"{match.group('owner')}/{match.group('repo')}", int(match.group("number"))


def manual_delivery_id(repo_full: str, number: int) -> str:
    """Stable delivery id for manually-triggered triage. Re-runs reuse it."""
    return f"manual-{repo_full.replace('/', '__')}-{number}"


async def build_issues_opened_payload(github: GitHubBackend, repo_full: str, number: int) -> dict[str, Any]:
    """Fetch the issue + repo metadata and synthesize an `issues.opened` payload."""
    issue = await github.get_issue(repo_full, number)
    if issue.is_pull_request:
        raise ManualTriageError(f"{repo_full}#{number} is a pull request, not an issue")
    repo = await github.get_repo(repo_full)
    return {
        "action": "opened",
        "issue": {
            "number": issue.number,
            "title": issue.title,
            "body": issue.body,
            "state": issue.state,
            "user": {"login": issue.author},
            "labels": [{"name": lbl} for lbl in issue.labels],
        },
        "repository": {
            "full_name": repo.full_name,
            "default_branch": repo.default_branch,
            "clone_url": repo.clone_url,
            "private": repo.private,
        },
    }


# trace:v1 id=impl.manual-triage-trigger work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-BKNZHMZ0
def build_manual_trigger(
    *,
    repo_full: str,
    number: int,
    delivery_id: str,
    actor_login: str,
    tool: str,
    capabilities: frozenset[Capability] | None = None,
    installation_id: int | None = None,
) -> TriggerContext:
    """Authorize work that *our own code* queues (CLI triage, self-report fix).

    Routing authorizes webhook events: it checks the sender against the
    allowlist and stores the resulting TriggerContext in the payload. Host-queued
    work has no sender to check, so the caller builds that record explicitly —
    with `manual_cli` as the provenance kind. Without it the run carries no
    trigger, `_attach_run_token` mints nothing, and every proxy write 401s
    (which is how manual triage silently failed in orchestrator mode).
    """
    return TriggerContext(
        run_id=str(uuid4()),
        delivery_id=delivery_id,
        repository_id=0,  # audit-only: the proxy authorizes on the repo *name*
        repository_full_name=repo_full,
        installation_id=installation_id or 0,
        actor_id=0,
        actor_login=actor_login or "carter-omp",
        actor_type="Bot",
        event_type="issues",
        action="opened",
        trigger_kind="manual_cli",
        trigger_object_id=None,
        trigger_value=tool,
        issue_number=number,
        pull_request_number=None,
        capabilities=capabilities if capabilities is not None else capabilities_for("triage_issue"),
        policy_version=POLICY_VERSION,
        authorized_at=datetime.now(UTC),
    )


# trace:v1 id=impl.manual-triage-enqueue work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-BKNZHMZ0
async def enqueue_manual_triage(
    *,
    db: Database,
    github: GitHubBackend,
    repo_full: str,
    number: int,
    actor_login: str = "",
    tool: str = "cli",
    capabilities: frozenset[Capability] | None = None,
    installation_id: int | None = None,
) -> str:
    """Fetch the issue from GitHub and queue it for the worker pool.

    Returns the delivery_id. A row may already exist from a previous manual
    triage; inactive rows are replaced so the fresh payload (and reset attempt
    counter) wins. Active rows are left intact.
    """
    delivery = manual_delivery_id(repo_full, number)
    existing = db.get_event(delivery)
    if existing is not None and existing.state in ("queued", "running"):
        raise ManualTriageConflict(delivery, existing.state)

    payload = await build_issues_opened_payload(github, repo_full, number)
    payload["_carter_omp_trigger"] = build_manual_trigger(
        repo_full=repo_full,
        number=number,
        delivery_id=delivery,
        actor_login=actor_login,
        tool=tool,
        capabilities=capabilities,
        installation_id=installation_id,
    ).to_record()
    replaced = db.replace_event_if_state_in(
        delivery_id=delivery,
        event_type="issues",
        repo=repo_full,
        issue_key=issue_key(repo_full, number),
        payload=payload,
        state="queued",
        allowed_existing_states=INACTIVE_EVENT_STATES,
    )
    if not replaced:
        current = db.get_event(delivery)
        state = current.state if current is not None else "active"
        raise ManualTriageConflict(delivery, state)
    return delivery


_TERMINAL_STATES: tuple[str, ...] = ("done", "failed", "skipped")


async def await_terminal_state(
    db: Database,
    delivery_id: str,
    *,
    poll_interval: float = 2.0,
    timeout: float | None = None,
) -> EventRow | None:
    """Block until the event row reaches a terminal state, vanishes, or times out.

    Pure DB polling — the caller MUST NOT spawn its own ``WorkerPool``; the
    long-lived ``serve`` process is the only owner of the dispatcher loop.
    Returns the final row, or ``None`` if the row was deleted while waiting.
    Raises ``ManualTriageTimeout`` if ``timeout`` elapses first.
    """
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        row = db.get_event(delivery_id)
        if row is None:
            return None
        if row.state in _TERMINAL_STATES:
            return row

        sleep_for = poll_interval
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                assert timeout is not None
                raise ManualTriageTimeout(delivery_id, row.state, timeout)
            sleep_for = min(poll_interval, remaining)
        await asyncio.sleep(sleep_for)


__all__ = [
    "InvalidIssueRef",
    "ManualTriageError",
    "ManualTriageConflict",
    "ManualTriageTimeout",
    "await_terminal_state",
    "build_issues_opened_payload",
    "enqueue_manual_triage",
    "manual_delivery_id",
    "parse_issue_ref",
]
