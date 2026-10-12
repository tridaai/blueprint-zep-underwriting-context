"""The renewal chase: the expiry watch learns to ask.

The expiry watch (see :mod:`timeline`) names the facts the
desk relies on whose windows are closing — and until now
nothing happened next: the underwriter had to notice the row,
remember the broker, and compose the ask from memory. The
follow-through machinery (see :mod:`followthrough`) knows how
to ask a broker for material and recognise the answer, but it
only ever asked off a gate decision, about predicates the
file *lacked*. This module joins the two: an expiring fact
becomes a chase.

- **A chase is a projection, not a record.** Rows are computed
  in code from the watch's own rows, the follow-through store,
  and the brief store — the same pure-projection discipline as
  the timeline and the portfolio brief. Building the rows
  writes nothing; the only durable artifact a chase ever
  creates is the follow-through ask its send opens, which
  rides the follow-through store's existing table (the
  record's ``origin`` and the renewal items' fields are payload
  data — **no migration**).
- **Dedupe is by (account, predicate), against open asks** —
  the key asks themselves use: a follow-through item names
  its predicate, and an ask *covers* a predicate while it is
  open (not complete) with an item naming it. A predicate an
  open request-info ask covers gets **no chase row**: the
  broker is already being asked about that line of the file,
  and a chase would ask twice. The dedupe suppresses only the
  unsent states — a chase ask already open (ASKED) or already
  answered (SATISFIED) is the desk's own live work and history,
  and stays visible beside the decision ask that came later.
- **The send rides round 4's machinery, unchanged.** Sending
  composes renewal items from the flagged facts
  (:func:`followthrough.compose_renewal_items`) and opens the
  ask on the account's latest brief
  (:func:`followthrough.open_renewal_ask`) — the ask attaches
  to a brief because that is where asks live; an account with
  no brief gets the row in state ``WAITING_ON_BRIEF`` and the
  send is refused by name. There is no brief-less ask store,
  and the chase invents none. Once sent, the chase *is* an
  ordinary open ask: the renewal document's arrival is
  recognised by the follow-through evaluation on ingestion —
  a covering successor flips the item, the fresh fact
  supersedes, the watch row clears — and this projection then
  reads the completed ask as SATISFIED, naming the document
  that satisfied it. No step of that path is chase-specific
  code.
- **States** (see :class:`schemas.RenewalChase`): ``OPEN``
  (expiring, brief on file, sendable), ``ASKED`` (the chase
  ask is open), ``SATISFIED`` (the ask completed and nothing
  under the predicate is flagged), ``LAPSED_UNSENT`` (a fact
  lapsed while the chase sat unsent — named plainly, and
  still sendable: late is not never), ``WAITING_ON_BRIEF``
  (expiring, but no brief exists to attach an ask to). A fact
  that gains a covering successor without any chase simply
  leaves the watch, and its unsent row leaves with it — the
  desk never needed the chase, and the projection does not
  pretend it sent one.
- **Refusals are named** (:class:`errors.ChaseError`): nothing
  expiring under the predicate, already covered by an open
  request-info ask (the dedupe, enforced at send as well as
  at row-building), a chase ask already open, the renewal
  already on file, no brief to attach to, no actor named.
  A refused send opens no record and changes no state.

The surfaces read the same rows: the desk queue annotates
each account with its chases (its order never reads them),
the portfolio brief counts open chases per account beside
its signals — displayed, unweighted, exactly the thin-facts
discipline — and the console and CLI serve the rows and
the send.
"""

from __future__ import annotations

from datetime import date

from . import followthrough as follow_mod
from . import timeline as timeline_mod
from .errors import ChaseError
from .ontology import Account, Fact, predicate_label
from .ports import ContextPort
from .schemas import (
    ChaseFact,
    DeskChase,
    ExpiryEntry,
    FollowThroughRecord,
    RenewalChase,
)

