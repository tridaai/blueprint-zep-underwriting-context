"""Time, visible: the account timeline and the expiry watch.

The context graph is temporal — every fact lives inside a
validity window — but until now the only surface that showed
time was the brief, which answers "what is true as of this
date" and nothing else. This module is the two read models
that show the account's *time shape*:

- **The timeline** (:func:`build_account_timeline`) — one
  chronology per account, assembled in code from stored
  records: every fact placed on its validity window with its
  citation, and the account's episodes, gate decisions,
  staleness flags (raised and resolved), and follow-through
  asks (opened, items received, completed) interleaved as
  dated events in the same ordering. Status on each fact is
  the temporal rule's own verdict at the timeline's as-of
  date — :func:`temporal.resolve`, the same function assembly
  uses, never a second reading of the windows.
- **The expiry watch** (:func:`expiry_watch_for_account`,
  :func:`build_expiry_watch`) — the desk-level warning list:
  facts the desk relies on whose windows are closing.
  Computed over the facts :func:`temporal.resolve` calls
  current, plus the ones it reports expired. A current fact
  whose ``valid_to`` falls within the horizon (config:
  ``EXPIRY_HORIZON_DAYS``, default 45) is **EXPIRING**; the
  last fact in force for its key whose window has closed is
  **LAPSED**, with negative days remaining.

**The successor rule is a window rule** — the same relation
the temporal rule itself uses. A fact has a *covering
successor* when another fact for the same (predicate,
entity_ref) has a window that covers this fact's ``valid_to``
(the renewal whose term starts the day the old term ends —
the seeded dataset's own convention). A fact with a covering
successor is neither expiring nor lapsed, whatever the
calendar says: the renewal is on file, and a watch that cries
over renewed policies teaches the desk to ignore it. A later
fact that starts *after* a gap does not cover — the row then
says so (``successor_exists`` with its start date), because
the gap itself was real exposure. Facts a successor
*replaced* mid-window are history, not lapses: resolve names
their successor, and the watch believes it.

Both models are **pure projections**: they read the port and
the record stores and write nothing — computing a timeline
or a watch mutates no brief, flag, ask, or fact (pinned by
test). Account scoping is by construction: every read names
exactly one account, so another account's events cannot
enter the chronology — near-identical names included.

Day math runs on an explicit as-of date the caller passes —
the surfaces default it to today at the service boundary,
and the eval pack pins it, so the watch is hermetic under
test. The watch observes; it never changes what any brief
considers current.
"""

from __future__ import annotations

from datetime import date

from .ontology import Account, Fact, predicate_label
from .ports import ContextPort
from .schemas import (
    AccountTimeline,
    DeskExpiry,
    ExpiryEntry,
    TimelineEvent,
)
from .temporal import resolve

# Same-day ordering between event kinds: the document lands,
# its facts take effect, the human decides, the watches react,
# the asks proceed. Within a kind, label then reference keeps
# the order deterministic.
_KIND_RANK = {
    "episode": 0,
    "fact": 1,
    "decision": 2,
    "staleness_flag": 3,
    "staleness_resolved": 4,
    "follow_opened": 5,
    "follow_received": 6,
    "follow_completed": 7,
}


def _key(fact: Fact) -> tuple[str, str]:
    return (fact.predicate, fact.entity_ref)


def _covering_successor(fact: Fact, facts: list[Fact]) -> Fact | None:
    """The fact whose window covers ``fact``'s end, if any:
    same key, a different fact, in force at ``fact.valid_to``
    (its window opens no later than that day and closes after
    it, or never). This is the renewal-on-file relation — the
    temporal rule's own notion of continuity."""
    if fact.valid_to is None:
        return None
    for other in facts:
        if other.fact_id == fact.fact_id or _key(other) != _key(fact):
            continue
        if other.valid_from <= fact.valid_to and (
            other.valid_to is None or fact.valid_to < other.valid_to
        ):
            return other
    return None


