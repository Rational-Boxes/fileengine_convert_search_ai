# Chat‑with‑AI

Permission‑scoped, RAG‑grounded, agentic chat over a user's FileEngine documents —
served by convert_search_ai (CSAI). This document describes **what is implemented
today** and a **plan for expansion**. It is the source of truth for the feature;
the [SPECIFICATION](./SPECIFICATION.md) covers the wider CSAI service.

---

## 1. What it is

A logged‑in user opens a chat and asks questions in natural language. The assistant
answers **grounded in the documents that user is allowed to read**, cites its
sources, and can optionally reach the public web and even write a generated report
back into the user's file storage. Every piece of retrieved content and every
citation is gated by the caller's live FileEngine **READ** permission — the AI can
never surface content the user couldn't open themselves.

```
        ┌─────────┐   WebSocket /chat    ┌──────────────────────────────────────┐
 user → │  SPA    │ ───────────────────► │  CSAI                                │
        │ (chat)  │ ◄─ token/citations ─ │  retrieve (pgvector, READ‑gated)     │
        └─────────┘                      │  + tools (web / files) + LLM stream  │
                                         └───────────┬──────────────────────────┘
                                     gRPC (as the user) │  CheckPermission(READ)
                                                        ▼
                                                  FileEngine core
```

Ingestion (event‑driven: extract → chunk → embed → index into pgvector) is covered
in the SPECIFICATION / DEVELOPMENT_PLAN; this doc starts at **retrieval + chat**.

---

## 2. Current features

### 2.1 Transport & turn protocol — WebSocket `/chat`

- **Connect / auth** (`api.py`, `http_auth.py`): a bearer token via the
  `Authorization: Bearer <token>` header **or** a `?token=<token>` query param
  (browsers can't set WS headers, so the query param is the SPA path). The token is
  the bridge‑issued JWT — verified locally with the shared secret, or by bridge
  introspection. Tenant is resolved from `X-Tenant` → Host subdomain → default.
  Auth is checked **before** `accept()`; failure emits an `error` event and closes
  with code `4401`. Basic auth is **not** accepted on the socket.

- **Client → server** message:
  ```json
  {
    "message": "the user's question",          // required
    "system_prompt": "optional seed prompt",   // per‑conversation, client‑supplied
    "history": [{"role":"user","content":"…"}, …],   // prior turns, client‑supplied
    "k": 8,                                     // retrieval depth (capped at CSAI_MAX_CHAT_K=12)
    "web_search": true,                         // optional per‑turn override
    "conversation_id": "hexuuid"               // optional — resume/append to a chat
  }
  ```

- **Server → client** event stream (`chat.py`, `api.py`):
  | event | payload | meaning |
  |---|---|---|
  | `conversation` | `{id}` | conversation id (after the user message is persisted) |
  | `token` | `{text}` | a streamed delta of the answer |
  | `tool_call` | `{name, args}` | the model invoked a tool |
  | `tool_result` | `{name}` | a tool returned |
  | `citations` | `{citations:[…]}` | the source list for the turn (once, near the end) |
  | `done` | — | end of turn |
  | `error` | `{error}` | failure (auth, serialization, generation) |

- **Turn lifecycle:** persist the user message → stream the answer → persist the
  assistant message + citations. Persistence is best‑effort (a DB blip logs but
  never breaks the live answer).

### 2.2 Retrieval (RAG)

- **Vector search** (`retrieval.py`, `vectorstore.py`): the query is embedded and
  matched against the tenant's `chunks` table using a **pgvector HNSW** index with
  **cosine** distance. To survive permission filtering, it **over‑fetches**
  `max(k·4, k)` candidates, then keeps the first `k` the user may read.
- **Per‑chunk permission gating (query time)** (`permissions.py`): for each
  candidate, `PermissionGate.can_read()` calls the core's `CheckPermission(READ)`
  **as the requesting user**. Results are cached per `(tenant, user, file_uid)` with
  a TTL (`CSAI_PERMISSION_CACHE_TTL`, default 300 s) and are **fail‑closed** (any
  error ⇒ denied). Cache entries are evicted early on core `acl.changed` / `role.*`
  events (`invalidate_resource / _member / _tenant`).
- **Index‑time is ACL‑blind by design:** every chunk is indexed
  (`CSAI_INDEX_BYPASS_ACL`), and access is enforced only at retrieval — so a
  permission change takes effect immediately without re‑indexing.
- **Chunking** (`chunking.py`): Markdown/structure‑aware, ~1200 chars per chunk with
  ~150‑char overlap; oversized blocks (e.g. tables) are kept whole.
- **Context budget** (`guards.py`): retrieved chunks are trimmed to
  `CSAI_MAX_CONTEXT_CHARS` (default 12000; the top chunk is always kept).
- **Graceful degradation:** if the vector store is unavailable, retrieval returns
  empty and the model answers from general knowledge / web (logged).

### 2.3 Agentic tools

The model runs a bounded tool‑use loop (`CSAI_TOOL_MAX_ITERATIONS`, default **8**)
via the provider's native function‑calling (`chat.py`, `llm_tools.py`,
`providers/chat.py`). Tools available:

