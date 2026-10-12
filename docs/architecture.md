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

One clock rule stands under all of this: the system's stamps
(decision times, review attestations) are taken in UTC, so the
default `as_of` — "today" — is read in UTC too
(`temporal.today()`). Reading the host-local date instead lets
the two disagree (on a host west of UTC, every evening), and a
decision recorded "now" would carry a `valid_from` later than
the default `as_of`: the future-fact exclusion above would drop
the desk's own decision out of the next brief.

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

## The review gate — correspondence waits for a human

The injection screen answers "is this document trying to
manipulate the reader?" A quieter question sits behind it: even
honest broker correspondence is a free-text channel, and its
assertions are the least reliable paper in the file. Round 6
holds them to a review gate (`review.py`, with the machinery in
the adapters and the citation index).

**Provenance tiers, in code.** Every document kind maps to one
tier in `ontology.origin_tier_for_kind`, and every fact carries
its tier as `origin`:

| Tier | Kinds | Entry |
|---|---|---|
| `system_record` | `decision_memo` | On arrival — the system does not review its own records |
| `filed_document` | `submission`, `claims_extract`, `endorsement`, `inspection` | On arrival — structured extracts of filed paper |
| `correspondence` | `correspondence` | **Only by confirmation** — assertions land as candidates |

An unlisted kind defaults to filed paper: the gated tier is
chosen by the ontology, never fallen into.

**Candidates.** A correspondence assertion becomes a
`CandidateFact`: the extracted predicate, value, and validity
window, the asserting document and its received date, and the
document's free text as the source excerpt. Candidates live in
the cited layer — the adapters hold them beside the facts, and
the citation index persists them (migration 0006, the
`candidate_facts` table) so the queue survives a restart like
everything else the desk relies on. But no read path serves
them: assembly, the timeline, and the expiry watch read facts
only, so a pending candidate is indistinguishable from absent —
the temporal machinery never sees it. The episode itself is on
the record (arrival is a fact of the file, as with a quarantined
document); only its assertions wait. The screen stays upstream:
a quarantined document's assertions become neither facts nor
candidates, and ingestion idempotency (episodes keyed by
document) means a re-ingested letter creates no second
candidate.

**Decisions.** The service's `review_candidate` is the only
door, mirroring the gate's discipline: a named actor is
required; a rejection additionally requires a reason, and the
rejected candidate stays on the record with both — the audit of
what the desk refused, never a silent deletion. A decided
candidate cannot be decided again.

**Confirmation and `recorded_at`.** A confirmed candidate
becomes a fact keeping its id and its document's validity
window — the window is the document's truth. Its `recorded_at`,
though, is the **confirmation date**, not the document's
received date: the graph did not record the fact until the desk
attested it, and the document's own date is not hidden (it
stays on the episode and the candidate record). The choice
matters exactly where `temporal.resolve` breaks ties — equal
windows compete on `recorded_at`. With the document's date, a
letter confirmed late would silently lose to filed paper that
arrived while it waited, and the underwriter's confirmation
would change nothing: a trap. With the confirmation date, the
attestation competes as the latest recording — the desk vouched
for the value knowing what else was on file, and the fact that
wins is the one a human most recently stood behind. The fact
carries `confirmed_by` / `confirmed_at` so a brief reader can
see it entered by review, and the cache decorator bumps the
account's facts-version on confirmation, exactly as it does on
ingestion.

**Confirmation is an arrival; arrival is not.** A pending
candidate's ingestion runs neither watch — no brief could see
it, so nothing it carries can be material yet. Confirmation
re-runs the staleness evaluation and the follow-through
recognition with the candidate's episode as the trigger: a
confirmed fact can stale a decided brief, and a confirmed
broker reply can answer an open request-info ask — at
confirmation time, pinned by tests and by the eval scenario.

**On the Zep profile** the gate governs the cited layer exactly
as it does offline: the episode payload goes to Zep at
ingestion (Zep's server-side graph is Zep's, as with
quarantine), but no brief cites a correspondence assertion
until it is confirmed in the index the briefs read.

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

## The morning portfolio brief — the book, ranked

