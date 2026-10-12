"""The decision book: one period, every governed act, in order.

Underwriting gets audited as a book, not account by account.
Until now the trail was complete but scattered: a decision on
its brief, a staleness flag in its store, an ask in another,
a review-gate act in the cited layer — each with its own
per-account surface, and nothing that laid a period's acts
side by side for the reviewer who holds the whole desk
accountable: errors-and-omissions review, a regulator's
sample, the desk's own hindsight.

This module composes that document — a pure projection, like
the timeline and the portfolio brief, over the stores that
own the acts:

- the **brief store** — gate decisions (action, actor,
  reason, the moment recorded). A decision on a renewal brief
  is marked as a renewal and names the baseline brief it was
  decided against;
- the **staleness store** — flags raised (the material change
  that moved under a decision, the episodes that triggered
  it) and flags resolved (by the fresh decision that is the
  only resolution path);
- the **follow-through store** — request-info asks opened,
  items received (with the episode that satisfied each),
  asks completed; and renewal chases sent, which are the
  chase-origin records in the same store, listed under their
  own kind so a chase never double-counts as an ask opening;
- the **review gate** — the cited layer's candidates the desk
  confirmed into the graph or rejected onto the record, with
  the actor and, on a rejection, the reason.

**The window rule, printed on the book's face:** an act
stands in the book when its recorded moment — read in UTC —
falls on or between the period's first and last day. Both
bounds are inclusive. A naive stamp is read as the UTC stamp
it was always meant to be (every writer in this codebase
stamps UTC).

**Undated acts.** Through the service, none exist: every
act's moment is a required field on its record or is stamped
as the act lands (a candidate's ``reviewed_at``, an item's
``received_at``, a flag's ``resolved_at``). What has no
moment is not yet an act — a pending candidate, an open
item, an unresolved flag — and is excluded by state, not by
date. The composition still defends the reviewer's trust: a
record that *claims* a completed act but carries no moment
(a confirmed candidate with no ``reviewed_at`` — unreachable
through the service) is listed in the book's undated
section, named with its source, never silently dropped and
never given a date it does not carry.

**Integrity.** The book carries no generation stamp, so the
same stores compose to the same bytes; a SHA-256 digest over
the book's content (period, sources, counts, entries,
undated) is printed on its face. The face also carries the
coverage statement: the period, the counts by kind, and the
source list above — the completeness rule is that every
source listed contributes every act it holds in the window,
so a future store of acts joins the book by joining that
list, or the book is lying and says so on its face.

Composing a book writes nothing — pinned by test, as every
projection here is. The per-account surfaces are untouched.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone

from .errors import DecisionBookError
from .ontology import Account, predicate_label
from .ports import ContextPort
from .schemas import DecisionBook, DecisionBookEntry, DecisionBookUndated

WINDOW_RULE = (
    "An act stands in the book when its recorded moment — read in "
    "UTC — falls on or between the period's first and last day; "
    "both bounds are inclusive. A record claiming a completed act "
    "but carrying no moment is listed as undated: never dropped, "
    "never dated by invention."
)

SOURCES = [
    "brief store — gate decisions (a renewal brief's decision is "
    "marked, with the baseline it was decided against)",
    "staleness store — decisions gone stale: flags raised and "
    "their resolutions",
    "follow-through store — request-info asks opened, items "
    "received, asks completed; renewal chases sent",
    "review gate (the cited layer) — correspondence candidates "
    "confirmed and rejected",
]

# The act kinds, in the order the face counts them.
KINDS = [
    "decision",
    "review_confirmed",
    "review_rejected",
    "staleness_raised",
    "staleness_resolved",
    "ask_opened",
    "chase_sent",
    "ask_received",
    "ask_completed",
]

# Same-moment ordering between kinds, in the timeline's spirit:
# the human decides, the gate admits or refuses paper, the
# watches react, the asks proceed. Within a kind: account, then
# the source record's identity, then the summary — so the order
# is total and the bytes are stable.
_KIND_RANK = {
    "decision": 0,
    "review_confirmed": 1,
    "review_rejected": 1,
    "staleness_raised": 2,
    "staleness_resolved": 3,
    "ask_opened": 4,
    "chase_sent": 4,
    "ask_received": 5,
    "ask_completed": 6,
}


def _utc(moment: datetime) -> datetime:
    """The moment in UTC; a naive stamp is the UTC stamp its
    writer meant (every writer here stamps UTC)."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _in_window(moment: datetime, start: date, end: date) -> bool:
    return start <= _utc(moment).date() <= end


