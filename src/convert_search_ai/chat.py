"""RAG chat-with-documents (M3) + optional web-search tool (WEB_SEARCH_TOOL_PLAN).

Retrieve permission-scoped document context for the user's message, build a prompt
seeded by the conversation-specific system prompt, and stream the answer from the
configured ChatProvider. When web search is enabled (off by default) and the
provider supports tools, the model may additionally call the ``web_search`` tool;
document and web sources share one ``[n]`` citation numbering.

``answer`` is a sync generator of event dicts:

  {"type": "token", "text": "..."}       streamed answer deltas
  {"type": "tool_call", "name", "args"}  the model invoked a tool (e.g. web_search)
  {"type": "tool_result", "name"}        a tool returned
  {"type": "citations", "citations": [{"marker", "kind", "file_uid"|"url", "title"?}]}
"""
from __future__ import annotations

from typing import Iterator, List, Optional
from urllib.parse import urlparse

from . import audit, guards
from .config import Config
from .llm_tools import ToolContext, build_tools
from .retrieval import Retriever
from .vectorstore import RetrievedChunk

_INSTRUCTIONS = (
    "Answer using ONLY the provided context excerpts from the user's documents. "
    "If the answer is not in the context, say you don't know. Cite the excerpts "
    "you use by their [n] marker."
)

_INSTRUCTIONS_WEB = (
    "Answer using the provided context excerpts from the user's documents. If they "
    "are insufficient or the question needs current/external information, you may "
    "call the web_search tool. Prefer the user's documents when they conflict with "
    "the web. Cite every claim by its [n] marker — document and web sources share "
    "one numbering — and make clear which statements come from the web."
)


class ChatService:
    def __init__(self, config: Config, *, retriever: Optional[Retriever] = None, chat=None):
        self.config = config
        self.retriever = retriever or Retriever(config)
        self._chat = chat

    @property
    def chat(self):
        if self._chat is None:
            from .providers import make_chat_provider
            self._chat = make_chat_provider(self.config)
        return self._chat

    def answer(self, identity, *, message: str, system_prompt: str = "",
               history: Optional[List[dict]] = None, k: int = 8,
               web_search: Optional[bool] = None) -> Iterator[dict]:
        msg = guards.check_query(message, self.config.max_query_chars)
        k = guards.cap_k(k, self.config.max_chat_k)
        chunks = self.retriever.retrieve(identity, msg, k=k)
        chunks, trimmed = guards.trim_context(chunks, self.config.max_context_chars)

        tools = self._select_tools(web_search)
        messages = list(history or []) + [{"role": "user", "content": msg}]
        system = self._build_system(system_prompt, chunks, tools_enabled=bool(tools))
        doc_citations = self._doc_citations(chunks)

        if not tools:
            for delta in self.chat.stream(messages, system=system):
                yield {"type": "token", "text": delta}
            self._audit(identity, chunks, doc_citations, trimmed, web_searches=0)
            yield {"type": "citations", "citations": doc_citations}
            return

        # --- tool loop ---------------------------------------------------------
        ctx = ToolContext(identity=identity, config=self.config)
        web_citations: List[dict] = []
        counters = {"marker": len(chunks), "searches": 0}  # web markers continue after docs
        tools_by_name = {t.name: t for t in tools}

        def execute(name: str, args: dict) -> str:
            tool = tools_by_name.get(name)
            if tool is None:
                return f"(unknown tool: {name})"
            counters["searches"] += 1
            out = tool.run(args or {}, ctx)
            if not out.sources:
                return out.text
            lines = []
            for s in out.sources:
                counters["marker"] += 1
                m = counters["marker"]
                web_citations.append({"marker": m, "kind": "web",
                                      "url": s["url"], "title": s.get("title", "")})
                head = s.get("title") or s["url"]
                lines.append(f"[{m}] {head} ({urlparse(s['url']).netloc})\n"
                             f"{s.get('snippet', '')}\nSource: {s['url']}")
            return "\n\n".join(lines)

        specs = [{"name": t.name, "description": t.description, "schema": t.schema} for t in tools]
        for ev in self.chat.run_tools(messages, system=system, tools=specs, execute=execute,
                                      max_iterations=self.config.web_max_iterations):
            et = ev.get("type")
            if et == "text":
                yield {"type": "token", "text": ev.get("text", "")}
            elif et == "tool_call":
                yield {"type": "tool_call", "name": ev.get("name"), "args": ev.get("args")}
            elif et == "tool_result":
                yield {"type": "tool_result", "name": ev.get("name")}

        citations = doc_citations + web_citations
        self._audit(identity, chunks, citations, trimmed, web_searches=counters["searches"])
        yield {"type": "citations", "citations": citations}

    # ----------------------------------------------------------------- helpers
    def _select_tools(self, web_search: Optional[bool]):
        """Decide whether to offer tools this turn: global enable + per-message
        opt-in (or the configured default) + provider tool support."""
        if not getattr(self.config, "web_search_enabled", False):
            return []
        want = self.config.web_search_default if web_search is None else bool(web_search)
        if not want or not getattr(self.chat, "supports_tools", False):
            return []
        return build_tools(self.config)

    @staticmethod
    def _doc_citations(chunks: List[RetrievedChunk]) -> List[dict]:
        seen, out = set(), []
        for i, c in enumerate(chunks):
            if c.file_uid not in seen:
                seen.add(c.file_uid)
                out.append({"marker": i + 1, "kind": "doc", "file_uid": c.file_uid})
        return out

    def _audit(self, identity, chunks, citations, trimmed, *, web_searches: int) -> None:
        audit.record(action="chat", user=identity.user, tenant=identity.tenant, result="ok",
                     retrieved=len(chunks), citations=len(citations),
                     web_searches=web_searches, context_trimmed=trimmed)

    def _build_system(self, system_prompt: str, chunks: List[RetrievedChunk],
                      *, tools_enabled: bool = False) -> str:
        context = ("\n\n".join(f"[{i + 1}] (file {c.file_uid})\n{c.text}" for i, c in enumerate(chunks))
                   if chunks else "(no relevant context found)")
        parts = []
        if system_prompt and system_prompt.strip():
            parts.append(system_prompt.strip())
        parts.append(_INSTRUCTIONS_WEB if tools_enabled else _INSTRUCTIONS)
        parts.append("Context:\n" + context)
        return "\n\n".join(parts)
