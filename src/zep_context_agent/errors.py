"""Errors the caller is meant to see distinctly.

The two that matter most are honesty errors: a Zep profile that is
not configured, or configured but unreachable, must say so — it must
never quietly fall back to the offline graph and present local
results as Zep's.
"""

from __future__ import annotations


class ContextError(Exception):
    """Base class for context-layer failures."""


class ContextNotConfigured(ContextError):
    """The Zep profile was selected but its configuration is missing."""


class ContextUnavailable(ContextError):
    """The Zep API was configured but could not serve the request."""


class AccountScopeViolation(ContextError):
    """A read or write tried to cross an account boundary.

    Account scoping is enforced in code in every adapter: episodes
    and facts carry exactly one account, and a port call names
    exactly one account. A mismatch is a programming error or an
    attack, never a result.
    """


class BriefNotFound(ContextError):
    """No brief with that id."""


class AccountNotFound(ContextError):
    """No account with that id in the loaded dataset."""


class DecisionError(ContextError):
    """A gate decision that cannot be recorded (already decided, or
    missing the actor the audit trail requires)."""