#: The states a chase can be *open* in — work outstanding in
#: some form. SATISFIED is closed business and never counts.
OPEN_STATES: tuple[str, ...] = (
    "OPEN",
    "ASKED",
    "LAPSED_UNSENT",
    "WAITING_ON_BRIEF",
)


# ---------------------------------------------------------------------------
# The dedupe — what already covers a predicate
# ---------------------------------------------------------------------------


def covered_predicates(follow_store, account_id: str) -> dict[str, FollowThroughRecord]:
    """Predicates an open ask already covers, by record: the
    (account, predicate) dedupe key asks themselves use. An
    ask covers a predicate while it is open (not complete)
    and carries an item naming it — a decision-born ask's
    predicate items. Renewal items never *cover* for this
    purpose: a chase ask's own coverage is the chase's state,
    read from the record itself."""
    covered: dict[str, FollowThroughRecord] = {}
    for record in follow_mod.open_records(follow_store, account_id):
        if record.origin != "decision":
            continue
        for item in record.items:
            if item.kind == "predicate" and item.predicate:
                covered.setdefault(item.predicate, record)
    return covered


def _chase_records(follow_store, account_id: str) -> list[FollowThroughRecord]:
    return [
        r
        for r in follow_store.list(account_id)
        if r.origin == "renewal_chase"
    ]


# ---------------------------------------------------------------------------
# Row construction — one predicate at a time
# ---------------------------------------------------------------------------


def _fact_from_watch(row: ExpiryEntry) -> ChaseFact:
    return ChaseFact(
        predicate=row.predicate,
        entity_ref=row.entity_ref,
        value=row.value,
        unit=row.unit,
        valid_from=row.valid_from,
        valid_to=row.valid_to,
        watch_state=row.state,
        days_remaining=row.days_remaining,
        successor_exists=row.successor_exists,
        episode_id=row.episode_id,
        doc_id=row.doc_id,
        doc_title=row.doc_title,
    )


def _fact_from_item(item, fact: Fact | None, episodes_by_id, as_of: date) -> ChaseFact:
    """A chase fact rebuilt from the ask item that chased it —
    for rows whose ask is the source (ASKED rows past a partial
    answer, SATISFIED rows). The window and its clock come from
    the chased fact itself; the satisfying document is named
    from the episode the recognition recorded."""
    valid_from = fact.valid_from if fact is not None else item.expires_on
    days = (item.expires_on - as_of).days if item.expires_on else 0
    satisfied_doc_id = satisfied_doc_title = ""
    if item.received_episode_id:
        episode = episodes_by_id.get(item.received_episode_id)
        if episode is not None:
            satisfied_doc_id = episode.doc_id
            satisfied_doc_title = episode.title
    return ChaseFact(
        predicate=item.predicate,
        entity_ref=item.entity_ref,
        value=item.value if item.value is not None else "",
        unit=item.unit,
        valid_from=valid_from,
        valid_to=item.expires_on,
        watch_state="lapsed" if days < 0 else "expiring",
        days_remaining=days,
        episode_id=fact.episode_id if fact is not None else "",
        doc_id=item.doc_id,
        doc_title=item.doc_title,
        satisfied_doc_id=satisfied_doc_id,
        satisfied_doc_title=satisfied_doc_title,
    )


