Turn ended without answering.

Thread: {{repo.full_name}}#{{issue.number}} — {{issue.title}}
Branch: `{{workspace.branch}}`

A mention is answered with exactly one turn-ending action:

1. `gh_post_comment` — reply on this thread: what you changed and where (branch/PR), or why there is nothing to do.
2. `abort_task` — unrecoverable environment failure: silent to the reporter, and file the harness fault with `report_pain_point`.

Review what you already did this turn and post the reply now. Do NOT re-read the thread or re-run the work you already finished. NEVER end a mention turn with neither action: the human is waiting and a silent run reads as the bot ignoring them.

Before the terminal call: if this run burned real effort on something a short note would have skipped — a misleading error, a gate that lied, a fix that took three tries — call `record_solution` once (trigger in the next run's words, answer concrete). Skip it when nothing stung.
