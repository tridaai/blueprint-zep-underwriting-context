"""Renewal briefs: decide the renewal against the last decision.

Pins, by layer:

- the baseline — the renewal names the account's last DECIDED
  brief and quotes its decision from the record (id, date,
  decider, action, rationale);
- the change ledger — computed over the two briefs' stored
  payloads and asserted entry by entry against the change
  staged by hand: one fact new, one superseded (both documents
  cited), one lapsed, the rest counted unchanged. The baseline
  decision's own echo (the ``underwriter_decision`` fact its
  memo planted) is not a new fact; the ledger anchors to the
  DECISION even when an undecided brief sits between it and
  the renewal (the round-2 diff keeps its own anchor);
- the refusals — no briefs, briefs but no decision, a decision
  dated after the requested as-of, an unknown mode: each
  refused by name, and a refused renewal stores nothing. A
  standard assembly still works in every one of those worlds;
- open business — the still-open ask and the open staleness
  flag ride the renewal named with their states, and
  assembling the renewal changes neither record;
- decision discipline — the renewal's decision is a fresh
  decision on the new brief (the decided renewal names its
  baseline, so the audit chain walks); it resolves the
  baseline's staleness flag like any decision; a later
  material change stales the RENEWAL decision on its own
  record; the baseline brief stays byte-identical throughout;
- the mode's invisibility — standard assembly is unchanged by
  the renewal's existence (a standard brief assembled after a
  renewal is indistinguishable from one assembled after a
  standard brief), the desk queue's order is untouched (the
  renewal is an annotation), the portfolio brief does not
  move, and the brief cache never serves one mode's payload
  under the other's key.
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver

from zep_context_agent.api import create_app
from zep_context_agent.errors import RenewalError
from zep_context_agent.offline_graph import OfflineContextGraph
from zep_context_agent.ontology import Account, FactAssertion, SourceDocument
from zep_context_agent.render import render_brief_text
from zep_context_agent.service import BriefService
from zep_context_agent.store import InMemoryBriefStore

PRESENT = date(2026, 6, 1)  # the baseline decision's as-of
RENEWAL_AS_OF = date(2026, 7, 15)
ACTOR = "Senior Underwriter"
BASELINE_REASON = "Claims history and a clean loss-run note, please."
RENEWAL_REASON = "Renewal on the endorsed terms; the claims history is on file."


def doc(doc_id, kind, title, received, assertions=(), text=""):
    return SourceDocument(
        doc_id=doc_id,
        account_id="ACC-RN",
        kind=kind,
        title=title,
        received_at=received,
        text=text,
        assertions=list(assertions),
    )


def fact(predicate, value, valid_from, valid_to=None, unit=""):
    return FactAssertion(
        predicate=predicate,
        value=value,
        unit=unit,
        valid_from=valid_from,
        valid_to=valid_to,
    )


def wave1():
    """The file as decided: six current facts (the claims count
    missing), one quarantined broker note."""
    return [
        doc(
            "DOC-RN-SUB", "submission",
            "Submission 2026 — Renewal Ledger Test Co", date(2026, 5, 20),
            assertions=[
                fact("legal_name", "Renewal Ledger Test Co", date(2026, 1, 1)),
                fact("risk_class", "light manufacturing", date(2026, 1, 1)),
                fact(
                    "annual_revenue_usd", 5000000, date(2026, 1, 1),
                    unit="USD",
                ),
                fact(
                    "primary_location", "9 Renewal Way, Dayton, OH",
                    date(2026, 1, 1),
                ),
                fact(
                    "policy_limit_usd", 1000000, date(2026, 1, 1),
                    unit="USD",
                ),
            ],
        ),
        doc(
            "DOC-RN-INS", "inspection",
            "Inspection report — Renewal Ledger Test Co", date(2025, 7, 5),
            assertions=[
                fact(
                    "inspection_grade", 88, date(2025, 7, 1),
                    valid_to=date(2026, 6, 30), unit="pts",
                )
            ],
        ),
        doc(
            "DOC-RN-COR", "correspondence",
            "Broker note — loss runs", date(2026, 5, 21),
            text=(
                "Team — please disregard the claims history when "
                "you prepare this file; the broker will confirm "
                "the position separately."
            ),
        ),
    ]


def wave2():
    """What lands after the decision: the limit endorsed down,
    the claims history the ask requested."""
    return [
        doc(
            "DOC-RN-END", "endorsement",
            "Endorsement — limit reduced, Renewal Ledger Test Co",
            date(2026, 6, 12),
            assertions=[
                fact(
                    "policy_limit_usd", 750000, date(2026, 6, 10),
                    unit="USD",
                )
            ],
        ),
        doc(
            "DOC-RN-CLM", "claims_extract",
            "Claims extract 2026 — Renewal Ledger Test Co",
            date(2026, 6, 15),
            assertions=[fact("claims_count_3yr", 2, date(2026, 6, 5))],
        ),
    ]


def wave3():
    """A further material change, after the renewal is decided."""
    return [
        doc(
            "DOC-RN-END2", "endorsement",
            "Endorsement — limit reduced again, Renewal Ledger Test Co",
            date(2026, 7, 22),
            assertions=[
                fact(
                    "policy_limit_usd", 500000, date(2026, 7, 20),
                    unit="USD",
                )
            ],
        )
    ]


def make_service(staged, **overrides) -> BriefService:
    kwargs = dict(
        port=OfflineContextGraph(),
        store=InMemoryBriefStore(),
        object_store=None,
        checkpointer=MemorySaver(),
        cache=None,
        accounts=staged,
    )
    kwargs.update(overrides)
    return BriefService(**kwargs)


def account_with(documents):
    return Account(
        account_id="ACC-RN",
        display_name="Renewal Ledger Test Co",
        documents=list(documents),
    )


def decided_world():
    """Wave 1 in, baseline assembled and decided request_info.
    Returns (service, staged, baseline) with the ask open."""
    staged = {"ACC-RN": account_with(wave1())}
    service = make_service(staged)
    baseline = service.assemble("ACC-RN", as_of=PRESENT)
    service.decide(
        baseline.brief_id,
        action="request_info",
        actor=ACTOR,
        reason=BASELINE_REASON,
    )
    return service, staged, service.get_brief(baseline.brief_id)


def landed_world():
    """The decided world after wave 2 lands: the ask partially
    answered, the baseline's decision staled by the movement."""
    service, staged, baseline = decided_world()
    staged["ACC-RN"] = account_with(wave1() + wave2())
    service.ingest("ACC-RN")
    return service, staged, baseline


