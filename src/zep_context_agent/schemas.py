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


class FactSupport(BaseModel):
    """How well-backed one current fact is (see :mod:`support`).

    Computed in code at assembly from the fact's provenance:
    the origin tier of its citing document, how many independent
    documents (distinct documents — never re-ingestions) carry
    an in-force assertion agreeing with it, and whether it
    displaced an earlier, different value (``contested`` — the
    displaced value and its document are named, so the brief
    can say what the current value replaced). A grade is
    information for the underwriter: it feeds no gate, no
    staleness rule, no follow-through state."""

    grade: Literal["STRONG", "STANDARD", "THIN"]
    tier: str  # the citing document's origin tier (round 6)
    supporting_documents: int
    contested: bool = False
    contested_value: FactValue | None = None
    contested_doc_id: str = ""
    contested_doc_title: str = ""
    reasons: list[str] = Field(default_factory=list)


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
    # The support grade (see :mod:`support`), computed at
    # assembly. ``None`` on briefs stored before grading
    # existed — the record is never rewritten to add one.
    support: FactSupport | None = None


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


class RenewalBaseline(BaseModel):
    """The last decision a renewal brief is decided against
    (see :mod:`renewals`): the decided brief's identity and its
    gate decision, quoted from that brief's record — the brief
    itself is never edited to carry this; the quotation lives
    on the renewal."""

    brief_id: str
    as_of: date  # the baseline brief's as-of
    action: DecisionAction
    actor: str
    reason: str  # the decider's rationale, quoted from the record
    decided_at: datetime


class RenewalLedger(BaseModel):
    """The change ledger since the baseline decision, computed
    in code over the two briefs' stored payloads in the brief
    diff's idiom (see :mod:`renewals`): facts new since the
    decision, facts superseded (the value the decision rested
    on, handed off to the value standing now, both documents
    cited), facts lapsed since (current at the decision, not
    current now — the window closed with no successor in
    force), and the facts that stand exactly as decided,
    counted rather than listed: the ledger is about change.
    The baseline decision's own echo — the ``underwriter_
    decision`` fact its decision memo planted — is not a new
    fact; it is the baseline, shown in the baseline block."""

    new_facts: list[DiffFact] = Field(default_factory=list)
    superseded: list[DiffSuperseded] = Field(default_factory=list)
    lapsed: list[DiffFact] = Field(default_factory=list)
    unchanged_count: int = 0


class RenewalOpenAsk(BaseModel):
    """A still-open follow-through ask inherited by a renewal
    brief: named with its state, never answered, closed, or
    altered by the renewal — the ask's own record is the only
    place its state changes."""

    record_id: str
    brief_id: str  # the brief the ask rides on
    origin: Literal["decision", "renewal_chase"]
    status: Literal["open", "partially_received"]
    received_count: int
    total_count: int
    opened_by: str
    opened_at: datetime


class RenewalOpenStaleness(BaseModel):
    """An open staleness record touching the account, inherited
    by a renewal brief: the stale decision it refers to and
    when the watch flagged it. The renewal decision — like any
    fresh decision — is what resolves it; assembling the
    renewal does not."""

    record_id: str
    brief_id: str  # the DECIDED brief whose facts went stale
    decision_action: str
    decision_actor: str
    decided_at: datetime
    flagged_at: datetime


class RenewalInherited(BaseModel):
    """The open business a renewal brief inherits: the asks
    still waiting on the broker and the staleness flags still
    open on the account, read from the stores that own them at
    assembly time."""

    open_asks: list[RenewalOpenAsk] = Field(default_factory=list)
    open_staleness: list[RenewalOpenStaleness] = Field(default_factory=list)


class RenewalSection(BaseModel):
    """A brief assembled AS a renewal (see :mod:`renewals`):
    the baseline decision it is decided against, the change
    ledger since that decision, and the open business it
    inherits. ``None`` on a standard brief — standard assembly
    is untouched by the mode's existence. The section is
    computed at assembly and stored on the renewal brief; the
    baseline brief's record is read, never written."""

    baseline: RenewalBaseline
    ledger: RenewalLedger
    inherited: RenewalInherited


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
    # Set only on a brief assembled in renewal mode (see
    # :mod:`renewals`): the last decision it is decided
    # against, the change ledger since it, and the open
    # business inherited. ``None`` on every standard brief,
    # and on briefs stored before renewal assembly existed —
    # the record is never rewritten to add one.
    renewal: RenewalSection | None = None
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
    """One thing an ask asked the broker for.

    ``kind`` is ``predicate`` (a fact the brief could not resolve),
    ``document`` (a quarantined document to resend cleanly), or
    ``renewal`` (a current fact whose window is closing — the
    renewal chase's item, see :mod:`chases`). State is ``open``
    until the answer is recognised, then ``received`` — with the
    episode that satisfied it named, so the desk can see exactly
    what closed the item.

    Recognition differs by kind, honestly: a predicate item is
    answered when the predicate becomes a current fact; a renewal
    item is answered only when a successor fact's window *covers*
    the chased fact's end (the timeline's successor rule) — the
    expiring fact is already current, so its mere presence can
    never satisfy its own chase. Renewal items therefore carry
    the chased fact's identity: ``fact_id``, its value and unit,
    its entity, the document it stands on, and ``expires_on``
    (its ``valid_to``).
    """

    kind: Literal["predicate", "document", "renewal"]
    predicate: str = ""  # predicate + renewal items
    label: str  # the underwriter-terms name the draft asked by
    doc_id: str = ""  # document items: the quarantined document;
    # renewal items: the document the chased fact stands on
    doc_title: str = ""
    episode_id: str = ""  # document items: the quarantined episode
    reason: str = ""  # document items: why it was quarantined
    entity_ref: str = ""  # renewal items: the chased fact's entity
    fact_id: str = ""  # renewal items: the chased fact in the graph
    value: FactValue | None = None  # renewal items: the chased value
    unit: str = ""  # renewal items: the chased fact's unit
    expires_on: date | None = None  # renewal items: the window's end
    status: Literal["open", "received"] = "open"
    received_episode_id: str = ""
    received_at: datetime | None = None