def _row_for_predicate(
    *,
    account: Account,
    brief_store,
    follow_store,
    predicate: str,
    flagged: list[ExpiryEntry],
    facts: list[Fact],
    episodes_by_id: dict,
    as_of: date,
) -> RenewalChase | None:
    """The chase row for one predicate, or ``None`` when the
    predicate has neither flagged facts nor chase history.
    State resolution follows the module docstring's order."""
    records = [
        r
        for r in _chase_records(follow_store, account.account_id)
        if any(i.predicate == predicate for i in r.items)
    ]
    open_ask = next((r for r in records if r.status != "complete"), None)
    briefs = brief_store.list(account.account_id)
    latest = briefs[-1] if briefs else None
    label = predicate_label(predicate)
    facts_by_id = {f.fact_id: f for f in facts}

    if open_ask is not None:
        row_facts = []
        flagged_by_key = {
            (r.entity_ref, r.valid_to): r for r in flagged
        }
        for item in open_ask.items:
            if item.predicate != predicate:
                continue
            watch_row = flagged_by_key.get((item.entity_ref, item.expires_on))
            if watch_row is not None and item.status != "received":
                row_facts.append(_fact_from_watch(watch_row))
            else:
                row_facts.append(
                    _fact_from_item(
                        item,
                        facts_by_id.get(item.fact_id),
                        episodes_by_id,
                        as_of,
                    )
                )
        attached = next(
            (b for b in briefs if b.brief_id == open_ask.brief_id), None
        )
        return RenewalChase(
            account_id=account.account_id,
            account_name=account.display_name,
            predicate=predicate,
            label=label,
            state="ASKED",
            facts=row_facts,
            brief_id=open_ask.brief_id,
            brief_status=attached.status if attached is not None else None,
            ask_record_id=open_ask.record_id,
            ask_status=open_ask.status,
            days_remaining=min(
                (f.days_remaining for f in row_facts), default=None
            ),
        )

    if flagged:
        row_facts = [_fact_from_watch(r) for r in flagged]
        if any(f.watch_state == "lapsed" for f in row_facts):
            state = "LAPSED_UNSENT"
        elif latest is None:
            state = "WAITING_ON_BRIEF"
        else:
            state = "OPEN"
        return RenewalChase(
            account_id=account.account_id,
            account_name=account.display_name,
            predicate=predicate,
            label=label,
            state=state,
            facts=row_facts,
            brief_id=latest.brief_id if latest is not None else None,
            brief_status=latest.status if latest is not None else None,
            days_remaining=min(f.days_remaining for f in row_facts),
        )

    completed = [r for r in records if r.status == "complete"]
    if completed:
        record = completed[-1]
        row_facts = [
            _fact_from_item(
                item, facts_by_id.get(item.fact_id), episodes_by_id, as_of
            )
            for item in record.items
            if item.predicate == predicate
        ]
        return RenewalChase(
            account_id=account.account_id,
            account_name=account.display_name,
            predicate=predicate,
            label=label,
            state="SATISFIED",
            facts=row_facts,
            brief_id=record.brief_id,
            ask_record_id=record.record_id,
            ask_status=record.status,
            days_remaining=None,
        )
    return None


def chases_for_account(
    *,
    port: ContextPort,
    account: Account,
    brief_store,
    follow_store,
    as_of: date,
    horizon_days: int,
) -> list[RenewalChase]:
    """Every chase row for one account at ``as_of``: one per
    predicate the watch flags or a chase has touched, minus
    the predicates an open request-info ask covers (the
    dedupe — unsent rows only; ASKED and SATISFIED rows are
    the chase's own story and stay). Currency is the watch's:
    this function adds the chase, never a second expiry
    opinion. Pure reads — computing the rows mutates nothing."""
    watch = timeline_mod.expiry_watch_for_account(
        port=port,
        account=account,
        as_of=as_of,
        horizon_days=horizon_days,
    )
    facts = port.get_facts(account.account_id)
    episodes_by_id = {
        e.episode_id: e for e in port.get_episodes(account.account_id)
    }
    covered = covered_predicates(follow_store, account.account_id)

    predicates: list[str] = []
    for row in watch:
        if row.predicate not in predicates:
            predicates.append(row.predicate)
    for record in _chase_records(follow_store, account.account_id):
        for item in record.items:
            if item.predicate and item.predicate not in predicates:
                predicates.append(item.predicate)

    rows: list[RenewalChase] = []
    for predicate in predicates:
        row = _row_for_predicate(
            account=account,
            brief_store=brief_store,
            follow_store=follow_store,
            predicate=predicate,
            flagged=[r for r in watch if r.predicate == predicate],
            facts=facts,
            episodes_by_id=episodes_by_id,
            as_of=as_of,
        )
        if row is None:
            continue
        if predicate in covered and row.state not in ("ASKED", "SATISFIED"):
            continue  # the dedupe: already asked; never ask twice
        rows.append(row)
    rows.sort(
        key=lambda r: (
            r.days_remaining is None,
            r.days_remaining or 0,
            r.predicate,
        )
    )
    return rows


