"""Support grading: how well-backed each current fact is.

Pins, by layer:

- the algebra, on hand-built fact sets — the grade is the
  citing tier lifted by independent corroboration (distinct
  documents, in force, agreeing) and demoted one step by a
  contest (an earlier, different value this fact displaced).
  A broker's letter caps at STANDARD however corroborated;
  the system's own record starts at STRONG. Every expected
  grade below is derived from that statement by hand.
- independence is honest — the same document asserting twice
  is one document; a closed window neither supports nor
  contests; a future document does neither either.
- the upgrade — a fact whose second supporting document
  arrives grades higher on re-assembly, while the DECIDED
  brief that carried the old grade stays byte-identical.
- no decision impact — a brief full of thin facts decides
  exactly like any other; the grade feeds no gate.
- the surfaces — the brief renders its soft spots (thin
  facts only, the displaced value named; one line when none;
  an honest "not graded" for a pre-grading record), the desk
  queue counts thin facts when built with the port and says
  None without it, and the portfolio displays the count
  unweighted: a decided-clean account with thin facts still
  scores zero.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from langgraph.checkpoint.memory import MemorySaver

from zep_context_agent import support as support_mod
from zep_context_agent.offline_graph import OfflineContextGraph
from zep_context_agent.ontology import Account, Fact, FactAssertion, SourceDocument
from zep_context_agent.portfolio import build_portfolio_brief
from zep_context_agent.render import render_brief_text
from zep_context_agent.schemas import Brief, CitedFact
from zep_context_agent.service import BriefService
from zep_context_agent.staleness import build_desk_queue
from zep_context_agent.store import InMemoryBriefStore

AS_OF = date(2026, 6, 1)


# -- the algebra, on hand-built facts ---------------------------------------


def make_fact(
    fact_id,
    doc_id,
    predicate,
    value,
    *,
    origin="filed_document",
    valid_from=date(2026, 1, 1),
    valid_to=None,
    recorded_at=None,
    entity_ref="",
):
    return Fact(
        fact_id=fact_id,
        account_id="ACC-T",
        predicate=predicate,
        value=value,
        unit="",
        entity_ref=entity_ref,
        valid_from=valid_from,
        valid_to=valid_to,
        recorded_at=recorded_at or valid_from,
        episode_id=f"ep-{doc_id}",
        doc_id=doc_id,
        origin=origin,
    )


def grade_of(facts, predicate):
    graded = support_mod.assess_support(facts, AS_OF)
    (support,) = [
        s
        for fid, s in graded.items()
        if next(f for f in facts if f.fact_id == fid).predicate == predicate
    ]
    return support


def test_system_record_is_strong():
    facts = [
        make_fact("f1", "DOC-MEMO", "underwriter_decision", "proceed",
                  origin="system_record", entity_ref="brief-1")
    ]
    support = grade_of(facts, "underwriter_decision")
    assert support.grade == "STRONG"
    assert support.tier == "system_record"
    assert support.reasons == ["the system's own record"]


def test_single_filed_document_is_standard():
    facts = [make_fact("f1", "DOC-SUB", "annual_revenue_usd", 8000000)]
    support = grade_of(facts, "annual_revenue_usd")
    assert support.grade == "STANDARD"
    assert support.supporting_documents == 1
    assert support.contested is False


def test_second_independent_document_upgrades_to_strong():
    facts = [
        make_fact("f1", "DOC-SUB", "annual_revenue_usd", 8000000),
        make_fact("f2", "DOC-CLM", "annual_revenue_usd", 8000000,
                  valid_from=date(2026, 3, 1)),
    ]
    support = grade_of(facts, "annual_revenue_usd")
    assert support.grade == "STRONG"
    assert support.supporting_documents == 2


def test_the_same_document_twice_is_still_one_document():
    facts = [
        make_fact("f1", "DOC-SUB", "annual_revenue_usd", 8000000),
        make_fact("f2", "DOC-SUB", "annual_revenue_usd", 8000000,
                  valid_from=date(2026, 3, 1)),
    ]
    support = grade_of(facts, "annual_revenue_usd")
    assert support.grade == "STANDARD"
    assert support.supporting_documents == 1


def test_a_closed_window_neither_supports_nor_contests():
    facts = [
        make_fact("f1", "DOC-OLD", "annual_revenue_usd", 8000000,
                  valid_from=date(2024, 1, 1), valid_to=date(2025, 1, 1)),
        make_fact("f2", "DOC-SUB", "annual_revenue_usd", 8000000,
                  valid_from=date(2025, 1, 1)),
    ]
    support = grade_of(facts, "annual_revenue_usd")
    assert support.grade == "STANDARD"
    assert support.supporting_documents == 1
    assert support.contested is False


def test_single_correspondence_source_is_thin():
    facts = [
        make_fact("f1", "DOC-COR", "primary_location", "9 Letter Lane",
                  origin="correspondence")
    ]
    support = grade_of(facts, "primary_location")
    assert support.grade == "THIN"
    assert support.reasons == ["a single correspondence source"]


def test_a_corroborated_letter_caps_at_standard():
    facts = [
        make_fact("f1", "DOC-INS", "deductible_usd", 10000,
                  valid_from=date(2026, 1, 15)),
        make_fact("f2", "DOC-COR", "deductible_usd", 10000,
                  origin="correspondence", valid_from=date(2026, 2, 1)),
    ]
    support = grade_of(facts, "deductible_usd")
    # The letter wins on valid_from; two documents agree; the
    # tier caps the grade below STRONG.
    assert support.tier == "correspondence"
    assert support.supporting_documents == 2
    assert support.grade == "STANDARD"


def test_contest_demotes_standard_to_thin_and_names_the_displaced():
    facts = [
        make_fact("f1", "DOC-END-1", "policy_limit_usd", 2000000,
                  valid_from=date(2026, 1, 1), valid_to=date(2026, 3, 1),
                  entity_ref="POL-T"),
        make_fact("f2", "DOC-END-2", "policy_limit_usd", 1500000,
                  valid_from=date(2026, 3, 1), entity_ref="POL-T"),
    ]
    support = grade_of(
        facts, "policy_limit_usd"
    )
    assert support.grade == "THIN"
    assert support.contested is True
    assert support.contested_value == 2000000
    assert support.contested_doc_id == "DOC-END-1"
    assert "contested — an earlier value was replaced" in support.reasons


def test_contest_demotes_corroborated_strong_to_standard():
    facts = [
        make_fact("f1", "DOC-A", "policy_limit_usd", 100,
                  valid_from=date(2025, 1, 1), valid_to=date(2026, 1, 1),
                  entity_ref="POL-T"),
        make_fact("f2", "DOC-B", "policy_limit_usd", 200,
                  valid_from=date(2026, 1, 1), entity_ref="POL-T"),
        make_fact("f3", "DOC-C", "policy_limit_usd", 200,
                  valid_from=date(2026, 2, 1), entity_ref="POL-T"),
    ]
    support = grade_of(facts, "policy_limit_usd")
    assert support.supporting_documents == 2
    assert support.contested is True
    assert support.grade == "STANDARD"  # STRONG, demoted one step


def test_contest_demotes_the_system_record_to_standard():
    facts = [
        make_fact("f1", "DOC-MEMO-1", "underwriter_decision", "proceed",
                  origin="system_record", entity_ref="brief-1",
                  valid_from=date(2026, 1, 1), valid_to=date(2026, 2, 1)),
        make_fact("f2", "DOC-MEMO-2", "underwriter_decision", "escalate",
                  origin="system_record", entity_ref="brief-1",
                  valid_from=date(2026, 2, 1)),
    ]
    support = grade_of(facts, "underwriter_decision")
    assert support.grade == "STANDARD"
    assert support.contested is True


def test_a_future_document_neither_supports_nor_contests():
    facts = [
        make_fact("f1", "DOC-SUB", "policy_limit_usd", 1000000,
                  entity_ref="POL-T"),
        make_fact("f2", "DOC-FUT", "policy_limit_usd", 2000000,
                  valid_from=date(2027, 1, 1), entity_ref="POL-T"),
    ]
    support = grade_of(facts, "policy_limit_usd")
    assert support.grade == "STANDARD"
    assert support.supporting_documents == 1
    assert support.contested is False


def test_only_the_winner_is_graded_and_thin_count_reads_the_grades():
    facts = [
        make_fact("f1", "DOC-SUB", "legal_name", "Test Co"),
        make_fact("f2", "DOC-COR", "primary_location", "9 Letter Lane",
                  origin="correspondence"),
        make_fact("f3", "DOC-COR-2", "primary_location", "9 Letter Lane",
                  origin="correspondence", valid_from=date(2025, 1, 1),
                  valid_to=date(2026, 1, 1)),
    ]
    graded = support_mod.assess_support(facts, AS_OF)
    assert set(graded) == {"f1", "f2"}  # f3 lost; losers are not graded
    assert support_mod.thin_count(facts, AS_OF) == 1


# -- the staged world: upgrade, decisions, surfaces ---------------------------


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


def full_submission(doc_id, account_id, name, revenue=8000000):
    values = {
        "legal_name": name,
        "risk_class": "light manufacturing",
        "annual_revenue_usd": revenue,
        "primary_location": "1 Support Way, Dayton, OH",
        "policy_limit_usd": 1000000,
        "claims_count_3yr": 1,
    }
    return doc(
        doc_id, account_id, "submission", f"Submission 2026 — {name}",
        date(2026, 5, 20),
        assertions=[
            fact(p, v, date(2026, 1, 1),
                 unit="USD" if p.endswith("_usd") else "")
            for p, v in values.items()
        ],
    )


def upgrade_account(documents):
    return Account(
        account_id="ACC-U1",
        display_name="Support Upgrade Co",
        documents=documents,
    )


def letter_account():
    return Account(
        account_id="ACC-U2",
        display_name="Support Letter Co",
        documents=[
            doc(
                "DOC-U2-SUB", "ACC-U2", "submission",
                "Submission 2026 — Support Letter Co", date(2026, 5, 20),
                assertions=[
                    fact("legal_name", "Support Letter Co", date(2026, 1, 1)),
                    fact("risk_class", "light manufacturing", date(2026, 1, 1)),
                    fact("annual_revenue_usd", 4000000, date(2026, 1, 1),
                         unit="USD"),
                    fact("claims_count_3yr", 0, date(2026, 1, 1)),
                ],
            ),
            doc(
                "DOC-U2-END1", "ACC-U2", "endorsement",
                "Policy POL-U2 issued — limit 2,000,000", date(2026, 1, 12),
                assertions=[
                    fact("policy_limit_usd", 2000000, date(2026, 1, 1),
                         date(2026, 3, 1), entity_ref="POL-U2", unit="USD"),
                ],
            ),
            doc(
                "DOC-U2-END2", "ACC-U2", "endorsement",
                "Mid-term endorsement — limit reduced", date(2026, 3, 2),
                assertions=[
                    fact("policy_limit_usd", 1500000, date(2026, 3, 1),
                         entity_ref="POL-U2", unit="USD"),
                ],
            ),
            doc(
                "DOC-U2-INS", "ACC-U2", "inspection",
                "Inspection report — deductible confirmed", date(2026, 1, 20),
                assertions=[
                    fact("deductible_usd", 10000, date(2026, 1, 15),
                         unit="USD"),
                ],
            ),
            doc(
                "DOC-U2-COR1", "ACC-U2", "correspondence",
                "Broker letter — new premises", date(2026, 2, 10),
                text="Confirming the move to 9 Letter Lane.",
                assertions=[
                    fact("primary_location", "9 Letter Lane",
                         date(2026, 2, 1)),
                ],
            ),
            doc(
                "DOC-U2-COR2", "ACC-U2", "correspondence",
                "Broker letter — deductible", date(2026, 2, 12),
                text="The deductible is 10,000 as inspected.",
                assertions=[
                    fact("deductible_usd", 10000, date(2026, 2, 1),
                         unit="USD"),
                ],
            ),
        ],
    )


def make_service(accounts):
    return BriefService(
        port=OfflineContextGraph(),
        store=InMemoryBriefStore(),
        object_store=None,
        checkpointer=MemorySaver(),
        cache=None,
        accounts=accounts,
    )


def confirm_all(service, account_id):
    for candidate in service.list_candidates(account_id, state="pending"):
        service.review_candidate(
            candidate.candidate_id,
            account_id=account_id,
            action="confirm",
            actor="Test Desk",
        )


def grades_by_predicate(brief):
    return {
        f.predicate: f.support.grade
        for f in brief.current_facts
        if f.support is not None
    }


def test_upgrade_on_reassembly_and_the_decided_brief_untouched():
    accounts = {"ACC-U1": upgrade_account([full_submission("DOC-U1-SUB", "ACC-U1", "Support Upgrade Co")])}
    service = make_service(accounts)
    service.ingest("ACC-U1")
    first = service.assemble("ACC-U1", as_of=AS_OF)
    revenue = next(
        f for f in first.current_facts if f.predicate == "annual_revenue_usd"
    )
    assert revenue.support.grade == "STANDARD"
    assert revenue.support.supporting_documents == 1

    decided = service.decide(
        first.brief_id,
        action="proceed",
        actor="Test Underwriter",
        reason="The file as it stood.",
    )
    snapshot = decided.model_dump_json()

    # The second, independent document lands, agreeing exactly.
    accounts["ACC-U1"] = upgrade_account([
        full_submission("DOC-U1-SUB", "ACC-U1", "Support Upgrade Co"),
        doc(
            "DOC-U1-CLM", "ACC-U1", "claims_extract",
            "Claims extract — loss run to May 2026", date(2026, 5, 28),
            assertions=[
                fact("annual_revenue_usd", 8000000, date(2026, 5, 1),
                     unit="USD"),
            ],
        ),
    ])
    service.ingest("ACC-U1")
    fresh = service.assemble("ACC-U1", as_of=AS_OF)
    revenue = next(
        f for f in fresh.current_facts if f.predicate == "annual_revenue_usd"
    )
    assert revenue.support.grade == "STRONG"
    assert revenue.support.supporting_documents == 2

    # The decided brief is history: same bytes, same old grade.
    stored = service.get_brief(decided.brief_id)
    assert stored.model_dump_json() == snapshot
    old = next(
        f for f in stored.current_facts
        if f.predicate == "annual_revenue_usd"
    )
    assert old.support.grade == "STANDARD"


def test_letter_world_grades_and_decides_like_any_other():
    accounts = {"ACC-U2": letter_account()}
    service = make_service(accounts)
    service.ingest("ACC-U2")

    # Before the desk confirms the letters, the letter facts do
    # not exist to grade: the location is missing, not thin.
    # The deductible already stands on the filed inspection
    # alone — one document, STANDARD.
    early = service.assemble("ACC-U2", as_of=AS_OF)
    assert "primary_location" in early.missing_predicates
    early_deductible = next(
        f for f in early.current_facts if f.predicate == "deductible_usd"
    )
    assert early_deductible.doc_id == "DOC-U2-INS"
    assert early_deductible.support.grade == "STANDARD"
    assert early_deductible.support.supporting_documents == 1

    confirm_all(service, "ACC-U2")
    brief = service.assemble("ACC-U2", as_of=AS_OF)
    grades = grades_by_predicate(brief)
    assert grades == {
        "legal_name": "STANDARD",
        "risk_class": "STANDARD",
        "annual_revenue_usd": "STANDARD",
        "claims_count_3yr": "STANDARD",
        "policy_limit_usd": "THIN",       # contested: 2,000,000 displaced
        "primary_location": "THIN",       # a single broker's letter
        "deductible_usd": "STANDARD",     # letter + inspection agree
    }
    limit = next(
        f for f in brief.current_facts if f.predicate == "policy_limit_usd"
    )
    assert limit.support.contested_value == 2000000
    assert limit.support.contested_doc_id == "DOC-U2-END1"
    # The confirmed letter (later window) now carries the
    # deductible, with the inspection as its second document:
    # two supporting documents, and the tier is the letter's.
    deductible = next(
        f for f in brief.current_facts if f.predicate == "deductible_usd"
    )
    assert deductible.doc_id == "DOC-U2-COR2"
    assert deductible.support.tier == "correspondence"
    assert deductible.support.supporting_documents == 2

    # The gate does not read grades: a thin brief decides in
    # exactly the same shape as a strong one.
    decided = service.decide(
        brief.brief_id,
        action="proceed",
        actor="Test Underwriter",
        reason="Thin spots noted and accepted.",
    )
    assert decided.decision.action == "proceed"
    assert decided.status == "DECIDED"


def test_render_lists_the_soft_spots_and_only_those():
    accounts = {"ACC-U2": letter_account()}
    service = make_service(accounts)
    service.ingest("ACC-U2")
    confirm_all(service, "ACC-U2")
    brief = service.assemble("ACC-U2", as_of=AS_OF)
    text = render_brief_text(brief)
    section = text.split("SOFT SPOTS")[1].split("SUPERSEDED")[0]
    assert "policy_limit_usd" in section
    assert "primary_location" in section
    assert "replaced 2,000,000" in section
    assert "legal_name" not in section
    assert "deductible_usd" not in section
    # The vocabulary is printed on the brief.
    assert "STRONG —" in section and "THIN —" in section


def test_render_says_none_when_nothing_is_thin():
    accounts = {"ACC-U1": upgrade_account([full_submission("DOC-U1-SUB", "ACC-U1", "Support Upgrade Co")])}
    service = make_service(accounts)
    service.ingest("ACC-U1")
    brief = service.assemble("ACC-U1", as_of=AS_OF)
    text = render_brief_text(brief)
    section = text.split("SOFT SPOTS")[1].split("SUPERSEDED")[0]
    assert "None — every current fact grades STANDARD or STRONG." in section


def test_render_of_a_pre_grading_brief_says_not_graded():
    brief = Brief(
        brief_id="brief-legacy",
        account_id="ACC-T",
        account_name="Legacy Co",
        as_of=AS_OF,
        backend="offline",
        current_facts=[
            CitedFact(
                predicate="legal_name",
                value="Legacy Co",
                valid_from=date(2026, 1, 1),
                episode_id="ep-1",
                doc_id="DOC-1",
                doc_title="Submission",
                recorded_at=date(2026, 1, 1),
            )
        ],
        created_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    text = render_brief_text(brief)
    assert "Not graded — this brief was assembled before" in text
    assert "None — every current fact" not in text


def test_queue_and_portfolio_carry_the_count_unweighted():
    accounts = {"ACC-U2": letter_account()}
    service = make_service(accounts)
    service.ingest("ACC-U2")
    confirm_all(service, "ACC-U2")
    brief = service.assemble("ACC-U2", as_of=AS_OF)
    service.decide(
        brief.brief_id,
        action="proceed",
        actor="Test Underwriter",
        reason="Thin spots noted and accepted.",
    )

    queue = build_desk_queue(
        accounts,
        service.store,
        service.staleness_store,
        follow_store=service.follow_store,
        today=AS_OF,
        port=service.port,
    )
    (entry,) = queue
    assert entry.thin_fact_count == 2

    queue_no_port = build_desk_queue(
        accounts,
        service.store,
        service.staleness_store,
        follow_store=service.follow_store,
        today=AS_OF,
    )
    assert queue_no_port[0].thin_fact_count is None

    portfolio = build_portfolio_brief(
        port=service.port,
        accounts=accounts,
        brief_store=service.store,
        staleness_store=service.staleness_store,
        follow_store=service.follow_store,
        as_of=AS_OF,
        horizon_days=45,
    )
    (row,) = portfolio.rows
    assert row.thin_facts == 2
    # Decided and otherwise quiet: the published weights score
    # it zero — the thin count moved nothing.
    assert row.score == 0
