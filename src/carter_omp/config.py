"""Env-driven configuration for carter-omp."""

from __future__ import annotations

import random
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from carter_omp.github_events import TriggerPolicy

ThinkingLevel = Literal["off", "low", "medium", "high", "xhigh", "max"]


# trace:v1 id=impl.config-repo-owner-scope work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-T692W95P
class Settings(BaseSettings):
    """Strongly-typed runtime configuration.

    Loaded from process env, optionally pre-populated by `.env`.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # GitHub
    # Credential precedence on the github-proxy side: GitHub App (private
    # key + app/installation IDs, preferred) over long-lived PAT (legacy).
    # The orchestrator never holds either; it talks to github-proxy over
    # HMAC RPC. Validated end-to-end in `_validate_proxy_or_pat` below.
    github_token: SecretStr | None = Field(None, alias="GITHUB_TOKEN")
    github_app_id: str | None = Field(None, alias="CARTER_OMP_GITHUB_APP_ID")
    github_app_private_key_file: Path | None = Field(None, alias="CARTER_OMP_GITHUB_PRIVATE_KEY_FILE")
    github_webhook_secret: SecretStr = Field(..., alias="GITHUB_WEBHOOK_SECRET")
    bot_login: str = Field(..., alias="CARTER_OMP_BOT_LOGIN")
    git_author_name: str | None = Field(None, alias="CARTER_OMP_GIT_AUTHOR_NAME")
    git_author_email: str = Field(..., alias="CARTER_OMP_GIT_AUTHOR_EMAIL")
    repo_allowlist_raw: str = Field("", alias="CARTER_OMP_REPO_ALLOWLIST")
    pr_review_enabled: bool = Field(True, alias="CARTER_OMP_PR_REVIEW_ENABLED")

    # Release sentinel (default off; explicit elevated trigger only).
    release_sentinel_enabled: bool = Field(False, alias="CARTER_OMP_RELEASE_SENTINEL_ENABLED")
    release_commit_prefix: str = Field("chore: bump version to ", alias="CARTER_OMP_RELEASE_COMMIT_PREFIX")
    release_max_rounds: int = Field(5, alias="CARTER_OMP_RELEASE_MAX_ROUNDS")
    release_task_timeout_seconds: float = Field(3600.0, alias="CARTER_OMP_RELEASE_TASK_TIMEOUT_SECONDS")
    release_model: str | None = Field(None, alias="CARTER_OMP_RELEASE_MODEL")
    # Strict trigger policy (explicit invocation only). Production default is
    # `strict`: only an exact authorized trigger (configured label added by an
    # authorized sender, or an exact bot mention by an authorized sender)
    # queues work. `legacy` preserves the previous ambient routing for tests.
    trigger_mode: str = Field("strict", alias="CARTER_OMP_TRIGGER_MODE")
    trigger_label: str = Field("carter-omp", alias="CARTER_OMP_TRIGGER_LABEL")
    label_triggers: bool = Field(True, alias="CARTER_OMP_LABEL_TRIGGERS")
    mention_triggers: bool = Field(True, alias="CARTER_OMP_MENTION_TRIGGERS")
    auto_issue_triage: bool = Field(False, alias="CARTER_OMP_AUTO_ISSUE_TRIAGE")
    auto_pr_review: bool = Field(False, alias="CARTER_OMP_AUTO_PR_REVIEW")
    auto_comment_followups: bool = Field(False, alias="CARTER_OMP_AUTO_COMMENT_FOLLOWUPS")
    # Immutable authorization identity. User/repo IDs are the root of trust;
    # logins and full names exist for readability and startup validation only.
    authorized_user_ids_raw: str = Field("", alias="CARTER_OMP_AUTHORIZED_USER_IDS")
    authorized_logins_raw: str = Field("", alias="CARTER_OMP_AUTHORIZED_LOGINS")
    allowed_repo_ids_raw: str = Field("", alias="CARTER_OMP_REPO_IDS")
    allowed_repo_names_raw: str = Field("", alias="CARTER_OMP_REPOS")
    # Owner scope: when set (e.g. `carterlasalle`), any repository owned by
    # these GitHub user/org logins is authorized — past, present, and future —
    # without listing IDs. The App-install list stays the real boundary:
    # only repos where the App is actually installed can deliver webhooks.
    allowed_repo_owners_raw: str = Field("", alias="CARTER_OMP_REPO_OWNERS")
    github_installation_id: int | None = Field(None, alias="CARTER_OMP_GITHUB_INSTALLATION_ID")

    @property
    def authorized_user_ids(self) -> frozenset[int]:
        """Immutable authorized operator IDs. Empty = refuse to start."""
        ids: set[int] = set()
        for piece in self.authorized_user_ids_raw.split(","):
            piece = piece.strip()
            if not piece:
                continue
            try:
                ids.add(int(piece))
            except ValueError:
                continue
        return frozenset(ids)

    @property
    def authorized_logins(self) -> frozenset[str]:
        """Readable logins for diagnostics/startup validation only."""
        return frozenset(
            piece.strip().lstrip("@").lower().removesuffix("[bot]")
            for piece in self.authorized_logins_raw.split(",")
            if piece.strip()
        )

    @property
    def allowed_repo_ids(self) -> frozenset[int]:
        """Immutable authorized repository IDs. Empty = refuse to start."""
        ids: set[int] = set()
        for piece in self.allowed_repo_ids_raw.split(","):
            piece = piece.strip()
            if not piece:
                continue
            try:
                ids.add(int(piece))
            except ValueError:
                continue
        return frozenset(ids)

    @property
    def allowed_repo_names(self) -> frozenset[str]:
        """Readable repo names for display/startup validation only."""
        return frozenset(piece.strip().lower() for piece in self.allowed_repo_names_raw.split(",") if piece.strip())

    # trace:exempt reason=internal-detail
    @property
    def allowed_repo_owners(self) -> frozenset[str]:
        """Owner logins whose repos are all authorized (past/present/future)."""
        return frozenset(
            piece.strip().lstrip("@").lower() for piece in self.allowed_repo_owners_raw.split(",") if piece.strip()
        )

    @property
    def trigger_policy(self) -> TriggerPolicy:
        """First-class trigger policy derived from the flat env fields."""
        from carter_omp.github_events import TriggerPolicy

        mode = self.trigger_mode.strip().lower()
        return TriggerPolicy(
            mode="legacy" if mode == "legacy" else "strict",
            authorized_user_ids=self.authorized_user_ids,
            label_triggers=self.label_triggers,
            trigger_label=self.trigger_label,
            mention_triggers=self.mention_triggers,
            auto_issue_triage=self.auto_issue_triage,
            auto_pr_review=self.auto_pr_review,
            auto_followup_comments=self.auto_comment_followups,
        )

    # github-proxy. Set BOTH to route GitHub through the proxy; leave both empty
    # to keep PAT-on-orchestrator behavior. Mixing the two (PAT + proxy) is
    # rejected to prevent silent fallback to direct GitHub access.
    github_proxy_url: str | None = Field(None, alias="CARTER_OMP_GH_PROXY_URL")
    github_proxy_hmac_key: SecretStr | None = Field(None, alias="CARTER_OMP_GH_PROXY_HMAC_KEY")
    # Bind address for `python -m carter_omp.proxy serve`. Internal-only by
    # default; github-proxy never exposes a host port.
    github_proxy_bind_host: str = Field("0.0.0.0", alias="CARTER_OMP_GH_PROXY_BIND_HOST")
    github_proxy_bind_port: int = Field(8081, alias="CARTER_OMP_GH_PROXY_BIND_PORT")

    # github-proxy: maximum request body size (bytes). Bodies larger than this
    # are rejected with 413 BEFORE the proxy reads them into memory. Tight
    # by design — every typed endpoint payload fits in a few KB.
    github_proxy_max_body_bytes: int = Field(1 << 20, alias="CARTER_OMP_GH_PROXY_MAX_BODY_BYTES")
    # Hard wall-clock budget (seconds) for a single git subprocess invoked
    # by github-proxy. Bounds how long a hung git can pin a request handler.
    github_proxy_git_timeout_seconds: float = Field(60.0, alias="CARTER_OMP_GH_PROXY_GIT_TIMEOUT_SECONDS")

    # Model selection
    model: str = Field("anthropic/claude-sonnet-4-6", alias="CARTER_OMP_MODEL")
    provider: str | None = Field(None, alias="CARTER_OMP_PROVIDER")
    thinking_level: ThinkingLevel = Field("high", alias="CARTER_OMP_THINKING")

    # Runtime
    max_concurrency: int = Field(8, alias="CARTER_OMP_MAX_CONCURRENCY")
    task_timeout_seconds: float = Field(2400.0, alias="CARTER_OMP_TASK_TIMEOUT_SECONDS")
    task_timeout_hard_grace_seconds: float = Field(60.0, alias="CARTER_OMP_TASK_TIMEOUT_HARD_GRACE_SECONDS")
    request_timeout_seconds: float = Field(120.0, alias="CARTER_OMP_REQUEST_TIMEOUT_SECONDS")

    # Automatic retry of transiently-failed events. When an event handler
    # raises (and it isn't an operator cancel or a shutdown interrupt), the
    # dispatcher re-queues the delivery with escalating backoff instead of
    # giving up, so ephemeral failures (git fetch timeouts, upstream 5xx/429,
    # flaky RPC startup) self-heal. After `event_max_retries` retries the row
    # stays `failed`. `event_retry_delays_seconds` is a comma-separated backoff
    # schedule: the Nth retry waits the Nth value (last value repeats), jittered.
    # Set `event_max_retries=0` to restore fail-fast behavior.
    event_max_retries: int = Field(3, alias="CARTER_OMP_EVENT_MAX_RETRIES")
    event_retry_delays_raw: str = Field("30,120,600", alias="CARTER_OMP_EVENT_RETRY_DELAYS_SECONDS")
    # Premature-end reminder. When a `triage_issue` turn ends without the
    # agent having reached a terminal tool (`gh_open_pr`,
    # `mark_unable_to_reproduce`, `abort_task`) for a `bug`/`documentation`
    # classification, the driver sends up to this many "you stopped before
    # opening a PR — continue" reminder prompts into the same omp session.
    # Set to 0 to disable.
    task_completion_max_reminders: int = Field(2, alias="CARTER_OMP_TASK_COMPLETION_MAX_REMINDERS")
    omp_command: str = Field("omp", alias="CARTER_OMP_OMP_COMMAND")

    # Graceful shutdown (Phase B). On SIGTERM the dispatcher stops claiming
    # new work, then waits up to `drain` seconds for in-flight events to
    # complete cleanly; any still running after that get their omp
    # subprocess killed and the row left in `running` so it requeues on
    # next start. Sum of both MUST stay below the compose `stop_grace_period`.
    shutdown_drain_timeout_seconds: float = Field(25.0, alias="CARTER_OMP_SHUTDOWN_DRAIN_TIMEOUT_SECONDS")
    shutdown_kill_timeout_seconds: float = Field(5.0, alias="CARTER_OMP_SHUTDOWN_KILL_TIMEOUT_SECONDS")

    # Paths
    workspace_root: Path = Field(Path("./data/workspaces"), alias="CARTER_OMP_WORKSPACE_ROOT")
    sqlite_path: Path = Field(Path("./data/carter-omp.sqlite"), alias="CARTER_OMP_SQLITE_PATH")
    log_dir: Path = Field(Path("./data/logs"), alias="CARTER_OMP_LOG_DIR")

    # Server
    bind_host: str = Field("0.0.0.0", alias="CARTER_OMP_BIND_HOST")
    bind_port: int = Field(8080, alias="CARTER_OMP_BIND_PORT")

    # Dev-only replay header value; if empty, /replay is disabled
    replay_token: SecretStr | None = Field(None, alias="CARTER_OMP_REPLAY_TOKEN")

    # Per-submitter rate limiting. `window_seconds` defines the rolling window;
    # `default` is the per-window cap for unknown/first-time submitters;
    # `contributor` is the cap for accounts whose GitHub author_association is
    # `CONTRIBUTOR` (i.e. already has a merged PR). `unlimited_raw` is a
    # comma-separated allowlist of logins that bypass the limiter entirely;
    # accounts with author_association OWNER/MEMBER/COLLABORATOR also bypass.
    rate_limit_window_seconds: float = Field(3600.0, alias="CARTER_OMP_RATE_LIMIT_WINDOW_SECONDS")
    rate_limit_default: int = Field(3, alias="CARTER_OMP_RATE_LIMIT_DEFAULT")
    rate_limit_contributor: int = Field(10, alias="CARTER_OMP_RATE_LIMIT_CONTRIBUTOR")
    rate_limit_unlimited_raw: str = Field("", alias="CARTER_OMP_RATE_LIMIT_UNLIMITED")
    # Logins (comma-separated, `@` prefix optional, case-insensitive) whose `@bot_login`
    # mentions are treated as authoritative directives. These accounts also
    # bypass rate limiting regardless of `author_association`.
    maintainer_logins_raw: str = Field("", alias="CARTER_OMP_MAINTAINER_LOGINS")
    # Bot logins (e.g. chatgpt-codex-connector) whose comments/reviews are
    # treated as authoritative directives without requiring an `@bot` mention.
    # Comma-separated; `@` prefix optional.
    reviewer_bots_raw: str = Field("", alias="CARTER_OMP_REVIEWER_BOTS")

    # Question auto-close. When the bot answers an issue classified as
    # `question`, the comment is suffixed with a 👎-to-keep-open prompt and a
    # row is scheduled in `pending_closures`. The scheduler closes the issue
    # after `question_autoclose_hours` unless the issue author downvoted the
    # comment, a human follow-up arrived, or the issue was closed externally.
    # Default off (delegated continuation only). Set `=true` to enable.
    question_autoclose_enabled: bool = Field(False, alias="CARTER_OMP_QUESTION_AUTOCLOSE_ENABLED")
    question_autoclose_hours: float = Field(4.0, alias="CARTER_OMP_QUESTION_AUTOCLOSE_HOURS")
    question_autoclose_scan_seconds: float = Field(60.0, alias="CARTER_OMP_QUESTION_AUTOCLOSE_SCAN_SECONDS")
    # Local issue/PR search index. Webhooks keep it fresh in real time; this
    # interval controls the periodic GitHub reconcile (first pass = full
    # backfill of every allowlisted repo). <= 0 disables the reconciler —
    # `gh_search_issues` then falls back to the remote search API until the
    # repo has a sync watermark.
    issue_index_sync_seconds: float = Field(900.0, alias="CARTER_OMP_ISSUE_INDEX_SYNC_SECONDS")

    # pi-natives build-output cache. Hardlinks pre-built
    # `packages/natives/native/*.node` (and its companions) into new
    # workspaces keyed by the git tree-hashes of inputs that determine the
    # build output. Misses are captured automatically when a task that
    # finishes successfully has fresh artifacts. Disable to fall back to
    # per-workspace builds.
    natives_cache_enabled: bool = Field(True, alias="CARTER_OMP_NATIVES_CACHE_ENABLED")
    natives_cache_root: Path = Field(Path("/data/cache/pi-natives"), alias="CARTER_OMP_NATIVES_CACHE_ROOT")
    natives_cache_max_entries_per_repo: int = Field(8, alias="CARTER_OMP_NATIVES_CACHE_MAX_ENTRIES_PER_REPO")
    natives_cache_max_bytes: int = Field(4 * 1024**3, alias="CARTER_OMP_NATIVES_CACHE_MAX_BYTES")
    natives_cache_gc_interval_seconds: float = Field(3600.0, alias="CARTER_OMP_NATIVES_CACHE_GC_INTERVAL_SECONDS")

    # Post-run workspace cache reclamation. Every task run reinstalls
    # node_modules (`ensure_workspace_dependencies`), so between runs the
    # checkout's node_modules and the workspace-private bun install cache are
    # dead weight — multiple GB per issue that would otherwise persist until
    # the issue closes and exhaust the disk. When enabled, the worker strips
    # them after every event and WorkerPool.start() sweeps all workspaces once
    # at boot. Costs a dependency re-download on the next run for that issue.
    reclaim_workspace_caches: bool = Field(True, alias="CARTER_OMP_RECLAIM_WORKSPACE_CACHES")

    @field_validator("bot_login", mode="after")
    @classmethod
    def _require_bot_login(cls, value: str) -> str:
        cleaned = value.strip().removeprefix("@").lower()
        if cleaned.endswith("[bot]"):
            cleaned = cleaned[:-5]
        if not cleaned:
            raise ValueError("CARTER_OMP_BOT_LOGIN must be a non-empty GitHub login")
        return cleaned

    @field_validator("replay_token", mode="before")
    @classmethod
    def _blank_replay_disables(cls, value: object) -> object:
        # Treat empty/whitespace strings as 'disabled'. Without this, an empty
        # CARTER_OMP_REPLAY_TOKEN becomes SecretStr("") which the server would
        # happily compare against an empty X-CarterOmp-Replay-Token header.
        if isinstance(value, str) and not value.strip():
            return None
        if hasattr(value, "get_secret_value"):
            inner = value.get_secret_value()  # type: ignore[attr-defined]
            if isinstance(inner, str) and not inner.strip():
                return None
        return value

    @field_validator("github_token", mode="before")
    @classmethod
    def _blank_token_disables(cls, value: object) -> object:
        """Treat empty/whitespace `GITHUB_TOKEN` as 'unset' so proxy-only
        deployments don't have to remove the env var."""
        if isinstance(value, str) and not value.strip():
            return None
        if hasattr(value, "get_secret_value"):
            inner = value.get_secret_value()  # type: ignore[attr-defined]
            if isinstance(inner, str) and not inner.strip():
                return None
        return value

    @field_validator("github_proxy_url", mode="before")
    @classmethod
    def _blank_proxy_url_disables(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("github_proxy_hmac_key", mode="before")
    @classmethod
    def _blank_proxy_key_disables(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        if hasattr(value, "get_secret_value"):
            inner = value.get_secret_value()  # type: ignore[attr-defined]
            if isinstance(inner, str) and not inner.strip():
                return None
        return value

    @model_validator(mode="after")
    def _validate_proxy_or_pat(self) -> Settings:
        """Enforce credential-mode exclusivity.

        - PAT and proxy URL both set → reject (silent fallback to direct
          GitHub would defeat the isolation goal).
        - Proxy URL set but no HMAC key (or vice versa) → reject.
        - Neither PAT/App credentials nor proxy URL → reject.
        - PAT and App credentials both set → reject (one credential source).
        - App ID without key file (or vice versa) → reject.
        """
        has_token = self.github_token is not None
        has_app = self.github_app_id is not None or self.github_app_private_key_file is not None
        has_url = bool(self.github_proxy_url)
        has_key = self.github_proxy_hmac_key is not None
        if has_token and has_url:
            raise ValueError(
                "GITHUB_TOKEN and CARTER_OMP_GH_PROXY_URL are mutually exclusive — "
                "set ONE to choose between direct-PAT and github-proxy modes."
            )
        if has_url != has_key:
            raise ValueError(
                "CARTER_OMP_GH_PROXY_URL and CARTER_OMP_GH_PROXY_HMAC_KEY must both be set together (or both empty)."
            )
        if has_token and has_app:
            raise ValueError("GITHUB_TOKEN and GitHub App credentials are mutually exclusive — set ONE.")
        if (self.github_app_id is None) != (self.github_app_private_key_file is None):
            raise ValueError(
                "CARTER_OMP_GITHUB_APP_ID and CARTER_OMP_GITHUB_PRIVATE_KEY_FILE must both be set together."
            )
        if not has_token and not has_app and not has_url:
            raise ValueError(
                "no GitHub access configured: set GITHUB_TOKEN, GitHub App credentials, or set "
                "CARTER_OMP_GH_PROXY_URL + CARTER_OMP_GH_PROXY_HMAC_KEY to use github-proxy."
            )
        return self

    # trace:exempt reason=internal-detail
    @model_validator(mode="after")
    def _validate_strict_identity(self) -> Settings:
        """Refuse to start when the strict authorization identity is incomplete.

        Fail-closed: no authorized user IDs or no repo IDs means no trigger
        could ever authorize, so starting would only serve skips. Strict mode
        with ambient triggers enabled is a contradictory configuration.
        """
        if self.trigger_mode.strip().lower() != "strict":
            return self
        if not self.authorized_user_ids:
            raise ValueError("refusing to start: no CARTER_OMP_AUTHORIZED_USER_IDS configured")
        if not self.allowed_repo_ids and not self.allowed_repo_owners:
            raise ValueError("refusing to start: no CARTER_OMP_REPO_IDS or CARTER_OMP_REPO_OWNERS configured")
        if self.auto_issue_triage or self.auto_pr_review or self.auto_comment_followups:
            raise ValueError(
                "refusing to start: strict mode forbids ambient triggers "
                "(CARTER_OMP_AUTO_ISSUE_TRIAGE/AUTO_PR_REVIEW/AUTO_COMMENT_FOLLOWUPS must be false)"
            )
        if self.bot_login in self.authorized_logins:
            raise ValueError("refusing to start: bot login must not be in CARTER_OMP_AUTHORIZED_LOGINS")
        return self

    @field_validator("repo_allowlist_raw", mode="before")
    @classmethod
    def _coerce_allowlist(cls, v: object) -> str:
        if v is None:
            return ""
        if isinstance(v, str):
            return v
        if isinstance(v, (list, tuple)):
            return ",".join(str(item) for item in v)
        return str(v)

    @property
    def repo_allowlist(self) -> frozenset[str]:
        items = [piece.strip().lower() for piece in self.repo_allowlist_raw.split(",")]
        return frozenset(item for item in items if item)

    @field_validator("rate_limit_unlimited_raw", mode="before")
    @classmethod
    def _coerce_unlimited(cls, v: object) -> str:
        if v is None:
            return ""
        if isinstance(v, str):
            return v
        if isinstance(v, (list, tuple)):
            return ",".join(str(item) for item in v)
        return str(v)

    @property
    def rate_limit_unlimited(self) -> frozenset[str]:
        items = [piece.strip().lstrip("@").lower() for piece in self.rate_limit_unlimited_raw.split(",")]
        return frozenset(item for item in items if item)

    @field_validator("maintainer_logins_raw", mode="before")
    @classmethod
    def _coerce_maintainers(cls, v: object) -> str:
        if v is None:
            return ""
        if isinstance(v, str):
            return v
        if isinstance(v, (list, tuple)):
            return ",".join(str(item) for item in v)
        return str(v)

    @field_validator("reviewer_bots_raw", mode="before")
    @classmethod
    def _coerce_reviewer_bots(cls, v: object) -> str:
        if v is None:
            return ""
        if isinstance(v, str):
            return v
        if isinstance(v, (list, tuple)):
            return ",".join(str(item) for item in v)
        return str(v)

    @property
    def reviewer_bots(self) -> frozenset[str]:
        items = [piece.strip().lstrip("@").lower() for piece in self.reviewer_bots_raw.split(",")]
        return frozenset(item for item in items if item)

    @property
    def maintainer_logins(self) -> frozenset[str]:
        items = [
            piece.strip().lstrip("@").lower().removesuffix("[bot]") for piece in self.maintainer_logins_raw.split(",")
        ]
        return frozenset(item for item in items if item)

    def allows(self, full_name: str) -> bool:
        return full_name.lower() in self.repo_allowlist

    @property
    def model_pool(self) -> tuple[str, ...]:
        """CARTER_OMP_MODEL may be a single id or a comma-separated list; this
        returns the parsed pool (always non-empty)."""
        items = [piece.strip() for piece in self.model.split(",") if piece.strip()]
        return tuple(items) or (self.model,)

    def pick_model(self) -> str:
        """Random selection from the pool (uniform). One-element pools return that one."""
        return random.choice(self.model_pool)

    @property
    def release_model_pool(self) -> tuple[str, ...]:
        """Release-specific model pool, falling back to the general pool."""
        items = [piece.strip() for piece in (self.release_model or "").split(",") if piece.strip()]
        return tuple(items) or self.model_pool

    def pick_release_model(self) -> str:
        """Select a release model, falling back to the general selector."""
        if not self.release_model or not self.release_model.strip():
            return self.pick_model()
        return random.choice(self.release_model_pool)

    @field_validator("event_retry_delays_raw", mode="before")
    @classmethod
    def _coerce_retry_delays(cls, v: object) -> str:
        if v is None:
            return ""
        if isinstance(v, (list, tuple)):
            return ",".join(str(item) for item in v)
        return str(v)

    @property
    def event_retry_delays(self) -> tuple[float, ...]:
        """Parsed backoff schedule in seconds; always non-empty."""
        vals: list[float] = []
        for piece in self.event_retry_delays_raw.split(","):
            piece = piece.strip()
            if not piece:
                continue
            try:
                seconds = float(piece)
            except ValueError:
                continue
            if seconds >= 0:
                vals.append(seconds)
        return tuple(vals) or (30.0,)

    def retry_delay_seconds(self, retry_index: int) -> float:
        """Backoff before the `retry_index`-th retry (1-based), with jitter.

        Clamps to the last configured delay; applies ±20% jitter so a
        fleet-wide outage doesn't replay every event in lockstep.
        """
        delays = self.event_retry_delays
        idx = min(max(retry_index, 1), len(delays)) - 1
        return delays[idx] * (0.8 + random.random() * 0.4)

    @property
    def resolved_author_name(self) -> str:
        """Falls back to bot_login if CARTER_OMP_GIT_AUTHOR_NAME isn't set."""
        return (self.git_author_name or self.bot_login).strip()

    def ensure_paths(self) -> None:
        for path in (self.workspace_root, self.sqlite_path.parent, self.log_dir):
            path.mkdir(parents=True, exist_ok=True)


@cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


def reset_settings_cache() -> None:
    """Invalidate the cached settings (tests)."""
    get_settings.cache_clear()


class _ProxyEnvLoader(BaseSettings):
    """Minimal env loader for `python -m carter_omp.proxy serve`.

    Validates only the fields the github-proxy container actually needs
    (PAT, HMAC key, bind address, paths). Keeping this separate from the
    orchestrator-mode `Settings()` ctor avoids dragging in
    `_validate_proxy_or_pat` and friends, which would reject a perfectly
    valid proxy deployment (no webhook secret, no bot_login, no proxy URL)
    before `serve()` can give a specific error.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    github_token: SecretStr | None = Field(None, alias="GITHUB_TOKEN")
    github_app_id: str | None = Field(None, alias="CARTER_OMP_GITHUB_APP_ID")
    github_app_private_key_file: Path | None = Field(None, alias="CARTER_OMP_GITHUB_PRIVATE_KEY_FILE")
    github_installation_id: int | None = Field(None, alias="CARTER_OMP_GITHUB_INSTALLATION_ID")
    github_proxy_hmac_key: SecretStr = Field(..., alias="CARTER_OMP_GH_PROXY_HMAC_KEY")
    github_proxy_bind_host: str = Field("0.0.0.0", alias="CARTER_OMP_GH_PROXY_BIND_HOST")
    github_proxy_bind_port: int = Field(8081, alias="CARTER_OMP_GH_PROXY_BIND_PORT")
    workspace_root: Path = Field(Path("./data/workspaces"), alias="CARTER_OMP_WORKSPACE_ROOT")
    log_dir: Path = Field(Path("./data/logs"), alias="CARTER_OMP_LOG_DIR")
    github_proxy_max_body_bytes: int = Field(1 << 20, alias="CARTER_OMP_GH_PROXY_MAX_BODY_BYTES")
    github_proxy_git_timeout_seconds: float = Field(60.0, alias="CARTER_OMP_GH_PROXY_GIT_TIMEOUT_SECONDS")

    @field_validator("github_proxy_hmac_key", mode="before")
    @classmethod
    def _reject_blank(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            raise ValueError("must be a non-empty string")
        if hasattr(value, "get_secret_value"):
            inner = value.get_secret_value()  # type: ignore[attr-defined]
            if isinstance(inner, str) and not inner.strip():
                raise ValueError("must be a non-empty string")
        return value

    @model_validator(mode="after")
    def _require_credential(self) -> _ProxyEnvLoader:
        has_token = self.github_token is not None
        has_app = self.github_app_id is not None or self.github_app_private_key_file is not None
        if has_token and has_app:
            raise ValueError("GITHUB_TOKEN and GitHub App credentials are mutually exclusive.")
        if (self.github_app_id is None) != (self.github_app_private_key_file is None):
            raise ValueError("CARTER_OMP_GITHUB_APP_ID and CARTER_OMP_GITHUB_PRIVATE_KEY_FILE go together.")
        if not has_token and not has_app:
            raise ValueError("github-proxy needs GITHUB_TOKEN or GitHub App credentials.")
        return self


def load_proxy_settings() -> Settings:
    """Build a `Settings` instance suitable for the github-proxy process.

    Only the env vars the proxy actually consumes are required; the
    orchestrator-only fields (webhook secret, bot_login, …) are set to
    inert placeholders since `proxy.server` never reads them. Skips the
    `Settings()` cross-field validator (which presumes orchestrator
    semantics) by routing through `model_construct`.
    """
    loader = _ProxyEnvLoader()  # type: ignore[call-arg]
    return Settings.model_construct(
        github_token=loader.github_token,
        github_app_id=loader.github_app_id,
        github_app_private_key_file=loader.github_app_private_key_file,
        github_installation_id=loader.github_installation_id,
        github_webhook_secret=SecretStr(""),
        bot_login="github-proxy",
        git_author_email="github-proxy@invalid",
        github_proxy_url=None,
        github_proxy_hmac_key=loader.github_proxy_hmac_key,
        github_proxy_bind_host=loader.github_proxy_bind_host,
        github_proxy_bind_port=loader.github_proxy_bind_port,
        workspace_root=loader.workspace_root,
        log_dir=loader.log_dir,
        github_proxy_max_body_bytes=loader.github_proxy_max_body_bytes,
        github_proxy_git_timeout_seconds=loader.github_proxy_git_timeout_seconds,
    )
