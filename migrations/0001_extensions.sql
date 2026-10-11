-- 0001: extensions the deployment relies on.
-- pgvector is available in the compose image (pgvector/pgvector) and
-- reserved here for future retrieval work over the brief store; the
-- brief pipeline itself reads facts whole per account and needs no
-- vector index in v1.
CREATE EXTENSION IF NOT EXISTS vector;
