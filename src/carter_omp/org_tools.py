"""Org-scoped agent tools: per-owner MCP servers, skills, and env for runs.

A deployment serves many GitHub owners from one container, but third-party
tooling is rarely deployment-wide: a Sentry token belongs to one org, a
staging credential to another. This module loads a single TOML file
(`org_tools.toml`, pointed at by `CARTER_OMP_ORG_TOOLS_FILE`) describing any
number of named tools, each bound to the owners it may serve:

```toml
[[tool]]
name = "sentry"
owners = ["phoenix-fire-labs"]
kind = "mcp-stdio"              # the only kind today
command = "npx"
args = ["-y", "@sentry/mcp-server"]
env = ["SENTRY_ACCESS_TOKEN"]   # orchestrator env names forwarded to the run
skills = ["ci-fix-dont-freeze"] # vendored skill dirs staged for the run
prompt_hint = "Sentry MCP `sentry` is available: query recent issues/events ..."
```

Nothing here is Sentry-specific: adding Datadog for another org is a new
`[[tool]]` block plus the secret in `.env` + compose allowlist. Secrets are
never written into the TOML — `env` names orchestrator env vars, and the
worker forwards only the entries the matched tools name, so a run for
`owner-a` never sees `owner-b`'s token in `printenv`.

The MCP stdio child inherits the omp subprocess env, so forwarding the token
as an env var (rather than a CLI flag or a file) keeps it out of `ps` output
and off disk. The generated `mcp.json` therefore carries no secrets.
"""

from __future__ import annotations

import json
import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


# trace:v1 id=impl.org-tools-entry work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-T692W95P
@dataclass(slots=True, frozen=True)
class OrgTool:
    """One named third-party tool bound to one or more repo owners."""

    name: str
    owners: frozenset[str]
    kind: str
    command: str = ""
    args: tuple[str, ...] = ()
    # mcp-http: remote Streamable-HTTP endpoint and static headers. Header
    # values may use `${VAR}` placeholders; OMP expands them from the run env,
    # so the secret stays out of the generated mcp.json.
    url: str = ""
    headers: tuple[tuple[str, str], ...] = ()
    # Orchestrator env var names forwarded into the run's env (the secret
    # itself lives in the container env / .env, never in the TOML file).
    env: tuple[str, ...] = ()
    # Vendored skill directory names (under `skills/`) staged for the run.
    skills: tuple[str, ...] = ()
    prompt_hint: str = ""


# trace:v1 id=impl.org-tools-catalog work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-T692W95P
@dataclass(slots=True, frozen=True)
class OrgToolCatalog:
    """All configured tools, with per-owner lookup."""

    tools: tuple[OrgTool, ...] = ()

    # trace:v1 id=impl.org-tools-for-owner work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-T692W95P
    def for_owner(self, owner: str) -> tuple[OrgTool, ...]:
        """Tools bound to this repo owner (login, case-insensitive)."""
        want = owner.strip().lower()
        return tuple(t for t in self.tools if want in t.owners or "*" in t.owners)

    # trace:v1 id=impl.org-tools-forwarded-env work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-T692W95P
    def forwarded_env(self, owner: str, env: dict[str, str] | None = None) -> dict[str, str]:
        """Env overlay for a run: only vars named by this owner's tools.

        Missing or blank values are skipped (a tool whose secret is unset
        simply gets no credential rather than an empty one), so `env` may be
        `os.environ` directly.
        """
        import os

        source = env if env is not None else os.environ
        out: dict[str, str] = {}
        for tool in self.for_owner(owner):
            for key in tool.env:
                value = source.get(key, "")
                if value:
                    out[key] = value
        return out

    # trace:v1 id=impl.org-tools-mcp-servers work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-T692W95P
    def mcp_servers(self, owner: str) -> dict[str, dict]:
        """`mcpServers` fragment for a run's generated `mcp.json`."""
        servers: dict[str, dict] = {}
        for tool in self.for_owner(owner):
            if tool.kind == "mcp-stdio":
                servers[tool.name] = {"command": tool.command, "args": list(tool.args)}
            elif tool.kind == "mcp-http":
                entry: dict = {"type": "http", "url": tool.url}
                if tool.headers:
                    entry["headers"] = dict(tool.headers)
                servers[tool.name] = entry
        return servers

    # trace:v1 id=impl.org-tools-prompt-section work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-T692W95P
    def prompt_section(self, owner: str) -> str:
        """System-prompt section naming this run's org tools ("" when none)."""
        hints = [t.prompt_hint for t in self.for_owner(owner) if t.prompt_hint.strip()]
        if not hints:
            return ""
        lines = [
            "<org-tools>",
            "Org-scoped tooling available to this run:",
            *("  - " + h for h in hints),
            "</org-tools>",
        ]
        return "\n".join(lines)


