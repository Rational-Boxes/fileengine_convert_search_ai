# convert_search_ai

FileEngine microservice for **format conversion**, **search**, and **vector-backed
RAG chat** — every result and citation gated by the user's FileEngine read
permission. See [`design_documents/`](./design_documents) for the
[specification](./design_documents/SPECIFICATION.md),
[development plan](./design_documents/DEVELOPMENT_PLAN.md), and the
[event contract](./design_documents/EVENT_CONTRACT.md).

## Status — M0 (scaffolding)

This is the M0 skeleton: package layout, environment `Config`, the LDAP auth +
gRPC core client (reused from the FileEngine ecosystem), a FastAPI app with
health/readiness, the Postgres baseline migration, and a pytest harness with
`@live` gating. Conversion (M1), search (M2), and RAG chat (M3) are built on top.

## Layout

```
src/convert_search_ai/
  config.py       environment-driven Config (FILEENGINE_* shared, CSAI_* local)
  ldap_auth.py    LDAP bind + role resolution -> Identity (ported from mcp)
  _client.py      bootstrap the reused `fileengine` Python client
  core_client.py  build identity-bound ManagedFiles (end-user vs agent)
  schema.py       per-tenant `tenant_<tenant>` schema model + DDL (mirrors core)
  db.py           psycopg access; search_path scoped to the tenant schema
  app.py          FastAPI app (/healthz, /readyz) + `convert-search-ai` entrypoint
migrations/0001_baseline.sql   database-wide extensions (vector, pg_trgm)
tests/            unit tests + @live integration smoke tests
Containerfile     image with LibreOffice / ImageMagick / FFmpeg
```

## Develop

```bash
pip install ../python_interface      # the reused FileEngine gRPC client
pip install -e ".[dev,pdf]"          # 'pdf' = pdfplumber for table-preserving extraction
cp .env.example .env                 # fill in agent creds etc.
pytest -q                            # unit tests; @live tests need LDAP + core
convert-search-ai                    # serve the FastAPI app (uvicorn)
```

Install the database-wide extensions once (needs `pgvector`), then provision a
tenant's schema + tables on demand (mirrors the core's `tenant_<tenant>` model —
the schema *is* the tenant, no tenant column):

```bash
psql "host=localhost port=5434 dbname=convert_search_ai user=... password=..." \
  -f migrations/0001_baseline.sql
python -c "from convert_search_ai.config import Config; \
           from convert_search_ai.db import provision_tenant; \
           provision_tenant(Config(), 'default')"
```

## Key invariants

- **Permission gating is non-negotiable.** Retrieval is always evaluated as the
  *end user* via an identity-bound gRPC client (`core_client.client_for`); the
  agent identity is used only for indexing / rendition writes.
- **Tenant isolation mirrors the core.** Each tenant's storage is a
  `tenant_<tenant>` Postgres schema (the schema is the tenant — no tenant column);
  queries are scoped by `search_path`, the same relationship the core uses.
- **Reuses, not reimplements.** The gRPC client is `fileengine` from
  `python_interface`; LDAP auth mirrors `mcp`/the bridges; events come from the
  core publisher per `EVENT_CONTRACT.md`.
