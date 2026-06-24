# convert_search_ai — Development Plan

Status: **Design** (implementation not yet started — this project stops at design
documents for now). Companion: [`EVENT_CONTRACT.md`](./EVENT_CONTRACT.md),
[`SPECIFICATION.md`](./SPECIFICATION.md).

## 1. Purpose & scope

`convert_search_ai` is a Python/FastAPI microservice in the FileEngine ecosystem
(sibling to `mcp`, `http_bridge`, `webdav_bridge`). For files held in FileEngine
it provides, gated end-to-end by FileEngine permissions:

1. **Conversion** — a plugin framework that detects MIME type and produces
   presentation **renditions** (preview images, web PDF, video previews,
   thumbnails) stored as *hidden children* of the source file in FileEngine.
2. **Extraction** — where possible, converts document content to **Markdown**,
   stored in Postgres to drive search.
3. **Search** — permission-gated **full-text** (fuzzy) search over the extracted
   Markdown, plus a **text-request** API for downstream AI analysis services.
4. **Chat-with-documents** — RAG over chunked + vectorized content, served over
   **WebSockets**, every retrieved chunk and citation gated by the requesting
   user's read permission.

This service **is** the *"future external re-rendition service"* anticipated by
core's [`file_renditions.md`](../../file_engine_core/design_documents/file_renditions.md).

**Out of scope:** the advanced AI information-extraction microservice (a separate
service); it consumes the extracted Markdown via the text-request API.

## 2. Position in the ecosystem

| Dependency | Role | Dev endpoint |
|------------|------|--------------|
| FileEngine core (gRPC) | File content, renditions, **permission checks** | `:50051` |
| LDAP / OpenLDAP | Authentication + role authority (mirrors the bridges) | `:1389` |
| PostgreSQL (+ `pgvector`) | Extracted text, chunks, embeddings, job state | `:5434` (dev) |
| Redis | Event consumption + task queue (dev transport) | `:6379` |
| MinIO / S3 | Object store (reached **through** core, not directly) | `:9000` |

Conventions inherited from `mcp` (mirror exactly): `Config` read from environment
+ a working-dir `.env`; `FILEENGINE_*` env names; trusted-upstream
`AuthenticationContext{user, roles, claims, tenant}`; LDAP→`Identity`→bearer-token
auth with per-session tenancy; `pyproject.toml` src-layout package; pytest with
`@live` integration gating; a `Containerfile` for deployment.

## 3. Architecture

```
                 ┌─────────────────────── convert_search_ai ───────────────────────┐
  FileEngine     │                                                                  │
  core ──events──▶  Ingestion          Conversion pipeline        Indexing          │
  (generic file  │  (event consumer)──▶ (MIME → plugin)──┬──────▶ extract → Markdown │
   activity, via │   + backfill sweep                    │         chunk → embed     │
   pluggable     │                                       └─rendition writer          │
   broker)       │                                          (gRPC: hidden children)  │
                 │                                                  │                │
                 │   Postgres  ◀── documents / chunks / vectors ────┘                │
                 │      ▲                                                            │
  user requests  │      │  permission gate (core CheckPermission, ≤5 min cache)      │
  ──────────────▶│  Search API · Text-request API · Chat (WebSocket RAG)            │
                 └──────────────────────────────────────────────────────────────────┘
```

Four internal subsystems:

- **Ingestion** — consumes generic file-activity events (§4) and enqueues
  conversion/indexing work; a reconciliation sweep backfills history and repairs
  missed events.
- **Conversion pipeline** — MIME detection → plugin registry → renditions +
  Markdown extraction (§5).
- **Indexing** — extraction → Postgres full text (FTS) + chunking → pluggable
  embeddings → `pgvector` (§6, §7).
- **Serving** — search, text-request, and WebSocket RAG chat, all behind the
  permission gate (§8).

## 4. Event-driven ingestion (depends on core publisher)

Ingestion is **event-driven**. FileEngine core does not yet emit file-activity
events — adding a **generic, broker-agnostic event publisher to core (Redis in
dev)** is the agreed **next effort** and a hard dependency for live ingestion.
The contract both sides build against is specified in
[`EVENT_CONTRACT.md`](./EVENT_CONTRACT.md). Key properties:

- **Generic & shared:** core publishes file-activity events that *any* consumer
  may subscribe to; `convert_search_ai` is one subscriber. The contract is owned
  jointly, not specific to this service.
