"""LangGraph pipeline: ingest → assemble → verify → gate.

The graph earns its place at the gate: with a checkpointer
attached, a run executes to the gate and *pauses* there — graph
state persists under thread_id = the brief id — until the service
resumes it with the underwriter's decision. Everything before the
gate is a straight line, and the step functions below are shared
with the no-checkpointer service path so the two can never drift:

- **ingest** — the account's documents become episodes (screened,
  archived, lineage intact). Idempotent: re-running a brief for an
  already-ingested account re-reads the same graph.
- **assemble** — temporal resolution and citation in code
  (:mod:`assembly`), then the labelled narrative (:mod:`narrate`).
- **verify** — the guardrail checks over the assembled brief
  (:mod:`guardrails`). A brief that fails verification is stored
  as ``VERIFICATION_FAILED`` and never reaches the gate.
- **gate** — an ``interrupt()``. The resume payload is the decision
  (action, actor, reason); the node records it via
  :mod:`decisions` and completes the run.

Code computes everything in the brief; the only model in the
system narrates assembled facts under a numeric verifier, and the
human decides. Nothing in the graph prices, binds, or declines.
"""

from __future__ import annotations

from datetime import date
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph
from langgraph.types import interrupt

from . import assembly, decisions, diffing, guardrails, ingestion, narrate
from . import followthrough as follow_mod
from . import staleness as staleness_mod
from .ontology import Account
from .ports import ContextPort
from .schemas import Brief, VerificationResult


class Runtime:
    """The collaborators the pipeline steps close over."""

    def __init__(
        self, *, port: ContextPort, store, object_store, accounts,
        staleness_store=None, follow_store=None,
    ):
        self.port = port
        self.store = store
        self.object_store = object_store
        self.accounts: dict[str, Account] = accounts
        self.staleness_store = staleness_store
        self.follow_store = follow_store


class BriefState(TypedDict, total=False):
    account_id: str
    as_of: str  # ISO date
    brief_id: str
    brief: dict[str, Any]
    status: str


# -- shared steps ------------------------------------------------------------


def step_ingest(runtime: Runtime, account: Account) -> None:
    before = {
        e.episode_id for e in runtime.port.get_episodes(account.account_id)
    }
    episodes = ingestion.ingest_account(
        runtime.port, account, object_store=runtime.object_store
    )
    new_ids = staleness_mod.new_episode_ids(before, episodes)
    if runtime.staleness_store is not None:
        # New material landed: if the account's latest brief is
        # DECIDED, did the facts move under its decision? (See
        # staleness.py — the decided brief is read, never edited.)
        staleness_mod.evaluate_after_ingestion(
            port=runtime.port,
            brief_store=runtime.store,
            staleness_store=runtime.staleness_store,
            account=account,
            trigger_episode_ids=new_ids,
        )
    if runtime.follow_store is not None:
        # …and did the broker answer an open request-info ask?
        # (See followthrough.py — the two watches are independent
        # and never write each other's records.)
        follow_mod.evaluate_after_ingestion(
            port=runtime.port,
            follow_store=runtime.follow_store,
            account=account,
            trigger_episode_ids=new_ids,
        )


def step_assemble(
    runtime: Runtime, account: Account, as_of: date, brief_id: str
) -> Brief:
    brief = assembly.assemble_brief(runtime.port, account, as_of, brief_id=brief_id)
    return narrate.narrate(brief)


def step_verify(runtime: Runtime, brief: Brief) -> Brief:
    facts = runtime.port.get_facts(brief.account_id)
    citations_complete, leakage_free, violations = guardrails.check_brief(
        account_id=brief.account_id,
        current_facts=brief.current_facts,
        episodes=brief.sources,
        edges=brief.edges,
        account_facts=facts,
    )
    brief.verification = VerificationResult(
        citations_complete=citations_complete,
        leakage_free=leakage_free,
        violations=violations,
    )
    if violations:
        brief.status = "VERIFICATION_FAILED"
        brief.flags.append(
            "Verification failed — this brief must not be used: "
            + "; ".join(violations)
        )
    runtime.store.save(brief)
    # The brief remembers: diff it against the previous stored
    # brief for the account (see diffing for the baseline rule).
    # attach_diff re-saves with the diff attached; a first brief
    # keeps diff=None and the surfaces say so.
    diffing.attach_diff(runtime.store, brief)
    return brief


def run_pipeline(
    runtime: Runtime, account: Account, as_of: date, brief_id: str
) -> Brief:
    """ingest → assemble → verify, without the gate (the
    no-checkpointer path and the graph's first three nodes)."""
    step_ingest(runtime, account)
    brief = step_assemble(runtime, account, as_of, brief_id)
    return step_verify(runtime, brief)


# -- graph -------------------------------------------------------------------


def build_graph(runtime: Runtime, checkpointer=None):
    def ingest_node(state: BriefState) -> dict:
        step_ingest(runtime, runtime.accounts[state["account_id"]])
        return {}

    def assemble_node(state: BriefState) -> dict:
        brief = step_assemble(
            runtime,
            runtime.accounts[state["account_id"]],
            date.fromisoformat(state["as_of"]),
            state["brief_id"],
        )
        return {"brief": brief.model_dump(mode="json")}

    def verify_node(state: BriefState) -> dict:
        brief = step_verify(runtime, Brief.model_validate(state["brief"]))
        return {"brief": brief.model_dump(mode="json"), "status": brief.status}

    def gate_node(state: BriefState) -> dict:
        brief = Brief.model_validate(state["brief"])
        if brief.status == "VERIFICATION_FAILED":
            return {}
        payload = interrupt(
            {
                "brief_id": brief.brief_id,
                "account_id": brief.account_id,
                "status": "AWAITING_UNDERWRITER",
            }
        )
        brief = decisions.apply_decision(
            runtime.port,
            brief,
            action=payload["action"],
            actor=payload["actor"],
            reason=payload["reason"],
        )
        runtime.store.save(brief)
        return {"brief": brief.model_dump(mode="json"), "status": brief.status}

    builder = StateGraph(BriefState)
    builder.add_node("ingest", ingest_node)
    builder.add_node("assemble", assemble_node)
    builder.add_node("verify", verify_node)
    builder.add_node("gate", gate_node)
    builder.set_entry_point("ingest")
    builder.add_edge("ingest", "assemble")
    builder.add_edge("assemble", "verify")
    builder.add_edge("verify", "gate")
    builder.add_edge("gate", END)
    return builder.compile(checkpointer=checkpointer)
