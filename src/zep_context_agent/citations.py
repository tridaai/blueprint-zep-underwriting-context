"""The citation index: the durable record behind every citation.

A brief cites episode payloads — the structured assertions a source
document carried — not paraphrases. In v1 that index lived inside
the adapters' process memory (the ``zep`` adapter kept it in an
embedded local graph; the ``offline`` adapter *is* a local graph),
so a restart dropped the cited layer and re-derived it by
re-ingesting the account's documents. That worked — ingestion is
idempotent — but it made the audit trail's foundation a cache.

This module is the index as a store, with the repo's usual split:

- :class:`PostgresCitationIndex` — the production implementation,
  one row per episode in the ``citation_index`` table (migration
  0003), selected when ``DATABASE_URL`` is set;
- :class:`InMemoryCitationIndex` — the test double, constructed
  explicitly by tests. It is never substituted silently: with no
  database configured, :func:`default_citation_index` returns
  ``None`` and the adapters keep their v1 in-process behaviour,
  stated as such in readiness.

Both ContextPort profiles write through the same index: the
offline adapter directly, the zep adapter for the episode-payload
index its cited reads come from. Writes happen at ingestion time,
one record per episode — the episode plus the facts and edges that
episode contributed. Reads hydrate an adapter's in-memory state
lazily, per account, the first time the account is touched; after
hydration the adapter serves from memory exactly as before, with
the database as its backing record.

The index also carries the review gate's candidate records
(migration 0006): a correspondence episode's assertions wait here
— beside the cited layer, never in it — so the queue survives a
restart like everything else the desk relies on. A confirmation
appends the admitted fact to its episode's record here
(:meth:`append_facts`) and updates the candidate's state; a
rejection updates the state and nothing else.
"""

from __future__ import annotations

import json
import threading

from .ontology import CandidateFact, Edge, Episode, Fact


class InMemoryCitationIndex:
    """Dict-backed citation index (the test double)."""

    name = "in-memory test double"

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], tuple[Episode, list[Fact], list[Edge]]] = {}
        self._order: list[tuple[str, str]] = []
        self._candidates: dict[tuple[str, str], CandidateFact] = {}
        self._candidate_order: list[tuple[str, str]] = []

    def record_episode(
        self,
        account_id: str,
        episode: Episode,
        facts: list[Fact],
        edges: list[Edge],
    ) -> None:
        key = (account_id, episode.episode_id)
        if key in self._records:
            return  # ingestion is idempotent; the first record stands
        self._records[key] = (episode, list(facts), list(edges))
        self._order.append(key)

    def append_facts(
        self, account_id: str, episode_id: str, facts: list[Fact]
    ) -> None:
        """Add facts to an episode's standing record — the write a
        confirmation makes when a candidate is admitted after its
        episode was recorded with no facts."""
        key = (account_id, episode_id)
        if key not in self._records:
            return
        episode, ep_facts, ep_edges = self._records[key]
        known = {f.fact_id for f in ep_facts}
        ep_facts.extend(f for f in facts if f.fact_id not in known)

    def load_account(
        self, account_id: str
    ) -> tuple[list[Episode], list[Fact], list[Edge]]:
        episodes: list[Episode] = []
        facts: list[Fact] = []
        edges: list[Edge] = []
        for key in self._order:
            if key[0] != account_id:
                continue
            episode, ep_facts, ep_edges = self._records[key]
            episodes.append(episode)
            facts.extend(ep_facts)
            edges.extend(ep_edges)
        return episodes, facts, edges

    # -- the review gate's records ------------------------------------

    def record_candidates(
        self, account_id: str, candidates: list[CandidateFact]
    ) -> None:
        for candidate in candidates:
            key = (account_id, candidate.candidate_id)
            if key in self._candidates:
                continue  # idempotent; the first record stands
            self._candidates[key] = candidate
            self._candidate_order.append(key)

    def update_candidate(self, candidate: CandidateFact) -> None:
        key = (candidate.account_id, candidate.candidate_id)
        if key not in self._candidates:
            self._candidate_order.append(key)
        self._candidates[key] = candidate

    def load_candidates(self, account_id: str) -> list[CandidateFact]:
        return [
            self._candidates[key]
            for key in self._candidate_order
            if key[0] == account_id
        ]


