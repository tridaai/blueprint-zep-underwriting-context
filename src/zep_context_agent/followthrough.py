"""Request-info follow-through: a decision becomes a tracked ask.

An underwriter who decides ``request_info`` has named what the
file lacks — and until this module, the system recorded the
decision and forgot the ask. The asking was left to the human's
memory, and when the broker replied, nothing connected the new
material to the open ask. This module is the follow-through:

- **The ask is composed in code** (:func:`compose_items`) from
  the decided brief: its ``missing_predicates`` (asked for by
  their underwriter-terms names — :func:`ontology.predicate_label`)
  and its ``quarantined`` documents (named by title, to be resent
  cleanly). The draft (:func:`compose_draft`) is deterministic
  text, headed DRAFT, stored on the record. **It is never sent —
  there is no send path anywhere in the system**; the API and
  console show it for the underwriter to copy.
- **A request-info ask on a complete file is refused — the
  draft is.** Composing an ask from a brief with no missing
  predicates and no quarantined documents raises
  :class:`FollowThroughError`: there is nothing to ask, and an
  open ask with nothing in it could never complete. The
  *decision* still records — the gate is the human's, and the
  ontology's completeness view does not overrule it (an
  underwriter may want something the ontology does not track) —
  but no record opens, and the service names the refusal on the
  decided brief's audit trail (``follow_through_refused``), so
  "we asked" is never a fiction the record tells.
- **The answer is recognised, not asserted**
  (:func:`evaluate_after_ingestion`). When new episodes are
  ingested for the account — through the same service and
  pipeline hooks the staleness watch uses — each open record is
  re-read against a fresh, transient assembly (never stored):
  a requested predicate that is now a current fact is marked
  ``received``, with the episode the assembly cites for it. A
  requested document counts as resent when a non-quarantined
  episode exists carrying the same title under a new document
  id. A resend under the *same* document id cannot count:
  ingestion is idempotent by document id, so it is
  indistinguishable from a duplicate arrival and the quarantined
  episode stands — the broker's corrected copy must arrive as
  a new document, as every wave in the dataset does. Marking
  never reads raw episode text; it reads the same resolved
  fact set an underwriter would see in a brief.
- **The record closes when every item is received** — status
  ``open`` → ``partially_received`` → ``complete``, with the
  completion stamp. Nothing auto-assembles or auto-decides at
  completion: the desk queue signals ``ready_for_review`` and
  the underwriter works the account as usual.
- **Independence from the staleness watch.** The same ingestion
  can complete an ask and stale a decision; the two records
  never write each other. A fresh decision resolves staleness
  (round 3's rule) and leaves an open ask open — the broker
  still owes the answer — and a completed ask clears no flag.

The store split follows the brief store's: Postgres (JSONB
payloads, migration 0005) in production, an in-memory double in
tests. A separate store (not a field on the brief) because the
decided brief is an audit record that is never edited, and the
ask has its own lifecycle — items flip over days, long after
the brief froze.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import date, datetime, timezone

from . import assembly
from .errors import FollowThroughError
from .ontology import Account, predicate_label
from .ports import ContextPort
from .schemas import Brief, FollowThroughItem, FollowThroughRecord


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


class InMemoryFollowThroughStore:
    """Dict-backed follow-through store (the test double)."""

    def __init__(self) -> None:
        self._records: dict[str, FollowThroughRecord] = {}
        self._order: list[str] = []

    def save(self, record: FollowThroughRecord) -> FollowThroughRecord:
        if record.record_id not in self._records:
            self._order.append(record.record_id)
        self._records[record.record_id] = record
        return record

    def get(self, record_id: str) -> FollowThroughRecord | None:
        return self._records.get(record_id)

    def list(self, account_id: str | None = None) -> list[FollowThroughRecord]:
        records = [self._records[rid] for rid in self._order]
        if account_id is not None:
            records = [r for r in records if r.account_id == account_id]
        return records


class PostgresFollowThroughStore:
    """Follow-through store over the ``follow_through_records`` table."""

    def __init__(self, url: str) -> None:
        from .db import ensure_migrated

        ensure_migrated(url)
        self._url = url
        self._lock = threading.Lock()

    def _connect(self):
        from .db import connect

        return connect(self._url)

    def save(self, record: FollowThroughRecord) -> FollowThroughRecord:
        payload = json.dumps(record.model_dump(mode="json"))
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO follow_through_records "
                "(record_id, account_id, brief_id, payload, complete) "
                "VALUES (%s, %s, %s, %s::jsonb, %s) "
                "ON CONFLICT (record_id) DO UPDATE SET "
                "payload = EXCLUDED.payload, complete = EXCLUDED.complete, "
                "updated_at = now()",
                (
                    record.record_id,
                    record.account_id,
                    record.brief_id,
                    payload,
                    record.status == "complete",
                ),
            )
            conn.commit()
        return record

    def get(self, record_id: str) -> FollowThroughRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM follow_through_records "
                "WHERE record_id = %s",
                (record_id,),
            ).fetchone()
        if row is None:
            return None
        payload = row[0]
        if isinstance(payload, str):
            payload = json.loads(payload)
        return FollowThroughRecord.model_validate(payload)

    def list(self, account_id: str | None = None) -> list[FollowThroughRecord]:
        query = "SELECT payload FROM follow_through_records"
        params: tuple = ()
        if account_id is not None:
            query += " WHERE account_id = %s"
            params = (account_id,)
        query += " ORDER BY seq"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        records = []
        for (payload,) in rows:
            if isinstance(payload, str):
                payload = json.loads(payload)
            records.append(FollowThroughRecord.model_validate(payload))
        return records


def default_follow_store():
    """The configured follow-through store: Postgres when
    ``DATABASE_URL`` is set, the in-memory test double otherwise
    — the brief store's split, for the same reasons."""
    from .db import database_url

    url = database_url()
    if url:
        return PostgresFollowThroughStore(url)
    return InMemoryFollowThroughStore()


