"""Checkpointed gate: LangGraph state persistence.

The brief gate is a real pause in the graph, not a status flag:
with a checkpointer attached, a run executes up to the gate and its
graph state persists (thread_id = the brief id); the service's
decision call then *resumes* the thread, and the graph completes.
A process restart loses nothing — a new service instance over the
same database sees the store record AND the paused thread.

The split of responsibilities, kept deliberately:

- the **store** (``store.py``) is the record of briefs and
  decisions; every surface reads it;
- the **checkpointer** (this module) holds graph state — where the
  run paused and what it carried. It never decides anything.

Which saver runs is a deployment decision, made in
:func:`get_checkpointer`:

- ``DATABASE_URL`` set → the **official LangGraph Postgres saver**
  (``langgraph-checkpoint-postgres``) over a psycopg 3 connection
  pool, in the same PostgreSQL as the store. This is the production
  configuration; its tables are created by the saver's own
  ``setup()``.
- No ``DATABASE_URL`` → LangGraph's in-memory saver. A test double:
  the gate still pauses and resumes within the process, nothing
  persists.
- ``CHECKPOINTS=off`` disables the whole thing: no checkpointer is
  attached and the gate flow is the service-state flow — the service
  applies the decision to the stored brief directly.
"""

from __future__ import annotations

from .config import env_str, load_dotenv

_POSTGRES_SAVERS: dict[str, object] = {}


def checkpoints_enabled() -> bool:
    """``CHECKPOINTS`` env (read at call time): on unless explicitly off."""
    load_dotenv()
    raw = (env_str("CHECKPOINTS") or "on").strip().lower()
    return raw not in {"off", "0", "false", "no"}


def _postgres_checkpointer(url: str):
    """The official LangGraph Postgres saver over a shared pool.

    One saver (and pool) per database URL per process: surfaces that
    rebuild their service share the pool instead of leaking a new
    one per construction. A saver whose pool was closed is rebuilt,
    never handed out dead. The pool carries the settings the saver
    requires (autocommit, no prepared statements, dict rows); the
    saver's ``setup()`` creates its checkpoint tables on first use.
    """
    existing = _POSTGRES_SAVERS.get(url)
    if existing is not None:
        pool = getattr(existing, "conn", None)
        if pool is None or not getattr(pool, "closed", False):
            return existing
        _POSTGRES_SAVERS.pop(url, None)
    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    from .db import ensure_migrated

    ensure_migrated(url)
    pool = ConnectionPool(
        url,
        min_size=1,
        max_size=8,
        open=True,
        kwargs={
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
        },
    )
    saver = PostgresSaver(pool)
    saver.setup()
    _POSTGRES_SAVERS[url] = saver
    return saver


def get_checkpointer():
    """The configured checkpointer, or None when CHECKPOINTS=off.

    ``DATABASE_URL`` set → the official Postgres saver (production).
    Unset → LangGraph's in-memory saver (test double; the gate
    works, nothing persists). See the module docstring.
    """
    if not checkpoints_enabled():
        return None
    from .db import database_url

    url = database_url()
    if url:
        return _postgres_checkpointer(url)
    from langgraph.checkpoint.memory import MemorySaver

    return MemorySaver()
