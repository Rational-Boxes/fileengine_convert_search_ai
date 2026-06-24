-- convert_search_ai — database-wide baseline (M0).
--
-- This service mirrors the core's tenant↔schema model: each tenant's data lives
-- in its own `tenant_<tenant>` schema (see src/convert_search_ai/schema.py),
-- provisioned on demand by code — NOT by a static migration. This file only
-- installs the database-wide extensions those per-tenant tables depend on.
--
-- Apply once against this service's own database:
--   psql "host=localhost port=5434 dbname=convert_search_ai user=... password=..." \
--     -f migrations/0001_baseline.sql
--
-- Then provision a tenant's schema + tables (idempotent):
--   python -c "from convert_search_ai.config import Config; \
--              from convert_search_ai.db import provision_tenant; \
--              provision_tenant(Config(), 'default')"
--
-- The per-tenant DDL (documents, chunks with vector(1024) + tsvector + HNSW/GIN
-- indexes) is the single source of truth in schema.py's _TENANT_DDL.

CREATE EXTENSION IF NOT EXISTS vector;     -- pgvector: ANN over embeddings
CREATE EXTENSION IF NOT EXISTS pg_trgm;    -- fuzzy / trigram full-text matching
