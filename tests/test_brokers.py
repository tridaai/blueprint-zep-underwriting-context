"""The broker book: the whole book grouped by placing broker.

Pins, by layer:

- identity — the broker of record is the ``placed_by`` edge
  on the account's own paper; the most recent document's
  assertion wins; an account whose paper names no broker
  groups under an explicit unattributed row, never dropped;
  a ``submitted_by`` edge is not a placing;
- the figures — hand-derived from the staged world below,
  and pinned a second way: every count equals the
  per-account machinery's own figure (the portfolio brief's
  signals, the follow store's open records) summed over the
  broker's accounts. The book composes; it never re-derives;
- the decided mix — each account counts once, under the
  action of its LATEST decided brief, or as undecided;
- the order — the published chasing order: open asks owed,
  then stale decisions, then name, then id; the unattributed
  row always last; no score exists anywhere on the book;
- the disciplines — a pure projection (queue, portfolio,
  stores, decided briefs byte-identical around the build;
  two builds byte-identical), and nothing crosses accounts:
  a row carries counts plus its own accounts' open-ask items,
  and no other account's name, value, or text;
- the surface — the API serves the book with its order rule.

The staged world: broker Test Alpha places three accounts —
one decided request-info with an ask outstanding, one decided
proceed then made stale by an endorsement, with an inspection
grade running out (an open chase stands on it), and one that
moved to Alpha on its latest submission and was never opened.
Broker Test Bravo places one clean decided account. One
account's paper names no broker at all.
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver

from zep_context_agent import brokers as brokers_mod
from zep_context_agent.api import create_app
from zep_context_agent.datasets import load_accounts
from zep_context_agent.offline_graph import OfflineContextGraph
from zep_context_agent.ontology import (
    Account,
    EdgeAssertion,
    FactAssertion,
    SourceDocument,
)
from zep_context_agent.service import BriefService
from zep_context_agent.store import InMemoryBriefStore

AS_OF = date(2026, 6, 1)
HORIZON = 45

ALPHA = "BRK-TA"
BRAVO = "BRK-TB"


def doc(doc_id, account_id, kind, title, received, assertions=(), edges=(), text=""):
    return SourceDocument(
        doc_id=doc_id,
        account_id=account_id,
        kind=kind,
        title=title,
        received_at=received,
        text=text,
        assertions=list(assertions),
        edges=list(edges),
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


def placed_by(account_id, broker_id, broker_name):
    return EdgeAssertion(
        source_ref=account_id,
        edge_type="placed_by",
        target_ref=broker_id,
        label=broker_name,
    )


def submission(doc_id, account_id, name, broker=None, *, omit=(), received=date(2026, 5, 20)):
    """A submission carrying the complete expected file, minus
    any predicate in ``omit``, and — when ``broker`` is given —
    the account's placing-broker edge."""
    values = {
        "legal_name": name,
        "risk_class": "light manufacturing",
        "annual_revenue_usd": 5000000,
        "primary_location": "1 Broker Way, Dayton, OH",
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
    edges = [placed_by(account_id, *broker)] if broker else []
    return doc(
        doc_id, account_id, "submission",
        f"Submission 2026 — {name}", received,
        assertions=assertions, edges=edges,
    )


def staged_world():
    ba2_phase1 = Account(
        account_id="ACC-BA2",
        display_name="Broker Alpha Two Co",
        documents=[
            submission(
                "DOC-BA2-SUB", "ACC-BA2", "Broker Alpha Two Co",
                broker=(ALPHA, "Test Alpha Brokers"),
            ),
            doc(
                "DOC-BA2-INS", "ACC-BA2", "inspection",
                "Inspection report — Broker Alpha Two Co",
                date(2025, 6, 20),
                assertions=[
                    fact("inspection_grade", 92, date(2025, 6, 15),
                         date(2026, 6, 20), unit="pts"),
                ],
            ),
        ],
    )
    ba2_full = Account(
        account_id="ACC-BA2",
        display_name="Broker Alpha Two Co",
        documents=[
            *ba2_phase1.documents,
            doc(
                "DOC-BA2-END", "ACC-BA2", "endorsement",
                "Endorsement — limit reduced, Broker Alpha Two Co",
                date(2026, 5, 16),
                assertions=[
                    fact("policy_limit_usd", 750000, date(2026, 5, 15),
                         unit="USD"),
                ],
            ),
        ],
    )
    staged = {
        "ACC-BA1": Account(
            account_id="ACC-BA1",
            display_name="Broker Alpha One Co",
            documents=[
                submission(
                    "DOC-BA1-SUB", "ACC-BA1", "Broker Alpha One Co",
                    broker=(ALPHA, "Test Alpha Brokers"),
                    omit=("claims_count_3yr",),
                ),
            ],
        ),
        "ACC-BA2": ba2_phase1,
        # Moved brokers: the 2025 submission names the old
        # broker; the 2026 renewal names Alpha. Of record is
        # the latest paper's broker.
        "ACC-BC1": Account(
            account_id="ACC-BC1",
            display_name="Broker Moved Co",
            documents=[
                submission(
                    "DOC-BC1-SUB1", "ACC-BC1", "Broker Moved Co",
                    broker=("BRK-TOLD", "Test Old Brokers"),
                    received=date(2025, 5, 20),
                ),
                submission(
                    "DOC-BC1-SUB2", "ACC-BC1", "Broker Moved Co",
                    broker=(ALPHA, "Test Alpha Brokers"),
                ),
            ],
        ),
        "ACC-BB1": Account(
            account_id="ACC-BB1",
            display_name="Broker Bravo One Co",
            documents=[
                submission(
                    "DOC-BB1-SUB", "ACC-BB1", "Broker Bravo One Co",
                    broker=(BRAVO, "Test Bravo Brokers"),
                ),
            ],
        ),
        "ACC-BU1": Account(
            account_id="ACC-BU1",
            display_name="Broker Unplaced Co",
            documents=[
                submission("DOC-BU1-SUB", "ACC-BU1", "Broker Unplaced Co"),
            ],
        ),
    }
    service = BriefService(
        port=OfflineContextGraph(),
        store=InMemoryBriefStore(),
        object_store=None,
        checkpointer=MemorySaver(),
        cache=None,
        accounts=staged,
    )
    # BA1: request_info on a file missing its claims count —
    # the ask opens with exactly that item outstanding.
    brief_ba1 = service.assemble("ACC-BA1", as_of=AS_OF)
    service.decide(
        brief_ba1.brief_id,
        action="request_info",
        actor="Test Underwriter",
        reason="Claims history outstanding.",
    )
    # BA2: decide on the first wave, then the endorsement lands
    # on the decision — the staleness watch does the rest. The
    # inspection grade runs out on 2026-06-20, inside the
    # horizon: an expiring fact, and a chase standing open.
    brief_ba2 = service.assemble("ACC-BA2", as_of=date(2026, 5, 1))
    service.decide(
        brief_ba2.brief_id,
        action="proceed",
        actor="Test Underwriter",
        reason="The file as it stood.",
    )
    staged["ACC-BA2"] = ba2_full
    service.ingest("ACC-BA2")
    # BB1: decided, nothing lands after — clean.
    brief_bb1 = service.assemble("ACC-BB1", as_of=AS_OF)
    service.decide(
        brief_bb1.brief_id,
        action="proceed",
        actor="Test Underwriter",
        reason="Complete file; nothing outstanding.",
    )
    return service, staged


@pytest.fixture(scope="module")
def world():
    return staged_world()


def build(service, **kwargs):
    kwargs.setdefault("as_of", AS_OF)
    kwargs.setdefault("horizon_days", HORIZON)
    return service.broker_book(**kwargs)


def rows_by_id(book):
    return {r.broker_id: r for r in book.rows}


# -- identity ---------------------------------------------------------------


def test_broker_of_record_reads_the_dataset_edge():
    accounts = load_accounts()
    assert brokers_mod.broker_of_record(accounts["ACC-001"]) == (
        "BRK-ALDER",
        "Alder & Voss Insurance Brokers",
    )
    # Every account in the shipped world names a broker.
    assert all(
        brokers_mod.broker_of_record(a)[0] is not None
        for a in accounts.values()
    )


def test_broker_of_record_latest_paper_wins(world):
    _, staged = world
    assert brokers_mod.broker_of_record(staged["ACC-BC1"]) == (
        ALPHA,
        "Test Alpha Brokers",
    )


def test_broker_of_record_unattributed_and_submitted_by_is_not_a_placing():
    bare = Account(
        account_id="ACC-BX",
        display_name="No Broker Co",
        documents=[submission("DOC-BX-SUB", "ACC-BX", "No Broker Co")],
    )
    assert brokers_mod.broker_of_record(bare) == (None, "Unattributed")
    sent_only = Account(
        account_id="ACC-BY",
        display_name="Sent Only Co",
        documents=[
            doc(
                "DOC-BY-SUB", "ACC-BY", "submission",
                "Submission 2026 — Sent Only Co", date(2026, 5, 20),
                edges=[
                    EdgeAssertion(
                        source_ref="DOC-BY-SUB",
                        edge_type="submitted_by",
                        target_ref="BRK-TS",
                        label="Test Sender Brokers",
                    )
                ],
            )
        ],
    )
    assert brokers_mod.broker_of_record(sent_only) == (None, "Unattributed")


# -- the figures, hand-derived ----------------------------------------------


def test_the_book_hand_derived(world):
    service, _ = world
    book = build(service)
    assert [r.broker_id for r in book.rows] == [ALPHA, BRAVO, None]
    rows = rows_by_id(book)

    alpha = rows[ALPHA]
    assert alpha.broker_name == "Test Alpha Brokers"
    assert alpha.accounts == 3
    assert alpha.account_ids == ["ACC-BA1", "ACC-BA2", "ACC-BC1"]
    assert alpha.decided_proceed == 1
    assert alpha.decided_request_info == 1
    assert alpha.decided_escalate == 0
    assert alpha.undecided == 1
    assert alpha.open_asks == 1
    assert alpha.open_items == 1
    assert alpha.stale_decisions == 1
    assert alpha.expiring_facts == 1
    assert alpha.lapsed_facts == 0
    assert alpha.pending_candidates == 0
    # BA2's expiring inspection grade: a chase standing open.
    assert alpha.open_chases == 1
    assert len(alpha.asks) == 1
    ask = alpha.asks[0]
    assert ask.account_id == "ACC-BA1"
    assert ask.origin == "decision"
    assert ask.status == "open"
    assert [(i.kind, i.predicate) for i in ask.items] == [
        ("predicate", "claims_count_3yr")
    ]

    bravo = rows[BRAVO]
    assert bravo.accounts == 1
    assert bravo.decided_proceed == 1
    assert bravo.undecided == 0
    assert bravo.open_asks == 0 and bravo.asks == []
    assert bravo.stale_decisions == 0
    assert bravo.expiring_facts == 0
    assert bravo.open_chases == 0

    unattributed = rows[None]
    assert unattributed.broker_name == "Unattributed"
    assert unattributed.accounts == 1
    assert unattributed.account_ids == ["ACC-BU1"]
    assert unattributed.undecided == 1
    assert unattributed.open_asks == 0


def test_the_order_is_the_published_rule(world):
    service, _ = world
    book = build(service)
    assert book.order_rule == brokers_mod.ORDER_RULE
    # Alpha owes an ask and carries a stale decision; Bravo is
    # clean; the unattributed row closes the book although the
    # chasing order never reaches it.
    assert [r.broker_id for r in book.rows] == [ALPHA, BRAVO, None]
    # A ledger, not a ranking: no score, no weights anywhere
    # on the model (the order rule's own sentence says as much
    # in words; this pins it in structure).
    assert "score" not in book.model_dump()
    assert "weights" not in book.model_dump()
    for row in book.rows:
        assert "score" not in row.model_dump()


def test_stale_breaks_an_asks_tie_before_names():
    """Two brokers tie at zero asks owed: the one carrying a
    stale decision works first, although its name sorts
    last — the order is the rule, not the alphabet."""

    def one_account(account_id, name, broker):
        return Account(
            account_id=account_id,
            display_name=name,
            documents=[
                submission(
                    f"DOC-{account_id}-SUB", account_id, name,
                    broker=broker,
                )
            ],
        )

    zulu = one_account("ACC-OZ", "Order Zulu Co", ("BRK-OZ", "Zulu Brokers"))
    zulu_full = Account(
        account_id="ACC-OZ",
        display_name="Order Zulu Co",
        documents=[
            *zulu.documents,
            doc(
                "DOC-OZ-END", "ACC-OZ", "endorsement",
                "Endorsement — limit reduced, Order Zulu Co",
                date(2026, 5, 16),
                assertions=[
                    fact("policy_limit_usd", 750000, date(2026, 5, 15),
                         unit="USD"),
                ],
            ),
        ],
    )
    staged = {
        "ACC-OA": one_account(
            "ACC-OA", "Order Alpha Co", ("BRK-OA", "Alpha Brokers")
        ),
        "ACC-OZ": zulu,
    }
    service = BriefService(
        port=OfflineContextGraph(),
        store=InMemoryBriefStore(),
        object_store=None,
        checkpointer=MemorySaver(),
        cache=None,
        accounts=staged,
    )
    for account_id in ("ACC-OA", "ACC-OZ"):
        brief = service.assemble(account_id, as_of=date(2026, 5, 1))
        service.decide(
            brief.brief_id,
            action="proceed",
            actor="Test Underwriter",
            reason="The file as it stood.",
        )
    staged["ACC-OZ"] = zulu_full
    service.ingest("ACC-OZ")
    book = build(service)
    assert [r.broker_id for r in book.rows] == ["BRK-OZ", "BRK-OA"]
    assert rows_by_id(book)["BRK-OZ"].stale_decisions == 1


# -- composition: the book is the surfaces, summed ---------------------------


def test_every_figure_is_the_per_account_surfaces_own(world):
    service, _ = world
    book = build(service)
    portfolio = service.portfolio_brief(as_of=AS_OF, horizon_days=HORIZON)
    signals_by_account = {r.account_id: r.signals for r in portfolio.rows}
    for row in book.rows:
        members = row.account_ids
        assert row.stale_decisions == sum(
            signals_by_account[a].stale_decisions for a in members
        )
        assert row.expiring_facts == sum(
            signals_by_account[a].expiring_facts for a in members
        )
        assert row.lapsed_facts == sum(
            signals_by_account[a].lapsed_facts for a in members
        )
        assert row.pending_candidates == sum(
            signals_by_account[a].pending_candidates for a in members
        )
        assert row.open_asks == sum(
            signals_by_account[a].open_asks for a in members
        )
        assert row.undecided == sum(
            signals_by_account[a].no_decided_brief for a in members
        )
        # The chase count is the portfolio row's display count,
        # summed — the one place both surfaces read it from.
        chases_by_account = {r.account_id: r.open_chases for r in portfolio.rows}
        assert row.open_chases == sum(
            chases_by_account[a] for a in members
        )
    # And the whole book accounts for the whole book.
    assert sum(r.accounts for r in book.rows) == len(portfolio.rows)
    assert sum(r.open_asks for r in book.rows) == sum(
        r.signals.open_asks for r in portfolio.rows
    )


def test_ask_listing_is_the_follow_stores_records(world):
    service, _ = world
    book = build(service)
    listed = {
        (a.account_id, a.record_id): a
        for row in book.rows
        for a in row.asks
    }
    open_records = [
        r for r in service.list_follow_through() if r.status != "complete"
    ]
    assert {(r.account_id, r.record_id) for r in open_records} == set(listed)
    for record in open_records:
        listing = listed[(record.account_id, record.record_id)]
        assert listing.status == record.status
        assert listing.origin == record.origin
        assert [i.predicate for i in listing.items] == [
            i.predicate for i in record.items if i.status == "open"
        ]


# -- the disciplines ----------------------------------------------------------


def test_book_is_pure_and_deterministic(world):
    service, _ = world
    decided_before = {
        b.brief_id: b.model_dump_json() for b in service.list_briefs()
    }
    queue_before = [e.model_dump_json() for e in service.desk_queue()]
    portfolio_before = service.portfolio_brief(
        as_of=AS_OF, horizon_days=HORIZON
    ).model_dump_json()
    first = build(service)
    second = build(service)
    assert first.model_dump_json() == second.model_dump_json()
    assert {
        b.brief_id: b.model_dump_json() for b in service.list_briefs()
    } == decided_before
    assert [e.model_dump_json() for e in service.desk_queue()] == queue_before
    assert (
        service.portfolio_brief(
            as_of=AS_OF, horizon_days=HORIZON
        ).model_dump_json()
        == portfolio_before
    )


def test_nothing_crosses_accounts(world):
    service, _ = world
    book = build(service)
    dump = book.model_dump_json()
    # BA2's facts stay on BA2's brief: the endorsement's value
    # and BA2's name appear nowhere in the book (BA2 has no
    # open ask, so it contributes counts only).
    assert "750000" not in dump
    assert "Broker Alpha Two Co" not in dump
    # The one listing names its own account and nothing else's.
    alpha = rows_by_id(book)[ALPHA]
    assert [a.account_id for a in alpha.asks] == ["ACC-BA1"]


def test_module_directly_and_text(world):
    service, staged = world
    book = brokers_mod.build_broker_book(
        port=service.port,
        accounts=staged,
        brief_store=service.store,
        staleness_store=service.staleness_store,
        follow_store=service.follow_store,
        as_of=AS_OF,
        horizon_days=HORIZON,
    )
    text = brokers_mod.render_broker_book_text(book)
    assert "Test Alpha Brokers" in text
    assert "Unattributed" in text
    assert "Claims count, last 3 years" in text


def test_api_serves_the_book_with_its_order_rule(world):
    service, _ = world
    client = TestClient(create_app(service))
    resp = client.get("/brokers", params={"as_of": AS_OF.isoformat()})
    assert resp.status_code == 200
    body = resp.json()
    assert body["order_rule"] == brokers_mod.ORDER_RULE
    assert [r["broker_id"] for r in body["rows"]] == [ALPHA, BRAVO, None]
    alpha = body["rows"][0]
    assert alpha["open_asks"] == 1 and alpha["open_items"] == 1
    assert alpha["asks"][0]["items"][0]["predicate"] == "claims_count_3yr"
