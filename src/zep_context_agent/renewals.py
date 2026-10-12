"""Renewal briefs: decide the renewal against the last decision.

A returning account is not a new account. A standard brief
assembles the current facts well, and the round-2 diff says what
changed since the *previous assembly* — but the comparison an
underwriter actually works from is the last **decision**: what
did we decide, on what facts, and what has moved since? When the
previous assembly was never decided, or a decision sits between
two assemblies, the diff's baseline is the wrong anchor. This
module is the renewal assembly mode:

- **Explicit, never silent.** Renewal is a mode requested on
  assembly (``mode="renewal"`` on the service and the API, a
  flag on the CLI). Standard assembly is byte-identical whether
  or not the account has renewal history — the mode changes
  nothing until it is asked for. And it is refused, by name
  (:class:`RenewalError`), when there is nothing to renew
  against: no brief for the account has been DECIDED on or
  before the requested date. A refused renewal assembles and
  stores nothing.
- **The baseline** is the account's most recent DECIDED brief
  dated no later than the renewal — the diff's as-of discipline
  (a comparison never borrows hindsight from a later-dated
  brief), applied to decisions: *decided* means the brief's
  status is ``DECIDED`` and its decision is one of the gate's
  three actions (``proceed`` / ``request_info`` / ``escalate``
  — the decision record's own vocabulary). The renewal carries
  the baseline's identity and its decision quoted from the
  record: id, decided at, by whom, the action, the rationale.
- **The change ledger** is computed in code over the two
  briefs' stored payloads, in the brief diff's idiom
  (:func:`diffing.compute_diff` between the baseline brief and
  the renewal): facts **new** since the decision, facts
  **superseded** (the value the decision rested on handed off
  to the value standing now, both documents cited), facts
  **lapsed** since (current at the decision, not current now —
  the diff's removals, which are the temporal rule's
  expired-with-no-successor verdicts and facts gone from the
  file entirely), and the facts that stand exactly as decided,
  **counted, not listed** — the ledger is about change. One
  exclusion, on the staleness watch's precedent: the baseline
  decision's own echo — the ``underwriter_decision`` fact its
  decision memo planted, keyed to the baseline brief — is not
  news about the account; it is the baseline, and it is shown
  in the baseline block, not the ledger.
- **Open business is inherited, not touched.** The renewal
  names the account's still-open follow-through asks and open
  staleness records, read from the stores that own them, with
  their states. Assembling a renewal closes nothing, answers
  nothing, resolves nothing: an ask still waits on the broker,
  and a staleness flag still clears only the round-3 way — a
  fresh decision, which the renewal's own decision is.
- **Decision discipline is unchanged.** The renewal ends at
  the same gate; its decision is a fresh decision on the new
  brief, recorded by the same machinery. The baseline decision
  is context, never carried or implied — and because the
  renewal brief's own payload names its baseline (brief id +
  decision), the audit chain account → decision → renewal →
  decision is walkable from the records alone. Staleness
  treats a renewal decision exactly like any decision: the
  baseline's open flags resolve by it, and a material change
  after it stales the *renewal* decision on its own record —
  the baseline brief, and the baseline decision's record on
  it, stay byte-identical throughout.

The section is computed at assembly (in the pipeline's verify
step, beside the diff's attachment — so the checkpointed brief
the gate later resumes carries it, and a gate decision can
never overwrite it) and stored on the renewal brief. No new
store, no migration: briefs are stored whole as JSONB payloads,
and ``renewal`` is an optional field on the payload — briefs
stored before the mode existed parse with it ``None``.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from . import diffing
from . import followthrough as follow_mod
from . import staleness as staleness_mod
from .errors import RenewalError
from .schemas import (
    AuditEntry,
    Brief,
    RenewalBaseline,
    RenewalInherited,
    RenewalLedger,
    RenewalOpenAsk,
    RenewalOpenStaleness,
    RenewalSection,
)
from .staleness import DECISION_PREDICATE


def _now() -> datetime:
    return datetime.now(timezone.utc)


def find_baseline(
    brief_store, account_id: str, as_of: date
) -> Brief | None:
    """The renewal baseline for an assembly dated ``as_of``:
    the account's most recent DECIDED brief (a recorded gate
    decision — ``proceed``, ``request_info``, or ``escalate``)
    whose own as-of is not later than the renewal's. The same
    finder the staleness watch uses for "the latest decided
    brief", with the diff's no-borrowed-hindsight discipline
    applied to the date."""
    decided = [
        b
        for b in brief_store.list(account_id)
        if b.status == "DECIDED" and b.decision is not None
        and b.as_of <= as_of
    ]
    return decided[-1] if decided else None


def require_baseline(brief_store, account_id: str, as_of: date) -> Brief:
    """The baseline, or the named refusal. The message says
    which world the account is in — never briefed, briefed but
    never decided, or decided only after the requested date —
    because each has a different next step."""
    baseline = find_baseline(brief_store, account_id, as_of)
    if baseline is not None:
        return baseline
    briefs = brief_store.list(account_id)
    decided = [
        b for b in briefs if b.status == "DECIDED" and b.decision is not None
    ]
    if decided:
        detail = (
            f"its last decision is on brief {decided[-1].brief_id}, "
            f"assembled as of {decided[-1].as_of.isoformat()} — later "
            f"than the requested {as_of.isoformat()}, and a renewal "
            "never compares against a decision dated after it"
        )
    elif briefs:
        detail = (
            "it has briefs but none has been decided — a renewal is "
            "decided against the last decision, and there is none yet"
        )
    else:
        detail = "it has no briefs at all"
    raise RenewalError(
        f"Account {account_id} has nothing to renew against: "
        f"{detail}. Assemble a standard brief and decide it first; "
        "the renewal is the next brief after a decision."
    )


def compute_ledger(baseline: Brief, renewal: Brief) -> RenewalLedger:
    """The change ledger between the baseline brief's payload
    and the renewal's — the brief diff's computation, renamed
    to the decision's terms, with the baseline's own decision
    echo excluded and the unchanged facts counted."""
    diff = diffing.compute_diff(baseline, renewal)
    new_facts = [
        f
        for f in diff.added
        if not (
            f.predicate == DECISION_PREDICATE
            and f.entity_ref == baseline.brief_id
        )
    ]
    base = {
        (f.predicate, f.entity_ref): f for f in baseline.current_facts
    }
    current = {
        (f.predicate, f.entity_ref): f for f in renewal.current_facts
    }
    unchanged = sum(
        1
        for key, old in base.items()
        if key in current
        and (current[key].value, current[key].unit) == (old.value, old.unit)
    )
    return RenewalLedger(
        new_facts=new_facts,
        superseded=diff.superseded,
        lapsed=diff.removed,
        unchanged_count=unchanged,
    )


def collect_inherited(
    *, staleness_store, follow_store, account_id: str
) -> RenewalInherited:
    """The account's open business at assembly time, read from
    the owning stores: asks not yet complete (either origin —
    a decision's request-info ask or a renewal chase's), and
    staleness records not yet resolved. Read-only: the renewal
    names this business; it alters none of it."""
    asks: list[RenewalOpenAsk] = []
    if follow_store is not None:
        for record in follow_mod.open_records(follow_store, account_id):
            asks.append(
                RenewalOpenAsk(
                    record_id=record.record_id,
                    brief_id=record.brief_id,
                    origin=record.origin,
                    status=record.status,
                    received_count=sum(
                        1 for i in record.items if i.status == "received"
                    ),
                    total_count=len(record.items),
                    opened_by=record.decision_actor,
                    opened_at=record.decided_at,
                )
            )
    stale: list[RenewalOpenStaleness] = []
    if staleness_store is not None:
        for record in staleness_mod.open_records(
            staleness_store, account_id
        ):
            stale.append(
                RenewalOpenStaleness(
                    record_id=record.record_id,
                    brief_id=record.brief_id,
                    decision_action=record.decision_action,
                    decision_actor=record.decision_actor,
                    decided_at=record.decided_at,
                    flagged_at=record.flagged_at,
                )
            )
    return RenewalInherited(open_asks=asks, open_staleness=stale)


def build_section(
    *,
    brief_store,
    staleness_store,
    follow_store,
    baseline: Brief,
    brief: Brief,
) -> RenewalSection:
    decision = baseline.decision
    return RenewalSection(
        baseline=RenewalBaseline(
            brief_id=baseline.brief_id,
            as_of=baseline.as_of,
            action=decision.action,
            actor=decision.actor,
            reason=decision.reason,
            decided_at=decision.decided_at,
        ),
        ledger=compute_ledger(baseline, brief),
        inherited=collect_inherited(
            staleness_store=staleness_store,
            follow_store=follow_store,
            account_id=brief.account_id,
        ),
    )


def attach_renewal(
    *,
    brief_store,
    staleness_store,
    follow_store,
    brief: Brief,
) -> Brief:
    """Compute ``brief``'s renewal section against its baseline
    and store the brief again with it attached — the diff's
    attachment pattern, one step later in the same verify step.
    The baseline is re-found here (the service's pre-check and
    this lookup read the same store within one assembly, so
    they agree); if it has somehow vanished, the refusal is
    the same named one."""
    baseline = require_baseline(brief_store, brief.account_id, brief.as_of)
    brief.renewal = build_section(
        brief_store=brief_store,
        staleness_store=staleness_store,
        follow_store=follow_store,
        baseline=baseline,
        brief=brief,
    )
    brief.audit.append(
        AuditEntry(
            at=_now(),
            event="renewal_baseline",
            detail=(
                f"assembled as a renewal against brief "
                f"{baseline.brief_id} — {baseline.decision.action} by "
                f"{baseline.decision.actor} on "
                f"{baseline.decision.decided_at.date().isoformat()}"
            ),
        )
    )
    brief_store.save(brief)
    return brief
