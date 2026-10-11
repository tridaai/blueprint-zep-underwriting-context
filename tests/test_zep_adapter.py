"""The Zep adapter's contract, pinned over a stubbed transport.

Live verification against a real Zep workspace is pending an API
key (see README "Honest status"); these tests pin what the adapter
sends and how it fails — in particular that it fails honestly and
never falls back to the offline graph.
"""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

from zep_context_agent.errors import ContextNotConfigured, ContextUnavailable
from zep_context_agent.ingestion import ingest_document
from zep_context_agent.ontology import FactAssertion, SourceDocument
from zep_context_agent.zep_graph import ZepContextGraph


def make_adapter(handler) -> ZepContextGraph:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return ZepContextGraph(
        api_key="test-key", base_url="https://api.getzep.com/api/v2", client=client
    )


def ok_handler(requests: list) -> object:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/threads/context") or "/context" in request.url.path:
            return httpx.Response(200, json={"context": "ZEP CONTEXT BLOCK"})
        return httpx.Response(200, json={})

    return handler


def sample_doc() -> SourceDocument:
    return SourceDocument(
        doc_id="DOC-Z-1", account_id="ACC-Z", kind="submission",
        title="Submission", received_at=date(2025, 1, 10),
        assertions=[
            FactAssertion(predicate="legal_name", value="Zed Co", valid_from=date(2025, 1, 10))
        ],
    )


def test_missing_key_is_not_configured(monkeypatch):
    monkeypatch.delenv("ZEP_API_KEY", raising=False)
    with pytest.raises(ContextNotConfigured):
        ZepContextGraph(api_key=None)


def test_ingest_posts_episode_to_account_group():
    requests: list = []
    adapter = make_adapter(ok_handler(requests))
    ingest_document(adapter, sample_doc())
    posts = [r for r in requests if r.url.path.endswith("/graph/episode")]
    assert len(posts) == 1
    payload = json.loads(posts[0].content)
    assert payload["group_id"] == "uw-account-ACC-Z"
    body = json.loads(payload["episodes"][0]["episode_body"])
    assert body["doc_id"] == "DOC-Z-1"
    assert body["assertions"][0]["predicate"] == "legal_name"
    assert posts[0].headers["Authorization"] == "Api-Key test-key"
    # Cited facts read back from the ingested payloads.
    assert [f.value for f in adapter.get_facts("ACC-Z")] == ["Zed Co"]


def test_context_block_comes_from_zep_thread():
    requests: list = []
    adapter = make_adapter(ok_handler(requests))
    block = adapter.get_context_block("ACC-Z", date(2026, 6, 1))
    assert block == "ZEP CONTEXT BLOCK"
    paths = [r.url.path for r in requests]
    assert any(p.endswith("/threads") for p in paths)
    assert any("/threads/uw-brief-ACC-Z/context" in p for p in paths)


def test_http_500_is_unavailable_not_offline():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    adapter = make_adapter(handler)
    with pytest.raises(ContextUnavailable):
        ingest_document(adapter, sample_doc())
    with pytest.raises(ContextUnavailable):
        adapter.get_context_block("ACC-Z", date(2026, 6, 1))


def test_transport_error_is_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    adapter = make_adapter(handler)
    with pytest.raises(ContextUnavailable):
        adapter.search_facts("ACC-Z", "limit")
