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

It closes with the morning portfolio brief over the demo's
world — the whole book of forty accounts, of which only this
one has been ingested and decided: the ranking shows what
that means. The decided account carries no signal; the other
thirty-nine stand at "no decided brief", the unexamined —
which is exactly the distinction the brief exists to draw.

And then the broker book over the same world: six brokers,
grouped off the placing-broker edge on each account's own
submission. This account's broker shows one decided account
and nothing owed; the other brokers' books stand unexamined,
which the decided mix says out loud.
"""

from __future__ import annotations

from langgraph.checkpoint.memory import MemorySaver

from .brokers import render_broker_book_text
from .datasets import DATASET_PRESENT
from .offline_graph import OfflineContextGraph
from .ontology import predicate_label
from .portfolio import render_portfolio_text
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
    # The morning portfolio brief over the demo's world: this
    # account decided and quiet; the rest of the book never
    # opened. The brief counts signals from the same machinery
    # the per-account surfaces use — it invents none of them.
    print()
    print(
        render_portfolio_text(
            service.portfolio_brief(as_of=DATASET_PRESENT)
        ),
        end="",
    )
    # The broker book over the same world: what each broker
    # owes the desk, grouped off the placing-broker edge on
    # each account's own submission. This account's broker
    # shows its one decided account and nothing owed.
    print()
    print(
        render_broker_book_text(
            service.broker_book(as_of=DATASET_PRESENT)
        ),
        end="",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