def open_records(store, account_id: str) -> list[FollowThroughRecord]:
    """The account's not-yet-complete asks, oldest first."""
    return [
        r for r in store.list(account_id) if r.status != "complete"
    ]


def record_for_brief(store, brief_id: str) -> FollowThroughRecord | None:
    """The follow-through record opened by a brief's decision."""
    for record in store.list():
        if record.brief_id == brief_id:
            return record
    return None


def latest_for_account(store, account_id: str) -> FollowThroughRecord | None:
    records = store.list(account_id)
    return records[-1] if records else None


# ---------------------------------------------------------------------------
# The ask — composed in code from the decided brief
# ---------------------------------------------------------------------------


def compose_items(brief: Brief) -> list[FollowThroughItem]:
    """The structured ask a ``request_info`` decision on ``brief``
    makes: one item per missing predicate (in the brief's own
    order), one per quarantined document. Raises
    :class:`FollowThroughError` when the brief has neither —
    a request for information on a complete file is a
    contradiction the code names, not an empty ask it opens."""
    items = [
        FollowThroughItem(
            kind="predicate",
            predicate=predicate,
            label=predicate_label(predicate),
        )
        for predicate in brief.missing_predicates
    ]
    items += [
        FollowThroughItem(
            kind="document",
            label=note.doc_title,
            doc_id=note.doc_id,
            doc_title=note.doc_title,
            episode_id=note.episode_id,
            reason=note.reason,
        )
        for note in brief.quarantined
    ]
    if not items:
        raise FollowThroughError(
            f"Brief {brief.brief_id} has nothing to ask for: no "
            "predicate is missing and no document was quarantined, "
            "so a request_info decision would open an ask that can "
            "never complete. Decide proceed or escalate instead."
        )
    return items


def compose_draft(brief: Brief, items: list[FollowThroughItem]) -> str:
    """The broker request, as deterministic text.

    Composed from the brief and the structured items only — no
    model, no free invention. Headed DRAFT and addressed to the
    broker of record for the underwriter to copy: the system
    prepares the ask, the human sends it (outside this system,
    as with every gate decision here)."""
    decision = brief.decision
    lines = [
        "DRAFT — request for information, prepared for the "
        "underwriter to send; not sent.",
        f"To: the broker of record for {brief.account_name} "
        f"({brief.account_id})",
    ]
    if decision is not None:
        lines.append(
            f"Re: Underwriting file for {brief.account_name} — "
            f"information requested on brief {brief.brief_id} "
            f"(decision: request_info by {decision.actor} on "
            f"{decision.decided_at.date().isoformat()})"
        )
        lines.append(f"Underwriter's note: {decision.reason}")
    lines.append("")
    predicates = [i for i in items if i.kind == "predicate"]
    documents = [i for i in items if i.kind == "document"]
    if predicates:
        lines.append("To complete the file, please provide:")
        for item in predicates:
            lines.append(f"- {item.label} (not in the file)")
        lines.append("")
    if documents:
        lines.append(
            "Please also resend the following document(s) — the "
            "copies on file could not be used:"
        )
        for item in documents:
            lines.append(f'- "{item.doc_title}" — {item.reason}')
        lines.append("")
    lines.append(
        "This request was prepared from the underwriting brief; "
        "reply through the usual broker channel."
    )
    return "\n".join(lines) + "\n"