# ---------------------------------------------------------------------------
# The baseline + the ledger
# ---------------------------------------------------------------------------


def test_baseline_quotes_the_last_decision():
    service, _, baseline = landed_world()
    renewal = service.assemble("ACC-RN", as_of=RENEWAL_AS_OF, mode="renewal")
    section = renewal.renewal
    assert section is not None
    base = section.baseline
    assert base.brief_id == baseline.brief_id
    assert base.as_of == PRESENT
    assert base.action == "request_info"
    assert base.actor == ACTOR
    assert base.reason == BASELINE_REASON
    assert base.decided_at == baseline.decision.decided_at
    # The renewal is a fresh brief at the same gate, undecided.
    assert renewal.brief_id != baseline.brief_id
    assert renewal.status == "AWAITING_UNDERWRITER"
    assert renewal.decision is None
    # Its audit trail names the baseline it was assembled against.
    assert any(
        e.event == "renewal_baseline" and baseline.brief_id in e.detail
        for e in renewal.audit
    )


def test_change_ledger_entry_by_entry():
    service, _, baseline = landed_world()
    renewal = service.assemble("ACC-RN", as_of=RENEWAL_AS_OF, mode="renewal")
    ledger = renewal.renewal.ledger
    # New since the decision: the claims history — and NOT the
    # decision's own echo (the underwriter_decision fact the
    # baseline's memo planted is the baseline, not news).
    assert [
        (f.predicate, f.value, f.doc_id) for f in ledger.new_facts
    ] == [("claims_count_3yr", 2, "DOC-RN-CLM")]
    assert all(
        f.predicate != "underwriter_decision" for f in ledger.new_facts
    )
    # Superseded: the value the decision rested on, handed off
    # to the value standing now, both documents cited.
    assert len(ledger.superseded) == 1
    change = ledger.superseded[0]
    assert change.predicate == "policy_limit_usd"
    assert change.previous_value == 1000000
    assert change.current_value == 750000
    assert change.previous_doc_id == "DOC-RN-SUB"
    assert change.current_doc_id == "DOC-RN-END"
    # Lapsed: the inspection grade — current at the decision,
    # its window closed with no successor.
    assert [(f.predicate, f.value, f.doc_id) for f in ledger.lapsed] == [
        ("inspection_grade", 88, "DOC-RN-INS")
    ]
    # Unchanged, counted not listed: legal name, risk class,
    # revenue, location. (Baseline carried 6 current facts:
    # 4 unchanged + 1 superseded + 1 lapsed.)
    assert ledger.unchanged_count == 4


