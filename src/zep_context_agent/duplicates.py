"""The duplicate watch: the same insured, twice.

One insured placed through two submissions becomes two
accounts, and every exposure view the desk reads counts it
twice. Until this module, nothing in the system ever looked
across accounts at all — the graph's scoping is contractual
(every port call names exactly one account, and the leakage
guardrail double-checks it), so each of the forty accounts
stood alone and no identity check existed. This module is
that check, built under the same discipline the scoping
demands: it reads each account's own facts through the port,
one account at a time, and compares the *values* in code.
Nothing crosses into a brief; the only cross-account artifact
is the pair itself — two ids, the evidence, and the verdict.

- **Detection, in code, from current facts only.** A pair is
  a candidate when two things hold of the accounts' CURRENT
  facts (each side resolved by the temporal rule as of the
  watch's date — the account's legal name, primary location,
  risk class, and annual revenue — plus its broker of record,
  read off its own paper by :func:`brokers.broker_of_record`,
  the one identity reader):

  1. **Name similarity** at or over
     :data:`NAME_SIMILARITY_THRESHOLD`. The rule is stated,
     deterministic, and printed on the watch's face: both
     legal names are normalised (lowercased, punctuation
     removed, legal-form tokens — Inc, LLC, Corp, Co,
     Company, Ltd — dropped) and compared with the
     character-sequence ratio (``difflib.SequenceMatcher``).
     The threshold lives here and nowhere else.
  2. **At least one corroborating signal.** A bare name
     similarity NEVER flags alone — near-identical names are
     also how distinct insureds look, which is why they are
     traps. Corroboration is one STRONG signal (the same
     primary location, exactly; the same broker of record; or
     the same risk class with annual revenue within
     :data:`REVENUE_NEAR_FRACTION` of each other) or two WEAK
     ones (the same state, parsed from the location; the
     same risk-class line — the line of business before the
     class's own detail). Every flag carries its evidence:
     both sides' values, the similarity figure, and each
     corroborator that fired, with its detail.

  Pairs are computed, never stored judgements: detection
  re-derives from the facts on every read, in the projection
  idiom of the chases and the broker book. An account with
  no current legal name cannot pair — there is nothing to
  compare — and is simply absent.

- **The desk's word.** For a flagged pair, a named
  underwriter records a verdict: ``SAME_INSURED`` (with a
  note) or ``DISTINCT`` (with the distinguishing fact
  stated). Verdicts are append-only records in their own
  store (migration 0007 — durable governed records, like
  the staleness and follow-through records; the in-memory
  double is the test double, as everywhere here). The
  pair's standing is derived from the LATEST verdict:

  - ``DISTINCT`` suppresses the pair — while the evidence
    stands. The verdict carries the pair's evidence
    fingerprint (a SHA-256 over exactly what the
    underwriter was shown: both names, the similarity, both
    locations, risk classes, revenues, brokers, and the
    corroborators). If the underlying facts change
    materially, the fingerprint moves, the suppression no
    longer matches the evidence, and the pair re-flags as a
    NEW flag (``reflagged``) — the watermark idiom: the
    verdict answered the evidence it saw, not whatever the
    file becomes later.
  - ``SAME_INSURED`` answers an identity question, not an
    evidence one, so it carries no fingerprint condition:
    it stands while the pair still flags. And it changes
    NOTHING operational — no merge, no fact movement, no
    brief changes. It marks the pair for the exposure
    reading below and nothing else. The desk merges
    accounts in their system of record, not here; this
    system reports the double-count, it does not repair it.

- **The exposure reading.** The portfolio brief annotates
  the confirmed pairs (see :mod:`portfolio`): "N accounts
  in M confirmed duplicate pairs", the pairs named. The
  book's totals by account are unchanged — exposure
  double-count is reported, not silently corrected — and
  the published weights, scores, and order are untouched:
  the annotation rides the brief, never a row. Unverdicted
  flags do NOT annotate: an unproven suspicion does not
  print as fact.

- **The desk queue does not change.** Pair work is book
  hygiene, not account urgency: a possible duplicate does
  not make either account's decision staler, its asks
  bigger, or its facts thinner, so the queue's order and
  rows are exactly as they were. The watch is its own
  surface.

There is deliberately no ingestion hook: nothing is
evaluated when documents land. The watch is read-time
computation over current facts, so a fact change shows up
the next time the watch is read — which is exactly the
re-flag the fingerprint rule describes.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
from itertools import combinations

from . import brokers as brokers_mod
from . import temporal
from .errors import DuplicateError
from .ontology import Account
from .ports import ContextPort
from .schemas import (
    ConfirmedDuplicatePair,
    DuplicateAnnotation,
    DuplicateCorroborator,
    DuplicatePair,
    DuplicateSide,
    DuplicateVerdictRecord,
    DuplicateWatch,
)

#: The name-similarity threshold — the one place it lives.
#: Two normalised legal names must agree at this character-
#: sequence ratio or better before a pair can flag at all;
#: corroboration is still required on top of it.
NAME_SIMILARITY_THRESHOLD = 0.90

#: How near two annual revenues must be, as a fraction of
#: the larger, for the same-risk-class corroborator: within
#: ten percent reads as the same insured's revenue stated
#: twice; further apart is two different books of business.
REVENUE_NEAR_FRACTION = 0.10

#: Legal-form tokens dropped in name normalisation: the
#: suffix is how a filing is styled, not who the insured is.
LEGAL_FORM_TOKENS = frozenset(
    {
        "inc",
        "llc",
        "corp",
        "corporation",
        "co",
        "company",
        "ltd",
        "lp",
        "llp",
        "plc",
        "the",
    }
)

#: The rule, printed on the watch's face — the desk can read
#: exactly what flags and argue with it in the open.
RULE_TEXT = (
    "A pair flags when the two accounts' current legal names, "
    "normalised (lowercased, punctuation removed, legal-form "
    "tokens dropped), reach a character-sequence similarity of "
    f"{NAME_SIMILARITY_THRESHOLD:.2f} or better AND at least one "
    "corroborating signal holds: a strong one (the same primary "
    "location; the same broker of record; the same risk class "
    "with annual revenue within "
    f"{REVENUE_NEAR_FRACTION:.0%}) or two weak ones (the same "
    "state; the same risk-class line). A bare name similarity "
    "never flags alone. Pairs are ordered by similarity, "
    "highest first, then by account id."
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Name similarity — the stated rule
# ---------------------------------------------------------------------------


def normalise_name(name: str) -> str:
    """A legal name, normalised for comparison: lowercased,
    punctuation turned to spaces, legal-form tokens dropped,
    whitespace collapsed. "Meridian Casting Co." and
    "Meridian Castings LLC" normalise to "meridian casting"
    and "meridian castings" — the styling is gone, the
    insured's name remains."""
    tokens = [
        token
        for token in re.split(r"[^a-z0-9]+", name.lower())
        if token and token not in LEGAL_FORM_TOKENS
    ]
    return " ".join(tokens)


