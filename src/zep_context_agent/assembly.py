"""Brief assembly: one account, resolved as of a date, in code.

Assembly is a read over the ContextPort plus the temporal rule —
no model touches it:

1. Read the account's facts, episodes, and edges (account-scoped
   by the port contract).
2. Resolve temporally (:mod:`temporal`): current facts, and the
   superseded facts with the successor that replaced each — or
   ``expired`` when none did.
3. Attach citations: every fact names its episode, and the episode
   names its document, so a line in the brief traces to paper.
4. Report what could not be resolved: the ontology's expected
   predicates with no current fact are listed as missing. The
   brief says "not in the file" instead of guessing.
5. Grade each current fact's support (see :mod:`support`): the
   tier of its citing document, the independent documents that
   corroborate it, whether an earlier value was displaced. The
   grade rides the fact; it decides nothing.
6. Fetch the provider's own context block and label it as such.
   It is context for the reader, never a citation source. If the
   provider cannot serve it, the brief assembles from cited facts
   alone and says so — on the Zep profile an unreachable Zep is a
   flagged degradation of the brief, not a silent swap of engines.

Verification (citations, leakage) is a separate step —
:mod:`guardrails` over the assembled brief, run by the pipeline's
verify node — so assembly stays a pure function of the port.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from .errors import ContextUnavailable
from .ontology import EXPECTED_PREDICATES, Account, Fact
from .ports import ContextPort
from .schemas import (
    AuditEntry,
    Brief,
    CitedFact,
    FactSupport,
    QuarantineNote,
    SupersededFact,
)
from .support import assess_support
from .temporal import resolve


def _now() -> datetime:
    return datetime.now(timezone.utc)


def assemble_brief(
    port: ContextPort,
    account: Account,
    as_of: date,
    *,
    brief_id: str | None = None,
) -> Brief:
    account_id = account.account_id
    facts = port.get_facts(account_id)
    episodes = port.get_episodes(account_id)
    edges = port.get_edges(account_id)
    episode_by_id = {e.episode_id: e for e in episodes}
    doc_titles = {d.doc_id: d.title for d in account.documents}

    def cite(fact: Fact, support: FactSupport | None = None) -> CitedFact:
        episode = episode_by_id.get(fact.episode_id)
        return CitedFact(
            predicate=fact.predicate,
            value=fact.value,
            unit=fact.unit,
            entity_ref=fact.entity_ref,
            valid_from=fact.valid_from,
            valid_to=fact.valid_to,
            episode_id=fact.episode_id,
            doc_id=fact.doc_id,
            doc_title=doc_titles.get(fact.doc_id, episode.title if episode else ""),
            recorded_at=fact.recorded_at,
            support=support,
        )

    current, superseded = resolve(facts, as_of)
    supports = assess_support(facts, as_of, doc_titles)
    brief = Brief(
        brief_id=brief_id or f"brief-{uuid.uuid4().hex[:12]}",
        account_id=account_id,
        account_name=account.display_name,
        as_of=as_of,
        backend=getattr(port, "name", "unknown"),
        current_facts=[cite(f, supports.get(f.fact_id)) for f in current],
        superseded_facts=[
            SupersededFact(
                **cite(old).model_dump(exclude={"support"}),
                successor_value=successor.value if successor else None,
                successor_doc_id=successor.doc_id if successor else "",
                reason="replaced" if successor else "expired",
            )
            for old, successor in superseded
        ],
        missing_predicates=[
            p
            for p in EXPECTED_PREDICATES
            if p not in {f.predicate for f in current}
        ],
        edges=edges,
        sources=sorted(episodes, key=lambda e: (e.received_at, e.episode_id)),
        quarantined=[
            QuarantineNote(
                episode_id=e.episode_id,
                doc_id=e.doc_id,
                doc_title=e.title,
                reason=e.quarantine_reason,
            )
            for e in episodes
            if e.quarantined
        ],
        created_at=_now(),
    )
    brief.flags.extend(
        f"Quarantined source excluded: {q.doc_title} — {q.reason}"
        for q in brief.quarantined
    )
    if brief.missing_predicates:
        brief.flags.append(
            "Not resolved from the file: "
            + ", ".join(brief.missing_predicates)
            + "."
        )
    try:
        block = port.get_context_block(account_id, as_of)
    except ContextUnavailable as exc:
        block = None
        brief.context_block_source = "unavailable"
        brief.flags.append(
            f"Provider context unavailable ({exc}); the brief is "
            "assembled from cited facts alone."
        )
    if block is not None:
        brief.context_block = block
        brief.context_block_source = brief.backend
    brief.audit.append(
        AuditEntry(
            at=_now(),
            event="assembled",
            detail=(
                f"{len(brief.current_facts)} current fact(s), "
                f"{len(brief.superseded_facts)} superseded, "
                f"{sum(1 for s in supports.values() if s.grade == 'THIN')} "
                f"grading thin, backend={brief.backend}"
            ),
        )
    )
    return brief
