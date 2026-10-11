"""The insurance-account ontology: entities, edges, facts, episodes.

This is the versioned schema a deployment team designs per carrier —
the work an FDE actually does on top of a context layer. Zep (or the
offline adapter) stores the graph; this module defines what the
graph *means* for specialty underwriting:

- **Entities** — the things an account's history is about.
- **Edges** — typed relationships between them.
- **Facts** — one entity's predicate holding one value over a
  *validity window*. Facts are how the graph stays honest over time:
  when a limit is endorsed down mid-term, the old fact's window
  closes and a new one opens. Nothing is overwritten, so "what was
  true on the day the underwriter looked" is always answerable.
- **Episodes** — the raw ingested units (one source document each).
  Every fact cites exactly one episode, and every episode names its
  source document, so every line in a brief traces to paper.

All models are Pydantic and JSON-serializable; the same definitions
describe the offline graph and the ontology payload a Zep deployment
registers (custom entity/edge types on the Zep side).
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

ONTOLOGY_VERSION = "uw-context/1.0"

# ---------------------------------------------------------------------------
# Entity and edge types (the ontology proper)
# ---------------------------------------------------------------------------

ENTITY_TYPES: dict[str, str] = {
    "insured_account": "The insured business whose risk is underwritten.",
    "broker": "The placing broker of record for an account or submission.",
    "policy": "A policy (bound, quoted, or expired) held by the account.",
    "claim": "A claim filed by the account.",
    "submission": "A broker submission package for a policy period.",
    "correspondence": "A letter, email, or note exchanged about the account.",
    "location": "A premises or site the account operates from.",
    "coverage": "A coverage line (e.g. product liability) on a policy.",
}

EDGE_TYPES: dict[str, str] = {
    "placed_by": "insured_account -> broker: the broker places this account.",
    "holds": "insured_account -> policy: the account holds the policy.",
    "covers": "policy -> coverage: the policy includes the coverage line.",
    "located_at": "insured_account -> location: the account operates there.",
    "filed": "insured_account -> claim: the account filed the claim.",
    "arising_under": "claim -> policy: the claim arose under the policy.",
    "submitted_by": "submission -> broker: the broker sent the submission.",
    "concerns": "submission|correspondence -> insured_account: about this account.",
}

# Predicates a complete brief is expected to resolve for an account.
# Assembly reports which of these are missing instead of guessing.
EXPECTED_PREDICATES: tuple[str, ...] = (
    "legal_name",
    "risk_class",
    "annual_revenue_usd",
    "primary_location",
    "policy_limit_usd",
    "claims_count_3yr",
)

# How each predicate is named when an underwriter — or a broker —
# reads it: the request-info draft (see followthrough.py) asks for
# missing items by these names, never by raw predicate. A predicate
# outside the map is named by its raw form (the fallback keeps a new
# ontology predicate askable before its label is written).
PREDICATE_LABELS: dict[str, str] = {
    "legal_name": "Legal name of the insured",
    "risk_class": "Risk class",
    "annual_revenue_usd": "Annual revenue (USD)",
    "primary_location": "Primary operating location",
    "policy_limit_usd": "Policy limit (USD)",
    "claims_count_3yr": "Claims count, last 3 years",
    "underwriter_decision": "Underwriter decision",
    "deductible_usd": "Deductible (USD)",
    "inspection_grade": "Inspection grade",
    "sprinkler_coverage_pct": "Sprinkler coverage (%)",
}


def predicate_label(predicate: str) -> str:
    """The underwriter-terms name for a predicate."""
    return PREDICATE_LABELS.get(predicate, predicate)


# ---------------------------------------------------------------------------
# Provenance tiers: where a document's assertions come from decides
# how they enter the graph.
#
# - ``system_record`` — produced inside the system itself (the gate's
#   decision memos). It enters on arrival: the system does not review
#   its own records.
# - ``filed_document`` — the carrier's structured paper: submissions,
#   claims extracts, endorsement schedules, inspection reports. The
#   assertions arrive as structured extracts of filed documents and
#   enter on arrival.
# - ``correspondence`` — broker letters, emails, and notes. Free-text
#   channels: the injection screen already treats their prose as
#   untrusted, and their *assertions* are held to the same standard —
#   they land as candidate facts (see CandidateFact) and enter the
#   graph only when an underwriter confirms them at the review gate
#   (see review.py). Nothing else is gated: the tier is a property
#   of the document kind, decided here, in one place.
# ---------------------------------------------------------------------------

ORIGIN_TIERS: dict[str, str] = {
    "decision_memo": "system_record",
    "submission": "filed_document",
    "claims_extract": "filed_document",
    "endorsement": "filed_document",
    "inspection": "filed_document",
    "correspondence": "correspondence",
}


def origin_tier_for_kind(kind: str) -> str:
    """The provenance tier a document kind's assertions carry.

    An unknown kind is filed paper by default — the correspondence
    tier must be chosen by the ontology, never fallen into."""
    return ORIGIN_TIERS.get(kind, "filed_document")


FactValue = str | int | float


class SourceDocument(BaseModel):
    """One document in an account's file, as it arrived.

    ``assertions`` are the structured facts the document carries (an
    ACORD extract, a claims run, an endorsement schedule). Free
    ``text`` rides along for correspondence: it is untrusted input —
    screened before anything in it is believed (see guardrails.py).
    """

    doc_id: str
    account_id: str
    kind: Literal[
        "submission", "claims_extract", "endorsement", "inspection",
        "correspondence", "decision_memo",
    ]
    title: str
    received_at: date
    text: str = ""
    assertions: list["FactAssertion"] = Field(default_factory=list)
    edges: list["EdgeAssertion"] = Field(default_factory=list)


class FactAssertion(BaseModel):
    """A fact as one document asserts it, before temporal resolution."""

    predicate: str
    value: FactValue
    unit: str = ""
    valid_from: date
    valid_to: date | None = None  # None = still in force when asserted
    entity_ref: str = ""  # e.g. "POL-2210" for a policy-scoped fact


class EdgeAssertion(BaseModel):
    source_ref: str
    edge_type: str
    target_ref: str
    label: str = ""


class Episode(BaseModel):
    """One ingested source document inside the context graph."""

    episode_id: str
    account_id: str
    doc_id: str
    kind: str
    title: str
    received_at: date
    screened: bool = False
    quarantined: bool = False
    quarantine_reason: str = ""


class Fact(BaseModel):
    """A resolved fact in the graph: an assertion plus its lineage."""

    fact_id: str
    account_id: str
    predicate: str
    value: FactValue
    unit: str = ""
    entity_ref: str = ""
    valid_from: date
    valid_to: date | None = None
    # The date the graph recorded the fact. For facts that enter on
    # arrival (system records, filed documents) that is the
    # asserting document's received date. For a fact that entered
    # through the review gate it is the confirmation date: the
    # document's own date is document truth and stays on its
    # episode and its candidate record, but the graph did not
    # record the fact until the desk attested it — and temporal
    # resolution's tie-break (latest recorded_at wins among equal
    # windows) then honours the attestation as the latest
    # recording, instead of letting a confirmed fact silently lose
    # to paper that arrived while it waited for review.
    recorded_at: date
    episode_id: str
    doc_id: str
    # The provenance tier of the asserting document (see
    # ORIGIN_TIERS). Facts written before the tiers existed carry
    # the filed-document default when read back.
    origin: str = "filed_document"
    # The review gate's stamps, set only on a fact that entered by
    # confirmation: who attested it, and when (== recorded_at).
    confirmed_by: str = ""
    confirmed_at: date | None = None


class CandidateFact(BaseModel):
    """A correspondence assertion waiting at the review gate.

    The extracted fact, exactly as its document asserted it, plus
    the review state: ``pending`` until an underwriter confirms it
    (it becomes a :class:`Fact` in the graph, keeping this id) or
    rejects it (the record stays, with the reason — the audit of
    what the desk refused, never a silent deletion). Candidates
    live in the cited layer beside the facts (adapters + citation
    index), but no read surface serves them: briefs, timelines,
    and the expiry watch read facts only, so a pending candidate
    is indistinguishable from absent until it is confirmed.
    """

    candidate_id: str  # the fact_id the fact carries on confirmation
    account_id: str
    episode_id: str
    doc_id: str
    doc_title: str
    doc_kind: str  # the asserting document's kind ("correspondence")
    origin: str = "correspondence"
    predicate: str
    value: FactValue
    unit: str = ""
    entity_ref: str = ""
    valid_from: date
    valid_to: date | None = None
    document_received_at: date  # the asserting document's own date
    excerpt: str = ""  # the document's free text — untrusted, fenced at render
    state: Literal["pending", "confirmed", "rejected"] = "pending"
    review_actor: str = ""
    review_reason: str = ""
    reviewed_at: datetime | None = None
    fact_recorded_at: date | None = None  # set on confirmation


class Edge(BaseModel):
    account_id: str
    source_ref: str
    edge_type: str
    target_ref: str
    label: str = ""
    doc_id: str


class Account(BaseModel):
    account_id: str
    display_name: str
    documents: list[SourceDocument] = Field(default_factory=list)


SourceDocument.model_rebuild()
