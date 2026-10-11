"""The demo: one synthetic account, end to end, on the offline adapter.

Walks ACC-001 (Meridian Casting Co. — the account with every trap
armed: a limit endorsed down mid-term, a relocation, a claims
count a broker email tried to wish away) through the desk's whole
path. Ingestion first: the broker's relocation letter arrives as
correspondence, so its assertion waits at the review gate — the
demo shows the queue, and the underwriter confirms the relocation
into the graph. Then the brief, printed in full: current facts
with citations, the superseded facts the graph retired, the
quarantined source, the verification result, and the gate it
stops at. It then records a demonstration decision so the audit
trail — and the decision joining the account's history as a new
episode — is visible too.

The demo always runs the labelled offline adapter with in-memory
stores: it is a smoke run of the assembly rules, not a Zep
performance claim. For the real profile, serve the API with
CONTEXT_BACKEND=zep (see README).
"""

from __future__ import annotations

from langgraph.checkpoint.memory import MemorySaver

from .datasets import DATASET_PRESENT
from .offline_graph import OfflineContextGraph
from .ontology import predicate_label
from .render import render_brief_text
from .service import BriefService
from .store import InMemoryBriefStore

DEMO_ACCOUNT = "ACC-001"


def main() -> int:
    service = BriefService(
        port=OfflineContextGraph(),
        store=InMemoryBriefStore(),
        object_store=None,
        checkpointer=MemorySaver(),
    )
    # The review gate, walked once: ingestion holds the broker's
    # relocation letter at the gate (the injection email's
    # assertion never gets even that far — the screen quarantined
    # it), and the underwriter confirms what the letter asserted.
    service.ingest(DEMO_ACCOUNT)
    pending = service.list_candidates(DEMO_ACCOUNT, state="pending")
    for candidate in pending:
        print(
            f"REVIEW GATE: {predicate_label(candidate.predicate)} "
            f"from \"{candidate.doc_title}\" waits as a candidate — "
            "correspondence does not enter the graph on its own say-so."
        )
        service.review_candidate(
            candidate.candidate_id,
            account_id=DEMO_ACCOUNT,
            action="confirm",
            actor="Demo Underwriter",
        )
        print(
            "  confirmed by Demo Underwriter — the fact is in the "
            "graph, stamped with the attestation."
        )
    brief = service.assemble(DEMO_ACCOUNT, as_of=DATASET_PRESENT)
    print(render_brief_text(brief))
    decided = service.decide(
        brief.brief_id,
        action="proceed",
        actor="Demo Underwriter",
        reason="Current facts support renewal on the endorsed terms.",
    )
    print(
        f"GATE DECISION RECORDED: {decided.decision.action} by "
        f"{decided.decision.actor} — {decided.decision.reason}"
    )
    print(
        "The decision has joined the account's history as a new "
        "episode; the next brief for this account cites it as a "
        "current fact."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
