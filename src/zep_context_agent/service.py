"""The brief service: the one door every surface walks through.

API, console, CLI, demo, and evals all call this service; it owns
the collaborators (ContextPort, brief store, object store,
checkpointer, account registry) and the two operations that
matter:

- :meth:`assemble` — run the pipeline for one account up to the
  gate and return the stored brief (``AWAITING_UNDERWRITER``).
- :meth:`decide` — record the underwriter's decision. With a
  checkpointer, this *resumes* the paused graph thread and the
  graph records the decision; without one, the service applies
  the same :mod:`decisions` function to the stored brief. Either
  way the record is identical.

Errors are the honest kind: an unknown account is
:class:`AccountNotFound`, a second decision is
:class:`DecisionError`, and a Zep profile that is unconfigured or
unreachable raises from the adapter — nothing here catches those
into a quieter shape.
"""

from __future__ import annotations

import uuid
from datetime import date

from langgraph.types import Command

from . import decisions
from .checkpoints import get_checkpointer
from .datasets import load_accounts
from .errors import AccountNotFound, BriefNotFound, DecisionError
from .graph import Runtime, build_graph, run_pipeline
from .ingestion import ingest_account
from .object_store import get_object_store
from .ontology import Account, Episode
from .schemas import Brief
from .store import default_store
from .wiring import build_context_port


class BriefService:
    def __init__(
        self,
        *,
        port=None,
        store=None,
        object_store="default",
        checkpointer="default",
        accounts: dict[str, Account] | None = None,
    ) -> None:
        self.port = port if port is not None else build_context_port()
        self.store = store if store is not None else default_store()
        self.object_store = (
            get_object_store() if object_store == "default" else object_store
        )
        self.accounts = accounts if accounts is not None else load_accounts()
        self.checkpointer = (
            get_checkpointer() if checkpointer == "default" else checkpointer
        )
        self.runtime = Runtime(
            port=self.port,
            store=self.store,
            object_store=self.object_store,
            accounts=self.accounts,
        )
        self._graph = (
            build_graph(self.runtime, self.checkpointer)
            if self.checkpointer is not None
            else None
        )

    # -- accounts -----------------------------------------------------------

    def list_accounts(self) -> list[Account]:
        return [self.accounts[key] for key in sorted(self.accounts)]

    def get_account(self, account_id: str) -> Account:
        try:
            return self.accounts[account_id]
        except KeyError:
            raise AccountNotFound(f"No account {account_id!r} in the dataset.")

    # -- ingestion ------------------------------------------------------------

    def ingest(self, account_id: str) -> list[Episode]:
        account = self.get_account(account_id)
        return ingest_account(
            self.port, account, object_store=self.object_store
        )

    # -- briefs ---------------------------------------------------------------

    def assemble(self, account_id: str, as_of: date | None = None) -> Brief:
        account = self.get_account(account_id)
        as_of = as_of or date.today()
        brief_id = f"brief-{uuid.uuid4().hex[:12]}"
        if self._graph is not None:
            self._graph.invoke(
                {
                    "account_id": account_id,
                    "as_of": as_of.isoformat(),
                    "brief_id": brief_id,
                },
                config={"configurable": {"thread_id": brief_id}},
            )
            stored = self.store.get(brief_id)
            if stored is None:  # pragma: no cover - verify always saves
                raise RuntimeError(f"Pipeline produced no brief {brief_id}.")
            return stored
        return run_pipeline(self.runtime, account, as_of, brief_id)

    def get_brief(self, brief_id: str) -> Brief:
        brief = self.store.get(brief_id)
        if brief is None:
            raise BriefNotFound(f"No brief {brief_id!r}.")
        return brief

    def list_briefs(self, account_id: str | None = None) -> list[Brief]:
        return self.store.list(account_id)

    # -- the gate ---------------------------------------------------------------

    def decide(
        self, brief_id: str, *, action: str, actor: str, reason: str
    ) -> Brief:
        brief = self.get_brief(brief_id)
        if brief.status == "VERIFICATION_FAILED":
            raise DecisionError(
                f"Brief {brief_id} failed verification and cannot be decided."
            )
        if brief.decision is not None:
            raise DecisionError(
                f"Brief {brief_id} already carries a decision "
                f"({brief.decision.action} by {brief.decision.actor}); "
                "assemble a new brief to revisit the account."
            )
        if not actor or not actor.strip():
            raise DecisionError("A gate decision requires an actor.")
        if not reason or not reason.strip():
            raise DecisionError("A gate decision requires a reason.")
        if self._graph is not None:
            self._graph.invoke(
                Command(
                    resume={"action": action, "actor": actor, "reason": reason}
                ),
                config={"configurable": {"thread_id": brief_id}},
            )
            return self.get_brief(brief_id)
        brief = decisions.apply_decision(
            self.port, brief, action=action, actor=actor, reason=reason
        )
        self.store.save(brief)
        return brief

    # -- readiness --------------------------------------------------------------

    def readiness(self) -> dict:
        from .db import database_url

        url = database_url()
        db_state = "postgres" if url else "in-memory test doubles"
        if url:
            from .db import ping

            db_state = "postgres" if ping(url) else "postgres UNREACHABLE"
        return {
            "context_backend": getattr(self.port, "name", "unknown"),
            "database": db_state,
            "object_store": (
                "s3-compatible" if self.object_store is not None else "inline only"
            ),
            "checkpointer": (
                type(self.checkpointer).__name__
                if self.checkpointer is not None
                else "off (service-state gate)"
            ),
            "accounts_loaded": len(self.accounts),
        }