The desk queue orders the work; it does not summarise the
book. `portfolio.py` is the cross-account projection that
does: per account, six signal counts, each read from the
machinery that owns the verdict — `staleness.open_records`
for stale decisions, `followthrough.open_records` for open
asks, `timeline.expiry_watch_for_account` for the LAPSED /
EXPIRING split (the temporal rule's currency, the watch's
successor discipline, the caller's horizon), the port's
candidate read for the review gate's pending count, and
`staleness.latest_decided_brief` for the never-decided flag.
No signal is re-implemented here; the module is composition,
plus the one piece of arithmetic the round adds:

- **The score.** `PORTFOLIO_WEIGHTS` — authored data in the
  module, echoed onto every brief the projection returns:
  stale decision 100, lapsed fact 40, open ask 30, expiring
  fact 20, pending candidate 10, no decided brief 5. The
  order follows the desk queue's own precedence (a decision
  on superseded facts is the first work of the day; a lapse
  is exposure already open; an ask is the broker's silence;
  an expiry is a clock; candidates are the desk's own
  backlog; never-decided is a standing nudge). Rows carry
  their counts beside their score, so the ranking is
  auditable without recomputing it.
- **The order.** Score descending, ties by account name
  then id — total and stable. A zero score is reachable
  only by a decided account with nothing outstanding, so
  clean accounts close the list in name order, listed
  rather than dropped: the brief's claim "the book is
  clean" requires seeing the clean rows.
- **A lens, not a re-ordering.** The brief does not feed
  the desk queue and changes no per-account surface: the
  queue, the watch, and the stores read byte-identical
  before and after a build, pinned in tests and in the
  eval pack's portfolio scenario — which also pins the
  seeded book's ranking (ACC-006 first on its two planted
  lapsed facts; the undecided rest tied in name order).
  Like the timeline and the watch, the projection is pure
  and hermetic: as-of and horizon are the caller's, the
  surfaces default them at the boundary.
  Served at `GET /portfolio`, as a view in the console,
  via the CLI's `portfolio` command, and printed by the
  demo over its own world — where the one decided account
  closes the list at zero and the unopened thirty-nine
  stand at never-decided.

## Support grading — the brief's soft spots

Every fact on the brief is cited, and citations are not
equal: a fact from the gate's own decision memo, one from a
filed inspection, and one confirmed out of a broker's letter
carry different weight, and until now the brief treated them
identically. `support.py` grades each *current* fact at
assembly, in code, from provenance the graph already carries:

- **The inputs.** The citing document's origin tier
  (`ontology.origin_tier_for_kind` — the review gate's tiers,
  reused, never redefined); the number of *independent
  supporting documents* — distinct `doc_id`s whose assertions
  for the fact's key cover the as-of date and agree with the
  resolved value, the citing document included; and the
  *contest* — whether the temporal resolution displaced an
  earlier, different value for the key. Independence is
  counted in documents, never episodes: ingestion is
  idempotent per document, and one episode per document
  means a re-ingestion cannot inflate support. Superseded
  statuses are read off `temporal.resolve`'s own pairing —
  the grade re-implements no temporality. A fact whose
  window has closed neither supports nor contests; neither
  does a future one. Quarantined documents and unconfirmed
  candidates are not facts, so they can do neither either —
  the planted injection asserting a claims count of zero
  leaves the filed count uncontested.
- **The grades.** `STRONG` — the system's own record, or
  filed paper corroborated by a second document.
  `STANDARD` — a single filed document, or a broker's
  letter with corroboration (correspondence caps there: a
  letter never grades STRONG, however corroborated).
  `THIN` — a single correspondence source. Any grade whose
  value displaced an earlier one is demoted one step
  (STRONG → STANDARD → THIN) and carries the displaced
  value and its document, named. The vocabulary and the
  reasons are authored in `support.py`, the reasons ride
  the grade, and the legend is printed on the brief itself,
  so no reader needs the code to interpret a grade.
- **The surfaces.** The grade rides `CitedFact.support`;
  the text brief tags each current fact's line and adds a
  **soft spots** section listing the THIN facts and only
  those — predicate, value, why, the replaced value, the
  citation — or one line saying none. The desk queue
  carries the account's thin count beside the expiry watch
  (computed under the same condition — port access; `None`,
  not zero, without it), and the portfolio row carries it
  too. On the portfolio the count is displayed and *only*
  displayed: it has no entry in `PORTFOLIO_WEIGHTS`, enters
  no score, and cannot move a row. The weights are
  published data; if the desk ever wants thin support to
  rank, that is a separate deliberate change to the
  weights, argued in the open — not a side effect of a
  count. The queue's order likewise never reads it.
