"""Issue #14: the run token must follow the run, not a pre-run snapshot.

`worker._attach_run_token` mints before the agent starts, but the run's
identity does not exist yet: `classify_issue(branch_slug=…)` renames the
workspace branch and the run opens its own PR later. The proxy pinned both
claims, so a triage run could commit its fix and never publish it — push and
open-PR 403'd (`run token branch mismatch`) and review requests on the PR the
run had just opened 403'd (`run token thread mismatch`).

The fix is in two halves, both asserted here:
- the proxy accepts a branch in the *same run namespace* (`carter-omp/<hex>/`)
  and both threads the run owns (originating issue, its own PR);
- the token is re-minted from the live branch/PR (`refresh_run_token`), which
  the tools that change either call.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

from carter_omp import worker
from carter_omp.capabilities import POLICY_VERSION, Capability
from carter_omp.config import Settings
from carter_omp.github_client import GitHubError
from carter_omp.github_events import TriggerContext
from carter_omp.proxy_client import GitHubProxyClient
from carter_omp.run_token import mint_run_token, verify_run_token
from tests.test_proxy_server import (
    _HMAC,
    _async_client,
    _build_app,
    _build_settings,
    _run_token,
    _signed,
)

ORIGINAL = "carter-omp/abc123/issue-4"
RENAMED = "carter-omp/abc123/fix-save-crash"
OTHER_RUN = "carter-omp/beef99/fix-save-crash"
PR_NUMBER = 10


@pytest.fixture
def proxy_settings(tmp_path: Path) -> Settings:
    return _build_settings(tmp_path)


def _gh(req: httpx.Request) -> httpx.Response:
    if req.url.path == "/repos/octo/widget/pulls":
        head = json.loads(req.content)["head"]
        return httpx.Response(
            201,
            json={"number": PR_NUMBER, "html_url": "u", "head": {"ref": head}, "base": {"ref": "main"}, "state": "open"},
        )
    return httpx.Response(201, json={})


async def _open_pr(client: httpx.AsyncClient, head: str) -> httpx.Response:
    body = json.dumps(
        {
            "repo": "octo/widget",
            "head": head,
            "base": "main",
            "title": "t",
            "body": "b",
            "draft": False,
            "maintainer_can_modify": True,
        }
    ).encode()
    headers = _signed(
        "POST",
        "/gh/v1/open_pull_request",
        body,
        run_token=_run_token(issue=4, branch=ORIGINAL, capabilities={"open_pr"}),
    )
    return await client.post(
        "/gh/v1/open_pull_request", content=body, headers={**headers, "Content-Type": "application/json"}
    )


# ---------- token round-trip ----------


def test_token_carries_the_runs_own_pull_request() -> None:
    token = mint_run_token(
        key=_HMAC.encode(),
        run_id="run-1",
        repo_id=1,
        repo="octo/widget",
        issue=4,
        pull_request=10,
        branch=ORIGINAL,
        capabilities={"comment"},
        now=time.time(),
    )
    parsed = verify_run_token(key=_HMAC.encode(), token=token)
    assert parsed is not None
    assert (parsed.issue, parsed.pull_request, parsed.branch) == (4, 10, ORIGINAL)


def test_pre_fix_tokens_still_verify() -> None:
    """Tokens minted before the PR claim existed (in-flight runs) keep working.

    Signed here with the production scheme from a payload that never carried
    `pull_request`, so the wire format — not just the dataclass default — is
    what is under test.
    """
    payload = {
        "run_id": "run-1",
        "repo_id": 1,
        "repo": "octo/widget",
        "issue": 4,
        "workspace": None,
        "branch": ORIGINAL,
        "capabilities": ["comment"],
        "iat": int(time.time()),
        "exp": int(time.time()) + 600,
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    signature = hmac.new(_HMAC.encode(), b"carter-omp-run-token\n" + raw, hashlib.sha256).digest()
    token = base64.urlsafe_b64encode(raw).decode().rstrip("=") + "." + base64.urlsafe_b64encode(signature).decode().rstrip("=")

    parsed = verify_run_token(key=_HMAC.encode(), token=token)
    assert parsed is not None
    assert parsed.pull_request is None
    assert parsed.issue == 4
    assert parsed.branch == ORIGINAL


# ---------- proxy scope ----------


async def test_renamed_branch_publishes_under_the_same_run_namespace(proxy_settings: Settings) -> None:
    """The rename `classify_issue(branch_slug=…)` performs keeps `carter-omp/<hex>/`."""
    app = _build_app(proxy_settings, _gh)
    async with await _async_client(app) as client:
        resp = await _open_pr(client, RENAMED)
    assert resp.status_code == 200, resp.text
    assert resp.json()["number"] == PR_NUMBER


async def test_original_branch_still_publishes(proxy_settings: Settings) -> None:
    app = _build_app(proxy_settings, _gh)
    async with await _async_client(app) as client:
        resp = await _open_pr(client, ORIGINAL)
    assert resp.status_code == 200, resp.text


async def test_another_runs_namespace_is_still_rejected(proxy_settings: Settings) -> None:
    app = _build_app(proxy_settings, _gh)
    async with await _async_client(app) as client:
        resp = await _open_pr(client, OTHER_RUN)
    assert resp.status_code == 403
    assert "branch mismatch" in resp.text


async def test_branch_outside_carter_omp_namespace_is_still_rejected(proxy_settings: Settings) -> None:
    app = _build_app(proxy_settings, _gh)
    async with await _async_client(app) as client:
        resp = await _open_pr(client, "main")
    assert resp.status_code == 403


async def test_run_may_request_review_on_its_own_pr(proxy_settings: Settings) -> None:
    app = _build_app(proxy_settings, _gh)
    own_body = json.dumps(
        {"repo": "octo/widget", "pr_number": PR_NUMBER, "reviewers": ["alice"], "team_reviewers": None}
    ).encode()
    foreign_body = json.dumps(
        {"repo": "octo/widget", "pr_number": PR_NUMBER + 1, "reviewers": ["alice"], "team_reviewers": None}
    ).encode()
    token = _run_token(issue=4, pull_request=PR_NUMBER, capabilities={"request_review"})
    async with await _async_client(app) as client:
        own = await client.post(
            "/gh/v1/request_reviewers",
            content=own_body,
            headers={
                **_signed("POST", "/gh/v1/request_reviewers", own_body, run_token=token),
                "Content-Type": "application/json",
            },
        )
        foreign = await client.post(
            "/gh/v1/request_reviewers",
            content=foreign_body,
            headers={
                **_signed("POST", "/gh/v1/request_reviewers", foreign_body, run_token=token),
                "Content-Type": "application/json",
            },
        )
    assert own.status_code == 200, own.text
    assert foreign.status_code == 403
    assert "thread mismatch" in foreign.text


# ---------- orchestrator refresh ----------


def _trigger() -> TriggerContext:
    return TriggerContext(
        run_id="run-1",
        delivery_id="d-1",
        repository_id=1,
        repository_full_name="octo/widget",
        installation_id=2,
        actor_id=3,
        actor_login="carterlasalle",
        actor_type="User",
        event_type="issues",
        action="labeled",
        trigger_kind="label",
        trigger_object_id=None,
        trigger_value="carter-omp",
        issue_number=4,
        pull_request_number=None,
        capabilities=frozenset({Capability.OPEN_PR, Capability.PUSH_BRANCH, Capability.REQUEST_REVIEW}),
        policy_version=POLICY_VERSION,
        authorized_at=datetime.now(UTC),
    )


async def test_attach_run_token_installs_a_refresh_that_follows_the_rename_and_pr(
    proxy_settings: Settings,
) -> None:
    """The production mint path: rename + PR open are re-minted onto the token."""
    app = _build_app(proxy_settings, _gh)
    client = GitHubProxyClient(
        base_url="http://proxy.test",
        hmac_key=_HMAC,
        transport=httpx.ASGITransport(app=app),
    )
    inputs = SimpleNamespace(
        trigger=_trigger(),
        settings=proxy_settings,
        workspace=SimpleNamespace(branch=ORIGINAL),
        github=client,
        git_transport=None,
    )
    bindings = SimpleNamespace(github=client, git_transport=None)

    worker._attach_run_token(cast(Any, inputs), cast(Any, bindings))
    assert callable(bindings.refresh_run_token)

    # Only the refresh widens the thread claim to the PR this run opens.
    with pytest.raises(GitHubError) as before:
        await bindings.github.request_reviewers(repo="octo/widget", pr_number=PR_NUMBER, reviewers=["alice"])
    assert before.value.status == 403

    bindings.refresh_run_token(RENAMED, PR_NUMBER)
    pr = await bindings.github.open_pull_request(
        repo="octo/widget", head=RENAMED, base="main", title="t", body="b"
    )
    assert pr.number == PR_NUMBER
    await bindings.github.request_reviewers(repo="octo/widget", pr_number=PR_NUMBER, reviewers=["alice"])