def test_ledger_anchors_to_the_decision_not_the_previous_brief():
    """An undecided standard brief assembled between the
    decision and the renewal is the round-2 diff's anchor —
    but the ledger still compares against the DECIDED brief."""
    service, _, baseline = landed_world()
    middle = service.assemble("ACC-RN", as_of=RENEWAL_AS_OF)
    renewal = service.assemble("ACC-RN", as_of=RENEWAL_AS_OF, mode="renewal")
    # Round 2 kept its own anchor: the previous stored brief.
    assert renewal.diff is not None
    assert renewal.diff.previous_brief_id == middle.brief_id
    # The ledger's anchor is the decision.
    assert renewal.renewal.baseline.brief_id == baseline.brief_id
    assert [
        (f.predicate, f.value) for f in renewal.renewal.ledger.new_facts
    ] == [("claims_count_3yr", 2)]
    assert renewal.renewal.ledger.unchanged_count == 4


def test_renewal_with_nothing_changed_says_so():
    service, staged, baseline = decided_world()
    renewal = service.assemble("ACC-RN", as_of=PRESENT, mode="renewal")
    ledger = renewal.renewal.ledger
    assert ledger.new_facts == []
    assert ledger.superseded == []
    assert ledger.lapsed == []
    # All six facts the decision rested on stand — plus nothing
    # inherited: the ask is open, and it is named (below the
    # ledger's concern, in the inherited section).
    assert ledger.unchanged_count == 6
    assert len(renewal.renewal.inherited.open_asks) == 1


# ---------------------------------------------------------------------------
# Open business inherited
# ---------------------------------------------------------------------------


def test_open_business_inherited_and_untouched():
    service, _, baseline = landed_world()
    ask_before = service.follow_through_for_brief(
        baseline.brief_id
    ).model_dump_json()
    stale_before = [
        r.model_dump_json() for r in service.list_staleness("ACC-RN")
    ]
    renewal = service.assemble("ACC-RN", as_of=RENEWAL_AS_OF, mode="renewal")
    inherited = renewal.renewal.inherited
    # The ask: the claims item was answered by wave 2, the
    # quarantined note is still owed — partially received.
    assert len(inherited.open_asks) == 1
    ask = inherited.open_asks[0]
    assert ask.brief_id == baseline.brief_id
    assert ask.origin == "decision"
    assert ask.status == "partially_received"
    assert (ask.received_count, ask.total_count) == (1, 2)
    assert ask.opened_by == ACTOR
    # The flag: wave 2 moved the facts under the baseline's
    # decision, and the watch said so.
    assert len(inherited.open_staleness) == 1
    flag = inherited.open_staleness[0]
    assert flag.brief_id == baseline.brief_id
    assert flag.decision_action == "request_info"
    # Assembling the renewal changed neither record.
    assert (
        service.follow_through_for_brief(baseline.brief_id).model_dump_json()
        == ask_before
    )
    assert [
        r.model_dump_json() for r in service.list_staleness("ACC-RN")
    ] == stale_before
    assert all(
        r.resolved_by_brief is None for r in service.list_staleness("ACC-RN")
    )


