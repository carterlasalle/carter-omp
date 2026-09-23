"""Authorization surface for carter-omp (layout module).

The implementation lives in :mod:`carter_omp.github_events`; this module
re-exports the authorization vocabulary so ``carter_omp.authz`` is the
stable import location for trusted routing code.
"""

from carter_omp.github_events import (
    Actor,
    AuthorizationDecision,
    TriggerContext,
    TriggerKind,
    TriggerPolicy,
    authorize_event,
    classify_trigger,
    route_authorized_event,
)

__all__ = [
    "Actor",
    "AuthorizationDecision",
    "TriggerContext",
    "TriggerKind",
    "TriggerPolicy",
    "authorize_event",
    "classify_trigger",
    "route_authorized_event",
]
