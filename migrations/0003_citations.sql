-- 0003: the citation index — the episode-payload record behind
-- every citation a brief carries. One row per ingested episode:
-- the episode itself plus the facts and edges that episode
-- contributed, as JSONB. Both ContextPort profiles write here
-- (the offline adapter directly; the zep adapter for the payload
-- index its cited reads come from), so a process restart re-reads
-- the cited layer from the database instead of losing it and
-- re-deriving it from the source documents.
CREATE TABLE IF NOT EXISTS citation_index (
    account_id TEXT NOT NULL,
    episode_id TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    episode JSONB NOT NULL,
    facts JSONB NOT NULL DEFAULT '[]',
    edges JSONB NOT NULL DEFAULT '[]',
    seq BIGINT GENERATED ALWAYS AS IDENTITY,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, episode_id)
);
CREATE INDEX IF NOT EXISTS citation_index_account_idx ON citation_index (account_id, seq);
