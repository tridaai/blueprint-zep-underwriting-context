"""Render a brief as text: the export and demo view of the record.

Rendered from the same :class:`Brief` the API serves as JSON, so
the two can never disagree. Citations ride on every fact line —
that is the point of the document.
"""

from __future__ import annotations

from .ontology import FactValue
from .schemas import Brief


def _value(value: FactValue, unit: str) -> str:
    if isinstance(value, (int, float)) and abs(value) >= 1000:
        text = f"{value:,}"
    else:
        text = str(value)
    return f"{text} {unit}".strip()


def render_brief_text(brief: Brief) -> str:
    lines: list[str] = []
    lines.append(f"UNDERWRITING CONTEXT BRIEF — {brief.account_name}")
    lines.append(
        f"Account {brief.account_id} · as of {brief.as_of.isoformat()} · "
        f"context backend: {brief.backend} · brief {brief.brief_id}"
    )
    lines.append(f"Status: {brief.status}")
    lines.append("")
    lines.append("SUMMARY (narrative — not a source of facts)")
    lines.append(f"[{brief.narrative_source}] {brief.narrative}")
    lines.append("")
    lines.append("CURRENT FACTS")
    if not brief.current_facts:
        lines.append("  (none resolved)")
    for fact in brief.current_facts:
        ref = f" [{fact.entity_ref}]" if fact.entity_ref else ""
        lines.append(
            f"  - {fact.predicate}{ref}: {_value(fact.value, fact.unit)} "
            f"(in force from {fact.valid_from.isoformat()}) "
            f"[source: {fact.doc_title} ({fact.doc_id}), "
            f"recorded {fact.recorded_at.isoformat()}]"
        )
    lines.append("")
    lines.append("SUPERSEDED FACTS (shown as superseded — not current)")
    if not brief.superseded_facts:
        lines.append("  (none)")
    for fact in brief.superseded_facts:
        if fact.reason == "replaced":
            outcome = f"replaced by {_value(fact.successor_value, fact.unit)}"
        else:
            outcome = "expired with no successor"
        lines.append(
            f"  - {fact.predicate}: {_value(fact.value, fact.unit)} "
            f"(was in force from {fact.valid_from.isoformat()}) — {outcome} "
            f"[source: {fact.doc_title} ({fact.doc_id})]"
        )
    lines.append("")
    if brief.diff is None:
        lines.append("WHAT CHANGED")
        lines.append("  First brief — no earlier brief to compare against.")
    else:
        diff = brief.diff
        lines.append(
            "WHAT CHANGED SINCE "
            f"{diff.previous_as_of.isoformat()} (brief {diff.previous_brief_id})"
        )
        if not (diff.added or diff.superseded or diff.removed or diff.decisions):
            lines.append(
                "  No changes — the facts stand as the previous brief left them."
            )
        for fact in diff.added:
            ref = f" [{fact.entity_ref}]" if fact.entity_ref else ""
            lines.append(
                f"  + added: {fact.predicate}{ref}: "
                f"{_value(fact.value, fact.unit)} "
                f"[source: {fact.doc_title} ({fact.doc_id})]"
            )
        for change in diff.superseded:
            ref = f" [{change.entity_ref}]" if change.entity_ref else ""
            now = (
                _value(change.current_value, change.unit)
                if change.current_value is not None
                else "no current successor"
            )
            lines.append(
                f"  ~ superseded: {change.predicate}{ref}: "
                f"{_value(change.previous_value, change.unit)} → {now}"
            )
        for fact in diff.removed:
            ref = f" [{fact.entity_ref}]" if fact.entity_ref else ""
            lines.append(
                f"  - removed: {fact.predicate}{ref}: "
                f"{_value(fact.value, fact.unit)} — no longer current "
                f"[was source: {fact.doc_title} ({fact.doc_id})]"
            )
        for decision in diff.decisions:
            by = f" by {decision.actor}" if decision.actor else ""
            on = (
                f" on {decision.decided_on.isoformat()}"
                if decision.decided_on
                else ""
            )
            lines.append(
                f"  · decision recorded: {decision.action} on brief "
                f"{decision.brief_id}{by}{on}"
            )
    lines.append("")
    lines.append("NOT RESOLVED FROM THE FILE")
    lines.append(
        "  " + (", ".join(brief.missing_predicates) if brief.missing_predicates else "(none)")
    )
    if brief.edges:
        lines.append("")
        lines.append("RELATIONSHIPS")
        for edge in brief.edges:
            label = f" — {edge.label}" if edge.label else ""
            lines.append(f"  - {edge.source_ref} -{edge.edge_type}-> {edge.target_ref}{label}")
    if brief.quarantined:
        lines.append("")
        lines.append("QUARANTINED SOURCES (assertions excluded)")
        for note in brief.quarantined:
            lines.append(f"  - {note.doc_title} ({note.doc_id}): {note.reason}")
    if brief.context_block:
        lines.append("")
        lines.append(
            f"PROVIDER CONTEXT (labelled, not a citation source — via {brief.context_block_source})"
        )
        lines.append(brief.context_block)
    elif brief.context_block_source == "unavailable":
        lines.append("")
        lines.append("PROVIDER CONTEXT: unavailable — brief assembled from cited facts alone.")
    if brief.flags:
        lines.append("")
        lines.append("FLAGS")
        for flag in brief.flags:
            lines.append(f"  - {flag}")
    if brief.verification is not None:
        lines.append("")
        v = brief.verification
        lines.append(
            "VERIFICATION: "
            f"citations {'complete' if v.citations_complete else 'INCOMPLETE'}, "
            f"leakage {'none' if v.leakage_free else 'FOUND'}"
        )
        for violation in v.violations:
            lines.append(f"  - {violation}")
    lines.append("")
    if brief.decision is not None:
        d = brief.decision
        lines.append(
            f"GATE DECISION: {d.action} by {d.actor} "
            f"at {d.decided_at.isoformat()} — {d.reason}"
        )
    else:
        lines.append("GATE: AWAITING_UNDERWRITER — no decision recorded.")
    return "\n".join(lines) + "\n"
