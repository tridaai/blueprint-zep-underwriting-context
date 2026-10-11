"""Time, visible: the account timeline and the expiry watch.

Pins, by layer:

- the watch's day math — a policy inside the horizon flags
  EXPIRING with exact days remaining; the horizon is
  configurable (argument, and EXPIRY_HORIZON_DAYS for the
  default) and a fact beyond it is silent;
- the successor discipline — a policy renewed before expiry
  (successor window covering its end) never flags, at any
  date; a fact *replaced* mid-window is history, not a lapse;
  a successor that starts after a gap is named on the lapsed
  row, because the gap was real;
- the crossing — unrenewed, the same policy reads EXPIRING
  before its end and LAPSED, negative days, after it;
- the timeline — one chronology: facts on their windows
  with the temporal rule's status, episodes, decisions,
  staleness flags and resolutions, follow-through movement,
  deterministically ordered;
- the disciplines — the timeline is account-scoped against a
  near-identically named neighbour, and both models are pure
  projections: computing them mutates nothing;
- the surfaces — desk-queue rows carry the watch, the API
  serves both models, unknown accounts 404.
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver

from zep_context_agent import config
from zep_context_agent.api import create_app
from zep_context_agent.datasets import DATASET_PRESENT
from zep_context_agent.offline_graph import OfflineContextGraph
from zep_context_agent.ontology import Account, FactAssertion, SourceDocument
from zep_context_agent.service import BriefService
from zep_context_agent.staleness import build_desk_queue
from zep_context_agent.store import InMemoryBriefStore
from zep_context_agent.timeline import _KIND_RANK

AS_OF = date(2026, 3, 1)  # the staged world's "today" for the watch


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


def staged_accounts() -> dict[str, Account]:
    wa = Account(
        account_id="ACC-WA",
        display_name="Watchful Widgets Inc",
        documents=[
            doc(
                "DOC-WA-SUB", "ACC-WA", "submission",
                "Submission 2025 — Watchful Widgets Inc", date(2025, 3, 10),
                assertions=[
                    fact("legal_name", "Watchful Widgets Inc", date(2025, 1, 1)),
                    fact("risk_class", "light manufacturing", date(2025, 1, 1)),
                    fact("primary_location", "9 Watch Way, Akron, OH", date(2025, 1, 1)),
                    fact("annual_revenue_usd", 8000000, date(2025, 1, 1), unit="USD"),
                    fact("claims_count_3yr", 1, date(2025, 1, 1)),
                ],
            ),
            doc(
                "DOC-WA-POL", "ACC-WA", "endorsement",
                "Policy POL-WA-2025 schedule — Watchful Widgets Inc",
                date(2025, 3, 15),
                assertions=[
                    fact("policy_limit_usd", 3000000, date(2025, 3, 15),
                         date(2026, 3, 20), entity_ref="POL-WA-2025", unit="USD"),
                    fact("deductible_usd", 10000, date(2025, 3, 15),
                         date(2026, 3, 20), entity_ref="POL-WA-2025", unit="USD"),
                    fact("policy_limit_usd", 500000, date(2025, 3, 15),
                         date(2027, 6, 1), entity_ref="POL-WA-FAR", unit="USD"),
                ],
            ),
        ],
    )
    wb = Account(
        account_id="ACC-WB",
        display_name="Watchful Widgets Inc LLC",
        documents=[
            doc(
                "DOC-WB-SUB", "ACC-WB", "submission",
                "Submission 2025 — Watchful Widgets Inc LLC", date(2025, 4, 1),
                assertions=[
                    fact("legal_name", "Watchful Widgets Inc LLC", date(2025, 1, 1)),
                    fact("risk_class", "light manufacturing", date(2025, 1, 1)),
                    fact("primary_location", "9 Watch Way, Akron, OH", date(2025, 1, 1)),
                    fact("annual_revenue_usd", 8100000, date(2025, 1, 1), unit="USD"),
                    fact("claims_count_3yr", 0, date(2025, 1, 1)),
                ],
            ),
            doc(
                "DOC-WB-POL", "ACC-WB", "endorsement",
                "Policy POL-WB-2025 schedule — Watchful Widgets Inc LLC",
                date(2025, 4, 1),
                assertions=[
                    fact("policy_limit_usd", 1500000, date(2025, 4, 1),
                         date(2026, 3, 10), entity_ref="POL-WB-2025", unit="USD"),
                ],
            ),
            doc(
                "DOC-WB-REN", "ACC-WB", "endorsement",
                "Renewal POL-WB-2025 — Watchful Widgets Inc LLC",
                date(2026, 2, 20),
                assertions=[
                    fact("policy_limit_usd", 1750000, date(2026, 3, 10),
                         date(2027, 3, 10), entity_ref="POL-WB-2025", unit="USD"),
                ],
            ),
        ],
    )
    wg = Account(
        account_id="ACC-WG",
        display_name="Gap Renewal Co",
        documents=[
            doc(
                "DOC-WG-POL", "ACC-WG", "endorsement",
                "Policy POL-WG-2024 schedule — Gap Renewal Co",
                date(2025, 1, 20),
                assertions=[
                    fact("policy_limit_usd", 900000, date(2025, 2, 1),
                         date(2026, 2, 1), entity_ref="POL-WG-2024", unit="USD"),
                ],
            ),
            doc(
                "DOC-WG-REN", "ACC-WG", "endorsement",
                "Renewal POL-WG-2024 (late) — Gap Renewal Co",
                date(2026, 1, 25),
                assertions=[
                    fact("policy_limit_usd", 950000, date(2026, 3, 1),
                         date(2027, 3, 1), entity_ref="POL-WG-2024", unit="USD"),
                ],
            ),
        ],
    )
    return {a.account_id: a for a in (wa, wb, wg)}


def make_service(accounts) -> BriefService:
    return BriefService(
        port=OfflineContextGraph(),
        store=InMemoryBriefStore(),
        object_store=None,
        checkpointer=MemorySaver(),
        cache=None,
        accounts=accounts,
    )


@pytest.fixture
def staged():
    accounts = staged_accounts()
    service = make_service(accounts)
    for account_id in accounts:
        service.ingest(account_id)
    return service


def rows_for(service, account_id, as_of=AS_OF, horizon=45):
    return [
        r
        for r in service.expiry_watch(as_of=as_of, horizon_days=horizon)
        if r.account_id == account_id
    ]


# -- the watch: day math + horizon -----------------------------------------


def test_expiring_day_math_is_exact(staged):
    rows = rows_for(staged, "ACC-WA")
    by_predicate = {r.predicate: r for r in rows}
    assert set(by_predicate) == {"policy_limit_usd", "deductible_usd"}
    limit = by_predicate["policy_limit_usd"]
    assert limit.entity_ref == "POL-WA-2025"
    assert limit.state == "expiring"
    assert limit.valid_to == date(2026, 3, 20)
    assert limit.days_remaining == 19  # 2026-03-20 − 2026-03-01
    assert limit.successor_exists is False
    assert limit.doc_id == "DOC-WA-POL"


def test_beyond_the_horizon_is_silent(staged):
    # POL-WA-FAR ends 457 days out: silent at 45, silent at 400,
    # named at 500 — the math, not a mood.
    assert all(
        r.entity_ref != "POL-WA-FAR" for r in rows_for(staged, "ACC-WA")
    )
    assert all(
        r.entity_ref != "POL-WA-FAR"
        for r in rows_for(staged, "ACC-WA", horizon=400)
    )
    far = [
        r
        for r in rows_for(staged, "ACC-WA", horizon=500)
        if r.entity_ref == "POL-WA-FAR"
    ]
    assert len(far) == 1
    assert far[0].state == "expiring" and far[0].days_remaining == 457


def test_horizon_argument_and_boundary(staged):
    assert rows_for(staged, "ACC-WA", horizon=10) == []
    boundary = rows_for(staged, "ACC-WA", horizon=19)
    assert {r.predicate for r in boundary} == {
        "policy_limit_usd",
        "deductible_usd",
    }


def test_horizon_config(monkeypatch):
    monkeypatch.delenv("EXPIRY_HORIZON_DAYS", raising=False)
    assert config.expiry_horizon_days() == 45
    monkeypatch.setenv("EXPIRY_HORIZON_DAYS", "30")
    assert config.expiry_horizon_days() == 30
    monkeypatch.setenv("EXPIRY_HORIZON_DAYS", "not-a-number")
    assert config.expiry_horizon_days() == 45  # unreadable → default
    monkeypatch.setenv("EXPIRY_HORIZON_DAYS", "0")
    assert config.expiry_horizon_days() == 45  # never a silent disable


# -- the watch: successor discipline ----------------------------------------


def test_renewed_before_expiry_never_flags(staged):
    # ACC-WB's renewal was on file before the old term ended and
    # its window covers the end: no row before expiry, none at
    # expiry, none after — at any horizon.
    for as_of in (date(2026, 3, 1), date(2026, 3, 10), date(2026, 4, 5), date(2027, 1, 1)):
        assert rows_for(staged, "ACC-WB", as_of=as_of) == []


def test_unrenewed_policy_crosses_to_lapsed(staged):
    rows = rows_for(staged, "ACC-WA", as_of=date(2026, 4, 5))
    limit = next(r for r in rows if r.predicate == "policy_limit_usd")
    assert limit.state == "lapsed"
    assert limit.days_remaining == -16  # 2026-03-20 − 2026-04-05
    assert limit.successor_exists is False


def test_gap_successor_is_named_not_covering(staged):
    rows = rows_for(staged, "ACC-WG", as_of=date(2026, 2, 15))
    assert len(rows) == 1
    row = rows[0]
    assert row.state == "lapsed"
    assert row.days_remaining == -14  # 2026-02-01 − 2026-02-15
    assert row.successor_exists is True
    assert row.successor_valid_from == date(2026, 3, 1)
    # Once the late renewal is in force, the old fact is history
    # with a successor resolve can name — the row goes away.
    assert rows_for(staged, "ACC-WG", as_of=date(2026, 3, 15)) == []


def test_replaced_facts_are_not_lapses(service):
    # The seeded world: ACC-001's original limit was replaced
    # mid-window by an open-ended successor; ACC-011's policy
    # was renewed seamlessly. Neither may appear — as lapsed
    # or expiring — while ACC-006's inspection facts, never
    # renewed, are the dataset's genuine lapse.
    for account_id in ("ACC-001", "ACC-011"):
        service.ingest(account_id)
    service.ingest("ACC-006")
    rows = service.expiry_watch(as_of=DATASET_PRESENT, horizon_days=45)
    assert [r for r in rows if r.account_id in ("ACC-001", "ACC-011")] == []
    lapsed = [r for r in rows if r.account_id == "ACC-006"]
    assert {r.predicate for r in lapsed} == {
        "inspection_grade",
        "sprinkler_coverage_pct",
    }
    assert all(
        r.state == "lapsed"
        and r.days_remaining == (date(2024, 3, 20) - DATASET_PRESENT).days
        and r.successor_exists is False
        for r in lapsed
    )


# -- the timeline -------------------------------------------------------------


def test_timeline_orders_and_places_every_fact(staged):
    timeline = staged.account_timeline("ACC-WA", as_of=AS_OF)
    events = timeline.events
    keys = [(e.date, _KIND_RANK[e.kind]) for e in events]
    assert keys == sorted(keys)  # one chronology, deterministic
    facts = staged.port.get_facts("ACC-WA")
    fact_events = [e for e in events if e.kind == "fact"]
    assert len(fact_events) == len(facts)  # every fact, exactly once
    limit = next(
        e
        for e in fact_events
        if e.predicate == "policy_limit_usd" and e.entity_ref == "POL-WA-2025"
    )
    assert limit.date == date(2025, 3, 15)
    assert limit.valid_to == date(2026, 3, 20)
    assert limit.fact_status == "current"
    assert limit.doc_id == "DOC-WA-POL" and limit.episode_id
    episodes = [e for e in events if e.kind == "episode"]
    assert {e.doc_id for e in episodes} == {"DOC-WA-SUB", "DOC-WA-POL"}


def test_timeline_statuses_follow_the_temporal_rule(staged):
    timeline = staged.account_timeline("ACC-WB", as_of=AS_OF)
    by_value = {
        e.value: e
        for e in timeline.events
        if e.kind == "fact" and e.predicate == "policy_limit_usd"
    }
    # At 2026-03-01 the old limit is current, the renewal's
    # window has not opened: scheduled, not borrowed-from.
    assert by_value[1500000].fact_status == "current"
    assert by_value[1750000].fact_status == "scheduled"
    later = staged.account_timeline("ACC-WB", as_of=date(2026, 4, 5))
    by_value = {
        e.value: e
        for e in later.events
        if e.kind == "fact" and e.predicate == "policy_limit_usd"
    }
    assert by_value[1500000].fact_status == "superseded"
    assert by_value[1750000].fact_status == "current"


def test_timeline_interleaves_decisions_flags_and_asks():
    accounts = staged_accounts()
    service = make_service(accounts)
    service.ingest("ACC-WA")
    brief = service.assemble("ACC-WA", as_of=date(2026, 3, 1))
    service.decide(
        brief.brief_id,
        action="proceed",
        actor="Test Underwriter",
        reason="Pinned flow: decide, then the facts move.",
    )
    # The endorsement lands: the limit halves, the decision stales.
    accounts["ACC-WA"].documents.append(
        doc(
            "DOC-WA-END2", "ACC-WA", "endorsement",
            "Mid-term endorsement — Watchful Widgets Inc", date(2026, 3, 5),
            assertions=[
                fact("policy_limit_usd", 1500000, date(2026, 3, 5),
                     entity_ref="POL-WA-2025", unit="USD"),
            ],
        )
    )
    service.ingest("ACC-WA")
    assert len(service.list_staleness("ACC-WA")) == 1
    brief2 = service.assemble("ACC-WA", as_of=date(2026, 3, 6))
    service.decide(
        brief2.brief_id,
        action="proceed",
        actor="Test Underwriter",
        reason="Pinned flow: decided again on current facts.",
    )
    timeline = service.account_timeline("ACC-WA", as_of=date(2026, 3, 6))
    kinds = [e.kind for e in timeline.events]
    assert kinds.count("decision") == 2
    assert kinds.count("staleness_flag") == 1
    assert kinds.count("staleness_resolved") == 1
    flag = next(e for e in timeline.events if e.kind == "staleness_flag")
    resolved = next(
        e for e in timeline.events if e.kind == "staleness_resolved"
    )
    assert flag.record_id == resolved.record_id
    assert flag.brief_id == brief.brief_id
    assert resolved.brief_id == brief2.brief_id


def test_timeline_interleaves_follow_through():
    account = Account(
        account_id="ACC-WQ",
        display_name="Ask Widgets Co",
        documents=[
            doc(
                "DOC-WQ-SUB", "ACC-WQ", "submission",
                "Submission 2026 — Ask Widgets Co", date(2026, 5, 20),
                assertions=[
                    fact("legal_name", "Ask Widgets Co", date(2026, 1, 1)),
                    fact("risk_class", "light manufacturing", date(2026, 1, 1)),
                    fact("primary_location", "4 Ask Ave, Dayton, OH", date(2026, 1, 1)),
                    fact("policy_limit_usd", 1000000, date(2026, 1, 1), unit="USD"),
                ],
            )
        ],
    )
    accounts = {"ACC-WQ": account}
    service = make_service(accounts)
    brief = service.assemble("ACC-WQ", as_of=date(2026, 6, 1))
    service.decide(
        brief.brief_id,
        action="request_info",
        actor="Test Underwriter",
        reason="Pinned flow: the gaps, asked for.",
    )
    account.documents.append(
        doc(
            "DOC-WQ-REPLY", "ACC-WQ", "claims_extract",
            "Broker reply — Ask Widgets Co", date(2026, 6, 3),
            assertions=[
                fact("claims_count_3yr", 2, date(2026, 6, 3)),
                fact("annual_revenue_usd", 4200000, date(2025, 1, 1), unit="USD"),
            ],
        )
    )
    service.ingest("ACC-WQ")
    timeline = service.account_timeline("ACC-WQ", as_of=date(2026, 6, 4))
    kinds = [e.kind for e in timeline.events]
    assert kinds.count("follow_opened") == 1
    assert kinds.count("follow_received") == 2
    assert kinds.count("follow_completed") == 1
    received = [e for e in timeline.events if e.kind == "follow_received"]
    assert all(e.episode_id == "ep-DOC-WQ-REPLY" for e in received)


def test_timeline_never_leaks_the_near_identical_neighbour(staged):
    timeline = staged.account_timeline("ACC-WA", as_of=AS_OF)
    assert timeline.account_name == "Watchful Widgets Inc"
    for event in timeline.events:
        assert not event.doc_id.startswith("DOC-WB")
        assert "LLC" not in event.label
        assert "LLC" not in event.doc_title
    other = staged.account_timeline("ACC-WB", as_of=AS_OF)
    assert all(
        not e.doc_id.startswith("DOC-WA") for e in other.events
    )


def test_both_models_are_pure_projections(staged):
    def snapshot(service):
        return (
            [b.model_dump_json() for b in service.list_briefs()],
            [r.model_dump_json() for r in service.list_staleness()],
            [r.model_dump_json() for r in service.list_follow_through()],
            {
                aid: sorted(f.fact_id for f in service.port.get_facts(aid))
                for aid in ("ACC-WA", "ACC-WB", "ACC-WG")
            },
        )

    before = snapshot(staged)
    staged.account_timeline("ACC-WA", as_of=AS_OF)
    staged.expiry_watch(as_of=AS_OF, horizon_days=45)
    staged.desk_queue()
    assert snapshot(staged) == before


# -- the surfaces ---------------------------------------------------------------


def test_desk_queue_rows_carry_the_watch(staged):
    queue = build_desk_queue(
        staged.accounts,
        staged.store,
        staged.staleness_store,
        follow_store=staged.follow_store,
        today=AS_OF,
        port=staged.port,
    )
    by_account = {e.account_id: e for e in queue}
    wa = by_account["ACC-WA"]
    assert {(x.predicate, x.state, x.days_remaining) for x in wa.expiry} == {
        ("policy_limit_usd", "expiring", 19),
        ("deductible_usd", "expiring", 19),
    }
    assert by_account["ACC-WB"].expiry == []  # renewed — silent
    # Without port access the section is simply absent, as before.
    bare = build_desk_queue(
        staged.accounts,
        staged.store,
        staged.staleness_store,
        follow_store=staged.follow_store,
        today=AS_OF,
    )
    assert all(e.expiry == [] for e in bare)


def test_api_serves_timeline_and_watch(staged):
    client = TestClient(create_app(staged))
    timeline = client.get(
        "/accounts/ACC-WA/timeline", params={"as_of": "2026-03-01"}
    )
    assert timeline.status_code == 200
    body = timeline.json()
    assert body["account_id"] == "ACC-WA" and body["as_of"] == "2026-03-01"
    assert any(e["kind"] == "fact" for e in body["events"])
    assert client.get("/accounts/ACC-999/timeline").status_code == 404

    watch = client.get(
        "/expiry-watch", params={"as_of": "2026-03-01", "horizon_days": 45}
    )
    assert watch.status_code == 200
    rows = watch.json()
    wa = [r for r in rows if r["account_id"] == "ACC-WA"]
    assert {(r["predicate"], r["days_remaining"]) for r in wa} == {
        ("policy_limit_usd", 19),
        ("deductible_usd", 19),
    }
    assert [r for r in rows if r["account_id"] == "ACC-WB"] == []
    queue = client.get("/desk-queue").json()
    assert all("expiry" in e for e in queue)
