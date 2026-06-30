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

# Appended when the document tools are available. Workflow: explore folders,
# confirm the destination FIRST, then stream the report wrapped in SAVE_REPORT
# markers — the app diverts the marked body to a file automatically (no fragile
# giant tool-argument needed).
_INSTRUCTIONS_DOCUMENT = (
    "If the user asks to save, export, or turn this conversation (or its results) "
    "into a document or report:\n"
    "1. Use the list_folders tool to explore the user's folders (start at '/' and "
    "drill in) so your suggestion matches their layout.\n"
    "2. Decide the destination FIRST: propose a specific folder + file name and get "
    "the user's confirmation (offer to create a folder, e.g. 'Reports', if none "
    "fits). Settle the destination before writing the report.\n"
    "3. Then write the report wrapped in these markers, with the confirmed "
    "destination in the opening marker:\n"
    "   [[SAVE_REPORT path=\"/Confirmed/Folder\" file=\"report-name\" title=\"Report Title\"]]\n"
    "   ...the full report as Markdown or HTML (headings, paragraphs, tables, lists)...\n"
    "   [[/SAVE_REPORT]]\n"
    "Everything between the markers is saved automatically to that path as an HTML "
    "document with a PDF preview — this is the ONLY way to save; there is no save "
    "tool to call. Put the ENTIRE report between the markers: write the opening "
    "marker, then the complete report, then the closing [[/SAVE_REPORT]] marker — do "
    "not place report content after the closing marker, and do not emit the markers "
    "around just a preamble. Do not claim a report is saved unless you actually "
    "emitted the full marked block."
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
        system = self._build_system(system_prompt, chunks, tools=tools)
        doc_citations = self._doc_citations(chunks)

        if not tools:
            answer_parts: List[str] = []
            for delta in self.chat.stream(messages, system=system):
                answer_parts.append(delta)
                yield {"type": "token", "text": delta}
            # Divert any marker-wrapped report from the stream into a saved file.
            yield from self._save_marked_reports(identity, "".join(answer_parts), [])
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
        answer_parts: List[str] = []
        for ev in self.chat.run_tools(messages, system=system, tools=specs, execute=execute,
                                      max_iterations=self.config.web_max_iterations):
            et = ev.get("type")
            if et == "text":
                answer_parts.append(ev.get("text", ""))
                # Keep the running reply on the context so the marked report can be
                # extracted and saved after the stream completes.
                ctx.answer_text = "".join(answer_parts)
                yield {"type": "token", "text": ev.get("text", "")}
            elif et == "tool_call":
                yield {"type": "tool_call", "name": ev.get("name"), "args": ev.get("args")}
            elif et == "tool_result":
                yield {"type": "tool_result", "name": ev.get("name")}

        # Divert any marker-wrapped report into a saved file.
        yield from self._save_marked_reports(identity, ctx.answer_text, ctx.saved)

        citations = doc_citations + web_citations
        self._audit(identity, chunks, citations, trimmed, web_searches=counters["searches"])
        yield {"type": "citations", "citations": citations}

    # ----------------------------------------------------------------- helpers
    def _save_marked_reports(self, identity, answer_text: str, saved: List[str]):
        """Write any ``[[SAVE_REPORT …]] … [[/SAVE_REPORT]]`` block the model streamed
        to a file in the user's storage — the destination travels in the START
        marker (set before the report), and the closing marker OR a stream cutoff
        triggers the save. Deterministic: no tool call required. Yields confirmation
        token events. ``saved`` dedupes locations already written this turn."""
        from .llm_tools import (ReportSaveError, parse_report_markers,
                                report_location, save_report_document)
        for rep in parse_report_markers(answer_text):
            loc = report_location(rep.path, rep.filename)
            if loc in saved:
                continue
            try:
                uid, loc, nbytes = save_report_document(
                    identity, self.config, path=rep.path, filename=rep.filename,
                    title=rep.title, body=rep.body, create_folders=True,
                    max_bytes=getattr(self.config, "chat_document_max_bytes", 5_000_000))
            except ReportSaveError as e:
                audit.record(action="save_report", user=identity.user, tenant=identity.tenant,
                             result="error", reason=e.kind)
                yield {"type": "token",
                       "text": f"\n\n⚠ Could not save the report to {loc}: {e.message}\n"}
                continue
            saved.append(loc)
            audit.record(action="save_report", user=identity.user, tenant=identity.tenant,
                         result="ok", bytes=nbytes, truncated=not rep.complete)
            note = "" if rep.complete else (" (note: the report may have been cut off before "
                                            "completion — regenerate if it looks incomplete)")
            yield {"type": "token", "text": (
                f"\n\n✅ Saved the report to {loc} (file {uid}){note}. A PDF preview is "
                f"being generated.\n")}

    def _select_tools(self, web_search: Optional[bool]):
        """Decide which tools to offer this turn. Requires provider tool support.
        Web tools need the global enable + per-message opt-in (or the configured
        default); the folder-exploration tool is offered whenever the document
        feature is enabled (saving itself is marker-driven, not a tool)."""
        if not getattr(self.chat, "supports_tools", False):
            return []
        include_web = False
        if getattr(self.config, "web_search_enabled", False):
            include_web = (self.config.web_search_default if web_search is None
                           else bool(web_search))
        return build_tools(self.config, include_web=include_web)

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
                      *, tools: Optional[List] = None) -> str:
        context = ("\n\n".join(f"[{i + 1}] (file {c.file_uid})\n{c.text}" for i, c in enumerate(chunks))
                   if chunks else "(no relevant context found)")
        names = {getattr(t, "name", "") for t in (tools or [])}
        parts = []
        if system_prompt and system_prompt.strip():
            parts.append(system_prompt.strip())
        parts.append(_INSTRUCTIONS_WEB if "web_search" in names else _INSTRUCTIONS)
        # Marker-driven report saving works in any path; offer the guidance whenever
        # the document feature is enabled (not tied to a specific tool being present).
        if getattr(self.config, "chat_document_tool_enabled", True):
            parts.append(_INSTRUCTIONS_DOCUMENT)
        parts.append("Context:\n" + context)
        return "\n\n".join(parts)
