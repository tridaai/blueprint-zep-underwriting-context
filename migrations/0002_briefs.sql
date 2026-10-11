-- 0002: the brief store — assembled briefs and gate decisions.
-- The payload is the full brief record (facts with citations,
-- superseded section, guardrail results, decision, audit trail) as
-- JSONB; the columns exist for identity, ordering, filtering, and
-- the decided flag. `seq` preserves insertion order (a re-save of a
-- decided brief updates in place and keeps its position).
CREATE TABLE IF NOT EXISTS briefs (
    brief_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    seq BIGINT GENERATED ALWAYS AS IDENTITY,
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'AWAITING_UNDERWRITER',
    decided BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS briefs_account_idx ON briefs (account_id, seq);