- **`list_folders`** *(on by default, `CSAI_CHAT_DOCUMENT_TOOL`)* — browse the
  user's storage under a path, executed **as the user** (ACLs apply). Used so the
  model can pick a real save destination.
- **`web_search`** *(off by default, `CSAI_WEB_SEARCH_ENABLED`)* — public‑web
  search (DuckDuckGo by default; `fake`/`none` also selectable). Returns titled
  results with snippets, each added to the citation list.
- **`fetch_page`** *(off by default, needs web search + `CSAI_WEB_FETCH_PAGES`)* —
  fetch and extract the readable text of one HTTPS URL, **SSRF‑guarded**
  (`webfetch.py`: public IPs only, https only, redirects re‑validated, size/timeout
  capped, text content types only).

**Report saving** (`llm_tools.py`, marker‑driven, on by default) — the model can
wrap a generated report in `[[SAVE_REPORT path="…" file="…" title="…"]] … [[/SAVE_REPORT]]`
markers; CSAI parses them, renders Markdown → styled HTML, and writes the file into
FileEngine **as the user** (subject to WRITE on the destination; capped at
`CSAI_CHAT_DOCUMENT_MAX_BYTES`, default 5 MB). The core generates a PDF preview.
*(Planned: attach a tamper‑evident chat provenance log to every saved report — see
§4.6.)*

### 2.4 Citations

One `citations` event per turn (`chat.py`), unified numbering:
```json
{ "marker": 1, "kind": "doc",  "file_uid": "…", "title": "…" }
{ "marker": 2, "kind": "web",  "url": "https://…", "title": "…" }
```
Document citations are deduped per `file_uid` (documents [1..n]), web citations
follow ([n+1..]). The SPA renders inline `[n]` markers — `doc` links to the file in
the browser, `web` links out. Citations are persisted with the assistant message so
a re‑opened conversation still resolves its markers.

### 2.5 Conversations & history

- **Persistence** (`conversations.py`, `schema.py`): per‑tenant Postgres schema,
  `conversations` + `conversation_messages(role, content, citations JSONB)`, scoped
  by `user_id` (a user only sees their own).
- **CRUD** (`api.py`): `GET/POST /conversations`, `GET/DELETE /conversations/{id}`
  (404 if not yours). New chats auto‑title from the first message.
- **System prompt & history are client‑supplied** today: the SPA passes the seed
  `system_prompt` and the prior `history` on each turn (the server stores messages
  but does not yet reconstruct model context from them automatically — see §4).

### 2.6 Providers

Pluggable, provider‑agnostic (`providers/`):

- **Chat:** `anthropic` (default, `claude-sonnet-4-6`), `openai` (any
  OpenAI‑compatible `base_url`), `ollama` (local), plus `echo` / `echo-tools` dev
  stubs (offline, deterministic). All real providers support tool‑calling.
- **Embeddings:** `hash` (offline default), `voyage`, `openai`, `ollama`,
  `openai-compatible`. `CSAI_EMBEDDING_DIMENSION` (default 1024) is load‑bearing —
  it must match the pgvector column fixed at tenant provisioning.

### 2.7 Security & audit invariants

- **Every result and citation is gated by the end‑user's live READ permission**
  (never the service/agent identity); fail‑closed.
- **Tenant isolation** by Postgres schema; **conversation ownership** by `user_id`.
- **Audit is content‑free** (`audit.py`): `chat`, `web_search`, `fetch_page`,
  `list_folders`, `save_report`, `search`, `document_text` record actor, tenant,
  result and counts — **the query text and document content are never logged**.
- **SSRF protection** on `fetch_page`; **size/rate guards** on query length,
  retrieval `k`, context, and response bytes (`guards.py`).

---

## 3. Configuration quick reference

