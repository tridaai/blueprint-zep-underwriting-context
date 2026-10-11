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

**The citation index is a store.** The episode payloads the cited
layer reads from are persisted in the blueprint's Postgres
(`citation_index`, migration 0003 — :mod:`citations`): one record
per episode carrying the episode plus the facts and edges it
contributed. Both profiles write through it at ingestion — the
offline adapter directly, the zep adapter for its payload index —
and an adapter hydrates an account's state from it on first
touch, so a process restart re-reads the cited layer from the
database instead of losing it. With no `DATABASE_URL` there is
no index: the adapters keep their in-process form (the
test-double mode), and `/readiness` reports which one is live
(`citation_index: postgres | in-memory test double |
in-process only`).

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

## Brief diffs — what changed since the last brief

Briefs are the record, so a revisit can be answered from the
record. At verify time, each stored brief is compared against a
baseline brief for the same account and the comparison is
attached to the new brief as its `diff` (:mod:`diffing`). The
comparison reads the two brief payloads only — fact sets, never
prose — so it is deterministic and involves no model:

- **added** — a (predicate, entity) key current now that was not
  current in the baseline;
- **superseded** — a key whose current value handed off: either
  both briefs carry the key with different values, or the old
  value sits in the new brief's superseded section as *replaced*
  (the entry names the successor the new brief itself records);
- **removed** — a key current in the baseline and current no
  longer: its window closed (*expired*), or it is absent from the
  new brief entirely. Removals are always named — a fact never
  leaves a brief silently;
- **decisions** — the baseline brief's own gate decision (it was
  recorded after that brief was assembled, so it belongs to the
  interval), plus any new `underwriter_decision` fact, deduplicated
  when both name the same decision.

**The baseline rule** is the temporal discipline applied to the
briefs themselves: the baseline is the most recent stored brief
for the account that is not this brief, did not fail verification
(a brief that must not be used cannot anchor a comparison), and
has an `as_of` **not later than** this brief's. A brief never
diffs against a brief that looked further into the future — an
as-of re-assembly dated before an existing brief gets no diff
rather than a change narrative written with borrowed hindsight.
Equal `as_of` is allowed, disambiguated by the store's insertion
order, because re-assembling at the same as-of after new documents
land is the normal desk flow. When no brief qualifies, `diff` is
`None` and every surface — API, text export, console — says
*first brief* instead of showing an empty comparison.

The diff rides the brief record, so the cached brief payload
carries it too: the cache's facts-version bump on ingestion
covers the diff exactly as it covers the facts (a test pins the
pair together), and the gate's resume path preserves it on the
decided brief.

## Staleness — decisions go stale

A gate decision is a statement about the facts as they stood.
When new material lands afterwards, the decision does not change
— but its ground may have. The staleness watch (`staleness.py`)
exists to say so, in code, on a record of its own:

- **Evaluation points.** Every ingestion through the service —
  the ingest endpoint, and the pipeline's ingest step, which
  every assembly traverses — snapshots the account's episode ids
  before and after. Episodes that were not there before are the
  triggers; ingestion is idempotent, so re-ingesting the file
  that was already there yields no triggers and is skipped
  without evaluating. A duplicate arrival cannot flag.
- **The comparison.** When the account's latest brief is
  DECIDED, the account's facts are resolved as of today into a
  transient brief (assembled, never stored) and diffed against
  the decided brief with the same `compute_diff` the brief
  diffs use. The decided brief is read, never written — a test
  pins it byte-identical across a flagging.
