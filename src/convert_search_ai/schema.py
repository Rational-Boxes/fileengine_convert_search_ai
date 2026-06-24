"""Per-tenant Postgres schema isolation — mirrors the core's tenant↔schema model.

The FileEngine core isolates each tenant in a ``tenant_<tenant>`` schema
(empty/unset → ``tenant_default``, with ``-`` / ``.`` / space sanitized to ``_``).
This add-on microservice partitions **its own** storage the same way: each tenant
gets a ``tenant_<tenant>`` schema holding this service's ``documents`` and
``chunks`` tables. The schema *is* the tenant, so the tables carry **no tenant
column** — scoping is done by setting ``search_path`` to the tenant's schema.

Database-wide objects (the ``vector`` / ``pg_trgm`` extensions) live once at the
database level — see ``migrations/0001_baseline.sql``. The per-tenant tables are
provisioned on demand by code (``ensure_tenant_schema``), exactly as the core
provisions a tenant schema rather than via a static migration.
"""
import re

# Anything outside [A-Za-z0-9_] becomes '_' — a superset of the core's
# '-'/'.'/space replacement, so the schema name is always a safe identifier.
_UNSAFE = re.compile(r"[^A-Za-z0-9_]")


def schema_name(tenant: str) -> str:
    """The tenant's schema: ``tenant_<sanitized-tenant>``.

    Empty/unset → ``tenant_default`` (avoids the reserved word ``default``),
    matching the core's ``get_schema_prefix``."""
    t = (tenant or "").strip()
    if not t:
        return "tenant_default"
    return "tenant_" + _UNSAFE.sub("_", t)


# Idempotent DDL for one tenant's tables, parameterized by schema name. Kept as
# the single source of truth (the migration file only handles DB-wide extensions).
_TENANT_DDL = '''
CREATE SCHEMA IF NOT EXISTS "{schema}";

-- One row per source file we have processed (the schema scopes the tenant).
CREATE TABLE IF NOT EXISTS "{schema}".documents (
    file_uid        TEXT PRIMARY KEY,
    source_version  TEXT        NOT NULL DEFAULT '',   -- FileEngine version id (string)
    mime            TEXT        NOT NULL DEFAULT '',
    name            TEXT        NOT NULL DEFAULT '',
    path            TEXT        NOT NULL DEFAULT '',
    content_md      TEXT,                               -- extracted Markdown (NULL until extracted)
    status          TEXT        NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','converting','converted','indexed','unsupported','error')),
    error           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Full-text vector over name + extracted Markdown (M2 search).
    fts             tsvector GENERATED ALWAYS AS (
                        to_tsvector('english', coalesce(name, '') || ' ' || coalesce(content_md, ''))
                    ) STORED
);
CREATE INDEX IF NOT EXISTS idx_documents_status
    ON "{schema}".documents (status);
CREATE INDEX IF NOT EXISTS idx_documents_fts
    ON "{schema}".documents USING gin (fts);
CREATE INDEX IF NOT EXISTS idx_documents_content_trgm
    ON "{schema}".documents USING gin (content_md gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_documents_name_trgm
    ON "{schema}".documents USING gin (name gin_trgm_ops);

-- Chunked + vectorized content for search and RAG. embedding is vector(1024) to
-- match CSAI_EMBEDDING_DIMENSION's default; a model change is an explicit
-- migration (ALTER + re-embed), never a silent mismatch.
CREATE TABLE IF NOT EXISTS "{schema}".chunks (
    id          BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    file_uid    TEXT        NOT NULL REFERENCES "{schema}".documents (file_uid) ON DELETE CASCADE,
    ordinal     INTEGER     NOT NULL,
    text        TEXT        NOT NULL,
    embedding   vector(1024),
    fts         tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_chunks_fts
    ON "{schema}".chunks USING gin (fts);
CREATE INDEX IF NOT EXISTS idx_chunks_text_trgm
    ON "{schema}".chunks USING gin (text gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_chunks_embedding
    ON "{schema}".chunks USING hnsw (embedding vector_cosine_ops);
'''


def tenant_ddl(tenant: str) -> str:
    """The idempotent DDL that provisions a tenant's schema + tables."""
    return _TENANT_DDL.format(schema=schema_name(tenant))


def ensure_tenant_schema(conn, tenant: str) -> str:
    """Create the tenant's schema + tables if absent (idempotent).

    ``conn`` is an open psycopg connection (the extensions must already exist at
    the database level). Returns the schema name."""
    name = schema_name(tenant)
    with conn.cursor() as cur:
        cur.execute(tenant_ddl(tenant))
    conn.commit()
    return name