class PostgresCitationIndex:
    """Citation index over the ``citation_index`` table."""

    name = "postgres"

    def __init__(self, url: str) -> None:
        from .db import ensure_migrated

        ensure_migrated(url)
        self._url = url
        self._lock = threading.Lock()

    def _connect(self):
        from .db import connect

        return connect(self._url)

    def record_episode(
        self,
        account_id: str,
        episode: Episode,
        facts: list[Fact],
        edges: list[Edge],
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO citation_index "
                "(account_id, episode_id, doc_id, episode, facts, edges) "
                "VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb) "
                "ON CONFLICT (account_id, episode_id) DO NOTHING",
                (
                    account_id,
                    episode.episode_id,
                    episode.doc_id,
                    json.dumps(episode.model_dump(mode="json")),
                    json.dumps([f.model_dump(mode="json") for f in facts]),
                    json.dumps([e.model_dump(mode="json") for e in edges]),
                ),
            )
            conn.commit()

    def append_facts(
        self, account_id: str, episode_id: str, facts: list[Fact]
    ) -> None:
        """Add facts to an episode's standing record (a review-gate
        confirmation admitting a candidate after the fact)."""
        if not facts:
            return
        payload = json.dumps([f.model_dump(mode="json") for f in facts])
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE citation_index SET facts = facts || %s::jsonb "
                "WHERE account_id = %s AND episode_id = %s",
                (payload, account_id, episode_id),
            )
            conn.commit()

    def load_account(
        self, account_id: str
    ) -> tuple[list[Episode], list[Fact], list[Edge]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT episode, facts, edges FROM citation_index "
                "WHERE account_id = %s ORDER BY seq",
                (account_id,),
            ).fetchall()
        episodes: list[Episode] = []
        facts: list[Fact] = []
        edges: list[Edge] = []
        for raw_episode, raw_facts, raw_edges in rows:
            if isinstance(raw_episode, str):
                raw_episode = json.loads(raw_episode)
            if isinstance(raw_facts, str):
                raw_facts = json.loads(raw_facts)
            if isinstance(raw_edges, str):
                raw_edges = json.loads(raw_edges)
            episodes.append(Episode.model_validate(raw_episode))
            facts.extend(Fact.model_validate(f) for f in raw_facts)
            edges.extend(Edge.model_validate(e) for e in raw_edges)
        return episodes, facts, edges

    # -- the review gate's records (migration 0006) --------------------

    def record_candidates(
        self, account_id: str, candidates: list[CandidateFact]
    ) -> None:
        if not candidates:
            return
        with self._lock, self._connect() as conn:
            for candidate in candidates:
                conn.execute(
                    "INSERT INTO candidate_facts "
                    "(candidate_id, account_id, episode_id, state, payload) "
                    "VALUES (%s, %s, %s, %s, %s::jsonb) "
                    "ON CONFLICT (candidate_id) DO NOTHING",
                    (
                        candidate.candidate_id,
                        account_id,
                        candidate.episode_id,
                        candidate.state,
                        json.dumps(candidate.model_dump(mode="json")),
                    ),
                )
            conn.commit()

    def update_candidate(self, candidate: CandidateFact) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO candidate_facts "
                "(candidate_id, account_id, episode_id, state, payload) "
                "VALUES (%s, %s, %s, %s, %s::jsonb) "
                "ON CONFLICT (candidate_id) DO UPDATE SET "
                "state = EXCLUDED.state, payload = EXCLUDED.payload, "
                "updated_at = now()",
                (
                    candidate.candidate_id,
                    candidate.account_id,
                    candidate.episode_id,
                    candidate.state,
                    json.dumps(candidate.model_dump(mode="json")),
                ),
            )
            conn.commit()

    def load_candidates(self, account_id: str) -> list[CandidateFact]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload FROM candidate_facts "
                "WHERE account_id = %s ORDER BY seq",
                (account_id,),
            ).fetchall()
        candidates = []
        for (payload,) in rows:
            if isinstance(payload, str):
                payload = json.loads(payload)
            candidates.append(CandidateFact.model_validate(payload))
        return candidates


def default_citation_index():
    """The configured citation index: Postgres when ``DATABASE_URL``
    is set, otherwise ``None`` — the adapters then keep their
    in-process index, and readiness says so."""
    from .db import database_url

    url = database_url()
    if url:
        return PostgresCitationIndex(url)
    return None


def describe_index(port) -> str:
    """Name the citation index a port is backed by, for readiness."""
    delegate = getattr(port, "_delegate", port)  # unwrap the cache decorator
    index = getattr(delegate, "citation_index", None)
    if index is None:
        return "in-process only"
    return getattr(index, "name", "unknown")