def covering_successor(fact: Fact, facts: list[Fact]) -> Fact | None:
    """The public reading of the successor rule above: the
    fact whose window covers ``fact``'s end, if one is on file.
    The follow-through recognition (a renewal item is answered
    when a covering successor lands) and the renewal chase read
    this same relation — the rule lives here, in one place,
    and no other module re-derives it."""
    return _covering_successor(fact, facts)


def _later_fact(fact: Fact, facts: list[Fact]) -> Fact | None:
    """The earliest later fact for the same key, if any — a
    successor on file that does *not* cover the end (it starts
    after a gap). Named on the row so the desk can see the
    lapse was real but the story continued."""
    later = [
        other
        for other in facts
        if other.fact_id != fact.fact_id
        and _key(other) == _key(fact)
        and other.valid_from > fact.valid_from
    ]
    return min(later, key=lambda f: (f.valid_from, f.recorded_at)) if later else None


# ---------------------------------------------------------------------------
# The expiry watch
# ---------------------------------------------------------------------------


def expiry_watch_for_account(
    *,
    port: ContextPort,
    account: Account,
    as_of: date,
    horizon_days: int,
) -> list[ExpiryEntry]:
    """The watch rows for one account at ``as_of``: EXPIRING
    facts (current, window closing within ``horizon_days``,
    no covering successor) and LAPSED facts (the last fact in
    force for its key, window closed, no covering successor),
    soonest / most-lapsed first. Currency is
    :func:`temporal.resolve`'s verdict — the watch adds the
    calendar, never a second opinion about what is current."""
    facts = port.get_facts(account.account_id)
    doc_titles = {d.doc_id: d.title for d in account.documents}
    current, superseded = resolve(facts, as_of)
    rows: list[ExpiryEntry] = []

    def row(fact: Fact, state: str) -> ExpiryEntry:
        later = _later_fact(fact, facts)
        return ExpiryEntry(
            account_id=account.account_id,
            account_name=account.display_name,
            predicate=fact.predicate,
            entity_ref=fact.entity_ref,
            value=fact.value,
            unit=fact.unit,
            valid_from=fact.valid_from,
            valid_to=fact.valid_to,
            state=state,
            days_remaining=(fact.valid_to - as_of).days,
            successor_exists=later is not None,
            successor_valid_from=later.valid_from if later else None,
            episode_id=fact.episode_id,
            doc_id=fact.doc_id,
            doc_title=doc_titles.get(fact.doc_id, ""),
        )

    for fact in current:
        if fact.valid_to is None:
            continue
        days = (fact.valid_to - as_of).days
        if days > horizon_days:
            continue
        if _covering_successor(fact, facts) is not None:
            continue  # renewed — the successor's window covers the end
        rows.append(row(fact, "expiring"))

    # Lapsed: per key, among the facts resolve reports expired
    # (superseded with no successor), only the last one in
    # force — its window's end is where coverage stopped. The
    # earlier ones handed off inside the key and are history.
    lapsed_candidates: dict[tuple[str, str], Fact] = {}
    for old, successor in superseded:
        if successor is not None or old.valid_to is None:
            continue
        if old.valid_to > as_of:
            continue
        if _covering_successor(old, facts) is not None:
            continue
        key = _key(old)
        standing = lapsed_candidates.get(key)
        if standing is None or (old.valid_to, old.valid_from) > (
            standing.valid_to,
            standing.valid_from,
        ):
            lapsed_candidates[key] = old
    for fact in lapsed_candidates.values():
        rows.append(row(fact, "lapsed"))

    rows.sort(
        key=lambda r: (
            r.days_remaining,
            r.account_id,
            r.predicate,
            r.entity_ref,
            r.valid_to,
        )
    )
    return rows


def build_expiry_watch(
    *,
    port: ContextPort,
    accounts: dict[str, Account],
    as_of: date,
    horizon_days: int,
) -> list[ExpiryEntry]:
    """The desk-level watch: every account's rows, most
    lapsed first, then soonest to expire. Pure reads."""
    rows: list[ExpiryEntry] = []
    for account_id in sorted(accounts):
        rows.extend(
            expiry_watch_for_account(
                port=port,
                account=accounts[account_id],
                as_of=as_of,
                horizon_days=horizon_days,
            )
        )
    rows.sort(
        key=lambda r: (
            r.days_remaining,
            r.account_id,
            r.predicate,
            r.entity_ref,
            r.valid_to,
        )
    )
    return rows


