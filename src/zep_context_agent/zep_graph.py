"""The Zep Cloud ContextPort adapter (Zep v4, "Context API").

Talks to the current Zep platform through the official
``zep-cloud`` Python SDK (pinned in pyproject — transport only,
no SDK code is vendored in). Zep v4 addresses graphs, users, and
threads by server-assigned UUIDs and keeps per-graph ontologies;
the v2 REST surface this adapter used before (client-named
``group_id`` graphs, a hand-rolled episode POST) is retired.

How the mapping works, honestly:

- **One Context Graph per account.** An account's graph is
  provisioned lazily on first touch: ``graph.create`` under the
  name ``uw-account-<account_id>`` (with a deterministic
  idempotency key, so a retried create returns the same graph),
  then ``graph.set_ontology`` with the blueprint's ontology —
  before the graph's first episode, because the v4 ontology is
  not retroactive. The account → graph UUID mapping and the
  ontology-applied flag persist in the blueprint's own graph
  registry (see :mod:`zep_graphs`, migration 0008); the flag
  flips only after the set succeeds, so a crash mid-provisioning
  re-runs the set instead of assuming it landed.
- **Writes go to Zep for real.** One document at a time lands as
  a JSON episode (``graph.episode.add``) carrying the document's
  ``received_at`` as its ``reference_time`` (the event time that
  drives Zep's temporal invalidation), ``document_id`` set to the
  document id (Zep groups one document's episodes and builds a
  per-document summary), and metadata — account, document, kind —
  projected onto the derived artifacts and searchable via
  metadata filters. Bulk loads through the ingestion entry point
  (more than one document in a run) go through the **Batch API**
  instead: ``batch.create`` → ``add_items`` → ``process``,
  polled on ``batch.get`` until terminal.
- **Cited facts** are read back from the episode-payload citation
  index this adapter keeps for what it ingested. An audit-grade
  brief cannot cite a paraphrase: the episode payload *is* the
  citation record, and Zep builds its semantic graph from those
  same payloads server-side. The index is an embedded local
  graph; when a citation index store is wired in (production
  wiring attaches the Postgres one), it persists there and a
  restart re-reads the cited layer from the database — see
  :mod:`citations`.
- **Provider search enriches, never cites.** ``search_facts``
  runs Zep's own per-scope search over the account graph
  (``graph.search_edges`` with the cross-encoder reranker and an
  account metadata filter) and keeps the ranked hits as provider
  material: they surface inside the brief's provider-context
  block, labelled as provider context. The cited facts the call
  returns still come from the index, so a hit can always be
  traced to its episode.
- **The context block** is Zep's own, from ``graph.get_context``
  over the account graph. (In v2 this adapter fetched a thread's
  user context; v4 thread context blocks are built over *user*
  graphs, and an account's graph here is a shared graph — the
  graph-scoped context block is the correct v4 route, so the
  thread construction went away.) If Zep is unreachable the call
  raises — the brief then assembles from cited facts alone and
  says the provider context was unavailable. It never silently
  substitutes the offline graph.
- **The review gate** governs the cited layer on this profile
  exactly as it does offline: the episode payload goes to Zep as
  ingested (Zep's server-side graph is Zep's), but a
  correspondence document's assertions are held as candidates in
  the index and cited by no brief until an underwriter confirms
  them — the same posture quarantine already takes on this
  adapter. The gate's methods delegate to the index.

Configuration: ``ZEP_API_KEY`` (required), ``ZEP_API_BASE_URL``
(the SDK's base-URL override; unset = the SDK's hosted default,
``https://api.getzep.com/api/v4``), ``ZEP_TIMEOUT_SECONDS``,
``ZEP_BATCH_POLL_SECONDS`` / ``ZEP_BATCH_MAX_POLLS``.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import date, datetime, time as day_time, timezone

from .config import env_float, env_int, env_str
from .errors import ContextError, ContextNotConfigured, ContextUnavailable
from .offline_graph import OfflineContextGraph
from .ontology import (
    EDGE_TYPES,
    ENTITY_TYPES,
    CandidateFact,
    Edge,
    Episode,
    Fact,
    SourceDocument,
)
from .zep_graphs import default_graph_registry

SOURCE_NAME = "trida-uw-context-blueprint"

# Batch statuses after which polling stops. "partial" lands some
# items and refuses others — handled per item, below.
_BATCH_TERMINAL = {"succeeded", "partial", "failed", "canceled", "invalid"}


def _graph_name(account_id: str) -> str:
    return f"uw-account-{account_id}"


def _reference_time(received: date) -> str:
    """A document's received date as a v4 reference_time: the
    event time (UTC start of that day) driving temporal
    invalidation on Zep's side."""
    return datetime.combine(
        received, day_time.min, tzinfo=timezone.utc
    ).isoformat()


