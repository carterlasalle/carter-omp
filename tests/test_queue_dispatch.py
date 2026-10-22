"""Dispatch action -> task mapping in WorkerPool._dispatch."""

from __future__ import annotations

import pytest

from carter_omp import tasks
from carter_omp.config import Settings
from carter_omp.db import Database, EventRow
from carter_omp.github_backend import GitHubBackend
from carter_omp.github_client import CommentInfo
from carter_omp.github_events import Actor, AuthorizationDecision, route_authorized_event
from carter_omp.queue import WorkerPool
from carter_omp.sandbox import LocalGitTransport, SandboxManager
from carter_omp.slot_pool import SlotPool
from tests.fakes import FakeGitHub


def _state(db: Database, delivery: str) -> str:
    """The stored event's state, asserting the row exists."""
    row = db.get_event(delivery)
    assert row is not None, f"no event {delivery}"
    return row.state


class _StubGitHub(FakeGitHub):
    """Sentinel; dispatch tests stub out the task body."""


class _StubSandbox(SandboxManager):
    def __init__(self) -> None:
        self.natives_cache = None

    def reclaim_workspace_caches(self, *, repo: str, number: int | str) -> bool:
        del repo, number
        return False

    def reclaim_all_caches(self) -> int:
        return 0


def _make_pool(settings: Settings, db: Database, *, github: GitHubBackend | None = None) -> WorkerPool:
    return WorkerPool(
        settings=settings,
        db=db,
        github=github if github is not None else _StubGitHub(),
        sandbox=_StubSandbox(),
        git_transport=LocalGitTransport(token=None),
        slot_pool=SlotPool(),
    )


def _pr_row(action: str, *, delivery: str = "pr1") -> EventRow:
    return EventRow(
        delivery_id=delivery,
        event_type="pull_request",
        repo="octo/widget",
        issue_key="octo/widget#7",
        payload={"action": action, "pull_request": {"number": 7}},
        received_at="2026-01-01T00:00:00Z",
        state="running",
        attempts=1,
        last_error=None,
    )


def _issue_row(action: str, *, delivery: str = "is1") -> EventRow:
    return EventRow(
        delivery_id=delivery,
        event_type="issues",
        repo="octo/widget",
        issue_key="octo/widget#4",
        payload={"action": action, "issue": {"number": 4}},
        received_at="2026-01-01T00:00:00Z",
        state="running",
        attempts=1,
        last_error=None,
    )