| Env var | Default | Purpose |
|---|---|---|
| `CSAI_CHAT_PROVIDER` | `anthropic` | chat backend (`anthropic`/`openai`/`ollama`/`echo`) |
| `CSAI_CHAT_MODEL` | `claude-sonnet-4-6` | chat model |
| `CSAI_CHAT_BASE_URL` / `CSAI_CHAT_API_KEY` | — | OpenAI‑compatible endpoint + key |
| `CSAI_CHAT_MAX_TOKENS` | `8192` | max answer tokens (roomy for reports) |
| `CSAI_EMBEDDING_PROVIDER` | `hash` | embedding backend |
| `CSAI_EMBEDDING_DIMENSION` | `1024` | **load‑bearing** — must match the pgvector column |
| `CSAI_MAX_CHAT_K` | `12` | retrieval depth cap |
| `CSAI_MAX_CONTEXT_CHARS` | `12000` | context budget fed to the model |
| `CSAI_PERMISSION_CACHE_TTL` | `300` | READ‑permission cache TTL (s) |
| `CSAI_CHAT_DOCUMENT_TOOL` | `true` | enable `list_folders` + report saving |
| `CSAI_CHAT_DOCUMENT_MAX_BYTES` | `5 MB` | saved‑report size cap |
| `CSAI_WEB_SEARCH_ENABLED` | `false` | master switch for web tools |
| `CSAI_WEB_SEARCH_DEFAULT` | `false` | web search on by default per turn |
| `CSAI_WEB_SEARCH_PROVIDER` | `duckduckgo` | `duckduckgo`/`fake`/`none` |
| `CSAI_WEB_SEARCH_RESULTS` | `5` | results per search |
| `CSAI_WEB_FETCH_PAGES` | `false` | enable `fetch_page` |
| `CSAI_TOOL_MAX_ITERATIONS` | `8` | tool‑use rounds per answer |
| `CSAI_BRIDGE_URL` / `FILEENGINE_JWT_SECRET` | — | token verification path |

---

## 4. Planned expansion

Grouped by theme; roughly ordered within each by value‑to‑effort. Nothing here is
implemented yet.

### 4.1 Retrieval quality
- **Hybrid retrieval** — blend pgvector ANN with the existing full‑text/fuzzy
  search (`search.py`) and fuse (RRF), so exact‑term and semantic matches both win.
- **Relevance threshold + "no grounded answer"** — a distance/score floor so weak
  matches don't masquerade as sources; when nothing clears the bar, say so (and fall
  back to web/general knowledge only if allowed).
- **Reranking** — an optional cross‑encoder / provider rerank pass over the
  over‑fetched candidates before the top‑`k` cut, improving citation precision.
- **Scoped retrieval** — let a turn restrict RAG to a folder, a tag/claim, or a
  selected set of documents ("chat about this folder").
- **Larger‑than‑context handling** — map‑reduce / iterative retrieval for questions
  that need many chunks beyond the context budget.

### 4.2 Conversational experience
- **Server‑side context** — reconstruct model context from the stored conversation
  instead of trusting client‑supplied `history` (with server‑side token budgeting
  and summarization of older turns).
- **Persisted per‑conversation system prompt** — store the seed prompt with the
  conversation so it survives reloads and isn't re‑sent each turn.
- **Streaming citations** — emit citations incrementally as chunks/tool results are
  chosen, so the UI can light up markers mid‑answer.
- **Follow‑up suggestions & clarifying questions** — model‑proposed next questions;
  ask‑to‑disambiguate when a query is under‑specified.
- **Feedback loop** — thumbs up/down + reason on answers, persisted for evaluation
  and prompt/retrieval tuning.
- **Regenerate / stop / edit‑and‑resend** turn controls.

### 4.3 Agentic capabilities
- **Document‑native tools** — `summarize_document`, `compare_documents`,
  `extract_fields` (structured extraction to JSON), `answer_from_document(uid)` for
  a user‑pinned file.
- **Template‑driven generation** — generate a report from a chosen template/skeleton
  document, not just free‑form Markdown.
- **More web backends** — Brave/Bing/Google/SearXNG providers behind the same tool
  contract; per‑tenant provider choice.
- **Multi‑modal** — reason over page images / diagrams / rendered CAD previews the
  core already produces (image inputs to vision‑capable models).
- **Tool budget & policy** — per‑tenant/per‑user caps on web calls, iterations, and
  token spend, surfaced as config + admin controls.

### 4.4 Governance, safety, cost
- **Per‑tenant admin controls** — enable/disable web search, report saving, and
  provider/model selection per tenant from the admin console (ldap_manager).