def build_chases(
    *,
    port: ContextPort,
    accounts: dict[str, Account],
    brief_store,
    follow_store,
    as_of: date,
    horizon_days: int,
) -> list[RenewalChase]:
    """The desk-level chase list: every account's rows, the
    soonest clock first (lapsed rows lead, as on the watch),
    satisfied rows closing the list. Pure reads."""
    rows: list[RenewalChase] = []
    for account_id in sorted(accounts):
        rows.extend(
            chases_for_account(
                port=port,
                account=accounts[account_id],
                brief_store=brief_store,
                follow_store=follow_store,
                as_of=as_of,
                horizon_days=horizon_days,
            )
        )
    rows.sort(
        key=lambda r: (
            r.days_remaining is None,
            r.days_remaining or 0,
            r.account_name,
            r.account_id,
            r.predicate,
        )
    )
    return rows


def open_chase_count(rows: list[RenewalChase]) -> int:
    """How many of an account's chase rows stand open — the
    portfolio brief's display count. SATISFIED never counts."""
    return sum(1 for r in rows if r.state in OPEN_STATES)


def desk_chases(rows: list[RenewalChase]) -> list[DeskChase]:
    """An account's chase rows in desk-queue shape."""
    return [
        DeskChase(
            predicate=r.predicate,
            label=r.label,
            state=r.state,
            days_remaining=r.days_remaining,
            ask_record_id=r.ask_record_id,
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# The send — the chase becomes an ordinary open ask
# ---------------------------------------------------------------------------


def compose_chase_reason(items) -> str:
    """The reason the chase record carries, composed in code
    from the items: what is running out, and when it ends."""
    named = "; ".join(
        f"{item.label}"
        f"{f' [{item.entity_ref}]' if item.entity_ref else ''} "
        f"ends {item.expires_on}"
        for item in items
    )
    return f"Renewal chase: {named}."


def send_renewal_chase(
    *,
    port: ContextPort,
    account: Account,
    brief_store,
    follow_store,
    predicate: str,
    actor: str,
    as_of: date,
    horizon_days: int,
) -> tuple[FollowThroughRecord, RenewalChase]:
    """Send one chase: open its follow-through ask on the
    account's latest brief, through the follow-through
    machinery, and return the record beside the row as it now
    reads (ASKED). Every refusal is a :class:`ChaseError`
    naming its cause; a refused send opens nothing."""
    label = predicate_label(predicate)
    if not actor or not actor.strip():
        raise ChaseError(
            "A renewal chase requires an actor — the send is a "
            "named human act, and the ask carries the name."
        )
    rows = chases_for_account(
        port=port,
        account=account,
        brief_store=brief_store,
        follow_store=follow_store,
        as_of=as_of,
        horizon_days=horizon_days,
    )
    row = next((r for r in rows if r.predicate == predicate), None)
    if row is not None and row.state == "ASKED":
        raise ChaseError(
            f"The renewal chase for {label} on {account.display_name} "
            f"({account.account_id}) was already sent — ask "
            f"{row.ask_record_id} is still open. The broker has "
            "been asked once; the chase does not nag twice."
        )
    covered = covered_predicates(follow_store, account.account_id)
    if predicate in covered:
        record = covered[predicate]
        raise ChaseError(
            f"{label} on {account.display_name} ({account.account_id}) "
            f"is already asked of the broker: ask {record.record_id} "
            f"from brief {record.brief_id} (request_info by "
            f"{record.decision_actor}) is still open and names it. "
            "A renewal chase would ask twice — the dedupe refuses."
        )
    if row is None:
        raise ChaseError(
            f"There is no renewal chase for {label} on "
            f"{account.display_name} ({account.account_id}): "
            "nothing the account relies on under that predicate "
            "is expiring inside the horizon, and no chase for it "
            "stands open or satisfied."
        )
    if row.state == "SATISFIED":
        docs = ", ".join(
            f'"{f.satisfied_doc_title}"' for f in row.facts if f.satisfied_doc_title
        )
        raise ChaseError(
            f"The renewal chase for {label} on {account.display_name} "
            f"({account.account_id}) is already satisfied"
            + (f" — the renewal arrived in {docs}." if docs else ".")
        )
    # A LAPSED_UNSENT row outranks WAITING_ON_BRIEF in the
    # projection, so a lapsed chase on a brief-less account
    # reaches here too — refuse it on the brief, not the state.
    if row.state == "WAITING_ON_BRIEF" or row.brief_id is None:
        raise ChaseError(
            f"The renewal chase for {label} on {account.display_name} "
            f"({account.account_id}) cannot be sent: the account has "
            "no brief, and a follow-through ask attaches to a brief. "
            "Assemble a brief first — the chase stays on the desk "
            "until then."
        )
    # OPEN or LAPSED_UNSENT: the send itself.
    watch = timeline_mod.expiry_watch_for_account(
        port=port,
        account=account,
        as_of=as_of,
        horizon_days=horizon_days,
    )
    flagged = [r for r in watch if r.predicate == predicate]
    items = follow_mod.compose_renewal_items(
        flagged, port.get_facts(account.account_id)
    )
    briefs = brief_store.list(account.account_id)
    latest = briefs[-1]  # the no-brief case was refused above
    record = follow_mod.open_renewal_ask(
        follow_store,
        latest,
        items,
        actor=actor,
        reason=compose_chase_reason(items),
    )
    refreshed = chases_for_account(
        port=port,
        account=account,
        brief_store=brief_store,
        follow_store=follow_store,
        as_of=as_of,
        horizon_days=horizon_days,
    )
    new_row = next(r for r in refreshed if r.predicate == predicate)
    return record, new_row


# ---------------------------------------------------------------------------
# Text rendering (the CLI's shape; the watch precedent)
# ---------------------------------------------------------------------------


def _fmt_value(value, unit: str) -> str:
    text = f"{value:,}" if isinstance(value, (int, float)) else str(value)
    return f"{text} {unit}" if unit else text


def _clock(fact: ChaseFact) -> str:
    if fact.watch_state == "lapsed":
        return f"lapsed {-fact.days_remaining}d ago"
    return f"expires in {fact.days_remaining}d"


def render_chases_text(
    rows: list[RenewalChase], as_of: date, horizon_days: int
) -> str:
    lines = [
        f"Renewal chases as of {as_of} (expiry horizon {horizon_days} days)",
        "",
    ]
    if not rows:
        lines.append("Nothing is running out unasked.")
    for r in rows:
        lines.append(
            f"{r.account_id}  {r.account_name}  {r.predicate} — {r.state}"
        )
        for f in r.facts:
            line = (
                f"    {r.label}"
                f"{f' [{f.entity_ref}]' if f.entity_ref else ''}: "
                f"{_fmt_value(f.value, f.unit)} ends {f.valid_to} "
                f"({_clock(f)})"
            )
            if f.satisfied_doc_title:
                line += f' — renewed by "{f.satisfied_doc_title}"'
            elif f.doc_title:
                line += f'  [{f.doc_title} ({f.doc_id})]'
            lines.append(line)
        if r.state == "WAITING_ON_BRIEF":
            lines.append(
                "    no brief on the account — assemble one before "
                "the chase can be sent"
            )
        if r.ask_record_id:
            lines.append(f"    ask {r.ask_record_id} ({r.ask_status})")
    return "\n".join(lines) + "\n"
