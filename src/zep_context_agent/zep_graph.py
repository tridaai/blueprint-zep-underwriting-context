"""The Zep Cloud ContextPort adapter.

Talks to the Zep API (v2 REST surface) over plain HTTPS — no vendor
SDK is vendored in. How the mapping works, honestly:

- **Writes** go to Zep for real: each source document is ingested
  as a JSON episode into the account's own graph (Zep ``group_id``
  = one scope per insurance account — that is the account-scoping
  seam on the Zep side, mirroring the access policies a deployment
  configures there).
- **Cited facts** are read back from the episode payload index this
  adapter keeps for what it ingested. An audit-grade brief cannot
  cite a paraphrase: the episode payload *is* the citation record,
  and Zep builds its semantic graph from those same payloads
  server-side. The index is an embedded local graph; when a
  citation index store is wired in (production wiring attaches
  the Postgres one), it persists there and a restart re-reads the
  cited layer from the database — see :mod:`citations`.
- **The context block** is Zep's own: a per-account thread's user
  context, fetched live. If Zep is unreachable the call raises —
  the brief then assembles from cited facts alone and says the
  provider context was unavailable. It never silently substitutes
  the offline graph.

Configuration: ``ZEP_API_KEY`` (required), ``ZEP_API_BASE_URL``
(default the hosted API; point it at a self-hosted/BYOC Zep stack
to retarget), ``ZEP_TIMEOUT_SECONDS``.
"""

from __future__ import annotations

import json
from datetime import date

import httpx

from .config import env_float, env_str
from .errors import ContextNotConfigured, ContextUnavailable
from .offline_graph import OfflineContextGraph
from .ontology import Edge, Episode, Fact, SourceDocument

DEFAULT_BASE_URL = "https://api.getzep.com/api/v2"


def _group_id(account_id: str) -> str:
    return f"uw-account-{account_id}"


def _thread_id(account_id: str) -> str:
    return f"uw-brief-{account_id}"


class ZepContextGraph:
    name = "zep"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        *,
        client: httpx.Client | None = None,
        citation_index=None,
    ) -> None:
        self._api_key = api_key if api_key is not None else env_str("ZEP_API_KEY")
        if not self._api_key:
            raise ContextNotConfigured(
                "CONTEXT_BACKEND=zep but ZEP_API_KEY is not set. Add the "
                "key from the Zep dashboard (see .env.example), or run "
                "CONTEXT_BACKEND=offline for the labelled local graph."
            )
        self._base_url = (
            base_url or env_str("ZEP_API_BASE_URL") or DEFAULT_BASE_URL
        ).rstrip("/")
        timeout = env_float("ZEP_TIMEOUT_SECONDS", 30.0)
        self._client = client or httpx.Client(timeout=timeout)
        # The episode-payload index for cited facts (see module docstring).
        self._index = OfflineContextGraph(citation_index=citation_index)

    @property
    def citation_index(self):
        """The citation index backing the cited layer, if any."""
        return self._index.citation_index

    # -- HTTP plumbing ------------------------------------------------------
    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        url = f"{self._base_url}{path}"
        headers = {"Authorization": f"Api-Key {self._api_key}"}
        try:
            response = self._client.request(
                method, url, json=payload, headers=headers
            )
        except httpx.HTTPError as exc:
            raise ContextUnavailable(
                f"Zep API unreachable at {self._base_url}: {exc}"
            ) from exc
        if response.status_code >= 400:
            raise ContextUnavailable(
                f"Zep API {method} {path} answered "
                f"{response.status_code}: {response.text[:200]}"
            )
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            return {}

    # -- writes ---------------------------------------------------------------
    def ingest_episode(self, doc: SourceDocument, episode: Episode) -> Episode:
        payload = {
            "group_id": _group_id(doc.account_id),
            "episodes": [
                {
                    "name": episode.episode_id,
                    "episode_body": json.dumps(
                        {
                            "doc_id": doc.doc_id,
                            "kind": doc.kind,
                            "title": doc.title,
                            "account_id": doc.account_id,
                            "assertions": [
                                a.model_dump(mode="json") for a in doc.assertions
                            ],
                            "edges": [
                                e.model_dump(mode="json") for e in doc.edges
                            ],
                        }
                    ),
                    "type": "json",
                    "source": "trida-uw-context-blueprint",
                    "source_description": (
                        f"{doc.kind} document for account {doc.account_id}"
                    ),
                    "reference_time": doc.received_at.isoformat(),
                }
            ],
        }
        self._request("POST", "/graph/episode", payload)
        return self._index.ingest_episode(doc, episode)

    # -- reads (cited layer, from the episode-payload index) ------------------
    def get_facts(self, account_id: str) -> list[Fact]:
        return self._index.get_facts(account_id)

    def get_episodes(self, account_id: str) -> list[Episode]:
        return self._index.get_episodes(account_id)

    def get_edges(self, account_id: str) -> list[Edge]:
        return self._index.get_edges(account_id)

    def search_facts(self, account_id: str, query: str, limit: int = 10) -> list[Fact]:
        # Ranked retrieval against Zep's graph search enriches the
        # cited layer; the cited facts themselves come from the index
        # so a hit can always be traced to its episode.
        self._request(
            "POST",
            "/graph/search",
            {"group_id": _group_id(account_id), "query": query, "limit": limit},
        )
        return self._index.search_facts(account_id, query, limit)

    def get_context_block(self, account_id: str, as_of: date) -> str | None:
        thread_id = _thread_id(account_id)
        self._request(
            "POST",
            "/threads",
            {"thread_id": thread_id, "user_ids": [account_id]},
        )
        result = self._request("GET", f"/threads/{thread_id}/context")
        context = result.get("context")
        return context if isinstance(context, str) and context else None
