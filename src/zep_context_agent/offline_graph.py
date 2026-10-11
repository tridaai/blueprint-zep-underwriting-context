"""The offline ContextPort adapter: a local temporal context graph.

Labelled fallback, not a mock of Zep's intelligence: it stores the
same episodes, facts, and edges, resolves the same temporal
semantics, and enforces the same account scoping — in memory, with
lexical search. What it does *not* do is Zep's learned extraction
and semantic ranking; the README says so wherever the adapter is
named. Tests, evals, and the demo run here, deterministically.

When a citation index is wired in (production wiring attaches the
Postgres one), every ingested episode is written through to it and
each account's state is hydrated back from it on first touch — so
the cited layer survives a process restart. Without one, the
graph is exactly the v1 in-process store.
"""

from __future__ import annotations

import re
from datetime import date
from typing import TYPE_CHECKING

from .errors import AccountScopeViolation
from .ontology import Edge, Episode, Fact, SourceDocument
from .temporal import resolve

if TYPE_CHECKING:
    from .citations import InMemoryCitationIndex, PostgresCitationIndex

    CitationIndex = InMemoryCitationIndex | PostgresCitationIndex


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


class OfflineContextGraph:
    name = "offline"

    def __init__(self, citation_index: "CitationIndex | None" = None) -> None:
        self._episodes: dict[str, dict[str, Episode]] = {}
        self._facts: dict[str, list[Fact]] = {}
        self._edges: dict[str, list[Edge]] = {}
        self.citation_index = citation_index
        self._hydrated: set[str] = set()

    # -- citation-index hydration -------------------------------------------
    def _hydrate(self, account_id: str) -> None:
        """Load the account's persisted cited layer, once per
        process. Merges under whatever is already in memory (a
        record this process ingested before hydrating stands)."""
        if self.citation_index is None or account_id in self._hydrated:
            return
        self._hydrated.add(account_id)
        episodes, facts, edges = self.citation_index.load_account(account_id)
        local_episodes = self._episodes.setdefault(account_id, {})
        for episode in episodes:
            local_episodes.setdefault(episode.episode_id, episode)
        local_facts = self._facts.setdefault(account_id, [])
        known_facts = {f.fact_id for f in local_facts}
        local_facts.extend(f for f in facts if f.fact_id not in known_facts)
        local_edges = self._edges.setdefault(account_id, [])
        known_edges = {
            (e.source_ref, e.edge_type, e.target_ref) for e in local_edges
        }
        for edge in edges:
            key = (edge.source_ref, edge.edge_type, edge.target_ref)
            if key not in known_edges:
                local_edges.append(edge)
                known_edges.add(key)

    # -- writes ------------------------------------------------------------
    def ingest_episode(self, doc: SourceDocument, episode: Episode) -> Episode:
        if episode.account_id != doc.account_id:
            raise AccountScopeViolation(
                f"episode {episode.episode_id} is scoped to "
                f"{episode.account_id}, document to {doc.account_id}"
            )
        account_id = doc.account_id
        self._hydrate(account_id)
        episodes = self._episodes.setdefault(account_id, {})
        if episode.episode_id in episodes:
            return episodes[episode.episode_id]  # idempotent re-ingest

        episodes[episode.episode_id] = episode
        facts = self._facts.setdefault(account_id, [])
        new_facts: list[Fact] = []
        if not episode.quarantined:
            for idx, assertion in enumerate(doc.assertions):
                fact = Fact(
                    fact_id=f"{episode.episode_id}:f{idx}",
                    account_id=account_id,
                    predicate=assertion.predicate,
                    value=assertion.value,
                    unit=assertion.unit,
                    entity_ref=assertion.entity_ref,
                    valid_from=assertion.valid_from,
                    valid_to=assertion.valid_to,
                    recorded_at=doc.received_at,
                    episode_id=episode.episode_id,
                    doc_id=doc.doc_id,
                )
                facts.append(fact)
                new_facts.append(fact)
        edges = self._edges.setdefault(account_id, [])
        new_edges: list[Edge] = []
        seen = {(e.source_ref, e.edge_type, e.target_ref) for e in edges}
        for assertion in doc.edges:
            key = (assertion.source_ref, assertion.edge_type, assertion.target_ref)
            if key not in seen:
                edge = Edge(
                    account_id=account_id,
                    source_ref=assertion.source_ref,
                    edge_type=assertion.edge_type,
                    target_ref=assertion.target_ref,
                    label=assertion.label,
                    doc_id=doc.doc_id,
                )
                edges.append(edge)
                new_edges.append(edge)
                seen.add(key)
        if self.citation_index is not None:
            self.citation_index.record_episode(
                account_id, episode, new_facts, new_edges
            )
        return episode

    # -- reads --------------------------------------------------------------
    def get_facts(self, account_id: str) -> list[Fact]:
        self._hydrate(account_id)
        return list(self._facts.get(account_id, []))

    def get_episodes(self, account_id: str) -> list[Episode]:
        self._hydrate(account_id)
        return list(self._episodes.get(account_id, {}).values())

    def get_edges(self, account_id: str) -> list[Edge]:
        self._hydrate(account_id)
        return list(self._edges.get(account_id, []))

    def search_facts(self, account_id: str, query: str, limit: int = 10) -> list[Fact]:
        self._hydrate(account_id)
        query_tokens = _tokens(query)
        if not query_tokens:
            return []
        scored: list[tuple[float, Fact]] = []
        for fact in self._facts.get(account_id, []):
            haystack = _tokens(
                f"{fact.predicate} {fact.value} {fact.entity_ref}"
            )
            overlap = len(query_tokens & haystack)
            if overlap:
                scored.append((overlap / len(query_tokens), fact))
        scored.sort(key=lambda pair: (-pair[0], pair[1].predicate, pair[1].fact_id))
        return [fact for _, fact in scored[:limit]]

    def get_context_block(self, account_id: str, as_of: date) -> str | None:
        current, _ = resolve(self.get_facts(account_id), as_of)
        if not current:
            return None
        lines = ["Current facts (local graph, resolved as of "
                 f"{as_of.isoformat()}):"]
        for fact in current:
            unit = f" {fact.unit}" if fact.unit else ""
            lines.append(f"- {fact.predicate}: {fact.value}{unit}")
        return "\n".join(lines)