def _episode_payload(doc: SourceDocument) -> dict:
    """The episode body, exactly as the v2 adapter sent it: the
    citation record a brief's facts trace back to."""
    return {
        "doc_id": doc.doc_id,
        "kind": doc.kind,
        "title": doc.title,
        "account_id": doc.account_id,
        "assertions": [a.model_dump(mode="json") for a in doc.assertions],
        "edges": [e.model_dump(mode="json") for e in doc.edges],
    }


def _episode_metadata(doc: SourceDocument) -> dict:
    """Episode metadata (≤10 keys): projected onto the artifacts
    Zep derives and searchable via metadata filters."""
    return {"account_id": doc.account_id, "doc_id": doc.doc_id, "kind": doc.kind}


def _source_description(doc: SourceDocument) -> str:
    return f"{doc.kind} document for account {doc.account_id}"


def _parse_edge_spec(spec: str) -> tuple[list[tuple[str, str]], str]:
    """``EDGE_TYPES`` values carry their source/target shape in
    prose ("submission|correspondence -> insured_account: about
    this account."). Split it into the SDK's source/target pairs
    plus the remaining description."""
    head, _, tail = spec.partition(":")
    head = head.strip()
    if "->" not in head:
        return [], spec
    sources, _, target = head.partition("->")
    pairs = [
        (source.strip(), target.strip()) for source in sources.split("|")
    ]
    return pairs, (tail.strip() or spec)


def _sdk_entity_types() -> list:
    from zep_cloud import EntityType

    return [
        EntityType(name=name, description=description)
        for name, description in ENTITY_TYPES.items()
    ]


def _sdk_edge_types() -> list:
    from zep_cloud import EdgeSourceTarget, EdgeType

    edge_types = []
    for name, spec in EDGE_TYPES.items():
        pairs, description = _parse_edge_spec(spec)
        edge_types.append(
            EdgeType(
                name=name,
                description=description,
                source_targets=[
                    EdgeSourceTarget(source=source, target=target)
                    for source, target in pairs
                ]
                or None,
            )
        )
    return edge_types


def _default_client(api_key: str, base_url: str | None, timeout: float):
    """Build the real SDK client. ``base_url`` is the SDK's own
    override; unset means the hosted v4 default."""
    from zep_cloud import Zep

    kwargs: dict = {"api_key": api_key, "timeout": timeout}
    if base_url:
        kwargs["base_url"] = base_url
    return Zep(**kwargs)


