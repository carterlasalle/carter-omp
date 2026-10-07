"""Deploy-surface contracts: compose env allowlist and the image entrypoint.

`compose.yaml` forwards a hand-maintained allowlist (`env_file:` is absent by
design so the GitHub credential cannot leak into the orchestrator), which means
a `Settings` key that is documented in `.env.example` but missing from that
allowlist silently does nothing in a compose deployment. Same class of drift
for `entrypoint.sh`: the image ENTRYPOINT execs it, so its first line must be
the shebang.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from carter_omp.config import Settings

ROOT = Path(__file__).resolve().parents[1]

# Settings the orchestrator never receives: compose wires them into the
# github-proxy container only (`GITHUB_TOKEN` is additionally rejected on the
# orchestrator side by `Settings._validate_proxy_or_pat`, and the App private
# key is mounted into the proxy alone).
PROXY_ONLY_SETTINGS = frozenset(
    {
        "GITHUB_TOKEN",
        "CARTER_OMP_GITHUB_APP_ID",
        "CARTER_OMP_GITHUB_PRIVATE_KEY_FILE",
    }
)


def _orchestrator_environment() -> dict[str, Any]:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text())
    orchestrator = next(
        service
        for service in compose["services"].values()
        if "CARTER_OMP_GH_PROXY_URL" in (service.get("environment") or {})
    )
    return orchestrator["environment"]


def _documented_aliases() -> set[str]:
    """`VAR=` assignments in `.env.example` (commented-out lines excluded)."""
    text = (ROOT / ".env.example").read_text()
    return {match.group(1) for match in re.finditer(r"^([A-Z][A-Z0-9_]+)=", text, re.MULTILINE)}


def test_orchestrator_compose_env_covers_documented_settings() -> None:
    settings_aliases = {field.alias for field in Settings.model_fields.values() if field.alias}
    documented = _documented_aliases() & settings_aliases
    missing = sorted(documented - set(_orchestrator_environment()) - PROXY_ONLY_SETTINGS)
    assert missing == [], f"documented settings never reach the orchestrator container: {missing}"


def test_entrypoint_shebang_is_the_first_line() -> None:
    path = ROOT / "entrypoint.sh"
    first_line = path.read_bytes().splitlines()[0]
    assert first_line.startswith(b"#!"), f"entrypoint.sh line 1 must be a shebang, got {first_line!r}"
    assert os.access(path, os.X_OK), "entrypoint.sh must be executable"
