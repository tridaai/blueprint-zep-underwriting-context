"""Ingestion: source documents become episodes, with lineage.

The pipeline for one document, in order:

1. **Screen** — the injection screen (``guardrails.screen_document``)
   reads the document's free text. A tripped document is marked
   quarantined: its episode is recorded (the file shows it arrived)
   but neither adapter turns its assertions into facts.
2. **Archive** — when an object store is configured, the document
   is written to it as JSON under
   ``accounts/<account_id>/documents/<doc_id>.json``, so a brief's
   citation names a durable object.
3. **Ingest** — the document and its episode go through the
   ContextPort. Episode ids are derived from the document id, so
   re-ingesting the same account is idempotent in every adapter.
   A single document ingests in real time
   (:func:`ingest_document`). A run carrying more than one
   document (:func:`ingest_documents` — an account backfill, a
   seed load) goes through the port's bulk path when the adapter
   offers one: on the Zep profile that is the Batch API, so a
   backfill is one governed submission instead of N round trips.
   Adapters without a bulk path take the same documents one at a
   time, unchanged.

One gate sits inside step 3, in the adapters: provenance. A
document's kind assigns its assertions a tier (see
:mod:`ontology`): system records and filed documents become
facts on arrival, but a correspondence document's assertions
become *candidates* — held at the review gate until an
underwriter confirms them (see :mod:`review`). The screen is
upstream of the gate: a quarantined document's assertions become
neither facts nor candidates.

Nothing here interprets a document: assertions arrive structured
(an ACORD extract, a claims run, an endorsement schedule — in a
production deployment, the output of the carrier's document
pipeline) and are stored as asserted. Interpretation — what is
current, what is superseded — is assembly's job, over time.
"""

from __future__ import annotations

import json

from .guardrails import screen_document
from .ontology import Account, Episode, SourceDocument
from .ports import ContextPort


def episode_id_for(doc: SourceDocument) -> str:
    return f"ep-{doc.doc_id}"


def _prepare_episode(doc: SourceDocument, *, object_store=None) -> Episode:
    """Screen and archive one document; return its episode. The
    screen's verdict rides on the episode — the adapters honour
    quarantine when they turn assertions into facts."""
    quarantined, reason = screen_document(doc)
    episode = Episode(
        episode_id=episode_id_for(doc),
        account_id=doc.account_id,
        doc_id=doc.doc_id,
        kind=doc.kind,
        title=doc.title,
        received_at=doc.received_at,
        screened=True,
        quarantined=quarantined,
        quarantine_reason=reason,
    )
    if object_store is not None:
        object_store.put(
            f"accounts/{doc.account_id}/documents/{doc.doc_id}.json",
            json.dumps(doc.model_dump(mode="json"), indent=2).encode("utf-8"),
        )
    return episode


def ingest_document(
    port: ContextPort,
    doc: SourceDocument,
    *,
    object_store=None,
) -> Episode:
    """Screen, archive, and ingest one document, in real time.
    Returns the episode."""
    episode = _prepare_episode(doc, object_store=object_store)
    return port.ingest_episode(doc, episode)


def ingest_documents(
    port: ContextPort,
    docs: list[SourceDocument],
    *,
    object_store=None,
) -> list[Episode]:
    """Screen, archive, and ingest a run of documents.

    More than one document in the run takes the port's bulk path
    when the adapter offers one (``ingest_documents_batch`` — on
    the Zep profile, the Batch API); every document is still
    screened and archived here first, and quarantined documents
    ride through the batch with their verdict on the episode,
    exactly as they do one at a time. A single document — or an
    adapter with no bulk path — ingests one at a time, unchanged.
    """
    prepared = [
        (doc, _prepare_episode(doc, object_store=object_store)) for doc in docs
    ]
    batch = getattr(port, "ingest_documents_batch", None)
    if len(prepared) > 1 and callable(batch):
        return batch(prepared)
    return [port.ingest_episode(doc, episode) for doc, episode in prepared]


def ingest_account(
    port: ContextPort,
    account: Account,
    *,
    object_store=None,
) -> list[Episode]:
    """Ingest every document of one account, in file order."""
    return ingest_documents(port, account.documents, object_store=object_store)
