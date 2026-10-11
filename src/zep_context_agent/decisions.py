"""Recording the underwriter's gate decision.

One function, used by both decision paths (the checkpointed graph
resume and the service-state fallback), so a decision means the
same thing however it arrives:

- the decision lands on the brief — action, actor, reason, time —
  with an audit entry;
- the brief's status becomes ``DECIDED``; a second decision on the
  same brief is refused (:class:`DecisionError`), in the service,
  before any of this runs;
- the decision joins the account's history as a new episode: a
  ``decision_memo`` document whose assertions carry the action.
  Its content rides in assertions, not free text — free text is
  the untrusted channel the injection screen watches, and a system
  record has no business in it. The next brief for the account
  resolves ``underwriter_decision`` as a current fact, cited to
  this memo, exactly like any other source.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .ingestion import ingest_document
from .ontology import FactAssertion, SourceDocument
from .ports import ContextPort
from .schemas import AuditEntry, Brief, Decision


def apply_decision(
    port: ContextPort,
    brief: Brief,
    *,
    action: str,
    actor: str,
    reason: str,
    at: datetime | None = None,
) -> Brief:
    decided_at = at or datetime.now(timezone.utc)
    brief.decision = Decision(
        action=action, actor=actor, reason=reason, decided_at=decided_at
    )
    brief.status = "DECIDED"
    brief.audit.append(
        AuditEntry(
            at=decided_at,
            event="gate_decision",
            detail=f"{action} by {actor}: {reason}",
        )
    )
    memo = SourceDocument(
        doc_id=f"decision-{brief.brief_id}",
        account_id=brief.account_id,
        kind="decision_memo",
        title=f"Underwriter decision on brief {brief.brief_id}: {action}",
        received_at=decided_at.date(),
        assertions=[
            FactAssertion(
                predicate="underwriter_decision",
                value=action,
                valid_from=decided_at.date(),
                entity_ref=brief.brief_id,
            )
        ],
    )
    ingest_document(port, memo)
    return brief
