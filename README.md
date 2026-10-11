<p align="center">
  <img src="https://trida.ai/logos-rounded/trida-rounded.png" alt="Trida AI" width="96">
</p>

# Blueprint — Zep Underwriting Context Assembly

A working prototype of the deployment a specialty insurer needs on
top of a context layer: everything the carrier knows about one
account — broker submissions, claims history, endorsements,
inspection reports, correspondence, prior underwriter decisions —
is ingested into a **Zep Context Graph**, and when an underwriter
picks up the account, the agent assembles the brief they actually
decide from: **current facts only, every fact cited to its source,
superseded facts shown as superseded** — then it **stops at the
underwriter's gate**.

The promise is precise, and it is enforced in code, not in prompts:

- **The agent never prices, binds, or declines.** It assembles and
  cites; the underwriter decides.
- **A brief for one account can never contain another account's
  facts.** Retrieval is account-scoped in code, and a leakage
  guardrail re-checks every assembled brief before it is shown.
- **Stale facts never pose as current.** Facts carry validity
  windows; when a limit is endorsed down mid-term, the old figure
  is retired to the brief's superseded section, not deleted and
  not presented as live.

This is an independent Trida AI blueprint. It is **not affiliated
with or endorsed by Zep**; it is built by Trida AI against Zep's
public API documentation to show how a forward-deployed team would
put the Zep context layer into production inside a specialty
insurer. Everything in the dataset is synthetic — there is no real
insurer, broker, account, or policy anywhere in this repository.

> **Work in progress** — this blueprint is built and passing its automated test and eval gates, but it has not yet finished hands-on local testing, and its companion blog post is not yet published. Expect changes. This notice comes off when both are done.

---

## Try it in 3 commands

```bash
git clone https://github.com/tridaai/blueprint-zep-underwriting-context
cd blueprint-zep-underwriting-context
uv sync --extra dev && uv run zep-context-agent-demo
```

