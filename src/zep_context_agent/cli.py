"""Command line: accounts, ingest, brief, decide, show.

A thin shell over :class:`BriefService` — the same service the API
serves — for operators and for smoke runs:

    zep-context-agent accounts
    zep-context-agent ingest ACC-001
    zep-context-agent brief ACC-001 [--as-of 2026-06-01] [--renewal]
    zep-context-agent decide <brief_id> proceed --actor "…" --reason "…"
    zep-context-agent show <brief_id>
    zep-context-agent queue
    zep-context-agent portfolio [--as-of 2026-06-01] [--horizon-days 45]
    zep-context-agent brokers [--as-of 2026-06-01] [--horizon-days 45]
    zep-context-agent decision-book --from 2026-06-01 --to 2026-06-14
    zep-context-agent timeline ACC-001 [--as-of 2026-06-01]
    zep-context-agent watch [--as-of 2026-06-01] [--horizon-days 45]
    zep-context-agent chases [--account ACC-001] [--as-of 2026-06-01] [--horizon-days 45]
    zep-context-agent chase-send ACC-001 inspection_grade --actor "…"
    zep-context-agent review [--account ACC-001]
    zep-context-agent confirm <candidate_id> --account ACC-001 --actor "…"
    zep-context-agent reject <candidate_id> --account ACC-001 --actor "…" --reason "…"

Configuration is the environment's (``CONTEXT_BACKEND`` & friends —
see .env.example). Code defaults to the offline adapter when no
backend is configured; a selected-but-unconfigured Zep profile
fails loudly here exactly as it does in the API.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from .brokers import render_broker_book_text
from .chases import render_chases_text
from .decisionbook import render_decision_book_text
from .config import load_dotenv
from .errors import ContextError
from .followthrough import render_follow_through_text
from .portfolio import render_portfolio_text
from .render import render_brief_text
from .review import render_review_queue_text
from .service import BriefService
from .timeline import render_expiry_watch_text, render_timeline_text


def _service() -> BriefService:
    load_dotenv()
    return BriefService()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="zep-context-agent")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("accounts", help="List the synthetic accounts")

    p_ingest = sub.add_parser("ingest", help="Ingest one account's documents")
    p_ingest.add_argument("account_id")

    p_brief = sub.add_parser("brief", help="Assemble a brief and stop at the gate")
    p_brief.add_argument("account_id")
    p_brief.add_argument("--as-of", type=date.fromisoformat, default=None)
    p_brief.add_argument(
        "--renewal",
        action="store_true",
        help="Assemble as a renewal: against the account's last "
        "decision, with its change ledger and inherited open "
        "business (refused by name when nothing was decided)",
    )

    p_decide = sub.add_parser("decide", help="Record the underwriter's decision")
    p_decide.add_argument("brief_id")
    p_decide.add_argument("action", choices=["proceed", "request_info", "escalate"])
    p_decide.add_argument("--actor", required=True)
    p_decide.add_argument("--reason", required=True)

    p_show = sub.add_parser("show", help="Render a stored brief as text")
    p_show.add_argument("brief_id")

    sub.add_parser(
        "queue",
        help="The desk queue: stale decisions first, then awaiting briefs",
    )

    p_portfolio = sub.add_parser(
        "portfolio",
        help="The morning portfolio brief: the whole book, "
        "signals counted and ranked",
    )
    p_portfolio.add_argument("--as-of", type=date.fromisoformat, default=None)
    p_portfolio.add_argument("--horizon-days", type=int, default=None)

    p_brokers = sub.add_parser(
        "brokers",
        help="The broker book: the whole book grouped by "
        "placing broker — what each broker owes the desk",
    )
    p_brokers.add_argument("--as-of", type=date.fromisoformat, default=None)
    p_brokers.add_argument("--horizon-days", type=int, default=None)

    p_book = sub.add_parser(
        "decision-book",
        help="The decision book: every governed act in a period, "
        "in time order, with its counts and digest",
    )
    p_book.add_argument("--from", dest="start", type=date.fromisoformat, required=True)
    p_book.add_argument("--to", dest="end", type=date.fromisoformat, required=True)

    p_asks = sub.add_parser(
        "asks",
        help="Request-info follow-through records (drafts + item state)",
    )
    p_asks.add_argument("account_id", nargs="?", default=None)

    p_timeline = sub.add_parser(
        "timeline",
        help="One account's chronology: facts on their windows, "
        "episodes, decisions, flags, asks",
    )
    p_timeline.add_argument("account_id")
    p_timeline.add_argument("--as-of", type=date.fromisoformat, default=None)

    p_watch = sub.add_parser(
        "watch",
        help="The expiry watch: relied-on facts running out",
    )
    p_watch.add_argument("--as-of", type=date.fromisoformat, default=None)
    p_watch.add_argument("--horizon-days", type=int, default=None)

    p_chases = sub.add_parser(
        "chases",
        help="The renewal chases: expiring facts joined to the "
        "ask machinery — open, asked, satisfied, lapsed unsent",
    )
    p_chases.add_argument("--account", default=None)
    p_chases.add_argument("--as-of", type=date.fromisoformat, default=None)
    p_chases.add_argument("--horizon-days", type=int, default=None)

    p_chase_send = sub.add_parser(
        "chase-send",
        help="Send one renewal chase: open its follow-through "
        "ask on the account's latest brief",
    )
    p_chase_send.add_argument("account_id")
    p_chase_send.add_argument("predicate")
    p_chase_send.add_argument("--actor", required=True)

    p_review = sub.add_parser(
        "review",
        help="The review queue: correspondence assertions waiting "
        "for an underwriter",
    )
    p_review.add_argument("--account", default=None)

    p_confirm = sub.add_parser(
        "confirm",
        help="Confirm a candidate into the graph as a fact",
    )
    p_confirm.add_argument("candidate_id")
    p_confirm.add_argument("--account", required=True)
    p_confirm.add_argument("--actor", required=True)

    p_reject = sub.add_parser(
        "reject",
        help="Reject a candidate, with the reason on the record",
    )
    p_reject.add_argument("candidate_id")
    p_reject.add_argument("--account", required=True)
    p_reject.add_argument("--actor", required=True)
    p_reject.add_argument("--reason", required=True)

    args = parser.parse_args(argv)
    try:
        service = _service()
        if args.command == "accounts":
            for account in service.list_accounts():
                print(
                    f"{account.account_id}  {account.display_name}  "
                    f"({len(account.documents)} documents)"
                )
        elif args.command == "ingest":
            episodes = service.ingest(args.account_id)
            quarantined = [e.doc_id for e in episodes if e.quarantined]
            print(f"{len(episodes)} episodes ingested for {args.account_id}")
            if quarantined:
                print(f"quarantined: {', '.join(quarantined)}")
        elif args.command == "brief":
            brief = service.assemble(
                args.account_id,
                as_of=args.as_of,
                mode="renewal" if args.renewal else "standard",
            )
            print(render_brief_text(brief))
        elif args.command == "decide":
            brief = service.decide(
                args.brief_id,
                action=args.action,
                actor=args.actor,
                reason=args.reason,
            )
            print(
                f"{brief.brief_id}: {brief.decision.action} by "
                f"{brief.decision.actor} — {brief.decision.reason}"
            )
        elif args.command == "show":
            print(render_brief_text(service.get_brief(args.brief_id)))
        elif args.command == "queue":
            for entry in service.desk_queue():
                line = f"{entry.account_id}  {entry.state}"
                if entry.brief_is_renewal:
                    line += "  RENEWAL (latest brief)"
                if entry.decision is not None:
                    line += (
                        f"  {entry.decision.action} by "
                        f"{entry.decision.actor} on "
                        f"{entry.decision.decided_on}"
                    )
                if entry.staleness is not None:
                    s = entry.staleness
                    moved = ", ".join(
                        f"{c.predicate} {c.previous_value} → "
                        f"{c.current_value}"
                        for c in s.changes.superseded
                    )
                    line += (
                        f"  STALE {s.age_days}d (flagged "
                        f"{s.flagged_at.date()})"
                        + (f": {moved}" if moved else "")
                    )
                if entry.follow_through is not None:
                    f = entry.follow_through
                    line += (
                        f"  ASK {f.status} ({f.received_count} of "
                        f"{f.total_count} received)"
                    )
                    if entry.ready_for_review:
                        line += "  READY FOR REVIEW"
                for x in entry.expiry:
                    if x.state == "lapsed":
                        line += (
                            f"  LAPSED {x.predicate} "
                            f"({-x.days_remaining}d ago)"
                        )
                    else:
                        line += (
                            f"  EXPIRING {x.predicate} "
                            f"(in {x.days_remaining}d)"
                        )
                for c in entry.chases:
                    if c.state == "SATISFIED":
                        line += f"  CHASE {c.predicate} SATISFIED"
                    else:
                        line += f"  CHASE {c.predicate} {c.state}"
                if entry.thin_fact_count:
                    line += f"  THIN {entry.thin_fact_count} fact(s)"
                print(line)
        elif args.command == "portfolio":
            brief = service.portfolio_brief(
                as_of=args.as_of, horizon_days=args.horizon_days
            )
            print(render_portfolio_text(brief), end="")
        elif args.command == "brokers":
            book = service.broker_book(
                as_of=args.as_of, horizon_days=args.horizon_days
            )
            print(render_broker_book_text(book), end="")
        elif args.command == "decision-book":
            book = service.decision_book(args.start, args.end)
            print(render_decision_book_text(book), end="")
        elif args.command == "timeline":
            timeline = service.account_timeline(
                args.account_id, as_of=args.as_of
            )
            print(render_timeline_text(timeline))
        elif args.command == "watch":
            from .temporal import today as _today

            from .config import expiry_horizon_days

            as_of = args.as_of or _today()
            horizon = (
                args.horizon_days
                if args.horizon_days is not None
                else expiry_horizon_days()
            )
            rows = service.expiry_watch(
                as_of=as_of, horizon_days=horizon
            )
            print(render_expiry_watch_text(rows, as_of, horizon))
        elif args.command == "asks":
            records = service.list_follow_through(args.account_id)
            if not records:
                print("No follow-through records.")
            for record in records:
                print(render_follow_through_text(record))
        elif args.command == "chases":
            from .temporal import today as _today

            from .config import expiry_horizon_days

            as_of = args.as_of or _today()
            horizon = (
                args.horizon_days
                if args.horizon_days is not None
                else expiry_horizon_days()
            )
            rows = service.renewal_chases(
                args.account, as_of=as_of, horizon_days=horizon
            )
            print(render_chases_text(rows, as_of, horizon), end="")
        elif args.command == "chase-send":
            record, row = service.send_chase(
                args.account_id, args.predicate, actor=args.actor
            )
            print(
                f"CHASE SENT — {row.predicate} on {row.account_id}: "
                f"ask {record.record_id} opened on brief "
                f"{record.brief_id} in {args.actor}'s name."
            )
            print(render_follow_through_text(record))
        elif args.command == "review":
            print(
                render_review_queue_text(
                    service.review_queue(args.account)
                )
            )
        elif args.command in ("confirm", "reject"):
            candidate = service.review_candidate(
                args.candidate_id,
                account_id=args.account,
                action=args.command,
                actor=args.actor,
                reason=getattr(args, "reason", "") or "",
            )
            if candidate.state == "confirmed":
                print(
                    f"{candidate.candidate_id}: CONFIRMED by "
                    f"{candidate.review_actor} — {candidate.predicate} "
                    f"entered the graph, recorded "
                    f"{candidate.fact_recorded_at}."
                )
            else:
                print(
                    f"{candidate.candidate_id}: REJECTED by "
                    f"{candidate.review_actor} — {candidate.review_reason}"
                )
    except ContextError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
