"""Host-queued work carries its own authorization.

Webhook events are authorized by the router, which stores a TriggerContext in
the payload. Work our own code queues (the `carter-omp triage` CLI, the
self-report dispatch) has no sender to check, so it must build that record
itself — without it the run has no trigger, mints no run token, and every proxy
write 401s.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from carter_omp import tasks
from carter_omp.capabilities import ISSUE_RUN_CAPABILITIES, Capability
from carter_omp.db import issue_key
from carter_omp.github_client import IssueInfo, RepoInfo
from carter_omp.github_events import TriggerContext
from carter_omp.manual_triage import ManualTriageConflict, enqueue_manual_triage, manual_delivery_id


def _github() -> SimpleNamespace:
    async def get_issue(repo: str, number: int) -> IssueInfo:
        return IssueInfo(
            repo=repo,
            number=number,
            title="[bot-report] gate blocks an untraced file",
            body="expected/actual",
            state="open",
            author="carter-omp[bot]",
            labels=("bot-report", "carter-omp"),
            is_pull_request=False,
        )

    async def get_repo(repo: str) -> RepoInfo:
        return RepoInfo(full_name=repo, default_branch="main", clone_url="https://x/self.git", private=False)

    return SimpleNamespace(get_issue=get_issue, get_repo=get_repo)


async def test_enqueue_manual_triage_authorizes_host_queued_work(db) -> None:
    delivery = await enqueue_manual_triage(
        db=db,
        github=cast(Any, _github()),
        repo_full="octo/self",
        number=7,
        actor_login="carter-omp[bot]",
        tool="self-report",
        installation_id=42,
    )

    assert delivery == manual_delivery_id("octo/self", 7)
    row = db.get_event(delivery)
    assert row is not None and row.state == "queued"
    assert row.issue_key == issue_key("octo/self", 7)

    trigger = tasks._trigger_from_payload(row.payload)
    assert isinstance(trigger, TriggerContext)
    assert trigger.trigger_kind == "manual_cli"
    assert trigger.trigger_value == "self-report"
    assert trigger.repository_full_name == "octo/self"
    assert trigger.issue_number == 7
    assert trigger.installation_id == 42
    assert trigger.actor_login == "carter-omp[bot]"
    # Full issue-run rights: a fix run must be able to push and open its PR.
    assert trigger.capabilities == ISSUE_RUN_CAPABILITIES
    assert Capability.PUSH_BRANCH in trigger.capabilities
    assert trigger.run_id  # a fresh run identity, so the token mint has one


async def test_enqueue_manual_triage_reuses_the_slot_and_refuses_active_rows(db) -> None:
    first = await enqueue_manual_triage(db=db, github=cast(Any, _github()), repo_full="octo/self", number=7)
    with pytest.raises(ManualTriageConflict):
        await enqueue_manual_triage(db=db, github=cast(Any, _github()), repo_full="octo/self", number=7)

    # A finished row is replaceable: re-triggering the same issue is normal.
    db.mark_event(first, "done")
    again = await enqueue_manual_triage(db=db, github=cast(Any, _github()), repo_full="octo/self", number=7)
    assert again == first
    assert db.get_event(again).state == "queued"
