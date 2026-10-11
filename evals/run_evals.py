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

    gates = {
        "brief_correctness": brief_rate >= CORRECTNESS_GATE,
        "temporal_correctness": temporal_rate >= CORRECTNESS_GATE,
        "leakage_cases": leakage_cases_rate == 1.0,
        "leakage_sweep_zero": total_violations == 0,
        "citation_completeness": citations_ok,
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
        "gates": gates,
        "passed": all(gates.values()),
    }


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
    if live is not None:
        print(f"live Zep (reported, not gated): {live.get('summary', live.get('skipped', ''))}")
    print("GATE:", "PASS" if payload["passed"] else "FAIL")
    return 0 if payload["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
