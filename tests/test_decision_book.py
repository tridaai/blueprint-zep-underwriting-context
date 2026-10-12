"""The decision book: a period's governed acts, in one document.

Pins:

- composition — every surveyed source contributes its acts:
  gate decisions (renewals marked, baselines named), staleness
  raised/resolved, asks opened/received/completed, renewal
  chases sent (never double-counted as ask openings),
  review-gate confirmations/rejections. Each act appears
  exactly once, in time order, with the counts matching the
  stores' own reads;
- the window — both bounds inclusive, moments read in UTC; an
  act one day outside is absent;
- integrity — the same stores compose to the same bytes and
  the same digest; composing writes nothing (the stores are
  byte-identical across a build), and decided briefs stay
  byte-identical;
- honesty — an empty window composes to an empty book with a
  digest, not an error; a reversed period is refused by name;
  a record claiming a completed act with no moment stands in
  the undated section, never dropped, never dated by
  invention.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient

from zep_context_agent.api import create_app
from zep_context_agent.decisionbook import build_decision_book
from zep_context_agent.errors import DecisionBookError
from zep_context_agent.followthrough import (
    InMemoryFollowThroughStore,
)
from zep_context_agent.ingestion import ingest_document
from zep_context_agent.offline_graph import OfflineContextGraph
from zep_context_agent.ontology import Account, FactAssertion, SourceDocument
from zep_context_agent.schemas import (
    Brief,
    BriefDiff,
    Decision,
    DiffSuperseded,
    FollowThroughItem,
    FollowThroughRecord,
    RenewalBaseline,
    RenewalInherited,
    RenewalLedger,
    RenewalSection,
    StalenessRecord,
)
from zep_context_agent.staleness import InMemoryStalenessStore
from zep_context_agent.offline_graph import OfflineContextGraph
from zep_context_agent.service import BriefService
from zep_context_agent.store import InMemoryBriefStore


def make_service(accounts, **overrides) -> BriefService:
    from langgraph.checkpoint.memory import MemorySaver

    kwargs = dict(
        port=OfflineContextGraph(),
        store=InMemoryBriefStore(),
        object_store=None,
        checkpointer=MemorySaver(),
        cache=None,
        accounts=accounts,
    )
    kwargs.update(overrides)
    return BriefService(**kwargs)

START = date(2026, 6, 1)
END = date(2026, 6, 14)


def _at(day: int, hour: int = 9) -> datetime:
    return datetime(2026, 6, day, hour, tzinfo=timezone.utc)


def _brief(brief_id, account_id, name, **updates) -> Brief:
    brief = Brief(
        brief_id=brief_id,
        account_id=account_id,
        account_name=name,
        as_of=date(2026, 6, 1),
        backend="offline",
        created_at=_at(1, 8),
    )
    return brief.model_copy(update=updates) if updates else brief


def _decided(brief_id, account_id, name, at, action="proceed", **updates):
    return _brief(
        brief_id,
        account_id,
        name,
        status="DECIDED",
        decision=Decision(
            action=action,
            actor="Senior Underwriter",
            reason="The file supports it.",
            decided_at=at,
        ),
        **updates,
    )


def _accounts() -> dict[str, Account]:
    return {
        "ACC-DB1": Account(
            account_id="ACC-DB1", display_name="Decision Book One Co", documents=[]
        ),
        "ACC-DB2": Account(
            account_id="ACC-DB2", display_name="Decision Book Two Co", documents=[]
        ),
    }


def _staged_service():
    """A service whose stores hold a fortnight of mixed acts,
    staged at their recorded moments."""
    accounts = _accounts()
    service = make_service(accounts)

    # Gate decisions: one plain, one renewal, one outside the window.
    service.store.save(
        _decided("brief-db1-a", "ACC-DB1", "Decision Book One Co", _at(3))
    )
    baseline = RenewalBaseline(
        brief_id="brief-db1-a",
        as_of=date(2026, 6, 1),
        action="proceed",
        actor="Senior Underwriter",
        reason="The file supports it.",
        decided_at=_at(3),
    )
    service.store.save(
        _decided(
            "brief-db1-b",
            "ACC-DB1",
            "Decision Book One Co",
            _at(10),
            renewal=RenewalSection(
                baseline=baseline,
                ledger=RenewalLedger(),
                inherited=RenewalInherited(),
            ),
        )
    )
    service.store.save(
        _decided("brief-db2-old", "ACC-DB2", "Decision Book Two Co",
                 datetime(2026, 5, 31, 9, tzinfo=timezone.utc))
    )
    service.store.save(_brief("brief-db2-open", "ACC-DB2", "Decision Book Two Co"))

    # Staleness: raised on the 4th, resolved on the 11th.
    changes = BriefDiff(
        previous_brief_id="brief-db1-a",
        previous_as_of=date(2026, 6, 1),
        superseded=[
            DiffSuperseded(
                predicate="policy_limit_usd",
                unit="USD",
                previous_value=1000000,
                current_value=750000,
            )
        ],
    )
    service.staleness_store.save(
        StalenessRecord(
            record_id="stale-db1",
            account_id="ACC-DB1",
            brief_id="brief-db1-a",
            decision_action="proceed",
            decision_actor="Senior Underwriter",
            decided_at=_at(3),
            flagged_at=_at(4),
            trigger_episode_ids=["ep-db1-end"],
            changes=changes,
            resolved_by_brief="brief-db1-b",
            resolved_at=_at(11),
            resolved_by="Senior Underwriter",
        )
    )

    # Follow-through: a decision ask (opened 3rd, item received
    # 6th, complete 6th) and a renewal chase sent on the 5th.
    service.follow_store.save(
        FollowThroughRecord(
            record_id="ask-db1",
            account_id="ACC-DB1",
            brief_id="brief-db1-a",
            origin="decision",
            decision_actor="Senior Underwriter",
            decision_reason="Claims history, please.",
            decided_at=_at(3),
            created_at=_at(3, 10),
            items=[
                FollowThroughItem(
                    kind="predicate",
                    predicate="claims_count_3yr",
                    label="Claims history (3 years)",
                    status="received",
                    received_episode_id="ep-db1-clm",
                    received_at=_at(6),
                )
            ],
            status="complete",
            completed_at=_at(6, 11),
            updated_at=_at(6, 11),
        )
    )
    service.follow_store.save(
        FollowThroughRecord(
            record_id="ask-db2-chase",
            account_id="ACC-DB2",
            brief_id="brief-db2-open",
            origin="renewal_chase",
            decision_actor="Desk Lead",
            decision_reason="The inspection grade is expiring.",
            decided_at=_at(5),
            created_at=_at(5),
            items=[
                FollowThroughItem(
                    kind="renewal",
                    predicate="inspection_grade",
                    label="Inspection grade",
                    expires_on=date(2026, 6, 20),
                )
            ],
            status="open",
            updated_at=_at(5),
        )
    )

    # Review gate: one confirmation (8th), one rejection (9th),
    # staged through the port at their recorded moments.
    letter = SourceDocument(
        doc_id="DOC-DB2-COR",
        account_id="ACC-DB2",
        kind="correspondence",
        title="Broker letter — revenue and location",
        received_at=date(2026, 6, 2),
        assertions=[
            FactAssertion(
                predicate="annual_revenue_usd",
                value=5400000,
                unit="USD",
                valid_from=date(2026, 1, 1),
            ),
            FactAssertion(
                predicate="primary_location",
                value="9 Elsewhere Road",
                valid_from=date(2026, 1, 1),
            ),
        ],
    )
    ingest_document(service.port, letter)
    candidates = service.port.get_candidates("ACC-DB2")
    assert len(candidates) == 2
    by_predicate = {c.predicate: c for c in candidates}
    service.port.confirm_candidate(
        "ACC-DB2",
        by_predicate["annual_revenue_usd"].candidate_id,
        actor="Senior Underwriter",
        at=_at(8),
    )
    service.port.reject_candidate(
        "ACC-DB2",
        by_predicate["primary_location"].candidate_id,
        actor="Senior Underwriter",
        reason="Contradicts the filed submission.",
        at=_at(9),
    )
    return service


def _snapshot(service) -> dict:
    return {
        "briefs": [b.model_dump_json() for b in service.store.list()],
        "staleness": [
            r.model_dump_json() for r in service.staleness_store.list()
        ],
        "follow": [r.model_dump_json() for r in service.follow_store.list()],
        "candidates": {
            aid: [c.model_dump_json() for c in service.port.get_candidates(aid)]
            for aid in ("ACC-DB1", "ACC-DB2")
        },
    }


def test_book_composes_every_act_once_in_order():
    service = _staged_service()
    book = service.decision_book(START, END)
    kinds = [e.kind for e in book.entries]
    assert kinds == [
        "decision",  # 3rd, the plain decision
        "ask_opened",  # 3rd, its ask
        "staleness_raised",  # 4th
        "chase_sent",  # 5th
        "ask_received",  # 6th
        "ask_completed",  # 6th, after the receipt that completed it
        "review_confirmed",  # 8th
        "review_rejected",  # 9th
        "decision",  # 10th, the renewal decision
        "staleness_resolved",  # 11th
    ]
    assert book.total == 10
    assert book.counts == {
        "decision": 2,
        "review_confirmed": 1,
        "review_rejected": 1,
        "staleness_raised": 1,
        "staleness_resolved": 1,
        "ask_opened": 1,
        "chase_sent": 1,
        "ask_received": 1,
        "ask_completed": 1,
    }
    # The chase is its own kind — never also an ask opening.
    assert [e.record_id for e in book.entries if e.kind == "chase_sent"] == [
        "ask-db2-chase"
    ]
    # The renewal decision is marked, with its baseline named.
    renewal = book.entries[8]
    assert renewal.renewal is True
    assert renewal.baseline_brief_id == "brief-db1-a"
    assert renewal.brief_id == "brief-db1-b"
    # The decision one day before the window is absent.
    assert all(e.brief_id != "brief-db2-old" for e in book.entries)
    # Every entry carries its own account only.
    for entry in book.entries:
        assert entry.account_id in ("ACC-DB1", "ACC-DB2")
        other = "ACC-DB2" if entry.account_id == "ACC-DB1" else "ACC-DB1"
        assert other not in entry.summary
    assert book.undated == []
    assert len(book.digest) == 64
    assert book.sources  # the coverage statement's source list


def test_window_bounds_are_inclusive():
    service = _staged_service()
    service.store.save(
        _decided(
            "brief-db2-edge",
            "ACC-DB2",
            "Decision Book Two Co",
            datetime(2026, 6, 14, 23, 30, tzinfo=timezone.utc),
        )
    )
    book = service.decision_book(START, END)
    assert any(e.brief_id == "brief-db2-edge" for e in book.entries)
    day_after = service.decision_book(START, date(2026, 6, 13))
    assert all(e.brief_id != "brief-db2-edge" for e in day_after.entries)


def test_book_is_byte_stable_and_writes_nothing():
    service = _staged_service()
    before = _snapshot(service)
    first = service.decision_book(START, END)
    second = service.decision_book(START, END)
    assert first.model_dump_json() == second.model_dump_json()
    assert first.digest == second.digest
    assert _snapshot(service) == before


def test_empty_window_is_an_honest_empty_book():
    service = _staged_service()
    book = service.decision_book(date(2027, 1, 1), date(2027, 1, 7))
    assert book.entries == []
    assert book.total == 0
    assert all(count == 0 for count in book.counts.values())
    assert len(book.digest) == 64


def test_reversed_period_is_refused_by_name():
    service = _staged_service()
    with pytest.raises(DecisionBookError, match="period is empty"):
        service.decision_book(END, START)


def test_undated_act_is_listed_never_dated_or_dropped():
    # A confirmed candidate with no review moment: unreachable
    # through the service, defended at the composition.
    class StubPort:
        def get_candidates(self, account_id, state=None):
            from zep_context_agent.ontology import CandidateFact

            if account_id != "ACC-DB1":
                return []
            return [
                CandidateFact(
                    candidate_id="cand-undated",
                    account_id=account_id,
                    episode_id="ep-undated",
                    doc_id="DOC-UNDATED",
                    doc_title="A letter",
                    doc_kind="correspondence",
                    predicate="annual_revenue_usd",
                    value=100,
                    valid_from=date(2026, 1, 1),
                    document_received_at=date(2026, 6, 2),
                    state="confirmed",
                    review_actor="Senior Underwriter",
                    reviewed_at=None,
                )
            ]

    book = build_decision_book(
        port=StubPort(),
        accounts=_accounts(),
        brief_store=InMemoryBriefStore(),
        staleness_store=InMemoryStalenessStore(),
        follow_store=InMemoryFollowThroughStore(),
        start=START,
        end=END,
    )
    assert book.entries == []
    assert len(book.undated) == 1
    assert book.undated[0].kind == "review_confirmed"
    assert book.undated[0].source_ref == "cand-undated"


def test_api_decision_book_json_text_and_refusal():
    service = _staged_service()
    client = TestClient(create_app(service))
    resp = client.get(
        "/decision-book", params={"start": "2026-06-01", "end": "2026-06-14"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 10
    assert body["counts"]["decision"] == 2
    assert len(body["digest"]) == 64

    text = client.get(
        "/decision-book",
        params={"start": "2026-06-01", "end": "2026-06-14", "format": "text"},
    )
    assert text.status_code == 200
    assert text.headers["content-type"].startswith("text/plain")
    assert "DECISION BOOK — 2026-06-01 to 2026-06-14" in text.text
    assert body["digest"] in text.text

    refused = client.get(
        "/decision-book", params={"start": "2026-06-14", "end": "2026-06-01"}
    )
    assert refused.status_code == 400
    assert "period is empty" in refused.json()["detail"]


def test_real_service_acts_land_in_the_book():
    # The full path, at the wall clock: assemble, request info
    # (decision + ask), then the book over a window that covers
    # today lists both acts.
    from datetime import timedelta

    from zep_context_agent.temporal import today

    accounts = {
        "ACC-DB9": Account(
            account_id="ACC-DB9",
            display_name="Decision Book Live Co",
            documents=[
                SourceDocument(
                    doc_id="DOC-DB9-SUB",
                    account_id="ACC-DB9",
                    kind="submission",
                    title="Submission 2026 — Decision Book Live Co",
                    received_at=date(2026, 5, 20),
                    assertions=[
                        FactAssertion(
                            predicate="legal_name",
                            value="Decision Book Live Co",
                            valid_from=date(2026, 1, 1),
                        ),
                        FactAssertion(
                            predicate="risk_class",
                            value="light manufacturing",
                            valid_from=date(2026, 1, 1),
                        ),
                    ],
                )
            ],
        )
    }
    service = make_service(accounts)
    brief = service.assemble("ACC-DB9", as_of=date(2026, 6, 1))
    decided = service.decide(
        brief.brief_id,
        action="request_info",
        actor="Senior Underwriter",
        reason="Revenue and claims, please.",
    )
    assert decided.decision is not None
    book = service.decision_book(today() - timedelta(days=1), today())
    kinds = [e.kind for e in book.entries if e.account_id == "ACC-DB9"]
    assert kinds == ["decision", "ask_opened"]
