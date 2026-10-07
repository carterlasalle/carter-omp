# New issue: {{repo.full_name}}#{{issue.number}}

Title: {{issue.title}}
Author: @{{issue.author}}
Labels (current): {{issue.labels}}
Default branch: `{{repo.default_branch}}`
Working branch: `{{workspace.branch}}` — checked out at cwd.

---

{{issue.body}}

---

## Comments on this issue
<!-- trace:v1 id=doc.kickoff-comments-section work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-BKNZHMZ0 -->

{{thread}}

---

Worktree: cwd; working branch ready for commits if classification calls for code. MUST complete:

1. **Triage first.** The issue body and every comment so far are inline above — read them before classifying, and call `fetch_issue_thread` only for comments posted after this run started. Run `gh_search_issues` for duplicates and already-merged fixes — reporter may use an older release than worktree; then call `classify_issue(primary=..., priority=..., functional=[...], rationale=...)`.

   Before `bug`, system-prompt merit gate: ALL pass — broken contract, demonstrated impact, deliberate-tradeoff check, upstream vs this-repo cause, premise verification. NEVER comment, push, or open a PR before classification.

2. Follow classification workflow; system prompt defines full per-type behavior:
   - `bug` / `documentation` → ack comment → reproduce → fix → PR.
   - `question` → one comment, then stop.
   - `enhancement` / `proposal` → one thoughtful comment, then stop.
   - `wontfix` → one comment explaining design rationale, then stop.
   - `invalid` / `duplicate` → one brief comment, then stop.

3. If `bug` remains unreproduced after a real attempt, call `mark_unable_to_reproduce` with exact needed reporter details. NEVER guess fixes.
