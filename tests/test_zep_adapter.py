"""The Zep v4 adapter's contract, pinned over a fake SDK client.

Live verification against a real Zep workspace is pending an API
key (see README "Honest status"); these tests pin what the adapter
sends through the official ``zep-cloud`` SDK surface and how it
fails — in particular that it fails honestly and never falls back
to the offline graph. The fake below implements exactly the SDK
surface the adapter uses (graph create/set_ontology/episode.add/
search_edges/get_context, batch create/add_items/process/get/
list_items), with the real SDK's parameter shapes.
"""

from __future__ import annotations

import json
import os
from datetime import date

import pytest

from zep_context_agent.errors import ContextNotConfigured, ContextUnavailable
from zep_context_agent.ingestion import ingest_account, ingest_document
from zep_context_agent.ontology import (
    EDGE_TYPES,
    ENTITY_TYPES,
    Account,
    FactAssertion,
    SourceDocument,
)
from zep_context_agent.zep_graph import ZepContextGraph
from zep_context_agent.zep_graphs import InMemoryGraphRegistry


# ---------------------------------------------------------------------------
# The fake zep_cloud client
# ---------------------------------------------------------------------------


class FakeGraph:
    def __init__(self, uuid, name):
        self.uuid_ = uuid
        self.graph_id = None
        self.name = name


class FakeEdge:
    def __init__(self, fact, name="holds", score=0.9):
        self.fact = fact
        self.name = name
        self.score = score


class FakeContextResponse:
    def __init__(self, context):
        self.context = context


class FakeBatch:
    def __init__(self, uuid, status="draft"):
        self.uuid_ = uuid
        self.status = status


class FakeBatchItem:
    def __init__(self, status, sequence_index):
        self.status = status
        self.sequence_index = sequence_index


class FakeRejectedItem:
    def __init__(self, index, reason):
        self.index = index
        self.reason = reason


class FakeAddItemsResponse:
    def __init__(self, rejected_items=()):
        self.items = []
        self.rejected_items = list(rejected_items)


class FakeEpisodeResource:
    def __init__(self, fake):
        self._fake = fake

    def add(self, graph_uuid, **kwargs):
        self._fake.record("graph.episode.add", graph_uuid=graph_uuid, **kwargs)
        return object()


class FakeGraphResource:
    def __init__(self, fake):
        self._fake = fake
        self.episode = FakeEpisodeResource(fake)

    def create(self, **kwargs):
        self._fake.record("graph.create", **kwargs)
        key = kwargs.get("idempotency_key")
        if key in self._fake.graphs_by_key:
            return self._fake.graphs_by_key[key]
        self._fake.graph_counter += 1
        graph = FakeGraph(f"graph-uuid-{self._fake.graph_counter}", kwargs.get("name"))
        if key:
            self._fake.graphs_by_key[key] = graph
        return graph

    def set_ontology(self, graph_uuid, **kwargs):
        self._fake.record("graph.set_ontology", graph_uuid=graph_uuid, **kwargs)
        return object()

    def get_ontology(self, graph_uuid, **kwargs):
        self._fake.record("graph.get_ontology", graph_uuid=graph_uuid)
        return object()

    def search_edges(self, graph_uuid, **kwargs):
        self._fake.record("graph.search_edges", graph_uuid=graph_uuid, **kwargs)
        return iter(self._fake.search_results)

    def get_context(self, graph_uuid, **kwargs):
        self._fake.record("graph.get_context", graph_uuid=graph_uuid, **kwargs)
        return FakeContextResponse(self._fake.context_block)


class FakeBatchResource:
    def __init__(self, fake):
        self._fake = fake

    def create(self, **kwargs):
        self._fake.record("batch.create", **kwargs)
        return FakeBatch("batch-uuid-1")

    def add_items(self, batch_uuid, **kwargs):
        self._fake.record("batch.add_items", batch_uuid=batch_uuid, **kwargs)
        return FakeAddItemsResponse(self._fake.batch_rejected)

    def process(self, batch_uuid, **kwargs):
        self._fake.record("batch.process", batch_uuid=batch_uuid)
        return object()

    def get(self, batch_uuid, **kwargs):
        self._fake.record("batch.get", batch_uuid=batch_uuid)
        if len(self._fake.batch_statuses) > 1:
            status = self._fake.batch_statuses.pop(0)
        else:
            status = self._fake.batch_statuses[0]
        return FakeBatch(batch_uuid, status=status)

    def list_items(self, batch_uuid, **kwargs):
        self._fake.record("batch.list_items", batch_uuid=batch_uuid)
        return iter(self._fake.batch_items)


