# convert_search_ai

FileEngine microservice for **format conversion**, **search**, and **vector-backed
RAG chat** — every result and citation gated by the user's FileEngine read
permission. See [`design_documents/`](./design_documents) for the
[specification](./design_documents/SPECIFICATION.md),
[development plan](./design_documents/DEVELOPMENT_PLAN.md), and the
[event contract](./design_documents/EVENT_CONTRACT.md).

## Status

- **M0** — scaffolding: package, `Config`, LDAP auth + gRPC client, health/readiness,
  Postgres baseline, `@live`-gated pytest harness.
- **M1** — conversion + renditions: plugin framework, MIME detection, advanced
  structure/table-preserving PDF/Office extraction, idempotent hidden-child
  rendition writer, the event-driven ingest worker + reconcile sweep.
  Documents (PDF + Office) get a consistent preview set: an icon-sized
  `thumbnail` and a larger first-page `preview` image (poppler), plus — for
  Office formats — an inline `pdf` rendition (LibreOffice) for in-browser
  viewing; images get thumbnail/preview, video a poster + web clip.
- **M2** — extraction + search: per-tenant Postgres FTS + `pg_trgm` fuzzy over the
  extracted Markdown, bearer-token auth, a permission-gated search + text-request
  surface, and a permission cache that is both TTL-bounded (≤5 min) and
  **invalidated in real time** by the core's `acl.changed`/`role.*` events.
- **M3** — vectorization + RAG chat: heading-aware chunking, pluggable embeddings
  (offline `hash` default; Voyage; **any OpenAI-compatible endpoint** via
  `base_url`, including a local **Ollama**), pgvector ANN retrieval scoped by the
  user's read permission, and a WebSocket `/chat` streaming RAG answers + citations
  via pluggable chat providers (Claude or any OpenAI-compatible / Ollama endpoint;
  offline `echo` default).
- **Hardening** — production guards on every request surface (query/payload size
  caps, structured error mapping), a content-/secret-free audit log, and the
  full-stack `@live` end-to-end test (`tests/test_e2e_live.py`) covering
  event-driven ingest → pgvector index → permission-gated search → RAG chat →
  real-time permission-cache invalidation.

## API surface (`api.py`)

| Method | Path | Purpose |
|--------|------|---------|
| GET  | `/healthz` | liveness |
| GET  | `/readyz` | readiness (gRPC core + LDAP) |
| POST | `/auth/token` | LDAP bind → bearer token |
| GET  | `/whoami` | resolved identity |
| POST | `/search` | permission-gated full-text + fuzzy search |
| GET  | `/documents/{uid}/text` | extracted Markdown (READ-gated) |
| WS   | `/chat` | permission-scoped RAG chat (streamed tokens + citations) |
| POST | `/ingest/reconcile` | trigger a reconcile sweep |

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

## AI providers (embeddings + chat)

Both providers are pluggable (DEVELOPMENT_PLAN §7). Defaults are offline and
dependency-free — `hash` embeddings + `echo` chat — so dev and tests run with no
API key. Real providers are opt-in extras:

```bash
pip install -e ".[openai]"      # the OpenAI SDK — the client for OpenAI, Ollama, vLLM, LM Studio, …
# or ".[anthropic]" (Claude, the chat default) / ".[voyage]" (Voyage embeddings)
```

### Local Ollama (or any OpenAI-compatible endpoint)

The `ollama` provider speaks the OpenAI API, so the same `openai` SDK drives it —
only the base URL differs (it defaults to a local Ollama). Pull the models, then
configure `.env`:

```bash
ollama pull nomic-embed-text
ollama pull llama3.1                   # any chat model you have pulled
```

```ini
CSAI_EMBEDDING_PROVIDER=ollama
CSAI_EMBEDDING_MODEL=nomic-embed-text
CSAI_EMBEDDING_DIMENSION=768           # MUST equal the model's native output width
CSAI_CHAT_PROVIDER=ollama
CSAI_CHAT_MODEL=llama3.1
# CSAI_{EMBEDDING,CHAT}_BASE_URL default to http://localhost:11434/v1 for `ollama`;
# point them at OpenAI / vLLM / LM Studio / a remote Ollama instead if needed.
# CSAI_{EMBEDDING,CHAT}_API_KEY is optional for Ollama (ignored), required for OpenAI.
```

> **Embedding dimension is load-bearing.** `CSAI_EMBEDDING_DIMENSION` sets the
> pgvector column width when a tenant is provisioned, and must match the model
> (nomic-embed-text → **768**, text-embedding-3-small → 1536, voyage-3 → 1024).
> Set it **before** provisioning a tenant; switching models later is an explicit
> migration (ALTER the column + re-embed), never a silent mismatch.

Plain `openai` / `openai-compatible` providers work identically — set the
provider name, `*_BASE_URL`, and `*_API_KEY`. See `.env.example` for the full
list of provider knobs.

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
- **One login, two services.** With `CSAI_BRIDGE_URL` set, a bearer token minted
  by the HTTP bridge (LDAP **or** OAuth) is accepted here via the bridge's
  `/v1/auth/introspect` (cached briefly). The bridge is the upstream token
  authority — no shared secret or common token format — so an SPA logs in once
  and calls both services with the same token. The service still issues its own
  tokens / accepts Basic auth when coordination is off (standalone).