- **Consumer-agnostic transport:** the consumer talks to a small `EventSource`
  interface; Redis (Streams, with consumer groups) is the dev implementation,
  swappable to Kafka/NATS/etc. without touching ingestion logic.
- **Delivery:** at-least-once; processing is **idempotent** keyed by
  `(file_uid, source_version)`; per-`file_uid` ordering is best-effort.
- **Events handled:** created / updated (new version) / moved / renamed /
  deleted / restored. Deletes cascade to this service's rows; moves need no
  re-index (identity is the stable file UID).
- **Backfill / reconcile:** because events are go-forward only, a sweep walks
  FileEngine (per tenant) to (a) populate the initial corpus and (b) detect drift
  (files indexed but changed, or present but unindexed). Runs on first start and
  on a schedule.

Until the core publisher lands, ingestion can be developed against a **dev stub
publisher** emitting the same contract onto Redis, plus the reconcile sweep — no
consumer changes when core goes live.

## 5. Conversion plugin framework (Milestone 1 focus)

- **MIME detection** from content (magic bytes) with extension fallback.
- **Plugin registry:** each plugin declares the MIME types it handles and the
  outputs it produces. A single `ConversionPlugin` interface:
  - `supports(mime) -> bool`
  - `render(source) -> [Rendition]` (preview image(s), web PDF, video preview,
    thumbnail)
  - `extract(source) -> Markdown | None` (text content, when extractable)
- **Initial plugins:**
  - **Office / documents** → LibreOffice (headless) → PDF + Markdown/text.
  - **Images** → ImageMagick → thumbnail + web preview.
  - **Video** → FFMPEG → web-optimized preview + poster thumbnail.
  - **PDF / text / markdown** → native extractor → Markdown.
  - Unknown MIME with no plugin → recorded as `unsupported` (no failure).
- **Renditions are written back as hidden children** of the source file, per
  `file_renditions.md`:
  - Create child with `parent_uid = <source file uid>` via existing gRPC
    (`StreamFileUpload`); **no new core RPC**.
  - Name `<timestamp>-format.ext` (e.g. `20260623T0200Z-pdf.pdf`); identity is by
    `parent_uid`, one level deep, leaves only.
  - List/refresh via targeted `ListDirectory(<file-uid>)`.
  - Move/copy/delete of the source are handled by core (renditions follow / cascade).
- **Re-rendition:** on an `updated` event, regenerate renditions for the new
  version; supersede stale ones (naming carries the timestamp).
- **Heavy work** (LibreOffice, FFMPEG, embeddings) runs on **broker-backed
  workers** behind the same pluggable transport as events (Redis dev); jobs are
  retriable with a dead-letter path. The container ships LibreOffice, ImageMagick,
  and FFMPEG.

## 6. Storage & data model (Postgres)