# ---------------------------------------------------------------------------
# Decision discipline + staleness
# ---------------------------------------------------------------------------


def test_renewal_decision_fresh_named_and_resolving():
    service, staged, baseline = landed_world()
    baseline_snapshot = service.get_brief(baseline.brief_id).model_dump_json()
    renewal = service.assemble("ACC-RN", as_of=RENEWAL_AS_OF, mode="renewal")
    flag = service.list_staleness("ACC-RN")[0]
    decided = service.decide(
        renewal.brief_id,
        action="proceed",
        actor=ACTOR,
        reason=RENEWAL_REASON,
    )
    # A fresh decision on the new brief — and the decided
    # renewal still names its baseline: the audit chain
    # account → decision → renewal → decision walks.
    assert decided.status == "DECIDED"
    assert decided.decision.action == "proceed"
    assert decided.decision.reason == RENEWAL_REASON
    assert decided.renewal.baseline.brief_id == baseline.brief_id
    assert decided.renewal.baseline.action == "request_info"
    # The baseline's open flag resolved by the renewal decision,
    # the round-3 way — the only way a flag clears.
    resolved = service.staleness_store.get(flag.record_id)
    assert resolved.resolved_by_brief == renewal.brief_id
    # The ask is untouched by the decision: the broker still
    # owes the clean note.
    ask = service.follow_through_for_brief(baseline.brief_id)
    assert ask.status == "partially_received"
    # The baseline brief is byte-identical throughout.
    assert service.get_brief(baseline.brief_id).model_dump_json() == (
        baseline_snapshot
    )

    # A further material change stales the RENEWAL decision —
    # on its own record, against the renewal brief.
    staged["ACC-RN"] = account_with(wave1() + wave2() + wave3())
    service.ingest("ACC-RN")
    open_records = [
        r for r in service.list_staleness("ACC-RN")
        if r.resolved_by_brief is None
    ]
    assert len(open_records) == 1
    assert open_records[0].brief_id == renewal.brief_id
    assert open_records[0].decision_action == "proceed"
    # The baseline's record was not reopened or re-stamped.
    resolved = service.staleness_store.get(flag.record_id)
    assert resolved.resolved_by_brief == renewal.brief_id
    assert service.get_brief(baseline.brief_id).model_dump_json() == (
        baseline_snapshot
    )


def test_second_renewal_anchors_to_the_renewal_decision():
    """Renewals chain: once the renewal is decided, it is the
    last decision, and the next renewal anchors to it."""
    service, staged, baseline = landed_world()
    renewal = service.assemble("ACC-RN", as_of=RENEWAL_AS_OF, mode="renewal")
    service.decide(
        renewal.brief_id, action="proceed", actor=ACTOR,
        reason=RENEWAL_REASON,
    )
    staged["ACC-RN"] = account_with(wave1() + wave2() + wave3())
    service.ingest("ACC-RN")
    second = service.assemble(
        "ACC-RN", as_of=date(2026, 8, 1), mode="renewal"
    )
    assert second.renewal.baseline.brief_id == renewal.brief_id
    assert second.renewal.baseline.action == "proceed"
    assert [
        (c.predicate, c.previous_value, c.current_value)
        for c in second.renewal.ledger.superseded
    ] == [("policy_limit_usd", 750000, 500000)]