class FakeZepClient:
    """A zep_cloud.Zep-shaped double: .graph / .batch resources,
    an event log for ordering assertions, and scripted outcomes."""

    def __init__(self):
        self.events: list[tuple[str, dict]] = []
        self.graph = FakeGraphResource(self)
        self.batch = FakeBatchResource(self)
        self.graphs_by_key: dict[str, FakeGraph] = {}
        self.graph_counter = 0
        self.search_results: list[FakeEdge] = []
        self.context_block = "ZEP CONTEXT BLOCK"
        self.batch_statuses: list[str] = ["succeeded"]
        self.batch_items: list[FakeBatchItem] = []
        self.batch_rejected: list[FakeRejectedItem] = []
        self.fail_on: set[str] = set()

    def record(self, event: str, **payload) -> None:
        if event in self.fail_on:
            raise RuntimeError(f"fake SDK failure on {event}")
        self.events.append((event, payload))

    def calls(self, event: str) -> list[dict]:
        return [payload for name, payload in self.events if name == event]


def make_adapter(fake: FakeZepClient, **kwargs) -> ZepContextGraph:
    kwargs.setdefault("graph_registry", InMemoryGraphRegistry())
    kwargs.setdefault("batch_poll_seconds", 0.0)
    return ZepContextGraph(api_key="test-key", client=fake, **kwargs)


def sample_doc(**overrides) -> SourceDocument:
    fields = dict(
        doc_id="DOC-Z-1", account_id="ACC-Z", kind="submission",
        title="Submission", received_at=date(2025, 1, 10),
        assertions=[
            FactAssertion(
                predicate="legal_name", value="Zed Co",
                valid_from=date(2025, 1, 10),
            )
        ],
    )
    fields.update(overrides)
    return SourceDocument(**fields)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_missing_key_is_not_configured(monkeypatch):
    monkeypatch.delenv("ZEP_API_KEY", raising=False)
    with pytest.raises(ContextNotConfigured):
        ZepContextGraph(api_key=None)


def test_base_url_and_timeout_reach_the_client_factory():
    captured = {}

    def factory(api_key, base_url, timeout):
        captured.update(api_key=api_key, base_url=base_url, timeout=timeout)
        return FakeZepClient()

    ZepContextGraph(
        api_key="k", base_url="https://example.internal/api/v4",
        client_factory=factory,
    )
    assert captured["api_key"] == "k"
    assert captured["base_url"] == "https://example.internal/api/v4"
    assert captured["timeout"] == 30.0


# ---------------------------------------------------------------------------
# Provisioning: create + ontology, before the first episode, once
# ---------------------------------------------------------------------------


def test_ingest_provisions_then_sends_episode_with_v4_fields():
    fake = FakeZepClient()
    adapter = make_adapter(fake)
    ingest_document(adapter, sample_doc())

    order = [name for name, _ in fake.events]
    assert order == ["graph.create", "graph.set_ontology", "graph.episode.add"]

    create = fake.calls("graph.create")[0]
    assert create["name"] == "uw-account-ACC-Z"
    assert create["idempotency_key"] == "uw-account-ACC-Z"

    add = fake.calls("graph.episode.add")[0]
    assert add["graph_uuid"] == "graph-uuid-1"
    assert add["type"] == "json"
    assert add["document_id"] == "DOC-Z-1"
    assert add["reference_time"] == "2025-01-10T00:00:00+00:00"
    assert add["metadata"] == {
        "account_id": "ACC-Z", "doc_id": "DOC-Z-1", "kind": "submission",
    }
    body = json.loads(add["data"])
    assert body["doc_id"] == "DOC-Z-1"
    assert body["assertions"][0]["predicate"] == "legal_name"
    # Cited facts read back from the ingested payloads.
    assert [f.value for f in adapter.get_facts("ACC-Z")] == ["Zed Co"]


def test_ontology_payload_maps_the_blueprint_ontology():
    fake = FakeZepClient()
    adapter = make_adapter(fake)
    ingest_document(adapter, sample_doc())

    ontology = fake.calls("graph.set_ontology")[0]
    assert ontology["graph_uuid"] == "graph-uuid-1"
    entity_names = {et.name for et in ontology["entity_types"]}
    assert entity_names == set(ENTITY_TYPES)
    edge_types = {et.name: et for et in ontology["edge_types"]}
    assert set(edge_types) == set(EDGE_TYPES)
    pairs = {
        (st.source, st.target) for st in edge_types["concerns"].source_targets
    }
    assert pairs == {
        ("submission", "insured_account"),
        ("correspondence", "insured_account"),
    }
    placed = edge_types["placed_by"].source_targets
    assert [(st.source, st.target) for st in placed] == [
        ("insured_account", "broker")
    ]


