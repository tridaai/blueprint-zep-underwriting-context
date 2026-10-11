"""The demo: one synthetic account, end to end, on the offline adapter.

Assembles the brief for ACC-001 (Meridian Casting Co. — the
account with every trap armed: a limit endorsed down mid-term, a
relocation, a claims count a broker email tried to wish away) and
prints it in full: current facts with citations, the superseded
facts the graph retired, the quarantined source, the verification
result, and the gate it stops at. It then records a demonstration
decision so the audit trail — and the decision joining the
account's history as a new episode — is visible too.

The demo always runs the labelled offline adapter with in-memory
stores: it is a smoke run of the assembly rules, not a Zep
performance claim. For the real profile, serve the API with
CONTEXT_BACKEND=zep (see README).
"""

from __future__ import annotations

from langgraph.checkpoint.memory import MemorySaver

from .datasets import DATASET_PRESENT
from .offline_graph import OfflineContextGraph
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
