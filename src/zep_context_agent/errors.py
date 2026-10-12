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


class FollowThroughError(ContextError):
    """A request-info ask with nothing in it.

    A ``request_info`` decision opens a follow-through record from
    the brief's gaps — missing predicates and quarantined
    documents. Composing the ask for a brief with neither raises
    this error: the draft is refused, with the reason, and no
    record opens (the decision itself still records — the gate is
    the human's; the service names the refusal on the brief's
    audit trail instead of fabricating an empty ask). See
    followthrough.py."""


class ChaseError(ContextError):
    """A renewal chase that cannot be sent.

    Sending a chase opens a follow-through ask for an expiring
    fact (see chases.py). The send is refused — with the cause
    named — when there is nothing expiring under the predicate,
    when an open request-info ask already covers the predicate
    (the broker is already being asked; a chase would ask
    twice), when a chase ask for it is already open, when the
    renewal has already landed, or when the account has no
    brief for the ask to attach to. A refused send opens no
    record and changes no state."""


class RenewalError(ContextError):
    """A renewal brief that cannot be assembled.

    Renewal assembly (see renewals.py) decides a returning
    account against its last decision, so it needs one: an
    account with no DECIDED brief on or before the requested
    date — never briefed, briefed but never decided, or
    decided only later than the date — has nothing to renew
    against, and the request is refused by name rather than
    quietly served as a standard brief. A refused renewal
    assembles nothing and stores nothing."""


class ReviewError(ContextError):
    """A review-gate action that cannot be taken.

    Confirming or rejecting a candidate that does not exist, is
    not pending, or sits under a different account — or a
    rejection without the reason the audit requires — raises this
    error with the cause named. The candidate record is never
    edited by a refused action."""


class IngestionInProgress(ContextError):
    """Another worker holds this account's ingestion lock.

    Ingestion is coordinated per account when a cache backend is
    configured (see ``cache.py``): two workers never ingest the same
    account concurrently, and the second one is told so instead of
    racing the first."""
