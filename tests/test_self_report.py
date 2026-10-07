"""Self-report: the bot filing its own friction reports against the harness.

Any authorized run may report a harness defect ("this tool rejected a valid
call", "this error lied about the cause", "this cost an hour"). The report
becomes a tracked issue on the configured self-report repo, labelled
`bot-report` plus the deployment's trigger label, deduped by title so a repeat
appends evidence instead of filing another issue.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from carter_omp.config import Settings
from carter_omp.host_tools import build
from carter_omp.proxy_client import GitHubProxyClient
from carter_omp.proxy_hmac import HEADER_RUN_TOKEN
from tests.test_host_tools import _bindings, _ctx, _stop_loop
from tests.test_proxy_server import _HMAC, _async_client, _build_app, _build_settings, _run_token, _signed

SELF_REPO = "octo/self"
ENDPOINT = "/gh/v1/self-report"


@pytest.fixture
def proxy_settings(tmp_path: Path) -> Settings:
    cfg = _build_settings(tmp_path)
    cfg.repo_allowlist_raw = "octo/widget,octo/self"
    cfg.self_report_repo = SELF_REPO
    cfg.trigger_label = "carter-omp"
    return cfg


def _report_body(title: str = "gh_push_branch rejects a renamed branch") -> bytes:
    return json.dumps(
        {
            "title": title,
            "body": "**Expected** the push to succeed.\n\n**Actual** 403 branch mismatch.",
            "severity": "high",
        }
    ).encode()


def _headers(body: bytes, *, capabilities: set[str] | None = None) -> dict[str, str]:
    token = _run_token(
        issue=4,
        branch="carter-omp/abc123/fix-x",
        capabilities=capabilities if capabilities is not None else {"report_upstream"},
    )
    return {
        **_signed("POST", ENDPOINT, body, run_token=token),
        HEADER_RUN_TOKEN: token,
        "Content-Type": "application/json",
    }


async def test_self_report_files_one_labelled_issue_with_run_provenance(proxy_settings: Settings) -> None:
    captured: dict[str, Any] = {}

    def gh(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json=[])
        captured["create"] = json.loads(req.content)
        captured["path"] = req.url.path
        return httpx.Response(
            201,
            json={
                "number": 41,
                "title": captured["create"]["title"],
                "state": "open",
                "labels": [{"name": "bot-report"}],
                "user": {"login": "carter-omp[bot]"},
                "html_url": "https://github.com/octo/self/issues/41",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "comments": 0,
            },
        )

    app = _build_app(proxy_settings, gh)
    body = _report_body()
    async with await _async_client(app) as client:
        resp = await client.post(ENDPOINT, content=body, headers=_headers(body))

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload == {
        "number": 41,
        "url": "https://github.com/octo/self/issues/41",
        "created": True,
        "detail": "filed report #41",
    }
    assert captured["path"] == "/repos/octo/self/issues"
    created = captured["create"]
    # Title carries the dedupe marker; labels are the report label, the
    # deployment's trigger label, and the severity bucket.
    assert created["title"] == "[bot-report] gh_push_branch rejects a renamed branch"
    assert set(created["labels"]) == {"bot-report", "carter-omp", "prio:p1"}
    # Provenance is built from the run token (repo#thread + the token's run id),
    # never from anything the caller sent.
    assert "octo/widget#4" in created["body"]
    assert "run-test" in created["body"]  # `_run_token` mints run_id=run-test


async def test_self_report_appends_to_the_matching_open_report(proxy_settings: Settings) -> None:
    """A repeated fault is evidence on the existing issue, not a new issue."""
    calls: list[str] = []

    def gh(req: httpx.Request) -> httpx.Response:
        calls.append(f"{req.method} {req.url.path}")
        if req.method == "GET":
            return httpx.Response(
                200,
                json=[
                    {
                        "number": 7,
                        "title": "[bot-report] gh_push_branch rejects a renamed branch",
                        "state": "open",
                        "labels": [{"name": "bot-report"}],
                        "user": {"login": "carter-omp[bot]"},
                        "html_url": "https://github.com/octo/self/issues/7",
                        "created_at": "2026-01-01T00:00:00Z",
                        "updated_at": "2026-01-01T00:00:00Z",
                        "comments": 1,
                    }
                ],
            )
        return httpx.Response(201, json={"id": 1, "body": "again"})

    app = _build_app(proxy_settings, gh)
    body = _report_body()
    async with await _async_client(app) as client:
        resp = await client.post(ENDPOINT, content=body, headers=_headers(body))

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["created"] is False
    assert payload["number"] == 7
    assert "POST /repos/octo/self/issues/7/comments" in calls
    assert "POST /repos/octo/self/issues" not in calls


async def test_self_report_requires_the_capability(proxy_settings: Settings) -> None:
    app = _build_app(proxy_settings, lambda _: httpx.Response(200, json=[]))
    body = _report_body()
    async with await _async_client(app) as client:
        resp = await client.post(ENDPOINT, content=body, headers=_headers(body, capabilities={"comment"}))
    assert resp.status_code == 403
    assert "report_upstream" in resp.text


async def test_self_report_is_disabled_without_a_target_repo(proxy_settings: Settings) -> None:
    proxy_settings.self_report_repo = ""
    app = _build_app(proxy_settings, lambda _: httpx.Response(200, json=[]))
    body = _report_body()
    async with await _async_client(app) as client:
        resp = await client.post(ENDPOINT, content=body, headers=_headers(body))
    assert resp.status_code == 404


async def test_self_report_rejects_an_unknown_severity(proxy_settings: Settings) -> None:
    app = _build_app(proxy_settings, lambda _: httpx.Response(200, json=[]))
    body = json.dumps({"title": "t", "body": "b", "severity": "catastrophic"}).encode()
    async with await _async_client(app) as client:
        resp = await client.post(ENDPOINT, content=body, headers=_headers(body))
    assert resp.status_code == 400


# ---------- the host tool ----------


def test_report_pain_point_tool_files_through_the_backend(db, tmp_path: Path, settings: Settings) -> None:
    """The tool passes the model's description and the run's context; the
    destination and provenance stay host-side."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[])
        seen["path"] = request.url.path
        seen["payload"] = json.loads(request.content)
        return httpx.Response(
            201,
            json={
                "number": 12,
                "title": seen["payload"]["title"],
                "state": "open",
                "labels": [],
                "user": {"login": "carter-omp[bot]"},
                "html_url": "https://github.com/octo/self/issues/12",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "comments": 0,
            },
        )

    bindings, loop, thread = _bindings(db, tmp_path, httpx.MockTransport(handler))
    settings.self_report_repo = SELF_REPO
    object.__setattr__(bindings, "settings", settings)
    try:
        tool = next(x for x in build(bindings) if x.name == "report_pain_point")
        result = tool.execute(
            {
                "title": "gate blocks an untraced file",
                "details": "`trace verify` rejected the write although the marker was present.",
                "severity": "high",
                "area": "gate",
            },
            _ctx(),
        )
    finally:
        _stop_loop(loop, thread)

    assert seen["path"] == "/repos/octo/self/issues"
    assert seen["payload"]["title"] == "[bot-report] gate: gate blocks an untraced file"
    assert seen["payload"]["labels"] == ["bot-report", "prio:p1"]
    payload = json.loads(result)
    assert payload["number"] == 12
    assert payload["created"] is True
    assert payload["url"].endswith("/issues/12")


