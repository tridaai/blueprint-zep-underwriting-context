"""The morning portfolio brief: the whole book, ranked, in one view.

Every earlier round deepened the per-account view — the brief,
the staleness watch, the follow-through asks, the expiry watch,
the review gate. The desk queue orders the work, but it answers
"what do I open next", not "how does the book stand". A desk
works forty accounts, and at the start of the day someone asks
the portfolio question: where is the exposure, where is the
broker silent, what has never been looked at. This module is
that projection:

- **Signals, counted in code** (:func:`account_signals`). Six
  counts per account, each read from the machinery that already
  owns it — never a second implementation of any of them:
  open staleness records (:func:`staleness.open_records`), open
  request-info asks (:func:`followthrough.open_records`), the
  expiry watch's LAPSED and EXPIRING rows
  (:func:`timeline.expiry_watch_for_account` — the temporal
  rule's own currency verdicts, with the watch's successor
  discipline intact), correspondence candidates still pending
  at the review gate (the port's candidate read), and whether
  the account has ever had a brief decided
  (:func:`staleness.latest_decided_brief`).
- **"No decided brief" is its own signal.** An account nobody
  has ever decided on is not a clean account; it is unexamined.
  It counts — once, as a flag, not per brief — and it is what
  separates "nothing outstanding" from "never opened": an
  account with a decision and no other signal scores zero and
  is the only kind of row that does.
- **Ranking, published** (:data:`PORTFOLIO_WEIGHTS`). The score
  is the weighted sum of the counts, computed in code, and the
  weights are authored data printed on the brief itself — the
  desk can read why an account ranks first and argue with the
  numbers in the open. The order of the weights follows the
  desk queue's own precedence: a decision resting on
  superseded facts is the first work of the day; a lapsed fact
  is exposure already open; an open ask is the broker owing
  the desk an answer; an expiring fact is a clock running;
  pending candidates are the desk's own review backlog; a
  never-decided account is a standing nudge. Ties break by
  account name, then id, so the order is total and stable.
  Accounts with no signals at all are still listed — a clean
  book is a fact worth printing — ranked last by name.
- **A lens, not a re-ordering.** The desk queue (round 3)
  keeps its own order and its own meaning; this brief does
  not feed it, and building the brief changes no per-account
  surface. Both properties are pinned by test: the queue,
  the watch, and every store read byte-identical before and
  after the brief is built.
- **Thin facts ride along, unweighted** (round 8). Each row
  also counts the account's current facts grading THIN on
  support (:mod:`support`) as of the brief's date — the
  soft spots under the account, next to its signals. The
  count is displayed and *only* displayed: it has no entry
  in :data:`PORTFOLIO_WEIGHTS`, enters no score, and cannot
  move a single row. The weights above are published data;
  if the desk ever wants thin support to rank, that is a
  separate, deliberate change to the weights — argued in
  the open, not smuggled in as a side effect of a count.

Like the timeline and the watch, the brief is a **pure
projection**: it reads the port and the record stores and
writes nothing. The as-of date and the expiry horizon are
the caller's — the surfaces default them at the boundary —
so the brief is byte-deterministic given the store, and the
eval pack pins it.
"""

from __future__ import annotations

from datetime import date

from . import followthrough as follow_mod
from . import staleness as staleness_mod
from . import support as support_mod
from . import timeline as timeline_mod
from .ontology import Account
from .ports import ContextPort
from .schemas import PortfolioBrief, PortfolioRow, PortfolioSignals

#: The ranking's weights — authored data, printed on every
#: brief. Score = sum(weight x count) over the six signals.
#: The desk can disagree with these numbers; it cannot be
#: surprised by them.
PORTFOLIO_WEIGHTS: dict[str, int] = {
    "stale_decisions": 100,
    "lapsed_facts": 40,
    "open_asks": 30,
    "expiring_facts": 20,
    "pending_candidates": 10,
    "no_decided_brief": 5,
}

#: The signals in the brief's display order (the weights'
#: order): decision integrity first, then exposure, then
#: the broker's silence, then the calendar, then the desk's
#: own backlog, then the unexamined.
SIGNAL_ORDER: tuple[str, ...] = tuple(PORTFOLIO_WEIGHTS)

#: How each signal is named when an underwriter reads it.
SIGNAL_LABELS: dict[str, str] = {
    "stale_decisions": "stale decision(s)",
    "lapsed_facts": "lapsed fact(s)",
    "open_asks": "open ask(s)",
    "expiring_facts": "expiring fact(s)",
    "pending_candidates": "candidate(s) awaiting review",
    "no_decided_brief": "no decided brief",
}


