"""The staleness watch: decisions go stale, and the desk finds out.

An underwriter decides on Monday's facts. On Wednesday a new
endorsement lands. The decided brief is the audit record of what
was decided on — it is never edited — but nothing in it can say
the world moved. This module is the watcher:

- **Evaluation** runs whenever episodes are ingested through the
  service (the ingest endpoint, and the pipeline's ingest step,
  which every assembly traverses). If the account's latest brief
  is DECIDED, the account's current fact set — resolved today —
  is diffed against the facts the decided brief carried, using
  the round-2 diff machinery (:mod:`diffing`). A *material*
  change raises a :class:`StalenessRecord`: the decision it
  refers to, the episodes that triggered the evaluation, and
  the material slice of the diff. The record lives in its own
  store; the decided brief stays byte-identical.
- **Materiality** is defined in exactly one place —
  :func:`material_slice`. A change is material when a fact the
  decided brief carried as current was superseded or removed,
  or a new current fact appears on a predicate the brief *used*:
  one of its current facts' predicates, or a predicate it
  reported missing ("not in the file" becoming a value is
  exactly the kind of news a decision wants). Two exclusions:
  the ``underwriter_decision`` predicate never counts — it is
  the gate's own bookkeeping, and a decision must not stale
  itself — and an unrelated new predicate the brief never
  touched does not flag on its own (every ontology-expected
  predicate is by construction either current or missing in a
  brief, so "untouched" means outside the brief's vocabulary).
- **Triggers are episodes first ingested after the decision.**
  Ingestion is idempotent; re-ingesting the file that was
  already there yields no new episodes and is skipped without
  evaluating — a duplicate arrival cannot flag. New material
  that changes nothing material (a quarantined document, whose
  assertions never become facts; a restatement of a retired
  fact) evaluates to an empty slice and does not flag either.
- **Resolution** is a fresh decision: re-assemble, decide at
  the gate, and the open records for the account are stamped
  ``resolved_by_brief`` with the new brief's id. There is
  deliberately no dismiss — a stale flag clears when an
  underwriter has decided on current facts, not when someone
  acknowledges it. If more material lands while a flag is open,
  its episodes join the open record's trigger list; the slice
  stays the one that raised the flag.
- **The desk queue** (:func:`build_desk_queue`) is a read model
  over briefs and staleness records: stale decisions first
  (oldest decision first — it has rested on wrong facts the
  longest), then briefs awaiting the gate (oldest first), then
  clean decisions (most recent first), then accounts with no
  brief. It decides nothing and mutates nothing.

The store split follows the brief store's: Postgres (JSONB
payloads, migration 0004) in production, an in-memory double in
tests. Evaluation reads the ContextPort directly — never the
brief cache — so a flag appears even when the decided brief was
cache-served minutes before; the cache's version bump on
ingestion is a second, independent guarantee that no stale
payload is served as current.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import date, datetime, timezone

from . import assembly, diffing
from .ontology import Account
from .ports import ContextPort
from .schemas import (
    Brief,
    BriefDiff,
    DeskDecision,
    DeskFollowThrough,
    DeskQueueEntry,
    DeskStaleness,
    StalenessRecord,
)

#: The gate's own bookkeeping predicate. A decision memo joining
#: the graph after a decision is the decision's echo, never news
#: about the account — it is excluded from materiality outright.
DECISION_PREDICATE = "underwriter_decision"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


class InMemoryStalenessStore:
    """Dict-backed staleness store (the test double)."""

    def __init__(self) -> None:
        self._records: dict[str, StalenessRecord] = {}
        self._order: list[str] = []

    def save(self, record: StalenessRecord) -> StalenessRecord:
        if record.record_id not in self._records:
            self._order.append(record.record_id)
        self._records[record.record_id] = record
        return record

    def get(self, record_id: str) -> StalenessRecord | None:
        return self._records.get(record_id)

    def list(self, account_id: str | None = None) -> list[StalenessRecord]:
        records = [self._records[rid] for rid in self._order]
        if account_id is not None:
            records = [r for r in records if r.account_id == account_id]
        return records


class PostgresStalenessStore:
    """Staleness store over the ``staleness_records`` table."""

    def __init__(self, url: str) -> None:
        from .db import ensure_migrated

        ensure_migrated(url)
        self._url = url
        self._lock = threading.Lock()

    def _connect(self):
        from .db import connect

        return connect(self._url)

    def save(self, record: StalenessRecord) -> StalenessRecord:
        payload = json.dumps(record.model_dump(mode="json"))
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO staleness_records "
                "(record_id, account_id, brief_id, payload, resolved) "
                "VALUES (%s, %s, %s, %s::jsonb, %s) "
                "ON CONFLICT (record_id) DO UPDATE SET "
                "payload = EXCLUDED.payload, resolved = EXCLUDED.resolved, "
                "updated_at = now()",
                (
                    record.record_id,
                    record.account_id,
                    record.brief_id,
                    payload,
                    record.resolved_by_brief is not None,
                ),
            )
            conn.commit()
        return record

    def get(self, record_id: str) -> StalenessRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM staleness_records WHERE record_id = %s",
                (record_id,),
            ).fetchone()
        if row is None:
            return None
        payload = row[0]
        if isinstance(payload, str):
            payload = json.loads(payload)
        return StalenessRecord.model_validate(payload)

    def list(self, account_id: str | None = None) -> list[StalenessRecord]:
        query = "SELECT payload FROM staleness_records"
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
            records.append(StalenessRecord.model_validate(payload))
        return records


def default_staleness_store():
    """The configured staleness store: Postgres when
    ``DATABASE_URL`` is set, the in-memory test double otherwise
    — the brief store's split, for the same reasons."""
    from .db import database_url

    url = database_url()
    if url:
        return PostgresStalenessStore(url)
    return InMemoryStalenessStore()


