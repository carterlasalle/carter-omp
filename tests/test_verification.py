"""Repository-native verification policy parsing."""

from __future__ import annotations

import pytest

from carter_omp import verification


def test_parse_policy_reads_phases_and_requirements() -> None:
    policy = verification.parse_policy(
        """
version: 1
verification:
  fast: ["uv run ruff check .", "yarn lint"]
  targeted: ["uv run pytest tests/affected/"]
  full: ["uv run pytest"]
publication:
  require_fast: true
  require_full: true
"""
    )

    assert policy is not None
    assert policy.fast == ("uv run ruff check .", "yarn lint")
    assert policy.targeted == ("uv run pytest tests/affected/",)
    assert policy.require_fast and policy.require_full
    assert not policy.require_targeted


def test_empty_policy_file_is_no_policy() -> None:
    assert verification.parse_policy("") is None
    assert verification.parse_policy("# comments only\n") is None


@pytest.mark.parametrize(
    "text",
    [
        "version: 2\nverification: {}\n",
        "version: 1\nverification:\n  fast: uv run ruff\n",
        "version: 1\nverification:\n  fast: ['']\n",
        "version: 1\npublication:\n  require_fast: 'yes'\n",
        "version: 1\nverification:\n  fast: [" + ", ".join(["'x'"] * 21) + "]\n",
    ],
)
def test_parse_policy_rejects_untrusted_shapes(text: str) -> None:
    with pytest.raises(verification.PolicyError):
        verification.parse_policy(text)
