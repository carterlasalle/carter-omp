# GitHub App ("Carter OMP")

<!-- trace:v1 id=doc.github-app work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-T692W95P -->

Production authentication is a private GitHub App. Never bake security logic
around its display name or slug — identity is the immutable App/user/
installation/repo IDs, verified at startup and on every webhook.

## Permissions (start minimal)

- Metadata: Read
- Contents: Read/Write
- Issues: Read/Write
- Pull requests: Read/Write
- Actions: Read — only when release diagnosis is enabled
- Workflows: only for installations that need workflow-file edits

Never request administration, secrets, deployments, members, or org
administration unless a concrete operation requires one.

## Install

**Only select repositories.** Each webhook must carry the expected
`installation.id` and `repository.id` before it can become work.

## Startup validation

The orchestrator refuses to start when: no authorized user/repo IDs are
configured, the App identity cannot be verified, repo ID/name mappings
mismatch, the bot is in the operator allowlist unintentionally, strict mode
has ambient triggers enabled, the private key is insecurely permissioned,
a GitHub credential is visible to the orchestrator in proxy-only mode, the
proxy signing key is absent, or the OMP version is RPC-incompatible.

Prefer `CARTER_OMP_GITHUB_PRIVATE_KEY_FILE=/run/secrets/github-app.pem`
(mode `0400`, visible only to the github-proxy container) over an inline
`.env` blob.

## Events subscribed

Issues, issue comments, pull requests, pull-request review comments.
Workflow runs only when the release sentinel is explicitly enabled.
