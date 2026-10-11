-- 0006: the review gate's candidate records — assertions from
-- correspondence-tier documents wait here (beside the cited layer
-- in citation_index, never in it) until an underwriter confirms
-- or rejects them. The payload is the full candidate as JSONB —
-- the extracted fact, its source excerpt, its review state and
-- stamps; the columns exist for identity, per-account reads, and
-- the pending/decided split the review queue reads. A
-- confirmation also appends the admitted fact to its episode's
-- row in citation_index; this table stays the audit of what was
-- asked, who decided, and why — rejected candidates included.
CREATE TABLE IF NOT EXISTS candidate_facts (
    candidate_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    episode_id TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    seq BIGINT GENERATED ALWAYS AS IDENTITY,
    payload JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS candidate_facts_account_idx ON candidate_facts (account_id, seq);