def name_similarity(name_a: str, name_b: str) -> float:
    """The character-sequence ratio of the two normalised
    names (``difflib.SequenceMatcher``): 1.0 when the
    normalised names are identical, 0.0 when either side
    normalises to nothing."""
    a, b = normalise_name(name_a), normalise_name(name_b)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


# ---------------------------------------------------------------------------
# Evidence — one account's own current facts
# ---------------------------------------------------------------------------


def _side(port: ContextPort, account: Account, as_of: date) -> DuplicateSide | None:
    """The account's identity evidence: its current facts,
    resolved by the temporal rule as of ``as_of``, and its
    broker of record. ``None`` when the account has no
    current legal name — there is nothing to compare, and
    the account simply cannot pair."""
    current, _ = temporal.resolve(port.get_facts(account.account_id), as_of)
    values: dict[str, object] = {}
    for fact in current:
        values.setdefault(fact.predicate, fact.value)
    legal_name = values.get("legal_name")
    if not isinstance(legal_name, str) or not legal_name.strip():
        return None
    revenue = values.get("annual_revenue_usd")
    if isinstance(revenue, bool) or not isinstance(revenue, (int, float)):
        revenue = None
    broker_id, broker_name = brokers_mod.broker_of_record(account)
    location = values.get("primary_location")
    risk_class = values.get("risk_class")
    return DuplicateSide(
        account_id=account.account_id,
        account_name=account.display_name,
        legal_name=legal_name,
        primary_location=location if isinstance(location, str) else "",
        risk_class=risk_class if isinstance(risk_class, str) else "",
        annual_revenue_usd=float(revenue) if revenue is not None else None,
        broker_id=broker_id,
        broker_name=broker_name,
    )


