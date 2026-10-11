"""The Redis layer's discipline, pinned over the in-memory double
(the double models the semantics; the Redis adapter is exercised
for its degraded-mode honesty without a server).

The pins that matter:
- a cache hit serves the assembled brief without reassembling;
- ingesting a superseding episode invalidates the account's cached
  brief — the re-assembled brief reflects the new fact;
- keys are account-scoped: account A's brief is unreachable under
  account B's key space;
- the ingestion lock claims, blocks, and releases;
- disabled mode is honest: no REDIS_URL, no cache — and no silent
  in-memory substitution.
"""

from __future__ import annotations

from datetime import date

import pytest

from zep_context_agent import cache as cache_mod
from zep_context_agent.cache import (
    CachingContextPort,
    InMemoryCache,
    RedisCache,
    brief_key,
    get_cache,
    lock_key,
    version_key,
)
from zep_context_agent.errors import IngestionInProgress
from zep_context_agent.ingestion import ingest_document
from zep_context_agent.offline_graph import OfflineContextGraph
from zep_context_agent.ontology import FactAssertion, SourceDocument

AS_OF = date(2026, 6, 1)


def test_brief_cache_hit(cached_service):
    first = cached_service.assemble("ACC-001", as_of=AS_OF)
    second = cached_service.assemble("ACC-001", as_of=AS_OF)
    assert second.brief_id == first.brief_id  # served, not reassembled
    assert len(cached_service.store.list("ACC-001")) == 1


def test_invalidation_on_ingest(cached_service):
    """THE pin: assemble → ingest a superseding episode → the next
    brief reflects the new fact, cache notwithstanding."""
    before = cached_service.assemble("ACC-001", as_of=AS_OF)
    assert [f.value for f in before.current_facts if f.predicate == "policy_limit_usd"] == [2500000]
    doc = SourceDocument(
        doc_id="DOC-001-END3", account_id="ACC-001", kind="endorsement",
        title="Late endorsement — limit raised", received_at=date(2026, 5, 1),
        assertions=[
            FactAssertion(
                predicate="policy_limit_usd", value=3000000,
                valid_from=date(2026, 5, 1), entity_ref="POL-ACC001-2024",
            )
        ],
    )
    ingest_document(cached_service.port, doc)
    after = cached_service.assemble("ACC-001", as_of=AS_OF)
    assert after.brief_id != before.brief_id
    assert [f.value for f in after.current_facts if f.predicate == "policy_limit_usd"] == [3000000]


def test_invalidation_covers_the_diff_field(cached_service):
    """The brief diff rides in the cached payload: after a
    superseding ingestion, the re-assembled brief must show the
    new fact AND a diff reflecting the change — and the diff
    survives being served back from the cache."""
    first = cached_service.assemble("ACC-001", as_of=AS_OF)
    assert first.diff is None  # first brief for the account
    doc = SourceDocument(
        doc_id="DOC-001-END3", account_id="ACC-001", kind="endorsement",
        title="Late endorsement — limit raised", received_at=date(2026, 5, 1),
        assertions=[
            FactAssertion(
                predicate="policy_limit_usd", value=3000000,
                valid_from=date(2026, 5, 1), entity_ref="POL-ACC001-2024",
            )
        ],
    )
    ingest_document(cached_service.port, doc)
    after = cached_service.assemble("ACC-001", as_of=AS_OF)
    assert after.brief_id != first.brief_id
    assert after.diff is not None
    assert after.diff.previous_brief_id == first.brief_id
    changes = [c for c in after.diff.superseded if c.predicate == "policy_limit_usd"]
    assert len(changes) == 1
    assert changes[0].previous_value == 2500000
    assert changes[0].current_value == 3000000
    # Served from the cache under the new version, diff intact.
    served = cached_service.assemble("ACC-001", as_of=AS_OF)
    assert served.brief_id == after.brief_id
    assert served.diff is not None
    assert served.diff.previous_brief_id == first.brief_id


def test_decision_invalidates_cached_brief(cached_service):
    brief = cached_service.assemble("ACC-002", as_of=AS_OF)
    cached_service.decide(
        brief.brief_id, action="proceed", actor="R. Osei", reason="Clean file."
    )
    again = cached_service.assemble("ACC-002", as_of=AS_OF)
    # The decision memo is a new episode: the version bumped, so this
    # is a fresh brief — not the decided one served from cache.
    assert again.brief_id != brief.brief_id
    assert again.status == "AWAITING_UNDERWRITER"