def open_records(store, account_id: str) -> list[StalenessRecord]:
    """The account's unresolved staleness records, oldest first."""
    return [
        r for r in store.list(account_id) if r.resolved_by_brief is None
    ]


# ---------------------------------------------------------------------------
# Materiality — the one place the rule lives
# ---------------------------------------------------------------------------


def material_slice(decided: Brief, diff: BriefDiff) -> BriefDiff | None:
    """The material part of ``diff`` against the DECIDED brief
    ``decided``, or ``None`` when nothing in it can move the
    decision. The rule is the module docstring's: supersessions
    and removals of facts the brief carried, additions on
    predicates the brief used (current or reported missing),
    and never the gate's own ``underwriter_decision`` echo."""
    used = {f.predicate for f in decided.current_facts} | set(
        decided.missing_predicates
    )
    superseded = [
        c for c in diff.superseded if c.predicate != DECISION_PREDICATE
    ]
    removed = [f for f in diff.removed if f.predicate != DECISION_PREDICATE]
    added = [
        f
        for f in diff.added
        if f.predicate != DECISION_PREDICATE and f.predicate in used
    ]
    if not (added or superseded or removed):
        return None
    return BriefDiff(
        previous_brief_id=diff.previous_brief_id,
        previous_as_of=diff.previous_as_of,
        added=added,
        superseded=superseded,
        removed=removed,
        decisions=[],  # the decision rides the record itself
    )


# ---------------------------------------------------------------------------
# Evaluation + resolution
# ---------------------------------------------------------------------------


def new_episode_ids(before_ids: set[str], episodes) -> list[str]:
    """Episode ids in ``episodes`` that were not there before —
    the honest trigger set. Ingestion is idempotent, so a
    re-ingested document is in ``before_ids`` and never counts
    as new material."""
    return [e.episode_id for e in episodes if e.episode_id not in before_ids]


def latest_decided_brief(brief_store, account_id: str) -> Brief | None:
    decided = [
        b
        for b in brief_store.list(account_id)
        if b.status == "DECIDED" and b.decision is not None
    ]
    return decided[-1] if decided else None