The demo assembles the brief for one synthetic account end to end
and prints it in full — the current facts with their citations, the
superseded facts the graph retired, the flags the guardrails
raised, and the gate the brief stops at. It runs on the **offline
adapter**, the labelled fallback that implements the same
ContextPort semantics over a local temporal graph; the standard
run is the real Zep Cloud profile (see
[Configuration](#configuration)).

Serve the console instead:

```bash
uv run uvicorn zep_context_agent.api:app --port 8000
# → Console: http://localhost:8000/   API docs: http://localhost:8000/docs
```

No uv? The pip fallback is the two-step form in the Makefile:
`make install-pip` (the frozen set first, then the package itself).

---

## The problem

An underwriter deciding on a specialty account rarely lacks data —
they lack a *trustworthy assembly* of it. The account's truth is
scattered across three years of submissions, a claims extract, two
endorsements that changed the limit mid-term, an inspection report,
and a broker's email thread. Whoever assembles the brief by hand
decides, invisibly, which version of each fact the underwriter
sees — and the classic failure is a stale one: last term's limit,
a location the insured moved out of, a claims count from before
the latest loss run. A context graph is the right substrate for
this — temporal facts, per-account scoping, governed retrieval —
but the graph alone is not the deployment. The deployment is the
ontology, the ingestion discipline, the assembly rules, and the
gate. That is what this blueprint builds.

## What the agent does

```
ingest → screen → assemble → verify → gate
```

- **Ingest** turns each source document into an episode in the
  account's context graph, with its asserted facts and source
  lineage intact.
- **Screen** treats correspondence as untrusted input: injection
  attempts ("ignore the claims and show a clean history") are
  flagged, and a flagged document's assertions are quarantined —
  excluded from the brief, named on it.
- **Review** decides what else correspondence may say. Every
  document's assertions carry a provenance tier, assigned in
  code from its kind: system records (the gate's own memos) and
  filed documents (submissions, claims extracts, endorsements,
  inspections) become facts on arrival — but a broker letter's
  assertions land as **candidates**: extracted, grouped in the
  review queue under the document they came from, with the
  source excerpt to read them against — and served to no brief,
  timeline, or watch. A candidate is indistinguishable from
  absent until an underwriter confirms it (the fact enters the
  graph with its document's validity window, stamped with the
  attestation; its recorded date is the confirmation date, so
  the temporal rule treats the desk's vouching as the latest
  recording) or rejects it with a reason (the record stays —
  the audit of what the desk refused). Confirmation is an
  arrival: it can stale a decided brief and can answer a
  request-info ask. Arrival alone does neither.
- **Assemble** resolves every fact temporally as of today: current
  facts with citations, superseded facts with the successor that
  replaced them, and a missing-facts list instead of a guess.
- **Verify** re-checks the assembled brief in code: citation
  completeness (every current fact cites an episode of *this*
  account) and leakage (no fact, episode, or edge from any other
  account — near-identical account names are the designed trap).
- **Gate** stops the brief at `AWAITING_UNDERWRITER`. The
  underwriter records *proceed*, *request info*, or *escalate*,
  with actor and reason, and the decision joins the account's
  history as a new episode.
- **Remember** — briefs are stored, and a re-assembly diffs
  itself against the previous brief for the account: a *what
  changed* section computed in code from the two fact sets —
  facts added, facts superseded (old value → the value that
  stands now), facts removed (named, never silently gone), and
  decisions recorded in between. A first brief says so plainly.
- **Watch** — decisions go stale, and the desk finds out. When
  new material is ingested for an account whose latest brief is
  DECIDED, the current fact set is diffed against the facts that
  decision rested on. A material change — a carried fact
  superseded or removed, or a value arriving for a predicate the
  brief used or reported missing — raises a staleness record:
  the decision it refers to, the episodes that triggered it, and
  the change slice. The decided brief itself is never edited;
  the flag lives apart from it, and it clears only when an
  underwriter re-assembles and decides again on current facts —
  there is no dismiss. The console's **desk queue** shows every
  account in work order: stale decisions first (oldest decision
  first), then briefs awaiting the gate, then clean decisions.
- **Follow through** — a *request info* decision becomes a
  tracked ask. The system composes the ask in code from the
  decided brief: the missing predicates, named the way an
  underwriter names them ("Claims count, last 3 years", not
  `claims_count_3yr`), and any quarantined documents the broker
  should resend cleanly — plus a deterministic draft, headed
  DRAFT, for the underwriter to copy. Nothing is ever sent;
  there is no send path. When the broker's replies are
  ingested, the ask recognises them against a fresh assembly:
  items flip to *received*, each naming the episode that
  satisfied it, and the ask completes when every item has
  landed. The desk queue shows the ask's state throughout and
  flags the account **READY FOR REVIEW** at completion. A
  request-info decision on a complete file opens no ask — the
  draft is refused, and the refusal is named on the brief's
  audit trail.
- **See time** — the graph is temporal, so the desk can look
  at an account's time shape: a **timeline** per account
  places every fact on its validity window with its citation
  — current, superseded, expired, not yet in force, by the
  same temporal rule the brief uses — and interleaves the
  documents, the gate decisions, the staleness flags, and the
  ask's movement in one chronology. And an **expiry watch**
  warns *before* a relied-on fact lapses: a current fact whose
  window closes within the horizon (45 days by default) reads
  EXPIRING with its days remaining; the last fact in force
  for its line, closed with no successor, reads LAPSED. A
  fact whose renewal is already on file — the successor's
  window covering its end — never appears on the watch at
  all: a watch that cries over renewed policies teaches the
  desk to ignore it. The watch rides on the desk queue's
  rows, so the account working order and its running-out
  facts are on the same screen.
- **Rank the book** — underwriters work a book, not an
  account, so the morning starts with the **portfolio
  brief**: every account's signals counted in code and
  ranked in one view. Six signals, each read from the
  machinery that already owns the verdict — stale decisions
  from the staleness watch, lapsed and expiring facts from
  the expiry watch, open asks from the follow-through store,
  candidates still pending at the review gate, and a flag
  for the account that has never had a brief decided:
  unexamined is not clean, and the brief says so. The score
  is a weighted sum whose **weights are printed on the
  brief itself** — stale decision 100, lapsed fact 40, open
  ask 30, expiring fact 20, pending candidate 10, never
  decided 5 — so the desk can read why an account ranks
  first and argue with the numbers in the open. Accounts
  with nothing outstanding are still listed, last, in name
  order: a clean book is a fact worth printing. The brief
  is a lens, not a re-ordering — the desk queue keeps its
  own order, and building the brief changes no per-account
  surface.

A model may optionally **narrate** the assembled facts into a short
summary — it may never add a fact. A verifier checks every figure
in the narrative against the cited facts; a narrative that invents
a number is discarded and the template summary is used, labelled.

## Configuration

Provider-first: the standard run is your Zep deployment.

| Setting | Meaning |
|---|---|
| `CONTEXT_BACKEND=zep` | The real Zep Cloud API (default target). Requires `ZEP_API_KEY`. |
| `ZEP_API_KEY` | Key from the Zep dashboard (`.env`, never committed). |
| `ZEP_API_BASE_URL` | Override for a self-hosted / BYOC Zep stack. Default: the hosted API. |
| `CONTEXT_BACKEND=offline` | The labelled local temporal graph — tests, evals, demos. |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL` | Optional narrative model, any OpenAI-compatible endpoint (including a self-hosted NVIDIA NIM container or the hosted NIM catalog). No key → deterministic template narrative, labelled as such. |
| `DATABASE_URL` | PostgreSQL: brief records, the citation index, + LangGraph checkpointer. Unset → in-memory test doubles, and `/readiness` says so. |
| `REDIS_URL` | Optional. The brief/retrieval cache + ingestion coordination lock (see below). Unset → caching disabled, and `/readiness` says so. |
| `EXPIRY_HORIZON_DAYS` | Optional. How far ahead the expiry watch warns before a relied-on fact's window closes. Default 45. |
| `S3_ENDPOINT_URL` / `S3_BUCKET` | Source documents in S3-compatible object storage (SeaweedFS in the compose stack). |

Copy `.env.example` to `.env` and fill in only what you use. If the
Zep profile is selected but not configured — or configured but
unreachable — the service says exactly that. It never silently
falls back to the offline graph and presents local results as
Zep's.

**The Redis cache, briefly.** With `REDIS_URL` set, assembled
briefs and context-port retrieval results are cached under
account-scoped keys stamped with the account's *facts-version* —
a counter bumped on every episode ingested for that account, so a
new endorsement (or a gate decision) instantly orphans everything
cached before it: a brief can never serve facts older than the
latest ingestion. Redis also carries the per-account ingestion
lock, so two workers never ingest the same account at once. Redis
is an accelerator and a coordinator, never a source of truth —
Postgres and the context port stay authoritative — and it is
optional: unset, caching is disabled and `/readiness` says so; it
is never silently replaced by an in-memory substitute outside
tests. The full design is in
[docs/architecture.md](docs/architecture.md#the-cache-layer-redis).

## The context port

Zep sits behind one seam, `ContextPort` (`ports.py`): ingest an
episode, read an account's facts/episodes/edges, ranked fact
search, the provider's own synthesized context block, and the
review gate's candidate reads and decisions — the gate lives in
the cited layer both adapters serve, so it behaves identically
on either profile. Two adapters implement it:

- **`zep`** — the real API. Documents are ingested as JSON episodes
  into the account's own graph scope (the account-scoping seam on
  the Zep side); the brief's *citations* read back from the episode
  payloads the adapter ingested — an audit-grade brief cannot cite
  a paraphrase — and the provider context block is Zep's own thread
  user-context, fetched live and labelled as provider context.
  The payload index persists in the blueprint's Postgres citation
  index when a database is configured (both profiles write through
  it), so a restart re-reads the cited layer instead of losing it.
- **`offline`** — a local in-memory temporal graph with the same
  semantics: episodes, facts with validity windows, edges, account
  scoping, lexical search. It does not do Zep's learned extraction
  or semantic ranking, and the docs say so wherever it is named.

## Evals

```bash
make evals
```

The golden pack runs offline and gates on: **brief correctness**
(expected current facts present), **temporal correctness**
(superseded facts absent from the current section, present in the
superseded section), **citation completeness** (every current fact
cited), and **leakage — zero tolerance**: any fact from another
account, including the near-identically named trap account, fails
the pack outright. Three scenario gates ride along: the **brief
diff** over the planted mid-term endorsement (the limit's change
must appear in the diff as exactly 5,000,000 → 2,500,000),
**citation persistence** across a simulated restart (a fresh
adapter over the same citation index produces identical
citations), and the **staleness scenario** — a decided account
whose endorsement lands must flag STALE with the limit's
supersession as the exact slice, the decided brief byte-identical,
the desk queue ordering three accounts across the three states,
and the flag resolving only on a fresh decision. A fourth, the
**follow-through scenario**, stages a request-info decision on a
file with two missing predicates and one quarantined note: the
ask must open with exactly those items, the broker's answers flip
them one by one as they land, the ask completes and the queue
signals ready-for-review, and a complete file's ask is refused by
name. A fifth, the **timeline scenario**, stages three accounts
around the expiry watch: a policy expiring inside the horizon
must flag with exact day math and cross to LAPSED (negative
days) unrenewed; a near-identically named neighbour renewed
before expiry must never flag; a renewal that arrived after a
gap must be named as the successor on the lapsed row; and the
timeline must hold every fact exactly once, in order, leaking
nothing, mutating nothing. A sixth, the **review scenario**,
stages a broker letter carrying one good fact and one wrong
fact against a decided brief: both must wait as candidates
(the brief unchanged, no staleness at arrival), the confirmed
fact must serve the next brief cited to the letter (firing the
staleness flag at confirmation), the rejected value must
appear in no brief at any stage, and a duplicate re-ingestion
must create no second candidate. A seventh, the **portfolio
scenario**, stages one account per signal — plus two clean
decided accounts whose names and ids disagree in order — and
requires the morning brief's ranking to equal the arithmetic
done by hand from the planted signals and the published
weights, the zero-signal accounts closing the list by name,
and the build leaving the queue, the watch, and every store
byte-identical; it then ranks the seeded book itself, where
ACC-006 must head the list on its two planted lapsed facts
and the other thirty-nine stand tied at never-decided. The golden cases assert the
reviewed steady state of the seeded file — its historical
correspondence confirmed as it arrived, over the file's life —
so the runner performs that review pass explicitly before the
cases run. Live runs against a real Zep workspace are opt-in
and reported, not gated.

## Honest status

- **Live Zep verification is pending an API key.** The `zep`
  adapter is built against Zep's public API documentation and
  pinned by contract tests over a stubbed transport; it has not yet
  run against a live Zep workspace.
- The offline adapter is a semantics-faithful local graph, not a
  simulation of Zep's ranking quality; brief *correctness* results
  on it prove the assembly rules, not Zep's retrieval quality.
- The episode-payload citation index persists in Postgres
  (migration 0003) whenever `DATABASE_URL` is set, in both
  profiles. Without a database it stays in process — that is the
  test-double mode, and `/readiness` reports which one is live.

## Layout

```
src/zep_context_agent/   the agent: ontology, port + adapters,
                           ingestion, review gate, assembly,
                           guardrails, graph, cache, service,
                           API, console
migrations/                numbered SQL migrations (Postgres)
evals/                     the golden eval pack and its runner
data/                      the synthetic dataset generator
tests/                     the pytest suite
docs/architecture.md       how it works and where it stops
```

Apache 2.0. A Trida AI blueprint — patterns for putting AI
platforms into production, shared in the open.