_STATE_RE = re.compile(r",\s*([A-Za-z]{2})\s*$")


def _state_of(location: str) -> str:
    """The state code a location ends in ("…, Pittsburgh, PA"
    → "PA"), or "" when the location does not carry one."""
    match = _STATE_RE.search(location or "")
    return match.group(1).upper() if match else ""


def _risk_line(risk_class: str) -> str:
    """The line of business a risk class sits under: the text
    before the class's own detail ("Product liability —
    consumer goods" → "Product liability"). A class with no
    detail separator is its own line."""
    return risk_class.split(" — ")[0].strip()


def _revenue_near(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return False
    larger = max(a, b)
    if larger <= 0:
        return a == b
    return abs(a - b) <= REVENUE_NEAR_FRACTION * larger


def _money(value: float) -> str:
    return f"{value:,.0f}"


def _corroborators(
    a: DuplicateSide, b: DuplicateSide
) -> list[DuplicateCorroborator]:
    """Every corroborating signal that fires between the two
    sides, strong first. The flag rule reads this list; the
    underwriter reads its details."""
    found: list[DuplicateCorroborator] = []
    if a.primary_location and a.primary_location == b.primary_location:
        found.append(
            DuplicateCorroborator(
                kind="same_location",
                strength="strong",
                detail=f"same primary location: {a.primary_location}",
            )
        )
    if a.broker_id is not None and a.broker_id == b.broker_id:
        found.append(
            DuplicateCorroborator(
                kind="same_broker",
                strength="strong",
                detail=(
                    f"same broker of record: {a.broker_name} "
                    f"({a.broker_id})"
                ),
            )
        )
    if (
        a.risk_class
        and a.risk_class == b.risk_class
        and _revenue_near(a.annual_revenue_usd, b.annual_revenue_usd)
    ):
        found.append(
            DuplicateCorroborator(
                kind="same_risk_class_close_revenue",
                strength="strong",
                detail=(
                    f"same risk class ({a.risk_class}) with annual "
                    f"revenue {_money(a.annual_revenue_usd)} vs "
                    f"{_money(b.annual_revenue_usd)} USD — within "
                    f"{REVENUE_NEAR_FRACTION:.0%}"
                ),
            )
        )
    state_a, state_b = _state_of(a.primary_location), _state_of(b.primary_location)
    if state_a and state_a == state_b:
        found.append(
            DuplicateCorroborator(
                kind="same_state",
                strength="weak",
                detail=f"same state: {state_a}",
            )
        )
    line_a, line_b = _risk_line(a.risk_class), _risk_line(b.risk_class)
    if line_a and line_a == line_b:
        found.append(
            DuplicateCorroborator(
                kind="same_risk_line",
                strength="weak",
                detail=f"same risk-class line: {line_a}",
            )
        )
    return found


def _corroborated(corroborators: list[DuplicateCorroborator]) -> bool:
    """The corroboration rule: one strong signal, or two
    weak ones. Nothing less flags, however similar the
    names."""
    strong = sum(1 for c in corroborators if c.strength == "strong")
    weak = sum(1 for c in corroborators if c.strength == "weak")
    return strong >= 1 or weak >= 2


def evidence_fingerprint(
    a: DuplicateSide,
    b: DuplicateSide,
    similarity: float,
    corroborators: list[DuplicateCorroborator],
) -> str:
    """A SHA-256 over exactly the evidence a flag stands on:
    both accounts' identity values, the similarity figure,
    and the corroborators that fired. Any material change
    to the underlying facts moves the fingerprint — which
    is what a DISTINCT verdict's suppression is keyed to."""
    payload = {
        "accounts": [a.account_id, b.account_id],
        "legal_names": {
            a.account_id: a.legal_name,
            b.account_id: b.legal_name,
        },
        "similarity": round(similarity, 4),
        "locations": {
            a.account_id: a.primary_location,
            b.account_id: b.primary_location,
        },
        "risk_classes": {
            a.account_id: a.risk_class,
            b.account_id: b.risk_class,
        },
        "revenues": {
            a.account_id: a.annual_revenue_usd,
            b.account_id: b.annual_revenue_usd,
        },
        "brokers": {a.account_id: a.broker_id, b.account_id: b.broker_id},
        "corroborators": sorted(c.kind for c in corroborators),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()


# ---------------------------------------------------------------------------
# Detection — computed pairs, re-derived on every read
# ---------------------------------------------------------------------------


def detect_pairs(
    *,
    port: ContextPort,
    accounts: dict[str, Account],
    as_of: date,
) -> list[DuplicatePair]:
    """Every candidate pair on the book, from current facts
    only: name similarity at or over the threshold AND
    corroboration. Verdicts are NOT applied here — this is
    the raw detection the watch joins verdicts onto, and
    the list a verdict is recorded against. Ordered by
    similarity, highest first, then by account id."""
    sides: dict[str, DuplicateSide] = {}
    for account_id in sorted(accounts):
        side = _side(port, accounts[account_id], as_of)
        if side is not None:
            sides[account_id] = side
    pairs: list[DuplicatePair] = []
    for id_a, id_b in combinations(sorted(sides), 2):
        a, b = sides[id_a], sides[id_b]
        similarity = name_similarity(a.legal_name, b.legal_name)
        if similarity < NAME_SIMILARITY_THRESHOLD:
            continue
        corroborators = _corroborators(a, b)
        if not _corroborated(corroborators):
            continue
        pairs.append(
            DuplicatePair(
                account_a=id_a,
                account_b=id_b,
                name_similarity=round(similarity, 4),
                threshold=NAME_SIMILARITY_THRESHOLD,
                corroborators=corroborators,
                sides=[a, b],
                fingerprint=evidence_fingerprint(
                    a, b, similarity, corroborators
                ),
                state="FLAGGED",
            )
        )
    pairs.sort(
        key=lambda p: (-p.name_similarity, p.account_a, p.account_b)
    )
    return pairs


# ---------------------------------------------------------------------------
# The verdict store
# ---------------------------------------------------------------------------


class InMemoryDuplicateVerdictStore:
    """Dict-backed verdict store (the test double)."""

    def __init__(self) -> None:
        self._records: dict[str, DuplicateVerdictRecord] = {}
        self._order: list[str] = []

    def save(self, record: DuplicateVerdictRecord) -> DuplicateVerdictRecord:
        if record.record_id not in self._records:
            self._order.append(record.record_id)
        self._records[record.record_id] = record
        return record

    def get(self, record_id: str) -> DuplicateVerdictRecord | None:
        return self._records.get(record_id)

    def list(
        self, pair: tuple[str, str] | None = None
    ) -> list[DuplicateVerdictRecord]:
        records = [self._records[rid] for rid in self._order]
        if pair is not None:
            records = [
                r
                for r in records
                if (r.account_a, r.account_b) == pair
            ]
        return records


class PostgresDuplicateVerdictStore:
    """Verdict store over the ``duplicate_verdicts`` table."""

    def __init__(self, url: str) -> None:
        from .db import ensure_migrated

        ensure_migrated(url)
        self._url = url
        self._lock = threading.Lock()

    def _connect(self):
        from .db import connect

        return connect(self._url)

    def save(self, record: DuplicateVerdictRecord) -> DuplicateVerdictRecord:
        payload = json.dumps(record.model_dump(mode="json"))
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO duplicate_verdicts "
                "(record_id, account_a, account_b, verdict, payload) "
                "VALUES (%s, %s, %s, %s, %s::jsonb) "
                "ON CONFLICT (record_id) DO UPDATE SET "
                "payload = EXCLUDED.payload, updated_at = now()",
                (
                    record.record_id,
                    record.account_a,
                    record.account_b,
                    record.verdict,
                    payload,
                ),
            )
            conn.commit()
        return record

    def get(self, record_id: str) -> DuplicateVerdictRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM duplicate_verdicts "
                "WHERE record_id = %s",
                (record_id,),
            ).fetchone()
        if row is None:
            return None
        payload = row[0]
        if isinstance(payload, str):
            payload = json.loads(payload)
        return DuplicateVerdictRecord.model_validate(payload)

    def list(
        self, pair: tuple[str, str] | None = None
    ) -> list[DuplicateVerdictRecord]:
        query = "SELECT payload FROM duplicate_verdicts"
        params: tuple = ()
        if pair is not None:
            query += " WHERE account_a = %s AND account_b = %s"
            params = pair
        query += " ORDER BY seq"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        records = []
        for (payload,) in rows:
            if isinstance(payload, str):
                payload = json.loads(payload)
            records.append(DuplicateVerdictRecord.model_validate(payload))
        return records


