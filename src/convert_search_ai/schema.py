# Copyright (C) 2026 James Hickman
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

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
                    CHECK (status IN ('pending','converting','converted','indexed','index_failed','unsupported','error')),
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

-- Chunked + vectorized content for search and RAG. The embedding column's
-- dimension is the deployment's CSAI_EMBEDDING_DIMENSION (must match the chosen
-- model — e.g. 1024 voyage-3, 768 nomic-embed-text, 1536 text-embedding-3-small).
-- A model change is an explicit migration (ALTER + re-embed), never a silent
-- mismatch; the schema is fixed at provisioning time.
CREATE TABLE IF NOT EXISTS "{schema}".chunks (
    id          BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    file_uid    TEXT        NOT NULL REFERENCES "{schema}".documents (file_uid) ON DELETE CASCADE,
    ordinal     INTEGER     NOT NULL,
    text        TEXT        NOT NULL,
    embedding   vector({dimension}),
    fts         tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_chunks_fts
    ON "{schema}".chunks USING gin (fts);
CREATE INDEX IF NOT EXISTS idx_chunks_text_trgm
    ON "{schema}".chunks USING gin (text gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_chunks_embedding
    ON "{schema}".chunks USING hnsw (embedding vector_cosine_ops);

-- Persisted chat conversations, scoped per user within the tenant schema so a
-- user can resume past chats. Ids are app-generated (uuid hex).
-- Erasure tombstones (PROPOSAL_accountability_record.md §5.4.5).
--
-- An erasure can arrive while a conversion is mid-flight on the same uid — text
-- being extracted, embeddings being written. Purging and then letting that job
-- finish puts the derived data straight back, AFTER the erasure was recorded
-- complete. That is how purges silently fail, so honouring one has two parts:
-- destroy what exists, and refuse to write derived data for the uid thereafter.
-- Cancelling in-flight work is best-effort; this refusal is what closes the race.
--
-- Rows are permanent. They are tiny, and the alternative — expiring them — is a
-- window in which a late job can resurrect erased content.
CREATE TABLE IF NOT EXISTS "{schema}".erased_documents (
    file_uid    TEXT        PRIMARY KEY,
    erasure_id  TEXT        NOT NULL DEFAULT '',
    erased_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS "{schema}".conversations (
    id          TEXT        PRIMARY KEY,
    user_id     TEXT        NOT NULL,
    title       TEXT        NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- The conversation's RAG folder scope (a JSON array of uid/path pairs; NULL or
    -- empty = all docs), so the "Limit to folders" tool restores on resume.
    scope       JSONB
);
CREATE INDEX IF NOT EXISTS idx_conversations_user
    ON "{schema}".conversations (user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS "{schema}".conversation_messages (
    id              BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    conversation_id TEXT        NOT NULL REFERENCES "{schema}".conversations (id) ON DELETE CASCADE,
    role            TEXT        NOT NULL CHECK (role IN ('user','assistant')),
    content         TEXT        NOT NULL DEFAULT '',
    citations       JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_conv_messages
    ON "{schema}".conversation_messages (conversation_id, id);

-- Tenant-managed MCP integrations (MCP_INTEGRATIONS.md). Each row is an external
-- MCP server the tenant admin registered; the chat model may call its tools (with
-- per-call user consent). Lives in the tenant schema, so isolation is automatic.
-- `secret_enc` is Fernet-encrypted at rest and never returned by the API.
CREATE TABLE IF NOT EXISTS "{schema}".mcp_integration (
    id               TEXT        PRIMARY KEY,
    name             TEXT        NOT NULL,
    slug             TEXT        NOT NULL,
    description      TEXT        NOT NULL DEFAULT '',
    transport        TEXT        NOT NULL DEFAULT 'streamable-http'
                     CHECK (transport IN ('streamable-http','sse')),
    endpoint_url     TEXT        NOT NULL,
    -- none | bearer | header | oauth. Validated in the app layer (mcp_admin); no DB
    -- CHECK so adding an auth type is a code change, not a schema migration.
    auth_type        TEXT        NOT NULL DEFAULT 'none',
    auth_header      TEXT        NOT NULL DEFAULT '',   -- header name when auth_type='header'
    secret_enc       BYTEA,                             -- Fernet(token/client_secret); NULL when none
    -- OAuth 2.0 client-credentials (auth_type='oauth'): CSAI fetches a bearer token
    -- from token_url with oauth_client_id + the decrypted secret, and calls the MCP
    -- server with it. secret_enc holds the client_secret.
    token_url        TEXT        NOT NULL DEFAULT '',
    oauth_client_id  TEXT        NOT NULL DEFAULT '',
    oauth_scope      TEXT        NOT NULL DEFAULT '',
    headers          JSONB       NOT NULL DEFAULT '{{}}'::jsonb,
    enabled          BOOLEAN     NOT NULL DEFAULT false,
    allowed_tools    JSONB,                             -- NULL = expose all discovered tools
    allowed_roles    JSONB,                             -- NULL/empty = all users; else only these roles may use it
    forward_identity BOOLEAN     NOT NULL DEFAULT false,
    created_by       TEXT        NOT NULL DEFAULT '',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (name),
    UNIQUE (slug)
);
CREATE INDEX IF NOT EXISTS idx_mcp_integration_enabled
    ON "{schema}".mcp_integration (enabled);
-- Idempotent migration for tenants provisioned before OAuth support: add the
-- columns and drop the old auth_type CHECK (which forbade 'oauth').
-- Self-heal tenants provisioned before the conversation RAG folder scope landed.
ALTER TABLE "{schema}".conversations ADD COLUMN IF NOT EXISTS scope JSONB;
ALTER TABLE "{schema}".mcp_integration ADD COLUMN IF NOT EXISTS token_url TEXT NOT NULL DEFAULT '';
ALTER TABLE "{schema}".mcp_integration ADD COLUMN IF NOT EXISTS oauth_client_id TEXT NOT NULL DEFAULT '';
ALTER TABLE "{schema}".mcp_integration ADD COLUMN IF NOT EXISTS oauth_scope TEXT NOT NULL DEFAULT '';
ALTER TABLE "{schema}".mcp_integration ADD COLUMN IF NOT EXISTS allowed_roles JSONB;
ALTER TABLE "{schema}".mcp_integration DROP CONSTRAINT IF EXISTS mcp_integration_auth_type_check;
-- Self-heal tenants provisioned before 'index_failed' existed. The status was
-- added to the pipeline WITHOUT widening this constraint, so the write that was
-- supposed to record an embedding failure raised CheckViolation instead, the
-- exception escaped convert(), and the row kept the 'converting' value written
-- moments earlier. Permanently: 'converting' is not a terminal state, so nothing
-- reported it, and the document simply never appeared in the index.
--
-- DROP-then-ADD rather than a new constraint name, so the constraint keeps the
-- name Postgres generated for it and re-running this is a no-op. CREATE TABLE
-- IF NOT EXISTS above cannot fix an existing tenant, which is the whole reason
-- this block exists.
ALTER TABLE "{schema}".documents DROP CONSTRAINT IF EXISTS documents_status_check;
ALTER TABLE "{schema}".documents ADD CONSTRAINT documents_status_check
    CHECK (status IN ('pending','converting','converted','indexed','index_failed','unsupported','error'));
'''


def tenant_ddl(tenant: str, dimension: int = 1024) -> str:
    """The idempotent DDL that provisions a tenant's schema + tables.

    ``dimension`` is the pgvector embedding width — must match the deployment's
    CSAI_EMBEDDING_DIMENSION / the chosen embedding model."""
    return _TENANT_DDL.format(schema=schema_name(tenant), dimension=int(dimension))


# Namespace for the provisioning advisory lock — fixed, so these locks cannot
# collide with any other advisory lock taken on this database.
_PROVISION_LOCK_CLASS = 0x0D15C


def ensure_tenant_schema(conn, tenant: str, dimension: int = 1024) -> str:
    """Create the tenant's schema + tables if absent (idempotent).

    ``conn`` is an open psycopg connection (the extensions must already exist at
    the database level). ``dimension`` sets the embedding column width. Returns
    the schema name.

    Serialised across PROCESSES by an advisory lock: idempotent DDL is not the
    same as concurrency-safe DDL, and two transactions running these statements
    at once take table locks in interleaved order, which Postgres resolves by
    killing one with DeadlockDetected. ``db.connect_for_tenant`` holds the
    matching in-process lock; this covers the separate API and worker processes,
    which cannot see each other's memo.

    Transaction-scoped, so the commit below releases it — this must therefore
    NOT be handed an autocommit connection, or the lock would be dropped before
    the DDL it guards."""
    name = schema_name(tenant)
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
                    (_PROVISION_LOCK_CLASS, name))
        cur.execute(tenant_ddl(tenant, dimension))
    conn.commit()
    return name