def test_provisioned_graph_is_not_reprovisioned():
    fake = FakeZepClient()
    adapter = make_adapter(fake)
    ingest_document(adapter, sample_doc())
    ingest_document(adapter, sample_doc(doc_id="DOC-Z-2", title="Second"))
    assert len(fake.calls("graph.create")) == 1
    assert len(fake.calls("graph.set_ontology")) == 1
    assert len(fake.calls("graph.episode.add")) == 2
    record = adapter.graph_registry.get("ACC-Z")
    assert record is not None and record.ontology_applied


def test_graph_mapping_persists_across_adapter_instances():
    registry = InMemoryGraphRegistry()
    fake_one, fake_two = FakeZepClient(), FakeZepClient()
    first = make_adapter(fake_one, graph_registry=registry)
    ingest_document(first, sample_doc())
    uuid = fake_one.calls("graph.episode.add")[0]["graph_uuid"]

    second = make_adapter(fake_two, graph_registry=registry)
    ingest_document(second, sample_doc(doc_id="DOC-Z-9", title="Later"))
    # The second adapter reuses the standing mapping: no create,
    # no ontology re-set, episodes land in the same graph.
    assert fake_two.calls("graph.create") == []
    assert fake_two.calls("graph.set_ontology") == []
    assert fake_two.calls("graph.episode.add")[0]["graph_uuid"] == uuid


# ---------------------------------------------------------------------------
# Search + context block
# ---------------------------------------------------------------------------


def test_search_facts_uses_v4_search_and_returns_index_facts():
    fake = FakeZepClient()
    fake.search_results = [
        FakeEdge(fact="Zed Co holds policy POL-2210", score=0.87)
    ]
    adapter = make_adapter(fake)
    ingest_document(adapter, sample_doc())

    facts = adapter.search_facts("ACC-Z", "Zed", limit=5)
    # Cited facts still come from the episode-payload index.
    assert [f.value for f in facts] == ["Zed Co"]

    search = fake.calls("graph.search_edges")[0]
    assert search["graph_uuid"] == "graph-uuid-1"
    assert search["reranker"] == "cross_encoder"
    group = search["filters"].metadata_filters
    assert group.type == "and"
    assert group.filters[0].property_name == "account_id"
    assert group.filters[0].operator == "eq"
    assert group.filters[0].value == "ACC-Z"

    # The provider hits enrich the provider-context surface.
    hits = adapter.provider_search_hits("ACC-Z")
    assert hits[0]["fact"] == "Zed Co holds policy POL-2210"
    block = adapter.get_context_block("ACC-Z", date(2026, 6, 1))
    assert block is not None
    assert "ZEP CONTEXT BLOCK" in block
    assert "Zed Co holds policy POL-2210" in block


def test_context_block_comes_from_the_account_graph():
    fake = FakeZepClient()
    adapter = make_adapter(fake)
    ingest_document(adapter, sample_doc())
    block = adapter.get_context_block("ACC-Z", date(2026, 6, 1))
    assert block == "ZEP CONTEXT BLOCK"
    call = fake.calls("graph.get_context")[0]
    assert call["graph_uuid"] == "graph-uuid-1"
    assert "ACC-Z" in call["query"]


# ---------------------------------------------------------------------------
# Batch backfill
# ---------------------------------------------------------------------------


def two_doc_account() -> Account:
    return Account(
        account_id="ACC-Z",
        display_name="Zed Co",
        documents=[
            sample_doc(),
            sample_doc(
                doc_id="DOC-Z-2", title="Claims extract", kind="claims_extract",
                received_at=date(2025, 2, 1),
                assertions=[
                    FactAssertion(
                        predicate="claims_count_3yr", value=2,
                        valid_from=date(2025, 2, 1),
                    )
                ],
            ),
        ],
    )


