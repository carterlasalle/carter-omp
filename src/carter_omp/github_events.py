"""Authorization-first webhook routing for carter-omp.

Security invariant: an unsolicited GitHub event is incapable of initiating an
OMP/model run. Routing order is fixed:

    repo authorization -> actor authorization -> explicit trigger validation
    -> capability derivation -> rate limiting -> queue

`route()` is the single entry point. It never authorizes on mutable issue
state (current labels), `author_association`, PR/issue authorship, commit
authorship, or comment text claiming an identity — only on the signed
webhook's `sender.id`, `repository.id`, `installation.id`, and an explicit
trigger (configured label added by that sender, or an exact bot `@mention`
in a comment by that sender).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from carter_omp.capabilities import POLICY_VERSION, Capability, capabilities_for
from carter_omp.db import issue_key
from carter_omp.pragmas import parse_pragmas

log = logging.getLogger(__name__)

Decision = Literal["queue", "skip"]

TriggerKind = Literal["label", "mention", "manual_cli"]

REPLAYABLE_SKIP_REASONS: frozenset[str] = frozenset()


# trace:v1 id=impl.immutable-actor work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-T692W95P
@dataclass(slots=True, frozen=True)
class Actor:
    """Immutable GitHub identity. Authorization checks use `id`, never `login`."""

    id: int
    login: str
    type: str


@dataclass(slots=True, frozen=True)
class TriggerContext:
    """Immutable authorization record for one admitted execution."""

    run_id: str
    delivery_id: str
    repository_id: int
    repository_full_name: str
    installation_id: int
    actor_id: int
    actor_login: str
    actor_type: str
    event_type: str
    action: str
    trigger_kind: TriggerKind
    trigger_object_id: int | None
    trigger_value: str
    issue_number: int | None
    pull_request_number: int | None
    capabilities: frozenset[Capability]
    policy_version: str
    authorized_at: datetime

    def to_record(self) -> dict[str, object]:
        """Serialize for durable storage alongside the event payload."""
        return {
            "run_id": self.run_id,
            "delivery_id": self.delivery_id,
            "repository_id": self.repository_id,
            "repository_full_name": self.repository_full_name,
            "installation_id": self.installation_id,
            "actor_id": self.actor_id,
            "actor_login": self.actor_login,
            "actor_type": self.actor_type,
            "event_type": self.event_type,
            "action": self.action,
            "trigger_kind": self.trigger_kind,
            "trigger_object_id": self.trigger_object_id,
            "trigger_value": self.trigger_value,
            "issue_number": self.issue_number,
            "pull_request_number": self.pull_request_number,
            "capabilities": sorted(c.value for c in self.capabilities),
            "policy_version": self.policy_version,
            "authorized_at": self.authorized_at.isoformat(),
        }

    @staticmethod
    def from_record(record: Mapping[str, Any]) -> TriggerContext:
        """Rehydrate a stored trigger. Never re-authorizes: replay only."""
        return TriggerContext(
            run_id=str(record["run_id"]),
            delivery_id=str(record["delivery_id"]),
            repository_id=int(record["repository_id"]),
            repository_full_name=str(record["repository_full_name"]),
            installation_id=int(record["installation_id"]),
            actor_id=int(record["actor_id"]),
            actor_login=str(record["actor_login"]),
            actor_type=str(record.get("actor_type") or "User"),
            event_type=str(record["event_type"]),
            action=str(record["action"]),
            trigger_kind=record["trigger_kind"],  # type: ignore[arg-type]
            trigger_object_id=(
                None if record.get("trigger_object_id") is None else int(record["trigger_object_id"])  # type: ignore[arg-type]
            ),
            trigger_value=str(record.get("trigger_value") or ""),
            issue_number=None if record.get("issue_number") is None else int(record["issue_number"]),  # type: ignore[arg-type]
            pull_request_number=(
                None if record.get("pull_request_number") is None else int(record["pull_request_number"])  # type: ignore[arg-type]
            ),
            capabilities=frozenset(Capability(str(c)) for c in (record.get("capabilities") or [])),
            policy_version=str(record["policy_version"]),
            authorized_at=datetime.fromisoformat(str(record["authorized_at"])),
        )


@dataclass(slots=True, frozen=True)
class AuthorizationDecision:
    authorized: bool
    reason: str
    trigger_kind: TriggerKind | None = None
    trigger_value: str | None = None
    trigger_object_id: int | None = None
    actor: Actor | None = None


@dataclass(slots=True, frozen=True)
class RouteDecision:
    decision: Decision
    task: str | None
    repo: str | None
    issue_key: str | None
    reason: str
    submitter: str | None = None
    association: str | None = None
    directive: bool = False
    directive_body: str | None = None
    directive_author: str | None = None
    directive_pragmas: tuple[tuple[str, str], ...] = ()
    directive_authorizes_impl: bool = False
    trigger: TriggerContext | None = None
    authz: AuthorizationDecision | None = None

    @property
    def should_queue(self) -> bool:
        return self.decision == "queue"


@dataclass(slots=True, frozen=True)
class TriggerPolicy:
    mode: Literal["strict", "legacy"] = "strict"
    authorized_user_ids: frozenset[int] = frozenset()
    label_triggers: bool = True
    trigger_label: str = "carter-omp"
    mention_triggers: bool = True
    auto_issue_triage: bool = False
    auto_pr_review: bool = False
    auto_followup_comments: bool = False
    allow_reviewer_bot_directives: bool = False
    release_sentinel: bool = False


def verify_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    """Constant-time HMAC-SHA256 verification of `X-Hub-Signature-256`."""
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    provided = signature_header.removeprefix("sha256=")
    return hmac.compare_digest(expected, provided)


def _repo_full_name(payload: Mapping[str, Any]) -> str | None:
    repo = payload.get("repository")
    if isinstance(repo, dict):
        full = repo.get("full_name")
        if isinstance(full, str):
            return full
    return None


def _normalize_bot_login(login: str | None) -> str:
    if not isinstance(login, str):
        return ""
    cleaned = login.strip().removeprefix("@")
    if cleaned.lower().endswith("[bot]"):
        cleaned = cleaned[:-5]
    return cleaned.lower()


def _login_matches_bot(login: str | None, bot_login: str) -> bool:
    normalized_login = _normalize_bot_login(login)
    return bool(normalized_login) and normalized_login == _normalize_bot_login(bot_login)


def _login_matches_personal_repo_owner(
    login: str | None,
    repository: Mapping[str, Any] | None,
) -> bool:
    """Return whether `login` owns this personal-account repository."""
    if not isinstance(login, str) or not login:
        return False
    owner_login: str | None = None
    owner_type: str | None = None
    if isinstance(repository, Mapping):
        owner = repository.get("owner")
        if isinstance(owner, Mapping):
            raw_login = owner.get("login")
            if isinstance(raw_login, str) and raw_login:
                owner_login = raw_login
            raw_type = owner.get("type")
            if isinstance(raw_type, str) and raw_type:
                owner_type = raw_type
    if owner_type is None or owner_type.lower() != "user":
        return False
    if not owner_login:
        return False
    return login.lower() == owner_login.lower()


def _effective_association(
    login: str | None,
    association: str | None,
    repository: Mapping[str, Any] | None,
) -> str | None:
    if association:
        return association
    if _login_matches_personal_repo_owner(login, repository):
        return "OWNER"
    return association


PrIssueResolver = Callable[[str, int], str | None] | None


def _is_bot_account(user: Mapping[str, Any] | None, bot_login: str) -> bool:
    if not isinstance(user, Mapping):
        return False
    login = str(user.get("login") or "")
    if not login:
        return False
    if _login_matches_bot(login, bot_login):
        return True
    if login.lower().endswith("[bot]"):
        return True
    if str(user.get("type") or "") == "Bot":
        return True
    return False


def _submitter_info(obj: Mapping[str, Any] | None) -> tuple[str | None, str | None]:
    """Extract `(login, author_association)` from an issue/comment object."""
    if not isinstance(obj, Mapping):
        return None, None
    user = obj.get("user")
    login: str | None = None
    if isinstance(user, Mapping):
        raw = user.get("login")
        if isinstance(raw, str) and raw:
            login = raw
    assoc = obj.get("author_association")
    return login, (str(assoc) if isinstance(assoc, str) and assoc else None)


def _sender_actor(payload: Mapping[str, Any]) -> Actor | None:
    sender = payload.get("sender")
    if not isinstance(sender, Mapping):
        return None
    raw_id = sender.get("id")
    if isinstance(raw_id, bool) or not isinstance(raw_id, int):
        return None
    login = sender.get("login")
    if not isinstance(login, str) or not login:
        return None
    raw_type = sender.get("type")
    actor_type = raw_type if isinstance(raw_type, str) and raw_type else "User"
    return Actor(id=raw_id, login=login, type=actor_type)


def _repository_id(payload: Mapping[str, Any]) -> int | None:
    repo = payload.get("repository")
    if not isinstance(repo, Mapping):
        return None
    raw_id = repo.get("id")
    if isinstance(raw_id, bool) or not isinstance(raw_id, int):
        return None
    return raw_id


def _installation_id(payload: Mapping[str, Any]) -> int | None:
    installation = payload.get("installation")
    if not isinstance(installation, Mapping):
        return None
    raw_id = installation.get("id")
    if isinstance(raw_id, bool) or not isinstance(raw_id, int):
        return None
    return raw_id


def extract_mention(body: str | None, bot_login: str) -> str | None:
    """Return `body` with `@<bot_login>` mentions stripped, or None if no mention.

    Match is case-insensitive and word-boundary aware (hyphens in logins are
    part of the token, so `@carter_omp-bot` does NOT match `@carter_omp-bot-extra`).
    """
    if not isinstance(body, str) or not body:
        return None
    login = _normalize_bot_login(bot_login)
    if not login:
        return None
    pattern = re.compile(
        rf"(?<![A-Za-z0-9_-])@{re.escape(login)}(?:\[bot\](?![A-Za-z0-9_-])|(?![A-Za-z0-9_\[-]))",
        re.IGNORECASE,
    )
    if not pattern.search(body):
        return None
    stripped = pattern.sub("", body)
    stripped = re.sub(r"[ \t]+", " ", stripped)
    stripped = re.sub(r"\n[ \t]+", "\n", stripped)
    return stripped.strip()


def is_maintainer(
    login: str | None,
    association: str | None,
    *,
    maintainers: frozenset[str],
) -> bool:
    """A maintainer is anyone in `maintainers` or with a trusted association."""
    if isinstance(login, str) and login and login.lower() in maintainers:
        return True
    if isinstance(association, str) and association.upper() in TRUSTED_ASSOCIATIONS:
        return True
    return False


def is_implementation_authorizer(
    login: str | None,
    association: str | None,
    *,
    maintainers: frozenset[str],
) -> bool:
    """Return whether this author may authorize implementation work."""
    if isinstance(login, str) and login and login.lower() in maintainers:
        return True
    if isinstance(association, str) and association.upper() == "OWNER":
        return True
    return False


# trace:v1 id=impl.authorize-event work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-BKNZHMZ0
def authorize_event(
    event_type: str,
    payload: Mapping[str, Any],
    *,
    policy: TriggerPolicy,
    allowed_repo_ids: frozenset[int] | None,
    installation_id: int | None,
    bot_login: str,
) -> AuthorizationDecision:
    """Decide whether this exact webhook event is an authorized trigger.

    Never consults mutable issue state, associations, or display names — only
    the signed payload's `sender.id`, `repository.id`, `installation.id`,
    and the trigger itself (label name + labeled action, or exact mention).
    """
    repo_id = _repository_id(payload)
    if repo_id is None:
        return AuthorizationDecision(authorized=False, reason="repo_not_authorized")
    if allowed_repo_ids is not None and repo_id not in allowed_repo_ids:
        return AuthorizationDecision(authorized=False, reason="repo_not_authorized")
    if installation_id is not None:
        got_installation = _installation_id(payload)
        if got_installation is None or got_installation != installation_id:
            return AuthorizationDecision(authorized=False, reason="installation_not_authorized")

    actor = _sender_actor(payload)
    if actor is None:
        return AuthorizationDecision(authorized=False, reason="missing_sender")
    if actor.login == "ghost":
        return AuthorizationDecision(authorized=False, reason="ghost_sender")
    if actor.type == "Bot" and actor.id not in policy.authorized_user_ids:
        return AuthorizationDecision(authorized=False, reason="bot_event_ignored")
    if _login_matches_bot(actor.login, bot_login) and actor.id not in policy.authorized_user_ids:
        return AuthorizationDecision(authorized=False, reason="bot_event_ignored")

    action = str(payload.get("action") or "")

    if event_type in ("issues", "pull_request") and action == "labeled":
        if not policy.label_triggers:
            return AuthorizationDecision(authorized=False, reason="wrong_label", actor=actor)
        label = payload.get("label")
        label_name = label.get("name") if isinstance(label, Mapping) else None
        if not isinstance(label_name, str) or label_name.casefold() != policy.trigger_label.casefold():
            return AuthorizationDecision(authorized=False, reason="wrong_label", actor=actor)
        if actor.id not in policy.authorized_user_ids:
            return AuthorizationDecision(authorized=False, reason="actor_not_authorized", actor=actor)
        return AuthorizationDecision(
            authorized=True,
            reason="authorized_label_trigger",
            trigger_kind="label",
            trigger_value=label_name,
            actor=actor,
        )

    if event_type in ("issue_comment", "pull_request_review_comment") and action == "created":
        comment = payload.get("comment")
        body = comment.get("body") if isinstance(comment, Mapping) else None
        if not policy.mention_triggers:
            return AuthorizationDecision(authorized=False, reason="mention_required", actor=actor)
        if extract_mention(body if isinstance(body, str) else None, bot_login) is None:
            if actor.id not in policy.authorized_user_ids:
                return AuthorizationDecision(authorized=False, reason="actor_not_authorized", actor=actor)
            return AuthorizationDecision(authorized=False, reason="mention_required", actor=actor)
        if actor.id not in policy.authorized_user_ids:
            return AuthorizationDecision(authorized=False, reason="actor_not_authorized", actor=actor)
        comment_id: int | None = None
        if isinstance(comment, Mapping):
            raw_comment_id = comment.get("id")
            if isinstance(raw_comment_id, int) and not isinstance(raw_comment_id, bool):
                comment_id = raw_comment_id
        return AuthorizationDecision(
            authorized=True,
            reason="authorized_mention_trigger",
            trigger_kind="mention",
            trigger_value=body if isinstance(body, str) else "",
            trigger_object_id=comment_id,
            actor=actor,
        )

    return AuthorizationDecision(authorized=False, reason="explicit_trigger_required", actor=actor)


def classify_trigger(
    event_type: str,
    payload: Mapping[str, Any],
) -> str:
    """Return the trigger shape of an event without authorizing it."""
    action = str(payload.get("action") or "")
    if event_type in ("issues", "pull_request") and action == "labeled":
        label = payload.get("label")
        label_name = label.get("name") if isinstance(label, Mapping) else None
        return f"label:{label_name}" if isinstance(label_name, str) else "label:?"
    if event_type in ("issue_comment", "pull_request_review_comment"):
        return "mention" if action == "created" else f"{event_type}.{action}"
    return f"{event_type}.{action}"


def _build_trigger_context(
    *,
    delivery_id: str,
    repo: str,
    repo_id: int | None,
    installation_id: int | None,
    actor: Actor,
    event_type: str,
    action: str,
    trigger_kind: TriggerKind,
    trigger_value: str,
    trigger_object_id: int | None,
    issue_number: int | None,
    pull_request_number: int | None,
    task: str,
    extra_capabilities: frozenset[Capability] = frozenset(),
) -> TriggerContext:
    return TriggerContext(
        run_id=uuid.uuid4().hex,
        delivery_id=delivery_id,
        repository_id=repo_id or 0,
        repository_full_name=repo,
        installation_id=installation_id or 0,
        actor_id=actor.id,
        actor_login=actor.login,
        actor_type=actor.type,
        event_type=event_type,
        action=action,
        trigger_kind=trigger_kind,
        trigger_object_id=trigger_object_id,
        trigger_value=trigger_value,
        issue_number=issue_number,
        pull_request_number=pull_request_number,
        capabilities=capabilities_for(task) | extra_capabilities,
        policy_version=POLICY_VERSION,
        authorized_at=datetime.now(UTC),
    )


def _skip_checks_extra(body: str) -> frozenset[Capability]:
    """Grant SKIP_CHECKS only from the explicit `/allow-skip-checks` directive.

    Parsed in trusted routing code before the model runs; the model-visible
    tool schemas expose no skip parameter. Audited via the trigger record.
    """
    for line in body.splitlines():
        if line.strip().lower() == "/allow-skip-checks":
            return frozenset({Capability.SKIP_CHECKS})
    return frozenset()


def _pr_review_pr(pr: Mapping[str, Any], repo: str, action: str, bot_login: str) -> RouteDecision:
    """Build a `review_pr` decision for an incoming PR, or the matching skip."""
    if str(pr.get("state") or "open") != "open":
        return RouteDecision("skip", None, repo, None, "PR not open")
    if bool(pr.get("draft")):
        return RouteDecision("skip", None, repo, None, "draft PR")
    if _is_bot_account(pr.get("user") or {}, bot_login):
        return RouteDecision("skip", None, repo, None, "bot-authored PR")
    number = pr.get("number")
    if not isinstance(number, int):
        return RouteDecision("skip", None, repo, None, "PR missing number")
    login, assoc = _submitter_info(pr)
    return RouteDecision(
        "queue",
        "review_pr",
        repo,
        issue_key(repo, number),
        f"pull_request.{action}",
        submitter=login,
        association=assoc,
    )


# trace:v1 id=impl.route-authorized work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-BKNZHMZ0
def route_authorized_event(
    event_type: str,
    payload: Mapping[str, Any],
    *,
    decision: AuthorizationDecision,
    delivery_id: str,
    repo: str,
    bot_login: str,
    resolve_issue_from_pr: PrIssueResolver = None,
) -> RouteDecision:
    """Map an authorized trigger to a task. Call only after `authorize_event`."""
    assert decision.authorized and decision.actor is not None and decision.trigger_kind is not None
    actor = decision.actor
    action = str(payload.get("action") or "")
    repo_id = _repository_id(payload)
    installation_id = _installation_id(payload)

    def _resolve_pr_key(pr_number: int) -> str:
        if resolve_issue_from_pr is not None:
            resolved = resolve_issue_from_pr(repo, pr_number)
            if resolved:
                return resolved
        return issue_key(repo, pr_number)

    def _queue(
        task: str,
        issue_key_value: str | None,
        reason: str,
        *,
        issue_number: int | None = None,
        pull_request_number: int | None = None,
        directive_body: str | None = None,
        directive_pragmas: tuple[tuple[str, str], ...] = (),
        extra_capabilities: frozenset[Capability] = frozenset(),
    ) -> RouteDecision:
        return RouteDecision(
            "queue",
            task,
            repo,
            issue_key_value,
            reason,
            submitter=actor.login,
            association=None,
            directive=directive_body is not None,
            directive_body=directive_body,
            directive_author=actor.login,
            directive_pragmas=directive_pragmas,
            directive_authorizes_impl=True,
            trigger=_build_trigger_context(
                delivery_id=delivery_id,
                repo=repo,
                repo_id=repo_id,
                installation_id=installation_id,
                actor=actor,
                event_type=event_type,
                action=action,
                trigger_kind=decision.trigger_kind or "label",
                trigger_value=decision.trigger_value or "",
                trigger_object_id=decision.trigger_object_id,
                issue_number=issue_number,
                pull_request_number=pull_request_number,
                task=task,
                extra_capabilities=extra_capabilities,
            ),
            authz=decision,
        )

    if event_type == "issues" and action == "labeled":
        issue = payload.get("issue") or {}
        if "pull_request" in issue:
            return RouteDecision("skip", None, repo, None, "issue is a pull request")
        number = issue.get("number")
        if not isinstance(number, int):
            return RouteDecision("skip", None, repo, None, "issue missing number")
        key = issue_key(repo, number)
        return _queue("triage_issue", key, "issues.labeled", issue_number=number)

    if event_type == "pull_request" and action == "labeled":
        pr = payload.get("pull_request") or {}
        number = pr.get("number")
        if not isinstance(number, int):
            return RouteDecision("skip", None, repo, None, "PR missing number")
        if _login_matches_bot(str((pr.get("user") or {}).get("login") or ""), bot_login):
            key = _resolve_pr_key(number)
            return _queue("handle_pr_conversation", key, "pull_request.labeled", pull_request_number=number)
        key = issue_key(repo, number)
        review_probe = _pr_review_pr(pr, repo, action, bot_login)
        if review_probe.decision == "skip":
            return review_probe
        return _queue("review_pr", key, "pull_request.labeled", pull_request_number=number)

    if event_type == "issue_comment" and action == "created":
        comment = payload.get("comment") or {}
        body = str(comment.get("body") or "")
        stripped = extract_mention(body, bot_login) or ""
        cleaned, pragmas = parse_pragmas(stripped)
        issue = payload.get("issue") or {}
        number = issue.get("number")
        if not isinstance(number, int):
            return RouteDecision("skip", None, repo, None, "comment missing issue number")
        if "pull_request" in issue:
            key = _resolve_pr_key(number)
            return _queue(
                "handle_pr_conversation",
                key,
                "issue_comment.created on PR",
                pull_request_number=number,
                directive_body=cleaned,
                directive_pragmas=pragmas,
                extra_capabilities=_skip_checks_extra(cleaned),
            )
        key = issue_key(repo, number)
        return _queue(
            "handle_comment",
            key,
            "issue_comment.created",
            issue_number=number,
            directive_body=cleaned,
            directive_pragmas=pragmas,
            extra_capabilities=_skip_checks_extra(cleaned),
        )

    if event_type == "pull_request_review_comment" and action == "created":
        comment = payload.get("comment") or {}
        body = str(comment.get("body") or "")
        stripped = extract_mention(body, bot_login) or ""
        cleaned, pragmas = parse_pragmas(stripped)
        pr = payload.get("pull_request") or {}
        number = pr.get("number")
        if not isinstance(number, int):
            return RouteDecision("skip", None, repo, None, "PR missing number")
        key = _resolve_pr_key(number)
        if _login_matches_bot(str((pr.get("user") or {}).get("login") or ""), bot_login):
            return _queue(
                "handle_review",
                key,
                "pull_request_review_comment.created",
                pull_request_number=number,
                directive_body=cleaned,
                directive_pragmas=pragmas,
                extra_capabilities=_skip_checks_extra(cleaned),
            )
        return _queue(
            "review_pr",
            key,
            "pull_request_review_comment.created",
            pull_request_number=number,
            directive_body=cleaned,
            directive_pragmas=pragmas,
            extra_capabilities=_skip_checks_extra(cleaned),
        )

    return RouteDecision("skip", None, repo, None, f"{event_type}.{action} not handled")


def _legacy_route(
    event_type: str,
    payload: Mapping[str, Any],
    *,
    allowlist: frozenset[str],
    bot_login: str,
    maintainers: frozenset[str] = frozenset(),
    reviewer_bots: frozenset[str] = frozenset(),
    resolve_issue_from_pr: PrIssueResolver = None,
    pr_review_enabled: bool = True,
    release_sentinel_enabled: bool = False,
    release_commit_prefix: str = "chore: bump version to ",
) -> RouteDecision:
    """Pre-authorization router, preserved for non-production testing only."""
    repo = _repo_full_name(payload)
    if repo is None or repo.lower() not in allowlist:
        return RouteDecision("skip", None, repo, None, "repo not on allowlist")

    action = str(payload.get("action") or "")

    if event_type == "workflow_run":
        if action != "completed":
            return RouteDecision("skip", None, repo, None, f"workflow_run.{action} ignored")
        if not release_sentinel_enabled:
            return RouteDecision("skip", None, repo, None, "release sentinel disabled")
        run = payload.get("workflow_run")
        repository = payload.get("repository")
        if not isinstance(run, Mapping) or not isinstance(repository, Mapping):
            return RouteDecision("skip", None, repo, None, "workflow_run payload incomplete")
        default_branch = str(repository.get("default_branch") or "")
        head_branch = str(run.get("head_branch") or "")
        if head_branch != default_branch and re.match(r"^v[0-9]", head_branch) is None:
            return RouteDecision("skip", None, repo, None, "not a default-branch/tag run")
        head_commit = run.get("head_commit")
        message = str(head_commit.get("message") or "") if isinstance(head_commit, Mapping) else ""
        if not message.startswith(release_commit_prefix):
            return RouteDecision("skip", None, repo, None, "not a release commit")
        return RouteDecision(
            "queue",
            "handle_release_ci",
            repo,
            f"{repo}#release",
            f"workflow_run {run.get('name') or ''} {run.get('conclusion') or ''}",
        )

    def _resolve_pr_key(pr_number: int) -> str:
        if resolve_issue_from_pr is not None:
            resolved = resolve_issue_from_pr(repo, pr_number)
            if resolved:
                return resolved
        return issue_key(repo, pr_number)

    def _reviewer_bot_login(user: Mapping[str, Any] | None) -> str | None:
        if not isinstance(user, Mapping):
            return None
        raw_login = str(user.get("login") or "").lower()
        if not raw_login:
            return None
        login = raw_login.removesuffix("[bot]")
        if login in reviewer_bots:
            return login
        return raw_login if raw_login in reviewer_bots else None

    def _directive_kwargs(comment: Mapping[str, Any] | None, login: str | None, assoc: str | None) -> dict[str, Any]:
        if not isinstance(comment, Mapping):
            return {}
        body = str(comment.get("body") or "")
        rb_login = _reviewer_bot_login(comment.get("user"))
        if rb_login is not None:
            cleaned, pragmas = parse_pragmas(body)
            return {
                "directive": True,
                "directive_body": cleaned,
                "directive_author": rb_login,
                "directive_pragmas": pragmas,
                "directive_authorizes_impl": False,
            }
        if not is_maintainer(login, assoc, maintainers=maintainers):
            return {}
        stripped = extract_mention(body, bot_login)
        if stripped is None:
            return {}
        cleaned, pragmas = parse_pragmas(stripped)
        authorizes_impl = is_implementation_authorizer(login, assoc, maintainers=maintainers)
        return {
            "directive": True,
            "directive_body": cleaned,
            "directive_author": login,
            "directive_pragmas": pragmas,
            "directive_authorizes_impl": authorizes_impl,
        }

    if event_type == "issues":
        issue = payload.get("issue") or {}
        if "pull_request" in issue:
            return RouteDecision("skip", None, repo, None, "issue is a pull request")
        number = issue.get("number")
        if not isinstance(number, int):
            return RouteDecision("skip", None, repo, None, "issue missing number")
        key = issue_key(repo, number)
        if action in ("opened", "reopened"):
            login, assoc = _submitter_info(issue)
            return RouteDecision(
                "queue", "triage_issue", repo, key, f"issues.{action}", submitter=login, association=assoc
            )
        if action == "closed":
            return RouteDecision("queue", "cleanup_workspace", repo, key, "issues.closed")
        return RouteDecision("skip", None, repo, key, f"issues.{action} ignored")

    if event_type == "issue_comment" and action == "created":
        comment = payload.get("comment") or {}
        rb_login = _reviewer_bot_login(comment.get("user"))
        if rb_login is None and _is_bot_account(comment.get("user"), bot_login):
            return RouteDecision("skip", None, repo, None, "bot/self comment")
        issue = payload.get("issue") or {}
        number = issue.get("number")
        if not isinstance(number, int):
            return RouteDecision("skip", None, repo, None, "comment missing issue number")
        if "pull_request" in issue:
            key = _resolve_pr_key(number)
            login, assoc = _submitter_info(comment)
            assoc = _effective_association(login, assoc, payload.get("repository"))
            issue_user_raw = issue.get("user")
            issue_user = issue_user_raw if isinstance(issue_user_raw, Mapping) else {}
            if _login_matches_bot(str(issue_user.get("login") or ""), bot_login):
                return RouteDecision(
                    "queue",
                    "handle_pr_conversation",
                    repo,
                    key,
                    f"issue_comment.created on PR #{number}",
                    submitter=login,
                    association=assoc,
                    **_directive_kwargs(comment, login, assoc),
                )
            return RouteDecision("skip", None, repo, issue_key(repo, number), "incoming PR comments ignored")
        key = issue_key(repo, number)
        login, assoc = _submitter_info(comment)
        assoc = _effective_association(login, assoc, payload.get("repository"))
        return RouteDecision(
            "queue",
            "handle_comment",
            repo,
            key,
            "issue_comment.created",
            submitter=login,
            association=assoc,
            **_directive_kwargs(comment, login, assoc),
        )

    if event_type == "pull_request" and action in ("opened", "reopened", "ready_for_review"):
        if not pr_review_enabled:
            return RouteDecision("skip", None, repo, None, "PR review disabled")
        pr = payload.get("pull_request") or {}
        return _pr_review_pr(pr, repo, action, bot_login)

    if event_type == "pull_request_review_comment" and action == "created":
        comment = payload.get("comment") or {}
        rb_login = _reviewer_bot_login(comment.get("user"))
        if rb_login is None and _is_bot_account(comment.get("user"), bot_login):
            return RouteDecision("skip", None, repo, None, "bot/self review comment")
        pr = payload.get("pull_request") or {}
        pr_user = pr.get("user") or {}
        if not _login_matches_bot(str(pr_user.get("login") or ""), bot_login):
            return RouteDecision("skip", None, repo, None, "PR not authored by bot")
        number = pr.get("number")
        if not isinstance(number, int):
            return RouteDecision("skip", None, repo, None, "PR missing number")
        key = _resolve_pr_key(number)
        login, assoc = _submitter_info(comment)
        assoc = _effective_association(login, assoc, payload.get("repository"))
        return RouteDecision(
            "queue",
            "handle_review",
            repo,
            key,
            "pull_request_review_comment.created",
            submitter=login,
            association=assoc,
            **_directive_kwargs(comment, login, assoc),
        )

    if event_type == "pull_request" and action == "closed":
        pr = payload.get("pull_request") or {}
        number = pr.get("number")
        if not isinstance(number, int):
            return RouteDecision("skip", None, repo, None, "PR missing number")
        reason = "pull_request.merged" if bool(pr.get("merged")) else "pull_request.closed"
        return RouteDecision("queue", "cleanup_workspace", repo, _resolve_pr_key(number), reason)

    return RouteDecision("skip", None, repo, None, f"{event_type}.{action} not handled")


# trace:v1 id=impl.strict-router work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-BKNZHMZ0
def route(
    event_type: str,
    payload: Mapping[str, Any],
    *,
    allowlist: frozenset[str],
    bot_login: str,
    maintainers: frozenset[str] = frozenset(),
    reviewer_bots: frozenset[str] = frozenset(),
    resolve_issue_from_pr: PrIssueResolver = None,
    pr_review_enabled: bool = True,
    release_sentinel_enabled: bool = False,
    release_commit_prefix: str = "chore: bump version to ",
    policy: TriggerPolicy | None = None,
    allowed_repo_ids: frozenset[int] | None = None,
    installation_id: int | None = None,
    delivery_id: str = "",
) -> RouteDecision:
    """Decide whether and how to handle a webhook event.

    Strict mode (default): authorization-first. Only an exact authorized
    trigger (configured label added by an authorized sender, or an exact bot
    mention by an authorized sender) queues work. Everything else — issue
    opened/reopened, unlabeled comments, PR opened/reopened/ready_for_review,
    review comments without mention, workflow runs — is indexed/skipped with
    `state=skipped` and never invokes the model.

    Legacy mode (`policy.mode == "legacy"`): preserves the previous ambient
    routing for testing. Never use in production.
    """
    effective_policy = policy or TriggerPolicy()
    if effective_policy.mode == "legacy":
        return _legacy_route(
            event_type,
            payload,
            allowlist=allowlist,
            bot_login=bot_login,
            maintainers=maintainers,
            reviewer_bots=reviewer_bots,
            resolve_issue_from_pr=resolve_issue_from_pr,
            pr_review_enabled=pr_review_enabled,
            release_sentinel_enabled=release_sentinel_enabled,
            release_commit_prefix=release_commit_prefix,
        )

    repo = _repo_full_name(payload)
    if repo is None or repo.lower() not in allowlist:
        return RouteDecision("skip", None, repo, None, "repo not on allowlist")

    action = str(payload.get("action") or "")

    if event_type == "issues" and action == "closed":
        number = (payload.get("issue") or {}).get("number") if isinstance(payload.get("issue"), Mapping) else None
        key = issue_key(repo, number) if isinstance(number, int) else None
        return RouteDecision("queue", "cleanup_workspace", repo, key, "issues.closed")
    if event_type == "pull_request" and action == "closed":
        pr = payload.get("pull_request") or {}
        number = pr.get("number") if isinstance(pr, Mapping) else None
        if not isinstance(number, int):
            return RouteDecision("skip", None, repo, None, "PR missing number")
        reason = "pull_request.merged" if bool(pr.get("merged")) else "pull_request.closed"
        key = issue_key(repo, number)
        if resolve_issue_from_pr is not None:
            resolved = resolve_issue_from_pr(repo, number)
            if resolved:
                key = resolved
        return RouteDecision("queue", "cleanup_workspace", repo, key, reason)

    authz = authorize_event(
        event_type,
        payload,
        policy=effective_policy,
        allowed_repo_ids=allowed_repo_ids,
        installation_id=installation_id,
        bot_login=bot_login,
    )
    if not authz.authorized:
        key: str | None = None
        number: Any = None
        container = payload.get("issue") or payload.get("pull_request")
        if isinstance(container, Mapping):
            number = container.get("number")
        if isinstance(number, int):
            key = issue_key(repo, number)
        return RouteDecision(
            "skip",
            None,
            repo,
            key,
            authz.reason,
            submitter=authz.actor.login if authz.actor else None,
            authz=authz,
        )
    return route_authorized_event(
        event_type,
        payload,
        decision=authz,
        delivery_id=delivery_id,
        repo=repo,
        bot_login=bot_login,
        resolve_issue_from_pr=resolve_issue_from_pr,
    )


TRUSTED_ASSOCIATIONS: frozenset[str] = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
"""GitHub `author_association` values that bypass per-user rate limiting."""


def rate_limit_cap(
    login: str,
    association: str | None,
    *,
    unlimited: frozenset[str],
    default: int,
    contributor: int,
) -> int | None:
    """Return the per-window submission cap for a submitter, or `None` for unlimited.

    Precedence: explicit `unlimited` allowlist > trusted GitHub association
    (`OWNER`/`MEMBER`/`COLLABORATOR`) > `CONTRIBUTOR` tier > default tier.
    """
    if login.lower() in unlimited:
        return None
    if association:
        upper = association.upper()
        if upper in TRUSTED_ASSOCIATIONS:
            return None
        if upper == "CONTRIBUTOR":
            return contributor
    return default


__all__ = [
    "Actor",
    "AuthorizationDecision",
    "Decision",
    "REPLAYABLE_SKIP_REASONS",
    "RouteDecision",
    "TRUSTED_ASSOCIATIONS",
    "TriggerContext",
    "TriggerKind",
    "TriggerPolicy",
    "authorize_event",
    "classify_trigger",
    "extract_mention",
    "is_maintainer",
    "is_implementation_authorizer",
    "rate_limit_cap",
    "route",
    "route_authorized_event",
    "verify_signature",
]