class ZepContextGraph:
    name = "zep"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        *,
        client=None,
        client_factory=None,
        citation_index=None,
        graph_registry=None,
        batch_poll_seconds: float | None = None,
        batch_max_polls: int | None = None,
    ) -> None:
        self._api_key = api_key if api_key is not None else env_str("ZEP_API_KEY")
        if not self._api_key:
            raise ContextNotConfigured(
                "CONTEXT_BACKEND=zep but ZEP_API_KEY is not set. Add the "
                "key from the Zep dashboard (see .env.example), or run "
                "CONTEXT_BACKEND=offline for the labelled local graph."
            )
        self._base_url = base_url or env_str("ZEP_API_BASE_URL")
        timeout = env_float("ZEP_TIMEOUT_SECONDS", 30.0)
        if client is not None:
            self._client = client
        else:
            factory = client_factory or _default_client
            try:
                self._client = factory(self._api_key, self._base_url, timeout)
            except Exception as exc:
                raise ContextUnavailable(
                    f"Could not build the Zep v4 client: {exc}"
                ) from exc
        # The episode-payload index for cited facts (see module docstring).
        self._index = OfflineContextGraph(citation_index=citation_index)
        # The account -> graph UUID registry (zep_graphs.py).
        self._registry = (
            graph_registry
            if graph_registry is not None
            else default_graph_registry()
        )
        # Provider-ranked search hits per account: enrichment for
        # the provider-context surface, never a citation source.
        self._provider_hits: dict[str, list[dict]] = {}
        self._batch_poll_seconds = (
            batch_poll_seconds
            if batch_poll_seconds is not None
            else env_float("ZEP_BATCH_POLL_SECONDS", 2.0)
        )
        self._batch_max_polls = (
            batch_max_polls
            if batch_max_polls is not None
            else env_int("ZEP_BATCH_MAX_POLLS", 300)
        )

    @property
    def citation_index(self):
        """The citation index backing the cited layer, if any."""
        return self._index.citation_index

    @property
    def graph_registry(self):
        """The account -> graph registry backing provisioning."""
        return self._registry

    def provider_search_hits(self, account_id: str) -> list[dict]:
        """The provider-ranked hits from the account's last Zep
        search (empty until one runs). Provider material only —
        nothing here is citable."""
        return list(self._provider_hits.get(account_id, []))

    # -- SDK plumbing ---------------------------------------------------------

    def _call(self, fn, *args, **kwargs):
        """One SDK call, with failures named the way the port's
        callers already expect: configured but unserved is
        ContextUnavailable — never a quiet offline answer."""
        try:
            return fn(*args, **kwargs)
        except ContextError:
            raise
        except Exception as exc:
            raise ContextUnavailable(f"Zep v4 call failed: {exc}") from exc

    # -- provisioning ---------------------------------------------------------

    def _graph_uuid(self, account_id: str) -> str:
        """The account's Zep graph UUID, provisioning on first
        touch: create the graph, record it, set the ontology,
        record that — in that order, because the v4 ontology is
        not retroactive and must land before the first episode."""
        record = self._registry.get(account_id)
        if record is not None and record.ontology_applied:
            return record.graph_uuid
        if record is None:
            graph = self._call(
                self._client.graph.create,
                name=_graph_name(account_id),
                description=(
                    "Underwriting context graph for account "
                    f"{account_id} (Trida UW context blueprint)."
                ),
                idempotency_key=_graph_name(account_id),
            )
            graph_uuid = getattr(graph, "uuid_", None) or getattr(
                graph, "graph_id", None
            )
            if not graph_uuid:
                raise ContextUnavailable(
                    "Zep graph.create answered without a graph UUID; "
                    f"cannot provision account {account_id}."
                )
            record = self._registry.record(account_id, graph_uuid)
        if not record.ontology_applied:
            self._call(
                self._client.graph.set_ontology,
                record.graph_uuid,
                entity_types=_sdk_entity_types(),
                edge_types=_sdk_edge_types(),
            )
            self._registry.mark_ontology_applied(account_id)
        return record.graph_uuid

    # -- writes ---------------------------------------------------------------

    def ingest_episode(self, doc: SourceDocument, episode: Episode) -> Episode:
        graph_uuid = self._graph_uuid(doc.account_id)
        self._call(
            self._client.graph.episode.add,
            graph_uuid,
            type="json",
            data=json.dumps(_episode_payload(doc)),
            source_description=_source_description(doc),
            reference_time=_reference_time(doc.received_at),
            metadata=_episode_metadata(doc),
            document_id=doc.doc_id,
        )
        return self._index.ingest_episode(doc, episode)

    def ingest_documents_batch(
        self, entries: list[tuple[SourceDocument, Episode]]
    ) -> list[Episode]:
        """Bulk ingestion through the Zep Batch API — the path
        :func:`ingestion.ingest_documents` takes when more than
        one document arrives in a run. One batch per account:
        create, add every document as a graph-episode item,
        process, poll to terminal. Items are matched back to
        documents by sequence index. A fully succeeded batch
        records every episode in the cited layer; a partial one
        records what landed and raises, naming the documents that
        did not — the cited layer never claims an episode Zep
        refused."""
        by_account: dict[str, list[tuple[SourceDocument, Episode]]] = {}
        for doc, episode in entries:
            by_account.setdefault(doc.account_id, []).append((doc, episode))
        ingested: list[Episode] = []
        for account_id, items in by_account.items():
            ingested.extend(self._ingest_batch_for_account(account_id, items))
        return ingested

    def _ingest_batch_for_account(
        self, account_id: str, items: list[tuple[SourceDocument, Episode]]
    ) -> list[Episode]:
        from zep_cloud import BatchItemInput

        graph_uuid = self._graph_uuid(account_id)
        doc_ids = sorted(doc.doc_id for doc, _ in items)
        digest = hashlib.sha256(",".join(doc_ids).encode("utf-8")).hexdigest()
        batch = self._call(
            self._client.batch.create,
            metadata={"account_id": account_id, "source": SOURCE_NAME},
            idempotency_key=f"uw-batch-{account_id}-{digest[:32]}",
        )
        batch_uuid = getattr(batch, "uuid_", None)
        if not batch_uuid:
            raise ContextUnavailable(
                "Zep batch.create answered without a batch UUID; "
                f"backfill for account {account_id} cannot proceed."
            )
        response = self._call(
            self._client.batch.add_items,
            batch_uuid,
            items=[
                BatchItemInput(
                    type="graph_episode",
                    graph_uuid=graph_uuid,
                    data=json.dumps(_episode_payload(doc)),
                    data_type="json",
                    document_id=doc.doc_id,
                    reference_time=_reference_time(doc.received_at),
                    source_description=_source_description(doc),
                    metadata=_episode_metadata(doc),
                )
                for doc, _ in items
            ],
        )
        rejected = list(getattr(response, "rejected_items", None) or [])
        if rejected:
            reasons = "; ".join(
                f"item {r.index}: {r.reason}" for r in rejected[:3]
            )
            raise ContextUnavailable(
                f"Zep batch refused {len(rejected)} of {len(items)} "
                f"item(s) for account {account_id} at submission: {reasons}"
            )
        self._call(self._client.batch.process, batch_uuid)
        batch = self._poll_batch(batch_uuid, account_id)
        status = getattr(batch, "status", None)
        if status == "succeeded":
            return [self._index.ingest_episode(doc, ep) for doc, ep in items]
        if status == "partial":
            failed = self._failed_batch_items(batch_uuid, items)
            landed = [
                (doc, ep) for doc, ep in items if doc.doc_id not in failed
            ]
            for doc, ep in landed:
                self._index.ingest_episode(doc, ep)
            raise ContextUnavailable(
                f"Zep batch for account {account_id} finished partial: "
                f"{len(failed)} document(s) did not land "
                f"({', '.join(sorted(failed))}); the rest are ingested. "
                "Re-run the backfill for the named documents."
            )
        raise ContextUnavailable(
            f"Zep batch for account {account_id} finished with status "
            f"{status!r}; no episodes from it were recorded."
        )

    def _poll_batch(self, batch_uuid: str, account_id: str):
        for _ in range(self._batch_max_polls):
            batch = self._call(self._client.batch.get, batch_uuid)
            if getattr(batch, "status", None) in _BATCH_TERMINAL:
                return batch
            time.sleep(self._batch_poll_seconds)
        raise ContextUnavailable(
            f"Zep batch {batch_uuid} for account {account_id} did not "
            f"reach a terminal status after {self._batch_max_polls} polls."
        )

    def _failed_batch_items(
        self, batch_uuid: str, items: list[tuple[SourceDocument, Episode]]
    ) -> set[str]:
        """The doc_ids whose batch items failed, matched by
        sequence index (submission order)."""
        pager = self._call(self._client.batch.list_items, batch_uuid)
        failed: set[str] = set()
        for item in pager:
            if getattr(item, "status", None) in {"failed", "skipped", "canceled"}:
                index = getattr(item, "sequence_index", None)
                if index is not None and 0 <= index < len(items):
                    failed.add(items[index][0].doc_id)
        return failed

    # -- reads (cited layer, from the episode-payload index) ------------------

    def get_facts(self, account_id: str) -> list[Fact]:
        return self._index.get_facts(account_id)

    def get_episodes(self, account_id: str) -> list[Episode]:
        return self._index.get_episodes(account_id)

    def get_edges(self, account_id: str) -> list[Edge]:
        return self._index.get_edges(account_id)

    def search_facts(self, account_id: str, query: str, limit: int = 10) -> list[Fact]:
        # Ranked retrieval runs against Zep's own graph search —
        # per-scope (this account's graph), cross-encoder reranked,
        # and fenced to the account by a metadata filter. The hits
        # enrich the provider-context surface; the cited facts
        # themselves come from the index so a hit can always be
        # traced to its episode.
        from zep_cloud import EdgeSearchFilters, MetadataFilter, MetadataFilterGroup

        graph_uuid = self._graph_uuid(account_id)
        pager = self._call(
            self._client.graph.search_edges,
            graph_uuid,
            query=query,
            reranker="cross_encoder",
            page_size=limit,
            filters=EdgeSearchFilters(
                metadata_filters=MetadataFilterGroup(
                    type="and",
                    filters=[
                        MetadataFilter(
                            property_name="account_id",
                            operator="eq",
                            value=account_id,
                        )
                    ],
                )
            ),
        )
        hits: list[dict] = []
        for edge in pager:
            hits.append(
                {
                    "fact": getattr(edge, "fact", None),
                    "name": getattr(edge, "name", None),
                    "score": getattr(edge, "score", None),
                }
            )
            if len(hits) >= limit:
                break
        self._provider_hits[account_id] = hits
        return self._index.search_facts(account_id, query, limit)

    # -- the review gate (the cited layer's, via the index) ---------------

    def get_candidates(
        self, account_id: str, state: str | None = None
    ) -> list[CandidateFact]:
        return self._index.get_candidates(account_id, state)

    def confirm_candidate(
        self, account_id: str, candidate_id: str, *, actor: str, at
    ) -> Fact:
        return self._index.confirm_candidate(
            account_id, candidate_id, actor=actor, at=at
        )

    def reject_candidate(
        self, account_id: str, candidate_id: str, *, actor: str,
        reason: str, at,
    ) -> CandidateFact:
        return self._index.reject_candidate(
            account_id, candidate_id, actor=actor, reason=reason, at=at
        )

    def get_context_block(self, account_id: str, as_of: date) -> str | None:
        # The provider's own context, graph-scoped: v4 context
        # blocks over threads are built on user graphs; this
        # account's graph is a shared graph, so graph.get_context
        # over it is the v4 route (see module docstring).
        graph_uuid = self._graph_uuid(account_id)
        result = self._call(
            self._client.graph.get_context,
            graph_uuid,
            query=(
                f"Underwriting context for account {account_id} "
                f"as of {as_of.isoformat()}"
            ),
        )
        block = getattr(result, "context", None)
        parts: list[str] = []
        if isinstance(block, str) and block:
            parts.append(block)
        hits = self._provider_hits.get(account_id) or []
        if hits:
            lines = [
                "Related facts from Zep graph search "
                "(provider-ranked, not a citation source):"
            ]
            for hit in hits:
                if hit.get("fact"):
                    lines.append(f"- {hit['fact']}")
            if len(lines) > 1:
                parts.append("\n".join(lines))
        return "\n\n".join(parts) if parts else None