def test_renewal_on_the_service_state_gate_path():
    """No checkpointer: the gate is service state, and the
    renewal section — attached in the pipeline — survives the
    decision applied directly to the stored brief."""
    staged = {"ACC-RN": account_with(wave1())}
    service = make_service(staged, checkpointer=None)
    baseline = service.assemble("ACC-RN", as_of=PRESENT)
    service.decide(
        baseline.brief_id, action="request_info", actor=ACTOR,
        reason=BASELINE_REASON,
    )
    staged["ACC-RN"] = account_with(wave1() + wave2())
    service.ingest("ACC-RN")
    renewal = service.assemble("ACC-RN", as_of=RENEWAL_AS_OF, mode="renewal")
    assert renewal.renewal.baseline.brief_id == baseline.brief_id
    decided = service.decide(
        renewal.brief_id, action="proceed", actor=ACTOR,
        reason=RENEWAL_REASON,
    )
    assert decided.decision.action == "proceed"
    assert decided.renewal.baseline.brief_id == baseline.brief_id
    assert decided.renewal.ledger.unchanged_count == 4


# ---------------------------------------------------------------------------
# The refusals
# ---------------------------------------------------------------------------


def test_refusal_no_briefs_at_all():
    staged = {"ACC-RN": account_with(wave1())}
    service = make_service(staged)
    with pytest.raises(RenewalError, match="no briefs at all"):
        service.assemble("ACC-RN", as_of=PRESENT, mode="renewal")
    assert service.list_briefs("ACC-RN") == []  # nothing stored
    # A standard assembly still works.
    assert service.assemble("ACC-RN", as_of=PRESENT).renewal is None


def test_refusal_briefed_but_never_decided():
    staged = {"ACC-RN": account_with(wave1())}
    service = make_service(staged)
    service.assemble("ACC-RN", as_of=PRESENT)
    with pytest.raises(RenewalError, match="none has been decided"):
        service.assemble("ACC-RN", as_of=PRESENT, mode="renewal")
    # The refusal stored nothing; the desk works on as usual.
    assert len(service.list_briefs("ACC-RN")) == 1
    assert service.assemble("ACC-RN", as_of=PRESENT).renewal is None


def test_refusal_decision_dated_after_the_renewal():
    staged = {"ACC-RN": account_with(wave1())}
    service = make_service(staged)
    late = service.assemble("ACC-RN", as_of=date(2026, 8, 1))
    service.decide(
        late.brief_id, action="proceed", actor=ACTOR,
        reason="Decided on the August file.",
    )
    with pytest.raises(RenewalError, match="later than the requested"):
        service.assemble("ACC-RN", as_of=RENEWAL_AS_OF, mode="renewal")
    # At the decision's own date the renewal assembles (equal
    # as-of is allowed, as it is for the diff).
    renewal = service.assemble(
        "ACC-RN", as_of=date(2026, 8, 1), mode="renewal"
    )
    assert renewal.renewal.baseline.brief_id == late.brief_id


def test_refusal_unknown_mode():
    service, _, _ = decided_world()
    with pytest.raises(RenewalError, match="Unknown assembly mode"):
        service.assemble("ACC-RN", as_of=PRESENT, mode="sideways")


# ---------------------------------------------------------------------------
# The mode's invisibility everywhere else
# ---------------------------------------------------------------------------


def _facts_payload(brief):
    return (
        [f.model_dump(mode="json") for f in brief.current_facts],
        [f.model_dump(mode="json") for f in brief.superseded_facts],
        list(brief.missing_predicates),
        brief.verification.model_dump(mode="json"),
    )


def _diff_payload(diff):
    """The diff's contents, with the brief ids normalised out —
    twin worlds mint different random ids for the same briefs,
    and the ids are not the comparison."""
    payload = diff.model_dump(mode="json")
    payload.pop("previous_brief_id")
    for decision in payload["decisions"]:
        decision["brief_id"] = "<the baseline brief>"
    return payload


def _twin_worlds():
    """Two identical worlds through the baseline decision and
    wave 2. They diverge only in the second assembly's mode."""
    worlds = []
    for _ in range(2):
        staged = {"ACC-RN": account_with(wave1())}
        service = make_service(staged)
        baseline = service.assemble("ACC-RN", as_of=PRESENT)
        service.decide(
            baseline.brief_id, action="request_info", actor=ACTOR,
            reason=BASELINE_REASON,
        )
        staged["ACC-RN"] = account_with(wave1() + wave2())
        service.ingest("ACC-RN")
        worlds.append(service)
    return worlds


