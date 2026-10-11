"""The brief record: what assembly produces and the gate decides on.

A brief is the unit of record. It carries the current facts (each
with its citation), the superseded facts it retired and what
replaced them, the predicates it could not resolve, the guardrail
results, and — once the underwriter acts — the decision and the
audit trail. Briefs are stored whole (JSONB in Postgres, dicts in
the test double) and rendered to text from this one shape, so the
console, the export, and the eval pack all read the same record.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from .ontology import CandidateFact, Edge, Episode, FactValue

BriefStatus = Literal[
    "AWAITING_UNDERWRITER", "DECIDED", "VERIFICATION_FAILED"
]

DecisionAction = Literal["proceed", "request_info", "escalate"]


class CitedFact(BaseModel):
    """A current fact plus the citation an underwriter can check."""

    predicate: str
    value: FactValue
    unit: str = ""
    entity_ref: str = ""
    valid_from: date
    valid_to: date | None = None
    episode_id: str
    doc_id: str
    doc_title: str
    recorded_at: date


class SupersededFact(BaseModel):
    """A fact that was in force and no longer is, shown as such."""

    predicate: str
    value: FactValue
    unit: str = ""
    entity_ref: str = ""
    valid_from: date
    valid_to: date | None = None
    episode_id: str
    doc_id: str
    doc_title: str
    recorded_at: date
    successor_value: FactValue | None = None
    successor_doc_id: str = ""
    reason: Literal["replaced", "expired"]


class QuarantineNote(BaseModel):
    """A document whose assertions were screened out of the brief."""

    episode_id: str
    doc_id: str
    doc_title: str
    reason: str


class VerificationResult(BaseModel):
    citations_complete: bool
    leakage_free: bool
    violations: list[str] = Field(default_factory=list)


class DiffFact(BaseModel):
    """One fact in a brief diff: an addition, or a removal."""

    predicate: str
    value: FactValue
    unit: str = ""
    entity_ref: str = ""
    doc_id: str = ""
    doc_title: str = ""


class DiffSuperseded(BaseModel):
    """A fact whose value handed off between the two briefs: the
    value the previous brief showed as current, and what stands
    in its place now (``current_value`` is the successor the new
    brief itself names; it is ``None`` only when that successor is
    itself no longer current)."""

    predicate: str
    entity_ref: str = ""
    unit: str = ""
    previous_value: FactValue
    current_value: FactValue | None = None
    previous_doc_id: str = ""
    current_doc_id: str = ""


class DiffDecision(BaseModel):
    """A gate decision recorded between the two briefs."""

    brief_id: str
    action: str
    actor: str = ""
    decided_on: date | None = None


class BriefDiff(BaseModel):
    """What changed since the previous brief, computed in code
    from the two stored brief payloads (see :mod:`diffing`).
    ``None`` on a brief means no earlier brief was eligible to
    compare against — the surfaces say "first brief"."""

    previous_brief_id: str
    previous_as_of: date
    added: list[DiffFact] = Field(default_factory=list)
    superseded: list[DiffSuperseded] = Field(default_factory=list)
    removed: list[DiffFact] = Field(default_factory=list)
    decisions: list[DiffDecision] = Field(default_factory=list)


class Decision(BaseModel):
    action: DecisionAction
    actor: str
    reason: str
    decided_at: datetime


class AuditEntry(BaseModel):
    at: datetime
    event: str
    detail: str = ""


class Brief(BaseModel):
    brief_id: str
    account_id: str
    account_name: str
    as_of: date
    backend: str  # the ContextPort adapter that served: "zep" | "offline"
    status: BriefStatus = "AWAITING_UNDERWRITER"
    current_facts: list[CitedFact] = Field(default_factory=list)
    superseded_facts: list[SupersededFact] = Field(default_factory=list)
    missing_predicates: list[str] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)
    sources: list[Episode] = Field(default_factory=list)
    quarantined: list[QuarantineNote] = Field(default_factory=list)
    context_block: str | None = None
    context_block_source: str = ""  # adapter name, or "unavailable"
    narrative: str = ""
    narrative_source: str = ""  # "model:<name>" | "template"
    flags: list[str] = Field(default_factory=list)
    verification: VerificationResult | None = None
    diff: BriefDiff | None = None
    decision: Decision | None = None
    audit: list[AuditEntry] = Field(default_factory=list)
    created_at: datetime


class StalenessRecord(BaseModel):
    """A decided brief whose facts moved underneath it.

    The record lives apart from the brief: the decided brief is
    the audit trail of what the underwriter saw and stays
    byte-identical; this record says the world changed after the
    decision, names the episodes that arrived (the triggers), and
    carries the material slice of the change — the same diff
    machinery as a brief diff, computed against the account's
    current fact set (see :mod:`staleness`).

    Resolution is a fresh decision on a new brief, stamped here
    (``resolved_by_brief``); there is no dismiss path.
    """

    record_id: str
    account_id: str
    brief_id: str  # the DECIDED brief whose facts went stale
    decision_action: str
    decision_actor: str
    decided_at: datetime
    flagged_at: datetime
    trigger_episode_ids: list[str] = Field(default_factory=list)
    changes: BriefDiff  # the material slice; decisions always []
    resolved_by_brief: str | None = None
    resolved_at: datetime | None = None
    resolved_by: str | None = None


class FollowThroughItem(BaseModel):
    """One thing a request-info decision asked the broker for.

    ``kind`` is ``predicate`` (a fact the brief could not resolve)
    or ``document`` (a quarantined document to resend cleanly).
    State is ``open`` until the answer is recognised in a fresh
    assembly, then ``received`` — with the episode that satisfied
    it named, so the desk can see exactly what closed the item.
    """

    kind: Literal["predicate", "document"]
    predicate: str = ""  # predicate items
    label: str  # the underwriter-terms name the draft asked by
    doc_id: str = ""  # document items: the quarantined document
    doc_title: str = ""
    episode_id: str = ""  # document items: the quarantined episode
    reason: str = ""  # document items: why it was quarantined
    status: Literal["open", "received"] = "open"
    received_episode_id: str = ""
    received_at: datetime | None = None


class FollowThroughRecord(BaseModel):
    """A request-info decision, followed through.

    Opened when an underwriter records ``request_info`` on a
    brief: the ask — missing predicates, quarantined documents to
    resend — composed in code from that brief, with a deterministic
    draft the underwriter can copy. The draft is never sent; there
    is no send path. Items flip to ``received`` as broker replies
    are ingested and recognised against a fresh assembly's facts.
    The record closes (``complete``) when every item is received.

    The decided brief is never edited; this record lives apart
    from it, like the staleness record. The two watches are
    independent: an ask can complete while a staleness flag is
    open, and a flag can resolve while an ask is still open.
    """

    record_id: str
    account_id: str
    brief_id: str  # the DECIDED (request_info) brief the ask came from
    decision_actor: str
    decision_reason: str
    decided_at: datetime
    created_at: datetime
    items: list[FollowThroughItem] = Field(default_factory=list)
    draft_text: str = ""
    status: Literal["open", "partially_received", "complete"] = "open"
    completed_at: datetime | None = None
    updated_at: datetime


class TimelineEvent(BaseModel):
    """One dated entry in an account's timeline (see
    :mod:`timeline`): a fact on its validity window, an episode
    arriving, a gate decision, a staleness flag opening or
    resolving, a follow-through ask opening, an item being
    received, an ask completing. ``date`` is the event's point
    in the chronology — for a fact, the day its window opens;
    ``valid_to`` carries the window's end for fact events.
    ``fact_status`` is the temporal rule's verdict at the
    timeline's as-of date: ``current``, ``superseded`` (replaced
    by a successor), ``expired`` (window closed, no successor),
    or ``scheduled`` (window has not opened yet)."""

    kind: Literal[
        "episode",
        "fact",
        "decision",
        "staleness_flag",
        "staleness_resolved",
        "follow_opened",
        "follow_received",
        "follow_completed",
    ]
    date: date
    label: str
    detail: str = ""
    # Fact events.
    predicate: str = ""
    value: FactValue | None = None
    unit: str = ""
    entity_ref: str = ""
    valid_to: date | None = None
    fact_status: Literal[
        "current", "superseded", "expired", "scheduled"
    ] | None = None
    # Citation (fact + episode events name their paper).
    episode_id: str = ""
    doc_id: str = ""
    doc_title: str = ""
    # Record references (decision / watch / ask events).
    brief_id: str = ""
    record_id: str = ""


class AccountTimeline(BaseModel):
    """An account's whole recorded history as one chronology —
    a pure projection over the port and the record stores,
    assembled in code (see :mod:`timeline`)."""

    account_id: str
    account_name: str
    as_of: date
    events: list[TimelineEvent] = Field(default_factory=list)


class ExpiryEntry(BaseModel):
    """One row of the expiry watch (see :mod:`timeline`): a
    relied-on fact whose validity window closes within the
    horizon (``expiring``) or has closed with no successor in
    force (``lapsed``). ``days_remaining`` is negative when
    lapsed. ``successor_exists`` names that a later fact for
    the same predicate and entity is on file even though it
    does not cover the lapse (it starts after a gap) — a fact
    whose successor's window *covers* its end never appears
    here at all: a renewal in force is not watch noise."""

    account_id: str
    account_name: str
    predicate: str
    entity_ref: str = ""
    value: FactValue
    unit: str = ""
    valid_from: date
    valid_to: date
    state: Literal["expiring", "lapsed"]
    days_remaining: int
    successor_exists: bool = False
    successor_valid_from: date | None = None
    episode_id: str = ""
    doc_id: str = ""
    doc_title: str = ""


class DeskDecision(BaseModel):
    """The decision summary a desk-queue row shows."""

    action: str
    actor: str
    decided_on: date | None = None


class DeskFollowThrough(BaseModel):
    """An account's request-info ask, as the desk queue shows it."""

    record_id: str
    brief_id: str  # the decided brief the ask came from
    status: Literal["open", "partially_received", "complete"]
    received_count: int
    total_count: int