- **Materiality** is one function (`material_slice`). A change
  is material when a fact the decided brief carried as current
  was superseded or removed, or a new current fact appears on a
  predicate the brief *used* — one of its current facts'
  predicates, or a predicate it reported missing ("not in the
  file" becoming a value is precisely the news a decision
  wants). Two exclusions: the `underwriter_decision` predicate
  never counts (it is the gate's own bookkeeping — a decision
  must not stale itself over its own memo), and a new fact on a
  predicate the brief never touched does not flag alone (every
  ontology-expected predicate is by construction either
  current or missing in a brief, so "untouched" means outside
  the brief's vocabulary). A quarantined document's assertions
  never become facts, so its arrival cannot flag either.
- **The record.** A material change writes a `StalenessRecord`
  to its own store (migration 0004): the decision it refers to
  (brief, action, actor, time), the trigger episode ids, and
  the material slice. If more material lands while the flag is
  open, its episodes join the trigger list; the slice stays the
  one that raised the flag.
- **Resolution** is the gate, again: re-assemble and record a
  fresh decision, and the service stamps the account's open
  records `resolved_by_brief` with the new brief's id. There is
  deliberately **no dismiss** — a stale flag clears when an
  underwriter has decided on current facts, not when someone
  acknowledges it. (A dismiss-with-reason was considered: it
  would let the queue look clean while a decision still rests
  on superseded facts, which is the failure this watch exists
  to prevent. If a deployment truly needs one, it belongs in
  the carrier's workflow system, not in the flag's semantics.)

The **desk queue** (`GET /desk-queue`, the console's home view,
the CLI's `queue` command) is a read model over briefs and
staleness records: stale decisions first — oldest decision
first, it has rested on wrong facts the longest — then briefs
awaiting the gate (oldest first), then clean decisions (most
recent first), then accounts with no brief. Each stale row
carries the flag's age, its triggers, and the change slice. The
queue decides nothing and mutates nothing.

Evaluation reads the ContextPort and the brief store directly —
never the brief cache — so a flag appears even when the decided
brief was cache-served minutes before the material landed (a
test pins exactly that); the cache's own version bump on
ingestion independently guarantees the stale payload is never
served as current.

## Request-info follow-through — decisions become tracked asks

A `request_info` decision names what the file lacks. The
follow-through (`followthrough.py`) makes that a tracked ask
instead of a note in a decision's reason:

- **Composition.** When the gate records `request_info`, the
  service composes the ask from the decided brief: one item per
  missing predicate — named by its underwriter-terms label
  (`ontology.predicate_label`: "Claims count, last 3 years",
  never the raw predicate) — and one per quarantined document,
  named by title with its quarantine reason. The items are
  stored structured on a `FollowThroughRecord` in its own
  store (migration 0005), and the broker draft is composed from
  them as deterministic text, headed DRAFT and carrying the
  decision's actor, date, and the underwriter's own note. The
  draft is **never sent — there is no send path**; the API
  (`GET /briefs/{id}/follow-through`), the console's brief
  panel, and the CLI's `asks` command show it for the
  underwriter to copy.
- **The refusal.** A brief with no missing predicates and no
  quarantined documents has nothing to ask, and composing its
  ask raises `FollowThroughError`. The *decision* still
  records — the gate is the human's, and the ontology's
  completeness view does not overrule it (an underwriter may
  want something the ontology does not track) — but no record
  opens, and the service appends a `follow_through_refused`
  entry to the decided brief's audit trail naming the reason.
  "We asked the broker" is never a fiction the record tells.
- **Recognition.** Evaluation rides the same ingestion hooks
  as the staleness watch (the service's `ingest`, and the
  pipeline's ingest step). With new episodes as the trigger
  set — so a duplicate arrival evaluates nothing — each open
  record is re-read against one fresh, transient assembly
  (never stored): a predicate item is `received` when the
  predicate stands as a current fact there, and the item names
  the episode the assembly cites for it. A document item is
  `received` when a non-quarantined episode — not the
  quarantined episode itself — carries the requested title:
  the corrected resend, under a new document id. (A resend
  under the *same* document id cannot count: ingestion is
  idempotent by document id, making it indistinguishable from
  a duplicate arrival, and the quarantined episode stands.
  A still-quarantined "resend" satisfies nothing either.)
  Status moves `open` → `partially_received` → `complete`,
  with the completion stamp; per-item states persist across
  waves, so partial answers are visible as they accumulate.
- **The desk.** The queue entry for an account carries the ask
  riding on its latest DECIDED brief — status and received
  count — whatever the row's own state, and sets
  `ready_for_review` when the ask is complete, belongs to the
  latest decided brief, and that brief is still the latest
  brief. Completion auto-assembles and auto-decides nothing;
  the underwriter re-assembles and decides as usual.
- **Independence.** The two watches never write each other.
  The broker's answers typically do both jobs at once — a
  missing predicate becoming a value is material under the
  staleness rule, so completing an ask usually leaves the
  decision flagged STALE until the fresh decision resolves
  it — and an open ask is equally untouched by a fresh
  decision: it closes when the broker's answers are
  recognised, not when the desk moves on. Both records stay
  truthful, separately.

## Time, visible — the timeline and the expiry watch

The temporal rule answers "what is true as of this date".
Two read models in `timeline.py` answer the questions around
it — what does this account's whole history look like, and
what is about to stop being true:

- **The timeline.** One chronology per account, assembled in
  code from the port and the record stores: every fact on
  its validity window (cited; status — `current`,
  `superseded`, `expired`, `scheduled` — is
  `temporal.resolve`'s verdict at the timeline's as-of date,
  not a re-reading of the windows), the episodes that carried
  them, every gate decision, staleness flags raised and
  resolved, and follow-through movement (ask opened, item
  received, ask completed). Same-day ordering is fixed by
  kind — document, fact, decision, watch, ask — so the
  chronology is deterministic and diff-able. It is a pure
  projection: computing it writes nothing, and account
  scoping is by construction (every read names one account;
  the near-identical-name trap is pinned against it in tests
  and in the eval pack). Served at
  `GET /accounts/{id}/timeline`, rendered in the console's
  account view, and printable via the CLI's `timeline`
  command.
- **The expiry watch.** A desk-level list computed over the
  facts `resolve` calls current, plus the ones it reports
  expired. A current fact with a `valid_to` inside the
  horizon — `EXPIRY_HORIZON_DAYS`, default 45 — is
  EXPIRING; per (predicate, entity) key, the last fact in
  force whose window has closed is LAPSED, days remaining
  negative. The successor rule is a *window* rule, the
  temporal rule's own relation: a fact whose successor's
  window covers its `valid_to` (the renewal whose term
  starts the day the old term ends — the seeded dataset's
  convention) is neither expiring nor lapsed, at any date.
  A later fact that starts after a gap does not cover: the
  lapsed row names it (`successor_exists`, with its start),
  because the gap itself was real exposure. Facts a
  successor *replaced* mid-window are history — resolve
  names their successor — never lapses. Day math runs on an
  explicit as-of the caller passes (the surfaces default it
  to today at the service boundary; the eval pins it), so
  the watch is hermetic under test. It observes only: no
  brief's notion of current changes because the watch ran.
  Served at `GET /expiry-watch`, as a view in the console,
  via the CLI's `watch` command, and as a section on each
  desk-queue row — the queue's *ordering* is untouched by
  it; a lapsing policy does not outrank a stale decision,
  the row simply shows both clocks.

## Storage topology

| Concern | Production | Without configuration |
|---|---|---|
| Brief records + decisions | PostgreSQL (`briefs`, JSONB payload, numbered migrations + startup runner) | In-memory store (test double) |
| Citation index | PostgreSQL (`citation_index`, migration 0003) — the episode payloads behind every citation | In-process adapter memory (test-double mode) |
| Staleness records | PostgreSQL (`staleness_records`, migration 0004) — flags on decided briefs, stored apart from the briefs themselves | In-memory store (test double) |
| Follow-through records | PostgreSQL (`follow_through_records`, migration 0005) — request-info asks with items, draft, and per-item state, stored apart from the decided briefs | In-memory store (test double) |
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
  citations.py     the citation index store (Postgres + in-memory double)
  diffing.py       brief diffs: what changed since the previous brief, in code
  staleness.py     the staleness watch + records store + the desk queue
  followthrough.py request-info asks: composition, drafts, recognition, records store
  timeline.py      the account timeline + the expiry watch (pure read models)
  cache.py         CachePort, Redis adapter, in-memory double, caching port decorator
  store.py / db.py brief store (Postgres + migrations) + in-memory double
  checkpoints.py   Postgres checkpointer / MemorySaver / off
  service.py       the one door: accounts, ingest, assemble, decide, readiness
  api.py           FastAPI surface + console     render.py  the text brief export
data/generate_dataset.py   the seeded synthetic universe (40 accounts)
evals/                     the golden pack: correctness, temporal, citations, leakage = 0
```