def _self_report_handler(calls: list[str], *, created_number: int = 12):
    """Mock GitHub for the tool path: create + repo metadata + issue read."""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.method == "GET" and request.url.path == "/repos/octo/self/issues":
            return httpx.Response(200, json=[])
        if request.method == "GET" and request.url.path == "/repos/octo/self":
            return httpx.Response(
                200,
                json={
                    "full_name": "octo/self",
                    "default_branch": "main",
                    "clone_url": "https://x/self.git",
                    "private": False,
                },
            )
        if request.url.path.endswith("/comments"):
            return httpx.Response(201, json={"id": 2, "body": "appended", "user": {"login": "carter-omp[bot]"}})
        if request.method == "GET":  # get_issue for the enqueued payload / verification
            return httpx.Response(
                200,
                json={
                    "number": created_number,
                    "title": "[bot-report] gate: gate blocks an untraced file",
                    "body": "expected/actual",
                    "state": "open",
                    "labels": [{"name": "bot-report"}],
                    "user": {"login": "carter-omp[bot]"},
                },
            )
        return httpx.Response(
            201,
            json={
                "number": created_number,
                "title": "[bot-report] gate: gate blocks an untraced file",
                "state": "open",
                "labels": [],
                "user": {"login": "carter-omp[bot]"},
                "html_url": f"https://github.com/octo/self/issues/{created_number}",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "comments": 0,
            },
        )

    return handler


