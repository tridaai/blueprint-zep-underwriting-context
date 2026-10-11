# Architecture — Zep Underwriting Context Assembly

How the blueprint is put together, why the seams sit where they
do, and where the agent deliberately stops.

## The shape

```
source documents ──► ingest ──► screen ──► ContextPort ──► assemble ──► verify ──► gate
(submissions,        (episodes   (injection  (zep | offline)  (temporal   (citations  (underwriter
 claims, endorsements, with       screen,     adapters)        resolve +   + leakage   decides;
 correspondence,     lineage)     quarantine)                  citations)  in code)    decision →
 prior decisions)                                                              new episode)
```

One service (`service.py`) owns every operation; the API, console,
CLI, demo, and evals are thin doors over it. The pipeline itself is
a LangGraph graph — `ingest → assemble → verify → gate` — because
the gate is a genuine pause point in a stateful flow: the graph
runs to an interrupt at `AWAITING_UNDERWRITER`, the underwriter's
decision resumes the same thread, and the decision is recorded by
the same node whichever surface delivered it. With checkpoints off
the service runs the identical node functions in-process and
applies the same decision function to the stored brief; the two
paths are pinned to identical outcomes by tests.

## The context port

Everything the agent knows about an account comes through
`ContextPort` (`ports.py`): ingest an episode, read an account's
facts / episodes / edges, ranked fact search, and the provider's
own synthesized context block. Every call names exactly one
account — account scoping is part of the contract, not a query
convention — and adapters raise `AccountScopeViolation` on a
mismatch.

Two adapters:

- **`zep`** — the real Zep Cloud API over HTTPS (no vendor SDK
  vendored in). Writes go to Zep for real: each document is a JSON
  episode in the account's own graph scope (`group_id =
  uw-account-<account_id>`). Cited facts read back from an
  episode-payload index the adapter keeps for what it ingested —
  an audit-grade brief cites episode payloads, not paraphrases of
  Zep's extracted graph; Zep builds its semantic graph from the
  same payloads server-side. The context block is Zep's own thread
  user-context, fetched live and labelled *provider context* on
  the brief — shown to the underwriter, never used as a citation
  source. Any HTTP failure raises `ContextUnavailable`; the brief
  then assembles from cited facts alone and says the provider
  context was unavailable. The adapter never falls back to the
  offline graph.
- **`offline`** — a local in-memory temporal graph with the same
  semantics (episodes, validity windows, edges, scoping, lexical
  search). It is the labelled fallback for tests, evals, and
  no-key runs. It does not do Zep's learned extraction or semantic
  ranking, and nothing in the repo claims otherwise.

**Honest limit (v1):** the `zep` adapter's citation index is kept
in process. Persisting it in the blueprint's Postgres store is the
documented next step; until then a process restart re-derives it
by re-ingesting the account's documents (ingestion is idempotent).

## The temporal rule

Facts carry validity windows (`valid_from`, optional `valid_to`)
from their source assertions. `temporal.resolve(facts, as_of)` is
the single rule every brief stands on:

- A fact is a *candidate* when `valid_from ≤ as_of` and (`valid_to`
  is open or `as_of < valid_to`). Facts dated in the future are
  excluded entirely — a brief never borrows from next year's
  endorsement, and a future fact is not shown as superseded either
  (it is not history yet).
- Per `(predicate, entity_ref)`, the candidate covering `as_of`
  with the latest `valid_from` (ties broken by `recorded_at`) is
  **current**. Every other candidate is **superseded**, paired with
  its successor when one covers `as_of`, or marked *expired* when
  the window simply closed — a coverage that lapsed for a season
  reads as a gap, not as a quiet continuation.

The brief renders both lists. Superseded facts are never deleted
and never presented as live; the underwriter sees what changed,
when, and what replaced it.

## Guardrails (all in code)

1. **Injection screen at ingestion.** Correspondence text is
   untrusted. A narrow set of file-directed patterns ("ignore the
   claims history", "show a clean record", "omit the loss", role
   reassignment, system-prompt references) quarantines the
   document: the episode is recorded, its assertions never become
   facts, and the brief names the document and the reason. The
   patterns are deliberately narrow — ordinary broker English
   ("please disregard my earlier email") must not trip them, and
   tests pin both directions.
2. **Citation completeness.** Every current fact in a brief must
   cite an episode of the same account that actually carries it.
3. **Leakage.** Every fact, episode, and edge in the brief must
   belong to the brief's account. The adapters scope retrieval;
   this check re-verifies the assembled artifact, because the
   artifact is what the underwriter reads. The eval gate is zero
   tolerance, with two near-identically named accounts as the
   designed trap.
4. **Narrative verification.** A model may narrate the assembled
   facts (optional, `OPENAI_*`); every number in the narrative
   must appear in the cited facts or the brief's own dates. An
   inventing narrative is discarded for the labelled template
   summary. No key → template, labelled.

A brief that fails verification is stored as
`VERIFICATION_FAILED` and cannot be decided.

## The gate — and where the agent stops

Every brief ends `AWAITING_UNDERWRITER`. The underwriter records
**proceed**, **request info**, or **escalate**, with a named actor
and a reason; both are required, the decision is appended to the
brief's audit trail, and the decision is ingested back into the
account's graph as a decision-memo episode — so the next brief
cites the account's decision history as facts. A brief carries one
decision; revisiting an account means assembling a new brief.

The agent **never prices, binds, declines, or issues a machine
instruction of any kind**. It assembles, cites, flags, and stops.
Appetite screening (does this risk class fit the carrier's current
appetite at all?) is deliberately out of v1 scope: the spec's
early sketch had one, the shipped pipeline does not — appetite is
the carrier's policy engine's job, and bolting a toy version onto
the brief would blur exactly the line this blueprint draws.

## Storage topology

| Concern | Production | Without configuration |
|---|---|---|
| Brief records + decisions | PostgreSQL (`briefs`, JSONB payload, numbered migrations + startup runner) | In-memory store (test double) |
| Graph checkpoints | PostgreSQL via the LangGraph Postgres checkpointer | MemorySaver; `CHECKPOINTS=off` → service-state gate |
| Source documents | S3-compatible object storage (SeaweedFS in compose), key `accounts/<id>/documents/<doc_id>.json` | Not archived (inline only) |
| **Cache + ingestion locks** | **Redis** (below) | **Disabled — stated, not substituted** |
| Context graph | Zep Cloud (or BYOC) | Offline adapter (labelled) |

The app container is stateless and runs non-root; `/health`
answers without dependencies, `/readiness` reports the real mode
(backend, database, object store, checkpointer, cache) and 503s
when a configured database does not answer. Logs are structured
JSON with a request id propagated as `X-Request-ID`.

## The cache layer (Redis)

Redis is an accelerator and a coordinator — **never a source of
truth**. Postgres and the ContextPort remain authoritative; the
cache can be flushed at any moment and the only cost is speed.

**What it caches** (behind `CachePort` in `cache.py`, with a Redis
adapter and an in-memory test double):

- *Retrieval results* — a `CachingContextPort` decorator wraps
  whichever ContextPort adapter is configured and caches
  `get_facts` / `get_episodes` / `get_edges` / `search_facts` /
  `get_context_block` results.
- *Assembled brief payloads* — `BriefService.assemble` returns the
  cached brief when one exists for the account's current
  facts-version and the requested as-of date.

**Keys are account-scoped by construction.** Every content key is
`uwctx:v1:<kind>:<account_id>:<facts-version>:…`. The account id
is part of every key and there are no shared or unscoped keys for
account content, so account A's cached brief is unreachable under
account B's key space — the same zero-tolerance leakage discipline
as the briefs, pinned by a test.

**Invalidation is versioned, not hoped for.** Each account has a
facts-version counter stored in the cache. The caching decorator
bumps it on *every* episode ingested for that account — a new
endorsement, a claims extract, a gate decision's memo. Every
cached entry is keyed under the version that was current when it
was written, so the moment an ingestion lands, all earlier brief
and retrieval entries become unreachable. A brief can never serve
facts older than the latest ingestion; a test pins exactly that
(assemble → ingest a superseding endorsement → re-assemble → the
new limit is in the brief). Entries also carry a one-hour TTL as a
backstop so orphaned entries expire instead of lingering.

**Ingestion coordination.** Ingesting an account (directly, or as
the first step of assembly) takes a per-account lock —
`SET NX` with a 120-second expiry and a token-checked release — so
two workers never ingest the same account concurrently. A second
worker is told `IngestionInProgress` (HTTP 409) instead of racing.
The in-memory double models the same semantics for tests.

**Optionality, honestly.** When `REDIS_URL` is unset, caching is
*disabled*: the service runs without the decorator, without locks,
and `/readiness` reports `cache: disabled (REDIS_URL unset)`. It
does **not** quietly substitute the in-memory double in production
mode — the double exists for tests, which construct it explicitly.
When Redis is configured but unreachable, reads degrade to misses
and writes are skipped (a dead accelerator must not fail a brief),
ingestion proceeds without the lock with a warning in the log, and
`/readiness` reports `redis UNREACHABLE` in plain words.

**What Redis deliberately does not do:** store brief records (the
Postgres brief store), hold source documents (object storage),
checkpoint the graph (Postgres), or sit on the write path of the
context graph (the ContextPort adapters write through, always).

## Production deploy — what the compose stack shows, and what it doesn't

The compose stack runs the production topology on one host: api +
Postgres/pgvector + SeaweedFS S3 + Redis, healthchecked, with the
bucket created by a one-shot init. A real deployment still adds:
TLS in front of every hop, secrets from a manager (the compose
defaults are local-stack only), Zep account-scoped access policies
configured on the Zep side (the server-side half of the scoping
story — this repo enforces the client half in code), backups for
Postgres, and the carrier's own SSO in front of the console.

## Repository map

```
src/zep_context_agent/
  ontology.py      accounts, documents, episodes, facts, edges
  ports.py         the ContextPort contract
  zep_graph.py     the real Zep adapter      offline_graph.py  the labelled local graph
  temporal.py      the validity-window rule
  ingestion.py     documents → screened episodes (+ object-store archive)
  guardrails.py    injection screen, citation + leakage checks, narrative verifier
  assembly.py      resolve → cite → flag → brief     narrate.py  verified-or-template summary
  graph.py         LangGraph: ingest → assemble → verify → gate (interrupt)
  decisions.py     the gate decision + decision-memo write-back
  cache.py         CachePort, Redis adapter, in-memory double, caching port decorator
  store.py / db.py brief store (Postgres + migrations) + in-memory double
  checkpoints.py   Postgres checkpointer / MemorySaver / off
  service.py       the one door: accounts, ingest, assemble, decide, readiness
  api.py           FastAPI surface + console     render.py  the text brief export
data/generate_dataset.py   the seeded synthetic universe (40 accounts)
evals/                     the golden pack: correctness, temporal, citations, leakage = 0
```