def evaluate_after_ingestion(
    *,
    port: ContextPort,
    brief_store,
    staleness_store,
    account: Account,
    trigger_episode_ids: list[str],
    as_of: date | None = None,
) -> StalenessRecord | None:
    """Compare the account's latest DECIDED brief against the
    facts as they now stand; raise (or extend) a staleness
    record when the change is material.

    Returns the open record for the decision — newly created,
    or the existing one with the new triggers appended — and
    ``None`` when there is nothing to flag: no decided brief, no
    new episodes, or no material change. The decided brief is
    read, never written."""
    if not trigger_episode_ids:
        return None
    decided = latest_decided_brief(brief_store, account.account_id)
    if decided is None:
        return None
    existing = [
        r for r in open_records(staleness_store, account.account_id)
        if r.brief_id == decided.brief_id
    ]
    if existing:
        record = existing[0]
        for episode_id in trigger_episode_ids:
            if episode_id not in record.trigger_episode_ids:
                record.trigger_episode_ids.append(episode_id)
        staleness_store.save(record)
        return record
    current = assembly.assemble_brief(
        port, account, as_of or date.today()
    )
    slice_ = material_slice(decided, diffing.compute_diff(decided, current))
    if slice_ is None:
        return None
    record = StalenessRecord(
        record_id=f"stale-{uuid.uuid4().hex[:12]}",
        account_id=account.account_id,
        brief_id=decided.brief_id,
        decision_action=decided.decision.action,
        decision_actor=decided.decision.actor,
        decided_at=decided.decision.decided_at,
        flagged_at=_now(),
        trigger_episode_ids=list(trigger_episode_ids),
        changes=slice_,
    )
    staleness_store.save(record)
    return record


def resolve_open_records(
    staleness_store,
    *,
    account_id: str,
    resolved_by_brief: str,
    actor: str,
    at: datetime | None = None,
) -> list[StalenessRecord]:
    """Stamp the account's open staleness records resolved by a
    fresh gate decision — the only way a flag clears. Records
    referring to the deciding brief itself are skipped (a brief
    cannot resolve its own staleness; in practice a decided
    brief is never the freshly decided one)."""
    resolved = []
    for record in open_records(staleness_store, account_id):
        if record.brief_id == resolved_by_brief:
            continue
        record.resolved_by_brief = resolved_by_brief
        record.resolved_at = at or _now()
        record.resolved_by = actor
        staleness_store.save(record)
        resolved.append(record)
    return resolved


# ---------------------------------------------------------------------------
# The desk queue — a read model
# ---------------------------------------------------------------------------


