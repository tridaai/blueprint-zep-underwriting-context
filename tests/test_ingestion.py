"""Ingestion: documents become episodes with lineage; the archive
copy lands in object storage; quarantined assertions never land."""

from __future__ import annotations

from zep_context_agent.ingestion import ingest_account
from zep_context_agent.object_store import InMemoryObjectStore
from zep_context_agent.offline_graph import OfflineContextGraph


def test_ingest_account_lineage(accounts):
    port = OfflineContextGraph()
    account = accounts["ACC-001"]
    episodes = ingest_account(port, account)
    assert len(episodes) == len(account.documents) == 7
    facts = port.get_facts("ACC-001")
    by_doc = {d.doc_id: d for d in account.documents}
    for fact in facts:
        assert fact.doc_id in by_doc
        assert fact.episode_id == f"ep-{fact.doc_id}"


def test_quarantined_assertions_never_become_facts(accounts):
    port = OfflineContextGraph()
    ingest_account(port, accounts["ACC-001"])
    claims = [
        f for f in port.get_facts("ACC-001") if f.predicate == "claims_count_3yr"
    ]
    # The extract's 2 stands; the injection's 0 was quarantined away.
    assert [f.value for f in claims] == [2]


def test_documents_are_archived_to_object_storage(accounts):
    port = OfflineContextGraph()
    store = InMemoryObjectStore()
    ingest_account(port, accounts["ACC-002"], object_store=store)
    assert store.exists("accounts/ACC-002/documents/DOC-002-SUB.json")
    payload = store.get("accounts/ACC-002/documents/DOC-002-SUB.json")
    assert b"Meridian Castings LLC" in payload


def test_ingestion_is_repeatable(accounts):
    port = OfflineContextGraph()
    account = accounts["ACC-003"]
    ingest_account(port, account)
    first = len(port.get_facts("ACC-003"))
    ingest_account(port, account)
    assert len(port.get_facts("ACC-003")) == first
