# Upstream parity

<!-- trace:v1 id=doc.upstream-parity work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-XM327PK3 -->

Every original robomp trigger/tool/workflow and its carter-omp replacement.
"Gated" means: preserved, but requires an explicit authorized trigger.

## Triggers

| Upstream | carter-omp |
|---|---|
| `issues.opened` → auto triage | intentionally disabled by default; index only (`explicit_trigger_required`). Carter label/mention bootstraps triage |
| `issues.reopened` → auto re-triage | intentionally disabled; same as opened |
| random `issue_comment` → `handle_comment` | gated: authorized mention only, else skip |
| reviewer-bot comment → implicit directive | intentionally disabled in strict mode; review text is untrusted context, Carter mention authorizes |
| `pull_request.opened/reopened/ready_for_review` → auto review | intentionally disabled; index only. Authorized label/mention queues `review_pr` |
| random PR conversation comment | skip; authorized mention on bot PR resumes, on foreign PR reviews only |
| `pull_request_review_comment` on bot PR | gated: authorized mention resumes; otherwise index-only |
| `workflow_run.completed` → release sentinel | default off; explicit `@carter-omp release-fix` or authorized release label only |
| `issues.closed` / `pull_request.closed` | preserved: deterministic cleanup, no model |
| `/replay` any inactive event | gated: done/failed authorized events only; skipped stays unauthorized |

## Tools

| Upstream | carter-omp |
|---|---|
| `fetch_issue_thread`, `search_issues`, `search_commits`, `abort_task` | preserved (always exposed) |
| `gh_post_comment` (+ arbitrary `number`) | gated on `COMMENT`; bound to current thread (no arbitrary target) |
| `classify_issue`, `set_issue_labels` (+ `number`) | gated on `LABEL`; bound to current issue; classification never grants caps |
| `gh_push_branch` (+ `skip_checks`) | gated on `PUSH_BRANCH`; workspace branch + `carter-omp/*` only, no force push; `skip_checks` removed from schema (capability only) |
| `gh_open_pr` (+ `skip_checks`) | gated on `OPEN_PR`; head/base/repo pinned; suite gates unchanged; `skip_checks` removed from schema |
| `gh_request_review` | gated on `REQUEST_REVIEW`; current repo/PR only |
| `fetch_pr`, `classify_pr`, `pr_review_comment`, `submit_pr_review` | gated on `REVIEW_PR`; review-only, never exposed on issue runs |
| `release_retag` (+ `skip_checks`) | replaced: exposed only with `UPDATE_DEFAULT_BRANCH` + `MOVE_RELEASE_TAG`; `skip_checks` removed from schema |
| `repro_record`, `mark_unable_to_reproduce` | gated on `EDIT_WORKTREE` |
| `release_ci_status`, `release_job_log` | gated on `READ_GITHUB` |
| uniform tool set for cache warmth | replaced: capability-filtered exposure per run |

## Workflows

| Upstream | carter-omp |
|---|---|
| triage/classify/label | preserved once authorized |
| answer questions | preserved; autoclose code preserved, default off (delegated continuation) |
| reproduce/fix/PR | preserved once authorized |
| review incoming PR | preserved once authorized (read-only caps) |
| respond to review | preserved once authorized on bot branch |
| persistent sessions / `--continue` | preserved; resume keyed by stored trigger, stranger comments are context only |
| crash recovery / durable queue / per-issue serialization | preserved |
| dashboard | preserved + Trigger/Actor/decision/capability/run-ID columns |
| audited host tools | preserved; denials audited too |
| cancellation / timeouts / draining | preserved; `stop` cancels without a model turn |
| model/thinking overrides | preserved; pragmas accepted only from authorized directives |
| HMAC webhook + delivery dedup | preserved (raw-body first) |
| credential sidecar | preserved and hardened (App tokens, per-run capability token design, expanded scrub) |
| git guards (branch/clean-tree/identity/remote/HEAD/redaction) | preserved |
| rate limiting | preserved as cost defense after authorization |
| local issue index | preserved |
| hardcoded `bun run fix/check/test` gates | preserved as default gates; repo-configurable policy is operator follow-up |
| whole oh-my-pi checkout at `/work/pi` | replaced: pinned prebuilt OMP binary, no monorepo mount |
| PAT in orchestrator | replaced: proxy-only in production |