@pytest.mark.parametrize("action", ["opened", "reopened", "labeled"])
@pytest.mark.asyncio
async def test_dispatch_routes_issue_triage_actions_to_triage_issue(
    settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    """Every issue action `route` can queue for triage MUST reach `tasks.triage_issue`."""
    seen: list[str] = []

    async def fake_triage_issue(*, payload, **_kwargs) -> None:
        seen.append(str(payload.get("action")))

    monkeypatch.setattr(tasks, "triage_issue", fake_triage_issue)

    await _make_pool(settings, db)._dispatch(_issue_row(action))  # noqa: SLF001

    assert seen == [action]


@pytest.mark.parametrize("action", ["opened", "reopened", "ready_for_review"])
@pytest.mark.asyncio
async def test_dispatch_routes_pr_review_actions_to_review_pr(
    settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    """Every PR action `route` can queue for review MUST reach `tasks.review_pr`."""
    seen: list[str] = []

    async def fake_review_pr(*, payload, **_kwargs) -> None:
        seen.append(str(payload.get("action")))

    monkeypatch.setattr(tasks, "review_pr", fake_review_pr)

    await _make_pool(settings, db)._dispatch(_pr_row(action))  # noqa: SLF001

    assert seen == [action]


@pytest.mark.asyncio
async def test_dispatch_pr_synchronize_is_noop(
    settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Actions `route` never queues for review must NOT spawn a review task."""
    called = False

    async def fake_review_pr(**_kwargs) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(tasks, "review_pr", fake_review_pr)

    await _make_pool(settings, db)._dispatch(_pr_row("synchronize"))  # noqa: SLF001

    assert called is False


@pytest.mark.asyncio
async def test_dispatch_routes_completed_workflow_to_release_handler(
    settings: Settings,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, int]] = []

    async def fake_handle_release_ci(*, payload, attempts, **_kwargs) -> None:
        seen.append((str(payload.get("action")), attempts))

    monkeypatch.setattr(tasks, "handle_release_ci", fake_handle_release_ci, raising=False)
    row = EventRow(
        delivery_id="release-1",
        event_type="workflow_run",
        repo="octo/widget",
        issue_key="octo/widget#release",
        payload={"action": "completed", "workflow_run": {"id": 10}},
        received_at="2026-01-01T00:00:00Z",
        state="running",
        attempts=2,
        last_error=None,
    )

    await _make_pool(settings, db)._dispatch(row)  # noqa: SLF001

    assert seen == [("completed", 2)]


def test_control_stop_marks_done_without_dispatch(tmp_path, settings) -> None:

    from carter_omp.queue import _control_command

    assert _control_command("stop") == "stop"
    assert _control_command("status") == "status"
    assert _control_command("fix the bug") is None
    # People type `@carter-omp status?` — trailing punctuation must not turn a
    # deterministic DB answer into a full model run.
    assert _control_command("status?") == "status"
    assert _control_command("STOP!") == "stop"
    # Only a bare command line counts: prose that merely starts with a command
    # word stays a normal follow-up (a model run), not a canned DB answer.
    assert _control_command("status: what's the state?") is None
    # `review`, `resume`, and `release-fix` need a model run, so they must not be
    # claimed as deterministic control commands.
    for ordinary in ("review", "resume", "release-fix"):
        assert _control_command(ordinary) is None


@pytest.mark.asyncio
async def test_dispatch_and_mark_keeps_a_worker_recorded_failure(
    settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The agent's own abort records `failed` with its reason; the success path
    must not paint over it — that is what made an aborted run look green."""
    db.record_event(
        delivery_id="d-abort",
        event_type="issues",
        repo="octo/widget",
        issue_key="octo/widget#4",
        payload={"action": "labeled", "issue": {"number": 4}},
    )
    row = db.claim_next_event()
    assert row is not None
    db.mark_event(row.delivery_id, "failed", error="agent aborted: harness fault")

    async def _noop(*_a, **_k) -> None:
        return None

    monkeypatch.setattr(WorkerPool, "_dispatch", _noop)

    await _make_pool(settings, db)._dispatch_and_mark(row)  # noqa: SLF001

    latest = db.get_event(row.delivery_id)
    assert latest is not None
    assert latest.state == "failed"
    assert latest.last_error == "agent aborted: harness fault"


class _RecordingGitHub(FakeGitHub):
    def __init__(self) -> None:
        self.comments: list[tuple[str, int, str]] = []

    async def post_comment(self, repo: str, number: int, body: str) -> CommentInfo:
        self.comments.append((repo, number, body))
        return CommentInfo(id=len(self.comments), author="bot", body=body, created_at="")


def _record_task_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def _make(name: str):
        async def _fake(**_kwargs: object) -> None:
            calls.append(name)

        return _fake

    for name in (
        "triage_issue",
        "handle_comment",
        "handle_pr_conversation",
        "review_pr",
        "handle_review",
        "handle_release_ci",
        "cleanup_workspace",
    ):
        monkeypatch.setattr(tasks, name, _make(name))
    return calls


def _insert_command_row(db: Database, command: str, *, delivery: str) -> EventRow:
    db.record_event(
        delivery_id=delivery,
        event_type="issue_comment",
        repo="octo/widget",
        issue_key="octo/widget#4",
        payload={
            "action": "created",
            "issue": {"number": 4},
            "_carter_omp_directive": {"body": command, "author": "carterlasalle"},
        },
    )
    row = db.claim_next_event()
    assert row is not None
    return row


@pytest.mark.asyncio
async def test_status_control_command_answers_from_db_without_model(
    settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`@bot status` posts a DB answer and invokes no task entry point."""
    db.upsert_issue(
        key="octo/widget#4",
        repo="octo/widget",
        number=4,
        state="reproducing",
        branch="carter-omp/ab/fix",
        pr_number=12,
    )
    db.record_event(
        delivery_id="prev",
        event_type="issue_comment",
        repo="octo/widget",
        issue_key="octo/widget#4",
        payload={"action": "created"},
    )
    prev = db.claim_next_event()
    assert prev is not None
    db.mark_event(prev.delivery_id, "done")

    calls = _record_task_calls(monkeypatch)
    github = _RecordingGitHub()
    row = _insert_command_row(db, "status", delivery="cmd-status")

    await _make_pool(settings, db, github=github)._dispatch_and_mark(row)  # noqa: SLF001

    assert calls == []
    assert _state(db, "cmd-status") == "done"
    assert len(github.comments) == 1
    repo, number, body = github.comments[0]
    assert (repo, number) == ("octo/widget", 4)
    assert "`octo/widget#4`" in body
    assert "`reproducing`" in body
    assert "carter-omp/ab/fix" in body
    assert "#12" in body
    # The status command reports the run before it, not itself.
    assert "`issue_comment` — `done`" in body


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["review", "resume", "release-fix"])
async def test_control_words_without_a_handler_are_ordinary_directives(
    settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    """These have no deterministic handler, so they stay model directives."""
    seen: list[str] = []

    async def fake_handle_comment(**_kwargs: object) -> None:
        seen.append(command)

    monkeypatch.setattr(tasks, "handle_comment", fake_handle_comment)
    row = _insert_command_row(db, command, delivery=f"cmd-{command}")

    await _make_pool(settings, db)._dispatch_and_mark(row)  # noqa: SLF001

    assert seen == [command]


# ---------- routed task -> dispatch (regression for #9) ----------


_TASK_ENTRY_POINTS = (
    "triage_issue",
    "handle_release_ci",
    "handle_pr_conversation",
    "handle_comment",
    "review_pr",
    "handle_review",
    "cleanup_workspace",
)

_BOT_PR = {"number": 7, "state": "open", "draft": False, "user": {"login": "carter-omp[bot]"}}
_HUMAN_PR = {"number": 7, "state": "open", "draft": False, "user": {"login": "alice"}}


# Every `(event, action)` pair `route_authorized_event` can queue, with the task
# it assigns. `assigned` rows were the gap: routed to a task, but `_dispatch`
# re-derived the task from `(event, action)` and had no `assigned` branch, so the
# delivery no-op'd and was recorded `done` (#9).
_ROUTED_CASES = [
    ("issues", {"action": "labeled", "label": {"name": "carter-omp"}, "issue": {"number": 4}}, "triage_issue"),
    (
        "issues",
        {"action": "assigned", "assignee": {"login": "carter-omp[bot]"}, "issue": {"number": 4}},
        "triage_issue",
    ),
    ("pull_request", {"action": "labeled", "pull_request": _HUMAN_PR}, "review_pr"),
    ("pull_request", {"action": "labeled", "pull_request": _BOT_PR}, "handle_pr_conversation"),
    (
        "pull_request",
        {"action": "assigned", "assignee": {"login": "carter-omp[bot]"}, "pull_request": _HUMAN_PR},
        "review_pr",
    ),
    (
        "pull_request",
        {"action": "assigned", "assignee": {"login": "carter-omp[bot]"}, "pull_request": _BOT_PR},
        "handle_pr_conversation",
    ),
    (
        "issue_comment",
        {"action": "created", "comment": {"id": 1, "body": "@carter-omp go"}, "issue": {"number": 4}},
        "handle_comment",
    ),
    (
        "issue_comment",
        {
            "action": "created",
            "comment": {"id": 1, "body": "@carter-omp go"},
            "issue": {"number": 7, "pull_request": {}},
        },
        "handle_pr_conversation",
    ),
    (
        "pull_request_review_comment",
        {"action": "created", "comment": {"id": 1, "body": "@carter-omp go"}, "pull_request": _BOT_PR},
        "handle_review",
    ),
    (
        "pull_request_review_comment",
        {"action": "created", "comment": {"id": 1, "body": "@carter-omp go"}, "pull_request": _HUMAN_PR},
        "review_pr",
    ),
]


def _routed_task(event_type: str, payload: dict) -> str:
    decision = route_authorized_event(
        event_type,
        payload,
        decision=AuthorizationDecision(
            authorized=True,
            reason="authorized_label_trigger",
            trigger_kind="label",
            trigger_value="carter-omp",
            actor=Actor(id=12345678, login="carterlasalle", type="User"),
        ),
        delivery_id="d-route",
        repo="octo/widget",
        bot_login="carter-omp",
    )
    assert decision.decision == "queue", decision.reason
    assert decision.task is not None
    return decision.task


@pytest.mark.parametrize(
    "event_type,payload,expected",
    _ROUTED_CASES,
    ids=[f"{e}-{p['action']}-{x}" for e, p, x in _ROUTED_CASES],
)
@pytest.mark.asyncio
async def test_dispatch_runs_the_task_the_router_assigned(
    settings: Settings,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    event_type: str,
    payload: dict,
    expected: str,
) -> None:
    """Every queued `(event, action)` pair MUST run the task `route` picked."""
    task = _routed_task(event_type, payload)
    assert task == expected

    calls: list[str] = []

    def _recorder(name: str):
        async def _fake(**_kwargs: object) -> None:
            calls.append(name)

        return _fake

    for name in _TASK_ENTRY_POINTS:
        monkeypatch.setattr(tasks, name, _recorder(name))

    row = EventRow(
        delivery_id="d-route",
        event_type=event_type,
        repo="octo/widget",
        issue_key="octo/widget#4",
        payload={**payload, "_carter_omp_task": task},
        received_at="2026-01-01T00:00:00Z",
        state="running",
        attempts=1,
        last_error=None,
    )
    await _make_pool(settings, db)._dispatch(row)  # noqa: SLF001

    assert calls == [expected]


async def test_stranded_issue_is_redispatched_with_the_failed_delivery_payload(
    settings: Settings, db: Database
) -> None:
    """A run that burned its retry budget must not leave its issue stuck.

    The failed event is terminal, so without a sweep the issue sits in
    `reproducing` with nothing queued to carry it — the 402 credit exhaustion
    on carterlasalle/autolight#4 left it that way for hours. The re-queue
    reuses the failed delivery's payload, so it dispatches the same routed task
    and resumes the same workspace.
    """
    settings.event_max_retries = 0
    settings.redispatch_max_per_issue = 1
    db.upsert_issue(key="octo/widget#4", repo="octo/widget", number=4, state="reproducing", branch="b")
    db.record_event(
        delivery_id="d1",
        event_type="issues",
        repo="octo/widget",
        issue_key="octo/widget#4",
        payload={"_carter_omp_task": "triage_issue", "action": "opened"},
        state="failed",
        last_error="omp agent error (stopReason=error): 402 Insufficient credits",
    )

    redispatched = await _make_pool(settings, db)._redispatch_stranded()  # noqa: SLF001

    assert redispatched == 1
    queued = [row for row in db.list_events(limit=10) if row.state == "queued"]
    assert [row.issue_key for row in queued] == ["octo/widget#4"]
    assert queued[0].payload["_carter_omp_task"] == "triage_issue"


async def test_stranded_issue_past_the_redispatch_cap_is_abandoned(settings: Settings, db: Database) -> None:
    """The sweep is bounded: one re-queue per issue, then it settles visibly.

    Without the cap a permanently-failing issue would be re-dispatched every
    interval, spending provider credits on each pass.
    """
    settings.event_max_retries = 0
    settings.redispatch_max_per_issue = 1
    db.upsert_issue(key="octo/widget#4", repo="octo/widget", number=4, state="reproducing", branch="b")
    db.record_event(
        delivery_id="redispatch-octo/widget__4-1",
        event_type="issues",
        repo="octo/widget",
        issue_key="octo/widget#4",
        payload={"_carter_omp_task": "triage_issue", "action": "opened"},
        state="failed",
        last_error="omp agent error (stopReason=error): 402 Insufficient credits",
    )

    redispatched = await _make_pool(settings, db)._redispatch_stranded()  # noqa: SLF001

    assert redispatched == 0
    issue = db.get_issue("octo/widget#4")
    assert issue is not None and issue.state == "abandoned"
    assert [row.state for row in db.list_events(limit=10)] == ["failed"]


async def test_cancelled_run_is_not_redispatched(settings: Settings, db: Database) -> None:
    """`@bot stop` must not be undone by the sweep.

    A cancel leaves the row `failed` while the issue keeps its in-flight state,
    so without the exclusion the sweeper would re-queue work a human just
    stopped. The recorded error is the worker's wrapper around the reason, which
    is why the check matches a substring.
    """
    settings.event_max_retries = 0
    settings.redispatch_max_per_issue = 1
    db.upsert_issue(key="octo/widget#4", repo="octo/widget", number=4, state="reproducing", branch="b")
    db.record_event(
        delivery_id="d1",
        event_type="issues",
        repo="octo/widget",
        issue_key="octo/widget#4",
        payload={"_carter_omp_task": "triage_issue", "action": "opened"},
        state="failed",
        last_error="RpcProcessExitError('cancelled by operator')",
    )

    assert db.stranded_issues(max_attempts=0) == []
    assert await _make_pool(settings, db)._redispatch_stranded() == 0  # noqa: SLF001
    assert [row.state for row in db.list_events(limit=5)] == ["failed"]


@pytest.mark.asyncio
async def test_reaper_removes_abandoned_workspace_without_live_event(
    settings: Settings, db: Database
) -> None:
    """A settled issue's worktree is pure disk pressure — the sweep removes it."""
    removed: list[tuple[str, int | str]] = []

    class _ReapingSandbox(_StubSandbox):
        def reap_settled_workspaces(self, keys) -> int:  # type: ignore[no-untyped-def]
            removed.extend(keys)
            return len(keys)

    def _pool() -> WorkerPool:
        pool = _make_pool(settings, db)
        pool.sandbox = _ReapingSandbox()  # type: ignore[assignment]
        return pool

    db.upsert_issue(key="octo/widget#4", repo="octo/widget", number=4, state="abandoned", branch="b")

    assert await _pool()._reap_settled_workspaces() == 1  # noqa: SLF001
    assert removed == [("octo/widget", 4)]


@pytest.mark.asyncio
async def test_reaper_skips_abandoned_workspace_with_live_event(
    settings: Settings, db: Database
) -> None:
    """An abandoned row with a queued event may be mid-reopen — never touch it."""
    removed: list[tuple[str, int | str]] = []

    class _ReapingSandbox(_StubSandbox):
        def reap_settled_workspaces(self, keys) -> int:  # type: ignore[no-untyped-def]
            removed.extend(keys)
            return len(keys)

    def _pool() -> WorkerPool:
        pool = _make_pool(settings, db)
        pool.sandbox = _ReapingSandbox()  # type: ignore[assignment]
        return pool

    db.upsert_issue(key="octo/widget#4", repo="octo/widget", number=4, state="abandoned", branch="b")
    db.record_event(
        delivery_id="d-reopen",
        event_type="issues",
        repo="octo/widget",
        issue_key="octo/widget#4",
        payload={"action": "reopened"},
        state="queued",
    )

    assert await _pool()._reap_settled_workspaces() == 0  # noqa: SLF001
    assert removed == []


@pytest.mark.asyncio
async def test_reaper_keeps_going_when_one_remove_fails(settings: Settings, db: Database) -> None:
    """One bad checkout must not pin every other settled worktree on disk."""
    removed: list[tuple[str, int | str]] = []

    class _FlakySandbox(_StubSandbox):
        def reap_settled_workspaces(self, keys) -> int:  # type: ignore[no-untyped-def]
            raise OSError("disk hiccup")

    def _pool() -> WorkerPool:
        pool = _make_pool(settings, db)
        pool.sandbox = _FlakySandbox()  # type: ignore[assignment]
        return pool

    db.upsert_issue(key="octo/widget#4", repo="octo/widget", number=4, state="abandoned", branch="b")
    db.upsert_issue(key="octo/widget#5", repo="octo/widget", number=5, state="abandoned", branch="b")

    assert await _pool()._reap_settled_workspaces() == 0  # noqa: SLF001
    assert removed == []