def _run_tool(db, tmp_path, settings: Settings, handler, *, repo: str = "octo/widget") -> dict[str, Any]:
    from carter_omp.github_client import RepoInfo

    bindings, loop, thread = _bindings(db, tmp_path, httpx.MockTransport(handler))
    settings.self_report_repo = SELF_REPO
    object.__setattr__(bindings, "settings", settings)
    object.__setattr__(bindings, "repo", RepoInfo(full_name=repo, default_branch="main", clone_url="", private=False))
    try:
        tool = next(x for x in build(bindings) if x.name == "report_pain_point")
        return json.loads(
            tool.execute(
                {
                    "title": "gate blocks an untraced file",
                    "details": "expected/actual",
                    "area": "gate",
                    "severity": "high",
                },
                _ctx(),
            )
        )
    finally:
        _stop_loop(loop, thread)


def test_report_pain_point_requires_the_capability(db, tmp_path: Path) -> None:
    """A run whose trigger granted no report capability cannot file anything."""
    from carter_omp.capabilities import Capability

    bindings, loop, thread = _bindings(db, tmp_path, httpx.MockTransport(lambda _: httpx.Response(201, json={})))
    object.__setattr__(
        bindings,
        "capabilities",
        frozenset({Capability.READ_REPO, Capability.RUN_COMMANDS}),
    )
    try:
        names = {x.name for x in build(bindings)}
    finally:
        _stop_loop(loop, thread)
    assert "report_pain_point" not in names


def test_github_proxy_client_ignores_caller_supplied_repo() -> None:
    """The proxy client must not forward a caller-chosen destination repo."""
    sent: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"number": 1, "created": True, "url": "u", "detail": "d"})

    client = GitHubProxyClient(base_url="http://proxy.test", hmac_key=_HMAC, transport=httpx.MockTransport(handler))
    import asyncio

    result = asyncio.run(
        client.self_report(repo="attacker/elsewhere", title="t", body="b", severity="low", provenance="x")
    )
    assert result == {"number": 1, "created": True, "url": "u", "detail": "d"}
    assert "repo" not in sent["payload"]
    assert "provenance" not in sent["payload"]


async def test_self_report_appends_to_a_known_issue_without_listing(proxy_settings: Settings) -> None:
    """The orchestrator's fingerprint hit short-circuits GitHub's laggy list."""
    calls: list[str] = []

    def gh(req: httpx.Request) -> httpx.Response:
        calls.append(f"{req.method} {req.url.path}")
        if req.method == "GET":
            return httpx.Response(
                200,
                json={
                    "number": 7,
                    "title": "[bot-report] gate blocks an untraced file",
                    "state": "open",
                    "labels": [{"name": "bot-report"}],
                    "user": {"login": "carter-omp[bot]"},
                },
            )
        return httpx.Response(201, json={"id": 1, "body": "again"})

    app = _build_app(proxy_settings, gh)
    body = json.dumps(
        {
            "title": "gate blocks an untraced file",
            "body": "expected/actual",
            "severity": "medium",
            "issue_number": 7,
        }
    ).encode()
    async with await _async_client(app) as client:
        resp = await client.post(ENDPOINT, content=body, headers=_headers(body))

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["created"] is False
    assert payload["number"] == 7
    assert calls == ["GET /repos/octo/self/issues/7", "POST /repos/octo/self/issues/7/comments"]


