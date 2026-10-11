"""Leakage: zero tolerance, with the near-identical names as the trap.

ACC-001 "Meridian Casting Co." and ACC-002 "Meridian Castings LLC"
share a populated graph in these tests — the deployment shape —
and their briefs must still share nothing.
"""

from __future__ import annotations

from datetime import date

from zep_context_agent.guardrails import brief_violations

AS_OF = date(2026, 6, 1)


def test_near_name_accounts_share_nothing(service):
    brief_a = service.assemble("ACC-001", as_of=AS_OF)
    brief_b = service.assemble("ACC-002", as_of=AS_OF)
    facts_a = {(f.predicate, f.value, f.episode_id) for f in brief_a.current_facts}
    facts_b = {(f.predicate, f.value, f.episode_id) for f in brief_b.current_facts}
    assert facts_a.isdisjoint(facts_b)
    eps_a = {e.episode_id for e in brief_a.sources}
    eps_b = {e.episode_id for e in brief_b.sources}
    assert eps_a.isdisjoint(eps_b)
    assert all(e.account_id == "ACC-001" for e in brief_a.sources)
    assert all(e.account_id == "ACC-002" for e in brief_b.sources)
    assert brief_a.verification.leakage_free is True
    assert brief_b.verification.leakage_free is True
    # The other Meridian's signature figures appear in neither brief.
    assert all(f.value != 1000000 for f in brief_a.current_facts)
    assert all(f.value != 2500000 for f in brief_b.current_facts)


def test_designed_accounts_all_clean(service):
    for account_id in ["ACC-001", "ACC-002", "ACC-003", "ACC-004", "ACC-005", "ACC-006"]:
        brief = service.assemble(account_id, as_of=AS_OF)
        violations = brief_violations(brief, service.port.get_facts(account_id))
        assert violations == [], f"{account_id}: {violations}"
        assert brief.verification.citations_complete is True