# trace:v1 id=impl.org-tools-parse work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-T692W95P
def _parse_tool(raw: dict, index: int) -> OrgTool:
    where = f"[[tool]] #{index}"
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"{where} needs a non-empty `name`")
    owners = raw.get("owners", [])
    if not isinstance(owners, list) or not owners or not all(isinstance(o, str) and o.strip() for o in owners):
        raise ValueError(f"{where} ({name}) needs a non-empty `owners` list of login strings")
    kind = raw.get("kind", "mcp-stdio")
    if kind not in ("mcp-stdio", "mcp-http"):
        raise ValueError(f"{where} ({name}): unknown `kind` {kind!r} (expected mcp-stdio or mcp-http)")
    command = raw.get("command", "")
    url = raw.get("url", "")
    headers_raw = raw.get("headers", {})
    if not isinstance(headers_raw, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in headers_raw.items()
    ):
        raise ValueError(f"{where} ({name}): `headers` must be a table of strings")
    if kind == "mcp-stdio":
        if not isinstance(command, str) or not command.strip():
            raise ValueError(f"{where} ({name}) needs a non-empty `command` for mcp-stdio")
        if url:
            raise ValueError(f"{where} ({name}): mcp-stdio does not take `url`")
    else:
        if not isinstance(url, str) or not url.strip():
            raise ValueError(f"{where} ({name}) needs a non-empty `url` for mcp-http")
        if not url.startswith(("https://", "http://127.0.0.1")):
            raise ValueError(f"{where} ({name}): mcp-http url must be https (or loopback http)")
        if command:
            raise ValueError(f"{where} ({name}): mcp-http does not take `command`")
    args = raw.get("args", [])
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise ValueError(f"{where} ({name}): `args` must be a list of strings")
    env = raw.get("env", [])
    if not isinstance(env, list) or not all(isinstance(e, str) and e.strip() for e in env):
        raise ValueError(f"{where} ({name}): `env` must be a list of env var names")
    skills = raw.get("skills", [])
    if not isinstance(skills, list) or not all(isinstance(s, str) and s.strip() for s in skills):
        raise ValueError(f"{where} ({name}): `skills` must be a list of skill directory names")
    prompt_hint = raw.get("prompt_hint", "")
    if not isinstance(prompt_hint, str):
        raise ValueError(f"{where} ({name}): `prompt_hint` must be a string")
    return OrgTool(
        name=name.strip(),
        owners=frozenset(o.strip().lower() for o in owners),
        kind=kind,
        command=command.strip() if isinstance(command, str) else "",
        args=tuple(args),
        url=url.strip() if isinstance(url, str) else "",
        headers=tuple(sorted((str(k), str(v)) for k, v in headers_raw.items())),
        env=tuple(e.strip() for e in env),
        skills=tuple(skills),
        prompt_hint=prompt_hint.strip(),
    )


# trace:v1 id=impl.org-tools-load work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-T692W95P
def load_catalog(path: str | Path | None) -> OrgToolCatalog:
    """Load the org-tools TOML file. Missing path (feature off) → empty catalog.

    A present-but-unreadable or invalid file raises: a half-configured tool
    (e.g. a typo'd owner) must fail at startup, not silently run without
    the tool the operator expected.
    """
    if path is None or not str(path).strip():
        return OrgToolCatalog()
    file = Path(path)
    if not file.is_file():
        log.warning("org tools file not found, running without org tools", extra={"path": str(file)})
        return OrgToolCatalog()
    try:
        data = tomllib.loads(file.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"org tools file {file} unreadable: {exc}") from exc
    raw_tools = data.get("tool", [])
    if not isinstance(raw_tools, list):
        raise ValueError(f"org tools file {file}: `[[tool]]` must be a list of tables")
    tools = tuple(_parse_tool(raw, i) for i, raw in enumerate(raw_tools) if isinstance(raw, dict))
    seen: set[str] = set()
    for tool in tools:
        if tool.name in seen:
            raise ValueError(f"org tools file {file}: duplicate tool name {tool.name!r}")
        seen.add(tool.name)
    return OrgToolCatalog(tools=tools)


# trace:v1 id=impl.org-tools-mcp-json work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-T692W95P
def write_run_mcp_json(catalog: OrgToolCatalog, owner: str, dest: str | Path) -> Path | None:
    """Write the per-run `mcp.json` for this owner. None when no MCP tools.

    The file carries server commands only — never secrets (those ride the
    process env). Callers place it outside the repo worktree so the push
    gates never see it.
    """
    path = Path(dest)
    servers = catalog.mcp_servers(owner)
    if not servers:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"mcpServers": servers}, indent=2) + "\n"
    if path.is_file() and path.read_text(encoding="utf-8") == body:
        return path
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.chmod(0o644)
    tmp.replace(path)
    return path


__all__ = ["OrgTool", "OrgToolCatalog", "load_catalog", "write_run_mcp_json"]
