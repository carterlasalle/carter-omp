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

## What each variable does
<!-- trace:v1 id=doc.triggers-env-vars work=WORK-CO-Q8Z1HJJJ -->

| Variable | Default | Meaning |
|---|---|---|
| `CARTER_OMP_TRIGGER_MODE` | `strict` | `strict`: only an exact authorized trigger queues work. `legacy` preserves the old ambient routing — tests only, never production. |
| `CARTER_OMP_TRIGGER_LABEL` | `carter-omp` | The label name that acts as the one-shot button. Only the signed `labeled` event from an authorized sender counts — the label merely existing on an issue means nothing. Other values (`carter-omp:running`, `:done`, …) are state indicators and never trigger. |
| `CARTER_OMP_MENTION_TRIGGERS` | `true` | Master switch for `@carter-omp <directive>` comments. `false` disables mention triggers even from Carter. Requires authorized `sender.id` AND an exact bot mention. |
| `CARTER_OMP_LABEL_TRIGGERS` | `true` | Master switch for label triggers. `false` disables label triggers even from Carter. |
| `CARTER_OMP_AUTO_ISSUE_TRIAGE` | `false` | When `true`, every opened/reopened issue auto-queues triage with no explicit trigger. Ambient behavior — keep `false` in production. Startup refuses `strict` + `true`. |
| `CARTER_OMP_AUTO_PR_REVIEW` | `false` | When `true`, every opened/reopened/ready PR auto-queues a review. Ambient behavior — keep `false` in production. Startup refuses `strict` + `true`. |
| `CARTER_OMP_AUTO_COMMENT_FOLLOWUPS` | `false` | When `true`, ordinary follow-up comments resume the session without a mention. Ambient behavior — keep `false` in production. Startup refuses `strict` + `true`. |
| `CARTER_OMP_REVIEWER_BOTS` | empty | Comma-separated bot logins whose comments become authoritative directives without a mention. Empty (production default) means reviewer comments are untrusted context — act on them with `@carter-omp fix the review findings`. Only set this for a deliberate automation whose actor ID you trust. |
| `CARTER_OMP_RELEASE_SENTINEL_ENABLED` | `false` | When `true`, `workflow_run.completed` events can start release-diagnosis runs on their own. Clearly dangerous opt-in — default `false`; release repair goes through explicit `@carter-omp release-fix` instead. |
| `CARTER_OMP_QUESTION_AUTOCLOSE_ENABLED` | `false` | Keeps the question-autoclose machinery available. When enabled, starting the question workflow delegates permission for the later close action. Default `false`; never starts another model run. |

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