def desk_expiry(rows: list[ExpiryEntry]) -> list[DeskExpiry]:
    """An account's watch rows in desk-queue shape."""
    return [
        DeskExpiry(
            predicate=r.predicate,
            entity_ref=r.entity_ref,
            value=r.value,
            unit=r.unit,
            valid_to=r.valid_to,
            state=r.state,
            days_remaining=r.days_remaining,
            successor_exists=r.successor_exists,
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# The account timeline
# ---------------------------------------------------------------------------


def build_account_timeline(
    *,
    port: ContextPort,
    account: Account,
    brief_store,
    staleness_store,
    follow_store,
    as_of: date,
) -> AccountTimeline:
    """One account's chronology at ``as_of``. Facts are placed
    on their windows with the temporal rule's status; episodes,
    decisions, staleness flags, and follow-through movement
    interleave as point events. A pure projection — nothing
    here writes."""
    account_id = account.account_id
    facts = port.get_facts(account_id)
    episodes = port.get_episodes(account_id)
    doc_titles = {d.doc_id: d.title for d in account.documents}
    episode_titles = {e.episode_id: e.title for e in episodes}

    current, superseded = resolve(facts, as_of)
    current_ids = {f.fact_id for f in current}
    successor_by_old = {old.fact_id: succ for old, succ in superseded}

    events: list[TimelineEvent] = []

    for episode in episodes:
        label = f"Document received: {episode.title}"
        detail = f"kind: {episode.kind}"
        if episode.quarantined:
            detail += f" — QUARANTINED: {episode.quarantine_reason}"
        events.append(
            TimelineEvent(
                kind="episode",
                date=episode.received_at,
                label=label,
                detail=detail,
                episode_id=episode.episode_id,
                doc_id=episode.doc_id,
                doc_title=episode.title,
            )
        )

    for fact in facts:
        if fact.fact_id in current_ids:
            status = "current"
        elif fact.fact_id in successor_by_old:
            status = (
                "superseded"
                if successor_by_old[fact.fact_id] is not None
                else "expired"
            )
        else:
            status = "scheduled"  # window has not opened at as_of
        window = f"{fact.valid_from} → {fact.valid_to or 'open'}"
        events.append(
            TimelineEvent(
                kind="fact",
                date=fact.valid_from,
                valid_to=fact.valid_to,
                label=(
                    f"{predicate_label(fact.predicate)}: "
                    f"{fact.value}{(' ' + fact.unit) if fact.unit else ''}"
                    f" — {status}"
                ),
                detail=f"validity window {window}",
                predicate=fact.predicate,
                value=fact.value,
                unit=fact.unit,
                entity_ref=fact.entity_ref,
                fact_status=status,
                episode_id=fact.episode_id,
                doc_id=fact.doc_id,
                doc_title=doc_titles.get(
                    fact.doc_id, episode_titles.get(fact.episode_id, "")
                ),
            )
        )

    for brief in brief_store.list(account_id):
        if brief.decision is None:
            continue
        events.append(
            TimelineEvent(
                kind="decision",
                date=brief.decision.decided_at.date(),
                label=(
                    f"Decision: {brief.decision.action} by "
                    f"{brief.decision.actor}"
                ),
                detail=brief.decision.reason,
                brief_id=brief.brief_id,
            )
        )

    for record in staleness_store.list(account_id):
        events.append(
            TimelineEvent(
                kind="staleness_flag",
                date=record.flagged_at.date(),
                label=(
                    f"Decision flagged STALE — {record.decision_action} "
                    f"by {record.decision_actor} (decided "
                    f"{record.decided_at.date()})"
                ),
                detail=(
                    "triggered by "
                    + ", ".join(record.trigger_episode_ids)
                ),
                brief_id=record.brief_id,
                record_id=record.record_id,
            )
        )
        if record.resolved_by_brief is not None and record.resolved_at:
            events.append(
                TimelineEvent(
                    kind="staleness_resolved",
                    date=record.resolved_at.date(),
                    label=(
                        f"Staleness resolved — fresh decision on "
                        f"{record.resolved_by_brief} by {record.resolved_by}"
                    ),
                    brief_id=record.resolved_by_brief,
                    record_id=record.record_id,
                )
            )

    for record in follow_store.list(account_id):
        if record.origin == "renewal_chase":
            opened_label = (
                f"Renewal chase sent — {len(record.items)} "
                f"expiring fact(s) asked of the broker"
            )
            opened_detail = (
                f"chase by {record.decision_actor}, attached to "
                f"brief {record.brief_id}"
            )
        else:
            opened_label = (
                f"Request-info ask opened — {len(record.items)} "
                f"item(s) asked of the broker"
            )
            opened_detail = f"ask from brief {record.brief_id}"
        events.append(
            TimelineEvent(
                kind="follow_opened",
                date=record.created_at.date(),
                label=opened_label,
                detail=opened_detail,
                brief_id=record.brief_id,
                record_id=record.record_id,
            )
        )
        for item in record.items:
            if item.status == "received" and item.received_at is not None:
                events.append(
                    TimelineEvent(
                        kind="follow_received",
                        date=item.received_at.date(),
                        label=f"Ask item received: {item.label}",
                        detail=(
                            f"recognised in {item.received_episode_id}"
                        ),
                        episode_id=item.received_episode_id,
                        brief_id=record.brief_id,
                        record_id=record.record_id,
                    )
                )
        if record.completed_at is not None:
            events.append(
                TimelineEvent(
                    kind="follow_completed",
                    date=record.completed_at.date(),
                    label="Ask complete — every item received",
                    brief_id=record.brief_id,
                    record_id=record.record_id,
                )
            )

    events.sort(
        key=lambda e: (e.date, _KIND_RANK[e.kind], e.label, e.episode_id, e.record_id)
    )
    return AccountTimeline(
        account_id=account_id,
        account_name=account.display_name,
        as_of=as_of,
        events=events,
    )


# ---------------------------------------------------------------------------
# Text renderings (the CLI's shape; the follow-through precedent)
# ---------------------------------------------------------------------------


def _fmt_value(value, unit: str) -> str:
    text = f"{value:,}" if isinstance(value, (int, float)) else str(value)
    return f"{text} {unit}" if unit else text


def render_timeline_text(timeline: AccountTimeline) -> str:
    lines = [
        f"Timeline — {timeline.account_name} ({timeline.account_id})"
        f" as of {timeline.as_of}",
        "",
    ]
    for event in timeline.events:
        line = f"{event.date}  {event.kind:<18} {event.label}"
        if event.doc_id:
            line += f"  [{event.doc_title} ({event.doc_id})]"
        lines.append(line)
        if event.detail:
            lines.append(f"{'':>12}  {event.detail}")
    return "\n".join(lines)


def render_expiry_watch_text(rows: list[ExpiryEntry], as_of: date, horizon_days: int) -> str:
    lines = [
        f"Expiry watch as of {as_of} (horizon {horizon_days} days)",
        "",
    ]
    if not rows:
        lines.append("Nothing the desk relies on is running out.")
    for r in rows:
        if r.state == "lapsed":
            when = f"LAPSED {-r.days_remaining}d ago"
        else:
            when = f"expires in {r.days_remaining}d"
        line = (
            f"{r.account_id}  {r.account_name}  {r.predicate}"
            f"{f' [{r.entity_ref}]' if r.entity_ref else ''}: "
            f"{_fmt_value(r.value, r.unit)} ends {r.valid_to} — {when}"
        )
        if r.successor_exists:
            line += f" (a successor is on file from {r.successor_valid_from}, after a gap)"
        line += f"  [{r.doc_title} ({r.doc_id})]"
        lines.append(line)
    return "\n".join(lines)