def _source_ref(entry: DecisionBookEntry) -> str:
    return entry.brief_id or entry.record_id or entry.candidate_id


def _change_summary(changes) -> str:
    parts = []
    if changes.added:
        parts.append(f"{len(changes.added)} fact(s) added")
    if changes.superseded:
        parts.append(f"{len(changes.superseded)} fact(s) superseded")
    if changes.removed:
        parts.append(f"{len(changes.removed)} fact(s) removed")
    return ", ".join(parts) if parts else "no fact movement"


def build_decision_book(
    *,
    port: ContextPort,
    accounts: dict[str, Account],
    brief_store,
    staleness_store,
    follow_store,
    start: date,
    end: date,
) -> DecisionBook:
    """Every governed act recorded in ``[start, end]``, in time
    order. Pure reads; the stores and the cited layer are
    unchanged by building it."""
    if start > end:
        raise DecisionBookError(
            f"The decision book's period is empty: {start} is "
            f"after {end}. Name a first day on or before the last."
        )

    def name_of(account_id: str, fallback: str = "") -> str:
        account = accounts.get(account_id)
        return account.display_name if account else (fallback or account_id)

    entries: list[DecisionBookEntry] = []
    undated: list[DecisionBookUndated] = []

    # -- the brief store: gate decisions ---------------------------------
    for brief in brief_store.list():
        if brief.decision is None:
            continue
        decision = brief.decision
        if not _in_window(decision.decided_at, start, end):
            continue
        renewal = brief.renewal is not None
        summary = (
            f"Gate decision on brief {brief.brief_id}: "
            f"{decision.action} by {decision.actor} — {decision.reason}"
        )
        if renewal:
            summary += (
                f" A renewal decision, decided against baseline "
                f"brief {brief.renewal.baseline.brief_id}."
            )
        entries.append(
            DecisionBookEntry(
                at=_utc(decision.decided_at),
                kind="decision",
                account_id=brief.account_id,
                account_name=name_of(brief.account_id, brief.account_name),
                actor=decision.actor,
                summary=summary,
                brief_id=brief.brief_id,
                action=decision.action,
                renewal=renewal,
                baseline_brief_id=(
                    brief.renewal.baseline.brief_id if renewal else ""
                ),
            )
        )

    # -- the staleness store: flags raised and resolved ------------------
    for record in staleness_store.list():
        name = name_of(record.account_id)
        if _in_window(record.flagged_at, start, end):
            entries.append(
                DecisionBookEntry(
                    at=_utc(record.flagged_at),
                    kind="staleness_raised",
                    account_id=record.account_id,
                    account_name=name,
                    summary=(
                        f"Decision on brief {record.brief_id} "
                        f"({record.decision_action} by "
                        f"{record.decision_actor}) went stale — "
                        f"{_change_summary(record.changes)}; "
                        f"triggered by "
                        f"{len(record.trigger_episode_ids)} episode(s)."
                    ),
                    brief_id=record.brief_id,
                    record_id=record.record_id,
                )
            )
        if record.resolved_by_brief is not None:
            if record.resolved_at is None:
                undated.append(
                    DecisionBookUndated(
                        kind="staleness_resolved",
                        account_id=record.account_id,
                        account_name=name,
                        source_ref=record.record_id,
                        note=(
                            "The record names a resolving brief "
                            f"({record.resolved_by_brief}) but carries "
                            "no resolution moment."
                        ),
                    )
                )
            elif _in_window(record.resolved_at, start, end):
                entries.append(
                    DecisionBookEntry(
                        at=_utc(record.resolved_at),
                        kind="staleness_resolved",
                        account_id=record.account_id,
                        account_name=name,
                        actor=record.resolved_by or "",
                        summary=(
                            f"Staleness on brief {record.brief_id} "
                            f"resolved by the fresh decision on brief "
                            f"{record.resolved_by_brief}"
                            + (
                                f" ({record.resolved_by})"
                                if record.resolved_by
                                else ""
                            )
                            + " — the only resolution path there is."
                        ),
                        brief_id=record.brief_id,
                        record_id=record.record_id,
                    )
                )

    # -- the follow-through store: asks and chases ------------------------
    for record in follow_store.list():
        name = name_of(record.account_id)
        labels = "; ".join(item.label for item in record.items)
        if record.origin == "renewal_chase":
            if _in_window(record.created_at, start, end):
                entries.append(
                    DecisionBookEntry(
                        at=_utc(record.created_at),
                        kind="chase_sent",
                        account_id=record.account_id,
                        account_name=name,
                        actor=record.decision_actor,
                        summary=(
                            f"Renewal chase sent by "
                            f"{record.decision_actor}, riding on brief "
                            f"{record.brief_id}: {labels}."
                        ),
                        brief_id=record.brief_id,
                        record_id=record.record_id,
                    )
                )
        elif _in_window(record.created_at, start, end):
            entries.append(
                DecisionBookEntry(
                    at=_utc(record.created_at),
                    kind="ask_opened",
                    account_id=record.account_id,
                    account_name=name,
                    actor=record.decision_actor,
                    summary=(
                        f"Request-info ask opened on brief "
                        f"{record.brief_id} — {len(record.items)} "
                        f"item(s): {labels}."
                    ),
                    brief_id=record.brief_id,
                    record_id=record.record_id,
                )
            )
        for item in record.items:
            if item.status != "received":
                continue
            if item.received_at is None:
                undated.append(
                    DecisionBookUndated(
                        kind="ask_received",
                        account_id=record.account_id,
                        account_name=name,
                        source_ref=record.record_id,
                        note=(
                            f"Item “{item.label}” stands received but "
                            "carries no receipt moment."
                        ),
                    )
                )
            elif _in_window(item.received_at, start, end):
                entries.append(
                    DecisionBookEntry(
                        at=_utc(item.received_at),
                        kind="ask_received",
                        account_id=record.account_id,
                        account_name=name,
                        summary=(
                            f"Ask {record.record_id}: item received — "
                            f"{item.label} (satisfied by episode "
                            f"{item.received_episode_id})."
                        ),
                        brief_id=record.brief_id,
                        record_id=record.record_id,
                    )
                )
        if record.status == "complete" and record.completed_at is not None:
            if _in_window(record.completed_at, start, end):
                entries.append(
                    DecisionBookEntry(
                        at=_utc(record.completed_at),
                        kind="ask_completed",
                        account_id=record.account_id,
                        account_name=name,
                        summary=(
                            f"Ask {record.record_id} complete — all "
                            f"{len(record.items)} item(s) received."
                        ),
                        brief_id=record.brief_id,
                        record_id=record.record_id,
                    )
                )

    # -- the review gate: candidates confirmed and rejected ---------------
    for account_id in sorted(accounts):
        name = name_of(account_id)
        for candidate in port.get_candidates(account_id):
            if candidate.state == "pending":
                continue  # not yet an act — excluded by state
            kind = (
                "review_confirmed"
                if candidate.state == "confirmed"
                else "review_rejected"
            )
            what = (
                f"{predicate_label(candidate.predicate)}: "
                f"{candidate.value}"
                + (f" {candidate.unit}" if candidate.unit else "")
            )
            if candidate.reviewed_at is None:
                undated.append(
                    DecisionBookUndated(
                        kind=kind,
                        account_id=account_id,
                        account_name=name,
                        source_ref=candidate.candidate_id,
                        note=(
                            f"The candidate stands {candidate.state} "
                            f"(by {candidate.review_actor or 'an unnamed actor'}) "
                            "but carries no review moment."
                        ),
                    )
                )
            elif _in_window(candidate.reviewed_at, start, end):
                if candidate.state == "confirmed":
                    summary = (
                        f"Candidate {candidate.candidate_id} "
                        f"confirmed by {candidate.review_actor} — "
                        f"{what}, from “{candidate.doc_title}”."
                    )
                else:
                    summary = (
                        f"Candidate {candidate.candidate_id} "
                        f"rejected by {candidate.review_actor} — "
                        f"{what}, from “{candidate.doc_title}”. "
                        f"Reason: {candidate.review_reason}"
                    )
                entries.append(
                    DecisionBookEntry(
                        at=_utc(candidate.reviewed_at),
                        kind=kind,
                        account_id=account_id,
                        account_name=name,
                        actor=candidate.review_actor,
                        summary=summary,
                        candidate_id=candidate.candidate_id,
                    )
                )

    entries.sort(
        key=lambda e: (
            e.at,
            _KIND_RANK[e.kind],
            e.account_id,
            _source_ref(e),
            e.summary,
        )
    )
    undated.sort(key=lambda u: (u.kind, u.account_id, u.source_ref))

    counts = {kind: 0 for kind in KINDS}
    for entry in entries:
        counts[entry.kind] += 1

    book = DecisionBook(
        start=start,
        end=end,
        window_rule=WINDOW_RULE,
        sources=list(SOURCES),
        counts=counts,
        total=len(entries),
        entries=entries,
        undated=undated,
    )
    canonical = json.dumps(
        book.model_dump(mode="json", exclude={"digest"}),
        sort_keys=True,
        separators=(",", ":"),
    )
    book.digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return book


