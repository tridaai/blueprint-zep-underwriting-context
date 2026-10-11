"""Temporal resolution: which facts are true *now*, and which were.

The rule, applied identically by assembly, the offline context
block, and the eval pack:

- A fact is a **candidate** at ``as_of`` when its window covers the
  date: ``valid_from <= as_of`` and (``valid_to`` is None or
  ``as_of < valid_to``).
- Among candidates for the same (predicate, entity_ref), the
  **current** fact is the one with the latest ``valid_from``;
  ties break on the latest ``recorded_at`` (a correction recorded
  later wins over the document it corrects).
- Every other fact that was ever in force before ``as_of`` but is
  not current is **superseded** — either replaced by the current
  fact for its key, or expired with no successor. Facts whose
  window has not opened yet are neither: they are the future, and
  a brief must not borrow from it.
"""

from __future__ import annotations

from datetime import date

from .ontology import Fact


def _key(fact: Fact) -> tuple[str, str]:
    return (fact.predicate, fact.entity_ref)


def resolve(
    facts: list[Fact], as_of: date
) -> tuple[list[Fact], list[tuple[Fact, Fact | None]]]:
    """Split ``facts`` into (current, [(superseded, successor|None)]).

    Both lists are deterministically ordered (predicate, entity_ref,
    valid_from) so briefs and evals compare byte-for-byte.
    """
    current: list[Fact] = []
    superseded: list[tuple[Fact, Fact | None]] = []

    by_key: dict[tuple[str, str], list[Fact]] = {}
    for fact in facts:
        if fact.valid_from <= as_of:
            by_key.setdefault(_key(fact), []).append(fact)

    winners: dict[tuple[str, str], Fact] = {}
    for key, group in by_key.items():
        covering = [
            f for f in group if f.valid_to is None or as_of < f.valid_to
        ]
        if covering:
            winner = max(covering, key=lambda f: (f.valid_from, f.recorded_at))
            winners[key] = winner
            current.append(winner)

    for key, group in by_key.items():
        winner = winners.get(key)
        for fact in group:
            if winner is not None and fact.fact_id == winner.fact_id:
                continue
            superseded.append((fact, winner))

    current.sort(key=lambda f: (f.predicate, f.entity_ref, f.valid_from))
    superseded.sort(
        key=lambda pair: (pair[0].predicate, pair[0].entity_ref, pair[0].valid_from)
    )
    return current, superseded
