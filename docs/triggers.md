# Triggers

<!-- trace:v1 id=doc.trigger-contract work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-BKNZHMZ0 -->

Default production configuration: unsolicited GitHub content cannot start a run.

```dotenv
CARTER_OMP_TRIGGER_MODE=strict
CARTER_OMP_TRIGGER_LABEL=carter-omp
CARTER_OMP_MENTION_TRIGGERS=true
CARTER_OMP_LABEL_TRIGGERS=true
CARTER_OMP_AUTO_ISSUE_TRIAGE=false
CARTER_OMP_AUTO_PR_REVIEW=false
CARTER_OMP_AUTO_COMMENT_FOLLOWUPS=false
CARTER_OMP_REVIEWER_BOTS=
CARTER_OMP_RELEASE_SENTINEL_ENABLED=false
CARTER_OMP_QUESTION_AUTOCLOSE_ENABLED=false
```

## Label trigger

Carter adds the `carter-omp` label. The router checks the signed `labeled`
webhook's `sender.id` — not the current label set. Unauthorized label adds
skip with `actor_not_authorized`; the stale label is never reusable because
the bot consumes it (remove → `carter-omp:running` → terminal state label).

Only `carter-omp` triggers. `carter-omp:running`, `carter-omp:done`,
`carter-omp:needs-input`, `carter-omp:failed` are state indicators.

## Mention trigger

`@carter-omp <directive>` in a comment, requiring BOTH authorized `sender.id`
AND an exact bot mention (token-boundary, case-insensitive;
`@carter-omp-evil` does not match). The remaining text becomes the trusted
operator directive. Small control commands: `status`, `stop`, `review`,
`resume`, `release-fix`. `stop` cancels without a model turn; `status` answers
from the DB.

## What does NOT trigger

Issue opened/reopened, PR opened/reopened/ready_for_review/synchronized,
unlabeled comments, review comments without mention, reviewer-bot comments,
workflow runs — all `state=skipped` (`explicit_trigger_required`,
`mention_required`, etc.) with no model invocation.

## Routing table

| Event | Strict behavior |
|---|---|
| `issues.opened/reopened` | index only, skip |
| `issues.labeled` (trigger, authorized) | queue `triage_issue`/resume |
| `issue_comment.created` (mention, authorized) | queue `handle_comment`/bootstrap |
| `pull_request.opened/reopened/ready_for_review` | index only, skip |
| `pull_request.labeled` (trigger, authorized) | queue `review_pr` (or resume bot PR) |
| `pull_request_review_comment.created` (mention, authorized) | queue `handle_review`/review-only |
| `pull_request.closed` / `issues.closed` | deterministic cleanup, no model |
| `workflow_run.completed` | skip (release repair via explicit `@carter-omp release-fix` only) |