- **No new state, no decision impact.** Grades are derived
  at assembly from facts already stored; there is no
  migration, because briefs are whole JSONB payloads and
  `CitedFact.support` defaults to absent, so briefs written
  before grading parse unchanged and render an honest
  "not graded" instead of a verdict nobody computed. A
  grade change therefore never makes a decided brief look
  edited: the decided record is byte-identical, the upgrade
  shows only on the fresh brief, and the brief diff — which
  compares values, not grades — is unmoved. No gate reads a
  grade: the staleness diff, the follow-through matcher,
  and the decision path are untouched, and a brief full of
  thin facts decides in exactly the same shape as a strong
  one. Pinned in `tests/test_support.py` and the eval
  pack's support scenario, which derives ACC-001's whole
  grade map by hand from its planted documents.

## The renewal chase — the watch learns to ask

The expiry watch is a warning with no verb attached: it
names the relied-on facts whose windows are closing, and
then the desk is on its own — notice the row, remember the
broker, compose the ask. The follow-through machinery has
the verb, but it only ever asked off a gate decision, about
predicates the file *lacked*. The renewal chase
(`chases.py`) is the join, and it is deliberately small:
a projection, a send, and no new machinery anywhere else.

- **A projection, not a record.** Chase rows are computed
  in code from three existing sources — the expiry watch's
  rows (the only expiry opinion in the system; currency is
  still `temporal.resolve`'s), the follow-through store,
  and the brief store — under the same pure-projection
  discipline as the timeline and the portfolio brief:
  computing the rows writes nothing, and two computations
  over the same state agree. The only durable artifact a
  chase ever creates is the follow-through ask its send
  opens, which rides migration 0005's table: the record's
  `origin` (`decision` | `renewal_chase`) and the renewal
  items' chased-fact fields are payload data on the JSONB
  record, with defaults that keep pre-chase payloads valid.
  **No migration** — there is no chase table because there
  is no chase state the projection cannot recompute.
- **One row per (account, predicate), five states.** The
  dedupe key is the account-and-predicate pair — the key
  asks themselves use, since a follow-through item names
  its predicate — so all of a predicate's expiring facts
  (several policies, one predicate) ride one row and one
  ask. States resolve in a fixed order. **ASKED**: a chase
  ask for the predicate is open. **LAPSED_UNSENT**: flagged
  facts exist, at least one lapsed, no chase ever sent —
  the failure named, not aged out; the state outranks
  WAITING_ON_BRIEF because the lapse is the news, and the
  send still works from it (late is not never).
  **WAITING_ON_BRIEF**: facts are expiring but the account
  has no brief. **OPEN**: expiring, brief on file, sendable.
  **SATISFIED**: a chase ask completed and nothing under
  the predicate is flagged — the row names the document
  that satisfied it, read off the items' recorded episodes.
  A fact renewed without any chase simply leaves the watch,
  and its unsent row leaves with it: the projection never
  pretends a chase happened that didn't.
- **The dedupe.** An ask *covers* a predicate while it is
  open and carries an item naming it. A predicate an open
  request-info ask covers gets no chase row — the broker is
  already being asked about that line of the file, and two
  asks for one predicate is exactly the double-ask the
  round exists to prevent. The suppression applies to the
  unsent states only: an ASKED or SATISFIED row is the
  chase's own live work or history and stays visible even
  if a decision ask opens later. The send enforces the same
  rule a second time (rows are a view; the send is the
  act), refusing by name with the covering ask's id.
- **The send rides round 4, unchanged.**
  `send_renewal_chase` is a named human act (an actor is
  required, as at every gate here). It composes renewal
  items from the flagged facts — one per fact, carrying
  the chased fact's id, value, entity, source document,
  and expiry — and opens the record on the account's
  **latest brief** through `open_renewal_ask`. The brief
  attachment is the honest precondition the survey found:
  asks live on briefs (`open_follow_through` refuses a
  brief with no decision behind its draft; the chase has
  its own drafting, but not its own store), so an account
  with no brief gets the WAITING_ON_BRIEF row and a named
  refusal — there is no brief-less ask, and the chase
  invents none. The draft is composed in code in
  follow-through's idiom: DRAFT-headed, each line naming
  the fact, its value, the document it stands on, and the
  day its term ends, asking for the renewal document that
  restates it. Nothing is sent; there is no send path, as
  with every ask in this system. The decided brief the ask
  attaches to is read, never written.
- **Recognition is the successor rule, once.** A renewal
  item cannot use the predicate item's recognition — the
  expiring fact is *already* a current fact, so presence
  would satisfy the chase with the very fact being chased.
  Instead the follow-through evaluation answers a renewal
  item when a successor fact's window **covers the chased
  fact's end** — `timeline.covering_successor`, the one
  successor relation, exposed for this read and never
  re-derived. From there the path is entirely the existing
  one: the renewal's ingestion triggers the evaluation,
  the item flips naming the renewal's episode, the record
  completes when every item lands, the fresh fact
  supersedes in the graph, the watch row clears (the same
  covering relation), and this projection reads the
  completed ask as SATISFIED. A renewal that starts after
  a gap does not cover, so it does not satisfy: the item
  stays open and the watch names the lapse with its
  successor — the gap was real exposure, and both surfaces
  say so in their own words.
- **Surfaces, under the standing disciplines.** The desk
  queue annotates each account's row with its chases
  (predicate, state, clock) computed alongside the watch;
  the queue's order never reads them — pinned by building
  the queue with and without the annotation inputs and
  comparing orders. The portfolio brief counts **open
  chases** per account (every state but SATISFIED) on the
  row beside the thin-fact count, under exactly that
  discipline: no entry in `PORTFOLIO_WEIGHTS`, no score,
  no ranking movement — if the desk ever wants chases to
  rank, the weights are published data and changing them
  is a separate, deliberate act. The console shows the
  rows on the queue and a chase card on the account (with
  the send, for the states that can send), the timeline
  labels a chase ask's opening as the chase it was, and
  the API serves `GET /chases` and the send
  (`POST /accounts/{id}/chases/{predicate}/send`), with
  refusals as 409s naming their cause. Pinned in
  `tests/test_chases.py` and the eval pack's renewal chase
  scenario, which stages the lifecycle, the dedupe, the
  no-brief refusal, and the lapse — then checks the seeded
  book, where the desk's whole chase list is ACC-006's two
  planted lapsed facts.

## Renewal briefs — decide against the last decision

The brief diff answers *what changed since the last brief*.
The question a returning account actually poses is sharper:
*what has changed since we decided?* The two anchors diverge
exactly when the work is interesting — a brief assembled and
never decided, a decision recorded between two assemblies —
and in those worlds the diff's baseline is the wrong one.
Renewal assembly (`renewals.py`) is the mode that anchors to
the decision. It is a mode, not a behaviour: requested
explicitly (`mode` on assembly — the API field, the CLI flag,
the console button), and standard assembly is byte-identical
whether or not the account has renewal history.

- **The baseline is a decision, found by the watch's own
  rule.** The most recent brief for the account with status
  `DECIDED` and a recorded decision — the gate's three
  actions, `proceed` / `request_info` / `escalate`, being the
  decision record's whole vocabulary — whose as-of is not
  later than the renewal's. The as-of bound is the diff's
  no-borrowed-hindsight discipline applied to decisions: a
  renewal never compares against a decision dated after it.
  The renewal carries the baseline quoted from that brief's
  record: its id and as-of, the action, the decider, the
  rationale, the moment. The baseline brief is read, never
  written — the quotation lives on the renewal.
- **The ledger is the diff machinery, renamed to the
  decision's terms.** `diffing.compute_diff` runs over the
  baseline brief's payload and the renewal's: **new** facts,
  **superseded** facts (the value the decision rested on,
  the value standing now, both documents cited), **lapsed**
  facts (the diff's removals — facts current at the decision
  whose windows have since closed with no successor in
  force, the temporal rule's `expired` verdict, plus any
  fact gone from the file entirely), and the unchanged facts
  **counted, not listed**: the ledger is about change, and a
  renewal where nothing moved says so in one line. One
  exclusion, on the staleness watch's precedent: the
  baseline decision's own echo — the `underwriter_decision`
  fact its decision memo planted, keyed to the baseline
  brief — is not news about the account. It is the baseline,
  and it appears in the baseline block, never as a "new
  fact" in the ledger.
- **Open business is inherited by reference, never by
  touch.** At assembly the renewal reads the follow-through
  store's open records and the staleness store's open
  records for the account and names them with their states:
  the ask still waiting on the broker (either origin — a
  decision's request-info ask or a chase's), and the flags
  still open. Assembling a renewal completes no item,
  resolves no flag, and writes to neither store; the section
  is a snapshot on the brief, and the records' own pages
  remain the only place their state changes.
- **The refusal is part of the design.** No DECIDED brief on
  or before the requested date — never briefed, briefed but
  never decided, or decided only later — and renewal
  assembly raises `RenewalError` naming which world the
  account is in. The check runs at the service door, before
  any pipeline work: a refused renewal assembles nothing and
  stores nothing, and is never quietly served as a standard
  brief. (The pipeline re-finds the baseline when it
  attaches the section; the two reads agree within one
  assembly.)
- **Attachment rides the verify step, beside the diff.**
  The section is computed in `step_verify`, after
  `attach_diff`, and re-saved on the brief — so the payload
  the gate's checkpoint holds already carries it, and the
  decision recorded on resume lands on a brief that names
  its baseline. The audit chain account → decision →
  renewal → decision is therefore walkable from the brief
  records alone: each renewal states its baseline's brief
  id and decision, and carries its own decision when the
  gate acts. The renewal brief also gains an audit entry
  (`renewal_baseline`) naming the decision it was assembled
  against.
- **Decision discipline is deliberately unoriginal.** The
  renewal's decision is a fresh decision through the
  existing gate; the baseline decision is never carried,
  copied, or implied. Staleness needs no renewal case: the
  watch already keys on *the latest decided brief*, so the
  baseline's open flags resolve by the renewal's decision
  (the one resolution path there is), a material change
  after it raises a record against the renewal brief, and
  the baseline's records keep their history — stamped
  resolved, never reopened, never edited.
- **No new storage, and the cache stays honest.** The
  section is an optional field on the brief payload
  (`renewal`, `None` on every standard brief and on briefs
  stored before the mode existed) — the support grade's
  precedent on the same JSONB record, so **no migration**.
  The brief payload cache is keyed (account, facts-version,
  as-of) with no mode dimension, and a renewal is a
  different payload for that key: renewal assemblies bypass
  the payload cache on read *and* write, so neither mode
  can be served the other's brief. The port-level retrieval
  cache is keyed on facts and serves both modes alike.
- **Surfaces, under the standing disciplines.** The API
  takes the mode on `POST /briefs` (a refusal is a 409
  naming its cause) and the brief payload carries the
  section; the text export renders baseline, ledger, and
  inherited business ahead of the facts they frame. The
  console adds the renewal badge, the baseline/ledger/
  inherited card, and the assemble-as-renewal action. The
  desk queue annotates an account whose latest brief is a
  renewal (`brief_is_renewal`) — display only; the order is
  built exactly as before, pinned by a twin-world comparison
  (identical worlds diverging only in the second assembly's
  mode produce identical queue orders). The portfolio brief
  takes **no renewal signal**, deliberately: every published
  weight ranks a state of the account's *file* — a stale
  decision, a lapse, an open ask — while renewal-ness is a
  state of the desk's *paperwork*, how the underwriter chose
  to work the account. Counting it would rank workflow
  choices as if they were exposure; if that ever changes,
  the weights are published data and the change is a
  separate, deliberate act. Pinned in
  `tests/test_renewals.py` and the eval pack's renewal brief
  scenario: a staged decision, the wave that moves it, a
  ledger asserted entry by entry against the staged change,
  the inherited ask and flag untouched by assembly, the
  renewal decision resolving and then staling on its own
  record — the baseline brief byte-identical at every step —
  and, on the seeded book, a renewal against a fresh
  decision with an empty ledger.

## The broker book — the book, grouped by broker

`brokers.py` is the per-broker projection: the whole book
grouped by placing broker, answering what each broker owes
the desk and how their accounts stand. Every earlier
cross-account surface groups by account; the desk's asks,
though, are in substance asks *of* brokers — each
follow-through draft is addressed to "the broker of record"
for its account — so the owed-work question is a broker
question, and this is its read model.

- **Identity: the account's own paper.** The grouping key
  is the ontology's `placed_by` edge (insured_account ->
  broker), read from the account records' documents — the
  same place the portfolio brief reads account names — so
  the whole book groups whether or not an account has been
  ingested. The identical assertion lands in the graph at
  ingestion; the book invents no second source for it.
  Documents are read in arrival order and the most recent
  assertion wins: the broker *of record* is the latest
  paper's broker. Only edges the account itself sources
  count (a submission's `submitted_by` edge names a sender,
  not a placing). An account whose paper names no broker
  groups under an explicit unattributed row (`broker_id`
  is null): grouped and counted like any other row, never
  silently dropped.