async def test_self_report_refuses_to_append_to_a_non_report_issue(proxy_settings: Settings) -> None:
    """A fingerprint hit must not become a comment on an unrelated issue."""

    def gh(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(
                200,
                json={
                    "number": 3,
                    "title": "Deploy and CI drift",
                    "state": "open",
                    "labels": [],
                    "user": {"login": "carterlasalle"},
                },
            )
        raise AssertionError("must not comment on a non-report issue")

    app = _build_app(proxy_settings, gh)
    body = json.dumps({"title": "t", "body": "b", "severity": "low", "issue_number": 3}).encode()
    async with await _async_client(app) as client:
        resp = await client.post(ENDPOINT, content=body, headers=_headers(body))

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["created"] is False
    assert payload["number"] is None
    assert "not a" in payload["detail"]


def test_report_pain_point_appends_on_the_second_call(db, tmp_path: Path, settings: Settings) -> None:
    """Two runs hitting the same fault converge on one issue via the DB."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.url.path == "/repos/octo/self/issues" and request.method == "GET":
            return httpx.Response(200, json=[])
        if request.method == "GET":  # verify a known report
            return httpx.Response(
                200,
                json={
                    "number": 12,
                    "title": "[bot-report] gate: gate blocks an untraced file",
                    "state": "open",
                    "labels": [{"name": "bot-report"}],
                    "user": {"login": "carter-omp[bot]"},
                },
            )
        if request.url.path.endswith("/comments"):
            return httpx.Response(201, json={"id": 2, "body": "again"})
        return httpx.Response(
            201,
            json={
                "number": 12,
                "title": "[bot-report] gate: gate blocks an untraced file",
                "state": "open",
                "labels": [],
                "user": {"login": "carter-omp[bot]"},
                "html_url": "https://github.com/octo/self/issues/12",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "comments": 0,
            },
        )

    bindings, loop, thread = _bindings(db, tmp_path, httpx.MockTransport(handler))
    settings.self_report_repo = SELF_REPO
    object.__setattr__(bindings, "settings", settings)
    args = {"title": "gate blocks an untraced file", "details": "expected/actual", "area": "gate", "severity": "high"}
    try:
        tool = next(x for x in build(bindings) if x.name == "report_pain_point")
        first = json.loads(tool.execute(dict(args), _ctx()))
        second = json.loads(tool.execute(dict(args), _ctx()))
    finally:
        _stop_loop(loop, thread)

    assert first["created"] is True and first["number"] == 12
    assert second["created"] is False and second["number"] == 12
    assert "POST /repos/octo/self/issues/12/comments" in calls
    assert calls.count("POST /repos/octo/self/issues") == 1  # created once


def test_report_pain_point_dispatches_a_fix_run(db, tmp_path: Path, settings: Settings) -> None:
    """A filed report is worked immediately, not left for a human trigger.

    The report is created by the bot, so the router ignores the resulting
    webhook event (that guard is what stops runs triggering each other). The
    tool therefore queues the run itself, authorized by a `manual_cli` trigger.
    """
    from carter_omp import tasks
    from carter_omp.github_events import TriggerContext
    from carter_omp.manual_triage import manual_delivery_id

    calls: list[str] = []
    result = _run_tool(db, tmp_path, settings, _self_report_handler(calls))
    assert result["created"] is True

    delivery = manual_delivery_id(SELF_REPO, 12)
    assert result["dispatched"] == delivery
    row = db.get_event(delivery)
    assert row is not None and row.state == "queued"
    trigger = tasks._trigger_from_payload(row.payload)
    assert isinstance(trigger, TriggerContext)
    assert trigger.trigger_kind == "manual_cli"
    assert trigger.trigger_value == "self-report"
    assert trigger.issue_number == 12


def test_report_pain_point_does_not_dispatch_on_append(db, tmp_path: Path, settings: Settings) -> None:
    """Appended evidence means the fix is already underway."""
    from carter_omp.manual_triage import manual_delivery_id

    calls: list[str] = []
    first = _run_tool(db, tmp_path, settings, _self_report_handler(calls))
    assert first["created"] is True
    # Second distinct fault with the same title resolves through the fingerprint
    # and must not queue a second run.
    calls.clear()
    second = _run_tool(db, tmp_path, settings, _self_report_handler(calls))
    assert second["created"] is False
    assert "dispatched" not in second
    assert db.get_event(manual_delivery_id(SELF_REPO, 12)) is not None


def test_report_pain_point_dispatch_can_be_turned_off(db, tmp_path: Path, settings: Settings) -> None:
    from carter_omp.manual_triage import manual_delivery_id

    settings.self_report_dispatch = False
    calls: list[str] = []
    result = _run_tool(db, tmp_path, settings, _self_report_handler(calls))
    assert result["created"] is True
    assert "dispatched" not in result
    assert db.get_event(manual_delivery_id(SELF_REPO, 12)) is None


def test_report_pain_point_from_the_self_repo_does_not_dispatch(db, tmp_path: Path, settings: Settings) -> None:
    """Otherwise a harness bug fixed inside the harness would queue itself forever."""
    from carter_omp.manual_triage import manual_delivery_id

    calls: list[str] = []
    result = _run_tool(db, tmp_path, settings, _self_report_handler(calls), repo=SELF_REPO)
    assert result["created"] is True
    assert "dispatched" not in result
    assert db.get_event(manual_delivery_id(SELF_REPO, 12)) is None


def test_workflow_permission_rejection_is_translated() -> None:
    """The raw remote refusal is recognised so the push tool can name the fix."""
    from carter_omp.git_ops import WORKFLOW_PERMISSION_HINT, is_workflow_permission_error

    raw = (
        "! [remote rejected] HEAD -> carter-omp/x (refusing to allow a GitHub App to create or update "
        "workflow `.github/workflows/ci.yml` without `workflows` permission)"
    )
    assert is_workflow_permission_error(raw) is True
    assert is_workflow_permission_error("fatal: could not read from remote repository") is False
    assert "Workflows: Read and write" in WORKFLOW_PERMISSION_HINT


def test_report_pain_point_does_not_double_the_area_prefix(db, tmp_path: Path, settings: Settings) -> None:
    """`area="infra"` + a title already starting with "infra:" must not double up.

    Two reports for one fault (carter-omp#30 vs #33) came from exactly that:
    the titles differed, so neither the fingerprint nor the title fallback
    matched.
    """
    calls: list[str] = []
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.method == "GET" and request.url.path == "/repos/octo/self/issues":
            return httpx.Response(200, json=[])
        if request.method == "GET" and request.url.path == "/repos/octo/self":
            return httpx.Response(
                200,
                json={"full_name": "octo/self", "default_branch": "main", "clone_url": "x", "private": False},
            )
        if request.url.path.endswith("/comments"):
            return httpx.Response(201, json={"id": 2, "body": "appended"})
        if request.method == "POST":
            seen["title"] = json.loads(request.content)["title"]
        return httpx.Response(
            201,
            json={
                "number": 12,
                "title": seen.get("title", "t"),
                "state": "open",
                "labels": [],
                "user": {"login": "carter-omp[bot]"},
                "html_url": "https://github.com/octo/self/issues/12",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
                "comments": 0,
            },
        )

    bindings, loop, thread = _bindings(db, tmp_path, httpx.MockTransport(handler))
    settings.self_report_repo = SELF_REPO
    settings.self_report_dispatch = False
    object.__setattr__(bindings, "settings", settings)
    try:
        tool = next(x for x in build(bindings) if x.name == "report_pain_point")
        tool.execute(
            {"title": "infra: App token lacks workflows", "details": "d", "area": "infra", "severity": "low"},
            _ctx(),
        )
    finally:
        _stop_loop(loop, thread)

    assert seen["title"] == "[bot-report] infra: App token lacks workflows"
