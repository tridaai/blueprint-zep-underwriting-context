"""The morning portfolio brief: the whole book, ranked, in one view.

Pins, by layer:

- the signals — each count is the owning machinery's own
  verdict: the staleness watch's open records, the
  follow-through store's open asks, the expiry watch's LAPSED
  and EXPIRING rows (horizon included — a shorter horizon
  moves the expiring count, because the count rides the
  watch), the review gate's pending candidates, and the
  never-decided flag;
- never-decided is distinct from clean — an account with a
  brief awaiting the gate counts as unexamined, and outranks
  an account that was decided and has nothing outstanding;
- the ranking — score is the published weights over the
  counts, the weights ride the brief itself, ties break by
  account name, and zero-signal accounts close the list in
  name order (not id order);
- the disciplines — the brief is a pure projection: the desk
  queue, the expiry watch, and every store read byte-identical
  before and after it is built, and two builds are
  byte-identical to each other;
- the surface — the API serves the brief with its weights.

The staged world plants one account per signal, plus two
clean decided accounts whose names and ids disagree in order,
so the expected ranking below is arithmetic done by hand from
the planted signals and the authored weights — not the
implementation's output copied back at it.
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver

from zep_context_agent import portfolio as portfolio_mod
from zep_context_agent.api import create_app
from zep_context_agent.offline_graph import OfflineContextGraph
from zep_context_agent.ontology import Account, FactAssertion, SourceDocument
from zep_context_agent.portfolio import PORTFOLIO_WEIGHTS
from zep_context_agent.service import BriefService
from zep_context_agent.staleness import build_desk_queue
from zep_context_agent.store import InMemoryBriefStore

AS_OF = date(2026, 6, 1)
HORIZON = 45

#: The authored weights, restated here so a silent edit to
#: the ranking has to argue with this test too.
EXPECTED_WEIGHTS = {
    "stale_decisions": 100,
    "lapsed_facts": 40,
    "open_asks": 30,
    "expiring_facts": 20,
    "pending_candidates": 10,
    "no_decided_brief": 5,
}

#: Hand-computed from the planted signals below:
#:   PA  stale 1                                  -> 100
#:   PC  lapsed 1 + expiring 1 + never decided    ->  65
#:   PB  open ask 1                               ->  30
#:   PD  candidates 2 + never decided             ->  25
#:   PF  never decided (brief awaiting)           ->   5
#:   PG  clean decided                            ->   0
#:   PE  clean decided                            ->   0
#: Zero rows order by NAME: Delta (ACC-PG) before Echo
#: (ACC-PE) — id order would reverse them.
EXPECTED_ORDER = ["ACC-PA", "ACC-PC", "ACC-PB", "ACC-PD", "ACC-PF", "ACC-PG", "ACC-PE"]
EXPECTED_SCORES = {
    "ACC-PA": 100,
    "ACC-PC": 65,
    "ACC-PB": 30,
    "ACC-PD": 25,
    "ACC-PF": 5,
    "ACC-PG": 0,
    "ACC-PE": 0,
}
EXPECTED_SIGNALS = {
    "ACC-PA": {"stale_decisions": 1},
    "ACC-PC": {"lapsed_facts": 1, "expiring_facts": 1, "no_decided_brief": 1},
    "ACC-PB": {"open_asks": 1},
    "ACC-PD": {"pending_candidates": 2, "no_decided_brief": 1},
    "ACC-PF": {"no_decided_brief": 1},
    "ACC-PG": {},
    "ACC-PE": {},
}


def doc(doc_id, account_id, kind, title, received, assertions=(), text=""):
    return SourceDocument(
        doc_id=doc_id,
        account_id=account_id,
        kind=kind,
        title=title,
        received_at=received,
        text=text,
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


def submission(doc_id, account_id, name, *, omit=(), extra=()):
    """A submission carrying the complete expected file (the
    six predicates assembly looks for), open windows, minus
    any predicate in ``omit``."""
    values = {
        "legal_name": name,
        "risk_class": "light manufacturing",
        "annual_revenue_usd": 5000000,
        "primary_location": "1 Portfolio Way, Dayton, OH",
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
        f"Submission 2026 — {name}", date(2026, 5, 20),
        assertions=assertions + list(extra),
    )


def staged_world():
    """Seven accounts, one per signal (see the header table).

    PA's file arrives in two waves around a decision, the way
    the staleness scenario stages it: the account object in
    the service's registry is swapped for the fuller file and
    the endorsement is ingested onto the decision."""
    pa_phase1 = Account(
        account_id="ACC-PA",
        display_name="Portfolio Zulu Co",
        documents=[submission("DOC-PA-SUB", "ACC-PA", "Portfolio Zulu Co")],
    )
    pa_full = Account(
        account_id="ACC-PA",
        display_name="Portfolio Zulu Co",
        documents=[
            submission("DOC-PA-SUB", "ACC-PA", "Portfolio Zulu Co"),
            doc(
                "DOC-PA-END", "ACC-PA", "endorsement",
                "Endorsement — limit reduced, Portfolio Zulu Co",
                date(2026, 5, 16),
                assertions=[
                    fact("policy_limit_usd", 750000, date(2026, 5, 15),
                         unit="USD"),
                ],
            ),
        ],
    )
    staged = {
        "ACC-PA": pa_phase1,
        "ACC-PB": Account(
            account_id="ACC-PB",
            display_name="Portfolio Mike Co",
            documents=[
                submission("DOC-PB-SUB", "ACC-PB", "Portfolio Mike Co",
                           omit=("claims_count_3yr",)),
            ],
        ),
        "ACC-PC": Account(
            account_id="ACC-PC",
            display_name="Portfolio Alpha Co",
            documents=[
                submission("DOC-PC-SUB", "ACC-PC", "Portfolio Alpha Co",
                           omit=("policy_limit_usd",)),
                doc(
                    "DOC-PC-POL", "ACC-PC", "endorsement",
                    "Policy schedules — Portfolio Alpha Co",
                    date(2026, 1, 12),
                    assertions=[
                        fact("policy_limit_usd", 900000, date(2025, 6, 1),
                             date(2026, 5, 1), entity_ref="POL-PC-1",
                             unit="USD"),
                        fact("cyber_limit_usd", 500000, date(2026, 1, 1),
                             date(2026, 7, 1), entity_ref="POL-PC-2",
                             unit="USD"),
                    ],
                ),
            ],
        ),
        "ACC-PD": Account(
            account_id="ACC-PD",
            display_name="Portfolio Bravo Co",
            documents=[
                submission("DOC-PD-SUB", "ACC-PD", "Portfolio Bravo Co"),
                doc(
                    "DOC-PD-COR", "ACC-PD", "correspondence",
                    "Broker letter — deductible and sprinklers",
                    date(2026, 5, 25),
                    text="Confirming the deductible and the sprinkler "
                    "position for the file.",
                    assertions=[
                        fact("deductible_usd", 25000, date(2026, 1, 1),
                             unit="USD"),
                        fact("sprinkler_coverage_pct", 95, date(2026, 1, 1),
                             unit="%"),
                    ],
                ),
            ],
        ),
        "ACC-PE": Account(
            account_id="ACC-PE",
            display_name="Portfolio Echo Co",
            documents=[submission("DOC-PE-SUB", "ACC-PE", "Portfolio Echo Co")],
        ),
        "ACC-PF": Account(
            account_id="ACC-PF",
            display_name="Portfolio Charlie Co",
            documents=[
                submission("DOC-PF-SUB", "ACC-PF", "Portfolio Charlie Co")
            ],
        ),
        "ACC-PG": Account(
            account_id="ACC-PG",
            display_name="Portfolio Delta Co",
            documents=[submission("DOC-PG-SUB", "ACC-PG", "Portfolio Delta Co")],
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
    # PA: decide on the first wave, then the endorsement lands.
    brief_pa = service.assemble("ACC-PA", as_of=date(2026, 5, 1))
    service.decide(
        brief_pa.brief_id,
        action="proceed",
        actor="Test Underwriter",
        reason="The file as it stood.",
    )
    staged["ACC-PA"] = pa_full
    service.ingest("ACC-PA")
    # PB: a request_info decision on an incomplete file opens the ask.
    brief_pb = service.assemble("ACC-PB", as_of=AS_OF)
    service.decide(
        brief_pb.brief_id,
        action="request_info",
        actor="Test Underwriter",
        reason="Claims history outstanding.",
    )
    # PC and PD: ingestion only — the watch and the gate do the rest.
    service.ingest("ACC-PC")
    service.ingest("ACC-PD")
    # PE and PG: decided, nothing lands after — clean.
    for account_id in ("ACC-PE", "ACC-PG"):
        brief = service.assemble(account_id, as_of=AS_OF)
        service.decide(
            brief.brief_id,
            action="proceed",
            actor="Test Underwriter",
            reason="Complete file; nothing outstanding.",
        )
    # PF: assembled, never decided — awaiting at the gate.
    service.assemble("ACC-PF", as_of=AS_OF)
    return service, staged


@pytest.fixture(scope="module")
def world():
    return staged_world()


def build(service, **kwargs):
    kwargs.setdefault("as_of", AS_OF)
    kwargs.setdefault("horizon_days", HORIZON)
    return service.portfolio_brief(**kwargs)


def test_signals_are_the_owners_verdicts(world):
    service, _ = world
    brief = build(service)
    for row in brief.rows:
        got = {
            name: getattr(row.signals, name)
            for name in EXPECTED_WEIGHTS
            if getattr(row.signals, name)
        }
        assert got == EXPECTED_SIGNALS[row.account_id], row.account_id


def test_ranking_is_the_published_weights_over_the_counts(world):
    service, _ = world
    brief = build(service)
    assert brief.weights == EXPECTED_WEIGHTS == PORTFOLIO_WEIGHTS
    assert [r.account_id for r in brief.rows] == EXPECTED_ORDER
    assert {r.account_id: r.score for r in brief.rows} == EXPECTED_SCORES
    for row in brief.rows:
        assert row.score == sum(
            brief.weights[name] * getattr(row.signals, name)
            for name in EXPECTED_WEIGHTS
        )


def test_zero_signal_accounts_close_the_list_by_name(world):
    service, _ = world
    brief = build(service)
    tail = brief.rows[-2:]
    # Name order (Delta before Echo), NOT id order (PE before PG).
    assert [r.account_id for r in tail] == ["ACC-PG", "ACC-PE"]
    for row in tail:
        assert row.score == 0
        assert row.signals.model_dump() == {
            name: 0 for name in EXPECTED_WEIGHTS
        }


def test_never_decided_is_distinct_from_clean(world):
    service, _ = world
    brief = build(service)
    by_id = {r.account_id: r for r in brief.rows}
    # Awaiting at the gate is still unexamined.
    assert by_id["ACC-PF"].signals.no_decided_brief == 1
    # Decided and quiet is the only zero there is.
    assert by_id["ACC-PE"].signals.no_decided_brief == 0
    assert by_id["ACC-PG"].signals.no_decided_brief == 0
    # And the unexamined outrank the clean.
    assert EXPECTED_ORDER.index("ACC-PF") < EXPECTED_ORDER.index("ACC-PG")


def test_expiring_count_rides_the_watch_horizon(world):
    service, _ = world
    wide = build(service, horizon_days=45)
    narrow = build(service, horizon_days=10)
    pc_wide = next(r for r in wide.rows if r.account_id == "ACC-PC")
    pc_narrow = next(r for r in narrow.rows if r.account_id == "ACC-PC")
    # The cyber fact closes 30 days out: inside 45, outside 10.
    # The lapsed fact does not move — a lapse is not a horizon call.
    assert pc_wide.signals.expiring_facts == 1
    assert pc_narrow.signals.expiring_facts == 0
    assert pc_narrow.signals.lapsed_facts == 1
    assert pc_narrow.score == pc_wide.score - EXPECTED_WEIGHTS["expiring_facts"]


def _snapshot(service, staged) -> tuple:
    queue = build_desk_queue(
        staged,
        service.store,
        service.staleness_store,
        follow_store=service.follow_store,
        today=AS_OF,
        port=service.port,
    )
    return (
        [e.model_dump_json() for e in queue],
        [
            r.model_dump_json()
            for r in service.expiry_watch(as_of=AS_OF, horizon_days=HORIZON)
        ],
        [b.model_dump_json() for b in service.list_briefs()],
        [r.model_dump_json() for r in service.list_staleness()],
        [r.model_dump_json() for r in service.list_follow_through()],
        {
            aid: [
                (c.candidate_id, c.state)
                for c in service.list_candidates(aid)
            ]
            for aid in staged
        },
        {
            aid: sorted(f.fact_id for f in service.port.get_facts(aid))
            for aid in staged
        },
    )


def test_brief_is_pure_and_leaves_the_queue_untouched(world):
    service, staged = world
    before = _snapshot(service, staged)
    first = build(service)
    second = build(service)
    assert _snapshot(service, staged) == before
    # Byte-deterministic given the store: no wall clock inside.
    assert first.model_dump_json() == second.model_dump_json()


def test_portfolio_module_directly(world):
    service, staged = world
    brief = portfolio_mod.build_portfolio_brief(
        port=service.port,
        accounts=staged,
        brief_store=service.store,
        staleness_store=service.staleness_store,
        follow_store=service.follow_store,
        as_of=AS_OF,
        horizon_days=HORIZON,
    )
    assert [r.account_id for r in brief.rows] == EXPECTED_ORDER
    text = portfolio_mod.render_portfolio_text(brief)
    assert "stale_decisions 100" in text
    assert "Portfolio Zulu Co" in text.splitlines()[3]


def test_api_serves_the_brief_with_its_weights(world):
    service, _ = world
    client = TestClient(create_app(service))
    resp = client.get("/portfolio", params={"as_of": AS_OF.isoformat()})
    assert resp.status_code == 200
    body = resp.json()
    assert body["weights"] == EXPECTED_WEIGHTS
    assert body["as_of"] == AS_OF.isoformat()
    assert [r["account_id"] for r in body["rows"]] == EXPECTED_ORDER
    top = body["rows"][0]
    assert top["signals"]["stale_decisions"] == 1 and top["score"] == 100
