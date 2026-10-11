"""The brief store: the record of briefs and gate decisions.

Responsibility split, kept deliberately:

- the **store** (this module) is the record — assembled briefs,
  decisions, audit trails; every surface (API, console, export,
  evals) reads briefs from here;
- the **checkpointer** (``checkpoints.py``) holds graph state —
  where a run paused at the gate. It never decides anything.

Implementations: :class:`PostgresBriefStore` (JSONB payloads in
the ``briefs`` table — the production configuration, selected when
``DATABASE_URL`` is set) and :class:`InMemoryBriefStore`, the test
double. Both keep insertion order via a sequence, so lists read the
same way in either mode.
"""

from __future__ import annotations

import json
import threading

from .schemas import Brief


class InMemoryBriefStore:
    """Dict-backed brief store (tests and no-database smoke runs)."""

    def __init__(self) -> None:
        self._briefs: dict[str, Brief] = {}
        self._order: list[str] = []

    def save(self, brief: Brief) -> Brief:
        if brief.brief_id not in self._briefs:
            self._order.append(brief.brief_id)
        self._briefs[brief.brief_id] = brief
        return brief

    def get(self, brief_id: str) -> Brief | None:
        return self._briefs.get(brief_id)

    def list(self, account_id: str | None = None) -> list[Brief]:
        briefs = [self._briefs[bid] for bid in self._order]
        if account_id is not None:
            briefs = [b for b in briefs if b.account_id == account_id]
        return briefs


class PostgresBriefStore:
    """Brief store over the ``briefs`` table (one connection per call)."""

    def __init__(self, url: str) -> None:
        from .db import ensure_migrated

        ensure_migrated(url)
        self._url = url
        self._lock = threading.Lock()

    def _connect(self):
        from .db import connect

        return connect(self._url)

    def save(self, brief: Brief) -> Brief:
        payload = json.dumps(brief.model_dump(mode="json"))
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO briefs (brief_id, account_id, payload, status, decided) "
                "VALUES (%s, %s, %s::jsonb, %s, %s) "
                "ON CONFLICT (brief_id) DO UPDATE SET "
                "payload = EXCLUDED.payload, status = EXCLUDED.status, "
                "decided = EXCLUDED.decided, updated_at = now()",
                (
                    brief.brief_id,
                    brief.account_id,
                    payload,
                    brief.status,
                    brief.decision is not None,
                ),
            )
            conn.commit()
        return brief

    def get(self, brief_id: str) -> Brief | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM briefs WHERE brief_id = %s", (brief_id,)
            ).fetchone()
        if row is None:
            return None
        payload = row[0]
        if isinstance(payload, str):
            payload = json.loads(payload)
        return Brief.model_validate(payload)

    def list(self, account_id: str | None = None) -> list[Brief]:
        query = "SELECT payload FROM briefs"
        params: tuple = ()
        if account_id is not None:
            query += " WHERE account_id = %s"
            params = (account_id,)
        query += " ORDER BY seq"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        briefs = []
        for (payload,) in rows:
            if isinstance(payload, str):
                payload = json.loads(payload)
            briefs.append(Brief.model_validate(payload))
        return briefs


def default_store():
    """The configured brief store: Postgres when ``DATABASE_URL``
    is set, the in-memory test double otherwise."""
    from .db import database_url

    url = database_url()
    if url:
        return PostgresBriefStore(url)
    return InMemoryBriefStore()