def open_follow_through(
    store, brief: Brief, items: list[FollowThroughItem]
) -> FollowThroughRecord:
    """Open the record for a just-decided ``request_info`` brief.

    ``items`` come from :func:`compose_items`; the draft is
    composed here from the decided brief, so it names the
    decision as recorded."""
    if brief.decision is None:
        raise FollowThroughError(
            f"Brief {brief.brief_id} carries no decision; a "
            "follow-through record opens on a recorded decision."
        )
    now = _now()
    record = FollowThroughRecord(
        record_id=f"ask-{uuid.uuid4().hex[:12]}",
        account_id=brief.account_id,
        brief_id=brief.brief_id,
        decision_actor=brief.decision.actor,
        decision_reason=brief.decision.reason,
        decided_at=brief.decision.decided_at,
        created_at=now,
        items=items,
        status="open",
        updated_at=now,
    )
    record.draft_text = compose_draft(brief, record.items)
    return store.save(record)


# ---------------------------------------------------------------------------
# The answer — recognised against a fresh assembly
# ---------------------------------------------------------------------------


def _refresh_status(record: FollowThroughRecord, at: datetime) -> None:
    received = sum(1 for i in record.items if i.status == "received")
    if received == len(record.items) and record.items:
        record.status = "complete"
        if record.completed_at is None:
            record.completed_at = at
    elif received:
        record.status = "partially_received"
    else:
        record.status = "open"
    record.updated_at = at


def evaluate_after_ingestion(
    *,
    port: ContextPort,
    follow_store,
    account: Account,
    trigger_episode_ids: list[str],
    as_of: date | None = None,
) -> list[FollowThroughRecord]:
    """Re-read the account's open asks against the facts as they
    now stand; flip the items the new material satisfies.

    Returns the records that changed. Marking reads one fresh,
    transient assembly (never stored) — the same resolved fact
    set an underwriter would see — plus the account's episodes:

    - a *predicate* item is received when the predicate is a
      current fact in the fresh assembly; the satisfying episode
      is the one the assembly cites for that fact;
    - a *document* item is received when a non-quarantined
      episode — not the quarantined episode itself — carries
      the requested document's title (a corrected resend under
      a new document id), choosing the earliest such episode
      in file order.

    No new episodes (an idempotent re-ingestion) means no
    evaluation — a duplicate arrival marks nothing."""
    if not trigger_episode_ids:
        return []
    open_recs = open_records(follow_store, account.account_id)
    if not open_recs:
        return []
    current = assembly.assemble_brief(
        port, account, as_of or date.today()
    )
    episodes = sorted(
        port.get_episodes(account.account_id),
        key=lambda e: (e.received_at, e.episode_id),
    )
    at = _now()
    changed: list[FollowThroughRecord] = []
    for record in open_recs:
        touched = False
        for item in record.items:
            if item.status == "received":
                continue
            if item.kind == "predicate":
                fact = next(
                    (
                        f
                        for f in current.current_facts
                        if f.predicate == item.predicate
                    ),
                    None,
                )
                if fact is not None:
                    item.status = "received"
                    item.received_episode_id = fact.episode_id
                    item.received_at = at
                    touched = True
            else:  # document item: a clean resend under a new id
                match = next(
                    (
                        e
                        for e in episodes
                        if not e.quarantined
                        and e.episode_id != item.episode_id
                        and e.title == item.doc_title
                    ),
                    None,
                )
                if match is not None:
                    item.status = "received"
                    item.received_episode_id = match.episode_id
                    item.received_at = at
                    touched = True
        if touched:
            _refresh_status(record, at)
            follow_store.save(record)
            changed.append(record)
    return changed


def render_follow_through_text(record: FollowThroughRecord) -> str:
    """The record as text: the ask, its items and their state,
    and the draft — the CLI's view, from the same record the API
    serves as JSON."""
    received = sum(1 for i in record.items if i.status == "received")
    lines = [
        f"FOLLOW-THROUGH {record.record_id} — {record.status} "
        f"({received} of {len(record.items)} items received)",
        f"Account {record.account_id} · from brief {record.brief_id} · "
        f"request_info by {record.decision_actor} on "
        f"{record.decided_at.date().isoformat()}: {record.decision_reason}",
        "",
        "ITEMS",
    ]
    for item in record.items:
        mark = "x" if item.status == "received" else " "
        via = (
            f" — received via {item.received_episode_id}"
            if item.status == "received"
            else ""
        )
        lines.append(f"  [{mark}] {item.label} ({item.kind}){via}")
    if record.completed_at is not None:
        lines.append("")
        lines.append(
            f"COMPLETE — every item received as of "
            f"{record.completed_at.date().isoformat()}. The broker "
            "has answered; review is the underwriter's next step."
        )
    lines.append("")
    lines.append("THE ASK (draft — prepared for the underwriter; never sent)")
    lines.append(record.draft_text.rstrip())
    return "\n".join(lines) + "\n"
