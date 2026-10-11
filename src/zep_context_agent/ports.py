"""The ContextPort: the one seam between this blueprint and Zep.

Everything the workflow needs from a context layer goes through
this protocol, so the deployment choice — Zep Cloud, a self-hosted
Zep stack, or the offline local graph — is configuration, not code:

- ``ingest_episode`` writes one source document into the graph.
- ``get_facts`` / ``get_episodes`` / ``get_edges`` read one
  account's structured, cited layer back. Assembly needs
  completeness (every current fact), which ranked search cannot
  promise, so facts are read whole per account.
- ``search_facts`` is ranked retrieval over the same facts (what a
  human or a follow-up agent would query interactively).
- ``get_candidates`` / ``confirm_candidate`` / ``reject_candidate``
  are the review gate: assertions from correspondence-tier
  documents land as candidates at ingestion — stored, never
  served by the fact reads above — and enter the graph only when
  an underwriter confirms them. Rejection keeps the record, with
  the reason. See :mod:`review`.
- ``get_context_block`` is the provider's own synthesized context
  for the account (Zep's thread user-context on the Zep side; a
  locally composed equivalent offline). It is shown in the brief
  labelled as provider context — it is never a citation source.

Account scoping is part of the contract: every call names exactly
one account, adapters store facts per account, and a cross-account
read is impossible by construction, then double-checked by the
leakage guardrail at verification time.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Protocol

from .ontology import CandidateFact, Edge, Episode, Fact, SourceDocument


class ContextPort(Protocol):
    name: str

    def ingest_episode(self, doc: SourceDocument, episode: Episode) -> Episode:
        """Store one screened document as an episode with its facts."""
        ...

    def get_facts(self, account_id: str) -> list[Fact]:
        ...

    def get_episodes(self, account_id: str) -> list[Episode]:
        ...

    def get_edges(self, account_id: str) -> list[Edge]:
        ...

    def search_facts(self, account_id: str, query: str, limit: int = 10) -> list[Fact]:
        ...

    def get_context_block(self, account_id: str, as_of: date) -> str | None:
        ...

    # -- the review gate ------------------------------------------------

    def get_candidates(
        self, account_id: str, state: str | None = None
    ) -> list[CandidateFact]:
        """The account's candidate facts (all states, or one)."""
        ...

    def confirm_candidate(
        self, account_id: str, candidate_id: str, *, actor: str, at: datetime
    ) -> Fact:
        """Admit a pending candidate into the graph as a fact:
        ``recorded_at`` becomes the confirmation date (the date of
        ``at``), the confirming actor is stamped on it. Returns
        the fact."""
        ...

    def reject_candidate(
        self,
        account_id: str,
        candidate_id: str,
        *,
        actor: str,
        reason: str,
        at: datetime,
    ) -> CandidateFact:
        """Refuse a pending candidate, with the reason on the
        record. The candidate stays — rejected, auditable, and
        never a fact. Returns the updated candidate."""
        ...