class FollowThroughRecord(BaseModel):
    """An ask to the broker, followed through.

    Two origins, one record. A ``request_info`` decision opens a
    record (``origin == "decision"``): the ask — missing
    predicates, quarantined documents to resend — composed in
    code from the decided brief. A renewal chase opens the other
    (``origin == "renewal_chase"``, see :mod:`chases`): the ask
    for the renewal document that would refresh an expiring
    fact, composed in code from the fact and its expiry, and
    attached to the account's latest brief — the file it belongs
    to — whatever that brief's decision state. Records stored
    before origins existed parse as ``"decision"``.

    Either way the draft is deterministic text the underwriter
    can copy; the system itself sends nothing. Items flip to
    ``received`` as broker replies are ingested and recognised.
    The record closes (``complete``) when every item is received.

    The brief is never edited; this record lives apart from it,
    like the staleness record. The watches are independent: an
    ask can complete while a staleness flag is open, and a flag
    can resolve while an ask is still open. For a chase record,
    ``decision_actor`` / ``decision_reason`` / ``decided_at``
    carry the chase's own act: who sent the chase, the composed
    reason (the expiring facts it names), and when.
    """

    record_id: str
    account_id: str
    brief_id: str  # the brief the ask came from / attaches to
    origin: Literal["decision", "renewal_chase"] = "decision"
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


class ChaseFact(BaseModel):
    """One expiring (or lapsed, or satisfied) fact on a renewal
    chase row: the watch's own row for the fact, verbatim, plus —
    on a SATISFIED row — the document whose arrival refreshed it.
    ``watch_state`` is the expiry watch's verdict for the fact at
    the chase's as-of date."""

    predicate: str
    entity_ref: str = ""
    value: FactValue
    unit: str = ""
    valid_from: date
    valid_to: date
    watch_state: Literal["expiring", "lapsed"]
    days_remaining: int
    successor_exists: bool = False
    episode_id: str = ""
    doc_id: str = ""
    doc_title: str = ""
    satisfied_doc_id: str = ""  # SATISFIED rows: the renewal's paper
    satisfied_doc_title: str = ""


class RenewalChase(BaseModel):
    """One renewal chase: an account, a predicate, and the state
    of the desk's attempt to get the expiring fact renewed (see
    :mod:`chases`). A pure projection over the expiry watch, the
    follow-through store, and the brief store — computed, never
    stored; the only durable artifact a chase ever creates is
    the follow-through ask its send opens.

    States, in the order the projection resolves them:

    - ``ASKED`` — a chase ask for this predicate is open: the
      broker owes the renewal.
    - ``LAPSED_UNSENT`` — a flagged fact has lapsed while no
      chase was ever sent. The failure is named, not aged out;
      the chase can still be sent, late.
    - ``WAITING_ON_BRIEF`` — facts are expiring but the account
      has no brief: an ask attaches to a brief, so there is
      nothing to send against yet.
    - ``OPEN`` — facts are expiring, a brief exists to attach
      to, and no ask covers the predicate: the chase can be sent.
    - ``SATISFIED`` — a chase ask completed and nothing under
      the predicate is flagged any more: the renewal landed,
      and the row names the document that carried it.

    A predicate an open request-info ask already covers gets no
    row at all — the dedupe: the broker is already being asked
    about that predicate, and two asks never run for one line
    of the file.
    """

    account_id: str
    account_name: str
    predicate: str
    label: str  # the predicate's underwriter-terms name
    state: Literal[
        "OPEN", "ASKED", "SATISFIED", "LAPSED_UNSENT", "WAITING_ON_BRIEF"
    ]
    facts: list[ChaseFact] = Field(default_factory=list)
    # The brief a send would attach to (the account's latest),
    # or the brief the chase ask rides on. ``None`` only in
    # WAITING_ON_BRIEF — there is no brief.
    brief_id: str | None = None
    brief_status: str | None = None
    ask_record_id: str | None = None  # the chase's ask, once sent
    ask_status: str | None = None
    # The soonest fact's days remaining (negative when lapsed);
    # the row's clock. ``None`` on a SATISFIED row: the chase is
    # closed, and a closed chase has no clock.
    days_remaining: int | None = None


