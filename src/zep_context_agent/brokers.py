"""The broker book: what each broker owes the desk, per broker.

Every surface before this one is per-account — the brief, the
queue, the watches, the portfolio brief. But accounts arrive
through brokers, and the desk's follow-through asks are, in
substance, asks *of* brokers: the draft on every record is
addressed "to the broker of record" for the account. So the
question a desk lead actually asks on a Monday — "what are we
waiting on from Alder & Voss, across all their accounts, and
how does their book stand?" — could only be answered by
reading forty briefs. This module is that answer, composed:

- **Broker identity is the account's own paper.** The
  ontology's ``placed_by`` edge (insured_account -> broker)
  is asserted on the account's submission. The book reads it
  from the account records — the same place the portfolio
  brief reads account names — so the whole book groups even
  before an account is ingested; the identical assertion
  lands in the graph at ingestion, and nothing here invents
  a second source for it. If an account's paper ever carries
  the edge on several documents, the most recent document's
  assertion is the broker *of record* — that is what "of
  record" means. An account whose paper names no broker
  groups under an explicit **unattributed** row: counted like
  any other row, never silently dropped.
- **Every figure is another surface's own computation,
  re-aggregated.** The counts are :func:`portfolio.account_signals`
  summed over the broker's accounts — the staleness watch's
  open records, the expiry watch's LAPSED and EXPIRING rows,
  the review gate's pending candidates — plus the decided
  mix off :func:`staleness.latest_decided_brief` and the
  chase projection's open count, read exactly as the
  portfolio brief reads them. The owed listing is the
  follow-through store's open records with their outstanding
  items. No per-account verdict is re-derived anywhere in
  this module; if a count here ever disagreed with the
  per-account surface it sums, the surface would be right
  and this module would be the bug.
- **A ledger, not a ranking.** No score is computed and no
  weights exist — the portfolio brief is the book's ranking,
  and it keeps that job. The rows carry one order, because
  the book is a chasing tool: most open asks owed first,
  then most stale decisions, then broker name, then broker
  id, with the unattributed row always last (there is no
  broker to chase there). The rule is printed on the book's
  face and pinned by test.
- **Nothing crosses accounts.** A broker row aggregates
  *counts* over its accounts; the only text it carries is an
  open ask's own outstanding items, listed under that ask's
  own account. Facts, values, and citations stay on their
  account's brief, where the leakage discipline already
  governs them — the book never quotes one account's file
  into another's row.
- **Read-only.** There are no broker-level decisions and no
  broker-level acts: the gate decides briefs, the chase
  machinery sends chases, and this book changes neither.
  Like the portfolio brief it is a **pure projection** —
  it reads the port and the record stores and writes
  nothing, so the queue, the watches, and every decided
  brief are byte-identical before and after it is built.
"""

from __future__ import annotations

from datetime import date

from . import chases as chases_mod
from . import followthrough as follow_mod
from . import portfolio as portfolio_mod
from . import staleness as staleness_mod
from .ontology import Account
from .ports import ContextPort
from .schemas import (
    BrokerAccountAsk,
    BrokerAskItem,
    BrokerBook,
    BrokerRow,
)

#: How the row for accounts whose paper names no placing
#: broker is headed. They are the desk's accounts all the
#: same; the row says plainly that no broker is of record.
UNATTRIBUTED_NAME = "Unattributed"

#: The book's order, printed on its face. An order is
#: published because the book is a chasing tool — the desk
#: works the longest-owed broker first — and a published
#: order can be argued with; an implicit one cannot. It is
#: an order, not a score: nothing is weighted or summed.
ORDER_RULE = (
    "Rows are ordered by open asks owed, then stale "
    "decisions, then broker name, then broker id; the "
    "unattributed row closes the book. No score is "
    "computed — this is a ledger, not a ranking."
)


def broker_of_record(account: Account) -> tuple[str | None, str]:
    """The account's placing broker, from its own paper:
    ``(broker_id, broker_name)`` off the ``placed_by`` edge
    its documents assert, or ``(None, UNATTRIBUTED_NAME)``
    when no document names one. Documents are read in
    arrival order, so where several assert the edge the
    most recent document's broker is the broker of record.
    Only edges the account itself is the source of count —
    a submission's ``submitted_by`` edge names who sent a
    package, not who places the account."""
    found: tuple[str, str] | None = None
    for doc in sorted(
        account.documents, key=lambda d: (d.received_at, d.doc_id)
    ):
        for edge in doc.edges:
            if (
                edge.edge_type == "placed_by"
                and edge.source_ref == account.account_id
            ):
                found = (edge.target_ref, edge.label or edge.target_ref)
    if found is None:
        return None, UNATTRIBUTED_NAME
    return found


def _account_ask(record, account: Account) -> BrokerAccountAsk:
    """One open follow-through record as the book lists it:
    the record's identity and state, and the items still
    outstanding on it — received items are answered business
    and are not owed, so they are not listed."""
    return BrokerAccountAsk(
        account_id=account.account_id,
        account_name=account.display_name,
        record_id=record.record_id,
        brief_id=record.brief_id,
        origin=record.origin,
        status=record.status,
        items=[
            BrokerAskItem(
                kind=item.kind,
                predicate=item.predicate,
                label=item.label,
                doc_id=item.doc_id,
                doc_title=item.doc_title,
                expires_on=item.expires_on,
            )
            for item in record.items
            if item.status == "open"
        ],
    )


