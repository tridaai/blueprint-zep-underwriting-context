"""Wiring: build the configured ContextPort.

The selection rule is deliberately boring, and it is the honesty
rule of this blueprint:

- ``CONTEXT_BACKEND`` unset → the offline adapter. Code defaults
  to offline so tests, evals, and a no-key clone run at all.
- ``CONTEXT_BACKEND=offline`` → the offline adapter, labelled.
- ``CONTEXT_BACKEND=zep`` → the real Zep adapter. If the profile
  is selected but ``ZEP_API_KEY`` is missing, construction raises
  :class:`ContextNotConfigured`; if Zep is unreachable, its calls
  raise :class:`ContextUnavailable`. Neither is ever papered over
  by quietly serving the offline graph instead — a brief assembled
  from the local graph must never wear Zep's name.
"""

from __future__ import annotations

from .citations import default_citation_index
from .config import context_backend
from .errors import ContextNotConfigured
from .offline_graph import OfflineContextGraph
from .ports import ContextPort
from .zep_graphs import default_graph_registry


def build_context_port() -> ContextPort:
    backend = context_backend()
    # Both profiles persist their cited layer through the same
    # citation index (Postgres when DATABASE_URL is set, in-process
    # only otherwise — readiness states which).
    citation_index = default_citation_index()
    if backend == "offline":
        return OfflineContextGraph(citation_index=citation_index)
    if backend == "zep":
        from .zep_graph import ZepContextGraph

        return ZepContextGraph(
            citation_index=citation_index,
            graph_registry=default_graph_registry(),
        )
    raise ContextNotConfigured(
        f"Unknown CONTEXT_BACKEND={backend!r}. Use 'zep' (the real "
        "Zep Cloud profile) or 'offline' (the labelled local graph)."
    )
