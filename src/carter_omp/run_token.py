"""Short-lived per-run authorization tokens for the github-proxy channel.

The orchestrator HMAC proves *which container* is calling. The run token
proves *which authorized run* the call belongs to and exactly what it may
touch: repo, thread, workspace, branch, and capabilities with a short
expiry. The proxy verifies both, then independently checks that the
requested operation is covered. Compromising the generic HMAC channel alone
grants nothing beyond unauthenticated rejection.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# Run tokens live minutes, not hours: enough for one tool call round-trip.
RUN_TOKEN_TTL_SECONDS = 600


# trace:v1 id=impl.run-token work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-9N23MPRP
@dataclass(slots=True, frozen=True)
class RunToken:
    run_id: str
    repo_id: int
    repo: str
    issue: int | None
    workspace: str | None
    branch: str | None
    capabilities: frozenset[str]
    iat: int
    exp: int


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def mint_run_token(
    *,
    key: bytes,
    run_id: str,
    repo_id: int,
    repo: str,
    issue: int | None = None,
    workspace: str | None = None,
    branch: str | None = None,
    capabilities: frozenset[str] | set[str] | tuple[str, ...] | list[str],
    ttl_seconds: int = RUN_TOKEN_TTL_SECONDS,
    now: float | None = None,
) -> str:
    """Mint a signed run token. `key` is the shared proxy HMAC key."""
    now_int = int(now if now is not None else time.time())
    payload = {
        "run_id": run_id,
        "repo_id": repo_id,
        "repo": repo,
        "issue": issue,
        "workspace": workspace,
        "branch": branch,
        "capabilities": sorted(set(capabilities)),
        "iat": now_int,
        "exp": now_int + ttl_seconds,
    }
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    sig = hmac.new(key, b"carter-omp-run-token\n" + body, hashlib.sha256).digest()
    return _b64e(body) + "." + _b64e(sig)


def verify_run_token(*, key: bytes, token: str | None, now: float | None = None) -> RunToken | None:
    """Verify a run token. Returns the parsed token or None on any failure."""
    if not token or "." not in token:
        return None
    try:
        body_b64, sig_b64 = token.rsplit(".", 1)
        body = _b64d(body_b64)
        sig = _b64d(sig_b64)
    except Exception:
        return None
    expected = hmac.new(key, b"carter-omp-run-token\n" + body, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, sig):
        return None
    try:
        payload: Mapping[str, Any] = json.loads(body.decode("utf-8"))
    except Exception:
        return None
    try:
        now_int = int(now if now is not None else time.time())
        exp = int(payload["exp"])
        if now_int > exp:
            return None
        issue = payload.get("issue")
        return RunToken(
            run_id=str(payload["run_id"]),
            repo_id=int(payload["repo_id"]),
            repo=str(payload["repo"]),
            issue=None if issue is None else int(issue),
            workspace=str(payload["workspace"]) if payload.get("workspace") is not None else None,
            branch=str(payload["branch"]) if payload.get("branch") is not None else None,
            capabilities=frozenset(str(c) for c in (payload.get("capabilities") or [])),
            iat=int(payload["iat"]),
            exp=exp,
        )
    except (KeyError, TypeError, ValueError):
        return None


__all__ = ["RUN_TOKEN_TTL_SECONDS", "RunToken", "mint_run_token", "verify_run_token"]
