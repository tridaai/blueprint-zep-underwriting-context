"""The renewal chase: the expiry watch learns to ask.

Pins, by layer:

- the row — an expiring fact with no covering ask raises a
  chase row naming the fact, its value, its window's end and
  its exact days left; the states resolve honestly (OPEN with
  a brief on file, WAITING_ON_BRIEF without one, LAPSED_UNSENT
  once the window closes unchased — and the lapsed state
  outranks the missing brief, because the failure is the news);
- the dedupe — a predicate an open request-info ask already
  covers gets NO chase row, and a send for it is refused
  naming the covering ask: the broker is never asked twice
  about one line of the file;
- the send — it opens an ordinary follow-through ask through
  the round-4 machinery: origin ``renewal_chase``, one
  renewal item per flagged fact carrying the chased fact's
  identity, a draft composed in code naming the fact and the
  day its term ends, attached to the account's latest brief;
  a second send is refused by name; a send with no brief is
  refused by name and opens nothing;
- the lifecycle — the renewal document arrives (new document,
  window covering the chased fact's end), the item flips
  through the existing recognition naming the renewal's
  episode, the ask completes, the watch clears, the chase
  reads SATISFIED naming the renewal document; a renewal
  that starts after a gap does NOT satisfy the item — the
  gap was real exposure and the chase stays open saying so;
- the disciplines — decided briefs byte-identical throughout;
  the chase projection is pure (computing rows mutates
  nothing); the desk queue carries chases as annotations and
  its order is untouched by them; the portfolio brief counts
  open chases display-only — weights and scores unmoved;
- the surfaces — the API serves the rows and the send, and
  refusals arrive as 409s naming their cause.
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver

from zep_context_agent.api import create_app
from zep_context_agent.chases import (
    chases_for_account,
    covered_predicates,
    open_chase_count,
)
from zep_context_agent.errors import ChaseError
from zep_context_agent.offline_graph import OfflineContextGraph
from zep_context_agent.ontology import Account, FactAssertion, SourceDocument
from zep_context_agent.portfolio import (
    PORTFOLIO_WEIGHTS,
    build_portfolio_brief,
    score_signals,
)
from zep_context_agent.service import BriefService
from zep_context_agent.staleness import build_desk_queue
from zep_context_agent.store import InMemoryBriefStore

AS_OF = date(2026, 6, 1)
HORIZON = 45
ACTOR = "Test Underwriter"


def doc(doc_id, account_id, kind, title, received, assertions=()):
    return SourceDocument(
        doc_id=doc_id,
        account_id=account_id,
        kind=kind,
        title=title,
        received_at=received,
        assertions=list(assertions),
    )


def fact(predicate, value, valid_from, valid_to=None, entity_ref="", unit=""):
    return FactAssertion(
        predicate=predicate,
        value=value,
        unit=unit,
        valid_from=valid_from,
        valid_to=valid_to,
        entity_ref=entity_ref,
    )


def submission(doc_id, account_id, name, *, omit=()):
    values = {
        "legal_name": name,
        "risk_class": "light manufacturing",
        "annual_revenue_usd": 5000000,
        "primary_location": "1 Chase Way, Dayton, OH",
        "policy_limit_usd": 1000000,
        "claims_count_3yr": 1,
    }
    assertions = [
        fact(
            predicate,
            value,
            date(2026, 1, 1),
            unit="USD" if predicate.endswith("_usd") else "",
        )
        for predicate, value in values.items()
        if predicate not in omit
    ]
    return doc(
        doc_id, account_id, "submission",
        f"Submission 2026 — {name}", date(2026, 5, 20), assertions,
    )


def inspection(doc_id, account_id, name, grade, valid_from, valid_to, received):
    return doc(
        doc_id, account_id, "inspection",
        f"Inspection report — {name}", received,
        assertions=[
            fact("inspection_grade", grade, valid_from, valid_to, unit="pts")
        ],
    )


def make_service(staged) -> BriefService:
    return BriefService(
        port=OfflineContextGraph(),
        store=InMemoryBriefStore(),
        object_store=None,
        checkpointer=MemorySaver(),
        cache=None,
        accounts=staged,
    )


def ch_account(documents=None) -> Account:
    docs = documents or [
        submission("DOC-CH-SUB", "ACC-CH", "Chase Test Co"),
        inspection(
            "DOC-CH-INS", "ACC-CH", "Chase Test Co", 92,
            date(2025, 6, 15), date(2026, 6, 20), date(2025, 6, 20),
        ),
    ]
    return Account(
        account_id="ACC-CH", display_name="Chase Test Co", documents=docs
    )


def decided_ch_service():
    """ACC-CH, ingested, briefed, decided — with one fact
    expiring 19 days after AS_OF."""
    staged = {"ACC-CH": ch_account()}
    service = make_service(staged)
    service.ingest("ACC-CH")
    brief = service.assemble("ACC-CH", as_of=AS_OF)
    service.decide(
        brief.brief_id, action="proceed", actor=ACTOR,
        reason="The file as it stands.",
    )
    snapshot = service.get_brief(brief.brief_id).model_dump_json()
    return service, staged, brief, snapshot


def rows_for(service, account_id, as_of=AS_OF):
    return service.renewal_chases(account_id, as_of=as_of, horizon_days=HORIZON)


# ---------------------------------------------------------------------------
# The row and its states
# ---------------------------------------------------------------------------


def test_expiring_fact_raises_an_open_chase_row():
    service, _, brief, _ = decided_ch_service()
    rows = rows_for(service, "ACC-CH")
    assert len(rows) == 1
    row = rows[0]
    assert row.predicate == "inspection_grade"
    assert row.label == "Inspection grade"
    assert row.state == "OPEN"
    assert row.brief_id == brief.brief_id
    assert row.ask_record_id is None
    assert row.days_remaining == 19
    assert len(row.facts) == 1
    f = row.facts[0]
    assert (f.value, f.valid_to, f.watch_state, f.days_remaining) == (
        92,
        date(2026, 6, 20),
        "expiring",
        19,
    )
    assert f.doc_id == "DOC-CH-INS"


def test_no_brief_means_waiting_and_no_send():
    staged = {
        "ACC-NB": Account(
            account_id="ACC-NB",
            display_name="No Brief Test Co",
            documents=[
                submission("DOC-NB-SUB", "ACC-NB", "No Brief Test Co"),
                inspection(
                    "DOC-NB-INS", "ACC-NB", "No Brief Test Co", 88,
                    date(2025, 6, 15), date(2026, 6, 20), date(2025, 6, 20),
                ),
            ],
        )
    }
    service = make_service(staged)
    service.ingest("ACC-NB")
    rows = rows_for(service, "ACC-NB")
    assert len(rows) == 1
    assert rows[0].state == "WAITING_ON_BRIEF"
    assert rows[0].brief_id is None
    with pytest.raises(ChaseError, match="no brief"):
        service.send_chase(
            "ACC-NB", "inspection_grade", actor=ACTOR,
            as_of=AS_OF, horizon_days=HORIZON,
        )
    # The refused send opened nothing.
    assert service.list_follow_through("ACC-NB") == []
    # And once the window closes, the lapse outranks the
    # missing brief: the failure is the news.
    later = rows_for(service, "ACC-NB", as_of=date(2026, 7, 5))
    assert later[0].state == "LAPSED_UNSENT"
    assert later[0].days_remaining == -15


def test_lapsed_unsent_names_the_failure_and_still_sends():
    service, _, _, _ = decided_ch_service()
    rows = rows_for(service, "ACC-CH", as_of=date(2026, 7, 5))
    assert len(rows) == 1
    assert rows[0].state == "LAPSED_UNSENT"
    assert rows[0].days_remaining == -15
    assert rows[0].facts[0].watch_state == "lapsed"
    # Late is not never: the send still works from LAPSED_UNSENT.
    record, row = service.send_chase(
        "ACC-CH", "inspection_grade", actor=ACTOR,
        as_of=date(2026, 7, 5), horizon_days=HORIZON,
    )
    assert record.status == "open"
    assert row.state == "ASKED"


# ---------------------------------------------------------------------------
# The dedupe
# ---------------------------------------------------------------------------


def dedupe_service():
    """ACC-DD: the claims predicate's only fact opens
    2026-03-01 — after the brief's as-of — so the brief
    reports it missing and the request-info ask covers it,
    while the watch at AS_OF sees it expiring."""
    staged = {
        "ACC-DD": Account(
            account_id="ACC-DD",
            display_name="Dedupe Test Co",
            documents=[
                submission(
                    "DOC-DD-SUB", "ACC-DD", "Dedupe Test Co",
                    omit=("claims_count_3yr",),
                ),
                doc(
                    "DOC-DD-SCH", "ACC-DD", "endorsement",
                    "Claims schedule — Dedupe Test Co", date(2026, 2, 15),
                    assertions=[
                        fact(
                            "claims_count_3yr", 2,
                            date(2026, 3, 1), date(2026, 6, 20),
                        )
                    ],
                ),
            ],
        )
    }
    service = make_service(staged)
    service.ingest("ACC-DD")
    brief = service.assemble("ACC-DD", as_of=date(2026, 2, 1))
    assert brief.missing_predicates == ["claims_count_3yr"]
    service.decide(
        brief.brief_id, action="request_info", actor=ACTOR,
        reason="The claims count, please.",
    )
    return service, brief


def test_decision_covered_predicate_gets_no_chase_row():
    service, brief = dedupe_service()
    ask = service.follow_through_for_brief(brief.brief_id)
    assert ask is not None and ask.status == "open"
    assert covered_predicates(service.follow_store, "ACC-DD") == {
        "claims_count_3yr": ask
    }
    # The watch flags the predicate (it is expiring at AS_OF) —
    # and still no chase row: the broker is already asked.
    watch = service.expiry_watch(as_of=AS_OF, horizon_days=HORIZON)
    assert any(
        r.account_id == "ACC-DD" and r.predicate == "claims_count_3yr"
        for r in watch
    )
    assert rows_for(service, "ACC-DD") == []
    with pytest.raises(ChaseError, match="already asked"):
        service.send_chase(
            "ACC-DD", "claims_count_3yr", actor=ACTOR,
            as_of=AS_OF, horizon_days=HORIZON,
        )


# ---------------------------------------------------------------------------
# The send + the lifecycle
# ---------------------------------------------------------------------------


def test_send_opens_an_ordinary_ask_through_the_machinery():
    service, _, brief, snapshot = decided_ch_service()
    record, row = service.send_chase(
        "ACC-CH", "inspection_grade", actor=ACTOR,
        as_of=AS_OF, horizon_days=HORIZON,
    )
    assert row.state == "ASKED"
    assert row.ask_record_id == record.record_id
    assert record.origin == "renewal_chase"
    assert record.brief_id == brief.brief_id
    assert record.decision_actor == ACTOR
    assert "inspection_grade" not in record.decision_reason  # labels, not ids
    assert "Inspection grade" in record.decision_reason
    assert len(record.items) == 1
    item = record.items[0]
    graph_fact = next(
        f
        for f in service.port.get_facts("ACC-CH")
        if f.predicate == "inspection_grade"
    )
    assert (item.kind, item.predicate, item.fact_id) == (
        "renewal",
        "inspection_grade",
        graph_fact.fact_id,
    )
    assert item.expires_on == date(2026, 6, 20)
    assert item.doc_id == "DOC-CH-INS"
    # The draft: composed in code, naming the fact and its end.
    assert record.draft_text.startswith("DRAFT")
    assert "Inspection grade" in record.draft_text
    assert "2026-06-20" in record.draft_text
    assert "Inspection report — Chase Test Co" in record.draft_text
    # The decided brief is untouched by the send.
    assert service.get_brief(brief.brief_id).model_dump_json() == snapshot
    # And a second send is refused by name.
    with pytest.raises(ChaseError, match="already sent"):
        service.send_chase(
            "ACC-CH", "inspection_grade", actor=ACTOR,
            as_of=AS_OF, horizon_days=HORIZON,
        )
    with pytest.raises(ChaseError, match="nothing the account relies on"):
        service.send_chase(
            "ACC-CH", "risk_class", actor=ACTOR,
            as_of=AS_OF, horizon_days=HORIZON,
        )


def test_the_renewal_lands_and_the_chase_reads_satisfied():
    service, staged, brief, snapshot = decided_ch_service()
    record, _ = service.send_chase(
        "ACC-CH", "inspection_grade", actor=ACTOR,
        as_of=AS_OF, horizon_days=HORIZON,
    )
    renewal = inspection(
        "DOC-CH-REN", "ACC-CH", "Chase Test Co", 95,
        date(2026, 6, 20), date(2027, 6, 20), date(2026, 6, 10),
    )
    staged["ACC-CH"] = ch_account(
        documents=list(staged["ACC-CH"].documents) + [renewal]
    )
    service.ingest("ACC-CH")
    record = service.follow_through_for_brief(brief.brief_id)
    assert record.status == "complete"
    item = record.items[0]
    assert item.status == "received"
    assert item.received_episode_id == "ep-DOC-CH-REN"
    # The watch has cleared: the renewal covers the old end.
    assert service.expiry_watch(as_of=AS_OF, horizon_days=HORIZON) == []
    rows = rows_for(service, "ACC-CH")
    assert len(rows) == 1
    row = rows[0]
    assert row.state == "SATISFIED"
    assert row.days_remaining is None
    assert row.facts[0].satisfied_doc_id == "DOC-CH-REN"
    assert "Inspection report" in row.facts[0].satisfied_doc_title
    # The timeline tells the story with the chase's own label.
    timeline = service.account_timeline("ACC-CH", as_of=AS_OF)
    assert any(
        e.kind == "follow_opened" and "Renewal chase sent" in e.label
        for e in timeline.events
    )
    # Closed business: a further send is refused as satisfied,
    # and the decided brief never moved.
    with pytest.raises(ChaseError, match="already satisfied"):
        service.send_chase(
            "ACC-CH", "inspection_grade", actor=ACTOR,
            as_of=AS_OF, horizon_days=HORIZON,
        )
    assert service.get_brief(brief.brief_id).model_dump_json() == snapshot


def test_a_gap_renewal_does_not_satisfy_the_chase():
    service, staged, brief, _ = decided_ch_service()
    record, _ = service.send_chase(
        "ACC-CH", "inspection_grade", actor=ACTOR,
        as_of=AS_OF, horizon_days=HORIZON,
    )
    late_renewal = inspection(
        "DOC-CH-GAP", "ACC-CH", "Chase Test Co", 95,
        date(2026, 7, 1), date(2027, 7, 1), date(2026, 6, 25),
    )
    staged["ACC-CH"] = ch_account(
        documents=list(staged["ACC-CH"].documents) + [late_renewal]
    )
    service.ingest("ACC-CH")
    record = service.follow_through_for_brief(brief.brief_id)
    # The renewal starts after the chased window closed: the
    # gap was real exposure, so the item stays open and the
    # watch names the lapse with its successor.
    assert record.status == "open"
    assert record.items[0].status == "open"
    watch = service.expiry_watch(as_of=date(2026, 6, 25), horizon_days=HORIZON)
    assert len(watch) == 1
    assert watch[0].state == "lapsed"
    assert watch[0].successor_exists is True
    rows = rows_for(service, "ACC-CH", as_of=date(2026, 6, 25))
    assert rows[0].state == "ASKED"


def test_one_chase_per_predicate_however_many_facts():
    staged = {
        "ACC-ME": Account(
            account_id="ACC-ME",
            display_name="Multi Entity Test Co",
            documents=[
                submission(
                    "DOC-ME-SUB", "ACC-ME", "Multi Entity Test Co",
                    omit=("policy_limit_usd",),
                ),
                doc(
                    "DOC-ME-POL", "ACC-ME", "endorsement",
                    "Policy schedules — Multi Entity Test Co",
                    date(2026, 5, 1),
                    assertions=[
                        fact(
                            "policy_limit_usd", 1000000,
                            date(2025, 6, 1), date(2026, 6, 20),
                            entity_ref="POL-ME-1", unit="USD",
                        ),
                        fact(
                            "policy_limit_usd", 500000,
                            date(2025, 6, 1), date(2026, 6, 25),
                            entity_ref="POL-ME-2", unit="USD",
                        ),
                    ],
                ),
            ],
        )
    }
    service = make_service(staged)
    service.ingest("ACC-ME")
    brief = service.assemble("ACC-ME", as_of=AS_OF)
    service.decide(
        brief.brief_id, action="proceed", actor=ACTOR, reason="Filed."
    )
    rows = rows_for(service, "ACC-ME")
    assert len(rows) == 1
    assert rows[0].predicate == "policy_limit_usd"
    assert [f.entity_ref for f in rows[0].facts] == ["POL-ME-1", "POL-ME-2"]
    assert rows[0].days_remaining == 19
    record, _ = service.send_chase(
        "ACC-ME", "policy_limit_usd", actor=ACTOR,
        as_of=AS_OF, horizon_days=HORIZON,
    )
    assert len(record.items) == 2
    # Renew one policy: the ask — and the chase — stay open.
    renewal_1 = doc(
        "DOC-ME-REN1", "ACC-ME", "endorsement",
        "Renewal POL-ME-1 — Multi Entity Test Co", date(2026, 6, 10),
        assertions=[
            fact(
                "policy_limit_usd", 1100000,
                date(2026, 6, 20), date(2027, 6, 20),
                entity_ref="POL-ME-1", unit="USD",
            )
        ],
    )
    staged["ACC-ME"] = Account(
        account_id="ACC-ME",
        display_name="Multi Entity Test Co",
        documents=list(staged["ACC-ME"].documents) + [renewal_1],
    )
    service.ingest("ACC-ME")
    record = service.follow_through_for_brief(brief.brief_id)
    assert record.status == "partially_received"
    by_entity = {i.entity_ref: i for i in record.items}
    assert by_entity["POL-ME-1"].status == "received"
    assert by_entity["POL-ME-2"].status == "open"
    rows = rows_for(service, "ACC-ME")
    assert rows[0].state == "ASKED"
    assert rows[0].ask_status == "partially_received"
    # Renew the second: the chase closes, satisfied on both.
    renewal_2 = doc(
        "DOC-ME-REN2", "ACC-ME", "endorsement",
        "Renewal POL-ME-2 — Multi Entity Test Co", date(2026, 6, 12),
        assertions=[
            fact(
                "policy_limit_usd", 550000,
                date(2026, 6, 25), date(2027, 6, 25),
                entity_ref="POL-ME-2", unit="USD",
            )
        ],
    )
    staged["ACC-ME"] = Account(
        account_id="ACC-ME",
        display_name="Multi Entity Test Co",
        documents=list(staged["ACC-ME"].documents) + [renewal_2],
    )
    service.ingest("ACC-ME")
    assert service.follow_through_for_brief(brief.brief_id).status == "complete"
    rows = rows_for(service, "ACC-ME")
    assert rows[0].state == "SATISFIED"
    assert {f.satisfied_doc_id for f in rows[0].facts} == {
        "DOC-ME-REN1",
        "DOC-ME-REN2",
    }


# ---------------------------------------------------------------------------
# The disciplines — purity, queue order, portfolio weights
# ---------------------------------------------------------------------------


def _snapshot(service):
    return (
        [b.model_dump_json() for b in service.list_briefs()],
        [r.model_dump_json() for r in service.list_follow_through()],
        [r.model_dump_json() for r in service.list_staleness()],
        sorted(
            f.fact_id
            for aid in ("ACC-CH",)
            for f in service.port.get_facts(aid)
        ),
    )


def test_chase_projection_is_pure_and_leaves_the_queue_ordered():
    service, staged, _, _ = decided_ch_service()
    before = _snapshot(service)
    rows = chases_for_account(
        port=service.port,
        account=staged["ACC-CH"],
        brief_store=service.store,
        follow_store=service.follow_store,
        as_of=AS_OF,
        horizon_days=HORIZON,
    )
    assert len(rows) == 1
    build_portfolio_brief(
        port=service.port,
        accounts=staged,
        brief_store=service.store,
        staleness_store=service.staleness_store,
        follow_store=service.follow_store,
        as_of=AS_OF,
        horizon_days=HORIZON,
    )
    annotated = build_desk_queue(
        staged,
        service.store,
        service.staleness_store,
        follow_store=service.follow_store,
        today=AS_OF,
        port=service.port,
    )
    plain = build_desk_queue(
        staged,
        service.store,
        service.staleness_store,
        follow_store=service.follow_store,
        today=AS_OF,
    )
    assert _snapshot(service) == before
    # The annotation rides the row; the order never reads it.
    assert [e.account_id for e in annotated] == [e.account_id for e in plain]
    entry = annotated[0]
    assert len(entry.chases) == 1
    assert entry.chases[0].predicate == "inspection_grade"
    assert entry.chases[0].state == "OPEN"
    assert entry.chases[0].days_remaining == 19


def test_portfolio_counts_open_chases_display_only():
    service, staged, _, _ = decided_ch_service()

    def portfolio():
        return build_portfolio_brief(
            port=service.port,
            accounts=staged,
            brief_store=service.store,
            staleness_store=service.staleness_store,
            follow_store=service.follow_store,
            as_of=AS_OF,
            horizon_days=HORIZON,
        )

    brief = portfolio()
    assert brief.weights == PORTFOLIO_WEIGHTS
    row = brief.rows[0]
    assert row.account_id == "ACC-CH"
    assert row.open_chases == 1
    assert row.score == score_signals(row.signals)
    assert "open_chases" not in brief.weights
    service.send_chase(
        "ACC-CH", "inspection_grade", actor=ACTOR,
        as_of=AS_OF, horizon_days=HORIZON,
    )
    assert portfolio().rows[0].open_chases == 1  # ASKED is still open
    renewal = inspection(
        "DOC-CH-REN", "ACC-CH", "Chase Test Co", 95,
        date(2026, 6, 20), date(2027, 6, 20), date(2026, 6, 10),
    )
    staged["ACC-CH"] = ch_account(
        documents=list(staged["ACC-CH"].documents) + [renewal]
    )
    service.ingest("ACC-CH")
    assert portfolio().rows[0].open_chases == 0  # satisfied never counts
    assert open_chase_count(rows_for(service, "ACC-CH")) == 0


# ---------------------------------------------------------------------------
# The API surface
# ---------------------------------------------------------------------------


def test_api_serves_rows_and_the_send():
    _, staged, _, _ = decided_ch_service()
    service = make_service(staged)
    service.ingest("ACC-CH")
    brief = service.assemble("ACC-CH", as_of=AS_OF)
    service.decide(
        brief.brief_id, action="proceed", actor=ACTOR, reason="Filed."
    )
    client = TestClient(create_app(service))
    listed = client.get(
        "/chases",
        params={"account_id": "ACC-CH", "as_of": AS_OF.isoformat()},
    )
    assert listed.status_code == 200
    body = listed.json()
    assert len(body) == 1
    assert body[0]["state"] == "OPEN"
    assert body[0]["facts"][0]["valid_to"] == "2026-06-20"
    sent = client.post(
        "/accounts/ACC-CH/chases/inspection_grade/send",
        json={"actor": ACTOR},
    )
    assert sent.status_code == 200
    payload = sent.json()
    assert payload["ask"]["origin"] == "renewal_chase"
    assert payload["chase"]["state"] == "ASKED"
    again = client.post(
        "/accounts/ACC-CH/chases/inspection_grade/send",
        json={"actor": ACTOR},
    )
    assert again.status_code == 409
    assert "already sent" in again.json()["detail"]
    unknown = client.get("/chases", params={"account_id": "ACC-XXX"})
    assert unknown.status_code == 404
    queue = client.get("/desk-queue")
    entry = next(e for e in queue.json() if e["account_id"] == "ACC-CH")
    assert entry["chases"][0]["state"] == "ASKED"