Its **own** database/schema in the Postgres instance, **per-tenant partitioned**
(mirroring FileEngine's tenant isolation). Every row references the FileEngine
`file_uid` + `tenant` — nothing exists without a source document.

- `documents` — `file_uid`, `tenant`, `source_version`, `mime`, `content_md`,
  `status` (`pending`/`converted`/`indexed`/`unsupported`/`error`), timestamps.
- `chunks` — `id`, `file_uid`, `tenant`, `ordinal`, `text`, `embedding vector(N)`,
  `fts tsvector`. `N` is provider-dependent (§7) — vector column/dimension is
  managed per active embedding model; a model change is a migration, not a silent
  mismatch.
- `renditions` (optional cache/index) — mirror of what core holds, for status and
  idempotency; source of truth is FileEngine.
- Indexes: GIN on `fts` (+ `pg_trgm` for fuzzy), an ANN index (e.g. HNSW/IVFFlat)
  on `embedding`.

**Requires the `pgvector` extension** — confirm availability in the deployment
Postgres (not currently used elsewhere in the ecosystem).

## 7. Pluggable AI providers

Both embeddings and chat sit behind interfaces, selected by config (mirrors the
broker-agnostic stance):

- `EmbeddingProvider`: `embed(texts) -> [vector]`, exposes `dimension` and
  `model_id`. Implementations are deploy-time choices (hosted or self-hosted);
  the schema adapts to the active `dimension`.
- `ChatProvider`: streaming chat completion over a list of messages + retrieved
  context. Anthropic Claude is the ecosystem default and the reference
  implementation; the interface keeps the door open to others.
- Config declares the active providers, models, keys, and (for embeddings) the
  dimension. Switching embedding models triggers a re-embed migration.

## 8. Permission gating (non-negotiable)

Every piece of returned information is gated by the **requesting user's** FileEngine
read permission — search hits, text requests, and each RAG chunk/citation:

- On retrieval, call core `CheckPermission(file_uid, READ)` using the requester's
  `AuthenticationContext`; drop anything not readable **before** it reaches results
  or the LLM context.
- **Cache** decisions per `(user, file_uid)` for **≤ 5 minutes**, then refresh
  (per spec). Cache is fail-closed.
- Indexing uses the service's own agent identity; **retrieval never reuses it** —
  authorization is always evaluated as the end user.

## 9. Authentication & APIs

- **Auth:** LDAP (mirror `mcp`'s `ldap_auth`) → `Identity{user, roles, tenant}` →
  issued **bearer token**; `X-Tenant` / host scopes the session tenant. WebSocket
  connections authenticate on connect with the same token.
- **REST/HTTP:**
  - `POST /search` — FTS + fuzzy over Markdown; returns hits referencing
    `file_uid` (+ snippet), permission-filtered.
  - `GET /documents/{file_uid}/text` — extracted Markdown (text-request API).
  - Rendition listing/fetch stays in core + `http_bridge`
    (`GET /v1/files/{uid}/renditions`); this service *produces* renditions, the
    bridge *serves* them.
  - `POST /ingest/reconcile`, health/readiness, metrics.
- **WebSocket:** `/chat` — RAG session seeded by a **conversation-specific system
  prompt** supplied by the frontend; replies stream tokens and, where a document
  is referenced, include a citation linking to the file. Retrieval is scoped to
  content the connected user can read.

## 10. Milestones

Dependency order: convert/extract → store → search → chat. The chosen first
milestone is **conversion + renditions**.

- **M0 — Scaffolding.** Package/`pyproject`, `Config`, gRPC client + LDAP auth
  (port from `mcp`), `Containerfile` with LibreOffice/ImageMagick/FFMPEG, Postgres
  migrations baseline, CI + pytest harness with `@live` gating.
- **M1 — Conversion + renditions (first deliverable).** Plugin framework, MIME
  detection, Office/image/video/PDF plugins, rendition writer (hidden children via
  gRPC), worker/queue (Redis dev), reconcile sweep, **dev stub event publisher**
  against `EVENT_CONTRACT.md`. Exit: dropping a file results in correct hidden-child
  renditions, idempotently.
- **M2 — Extraction + full-text search.** Markdown extraction, `documents` table,
  Postgres FTS + `pg_trgm`, `POST /search` and text-request API, **permission gate
  + 5-min cache**. Exit: permission-correct fuzzy search referencing `file_uid`.
- **M3 — Vectorization + RAG chat.** Chunking, pluggable embeddings, `pgvector`
  ANN, WebSocket chat with per-conversation system prompt, streamed citations,
  per-user permission scoping. Exit: chat answers only from content the user may read.
- **Cross-cutting / parallel dependency:** core's generic Redis event publisher
  (the next effort) replaces the M1 stub with no consumer changes.

## 11. Testing

- **Unit (run anywhere):** MIME detection, plugin registry/dispatch, chunking,
  Markdown normalization, permission-cache TTL + fail-closed, `EventSource` adapter
  contract, provider interface fakes.
- **Integration (`@live`, gated on services up):** end-to-end convert→rendition
  against core+gRPC; FTS correctness; RAG retrieval permission filtering with two
  users of differing ACLs; event consume→index loop against the Redis dev publisher.
- Reuse the canonical dev identity (`testuser@rationalboxes.com`) and the
  config-derived-credentials pattern established in `mcp`'s tests (no hardcoded creds).

## 12. Risks & open items

- **Core event publisher** is a prerequisite for live ingestion (next effort);
  M1 proceeds on the stub + reconcile sweep until it lands.
- **`pgvector`** must be present in the Postgres deployment — verify early.
- **LibreOffice headless** stability/throughput for Office conversion; isolate in
  workers with timeouts.
- **Embedding dimension changes** require a re-embed migration — make the vector
  column/model versioned from day one.
- **Tenancy isolation** must hold across this service's own tables and the
  permission cache.
