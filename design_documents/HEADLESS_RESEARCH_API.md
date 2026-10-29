# Headless research API (desktop applications)

Status: **investigation** (branch `research/headless-research-api`, opened 2026-10-29).
Nothing here is implemented yet.

## 1. Goal

Desktop applications (Reviera first) drive the research tooling without a
person in a browser. In headless mode it must:

- do **focused, structured fact extraction** from the documents in a specific
  folder. This is the strongest case and the main mode. Reviera pulls given
  values (manufacturer, model, dimensions, fire rating, EPD figures,
  certificate numbers and expiry dates, ...) from datasheets and
  sustainability certificates to populate model metadata;
- still produce **reports**, by the **existing technique** (owner,
  2026-10-29): the report always lives in FileEngine, so its provenance
  sidecar is kept; the desktop application downloads the file;
- **stream** progress and results (SSE), with a non-streaming option for
  simple callers.

Desktop apps sign in with generated credentials: API key → `/v1/auth/token` →
session bearer, renewed while the key is live (http_bridge 1.9.66).

## 2. What exists (reviewed 2026-10-29, csai master 81343b3)

**Usable as is**

- `ChatService.answer()` (`chat.py`) is a transport-neutral generator of events:
  `token`, `tool_call`, `tool_result`, `citations`, `report_saved`, plus `error`
  from the transport. It already does permission-scoped retrieval, folder
  scoping (`scope_folder_uids`), the document search/read tools, optional web
  search, MCP tools and pinned-destination reports (`report_target`).
- Authentication: `resolve_identity` → `BridgeTokenVerifier` verifies http_bridge
  HS256 tokens locally (`jwt_verify.py`) and checks the jti revocation list, so
  API-key session tokens already work here. Tenant binding is enforced.

**Shaped for the browser**

| # | Gap | Where |
|---|---|---|
| 1 | Only transport is `WS /chat`. Errors are frames, not statuses. A client disconnect does not stop the provider call (the worker thread runs to completion). | `api.py` `chat`, `_stream_answer` |
| 2 | No structured output. Every prompt asks for prose with `[n]` markers and `(file <uid>)` references meant for the SPA linkifier. Providers have no JSON mode or schema parameter. | `chat.py` `_INSTRUCTIONS*`, `providers/chat.py` |
| 3 | A report is only produced into FileEngine (as `.docx`) when a folder and filename are pinned. The draft cannot be returned to the caller. | `_save_marked_reports`, `llm_tools.save_report_document` |
| 4 | MCP tool calls block on an interactive `tool_consent` reply (120 s timeout, then deny). A headless caller would stall on every external tool. | `consent.py`, `_stream_answer.read_control` |
| 5 | No per-user or per-key rate limit or token budget. `CSAI_MAX_QUERY_CHARS` = 1000 is too small for headless prompts that carry instructions or data. | `config.py`, `guards.py` |
| 6 | Every turn is persisted to the user's conversation list. | `_begin_turn` / `_end_turn` |

Production model: `openai-compatible` → DeepInfra, `deepseek-ai/DeepSeek-V4-Flash`
(scripts `roles/convert_search_ai/defaults/main.yml`). Whether it honours
`response_format` (`json_object` / `json_schema`) must be measured (§5).

## 3. Proposed shape

New routes over the existing services; `WS /chat` is unchanged for the SPA.
Every route answers `text/event-stream` (default) or `application/json`
(wait, return the final result only).

### 3.1 Extraction — `POST /csai/v1/extract` (the main mode)

Extraction is not chat. RAG top-k retrieval across the tenant is the wrong
tool: a value buried in one table of one datasheet is easy to miss, and a
value from the wrong product's datasheet is worse than none. Extraction works
**per document**: each file in scope is read whole (its extracted Markdown,
paged when long) and asked the same schema, independently.

**Source text.** Each file is read from csai's own stored Markdown,
`documents.content_md`: the whole document, extracted at index time with its
table structure kept as Markdown tables. Nothing is converted again at
request time. `documents.source_version` is the FileEngine version that text
came from, and becomes the result's version stamp.

**Stale or missing text.** Indexing is asynchronous, so the stored text can
lag the file. Before reading, compare `source_version` with the file's current
version (`Stat`):

- current: extract;
- behind, or `status` pending: re-index that file first (the ingest path
  already exists), then extract; the `item` is late, not wrong;
- `status` failed, or no text (`content_md` NULL; for example a scanned
  certificate with no text layer): `skipped` with the reason, never an empty
  "nothing found".

**Markdown is the input, by decision** (owner, 2026-10-29): extraction reads
`content_md` and nothing else until advanced multimodal model support exists.
A file with no usable text (a scan with no text layer) is reported as
`skipped: no text` so the caller knows to check it by hand; there is no OCR or
image pass in this work.

