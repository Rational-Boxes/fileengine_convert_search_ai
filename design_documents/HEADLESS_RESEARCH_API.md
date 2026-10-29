# Headless research API (desktop applications)

Status: **investigation** (branch `research/headless-research-api`, opened 2026-10-29).
Nothing here is implemented yet.

## 1. Goal

Desktop applications (Reviera first) drive the research chat without a
person in a browser. In headless mode the chat must:

- return **structured JSON** that a program can consume, as the main mode;
- still be able to produce a **drafted document**;
- **stream** its progress and answer (SSE), with a non-streaming option for
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

A new route over the same `ChatService`; `WS /chat` is unchanged for the SPA.

```
POST /csai/v1/research
Accept: text/event-stream      (default — streamed)
Accept: application/json       (wait, return the result only)
```

```json
{
  "message": "...",
  "mode": "json | document | answer",
  "schema": { "...": "JSON Schema (json mode)" },
  "document": { "format": "markdown",
                "save_to": { "folder_uid": "...", "filename": "..." } },
  "scope_folders": ["<uid>"],
  "web_search": false,
  "k": 8,
  "conversation_id": null,
  "persist": false,
  "tools": { "mcp": "deny" }
}
```

SSE events, reusing the engine's names:

| event | data |
|---|---|
| `token` | streamed text (in json mode, the raw JSON as it is generated: progress only) |
| `tool_call` / `tool_result` | progress |
| `result` | the authoritative outcome (below) |
| `error` | `{error, kind}`; HTTP status is used where the failure precedes the stream |
| `done` | end |

`result` by mode:

- **json**: `{data, valid, citations}`. Parsed, validated against `schema`,
  with one repair attempt on failure. Citations are structured
  (`file_uid`, `title`, `url`, `kind`), not markers in text.
- **document**: `{title, markdown, citations, saved?: {uid, path}}`. The draft
  is always returned; it is also saved through the existing
  `save_report_document` if `save_to` is given.
- **answer**: the prose answer plus structured citations.

Behaviour:

- Headless modes get their own system instructions (JSON only, matching the
  schema; no prose, no `(file <uid>)` link convention).
- MCP tools are omitted by default (`tools.mcp: "deny"`). A tenant-admin
  pre-approved list is a possible later option.
- Client disconnect cancels generation.
- `persist: false` by default, so automated calls do not fill the sidebar.
- Separate, larger headless input cap.
- Rate limit and token budget (see §4).
- Audit records mode, headless, and the API key id (`skid`) when present.
- `X-Accel-Buffering: no` on SSE responses; check the `/csai/` proxy read
  timeout covers a long answer.

## 4. Open decisions (owner)

1. Rate limits and budget: per user, per API key, or both?
2. A separate `ai` scope on generated credentials (research access without
   full REST access), now or later? Today `api` covers both.
3. Should headless json mode allow web search by default, or only on request?

## 5. Investigation tasks

- [ ] Measure DeepSeek V4 Flash on DeepInfra: does `response_format`
      `json_object` / `json_schema` work, through streaming and through the
      tool loop? How often is the output invalid without enforcement?
- [ ] Same for the Anthropic provider (tool-forced output as the schema route).
- [ ] Cancellation: can the provider stream be closed from the worker thread
      when the SSE client goes away (OpenAI SDK `completion.close()`)?
- [ ] nginx: does the `/csai/` location buffer or time out SSE today?
- [ ] Citation shape for json mode: where they live in `data` versus beside it.
- [ ] Prototype the route and an end-to-end test driven by an API-key session
      token, as a desktop client would.
