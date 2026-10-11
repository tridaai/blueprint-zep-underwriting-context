"""The cache + coordination layer: Redis when configured, absent when not.

Redis has exactly two jobs in this blueprint, and being a source of
truth is neither of them:

1. **Cache.** Assembled brief payloads and ContextPort retrieval
   results, so a busy underwriting desk does not re-assemble the
   same account brief on every click. Postgres and the ContextPort
   stay authoritative; anything in the cache can be dropped and
   rebuilt.
2. **Ingestion coordination.** A short-lived lock per account, so
   two workers never ingest the same account at the same time.

Three design rules carry the weight:

- **Invalidation is versioned, not hoped for.** Every cached entry
  is keyed by the account's *facts-version* — a counter in the
  cache itself, bumped by :class:`CachingContextPort` on every
  episode ingested for that account. The moment a new episode
  lands (a fresh endorsement, a gate decision memo), every brief
  and retrieval entry written under the old version becomes
  unreachable. A brief can never serve facts older than the latest
  ingestion.
- **Keys are account-scoped by construction.** The account id is
  part of every brief/retrieval key; there are no shared or
  unscoped keys for account content, so account A's cached brief
  is unreachable under account B's key space — the same zero-
  tolerance leakage discipline as the briefs themselves.
- **Redis is optional, and its absence is stated, not papered
  over.** When ``REDIS_URL`` is unset, :func:`get_cache` returns
  ``None`` and the service runs with caching *disabled* — it does
  not quietly substitute an in-memory cache in production mode.
  The in-memory double below exists for tests, which construct it
  explicitly. If Redis is configured but unreachable, cache reads
  degrade to misses and writes are skipped (an accelerator being
  down must not take the brief down with it), while ``/readiness``
  reports ``redis UNREACHABLE`` in plain words. Cached entries also
  carry a one-hour TTL as a backstop, so an entry orphaned by a
  version bump expires instead of lingering.

What Redis deliberately does **not** do: store briefs as records
(that is the Postgres brief store), hold source documents (object
storage), checkpoint the graph (Postgres), or replace any read of
the ContextPort on the write path.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import threading
import time
from datetime import date

from pydantic import TypeAdapter

from .config import env_str
from .ontology import CandidateFact, Edge, Episode, Fact, SourceDocument
from .ports import ContextPort

logger = logging.getLogger("zep_context_agent.cache")

KEY_PREFIX = "uwctx:v1"
ENTRY_TTL_SECONDS = 3600
LOCK_TTL_SECONDS = 120


def redis_url() -> str | None:
    return env_str("REDIS_URL")


def version_key(account_id: str) -> str:
    return f"{KEY_PREFIX}:facts-version:{account_id}"


def brief_key(account_id: str, version: int, as_of: date) -> str:
    return f"{KEY_PREFIX}:brief:{account_id}:{version}:{as_of.isoformat()}"


def retrieval_key(account_id: str, version: int, method: str, args: str) -> str:
    digest = hashlib.sha256(args.encode("utf-8")).hexdigest()[:16]
    return f"{KEY_PREFIX}:retrieval:{account_id}:{version}:{method}:{digest}"


def lock_key(account_id: str) -> str:
    return f"{KEY_PREFIX}:lock:ingest:{account_id}"


# ---------------------------------------------------------------------------
# The port
# ---------------------------------------------------------------------------


class CachePort:
    """The cache surface the service depends on. Two implementations:
    :class:`RedisCache` and :class:`InMemoryCache` (test double).

    Implementations never raise for an unreachable backend on reads:
    a failed read is a miss. Coordination failures are explicit:
    :meth:`try_lock` returns ``None`` both for "held by someone
    else" and "backend down" — either way the caller must not
    proceed as if it held the lock.
    """

    name: str = "cache"

    def get(self, key: str) -> bytes | None:
        raise NotImplementedError

    def set(self, key: str, value: bytes, ttl_seconds: int | None = None) -> None:
        raise NotImplementedError

    def delete(self, key: str) -> None:
        raise NotImplementedError

    def incr(self, key: str) -> int | None:
        """Atomically increment a counter; ``None`` when unavailable."""
        raise NotImplementedError

    def try_lock(self, key: str, ttl_seconds: int) -> str | None:
        """Claim ``key``; returns an ownership token, or ``None``."""
        raise NotImplementedError

    def unlock(self, key: str, token: str) -> bool:
        """Release a lock this caller holds (token-checked)."""
        raise NotImplementedError

    def ping(self) -> bool:
        raise NotImplementedError


class InMemoryCache(CachePort):
    """The test double: an honest in-process model of the Redis
    semantics the service relies on (values with TTLs, atomic
    counters, token-checked locks with expiry). Tests construct it
    explicitly — it is never substituted silently at runtime."""

    name = "in-memory"

    def __init__(self) -> None:
        self._values: dict[str, tuple[bytes, float | None]] = {}
        self._locks: dict[str, tuple[str, float]] = {}
        self._mutex = threading.Lock()

    def _live(self, key: str) -> bytes | None:
        item = self._values.get(key)
        if item is None:
            return None
        value, expires = item
        if expires is not None and time.monotonic() > expires:
            self._values.pop(key, None)
            return None
        return value

    def get(self, key: str) -> bytes | None:
        with self._mutex:
            return self._live(key)

    def set(self, key: str, value: bytes, ttl_seconds: int | None = None) -> None:
        expires = (
            time.monotonic() + ttl_seconds if ttl_seconds is not None else None
        )
        with self._mutex:
            self._values[key] = (value, expires)

    def delete(self, key: str) -> None:
        with self._mutex:
            self._values.pop(key, None)

    def incr(self, key: str) -> int | None:
        # Mirrors Redis INCR: the counter lives in the same key space
        # as values, readable by get() — one store, no side channel.
        with self._mutex:
            raw = self._live(key)
            current = int(raw.decode("utf-8")) if raw is not None else 0
            current += 1
            self._values[key] = (str(current).encode("utf-8"), None)
            return current

    def try_lock(self, key: str, ttl_seconds: int) -> str | None:
        with self._mutex:
            held = self._locks.get(key)
            if held is not None and time.monotonic() <= held[1]:
                return None
            token = secrets.token_hex(16)
            self._locks[key] = (token, time.monotonic() + ttl_seconds)
            return token

    def unlock(self, key: str, token: str) -> bool:
        with self._mutex:
            held = self._locks.get(key)
            if held is None or held[0] != token:
                return False
            del self._locks[key]
            return True

    def ping(self) -> bool:
        return True


class RedisCache(CachePort):
    """The Redis adapter (``redis`` package, lazy connection)."""

    name = "redis"

    def __init__(self, url: str) -> None:
        self._url = url
        self._client = None

    def _redis(self):
        if self._client is None:
            import redis

            self._client = redis.Redis.from_url(
                self._url, socket_connect_timeout=2, socket_timeout=2
            )
        return self._client

    def get(self, key: str) -> bytes | None:
        try:
            return self._redis().get(key)
        except Exception:  # cache down == miss, never a failed brief
            logger.warning("cache get failed (treated as miss)", exc_info=True)
            return None

    def set(self, key: str, value: bytes, ttl_seconds: int | None = None) -> None:
        try:
            self._redis().set(key, value, ex=ttl_seconds)
        except Exception:
            logger.warning("cache set failed (skipped)", exc_info=True)

    def delete(self, key: str) -> None:
        try:
            self._redis().delete(key)
        except Exception:
            logger.warning("cache delete failed (skipped)", exc_info=True)

    def incr(self, key: str) -> int | None:
        try:
            return int(self._redis().incr(key))
        except Exception:
            logger.warning("cache incr failed (version unknown)", exc_info=True)
            return None

    def try_lock(self, key: str, ttl_seconds: int) -> str | None:
        token = secrets.token_hex(16)
        try:
            ok = self._redis().set(key, token, nx=True, ex=ttl_seconds)
        except Exception:
            logger.warning("cache lock failed (not acquired)", exc_info=True)
            return None
        return token if ok else None

    def unlock(self, key: str, token: str) -> bool:
        # Release only our own lock: compare-and-delete, atomically.
        script = (
            "if redis.call('get', KEYS[1]) == ARGV[1] then "
            "return redis.call('del', KEYS[1]) else return 0 end"
        )
        try:
            return bool(self._redis().eval(script, 1, key, token))
        except Exception:
            logger.warning("cache unlock failed (lock will expire)", exc_info=True)
            return False

    def ping(self) -> bool:
        try:
            return bool(self._redis().ping())
        except Exception:
            return False


def get_cache() -> CachePort | None:
    """The configured cache, or ``None`` — caching disabled.

    ``REDIS_URL`` unset means disabled, stated as such in readiness;
    there is deliberately no fallback to the in-memory double here.
    """
    url = redis_url()
    if not url:
        return None
    return RedisCache(url)


# ---------------------------------------------------------------------------
# The caching ContextPort decorator
# ---------------------------------------------------------------------------

_FACTS = TypeAdapter(list[Fact])
_EPISODES = TypeAdapter(list[Episode])
_EDGES = TypeAdapter(list[Edge])


class CachingContextPort:
    """A ContextPort decorator adding the retrieval cache + version
    bumps. Wraps whichever adapter is configured (``zep`` or
    ``offline``) without changing its semantics: every read names
    one account, every cached payload is keyed under that account
    and its current facts-version, and every successful ingestion
    bumps the version so nothing written earlier can be served
    again."""

    def __init__(self, delegate: ContextPort, cache: CachePort) -> None:
        self._delegate = delegate
        self._cache = cache

    @property
    def name(self) -> str:
        return self._delegate.name

    # -- version discipline ------------------------------------------------

    def facts_version(self, account_id: str) -> int | None:
        """The account's current facts-version, or ``None`` when the
        cache cannot say (backend down) — callers treat ``None`` as
        "do not serve or store cached content for this read"."""
        raw = self._cache.get(version_key(account_id))
        if raw is None:
            # Distinguish "never ingested" (0) from "cache down":
            # the double and Redis both answer get() reliably when up.
            return 0 if self._cache.ping() else None
        try:
            return int(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    def _bump(self, account_id: str) -> None:
        if self._cache.incr(version_key(account_id)) is None:
            # The version can no longer be trusted: drop the stamp so
            # later reads bypass the cache instead of serving stale
            # entries under a pre-outage version. TTLs bound the rest.
            self._cache.delete(version_key(account_id))

    # -- writes ---------------------------------------------------------------

    def ingest_episode(self, doc: SourceDocument, episode: Episode) -> Episode:
        stored = self._delegate.ingest_episode(doc, episode)
        self._bump(episode.account_id)
        return stored

    # -- the review gate --------------------------------------------------
    # Candidate reads are never cached: review state changes by a
    # human action outside ingestion, and a cached queue would
    # show the desk a decision that was already taken. A
    # confirmation, though, changes the account's facts — so it
    # bumps the facts-version exactly like an ingestion, orphaning
    # every brief and retrieval entry written before it.

    def get_candidates(
        self, account_id: str, state: str | None = None
    ) -> list[CandidateFact]:
        return self._delegate.get_candidates(account_id, state)

    def confirm_candidate(
        self, account_id: str, candidate_id: str, *, actor: str, at
    ) -> Fact:
        fact = self._delegate.confirm_candidate(
            account_id, candidate_id, actor=actor, at=at
        )
        self._bump(account_id)
        return fact

    def reject_candidate(
        self, account_id: str, candidate_id: str, *, actor: str,
        reason: str, at,
    ) -> CandidateFact:
        return self._delegate.reject_candidate(
            account_id, candidate_id, actor=actor, reason=reason, at=at
        )

    # -- cached reads -----------------------------------------------------------

    def _read(self, account_id: str, method: str, args: str, adapter, fetch):
        version = self.facts_version(account_id)
        if version is None:
            return fetch()
        key = retrieval_key(account_id, version, method, args)
        raw = self._cache.get(key)
        if raw is not None:
            try:
                return adapter.validate_json(raw.decode("utf-8"))
            except Exception:
                logger.warning("cached payload unreadable; refetching")
        result = fetch()
        try:
            payload = adapter.dump_json(result)
            self._cache.set(key, payload, ttl_seconds=ENTRY_TTL_SECONDS)
        except Exception:
            logger.warning("cache store failed (skipped)", exc_info=True)
        return result

    def get_facts(self, account_id: str) -> list[Fact]:
        return self._read(
            account_id, "get_facts", "", _FACTS,
            lambda: self._delegate.get_facts(account_id),
        )

    def get_episodes(self, account_id: str) -> list[Episode]:
        return self._read(
            account_id, "get_episodes", "", _EPISODES,
            lambda: self._delegate.get_episodes(account_id),
        )

    def get_edges(self, account_id: str) -> list[Edge]:
        return self._read(
            account_id, "get_edges", "", _EDGES,
            lambda: self._delegate.get_edges(account_id),
        )

    def search_facts(self, account_id: str, query: str, limit: int = 10) -> list[Fact]:
        return self._read(
            account_id, "search_facts", json.dumps([query, limit]), _FACTS,
            lambda: self._delegate.search_facts(account_id, query, limit),
        )

    def get_context_block(self, account_id: str, as_of: date) -> str | None:
        version = self.facts_version(account_id)
        if version is None:
            return self._delegate.get_context_block(account_id, as_of)
        key = retrieval_key(account_id, version, "context_block", as_of.isoformat())
        raw = self._cache.get(key)
        if raw is not None:
            return raw.decode("utf-8")
        block = self._delegate.get_context_block(account_id, as_of)
        if block is not None:
            self._cache.set(
                key, block.encode("utf-8"), ttl_seconds=ENTRY_TTL_SECONDS
            )
        return block