def test_batch_backfill_submits_items_and_polls_to_completion():
    fake = FakeZepClient()
    fake.batch_statuses = ["processing", "succeeded"]
    adapter = make_adapter(fake)

    episodes = ingest_account(adapter, two_doc_account())
    assert [e.doc_id for e in episodes] == ["DOC-Z-1", "DOC-Z-2"]

    create = fake.calls("batch.create")[0]
    assert create["idempotency_key"].startswith("uw-batch-ACC-Z-")
    assert create["metadata"]["account_id"] == "ACC-Z"
    add = fake.calls("batch.add_items")[0]
    items = add["items"]
    assert len(items) == 2
    assert all(item.type == "graph_episode" for item in items)
    assert all(item.graph_uuid == "graph-uuid-1" for item in items)
    assert items[0].document_id == "DOC-Z-1"
    assert items[0].data_type == "json"
    assert items[0].reference_time == "2025-01-10T00:00:00+00:00"
    assert items[0].metadata["doc_id"] == "DOC-Z-1"
    assert len(fake.calls("batch.process")) == 1
    assert len(fake.calls("batch.get")) == 2  # polled until terminal
    # The single-document path was NOT used for a bulk run.
    assert fake.calls("graph.episode.add") == []
    # And the cited layer holds both documents' facts.
    predicates = {f.predicate for f in adapter.get_facts("ACC-Z")}
    assert predicates == {"legal_name", "claims_count_3yr"}


def test_single_document_ingest_stays_on_episode_add():
    fake = FakeZepClient()
    adapter = make_adapter(fake)
    ingest_document(adapter, sample_doc())
    assert fake.calls("batch.create") == []
    assert len(fake.calls("graph.episode.add")) == 1


def test_batch_partial_records_landed_and_names_failed():
    fake = FakeZepClient()
    fake.batch_statuses = ["partial"]
    fake.batch_items = [
        FakeBatchItem("succeeded", 0),
        FakeBatchItem("failed", 1),
    ]
    adapter = make_adapter(fake)
    with pytest.raises(ContextUnavailable) as excinfo:
        ingest_account(adapter, two_doc_account())
    assert "DOC-Z-2" in str(excinfo.value)
    # The landed document is in the cited layer; the failed one is not.
    assert {f.predicate for f in adapter.get_facts("ACC-Z")} == {"legal_name"}
    assert {e.doc_id for e in adapter.get_episodes("ACC-Z")} == {"DOC-Z-1"}


def test_batch_submission_rejection_is_unavailable():
    fake = FakeZepClient()
    fake.batch_rejected = [FakeRejectedItem(1, "payload too large")]
    adapter = make_adapter(fake)
    with pytest.raises(ContextUnavailable):
        ingest_account(adapter, two_doc_account())
    assert fake.calls("batch.process") == []


# ---------------------------------------------------------------------------
# Failure honesty
# ---------------------------------------------------------------------------


def test_sdk_error_is_unavailable_not_offline():
    fake = FakeZepClient()
    fake.fail_on = {"graph.episode.add"}
    adapter = make_adapter(fake)
    with pytest.raises(ContextUnavailable):
        ingest_document(adapter, sample_doc())


def test_context_block_error_is_unavailable():
    fake = FakeZepClient()
    fake.fail_on = {"graph.get_context"}
    adapter = make_adapter(fake)
    with pytest.raises(ContextUnavailable):
        adapter.get_context_block("ACC-Z", date(2026, 6, 1))


def test_search_error_is_unavailable():
    fake = FakeZepClient()
    fake.fail_on = {"graph.search_edges"}
    adapter = make_adapter(fake)
    with pytest.raises(ContextUnavailable):
        adapter.search_facts("ACC-Z", "limit")


# ---------------------------------------------------------------------------
# The Postgres registry (env-gated, the repo's TEST_DATABASE_URL pattern)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL not set — Postgres graph registry test skipped",
)
def test_postgres_graph_registry_round_trip():
    from zep_context_agent.zep_graphs import PostgresGraphRegistry

    url = os.environ["TEST_DATABASE_URL"]
    registry = PostgresGraphRegistry(url)
    account_id = "ACC-REG-TEST"
    assert registry.get(account_id) is None
    record = registry.record(account_id, "graph-uuid-reg")
    assert record.graph_uuid == "graph-uuid-reg"
    assert not record.ontology_applied
    # The first provisioning stands against a second record call.
    assert registry.record(account_id, "graph-uuid-other").graph_uuid == (
        "graph-uuid-reg"
    )
    updated = registry.mark_ontology_applied(account_id)
    assert updated is not None and updated.ontology_applied
    assert registry.get(account_id).ontology_applied
