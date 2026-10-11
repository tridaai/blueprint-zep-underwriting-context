-- 0005: the follow-through records — a request_info decision
-- becomes a tracked ask (missing predicates, quarantined
-- documents to resend) with a drafted broker request and a
-- per-item state that flips to received as replies are ingested.
-- The payload is the full record as JSONB — the items, the draft
-- text, the decision it came from, and the completion stamp; the
-- columns exist for identity, per-account reads, and the
-- open/complete split the desk queue reads. The decided brief the
-- ask came from is never edited — this record lives apart from
-- it, exactly like the staleness records (0004).
CREATE TABLE IF NOT EXISTS follow_through_records (
    record_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    brief_id TEXT NOT NULL,
    seq BIGINT GENERATED ALWAYS AS IDENTITY,
    payload JSONB NOT NULL,
    complete BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS follow_through_records_account_idx ON follow_through_records (account_id, seq);
CREATE INDEX IF NOT EXISTS follow_through_records_brief_idx ON follow_through_records (brief_id);
