"""Unit tests for `carter_omp.org_tools` (org-scoped agent tools)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from carter_omp.org_tools import OrgToolCatalog, load_catalog, write_run_mcp_json

_SENTRY_TOML = """\
[[tool]]
name = "sentry"
owners = ["Phoenix-Fire-Labs"]
command = "npx"
args = ["-y", "@sentry/mcp-server"]
env = ["SENTRY_ACCESS_TOKEN"]
skills = ["ci-fix-dont-freeze"]
prompt_hint = "Sentry MCP `sentry` is available: query recent issues/events for this repo."
"""


def _catalog(tmp_path: Path, body: str = _SENTRY_TOML) -> OrgToolCatalog:
    file = tmp_path / "org_tools.toml"
    file.write_text(body, encoding="utf-8")
    return load_catalog(file)


def test_missing_file_disables_feature(tmp_path: Path) -> None:
    assert load_catalog(None) == OrgToolCatalog()
    assert load_catalog("") == OrgToolCatalog()
    assert load_catalog(tmp_path / "absent.toml") == OrgToolCatalog()


def test_owner_match_is_case_insensitive(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    assert [t.name for t in catalog.for_owner("phoenix-fire-labs")] == ["sentry"]
    assert [t.name for t in catalog.for_owner("Phoenix-Fire-Labs")] == ["sentry"]
    assert catalog.for_owner("someone-else") == ()


def test_forwarded_env_only_names_tool_vars(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    catalog = _catalog(tmp_path)
    monkeypatch.setenv("SENTRY_ACCESS_TOKEN", "sntryu_test")
    monkeypatch.setenv("OTHER_ORG_TOKEN", "nope")
    assert catalog.forwarded_env("phoenix-fire-labs") == {"SENTRY_ACCESS_TOKEN": "sntryu_test"}
    assert catalog.forwarded_env("someone-else") == {}


def test_forwarded_env_skips_unset_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    catalog = _catalog(tmp_path)
    monkeypatch.delenv("SENTRY_ACCESS_TOKEN", raising=False)
    assert catalog.forwarded_env("phoenix-fire-labs") == {}


def test_mcp_servers_fragment_has_no_secrets(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    servers = catalog.mcp_servers("phoenix-fire-labs")
    assert servers == {"sentry": {"command": "npx", "args": ["-y", "@sentry/mcp-server"]}}
    assert catalog.mcp_servers("someone-else") == {}


def test_write_run_mcp_json_roundtrip(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    dest = tmp_path / "run" / "mcp.json"
    assert write_run_mcp_json(catalog, "phoenix-fire-labs", dest) == dest
    assert json.loads(dest.read_text(encoding="utf-8"))["mcpServers"]["sentry"]["command"] == "npx"
    assert "sntryu" not in dest.read_text(encoding="utf-8")


def test_write_run_mcp_json_none_without_tools(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    dest = tmp_path / "run" / "mcp.json"
    assert write_run_mcp_json(catalog, "someone-else", dest) is None
    assert not dest.exists()


def test_prompt_section_names_tools(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    section = catalog.prompt_section("phoenix-fire-labs")
    assert "sentry" in section
    assert catalog.prompt_section("someone-else") == ""


def test_rejects_unknown_kind(tmp_path: Path) -> None:
    file = tmp_path / "org_tools.toml"
    file.write_text('[[tool]]\nname = "x"\nowners = ["o"]\nkind = "cli"\ncommand = "x"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="unknown `kind`"):
        load_catalog(file)


def test_rejects_duplicate_names(tmp_path: Path) -> None:
    file = tmp_path / "org_tools.toml"
    file.write_text(_SENTRY_TOML + _SENTRY_TOML.replace("sentry", "sentry", 1), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate tool name"):
        load_catalog(file)


def test_rejects_missing_owners(tmp_path: Path) -> None:
    file = tmp_path / "org_tools.toml"
    file.write_text('[[tool]]\nname = "x"\ncommand = "x"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="`owners`"):
        load_catalog(file)


_GLOBAL_HTTP_TOML = """\
[[tool]]
name = "tinyfish"
owners = ["*"]
kind = "mcp-http"
url = "https://agent.tinyfish.ai/mcp"
headers = { "X-API-Key" = "${TINYFISH_API_KEY}" }
env = ["TINYFISH_API_KEY"]
prompt_hint = "TinyFish MCP: search + fetch_content."
"""


def test_global_http_tool_applies_to_every_owner(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path, _GLOBAL_HTTP_TOML)

    assert [t.name for t in catalog.for_owner("phoenix-fire-labs")] == ["tinyfish"]
    assert [t.name for t in catalog.for_owner("carterlasalle")] == ["tinyfish"]
    assert catalog.mcp_servers("anyone") == {
        "tinyfish": {
            "type": "http",
            "url": "https://agent.tinyfish.ai/mcp",
            "headers": {"X-API-Key": "${TINYFISH_API_KEY}"},
        }
    }
    assert catalog.forwarded_env("anyone", env={"TINYFISH_API_KEY": "tf_test"}) == {
        "TINYFISH_API_KEY": "tf_test"
    }


def test_http_tool_requires_url_and_rejects_command(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _catalog(tmp_path, '[[tool]]\nname = "x"\nowners = ["*"]\nkind = "mcp-http"\n')
    with pytest.raises(ValueError):
        _catalog(
            tmp_path,
            '[[tool]]\nname = "x"\nowners = ["*"]\nkind = "mcp-http"\nurl = "https://x/mcp"\ncommand = "npx"\n',
        )
