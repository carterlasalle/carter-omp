# Architecture

## Data flow

<!-- trace:v1 id=doc.architecture work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-XM327PK3 -->
```text
                public
                  │
                  ▼
          ┌──────────────┐
          │  carter-omp  │  webhook auth → ID authz → trigger →
          │ orchestrator │  capabilities → queue → OMP subprocess
          └──────┬───────┘
                 │ private HMAC
                 ▼
          ┌──────────────┐
          │ github-proxy │  App key only; per-op repo/thread/branch checks
          └──────┬───────┘
                 ▼
               GitHub
```

- OMP child/runner gets: worktree, model access, capability-gated host
  tools. No GitHub credential.
- Proxy gets: GitHub credential, no model, no arbitrary code execution.
- Agent subprocess env is scrubbed (`_SCRUBBED_ENV_KEYS`); repo-owned
  commands (install/tests/formatter) run credential-free.

Webhook (`POST /webhook/github`, HMAC-SHA256 over the raw body, dedup on
`X-GitHub-Delivery`) → `github_events.route` (strict: repo ID → actor ID →
explicit trigger → `TriggerContext` + `Capability` set) → durable SQLite
`events` → `WorkerPool` (per-issue serialization, `CARTER_OMP_MAX_CONCURRENCY`
across issues, `stop` via the cancellation channel) → `sandbox` worktree
(`carter-omp/<hex>/<slug>`, persistent `session_dir`, `--continue` resume) →
`worker.run_task` (`omp --mode rpc`, built-ins limited to worktree coding
tools, host tools filtered by capability) → comment/label/push/PR via proxy.

Replays reuse the stored `TriggerContext`; a skipped event can never replay
into an authorized one.

## Key modules

`authz` lives in `github_events` (`Actor`, `TriggerContext`,
`TriggerPolicy`, `authorize_event`, `classify_trigger`,
`route_authorized_event`) with profiles in `capabilities.py`.
Enforcement points: `host_tools.ToolBindings.require`, proxy endpoints,
`server.py` webhook ingress, `tasks.py`/`worker.py` trigger threading.
