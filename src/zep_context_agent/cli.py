"""Command line: accounts, ingest, brief, decide, show.

A thin shell over :class:`BriefService` — the same service the API
serves — for operators and for smoke runs:

    zep-context-agent accounts
    zep-context-agent ingest ACC-001
    zep-context-agent brief ACC-001 [--as-of 2026-06-01]
    zep-context-agent decide <brief_id> proceed --actor "…" --reason "…"
    zep-context-agent show <brief_id>

Configuration is the environment's (``CONTEXT_BACKEND`` & friends —
see .env.example). Code defaults to the offline adapter when no
backend is configured; a selected-but-unconfigured Zep profile
fails loudly here exactly as it does in the API.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from .config import load_dotenv
from .errors import ContextError
from .render import render_brief_text
from .service import BriefService


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

    p_decide = sub.add_parser("decide", help="Record the underwriter's decision")
    p_decide.add_argument("brief_id")
    p_decide.add_argument("action", choices=["proceed", "request_info", "escalate"])
    p_decide.add_argument("--actor", required=True)
    p_decide.add_argument("--reason", required=True)

    p_show = sub.add_parser("show", help="Render a stored brief as text")
    p_show.add_argument("brief_id")

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
            brief = service.assemble(args.account_id, as_of=args.as_of)
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
    except ContextError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