def default_duplicate_verdict_store():
    """The configured verdict store: Postgres when
    ``DATABASE_URL`` is set, the in-memory test double
    otherwise — the staleness store's split, for the same
    reasons."""
    from .db import database_url

    url = database_url()
    if url:
        return PostgresDuplicateVerdictStore(url)
    return InMemoryDuplicateVerdictStore()


def latest_verdicts(store) -> dict[tuple[str, str], DuplicateVerdictRecord]:
    """The latest verdict per pair, over the store's whole
    history: verdicts are append-only, and the pair's
    standing is its most recent word."""
    latest: dict[tuple[str, str], DuplicateVerdictRecord] = {}
    for record in store.list():
        latest[(record.account_a, record.account_b)] = record
    return latest


# ---------------------------------------------------------------------------
# The watch — detection joined to the desk's word
# ---------------------------------------------------------------------------


def build_duplicate_watch(
    *,
    port: ContextPort,
    accounts: dict[str, Account],
    verdict_store,
    as_of: date,
) -> DuplicateWatch:
    """The duplicate watch as of ``as_of``: every detected
    pair, with its standing derived from the verdict store.
    A pure projection — building it writes nothing."""
    latest = latest_verdicts(verdict_store)
    pairs = []
    for pair in detect_pairs(port=port, accounts=accounts, as_of=as_of):
        record = latest.get((pair.account_a, pair.account_b))
        if record is None:
            pairs.append(pair)
        elif record.verdict == "SAME_INSURED":
            pairs.append(
                pair.model_copy(
                    update={"state": "CONFIRMED_SAME", "verdict": record}
                )
            )
        elif record.fingerprint == pair.fingerprint:
            pairs.append(
                pair.model_copy(
                    update={"state": "DISTINCT", "verdict": record}
                )
            )
        else:
            # The desk said DISTINCT — of other facts. The
            # evidence moved materially since the verdict, so
            # the pair stands as a new flag, the old verdict
            # riding it as history.
            pairs.append(
                pair.model_copy(
                    update={
                        "state": "FLAGGED",
                        "reflagged": True,
                        "verdict": record,
                    }
                )
            )
    return DuplicateWatch(
        as_of=as_of,
        threshold=NAME_SIMILARITY_THRESHOLD,
        rule=RULE_TEXT,
        pairs=pairs,
    )


