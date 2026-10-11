"""Brief assembly over the designed accounts: the traps, resolved."""

from __future__ import annotations

from datetime import date

AS_OF = date(2026, 6, 1)


def current(brief, predicate):
    return [f for f in brief.current_facts if f.predicate == predicate]


def test_demo_account_brief(service):
    brief = service.assemble("ACC-001", as_of=AS_OF)
    assert brief.status == "AWAITING_UNDERWRITER"
    assert brief.backend == "offline"
    # The endorsed-down limit is current; the old one is superseded
    # and names its successor.
    assert [f.value for f in current(brief, "policy_limit_usd")] == [2500000]
    retired = [f for f in brief.superseded_facts if f.predicate == "policy_limit_usd"]
    assert len(retired) == 1
    assert retired[0].value == 5000000
    assert retired[0].successor_value == 2500000
    assert retired[0].reason == "replaced"
    # The relocation resolved; the claims count is the extract's.
    assert [f.value for f in current(brief, "primary_location")] == [
        "2280 Industrial Pkwy, Pittsburgh, PA"
    ]
    assert [f.value for f in current(brief, "claims_count_3yr")] == [2]
    # Nothing expected is missing; the injection is named.
    assert brief.missing_predicates == []
    assert [q.doc_id for q in brief.quarantined] == ["DOC-001-INJ"]
    # Verification ran and passed; every fact carries a citation.
    assert brief.verification.citations_complete is True
    assert brief.verification.leakage_free is True
    assert all(f.doc_id and f.episode_id for f in brief.current_facts)
    assert brief.narrative_source == "template"


def test_mid_history_brief(service):
    brief = service.assemble("ACC-001", as_of=date(2024, 6, 1))
    assert [f.value for f in current(brief, "policy_limit_usd")] == [5000000]
    assert "claims_count_3yr" in brief.missing_predicates


def test_renamed_insured(service):
    brief = service.assemble("ACC-003", as_of=AS_OF)
    assert [f.value for f in current(brief, "legal_name")] == [
        "Brightpath Freight Group Inc"
    ]
    retired = [f for f in brief.superseded_facts if f.predicate == "legal_name"]
    assert [f.value for f in retired] == ["Brightpath Logistics Inc"]


def test_dropped_and_reinstated_coverage(service):
    brief = service.assemble("ACC-004", as_of=AS_OF)
    assert [f.value for f in current(brief, "cyber_limit_usd")] == [3000000]
    gap = service.assemble("ACC-004", as_of=date(2024, 9, 1))
    assert current(gap, "cyber_limit_usd") == []
    assert "cyber_limit_usd" not in gap.missing_predicates  # not an expected predicate
    retired = [f for f in gap.superseded_facts if f.predicate == "cyber_limit_usd"]
    assert retired and retired[0].reason == "expired"


def test_future_fact_never_surfaces(service):
    brief = service.assemble("ACC-005", as_of=AS_OF)
    assert [f.value for f in current(brief, "policy_limit_usd")] == [1500000]
    assert all(
        f.value != 2000000
        for f in brief.current_facts + brief.superseded_facts
        if f.predicate == "policy_limit_usd"
    )


def test_incomplete_file_reports_missing(service):
    brief = service.assemble("ACC-006", as_of=AS_OF)
    assert "claims_count_3yr" in brief.missing_predicates
    retired = [f for f in brief.superseded_facts if f.predicate == "inspection_grade"]
    assert retired and retired[0].reason == "expired"
