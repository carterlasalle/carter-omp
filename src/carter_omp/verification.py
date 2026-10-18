"""Repository-native verification policy (`.carter-omp.yml`).

The policy is read from the **base branch**, never from the PR head: a pull
request must not be able to edit the gates that judge it. Parsing is strict
and bounded — a malformed or oversized policy is an error, not a silent
fallback.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import yaml

POLICY_PATH = ".carter-omp.yml"
_MAX_COMMANDS_PER_PHASE = 20
_MAX_COMMAND_CHARS = 500


class PolicyError(ValueError):
    """The policy file exists but cannot be trusted."""


@dataclass(frozen=True, slots=True)
class VerificationPolicy:
    fast: tuple[str, ...] = ()
    targeted: tuple[str, ...] = ()
    full: tuple[str, ...] = ()
    require_fast: bool = False
    require_targeted: bool = False
    require_full: bool = False

    def phase(self, name: str) -> tuple[str, ...]:
        return {"fast": self.fast, "targeted": self.targeted, "full": self.full}[name]

    def required(self, name: str) -> bool:
        return {"fast": self.require_fast, "targeted": self.require_targeted, "full": self.require_full}[name]


def _commands(raw: Any, where: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise PolicyError(f"{where} must be a list of shell commands")
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise PolicyError(f"{where} entries must be non-empty strings")
        if len(item) > _MAX_COMMAND_CHARS:
            raise PolicyError(f"{where} entry exceeds {_MAX_COMMAND_CHARS} characters")
        out.append(item.strip())
    if len(out) > _MAX_COMMANDS_PER_PHASE:
        raise PolicyError(f"{where} defines more than {_MAX_COMMANDS_PER_PHASE} commands")
    return tuple(out)


def _flag(raw: Mapping[str, Any], key: str) -> bool:
    value = raw.get(key, False)
    if not isinstance(value, bool):
        raise PolicyError(f"publication.{key} must be a boolean")
    return value


def parse_policy(text: str) -> VerificationPolicy | None:
    """Parse a policy document. Empty file → None; malformed → PolicyError."""
    if not text.strip():
        return None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PolicyError(f"invalid YAML: {exc}") from exc
    if data is None:
        return None
    if not isinstance(data, Mapping):
        raise PolicyError("the policy root must be a mapping")
    if data.get("version") != 1:
        raise PolicyError("unsupported policy version (expected 1)")
    verification = data.get("verification") or {}
    publication = data.get("publication") or {}
    if not isinstance(verification, Mapping) or not isinstance(publication, Mapping):
        raise PolicyError("verification and publication must be mappings")
    return VerificationPolicy(
        fast=_commands(verification.get("fast"), "verification.fast"),
        targeted=_commands(verification.get("targeted"), "verification.targeted"),
        full=_commands(verification.get("full"), "verification.full"),
        require_fast=_flag(publication, "require_fast"),
        require_targeted=_flag(publication, "require_targeted"),
        require_full=_flag(publication, "require_full"),
    )