```json
{
  "folder_uid": "<uid>",
  "recursive": false,
  "files": ["<uid>"],
  "match": { "extensions": [".pdf"], "name": "*datasheet*" },
  "schema": { "...": "JSON Schema of the values wanted" },
  "instructions": "optional guidance (units, which product, ...)"
}
```

**Target: a folder or a list of document ids, both supported** (owner,
2026-10-29). Exactly one per request; both or neither is a 400.

- `folder_uid`: every file in the folder (`recursive` for subfolders),
  optionally narrowed by `match`. Renditions and other hidden children are
  never included.
- `files`: explicit document ids, which may sit in different folders (the
  documents a model element already links to). Order is kept, duplicates are
  dropped, and an id the caller cannot use is reported as `skipped:
  not found or not readable`, never silently dropped: the caller named it.
  One reason for both, so the route cannot be used to probe which ids exist.
  Check existence explicitly as well as READ: the core's `CheckPermission`
  answers true for an id that does not exist.

The schema describes the values; the service wraps each one with evidence, so
the caller never has to ask for it:

```json
{ "fire_rating": { "value": "EI 60", "unit": null,
                   "evidence": { "quote": "Fire resistance: EI 60", "page": 3 },
                   "found": true } }
```

SSE events:

| event | data |
|---|---|
| `plan` | the files in scope (uid, name, version), so a client can show progress |
| `item` | one file's result: `{file_uid, name, version, data, valid, skipped?}` |
| `error` | `{file_uid?, error, kind}`. A per-file error does not end the run |
| `done` | summary: counts of extracted, not found, skipped, failed |

Rules:

- **Absent means `found: false`, never a guess.** Each value needs a
  supporting quote from that document. A value without one is dropped, and
  the drop is counted.
- **Validated** against the schema, with one repair attempt per file.
- **Version-stamped.** Each `item` names the version it was read from, so
  Reviera can tell when a datasheet has been revised since a value was taken.
- Only files the caller can READ; unreadable ones are skipped silently,
  exactly as search does.
- No web search and no MCP tools: the answer comes from the documents only.
- Cancelling (client disconnect) stops after the current file.
- Results are not written anywhere by the service. Reviera decides what
  becomes model metadata.

### 3.2 Reports — the existing technique

Reports use today's pinned-destination report mode unchanged: the model writes
`[[SAVE_REPORT ...]]`, the service saves a `.docx` with its provenance sidecar
into the pinned folder, and emits `report_saved {uid, name, path}`. The desktop
application then downloads the file through the REST door
(`GET /v1/files/{uid}/content`). The SSE route only needs to carry the same
events; the client relies on `report_saved`, not on the streamed tokens (which
include the markers and a server-written confirmation line).

### 3.3 Questions — `POST /csai/v1/research`

The existing chat (answer mode) over SSE, for a desktop "ask about these
documents" panel. Same body as a `WS /chat` frame, plus `persist` (default
false). Lowest priority of the three.

### 3.4 Common behaviour

- Sign in with the REST door's bearer, including API-key sessions.
- MCP tools omitted by default (headless cannot answer a consent prompt).
- Larger headless input cap; rate limit and token budget (§4).
- Audit records route, mode, file count, and the API key id (`skid`).
- `X-Accel-Buffering: no` on SSE; check the `/csai/` proxy read timeout.

## 4. Open decisions (owner)

1. Rate limits and budget: per user, per API key, or both?
2. A separate `ai` scope on generated credentials (research access without
   full REST access), now or later? Today `api` covers both.
3. Cache extractions? Keyed by (file, version, schema hash), a repeat request
   for an unchanged datasheet would cost nothing. Where would the cache live:
   csai's own table, or FileEngine metadata on the source file?

## 5. Investigation tasks

- [ ] Measure DeepSeek V4 Flash on DeepInfra: does `response_format`
      `json_object` / `json_schema` work? How often is output invalid without
      enforcement, and how often does it invent a value that is not in the
      document (the evidence rule catches these: measure how many)?
- [ ] Same for the Anthropic provider (tool-forced output as the schema route).
- [ ] Long documents: page through the extracted text per file, or retrieve
      within the one file first? Measure cost per datasheet.
- [ ] Cancellation: close the provider stream from the worker thread when the
      SSE client goes away (OpenAI SDK `completion.close()`).
- [ ] nginx: does the `/csai/` location buffer or time out SSE today?
- [ ] Prototype `/v1/extract` and an end-to-end test driven by an API-key
      session token over a fixture folder of datasheets, as Reviera would.
