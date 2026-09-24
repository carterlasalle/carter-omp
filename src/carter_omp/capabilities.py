"""Capability system for carter-omp — deterministic, host-assigned permissions.

Capabilities are derived by trusted routing code (`authz.capabilities_for`)
from an authorized trigger. The model can never request, grant, or escalate
them; host tools (`host_tools.ToolBindings.require`) and the github-proxy
enforce them again at execution time.
"""

from __future__ import annotations

from enum import StrEnum


# trace:v1 id=impl.capability-model work=WORK-CO-Q8Z1HJJJ satisfies=REQ-CO-T692W95P
class Capability(StrEnum):
    READ_REPO = "read_repo"
    RUN_COMMANDS = "run_commands"
    EDIT_WORKTREE = "edit_worktree"

    READ_GITHUB = "read_github"
    COMMENT = "comment"
    LABEL = "label"

    PUSH_BRANCH = "push_branch"
    OPEN_PR = "open_pr"
    REQUEST_REVIEW = "request_review"

    REVIEW_PR = "review_pr"

    SKIP_CHECKS = "skip_checks"

    UPDATE_DEFAULT_BRANCH = "update_default_branch"
    MOVE_RELEASE_TAG = "move_release_tag"


#: Policy version stamped on every TriggerContext and authorization decision.
POLICY_VERSION = "carter-omp/v1"

#: Standard authorized issue run: full coding-agent functionality, no release
#: privilege, no check bypass.
ISSUE_RUN_CAPABILITIES: frozenset[Capability] = frozenset(
    {
        Capability.READ_REPO,
        Capability.RUN_COMMANDS,
        Capability.EDIT_WORKTREE,
        Capability.READ_GITHUB,
        Capability.COMMENT,
        Capability.LABEL,
        Capability.PUSH_BRANCH,
        Capability.OPEN_PR,
        Capability.REQUEST_REVIEW,
    }
)

#: Incoming-PR review: read-only with respect to source publishing. A
#: temporary checkout may exist for testing, but nothing may be pushed.
PR_REVIEW_CAPABILITIES: frozenset[Capability] = frozenset(
    {
        Capability.READ_REPO,
        Capability.RUN_COMMANDS,
        Capability.READ_GITHUB,
        Capability.COMMENT,
        Capability.REVIEW_PR,
        Capability.LABEL,
    }
)

#: Release repair: only an explicitly authorized release command grants these.
#: They expire when the run completes.
RELEASE_RUN_CAPABILITIES: frozenset[Capability] = frozenset(
    {
        Capability.READ_REPO,
        Capability.RUN_COMMANDS,
        Capability.EDIT_WORKTREE,
        Capability.READ_GITHUB,
        Capability.COMMENT,
        Capability.LABEL,
        Capability.PUSH_BRANCH,
        Capability.OPEN_PR,
        Capability.REQUEST_REVIEW,
        Capability.UPDATE_DEFAULT_BRANCH,
        Capability.MOVE_RELEASE_TAG,
    }
)


def capabilities_for(task: str) -> frozenset[Capability]:
    """Return the capability profile for an authorized task kind."""
    if task == "review_pr":
        return PR_REVIEW_CAPABILITIES
    if task == "handle_release_ci":
        return RELEASE_RUN_CAPABILITIES
    return ISSUE_RUN_CAPABILITIES


# Explicit minimal OMP built-in tool set, verified against
# @oh-my-pi/pi-coding-agent 18.2.11 `src/tools/builtin-names.ts`.
# All GitHub side effects go through carter-omp host tools; all source
# mutations stay in the current isolated worktree.
OMP_BUILTIN_TOOLS: tuple[str, ...] = (
    "read",
    "bash",
    "edit",
    "write",
    "grep",
    "glob",
    "lsp",
    "task",
    "todo",
    "ast_edit",
    "debug",
    "eval",
)

# Built-ins that must never be enabled: OMP's own GitHub surface (all
# GitHub access goes through host tools), host desktop control, browser
# automation, image generation, and TTS.
OMP_FORBIDDEN_BUILTINS: tuple[str, ...] = (
    "github",
    "computer",
    "browser",
    "generate_image",
    "tts",
)


__all__ = [
    "POLICY_VERSION",
    "Capability",
    "ISSUE_RUN_CAPABILITIES",
    "PR_REVIEW_CAPABILITIES",
    "RELEASE_RUN_CAPABILITIES",
    "OMP_BUILTIN_TOOLS",
    "OMP_FORBIDDEN_BUILTINS",
    "capabilities_for",
]
