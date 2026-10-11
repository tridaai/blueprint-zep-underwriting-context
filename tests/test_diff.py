"""Brief diffs: what changed since the last brief, computed in code.

Two layers of pins:
- the pure comparison over two brief payloads (added, superseded,
  removed named never silent, decisions from the interval);
- the baseline rule (most recent stored brief, never one that
  failed verification, never one dated later than this brief) and
  the service flows that exercise it (re-assembly, decide →
  re-assemble, ingest → re-assemble), including that a gate
  decision taken through the graph resume does not clobber the
  diff riding on the brief.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from zep_context_agent import diffing
from zep_context_agent.ingestion import ingest_document
from zep_context_agent.ontology import FactAssertion, SourceDocument
from zep_context_agent.schemas import (
    Brief,
    CitedFact,
    Decision,
    SupersededFact,
)
from zep_context_agent.store import InMemoryBriefStore

NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)
AS_OF = date(2026, 6, 1)


def cited(predicate, value, *, ref="", doc="DOC-1", valid_from=date(2025, 1, 1)):
    return CitedFact(
        predicate=predicate, value=value, entity_ref=ref,
        valid_from=valid_from, episode_id=f"ep-{doc}", doc_id=doc,
        doc_title=f"Title {doc}", recorded_at=valid_from,
    )


def brief(brief_id, current, *, as_of=AS_OF, superseded=(), decision=None,
          status="AWAITING_UNDERWRITER"):
    return Brief(
        brief_id=brief_id, account_id="ACC-T", account_name="Test Co",
        as_of=as_of, backend="offline", status=status,
        current_facts=list(current), superseded_facts=list(superseded),
        decision=decision, created_at=NOW,
    )


# -- the pure comparison ------------------------------------------------------


def test_added_superseded_removed():
    before = brief("b1", [
        cited("legal_name", "Old Co"),
        cited("policy_limit_usd", 5000000, ref="POL-1", doc="DOC-END"),
        cited("inspection_grade", "B", doc="DOC-INSP"),
    ])
    after = brief("b2", [
        cited("legal_name", "Old Co"),
        cited("policy_limit_usd", 2500000, ref="POL-1", doc="DOC-END2"),
        cited("claims_count_3yr", 2, doc="DOC-CLM"),
    ], superseded=[
        SupersededFact(
            **cited("policy_limit_usd", 5000000, ref="POL-1", doc="DOC-END").model_dump(),
            successor_value=2500000, successor_doc_id="DOC-END2",
            reason="replaced",
        ),
        SupersededFact(
            **cited("inspection_grade", "B", doc="DOC-INSP").model_dump(),
            successor_value=None, successor_doc_id="", reason="expired",
        ),
    ])
    diff = diffing.compute_diff(before, after)
    assert diff.previous_brief_id == "b1"
    assert [(f.predicate, f.value) for f in diff.added] == [("claims_count_3yr", 2)]
    assert len(diff.superseded) == 1
    change = diff.superseded[0]
    assert change.predicate == "policy_limit_usd"
    assert change.previous_value == 5000000
    assert change.current_value == 2500000
    # The retired inspection grade closed as expired: it is named
    # under removed, not folded into superseded and never dropped.
    assert [(f.predicate, f.value) for f in diff.removed] == [("inspection_grade", "B")]


def test_removed_when_replaced_by_a_fact_that_itself_expired():
    before = brief("b1", [cited("cyber_limit_usd", 2000000, doc="DOC-A")])
    after = brief("b2", [], superseded=[
        SupersededFact(
            **cited("cyber_limit_usd", 2000000, doc="DOC-A").model_dump(),
            successor_value=3000000, successor_doc_id="DOC-B",
            reason="replaced",
        ),
        SupersededFact(
            **cited("cyber_limit_usd", 3000000, doc="DOC-B").model_dump(),
            successor_value=None, successor_doc_id="", reason="expired",
        ),
    ])
    diff = diffing.compute_diff(before, after)
    # The hand-off is recorded as superseded (with the successor
    # the new brief itself names), even though nothing is current.
    assert diff.removed == []
    assert len(diff.superseded) == 1
    assert diff.superseded[0].current_value == 3000000


def test_identical_briefs_produce_an_empty_diff():
    facts = [cited("legal_name", "Same Co")]
    diff = diffing.compute_diff(brief("b1", facts), brief("b2", facts))
    assert diff.added == [] and diff.superseded == []
    assert diff.removed == [] and diff.decisions == []


def test_decisions_come_from_the_interval():
    decided = Decision(
        action="proceed", actor="J. Alvarez", reason="Clean file.",
        decided_at=datetime(2026, 6, 3, tzinfo=timezone.utc),
    )
    before = brief("b1", [cited("legal_name", "Old Co")], decision=decided)
    # The decision memo has joined the graph: the next brief cites
    # it as a current fact. It must appear once, with the actor.
    memo_fact = cited(
        "underwriter_decision", "proceed", ref="b1", doc="decision-b1",
        valid_from=date(2026, 6, 3),
    )
    after = brief("b2", [cited("legal_name", "Old Co"), memo_fact],
                  as_of=date(2026, 6, 4))
    diff = diffing.compute_diff(before, after)
    assert len(diff.decisions) == 1
    assert diff.decisions[0].brief_id == "b1"
    assert diff.decisions[0].action == "proceed"
    assert diff.decisions[0].actor == "J. Alvarez"
    assert diff.decisions[0].decided_on == date(2026, 6, 3)
    assert [f.predicate for f in diff.added] == ["underwriter_decision"]


# -- the baseline rule ----------------------------------------------------------


def test_first_brief_has_no_diff():
    store = InMemoryBriefStore()
    only = brief("b1", [cited("legal_name", "Solo Co")])
    store.save(only)
    assert diffing.attach_diff(store, only) is only
    assert only.diff is None


def test_baseline_skips_failed_briefs_and_future_dated_briefs():
    store = InMemoryBriefStore()
    good = brief("b1", [cited("legal_name", "V1")], as_of=date(2026, 1, 1))
    failed = brief("b2", [cited("legal_name", "BAD")], as_of=date(2026, 2, 1),
                   status="VERIFICATION_FAILED")
    future = brief("b3", [cited("legal_name", "FUTURE")], as_of=date(2026, 9, 1))
    for b in (good, failed, future):
        store.save(b)
    current = brief("b4", [cited("legal_name", "V2")], as_of=date(2026, 6, 1))
    store.save(current)
    diffing.attach_diff(store, current)
    assert current.diff is not None
    assert current.diff.previous_brief_id == "b1"  # not b2, not b3
    assert current.diff.superseded[0].previous_value == "V1"


def test_equal_as_of_reassembly_uses_the_stored_order():
    store = InMemoryBriefStore()
    first = brief("b1", [cited("legal_name", "V1")])
    second = brief("b2", [cited("legal_name", "V2")])
    store.save(first)
    store.save(second)
    diffing.attach_diff(store, second)
    assert second.diff is not None
    assert second.diff.previous_brief_id == "b1"


# -- service flows ---------------------------------------------------------------


def test_reassembly_with_no_changes_has_an_empty_diff(service):
    first = service.assemble("ACC-001", as_of=AS_OF)
    assert first.diff is None
    second = service.assemble("ACC-001", as_of=AS_OF)
    assert second.diff is not None
    assert second.diff.previous_brief_id == first.brief_id
    assert second.diff.added == [] and second.diff.superseded == []
    assert second.diff.removed == [] and second.diff.decisions == []


def test_ingest_then_reassemble_shows_the_supersession(service):
    before = service.assemble("ACC-001", as_of=AS_OF)
    doc = SourceDocument(
        doc_id="DOC-001-END3", account_id="ACC-001", kind="endorsement",
        title="Late endorsement — limit raised", received_at=date(2026, 5, 1),
        assertions=[
            FactAssertion(
                predicate="policy_limit_usd", value=3000000,
                valid_from=date(2026, 5, 1), entity_ref="POL-ACC001-2024",
            )
        ],
    )
    ingest_document(service.port, doc)
    after = service.assemble("ACC-001", as_of=AS_OF)
    assert after.diff is not None
    assert after.diff.previous_brief_id == before.brief_id
    changes = [c for c in after.diff.superseded if c.predicate == "policy_limit_usd"]
    assert len(changes) == 1
    assert changes[0].previous_value == 2500000
    assert changes[0].current_value == 3000000


def test_decision_lands_in_the_next_briefs_diff(service):
    first = service.assemble("ACC-001", as_of=AS_OF)
    service.decide(
        first.brief_id, action="request_info", actor="R. Osei",
        reason="Need the updated loss run.",
    )
    second = service.assemble("ACC-001", as_of=date.today())
    assert second.diff is not None
    assert len(second.diff.decisions) == 1
    entry = second.diff.decisions[0]
    assert entry.brief_id == first.brief_id
    assert entry.action == "request_info"
    assert entry.actor == "R. Osei"
    assert any(f.predicate == "underwriter_decision" for f in second.diff.added)


def test_gate_resume_preserves_the_diff(service):
    first = service.assemble("ACC-001", as_of=AS_OF)
    second = service.assemble("ACC-001", as_of=AS_OF)
    assert second.diff is not None
    decided = service.decide(
        second.brief_id, action="proceed", actor="J. Alvarez",
        reason="File is complete.",
    )
    assert decided.decision is not None
    assert decided.diff is not None
    assert decided.diff.previous_brief_id == first.brief_id
    stored = service.get_brief(second.brief_id)
    assert stored.diff is not None and stored.decision is not None


def test_render_states_first_brief_and_changes(service):
    from zep_context_agent.render import render_brief_text

    first = service.assemble("ACC-002", as_of=AS_OF)
    assert "First brief — no earlier brief to compare against." in (
        render_brief_text(first)
    )
    second = service.assemble("ACC-002", as_of=AS_OF)
    text = render_brief_text(second)
    assert f"WHAT CHANGED SINCE {AS_OF.isoformat()}" in text
    assert "No changes" in text
