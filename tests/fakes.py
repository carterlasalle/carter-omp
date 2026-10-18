"""Typed test doubles for the orchestrator's collaborators.

Each fake subclasses the real type so it satisfies the interface its call site
declares, and accepts per-test overrides instead of hand-built namespaces.
"""

from __future__ import annotations

from dataclasses import MISSING, fields
from typing import Any

from carter_omp.github_client import GitHubClient
from carter_omp.host_tools import ToolBindings
from carter_omp.sandbox import SandboxManager


class FakeGitHub(GitHubClient):
    """A `GitHubBackend`-compatible client; tests override the methods they use.

    Constructing the real client only stores the token — no request happens
    until a method is called, so an unoverridden method fails loudly instead
    of silently returning a namespace attribute.
    """

    def __init__(self) -> None:
        super().__init__("test-token")


class FakeBindings(ToolBindings):
    """A mutable `ToolBindings` for tests.

    The real class is frozen and tests need to swap the DB, the abort
    controller, and the clients mid-test. Subclassing keeps nominal
    assignability to every API that takes `ToolBindings`; overriding
    `__setattr__` restores the mutability the frozen base removed.
    """

    def __init__(self, **overrides: Any) -> None:
        for field_info in fields(ToolBindings):
            if field_info.name in overrides:
                value = overrides[field_info.name]
            elif field_info.default is not MISSING:
                value = field_info.default
            elif field_info.default_factory is not MISSING:
                value = field_info.default_factory()
            else:
                value = None
            object.__setattr__(self, field_info.name, value)


class FakeSandbox(SandboxManager):
    """A `SandboxManager`-compatible fake with per-test method overrides.

    The real constructor creates pool directories; tests that only exercise
    one operation pass that operation in and skip the filesystem setup.
    """

    def __init__(self, **methods: Any) -> None:
        for name, impl in methods.items():
            setattr(self, name, impl)