def score_signals(signals: PortfolioSignals) -> int:
    """The account's priority score: the published weights
    over its signal counts. The only arithmetic in the round,
    and it is deliberately this small."""
    return sum(
        PORTFOLIO_WEIGHTS[name] * getattr(signals, name)
        for name in SIGNAL_ORDER
    )


def account_signals(
    *,
    port: ContextPort,
    account: Account,
    brief_store,
    staleness_store,
    follow_store,
    as_of: date,
    horizon_days: int,
) -> PortfolioSignals:
    """One account's six signal counts, each from the machinery
    that owns it. Nothing here re-derives a verdict: staleness
    is the watch's open records, currency is the temporal
    rule's (via the expiry watch), an open ask is a record the
    follow-through store has not closed, a pending candidate
    is the review gate's own state."""
    account_id = account.account_id
    watch = timeline_mod.expiry_watch_for_account(
        port=port,
        account=account,
        as_of=as_of,
        horizon_days=horizon_days,
    )
    return PortfolioSignals(
        stale_decisions=len(
            staleness_mod.open_records(staleness_store, account_id)
        ),
        lapsed_facts=sum(1 for r in watch if r.state == "lapsed"),
        open_asks=len(follow_mod.open_records(follow_store, account_id)),
        expiring_facts=sum(1 for r in watch if r.state == "expiring"),
        pending_candidates=len(
            port.get_candidates(account_id, state="pending")
        ),
        no_decided_brief=(
            0
            if staleness_mod.latest_decided_brief(brief_store, account_id)
            is not None
            else 1
        ),
    )


def build_portfolio_brief(
    *,
    port: ContextPort,
    accounts: dict[str, Account],
    brief_store,
    staleness_store,
    follow_store,
    as_of: date,
    horizon_days: int,
) -> PortfolioBrief:
    """Every account, ranked: score descending, ties by name
    then id — so an account carrying no signal at all (score
    zero: decided, nothing outstanding) closes the list in
    name order. Pure reads; the stores are unchanged by
    building it."""
    rows = []
    for account_id in sorted(accounts):
        account = accounts[account_id]
        signals = account_signals(
            port=port,
            account=account,
            brief_store=brief_store,
            staleness_store=staleness_store,
            follow_store=follow_store,
            as_of=as_of,
            horizon_days=horizon_days,
        )
        rows.append(
            PortfolioRow(
                account_id=account_id,
                account_name=account.display_name,
                signals=signals,
                score=score_signals(signals),
                # Display only — never scored (see the module
                # docstring): the count of the account's
                # current facts grading THIN at the brief's
                # date, from the support module's own grading.
                thin_facts=support_mod.thin_count(
                    port.get_facts(account_id), as_of
                ),
            )
        )
    rows.sort(key=lambda r: (-r.score, r.account_name, r.account_id))
    return PortfolioBrief(
        as_of=as_of,
        horizon_days=horizon_days,
        weights=dict(PORTFOLIO_WEIGHTS),
        rows=rows,
    )


# ---------------------------------------------------------------------------
# Text rendering (the CLI's and demo's shape; the watch precedent)
# ---------------------------------------------------------------------------


def render_portfolio_text(brief: PortfolioBrief) -> str:
    weights = ", ".join(
        f"{name} {brief.weights[name]}" for name in SIGNAL_ORDER
    )
    lines = [
        f"MORNING PORTFOLIO BRIEF — {len(brief.rows)} account(s) "
        f"as of {brief.as_of} (expiry horizon {brief.horizon_days} days)",
        f"Ranking weights: {weights}. Score is the weighted sum "
        "of the signal counts; ties break by account name. "
        "Thin-fact counts (support grading) are shown for "
        "information only — unweighted, never scored.",
        "",
    ]
    if not brief.rows:
        lines.append("No accounts on the book.")
        return "\n".join(lines) + "\n"
    for rank, row in enumerate(brief.rows, start=1):
        counts = [
            f"{getattr(row.signals, name)} {SIGNAL_LABELS[name]}"
            for name in SIGNAL_ORDER
            if getattr(row.signals, name)
        ]
        detail = "; ".join(counts) if counts else "nothing outstanding"
        if row.thin_facts:
            detail += f"; {row.thin_facts} thin fact(s)"
        lines.append(
            f"{rank:>3}. {row.account_id}  {row.account_name} — "
            f"score {row.score}: {detail}"
        )
    return "\n".join(lines) + "\n"
