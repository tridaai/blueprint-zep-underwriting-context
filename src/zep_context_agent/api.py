"""FastAPI surface: accounts, ingestion, briefs, the gate, exports.

The API is a thin door over :class:`BriefService` — every rule
(temporal resolution, quarantine, leakage, the gate) lives in the
service and its modules, so the console, the CLI, and any HTTP
client see identical behaviour.

Error honesty, mapped to statuses:

- unknown account / brief → 404;
- a decision that cannot be recorded (already decided, missing
  actor or reason) → 409 / 422;
- a Zep profile that is configured but unreachable → 502, with the
  adapter's message. There is no code path in which an error is
  answered by quietly serving the offline graph instead.

``/health`` answers without touching dependencies; ``/readiness``
reports the real serving mode (context backend, database, object
store, checkpointer) and 503s when a configured database does not
answer — a replica that cannot reach its system of record is not
ready, and says so.

Every request carries an ``X-Request-ID`` (the caller's, when
sent) and emits one JSON access line (see ``observability.py``).
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .config import load_dotenv
from .db import database_url, ensure_migrated, ping
from .errors import (
    AccountNotFound,
    BriefNotFound,
    ChaseError,
    ContextError,
    ContextNotConfigured,
    DecisionError,
    FollowThroughError,
    IngestionInProgress,
    RenewalError,
    ReviewError,
)
from .observability import configure_json_logging, new_request_id, request_id_ctx
from .render import render_brief_text
from .schemas import (
    AssembleRequest,
    ChaseSendRequest,
    DecisionRequest,
    ReviewRequest,
)
from .service import BriefService

logger = logging.getLogger("zep_context_agent.api")

_STATIC_DIR = Path(__file__).parent / "static"
_INDEX_HTML = _STATIC_DIR / "index.html"
_BRAND_DIR = _STATIC_DIR / "brand"


def build_service_from_env() -> BriefService:
    load_dotenv()
    return BriefService()


def create_app(service: BriefService | None = None) -> FastAPI:
    svc = service if service is not None else build_service_from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        configure_json_logging()
        if database_url():
            ensure_migrated()
            logger.info("startup: migrations current, database configured")
        else:
            logger.info("startup: no DATABASE_URL — in-memory test doubles")
        app.state.service = svc
        yield
        logger.info("shutdown: drained")

    app = FastAPI(
        title="Trida AI Blueprint — Zep Underwriting Context Assembly",
        description=(
            "Reference prototype. Synthetic data only. The agent assembles "
            "and cites an underwriter's account brief from a Zep Context "
            "Graph and stops at the underwriter's gate; it never prices, "
            "binds, or declines. Independent Trida AI blueprint — not "
            "affiliated with or endorsed by Zep."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.service = svc

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or new_request_id()
        token = request_id_ctx.set(request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id
            logger.info(
                "request",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    "event": "http_request",
                },
            )
            return response
        finally:
            request_id_ctx.reset(token)

    @app.exception_handler(AccountNotFound)
    @app.exception_handler(BriefNotFound)
    def not_found_handler(request: Request, exc: ContextError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(DecisionError)
    def decision_handler(request: Request, exc: DecisionError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(FollowThroughError)
    def follow_through_handler(
        request: Request, exc: FollowThroughError
    ) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(ChaseError)
    def chase_handler(request: Request, exc: ChaseError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(RenewalError)
    def renewal_handler(request: Request, exc: RenewalError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(ReviewError)
    def review_handler(request: Request, exc: ReviewError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(IngestionInProgress)
    def ingestion_handler(
        request: Request, exc: IngestionInProgress
    ) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(ContextNotConfigured)
    def not_configured_handler(
        request: Request, exc: ContextNotConfigured
    ) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.exception_handler(ContextError)
    def context_error_handler(request: Request, exc: ContextError) -> JSONResponse:
        return JSONResponse(status_code=502, content={"detail": str(exc)})

    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

    @app.get("/favicon.svg", include_in_schema=False)
    def favicon_svg() -> FileResponse:
        return FileResponse(_BRAND_DIR / "favicon.svg", media_type="image/svg+xml")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon_ico() -> FileResponse:
        return FileResponse(_BRAND_DIR / "favicon.ico", media_type="image/x-icon")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _INDEX_HTML.read_text(encoding="utf-8")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "prototype": "trida-blueprint-zep-underwriting-context"}

    @app.get("/readiness")
    def readiness() -> JSONResponse:
        detail = svc.readiness()
        if database_url() and not ping():
            return JSONResponse(
                status_code=503,
                content={"status": "not_ready", **detail},
            )
        return JSONResponse(status_code=200, content={"status": "ready", **detail})

    @app.get("/evals/results")
    def eval_results() -> dict:
        candidates = [
            Path(__file__).parent / "data" / "eval_results.json",
            Path.cwd() / "evals" / "results.json",
            Path(__file__).resolve().parents[2] / "evals" / "results.json",
        ]
        for candidate in candidates:
            if candidate.is_file():
                return json.loads(candidate.read_text(encoding="utf-8"))
        raise HTTPException(
            status_code=404,
            detail="No eval results yet. Run: python evals/run_evals.py",
        )

    # -- accounts + ingestion -------------------------------------------------

    @app.get("/accounts")
    def list_accounts() -> list[dict]:
        return [
            {
                "account_id": a.account_id,
                "display_name": a.display_name,
                "documents": len(a.documents),
            }
            for a in svc.list_accounts()
        ]

    @app.get("/accounts/{account_id}")
    def get_account(account_id: str) -> dict:
        account = svc.get_account(account_id)
        return account.model_dump(mode="json")

    @app.post("/accounts/{account_id}/ingest")
    def ingest(account_id: str) -> dict:
        episodes = svc.ingest(account_id)
        return {
            "account_id": account_id,
            "episodes": [e.model_dump(mode="json") for e in episodes],
            "quarantined": [e.doc_id for e in episodes if e.quarantined],
            "open_staleness": [
                r.record_id
                for r in svc.list_staleness(account_id)
                if r.resolved_by_brief is None
            ],
            "follow_through": [
                {
                    "record_id": r.record_id,
                    "brief_id": r.brief_id,
                    "status": r.status,
                    "received_count": sum(
                        1 for i in r.items if i.status == "received"
                    ),
                    "total_count": len(r.items),
                }
                for r in svc.list_follow_through(account_id)
                if r.status != "complete"
            ],
        }

    # -- the desk queue + staleness --------------------------------------------

    @app.get("/desk-queue")
    def desk_queue() -> list[dict]:
        """Every account in desk order: stale decisions first,
        then briefs awaiting the gate, then clean decisions.
        A read model — it decides nothing."""
        return [e.model_dump(mode="json") for e in svc.desk_queue()]

    @app.get("/portfolio")
    def portfolio(
        as_of: date | None = None, horizon_days: int | None = None
    ) -> dict:
        """The morning portfolio brief: every account's signals
        — stale decisions, lapsed and expiring facts, open
        asks, candidates awaiting review, never-decided — counted
        in code and ranked by the weights printed on the brief
        itself. A pure projection: it mutates nothing and does
        not re-order the desk queue, which keeps its own order.
        ``as_of`` pins the temporal verdicts (defaults to
        today); the horizon defaults to EXPIRY_HORIZON_DAYS."""
        brief = svc.portfolio_brief(as_of=as_of, horizon_days=horizon_days)
        return brief.model_dump(mode="json")

    @app.get("/staleness")
    def list_staleness(account_id: str | None = None) -> list[dict]:
        return [
            r.model_dump(mode="json") for r in svc.list_staleness(account_id)
        ]

    @app.get("/follow-through")
    def list_follow_through(account_id: str | None = None) -> list[dict]:
        """Request-info asks and their per-item state. The draft
        on each record was prepared for the underwriter to copy —
        nothing here sends anything."""
        return [
            r.model_dump(mode="json")
            for r in svc.list_follow_through(account_id)
        ]

    # -- renewal chases ---------------------------------------------------------

    @app.get("/chases")
    def list_chases(
        account_id: str | None = None,
        as_of: date | None = None,
        horizon_days: int | None = None,
    ) -> list[dict]:
        """The renewal chases: expiring facts joined to the ask
        machinery, per account and predicate — OPEN (sendable),
        ASKED (the broker owes the renewal), SATISFIED (the
        renewal landed; the row names its document),
        LAPSED_UNSENT (a fact lapsed unchased), WAITING_ON_BRIEF
        (no brief to attach an ask to). A pure projection; a
        predicate an open request-info ask covers has no row —
        the broker is never asked twice."""
        return [
            r.model_dump(mode="json")
            for r in svc.renewal_chases(
                account_id, as_of=as_of, horizon_days=horizon_days
            )
        ]

    @app.post("/accounts/{account_id}/chases/{predicate}/send")
    def send_chase(
        account_id: str, predicate: str, request: ChaseSendRequest
    ) -> dict:
        """Send one renewal chase: open its follow-through ask
        on the account's latest brief, drafted in code from the
        expiring fact and its expiry. Refusals are 409s naming
        their cause — already asked by a decision, already
        chased, already satisfied, nothing expiring, or no
        brief to attach to. The draft rides the returned ask
        record; like every ask here, the system sends nothing
        itself."""
        record, row = svc.send_chase(
            account_id, predicate, actor=request.actor
        )
        return {
            "ask": record.model_dump(mode="json"),
            "chase": row.model_dump(mode="json"),
        }

    @app.get("/briefs/{brief_id}/follow-through")
    def brief_follow_through(brief_id: str) -> dict:
        svc.get_brief(brief_id)  # 404 for an unknown brief
        record = svc.follow_through_for_brief(brief_id)
        if record is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Brief {brief_id} has no follow-through record "
                    "(only a request_info decision opens one)."
                ),
            )
        return record.model_dump(mode="json")

    # -- the review gate ---------------------------------------------------------

    @app.get("/review-queue")
    def review_queue(account_id: str | None = None) -> list[dict]:
        """The ingestion review queue: correspondence assertions
        waiting as candidates, grouped by the document they came
        from, with the source excerpt — plus the decided records
        (confirmed, rejected) as their audit. Nothing in the
        queue is a fact until an underwriter confirms it."""
        return [
            g.model_dump(mode="json")
            for g in svc.review_queue(account_id)
        ]

    @app.post("/candidates/{candidate_id}/decision")
    def review_decision(candidate_id: str, request: ReviewRequest) -> dict:
        candidate = svc.review_candidate(
            candidate_id,
            account_id=request.account_id,
            action=request.action,
            actor=request.actor,
            reason=request.reason,
        )
        payload = candidate.model_dump(mode="json")
        if candidate.state == "confirmed":
            fact = next(
                (
                    f
                    for f in svc.port.get_facts(request.account_id)
                    if f.fact_id == candidate_id
                ),
                None,
            )
            payload["fact"] = (
                fact.model_dump(mode="json") if fact is not None else None
            )
        return payload

    # -- timeline + the expiry watch ---------------------------------------------

    @app.get("/accounts/{account_id}/timeline")
    def account_timeline(
        account_id: str, as_of: date | None = None
    ) -> dict:
        """The account's chronology: every fact on its validity
        window with its citation, episodes, decisions, staleness
        flags, and follow-through asks interleaved in one
        ordering. A pure projection — computing it mutates
        nothing. ``as_of`` pins the temporal rule's verdicts
        (defaults to today)."""
        timeline = svc.account_timeline(account_id, as_of=as_of)
        return timeline.model_dump(mode="json")

    @app.get("/expiry-watch")
    def expiry_watch(
        as_of: date | None = None, horizon_days: int | None = None
    ) -> list[dict]:
        """The desk-level expiry watch: relied-on facts whose
        windows close within the horizon (EXPIRING) or have
        closed with no successor in force (LAPSED), most
        lapsed first. Facts a renewal covers never appear."""
        return [
            r.model_dump(mode="json")
            for r in svc.expiry_watch(as_of=as_of, horizon_days=horizon_days)
        ]

    # -- briefs + gate ----------------------------------------------------------

    @app.post("/briefs", status_code=201)
    def assemble(request: AssembleRequest) -> dict:
        """Assemble a brief and stop at the gate. ``mode`` is
        "standard" (the default) or "renewal" — a renewal is
        assembled against the account's last decision and
        carries its baseline, the change ledger since it, and
        the open business inherited (see renewals.py). A
        renewal with no decided brief to renew against is a
        409 naming the cause; it assembles nothing."""
        brief = svc.assemble(
            request.account_id, as_of=request.as_of, mode=request.mode
        )
        return brief.model_dump(mode="json")

    @app.get("/briefs")
    def list_briefs(account_id: str | None = None) -> list[dict]:
        return [
            {
                "brief_id": b.brief_id,
                "account_id": b.account_id,
                "account_name": b.account_name,
                "as_of": b.as_of.isoformat(),
                "status": b.status,
                "decision": b.decision.action if b.decision else None,
                "renewal": b.renewal is not None,
                "renewal_baseline": (
                    b.renewal.baseline.brief_id if b.renewal else None
                ),
                "created_at": b.created_at.isoformat(),
            }
            for b in svc.list_briefs(account_id)
        ]

    @app.get("/briefs/{brief_id}")
    def get_brief(brief_id: str) -> dict:
        return svc.get_brief(brief_id).model_dump(mode="json")

    @app.get("/briefs/{brief_id}/export")
    def export_brief(brief_id: str, format: str = Query("json")) -> object:
        brief = svc.get_brief(brief_id)
        if format == "text":
            return PlainTextResponse(
                render_brief_text(brief),
                media_type="text/plain",
                headers={
                    "Content-Disposition": f'attachment; filename="{brief_id}.txt"'
                },
            )
        return brief.model_dump(mode="json")

    @app.post("/briefs/{brief_id}/decision")
    def decide(brief_id: str, request: DecisionRequest) -> dict:
        brief = svc.decide(
            brief_id,
            action=request.action,
            actor=request.actor,
            reason=request.reason,
        )
        return brief.model_dump(mode="json")

    return app


app = create_app()
