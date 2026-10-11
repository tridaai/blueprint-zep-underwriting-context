"""Brief diffs: what changed since the last brief, computed in code.

An underwriter revisiting an account asks one question first:
*what changed?* Round 2 answers it from the record. When a brief
is stored, it is compared against the previous stored brief for
the same account, and the comparison is attached to the new brief
as its ``diff``. The comparison reads the two brief payloads only
— fact sets, not prose — so it is deterministic, testable, and
involves no model.

**The baseline rule.** The baseline is the most recent stored
brief for the account that

- is not this brief,
- did not fail verification (a brief that must not be used cannot
  anchor a comparison), and
- has an ``as_of`` **not later than** this brief's. A brief never
  diffs against a brief that looked further into the future: an
  as-of re-assembly dated before an existing brief gets no diff
  rather than a change narrative written with borrowed hindsight.
  Equal as-of is allowed (the store's insertion order
  disambiguates) because re-assembling an account at the same
  as-of after new documents land is the normal desk flow.

When no brief qualifies, ``diff`` stays ``None`` and the surfaces
say *first brief*.

**The comparison.** Facts are keyed by ``(predicate, entity_ref)``
— the temporal rule's own key:

- a key current now that was not current before → **added**;
- a key current in both with a different value → **superseded**:
  the value the last brief showed, handed off to the value this
  one shows;
- a key current before and not current now → **superseded** when
  this brief's superseded section carries the old value as
  *replaced* (the entry names the successor this brief records),
  **removed** when it closed as *expired* — and **removed** when
  it is absent from this brief entirely. A removal is always
  named; nothing leaves the brief silently;
- **decisions**: the baseline brief's own gate decision (recorded
  after it was assembled, so it belongs to the interval), plus
  any ``underwriter_decision`` fact current now that was not
  current before — deduplicated when both name the same decision.
"""

from __future__ import annotations

from .schemas import (
    Brief,
    BriefDiff,
    CitedFact,
    DiffDecision,
    DiffFact,
    DiffSuperseded,
)


def _key(fact: CitedFact) -> tuple[str, str]:
    return (fact.predicate, fact.entity_ref)


def _diff_fact(fact: CitedFact) -> DiffFact:
    return DiffFact(
        predicate=fact.predicate,
        value=fact.value,
        unit=fact.unit,
        entity_ref=fact.entity_ref,
        doc_id=fact.doc_id,
        doc_title=fact.doc_title,
    )


def compute_diff(previous: Brief, current: Brief) -> BriefDiff:
    """The fact-set comparison of two stored brief payloads."""
    prev_current = {_key(f): f for f in previous.current_facts}
    cur_current = {_key(f): f for f in current.current_facts}
    cur_superseded = {
        (f.predicate, f.value, f.entity_ref): f for f in current.superseded_facts
    }

    added: list[DiffFact] = []
    superseded: list[DiffSuperseded] = []
    removed: list[DiffFact] = []

    for key, fact in cur_current.items():
        old = prev_current.get(key)
        if old is None:
            added.append(_diff_fact(fact))
        elif (old.value, old.unit) != (fact.value, fact.unit):
            superseded.append(
                DiffSuperseded(
                    predicate=fact.predicate,
                    entity_ref=fact.entity_ref,
                    unit=fact.unit,
                    previous_value=old.value,
                    current_value=fact.value,
                    previous_doc_id=old.doc_id,
                    current_doc_id=fact.doc_id,
                )
            )

    for key, old in prev_current.items():
        if key in cur_current:
            continue
        retired = cur_superseded.get((old.predicate, old.value, old.entity_ref))
        if retired is not None and retired.reason == "replaced":
            superseded.append(
                DiffSuperseded(
                    predicate=old.predicate,
                    entity_ref=old.entity_ref,
                    unit=old.unit,
                    previous_value=old.value,
                    current_value=retired.successor_value,
                    previous_doc_id=old.doc_id,
                    current_doc_id=retired.successor_doc_id,
                )
            )
        else:
            # Expired, or absent from this brief altogether: named
            # as removed either way — never silently gone.
            removed.append(_diff_fact(old))

    decisions: list[DiffDecision] = []
    named: set[str] = set()
    if previous.decision is not None:
        decisions.append(
            DiffDecision(
                brief_id=previous.brief_id,
                action=previous.decision.action,
                actor=previous.decision.actor,
                decided_on=previous.decision.decided_at.date(),
            )
        )
        named.add(previous.brief_id)
    for fact in current.current_facts:
        if fact.predicate != "underwriter_decision":
            continue
        deciding_brief = fact.entity_ref
        if _key(fact) in prev_current or deciding_brief in named:
            continue
        decisions.append(
            DiffDecision(
                brief_id=deciding_brief,
                action=str(fact.value),
                decided_on=fact.recorded_at,
            )
        )
        named.add(deciding_brief)

    order = lambda entry: (entry.predicate, entry.entity_ref)  # noqa: E731
    return BriefDiff(
        previous_brief_id=previous.brief_id,
        previous_as_of=previous.as_of,
        added=sorted(added, key=order),
        superseded=sorted(superseded, key=order),
        removed=sorted(removed, key=order),
        decisions=decisions,
    )


def previous_brief(store, brief: Brief) -> Brief | None:
    """The baseline for ``brief`` under the rule in the docstring:
    the most recent stored brief for the account that is not this
    brief, did not fail verification, and is dated no later."""
    candidates = [
        b
        for b in store.list(brief.account_id)
        if b.brief_id != brief.brief_id
        and b.status != "VERIFICATION_FAILED"
        and b.as_of <= brief.as_of
    ]
    return candidates[-1] if candidates else None


def attach_diff(store, brief: Brief) -> Brief:
    """Compute ``brief``'s diff against its baseline and store the
    brief again with it attached (a re-save keeps the brief's
    position in the store's order). No baseline → ``diff`` stays
    ``None`` and nothing is re-saved."""
    baseline = previous_brief(store, brief)
    if baseline is None:
        return brief
    brief.diff = compute_diff(baseline, brief)
    store.save(brief)
    return brief