- **Figures: the surfaces, summed.** No per-account
  verdict is computed here. The counts are
  `portfolio.account_signals` aggregated over the broker's
  accounts — staleness from the watch's open records,
  expiring and lapsed from the expiry watch, pending
  candidates from the review gate — plus the decided mix
  off `staleness.latest_decided_brief` (each account counts
  once, under its latest decided brief's action, or as
  undecided) and the open-chase count read from the chase
  projection exactly as the portfolio row reads it. The
  owed listing is the follow-through store's open records
  with their still-outstanding items (received items are
  answered business and are not listed). If a figure here
  ever disagreed with the per-account surface it sums, the
  surface would be right and this module the bug; the
  composition is pinned by test and by the eval pack's
  broker book scenario, both of which assert the book
  against the portfolio brief over the same world.
- **A ledger, not a ranking.** No score is computed and
  no weights exist — the portfolio brief is the book's
  ranking and keeps that job, untouched by this round.
  The rows carry one published order, because the book is
  a chasing tool: open asks owed descending, stale
  decisions descending, then broker name, then broker id,
  with the unattributed row always last — there is no
  broker to chase there. The rule is printed on the book
  itself (`order_rule`) and pinned.
- **Nothing crosses accounts.** A row aggregates *counts*;
  the only text it carries is an open ask's own outstanding
  items, listed under that ask's own account. Fact values
  and citations never enter the book — they stay on the
  account's brief, where the leakage discipline already
  governs them.