def record_verdict(
    *,
    port: ContextPort,
    accounts: dict[str, Account],
    verdict_store,
    account_a: str,
    account_b: str,
    verdict: str,
    actor: str,
    note: str,
    as_of: date,
) -> DuplicateVerdictRecord:
    """Record a named underwriter's verdict on a flagged
    pair. The pair must flag on CURRENT facts — a verdict
    answers a live flag, not a memory of one — and the
    record carries the pair's current evidence fingerprint.
    Refusals raise :class:`DuplicateError` naming the cause
    and record nothing. The verdict itself moves no fact,
    edits no brief, and merges nothing: it is the desk's
    word on the pair, stored as its own record."""
    if verdict not in ("SAME_INSURED", "DISTINCT"):
        raise DuplicateError(
            f"Verdict must be SAME_INSURED or DISTINCT, got {verdict!r}."
        )
    if not actor or not actor.strip():
        raise DuplicateError("A verdict needs the underwriter's name.")
    if not note or not note.strip():
        raise DuplicateError(
            "A verdict needs its note — for DISTINCT, the "
            "distinguishing fact; for SAME_INSURED, the note "
            "the desk will merge on."
        )
    if account_a == account_b:
        raise DuplicateError(
            f"An account cannot be its own duplicate ({account_a})."
        )
    for account_id in (account_a, account_b):
        if account_id not in accounts:
            raise DuplicateError(f"No account {account_id!r} in the dataset.")
    pair_id = tuple(sorted((account_a, account_b)))
    pair = next(
        (
            p
            for p in detect_pairs(port=port, accounts=accounts, as_of=as_of)
            if (p.account_a, p.account_b) == pair_id
        ),
        None,
    )
    if pair is None:
        raise DuplicateError(
            f"{pair_id[0]} and {pair_id[1]} are not a flagged pair on "
            "current facts — the watch flags a pair only when the "
            "normalised legal names reach the similarity threshold "
            "with a corroborating signal. There is nothing to "
            "verdict."
        )
    record = DuplicateVerdictRecord(
        record_id=f"dup-{uuid.uuid4().hex[:12]}",
        account_a=pair_id[0],
        account_b=pair_id[1],
        verdict=verdict,  # type: ignore[arg-type]
        actor=actor,
        note=note,
        fingerprint=pair.fingerprint,
        recorded_at=_now(),
    )
    verdict_store.save(record)
    return record