# ---------------------------------------------------------------------------
# Text rendering (the CLI's shape; the broker book precedent)
# ---------------------------------------------------------------------------

_KIND_LABEL = {
    "decision": "DECISION",
    "review_confirmed": "REVIEW — CONFIRMED",
    "review_rejected": "REVIEW — REJECTED",
    "staleness_raised": "STALENESS — RAISED",
    "staleness_resolved": "STALENESS — RESOLVED",
    "ask_opened": "ASK — OPENED",
    "chase_sent": "CHASE — SENT",
    "ask_received": "ASK — ITEM RECEIVED",
    "ask_completed": "ASK — COMPLETED",
}


def render_decision_book_text(book: DecisionBook) -> str:
    """The book as text — the same content the API serves as
    JSON, laid out for a reviewer to read or a file to keep."""
    lines = [
        f"DECISION BOOK — {book.start.isoformat()} to "
        f"{book.end.isoformat()}",
        f"{book.total} governed act(s) in the period.",
        "",
        "Coverage: " + "; ".join(book.sources) + ".",
        f"Window rule: {book.window_rule}",
        "Counts: "
        + " · ".join(
            f"{_KIND_LABEL[kind].lower()} {count}"
            for kind, count in book.counts.items()
        )
        + ".",
        f"Digest (SHA-256 over the book's content): {book.digest}",
        "",
    ]
    if not book.entries:
        lines.append(
            "No governed act was recorded in this period. The book "
            "is empty, and it says so — an empty window is a fact, "
            "not an error."
        )
    for entry in book.entries:
        lines.append(
            f"{entry.at.strftime('%Y-%m-%d %H:%M UTC')}  "
            f"{_KIND_LABEL[entry.kind]}  {entry.account_id}  "
            f"{entry.account_name}"
        )
        lines.append(f"  {entry.summary}")
    if book.undated:
        lines.append("")
        lines.append(
            f"UNDATED — {len(book.undated)} record(s) claiming a "
            "completed act but carrying no moment. They are listed, "
            "never dated by invention:"
        )
        for item in book.undated:
            lines.append(
                f"  {_KIND_LABEL.get(item.kind, item.kind)}  "
                f"{item.account_id}  {item.account_name}  "
                f"{item.source_ref} — {item.note}"
            )
    return "\n".join(lines).rstrip() + "\n"