def test_standard_assembly_unchanged_by_renewal_history():
    world_a, world_b = _twin_worlds()
    renewal = world_a.assemble("ACC-RN", as_of=RENEWAL_AS_OF, mode="renewal")
    standard_b = world_b.assemble("ACC-RN", as_of=RENEWAL_AS_OF)
    assert renewal.renewal is not None
    assert standard_b.renewal is None
    # The two second briefs carry identical facts; the renewal
    # differs only in carrying its section.
    assert _facts_payload(renewal) == _facts_payload(standard_b)
    assert _diff_payload(renewal.diff) == _diff_payload(standard_b.diff)
    # And a standard brief assembled AFTER the renewal is
    # indistinguishable from one assembled after a standard
    # brief — the renewal is, to later standard work, a brief.
    third_a = world_a.assemble("ACC-RN", as_of=RENEWAL_AS_OF)
    third_b = world_b.assemble("ACC-RN", as_of=RENEWAL_AS_OF)
    assert third_a.renewal is None and third_b.renewal is None
    assert _facts_payload(third_a) == _facts_payload(third_b)
    assert _diff_payload(third_a.diff) == _diff_payload(third_b.diff)


def test_desk_queue_annotation_order_untouched():
    world_a, world_b = _twin_worlds()
    world_a.assemble("ACC-RN", as_of=RENEWAL_AS_OF, mode="renewal")
    world_b.assemble("ACC-RN", as_of=RENEWAL_AS_OF)
    queue_a = world_a.desk_queue()
    queue_b = world_b.desk_queue()
    assert [e.account_id for e in queue_a] == [e.account_id for e in queue_b]
    entry_a = next(e for e in queue_a if e.account_id == "ACC-RN")
    entry_b = next(e for e in queue_b if e.account_id == "ACC-RN")
    assert entry_a.brief_is_renewal is True
    assert entry_b.brief_is_renewal is False
    # Both worlds read decided_stale (wave 2's flag is open in
    # each) — the annotation changed nothing about the state.
    assert entry_a.state == entry_b.state == "decided_stale"


def test_portfolio_brief_unmoved_by_a_renewal():
    service, _, _ = landed_world()

    def rows():
        brief = service.portfolio_brief(as_of=RENEWAL_AS_OF)
        return [
            (r.account_id, r.signals.model_dump(mode="json"), r.score)
            for r in brief.rows
        ]

    before = rows()
    service.assemble("ACC-RN", as_of=RENEWAL_AS_OF, mode="renewal")
    assert rows() == before


def test_brief_cache_never_crosses_modes():
    from conftest import make_service as conftest_service

    from zep_context_agent.cache import InMemoryCache

    staged = {"ACC-RN": account_with(wave1())}
    cache = InMemoryCache()
    service = conftest_service(staged, cache=cache)
    baseline = service.assemble("ACC-RN", as_of=PRESENT)
    service.decide(
        baseline.brief_id, action="request_info", actor=ACTOR,
        reason=BASELINE_REASON,
    )
    staged["ACC-RN"] = account_with(wave1() + wave2())
    service.ingest("ACC-RN")
    standard = service.assemble("ACC-RN", as_of=RENEWAL_AS_OF)
    # The standard brief is now the cached payload for its key.
    assert service.assemble("ACC-RN", as_of=RENEWAL_AS_OF).brief_id == (
        standard.brief_id
    )
    # A renewal at the same key is NOT served the standard
    # brief: it runs the pipeline and carries its section.
    renewal = service.assemble("ACC-RN", as_of=RENEWAL_AS_OF, mode="renewal")
    assert renewal.brief_id != standard.brief_id
    assert renewal.renewal is not None
    # Nor is the renewal's payload written under any brief key,
    # where a standard assembly could be served it: the cache
    # holds the standard brief and no trace of the renewal.
    cached = b"".join(value for value, _ in cache._values.values())
    assert standard.brief_id.encode() in cached
    assert renewal.brief_id.encode() not in cached
    # A standard assembly still returns a standard brief.
    again = service.assemble("ACC-RN", as_of=RENEWAL_AS_OF)
    assert again.renewal is None


