"""Support grading: how well-backed each current fact is.

Every fact on a brief is cited — but citations are not equal. A
fact the system recorded itself, one standing on filed carrier
paper, and one a broker asserted in a letter the desk confirmed
at the review gate carry different weight, and an underwriter
reading the brief should not have to reconstruct that from the
citation lines. This module grades it, in code, at assembly:

- **The inputs are the provenance the graph already carries** —
  the citing fact's origin tier (round 6's ontology tiers, read
  here and never redefined), the set of *independent* documents
  whose in-force assertions agree with the resolved value, and
  whether an earlier, different value was displaced by this one.
  Independence means distinct documents: one document asserting
  a value twice is still one document, and a re-ingestion never
  creates a second episode to count. Quarantined documents and
  unconfirmed candidates are not facts, so they can neither
  support nor contest — the screens upstream of the graph are
  what keep a planted contradiction from "contesting" a fact.
- **The grades** (authored here, printed on the brief):

  - ``STRONG`` — the system's own record (a decision memo: it
    *is* the record, there is nothing behind it to check), or
    filed paper corroborated by a second independent document.
  - ``STANDARD`` — a single filed document, or a correspondence
    fact corroborated by a second independent document. A
    broker's letter caps here however much corroboration it
    gathers: its tier is the ceiling.
  - ``THIN`` — a single correspondence source, or any fact a
    contest demotes into it (below).

- **Contested.** A current fact is contested when a superseded
  fact for the same key carried a different value and this fact
  is the successor the temporal rule names for it — the limit
  the endorsement reduced, the revenue the loss run restated.
  A contest demotes the grade one step: a value that displaced
  another value is never graded on its paper alone, because
  the underwriter's first question about it is whether the
  displacement itself was right. The displaced value and its
  document ride the grade, so the brief can name what the
  current value replaced.

Grades are **derived at assembly** from facts already in the
graph — nothing new is stored, no migration: a brief stored
before grading existed parses with ``support=None`` on its
facts and stays byte-identical, and a grade says nothing a
decision can act on. It changes no gate, no staleness rule,
no follow-through state — it is information for the human,
printed where the human reads.
"""

from __future__ import annotations

from datetime import date

from .ontology import Fact
from .schemas import FactSupport
from .temporal import resolve

#: The grade vocabulary, best to worst. Authored here, printed
#: on every brief that carries grades.
SUPPORT_GRADES: tuple[str, ...] = ("STRONG", "STANDARD", "THIN")

#: How the grades read when an underwriter meets them.
GRADE_LEGEND = (
    "STRONG — the system's own record, or filed paper a second "
    "document corroborates. STANDARD — a single filed document, "
    "or a broker's letter with corroboration. THIN — a single "
    "correspondence source, or a value standing where an earlier "
    "value was replaced."
)

_DEMOTE = {"STRONG": "STANDARD", "STANDARD": "THIN", "THIN": "THIN"}


def _covers(fact: Fact, as_of: date) -> bool:
    """In force at ``as_of`` — the temporal rule's candidate test."""
    return fact.valid_from <= as_of and (
        fact.valid_to is None or as_of < fact.valid_to
    )


def assess_support(
    facts: list[Fact],
    as_of: date,
    doc_titles: dict[str, str] | None = None,
) -> dict[str, FactSupport]:
    """Grade every current fact in ``facts`` at ``as_of``.

    Returns ``{fact_id: FactSupport}`` for the facts the
    temporal rule resolves as current — keyed by fact id,
    because two documents can assert the same key and only the
    winner is graded (its supporters are counted, not graded).
    Pure: the same facts and date grade identically every time.
    """
    doc_titles = doc_titles or {}
    current, superseded = resolve(facts, as_of)

    covering_by_key: dict[tuple[str, str], list[Fact]] = {}
    for fact in facts:
        if _covers(fact, as_of):
            covering_by_key.setdefault(
                (fact.predicate, fact.entity_ref), []
            ).append(fact)

    displaced_by_winner: dict[str, list[Fact]] = {}
    for old, successor in superseded:
        if successor is not None:
            displaced_by_winner.setdefault(successor.fact_id, []).append(old)

    graded: dict[str, FactSupport] = {}
    for winner in current:
        key = (winner.predicate, winner.entity_ref)
        supporting = {
            f.doc_id
            for f in covering_by_key.get(key, [])
            if f.value == winner.value
        }
        count = len(supporting)
        tier = winner.origin
        if tier == "system_record":
            grade = "STRONG"
            reasons = ["the system's own record"]
        elif tier == "correspondence":
            if count >= 2:
                grade = "STANDARD"
                reasons = [
                    "a broker's letter, corroborated by "
                    f"{count} documents"
                ]
            else:
                grade = "THIN"
                reasons = ["a single correspondence source"]
        else:
            # filed_document — and any tier the ontology has not
            # named, which the ontology itself defaults to filed.
            if count >= 2:
                grade = "STRONG"
                reasons = [
                    f"filed paper, corroborated by {count} documents"
                ]
            else:
                grade = "STANDARD"
                reasons = ["a single filed document"]

        displaced = [
            old
            for old in displaced_by_winner.get(winner.fact_id, [])
            if old.value != winner.value
        ]
        contested = bool(displaced)
        named = (
            max(displaced, key=lambda o: (o.valid_from, o.recorded_at))
            if displaced
            else None
        )
        if contested:
            grade = _DEMOTE[grade]
            reasons = reasons + [
                "contested — an earlier value was replaced"
            ]
        graded[winner.fact_id] = FactSupport(
            grade=grade,
            tier=tier,
            supporting_documents=count,
            contested=contested,
            contested_value=named.value if named else None,
            contested_doc_id=named.doc_id if named else "",
            contested_doc_title=(
                doc_titles.get(named.doc_id, "") if named else ""
            ),
            reasons=reasons,
        )
    return graded


def thin_count(facts: list[Fact], as_of: date) -> int:
    """How many of the account's current facts grade THIN at
    ``as_of`` — the count the desk queue and the portfolio
    brief display (unweighted: see portfolio.py)."""
    return sum(
        1
        for support in assess_support(facts, as_of).values()
        if support.grade == "THIN"
    )