- **Read-only, and a pure projection.** There are no
  broker-level decisions and no broker-level acts: the
  gate decides briefs and the chase machinery sends
  chases, unchanged. Building the book writes nothing —
  the queue, the portfolio brief, the watches, and every
  decided brief are byte-identical around the build, and
  two builds are byte-identical to each other. No
  migration: the book stores nothing. Surfaces: the
  service's `broker_book`, `GET /brokers`, the console's
  Brokers view, the CLI's `brokers` command, and a closing
  beat in the demo, where the decided account's broker
  shows one proceed and nothing owed.

## Storage topology

| Concern | Production | Without configuration |
|---|---|---|
| Brief records + decisions | PostgreSQL (`briefs`, JSONB payload, numbered migrations + startup runner) | In-memory store (test double) |
| Citation index | PostgreSQL (`citation_index`, migration 0003) — the episode payloads behind every citation | In-process adapter memory (test-double mode) |
| Staleness records | PostgreSQL (`staleness_records`, migration 0004) — flags on decided briefs, stored apart from the briefs themselves | In-memory store (test double) |
| Follow-through records | PostgreSQL (`follow_through_records`, migration 0005) — request-info asks and renewal-chase asks with items, draft, and per-item state, stored apart from the briefs they ride on | In-memory store (test double) |
| Review candidates | PostgreSQL (`candidate_facts`, migration 0006) — correspondence assertions awaiting review, beside the citation index that persists them | Adapter memory with the rest of the cited layer |
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
  citations.py     the citation index store (Postgres + in-memory double) + candidate records
  diffing.py       brief diffs: what changed since the previous brief, in code
  staleness.py     the staleness watch + records store + the desk queue
  followthrough.py asks (request-info + renewal chase): composition, drafts, recognition, records store
  review.py        the ingestion review gate: the queue read model + text rendering
  timeline.py      the account timeline + the expiry watch (pure read models)
  chases.py        the renewal chase: watch rows joined to asks — projection, dedupe, the send
  portfolio.py     the morning portfolio brief: signals counted, the book ranked
  brokers.py       the broker book: the book grouped by placing broker — what each broker owes
  support.py       support grading: how well-backed each current fact is (derived at assembly)
  renewals.py      renewal briefs: the baseline decision, the change ledger, inherited open business
  cache.py         CachePort, Redis adapter, in-memory double, caching port decorator
  store.py / db.py brief store (Postgres + migrations) + in-memory double
  checkpoints.py   Postgres checkpointer / MemorySaver / off
  service.py       the one door: accounts, ingest, assemble, decide, readiness
  api.py           FastAPI surface + console     render.py  the text brief export
data/generate_dataset.py   the seeded synthetic universe (40 accounts)
evals/                     the golden pack: correctness, temporal, citations, leakage = 0
```
