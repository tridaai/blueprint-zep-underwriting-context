"""The duplicate watch: the same insured, twice.

Pins, by layer:

- detection — candidate pairs are computed from CURRENT
  facts only: normalised legal-name similarity at or over
  the one published threshold AND a corroborating signal.
  A bare name similarity never flags (the identical-name
  pair below, different everything, does not flag); one
  weak signal does not flag (the same-state-only pair);
  corroboration without the name does not flag (the
  same-broker, same-class, near-revenue pair with dissimilar
  names). A single strong signal does flag (the same-broker
  pair; the same-location pair);
- evidence — every flag carries both sides' own values, the
  similarity figure, and the corroborators that fired; a
  side's values equal that account's own resolved facts,
  read back through the port — nothing crosses accounts;
- verdicts — append-only records: refusals name their cause
  (unflagged pair, same account, missing actor or note);
  DISTINCT suppresses while the evidence fingerprint stands
  and a material fact change re-flags the pair as new;
  SAME_INSURED changes nothing operational — briefs, facts,
  and the desk queue byte-identical around the act;
- the exposure reading — a confirmed pair annotates the
  portfolio brief (counted, named, with its verdict) while
  every row, score, and weight stands exactly as it was;
  an unverdicted flag never annotates;
- the seeded book — with the desk's review pass performed,
  exactly one pair flags: the two Meridian accounts, on
  their two weak corroborators;
- the surfaces — the API serves the watch and takes the
  verdict; the CLI prints the same watch and records the
  same act.

The staged world: three pairs that must flag (two weak
signals; one strong location; one strong broker) and three
that must not (identical names, no corroboration; one weak
signal only; full corroboration, dissimilar names).
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver

from zep_context_agent import cli as cli_mod
from zep_context_agent import duplicates as dup_mod
from zep_context_agent.api import create_app
from zep_context_agent.datasets import DATASET_PRESENT, load_accounts
from zep_context_agent.errors import AccountNotFound, DuplicateError
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


def fact(predicate, value, valid_from, unit=""):
    return FactAssertion(
        predicate=predicate,
        value=value,
        unit=unit,
        valid_from=valid_from,
    )


def submission(doc_id, account_id, name, location, risk_class, revenue, broker):
    return SourceDocument(
        doc_id=doc_id,
        account_id=account_id,
        kind="submission",
        title=f"Submission 2026 — {name}",
        received_at=date(2026, 5, 20),
        assertions=[
            fact("legal_name", name, date(2026, 1, 1)),
            fact("risk_class", risk_class, date(2026, 1, 1)),
            fact(
                "annual_revenue_usd",
                revenue,
                date(2026, 1, 1),
                unit="USD",
            ),
            fact("primary_location", location, date(2026, 1, 1)),
            fact("policy_limit_usd", 1000000, date(2026, 1, 1), unit="USD"),
            fact("claims_count_3yr", 1, date(2026, 1, 1)),
        ],
        edges=[
            EdgeAssertion(
                source_ref=account_id,
                edge_type="placed_by",
                target_ref=broker[0],
                label=broker[1],
            )
        ],
    )


def staged_accounts():
    return {
        # Flags: two weak corroborators (same state, same
        # risk-class line), like the seeded Meridian pair.
        "ACC-DP1": Account(
            account_id="ACC-DP1",
            display_name="Keystone Castings LLC",
            documents=[
                submission(
                    "DOC-DP1-SUB", "ACC-DP1", "Keystone Castings LLC",
                    "400 Quarry Road, Erie, PA",
                    "Product liability — fabricated metal products",
                    12000000, ("BRK-DA", "Test Delta A Brokers"),
                )
            ],
        ),
        "ACC-DP2": Account(
            account_id="ACC-DP2",
            display_name="Keystone Casting Co.",
            documents=[
                submission(
                    "DOC-DP2-SUB", "ACC-DP2", "Keystone Casting Co.",
                    "12 Foundry Street, Pittsburgh, PA",
                    "Product liability — consumer goods",
                    4000000, ("BRK-DB", "Test Delta B Brokers"),
                )
            ],
        ),
        # Does NOT flag: identical normalised names, and
        # different everything else — no corroborator at all.
        "ACC-DP3": Account(
            account_id="ACC-DP3",
            display_name="Harbor Point Logistics LLC",
            documents=[
                submission(
                    "DOC-DP3-SUB", "ACC-DP3", "Harbor Point Logistics LLC",
                    "1 Dock Way, Seattle, WA",
                    "Cargo — freight forwarding",
                    30000000, ("BRK-DC", "Test Delta C Brokers"),
                )
            ],
        ),
        "ACC-DP4": Account(
            account_id="ACC-DP4",
            display_name="Harbor Point Logistics Inc",
            documents=[
                submission(
                    "DOC-DP4-SUB", "ACC-DP4", "Harbor Point Logistics Inc",
                    "9 Pier Road, Miami, FL",
                    "Property — warehousing",
                    2000000, ("BRK-DD", "Test Delta D Brokers"),
                )
            ],
        ),
        # Flags: the same primary location, exactly (strong).
        "ACC-DP5": Account(
            account_id="ACC-DP5",
            display_name="Summit Abrasives LLC",
            documents=[
                submission(
                    "DOC-DP5-SUB", "ACC-DP5", "Summit Abrasives LLC",
                    "77 Grit Lane, Akron, OH",
                    "Abrasives — manufacturing",
                    8000000, ("BRK-DE", "Test Delta E Brokers"),
                )
            ],
        ),
        "ACC-DP6": Account(
            account_id="ACC-DP6",
            display_name="Summit Abrasive Co.",
            documents=[
                submission(
                    "DOC-DP6-SUB", "ACC-DP6", "Summit Abrasive Co.",
                    "77 Grit Lane, Akron, OH",
                    "Abrasives — distribution",
                    3000000, ("BRK-DF", "Test Delta F Brokers"),
                )
            ],
        ),
        # Does NOT flag: near-identical names and exactly one
        # weak corroborator (the same state).
        "ACC-DP7": Account(
            account_id="ACC-DP7",
            display_name="Bluepeak Polymers LLC",
            documents=[
                submission(
                    "DOC-DP7-SUB", "ACC-DP7", "Bluepeak Polymers LLC",
                    "1 Resin Road, Houston, TX",
                    "Product liability — plastics",
                    6000000, ("BRK-DG", "Test Delta G Brokers"),
                )
            ],
        ),
        "ACC-DP8": Account(
            account_id="ACC-DP8",
            display_name="Bluepeak Polymer Co.",
            documents=[
                submission(
                    "DOC-DP8-SUB", "ACC-DP8", "Bluepeak Polymer Co.",
                    "9 Resin Road, Dallas, TX",
                    "Environmental — remediation",
                    7000000, ("BRK-DH", "Test Delta H Brokers"),
                )
            ],
        ),
        # Does NOT flag: same broker, same risk class, revenue
        # within ten percent — full corroboration — but the
        # names are not similar. The name gate holds both ways.
        "ACC-DP9": Account(
            account_id="ACC-DP9",
            display_name="Northfield Aggregates LLC",
            documents=[
                submission(
                    "DOC-DP9-SUB", "ACC-DP9", "Northfield Aggregates LLC",
                    "5 Quarry Pit, Duluth, MN",
                    "Product liability — aggregates",
                    10000000, ("BRK-DI", "Test Delta I Brokers"),
                )
            ],
        ),
        "ACC-DP10": Account(
            account_id="ACC-DP10",
            display_name="Southbrook Quarries Inc",
            documents=[
                submission(
                    "DOC-DP10-SUB", "ACC-DP10", "Southbrook Quarries Inc",
                    "8 Stone Way, Duluth, MN",
                    "Product liability — aggregates",
                    10500000, ("BRK-DI", "Test Delta I Brokers"),
                )
            ],
        ),
        # Flags: the same broker of record (strong), alone —
        # different states, different risk-class lines.
        "ACC-DP11": Account(
            account_id="ACC-DP11",
            display_name="Cascade Foundry Works LLC",
            documents=[
                submission(
                    "DOC-DP11-SUB", "ACC-DP11", "Cascade Foundry Works LLC",
                    "3 Mill Street, Portland, OR",
                    "Foundry — castings",
                    15000000, ("BRK-DJ", "Test Delta J Brokers"),
                )
            ],
        ),
        "ACC-DP12": Account(
            account_id="ACC-DP12",
            display_name="Cascade Foundry Works Inc",
            documents=[
                submission(
                    "DOC-DP12-SUB", "ACC-DP12", "Cascade Foundry Works Inc",
                    "21 Dockside Avenue, Boise, ID",
                    "Marine — hull repair",
                    2500000, ("BRK-DJ", "Test Delta J Brokers"),
                )
            ],
        ),
    }


def make_service(accounts) -> BriefService:
    service = BriefService(
        port=OfflineContextGraph(),
        store=InMemoryBriefStore(),
        object_store=None,
        checkpointer=MemorySaver(),
        cache=None,
        accounts=accounts,
    )
    for account_id in sorted(accounts):
        service.ingest(account_id)
    return service


@pytest.fixture(scope="module")
def world():
    accounts = staged_accounts()
    return make_service(accounts), accounts


def pair_of(watch, account_a, account_b):
    return next(
        (
            p
            for p in watch.pairs
            if (p.account_a, p.account_b) == (account_a, account_b)
        ),
        None,
    )


def flagged_pairs(watch):
    return [(p.account_a, p.account_b) for p in watch.pairs]


# -- detection ---------------------------------------------------------------


def test_detection_flags_only_corroborated_similar_pairs(world):
    service, _ = world
    watch = service.duplicate_watch(as_of=AS_OF)
    assert flagged_pairs(watch) == [
        ("ACC-DP11", "ACC-DP12"),  # similarity 1.00, same broker
        ("ACC-DP1", "ACC-DP2"),  # two weak signals
        ("ACC-DP5", "ACC-DP6"),  # same location
    ]
    assert all(p.state == "FLAGGED" for p in watch.pairs)
    assert watch.threshold == dup_mod.NAME_SIMILARITY_THRESHOLD == 0.90


def test_bare_name_similarity_never_flags(world):
    service, _ = world
    # The names are identical once normalised — similarity
    # 1.0 — and still no flag: no corroborator fires.
    assert (
        dup_mod.name_similarity(
            "Harbor Point Logistics LLC", "Harbor Point Logistics Inc"
        )
        == 1.0
    )
    watch = service.duplicate_watch(as_of=AS_OF)
    assert pair_of(watch, "ACC-DP3", "ACC-DP4") is None
    # One weak signal is not corroboration either.
    assert pair_of(watch, "ACC-DP7", "ACC-DP8") is None
    # And corroboration without the name is not a flag.
    assert pair_of(watch, "ACC-DP9", "ACC-DP10") is None


def test_a_single_strong_signal_flags(world):
    service, _ = world
    watch = service.duplicate_watch(as_of=AS_OF)
    broker_pair = pair_of(watch, "ACC-DP11", "ACC-DP12")
    assert [c.kind for c in broker_pair.corroborators] == ["same_broker"]
    location_pair = pair_of(watch, "ACC-DP5", "ACC-DP6")
    kinds = {c.kind: c.strength for c in location_pair.corroborators}
    assert kinds["same_location"] == "strong"


def test_the_flag_carries_its_evidence(world):
    service, accounts = world
    watch = service.duplicate_watch(as_of=AS_OF)
    pair = pair_of(watch, "ACC-DP1", "ACC-DP2")
    assert pair.name_similarity >= 0.90
    assert {c.kind for c in pair.corroborators} == {
        "same_state",
        "same_risk_line",
    }
    details = " ".join(c.detail for c in pair.corroborators)
    assert "PA" in details and "Product liability" in details
    assert len(pair.fingerprint) == 64
    # Each side's evidence is that account's own current
    # facts, read back through the port — nothing crossed.
    from zep_context_agent import temporal

    for side in pair.sides:
        current, _ = temporal.resolve(
            service.port.get_facts(side.account_id), AS_OF
        )
        own = {f.predicate: f.value for f in current}
        assert side.legal_name == own["legal_name"]
        assert side.primary_location == own["primary_location"]
        assert side.risk_class == own["risk_class"]
        assert side.annual_revenue_usd == own["annual_revenue_usd"]


# -- verdicts ------------------------------------------------------------------


@pytest.fixture()
def fresh_world():
    accounts = staged_accounts()
    return make_service(accounts), accounts


def test_verdict_refusals_name_their_cause(fresh_world):
    service, _ = fresh_world
    with pytest.raises(DuplicateError, match="not a flagged pair"):
        service.record_duplicate_verdict(
            "ACC-DP3",
            "ACC-DP4",
            verdict="DISTINCT",
            actor="Test Underwriter",
            note="Different insureds.",
        )
    with pytest.raises(DuplicateError, match="its own duplicate"):
        service.record_duplicate_verdict(
            "ACC-DP1",
            "ACC-DP1",
            verdict="DISTINCT",
            actor="Test Underwriter",
            note="n/a",
        )
    with pytest.raises(DuplicateError, match="underwriter's name"):
        service.record_duplicate_verdict(
            "ACC-DP1",
            "ACC-DP2",
            verdict="DISTINCT",
            actor=" ",
            note="Different insureds.",
        )
    with pytest.raises(DuplicateError, match="needs its note"):
        service.record_duplicate_verdict(
            "ACC-DP1",
            "ACC-DP2",
            verdict="SAME_INSURED",
            actor="Test Underwriter",
            note="",
        )
    with pytest.raises(AccountNotFound):
        service.record_duplicate_verdict(
            "ACC-DP1",
            "ACC-NOPE",
            verdict="DISTINCT",
            actor="Test Underwriter",
            note="Different insureds.",
        )
    # Every refusal recorded nothing.
    assert service.list_duplicate_verdicts() == []


def test_distinct_suppresses_until_the_facts_move(fresh_world):
    service, accounts = fresh_world
    record = service.record_duplicate_verdict(
        "ACC-DP2",  # either order — the record stores them sorted
        "ACC-DP1",
        verdict="DISTINCT",
        actor="Test Underwriter",
        note="Different insureds — separate Pennsylvania corporations.",
    )
    assert (record.account_a, record.account_b) == ("ACC-DP1", "ACC-DP2")
    assert len(record.fingerprint) == 64
    watch = service.duplicate_watch(as_of=AS_OF)
    pair = pair_of(watch, "ACC-DP1", "ACC-DP2")
    assert pair.state == "DISTINCT" and not pair.reflagged

    # A material fact change: DP2's location is restated (a
    # different Pittsburgh street becomes a new Erie address —
    # same state, so the pair still flags, on new evidence).
    accounts["ACC-DP2"].documents.append(
        SourceDocument(
            doc_id="DOC-DP2-END",
            account_id="ACC-DP2",
            kind="endorsement",
            title="Endorsement — location restated, Keystone Casting Co.",
            received_at=date(2026, 5, 28),
            assertions=[
                fact(
                    "primary_location",
                    "500 Quarry Road, Erie, PA",
                    date(2026, 5, 1),
                )
            ],
        )
    )
    service.ingest("ACC-DP2")
    watch = service.duplicate_watch(as_of=AS_OF)
    pair = pair_of(watch, "ACC-DP1", "ACC-DP2")
    assert pair.state == "FLAGGED" and pair.reflagged
    assert pair.fingerprint != record.fingerprint
    # The old verdict rides the re-flag as history, and the
    # history itself is append-only: one record, unedited.
    assert pair.verdict.record_id == record.record_id
    assert len(service.list_duplicate_verdicts()) == 1

    # A fresh verdict on the new evidence stands.
    service.record_duplicate_verdict(
        "ACC-DP1",
        "ACC-DP2",
        verdict="SAME_INSURED",
        actor="Test Underwriter",
        note="On the restated file: one insured, placed twice.",
    )
    watch = service.duplicate_watch(as_of=AS_OF)
    assert pair_of(watch, "ACC-DP1", "ACC-DP2").state == "CONFIRMED_SAME"
    assert len(service.list_duplicate_verdicts()) == 2


def snapshots(service, accounts):
    return {
        "briefs": [b.model_dump_json() for b in service.store.list()],
        "facts": {
            aid: [f.model_dump_json() for f in service.port.get_facts(aid)]
            for aid in sorted(accounts)
        },
        "queue": [e.model_dump_json() for e in service.desk_queue()],
    }


def test_same_insured_changes_nothing_operational(fresh_world):
    service, accounts = fresh_world
    # Two decided briefs on the book, so the pin covers the
    # record a verdict might be tempted to touch.
    for account_id in ("ACC-DP1", "ACC-DP2"):
        brief = service.assemble(account_id, as_of=AS_OF)
        service.decide(
            brief.brief_id,
            action="proceed",
            actor="Test Underwriter",
            reason="The file as it stood.",
        )
    before = snapshots(service, accounts)
    service.record_duplicate_verdict(
        "ACC-DP1",
        "ACC-DP2",
        verdict="SAME_INSURED",
        actor="Test Underwriter",
        note="One insured, placed twice — merge in the system of record.",
    )
    after = snapshots(service, accounts)
    # Briefs byte-identical, facts unmoved, and the desk
    # queue — order and rows — exactly as it was: pair work
    # is book hygiene, not account urgency.
    assert after == before


def test_confirmed_pair_annotates_the_portfolio_brief_only(fresh_world):
    service, _ = fresh_world
    before = service.portfolio_brief(as_of=AS_OF)
    assert before.duplicates.pair_count == 0
    service.record_duplicate_verdict(
        "ACC-DP1",
        "ACC-DP2",
        verdict="SAME_INSURED",
        actor="Test Underwriter",
        note="One insured, placed twice.",
    )
    after = service.portfolio_brief(as_of=AS_OF)
    annotation = after.duplicates
    assert annotation.pair_count == 1
    assert annotation.account_count == 2
    assert annotation.pairs[0].account_a == "ACC-DP1"
    assert annotation.pairs[0].account_b == "ACC-DP2"
    assert annotation.pairs[0].actor == "Test Underwriter"
    # The unverdicted Summit pair does NOT annotate — a
    # suspicion does not print as fact.
    assert all(
        p.account_a != "ACC-DP5" for p in annotation.pairs
    )
    # Rows, scores, order, weights: exactly as they were.
    assert [r.model_dump() for r in after.rows] == [
        r.model_dump() for r in before.rows
    ]
    assert after.weights == before.weights


def test_watch_and_brief_are_pure_projections(fresh_world):
    service, accounts = fresh_world
    before = snapshots(service, accounts)
    first = service.duplicate_watch(as_of=AS_OF)
    second = service.duplicate_watch(as_of=AS_OF)
    assert first.model_dump_json() == second.model_dump_json()
    service.portfolio_brief(as_of=AS_OF)
    assert snapshots(service, accounts) == before
    assert service.list_duplicate_verdicts() == []


# -- the seeded book -------------------------------------------------------------


@pytest.fixture(scope="module")
def seeded_world():
    accounts = load_accounts()
    service = make_service(accounts)
    # The desk's review pass, as the eval runner performs it:
    # correspondence facts stand only once confirmed.
    for account_id in sorted(accounts):
        for candidate in service.list_candidates(
            account_id, state="pending"
        ):
            service.review_candidate(
                candidate.candidate_id,
                account_id=account_id,
                action="confirm",
                actor="Test Desk",
            )
    return service, accounts


def test_seeded_book_flags_exactly_the_meridian_pair(seeded_world):
    service, _ = seeded_world
    watch = service.duplicate_watch(as_of=DATASET_PRESENT)
    assert flagged_pairs(watch) == [("ACC-001", "ACC-002")]
    pair = watch.pairs[0]
    assert pair.name_similarity > 0.96
    assert {c.kind for c in pair.corroborators} == {
        "same_state",
        "same_risk_line",
    }
    sides = {s.account_id: s for s in pair.sides}
    one, two = sides["ACC-001"], sides["ACC-002"]
    assert one.legal_name == "Meridian Casting Co."
    assert one.primary_location == "2280 Industrial Pkwy, Pittsburgh, PA"
    assert one.risk_class == "Product liability — fabricated metal products"
    assert one.annual_revenue_usd == 21900000
    assert one.broker_id == "BRK-ALDER"
    assert two.legal_name == "Meridian Castings LLC"
    assert two.primary_location == "77 Quarry Bend, Erie, PA"
    assert two.risk_class == "Product liability — consumer goods"
    assert two.annual_revenue_usd == 9600000
    assert two.broker_id == "BRK-HARBOR"


# -- the surfaces ------------------------------------------------------------------


def test_api_serves_the_watch_and_takes_the_verdict(fresh_world):
    service, _ = fresh_world
    client = TestClient(create_app(service))
    watch = client.get("/duplicates", params={"as_of": str(AS_OF)})
    assert watch.status_code == 200
    payload = watch.json()
    assert ("ACC-DP1", "ACC-DP2") in [
        (p["account_a"], p["account_b"]) for p in payload["pairs"]
    ]
    assert payload["threshold"] == 0.90

    created = client.post(
        "/duplicates/verdict",
        json={
            "account_a": "ACC-DP1",
            "account_b": "ACC-DP2",
            "verdict": "SAME_INSURED",
            "actor": "Test Underwriter",
            "note": "One insured, placed twice.",
        },
    )
    assert created.status_code == 201
    assert created.json()["record_id"].startswith("dup-")

    watch = client.get("/duplicates", params={"as_of": str(AS_OF)}).json()
    pair = next(
        p
        for p in watch["pairs"]
        if (p["account_a"], p["account_b"]) == ("ACC-DP1", "ACC-DP2")
    )
    assert pair["state"] == "CONFIRMED_SAME"
    history = client.get("/duplicates/verdicts").json()
    assert len(history) == 1 and history[0]["verdict"] == "SAME_INSURED"

    refused = client.post(
        "/duplicates/verdict",
        json={
            "account_a": "ACC-DP3",
            "account_b": "ACC-DP4",
            "verdict": "DISTINCT",
            "actor": "Test Underwriter",
            "note": "Different insureds.",
        },
    )
    assert refused.status_code == 409
    assert "not a flagged pair" in refused.json()["detail"]


def test_cli_prints_the_watch_and_records_the_verdict(
    fresh_world, monkeypatch, capsys
):
    service, _ = fresh_world
    monkeypatch.setattr(cli_mod, "_service", lambda: service)
    assert cli_mod.main(["duplicates", "--as-of", str(AS_OF)]) == 0
    out = capsys.readouterr().out
    assert "DUPLICATE WATCH" in out
    assert "ACC-DP1 × ACC-DP2" in out
    assert "same risk-class line: Product liability" in out

    assert (
        cli_mod.main(
            [
                "duplicate-verdict",
                "ACC-DP1",
                "ACC-DP2",
                "same_insured",
                "--actor",
                "Test Underwriter",
                "--note",
                "One insured, placed twice.",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert out.startswith("SAME_INSURED — ACC-DP1 × ACC-DP2")
    watch = service.duplicate_watch(as_of=AS_OF)
    assert pair_of(watch, "ACC-DP1", "ACC-DP2").state == "CONFIRMED_SAME"
