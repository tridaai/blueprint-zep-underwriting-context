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
from zep_context_agent.service import BriefService  # noqa: E402
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

    gates = {
        "brief_correctness": brief_rate >= CORRECTNESS_GATE,
        "temporal_correctness": temporal_rate >= CORRECTNESS_GATE,
        "leakage_cases": leakage_cases_rate == 1.0,
        "leakage_sweep_zero": total_violations == 0,
        "citation_completeness": citations_ok,
        "brief_diff_scenario": diff_scenario["passed"],
        "citation_persistence": persistence_scenario["passed"],
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
    ):
        failed = [k for k, ok in scenario["checks"].items() if not ok]
        print(f"{label}: {'PASS' if scenario['passed'] else 'FAIL ' + ', '.join(failed)}")
    if live is not None:
        print(f"live Zep (reported, not gated): {live.get('summary', live.get('skipped', ''))}")
    print("GATE:", "PASS" if payload["passed"] else "FAIL")
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
