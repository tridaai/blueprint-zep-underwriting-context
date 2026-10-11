"""Shared fixtures: the synthetic universe and service factories.

Every service fixture is built on explicit doubles — a fresh
offline graph, an in-memory brief store, no object store, and
(except where a test says otherwise) **no cache**: production code
never substitutes the in-memory cache silently, and neither do the
tests.
"""

from __future__ import annotations

import pytest
from langgraph.checkpoint.memory import MemorySaver

from zep_context_agent.cache import InMemoryCache
from zep_context_agent.datasets import load_accounts
from zep_context_agent.offline_graph import OfflineContextGraph
from zep_context_agent.service import BriefService
from zep_context_agent.store import InMemoryBriefStore


@pytest.fixture(scope="session")
def accounts():
    return load_accounts()


def make_service(accounts, **overrides) -> BriefService:
    kwargs = dict(
        port=OfflineContextGraph(),
        store=InMemoryBriefStore(),
        object_store=None,
        checkpointer=MemorySaver(),
        cache=None,
        accounts=accounts,
    )
    kwargs.update(overrides)
    return BriefService(**kwargs)


@pytest.fixture
def service(accounts):
    """Graph path (MemorySaver checkpointer), caching disabled."""
    return make_service(accounts)


@pytest.fixture
def service_no_checkpointer(accounts):
    """Service-state gate path (CHECKPOINTS=off equivalent)."""
    return make_service(accounts, checkpointer=None)


@pytest.fixture
def cached_service(accounts):
    """Graph path with the in-memory cache double wired in."""
    return make_service(accounts, cache=InMemoryCache())
