"""The ingestion review gate: correspondence waits for a human.

The graph's raw material arrives as document assertions, and not
all paper is equal. The ontology assigns every document kind a
provenance tier (:func:`ontology.origin_tier_for_kind`):

- **system records** (the gate's own decision memos) and **filed
  documents** (submissions, claims extracts, endorsements,
  inspections) become facts on arrival;
- **correspondence** — broker letters, emails, notes — does not.
  Its assertions land as *candidate facts* (see
  :class:`ontology.CandidateFact`): extracted, stored in the
  cited layer beside the facts, grouped here for review — and
  served by no read path. Briefs, timelines, and the expiry watch
  read facts only, so a pending candidate is indistinguishable
  from absent: the temporal machinery never sees it. The
  injection screen stays upstream of the gate (a quarantined
  document's assertions become neither facts nor candidates), and
  idempotent re-ingestion creates no duplicate candidates (the
  episode already stands, so its assertions are never re-read).

The queue is this module's read model: candidates grouped by
account and document, each with the extracted predicate, value,
and validity window, and the source excerpt it came from (the
document's free text — untrusted, fenced wherever it renders).

A decision is the service's (:meth:`BriefService.review_candidate`):

- **Confirm** — the candidate becomes a fact in the graph,
  keeping its id and its document's validity window. Its
  ``recorded_at`` is the *confirmation* date: the window is the
  document's truth, but the graph records the fact when the desk
  attests it, so temporal resolution's tie-break treats the
  attestation as the latest recording (see :class:`ontology.Fact`).
  The confirming actor is stamped on both the fact and the
  candidate record. Confirmation is also an arrival: the service
  re-runs the staleness and follow-through evaluations with the
  candidate's episode as the trigger, so a confirmed fact can
  stale a decided brief and can answer an open request-info ask —
  at confirmation time, never at arrival time.
- **Reject** — requires a reason. The candidate stays on the
  record as rejected, with actor, reason, and time: the audit of
  what the desk refused. It never becomes a fact, and it cannot
  be decided again.

The gate is a workflow state, not a model feature: candidates
are created by the adapters from structured assertions, exactly
like facts, so the offline and Zep cited layers behave
identically and no provider is involved in holding or admitting
anything.
"""

from __future__ import annotations

from .guardrails import fence_text
from .ontology import Account, CandidateFact, predicate_label
from .ports import ContextPort
from .schemas import ReviewGroup


def build_review_queue(
    port: ContextPort,
    accounts: dict[str, Account],
    account_id: str | None = None,
) -> list[ReviewGroup]:
    """The review queue: candidates grouped by (account, document).

    Groups are ordered by account, then by the document's
    received date; candidates inside a group keep ingestion
    order (assertion order within the document). Groups with
    pending candidates sort before fully decided groups for the
    same account, so the desk's open work leads."""
    ids = [account_id] if account_id is not None else sorted(accounts)
    groups: list[ReviewGroup] = []
    for aid in ids:
        account = accounts.get(aid)
        if account is None:
            continue
        by_episode: dict[str, ReviewGroup] = {}
        order: list[str] = []
        for candidate in port.get_candidates(aid):
            group = by_episode.get(candidate.episode_id)
            if group is None:
                group = ReviewGroup(
                    account_id=aid,
                    account_name=account.display_name,
                    episode_id=candidate.episode_id,
                    doc_id=candidate.doc_id,
                    doc_title=candidate.doc_title,
                    document_received_at=candidate.document_received_at,
                    excerpt=candidate.excerpt,
                )
                by_episode[candidate.episode_id] = group
                order.append(candidate.episode_id)
            group.candidates.append(candidate)
        for episode_id in order:
            group = by_episode[episode_id]
            group.pending_count = sum(
                1 for c in group.candidates if c.state == "pending"
            )
            groups.append(group)
    groups.sort(
        key=lambda g: (
            g.account_id,
            0 if g.pending_count else 1,
            g.document_received_at,
            g.episode_id,
        )
    )
    return groups


def _value_text(candidate: CandidateFact) -> str:
    unit = f" {candidate.unit}" if candidate.unit else ""
    window = f"from {candidate.valid_from.isoformat()}"
    if candidate.valid_to is not None:
        window += f" to {candidate.valid_to.isoformat()}"
    scope = f" [{candidate.entity_ref}]" if candidate.entity_ref else ""
    return (
        f"{predicate_label(candidate.predicate)}: "
        f"{candidate.value}{unit}{scope} — valid {window}"
    )


def render_review_queue_text(groups: list[ReviewGroup]) -> str:
    """The queue as text — the CLI's view, from the same groups
    the API serves as JSON."""
    pending = sum(g.pending_count for g in groups)
    total = sum(len(g.candidates) for g in groups)
    lines = [
        f"REVIEW QUEUE — {pending} pending of {total} candidate(s) "
        f"across {len(groups)} document(s)",
        "",
    ]
    if not groups:
        lines.append(
            "No correspondence assertions are waiting: every "
            "candidate has been decided, or none has arrived."
        )
        return "\n".join(lines) + "\n"
    for group in groups:
        lines.append(
            f"{group.account_id}  {group.account_name} — "
            f"\"{group.doc_title}\" ({group.doc_id}, received "
            f"{group.document_received_at.isoformat()})"
        )
        if group.excerpt:
            lines.append("  Source excerpt (untrusted document text):")
            for line in fence_text(group.excerpt).splitlines():
                lines.append(f"    {line}")
        for candidate in group.candidates:
            if candidate.state == "pending":
                mark, state = "[ ]", "PENDING"
            elif candidate.state == "confirmed":
                mark = "[x]"
                state = (
                    f"CONFIRMED by {candidate.review_actor} — "
                    f"recorded {candidate.fact_recorded_at}"
                )
            else:
                mark = "[-]"
                state = (
                    f"REJECTED by {candidate.review_actor} — "
                    f"{candidate.review_reason}"
                )
            lines.append(
                f"  {mark} {candidate.candidate_id}  "
                f"{_value_text(candidate)} — {state}"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