class DeskChase(BaseModel):
    """A renewal chase, as the desk queue shows it: the
    predicate, its state, and its clock. An annotation on the
    account's row — the queue's order never reads it."""

    predicate: str
    label: str
    state: Literal[
        "OPEN", "ASKED", "SATISFIED", "LAPSED_UNSENT", "WAITING_ON_BRIEF"
    ]
    days_remaining: int | None = None
    ask_record_id: str | None = None


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
    # The account's latest brief was assembled as a renewal
    # (see :mod:`renewals`) — it is being decided against the
    # account's last decision. An annotation like the watch:
    # the queue's order never reads it.
    brief_is_renewal: bool = False
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
    # How many of the account's current facts grade THIN on
    # support (see :mod:`support`), computed as of the queue's
    # date when the queue is built with port access — the same
    # condition as the watch. ``None`` when the queue was built
    # without the port: uncomputed, not zero. A display count
    # only; the queue's order never reads it.
    thin_fact_count: int | None = None
    # The account's renewal chases (see :mod:`chases`), computed
    # under the same condition as the watch (port access) plus
    # the follow-through store: expiring facts the desk can
    # chase, chases already asked, renewals that landed, and
    # lapses nobody chased. An annotation only — the queue's
    # order never reads it.
    chases: list[DeskChase] = Field(default_factory=list)


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


class PortfolioSignals(BaseModel):
    """One account's signal counts for the morning portfolio
    brief (see :mod:`portfolio`). Every count is read from the
    machinery that owns the verdict — the staleness watch's
    open records, the follow-through store's open asks, the
    expiry watch's rows, the review gate's pending candidates —
    never re-derived here.

    ``no_decided_brief`` is a flag, not a count: 1 when no
    brief for the account has ever been decided. An unexamined
    account is not a clean one, and this is the field that
    says so."""

    stale_decisions: int = 0
    lapsed_facts: int = 0
    open_asks: int = 0
    expiring_facts: int = 0
    pending_candidates: int = 0
    no_decided_brief: int = 0


class PortfolioRow(BaseModel):
    """One account's line in the portfolio brief: its signal
    counts and the score the published weights compute from
    them. The row carries the counts beside the score so the
    ranking is auditable at a glance — the desk never has to
    take the number on faith."""

    account_id: str
    account_name: str
    signals: PortfolioSignals
    score: int
    # How many of the account's current facts grade THIN on
    # support (see :mod:`support`), as of the brief's date.
    # Displayed on the row, deliberately NOT a signal: it has
    # no weight, enters no score, and cannot move the ranking
    # — the published weights are a separate, deliberate act.
    thin_facts: int = 0
    # How many renewal chases stand open on the account (see
    # :mod:`chases`) as of the brief's date: expiring facts
    # awaiting a chase, chases asked and unanswered, lapses
    # nobody chased. Displayed on the row under exactly the
    # thin-facts discipline — NOT a signal, no weight, no
    # score, no ranking movement. A satisfied chase is closed
    # business and does not count.
    open_chases: int = 0


class PortfolioBrief(BaseModel):
    """The morning portfolio brief (see :mod:`portfolio`): the
    whole book ranked by priority score, as of a stated date,
    with the ranking weights printed on its face. A pure
    projection over the port and the record stores — building
    it mutates nothing, and the per-account surfaces (the
    desk queue, the expiry watch) are untouched by it."""

    as_of: date
    horizon_days: int
    weights: dict[str, int]
    rows: list[PortfolioRow] = Field(default_factory=list)


# -- API request shapes ------------------------------------------------------


class AssembleRequest(BaseModel):
    account_id: str
    as_of: date | None = None
    # "standard" (the default — the brief as it has always
    # assembled) or "renewal" (decide against the account's
    # last decision; see :mod:`renewals`). Renewal mode is an
    # explicit request, never a silent behaviour change, and
    # is refused by name when the account has no decided brief
    # to renew against.
    mode: Literal["standard", "renewal"] = "standard"


class DecisionRequest(BaseModel):
    action: DecisionAction
    actor: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class ChaseSendRequest(BaseModel):
    """Sending one renewal chase: a named human act, like every
    gate decision here — the send opens a follow-through ask in
    the sender's name, and the record carries it."""

    actor: str = Field(min_length=1)


class ReviewRequest(BaseModel):
    """A review-gate decision on one candidate: confirm it into
    the graph, or reject it onto the audit record. ``reason`` is
    required for a rejection (the service enforces it — the
    record is the audit of what the desk refused)."""

    account_id: str
    action: Literal["confirm", "reject"]
    actor: str = Field(min_length=1)
    reason: str = ""
