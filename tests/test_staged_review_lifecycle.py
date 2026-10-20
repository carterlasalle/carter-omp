"""Staged PR review lifecycle tests (mocked GitHub via httpx.MockTransport)."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest
from omp_rpc import HostToolContext, RpcCommandError

from carter_omp.capabilities import PR_REVIEW_CAPABILITIES as REVIEW_CAPS
from carter_omp.db import Database
from carter_omp.github_client import GitHubClient, IssueInfo, RepoInfo
from carter_omp.host_tools import ToolBindings, build
from carter_omp.sandbox import LocalGitTransport, Workspace

# Unified diff whose anchorable lines are RIGHT {10..14} / LEFT {9..12};
# line 15+ is a gap (unanchorable) and RIGHT line 9 is before the hunk.
_PATCH = "@@ -9,5 +10,6 @@ def f():\n ctx1\n-old10\n+new11\n ctx2\n+new13\n ctx3\n"


def _stub_workspace(tmp_path: Path) -> Workspace:
    root = tmp_path / "ws"
    repo_dir = root / "repo"
    session_dir = root / ".omp-session"
    context_dir = root / "context"
    artifacts_dir = root / "artifacts"
    for p in (root, repo_dir, session_dir, context_dir, context_dir / "repro", artifacts_dir):
        p.mkdir(parents=True, exist_ok=True)
    return Workspace(
        root=root,
        repo_dir=repo_dir,
        session_dir=session_dir,
        context_dir=context_dir,
        artifacts_dir=artifacts_dir,
        branch="carter-omp/abc12345/some-issue",
        repo_full_name="octo/widget",
        issue_number=42,
    )


def _stub_repo() -> RepoInfo:
    return RepoInfo(
        full_name="octo/widget",
        default_branch="main",
        clone_url="https://x/octo/widget.git",
        private=False,
    )


def _make_loop_in_background() -> tuple[asyncio.AbstractEventLoop, threading.Thread]:
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    return loop, t


def _stop_loop(loop: asyncio.AbstractEventLoop, t: threading.Thread) -> None:
    loop.call_soon_threadsafe(loop.stop)
    t.join(timeout=2.0)
    loop.close()


def _review_bindings(
    db: Database,
    tmp_path: Path,
    transport: httpx.MockTransport,
    *,
    platform: str = "github",
) -> tuple[ToolBindings, asyncio.AbstractEventLoop, threading.Thread]:
    github = GitHubClient("token", transport=transport, platform=platform)
    loop, thread = _make_loop_in_background()
    issue = IssueInfo(
        repo="octo/widget",
        number=99,
        title="contributor PR",
        body="body",
        state="open",
        author="alice",
        labels=(),
        is_pull_request=True,
    )
    workspace = _stub_workspace(tmp_path)
    workspace.issue_number = 99
    bindings = ToolBindings(
        db=db,
        github=github,
        git_transport=LocalGitTransport(token=None),
        repo=_stub_repo(),
        issue=issue,
        workspace=workspace,
        loop=loop,
        author_name="carter_omp-bot",
        author_email="carter_omp-bot@example.invalid",
        inbound_thread_number=99,
        inbound_is_pr=True,
        review_mode=True,
        capabilities=REVIEW_CAPS,
    )
    db.upsert_issue(
        key=bindings.issue_key,
        repo="octo/widget",
        number=99,
        state="reviewing",
        branch=bindings.workspace.branch,
        session_dir=str(bindings.workspace.session_dir),
        pr_number=99,
    )
    return bindings, loop, thread


def _ctx() -> HostToolContext[Any]:
    return HostToolContext(tool_call_id="tc-1", _cancel_event=threading.Event(), _send_update=lambda _payload: None)


def _pr_files_response(request: httpx.Request, patch: str = _PATCH, *, status: int = 200) -> httpx.Response:
    if status != 200:
        return httpx.Response(status, json={"message": "files fetch failed"})
    return httpx.Response(
        200,
        json=[{"filename": "src/app.py", "status": "modified", "additions": 1, "deletions": 1, "patch": patch}],
    )


def _review_ok(body: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": 44,
            "user": {"login": "carter_omp-bot"},
            "body": body.get("body", "ok"),
            "state": "COMMENTED",
            "submitted_at": "t",
        },
    )


def _pr_payload(sha: str, number: int = 99) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "number": number,
            "html_url": f"https://x/octo/widget/pull/{number}",
            "head": {"ref": "fix-crash", "sha": sha},
            "base": {"ref": "main"},
            "state": "open",
            "user": {"login": "alice"},
        },
    )


def _require_tool(bindings: ToolBindings, name: str) -> Any:
    tool = next((x for x in build(bindings) if x.name == name), None)
    assert tool is not None, f"tool {name} not registered"
    return tool


def test_staged_lifecycle_01_stage_submit_happy_path(db: Database, tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/octo/widget/pulls/99/files":
            return _pr_files_response(request)
        if request.url.path.endswith("/reviews"):
            captured["path"] = request.url.path
            captured["body"] = json.loads(request.content)
            return _review_ok(captured["body"])
        return httpx.Response(404, json={"message": "unrouted"})

    bindings, loop, t = _review_bindings(db, tmp_path, httpx.MockTransport(handler))
    try:
        stage_tool = _require_tool(bindings, "pr_review_comment")
        submit_tool = _require_tool(bindings, "submit_pr_review")
        staged = stage_tool.execute(
            {
                "path": "src/app.py",
                "line": 12,
                "side": "RIGHT",
                "start_line": 10,
                "start_side": "RIGHT",
                "body": "blocking: this dereferences cfg before the guard.",
            },
            _ctx(),
        )
        assert "staged_count=1" in staged
        rows = db.list_staged_review_comments(bindings.issue_key)
        assert len(rows) == 1
        assert rows[0].path == "src/app.py"
        result = submit_tool.execute({"body": "review:p1 — one blocking issue", "event": "APPROVE"}, _ctx())
    finally:
        _stop_loop(loop, t)

    assert "submitted PR review" in result
    assert captured["path"].endswith("/pulls/99/reviews")
    assert captured["body"] == {
        "body": "review:p1 — one blocking issue",
        "event": "COMMENT",
        "comments": [
            {
                "path": "src/app.py",
                "line": 12,
                "side": "RIGHT",
                "body": "blocking: this dereferences cfg before the guard.",
                "start_line": 10,
                "start_side": "RIGHT",
            }
        ],
    }
    assert db.list_staged_review_comments(bindings.issue_key) == []


def test_staged_lifecycle_02_placeholder_rejected_when_path_not_in_diff(db: Database, tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/octo/widget/pulls/99/files":
            return _pr_files_response(request)
        return httpx.Response(404, json={"message": "unrouted"})

    bindings, loop, t = _review_bindings(db, tmp_path, httpx.MockTransport(handler))
    try:
        stage_tool = _require_tool(bindings, "pr_review_comment")
        with pytest.raises(RpcCommandError, match="not in the PR diff"):
            stage_tool.execute({"path": "y", "line": 1, "body": "x"}, _ctx())
    finally:
        _stop_loop(loop, t)
    assert db.list_staged_review_comments(bindings.issue_key) == []


def test_staged_lifecycle_03_invalid_args_rejected_with_zero_rows(db: Database, tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/octo/widget/pulls/99/files":
            return _pr_files_response(request)
        return httpx.Response(404, json={"message": "unrouted"})

    bindings, loop, t = _review_bindings(db, tmp_path, httpx.MockTransport(handler))
    try:
        stage_tool = _require_tool(bindings, "pr_review_comment")
        with pytest.raises(RpcCommandError, match="must be RIGHT or LEFT"):
            stage_tool.execute({"path": "src/app.py", "line": 12, "side": "UP", "body": "bad side"}, _ctx())
        assert db.list_staged_review_comments(bindings.issue_key) == []
        with pytest.raises(RpcCommandError, match="must be <= line"):
            stage_tool.execute(
                {
                    "path": "src/app.py",
                    "line": 14,
                    "side": "RIGHT",
                    "start_line": 15,
                    "start_side": "RIGHT",
                    "body": "reversed range",
                },
                _ctx(),
            )
        assert db.list_staged_review_comments(bindings.issue_key) == []
        with pytest.raises(RpcCommandError, match="positive integer 'line'"):
            stage_tool.execute({"path": "src/app.py", "line": 0, "body": "line zero"}, _ctx())
        assert db.list_staged_review_comments(bindings.issue_key) == []
        with pytest.raises(RpcCommandError, match="non-empty 'body'"):
            stage_tool.execute({"path": "src/app.py", "line": 12, "body": "   "}, _ctx())
        assert db.list_staged_review_comments(bindings.issue_key) == []
    finally:
        _stop_loop(loop, t)
    assert db.list_staged_review_comments(bindings.issue_key) == []


def test_staged_lifecycle_04_list_get_update_unstage_clear_lifecycle(db: Database, tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/octo/widget/pulls/99/files":
            return _pr_files_response(request)
        return httpx.Response(404, json={"message": "unrouted"})

    bindings, loop, t = _review_bindings(db, tmp_path, httpx.MockTransport(handler))
    try:
        stage_tool = _require_tool(bindings, "pr_review_comment")
        list_tool = _require_tool(bindings, "list_staged_pr_review_comments")
        get_tool = _require_tool(bindings, "get_staged_pr_review_comment")
        update_tool = _require_tool(bindings, "update_staged_pr_review_comment")
        unstage_tool = _require_tool(bindings, "unstage_pr_review_comment")
        clear_tool = _require_tool(bindings, "clear_staged_pr_review_comments")

        stage_tool.execute({"path": "src/app.py", "line": 12, "body": "first"}, _ctx())
        stage_tool.execute({"path": "src/app.py", "line": 13, "body": "second"}, _ctx())
        rows = db.list_staged_review_comments(bindings.issue_key)
        assert len(rows) == 2
        first_id, second_id = rows[0].id, rows[1].id

        out = list_tool.execute({}, _ctx())
        assert "src/app.py" in out and "first" in out and "second" in out

        out = get_tool.execute({"id": first_id}, _ctx())
        assert "src/app.py" in out and "first" in out
        with pytest.raises(RpcCommandError):
            get_tool.execute({"id": 999999}, _ctx())

        # DAO get: hit and miss.
        row = db.get_staged_review_comment(bindings.issue_key, first_id)
        assert row is not None and row.path == "src/app.py" and row.line == 12
        assert db.get_staged_review_comment(bindings.issue_key, 999999) is None

        out = update_tool.execute({"id": first_id, "line": 11, "body": "first-edited"}, _ctx())
        assert out.startswith(f"updated staged review comment #{first_id}")
        row = db.get_staged_review_comment(bindings.issue_key, first_id)
        assert row is not None and row.line == 11 and row.body == "first-edited"

        # Update revalidation: moving onto the line-15 gap must be rejected, row unchanged.
        with pytest.raises(RpcCommandError, match="not anchorable"):
            update_tool.execute({"id": first_id, "line": 15}, _ctx())
        row = db.get_staged_review_comment(bindings.issue_key, first_id)
        assert row is not None and row.line == 11 and row.body == "first-edited"

        # Explicit null clears the range while keeping the rest.
        update_tool.execute({"id": first_id, "start_line": 10, "start_side": "RIGHT"}, _ctx())
        row = db.get_staged_review_comment(bindings.issue_key, first_id)
        assert row is not None and row.start_line == 10 and row.start_side == "RIGHT"
        update_tool.execute({"id": first_id, "start_line": None, "start_side": None}, _ctx())
        row = db.get_staged_review_comment(bindings.issue_key, first_id)
        assert row is not None and row.start_line is None and row.start_side is None
        assert row.line == 11 and row.body == "first-edited"

        # DAO update takes the full merged field set; unknown id returns None.
        merged = db.update_staged_review_comment(
            bindings.issue_key,
            first_id,
            path=row.path,
            line=row.line,
            side=row.side,
            start_line=row.start_line,
            start_side=row.start_side,
            body="via-dao",
        )
        assert merged is not None and merged.body == "via-dao" and merged.line == 11
        assert (
            db.update_staged_review_comment(
                bindings.issue_key,
                999999,
                path="src/app.py",
                line=11,
                side="RIGHT",
                start_line=None,
                start_side=None,
                body="nope",
            )
            is None
        )

        with pytest.raises(RpcCommandError):
            unstage_tool.execute({"id": 999999}, _ctx())
        unstage_tool.execute({"id": second_id}, _ctx())
        remaining = db.list_staged_review_comments(bindings.issue_key)
        assert [r.id for r in remaining] == [first_id]
        # DAO delete: already-removed id returns False.
        assert db.delete_staged_review_comment(bindings.issue_key, second_id) is False

        clear_tool.execute({}, _ctx())
        assert db.list_staged_review_comments(bindings.issue_key) == []
    finally:
        _stop_loop(loop, t)


def test_staged_lifecycle_05_unstage_preserves_siblings(db: Database, tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/octo/widget/pulls/99/files":
            return _pr_files_response(request)
        return httpx.Response(404, json={"message": "unrouted"})

    bindings, loop, t = _review_bindings(db, tmp_path, httpx.MockTransport(handler))
    try:
        stage_tool = _require_tool(bindings, "pr_review_comment")
        unstage_tool = _require_tool(bindings, "unstage_pr_review_comment")
        stage_tool.execute({"path": "src/app.py", "line": 10, "body": "one"}, _ctx())
        stage_tool.execute({"path": "src/app.py", "line": 11, "body": "two"}, _ctx())
        stage_tool.execute({"path": "src/app.py", "line": 12, "body": "three"}, _ctx())
        rows = db.list_staged_review_comments(bindings.issue_key)
        assert len(rows) == 3
        unstage_tool.execute({"id": rows[1].id}, _ctx())
        remaining = db.list_staged_review_comments(bindings.issue_key)
        assert [r.body for r in remaining] == ["one", "three"]
    finally:
        _stop_loop(loop, t)


def test_staged_lifecycle_06_stale_anchors_dropped_at_submit_with_diagnostics(db: Database, tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/octo/widget/pulls/99/files":
            return _pr_files_response(request)
        if request.url.path.endswith("/reviews"):
            captured["body"] = json.loads(request.content)
            return _review_ok(captured["body"])
        return httpx.Response(404, json={"message": "unrouted"})

    bindings, loop, t = _review_bindings(db, tmp_path, httpx.MockTransport(handler))
    try:
        # Bypass staging-time validation to simulate rows that went stale.
        db.stage_review_comment(issue_key=bindings.issue_key, path="src/app.py", line=12, body="kept")
        db.stage_review_comment(issue_key=bindings.issue_key, path="src/app.py", line=15, body="gap finding")
        db.stage_review_comment(issue_key=bindings.issue_key, path="src/other.py", line=12, body="wrong path")
        db.stage_review_comment(
            issue_key=bindings.issue_key, path="src/app.py", line=13, side="LEFT", body="side mismatch"
        )
        submit_tool = _require_tool(bindings, "submit_pr_review")
        result = submit_tool.execute({"body": "summary"}, _ctx())
    finally:
        _stop_loop(loop, t)

    assert captured["body"]["comments"] == [{"path": "src/app.py", "line": 12, "side": "RIGHT", "body": "kept"}]
    assert "dropped=3" in result
    assert "## Not anchored to diff" in captured["body"]["body"]
    assert "**`src/app.py:15`** — gap finding" in captured["body"]["body"]
    assert "**`src/other.py:12`** — wrong path" in captured["body"]["body"]
    assert "**`src/app.py:13`** — side mismatch" in captured["body"]["body"]
    assert db.list_staged_review_comments(bindings.issue_key) == []


def test_staged_lifecycle_07_renamed_deleted_multiline_patchless_cases(db: Database, tmp_path: Path) -> None:
    captured: dict[str, Any] = {}
    files = [
        {"filename": "src/app.py", "status": "modified", "additions": 1, "deletions": 1, "patch": _PATCH},
        {"filename": "src/new.py", "status": "renamed", "additions": 1, "deletions": 1, "patch": _PATCH},
        {"filename": "src/binary.py", "status": "modified", "additions": 0, "deletions": 0, "patch": ""},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/octo/widget/pulls/99/files":
            return httpx.Response(200, json=files)
        if request.url.path.endswith("/reviews"):
            captured["body"] = json.loads(request.content)
            return _review_ok(captured["body"])
        return httpx.Response(404, json={"message": "unrouted"})

    bindings, loop, t = _review_bindings(db, tmp_path, httpx.MockTransport(handler))
    try:
        stage_tool = _require_tool(bindings, "pr_review_comment")
        # Patchless file has no diff lines: refused at staging, because one
        # such anchor 422s the whole review at submit time.
        with pytest.raises(RpcCommandError) as exc:
            stage_tool.execute({"path": "src/binary.py", "line": 12, "body": "binary finding"}, _ctx())
        assert "no diff to anchor to" in str(exc.value)
        # Bypass validation for the stale/renamed/range/patchless rows.
        db.stage_review_comment(issue_key=bindings.issue_key, path="src/app.py", line=12, body="single kept")
        db.stage_review_comment(issue_key=bindings.issue_key, path="src/old.py", line=12, body="renamed away")
        db.stage_review_comment(issue_key=bindings.issue_key, path="src/binary.py", line=12, body="binary finding")
        db.stage_review_comment(
            issue_key=bindings.issue_key,
            path="src/app.py",
            line=12,
            side="RIGHT",
            start_line=10,
            start_side="RIGHT",
            body="valid range",
        )
        db.stage_review_comment(
            issue_key=bindings.issue_key,
            path="src/app.py",
            line=12,
            side="RIGHT",
            start_line=15,
            start_side="RIGHT",
            body="bad range",
        )
        submit_tool = _require_tool(bindings, "submit_pr_review")
        result = submit_tool.execute({"body": "summary"}, _ctx())
    finally:
        _stop_loop(loop, t)

    kept = {(c["path"], c["line"], c["body"]) for c in captured["body"]["comments"]}
    assert ("src/app.py", 12, "single kept") in kept
    assert ("src/app.py", 12, "valid range") in kept
    assert all(c["path"] != "src/binary.py" for c in captured["body"]["comments"])
    assert all(c["path"] != "src/old.py" for c in captured["body"]["comments"])
    assert not any(c["body"] == "bad range" for c in captured["body"]["comments"])
    assert "dropped=3" in result
    assert "src/binary.py:12" in captured["body"]["body"]
    assert "src/old.py:12" in captured["body"]["body"]
    assert db.list_staged_review_comments(bindings.issue_key) == []


def test_staged_lifecycle_08_head_moved_submit_rejection(db: Database, tmp_path: Path) -> None:
    posts: list[str] = []
    old_sha = "a" * 40
    new_sha = "b" * 40

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/octo/widget/pulls/99/files":
            return _pr_files_response(request)
        if request.url.path == "/repos/octo/widget/pulls/99":
            return _pr_payload(new_sha)
        if request.url.path.endswith("/reviews"):
            posts.append(request.url.path)
            return _review_ok({})
        return httpx.Response(404, json={"message": "unrouted"})

    bindings, loop, t = _review_bindings(db, tmp_path, httpx.MockTransport(handler))
    try:
        stage_tool = _require_tool(bindings, "pr_review_comment")
        submit_tool = _require_tool(bindings, "submit_pr_review")
        stage_tool.execute({"path": "src/app.py", "line": 12, "body": "finding"}, _ctx())
        db.set_review_head(bindings.issue_key, old_sha)
        with pytest.raises(RpcCommandError, match="PR head moved"):
            submit_tool.execute({"body": "summary"}, _ctx())
    finally:
        _stop_loop(loop, t)

    assert posts == []
    rows = db.list_staged_review_comments(bindings.issue_key)
    assert len(rows) == 1
    assert not db.has_successful_tool_call(bindings.issue_key, "submit_pr_review")


@pytest.mark.parametrize("status", [422, 500])
def test_staged_lifecycle_09_fallback_posts_only_anchorable_comments(db: Database, tmp_path: Path, status: int) -> None:
    comment_bodies: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/octo/widget/pulls/99/files":
            return _pr_files_response(request)
        if request.url.path.endswith("/reviews"):
            return httpx.Response(status, json={"message": "rejected"})
        if request.url.path == "/repos/octo/widget/issues/99/comments":
            if request.method == "GET":
                return httpx.Response(200, json=[])
            body = json.loads(request.content)["body"]
            comment_bodies.append(body)
            return httpx.Response(200, json={"id": len(comment_bodies), "body": body})
        return httpx.Response(404, json={"message": "unrouted"})

    bindings, loop, t = _review_bindings(db, tmp_path, httpx.MockTransport(handler))
    try:
        stage_tool = _require_tool(bindings, "pr_review_comment")
        submit_tool = _require_tool(bindings, "submit_pr_review")
        stage_tool.execute({"path": "src/app.py", "line": 12, "body": "anchorable finding"}, _ctx())
        db.stage_review_comment(issue_key=bindings.issue_key, path="src/app.py", line=15, body="gap finding")
        result = submit_tool.execute({"body": "summary"}, _ctx())
    finally:
        _stop_loop(loop, t)

    assert f"review rejected ({status}" in result
    assert len(comment_bodies) == 2
    assert "## Not anchored to diff" in comment_bodies[0]
    assert "**`src/app.py:15`** — gap finding" in comment_bodies[0]
    assert comment_bodies[1] == "**`src/app.py:12`**\n\nanchorable finding"
    # The dropped comment is folded into the summary; it is never posted standalone.
    assert not any(body == "**`src/app.py:15`**\n\ngap finding" for body in comment_bodies)
    assert db.list_staged_review_comments(bindings.issue_key) == []


def test_staged_lifecycle_10_retry_idempotency_no_dupes(db: Database, tmp_path: Path) -> None:
    review_posts: list[dict[str, Any]] = []

    def success_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/octo/widget/pulls/99/files":
            return _pr_files_response(request)
        if request.url.path.endswith("/reviews"):
            body = json.loads(request.content)
            review_posts.append(body)
            return _review_ok(body)
        return httpx.Response(404, json={"message": "unrouted"})

    bindings, loop, t = _review_bindings(db, tmp_path, httpx.MockTransport(success_handler))
    try:
        stage_tool = _require_tool(bindings, "pr_review_comment")
        submit_tool = _require_tool(bindings, "submit_pr_review")
        stage_tool.execute({"path": "src/app.py", "line": 12, "body": "finding"}, _ctx())
        first = submit_tool.execute({"body": "summary"}, _ctx())
        submit_tool.execute({"body": "summary"}, _ctx())
    finally:
        _stop_loop(loop, t)

    assert "submitted PR review" in first
    assert len(review_posts) == 2
    assert len(review_posts[0]["comments"]) == 1
    assert review_posts[1]["comments"] == []
    assert db.list_staged_review_comments(bindings.issue_key) == []

    # Fallback path: the second identical fallback must not post duplicate issue comments.
    db.clear_staged_review_comments(bindings.issue_key)
    issue_posts: list[str] = []

    def fallback_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/octo/widget/pulls/99/files":
            return _pr_files_response(request)
        if request.url.path.endswith("/reviews"):
            return httpx.Response(422, json={"message": "Validation failed"})
        if request.url.path == "/repos/octo/widget/issues/99/comments":
            if request.method == "GET":
                return httpx.Response(200, json=[])
            body = json.loads(request.content)["body"]
            issue_posts.append(body)
            return httpx.Response(200, json={"id": len(issue_posts), "body": body})
        return httpx.Response(404, json={"message": "unrouted"})

    bindings2, loop2, t2 = _review_bindings(db, tmp_path, httpx.MockTransport(fallback_handler))
    try:
        stage_tool2 = _require_tool(bindings2, "pr_review_comment")
        submit_tool2 = _require_tool(bindings2, "submit_pr_review")
        stage_tool2.execute({"path": "src/app.py", "line": 12, "body": "finding"}, _ctx())
        submit_tool2.execute({"body": "summary"}, _ctx())
        posted_after_first = list(issue_posts)
        assert len(posted_after_first) == 2
        submit_tool2.execute({"body": "summary"}, _ctx())
    finally:
        _stop_loop(loop2, t2)

    assert issue_posts == posted_after_first, "fallback retry must not duplicate issue comments"
    assert db.list_staged_review_comments(bindings2.issue_key) == []


def test_staged_lifecycle_11_resume_staged_rows_visible_to_new_bindings(db: Database, tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/octo/widget/pulls/99/files":
            return _pr_files_response(request)
        return httpx.Response(404, json={"message": "unrouted"})

    bindings, loop, t = _review_bindings(db, tmp_path, httpx.MockTransport(handler))
    try:
        stage_tool = _require_tool(bindings, "pr_review_comment")
        stage_tool.execute({"path": "src/app.py", "line": 12, "body": "resume me"}, _ctx())
        issue_key = bindings.issue_key
    finally:
        _stop_loop(loop, t)

    bindings2, loop2, t2 = _review_bindings(db, tmp_path, httpx.MockTransport(handler))
    try:
        assert bindings2.issue_key == issue_key
        rows = db.list_staged_review_comments(bindings2.issue_key)
        assert [(r.path, r.line, r.body) for r in rows] == [("src/app.py", 12, "resume me")]
        list_tool = _require_tool(bindings2, "list_staged_pr_review_comments")
        out = list_tool.execute({}, _ctx())
        assert "resume me" in out
    finally:
        _stop_loop(loop2, t2)


def test_staged_lifecycle_12_preview_zero_mutation(db: Database, tmp_path: Path) -> None:
    posts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/octo/widget/pulls/99/files":
            return _pr_files_response(request)
        if request.url.path == "/repos/octo/widget/pulls/99":
            return _pr_payload("c" * 40)
        if request.method == "POST":
            posts.append(f"{request.method} {request.url.path}")
            return httpx.Response(500, json={"message": "must not POST"})
        return httpx.Response(404, json={"message": "unrouted"})

    bindings, loop, t = _review_bindings(db, tmp_path, httpx.MockTransport(handler))
    try:
        stage_tool = _require_tool(bindings, "pr_review_comment")
        preview_tool = _require_tool(bindings, "preview_pr_review")
        stage_tool.execute({"path": "src/app.py", "line": 12, "body": "kept"}, _ctx())
        db.stage_review_comment(issue_key=bindings.issue_key, path="src/app.py", line=15, body="gap")
        before = db.list_staged_review_comments(bindings.issue_key)
        assert len(before) == 2
        out = preview_tool.execute({}, _ctx())
        assert out.startswith(f"preview: 2 staged comment(s) for {bindings.issue_key}")
        assert "## Inline (1)" in out
        assert "## Excluded (1)" in out
        assert "## Validation" in out
        assert "src/app.py:12" in out
        assert "src/app.py:15" in out
        after = db.list_staged_review_comments(bindings.issue_key)
        assert [(r.path, r.line, r.body) for r in after] == [(r.path, r.line, r.body) for r in before]
    finally:
        _stop_loop(loop, t)

    assert posts == [], "preview must not issue GitHub writes"


def test_staged_lifecycle_13_cross_issue_isolation(db: Database, tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/octo/widget/pulls/99/files":
            return _pr_files_response(request)
        return httpx.Response(404, json={"message": "unrouted"})

    bindings, loop, t = _review_bindings(db, tmp_path, httpx.MockTransport(handler))
    try:
        stage_tool = _require_tool(bindings, "pr_review_comment")
        clear_tool = _require_tool(bindings, "clear_staged_pr_review_comments")
        stage_tool.execute({"path": "src/app.py", "line": 12, "body": "mine"}, _ctx())
        other_key = "octo/widget#42"
        db.stage_review_comment(issue_key=other_key, path="src/app.py", line=11, body="theirs")
        assert [r.body for r in db.list_staged_review_comments(bindings.issue_key)] == ["mine"]
        assert [r.body for r in db.list_staged_review_comments(other_key)] == ["theirs"]

        clear_tool.execute({}, _ctx())
        assert db.list_staged_review_comments(bindings.issue_key) == []
        assert [r.body for r in db.list_staged_review_comments(other_key)] == ["theirs"]
    finally:
        _stop_loop(loop, t)


def test_staged_lifecycle_14_classification_review_once_sanity(db: Database, tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/octo/widget/pulls/99/files":
            return _pr_files_response(request)
        if request.url.path.endswith("/reviews"):
            captured["body"] = json.loads(request.content)
            return _review_ok(captured["body"])
        return httpx.Response(404, json={"message": "unrouted"})

    bindings, loop, t = _review_bindings(db, tmp_path, httpx.MockTransport(handler))
    try:
        assert not db.has_successful_tool_call(bindings.issue_key, "submit_pr_review")
        stage_tool = _require_tool(bindings, "pr_review_comment")
        stage_tool.execute({"path": "src/app.py", "line": 12, "body": "finding"}, _ctx())
        # Staging (and lifecycle reads) never satisfy the review-once ledger.
        assert not db.has_successful_tool_call(bindings.issue_key, "submit_pr_review")
        list_tool = _require_tool(bindings, "list_staged_pr_review_comments")
        list_tool.execute({}, _ctx())
        assert not db.has_successful_tool_call(bindings.issue_key, "submit_pr_review")
        row = db.list_staged_review_comments(bindings.issue_key)[0]
        get_tool = _require_tool(bindings, "get_staged_pr_review_comment")
        get_tool.execute({"id": row.id}, _ctx())
        assert not db.has_successful_tool_call(bindings.issue_key, "submit_pr_review")
        preview_tool = _require_tool(bindings, "preview_pr_review")
        try:
            preview_tool.execute({}, _ctx())
        except RpcCommandError:
            pass
        assert not db.has_successful_tool_call(bindings.issue_key, "submit_pr_review")
        submit_tool = _require_tool(bindings, "submit_pr_review")
        submit_tool.execute({"body": "summary"}, _ctx())
    finally:
        _stop_loop(loop, t)

    assert db.has_successful_tool_call(bindings.issue_key, "submit_pr_review")
