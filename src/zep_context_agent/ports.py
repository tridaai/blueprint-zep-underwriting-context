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

from datetime import date
from typing import Protocol

from .ontology import Edge, Episode, Fact, SourceDocument


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