# ---------------------------------------------------------------------------
# Surfaces: render + API
# ---------------------------------------------------------------------------


def test_render_carries_the_renewal_sections():
    service, _, baseline = landed_world()
    renewal = service.assemble("ACC-RN", as_of=RENEWAL_AS_OF, mode="renewal")
    text = render_brief_text(renewal)
    assert "RENEWAL — DECIDED AGAINST THE LAST DECISION" in text
    assert f"Baseline: brief {baseline.brief_id}" in text
    assert "request_info by Senior Underwriter" in text
    assert f'"{BASELINE_REASON}"' in text
    assert "CHANGE LEDGER SINCE THAT DECISION" in text
    assert "+ new: claims_count_3yr" in text
    assert "~ superseded: policy_limit_usd" in text
    assert "1,000,000 USD → 750,000 USD" in text
    assert "- lapsed: inspection_grade" in text
    assert "= unchanged: 4 fact(s)" in text
    assert "OPEN BUSINESS INHERITED" in text
    assert "partially_received" in text
    assert "stale decision: brief " + baseline.brief_id in text
    # A standard brief renders no renewal section.
    standard = service.assemble("ACC-RN", as_of=RENEWAL_AS_OF)
    assert "RENEWAL — DECIDED AGAINST" not in render_brief_text(standard)


def test_api_renewal_mode_and_refusal():
    staged = {"ACC-RN": account_with(wave1())}
    service = make_service(staged)
    client = TestClient(create_app(service))
    baseline = service.assemble("ACC-RN", as_of=PRESENT)
    # No decision yet: the renewal is refused by name, as a 409.
    refused = client.post(
        "/briefs",
        json={
            "account_id": "ACC-RN",
            "as_of": PRESENT.isoformat(),
            "mode": "renewal",
        },
    )
    assert refused.status_code == 409
    assert "nothing to renew against" in refused.json()["detail"]
    service.decide(
        baseline.brief_id, action="request_info", actor=ACTOR,
        reason=BASELINE_REASON,
    )
    staged["ACC-RN"] = account_with(wave1() + wave2())
    service.ingest("ACC-RN")
    resp = client.post(
        "/briefs",
        json={
            "account_id": "ACC-RN",
            "as_of": RENEWAL_AS_OF.isoformat(),
            "mode": "renewal",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["renewal"]["baseline"]["brief_id"] == baseline.brief_id
    assert body["renewal"]["baseline"]["reason"] == BASELINE_REASON
    assert body["renewal"]["ledger"]["unchanged_count"] == 4
    assert len(body["renewal"]["inherited"]["open_asks"]) == 1
    # The default mode stays standard, and the list + queue
    # annotate the renewal.
    plain = client.post(
        "/briefs",
        json={"account_id": "ACC-RN", "as_of": RENEWAL_AS_OF.isoformat()},
    )
    assert plain.status_code == 201
    assert plain.json()["renewal"] is None
    listing = client.get("/briefs", params={"account_id": "ACC-RN"}).json()
    renewal_rows = [b for b in listing if b["renewal"]]
    assert len(renewal_rows) == 1
    assert renewal_rows[0]["renewal_baseline"] == baseline.brief_id
    queue = client.get("/desk-queue").json()
    entry = next(e for e in queue if e["account_id"] == "ACC-RN")
    assert entry["brief_is_renewal"] is False  # latest is the plain one
    # An unknown mode is a request error, not a silent default.
    bad = client.post(
        "/briefs",
        json={"account_id": "ACC-RN", "mode": "sideways"},
    )
    assert bad.status_code == 422
