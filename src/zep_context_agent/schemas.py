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

from .ontology import Edge, Episode, FactValue

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
    decision: Decision | None = None
    audit: list[AuditEntry] = Field(default_factory=list)
    created_at: datetime


# -- API request shapes ------------------------------------------------------


class AssembleRequest(BaseModel):
    account_id: str
    as_of: date | None = None


class DecisionRequest(BaseModel):
    action: DecisionAction
    actor: str = Field(min_length=1)
    reason: str = Field(min_length=1)
