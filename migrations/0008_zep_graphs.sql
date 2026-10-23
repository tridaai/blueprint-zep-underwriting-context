-- 0008: the Zep v4 graph registry — which Zep Context Graph
-- (server-assigned UUID) serves which insurance account, and
-- whether the blueprint's ontology has been applied to it.
-- Zep v4 addresses graphs by UUID, not by a client-chosen id,
-- so the account -> graph mapping the deployment owns has to
-- live somewhere durable: here. One row per account, written
-- when the account's graph is first provisioned (graph.create
-- plus graph.set_ontology, which is not retroactive and must
-- land before the graph's first episode). ontology_applied
-- flips only after the set_ontology call succeeds, so a crash
-- between create and set re-runs the set on the next touch
-- instead of assuming it landed.
CREATE TABLE IF NOT EXISTS zep_graphs (
    account_id TEXT PRIMARY KEY,
    graph_uuid TEXT NOT NULL,
    ontology_applied BOOLEAN NOT NULL DEFAULT FALSE,
    seq BIGINT GENERATED ALWAYS AS IDENTITY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS zep_graphs_graph_idx ON zep_graphs (graph_uuid);
