"""Brief stores: the in-memory double's semantics, and the Postgres
store behind an env gate (skipped without a test database)."""

from __future__ import annotations

import os
from datetime import date, datetime, timezone

import pytest

from zep_context_agent.schemas import Brief
from zep_context_agent.store import InMemoryBriefStore, PostgresBriefStore


def brief(brief_id: str, account_id: str = "ACC-001") -> Brief:
    return Brief(
        brief_id=brief_id, account_id=account_id, account_name="Test Co",
        as_of=date(2026, 6, 1), backend="offline",
        created_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )


def test_in_memory_roundtrip_and_order():
    store = InMemoryBriefStore()
    store.save(brief("brief-1"))
    store.save(brief("brief-2", "ACC-002"))
    store.save(brief("brief-3"))
    assert store.get("brief-2").account_id == "ACC-002"
    assert store.get("brief-nope") is None
    assert [b.brief_id for b in store.list()] == ["brief-1", "brief-2", "brief-3"]
    assert [b.brief_id for b in store.list("ACC-001")] == ["brief-1", "brief-3"]
    # Saving the same brief again updates in place (decision path).
    updated = brief("brief-1").model_copy(update={"status": "DECIDED"})
    store.save(updated)
    assert store.get("brief-1").status == "DECIDED"
    assert len(store.list()) == 3


@pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL not set — Postgres store test skipped",
)
def test_postgres_store_roundtrip():
    from zep_context_agent.db import ensure_migrated

    url = os.environ["TEST_DATABASE_URL"]
    ensure_migrated(url)
    store = PostgresBriefStore(url)
    store.save(brief("brief-pg-1"))
    loaded = store.get("brief-pg-1")
    assert loaded is not None and loaded.account_id == "ACC-001"
    assert any(b.brief_id == "brief-pg-1" for b in store.list("ACC-001"))
