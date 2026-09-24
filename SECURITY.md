# trace:exempt reason=prose-docs-no-behavior

# Security policy
<!-- trace:v1 id=doc.security-policy-2 work=WORK-CO-Q8Z1HJJJ -->

<!-- trace:v1 id=doc.security-policy work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-XM327PK3 -->

## Reporting a vulnerability
<!-- trace:v1 id=doc.security-reporting work=WORK-CO-Q8Z1HJJJ -->

**Do not open a public issue.** Email the maintainer directly
(or use GitHub's private vulnerability reporting on this repo if enabled)
with a description, reproduction steps, and affected commit.

We aim to acknowledge within 72 hours and will coordinate a fix and
disclosure timeline with you.

## Supported versions
<!-- trace:v1 id=doc.security-versions work=WORK-CO-Q8Z1HJJJ -->

Only the latest `main` is supported. There are no versioned releases yet.

## Scope notes
<!-- trace:v1 id=doc.security-scope work=WORK-CO-Q8Z1HJJJ -->

- `docs/security.md` describes the threat model (trusted vs untrusted,
  fail-closed rules). This file is about *reporting*, not the model.
- The daemon runs with least privilege (non-root, dropped capabilities,
  no Docker socket, credential sidecar). A sandbox escape or credential
  leak across the orchestrator/proxy boundary is in scope.
- Prompt-injection *content* in issues/PRs is an expected input, not a
  vulnerability by itself; a bypass of capability enforcement from such
  content is.
