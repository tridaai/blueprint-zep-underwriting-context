"""The offline ContextPort adapter: a local temporal context graph.

Labelled fallback, not a mock of Zep's intelligence: it stores the
same episodes, facts, and edges, resolves the same temporal
semantics, and enforces the same account scoping — in memory, with
lexical search. What it does *not* do is Zep's learned extraction
and semantic ranking; the README says so wherever the adapter is
named. Tests, evals, and the demo run here, deterministically.
"""

from __future__ import annotations

import re
from datetime import date

from .errors import AccountScopeViolation
from .ontology import Edge, Episode, Fact, SourceDocument
from .temporal import resolve


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


class OfflineContextGraph:
    name = "offline"

    def __init__(self) -> None:
        self._episodes: dict[str, dict[str, Episode]] = {}
        self._facts: dict[str, list[Fact]] = {}
        self._edges: dict[str, list[Edge]] = {}

    # -- writes ------------------------------------------------------------
    def ingest_episode(self, doc: SourceDocument, episode: Episode) -> Episode:
        if episode.account_id != doc.account_id:
            raise AccountScopeViolation(
                f"episode {episode.episode_id} is scoped to "
                f"{episode.account_id}, document to {doc.account_id}"
            )
        account_id = doc.account_id
        episodes = self._episodes.setdefault(account_id, {})
        if episode.episode_id in episodes:
            return episodes[episode.episode_id]  # idempotent re-ingest

        episodes[episode.episode_id] = episode
        facts = self._facts.setdefault(account_id, [])
        if not episode.quarantined:
            for idx, assertion in enumerate(doc.assertions):
                facts.append(
                    Fact(
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
                )
        edges = self._edges.setdefault(account_id, [])
        seen = {(e.source_ref, e.edge_type, e.target_ref) for e in edges}
        for assertion in doc.edges:
            key = (assertion.source_ref, assertion.edge_type, assertion.target_ref)
            if key not in seen:
                edges.append(
                    Edge(
                        account_id=account_id,
                        source_ref=assertion.source_ref,
                        edge_type=assertion.edge_type,
                        target_ref=assertion.target_ref,
                        label=assertion.label,
                        doc_id=doc.doc_id,
                    )
                )
                seen.add(key)
        return episode

    # -- reads --------------------------------------------------------------
    def get_facts(self, account_id: str) -> list[Fact]:
        return list(self._facts.get(account_id, []))

    def get_episodes(self, account_id: str) -> list[Episode]:
        return list(self._episodes.get(account_id, {}).values())

    def get_edges(self, account_id: str) -> list[Edge]:
        return list(self._edges.get(account_id, []))

    def search_facts(self, account_id: str, query: str, limit: int = 10) -> list[Fact]:
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
