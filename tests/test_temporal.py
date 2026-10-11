"""The temporal rule: validity windows decide what is current.

These tests pin resolve() directly — the assembly, the eval pack,
and the brief an underwriter reads all stand on this function.
"""

from __future__ import annotations

from datetime import date

from zep_context_agent.ontology import Fact
from zep_context_agent.temporal import resolve


def fact(predicate, value, valid_from, valid_to=None, recorded_at=None, ref=""):
    return Fact(
        fact_id=f"f-{predicate}-{value}-{valid_from}",
        account_id="ACC-X",
        predicate=predicate,
        value=value,
        entity_ref=ref,
        valid_from=valid_from,
        valid_to=valid_to,
        recorded_at=recorded_at or valid_from,
        episode_id="ep-1",
        doc_id="DOC-1",
    )


def test_window_boundaries():
    f = fact("policy_limit_usd", 100, date(2024, 1, 1), date(2025, 1, 1))
    current, _ = resolve([f], date(2024, 1, 1))
    assert [c.value for c in current] == [100]  # valid_from is inclusive
    current, superseded = resolve([f], date(2025, 1, 1))
    assert current == []  # valid_to is exclusive
    assert [s.value for s, _ in superseded] == [100]


def test_replaced_fact_names_its_successor():
    old = fact("policy_limit_usd", 5000000, date(2024, 2, 1), date(2025, 7, 1))
    new = fact("policy_limit_usd", 2500000, date(2025, 7, 1))
    current, superseded = resolve([old, new], date(2026, 6, 1))
    assert [c.value for c in current] == [2500000]
    assert len(superseded) == 1
    retired, successor = superseded[0]
    assert retired.value == 5000000
    assert successor is not None and successor.value == 2500000


def test_expired_without_successor():
    f = fact("inspection_grade", "B", date(2023, 3, 20), date(2024, 3, 20))
    current, superseded = resolve([f], date(2026, 6, 1))
    assert current == []
    assert superseded[0][1] is None  # expired, replaced by nothing


def test_future_fact_is_neither_current_nor_superseded():
    now = fact("policy_limit_usd", 1500000, date(2025, 10, 1))
    future = fact("policy_limit_usd", 2000000, date(2027, 1, 1))
    current, superseded = resolve([now, future], date(2026, 6, 1))
    assert [c.value for c in current] == [1500000]
    assert superseded == []  # the future is not history either


def test_recorded_at_breaks_same_day_ties():
    early = fact("claims_count_3yr", 1, date(2025, 2, 10), recorded_at=date(2025, 2, 10))
    late = fact("claims_count_3yr", 2, date(2025, 2, 10), recorded_at=date(2025, 2, 11))
    current, superseded = resolve([early, late], date(2026, 6, 1))
    assert [c.value for c in current] == [2]
    assert superseded[0][0].value == 1


def test_entity_refs_resolve_independently():
    a = fact("policy_limit_usd", 100, date(2024, 1, 1), ref="POL-1")
    b = fact("policy_limit_usd", 200, date(2024, 1, 1), ref="POL-2")
    current, superseded = resolve([a, b], date(2026, 6, 1))
    assert {c.value for c in current} == {100, 200}
    assert superseded == []


def test_gap_period_has_no_current_fact():
    dropped = fact("cyber_limit_usd", 2000000, date(2023, 6, 1), date(2024, 6, 1))
    reinstated = fact("cyber_limit_usd", 3000000, date(2025, 1, 15))
    current, _ = resolve([dropped, reinstated], date(2024, 9, 1))
    assert current == []  # uncovered season: the brief must say missing
    current, _ = resolve([dropped, reinstated], date(2026, 6, 1))
    assert [c.value for c in current] == [3000000]