def build_broker_book(
    *,
    port: ContextPort,
    accounts: dict[str, Account],
    brief_store,
    staleness_store,
    follow_store,
    as_of: date,
    horizon_days: int,
) -> BrokerBook:
    """Every broker's row over the whole book, in the book's
    published order. Pure reads; the stores are unchanged by
    building it."""
    grouped: dict[str | None, BrokerRow] = {}
    for account_id in sorted(accounts):
        account = accounts[account_id]
        broker_id, broker_name = broker_of_record(account)
        row = grouped.setdefault(
            broker_id,
            BrokerRow(broker_id=broker_id, broker_name=broker_name),
        )
        signals = portfolio_mod.account_signals(
            port=port,
            account=account,
            brief_store=brief_store,
            staleness_store=staleness_store,
            follow_store=follow_store,
            as_of=as_of,
            horizon_days=horizon_days,
        )
        row.accounts += 1
        row.account_ids.append(account_id)
        decided = staleness_mod.latest_decided_brief(
            brief_store, account_id
        )
        if decided is None:
            row.undecided += 1
        elif decided.decision.action == "proceed":
            row.decided_proceed += 1
        elif decided.decision.action == "request_info":
            row.decided_request_info += 1
        else:
            row.decided_escalate += 1
        row.stale_decisions += signals.stale_decisions
        row.expiring_facts += signals.expiring_facts
        row.lapsed_facts += signals.lapsed_facts
        row.pending_candidates += signals.pending_candidates
        row.open_chases += chases_mod.open_chase_count(
            chases_mod.chases_for_account(
                port=port,
                account=account,
                brief_store=brief_store,
                follow_store=follow_store,
                as_of=as_of,
                horizon_days=horizon_days,
            )
        )
        for record in follow_mod.open_records(follow_store, account_id):
            listing = _account_ask(record, account)
            row.open_asks += 1
            row.open_items += len(listing.items)
            row.asks.append(listing)
    rows = sorted(
        (r for r in grouped.values() if r.broker_id is not None),
        key=lambda r: (
            -r.open_asks,
            -r.stale_decisions,
            r.broker_name,
            r.broker_id or "",
        ),
    )
    if None in grouped:
        rows.append(grouped[None])
    return BrokerBook(
        as_of=as_of,
        horizon_days=horizon_days,
        order_rule=ORDER_RULE,
        rows=rows,
    )


# ---------------------------------------------------------------------------
# Text rendering (the CLI's and demo's shape; the portfolio precedent)
# ---------------------------------------------------------------------------


def render_broker_book_text(book: BrokerBook) -> str:
    total_accounts = sum(r.accounts for r in book.rows)
    lines = [
        f"BROKER BOOK — {len(book.rows)} broker row(s), "
        f"{total_accounts} account(s) as of {book.as_of} "
        f"(expiry horizon {book.horizon_days} days)",
        book.order_rule,
        "Every count is the per-account machinery's own "
        "figure summed over the broker's accounts — the "
        "staleness watch, the expiry watch, the follow-through "
        "store, the review gate, the chase projection — "
        "counted, never re-derived.",
        "",
    ]
    if not book.rows:
        lines.append("No accounts on the book.")
        return "\n".join(lines) + "\n"
    for row in book.rows:
        decided = (
            f"{row.decided_proceed} proceed · "
            f"{row.decided_request_info} request-info · "
            f"{row.decided_escalate} escalate · "
            f"{row.undecided} undecided"
        )
        lines.append(
            f"{row.broker_name} — {row.accounts} account(s): "
            f"decided {decided}"
        )
        owed = []
        if row.open_asks:
            owed.append(
                f"{row.open_asks} open ask(s), "
                f"{row.open_items} item(s) outstanding"
            )
        if row.stale_decisions:
            owed.append(f"{row.stale_decisions} stale decision(s)")
        if row.expiring_facts:
            owed.append(f"{row.expiring_facts} expiring fact(s)")
        if row.lapsed_facts:
            owed.append(f"{row.lapsed_facts} lapsed fact(s)")
        if row.pending_candidates:
            owed.append(
                f"{row.pending_candidates} candidate(s) awaiting review"
            )
        if row.open_chases:
            owed.append(f"{row.open_chases} open chase(s)")
        lines.append(
            "  " + ("; ".join(owed) if owed else "nothing outstanding")
        )
        for ask in row.asks:
            items = "; ".join(
                item.label
                + (
                    f" (expires {item.expires_on})"
                    if item.expires_on
                    else ""
                )
                for item in ask.items
            )
            lines.append(
                f"  {ask.account_id} {ask.account_name} — "
                f"ask {ask.record_id} ({ask.origin}, {ask.status}): "
                f"{items if items else 'no items outstanding'}"
            )
    return "\n".join(lines) + "\n"
