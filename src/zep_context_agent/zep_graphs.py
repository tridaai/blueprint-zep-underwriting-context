"""The Zep graph registry: which Zep graph serves which account.

Zep v4 addresses graphs by server-assigned UUID — ``graph.create``
returns the UUID and every later call names it. The v2 adapter this
replaces named graphs itself (``group_id``), so it needed no record
of its own. The v4 adapter provisions one Context Graph per
insurance account and must remember the mapping durably: a restart
that "re-created" an account's graph would strand everything
already ingested in the old one.

The registry also records whether the blueprint's ontology has been
applied to the graph. ``graph.set_ontology`` is not retroactive —
it must land before the graph's first episode — and it is only
*recorded* here after the call succeeds, so a crash between create
and set leaves the flag down and the next touch sets the ontology
before ingesting anything.

Implementations follow the repo's store split (see
:mod:`citations`): :class:`PostgresGraphRegistry` over the
``zep_graphs`` table (migration 0008), selected when
``DATABASE_URL`` is set, and :class:`InMemoryGraphRegistry`, the
test double. Records are write-once per account: the first
provisioning stands, mirroring ingestion's idempotency.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class GraphRecord:
    account_id: str
    graph_uuid: str
    ontology_applied: bool


class InMemoryGraphRegistry:
    """Dict-backed graph registry (the test double)."""

    name = "in-memory test double"

    def __init__(self) -> None:
        self._records: dict[str, GraphRecord] = {}

    def get(self, account_id: str) -> GraphRecord | None:
        return self._records.get(account_id)

    def record(
        self, account_id: str, graph_uuid: str, *, ontology_applied: bool = False
    ) -> GraphRecord:
        standing = self._records.get(account_id)
        if standing is not None:
            return standing  # the first provisioning stands
        record = GraphRecord(account_id, graph_uuid, ontology_applied)
        self._records[account_id] = record
        return record

    def mark_ontology_applied(self, account_id: str) -> GraphRecord | None:
        standing = self._records.get(account_id)
        if standing is None or standing.ontology_applied:
            return standing
        updated = GraphRecord(account_id, standing.graph_uuid, True)
        self._records[account_id] = updated
        return updated


class PostgresGraphRegistry:
    """Graph registry over the ``zep_graphs`` table."""

    name = "postgres"

    def __init__(self, url: str) -> None:
        from .db import ensure_migrated

        ensure_migrated(url)
        self._url = url
        self._lock = threading.Lock()

    def _connect(self):
        from .db import connect

        return connect(self._url)

    def get(self, account_id: str) -> GraphRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT graph_uuid, ontology_applied FROM zep_graphs "
                "WHERE account_id = %s",
                (account_id,),
            ).fetchone()
        if row is None:
            return None
        return GraphRecord(account_id, row[0], bool(row[1]))

    def record(
        self, account_id: str, graph_uuid: str, *, ontology_applied: bool = False
    ) -> GraphRecord:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO zep_graphs (account_id, graph_uuid, ontology_applied) "
                "VALUES (%s, %s, %s) ON CONFLICT (account_id) DO NOTHING",
                (account_id, graph_uuid, ontology_applied),
            )
            conn.commit()
        standing = self.get(account_id)
        assert standing is not None  # just inserted or already standing
        return standing

    def mark_ontology_applied(self, account_id: str) -> GraphRecord | None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE zep_graphs SET ontology_applied = TRUE, "
                "updated_at = now() WHERE account_id = %s",
                (account_id,),
            )
            conn.commit()
        return self.get(account_id)


def default_graph_registry():
    """The configured graph registry: Postgres when ``DATABASE_URL``
    is set, the in-memory test double otherwise (provisioning state
    then lasts for the process — the same honesty rule the citation
    index follows)."""
    from .db import database_url

    url = database_url()
    if url:
        return PostgresGraphRegistry(url)
    return InMemoryGraphRegistry()