class DeskStaleness(BaseModel):
    """An open staleness flag, as the desk queue shows it."""

    record_id: str
    brief_id: str  # the decided brief that went stale
    flagged_at: datetime
    age_days: int
    trigger_episode_ids: list[str] = Field(default_factory=list)
    changes: BriefDiff


class DeskExpiry(BaseModel):
    """An account's expiry-watch rows, as the desk queue shows
    them: the relied-on facts running out under this account,
    soonest (or most lapsed) first. Empty when nothing the
    account relies on is ending — a renewed fact never appears."""

    predicate: str
    entity_ref: str = ""
    value: FactValue
    unit: str = ""
    valid_to: date
    state: Literal["expiring", "lapsed"]
    days_remaining: int
    successor_exists: bool = False


class DeskQueueEntry(BaseModel):
    """One account's row in the underwriter's desk queue — a read
    model over briefs and staleness records; it decides nothing.

    ``state`` is the desk's work classification: ``decided_stale``
    (a decision rests on superseded facts — first work of the
    day), ``awaiting`` (a brief waits at the gate, including one
    that failed verification and must be re-assembled),
    ``decided`` (decided, facts unmoved since), ``no_brief``
    (nothing assembled yet).
    """

    account_id: str
    account_name: str
    state: Literal["decided_stale", "awaiting", "decided", "no_brief"]
    brief_id: str | None = None  # the account's latest brief
    brief_status: str | None = None
    brief_as_of: date | None = None
    decision: DeskDecision | None = None
    staleness: DeskStaleness | None = None
    # The request-info ask riding on the account's latest DECIDED
    # brief, when one was opened. ``ready_for_review`` is the
    # desk's signal that the broker has answered everything and
    # the account waits on the underwriter again: it is set only
    # when the ask is complete, belongs to the account's latest
    # decided brief, and that brief is still the latest brief —
    # once the underwriter re-assembles, the new brief is the work.
    follow_through: DeskFollowThrough | None = None
    ready_for_review: bool = False
    # The expiry watch for this account (see :mod:`timeline`),
    # computed as of the queue's date when the queue is built
    # with port access. Facts the account relies on that are
    # running out — the third clock on the row, beside the
    # decision's staleness and the ask's completeness.
    expiry: list[DeskExpiry] = Field(default_factory=list)


class ReviewGroup(BaseModel):
    """One document's candidates in the review queue (see
    :mod:`review`): the assertions a correspondence document
    carried, grouped under the document they came from, with
    the source excerpt the underwriter reads them against.
    Candidates of every state ride the group — pending work
    first in the surfaces' concern, with confirmed and rejected
    records shown as the audit they are."""

    account_id: str
    account_name: str
    episode_id: str
    doc_id: str
    doc_title: str
    document_received_at: date
    excerpt: str = ""
    candidates: list[CandidateFact] = Field(default_factory=list)
    pending_count: int = 0


# -- API request shapes ------------------------------------------------------


class AssembleRequest(BaseModel):
    account_id: str
    as_of: date | None = None


class DecisionRequest(BaseModel):
    action: DecisionAction
    actor: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class ReviewRequest(BaseModel):
    """A review-gate decision on one candidate: confirm it into
    the graph, or reject it onto the audit record. ``reason`` is
    required for a rejection (the service enforces it — the
    record is the audit of what the desk refused)."""

    account_id: str
    action: Literal["confirm", "reject"]
    actor: str = Field(min_length=1)
    reason: str = ""
