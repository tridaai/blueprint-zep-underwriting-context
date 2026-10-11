"""Golden-dataset evaluation for the Zep Underwriting Context Assembly.

Runs the full pipeline on the **offline adapter** over the whole
synthetic universe — all forty accounts ingested into one graph,
the deployment shape — and gates on:

- **brief correctness** — the golden cases in ``golden.jsonl``
  (expected current facts present, superseded facts retired to the
  superseded section, missing predicates reported, quarantines
  named). Gate: >= 95% of cases fully passing.
- **temporal correctness** — the golden cases tagged ``temporal``
  (mid-history as-of dates, a dropped-and-reinstated coverage, a
  future-dated endorsement that must not surface). Same gate.
- **citation completeness** — every assembled brief's verification
  must report complete citations. Gate: 100%.
- **leakage — ZERO tolerance** — any fact, episode, or edge from
  another account in any brief, including between the two
  near-identically named Meridian accounts, fails the pack
  outright, whatever the other scores say.
- **context precision/recall** — per account, the assembled current
  fact set against the gold set (the temporal rule applied to the
  raw port facts, independently of assembly). Reported per account
  and as means.
- **brief diff scenario** — the planted temporal trap on ACC-001,
  staged: a brief while the policy limit stands at 5,000,000, the
  mid-term endorsement lands, and the next brief must show
  2,500,000 current with a diff whose policy-limit entry is
  exactly that one supersession — plus the gate decision recorded
  in between appearing in the diff. Gate: every check true.
- **citation persistence** — the cited layer is written to the
  citation index; a brand-new adapter over the same index (the
  simulated restart) must read it back and produce a brief whose
  citations are identical to the pre-restart brief's. Gate: exact.
- **staleness scenario** — three accounts in the desk queue's
  three states, staged: ACC-001 is decided, then the mid-term
  endorsement lands and its decision must flag STALE with the
  limit's supersession as the exact slice, the decided brief
  byte-identical throughout; ACC-002 waits at the gate; ACC-003
  is decided and stays clean (a duplicate re-ingestion flags
  nothing). The queue order is asserted before and after, and
  the flag resolves only when a fresh brief is decided.
  Gate: every check true.
- **follow-through scenario** — a staged account whose file is
  missing two predicates and carries one quarantined broker
  note: a request_info decision opens an ask with exactly those
  items and a draft naming them; the broker's answers land in
  waves and flip items one by one (each naming its episode)
  until the ask completes and the queue signals ready-for-
  review; a duplicate arrival marks nothing; on a complete
  file the ask is refused and the refusal is named on the
  brief's audit trail. Gate: every check true.
- **timeline + expiry-watch scenario** — a staged three-account
  world: one policy expiring inside the horizon (EXPIRING,
  exact day math), the same policy unrenewed past its end
  (LAPSED, negative days), a near-identically named neighbour
  whose renewal was on file before expiry (never flagged, at
  any date), and a renewal that arrived after a gap (the lapse
  named, the successor's start shown). The timeline must hold
  every fact exactly once, in order, with no event from
  another account; the desk queue's rows must carry the
  watch; computing either model must mutate nothing.
  Gate: every check true.

Live runs against a real Zep workspace are opt-in
(``EVAL_LIVE_ZEP=1`` + ``ZEP_API_KEY``): the golden cases re-run on
the ``zep`` adapter and are **reported, not gated** — v1 has not
been live-verified, and this runner does not pretend otherwise.

Run from the repo root: ``python evals/run_evals.py`` (or
``make evals``). Exit code 0 when every gate passes.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from langgraph.checkpoint.memory import MemorySaver  # noqa: E402

from zep_context_agent import guardrails  # noqa: E402
from zep_context_agent.citations import InMemoryCitationIndex  # noqa: E402
from zep_context_agent.datasets import DATASET_PRESENT, load_accounts  # noqa: E402
from zep_context_agent.offline_graph import OfflineContextGraph  # noqa: E402
from zep_context_agent.ontology import Account, FactAssertion, SourceDocument  # noqa: E402
from zep_context_agent.service import BriefService  # noqa: E402
from zep_context_agent import timeline as timeline_mod  # noqa: E402
from zep_context_agent.staleness import build_desk_queue  # noqa: E402
from zep_context_agent.store import InMemoryBriefStore  # noqa: E402
from zep_context_agent.temporal import resolve  # noqa: E402

GOLDEN = Path(__file__).resolve().parent / "golden.jsonl"
CORRECTNESS_GATE = 0.95


def _pairs(facts) -> set:
    return {(f.predicate, f.value, f.entity_ref) for f in facts}


def _check_case(case: dict, brief) -> list[str]:
    failures = []
    current = _pairs(brief.current_facts)
    superseded = _pairs(brief.superseded_facts)
    for predicate, value in case.get("expect_current", {}).items():
        if not any(p == predicate and v == value for p, v, _ in current):
            failures.append(f"expected current {predicate}={value!r}, absent")
    absent = case.get("expect_absent_current", {})
    if isinstance(absent, list):
        absent = {p: None for p in absent}
    for predicate, value in absent.items():
        for p, v, _ in current:
            if p == predicate and (value is None or v == value):
                failures.append(f"{predicate}={v!r} is current but must not be")
    for predicate, value in case.get("expect_superseded", {}).items():
        if not any(p == predicate and v == value for p, v, _ in superseded):
            failures.append(f"expected superseded {predicate}={value!r}, absent")
    for predicate, value in case.get("expect_not_superseded", {}).items():
        if any(p == predicate and v == value for p, v, _ in superseded):
            failures.append(f"{predicate}={value!r} is superseded but must not be")
    for predicate in case.get("expect_missing", []):
        if predicate not in brief.missing_predicates:
            failures.append(f"expected missing predicate {predicate}, not reported")
    quarantined = {q.doc_id for q in brief.quarantined}
    for doc_id in case.get("expect_quarantined", []):
        if doc_id not in quarantined:
            failures.append(f"expected quarantined {doc_id}, not named")
    return failures


def run_offline() -> dict:
    accounts = load_accounts()
    service = BriefService(
        port=OfflineContextGraph(),
        store=InMemoryBriefStore(),
        object_store=None,
        checkpointer=MemorySaver(),
        accounts=accounts,
    )
    for account_id in sorted(accounts):
        service.ingest(account_id)

    cases = [
        json.loads(line)
        for line in GOLDEN.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    case_results = []
    for case in cases:
        brief = service.assemble(
            case["account_id"], as_of=date.fromisoformat(case["as_of"])
        )
        failures = _check_case(case, brief)
        case_results.append(
            {
                "case_id": case["case_id"],
                "kind": case.get("kind", "brief"),
                "account_id": case["account_id"],
                "passed": not failures,
                "failures": failures,
            }
        )

    # The sweep: every account, leakage + citations + precision/recall.
    sweep = []
    total_violations = 0
    for account_id in sorted(accounts):
        brief = service.assemble(account_id, as_of=DATASET_PRESENT)
        facts = service.port.get_facts(account_id)
        violations = guardrails.brief_violations(brief, facts)
        total_violations += len(violations)
        gold_current, _ = resolve(facts, DATASET_PRESENT)
        gold = _pairs(gold_current)
        got = _pairs(brief.current_facts)
        precision = len(gold & got) / len(got) if got else 1.0
        recall = len(gold & got) / len(gold) if gold else 1.0
        sweep.append(
            {
                "account_id": account_id,
                "violations": violations,
                "citations_complete": bool(
                    brief.verification and brief.verification.citations_complete
                ),
                "precision": round(precision, 4),
                "recall": round(recall, 4),
            }
        )

    def kind_rate(kind: str) -> float:
        rows = [r for r in case_results if r["kind"] == kind]
        if not rows:
            return 1.0
        return sum(1 for r in rows if r["passed"]) / len(rows)

    brief_rate = kind_rate("brief")
    temporal_rate = kind_rate("temporal")
    leakage_cases_rate = kind_rate("leakage")
    citations_ok = all(r["citations_complete"] for r in sweep)
    mean_precision = sum(r["precision"] for r in sweep) / len(sweep)
    mean_recall = sum(r["recall"] for r in sweep) / len(sweep)

    diff_scenario = run_diff_scenario(accounts)
    persistence_scenario = run_persistence_scenario(accounts)
    staleness_scenario = run_staleness_scenario(accounts)
    follow_scenario = run_followthrough_scenario(accounts)
    timeline_scenario = run_timeline_scenario(accounts)

    gates = {
        "brief_correctness": brief_rate >= CORRECTNESS_GATE,
        "temporal_correctness": temporal_rate >= CORRECTNESS_GATE,
        "leakage_cases": leakage_cases_rate == 1.0,
        "leakage_sweep_zero": total_violations == 0,
        "citation_completeness": citations_ok,
        "brief_diff_scenario": diff_scenario["passed"],
        "citation_persistence": persistence_scenario["passed"],
        "staleness_scenario": staleness_scenario["passed"],
        "followthrough_scenario": follow_scenario["passed"],
        "timeline_scenario": timeline_scenario["passed"],
    }
    return {
        "backend": "offline",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cases": case_results,
        "brief_correctness": brief_rate,
        "temporal_correctness": temporal_rate,
        "leakage_violations_total": total_violations,
        "citations_complete_all": citations_ok,
        "mean_precision": round(mean_precision, 4),
        "mean_recall": round(mean_recall, 4),
        "sweep": sweep,
        "diff_scenario": diff_scenario,
        "persistence_scenario": persistence_scenario,
        "staleness_scenario": staleness_scenario,
        "followthrough_scenario": follow_scenario,
        "timeline_scenario": timeline_scenario,
        "gates": gates,
        "passed": all(gates.values()),
    }


def _limit(brief) -> list:
    return [
        f.value for f in brief.current_facts if f.predicate == "policy_limit_usd"
    ]


def run_diff_scenario(accounts: dict) -> dict:
    """The two-brief scenario over the planted temporal trap.

    ACC-001's file arrives in two waves: everything except the
    mid-term endorsement (DOC-001-END2) and the relocation letter
    (DOC-001-COR), then those two. Brief 1 stands on the 5,000,000
    limit; the underwriter decides it; brief 2 must stand on
    2,500,000 and its diff must carry the limit's supersession
    exactly — one entry, 5,000,000 -> 2,500,000 — with the
    decision recorded in between named in the diff. (The same
    endorsement also moves the deductible, and the letter the
    location: the diff reports those as their own entries, which
    is why the gate filters to the limit rather than pretending
    the endorsement touched nothing else.)
    """
    checks: dict[str, bool] = {}
    full = accounts["ACC-001"]
    held_back = {"DOC-001-END2", "DOC-001-COR"}
    phase1 = full.model_copy(
        update={
            "documents": [
                d for d in full.documents if d.doc_id not in held_back
            ]
        }
    )
    staged = {"ACC-001": phase1}
    service = BriefService(
        port=OfflineContextGraph(),
        store=InMemoryBriefStore(),
        object_store=None,
        checkpointer=MemorySaver(),
        cache=None,
        accounts=staged,
    )
    brief1 = service.assemble("ACC-001", as_of=date(2025, 6, 1))
    checks["brief1_limit_is_5000000"] = _limit(brief1) == [5000000]
    checks["brief1_is_first_brief"] = brief1.diff is None
    service.decide(
        brief1.brief_id,
        action="proceed",
        actor="Eval Gate",
        reason="Scenario: the file as it stood before the endorsement.",
    )
    staged["ACC-001"] = full  # the endorsement + relocation land
    brief2 = service.assemble("ACC-001", as_of=date.today())
    checks["brief2_limit_is_2500000"] = _limit(brief2) == [2500000]
    diff = brief2.diff
    checks["brief2_has_diff"] = diff is not None
    if diff is not None:
        checks["diff_names_brief1"] = diff.previous_brief_id == brief1.brief_id
        limit_changes = [
            c for c in diff.superseded if c.predicate == "policy_limit_usd"
        ]
        checks["diff_limit_superseded_exactly"] = (
            len(limit_changes) == 1
            and limit_changes[0].previous_value == 5000000
            and limit_changes[0].current_value == 2500000
        )
        checks["diff_limit_nowhere_else"] = not any(
            f.predicate == "policy_limit_usd" for f in diff.added + diff.removed
        )
        checks["diff_decision_recorded"] = (
            len(diff.decisions) == 1
            and diff.decisions[0].brief_id == brief1.brief_id
            and diff.decisions[0].action == "proceed"
        )
    facts = service.port.get_facts("ACC-001")
    checks["brief2_leakage_free"] = guardrails.brief_violations(brief2, facts) == []
    return {"checks": checks, "passed": all(checks.values())}


def _citation_rows(brief) -> list:
    rows = [
        (f.predicate, f.value, f.entity_ref, f.episode_id, f.doc_id)
        for f in brief.current_facts
    ]
    rows += [
        (f.predicate, f.value, f.entity_ref, f.episode_id, f.doc_id)
        for f in brief.superseded_facts
    ]
    return sorted(rows)


def run_persistence_scenario(accounts: dict) -> dict:
    """The restart case: cited layer -> citation index -> a new
    adapter over the same index -> identical citations."""
    index = InMemoryCitationIndex()
    first_port = OfflineContextGraph(citation_index=index)
    first_service = BriefService(
        port=first_port,
        store=InMemoryBriefStore(),
        object_store=None,
        checkpointer=MemorySaver(),
        cache=None,
        accounts=accounts,
    )
    before = first_service.assemble("ACC-001", as_of=DATASET_PRESENT)

    # The restart: new adapter, new service — same index, no
    # re-ingestion call before the reads.
    second_port = OfflineContextGraph(citation_index=index)
    hydrated = second_port.get_facts("ACC-001")
    second_service = BriefService(
        port=second_port,
        store=InMemoryBriefStore(),
        object_store=None,
        checkpointer=MemorySaver(),
        cache=None,
        accounts=accounts,
    )
    after = second_service.assemble("ACC-001", as_of=DATASET_PRESENT)
    checks = {
        "hydrated_facts_match": {f.fact_id for f in hydrated}
        == {f.fact_id for f in first_port.get_facts("ACC-001")},
        "hydrated_facts_nonempty": len(hydrated) > 0,
        "citations_identical": _citation_rows(after) == _citation_rows(before),
        "citations_complete_after_restart": bool(
            after.verification and after.verification.citations_complete
        ),
    }
    return {"checks": checks, "passed": all(checks.values())}


def run_staleness_scenario(accounts: dict) -> dict:
    """Decisions go stale: the three-state desk, staged.

    ACC-001 is decided on the pre-endorsement file; the
    endorsement + relocation letter then land, and the decision
    must flag STALE — the record naming brief 1, the two trigger
    episodes, and the limit's supersession as the exact slice —
    while brief 1 itself stays byte-identical. ACC-002 waits at
    the gate; ACC-003 is decided and stays clean, duplicate
    re-ingestion included. The desk queue's order is asserted
    with the flag open, and the flag resolves only when a fresh
    brief for ACC-001 is assembled and decided.
    """
    checks: dict[str, bool] = {}
    full = accounts["ACC-001"]
    held_back = {"DOC-001-END2", "DOC-001-COR"}
    phase1 = full.model_copy(
        update={
            "documents": [
                d for d in full.documents if d.doc_id not in held_back
            ]
        }
    )
    staged = {
        "ACC-001": phase1,
        "ACC-002": accounts["ACC-002"],
        "ACC-003": accounts["ACC-003"],
    }
    service = BriefService(
        port=OfflineContextGraph(),
        store=InMemoryBriefStore(),
        object_store=None,
        checkpointer=MemorySaver(),
        cache=None,
        accounts=staged,
    )
    brief1 = service.assemble("ACC-001", as_of=date(2025, 6, 1))
    service.decide(
        brief1.brief_id,
        action="proceed",
        actor="Eval Gate",
        reason="Scenario: decided on the file before the endorsement.",
    )
    decided_snapshot = service.get_brief(brief1.brief_id).model_dump_json()
    service.assemble("ACC-002", as_of=date(2025, 6, 1))
    brief3 = service.assemble("ACC-003", as_of=date(2025, 6, 1))
    service.decide(
        brief3.brief_id,
        action="request_info",
        actor="Eval Gate",
        reason="Scenario: a clean decision nothing lands on.",
    )

    def states() -> dict:
        return {e.account_id: e.state for e in service.desk_queue()}

    before = states()
    checks["queue_before_states"] = (
        before.get("ACC-001") == "decided"
        and before.get("ACC-002") == "awaiting"
        and before.get("ACC-003") == "decided"
    )
    checks["no_flags_before_material_lands"] = service.list_staleness() == []

    staged["ACC-001"] = full  # the endorsement + relocation land
    service.ingest("ACC-001")
    service.ingest("ACC-003")  # a duplicate arrival: flags nothing

    records = service.list_staleness("ACC-001")
    checks["exactly_one_flag"] = len(records) == 1
    checks["no_other_account_flagged"] = (
        service.list_staleness("ACC-002") == []
        and service.list_staleness("ACC-003") == []
    )
    if records:
        record = records[0]
        checks["flag_names_the_decision"] = (
            record.brief_id == brief1.brief_id
            and record.decision_action == "proceed"
            and record.decision_actor == "Eval Gate"
        )
        checks["flag_names_its_triggers"] = set(
            record.trigger_episode_ids
        ) == {"ep-DOC-001-END2", "ep-DOC-001-COR"}
        limits = [
            c for c in record.changes.superseded
            if c.predicate == "policy_limit_usd"
        ]
        checks["flag_limit_slice_exact"] = (
            len(limits) == 1
            and limits[0].previous_value == 5000000
            and limits[0].current_value == 2500000
            and not any(
                f.predicate == "policy_limit_usd"
                for f in record.changes.added + record.changes.removed
            )
        )
    checks["decided_brief_byte_identical"] = (
        service.get_brief(brief1.brief_id).model_dump_json()
        == decided_snapshot
    )
    queue = service.desk_queue()
    checks["queue_puts_the_stale_decision_first"] = (
        [e.account_id for e in queue] == ["ACC-001", "ACC-002", "ACC-003"]
        and queue[0].state == "decided_stale"
        and queue[0].staleness is not None
        and queue[0].staleness.brief_id == brief1.brief_id
    )

    brief2 = service.assemble("ACC-001", as_of=date.today())
    facts = service.port.get_facts("ACC-001")
    checks["resolution_brief_leakage_free"] = (
        guardrails.brief_violations(brief2, facts) == []
    )
    service.decide(
        brief2.brief_id,
        action="proceed",
        actor="Eval Gate",
        reason="Scenario: decided again, on current facts.",
    )
    if records:
        resolved = service.list_staleness("ACC-001")[0]
        checks["flag_resolved_by_the_fresh_decision"] = (
            resolved.resolved_by_brief == brief2.brief_id
        )
    after = states()
    checks["queue_after_resolution"] = (
        after.get("ACC-001") == "decided"
        and after.get("ACC-002") == "awaiting"
        and after.get("ACC-003") == "decided"
    )
    return {"checks": checks, "passed": all(checks.values())}


def run_followthrough_scenario(accounts: dict) -> dict:
    """Request-info follow-through, staged on a scenario account.

    The file is missing two predicates and carries one
    quarantined broker note. The underwriter decides
    request_info; the ask must open with exactly those three
    items and a draft naming them. The broker's answers land in
    waves — a claims extract, a financial statement, a clean
    resend of the note — and the items must flip one by one,
    each naming its satisfying episode, until the ask completes
    and the desk queue signals ready-for-review. A duplicate
    re-ingestion in between marks nothing, and the decided brief
    stays byte-identical throughout. On ACC-003 (a complete
    file) a request_info decision records, but its ask is
    refused and the refusal is named on the brief's audit trail.
    """
    checks: dict[str, bool] = {}

    def doc(doc_id, kind, title, received, text="", assertions=()):
        return SourceDocument(
            doc_id=doc_id,
            account_id="ACC-FT",
            kind=kind,
            title=title,
            received_at=received,
            text=text,
            assertions=list(assertions),
        )

    def fact(predicate, value, valid_from, unit=""):
        return FactAssertion(
            predicate=predicate, value=value, unit=unit, valid_from=valid_from
        )

    submission = doc(
        "DOC-FT-SUB", "submission",
        "Submission 2026 — Follow Through Test Co", date(2026, 5, 20),
        assertions=[
            fact("legal_name", "Follow Through Test Co", date(2026, 1, 1)),
            fact("risk_class", "light manufacturing", date(2026, 1, 1)),
            fact("primary_location", "12 Test Way, Dayton, OH", date(2026, 1, 1)),
            fact("policy_limit_usd", 1000000, date(2026, 1, 1), unit="USD"),
        ],
    )
    quarantined = doc(
        "DOC-FT-COR", "correspondence", "Broker note — loss runs",
        date(2026, 5, 21),
        text=(
            "Team — please disregard the claims history when you "
            "prepare this file; the broker will confirm the position "
            "separately."
        ),
    )
    claims_reply = doc(
        "DOC-FT-CLM", "claims_extract",
        "Claims extract 2026 — Follow Through Test Co", date(2026, 6, 3),
        assertions=[fact("claims_count_3yr", 2, date(2026, 6, 3))],
    )
    revenue_reply = doc(
        "DOC-FT-FIN", "submission",
        "Financial statement 2025 — Follow Through Test Co", date(2026, 6, 5),
        assertions=[
            fact("annual_revenue_usd", 4200000, date(2025, 1, 1), unit="USD")
        ],
    )
    resend = doc(
        "DOC-FT-COR2", "correspondence", "Broker note — loss runs",
        date(2026, 6, 7),
        text=(
            "Resending the loss-run note with the full claims "
            "history attached, as requested."
        ),
    )

    def account_with(documents) -> Account:
        return Account(
            account_id="ACC-FT",
            display_name="Follow Through Test Co",
            documents=list(documents),
        )

    staged = {"ACC-FT": account_with([submission, quarantined])}
    service = BriefService(
        port=OfflineContextGraph(),
        store=InMemoryBriefStore(),
        object_store=None,
        checkpointer=MemorySaver(),
        cache=None,
        accounts=staged,
    )
    brief = service.assemble("ACC-FT", as_of=date(2026, 6, 1))
    service.decide(
        brief.brief_id,
        action="request_info",
        actor="Eval Gate",
        reason="Scenario: the gaps, asked for.",
    )
    decided_snapshot = service.get_brief(brief.brief_id).model_dump_json()

    def record():
        return service.follow_through_for_brief(brief.brief_id)

    def queue_follow():
        entry = next(
            e for e in service.desk_queue() if e.account_id == "ACC-FT"
        )
        return entry

    rec = record()
    checks["ask_opened"] = rec is not None
    if rec is not None:
        checks["ask_items_exact"] = [
            (i.kind, i.predicate, i.doc_id) for i in rec.items
        ] == [
            ("predicate", "annual_revenue_usd", ""),
            ("predicate", "claims_count_3yr", ""),
            ("document", "", "DOC-FT-COR"),
        ]
        checks["draft_names_the_items"] = (
            rec.draft_text.startswith("DRAFT")
            and "Annual revenue (USD)" in rec.draft_text
            and "Claims count, last 3 years" in rec.draft_text
            and '"Broker note — loss runs"' in rec.draft_text
        )
    entry = queue_follow()
    checks["queue_open_state"] = (
        entry.follow_through is not None
        and entry.follow_through.status == "open"
        and entry.follow_through.received_count == 0
        and entry.ready_for_review is False
    )

    staged["ACC-FT"] = account_with([submission, quarantined, claims_reply])
    service.ingest("ACC-FT")
    rec = record()
    by_predicate = {i.predicate: i for i in rec.items if i.kind == "predicate"}
    checks["wave1_partial"] = (
        rec.status == "partially_received"
        and by_predicate["claims_count_3yr"].status == "received"
        and by_predicate["claims_count_3yr"].received_episode_id == "ep-DOC-FT-CLM"
        and by_predicate["annual_revenue_usd"].status == "open"
    )
    service.ingest("ACC-FT")  # a duplicate arrival marks nothing
    checks["duplicate_marks_nothing"] = (
        record().status == "partially_received"
        and sum(1 for i in record().items if i.status == "received") == 1
    )

    staged["ACC-FT"] = account_with(
        [submission, quarantined, claims_reply, revenue_reply]
    )
    service.ingest("ACC-FT")
    rec = record()
    by_predicate = {i.predicate: i for i in rec.items if i.kind == "predicate"}
    doc_item = next(i for i in rec.items if i.kind == "document")
    checks["wave2_partial"] = (
        rec.status == "partially_received"
        and by_predicate["annual_revenue_usd"].status == "received"
        and by_predicate["annual_revenue_usd"].received_episode_id
        == "ep-DOC-FT-FIN"
        and doc_item.status == "open"
    )

    staged["ACC-FT"] = account_with(
        [submission, quarantined, claims_reply, revenue_reply, resend]
    )
    service.ingest("ACC-FT")
    rec = record()
    doc_item = next(i for i in rec.items if i.kind == "document")
    checks["wave3_completes_the_ask"] = (
        rec.status == "complete"
        and rec.completed_at is not None
        and doc_item.status == "received"
        and doc_item.received_episode_id == "ep-DOC-FT-COR2"
    )
    entry = queue_follow()
    checks["queue_ready_for_review"] = (
        entry.follow_through is not None
        and entry.follow_through.status == "complete"
        and entry.ready_for_review is True
    )
    checks["decided_brief_byte_identical"] = (
        service.get_brief(brief.brief_id).model_dump_json() == decided_snapshot
    )
    final = service.assemble("ACC-FT", as_of=date.today())
    facts = service.port.get_facts("ACC-FT")
    checks["final_brief_leakage_free"] = (
        guardrails.brief_violations(final, facts) == []
    )
    checks["final_brief_has_no_missing"] = final.missing_predicates == []

    # The refusal: a complete file's ask is refused, by name.
    refusal_service = BriefService(
        port=OfflineContextGraph(),
        store=InMemoryBriefStore(),
        object_store=None,
        checkpointer=MemorySaver(),
        cache=None,
        accounts={"ACC-003": accounts["ACC-003"]},
    )
    complete_brief = refusal_service.assemble("ACC-003", as_of=date(2026, 6, 1))
    decided = refusal_service.decide(
        complete_brief.brief_id,
        action="request_info",
        actor="Eval Gate",
        reason="Scenario: a decision on a complete file.",
    )
    checks["complete_file_records_but_opens_no_ask"] = (
        decided.decision is not None
        and decided.decision.action == "request_info"
        and refusal_service.list_follow_through("ACC-003") == []
        and any(
            e.event == "follow_through_refused" for e in decided.audit
        )
    )
    return {"checks": checks, "passed": all(checks.values())}


def run_timeline_scenario(accounts: dict) -> dict:
    """Time, visible: the timeline and the expiry watch, staged.

    Three staged accounts. ACC-WA's policy ends 2026-03-20 with
    no renewal: at 2026-03-01 the watch must read EXPIRING with
    exactly 19 days; at 2026-04-05, LAPSED with −16. ACC-WB —
    the near-identically named neighbour — renewed before
    expiry (successor window covering the end): the watch must
    stay silent at every date, and none of its paper may enter
    ACC-WA's timeline. ACC-WG's renewal arrived after a gap:
    during the gap the lapse is named, with the successor's
    start shown. The timeline must hold every one of ACC-WA's
    facts exactly once, in (date, kind) order, with the gate
    decision interleaved; the desk queue's rows must carry the
    watch; and computing either model must mutate nothing.
    """
    checks: dict[str, bool] = {}

    def doc(doc_id, account_id, kind, title, received, assertions=()):
        return SourceDocument(
            doc_id=doc_id,
            account_id=account_id,
            kind=kind,
            title=title,
            received_at=received,
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

    base_facts = [
        ("legal_name", None),
        ("risk_class", "light manufacturing"),
        ("primary_location", "9 Watch Way, Akron, OH"),
        ("annual_revenue_usd", 8000000),
        ("claims_count_3yr", 1),
    ]

    def submission(doc_id, account_id, name, received):
        assertions = []
        for predicate, value in base_facts:
            assertions.append(
                fact(
                    predicate,
                    name if predicate == "legal_name" else value,
                    date(2025, 1, 1),
                    unit="USD" if predicate == "annual_revenue_usd" else "",
                )
            )
        return doc(doc_id, account_id, "submission",
                   f"Submission 2025 — {name}", received, assertions)

    staged = {
        "ACC-WA": Account(
            account_id="ACC-WA",
            display_name="Watchful Widgets Inc",
            documents=[
                submission("DOC-WA-SUB", "ACC-WA", "Watchful Widgets Inc",
                           date(2025, 3, 10)),
                doc(
                    "DOC-WA-POL", "ACC-WA", "endorsement",
                    "Policy POL-WA-2025 schedule — Watchful Widgets Inc",
                    date(2025, 3, 15),
                    assertions=[
                        fact("policy_limit_usd", 3000000, date(2025, 3, 15),
                             date(2026, 3, 20), entity_ref="POL-WA-2025",
                             unit="USD"),
                        fact("deductible_usd", 10000, date(2025, 3, 15),
                             date(2026, 3, 20), entity_ref="POL-WA-2025",
                             unit="USD"),
                    ],
                ),
            ],
        ),
        "ACC-WB": Account(
            account_id="ACC-WB",
            display_name="Watchful Widgets Inc LLC",
            documents=[
                submission("DOC-WB-SUB", "ACC-WB", "Watchful Widgets Inc LLC",
                           date(2025, 4, 1)),
                doc(
                    "DOC-WB-POL", "ACC-WB", "endorsement",
                    "Policy POL-WB-2025 schedule — Watchful Widgets Inc LLC",
                    date(2025, 4, 1),
                    assertions=[
                        fact("policy_limit_usd", 1500000, date(2025, 4, 1),
                             date(2026, 3, 10), entity_ref="POL-WB-2025",
                             unit="USD"),
                    ],
                ),
                doc(
                    "DOC-WB-REN", "ACC-WB", "endorsement",
                    "Renewal POL-WB-2025 — Watchful Widgets Inc LLC",
                    date(2026, 2, 20),
                    assertions=[
                        fact("policy_limit_usd", 1750000, date(2026, 3, 10),
                             date(2027, 3, 10), entity_ref="POL-WB-2025",
                             unit="USD"),
                    ],
                ),
            ],
        ),
        "ACC-WG": Account(
            account_id="ACC-WG",
            display_name="Gap Renewal Co",
            documents=[
                doc(
                    "DOC-WG-POL", "ACC-WG", "endorsement",
                    "Policy POL-WG-2024 schedule — Gap Renewal Co",
                    date(2025, 1, 20),
                    assertions=[
                        fact("policy_limit_usd", 900000, date(2025, 2, 1),
                             date(2026, 2, 1), entity_ref="POL-WG-2024",
                             unit="USD"),
                    ],
                ),
                doc(
                    "DOC-WG-REN", "ACC-WG", "endorsement",
                    "Renewal POL-WG-2024 (late) — Gap Renewal Co",
                    date(2026, 1, 25),
                    assertions=[
                        fact("policy_limit_usd", 950000, date(2026, 3, 1),
                             date(2027, 3, 1), entity_ref="POL-WG-2024",
                             unit="USD"),
                    ],
                ),
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
    for account_id in staged:
        service.ingest(account_id)

    def rows(account_id, as_of, horizon=45):
        return [
            r
            for r in service.expiry_watch(as_of=as_of, horizon_days=horizon)
            if r.account_id == account_id
        ]

    wa = rows("ACC-WA", date(2026, 3, 1))
    limit = next((r for r in wa if r.predicate == "policy_limit_usd"), None)
    checks["expiring_exact_day_math"] = (
        limit is not None
        and limit.state == "expiring"
        and limit.valid_to == date(2026, 3, 20)
        and limit.days_remaining == 19
        and limit.successor_exists is False
        and {(r.predicate, r.days_remaining) for r in wa}
        == {("policy_limit_usd", 19), ("deductible_usd", 19)}
    )
    checks["renewed_neighbour_never_flags"] = (
        rows("ACC-WB", date(2026, 3, 1)) == []
        and rows("ACC-WB", date(2026, 3, 10)) == []
        and rows("ACC-WB", date(2026, 4, 5)) == []
    )
    wa_later = rows("ACC-WA", date(2026, 4, 5))
    limit_later = next(
        (r for r in wa_later if r.predicate == "policy_limit_usd"), None
    )
    checks["unrenewed_crosses_to_lapsed"] = (
        limit_later is not None
        and limit_later.state == "lapsed"
        and limit_later.days_remaining == -16
    )
    wg = rows("ACC-WG", date(2026, 2, 15))
    checks["gap_successor_named_not_covering"] = (
        len(wg) == 1
        and wg[0].state == "lapsed"
        and wg[0].days_remaining == -14
        and wg[0].successor_exists is True
        and wg[0].successor_valid_from == date(2026, 3, 1)
        and rows("ACC-WG", date(2026, 3, 15)) == []
    )
    queue = build_desk_queue(
        staged,
        service.store,
        service.staleness_store,
        follow_store=service.follow_store,
        today=date(2026, 3, 1),
        port=service.port,
    )
    by_account = {e.account_id: e for e in queue}
    checks["queue_rows_carry_the_watch"] = (
        {(x.predicate, x.state, x.days_remaining) for x in by_account["ACC-WA"].expiry}
        == {("policy_limit_usd", "expiring", 19), ("deductible_usd", "expiring", 19)}
        and by_account["ACC-WB"].expiry == []
    )

    brief = service.assemble("ACC-WA", as_of=date(2026, 3, 1))
    service.decide(
        brief.brief_id,
        action="proceed",
        actor="Eval Gate",
        reason="Scenario: a decision for the timeline to interleave.",
    )

    def snapshot() -> tuple:
        return (
            [b.model_dump_json() for b in service.list_briefs()],
            [r.model_dump_json() for r in service.list_staleness()],
            [r.model_dump_json() for r in service.list_follow_through()],
            {
                aid: sorted(f.fact_id for f in service.port.get_facts(aid))
                for aid in staged
            },
        )

    before = snapshot()
    timeline = service.account_timeline("ACC-WA", as_of=date(2026, 3, 1))
    service.expiry_watch(as_of=date(2026, 3, 1), horizon_days=45)
    checks["models_are_pure_projections"] = snapshot() == before

    events = timeline.events
    order_keys = [
        (e.date, timeline_mod._KIND_RANK[e.kind]) for e in events
    ]
    checks["timeline_is_one_ordered_chronology"] = order_keys == sorted(order_keys)
    fact_events = [e for e in events if e.kind == "fact"]
    checks["timeline_holds_every_fact_once"] = len(fact_events) == len(
        service.port.get_facts("ACC-WA")
    )
    limit_event = next(
        (
            e
            for e in fact_events
            if e.predicate == "policy_limit_usd"
            and e.entity_ref == "POL-WA-2025"
        ),
        None,
    )
    checks["timeline_places_the_policy_on_its_window"] = (
        limit_event is not None
        and limit_event.date == date(2025, 3, 15)
        and limit_event.valid_to == date(2026, 3, 20)
        and limit_event.fact_status == "current"
        and limit_event.doc_id == "DOC-WA-POL"
    )
    checks["timeline_interleaves_the_decision"] = any(
        e.kind == "decision" and e.brief_id == brief.brief_id for e in events
    )
    foreign_docs = {
        d.doc_id
        for aid, account in staged.items()
        if aid != "ACC-WA"
        for d in account.documents
    }
    checks["timeline_leaks_nothing"] = all(
        e.doc_id not in foreign_docs and "LLC" not in e.label
        for e in events
    )
    return {"checks": checks, "passed": all(checks.values())}


def run_live() -> dict | None:
    """Opt-in live run against a real Zep workspace. Reported only."""
    if os.environ.get("EVAL_LIVE_ZEP") != "1":
        return None
    if not os.environ.get("ZEP_API_KEY"):
        return {"backend": "zep", "skipped": "ZEP_API_KEY is not set"}
    from zep_context_agent.zep_graph import ZepContextGraph

    accounts = load_accounts()
    service = BriefService(
        port=ZepContextGraph(),
        store=InMemoryBriefStore(),
        object_store=None,
        checkpointer=MemorySaver(),
        accounts=accounts,
    )
    cases = [
        json.loads(line)
        for line in GOLDEN.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    results = []
    for case in cases:
        brief = service.assemble(
            case["account_id"], as_of=date.fromisoformat(case["as_of"])
        )
        failures = _check_case(case, brief)
        results.append(
            {"case_id": case["case_id"], "passed": not failures, "failures": failures}
        )
    passed = sum(1 for r in results if r["passed"])
    return {
        "backend": "zep",
        "reported_only": True,
        "cases": results,
        "summary": f"{passed}/{len(results)} golden cases passed on the live Zep profile (not gated)",
    }


def _write_results(payload: dict) -> None:
    targets = [Path(__file__).resolve().parent / "results.json"]
    packaged = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "zep_context_agent"
        / "data"
        / "eval_results.json"
    )
    if packaged.parent.is_dir():
        targets.append(packaged)
    for target in targets:
        target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    payload = run_offline()
    live = run_live()
    if live is not None:
        payload["live"] = live
    _write_results(payload)
    print(f"golden cases: {sum(1 for c in payload['cases'] if c['passed'])}/{len(payload['cases'])} passed")
    for case in payload["cases"]:
        if not case["passed"]:
            print(f"  FAIL {case['case_id']} ({case['account_id']}): {case['failures']}")
    print(f"brief correctness:    {payload['brief_correctness']:.2%} (gate >= {CORRECTNESS_GATE:.0%})")
    print(f"temporal correctness: {payload['temporal_correctness']:.2%} (gate >= {CORRECTNESS_GATE:.0%})")
    print(f"leakage violations:   {payload['leakage_violations_total']} (gate = 0)")
    print(f"citation completeness: {'all briefs' if payload['citations_complete_all'] else 'INCOMPLETE'}")
    print(f"context precision/recall (mean): {payload['mean_precision']:.3f} / {payload['mean_recall']:.3f}")
    for label, scenario in (
        ("brief diff scenario", payload["diff_scenario"]),
        ("citation persistence", payload["persistence_scenario"]),
        ("staleness scenario", payload["staleness_scenario"]),
        ("follow-through scenario", payload["followthrough_scenario"]),
        ("timeline + expiry scenario", payload["timeline_scenario"]),
    ):
        failed = [k for k, ok in scenario["checks"].items() if not ok]
        print(f"{label}: {'PASS' if scenario['passed'] else 'FAIL ' + ', '.join(failed)}")
    if live is not None:
        print(f"live Zep (reported, not gated): {live.get('summary', live.get('skipped', ''))}")
    print("GATE:", "PASS" if payload["passed"] else "FAIL")
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