- **PII / injection defenses** — prompt‑injection hardening for fetched web content
  and documents (treat retrieved text as untrusted), optional PII redaction in
  citations.
- **Answer caching** — cache embeddings and (optionally) answers for identical
  query + document‑set + permission fingerprint.
- **Cost & usage metering** — per‑user/tenant token and tool accounting for billing
  and quotas (building on the content‑free audit stream).

### 4.5 Observability & evaluation
- **Groundedness scoring** — flag/annotate claims not supported by a citation.
- **Eval harness** — golden Q/A sets per tenant, retrieval hit‑rate and answer‑
  quality regression tracking in CI.
- **Trace view** — an admin/debug view of a turn: retrieved chunks (redacted),
  tool calls, and final citations, for support and tuning.

### 4.6 Report provenance: attached chat log

**Goal.** A report generated from a chat is an AI artifact — it must be
**auditable and attributable**. Every saved report will carry a durable log of the
chat that produced it, including **the identity of the user who was chatting**, so
anyone reviewing the report can see *who* asked for it and *how* it came to be, and
so an org has an accountability trail for AI‑authored documents.

**What is captured (the provenance record).**
- The **chatting user's identity** (uid/email) and tenant, plus the timestamp the
  report was generated.
- The **full conversation transcript** that led to the report — every turn of the
  chat (user prompts + assistant answers) up to the save, stored **in its entirety
  (no truncation)**.
- The **grounding**: the citations/sources used (document `file_uid`s + any web
  URLs) and the retrieval parameters.
- The **generation context**: chat provider + model, the (system) prompt in effect,
  and the `conversation_id`.
- Two renderings: a **machine‑readable JSON** record and a **human‑readable HTML**
  transcript (so it previews cleanly).

**Storage — folded into the report's hidden child files.** The provenance log is
written as a **hidden child/sidecar of the report file**, in the same associated‑
artifact space the core already uses for renditions/previews — not as a visible
sibling in the folder. It travels and versions with the report, is created in the
same atomic "save‑as‑the‑user" step, and never clutters directory listings. (A new
reserved child kind, e.g. `provenance` / `chatlog`, alongside the existing preview
rendition.)

**Access — from the preview, where available.** When a document has a provenance
log, the **preview UI surfaces it** — e.g. an "🧾 Generated by chat · view log"
affordance in the preview / details drawer that opens the rendered transcript
showing who chatted, when, the prompts/answers, and the cited sources. Absent for
documents that weren't chat‑generated.

**Permissions & privacy.** The log **follows the report**: it is readable by anyone
who can READ the report (provenance is part of the artifact) and is written under
the creating user's identity. The transcript is the creator's *own* session content
— it exposes no other user's private chats. **Personally identifiable information
(PII) is redacted** from the stored transcript; **web‑fetched / internet content is
kept verbatim** (not redacted), since it is public reference material, not personal
data.

**Integrity.** Write the JSON record with a content hash (and, later, an optional
signature) so the provenance can't be silently altered after the fact —
"tamper‑evident."

**Decisions (settled).**
- **Full transcript** — the entire conversation up to the save is stored,
  untruncated (no size cap / no "generating turn only" trimming).
- **Redact PII, keep web content** — personally identifiable information is scrubbed
  from the stored transcript; web‑fetched / internet content is preserved verbatim.
- **Lifespan = the report's.** The log is a hidden child/sidecar, so it versions and
  is deleted **with** the report (child cascade); it has no separate retention or
  legal‑hold and follows the document's read access.
- **No backfill** — only reports generated after this ships get a log; existing
  reports are left as‑is.

**Touch points.** CSAI `llm_tools.py` (emit the record at `save_report`),
`conversations.py` (source the transcript), FileEngine core (a hidden‑child/sidecar
kind + its ACL semantics), and the SPA preview/details drawer (surface + render).

---

## 5. References

Implementation entry points: `api.py` (WS transport + turn flow), `chat.py`
(orchestration), `retrieval.py` / `vectorstore.py` / `chunking.py` (RAG),
`llm_tools.py` / `webfetch.py` (tools), `conversations.py` (history),
`permissions.py` / `guards.py` (gating), `providers/` (chat + embeddings),
`config.py` (all `CSAI_*` knobs). See also [SPECIFICATION](./SPECIFICATION.md),
[DEVELOPMENT_PLAN](./DEVELOPMENT_PLAN.md), [WEB_SEARCH_TOOL_PLAN](./WEB_SEARCH_TOOL_PLAN.md),
and [EVENT_CONTRACT](./EVENT_CONTRACT.md).
