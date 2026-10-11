"""The gate: briefs stop at AWAITING_UNDERWRITER; decisions are
recorded with actor + reason, audited, and join the account's
history. Both gate paths (checkpointed graph, service-state) are
pinned to identical behaviour."""

from __future__ import annotations

from datetime import date

import pytest

from zep_context_agent.errors import DecisionError

AS_OF = date(2026, 6, 1)


@pytest.mark.parametrize("action", ["proceed", "request_info", "escalate"])
def test_decision_recorded(service, action):
    brief = service.assemble("ACC-002", as_of=AS_OF)
    decided = service.decide(
        brief.brief_id, action=action, actor="R. Osei", reason="File supports it."
    )
    assert decided.status == "DECIDED"
    assert decided.decision.action == action
    assert decided.decision.actor == "R. Osei"
    assert decided.decision.reason == "File supports it."
    assert any(e.event == "gate_decision" for e in decided.audit)


@pytest.mark.parametrize("action", ["proceed", "request_info", "escalate"])
def test_decision_recorded_without_checkpointer(service_no_checkpointer, action):
    brief = service_no_checkpointer.assemble("ACC-002", as_of=AS_OF)
    decided = service_no_checkpointer.decide(
        brief.brief_id, action=action, actor="R. Osei", reason="File supports it."
    )
    assert decided.status == "DECIDED"
    assert decided.decision.action == action


def test_second_decision_refused(service):
    brief = service.assemble("ACC-002", as_of=AS_OF)
    service.decide(brief.brief_id, action="proceed", actor="A", reason="ok")
    with pytest.raises(DecisionError):
        service.decide(brief.brief_id, action="escalate", actor="B", reason="changed")


@pytest.mark.parametrize("actor,reason", [("", "ok"), ("A", ""), ("  ", "ok")])
def test_decision_requires_actor_and_reason(service, actor, reason):
    brief = service.assemble("ACC-002", as_of=AS_OF)
    with pytest.raises(DecisionError):
        service.decide(brief.brief_id, action="proceed", actor=actor, reason=reason)


def test_decision_joins_account_history(service):
    brief = service.assemble("ACC-002", as_of=AS_OF)
    service.decide(
        brief.brief_id, action="escalate", actor="R. Osei", reason="Refer to senior."
    )
    later = service.assemble("ACC-002", as_of=date.today())
    decisions = [
        f for f in later.current_facts if f.predicate == "underwriter_decision"
    ]
    assert any(
        f.value == "escalate" and f.entity_ref == brief.brief_id for f in decisions
    )


def test_unknown_brief_and_account(service):
    from zep_context_agent.errors import AccountNotFound, BriefNotFound

    with pytest.raises(BriefNotFound):
        service.get_brief("brief-nope")
    with pytest.raises(AccountNotFound):
        service.assemble("ACC-999", as_of=AS_OF)
