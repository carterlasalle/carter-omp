"""Dispatch action -> task mapping in WorkerPool._dispatch."""

from __future__ import annotations

import pytest

from carter_omp import tasks
from carter_omp.config import Settings
from carter_omp.db import Database, EventRow
from carter_omp.queue import WorkerPool
from carter_omp.slot_pool import SlotPool


class _StubGitHub:
    """Sentinel; dispatch tests stub out the task body."""


class _StubSandbox:
    natives_cache = None

    def reclaim_workspace_caches(self, *, repo: str, number: int | str) -> bool:
        del repo, number
        return False

    def reclaim_all_caches(self) -> int:
        return 0


class _StubGitTransport:
    pass


def _make_pool(settings: Settings, db: Database, *, github: object | None = None) -> WorkerPool:
    return WorkerPool(
        settings=settings,
        db=db,
        github=github if github is not None else _StubGitHub(),  # type: ignore[arg-type]
        sandbox=_StubSandbox(),  # type: ignore[arg-type]
        git_transport=_StubGitTransport(),  # type: ignore[arg-type]
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


class _RecordingGitHub:
    def __init__(self) -> None:
        self.comments: list[tuple[str, int, str]] = []

    async def post_comment(self, repo: str, number: int, body: str) -> None:
        self.comments.append((repo, number, body))


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
    assert db.get_event("cmd-status").state == "done"
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
