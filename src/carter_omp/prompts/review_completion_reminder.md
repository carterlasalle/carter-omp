PR review unfinished.

PR: {{repo.full_name}}#{{issue.number}} — {{issue.title}}
Review workspace: `{{workspace.branch}}`

Review started; terminal action not reached.
Incoming PR review terminal action: exactly one:
1. `submit_pr_review` — submit batched review summary plus staged inline comments.
2. `abort_task` — unrecoverable environment failure.

Review staged comments, TodoList, prior tool calls; continue from where stopped. Do NOT re-classify unless earlier classify call failed. Do NOT post standalone inline findings. Staged comments → call `submit_pr_review` now. No inline issues → still call `submit_pr_review` with summary-only verdict.

Before the terminal call: if this run burned real effort on something a short note would have skipped — a misleading error, a gate that lied, a fix that took three tries — call `record_solution` once (trigger in the next run's words, answer concrete). Skip it when nothing stung.

MUST end this turn by calling one listed terminal tool.
