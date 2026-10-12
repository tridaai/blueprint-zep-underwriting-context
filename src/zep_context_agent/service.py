"""The brief service: the one door every surface walks through.

API, console, CLI, demo, and evals all call this service; it owns
the collaborators (ContextPort, brief store, object store,
checkpointer, account registry) and the two operations that
matter:

- :meth:`assemble` — run the pipeline for one account up to the
  gate and return the stored brief (``AWAITING_UNDERWRITER``).
  In renewal mode (see :mod:`renewals`) the brief is assembled
  against the account's last decision; the mode is refused by
  name, before any pipeline work runs, when the account has no
  decided brief to renew against.
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
from datetime import date, datetime, timezone

from langgraph.types import Command

from . import cache as cache_mod
from . import decisions
from . import followthrough as follow_mod
from . import renewals as renewals_mod
from . import review as review_mod
from . import staleness as staleness_mod
from .checkpoints import get_checkpointer
from .datasets import load_accounts
from .errors import (
    AccountNotFound,
    BriefNotFound,
    DecisionError,
    FollowThroughError,
    IngestionInProgress,
    RenewalError,
    ReviewError,
)
from .followthrough import default_follow_store
from .graph import Runtime, build_graph, run_pipeline
from .ingestion import ingest_account
from .object_store import get_object_store
from .ontology import Account, Episode
from .schemas import AuditEntry, Brief
from .staleness import default_staleness_store
from .store import default_store
from .wiring import build_context_port

logger = logging.getLogger("zep_context_agent.service")


class BriefService:
    def __init__(
        self,
        *,
        port=None,
        store=None,
        staleness_store="default",
        follow_store="default",
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
        self.staleness_store = (
            default_staleness_store()
            if staleness_store == "default"
            else staleness_store
        )
        self.follow_store = (
            default_follow_store() if follow_store == "default" else follow_store
        )
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
            staleness_store=self.staleness_store,
            follow_store=self.follow_store,
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
            before = {
                e.episode_id for e in self.port.get_episodes(account_id)
            }
            episodes = ingest_account(
                self.port, account, object_store=self.object_store
            )
            # New material landed: if the account's latest brief is
            # DECIDED, did the facts move under its decision?
            new_ids = staleness_mod.new_episode_ids(before, episodes)
            staleness_mod.evaluate_after_ingestion(
                port=self.port,
                brief_store=self.store,
                staleness_store=self.staleness_store,
                account=account,
                trigger_episode_ids=new_ids,
            )
            # …and did the broker answer an open ask?
            follow_mod.evaluate_after_ingestion(
                port=self.port,
                follow_store=self.follow_store,
                account=account,
                trigger_episode_ids=new_ids,
            )
            return episodes

    # -- the review gate ----------------------------------------------------

    def list_candidates(
        self, account_id: str | None = None, state: str | None = None
    ) -> list:
        """Candidate facts (see :mod:`review`): one account's, or
        every account's in account order."""
        if account_id is not None:
            return self.port.get_candidates(account_id, state)
        candidates = []
        for aid in sorted(self.accounts):
            candidates.extend(self.port.get_candidates(aid, state))
        return candidates

    def review_queue(self, account_id: str | None = None) -> list:
        """The review queue: candidates grouped by document."""
        return review_mod.build_review_queue(
            self.port, self.accounts, account_id
        )

    def review_candidate(
        self,
        candidate_id: str,
        *,
        account_id: str,
        action: str,
        actor: str,
        reason: str = "",
    ):
        """Decide one candidate at the review gate.

        Confirming admits the fact into the graph — and is an
        arrival: the staleness watch and the follow-through
        recognition re-run with the candidate's episode as the
        trigger, exactly as they do for an ingestion (a pending
        candidate's arrival ran neither, because no brief could
        see it). Rejecting keeps the record with its reason and
        changes nothing in the graph."""
        account = self.get_account(account_id)
        if action not in ("confirm", "reject"):
            raise ReviewError(
                f"Unknown review action {action!r}: confirm or reject."
            )
        if not actor or not actor.strip():
            raise ReviewError("A review decision requires an actor.")
        if action == "reject" and not (reason and reason.strip()):
            raise ReviewError(
                "Rejecting a candidate requires a reason — the "
                "record is the audit of what the desk refused."
            )
        candidate = next(
            (
                c
                for c in self.port.get_candidates(account_id)
                if c.candidate_id == candidate_id
            ),
            None,
        )
        if candidate is None:
            raise ReviewError(
                f"No candidate {candidate_id!r} on account {account_id}."
            )
        if candidate.state != "pending":
            raise ReviewError(
                f"Candidate {candidate_id} is already "
                f"{candidate.state} (by {candidate.review_actor}); "
                "a decided candidate cannot be decided again."
            )
        at = datetime.now(timezone.utc)
        if action == "confirm":
            self.port.confirm_candidate(
                account_id, candidate_id, actor=actor, at=at
            )
            staleness_mod.evaluate_after_ingestion(
                port=self.port,
                brief_store=self.store,
                staleness_store=self.staleness_store,
                account=account,
                trigger_episode_ids=[candidate.episode_id],
            )
            follow_mod.evaluate_after_ingestion(
                port=self.port,
                follow_store=self.follow_store,
                account=account,
                trigger_episode_ids=[candidate.episode_id],
            )
        else:
            self.port.reject_candidate(
                account_id,
                candidate_id,
                actor=actor,
                reason=reason,
                at=at,
            )
        return next(
            c
            for c in self.port.get_candidates(account_id)
            if c.candidate_id == candidate_id
        )

    # -- staleness + the desk queue -------------------------------------------

    def list_staleness(self, account_id: str | None = None):
        return self.staleness_store.list(account_id)

    def list_follow_through(self, account_id: str | None = None):
        return self.follow_store.list(account_id)

    def follow_through_for_brief(self, brief_id: str):
        return follow_mod.record_for_brief(self.follow_store, brief_id)

    def desk_queue(self) -> list:
        """Every account in desk order (see staleness.build_desk_queue)."""
        return staleness_mod.build_desk_queue(
            self.accounts,
            self.store,
            self.staleness_store,
            follow_store=self.follow_store,
            port=self.port,
        )

    # -- timeline + the expiry watch -------------------------------------------

    def account_timeline(self, account_id: str, as_of: date | None = None):
        """The account's chronology (see timeline.build_account_timeline):
        facts on their windows, episodes, decisions, flags, asks —
        one ordering, a pure projection."""
        from . import timeline as timeline_mod

        account = self.get_account(account_id)
        return timeline_mod.build_account_timeline(
            port=self.port,
            account=account,
            brief_store=self.store,
            staleness_store=self.staleness_store,
            follow_store=self.follow_store,
            as_of=as_of or date.today(),
        )

    def expiry_watch(
        self, as_of: date | None = None, horizon_days: int | None = None
    ) -> list:
        """The desk-level expiry watch (see timeline.build_expiry_watch):
        relied-on facts running out, most lapsed first. The
        horizon defaults to the configured EXPIRY_HORIZON_DAYS."""
        from . import timeline as timeline_mod
        from .config import expiry_horizon_days

        return timeline_mod.build_expiry_watch(
            port=self.port,
            accounts=self.accounts,
            as_of=as_of or date.today(),
            horizon_days=(
                horizon_days if horizon_days is not None else expiry_horizon_days()
            ),
        )

    # -- renewal chases ---------------------------------------------------------

    def renewal_chases(
        self,
        account_id: str | None = None,
        as_of: date | None = None,
        horizon_days: int | None = None,
    ) -> list:
        """The renewal chases (see chases.build_chases): every
        account's chase rows, or one account's — expiring facts
        the desk can chase, chases already asked, renewals that
        landed, lapses nobody chased. A pure projection over
        the expiry watch, the follow-through store, and the
        brief store; the horizon defaults to the configured
        EXPIRY_HORIZON_DAYS, like the watch."""
        from . import chases as chases_mod
        from .config import expiry_horizon_days

        as_of = as_of or date.today()
        horizon = (
            horizon_days if horizon_days is not None else expiry_horizon_days()
        )
        if account_id is not None:
            account = self.get_account(account_id)
            return chases_mod.chases_for_account(
                port=self.port,
                account=account,
                brief_store=self.store,
                follow_store=self.follow_store,
                as_of=as_of,
                horizon_days=horizon,
            )
        return chases_mod.build_chases(
            port=self.port,
            accounts=self.accounts,
            brief_store=self.store,
            follow_store=self.follow_store,
            as_of=as_of,
            horizon_days=horizon,
        )

    def send_chase(
        self,
        account_id: str,
        predicate: str,
        *,
        actor: str,
        as_of: date | None = None,
        horizon_days: int | None = None,
    ):
        """Send one renewal chase: open its follow-through ask
        on the account's latest brief (see
        chases.send_renewal_chase) and return the record with
        the chase row as it now reads. The send is idempotent
        in the only way that matters — a second send is refused
        by name, because the broker has been asked once. The
        draft on the record is prepared for the underwriter to
        copy; as with every ask here, the system sends nothing
        itself."""
        from . import chases as chases_mod
        from .config import expiry_horizon_days

        account = self.get_account(account_id)
        return chases_mod.send_renewal_chase(
            port=self.port,
            account=account,
            brief_store=self.store,
            follow_store=self.follow_store,
            predicate=predicate,
            actor=actor,
            as_of=as_of or date.today(),
            horizon_days=(
                horizon_days
                if horizon_days is not None
                else expiry_horizon_days()
            ),
        )

    # -- the morning portfolio brief -------------------------------------------

    def portfolio_brief(
        self, as_of: date | None = None, horizon_days: int | None = None
    ):
        """The morning portfolio brief (see portfolio.build_portfolio_brief):
        every account's signals counted from the machinery that
        owns them, ranked by the published weights. A pure
        projection — it mutates nothing and re-orders nothing;
        the desk queue keeps its own order. The horizon defaults
        to the configured EXPIRY_HORIZON_DAYS, like the watch."""
        from . import portfolio as portfolio_mod
        from .config import expiry_horizon_days

        return portfolio_mod.build_portfolio_brief(
            port=self.port,
            accounts=self.accounts,
            brief_store=self.store,
            staleness_store=self.staleness_store,
            follow_store=self.follow_store,
            as_of=as_of or date.today(),
            horizon_days=(
                horizon_days if horizon_days is not None else expiry_horizon_days()
            ),
        )

    # -- briefs ---------------------------------------------------------------

    def _facts_version(self, account_id: str) -> int | None:
        if self._caching_port is None:
            return None
        return self._caching_port.facts_version(account_id)

    def assemble(
        self,
        account_id: str,
        as_of: date | None = None,
        *,
        mode: str = "standard",
    ) -> Brief:
        account = self.get_account(account_id)
        as_of = as_of or date.today()
        if mode not in ("standard", "renewal"):
            raise RenewalError(
                f"Unknown assembly mode {mode!r}: 'standard' or 'renewal'."
            )
        if mode == "renewal":
            # The refusal comes first, before any pipeline work:
            # a renewal with nothing to renew against assembles
            # nothing and stores nothing (see renewals.py). The
            # pipeline re-finds the same baseline when it
            # attaches the section — the store cannot move
            # between the two reads inside one assembly.
            renewals_mod.require_baseline(self.store, account_id, as_of)
        # Brief cache: keyed by account + facts-version + as-of, so a
        # hit is only possible when no episode has been ingested for
        # this account since the brief was assembled. Any ingestion —
        # including a gate decision's memo — bumps the version and
        # orphans the entry. The key has no mode dimension, and a
        # renewal brief is a different payload for the same key —
        # so renewal assemblies bypass the payload cache on both
        # sides: a renewal is never served the standard brief, and
        # never stored where a standard assembly would find it.
        # (The port-level retrieval cache underneath is keyed on
        # facts and unaffected — it serves both modes alike.)
        version = self._facts_version(account_id)
        if mode == "standard" and self.cache is not None and version is not None:
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
                        "mode": mode,
                    },
                    config={"configurable": {"thread_id": brief_id}},
                )
                brief = self.store.get(brief_id)
                if brief is None:  # pragma: no cover - verify always saves
                    raise RuntimeError(f"Pipeline produced no brief {brief_id}.")
            else:
                brief = run_pipeline(
                    self.runtime, account, as_of, brief_id, mode
                )
        if mode == "standard" and self.cache is not None:
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
            decided = self.get_brief(brief_id)
        else:
            decided = decisions.apply_decision(
                self.port, brief, action=action, actor=actor, reason=reason
            )
            self.store.save(decided)
        # A fresh decision on current facts is the one resolution
        # a staleness flag accepts: stamp the account's open
        # records resolved by this brief.
        staleness_mod.resolve_open_records(
            self.staleness_store,
            account_id=decided.account_id,
            resolved_by_brief=decided.brief_id,
            actor=actor,
        )
        # A request_info decision opens its follow-through ask,
        # composed from the decided brief. A brief with nothing
        # to ask opens no record: the draft is refused and the
        # refusal is named on the brief's audit trail (the
        # decision itself stands — the gate is the human's).
        # (An open ask is NOT closed by this or any decision —
        # the two watches are independent; the ask closes when
        # the broker's answers are recognised.)
        if action == "request_info":
            try:
                items = follow_mod.compose_items(decided)
            except FollowThroughError as exc:
                decided.audit.append(
                    AuditEntry(
                        at=datetime.now(timezone.utc),
                        event="follow_through_refused",
                        detail=str(exc),
                    )
                )
                self.store.save(decided)
            else:
                follow_mod.open_follow_through(
                    self.follow_store, decided, items
                )
        return decided

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
        from .citations import describe_index

        return {
            "context_backend": getattr(self.port, "name", "unknown"),
            "citation_index": describe_index(self.port),
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