def confirmed_annotation(
    *,
    port: ContextPort,
    accounts: dict[str, Account],
    verdict_store,
    as_of: date,
) -> DuplicateAnnotation:
    """The portfolio brief's annotation: the pairs standing
    CONFIRMED_SAME on the watch, counted and named. Only a
    SAME_INSURED verdict puts a pair here — an unverdicted
    flag, however strong its evidence, is a suspicion, and
    a suspicion does not print as fact."""
    watch = build_duplicate_watch(
        port=port, accounts=accounts, verdict_store=verdict_store, as_of=as_of
    )
    confirmed = [p for p in watch.pairs if p.state == "CONFIRMED_SAME"]
    account_ids: set[str] = set()
    named = []
    for pair in confirmed:
        account_ids.update((pair.account_a, pair.account_b))
        named.append(
            ConfirmedDuplicatePair(
                account_a=pair.account_a,
                account_b=pair.account_b,
                account_a_name=accounts[pair.account_a].display_name,
                account_b_name=accounts[pair.account_b].display_name,
                record_id=pair.verdict.record_id if pair.verdict else "",
                actor=pair.verdict.actor if pair.verdict else "",
            )
        )
    return DuplicateAnnotation(
        pair_count=len(named),
        account_count=len(account_ids),
        pairs=named,
    )


# ---------------------------------------------------------------------------
# Text rendering (the CLI's shape; the portfolio precedent)
# ---------------------------------------------------------------------------


def _side_lines(side: DuplicateSide) -> list[str]:
    revenue = (
        f"{_money(side.annual_revenue_usd)} USD"
        if side.annual_revenue_usd is not None
        else "not on file"
    )
    broker = (
        f"{side.broker_name} ({side.broker_id})"
        if side.broker_id
        else side.broker_name
    )
    return [
        f"    {side.account_id}  {side.account_name}",
        f"      legal name: {side.legal_name}",
        f"      location:   {side.primary_location or 'not on file'}",
        f"      risk class: {side.risk_class or 'not on file'}",
        f"      revenue:    {revenue}",
        f"      broker:     {broker}",
    ]


def render_duplicate_watch_text(watch: DuplicateWatch) -> str:
    lines = [
        f"DUPLICATE WATCH — {len(watch.pairs)} candidate pair(s) "
        f"as of {watch.as_of}",
        f"Rule: {watch.rule}",
        "A verdict is the desk's word, recorded on its own record. "
        "Nothing here merges accounts or moves a fact — the desk "
        "merges in their system of record; this watch reports.",
        "",
    ]
    if not watch.pairs:
        lines.append("No candidate pairs on current facts.")
        return "\n".join(lines) + "\n"
    for pair in watch.pairs:
        state = pair.state
        if pair.reflagged:
            state += " (re-flagged: the facts changed since the DISTINCT verdict)"
        lines.append(
            f"{pair.account_a} × {pair.account_b} — {state} — "
            f"name similarity {pair.name_similarity:.2f} "
            f"(threshold {pair.threshold:.2f})"
        )
        for side in pair.sides:
            lines.extend(_side_lines(side))
        for corroborator in pair.corroborators:
            lines.append(
                f"    corroborator [{corroborator.strength}]: "
                f"{corroborator.detail}"
            )
        if pair.verdict is not None:
            lines.append(
                f"    verdict: {pair.verdict.verdict} by "
                f"{pair.verdict.actor} — {pair.verdict.note}"
            )
        lines.append("")
    return "\n".join(lines) + "\n"
