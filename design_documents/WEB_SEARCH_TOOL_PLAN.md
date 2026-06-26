# convert_search_ai — Web Search Tool for Chat-with-Documents

Status: **Design / for review** (implementation not started). Companion:
[`DEVELOPMENT_PLAN.md`](./DEVELOPMENT_PLAN.md) (§7 providers, §8 permission gating,
§11 testing), [`EVENT_CONTRACT.md`](./EVENT_CONTRACT.md).

## 1. Goal

Give chat-with-documents the ability to **combine context from the user's document
set with results from the public internet**. The internet access is exposed to the
LLM as a **callable tool** (the model decides when to search), the search backend is
**configurable and pluggable**, and the **default backend is DuckDuckGo** (no API
key). The document-set RAG path and its non-negotiable permission gating are
preserved unchanged.

Why a tool (not retrieval-augmentation): the model searches only when it judges the
documents insufficient, can reformulate the query, and can search iteratively —
yielding better answers and a clear "the assistant chose to search the web" UX,
at the cost of a per-provider tool-calling integration (§4).

## 2. Current architecture (what we build on)

- `chat.py::ChatService.answer(identity, message, system_prompt, history, k)` — sync
  generator of events. Today: `guards.check_query` → `Retriever.retrieve()`
  (permission-scoped vector chunks) → `guards.trim_context` → `_build_system()`
  (numbered `[n]` context + the strict `_INSTRUCTIONS` *"answer using ONLY the
  provided context"*) → `self.chat.stream(messages, system)` → `{type:token}`
  deltas → one `{type:citations}` of `{file_uid, marker}`.
- `providers/base.py::ChatProvider.stream(messages, *, system)` — **text-only**
  streaming. Implementations: `AnthropicChatProvider` (default,
  `claude-sonnet-4-6`), `OpenAICompatibleChatProvider` (OpenAI/Ollama/vLLM/…),
  `EchoChatProvider` (offline). **No tool-calling today.**
- `providers/factory.py::make_chat_provider(config)` — name-dispatch, lazy imports,
  offline fake default. The pattern to mirror for new providers/tools.
- `api.py` WS `/chat` — runs the sync generator on a worker thread
  (`_stream_answer`), bridging to the socket; payload carries
  `message`/`system_prompt`/`history`/`k`. Frontend `chatService.ts` parses
  `token`/`citations`/`done`/`error`.
- `config.py` — `_env`/`_first`/`_bool` knobs; provider knobs `CSAI_CHAT_PROVIDER`,
  `CSAI_CHAT_MODEL`, etc. Guards in `guards.py`; `audit.record` per chat.

**Key constraint:** the `ChatProvider` interface is stream-only, so exposing a tool
to the LLM requires extending the provider abstraction with a tool-calling loop
(§4). This is the main architectural lift.

## 3. Design overview

Two new sub-systems plus an agentic loop, all behind config:

1. **Search backends** — a `WebSearchProvider` abstraction (mirrors the embedding/
   chat provider pattern) with a DuckDuckGo default and offline fake. §5.
2. **Tool layer** — a generic `Tool` (name, JSON schema, `run(args, ctx)`), and a
   `web_search` tool backed by the configured `WebSearchProvider`. Extensible to a
   future `fetch_page` tool or a `document_search` tool. §6.
3. **Tool-calling chat providers** — extend `ChatProvider` so Anthropic and
   OpenAI-compatible providers can be given tool schemas and drive a tool loop;
   providers that can't do tools degrade gracefully. §4.
4. **Agentic loop in `ChatService`** — keep today's permission-scoped document
   retrieval pre-injected into the system prompt (preserves §8 gating cleanly), and
   additionally offer the `web_search` tool to the model; run the tool loop;
   surface tool activity + web citations as events. §7.

**Hybrid, deliberately:** documents are pre-retrieved into context as they are
today (their permission gate stays simple and airtight); the web is a *tool*. We do
**not** turn document retrieval into a tool in the first cut — that would route
permission-sensitive retrieval through model-chosen calls and complicate §8. It is
noted as a later option (§11).

## 4. Tool-calling provider abstraction (the lift)

Extend `providers/base.py`:

```python
class ChatProvider(ABC):
    supports_tools: bool = False           # capability flag
    def stream(self, messages, *, system=None) -> Iterator[str]: ...   # unchanged

    # New: a single agentic turn. Yields a stream of typed events and, when the
    # model wants a tool, pauses for the caller to supply results, then continues.
    def run_tools(self, messages, *, system=None, tools=None,
                  max_iterations=4) -> Iterator[ChatEvent]: ...
```

`ChatEvent` is an internal union: `text(delta)`, `tool_call(id, name, args)`,
`tool_result(id, ...)` (echoed back by the loop driver), `end`. Concretely the
driver lives in `ChatService` and the provider exposes the provider-specific
mechanics:

- **Anthropic** (`AnthropicChatProvider`): pass `tools=[schema]`; stream; collect
  `tool_use` content blocks; the caller runs the tool and appends a `tool_result`
  block to `messages`; re-invoke until the model stops requesting tools.
  `supports_tools = True`.
- **OpenAI-compatible** (`OpenAICompatibleChatProvider`): pass `tools=[…]`;
  read `tool_calls` deltas; append `role:"tool"` messages with results; loop.
  `supports_tools = True` (note: some local Ollama models don't actually honor
  tools — see fallback).
- **Echo / non-tool models**: `supports_tools = False`.

**Fallback for non-tool providers:** if `supports_tools` is False (or tools are
disabled), `ChatService` runs exactly today's text-only path (no web). Optional
later enhancement: a ReAct-style text-protocol loop for models without native tool
APIs (§11) — out of scope for v1.

This keeps `stream()` untouched (zero risk to the existing happy path) and isolates
all tool complexity in the new `run_tools()` path + the loop driver.

## 5. Search backends — `WebSearchProvider` (default DuckDuckGo)

New `providers/websearch.py` + `make_web_search_provider(config)` in `factory.py`,
mirroring `make_chat_provider`:

```python
class WebSearchProvider(ABC):
    def search(self, query: str, *, k: int) -> list[WebResult]: ...
# WebResult = {title, url, snippet, published?: str}
```

- `DuckDuckGoSearchProvider` — **default**. Lazy-imports the `ddgs` library (the
  maintained DuckDuckGo client; matches the lazy `import anthropic`/`openai`
  pattern). No API key. Honors region/safesearch/time-range config.
- `FakeWebSearchProvider` — deterministic, offline; keeps unit tests dependency-
  free (like `EchoChatProvider`).
- `NullWebSearchProvider` — disabled; `search()` returns `[]`.
- Future drop-ins: Brave, Tavily, SerpAPI (each a `WebSearchProvider`).

Selection: `CSAI_WEB_SEARCH_PROVIDER` (default `duckduckgo`; `fake` for tests;
`none`/`""` disables). **Snippets-only in v1** (title/url/snippet). Optional
`fetch_page` (extract readable page text) is a separate tool gated behind a flag,
with SSRF protections — deferred to a later phase (§11, §9).

## 6. Tool layer

New `agent/tools.py` (separate from the existing `tools.py`, which wraps conversion
subprocesses — name TBD to avoid confusion, e.g. `llm_tools.py`):

```python
class Tool(ABC):
    name: str                              # "web_search"
    description: str
    schema: dict                           # JSON schema for the args
    def run(self, args: dict, ctx: ToolContext) -> ToolOutput: ...
```

`WebSearchTool.run({"query": str, "max_results"?: int}, ctx)`:
1. `guards`-cap the query + result count; `audit.record(action="web_search", …)`.
2. `provider.search(query, k=...)`.
3. Return a compact, model-friendly result block (numbered) **and** structured
   `sources` (url/title) for citations.

`ToolContext` carries `identity`, `config`, and a `sources` accumulator so the loop
can build citations. Tools are registered in a small registry, so enabling/disabling
or adding tools is config-driven.

## 7. Agentic loop in `ChatService`

`ChatService.answer()` gains web-tool support without disturbing the document path:

1. As today: guard, **permission-scoped document retrieval**, trim, build the
   system prompt with numbered `[n]` document context. Swap `_INSTRUCTIONS` for a
   tool-aware variant: *prefer the user's documents; if they're insufficient, you
   may call `web_search`; cite every claim by its `[n]` marker; clearly attribute
   web-derived statements.*
2. Decide tool exposure: `tools = [web_search]` iff web search is enabled
   (config + per-message flag) **and** `chat.supports_tools`. Otherwise → today's
   `stream()` path.
3. Run the loop via `chat.run_tools(messages, system, tools, max_iterations)`:
   - `text` events → `{type:token}` deltas (unchanged downstream).
   - `tool_call` → emit `{type:"tool_call", name, args}`; execute the tool;
     append `tool_result`; emit `{type:"tool_result", name, count}`.
   - Web results discovered during the loop append to the citation set.
4. Emit a **unified** `{type:citations}` spanning documents and web:
   `{marker, kind:"doc", file_uid}` or `{marker, kind:"web", url, title}`.
5. `audit.record` gains `web_searches`, `tool_iterations`, and the
   "query-sent-externally" flag.

Bound by `max_iterations` (default small, e.g. 3–4) to cap latency/cost. The loop
runs in the existing worker thread (`_stream_answer`), so blocking HTTP for search
never blocks the event loop.

## 8. Contract changes (additive, backward compatible)

- **WS request payload** (`/chat`): optional `web_search: bool` (default from
  config). Optional `web_mode: "off"|"on"|"auto"` (auto = offer the tool but let
  the model decide — the default behavior of a tool).
- **New WS events** (extend `EVENT_CONTRACT.md`): `{type:"tool_call", name, args}`,
  `{type:"tool_result", name, count}` for UI transparency ("Searching the web…").
- **Citations event**: add `kind` (`"doc"`/`"web"`), `url`, `title`; existing
  `{file_uid, marker}` shape stays valid for documents.
- **Frontend** (small follow-up, separate PR): `chatService.ts` parse the new
  fields/events; `Citation` type + `ChatView` render web citations as external
  links and show a "searched the web" affordance.

## 9. Security, privacy, guardrails (non-negotiable)

- **Privacy:** document context is permission-scoped and private, but a web search
  **sends query text to DuckDuckGo** — and because the *model* forms the query, it
  may include reformulations of the user's wording. Therefore: web search is
  **opt-in / disabled by default**, with a tenant/admin master switch, and **every**
  tool call is audited (provider, query-sent flag, result count). Document content
  is never sent to the web.
- **No permission bypass:** web results are public, so they need no permission
  filtering; document gating (§8 of DEVELOPMENT_PLAN) is untouched. Web and document
  context are clearly labeled so neither the model nor the UI conflates them.
- **Abuse caps:** `max_iterations`, results-per-search cap, total web-chars into the
  context budget (shares `max_context_chars`, documents prioritized), and a
  search/fetch wall-clock timeout.
- **SSRF (only if/when `fetch_page` lands):** https-only, block private/loopback/
  link-local IPs, redirect + size caps, strip scripts/HTML. v1 is snippets-only, so
  no outbound fetch beyond the search API itself.

## 10. Configuration (`config.py`, `_env` pattern)

```
CSAI_WEB_SEARCH_PROVIDER   duckduckgo   # duckduckgo | fake | none
CSAI_WEB_SEARCH_ENABLED    false        # global master switch (off by default)
CSAI_WEB_SEARCH_DEFAULT    false        # default when the client omits web_search
CSAI_WEB_SEARCH_RESULTS    5            # results per search
CSAI_WEB_MAX_ITERATIONS    3            # tool-loop cap per answer
CSAI_WEB_MAX_CHARS         4000         # web text into the shared context budget
CSAI_WEB_TIMEOUT_MS        4000         # per-search wall-clock
CSAI_WEB_REGION / _SAFESEARCH / _TIMELIMIT   # DDG params
# (deferred) CSAI_WEB_FETCH_PAGES  false  # snippets-only vs fetch+extract
```

## 11. Testing (mirror DEVELOPMENT_PLAN §11)

- `FakeWebSearchProvider` (deterministic) + a `ToolEchoChatProvider` that
  deterministically emits one `web_search` `tool_call` then a final answer — so the
  whole loop is exercised offline, dependency-free, like the existing
  `FakeRetriever`/`CapturingChat` in `tests/test_chat.py`.
- Unit: tool-loop happy path (call → result → final), `max_iterations` cap,
  capability fallback (`supports_tools=False` → today's text path, no web), unified
  citation numbering/ordering (docs then web, contiguous `[n]`), instruction
  selection, query/result caps, audit fields, disabled-by-default posture.
- Contract: WS emits `tool_call`/`tool_result`/`citations(kind=web)` in order.
- `@live`-marked: a real DuckDuckGo search + a real Anthropic/OpenAI tool round-trip
  (network + key; opt-in like the other live tests).

## 12. Phased rollout

1. **P1 — backends + tool, no model loop yet:** `WebSearchProvider` (+DDG+fake),
   `WebSearchTool`, config, audit, unit tests. Self-contained, no provider changes.
2. **P2 — tool-calling providers + loop:** extend `ChatProvider` with `run_tools`
   for Anthropic + OpenAI-compatible; capability flag + fallback; wire the loop into
   `ChatService`; tool-aware instructions; unified citations; new WS events.
3. **P3 — frontend:** render web citations as links + "searching the web" indicator;
   per-conversation toggle; `EVENT_CONTRACT.md` update.
4. **P4 — extensions:** `fetch_page` tool with SSRF guards; additional providers
   (Brave/Tavily); optionally expose `document_search` as a tool for fully agentic
   RAG; ReAct fallback for non-tool models.

## 13. Open decisions (for review)

1. **DuckDuckGo client:** the `ddgs` library (easy, unofficial, rate-limited — adds
   a runtime dep, lazy-imported) vs a small `urllib` DDG lite/HTML client (no dep,
   more fragile). Recommendation: `ddgs`.
2. **Default posture:** web search **off by default**, opt-in per conversation
   (recommended for the privacy reason in §9) vs enabled by default.
3. **First-cut depth:** snippets-only (v1) vs include `fetch_page`+extract (P4) in
   the initial implementation.
4. **Non-tool models:** acceptable that web search is simply unavailable when the
   configured chat provider can't do tools (e.g. Echo, some Ollama models), with the
   ReAct fallback deferred? (Recommendation: yes.)
5. **Scope of "configurable":** provider-pluggable (DDG default, Brave/Tavily later)
   is in scope; is exposing **document search** as an additional LLM tool wanted now
   or later (§11/P4)?

---

*Reviewer notes:* the only structural change to the existing happy path is the
additive `run_tools()` capability on `ChatProvider`; `stream()` and the document
RAG + permission gate are untouched, so a deployment with web search disabled
behaves exactly as today.
