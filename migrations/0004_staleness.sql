-- 0004: the staleness records — a decided brief whose facts moved
-- after the decision gets a flag of its own, stored apart from the
-- brief (the decided brief is an audit record and is never
-- edited). The payload is the full record as JSONB — the decision
-- it refers to, the trigger episodes, the material change slice,
-- and the resolution stamp once a fresh decision lands; the
-- columns exist for identity, per-account reads, and the
-- open/resolved split the desk queue orders by.
CREATE TABLE IF NOT EXISTS staleness_records (
    record_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    brief_id TEXT NOT NULL,
    seq BIGINT GENERATED ALWAYS AS IDENTITY,
    payload JSONB NOT NULL,
    resolved BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS staleness_records_account_idx ON staleness_records (account_id, seq);
