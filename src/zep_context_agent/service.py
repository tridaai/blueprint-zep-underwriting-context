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

import logging
import uuid
from contextlib import contextmanager
from datetime import date

from langgraph.types import Command

from . import cache as cache_mod
from . import decisions
from .checkpoints import get_checkpointer
from .datasets import load_accounts
from .errors import (
    AccountNotFound,
    BriefNotFound,
    DecisionError,
    IngestionInProgress,
)
from .graph import Runtime, build_graph, run_pipeline
from .ingestion import ingest_account
from .object_store import get_object_store
from .ontology import Account, Episode
from .schemas import Brief
from .store import default_store
from .wiring import build_context_port

logger = logging.getLogger("zep_context_agent.service")


class BriefService:
    def __init__(
        self,
        *,
        port=None,
        store=None,
        object_store="default",
        checkpointer="default",
        cache="default",
        accounts: dict[str, Account] | None = None,
    ) -> None:
        raw_port = port if port is not None else build_context_port()
        # The cache layer is opt-in by configuration: "default" builds
        # from REDIS_URL and is None (disabled, and readiness says so)
        # when unset. A CachePort passed explicitly — the in-memory
        # double in tests — is used as given.
        self.cache = cache_mod.get_cache() if cache == "default" else cache
        self._caching_port: cache_mod.CachingContextPort | None = None
        if self.cache is not None:
            self._caching_port = cache_mod.CachingContextPort(raw_port, self.cache)
            self.port = self._caching_port
        else:
            self.port = raw_port
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

    @contextmanager
    def _ingest_guard(self, account_id: str):
        """One worker ingests an account at a time, when a cache
        backend provides the coordination lock. Without a cache the
        service is a single-process prototype and proceeds unlocked
        — the mode readiness reports."""
        if self.cache is None:
            yield
            return
        if not self.cache.ping():
            # Coordination backend configured but down: proceed
            # unlocked and say so in the log — readiness already
            # reports the outage; a dead accelerator must not take
            # ingestion down with it.
            logger.warning(
                "cache unreachable: ingesting %s without the coordination lock",
                account_id,
            )
            yield
            return
        key = cache_mod.lock_key(account_id)
        token = self.cache.try_lock(key, cache_mod.LOCK_TTL_SECONDS)
        if token is None:
            raise IngestionInProgress(
                f"Account {account_id} is already being ingested by "
                "another worker; retry when it finishes."
            )
        try:
            yield
        finally:
            self.cache.unlock(key, token)

    def ingest(self, account_id: str) -> list[Episode]:
        account = self.get_account(account_id)
        with self._ingest_guard(account_id):
            return ingest_account(
                self.port, account, object_store=self.object_store
            )

    # -- briefs ---------------------------------------------------------------

    def _facts_version(self, account_id: str) -> int | None:
        if self._caching_port is None:
            return None
        return self._caching_port.facts_version(account_id)

    def assemble(self, account_id: str, as_of: date | None = None) -> Brief:
        account = self.get_account(account_id)
        as_of = as_of or date.today()
        # Brief cache: keyed by account + facts-version + as-of, so a
        # hit is only possible when no episode has been ingested for
        # this account since the brief was assembled. Any ingestion —
        # including a gate decision's memo — bumps the version and
        # orphans the entry.
        version = self._facts_version(account_id)
        if self.cache is not None and version is not None:
            raw = self.cache.get(cache_mod.brief_key(account_id, version, as_of))
            if raw is not None:
                try:
                    return Brief.model_validate_json(raw.decode("utf-8"))
                except Exception:
                    logger.warning("cached brief unreadable; reassembling")
        brief_id = f"brief-{uuid.uuid4().hex[:12]}"
        with self._ingest_guard(account_id):
            if self._graph is not None:
                self._graph.invoke(
                    {
                        "account_id": account_id,
                        "as_of": as_of.isoformat(),
                        "brief_id": brief_id,
                    },
                    config={"configurable": {"thread_id": brief_id}},
                )
                brief = self.store.get(brief_id)
                if brief is None:  # pragma: no cover - verify always saves
                    raise RuntimeError(f"Pipeline produced no brief {brief_id}.")
            else:
                brief = run_pipeline(self.runtime, account, as_of, brief_id)
        if self.cache is not None:
            version = self._facts_version(account_id)
            if version is not None:
                try:
                    self.cache.set(
                        cache_mod.brief_key(account_id, version, as_of),
                        brief.model_dump_json().encode("utf-8"),
                        ttl_seconds=cache_mod.ENTRY_TTL_SECONDS,
                    )
                except Exception:
                    logger.warning("brief cache store failed (skipped)")
        return brief

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
        if self.cache is None:
            cache_state = "disabled (REDIS_URL unset)"
        elif self.cache.name == "redis":
            cache_state = "redis" if self.cache.ping() else "redis UNREACHABLE"
        else:
            cache_state = self.cache.name
        return {
            "context_backend": getattr(self.port, "name", "unknown"),
            "cache": cache_state,
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
