-- 0007: the duplicate verdicts — a named underwriter's word on
-- a flagged duplicate pair (SAME_INSURED with a note, or
-- DISTINCT with the distinguishing fact stated), stored apart
-- from both accounts: a verdict edits no brief, moves no fact,
-- and merges nothing. The payload is the full record as JSONB —
-- the pair, the verdict, the actor, the note, and the evidence
-- fingerprint the verdict was recorded against (a DISTINCT
-- verdict suppresses only while the pair's current evidence
-- still hashes to it); the columns exist for identity and
-- per-pair reads. The pairs themselves are computed from
-- current facts on every read and are never stored.
CREATE TABLE IF NOT EXISTS duplicate_verdicts (
    record_id TEXT PRIMARY KEY,
    account_a TEXT NOT NULL,
    account_b TEXT NOT NULL,
    verdict TEXT NOT NULL,
    seq BIGINT GENERATED ALWAYS AS IDENTITY,
    payload JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS duplicate_verdicts_pair_idx ON duplicate_verdicts (account_a, account_b, seq);