def build_desk_queue(
    accounts: dict[str, Account],
    brief_store,
    staleness_store,
    *,
    follow_store=None,
    today: date | None = None,
    port: ContextPort | None = None,
    expiry_horizon_days: int | None = None,
) -> list[DeskQueueEntry]:
    """Every account, in the order the desk should work it:
    stale decisions first (oldest decision first), awaiting
    briefs (oldest first), clean decisions (most recent first),
    accounts with no brief last. Pure reads — the queue
    decides nothing and mutates nothing.

    When ``port`` is given, each row also carries the
    account's expiry watch (see :mod:`timeline`) computed as
    of ``today``: the relied-on facts running out under the
    account. The ordering is untouched by the watch — a
    lapsing policy does not outrank a stale decision; the
    row simply says both. The same port access computes the
    row's thin-fact count (see :mod:`support`): how many of
    the account's current facts grade THIN. Also display
    only — thin support is something to know at the gate,
    not a re-ordering of the day. With the follow-through
    store as well, the row carries the account's renewal
    chases (see :mod:`chases`) under the same discipline:
    annotation, never ordering."""
    today = today or date.today()
    stale_rows: list[tuple[datetime, DeskQueueEntry]] = []
    awaiting_rows: list[tuple[datetime, DeskQueueEntry]] = []
    decided_rows: list[tuple[datetime, DeskQueueEntry]] = []
    no_brief_rows: list[DeskQueueEntry] = []

    for account_id in sorted(accounts):
        account = accounts[account_id]
        briefs = brief_store.list(account_id)
        latest = briefs[-1] if briefs else None
        open_recs = open_records(staleness_store, account_id)
        entry = DeskQueueEntry(
            account_id=account_id,
            account_name=account.display_name,
            state="no_brief",
        )
        if latest is not None:
            entry.brief_id = latest.brief_id
            entry.brief_status = latest.status
            entry.brief_as_of = latest.as_of
            # Was the latest brief assembled as a renewal (see
            # renewals.py)? A display annotation under the same
            # discipline as the watch and the thin count — the
            # ordering below never reads it.
            entry.brief_is_renewal = latest.renewal is not None
            if latest.decision is not None:
                entry.decision = DeskDecision(
                    action=latest.decision.action,
                    actor=latest.decision.actor,
                    decided_on=latest.decision.decided_at.date(),
                )
        if port is not None:
            from . import support as support_mod
            from . import timeline as timeline_mod
            from .config import (
                expiry_horizon_days as _default_horizon,
            )

            horizon = (
                expiry_horizon_days
                if expiry_horizon_days is not None
                else _default_horizon()
            )
            entry.thin_fact_count = support_mod.thin_count(
                port.get_facts(account_id), today
            )
            entry.expiry = timeline_mod.desk_expiry(
                timeline_mod.expiry_watch_for_account(
                    port=port,
                    account=account,
                    as_of=today,
                    horizon_days=horizon,
                )
            )
            if follow_store is not None:
                # The account's renewal chases (see chases.py):
                # the watch's expiring facts, joined to the ask
                # machinery — which can be chased, which are
                # already asked, which lapsed unasked. An
                # annotation like the watch itself; the order
                # below never reads it.
                from . import chases as chases_mod

                entry.chases = chases_mod.desk_chases(
                    chases_mod.chases_for_account(
                        port=port,
                        account=account,
                        brief_store=brief_store,
                        follow_store=follow_store,
                        as_of=today,
                        horizon_days=horizon,
                    )
                )
        if follow_store is not None:
            # The ask riding on the account's latest DECIDED brief
            # (see followthrough.py). Attached whatever the row's
            # state — a stale decision and an open ask can both be
            # true of one account, and the queue shows both.
            decided_briefs = [
                b
                for b in briefs
                if b.status == "DECIDED" and b.decision is not None
            ]
            if decided_briefs:
                ask = next(
                    (
                        r
                        for r in follow_store.list(account_id)
                        if r.brief_id == decided_briefs[-1].brief_id
                    ),
                    None,
                )
                if ask is not None:
                    entry.follow_through = DeskFollowThrough(
                        record_id=ask.record_id,
                        brief_id=ask.brief_id,
                        status=ask.status,
                        received_count=sum(
                            1 for i in ask.items if i.status == "received"
                        ),
                        total_count=len(ask.items),
                    )
                    entry.ready_for_review = (
                        ask.status == "complete"
                        and latest is not None
                        and latest.brief_id == ask.brief_id
                        and latest.status == "DECIDED"
                    )
        if open_recs:
            record = open_recs[0]
            entry.state = "decided_stale"
            entry.decision = DeskDecision(
                action=record.decision_action,
                actor=record.decision_actor,
                decided_on=record.decided_at.date(),
            )
            entry.staleness = DeskStaleness(
                record_id=record.record_id,
                brief_id=record.brief_id,
                flagged_at=record.flagged_at,
                age_days=max(
                    0, (today - record.flagged_at.date()).days
                ),
                trigger_episode_ids=list(record.trigger_episode_ids),
                changes=record.changes,
            )
            stale_rows.append((record.decided_at, entry))
        elif latest is None:
            no_brief_rows.append(entry)
        elif latest.status == "DECIDED":
            entry.state = "decided"
            decided_rows.append((latest.decision.decided_at, entry))
        else:
            # AWAITING_UNDERWRITER — and VERIFICATION_FAILED, which
            # is also desk work (re-assemble); brief_status on the
            # row says which.
            entry.state = "awaiting"
            awaiting_rows.append((latest.created_at, entry))

    stale_rows.sort(key=lambda row: row[0])
    awaiting_rows.sort(key=lambda row: row[0])
    decided_rows.sort(key=lambda row: row[0], reverse=True)
    return (
        [entry for _, entry in stale_rows]
        + [entry for _, entry in awaiting_rows]
        + [entry for _, entry in decided_rows]
        + no_brief_rows
    )