def test_cache_keys_are_account_scoped(cached_service):
    cache = cached_service.cache
    brief_a = cached_service.assemble("ACC-001", as_of=AS_OF)
    version_a = cached_service._facts_version("ACC-001")
    assert cache.get(brief_key("ACC-001", version_a, AS_OF)) is not None
    # A's brief is unreachable under B's key space, at any version.
    assert cache.get(brief_key("ACC-002", version_a, AS_OF)) is None
    brief_b = cached_service.assemble("ACC-002", as_of=AS_OF)
    assert brief_b.account_id == "ACC-002"
    assert brief_b.brief_id != brief_a.brief_id
    # And every content key in the cache names its account.
    for key in list(cache._values):
        if ":brief:" in key or ":retrieval:" in key:
            assert ":ACC-" in key


def test_retrieval_cache_hit_and_version_bump():
    cache = InMemoryCache()
    delegate = OfflineContextGraph()
    port = CachingContextPort(delegate, cache)
    doc = SourceDocument(
        doc_id="DOC-C-1", account_id="ACC-C", kind="submission",
        title="Submission", received_at=date(2025, 1, 10),
        assertions=[
            FactAssertion(predicate="legal_name", value="Cache Co", valid_from=date(2025, 1, 10))
        ],
    )
    ingest_document(port, doc)
    calls = []
    original = delegate.get_facts
    delegate.get_facts = lambda a: (calls.append(a), original(a))[1]
    assert [f.value for f in port.get_facts("ACC-C")] == ["Cache Co"]
    assert [f.value for f in port.get_facts("ACC-C")] == ["Cache Co"]
    assert len(calls) == 1  # second read served from cache
    # A new episode bumps the version: the next read refetches.
    ingest_document(port, SourceDocument(
        doc_id="DOC-C-2", account_id="ACC-C", kind="claims_extract",
        title="Extract", received_at=date(2025, 2, 1),
        assertions=[
            FactAssertion(predicate="claims_count_3yr", value=4, valid_from=date(2025, 2, 1))
        ],
    ))
    assert {f.predicate for f in port.get_facts("ACC-C")} == {"legal_name", "claims_count_3yr"}
    assert len(calls) == 2


def test_lock_claim_block_release():
    cache = InMemoryCache()
    key = lock_key("ACC-001")
    token = cache.try_lock(key, 60)
    assert token is not None
    assert cache.try_lock(key, 60) is None  # held
    assert cache.unlock(key, "not-the-token") is False  # not ours
    assert cache.try_lock(key, 60) is None  # still held
    assert cache.unlock(key, token) is True
    assert cache.try_lock(key, 60) is not None  # free again


def test_lock_expiry():
    cache = InMemoryCache()
    key = lock_key("ACC-001")
    assert cache.try_lock(key, -1) is not None  # expires immediately
    assert cache.try_lock(key, 60) is not None  # so the next claim wins


def test_service_ingest_lock_blocks_second_worker(cached_service):
    cache = cached_service.cache
    token = cache.try_lock(lock_key("ACC-003"), 60)
    try:
        with pytest.raises(IngestionInProgress):
            cached_service.ingest("ACC-003")
    finally:
        cache.unlock(lock_key("ACC-003"), token)
    assert cached_service.ingest("ACC-003")  # released: works again


def test_disabled_mode_is_honest(monkeypatch, accounts):
    monkeypatch.delenv("REDIS_URL", raising=False)
    assert get_cache() is None  # no silent in-memory substitution
    from conftest import make_service

    svc = make_service(accounts, cache=None)
    assert svc.readiness()["cache"] == "disabled (REDIS_URL unset)"
    brief = svc.assemble("ACC-001", as_of=AS_OF)  # works, uncached
    assert brief.status == "AWAITING_UNDERWRITER"


def test_redis_unreachable_degrades_never_crashes(monkeypatch, accounts):
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6399/0")
    cache = get_cache()
    assert isinstance(cache, RedisCache)
    assert cache.ping() is False
    assert cache.get(version_key("ACC-001")) is None  # miss, not a crash
    assert cache.incr(version_key("ACC-001")) is None  # unavailable, stated
    from conftest import make_service

    svc = make_service(accounts, cache=cache)
    assert svc.readiness()["cache"] == "redis UNREACHABLE"
    brief = svc.assemble("ACC-001", as_of=AS_OF)  # still serves, uncached
    assert [f.value for f in brief.current_facts if f.predicate == "policy_limit_usd"] == [2500000]
